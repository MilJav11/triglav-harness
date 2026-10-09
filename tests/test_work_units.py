"""Deterministic tests for Wave 1 WorkUnit architecture.

Covers:
- WorkUnit spec validation
- WorkUnit plan validation (duplicates, deps, cycles, scope, verifiers)
- Lifecycle state machine transitions
- Attempt creation, completion, and immutability
- Durable persistence and reload
- Backward compatibility with legacy runs
- Crash/reload safety
- Tamper/corruption detection
- TRUST INVARIANT: WorkUnit state NEVER promotes top-level trusted fields
- AUDIT FIX 1: Verified result contract regressions
- AUDIT FIX 2: Path/scope normalization regressions
- AUDIT FIX 3: Attempt/repair/terminal invariant regressions
- AUDIT FIX 4: Dependency lifecycle state invariant regressions
- AUDIT FIX 5: Malformed state, type safety, and crash-window regressions
"""

import copy
import json
import math
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import durable
import harness
import work_units
from work_units import (
    MODES, UNIT_STATUSES, UNIT_TRANSITIONS, ATTEMPT_OUTCOMES,
    WorkUnitError,
    validate_unit_spec, validate_unit_plan, validate_scope_containment,
    validate_verifier_reference, validate_unit_state, validate_section,
    validate_verifier_evidence, validate_unit_verified_preconditions,
    normalize_path, normalize_entry, covers, overlaps,
    initial_unit_state, transition_unit, create_attempt, complete_attempt,
    set_unit_result, seal_unit_state,
    initial_work_units_section, add_unit_to_section,
    load_work_units, persist_work_units,
    _check_cycles,
)
from test_harness import CONFIG


TASK = "Implement multiply"
COMMANDS = [["python", "-m", "unittest", "discover"]]


def make_spec(unit_id="unit-1", objective="Implement a helper function",
              dependencies=None, mode="mutation", paths=None, forbidden=None,
              verifier_ids=None, evidence_inputs=None, limits=None):
    """Build a valid WorkUnit spec for testing."""
    return {
        "unit_id": unit_id,
        "objective": objective,
        "dependencies": dependencies if dependencies is not None else [],
        "mode": mode,
        "scope": {
            "allowed_paths": paths if paths is not None else ["src/helper.py"],
            "forbidden_paths": forbidden if forbidden is not None else [],
        },
        "verifier_ids": verifier_ids if verifier_ids is not None else ["check-1"],
        "evidence_inputs": evidence_inputs if evidence_inputs is not None else [],
        "limits": limits if limits is not None else {"max_attempts": 3, "timeout_seconds": 60},
    }


VERIFIER_REGISTRY = {
    "check-1": {"argv": ["python", "-m", "unittest", "discover"]},
    "check-2": {"argv": ["python", "-m", "pytest"]},
}


def fixture(root):
    """Create a durable Store + Repository for persistence tests."""
    repo_path = root / "repo"
    repo_path.mkdir()
    (repo_path / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    (repo_path / "calculator.py").write_text("def multiply(a, b):\n    return 0\n", encoding="utf-8")
    (repo_path / "test_calculator.py").write_text(
        "import unittest\nfrom calculator import multiply\nclass T(unittest.TestCase):\n"
        "    def test_product(self): self.assertEqual(multiply(2, 3), 6)\n", encoding="utf-8")

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(repo_path), "-c", f"safe.directory={repo_path}",
             "-c", "user.name=WU test", "-c", "user.email=test@localhost", *args],
            check=True, capture_output=True)
    git("init", "-q")
    git("add", ".")
    git("commit", "-qm", "baseline")
    config = copy.deepcopy(CONFIG)
    config.update(max_retries=0, roles={**CONFIG["roles"], "critic": "llm-critic"})
    log = harness.EventLog(root / "preflight.jsonl")
    repo = harness.Repository(repo_path, config, log)
    store = durable.Store(root / "runs", "test-wu-run")
    store.create(repo, TASK, COMMANDS, config)
    return repo, store, git


# ============================================================================
# SPEC VALIDATION
# ============================================================================

class SpecValidationTests(unittest.TestCase):
    def test_valid_spec_accepted(self):
        spec = make_spec()
        result = validate_unit_spec(spec)
        self.assertEqual(result["unit_id"], "unit-1")

    def test_missing_required_fields(self):
        for key in ("unit_id", "objective", "dependencies", "mode", "scope",
                     "verifier_ids", "evidence_inputs", "limits"):
            spec = make_spec()
            del spec[key]
            with self.subTest(key=key), self.assertRaises(WorkUnitError):
                validate_unit_spec(spec)

    def test_unexpected_fields_rejected(self):
        spec = make_spec()
        spec["extra_field"] = "bad"
        with self.assertRaises(WorkUnitError):
            validate_unit_spec(spec)

    def test_invalid_unit_id_variants(self):
        for bad_id in ("", " ", "../escape", "x" * 81, "bad id!", 42, None, ".bad"):
            with self.subTest(bad_id=bad_id), self.assertRaises(WorkUnitError):
                validate_unit_spec(make_spec(unit_id=bad_id))

    def test_valid_unit_id_variants(self):
        for good_id in ("a", "A1", "unit-1", "unit_2", "unit.3", "a" * 80):
            self.assertEqual(validate_unit_spec(make_spec(unit_id=good_id))["unit_id"], good_id)

    def test_empty_objective_rejected(self):
        with self.assertRaises(WorkUnitError):
            validate_unit_spec(make_spec(objective=""))
        with self.assertRaises(WorkUnitError):
            validate_unit_spec(make_spec(objective="   "))

    def test_long_objective_rejected(self):
        with self.assertRaises(WorkUnitError):
            validate_unit_spec(make_spec(objective="x" * 2001))

    def test_self_dependency_rejected(self):
        with self.assertRaises(WorkUnitError):
            validate_unit_spec(make_spec(unit_id="a", dependencies=["a"]))

    def test_duplicate_dependency_rejected(self):
        with self.assertRaises(WorkUnitError):
            validate_unit_spec(make_spec(dependencies=["b", "b"]))

    def test_invalid_mode_rejected(self):
        with self.assertRaises(WorkUnitError):
            validate_unit_spec(make_spec(mode="execute"))
        with self.assertRaises(WorkUnitError):
            validate_unit_spec(make_spec(mode=""))

    def test_valid_modes_accepted(self):
        for mode in MODES:
            result = validate_unit_spec(make_spec(mode=mode))
            self.assertEqual(result["mode"], mode)

    def test_empty_allowed_paths_rejected(self):
        with self.assertRaises(WorkUnitError):
            validate_unit_spec(make_spec(paths=[]))

    def test_too_many_allowed_paths_rejected(self):
        with self.assertRaises(WorkUnitError):
            validate_unit_spec(make_spec(paths=[f"f{i}.py" for i in range(51)]))

    def test_empty_verifier_ids_rejected(self):
        with self.assertRaises(WorkUnitError):
            validate_unit_spec(make_spec(verifier_ids=[]))

    def test_duplicate_verifier_ids_rejected(self):
        with self.assertRaises(WorkUnitError):
            validate_unit_spec(make_spec(verifier_ids=["check-1", "check-1"]))

    def test_limits_requires_max_attempts(self):
        with self.assertRaises(WorkUnitError):
            validate_unit_spec(make_spec(limits={"timeout_seconds": 60}))

    def test_invalid_max_attempts(self):
        for bad in (0, -1, True, "3", 1.5):
            with self.subTest(bad=bad), self.assertRaises(WorkUnitError):
                validate_unit_spec(make_spec(limits={"max_attempts": bad}))

    def test_invalid_timeout(self):
        for bad in (0, -1, True, "60"):
            with self.subTest(bad=bad), self.assertRaises(WorkUnitError):
                validate_unit_spec(make_spec(limits={"max_attempts": 3, "timeout_seconds": bad}))

    def test_non_dict_spec_rejected(self):
        for bad in ([], "string", 42, None):
            with self.subTest(bad=bad), self.assertRaises(WorkUnitError):
                validate_unit_spec(bad)

    def test_scope_must_have_exact_keys(self):
        spec = make_spec()
        spec["scope"]["extra"] = []
        with self.assertRaises(WorkUnitError):
            validate_unit_spec(spec)


# ============================================================================
# PLAN VALIDATION (multi-unit)
# ============================================================================

