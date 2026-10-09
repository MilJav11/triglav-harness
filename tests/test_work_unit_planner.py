"""Deterministic tests for Wave 3 WorkUnit architecture.

Covers all 40 required scenarios from the Wave 3 specification:
1. valid single-unit plan
2. valid sequential two-unit plan
3. valid branching DAG
4. deterministic topological ordering
5. canonical IDs controller-assigned
6. planner aliases translated correctly
7. empty plan rejected
8. >8 units rejected
9. duplicate proposal alias rejected
10. unknown dependency rejected
11. self-dependency rejected
12. cycle rejected
13. unsupported mode rejected
14. scope expansion rejected
15. traversal/absolute/drive path rejected
16. unknown verifier rejected
17. planner cannot redefine verifier command
18. inadequate verifier coverage rejected
19. planner cannot set successful evidence
20. planner cannot set trust_level
21. planner cannot set canonical result/status
22. planner-supplied budget expansion ignored/rejected
23. one schema correction succeeds
24. second schema correction impossible
25. one allowed replan succeeds
26. second replan impossible
27. execution failure does NOT trigger planner replan
28. planner timeout consumes budget
29. retry does not reset runtime budget
30. crash/reload preserves planning attempt count
31. unvalidated persisted proposal cannot execute
32. accepted plan survives reload unchanged
33. accepted canonical IDs survive reload unchanged
34. accepted sequence survives reload unchanged
35. planner cannot alter accepted plan after execution begins
36. existing fixed Wave 2 execution works without planner
37. legacy Manager path works without planner
38. end-to-end high-level task → plan → Wave 2 ScriptedExecutor → MILESTONE_READY
39. end-to-end result leaves all top-level trusted fields unchanged
40. environment/source fingerprint covers any new planning controller module
"""

from __future__ import annotations

import copy
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import durable
import environment
import harness
import manager
import work_units
from work_units import WorkUnitError
from work_unit_scheduler import (
    WorkUnitScheduler,
    ScriptedExecutor,
    MILESTONE_READY,
    MILESTONE_FAILED,
)
import work_unit_planner as wup
from work_unit_planner import (
    PlannerError,
    PlannerTimeout,
    PlannerBudgetExhausted,
    ScriptedPlanner,
    PlanningController,
    build_planner_context,
    validate_proposal_schema,
    validate_and_build_canonical_plan,
    validate_canonical_plan,
    load_planning_state,
    persist_planning_state,
    build_work_units_section_from_plan,
    MAX_PLAN_UNITS,
)
from test_harness import CONFIG


TASK = "Wave 3 WorkUnit high-level planning task"
PARENT_SCOPE = {"allowed_paths": ["calculator.py", "helper.py"], "forbidden_paths": ["secret.py"]}
VERIFIER_REGISTRY = {
    "check-calc": {
        "argv": ["python", "-m", "unittest", "test_calculator.py"],
        "description": "Run calculator unit tests",
        "modes": ["mutation", "read_only"],
        "paths": ["calculator.py"],
        "capabilities": ["calculator.correctness"],
    },
    "check-helper": {
        "argv": ["python", "-m", "unittest", "test_helper.py"],
        "description": "Run helper unit tests",
        "modes": ["mutation", "read_only"],
        "paths": ["helper.py"],
        "capabilities": ["helper.correctness"],
    },
}

DEFAULT_TEST_CAPABILITY_POLICY = {
    "paths": {
        "calculator.py": ["calculator.correctness"],
        "helper.py": ["helper.correctness"],
    },
}


class AdvancingClock:
    def __init__(self, start=0.0):
        self.time = start

    def __call__(self):
        return self.time

    def advance(self, dt):
        self.time += dt


_OMIT = object()


def make_proposal_unit(
    alias="u1",
    objective="Implement multiply in calculator",
    dependencies=None,
    mode="mutation",
    paths=None,
    forbidden=None,
    verifier_ids=None,
    purpose=None,
    purpose_id=None,
    required_capabilities=None,
    semantic_coverage_required=None,
):
    unit = {
        "alias": alias,
        "objective": objective,
        "dependencies": dependencies if dependencies is not None else [],
        "mode": mode,
        "scope": {
            "allowed_paths": paths if paths is not None else ["calculator.py"],
            "forbidden_paths": forbidden if forbidden is not None else [],
        },
        "verifier_ids": verifier_ids if verifier_ids is not None else ["check-calc"],
    }
    if purpose is not None:
        unit["purpose"] = purpose
    if purpose_id is not None:
        unit["purpose_id"] = purpose_id
    if required_capabilities is not None:
        unit["required_capabilities"] = required_capabilities
    if semantic_coverage_required is not None:
        unit["semantic_coverage_required"] = semantic_coverage_required
    return unit


