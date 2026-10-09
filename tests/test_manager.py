"""Deterministic Manager-loop tests. Git, file writes, verifiers, supervision and durable state are real;
only the model-facing planner/executor/critic/reviewer/gateway are scripted."""
import argparse
import ast
import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import durable
import environment
import harness
import manager
import supervision
from test_harness import CONFIG

TASK = "Implement add and sub"
TEST_ADD = "import unittest\nfrom calc import add\nclass T(unittest.TestCase):\n    def test_add(self): self.assertEqual(add(2, 3), 5)\n"
TEST_SUB = "import unittest\nfrom calc import sub\nclass T(unittest.TestCase):\n    def test_sub(self): self.assertEqual(sub(5, 3), 2)\n"
BASE = "def add(a, b):\n    return 0\n\ndef sub(a, b):\n    return 0\n"
ADD_OK = "def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return 0\n"
ADD_OK_DOC = 'def add(a, b):\n    """Add."""\n    return a + b\n\ndef sub(a, b):\n    return 0\n'
BOTH_OK = "def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a - b\n"
CHECK1 = ["python", "-m", "unittest", "test_add"]
CHECK2 = ["python", "-m", "unittest", "test_sub"]
TWO = [CHECK1, CHECK2]
TWO_CRITERIA = [{"id": "ADD", "text": "add works", "checks": ["check-1"]},
                {"id": "SUB", "text": "sub works", "checks": ["check-2"]}]
LOOSE = {"max_rounds": 10, "max_step_retries": 10, "max_consecutive_failed_rounds": 10, "max_stall_rounds": 10}


class SimulatedKill(BaseException):
    """Stands in for a hard process kill: no cleanup handler may commit anything."""


def hard_kill(self, exc):
    raise exc


def git_fixture(root):
    path = root / "repo"
    path.mkdir()
    (path / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    for name, text in (("calc.py", BASE), ("test_add.py", TEST_ADD), ("test_sub.py", TEST_SUB)):
        (path / name).write_text(text, encoding="utf-8")

    def git(*args):
        return subprocess.run(["git", "-C", str(path), "-c", f"safe.directory={path}", "-c", "user.name=Manager test",
                               "-c", "user.email=test@localhost", *args], check=True, capture_output=True)
    git("init", "-q")
    git("add", ".")
    git("commit", "-qm", "baseline")
    return path, git


def wrong(tag):
    return f"def add(a, b):\n    return 0  # attempt {tag}\n\ndef sub(a, b):\n    return 0\n"


def returns(n):
    return f"def add(a, b):\n    return {n}\n\ndef sub(a, b):\n    return 0\n"


def step(step_id="s1", goal=None, checks=("check-1",), paths=("calc.py",), forbidden=(), risk="low",
         reviewer=False, effort="small"):
    return {"step_id": step_id, "goal": goal or f"Implement via {step_id}", "rationale": "next bounded step",
            "scope": {"allowed_paths": list(paths), "forbidden_paths": list(forbidden)},
            "acceptance_checks": list(checks), "risk": risk, "needs_reviewer": reviewer,
            "estimated_effort": effort, "completion_signal": "the listed checks pass"}


def clean_critic():
    return {"status": "completed", "input_sha256": "a" * 64,
            "result": {"findings": [], "uncertainties": [], "evidence_reviewed": ["tests"], "summary": "No defects"}}


def finding_critic(severity):
    item = {"severity": severity, "category": "logic", "evidence": "diff", "path": "calc.py", "line": 1,
            "symbol": None, "reason": "possible defect", "suggested_fix": "check", "suggested_test": "add test"}
    value = clean_critic()
    value["result"]["findings"] = [item]
    return value


class FakeGateway:
    """Mimics the one-large-model-at-a-time switch; records every load/unload/request."""
    def __init__(self, *args):
        self.events, self.loaded, self.peak = [], None, 0
        self.active_role = None

    def unload(self):
        self.events.append("unload")
        self.loaded = None

    def chat(self, role, messages=None, phase="x", max_tokens=1):
        if self.loaded not in (None, role):
            self.events.append("switch-unload")
            self.loaded = None
        self.loaded = role
        self.peak = max(self.peak, 1)
        self.events.append(role)
        return "{}"


class Script:
    """Returns scripted results in order; an Exception/BaseException instance is raised instead."""
    def __init__(self, items, gateway=None, role=None, repeat_last=False):
        self.items, self.calls, self.gateway, self.role, self.repeat_last = list(items), [], gateway, role, repeat_last

    def next(self, *args):
        self.calls.append(args)
        if self.gateway is not None:
            self.gateway.chat(self.role)
        if not self.items:
            raise AssertionError("unexpected extra call")
        item = self.items[0] if self.repeat_last and len(self.items) == 1 else self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class Planner(Script):
    def plan(self, context, tier):
        return self.next(copy.deepcopy(context), tier)

    def tiers(self):
        return [call[1] for call in self.calls]


class RepairPlanner(Planner):
    """Script explicit fresh repair plans for tests that previously used cached retry.

    Plain Planner remains strict for blind-replay and invalid-contract tests.
    Each repair uses the supplied failure binding and a new bounded repair goal.
    """
    def __init__(self, items, *args, **kwargs):
        super().__init__(items, *args, **kwargs)
        self.last = copy.deepcopy(self.items[-1])

    def plan(self, context, tier):
        if not self.items:
            self.items.append(copy.deepcopy(self.last))
        value = super().plan(context, tier)
        if isinstance(value, dict) and context.get("recovery_evidence", {}).get("step_id"):
            value = copy.deepcopy(value)
            failure = context["recovery_evidence"]
            value["step_id"] = f"repair-{failure['round']}"
            value["goal"] = f"Repair {failure['classification']} from round {failure['round']}; inspect calc.py and rerun check-1"
            value["recovery_from"] = {"round": failure["round"], "fingerprint": failure["fingerprint"]}
            constraints = context.get("planning_constraints", {})
            floor = constraints.get("risk_floor")
            if floor and manager.RISKS.index(value["risk"]) < manager.RISKS.index(floor):
                value["risk"] = floor
            if constraints.get("reviewer_required"):
                value["needs_reviewer"] = True
        return value


class Executor(Script):
    def execute(self, step_, context, repo):
        self.calls.append((step_, copy.deepcopy(context)))
        if self.gateway is not None:
            self.gateway.chat("code")
        if not self.items:
            raise AssertionError("unexpected extra executor call")
        item = self.items[0] if self.repeat_last and len(self.items) == 1 else self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, str):
            item = {"calc.py": item}
        for name, text in item.items():
            repo.write(name, text)
        return "SECRET_SUMMARY hidden model summary"


class Direct(Executor):
    """Writes files straight to disk, bypassing the scoped repository (as a misbehaving tool or test would)."""
    def execute(self, step_, context, repo):
        self.calls.append((step_, copy.deepcopy(context)))
        item = self.items.pop(0)
        for name, text in item.items():
            target = repo.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        return "done"


class TierPlanner(Planner):
    def plan(self, context, tier):
        self.role = "review" if tier == "reviewer" else "code"
        return self.next(copy.deepcopy(context), tier)


class Critic(Script):
    def critique(self, step_, context, evidence, diff):
        return self.next(step_["step_id"])


class Reviewer(Script):
    def review(self, step_, context, evidence, diff, critic):
        return self.next(step_["step_id"], critic)


class Forbidden:
    """An agent that must never be called."""
    def __init__(self, name):
        self.name = name

    def __getattr__(self, attr):
        raise AssertionError(f"{self.name} must not be invoked")


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class ManagerCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path, self.git = git_fixture(self.root)
        self.gateway = FakeGateway()

    def config(self):
        config = copy.deepcopy(CONFIG)
        config["roles"] = {**CONFIG["roles"], "critic": "llm-critic"}
        return config

    def make(self, commands=None, criteria=None, run_id="auto-run", **kwargs):
        commands = commands or [CHECK1]
        criteria = criteria if criteria is not None else ["add works"]
        criteria = [json.dumps(c) if isinstance(c, dict) else c for c in criteria]
        kwargs["budgets"] = {**(kwargs.get("budgets") or {})}
        log = harness.EventLog(self.root / "preflight.jsonl")
        repo = harness.Repository(self.path, self.config(), log)
        store = durable.Store(self.root / "runs", run_id)
        manager.create_run(store, repo, TASK, commands, self.config(), criteria=criteria, **kwargs)
        self.store = store
        return store

    def two(self, **kwargs):
        kwargs.setdefault("budgets", LOOSE)
        return self.make(TWO, TWO_CRITERIA, **kwargs)

    def agents(self, planner=None, executor=None, critic=None, reviewer=None):
        return manager.Agents(planner or Forbidden("planner"), executor or Forbidden("executor"),
                              critic or Forbidden("critic"), reviewer or Forbidden("reviewer"))

    def run_loop(self, planner=None, executor=None, critic=None, reviewer=None, store=None, clock=None, gateway=None):
        return manager.execute(store or self.store, agents=self.agents(planner, executor, critic, reviewer),
                               gateway=gateway or self.gateway, clock=clock or time.monotonic)

    def reload(self):
        store = durable.Store(self.root / "runs", self.store.directory.name)
        store.load()
        return store

    def resume(self, planner=None, executor=None, critic=None, reviewer=None, **kwargs):
        store = self.reload()
        clock = kwargs.pop("clock", None)
        if "enable_reviewer" in kwargs:
            kwargs["reviewer"] = kwargs.pop("enable_reviewer")
        return manager.resume(store, agents=self.agents(planner, executor, critic, reviewer), gateway=self.gateway,
                              clock=clock or time.monotonic, **kwargs)

    def state(self):
        return self.reload().state

    def rounds(self):
        return self.state()["manager"]["rounds"]

    def kill(self, planner=None, executor=None, critic=None, reviewer=None):
        with patch.object(manager.ManagerLoop, "fail", hard_kill), self.assertRaises(SimulatedKill):
            self.run_loop(planner, executor, critic, reviewer)

    def dirs(self):
        return sorted(p.name for p in (self.store.directory / "checkpoints").iterdir())