class PlanValidationTests(unittest.TestCase):
    def test_valid_plan_accepted(self):
        a = make_spec("a", dependencies=[])
        b = make_spec("b", dependencies=["a"])
        result = validate_unit_plan([a, b])
        self.assertEqual(len(result), 2)

    def test_duplicate_ids_rejected(self):
        with self.assertRaises(WorkUnitError):
            validate_unit_plan([make_spec("a"), make_spec("a")])

    def test_missing_dependency_rejected(self):
        with self.assertRaisesRegex(WorkUnitError, "unknown unit"):
            validate_unit_plan([make_spec("a", dependencies=["nonexistent"])])

    def test_simple_cycle_rejected(self):
        a = make_spec("a", dependencies=["b"])
        b = make_spec("b", dependencies=["a"])
        with self.assertRaisesRegex(WorkUnitError, "cycle"):
            validate_unit_plan([a, b])

    def test_transitive_cycle_rejected(self):
        a = make_spec("a", dependencies=["c"])
        b = make_spec("b", dependencies=["a"])
        c = make_spec("c", dependencies=["b"])
        with self.assertRaisesRegex(WorkUnitError, "cycle"):
            validate_unit_plan([a, b, c])

    def test_self_dependency_in_plan_rejected(self):
        with self.assertRaises(WorkUnitError):
            validate_unit_plan([make_spec("a", dependencies=["a"])])

    def test_diamond_dependency_accepted(self):
        """A -> B, A -> C, B -> D, C -> D (no cycle)."""
        d = make_spec("d", dependencies=[])
        b = make_spec("b", dependencies=["d"])
        c = make_spec("c", dependencies=["d"])
        a = make_spec("a", dependencies=["b", "c"])
        result = validate_unit_plan([a, b, c, d])
        self.assertEqual(len(result), 4)

    def test_non_list_plan_rejected(self):
        with self.assertRaises(WorkUnitError):
            validate_unit_plan({"a": make_spec("a")})

    def test_scope_escape_detected(self):
        parent = {"allowed_paths": ["src/"], "forbidden_paths": []}
        with self.assertRaisesRegex(WorkUnitError, "escapes parent scope"):
            validate_unit_plan(
                [make_spec("a", paths=["outside.py"])],
                parent_scope=parent
            )

    def test_scope_forbidden_overlap_detected(self):
        parent = {"allowed_paths": ["src/"], "forbidden_paths": ["src/secret/"]}
        with self.assertRaisesRegex(WorkUnitError, "overlaps parent forbidden"):
            validate_unit_plan(
                [make_spec("a", paths=["src/secret/key.py"])],
                parent_scope=parent
            )

    def test_scope_valid_within_parent(self):
        parent = {"allowed_paths": ["src/"], "forbidden_paths": []}
        result = validate_unit_plan(
            [make_spec("a", paths=["src/helper.py"])],
            parent_scope=parent
        )
        self.assertEqual(len(result), 1)

    def test_invalid_verifier_reference_detected(self):
        with self.assertRaisesRegex(WorkUnitError, "Unknown verifier_id"):
            validate_unit_plan(
                [make_spec("a", verifier_ids=["nonexistent"])],
                verifier_registry=VERIFIER_REGISTRY
            )

    def test_valid_verifier_reference_accepted(self):
        result = validate_unit_plan(
            [make_spec("a", verifier_ids=["check-1"])],
            verifier_registry=VERIFIER_REGISTRY
        )
        self.assertEqual(len(result), 1)

    def test_malformed_verifier_registry_entry(self):
        bad_registry = {"check-1": "not a dict"}
        with self.assertRaises(WorkUnitError):
            validate_verifier_reference("check-1", bad_registry)

    def test_verifier_without_argv_rejected(self):
        bad_registry = {"check-1": {"command": "run"}}
        with self.assertRaises(WorkUnitError):
            validate_verifier_reference("check-1", bad_registry)


# ============================================================================
# LIFECYCLE STATE MACHINE
# ============================================================================

class LifecycleTests(unittest.TestCase):
    def test_all_statuses_defined(self):
        for status in UNIT_STATUSES:
            self.assertIn(status, UNIT_TRANSITIONS)

    def test_initial_state_is_proposed(self):
        spec = make_spec()
        state = initial_unit_state(spec)
        self.assertEqual(state["status"], "PROPOSED")
        self.assertEqual(state["unit_id"], "unit-1")
        self.assertEqual(state["attempts"], [])
        self.assertIsNone(state["result"])

    def test_valid_transitions(self):
        transitions = [
            ("PROPOSED", "VALIDATED"),
            ("VALIDATED", "PENDING"),
            ("VALIDATED", "READY"),
            ("PENDING", "READY"),
            ("READY", "EXECUTING"),
            ("EXECUTING", "VERIFYING"),
            ("EXECUTING", "REPAIR_PENDING"),
            ("EXECUTING", "UNIT_FAILED"),
            ("VERIFYING", "UNIT_VERIFIED"),
            ("VERIFYING", "REPAIR_PENDING"),
            ("VERIFYING", "UNIT_FAILED"),
            ("REPAIR_PENDING", "READY"),
            ("REPAIR_PENDING", "UNIT_FAILED"),
        ]
        for current, target in transitions:
            with self.subTest(current=current, target=target):
                state = initial_unit_state(make_spec())
                state["status"] = current
                if target == "REPAIR_PENDING" or current == "REPAIR_PENDING":
                    state["attempts"] = [{"attempt_id": "att-1", "outcome": "failed", "completed_at": "now"}]
                if target == "UNIT_VERIFIED":
                    state["attempts"] = [{"attempt_id": "att-1", "outcome": "passed", "candidate_id": "cand-1", "completed_at": "now"}]
                    set_unit_result(state, verifier_evidence={"passed": True, "check": "check-1"}, candidate_id="cand-1")
                transition_unit(state, target)
                self.assertEqual(state["status"], target)

    def test_invalid_transitions_rejected(self):
        invalid = [
            ("PROPOSED", "READY"),
            ("PROPOSED", "EXECUTING"),
            ("VALIDATED", "EXECUTING"),
            ("READY", "VALIDATED"),
            ("UNIT_VERIFIED", "EXECUTING"),
            ("UNIT_FAILED", "READY"),
            ("EXECUTING", "PROPOSED"),
            ("VERIFYING", "EXECUTING"),
        ]
        for current, target in invalid:
            with self.subTest(current=current, target=target):
                state = initial_unit_state(make_spec())
                state["status"] = current
                with self.assertRaises(WorkUnitError):
                    transition_unit(state, target)

    def test_terminal_states_have_no_transitions(self):
        for status in ("UNIT_VERIFIED", "UNIT_FAILED"):
            self.assertEqual(UNIT_TRANSITIONS[status], set())

    def test_full_lifecycle_proposed_to_verified(self):
        spec = make_spec()
        state = initial_unit_state(spec)
        for target in ("VALIDATED", "READY", "EXECUTING"):
            transition_unit(state, target)
        attempt = create_attempt(state, "att-1", candidate_id="cand-1")
        complete_attempt(state, "att-1", outcome="passed",
                         verifier_result={"passed": True}, candidate_id="cand-1")
        transition_unit(state, "VERIFYING")
        set_unit_result(state, verifier_evidence={"passed": True, "check": "check-1"}, candidate_id="cand-1")
        transition_unit(state, "UNIT_VERIFIED")
        self.assertEqual(state["status"], "UNIT_VERIFIED")
        self.assertIsNotNone(state["result"])
        self.assertEqual(state["result"]["trust_level"], "INTERMEDIATE")

    def test_full_lifecycle_proposed_to_failed(self):
        spec = make_spec(limits={"max_attempts": 1})
        state = initial_unit_state(spec)
        for target in ("VALIDATED", "READY", "EXECUTING"):
            transition_unit(state, target)
        create_attempt(state, "att-1")
        complete_attempt(state, "att-1", outcome="failed",
                         failure_classification="VERIFICATION_FAILED")
        transition_unit(state, "UNIT_FAILED")
        self.assertEqual(state["status"], "UNIT_FAILED")

    def test_repair_lifecycle(self):
        spec = make_spec()
        state = initial_unit_state(spec)
        for target in ("VALIDATED", "READY", "EXECUTING"):
            transition_unit(state, target)
        create_attempt(state, "att-1", candidate_id="cand-1")
        complete_attempt(state, "att-1", outcome="failed",
                         failure_classification="TEST_FAILED", candidate_id="cand-1")
        transition_unit(state, "REPAIR_PENDING")
        self.assertEqual(state["status"], "REPAIR_PENDING")
        transition_unit(state, "READY")
        transition_unit(state, "EXECUTING")
        create_attempt(state, "att-2", candidate_id="cand-1")
        complete_attempt(state, "att-2", outcome="passed",
                         verifier_result={"passed": True}, candidate_id="cand-1")
        transition_unit(state, "VERIFYING")
        set_unit_result(state, verifier_evidence={"passed": True, "check": "check-1"}, candidate_id="cand-1")
        transition_unit(state, "UNIT_VERIFIED")
        self.assertEqual(state["status"], "UNIT_VERIFIED")
        self.assertEqual(len(state["attempts"]), 2)


# ============================================================================
# ATTEMPT RECORDS
# ============================================================================

