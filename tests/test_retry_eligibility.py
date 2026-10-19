"""Unit tests for the deterministic retry-eligibility classifier.

Phase 3.1: Validates ALLOW_VERIFIER_FAILURE conditions, complete authoritative
runtime evidence contracts, and exhaustive fail-closed DENY conditions.
"""

import hashlib
import json
import math
from pathlib import Path
import subprocess
import tempfile
import unittest
from copy import deepcopy

import durable
import harness

from retry_eligibility import (
    ALLOW_VERIFIER_FAILURE,
    CANONICAL_SNAPSHOT_FIELDS,
    DECISION_ALLOW,
    DECISION_DENY,
    DENY_ATTEMPTS_EXHAUSTED,
    DENY_CONTRADICTORY_EVIDENCE,
    DENY_DEFAULT,
    DENY_ENVIRONMENT_DRIFT,
    DENY_INSUFFICIENT_BUDGET,
    DENY_MISSING_EVIDENCE,
    DENY_PROCESS_NOT_IDLE,
    DENY_SCOPE_VIOLATION,
    DENY_TIMEOUT,
    DENY_UNKNOWN_FAILURE,
    DENY_UNVERIFIED_MUTATIONS,
    POLICY_B,
    _durable_digest,
    classify_retry_eligibility,
)


class MockSupervisor:
    """Mock representing authoritative supervision.Supervisor with reconcile and require_idle."""

    def __init__(self, children=None, idle_error=None, reconcile_error=None):
        self._children = (
            children
            if children is not None
            else [{"child_id": "c1", "state": "EXITED", "purpose": "verifier", "pid": 100}]
        )
        self._idle_error = idle_error
        self._reconcile_error = reconcile_error

    def reconcile(self):
        if self._reconcile_error:
            raise RuntimeError(self._reconcile_error)
        return self._children

    def require_idle(self):
        if self._idle_error:
            raise RuntimeError(self._idle_error)
        return True


def make_valid_unit(
    *,
    unit_id="unit-1",
    status="VERIFYING",
    allowed_paths=None,
    forbidden_paths=None,
    verifier_ids=None,
    changed_paths=None,
    max_attempts=2,
    mode="mutation",
    exit_code=1,
    failure_classification="verification_failed",
    outcome="failed",
    elapsed_seconds=10.0,
    timeout_seconds=60.0,
    branch="refs/heads/main",
    snapshot_status="",
    index_sha256="b" * 64,
):
    if allowed_paths is None:
        allowed_paths = ["calculator.py"]
    if forbidden_paths is None:
        forbidden_paths = ["tests/test_calculator.py"]
    if verifier_ids is None:
        verifier_ids = ["v1"]
    if changed_paths is None:
        changed_paths = ["calculator.py"]

    verifier_result = {}
    for vid in verifier_ids:
        verifier_result[vid] = {
            "argv": ["python", "-m", "unittest", f"tests/test_{vid}.py"],
            "exit_code": exit_code,
            "passed": (exit_code == 0),
            "stdout": "",
            "stderr": "AssertionError: 2 != 3",
        }

    pre_snap_body = {
        "repository": r"C:\AI\worktrees\triglav-release",
        "git_directory": r"C:\AI\worktrees\triglav-release\.git",
        "git_identity": [100, 200],
        "head": "a" * 40,
        "branch": branch,
        "status": snapshot_status,
        "index_sha256": index_sha256,
        "files": {
            "calculator.py": {"sha256": hashlib.sha256(b"orig-hash").hexdigest(), "mode": 0o666},
            "tests/test_calculator.py": {"sha256": hashlib.sha256(b"test-hash").hexdigest(), "mode": 0o666},
        },
    }
    pre_snap = dict(pre_snap_body)
    pre_snap["fingerprint"] = _durable_digest(pre_snap_body)

    return {
        "unit_id": unit_id,
        "status": status,
        "spec": {
            "unit_id": unit_id,
            "mode": mode,
            "scope": {
                "allowed_paths": allowed_paths,
                "forbidden_paths": forbidden_paths,
            },
            "verifier_ids": verifier_ids,
            "limits": {
                "max_attempts": max_attempts,
                "timeout_seconds": timeout_seconds,
            },
        },
        "attempts": [
            {
                "attempt_id": f"{unit_id}-attempt-1",
                "outcome": outcome,
                "failure_classification": failure_classification,
                "changed_paths": changed_paths,
                "verifier_result": verifier_result,
                "pre_attempt_snapshot": pre_snap,
                "timeout_info": {
                    "elapsed_seconds": elapsed_seconds,
                    "stage_timeout": 30.0,
                },
            }
        ],
    }


def make_valid_candidate_snapshot(
    changed_paths=None,
    head="a" * 40,
    repository=r"C:\AI\worktrees\triglav-release",
    git_directory=r"C:\AI\worktrees\triglav-release\.git",
    git_identity=None,
    branch="refs/heads/main",
    status="",
    index_sha256="b" * 64,
):
    if changed_paths is None:
        changed_paths = ["calculator.py"]
    if git_identity is None:
        git_identity = [100, 200]
    files = {
        "calculator.py": {"sha256": hashlib.sha256(b"orig-hash").hexdigest(), "mode": 0o666},
        "tests/test_calculator.py": {"sha256": hashlib.sha256(b"test-hash").hexdigest(), "mode": 0o666},
    }
    for p in changed_paths:
        if isinstance(p, str):
            files[p] = {"sha256": hashlib.sha256(f"mutated-{p}".encode()).hexdigest(), "mode": 0o666}
        else:
            files[str(p)] = {"sha256": hashlib.sha256(f"mutated-{p}".encode()).hexdigest(), "mode": 0o666}
    body = {
        "repository": repository,
        "git_directory": git_directory,
        "git_identity": git_identity,
        "head": head,
        "branch": branch,
        "status": status,
        "index_sha256": index_sha256,
        "files": files,
    }
    snap = dict(body)
    snap["fingerprint"] = _durable_digest(body)
    return snap


def default_verifier_registry(verifier_ids=None):
    if verifier_ids is None:
        verifier_ids = ["v1"]
    return {
        vid: {"argv": ["python", "-m", "unittest", f"tests/test_{vid}.py"]}
        for vid in verifier_ids
    }


def default_trusted_kwargs(unit=None, changed_paths=None, verifier_ids=None):
    if unit is not None and isinstance(unit, dict):
        if changed_paths is None:
            attempts = unit.get("attempts")
            if attempts and isinstance(attempts, list) and isinstance(attempts[0], dict):
                changed_paths = attempts[0].get("changed_paths", ["calculator.py"])
            else:
                changed_paths = ["calculator.py"]
        if verifier_ids is None:
            spec = unit.get("spec")
            if spec and isinstance(spec, dict):
                verifier_ids = spec.get("verifier_ids", ["v1"])
            else:
                verifier_ids = ["v1"]
    return {
        "remaining_unit_budget": 50.0,
        "remaining_task_budget": 500.0,
        "supervisor": MockSupervisor(),
        "environment_status": "SAFE",
        "repo_mutated_during_verification": False,
        "verifier_registry": default_verifier_registry(verifier_ids),
        "candidate_snapshot": make_valid_candidate_snapshot(changed_paths),
    }


def make_valid_controller_evidence(
    *,
    child_id="verification_command-01abcdef0123",
    token="01abcdef0123456789abcdef01234567",
    vid="v1",
    exit_code=1,
    termination="natural",
    termination_basis="signaled_without_controller_termination",
    cleanup="observed_empty",
    intended_argv=None,
    resolved_argv=None,
    pid=1001,
    executable="C:/Python311/python.exe",
    job=None,
    execution_started=True,
    broker=None,
    source="controller_native_handles",
    run_id="run-1",
    unit_id="unit-1",
    attempt_id="unit-1-attempt-1",
):
    if intended_argv is None:
        intended_argv = ["python", "-m", "unittest", f"tests/test_{vid}.py"]
    if resolved_argv is None:
        resolved_argv = ["C:/Python311/python.exe", "-m", "unittest", f"tests/test_{vid}.py"]
    if job is None:
        job = f"Local\\harness-{token}"

    evidence = {
        "schema_version": 1,
        "source": source,
        "child_id": child_id,
        "broker": broker,
        "verifier": {
            "pid": pid,
            "identity": {
                "created": "2026-10-16T10:00:00Z",
                "executable": executable,
            },
        },
        "intended_argv": list(intended_argv),
        "intended_command_sha256": hashlib.sha256(json.dumps(intended_argv, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "resolved_argv": list(resolved_argv),
        "resolved_command_sha256": hashlib.sha256(json.dumps(resolved_argv, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "job": job,
        "termination": termination,
        "termination_basis": termination_basis,
        "observed_exit_code": exit_code,
        "cleanup": cleanup,
        "execution_started": execution_started,
    }
    if run_id is not None:
        evidence["run_id"] = run_id
    if unit_id is not None:
        evidence["unit_id"] = unit_id
    if attempt_id is not None:
        evidence["attempt_id"] = attempt_id
    return evidence


def make_valid_policy_b_unit(
    *,
    unit_id="unit-1",
    status="VERIFYING",
    allowed_paths=None,
    forbidden_paths=None,
    verifier_ids=None,
    changed_paths=None,
    max_attempts=2,
    mode="mutation",
    exit_code=1,
    failure_classification="verification_failed",
    outcome="failed",
    elapsed_seconds=10.0,
    timeout_seconds=60.0,
    termination="natural",
    termination_basis="signaled_without_controller_termination",
    cleanup="observed_empty",
    descendant_cleanup=None,
    run_id="run-1",
):
    unit = make_valid_unit(
        unit_id=unit_id,
        status=status,
        allowed_paths=allowed_paths,
        forbidden_paths=forbidden_paths,
        verifier_ids=verifier_ids,
        changed_paths=changed_paths,
        max_attempts=max_attempts,
        mode=mode,
        exit_code=exit_code,
        failure_classification=failure_classification,
        outcome=outcome,
        elapsed_seconds=elapsed_seconds,
        timeout_seconds=timeout_seconds,
    )
    if run_id is not None:
        unit["run_id"] = run_id
    if verifier_ids is None:
        verifier_ids = ["v1"]

    for idx, vid in enumerate(verifier_ids, 1):
        token = f"{idx:02d}abcdef0123456789abcdef01234567"
        child_id = f"verification_command-{token[:12]}"
        pid = 1000 + idx
        v_entry = unit["attempts"][0]["verifier_result"][vid]
        evidence = make_valid_controller_evidence(
            child_id=child_id,
            token=token,
            vid=vid,
            exit_code=v_entry["exit_code"],
            termination=termination,
            termination_basis=termination_basis,
            cleanup=cleanup,
            pid=pid,
            run_id=run_id,
            unit_id=unit_id,
            attempt_id=unit["attempts"][0]["attempt_id"],
        )
        v_entry["controller_evidence"] = evidence
        v_entry["child_id"] = child_id
        if descendant_cleanup is not None:
            v_entry["descendant_cleanup"] = descendant_cleanup
    return unit


def make_valid_policy_b_supervisor(
    verifier_ids=None,
    exit_code=1,
    termination="natural",
    descendant_cleanup=None,
    run_id="run-1",
    unit_id="unit-1",
):
    if verifier_ids is None:
        verifier_ids = ["v1"]
    children = []
    for idx, vid in enumerate(verifier_ids, 1):
        token = f"{idx:02d}abcdef0123456789abcdef01234567"
        child_id = f"verification_command-{token[:12]}"
        argv = ["C:/Python311/python.exe", "-m", "unittest", f"tests/test_{vid}.py"]
        cmd_hash = hashlib.sha256(json.dumps(argv, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        child_rec = {
            "child_id": child_id,
            "state": "EXITED",
            "purpose": "verification_command",
            "pid": 1000 + idx,
            "controller_pid": 9999,
            "executable": "C:/Python311/python.exe",
            "command_sha256": cmd_hash,
            "containment": "job",
            "job": f"Local\\harness-{token}",
            "token": token,
            "started_at": 1000.0,
            "boot": "boot-guid-1234",
            "last_heartbeat": None,
            "exit_code": exit_code,
            "termination": termination,
            "reason": "exit_observed",
            "identity": {"created": "2026-10-16T10:00:00Z", "executable": "C:/Python311/python.exe"},
        }
        if run_id is not None:
            child_rec["run_id"] = run_id
        if unit_id is not None:
            child_rec["unit_id"] = unit_id
            child_rec["attempt_id"] = f"{unit_id}-attempt-1"
        if descendant_cleanup is not None:
            child_rec["descendant_cleanup"] = descendant_cleanup
        children.append(child_rec)
    return MockSupervisor(children=children)


def default_policy_b_kwargs(
    unit=None,
    changed_paths=None,
    verifier_ids=None,
    exit_code=1,
    termination="natural",
    descendant_cleanup=None,
    run_id="run-1",
):
    kwargs = default_trusted_kwargs(unit=unit, changed_paths=changed_paths, verifier_ids=verifier_ids)
    unit_id = unit.get("unit_id", "unit-1") if isinstance(unit, dict) else "unit-1"
    if verifier_ids is None:
        if unit is not None and isinstance(unit, dict):
            spec = unit.get("spec")
            if spec and isinstance(spec, dict):
                verifier_ids = spec.get("verifier_ids", ["v1"])
        if verifier_ids is None:
            verifier_ids = ["v1"]
    kwargs["supervisor"] = make_valid_policy_b_supervisor(
        verifier_ids=verifier_ids,
        exit_code=exit_code,
        termination=termination,
        descendant_cleanup=descendant_cleanup,
        run_id=run_id,
        unit_id=unit_id,
    )
    # Complete controller-captured verification before and after snapshots matching candidate
    kwargs["verification_snapshot_before"] = deepcopy(kwargs["candidate_snapshot"])
    kwargs["verification_snapshot_after"] = deepcopy(kwargs["candidate_snapshot"])
    # Explicit trusted controller opt-in (Requirement 8)
    kwargs["policy"] = POLICY_B
    return kwargs


class TestRetryEligibilityPositive(unittest.TestCase):
    """Positive test cases where retry eligibility is strictly allowed."""

    def test_single_failing_verifier_clean_scope_allows_retry(self):
        unit = make_valid_unit()
        decision = classify_retry_eligibility(
            unit,
            **default_trusted_kwargs(unit),
        )
        self.assertTrue(decision.is_allowed())
        self.assertTrue(bool(decision))
        self.assertEqual(decision.decision, DECISION_ALLOW)
        self.assertEqual(decision.reason_code, ALLOW_VERIFIER_FAILURE)
        self.assertEqual(decision.details["failed_verifiers"], ["v1"])

    def test_multiple_verifiers_partial_failure_allows_retry(self):
        unit = make_valid_unit(verifier_ids=["v1", "v2"])
        unit["attempts"][0]["verifier_result"]["v1"]["exit_code"] = 0
        unit["attempts"][0]["verifier_result"]["v1"]["passed"] = True
        unit["attempts"][0]["verifier_result"]["v2"]["exit_code"] = 1
        unit["attempts"][0]["verifier_result"]["v2"]["passed"] = False

        decision = classify_retry_eligibility(unit, **default_trusted_kwargs(unit))
        self.assertTrue(decision.is_allowed())
        self.assertEqual(decision.reason_code, ALLOW_VERIFIER_FAILURE)
        self.assertEqual(decision.details["failed_verifiers"], ["v2"])

    def test_model_server_child_allowed_when_ownership_proven(self):
        unit = make_valid_unit()
        kwargs = default_trusted_kwargs(unit)
        kwargs["supervisor"] = MockSupervisor(
            children=[
                {
                    "child_id": "llama-gateway",
                    "state": "RUNNING",
                    "purpose": "model_server",
                    "containment": "job",
                    "pid": 5678,
                    "identity": {"created": "2026-10-16T00:00:00Z"},
                }
            ]
        )
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertTrue(decision.is_allowed())

    def test_verification_snapshots_matching_allows_retry(self):
        unit = make_valid_unit()
        kwargs = default_trusted_kwargs(unit)
        del kwargs["repo_mutated_during_verification"]
        snap = {"head": "abc", "fingerprint": "f1"}
        kwargs["verification_snapshot_before"] = snap
        kwargs["verification_snapshot_after"] = snap
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertTrue(decision.is_allowed())


class TestRetryEligibilityVerifierRegistryBinding(unittest.TestCase):
    """Validation that verifier registry is mandatory and exact argv binding is enforced."""

    def test_missing_verifier_registry_denies(self):
        unit = make_valid_unit()
        kwargs = default_trusted_kwargs(unit)
        del kwargs["verifier_registry"]
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)

    def test_verifier_id_missing_from_registry_denies(self):
        unit = make_valid_unit(verifier_ids=["v1", "v_unregistered"])
        kwargs = default_trusted_kwargs(unit)
        del kwargs["verifier_registry"]["v_unregistered"]
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)

    def test_verifier_argv_mismatch_against_registry_denies(self):
        unit = make_valid_unit()
        unit["attempts"][0]["verifier_result"]["v1"]["argv"] = ["python", "-m", "unittest", "tampered_test.py"]
        decision = classify_retry_eligibility(unit, **default_trusted_kwargs(unit))
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_CONTRADICTORY_EVIDENCE)


class TestRetryEligibilitySchedulerTransitions(unittest.TestCase):
    """Validation of unit status against existing scheduler lifecycle transitions."""

    def test_valid_statuses_for_retry(self):
        for valid_status in ("VERIFYING", "EXECUTING", "REPAIR_PENDING"):
            unit = make_valid_unit(status=valid_status)
            decision = classify_retry_eligibility(unit, **default_trusted_kwargs(unit))
            self.assertTrue(decision.is_allowed())

    def test_invalid_scheduler_statuses_deny(self):
        for bad_status in ("PROPOSED", "VALIDATED", "PENDING", "READY"):
            unit = make_valid_unit(status=bad_status)
            decision = classify_retry_eligibility(unit, **default_trusted_kwargs(unit))
            self.assertFalse(decision.is_allowed())
            self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)

    def test_terminal_unit_statuses_deny_exhausted(self):
        for terminal_status in ("UNIT_FAILED", "UNIT_VERIFIED"):
            unit = make_valid_unit(status=terminal_status)
            decision = classify_retry_eligibility(unit, **default_trusted_kwargs(unit))
            self.assertFalse(decision.is_allowed())
            self.assertEqual(decision.reason_code, DENY_ATTEMPTS_EXHAUSTED)


class TestRetryEligibilityDurableSnapshotSchema(unittest.TestCase):
    """Validation of real durable.snapshot() schema, identity, and coverage."""

    def test_missing_snapshot_field_denies(self):
        for field in ("repository", "git_directory", "git_identity", "head", "index_sha256", "files", "fingerprint"):
            unit = make_valid_unit()
            kwargs = default_trusted_kwargs(unit)
            del kwargs["candidate_snapshot"][field]
            decision = classify_retry_eligibility(unit, **kwargs)
            self.assertFalse(decision.is_allowed())
            self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)

    def test_candidate_git_head_drift_denies(self):
        unit = make_valid_unit()
        kwargs = default_trusted_kwargs(unit)
        kwargs["candidate_snapshot"]["head"] = "drifted-head-hash"
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_UNVERIFIED_MUTATIONS)

    def test_git_identity_mismatch_denies(self):
        unit = make_valid_unit()
        kwargs = default_trusted_kwargs(unit)
        kwargs["candidate_snapshot"]["git_identity"] = [999, 888]
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_UNVERIFIED_MUTATIONS)

    def test_repository_path_mismatch_denies(self):
        unit = make_valid_unit()
        kwargs = default_trusted_kwargs(unit)
        kwargs["candidate_snapshot"]["repository"] = "C:/Other/Repo"
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_UNVERIFIED_MUTATIONS)

    def test_missing_frozen_path_in_snapshot_denies(self):
        unit = make_valid_unit()
        kwargs = default_trusted_kwargs(unit)
        del unit["attempts"][0]["pre_attempt_snapshot"]["files"]["tests/test_calculator.py"]
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)

    def test_empty_snapshot_files_denies(self):
        unit = make_valid_unit()
        kwargs = default_trusted_kwargs(unit)
        kwargs["candidate_snapshot"]["files"] = {}
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)

    def test_candidate_snapshot_with_untracked_mutation_denies(self):
        unit = make_valid_unit(changed_paths=["calculator.py"])
        kwargs = default_trusted_kwargs(unit)
        unit["attempts"][0]["pre_attempt_snapshot"]["files"]["untracked.py"] = {
            "sha256": "orig-untracked",
            "mode": 33188,
        }
        kwargs["candidate_snapshot"]["files"]["untracked.py"] = {
            "sha256": "mutated-untracked",
            "mode": 33188,
        }
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_UNVERIFIED_MUTATIONS)

    def test_changed_paths_not_reflected_in_snapshot_denies(self):
        unit = make_valid_unit(changed_paths=["calculator.py"])
        kwargs = default_trusted_kwargs(unit)
        kwargs["candidate_snapshot"]["files"]["calculator.py"] = dict(
            unit["attempts"][0]["pre_attempt_snapshot"]["files"]["calculator.py"]
        )
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_CONTRADICTORY_EVIDENCE)

    def test_forbidden_path_mutated_in_snapshot_denies(self):
        unit = make_valid_unit(changed_paths=["tests/test_calculator.py"])
        kwargs = default_trusted_kwargs(unit, changed_paths=["tests/test_calculator.py"])
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_SCOPE_VIOLATION)

    def test_missing_pre_attempt_snapshot_denies(self):
        unit = make_valid_unit()
        del unit["attempts"][0]["pre_attempt_snapshot"]
        decision = classify_retry_eligibility(unit, **default_trusted_kwargs(unit))
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)

    def test_missing_candidate_snapshot_denies(self):
        unit = make_valid_unit()
        kwargs = default_trusted_kwargs(unit)
        kwargs["candidate_snapshot"] = None
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)

    def test_malformed_candidate_snapshot_files_type_denies(self):
        unit = make_valid_unit()
        kwargs = default_trusted_kwargs(unit)
        kwargs["candidate_snapshot"]["files"] = "not-a-dict"
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)

    def test_malformed_snapshot_file_entry_denies(self):
        unit = make_valid_unit()
        kwargs = default_trusted_kwargs(unit)
        kwargs["candidate_snapshot"]["files"]["calculator.py"] = {"sha256": "abc"}
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)


