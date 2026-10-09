import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from critic import CRITIC_SCHEMA, parse_critic
from harness import ROOT, EventLog, Gateway, Workflow, load_config, main
from test_harness import CONFIG


EMPTY = {"findings": [], "uncertainties": [], "evidence_reviewed": ["supplied tests and diff"],
         "summary": "No concrete potential defects found."}
FINDING = {"severity": "blocker", "category": "boundary", "evidence": "diff uses > 18",
           "path": "sample.py", "line": 2, "symbol": "eligible", "reason": "18 must be included",
           "suggested_fix": "Use >= 18", "suggested_test": "Test age 18"}


class CriticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = dict(CONFIG, roles={**CONFIG["roles"], "critic": "llm-critic"}, max_retries=0,
                           startup_timeout_seconds=2, memory_recovery_timeout_seconds=0,
                           critic={"min_available_ram_gb": 30})
        self.log = EventLog(self.root / "events.jsonl", config=self.config)
        self.repo, self.gateway = Mock(), Mock()
        self.repo.diff.return_value = "sample diff"
        self.answer = json.dumps(EMPTY)
        self.approved = True
        self.timeline = []
        def chat(role, messages, phase, max_tokens=1300):
            self.timeline.append((role, phase))
            if phase == "plan":
                return "plan"
            if phase == "critic":
                if isinstance(self.answer, Exception):
                    raise self.answer
                return self.answer
            if phase == "review":
                return json.dumps({"approved": self.approved, "findings": "" if self.approved else "repair"})
            raise AssertionError(phase)
        self.gateway.chat.side_effect = chat
        self.gateway.unload.side_effect = lambda: self.timeline.append(("unload", None))
        self.workflow = Workflow(self.repo, self.gateway, self.config, self.log)
        self.workflow.implement = Mock(return_value="implemented")
        self.workflow.verify = Mock(return_value=(True, "tests passed"))

    def run_workflow(self, critic=True, reviewer=True):
        return self.workflow.run("acceptance criteria", [["python", "-m", "unittest"]], reviewer, critic)

    def events(self):
        return [json.loads(line) for line in self.log.path.read_text().splitlines()]

    def test_default_workflow_is_unchanged_and_never_calls_critic(self):
        for reviewer in (False, True):
            with self.subTest(reviewer=reviewer):
                result = self.run_workflow(critic=False, reviewer=reviewer)
                self.assertEqual(result["status"], "passed")
                self.assertNotIn("critic", result)
                self.assertFalse(any(call.args[0] == "critic" for call in self.gateway.chat.call_args_list))
                self.assertFalse(any(e["event"].startswith("critic_") for e in self.events()))

    def test_critic_requires_explicit_senior_reviewer(self):
        with self.assertRaisesRegex(ValueError, "requires --reviewer"):
            self.run_workflow(reviewer=False)
        self.gateway.chat.assert_not_called()

    def test_missing_critic_role_fails_before_coding(self):
        del self.config["roles"]["critic"]
        with self.assertRaisesRegex(ValueError, "No critic role"):
            self.run_workflow()
        self.gateway.chat.assert_not_called()

    def test_findings_are_forwarded_but_cannot_fail_workflow(self):
        self.answer = json.dumps(dict(EMPTY, findings=[FINDING]))
        result = self.run_workflow()
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["critic"]["result"]["findings"], [FINDING])
        prompt = next(c.args[1][0]["content"] for c in self.gateway.chat.call_args_list if c.args[2] == "review")
        for expected in ("acceptance criteria", "sample diff", "tests passed", "boundary", "Use >= 18",
                         "senior reviewer", "do not prove correctness", '"advisory_only": true'):
            self.assertIn(expected, prompt)
        self.assertIn("critic_forwarded", [e["event"] for e in self.events()])

    def test_no_findings_cannot_override_senior_rejection(self):
        self.approved = False
        result = self.run_workflow()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["critic"]["status"], "completed")

    def test_critic_cannot_override_failed_verification(self):
        self.workflow.verify.return_value = (False, "tests failed")
        result = self.run_workflow()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["critic"]["status"], "not_run")
        self.assertNotIn(("critic", "critic"), self.timeline)

    def test_final_verification_still_required(self):
        self.workflow.verify.side_effect = [(True, "passed"), (False, "failed")]
        self.assertEqual(self.run_workflow()["status"], "failed")

    def test_malformed_output_is_recorded_without_raw_content(self):
        self.answer = '<think>HIDDEN</think>{"approved":true}'
        result = self.run_workflow()
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["critic"]["status"], "malformed")
        self.assertNotIn("HIDDEN", self.log.path.read_text())
        prompt = self.gateway.chat.call_args.args[1][0]["content"]
        self.assertIn('"status": "malformed"', prompt)
        self.assertIn("evidence is unavailable", prompt)

    def test_model_failure_is_advisory_and_unloads(self):
        self.answer = RuntimeError("HTTP body HIDDEN")
        result = self.run_workflow()
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["critic"]["status"], "failed")
        self.assertNotIn("HIDDEN", self.log.path.read_text())
        self.assertIn('"status": "failed"', self.gateway.chat.call_args.args[1][0]["content"])
        index = self.timeline.index(("critic", "critic"))
        self.assertEqual(self.timeline[index + 1:index + 3], [("unload", None), ("review", "review")])

    def test_invalid_unicode_is_malformed_instead_of_crashing_jsonl(self):
        self.answer = json.dumps(dict(EMPTY, summary="\ud800"))
        result = self.run_workflow()
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["critic"]["status"], "malformed")
        self.assertNotIn("\\ud800", self.log.path.read_text())

    def test_critic_unloads_before_senior_review(self):
        self.run_workflow()
        index = self.timeline.index(("critic", "critic"))
        self.assertEqual(self.timeline[index + 1:index + 3], [("unload", None), ("review", "review")])

    def test_cleanup_failure_blocks_senior_start(self):
        self.gateway.unload.side_effect = TimeoutError("unload incomplete")
        with self.assertRaises(TimeoutError):
            self.run_workflow()
        self.assertNotIn(("review", "review"), self.timeline)
        self.assertIn("critic_cleanup_failed", [e["event"] for e in self.events()])

    def test_large_input_is_unavailable_without_truncation_or_request(self):
        self.repo.diff.return_value = "x" * 6001
        result = self.run_workflow()
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["critic"]["status"], "unavailable")
        self.assertNotIn(("critic", "critic"), self.timeline)
        self.assertIn("input_too_large", self.gateway.chat.call_args.args[1][0]["content"])

    def test_repair_invalidates_previous_critic_evidence(self):
        self.config["max_retries"] = 1
        def implement(*args):
            index = self.workflow.implement.call_count
            self.repo.diff.return_value = f"diff revision {index}"
            self.approved = index == 2
            self.answer = json.dumps(dict(EMPTY, summary=f"critic revision {index}"))
            return "done"
        self.workflow.implement.side_effect = implement
        result = self.run_workflow()
        self.assertEqual(result["status"], "passed")
        records = [e for e in self.events() if e["event"] == "critic_result"]
        self.assertEqual(len(records), 2)
        self.assertNotEqual(records[0]["input_sha256"], records[1]["input_sha256"])
        prompts = [c.args[1][0]["content"] for c in self.gateway.chat.call_args_list if c.args[2] == "review"]
        self.assertIn("critic revision 2", prompts[1])
        self.assertNotIn("critic revision 1", prompts[1])
        self.assertIn("critic_invalidated", [e["event"] for e in self.events()])

    def test_changed_verification_evidence_is_reviewed_again(self):
        self.config["max_retries"] = 1
        self.workflow.verify.side_effect = [(True, "old evidence"), (True, "new evidence"), (True, "final")]
        def implement(*args):
            self.approved = self.workflow.implement.call_count == 2
            return "done"
        self.workflow.implement.side_effect = implement
        self.run_workflow()
        records = [e for e in self.events() if e["event"] == "critic_result"]
        self.assertNotEqual(records[0]["input_sha256"], records[1]["input_sha256"])

    def test_failed_repair_verification_marks_previous_critic_stale(self):
        self.config["max_retries"] = 1
        self.approved = False
        self.workflow.verify.side_effect = [(True, "old evidence"), (False, "repair failed")]
        result = self.run_workflow()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["critic"]["status"], "stale")
        self.assertEqual(self.timeline.count(("critic", "critic")), 1)

    def test_mutation_during_critic_blocks_stale_senior_review(self):
        self.gateway.unload.side_effect = lambda: setattr(self.repo.diff, "return_value", "external mutation")
        result = self.run_workflow()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["critic"]["status"], "stale")
        self.assertNotIn(("review", "review"), self.timeline)

    def test_final_mutation_invalidates_critic_and_senior(self):
        def verify(*args):
            if self.workflow.verify.call_count == 2:
                self.repo.diff.return_value = "changed by verification"
            return True, "passed"
        self.workflow.verify.side_effect = verify
        result = self.run_workflow()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["critic"]["status"], "stale")

    def test_contract_rejects_verdicts_unknown_fields_and_invalid_types(self):
        variants = [dict(EMPTY, approved=True), dict(EMPTY, status="passed"), dict(EMPTY, reasoning_content="HIDDEN"),
                    dict(EMPTY, findings=[dict(FINDING, severity="critical")]), dict(EMPTY, findings=[FINDING] * 5),
                    dict(EMPTY, findings=[dict(FINDING, line=True)]), dict(EMPTY, summary="x" * 401),
                    dict(EMPTY, findings=[dict(FINDING, evidence="<think>HIDDEN</think>")]),
                    dict(EMPTY, findings=[dict(FINDING, line=0)]), [], None]
        for value in variants:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_critic(json.dumps(value))
        for text in ('```json\n' + json.dumps(EMPTY) + '\n```', '{"summary":"a","summary":"b"}',
                     'x' * 10001, '[' * 2000):
            with self.assertRaises(ValueError):
                parse_critic(text)

    def test_bounded_valid_result_logs_only_contract_fields(self):
        finding = {key: ("x" * 240 if isinstance(value, str) and key != "severity" else value)
                   for key, value in FINDING.items()}
        self.answer = json.dumps(dict(EMPTY, findings=[finding] * 4, summary="x" * 400,
                                      uncertainties=["x" * 240] * 4, evidence_reviewed=["x" * 240] * 4))
        self.run_workflow()
        record = next(e for e in self.events() if e["event"] == "critic_result")
        self.assertLess(len(json.dumps(record)), 11000)
        self.assertEqual(record["finding_count"], 4)

    def test_real_configured_progress_and_metadata_fallback(self):
        real = load_config(ROOT / "config/harness.json")
        for metadata, expected in ((real["model_metadata"], "Nemotron-3.5-Lightning-30B-A3B-Q4_K_M [llm-critic] / llama.cpp"),
                                   ({}, "llm-critic"), (None, "llm-critic")):
            log = EventLog(self.root / "progress.jsonl", True, dict(self.config, model_metadata=metadata))
            output = io.StringIO()
            with redirect_stderr(output):
                log.emit("model_selected", model="llm-critic")
                log.emit("critic_start", model="llm-critic")
                log.emit("critic_result", model="llm-critic", finding_count=2, result="HIDDEN")
                log.emit("model_unload_start", running=[{"model": "llm-critic"}])
            self.assertEqual(output.getvalue().count(expected), 4)
            self.assertIn("findings=2", output.getvalue())
            self.assertNotIn("HIDDEN", output.getvalue())

    def test_gateway_schema_and_hidden_reasoning_not_logged(self):
        gateway = Gateway(self.config, self.log)
        gateway.switch = Mock()
        gateway.check_active_ram = Mock()
        gateway.request = Mock(return_value={"choices": [{"finish_reason": "stop", "message": {
            "content": self.answer, "reasoning_content": "HIDDEN"}}],
            "usage": {"prompt_tokens": 12, "extra": "HIDDEN"}, "timings": {"reasoning": "HIDDEN"}})
        self.assertEqual(gateway.chat("critic", [], "critic", 1536), self.answer)
        payload = gateway.request.call_args.args[2]
        self.assertEqual(payload["response_format"]["json_schema"]["schema"], CRITIC_SCHEMA)
        self.assertEqual(payload["model"], "llm-critic")
        self.assertEqual(payload["max_tokens"], 1536)
        self.assertEqual(gateway.request.call_args.kwargs["timeout"], 180)
        self.assertNotIn("HIDDEN", self.log.path.read_text())

    def test_gateway_rejects_truncated_critic_even_when_json_valid(self):
        gateway = Gateway(self.config, self.log)
        gateway.switch = Mock()
        gateway.request = Mock(return_value={"choices": [{"finish_reason": "length", "message": {"content": self.answer}}]})
        with self.assertRaisesRegex(RuntimeError, "Incomplete critic"):
            gateway.chat("critic", [], "critic")

    def test_gateway_failure_does_not_log_http_response_body(self):
        gateway = Gateway(self.config, self.log)
        gateway.switch = Mock()
        gateway.request = Mock(side_effect=RuntimeError("HIDDEN"))
        with self.assertRaises(RuntimeError):
            gateway.chat("critic", [], "critic")
        self.assertNotIn("HIDDEN", self.log.path.read_text())

    def test_gateway_one_model_at_a_time_for_all_three_roles(self):
        gateway = Gateway(self.config, self.log)
        resident, calls = [], []
        gateway.running = lambda: [{"model": name, "state": "ready"} for name in resident]
        def request(method, path, payload=None, **kwargs):
            calls.append(path)
            if path == "/api/models/unload":
                resident.clear()
            elif method == "GET":
                self.assertEqual(resident, [])
                resident.append(path.split("/")[2])
            else:
                return {"choices": [{"finish_reason": "stop", "message": {"content": self.answer}}]}
        gateway.request = request
        with patch("harness.servers", side_effect=lambda: [{"pid": 123}] if resident else []), \
             patch("harness.available_ram_gb", return_value=50):
            for role in ("code", "critic", "review"):
                gateway.chat(role, [], role)
            gateway.unload()
        self.assertEqual(resident, [])
        self.assertEqual(calls, ["/upstream/llm-code/health", "/v1/chat/completions", "/api/models/unload",
                                 "/upstream/llm-critic/health", "/v1/chat/completions", "/api/models/unload",
                                 "/upstream/llm-review/health", "/v1/chat/completions", "/api/models/unload"])

    def test_orphan_or_failed_unload_blocks_critic_start(self):
        for resident in ([], [{"model": "llm-code"}]):
            gateway = Gateway(dict(self.config, shutdown_timeout_seconds=0), self.log)
            gateway.running = Mock(return_value=resident)
            gateway.request = Mock()
            with patch("harness.servers", return_value=[{"pid": 123}]), patch("harness.available_ram_gb", return_value=50):
                with self.assertRaises(TimeoutError):
                    gateway.switch("critic")
            self.assertFalse(any(c.args[0] == "GET" for c in gateway.request.call_args_list))

    def test_critic_ram_guard_applies_and_waits_for_recovery(self):
        gateway = Gateway(self.config, self.log)
        gateway.running = Mock(return_value=[])
        gateway.request = Mock()
        with patch("harness.servers", return_value=[]), patch("harness.available_ram_gb", return_value=29):
            with self.assertRaisesRegex(RuntimeError, "required 30"):
                gateway.switch("critic")
        gateway.request.assert_not_called()
        self.config["memory_recovery_timeout_seconds"] = 2
        gateway.running.side_effect = [[], [{"model": "llm-critic", "state": "ready"}]]
        with patch("harness.servers", return_value=[]), patch("harness.time.sleep") as sleep, \
             patch("harness.available_ram_gb", side_effect=[29, 31, 31]):
            gateway.switch("critic")
        sleep.assert_called_once_with(1)

    def test_custom_critic_alias_routes_generically(self):
        self.config["roles"]["critic"] = "replacement-model"
        result = self.run_workflow()
        self.assertEqual(result["critic"]["identity"]["alias"], "replacement-model")
        self.assertEqual(result["critic"]["identity"]["display_name"], "replacement-model")

    def test_cli_explicit_opt_in_and_rejects_nonreviewer_use(self):
        for args in (["--critic", "health"], ["--critic", "run", "--repo", ".", "--task", "x", "--verify", "git diff --check"]):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                main(args)
            self.assertEqual(error.exception.code, 2)
        for enabled in (False, True):
            gateway = Mock(active_role=None)
            workflow = Mock()
            workflow.run.return_value = {"status": "passed", "diff": ""}
            with patch("harness.ROOT", self.root), patch("harness.load_config", return_value=self.config), \
                 patch("harness.Repository"), patch("harness.Gateway", return_value=gateway), \
                 patch("harness.Workflow", return_value=workflow), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(main((["--critic"] if enabled else []) + ["run", "--repo", ".", "--task", "x",
                                      "--verify", "python -m unittest", "--reviewer"]), 0)
            self.assertEqual(workflow.run.call_args.kwargs["critic"], enabled)
