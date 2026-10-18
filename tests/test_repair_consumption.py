"""Offline continuous consume/allocation; no Attempt 2 executor is permitted."""
import copy
import os
import subprocess
import sys
import threading
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import durable
import manager
import retry_eligibility
import supervision
import work_units
from work_unit_scheduler import WorkUnitScheduler, WorkUnitSchedulingError
from test_policy_b_shadow import make_shadow_fixture


@unittest.skipUnless(os.name == "nt", "Actual native Windows observations required")
class RepairConsumptionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(patch.stopall)
        patch("harness.Gateway", side_effect=AssertionError("No models")).start()
        patch("durable.Gateway", side_effect=AssertionError("No models")).start()
        patch("socket.socket", side_effect=AssertionError("No networking")).start()
        patch("socket.create_connection", side_effect=AssertionError("No networking")).start()
        patch.dict(os.environ, {"PYTHONDONTWRITEBYTECODE": "1", "GIT_OPTIONAL_LOCKS": "0"}).start()
        self.repo, self.store, self.sup, self.scheduler, self.executor = make_shadow_fixture(Path(self.temp.name))
        original = self.executor.execute
        def first_only(spec, context, repo):
            self.assertEqual(context["attempt_number"], 1, "Attempt 2 must never execute")
            return original(spec, context, repo)
        patch.object(self.executor, "execute", side_effect=first_only).start()

    def allocate(self):
        return self.scheduler.allocate_repair_recovery("unit-1")

    def reload(self):
        store = durable.Store(self.store.directory.parent, self.store.directory.name)
        store.load()
        return store

    def assert_no_launch(self, store=None):
        store = store or self.reload()
        before = (store.directory / "events.jsonl").read_bytes()
        with patch.object(self.executor, "execute", side_effect=AssertionError("No executor")), \
                patch.object(self.repo, "execute", side_effect=AssertionError("No verifier")), \
                patch("winprocess.ControllerVerifier", side_effect=AssertionError("No native launch")):
            scheduler = WorkUnitScheduler(store, self.repo, supervisor=supervision.Supervisor(store))
            self.assertIsNotNone(scheduler.reconciliation_stop)
            self.assertEqual(scheduler.run_sequence()["status"], "HUMAN_ACTION_REQUIRED")
            section = scheduler.get_section()
            unit = section["units"]["unit-1"]
            operations = [lambda: scheduler.allocate_repair_recovery("unit-1"),
                          lambda: scheduler.authorize_repair_recovery("unit-1"),
                          lambda: scheduler.observe_policy_b_shadow("unit-1"),
                          lambda: scheduler.execute_attempt(unit, section, 2),
                          lambda: scheduler.execute_unit(unit, section),
                          lambda: scheduler.invoke_native_verifier("unit-1", "unit-1-attempt-2", "check-calc"),
                          lambda: scheduler.revalidate_milestone(section),
                          lambda: supervision._VerifierInvocation(scheduler, "unit-1", "unit-1-attempt-2", "check-calc")]
            for operation in operations:
                with self.assertRaises((manager.Stop, durable.DurableError)):
                    operation()
        self.assertEqual((store.directory / "events.jsonl").read_bytes(), before)

    def test_continuous_operation_consumes_and_allocates_atomically_without_dispatch(self):
        original = self.executor.execute
        def check_mode(spec, context, repo):
            mode = self.reload().state["repair_recovery"]
            self.assertEqual(mode["schema_version"], 2)
            self.assertEqual(mode["operation"], "ALLOCATE_ONLY")
            return original(spec, context, repo)
        with patch.object(self.executor, "execute", side_effect=check_mode), \
                patch("retry_eligibility.classify_retry_eligibility", wraps=retry_eligibility.classify_retry_eligibility) as classifier:
            result = self.allocate()
        self.assertEqual(classifier.call_count, 2)
        grant = result["authorization"]
        self.assertEqual(grant["schema_version"], 2)
        self.assertEqual(grant["state"], "CONSUMED")
        self.assertEqual(len(self.executor.calls), 1)
        loaded = self.reload()
        records = loaded.records
        self.assertEqual(records[-2]["repair_authorization"]["event"], "repair_authorization_granted")
        self.assertEqual(records[-1]["repair_authorization"], {
            "event": "repair_authorization_consumed", "authorization_id": grant["authorization_id"], "attempt_id": "unit-1-attempt-2"})
        unit = loaded.state["work_units"]["units"]["unit-1"]
        self.assertEqual(len(unit["attempts"]), 2)
        self.assertEqual(unit["status"], "EXECUTING")
        self.assertEqual(unit["current_attempt"], "unit-1-attempt-2")
        self.assertEqual(unit["attempts"][1]["stage"], "allocated")
        self.assertIsNone(unit["attempts"][1]["execution_result"])
        self.assertEqual(unit["attempts"][0], records[-2]["state"]["work_units"]["units"]["unit-1"]["attempts"][0])
        self.assertGreaterEqual(loaded.state["manager"]["runtime_seconds"], records[-2]["state"]["manager"]["runtime_seconds"])
        self.assertEqual(loaded.state["verified_progress"], [])
        self.assertIsNone(loaded.state["last_verified_checkpoint"])
        self.assert_no_launch(loaded)

    def test_issuance_only_session_cannot_be_consumed_or_adopted(self):
        result = self.scheduler.authorize_repair_recovery("unit-1")
        self.assertEqual(result["authorization"]["state"], "GRANTED")
        self.assertEqual(self.store.state["repair_recovery"]["schema_version"], 1)
        before = (self.store.directory / "events.jsonl").read_bytes()
        with self.assertRaises(durable.DurableError):
            self.store._consume_repair_grant(self.store._repair_controller)
        self.assertEqual((self.store.directory / "events.jsonl").read_bytes(), before)
        self.assertEqual(len(self.store.state["work_units"]["units"]["unit-1"]["attempts"]), 1)
        self.assert_no_launch()

    def test_json_and_reconstructed_controller_cannot_consume(self):
        self.allocate()
        before = (self.store.directory / "events.jsonl").read_bytes()
        mode = self.store.state["repair_recovery"]
        fake = durable._RepairController(self.store, self.scheduler, mode)
        for controller in ({"state": "GRANTED"}, fake, self.store._repair_controller):
            with self.assertRaises(durable.DurableError):
                self.store._consume_repair_grant(controller)
        with self.assertRaises(durable.DurableError):
            self.store.append("repair_authorization_consumed", authorization_id="repair-x")
        self.assertEqual((self.store.directory / "events.jsonl").read_bytes(), before)

    def test_allocated_attempt_and_completed_source_are_immutable(self):
        self.allocate()
        before = (self.store.directory / "events.jsonl").read_bytes()
        for index, key, value in ((1, "stage", "executing"), (1, "execution_result", {}),
                                  (0, "changed_paths", ["foreign.py"])):
            section = copy.deepcopy(self.store.state["work_units"])
            unit = section["units"]["unit-1"]
            unit["attempts"][index][key] = value
            work_units.seal_unit_state(unit)
            work_units._reseal_section(section)
            with self.assertRaises(durable.DurableError):
                self.store.commit(work_units=section)
            self.assertEqual((self.store.directory / "events.jsonl").read_bytes(), before)
            self.store.load()

    def test_duplicate_and_concurrent_consumption_commit_only_once(self):
        original = self.store._consume_repair_grant
        results, errors = [], []
        attempted = threading.Event()
        workers = []
        def race(controller):
            def consume():
                try:
                    attempted.set()
                    results.append(original(controller))
                except BaseException as exc:
                    errors.append(exc)
            worker = threading.Thread(target=consume)
            workers.append(worker)
            worker.start()
            self.assertTrue(attempted.wait(timeout=20))
            result = original(controller)
            results.append(result)
            return result
        with patch.object(self.store, "_consume_repair_grant", side_effect=race):
            self.allocate()
        for worker in workers:
            worker.join(timeout=40)
            self.assertFalse(worker.is_alive(), "Consumption must not deadlock")
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], durable.DurableError)
        records = [r for r in self.store.records if (r.get("repair_authorization") or {}).get("event") == "repair_authorization_consumed"]
        self.assertEqual(len(records), 1)
        self.assertEqual(len(self.executor.calls), 1)
        self.assert_no_launch()

    def test_failed_consumption_before_wal_leaves_only_granted(self):
        original = self.store.append
        def fault(event, **fields):
            if (fields.get("repair_authorization") or {}).get("event") == "repair_authorization_consumed":
                raise OSError("consumption failed before WAL")
            return original(event, **fields)
        with patch.object(self.store, "append", side_effect=fault), self.assertRaises(OSError):
            self.allocate()
        self.assertTrue(self.store._repair_write_failed)
        loaded = self.reload()
        self.assertEqual(len(loaded.state["work_units"]["units"]["unit-1"]["attempts"]), 1)
        self.assertEqual(next(iter(loaded.state["repair_authorizations"].values()))["state"], "GRANTED")
        self.assert_no_launch(loaded)

    def test_torn_consumption_refuses_load_without_truncation(self):
        original = self.store.append
        def fault(event, **fields):
            if (fields.get("repair_authorization") or {}).get("event") == "repair_authorization_consumed":
                with (self.store.directory / "events.jsonl").open("ab") as stream:
                    stream.write(b'{"seq')
                    stream.flush()
                    os.fsync(stream.fileno())
                raise OSError("consumption torn")
            return original(event, **fields)
        with patch.object(self.store, "append", side_effect=fault), self.assertRaises(OSError):
            self.allocate()
        before = (self.store.directory / "events.jsonl").read_bytes()
        with self.assertRaises(durable.DurableError):
            self.reload()
        with self.assertRaises(durable.DurableError):
            self.store.commit(current_step="forbidden followup")
        self.assert_no_launch(self.store)
        self.assertEqual((self.store.directory / "events.jsonl").read_bytes(), before)

    def test_post_wal_exception_reloads_exactly_one_unlaunched_allocation(self):
        original = self.store.append
        def fault(event, **fields):
            result = original(event, **fields)
            if (fields.get("repair_authorization") or {}).get("event") == "repair_authorization_consumed":
                raise OSError("WAL committed but return uncertain")
            return result
        with patch.object(self.store, "append", side_effect=fault), self.assertRaises(OSError):
            self.allocate()
        self.assertTrue(self.store._repair_write_failed)
        loaded = self.reload()
        self.assertEqual(len(loaded.state["work_units"]["units"]["unit-1"]["attempts"]), 2)
        self.assert_no_launch(loaded)

    def test_cache_failure_reloads_atomic_consumption_without_relaunch(self):
        original = durable.atomic_write
        def fault(path, data):
            if path.name == "state.json" and b'"consumption"' in data:
                raise OSError("consumption cache failed")
            return original(path, data)
        with patch("durable.atomic_write", side_effect=fault), self.assertRaises(OSError):
            self.allocate()
        loaded = self.reload()
        self.assertEqual(len(loaded.state["work_units"]["units"]["unit-1"]["attempts"]), 2)
        self.assert_no_launch(loaded)

    def drift_before_consume(self, change):
        original = self.store._consume_repair_grant
        def fault(controller):
            change(controller)
            return original(controller)
        with patch.object(self.store, "_consume_repair_grant", side_effect=fault), \
                self.assertRaises((durable.DurableError, WorkUnitSchedulingError)):
            self.allocate()
        loaded = self.reload()
        self.assertEqual(next(iter(loaded.state["repair_authorizations"].values()))["state"], "GRANTED")
        self.assertEqual(len(loaded.state["work_units"]["units"]["unit-1"]["attempts"]), 1)
        self.assert_no_launch(loaded)

    def test_repository_drift_after_granted_refuses_consumption(self):
        self.drift_before_consume(lambda controller: self.repo.write("calculator.py", "# changed candidate\n"))

    def test_environment_drift_after_granted_refuses_consumption(self):
        self.drift_before_consume(lambda controller: patch("environment.compare", return_value={"decision": "UNSAFE"}).start())

    def test_expired_task_budget_after_granted_refuses_consumption(self):
        def expired(controller):
            self.scheduler.clock = lambda: controller.mode["task_deadline"] + 1
        self.drift_before_consume(expired)

    def test_expired_unit_budget_after_granted_refuses_consumption(self):
        def expired(controller):
            grant = next(iter(self.store.state["repair_authorizations"].values()))
            self.scheduler.clock = lambda: grant["budget"]["unit_deadline"] + 1
        self.drift_before_consume(expired)

    def test_wrong_controller_session_refuses_consumption(self):
        def foreign(controller):
            controller.mode["controller_session"]["controller_sha256"] = "0" * 64
        self.drift_before_consume(foreign)

    def test_wrong_grant_reference_refuses_consumption(self):
        def foreign(controller):
            controller.grant_record["sha256"] = "0" * 64
        self.drift_before_consume(foreign)

    def test_changed_registry_refuses_consumption(self):
        def foreign(controller):
            self.scheduler.verifier_registry = {"foreign": {"argv": ["python"]}}
        self.drift_before_consume(foreign)

    def test_supervisor_not_idle_refuses_consumption(self):
        self.drift_before_consume(lambda controller: patch.object(self.sup, "require_idle", side_effect=durable.DurableError("busy")).start())

    def test_process_ownership_change_refuses_consumption(self):
        self.drift_before_consume(lambda controller: patch("winprocess.boot_identity", return_value="foreign-boot").start())

    def test_reload_cannot_reconstruct_the_continuous_session(self):
        self.drift_before_consume(lambda controller: self.store.load())

    def test_ordinary_entry_points_cannot_consume_even_inside_continuous_operation(self):
        original = self.store._consume_repair_grant
        def check(controller):
            before = (self.store.directory / "events.jsonl").read_bytes()
            section = self.scheduler.get_section()
            unit = section["units"]["unit-1"]
            with self.assertRaises(manager.Stop):
                self.scheduler.execute_attempt(unit, section, 2)
            with self.assertRaises(durable.DurableError):
                self.store.commit(repair_authorizations=self.store.state["repair_authorizations"])
            self.assertEqual((self.store.directory / "events.jsonl").read_bytes(), before)
            return original(controller)
        with patch.object(self.store, "_consume_repair_grant", side_effect=check):
            self.allocate()
        self.assert_no_launch()

    def test_historical_invocation_is_retained_but_new_native_invocation_is_rejected(self):
        self.allocate()
        state = copy.deepcopy(self.store.state)
        supervision.validate_invocations(state)
        child = next(iter(state["supervision"]["children"].values()))
        child["invocation"]["attempt_id"] = "unit-1-attempt-2"
        with self.assertRaises(durable.DurableError):
            supervision.validate_invocations(state)
        state = copy.deepcopy(self.store.state)
        next(iter(state["supervision"]["children"].values()))["invocation"]["phase"] = "RELEASED"
        with self.assertRaises(durable.DurableError):
            supervision.validate_invocations(state)
        self.assert_no_launch()

    def test_consumption_schema_rejects_wrong_types_and_budget_contradictions(self):
        self.allocate()
        original = copy.deepcopy(self.store.state)
        for key, value in (("checked_clock", True), ("task_runtime_seconds", float("nan")),
                           ("task_runtime_seconds", 0), ("unit_elapsed_seconds", []),
                           ("allocated_at", None), ("controller_session", {}), ("attempt_id", "foreign")):
            with self.subTest(key=key, value=value):
                state = copy.deepcopy(original)
                next(iter(state["repair_authorizations"].values()))["consumption"][key] = value
                with self.assertRaises(durable.DurableError):
                    durable.validate_state(state)
        state = copy.deepcopy(original)
        state["manager"]["runtime_seconds"] = float("inf")
        with self.assertRaises(durable.DurableError):
            durable.validate_state(state)

    def test_foreign_store_cannot_consume_current_controller_capability(self):
        original = self.store._consume_repair_grant
        def check(controller):
            loaded = self.reload()
            with self.assertRaises(durable.DurableError):
                loaded._consume_repair_grant(controller)
            return original(controller)
        with patch.object(self.store, "_consume_repair_grant", side_effect=check):
            self.allocate()
        self.assert_no_launch()

    def test_replay_rejects_incomplete_contradictory_and_replayed_consumption(self):
        self.allocate()
        original = copy.deepcopy(self.store.records)
        cases = ("missing_marker", "wrong_marker", "without_allocation", "without_consumption", "wrong_hash",
                 "wrong_session", "started_executor", "source_changed", "expired", "runtime_reset", "extra_state", "duplicate")
        for case in cases:
            with self.subTest(case=case):
                records = copy.deepcopy(original)
                record = records[-1]
                state = record["state"]
                grant = next(iter(state["repair_authorizations"].values()))
                unit = state["work_units"]["units"]["unit-1"]
                if case == "missing_marker":
                    del record["repair_authorization"]
                elif case == "wrong_marker":
                    record["repair_authorization"]["attempt_id"] = "foreign"
                elif case == "without_allocation":
                    unit["attempts"].pop()
                    unit.update(status="VERIFYING", current_attempt=None)
                elif case == "without_consumption":
                    state["repair_authorizations"][grant["authorization_id"]] = durable._granted_form(grant)
                elif case == "wrong_hash":
                    grant["consumption"]["grant_wal"]["sha256"] = "0" * 64
                elif case == "wrong_session":
                    grant["consumption"]["controller_session"]["controller_sha256"] = "0" * 64
                elif case == "started_executor":
                    unit["attempts"][1]["stage"] = "executing"
                elif case == "source_changed":
                    unit["attempts"][0]["changed_paths"].append("foreign.py")
                elif case == "expired":
                    grant["consumption"]["checked_clock"] = grant["consumption"]["task_deadline"] + 1
                elif case == "runtime_reset":
                    state["manager"]["runtime_seconds"] = 0
                elif case == "extra_state":
                    state["status"] = "FAILED"
                else:
                    record = copy.deepcopy(record)
                    record["sequence"] += 1
                    record["previous"] = records[-1]["sha256"]
                    record["state"]["revision"] += 1
                    records.append(record)
                work_units.seal_unit_state(unit)
                work_units._reseal_section(state["work_units"])
                record.pop("sha256", None)
                record["sha256"] = durable.digest(record)
                ledger = self.store.directory / "events.jsonl"
                ledger.write_bytes(b"".join(durable.encoded(r) + b"\n" for r in records))
                before = ledger.read_bytes()
                with self.assertRaises(durable.DurableError):
                    self.reload()
                self.assertEqual(ledger.read_bytes(), before)

    def test_real_controller_death_at_each_transaction_boundary(self):
        script = r'''
import os, sys
from pathlib import Path
from unittest.mock import patch
from test_policy_b_shadow import make_shadow_fixture
repo, store, sup, scheduler, executor = make_shadow_fixture(Path(sys.argv[1]))
boundary = sys.argv[2]
append = store.append
def crash(event, **fields):
    marker = (fields.get('repair_authorization') or {}).get('event')
    if boundary == 'torn_consumption' and marker == 'repair_authorization_consumed':
        with (store.directory / 'events.jsonl').open('ab') as stream:
            stream.write(b'{"seq')
            stream.flush()
            os.fsync(stream.fileno())
        os._exit(73)
    result = append(event, **fields)
    if (boundary == 'mode' and 'repair_recovery' in fields
        or boundary == 'decision' and event == 'repair_policy_b_decision'
        or boundary == 'grant' and marker == 'repair_authorization_granted'
        or boundary == 'consumption' and marker == 'repair_authorization_consumed'):
        os._exit(73)
    return result
with patch('harness.Gateway', side_effect=AssertionError('No models')), patch('socket.socket', side_effect=AssertionError('No network')), patch.object(store, 'append', side_effect=crash):
    scheduler.allocate_repair_recovery('unit-1')
'''
        for boundary in ("mode", "decision", "grant", "torn_consumption", "consumption"):
            with self.subTest(boundary=boundary):
                root = Path(self.temp.name) / boundary
                root.mkdir()
                process = subprocess.run([sys.executable, "-c", script, str(root), boundary], capture_output=True, timeout=40)
                self.assertEqual(process.returncode, 73, process.stderr.decode(errors="replace"))
                store = durable.Store(root / "runs", "shadow-test")
                before = (store.directory / "events.jsonl").read_bytes()
                if boundary in ("decision", "torn_consumption"):
                    with self.assertRaises(durable.DurableError):
                        store.load()
                else:
                    store.load()
                    self.assertEqual(len(store.state["work_units"]["units"]["unit-1"]["attempts"]),
                                     {"mode": 0, "grant": 1, "consumption": 2}[boundary])
                    scheduler = WorkUnitScheduler(store, self.repo, supervisor=supervision.Supervisor(store))
                    self.assertIsNotNone(scheduler.reconciliation_stop)
                    self.assertEqual(scheduler.run_sequence()["status"], "HUMAN_ACTION_REQUIRED")
                self.assertEqual((store.directory / "events.jsonl").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