class StepContractTests(unittest.TestCase):
    CHECKS = manager.build_checks(TWO)
    SCOPE = {"allowed_paths": [], "forbidden_paths": ["secrets/"]}

    def parse(self, raw, proven=(), scope=None):
        return manager.parse_step(raw, checks=self.CHECKS, proven=proven, run_scope=scope or self.SCOPE)

    def test_valid_step_is_normalized(self):
        value = self.parse(json.dumps(step(paths=["./calc.py", "src\\"])))
        self.assertEqual(value["scope"]["allowed_paths"], ["calc.py", "src/"])

    def test_json_wrapped_in_text_and_fences_is_refused(self):
        raw = "<think>hidden</think>\n```json\n" + json.dumps(step()) + "\n```"
        with self.assertRaises(manager.StepError):
            self.parse(raw)

    def test_invalid_schema_variants_are_rejected(self):
        bad = []
        bad.append({k: v for k, v in step().items() if k != "risk"})
        bad.append({**step(), "command": "rm -rf /"})
        bad.append({**step(), "risk": "extreme"})
        bad.append({**step(), "needs_reviewer": "yes"})
        bad.append({**step(), "estimated_effort": "large"})
        bad.append({**step(), "goal": ""})
        bad.append({**step(), "goal": "<think>plan</think> do it"})
        bad.append({**step(), "step_id": "bad id!"})
        bad.append({**step(), "acceptance_checks": ["check-9"]})
        bad.append({**step(), "acceptance_checks": []})
        bad.append({**step(), "acceptance_checks": ["check-1", "check-1"]})
        bad.append({**step(), "scope": {"allowed_paths": [], "forbidden_paths": []}})
        bad.append({**step(), "scope": {"allowed_paths": ["a"], "forbidden_paths": [], "shell": ["x"]}})
        for value in bad:
            with self.subTest(value=value), self.assertRaises(manager.StepError) as caught:
                self.parse(json.dumps(value))
            self.assertEqual(caught.exception.code, "INVALID_MANAGER_OUTPUT")
        for raw in ("not json", "", "[1, 2]", '{"step_id": "a", "step_id": "b"}', "x" * 20001, 5):
            with self.subTest(raw=raw), self.assertRaises(manager.StepError):
                self.parse(raw)

    def test_forbidden_and_escaping_paths_are_refused(self):
        for paths in (["secrets/key.txt"], ["secrets/"], [".git/config"], ["../outside.py"], ["/abs.py"],
                      ["C:/abs.py"], ["src/*.py"], ["calc.py", "SECRETS/x"]):
            with self.subTest(paths=paths), self.assertRaises(manager.StepError) as caught:
                self.parse(json.dumps(step(paths=paths)))
            self.assertEqual(caught.exception.code, "FORBIDDEN_PATH_PROPOSED")

    def test_step_cannot_allow_what_it_also_forbids_and_run_allowlist_is_enforced(self):
        with self.assertRaises(manager.StepError):
            self.parse(json.dumps(step(paths=["src/"], forbidden=["src/private.py"])))
        scope = {"allowed_paths": ["src/"], "forbidden_paths": []}
        self.assertEqual(self.parse(json.dumps(step(paths=["src/a.py"])), scope=scope)["scope"]["allowed_paths"], ["src/a.py"])
        with self.assertRaises(manager.StepError) as caught:
            self.parse(json.dumps(step(paths=["calc.py"])), scope=scope)
        self.assertEqual(caught.exception.code, "FORBIDDEN_PATH_PROPOSED")

    def test_step_must_add_progress(self):
        with self.assertRaisesRegex(manager.StepError, "already-proven"):
            self.parse(json.dumps(step(checks=["check-1"])), proven=["check-1"])
        self.assertEqual(self.parse(json.dumps(step(checks=["check-1", "check-2"])), proven=["check-1"])["acceptance_checks"],
                         ["check-1", "check-2"])

    def test_fingerprint_ignores_identity_and_rationale_but_not_substance(self):
        a = self.parse(json.dumps(step(step_id="a", goal="Do  the THING")))
        b = self.parse(json.dumps({**step(step_id="b", goal="do the thing"), "rationale": "different words"}))
        c = self.parse(json.dumps(step(step_id="a", goal="do the thing", paths=["other.py"])))
        self.assertEqual(manager.step_fingerprint(a), manager.step_fingerprint(b))
        self.assertNotEqual(manager.step_fingerprint(a), manager.step_fingerprint(c))


class PolicyTests(unittest.TestCase):
    def policy(self, critic_on=True, reviewer_on=True, **kwargs):
        value = manager.parse_step(json.dumps(step(**kwargs)), checks=manager.build_checks(TWO), proven=[],
                                   run_scope={"allowed_paths": [], "forbidden_paths": []})
        return manager.model_policy(value, critic_enabled=critic_on, reviewer_enabled=reviewer_on)

    def test_low_risk_uses_no_critic_and_no_reviewer(self):
        policy = self.policy(risk="low")
        self.assertEqual((policy["risk"], policy["planner"], policy["critic"], policy["reviewer"]),
                         ("low", "executor", "skip", "not_required"))

    def test_medium_risk_uses_critic_and_reviewer_only_on_escalation(self):
        policy = self.policy(risk="medium")
        self.assertEqual((policy["planner"], policy["critic"], policy["reviewer"]), ("executor", "run", "if_escalated"))
        self.assertEqual(self.policy(critic_on=False, risk="medium")["critic"], "skip")

    def test_high_risk_plans_with_reviewer_and_requires_arbitration(self):
        policy = self.policy(risk="high")
        self.assertEqual((policy["planner"], policy["critic"], policy["reviewer"]), ("reviewer", "run", "required"))
        self.assertEqual(self.policy(reviewer_on=False, risk="high")["planner"], "executor")

    def test_manager_can_raise_but_not_lower_policy(self):
        self.assertEqual(self.policy(risk="low", reviewer=True)["reviewer"], "required")  # needs_reviewer
        self.assertEqual(self.policy(risk="low", effort="medium")["risk"], "medium")
        self.assertEqual(self.policy(risk="low", paths=["config/app.json"])["risk"], "high")
        self.assertEqual(self.policy(risk="low", paths=[f"f{i}.py" for i in range(9)])["risk"], "medium")
        value = manager.parse_step(json.dumps(step(risk="low")), checks=manager.build_checks(TWO), proven=[],
                                   run_scope={"allowed_paths": [], "forbidden_paths": []})
        self.assertEqual(manager.effective_risk(value, floor="high")[0], "high")


class GateTests(unittest.TestCase):
    ALL = {name: True for name, _ in manager.GATE_NAMES}

    def test_all_true_is_trusted(self):
        self.assertEqual(manager.evaluate_gates(self.ALL), (True, []))

    def test_each_gate_independently_blocks_and_missing_facts_fail_closed(self):
        for name, message in manager.GATE_NAMES:
            with self.subTest(gate=name):
                trusted, reasons = manager.evaluate_gates({**self.ALL, name: False})
                self.assertFalse(trusted)
                self.assertEqual(reasons, [message])
                trusted, _ = manager.evaluate_gates({k: v for k, v in self.ALL.items() if k != name})
                self.assertFalse(trusted)
        self.assertFalse(manager.evaluate_gates({})[0])
        self.assertFalse(manager.evaluate_gates({**self.ALL, "reviewer_ok": "yes"})[0])

    def test_reviewer_approval_cannot_override_failed_verification(self):
        trusted, reasons = manager.evaluate_gates({**self.ALL, "reviewer_ok": True, "verification_passed": False})
        self.assertFalse(trusted)
        self.assertIn("required deterministic verification did not pass", reasons)

    def test_passing_verification_cannot_override_required_reviewer_failure(self):
        self.assertFalse(manager.evaluate_gates({**self.ALL, "verification_passed": True, "reviewer_ok": False})[0])

    def test_model_text_is_not_a_gate_input(self):
        self.assertEqual({name for name, _ in manager.GATE_NAMES} & {"summary", "critic_approved", "model_says_done"}, set())


class FingerprintTests(unittest.TestCase):
    def test_timing_and_paths_are_normalized_but_real_differences_are_not(self):
        a = manager.failure_fingerprint("VERIFICATION_FAILED", 'Ran 1 test in 0.002s\nFile "C:\\Temp\\a\\test.py", line 4\nFAILED')
        b = manager.failure_fingerprint("VERIFICATION_FAILED", 'Ran 1 test in 0.150s\nFile "C:\\Temp\\b\\test.py", line 4\nFAILED')
        c = manager.failure_fingerprint("VERIFICATION_FAILED", 'Ran 1 test in 0.150s\nFile "C:\\Temp\\b\\test.py", line 9\nFAILED')
        d = manager.failure_fingerprint("NO_DIFF", 'Ran 1 test in 0.150s\nFile "C:\\Temp\\b\\test.py", line 4\nFAILED')
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertNotEqual(b, d)

    def test_budget_validation_rejects_unsafe_values(self):
        self.assertEqual(manager.resolve_budgets()["max_rounds"], 6)
        for override in ({"max_rounds": 0}, {"max_rounds": True}, {"max_step_retries": -1}, {"max_runtime_seconds": 10**9},
                         {"max_model_invocations": {"gpu": 1}}, {"max_model_invocations": {"code": 0}}, {"bogus": 1}):
            with self.subTest(override=override), self.assertRaises(ValueError):
                manager.resolve_budgets(override)
        self.assertEqual(manager.resolve_budgets({"max_rounds": None})["max_rounds"], 6)


class LoopSuccessTests(ManagerCase):
    def test_one_round_success_with_real_evidence(self):
        self.make()
        planner, executor = Planner([step()]), Executor([ADD_OK])
        report = self.run_loop(planner, executor)
        state = self.state()
        self.assertEqual((report["status"], state["status"]), ("COMPLETED", "COMPLETED"))
        self.assertEqual([r["outcome"] for r in state["manager"]["rounds"]], ["TRUSTED"])
        self.assertEqual((len(planner.calls), len(executor.calls)), (1, 1))
        self.assertEqual(state["round_number"], 1)
        self.assertEqual(self.dirs(), ["0001.json"])
        self.assertEqual(state["remaining_work"], [])
        self.assertEqual(self.gateway.loaded, None)

    def test_verification_pass_is_recorded_as_command_evidence(self):
        self.make()
        self.run_loop(Planner([step()]), Executor([ADD_OK]))
        store = self.reload()
        checkpoint = store.read_evidence(store.state["last_verified_checkpoint"]["reference"])
        evidence = store.read_evidence(checkpoint["verification"][-1])
        self.assertTrue(evidence["passed"])
        self.assertEqual([r["argv"] for r in evidence["commands"]], [CHECK1, ["git", "diff", "--check"]])
        self.assertEqual([r["exit_code"] for r in evidence["commands"]], [0, 0])
        self.assertEqual(checkpoint["critic_status"], "not_run")
        self.assertIsNone(checkpoint["reviewer"])

    def test_multi_round_success_carries_regression_checks(self):
        self.two()
        planner = Planner([step("s1", checks=["check-1"]), step("s2", checks=["check-2"])])
        executor = Executor([ADD_OK, BOTH_OK])
        report = self.run_loop(planner, executor)
        state = self.state()
        self.assertEqual(report["status"], "COMPLETED")
        self.assertEqual(len(state["manager"]["rounds"]), 2)
        self.assertEqual(state["manager"]["proven_checks"], ["check-1", "check-2"])
        self.assertEqual(self.dirs(), ["0001.json", "0002.json"])
        store = self.reload()
        second = store.read_evidence(state["manager"]["rounds"][1]["evidence"]["checkpoint"])
        commands = [r["argv"] for r in store.read_evidence(second["verification"][-1])["commands"]]
        self.assertEqual(commands, [CHECK1, CHECK2, ["git", "diff", "--check"]])
        # Round 2's planner sees round 1 as verified, and only the remaining work.
        second_context = planner.calls[1][0]
        self.assertEqual([s["step_id"] for s in second_context["trusted_state"]["verified_steps"]], ["s1"])
        self.assertEqual(second_context["remaining_work"], ["sub works"])

    def test_checkpoint_exists_after_round_one_before_the_task_is_complete(self):
        self.two(budgets={**LOOSE, "max_rounds": 1})
        report = self.run_loop(Planner([step("s1", checks=["check-1"])]), Executor([ADD_OK]))
        state = self.state()
        self.assertEqual(report["status"], "BUDGET_EXHAUSTED")
        self.assertEqual(state["status"], "BUDGET_EXHAUSTED")
        self.assertEqual(len(state["verified_progress"]), 1)
        self.assertEqual(state["manager"]["stop"]["reason"], "max_rounds")
        self.assertTrue(state["manager"]["stop"]["resumable"])

    def test_acceptance_criterion_without_evidence_remains_not_proven(self):
        self.two(budgets={**LOOSE, "max_rounds": 1})
        self.assertEqual({c["status"] for c in manager.criteria_status(self.store.state["manager"])}, {"NOT_PROVEN"})
        value = manager.evaluate_completion(self.store, durable.snapshot(self.run_repo()))
        self.assertFalse(value["all_proven"])
        self.assertEqual([c["status"] for c in value["criteria"]], ["NOT_PROVEN", "NOT_PROVEN"])
        self.run_loop(Planner([step("s1", checks=["check-1"])]), Executor([ADD_OK]))
        store = self.reload()
        value = manager.evaluate_completion(store, durable.snapshot(self.run_repo()))
        self.assertEqual([(c["id"], c["status"]) for c in value["criteria"]], [("ADD", "PASS"), ("SUB", "NOT_PROVEN")])
        self.assertEqual(value["criteria"][1]["missing_checks"], ["check-2"])
        self.assertEqual(value["criteria"][1]["evidence"], [])
        self.assertFalse(value["all_proven"])
        self.assertNotEqual(store.state["status"], "COMPLETED")

    def run_repo(self):
        return harness.Repository(self.path, self.config(), harness.EventLog(self.root / "x.jsonl"))

    def test_manager_completion_claim_never_completes_the_run(self):
        self.two(budgets={**LOOSE, "max_rounds": 1})
        claim = step("s1", checks=["check-1"])
        claim["completion_signal"] = "TASK COMPLETE: every criterion is satisfied"
        claim["rationale"] = "All done, mark the whole task COMPLETED"
        self.run_loop(Planner([claim]), Executor([ADD_OK]))
        self.assertEqual(self.state()["status"], "BUDGET_EXHAUSTED")
        self.assertNotEqual(self.state()["status"], "COMPLETED")

    def test_completion_evidence_references_are_durable_and_checksummed(self):
        self.two()
        self.run_loop(Planner([step("s1", checks=["check-1"]), step("s2", checks=["check-2"])]), Executor([ADD_OK, BOTH_OK]))
        store = self.reload()
        completion = store.read_evidence(store.state["manager"]["completion"]["reference"])
        self.assertTrue(completion["all_proven"])
        for criterion in completion["criteria"]:
            self.assertEqual(criterion["status"], "PASS")
            self.assertGreaterEqual(len(criterion["evidence"]), 2)
            for reference in criterion["evidence"]:
                self.assertEqual(store.read_evidence(reference) is not None, True)
        report = json.loads((store.directory / "final-report.json").read_text())
        self.assertEqual(report["status"], "COMPLETED")
        self.assertEqual(report["completion"]["reference"], store.state["manager"]["completion"]["reference"])
        first = completion["criteria"][0]["evidence"][0]
        path = store.directory / first["path"]
        tampered = json.loads(path.read_text())
        tampered["passed"] = not tampered["passed"]
        path.write_text(json.dumps(tampered))
        with self.assertRaisesRegex(durable.DurableError, "checksum"):
            store.read_evidence(first)

    def test_default_criterion_binds_to_every_pinned_check(self):
        self.make(TWO, ["both work"], budgets=LOOSE)
        self.run_loop(Planner([step("s1", checks=["check-1", "check-2"])]), Executor([BOTH_OK]))
        self.assertEqual(self.state()["status"], "COMPLETED")


