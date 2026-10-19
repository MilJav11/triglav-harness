"""Focused fault/replay boundaries for ACTIVE v6; no real model transport."""
import copy
import os
import unittest
from unittest.mock import patch

import durable
import safe_qwen_active_recovery as active
import safe_qwen_active_dispatch as dispatch
import supervised_safe_qwen as adapter
import test_safe_qwen_active_dispatch as fixtures


@unittest.skipUnless(os.name == "nt", "Real native Windows lifecycle required")
class DispatchBoundaryTests(unittest.TestCase):
    setUp = fixtures.ActiveDispatchTests.setUp
    prepare_timeout_fixture = fixtures.ActiveDispatchTests.prepare_timeout_fixture
    stop_fixture = fixtures.ActiveDispatchTests.stop_fixture
    servers = fixtures.ActiveDispatchTests.servers
    close_supervisor = fixtures.ActiveDispatchTests.close_supervisor
    reload = fixtures.ActiveDispatchTests.reload
    run_dispatch = fixtures.ActiveDispatchTests.run_dispatch

    def fail_boundary(self, event, *, before=False, torn=False, cache=False):
        original = self.store.append
        write = durable.atomic_write
        captured, created, resumed, identities = [], [], [], {}
        import winprocess
        launch, resume = winprocess.ControllerVerifier.launch, winprocess.ControllerVerifier.resume
        def launched(native, *args):
            result = launch(native, *args); created.append(native.pid); identities[native.pid] = copy.deepcopy(native.identity); return result
        def released(native, identity):
            result = resume(native, identity); resumed.append(native.pid); return result
        def faulty(kind, **fields):
            hit = fields.get(active.MARKER, {}).get("event") == event
            if hit and before:
                if torn:
                    with (self.store.directory / "events.jsonl").open("ab") as stream:
                        stream.write(b'{"sequence":'); stream.flush(); os.fsync(stream.fileno())
                captured.append((self.store.directory / "events.jsonl").read_bytes())
                raise OSError("Before " + event)
            result = original(kind, **fields)
            if hit:
                captured.append((self.store.directory / "events.jsonl").read_bytes())
                if not cache: raise OSError("Uncertain " + event)
            return result
        def cache_failed(path, raw):
            if captured and path == self.store.directory / "state.json": raise OSError("Cache failure")
            return write(path, raw)
        with patch.object(self.store, "append", side_effect=faulty), patch("durable.atomic_write", side_effect=cache_failed), \
                patch.object(winprocess.ControllerVerifier, "launch", launched), patch.object(winprocess.ControllerVerifier, "resume", released), \
                self.assertRaises((OSError, durable.DurableError, adapter.AdapterError)):
            self.run_dispatch()
        self.assertTrue(captured, "Required boundary was reached")
        self.assertTrue(self.store._repair_write_failed)
        self.assertEqual((self.store.directory / "events.jsonl").read_bytes(), captured[-1])
        self.assertIsNone(self.store._safe_qwen_active_controller)
        self.assertFalse([dict(pid=pid, original=identities[pid], current=winprocess.identity(pid)) for pid in created
                          if winprocess.identity(pid) == identities[pid]])
        if event == "AI_INTENT": self.assertEqual(len(created), 2)
        if event == "AI_RELEASE":
            self.assertEqual(len(created), 3)
            self.assertEqual(len(resumed), 2)
        if event == "AI_EDIT_INTENT": self.assertEqual((self.repo.root / "calculator.py").read_bytes(), self.before)
        if torn:
            with self.assertRaises(durable.DurableError): self.reload()
        else:
            loaded = self.reload()
            self.assertEqual(len(loaded.state["work_units"]["units"]["safe-qwen-task"]["attempts"]), 2)
            self.assertIsNone(loaded._safe_qwen_active_controller)
            with self.assertRaises(durable.DurableError): dispatch.run(loaded.state[active.KEY])

    def test_failure_before_dispatch_intent_launches_nothing(self): self.fail_boundary("AI_INTENT", before=True)
    def test_uncertain_dispatch_intent_launches_nothing(self): self.fail_boundary("AI_INTENT")
    def test_torn_dispatch_intent_refuses_reload(self): self.fail_boundary("AI_INTENT", before=True, torn=True)
    def test_uncertain_worker_binding_never_resumes(self): self.fail_boundary("AI_RELEASE")
    def test_failure_before_worker_binding_never_resumes(self): self.fail_boundary("AI_RELEASE", before=True)
    def test_uncertain_model_intent_never_sends_request(self): self.fail_boundary("AI_MODEL_INTENT")
    def test_uncertain_model_response_never_dispatches_tool(self): self.fail_boundary("AI_MODEL_RESPONSE")
    def test_uncertain_edit_intent_never_mutates(self): self.fail_boundary("AI_EDIT_INTENT")
    def test_uncertain_edit_observation_never_reapplies(self): self.fail_boundary("AI_EDIT_APPLIED")
    def test_uncertain_completion_is_historical_only(self): self.fail_boundary("AI_COMPLETE")
    def test_completed_wal_stale_cache_never_relaunches(self): self.fail_boundary("AI_COMPLETE", cache=True)

    def refuse(self, change, *, error=durable.DurableError):
        operation = dispatch.prepare
        def changed(owner):
            self.assertEqual(next(iter(owner.store.state["repair_authorizations"].values()))["state"], "CONSUMED")
            change(owner)
            return operation(owner)
        with patch.object(dispatch, "prepare", side_effect=changed), self.assertRaises(error): self.run_dispatch()
        state = self.reload().state
        self.assertIsNone(state[active.KEY]["dispatch"])
        self.assertEqual(state["work_units"]["units"]["safe-qwen-task"]["attempts"][1]["stage"], "allocated")

    def test_candidate_drift_refuses_before_intent(self):
        self.refuse(lambda owner: (self.repo.root / "calculator.py").write_text("# foreign candidate\n"))

    def test_config_model_drift_refuses_before_intent(self):
        self.refuse(lambda owner: owner.executor.config["roles"].update(code="foreign-model"), error=ValueError)

    def test_gateway_parent_drift_refuses_before_intent(self):
        from pathlib import Path
        self.refuse(lambda owner: Path(owner.executor.config["safe_qwen_gateway"]["pid_file"]).write_text("99999\n"))

    def test_foreign_session_refuses_before_intent(self):
        self.refuse(lambda owner: owner.identity["session"].update(manager_session=999))

    def test_scope_drift_refuses_before_intent(self):
        def drift(owner):
            owner.scheduler.parent_scope = copy.deepcopy(owner.scheduler.parent_scope)
            owner.scheduler.parent_scope["allowed_paths"].append("foreign.py")
        self.refuse(drift)

    def test_expired_budget_refuses_before_intent(self):
        def expired(owner):
            item = patch.object(owner.scheduler, "remaining_task_budget", return_value=0)
            item.start(); self.addCleanup(item.stop)
        self.refuse(expired)

    def test_concurrent_foreign_thread_cannot_prepare_dispatch(self):
        import threading
        operation = dispatch.prepare
        errors = []
        class Stopped(Exception): pass
        def selected(owner):
            def foreign():
                try: operation(owner)
                except durable.DurableError: errors.append("refused")
            worker = threading.Thread(target=foreign)
            worker.start(); worker.join(5)
            self.assertFalse(worker.is_alive())
            raise Stopped()
        with patch.object(dispatch, "prepare", side_effect=selected), self.assertRaises(Stopped): self.run_dispatch()
        self.assertEqual(errors, ["refused"])
        self.assertIsNone(self.reload().state[active.KEY]["dispatch"])

    def test_cleanup_failure_refuses_completion_and_replay(self):
        def fail_cleanup():
            if self.store.state[active.KEY]["dispatch"] is not None:
                self.cleanup_error = RuntimeError("Injected owned model cleanup failure")
        self.on_request = fail_cleanup
        with self.assertRaises((durable.DurableError, adapter.AdapterError)): self.run_dispatch()
        self.assertTrue(self.store._repair_write_failed)
        state = self.reload().state
        self.assertEqual(state[active.KEY]["dispatch"]["phase"], "RELEASED")
        self.assertIsNone(self.store._safe_qwen_active_controller)
        self.assertIsNone(state["last_verified_checkpoint"])

    def test_repository_drift_stops_edits_but_allows_owned_cleanup(self):
        changed = []
        def drift():
            if self.store.state[active.KEY]["dispatch"] is not None and not changed:
                (self.repo.root / "foreign.txt").write_text("external mutation\n")
                changed.append(True)
        self.on_request = drift
        with self.assertRaises((durable.DurableError, adapter.AdapterError)): self.run_dispatch()
        self.assertEqual(changed, [True])
        self.assertIsNone(self.proc, "Unchanged owned model must still unload safely")
        self.assertEqual((self.repo.root / "calculator.py").read_bytes(), self.before)

    def test_existing_v5_allocation_has_no_dispatch_authority(self):
        from test_supervised_safe_qwen import wire
        self.responses = [wire(self.submit)]
        consumed = durable.Store._consume_ai_repair_grant
        owners = []
        def capture(store, owner):
            owners.append(owner)
            return consumed(store, owner)
        with patch.object(durable.Store, "_consume_ai_repair_grant", capture):
            self.scheduler.allocate_safe_qwen_repair_recovery("safe-qwen-task", gateway=self.gateway, timeout_seconds=180)
        self.assertTrue(owners[0].closed)
        with self.assertRaises(durable.DurableError): dispatch.run(owners[0])
        with self.assertRaises(durable.DurableError): self.run_dispatch()
        self.assertEqual(self.reload().state[active.KEY]["schema_version"], 5)

    def test_real_controller_death_before_resume_and_during_execution(self):
        import json
        import sys
        import tempfile
        import time
        from pathlib import Path
        import winprocess
        source = Path(__file__).resolve().parents[1]
        for boundary in ("AI_RELEASE", "AI_MODEL_INTENT"):
            with self.subTest(boundary=boundary):
                scratch = self.root / boundary; scratch.mkdir()
                info = scratch / "selected.json"
                script = scratch / "controller-death.py"
                script.write_text(
                    "import sys,os,tempfile,json\nfrom pathlib import Path\n"
                    f"sys.path[:0]=[{str(source)!r},{str(source/'tests')!r}]\n"
                    f"tempfile.tempdir={str(scratch)!r}\n"
                    "from test_safe_qwen_active_dispatch import ActiveDispatchTests\n"
                    "t=ActiveDispatchTests('test_one_second_worker_repairs_file_and_stops_unverified');t.setUp()\n"
                    "original=t.store.append\n"
                    "def crash(event,**fields):\n result=original(event,**fields)\n"
                    f" if fields.get('safe_qwen_active_event',{{}}).get('event')=={boundary!r}:\n"
                    "  d=fields['state']['safe_qwen_active_recovery']['dispatch']\n"
                    f"  Path({str(info)!r}).write_text(json.dumps({{'directory':str(t.store.directory),'pid':d['process']['pid'],'identity':d['process']['identity']}}))\n"
                    "  os._exit(73)\n return result\n"
                    "t.store.append=crash\nt.run_dispatch()\n")
                native = winprocess.ControllerVerifier("Local\\ai-dispatch-death-" + boundary + "-" + self.root.name)
                try:
                    with tempfile.TemporaryFile() as inp, tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
                        native.launch([str(Path(sys.executable).resolve()), "-I", "-B", str(script)], scratch, (inp, out, err))
                        native.resume(native.identity)
                        code = native.wait(150); err.seek(0)
                        self.assertEqual(code, 73, err.read(8192).decode(errors="replace"))
                        selected = json.loads(info.read_text())
                        end = time.monotonic() + 5
                        while winprocess.identity(selected["pid"]) == selected["identity"] and time.monotonic() < end: time.sleep(0.01)
                        # Outer fixture Job stays OPEN: the worker's own Job
                        # must kill it when its controller disappears.
                        self.assertNotEqual(winprocess.identity(selected["pid"]), selected["identity"])
                finally:
                    native.terminate_job(); native.drain(10); native.close()
                directory = Path(selected["directory"])
                loaded = durable.Store(directory.parent, directory.name)
                before = (directory / "events.jsonl").read_bytes()
                loaded.load()
                self.assertIsNone(loaded._safe_qwen_active_controller)
                self.assertEqual(loaded.state[active.KEY]["dispatch"]["phase"], "RELEASED")
                with self.assertRaises(durable.DurableError): dispatch.run(loaded.state[active.KEY])
                self.assertEqual((directory / "events.jsonl").read_bytes(), before)

    def test_native_timeout_kills_worker_and_descendant(self):
        import json
        import sys
        import winprocess
        marker = self.root / "worker-descendant.json"
        worker = self.root / "hung-worker.py"
        worker.write_text("import subprocess,sys,time,json\nfrom pathlib import Path\n"
                          "p=subprocess.Popen([sys.executable,'-I','-B','-c','import time;time.sleep(120)'],close_fds=True)\n"
                          f"Path({str(marker)!r}).write_text(json.dumps({{'pid':p.pid}}))\ntime.sleep(120)\n")
        launch = winprocess.ControllerVerifier.launch
        created = []
        def launched(native, argv, cwd, streams):
            if len(created) == 2:
                self.assertEqual(self.reload().state[active.KEY]["dispatch"]["phase"], "INTENT")
                argv = [*argv[:-1], str(worker)]  # Fixed native fault fixture, never model data.
            result = launch(native, argv, cwd, streams); created.append(native.pid); return result
        with patch.object(winprocess.ControllerVerifier, "launch", launched):
            result = self.scheduler.dispatch_safe_qwen_repair_recovery("safe-qwen-task", gateway=self.gateway, timeout_seconds=90)
        self.assertEqual(result["dispatch"]["result"]["evidence"]["termination_basis"], "controller_deadline")
        self.assertEqual(result["dispatch"]["result"]["evidence"]["cleanup"], "observed_empty")
        self.assertTrue(marker.exists())
        self.assertIsNone(winprocess.identity(json.loads(marker.read_text())["pid"]))
        self.assertTrue(all(winprocess.identity(pid) is None for pid in created))
        self.assertEqual(self.reload().state["work_units"]["units"]["safe-qwen-task"]["status"], "UNIT_FAILED")