class TestRetryEligibilityVerifierMutations(unittest.TestCase):
    """Validation of verifier mutation guards and tamper detection."""

    def test_repo_mutated_during_verification_flag_denies(self):
        unit = make_valid_unit()
        kwargs = default_trusted_kwargs(unit)
        kwargs["repo_mutated_during_verification"] = True
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_UNVERIFIED_MUTATIONS)

    def test_verification_snapshot_mismatch_denies(self):
        unit = make_valid_unit()
        kwargs = default_trusted_kwargs(unit)
        del kwargs["repo_mutated_during_verification"]
        kwargs["verification_snapshot_before"] = {"head": "abc", "fingerprint": "f1"}
        kwargs["verification_snapshot_after"] = {"head": "def", "fingerprint": "f2"}
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_UNVERIFIED_MUTATIONS)

    def test_missing_verifier_mutation_evidence_denies(self):
        unit = make_valid_unit()
        kwargs = default_trusted_kwargs(unit)
        del kwargs["repo_mutated_during_verification"]
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)

    def test_non_dict_verification_snapshots_denies(self):
        unit = make_valid_unit()
        kwargs = default_trusted_kwargs(unit)
        del kwargs["repo_mutated_during_verification"]
        kwargs["verification_snapshot_before"] = "not-a-dict"
        kwargs["verification_snapshot_after"] = "not-a-dict"
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)

    def test_verifier_tampered_repo_classification_denies(self):
        unit = make_valid_unit(failure_classification="verifier_tampered_repo")
        decision = classify_retry_eligibility(unit, **default_trusted_kwargs(unit))
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_UNVERIFIED_MUTATIONS)

    def test_git_error_classification_denies(self):
        unit = make_valid_unit(failure_classification="git_error")
        decision = classify_retry_eligibility(unit, **default_trusted_kwargs(unit))
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_UNVERIFIED_MUTATIONS)


class TestRetryEligibilityEnvironment(unittest.TestCase):
    """Validation of environment drift and UNSAFE execution decisions."""

    def test_environment_drift_string_denies(self):
        for bad_env in ("UNSAFE", "REVALIDATION_REQUIRED", "UNKNOWN", "DRIFT"):
            unit = make_valid_unit()
            kwargs = default_trusted_kwargs(unit)
            kwargs["environment_status"] = bad_env
            decision = classify_retry_eligibility(unit, **kwargs)
            self.assertFalse(decision.is_allowed())
            self.assertEqual(decision.reason_code, DENY_ENVIRONMENT_DRIFT)

    def test_environment_drift_dict_denies(self):
        unit = make_valid_unit()
        kwargs = default_trusted_kwargs(unit)
        kwargs["environment_status"] = {"decision": "UNSAFE", "changes": ["binary_changed"]}
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_ENVIRONMENT_DRIFT)

    def test_missing_environment_status_denies(self):
        unit = make_valid_unit()
        kwargs = default_trusted_kwargs(unit)
        kwargs["environment_status"] = None
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)

    def test_invalid_environment_status_type_denies(self):
        unit = make_valid_unit()
        kwargs = default_trusted_kwargs(unit)
        kwargs["environment_status"] = 12345
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)


class TestRetryEligibilitySupervisorAuthority(unittest.TestCase):
    """Validation of real supervision.Supervisor instance authority."""

    def test_missing_supervisor_denies(self):
        unit = make_valid_unit()
        kwargs = default_trusted_kwargs(unit)
        kwargs["supervisor"] = None
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)

    def test_supervisor_missing_require_idle_method_denies(self):
        unit = make_valid_unit()
        kwargs = default_trusted_kwargs(unit)

        class IncompleteSupervisor:
            def reconcile(self):
                return []

        kwargs["supervisor"] = IncompleteSupervisor()
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)

    def test_require_idle_refused_by_supervisor_denies(self):
        unit = make_valid_unit()
        kwargs = default_trusted_kwargs(unit)
        kwargs["require_idle"] = True
        kwargs["supervisor"] = MockSupervisor(idle_error="PROCESS_RECOVERY: child still active")
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_PROCESS_NOT_IDLE)

    def test_unproven_model_server_ownership_denies(self):
        unit = make_valid_unit()
        kwargs = default_trusted_kwargs(unit)
        kwargs["supervisor"] = MockSupervisor(
            children=[{"child_id": "llama-gateway", "state": "RUNNING", "purpose": "model_server"}]
        )
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_PROCESS_NOT_IDLE)

    def test_unrecognized_supervisor_state_denies(self):
        unit = make_valid_unit()
        kwargs = default_trusted_kwargs(unit)
        kwargs["supervisor"] = MockSupervisor(
            children=[{"child_id": "c1", "state": "UNREGISTERED", "purpose": "verifier"}]
        )
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_PROCESS_NOT_IDLE)

    def test_unclean_supervisor_state_denies(self):
        for unclean in ("UNKNOWN", "STARTING", "LOST", "STALE"):
            unit = make_valid_unit()
            kwargs = default_trusted_kwargs(unit)
            kwargs["supervisor"] = MockSupervisor(
                children=[{"child_id": "c1", "state": unclean, "purpose": "verifier"}]
            )
            decision = classify_retry_eligibility(unit, **kwargs)
            self.assertFalse(decision.is_allowed())
            self.assertEqual(decision.reason_code, DENY_PROCESS_NOT_IDLE)

    def test_active_unauthorized_child_denies(self):
        unit = make_valid_unit()
        kwargs = default_trusted_kwargs(unit)
        kwargs["supervisor"] = MockSupervisor(
            children=[{"child_id": "test-runner", "state": "RUNNING", "purpose": "verifier"}]
        )
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_PROCESS_NOT_IDLE)

    def test_malformed_supervisor_child_item_denies(self):
        unit = make_valid_unit()
        kwargs = default_trusted_kwargs(unit)
        kwargs["supervisor"] = MockSupervisor(children=["not-a-dict"])
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)

    def test_supervisor_reconcile_exception_denies(self):
        unit = make_valid_unit()
        kwargs = default_trusted_kwargs(unit)
        kwargs["supervisor"] = MockSupervisor(reconcile_error="Reconcile IPC failed")
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_PROCESS_NOT_IDLE)


class TestRetryEligibilityBudgetParams(unittest.TestCase):
    """Validation of min_budget_seconds parameter strictness."""

    def test_min_budget_seconds_invalid_values_deny(self):
        for bad_min in (0, -1.0, float("nan"), float("inf"), "1.0", True):
            unit = make_valid_unit()
            decision = classify_retry_eligibility(
                unit,
                min_budget_seconds=bad_min,
                **default_trusted_kwargs(unit),
            )
            self.assertFalse(decision.is_allowed())
            self.assertEqual(decision.reason_code, DENY_INSUFFICIENT_BUDGET)


class TestRetryEligibilityBudgets(unittest.TestCase):
    """Validation of budget numeric properties and derivations."""

    def test_missing_task_budget_denies(self):
        unit = make_valid_unit()
        kwargs = default_trusted_kwargs(unit)
        del kwargs["remaining_task_budget"]
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)

    def test_nan_task_budget_denies(self):
        unit = make_valid_unit()
        kwargs = default_trusted_kwargs(unit)
        kwargs["remaining_task_budget"] = float("nan")
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_INSUFFICIENT_BUDGET)

    def test_inf_task_budget_denies(self):
        unit = make_valid_unit()
        kwargs = default_trusted_kwargs(unit)
        kwargs["remaining_task_budget"] = float("inf")
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_INSUFFICIENT_BUDGET)

    def test_negative_task_budget_denies(self):
        unit = make_valid_unit()
        kwargs = default_trusted_kwargs(unit)
        kwargs["remaining_task_budget"] = -5.0
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_INSUFFICIENT_BUDGET)

    def test_boolean_task_budget_denies(self):
        unit = make_valid_unit()
        kwargs = default_trusted_kwargs(unit)
        kwargs["remaining_task_budget"] = True
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_INSUFFICIENT_BUDGET)

    def test_nan_unit_budget_denies(self):
        unit = make_valid_unit()
        kwargs = default_trusted_kwargs(unit)
        kwargs["remaining_unit_budget"] = float("nan")
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_INSUFFICIENT_BUDGET)

    def test_boolean_unit_budget_denies(self):
        unit = make_valid_unit()
        kwargs = default_trusted_kwargs(unit)
        kwargs["remaining_unit_budget"] = False
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_INSUFFICIENT_BUDGET)

    def test_missing_unit_budget_without_timeout_info_denies(self):
        unit = make_valid_unit()
        del unit["attempts"][0]["timeout_info"]
        kwargs = default_trusted_kwargs(unit)
        del kwargs["remaining_unit_budget"]
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)

    def test_calculated_remaining_unit_budget_exhausted_denies(self):
        unit = make_valid_unit(elapsed_seconds=59.5, timeout_seconds=60.0)
        kwargs = default_trusted_kwargs(unit)
        del kwargs["remaining_unit_budget"]
        decision = classify_retry_eligibility(unit, min_budget_seconds=1.0, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_INSUFFICIENT_BUDGET)

    def test_calculated_unit_budget_malformed_elapsed_denies(self):
        unit = make_valid_unit()
        unit["attempts"][0]["timeout_info"]["elapsed_seconds"] = float("nan")
        kwargs = default_trusted_kwargs(unit)
        del kwargs["remaining_unit_budget"]
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_INSUFFICIENT_BUDGET)


class TestRetryEligibilityVerifierOutcomes(unittest.TestCase):
    """Validation of verifier exit codes, types, and contradiction detection."""

    def test_exit_code_is_boolean_denies(self):
        unit = make_valid_unit()
        unit["attempts"][0]["verifier_result"]["v1"]["exit_code"] = True
        decision = classify_retry_eligibility(unit, **default_trusted_kwargs(unit))
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)

    def test_exit_code_is_string_denies(self):
        unit = make_valid_unit()
        unit["attempts"][0]["verifier_result"]["v1"]["exit_code"] = "1"
        decision = classify_retry_eligibility(unit, **default_trusted_kwargs(unit))
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)

    def test_passed_is_integer_denies(self):
        unit = make_valid_unit()
        unit["attempts"][0]["verifier_result"]["v1"]["passed"] = 0
        decision = classify_retry_eligibility(unit, **default_trusted_kwargs(unit))
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)

    def test_passed_is_string_denies(self):
        unit = make_valid_unit()
        unit["attempts"][0]["verifier_result"]["v1"]["passed"] = "False"
        decision = classify_retry_eligibility(unit, **default_trusted_kwargs(unit))
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)

    def test_argv_is_empty_denies(self):
        unit = make_valid_unit()
        kwargs = default_trusted_kwargs(unit)
        kwargs["verifier_registry"]["v1"]["argv"] = []
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)

    def test_argv_in_result_missing_denies(self):
        unit = make_valid_unit()
        del unit["attempts"][0]["verifier_result"]["v1"]["argv"]
        decision = classify_retry_eligibility(unit, **default_trusted_kwargs(unit))
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)

    def test_exit_code_zero_with_passed_false_denies_contradictory(self):
        unit = make_valid_unit()
        unit["attempts"][0]["verifier_result"]["v1"]["exit_code"] = 0
        unit["attempts"][0]["verifier_result"]["v1"]["passed"] = False
        decision = classify_retry_eligibility(unit, **default_trusted_kwargs(unit))
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_CONTRADICTORY_EVIDENCE)

    def test_exit_code_nonzero_with_passed_true_denies_contradictory(self):
        unit = make_valid_unit()
        unit["attempts"][0]["verifier_result"]["v1"]["exit_code"] = 1
        unit["attempts"][0]["verifier_result"]["v1"]["passed"] = True
        decision = classify_retry_eligibility(unit, **default_trusted_kwargs(unit))
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_CONTRADICTORY_EVIDENCE)

    def test_all_verifiers_exit_zero_on_failed_attempt_denies_contradictory(self):
        unit = make_valid_unit()
        unit["attempts"][0]["verifier_result"]["v1"]["exit_code"] = 0
        unit["attempts"][0]["verifier_result"]["v1"]["passed"] = True
        decision = classify_retry_eligibility(unit, **default_trusted_kwargs(unit))
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_CONTRADICTORY_EVIDENCE)

    def test_attempt_outcome_passed_denies_contradictory(self):
        for passed_outcome in ("passed", "success"):
            unit = make_valid_unit(outcome=passed_outcome)
            decision = classify_retry_eligibility(unit, **default_trusted_kwargs(unit))
            self.assertFalse(decision.is_allowed())
            self.assertEqual(decision.reason_code, DENY_CONTRADICTORY_EVIDENCE)

    def test_signal_or_kill_exit_code_denies(self):
        for bad_code in (-9, -15, 137, 143):
            unit = make_valid_unit()
            unit["attempts"][0]["verifier_result"]["v1"]["exit_code"] = bad_code
            decision = classify_retry_eligibility(unit, **default_trusted_kwargs(unit))
            self.assertFalse(decision.is_allowed())
            self.assertEqual(decision.reason_code, DENY_UNKNOWN_FAILURE)