class VerifiedExistingTests(ManagerCase):
    def existing(self, **kwargs):
        (self.path / "calc.py").write_text(ADD_OK, encoding="utf-8")
        self.git("add", "calc.py")
        self.git("commit", "-qm", "existing implementation")
        return self.make(budgets={**LOOSE, "max_rounds": 1}, **kwargs)

    def test_unchanged_existing_code_is_trusted_only_with_durable_acceptance_evidence(self):
        self.existing()
        report = self.run_loop(Planner([step()]), Direct([{}]))  # Model says "done"; writes nothing.
        store = self.reload()
        state = store.state
        record = state["manager"]["rounds"][0]
        self.assertEqual(report["status"], "COMPLETED")
        self.assertEqual((record["implementation"], record["verification"], record["trusted"]),
                         ("verified_existing", "passed", True))
        self.assertEqual(record["changed_paths"], [])
        checkpoint = store.read_evidence(state["last_verified_checkpoint"]["reference"])
        self.assertEqual(checkpoint["snapshot"], state["baseline"])
        self.assertFalse(checkpoint["gates"]["implementation_changed"])
        self.assertTrue(checkpoint["gates"]["implementation_valid"])
        evidence = store.read_evidence(checkpoint["verification"][-1])
        self.assertTrue(evidence["passed"])
        self.assertEqual([r["argv"] for r in evidence["commands"]], [CHECK1, ["git", "diff", "--check"]])
        self.assertTrue(all(r["exit_code"] == 0 for r in evidence["commands"]))
        completion = store.read_evidence(state["manager"]["completion"]["reference"])
        self.assertTrue(completion["all_proven"])
        self.assertEqual(completion["criteria"][0]["status"], "PASS")
        self.assertIn(checkpoint["verification"][-1], completion["criteria"][0]["evidence"])
        self.assertIn(state["last_verified_checkpoint"]["reference"], completion["criteria"][0]["evidence"])

    def test_done_claim_with_missing_verifier_results_cannot_verify_existing(self):
        self.existing()
        with patch.object(durable.DurableRepository, "execute", return_value=None):
            self.run_loop(Planner([step()]), Direct([{}]))
        state = self.state()
        record = state["manager"]["rounds"][0]
        self.assertEqual((record["implementation"], record["reason"]), ("no_change", "VERIFICATION_FAILED"))
        self.assertFalse(record["trusted"])
        self.assertIsNone(state["last_verified_checkpoint"])

    def test_no_diff_still_requires_reviewer_approval(self):
        self.existing(reviewer=True)
        reviewer = Reviewer([(False, "Incomplete evidence")])
        self.run_loop(Planner([step(reviewer=True)]), Direct([{}]), reviewer=reviewer)
        record = self.rounds()[0]
        self.assertEqual(len(reviewer.calls), 1)
        self.assertEqual((record["implementation"], record["reason"]),
                         ("verified_existing", "REVIEWER_REJECTED"))
        self.assertFalse(record["trusted"])
        self.assertIsNone(self.state()["last_verified_checkpoint"])

    def test_no_diff_preserves_critic_review_and_final_verification_order(self):
        self.existing(critic=True, reviewer=True)
        gateway = self.gateway
        self.run_loop(Planner([step(risk="medium", reviewer=True)]), Direct([{}]),
                      Critic([clean_critic()], gateway, "critic"),
                      Reviewer([(True, "Approved")], gateway, "review"))
        store = self.reload()
        record = store.state["manager"]["rounds"][0]
        self.assertEqual((record["implementation"], record["critic"], record["reviewer"], record["trusted"]),
                         ("verified_existing", "completed", "approved", True))
        self.assertEqual(len(record["evidence"]["verifier"]), 2)
        checkpoint = store.read_evidence(record["evidence"]["checkpoint"])
        first, final = [store.read_evidence(ref) for ref in checkpoint["verification"]]
        review = store.read_evidence(checkpoint["reviewer"])
        self.assertLessEqual(first["time"], review["time"])
        self.assertLessEqual(review["time"], final["time"])
        self.assertTrue(first["passed"] and final["passed"])
        self.assertFalse(checkpoint["gates"]["implementation_changed"])
        self.assertTrue(store.read_evidence(checkpoint["critic"])["advisory_only"])
        self.assertIsNone(gateway.loaded)

    def test_no_diff_reviewer_approval_cannot_override_failed_final_check(self):
        self.existing(reviewer=True)
        real = durable.DurableRepository.execute
        calls = []

        def flaky(repo, argv, timeout=180):
            if argv == CHECK1:
                calls.append(argv)
                if len(calls) == 2:
                    argv = ["python", "-m", "unittest", "test_missing_module"]
            return real(repo, argv, timeout)

        with patch.object(durable.DurableRepository, "execute", flaky):
            self.run_loop(Planner([step(reviewer=True)]), Direct([{}]),
                          reviewer=Reviewer([(True, "Approved")]))
        record = self.rounds()[0]
        self.assertEqual((record["reviewer"], record["reason"]), ("approved", "FINAL_VERIFICATION_FAILED"))
        self.assertFalse(record["trusted"])
        self.assertIsNone(self.state()["last_verified_checkpoint"])

    def test_checkpoint_without_verification_evidence_fails_closed(self):
        self.existing()
        real = manager.ManagerLoop.checkpoint

        def missing(loop, number, step_, policy, commands, verifications, *args):
            return real(loop, number, step_, policy, commands, [], *args)

        with patch.object(manager.ManagerLoop, "checkpoint", missing):
            self.run_loop(Planner([step()]), Direct([{}]))
        self.assertEqual(self.rounds()[0]["reason"], "GATES_REFUSED")
        self.assertIsNone(self.state()["last_verified_checkpoint"])


