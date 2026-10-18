"""Focused offline invocation/WAL faults with actual suspended Windows children.

No models, retry policy, repair executor or trusted checkpoint is involved.
"""
import copy
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import durable
import harness
import manager
import supervision as s
import winprocess
import work_units as wu
from work_unit_scheduler import WorkUnitScheduler
from test_work_units_wave2 import fixture, make_spec, TASK, COMMANDS


def make_fixture(root):
    repo, store, config = fixture(root, "invocation-test")
    registry = {"check-calc": {"argv": ["python", "-m", "unittest", "test_calculator.py"],
                               "timeout_seconds": 10}}
    spec = make_spec(limits={"max_attempts": 1, "timeout_seconds": 60})
    manager.create_run(store, repo, TASK, COMMANDS, config, criteria=["Task"],
                       allow=["calculator.py"], work_units=[spec], verifier_registry=registry)
    sup = s.Supervisor(store)
    scheduler = WorkUnitScheduler(store, repo, supervisor=sup, verifier_registry=registry)
    # The marker proves candidate Python code did/did not execute.
    repo.write("calculator.py", "from pathlib import Path\nPath('executed.marker').write_text('executed')\n"
               "def multiply(a, b): return a * b\n")
    section = scheduler.get_section()
    unit = section["units"]["unit-1"]
    wu.transition_unit(unit, "VALIDATED")
    wu.transition_unit(unit, "READY")
    wu.transition_unit(unit, "EXECUTING")
    att = wu.create_attempt(unit, "unit-1-attempt-1")
    att["pre_attempt_snapshot"] = durable.snapshot(repo)
    wu.transition_unit(unit, "VERIFYING")
    att["stage_timing"] = dict(stage="VERIFYING", unit_id="unit-1", attempt_id=att["attempt_id"],
                               stage_start_clock=time.monotonic(), attempt_elapsed_at_start=5.0)
    scheduler._persist_unit(section, unit)
    return repo, store, sup, scheduler


