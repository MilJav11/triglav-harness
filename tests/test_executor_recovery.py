import json
import tempfile
import unittest
from pathlib import Path

from unittest.mock import Mock, patch

from harness import EventLog, Gateway, Workflow, parse_executor_action, validate_executor_action, EXECUTOR_SCHEMA


CONFIG = {
    "max_actions": 32,
    "max_output_chars": 2000,
}


class FakeRepo:
    def __init__(self):
        self.content = (
            "def slugify(text):\n"
            "    return text.lower()\n"
            "slugify = None\n"
        )

    def files(self):
        return ["textutil.py"]

    def execute(self, argv):
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    def read(self, path):
        assert path == "textutil.py"
        return self.content

    def write(self, path, content):
        assert path == "textutil.py"
        self.content = content


class LoopingGateway:
    def __init__(self):
        self.implement_calls = 0
        self.recovery_calls = 0

    def chat(self, role, messages, phase, max_tokens):
        if phase == "implement":
            self.implement_calls += 1
            return json.dumps({
                "action": "read_file",
                "path": "textutil.py",
            })

        if phase == "edit_recovery":
            self.recovery_calls += 1
            # Reproduces the real failure mode: focused recovery ignores the
            # forced write contract and asks to read the same file again.
            return json.dumps({
                "action": "read_file",
                "path": "textutil.py",
            })

        raise AssertionError(phase)


class ExecutorRecoveryRegressionTests(unittest.TestCase):
    def workflow(self, answers):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        log = EventLog(Path(temp.name) / "events.jsonl")
        gateway = Mock()
        gateway.chat.side_effect = answers
        return Workflow(FakeRepo(), gateway, CONFIG, log), gateway, log

    def test_repeated_invalid_actions_stop_after_one_recovery_call(self):
        for answer in ('{}', '{"action":null}', '{"action":"unknown"}', 'not JSON',
                       '{"action":"read_file"}', '{"action":"run_command","argv":"pytest"}',
                       '{"action":"write_file","path":"textutil.py","content":null}'):
            with self.subTest(answer=answer):
                workflow, gateway, log = self.workflow([answer] * CONFIG["max_actions"])
                with self.assertRaisesRegex(RuntimeError, "executor_stalled: repeated invalid or no-action"):
                    workflow.implement("Repair textutil.py")
                self.assertEqual(gateway.chat.call_count, 2)
                events = [json.loads(line) for line in log.path.read_text().splitlines()]
                errors = [r for r in events if r["event"] == "tool_error"]
                self.assertEqual([r["consecutive_invalid_actions"] for r in errors], [1, 2])
                self.assertTrue(all(r["error_code"] == "invalid_executor_action" for r in errors))

    def test_invalid_action_can_recover_and_success_resets_counter(self):
        workflow, gateway, _ = self.workflow([
            '{}', '{"action":"read_file","path":"textutil.py"}', '{}',
            '{"action":"write_file","path":"textutil.py","content":"fixed"}',
            '{"action":"done","summary":"fixed"}',
        ])
        self.assertEqual(workflow.implement("Repair textutil.py"), "fixed")
        self.assertEqual(workflow.repo.content, "fixed")
        self.assertEqual(gateway.chat.call_count, 5)

    def test_truncated_or_wrapped_json_cannot_execute_an_inner_object(self):
        for text in ('{"content": {"action":"done"}',
                     'prose {"action":"done"}', '{"action":"done"} trailing',
                     '{"wrapper":{"action":"done"}}'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                validate_executor_action(parse_executor_action(text))
        self.assertEqual(parse_executor_action('```json\n{"action":"done"}\n```')["action"], "done")

    def test_real_gateway_constrains_implement_and_focused_edit_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            gateway = Gateway({"roles": {"code": "llm-code"}, "request_timeout_seconds": 2},
                              EventLog(Path(directory) / "events.jsonl"))
            gateway.request = Mock(return_value={"choices": [{"message": {"content": "{}"}}]})
            with patch.object(gateway, "switch"), patch.object(gateway, "check_active_ram"):
                gateway.chat("code", [], "edit_recovery", 2400)
                recovery = gateway.request.call_args.args[2]["response_format"]
                self.assertEqual(recovery["type"], "json_schema")
                schemas = recovery["json_schema"]["schema"]["oneOf"]
                self.assertEqual([s["properties"]["action"]["const"] for s in schemas], ["write_file", "replace_text"])
                self.assertTrue(all(not s["additionalProperties"] for s in schemas))
                self.assertEqual(set(schemas[0]["required"]), {"action", "path", "content"})
                gateway.chat("code", [], "implement", 2200)
                self.assertEqual(gateway.request.call_args.args[2]["response_format"]["json_schema"]["schema"], EXECUTOR_SCHEMA)

    def test_failed_focused_recovery_cannot_spin_to_max_actions(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)

        log = EventLog(Path(temp.name) / "events.jsonl")
        repo = FakeRepo()
        gateway = LoopingGateway()
        workflow = Workflow(repo, gateway, CONFIG, log)

        with self.assertRaisesRegex(
            RuntimeError,
            "executor_stalled: repeated read after failed edit recovery",
        ):
            workflow.implement("Remove the line that shadows slugify")

        self.assertEqual(gateway.recovery_calls, 1)
        self.assertEqual(gateway.implement_calls, 3)
        self.assertLess(gateway.implement_calls, CONFIG["max_actions"])

        events = [
            json.loads(line)
            for line in log.path.read_text(encoding="utf-8").splitlines()
        ]

        codes = [
            event.get("error_code")
            for event in events
            if event["event"] == "tool_error"
        ]

        self.assertIn("focused_edit_wrong_action", codes)
        self.assertIn("repeated_read_after_failed_edit_recovery", codes)


if __name__ == "__main__":
    unittest.main()
