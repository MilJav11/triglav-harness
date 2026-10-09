import json
import os
import py_compile
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from harness import EventLog, Gateway, Repository, Workflow, safe_command
from test_harness import CONFIG


class HardeningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo_dir = self.root / 'repo'
        self.repo_dir.mkdir()
        self.git('init', '-q')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@localhost',
                 'commit', '--allow-empty', '-qm', 'baseline')
        self.log = EventLog(self.root / 'events.jsonl')
        self.config = dict(CONFIG, startup_timeout_seconds=2, memory_recovery_timeout_seconds=0)
        self.repo = Repository(self.repo_dir, self.config, self.log)
        self.gateway = Gateway(self.config, self.log)
        self.process_patch = patch('harness.servers', return_value=[])
        self.process_patch.start()
        self.addCleanup(self.process_patch.stop)

    def git(self, *args):
        return subprocess.run(['git', '-c', f'safe.directory={self.repo_dir}', *args],
                              cwd=self.repo_dir, check=True, capture_output=True)

    def test_windows_aliases_and_streams_are_rejected(self):
        for name in ['.GIT/config', '.git./config', 'file:stream', 'C:relative',
                     r'\rooted', '../escape', 'NUL.txt', 'CON', 'dir /file']:
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.repo.write(name, 'bad')

    @unittest.skipUnless(os.name == 'nt', 'Windows junction regression')
    def test_junction_inside_repository_is_rejected(self):
        target = self.repo_dir / 'target'
        target.mkdir()
        junction = self.repo_dir / 'junction'
        subprocess.run(['cmd.exe', '/c', 'mklink', '/J', str(junction), str(target)],
                       check=True, capture_output=True)
        self.addCleanup(lambda: junction.rmdir())
        with self.assertRaisesRegex(ValueError, 'reparse'):
            self.repo.write('junction/file.txt', 'bad')

    def test_dirty_and_staged_work_refused_without_modification(self):
        self.repo.require_clean()
        self.repo.write('existing.txt', 'user work')
        with self.assertRaisesRegex(ValueError, 'clean'):
            self.repo.require_clean()
        self.git('add', 'existing.txt')
        with self.assertRaisesRegex(ValueError, 'clean'):
            self.repo.require_clean()
        self.assertEqual(self.repo.read('existing.txt'), 'user work')

    def test_review_includes_staged_changes_and_unicode_names(self):
        self.repo.write('price space-é.py', 'price = 42\n')
        self.assertIn('price = 42', self.repo.diff())
        self.assertIn('price space-é.py', self.repo.files())
        self.git('add', '.')
        self.assertIn('+price = 42', self.repo.diff())

    def test_large_diff_refuses_partial_review(self):
        self.repo.config = dict(self.config, max_file_bytes=100000)
        self.repo.write('big.txt', 'x' * 31000)
        with self.assertRaisesRegex(ValueError, 'complete-review'):
            self.repo.diff()

    def test_allowlist_rejects_executable_spoofing_and_git_helpers(self):
        for command in [[r'C:\evil\python.exe', '-m', 'unittest'], ['./git', 'status'],
                        ['git', 'diff', '--textconv'], ['git', 'diff', '--output=x'],
                        ['git', 'diff', '--ext-diff'], ['python', '-m', 'compileall', '..'],
                        ['python', '-m', {'bad': 'argv'}]]:
            self.assertFalse(safe_command(command), command)

    def test_orphan_process_blocks_switch_even_when_api_empty(self):
        self.gateway.running = Mock(return_value=[])
        self.gateway.request = Mock()
        self.config['shutdown_timeout_seconds'] = 0
        with patch('harness.servers', return_value=[{'pid': 999}]), patch('harness.available_ram_gb', return_value=50):
            with self.assertRaisesRegex(TimeoutError, 'unload incomplete'):
                self.gateway.switch('code')
        self.gateway.request.assert_not_called()

    def test_unload_timeout_blocks_next_start(self):
        self.gateway.running = Mock(return_value=[{'model': 'llm-review', 'state': 'stopping'}])
        self.gateway.request = Mock()
        self.config['shutdown_timeout_seconds'] = 0
        with patch('harness.available_ram_gb', return_value=50):
            with self.assertRaises(TimeoutError):
                self.gateway.switch('code')
        self.assertEqual(self.gateway.request.call_args.args[:2], ('POST', '/api/models/unload'))
        self.assertEqual(self.gateway.request.call_count, 1)

    def test_low_ram_preflight_explains_required_threshold(self):
        self.gateway.running = Mock(return_value=[])
        self.gateway.request = Mock()
        self.config['code_min_available_ram_gb'] = 16
        with patch('harness.available_ram_gb', return_value=8):
            with self.assertRaisesRegex(RuntimeError, '8.00.*16.00'):
                self.gateway.switch('code')
        self.gateway.request.assert_not_called()

    def test_switch_waits_for_ram_recovery(self):
        self.gateway.running = Mock(side_effect=[[], [{'model': 'llm-code', 'state': 'ready'}]])
        self.gateway.request = Mock()
        self.config.update(code_min_available_ram_gb=16, memory_recovery_timeout_seconds=2)
        with patch('harness.available_ram_gb', side_effect=[8, 18, 18]), patch('harness.time.sleep') as sleep:
            self.gateway.switch('code')
        sleep.assert_called_once_with(1)
        self.assertEqual(self.gateway.active_role, 'code')

    def test_ttl_expiry_rechecks_preflight(self):
        self.gateway.active_role = 'code'
        self.gateway.running = Mock(return_value=[])
        self.gateway.request = Mock()
        self.config['code_min_available_ram_gb'] = 16
        with patch('harness.available_ram_gb', return_value=8):
            with self.assertRaisesRegex(RuntimeError, 'Cannot load'):
                self.gateway.switch('code')
        self.gateway.request.assert_not_called()

    def test_startup_failure_preserves_cleanup_obligation(self):
        self.gateway.running = Mock(return_value=[])
        self.gateway.request = Mock(side_effect=RuntimeError('HTTP 500'))
        with patch('harness.available_ram_gb', return_value=50):
            with self.assertRaisesRegex(RuntimeError, 'HTTP 500'):
                self.gateway.switch('code')
        self.assertEqual(self.gateway.active_role, 'code')

    def test_profile_mismatch_refused_before_launch(self):
        self.config.update(review_profile='review-safe', review_profiles={'review-safe': {'min_available_ram_gb': 44}})
        self.gateway.running = Mock(return_value=[])
        self.gateway.request = Mock()
        with patch('pathlib.Path.read_text', return_value='{"profile":"review-performance"}'):
            with self.assertRaisesRegex(RuntimeError, 'profile mismatch'):
                self.gateway.switch('review')
        self.gateway.request.assert_not_called()

    def test_malformed_chat_response_is_explicit_failure(self):
        self.gateway.switch = Mock()
        for response in [None, {}, {'choices': []}, {'choices': [{'message': {'content': ['bad']}}]}]:
            self.gateway.request = Mock(return_value=response)
            with self.subTest(response=response), self.assertRaises(RuntimeError):
                self.gateway.chat('code', [], 'test')

    def test_malformed_review_is_not_approval(self):
        self.gateway.chat = Mock(return_value='{"approved":true}')
        with self.assertRaisesRegex(ValueError, 'Malformed review'):
            Workflow(self.repo, self.gateway, self.config, self.log).review('task', 'evidence', 'review')

    def test_jsonl_keeps_multiline_messages_in_one_record(self):
        self.log.emit('failure', message='line 1\nline 2\r\nUnicode é')
        lines = self.log.path.read_text(encoding='utf-8').splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])['message'], 'line 1\nline 2\r\nUnicode é')


    def test_unload_reset_after_server_success_is_verified(self):
        self.gateway.running = Mock(side_effect=[[{"model": "llm-review"}], []])
        self.gateway.request = Mock(side_effect=ConnectionResetError("connection reset"))
        with patch("harness.available_ram_gb", return_value=50):
            self.gateway.unload()
        self.assertIn("model_unloaded", self.log.path.read_text())

    def test_uncertain_unload_is_retried_only_if_model_remains(self):
        self.gateway.running = Mock(side_effect=[[{"model": "llm-review"}],
                                                 [{"model": "llm-review"}], []])
        self.gateway.request = Mock(side_effect=[ConnectionResetError("reset"), None])
        with patch("harness.available_ram_gb", return_value=50), patch("harness.time.sleep"):
            self.gateway.unload()
        self.assertEqual(self.gateway.request.call_count, 2)

    def test_command_timeout_terminates_spawned_child(self):
        self.repo.write("test_sleep.py", "import subprocess, sys, time\nfrom pathlib import Path\n"
                        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                        "Path('child.pid').write_text(str(child.pid))\ntime.sleep(60)\n")
        result = self.repo.execute(["python", "-m", "unittest", "test_sleep"], timeout=1)
        self.assertIsNone(result["exit_code"])
        self.assertIn("process tree terminated", result["stderr"])
        child = int((self.repo_dir / "child.pid").read_text())
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            k = ctypes.WinDLL("kernel32", use_last_error=True)
            k.OpenProcess.restype = wintypes.HANDLE
            k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            k.CloseHandle.argtypes = [wintypes.HANDLE]
            k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            handle = k.OpenProcess(0x100000, False, child)
            if handle:
                try:
                    self.assertEqual(k.WaitForSingleObject(handle, 1000), 0)
                finally:
                    k.CloseHandle(handle)


    def test_unreadable_new_file_refuses_review(self):
        (self.repo_dir / "source.py").write_bytes(bytes([255, 254, 255]))
        with self.assertRaisesRegex(ValueError, "Cannot completely review"):
            self.repo.diff()

    def test_generated_bytecode_does_not_crash_review(self):
        self.repo.write("llm_doctor.py", "value = 42\n")
        bytecode = Path(py_compile.compile(str(self.repo_dir / "llm_doctor.py"), doraise=True))
        self.assertIn("__pycache__", str(bytecode))
        self.assertIn(b"\0", bytecode.read_bytes())
        for name in ("legacy.pyc", "legacy.pyo", "extension.pyd", "UPPER.PYC"):
            (self.repo_dir / name).write_bytes(b"\xff\0")
        self.gateway.chat = Mock(return_value='{"approved":true,"findings":""}')
        approved, _ = Workflow(self.repo, self.gateway, self.config, self.log).review("task", "tests passed", "code")
        self.assertTrue(approved)
        prompt = self.gateway.chat.call_args.args[1][0]["content"]
        self.assertIn("value = 42", prompt)
        self.assertNotIn("__pycache__", prompt)
        self.assertNotIn("legacy.py", prompt)
        self.assertEqual(self.repo.files(), ["llm_doctor.py"])

    def test_tracked_and_staged_generated_artifacts_are_excluded(self):
        self.repo.write("__pycache__/tracked.pyc", "old")
        self.git("add", ".")
        self.git("-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-qm", "cache")
        (self.repo_dir / "__pycache__/tracked.pyc").write_bytes(b"\xff\0")
        (self.repo_dir / "new.pyo").write_bytes(b"\xff\0")
        self.repo.write("source.py", "value = 42\n")
        self.git("add", ".")
        diff = self.repo.diff()
        self.assertIn("+value = 42", diff)
        self.assertNotIn(".pyc", diff)
        self.assertNotIn(".pyo", diff)

    def test_binary_summary_preserves_text_and_detects_changes(self):
        self.repo.write("source.py", "value = 42\n")
        for name, data in (("opaque.bin", b"\xff\xfe\xff"), ("image.png", b"\0PNG")):
            (self.repo_dir / name).write_bytes(data)
        for staged in (False, True):
            with self.subTest(staged=staged):
                if staged:
                    self.git("add", ".")
                diff = self.repo.diff()
                self.assertIn("value = 42", diff)
                self.assertIn("Binary contents not text-reviewed: opaque.bin", diff)
                self.assertIn("sha256=", diff)
                self.assertNotIn("\ufffd", diff)
                (self.repo_dir / "opaque.bin").write_bytes(b"\xfe\xff" if staged else b"\xff\xfe")
                self.assertNotEqual(diff, self.repo.diff())

    def test_invalid_tracked_source_is_not_lossily_decoded(self):
        self.repo.write("source.py", "value = 42\n")
        self.git("add", ".")
        self.git("-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-qm", "source")
        for data in (b"value = '\xff'\n", b"value = '\0'\n"):
            with self.subTest(data=data):
                (self.repo_dir / "source.py").write_bytes(data)
                with self.assertRaisesRegex(ValueError, "Cannot completely review.*source"):
                    self.repo.diff()

    def test_unknown_non_utf8_file_is_not_silently_skipped(self):
        (self.repo_dir / "unknown.format").write_bytes(b"\xff\0")
        with self.assertRaisesRegex(ValueError, "unknown binary format"):
            self.repo.diff()

    def test_invalid_source_history_is_not_treated_as_text(self):
        source = self.repo_dir / "source.py"
        for data in (b"old\xff\n", b"old\0\n"):
            with self.subTest(data=data):
                source.write_bytes(data)
                self.git("add", ".")
                self.git("-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-qm", "old source")
                source.write_text("value = 42\n", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "Cannot completely review tracked diff"):
                    self.repo.diff()

    def test_binary_review_keeps_size_and_path_protections(self):
        (self.repo_dir / "large.bin").write_bytes(b"\xff" * 1001)
        with self.assertRaisesRegex(ValueError, "too large"):
            self.repo.diff()
        with self.assertRaises(ValueError):
            self.repo.review_file("../outside.bin")
        with self.assertRaises(ValueError):
            self.repo.review_file(".git/config")

    def test_binary_mutation_after_review_fails_final_gate(self):
        binary = self.repo_dir / "opaque.bin"
        binary.write_bytes(b"\xff\0")
        self.config["max_retries"] = 0
        self.gateway.chat = Mock(return_value="plan")
        self.gateway.unload = Mock()
        workflow = Workflow(self.repo, self.gateway, self.config, self.log)
        workflow.implement = Mock(return_value="implemented")
        workflow.review = Mock(return_value=(True, ""))
        def verify(commands):
            if workflow.verify.call_count == 2:
                binary.write_bytes(b"\xfe\0")
            return True, "passed tests"
        workflow.verify = Mock(side_effect=verify)
        result = workflow.run("task", [["python", "-m", "unittest"]])
        self.assertEqual(result["status"], "failed")
        self.assertIn("review is stale", result["verification"])

    def test_final_verifier_mutation_invalidates_review(self):
        self.config["max_retries"] = 0
        self.gateway.chat = Mock(return_value="plan")
        self.gateway.unload = Mock()
        workflow = Workflow(self.repo, self.gateway, self.config, self.log)
        workflow.implement = Mock(return_value="implemented")
        workflow.review = Mock(return_value=(True, ""))
        def verify(commands):
            if workflow.verify.call_count == 2:
                self.repo.write("unexpected.txt", "changed by final test")
            return True, "passed tests"
        workflow.verify = Mock(side_effect=verify)
        result = workflow.run("task", [["python", "-m", "unittest"]])
        self.assertEqual(result["status"], "failed")
        self.assertIn("review is stale", result["verification"])