class LoopFailureTests(ManagerCase):
    def test_verification_failure_triggers_evidence_driven_replan(self):
        self.make(budgets=LOOSE)
        planner, executor = RepairPlanner([step()]), Executor([wrong(1), ADD_OK])
        report = self.run_loop(planner, executor)
        rounds = self.rounds()
        self.assertEqual(report["status"], "COMPLETED")
        self.assertEqual([(r["outcome"], r["reason"], r["kind"]) for r in rounds],
                         [("UNTRUSTED", "VERIFICATION_FAILED", "normal"), ("TRUSTED", None, "repair")])
        self.assertEqual(len(planner.calls), 2, "failure must invoke fresh planning even at low risk")
        self.assertIn("recent_failure", executor.calls[1][1])
        self.assertEqual(executor.calls[1][1]["recent_failure"]["reason"], "VERIFICATION_FAILED")
        self.assertEqual(rounds[1]["attempt"], 2)

    def test_failed_verification_never_checkpoints(self):
        self.make(budgets={**LOOSE, "max_rounds": 1})
        self.run_loop(Planner([step()]), Executor([wrong(1)]))
        state = self.state()
        self.assertIsNone(state["last_verified_checkpoint"])
        self.assertEqual(state["verified_progress"], [])
        self.assertEqual(self.dirs(), [])
        self.assertEqual(state["manager"]["rounds"][0]["verification"], "failed")
        self.assertFalse(state["manager"]["rounds"][0]["trusted"])

    def test_repeated_identical_failure_is_stalled_and_stops_burning_model_time(self):
        self.make(budgets={**LOOSE, "max_stall_rounds": 2})
        executor = Executor([wrong(1), wrong(2), wrong(3), wrong(4)])
        report = self.run_loop(RepairPlanner([step()]), executor)
        state = self.state()
        self.assertEqual((report["status"], state["manager"]["stop"]["reason"]), ("STALLED", "NO_NEW_EVIDENCE"))
        self.assertEqual(len(executor.calls), 3)
        self.assertEqual([r["stalled"] for r in state["manager"]["rounds"]], [False, True, True])
        self.assertIsNone(state["last_verified_checkpoint"])
        self.assertEqual(state["manager"]["counters"]["stall_rounds"], 2)

    def test_oscillating_repair_is_a_stall(self):
        self.make(budgets={**LOOSE, "max_stall_rounds": 2})
        executor = Executor([returns(1), returns(2), returns(1), returns(2), returns(3)])
        self.run_loop(RepairPlanner([step()]), executor)
        self.assertEqual(self.state()["status"], "STALLED")
        self.assertEqual(len(executor.calls), 4)

    def test_stalled_run_refuses_automatic_resume(self):
        self.make(budgets={**LOOSE, "max_stall_rounds": 1})
        self.run_loop(RepairPlanner([step()]), Executor([wrong(1), wrong(2)]))
        self.assertEqual(self.state()["status"], "STALLED")
        with self.assertRaisesRegex(durable.DurableError, "STALLED.*refused"):
            self.resume()

    def test_max_rounds_exhausted_is_resumable_not_a_failure(self):
        self.make(budgets={**LOOSE, "max_rounds": 2})
        executor = Executor([returns(1), returns(2)])
        report = self.run_loop(RepairPlanner([step()]), executor)
        state = self.state()
        self.assertEqual((report["status"], state["manager"]["stop"]["reason"]), ("BUDGET_EXHAUSTED", "max_rounds"))
        self.assertNotIn(state["status"], {"FAILED", "STALLED", "BLOCKED"})
        with self.assertRaisesRegex(durable.DurableError, "still exhausted"):
            self.resume()
        self.assertEqual(self.state()["status"], "BUDGET_EXHAUSTED")
        report = self.resume(planner=RepairPlanner([step()]), executor=Executor([ADD_OK]), budgets={"max_rounds": 4})
        self.assertEqual(report["status"], "COMPLETED")
        self.assertEqual(self.state()["manager"]["counters"]["rounds_started"], 3)
        self.assertEqual(self.state()["manager"]["budget_history"][0]["after"]["max_rounds"], 4)

    def test_total_runtime_budget_exhaustion_abandons_the_round_and_is_resumable(self):
        self.make(budgets={**LOOSE, "max_runtime_seconds": 50})
        clock = Clock()

        class Slow(Executor):
            def execute(inner, step_, context, repo):
                clock.now += 100
                return super().execute(step_, context, repo)
        report = self.run_loop(Planner([step()]), Slow([wrong(1)]), clock=clock)
        state = self.state()
        self.assertEqual((report["status"], state["manager"]["stop"]["reason"]), ("BUDGET_EXHAUSTED", "max_total_runtime"))
        self.assertEqual(state["manager"]["rounds"][0]["outcome"], "ABANDONED")
        self.assertGreaterEqual(state["manager"]["runtime_seconds"], 100)
        self.assertIsNone(state["last_verified_checkpoint"])
        self.assertEqual(state["manager"]["rounds"][0]["verification"], "not_run")
        with self.assertRaisesRegex(durable.DurableError, "still exhausted"):
            self.resume(clock=Clock())
        # More time: the unfinished edit is re-verified (never trusted) before anything else happens.
        report = self.resume(planner=RepairPlanner([step()]), executor=Executor([ADD_OK]), budgets={"max_runtime_seconds": 500}, clock=Clock())
        rounds = self.rounds()
        self.assertEqual(rounds[1]["kind"], "verify_only")
        self.assertEqual(rounds[1]["reason"], "VERIFICATION_FAILED")
        self.assertEqual(report["status"], "COMPLETED")

    def test_step_retry_budget_exhausted(self):
        self.make(budgets={**LOOSE, "max_step_retries": 1})
        executor = Executor([returns(1), returns(2), returns(3)])
        report = self.run_loop(RepairPlanner([step()]), executor)
        self.assertEqual((report["status"], self.state()["manager"]["stop"]["reason"]), ("BUDGET_EXHAUSTED", "max_step_retries"))
        self.assertEqual(len(executor.calls), 2)
        self.assertEqual(self.state()["manager"]["rounds"][-1]["attempt"], 2)

    def test_consecutive_failed_round_budget(self):
        self.make(budgets={**LOOSE, "max_consecutive_failed_rounds": 2})
        executor = Executor([returns(1), returns(2), returns(3)])
        report = self.run_loop(RepairPlanner([step()]), executor)
        self.assertEqual(self.state()["manager"]["stop"]["reason"], "max_consecutive_failed_rounds")
        self.assertEqual(report["status"], "BUDGET_EXHAUSTED")

    def test_model_invocation_budget_is_optional_and_enforced(self):
        self.make(budgets={**LOOSE, "max_model_invocations": {"code": 1}})
        planner, executor = Planner([step()]), Executor([ADD_OK])
        report = self.run_loop(planner, executor)
        self.assertEqual(self.state()["manager"]["stop"]["reason"], "max_model_invocations:code")
        self.assertEqual(report["status"], "BUDGET_EXHAUSTED")
        self.assertEqual(len(executor.calls), 0)

    def test_invalid_manager_output_never_reaches_the_executor(self):
        self.make(budgets={**LOOSE, "max_consecutive_failed_rounds": 2})
        planner = Planner([{"foo": 1}, "not even json"])
        report = self.run_loop(planner, Forbidden("executor"))
        state = self.state()
        self.assertEqual(report["status"], "BUDGET_EXHAUSTED")
        self.assertEqual([r["reason"] for r in state["manager"]["rounds"]], ["INVALID_MANAGER_OUTPUT"] * 2)
        self.assertEqual([r["implementation"] for r in state["manager"]["rounds"]], ["not_started"] * 2)
        self.assertIsNone(state["last_verified_checkpoint"])

    def test_repeated_identical_invalid_output_is_a_stall(self):
        self.make(budgets={**LOOSE, "max_stall_rounds": 2})
        self.run_loop(Planner([{"foo": 1}], repeat_last=True), Forbidden("executor"))
        self.assertEqual(self.state()["status"], "STALLED")
        self.assertEqual(len(self.rounds()), 3)

    def test_manager_proposing_forbidden_path_is_refused_before_execution(self):
        self.make(budgets={**LOOSE, "max_rounds": 2}, forbid=["secrets/"])
        planner = Planner([step(paths=["secrets/key.txt"]), step(paths=[".git/config"])])
        self.run_loop(planner, Forbidden("executor"))
        self.assertEqual([r["reason"] for r in self.rounds()], ["FORBIDDEN_PATH_PROPOSED"] * 2)
        self.assertIsNone(self.state()["last_verified_checkpoint"])

    def test_manager_repeating_the_same_step_without_progress_is_a_stall(self):
        self.make(budgets={**LOOSE, "max_stall_rounds": 2})
        executor = Executor([returns(1)])
        planner = Planner([step(risk="medium")], repeat_last=True)
        self.run_loop(planner, executor)
        state = self.state()
        self.assertEqual(state["status"], "STALLED")
        self.assertEqual(len(executor.calls), 1, "blind replay must never reach the executor")
        # Invalid planning retains the underlying failed state, so it cannot buy a fresh stall window.
        self.assertEqual([r["reason"] for r in state["manager"]["rounds"]][1:],
                         ["INVALID_MANAGER_OUTPUT"] * 2)
        self.assertEqual(len(planner.calls), 3)

    def test_executor_with_no_diff_runs_failing_verifier_and_never_trusts(self):
        self.make(budgets={**LOOSE, "max_rounds": 1})
        self.run_loop(Planner([step()]), Executor([{}]))
        state = self.state()
        record = state["manager"]["rounds"][0]
        self.assertEqual((record["reason"], record["implementation"], record["verification"]),
                         ("VERIFICATION_FAILED", "no_change", "failed"))
        self.assertFalse(record["trusted"])
        self.assertEqual(record["outcome"], "UNTRUSTED")
        self.assertIsNone(state["last_verified_checkpoint"])
        self.assertEqual(state["verified_progress"], [])
        self.assertEqual(self.dirs(), [])
        store = self.reload()
        self.assertEqual(len(record["evidence"]["verifier"]), 1)
        evidence = store.read_evidence(record["evidence"]["verifier"][0])
        self.assertFalse(evidence["passed"])
        self.assertEqual([r["argv"] for r in evidence["commands"]], [CHECK1, ["git", "diff", "--check"]])
        self.assertNotEqual(evidence["commands"][0]["exit_code"], 0)
        self.assertEqual(evidence["snapshot"], state["baseline"])
        self.assertEqual(manager.criteria_status(state["manager"])[0]["status"], "NOT_PROVEN")

    def test_executor_error_is_a_failed_round_not_a_crash(self):
        self.make(budgets={**LOOSE, "max_rounds": 1})
        self.run_loop(Planner([step()]), Executor([manager.ExecutorError("Executor exceeded its action budget")]))
        self.assertEqual(self.rounds()[0]["reason"], "EXECUTOR_ERROR")
        self.assertEqual(self.state()["status"], "BUDGET_EXHAUSTED")

    def test_executor_modifying_a_forbidden_file_cannot_checkpoint(self):
        self.make(budgets={**LOOSE, "max_rounds": 1}, forbid=["secrets/"])

        self.run_loop(Planner([step()]), Direct([{"calc.py": ADD_OK, "secrets/key.txt": "leak"}]))
        state = self.state()
        self.assertEqual(state["manager"]["rounds"][0]["reason"], "SCOPE_VIOLATION")
        self.assertIsNone(state["last_verified_checkpoint"])
        self.assertEqual(state["evidence"]["verifier"], [])

    def test_out_of_scope_path_is_a_violation_even_if_not_globally_forbidden(self):
        self.make(budgets={**LOOSE, "max_rounds": 1})
        self.run_loop(Planner([step(paths=["calc.py"])]), Direct([{"calc.py": ADD_OK, "notes.txt": "extra"}]))
        self.assertEqual(self.rounds()[0]["reason"], "SCOPE_VIOLATION")
        self.assertIsNone(self.state()["last_verified_checkpoint"])

    def test_scoped_repository_refuses_writes_outside_scope_and_to_git(self):
        self.make(budgets={**LOOSE, "max_rounds": 1}, forbid=["secrets/"])
        seen = []

        class Probing(Executor):
            def execute(inner, step_, context, repo):
                for name in ("secrets/key.txt", "other.py", ".git/config", "../escape.py"):
                    try:
                        repo.write(name, "x")
                    except ValueError:
                        seen.append(name)
                repo.write("calc.py", ADD_OK)
                return "done"
        self.run_loop(Planner([step()]), Probing([]))
        self.assertEqual(seen, ["secrets/key.txt", "other.py", ".git/config", "../escape.py"])
        self.assertEqual(self.state()["status"], "COMPLETED")
        self.assertFalse((self.path / "secrets").exists())
        self.assertFalse((self.path / "other.py").exists())

    def test_scope_violation_persists_until_repaired(self):
        self.make(budgets={**LOOSE, "max_rounds": 2})
        executor = Direct([{"calc.py": ADD_OK, "notes.txt": "extra"}, {"calc.py": ADD_OK_DOC}])
        self.run_loop(RepairPlanner([step()]), executor)
        # Round 2 still carries notes.txt from round 1, so the cumulative scope check refuses it again.
        self.assertEqual([r["reason"] for r in self.rounds()], ["SCOPE_VIOLATION", "SCOPE_VIOLATION"])
        self.assertIsNone(self.state()["last_verified_checkpoint"])