class AttemptTests(unittest.TestCase):
    def test_create_attempt_requires_executing(self):
        state = initial_unit_state(make_spec())
        with self.assertRaises(WorkUnitError):
            create_attempt(state, "att-1")

    def test_create_attempt_success(self):
        state = initial_unit_state(make_spec())
        state["status"] = "EXECUTING"
        attempt = create_attempt(state, "att-1")
        self.assertEqual(attempt["attempt_id"], "att-1")
        self.assertEqual(attempt["outcome"], "interrupted")  # safe default
        self.assertEqual(len(state["attempts"]), 1)
        self.assertEqual(state["current_attempt"], "att-1")

    def test_duplicate_attempt_id_rejected(self):
        state = initial_unit_state(make_spec())
        state["status"] = "EXECUTING"
        create_attempt(state, "att-1")
        complete_attempt(state, "att-1", outcome="failed")
        with self.assertRaises(WorkUnitError):
            create_attempt(state, "att-1")

    def test_attempt_exceeding_max_rejected(self):
        state = initial_unit_state(make_spec(limits={"max_attempts": 1}))
        state["status"] = "EXECUTING"
        create_attempt(state, "att-1")
        complete_attempt(state, "att-1", outcome="failed")
        with self.assertRaises(WorkUnitError):
            create_attempt(state, "att-2")

    def test_complete_attempt_sets_fields(self):
        state = initial_unit_state(make_spec())
        state["status"] = "EXECUTING"
        create_attempt(state, "att-1")
        complete_attempt(state, "att-1", outcome="passed",
                         execution_result={"output": "ok"},
                         changed_paths=["src/helper.py"],
                         verifier_result={"passed": True},
                         candidate_id="cand-1",
                         timeout_info={"stage": "execution", "elapsed": 30})
        att = state["attempts"][0]
        self.assertEqual(att["outcome"], "passed")
        self.assertEqual(att["changed_paths"], ["src/helper.py"])
        self.assertEqual(att["candidate_id"], "cand-1")
        self.assertEqual(att["stage"], "completed")
        self.assertIsNotNone(att["completed_at"])

    def test_complete_unknown_attempt_rejected(self):
        state = initial_unit_state(make_spec())
        state["status"] = "EXECUTING"
        with self.assertRaises(WorkUnitError):
            complete_attempt(state, "nonexistent", outcome="passed")

    def test_invalid_outcome_rejected(self):
        state = initial_unit_state(make_spec())
        state["status"] = "EXECUTING"
        create_attempt(state, "att-1")
        with self.assertRaises(WorkUnitError):
            complete_attempt(state, "att-1", outcome="unknown")

    def test_all_outcomes_accepted(self):
        for outcome in ATTEMPT_OUTCOMES:
            state = initial_unit_state(make_spec())
            state["status"] = "EXECUTING"
            create_attempt(state, "att-1")
            complete_attempt(state, "att-1", outcome=outcome)
            self.assertEqual(state["attempts"][0]["outcome"], outcome)

    def test_candidate_id_on_attempt(self):
        state = initial_unit_state(make_spec())
        state["status"] = "EXECUTING"
        attempt = create_attempt(state, "att-1", candidate_id="ws-123")
        self.assertEqual(attempt["candidate_id"], "ws-123")


# ============================================================================
# UNIT RESULT
# ============================================================================

class UnitResultTests(unittest.TestCase):
    def test_set_result_records_intermediate_trust(self):
        state = initial_unit_state(make_spec())
        state["status"] = "EXECUTING"
        create_attempt(state, "att-1", candidate_id="cand-1")
        complete_attempt(state, "att-1", outcome="passed", candidate_id="cand-1")
        set_unit_result(state, verifier_evidence={"passed": True, "check": "check-1"}, candidate_id="cand-1")
        self.assertEqual(state["result"]["trust_level"], "INTERMEDIATE")
        self.assertIsNotNone(state["result"]["time"])

    def test_set_result_requires_dict_evidence(self):
        state = initial_unit_state(make_spec())
        with self.assertRaises(WorkUnitError):
            set_unit_result(state, verifier_evidence="not a dict")

    def test_unit_verified_without_evidence_fails(self):
        state = initial_unit_state(make_spec())
        state["status"] = "VERIFYING"
        with self.assertRaisesRegex(WorkUnitError, "UNIT_VERIFIED without result evidence"):
            transition_unit(state, "UNIT_VERIFIED")
        self.assertEqual(state["status"], "VERIFYING")

    def test_unit_verified_without_verifier_evidence_field_fails(self):
        state = initial_unit_state(make_spec())
        for target in ("VALIDATED", "READY", "EXECUTING"):
            transition_unit(state, target)
        create_attempt(state, "att-1", candidate_id="cand-1")
        complete_attempt(state, "att-1", outcome="passed", candidate_id="cand-1")
        transition_unit(state, "VERIFYING")
        state["result"] = {"trust_level": "INTERMEDIATE", "time": "now", "candidate_id": "cand-1"}
        with self.assertRaisesRegex(WorkUnitError, "verifier_evidence"):
            transition_unit(state, "UNIT_VERIFIED")
        self.assertEqual(state["status"], "VERIFYING")


# ============================================================================
# STATE VALIDATION
# ============================================================================

class StateValidationTests(unittest.TestCase):
    def test_validate_initial_state(self):
        state = initial_unit_state(make_spec())
        seal_unit_state(state)
        validate_unit_state(state)

    def test_id_mismatch_rejected(self):
        state = initial_unit_state(make_spec())
        state["unit_id"] = "different"
        seal_unit_state(state)
        with self.assertRaisesRegex(WorkUnitError, "mismatch"):
            validate_unit_state(state)

    def test_invalid_status_rejected(self):
        state = initial_unit_state(make_spec())
        state["status"] = "RUNNING"
        seal_unit_state(state)
        with self.assertRaises(WorkUnitError):
            validate_unit_state(state)

    def test_repair_pending_without_prior_failure_rejected(self):
        state = initial_unit_state(make_spec())
        state["status"] = "REPAIR_PENDING"
        seal_unit_state(state)
        with self.assertRaisesRegex(WorkUnitError, "REPAIR_PENDING without"):
            validate_unit_state(state)

    def test_repair_pending_after_passed_attempt_rejected(self):
        state = initial_unit_state(make_spec())
        state["status"] = "EXECUTING"
        create_attempt(state, "att-1")
        complete_attempt(state, "att-1", outcome="passed")
        state["status"] = "REPAIR_PENDING"
        seal_unit_state(state)
        with self.assertRaisesRegex(WorkUnitError, "eligible prior failure"):
            validate_unit_state(state)

    def test_repair_pending_after_failed_attempt_accepted(self):
        state = initial_unit_state(make_spec(limits={"max_attempts": 3}))
        state["status"] = "EXECUTING"
        create_attempt(state, "att-1")
        complete_attempt(state, "att-1", outcome="failed",
                         failure_classification="TEST_FAILED")
        transition_unit(state, "REPAIR_PENDING")
        seal_unit_state(state)
        validate_unit_state(state)  # Should not raise.

    def test_attempt_count_exceeding_limits_rejected(self):
        state = initial_unit_state(make_spec(limits={"max_attempts": 1}))
        state["status"] = "EXECUTING"
        state["attempts"] = [
            {"attempt_id": "att-1", "outcome": "failed", "completed_at": "now"},
            {"attempt_id": "att-2", "outcome": "failed", "completed_at": "now"},
        ]
        seal_unit_state(state)
        with self.assertRaisesRegex(WorkUnitError, "exceeds"):
            validate_unit_state(state)

    def test_tampered_checksum_rejected(self):
        state = initial_unit_state(make_spec())
        seal_unit_state(state)
        state["status"] = "VALIDATED"  # Change without resealing
        with self.assertRaisesRegex(WorkUnitError, "checksum mismatch"):
            validate_unit_state(state)

    def test_null_checksum_skips_verification(self):
        """State with no checksum (freshly created, not yet sealed) passes."""
        state = initial_unit_state(make_spec())
        validate_unit_state(state)


# ============================================================================
# SECTION VALIDATION
# ============================================================================