def fixture(root, run_id="wave3-test-run", capability_policy=_OMIT):
    """Create a git repo, Store, and Repository for testing."""
    repo_path = root / "repo"
    if not repo_path.exists():
        repo_path.mkdir(parents=True)
        (repo_path / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
        (repo_path / "calculator.py").write_text("def multiply(a, b):\n    return 0\n", encoding="utf-8")
        (repo_path / "helper.py").write_text("def helper_fn():\n    return 42\n", encoding="utf-8")
        (repo_path / "test_calculator.py").write_text(
            "import unittest\nfrom calculator import multiply\n"
            "class T(unittest.TestCase):\n"
            "    def test_product(self): self.assertEqual(multiply(2, 3), 6)\n"
            "if __name__ == '__main__': unittest.main()\n",
            encoding="utf-8",
        )
        (repo_path / "test_helper.py").write_text(
            "import unittest\nfrom helper import helper_fn\n"
            "class T(unittest.TestCase):\n"
            "    def test_h(self): self.assertEqual(helper_fn(), 42)\n"
            "if __name__ == '__main__': unittest.main()\n",
            encoding="utf-8",
        )

        def git(*args):
            return subprocess.run(
                ["git", "-C", str(repo_path), "-c", f"safe.directory={repo_path}",
                 "-c", "user.name=W3 test", "-c", "user.email=test@localhost", *args],
                check=True, capture_output=True,
            )
        git("init", "-q")
        git("add", ".")
        git("commit", "-qm", "baseline")

    cap = DEFAULT_TEST_CAPABILITY_POLICY if capability_policy is _OMIT else capability_policy
    config = copy.deepcopy(CONFIG)
    config.update(max_retries=0)
    log = harness.EventLog(root / "preflight.jsonl")
    repo = harness.Repository(repo_path, config, log)
    store = durable.Store(root / "runs", run_id)
    commands = [["python", "-m", "unittest", "test_calculator.py"]]
    opts = {"verifier_registry": VERIFIER_REGISTRY}
    if cap is not None:
        opts["capability_policy"] = cap
    store.create(repo, TASK, commands, config, extra=opts)
    return repo, store


class TestWave3PureValidation(unittest.TestCase):
    """Scenarios 1-22: Pure deterministic validation of proposals and authority."""

    def validate_plan(self, proposal, **kwargs):
        kwargs.setdefault("parent_scope", PARENT_SCOPE)
        kwargs.setdefault("approved_verifiers", VERIFIER_REGISTRY)
        kwargs.setdefault("capability_policy", DEFAULT_TEST_CAPABILITY_POLICY)
        return validate_and_build_canonical_plan(proposal, **kwargs)

    def test_01_valid_single_unit_plan(self):
        """Scenario 1: valid single-unit plan accepted with canonical ID wu-001."""
        proposal = [make_proposal_unit(alias="u1")]
        specs, seq = self.validate_plan(proposal)
        self.assertEqual(len(specs), 1)
        self.assertEqual(seq, ["wu-001"])
        self.assertEqual(specs[0]["unit_id"], "wu-001")
        self.assertEqual(specs[0]["dependencies"], [])
        self.assertEqual(specs[0]["mode"], "mutation")

    def test_02_valid_sequential_two_unit_plan(self):
        """Scenario 2: valid sequential two-unit plan; dependencies translated."""
        proposal = [
            make_proposal_unit(alias="step_a", paths=["calculator.py"]),
            make_proposal_unit(alias="step_b", dependencies=["step_a"], paths=["helper.py"], verifier_ids=["check-helper"]),
        ]
        specs, seq = self.validate_plan(proposal)
        self.assertEqual(seq, ["wu-001", "wu-002"])
        self.assertEqual(specs[0]["unit_id"], "wu-001")
        self.assertEqual(specs[0]["dependencies"], [])
        self.assertEqual(specs[1]["unit_id"], "wu-002")
        self.assertEqual(specs[1]["dependencies"], ["wu-001"])

    def test_03_valid_branching_dag(self):
        """Scenario 3: valid branching DAG (diamond)."""
        proposal = [
            make_proposal_unit(alias="root"),
            make_proposal_unit(alias="left", dependencies=["root"]),
            make_proposal_unit(alias="right", dependencies=["root"]),
            make_proposal_unit(alias="join", dependencies=["left", "right"]),
        ]
        specs, seq = self.validate_plan(proposal)
        self.assertEqual(len(seq), 4)
        self.assertEqual(specs[0]["unit_id"], "wu-001")
        self.assertEqual(specs[3]["unit_id"], "wu-004")
        self.assertIn("wu-001", specs[1]["dependencies"])
        self.assertIn("wu-001", specs[2]["dependencies"])
        self.assertIn(specs[1]["unit_id"], specs[3]["dependencies"])
        self.assertIn(specs[2]["unit_id"], specs[3]["dependencies"])

    def test_04_deterministic_topological_ordering(self):
        """Scenario 4: deterministic topological ordering using proposal order as stable tie-break."""
        # Two independent units: proposal order should dictate execution sequence stably
        p1 = [make_proposal_unit(alias="b"), make_proposal_unit(alias="a")]
        _, seq1 = self.validate_plan(p1)

        p2 = [make_proposal_unit(alias="a"), make_proposal_unit(alias="b")]
        _, seq2 = self.validate_plan(p2)

        # In both, canonical sequence is ["wu-001", "wu-002"]
        self.assertEqual(seq1, ["wu-001", "wu-002"])
        self.assertEqual(seq2, ["wu-001", "wu-002"])

        # Running p1 multiple times produces exact same result
        for _ in range(5):
            _, seq_repeat = self.validate_plan(p1)
            self.assertEqual(seq_repeat, seq1)

    def test_05_canonical_ids_controller_assigned(self):
        """Scenario 5: canonical IDs are controller-assigned; planner aliases are ignored."""
        proposal = [
            make_proposal_unit(alias="custom-id-999"),
            make_proposal_unit(alias="wu-888", dependencies=["custom-id-999"]),
        ]
        specs, seq = self.validate_plan(proposal)
        self.assertEqual(seq, ["wu-001", "wu-002"])
        self.assertEqual(specs[0]["unit_id"], "wu-001")
        self.assertEqual(specs[1]["unit_id"], "wu-002")

    def test_06_planner_aliases_translated_correctly(self):
        """Scenario 6: planner aliases in dependencies are translated to canonical controller IDs."""
        proposal = [
            make_proposal_unit(alias="first"),
            make_proposal_unit(alias="second", dependencies=["first"]),
        ]
        specs, _ = self.validate_plan(proposal)
        self.assertEqual(specs[1]["dependencies"], ["wu-001"])

    def test_07_empty_plan_rejected(self):
        """Scenario 7: empty plan is rejected fail-closed."""
        with self.assertRaises(PlannerError) as ctx:
            validate_proposal_schema({"units": []})
        self.assertEqual(ctx.exception.code, "empty_plan")

    def test_08_over_8_units_rejected(self):
        """Scenario 8: >8 units rejected; no silent truncation."""
        proposal = {"units": [make_proposal_unit(alias=f"u{i}") for i in range(9)]}
        with self.assertRaises(PlannerError) as ctx:
            validate_proposal_schema(proposal)
        self.assertEqual(ctx.exception.code, "too_many_units")

    def test_09_duplicate_proposal_alias_rejected(self):
        """Scenario 9: duplicate proposal alias is rejected."""
        proposal = [make_proposal_unit(alias="dup"), make_proposal_unit(alias="dup")]
        with self.assertRaises(PlannerError) as ctx:
            self.validate_plan(proposal)
        self.assertEqual(ctx.exception.code, "duplicate_alias")

    def test_10_unknown_dependency_rejected(self):
        """Scenario 10: unknown dependency alias is rejected."""
        proposal = [make_proposal_unit(alias="u1", dependencies=["ghost"])]
        with self.assertRaises(PlannerError) as ctx:
            self.validate_plan(proposal)
        self.assertEqual(ctx.exception.code, "unknown_dependency")

    def test_11_self_dependency_rejected(self):
        """Scenario 11: self-dependency is rejected."""
        proposal = [make_proposal_unit(alias="u1", dependencies=["u1"])]
        with self.assertRaises(PlannerError) as ctx:
            self.validate_plan(proposal)
        self.assertEqual(ctx.exception.code, "self_dependency")

    def test_12_cycle_rejected(self):
        """Scenario 12: dependency cycle is rejected."""
        proposal = [
            make_proposal_unit(alias="u1", dependencies=["u2"]),
            make_proposal_unit(alias="u2", dependencies=["u1"]),
        ]
        with self.assertRaises(PlannerError) as ctx:
            self.validate_plan(proposal)
        self.assertEqual(ctx.exception.code, "cycle_detected")

    def test_13_unsupported_mode_rejected(self):
        """Scenario 13: unsupported mode is rejected."""
        proposal = [make_proposal_unit(alias="u1", mode="dangerous_eval")]
        with self.assertRaises(PlannerError) as ctx:
            self.validate_plan(proposal)
        self.assertEqual(ctx.exception.code, "unsupported_mode")

    def test_14_scope_expansion_rejected(self):
        """Scenario 14: scope expansion outside parent scope is rejected."""
        proposal = [make_proposal_unit(alias="u1", paths=["unauthorized_file.py"])]
        with self.assertRaises(PlannerError) as ctx:
            self.validate_plan(proposal)
        self.assertEqual(ctx.exception.code, "scope_outside_parent")

        # Overlapping parent forbidden path
        proposal_forbidden = [make_proposal_unit(alias="u1", paths=["secret.py"])]
        with self.assertRaises(PlannerError) as ctx:
            self.validate_plan(proposal_forbidden)
        self.assertEqual(ctx.exception.code, "scope_outside_parent")

    def test_15_traversal_absolute_drive_path_rejected(self):
        """Scenario 15: traversal/absolute/drive path in scope is rejected."""
        bad_paths = ["../escape.py", "/etc/passwd", "C:/Windows/cmd.exe", "//share/file.txt"]
        for p in bad_paths:
            with self.subTest(path=p):
                proposal = [make_proposal_unit(alias="u1", paths=[p])]
                with self.assertRaises((PlannerError, WorkUnitError)) as ctx:
                    self.validate_plan(proposal)
                if isinstance(ctx.exception, PlannerError):
                    self.assertIn(ctx.exception.code, ("scope_absolute_or_traversal", "scope_outside_parent"))

    def test_16_unknown_verifier_rejected(self):
        """Scenario 16: unknown verifier ID is rejected."""
        proposal = [make_proposal_unit(alias="u1", verifier_ids=["hack_verifier"])]
        with self.assertRaises(PlannerError) as ctx:
            self.validate_plan(proposal)
        self.assertEqual(ctx.exception.code, "unknown_verifier")

    def test_17_planner_cannot_redefine_verifier_command(self):
        """Scenario 17: planner cannot supply or redefine verifier commands."""
        # Extra fields in proposal unit are rejected by schema validation
        bad_unit = make_proposal_unit(alias="u1")
        bad_unit["command"] = ["rm", "-rf", "/"]
        with self.assertRaises(PlannerError) as ctx:
            validate_proposal_schema({"units": [bad_unit]})
        self.assertEqual(ctx.exception.code, "malformed_schema")

    def test_18_inadequate_verifier_coverage_rejected(self):
        """Scenario 18: inadequate verifier coverage (empty verifier_ids) rejected."""
        proposal = [make_proposal_unit(alias="u1", verifier_ids=[])]
        with self.assertRaises(PlannerError) as ctx:
            self.validate_plan(proposal)
        self.assertEqual(ctx.exception.code, "verifier_coverage_missing")

    def test_19_planner_cannot_set_successful_evidence(self):
        """Scenario 19: planner cannot forge successful verifier evidence."""
        bad_unit = make_proposal_unit(alias="u1")
        bad_unit["verifier_evidence"] = {"passed": True}
        with self.assertRaises(PlannerError) as ctx:
            validate_proposal_schema({"units": [bad_unit]})
        self.assertEqual(ctx.exception.code, "malformed_schema")

        # Canonical specs created by controller always have empty evidence/result
        specs, _ = self.validate_plan([make_proposal_unit()])
        self.assertEqual(specs[0]["evidence_inputs"], [])

    def test_20_planner_cannot_set_trust_level(self):
        """Scenario 20: planner cannot set trust_level."""
        bad_unit = make_proposal_unit(alias="u1")
        bad_unit["trust_level"] = "TRUSTED"
        with self.assertRaises(PlannerError) as ctx:
            validate_proposal_schema({"units": [bad_unit]})
        self.assertEqual(ctx.exception.code, "malformed_schema")

    def test_21_planner_cannot_set_canonical_result_or_status(self):
        """Scenario 21: planner cannot set result or status."""
        bad_unit = make_proposal_unit(alias="u1")
        bad_unit["status"] = "UNIT_VERIFIED"
        bad_unit["result"] = {"passed": True}
        with self.assertRaises(PlannerError) as ctx:
            validate_proposal_schema({"units": [bad_unit]})
        self.assertEqual(ctx.exception.code, "malformed_schema")

    def test_22_planner_supplied_budget_expansion_ignored_or_rejected(self):
        """Scenario 22: planner cannot supply or expand budget limits."""
        bad_unit = make_proposal_unit(alias="u1")
        bad_unit["limits"] = {"timeout_seconds": 999999, "max_attempts": 100}
        with self.assertRaises(PlannerError) as ctx:
            validate_proposal_schema({"units": [bad_unit]})
        self.assertEqual(ctx.exception.code, "malformed_schema")

        # Controller limits are conservative defaults
        specs, _ = self.validate_plan([make_proposal_unit()])
        self.assertEqual(specs[0]["limits"]["max_attempts"], 2)


class TestWave3PlannerController(unittest.TestCase):
    """Scenarios 23-35: Planning controller retry bounds, timeout, durable state, immutability."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.repo, self.store = fixture(self.root)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_23_one_schema_correction_succeeds(self):
        """Scenario 23: first response is malformed schema; one schema correction succeeds."""
        malformed = {"invalid_key": "not_a_plan"}
        valid = {"units": [make_proposal_unit(alias="u1")]}
        planner = ScriptedPlanner(behaviors=[malformed, valid])

        controller = PlanningController(
            self.store,
            planner,
            parent_scope=PARENT_SCOPE,
            approved_verifiers=VERIFIER_REGISTRY,
            task=TASK,
        )
        specs, seq = controller.run()
        self.assertEqual(seq, ["wu-001"])
        self.assertEqual(planner.call_count, 2)

        # Durable state records that schema correction was consumed
        section = load_planning_state(self.store.state)
        self.assertTrue(section["schema_correction_used"])
        self.assertEqual(section["phase"], "PLAN_ACCEPTED")

    def test_24_second_schema_correction_impossible(self):
        """Scenario 24: second schema correction is impossible (only 1 allowed)."""
        malformed1 = {"bad": 1}
        malformed2 = {"bad": 2}
        planner = ScriptedPlanner(behaviors=[malformed1, malformed2, {"units": [make_proposal_unit()]}])

        controller = PlanningController(
            self.store,
            planner,
            parent_scope=PARENT_SCOPE,
            approved_verifiers=VERIFIER_REGISTRY,
            task=TASK,
        )
        with self.assertRaises(PlannerError) as ctx:
            controller.run()
        self.assertEqual(ctx.exception.code, "second_schema_correction_refused")
        self.assertEqual(planner.call_count, 2)

    def test_25_one_allowed_replan_succeeds(self):
        """Scenario 25: first proposal has decomposition defect (cycle); one replan succeeds."""
        cycle_proposal = {
            "units": [
                make_proposal_unit(alias="u1", dependencies=["u2"]),
                make_proposal_unit(alias="u2", dependencies=["u1"]),
            ]
        }
        valid_proposal = {"units": [make_proposal_unit(alias="u1")]}
        planner = ScriptedPlanner(behaviors=[cycle_proposal, valid_proposal])

        controller = PlanningController(
            self.store,
            planner,
            parent_scope=PARENT_SCOPE,
            approved_verifiers=VERIFIER_REGISTRY,
            task=TASK,
        )
        specs, seq = controller.run()
        self.assertEqual(seq, ["wu-001"])
        self.assertEqual(planner.call_count, 2)

        section = load_planning_state(self.store.state)
        self.assertTrue(section["replan_used"])
        self.assertEqual(section["phase"], "PLAN_ACCEPTED")

    def test_26_second_replan_impossible(self):
        """Scenario 26: second replan is impossible (only 1 replan allowed)."""
        cycle1 = {
            "units": [
                make_proposal_unit(alias="u1", dependencies=["u2"]),
                make_proposal_unit(alias="u2", dependencies=["u1"]),
            ]
        }
        cycle2 = {
            "units": [
                make_proposal_unit(alias="a", dependencies=["b"]),
                make_proposal_unit(alias="b", dependencies=["a"]),
            ]
        }
        planner = ScriptedPlanner(behaviors=[cycle1, cycle2, {"units": [make_proposal_unit()]}])

        controller = PlanningController(
            self.store,
            planner,
            parent_scope=PARENT_SCOPE,
            approved_verifiers=VERIFIER_REGISTRY,
            task=TASK,
        )
        with self.assertRaises(PlannerError) as ctx:
            controller.run()
        self.assertEqual(ctx.exception.code, "second_replan_refused")
        self.assertEqual(planner.call_count, 2)

    def test_27_execution_failure_does_not_trigger_planner_replan(self):
        """Scenario 27: WorkUnit execution failure does NOT trigger planner replan."""
        proposal = {"units": [make_proposal_unit(alias="u1")]}
        planner = ScriptedPlanner(behaviors=[proposal])

        controller = PlanningController(
            self.store,
            planner,
            parent_scope=PARENT_SCOPE,
            approved_verifiers=VERIFIER_REGISTRY,
            task=TASK,
        )
        canonical_specs, canonical_seq = controller.run()
        self.assertEqual(planner.call_count, 1)

        # Build Wave 2 work_units section
        wu_section = build_work_units_section_from_plan(canonical_specs, canonical_seq)
        self.store.commit(work_units=wu_section)

        # Run scheduler with executor that fails
        executor = ScriptedExecutor(default_behavior="failure")
        agents = type("Agents", (), {"executor": executor})()
        scheduler = WorkUnitScheduler(
            self.store,
            self.repo,
            verifier_registry=VERIFIER_REGISTRY,
            agents=agents,
            parent_scope=PARENT_SCOPE,
        )
        res = scheduler.run_sequence()
        self.assertEqual(res["status"], MILESTONE_FAILED)

        # Planner was NEVER called to escape the execution failure
        self.assertEqual(planner.call_count, 1)

    def test_28_planner_timeout_consumes_budget(self):
        """Scenario 28: planner timeout consumes budget and fails closed."""
        planner = ScriptedPlanner(behaviors=[PlannerTimeout("Model took too long")])
        controller = PlanningController(
            self.store,
            planner,
            parent_scope=PARENT_SCOPE,
            approved_verifiers=VERIFIER_REGISTRY,
            task=TASK,
        )
        with self.assertRaises(PlannerTimeout):
            controller.run()

        section = load_planning_state(self.store.state)
        self.assertEqual(section["phase"], "PLAN_REJECTED")
        self.assertEqual(section["rejection_reason"], "planner_timeout")

    def test_29_retry_does_not_reset_runtime_budget(self):
        """Scenario 29: retrying schema correction does not grant fresh budget."""
        # Simulated tight budget of 1 second; each planner step advances clock by 0.6s
        current_time = [100.0]
        def advance_clock():
            t = current_time[0]
            current_time[0] += 0.6
            return t

        malformed = {"bad": 1}
        planner = ScriptedPlanner(behaviors=[malformed, {"units": [make_proposal_unit()]}])
        controller = PlanningController(
            self.store,
            planner,
            parent_scope=PARENT_SCOPE,
            approved_verifiers=VERIFIER_REGISTRY,
            task=TASK,
            budgets={"max_runtime_seconds": 1.0},
            clock=advance_clock,
        )
        with self.assertRaises(PlannerBudgetExhausted):
            controller.run()

    def test_30_crash_reload_preserves_planning_attempt_count(self):
        """Scenario 30: crash/reload preserves schema_correction_used and attempt count."""
        malformed = {"bad": 1}
        planner1 = ScriptedPlanner(behaviors=[malformed, {"bad": 2}])
        controller1 = PlanningController(
            self.store,
            planner1,
            parent_scope=PARENT_SCOPE,
            approved_verifiers=VERIFIER_REGISTRY,
            task=TASK,
        )
        try:
            controller1.run()
        except PlannerError:
            pass

        # Reload store from disk
        store2 = durable.Store(self.root / "runs", "wave3-test-run")
        store2.load()
        section2 = load_planning_state(store2.state)
        self.assertTrue(section2["schema_correction_used"])
        self.assertGreaterEqual(len(section2["attempts"]), 1)

    def test_31_unvalidated_persisted_proposal_cannot_execute(self):
        """Scenario 31: unvalidated proposal cannot execute as Wave 2 work units."""
        from work_unit_scheduler import WorkUnitSchedulingError
        section = wup._initial_planning_state(TASK)
        section["phase"] = "PROPOSAL_RECEIVED"
        section["current_proposal"] = {"units": [make_proposal_unit()]}
        persist_planning_state(section, self.store)

        # Scheduler must refuse to run if work_units section is absent (unvalidated proposal cannot execute)
        scheduler = WorkUnitScheduler(self.store, self.repo, verifier_registry=VERIFIER_REGISTRY)
        with self.assertRaises(WorkUnitSchedulingError):
            scheduler.run_sequence()

    def test_32_accepted_plan_survives_reload_unchanged(self):
        """Scenario 32: accepted plan survives Store reload unchanged."""
        planner = ScriptedPlanner(behaviors=[{"units": [make_proposal_unit(alias="u1")]}])
        controller = PlanningController(self.store, planner, parent_scope=PARENT_SCOPE, approved_verifiers=VERIFIER_REGISTRY, task=TASK)
        specs1, seq1 = controller.run()

        # Reload store
        store2 = durable.Store(self.root / "runs", "wave3-test-run")
        store2.load()
        section2 = load_planning_state(store2.state)
        self.assertEqual(section2["accepted_plan"], specs1)
        self.assertEqual(section2["accepted_sequence"], seq1)

    def test_33_accepted_canonical_ids_survive_reload_unchanged(self):
        """Scenario 33: accepted canonical IDs survive reload unchanged."""
        planner = ScriptedPlanner(behaviors=[{"units": [make_proposal_unit(alias="step_a"), make_proposal_unit(alias="step_b", dependencies=["step_a"])]}])
        controller = PlanningController(self.store, planner, parent_scope=PARENT_SCOPE, approved_verifiers=VERIFIER_REGISTRY, task=TASK)
        _, seq1 = controller.run()
        self.assertEqual(seq1, ["wu-001", "wu-002"])

        store2 = durable.Store(self.root / "runs", "wave3-test-run")
        store2.load()
        section2 = load_planning_state(store2.state)
        self.assertEqual(section2["accepted_sequence"], ["wu-001", "wu-002"])

    def test_34_accepted_sequence_survives_reload_unchanged(self):
        """Scenario 34: accepted sequence ordering survives reload unchanged."""
        planner = ScriptedPlanner(behaviors=[{"units": [make_proposal_unit(alias="u1"), make_proposal_unit(alias="u2", dependencies=["u1"])]}])
        controller = PlanningController(self.store, planner, parent_scope=PARENT_SCOPE, approved_verifiers=VERIFIER_REGISTRY, task=TASK)
        _, seq1 = controller.run()

        store2 = durable.Store(self.root / "runs", "wave3-test-run")
        store2.load()
        section2 = load_planning_state(store2.state)
        self.assertEqual(section2["accepted_sequence"], seq1)

    def test_35_planner_cannot_alter_accepted_plan_after_execution_begins(self):
        """Scenario 35: planner cannot alter accepted plan after execution begins."""
        planner = ScriptedPlanner(behaviors=[
            {"units": [make_proposal_unit(alias="original")]},
            {"units": [make_proposal_unit(alias="mutated_attempt")]},
        ])
        controller = PlanningController(self.store, planner, parent_scope=PARENT_SCOPE, approved_verifiers=VERIFIER_REGISTRY, task=TASK)
        specs1, seq1 = controller.run()

        # Calling run() again on the accepted plan returns the original without calling planner
        specs2, seq2 = controller.run()
        self.assertEqual(specs1, specs2)
        self.assertEqual(seq1, seq2)
        self.assertEqual(planner.call_count, 1)


class TestWave3ManagerAndEndToEnd(unittest.TestCase):
    """Scenarios 36-40: Manager integration, end-to-end flow, and source fingerprinting."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.repo, self.store = fixture(self.root)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_36_existing_fixed_wave2_execution_works_without_planner(self):
        """Scenario 36: existing fixed Wave 2 execution works without invoking planner."""
        # Create a run with fixed work_units
        fixed_spec = {
            "unit_id": "wu-fixed",
            "objective": "Implement multiply",
            "dependencies": [],
            "mode": "mutation",
            "scope": {"allowed_paths": ["calculator.py"], "forbidden_paths": []},
            "verifier_ids": ["check-calc"],
            "evidence_inputs": [],
            "limits": {"max_attempts": 2, "timeout_seconds": 60},
        }
        run_id = "wave2-fixed-run"
        store = durable.Store(self.root / "runs", run_id)
        manager.create_run(
            store,
            self.repo,
            TASK,
            [["python", "-m", "unittest", "test_calculator.py"]],
            CONFIG,
            criteria=["AC1"],
            work_units=[fixed_spec],
            verifier_registry=VERIFIER_REGISTRY,
        )

        # Execute using scripted executor (without any planner)
        def write_mult(r, spec, ctx):
            r.write("calculator.py", "def multiply(a, b):\n    return a * b\n")
        executor = ScriptedExecutor()
        executor.set_behavior("wu-fixed", write_mult)
        agents = type("Agents", (), {"executor": executor})()

        res = manager.execute_work_units(store, agents=agents, verifier_registry=VERIFIER_REGISTRY)
        self.assertEqual(res["status"], MILESTONE_READY)

    def test_37_legacy_manager_path_works_without_planner(self):
        """Scenario 37: legacy Manager path works without planner."""
        # Non-WorkUnit run created without work_units and without work_unit_planner
        run_id = "legacy-manager-run"
        store = durable.Store(self.root / "runs", run_id)
        manager.create_run(
            store,
            self.repo,
            "Legacy task",
            [["python", "-m", "unittest", "test_calculator.py"]],
            CONFIG,
            criteria=["AC1"],
        )
        self.assertNotIn("work_units", store.state)
        self.assertNotIn("work_unit_planner", store.state)

    def test_38_end_to_end_high_level_task_to_milestone_ready(self):
        """Scenario 38: End-to-end: high-level task → plan → Wave 2 ScriptedExecutor → MILESTONE_READY."""
        # Create run using manager.create_run with work_unit_planning=True
        run_id = "test-e2e-run-38"
        store = durable.Store(self.root / "runs", run_id)
        manager.create_run(
            store,
            self.repo,
            TASK,
            [["python", "-m", "unittest", "test_calculator.py"]],
            CONFIG,
            criteria=["AC1"],
            allow=["calculator.py", "helper.py"],
            work_unit_planning=True,
            verifier_registry=VERIFIER_REGISTRY,
            capability_policy={
                "paths": {
                    "calculator.py": ["calculator.correctness"],
                    "helper.py": ["helper.correctness"],
                }
            },
        )

        # Scripted planner proposes 2-unit plan
        proposal = {
            "units": [
                make_proposal_unit(alias="calc_unit", objective="Implement multiply", paths=["calculator.py"]),
                make_proposal_unit(alias="helper_unit", objective="Implement helper", dependencies=["calc_unit"], paths=["helper.py"], verifier_ids=["check-helper"]),
            ]
        }
        planner = ScriptedPlanner(behaviors=[proposal])

        # Scripted executor implements both units
        def implement_calc(r, spec, ctx):
            r.write("calculator.py", "def multiply(a, b):\n    return a * b\n")

        def implement_helper(r, spec, ctx):
            r.write("helper.py", "def helper_fn():\n    return 42\n")

        executor = ScriptedExecutor()
        executor.set_behavior("wu-001", implement_calc)
        executor.set_behavior("wu-002", implement_helper)

        agents = type("Agents", (), {"planner": planner, "executor": executor})()

        # Run end-to-end plan and execute
        res = manager.plan_and_execute_work_units(
            store,
            planner,
            agents=agents,
            verifier_registry=VERIFIER_REGISTRY,
        )

        self.assertEqual(res["status"], MILESTONE_READY)
        self.assertTrue(res.get("work_units_complete"))

    def test_39_end_to_end_leaves_all_top_level_trusted_fields_unchanged(self):
        """Scenario 39: end-to-end planning and execution leaves top-level trusted fields untouched."""
        run_id = "test-e2e-run-39"
        store = durable.Store(self.root / "runs", run_id)
        manager.create_run(
            store,
            self.repo,
            TASK,
            [["python", "-m", "unittest", "test_calculator.py"]],
            CONFIG,
            criteria=["AC1"],
            allow=["calculator.py"],
            work_unit_planning=True,
            verifier_registry=VERIFIER_REGISTRY,
            capability_policy={
                "paths": {
                    "calculator.py": ["calculator.correctness"],
                }
            },
        )

        initial_vp = copy.deepcopy(store.state.get("verified_progress"))
        initial_lvc = copy.deepcopy(store.state.get("last_verified_checkpoint"))

        proposal = {"units": [make_proposal_unit(alias="u1")]}
        planner = ScriptedPlanner(behaviors=[proposal])

        def implement_calc(r, spec, ctx):
            r.write("calculator.py", "def multiply(a, b):\n    return a * b\n")

        executor = ScriptedExecutor()
        executor.set_behavior("wu-001", implement_calc)
        agents = type("Agents", (), {"planner": planner, "executor": executor})()

        res = manager.plan_and_execute_work_units(
            store,
            planner,
            agents=agents,
            verifier_registry=VERIFIER_REGISTRY,
        )
        self.assertEqual(res["status"], MILESTONE_READY)

        # TRUST INVARIANT: WorkUnit milestone completion does NOT promote trusted completion
        final_state = store.state
        self.assertEqual(final_state.get("verified_progress"), initial_vp)
        self.assertEqual(final_state.get("last_verified_checkpoint"), initial_lvc)
        self.assertNotEqual(final_state.get("status"), "VERIFIED")

    def test_40_environment_source_fingerprint_covers_work_unit_planner(self):
        """Scenario 40: environment/source fingerprint covers work_unit_planner.py."""
        fingerprint = environment.capture(CONFIG, self.repo.root, [["python", "-m", "unittest"]])
        source = fingerprint.get("source", {})
        self.assertIn("work_unit_planner.py", source)
        self.assertIsNotNone(source["work_unit_planner.py"].get("sha256"))
        self.assertEqual(len(source["work_unit_planner.py"]["sha256"]), 64)


class TestWave3StoreWALCrashScenarios(unittest.TestCase):
    """Deterministic Store/WAL tests for the 6 specific crash/reload scenarios."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.repo, self.store = fixture(self.root)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_crash_1_before_proposal_persisted(self):
        """Crash 1: Crash before proposal is persisted; resumes cleanly without corrupted state."""
        section = wup._initial_planning_state(TASK)
        persist_planning_state(section, self.store)

        # Crash & reload from disk
        store2 = durable.Store(self.root / "runs", "wave3-test-run")
        store2.load()

        planner = ScriptedPlanner(behaviors=[{"units": [make_proposal_unit(alias="u1")]}])
        controller = PlanningController(
            store2, planner,
            parent_scope=PARENT_SCOPE,
            approved_verifiers=VERIFIER_REGISTRY,
            task=TASK,
        )
        specs, seq = controller.run()
        self.assertEqual(seq, ["wu-001"])
        self.assertEqual(planner.call_count, 1)
        self.assertEqual(store2.state["work_unit_planner"]["phase"], "PLAN_ACCEPTED")

    def test_crash_2_after_proposal_persisted_before_validation(self):
        """Crash 2: Crash after proposal persisted but before validation.
        Unvalidated proposal cannot execute. Resuming validates it deterministically.
        """
        from work_unit_scheduler import WorkUnitSchedulingError
        proposal = {"units": [make_proposal_unit(alias="u1")]}
        section = wup._initial_planning_state(TASK)
        section["phase"] = "PROPOSAL_RECEIVED"
        section["current_proposal"] = copy.deepcopy(proposal)
        persist_planning_state(section, self.store)

        # Crash & reload from disk
        store2 = durable.Store(self.root / "runs", "wave3-test-run")
        store2.load()

        # Unvalidated proposal cannot execute in Wave 2
        with self.assertRaises(WorkUnitSchedulingError):
            WorkUnitScheduler(store2, self.repo, verifier_registry=VERIFIER_REGISTRY).run_sequence()

        # Resuming PlanningController re-validates and accepts without calling planner again
        planner = ScriptedPlanner()
        controller = PlanningController(
            store2, planner,
            parent_scope=PARENT_SCOPE,
            approved_verifiers=VERIFIER_REGISTRY,
            task=TASK,
        )
        specs, seq = controller.run()
        self.assertEqual(seq, ["wu-001"])
        self.assertEqual(planner.call_count, 0)  # Re-validated from persisted proposal!
        self.assertEqual(store2.state["work_unit_planner"]["phase"], "PLAN_ACCEPTED")

    def test_crash_3_after_validation_before_canonical_plan_persistence(self):
        """Crash 3: Crash after validation but before canonical plan persistence."""
        from work_unit_scheduler import WorkUnitSchedulingError
        proposal = {"units": [make_proposal_unit(alias="u1")]}
        section = wup._initial_planning_state(TASK)
        section["phase"] = "VALIDATING"
        section["current_proposal"] = copy.deepcopy(proposal)
        persist_planning_state(section, self.store)

        # Crash & reload
        store2 = durable.Store(self.root / "runs", "wave3-test-run")
        store2.load()

        # Wave 2 scheduler refuses: plan not accepted yet
        with self.assertRaises(WorkUnitSchedulingError):
            WorkUnitScheduler(store2, self.repo, verifier_registry=VERIFIER_REGISTRY).run_sequence()

        # Resume planning
        planner = ScriptedPlanner()
        controller = PlanningController(
            store2, planner,
            parent_scope=PARENT_SCOPE,
            approved_verifiers=VERIFIER_REGISTRY,
            task=TASK,
        )
        specs, seq = controller.run()
        self.assertEqual(seq, ["wu-001"])
        self.assertEqual(store2.state["work_unit_planner"]["phase"], "PLAN_ACCEPTED")

    def test_crash_4_after_plan_accepted(self):
        """Crash 4: Crash after PLAN_ACCEPTED; safely resumes into Wave 2 execution."""
        planner = ScriptedPlanner(behaviors=[{"units": [make_proposal_unit(alias="u1")]}])
        controller = PlanningController(
            self.store, planner,
            parent_scope=PARENT_SCOPE,
            approved_verifiers=VERIFIER_REGISTRY,
            task=TASK,
        )
        specs, seq = controller.run()

        # Simulate building and committing work_units before crash
        wu_section = build_work_units_section_from_plan(specs, seq)
        self.store.commit(work_units=wu_section)

        # Crash & reload
        store2 = durable.Store(self.root / "runs", "wave3-test-run")
        store2.load()

        # Planner is not called again
        controller2 = PlanningController(
            store2, planner,
            parent_scope=PARENT_SCOPE,
            approved_verifiers=VERIFIER_REGISTRY,
            task=TASK,
        )
        specs2, seq2 = controller2.run()
        self.assertEqual(specs, specs2)
        self.assertEqual(seq, seq2)
        self.assertEqual(planner.call_count, 1)

        # Wave 2 scheduler executes successfully
        executor = ScriptedExecutor()
        executor.set_behavior("wu-001", lambda r, s, c: r.write("calculator.py", "def multiply(a, b):\n    return a * b\n"))
        agents = type("Agents", (), {"executor": executor})()
        scheduler = WorkUnitScheduler(store2, self.repo, verifier_registry=VERIFIER_REGISTRY, agents=agents, parent_scope=PARENT_SCOPE)
        res = scheduler.run_sequence()
        self.assertEqual(res["status"], MILESTONE_READY)

    def test_crash_5_after_one_schema_correction_consumed(self):
        """Crash 5: Crash after one schema correction already consumed; counters preserved."""
        section = wup._initial_planning_state(TASK)
        section["schema_correction_used"] = True
        section["attempts"] = [{"attempt_number": 1, "kind": "schema_correction", "result": "schema_error"}]
        persist_planning_state(section, self.store)

        # Crash & reload
        store2 = durable.Store(self.root / "runs", "wave3-test-run")
        store2.load()

        # Next malformed response must immediately fail closed (second correction refused)
        planner = ScriptedPlanner(behaviors=[{"malformed": True}])
        controller = PlanningController(
            store2, planner,
            parent_scope=PARENT_SCOPE,
            approved_verifiers=VERIFIER_REGISTRY,
            task=TASK,
        )
        with self.assertRaises(PlannerError) as ctx:
            controller.run()
        self.assertEqual(ctx.exception.code, "second_schema_correction_refused")

    def test_crash_6_after_one_replan_consumed(self):
        """Crash 6: Crash after one replan already consumed; counters preserved."""
        section = wup._initial_planning_state(TASK)
        section["replan_used"] = True
        section["attempts"] = [{"attempt_number": 1, "kind": "replan", "result": "validation_error"}]
        persist_planning_state(section, self.store)

        # Crash & reload
        store2 = durable.Store(self.root / "runs", "wave3-test-run")
        store2.load()

        # Next decomposition defect must immediately fail closed (second replan refused)
        cycle_proposal = {
            "units": [
                make_proposal_unit(alias="u1", dependencies=["u2"]),
                make_proposal_unit(alias="u2", dependencies=["u1"]),
            ]
        }
        planner = ScriptedPlanner(behaviors=[cycle_proposal])
        controller = PlanningController(
            store2, planner,
            parent_scope=PARENT_SCOPE,
            approved_verifiers=VERIFIER_REGISTRY,
            task=TASK,
        )
        with self.assertRaises(PlannerError) as ctx:
            controller.run()
        self.assertEqual(ctx.exception.code, "second_replan_refused")


class AdvancingClock:
    def __init__(self, start: float = 0.0):
        self.t = float(start)

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += float(dt)


class TestWave3AuditRegressions(unittest.TestCase):
    """Regressions for the 8 Codex audit findings."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.repo, self.store = fixture(self.root)

    def tearDown(self):
        self.temp_dir.cleanup()

    def create_manager_run(self, run_id="wave3-test-run", max_runtime_seconds=3600.0, capability_policy=_OMIT):
        store = durable.Store(self.root / "runs", run_id)
        cap = DEFAULT_TEST_CAPABILITY_POLICY if capability_policy is _OMIT else capability_policy
        manager.create_run(
            store, self.repo, TASK, [["python", "-m", "unittest", "test_calculator.py"]],
            CONFIG, criteria=["AC1"], allow=["calculator.py", "helper.py"],
            work_unit_planning=True, verifier_registry=VERIFIER_REGISTRY,
            budgets={"max_runtime_seconds": max_runtime_seconds},
            capability_policy=cap,
        )
        return store

    # ----------------------------------------------------------------------- Fix 1
    def test_fix1_1_raw_alias_substituted_fails_on_reload(self):
        """Fix 1: Persisted PLAN_ACCEPTED with raw alias 'u1' instead of canonical 'wu-001' fails closed on Store reload."""
        planner = ScriptedPlanner(behaviors=[{"units": [make_proposal_unit(alias="u1")]}])
        controller = PlanningController(self.store, planner, parent_scope=PARENT_SCOPE, approved_verifiers=VERIFIER_REGISTRY, task=TASK)
        specs, seq = controller.run()

        state = copy.deepcopy(self.store.state)
        tampered_specs = copy.deepcopy(specs)
        tampered_specs[0]["unit_id"] = "u1"
        tampered_seq = ["u1"]
        digest = wup._digest({"specs": tampered_specs, "sequence": tampered_seq})
        state["work_unit_planner"]["accepted_plan"] = tampered_specs
        state["work_unit_planner"]["accepted_sequence"] = tampered_seq
        state["work_unit_planner"]["proposal_digest"] = digest

        state_file = self.store.directory / "state.json"
        import json
        state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")

        reloaded_store = durable.Store(self.root / "runs", "wave3-test-run")
        with self.assertRaises((durable.DurableError, ValueError, WorkUnitError)):
            reloaded_store.load()

    def test_fix1_2_bogus_digest_fails_on_reload(self):
        """Fix 1: Persisted PLAN_ACCEPTED with bogus digest fails closed on Store reload."""
        planner = ScriptedPlanner(behaviors=[{"units": [make_proposal_unit(alias="u1")]}])
        controller = PlanningController(self.store, planner, parent_scope=PARENT_SCOPE, approved_verifiers=VERIFIER_REGISTRY, task=TASK)
        controller.run()

        state = copy.deepcopy(self.store.state)
        state["work_unit_planner"]["proposal_digest"] = "bad" * 16
        state_file = self.store.directory / "state.json"
        import json
        state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")

        reloaded_store = durable.Store(self.root / "runs", "wave3-test-run")
        with self.assertRaises((durable.DurableError, ValueError, WorkUnitError)):
            reloaded_store.load()

    def test_fix1_3_cyclic_accepted_dependencies_fails_on_reload(self):
        """Fix 1: Persisted PLAN_ACCEPTED with cyclic dependencies fails closed on Store reload."""
        planner = ScriptedPlanner(behaviors=[{
            "units": [
                make_proposal_unit(alias="u1", paths=["calculator.py"], verifier_ids=["check-calc"]),
                make_proposal_unit(alias="u2", dependencies=["u1"], paths=["helper.py"], verifier_ids=["check-helper"]),
            ]
        }])
        controller = PlanningController(self.store, planner, parent_scope=PARENT_SCOPE, approved_verifiers=VERIFIER_REGISTRY, task=TASK)
        specs, seq = controller.run()

        state = copy.deepcopy(self.store.state)
        tampered_specs = copy.deepcopy(specs)
        tampered_specs[0]["dependencies"] = ["wu-002"]
        tampered_specs[1]["dependencies"] = ["wu-001"]
        digest = wup._digest({"specs": tampered_specs, "sequence": seq})
        state["work_unit_planner"]["accepted_plan"] = tampered_specs
        state["work_unit_planner"]["proposal_digest"] = digest
        state_file = self.store.directory / "state.json"
        import json
        state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")

        reloaded_store = durable.Store(self.root / "runs", "wave3-test-run")
        with self.assertRaises((durable.DurableError, ValueError, WorkUnitError)):
            reloaded_store.load()

    def test_fix1_4_objective_tampering_fails_on_reload(self):
        """Fix 1: Accepted objective changed on disk fails closed on Store reload (digest mismatch)."""
        planner = ScriptedPlanner(behaviors=[{"units": [make_proposal_unit(alias="u1")]}])
        controller = PlanningController(self.store, planner, parent_scope=PARENT_SCOPE, approved_verifiers=VERIFIER_REGISTRY, task=TASK)
        specs, seq = controller.run()

        state = copy.deepcopy(self.store.state)
        state["work_unit_planner"]["accepted_plan"][0]["objective"] = "Tampered objective"
        state_file = self.store.directory / "state.json"
        import json
        state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")

        reloaded_store = durable.Store(self.root / "runs", "wave3-test-run")
        with self.assertRaises((durable.DurableError, ValueError, WorkUnitError)):
            reloaded_store.load()

    def test_fix1_5_scope_tampering_fails_on_reload(self):
        """Fix 1: Accepted scope changed to outside parent fails closed on Store reload."""
        planner = ScriptedPlanner(behaviors=[{"units": [make_proposal_unit(alias="u1")]}])
        controller = PlanningController(self.store, planner, parent_scope=PARENT_SCOPE, approved_verifiers=VERIFIER_REGISTRY, task=TASK)
        specs, seq = controller.run()

        state = copy.deepcopy(self.store.state)
        tampered_specs = copy.deepcopy(specs)
        tampered_specs[0]["scope"]["allowed_paths"] = ["secret.py"]
        digest = wup._digest({"specs": tampered_specs, "sequence": seq})
        state["work_unit_planner"]["accepted_plan"] = tampered_specs
        state["work_unit_planner"]["proposal_digest"] = digest
        state_file = self.store.directory / "state.json"
        import json
        state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")

        reloaded_store = durable.Store(self.root / "runs", "wave3-test-run")
        with self.assertRaises((durable.DurableError, ValueError, WorkUnitError)):
            reloaded_store.load()

    def test_fix1_6_sequence_tampering_fails_on_reload(self):
        """Fix 1: Accepted sequence altered on disk fails closed on Store reload."""
        planner = ScriptedPlanner(behaviors=[{
            "units": [
                make_proposal_unit(alias="u1", paths=["calculator.py"], verifier_ids=["check-calc"]),
                make_proposal_unit(alias="u2", dependencies=["u1"], paths=["helper.py"], verifier_ids=["check-helper"]),
            ]
        }])
        controller = PlanningController(self.store, planner, parent_scope=PARENT_SCOPE, approved_verifiers=VERIFIER_REGISTRY, task=TASK)
        specs, seq = controller.run()

        state = copy.deepcopy(self.store.state)
        state["work_unit_planner"]["accepted_sequence"] = ["wu-001"]
        state_file = self.store.directory / "state.json"
        import json
        state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")

        reloaded_store = durable.Store(self.root / "runs", "wave3-test-run")
        with self.assertRaises((durable.DurableError, ValueError, WorkUnitError)):
            reloaded_store.load()

    def test_fix1_7_planner_vs_work_units_mismatch_fails_on_reload(self):
        """Fix 1: Mismatch between work_unit_planner and installed work_units fails closed on Store reload."""
        planner = ScriptedPlanner(behaviors=[{"units": [make_proposal_unit(alias="u1")]}])
        controller = PlanningController(self.store, planner, parent_scope=PARENT_SCOPE, approved_verifiers=VERIFIER_REGISTRY, task=TASK)
        specs, seq = controller.run()
        wu_section = build_work_units_section_from_plan(specs, seq)
        self.store.commit(work_units=wu_section)

        state = copy.deepcopy(self.store.state)
        state["work_units"]["units"]["wu-001"]["spec"]["objective"] = "Altered objective in work_units"
        work_units._reseal_section(state["work_units"])
        state_file = self.store.directory / "state.json"
        import json
        state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")

        reloaded_store = durable.Store(self.root / "runs", "wave3-test-run")
        with self.assertRaises((durable.DurableError, ValueError, WorkUnitError)):
            reloaded_store.load()

    # ----------------------------------------------------------------------- Fix 2
    def test_fix2_1_repeated_plan_work_units_preserves_executing_state(self):
        """Fix 2: Calling plan_work_units() while unit 1 is EXECUTING preserves state, attempts, and avoids replan."""
        store = self.create_manager_run("test-fix2-1")
        planner = ScriptedPlanner(behaviors=[{"units": [make_proposal_unit(alias="u1")]}])
        agents = type("Agents", (), {"planner": planner})()
        log = harness.EventLog(self.root / "event2_1.jsonl")
        loop = manager.ManagerLoop(store, None, self.repo, None, agents, log)
        specs, seq = loop.plan_work_units()
        self.assertEqual(planner.call_count, 1)

        wu_section = store.state["work_units"]
        unit = wu_section["units"]["wu-001"]
        work_units.transition_unit(unit, "VALIDATED")
        work_units.transition_unit(unit, "PENDING")
        work_units.transition_unit(unit, "READY")
        work_units.transition_unit(unit, "EXECUTING")
        work_units.create_attempt(unit, "att-001", candidate_id="cand-001")
        work_units.seal_unit_state(unit)
        work_units._reseal_section(wu_section)
        store.commit(work_units=wu_section)

        rogue_planner = ScriptedPlanner(behaviors=[{"units": [make_proposal_unit(alias="rogue")]}])
        specs2, seq2 = loop.plan_work_units(planner=rogue_planner)

        self.assertEqual(specs2, specs)
        self.assertEqual(seq2, seq)
        self.assertEqual(rogue_planner.call_count, 0)

        current_wu = store.state["work_units"]
        unit_after = current_wu["units"]["wu-001"]
        self.assertEqual(unit_after["status"], "EXECUTING")
        self.assertEqual(len(unit_after["attempts"]), 1)
        self.assertEqual(unit_after["attempts"][0]["attempt_id"], "att-001")

    def test_fix2_2_repeated_plan_work_units_preserves_verified_unit(self):
        """Fix 2: Calling plan_work_units() after unit 1 is UNIT_VERIFIED preserves unit status."""
        store = self.create_manager_run("test-fix2-2")
        planner = ScriptedPlanner(behaviors=[{"units": [make_proposal_unit(alias="u1")]}])
        agents = type("Agents", (), {"planner": planner})()
        log = harness.EventLog(self.root / "event2_2.jsonl")
        loop = manager.ManagerLoop(store, None, self.repo, None, agents, log)
        specs, seq = loop.plan_work_units()

        wu_section = store.state["work_units"]
        unit = wu_section["units"]["wu-001"]
        work_units.transition_unit(unit, "VALIDATED")
        work_units.transition_unit(unit, "PENDING")
        work_units.transition_unit(unit, "READY")
        work_units.transition_unit(unit, "EXECUTING")
        work_units.create_attempt(unit, "att-001")
        work_units.complete_attempt(unit, "att-001", outcome="passed", candidate_id="cand-001")
        work_units.transition_unit(unit, "VERIFYING")
        work_units.set_unit_result(unit, verifier_evidence={"passed": True, "check": "check-calc"}, candidate_id="cand-001", attempt_id="att-001")
        work_units.transition_unit(unit, "UNIT_VERIFIED")
        work_units.seal_unit_state(unit)
        work_units._reseal_section(wu_section)
        store.commit(work_units=wu_section)

        specs2, seq2 = loop.plan_work_units()
        self.assertEqual(store.state["work_units"]["units"]["wu-001"]["status"], "UNIT_VERIFIED")
        self.assertEqual(len(store.state["work_units"]["units"]["wu-001"]["attempts"]), 1)

    # ----------------------------------------------------------------------- Fix 3
    def test_fix3_1_malformed_then_correction_valid_at_most_2_calls(self):
        """Fix 3.1: Malformed proposal followed by valid correction makes at most 2 calls."""
        planner = ScriptedPlanner(behaviors=[
            {"bad": "schema"},
            {"units": [make_proposal_unit(alias="u1")]},
        ])
        controller = PlanningController(self.store, planner, parent_scope=PARENT_SCOPE, approved_verifiers=VERIFIER_REGISTRY, task=TASK)
        specs, seq = controller.run()
        self.assertEqual(planner.call_count, 2)
        self.assertEqual(seq, ["wu-001"])

    def test_fix3_2_malformed_then_corrected_decomposition_invalid_no_third_call(self):
        """Fix 3.2: Malformed proposal followed by invalid corrected decomposition makes NO third call."""
        cycle_proposal = {
            "units": [
                make_proposal_unit(alias="u1", dependencies=["u2"]),
                make_proposal_unit(alias="u2", dependencies=["u1"]),
            ]
        }
        planner = ScriptedPlanner(behaviors=[
            {"bad": "schema"},
            cycle_proposal,
            {"units": [make_proposal_unit(alias="u1")]},
        ])
        controller = PlanningController(self.store, planner, parent_scope=PARENT_SCOPE, approved_verifiers=VERIFIER_REGISTRY, task=TASK)
        with self.assertRaises(PlannerError) as ctx:
            controller.run()
        self.assertEqual(ctx.exception.code, "second_replan_refused")
        self.assertEqual(planner.call_count, 2)

    def test_fix3_3_valid_decomposition_defect_one_replan_total_2_calls(self):
        """Fix 3.3: Valid decomposition defect triggers one replan for 2 calls total."""
        cycle_proposal = {
            "units": [
                make_proposal_unit(alias="u1", dependencies=["u2"]),
                make_proposal_unit(alias="u2", dependencies=["u1"]),
            ]
        }
        planner = ScriptedPlanner(behaviors=[
            cycle_proposal,
            {"units": [make_proposal_unit(alias="u1")]},
        ])
        controller = PlanningController(self.store, planner, parent_scope=PARENT_SCOPE, approved_verifiers=VERIFIER_REGISTRY, task=TASK)
        specs, seq = controller.run()
        self.assertEqual(planner.call_count, 2)
        self.assertEqual(seq, ["wu-001"])

    def test_fix3_4_schema_correction_consumed_replan_unavailable(self):
        """Fix 3.4: When schema correction was consumed, replan is unavailable (ceiling 2)."""
        cycle_proposal = {
            "units": [
                make_proposal_unit(alias="u1", dependencies=["u2"]),
                make_proposal_unit(alias="u2", dependencies=["u1"]),
            ]
        }
        planner = ScriptedPlanner(behaviors=[
            {"bad": "schema"},
            cycle_proposal,
            {"units": [make_proposal_unit(alias="u1")]},
        ])
        controller = PlanningController(self.store, planner, parent_scope=PARENT_SCOPE, approved_verifiers=VERIFIER_REGISTRY, task=TASK)
        with self.assertRaises(PlannerError) as ctx:
            controller.run()
        self.assertEqual(ctx.exception.code, "second_replan_refused")
        self.assertEqual(planner.call_count, 2)

    def test_fix3_5_replan_consumed_schema_correction_unavailable(self):
        """Fix 3.5: When replan was consumed, schema correction is unavailable (ceiling 2)."""
        cycle_proposal = {
            "units": [
                make_proposal_unit(alias="u1", dependencies=["u2"]),
                make_proposal_unit(alias="u2", dependencies=["u1"]),
            ]
        }
        planner = ScriptedPlanner(behaviors=[
            cycle_proposal,
            {"bad": "schema in replan"},
            {"units": [make_proposal_unit(alias="u1")]},
        ])
        controller = PlanningController(self.store, planner, parent_scope=PARENT_SCOPE, approved_verifiers=VERIFIER_REGISTRY, task=TASK)
        with self.assertRaises(PlannerError) as ctx:
            controller.run()
        self.assertEqual(ctx.exception.code, "second_replan_refused")
        self.assertEqual(planner.call_count, 2)

    def test_fix3_6_crash_after_call_reservation_reload_does_not_regain_call(self):
        """Fix 3.6: Crash after call reservation consumes slot; reload does not regain it."""
        section = wup._initial_planning_state(TASK)
        section["planner_call_count"] = 1
        section["call_in_flight"] = False
        section["attempts"] = [{"attempt_number": 1, "kind": "initial", "result": "error"}]
        persist_planning_state(section, self.store)

        store2 = durable.Store(self.root / "runs", "wave3-test-run")
        store2.load()

        planner = ScriptedPlanner(behaviors=[
            {"bad": "schema"},
            {"units": [make_proposal_unit(alias="u1")]},
        ])
        controller = PlanningController(store2, planner, parent_scope=PARENT_SCOPE, approved_verifiers=VERIFIER_REGISTRY, task=TASK)
        with self.assertRaises(PlannerError) as ctx:
            controller.run()
        self.assertEqual(ctx.exception.code, "second_schema_correction_refused")
        self.assertEqual(planner.call_count, 1)

    def test_fix3_7_repeated_non_dict_responses_stop_at_2(self):
        """Fix 3.7: Repeated non-dict responses count as calls and stop at 2."""
        planner = ScriptedPlanner(behaviors=[
            "not a dict",
            12345,
            {"units": [make_proposal_unit(alias="u1")]},
        ])
        controller = PlanningController(self.store, planner, parent_scope=PARENT_SCOPE, approved_verifiers=VERIFIER_REGISTRY, task=TASK)
        with self.assertRaises(PlannerError) as ctx:
            controller.run()
        self.assertEqual(ctx.exception.code, "second_schema_correction_refused")
        self.assertEqual(planner.call_count, 2)

    def test_fix3_8_persisted_counters_cannot_be_reset_by_reload(self):
        """Fix 3.8: Persisted call counters are preserved across Store reloads."""
        section = wup._initial_planning_state(TASK)
        section["planner_call_count"] = 2
        section["schema_correction_used"] = True
        section["replan_used"] = True
        persist_planning_state(section, self.store)

        store2 = durable.Store(self.root / "runs", "wave3-test-run")
        store2.load()
        reloaded_sec = load_planning_state(store2.state)
        self.assertEqual(reloaded_sec["planner_call_count"], 2)
        self.assertTrue(reloaded_sec["schema_correction_used"])
        self.assertTrue(reloaded_sec["replan_used"])

    # ----------------------------------------------------------------------- Fix 4
    def test_fix4_planner_call_cannot_succeed_after_budget_expiry(self):
        """Fix 4: Under 10s budget, planner consumes 11s and returns valid proposal; rejected with BUDGET_EXHAUSTED."""
        clock = AdvancingClock(start=0.0)
        def slow_plan(ctx):
            clock.advance(11.0)
            return {"units": [make_proposal_unit(alias="u1")]}
        planner = ScriptedPlanner(behaviors=[slow_plan])
        controller = PlanningController(
            self.store, planner,
            parent_scope=PARENT_SCOPE,
            approved_verifiers=VERIFIER_REGISTRY,
            task=TASK,
            budgets={"max_runtime_seconds": 10.0},
            clock=clock,
        )
        with self.assertRaises(PlannerBudgetExhausted):
            controller.run()

        section = load_planning_state(self.store.state)
        self.assertEqual(section["phase"], "PLAN_REJECTED")
        self.assertEqual(section["rejection_reason"], "budget_exhausted")
        self.assertIsNone(section["accepted_plan"])

    # ----------------------------------------------------------------------- Fix 5
    def test_fix5_1_planner_consumes_11s_crashes_reload_reflects_exhaustion_no_retry(self):
        """Fix 5.1: Planner consumes 11s then crashes; reload reflects exhaustion and no retry begins."""
        run_id = "test-fix5-1"
        store = self.create_manager_run(run_id, max_runtime_seconds=10.0)
        clock = AdvancingClock(start=100.0)
        section = wup._initial_planning_state(TASK)
        section["planner_call_count"] = 1
        section["call_in_flight"] = True
        section["in_flight_timing"] = {
            "call_number": 1,
            "call_kind": "initial",
            "call_start_clock": 100.0,
            "call_start_runtime": 0.0,
            "accounted_runtime": 0.0,
            "max_runtime_seconds": 10.0,
        }
        persist_planning_state(section, store)

        clock.advance(11.0)

        store2 = durable.Store(self.root / "runs", run_id)
        store2.load()

        planner = ScriptedPlanner(behaviors=[{"units": [make_proposal_unit(alias="u1")]}])
        controller = PlanningController(
            store2, planner,
            parent_scope=PARENT_SCOPE,
            approved_verifiers=VERIFIER_REGISTRY,
            task=TASK,
            budgets={"max_runtime_seconds": 10.0},
            clock=clock,
        )
        with self.assertRaises(PlannerBudgetExhausted):
            controller.run()

        self.assertEqual(planner.call_count, 0)
        self.assertEqual(store2.state["work_unit_planner"]["phase"], "PLAN_REJECTED")
        self.assertEqual(store2.state["work_unit_planner"]["rejection_reason"], "budget_exhausted")
        self.assertGreaterEqual(store2.state["manager"]["runtime_seconds"], 11.0)

    def test_fix5_2_second_reload_does_not_double_count_runtime(self):
        """Fix 5.2: Subsequent reload does not double count already reconciled runtime."""
        clock = AdvancingClock(start=100.0)
        section = wup._initial_planning_state(TASK)
        section["planner_call_count"] = 1
        section["call_in_flight"] = True
        section["in_flight_timing"] = {
            "call_number": 1,
            "call_kind": "initial",
            "call_start_clock": 100.0,
            "call_start_runtime": 0.0,
            "accounted_runtime": 0.0,
            "max_runtime_seconds": 50.0,
        }
        persist_planning_state(section, self.store)

        clock.advance(11.0)

        store2 = durable.Store(self.root / "runs", "wave3-test-run")
        store2.load()
        planner2 = ScriptedPlanner(behaviors=[{"units": [make_proposal_unit(alias="u1")]}])
        controller2 = PlanningController(
            store2, planner2,
            parent_scope=PARENT_SCOPE,
            approved_verifiers=VERIFIER_REGISTRY,
            task=TASK,
            budgets={"max_runtime_seconds": 50.0},
            clock=clock,
        )
        self.assertAlmostEqual(controller2.base_runtime, 11.0, places=2)

        store3 = durable.Store(self.root / "runs", "wave3-test-run")
        store3.load()
        controller3 = PlanningController(
            store3, planner2,
            parent_scope=PARENT_SCOPE,
            approved_verifiers=VERIFIER_REGISTRY,
            task=TASK,
            budgets={"max_runtime_seconds": 50.0},
            clock=clock,
        )
        self.assertAlmostEqual(controller3.base_runtime, 11.0, places=2)

    def test_fix5_3_partial_already_accounted_runtime(self):
        """Fix 5.3: Partial already accounted runtime reconciles only remaining unaccounted portion."""
        run_id = "test-fix5-3"
        store = self.create_manager_run(run_id, max_runtime_seconds=50.0)
        clock = AdvancingClock(start=100.0)
        section = wup._initial_planning_state(TASK)
        section["planner_call_count"] = 1
        section["call_in_flight"] = True
        section["in_flight_timing"] = {
            "call_number": 1,
            "call_kind": "initial",
            "call_start_clock": 100.0,
            "call_start_runtime": 0.0,
            "accounted_runtime": 4.0,
            "max_runtime_seconds": 50.0,
        }
        persist_planning_state(section, store, manager_runtime=4.0)

        clock.advance(11.0)

        store2 = durable.Store(self.root / "runs", run_id)
        store2.load()
        planner = ScriptedPlanner(behaviors=[{"units": [make_proposal_unit(alias="u1")]}])
        controller = PlanningController(
            store2, planner,
            parent_scope=PARENT_SCOPE,
            approved_verifiers=VERIFIER_REGISTRY,
            task=TASK,
            budgets={"max_runtime_seconds": 50.0},
            clock=clock,
        )
        self.assertAlmostEqual(controller.base_runtime, 11.0, places=2)

    # ----------------------------------------------------------------------- Fix 6
    def test_fix6_manager_hands_off_current_runtime_and_stops_if_exhausted(self):
        """Fix 6: If Manager/session setup consumes 11s under 10s budget, planning never calls planner."""
        clock = AdvancingClock(start=0.0)
        planner = ScriptedPlanner(behaviors=[{"units": [make_proposal_unit()]}])
        agents = type("Agents", (), {"planner": planner})()

        run_id = "test-fix6-run"
        store = self.create_manager_run(run_id, max_runtime_seconds=10.0)
        log = harness.EventLog(self.root / "event6.jsonl")
        loop = manager.ManagerLoop(store, None, self.repo, None, agents, log, clock=clock)
        clock.advance(11.0)

        with self.assertRaises(manager.Stop) as ctx:
            loop.plan_work_units()
        self.assertEqual(ctx.exception.status, "BUDGET_EXHAUSTED")
        self.assertEqual(planner.call_count, 0)
        self.assertNotIn("work_units", store.state)

    # ----------------------------------------------------------------------- Fix 7
    def test_fix7_1_calculator_mutation_with_check_helper_rejected(self):
        """Fix 7.1: Calculator mutation unit choosing unrelated check-helper is rejected."""
        proposal = [make_proposal_unit(alias="u1", paths=["calculator.py"], verifier_ids=["check-helper"])]
        with self.assertRaises(PlannerError) as ctx:
            validate_and_build_canonical_plan(
                proposal, parent_scope=PARENT_SCOPE, approved_verifiers=VERIFIER_REGISTRY,
                capability_policy=DEFAULT_TEST_CAPABILITY_POLICY,
            )
        self.assertEqual(ctx.exception.code, "verifier_coverage_missing")

    def test_fix7_2_calculator_mutation_with_check_calc_accepted(self):
        """Fix 7.2: Calculator mutation unit choosing covering check-calc is accepted."""
        proposal = [make_proposal_unit(alias="u1", paths=["calculator.py"], verifier_ids=["check-calc"])]
        specs, seq = validate_and_build_canonical_plan(
            proposal, parent_scope=PARENT_SCOPE, approved_verifiers=VERIFIER_REGISTRY,
            capability_policy=DEFAULT_TEST_CAPABILITY_POLICY,
        )
        self.assertEqual(seq, ["wu-001"])

    def test_fix7_3_verifier_valid_mode_wrong_scope_rejected(self):
        """Fix 7.3: Verifier with valid mode but wrong scope is rejected."""
        proposal = [make_proposal_unit(alias="u1", paths=["helper.py"], verifier_ids=["check-calc"])]
        with self.assertRaises(PlannerError) as ctx:
            validate_and_build_canonical_plan(
                proposal, parent_scope=PARENT_SCOPE, approved_verifiers=VERIFIER_REGISTRY,
                capability_policy=DEFAULT_TEST_CAPABILITY_POLICY,
            )
        self.assertEqual(ctx.exception.code, "verifier_coverage_missing")

    def test_fix7_4_verifier_correct_scope_incompatible_mode_rejected(self):
        """Fix 7.4: Verifier with correct scope but incompatible mode is rejected."""
        readonly_calc_registry = {
            "check-calc-ro": {
                "argv": ["python", "-m", "unittest", "test_calculator.py"],
                "modes": ["read_only"],
                "paths": ["calculator.py"],
            }
        }
        proposal = [make_proposal_unit(alias="u1", mode="mutation", paths=["calculator.py"], verifier_ids=["check-calc-ro"])]
        with self.assertRaises(PlannerError) as ctx:
            validate_and_build_canonical_plan(
                proposal, parent_scope=PARENT_SCOPE, approved_verifiers=readonly_calc_registry,
                capability_policy=DEFAULT_TEST_CAPABILITY_POLICY,
            )
        self.assertEqual(ctx.exception.code, "verifier_coverage_missing")

    def test_fix7_5_multiple_verifier_composition_accepted(self):
        """Fix 7.5: Multiple verifiers covering different parts of composite scope are accepted."""
        proposal = [make_proposal_unit(
            alias="u1",
            paths=["calculator.py", "helper.py"],
            verifier_ids=["check-calc", "check-helper"],
        )]
        specs, seq = validate_and_build_canonical_plan(
            proposal, parent_scope=PARENT_SCOPE, approved_verifiers=VERIFIER_REGISTRY,
            capability_policy=DEFAULT_TEST_CAPABILITY_POLICY,
        )
        self.assertEqual(seq, ["wu-001"])

    def test_fix7_6_planner_cannot_alter_verifier_coverage_metadata(self):
        """Fix 7.6: Planner proposal trying to inject verifier metadata is rejected by schema validator."""
        unit = make_proposal_unit(alias="u1")
        unit["verifier_metadata"] = {"paths": ["*"]}
        with self.assertRaises(PlannerError) as ctx:
            validate_proposal_schema({"units": [unit]})
        self.assertEqual(ctx.exception.code, "malformed_schema")

    # ----------------------------------------------------------------------- Fix 8
    def test_fix8_scope_violation_terminal_not_replannable(self):
        """Fix 8: Scope violation (forbidden secret.py) fails terminally without replan; second proposal never requested."""
        forbidden_proposal = {
            "units": [make_proposal_unit(alias="u1", paths=["secret.py"], verifier_ids=["check-calc"])]
        }
        valid_proposal = {"units": [make_proposal_unit(alias="u1", paths=["calculator.py"])]}
        planner = ScriptedPlanner(behaviors=[forbidden_proposal, valid_proposal])
        controller = PlanningController(
            self.store, planner,
            parent_scope=PARENT_SCOPE,
            approved_verifiers=VERIFIER_REGISTRY,
            task=TASK,
        )
        with self.assertRaises(PlannerError) as ctx:
            controller.run()
        self.assertEqual(ctx.exception.code, "scope_outside_parent")
        self.assertEqual(planner.call_count, 1)
        section = load_planning_state(self.store.state)
        self.assertEqual(section["phase"], "PLAN_REJECTED")
        self.assertFalse(section["replan_used"])

    # ----------------------------------------------------------------------- Fix A: Cumulative Planning Budget
    def test_fix_a_1_stage_budget_cumulative_exhaustion(self):
        """Fix A.1: Stage budget = 10s. Call 1 (6s) + Call 2 (6s) = 12s cumulative > 10s -> budget_exhausted."""
        clock = AdvancingClock(start=0.0)
        def call1(ctx):
            clock.advance(6.0)
            return {"bad": "schema"}
        def call2(ctx):
            clock.advance(6.0)
            return {"units": [make_proposal_unit(alias="u1")]}

        planner = ScriptedPlanner(behaviors=[call1, call2])
        controller = PlanningController(
            self.store, planner,
            parent_scope=PARENT_SCOPE,
            approved_verifiers=VERIFIER_REGISTRY,
            task=TASK,
            budgets={"max_runtime_seconds": 100.0},
            planning_stage_budget_seconds=10.0,
            clock=clock,
        )
        with self.assertRaises(PlannerBudgetExhausted):
            controller.run()

        section = load_planning_state(self.store.state)
        self.assertEqual(section["phase"], "PLAN_REJECTED")
        self.assertEqual(section["rejection_reason"], "budget_exhausted")
        self.assertIsNone(section["accepted_plan"])
        self.assertEqual(planner.call_count, 2)
        self.assertGreaterEqual(section["planning_stage_consumed_seconds"], 10.0)

    def test_fix_a_2_stage_budget_within_cumulative_bound(self):
        """Fix A.2: Stage budget = 10s. Call 1 (4s) + Call 2 (4s) = 8s cumulative <= 10s -> PLAN_ACCEPTED."""
        clock = AdvancingClock(start=0.0)
        def call1(ctx):
            clock.advance(4.0)
            return {"bad": "schema"}
        def call2(ctx):
            clock.advance(4.0)
            return {"units": [make_proposal_unit(alias="u1")]}

        planner = ScriptedPlanner(behaviors=[call1, call2])
        controller = PlanningController(
            self.store, planner,
            parent_scope=PARENT_SCOPE,
            approved_verifiers=VERIFIER_REGISTRY,
            task=TASK,
            budgets={"max_runtime_seconds": 100.0},
            planning_stage_budget_seconds=10.0,
            clock=clock,
        )
        specs, seq = controller.run()
        self.assertEqual(seq, ["wu-001"])
        section = load_planning_state(self.store.state)
        self.assertEqual(section["phase"], "PLAN_ACCEPTED")
        self.assertEqual(section["planning_stage_consumed_seconds"], 8.0)

    def test_fix_a_3_crash_reload_preserves_cumulative_planning_consumed(self):
        """Fix A.3: Stage budget = 10s. Call 1 takes 6s, then crash. On reload, remaining authority is 4s, not 10s."""
        clock = AdvancingClock(start=0.0)
        def call1(ctx):
            clock.advance(6.0)
            return {"bad": "schema"}
        planner1 = ScriptedPlanner(behaviors=[call1])
        controller1 = PlanningController(
            self.store, planner1,
            parent_scope=PARENT_SCOPE,
            approved_verifiers=VERIFIER_REGISTRY,
            task=TASK,
            budgets={"max_runtime_seconds": 100.0},
            planning_stage_budget_seconds=10.0,
            clock=clock,
        )
        section = controller1._get_or_create_section()
        controller1._update_section(section, phase="PLANNING")
        ctx = build_planner_context(task=TASK, parent_scope=PARENT_SCOPE, approved_verifiers=VERIFIER_REGISTRY)
        controller1._timed_planner_call(ctx, section, "initial")
        self.assertEqual(controller1.cumulative_planning_consumed(), 6.0)

        # New controller instance from persisted store state
        store2 = durable.Store(self.root / "runs", "wave3-test-run")
        store2.load()
        def call2(ctx):
            clock.advance(5.0)  # 6 + 5 = 11 > 10
            return {"units": [make_proposal_unit(alias="u1")]}
        planner2 = ScriptedPlanner(behaviors=[call2])
        controller2 = PlanningController(
            store2, planner2,
            parent_scope=PARENT_SCOPE,
            approved_verifiers=VERIFIER_REGISTRY,
            task=TASK,
            budgets={"max_runtime_seconds": 100.0},
            clock=clock,
        )
        self.assertEqual(controller2.planning_stage_consumed_base, 6.0)
        self.assertEqual(controller2.remaining_planning_budget(), 4.0)

        with self.assertRaises(PlannerBudgetExhausted):
            controller2.run()

    def test_fix_a_4_schema_correction_consumes_cumulative_budget(self):
        """Fix A.4: Schema correction consumes from cumulative planning budget (7s + 4s = 11s > 10s)."""
        clock = AdvancingClock(start=0.0)
        def call1(ctx):
            clock.advance(7.0)
            return {"bad": "schema"}
        def call2(ctx):
            clock.advance(4.0)
            return {"units": [make_proposal_unit(alias="u1")]}
        planner = ScriptedPlanner(behaviors=[call1, call2])
        controller = PlanningController(
            self.store, planner,
            parent_scope=PARENT_SCOPE,
            approved_verifiers=VERIFIER_REGISTRY,
            task=TASK,
            budgets={"max_runtime_seconds": 100.0},
            planning_stage_budget_seconds=10.0,
            clock=clock,
        )
        with self.assertRaises(PlannerBudgetExhausted):
            controller.run()
        section = load_planning_state(self.store.state)
        self.assertEqual(section["phase"], "PLAN_REJECTED")

    def test_fix_a_5_replan_consumes_cumulative_budget(self):
        """Fix A.5: Replan consumes from cumulative planning budget (6s initial + 5s replan = 11s > 10s)."""
        clock = AdvancingClock(start=0.0)
        def call1(ctx):
            clock.advance(6.0)
            # Unknown dependency triggers replan
            return {"units": [make_proposal_unit(alias="u1", dependencies=["u_missing"])]}
        def call2(ctx):
            clock.advance(5.0)
            return {"units": [make_proposal_unit(alias="u1")]}
        planner = ScriptedPlanner(behaviors=[call1, call2])
        controller = PlanningController(
            self.store, planner,
            parent_scope=PARENT_SCOPE,
            approved_verifiers=VERIFIER_REGISTRY,
            task=TASK,
            budgets={"max_runtime_seconds": 100.0},
            planning_stage_budget_seconds=10.0,
            clock=clock,
        )
        with self.assertRaises(PlannerBudgetExhausted):
            controller.run()
        section = load_planning_state(self.store.state)
        self.assertEqual(section["phase"], "PLAN_REJECTED")

    def test_fix_a_6_effective_planner_deadline_capped_by_remaining_planning(self):
        """Fix A.6: Planner call deadline is capped by min(remaining_task, remaining_planning)."""
        clock = AdvancingClock(start=0.0)
        observed_deadlines = []
        def call1(ctx):
            observed_deadlines.append(ctx.get("deadline_seconds"))
            clock.advance(6.0)
            return {"bad": "schema"}
        def call2(ctx):
            observed_deadlines.append(ctx.get("deadline_seconds"))
            clock.advance(2.0)
            return {"units": [make_proposal_unit(alias="u1")]}
        planner = ScriptedPlanner(behaviors=[call1, call2])
        controller = PlanningController(
            self.store, planner,
            parent_scope=PARENT_SCOPE,
            approved_verifiers=VERIFIER_REGISTRY,
            task=TASK,
            budgets={"max_runtime_seconds": 100.0},
            planning_stage_budget_seconds=10.0,
            clock=clock,
        )
        specs, seq = controller.run()
        self.assertEqual(len(observed_deadlines), 2)
        # Call 1 deadline: min(100.0, 10.0) = 10.0
        self.assertEqual(observed_deadlines[0], 10.0)
        # Call 2 deadline: min(94.0, 4.0) = 4.0 (capped by remaining planning authority, NOT task 94s)
        self.assertEqual(observed_deadlines[1], 4.0)

    # ----------------------------------------------------------------------- Fix B: Final Acceptance Budget Check
    def test_fix_b_1_task_budget_expires_during_validation(self):
        """Fix B.1: Task budget expires during validation -> fail closed with budget_exhausted before PLAN_ACCEPTED."""
        clock = AdvancingClock(start=0.0)
        def call1(ctx):
            clock.advance(5.0)
            return {"units": [make_proposal_unit(alias="u1")]}
        planner = ScriptedPlanner(behaviors=[call1])
        controller = PlanningController(
            self.store, planner,
            parent_scope=PARENT_SCOPE,
            approved_verifiers=VERIFIER_REGISTRY,
            task=TASK,
            budgets={"max_runtime_seconds": 10.0},
            clock=clock,
        )
        orig_validate = wup.validate_canonical_plan
        def slow_validate(*args, **kwargs):
            clock.advance(6.0)  # 5.0 + 6.0 = 11.0 > 10.0
            return orig_validate(*args, **kwargs)

        try:
            wup.validate_canonical_plan = slow_validate
            with self.assertRaises(PlannerBudgetExhausted):
                controller.run()
        finally:
            wup.validate_canonical_plan = orig_validate

        section = load_planning_state(self.store.state)
        self.assertEqual(section["phase"], "PLAN_REJECTED")
        self.assertEqual(section["rejection_reason"], "budget_exhausted")
        self.assertIsNone(section["accepted_plan"])

    def test_fix_b_2_planning_stage_budget_expires_during_validation(self):
        """Fix B.2: Planning-stage budget expires during validation -> fail closed before PLAN_ACCEPTED."""
        clock = AdvancingClock(start=0.0)
        def call1(ctx):
            clock.advance(5.0)
            return {"units": [make_proposal_unit(alias="u1")]}
        planner = ScriptedPlanner(behaviors=[call1])
        controller = PlanningController(
            self.store, planner,
            parent_scope=PARENT_SCOPE,
            approved_verifiers=VERIFIER_REGISTRY,
            task=TASK,
            budgets={"max_runtime_seconds": 100.0},
            planning_stage_budget_seconds=10.0,
            clock=clock,
        )
        orig_validate = wup.validate_canonical_plan
        def slow_validate(*args, **kwargs):
            clock.advance(6.0)  # 5.0 + 6.0 = 11.0 > 10.0
            return orig_validate(*args, **kwargs)

        try:
            wup.validate_canonical_plan = slow_validate
            with self.assertRaises(PlannerBudgetExhausted):
                controller.run()
        finally:
            wup.validate_canonical_plan = orig_validate

        section = load_planning_state(self.store.state)
        self.assertEqual(section["phase"], "PLAN_REJECTED")
        self.assertEqual(section["rejection_reason"], "budget_exhausted")
        self.assertIsNone(section["accepted_plan"])

    def test_fix_b_3_validation_within_budget_succeeds(self):
        """Fix B.3: Validation elapsed time within budget leads to normal PLAN_ACCEPTED."""
        clock = AdvancingClock(start=0.0)
        def call1(ctx):
            clock.advance(3.0)
            return {"units": [make_proposal_unit(alias="u1")]}
        planner = ScriptedPlanner(behaviors=[call1])
        controller = PlanningController(
            self.store, planner,
            parent_scope=PARENT_SCOPE,
            approved_verifiers=VERIFIER_REGISTRY,
            task=TASK,
            budgets={"max_runtime_seconds": 100.0},
            planning_stage_budget_seconds=10.0,
            clock=clock,
        )
        orig_validate = wup.validate_canonical_plan
        called = [False]
        def normal_validate(*args, **kwargs):
            if not called[0]:
                called[0] = True
                clock.advance(2.0)  # 3.0 + 2.0 = 5.0 <= 10.0
            return orig_validate(*args, **kwargs)

        try:
            wup.validate_canonical_plan = normal_validate
            specs, seq = controller.run()
        finally:
            wup.validate_canonical_plan = orig_validate

        self.assertEqual(seq, ["wu-001"])
        section = load_planning_state(self.store.state)
        self.assertEqual(section["phase"], "PLAN_ACCEPTED")
        self.assertIsNotNone(section["accepted_plan"])
        self.assertEqual(section["planning_stage_consumed_seconds"], 5.0)

    def test_fix_b_4_validation_runtime_is_durably_persisted(self):
        """Fix B.4: Runtime consumed during validation is durably persisted in both planner section and manager."""
        clock = AdvancingClock(start=0.0)
        def call1(ctx):
            clock.advance(3.0)
            return {"units": [make_proposal_unit(alias="u1")]}
        planner = ScriptedPlanner(behaviors=[call1])
        run_id = "test-fix-b-4"
        store = self.create_manager_run(run_id, max_runtime_seconds=100.0)
        controller = PlanningController(
            store, planner,
            parent_scope=PARENT_SCOPE,
            approved_verifiers=VERIFIER_REGISTRY,
            task=TASK,
            budgets={"max_runtime_seconds": 100.0},
            planning_stage_budget_seconds=20.0,
            clock=clock,
        )
        orig_validate = wup.validate_canonical_plan
        called = [False]
        def timed_validate(*args, **kwargs):
            if not called[0]:
                called[0] = True
                clock.advance(4.0)  # 3 + 4 = 7
            return orig_validate(*args, **kwargs)

        try:
            wup.validate_canonical_plan = timed_validate
            controller.run()
        finally:
            wup.validate_canonical_plan = orig_validate

        section = load_planning_state(store.state)
        self.assertEqual(section["planning_stage_consumed_seconds"], 7.0)
        self.assertEqual(store.state["manager"]["runtime_seconds"], 7.0)

    # ----------------------------------------------------------------------- Fix C: Verifier Capability Authority
    def test_fix_c_1_objective_correctness_with_export_only_verifier_rejected(self):
        """Fix C.1: Purpose requires calculator correctness, but verifier only has export_symbols capability -> reject."""
        registry = {
            "check-calc": {
                "argv": ["python", "-m", "unittest", "test_calculator.py"],
                "modes": ["mutation"],
                "paths": ["calculator.py"],
                "capabilities": ["export_symbols"],
            }
        }
        cap_policy = {"allowed_unit_purposes": {"calculator_correctness": ["calculator_correctness"]}}
        proposal = [make_proposal_unit(alias="u1", purpose="calculator_correctness", paths=["calculator.py"], verifier_ids=["check-calc"])]
        with self.assertRaises(PlannerError) as ctx:
            validate_and_build_canonical_plan(
                proposal, parent_scope=PARENT_SCOPE, approved_verifiers=registry,
                capability_policy=cap_policy,
            )
        self.assertEqual(ctx.exception.code, "verifier_coverage_missing")

    def test_fix_c_2_objective_correctness_with_multiplication_verifier_accepted(self):
        """Fix C.2: Purpose requires calculator correctness and verifier provides it -> accept."""
        registry = {
            "check-calc": {
                "argv": ["python", "-m", "unittest", "test_calculator.py"],
                "modes": ["mutation"],
                "paths": ["calculator.py"],
                "capabilities": ["calculator_correctness", "multiplication_correctness"],
            }
        }
        cap_policy = {"allowed_unit_purposes": {"calculator_correctness": ["calculator_correctness"]}}
        proposal = [make_proposal_unit(alias="u1", purpose="calculator_correctness", paths=["calculator.py"], verifier_ids=["check-calc"])]
        specs, seq = validate_and_build_canonical_plan(
            proposal, parent_scope=PARENT_SCOPE, approved_verifiers=registry,
            capability_policy=cap_policy,
        )
        self.assertEqual(seq, ["wu-001"])
        self.assertEqual(specs[0]["required_capabilities"], ["calculator_correctness"])

    def test_fix_c_3_same_path_and_mode_wrong_capability_rejected(self):
        """Fix C.3: Matching path and mode, but missing required capability -> verifier_coverage_missing."""
        registry = {
            "check-calc": {
                "argv": ["python", "-m", "unittest", "test_calculator.py"],
                "modes": ["mutation"],
                "paths": ["calculator.py"],
                "capabilities": ["style_lint"],
            }
        }
        cap_policy = ["functional_correctness"]
        proposal = [make_proposal_unit(alias="u1", paths=["calculator.py"], verifier_ids=["check-calc"])]
        with self.assertRaises(PlannerError) as ctx:
            validate_and_build_canonical_plan(
                proposal, parent_scope=PARENT_SCOPE, approved_verifiers=registry,
                capability_policy=cap_policy,
            )
        self.assertEqual(ctx.exception.code, "verifier_coverage_missing")

    def test_fix_c_4_correct_capability_wrong_path_rejected(self):
        """Fix C.4: Verifier provides required capability but does not cover the unit path -> reject."""
        registry = {
            "check-helper": {
                "argv": ["python", "-m", "unittest", "test_helper.py"],
                "modes": ["mutation"],
                "paths": ["helper.py"],
                "capabilities": ["functional_correctness"],
            }
        }
        cap_policy = ["functional_correctness"]
        proposal = [make_proposal_unit(alias="u1", paths=["calculator.py"], verifier_ids=["check-helper"])]
        with self.assertRaises(PlannerError) as ctx:
            validate_and_build_canonical_plan(
                proposal, parent_scope=PARENT_SCOPE, approved_verifiers=registry,
                capability_policy=cap_policy,
            )
        self.assertEqual(ctx.exception.code, "verifier_coverage_missing")

    def test_fix_c_5_correct_capability_and_path_wrong_mode_rejected(self):
        """Fix C.5: Verifier provides capability and path, but wrong mode -> reject."""
        registry = {
            "check-calc": {
                "argv": ["python", "-m", "unittest", "test_calculator.py"],
                "modes": ["read_only"],
                "paths": ["calculator.py"],
                "capabilities": ["functional_correctness"],
            }
        }
        cap_policy = ["functional_correctness"]
        proposal = [make_proposal_unit(alias="u1", mode="mutation", paths=["calculator.py"], verifier_ids=["check-calc"])]
        with self.assertRaises(PlannerError) as ctx:
            validate_and_build_canonical_plan(
                proposal, parent_scope=PARENT_SCOPE, approved_verifiers=registry,
                capability_policy=cap_policy,
            )
        self.assertEqual(ctx.exception.code, "verifier_coverage_missing")

    def test_fix_c_6_multiple_required_capabilities_satisfied_by_multiple_verifiers(self):
        """Fix C.6: Multiple required capabilities satisfied across multiple selected verifiers -> accept."""
        registry = {
            "check-calc-syntax": {
                "argv": ["python", "-m", "py_compile", "calculator.py"],
                "modes": ["mutation"],
                "paths": ["calculator.py"],
                "capabilities": ["syntax_valid"],
            },
            "check-calc-tests": {
                "argv": ["python", "-m", "unittest", "test_calculator.py"],
                "modes": ["mutation"],
                "paths": ["calculator.py"],
                "capabilities": ["unit_test_pass"],
            },
        }
        cap_policy = ["syntax_valid", "unit_test_pass"]
        proposal = [make_proposal_unit(
            alias="u1", paths=["calculator.py"],
            verifier_ids=["check-calc-syntax", "check-calc-tests"]
        )]
        specs, seq = validate_and_build_canonical_plan(
            proposal, parent_scope=PARENT_SCOPE, approved_verifiers=registry,
            capability_policy=cap_policy,
        )
        self.assertEqual(seq, ["wu-001"])
        self.assertEqual(specs[0]["required_capabilities"], ["syntax_valid", "unit_test_pass"])

    def test_fix_c_7_one_required_capability_missing_rejected(self):
        """Fix C.7: Two capabilities required, only one provided by chosen verifier -> reject."""
        registry = {
            "check-calc-syntax": {
                "argv": ["python", "-m", "py_compile", "calculator.py"],
                "modes": ["mutation"],
                "paths": ["calculator.py"],
                "capabilities": ["syntax_valid"],
            },
            "check-calc-tests": {
                "argv": ["python", "-m", "unittest", "test_calculator.py"],
                "modes": ["mutation"],
                "paths": ["calculator.py"],
                "capabilities": ["unit_test_pass"],
            },
        }
        cap_policy = ["syntax_valid", "unit_test_pass"]
        proposal = [make_proposal_unit(
            alias="u1", paths=["calculator.py"],
            verifier_ids=["check-calc-syntax"]
        )]
        with self.assertRaises(PlannerError) as ctx:
            validate_and_build_canonical_plan(
                proposal, parent_scope=PARENT_SCOPE, approved_verifiers=registry,
                capability_policy=cap_policy,
            )
        self.assertEqual(ctx.exception.code, "verifier_coverage_missing")

    def test_fix_c_8_planner_injects_unapproved_capability_rejected(self):
        """Fix C.8: Planner tries to introduce unapproved capability ID -> unapproved_capability."""
        registry = {
            "check-calc": {
                "argv": ["python", "-m", "unittest", "test_calculator.py"],
                "modes": ["mutation"],
                "paths": ["calculator.py"],
                "capabilities": ["valid_cap"],
            }
        }
        unit = make_proposal_unit(alias="u1", paths=["calculator.py"], verifier_ids=["check-calc"])
        unit["required_capabilities"] = ["arbitrary_unapproved_cap"]
        with self.assertRaises(PlannerError) as ctx:
            validate_and_build_canonical_plan(
                [unit], parent_scope=PARENT_SCOPE, approved_verifiers=registry,
            )
        self.assertEqual(ctx.exception.code, "unapproved_capability")

    def test_fix_c_9_planner_removes_required_capability_rejected(self):
        """Fix C.9: Planner tries to remove controller-required capability -> required_capability_removed."""
        registry = {
            "check-calc": {
                "argv": ["python", "-m", "unittest", "test_calculator.py"],
                "modes": ["mutation"],
                "paths": ["calculator.py"],
                "capabilities": ["mandatory_audit", "extra_cap"],
            }
        }
        cap_policy = ["mandatory_audit"]
        unit = make_proposal_unit(alias="u1", paths=["calculator.py"], verifier_ids=["check-calc"])
        unit["required_capabilities"] = ["extra_cap"]  # omits mandatory_audit!
        with self.assertRaises(PlannerError) as ctx:
            validate_and_build_canonical_plan(
                [unit], parent_scope=PARENT_SCOPE, approved_verifiers=registry,
                capability_policy=cap_policy,
            )
        self.assertEqual(ctx.exception.code, "required_capability_removed")

    def test_fix_c_10_store_wal_reload_tampered_required_capabilities_fails_closed(self):
        """Fix C.10: Tampering with required_capabilities on reload in store/WAL fails closed."""
        registry = {
            "check-calc": {
                "argv": ["python", "-m", "unittest", "test_calculator.py"],
                "modes": ["mutation"],
                "paths": ["calculator.py"],
                "capabilities": ["calc_correct"],
            }
        }
        cap_policy = ["calc_correct"]
        planner = ScriptedPlanner(behaviors=[{"units": [make_proposal_unit(alias="u1", paths=["calculator.py"], verifier_ids=["check-calc"])]}])
        controller = PlanningController(
            self.store, planner,
            parent_scope=PARENT_SCOPE,
            approved_verifiers=registry,
            task=TASK,
            capability_policy=cap_policy,
        )
        specs, seq = controller.run()
        self.assertEqual(seq, ["wu-001"])

        # Tamper with store state on disk
        import json
        state = copy.deepcopy(self.store.state)
        state["work_unit_planner"]["accepted_plan"][0]["required_capabilities"] = ["uncovered_cap"]
        state["work_unit_planner"]["proposal_digest"] = wup._digest({
            "specs": state["work_unit_planner"]["accepted_plan"],
            "sequence": state["work_unit_planner"]["accepted_sequence"],
        })
        state_file = self.store.directory / "state.json"
        state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")

        store2 = durable.Store(self.root / "runs", "wave3-test-run")
        with self.assertRaises((durable.DurableError, ValueError, WorkUnitError)):
            store2.load()

    # ----------------------------------------------------------------------- Fix 1: Acceptance Persistence Budget Authority
    def test_fix_1_1_task_budget_exceeded_during_persistence_validation_fails_closed(self):
        """Fix 1.1: Task budget = 10, acceptance persistence / durable validation advances clock by 11
        -> NO PLAN_ACCEPTED, NO installed units, budget exhausted, runtime >= 11 persisted.
        """
        clock = AdvancingClock(0.0)
        registry = copy.deepcopy(VERIFIER_REGISTRY)
        planner = ScriptedPlanner(behaviors=[{"units": [make_proposal_unit(alias="u1")]}])
        store = self.create_manager_run("test-fix-1-1", max_runtime_seconds=10.0)
        controller = PlanningController(
            store,
            planner,
            parent_scope=PARENT_SCOPE,
            approved_verifiers=registry,
            task=TASK,
            budgets={"max_runtime_seconds": 10.0},
            clock=clock,
        )

        orig_validate_section = wup.validate_planning_section
        def advancing_validate_section(sec, durable_state=None):
            if isinstance(sec, dict) and sec.get("phase") == "PLAN_ACCEPTED":
                clock.advance(11.0)
            return orig_validate_section(sec, durable_state=durable_state)

        try:
            wup.validate_planning_section = advancing_validate_section
            with self.assertRaises(PlannerBudgetExhausted):
                controller.run()
        finally:
            wup.validate_planning_section = orig_validate_section

        section = store.state.get("work_unit_planner")
        self.assertIsNotNone(section)
        self.assertEqual(section["phase"], "PLAN_REJECTED")
        self.assertEqual(section["rejection_reason"], "budget_exhausted")
        self.assertIsNone(store.state.get("work_units"))
        self.assertGreaterEqual(section["planning_stage_timing"]["enclosing_task_base_runtime"], 11.0)
        self.assertGreaterEqual(store.state["manager"]["runtime_seconds"], 11.0)

    def test_fix_1_2_planning_stage_budget_exceeded_during_persistence_validation_fails_closed(self):
        """Fix 1.2: Planning stage budget = 10, persistence validation advances clock by 11 -> NO PLAN_ACCEPTED."""
        clock = AdvancingClock(0.0)
        registry = copy.deepcopy(VERIFIER_REGISTRY)
        planner = ScriptedPlanner(behaviors=[{"units": [make_proposal_unit(alias="u1")]}])
        controller = PlanningController(
            self.store,
            planner,
            parent_scope=PARENT_SCOPE,
            approved_verifiers=registry,
            task=TASK,
            budgets={"max_runtime_seconds": 100.0},
            planning_stage_budget_seconds=10.0,
            clock=clock,
        )

        orig_validate_section = wup.validate_planning_section
        def advancing_validate_section(sec, durable_state=None):
            if isinstance(sec, dict) and sec.get("phase") == "PLAN_ACCEPTED":
                clock.advance(11.0)
            return orig_validate_section(sec, durable_state=durable_state)

        try:
            wup.validate_planning_section = advancing_validate_section
            with self.assertRaises(PlannerBudgetExhausted):
                controller.run()
        finally:
            wup.validate_planning_section = orig_validate_section

        section = self.store.state.get("work_unit_planner")
        self.assertEqual(section["phase"], "PLAN_REJECTED")
        self.assertEqual(section["rejection_reason"], "budget_exhausted")
        self.assertIsNone(self.store.state.get("work_units"))

    def test_fix_1_3_normal_persistence_within_budget_succeeds(self):
        """Fix 1.3: Validation/persistence remains within budget -> normal PLAN_ACCEPTED."""
        clock = AdvancingClock(0.0)
        registry = copy.deepcopy(VERIFIER_REGISTRY)
        planner = ScriptedPlanner(behaviors=[{"units": [make_proposal_unit(alias="u1")]}])
        controller = PlanningController(
            self.store,
            planner,
            parent_scope=PARENT_SCOPE,
            approved_verifiers=registry,
            task=TASK,
            budgets={"max_runtime_seconds": 50.0},
            planning_stage_budget_seconds=10.0,
            clock=clock,
        )
        specs, seq = controller.run()
        self.assertEqual(seq, ["wu-001"])
        section = self.store.state.get("work_unit_planner")
        self.assertEqual(section["phase"], "PLAN_ACCEPTED")

    def test_fix_1_4_store_wal_reload_never_observes_transient_plan_accepted(self):
        """Fix 1.4: Store / WAL reload after rejected persistence never observes PLAN_ACCEPTED."""
        clock = AdvancingClock(0.0)
        registry = copy.deepcopy(VERIFIER_REGISTRY)
        planner = ScriptedPlanner(behaviors=[{"units": [make_proposal_unit(alias="u1")]}])
        controller = PlanningController(
            self.store,
            planner,
            parent_scope=PARENT_SCOPE,
            approved_verifiers=registry,
            task=TASK,
            budgets={"max_runtime_seconds": 10.0},
            clock=clock,
        )

        orig_validate_section = wup.validate_planning_section
        def advancing_validate_section(sec, durable_state=None):
            if isinstance(sec, dict) and sec.get("phase") == "PLAN_ACCEPTED":
                clock.advance(11.0)
            return orig_validate_section(sec, durable_state=durable_state)

        try:
            wup.validate_planning_section = advancing_validate_section
            with self.assertRaises(PlannerBudgetExhausted):
                controller.run()
        finally:
            wup.validate_planning_section = orig_validate_section

        # Reload store from disk
        store2 = durable.Store(self.root / "runs", "wave3-test-run")
        store2.load()
        section = store2.state.get("work_unit_planner")
        self.assertEqual(section["phase"], "PLAN_REJECTED")
        self.assertEqual(section["rejection_reason"], "budget_exhausted")
        self.assertIsNone(store2.state.get("work_units"))
        for rec in store2.records:
            if "state" in rec and "work_unit_planner" in rec["state"]:
                self.assertNotEqual(rec["state"]["work_unit_planner"].get("phase"), "PLAN_ACCEPTED")

    def test_fix_1_5_persistence_validation_time_not_lost_or_double_counted(self):
        """Fix 1.5: Validation time during persistence is charged cleanly without being lost or double counted."""
        clock = AdvancingClock(0.0)
        registry = copy.deepcopy(VERIFIER_REGISTRY)
        planner = ScriptedPlanner(behaviors=[{"units": [make_proposal_unit(alias="u1")]}])
        store = self.create_manager_run("test-fix-1-5", max_runtime_seconds=50.0)
        controller = PlanningController(
            store,
            planner,
            parent_scope=PARENT_SCOPE,
            approved_verifiers=registry,
            task=TASK,
            budgets={"max_runtime_seconds": 50.0},
            planning_stage_budget_seconds=20.0,
            clock=clock,
        )

        orig_validate_section = wup.validate_planning_section
        def advancing_validate_section(sec, durable_state=None):
            if isinstance(sec, dict) and sec.get("phase") == "PLAN_ACCEPTED":
                clock.advance(3.0)
            return orig_validate_section(sec, durable_state=durable_state)

        try:
            wup.validate_planning_section = advancing_validate_section
            specs, seq = controller.run()
        finally:
            wup.validate_planning_section = orig_validate_section

        section = store.state.get("work_unit_planner")
        self.assertEqual(section["phase"], "PLAN_ACCEPTED")
        self.assertAlmostEqual(section["planning_stage_timing"]["planning_stage_consumed_seconds"], 3.0, places=1)
        self.assertAlmostEqual(store.state["manager"]["runtime_seconds"], 3.0, places=1)
        self.assertAlmostEqual(controller.runtime(), 3.0, places=1)

    def test_fix_1_6_manager_end_to_end_cannot_install_canonical_units_on_persistence_budget_exhausted(self):
        """Fix 1.6: Manager end-to-end path cannot install canonical units when persistence budget exhausted."""
        clock = AdvancingClock(0.0)
        registry = copy.deepcopy(VERIFIER_REGISTRY)
        planner = ScriptedPlanner(behaviors=[{"units": [make_proposal_unit(alias="u1")]}])
        store = self.create_manager_run("test-fix-1-6", max_runtime_seconds=10.0)
        agents = type("MockAgents", (), {"planner": planner})()
        mgr = manager.ManagerLoop(
            store,
            None,
            self.repo,
            None,
            agents,
            durable.EventLog(self.root / "event.log"),
            clock,
        )

        orig_validate_section = wup.validate_planning_section
        def advancing_validate_section(sec, durable_state=None):
            if isinstance(sec, dict) and sec.get("phase") == "PLAN_ACCEPTED":
                clock.advance(11.0)
            return orig_validate_section(sec, durable_state=durable_state)

        try:
            wup.validate_planning_section = advancing_validate_section
            with self.assertRaises(manager.Stop) as ctx:
                mgr.plan_work_units(planner=planner, verifier_registry=registry)
            self.assertEqual(ctx.exception.status, "BUDGET_EXHAUSTED")
        finally:
            wup.validate_planning_section = orig_validate_section

        self.assertIsNone(store.state.get("work_units"))
        self.assertEqual(store.state["work_unit_planner"]["phase"], "PLAN_REJECTED")

    # ----------------------------------------------------------------------- Fix 2: Controller-Owned Capability Authority
    def test_fix_2_1_controller_purpose_requires_capability_with_original_objective(self):
        """Fix 2.1: Controller purpose calculator_correctness requires calculator.multiply.correctness;
        planner objective 'Implement correct multiplication' -> requirement present.
        """
        registry = {
            "check-calc": {
                "argv": ["python", "-m", "unittest", "test_calculator.py"],
                "modes": ["mutation"],
                "paths": ["calculator.py"],
                "capabilities": ["calculator.multiply.correctness"],
            }
        }
        cap_policy = {
            "allowed_unit_purposes": {
                "calculator_correctness": {
                    "required_capabilities": ["calculator.multiply.correctness"]
                }
            }
        }
        proposal = [make_proposal_unit(
            alias="u1",
            objective="Implement correct multiplication",
            purpose="calculator_correctness",
            paths=["calculator.py"],
            verifier_ids=["check-calc"],
        )]
        specs, seq = validate_and_build_canonical_plan(
            proposal, parent_scope=PARENT_SCOPE, approved_verifiers=registry,
            capability_policy=cap_policy,
        )
        self.assertEqual(seq, ["wu-001"])
        self.assertEqual(specs[0]["required_capabilities"], ["calculator.multiply.correctness"])

    def test_fix_2_2_planner_rewriting_objective_leaves_required_capability_unchanged(self):
        """Fix 2.2: Same controller purpose, planner rewrites objective:
        'Implement a product operation for two operands' -> SAME required capability remains present.
        """
        registry = {
            "check-calc": {
                "argv": ["python", "-m", "unittest", "test_calculator.py"],
                "modes": ["mutation"],
                "paths": ["calculator.py"],
                "capabilities": ["calculator.multiply.correctness"],
            }
        }
        cap_policy = {
            "allowed_unit_purposes": {
                "calculator_correctness": {
                    "required_capabilities": ["calculator.multiply.correctness"]
                }
            }
        }
        proposal = [make_proposal_unit(
            alias="u1",
            objective="Implement a product operation for two operands",
            purpose="calculator_correctness",
            paths=["calculator.py"],
            verifier_ids=["check-calc"],
        )]
        specs, seq = validate_and_build_canonical_plan(
            proposal, parent_scope=PARENT_SCOPE, approved_verifiers=registry,
            capability_policy=cap_policy,
        )
        self.assertEqual(seq, ["wu-001"])
        self.assertEqual(specs[0]["required_capabilities"], ["calculator.multiply.correctness"])

    def test_fix_2_3_planner_omits_purpose_fails_closed(self):
        """Fix 2.3: Planner attempts to omit purpose / required capability -> reject / NOT_PROVEN."""
        registry = {
            "check-calc": {
                "argv": ["python", "-m", "unittest", "test_calculator.py"],
                "modes": ["mutation"],
                "paths": ["calculator.py"],
                "capabilities": ["calculator.multiply.correctness"],
            }
        }
        cap_policy = {
            "allowed_unit_purposes": {
                "calculator_correctness": {
                    "required_capabilities": ["calculator.multiply.correctness"]
                }
            }
        }
        proposal = [make_proposal_unit(
            alias="u1",
            objective="Implement correct multiplication",
            purpose=None,
            paths=["calculator.py"],
            verifier_ids=["check-calc"],
        )]
        with self.assertRaises(PlannerError) as ctx:
            validate_and_build_canonical_plan(
                proposal, parent_scope=PARENT_SCOPE, approved_verifiers=registry,
                capability_policy=cap_policy,
            )
        self.assertIn(ctx.exception.code, ("missing_purpose", "verifier_coverage_missing"))

    def test_fix_2_4_planner_invents_unknown_purpose_id_fails_closed(self):
        """Fix 2.4: Planner invents unknown purpose ID -> reject."""
        registry = {
            "check-calc": {
                "argv": ["python", "-m", "unittest", "test_calculator.py"],
                "modes": ["mutation"],
                "paths": ["calculator.py"],
                "capabilities": ["calculator.multiply.correctness"],
            }
        }
        cap_policy = {
            "allowed_unit_purposes": {
                "calculator_correctness": {
                    "required_capabilities": ["calculator.multiply.correctness"]
                }
            }
        }
        proposal = [make_proposal_unit(
            alias="u1",
            purpose="invented_unapproved_purpose",
            paths=["calculator.py"],
            verifier_ids=["check-calc"],
        )]
        with self.assertRaises(PlannerError) as ctx:
            validate_and_build_canonical_plan(
                proposal, parent_scope=PARENT_SCOPE, approved_verifiers=registry,
                capability_policy=cap_policy,
            )
        self.assertEqual(ctx.exception.code, "unknown_purpose")

    def test_fix_2_5_planner_selects_export_verifier_only_fails_coverage(self):
        """Fix 2.5: Planner selects export verifier only (same file + same mode, but missing
        calculator.multiply.correctness) -> reject with verifier_coverage_missing.
        """
        registry = {
            "check-export": {
                "argv": ["python", "-m", "unittest", "test_export.py"],
                "modes": ["mutation"],
                "paths": ["calculator.py"],
                "capabilities": ["exports"],
            },
            "check-calc": {
                "argv": ["python", "-m", "unittest", "test_calculator.py"],
                "modes": ["mutation"],
                "paths": ["calculator.py"],
                "capabilities": ["calculator.multiply.correctness"],
            },
        }
        cap_policy = {
            "allowed_unit_purposes": {
                "calculator_correctness": {
                    "required_capabilities": ["calculator.multiply.correctness"]
                }
            }
        }
        proposal = [make_proposal_unit(
            alias="u1",
            purpose="calculator_correctness",
            paths=["calculator.py"],
            verifier_ids=["check-export"],
        )]
        with self.assertRaises(PlannerError) as ctx:
            validate_and_build_canonical_plan(
                proposal, parent_scope=PARENT_SCOPE, approved_verifiers=registry,
                capability_policy=cap_policy,
            )
        self.assertEqual(ctx.exception.code, "verifier_coverage_missing")

    def test_fix_2_6_planner_selects_correctness_verifier_accepted(self):
        """Fix 2.6: Planner selects correctness verifier -> accept."""
        registry = {
            "check-calc": {
                "argv": ["python", "-m", "unittest", "test_calculator.py"],
                "modes": ["mutation"],
                "paths": ["calculator.py"],
                "capabilities": ["calculator.multiply.correctness"],
            }
        }
        cap_policy = {
            "allowed_unit_purposes": {
                "calculator_correctness": {
                    "required_capabilities": ["calculator.multiply.correctness"]
                }
            }
        }
        proposal = [make_proposal_unit(
            alias="u1",
            purpose="calculator_correctness",
            paths=["calculator.py"],
            verifier_ids=["check-calc"],
        )]
        specs, seq = validate_and_build_canonical_plan(
            proposal, parent_scope=PARENT_SCOPE, approved_verifiers=registry,
            capability_policy=cap_policy,
        )
        self.assertEqual(seq, ["wu-001"])
        self.assertEqual(specs[0]["required_capabilities"], ["calculator.multiply.correctness"])

    def test_fix_2_7_planner_changes_objective_prose_after_plan_accepted_fails_closed(self):
        """Fix 2.7: Planner changes objective prose after PLAN_ACCEPTED -> digest rejects tampering."""
        registry = {
            "check-calc": {
                "argv": ["python", "-m", "unittest", "test_calculator.py"],
                "modes": ["mutation"],
                "paths": ["calculator.py"],
                "capabilities": ["calculator.multiply.correctness"],
            }
        }
        cap_policy = {
            "allowed_unit_purposes": {
                "calculator_correctness": {
                    "required_capabilities": ["calculator.multiply.correctness"]
                }
            }
        }
        planner = ScriptedPlanner(behaviors=[{
            "units": [make_proposal_unit(
                alias="u1",
                objective="Original objective",
                purpose="calculator_correctness",
                paths=["calculator.py"],
                verifier_ids=["check-calc"],
            )]
        }])
        controller = PlanningController(
            self.store,
            planner,
            parent_scope=PARENT_SCOPE,
            approved_verifiers=registry,
            task=TASK,
            capability_policy=cap_policy,
        )
        specs, seq = controller.run()
        self.assertEqual(seq, ["wu-001"])

        tampered_specs = copy.deepcopy(specs)
        tampered_specs[0]["objective"] = "Tampered objective prose"
        digest = self.store.state["work_unit_planner"]["proposal_digest"]

        with self.assertRaises(PlannerError) as ctx:
            validate_canonical_plan(
                tampered_specs,
                seq,
                parent_scope=PARENT_SCOPE,
                approved_verifiers=registry,
                digest=digest,
                capability_policy=cap_policy,
            )
        self.assertIn("Tampering detected", str(ctx.exception))

    def test_fix_2_8_persisted_canonical_work_unit_removes_required_capability_fails_on_reload(self):
        """Fix 2.8: Persisted canonical WorkUnit removes required capability -> Store/WAL reload fails closed."""
        registry = {
            "check-calc": {
                "argv": ["python", "-m", "unittest", "test_calculator.py"],
                "modes": ["mutation"],
                "paths": ["calculator.py"],
                "capabilities": ["calculator.multiply.correctness"],
            }
        }
        cap_policy = {
            "allowed_unit_purposes": {
                "calculator_correctness": {
                    "required_capabilities": ["calculator.multiply.correctness"]
                }
            }
        }
        planner = ScriptedPlanner(behaviors=[{
            "units": [make_proposal_unit(
                alias="u1",
                purpose="calculator_correctness",
                paths=["calculator.py"],
                verifier_ids=["check-calc"],
            )]
        }])
        controller = PlanningController(
            self.store,
            planner,
            parent_scope=PARENT_SCOPE,
            approved_verifiers=registry,
            task=TASK,
            capability_policy=cap_policy,
        )
        specs, seq = controller.run()

        import json
        state = copy.deepcopy(self.store.state)
        state["work_unit_planner"]["accepted_plan"][0]["required_capabilities"] = []
        state["work_unit_planner"]["proposal_digest"] = wup._digest({
            "specs": state["work_unit_planner"]["accepted_plan"],
            "sequence": state["work_unit_planner"]["accepted_sequence"],
        })
        state_file = self.store.directory / "state.json"
        state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")

        store2 = durable.Store(self.root / "runs", "wave3-test-run")
        with self.assertRaises((durable.DurableError, ValueError, WorkUnitError)):
            store2.load()

    def test_fix_2_9_absent_capability_policy_for_semantic_coverage_unit_fails_closed(self):
        """Fix 2.9: Controller capability policy absent for a Wave 3 unit that requires semantic coverage
        -> fail closed, not required_capabilities=[].
        """
        registry = {
            "check-calc": {
                "argv": ["python", "-m", "unittest", "test_calculator.py"],
                "modes": ["mutation"],
                "paths": ["calculator.py"],
                "capabilities": ["calculator.multiply.correctness"],
            }
        }
        proposal = [make_proposal_unit(
            alias="u1",
            mode="mutation",
            paths=["calculator.py"],
            verifier_ids=["check-calc"],
            semantic_coverage_required=True,
        )]
        with self.assertRaises(PlannerError) as ctx:
            validate_and_build_canonical_plan(
                proposal,
                parent_scope=PARENT_SCOPE,
                approved_verifiers=registry,
                capability_policy=None,
            )
        self.assertEqual(ctx.exception.code, "verifier_coverage_missing")

    def test_fix_2_10_legacy_fixed_wave2_plan_without_capability_policy_remains_compatible(self):
        """Fix 2.10: Legacy/fixed Wave 2 plan without Wave 3 capability policy remains compatible."""
        spec = {
            "unit_id": "wu-001",
            "objective": "Legacy fixed unit",
            "dependencies": [],
            "mode": "mutation",
            "scope": {
                "allowed_paths": ["calculator.py"],
                "forbidden_paths": [],
            },
            "verifier_ids": ["check-calc"],
            "evidence_inputs": [],
            "limits": {"max_attempts": 2, "timeout_seconds": 60.0},
        }
        valid_spec = work_units.validate_unit_spec(spec)
        self.assertNotIn("required_capabilities", valid_spec)

        section = work_units.initial_work_units_section()
        work_units.add_unit_to_section(section, spec)
        section["sequence"] = ["wu-001"]
        work_units.seal_unit_state(section["units"]["wu-001"])
        work_units.validate_section(section)
        self.assertEqual(section["sequence"], ["wu-001"])


class TestCase2G(unittest.TestCase):
    """Case 2G: Bounded auto-planned mutation WorkUnits without capability policy fail closed."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.repo, self.store = fixture(self.root, capability_policy=None)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_case_2g_1_manager_path_no_policy_mutation_rejected(self):
        """Case 2G.1: Manager path with NO capability_policy, auto-planned mutation unit:
        purpose = calculator_correctness, check-export only
        -> plan REJECTED
        -> NO PLAN_ACCEPTED written
        -> NO executable WorkUnit installed
        """
        registry = {
            "check-export": {
                "argv": ["python", "-c", "import calculator; assert hasattr(calculator, 'multiply')"],
                "modes": ["mutation"],
                "paths": ["calculator.py"],
                "capabilities": ["calculator.export"],
            }
        }
        proposal = [make_proposal_unit(
            alias="u1",
            objective="Implement correct multiplication",
            purpose="calculator_correctness",
            mode="mutation",
            paths=["calculator.py"],
            verifier_ids=["check-export"],
        )]
        planner = ScriptedPlanner(behaviors=[{"units": proposal}])
        agents = type("Agents", (), {"planner": planner})()
        run_id = "test-case-2g-1"
        store = durable.Store(self.root / "runs", run_id)
        manager.create_run(
            store, self.repo, TASK, [["python", "-m", "unittest", "test_calculator.py"]],
            CONFIG, criteria=["AC1"], allow=["calculator.py"],
            work_unit_planning=True, verifier_registry=registry,
        )
        log = harness.EventLog(self.root / "event_2g_1.jsonl")
        loop = manager.ManagerLoop(store, None, self.repo, None, agents, log)

        with self.assertRaises(PlannerError) as ctx:
            loop.plan_work_units(planner=planner, verifier_registry=registry, capability_policy=None)

        self.assertIn(ctx.exception.code, ("verifier_coverage_missing", "missing_capability_policy"))
        # NO PLAN_ACCEPTED written
        section = store.state.get("work_unit_planner")
        self.assertIsNotNone(section)
        self.assertEqual(section.get("phase"), "PLAN_REJECTED")
        self.assertNotEqual(section.get("phase"), "PLAN_ACCEPTED")
        # NO executable WorkUnit installed
        self.assertNotIn("work_units", store.state)

    def test_case_2g_2_manager_path_no_policy_purpose_omitted_rejected(self):
        """Case 2G.2: Same Manager path, but purpose OMITTED:
        -> plan REJECTED
        -> NO PLAN_ACCEPTED written
        -> NO executable WorkUnit installed
        """
        registry = {
            "check-export": {
                "argv": ["python", "-c", "import calculator; assert hasattr(calculator, 'multiply')"],
                "modes": ["mutation"],
                "paths": ["calculator.py"],
                "capabilities": ["calculator.export"],
            }
        }
        proposal = [make_proposal_unit(
            alias="u1",
            objective="Implement correct multiplication",
            purpose=None,
            mode="mutation",
            paths=["calculator.py"],
            verifier_ids=["check-export"],
        )]
        planner = ScriptedPlanner(behaviors=[{"units": proposal}])
        agents = type("Agents", (), {"planner": planner})()
        run_id = "test-case-2g-2"
        store = durable.Store(self.root / "runs", run_id)
        manager.create_run(
            store, self.repo, TASK, [["python", "-m", "unittest", "test_calculator.py"]],
            CONFIG, criteria=["AC1"], allow=["calculator.py"],
            work_unit_planning=True, verifier_registry=registry,
        )
        log = harness.EventLog(self.root / "event_2g_2.jsonl")
        loop = manager.ManagerLoop(store, None, self.repo, None, agents, log)

        with self.assertRaises(PlannerError) as ctx:
            loop.plan_work_units(planner=planner, verifier_registry=registry, capability_policy=None)

        self.assertIn(ctx.exception.code, ("verifier_coverage_missing", "missing_capability_policy", "missing_purpose"))
        # NO PLAN_ACCEPTED written
        section = store.state.get("work_unit_planner")
        self.assertIsNotNone(section)
        self.assertEqual(section.get("phase"), "PLAN_REJECTED")
        # NO executable WorkUnit installed
        self.assertNotIn("work_units", store.state)

    def test_case_2g_3_controller_path_no_policy_mutation_rejected(self):
        """Case 2G.3: Direct PlanningController path with NO policy + mutation unit:
        -> rejected / NOT_PROVEN / verifier_coverage_missing
        """
        registry = {
            "check-export": {
                "argv": ["python", "-c", "import calculator; assert hasattr(calculator, 'multiply')"],
                "modes": ["mutation"],
                "paths": ["calculator.py"],
                "capabilities": ["calculator.export"],
            }
        }
        proposal = [make_proposal_unit(
            alias="u1",
            objective="Implement correct multiplication",
            purpose="calculator_correctness",
            mode="mutation",
            paths=["calculator.py"],
            verifier_ids=["check-export"],
        )]
        planner = ScriptedPlanner(behaviors=[{"units": proposal}])
        controller = PlanningController(
            self.store,
            planner,
            parent_scope=PARENT_SCOPE,
            approved_verifiers=registry,
            task=TASK,
            capability_policy=None,
        )

        with self.assertRaises(PlannerError) as ctx:
            controller.run()

        self.assertIn(ctx.exception.code, ("verifier_coverage_missing", "missing_capability_policy"))
        section = load_planning_state(self.store.state)
        self.assertEqual(section["phase"], "PLAN_REJECTED")
        self.assertNotIn("work_units", self.store.state)

    def test_case_2g_4_positive_control_capability_policy_plan_accepted(self):
        """Case 2G.4: Valid positive control:
        capability_policy defines:
        calculator_correctness -> calculator.multiply.correctness
        planner selects purpose = calculator_correctness
        check-correctness provides required capability
        -> PLAN_ACCEPTED
        -> executable WorkUnit installed with required_capabilities bound
        """
        registry = {
            "check-correctness": {
                "argv": ["python", "-m", "unittest", "test_calculator.py"],
                "modes": ["mutation"],
                "paths": ["calculator.py"],
                "capabilities": ["calculator.multiply.correctness"],
            }
        }
        cap_policy = {
            "allowed_unit_purposes": {
                "calculator_correctness": {
                    "required_capabilities": ["calculator.multiply.correctness"]
                }
            }
        }
        proposal = [make_proposal_unit(
            alias="u1",
            objective="Implement correct multiplication",
            purpose="calculator_correctness",
            mode="mutation",
            paths=["calculator.py"],
            verifier_ids=["check-correctness"],
        )]
        planner = ScriptedPlanner(behaviors=[{"units": proposal}])
        agents = type("Agents", (), {"planner": planner})()
        run_id = "test-case-2g-4"
        store = durable.Store(self.root / "runs", run_id)
        manager.create_run(
            store, self.repo, TASK, [["python", "-m", "unittest", "test_calculator.py"]],
            CONFIG, criteria=["AC1"], allow=["calculator.py"],
            work_unit_planning=True, verifier_registry=registry,
            capability_policy=cap_policy,
        )
        log = harness.EventLog(self.root / "event_2g_4.jsonl")
        loop = manager.ManagerLoop(store, None, self.repo, None, agents, log)

        specs, seq = loop.plan_work_units(planner=planner, verifier_registry=registry, capability_policy=cap_policy)

        self.assertEqual(seq, ["wu-001"])
        self.assertEqual(store.state["work_unit_planner"]["phase"], "PLAN_ACCEPTED")
        self.assertIn("work_units", store.state)
        wu = store.state["work_units"]["units"]["wu-001"]
        self.assertEqual(wu["spec"]["required_capabilities"], ["calculator.multiply.correctness"])

    def test_case_2g_5_wrong_same_file_verifier_lacks_capability_rejected(self):
        """Case 2G.5: Wrong same-file verifier:
        policy exists:
        calculator_correctness -> calculator.multiply.correctness
        planner selects purpose = calculator_correctness
        verifier selected is check-export (lacks capability)
        -> REJECTED (verifier_coverage_missing)
        """
        registry = {
            "check-export": {
                "argv": ["python", "-c", "import calculator; assert hasattr(calculator, 'multiply')"],
                "modes": ["mutation"],
                "paths": ["calculator.py"],
                "capabilities": ["calculator.export"],
            },
            "check-correctness": {
                "argv": ["python", "-m", "unittest", "test_calculator.py"],
                "modes": ["mutation"],
                "paths": ["calculator.py"],
                "capabilities": ["calculator.multiply.correctness"],
            },
        }
        cap_policy = {
            "allowed_unit_purposes": {
                "calculator_correctness": {
                    "required_capabilities": ["calculator.multiply.correctness"]
                }
            }
        }
        proposal = [make_proposal_unit(
            alias="u1",
            objective="Implement correct multiplication",
            purpose="calculator_correctness",
            mode="mutation",
            paths=["calculator.py"],
            verifier_ids=["check-export"],
        )]
        planner = ScriptedPlanner(behaviors=[{"units": proposal}])
        agents = type("Agents", (), {"planner": planner})()
        run_id = "test-case-2g-5"
        store = durable.Store(self.root / "runs", run_id)
        manager.create_run(
            store, self.repo, TASK, [["python", "-m", "unittest", "test_calculator.py"]],
            CONFIG, criteria=["AC1"], allow=["calculator.py"],
            work_unit_planning=True, verifier_registry=registry,
            capability_policy=cap_policy,
        )
        log = harness.EventLog(self.root / "event_2g_5.jsonl")
        loop = manager.ManagerLoop(store, None, self.repo, None, agents, log)

        with self.assertRaises(PlannerError) as ctx:
            loop.plan_work_units(planner=planner, verifier_registry=registry, capability_policy=cap_policy)

        self.assertIn(ctx.exception.code, ("verifier_coverage_missing", "second_replan_refused"))
        self.assertEqual(store.state["work_unit_planner"]["phase"], "PLAN_REJECTED")
        self.assertNotIn("work_units", store.state)

    def test_case_2g_6_store_wal_tampering_mutation_removes_required_capabilities_fails_closed(self):
        """Case 2G.6: Store/WAL tampering:
        accepted Wave 3 mutation plan has required_capabilities removed/emptied:
        -> reload FAILS CLOSED
        -> refuses execution
        """
        registry = {
            "check-correctness": {
                "argv": ["python", "-m", "unittest", "test_calculator.py"],
                "modes": ["mutation"],
                "paths": ["calculator.py"],
                "capabilities": ["calculator.multiply.correctness"],
            }
        }
        cap_policy = {
            "allowed_unit_purposes": {
                "calculator_correctness": {
                    "required_capabilities": ["calculator.multiply.correctness"]
                }
            }
        }
        proposal = [make_proposal_unit(
            alias="u1",
            purpose="calculator_correctness",
            mode="mutation",
            paths=["calculator.py"],
            verifier_ids=["check-correctness"],
        )]
        planner = ScriptedPlanner(behaviors=[{"units": proposal}])
        run_id = "test-case-2g-6"
        store = durable.Store(self.root / "runs", run_id)
        manager.create_run(
            store, self.repo, TASK, [["python", "-m", "unittest", "test_calculator.py"]],
            CONFIG, criteria=["AC1"], allow=["calculator.py"],
            work_unit_planning=True, verifier_registry=registry,
            capability_policy=cap_policy,
        )
        agents = type("Agents", (), {"planner": planner})()
        log = harness.EventLog(self.root / "event_2g_6.jsonl")
        loop = manager.ManagerLoop(store, None, self.repo, None, agents, log)
        loop.plan_work_units(planner=planner, verifier_registry=registry, capability_policy=cap_policy)

        self.assertEqual(store.state["work_unit_planner"]["phase"], "PLAN_ACCEPTED")

        # Tamper: empty required_capabilities on disk
        state = copy.deepcopy(store.state)
        state["work_unit_planner"]["accepted_plan"][0]["required_capabilities"] = []
        if "work_units" in state:
            state["work_units"]["units"]["wu-001"]["spec"]["required_capabilities"] = []
            work_units._reseal_section(state["work_units"])
        state["work_unit_planner"]["proposal_digest"] = wup._digest({
            "specs": state["work_unit_planner"]["accepted_plan"],
            "sequence": state["work_unit_planner"]["accepted_sequence"],
        })
        state_file = store.directory / "state.json"
        state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")

        store2 = durable.Store(self.root / "runs", run_id)
        with self.assertRaises((durable.DurableError, ValueError, WorkUnitError)):
            store2.load()

    def test_case_2g_7_legacy_fixed_wave2_unit_without_capability_policy_compatible(self):
        """Case 2G.7: Legacy / fixed Wave 2 WorkUnit without Wave 3 capability policy:
        -> remains compatible
        -> executes normally
        """
        registry = {
            "check-calc": {
                "argv": ["python", "-m", "unittest", "test_calculator.py"],
                "modes": ["mutation"],
                "paths": ["calculator.py"],
            }
        }
        spec = {
            "unit_id": "wu-001",
            "objective": "Legacy fixed Wave 2 unit without capability policy",
            "dependencies": [],
            "mode": "mutation",
            "scope": {
                "allowed_paths": ["calculator.py"],
                "forbidden_paths": [],
            },
            "verifier_ids": ["check-calc"],
            "evidence_inputs": [],
            "limits": {"max_attempts": 2, "timeout_seconds": 60.0},
        }
        # Validates under Wave 1/2 contracts without required_capabilities
        valid_spec = work_units.validate_unit_spec(spec)
        self.assertNotIn("required_capabilities", valid_spec)

        section = work_units.initial_work_units_section()
        work_units.add_unit_to_section(section, spec)
        section["sequence"] = ["wu-001"]
        work_units.seal_unit_state(section["units"]["wu-001"])
        work_units.validate_section(section)
        self.assertEqual(section["sequence"], ["wu-001"])

        # Execute through Wave 2 scheduler
        run_id = "test-case-2g-7"
        store = durable.Store(self.root / "runs", run_id)
        manager.create_run(
            store, self.repo, TASK, [["python", "-m", "unittest", "test_calculator.py"]],
            CONFIG, criteria=["AC1"], allow=["calculator.py"],
        )
        store.commit(work_units=section)

        executor = ScriptedExecutor()
        executor.set_behavior("wu-001", {"writes": {"calculator.py": "def multiply(a, b):\n    return a * b\n"}})

        agents = type("Agents", (), {"executor": executor})()
        res = manager.execute_work_units(store, agents=agents, verifier_registry=registry)
        self.assertEqual(res["status"], "MILESTONE_READY")
        self.assertEqual(store.state["work_units"]["units"]["wu-001"]["status"], "UNIT_VERIFIED")


if __name__ == "__main__":
    unittest.main()