class CriticReviewerTests(ManagerCase):
    def enabled(self, **kwargs):
        return self.make(budgets=kwargs.pop("budgets", LOOSE), critic=kwargs.pop("critic", True),
                         reviewer=kwargs.pop("reviewer", False), **kwargs)

    def test_low_risk_step_skips_critic_and_reviewer(self):
        self.enabled(reviewer=True)
        planner = Planner([step(risk="low")])
        report = self.run_loop(planner, Executor([ADD_OK]), Critic([]), Reviewer([]))
        self.assertEqual(report["status"], "COMPLETED")
        self.assertEqual(planner.tiers(), ["executor"])
        self.assertEqual(self.state()["manager"]["rounds"][0]["policy"]["reviewer"], "not_required")
        self.assertEqual(self.state()["manager"]["counters"]["model_invocations"], {"code": 2})

    def test_medium_step_runs_critic_but_not_reviewer_when_clean(self):
        self.enabled(reviewer=True)
        critic = Critic([clean_critic()])
        self.run_loop(Planner([step(risk="medium")]), Executor([ADD_OK]), critic, Reviewer([]))
        self.assertEqual(self.state()["status"], "COMPLETED")
        self.assertEqual(len(critic.calls), 1)
        self.assertEqual(self.state()["manager"]["rounds"][0]["critic"], "completed")

    def test_critic_advisory_finding_triggers_another_round(self):
        self.enabled()
        planner = RepairPlanner([step("a", risk="medium"), step("b", risk="medium")])
        critic = Critic([finding_critic("high"), clean_critic()])
        executor = Executor([ADD_OK, ADD_OK_DOC])
        report = self.run_loop(planner, executor, critic)
        rounds = self.rounds()
        self.assertEqual(report["status"], "COMPLETED")
        self.assertEqual([(r["outcome"], r["reason"]) for r in rounds],
                         [("UNTRUSTED", "CRITIC_FINDINGS_UNRESOLVED"), ("TRUSTED", None)])
        self.assertEqual(rounds[0]["verification"], "passed", "deterministic evidence alone did not checkpoint")
        self.assertEqual(self.dirs(), ["0002.json"])

    def test_low_severity_findings_are_advisory_only(self):
        self.enabled()
        self.run_loop(Planner([step(risk="medium")]), Executor([ADD_OK]), Critic([finding_critic("low")]))
        self.assertEqual(self.state()["status"], "COMPLETED")
        self.assertEqual(len(self.rounds()), 1)

    def test_blocking_critic_finding_escalates_to_reviewer_who_arbitrates(self):
        self.enabled(reviewer=True)
        reviewer = Reviewer([(True, "Concern is a false positive")])
        self.run_loop(Planner([step(risk="medium")]), Executor([ADD_OK]), Critic([finding_critic("blocker")]), reviewer)
        state = self.state()
        self.assertEqual(state["status"], "COMPLETED")
        self.assertEqual(len(reviewer.calls), 1)
        self.assertEqual(reviewer.calls[0][1]["status"], "completed", "the reviewer receives the critic evidence")
        store = self.reload()
        checkpoint = store.read_evidence(state["last_verified_checkpoint"]["reference"])
        self.assertTrue(store.read_evidence(checkpoint["reviewer"])["approved"])
        self.assertEqual(store.read_evidence(checkpoint["critic"])["advisory_only"], True)

    def test_critic_output_cannot_create_trust(self):
        self.enabled()
        outputs = [{"status": "malformed"}, {"status": "failed"}, "garbage", None,
                   {"status": "completed", "result": {"approved": True, "findings": []}},
                   {"status": "approved"}]
        for index, output in enumerate(outputs):
            with self.subTest(output=output):
                self.path_reset()
                store = self.make(budgets={**LOOSE, "max_rounds": 1}, critic=True, run_id=f"critic-{index}")
                self.run_loop(Planner([step(risk="medium")]), Executor([ADD_OK]), Critic([output]), store=store)
                state = self.reload_named(f"critic-{index}")
                checkpoint = state.read_evidence(state.state["last_verified_checkpoint"]["reference"])
                recorded = state.read_evidence(checkpoint["critic"])
                self.assertNotEqual(recorded["status"], "completed")
                self.assertEqual(recorded["advisory_only"], True)
                self.assertNotIn("result", recorded)
                self.assertEqual(checkpoint["critic_status"], recorded["status"])

    def path_reset(self):
        self.git("checkout", "--", ".")
        self.git("clean", "-fdq")

    def reload_named(self, name):
        store = durable.Store(self.root / "runs", name)
        store.load()
        return store

    def test_malformed_critic_with_required_reviewer_rejection_creates_no_checkpoint(self):
        self.enabled(reviewer=True, budgets={**LOOSE, "max_rounds": 1})
        planner = Planner([step(risk="high"), step(risk="high")])
        reviewer = Reviewer([(False, "insufficient evidence")])
        self.run_loop(planner, Executor([ADD_OK]), Critic([{"status": "malformed"}]), reviewer)
        state = self.state()
        record = state["manager"]["rounds"][0]
        self.assertEqual((record["critic"], record["reviewer"], record["reason"]), ("malformed", "rejected", "REVIEWER_REJECTED"))
        self.assertIsNone(state["last_verified_checkpoint"])

    def test_reviewer_rejection_with_passing_verification_cannot_checkpoint(self):
        self.enabled(reviewer=True, budgets={**LOOSE, "max_rounds": 1})
        planner = Planner([step(risk="high"), step(risk="high")])
        reviewer = Reviewer([(False, "The change is incomplete")])
        self.run_loop(planner, Executor([ADD_OK]), Critic([clean_critic()]), reviewer)
        state = self.state()
        record = state["manager"]["rounds"][0]
        self.assertEqual((record["verification"], record["reviewer"], record["reason"]), ("passed", "rejected", "REVIEWER_REJECTED"))
        self.assertIsNone(state["last_verified_checkpoint"])
        self.assertEqual(self.dirs(), [])

    def test_reviewer_approval_with_failed_final_verification_cannot_checkpoint(self):
        self.enabled(reviewer=True, critic=False, budgets={**LOOSE, "max_rounds": 1})
        calls = []
        real = durable.DurableRepository.execute

        def flaky(repo, argv, timeout=180):
            if argv == CHECK1:
                calls.append(argv)
                if len(calls) == 2:
                    argv = ["python", "-m", "unittest", "test_missing_module"]
            return real(repo, argv, timeout)
        reviewer = Reviewer([(True, "Looks right")])
        with patch.object(durable.DurableRepository, "execute", flaky):
            self.run_loop(Planner([step(risk="high"), step(risk="high")]), Executor([ADD_OK]), None, reviewer)
        state = self.state()
        record = state["manager"]["rounds"][0]
        self.assertEqual((record["reviewer"], record["reason"]), ("approved", "FINAL_VERIFICATION_FAILED"))
        self.assertIsNone(state["last_verified_checkpoint"])

    def test_repository_changed_during_review_invalidates_approval(self):
        self.enabled(reviewer=True, critic=False, budgets={**LOOSE, "max_rounds": 1})

        class Tamper(Reviewer):
            def review(inner, step_, context, evidence, diff, critic):
                (Path(self.path) / "calc.py").write_text(BOTH_OK, encoding="utf-8")
                return True, "approve"
        self.run_loop(Planner([step(risk="high"), step(risk="high")]), Executor([ADD_OK]), None, Tamper([]))
        record = self.rounds()[0]
        self.assertEqual((record["reviewer"], record["reason"]), ("rejected", "REVIEWER_REJECTED"))
        self.assertIsNone(self.state()["last_verified_checkpoint"])

    def test_high_risk_requires_reviewer_and_plans_with_it(self):
        self.enabled(reviewer=True)
        planner = Planner([step(risk="high"), step(risk="high", goal="Reviewed plan")])
        reviewer = Reviewer([(True, "ok")])
        self.run_loop(planner, Executor([ADD_OK]), Critic([clean_critic()]), reviewer)
        state = self.state()
        self.assertEqual(planner.tiers(), ["executor", "reviewer"])
        self.assertEqual(state["status"], "COMPLETED")
        self.assertEqual(len(reviewer.calls), 1)
        self.assertEqual(state["manager"]["counters"]["model_invocations"], {"code": 2, "review": 2, "critic": 1})
        self.assertEqual(state["manager"]["rounds"][0]["policy"]["reviewer"], "required")
        self.assertEqual(state["manager"]["rounds"][0]["step_id"], "s1")
        self.assertIsNotNone(state["manager"]["rounds"][0]["evidence"]["reviewer"])

    def test_high_risk_without_reviewer_stops_for_a_human_before_any_edit(self):
        self.enabled(reviewer=False)
        report = self.run_loop(Planner([step(risk="high")]), Forbidden("executor"))
        self.assertEqual((report["status"], self.state()["manager"]["stop"]["reason"]),
                         ("HUMAN_ACTION_REQUIRED", "REVIEWER_REQUIRED_NOT_ENABLED"))
        self.assertIn("--reviewer", report["human_action"])

    def test_sensitive_path_floor_requires_reviewer_even_if_declared_low(self):
        self.enabled(reviewer=False)
        report = self.run_loop(Planner([step(risk="low", paths=["config/app.json"])]), Forbidden("executor"))
        self.assertEqual(report["status"], "HUMAN_ACTION_REQUIRED")
        self.assertEqual(self.rounds()[0]["outcome"], "ABANDONED")

    def test_manager_requested_reviewer_is_honoured_on_a_low_step(self):
        self.enabled(reviewer=True)
        reviewer = Reviewer([(True, "ok")])
        self.run_loop(Planner([step(risk="low", reviewer=True)]), Executor([ADD_OK]), None, reviewer)
        self.assertEqual(len(reviewer.calls), 1)
        self.assertEqual(self.state()["status"], "COMPLETED")

    def test_enabling_the_reviewer_on_resume_unblocks_a_high_risk_step(self):
        self.enabled(reviewer=False)
        self.run_loop(Planner([step(risk="high")]), Forbidden("executor"))
        reviewer = Reviewer([(True, "ok")])
        report = self.resume(Planner([step(risk="high"), step(risk="high")]), Executor([ADD_OK]),
                             Critic([clean_critic()]), reviewer, enable_reviewer=True)
        self.assertEqual(report["status"], "COMPLETED")
        self.assertTrue(self.state()["options"]["reviewer"])

    def test_one_large_model_at_a_time_and_nothing_resident_at_checkpoint(self):
        self.enabled(reviewer=True)
        gateway = self.gateway
        resident = []
        real = manager.ManagerLoop.checkpoint

        def spy(loop, *args, **kwargs):
            resident.append(gateway.loaded)
            return real(loop, *args, **kwargs)
        planner = TierPlanner([step(risk="high"), step(risk="high")], gateway)
        with patch.object(manager.ManagerLoop, "checkpoint", spy):
            self.run_loop(planner, Executor([ADD_OK], gateway, "code"), Critic([clean_critic()], gateway, "critic"),
                          Reviewer([(True, "ok")], gateway, "review"))
        roles = [e for e in gateway.events if e in {"code", "critic", "review"}]
        self.assertEqual(roles, ["code", "review", "code", "critic", "review"])
        self.assertEqual(gateway.peak, 1)
        self.assertEqual(resident, [None], "models must be unloaded before the authoritative checkpoint")
        self.assertIsNone(gateway.loaded)
        # The reviewer-planner is unloaded by the Manager before the executor starts.
        first_review = gateway.events.index("review")
        self.assertEqual(gateway.events[first_review + 1], "unload")


