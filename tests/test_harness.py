import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from harness import EventLog, Gateway, Repository, Workflow, load_config, parse_object, safe_command


CONFIG = {
    "base_url": "http://127.0.0.1:9292", "roles": {"code": "llm-code", "review": "llm-review"},
    "shutdown_timeout_seconds": 2, "request_timeout_seconds": 2, "min_available_ram_gb": 0,
    "max_actions": 4, "max_retries": 1, "max_file_bytes": 1000, "max_output_chars": 2000,
}


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        subprocess.run(["git", "init", "-q", str(self.path)], check=True)
        subprocess.run(["git", "-C", str(self.path), "-c", f"safe.directory={self.path}",
                        "-c", "user.name=Test", "-c", "user.email=test@localhost",
                        "commit", "--allow-empty", "-qm", "baseline"], check=True)
        process_patch = patch("harness.servers", return_value=[])
        process_patch.start()
        self.addCleanup(process_patch.stop)
        (self.path / ".git/info/exclude").write_text("__pycache__/\n")
        self.log = EventLog(self.path / ".git" / "events.jsonl")
        self.repo = Repository(self.path, CONFIG, self.log)

    def test_repository_boundary_and_atomic_write(self):
        self.repo.write("src/example.py", "x = 1\n")
        self.assertEqual(self.repo.read("src/example.py"), "x = 1\n")
        with self.assertRaises(ValueError):
            self.repo.write("../escape.txt", "bad")
        with self.assertRaises(ValueError):
            self.repo.write(".git/config", "bad")
        self.assertIn("src/example.py", self.repo.files())

    def test_config_requires_actual_loopback_host(self):
        config_path = self.path / "bad.json"
        config_path.write_text('{"base_url":"http://127.0.0.1:9292@evil.example"}')
        with self.assertRaises(ValueError):
            load_config(config_path)
    def test_command_allowlist(self):
        self.assertTrue(safe_command(["python", "-m", "unittest"]))
        self.assertTrue(safe_command(["git", "diff", "--check"]))
        self.assertFalse(safe_command(["git", "push", "origin", "main"]))
        self.assertFalse(safe_command(["python", "-c", "print(1)"]))
        self.assertFalse(safe_command(["powershell", "Remove-Item", "x"]))

    def test_lifecycle_retries_transient_poll_reset(self):
        gateway = Gateway(CONFIG, self.log)
        gateway.request = lambda *args, **kwargs: None
        with patch.object(gateway, "running", side_effect=[
            ["llm-review"], ConnectionResetError("reset"), []]), patch(
                "harness.available_ram_gb", side_effect=[8.0, 8.7]), patch("harness.time.sleep"):
            gateway.unload()
        events = [json.loads(line)["event"] for line in self.log.path.read_text().splitlines()]
        self.assertIn("model_unload_poll_retry", events)
        self.assertIn("model_unloaded", events)
    def test_model_output_json_fence(self):
        self.assertEqual(parse_object('```json\n{"action":"done"}\n```')["action"], "done")

    def test_lifecycle_waits_for_unload_and_ram(self):
        gateway = Gateway(CONFIG, self.log)
        observations = iter([["llm-code"], ["llm-code"], []])
        gateway.running = lambda: next(observations)
        gateway.request = lambda *args, **kwargs: None
        with patch("harness.available_ram_gb", side_effect=[8.0, 8.7]), patch("harness.time.sleep"):
            gateway.unload()
        events = [json.loads(line)["event"] for line in self.log.path.read_text().splitlines()]
        self.assertIn("model_unloaded", events)

    def test_repeated_read_uses_focused_edit(self):
        self.repo.write("calculator.py", "def multiply(a, b):\n    return 0\n")
        self.repo.write("test_multiply.py", "import unittest\nfrom calculator import multiply\nclass T(unittest.TestCase):\n    def test_m(self): self.assertEqual(multiply(2, 3), 6)\n")

        class FakeGateway:
            def __init__(self):
                self.calls = 0

            def chat(self, role, messages, phase, max_tokens=1300):
                if phase == "plan":
                    return "Implement multiply"
                if phase == "review":
                    return '{"approved":true,"findings":""}'
                if phase == "edit_recovery":
                    return json.dumps({"action": "write_file", "path": "calculator.py",
                                       "content": "def multiply(a, b): \t\n    return a * b\t \n \t\n"})
                self.calls += 1
                return '{"action":"read_file","path":"calculator.py"}'

            def unload(self):
                return None

        result = Workflow(self.repo, FakeGateway(), CONFIG, self.log).run(
            "Implement multiply", [["python", "-m", "unittest", "discover"]])
        self.assertEqual(result["status"], "passed", result["verification"])
        self.assertEqual(self.repo.read("calculator.py"), "def multiply(a, b):\n    return a * b\n\n")
    def test_verify_fails_on_test_failure(self):
        workflow = Workflow(self.repo, Gateway(CONFIG, self.log), CONFIG, self.log)
        passed, evidence = workflow.verify([["python", "-m", "unittest", "missing_test_module"]])
        self.assertFalse(passed)
        self.assertIn("missing_test_module", evidence)

    def test_workflow_repairs_after_failing_verification(self):
        class FakeGateway:
            def __init__(self):
                self.actions = iter([
                    '{"action":"write_file","path":"test_sample.py","content":"import unittest\\nclass T(unittest.TestCase):\\n def test_value(self): self.assertEqual(1, 2)\\n"}',
                    '{"action":"done","summary":"first attempt"}',
                    '{"action":"read_file","path":"test_sample.py"}',
                    '{"action":"write_file","path":"test_sample.py","content":"import unittest\\nclass T(unittest.TestCase):\\n def test_value(self): self.assertEqual(1, 1)\\n"}',
                    '{"action":"done","summary":"repaired"}',
                ])

            def unload(self):
                return None

            def chat(self, role, messages, phase, max_tokens=1300):
                if phase == "plan":
                    return "Write and test the file"
                if phase == "review":
                    return '{"approved":true,"findings":""}'
                return next(self.actions)

        workflow = Workflow(self.repo, FakeGateway(), CONFIG, self.log)
        result = workflow.run("Create a passing test", [["python", "-m", "unittest", "discover"]])
        self.assertEqual(result["status"], "passed", result["verification"])
        self.assertEqual(result["retry_count"], 1)
        self.assertIn("self.assertEqual(1, 1)", self.repo.read("test_sample.py"))

    def test_agent_write_normalizes_trailing_whitespace(self):
        for ending in ("\n", "\r\n", "\r"):
            for final_newline in (False, True):
                with self.subTest(ending=ending, final_newline=final_newline):
                    content = ending.join(["def value(): \t", "\treturn 1\t ", " \t", "# end \t"])
                    expected = ending.join(["def value():", "\treturn 1", "", "# end"])
                    if final_newline:
                        content += ending
                        expected += ending
                    gateway = Mock()
                    gateway.chat.side_effect = [
                        json.dumps({"action": "write_file", "path": "sample.py", "content": content}),
                        '{"action":"done","summary":"clean"}',
                    ]
                    result = Workflow(self.repo, gateway, CONFIG, self.log).implement("Write sample")
                    self.assertEqual(result, "clean")
                    self.assertEqual((self.path / "sample.py").read_bytes(), expected.encode("utf-8"))
        events = [json.loads(line) for line in self.log.path.read_text().splitlines()]
        self.assertEqual(sum(event["event"] == "file_written" for event in events), 6)
        self.assertFalse(any(event["event"] == "tool_error" for event in events))

    def test_agent_tool_errors_are_logged_and_write_safety_is_preserved(self):
        self.repo.write("sample.py", "original\n")
        original_git_config = (self.path / ".git/config").read_bytes()
        cases = [
            ({"action": "write_file", "path": "sample.py", "content": ["not text"]}, "must be text"),
            ({"action": "write_file", "path": "sample.py", "content": "x" * 1001 + " \t\n"}, "too large"),
            ({"action": "write_file", "path": "../escape.txt", "content": "secret contents \t\n"}, "not allowed"),
            ({"action": "write_file", "path": ".git/config", "content": "secret contents \t\n"}, "not allowed"),
            ({"action": "write_file", "content": "secret contents \t\n"}, "path"),
            ({"action": "read_file", "path": "missing.py"}, "missing"),
            ({"action": "run_command", "argv": ["git", "push"]}, "disallowed"),
        ]
        for action, error in cases:
            with self.subTest(action=action["action"], error=error):
                gateway = Mock()
                gateway.chat.side_effect = [json.dumps(action), '{"action":"done"}']
                Workflow(self.repo, gateway, CONFIG, self.log).implement("Try tool")
                events = [json.loads(line) for line in self.log.path.read_text().splitlines()]
                event = [event for event in events if event["event"] == "tool_error"][-1]
                self.assertEqual(event["action"], action["action"])
                self.assertEqual(event["path"], action.get("path"))
                self.assertIn(error, event["error"])
                self.assertNotIn("content", event)
                self.assertNotIn("secret contents", json.dumps(event))
                self.assertEqual(self.repo.read("sample.py"), "original\n")
        self.assertEqual((self.path / ".git/config").read_bytes(), original_git_config)
        self.assertEqual(sum(event["event"] == "file_written" for event in events), 1)
        self.assertEqual(sum(event["event"] == "tool_error" for event in events), len(cases))

    def test_agent_write_os_error_is_logged(self):
        gateway = Mock()
        gateway.chat.side_effect = [
            '{"action":"write_file","path":"sample.py","content":"secret contents"}',
            '{"action":"done"}',
        ]
        with patch.object(self.repo, "write", side_effect=OSError("disk failure")):
            Workflow(self.repo, gateway, CONFIG, self.log).implement("Write sample")
        events = [json.loads(line) for line in self.log.path.read_text().splitlines()]
        event = next(event for event in events if event["event"] == "tool_error")
        self.assertEqual(event["action"], "write_file")
        self.assertEqual(event["path"], "sample.py")
        self.assertEqual(event["error"], "disk failure")
        self.assertNotIn("secret contents", self.log.path.read_text())
        self.assertFalse((self.path / "sample.py").exists())

    def test_parse_error_does_not_log_previous_action_or_generated_content(self):
        gateway = Mock()
        gateway.chat.side_effect = [
            '{"action":"read_file","path":"missing.py"}',
            'invalid JSON with secret generated contents',
            '{"action":"done"}',
        ]
        with self.assertRaisesRegex(RuntimeError, "executor_stalled: repeated invalid or no-action"):
            Workflow(self.repo, gateway, CONFIG, self.log).implement("Try tools")
        self.assertEqual(gateway.chat.call_count, 2)
        events = [json.loads(line) for line in self.log.path.read_text().splitlines()]
        errors = [event for event in events if event["event"] == "tool_error"]
        self.assertEqual(len(errors), 2)
        self.assertIsNone(errors[-1]["action"])
        self.assertIsNone(errors[-1]["path"])
        self.assertEqual(errors[-1]["error"], "Expected one complete JSON action object")
        self.assertNotIn("secret generated contents", self.log.path.read_text())


if __name__ == "__main__":
    unittest.main()
