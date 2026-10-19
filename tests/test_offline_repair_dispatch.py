"""Actual Windows dispatch qualification; fixed actions, no models/network.

Injected native/fault workers are test fixtures, never supported action commands.
Nothing here authenticates a grant from synthetic JSON or a returned dictionary.
"""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import durable
import manager
import offline_scripted_executor as offline
import supervision
import winprocess
import work_units
from work_unit_scheduler import WorkUnitScheduler, WorkUnitSchedulingError
from test_policy_b_shadow import make_shadow_fixture


REPAIR = {"schema_version": 1, "actions": [{"op": "replace", "path": "calculator.py", "old": "a + b", "new": "a * b"}]}


@unittest.skipUnless(os.name == "nt", "Actual native Windows lifecycle required")
class OfflineRepairDispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.addCleanup(patch.stopall)
        patch("harness.Gateway", side_effect=AssertionError("No models")).start()
        patch("durable.Gateway", side_effect=AssertionError("No models")).start()
        patch("socket.socket", side_effect=AssertionError("No network")).start()
        patch("socket.create_connection", side_effect=AssertionError("No network")).start()
        patch.dict(os.environ, {"PYTHONDONTWRITEBYTECODE": "1", "GIT_OPTIONAL_LOCKS": "0"}).start()
        self.prepare(self.root)

    def prepare(self, root):
        self.repo, self.store, self.sup, self.scheduler, self.executor = make_shadow_fixture(root)
        self.launched = []
        self.resumed = []

    def dispatch(self, actions=None, timeout=20):
        return self.scheduler.dispatch_offline_repair_recovery("unit-1", copy.deepcopy(REPAIR if actions is None else actions), timeout_seconds=timeout)

    def reload(self):
        store = durable.Store(self.store.directory.parent, self.store.directory.name)
        store.load()
        return store

    def assert_gone(self, pid):
        deadline = time.monotonic() + 10
        while winprocess.identity(pid) is not None and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIsNone(winprocess.identity(pid))

    def native_tracking(self):
        launch, resume = winprocess.ControllerVerifier.launch, winprocess.ControllerVerifier.resume
        def tracked_launch(native, command, cwd, streams):
            worker = command[-1].endswith("offline_scripted_executor_worker.py")
            if worker:
                self.assertEqual(self.reload().state["repair_dispatch"]["phase"], "INTENT")
                self.assertIn("a + b", (self.repo.root / "calculator.py").read_text())
            result = launch(native, command, cwd, streams)
            if worker:
                self.launched.append(native.pid)
            return result
        def tracked_resume(native, identity):
            if native.pid in self.launched:
                state = self.reload().state
                self.assertEqual(state["repair_dispatch"]["phase"], "RELEASED")
                self.assertEqual(state["repair_dispatch"]["process"], {"pid": native.pid, "identity": identity})
                native.verify(identity)
                self.resumed.append(native.pid)
            return resume(native, identity)
        return patch.object(winprocess.ControllerVerifier, "launch", tracked_launch), patch.object(winprocess.ControllerVerifier, "resume", tracked_resume)

    def assert_no_adoption(self, store=None):
        store = store or self.reload()
        wal = (store.directory / "events.jsonl").read_bytes()
        with patch.object(winprocess.ControllerVerifier, "launch", side_effect=AssertionError("No launch")), \
                patch.object(winprocess.ControllerVerifier, "resume", side_effect=AssertionError("No resume")), \
                patch.object(self.executor, "execute", side_effect=AssertionError("No executor")):
            scheduler = WorkUnitScheduler(store, self.repo, supervisor=supervision.Supervisor(store),
                                          agents=manager.Agents(None, self.executor, None, None))
            self.assertEqual(scheduler.run_sequence()["status"], "HUMAN_ACTION_REQUIRED")
            section = scheduler.get_section()
            unit = section["units"]["unit-1"]
            for operation in (lambda: scheduler.dispatch_offline_repair_recovery("unit-1", REPAIR),
                              lambda: scheduler.allocate_repair_recovery("unit-1"),
                              lambda: scheduler.authorize_repair_recovery("unit-1"),
                              lambda: scheduler.execute_attempt(unit, section, 2),
                              lambda: scheduler.execute_unit(unit, section),
                              lambda: scheduler.invoke_native_verifier("unit-1", "unit-1-attempt-2", "check-calc")):
                with self.assertRaises((manager.Stop, durable.DurableError)):
                    operation()
            with self.assertRaises((durable.DurableError, offline.OfflineExecutorError)):
                offline._run_controller_script(store._repair_controller)
        self.assertEqual((store.directory / "events.jsonl").read_bytes(), wal)

    def test_wal_order_one_launch_native_identity_before_resume_and_actual_repair(self):
        launch, resume = self.native_tracking()
        with launch, resume, patch.object(offline, "run_script", side_effect=AssertionError("No opaque executor")):
            result = self.dispatch()
        self.assertEqual(len(self.launched), 1)
        self.assertEqual(self.resumed, self.launched)
        self.assert_gone(self.launched[0])
        self.assertEqual(len(self.executor.calls), 1)
        self.assertEqual(self.executor.calls[0]["attempt_number"], 1)
        self.assertTrue(result["executor_evidence"]["success"])
        self.assertIn("a * b", (self.repo.root / "calculator.py").read_text())
        loaded = self.reload()
        markers = [r for r in loaded.records if "repair_dispatch" in r]
        self.assertEqual([r["state"]["repair_dispatch"]["phase"] for r in markers], ["INTENT", "BOUND", "RELEASED", "COMPLETE"])
        allocated = next(r for r in loaded.records if (r.get("repair_authorization") or {}).get("event") == "repair_authorization_consumed")
        unit = loaded.state["work_units"]["units"]["unit-1"]
        self.assertEqual(len(unit["attempts"]), 2)
        self.assertEqual(unit["attempts"][0], allocated["state"]["work_units"]["units"]["unit-1"]["attempts"][0])
        self.assertEqual(unit["attempts"][1]["stage"], "executor_complete")
        self.assertIsNone(unit["attempts"][1]["completed_at"])
        self.assertIsNone(unit["attempts"][1]["verifier_result"])
        self.assertEqual(loaded.state["verified_progress"], [])
        self.assertIsNone(loaded.state["last_verified_checkpoint"])
        self.assertGreater(loaded.state["manager"]["runtime_seconds"], allocated["state"]["manager"]["runtime_seconds"])
        self.assert_no_adoption(loaded)

    def test_script_failure_is_terminal_and_no_attempt_three(self):
        result = self.dispatch({"schema_version": 1, "actions": [{"op": "fail", "message": "fixed failure"}]})
        self.assertFalse(result["executor_evidence"]["success"])
        unit = self.reload().state["work_units"]["units"]["unit-1"]
        self.assertEqual(unit["status"], "UNIT_FAILED")
        self.assertEqual(unit["attempts"][1]["outcome"], "failed")
        with self.assertRaises(work_units.WorkUnitError):
            work_units.create_attempt(unit, "unit-1-attempt-3")
        self.assert_no_adoption()

    def test_timeout_drains_native_child_and_charges_cumulative_budget(self):
        launch, resume = self.native_tracking()
        with launch, resume:
            result = self.dispatch({"schema_version": 1, "actions": [{"op": "sleep", "seconds": 60}]}, timeout=8)
        self.assertEqual(len(self.resumed), 1)
        self.assertEqual(result["dispatch"]["result"]["outcome"], "executor_timeout")
        self.assertEqual(result["executor_evidence"]["cleanup"], "observed_empty")
        self.assert_gone(self.launched[0])
        unit = self.reload().state["work_units"]["units"]["unit-1"]
        self.assertEqual(unit["status"], "UNIT_FAILED")
        self.assertGreater(unit["attempts"][1]["timeout_info"]["elapsed_seconds"], 0)
        self.assert_no_adoption()

    def test_before_and_after_required_wal_writes_never_resume_or_reissue(self):
        for phase in ("INTENT", "BOUND", "RELEASED"):
            for after in (False, True):
                with self.subTest(phase=phase, after=after):
                    root = self.root / (phase + str(after)); root.mkdir()
                    self.prepare(root)
                    original = self.store.append
                    def fault(event, **fields):
                        selected = (fields.get("repair_dispatch") or {}).get("event") == "repair_dispatch_" + phase.lower()
                        if selected and not after:
                            raise OSError("before required WAL")
                        result = original(event, **fields)
                        if selected:
                            raise OSError("after required WAL")
                        return result
                    launch, resume = self.native_tracking()
                    with launch, resume, patch.object(self.store, "append", side_effect=fault), self.assertRaises(offline.OfflineExecutorError):
                        self.dispatch()
                    self.assertEqual(self.resumed, [])
                    self.assertTrue(self.store._repair_write_failed)
                    for pid in self.launched:
                        self.assert_gone(pid)
                    self.assert_no_adoption()

    def test_identity_and_release_cache_failure_refuse_resume(self):
        for phase in ("BOUND", "RELEASED"):
            with self.subTest(phase=phase):
                root = self.root / phase; root.mkdir(); self.prepare(root)
                original = durable.atomic_write
                def fault(path, payload):
                    if Path(path).name == "state.json" and json.loads(payload).get("repair_dispatch", {}).get("phase") == phase:
                        raise OSError("required cache commit uncertain")
                    return original(path, payload)
                launch, resume = self.native_tracking()
                with launch, resume, patch("durable.atomic_write", side_effect=fault), self.assertRaises(offline.OfflineExecutorError):
                    self.dispatch()
                self.assertEqual(self.resumed, [])
                self.assertEqual(self.reload().state["repair_dispatch"]["phase"], phase)
                for pid in self.launched:
                    self.assert_gone(pid)
                self.assert_no_adoption()

    def test_torn_dispatch_wal_refuses_load_without_launch_or_truncation(self):
        original = self.store.append
        def fault(event, **fields):
            if "repair_dispatch" in fields:
                with (self.store.directory / "events.jsonl").open("ab") as stream:
                    stream.write(b'{"sequence":')
                    stream.flush(); os.fsync(stream.fileno())
                raise OSError("torn intent")
            return original(event, **fields)
        with patch.object(self.store, "append", side_effect=fault), self.assertRaises(offline.OfflineExecutorError):
            self.dispatch()
        wal = (self.store.directory / "events.jsonl").read_bytes()
        with self.assertRaises(durable.DurableError):
            self.reload()
        self.assertEqual((self.store.directory / "events.jsonl").read_bytes(), wal)

    def test_complete_commit_uncertainty_never_relaunches(self):
        original = self.store.append
        def fault(event, **fields):
            result = original(event, **fields)
            if (fields.get("repair_dispatch") or {}).get("event") == "repair_dispatch_complete":
                raise OSError("completed WAL cache not published")
            return result
        launch, resume = self.native_tracking()
        with launch, resume, patch.object(self.store, "append", side_effect=fault), self.assertRaises(OSError):
            self.dispatch()
        self.assertEqual(len(self.resumed), 1)
        self.assertEqual(self.reload().state["repair_dispatch"]["phase"], "COMPLETE")
        self.assert_no_adoption()

    def test_duplicate_and_concurrent_dispatch_are_refused(self):
        original = offline._ControllerDispatch.intent
        failures = []
        def intent(dispatch, evidence):
            def duplicate():
                try:
                    offline._run_controller_script(dispatch.controller)
                except Exception as exc:
                    failures.append(exc)
            thread = threading.Thread(target=duplicate)
            thread.start(); thread.join(timeout=20)
            self.assertFalse(thread.is_alive())
            return original(dispatch, evidence)
        launch, resume = self.native_tracking()
        with launch, resume, patch.object(offline._ControllerDispatch, "intent", intent):
            self.dispatch()
        self.assertEqual(len(failures), 1)
        self.assertEqual(len(self.launched), 1)
        self.assert_no_adoption()

    def after_consume_refusal(self, change):
        original = self.store._consume_repair_grant
        def fault(controller):
            result = original(controller)
            change(controller)
            return result
        launch, resume = self.native_tracking()
        with launch, resume, patch.object(self.store, "_consume_repair_grant", side_effect=fault), \
                self.assertRaises((durable.DurableError, offline.OfflineExecutorError)):
            self.dispatch()
        self.assertEqual(self.launched, [])
        self.assertNotIn("repair_dispatch", self.reload().state)

    def test_stale_candidate_after_consumption_prevents_creation(self):
        self.after_consume_refusal(lambda c: self.repo.write("calculator.py", "# stale candidate\n"))

    def test_environment_drift_after_consumption_prevents_creation(self):
        self.after_consume_refusal(lambda c: patch("environment.compare", return_value={"decision": "UNSAFE"}).start())

    def test_foreign_session_and_expired_budget_prevent_creation(self):
        for kind in ("session", "budget", "ownership", "contracts", "reload"):
            with self.subTest(kind=kind):
                root = self.root / kind; root.mkdir(); self.prepare(root)
                extras = []
                def change(c):
                    if kind == "session": c.mode["controller_session"]["controller_sha256"] = "0" * 64
                    elif kind == "budget": self.scheduler.clock = lambda: c.mode["task_deadline"] + 1
                    elif kind == "ownership":
                        extra = patch("winprocess.boot_identity", return_value="foreign")
                        extra.start(); extras.append(extra)
                    elif kind == "contracts": self.scheduler.verifier_registry = {}
                    else: self.store.load()
                try:
                    self.after_consume_refusal(change)
                finally:
                    for extra in extras: extra.stop()

    def test_candidate_drift_after_intent_prevents_native_creation(self):
        original = self.store.append
        def drift(event, **fields):
            result = original(event, **fields)
            if (fields.get("repair_dispatch") or {}).get("event") == "repair_dispatch_intent":
                self.repo.write("calculator.py", "# drift after intent\n")
            return result
        launch, resume = self.native_tracking()
        with launch, resume, patch.object(self.store, "append", side_effect=drift), self.assertRaises(offline.OfflineExecutorError):
            self.dispatch()
        self.assertEqual(self.launched, [])
        self.assertEqual(self.reload().state["repair_dispatch"]["result"]["outcome"], "executor_failed")
        self.assert_no_adoption()

    def test_integrated_descendants_drained_on_natural_exit_and_timeout(self):
        original = winprocess.ControllerVerifier.launch
        for timed_out in (False, True):
            with self.subTest(timeout=timed_out):
                root = self.root / str(timed_out); root.mkdir(); self.prepare(root)
                marker = root / "descendant.json"
                worker = root / "fixed_fault_worker.py"
                source = ("import json,subprocess,sys,time\nfrom pathlib import Path\n"
                          "v=json.load(sys.stdin)\n"
                          "p=subprocess.Popen([sys.executable,'-I','-B','-c','import time; time.sleep(60)'],"
                          "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,close_fds=True)\n"
                          f"Path({str(marker)!r}).write_text(json.dumps({{'pid':p.pid}}))\n")
                source += ("time.sleep(60)\n" if timed_out else
                           "json.dump(dict(schema_version=1,script_sha256=v['script_sha256'],status='ok',actions_completed=1,error=None),sys.stdout)\n")
                worker.write_text(source, encoding="utf-8")
                def launch(native, command, cwd, streams):
                    if command[-1].endswith("offline_scripted_executor_worker.py"):
                        self.assertEqual(self.reload().state["repair_dispatch"]["phase"], "INTENT")
                        value = original(native, [str(Path(sys.executable).resolve()), "-I", "-B", str(worker)], cwd, streams)
                        self.launched.append(native.pid)
                        return value
                    return original(native, command, cwd, streams)
                _, resume = self.native_tracking()
                with patch.object(winprocess.ControllerVerifier, "launch", launch), resume:
                    result = self.dispatch({"schema_version": 1, "actions": [{"op": "emit", "text": ""}]}, timeout=8 if timed_out else 20)
                self.assertEqual(result["executor_evidence"]["cleanup"], "observed_empty")
                self.assertEqual(len(self.resumed), 1)
                self.assert_gone(json.loads(marker.read_text())["pid"])
                self.assertEqual(result["dispatch"]["result"]["outcome"], "executor_timeout" if timed_out else "executor_succeeded")
                self.assert_no_adoption()

    def test_issuance_and_allocation_only_cannot_dispatch(self):
        for operation in ("authorize_repair_recovery", "allocate_repair_recovery"):
            with self.subTest(operation=operation):
                root = self.root / operation; root.mkdir(); self.prepare(root)
                getattr(self.scheduler, operation)("unit-1")
                cap = self.store._repair_controller
                self.assertTrue(cap.closed)
                with self.assertRaises((offline.OfflineExecutorError, durable.DurableError)):
                    offline._run_controller_script(cap)
                self.assert_no_adoption()

    def test_json_fake_adapter_and_ordinary_store_writes_cannot_dispatch(self):
        for value in (None, {}, {"state": "CONSUMED"}):
            with self.assertRaises(offline.OfflineExecutorError):
                offline._run_controller_script(value)
        self.dispatch()
        cap = self.store._repair_controller
        fake = durable._RepairController(self.store, self.scheduler, cap.mode)
        with self.assertRaises(durable.DurableError):
            offline._run_controller_script(fake)
        with self.assertRaises(durable.DurableError):
            self.store.commit(repair_dispatch=self.store.state["repair_dispatch"])
        with self.assertRaises(durable.DurableError):
            self.store.append("repair_dispatch_released", process={"pid": 1})
        with self.assertRaises(durable.DurableError):
            self.store._advance_repair_dispatch({"state": "RELEASED"})

    def test_invalid_oversized_actions_and_callback_first_attempt_never_execute(self):
        original = (self.store.directory / "events.jsonl").read_bytes()
        bad = [None, {}, {"schema_version": 1, "actions": [{"op": "shell", "command": "python"}]},
               {"schema_version": 1, "actions": [{"op": "write", "path": "../escape", "content": "x"}]},
               {"schema_version": 1, "actions": [{"op": "emit", "text": "x" * 8192}]}]
        for value in bad:
            with self.subTest(value=str(value)[:30]), self.assertRaises((WorkUnitSchedulingError, offline.OfflineExecutorError)):
                # Fresh scheduler: a refusal deliberately closes this operation.
                scheduler = WorkUnitScheduler(self.store, self.repo, supervisor=self.sup,
                                              agents=manager.Agents(None, self.executor, None, None))
                scheduler.dispatch_offline_repair_recovery("unit-1", value)
            self.assertEqual((self.store.directory / "events.jsonl").read_bytes(), original)
        self.executor.set_behavior("unit-1", lambda *args: self.fail("Callback must not execute"))
        scheduler = WorkUnitScheduler(self.store, self.repo, supervisor=self.sup, agents=manager.Agents(None, self.executor, None, None))
        with self.assertRaises(WorkUnitSchedulingError):
            scheduler.dispatch_offline_repair_recovery("unit-1", REPAIR)
        self.assertEqual(self.executor.calls, [])

    def test_read_only_workunit_refused_before_attempt_or_grant(self):
        from test_work_units_wave2 import make_spec
        root = self.root / "read-only"; root.mkdir()
        with patch("test_policy_b_shadow.make_spec", side_effect=lambda **kw: make_spec(mode="read_only", **kw)):
            self.prepare(root)
        before = (self.store.directory / "events.jsonl").read_bytes()
        with self.assertRaisesRegex(WorkUnitSchedulingError, "mutation WorkUnit"):
            self.dispatch()
        self.assertEqual(self.executor.calls, [])
        self.assertNotIn("repair_recovery", self.reload().state)
        self.assertEqual((self.store.directory / "events.jsonl").read_bytes(), before)

    def test_replay_rejects_removed_phases_contradictions_and_duplicate_release(self):
        self.dispatch()
        records = copy.deepcopy(self.store.records)
        selected = [i for i, r in enumerate(records) if "repair_dispatch" in r]
        cases = []
        missing = copy.deepcopy(records); del missing[selected[1]]; cases.append(missing)
        duplicate = copy.deepcopy(records); duplicate.insert(selected[2] + 1, copy.deepcopy(duplicate[selected[2]])); cases.append(duplicate)
        contradictory = copy.deepcopy(records)
        contradictory[selected[2]]["state"]["repair_dispatch"]["process"]["identity"]["created"] = "1"
        cases.append(contradictory)
        foreign = copy.deepcopy(records)
        foreign[selected[0]]["state"]["repair_dispatch"]["consumption_wal"]["sha256"] = "0" * 64
        cases.append(foreign)
        for values in cases:
            with self.assertRaises(durable.DurableError):
                durable._validate_repair_history(values, self.store.read_evidence)
        ref = self.store.state["repair_dispatch"]["evidence"]
        path = self.store.directory / ref["path"]
        payload = path.read_bytes(); path.write_bytes(b"{}")
        try:
            with self.assertRaises(durable.DurableError): self.reload()
        finally:
            path.write_bytes(payload)

    def test_malformed_native_outcome_evidence_is_rejected(self):
        self.dispatch()
        state = self.reload().state
        evidence = self.store.read_evidence(state["repair_dispatch"]["evidence"])
        # Invalid schema projections of a real result; never authenticated grants.
        bad = [("schema_version", True), ("identity_verified", 1), ("execution_started", "true"),
               ("observed_exit_code", False), ("cleanup", "unknown"), ("termination_basis", "worker_claim"),
               ("execution_seconds", float("nan")), ("duration_seconds", -1),
               ("after_snapshot_sha256", "foreign"), ("changed_path_count", True), ("action_result", None)]
        for key, value in bad:
            altered = copy.deepcopy(evidence); altered[key] = value
            with self.subTest(key=key), self.assertRaises(durable.DurableError):
                durable._validate_repair_dispatch(state, lambda ref: altered)

    def test_real_controller_death_at_dispatch_boundaries_never_adopts(self):
        child_script = r'''
import os,sys,json
from pathlib import Path
from unittest.mock import patch
from test_policy_b_shadow import make_shadow_fixture
import winprocess
repo,store,sup,scheduler,executor=make_shadow_fixture(Path(sys.argv[1]))
phase=sys.argv[2]
append,launch,resume=store.append,winprocess.ControllerVerifier.launch,winprocess.ControllerVerifier.resume
worker_pids=[]
def crash_append(event,**fields):
    value=append(event,**fields)
    if (fields.get('repair_dispatch') or {}).get('event')=='repair_dispatch_'+phase.lower(): os._exit(73)
    return value
def crash_launch(native,command,cwd,streams):
    value=launch(native,command,cwd,streams)
    if command[-1].endswith('offline_scripted_executor_worker.py'):
        worker_pids.append(native.pid)
        Path(sys.argv[1],'worker.json').write_text(json.dumps({'pid':native.pid}))
        if phase=='CREATED': os._exit(73)
    return value
def crash_resume(native,identity):
    value=resume(native,identity)
    if native.pid in worker_pids and phase=='RUNNING': os._exit(73)
    return value
with patch('harness.Gateway',side_effect=AssertionError('No models')),patch('socket.socket',side_effect=AssertionError('No network')),patch.object(store,'append',side_effect=crash_append),patch.object(winprocess.ControllerVerifier,'launch',crash_launch),patch.object(winprocess.ControllerVerifier,'resume',crash_resume):
    scheduler.dispatch_offline_repair_recovery('unit-1',{'schema_version':1,'actions':[{'op':'replace','path':'calculator.py','old':'a + b','new':'a * b'}]},timeout_seconds=20)
'''
        for phase in ("INTENT", "CREATED", "BOUND", "RELEASED", "RUNNING", "COMPLETE"):
            with self.subTest(phase=phase):
                root = self.root / phase; root.mkdir()
                process = subprocess.run([sys.executable, "-B", "-c", child_script, str(root), phase], capture_output=True, timeout=90)
                self.assertEqual(process.returncode, 73, process.stderr.decode(errors="replace"))
                marker = root / "worker.json"
                if marker.exists(): self.assert_gone(json.loads(marker.read_text())["pid"])
                self.store = durable.Store(root / "runs", "shadow-test")
                loaded = self.reload()
                self.assertEqual(loaded.state["repair_dispatch"]["phase"],
                                 {"CREATED": "INTENT", "RUNNING": "RELEASED"}.get(phase, phase))
                self.assertEqual(len(loaded.state["work_units"]["units"]["unit-1"]["attempts"]), 2)
                # Use the matching repository for replay preflight, never the original fixture.
                self.repo.root = Path(loaded.state["target_repository"])
                self.assert_no_adoption(loaded)


if __name__ == "__main__":
    unittest.main()