class TestRetryEligibilityAttempts(unittest.TestCase):
    """Negative tests for attempt exhaustion and limits."""

    def test_two_attempts_already_made_denies_exhausted(self):
        unit = make_valid_unit()
        second_attempt = dict(unit["attempts"][0])
        second_attempt["attempt_id"] = "unit-1-attempt-2"
        unit["attempts"].append(second_attempt)

        decision = classify_retry_eligibility(unit, **default_trusted_kwargs(unit))
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_ATTEMPTS_EXHAUSTED)

    def test_max_attempts_one_denies_exhausted(self):
        unit = make_valid_unit(max_attempts=1)
        decision = classify_retry_eligibility(unit, **default_trusted_kwargs(unit))
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_ATTEMPTS_EXHAUSTED)

    def test_max_attempts_greater_than_two_denies(self):
        unit = make_valid_unit(max_attempts=3)
        decision = classify_retry_eligibility(unit, **default_trusted_kwargs(unit))
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_ATTEMPTS_EXHAUSTED)

    def test_max_attempts_not_int_denies(self):
        for bad_val in (True, False, "2", 2.0, None):
            unit = make_valid_unit()
            unit["spec"]["limits"]["max_attempts"] = bad_val
            decision = classify_retry_eligibility(unit, **default_trusted_kwargs(unit))
            self.assertFalse(decision.is_allowed())
            self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)


class TestRetryEligibilityScope(unittest.TestCase):
    """Negative tests for scope violations."""

    def test_forbidden_path_modified_denies_scope_violation(self):
        paths = ["calculator.py", "tests/test_calculator.py"]
        unit = make_valid_unit(changed_paths=paths)
        decision = classify_retry_eligibility(unit, **default_trusted_kwargs(unit, paths))
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_SCOPE_VIOLATION)

    def test_unauthorized_path_modified_denies_scope_violation(self):
        paths = ["other.py"]
        unit = make_valid_unit(changed_paths=paths)
        decision = classify_retry_eligibility(unit, **default_trusted_kwargs(unit, paths))
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_SCOPE_VIOLATION)

    def test_git_metadata_modified_denies_scope_violation(self):
        paths = [".git/config"]
        unit = make_valid_unit(changed_paths=paths)
        decision = classify_retry_eligibility(unit, **default_trusted_kwargs(unit, paths))
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_SCOPE_VIOLATION)

    def test_read_only_unit_mutating_files_denies_scope_violation(self):
        unit = make_valid_unit(mode="read_only", changed_paths=["calculator.py"])
        decision = classify_retry_eligibility(unit, **default_trusted_kwargs(unit))
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_SCOPE_VIOLATION)


class TestRetryEligibilityTimeout(unittest.TestCase):
    """Negative tests for timeouts."""

    def test_outcome_timeout_denies_timeout(self):
        unit = make_valid_unit(outcome="timeout")
        decision = classify_retry_eligibility(unit, **default_trusted_kwargs(unit))
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_TIMEOUT)

    def test_failure_classification_timeout_denies_timeout(self):
        for fc in ("executor_timeout", "unit_timeout", "verifier_timeout", "timeout"):
            unit = make_valid_unit(failure_classification=fc)
            decision = classify_retry_eligibility(unit, **default_trusted_kwargs(unit))
            self.assertFalse(decision.is_allowed())
            self.assertEqual(decision.reason_code, DENY_TIMEOUT)


class TestRetryEligibilityCrashProofing(unittest.TestCase):
    """Hardening tests: Malformed inputs must return structured DENY, never crash."""

    def test_malformed_scope_allowed_paths_type_denies(self):
        unit = make_valid_unit()
        unit["spec"]["scope"]["allowed_paths"] = "not-a-list"
        decision = classify_retry_eligibility(unit, **default_trusted_kwargs(unit))
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)

    def test_malformed_changed_paths_contains_none_denies(self):
        unit = make_valid_unit()
        unit["attempts"][0]["changed_paths"] = [None]
        decision = classify_retry_eligibility(unit, **default_trusted_kwargs(unit))
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)

    def test_not_a_dict_unit_denies(self):
        decision = classify_retry_eligibility("not-a-dict", **default_trusted_kwargs())
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)

    def test_missing_spec_or_limits_denies(self):
        unit = make_valid_unit()
        del unit["spec"]
        decision = classify_retry_eligibility(unit, **default_trusted_kwargs(unit))
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)

    def test_empty_attempts_list_denies(self):
        unit = make_valid_unit()
        unit["attempts"] = []
        decision = classify_retry_eligibility(unit, **default_trusted_kwargs(unit))
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)

    def test_scope_violation_failure_classification_denies(self):
        unit = make_valid_unit(failure_classification="scope_violation")
        decision = classify_retry_eligibility(unit, **default_trusted_kwargs(unit))
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_SCOPE_VIOLATION)

    def test_unexpected_write_failure_classification_denies(self):
        unit = make_valid_unit(failure_classification="unexpected_write")
        decision = classify_retry_eligibility(unit, **default_trusted_kwargs(unit))
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_SCOPE_VIOLATION)

    def test_containment_failed_classification_denies(self):
        for fc in ("containment_failed", "cleanup_failed"):
            unit = make_valid_unit(failure_classification=fc)
            decision = classify_retry_eligibility(unit, **default_trusted_kwargs(unit))
            self.assertFalse(decision.is_allowed())
            self.assertEqual(decision.reason_code, DENY_PROCESS_NOT_IDLE)

    def test_unclassified_failure_denies_unknown(self):
        unit = make_valid_unit(failure_classification="arbitrary_issue")
        decision = classify_retry_eligibility(unit, **default_trusted_kwargs(unit))
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_UNKNOWN_FAILURE)

    def test_interrupted_outcome_denies_unknown(self):
        for bad_outcome in ("interrupted", "error", "aborted"):
            unit = make_valid_unit(outcome=bad_outcome)
            decision = classify_retry_eligibility(unit, **default_trusted_kwargs(unit))
            self.assertFalse(decision.is_allowed())
            self.assertEqual(decision.reason_code, DENY_UNKNOWN_FAILURE)

    def test_model_provided_safety_flags_ignored(self):
        unit = make_valid_unit()
        unit["model_assertion"] = "SAFE_TO_RETRY"
        unit["retry_requested"] = True
        unit["attempts"][0]["override_safety"] = True
        unit["attempts"][0]["all_tests_passed"] = True

        decision = classify_retry_eligibility(unit, **default_trusted_kwargs(unit))
        self.assertTrue(decision.is_allowed())
        self.assertEqual(decision.reason_code, ALLOW_VERIFIER_FAILURE)

        unit_bad = make_valid_unit(changed_paths=["tests/test_calculator.py"])
        unit_bad["model_assertion"] = "SAFE_TO_RETRY"
        unit_bad["retry_requested"] = True
        decision_bad = classify_retry_eligibility(
            unit_bad,
            **default_trusted_kwargs(unit_bad, ["tests/test_calculator.py"]),
        )
        self.assertFalse(decision_bad.is_allowed())
        self.assertEqual(decision_bad.reason_code, DENY_SCOPE_VIOLATION)

    def test_default_deny_on_unexpected_exception(self):
        class ExplodingDict(dict):
            def get(self, key, default=None):
                if key == "status":
                    raise RuntimeError("Unexpected internal crash")
                return super().get(key, default)

        unit = ExplodingDict()
        decision = classify_retry_eligibility(unit, **default_trusted_kwargs())
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_DEFAULT)


