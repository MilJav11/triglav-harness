"""Offline trusted issuance: actual controller, executor and native verifier flow.

No live models, no consumption, and no Attempt 2. Unlike the dormant schema
fixtures, grants here must come from the explicit uninterrupted controller API.
"""
import copy
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import durable
import manager
import supervision
import retry_eligibility
import work_units
from work_unit_scheduler import WorkUnitScheduler, WorkUnitSchedulingError
from test_policy_b_shadow import make_shadow_fixture


@unittest.skipUnless(os.name == "nt", "Actual Windows native verifier handles required")
class RepairGrantIssuanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(patch.stopall)
        patch("harness.Gateway", side_effect=AssertionError("No model calls")).start()
        patch("durable.Gateway", side_effect=AssertionError("No model calls")).start()
        patch("socket.socket", side_effect=AssertionError("No network")).start()
        patch("socket.create_connection", side_effect=AssertionError("No network")).start()
        patch.dict(os.environ, {"PYTHONDONTWRITEBYTECODE": "1", "GIT_OPTIONAL_LOCKS": "0"}).start()
        self.repo, self.store, self.sup, self.scheduler, self.executor = make_shadow_fixture(Path(self.temp.name))

    def issue(self):
        return self.scheduler.authorize_repair_recovery("unit-1")

    def reload(self):
        loaded = durable.Store(self.store.directory.parent, self.store.directory.name)
        loaded.load()
        return loaded

    def assert_stopped(self, loaded=None):
        store = loaded or self.store
        before = (store.directory / "events.jsonl").read_bytes()
        with patch.object(self.executor, "execute", side_effect=AssertionError("No Attempt 2")), \
                patch.object(self.repo, "execute", side_effect=AssertionError("No verifier replay")), \
                patch("winprocess.ControllerVerifier", side_effect=AssertionError("No native replay")):
            scheduler = WorkUnitScheduler(store, self.repo, supervisor=supervision.Supervisor(store))
            self.assertIn(scheduler.reconciliation_stop.reason,
                          {"REPAIR_AUTHORIZATION_DORMANT", "REPAIR_RECOVERY_REPLAY_REFUSED"})
            self.assertEqual(scheduler.run_sequence()["status"], "HUMAN_ACTION_REQUIRED")
            section = scheduler.get_section()
            with self.assertRaises(manager.Stop):
                scheduler.execute_attempt(section["units"]["unit-1"], section, 2)
            with self.assertRaises(manager.Stop):
                scheduler.revalidate_milestone(section)
            with self.assertRaises(manager.Stop):
                scheduler.authorize_repair_recovery("unit-1")
        self.assertEqual((store.directory / "events.jsonl").read_bytes(), before)
        self.assertEqual(len(store.state["work_units"]["units"]["unit-1"]["attempts"]), 1)

    def test_actual_controller_opt_in_native_failure_and_single_grant(self):
        original = self.executor.execute
        def first(spec, context, repo):
            state = self.reload().state
            self.assertEqual(state["repair_recovery"]["mode"], "ACTIVE")
            self.assertEqual(context["attempt_number"], 1)
            self.assertNotIn("repair_authorizations", state)
            return original(spec, context, repo)
        with patch.object(self.executor, "execute", side_effect=first), \
                patch("retry_eligibility.classify_retry_eligibility", wraps=retry_eligibility.classify_retry_eligibility) as classifier:
            result = self.issue()
        self.assertEqual(classifier.call_count, 1)
        self.assertEqual(classifier.call_args.kwargs["policy"], "B")
        grant = result["authorization"]
        self.assertEqual(grant["state"], "GRANTED")
        self.assertEqual(grant["policy_b"]["failed_verifiers"], ["check-calc"])
        self.assertEqual(len(self.executor.calls), 1)
        loaded = self.reload()
        self.assertEqual(loaded.state["repair_authorizations"], {grant["authorization_id"]: grant})
        self.assertEqual(loaded.records[-2]["event"], "repair_policy_b_decision")
        self.assertEqual(loaded.records[-1]["repair_authorization"]["event"], "repair_authorization_granted")
        self.assertEqual(grant["policy_wal"]["sequence"] + 1, loaded.records[-1]["sequence"])
        att = loaded.state["work_units"]["units"]["unit-1"]["attempts"][0]
        self.assertNotIn("policy_b_shadow", att)
        self.assertEqual(att["repair_observation"]["phase"], "OBSERVATIONS_COMPLETE")
        self.assertEqual(len(grant["native_evidence"]), 2)
        self.assertIsNone(loaded.state["last_verified_checkpoint"])
        self.assertEqual(loaded.state["verified_progress"], [])
        self.assert_stopped(loaded)

    def test_all_pass_has_no_grant_and_cannot_issue_on_reload(self):
        self.executor.set_behavior("unit-1", {"writes": {"calculator.py": "def multiply(a, b): return a * b\n"}})
        with self.assertRaises(durable.DurableError):
            self.issue()
        loaded = self.reload()
        self.assertNotIn("repair_authorizations", loaded.state)
        self.assert_stopped(loaded)

    def test_json_cannot_enable_mode_or_issue_and_foreign_controller_is_refused(self):
        before = (self.store.directory / "events.jsonl").read_bytes()
        for operation in (lambda: self.store.commit(repair_recovery={"mode": "ACTIVE"}),
                          lambda: self.store._begin_repair_recovery({}, "unit-1"),
                          lambda: self.store._issue_repair_grant({"state": "GRANTED"}),
                          lambda: self.store.append("repair_policy_b_decision", decision={"decision": "ALLOW"})):
            with self.assertRaises(durable.DurableError):
                operation()
        self.assertEqual((self.store.directory / "events.jsonl").read_bytes(), before)

    def test_shadow_allow_is_forever_ineligible(self):
        self.assertEqual(self.scheduler.observe_policy_b_shadow("unit-1")["decision"]["decision"], "ALLOW")
        with self.assertRaises(manager.Stop):
            self.issue()
        self.assertNotIn("repair_recovery", self.reload().state)
        self.assertNotIn("repair_authorizations", self.store.state)

    def test_grant_source_is_immutable_but_pending_status_and_runtime_can_advance(self):
        grant = self.issue()["authorization"]
        original_source = copy.deepcopy(self.store.get_record(grant["source_wal"]["sequence"]))
        section = copy.deepcopy(self.store.state["work_units"])
        unit = section["units"]["unit-1"]
        work_units.transition_unit(unit, "REPAIR_PENDING")
        work_units.seal_unit_state(unit)
        work_units._reseal_section(section)
        account = copy.deepcopy(self.store.state["manager"])
        account["runtime_seconds"] += 1
        self.store.commit(work_units=section, manager=account)
        loaded = self.reload()
        self.assertEqual(loaded.state["repair_authorizations"][grant["authorization_id"]], grant)
        self.assertEqual(loaded.get_record(grant["source_wal"]["sequence"]), original_source)
        self.assertEqual(loaded.state["work_units"]["units"]["unit-1"]["status"], "REPAIR_PENDING")
        self.assert_stopped(loaded)

    def assert_current_cannot_launch_or_append(self):
        before = (self.store.directory / "events.jsonl").read_bytes()
        with patch.object(self.executor, "execute", side_effect=AssertionError("No launch after interruption")), \
                patch("winprocess.ControllerVerifier", side_effect=AssertionError("No verifier after interruption")):
            section = self.scheduler.get_section()
            with self.assertRaises((manager.Stop, durable.DurableError)):
                self.scheduler.execute_attempt(section["units"]["unit-1"], section, 1)
            with self.assertRaises((manager.Stop, durable.DurableError)):
                self.scheduler.invoke_native_verifier("unit-1", "unit-1-attempt-1", "check-calc")
        if self.store._repair_write_failed:
            with self.assertRaises(durable.DurableError):
                self.store.commit(current_step="forbidden followup")
        self.assertEqual((self.store.directory / "events.jsonl").read_bytes(), before)

    def test_failed_mode_commit_before_wal_never_executes_or_retries(self):
        original = self.store.append
        def fault(event, **fields):
            if "repair_recovery" in fields:
                raise OSError("mode commit failed before WAL")
            return original(event, **fields)
        with patch.object(self.store, "append", side_effect=fault), self.assertRaises(OSError):
            self.issue()
        self.assertEqual(self.executor.calls, [])
        self.assert_current_cannot_launch_or_append()

    def test_mode_cache_failure_never_executes_and_reload_cannot_recreate_session(self):
        original = durable.atomic_write
        def fault(path, data):
            if path.name == "state.json" and b'"repair_recovery"' in data:
                raise OSError("mode cache commit uncertain")
            return original(path, data)
        with patch("durable.atomic_write", side_effect=fault), self.assertRaises(OSError):
            self.issue()
        self.assertEqual(self.executor.calls, [])
        self.assert_current_cannot_launch_or_append()
        loaded = self.reload()
        before = (loaded.directory / "events.jsonl").read_bytes()
        with patch("winprocess.ControllerVerifier", side_effect=AssertionError("No reload launch")):
            scheduler = WorkUnitScheduler(loaded, self.repo, supervisor=supervision.Supervisor(loaded))
            self.assertEqual(scheduler.reconciliation_stop.reason, "REPAIR_RECOVERY_REPLAY_REFUSED")
            with self.assertRaises(manager.Stop):
                scheduler.authorize_repair_recovery("unit-1")
        self.assertEqual((loaded.directory / "events.jsonl").read_bytes(), before)

    def test_torn_initial_decision_with_only_a_prefix_is_not_m0_tail(self):
        original = self.store.append
        def fault(event, **fields):
            if event == "repair_policy_b_decision":
                with (self.store.directory / "events.jsonl").open("ab") as stream:
                    stream.write(b'{"seq')
                    stream.flush()
                    os.fsync(stream.fileno())
                raise OSError("decision torn before event name")
            return original(event, **fields)
        with patch.object(self.store, "append", side_effect=fault), self.assertRaises(OSError):
            self.issue()
        self.assertEqual(len(self.executor.calls), 1)
        self.assert_current_cannot_launch_or_append()
        before = (self.store.directory / "events.jsonl").read_bytes()
        with self.assertRaises(durable.DurableError):
            self.reload()
        self.assertEqual((self.store.directory / "events.jsonl").read_bytes(), before)

    def test_complete_decision_without_grant_is_rejected(self):
        original = self.store.append
        def fault(event, **fields):
            value = original(event, **fields)
            if event == "repair_policy_b_decision":
                raise OSError("controller lost after decision")
            return value
        with patch.object(self.store, "append", side_effect=fault), self.assertRaises(OSError):
            self.issue()
        self.assert_current_cannot_launch_or_append()
        with self.assertRaises(durable.DurableError):
            self.reload()

    def test_torn_grant_and_failed_grant_before_wal_fail_closed(self):
        original = self.store.append
        def fault(event, **fields):
            if "repair_authorization" in fields:
                with (self.store.directory / "events.jsonl").open("ab") as stream:
                    stream.write(b'{"sequence":')
                raise OSError("grant torn")
            return original(event, **fields)
        with patch.object(self.store, "append", side_effect=fault), self.assertRaises(OSError):
            self.issue()
        self.assert_current_cannot_launch_or_append()
        with self.assertRaises(durable.DurableError):
            self.reload()

    def test_complete_grant_post_wal_exception_never_reissues_or_launches(self):
        original = self.store.append
        def fault(event, **fields):
            value = original(event, **fields)
            if "repair_authorization" in fields:
                raise OSError("grant durable but return uncertain")
            return value
        with patch.object(self.store, "append", side_effect=fault), self.assertRaises(OSError):
            self.issue()
        self.assert_current_cannot_launch_or_append()
        loaded = self.reload()
        self.assertEqual(len(loaded.state["repair_authorizations"]), 1)
        self.assert_stopped(loaded)

    def test_grant_cache_failure_never_consumes_and_recovers_only_history(self):
        original = durable.atomic_write
        def fault(path, data):
            if path.name == "state.json" and b'"repair_authorizations"' in data:
                raise OSError("grant cache failed")
            return original(path, data)
        with patch("durable.atomic_write", side_effect=fault), self.assertRaises(OSError):
            self.issue()
        self.assert_current_cannot_launch_or_append()
        loaded = self.reload()
        self.assertEqual(len(loaded.state["repair_authorizations"]), 1)
        self.assert_stopped(loaded)

    def test_native_observation_commit_failure_stops_before_next_verifier(self):
        original = self.store.append
        def fault(event, **fields):
            value = original(event, **fields)
            if (fields.get("transition") or {}).get("event") == "invocation_complete":
                raise OSError("complete observation commit uncertain")
            return value
        with patch.object(self.store, "append", side_effect=fault), self.assertRaises((OSError, supervision.VerifierEvidenceError)):
            self.issue()
        self.assertEqual(len(self.store.state["supervision"]["children"]), 1)
        self.assert_current_cannot_launch_or_append()
        self.assertNotIn("repair_authorizations", self.store.state)

    def test_registry_mutation_during_classification_refuses_grant(self):
        original = retry_eligibility.classify_retry_eligibility
        def fault(*args, **kwargs):
            result = original(*args, **kwargs)
            self.scheduler.verifier_registry = {"foreign": {"argv": ["python"]}}
            return result
        with patch("retry_eligibility.classify_retry_eligibility", side_effect=fault), self.assertRaises(WorkUnitSchedulingError):
            self.issue()
        self.assertNotIn("repair_authorizations", self.reload().state)
        self.assert_current_cannot_launch_or_append()

    def test_candidate_mutation_during_classification_refuses_grant(self):
        original = retry_eligibility.classify_retry_eligibility
        def fault(*args, **kwargs):
            result = original(*args, **kwargs)
            self.repo.write("calculator.py", "# foreign candidate\n")
            return result
        with patch("retry_eligibility.classify_retry_eligibility", side_effect=fault), self.assertRaises(WorkUnitSchedulingError):
            self.issue()
        self.assertNotIn("repair_authorizations", self.reload().state)

    def test_classifier_cannot_swallow_uncertain_wal_and_still_grant(self):
        original = self.store.append
        def fault(event, **fields):
            value = original(event, **fields)
            raise OSError("classifier write uncertain")
        def classifier(*args, **kwargs):
            with patch.object(self.store, "append", side_effect=fault):
                try:
                    self.store.commit(current_step="unexpected classifier mutation")
                except OSError:
                    pass
            return retry_eligibility.RetryDecision(True, "ALLOW", "ALLOW_VERIFIER_FAILURE", {"failed_verifiers": ["check-calc"]})
        with patch("retry_eligibility.classify_retry_eligibility", side_effect=classifier), self.assertRaises(WorkUnitSchedulingError):
            self.issue()
        self.assertNotIn("repair_authorizations", self.reload().state)
        self.assert_current_cannot_launch_or_append()

    def test_classifier_swallowing_pre_wal_commit_failure_cannot_grant(self):
        def classifier(*args, **kwargs):
            with patch.object(self.store, "append", side_effect=OSError("commit outcome uncertain")):
                try:
                    self.store.commit(current_step="untrusted side effect")
                except OSError:
                    pass
            return retry_eligibility.RetryDecision(True, "ALLOW", "ALLOW_VERIFIER_FAILURE", {"failed_verifiers": ["check-calc"]})
        with patch("retry_eligibility.classify_retry_eligibility", side_effect=classifier), self.assertRaises(durable.DurableError):
            self.issue()
        self.assertNotIn("repair_authorizations", self.reload().state)
        self.assert_current_cannot_launch_or_append()

    def test_completed_source_attempt_cannot_be_mutated_by_ordinary_commit(self):
        self.issue()
        before = (self.store.directory / "events.jsonl").read_bytes()
        section = copy.deepcopy(self.store.state["work_units"])
        unit = section["units"]["unit-1"]
        unit["attempts"][0]["changed_paths"].append("foreign.py")
        work_units.seal_unit_state(unit)
        work_units._reseal_section(section)
        with self.assertRaises(durable.DurableError):
            self.store.commit(work_units=section)
        self.assertEqual((self.store.directory / "events.jsonl").read_bytes(), before)

    def test_duplicate_grant_and_reconstructed_controller_are_refused(self):
        grant = self.issue()["authorization"]
        before = (self.store.directory / "events.jsonl").read_bytes()
        with self.assertRaises(durable.DurableError):
            self.store._issue_repair_grant(self.store._repair_controller)
        fake = durable._RepairController(self.store, self.scheduler, self.store.state["repair_recovery"])
        with self.assertRaises(durable.DurableError):
            self.store._issue_repair_grant(fake)
        with self.assertRaises(durable.DurableError):
            self.store.commit(repair_authorizations={grant["authorization_id"]: grant})
        self.assertEqual((self.store.directory / "events.jsonl").read_bytes(), before)

    def test_deadline_exhaustion_during_classification_refuses_grant(self):
        original = retry_eligibility.classify_retry_eligibility
        def fault(*args, **kwargs):
            result = original(*args, **kwargs)
            expired = self.store.state["repair_recovery"]["task_deadline"] + 1
            self.scheduler.clock = lambda: expired
            return result
        with patch("retry_eligibility.classify_retry_eligibility", side_effect=fault), self.assertRaises(WorkUnitSchedulingError):
            self.issue()
        self.assertNotIn("repair_authorizations", self.reload().state)

    def test_controller_boot_change_during_classification_refuses_grant(self):
        original = retry_eligibility.classify_retry_eligibility
        def fault(*args, **kwargs):
            result = original(*args, **kwargs)
            patch("winprocess.boot_identity", return_value="foreign-boot").start()
            return result
        with patch("retry_eligibility.classify_retry_eligibility", side_effect=fault), self.assertRaises(WorkUnitSchedulingError):
            self.issue()
        self.assertNotIn("repair_authorizations", self.reload().state)

    def test_environment_change_during_classification_refuses_grant(self):
        original = retry_eligibility.classify_retry_eligibility
        def fault(*args, **kwargs):
            result = original(*args, **kwargs)
            patch("environment.compare", return_value={"decision": "UNSAFE"}).start()
            return result
        with patch("retry_eligibility.classify_retry_eligibility", side_effect=fault), self.assertRaises(WorkUnitSchedulingError):
            self.issue()
        self.assertNotIn("repair_authorizations", self.reload().state)

    def test_max_attempts_one_and_safeqwen_cannot_opt_in(self):
        section = self.scheduler.get_section()
        section["units"]["unit-1"]["spec"]["limits"]["max_attempts"] = 1
        self.scheduler._persist_unit(section, section["units"]["unit-1"])
        with self.assertRaises(durable.DurableError):
            self.issue()
        self.assertEqual(self.executor.calls, [])
        self.assertNotIn("repair_recovery", self.reload().state)

    def test_actual_controller_death_after_decision_refuses_replay(self):
        # A fresh disposable run in a child; OS death cannot execute finally.
        script = r'''
import os, sys
from pathlib import Path
from unittest.mock import patch
from test_policy_b_shadow import make_shadow_fixture
repo, store, sup, scheduler, executor = make_shadow_fixture(Path(sys.argv[1]))
append = store.append
def crash(event, **fields):
    result = append(event, **fields)
    if event == 'repair_policy_b_decision':
        os._exit(73)
    return result
with patch('harness.Gateway', side_effect=AssertionError('No models')), patch('socket.socket', side_effect=AssertionError('No network')), patch.object(store, 'append', side_effect=crash):
    scheduler.authorize_repair_recovery('unit-1')
'''
        child_root = Path(self.temp.name) / "controller-death"
        child_root.mkdir()
        process = subprocess.run([sys.executable, "-c", script, str(child_root)], capture_output=True, timeout=40)
        self.assertEqual(process.returncode, 73, process.stderr.decode(errors="replace"))
        loaded = durable.Store(child_root / "runs", "shadow-test")
        before = (loaded.directory / "events.jsonl").read_bytes()
        with self.assertRaises(durable.DurableError):
            loaded.load()
        self.assertEqual((loaded.directory / "events.jsonl").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
