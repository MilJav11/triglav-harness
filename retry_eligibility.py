"""Deterministic retry-eligibility classifier for TRIGLAV WorkUnits.

Phase 3.1: Evaluates whether a failed WorkUnit attempt is eligible for
bounded recovery (Attempt 2). Default is strictly DENY.

Controller vs. Model Authority:
--------------------------------
All evidence evaluated by this classifier MUST originate from authoritative
controller, supervisor, filesystem, and deterministic verifier sources:
- Spec limits, scope, and verifier IDs (controller-owned, immutable)
- Pre-attempt and post-attempt repository snapshots (durable.snapshot)
- Supervisor process reconciliation records (supervision.Supervisor / Job Object)
- Deterministic verification execution (verifier_registry binding)
- Environment fingerprint comparison (environment.compare)
- Cumulative task and unit budgets (controller clock-tracked)

Arbitrary model-provided flags, assertions of correctness, self-evaluation,
or retry request parameters are untrusted and completely ignored.

Remaining Integration-Only Guarantees (Must be enforced by harness):
--------------------------------------------------------------------
- Pre-attempt and candidate snapshots must be captured via durable.snapshot(repo)
- Verifier registry definitions must be bound by controller from verified plan
- Supervisor instance must be a live supervision.Supervisor backed by Windows Job Objects
- Persisted WAL evidence must be committed under store.mutex
- Native observations must be bound to the trusted run/unit/attempt at invocation;
  matching identifiers in supplied dictionaries do not authenticate history.
"""

from __future__ import annotations

from dataclasses import dataclass
import copy
import hashlib
import json
import math
import os
import re
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

import work_units

# Supported retry policies
POLICY_A = "A"
POLICY_B = "B"
SUPPORTED_POLICIES = {"A", "B", "policy_b", "POLICY_B", "bounded_recovery"}


def _checksum(value: Any) -> str:
    """Canonical JSON sha256 checksum matching supervision.py:checksum."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _norm_path(p: Any) -> str:
    """Normalize filesystem paths matching supervision.py logic."""
    if not isinstance(p, (str, Path)):
        return ""
    try:
        return os.path.normcase(str(Path(p).resolve()))
    except Exception:
        return os.path.normcase(str(p))


# Stable decision constants
DECISION_ALLOW = "ALLOW"
DECISION_DENY = "DENY"

# Reason codes
ALLOW_VERIFIER_FAILURE = "ALLOW_VERIFIER_FAILURE"

DENY_ATTEMPTS_EXHAUSTED = "DENY_ATTEMPTS_EXHAUSTED"
DENY_SCOPE_VIOLATION = "DENY_SCOPE_VIOLATION"
DENY_TIMEOUT = "DENY_TIMEOUT"
DENY_UNVERIFIED_MUTATIONS = "DENY_UNVERIFIED_MUTATIONS"
DENY_ENVIRONMENT_DRIFT = "DENY_ENVIRONMENT_DRIFT"
DENY_PROCESS_NOT_IDLE = "DENY_PROCESS_NOT_IDLE"
DENY_MISSING_EVIDENCE = "DENY_MISSING_EVIDENCE"
DENY_CONTRADICTORY_EVIDENCE = "DENY_CONTRADICTORY_EVIDENCE"
DENY_INSUFFICIENT_BUDGET = "DENY_INSUFFICIENT_BUDGET"
DENY_UNKNOWN_FAILURE = "DENY_UNKNOWN_FAILURE"
DENY_DEFAULT = "DENY_DEFAULT"

# Valid scheduler states for evaluating retry eligibility
VALID_RETRY_STATUSES = ("VERIFYING", "EXECUTING", "REPAIR_PENDING")

# Recognized supervisor states from supervision.py
SUPERVISOR_LEGAL_STATES = {
    "STARTING", "RUNNING", "STOPPING", "EXITED", "LOST", "STALE", "UNKNOWN", "RESOLVED"
}

# Required fields in an authoritative durable.snapshot() dictionary (Policy A legacy)
REQUIRED_SNAPSHOT_FIELDS = (
    "repository",
    "git_directory",
    "git_identity",
    "head",
    "index_sha256",
    "files",
    "fingerprint",
)

# Canonical durable.snapshot() schema fields (Policy B canonical contract)
CANONICAL_SNAPSHOT_FIELDS = {
    "repository",
    "git_directory",
    "git_identity",
    "head",
    "branch",
    "status",
    "index_sha256",
    "files",
    "fingerprint",
}

_MISSING_SENTINEL = object()
_ABSENT_SENTINEL = object()


def _durable_digest(value: Any) -> str:
    """Canonical digest strictly matching durable.py:digest."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False).encode("utf-8")
    ).hexdigest()



@dataclass(frozen=True)
class RetryDecision:
    """Structured decision indicating retry eligibility and failure reason."""

    eligible: bool
    decision: str  # "ALLOW" or "DENY"
    reason_code: str
    details: dict[str, Any] | str | None = None

    def is_allowed(self) -> bool:
        return self.eligible and self.decision == DECISION_ALLOW

    def to_dict(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible,
            "decision": self.decision,
            "reason_code": self.reason_code,
            "details": self.details,
        }

    def __bool__(self) -> bool:
        return self.eligible


def _deny(reason_code: str, details: dict[str, Any] | str | None = None) -> RetryDecision:
    return RetryDecision(
        eligible=False,
        decision=DECISION_DENY,
        reason_code=reason_code,
        details=details,
    )


def _allow(reason_code: str, details: dict[str, Any] | str | None = None) -> RetryDecision:
    return RetryDecision(
        eligible=True,
        decision=DECISION_ALLOW,
        reason_code=reason_code,
        details=details,
    )


def _validate_budget_value(val: Any, name: str, min_val: float) -> tuple[bool, str]:
    if val is None:
        return False, f"missing {name}"
    if type(val) not in (int, float) or isinstance(val, bool):
        return False, f"{name} must be a number, got {type(val).__name__}"
    if not math.isfinite(val):
        return False, f"{name} must be finite (not NaN or infinity)"
    if val < min_val:
        return False, f"{name} ({val}s) < minimum required ({min_val}s)"
    return True, ""


def _type_exact_equal(left: Any, right: Any) -> bool:
    """Compare evidence recursively without Python's bool/int coercion."""
    if type(left) is not type(right):
        return False
    if type(left) is dict:
        return ({(type(k), k) for k in left} == {(type(k), k) for k in right}
                and all(_type_exact_equal(left[k], right[k]) for k in left))
    if type(left) is list:
        return (len(left) == len(right)
                and all(_type_exact_equal(a, b) for a, b in zip(left, right)))
    return left == right


def _native_process_key(pid: Any, identity: Any) -> tuple[int, str] | None:
    if (type(pid) is not int or pid <= 0 or type(identity) is not dict
            or set(identity) != {"created", "executable"}
            or any(type(identity[k]) is not str or not identity[k].strip()
                   for k in ("created", "executable"))):
        return None
    # Executable spelling must not allow the same PID/creation identity twice.
    return pid, identity["created"]


def _validate_lifecycle_metadata(record: dict[str, Any], label: str) -> RetryDecision | None:
    """Optional flat metadata cannot override or contradict native observations."""
    if "descendant_cleanup" in record:
        return _deny(DENY_CONTRADICTORY_EVIDENCE, f"{label}: worker cleanup receipt supplied")
    if "cleanup_error" in record:
        return _deny(DENY_PROCESS_NOT_IDLE, f"{label}: cleanup error supplied")
    if "termination" in record:
        term = record["termination"]
        if type(term) is str and term in ("forced", "timeout"):
            return _deny(DENY_TIMEOUT, f"{label}: termination is {term}")
        if type(term) is not str or term != "natural":
            return _deny(DENY_UNKNOWN_FAILURE, f"{label}: natural termination not established")
    if ("termination_basis" in record and
            (type(record["termination_basis"]) is not str or
             record["termination_basis"] != "signaled_without_controller_termination")):
        return _deny(DENY_UNKNOWN_FAILURE, f"{label}: conflicting termination basis")
    if "cleanup" in record:
        cleanup = record["cleanup"]
        if type(cleanup) is str and cleanup == "job_close":
            return _deny(DENY_CONTRADICTORY_EVIDENCE, f"{label}: worker Job-close receipt supplied")
        if type(cleanup) is not str or cleanup != "observed_empty":
            return _deny(DENY_PROCESS_NOT_IDLE, f"{label}: Job drainage not established")
    return None


