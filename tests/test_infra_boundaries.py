"""Real durable/Git/verification fixtures; scripted models and clock isolate boundaries."""
import copy
import http.client
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch, MagicMock

import critic
import durable
import harness
import manager
from test_manager import (ManagerCase, Planner, Executor, Critic, Reviewer, Clock,
                          step, ADD_OK, BASE, CHECK1, clean_critic, LOOSE)


def source():
    return {"task": "original task " * 1000,
            "step": {"id": "s1", "goal": "bounded step", "proposal_only": True},
            "criteria": [{"id": "AC1", "text": "acceptance " * 100, "status": "NOT_PROVEN", "checks": ["check-1"]}],
            "changed_paths": ["a.py", "b.py"],
            "scope": {"allowed_paths": ["a.py", "b.py"], "forbidden_paths": ["secrets/"], "violations": []},
            "verification": {"passed": True, "snapshot": "a" * 64, "reference": {"path": "verifier.json", "sha256": "b" * 64},
                             "commands": [{"argv": ["python", "-m", "pytest", "-q"], "exit_code": 0}]},
            "environment": {"ok": True, "drift": {"decision": "MATCH"}},
            "recovery": {"classification": "VERIFICATION_FAILED", "detail": "failed check " * 1000, "reference": "failure.json"},
            "trust": {"candidate_trusted": False, "checkpoint": None, "proven_checks": []},
            "diff": "diff --git a/a.py b/a.py\n" + '+"ž"\n' * 4000 +
                    "diff --git a/b.py b/b.py\n" + "-removed\n" * 4000}


class CriticPacketTests(unittest.TestCase):
    def test_oversized_packet_is_stable_bounded_and_explicitly_partial(self):
        raw = source()
        original = copy.deepcopy(raw)
        packet = critic.evidence_packet(raw)
        encoded = critic.packet_text(packet)
        self.assertLessEqual(len(encoded), 6000)
        self.assertEqual(encoded, critic.packet_text(critic.evidence_packet(raw)))
        self.assertEqual(raw, original)
        self.assertTrue(packet["truncated"])
        self.assertEqual(packet["source"], "controller_evidence")
        for key in ("changed_paths", "scope", "verification", "environment", "trust"):
            self.assertEqual(packet[key], raw[key])
        self.assertEqual(packet["criteria"][0]["status"], "NOT_PROVEN")
        self.assertEqual(packet["criteria"][0]["checks"], ["check-1"])
        self.assertEqual(packet["step"]["id"], "s1")
        self.assertEqual(packet["recovery"]["classification"], "VERIFICATION_FAILED")
        self.assertEqual(len(packet["diff"]["parts"]), 2)
        self.assertEqual(packet["diff"]["chars"], len(raw["diff"]))
        for field in [packet["task"], *(p["text"] for p in packet["diff"]["parts"])]:
            self.assertGreater(field["omitted_chars"], 0)
            self.assertEqual(field["chars"], len(field["head"]) + len(field["tail"]) + field["omitted_chars"])
            self.assertEqual(len(field["sha256"]), 64)

    def test_mandatory_manifest_cannot_silently_disappear(self):
        raw = source()
        raw["changed_paths"] = [str(n) + "x" * 80 for n in range(200)]
        with self.assertRaisesRegex(critic.CriticPacketError, "Mandatory"):
            critic.evidence_packet(raw)

    def test_missing_mandatory_section_fails_closed(self):
        raw = source()
        del raw["verification"]
        with self.assertRaises(critic.CriticPacketError):
            critic.evidence_packet(raw)