class TestPolicyBRetryEligibility(unittest.TestCase):
    """Focused validation of Policy B bounded recovery contracts."""

    # 1. Genuine nonzero verifier exit
    def test_policy_b_single_failing_verifier_allows_retry(self):
        unit = make_valid_policy_b_unit(exit_code=1)
        kwargs = default_policy_b_kwargs(unit, exit_code=1)
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertTrue(decision.is_allowed())
        self.assertEqual(decision.decision, DECISION_ALLOW)
        self.assertEqual(decision.reason_code, ALLOW_VERIFIER_FAILURE)
        self.assertEqual(decision.details["failed_verifiers"], ["v1"])
        self.assertEqual(decision.details["policy"], "B")

    def test_policy_b_multi_verifier_partial_failure_allows_retry(self):
        unit = make_valid_policy_b_unit(verifier_ids=["v1", "v2"])
        unit["attempts"][0]["verifier_result"]["v1"]["exit_code"] = 0
        unit["attempts"][0]["verifier_result"]["v1"]["passed"] = True
        unit["attempts"][0]["verifier_result"]["v1"]["controller_evidence"]["observed_exit_code"] = 0
        unit["attempts"][0]["verifier_result"]["v2"]["exit_code"] = 1
        unit["attempts"][0]["verifier_result"]["v2"]["passed"] = False
        unit["attempts"][0]["verifier_result"]["v2"]["controller_evidence"]["observed_exit_code"] = 1

        kwargs = default_policy_b_kwargs(unit, verifier_ids=["v1", "v2"])
        kwargs["supervisor"]._children[0]["exit_code"] = 0
        kwargs["supervisor"]._children[1]["exit_code"] = 1

        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertTrue(decision.is_allowed())
        self.assertEqual(decision.reason_code, ALLOW_VERIFIER_FAILURE)
        self.assertEqual(decision.details["failed_verifiers"], ["v2"])

    def test_policy_b_distinct_nonzero_exit_code_two_allows_retry(self):
        unit = make_valid_policy_b_unit(exit_code=2)
        kwargs = default_policy_b_kwargs(unit, exit_code=2)
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertTrue(decision.is_allowed())
        self.assertEqual(decision.reason_code, ALLOW_VERIFIER_FAILURE)
        self.assertEqual(decision.details["failed_verifiers"], ["v1"])

    # 2. Zero exit
    def test_policy_b_all_verifiers_exit_zero_denies_contradictory(self):
        unit = make_valid_policy_b_unit(exit_code=0)
        unit["attempts"][0]["verifier_result"]["v1"]["passed"] = True
        unit["attempts"][0]["verifier_result"]["v1"]["controller_evidence"]["observed_exit_code"] = 0
        kwargs = default_policy_b_kwargs(unit, exit_code=0)
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_CONTRADICTORY_EVIDENCE)

    def test_policy_b_exit_zero_with_passed_false_denies_contradictory(self):
        unit = make_valid_policy_b_unit(exit_code=0)
        unit["attempts"][0]["verifier_result"]["v1"]["passed"] = False
        unit["attempts"][0]["verifier_result"]["v1"]["controller_evidence"]["observed_exit_code"] = 0
        kwargs = default_policy_b_kwargs(unit, exit_code=0)
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_CONTRADICTORY_EVIDENCE)

    # 3. Missing controller exit evidence
    def test_policy_b_missing_child_id_denies_missing_evidence(self):
        unit = make_valid_policy_b_unit()
        del unit["attempts"][0]["verifier_result"]["v1"]["controller_evidence"]["child_id"]
        if "child_id" in unit["attempts"][0]["verifier_result"]["v1"]:
            del unit["attempts"][0]["verifier_result"]["v1"]["child_id"]
        kwargs = default_policy_b_kwargs(unit)
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)

    def test_policy_b_missing_exit_code_denies_missing_evidence(self):
        unit = make_valid_policy_b_unit()
        unit["attempts"][0]["verifier_result"]["v1"]["exit_code"] = None
        kwargs = default_policy_b_kwargs(unit)
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)

    def test_policy_b_supervisor_missing_exit_code_denies(self):
        unit = make_valid_policy_b_unit()
        kwargs = default_policy_b_kwargs(unit)
        kwargs["supervisor"]._children[0]["exit_code"] = None
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)

    def test_policy_b_missing_termination_denies(self):
        unit = make_valid_policy_b_unit()
        del unit["attempts"][0]["verifier_result"]["v1"]["controller_evidence"]["termination"]
        kwargs = default_policy_b_kwargs(unit)
        del kwargs["supervisor"]._children[0]["termination"]
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)

    def test_policy_b_missing_descendant_cleanup_denies(self):
        unit = make_valid_policy_b_unit()
        del unit["attempts"][0]["verifier_result"]["v1"]["controller_evidence"]["cleanup"]
        kwargs = default_policy_b_kwargs(unit)
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)

    def test_policy_b_current_scheduler_output_without_provenance_fails_closed(self):
        # Current scheduler output lacks controller_evidence; under Policy B it must fail closed.
        unit = make_valid_unit()  # No controller_evidence
        kwargs = default_policy_b_kwargs(unit)
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)

    # 4. Timeout / killed process
    def test_policy_b_termination_forced_denies_timeout(self):
        unit = make_valid_policy_b_unit()
        kwargs = default_policy_b_kwargs(unit, termination="forced")
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_TIMEOUT)

    def test_policy_b_v_entry_termination_forced_denies_timeout(self):
        unit = make_valid_policy_b_unit(termination="forced")
        kwargs = default_policy_b_kwargs(unit)
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_TIMEOUT)

    def test_policy_b_attempt_failure_classification_timeout_denies(self):
        for t_class in ("executor_timeout", "unit_timeout", "verifier_timeout", "timeout"):
            unit = make_valid_policy_b_unit(failure_classification=t_class)
            kwargs = default_policy_b_kwargs(unit)
            decision = classify_retry_eligibility(unit, **kwargs)
            self.assertFalse(decision.is_allowed())
            self.assertEqual(decision.reason_code, DENY_TIMEOUT)

    def test_policy_b_signal_killed_exit_code_denies(self):
        for bad_code in (-9, -15, 137, 143):
            unit = make_valid_policy_b_unit(exit_code=bad_code)
            kwargs = default_policy_b_kwargs(unit, exit_code=bad_code)
            decision = classify_retry_eligibility(unit, **kwargs)
            self.assertFalse(decision.is_allowed())
            self.assertEqual(decision.reason_code, DENY_UNKNOWN_FAILURE)

    def test_policy_b_abnormal_termination_launch_failed_denies(self):
        unit = make_valid_policy_b_unit(termination="launch_failed")
        kwargs = default_policy_b_kwargs(unit, termination="launch_failed")
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_UNKNOWN_FAILURE)

    # 5. Incomplete descendant cleanup
    def test_policy_b_incomplete_descendant_cleanup_denies(self):
        for bad_cleanup in ("incomplete", "failed", "none", "not_observed"):
            unit = make_valid_policy_b_unit(cleanup=bad_cleanup)
            kwargs = default_policy_b_kwargs(unit)
            decision = classify_retry_eligibility(unit, **kwargs)
            self.assertFalse(decision.is_allowed())
            self.assertEqual(decision.reason_code, DENY_PROCESS_NOT_IDLE)

    def test_policy_b_supervisor_child_state_not_exited_denies(self):
        for bad_state in ("RUNNING", "STARTING", "LOST", "STALE", "UNKNOWN"):
            unit = make_valid_policy_b_unit()
            kwargs = default_policy_b_kwargs(unit)
            kwargs["supervisor"]._children[0]["state"] = bad_state
            decision = classify_retry_eligibility(unit, **kwargs)
            self.assertFalse(decision.is_allowed())
            self.assertEqual(decision.reason_code, DENY_PROCESS_NOT_IDLE)

    # 6. Wrong registered invocation
    def test_policy_b_wrong_argv_denies_contradictory(self):
        unit = make_valid_policy_b_unit()
        unit["attempts"][0]["verifier_result"]["v1"]["argv"] = ["python", "-m", "unittest", "wrong_test.py"]
        unit["attempts"][0]["verifier_result"]["v1"]["controller_evidence"]["intended_argv"] = ["python", "-m", "unittest", "wrong_test.py"]
        kwargs = default_policy_b_kwargs(unit)
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_CONTRADICTORY_EVIDENCE)

    def test_policy_b_unregistered_verifier_id_denies_missing(self):
        unit = make_valid_policy_b_unit(verifier_ids=["v_unregistered"])
        kwargs = default_policy_b_kwargs(unit, verifier_ids=["v_unregistered"])
        del kwargs["verifier_registry"]["v_unregistered"]
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)

    # 7. Missing or conflicting provenance
    def test_policy_b_missing_provenance_child_not_in_supervisor_denies(self):
        unit = make_valid_policy_b_unit()
        ghost_id = "verification_command-ghost000000"
        unit["attempts"][0]["verifier_result"]["v1"]["controller_evidence"]["child_id"] = ghost_id
        unit["attempts"][0]["verifier_result"]["v1"]["child_id"] = ghost_id
        kwargs = default_policy_b_kwargs(unit)
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)

    def test_policy_b_conflicting_exit_code_provenance_denies(self):
        unit = make_valid_policy_b_unit(exit_code=1)
        kwargs = default_policy_b_kwargs(unit, exit_code=0)
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_CONTRADICTORY_EVIDENCE)

    def test_policy_b_conflicting_command_sha256_provenance_denies(self):
        unit = make_valid_policy_b_unit()
        kwargs = default_policy_b_kwargs(unit)
        kwargs["supervisor"]._children[0]["command_sha256"] = "0" * 64
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_CONTRADICTORY_EVIDENCE)

    # 8. Repository or environment drift
    def test_policy_b_git_head_drift_denies(self):
        unit = make_valid_policy_b_unit()
        kwargs = default_policy_b_kwargs(unit)
        kwargs["candidate_snapshot"] = make_valid_candidate_snapshot(head="c" * 40)
        kwargs["verification_snapshot_before"] = deepcopy(kwargs["candidate_snapshot"])
        kwargs["verification_snapshot_after"] = deepcopy(kwargs["candidate_snapshot"])
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_UNVERIFIED_MUTATIONS)

    def test_policy_b_untracked_repo_mutation_denies(self):
        unit = make_valid_policy_b_unit(changed_paths=["calculator.py"])
        kwargs = default_policy_b_kwargs(unit, changed_paths=["calculator.py"])
        kwargs["candidate_snapshot"] = make_valid_candidate_snapshot(changed_paths=["calculator.py", "untracked.py"])
        kwargs["verification_snapshot_before"] = deepcopy(kwargs["candidate_snapshot"])
        kwargs["verification_snapshot_after"] = deepcopy(kwargs["candidate_snapshot"])
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_UNVERIFIED_MUTATIONS)

    def test_policy_b_verifier_tampered_repo_denies(self):
        unit = make_valid_policy_b_unit()
        kwargs = default_policy_b_kwargs(unit)
        kwargs["repo_mutated_during_verification"] = True
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_UNVERIFIED_MUTATIONS)

    def test_policy_b_environment_unsafe_denies(self):
        unit = make_valid_policy_b_unit()
        kwargs = default_policy_b_kwargs(unit)
        kwargs["environment_status"] = "UNSAFE"
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_ENVIRONMENT_DRIFT)

    # 9. Exhausted attempt or budgets
    def test_policy_b_already_two_attempts_denies(self):
        unit = make_valid_policy_b_unit()
        second_attempt = dict(unit["attempts"][0])
        second_attempt["attempt_id"] = "unit-1-attempt-2"
        unit["attempts"].append(second_attempt)
        kwargs = default_policy_b_kwargs(unit)
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_ATTEMPTS_EXHAUSTED)

    def test_policy_b_max_attempts_one_denies(self):
        unit = make_valid_policy_b_unit(max_attempts=1)
        kwargs = default_policy_b_kwargs(unit)
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_ATTEMPTS_EXHAUSTED)

    def test_policy_b_exhausted_unit_budget_denies(self):
        unit = make_valid_policy_b_unit()
        kwargs = default_policy_b_kwargs(unit)
        kwargs["remaining_unit_budget"] = 0.0
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_INSUFFICIENT_BUDGET)

    def test_policy_b_exhausted_task_budget_denies(self):
        unit = make_valid_policy_b_unit()
        kwargs = default_policy_b_kwargs(unit)
        kwargs["remaining_task_budget"] = 0.0
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_INSUFFICIENT_BUDGET)

    # 10. Phase 3.1C-2 specific contract alignment tests
    def test_policy_b_distinguish_worker_receipt_rejects(self):
        # Requirement 2: Distinguish observed verifier exit from broker or worker receipts.
        unit = make_valid_policy_b_unit()
        unit["attempts"][0]["verifier_result"]["v1"]["descendant_cleanup"] = "job_close"
        kwargs = default_policy_b_kwargs(unit)
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_CONTRADICTORY_EVIDENCE)

        unit2 = make_valid_policy_b_unit()
        unit2["attempts"][0]["verifier_result"]["v1"]["controller_evidence"]["cleanup"] = "job_close"
        kwargs2 = default_policy_b_kwargs(unit2)
        decision2 = classify_retry_eligibility(unit2, **kwargs2)
        self.assertFalse(decision2.is_allowed())
        self.assertEqual(decision2.reason_code, DENY_CONTRADICTORY_EVIDENCE)

    def test_policy_b_broker_present_rejects(self):
        # Requirement 2: Reject broker receipts.
        unit = make_valid_policy_b_unit()
        unit["attempts"][0]["verifier_result"]["v1"]["controller_evidence"]["broker"] = {"pid": 9999}
        kwargs = default_policy_b_kwargs(unit)
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_CONTRADICTORY_EVIDENCE)

    def test_policy_b_source_not_controller_native_handles_rejects(self):
        # Requirement 1 & 2: Schema requires source='controller_native_handles'.
        unit = make_valid_policy_b_unit()
        unit["attempts"][0]["verifier_result"]["v1"]["controller_evidence"]["source"] = "worker"
        kwargs = default_policy_b_kwargs(unit)
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)

    def test_policy_b_independent_intended_argv_digest_mismatch_rejects(self):
        # Requirement 3: Validate intended argv digest independently.
        unit = make_valid_policy_b_unit()
        unit["attempts"][0]["verifier_result"]["v1"]["controller_evidence"]["intended_command_sha256"] = "0" * 64
        kwargs = default_policy_b_kwargs(unit)
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_CONTRADICTORY_EVIDENCE)

    def test_policy_b_independent_resolved_argv_digest_mismatch_rejects(self):
        # Requirement 3: Validate resolved argv digest independently.
        unit = make_valid_policy_b_unit()
        unit["attempts"][0]["verifier_result"]["v1"]["controller_evidence"]["resolved_command_sha256"] = "0" * 64
        kwargs = default_policy_b_kwargs(unit)
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_CONTRADICTORY_EVIDENCE)

    def test_policy_b_resolved_arguments_disagree_with_intended_rejects(self):
        # Requirement 3: Arguments after executable must agree between resolved and intended.
        unit = make_valid_policy_b_unit()
        tampered_resolved = ["C:/Python311/python.exe", "-m", "unittest", "tampered.py"]
        unit["attempts"][0]["verifier_result"]["v1"]["controller_evidence"]["resolved_argv"] = tampered_resolved
        unit["attempts"][0]["verifier_result"]["v1"]["controller_evidence"]["resolved_command_sha256"] = hashlib.sha256(
            json.dumps(tampered_resolved, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        kwargs = default_policy_b_kwargs(unit)
        kwargs["supervisor"]._children[0]["command_sha256"] = unit["attempts"][0]["verifier_result"]["v1"]["controller_evidence"]["resolved_command_sha256"]
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_CONTRADICTORY_EVIDENCE)

    def test_policy_b_verifier_identity_mismatch_rejects(self):
        # Requirement 5: Reject missing or conflicting child identity data.
        unit = make_valid_policy_b_unit()
        unit["attempts"][0]["verifier_result"]["v1"]["controller_evidence"]["verifier"]["pid"] = 99999
        kwargs = default_policy_b_kwargs(unit)
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_CONTRADICTORY_EVIDENCE)

    def test_policy_b_job_name_mismatch_rejects(self):
        # Requirement 4 & 5: Controller-confirmed Job containment and cleanup.
        unit = make_valid_policy_b_unit()
        unit["attempts"][0]["verifier_result"]["v1"]["controller_evidence"]["job"] = "Local\\harness-other"
        kwargs = default_policy_b_kwargs(unit)
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_CONTRADICTORY_EVIDENCE)

    def test_policy_b_run_id_binding_mismatch_rejects(self):
        # Requirement 6: Enforce relevant run bindings when authoritative data exists.
        unit = make_valid_policy_b_unit(run_id="run-1")
        kwargs = default_policy_b_kwargs(unit, run_id="run-2")
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_CONTRADICTORY_EVIDENCE)

    def test_policy_b_unit_id_binding_mismatch_rejects(self):
        # Requirement 6: Enforce unit_id binding.
        unit = make_valid_policy_b_unit(unit_id="unit-1")
        unit["attempts"][0]["verifier_result"]["v1"]["controller_evidence"]["unit_id"] = "unit-2"
        kwargs = default_policy_b_kwargs(unit)
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_CONTRADICTORY_EVIDENCE)

    def test_policy_b_attempt_id_binding_mismatch_rejects(self):
        # Requirement 6: Enforce attempt_id binding.
        unit = make_valid_policy_b_unit()
        unit["attempts"][0]["verifier_result"]["v1"]["controller_evidence"]["attempt_id"] = "wrong-attempt-id"
        kwargs = default_policy_b_kwargs(unit)
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_CONTRADICTORY_EVIDENCE)

    def test_policy_b_explicit_controller_opt_in_required(self):
        # Requirement 6 & 7: Policy B requires explicit trusted controller opt-in via parameter;
        # mutable spec cannot select it, and generic spec metadata must not alter legacy M0 decision.
        unit = make_valid_policy_b_unit()
        unit["spec"]["retry_policy"] = "B"
        kwargs = default_policy_b_kwargs(unit)
        del kwargs["policy"]  # Controller did not opt in
        decision = classify_retry_eligibility(unit, **kwargs)
        # Without explicit policy=POLICY_B argument, unit evaluates under default M0 (Policy A)
        # where standard verifier failure allows retry, and Policy B is NOT activated.
        self.assertTrue(decision.is_allowed())
        self.assertNotIn("policy", decision.details or {})

    def test_policy_b_context_policy_field_ignored(self):
        # Requirement 9: Remove implicit selection through mutable context fields.
        unit = make_valid_unit()
        kwargs = default_trusted_kwargs(unit)
        kwargs["context"] = {"policy": "B"}
        decision = classify_retry_eligibility(unit, **kwargs)
        # Without explicit policy=POLICY_B argument, context["policy"] does NOT opt in to Policy B;
        # unit evaluates under default M0 (Policy A) where standard verifier failure allows retry.
        self.assertTrue(decision.is_allowed())
        self.assertNotIn("policy", decision.details or {})

    def test_policy_b_execution_not_started_denies(self):
        # Verifier execution_started must be True.
        unit = make_valid_policy_b_unit()
        unit["attempts"][0]["verifier_result"]["v1"]["controller_evidence"]["execution_started"] = False
        kwargs = default_policy_b_kwargs(unit)
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_UNKNOWN_FAILURE)


class TestPhase31C2AFirewallRegressions(unittest.TestCase):
    """Specific regression tests for confirmed Codex false-ALLOW paths."""

    def test_policy_b_duplicate_child_id_conflicting_exit_denies(self):
        # 1. Reject duplicate child IDs in supervisor records; never overwrite
        unit = make_valid_policy_b_unit(exit_code=1)
        kwargs = default_policy_b_kwargs(unit, exit_code=1)
        child1 = dict(kwargs["supervisor"]._children[0])
        child2 = dict(kwargs["supervisor"]._children[0])
        child2["exit_code"] = 0
        kwargs["supervisor"]._children = [child1, child2]
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_CONTRADICTORY_EVIDENCE)

    def test_policy_b_forced_termination_contradiction_denies(self):
        # 2. Reject contradictions across termination fields (flat forced denies even if nested natural)
        unit = make_valid_policy_b_unit(exit_code=1)
        v_entry = unit["attempts"][0]["verifier_result"]["v1"]
        v_entry["termination"] = "forced"
        v_entry["controller_evidence"]["termination"] = "natural"
        kwargs = default_policy_b_kwargs(unit, exit_code=1, termination="natural")
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_TIMEOUT)

    def test_policy_b_conflicting_attempt_context_evidence_denies(self):
        # 3. Reject conflicting evidence sources; no silent precedence
        unit = make_valid_policy_b_unit(exit_code=1)
        kwargs = default_policy_b_kwargs(unit, exit_code=1)
        conflicting_ev = dict(unit["attempts"][0]["verifier_result"]["v1"]["controller_evidence"])
        conflicting_ev["observed_exit_code"] = 99
        kwargs["context"] = {"controller_evidence": {"v1": conflicting_ev}}
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_CONTRADICTORY_EVIDENCE)

    def test_policy_b_missing_broker_denies(self):
        # 4. Reject missing broker (broker must be explicitly None)
        unit = make_valid_policy_b_unit(exit_code=1)
        del unit["attempts"][0]["verifier_result"]["v1"]["controller_evidence"]["broker"]
        kwargs = default_policy_b_kwargs(unit, exit_code=1)
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)

    def test_policy_b_boolean_schema_version_true_denies(self):
        # 4. schema_version must be integer 1, NOT boolean True
        unit = make_valid_policy_b_unit(exit_code=1)
        unit["attempts"][0]["verifier_result"]["v1"]["controller_evidence"]["schema_version"] = True
        kwargs = default_policy_b_kwargs(unit, exit_code=1)
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)

    def test_policy_b_missing_run_unit_attempt_ids_denies(self):
        # 5. Prevent missing run/unit/attempt identifiers from acting as wildcards
        for field in ("run_id", "unit_id", "attempt_id"):
            with self.subTest(missing_field=field):
                unit = make_valid_policy_b_unit(exit_code=1)
                del unit["attempts"][0]["verifier_result"]["v1"]["controller_evidence"][field]
                kwargs = default_policy_b_kwargs(unit, exit_code=1)
                decision = classify_retry_eligibility(unit, **kwargs)
                self.assertFalse(decision.is_allowed())
                self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)

    def test_policy_b_stale_evidence_reused_for_another_unit_denies(self):
        # 5. Reject stale evidence reused across units
        unit = make_valid_policy_b_unit(unit_id="unit-2", exit_code=1)
        unit["attempts"][0]["verifier_result"]["v1"]["controller_evidence"]["unit_id"] = "unit-1"
        kwargs = default_policy_b_kwargs(unit, exit_code=1)
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed())
        self.assertEqual(decision.reason_code, DENY_CONTRADICTORY_EVIDENCE)

    def test_generic_spec_policy_under_default_a_allows(self):
        # 7. Generic spec.policy metadata must not alter legacy M0 decision
        unit = make_valid_unit()
        unit["spec"]["policy"] = "bounded_recovery"
        unit["spec"]["retry_policy"] = "B"
        kwargs = default_trusted_kwargs(unit)
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertTrue(decision.is_allowed())
        self.assertEqual(decision.decision, DECISION_ALLOW)
        self.assertEqual(decision.reason_code, ALLOW_VERIFIER_FAILURE)
        self.assertNotIn("policy", decision.details or {})