def _validate_policy_b_evidence(
    verifier_ids: list[str], results: dict[str, Any], attempt: dict[str, Any],
    context: dict[str, Any] | None, children: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]] | RetryDecision:
    """Canonical structural boundary, before any Policy B eligibility evaluation.

    Every supplied source is validated before selection; identical copies for one
    invocation are permitted, reused children/processes for different invocations
    are not. This checks consistency, not the caller's provenance/authenticity.
    """
    if (any(type(vid) is not str or not vid.strip() for vid in verifier_ids)
            or len(set(verifier_ids)) != len(verifier_ids)):
        return _deny(DENY_CONTRADICTORY_EVIDENCE, "invalid or duplicate verifier IDs")
    if type(results) is not dict or set(results) != set(verifier_ids):
        return _deny(DENY_MISSING_EVIDENCE, "verifier_result must cover exactly the selected verifiers")
    if context is not None and type(context) is not dict:
        return _deny(DENY_MISSING_EVIDENCE, "context must be a dict")

    sources: dict[str, list[tuple[str, Any]]] = {vid: [] for vid in verifier_ids}
    metadata = [("attempt", attempt), ("context", context or {})]
    for label, owner in metadata:
        if "controller_evidence" in owner:
            container = owner["controller_evidence"]
            if (type(container) is not dict or not container or
                    any(type(vid) is not str or vid not in sources for vid in container)):
                return _deny(DENY_MISSING_EVIDENCE, f"{label}: malformed controller_evidence container")
            for vid, evidence in container.items():
                sources[vid].append((label, evidence))
    for vid, entry in results.items():
        if type(entry) is not dict:
            return _deny(DENY_MISSING_EVIDENCE, f"{vid}: malformed verifier result")
        if (type(entry.get("exit_code")) is not int or type(entry.get("passed")) is not bool
                or type(entry.get("argv")) is not list or not entry["argv"]
                or any(type(arg) is not str or not arg or "\0" in arg for arg in entry["argv"])
                or ("child_id" in entry and type(entry["child_id"]) is not str)):
            return _deny(DENY_MISSING_EVIDENCE, f"{vid}: malformed verifier result fields")
        metadata.append((f"result {vid}", entry))
        if "controller_evidence" in entry:
            sources[vid].append(("result", entry["controller_evidence"]))

    seen_processes: set[tuple[int, str]] = set()
    for cid, child in children.items():
        if type(child) is not dict or type(cid) is not str or not cid:
            return _deny(DENY_MISSING_EVIDENCE, "malformed supervisor child")
        pid, identity = child.get("pid"), child.get("identity")
        if pid is not None or identity is not None:
            key = _native_process_key(pid, identity)
            if key is None:
                return _deny(DENY_MISSING_EVIDENCE, f"{cid}: malformed native process identity")
            if key in seen_processes:
                return _deny(DENY_CONTRADICTORY_EVIDENCE, "supervisor children reuse native process identity")
            seen_processes.add(key)
        if child.get("purpose") == "verification_command":
            metadata.append((f"supervisor {cid}", child))

    string_fields = (
        "source", "child_id", "intended_command_sha256", "resolved_command_sha256",
        "job", "termination", "termination_basis", "cleanup", "run_id", "unit_id", "attempt_id",
    )
    required = set(string_fields) | {
        "schema_version", "broker", "verifier", "intended_argv", "resolved_argv",
        "observed_exit_code", "execution_started",
    }
    canonical: dict[str, dict[str, Any]] = {}
    seen_children: set[str] = set()
    seen_processes = set()
    for vid, records in sources.items():
        if not records:
            return _deny(DENY_MISSING_EVIDENCE, f"{vid}: missing controller_evidence")
        for label, evidence in records:
            label = f"{label} evidence for {vid}"
            if type(evidence) is not dict or not required.issubset(evidence):
                return _deny(DENY_MISSING_EVIDENCE, f"{label}: missing required native evidence fields")
            # Version 1 has a closed schema; failure-only cleanup_error is handled below.
            if set(evidence) - required - {"cleanup_error"}:
                return _deny(DENY_MISSING_EVIDENCE, f"{label}: unsupported native evidence fields")
            if (type(evidence["schema_version"]) is not int or evidence["schema_version"] != 1
                    or any(type(evidence[k]) is not str or not evidence[k].strip() for k in string_fields)
                    or type(evidence["observed_exit_code"]) is not int
                    or type(evidence["execution_started"]) is not bool):
                return _deny(DENY_MISSING_EVIDENCE, f"{label}: invalid native evidence field types")
            if evidence["broker"] is not None:
                return _deny(DENY_CONTRADICTORY_EVIDENCE, f"{label}: broker must be explicitly None")
            proc = evidence["verifier"]
            if (type(proc) is not dict or set(proc) != {"pid", "identity"}
                    or _native_process_key(proc["pid"], proc["identity"]) is None):
                return _deny(DENY_MISSING_EVIDENCE, f"{label}: malformed native verifier identity")
            for name in ("intended_argv", "resolved_argv"):
                argv = evidence[name]
                if (type(argv) is not list or not argv
                        or any(type(arg) is not str or not arg or "\0" in arg for arg in argv)):
                    return _deny(DENY_MISSING_EVIDENCE, f"{label}: malformed {name}")
            for name in ("intended_command_sha256", "resolved_command_sha256"):
                digest = evidence[name]
                if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                    return _deny(DENY_MISSING_EVIDENCE, f"{label}: malformed {name}")
            metadata.append((label, evidence))
            if vid in canonical and not _type_exact_equal(canonical[vid], evidence):
                return _deny(DENY_CONTRADICTORY_EVIDENCE, f"{vid}: conflicting controller_evidence sources")
            canonical[vid] = evidence
        evidence = canonical[vid]
        cid = evidence["child_id"]
        key = _native_process_key(evidence["verifier"]["pid"], evidence["verifier"]["identity"])
        if cid in seen_children or key in seen_processes:
            return _deny(DENY_CONTRADICTORY_EVIDENCE, "verifier invocations reuse a child or native process identity")
        seen_children.add(cid)
        seen_processes.add(key)
        child = children.get(cid)
        if child is not None:
            if (any(type(child.get(k)) is not str or not child[k].strip() for k in
                    ("purpose", "state", "executable", "command_sha256", "containment", "job", "termination", "run_id"))
                    or type(child.get("exit_code")) is not int):
                return _deny(DENY_MISSING_EVIDENCE, f"{cid}: malformed supervisor evidence fields")

    for label, record in metadata:
        invalid = _validate_lifecycle_metadata(record, label)
        if invalid is not None:
            return invalid
    return canonical


def collect_snapshot_sources(
    unit: dict[str, Any],
    attempt: dict[str, Any],
    context: dict[str, Any] | None,
    candidate_snapshot_arg: Any = _MISSING_SENTINEL,
    verification_snapshot_before_arg: Any = _MISSING_SENTINEL,
    verification_snapshot_after_arg: Any = _MISSING_SENTINEL,
) -> dict[str, list[tuple[str, Any]]]:
    """Collect all supplied snapshot sources across the 4 stages.

    Uses _MISSING_SENTINEL to distinguish omitted arguments from explicit None.
    Does not use milestone/checkpoint/result snapshots as fallback retry evidence.
    """
    sources: dict[str, list[tuple[str, Any]]] = {
        "pre_attempt": [],
        "candidate": [],
        "verification_before": [],
        "verification_after": [],
    }

    # pre_attempt stage: mandatory in attempt; include context if supplied
    if isinstance(attempt, dict) and "pre_attempt_snapshot" in attempt:
        sources["pre_attempt"].append(("attempt pre_attempt_snapshot", attempt["pre_attempt_snapshot"]))
    if isinstance(context, dict) and "pre_attempt_snapshot" in context:
        sources["pre_attempt"].append(("context pre_attempt_snapshot", context["pre_attempt_snapshot"]))

    # candidate stage: explicit argument, context, attempt candidate_snapshot, attempt post_attempt_snapshot
    if candidate_snapshot_arg is not _MISSING_SENTINEL:
        sources["candidate"].append(("candidate_snapshot argument", candidate_snapshot_arg))
    if isinstance(context, dict) and "candidate_snapshot" in context:
        sources["candidate"].append(("context candidate_snapshot", context["candidate_snapshot"]))
    if isinstance(attempt, dict):
        if "candidate_snapshot" in attempt:
            sources["candidate"].append(("attempt candidate_snapshot", attempt["candidate_snapshot"]))
        if "post_attempt_snapshot" in attempt:
            sources["candidate"].append(("attempt post_attempt_snapshot", attempt["post_attempt_snapshot"]))

    # verification_before stage: explicit argument, context, attempt
    if verification_snapshot_before_arg is not _MISSING_SENTINEL:
        sources["verification_before"].append(
            ("verification_snapshot_before argument", verification_snapshot_before_arg)
        )
    if isinstance(context, dict) and "verification_snapshot_before" in context:
        sources["verification_before"].append(
            ("context verification_snapshot_before", context["verification_snapshot_before"])
        )
    if isinstance(attempt, dict) and "verification_snapshot_before" in attempt:
        sources["verification_before"].append(
            ("attempt verification_snapshot_before", attempt["verification_snapshot_before"])
        )

    # verification_after stage: explicit argument, context, attempt
    if verification_snapshot_after_arg is not _MISSING_SENTINEL:
        sources["verification_after"].append(
            ("verification_snapshot_after argument", verification_snapshot_after_arg)
        )
    if isinstance(context, dict) and "verification_snapshot_after" in context:
        sources["verification_after"].append(
            ("context verification_snapshot_after", context["verification_snapshot_after"])
        )
    if isinstance(attempt, dict) and "verification_snapshot_after" in attempt:
        sources["verification_after"].append(
            ("attempt verification_snapshot_after", attempt["verification_snapshot_after"])
        )

    return sources


def _hex_digest(value: Any, lengths: tuple[int, ...] = (64,)) -> bool:
    return (type(value) is str and len(value) in lengths
            and all(c in "0123456789abcdef" for c in value))


def _safe_path_component(value: str) -> bool:
    # Windows aliases must not create a second spelling of a protected path.
    reserved = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
        f"{prefix}{number}" for prefix in ("COM", "LPT") for number in "123456789¹²³"
    }
    return (bool(value) and value not in (".", "..") and not value.endswith((".", " "))
            and not any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF
                        or c in '<>:"/\\|?*' for c in value)
            and value.split(".")[0].upper() not in reserved
            and re.fullmatch(r"[^.]{1,6}~[0-9]+(?:\..*)?", value) is None)


def _canonical_absolute_path(value: Any) -> bool:
    if type(value) is not str or not value:
        return False
    windows = PureWindowsPath(value)
    path = windows if windows.drive or "\\" in value else PurePosixPath(value)
    if not path.is_absolute() or str(path) != value:
        return False
    if isinstance(path, PureWindowsPath):
        drive = path.drive
        if drive.startswith("\\\\"):
            share = drive[2:].split("\\")
            if len(share) != 2 or not all(_safe_path_component(p) for p in share):
                return False
        elif re.fullmatch(r"[A-Za-z]:", drive) is None:
            return False
    return all(_safe_path_component(p) for p in path.parts[1:])


def _safe_snapshot_file_path(value: Any) -> bool:
    # Git emits forward-slash relative paths; do not repair alternate spellings.
    return (type(value) is str and bool(value) and "\\" not in value
            and all(_safe_path_component(p) and p.lower() != ".git" for p in value.split("/")))