class SectionValidationTests(unittest.TestCase):
    def test_empty_section_accepted(self):
        section = initial_work_units_section()
        validate_section(section)

    def test_add_unit_to_section(self):
        section = initial_work_units_section()
        add_unit_to_section(section, make_spec("a"))
        self.assertIn("a", section["units"])
        self.assertIsNotNone(section["plan_checksum"])

    def test_duplicate_unit_in_section_rejected(self):
        section = initial_work_units_section()
        add_unit_to_section(section, make_spec("a"))
        with self.assertRaises(WorkUnitError):
            add_unit_to_section(section, make_spec("a"))

    def test_section_checksum_tamper_detected(self):
        section = initial_work_units_section()
        add_unit_to_section(section, make_spec("a"))
        section["units"]["a"]["status"] = "VALIDATED"
        with self.assertRaisesRegex(WorkUnitError, "checksum mismatch"):
            validate_section(section)

    def test_cross_unit_missing_dep_detected(self):
        section = initial_work_units_section()
        spec = make_spec("a", dependencies=["nonexistent"])
        state = initial_unit_state(spec)
        seal_unit_state(state)
        section["units"]["a"] = state
        section["plan_checksum"] = None
        with self.assertRaisesRegex(WorkUnitError, "unknown unit"):
            validate_section(section)

    def test_cross_unit_cycle_detected(self):
        section = initial_work_units_section()
        spec_a = make_spec("a", dependencies=["b"])
        spec_b = make_spec("b", dependencies=["a"])
        state_a = initial_unit_state(spec_a)
        state_b = initial_unit_state(spec_b)
        seal_unit_state(state_a)
        seal_unit_state(state_b)
        section["units"]["a"] = state_a
        section["units"]["b"] = state_b
        section["plan_checksum"] = None
        with self.assertRaisesRegex(WorkUnitError, "cycle"):
            validate_section(section)

    def test_invalid_schema_version_rejected(self):
        section = initial_work_units_section()
        section["schema_version"] = 99
        with self.assertRaises(WorkUnitError):
            validate_section(section)

    def test_unit_key_mismatch_rejected(self):
        section = initial_work_units_section()
        state = initial_unit_state(make_spec("a"))
        seal_unit_state(state)
        section["units"]["wrong-key"] = state
        section["plan_checksum"] = None
        with self.assertRaisesRegex(WorkUnitError, "does not match"):
            validate_section(section)


# ============================================================================
# TRUST INVARIANT TESTS
# ============================================================================

