"""Real Windows standalone qualification. No recovery dispatch, models or network.

Fault workers are fixed test fixtures, not supported adapter commands or authenticated
executor evidence. All repositories/processes are disposable and owned by the test.
"""
import ctypes
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

import offline_scripted_executor as offline
import winprocess


def script(*actions):
    return {"schema_version": 1, "actions": list(actions)}


@unittest.skipUnless(os.name == "nt", "Real Windows Job/retained-handle qualification")
class SupervisedOfflineExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repository"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True, capture_output=True)
        (self.repo / "calculator.py").write_text("def multiply(a, b): return a + b\n", encoding="utf-8")
        self.addCleanup(patch.stopall)
        patch("socket.socket", side_effect=AssertionError("No network")).start()
        patch("socket.create_connection", side_effect=AssertionError("No network")).start()
        patch("harness.Gateway", side_effect=AssertionError("No models")).start()
        patch("durable.Gateway", side_effect=AssertionError("No models")).start()
        patch("work_unit_scheduler.WorkUnitScheduler.execute_attempt", side_effect=AssertionError("No scheduler integration")).start()
        patch("durable.Store._consume_repair_grant", side_effect=AssertionError("No repair dispatch")).start()

    def run_script(self, actions, **kwargs):
        return offline.run_script(self.repo, actions, allowed_paths=["calculator.py"],
                                  timeout_seconds=kwargs.pop("timeout_seconds", 5), **kwargs)

    def assert_gone(self, pid):
        end = time.monotonic() + 10
        while winprocess.identity(pid) is not None and time.monotonic() < end:
            time.sleep(0.02)
        self.assertIsNone(winprocess.identity(pid))

    def assert_clean(self, evidence):
        self.assertEqual(evidence["source"], "controller_native_handles")
        self.assertEqual(evidence["cleanup"], "observed_empty")
        self.assertLessEqual(len(offline._json(evidence)), offline.MAX_EVIDENCE_BYTES)
        if evidence["process"]:
            self.assert_gone(evidence["process"]["pid"])

    def fault_worker(self, source):
        """Inject a fixed fixture at the native launch seam, never through protocol."""
        worker = self.root / "fault_worker.py"
        worker.write_text(source, encoding="utf-8")
        original = winprocess.ControllerVerifier.launch
        def launch(native, command, cwd, streams):
            return original(native, [str(Path(sys.executable).resolve()), "-I", "-B", str(worker)], cwd, streams)
        return patch.object(winprocess.ControllerVerifier, "launch", launch)

    def test_successful_edit_exact_contents_and_native_evidence(self):
        result = self.run_script(script({"op": "write", "path": "calculator.py",
                                        "content": "def multiply(a, b): return a * b\n"}))
        self.assertTrue(result["success"])
        self.assertTrue(result["scope_verified"])
        self.assertTrue(result["exact_edits_verified"])
        self.assertTrue(result["identity_verified"])
        self.assertEqual(result["termination_basis"], "signaled_without_controller_termination")
        self.assertEqual(result["observed_exit_code"], 0)
        self.assertEqual(result["changed_paths"], ["calculator.py"])
        self.assertIn("a * b", (self.repo / "calculator.py").read_text())
        self.assert_clean(result)

    def test_replacement_and_deterministic_failed_test_then_repaired_test(self):
        test = self.repo / "test_calculator.py"
        test.write_text("import unittest\nfrom calculator import multiply\n"
                        "class Test(unittest.TestCase):\n def test_multiply(self): self.assertEqual(multiply(3, 4), 12)\n",
                        encoding="utf-8")
        # -I intentionally excludes cwd; deterministic test execution uses discovery.
        before = subprocess.run([sys.executable, "-I", "-B", "-m", "unittest", "discover", "-s", str(self.repo)],
                                capture_output=True)
        self.assertEqual(before.returncode, 1)
        self.assertIn(b"7 != 12", before.stderr)
        result = self.run_script(script({"op": "replace", "path": "calculator.py", "old": "a + b", "new": "a * b"}))
        self.assertTrue(result["success"])
        after = subprocess.run([sys.executable, "-I", "-B", "-m", "unittest", "discover", "-s", str(self.repo)],
                               capture_output=True)
        self.assertEqual(after.returncode, 0, after.stderr)
        self.assert_clean(result)

    def test_new_nested_file_with_exact_allowlist(self):
        result = offline.run_script(self.repo, script({"op": "write", "path": "src/result.txt", "content": "ok\n"}),
                                    allowed_paths=["src/result.txt"], timeout_seconds=5)
        self.assertTrue(result["success"])
        self.assertEqual(result["changed_paths"], ["src", "src/result.txt"])
        self.assert_clean(result)

    def test_deterministic_failure_preserves_partial_edits_without_success(self):
        result = self.run_script(script({"op": "write", "path": "calculator.py", "content": "changed\n"},
                                        {"op": "fail", "message": "fixed offline failure"}))
        self.assertFalse(result["success"])
        self.assertEqual(result["observed_exit_code"], 10)
        self.assertEqual(result["action_result"]["error"], "fixed offline failure")
        self.assertEqual(result["action_result"]["actions_completed"], 1)
        self.assertTrue(result["exact_edits_verified"])
        self.assert_clean(result)

    def test_ambiguous_replace_fails_without_edit(self):
        result = self.run_script(script({"op": "replace", "path": "calculator.py", "old": "absent", "new": "x"}))
        self.assertFalse(result["success"])
        self.assertEqual(result["changed_paths"], [])
        self.assert_clean(result)

    def test_hung_child_deadline_is_enforced_during_execution(self):
        start = time.monotonic()
        result = self.run_script(script({"op": "sleep", "seconds": 60}), timeout_seconds=0.5)
        self.assertLess(time.monotonic() - start, 5)
        self.assertTrue(result["execution_started"])
        self.assertFalse(result["success"])
        self.assertEqual(result["termination_basis"], "controller_deadline")
        self.assertEqual(result["observed_exit_code"], 137)
        self.assert_clean(result)

    def test_cancel_during_execution_terminates_owned_child(self):
        event = threading.Event()
        original = winprocess.ControllerVerifier.resume
        def resume(native, identity):
            original(native, identity)
            event.set()
        with patch.object(winprocess.ControllerVerifier, "resume", resume):
            result = self.run_script(script({"op": "sleep", "seconds": 60}), cancel_event=event)
        self.assertFalse(result["success"])
        self.assertEqual(result["termination_basis"], "controller_cancel")
        self.assert_clean(result)

    def test_pre_cancel_never_creates_native_child(self):
        event = threading.Event()
        event.set()
        with patch.object(winprocess, "ControllerVerifier") as native, self.assertRaises(offline.OfflineExecutorError):
            self.run_script(script({"op": "sleep", "seconds": 60}), cancel_event=event)
        native.assert_not_called()

    def test_launch_is_suspended_job_owned_and_controller_is_not_member(self):
        original = winprocess.ControllerVerifier.resume
        observed = []
        def resume(native, identity):
            self.assertNotIn("fixed", (self.repo / "calculator.py").read_text())
            native.verify(identity)
            current = native.k.OpenProcess(0x1000, False, os.getpid())
            try:
                member = winprocess.W.BOOL()
                self.assertTrue(native.k.IsProcessInJob(current, native.job.handle, ctypes.byref(member)))
                self.assertFalse(member.value)
            finally:
                native.k.CloseHandle(current)
            observed.append(identity)
            return original(native, identity)
        with patch.object(winprocess.ControllerVerifier, "resume", resume):
            result = self.run_script(script({"op": "write", "path": "calculator.py", "content": "fixed\n"}))
        self.assertEqual(observed, [result["process"]["identity"]])
        self.assert_clean(result)

    def test_descendants_drained_on_normal_failure_timeout_and_cancel(self):
        for mode in ("normal", "failure", "timeout", "cancel"):
            with self.subTest(mode=mode):
                marker = self.root / "descendant.json"
                event = threading.Event()
                worker = ("import json, subprocess, sys, time\nfrom pathlib import Path\n"
                          "v=json.load(sys.stdin)\n"
                          "p=subprocess.Popen([sys.executable,'-I','-B','-c','import time; time.sleep(60)'],"
                          "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,close_fds=True)\n"
                          f"Path({str(marker)!r}).write_text(json.dumps({{'pid':p.pid}}))\n")
                if mode in ("timeout", "cancel"):
                    worker += "time.sleep(60)\n"
                else:
                    worker += ("json.dump(dict(schema_version=1,script_sha256=v['script_sha256'],status="
                               + repr("ok" if mode == "normal" else "failed") + ",actions_completed="
                               + ("1,error=None" if mode == "normal" else "0,error='fixed failure'")
                               + "),sys.stdout)\nsys.exit(" + ("0" if mode == "normal" else "10") + ")\n")
                def cancel():
                    end = time.monotonic() + 5
                    while not marker.exists() and time.monotonic() < end:
                        time.sleep(0.01)
                    event.set()
                thread = None
                if mode == "cancel":
                    thread = threading.Thread(target=cancel)
                    thread.start()
                try:
                    with self.fault_worker(worker):
                        result = self.run_script(script({"op": "emit", "text": ""}), timeout_seconds=1,
                                                 cancel_event=event if mode == "cancel" else None)
                    self.assert_clean(result)
                    self.assert_gone(json.loads(marker.read_text())["pid"])
                    self.assertEqual(result["success"], mode == "normal")
                finally:
                    if thread:
                        thread.join(timeout=6)
                    if marker.exists():
                        marker.unlink()

    def test_controller_keyboard_interrupt_cleans_up_and_propagates(self):
        original = winprocess.ControllerVerifier.wait
        raised = []
        def wait(native, timeout):
            if not raised:
                raised.append(True)
                raise KeyboardInterrupt("offline controller interruption")
            return original(native, timeout)
        with patch.object(winprocess.ControllerVerifier, "wait", wait), self.assertRaises(KeyboardInterrupt) as caught:
            self.run_script(script({"op": "sleep", "seconds": 60}))
        self.assert_clean(caught.exception.offline_evidence)

    def test_abrupt_child_death_has_no_success_receipt(self):
        with self.fault_worker("import os\nos._exit(19)\n"), self.assertRaises(offline.OfflineExecutorError) as caught:
            self.run_script(script({"op": "sleep", "seconds": 60}))
        self.assertEqual(caught.exception.evidence["observed_exit_code"], 19)
        self.assert_clean(caught.exception.evidence)

    def test_controller_death_before_resume_and_during_execution_kills_job(self):
        for phase in ("before_resume", "running"):
            with self.subTest(phase=phase):
                marker = self.root / (phase + ".json")
                runner = self.root / (phase + ".py")
                runner.write_text(
                    "import json,os,sys\nfrom pathlib import Path\n"
                    f"sys.path.insert(0,{str(Path(offline.__file__).parent)!r})\n"
                    "import offline_scripted_executor as o\nimport winprocess\n"
                    f"phase={phase!r}\n"
                    "original=winprocess.ControllerVerifier.resume\n"
                    "def resume(native,identity):\n"
                    " if phase=='running': original(native,identity)\n"
                    f" Path({str(marker)!r}).write_text(json.dumps(dict(pid=native.pid,identity=identity)))\n"
                    " os._exit(73)\n"
                    "winprocess.ControllerVerifier.resume=resume\n"
                    f"o.run_script({str(self.repo)!r},{{'schema_version':1,'actions':[{{'op':'sleep','seconds':60}}]}},"
                    "allowed_paths=['calculator.py'],timeout_seconds=60)\n", encoding="utf-8")
                process = subprocess.Popen([sys.executable, "-I", "-B", str(runner)],
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    stdout, stderr = process.communicate(timeout=15)
                    self.assertEqual(process.returncode, 73, (stdout, stderr))
                    record = json.loads(marker.read_text())
                    self.assertTrue(record["identity"]["created"].isdigit())
                    self.assert_gone(record["pid"])
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=10)

    def test_missing_or_conflicting_identity_never_resumes(self):
        original = winprocess.ControllerVerifier.launch
        for value in (None, {"created": "invalid", "executable": "foreign"}):
            with self.subTest(identity=value):
                def launch(native, *args):
                    original(native, *args)
                    native.identity = value
                with patch.object(winprocess.ControllerVerifier, "launch", launch), \
                        patch.object(winprocess.ControllerVerifier, "resume") as resume, \
                        self.assertRaises(offline.OfflineExecutorError) as caught:
                    self.run_script(script({"op": "sleep", "seconds": 60}))
                resume.assert_not_called()
                self.assert_clean(caught.exception.evidence)

    def test_missing_job_ownership_refuses_resume_and_cleans_up(self):
        original = winprocess.ControllerVerifier.verify
        calls = []
        def verify(native, identity):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("Injected missing Job membership evidence")
            return original(native, identity)
        with patch.object(winprocess.ControllerVerifier, "verify", verify), \
                patch.object(winprocess.ControllerVerifier, "resume") as resume, \
                self.assertRaises(offline.OfflineExecutorError) as caught:
            self.run_script(script({"op": "sleep", "seconds": 60}))
        resume.assert_not_called()
        self.assert_clean(caught.exception.evidence)

    def test_conflicting_native_exit_fails_closed(self):
        original = winprocess.ControllerVerifier.wait
        first = []
        def wait(native, timeout):
            if not first:
                first.append(True)
                return 0  # Contradicts the actual still-running child's later killed exit.
            return original(native, timeout)
        with patch.object(winprocess.ControllerVerifier, "wait", wait), self.assertRaises(offline.OfflineExecutorError) as caught:
            self.run_script(script({"op": "sleep", "seconds": 60}))
        self.assertFalse(caught.exception.evidence["success"])
        self.assertNotEqual(caught.exception.evidence["cleanup"], "observed_empty")
        self.assert_gone(caught.exception.evidence["process"]["pid"])

    def test_missing_retained_handle_exit_evidence_fails_closed(self):
        original = winprocess.ControllerVerifier.wait
        def wait(native, timeout):
            if timeout == 10:
                return None
            return original(native, timeout)
        with patch.object(winprocess.ControllerVerifier, "wait", wait), self.assertRaises(offline.OfflineExecutorError) as caught:
            self.run_script(script({"op": "emit", "text": ""}))
        self.assertFalse(caught.exception.evidence["success"])
        self.assertEqual(caught.exception.evidence["cleanup"], "not_observed")
        self.assert_gone(caught.exception.evidence["process"]["pid"])

    def test_deadline_expiry_before_resume_does_not_execute_actions(self):
        original = winprocess.ControllerVerifier.launch
        def launch(native, *args):
            original(native, *args)
            time.sleep(0.2)
        with patch.object(winprocess.ControllerVerifier, "launch", launch), \
                patch.object(winprocess.ControllerVerifier, "resume") as resume:
            result = self.run_script(script({"op": "write", "path": "calculator.py", "content": "never written"}),
                                     timeout_seconds=0.1)
        resume.assert_not_called()
        self.assertFalse(result["execution_started"])
        self.assertFalse(result["success"])
        self.assertEqual(result["termination_basis"], "controller_deadline")
        self.assertEqual(result["changed_paths"], [])
        self.assert_clean(result)

    def test_unconfirmed_cleanup_cannot_return_success(self):
        with patch.object(winprocess.ControllerVerifier, "drain", side_effect=RuntimeError("unconfirmed drain")), \
                self.assertRaises(offline.OfflineExecutorError) as caught:
            self.run_script(script({"op": "emit", "text": ""}))
        self.assertEqual(caught.exception.evidence["cleanup"], "not_observed")
        self.assert_gone(caught.exception.evidence["process"]["pid"])

    def test_source_or_repository_drift_before_resume_is_refused(self):
        original = winprocess.ControllerVerifier.launch
        def launch(native, *args):
            original(native, *args)
            (self.repo / "calculator.py").write_text("drift\n")
        with patch.object(winprocess.ControllerVerifier, "launch", launch), \
                patch.object(winprocess.ControllerVerifier, "resume") as resume, \
                self.assertRaises(offline.OfflineExecutorError) as caught:
            self.run_script(script({"op": "sleep", "seconds": 60}))
        resume.assert_not_called()
        self.assert_clean(caught.exception.evidence)

    def test_ambiguous_or_missing_worker_output_is_rejected(self):
        outputs = ["", "{}", '{"schema_version":1,"schema_version":1}', "{}{}", "x" * 3000,
                   '{"schema_version":1,"script_sha256":"foreign","status":"ok","actions_completed":1,"error":null}']
        for output in outputs:
            with self.subTest(output=output[:30]):
                with self.fault_worker(f"import sys\nsys.stdout.write({output!r})\n"), \
                        self.assertRaises(offline.OfflineExecutorError) as caught:
                    self.run_script(script({"op": "emit", "text": ""}))
                self.assert_clean(caught.exception.evidence)

    def test_worker_success_cannot_contradict_native_failure(self):
        worker = ("import json,sys\nv=json.load(sys.stdin)\n"
                  "json.dump(dict(schema_version=1,script_sha256=v['script_sha256'],status='ok',"
                  "actions_completed=1,error=None),sys.stdout)\nsys.exit(10)\n")
        with self.fault_worker(worker), self.assertRaises(offline.OfflineExecutorError) as caught:
            self.run_script(script({"op": "emit", "text": ""}))
        self.assert_clean(caught.exception.evidence)

    def test_claimed_edit_is_checked_against_actual_file_bytes(self):
        worker = ("import json,sys\nv=json.load(sys.stdin)\n"
                  "json.dump(dict(schema_version=1,script_sha256=v['script_sha256'],status='ok',"
                  "actions_completed=1,error=None),sys.stdout)\n")
        with self.fault_worker(worker), self.assertRaises(offline.OfflineExecutorError) as caught:
            self.run_script(script({"op": "write", "path": "calculator.py", "content": "never written"}))
        self.assertFalse(caught.exception.evidence["exact_edits_verified"])
        self.assert_clean(caught.exception.evidence)

    def test_scope_check_covers_ignored_files_and_git_metadata(self):
        for path in ("outside.txt", ".git/config"):
            with self.subTest(path=path):
                worker = f"from pathlib import Path\nPath({path!r}).write_text('outside scope')\n"
                with self.fault_worker(worker), self.assertRaises(offline.OfflineExecutorError) as caught:
                    self.run_script(script({"op": "emit", "text": ""}))
                self.assertFalse(caught.exception.evidence["scope_verified"])
                self.assertIn(path, caught.exception.evidence["changed_paths"])
                self.assert_clean(caught.exception.evidence)

    def test_output_is_bounded_sanitized_and_reports_truncation(self):
        result = self.run_script(script({"op": "emit", "text": "x" * 5000 + "\x1bλ\n"}), maximum_diagnostic_bytes=100)
        diagnostic = result["diagnostics"]
        self.assertTrue(diagnostic["truncated"])
        self.assertLessEqual(len(diagnostic["text"].encode("utf-8")), 100)
        self.assertNotIn("\x1b", diagnostic["text"])
        self.assertTrue(result["success"])
        self.assert_clean(result)

    def test_failure_diagnostic_is_sanitized_and_bounded(self):
        result = self.run_script(script({"op": "fail", "message": "λ\x1b" * 400}))
        message = result["action_result"]["error"]
        self.assertLessEqual(len(message.encode("utf-8")), 512)
        self.assertNotIn("\x1b", message)
        self.assertFalse(result["success"])
        self.assert_clean(result)

    def test_worker_rejects_malformed_duplicate_and_oversized_raw_input(self):
        worker = str(Path(offline.__file__).with_name("offline_scripted_executor_worker.py"))
        before = offline._snapshot(self.repo)
        for raw in (b"{}", b'{"schema_version":1,"schema_version":1}',
                    b"x" * (offline.MAX_INPUT_BYTES + 1)):
            process = subprocess.run([sys.executable, "-I", "-B", worker], input=raw,
                                     cwd=self.repo, capture_output=True, timeout=5)
            self.assertNotEqual(process.returncode, 0)
            self.assertEqual(process.stdout, b"")
            self.assertEqual(offline._snapshot(self.repo), before)

    def test_hard_linked_mutation_target_refused_before_launch(self):
        outside = self.root / "external.txt"
        outside.write_text("external original\n")
        target = self.repo / "calculator.py"
        target.unlink()
        os.link(outside, target)
        with patch.object(winprocess, "ControllerVerifier") as native, self.assertRaises(offline.OfflineExecutorError):
            self.run_script(script({"op": "write", "path": "calculator.py", "content": "forbidden mutation"}))
        native.assert_not_called()
        self.assertEqual(outside.read_text(), "external original\n")

    def test_source_drift_is_refused_before_resume(self):
        actual = offline._source_identity()
        changed = {**actual, "offline_scripted_executor_worker.py": "0" * 64}
        with patch.object(offline, "_source_identity", side_effect=[actual, changed]), \
                patch.object(winprocess.ControllerVerifier, "resume") as resume, \
                self.assertRaises(offline.OfflineExecutorError) as caught:
            self.run_script(script({"op": "sleep", "seconds": 60}))
        resume.assert_not_called()
        self.assert_clean(caught.exception.evidence)

    def test_post_execution_reparse_creation_fails_closed(self):
        outside = self.root / "outside"
        outside.mkdir()
        worker = ("import _winapi\n"
                  f"_winapi.CreateJunction({str(outside)!r}, 'escape')\n")
        with self.fault_worker(worker), self.assertRaises(offline.OfflineExecutorError) as caught:
            self.run_script(script({"op": "emit", "text": ""}))
        self.assertFalse(caught.exception.evidence["scope_verified"])
        self.assert_clean(caught.exception.evidence)

    def test_repository_root_replaced_by_junction_fails_closed(self):
        import _winapi
        outside = self.root / "outside"
        outside.mkdir()
        original = winprocess.ControllerVerifier.drain
        def drain(native, **kwargs):
            original(native, **kwargs)
            self.repo.rename(self.root / "original-repository")
            _winapi.CreateJunction(str(outside), str(self.repo))
        with patch.object(winprocess.ControllerVerifier, "drain", drain), \
                self.assertRaises(offline.OfflineExecutorError) as caught:
            self.run_script(script({"op": "emit", "text": ""}))
        self.assertFalse(caught.exception.evidence["scope_verified"])
        self.assert_clean(caught.exception.evidence)

    def test_launch_and_handle_close_failures_never_claim_success(self):
        with patch.object(winprocess.ControllerVerifier, "launch", side_effect=OSError("fixed launch failure")), \
                self.assertRaises(offline.OfflineExecutorError) as caught:
            self.run_script(script({"op": "emit", "text": ""}))
        self.assertFalse(caught.exception.evidence["success"])
        self.assert_clean(caught.exception.evidence)
        original = winprocess.ControllerVerifier.close
        def close(native):
            original(native)
            raise OSError("fixed close failure after closing handles")
        with patch.object(winprocess.ControllerVerifier, "close", close), self.assertRaises(offline.OfflineExecutorError) as caught:
            self.run_script(script({"op": "emit", "text": ""}))
        self.assertFalse(caught.exception.evidence["success"])
        self.assertEqual(caught.exception.evidence["cleanup"], "not_observed")
        self.assert_gone(caught.exception.evidence["process"]["pid"])

    def test_closed_protocol_rejects_wrong_types_unknown_fields_and_callbacks(self):
        bad = [None, {}, b"\x80\x04pickled data", {"schema_version": True, "actions": []}, script(),
               script({"op": "exec", "code": "raise Exception()"}),
               script({"op": "shell", "command": "python"}), script({"op": "import", "module": "os"}),
               script({"op": "write", "path": "calculator.py", "content": lambda: None}),
               script({"op": "write", "path": "calculator.py", "content": "x", "command": "python"}),
               script({"op": "sleep", "seconds": float("nan")}), script({"op": "sleep", "seconds": True}),
               script({"op": "emit", "text": "\0"}), script({"op": "emit", "text": "\ud800"})]
        with patch.object(winprocess, "ControllerVerifier") as native:
            for value in bad:
                with self.subTest(value=str(value)[:50]), self.assertRaises(offline.OfflineExecutorError):
                    self.run_script(value)
        native.assert_not_called()

    def test_oversized_actions_and_complete_input_refused_before_launch(self):
        bad = [script({"op": "emit", "text": "x" * (offline.MAX_FILE_BYTES + 1)}),
               script(*({"op": "emit", "text": ""} for _ in range(65))),
               script(*({"op": "emit", "text": "λ" * 10000} for _ in range(2)))]
        with patch.object(winprocess, "ControllerVerifier") as native:
            for value in bad:
                with self.assertRaises(offline.OfflineExecutorError):
                    self.run_script(value)
        native.assert_not_called()

    def test_invalid_traversal_ads_dos_and_protected_paths_refused(self):
        paths = ["../escape", "/absolute", "C:/escape", "a\\b", "a//b", "a/../b", "a:stream",
                 "NUL.txt", "COM1", "a.", ".git/config", ".GIT/index", "a/.git/config", "git~1/config"]
        with patch.object(winprocess, "ControllerVerifier") as native:
            for path in paths:
                with self.subTest(path=path), self.assertRaises(offline.OfflineExecutorError):
                    offline.run_script(self.repo, script({"op": "write", "path": path, "content": "x"}),
                                        allowed_paths=[path], timeout_seconds=1)
        native.assert_not_called()

    def test_forbidden_subtree_and_case_aliases_refused(self):
        for allowed, forbidden in ((["src/a.py"], ["src"]), (["calculator.py", "CALCULATOR.py"], [])):
            with self.assertRaises(offline.OfflineExecutorError):
                offline.run_script(self.repo, script({"op": "write", "path": allowed[0], "content": "x"}),
                                    allowed_paths=allowed, forbidden_paths=forbidden, timeout_seconds=1)
        with patch.object(winprocess, "ControllerVerifier") as native, self.assertRaises(offline.OfflineExecutorError):
            offline.run_script(self.repo, script({"op": "write", "path": "CALCULATOR.py", "content": "x"}),
                                allowed_paths=["CALCULATOR.py"], timeout_seconds=1)
        native.assert_not_called()

    def test_reparse_path_and_repository_ancestor_refused(self):
        import _winapi
        outside = self.root / "outside"
        outside.mkdir()
        link = self.repo / "link"
        _winapi.CreateJunction(str(outside), str(link))
        with self.assertRaises(offline.OfflineExecutorError):
            offline.run_script(self.repo, script({"op": "write", "path": "link/escape.py", "content": "x"}),
                                allowed_paths=["link/escape.py"], timeout_seconds=1)
        self.assertFalse((outside / "escape.py").exists())
        alias = self.root / "alias"
        _winapi.CreateJunction(str(self.repo), str(alias))
        with self.assertRaises(offline.OfflineExecutorError):
            offline.run_script(alias, script({"op": "emit", "text": ""}),
                                allowed_paths=["calculator.py"], timeout_seconds=1)

    def test_finite_timeout_and_bounded_diagnostic_options_required(self):
        for value in (None, True, 0, -1, float("inf"), float("nan"), 3601):
            with self.subTest(timeout=value), self.assertRaises(offline.OfflineExecutorError):
                self.run_script(script({"op": "emit", "text": ""}), timeout_seconds=value)
        for value in (True, 0, 4097):
            with self.assertRaises(offline.OfflineExecutorError):
                self.run_script(script({"op": "emit", "text": ""}), maximum_diagnostic_bytes=value)

    def test_no_persistence_resume_or_scheduler_compatible_execute_api(self):
        result = self.run_script(script({"op": "emit", "text": ""}))
        self.assertTrue(result["success"])
        self.assertFalse(hasattr(offline, "execute"))
        self.assertFalse(hasattr(offline, "resume"))
        self.assertFalse((self.repo / "events.jsonl").exists())
        self.assertEqual(result["changed_paths"], [])
        self.assert_clean(result)


if __name__ == "__main__":
    unittest.main()