@unittest.skipUnless(os.name == "nt", "Retained Windows native handles required")
class InvocationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # Any attempted model construction or networking fails the test.
        self.addCleanup(patch.stopall)
        patch("harness.Gateway", side_effect=AssertionError("No models authorized")).start()
        patch("durable.Gateway", side_effect=AssertionError("No models authorized")).start()
        patch("socket.socket", side_effect=AssertionError("No network authorized")).start()
        patch("socket.create_connection", side_effect=AssertionError("No network authorized")).start()
        self.repo, self.store, self.sup, self.scheduler = make_fixture(self.root)
        self.marker = self.repo.root / "executed.marker"

    def invoke(self, **changes):
        args = dict(unit_id="unit-1", attempt_id="unit-1-attempt-1", verifier_id="check-calc")
        args.update(changes)
        return self.scheduler.invoke_native_verifier(**args)

    def child(self, store=None):
        children = (store or self.store).state["supervision"]["children"]
        self.assertEqual(len(children), 1)
        return next(iter(children.values()))

    def reload(self):
        loaded = durable.Store(self.store.directory.parent, self.store.directory.name)
        loaded.load()
        return loaded

    def assert_recovery_refuses(self, store=None):
        store = store or self.reload()
        with patch.object(self.repo, "execute", side_effect=AssertionError("No replay execution")) as execute:
            scheduler = WorkUnitScheduler(store, self.repo, supervisor=s.Supervisor(store))
            self.assertEqual(scheduler.reconciliation_stop.reason, "INVOCATION_REPLAY_REFUSED")
            self.assertEqual(scheduler.run_sequence()["status"], "HUMAN_ACTION_REQUIRED")
            section = scheduler.get_section()
            with self.assertRaises(manager.Stop):
                scheduler.execute_attempt(section["units"]["unit-1"], section, 2)
            execute.assert_not_called()

    def fail_commit(self, event, *, after=False, cache_failure=False):
        original = self.store.commit
        def commit(*args, **fields):
            if fields.get("supervision_event", {}).get("event") == event:
                if cache_failure:
                    with patch("durable.atomic_write", side_effect=OSError("cache crash")):
                        return original(*args, **fields)
                if after:
                    original(*args, **fields)
                raise OSError("injected commit interruption")
            return original(*args, **fields)
        return patch.object(self.store, "commit", side_effect=commit)

    def test_complete_sequence_and_authoritative_bindings(self):
        before = copy.deepcopy(self.store.state)
        result = self.invoke()
        self.assertEqual(result["exit_code"], 0)
        self.assertTrue(self.marker.exists())
        child = self.child(self.reload())
        inv = child["invocation"]
        self.assertEqual(inv["phase"], "COMPLETE")
        self.assertEqual(child["run_id"], self.store.state["run_id"])
        self.assertEqual(inv["registry_sha256"], durable.digest(before["options"]["verifier_registry"]))
        self.assertEqual(inv["candidate_fingerprint"], before["work_units"]["units"]["unit-1"]["attempts"][0]
                         ["pre_attempt_snapshot"]["fingerprint"])
        self.assertEqual(child["command_sha256"], s.checksum(inv["resolved_argv"]))
        self.assertEqual(child["pid"], result["controller_evidence"]["verifier"]["pid"])
        self.assertEqual(child["identity"], result["controller_evidence"]["verifier"]["identity"])
        self.assertEqual(child["job"], result["controller_evidence"]["job"])
        self.assertEqual(child["token"], child["job"].removeprefix("Local\\harness-"))
        events = [r["transition"]["event"] for r in self.store.records if r.get("transition")]
        self.assertEqual(events, ["invocation_intent", "invocation_bound", "invocation_release",
                                  "process_exit", "invocation_complete"])
        self.assertEqual(self.store.state["work_units"], before["work_units"])
        self.assertEqual(self.store.state["verified_progress"], [])
        self.assertIsNone(self.store.state["last_verified_checkpoint"])
        self.assertGreater(self.store.state["manager"]["runtime_seconds"], before["manager"]["runtime_seconds"])
        self.assert_recovery_refuses()

    def test_before_intent_commit_never_creates_process(self):
        with self.fail_commit("invocation_intent"), patch.object(winprocess.ControllerVerifier, "launch") as launch:
            with self.assertRaises(OSError):
                self.invoke()
            launch.assert_not_called()
        self.assertEqual(self.reload().state["supervision"]["children"], {})
        self.assertFalse(self.marker.exists())

    def assert_constructor_failure_stops_reuse(self):
        self.assertIsNotNone(self.scheduler.reconciliation_stop)
        self.assertEqual(self.scheduler.reconciliation_stop.reason, "INVOCATION_REVIEW_REQUIRED")
        wal = (self.store.directory / "events.jsonl").read_bytes()
        state = copy.deepcopy(self.store.state)
        self.assertEqual(self.scheduler.run_sequence()["status"], "HUMAN_ACTION_REQUIRED")
        with self.assertRaises(manager.Stop):
            self.invoke()
        section = self.scheduler.get_section()
        unit = section["units"]["unit-1"]
        with self.assertRaises(manager.Stop):
            self.scheduler.evaluate_readiness(section)
        with self.assertRaises(manager.Stop):
            self.scheduler.execute_attempt(unit, section, 1)
        with self.assertRaises(manager.Stop):
            self.scheduler._run_guarded_verification(unit, section, "unit-1-attempt-1")
        with self.assertRaises(manager.Stop):
            self.scheduler.reconcile_on_resume()
        self.assertEqual((self.store.directory / "events.jsonl").read_bytes(), wal)
        self.assertEqual(self.store.state, state)
        self.assertFalse(self.marker.exists())

    def test_post_wal_constructor_exception_latches_without_followup(self):
        before = copy.deepcopy(self.store.state)
        append = self.store.append
        def post_wal(event, **fields):
            result = append(event, **fields)  # Complete real append, flush and fsync.
            if (fields.get("transition") or {}).get("event") == "invocation_intent":
                raise OSError("post-WAL/pre-state-publication constructor fault")
            return result
        with patch.object(self.store, "append", side_effect=post_wal) as writes, \
                patch("winprocess.ControllerVerifier") as native:
            with self.assertRaisesRegex(OSError, "post-WAL/pre-state-publication"):
                self.invoke()
            self.assertEqual(self.store.state, before)
            loaded = self.reload()
            self.assertFalse(loaded.tail)
            self.assertEqual(loaded.state["revision"], before["revision"] + 1)
            self.assertEqual(self.child(loaded)["invocation"]["phase"], "INTENT")
            self.assert_constructor_failure_stops_reuse()
            self.assertEqual(writes.call_count, 1)
            self.assert_recovery_refuses(loaded)
            native.assert_not_called()

    def test_intent_cache_failure_latches_without_followup(self):
        with self.fail_commit("invocation_intent", cache_failure=True), \
                patch.object(self.store, "append", wraps=self.store.append) as writes, \
                patch("winprocess.ControllerVerifier") as native:
            with self.assertRaisesRegex(OSError, "cache crash"):
                self.invoke()
            self.assertEqual(self.child()["invocation"]["phase"], "INTENT")
            self.assert_constructor_failure_stops_reuse()
            self.assertEqual(writes.call_count, 1)
            self.assert_recovery_refuses()
            native.assert_not_called()

    def test_after_intent_before_native_creation(self):
        with patch("winprocess.ControllerVerifier", side_effect=OSError("creation interrupted")):
            with self.assertRaises(s.VerifierEvidenceError):
                self.invoke()
        child = self.child(self.reload())
        self.assertEqual(child["invocation"]["phase"], "INTENT")
        self.assertIsNone(child["pid"])
        self.assertIsNone(child["identity"])
        self.assertFalse(self.marker.exists())
        self.assert_recovery_refuses()

    def test_after_native_creation_before_identity_commit(self):
        with self.fail_commit("invocation_bound"):
            with self.assertRaises(s.VerifierEvidenceError):
                self.invoke()
        self.assertEqual(self.child(self.reload())["invocation"]["phase"], "INTENT")
        self.assertIsNone(self.child()["pid"])
        self.assertFalse(self.marker.exists())
        self.assert_recovery_refuses()

    def test_identity_wal_committed_but_cache_failure_never_resumes(self):
        with self.fail_commit("invocation_bound", cache_failure=True):
            with self.assertRaises(s.VerifierEvidenceError):
                self.invoke()
        self.assertEqual(self.child(self.reload())["invocation"]["phase"], "BOUND")
        self.assertFalse(self.marker.exists())
        self.assert_recovery_refuses()

    def test_torn_identity_append_does_not_trigger_followup_write(self):
        append = self.store.append
        def torn(event, **fields):
            if (fields.get("transition") or {}).get("event") == "invocation_bound":
                with (self.store.directory / "events.jsonl").open("ab") as stream:
                    stream.write(b'{"torn-identity":')
                raise OSError("partial append")
            return append(event, **fields)
        with patch.object(self.store, "append", side_effect=torn), self.assertRaises(s.VerifierEvidenceError):
            self.invoke()
        loaded = self.reload()
        self.assertEqual(loaded.tail, b'{"torn-identity":')
        before = (self.store.directory / "events.jsonl").read_bytes()
        self.assert_recovery_refuses(loaded)
        self.assertEqual((self.store.directory / "events.jsonl").read_bytes(), before)
        self.assertFalse(self.marker.exists())

    def test_after_identity_persistence_before_resume_authorization(self):
        with self.fail_commit("invocation_release"):
            with self.assertRaises(s.VerifierEvidenceError):
                self.invoke()
        child = self.child(self.reload())
        self.assertEqual(child["invocation"]["phase"], "BOUND")
        self.assertIsNotNone(child["pid"])
        self.assertFalse(self.marker.exists())
        self.assert_recovery_refuses()

    def test_resume_authorization_commit_interrupted_before_release(self):
        with self.fail_commit("invocation_release", after=True):
            with self.assertRaises(s.VerifierEvidenceError):
                self.invoke()
        self.assertEqual(self.child(self.reload())["invocation"]["phase"], "RELEASED")
        self.assertFalse(self.marker.exists())
        self.assert_recovery_refuses()

    def test_identity_mismatch_never_binds_or_resumes(self):
        actual = self.sup.inspect
        self.sup.inspect = lambda pid: {**actual(pid), "created": "wrong"}
        with self.assertRaises(s.VerifierEvidenceError):
            self.invoke()
        self.assertEqual(self.child(self.reload())["invocation"]["phase"], "INTENT")
        self.assertFalse(self.marker.exists())

    def assert_clean_binding_failure(self, **changes):
        before = (self.store.directory / "events.jsonl").read_bytes()
        with patch.object(self.store, "append", wraps=self.store.append) as writes, \
                patch("winprocess.ControllerVerifier") as native:
            with self.assertRaises(durable.DurableError):
                self.invoke(**changes)
            self.assert_constructor_failure_stops_reuse()
            writes.assert_not_called()
            native.assert_not_called()
        self.assertEqual(self.store.state["supervision"]["children"], {})
        self.assertEqual((self.store.directory / "events.jsonl").read_bytes(), before)

    def test_wrong_binding_ids_refused_before_registration(self):
        self.assert_clean_binding_failure(unit_id="wrong")

    def test_wrong_attempt_precondition_has_no_intent(self):
        self.assert_clean_binding_failure(attempt_id="wrong")

    def test_wrong_verifier_precondition_has_no_intent(self):
        self.assert_clean_binding_failure(verifier_id="wrong")

    def test_caller_run_id_and_matching_json_do_not_grant_authority(self):
        original = self.store.state["run_id"]
        self.store.state["run_id"] = "wrong-run"
        with self.assertRaises(durable.DurableError):
            self.invoke()
        self.store.state["run_id"] = original
        with self.assertRaises(ValueError):
            self.sup.register("verification_command", sys.executable, [sys.executable], invocation={})
        self.assertEqual(self.store.state["supervision"]["children"], {})

    def test_registry_is_frozen_by_value_and_duplicate_refused(self):
        invocation = s._VerifierInvocation(self.scheduler, "unit-1", "unit-1-attempt-1", "check-calc")
        self.scheduler.verifier_registry["check-calc"]["argv"].append("wrong")
        self.assertNotIn("wrong", invocation.registry["check-calc"]["argv"])
        self.scheduler.verifier_registry = copy.deepcopy(invocation.registry)
        with self.assertRaises((durable.DurableError, manager.Stop)):
            self.invoke()
        self.assertEqual(len(self.store.state["supervision"]["children"]), 1)
        self.assertFalse(self.marker.exists())

    def test_changed_candidate_before_resume_refused(self):
        original = self.sup.started
        def started(*args, **kwargs):
            original(*args, **kwargs)
            self.repo.write("calculator.py", "raise RuntimeError('stale candidate')\n")
        with patch.object(self.sup, "started", side_effect=started), self.assertRaises(s.VerifierEvidenceError):
            self.invoke()
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.child()["invocation"]["phase"], "BOUND")

    def test_changed_boot_before_resume_refused(self):
        original = self.sup.started
        def started(*args, **kwargs):
            original(*args, **kwargs)
            self.sup.boot = lambda: "different-boot"
        with patch.object(self.sup, "started", side_effect=started), self.assertRaises(s.VerifierEvidenceError):
            self.invoke()
        self.assertFalse(self.marker.exists())

    def test_identity_and_release_are_durable_before_actual_resume(self):
        launch, resume = winprocess.ControllerVerifier.launch, winprocess.ControllerVerifier.resume
        def checked_launch(native, *args, **kwargs):
            child = self.child(self.reload())
            self.assertEqual(child["invocation"]["phase"], "INTENT")
            self.assertIsNone(child["pid"])
            launch(native, *args, **kwargs)
        def checked_resume(native, identity):
            child = self.child(self.reload())
            self.assertEqual(child["invocation"]["phase"], "RELEASED")
            self.assertEqual(child["pid"], native.pid)
            self.assertEqual(child["identity"], identity)
            native.verify(identity)
            self.assertFalse(self.marker.exists())
            return resume(native, identity)
        with patch.object(winprocess.ControllerVerifier, "launch", checked_launch), \
                patch.object(winprocess.ControllerVerifier, "resume", checked_resume):
            self.assertEqual(self.invoke()["exit_code"], 0)

    def test_cumulative_task_exhaustion_refuses_intent(self):
        self.scheduler.base_runtime = self.scheduler.budgets["max_runtime_seconds"]
        with self.assertRaises(durable.DurableError):
            self.invoke()
        self.assertEqual(self.store.state["supervision"]["children"], {})

    def test_deadline_is_clipped_by_shared_unit_budget(self):
        options = copy.deepcopy(self.store.state["options"])
        options["verifier_registry"]["check-calc"]["timeout_seconds"] = 100
        self.store.commit(options=options)
        self.scheduler.verifier_registry = copy.deepcopy(self.store.state["options"]["verifier_registry"])
        before = time.monotonic()
        invocation = s._VerifierInvocation(self.scheduler, "unit-1", "unit-1-attempt-1", "check-calc")
        self.assertLessEqual(invocation.intent["deadline"], before + 55)
        self.assertGreater(invocation.intent["deadline"], before)

    def test_deadline_expiry_after_binding_never_resumes(self):
        original = self.sup.started
        # The invocation retains its original clock; patch the clock before construction.
        now = time.monotonic()
        clock = [now]
        self.scheduler.clock = lambda: clock[0]
        def expire(*args, **kwargs):
            original(*args, **kwargs)
            clock[0] += 100
        with patch.object(self.sup, "started", side_effect=expire), self.assertRaises(s.VerifierEvidenceError):
            self.invoke()
        self.assertFalse(self.marker.exists())

    def test_incomplete_wal_tail_refuses_new_invocation(self):
        with (self.store.directory / "events.jsonl").open("ab") as stream:
            stream.write(b'{"torn":')
        with self.assertRaises(durable.DurableError):
            self.invoke()
        self.assertFalse(self.marker.exists())

    def test_conflicting_binding_or_phase_cannot_be_committed(self):
        s._VerifierInvocation(self.scheduler, "unit-1", "unit-1-attempt-1", "check-calc")
        for field, value in (("attempt_id", "wrong"), ("registry_sha256", "0" * 64), ("phase", "RELEASED")):
            with self.subTest(field=field):
                child = copy.deepcopy(self.child())
                child["invocation"][field] = value
                with self.assertRaises(durable.DurableError):
                    self.sup.change("invocation_release", child)
        self.assertEqual(self.child()["invocation"]["phase"], "INTENT")
        with self.assertRaises(durable.DurableError):
            self.sup.change("invocation_intent", copy.deepcopy(self.child()))

    def test_nan_clock_after_identity_binding_never_resumes(self):
        original = self.sup.started
        clock = [time.monotonic()]
        self.scheduler.clock = lambda: clock[0]
        def started(*args, **kwargs):
            original(*args, **kwargs)
            clock[0] = float("nan")
        with patch.object(self.sup, "started", side_effect=started), self.assertRaises(s.VerifierEvidenceError):
            self.invoke()
        self.assertFalse(self.marker.exists())

    def test_deleted_invocation_and_duplicate_child_refused(self):
        s._VerifierInvocation(self.scheduler, "unit-1", "unit-1-attempt-1", "check-calc")
        child = copy.deepcopy(self.child())
        del child["invocation"]
        with self.assertRaises(durable.DurableError):
            self.sup.change("process_registered", child)
        invocation = s._VerifierInvocation.__new__(s._VerifierInvocation)
        invocation.supervisor = self.sup
        invocation.intent = copy.deepcopy(self.child()["invocation"])
        with self.assertRaises(durable.DurableError):
            self.sup.register("verification_command", invocation.intent["resolved_argv"][0],
                              invocation.intent["resolved_argv"], invocation=invocation)

    def test_wal_hash_chain_and_semantic_replay_checks(self):
        s._VerifierInvocation(self.scheduler, "unit-1", "unit-1-attempt-1", "check-calc")
        # Rehashing a conflicting new state must not bypass semantic transition validation.
        state = copy.deepcopy(self.store.state)
        child = next(iter(state["supervision"]["children"].values()))
        child["invocation"]["candidate_fingerprint"] = "0" * 64
        state["revision"] += 1
        self.store.append("state_committed", state=state, transition=None)
        with self.assertRaises(durable.DurableError):
            self.reload()

    def test_real_controller_death_at_four_launch_boundaries(self):
        code = r'''
import os, sys
from pathlib import Path
from unittest.mock import patch
from test_verifier_invocations import make_fixture
import winprocess
root, boundary = Path(sys.argv[1]), sys.argv[2]
repo, store, sup, scheduler = make_fixture(root)
commit, launch = store.commit, winprocess.ControllerVerifier.launch
def crash_commit(*args, **fields):
    result = commit(*args, **fields)
    if fields.get('supervision_event', {}).get('event') == boundary:
        os._exit(77)
    return result
def crash_launch(self, *args, **kwargs):
    launch(self, *args, **kwargs)
    os._exit(77)
with patch('harness.Gateway', side_effect=AssertionError('no models')), \
     patch('durable.Gateway', side_effect=AssertionError('no models')), \
     patch('socket.socket', side_effect=AssertionError('no network')), \
     patch('socket.create_connection', side_effect=AssertionError('no network')), \
     patch.object(store, 'commit', side_effect=crash_commit):
    if boundary == 'native_creation':
        with patch.object(winprocess.ControllerVerifier, 'launch', crash_launch):
            scheduler.invoke_native_verifier('unit-1', 'unit-1-attempt-1', 'check-calc')
    else:
        scheduler.invoke_native_verifier('unit-1', 'unit-1-attempt-1', 'check-calc')
'''
        for boundary, phase in (("invocation_intent", "INTENT"), ("native_creation", "INTENT"),
                                ("invocation_bound", "BOUND"), ("invocation_release", "RELEASED")):
            with self.subTest(boundary=boundary):
                root = self.root / boundary
                root.mkdir()
                env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(Path(__file__).resolve().parent),
                                                                  str(Path(__file__).resolve().parents[1]))))
                result = subprocess.run([sys.executable, "-B", "-c", code, str(root), boundary],
                                        env=env, capture_output=True, timeout=30,
                                        creationflags=0x08000000)
                self.assertEqual(result.returncode, 77, result.stderr.decode(errors="replace"))
                store = durable.Store(root / "runs", "invocation-test")
                store.load()
                child = self.child(store)
                self.assertEqual(child["invocation"]["phase"], phase)
                self.assertFalse((root / "repo" / "executed.marker").exists())
                if child["pid"] is not None:
                    deadline = time.monotonic() + 5
                    while winprocess.identity(child["pid"]) == child["identity"] and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertNotEqual(winprocess.identity(child["pid"]), child["identity"])
                repo = harness.Repository(root / "repo", store.state["options"]["config"],
                                          harness.EventLog(root / "reload.jsonl"))
                with patch.object(repo, "execute", side_effect=AssertionError("No replay")):
                    scheduler = WorkUnitScheduler(store, repo, supervisor=s.Supervisor(store))
                    self.assertEqual(scheduler.reconciliation_stop.reason, "INVOCATION_REPLAY_REFUSED")


if __name__ == "__main__":
    unittest.main()