class TestCanonicalPolicyBEvidence(unittest.TestCase):
    """Synthetic predicate inputs, not proof of native or durable provenance."""

    SOURCES = ("result", "attempt", "context")

    def evidence_at(self, source, unit, kwargs):
        entry = unit["attempts"][0]["verifier_result"]["v1"]
        if source == "result":
            return entry["controller_evidence"]
        evidence = deepcopy(entry["controller_evidence"])
        owner = unit["attempts"][0] if source == "attempt" else kwargs.setdefault("context", {})
        owner["controller_evidence"] = {"v1": evidence}
        return evidence

    def assertDenied(self, unit, kwargs, reason=None):
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertEqual(decision.decision, DECISION_DENY, decision)
        self.assertFalse(decision.eligible, decision)
        # Schema errors must be handled by the boundary, not its exception catch.
        self.assertNotEqual(decision.reason_code, DENY_DEFAULT, decision)
        if reason is not None:
            self.assertEqual(decision.reason_code, reason, decision)

    def test_secondary_true_is_not_integer_one(self):
        for source in ("attempt", "context"):
            with self.subTest(source=source):
                unit = make_valid_policy_b_unit()
                kwargs = default_policy_b_kwargs(unit)
                self.evidence_at(source, unit, kwargs)["schema_version"] = True
                self.assertDenied(unit, kwargs, DENY_MISSING_EVIDENCE)

    def test_malformed_secondary_containers_matrix(self):
        for source in ("attempt", "context"):
            for malformed in (None, True, 1, "malformed", [], {}, [{"v1": {}}]):
                with self.subTest(source=source, malformed=malformed):
                    unit = make_valid_policy_b_unit()
                    kwargs = default_policy_b_kwargs(unit)
                    owner = unit["attempts"][0] if source == "attempt" else kwargs.setdefault("context", {})
                    owner["controller_evidence"] = malformed
                    self.assertDenied(unit, kwargs, DENY_MISSING_EVIDENCE)

    def test_flat_incomplete_cleanup_denies(self):
        unit = make_valid_policy_b_unit()
        kwargs = default_policy_b_kwargs(unit)
        unit["attempts"][0]["verifier_result"]["v1"]["cleanup"] = "incomplete"
        self.assertDenied(unit, kwargs, DENY_PROCESS_NOT_IDLE)

    def test_distinct_children_reusing_native_process_deny(self):
        unit = make_valid_policy_b_unit(verifier_ids=["v1", "v2"])
        kwargs = default_policy_b_kwargs(unit)
        results = unit["attempts"][0]["verifier_result"]
        proc = deepcopy(results["v1"]["controller_evidence"]["verifier"])
        results["v2"]["controller_evidence"]["verifier"] = proc
        kwargs["supervisor"]._children[1].update(pid=proc["pid"], identity=deepcopy(proc["identity"]))
        self.assertDenied(unit, kwargs, DENY_CONTRADICTORY_EVIDENCE)

    def test_schema_version_types_across_all_sources(self):
        for source in self.SOURCES:
            for malformed in (True, False, None, 1.0, "1", 0, 2, [], {}):
                with self.subTest(source=source, malformed=malformed):
                    unit = make_valid_policy_b_unit()
                    kwargs = default_policy_b_kwargs(unit)
                    self.evidence_at(source, unit, kwargs)["schema_version"] = malformed
                    self.assertDenied(unit, kwargs, DENY_MISSING_EVIDENCE)

    def test_malformed_records_across_all_sources(self):
        for source in self.SOURCES:
            for malformed in (None, True, 1, "malformed", [], [{}], {}):
                with self.subTest(source=source, malformed=malformed):
                    unit = make_valid_policy_b_unit()
                    kwargs = default_policy_b_kwargs(unit)
                    self.evidence_at(source, unit, kwargs)
                    if source == "result":
                        unit["attempts"][0]["verifier_result"]["v1"]["controller_evidence"] = malformed
                    else:
                        owner = unit["attempts"][0] if source == "attempt" else kwargs["context"]
                        owner["controller_evidence"]["v1"] = malformed
                    self.assertDenied(unit, kwargs, DENY_MISSING_EVIDENCE)

    def test_every_required_field_required_in_every_source(self):
        for source in self.SOURCES:
            for field in make_valid_controller_evidence():
                with self.subTest(source=source, missing=field):
                    unit = make_valid_policy_b_unit()
                    kwargs = default_policy_b_kwargs(unit)
                    del self.evidence_at(source, unit, kwargs)[field]
                    self.assertDenied(unit, kwargs, DENY_MISSING_EVIDENCE)

    def test_nested_identity_schema_matrix(self):
        changes = (
            ((), None), ((), []), ((), {}),
            (("pid",), True), (("pid",), 1.0), (("pid",), "1001"), (("pid",), 0),
            (("identity",), None), (("identity",), []), (("identity",), {}),
            (("identity", "created"), None), (("identity", "created"), 1),
            (("identity", "created"), ""), (("identity", "executable"), True),
            (("identity", "executable"), ""),
        )
        for source in self.SOURCES:
            for path, value in changes:
                with self.subTest(source=source, path=path, value=value):
                    unit = make_valid_policy_b_unit()
                    kwargs = default_policy_b_kwargs(unit)
                    evidence = self.evidence_at(source, unit, kwargs)
                    owner, key = evidence, "verifier"
                    for component in path:
                        owner, key = owner[key], component
                    owner[key] = deepcopy(value)
                    self.assertDenied(unit, kwargs, DENY_MISSING_EVIDENCE)

    def test_missing_nested_fields_across_all_sources(self):
        for source in self.SOURCES:
            for field in ("pid", "identity", "created", "executable"):
                with self.subTest(source=source, field=field):
                    unit = make_valid_policy_b_unit()
                    kwargs = default_policy_b_kwargs(unit)
                    proc = self.evidence_at(source, unit, kwargs)["verifier"]
                    del (proc if field in ("pid", "identity") else proc["identity"])[field]
                    self.assertDenied(unit, kwargs, DENY_MISSING_EVIDENCE)

    def test_secondary_pid_boolean_not_equal_to_one(self):
        unit = make_valid_policy_b_unit()
        kwargs = default_policy_b_kwargs(unit)
        entry = unit["attempts"][0]["verifier_result"]["v1"]
        entry["controller_evidence"]["verifier"]["pid"] = 1
        kwargs["supervisor"]._children[0]["pid"] = 1
        self.assertTrue(classify_retry_eligibility(unit, **kwargs).is_allowed())
        self.evidence_at("context", unit, kwargs)["verifier"]["pid"] = True
        self.assertDenied(unit, kwargs, DENY_MISSING_EVIDENCE)

    def test_flat_lifecycle_matrix_across_relevant_owners(self):
        changes = (
            ("cleanup", "incomplete"), ("cleanup", None), ("cleanup", True),
            ("cleanup_error", None), ("cleanup_error", "OSError"),
            ("descendant_cleanup", "job_close"), ("descendant_cleanup", None),
            ("termination", "forced"), ("termination", "timeout"),
            ("termination", None), ("termination", True),
            ("termination_basis", "controller_cancel"), ("termination_basis", None),
        )
        for owner_name in ("result", "attempt", "context", "supervisor"):
            for field, value in changes:
                with self.subTest(owner=owner_name, field=field, value=value):
                    unit = make_valid_policy_b_unit()
                    kwargs = default_policy_b_kwargs(unit)
                    owners = {
                        "result": unit["attempts"][0]["verifier_result"]["v1"],
                        "attempt": unit["attempts"][0], "context": kwargs.setdefault("context", {}),
                        "supervisor": kwargs["supervisor"]._children[0],
                    }
                    owners[owner_name][field] = value
                    self.assertDenied(unit, kwargs)

    def test_nested_lifecycle_and_scalar_type_matrix(self):
        changes = (
            ("cleanup", "incomplete"), ("cleanup", "job_close"), ("cleanup", True),
            ("termination", "forced"), ("termination", "timeout"), ("termination", None),
            ("termination_basis", "controller_deadline"), ("termination_basis", False),
            ("observed_exit_code", True), ("observed_exit_code", 1.0),
            ("execution_started", 1), ("execution_started", "True"),
            ("job", True), ("run_id", 1), ("unit_id", None), ("attempt_id", []),
            ("intended_argv", ["python", True]), ("resolved_argv", "python"),
            ("intended_command_sha256", []), ("resolved_command_sha256", "bad"),
        )
        for source in self.SOURCES:
            for field, value in changes:
                with self.subTest(source=source, field=field, value=value):
                    unit = make_valid_policy_b_unit()
                    kwargs = default_policy_b_kwargs(unit)
                    self.evidence_at(source, unit, kwargs)[field] = deepcopy(value)
                    self.assertDenied(unit, kwargs)

    def test_conflicting_secondary_values_deny(self):
        for source in ("attempt", "context"):
            for field, value in (("observed_exit_code", 0), ("run_id", "old-run"),
                                 ("unit_id", "old-unit"), ("attempt_id", "old-attempt")):
                with self.subTest(source=source, field=field):
                    unit = make_valid_policy_b_unit()
                    kwargs = default_policy_b_kwargs(unit)
                    self.evidence_at(source, unit, kwargs)[field] = value
                    self.assertDenied(unit, kwargs, DENY_CONTRADICTORY_EVIDENCE)

    def test_unknown_evidence_fields_and_foreign_records_deny(self):
        for source in self.SOURCES:
            unit = make_valid_policy_b_unit()
            kwargs = default_policy_b_kwargs(unit)
            self.evidence_at(source, unit, kwargs)["extra"] = {"schema_version": True}
            with self.subTest(source=source):
                self.assertDenied(unit, kwargs, DENY_MISSING_EVIDENCE)
        for source in ("attempt", "context"):
            unit = make_valid_policy_b_unit()
            kwargs = default_policy_b_kwargs(unit)
            self.evidence_at(source, unit, kwargs)
            owner = unit["attempts"][0] if source == "attempt" else kwargs["context"]
            owner["controller_evidence"]["foreign-verifier"] = {}
            with self.subTest(foreign_source=source):
                self.assertDenied(unit, kwargs, DENY_MISSING_EVIDENCE)

    def test_duplicate_identical_supervisor_records_deny(self):
        unit = make_valid_policy_b_unit()
        kwargs = default_policy_b_kwargs(unit)
        kwargs["supervisor"]._children.append(deepcopy(kwargs["supervisor"]._children[0]))
        self.assertDenied(unit, kwargs, DENY_CONTRADICTORY_EVIDENCE)

    def test_unreferenced_supervisor_child_cannot_alias_process(self):
        unit = make_valid_policy_b_unit()
        kwargs = default_policy_b_kwargs(unit)
        duplicate = deepcopy(kwargs["supervisor"]._children[0])
        duplicate["child_id"] = "verification_command-unused"
        # Changing executable spelling does not change the PID/creation pair.
        duplicate["identity"]["executable"] = "C:/Elsewhere/python.exe"
        kwargs["supervisor"]._children.append(duplicate)
        self.assertDenied(unit, kwargs, DENY_CONTRADICTORY_EVIDENCE)

    def test_verifier_process_reuse_denies_without_supervisor_alias(self):
        unit = make_valid_policy_b_unit(verifier_ids=["v1", "v2"])
        kwargs = default_policy_b_kwargs(unit)
        results = unit["attempts"][0]["verifier_result"]
        results["v2"]["controller_evidence"]["verifier"] = deepcopy(results["v1"]["controller_evidence"]["verifier"])
        self.assertDenied(unit, kwargs, DENY_CONTRADICTORY_EVIDENCE)

    def test_duplicate_verifier_ids_deny(self):
        unit = make_valid_policy_b_unit()
        kwargs = default_policy_b_kwargs(unit)
        unit["spec"]["verifier_ids"].append("v1")
        self.assertDenied(unit, kwargs, DENY_CONTRADICTORY_EVIDENCE)

    def test_duplicate_result_under_distinct_verifier_id_denies(self):
        unit = make_valid_policy_b_unit(verifier_ids=["v1", "v2"])
        kwargs = default_policy_b_kwargs(unit)
        results = unit["attempts"][0]["verifier_result"]
        results["v2"] = deepcopy(results["v1"])
        self.assertDenied(unit, kwargs, DENY_CONTRADICTORY_EVIDENCE)

    def test_unselected_result_record_denies(self):
        unit = make_valid_policy_b_unit()
        kwargs = default_policy_b_kwargs(unit)
        unit["attempts"][0]["verifier_result"]["extra"] = {}
        self.assertDenied(unit, kwargs, DENY_MISSING_EVIDENCE)

    def test_supervisor_native_and_exit_type_matrix(self):
        for field, value in (("pid", True), ("pid", 1001.0), ("identity", None),
                             ("exit_code", True), ("exit_code", 1.0), ("job", [])):
            with self.subTest(field=field, value=value):
                unit = make_valid_policy_b_unit()
                kwargs = default_policy_b_kwargs(unit)
                kwargs["supervisor"]._children[0][field] = value
                self.assertDenied(unit, kwargs, DENY_MISSING_EVIDENCE)

    def test_valid_single_with_identical_all_source_copies(self):
        unit = make_valid_policy_b_unit()
        kwargs = default_policy_b_kwargs(unit)
        for source in self.SOURCES:
            self.evidence_at(source, unit, kwargs)
        unit["attempts"][0]["cleanup"] = "observed_empty"
        kwargs["context"]["termination_basis"] = "signaled_without_controller_termination"
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertTrue(decision.is_allowed(), decision)
        self.assertEqual(decision.details["policy"], "B")

    def test_valid_single_with_one_source_at_a_time(self):
        for source in self.SOURCES:
            with self.subTest(source=source):
                unit = make_valid_policy_b_unit()
                kwargs = default_policy_b_kwargs(unit)
                self.evidence_at(source, unit, kwargs)
                if source != "result":
                    del unit["attempts"][0]["verifier_result"]["v1"]["controller_evidence"]
                decision = classify_retry_eligibility(unit, **kwargs)
                self.assertTrue(decision.is_allowed(), decision)

    def test_valid_multi_with_failed_and_successful_verifiers(self):
        unit = make_valid_policy_b_unit(verifier_ids=["v1", "v2", "v3"])
        kwargs = default_policy_b_kwargs(unit)
        results = unit["attempts"][0]["verifier_result"]
        for index, vid in enumerate(("v1", "v2", "v3")):
            exit_code = 1 if vid == "v2" else 0
            results[vid].update(exit_code=exit_code, passed=(exit_code == 0))
            results[vid]["controller_evidence"]["observed_exit_code"] = exit_code
            kwargs["supervisor"]._children[index]["exit_code"] = exit_code
        unit["attempts"][0]["controller_evidence"] = {
            vid: deepcopy(entry["controller_evidence"]) for vid, entry in results.items()
        }
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertTrue(decision.is_allowed(), decision)
        self.assertEqual(decision.details["failed_verifiers"], ["v2"])

    def test_same_pid_different_creation_identity_is_not_alias(self):
        unit = make_valid_policy_b_unit(verifier_ids=["v1", "v2"])
        kwargs = default_policy_b_kwargs(unit)
        evidence = unit["attempts"][0]["verifier_result"]["v2"]["controller_evidence"]
        evidence["verifier"]["pid"] = 1001
        evidence["verifier"]["identity"]["created"] = "2026-10-16T10:01:00Z"
        kwargs["supervisor"]._children[1].update(pid=1001, identity=deepcopy(evidence["verifier"]["identity"]))
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertTrue(decision.is_allowed(), decision)

    def test_unbound_native_producer_evidence_denies(self):
        unit = make_valid_policy_b_unit()
        kwargs = default_policy_b_kwargs(unit)
        evidence = unit["attempts"][0]["verifier_result"]["v1"]["controller_evidence"]
        for field in ("run_id", "unit_id", "attempt_id"):
            del evidence[field]
        self.assertDenied(unit, kwargs, DENY_MISSING_EVIDENCE)

    def test_matching_mutable_ids_do_not_prove_provenance(self):
        # This predicate cannot authenticate caller-owned history. Integration
        # must supply an invocation-bound controller/WAL envelope; none exists yet.
        unit = make_valid_policy_b_unit(unit_id="fabricated-unit", run_id="fabricated-run")
        kwargs = default_policy_b_kwargs(unit, run_id="fabricated-run")
        self.assertTrue(classify_retry_eligibility(unit, **kwargs).is_allowed())

    def test_default_and_explicit_a_compatibility(self):
        for policy in (None, "A"):
            unit = make_valid_unit()
            kwargs = default_trusted_kwargs(unit)
            unit["spec"].update(policy="B", retry_policy="B")
            kwargs.update(policy=policy, context={"policy": "B", "retry_policy": "B"})
            # These new strict Policy B gates must not silently activate in A.
            unit["attempts"][0]["controller_evidence"] = "legacy metadata"
            decision = classify_retry_eligibility(unit, **kwargs)
            self.assertTrue(decision.is_allowed(), decision)
            self.assertNotIn("policy", decision.details)
            kwargs["policy"] = "B"
            unit["run_id"] = "run-1"
            self.assertDenied(unit, kwargs, DENY_MISSING_EVIDENCE)