class InterruptionTests(ManagerCase):
    def test_interruption_during_planning_is_abandoned_and_replanned(self):
        self.make(budgets=LOOSE)
        self.kill(Planner([SimulatedKill()]))
        self.assertEqual(self.state()["status"], "PLANNING")
        report = self.resume(Planner([step()]), Executor([ADD_OK]))
        rounds = self.rounds()
        self.assertEqual(report["status"], "COMPLETED")
        self.assertEqual([(r["outcome"], r["reason"]) for r in rounds], [("ABANDONED", "INTERRUPTED_IN_PLANNING"), ("TRUSTED", None)])
        self.assertTrue(rounds[0]["interruption"])

    def test_interruption_before_execution_changes_nothing_and_starts_a_fresh_round(self):
        self.make(budgets=LOOSE)
        self.kill(Planner([step()]), Executor([SimulatedKill()]))
        state = self.state()
        self.assertEqual(state["status"], "EXECUTING")
        self.assertIsNone(state["last_verified_checkpoint"])
        planner = Planner([step()])
        report = self.resume(planner, Executor([ADD_OK]))
        self.assertEqual(report["status"], "COMPLETED")
        self.assertEqual(self.rounds()[0]["reason"], "INTERRUPTED_IN_EXECUTING")
        self.assertEqual(len(planner.calls), 1)

    def test_interruption_after_write_before_verification_reverifies_without_duplicating_work(self):
        self.make(budgets=LOOSE)

        class WriteThenDie(Executor):
            def execute(inner, step_, context, repo):
                repo.write("calc.py", ADD_OK)
                raise SimulatedKill()
        self.kill(Planner([step()]), WriteThenDie([]))
        self.assertEqual(self.state()["status"], "EXECUTING")
        self.assertIsNone(self.state()["last_verified_checkpoint"])
        self.assertEqual(self.state()["evidence"]["verifier"], [])
        report = self.resume(Forbidden("planner"), Forbidden("executor"))
        rounds = self.rounds()
        self.assertEqual(report["status"], "COMPLETED")
        self.assertEqual([(r["kind"], r["outcome"]) for r in rounds], [("normal", "ABANDONED"), ("verify_only", "TRUSTED")])
        self.assertEqual(self.state()["recovery"]["action"], "verify_only")

    def test_interruption_during_verification(self):
        self.make(budgets=LOOSE)
        with patch.object(manager.ManagerLoop, "verify", side_effect=SimulatedKill()):
            self.kill(Planner([step()]), Executor([ADD_OK]))
        self.assertEqual(self.state()["status"], "VERIFYING")
        self.assertIsNone(self.state()["last_verified_checkpoint"])
        report = self.resume(Forbidden("planner"), Forbidden("executor"))
        self.assertEqual(report["status"], "COMPLETED")
        self.assertEqual(self.rounds()[0]["reason"], "INTERRUPTED_IN_VERIFYING")

    def test_interruption_after_verification_before_checkpoint(self):
        self.make(budgets=LOOSE)
        with patch.object(manager.ManagerLoop, "checkpoint", side_effect=SimulatedKill()):
            self.kill(Planner([step()]), Executor([ADD_OK]))
        state = self.state()
        self.assertEqual(state["status"], "CHECKPOINTING")
        self.assertTrue(state["evidence"]["verifier"], "verification evidence exists but is not trusted")
        self.assertIsNone(state["last_verified_checkpoint"])
        self.assertEqual(self.dirs(), [])
        report = self.resume(Forbidden("planner"), Forbidden("executor"))
        self.assertEqual(report["status"], "COMPLETED")
        self.assertEqual(self.dirs(), ["0002.json"])

    def test_resume_from_trusted_checkpoint_keeps_it_and_continues(self):
        self.two()
        self.kill(Planner([step("s1", checks=["check-1"]), SimulatedKill()]), Executor([ADD_OK]))
        before = self.state()
        self.assertEqual(before["status"], "PLANNING")
        first = copy.deepcopy(before["last_verified_checkpoint"])
        self.assertIsNotNone(first)
        report = self.resume(Planner([step("s2", checks=["check-2"])]), Executor([BOTH_OK]))
        after = self.state()
        self.assertEqual(report["status"], "COMPLETED")
        self.assertEqual(after["verified_progress"][0], before["verified_progress"][0])
        self.assertEqual([r["outcome"] for r in after["manager"]["rounds"]], ["TRUSTED", "ABANDONED", "TRUSTED"])
        self.assertEqual(self.reload().read_evidence(first["reference"])["step"]["step_id"], "s1")

    def test_resume_does_not_trust_an_unverified_diff(self):
        self.make(budgets=LOOSE)

        class BreakThenDie(Executor):
            def execute(inner, step_, context, repo):
                repo.write("calc.py", wrong("x"))
                raise SimulatedKill()
        self.kill(Planner([step()]), BreakThenDie([]))
        executor = Executor([ADD_OK])
        report = self.resume(RepairPlanner([step()]), executor)
        rounds = self.rounds()
        self.assertEqual(report["status"], "COMPLETED")
        self.assertEqual((rounds[1]["kind"], rounds[1]["outcome"], rounds[1]["reason"]),
                         ("verify_only", "UNTRUSTED", "VERIFICATION_FAILED"))
        self.assertFalse(rounds[1]["trusted"])
        self.assertEqual(len(executor.calls), 1, "only the repair round executes; the stale diff was never trusted")
        self.assertEqual(rounds[2]["kind"], "repair")
        self.assertEqual(self.dirs(), ["0003.json"])

    def test_completed_run_resume_is_idempotent(self):
        self.make()
        self.run_loop(Planner([step()]), Executor([ADD_OK]))
        before = self.state()["revision"]
        report = self.resume(Forbidden("planner"), Forbidden("executor"))
        self.assertEqual(report["status"], "COMPLETED")
        self.assertEqual(self.state()["revision"], before)


class SupervisionAndDriftTests(ManagerCase):
    def unknown_model(self):
        sup = supervision.Supervisor(self.store)
        child = sup.register("model_server", sys.executable, [], "delegated")
        sup.reconcile()
        self.assertEqual(sup.child(child)["state"], "UNKNOWN")
        return child

    def test_unknown_model_child_requires_human_action_and_no_model_runs(self):
        self.make(budgets=LOOSE)
        child = self.unknown_model()
        report = self.run_loop(Forbidden("planner"), Forbidden("executor"))
        state = self.state()
        self.assertEqual((report["status"], state["status"]), ("HUMAN_ACTION_REQUIRED", "HUMAN_ACTION_REQUIRED"))
        self.assertEqual(state["manager"]["stop"]["reason"], "UNKNOWN_CHILD")
        self.assertIn(child, report["human_action"])
        self.assertEqual([e for e in self.gateway.events if e != "unload"], [], "no model request is attempted")
        self.assertIsNone(state["last_verified_checkpoint"])

    def test_unknown_model_child_blocks_resume_and_is_never_auto_resolved(self):
        self.make(budgets=LOOSE)
        child = self.unknown_model()
        with patch.object(durable, "recover_model", side_effect=AssertionError("recover-model invoked")), \
             patch.object(supervision.Supervisor, "resolve_model", side_effect=AssertionError("resolved automatically")):
            self.run_loop(Forbidden("planner"), Forbidden("executor"))
            with self.assertRaisesRegex(durable.DurableError, "HUMAN_ACTION_REQUIRED"):
                self.resume(Forbidden("planner"), Forbidden("executor"))
        self.assertEqual(self.state()["supervision"]["children"][child]["state"], "UNKNOWN")
        self.assertEqual(self.state()["status"], "HUMAN_ACTION_REQUIRED")

    def test_manager_never_references_recover_model_machinery(self):
        tree = ast.parse(Path(manager.__file__).read_text(encoding="utf-8"))
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        self.assertEqual(names & {"recover_model", "resolve_model", "terminate", "terminate_owned_job"}, set())
        imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
        self.assertNotIn("recover_model", imported)

    def test_unknown_child_appearing_mid_run_stops_the_next_model_action(self):
        self.two()
        planner = Planner([step("s1", checks=["check-1"])])
        real = manager.ManagerLoop.check_completion

        def poison(loop):
            if loop.store.state["manager"]["counters"]["trusted_rounds"] == 1 and not getattr(poison, "done", False):
                poison.done = True
                sup = supervision.Supervisor(loop.store)
                sup.register("model_server", sys.executable, [], "delegated")
            return real(loop)
        with patch.object(manager.ManagerLoop, "check_completion", poison):
            report = self.run_loop(planner, Executor([ADD_OK]))
        self.assertEqual((report["status"], report["stop"]["reason"]), ("HUMAN_ACTION_REQUIRED", "UNKNOWN_CHILD"))
        self.assertEqual(len(planner.calls), 1)

    def test_running_verifier_is_never_duplicated(self):
        self.make(budgets=LOOSE)
        sup = supervision.Supervisor(self.store)
        # Use the actual executable path that Supervisor.inspect() would see for the current process
        import winprocess
        actual_executable = winprocess.identity(os.getpid())['executable']
        child = sup.register("verification_command", actual_executable, ["python", "-m", "unittest"])
        sup.started(child, os.getpid())
        with patch.object(durable.DurableRepository, "execute", side_effect=AssertionError("verifier duplicated")):
            with self.assertRaisesRegex(durable.DurableError, "PROCESS_RECOVERY.*running child is never duplicated"):
                self.resume(Forbidden("planner"), Forbidden("executor"))
            self.store = self.reload()  # resume used its own store instance and appended to the ledger
            report = self.run_loop(Forbidden("planner"), Forbidden("executor"))
        self.assertEqual((report["status"], report["stop"]["reason"]), ("HUMAN_ACTION_REQUIRED", "CHILD_STILL_RUNNING"))
        self.assertEqual(self.state()["supervision"]["children"][child]["state"], "RUNNING")

    def test_repository_head_drift_refuses_resume_without_model_or_state_change(self):
        self.make(budgets={**LOOSE, "max_rounds": 1})
        self.run_loop(Planner([step()]), Executor([wrong(1)]))
        revision = self.state()["revision"]
        self.git("commit", "--allow-empty", "-qm", "manual commit")
        with self.assertRaisesRegex(durable.DurableError, "HEAD/branch changed"):
            self.resume(Forbidden("planner"), Forbidden("executor"), budgets={"max_rounds": 5})
        self.assertEqual(self.state()["revision"], revision)

    def test_unexpected_manual_edit_refuses_resume(self):
        self.make(budgets={**LOOSE, "max_rounds": 1})
        self.run_loop(Planner([step()]), Executor([wrong(1)]))
        (self.path / "test_add.py").write_text(TEST_ADD + "# sneaky\n", encoding="utf-8")
        with self.assertRaisesRegex(durable.DurableError, "Unexpected dirty"):
            self.resume(Forbidden("planner"), Forbidden("executor"), budgets={"max_rounds": 5})

    def test_head_change_during_a_round_fails_closed_to_human_action(self):
        self.make(budgets=LOOSE)
        git = self.git

        class Committer(Executor):
            def execute(inner, step_, context, repo):
                repo.write("calc.py", ADD_OK)
                git("commit", "-qam", "executor committed")
                return "done"
        report = self.run_loop(Planner([step()]), Committer([]))
        state = self.state()
        self.assertEqual((report["status"], state["manager"]["stop"]["reason"]), ("HUMAN_ACTION_REQUIRED", "REPOSITORY_DRIFT"))
        self.assertIsNone(state["last_verified_checkpoint"])
        self.assertEqual(state["manager"]["rounds"][0]["reason"], "GATES_REFUSED")

    def drift(self, sha, unsafe=False):
        real = durable.current_fingerprint

        def capture(store):
            value = real(store)
            value["configs"]["test_component"] = {"path": "component", "size": 1, "sha256": sha}
            if unsafe:
                value["source"]["harness.py"] = {**value["source"]["harness.py"], "sha256": "f" * 64}
            value["sha256"] = supervision.checksum({k: v for k, v in value.items() if k != "sha256"})
            return value
        return capture

    def test_environment_drift_mid_run_stops_for_a_human(self):
        self.make(budgets=LOOSE)
        same, changed = self.drift("a" * 64), self.drift("b" * 64)
        self.store.commit(environment=same(self.store))
        calls = []

        def capture(store):
            calls.append(1)
            return same(store) if len(calls) == 1 else changed(store)
        with patch.object(durable, "current_fingerprint", capture):
            report = self.run_loop(Forbidden("planner"), Forbidden("executor"))
        self.assertEqual((report["status"], report["stop"]["reason"]), ("HUMAN_ACTION_REQUIRED", "ENVIRONMENT_DRIFT"))

    def test_environment_revalidation_policy_on_resume(self):
        self.two(budgets={**LOOSE, "max_rounds": 1})
        self.store.commit(environment=self.drift("a" * 64)(self.store))
        with patch.object(durable, "current_fingerprint", self.drift("a" * 64)):
            self.run_loop(Planner([step("s1", checks=["check-1"])]), Executor([ADD_OK]))
        checkpoint = copy.deepcopy(self.state()["last_verified_checkpoint"])
        self.assertIsNotNone(checkpoint)
        changed = self.drift("b" * 64)
        with patch.object(durable, "current_fingerprint", changed):
            with self.assertRaisesRegex(durable.DurableError, "revalidation required"):
                self.resume(Forbidden("planner"), Forbidden("executor"), budgets={"max_rounds": 6})
            self.assertEqual(self.state()["last_verified_checkpoint"], checkpoint)
            report = self.resume(Planner([step("s2", checks=["check-2"])]), Executor([BOTH_OK]),
                                 budgets={"max_rounds": 6}, revalidate_environment=True)
        state = self.state()
        self.assertEqual(report["status"], "COMPLETED")
        self.assertEqual([(r["kind"], r["outcome"]) for r in state["manager"]["rounds"]],
                         [("normal", "TRUSTED"), ("verify_only", "TRUSTED"), ("normal", "TRUSTED")])
        self.assertFalse(state["environment_revalidation_pending"])
        revalidated = state["manager"]["rounds"][1]
        self.assertEqual(revalidated["implementation"], "verified_existing")
        proof = self.reload().read_evidence(revalidated["evidence"]["checkpoint"])
        self.assertFalse(proof["gates"]["implementation_changed"])
        self.assertTrue(proof["gates"]["implementation_valid"])
        self.assertEqual(proof["snapshot"], checkpoint["snapshot"])
        self.assertEqual(state["verified_progress"][0]["checkpoint"], checkpoint["reference"])

    def test_unsafe_environment_drift_is_refused_even_with_the_revalidation_flag(self):
        self.two(budgets={**LOOSE, "max_rounds": 1})
        self.run_loop(Planner([step("s1", checks=["check-1"])]), Executor([ADD_OK]))
        with patch.object(durable, "current_fingerprint", self.drift("a" * 64, unsafe=True)):
            with self.assertRaisesRegex(durable.DurableError, "ENVIRONMENT_DRIFT"):
                self.resume(Forbidden("planner"), Forbidden("executor"), budgets={"max_rounds": 6},
                            revalidate_environment=True)
        self.assertEqual(len(self.state()["verified_progress"]), 1)