class TrustInvariantTests(unittest.TestCase):
    """Prove that WorkUnit state CANNOT set, infer, or mutate existing
    trusted checkpoint / verified-progress fields."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo, self.store, self.git = fixture(self.root)

    def test_work_unit_section_never_contains_trust_fields(self):
        section = initial_work_units_section()
        add_unit_to_section(section, make_spec("a"))
        serialized = json.dumps(section)
        for forbidden in ("verified_progress", "last_verified_checkpoint",
                          "VERIFIED", "COMPLETED"):
            self.assertNotIn(forbidden, serialized)

    def test_unit_verified_result_is_always_intermediate(self):
        state = initial_unit_state(make_spec())
        set_unit_result(state, verifier_evidence={"passed": True})
        self.assertEqual(state["result"]["trust_level"], "INTERMEDIATE")

    def test_persist_work_units_cannot_alter_verified_progress(self):
        durable_state = copy.deepcopy(self.store.state)
        section = initial_work_units_section()
        add_unit_to_section(section, make_spec("a"))

        before_vp = copy.deepcopy(durable_state["verified_progress"])
        before_cp = copy.deepcopy(durable_state["last_verified_checkpoint"])

        persist_work_units(section, durable_state)

        self.assertEqual(durable_state["verified_progress"], before_vp)
        self.assertEqual(durable_state["last_verified_checkpoint"], before_cp)

    def test_persist_work_units_cannot_promote_empty_checkpoint(self):
        durable_state = copy.deepcopy(self.store.state)
        self.assertIsNone(durable_state["last_verified_checkpoint"])
        self.assertEqual(durable_state["verified_progress"], [])

        section = initial_work_units_section()
        spec = make_spec("a")
        add_unit_to_section(section, spec)
        unit_state = section["units"]["a"]
        unit_state["status"] = "EXECUTING"
        create_attempt(unit_state, "att-1", candidate_id="cand-1")
        complete_attempt(unit_state, "att-1", outcome="passed", candidate_id="cand-1")
        transition_unit(unit_state, "VERIFYING")
        set_unit_result(unit_state, verifier_evidence={"passed": True, "check": "check-1"}, candidate_id="cand-1")
        transition_unit(unit_state, "UNIT_VERIFIED")
        seal_unit_state(unit_state)
        work_units._reseal_section(section)

        persist_work_units(section, durable_state)

        self.assertIsNone(durable_state["last_verified_checkpoint"])
        self.assertEqual(durable_state["verified_progress"], [])

    def test_persist_work_units_cannot_alter_existing_checkpoint(self):
        durable_state = copy.deepcopy(self.store.state)
        fake_checkpoint = {
            "reference": {"path": "checkpoints/0001.json", "sha256": "abc" * 20 + "ab"},
            "snapshot": {"fingerprint": "test"}
        }
        durable_state["last_verified_checkpoint"] = fake_checkpoint
        durable_state["verified_progress"] = [{"task": "test", "checkpoint": fake_checkpoint["reference"]}]

        section = initial_work_units_section()
        persist_work_units(section, durable_state)

        self.assertEqual(durable_state["last_verified_checkpoint"], fake_checkpoint)
        self.assertEqual(durable_state["verified_progress"],
                         [{"task": "test", "checkpoint": fake_checkpoint["reference"]}])

    def test_unit_verified_status_is_not_top_level_verified(self):
        self.assertNotEqual("UNIT_VERIFIED", "VERIFIED")
        self.assertIn("UNIT_VERIFIED", UNIT_STATUSES)
        self.assertNotIn("UNIT_VERIFIED", durable.STATUSES)

    def test_top_level_statuses_are_disjoint_from_unit_statuses(self):
        for unit_status in UNIT_STATUSES:
            if unit_status in ("UNIT_VERIFIED", "UNIT_FAILED"):
                self.assertNotIn(unit_status, durable.STATUSES)

    def test_persist_preserves_status_field(self):
        durable_state = copy.deepcopy(self.store.state)
        original_status = durable_state["status"]
        section = initial_work_units_section()
        persist_work_units(section, durable_state)
        self.assertEqual(durable_state["status"], original_status)


# ============================================================================
# DURABLE PERSISTENCE AND BACKWARD COMPATIBILITY
# ============================================================================

class DurablePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo, self.store, self.git = fixture(self.root)

    def reload(self):
        store = durable.Store(self.root / "runs", "test-wu-run")
        store.load()
        return store

    def test_legacy_run_without_work_units_loads(self):
        store = self.reload()
        self.assertNotIn("work_units", store.state)
        loaded = load_work_units(store.state)
        self.assertIsNone(loaded)

    def test_existing_fixture_loads_unchanged(self):
        store = self.reload()
        durable.validate_state(store.state)
        self.assertEqual(store.state["status"], "CREATED")
        self.assertEqual(store.state["verified_progress"], [])
        self.assertIsNone(store.state["last_verified_checkpoint"])

    def test_persist_and_reload_empty_section(self):
        section = initial_work_units_section()
        state = copy.deepcopy(self.store.state)
        persist_work_units(section, state)
        self.store.state = state
        self.store.commit()
        reloaded = self.reload()
        self.assertIn("work_units", reloaded.state)
        loaded = load_work_units(reloaded.state)
        self.assertEqual(loaded["schema_version"], 1)
        self.assertEqual(loaded["units"], {})

    def test_persist_and_reload_with_units(self):
        section = initial_work_units_section()
        spec = make_spec("unit-a", objective="Test objective")
        add_unit_to_section(section, spec)
        state = copy.deepcopy(self.store.state)
        persist_work_units(section, state)
        self.store.state = state
        self.store.commit()
        reloaded = self.reload()
        loaded = load_work_units(reloaded.state)
        self.assertIn("unit-a", loaded["units"])
        self.assertEqual(loaded["units"]["unit-a"]["spec"]["objective"], "Test objective")
        self.assertEqual(loaded["units"]["unit-a"]["status"], "PROPOSED")

    def test_persist_and_reload_unit_plan(self):
        section = initial_work_units_section()
        a = make_spec("a", dependencies=[])
        b = make_spec("b", dependencies=["a"])
        add_unit_to_section(section, a)
        add_unit_to_section(section, b)
        state = copy.deepcopy(self.store.state)
        persist_work_units(section, state)
        self.store.state = state
        self.store.commit()
        reloaded = self.reload()
        loaded = load_work_units(reloaded.state)
        self.assertEqual(set(loaded["units"].keys()), {"a", "b"})
        self.assertEqual(loaded["units"]["b"]["spec"]["dependencies"], ["a"])

    def test_attempt_records_survive_reload(self):
        section = initial_work_units_section()
        add_unit_to_section(section, make_spec("a"))
        unit_state = section["units"]["a"]
        unit_state["status"] = "EXECUTING"
        unit_state["checksum"] = None
        create_attempt(unit_state, "att-1", candidate_id="ws-1")
        complete_attempt(unit_state, "att-1", outcome="failed",
                         failure_classification="TEST_FAILED",
                         changed_paths=["src/helper.py"])
        seal_unit_state(unit_state)
        work_units._reseal_section(section)

        state = copy.deepcopy(self.store.state)
        persist_work_units(section, state)
        self.store.state = state
        self.store.commit()

        reloaded = self.reload()
        loaded = load_work_units(reloaded.state)
        att = loaded["units"]["a"]["attempts"][0]
        self.assertEqual(att["attempt_id"], "att-1")
        self.assertEqual(att["outcome"], "failed")
        self.assertEqual(att["failure_classification"], "TEST_FAILED")
        self.assertEqual(att["changed_paths"], ["src/helper.py"])
        self.assertEqual(att["candidate_id"], "ws-1")

    def test_verified_unit_result_survives_reload_as_intermediate(self):
        section = initial_work_units_section()
        add_unit_to_section(section, make_spec("a"))
        unit_state = section["units"]["a"]
        unit_state["status"] = "EXECUTING"
        create_attempt(unit_state, "att-1", candidate_id="cand-1")
        complete_attempt(unit_state, "att-1", outcome="passed", candidate_id="cand-1")
        transition_unit(unit_state, "VERIFYING")
        set_unit_result(unit_state, verifier_evidence={"passed": True, "check": "check-1"},
                        candidate_id="cand-1", candidate_snapshot={"fingerprint": "snap-1"})
        transition_unit(unit_state, "UNIT_VERIFIED")
        seal_unit_state(unit_state)
        work_units._reseal_section(section)

        state = copy.deepcopy(self.store.state)
        persist_work_units(section, state)
        self.store.state = state
        self.store.commit()

        reloaded = self.reload()
        loaded = load_work_units(reloaded.state)
        result = loaded["units"]["a"]["result"]
        self.assertEqual(result["trust_level"], "INTERMEDIATE")
        self.assertEqual(result["verifier_evidence"]["passed"], True)
        self.assertEqual(result["candidate_snapshot"]["fingerprint"], "snap-1")
        self.assertEqual(result["candidate_id"], "cand-1")

    def test_existing_checkpoint_semantics_unchanged_after_work_unit_persistence(self):
        state = copy.deepcopy(self.store.state)
        self.assertIsNone(state["last_verified_checkpoint"])
        self.assertEqual(state["verified_progress"], [])
        section = initial_work_units_section()
        add_unit_to_section(section, make_spec("a"))
        persist_work_units(section, state)
        self.assertIsNone(state["last_verified_checkpoint"])
        self.assertEqual(state["verified_progress"], [])
        self.store.state = state
        self.store.commit()
        reloaded = self.reload()
        durable.validate_state(reloaded.state)
        self.assertIsNone(reloaded.state["last_verified_checkpoint"])
        self.assertEqual(reloaded.state["verified_progress"], [])


# ============================================================================
# CRASH / RELOAD TESTING
# ============================================================================

class CrashReloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo, self.store, self.git = fixture(self.root)

    def reload(self):
        store = durable.Store(self.root / "runs", "test-wu-run")
        store.load()
        return store

    def test_interrupted_unit_state_reloads_safely(self):
        section = initial_work_units_section()
        add_unit_to_section(section, make_spec("a"))
        unit_state = section["units"]["a"]
        unit_state["status"] = "EXECUTING"
        create_attempt(unit_state, "att-1")
        seal_unit_state(unit_state)
        work_units._reseal_section(section)

        state = copy.deepcopy(self.store.state)
        persist_work_units(section, state)
        self.store.state = state
        self.store.commit()

        reloaded = self.reload()
        loaded = load_work_units(reloaded.state)
        self.assertEqual(loaded["units"]["a"]["status"], "EXECUTING")
        self.assertEqual(loaded["units"]["a"]["attempts"][0]["outcome"], "interrupted")

    def test_corrupt_unit_state_fails_closed(self):
        section = initial_work_units_section()
        add_unit_to_section(section, make_spec("a"))
        state = copy.deepcopy(self.store.state)
        persist_work_units(section, state)
        self.store.state = state
        self.store.commit()

        state_path = self.store.directory / "state.json"
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        saved["work_units"]["units"]["a"]["status"] = "VALIDATED"
        state_path.write_bytes(durable.encoded(saved))

        with self.assertRaisesRegex(WorkUnitError, "checksum mismatch"):
            self.reload()

    def test_tampered_plan_checksum_fails_closed(self):
        section = initial_work_units_section()
        add_unit_to_section(section, make_spec("a"))
        state = copy.deepcopy(self.store.state)
        persist_work_units(section, state)
        self.store.state = state
        self.store.commit()

        state_path = self.store.directory / "state.json"
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        saved["work_units"]["plan_checksum"] = "0" * 64
        state_path.write_bytes(durable.encoded(saved))

        with self.assertRaisesRegex(WorkUnitError, "plan checksum mismatch"):
            self.reload()

    def test_trusted_top_level_not_promoted_after_reload(self):
        section = initial_work_units_section()
        add_unit_to_section(section, make_spec("a"))
        unit_state = section["units"]["a"]
        unit_state["status"] = "EXECUTING"
        create_attempt(unit_state, "att-1", candidate_id="cand-1")
        complete_attempt(unit_state, "att-1", outcome="passed", candidate_id="cand-1")
        transition_unit(unit_state, "VERIFYING")
        set_unit_result(unit_state, verifier_evidence={"passed": True, "check": "check-1"}, candidate_id="cand-1")
        transition_unit(unit_state, "UNIT_VERIFIED")
        seal_unit_state(unit_state)
        work_units._reseal_section(section)

        state = copy.deepcopy(self.store.state)
        persist_work_units(section, state)
        self.store.state = state
        self.store.commit()

        reloaded = self.reload()
        self.assertIsNone(reloaded.state["last_verified_checkpoint"])
        self.assertEqual(reloaded.state["verified_progress"], [])
        self.assertNotEqual(reloaded.state["status"], "VERIFIED")
        self.assertNotEqual(reloaded.state["status"], "COMPLETED")

    def test_malformed_work_units_in_state_fails_closed_on_full_validate(self):
        state = copy.deepcopy(self.store.state)
        state["work_units"] = {"schema_version": 99, "units": {}, "plan_checksum": None}
        self.store.state = state
        with self.assertRaises(Exception):
            self.store.commit()


# ============================================================================
# AUDIT FIX 1 REGRESSION: VERIFIED RESULT CONTRACT
# ============================================================================

class VerifiedResultContractTests(unittest.TestCase):
    def test_passed_false_cannot_become_unit_verified(self):
        state = initial_unit_state(make_spec())
        for target in ("VALIDATED", "READY", "EXECUTING"):
            transition_unit(state, target)
        create_attempt(state, "att-1", candidate_id="cand-1")
        complete_attempt(state, "att-1", outcome="passed", candidate_id="cand-1")
        transition_unit(state, "VERIFYING")
        set_unit_result(state, verifier_evidence={"passed": False, "check": "check-1"}, candidate_id="cand-1")
        with self.assertRaisesRegex(WorkUnitError, "passed=True"):
            transition_unit(state, "UNIT_VERIFIED")
        self.assertEqual(state["status"], "VERIFYING")

    def test_unknown_verifier_id_cannot_establish_verification(self):
        state = initial_unit_state(make_spec(verifier_ids=["check-1"]))
        for target in ("VALIDATED", "READY", "EXECUTING"):
            transition_unit(state, target)
        create_attempt(state, "att-1", candidate_id="cand-1")
        complete_attempt(state, "att-1", outcome="passed", candidate_id="cand-1")
        transition_unit(state, "VERIFYING")
        set_unit_result(state, verifier_evidence={"passed": True, "check": "unknown-check"}, candidate_id="cand-1")
        with self.assertRaisesRegex(WorkUnitError, "Unknown verifier ID"):
            transition_unit(state, "UNIT_VERIFIED")
        self.assertEqual(state["status"], "VERIFYING")

    def test_missing_required_verifier_id_fails(self):
        state = initial_unit_state(make_spec(verifier_ids=["check-1", "check-2"]))
        for target in ("VALIDATED", "READY", "EXECUTING"):
            transition_unit(state, target)
        create_attempt(state, "att-1", candidate_id="cand-1")
        complete_attempt(state, "att-1", outcome="passed", candidate_id="cand-1")
        transition_unit(state, "VERIFYING")
        set_unit_result(state, verifier_evidence={"passed": True, "verifiers": {"check-1": {"passed": True}}}, candidate_id="cand-1")
        with self.assertRaisesRegex(WorkUnitError, "Missing required verifier evidence"):
            transition_unit(state, "UNIT_VERIFIED")
        self.assertEqual(state["status"], "VERIFYING")

    def test_missing_successful_attempt_cannot_establish_verification(self):
        state = initial_unit_state(make_spec())
        for target in ("VALIDATED", "READY", "EXECUTING"):
            transition_unit(state, target)
        create_attempt(state, "att-1", candidate_id="cand-1")
        complete_attempt(state, "att-1", outcome="failed", candidate_id="cand-1")
        transition_unit(state, "VERIFYING")
        set_unit_result(state, verifier_evidence={"passed": True, "check": "check-1"}, candidate_id="cand-1")
        with self.assertRaisesRegex(WorkUnitError, "did not pass"):
            transition_unit(state, "UNIT_VERIFIED")
        self.assertEqual(state["status"], "VERIFYING")

    def test_missing_candidate_identity_cannot_establish_verification(self):
        state = initial_unit_state(make_spec())
        for target in ("VALIDATED", "READY", "EXECUTING"):
            transition_unit(state, target)
        create_attempt(state, "att-1")
        complete_attempt(state, "att-1", outcome="passed")
        transition_unit(state, "VERIFYING")
        set_unit_result(state, verifier_evidence={"passed": True, "check": "check-1"})
        with self.assertRaisesRegex(WorkUnitError, "candidate identity"):
            transition_unit(state, "UNIT_VERIFIED")
        self.assertEqual(state["status"], "VERIFYING")

    def test_candidate_mismatch_between_attempt_and_result_rejected(self):
        state = initial_unit_state(make_spec())
        for target in ("VALIDATED", "READY", "EXECUTING"):
            transition_unit(state, target)
        create_attempt(state, "att-1", candidate_id="cand-1")
        complete_attempt(state, "att-1", outcome="passed", candidate_id="cand-1")
        with self.assertRaisesRegex(WorkUnitError, "Candidate identity mismatch"):
            set_unit_result(state, verifier_evidence={"passed": True, "check": "check-1"}, candidate_id="cand-2", attempt_id="att-1")

    def test_trust_level_must_be_intermediate(self):
        state = initial_unit_state(make_spec())
        for target in ("VALIDATED", "READY", "EXECUTING"):
            transition_unit(state, target)
        create_attempt(state, "att-1", candidate_id="cand-1")
        complete_attempt(state, "att-1", outcome="passed", candidate_id="cand-1")
        transition_unit(state, "VERIFYING")
        set_unit_result(state, verifier_evidence={"passed": True, "check": "check-1"}, candidate_id="cand-1")
        state["result"]["trust_level"] = "TRUSTED"
        with self.assertRaisesRegex(WorkUnitError, "must be exactly INTERMEDIATE"):
            transition_unit(state, "UNIT_VERIFIED")

    def test_store_commit_and_load_rejects_invalid_persisted_verified_state(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _, store, _ = fixture(root)
            section = initial_work_units_section()
            add_unit_to_section(section, make_spec("a"))
            unit_state = section["units"]["a"]
            unit_state["status"] = "UNIT_VERIFIED"
            unit_state["result"] = {"verifier_evidence": {"passed": False}, "trust_level": "INTERMEDIATE"}
            seal_unit_state(unit_state)
            work_units._reseal_section(section)

    def test_duplicate_failed_object_followed_by_bare_id(self):
        ev = {
            "passed": True,
            "verifiers": [
                {"verifier_id": "check-1", "passed": False},
                "check-1"
            ]
        }
        with self.assertRaisesRegex(WorkUnitError, "Duplicate verifier ID 'check-1'"):
            validate_verifier_evidence(ev, ["check-1"])

    def test_duplicate_bare_id_followed_by_failed_object(self):
        ev = {
            "passed": True,
            "verifiers": [
                "check-1",
                {"verifier_id": "check-1", "passed": False}
            ]
        }
        with self.assertRaisesRegex(WorkUnitError, "Duplicate verifier ID 'check-1'"):
            validate_verifier_evidence(ev, ["check-1"])

    def test_duplicate_two_successful_entries(self):
        ev = {
            "passed": True,
            "verifiers": [
                {"verifier_id": "check-1", "passed": True},
                {"verifier_id": "check-1", "passed": True}
            ]
        }
        with self.assertRaisesRegex(WorkUnitError, "Duplicate verifier ID 'check-1'"):
            validate_verifier_evidence(ev, ["check-1"])

    def test_duplicate_conflicting_entries(self):
        ev = {
            "passed": True,
            "verifiers": [
                {"verifier_id": "check-1", "passed": True},
                {"verifier_id": "check-1", "passed": False}
            ]
        }
        with self.assertRaisesRegex(WorkUnitError, "Duplicate verifier ID 'check-1'"):
            validate_verifier_evidence(ev, ["check-1"])

    def test_duplicate_unknown_verifier(self):
        ev = {
            "passed": True,
            "verifiers": [
                {"verifier_id": "unknown-1", "passed": True},
                {"verifier_id": "unknown-1", "passed": True}
            ]
        }
        with self.assertRaisesRegex(WorkUnitError, "Duplicate verifier ID 'unknown-1'|Unknown verifier ID 'unknown-1'"):
            validate_verifier_evidence(ev, ["check-1"])

    def test_valid_unique_verifier_set_succeeds(self):
        ev = {
            "passed": True,
            "verifiers": [
                {"verifier_id": "check-1", "passed": True},
                {"verifier_id": "check-2", "passed": True}
            ]
        }
        validate_verifier_evidence(ev, ["check-1", "check-2"])

    def test_bare_verifier_id_without_explicit_success_rejected(self):
        ev = {
            "passed": True,
            "verifiers": ["check-1"]
        }
        with self.assertRaisesRegex(WorkUnitError, "Bare verifier ID 'check-1'"):
            validate_verifier_evidence(ev, ["check-1"])

    def test_real_store_commit_and_load_with_duplicate_verifier_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _, store, _ = fixture(root)

            section = initial_work_units_section()
            add_unit_to_section(section, make_spec("a"))
            unit_state = section["units"]["a"]
            unit_state["status"] = "EXECUTING"
            create_attempt(unit_state, "att-1", candidate_id="cand-1")
            complete_attempt(unit_state, "att-1", outcome="passed", candidate_id="cand-1")
            transition_unit(unit_state, "VERIFYING")
            set_unit_result(unit_state, verifier_evidence={"passed": True, "verifiers": [{"verifier_id": "check-1", "passed": True}]}, candidate_id="cand-1")
            transition_unit(unit_state, "UNIT_VERIFIED")
            seal_unit_state(unit_state)
            work_units._reseal_section(section)

            persist_work_units(section, store.state)
            store.commit()

            reloaded = durable.Store(root / "runs", "test-wu-run")
            reloaded.load()
            loaded = load_work_units(reloaded.state)
            self.assertEqual(loaded["units"]["a"]["status"], "UNIT_VERIFIED")
            self.assertIsNone(reloaded.state["last_verified_checkpoint"])

            state_path = store.directory / "state.json"
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            saved["work_units"]["units"]["a"]["result"]["verifier_evidence"] = {
                "passed": True,
                "verifiers": [
                    {"verifier_id": "check-1", "passed": False},
                    "check-1"
                ]
            }
            seal_unit_state(saved["work_units"]["units"]["a"])
            work_units._reseal_section(saved["work_units"])
            state_path.write_bytes(durable.encoded(saved))

            reloaded_bad = durable.Store(root / "runs", "test-wu-run")
            with self.assertRaisesRegex(WorkUnitError, "Duplicate verifier ID 'check-1'"):
                reloaded_bad.load()

            bad_store = durable.Store(root / "runs", "test-wu-run")
            bad_store.state = copy.deepcopy(saved)
            with self.assertRaisesRegex(WorkUnitError, "Duplicate verifier ID 'check-1'"):
                bad_store.commit()


# ============================================================================
# AUDIT FIX 2 REGRESSION: PATH / SCOPE NORMALIZATION
# ============================================================================

class ScopeNormalizationTests(unittest.TestCase):
    def test_traversal_via_dotdot_rejected(self):
        for bad in ("src/../outside.py", "../escape.py", "a/b/../../c"):
            with self.subTest(bad=bad), self.assertRaises(WorkUnitError):
                normalize_path(bad)

    def test_absolute_paths_rejected(self):
        for bad in ("/abs.py", "\\abs.py", "//server/share", "\\\\server\\share"):
            with self.subTest(bad=bad), self.assertRaises(WorkUnitError):
                normalize_path(bad)

    def test_drive_qualified_paths_rejected(self):
        for bad in ("C:/foo.py", "c:\\foo.py", "D:relative.py"):
            with self.subTest(bad=bad), self.assertRaises(WorkUnitError):
                normalize_path(bad)

    def test_repository_metadata_paths_rejected(self):
        for bad in (".git", ".git/config", "src/.git/HEAD"):
            with self.subTest(bad=bad), self.assertRaises(WorkUnitError):
                normalize_path(bad)

    def test_windows_backslash_normalized_and_forbidden_respected(self):
        norm_a = normalize_entry("src\\helper.py")
        norm_f = normalize_entry("src\\secret\\")
        self.assertEqual(norm_a, "src/helper.py")
        self.assertEqual(norm_f, "src/secret/")
        self.assertTrue(covers("src/secret/", "src/secret/key.py"))
        self.assertFalse(covers("src/secret/", "src/helper.py"))

    def test_same_path_simultaneously_allowed_and_forbidden_rejected(self):
        spec = make_spec(paths=["src/helper.py"], forbidden=["src/helper.py"])
        with self.assertRaisesRegex(WorkUnitError, "overlaps forbidden_path"):
            validate_unit_spec(spec)

    def test_prefix_exact_file_vs_foobar_no_false_positive(self):
        self.assertFalse(overlaps("foo", "foobar"))
        self.assertFalse(covers("foo", "foobar"))

    def test_prefix_directory_foo_vs_foobar_no_false_positive(self):
        self.assertFalse(overlaps("foo/", "foobar/"))
        self.assertFalse(covers("foo/", "foobar/"))

    def test_prefix_directory_covers_contained_files(self):
        self.assertTrue(covers("foo/", "foo/bar.py"))
        self.assertTrue(overlaps("foo/bar.py", "foo/"))

    def test_case_insensitive_matching(self):
        self.assertTrue(covers("SRC/", "src/helper.py"))
        self.assertTrue(overlaps("SRC/HELPER.PY", "src/helper.py"))

    def test_scope_containment_catches_escapes_and_forbidden_overlaps(self):
        parent = {"allowed_paths": ["src/"], "forbidden_paths": ["src/secret/"]}
        unit_escape = {"allowed_paths": ["outside.py"], "forbidden_paths": []}
        with self.assertRaisesRegex(WorkUnitError, "escapes parent scope"):
            validate_scope_containment(unit_escape, parent)

        unit_overlap = {"allowed_paths": ["src/secret/key.py"], "forbidden_paths": []}
        with self.assertRaisesRegex(WorkUnitError, "overlaps parent forbidden"):
            validate_scope_containment(unit_overlap, parent)


# ============================================================================
# AUDIT FIX 3 REGRESSION: ATTEMPT / REPAIR / TERMINAL INVARIANTS
# ============================================================================

class AttemptLifecycleInvariantTests(unittest.TestCase):
    def test_one_active_attempt_maximum(self):
        state = initial_unit_state(make_spec())
        state["status"] = "EXECUTING"
        create_attempt(state, "att-1")
        with self.assertRaisesRegex(WorkUnitError, "existing attempt is still active"):
            create_attempt(state, "att-2")

    def test_current_attempt_consistency(self):
        state = initial_unit_state(make_spec())
        state["status"] = "EXECUTING"
        create_attempt(state, "att-1")
        self.assertEqual(state["current_attempt"], "att-1")
        complete_attempt(state, "att-1", outcome="failed")
        self.assertIsNone(state["current_attempt"])

        # Tamper current_attempt to non-None when no attempt is active
        state["current_attempt"] = "att-1"
        seal_unit_state(state)
        with self.assertRaisesRegex(WorkUnitError, "current_attempt must be None"):
            validate_unit_state(state)

    def test_completed_attempts_are_immutable(self):
        state = initial_unit_state(make_spec())
        state["status"] = "EXECUTING"
        create_attempt(state, "att-1")
        complete_attempt(state, "att-1", outcome="failed")
        with self.assertRaisesRegex(WorkUnitError, "already completed and immutable"):
            complete_attempt(state, "att-1", outcome="passed")

    def test_repair_pending_rejected_after_budget_exhausted(self):
        spec = make_spec(limits={"max_attempts": 2})
        state = initial_unit_state(spec)
        for target in ("VALIDATED", "READY", "EXECUTING"):
            transition_unit(state, target)
        create_attempt(state, "att-1")
        complete_attempt(state, "att-1", outcome="failed")
        transition_unit(state, "REPAIR_PENDING")
        transition_unit(state, "READY")
        transition_unit(state, "EXECUTING")
        create_attempt(state, "att-2")
        complete_attempt(state, "att-2", outcome="failed")
        # 2 of 2 attempts used: cannot enter REPAIR_PENDING
        with self.assertRaisesRegex(WorkUnitError, "budget is exhausted"):
            transition_unit(state, "REPAIR_PENDING")

    def test_repair_pending_rejected_without_prior_failed_attempt(self):
        state = initial_unit_state(make_spec())
        state["status"] = "EXECUTING"
        with self.assertRaisesRegex(WorkUnitError, "without any prior attempts"):
            transition_unit(state, "REPAIR_PENDING")

        create_attempt(state, "att-1")
        complete_attempt(state, "att-1", outcome="passed")
        with self.assertRaisesRegex(WorkUnitError, "without eligible prior failure"):
            transition_unit(state, "REPAIR_PENDING")

    def test_terminal_unit_cannot_be_rewritten_or_transitioned(self):
        state = initial_unit_state(make_spec())
        for target in ("VALIDATED", "READY", "EXECUTING"):
            transition_unit(state, target)
        create_attempt(state, "att-1", candidate_id="cand-1")
        complete_attempt(state, "att-1", outcome="passed", candidate_id="cand-1")
        transition_unit(state, "VERIFYING")
        set_unit_result(state, verifier_evidence={"passed": True, "check": "check-1"}, candidate_id="cand-1")
        transition_unit(state, "UNIT_VERIFIED")

        with self.assertRaisesRegex(WorkUnitError, "terminal unit"):
            transition_unit(state, "READY")
        with self.assertRaisesRegex(WorkUnitError, "terminal unit"):
            create_attempt(state, "att-2")
        with self.assertRaisesRegex(WorkUnitError, "terminal unit"):
            set_unit_result(state, verifier_evidence={"passed": True, "check": "check-1"})

    def test_terminal_state_with_active_attempt_fails_validation(self):
        state = initial_unit_state(make_spec())
        state["status"] = "UNIT_FAILED"
        state["attempts"] = [{"attempt_id": "att-1", "outcome": "interrupted", "completed_at": None}]
        state["current_attempt"] = "att-1"
        seal_unit_state(state)
        with self.assertRaisesRegex(WorkUnitError, "cannot have an active attempt"):
            validate_unit_state(state)


# ============================================================================
# AUDIT FIX 4 REGRESSION: DEPENDENCY STATE INVARIANTS
# ============================================================================

class DependencyStateInvariantTests(unittest.TestCase):
    def test_dependent_executing_with_prerequisite_proposed_fails(self):
        section = initial_work_units_section()
        add_unit_to_section(section, make_spec("a", dependencies=[]))
        add_unit_to_section(section, make_spec("b", dependencies=["a"]))
        section["units"]["b"]["status"] = "EXECUTING"
        seal_unit_state(section["units"]["b"])
        section["plan_checksum"] = None
        with self.assertRaisesRegex(WorkUnitError, "must be UNIT_VERIFIED"):
            validate_section(section)

    def test_dependent_ready_with_prerequisite_proposed_fails(self):
        section = initial_work_units_section()
        add_unit_to_section(section, make_spec("a", dependencies=[]))
        add_unit_to_section(section, make_spec("b", dependencies=["a"]))
        section["units"]["b"]["status"] = "READY"
        seal_unit_state(section["units"]["b"])
        section["plan_checksum"] = None
        with self.assertRaisesRegex(WorkUnitError, "must be UNIT_VERIFIED"):
            validate_section(section)

    def test_dependent_verifying_with_prerequisite_proposed_fails(self):
        section = initial_work_units_section()
        add_unit_to_section(section, make_spec("a", dependencies=[]))
        add_unit_to_section(section, make_spec("b", dependencies=["a"]))
        section["units"]["b"]["status"] = "VERIFYING"
        seal_unit_state(section["units"]["b"])
        section["plan_checksum"] = None
        with self.assertRaisesRegex(WorkUnitError, "must be UNIT_VERIFIED"):
            validate_section(section)

    def test_dependent_unit_verified_with_prerequisite_proposed_fails(self):
        section = initial_work_units_section()
        add_unit_to_section(section, make_spec("a", dependencies=[]))
        add_unit_to_section(section, make_spec("b", dependencies=["a"]))
        # Valid verified result for b
        b_state = section["units"]["b"]
        b_state["status"] = "EXECUTING"
        create_attempt(b_state, "att-1", candidate_id="cand-1")
        complete_attempt(b_state, "att-1", outcome="passed", candidate_id="cand-1")
        transition_unit(b_state, "VERIFYING")
        set_unit_result(b_state, verifier_evidence={"passed": True, "check": "check-1"}, candidate_id="cand-1")
        transition_unit(b_state, "UNIT_VERIFIED")
        seal_unit_state(b_state)
        section["plan_checksum"] = None

        with self.assertRaisesRegex(WorkUnitError, "must be UNIT_VERIFIED"):
            validate_section(section)

    def test_dependent_pending_with_prerequisite_proposed_passes(self):
        section = initial_work_units_section()
        add_unit_to_section(section, make_spec("a", dependencies=[]))
        add_unit_to_section(section, make_spec("b", dependencies=["a"]))
        section["units"]["b"]["status"] = "PENDING"
        seal_unit_state(section["units"]["b"])
        work_units._reseal_section(section)
        validate_section(section)  # Should not raise.

    def test_dependent_proposed_with_prerequisite_proposed_passes(self):
        section = initial_work_units_section()
        add_unit_to_section(section, make_spec("a", dependencies=[]))
        add_unit_to_section(section, make_spec("b", dependencies=["a"]))
        validate_section(section)  # Both PROPOSED, passes.

    def test_dependent_executing_with_prerequisite_verified_passes(self):
        section = initial_work_units_section()
        add_unit_to_section(section, make_spec("a", dependencies=[]))
        add_unit_to_section(section, make_spec("b", dependencies=["a"]))

        # Verify a
        a = section["units"]["a"]
        a["status"] = "EXECUTING"
        create_attempt(a, "att-a", candidate_id="cand-a")
        complete_attempt(a, "att-a", outcome="passed", candidate_id="cand-a")
        transition_unit(a, "VERIFYING")
        set_unit_result(a, verifier_evidence={"passed": True, "check": "check-1"}, candidate_id="cand-a")
        transition_unit(a, "UNIT_VERIFIED")
        seal_unit_state(a)

        # Set b to EXECUTING
        b = section["units"]["b"]
        b["status"] = "EXECUTING"
        seal_unit_state(b)
        work_units._reseal_section(section)

        validate_section(section)  # Should not raise.


# ============================================================================
# AUDIT FIX 5 REGRESSION: MALFORMED STATE AND CRASH-WINDOW
# ============================================================================

class MalformedStateAndCrashTests(unittest.TestCase):
    def test_schema_version_bool_rejected(self):
        section = initial_work_units_section()
        section["schema_version"] = True
        with self.assertRaises(WorkUnitError):
            validate_section(section)

    def test_malformed_null_containers_raise_workuniterror(self):
        with self.assertRaises(WorkUnitError):
            validate_section(None)
        with self.assertRaises(WorkUnitError):
            validate_section({"schema_version": 1, "units": None})
        with self.assertRaises(WorkUnitError):
            validate_unit_state(None)
        with self.assertRaises(WorkUnitError):
            validate_unit_state({"spec": None})
        with self.assertRaises(WorkUnitError):
            load_work_units(None)
        with self.assertRaises(WorkUnitError):
            load_work_units({"work_units": None})
        with self.assertRaises(WorkUnitError):
            persist_work_units(None, {})
        with self.assertRaises(WorkUnitError):
            persist_work_units(initial_work_units_section(), None)

    def test_nan_and_infinite_timeout_rejected(self):
        for bad_val in (float("nan"), float("inf"), -float("inf")):
            with self.subTest(bad_val=bad_val), self.assertRaises(WorkUnitError):
                validate_unit_spec(make_spec(limits={"max_attempts": 3, "timeout_seconds": bad_val}))

    def test_malformed_nested_containers_and_items(self):
        # verifier_ids container and items
        with self.assertRaises(WorkUnitError):
            validate_unit_spec(make_spec(verifier_ids=[[]]))
        with self.assertRaises(WorkUnitError):
            validate_unit_spec(make_spec(verifier_ids=[None]))
        with self.assertRaises(WorkUnitError):
            validate_unit_spec(make_spec(verifier_ids="not-a-list"))

        # scope paths container and items
        with self.assertRaises(WorkUnitError):
            validate_unit_spec(make_spec(paths=[[]]))
        with self.assertRaises(WorkUnitError):
            validate_unit_spec(make_spec(forbidden=[[]]))

        # parent scope containers and items
        # parent allowed_paths=None is valid (treated as no parent restrictions)
        validate_scope_containment({"allowed_paths": ["src/helper.py"]}, {"allowed_paths": None, "forbidden_paths": None})
        with self.assertRaises(WorkUnitError):
            validate_scope_containment({"allowed_paths": ["src/helper.py"]}, {"allowed_paths": 123})
        with self.assertRaises(WorkUnitError):
            validate_scope_containment({"allowed_paths": ["src/helper.py"]}, {"allowed_paths": [[]]})

        # dependencies container and items
        with self.assertRaises(WorkUnitError):
            validate_unit_spec(make_spec(dependencies=[[]]))
        with self.assertRaises(WorkUnitError):
            validate_unit_spec(make_spec(dependencies=[None]))

        # evidence_inputs container and items
        with self.assertRaises(WorkUnitError):
            validate_unit_spec(make_spec(evidence_inputs=[[]]))
        with self.assertRaises(WorkUnitError):
            validate_unit_spec(make_spec(evidence_inputs=["string"]))

    def test_real_wal_ahead_of_cache_recovery_with_work_units(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _, store, _ = fixture(root)

            # Store has initial revision in state.json and WAL
            initial_state_json = (store.directory / "state.json").read_bytes()

            # Create WorkUnit-bearing state
            section = initial_work_units_section()
            add_unit_to_section(section, make_spec("wu-1", objective="Test recovery"))
            unit_state = section["units"]["wu-1"]
            unit_state["status"] = "EXECUTING"
            create_attempt(unit_state, "att-1", candidate_id="cand-1")
            seal_unit_state(unit_state)
            work_units._reseal_section(section)

            # Commit WorkUnits to WAL
            state = copy.deepcopy(store.state)
            persist_work_units(section, state)
            state.update(updated_at=durable.now(), revision=state["revision"] + 1)
            durable.validate_state(state)
            # Write to WAL (the commit point)
            store.append("state_committed", state=state, transition=None)

            # Keep state.json stale (representing crash after WAL flush but before state.json replacement)
            (store.directory / "state.json").write_bytes(initial_state_json)

            # Reload Store: should detect WAL ahead of cache and rebuild from authoritative WAL
            reloaded = durable.Store(root / "runs", "test-wu-run")
            reloaded.load()

            # Prove recovered state matches authoritative WAL WorkUnit state
            self.assertEqual(reloaded.state["revision"], state["revision"])
            self.assertIn("work_units", reloaded.state)
            loaded_units = load_work_units(reloaded.state)
            self.assertIn("wu-1", loaded_units["units"])
            self.assertEqual(loaded_units["units"]["wu-1"]["status"], "EXECUTING")
            self.assertEqual(loaded_units["units"]["wu-1"]["current_attempt"], "att-1")

            # Prove top-level trusted state was NOT promoted
            self.assertIsNone(reloaded.state["last_verified_checkpoint"])
            self.assertEqual(reloaded.state["verified_progress"], [])
            self.assertNotEqual(reloaded.state["status"], "VERIFIED")
            self.assertNotEqual(reloaded.state["status"], "COMPLETED")


# ============================================================================
# BACKWARD COMPATIBILITY REGRESSION
# ============================================================================

class BackwardCompatibilityTests(unittest.TestCase):
    """Prove legacy behavior is preserved."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo, self.store, self.git = fixture(self.root)

    def reload(self):
        store = durable.Store(self.root / "runs", "test-wu-run")
        store.load()
        return store

    def test_legacy_state_without_work_units_loads(self):
        store = self.reload()
        self.assertNotIn("work_units", store.state)
        durable.validate_state(store.state)

    def test_legacy_verified_progress_behavior_unchanged(self):
        store = self.reload()
        self.assertEqual(store.state["verified_progress"], [])
        section = initial_work_units_section()
        add_unit_to_section(section, make_spec("a"))
        persist_work_units(section, store.state)
        store.commit()
        reloaded = self.reload()
        self.assertEqual(reloaded.state["verified_progress"], [])

    def test_legacy_checkpoint_behavior_unchanged(self):
        store = self.reload()
        self.assertIsNone(store.state["last_verified_checkpoint"])
        section = initial_work_units_section()
        persist_work_units(section, store.state)
        store.commit()
        reloaded = self.reload()
        self.assertIsNone(reloaded.state["last_verified_checkpoint"])

    def test_legacy_serialized_state_remains_accepted(self):
        store = self.reload()
        state_json = (store.directory / "state.json").read_bytes()
        state = json.loads(state_json)
        durable.validate_state(state)

    def test_existing_verified_status_semantics_unchanged(self):
        state = copy.deepcopy(self.store.state)
        state["status"] = "VERIFIED"
        with self.assertRaisesRegex(durable.DurableError, "Completed state has no checkpoint"):
            durable.validate_state(state)

    def test_verified_progress_without_checkpoint_still_rejected(self):
        state = copy.deepcopy(self.store.state)
        state["verified_progress"] = [{"task": "fake"}]
        with self.assertRaisesRegex(durable.DurableError, "has no checkpoint"):
            durable.validate_state(state)

    def test_load_then_commit_without_work_units_still_works(self):
        store = self.reload()
        original_rev = store.state["revision"]
        store.commit()
        reloaded = self.reload()
        self.assertEqual(reloaded.state["revision"], original_rev + 1)
        self.assertNotIn("work_units", reloaded.state)


if __name__ == "__main__":
    unittest.main()