class StageDeadlineTests(ManagerCase):
    def prepare(self):
        self.make(critic=True, reviewer=True, review_policy="three-model",
                  budgets={**LOOSE, "max_rounds": 1, "round_timeout_seconds": 100,
                           "max_runtime_seconds": 500})
        self.clock = Clock()

    def run_models(self, critic_=None, reviewer=None, executor=None, planner=None):
        return self.run_loop(planner or Planner([step()]), executor or Executor([ADD_OK]),
                             critic_ or Critic([clean_critic()]), reviewer or Reviewer([(True, "ok")]),
                             clock=self.clock)

    def assert_timeout(self, stage):
        state = self.state()
        record = state["manager"]["rounds"][0]
        self.assertFalse(record["trusted"])
        self.assertIsNone(state["last_verified_checkpoint"])
        self.assertEqual(record["reason"], "ROUND_TIMEOUT")
        failure = self.reload().read_evidence(state["manager"]["last_failure"]["reference"])
        self.assertIn("stage=" + stage, failure["detail"])
        self.assertEqual(record["stages"][-1]["outcome"], "timeout")
        self.assertEqual(record["stages"][-1]["stage"], stage)
        self.assertIsNone(self.gateway.loaded)
        self.assertEqual(state["manager"]["stop"]["cleanup"], "passed")
        self.assertIn("unload", self.gateway.events)

    def test_required_stages_overrun_fail_closed_with_stage_evidence_and_cleanup(self):
        for name in ("planning_executor", "execution", "critic", "reviewer"):
            with self.subTest(stage=name):
                if hasattr(self, "store"):
                    # A separate durable run, same untouched fixture until execution.
                    (self.path / "calc.py").write_text(BASE)
                    self.git("add", ".")
                    self.git("commit", "--allow-empty", "-qm", "fresh fixture")
                self.make(run_id=name, critic=True, reviewer=True, review_policy="three-model",
                          budgets={**LOOSE, "max_rounds": 1, "round_timeout_seconds": 100})
                self.clock = Clock()
                method = {"planning_executor": (Planner, "plan"), "execution": (Executor, "execute"),
                          "critic": (Critic, "critique"), "reviewer": (Reviewer, "review")}[name]
                cls, attribute = method
                original = getattr(cls, attribute)
                def overrun(agent, *args):
                    result = original(agent, *args)
                    self.clock.now = 101
                    return result
                with patch.object(cls, attribute, overrun):
                    self.run_models()
                self.assert_timeout(name)

    def execute_hook(self, finish):
        original = durable.DurableRepository.execute
        calls = []
        def execute(repo, argv, timeout=None):
            if argv == CHECK1:
                calls.append(timeout)
            result = original(repo, argv, timeout)
            if argv == CHECK1 and len(calls) == 2:
                self.clock.now = finish
            return result
        return execute, calls

    def test_final_verification_overrun_never_checkpoints(self):
        self.prepare()
        execute, calls = self.execute_hook(101)
        with patch.object(durable.DurableRepository, "execute", execute):
            self.run_models()
        self.assert_timeout("final_verification")
        self.assertEqual(len(calls), 2)
        self.assertLessEqual(max(calls), 100)

    def bookkeeping(self, finish):
        original = manager.ManagerLoop.evidence
        count = []
        def evidence(loop, kind, value):
            result = original(loop, kind, value)
            if kind == "verifier":
                count.append(1)
                if len(count) == 2:
                    self.clock.now = finish
            return result
        return evidence

    def test_valid_trust_stages_bookkeeping_crosses_round_boundary(self):
        self.prepare()
        execute, _ = self.execute_hook(95)
        with patch.object(durable.DurableRepository, "execute", execute), \
                patch.object(manager.ManagerLoop, "evidence", self.bookkeeping(101)):
            self.run_models()
        state = self.state()
        self.assertEqual(state["status"], "COMPLETED")
        self.assertIsNotNone(state["last_verified_checkpoint"])
        stages = state["manager"]["rounds"][0]["stages"]
        self.assertEqual(stages[-1]["stage"], "final_verification")
        self.assertEqual(stages[-1]["outcome"], "completed")
        self.assertEqual(stages[-1]["elapsed_seconds"], 95)

    def test_finalization_reserve_and_total_runtime_remain_bounded(self):
        for finish, reason in ((126, "FINALIZATION_TIMEOUT"), (501, "max_total_runtime")):
            with self.subTest(finish=finish):
                self.make(run_id=str(finish), critic=True, reviewer=True, review_policy="three-model",
                          budgets={**LOOSE, "max_rounds": 1, "round_timeout_seconds": 100,
                                   "max_runtime_seconds": 500})
                self.clock = Clock()
                execute, _ = self.execute_hook(95)
                with patch.object(durable.DurableRepository, "execute", execute), \
                        patch.object(manager.ManagerLoop, "evidence", self.bookkeeping(finish)):
                    self.run_models()
                state = self.state()
                self.assertIsNone(state["last_verified_checkpoint"])
                recorded = state["manager"]["rounds"][0]["reason"] if finish == 126 else state["manager"]["stop"]["reason"]
                self.assertEqual(recorded, reason)
                self.assertEqual(state["manager"]["stop"]["cleanup"], "passed")
                (self.path / "calc.py").write_text(BASE)
                self.git("add", ".")
                self.git("commit", "--allow-empty", "-qm", "fresh fixture")

    def test_missing_required_reviewer_evidence_cannot_checkpoint(self):
        self.prepare()
        original = manager.ManagerLoop.checkpoint
        def checkpoint(loop, number, step_, policy, commands, verifications, critic_record, critic_ref,
                       reviewer_record, reviewer_ref, paths):
            return original(loop, number, step_, policy, commands, verifications, critic_record,
                            critic_ref, reviewer_record, None, paths)
        with patch.object(manager.ManagerLoop, "checkpoint", checkpoint):
            self.run_models()
        self.assertIsNone(self.state()["last_verified_checkpoint"])
        self.assertEqual(self.rounds()[0]["reason"], "GATES_REFUSED")

    def test_real_adapter_uses_durable_packet_without_executor_summary(self):
        config = {**self.config(), "max_file_bytes": 20000}
        with patch.object(self, "config", return_value=config):
            self.prepare()
        captured = []
        result = clean_critic()["result"]
        def chat(role, messages, phase, max_tokens=1):
            captured.append(messages[1]["content"])
            return json.dumps(result)
        self.gateway.chat = chat
        def agents(repo, gateway, config, log):
            return self.agents(Planner([step()]), Executor([ADD_OK + "# evidence line\n" * 1100]),
                               manager.ModelCritic(harness.Workflow(repo, gateway, config, log)), Reviewer([(True, "ok")]))
        manager.execute(self.store, agents=agents, gateway=self.gateway, clock=self.clock)
        self.assertEqual(self.state()["status"], "COMPLETED")
        self.assertEqual(len(captured), 1)
        self.assertLessEqual(len(captured[0]), 6000)
        self.assertNotIn("SECRET_SUMMARY", captured[0])
        packet = json.loads(captured[0])
        self.assertTrue(packet["verification"]["passed"])
        self.assertEqual(packet["changed_paths"], ["calc.py"])
        self.assertTrue(packet["truncated"])
        record = self.rounds()[0]
        evidence = self.reload().read_evidence(record["evidence"]["critic"])
        self.assertEqual(evidence["input_chars"], len(captured[0]))
        self.assertTrue(evidence["input_truncated"])
        self.assertEqual(evidence["status"], "completed")


class GatewayDeadlineTests(unittest.TestCase):
    def test_absolute_deadline_interrupts_trickled_body(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "100")
                self.end_headers()
                try:
                    for _ in range(100):
                        self.wfile.write(b" ")
                        self.wfile.flush()
                        time.sleep(0.02)
                except OSError:
                    pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        gateway = harness.Gateway({"base_url": f"http://127.0.0.1:{server.server_port}"}, None)
        start = time.monotonic()
        gateway.set_deadline(start + 0.2)
        with self.assertRaises((TimeoutError, OSError, http.client.IncompleteRead)):
            gateway.request("GET", "/", timeout=2)
        self.assertLess(time.monotonic() - start, 1.5)

    def test_expired_execution_deadline_does_not_prevent_cleanup(self):
        log = MagicMock()
        gateway = harness.Gateway({}, log)
        deadline = time.monotonic() - 1
        gateway.set_deadline(deadline)
        with patch.object(gateway, "_unload", side_effect=lambda: gateway.bounded_timeout(60)):
            self.assertEqual(gateway.unload(), 60)
        self.assertEqual(gateway.execution_deadline, deadline)