class ContextAndPersistenceTests(ManagerCase):
    WHITELIST = {"notes", "role", "task", "acceptance_criteria", "checks", "trusted_state", "remaining_work", "scope",
                 "budgets_remaining", "current_step", "recent_failure", "files", "task_contract_sha256",
                 "workspace", "recovery_evidence"}

    def test_fresh_context_contains_only_the_latest_failure_and_no_transcript(self):
        self.make(budgets=LOOSE)

        def broken(tag):
            return f"def add(a, b):\n    raise RuntimeError('{tag}')\n\ndef sub(a, b):\n    return 0\n"
        executor = Executor([broken("ROUND1_MARKER"), broken("ROUND2_MARKER"), ADD_OK])
        self.run_loop(RepairPlanner([step()]), executor)
        first, second, third = (call[1] for call in executor.calls)
        self.assertNotIn("recent_failure", first)
        self.assertIn("ROUND1_MARKER", json.dumps(second))
        self.assertIn("ROUND2_MARKER", json.dumps(third))
        self.assertNotIn("ROUND1_MARKER", json.dumps(third), "older failures are not carried forward")
        for context in (first, second, third):
            self.assertLessEqual(set(context), self.WHITELIST)
            self.assertLessEqual(len(json.dumps(context)), manager.MAX_CONTEXT_CHARS)
            self.assertNotIn("messages", context)
        store = self.reload()
        self.assertNotIn("recent_failure", manager.build_context(store, role="executor"))
        stored = json.loads((store.directory / "rounds/0003/context-executor.json").read_text())
        self.assertNotIn("ROUND1_MARKER", json.dumps(stored))
        self.assertEqual(manager.build_context(store, role="executor")["trusted_state"]["verified_steps"][0]["step_id"], "repair-2")

    def test_context_is_bounded_even_with_huge_failure_evidence(self):
        self.make(budgets={**LOOSE, "max_rounds": 1})
        self.run_loop(Planner([step()]), Executor([wrong(1)]))
        store = self.reload()
        manager_state = copy.deepcopy(store.state["manager"])
        manager_state["last_failure"]["detail"] = "x" * 100000
        store.state["manager"] = manager_state
        context = manager.build_context(store, role="planner", files=[f"file{i}.py" for i in range(500)])
        self.assertLessEqual(len(json.dumps(context)), manager.MAX_CONTEXT_CHARS)
        self.assertLessEqual(len(context.get("files", [])), 60)

    def test_hidden_reasoning_and_model_text_are_never_persisted(self):
        self.make(budgets=LOOSE, reviewer=True)
        raw = "<think>SECRET_THINK chain of thought</think>\n" + json.dumps(step(risk="high"))
        bad = json.dumps({**step(risk="high"), "rationale": "<think>SECRET_INLINE</think>"})
        planner = Planner([bad, raw, step(risk="high"), step(risk="high")])
        reviewer = Reviewer([(True, "<think>SECRET_FINDINGS</think> approved")])
        self.run_loop(planner, Executor([ADD_OK]), None, reviewer)
        state = self.state()
        self.assertEqual(state["status"], "COMPLETED")
        self.assertEqual(state["manager"]["rounds"][0]["reason"], "INVALID_MANAGER_OUTPUT")
        leaked = []
        for path in self.store.directory.rglob("*"):
            if path.is_file() and b"SECRET_" in path.read_bytes():
                leaked.append(path.name)
        self.assertEqual(leaked, [])
        store = self.reload()
        checkpoint = store.read_evidence(state["last_verified_checkpoint"]["reference"])
        self.assertIn("omitted", store.read_evidence(checkpoint["reviewer"])["findings"])

    def test_stall_fingerprint_changes_with_meaningful_progress_only(self):
        self.two(budgets={**LOOSE, "max_rounds": 1})
        before = manager.stall_fingerprint(self.reload().state)
        progress_before = manager.progress_fingerprint(self.reload().state)
        self.assertEqual(before, manager.stall_fingerprint(self.reload().state))
        self.run_loop(Planner([step("s1", checks=["check-1"])]), Executor([ADD_OK]))
        after = self.reload().state
        self.assertNotEqual(before, manager.stall_fingerprint(after))
        self.assertNotEqual(progress_before, manager.progress_fingerprint(after))

    def test_progress_fingerprint_is_unchanged_by_a_failed_round(self):
        self.make(budgets={**LOOSE, "max_rounds": 1})
        before = manager.progress_fingerprint(self.reload().state)
        self.run_loop(Planner([step()]), Executor([wrong(1)]))
        failed = self.reload().state
        self.assertEqual(before, manager.progress_fingerprint(failed))
        self.assertIsNotNone(failed["manager"]["last_failure"])

    def test_state_machine_rejects_illegal_transitions(self):
        self.make(budgets=LOOSE)
        loop = manager.ManagerLoop(self.store, None, None, self.gateway, None, None)
        with self.assertRaisesRegex(durable.DurableError, "Illegal round transition"):
            loop.enter("VERIFYING")
        self.assertEqual(set(manager.TRANSITIONS["READY"]), {"PLANNING"})
        self.assertNotIn("VERIFIED", manager.TRANSITIONS["VERIFYING"])
        self.assertNotIn("CHECKPOINTING", manager.TRANSITIONS["EXECUTING"])

    def test_round_phases_are_durable_and_visible(self):
        self.make(budgets=LOOSE)
        self.run_loop(Planner([step()]), Executor([ADD_OK]))
        phases = [r["state"]["manager"]["phase"] for r in self.reload().records if r["event"] == "state_committed"
                  and r["state"]["manager"]["active_round"] == 1]
        order = []
        for phase in phases:
            if not order or order[-1] != phase:
                order.append(phase)
        self.assertEqual(order, ["PLANNING", "EXECUTING", "VERIFYING", "CHECKPOINTING"])
        self.assertEqual(self.state()["manager"]["phase"], "VERIFIED")
        record = self.rounds()[0]
        self.assertEqual((record["implementation"], record["verification"], record["trusted"], record["outcome"]),
                         ("changed", "passed", True, "TRUSTED"))

    def test_corrupt_manager_state_is_refused(self):
        self.make(budgets=LOOSE)
        for mutate in (lambda m: m.update(phase="NOPE"), lambda m: m.update(criteria=[]),
                       lambda m: m["counters"].update(rounds_started=-1), lambda m: m["budgets"].pop("max_rounds")):
            broken = copy.deepcopy(self.store.state["manager"])
            mutate(broken)
            with self.subTest(), self.assertRaises(durable.DurableError):
                manager.validate(broken, self.store.state)


class CliAndCompatibilityTests(ManagerCase):
    def parse(self, *argv):
        out = io.StringIO()
        with patch.object(harness, "ROOT", self.root), redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = harness.main(["--config", str(self.config_file()), *argv])
        return code, out.getvalue()

    def config_file(self):
        path = self.root / "config.json"
        path.write_text(json.dumps({**self.config(), "base_url": "http://127.0.0.1:9292"}), encoding="utf-8")
        return path

    def test_dry_run_validates_and_prints_policy_without_models_or_state(self):
        with patch.object(harness, "Gateway", side_effect=AssertionError("models contacted")):
            code, out = self.parse("--critic", "autonomous-run", "--dry-run", "--repo", str(self.path), "--task", TASK,
                                   "--verify", "python -m unittest test_add", "--acceptance", "add works",
                                   "--max-rounds", "3", "--max-runtime-minutes", "5", "--forbid", "secrets/")
        summary = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual((summary["dry_run"], summary["models_contacted"], summary["critic_enabled"]), (True, False, True))
        self.assertEqual(summary["budgets"]["max_rounds"], 3)
        self.assertEqual(summary["budgets"]["max_runtime_seconds"], 300)
        self.assertEqual(summary["scope"]["forbidden_paths"], ["secrets/"])
        self.assertEqual(summary["criteria"][0]["checks"], ["check-1"])
        self.assertFalse((self.root / "runs" / "auto-run").exists())
        self.assertEqual([p.name for p in (self.root / "runs").glob("2*")] if (self.root / "runs").exists() else [], [])

    def test_dry_run_rejects_bad_inputs(self):
        code, _ = self.parse("autonomous-run", "--dry-run", "--repo", str(self.path), "--task", TASK,
                             "--verify", "rm -rf /", "--acceptance", "x")
        self.assertEqual(code, 1)
        code, _ = self.parse("autonomous-run", "--dry-run", "--repo", str(self.path), "--task", TASK,
                             "--verify", "python -m unittest test_add", "--acceptance", "x", "--max-rounds", "0")
        self.assertEqual(code, 1)

    def test_existing_critic_flag_contract_is_unchanged(self):
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            harness.main(["--critic", "run", "--repo", str(self.path), "--task", "t", "--verify", "python -m unittest"])
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            harness.main(["--critic", "status", "--run-id", "x"])

    def test_normal_run_and_long_run_commands_are_unchanged(self):
        parser_source = Path(harness.__file__).read_text(encoding="utf-8")
        for fragment in ('sub.add_parser("run"', 'sub.add_parser("long-run"', 'sub.add_parser("resume"',
                         'sub.add_parser("recover-model"'):
            self.assertIn(fragment, parser_source)
        import inspect
        self.assertEqual(list(inspect.signature(harness.Workflow.run).parameters),
                         ["self", "task", "commands", "reviewer", "critic"])
        repo = harness.Repository(self.path, self.config(), harness.EventLog(self.root / "e.jsonl"))
        (self.path / "calc.py").write_text(ADD_OK, encoding="utf-8")
        (self.path / "new.txt").write_text("new", encoding="utf-8")
        self.assertEqual(repo.diff(), repo.diff(None))
        only = repo.diff({"new.txt"})
        self.assertIn("new.txt", only)
        self.assertNotIn("calc.py", only)

    def test_plain_resume_refuses_manager_runs_and_status_reports_manager_state(self):
        self.make(budgets={**LOOSE, "max_rounds": 1})
        self.run_loop(Planner([step()]), Executor([wrong(1)]))
        args = argparse.Namespace(command="resume", run_id="auto-run", quiet=True, revalidate_unverified=False,
                                  revalidate_environment=False)
        with self.assertRaisesRegex(durable.DurableError, "autonomous-resume"):
            durable.cli(args, self.root)
        status = durable.cli(argparse.Namespace(command="status", run_id="auto-run"), self.root)
        self.assertEqual(status["resume_decision"], "SAFE")
        text = durable.concise_status(status)
        for fragment in ("RUN auto-run: BUDGET_EXHAUSTED", "MANAGER: phase READY", "ROUND 1: step s1 (low)",
                         "verify failed", "AC1: NOT_PROVEN", "STOPPED: BUDGET_EXHAUSTED / max_rounds",
                         "resumable with autonomous-resume"):
            self.assertIn(fragment, text)

    def test_status_shows_human_action_message(self):
        self.make(budgets=LOOSE)
        sup = supervision.Supervisor(self.store)
        sup.register("model_server", sys.executable, [], "delegated")
        sup.reconcile()
        self.run_loop(Forbidden("planner"), Forbidden("executor"))
        status = durable.cli(argparse.Namespace(command="status", run_id="auto-run"), self.root)
        text = durable.concise_status(status)
        self.assertIn("HUMAN ACTION:", text)
        self.assertIn("ambiguous", text)

    def test_recover_model_points_manager_runs_at_autonomous_resume(self):
        self.make(budgets=LOOSE)
        sup = supervision.Supervisor(self.store)
        child = sup.register("model_server", sys.executable, [], "delegated")
        sup.reconcile()
        args = argparse.Namespace(command="recover-model", run_id="auto-run", child_id=child, resolution="model_not_running",
                                  reason="Operator inspected the gateway and no model exists", confirm=True, quiet=True)
        with patch.object(durable.Gateway, "running", return_value=[]), patch("durable.winprocess.servers", return_value=[]):
            result = durable.cli(args, self.root)
        self.assertTrue(result["next"].startswith("autonomous-resume"))

    def test_budget_argument_parsing(self):
        args = argparse.Namespace(max_rounds=4, max_runtime_minutes=2.5, max_step_retries=None, max_consecutive_failures=None,
                                  max_stall_rounds=1, round_timeout_minutes=None, max_model_invocations=["review=1", "code=9"])
        value = manager.budget_args(args)
        self.assertEqual((value["max_rounds"], value["max_runtime_seconds"], value["max_stall_rounds"]), (4, 150.0, 1))
        self.assertEqual(value["max_model_invocations"], {"review": 1, "code": 9})
        args.max_model_invocations = ["gpu=1"]
        with self.assertRaises(ValueError):
            manager.budget_args(args)

    def test_acceptance_parsing(self):
        checks = manager.build_checks(TWO)
        parsed = manager.parse_criteria(["plain", '{"text": "only two", "checks": [2], "id": "X"}'], checks)
        self.assertEqual([(c["id"], c["checks"]) for c in parsed], [("AC1", ["check-1", "check-2"]), ("X", ["check-2"])])
        for bad in ([], ['{"text": ""}'], ['{"text": "a", "checks": ["check-9"]}'], ['{"text": "a", "extra": 1}'],
                    ['{"text": "a", "id": "D"}', '{"text": "b", "id": "D"}']):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                manager.parse_criteria(bad, checks)


