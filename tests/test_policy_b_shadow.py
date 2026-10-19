"""Offline integration: actual native observations, canonical snapshots, no repair."""
import copy
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import durable
import harness
import manager
import retry_eligibility as retry
import supervision
import winprocess
from work_unit_scheduler import WorkUnitScheduler, ScriptedExecutor, WorkUnitSchedulingError
from test_work_units_wave2 import fixture, make_spec, TASK, COMMANDS


def make_shadow_fixture(root, *, scripts=None, single=False, timeout=10, behavior=None):
    repo, store, config = fixture(root, "shadow-test")
    if scripts:
        for name, content in scripts.items():
            repo.write(name, content)
        for args in (("add", "."), ("commit", "-qm", "offline test fixture")):
            subprocess.run(["git", "-C", str(repo.root), "-c", f"safe.directory={repo.root}",
                            "-c", "user.name=Shadow test", "-c", "user.email=test@localhost", *args],
                           check=True, capture_output=True)
    registry = {"check-calc": {"argv": ["python", "-m", "unittest", "test_calculator.py"],
                               "timeout_seconds": timeout}}
    if not single:
        registry["check-readonly"] = {"argv": ["python", "-m", "unittest", "test_readonly.py"],
                                      "timeout_seconds": timeout}
    spec = make_spec(verifier_ids=list(registry), forbidden=["test_calculator.py", "test_readonly.py"])
    executor = ScriptedExecutor()
    executor.set_behavior("unit-1", behavior or {"writes": {"calculator.py": "def multiply(a, b): return a + b\n"}})
    manager.create_run(store, repo, TASK, COMMANDS, config, criteria=["Task"],
                       allow=["calculator.py"], forbid=spec["scope"]["forbidden_paths"],
                       work_units=[spec], verifier_registry=registry)
    supervisor = supervision.Supervisor(store)
    scheduler = WorkUnitScheduler(store, repo, supervisor=supervisor, verifier_registry=registry,
                                  agents=manager.Agents(None, executor, None, None))
    return repo, store, supervisor, scheduler, executor