def _valid_symbolic_ref(value: Any) -> bool:
    if type(value) is not str:
        return False
    if value == "":  # detached HEAD
        return True
    return (value.startswith("refs/") and not value.endswith(("/", "."))
            and not any(s in value for s in ("..", "@{", "//"))
            and not any(ord(c) < 33 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF
                        or c in '~^:?*[\\' for c in value)
            and all(p and not p.startswith(".") and not p.endswith(".lock") for p in value.split("/")))


def _valid_porcelain_status(value: Any) -> bool:
    """Parse porcelain v1 -z, including its extra source path for R/C records."""
    if type(value) is not str:
        return False
    if value == "":
        return True
    if not value.endswith("\0"):
        return False
    records = value.split("\0")[:-1]
    index = 0
    seen = set()
    while index < len(records):
        record = records[index]
        if len(record) < 4 or record[2] != " ":
            return False
        xy, name = record[:2], record[3:]
        legal = (xy == "??" or xy in ("DD", "AU", "UD", "UA", "DU", "AA", "UU")
                 or (xy[0] == " " and xy[1] in "MTADRC")
                 or (xy[0] in "MTARC" and xy[1] in " MTD") or xy == "D ")
        if not legal or not _safe_snapshot_file_path(name) or name.lower() in seen:
            return False
        seen.add(name.lower())
        index += 1
        if "R" in xy or "C" in xy:
            if index >= len(records) or not _safe_snapshot_file_path(records[index]):
                return False
            index += 1
    return True


def validate_normalize_snapshot(snap: Any, label: str) -> tuple[dict[str, Any] | None, RetryDecision | None]:
    """Validate snapshot dictionary against the canonical durable.snapshot() schema.

    Enforces strict types without bool/int coercion, validates the SHA256 digest
    fingerprint matching durable.digest, and checks file entry semantics.
    """
    if type(snap) is not dict:
        return None, _deny(DENY_MISSING_EVIDENCE, f"{label} must be a dict")

    # Closed schema validation
    snap_keys = set(snap)
    missing = CANONICAL_SNAPSHOT_FIELDS - snap_keys
    if missing:
        return None, _deny(DENY_MISSING_EVIDENCE, f"{label} missing required durable.snapshot field(s): {sorted(missing)}")
    extra = snap_keys - CANONICAL_SNAPSHOT_FIELDS
    if extra:
        return None, _deny(DENY_MISSING_EVIDENCE, f"{label} contains unexpected field(s): {sorted(extra)}")

    # Preserve producer spelling; normalization must not repair evidence.
    repo = snap["repository"]
    if not _canonical_absolute_path(repo):
        return None, _deny(DENY_MISSING_EVIDENCE, f"{label} repository must be a canonical absolute path")

    # The Git directory can legitimately be outside the repository worktree.
    git_dir = snap["git_directory"]
    if not _canonical_absolute_path(git_dir):
        return None, _deny(DENY_MISSING_EVIDENCE, f"{label} git_directory must be a canonical absolute path")

    # 3. git_identity: list of exactly two non-bool integers [st_dev, st_ino]
    git_id = snap["git_identity"]
    if (type(git_id) is not list or len(git_id) != 2
            or type(git_id[0]) is not int or isinstance(git_id[0], bool)
            or type(git_id[1]) is not int or isinstance(git_id[1], bool)
            or git_id[0] < 0 or git_id[1] < 0):
        return None, _deny(DENY_MISSING_EVIDENCE, f"{label} git_identity must be two nonnegative integers")

    # Full SHA-1 or SHA-256 Git object identity.
    head = snap["head"]
    if not _hex_digest(head, (40, 64)):
        return None, _deny(DENY_MISSING_EVIDENCE, f"{label} head must be a full lowercase Git object ID")

    # 5. branch: string (can be empty string for detached HEAD)
    branch = snap["branch"]
    if not _valid_symbolic_ref(branch):
        return None, _deny(DENY_MISSING_EVIDENCE, f"{label} branch must be a full symbolic Git ref or empty")

    # 6. status: string (can be empty string for clean working tree)
    status = snap["status"]
    if not _valid_porcelain_status(status):
        return None, _deny(DENY_MISSING_EVIDENCE, f"{label} status must be porcelain v1 -z")

    # SHA-256 of Git's complete staged-index listing.
    idx_sha = snap["index_sha256"]
    if not _hex_digest(idx_sha):
        return None, _deny(DENY_MISSING_EVIDENCE, f"{label} missing valid index_sha256")

    # 8. files: non-empty dict
    files = snap["files"]
    if type(files) is not dict:
        return None, _deny(DENY_MISSING_EVIDENCE, f"{label} files must be a dict")
    if len(files) == 0:
        return None, _deny(DENY_MISSING_EVIDENCE, f"{label} empty snapshot files dictionary is not authoritative")

    # Validate file entries
    seen_paths = set()
    for f_path, entry in files.items():
        if not _safe_snapshot_file_path(f_path) or f_path.lower() in seen_paths:
            return None, _deny(DENY_MISSING_EVIDENCE, f"{label} files contains invalid path key: {f_path!r}")
        seen_paths.add(f_path.lower())
        if entry is None:
            # Deleted tracked file, represented as None in durable.snapshot()
            continue
        if type(entry) is not dict or set(entry) != {"sha256", "mode"}:
            return None, _deny(DENY_MISSING_EVIDENCE, f"{label} malformed file entry for {f_path!r}")
        sha = entry["sha256"]
        mode = entry["mode"]
        if not _hex_digest(sha):
            return None, _deny(DENY_MISSING_EVIDENCE, f"{label} file entry {f_path!r} missing valid sha256")
        if type(mode) is not int or not 0 <= mode <= 0o7777:
            return None, _deny(DENY_MISSING_EVIDENCE, f"{label} file entry {f_path!r} invalid mode: {mode!r}")

    # 9. fingerprint: 64-hex char string, must match durable.digest calculation
    fp = snap["fingerprint"]
    if type(fp) is not str or len(fp) != 64 or any(c not in "0123456789abcdef" for c in fp):
        return None, _deny(DENY_MISSING_EVIDENCE, f"{label} fingerprint must be a 64-character lowercase hex string")

    body = {k: snap[k] for k in snap if k != "fingerprint"}
    try:
        expected_fp = _durable_digest(body)
    except Exception as exc:
        return None, _deny(DENY_MISSING_EVIDENCE, f"{label} fingerprint calculation error: {exc}")

    if fp != expected_fp:
        return None, _deny(DENY_CONTRADICTORY_EVIDENCE, f"{label} fingerprint mismatch: expected {expected_fp}, got {fp}")

    return copy.deepcopy(snap), None


def validate_stage_consistency(
    sources: list[tuple[str, Any]],
    stage_name: str,
) -> tuple[dict[str, Any] | None, RetryDecision | None]:
    """Validate all snapshot sources for a given stage and ensure pairwise exact equality.

    Every supplied source is validated; empty sources return DENY_MISSING_EVIDENCE,
    and any discrepancy returns DENY_CONTRADICTORY_EVIDENCE.
    """
    if not sources:
        return None, _deny(DENY_MISSING_EVIDENCE, f"missing authoritative {stage_name} snapshot evidence")

    validated: list[tuple[str, dict[str, Any]]] = []
    for label, raw_snap in sources:
        norm_snap, err = validate_normalize_snapshot(raw_snap, label)
        if err is not None:
            return None, err
        validated.append((label, norm_snap))

    first_label, first_snap = validated[0]
    for other_label, other_snap in validated[1:]:
        if not _type_exact_equal(first_snap, other_snap):
            return None, _deny(
                DENY_CONTRADICTORY_EVIDENCE,
                f"conflicting {stage_name} snapshot between {first_label} and {other_label}",
            )

    return first_snap, None


def validate_snapshot_transitions(
    pre_attempt: dict[str, Any],
    candidate: dict[str, Any],
    v_before: dict[str, Any],
    v_after: dict[str, Any],
) -> RetryDecision | None:
    """Validate identity invariants across all 4 snapshots and check verification integrity."""
    # 6 Identity Invariants: repository, git_directory, git_identity, head, branch, index_sha256
    stages_to_check = [
        ("candidate", candidate),
        ("verification_before", v_before),
        ("verification_after", v_after),
    ]

    for label, snap in stages_to_check:
        if snap["repository"] != pre_attempt["repository"]:
            return _deny(DENY_UNVERIFIED_MUTATIONS, f"{label} repository path mismatch with pre_attempt")
        if snap["git_directory"] != pre_attempt["git_directory"]:
            return _deny(DENY_UNVERIFIED_MUTATIONS, f"{label} git_directory mismatch with pre_attempt")
        if not _type_exact_equal(snap["git_identity"], pre_attempt["git_identity"]):
            return _deny(DENY_UNVERIFIED_MUTATIONS, f"{label} git_identity mismatch with pre_attempt")
        if snap["head"] != pre_attempt["head"]:
            return _deny(
                DENY_UNVERIFIED_MUTATIONS,
                f"{label} git HEAD drift detected: pre={pre_attempt['head']}, {label}={snap['head']}",
            )
        if snap["branch"] != pre_attempt["branch"]:
            return _deny(
                DENY_UNVERIFIED_MUTATIONS,
                f"{label} git branch drift detected: pre={pre_attempt['branch']!r}, {label}={snap['branch']!r}",
            )
        if snap["index_sha256"] != pre_attempt["index_sha256"]:
            return _deny(
                DENY_UNVERIFIED_MUTATIONS,
                f"{label} git index drift detected: pre={pre_attempt['index_sha256']}, {label}={snap['index_sha256']}",
            )

    # Verification integrity: v_before == candidate and v_after == v_before
    if not _type_exact_equal(v_before, candidate):
        return _deny(DENY_UNVERIFIED_MUTATIONS, "verification before snapshot does not match candidate snapshot")
    if not _type_exact_equal(v_after, v_before):
        return _deny(
            DENY_UNVERIFIED_MUTATIONS,
            "verification after snapshot does not match verification before snapshot (repo mutated during test execution)",
        )

    return None