class ExplicitThreeModelTests(ManagerCase):
    def required(self, **kwargs):
        return self.make(critic=True, reviewer=True, review_policy="three-model",
                         budgets=kwargs.pop("budgets", {**LOOSE, "max_rounds": 1}), **kwargs)

    def test_low_risk_explicit_policy_runs_all_gates_in_order(self):
        self.required()
        gateway = self.gateway
        planner = TierPlanner([step()], gateway)
        self.run_loop(planner, Executor([ADD_OK], gateway, "code"),
                      Critic([clean_critic()], gateway, "critic"),
                      Reviewer([(True, "ok")], gateway, "review"))
        store = self.reload()
        record = store.state["manager"]["rounds"][0]
        self.assertEqual(store.state["status"], "COMPLETED")
        self.assertEqual((record["risk"], record["critic"], record["reviewer"], record["trusted"]),
                         ("low", "completed", "approved", True))
        self.assertEqual(planner.tiers(), ["executor"])
        self.assertEqual([r for r in gateway.events if r in manager.ROLES], ["code", "code", "critic", "review"])
        checkpoint = store.read_evidence(record["evidence"]["checkpoint"])
        first, final = [store.read_evidence(ref) for ref in checkpoint["verification"]]
        self.assertTrue(store.read_evidence(checkpoint["critic"])["advisory_only"])
        reviewer = store.read_evidence(checkpoint["reviewer"])
        self.assertLessEqual(first["time"], reviewer["time"])
        self.assertLessEqual(reviewer["time"], final["time"])
        phases = [r["state"]["manager"]["phase"] for r in store.records if r["event"] == "state_committed"
                  and r["state"]["manager"]["active_round"] == 1]
        order = []
        for phase in phases:
            if not order or order[-1] != phase:
                order.append(phase)
        self.assertEqual(order, ["PLANNING", "EXECUTING", "VERIFYING", "CRITIQUING", "REVIEWING",
                                 "VERIFYING", "CHECKPOINTING"])
        self.assertTrue(first["passed"] and final["passed"])
        self.assertIsNone(gateway.loaded)

    def test_explicit_gates_do_not_lower_high_risk_planning(self):
        policy = manager.model_policy(step(risk="high"), critic_enabled=True, reviewer_enabled=True,
                                      review_policy="three-model")
        self.assertEqual((policy["risk"], policy["planner"], policy["critic"], policy["reviewer"]),
                         ("high", "reviewer", "run", "required"))

    def test_initial_verifier_failure_prevents_both_model_gates(self):
        self.required()
        self.run_loop(Planner([step()]), Executor([wrong(1)]))
        self.assertEqual(self.rounds()[0]["reason"], "VERIFICATION_FAILED")
        self.assertIsNone(self.state()["last_verified_checkpoint"])

    def test_reviewer_approval_cannot_override_final_verifier_failure(self):
        self.required()
        real = durable.DurableRepository.execute
        calls = []
        def flaky(repo, argv, timeout=None):
            if argv == CHECK1:
                calls.append(argv)
                if len(calls) == 2:
                    argv = ["python", "-m", "unittest", "missing_module"]
            return real(repo, argv, timeout)
        with patch.object(durable.DurableRepository, "execute", flaky):
            self.run_loop(Planner([step()]), Executor([ADD_OK]), Critic([clean_critic()]),
                          Reviewer([(True, "Approved")]))
        record = self.rounds()[0]
        self.assertEqual((record["reviewer"], record["reason"], record["trusted"]),
                         ("approved", "FINAL_VERIFICATION_FAILED", False))
        self.assertIsNone(self.state()["last_verified_checkpoint"])

    def test_missing_required_critic_evidence_cannot_checkpoint(self):
        self.required()
        real = manager.ManagerLoop.checkpoint
        def missing(loop, number, step_, policy, commands, verifications, critic_record, critic_ref,
                    reviewer_record, reviewer_ref, paths):
            return real(loop, number, step_, policy, commands, verifications, critic_record, None,
                        reviewer_record, reviewer_ref, paths)
        with patch.object(manager.ManagerLoop, "checkpoint", missing):
            self.run_loop(Planner([step()]), Executor([ADD_OK]), Critic([clean_critic()]),
                          Reviewer([(True, "Approved")]))
        self.assertEqual(self.rounds()[0]["reason"], "GATES_REFUSED")
        self.assertIsNone(self.state()["last_verified_checkpoint"])

    def test_review_policy_and_timeout_stay_pinned_on_resume(self):
        config = {**self.config(), "verification_timeout_seconds": 900}
        with patch.object(self, "config", return_value=config):
            self.required(budgets=LOOSE)
        self.kill(Planner([step()]), Executor([ADD_OK]), Critic([clean_critic()]),
                  Reviewer([SimulatedKill()]))
        report = self.resume(critic=Critic([clean_critic()]), reviewer=Reviewer([(True, "ok")]))
        state = self.state()
        self.assertEqual(report["status"], "COMPLETED")
        self.assertEqual(state["manager"]["review_policy"], "three-model")
        self.assertEqual(state["options"]["config"]["verification_timeout_seconds"], 900)
        self.assertEqual((self.rounds()[-1]["kind"], self.rounds()[-1]["policy"]["critic"],
                          self.rounds()[-1]["policy"]["reviewer"]), ("verify_only", "run", "required"))

    def test_unavailable_required_critic_cannot_satisfy_three_model_policy(self):
        self.required()
        self.run_loop(Planner([step()]), Executor([ADD_OK]), Critic([{"status": "unavailable"}]),
                      Reviewer([(True, "Approved")]))
        self.assertEqual((self.rounds()[0]["critic"], self.rounds()[0]["reviewer"], self.rounds()[0]["reason"]),
                         ("unavailable", "approved", "GATES_REFUSED"))
        self.assertIsNone(self.state()["last_verified_checkpoint"])

    def test_invalid_actions_become_bounded_untrusted_executor_failure(self):
        self.make(budgets={**LOOSE, "max_rounds": 1})
        gateway = FakeGateway()
        gateway.chat = lambda *args, **kwargs: '{}'
        calls = []
        def agents(repo, gateway_, config, log):
            result = manager.default_agents(repo, gateway_, {**config, "max_actions": 32}, log)
            result.planner = Planner([step()])
            original = gateway_.chat
            def chat(*args, **kwargs):
                calls.append(1)
                return original(*args, **kwargs)
            gateway_.chat = chat
            return result
        manager.execute(self.store, agents=agents, gateway=gateway)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.rounds()[0]["reason"], "EXECUTOR_ERROR")
        self.assertFalse(self.rounds()[0]["trusted"])
        self.assertIsNone(self.state()["last_verified_checkpoint"])


class ExplicitPolicyCliTests(ManagerCase):
    parse = CliAndCompatibilityTests.parse
    config_file = CliAndCompatibilityTests.config_file

    def test_invalid_command_deadlines_are_rejected_before_models(self):
        for timeout in ("0", "-1", "86401"):
            code, _ = self.parse("autonomous-run", "--verification-timeout-seconds", timeout,
                                 "--dry-run", "--repo", str(self.path), "--task", TASK,
                                 "--verify", "python -m pytest -q", "--acceptance", "passes")
            self.assertEqual(code, 1)
    def test_explicit_policy_dry_run_and_command_deadline(self):
        code, out = self.parse("--critic", "autonomous-run", "--reviewer", "--review-policy", "three-model",
                               "--verification-timeout-seconds", "900", "--dry-run", "--repo", str(self.path),
                               "--task", TASK, "--verify", "python -m pytest -q", "--acceptance", "passes")
        self.assertEqual(code, 0)
        summary = json.loads(out)
        self.assertEqual(summary["required_gates"], ["critic", "reviewer"])
        self.assertEqual(summary["verification_timeout_seconds"], 900)
        self.assertNotIn("no critic", summary["policy"]["low"])

    def test_explicit_policy_requires_both_enabled_roles(self):
        for flags in ([], ["--critic"], ["--reviewer"]):
            global_flags = ["--critic"] if "--critic" in flags else []
            run_flags = ["--reviewer"] if "--reviewer" in flags else []
            code, _ = self.parse(*global_flags, "autonomous-run", *run_flags, "--review-policy", "three-model",
                                 "--dry-run", "--repo", str(self.path), "--task", TASK,
                                 "--verify", "python -m pytest -q", "--acceptance", "passes")
            self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
