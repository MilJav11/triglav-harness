"""Identity-only qualification: real Store/native controller, zero worker/model launches.

Runtime/model resource bytes are inert fixture pins. Reserved invocation slots
are not authenticated execution receipts, grants or allocated attempts.
"""
import copy
import os
import threading
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import durable
import harness
import manager
import supervised_safe_qwen as adapter
import terminal_run
import work_units
from supervision import Supervisor
from work_unit_scheduler import WorkUnitScheduler
import test_safe_qwen_integration as fixture_tests


@unittest.skipUnless(os.name == "nt", "Native Windows controller identity required")
class SafeQwenRecoveryIdentityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_tests.IntegrationTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"PYTHONDONTWRITEBYTECODE": "1", "GIT_OPTIONAL_LOCKS": "0"}).start()
        patch("socket.socket", side_effect=AssertionError("No network")).start()
        patch("socket.create_connection", side_effect=AssertionError("No network")).start()
        patch("harness.Gateway.request", side_effect=AssertionError("No model requests")).start()
        patch("harness.Gateway.switch", side_effect=AssertionError("No model startup")).start()
        patch("winprocess.ControllerVerifier", side_effect=AssertionError("No worker/verifier launch")).start()
        patch("durable.Store._issue_repair_grant", side_effect=AssertionError("No grant")).start()
        patch("durable.Store._consume_repair_grant", side_effect=AssertionError("No consumption")).start()
        self.executor, self.cfg = self.fixture.configure()
        self.store = self.fixture.store
        self.parent = terminal_run.fixed_unit("Implement multiply", self.cfg, self.executor, ["check-1"])
        self.parent["limits"]["max_attempts"] = 2
        registry = {"check-1": {"argv": self.fixture.command, "timeout_seconds": 30}}
        manager.create_run(self.store, self.fixture.repo, "Implement multiply", [self.fixture.command], self.cfg,
                           criteria=["multiply"], work_units=[self.parent], verifier_registry=registry,
                           allow=["calculator.py"], forbid=["test_calculator.py"], executor=self.executor)
        section = copy.deepcopy(self.store.state["work_units"])
        unit = section["units"][self.parent["unit_id"]]
        work_units.transition_unit(unit, "VALIDATED")
        work_units.transition_unit(unit, "READY")
        work_units.seal_unit_state(unit)
        work_units._reseal_section(section)
        self.store.commit(work_units=section)
        self.sup = Supervisor(self.store)
        self.sup.__enter__()
        self.assertTrue(self.sup.worker_started.wait(2))
        self.addCleanup(self.sup.__exit__, None, None, None)
        self.repo = manager.ScopedRepository(self.fixture.repo.root, self.cfg, durable.DurableLog(self.store), self.store)
        self.scheduler = WorkUnitScheduler(self.store, self.repo, supervisor=self.sup,
                                          agents=SimpleNamespace(executor=self.executor))
        self.gateway = harness.Gateway(self.cfg, durable.DurableLog(self.store))
        self.gateway.supervisor = self.sup
        self.executor.bind_gateway(self.gateway)

    def select(self):
        return self.store.select_safe_qwen_recovery_identity(self.scheduler, self.parent["unit_id"])

    def reload(self):
        loaded = durable.Store(self.store.directory.parent, self.store.directory.name)
        loaded.load()
        return loaded

    def stop_heartbeat(self):
        self.sup.__exit__(None, None, None)

    def assert_dormant(self, store=None):
        store = store or self.store
        self.assertIsNone(store._repair_controller)
        self.assertIsNone(store._safe_qwen_identity_pending)
        self.assertEqual(store.state["work_units"]["units"][self.parent["unit_id"]]["attempts"], [])
        for key in ("repair_recovery", "repair_authorizations", "repair_dispatch", "repair_verification", "terminal_acceptance"):
            self.assertNotIn(key, store.state)
        self.assertIsNone(store.state["last_verified_checkpoint"])
        self.assertEqual(store.state["verified_progress"], [])
        scheduler = WorkUnitScheduler(store, self.repo, supervisor=self.sup)
        self.assertEqual(scheduler.reconciliation_stop.reason, "SAFEQWEN_RECOVERY_IDENTITY_ONLY")
        section = scheduler.get_section()
        for number in (1, 2, 3):
            with self.assertRaises(manager.Stop):
                scheduler.execute_attempt(section["units"][self.parent["unit_id"]], section, number)
        with self.assertRaises(manager.Stop):
            scheduler.revalidate_milestone(section)
        with self.assertRaises(durable.DurableError):
            store.commit_terminal("forbidden", controller=store._controller_token)

    def test_real_selection_two_distinct_single_attempt_slots_no_authority(self):
        before = copy.deepcopy(self.store.state["work_units"])
        identity = self.select()
        self.assertEqual(identity["schema_version"], 1)
        self.assertEqual(identity["kind"], "SAFEQWEN_IDENTITY_ONLY")
        self.assertEqual(self.store.state["work_units"], before)
        self.assertEqual(self.parent["limits"]["max_attempts"], 2)
        self.assertNotEqual(identity["invocations"][0]["execution_id"], identity["invocations"][1]["execution_id"])
        for number, slot in enumerate(identity["invocations"], 1):
            self.assertEqual(slot["attempt_number"], number)
            self.assertEqual(slot["attempt_id"], f"safe-qwen-task-attempt-{number}")
            self.assertEqual(slot["worker_spec"]["limits"]["max_attempts"], 1)
            terminal_run.validate_safe_qwen_unit_contract(slot["worker_spec"], self.cfg, self.executor)
        self.assertEqual(self.store.state["work_units"]["sequence"], [self.parent["unit_id"]])
        loaded = self.reload()
        self.assertEqual(loaded.state[durable._SAFE_QWEN_IDENTITY], identity)
        self.assert_dormant()
        self.assert_dormant(loaded)
        identity["invocations"].clear()
        self.assertEqual(len(self.store.state[durable._SAFE_QWEN_IDENTITY]["invocations"]), 2)

    def test_closed_schema_and_foreign_bindings(self):
        self.select()
        base = copy.deepcopy(self.store.state)
        mutations = [lambda i: i.pop("run_id"), lambda i: i.update(extra=True),
                     lambda i: i.update(schema_version=True), lambda i: i.update(schema_version=2),
                     lambda i: i.update(run_id="foreign"), lambda i: i.update(unit_id="foreign"),
                     lambda i: i.update(repository="C:/foreign"),
                     lambda i: i.update(controller_session={}),
                     lambda i: i.update(task_deadline=False), lambda i: i.update(unit_deadline=1),
                     lambda i: i.update(invocations=i["invocations"] * 2),
                     lambda i: i["invocations"][1].update(attempt_number=3),
                     lambda i: i["invocations"][0].update(attempt_number=True),
                     lambda i: i["invocations"][1].update(attempt_id=i["invocations"][0]["attempt_id"]),
                     lambda i: i["invocations"][1].update(execution_id=i["invocations"][0]["execution_id"]),
                     lambda i: i["invocations"][0]["worker_spec"]["limits"].update(max_attempts=2)]
        for key in ("parent_spec_sha256", "worker_spec_sha256", "scope_sha256", "registry_sha256", "config_sha256",
                    "model_pin_sha256", "environment_sha256", "budgets_sha256"):
            mutations.append(lambda i, key=key: i.update({key: "0" * 64}))
        for mutate in mutations:
            state = copy.deepcopy(base)
            mutate(state[durable._SAFE_QWEN_IDENTITY])
            with self.subTest(mutate=mutate), self.assertRaises(durable.DurableError):
                durable.validate_state(state)

    def test_config_model_session_scope_registry_parent_drift_refused(self):
        self.select()
        base = copy.deepcopy(self.store.state)
        def parent(state):
            state["work_units"]["units"][self.parent["unit_id"]]["spec"]["objective"] = "foreign"
        mutations = [lambda s: s["options"]["config"]["roles"].update(code="another-model"),
                     lambda s: s["options"]["config"].update(base_url="http://127.0.0.1:9999"),
                     lambda s: s["manager"].update(session=s["manager"]["session"] + 1),
                     lambda s: s["supervision"]["controller"].update(pid=1),
                     lambda s: s["manager"]["budgets"].update(max_runtime_seconds=123),
                     lambda s: s["options"]["verifier_registry"]["check-1"].update(timeout_seconds=1), parent]
        for mutate in mutations:
            state = copy.deepcopy(base)
            mutate(state)
            with self.subTest(mutate=mutate), self.assertRaises((durable.DurableError, work_units.WorkUnitError)):
                durable.validate_state(state)

    def test_no_json_commit_append_or_duplicate_selection(self):
        identity = self.select()
        self.stop_heartbeat()
        before = (self.store.directory / "events.jsonl").read_bytes()
        for action in (lambda: self.store.commit(safe_qwen_recovery_identity=identity),
                       lambda: self.store.append("state_committed", state=self.store.state, transition=None,
                                                 safe_qwen_recovery_identity={"event": "identity_selected"}),
                       lambda: self.store.select_safe_qwen_recovery_identity({}, self.parent["unit_id"]), self.select):
            with self.assertRaises(durable.DurableError):
                action()
        self.assertEqual((self.store.directory / "events.jsonl").read_bytes(), before)

    def test_ordinary_transport_and_repair_entrypoints_refuse_identity(self):
        identity = self.select()
        spec = identity["invocations"][0]["worker_spec"]
        with self.assertRaises(manager.ExecutorError):
            terminal_run.validate_safe_qwen_unit_contract(spec, self.cfg, self.executor, self.store)
        with self.assertRaises((adapter.AdapterError, manager.ExecutorError)):
            adapter.run_owned_gateway_worker(self.executor, spec, self.repo, gateway=self.gateway,
                                            audit_directory=self.fixture.root / "audit", timeout_seconds=2)
        with self.assertRaises(durable.DurableError):
            self.store._begin_repair_recovery(self.scheduler, self.parent["unit_id"])
        with self.assertRaises(durable.DurableError):
            self.store._require_repair_controller(identity)
        self.assert_dormant()

    def test_selection_after_attempt_or_invalid_parent_refused_before_reservation(self):
        section = copy.deepcopy(self.store.state["work_units"])
        unit = section["units"][self.parent["unit_id"]]
        work_units.transition_unit(unit, "EXECUTING")
        work_units.create_attempt(unit, "safe-qwen-task-attempt-1")
        work_units.seal_unit_state(unit)
        work_units._reseal_section(section)
        self.store.commit(work_units=section)
        with self.assertRaises(durable.DurableError):
            self.select()
        self.assertFalse((self.store.directory / durable._SAFE_QWEN_RESERVATION).exists())

    def test_foreign_scheduler_and_changed_model_pins_refused(self):
        with self.assertRaises(durable.DurableError):
            self.store.select_safe_qwen_recovery_identity(SimpleNamespace(store=self.store), self.parent["unit_id"])
        self.executor.config["roles"]["code"] = "foreign"
        with self.assertRaises((durable.DurableError, ValueError)):
            self.select()
        self.assertFalse((self.store.directory / durable._SAFE_QWEN_RESERVATION).exists())

    def test_repository_drift_and_expired_shared_budget_refuse(self):
        target = self.repo.root / "calculator.py"
        original = target.read_bytes()
        target.write_bytes(original + b"\n# drift\n")
        with self.assertRaises((durable.DurableError, manager.Stop)):
            self.select()
        target.write_bytes(original)
        with patch.object(self.scheduler, "remaining_task_budget", return_value=0), self.assertRaises(durable.DurableError):
            self.select()
        self.assertFalse((self.store.directory / durable._SAFE_QWEN_RESERVATION).exists())

    def test_crash_after_reservation_before_wal_fails_closed_on_reload(self):
        original = self.store.append
        def fail(event, **fields):
            if durable._SAFE_QWEN_IDENTITY in fields:
                raise OSError("before WAL")
            return original(event, **fields)
        with patch.object(self.store, "append", side_effect=fail), self.assertRaises(OSError):
            self.select()
        self.stop_heartbeat()
        self.assertTrue(self.store._repair_write_failed)
        with self.assertRaises(durable.DurableError):
            self.reload()

    def test_crash_after_wal_before_cache_reload_is_dormant(self):
        original = durable.atomic_write
        def fail(path, data):
            if self.store._safe_qwen_identity_pending is not None:
                raise OSError("cache replacement failed")
            return original(path, data)
        with patch("durable.atomic_write", side_effect=fail), self.assertRaises(OSError):
            self.select()
        self.stop_heartbeat()
        self.assertTrue(self.store._repair_write_failed)
        self.assert_dormant(self.reload())

    def test_uncertain_completed_wal_commit_latches_without_followup_write(self):
        original = self.store.append
        def fail(event, **fields):
            sequence = original(event, **fields)
            if durable._SAFE_QWEN_IDENTITY in fields:
                raise OSError("WAL succeeded but caller cannot establish outcome")
            return sequence
        with patch.object(self.store, "append", side_effect=fail), self.assertRaises(OSError):
            self.select()
        self.stop_heartbeat()
        wal = self.store.directory / "events.jsonl"
        before = wal.read_bytes()
        with self.assertRaises(durable.DurableError):
            self.store.commit(status="HUMAN_ACTION_REQUIRED")
        self.assertEqual(wal.read_bytes(), before)
        self.assert_dormant(self.reload())

    def test_concurrent_selection_reserves_exactly_once(self):
        barrier = threading.Barrier(2)
        results, failures = [], []
        def select():
            barrier.wait(timeout=5)
            try:
                results.append(self.select())
            except BaseException as exc:
                failures.append(exc)
        threads = [threading.Thread(target=select) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(results), 1)
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], durable.DurableError)
        self.assertEqual(sum(durable._SAFE_QWEN_IDENTITY in r for r in self.store.records), 1)
        self.assert_dormant(self.reload())

    def test_wrong_parent_and_third_slot_are_never_worker_units(self):
        for mutate in (lambda p: p["limits"].update(max_attempts=1),
                       lambda p: p["limits"].update(max_attempts=3),
                       lambda p: p.update(unit_id="independent-worker"),
                       lambda p: p.update(dependencies=["foreign"]),
                       lambda p: p["scope"].update(allowed_paths=["test_calculator.py"])):
            parent = copy.deepcopy(self.parent)
            mutate(parent)
            with self.assertRaises((ValueError, work_units.WorkUnitError)):
                terminal_run.recovery_worker_spec(parent, self.cfg)

    def test_projection_preserves_ordinary_scope_order_semantics(self):
        # Pure projection fixture only, not authenticated configuration or a
        # persisted identity: the ordinary contract compares sorted path lists.
        cfg, parent = copy.deepcopy(self.cfg), copy.deepcopy(self.parent)
        cfg["safe_qwen_identity"]["scope"]["frozen"] = ["a.py", "z.py"]
        parent["scope"]["forbidden_paths"] = ["z.py", "a.py"]
        worker = terminal_run.recovery_worker_spec(parent, cfg)
        self.assertEqual(worker["scope"], parent["scope"])
        self.assertEqual(worker["limits"]["max_attempts"], 1)

    def test_identity_removal_or_hash_valid_change_is_rejected_by_replay(self):
        self.select()
        self.stop_heartbeat()
        wal = self.store.directory / "events.jsonl"
        original = wal.read_bytes()
        for remove in (True, False):
            state = copy.deepcopy(self.store.state)
            state["revision"] += 1
            if remove:
                state.pop(durable._SAFE_QWEN_IDENTITY)
            else:
                identity = state[durable._SAFE_QWEN_IDENTITY]
                identity["identity_id"] = "0" * 32
                for slot in identity["invocations"]:
                    slot["execution_id"] = durable.digest([identity["identity_id"], identity["run_id"], identity["unit_id"],
                                                          slot["attempt_number"], identity["worker_spec_sha256"]])
            record = dict(sequence=len(self.store.records) + 1, time=durable.now(), event="state_committed",
                          previous=self.store.records[-1]["sha256"], state=state, transition=None)
            record["sha256"] = durable.digest(record)
            wal.write_bytes(original + durable.encoded(record) + b"\n")
            with self.assertRaises(durable.DurableError):
                self.reload()
        wal.write_bytes(original)

    def test_partial_initial_wal_even_before_identity_key_refuses(self):
        self.select()
        self.stop_heartbeat()
        wal = self.store.directory / "events.jsonl"
        lines = wal.read_bytes().splitlines(keepends=True)
        # Discard the selection and use an unrecognizable torn prefix. The
        # exclusive reservation, not substring guessing, makes this fatal.
        first = next(n for n, r in enumerate(self.store.records) if durable._SAFE_QWEN_IDENTITY in r)
        wal.write_bytes(b"".join(lines[:first]) + b'{"event":')
        prior = self.store.records[first - 1]["state"]
        (self.store.directory / "state.json").write_bytes(durable.encoded(prior) + b"\n")
        with self.assertRaises(durable.DurableError):
            self.reload()

    def test_active_torn_tail_missing_reservation_and_conflicting_history(self):
        self.select()
        self.stop_heartbeat()
        wal = self.store.directory / "events.jsonl"
        original = wal.read_bytes()
        wal.write_bytes(original + b'{"unrelated":')
        with self.assertRaises(durable.DurableError):
            self.reload()
        self.assertEqual(wal.read_bytes(), original + b'{"unrelated":')
        wal.write_bytes(original)
        reservation = self.store.directory / durable._SAFE_QWEN_RESERVATION
        reserved = reservation.read_bytes()
        reservation.unlink()
        with self.assertRaises(durable.DurableError):
            self.reload()
        reservation.write_bytes(reserved)
        records = copy.deepcopy(self.store.records)
        record = copy.deepcopy(records[-1])
        record.update(sequence=len(records) + 1, previous=records[-1]["sha256"])
        record["state"]["revision"] += 1
        record[durable._SAFE_QWEN_IDENTITY] = {"event": "identity_selected", "sha256": durable.digest(record["state"][durable._SAFE_QWEN_IDENTITY])}
        record.pop("sha256")
        record["sha256"] = durable.digest(record)
        wal.write_bytes(original + durable.encoded(record) + b"\n")
        with self.assertRaises(durable.DurableError):
            self.reload()

    def test_ordinary_m0_torn_tail_compatibility(self):
        self.stop_heartbeat()
        wal = self.store.directory / "events.jsonl"
        original = wal.read_bytes()
        wal.write_bytes(original + b'{"event":')
        self.assertEqual(self.reload().tail, b'{"event":')
        self.assertEqual(wal.read_bytes(), original + b'{"event":')

    def test_malformed_or_fabricated_identity_events_fail_closed(self):
        self.select()
        self.stop_heartbeat()
        wal = self.store.directory / "events.jsonl"
        original = wal.read_bytes()
        for event in (False, None, "safe_qwen_recovery_launched"):
            record = dict(sequence=len(self.store.records) + 1, time=durable.now(), event=event,
                          previous=self.store.records[-1]["sha256"])
            record["sha256"] = durable.digest(record)
            wal.write_bytes(original + durable.encoded(record) + b"\n")
            with self.assertRaises(durable.DurableError):
                self.reload()
        wal.write_bytes(original)


if __name__ == "__main__":
    unittest.main()