def evaluate_scope_mutations(
    pre_snap: dict[str, Any],
    candidate_snap: dict[str, Any],
    spec: dict[str, Any],
    attempt: dict[str, Any],
) -> RetryDecision | None:
    """Evaluate file mutations against unit scope, forbidden/frozen paths, and changed_paths.

    Handles file additions, deletions, edits, restorations, and mode changes.
    """
    changed_paths = attempt.get("changed_paths")
    if changed_paths is None or not isinstance(changed_paths, (list, tuple, set)):
        return _deny(DENY_MISSING_EVIDENCE, "missing or invalid changed_paths in attempt")

    for p in changed_paths:
        if not isinstance(p, str) or not p.strip():
            return _deny(DENY_MISSING_EVIDENCE, f"malformed path in changed_paths: {p!r}")

    mode = spec.get("mode")
    if mode not in ("mutation", "read_only"):
        return _deny(DENY_MISSING_EVIDENCE, f"invalid unit mode: {mode!r}")

    scope = spec.get("scope")
    if not isinstance(scope, dict):
        return _deny(DENY_MISSING_EVIDENCE, "spec missing scope dict")
    allowed_paths = scope.get("allowed_paths")
    forbidden_paths = scope.get("forbidden_paths")
    if not isinstance(allowed_paths, list):
        return _deny(DENY_MISSING_EVIDENCE, "scope.allowed_paths must be a list")
    if not isinstance(forbidden_paths, list):
        return _deny(DENY_MISSING_EVIDENCE, "scope.forbidden_paths must be a list")

    for a in allowed_paths:
        if not isinstance(a, str):
            return _deny(DENY_MISSING_EVIDENCE, f"scope.allowed_paths contains non-string: {a!r}")
    for f in forbidden_paths:
        if not isinstance(f, str):
            return _deny(DENY_MISSING_EVIDENCE, f"scope.forbidden_paths contains non-string: {f!r}")

    pre_files = pre_snap["files"]
    post_files = candidate_snap["files"]

    # Frozen/forbidden paths protection and coverage
    for f in forbidden_paths:
        if not f.endswith("/"):
            norm_f = f.replace("\\", "/").lower()
            pre_match = [k for k in pre_files if k.replace("\\", "/").lower() == norm_f]
            post_match = [k for k in post_files if k.replace("\\", "/").lower() == norm_f]
            if not pre_match or not post_match:
                return _deny(DENY_MISSING_EVIDENCE, f"missing snapshot coverage for frozen/forbidden path: {f!r}")
            if pre_files[pre_match[0]] is None or post_files[post_match[0]] is None:
                return _deny(DENY_MISSING_EVIDENCE, f"required frozen file is missing: {f!r}")
            if not _type_exact_equal(pre_files[pre_match[0]], post_files[post_match[0]]):
                return _deny(DENY_SCOPE_VIOLATION, f"frozen/forbidden path mutated in snapshot: {f!r}")
        else:
            for k in set(pre_files) | set(post_files):
                if work_units.covers(f, k):
                    if ((k in pre_files and pre_files[k] is None)
                            or (k in post_files and post_files[k] is None)):
                        return _deny(DENY_MISSING_EVIDENCE, f"required frozen file is missing: {k!r}")
                    if not _type_exact_equal(pre_files.get(k, _ABSENT_SENTINEL), post_files.get(k, _ABSENT_SENTINEL)):
                        return _deny(DENY_SCOPE_VIOLATION, f"frozen/forbidden directory mutated in snapshot: {k!r}")

    # Calculate actual mutated files using presence sentinels
    all_keys = set(pre_files) | set(post_files)
    mutated_files = set()
    for k in all_keys:
        if not _type_exact_equal(pre_files.get(k, _ABSENT_SENTINEL), post_files.get(k, _ABSENT_SENTINEL)):
            mutated_files.add(k.replace("\\", "/").lower())

    # Mode and allowed scope enforcement
    if mode == "read_only":
        if len(changed_paths) > 0:
            return _deny(DENY_SCOPE_VIOLATION, f"read_only unit changed files: {list(changed_paths)}")
        if len(mutated_files) > 0:
            return _deny(DENY_SCOPE_VIOLATION, f"read_only unit mutated files in snapshot: {sorted(mutated_files)}")
    else:
        for path in changed_paths:
            norm = path.replace("\\", "/").lower()
            if norm == ".git" or norm.startswith(".git/"):
                return _deny(DENY_SCOPE_VIOLATION, f"changed path touches git metadata: {path}")
            if any(work_units.covers(f, path) for f in forbidden_paths):
                return _deny(DENY_SCOPE_VIOLATION, f"changed path touches forbidden path: {path}")
            if not any(work_units.covers(a, path) for a in allowed_paths):
                return _deny(DENY_SCOPE_VIOLATION, f"changed path outside allowed scope: {path}")

    # Exact delta agreement with changed_paths
    norm_changed_paths = {p.replace("\\", "/").lower() for p in changed_paths}

    unrecorded = mutated_files - norm_changed_paths
    if unrecorded:
        return _deny(DENY_UNVERIFIED_MUTATIONS, f"snapshot mutated file(s) not in changed_paths: {sorted(unrecorded)}")

    unreflected = norm_changed_paths - mutated_files
    if unreflected:
        return _deny(DENY_CONTRADICTORY_EVIDENCE, f"changed_paths claimed mutation but snapshot files identical for: {sorted(unreflected)}")

    return None


def classify_retry_eligibility(
    unit: dict[str, Any],
    *,
    remaining_unit_budget: float | None = None,
    remaining_task_budget: float | None = None,
    min_budget_seconds: float = 1.0,
    supervisor: Any = None,
    candidate_snapshot: Any = _MISSING_SENTINEL,
    verification_snapshot_before: Any = _MISSING_SENTINEL,
    verification_snapshot_after: Any = _MISSING_SENTINEL,
    repo_mutated_during_verification: Any = _MISSING_SENTINEL,
    environment_status: str | dict[str, Any] | None = None,
    verifier_registry: dict[str, Any] | None = None,
    require_idle: bool = False,
    context: dict[str, Any] | None = None,
    policy: str | None = None,
) -> RetryDecision:
    """Classify whether a unit attempt is eligible for deterministic retry.

    Default decision is DENY. ALLOW_VERIFIER_FAILURE is granted only if
    all integrity, scope, process, budget, and verifier conditions pass.
    """
    try:
        return _classify_internal(
            unit,
            remaining_unit_budget=remaining_unit_budget,
            remaining_task_budget=remaining_task_budget,
            min_budget_seconds=min_budget_seconds,
            supervisor=supervisor,
            candidate_snapshot=candidate_snapshot,
            verification_snapshot_before=verification_snapshot_before,
            verification_snapshot_after=verification_snapshot_after,
            repo_mutated_during_verification=repo_mutated_during_verification,
            environment_status=environment_status,
            verifier_registry=verifier_registry,
            require_idle=require_idle,
            context=context,
            policy=policy,
        )
    except Exception as exc:
        return _deny(DENY_DEFAULT, f"Malformed input error: {type(exc).__name__}: {exc}")