class TestPhase31C2DSnapshotConsistencyGate(unittest.TestCase):
    """Regressions for Phase 3.1C-2D snapshot consistency gate."""

    def test_policy_b_candidate_index_sha256_drift_denies(self):
        # Defect 1: Candidate index_sha256 changes from pre-attempt index_sha256
        unit = make_valid_policy_b_unit()
        kwargs = default_policy_b_kwargs(unit)
        kwargs["candidate_snapshot"] = make_valid_candidate_snapshot(index_sha256="c" * 64)
        kwargs["verification_snapshot_before"] = deepcopy(kwargs["candidate_snapshot"])
        kwargs["verification_snapshot_after"] = deepcopy(kwargs["candidate_snapshot"])
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed(), decision)
        self.assertEqual(decision.decision, DECISION_DENY)
        self.assertEqual(decision.reason_code, DENY_UNVERIFIED_MUTATIONS)

    def test_policy_b_candidate_index_sha256_missing_or_invalid_denies(self):
        # Candidate snapshot must have a valid non-empty string index_sha256
        for bad_val in (None, "", "   ", 123, True, []):
            with self.subTest(bad_val=bad_val):
                unit = make_valid_policy_b_unit()
                kwargs = default_policy_b_kwargs(unit)
                if bad_val is None:
                    del kwargs["candidate_snapshot"]["index_sha256"]
                else:
                    kwargs["candidate_snapshot"]["index_sha256"] = bad_val
                decision = classify_retry_eligibility(unit, **kwargs)
                self.assertFalse(decision.is_allowed(), decision)
                self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)

    def test_policy_b_explicit_empty_candidate_snapshot_with_attempt_fallback_denies(self):
        # Defect 2: Explicit candidate_snapshot={} must not be ignored via fallback to attempt snapshot
        unit = make_valid_policy_b_unit()
        unit["attempts"][0]["candidate_snapshot"] = make_valid_candidate_snapshot()
        kwargs = default_policy_b_kwargs(unit)
        kwargs["candidate_snapshot"] = {}
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed(), decision)
        self.assertEqual(decision.decision, DECISION_DENY)
        self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)

    def test_policy_b_explicit_malformed_candidate_snapshot_with_attempt_fallback_denies(self):
        # Defect 2: Explicit malformed candidate_snapshot must not fall back to attempt snapshot
        unit = make_valid_policy_b_unit()
        unit["attempts"][0]["candidate_snapshot"] = make_valid_candidate_snapshot()
        kwargs = default_policy_b_kwargs(unit)
        kwargs["candidate_snapshot"] = {"repository": "C:/Repo"}  # missing fields
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed(), decision)
        self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)

    def test_policy_b_context_empty_candidate_snapshot_denies(self):
        # Context snapshot being empty dict must not fall back to attempt snapshot
        unit = make_valid_policy_b_unit()
        unit["attempts"][0]["candidate_snapshot"] = make_valid_candidate_snapshot()
        kwargs = default_policy_b_kwargs(unit)
        del kwargs["candidate_snapshot"]  # absent as arg
        kwargs["context"] = {"candidate_snapshot": {}}
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed(), decision)
        self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)

    def test_policy_b_candidate_snapshot_argument_contradicts_attempt_candidate_snapshot_denies(self):
        # Defect 3: Candidate snapshot passed explicitly contradicts attempt candidate_snapshot
        unit = make_valid_policy_b_unit()
        unit["attempts"][0]["candidate_snapshot"] = make_valid_candidate_snapshot()
        unit["attempts"][0]["candidate_snapshot"]["files"]["calculator.py"]["sha256"] = "c" * 64
        unit["attempts"][0]["candidate_snapshot"]["fingerprint"] = durable.digest(
            {k: v for k, v in unit["attempts"][0]["candidate_snapshot"].items() if k != "fingerprint"})
        kwargs = default_policy_b_kwargs(unit)
        kwargs["candidate_snapshot"] = make_valid_candidate_snapshot()
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed(), decision)
        self.assertEqual(decision.decision, DECISION_DENY)
        self.assertEqual(decision.reason_code, DENY_CONTRADICTORY_EVIDENCE)

    def test_policy_b_candidate_snapshot_argument_contradicts_context_candidate_snapshot_denies(self):
        # Candidate snapshot argument contradicts context candidate_snapshot
        unit = make_valid_policy_b_unit()
        kwargs = default_policy_b_kwargs(unit)
        kwargs["candidate_snapshot"] = make_valid_candidate_snapshot()
        contradicting_context_snap = make_valid_candidate_snapshot()
        contradicting_context_snap["head"] = "c" * 40
        contradicting_context_snap["fingerprint"] = durable.digest(
            {k: v for k, v in contradicting_context_snap.items() if k != "fingerprint"})
        kwargs["context"] = {"candidate_snapshot": contradicting_context_snap}
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed(), decision)
        self.assertEqual(decision.reason_code, DENY_CONTRADICTORY_EVIDENCE)

    def test_policy_b_attempt_candidate_and_post_attempt_snapshots_contradict_denies(self):
        # Attempt candidate_snapshot contradicts post_attempt_snapshot
        unit = make_valid_policy_b_unit()
        unit["attempts"][0]["candidate_snapshot"] = make_valid_candidate_snapshot()
        contradicting_post_snap = make_valid_candidate_snapshot()
        contradicting_post_snap["git_identity"] = [999, 999]
        unit["attempts"][0]["post_attempt_snapshot"] = contradicting_post_snap
        kwargs = default_policy_b_kwargs(unit)
        del kwargs["candidate_snapshot"]
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed(), decision)
        self.assertEqual(decision.reason_code, DENY_CONTRADICTORY_EVIDENCE)

    def test_policy_b_identical_candidate_snapshots_across_argument_and_attempt_allows(self):
        # Multiple identical candidate snapshot sources are allowed
        unit = make_valid_policy_b_unit()
        unit["attempts"][0]["candidate_snapshot"] = make_valid_candidate_snapshot()
        kwargs = default_policy_b_kwargs(unit)
        kwargs["candidate_snapshot"] = make_valid_candidate_snapshot()
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertTrue(decision.is_allowed(), decision)
        self.assertEqual(decision.decision, DECISION_ALLOW)
        self.assertEqual(decision.reason_code, ALLOW_VERIFIER_FAILURE)

    def test_policy_b_legitimate_allowed_file_change_with_stable_index_allows(self):
        # Legitimate file mutations in allowed paths with stable index_sha256 must pass
        unit = make_valid_policy_b_unit(changed_paths=["calculator.py"])
        kwargs = default_policy_b_kwargs(unit, changed_paths=["calculator.py"])
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertTrue(decision.is_allowed(), decision)
        self.assertEqual(decision.decision, DECISION_ALLOW)
        self.assertEqual(decision.reason_code, ALLOW_VERIFIER_FAILURE)

    def test_policy_a_preserves_allowed_decision_with_valid_snapshot(self):
        # Default Policy A allows valid unit with valid candidate snapshot
        unit = make_valid_unit()
        kwargs = default_trusted_kwargs(unit)
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertTrue(decision.is_allowed(), decision)
        self.assertEqual(decision.decision, DECISION_ALLOW)
        self.assertNotIn("policy", decision.details or {})

    def test_policy_a_explicit_empty_candidate_snapshot_uses_legacy_fallback(self):
        # Manager-approved legacy behavior, also checked against the historical oracle.
        unit = make_valid_unit()
        unit["attempts"][0]["candidate_snapshot"] = make_valid_candidate_snapshot()
        kwargs = default_trusted_kwargs(unit)
        kwargs["candidate_snapshot"] = {}
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertTrue(decision.is_allowed(), decision)
        self.assertEqual(decision.reason_code, ALLOW_VERIFIER_FAILURE)

    def test_policy_a_fallback_to_attempt_when_argument_absent(self):
        # Default Policy A: when candidate_snapshot argument is None, falls back to attempt snapshot
        unit = make_valid_unit()
        unit["attempts"][0]["candidate_snapshot"] = make_valid_candidate_snapshot()
        kwargs = default_trusted_kwargs(unit)
        del kwargs["candidate_snapshot"]  # absent argument
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertTrue(decision.is_allowed(), decision)
        self.assertEqual(decision.decision, DECISION_ALLOW)


