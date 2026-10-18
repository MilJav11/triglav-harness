"""Critical v7 persistence and actual controller-death boundaries, no inference."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import durable
import manager
import safe_qwen_active_recovery as active
import safe_qwen_active_verification as verification
import supervision
import winprocess
import work_unit_scheduler
import test_safe_qwen_active_second_verification as fixtures


@unittest.skipUnless(os.name == "nt", "Real native Windows lifecycle required")
class SecondVerificationCrashTests(unittest.TestCase):
    def setUp(self):
        fixtures.SecondVerificationTests.setUp(self)
        # Fault qualification needs durable dispatch, not repeated edit coverage.
        from test_supervised_safe_qwen import wire
        self.responses = [wire(self.submit), wire(self.submit)]
    prepare_timeout_fixture = fixtures.SecondVerificationTests.prepare_timeout_fixture
    stop_fixture = fixtures.SecondVerificationTests.stop_fixture
    servers = fixtures.SecondVerificationTests.servers
    close_supervisor = fixtures.SecondVerificationTests.close_supervisor
    reload = fixtures.SecondVerificationTests.reload
    run_verified = fixtures.SecondVerificationTests.run_verified

    def fail_boundary(self, event, *, before=False, torn=False, cache=False):
        append, write = self.store.append, durable.atomic_write
        captured, created, resumed = [], [], []
        launch, resume = winprocess.ControllerVerifier.launch, winprocess.ControllerVerifier.resume
        def created_native(native, *args):
            result = launch(native, *args); created.append((native.pid, copy.deepcopy(native.identity))); return result
        def released(native, identity):
            result = resume(native, identity); resumed.append(native.pid); return result
        def faulty(kind, **fields):
            data = fields.get("state", {}).get(active.KEY, {})
            marker = fields.get(active.MARKER) or fields.get("transition") or {}
            hit = (marker.get("event") == event and
                   (event == "AI_COMPLETE" or data.get("second_verification") is not None))
            if hit and before:
                if torn:
                    with (self.store.directory / "events.jsonl").open("ab") as stream:
                        stream.write(b'{"sequence":'); stream.flush(); os.fsync(stream.fileno())
                captured.append((self.store.directory / "events.jsonl").read_bytes())
                raise OSError("Before " + event)
            result = append(kind, **fields)
            if hit:
                captured.append((self.store.directory / "events.jsonl").read_bytes())
                if not cache: raise OSError("Uncertain " + event)
            return result
        def cache_failure(path, raw):
            if captured and path == self.store.directory / "state.json": raise OSError("Uncertain cache")
            return write(path, raw)
        with patch.object(self.store, "append", side_effect=faulty), patch("durable.atomic_write", side_effect=cache_failure), \
                patch.object(winprocess.ControllerVerifier, "launch", created_native), patch.object(winprocess.ControllerVerifier, "resume", released), \
                self.assertRaises((OSError, RuntimeError, durable.DurableError)):
            self.run_verified()
        self.assertTrue(captured, "Required fault boundary reached")
        self.assertTrue(self.store._repair_write_failed)
        self.assertIsNone(self.store._safe_qwen_active_controller)
        self.assertEqual((self.store.directory / "events.jsonl").read_bytes(), captured[-1], "No write after uncertain persistence")
        self.assertTrue(all(winprocess.identity(pid) != identity for pid, identity in created))
        if event in {"AI_COMPLETE", "VERIFY2_INTENT", "invocation_intent"}: self.assertEqual(len(created), 3)
        if event in {"invocation_bound", "invocation_release"}:
            self.assertEqual(len(created), 4); self.assertEqual(len(resumed), 3)
        if torn:
            with self.assertRaises(durable.DurableError): self.reload()
            return
        loaded = self.reload()
        self.assertIsNone(loaded._safe_qwen_active_controller)
        self.assertEqual(len(loaded.state["work_units"]["units"]["safe-qwen-task"]["attempts"]), 2)
        self.assertIsNone(loaded.state["last_verified_checkpoint"])
        wal = (loaded.directory / "events.jsonl").read_bytes()
        with patch.object(winprocess.ControllerVerifier, "launch", side_effect=AssertionError("Replay launched verifier")):
            scheduler = work_unit_scheduler.WorkUnitScheduler(loaded, self.repo, supervisor=supervision.Supervisor(loaded))
            with self.assertRaises((durable.DurableError, AttributeError)): verification.run(loaded.state[active.KEY], second=True)
            with self.assertRaises((manager.Stop, durable.DurableError)):
                scheduler.invoke_native_verifier("safe-qwen-task", "safe-qwen-task-attempt-2", "check-1")
        self.assertEqual((loaded.directory / "events.jsonl").read_bytes(), wal)

    def test_before_verification_intent_no_creation(self): self.fail_boundary("VERIFY2_INTENT", before=True)
    def test_uncertain_verification_intent_no_creation(self): self.fail_boundary("VERIFY2_INTENT")
    def test_torn_verification_intent_refuses_reload(self): self.fail_boundary("VERIFY2_INTENT", before=True, torn=True)
    def test_uncertain_dispatch_complete_never_verifies(self): self.fail_boundary("AI_COMPLETE")
    def test_uncertain_native_intent_no_creation(self): self.fail_boundary("invocation_intent")
    def test_uncertain_native_binding_never_resumes(self): self.fail_boundary("invocation_bound")
    def test_uncertain_native_release_never_resumes(self): self.fail_boundary("invocation_release")
    def test_uncertain_native_exit_stops_all_writes(self): self.fail_boundary("process_exit")
    def test_uncertain_native_complete_stops_all_writes(self): self.fail_boundary("invocation_complete")
    def test_uncertain_observation_never_seals(self): self.fail_boundary("VERIFY2_OBSERVATION")
    def test_uncertain_verification_complete_is_historical_only(self): self.fail_boundary("VERIFY2_COMPLETE")
    def test_complete_wal_stale_cache_never_relaunches(self): self.fail_boundary("VERIFY2_COMPLETE", cache=True)

    def test_actual_controller_death_before_and_after_native_launch_and_resume(self):
        source = Path(__file__).resolve().parents[1]
        for boundary in ("VERIFY2_INTENT", "invocation_bound", "resumed"):
            with self.subTest(boundary=boundary):
                scratch = self.root / boundary; scratch.mkdir()
                info = scratch / "selected.json"; script = scratch / "controller-death.py"
                script.write_text(
                    "import os,sys,tempfile,json\nfrom pathlib import Path\n"
                    f"sys.path[:0]=[{str(source)!r},{str(source/'tests')!r}]\n"
                    f"tempfile.tempdir={str(scratch)!r}\n"
                    "from test_safe_qwen_active_second_verification import SecondVerificationTests\n"
                    "import winprocess\n"
                    "t=SecondVerificationTests('test_timeout_drains_native_descendant');t.setUp()\n"
                    "original=t.store.append\n"
                    "def die():\n"
                    " d=t.store.state['safe_qwen_active_recovery']\n"
                    " children=[c for c in t.store.state['supervision']['children'].values() if c.get('invocation',{}).get('attempt_id')=='safe-qwen-task-attempt-2']\n"
                    f" Path({str(info)!r}).write_text(json.dumps({{'directory':str(t.store.directory),'children':children}}))\n"
                    " os._exit(73)\n"
                    "def crash(event,**fields):\n result=original(event,**fields)\n"
                    " d=fields.get('state',{}).get('safe_qwen_active_recovery',{})\n"
                    " marker=fields.get('safe_qwen_active_event') or fields.get('transition') or {}\n"
                    f" if d.get('second_verification') is not None and marker.get('event')=={boundary!r}:\n"
                    "  t.store.state=fields['state'];die()\n return result\n"
                    "resume=winprocess.ControllerVerifier.resume\n"
                    "def released(native,identity):\n result=resume(native,identity)\n"
                    f" if {boundary!r}=='resumed' and t.store.state.get('safe_qwen_active_recovery',{{}}).get('second_verification') is not None:die()\n"
                    " return result\n"
                    "t.store.append=crash\nwinprocess.ControllerVerifier.resume=released\nt.run_verified()\n")
                native = winprocess.ControllerVerifier("Local\\ai-verification-death-" + boundary + "-" + self.root.name)
                try:
                    with tempfile.TemporaryFile() as inp, tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
                        native.launch([str(Path(sys.executable).resolve()), "-I", "-B", str(script)], scratch, (inp, out, err))
                        native.resume(native.identity); code = native.wait(650); err.seek(0)
                        self.assertEqual(code, 73, err.read(8192).decode(errors="replace"))
                        selected = json.loads(info.read_text())
                        end = time.monotonic() + 5
                        while any(winprocess.identity(c["pid"]) == c["identity"] for c in selected["children"] if c["pid"]) and time.monotonic() < end:
                            time.sleep(0.01)
                        # The outer fixture Job is still open. The verifier's own Job must drain.
                        for child in selected["children"]:
                            if child["pid"]: self.assertNotEqual(winprocess.identity(child["pid"]), child["identity"])
                finally:
                    native.terminate_job(); native.drain(10); native.close()
                directory = Path(selected["directory"])
                loaded = durable.Store(directory.parent, directory.name); wal = (directory / "events.jsonl").read_bytes()
                with patch.object(winprocess.ControllerVerifier, "launch", side_effect=AssertionError("Reload created native verifier")):
                    loaded.load()
                    self.assertIsNone(loaded._safe_qwen_active_controller)
                    with self.assertRaises((durable.DurableError, AttributeError)): verification.run(loaded.state[active.KEY], second=True)
                self.assertEqual((directory / "events.jsonl").read_bytes(), wal)
                self.assertEqual(loaded.state[active.KEY]["second_verification"]["phase"], "OBSERVING")
                self.assertIsNone(loaded.state["last_verified_checkpoint"])