def _classify_internal(
    unit: dict[str, Any],
    *,
    remaining_unit_budget: float | None = None,
    remaining_task_budget: float | None = None,
    min_budget_seconds: float = 1.0,
    supervisor: Any = None,
    candidate_snapshot: Any = _MISSING_SENTINEL,
    verification_snapshot_before: Any = _MISSING_SENTINEL,
    verification_snapshot_after: Any = _MISSING_SENTINEL,
    repo_mutated_during_verification: Any = _MISSING_SENTINEL,
    environment_status: str | dict[str, Any] | None = None,
    verifier_registry: dict[str, Any] | None = None,
    require_idle: bool = False,
    context: dict[str, Any] | None = None,
    policy: str | None = None,
) -> RetryDecision:
    explicit_candidate_snapshot = candidate_snapshot
    explicit_v_before = verification_snapshot_before
    explicit_v_after = verification_snapshot_after

    # 0. Validate min_budget_seconds parameter strictly
    if type(min_budget_seconds) not in (int, float) or isinstance(min_budget_seconds, bool):
        return _deny(DENY_INSUFFICIENT_BUDGET, f"min_budget_seconds must be a number, got {type(min_budget_seconds).__name__}")
    if not math.isfinite(min_budget_seconds):
        return _deny(DENY_INSUFFICIENT_BUDGET, "min_budget_seconds must be finite")
    if min_budget_seconds <= 0:
        return _deny(DENY_INSUFFICIENT_BUDGET, f"min_budget_seconds must be positive, got {min_budget_seconds}")

    # 6. Preserve explicit Policy B opt-in via trusted caller parameter only.
    # 7. Preserve original default/Policy A classifier semantics.
    # Generic spec.policy metadata must not silently alter the legacy decision.
    if policy is not None and policy not in SUPPORTED_POLICIES:
        return _deny(DENY_UNKNOWN_FAILURE, f"unsupported retry policy: {policy!r}")
    is_policy_b = policy in ("B", "policy_b", "POLICY_B", "bounded_recovery")

    if context:
        if remaining_unit_budget is None:
            remaining_unit_budget = context.get("remaining_unit_budget")
        if remaining_task_budget is None:
            remaining_task_budget = context.get("remaining_task_budget")
        if supervisor is None:
            supervisor = context.get("supervisor")
        if not is_policy_b:
            if candidate_snapshot is _MISSING_SENTINEL or candidate_snapshot is None:
                candidate_snapshot = context.get("candidate_snapshot")
            if verification_snapshot_before is _MISSING_SENTINEL or verification_snapshot_before is None:
                verification_snapshot_before = context.get("verification_snapshot_before")
            if verification_snapshot_after is _MISSING_SENTINEL or verification_snapshot_after is None:
                verification_snapshot_after = context.get("verification_snapshot_after")
        if repo_mutated_during_verification is _MISSING_SENTINEL or (
                not is_policy_b and repo_mutated_during_verification is None):
            repo_mutated_during_verification = context.get("repo_mutated_during_verification", _MISSING_SENTINEL)
        if environment_status is None:
            environment_status = context.get("environment_status")
        if verifier_registry is None:
            verifier_registry = context.get("verifier_registry")
        if not require_idle and "require_idle" in context:
            require_idle = bool(context["require_idle"])
        # Requirement 9: Remove implicit selection through mutable spec/context fields.
        # Policy B requires explicit trusted controller opt-in; context["policy"] is ignored.

    if not is_policy_b:
        if repo_mutated_during_verification is _MISSING_SENTINEL:
            repo_mutated_during_verification = None
        if candidate_snapshot is _MISSING_SENTINEL:
            candidate_snapshot = None
        if verification_snapshot_before is _MISSING_SENTINEL:
            verification_snapshot_before = None
        if verification_snapshot_after is _MISSING_SENTINEL:
            verification_snapshot_after = None

    # 1. Structure validation
    if not isinstance(unit, dict):
        return _deny(DENY_MISSING_EVIDENCE, "unit must be a dict")

    # Validate WorkUnit state against scheduler transitions
    status = unit.get("status")
    if status in ("UNIT_FAILED", "UNIT_VERIFIED"):
        return _deny(DENY_ATTEMPTS_EXHAUSTED, f"unit is in terminal state {status}")
    if status not in VALID_RETRY_STATUSES:
        return _deny(DENY_MISSING_EVIDENCE, f"unit status {status!r} is not eligible for retry transition (expected {VALID_RETRY_STATUSES})")

    spec = unit.get("spec")
    attempts = unit.get("attempts")
    if not isinstance(spec, dict) or not isinstance(attempts, list):
        return _deny(DENY_MISSING_EVIDENCE, "unit missing valid spec or attempts list")

    # 2. Spec limits & attempt counts (Strict: max_attempts must be exactly 2)
    limits = spec.get("limits")
    if not isinstance(limits, dict):
        return _deny(DENY_MISSING_EVIDENCE, "spec missing limits dict")

    max_attempts = limits.get("max_attempts")
    if type(max_attempts) is not int or isinstance(max_attempts, bool):
        return _deny(DENY_MISSING_EVIDENCE, "limits.max_attempts must be an integer")
    if max_attempts != 2:
        return _deny(DENY_ATTEMPTS_EXHAUSTED, f"limits.max_attempts must be exactly 2 for bounded retry, got {max_attempts}")

    if len(attempts) == 0:
        return _deny(DENY_MISSING_EVIDENCE, "no attempts recorded")

    if len(attempts) >= max_attempts:
        return _deny(DENY_ATTEMPTS_EXHAUSTED, f"attempts ({len(attempts)}) >= max_attempts ({max_attempts})")

    if len(attempts) != 1:
        return _deny(DENY_ATTEMPTS_EXHAUSTED, f"attempt count is {len(attempts)}; only attempt 1 can retry")

    # 3. Attempt 1 examination
    att = attempts[0]
    if not isinstance(att, dict):
        return _deny(DENY_MISSING_EVIDENCE, "attempt 1 is not a dict")

    outcome = att.get("outcome")
    if outcome == "timeout":
        return _deny(DENY_TIMEOUT, "attempt outcome is timeout")
    if outcome in ("success", "passed"):
        return _deny(DENY_CONTRADICTORY_EVIDENCE, f"attempt outcome is {outcome!r}, cannot retry passed attempt")
    if outcome != "failed":
        return _deny(DENY_UNKNOWN_FAILURE, f"attempt outcome is {outcome!r}, expected 'failed'")

    fail_class = att.get("failure_classification")
    if not fail_class:
        return _deny(DENY_UNKNOWN_FAILURE, "missing failure_classification")
    if fail_class in ("executor_timeout", "unit_timeout", "verifier_timeout", "timeout"):
        return _deny(DENY_TIMEOUT, f"failure classification is {fail_class}")
    if fail_class in ("scope_violation", "unexpected_write"):
        return _deny(DENY_SCOPE_VIOLATION, f"failure classification is {fail_class}")
    if fail_class in ("verifier_tampered_repo", "git_error"):
        return _deny(DENY_UNVERIFIED_MUTATIONS, f"failure classification is {fail_class}")
    if fail_class in ("containment_failed", "cleanup_failed"):
        return _deny(DENY_PROCESS_NOT_IDLE, f"failure classification is {fail_class}")
    if fail_class != "verification_failed":
        return _deny(DENY_UNKNOWN_FAILURE, f"unsupported failure classification: {fail_class}")

    # 4. Mandatory verifier registry binding
    if verifier_registry is None or not isinstance(verifier_registry, dict):
        return _deny(DENY_MISSING_EVIDENCE, "missing mandatory controller verifier_registry")

    verifier_result = att.get("verifier_result")
    if not isinstance(verifier_result, dict) or not verifier_result:
        return _deny(DENY_MISSING_EVIDENCE, "missing or empty verifier_result")

    verifier_ids = spec.get("verifier_ids")
    if not isinstance(verifier_ids, list) or not verifier_ids:
        return _deny(DENY_MISSING_EVIDENCE, "spec missing verifier_ids")

    # Controller's expected invocation identity (Requirement 5)
    expected_unit_id = unit.get("unit_id")
    if type(expected_unit_id) is not str or not expected_unit_id:
        return _deny(DENY_MISSING_EVIDENCE, "unit missing valid unit_id")

    expected_attempt_id = att.get("attempt_id")
    if type(expected_attempt_id) is not str or not expected_attempt_id:
        return _deny(DENY_MISSING_EVIDENCE, "attempt missing valid attempt_id")

    # Authoritative run_id resolution
    sup_store_run = getattr(getattr(supervisor, "store", None), "state", {}).get("run_id") if hasattr(supervisor, "store") else None
    unit_run = unit.get("run_id")
    ctx_run = context.get("run_id") if context else None

    run_candidates = [r for r in (sup_store_run, unit_run, ctx_run) if r is not None]
    if len(set(run_candidates)) > 1:
        return _deny(DENY_CONTRADICTORY_EVIDENCE, f"conflicting run_id across controller sources: store={sup_store_run!r}, unit={unit_run!r}, context={ctx_run!r}")

    expected_run_id = run_candidates[0] if run_candidates else None
    if is_policy_b and (expected_run_id is None or type(expected_run_id) is not str or not expected_run_id):
        return _deny(DENY_MISSING_EVIDENCE, "Policy B requires authoritative run_id to establish invocation identity")

    # Policy B and verifier process provenance check
    reconciled_children_by_id: dict[str, dict[str, Any]] | None = None
    cached_supervisor_children: list[dict[str, Any]] | None = None
    requires_supervisor_check = is_policy_b or any(
        isinstance(v, dict) and ("child_id" in v or "controller_evidence" in v) for v in verifier_result.values()
    )

    if requires_supervisor_check:
        if supervisor is None:
            return _deny(DENY_MISSING_EVIDENCE, "missing authoritative supervisor instance")
        if not hasattr(supervisor, "reconcile") or not callable(supervisor.reconcile):
            return _deny(DENY_MISSING_EVIDENCE, "supervisor missing callable reconcile()")
        if not hasattr(supervisor, "require_idle") or not callable(supervisor.require_idle):
            return _deny(DENY_MISSING_EVIDENCE, "supervisor missing callable require_idle()")
        try:
            cached_supervisor_children = supervisor.reconcile()
        except Exception as exc:
            return _deny(DENY_PROCESS_NOT_IDLE, f"supervisor reconcile failed: {exc}")
        if not isinstance(cached_supervisor_children, list):
            return _deny(DENY_MISSING_EVIDENCE, "supervisor reconcile() must return a list")
        reconciled_children_by_id = {}
        for c in cached_supervisor_children:
            if not isinstance(c, dict):
                return _deny(DENY_MISSING_EVIDENCE, "supervisor child must be a dict")
            cid = c.get("child_id")
            if not isinstance(cid, str) or not cid:
                return _deny(DENY_MISSING_EVIDENCE, "supervisor child missing valid child_id")
            # Fix 1: Reject duplicate child IDs in ANY relevant supervisor/evidence collection.
            # Never silently overwrite duplicates using dictionary construction.
            if cid in reconciled_children_by_id:
                return _deny(DENY_CONTRADICTORY_EVIDENCE, f"duplicate child_id in supervisor records: {cid!r}")
            reconciled_children_by_id[cid] = c

    canonical_evidence = None
    if is_policy_b:
        canonical_evidence = _validate_policy_b_evidence(
            verifier_ids, verifier_result, att, context, reconciled_children_by_id,
        )
        if isinstance(canonical_evidence, RetryDecision):
            return canonical_evidence

    failed_vids: list[str] = []
    for vid in verifier_ids:
        if not isinstance(vid, str) or not vid.strip():
            return _deny(DENY_MISSING_EVIDENCE, f"invalid verifier id: {vid!r}")
        if vid not in verifier_registry:
            return _deny(DENY_MISSING_EVIDENCE, f"verifier {vid!r} missing from controller verifier_registry")

        defn = verifier_registry[vid]
        if not isinstance(defn, dict) or "argv" not in defn:
            return _deny(DENY_MISSING_EVIDENCE, f"malformed registry definition for verifier {vid!r}")
        expected_argv = defn["argv"]
        if not isinstance(expected_argv, list) or len(expected_argv) == 0:
            return _deny(DENY_MISSING_EVIDENCE, f"registry definition for {vid!r} has invalid argv")

        if vid not in verifier_result:
            return _deny(DENY_MISSING_EVIDENCE, f"verifier {vid!r} missing from verifier_result")
        v_entry = verifier_result[vid]
        if not isinstance(v_entry, dict):
            return _deny(DENY_MISSING_EVIDENCE, f"verifier {vid!r} entry is not a dict")
        if "exit_code" not in v_entry or "passed" not in v_entry or "argv" not in v_entry:
            return _deny(DENY_MISSING_EVIDENCE, f"verifier {vid!r} missing exit_code, passed, or argv")

        exit_code = v_entry["exit_code"]
        v_passed = v_entry["passed"]
        argv = v_entry["argv"]

        # Strict type checks: exit_code must be int (not bool), passed must be bool
        if type(exit_code) is not int or isinstance(exit_code, bool):
            return _deny(DENY_MISSING_EVIDENCE, f"verifier {vid!r} exit_code must be an integer, got {type(exit_code).__name__}")
        if type(v_passed) is not bool:
            return _deny(DENY_MISSING_EVIDENCE, f"verifier {vid!r} passed must be a boolean, got {type(v_passed).__name__}")

        # Exact argv binding against registry; no permissive fallback
        if argv != expected_argv:
            return _deny(DENY_CONTRADICTORY_EVIDENCE, f"verifier {vid!r} argv does not match registry definition")

        # Contradictory check
        if exit_code == 0 and not v_passed:
            return _deny(DENY_CONTRADICTORY_EVIDENCE, f"verifier {vid!r} has exit_code 0 but passed=False")
        if exit_code != 0 and v_passed:
            return _deny(DENY_CONTRADICTORY_EVIDENCE, f"verifier {vid!r} has exit_code {exit_code} but passed=True")

        if exit_code != 0:
            if exit_code < 0 or exit_code in (137, 143):
                return _deny(DENY_UNKNOWN_FAILURE, f"verifier {vid!r} exited with signal/kill {exit_code}")
            failed_vids.append(vid)

        if is_policy_b:
            evidence = canonical_evidence[vid]
        else:
            # Preserve Policy A source-selection and compatibility behavior.
            v_ev = v_entry.get("controller_evidence")
            att_ev = None
            if isinstance(att.get("controller_evidence"), dict):
                att_ev = att["controller_evidence"].get(vid)
            ctx_ev = None
            if context and isinstance(context.get("controller_evidence"), dict):
                ctx_ev = context["controller_evidence"].get(vid)
            available_ev = [(src, ev) for src, ev in (("v_entry", v_ev), ("attempt", att_ev), ("context", ctx_ev)) if ev is not None]
            if len(available_ev) > 1:
                first_src, first_ev = available_ev[0]
                for other_src, other_ev in available_ev[1:]:
                    if first_ev != other_ev:
                        return _deny(DENY_CONTRADICTORY_EVIDENCE, f"conflicting controller_evidence between {first_src} and {other_src} for verifier {vid!r}")
            evidence = available_ev[0][1] if available_ev else None

        if is_policy_b:
            # Structural/source consistency and uniqueness passed the boundary.
            # Preserve the existing invocation binding and eligibility gates.
            if type(evidence["source"]) is not str or evidence["source"] != "controller_native_handles":
                return _deny(DENY_MISSING_EVIDENCE, f"verifier {vid!r} controller_evidence source must be 'controller_native_handles', got {evidence['source']!r}")

            # Reject worker/broker receipts
            if v_entry.get("descendant_cleanup") is not None:
                return _deny(DENY_CONTRADICTORY_EVIDENCE, f"verifier {vid!r} contains worker descendant_cleanup receipt; broker/worker receipt rejected")
            if evidence["cleanup"] == "job_close":
                return _deny(DENY_CONTRADICTORY_EVIDENCE, f"verifier {vid!r} cleanup 'job_close' is a worker receipt, not controller-confirmed Job cleanup")

            # execution_started must be boolean True
            if type(evidence["execution_started"]) is not bool or evidence["execution_started"] is not True:
                return _deny(DENY_UNKNOWN_FAILURE, f"verifier {vid!r} execution_started must be boolean True")

            # Child ID validation
            child_id = evidence["child_id"]
            if not isinstance(child_id, str) or not child_id:
                return _deny(DENY_MISSING_EVIDENCE, f"verifier {vid!r} controller_evidence missing child_id")
            if not child_id.startswith("verification_command-"):
                return _deny(DENY_MISSING_EVIDENCE, f"verifier {vid!r} invalid child_id format in controller_evidence: {child_id!r}")
            if "child_id" in v_entry and v_entry["child_id"] != child_id:
                return _deny(DENY_CONTRADICTORY_EVIDENCE, f"verifier {vid!r} child_id mismatch between v_entry ({v_entry['child_id']!r}) and controller_evidence ({child_id!r})")

            sup_child = (
                reconciled_children_by_id.get(child_id)
                if (reconciled_children_by_id is not None)
                else None
            )
            if sup_child is None:
                return _deny(DENY_MISSING_EVIDENCE, f"verifier {vid!r} references child_id {child_id!r} not found in supervisor records")
            if sup_child.get("purpose") != "verification_command":
                return _deny(DENY_CONTRADICTORY_EVIDENCE, f"verifier {vid!r} supervisor child purpose is {sup_child.get('purpose')!r}, expected 'verification_command'")

            c_state = sup_child.get("state")
            if c_state != "EXITED":
                return _deny(DENY_PROCESS_NOT_IDLE, f"verifier {vid!r} supervisor child {child_id!r} in state {c_state!r}, expected 'EXITED'")

            # Exit code validation across v_entry, controller_evidence, and sup_child
            c_exit = sup_child.get("exit_code")
            e_exit = evidence["observed_exit_code"]
            if type(c_exit) is not int or isinstance(c_exit, bool):
                return _deny(DENY_MISSING_EVIDENCE, f"verifier {vid!r} supervisor child {child_id!r} missing integer exit_code")
            if type(e_exit) is not int or isinstance(e_exit, bool):
                return _deny(DENY_MISSING_EVIDENCE, f"verifier {vid!r} controller_evidence missing integer observed_exit_code")
            if c_exit != exit_code:
                return _deny(DENY_CONTRADICTORY_EVIDENCE, f"verifier {vid!r} exit_code {exit_code} contradicts supervisor child exit_code {c_exit}")
            if e_exit != exit_code:
                return _deny(DENY_CONTRADICTORY_EVIDENCE, f"verifier {vid!r} exit_code {exit_code} contradicts controller_evidence observed_exit_code {e_exit}")

            # Verifier process identity validation
            v_proc = evidence["verifier"]
            if not isinstance(v_proc, dict):
                return _deny(DENY_MISSING_EVIDENCE, f"verifier {vid!r} controller_evidence missing verifier process dict")
            v_pid = v_proc.get("pid")
            v_ident = v_proc.get("identity")
            if type(v_pid) is not int or isinstance(v_pid, bool) or v_pid <= 0:
                return _deny(DENY_MISSING_EVIDENCE, f"verifier {vid!r} controller_evidence invalid verifier pid: {v_pid!r}")
            if not isinstance(v_ident, dict) or type(v_ident.get("created")) is not str or not v_ident["created"] or type(v_ident.get("executable")) is not str or not v_ident["executable"]:
                return _deny(DENY_MISSING_EVIDENCE, f"verifier {vid!r} controller_evidence invalid verifier identity")

            s_pid = sup_child.get("pid")
            s_ident = sup_child.get("identity")
            if s_pid != v_pid:
                return _deny(DENY_CONTRADICTORY_EVIDENCE, f"verifier {vid!r} pid mismatch: supervisor={s_pid}, controller_evidence={v_pid}")
            if s_ident != v_ident:
                return _deny(DENY_CONTRADICTORY_EVIDENCE, f"verifier {vid!r} identity mismatch between supervisor and controller_evidence")

            # Validate intended and resolved argv digests independently
            intended_argv = evidence["intended_argv"]
            intended_sha = evidence["intended_command_sha256"]
            resolved_argv = evidence["resolved_argv"]
            resolved_sha = evidence["resolved_command_sha256"]

            if not isinstance(intended_argv, list) or len(intended_argv) == 0:
                return _deny(DENY_MISSING_EVIDENCE, f"verifier {vid!r} controller_evidence missing intended_argv")
            if intended_argv != expected_argv:
                return _deny(DENY_CONTRADICTORY_EVIDENCE, f"verifier {vid!r} intended_argv does not match registry definition")
            if intended_sha != _checksum(expected_argv):
                return _deny(DENY_CONTRADICTORY_EVIDENCE, f"verifier {vid!r} intended_command_sha256 mismatch")

            if not isinstance(resolved_argv, list) or len(resolved_argv) == 0:
                return _deny(DENY_MISSING_EVIDENCE, f"verifier {vid!r} controller_evidence missing resolved_argv")
            if resolved_sha != _checksum(resolved_argv):
                return _deny(DENY_CONTRADICTORY_EVIDENCE, f"verifier {vid!r} resolved_command_sha256 mismatch with resolved_argv")

            c_cmd_hash = sup_child.get("command_sha256")
            if c_cmd_hash != resolved_sha:
                return _deny(DENY_CONTRADICTORY_EVIDENCE, f"verifier {vid!r} command_sha256 mismatch between supervisor child and controller_evidence resolved_command_sha256")

            if len(resolved_argv) != len(intended_argv) or resolved_argv[1:] != intended_argv[1:]:
                return _deny(DENY_CONTRADICTORY_EVIDENCE, f"verifier {vid!r} resolved_argv arguments disagree with intended_argv")

            s_executable = sup_child.get("executable")
            if not s_executable or _norm_path(resolved_argv[0]) != _norm_path(s_executable):
                return _deny(DENY_CONTRADICTORY_EVIDENCE, f"verifier {vid!r} resolved_argv executable mismatch with supervisor child executable")
            if _norm_path(v_ident["executable"]) != _norm_path(s_executable):
                return _deny(DENY_CONTRADICTORY_EVIDENCE, f"verifier {vid!r} verifier identity executable mismatch with supervisor child executable")

            # Fix 2: Reject contradictions across ALL verifier-result, controller-evidence and supervisor termination fields.
            # A flat termination="forced" must deny even when nested evidence says natural.
            v_term = evidence["termination"]
            s_term = sup_child.get("termination")
            flat_term = v_entry.get("termination")
            att_term = att.get("termination")

            all_terms = [t for t in (v_term, s_term, flat_term, att_term) if t is not None]
            if any(t in ("forced", "timeout") for t in all_terms):
                return _deny(DENY_TIMEOUT, f"verifier {vid!r} process was forcefully terminated (timeout)")
            if len(set(all_terms)) > 1:
                return _deny(DENY_CONTRADICTORY_EVIDENCE, f"verifier {vid!r} conflicting termination fields: v_entry={flat_term!r}, evidence={v_term!r}, supervisor={s_term!r}")

            if s_term is None:
                return _deny(DENY_MISSING_EVIDENCE, f"Policy B missing termination in supervisor child for verifier {vid!r}")
            if v_term != "natural" or s_term != "natural":
                return _deny(DENY_UNKNOWN_FAILURE, f"verifier {vid!r} abnormal termination: evidence={v_term!r}, supervisor={s_term!r}")

            v_term_basis = evidence["termination_basis"]
            if v_term_basis != "signaled_without_controller_termination":
                return _deny(DENY_UNKNOWN_FAILURE, f"verifier {vid!r} abnormal termination_basis: {v_term_basis!r}")

            # Controller-confirmed Job cleanup
            cleanup = evidence["cleanup"]
            if cleanup != "observed_empty":
                return _deny(DENY_PROCESS_NOT_IDLE, f"verifier {vid!r} descendant cleanup incomplete: {cleanup!r}")
            if "cleanup_error" in evidence:
                return _deny(DENY_PROCESS_NOT_IDLE, f"verifier {vid!r} cleanup error: {evidence['cleanup_error']!r}")

            if sup_child.get("containment") != "job":
                return _deny(DENY_PROCESS_NOT_IDLE, f"verifier {vid!r} supervisor child containment is {sup_child.get('containment')!r}, expected 'job'")
            s_job = sup_child.get("job")
            e_job = evidence["job"]
            if not s_job or s_job != e_job:
                return _deny(DENY_CONTRADICTORY_EVIDENCE, f"verifier {vid!r} job name mismatch between supervisor ({s_job!r}) and controller_evidence ({e_job!r})")

            # Fix 5: Prevent missing run/unit/attempt identifiers from acting as wildcards.
            # Require binding to the controller's expected invocation identity.
            # Never infer trusted freshness merely because two untrusted dictionaries contain matching IDs.
            e_run = evidence["run_id"]
            if type(e_run) is not str or not e_run:
                return _deny(DENY_MISSING_EVIDENCE, f"verifier {vid!r} controller_evidence missing valid run_id")
            if e_run != expected_run_id:
                return _deny(DENY_CONTRADICTORY_EVIDENCE, f"verifier {vid!r} controller_evidence run_id {e_run!r} contradicts expected run_id {expected_run_id!r}")

            s_run = sup_child.get("run_id")
            if type(s_run) is not str or not s_run:
                return _deny(DENY_MISSING_EVIDENCE, f"verifier {vid!r} supervisor child missing valid run_id")
            if s_run != expected_run_id:
                return _deny(DENY_CONTRADICTORY_EVIDENCE, f"verifier {vid!r} supervisor child run_id {s_run!r} contradicts expected run_id {expected_run_id!r}")

            e_uid = evidence["unit_id"]
            if type(e_uid) is not str or not e_uid:
                return _deny(DENY_MISSING_EVIDENCE, f"verifier {vid!r} controller_evidence missing valid unit_id")
            if e_uid != expected_unit_id:
                return _deny(DENY_CONTRADICTORY_EVIDENCE, f"verifier {vid!r} controller_evidence unit_id {e_uid!r} contradicts expected unit_id {expected_unit_id!r}")

            s_uid = sup_child.get("unit_id")
            if s_uid is not None and (type(s_uid) is not str or s_uid != expected_unit_id):
                return _deny(DENY_CONTRADICTORY_EVIDENCE, f"verifier {vid!r} supervisor child unit_id {s_uid!r} contradicts expected unit_id {expected_unit_id!r}")

            e_att = evidence["attempt_id"]
            if type(e_att) is not str or not e_att:
                return _deny(DENY_MISSING_EVIDENCE, f"verifier {vid!r} controller_evidence missing valid attempt_id")
            if e_att != expected_attempt_id:
                return _deny(DENY_CONTRADICTORY_EVIDENCE, f"verifier {vid!r} controller_evidence attempt_id {e_att!r} contradicts expected attempt_id {expected_attempt_id!r}")

            s_att = sup_child.get("attempt_id")
            if s_att is not None and (type(s_att) is not str or s_att != expected_attempt_id):
                return _deny(DENY_CONTRADICTORY_EVIDENCE, f"verifier {vid!r} supervisor child attempt_id {s_att!r} contradicts expected attempt_id {expected_attempt_id!r}")
        else:
            # Policy A (M0): If child_id or controller_evidence is present, ensure no contradiction with supervisor
            child_id = (evidence.get("child_id") if evidence else None) or v_entry.get("child_id")
            if child_id is not None and reconciled_children_by_id is not None:
                sup_child = reconciled_children_by_id.get(child_id)
                if sup_child is None:
                    return _deny(DENY_MISSING_EVIDENCE, f"verifier {vid!r} references child_id {child_id!r} not found in supervisor records")
                c_state = sup_child.get("state")
                if c_state != "EXITED":
                    return _deny(DENY_PROCESS_NOT_IDLE, f"verifier {vid!r} supervisor child {child_id!r} in state {c_state!r}, expected 'EXITED'")
                c_exit = sup_child.get("exit_code")
                if c_exit != exit_code:
                    return _deny(DENY_CONTRADICTORY_EVIDENCE, f"verifier {vid!r} exit_code {exit_code} contradicts supervisor child exit_code {c_exit}")

    if not failed_vids:
        return _deny(DENY_CONTRADICTORY_EVIDENCE, "all verifiers passed with exit_code 0, but attempt marked failed")

    if is_policy_b:
        # Policy B Unified Snapshot Boundary
        if "pre_attempt_snapshot" not in att:
            return _deny(DENY_MISSING_EVIDENCE, "missing mandatory attempt pre_attempt_snapshot")
        sources = collect_snapshot_sources(
            unit,
            attempt=att,
            context=context,
            candidate_snapshot_arg=explicit_candidate_snapshot,
            verification_snapshot_before_arg=explicit_v_before,
            verification_snapshot_after_arg=explicit_v_after,
        )

        pre_snap, err = validate_stage_consistency(sources["pre_attempt"], "pre_attempt")
        if err is not None:
            return err

        cand_snap, err = validate_stage_consistency(sources["candidate"], "candidate")
        if err is not None:
            return err

        v_before_snap, err = validate_stage_consistency(sources["verification_before"], "verification_before")
        if err is not None:
            return err

        v_after_snap, err = validate_stage_consistency(sources["verification_after"], "verification_after")
        if err is not None:
            return err

        if repo_mutated_during_verification is True:
            return _deny(DENY_UNVERIFIED_MUTATIONS, "repository mutated during verification")
        if repo_mutated_during_verification is not _MISSING_SENTINEL and repo_mutated_during_verification is not False:
            return _deny(DENY_MISSING_EVIDENCE, "mutation flag must be exact False when supplied")
        if context is not None and "repo_mutated_during_verification" in context:
            if context["repo_mutated_during_verification"] is not False:
                return _deny(DENY_CONTRADICTORY_EVIDENCE, "context mutation flag must be exact False")

        trans_err = validate_snapshot_transitions(pre_snap, cand_snap, v_before_snap, v_after_snap)
        if trans_err is not None:
            return trans_err

        scope_err = evaluate_scope_mutations(pre_snap, cand_snap, spec, att)
        if scope_err is not None:
            return scope_err

        changed_paths = att.get("changed_paths")
    else:
        # 5. Scope & Changed Paths check (Policy A)
        changed_paths = att.get("changed_paths")
        if changed_paths is None or not isinstance(changed_paths, (list, tuple, set)):
            return _deny(DENY_MISSING_EVIDENCE, "missing or invalid changed_paths in attempt")

        for p in changed_paths:
            if not isinstance(p, str) or not p.strip():
                return _deny(DENY_MISSING_EVIDENCE, f"malformed path in changed_paths: {p!r}")

        mode = spec.get("mode")
        if mode not in ("mutation", "read_only"):
            return _deny(DENY_MISSING_EVIDENCE, f"invalid unit mode: {mode!r}")

        scope = spec.get("scope")
        if not isinstance(scope, dict):
            return _deny(DENY_MISSING_EVIDENCE, "spec missing scope dict")
        allowed_paths = scope.get("allowed_paths")
        forbidden_paths = scope.get("forbidden_paths")
        if not isinstance(allowed_paths, list):
            return _deny(DENY_MISSING_EVIDENCE, "scope.allowed_paths must be a list")
        if not isinstance(forbidden_paths, list):
            return _deny(DENY_MISSING_EVIDENCE, "scope.forbidden_paths must be a list")

        for a in allowed_paths:
            if not isinstance(a, str):
                return _deny(DENY_MISSING_EVIDENCE, f"scope.allowed_paths contains non-string: {a!r}")
        for f in forbidden_paths:
            if not isinstance(f, str):
                return _deny(DENY_MISSING_EVIDENCE, f"scope.forbidden_paths contains non-string: {f!r}")

        if mode == "read_only":
            if len(changed_paths) > 0:
                return _deny(DENY_SCOPE_VIOLATION, f"read_only unit changed files: {list(changed_paths)}")
        else:
            for path in changed_paths:
                norm = path.replace("\\", "/").lower()
                if norm == ".git" or norm.startswith(".git/"):
                    return _deny(DENY_SCOPE_VIOLATION, f"changed path touches git metadata: {path}")
                if any(work_units.covers(f, path) for f in forbidden_paths):
                    return _deny(DENY_SCOPE_VIOLATION, f"changed path touches forbidden path: {path}")
                if not any(work_units.covers(a, path) for a in allowed_paths):
                    return _deny(DENY_SCOPE_VIOLATION, f"changed path outside allowed scope: {path}")

        # 6. Snapshot & Repo Mutations check (Policy A)
        pre_snap = att.get("pre_attempt_snapshot")
        if not isinstance(pre_snap, dict) or not pre_snap:
            return _deny(DENY_MISSING_EVIDENCE, "missing pre_attempt_snapshot")

        # Exact legacy Policy A resolution, after its original context fallback.
        snap = candidate_snapshot or att.get("candidate_snapshot") or att.get("post_attempt_snapshot")

        if snap is None or not isinstance(snap, dict):
            return _deny(DENY_MISSING_EVIDENCE, "missing authoritative candidate snapshot evidence")
        for field in REQUIRED_SNAPSHOT_FIELDS:
            if field not in snap:
                return _deny(DENY_MISSING_EVIDENCE, f"candidate snapshot missing durable.snapshot field: {field}")

        # Validate complete durable.snapshot schema fields on pre_attempt_snapshot
        for field in REQUIRED_SNAPSHOT_FIELDS:
            if field not in pre_snap:
                return _deny(DENY_MISSING_EVIDENCE, f"pre_attempt_snapshot missing durable.snapshot field: {field}")

        # Validate authoritative repository identity
        if snap["repository"] != pre_snap["repository"]:
            return _deny(DENY_UNVERIFIED_MUTATIONS, "repository path mismatch between snapshots")
        if snap["git_directory"] != pre_snap["git_directory"]:
            return _deny(DENY_UNVERIFIED_MUTATIONS, "git_directory mismatch between snapshots")
        if snap["git_identity"] != pre_snap["git_identity"]:
            return _deny(DENY_UNVERIFIED_MUTATIONS, "git_identity mismatch between snapshots")
        if snap["head"] != pre_snap["head"]:
            return _deny(DENY_UNVERIFIED_MUTATIONS, f"candidate git HEAD drift detected: pre={pre_snap['head']}, candidate={snap['head']}")

        pre_files = pre_snap["files"]
        post_files = snap["files"]
        if not isinstance(pre_files, dict) or not isinstance(post_files, dict):
            return _deny(DENY_MISSING_EVIDENCE, "pre_attempt_snapshot or candidate_snapshot files is not a dict")
        if len(pre_files) == 0 or len(post_files) == 0:
            return _deny(DENY_MISSING_EVIDENCE, "empty snapshot files dictionary is not authoritative")

        # Validate file entry schemas
        for f_path, entry in pre_files.items():
            if not isinstance(f_path, str) or not isinstance(entry, dict) or "sha256" not in entry or "mode" not in entry:
                return _deny(DENY_MISSING_EVIDENCE, f"malformed pre_attempt_snapshot file entry for {f_path!r}")
        for f_path, entry in post_files.items():
            if not isinstance(f_path, str) or not isinstance(entry, dict) or "sha256" not in entry or "mode" not in entry:
                return _deny(DENY_MISSING_EVIDENCE, f"malformed candidate_snapshot file entry for {f_path!r}")

        # Frozen-path coverage check: frozen paths must have coverage and be unchanged
        for f in forbidden_paths:
            if not f.endswith("/"):
                norm_f = f.replace("\\", "/").lower()
                pre_match = [k for k in pre_files if k.replace("\\", "/").lower() == norm_f]
                post_match = [k for k in post_files if k.replace("\\", "/").lower() == norm_f]
                if not pre_match or not post_match:
                    return _deny(DENY_MISSING_EVIDENCE, f"missing snapshot coverage for frozen/forbidden path: {f!r}")
                if pre_files[pre_match[0]] != post_files[post_match[0]]:
                    return _deny(DENY_SCOPE_VIOLATION, f"frozen/forbidden path mutated in snapshot: {f!r}")
            else:
                for k in pre_files:
                    if work_units.covers(f, k):
                        if k not in post_files or pre_files[k] != post_files[k]:
                            return _deny(DENY_SCOPE_VIOLATION, f"frozen/forbidden directory mutated in snapshot: {k!r}")

        # Compare snapshot mutations against changed_paths
        all_keys = set(pre_files.keys()) | set(post_files.keys())
        mutated_files = set()
        for k in all_keys:
            if pre_files.get(k) != post_files.get(k):
                mutated_files.add(k.replace("\\", "/").lower())

        norm_changed_paths = {p.replace("\\", "/").lower() for p in changed_paths}

        unrecorded = mutated_files - norm_changed_paths
        if unrecorded:
            return _deny(DENY_UNVERIFIED_MUTATIONS, f"snapshot mutated file(s) not in changed_paths: {sorted(unrecorded)}")

        unreflected = norm_changed_paths - mutated_files
        if unreflected:
            return _deny(DENY_CONTRADICTORY_EVIDENCE, f"changed_paths claimed mutation but snapshot files identical for: {sorted(unreflected)}")

        # 6a. Verifier repo mutation check (Policy A)
        if repo_mutated_during_verification is True:
            return _deny(DENY_UNVERIFIED_MUTATIONS, "repository mutated during verification")

        if verification_snapshot_before is not None and verification_snapshot_after is not None:
            if not isinstance(verification_snapshot_before, dict) or not isinstance(verification_snapshot_after, dict):
                return _deny(DENY_MISSING_EVIDENCE, "verification snapshots must be dicts")
            if verification_snapshot_before != verification_snapshot_after:
                return _deny(DENY_UNVERIFIED_MUTATIONS, "verification snapshots mismatch (repo mutated during test execution)")
        elif repo_mutated_during_verification is not False:
            return _deny(DENY_MISSING_EVIDENCE, "missing verifier repository mutation check evidence")

    # 7. Real Supervisor Authority Check
    if supervisor is None:
        return _deny(DENY_MISSING_EVIDENCE, "missing authoritative supervisor instance")
    if not hasattr(supervisor, "reconcile") or not callable(supervisor.reconcile):
        return _deny(DENY_MISSING_EVIDENCE, "supervisor missing callable reconcile()")
    if not hasattr(supervisor, "require_idle") or not callable(supervisor.require_idle):
        return _deny(DENY_MISSING_EVIDENCE, "supervisor missing callable require_idle()")

    if cached_supervisor_children is not None:
        children = cached_supervisor_children
    else:
        try:
            children = supervisor.reconcile()
        except Exception as exc:
            return _deny(DENY_PROCESS_NOT_IDLE, f"supervisor reconcile failed: {exc}")

    if not isinstance(children, list):
        return _deny(DENY_MISSING_EVIDENCE, "supervisor reconcile() must return a list")

    for c in children:
        if not isinstance(c, dict):
            return _deny(DENY_MISSING_EVIDENCE, "supervisor child must be a dict")
        c_state = c.get("state")
        c_purpose = c.get("purpose")
        if not isinstance(c_state, str) or c_state not in SUPERVISOR_LEGAL_STATES:
            return _deny(DENY_PROCESS_NOT_IDLE, f"unrecognized supervisor state: {c_state!r}")
        if c_state in ("UNKNOWN", "STARTING", "LOST", "STALE"):
            return _deny(DENY_PROCESS_NOT_IDLE, f"supervisor child {c.get('child_id')} in unclean state {c_state}")
        if c_state in ("RUNNING", "STOPPING"):
            if require_idle:
                return _deny(DENY_PROCESS_NOT_IDLE, f"child active while require_idle=True ({c_state})")
            if c_purpose != "model_server":
                return _deny(DENY_PROCESS_NOT_IDLE, f"unauthorized active child: {c.get('child_id')} ({c_state}, purpose={c_purpose})")
            # Do not trust purpose alone: verify containment and process identity
            containment = c.get("containment")
            pid = c.get("pid")
            ident = c.get("identity")
            if containment not in ("job", "delegated") or type(pid) is not int or pid <= 0 or not isinstance(ident, dict):
                return _deny(DENY_PROCESS_NOT_IDLE, f"active model_server has unproven supervisor ownership: {c.get('child_id')}")

    if require_idle:
        try:
            supervisor.require_idle()
        except Exception as exc:
            return _deny(DENY_PROCESS_NOT_IDLE, f"authoritative require_idle refused: {exc}")

    # 8. Environment drift check (Mandatory: must be provided from controller evidence)
    if environment_status is None:
        return _deny(DENY_MISSING_EVIDENCE, "missing environment status evidence")

    if isinstance(environment_status, str):
        if environment_status.strip().upper() != "SAFE":
            return _deny(DENY_ENVIRONMENT_DRIFT, f"environment status: {environment_status}")
    elif isinstance(environment_status, dict):
        dec = environment_status.get("decision")
        if dec != "SAFE":
            return _deny(DENY_ENVIRONMENT_DRIFT, f"environment drift decision: {dec}")
    else:
        return _deny(DENY_MISSING_EVIDENCE, f"invalid environment_status type: {type(environment_status).__name__}")

    # 9. Budget check (Mandatory: both unit and task budgets must be finite positive numbers)
    if remaining_task_budget is None:
        return _deny(DENY_MISSING_EVIDENCE, "missing remaining_task_budget evidence")

    valid_t, msg_t = _validate_budget_value(remaining_task_budget, "remaining_task_budget", min_budget_seconds)
    if not valid_t:
        return _deny(DENY_INSUFFICIENT_BUDGET, msg_t)

    effective_unit_budget = remaining_unit_budget
    if effective_unit_budget is None:
        timeout_info = att.get("timeout_info")
        limit_t = limits.get("timeout_seconds")
        if isinstance(timeout_info, dict) and "elapsed_seconds" in timeout_info and limit_t is not None:
            elapsed = timeout_info.get("elapsed_seconds")
            if (
                type(elapsed) in (int, float)
                and type(limit_t) in (int, float)
                and not isinstance(elapsed, bool)
                and not isinstance(limit_t, bool)
                and math.isfinite(elapsed)
                and math.isfinite(limit_t)
                and elapsed >= 0
                and limit_t > 0
            ):
                effective_unit_budget = limit_t - elapsed
            else:
                return _deny(DENY_INSUFFICIENT_BUDGET, "malformed timeout_info or limits.timeout_seconds")
        else:
            return _deny(DENY_MISSING_EVIDENCE, "missing remaining_unit_budget evidence")

    valid_u, msg_u = _validate_budget_value(effective_unit_budget, "remaining_unit_budget", min_budget_seconds)
    if not valid_u:
        return _deny(DENY_INSUFFICIENT_BUDGET, msg_u)

    # 10. All conditions verified and passed
    details: dict[str, Any] = {
        "failed_verifiers": failed_vids,
        "changed_paths": list(changed_paths),
    }
    if is_policy_b:
        details["policy"] = "B"
    return _allow(ALLOW_VERIFIER_FAILURE, details)


# Functional alias
evaluate_retry_eligibility = classify_retry_eligibility