@unittest.skipUnless(os.name == "nt", "Actual suspended Windows verifier processes required")
class PolicyBShadowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.addCleanup(patch.stopall)
        patch("harness.Gateway", side_effect=AssertionError("No models authorized")).start()
        patch("durable.Gateway", side_effect=AssertionError("No models authorized")).start()
        patch("socket.socket", side_effect=AssertionError("No networking authorized")).start()
        patch("socket.create_connection", side_effect=AssertionError("No networking authorized")).start()
        patch.dict(os.environ, {"PYTHONDONTWRITEBYTECODE": "1", "GIT_OPTIONAL_LOCKS": "0"}).start()

    def prepare(self, **kwargs):
        self.repo, self.store, self.sup, self.scheduler, self.executor = make_shadow_fixture(self.root, **kwargs)

    def run_shadow(self):
        return self.scheduler.observe_policy_b_shadow("unit-1")

    def shadow(self):
        return self.store.state["work_units"]["units"]["unit-1"]["attempts"][0]["policy_b_shadow"]

    def assert_stopped(self):
        self.assertEqual(self.scheduler.reconciliation_stop.reason, "INVOCATION_REVIEW_REQUIRED")
        before = (self.store.directory / "events.jsonl").read_bytes()
        with self.assertRaises(manager.Stop):
            self.run_shadow()
        self.assertEqual(self.scheduler.run_sequence()["status"], "HUMAN_ACTION_REQUIRED")
        loaded = durable.Store(self.store.directory.parent, self.store.directory.name)
        loaded.load()
        with patch.object(self.repo, "execute", side_effect=AssertionError("No replay")), \
                patch("winprocess.ControllerVerifier") as native:
            scheduler = WorkUnitScheduler(loaded, self.repo, supervisor=supervision.Supervisor(loaded))
            self.assertEqual(scheduler.reconciliation_stop.reason, "INVOCATION_REPLAY_REFUSED")
            self.assertEqual(scheduler.run_sequence()["status"], "HUMAN_ACTION_REQUIRED")
            native.assert_not_called()
        self.assertEqual((self.store.directory / "events.jsonl").read_bytes(), before)
        self.assertEqual(len(self.executor.calls), 1)
        self.assertEqual(self.store.state["verified_progress"], [])
        self.assertIsNone(self.store.state["last_verified_checkpoint"])

    def test_two_real_verifiers_diagnostic_allow_has_no_repair(self):
        self.prepare()
        with patch("retry_eligibility.classify_retry_eligibility", wraps=retry.classify_retry_eligibility) as classifier:
            result = self.run_shadow()
        self.assertEqual(result["decision"]["decision"], "ALLOW")
        self.assertTrue(result["verification_complete"])
        self.assertEqual(classifier.call_args.kwargs["policy"], "B")
        shadow = self.shadow()
        self.assertEqual(set(shadow["observations"]), {"check-calc", "check-readonly"})
        unit = self.store.state["work_units"]["units"]["unit-1"]
        att = unit["attempts"][0]
        self.assertEqual(unit["status"], "VERIFYING")
        self.assertEqual(att["outcome"], "failed")
        self.assertIsNotNone(att["completed_at"])
        self.assertEqual(att["verifier_result"]["check-calc"]["exit_code"], 1)
        self.assertEqual(att["verifier_result"]["check-readonly"]["exit_code"], 0)
        self.assertEqual(shadow["candidate_snapshot"], shadow["verification_snapshot_before"])
        self.assertEqual(shadow["candidate_snapshot"], shadow["verification_snapshot_after"])
        self.assertNotEqual(att["pre_attempt_snapshot"], shadow["candidate_snapshot"])
        for vid, obs in shadow["observations"].items():
            binding = obs["binding"]
            child = self.sup.child(binding["child_id"])
            self.assertEqual(binding["invocation_id"], child["token"])
            self.assertEqual(child["invocation"]["verifier_id"], vid)
            self.assertEqual(child["invocation"]["candidate_fingerprint"], shadow["candidate_snapshot"]["fingerprint"])
            self.assertEqual(child["invocation"]["registry_sha256"], shadow["registry_sha256"])
            self.assertNotIn("invocation_id", obs["native"])
            self.assertNotIn("run_id", obs["native"])
            adapted = att["verifier_result"][vid]["controller_evidence"]
            self.assertEqual(adapted["run_id"], self.store.state["run_id"])
            self.assertEqual(adapted["unit_id"], unit["unit_id"])
            self.assertEqual(adapted["attempt_id"], att["attempt_id"])
            self.assertEqual(obs["after_snapshot"], shadow["candidate_snapshot"])
            for event, seq in binding["wal"].items():
                self.assertEqual(self.store.get_record(seq)["transition"],
                                 {"event": event, "child_id": child["child_id"]})
        for record in self.store.records:
            u = record.get("state", {}).get("work_units", {}).get("units", {}).get("unit-1")
            if u and u["attempts"]:
                self.assertIn(u["status"], {"EXECUTING", "VERIFYING"})
                self.assertEqual(len(u["attempts"]), 1)
        self.assertGreater(att["timeout_info"]["elapsed_seconds"], 0)
        self.assert_stopped()

    def test_single_native_failure_is_classified(self):
        self.prepare(single=True)
        self.assertEqual(self.run_shadow()["decision"]["decision"], "ALLOW")
        self.assert_stopped()

    def test_all_pass_is_diagnostic_only(self):
        self.prepare(behavior={"writes": {"calculator.py": "def multiply(a, b): return a * b\n"}})
        self.assertEqual(self.run_shadow()["decision"]["decision"], "DENY")
        self.assertEqual(self.store.state["work_units"]["units"]["unit-1"]["attempts"][0]["outcome"], "passed")
        self.assert_stopped()

    def test_foreign_native_child_is_refused(self):
        self.prepare()
        run = supervision._run_observed_verifier
        def foreign(*args, **kwargs):
            result = run(*args, **kwargs)
            result["controller_evidence"]["child_id"] = "verification_command-foreign"
            return result
        with patch("supervision._run_observed_verifier", side_effect=foreign), self.assertRaises(WorkUnitSchedulingError):
            self.run_shadow()
        self.assertNotIn("decision", self.shadow())
        self.assertEqual(len(self.store.state["supervision"]["children"]), 1)
        self.assert_stopped()

    def test_replayed_first_result_cannot_bind_second_verifier(self):
        self.prepare()
        run = supervision._run_observed_verifier
        results = []
        def replay(*args, **kwargs):
            result = run(*args, **kwargs)
            results.append(result)
            return copy.deepcopy(results[0])
        with patch("supervision._run_observed_verifier", side_effect=replay), self.assertRaises(WorkUnitSchedulingError):
            self.run_shadow()
        self.assertEqual(len(results), 2)
        self.assertNotIn("decision", self.shadow())
        self.assert_stopped()

    def test_missing_native_evidence_refuses_classification(self):
        self.prepare()
        run = supervision._run_observed_verifier
        def missing(*args, **kwargs):
            result = run(*args, **kwargs)
            del result["controller_evidence"]
            return result
        with patch("supervision._run_observed_verifier", side_effect=missing), self.assertRaises(KeyError):
            self.run_shadow()
        self.assertNotIn("decision", self.shadow())
        self.assert_stopped()

    def test_matching_ids_without_live_wal_record_are_refused(self):
        self.prepare()
        run = supervision._run_observed_verifier
        saved = []
        def missing_wal(*args, **kwargs):
            result = run(*args, **kwargs)
            saved.extend(self.store.records)
            self.store.records[:] = [r for r in self.store.records
                                      if (r.get("transition") or {}).get("event") != "invocation_bound"]
            return result
        try:
            with patch("supervision._run_observed_verifier", side_effect=missing_wal), self.assertRaises(durable.DurableError):
                self.run_shadow()
            self.assertNotIn("decision", self.shadow())
        finally:
            self.store.records[:] = saved
        self.assert_stopped()

    def test_repository_drift_stops_before_next_verifier(self):
        self.prepare(scripts={"test_calculator.py": "import unittest\nfrom pathlib import Path\n"
                              "class T(unittest.TestCase):\n"
                              " def test_drift(self):\n"
                              "  Path('calculator.py').write_text('def multiply(a,b): return 99\\n')\n"
                              "  self.fail('actual native failure')\n"})
        result = self.run_shadow()
        self.assertEqual(result["decision"]["decision"], "DENY")
        self.assertFalse(result["verification_complete"])
        self.assertEqual(len(self.shadow()["observations"]), 1)
        self.assertEqual(len(self.store.state["supervision"]["children"]), 1)
        self.assert_stopped()

    def test_ignored_file_contamination_is_not_exempted(self):
        self.prepare(scripts={"test_calculator.py": "import unittest\nfrom pathlib import Path\n"
                              "class T(unittest.TestCase):\n"
                              " def test_drift(self):\n"
                              "  Path('__pycache__').mkdir(exist_ok=True)\n"
                              "  Path('__pycache__/untrusted.pyc').write_bytes(b'bad')\n"
                              "  self.fail('native failure')\n"})
        result = self.run_shadow()
        self.assertEqual(result["decision"]["decision"], "DENY")
        self.assertFalse(result["verification_complete"])
        self.assert_stopped()

    def test_manual_candidate_drift_is_refused_before_executor(self):
        self.prepare()
        self.repo.write("calculator.py", "def multiply(a,b): return 100\n")
        with self.assertRaises(durable.DurableError):
            self.run_shadow()
        self.assertEqual(self.executor.calls, [])
        self.assertEqual(self.store.state["supervision"]["children"], {})

    def test_post_executor_candidate_is_not_rebased_before_verification(self):
        self.prepare()
        verify = self.scheduler._run_guarded_verification
        def drift(*args, **kwargs):
            path = self.repo.root / "__pycache__" / "between-stages.pyc"
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(b'contamination after candidate capture')
            return verify(*args, **kwargs)
        with patch.object(self.scheduler, "_run_guarded_verification", side_effect=drift):
            result = self.run_shadow()
        self.assertEqual(result["decision"]["decision"], "DENY")
        self.assertEqual(self.store.state["supervision"]["children"], {})
        self.assertNotIn("__pycache__/between-stages.pyc", self.shadow()["candidate_fs_snapshot"])
        self.assertIn("__pycache__/between-stages.pyc", self.shadow()["verification_fs_snapshot_before"])
        self.assert_stopped()

    def test_missing_verification_after_snapshot_never_yields_allow(self):
        self.prepare(single=True)
        capture = self.scheduler._shadow_snapshot
        def missing_after():
            if self.store.state["supervision"]["children"]:
                raise WorkUnitSchedulingError("injected missing after snapshot")
            return capture()
        with patch.object(self.scheduler, "_shadow_snapshot", side_effect=missing_after), \
                self.assertRaises(WorkUnitSchedulingError):
            self.run_shadow()
        self.assertNotIn("decision", self.shadow())
        self.assert_stopped()

    def test_actual_timeout_is_denied_without_second_verifier(self):
        self.prepare(timeout=1, scripts={"test_calculator.py": "import unittest,time\n"
                                          "class T(unittest.TestCase):\n"
                                          " def test_wait(self): time.sleep(5)\n"})
        result = self.run_shadow()
        self.assertEqual(result["decision"]["reason_code"], retry.DENY_TIMEOUT)
        self.assertEqual(len(self.store.state["supervision"]["children"]), 1)
        native = self.shadow()["observations"]["check-calc"]["native"]
        self.assertEqual(native["termination"], "timeout")
        self.assertEqual(native["cleanup"], "observed_empty")
        self.assert_stopped()

    def test_incomplete_job_cleanup_has_no_diagnostic_allow(self):
        self.prepare()
        with patch.object(winprocess.ControllerVerifier, "drain", side_effect=RuntimeError("cleanup not observed")), \
                self.assertRaises(supervision.VerifierEvidenceError):
            self.run_shadow()
        self.assertNotIn("decision", self.shadow())
        self.assertEqual(len(self.store.state["supervision"]["children"]), 1)
        self.assert_stopped()

    def test_executor_interruption_marker_refuses_recovery_before_intent(self):
        def interrupted(*args):
            raise KeyboardInterrupt("executor interrupted")
        self.prepare(behavior=interrupted)
        with self.assertRaises(KeyboardInterrupt):
            self.run_shadow()
        self.assertEqual(self.shadow()["phase"], "STARTED")
        self.assertEqual(self.store.state["supervision"]["children"], {})
        self.assert_stopped()

    def test_post_wal_intent_fault_never_launches_or_writes_again(self):
        self.prepare()
        append = self.store.append
        fault_bytes = []
        def fault(event, **fields):
            result = append(event, **fields)
            if (fields.get("transition") or {}).get("event") == "invocation_intent":
                fault_bytes.append((self.store.directory / "events.jsonl").read_bytes())
                raise OSError("post-WAL intent fault")
            return result
        with patch.object(self.store, "append", side_effect=fault), patch("winprocess.ControllerVerifier") as native:
            with self.assertRaisesRegex(OSError, "post-WAL intent fault"):
                self.run_shadow()
            native.assert_not_called()
        self.assertEqual((self.store.directory / "events.jsonl").read_bytes(), fault_bytes[0])
        self.assert_stopped()

    def test_observation_commit_fault_stops_sequence_without_replay(self):
        self.prepare()
        append = self.store.append
        fault_bytes = []
        def fault(event, **fields):
            result = append(event, **fields)
            units = fields.get("state", {}).get("work_units", {}).get("units", {})
            attempts = units.get("unit-1", {}).get("attempts", [])
            if attempts and attempts[0].get("policy_b_shadow", {}).get("observations"):
                fault_bytes.append((self.store.directory / "events.jsonl").read_bytes())
                raise OSError("post-WAL observation fault")
            return result
        with patch.object(self.store, "append", side_effect=fault), self.assertRaises(OSError):
            self.run_shadow()
        self.assertEqual(len(self.store.state["supervision"]["children"]), 1)
        self.assertEqual((self.store.directory / "events.jsonl").read_bytes(), fault_bytes[0])
        self.assert_stopped()

    def test_allow_diagnostic_post_wal_fault_never_dispatches_repair(self):
        self.prepare()
        append = self.store.append
        fault_bytes = []
        def fault(event, **fields):
            result = append(event, **fields)
            units = fields.get("state", {}).get("work_units", {}).get("units", {})
            attempts = units.get("unit-1", {}).get("attempts", [])
            if attempts and attempts[0].get("policy_b_shadow", {}).get("phase") == "DIAGNOSTIC_COMPLETE":
                fault_bytes.append((self.store.directory / "events.jsonl").read_bytes())
                raise OSError("post-WAL diagnostic fault")
            return result
        with patch.object(self.store, "append", side_effect=fault), self.assertRaises(OSError):
            self.run_shadow()
        loaded = durable.Store(self.store.directory.parent, self.store.directory.name)
        loaded.load()
        diagnostic = loaded.state["work_units"]["units"]["unit-1"]["attempts"][0]["policy_b_shadow"]
        self.assertEqual(diagnostic["decision"]["decision"], "ALLOW")
        self.assertEqual((self.store.directory / "events.jsonl").read_bytes(), fault_bytes[0])
        self.assert_stopped()

    def test_classifier_cannot_swallow_wal_fault_and_cause_followup_append(self):
        self.prepare()
        classify, append = retry.classify_retry_eligibility, self.store.append
        fault_bytes = []
        def fault(event, **fields):
            result = append(event, **fields)
            fault_bytes.append((self.store.directory / "events.jsonl").read_bytes())
            raise OSError("classifier Supervisor post-WAL fault")
        def reconcile():
            with patch.object(self.store, "append", side_effect=fault):
                self.store.commit(manager=copy.deepcopy(self.store.state["manager"]))
        def injected(*args, **kwargs):
            with patch.object(self.sup, "reconcile", side_effect=reconcile):
                return classify(*args, **kwargs)  # Real frozen classifier catches the exception.
        with patch("retry_eligibility.classify_retry_eligibility", side_effect=injected), \
                self.assertRaises(WorkUnitSchedulingError):
            self.run_shadow()
        self.assertEqual(len(fault_bytes), 1)
        self.assertEqual((self.store.directory / "events.jsonl").read_bytes(), fault_bytes[0])
        self.assertNotIn("decision", self.shadow())
        self.assert_stopped()

    def test_actual_crash_before_native_and_between_verifiers_refuses_replay(self):
        code = r'''
import os,sys
from pathlib import Path
from unittest.mock import patch
from test_policy_b_shadow import make_shadow_fixture
root, boundary = Path(sys.argv[1]), sys.argv[2]
repo, store, sup, scheduler, executor = make_shadow_fixture(root)
append = store.append
def crash(event, **fields):
    result = append(event, **fields)
    units = fields.get('state', {}).get('work_units', {}).get('units', {})
    attempts = units.get('unit-1', {}).get('attempts', [])
    if boundary == 'before_native' and attempts and attempts[0].get('policy_b_shadow', {}).get('phase') == 'STARTED':
        os._exit(77)
    if boundary == 'between_verifiers' and (fields.get('transition') or {}).get('event') == 'invocation_complete':
        os._exit(77)
    return result
with patch('durable.Gateway', side_effect=AssertionError('no models')), \
     patch('harness.Gateway', side_effect=AssertionError('no models')), \
     patch('socket.socket', side_effect=AssertionError('no network')), \
     patch.object(store, 'append', side_effect=crash):
    scheduler.observe_policy_b_shadow('unit-1')
'''
        for boundary in ("before_native", "between_verifiers"):
            with self.subTest(boundary=boundary):
                root = self.root / boundary
                root.mkdir()
                env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(Path(__file__).resolve().parent),
                                                                  str(Path(__file__).resolve().parents[1]))))
                result = subprocess.run([sys.executable, "-B", "-c", code, str(root), boundary],
                                        env=env, capture_output=True, timeout=30, creationflags=0x08000000)
                self.assertEqual(result.returncode, 77, result.stderr.decode(errors="replace"))
                store = durable.Store(root / "runs", "shadow-test")
                store.load()
                self.assertEqual(len(store.state["supervision"]["children"]), 0 if boundary == "before_native" else 1)
                before = (store.directory / "events.jsonl").read_bytes()
                repo = harness.Repository(root / "repo", store.state["options"]["config"], harness.EventLog(root / "read.jsonl"))
                with patch.object(repo, "execute", side_effect=AssertionError("no replay")), \
                        patch("winprocess.ControllerVerifier") as native:
                    scheduler = WorkUnitScheduler(store, repo, supervisor=supervision.Supervisor(store))
                    self.assertEqual(scheduler.reconciliation_stop.reason, "INVOCATION_REPLAY_REFUSED")
                    self.assertEqual(scheduler.run_sequence()["status"], "HUMAN_ACTION_REQUIRED")
                    native.assert_not_called()
                self.assertEqual((store.directory / "events.jsonl").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
