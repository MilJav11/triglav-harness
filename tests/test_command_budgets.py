"""Virtual long-running commands exercise deadlines without a multi-minute sleep."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import durable
import harness
import supervision_worker


class CommandBudgetTests(unittest.TestCase):
    def repository(self, cls, timeout=900):
        repo = cls.__new__(cls)
        repo.root = Path.cwd()
        repo.config = {"max_output_chars": 2000, "verification_timeout_seconds": timeout}
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        repo.log = harness.EventLog(Path(temporary.name) / "events.jsonl", config=repo.config)
        if cls is durable.DurableRepository:
            repo.store = Mock()
            repo.supervisor = Mock()
        return repo

    def test_test_tools_share_verification_budget_but_git_does_not(self):
        for argv in (["python", "-m", "pytest", "-q"], ["pytest", "-q"],
                     ["py", "-m", "unittest"], ["npm", "test"]):
            self.assertEqual(harness.command_timeout(argv, {}), 300)
            self.assertEqual(harness.command_timeout(argv, {"verification_timeout_seconds": 900}), 900)
        self.assertEqual(harness.command_timeout(["git", "status"], {}), 180)
        for timeout in (0, -1, True, 1.5, None, 86401):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                harness.verification_timeout({"verification_timeout_seconds": timeout})

    def test_executor_test_over_180_seconds_can_finish_with_a_finite_deadline(self):
        repo = self.repository(harness.Repository)
        proc = Mock(returncode=0)
        def virtual_wait(timeout):
            # A legitimate suite completes at t=450, beyond both old deadlines.
            if timeout < 450:
                raise subprocess.TimeoutExpired("pytest", timeout)
            return 0
        proc.wait.side_effect = virtual_wait
        with patch("harness.subprocess.Popen", return_value=proc):
            result = repo.execute(["python", "-m", "pytest", "-q"])
        self.assertEqual(result["exit_code"], 0)
        proc.wait.assert_called_once_with(timeout=900)

    def test_durable_executor_and_verifier_pass_the_same_pinned_deadline_to_broker(self):
        repo = self.repository(durable.DurableRepository)
        answer = {"exit_code": 0, "stdout": "passed", "stderr": "", "duration_seconds": 450}
        with patch("durable.run_command", return_value=answer.copy()) as broker:
            repo.execute(["python", "-m", "pytest", "-q"])
            self.assertEqual(broker.call_args.args[3], 900)
            workflow = harness.Workflow(repo, Mock(), repo.config, repo.log)
            self.assertTrue(workflow.verify([["python", "-m", "pytest", "-q"]])[0])
            self.assertEqual([call.args[3] for call in broker.call_args_list], [900, 900, 900])
            repo.execute(["python", "-m", "pytest", "-q"], timeout=1)
            self.assertEqual(broker.call_args.args[3], 1)

    def test_broker_enforces_configured_timeout_and_marks_forced_exit(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            (directory / "spec.json").write_text(json.dumps({
                "command": ["pytest"], "cwd": temp, "timeout": 900, "token": "token", "job": "job",
            }))
            (directory / "go.json").write_text('{"token":"token"}')
            proc = Mock()
            proc.wait.side_effect = subprocess.TimeoutExpired("pytest", 900)
            with patch.object(supervision_worker, "OwnedJob") as job, patch.object(
                    supervision_worker.subprocess, "Popen", return_value=proc):
                self.assertEqual(supervision_worker.main(directory), 0)
            proc.wait.assert_called_once_with(timeout=900)
            job.return_value.close.assert_called_once()
            receipt = json.loads((directory / "exit.json").read_text())
            self.assertEqual(receipt["termination"], "forced")
            self.assertIsNone(receipt["exit_code"])


if __name__ == "__main__":
    unittest.main()
