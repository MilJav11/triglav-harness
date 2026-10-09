import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from harness import ROOT, EventLog, Gateway, Workflow, load_config, main, model_identity
from test_harness import CONFIG


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.log = EventLog(self.root / "events.jsonl", progress=True)

    def test_progress_is_concise_and_never_prints_model_content(self):
        output = io.StringIO()
        with redirect_stderr(output):
            self.log.emit("task_start", command="run", task="SECRET prompt")
            self.log.emit("plan_start")
            self.log.emit("plan", text="SECRET reasoning")
            self.log.emit("model_selected", model="llm-code", role="code", available_ram_gb=20.25)
            self.log.emit("model_started", model="llm-code", role="code", startup_duration_seconds=3)
            for action in ("read_file", "write_file", "run_command", "list_files"):
                self.log.emit("agent_action", action=action, path="sample.py", content="SECRET file")
            self.log.emit("file_written", path="sample.py", content="SECRET file")
            self.log.emit("tool_error", action="write_file", path="sample.py", error="disk failure")
            self.log.emit("verification_start", commands=1)
            self.log.emit("command_result", argv=["python", "-m", "unittest"], exit_code=0,
                          duration_seconds=2, stdout="SECRET tool output", stderr="SECRET stderr")
            self.log.emit("test_result", passed=True)
            self.log.emit("retry", count=1)
            self.log.emit("model_unload_start", running=[{"model": "llm-code"}], available_ram_gb=12)
            self.log.emit("model_unloaded", shutdown_duration_seconds=2, available_ram_gb=40)
            self.log.emit("review_start", model="llm-review", role="review")
            self.log.emit("review_result", role="review", approved=True, findings="SECRET findings")
            self.log.emit("task_result", status="passed")
        text = output.getvalue()
        for expected in ("START", "PLAN", "ready", "read_file", "write_file", "run_command", "list_files",
                         "WRITE", "tool rejected", "VERIFY", "-> PASS", "RETRY", "unloading llm-code",
                         "free RAM 40.0 GiB", "REVIEW", "llm-review", "SUCCESS", "elapsed"):
            self.assertIn(expected, text)
        self.assertNotIn("SECRET", text)
        self.assertTrue(all(len(line) <= 260 for line in text.splitlines()))
        self.assertRegex(text, r"\[\d{2}:\d{2}:\d{2}\]")
        self.assertIn("SECRET reasoning", self.log.path.read_text())

    def test_request_telemetry_uses_only_available_response_data(self):
        gateway = Gateway(CONFIG, self.log)
        gateway.switch = Mock()
        gateway.check_active_ram = Mock()
        def response(*args, **kwargs):
            self.assertIn("waiting for response", output.getvalue())
            return {"choices": [{"message": {"content": "SECRET answer"}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                    "timings": {"predicted_per_second": 4.5, "prompt_ms": 123, "predicted_ms": 1000}}
        gateway.request = Mock(side_effect=response)
        output = io.StringIO()
        with redirect_stderr(output):
            self.assertEqual(gateway.chat("code", [], "implement"), "SECRET answer")
        text = output.getvalue()
        for expected in ("prompt=10", "completion=5", "total=15", "gen tok/s=4.5", "prompt ms=123.0"):
            self.assertIn(expected, text)
        self.assertNotIn("SECRET", text)
        events = [json.loads(line) for line in self.log.path.read_text().splitlines()]
        self.assertEqual(events[-1]["usage"]["total_tokens"], 15)
        self.assertIn("duration_seconds", events[-1])

    def test_missing_telemetry_and_control_characters(self):
        output = io.StringIO()
        with redirect_stderr(output):
            self.log.emit("model_request", model="llm-code", phase="plan", duration_seconds=4, usage=None, timings=None)
            self.log.emit("file_written", path="bad\n\x1bpath" + "x" * 1000)
        text = output.getvalue()
        self.assertEqual(len(text.splitlines()), 2)
        self.assertNotIn("\x1b", text)
        for unavailable in ("tok/s", "prompt=", "RAM", "None"):
            self.assertNotIn(unavailable, text)

    def test_progress_flushes_immediately(self):
        with patch("harness.print") as output:
            self.log.emit("task_start", command="run")
        self.assertTrue(output.call_args.kwargs["flush"])

    def test_terminal_failure_preserves_jsonl(self):
        with patch("harness.print", side_effect=BrokenPipeError):
            self.log.emit("task_start", command="run")
            self.log.emit("task_result", status="failed")
        self.assertEqual(len(self.log.path.read_text().splitlines()), 2)

    def test_cli_progress_default_and_quiet_preserve_stdout_and_evidence(self):
        for args, quiet in ((["health"], False), (["--quiet", "health"], True), (["health", "--quiet"], True)):
            with self.subTest(args=args):
                output, progress = io.StringIO(), io.StringIO()
                gateway = Mock(active_role=None)
                gateway.health.return_value = {"status": "ok"}
                gateway.running.return_value = []
                with patch("harness.ROOT", self.root), patch("harness.load_config", return_value=CONFIG), \
                     patch("harness.Gateway", return_value=gateway), redirect_stdout(output), redirect_stderr(progress):
                    self.assertEqual(main(args), 0)
                self.assertEqual(json.loads(output.getvalue()), {"health": {"status": "ok"}, "running": []})
                self.assertEqual(progress.getvalue() == "", quiet)
                if not quiet:
                    self.assertIn("SUCCESS", progress.getvalue())
                log = next((self.root / "runs").glob("*.jsonl"))
                events = [json.loads(line) for line in log.read_text().splitlines()]
                self.assertEqual(events[-1]["event"], "task_result")

    def test_cleanup_failure_cannot_print_success(self):
        gateway = Mock(active_role="code")
        gateway.health.return_value = {}
        gateway.running.return_value = []
        gateway.unload.side_effect = OSError("shutdown failed")
        output = io.StringIO()
        with patch("harness.ROOT", self.root), patch("harness.load_config", return_value=CONFIG), \
             patch("harness.Gateway", return_value=gateway), redirect_stdout(io.StringIO()), redirect_stderr(output):
            self.assertEqual(main(["health"]), 1)
        self.assertIn("FAILED", output.getvalue())
        self.assertNotIn("SUCCESS", output.getvalue())

    def test_configured_model_names_and_runtimes_cover_progress_events(self):
        config = load_config(ROOT / "config/harness.json")
        log = EventLog(self.root / "configured.jsonl", progress=True, config=config)
        for role, name, runtime in (("code", "Qwen3-Coder-30B-A3B-Instruct-Q4_K_M", "llama.cpp"),
                                    ("review", "GPT-OSS-120B-Q4_K_M", "HotPin")):
            with self.subTest(role=role):
                alias = config["roles"][role]
                output = io.StringIO()
                with redirect_stderr(output):
                    log.emit("model_selected", model=alias, role=role)
                    log.emit("model_started", model=alias, role=role, startup_duration_seconds=2)
                    log.emit("model_request_start", model=alias, role=role, phase="implement")
                    log.emit("model_request", model=alias, role=role, phase="implement", duration_seconds=3)
                    log.emit("model_unload_start", running=[{"model": alias}])
                    log.emit("model_unload_start", running=[alias])
                    log.emit("review_start", model=alias, role=role)
                    log.emit("review_result", role=role, approved=True)
                lines = output.getvalue().splitlines()
                self.assertEqual(len(lines), 8)
                for line in lines:
                    self.assertIn(f"{name} [{alias}] / {runtime}", line)
                self.assertIn("starting", lines[0])
                self.assertIn("ready", lines[1])
                self.assertIn("reviewing", lines[6])
                self.assertIn("-> PASS", lines[7])

    def test_display_metadata_does_not_change_internal_routing(self):
        config = dict(CONFIG, startup_timeout_seconds=2,
                      roles={"code": "custom-code", "review": "custom-review"},
                      model_metadata={"custom-code": {"display_name": "Different Model", "runtime": "Other Runtime"}})
        log = EventLog(self.root / "routing.jsonl", progress=True, config=config)
        gateway = Gateway(config, log)
        gateway.running = Mock(side_effect=[[], [{"model": "custom-code", "state": "ready"}],
                                            [{"model": "custom-code"}], []])
        gateway.request = Mock(side_effect=[{}, {"choices": [{"message": {"content": "answer"}}]}, None])
        messages = [{"role": "user", "content": "unchanged prompt"}]
        output = io.StringIO()
        with patch("harness.servers", return_value=[]), patch("harness.available_ram_gb", return_value=50), \
             redirect_stderr(output):
            self.assertEqual(gateway.chat("code", messages, "plan"), "answer")
            gateway.unload()
        calls = gateway.request.call_args_list
        self.assertEqual(calls[0].args, ("GET", "/upstream/custom-code/health"))
        self.assertEqual(calls[1].args[:2], ("POST", "/v1/chat/completions"))
        self.assertEqual(calls[1].args[2]["model"], "custom-code")
        self.assertEqual(calls[1].args[2]["messages"], messages)
        self.assertEqual(calls[2].args, ("POST", "/api/models/unload", {}))
        self.assertIn("Different Model [custom-code] / Other Runtime", output.getvalue())
        self.assertIn("unloading Different Model", output.getvalue())
        events = [json.loads(line) for line in log.path.read_text().splitlines()]
        self.assertEqual(next(e for e in events if e["event"] == "model_selected")["model"], "custom-code")

    def test_missing_or_invalid_display_metadata_falls_back_to_alias(self):
        for metadata in (None, {}, {"llm-code": None}, {"llm-code": {"display_name": " ", "runtime": []}}):
            with self.subTest(metadata=metadata):
                config = dict(CONFIG, model_metadata=metadata)
                log = EventLog(self.root / "fallback.jsonl", progress=True, config=config)
                self.assertEqual(log.model_label(role="code"), "llm-code")
                self.assertEqual(log.model_label("unconfigured-alias"), "unconfigured-alias")
                self.assertEqual(model_identity(config, "llm-code"),
                                 {"alias": "llm-code", "display_name": "llm-code", "runtime": None})
                output = io.StringIO()
                with redirect_stderr(output):
                    log.emit("model_selected", model="llm-code", role="code")
                self.assertIn("llm-code starting", output.getvalue())
                self.assertNotIn("None", output.getvalue())

    def test_final_metadata_identifies_actual_configured_reviewer(self):
        config = dict(CONFIG, max_retries=0, roles={"code": "custom-code", "review": "custom-review"},
                      model_metadata={"custom-review": {"display_name": "Review Model", "runtime": "Review Runtime"}})
        for reviewer, passed in ((True, True), (False, True), (True, False)):
            with self.subTest(reviewer=reviewer, passed=passed):
                gateway, repo = Mock(), Mock()
                gateway.chat.return_value = "plan"
                repo.diff.return_value = ""
                workflow = Workflow(repo, gateway, config, self.log)
                workflow.implement = Mock(return_value="done")
                workflow.verify = Mock(return_value=(passed, "evidence"))
                workflow.review = Mock(return_value=(True, ""))
                with redirect_stderr(io.StringIO()):
                    result = workflow.run("task", [["python", "-m", "unittest"]], reviewer=reviewer)
                alias = config["roles"]["review" if reviewer else "code"]
                self.assertEqual(result["reviewer_identity"], model_identity(config, alias))
                if passed:
                    self.assertEqual(result["reviewer"], alias)
                    self.assertEqual(workflow.review.call_args.args[2], "review" if reviewer else "code")
                events = [json.loads(line) for line in self.log.path.read_text().splitlines()]
                self.assertEqual(events[-1]["reviewer_identity"], result["reviewer_identity"])