class TestPhase31C2GUnifiedSnapshotBoundary(unittest.TestCase):
    """Exhaustive tests for Phase 3.1C-2G unified snapshot boundary."""

    def test_policy_b_missing_all_candidate_sources_denies(self):
        unit = make_valid_policy_b_unit()
        kwargs = default_policy_b_kwargs(unit)
        del kwargs["candidate_snapshot"]
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed(), decision)
        self.assertEqual(decision.decision, DECISION_DENY)
        self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)

    def test_policy_b_explicit_null_candidate_argument_denies_without_fallback(self):
        unit = make_valid_policy_b_unit()
        unit["attempts"][0]["candidate_snapshot"] = make_valid_candidate_snapshot()
        kwargs = default_policy_b_kwargs(unit)
        kwargs["candidate_snapshot"] = None
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed(), decision)
        self.assertEqual(decision.decision, DECISION_DENY)
        self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)

    def test_policy_b_explicit_null_context_candidate_denies(self):
        unit = make_valid_policy_b_unit()
        unit["attempts"][0]["candidate_snapshot"] = make_valid_candidate_snapshot()
        kwargs = default_policy_b_kwargs(unit)
        del kwargs["candidate_snapshot"]
        kwargs["context"] = {"candidate_snapshot": None}
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed(), decision)
        self.assertEqual(decision.decision, DECISION_DENY)
        self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)

    def test_policy_b_missing_verification_before_denies(self):
        unit = make_valid_policy_b_unit()
        kwargs = default_policy_b_kwargs(unit)
        del kwargs["verification_snapshot_before"]
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed(), decision)
        self.assertEqual(decision.decision, DECISION_DENY)
        self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)

    def test_policy_b_explicit_null_verification_before_denies(self):
        unit = make_valid_policy_b_unit()
        kwargs = default_policy_b_kwargs(unit)
        kwargs["verification_snapshot_before"] = None
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed(), decision)
        self.assertEqual(decision.decision, DECISION_DENY)
        self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)

    def test_policy_b_missing_verification_after_denies(self):
        unit = make_valid_policy_b_unit()
        kwargs = default_policy_b_kwargs(unit)
        del kwargs["verification_snapshot_after"]
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed(), decision)
        self.assertEqual(decision.decision, DECISION_DENY)
        self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)

    def test_policy_b_explicit_null_verification_after_denies(self):
        unit = make_valid_policy_b_unit()
        kwargs = default_policy_b_kwargs(unit)
        kwargs["verification_snapshot_after"] = None
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed(), decision)
        self.assertEqual(decision.decision, DECISION_DENY)
        self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)

    def test_policy_b_conflicting_verification_before_sources_denies(self):
        unit = make_valid_policy_b_unit()
        kwargs = default_policy_b_kwargs(unit)
        kwargs["verification_snapshot_before"] = make_valid_candidate_snapshot()
        kwargs["context"] = {"verification_snapshot_before": make_valid_candidate_snapshot(head="c" * 40)}
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed(), decision)
        self.assertEqual(decision.decision, DECISION_DENY)
        self.assertEqual(decision.reason_code, DENY_CONTRADICTORY_EVIDENCE)

    def test_policy_b_conflicting_verification_after_sources_denies(self):
        unit = make_valid_policy_b_unit()
        kwargs = default_policy_b_kwargs(unit)
        kwargs["verification_snapshot_after"] = make_valid_candidate_snapshot()
        kwargs["context"] = {"verification_snapshot_after": make_valid_candidate_snapshot(head="c" * 40)}
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed(), decision)
        self.assertEqual(decision.decision, DECISION_DENY)
        self.assertEqual(decision.reason_code, DENY_CONTRADICTORY_EVIDENCE)

    def test_policy_b_canonical_schema_missing_fields_denies(self):
        for field in CANONICAL_SNAPSHOT_FIELDS:
            with self.subTest(field=field):
                unit = make_valid_policy_b_unit()
                kwargs = default_policy_b_kwargs(unit)
                del kwargs["candidate_snapshot"][field]
                decision = classify_retry_eligibility(unit, **kwargs)
                self.assertFalse(decision.is_allowed(), decision)
                self.assertEqual(decision.decision, DECISION_DENY)
                self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)

    def test_policy_b_canonical_schema_unexpected_field_denies(self):
        unit = make_valid_policy_b_unit()
        kwargs = default_policy_b_kwargs(unit)
        kwargs["candidate_snapshot"]["extra_field"] = "not_in_schema"
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed(), decision)
        self.assertEqual(decision.decision, DECISION_DENY)
        self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)

    def test_policy_b_canonical_schema_strict_field_types_denies(self):
        bad_types = [
            ("branch", True),
            ("branch", 123),
            ("status", True),
            ("status", 123),
            ("git_identity", [True, 200]),
            ("git_identity", [100, False]),
            ("git_identity", [100]),
            ("git_identity", "not-a-list"),
            ("files", "not-a-dict"),
            ("files", {}),
            ("fingerprint", 12345),
            ("fingerprint", True),
            ("fingerprint", "not-64-hex"),
        ]
        for field, bad_val in bad_types:
            with self.subTest(field=field, bad_val=bad_val):
                unit = make_valid_policy_b_unit()
                kwargs = default_policy_b_kwargs(unit)
                kwargs["candidate_snapshot"][field] = bad_val
                decision = classify_retry_eligibility(unit, **kwargs)
                self.assertFalse(decision.is_allowed(), decision)
                self.assertEqual(decision.decision, DECISION_DENY)
                self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)

    def test_policy_b_canonical_schema_strict_file_entry_types_denies(self):
        bad_entries = [
            ("bad_mode_bool", {"sha256": "abc", "mode": True}),
            ("bad_mode_negative", {"sha256": "abc", "mode": -1}),
            ("bad_mode_str", {"sha256": "abc", "mode": "644"}),
            ("bad_sha_int", {"sha256": 123, "mode": 33188}),
            ("bad_sha_empty", {"sha256": "", "mode": 33188}),
            ("missing_mode", {"sha256": "abc"}),
            ("missing_sha256", {"mode": 33188}),
            ("extra_field", {"sha256": "abc", "mode": 33188, "extra": 1}),
        ]
        for name, entry in bad_entries:
            with self.subTest(name=name):
                unit = make_valid_policy_b_unit()
                kwargs = default_policy_b_kwargs(unit)
                kwargs["candidate_snapshot"]["files"]["calculator.py"] = entry
                decision = classify_retry_eligibility(unit, **kwargs)
                self.assertFalse(decision.is_allowed(), decision)
                self.assertEqual(decision.decision, DECISION_DENY)
                self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)

    def test_policy_b_fingerprint_tampering_denies(self):
        unit = make_valid_policy_b_unit()
        kwargs = default_policy_b_kwargs(unit)
        kwargs["candidate_snapshot"]["fingerprint"] = "0" * 64
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed(), decision)
        self.assertEqual(decision.decision, DECISION_DENY)
        self.assertEqual(decision.reason_code, DENY_CONTRADICTORY_EVIDENCE)

    def test_policy_b_git_branch_drift_denies(self):
        unit = make_valid_policy_b_unit()
        kwargs = default_policy_b_kwargs(unit)
        kwargs["candidate_snapshot"] = make_valid_candidate_snapshot(branch="refs/heads/feature-branch")
        kwargs["verification_snapshot_before"] = deepcopy(kwargs["candidate_snapshot"])
        kwargs["verification_snapshot_after"] = deepcopy(kwargs["candidate_snapshot"])
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed(), decision)
        self.assertEqual(decision.decision, DECISION_DENY)
        self.assertEqual(decision.reason_code, DENY_UNVERIFIED_MUTATIONS)

    def test_policy_b_detached_head_empty_branch_allows(self):
        unit = make_valid_policy_b_unit()
        unit["attempts"][0]["pre_attempt_snapshot"]["branch"] = ""
        unit["attempts"][0]["pre_attempt_snapshot"]["fingerprint"] = _durable_digest(
            {k: unit["attempts"][0]["pre_attempt_snapshot"][k] for k in unit["attempts"][0]["pre_attempt_snapshot"] if k != "fingerprint"}
        )
        kwargs = default_policy_b_kwargs(unit)
        kwargs["candidate_snapshot"] = make_valid_candidate_snapshot(branch="")
        kwargs["verification_snapshot_before"] = deepcopy(kwargs["candidate_snapshot"])
        kwargs["verification_snapshot_after"] = deepcopy(kwargs["candidate_snapshot"])
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertTrue(decision.is_allowed(), decision)
        self.assertEqual(decision.decision, DECISION_ALLOW)

    def test_policy_b_git_identity_drift_denies(self):
        unit = make_valid_policy_b_unit()
        kwargs = default_policy_b_kwargs(unit)
        kwargs["candidate_snapshot"] = make_valid_candidate_snapshot(git_identity=[999, 888])
        kwargs["verification_snapshot_before"] = deepcopy(kwargs["candidate_snapshot"])
        kwargs["verification_snapshot_after"] = deepcopy(kwargs["candidate_snapshot"])
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed(), decision)
        self.assertEqual(decision.decision, DECISION_DENY)
        self.assertEqual(decision.reason_code, DENY_UNVERIFIED_MUTATIONS)

    def test_policy_b_repository_path_mismatch_denies(self):
        unit = make_valid_policy_b_unit()
        kwargs = default_policy_b_kwargs(unit)
        kwargs["candidate_snapshot"] = make_valid_candidate_snapshot(repository=r"C:\Other\Repo")
        kwargs["verification_snapshot_before"] = deepcopy(kwargs["candidate_snapshot"])
        kwargs["verification_snapshot_after"] = deepcopy(kwargs["candidate_snapshot"])
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed(), decision)
        self.assertEqual(decision.decision, DECISION_DENY)
        self.assertEqual(decision.reason_code, DENY_UNVERIFIED_MUTATIONS)

    def test_policy_b_git_directory_mismatch_denies(self):
        unit = make_valid_policy_b_unit()
        kwargs = default_policy_b_kwargs(unit)
        kwargs["candidate_snapshot"] = make_valid_candidate_snapshot(git_directory=r"C:\Other\Repo\.git")
        kwargs["verification_snapshot_before"] = deepcopy(kwargs["candidate_snapshot"])
        kwargs["verification_snapshot_after"] = deepcopy(kwargs["candidate_snapshot"])
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed(), decision)
        self.assertEqual(decision.decision, DECISION_DENY)
        self.assertEqual(decision.reason_code, DENY_UNVERIFIED_MUTATIONS)

    def test_policy_b_verification_before_mismatch_with_candidate_denies(self):
        unit = make_valid_policy_b_unit()
        kwargs = default_policy_b_kwargs(unit)
        kwargs["verification_snapshot_before"] = make_valid_candidate_snapshot(head="c" * 40)
        kwargs["verification_snapshot_after"] = deepcopy(kwargs["verification_snapshot_before"])
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed(), decision)
        self.assertEqual(decision.decision, DECISION_DENY)
        self.assertEqual(decision.reason_code, DENY_UNVERIFIED_MUTATIONS)

    def test_policy_b_verification_after_mismatch_with_verification_before_denies(self):
        unit = make_valid_policy_b_unit()
        kwargs = default_policy_b_kwargs(unit)
        kwargs["verification_snapshot_after"] = make_valid_candidate_snapshot(changed_paths=["calculator.py", "tests/test_calculator.py"])
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed(), decision)
        self.assertEqual(decision.decision, DECISION_DENY)
        self.assertEqual(decision.reason_code, DENY_UNVERIFIED_MUTATIONS)

    def test_policy_b_missing_tracked_file_deletion_allows(self):
        unit = make_valid_policy_b_unit(changed_paths=["calculator.py"])
        kwargs = default_policy_b_kwargs(unit, changed_paths=["calculator.py"])
        cand = make_valid_candidate_snapshot(changed_paths=[])
        cand["files"]["calculator.py"] = None
        cand["fingerprint"] = _durable_digest({k: cand[k] for k in cand if k != "fingerprint"})
        kwargs["candidate_snapshot"] = cand
        kwargs["verification_snapshot_before"] = deepcopy(cand)
        kwargs["verification_snapshot_after"] = deepcopy(cand)
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertTrue(decision.is_allowed(), decision)
        self.assertEqual(decision.decision, DECISION_ALLOW)

    def test_policy_b_removed_untracked_path_allows(self):
        unit = make_valid_policy_b_unit(allowed_paths=["calculator.py", "temp.tmp"], changed_paths=["temp.tmp"])
        unit["attempts"][0]["pre_attempt_snapshot"]["files"]["temp.tmp"] = {"sha256": hashlib.sha256(b"temp-sha").hexdigest(), "mode": 0o666}
        unit["attempts"][0]["pre_attempt_snapshot"]["fingerprint"] = _durable_digest(
            {k: unit["attempts"][0]["pre_attempt_snapshot"][k] for k in unit["attempts"][0]["pre_attempt_snapshot"] if k != "fingerprint"}
        )
        kwargs = default_policy_b_kwargs(unit, changed_paths=["temp.tmp"])
        cand = make_valid_candidate_snapshot(changed_paths=[])
        kwargs["candidate_snapshot"] = cand
        kwargs["verification_snapshot_before"] = deepcopy(cand)
        kwargs["verification_snapshot_after"] = deepcopy(cand)
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertTrue(decision.is_allowed(), decision)
        self.assertEqual(decision.decision, DECISION_ALLOW)

    def test_policy_b_added_file_in_allowed_scope_allows(self):
        unit = make_valid_policy_b_unit(
            allowed_paths=["calculator.py", "helper.py"],
            changed_paths=["calculator.py", "helper.py"],
        )
        kwargs = default_policy_b_kwargs(unit, changed_paths=["calculator.py", "helper.py"])
        cand = make_valid_candidate_snapshot(changed_paths=["calculator.py", "helper.py"])
        kwargs["candidate_snapshot"] = cand
        kwargs["verification_snapshot_before"] = deepcopy(cand)
        kwargs["verification_snapshot_after"] = deepcopy(cand)
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertTrue(decision.is_allowed(), decision)
        self.assertEqual(decision.decision, DECISION_ALLOW)

    def test_policy_b_restored_tracked_file_allows(self):
        unit = make_valid_policy_b_unit(changed_paths=["calculator.py"])
        unit["attempts"][0]["pre_attempt_snapshot"]["files"]["calculator.py"] = None
        unit["attempts"][0]["pre_attempt_snapshot"]["fingerprint"] = _durable_digest(
            {k: unit["attempts"][0]["pre_attempt_snapshot"][k] for k in unit["attempts"][0]["pre_attempt_snapshot"] if k != "fingerprint"}
        )
        kwargs = default_policy_b_kwargs(unit, changed_paths=["calculator.py"])
        cand = make_valid_candidate_snapshot(changed_paths=["calculator.py"])
        kwargs["candidate_snapshot"] = cand
        kwargs["verification_snapshot_before"] = deepcopy(cand)
        kwargs["verification_snapshot_after"] = deepcopy(cand)
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertTrue(decision.is_allowed(), decision)
        self.assertEqual(decision.decision, DECISION_ALLOW)

    def test_policy_b_file_mode_change_allows(self):
        unit = make_valid_policy_b_unit(changed_paths=["calculator.py"])
        kwargs = default_policy_b_kwargs(unit, changed_paths=["calculator.py"])
        cand = make_valid_candidate_snapshot(changed_paths=[])
        cand["files"]["calculator.py"] = {"sha256": hashlib.sha256(b"orig-hash").hexdigest(), "mode": 0o444}
        cand["fingerprint"] = _durable_digest({k: cand[k] for k in cand if k != "fingerprint"})
        kwargs["candidate_snapshot"] = cand
        kwargs["verification_snapshot_before"] = deepcopy(cand)
        kwargs["verification_snapshot_after"] = deepcopy(cand)
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertTrue(decision.is_allowed(), decision)
        self.assertEqual(decision.decision, DECISION_ALLOW)

    def test_policy_b_unrecorded_mutation_denies(self):
        unit = make_valid_policy_b_unit(changed_paths=["calculator.py"])
        kwargs = default_policy_b_kwargs(unit, changed_paths=["calculator.py"])
        cand = make_valid_candidate_snapshot(changed_paths=["calculator.py", "untracked.py"])
        kwargs["candidate_snapshot"] = cand
        kwargs["verification_snapshot_before"] = deepcopy(cand)
        kwargs["verification_snapshot_after"] = deepcopy(cand)
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed(), decision)
        self.assertEqual(decision.decision, DECISION_DENY)
        self.assertEqual(decision.reason_code, DENY_UNVERIFIED_MUTATIONS)

    def test_policy_b_unreflected_claimed_path_denies(self):
        unit = make_valid_policy_b_unit(
            allowed_paths=["calculator.py", "tests/test_calculator.py"],
            forbidden_paths=[],
            changed_paths=["calculator.py", "tests/test_calculator.py"],
        )
        kwargs = default_policy_b_kwargs(unit, changed_paths=["calculator.py", "tests/test_calculator.py"])
        cand = make_valid_candidate_snapshot(changed_paths=["calculator.py"])
        kwargs["candidate_snapshot"] = cand
        kwargs["verification_snapshot_before"] = deepcopy(cand)
        kwargs["verification_snapshot_after"] = deepcopy(cand)
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed(), decision)
        self.assertEqual(decision.decision, DECISION_DENY)
        self.assertEqual(decision.reason_code, DENY_CONTRADICTORY_EVIDENCE)

    def test_policy_b_frozen_file_mutation_denies(self):
        unit = make_valid_policy_b_unit(
            allowed_paths=["calculator.py", "tests/test_calculator.py"],
            forbidden_paths=["tests/test_calculator.py"],
            changed_paths=["calculator.py", "tests/test_calculator.py"],
        )
        kwargs = default_policy_b_kwargs(unit, changed_paths=["calculator.py", "tests/test_calculator.py"])
        cand = make_valid_candidate_snapshot(changed_paths=["calculator.py", "tests/test_calculator.py"])
        kwargs["candidate_snapshot"] = cand
        kwargs["verification_snapshot_before"] = deepcopy(cand)
        kwargs["verification_snapshot_after"] = deepcopy(cand)
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed(), decision)
        self.assertEqual(decision.decision, DECISION_DENY)
        self.assertEqual(decision.reason_code, DENY_SCOPE_VIOLATION)

    def test_policy_b_frozen_file_missing_coverage_denies(self):
        unit = make_valid_policy_b_unit(
            forbidden_paths=["tests/test_calculator.py", "missing_frozen_file.py"],
        )
        kwargs = default_policy_b_kwargs(unit)
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed(), decision)
        self.assertEqual(decision.decision, DECISION_DENY)
        self.assertEqual(decision.reason_code, DENY_MISSING_EVIDENCE)

    def test_policy_b_frozen_directory_addition_denies(self):
        unit = make_valid_policy_b_unit(
            forbidden_paths=["tests/"],
            allowed_paths=["calculator.py", "tests/new_test.py"],
            changed_paths=["calculator.py", "tests/new_test.py"],
        )
        kwargs = default_policy_b_kwargs(unit, changed_paths=["calculator.py", "tests/new_test.py"])
        cand = make_valid_candidate_snapshot(changed_paths=["calculator.py", "tests/new_test.py"])
        kwargs["candidate_snapshot"] = cand
        kwargs["verification_snapshot_before"] = deepcopy(cand)
        kwargs["verification_snapshot_after"] = deepcopy(cand)
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed(), decision)
        self.assertEqual(decision.decision, DECISION_DENY)
        self.assertEqual(decision.reason_code, DENY_SCOPE_VIOLATION)

    def test_policy_b_read_only_mode_with_mutation_denies(self):
        unit = make_valid_policy_b_unit(mode="read_only", changed_paths=[])
        kwargs = default_policy_b_kwargs(unit, changed_paths=[])
        cand = make_valid_candidate_snapshot(changed_paths=["calculator.py"])
        kwargs["candidate_snapshot"] = cand
        kwargs["verification_snapshot_before"] = deepcopy(cand)
        kwargs["verification_snapshot_after"] = deepcopy(cand)
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertFalse(decision.is_allowed(), decision)
        self.assertEqual(decision.decision, DECISION_DENY)
        self.assertEqual(decision.reason_code, DENY_SCOPE_VIOLATION)

    def test_real_git_repository_durable_snapshot_compatibility(self):
        """Test with genuine durable.snapshot() output from a real isolated Git repository."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_path = Path(tmp_dir) / "test_repo"
            repo_path.mkdir()
            subprocess.run(["git", "-C", str(repo_path), "init", "-q"], check=True)
            subprocess.run(["git", "-C", str(repo_path), "config", "user.name", "Triglav Test"], check=True)
            subprocess.run(["git", "-C", str(repo_path), "config", "user.email", "test@triglav.local"], check=True)
            (repo_path / "calculator.py").write_text("def add(a, b): return a + b\n", encoding="utf-8")
            (repo_path / "test_calculator.py").write_text("import unittest\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo_path), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo_path), "commit", "-qm", "initial commit"], check=True)

            log_path = Path(tmp_dir) / "log.jsonl"
            repo = harness.Repository(repo_path, {}, harness.EventLog(log_path))

            # Genuine pre-attempt snapshot
            pre_snap = durable.snapshot(repo)

            # Mutate calculator.py
            (repo_path / "calculator.py").write_text("def add(a, b): return a + b + 1\n", encoding="utf-8")

            # Genuine candidate and verification snapshots
            cand_snap = durable.snapshot(repo)
            v_before = durable.snapshot(repo)
            v_after = durable.snapshot(repo)

            unit = make_valid_policy_b_unit(
                allowed_paths=["calculator.py"],
                forbidden_paths=["test_calculator.py"],
                changed_paths=["calculator.py"],
            )
            unit["attempts"][0]["pre_attempt_snapshot"] = pre_snap

            kwargs = default_policy_b_kwargs(unit, changed_paths=["calculator.py"])
            kwargs["candidate_snapshot"] = cand_snap
            kwargs["verification_snapshot_before"] = v_before
            kwargs["verification_snapshot_after"] = v_after

            decision = classify_retry_eligibility(unit, **kwargs)
            self.assertTrue(decision.is_allowed(), decision)
            self.assertEqual(decision.decision, DECISION_ALLOW)
            self.assertEqual(decision.reason_code, ALLOW_VERIFIER_FAILURE)
            self.assertEqual(decision.details["policy"], "B")


class TestPhase31C2IContractConformance(unittest.TestCase):
    """The complete preserved 2H matrix, with real producer snapshots."""

    def test_complete_39_case_native_matrix(self):
        from pathlib import Path
        from copy import deepcopy
        from collections import Counter
        import tempfile, subprocess, stat
        import durable, harness, retry_eligibility as classifier

        counts=Counter()
        def git(root,*args):
            return subprocess.run(['git','-C',str(root),*args],check=True,capture_output=True)
        def seal(s):
            s['fingerprint']=durable.digest({k:v for k,v in s.items() if k!='fingerprint'})
        def decide(label,pre,candidate,changed,expected='ALLOW',tweak=None):
            unit=make_valid_policy_b_unit(allowed_paths=['calculator.py','helper.py','missing.py'],forbidden_paths=['test_calculator.py'],changed_paths=changed)
            unit['attempts'][0]['pre_attempt_snapshot']=deepcopy(pre)
            kwargs=default_policy_b_kwargs(unit,changed_paths=changed)
            kwargs.update(candidate_snapshot=deepcopy(candidate),verification_snapshot_before=deepcopy(candidate),verification_snapshot_after=deepcopy(candidate))
            if tweak: tweak(unit,kwargs)
            d=classifier.classify_retry_eligibility(unit,**kwargs)
            counts[(expected,d.decision)]+=1
            with self.subTest(case=label):
                self.assertEqual(d.decision, expected, d)
                self.assertNotEqual(d.reason_code, DENY_DEFAULT, d)

        with tempfile.TemporaryDirectory(prefix='triglav-qualification-') as temporary:
            temporary_path=Path(temporary).resolve()
            assert temporary_path.is_relative_to(Path(tempfile.gettempdir()).resolve())
            root=temporary_path/'repository';root.mkdir()
            assert root.resolve().is_relative_to(temporary_path)
            git(root,'init','-q')
            initial={'calculator.py':'x = 1\n','test_calculator.py':'# frozen verification input\n','missing.py':'# tracked optional input\n','unicode-é.txt':'evidence\n'}
            def reset_files():
                for name,content in initial.items():
                    p=root/name
                    if p.exists(): p.chmod(stat.S_IREAD|stat.S_IWRITE)
                    p.write_text(content,encoding='utf-8')
                extra=root/'helper.py'
                if extra.exists(): extra.unlink()
            reset_files()
            git(root,'add','.')
            git(root,'-c','user.name=Offline Qualification','-c','user.email=offline@example.invalid','-c','core.hooksPath=NUL','commit','-qm','disposable fixture')
            repo=harness.Repository(root,{},harness.EventLog(temporary_path/'events.jsonl'))
            pre=durable.snapshot(repo)
            self.assertEqual(pre['files']['calculator.py']['mode'], stat.S_IMODE((root/'calculator.py').stat().st_mode))
            self.assertEqual(classifier._durable_digest({k:v for k,v in pre.items() if k!='fingerprint'}), pre['fingerprint'])
            for case in ('edit','addition','tracked deletion','restoration','untracked deletion','unchanged missing tracked','permission mode change','missing frozen test'):
                reset_files()
                if case=='restoration': (root/'calculator.py').unlink()
                if case=='untracked deletion': (root/'helper.py').write_text('temporary\n')
                if case=='unchanged missing tracked': (root/'missing.py').unlink()
                if case=='missing frozen test': (root/'test_calculator.py').unlink()
                before=durable.snapshot(repo)
                changed=['calculator.py'];expected='ALLOW'
                if case in ('edit','unchanged missing tracked','missing frozen test'): (root/'calculator.py').write_text('x = 2\n')
                elif case=='addition': (root/'helper.py').write_text('new = 1\n');changed=['helper.py']
                elif case=='tracked deletion': (root/'calculator.py').unlink()
                elif case=='restoration': (root/'calculator.py').write_text('x = 1\n')
                elif case=='untracked deletion': (root/'helper.py').unlink();changed=['helper.py']
                elif case=='permission mode change': (root/'calculator.py').chmod(stat.S_IREAD)
                if case=='missing frozen test': expected='DENY'
                after=durable.snapshot(repo)
                if case=='permission mode change': self.assertNotEqual(before['files']['calculator.py']['mode'], after['files']['calculator.py']['mode'])
                decide('NATIVE '+case,before,after,changed,expected)
            reset_files();pre=durable.snapshot(repo);(root/'calculator.py').write_text('x = 2\n');candidate=durable.snapshot(repo)
            def evaluate(label,tweak,expected='DENY'):
                decide(label,pre,candidate,['calculator.py'],expected,tweak)
            for source in ('context candidate','attempt candidate','attempt post'):
                for value in (None,{}):
                    def place(u,k,source=source,value=value):
                        if source=='context candidate': k['context']={'candidate_snapshot':value}
                        else: u['attempts'][0]['candidate_snapshot' if source=='attempt candidate' else 'post_attempt_snapshot']=value
                    evaluate(f'SOURCE {source} {value!r}',place)
            def contradictory_pre(u,k):
                s=deepcopy(u['attempts'][0]['pre_attempt_snapshot']);s['head']='f'*40;seal(s);k['context']={'pre_attempt_snapshot':s}
            evaluate('conflicting pre-attempt source',contradictory_pre)
            def missing_pre(u,k):
                s=u['attempts'][0].pop('pre_attempt_snapshot');k['context']={'pre_attempt_snapshot':s}
            evaluate('mandatory attempt pre missing, context fallback',missing_pre)
            for name,value in (('verification_snapshot_before',None),('verification_snapshot_after',None),('verification_snapshot_before',[])):
                evaluate(f'VERIFICATION {name} {value!r}',lambda u,k,name=name,value=value:k.update({name:value}))
            def mutation(u,k):
                k['verification_snapshot_after']['files']['calculator.py']['sha256']='f'*64;seal(k['verification_snapshot_after'])
            evaluate('verification file mutation',mutation)
            def candidate_identity(field,value):
                def change(u,k):
                    for name in ('candidate_snapshot','verification_snapshot_before','verification_snapshot_after'):
                        k[name][field]=value;seal(k[name])
                return change
            evaluate('branch drift',candidate_identity('branch','refs/heads/foreign'))
            evaluate('index drift',candidate_identity('index_sha256','f'*64))
            evaluate('fingerprint corruption',lambda u,k:k['candidate_snapshot'].update(fingerprint='0'*64))
            def all_stages(u,k):
                return [u['attempts'][0]['pre_attempt_snapshot']]+[k[name] for name in ('candidate_snapshot','verification_snapshot_before','verification_snapshot_after')]
            for field,value in (('repository','relative/repo'),('git_identity',[-1,-2]),('head','not-an-object-id'),('branch','not a git ref'),('status','not porcelain'),('index_sha256','not-a-sha256')):
                def invalid(u,k,field=field,value=value):
                    for s in all_stages(u,k):s[field]=deepcopy(value);seal(s)
                evaluate(f'MALFORMED {field}',invalid)
            for mode in (33188,33261,4096,10**50):
                def invalid_mode(u,k,mode=mode):
                    for s in all_stages(u,k):s['files']['test_calculator.py']['mode']=mode;seal(s)
                evaluate(f'MODE outside S_IMODE {mode}',invalid_mode)
            def invalid_sha(u,k):
                for s in all_stages(u,k):s['files']['test_calculator.py']['sha256']='not-a-sha256';seal(s)
            evaluate('non-hex frozen content digest',invalid_sha)
            def unsafe_path(u,k):
                for s in all_stages(u,k):s['files']['../escape.py']={'sha256':'a'*64,'mode':0o666};seal(s)
            evaluate('unsafe repository-relative file key',unsafe_path)
            def duplicate_path(u,k):
                for s in all_stages(u,k):s['files']['CALCULATOR.PY']=deepcopy(s['files']['calculator.py']);seal(s)
            evaluate('Windows duplicate file alias',duplicate_path)
            evaluate('wrong-typed mutation flag',lambda u,k:k.update(repo_mutated_during_verification=1))
            evaluate('unrecorded actual delta',lambda u,k:u['attempts'][0].update(changed_paths=[]))
            def frozen_mutation(u,k):
                for name in ('candidate_snapshot','verification_snapshot_before','verification_snapshot_after'):
                    k[name]['files']['test_calculator.py']['sha256']='f'*64;seal(k[name])
                u['attempts'][0]['changed_paths'].append('test_calculator.py')
            evaluate('forbidden frozen mutation',frozen_mutation)
            reset_files()
        self.assertEqual(sum(counts.values()), 39)
        self.assertEqual(counts, {('ALLOW', 'ALLOW'): 7, ('DENY', 'DENY'): 32})


    def valid_inputs(self):
        unit = make_valid_policy_b_unit()
        return unit, default_policy_b_kwargs(unit)

    def snapshots(self, unit, kwargs):
        return [unit["attempts"][0]["pre_attempt_snapshot"]] + [
            kwargs[k] for k in ("candidate_snapshot", "verification_snapshot_before", "verification_snapshot_after")]

    def check_body_change(self, change, allowed=False):
        unit, kwargs = self.valid_inputs()
        self.assertTrue(classify_retry_eligibility(unit, **kwargs).is_allowed())
        for snap in self.snapshots(unit, kwargs):
            change(snap)
            snap["fingerprint"] = durable.digest({k: v for k, v in snap.items() if k != "fingerprint"})
        decision = classify_retry_eligibility(unit, **kwargs)
        self.assertEqual(decision.is_allowed(), allowed, decision)
        self.assertNotEqual(decision.reason_code, DENY_DEFAULT, decision)

    def test_permission_bits_exact_range(self):
        for mode in (True, False, -1, 0o10000, 33188, 33261, 10**50, 438.0, "438"):
            with self.subTest(invalid=mode):
                self.check_body_change(lambda s: s["files"]["tests/test_calculator.py"].update(mode=mode))
        for mode in (0, 0o444, 0o666, 0o755, 0o7777):
            with self.subTest(valid=mode):
                self.check_body_change(lambda s: s["files"]["tests/test_calculator.py"].update(mode=mode), allowed=True)

    def test_canonical_absolute_repository_and_git_paths(self):
        invalid = (None, False, "relative/repo", "C:relative", "C:/Repo", "C:\\Repo\\..\\Else",
                   "C:\\Repo\\.\\Child", "C:\\Repo\\", "C:\\Repo. ", "C:\\NUL\\Repo",
                   "C:\\Repo\0", "C:\\PROGRA~1\\Repo", "\\\\?\\C:\\Repo")
        for field in ("repository", "git_directory"):
            for path in invalid:
                with self.subTest(field=field, invalid=path):
                    self.check_body_change(lambda s: s.update({field: path}))
            for path in ("C:\\Repo", "C:\\", "\\\\server\\share\\repo", "/tmp/repo"):
                with self.subTest(field=field, valid=path):
                    self.check_body_change(lambda s: s.update({field: path}), allowed=True)

    def test_git_identity_nonnegative_exact_integers(self):
        for value in ([-1, 2], [1, -2], [True, 2], [1, False], [1.0, 2], (1, 2), [], [1, 2, 3]):
            with self.subTest(value=value):
                self.check_body_change(lambda s: s.update(git_identity=value))
        self.check_body_change(lambda s: s.update(git_identity=[2**64-1, 2**100]), allowed=True)

    def test_head_and_sha256_formats(self):
        for value in ("abc1234", "g" * 40, "A" * 40, "a" * 39, "a" * 41, None, True):
            with self.subTest(head=value):
                self.check_body_change(lambda s: s.update(head=value))
        self.check_body_change(lambda s: s.update(head="c" * 64), allowed=True)
        for value in ("", "not-a-sha256", "A" * 64, "g" * 64, "a" * 63, "a" * 65, 123, None):
            with self.subTest(index=value):
                self.check_body_change(lambda s: s.update(index_sha256=value))
            with self.subTest(content=value):
                self.check_body_change(lambda s: s["files"]["tests/test_calculator.py"].update(sha256=value))

    def test_full_git_reference_grammar(self):
        for ref in ("main", "not a git ref", "refs/heads/a..b", "refs/heads/.hidden", "refs/heads/a.lock",
                    "refs/heads/end.", "refs/heads/a@{b", "refs//heads/a", "refs/heads/a/", "refs/heads/a?b",
                    "refs/heads/a\\b", "refs/heads/a\0", "refs/heads/a\ud800"):
            with self.subTest(invalid=ref):
                self.check_body_change(lambda s: s.update(branch=ref))
        for ref in ("", "refs/heads/main", "refs/heads/feature/subtask", "refs/heads/é"):
            with self.subTest(valid=ref):
                self.check_body_change(lambda s: s.update(branch=ref), allowed=True)

    def test_porcelain_v1_z_record_framing(self):
        valid = ("", " M calculator.py\0", "?? new.py\0", " A new.py\0",
                 "R  destination.py\0source.py\0", " C destination.py\0source.py\0",
                 " M space name.py\0?? unicode-é.py\0") + tuple(
                     f"{xy} conflicted.py\0" for xy in ("DD", "AU", "UD", "UA", "DU", "AA", "UU"))
        for status in valid:
            with self.subTest(valid=repr(status)):
                self.check_body_change(lambda s: s.update(status=status), allowed=True)
        for status in ("not porcelain", " M calculator.py", " M \0", " M a.py\0\0", "ZZ a.py\0",
                       "   a.py\0", "R  destination.py\0", "R  destination.py\0\0", "?? ../escape.py\0",
                       " M a.py\0 M A.py\0", " M a.py\0extra.py\0", "!! ignored.py\0"):
            with self.subTest(invalid=repr(status)):
                self.check_body_change(lambda s: s.update(status=status))

    def test_file_paths_and_windows_aliases(self):
        invalid = ("../escape.py", "/absolute.py", "C:/absolute.py", "back\\slash.py", "./a.py", "x//a.py",
                   "x/../a.py", "a.py.", "a.py ", ".git/config", "NUL.py", "COM¹.txt", "CALCUL~1.PY", "a\0.py")
        for path in invalid:
            with self.subTest(path=path):
                self.check_body_change(lambda s: s["files"].update({path: {"sha256": "c" * 64, "mode": 0o666}}))
        self.check_body_change(lambda s: s["files"].update({"CALCULATOR.PY": deepcopy(s["files"]["calculator.py"])}))
        for path in ("unicode-é.py", "space name.py", "nested/file.py", ".hidden"):
            with self.subTest(valid=path):
                self.check_body_change(lambda s: s["files"].update({path: {"sha256": "c" * 64, "mode": 0o666}}), allowed=True)

    def test_mandatory_attempt_pre_cannot_be_replaced(self):
        for value in (None, {}, False, []):
            unit, kwargs = self.valid_inputs()
            kwargs["context"] = {"pre_attempt_snapshot": deepcopy(unit["attempts"][0]["pre_attempt_snapshot"])}
            unit["attempts"][0]["pre_attempt_snapshot"] = value
            with self.subTest(value=value):
                self.assertEqual(classify_retry_eligibility(unit, **kwargs).reason_code, DENY_MISSING_EVIDENCE)

    def test_frozen_none_is_not_coverage(self):
        self.check_body_change(lambda s: s["files"].update({"tests/test_calculator.py": None}))
        unit, kwargs = self.valid_inputs()
        unit["spec"]["scope"]["forbidden_paths"] = ["tests/"]
        for snap in self.snapshots(unit, kwargs):
            snap["files"]["tests/test_calculator.py"] = None
            snap["fingerprint"] = durable.digest({k: v for k, v in snap.items() if k != "fingerprint"})
        self.assertEqual(classify_retry_eligibility(unit, **kwargs).reason_code, DENY_MISSING_EVIDENCE)

    def test_mutation_flag_exactness_and_conflicting_context(self):
        for value in (None, True, 1, 0, 0.0, "", "false", [], {}):
            for source in ("argument", "context"):
                unit, kwargs = self.valid_inputs()
                if source == "argument":
                    kwargs["repo_mutated_during_verification"] = value
                else:
                    kwargs["context"] = {"repo_mutated_during_verification": value}
                with self.subTest(source=source, value=value):
                    decision = classify_retry_eligibility(unit, **kwargs)
                    self.assertFalse(decision.is_allowed(), decision)
                    self.assertNotEqual(decision.reason_code, DENY_DEFAULT)
        unit, kwargs = self.valid_inputs()
        del kwargs["repo_mutated_during_verification"]
        self.assertTrue(classify_retry_eligibility(unit, **kwargs).is_allowed())

    def test_all_snapshot_source_types_fail_without_fallback(self):
        sources = (("pre_attempt_snapshot", "context"), ("pre_attempt_snapshot", "attempt"),
                   ("candidate_snapshot", "argument"), ("candidate_snapshot", "context"),
                   ("candidate_snapshot", "attempt"), ("post_attempt_snapshot", "attempt")) + tuple(
                       (key, source) for key in ("verification_snapshot_before", "verification_snapshot_after")
                       for source in ("argument", "context", "attempt"))
        for key, source in sources:
            for value in (None, {}, [], False):
                unit, kwargs = self.valid_inputs()
                owner = kwargs if source == "argument" else (
                    kwargs.setdefault("context", {}) if source == "context" else unit["attempts"][0])
                owner[key] = value
                with self.subTest(key=key, source=source, value=value):
                    self.assertEqual(classify_retry_eligibility(unit, **kwargs).reason_code, DENY_MISSING_EVIDENCE)

    def test_matching_all_snapshot_sources_allow(self):
        unit, kwargs = self.valid_inputs()
        attempt = unit["attempts"][0]
        kwargs["context"] = {"pre_attempt_snapshot": deepcopy(attempt["pre_attempt_snapshot"])}
        for key in ("candidate_snapshot", "verification_snapshot_before", "verification_snapshot_after"):
            attempt[key] = deepcopy(kwargs[key])
            kwargs["context"][key] = deepcopy(kwargs[key])
        attempt["post_attempt_snapshot"] = deepcopy(kwargs["candidate_snapshot"])
        self.assertTrue(classify_retry_eligibility(unit, **kwargs).is_allowed())

    def test_read_only_unchanged_snapshot_allows(self):
        unit, kwargs = self.valid_inputs()
        unit["spec"]["mode"] = "read_only"
        unit["attempts"][0]["changed_paths"] = []
        for key in ("candidate_snapshot", "verification_snapshot_before", "verification_snapshot_after"):
            kwargs[key] = deepcopy(unit["attempts"][0]["pre_attempt_snapshot"])
        self.assertTrue(classify_retry_eligibility(unit, **kwargs).is_allowed())

    def test_legacy_falsy_fallback_exact_decisions(self):
        # Historical 3.1C2C fixtures are also loaded unchanged by the external
        # differential probe; this test remains self-contained without the ZIP.
        for policy in (None, "A"):
            for value in ({}, False, "", [], None):
                for fallback in ("attempt", "post", "context"):
                    unit = make_valid_unit()
                    kwargs = default_trusted_kwargs(unit)
                    good = deepcopy(kwargs["candidate_snapshot"])
                    if fallback == "attempt":
                        unit["attempts"][0]["candidate_snapshot"] = good
                    elif fallback == "post":
                        unit["attempts"][0]["post_attempt_snapshot"] = good
                    else:
                        kwargs["context"] = {"candidate_snapshot": good}
                    kwargs.update(candidate_snapshot=value, policy=policy)
                    # Context fallback historically happens only for None, not
                    # other falsy arguments; the final or-chain excludes context.
                    expected = fallback != "context" or value is None
                    with self.subTest(policy=policy, value=value, fallback=fallback):
                        self.assertEqual(classify_retry_eligibility(unit, **kwargs).is_allowed(), expected)

    def test_real_git_format_equivalence_classes(self):
        from retry_eligibility import validate_normalize_snapshot
        import stat
        for object_format in ("sha1", "sha256"):
            with tempfile.TemporaryDirectory(prefix="triglav-formats-") as temporary:
                parent = Path(temporary).resolve()
                self.assertTrue(parent.is_relative_to(Path(tempfile.gettempdir()).resolve()))
                root = parent / "repository"
                root.mkdir()
                def git(*args):
                    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
                git("init", "-q", f"--object-format={object_format}")
                (root / "calculator.py").write_text("x = 1\n", encoding="utf-8")
                git("add", ".")
                git("-c", "user.name=Offline Qualification", "-c", "user.email=offline@example.invalid",
                    "-c", "core.hooksPath=NUL", "commit", "-qm", "disposable fixture")
                repo = harness.Repository(root, {}, harness.EventLog(parent / "events.jsonl"))
                def observe():
                    snap = durable.snapshot(repo)
                    validated, error = validate_normalize_snapshot(snap, "real Git")
                    self.assertIsNone(error, error)
                    self.assertEqual(validated, snap)
                    self.assertEqual(len(snap["head"]), 40 if object_format == "sha1" else 64)
                    self.assertEqual(snap["fingerprint"], durable.digest({k: v for k, v in snap.items() if k != "fingerprint"}))
                    return snap
                observe()
                (root / "calculator.py").write_text("x = 2\n", encoding="utf-8")
                self.assertIn(" M calculator.py\0", observe()["status"])
                extra = root / "unicode-é file.py"
                extra.write_text("new = 1\n", encoding="utf-8")
                self.assertIn("?? unicode-é file.py\0", observe()["status"])
                git("add", "--intent-to-add", "unicode-é file.py")
                self.assertIn(" A unicode-é file.py\0", observe()["status"])
                git("mv", "calculator.py", "renamed file.py")
                self.assertIn("renamed file.py\0calculator.py\0", observe()["status"])
                git("checkout", "--detach", "-q")
                self.assertEqual(observe()["branch"], "")
                target = root / "renamed file.py"
                target.chmod(stat.S_IREAD)
                try:
                    snap = observe()
                    self.assertEqual(snap["files"]["renamed file.py"]["mode"], stat.S_IMODE(target.stat().st_mode))
                finally:
                    target.chmod(stat.S_IREAD | stat.S_IWRITE)


if __name__ == "__main__":
    unittest.main()
