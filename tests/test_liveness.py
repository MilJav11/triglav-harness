"""Terminal liveness tests: manual observer ticks, no sleep or real model calls."""
import copy
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import Mock, patch

import durable
import harness as h
import manager as m
import supervision as s
from recovery import EpisodeProgress, EpisodeStop
from test_convergence import Repo, action
from test_harness import CONFIG
from test_manager import ManagerCase, Planner, RepairPlanner, Executor, Critic, Reviewer, step, ADD_OK, clean_critic


class ManualObservers:
    """Run one scheduled pulse *inside* the blocking call under test."""
    def __init__(self):
        self.now, self.events, self.threads = 0.0, [], []

    def event(self):
        event = Mock()
        event.stopped, event.remaining = False, 0
        def wait(interval):
            if event.stopped or not event.remaining:
                return True
            event.remaining -= 1
            self.now += interval
            return False
        event.wait.side_effect = wait
        event.set.side_effect = lambda: setattr(event, "stopped", True)
        self.events.append(event)
        return event

    def thread(self, target, **kwargs):
        thread = Mock()
        event = self.events[-1]
        def fire():
            event.remaining = 1
            target()
        thread.fire = fire
        self.threads.append(thread)
        return thread

    def fire(self):
        self.threads[-1].fire()


class LivenessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {**CONFIG, "heartbeat_seconds": 5, "request_timeout_seconds": 30,
                       "startup_timeout_seconds": 60}
        self.log = h.EventLog(self.root / "events.jsonl", True, self.config)
        self.output = io.StringIO()
        self.ticks = ManualObservers()
        for fixture in (patch("harness.threading.Event", self.ticks.event),
                        patch("harness.threading.Thread", self.ticks.thread),
                        patch("harness.time.monotonic", lambda: self.ticks.now),
                        redirect_stderr(self.output)):
            fixture.__enter__()
            self.addCleanup(fixture.__exit__, None, None, None)

    def gateway(self):
        gateway = h.Gateway(self.config, self.log)
        gateway.switch = Mock()
        gateway.check_active_ram = Mock()
        return gateway

    def test_model_request_pulse_during_wait_has_original_timeout_and_no_payload(self):
        gateway = self.gateway()
        self.log.set_context(round=2, step_id="repair-1")
        def request(*args, **kwargs):
            self.assertEqual(kwargs["timeout"], 30)
            self.ticks.fire()
            self.assertIn("waiting / generating still running", self.output.getvalue())
            return {"choices": [{"finish_reason": "stop", "message": {"content": "SECRET response"}}]}
        gateway.request = Mock(side_effect=request)
        self.assertEqual(gateway.chat("code", [], "implement"), "SECRET response")
        text = self.output.getvalue()
        for token in ("5s", "timeout=30s", "role=code", "round=2", "step_id=repair-1", "finish=stop"):
            self.assertIn(token, text)
        self.assertNotIn("SECRET", text)
        self.assertNotIn("operation_heartbeat", self.log.path.read_text())
        self.assertTrue(self.ticks.events[-1].stopped)
        self.ticks.threads[-1].join.assert_called_once_with(timeout=0.1)

    def test_request_timeout_is_not_extended_or_swallowed(self):
        gateway = self.gateway()
        def timeout(*args, **kwargs):
            self.ticks.fire()
            raise TimeoutError("SECRET backend body")
        gateway.request = Mock(side_effect=timeout)
        with self.assertRaises(TimeoutError):
            gateway.chat("code", [], "implement")
        self.assertEqual(gateway.request.call_args.kwargs["timeout"], 30)
        self.assertNotIn("SECRET", self.output.getvalue())
        self.assertTrue(self.ticks.events[-1].stopped)

    def test_critic_and_reviewer_waits_show_role_and_original_role_budget(self):
        self.config["roles"] = {**self.config["roles"], "critic": "llm-critic"}
        self.config["critic"] = {"request_timeout_seconds": 17}
        gateway = self.gateway()
        def request(*args, **kwargs):
            self.ticks.fire()
            return {"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]}
        gateway.request = Mock(side_effect=request)
        for role, phase, budget in (("critic", "critic", 17), ("review", "review", 30)):
            with self.subTest(role=role):
                gateway.chat(role, [], phase)
                self.assertEqual(gateway.request.call_args.kwargs["timeout"], budget)
                self.assertIn(f"timeout={budget}s | role={role}", self.output.getvalue())

    def test_command_timeout_and_cleanup_keep_original_waits_and_bounded_output(self):
        with patch("harness.subprocess.run", return_value=Mock(returncode=0, stdout=str(self.root))):
            repo = h.Repository(self.root, self.config, self.log)
        proc = Mock(pid=42, returncode=137)
        proc.poll.return_value = None
        def wait(timeout):
            if timeout == 7:
                self.ticks.fire()
                raise subprocess.TimeoutExpired("command", timeout)
        proc.wait.side_effect = wait
        def kill(*args, **kwargs):
            self.assertEqual(kwargs["timeout"], 15)
            self.ticks.fire()
            return Mock(returncode=0)
        with patch("harness.shutil.which", return_value="python"), patch("harness.subprocess.Popen", return_value=proc), \
             patch("harness.subprocess.run", side_effect=kill):
            result = repo.execute(["python", "-m", "unittest"], timeout=7)
        self.assertEqual([c.kwargs["timeout"] for c in proc.wait.call_args_list], [7, 10])
        self.assertIsNone(result["exit_code"])
        self.assertIn("process_alive=yes", self.output.getvalue())
        self.assertIn("timed-out command tree still running", self.output.getvalue())
        self.assertIn("timeout=7s", self.output.getvalue())
        self.assertIn("process tree terminated", result["stderr"])
        self.assertTrue(all(e.stopped for e in self.ticks.events))

    def test_supervised_command_shows_broker_liveness_without_changing_worker_budget(self):
        supervisor = Mock()
        supervisor.store.directory = self.root
        supervisor.register.return_value = "child"
        supervisor.child.return_value = {"token": "owned", "job": "job"}
        proc = Mock(pid=42)
        proc.poll.return_value = None
        def wait(timeout):
            self.assertEqual(timeout, 37)
            directory = self.root / "processes/child"
            spec = json.loads((directory / "spec.json").read_text())
            self.assertEqual(spec["timeout"], 7)
            self.ticks.fire()
            (directory / "exit.json").write_text(json.dumps({"token": "owned", "exit_code": 0, "termination": "natural"}))
            (directory / "stdout").write_text("SECRET" * 100)
            (directory / "stderr").write_text("")
        proc.wait.side_effect = wait
        with patch("supervision.subprocess.Popen", return_value=proc), patch("supervision.winprocess.wait_job_empty", side_effect=lambda _: self.ticks.fire()):
            result = s.run_command(supervisor, ["python"], self.root, 7, 20, progress=self.log)
        self.assertEqual(result["exit_code"], 0)
        self.assertLessEqual(len(result["stdout"]), 20)
        self.assertTrue(result["output_truncated"])
        supervisor.exited.assert_called_once_with("child", 0, "natural")
        for token in ("broker_alive=yes", "timeout=7s", "cleanup reserve=30s", "verification job drain"):
            self.assertIn(token, self.output.getvalue())
        self.assertNotIn("SECRET", self.output.getvalue())

    def test_startup_and_unload_pulse_preserve_lifecycle_and_deadlines(self):
        gateway = h.Gateway(self.config, self.log)
        gateway.running = Mock(side_effect=[[], [{"model": "llm-code", "state": "ready"}],
                                           [{"model": "llm-code", "state": "ready"}], []])
        def request(method, path, payload=None, timeout=10):
            self.ticks.fire()
            self.assertEqual(timeout, 60 if method == "GET" else self.config["shutdown_timeout_seconds"])
        gateway.request = Mock(side_effect=request)
        # The synthetic tick must fit inside the original unload deadline.
        self.config["shutdown_timeout_seconds"] = 20
        with patch("harness.servers", return_value=[]), patch("harness.available_ram_gb", return_value=50):
            gateway.switch("code")
            self.assertEqual(gateway.active_role, "code")
            gateway.unload()
        self.assertIsNone(gateway.active_role)
        self.assertIn("startup / load still running", self.output.getvalue())
        self.assertIn("model unload / process cleanup still running", self.output.getvalue())

    def test_fast_operations_zero_and_quiet_do_not_spam(self):
        for config, enabled in ((self.config, True), ({**self.config, "heartbeat_seconds": 0}, True), (self.config, False)):
            log = h.EventLog(self.root / "fast.jsonl", enabled, config)
            before = len(self.ticks.threads)
            with log.operation("REQUEST", "fast", 30):
                pass
            self.assertEqual(len(self.ticks.threads) - before, int(enabled and config["heartbeat_seconds"] != 0))
        self.assertEqual(self.output.getvalue(), "")

    def test_terminal_failure_does_not_block_request_or_observer_cleanup(self):
        gateway = self.gateway()
        def request(*args, **kwargs):
            with patch("harness.print", side_effect=BrokenPipeError):
                self.ticks.fire()
            return {"choices": [{"message": {"content": "done"}, "finish_reason": "stop"}]}
        gateway.request = request
        self.assertEqual(gateway.chat("code", [], "implement"), "done")
        self.assertFalse(self.log.progress)
        self.assertTrue(self.ticks.events[-1].stopped)
        self.ticks.threads[-1].join.assert_called_once()

    def test_reason_selection_does_not_print_arbitrary_diagnostics(self):
        self.log.emit("planner_validation", classification="missing_recovery_from", attempt=1,
                      correction_available=True, invalid_fields=["SECRET"], wire={"content": "SECRET"})
        self.log.emit("planner_validation", classification="SECRET", attempt=2, correction_available=False)
        self.log.emit("executor_tool_failure", action="replace_text", path="calc.py", classification="anchor_not_found", safe_reason="SECRET")
        self.log.emit("tool_error", action="write_file", error="SECRET payload")
        self.log.emit("manager_event", detail="SECRET reasoning")
        self.log.status("executor_stopped", reason="NO_PROGRESS", detail="SECRET detail")
        text = self.output.getvalue()
        for token in ("missing_recovery_from", "correction 1/1", "no correction remaining", "anchor_not_found", "read required", "NO_PROGRESS"):
            self.assertIn(token, text)
        self.assertNotIn("SECRET", text)

    def test_real_executor_no_progress_has_path_and_fixed_reason(self):
        answers = iter([action("write_file", path="calc.py", content=f"value = {i}\n") for i in range(1, 5)])
        gateway = Mock()
        gateway.chat.side_effect = lambda *a, **k: next(answers)
        workflow = h.Workflow(Repo(), gateway, self.config, self.log)
        with self.assertRaises(EpisodeStop):
            workflow.implement("Repair calc.py")
        self.assertIn("mutation blocked | calc.py | observation_required", self.output.getvalue())
        self.assertIn("NO_PROGRESS | calc.py | observation_required ignored", self.output.getvalue())
        self.assertEqual(gateway.chat.call_count, 4)

    def test_config_validation_and_default(self):
        self.assertEqual(h.heartbeat_interval({}), 15)
        for value in (True, -1, 0.01, 3601, float("nan"), float("inf"), "15"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                h.heartbeat_interval({"heartbeat_seconds": value})
        config_path = self.root / "config.json"
        config_path.write_text(json.dumps({**self.config, "heartbeat_seconds": -1}))
        with self.assertRaisesRegex(ValueError, "heartbeat_seconds"):
            h.load_config(config_path)


class ManagerLivenessTests(ManagerCase):
    def test_fresh_recovery_round_shows_evidence_binding(self):
        store = self.make(budgets={"max_rounds": 2})
        planner = RepairPlanner([step(), step(step_id="repair")])
        output = io.StringIO()
        with redirect_stderr(output):
            m.execute(store, agents=self.agents(planner, Executor(["def add(a,b): return 9\n", ADD_OK])),
                      gateway=self.gateway, progress=True)
        self.assertIn("round 2/2", output.getvalue())
        self.assertIn("planning evidence-bound repair", output.getvalue())
        self.assertIn("evidence-bound recovery_from round 1", output.getvalue())
        self.assertEqual(store.state["status"], "COMPLETED")

    def test_heartbeats_do_not_touch_durable_trust_counters_or_episode_state(self):
        store = self.make()
        log = durable.DurableLog(store, True)
        episode = EpisodeProgress()
        episode.unobserved["calc.py"] = 2
        episode.failure_counts[("replace_text", "calc.py", "anchor_not_found", "sha")] = 1
        before = copy.deepcopy((store.state, store.records, episode.__dict__))
        state_bytes = (store.directory / "state.json").read_bytes()
        ticks = ManualObservers()
        with redirect_stderr(io.StringIO()), patch("harness.threading.Event", ticks.event), \
             patch("harness.threading.Thread", ticks.thread), patch("harness.time.monotonic", lambda: ticks.now):
            with log.operation("COMMAND", "verification", 300):
                ticks.fire()
        self.assertEqual((store.state, store.records, episode.__dict__), before)
        self.assertEqual((store.directory / "state.json").read_bytes(), state_bytes)
        self.assertIsNone(store.state["last_verified_checkpoint"])

    def test_three_model_phases_check_ids_and_checkpoint_are_visible_in_order(self):
        store = self.make(critic=True, reviewer=True, review_policy="three-model", budgets={"max_rounds": 1})
        agents = self.agents(Planner([step()]), Executor([ADD_OK]), Critic([clean_critic()]), Reviewer([(True, "")]))
        output = io.StringIO()
        with redirect_stderr(output):
            m.execute(store, agents=agents, gateway=self.gateway, progress=True)
        phases = [line for line in output.getvalue().splitlines() if "PHASE" in line]
        names = ("executor candidate", "deterministic verification", "advisory critic", "authoritative review",
                 "final deterministic verification", "checkpoint gates", "trusted checkpoint")
        positions = [next(i for i, line in enumerate(phases) if name in line) for name in names]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("round 1/1", output.getvalue())
        self.assertIn("check_id=check-1", output.getvalue())
        self.assertEqual(store.state["status"], "COMPLETED")
        self.assertEqual(len(store.state["evidence"]["verifier"]), 2)

    def test_failed_verification_prints_check_id_exit_and_never_claims_trust(self):
        store = self.make(budgets={"max_rounds": 1})
        output = io.StringIO()
        with redirect_stderr(output):
            m.execute(store, agents=self.agents(Planner([step()]), Executor(["def add(a,b): return 0\n"])),
                      gateway=self.gateway, progress=True)
        lines = [line for line in output.getvalue().splitlines() if "COMMAND" in line and "FAIL" in line]
        self.assertTrue(any("check_id=check-1" in line and "exit=1" in line for line in lines))
        self.assertIn("VERIFICATION_FAILED", output.getvalue())
        self.assertNotIn("trusted checkpoint", output.getvalue())
        self.assertIsNone(store.state["last_verified_checkpoint"])

    def test_actual_planner_correction_prints_classification_and_accepted_step(self):
        store = self.make(budgets={"max_rounds": 1})
        planner = Planner([{}, step()])
        planner.supports_correction = True
        executor = Executor([ADD_OK])
        output = io.StringIO()
        with redirect_stderr(output):
            m.execute(store, agents=self.agents(planner, executor), gateway=self.gateway, progress=True)
        for token in ("plan rejected | missing_fields | correction 1/1", "plan accepted", "STEP", "step_id=s1"):
            self.assertIn(token, output.getvalue())
        self.assertEqual(len(planner.calls), 2)
        self.assertEqual(len(executor.calls), 1)
        self.assertEqual(store.state["status"], "COMPLETED")


if __name__ == "__main__":
    unittest.main()
