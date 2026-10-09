"""WorkUnit contracts, lifecycle, validation and durable persistence.

Wave 1: data model + validation + persistence integration only.
No scheduling, no model planning, no live execution.

TRUST RULE: WorkUnit state is INTERMEDIATE / UNTRUSTED progress.
UNIT_VERIFIED means only that the unit's registered deterministic local
gate passed against a recorded candidate state.  It MUST NEVER promote
existing top-level trusted state (VERIFIED, verified_progress,
last_verified_checkpoint).
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from datetime import datetime, timezone

import durable

# --------------------------------------------------------------------------- constants

SCHEMA_VERSION = 1

MODES = ("read_only", "mutation")

UNIT_STATUSES = (
    "PROPOSED",
    "VALIDATED",
    "PENDING",
    "READY",
    "EXECUTING",
    "VERIFYING",
    "UNIT_VERIFIED",
    "REPAIR_PENDING",
    "UNIT_FAILED",
)

MILESTONE_STATUSES = (
    "NOT_STARTED",
    "IN_PROGRESS",
    "MILESTONE_READY",
    "MILESTONE_FAILED",
    "CRITIC_REVIEWED",
)

MILESTONE_READY = "MILESTONE_READY"
CRITIC_REVIEWED = "CRITIC_REVIEWED"
WORK_UNITS_COMPLETE = "WORK_UNITS_COMPLETE"
MILESTONE_FAILED = "MILESTONE_FAILED"
WORK_UNITS_FAILED = "WORK_UNITS_FAILED"

# Legal lifecycle transitions.  Anything else fails closed.
UNIT_TRANSITIONS = {
    "PROPOSED":       {"VALIDATED"},
    "VALIDATED":      {"PENDING", "READY"},
    "PENDING":        {"READY"},
    "READY":          {"EXECUTING"},
    "EXECUTING":      {"VERIFYING", "REPAIR_PENDING", "UNIT_FAILED"},
    "VERIFYING":      {"UNIT_VERIFIED", "REPAIR_PENDING", "UNIT_FAILED"},
    "UNIT_VERIFIED":  set(),                       # terminal
    "REPAIR_PENDING": {"READY", "UNIT_FAILED"},
    "UNIT_FAILED":    set(),                       # terminal
}

# Attempt result classifications.
ATTEMPT_OUTCOMES = ("passed", "failed", "timeout", "error", "interrupted")

# Statuses that imply readiness, execution, verification, or completion
# and therefore require all prerequisite units to be UNIT_VERIFIED.
DEPENDENT_ACTIVE_STATUSES = {
    "READY",
    "EXECUTING",
    "VERIFYING",
    "UNIT_VERIFIED",
    "REPAIR_PENDING",
}

# --------------------------------------------------------------------------- helpers

def _now():
    return datetime.now(timezone.utc).isoformat()


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False).encode("utf-8")
    ).hexdigest()


def _safe_id(value, label="ID"):
    if not isinstance(value, str) or not value:
        raise WorkUnitError(f"{label} must be a non-empty string")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", value):
        raise WorkUnitError(f"Invalid {label}: {value!r}")
    return value


# --------------------------------------------------------------------------- errors

class WorkUnitError(durable.DurableError):
    """Fail closed.  No state mutation occurs."""


# --------------------------------------------------------------------------- paths and scope

def normalize_path(value):
    if not isinstance(value, str) or not value or len(value) > 300 or any(ord(c) < 32 for c in value):
        raise WorkUnitError("Invalid path")
    text = value.replace("\\", "/")
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text) or text.startswith("//"):
        raise WorkUnitError("Path must be repository-relative and not absolute or drive-qualified")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." or part.lower().rstrip(" .") == ".git" or ":" in part for part in parts):
        raise WorkUnitError("Path is not allowed")
    return "/".join(parts)


def normalize_entry(value):
    """A scope entry is an exact file or a directory prefix ending in '/'; globs are refused."""
    if not isinstance(value, str):
        raise WorkUnitError("Scope entry must be a string")
    if re.search(r"[*?\[\]{}]", value):
        raise WorkUnitError("Scope entries are exact files or directory prefixes; globs are refused")
    norm = normalize_path(value)
    if value.replace("\\", "/").endswith("/"):
        norm += "/"
    return norm


def covers(entry, path):
    """Case-insensitive prefix or exact match depending on directory boundary."""
    if not isinstance(entry, str) or not isinstance(path, str):
        raise WorkUnitError("covers expects string arguments")
    entry_l, path_l = entry.lower(), path.lower()
    return path_l.startswith(entry_l) if entry_l.endswith("/") else path_l == entry_l


def overlaps(a, b):
    if not isinstance(a, str) or not isinstance(b, str):
        raise WorkUnitError("overlaps expects string arguments")
    return covers(a, b) or covers(b, a)


# --------------------------------------------------------------------------- WorkUnit spec

def validate_unit_spec(spec):
    """Validate a single WorkUnit specification dict.

    Returns the validated spec; raises WorkUnitError on any defect.
    """
    if not isinstance(spec, dict):
        raise WorkUnitError("WorkUnit spec must be a dict")

    required = {"unit_id", "objective", "dependencies", "mode", "scope",
                "verifier_ids", "evidence_inputs", "limits"}
    optional = {"required_capabilities", "purpose"}
    missing = required - spec.keys()
    if missing:
        raise WorkUnitError(f"Missing required fields: {sorted(missing)}")
    extra = spec.keys() - required - optional
    if extra:
        raise WorkUnitError(f"Unexpected fields: {sorted(extra)}")

    if "required_capabilities" in spec:
        req_caps = spec["required_capabilities"]
        if not isinstance(req_caps, list):
            raise WorkUnitError("required_capabilities must be a list")
        for cap in req_caps:
            if not isinstance(cap, str) or not cap:
                raise WorkUnitError(f"Invalid required_capability item: {cap!r}")

    if "purpose" in spec:
        purpose_val = spec["purpose"]
        if not isinstance(purpose_val, str) or not purpose_val or len(purpose_val) > 80:
            raise WorkUnitError("spec.purpose must be a non-empty string <= 80 chars")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", purpose_val):
            raise WorkUnitError(f"Invalid spec.purpose: {purpose_val!r}")

    _safe_id(spec["unit_id"], "unit_id")

    # objective
    objective = spec["objective"]
    if not isinstance(objective, str) or not objective.strip() or len(objective) > 2000:
        raise WorkUnitError("objective must be a non-empty string <= 2000 chars")

    # dependencies
    deps = spec.get("dependencies")
    if not isinstance(deps, list):
        raise WorkUnitError("dependencies must be a list")
    for d in deps:
        if not isinstance(d, str) or not d:
            raise WorkUnitError(f"Invalid dependency item: {d!r}")
    if spec["unit_id"] in deps:
        raise WorkUnitError(f"Self-dependency: {spec['unit_id']}")
    if len(deps) != len(set(deps)):
        raise WorkUnitError("Duplicate dependency")

    # mode
    if spec["mode"] not in MODES:
        raise WorkUnitError(f"Invalid mode: {spec['mode']!r}; must be one of {MODES}")

    # scope
    scope = spec.get("scope")
    if not isinstance(scope, dict):
        raise WorkUnitError("scope must be a dict")
    if set(scope) != {"allowed_paths", "forbidden_paths"}:
        raise WorkUnitError("scope must have exactly allowed_paths and forbidden_paths")

    allowed = scope.get("allowed_paths")
    forbidden = scope.get("forbidden_paths")
    if not isinstance(allowed, list):
        raise WorkUnitError("scope.allowed_paths must be a list")
    if not isinstance(forbidden, list):
        raise WorkUnitError("scope.forbidden_paths must be a list")

    if not allowed:
        raise WorkUnitError("scope.allowed_paths must not be empty")
    if len(allowed) > 50:
        raise WorkUnitError("scope.allowed_paths exceeds maximum of 50 entries")

    # Normalize entries and check path validity and overlaps
    norm_allowed = []
    for p in allowed:
        if not isinstance(p, str) or not p:
            raise WorkUnitError(f"scope.allowed_paths item must be a non-empty string, got {p!r}")
        norm_p = normalize_entry(p)
        if norm_p.lower() in [x.lower() for x in norm_allowed]:
            raise WorkUnitError(f"Duplicate allowed path: {p!r}")
        norm_allowed.append(norm_p)

    norm_forbidden = []
    for p in forbidden:
        if not isinstance(p, str) or not p:
            raise WorkUnitError(f"scope.forbidden_paths item must be a non-empty string, got {p!r}")
        norm_p = normalize_entry(p)
        if norm_p.lower() in [x.lower() for x in norm_forbidden]:
            raise WorkUnitError(f"Duplicate forbidden path: {p!r}")
        norm_forbidden.append(norm_p)

    for a in norm_allowed:
        for f in norm_forbidden:
            if overlaps(a, f):
                raise WorkUnitError(f"scope allowed_path {a!r} overlaps forbidden_path {f!r}")

    # verifier_ids
    vids = spec.get("verifier_ids")
    if not isinstance(vids, list):
        raise WorkUnitError("verifier_ids must be a list")
    for v in vids:
        if not isinstance(v, str) or not v or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", v):
            raise WorkUnitError(f"Invalid verifier_id item: {v!r}")
    if not vids:
        raise WorkUnitError("verifier_ids must not be empty")
    if len(vids) != len(set(vids)):
        raise WorkUnitError("Duplicate verifier_id")

    # evidence_inputs
    ei = spec.get("evidence_inputs")
    if not isinstance(ei, list):
        raise WorkUnitError("evidence_inputs must be a list")
    for item in ei:
        if not isinstance(item, dict) or "reference" not in item:
            raise WorkUnitError("Each evidence_input must be a dict with at least a 'reference' key")

    # limits
    limits = spec.get("limits")
    if not isinstance(limits, dict):
        raise WorkUnitError("limits must be a dict")
    if "max_attempts" not in limits:
        raise WorkUnitError("limits must include max_attempts")
    max_attempts = limits["max_attempts"]
    if type(max_attempts) is not int or max_attempts < 1:
        raise WorkUnitError("limits.max_attempts must be a positive integer")
    if "timeout_seconds" in limits:
        timeout = limits["timeout_seconds"]
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise WorkUnitError("limits.timeout_seconds must be a finite positive number")

    return spec


def validate_scope_containment(unit_scope, parent_scope):
    """Verify that unit scope does not escape the parent task scope.

    parent_scope is the controller-owned run scope with allowed_paths and
    forbidden_paths.
    """
    if not isinstance(unit_scope, dict) or not isinstance(parent_scope, dict):
        raise WorkUnitError("Scopes must be dicts")

    parent_allowed_raw = parent_scope.get("allowed_paths")
    if parent_allowed_raw is None:
        parent_allowed = []
    elif isinstance(parent_allowed_raw, list):
        for p in parent_allowed_raw:
            if not isinstance(p, str) or not p:
                raise WorkUnitError(f"parent_scope allowed_paths item must be string: {p!r}")
        parent_allowed = [normalize_entry(p) for p in parent_allowed_raw]
    else:
        raise WorkUnitError("parent_scope allowed_paths must be a list or None")

    parent_forbidden_raw = parent_scope.get("forbidden_paths")
    if parent_forbidden_raw is None:
        parent_forbidden = []
    elif isinstance(parent_forbidden_raw, list):
        for p in parent_forbidden_raw:
            if not isinstance(p, str) or not p:
                raise WorkUnitError(f"parent_scope forbidden_paths item must be string: {p!r}")
        parent_forbidden = [normalize_entry(p) for p in parent_forbidden_raw]
    else:
        raise WorkUnitError("parent_scope forbidden_paths must be a list or None")

    unit_allowed_raw = unit_scope.get("allowed_paths")
    if not isinstance(unit_allowed_raw, list):
        raise WorkUnitError("unit_scope allowed_paths must be a list")
    for p in unit_allowed_raw:
        if not isinstance(p, str) or not p:
            raise WorkUnitError(f"unit_scope allowed_paths item must be string: {p!r}")
    unit_allowed = [normalize_entry(p) for p in unit_allowed_raw]

    unit_forbidden_raw = unit_scope.get("forbidden_paths")
    if unit_forbidden_raw is None:
        unit_forbidden = []
    elif isinstance(unit_forbidden_raw, list):
        for p in unit_forbidden_raw:
            if not isinstance(p, str) or not p:
                raise WorkUnitError(f"unit_scope forbidden_paths item must be string: {p!r}")
        unit_forbidden = [normalize_entry(p) for p in unit_forbidden_raw]
    else:
        raise WorkUnitError("unit_scope forbidden_paths must be a list or None")

    if parent_allowed:
        for unit_path in unit_allowed:
            if not any(covers(pa, unit_path) for pa in parent_allowed):
                raise WorkUnitError(
                    f"scope.allowed_paths entry {unit_path!r} escapes parent scope"
                )

    for unit_path in unit_allowed:
        for pf in parent_forbidden:
            if overlaps(pf, unit_path):
                raise WorkUnitError(
                    f"scope.allowed_paths entry {unit_path!r} overlaps parent forbidden path {pf!r}"
                )


# --------------------------------------------------------------------------- verifier registry

def validate_verifier_reference(verifier_id, registry):
    """Check that verifier_id refers to a controller-known deterministic verifier.

    registry is a dict mapping verifier IDs to their definitions.
    Models cannot author executable verification commands through verifier_ids.
    """
    if not isinstance(verifier_id, str):
        raise WorkUnitError("verifier_id must be a string")
    if not isinstance(registry, dict):
        raise WorkUnitError("Verifier registry must be a dict")
    if verifier_id not in registry:
        raise WorkUnitError(f"Unknown verifier_id: {verifier_id!r}")
    defn = registry[verifier_id]
    if not isinstance(defn, dict) or "argv" not in defn:
        raise WorkUnitError(f"Malformed verifier definition for {verifier_id!r}")
    if not isinstance(defn["argv"], list) or not all(isinstance(a, str) for a in defn["argv"]):
        raise WorkUnitError(f"Verifier {verifier_id!r} argv must be a list of strings")
    return defn


# --------------------------------------------------------------------------- WorkUnit plan

def validate_unit_plan(units, *, parent_scope=None, verifier_registry=None):
    """Validate a complete plan of WorkUnits.

    Checks:
    - Each unit passes spec validation
    - No duplicate IDs
    - No missing dependencies
    - No self-dependencies
    - No dependency cycles
    - Scope containment (if parent_scope provided)
    - Verifier references (if registry provided)

    Returns the list of validated unit specs.
    """
    if not isinstance(units, list):
        raise WorkUnitError("Unit plan must be a list")

    # Validate each spec individually
    validated = []
    ids = set()
    for unit in units:
        spec = validate_unit_spec(unit)
        uid = spec["unit_id"]
        if uid in ids:
            raise WorkUnitError(f"Duplicate unit_id: {uid!r}")
        ids.add(uid)
        validated.append(spec)

    # Check dependencies: no missing
    for spec in validated:
        for dep in spec["dependencies"]:
            if dep not in ids:
                raise WorkUnitError(
                    f"Unit {spec['unit_id']!r} depends on unknown unit {dep!r}"
                )

    # Check dependency cycles
    _check_cycles(validated)

    # Scope containment
    if parent_scope is not None:
        for spec in validated:
            validate_scope_containment(spec["scope"], parent_scope)

    # Verifier references
    if verifier_registry is not None:
        for spec in validated:
            for vid in spec["verifier_ids"]:
                validate_verifier_reference(vid, verifier_registry)

    return validated


def _check_cycles(units):
    """Detect cycles in the dependency graph using DFS."""
    adj = {u["unit_id"]: u["dependencies"] for u in units}
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {uid: WHITE for uid in adj}

    def dfs(uid):
        color[uid] = GRAY
        for dep in adj[uid]:
            if color[dep] == GRAY:
                raise WorkUnitError(f"Dependency cycle involving {uid!r} and {dep!r}")
            if color[dep] == WHITE:
                dfs(dep)
        color[uid] = BLACK

    for uid in adj:
        if color[uid] == WHITE:
            dfs(uid)


# --------------------------------------------------------------------------- verifier evidence & verification contract

def validate_verifier_evidence(evidence, required_verifier_ids):
    """Validate deterministic controller-owned verifier evidence.

    Invariants:
    - evidence must be a non-empty dict
    - evidence['passed'] must be explicitly True (not merely truthy)
    - every verifier ID may appear at most once across evidence reporting
    - duplicate verifier IDs (including duplicate bare IDs or objects) MUST be rejected
    - conflicting duplicate results MUST be rejected
    - bare verifier IDs must not silently imply success; explicit successful evidence required
    - all required verifier IDs must have explicit successful evidence
    - unknown verifier IDs must be rejected
    """
    if not isinstance(evidence, dict) or not evidence:
        raise WorkUnitError("verifier_evidence must be a non-empty dict")

    if evidence.get("passed") is not True:
        raise WorkUnitError("verifier_evidence must explicitly record success with passed=True")

    if not isinstance(required_verifier_ids, list):
        raise WorkUnitError("required_verifier_ids must be a list")

    seen_verifiers = {}

    def record_verifier(vid, entry):
        if not isinstance(vid, str) or not vid:
            raise WorkUnitError(f"Invalid verifier ID in evidence: {vid!r}")
        if vid in seen_verifiers:
            raise WorkUnitError(f"Duplicate verifier ID {vid!r} in evidence")
        if vid not in required_verifier_ids:
            raise WorkUnitError(f"Unknown verifier ID {vid!r} cannot establish verification")
        seen_verifiers[vid] = entry

    # 1. Inspect evidence["verifiers"] if present
    if "verifiers" in evidence:
        v_container = evidence["verifiers"]
        if isinstance(v_container, dict):
            for k, v in v_container.items():
                record_verifier(k, v)
        elif isinstance(v_container, list):
            for item in v_container:
                if isinstance(item, dict):
                    vid = item.get("verifier_id") or item.get("check") or item.get("id")
                    if not isinstance(vid, str) or not vid:
                        raise WorkUnitError("Verifier item in list must have a string verifier_id")
                    record_verifier(vid, item)
                elif isinstance(item, str):
                    # Bare string ID in list
                    record_verifier(item, item)
                else:
                    raise WorkUnitError("Invalid verifier item in verifiers list")
        else:
            raise WorkUnitError("evidence.verifiers must be a dict or list")

    # 2. Inspect evidence["verifier_ids"] if present
    if "verifier_ids" in evidence:
        v_ids = evidence["verifier_ids"]
        if not isinstance(v_ids, list):
            raise WorkUnitError("evidence.verifier_ids must be a list")
        for vid in v_ids:
            record_verifier(vid, vid)

    # 3. Inspect single check fields
    for key in ("check", "verifier_id"):
        if key in evidence:
            val = evidence[key]
            if not isinstance(val, str) or not val:
                raise WorkUnitError(f"evidence.{key} must be a non-empty string")
            record_verifier(val, evidence)

    # 4. Inspect direct keys on evidence
    for k, val in evidence.items():
        if k in ("passed", "verifiers", "verifier_ids", "check", "verifier_id",
                "commands", "time", "snapshot", "reference", "candidate_snapshot",
                "candidate_id", "attempt_id", "trust_level"):
            continue
        if k in required_verifier_ids:
            record_verifier(k, val)
        elif k.startswith("check") or k.startswith("verifier"):
            record_verifier(k, val)

    # 5. Check that all required verifier IDs are present
    for vid in required_verifier_ids:
        if vid not in seen_verifiers:
            raise WorkUnitError(f"Missing required verifier evidence for {vid!r}")

    # 6. Check that every entry has explicit successful evidence
    for vid in required_verifier_ids:
        entry = seen_verifiers[vid]
        if isinstance(entry, str):
            raise WorkUnitError(f"Bare verifier ID {vid!r} does not provide explicit successful evidence")
        if not isinstance(entry, dict):
            raise WorkUnitError(f"Verifier evidence for {vid!r} must be an explicit result dict")
        if entry.get("passed") is False:
            raise WorkUnitError(f"Verifier {vid!r} explicitly recorded failure")
        if entry.get("exit_code") is not None and entry.get("exit_code") != 0:
            raise WorkUnitError(f"Verifier {vid!r} recorded non-zero exit_code")
        if entry.get("passed") is not True and entry.get("exit_code") != 0:
            raise WorkUnitError(f"Verifier {vid!r} evidence does not record explicit success")


def validate_unit_verified_preconditions(state):
    """Validate all preconditions required to transition to or persist UNIT_VERIFIED."""
    result = state.get("result")
    if not isinstance(result, dict) or not result:
        raise WorkUnitError("UNIT_VERIFIED without result evidence")

    if result.get("trust_level") != "INTERMEDIATE":
        raise WorkUnitError(
            f"UNIT_VERIFIED result trust_level must be exactly INTERMEDIATE, got {result.get('trust_level')!r}"
        )

    # Verifier evidence check
    evidence = result.get("verifier_evidence")
    required_vids = state["spec"]["verifier_ids"]
    validate_verifier_evidence(evidence, required_vids)

    # Associated attempt check
    if not state.get("attempts"):
        raise WorkUnitError("UNIT_VERIFIED requires an associated successful attempt")

    attempt_id = result.get("attempt_id")
    matching_attempts = [a for a in state["attempts"] if a["attempt_id"] == attempt_id] if attempt_id else []

    if attempt_id:
        if not matching_attempts:
            raise WorkUnitError(f"Associated attempt {attempt_id!r} not found in unit attempts")
        attempt = matching_attempts[0]
    else:
        passed_attempts = [a for a in state["attempts"] if a.get("outcome") == "passed"]
        if not passed_attempts:
            raise WorkUnitError("UNIT_VERIFIED requires an associated successful attempt")
        attempt = passed_attempts[-1]

    if attempt.get("outcome") != "passed":
        raise WorkUnitError(
            f"Associated attempt {attempt.get('attempt_id')!r} did not pass (outcome={attempt.get('outcome')!r})"
        )

    # Candidate / workspace identity check
    cand_id_res = result.get("candidate_id")
    cand_snap_res = result.get("candidate_snapshot")
    cand_id_att = attempt.get("candidate_id")

    if not cand_id_res and not cand_snap_res and not cand_id_att:
        raise WorkUnitError("UNIT_VERIFIED requires candidate identity (candidate_id or candidate_snapshot)")

    # If candidate_id is present on both attempt and result, they must match
    if cand_id_res and cand_id_att and cand_id_res != cand_id_att:
        raise WorkUnitError(
            f"Candidate identity mismatch between attempt ({cand_id_att!r}) and result ({cand_id_res!r})"
        )


# --------------------------------------------------------------------------- unit execution state

def initial_unit_state(spec):
    """Create the initial durable state for a validated WorkUnit spec."""
    return {
        "unit_id": spec["unit_id"],
        "spec": copy.deepcopy(spec),
        "status": "PROPOSED",
        "created_at": _now(),
        "updated_at": _now(),
        "attempts": [],
        "current_attempt": None,
        "result": None,
        "checksum": None,
    }


def transition_unit(state, new_status):
    """Transition a unit to a new status.  Fails closed on invalid transitions."""
    current = state["status"]
    if current in ("UNIT_VERIFIED", "UNIT_FAILED"):
        raise WorkUnitError(f"Cannot transition terminal unit from {current!r}")
    if current not in UNIT_TRANSITIONS:
        raise WorkUnitError(f"Unknown current status: {current!r}")
    allowed = UNIT_TRANSITIONS[current]
    if new_status not in allowed:
        raise WorkUnitError(
            f"Invalid transition: {current} -> {new_status}; "
            f"allowed: {sorted(allowed) or 'none (terminal)'}"
        )

    # Validate transition preconditions BEFORE mutating status
    if new_status == "REPAIR_PENDING":
        max_attempts = state["spec"]["limits"]["max_attempts"]
        if len(state["attempts"]) >= max_attempts:
            raise WorkUnitError(
                f"Cannot enter REPAIR_PENDING when attempt budget is exhausted ({len(state['attempts'])} >= {max_attempts})"
            )
        if not state["attempts"]:
            raise WorkUnitError("REPAIR_PENDING without any prior attempts")
        last = state["attempts"][-1]
        if last["outcome"] not in ("failed", "timeout", "error"):
            raise WorkUnitError(
                f"REPAIR_PENDING without eligible prior failure (last outcome: {last['outcome']!r})"
            )
        if any(a.get("completed_at") is None for a in state["attempts"]):
            raise WorkUnitError("Cannot enter REPAIR_PENDING while an attempt is still active")

    elif new_status == "UNIT_VERIFIED":
        validate_unit_verified_preconditions(state)

    elif new_status == "EXECUTING":
        max_attempts = state["spec"]["limits"]["max_attempts"]
        if len(state["attempts"]) >= max_attempts:
            raise WorkUnitError(
                f"Cannot enter EXECUTING when attempt budget is exhausted ({len(state['attempts'])} >= {max_attempts})"
            )

    state["status"] = new_status
    state["updated_at"] = _now()
    return state


def validate_unit_state(state):
    """Validate a loaded unit execution state.  Raises WorkUnitError on any defect."""
    if not isinstance(state, dict):
        raise WorkUnitError("Unit state must be a dict")
    required = {"unit_id", "spec", "status", "created_at", "updated_at",
                "attempts", "current_attempt", "result", "checksum"}
    missing = required - state.keys()
    if missing:
        raise WorkUnitError(f"Missing unit state fields: {sorted(missing)}")

    _safe_id(state["unit_id"], "unit_id")

    if state["status"] not in UNIT_STATUSES:
        raise WorkUnitError(f"Invalid unit status: {state['status']!r}")

    # Spec inside state must be valid.
    if not isinstance(state.get("spec"), dict):
        raise WorkUnitError("Unit state spec must be a dict")
    validate_unit_spec(state["spec"])

    if state["unit_id"] != state["spec"]["unit_id"]:
        raise WorkUnitError("unit_id mismatch between state and spec")

    # Attempts
    if not isinstance(state.get("attempts"), list):
        raise WorkUnitError("attempts must be a list")

    seen_attempt_ids = set()
    active_attempts = []
    for i, attempt in enumerate(state["attempts"]):
        _validate_attempt(attempt, i)
        att_id = attempt["attempt_id"]
        if att_id in seen_attempt_ids:
            raise WorkUnitError(f"Duplicate attempt ID: {att_id!r}")
        seen_attempt_ids.add(att_id)
        if attempt.get("completed_at") is None:
            active_attempts.append(attempt)

    if len(active_attempts) > 1:
        raise WorkUnitError("Multiple unfinished attempts are not allowed")

    if active_attempts:
        if state["current_attempt"] != active_attempts[0]["attempt_id"]:
            raise WorkUnitError(
                f"current_attempt {state['current_attempt']!r} does not match active attempt {active_attempts[0]['attempt_id']!r}"
            )
    else:
        if state["current_attempt"] is not None:
            raise WorkUnitError("current_attempt must be None when no attempt is active")

    # Attempt count vs limits
    max_attempts = state["spec"]["limits"]["max_attempts"]
    if len(state["attempts"]) > max_attempts:
        raise WorkUnitError(
            f"Attempt count {len(state['attempts'])} exceeds max_attempts {max_attempts}"
        )

    # REPAIR_PENDING requires an eligible prior failure and allowance remains
    if state["status"] == "REPAIR_PENDING":
        if len(state["attempts"]) >= max_attempts:
            raise WorkUnitError(
                f"REPAIR_PENDING after attempt budget is exhausted ({len(state['attempts'])} >= {max_attempts})"
            )
        if not state["attempts"]:
            raise WorkUnitError("REPAIR_PENDING without any prior attempts")
        last = state["attempts"][-1]
        if last["outcome"] not in ("failed", "timeout", "error"):
            raise WorkUnitError(
                f"REPAIR_PENDING without eligible prior failure (last outcome: {last['outcome']!r})"
            )
        if active_attempts:
            raise WorkUnitError("REPAIR_PENDING cannot have an active attempt")

    # Terminal state cannot have active attempts
    if state["status"] in ("UNIT_VERIFIED", "UNIT_FAILED") and active_attempts:
        raise WorkUnitError(f"Terminal status {state['status']!r} cannot have an active attempt")

    # UNIT_VERIFIED requires valid verified evidence and preconditions
    if state["status"] == "UNIT_VERIFIED":
        validate_unit_verified_preconditions(state)

    # Result type check if present
    if state["result"] is not None:
        if not isinstance(state["result"], dict):
            raise WorkUnitError("result must be a dict or None")
        if state["result"].get("trust_level") != "INTERMEDIATE":
            raise WorkUnitError(
                f"result trust_level must be exactly INTERMEDIATE, got {state['result'].get('trust_level')!r}"
            )

    # checksum validation
    if state["checksum"] is not None:
        expected = _state_checksum(state)
        if state["checksum"] != expected:
            raise WorkUnitError("Unit state checksum mismatch (tampered data)")

    return state


def _validate_attempt(attempt, index):
    """Validate a single attempt record."""
    if not isinstance(attempt, dict):
        raise WorkUnitError(f"Attempt {index} must be a dict")
    required = {"attempt_id", "outcome"}
    missing = required - attempt.keys()
    if missing:
        raise WorkUnitError(f"Attempt {index} missing fields: {sorted(missing)}")
    _safe_id(attempt["attempt_id"], f"attempt[{index}].attempt_id")
    if attempt["outcome"] not in ATTEMPT_OUTCOMES:
        raise WorkUnitError(
            f"Attempt {index} invalid outcome: {attempt['outcome']!r}"
        )


# --------------------------------------------------------------------------- attempt records

def create_attempt(unit_state, attempt_id, *, candidate_id=None):
    """Create a new attempt record for a unit.

    The unit must be in EXECUTING status.
    """
    if unit_state["status"] in ("UNIT_VERIFIED", "UNIT_FAILED"):
        raise WorkUnitError(f"Cannot create attempt on terminal unit in status {unit_state['status']!r}")
    if unit_state["status"] != "EXECUTING":
        raise WorkUnitError(
            f"Cannot create attempt when unit is {unit_state['status']!r}; must be EXECUTING"
        )
    if any(a.get("completed_at") is None for a in unit_state["attempts"]):
        raise WorkUnitError("Cannot create a new attempt while an existing attempt is still active")

    max_attempts = unit_state["spec"]["limits"]["max_attempts"]
    if len(unit_state["attempts"]) >= max_attempts:
        raise WorkUnitError(
            f"Attempt count would exceed max_attempts ({max_attempts})"
        )
    _safe_id(attempt_id, "attempt_id")
    if any(a["attempt_id"] == attempt_id for a in unit_state["attempts"]):
        raise WorkUnitError(f"Duplicate attempt_id: {attempt_id!r}")

    attempt = {
        "attempt_id": attempt_id,
        "started_at": _now(),
        "completed_at": None,
        "outcome": "interrupted",  # Safe default; set properly on completion.
        "execution_result": None,
        "changed_paths": [],
        "verifier_result": None,
        "failure_classification": None,
        "candidate_id": candidate_id,
        "timeout_info": None,
        "stage": "executing",
    }
    unit_state["attempts"].append(attempt)
    unit_state["current_attempt"] = attempt_id
    unit_state["updated_at"] = _now()
    return attempt


def complete_attempt(unit_state, attempt_id, *, outcome, execution_result=None,
                     changed_paths=None, verifier_result=None,
                     failure_classification=None, candidate_id=None,
                     timeout_info=None):
    """Complete an attempt with its result.

    Completed attempts are immutable.
    """
    if unit_state["status"] in ("UNIT_VERIFIED", "UNIT_FAILED"):
        raise WorkUnitError(f"Cannot complete attempt on terminal unit in status {unit_state['status']!r}")
    if outcome not in ATTEMPT_OUTCOMES:
        raise WorkUnitError(f"Invalid attempt outcome: {outcome!r}")
    attempt = None
    for a in unit_state["attempts"]:
        if a["attempt_id"] == attempt_id:
            attempt = a
            break
    if attempt is None:
        raise WorkUnitError(f"Unknown attempt_id: {attempt_id!r}")

    if attempt.get("completed_at") is not None or attempt.get("stage") == "completed":
        raise WorkUnitError(f"Attempt {attempt_id!r} is already completed and immutable")

    attempt["completed_at"] = _now()
    attempt["outcome"] = outcome
    attempt["execution_result"] = execution_result
    attempt["changed_paths"] = changed_paths or []
    attempt["verifier_result"] = verifier_result
    attempt["failure_classification"] = failure_classification
    if candidate_id is not None:
        attempt["candidate_id"] = candidate_id
    if timeout_info is not None:
        attempt["timeout_info"] = timeout_info
    attempt["stage"] = "completed"

    if unit_state.get("current_attempt") == attempt_id:
        unit_state["current_attempt"] = None

    unit_state["updated_at"] = _now()
    return attempt


def set_unit_result(unit_state, *, verifier_evidence, candidate_id=None, candidate_snapshot=None, attempt_id=None):
    """Record the unit-level result (for UNIT_VERIFIED transitions).

    This is INTERMEDIATE evidence.  It MUST NOT be used to promote
    top-level trusted state.
    """
    if unit_state["status"] in ("UNIT_VERIFIED", "UNIT_FAILED"):
        raise WorkUnitError(f"Cannot set result on terminal unit in status {unit_state['status']!r}")
    if not isinstance(verifier_evidence, dict):
        raise WorkUnitError("verifier_evidence must be a dict")

    # Determine associated attempt
    if attempt_id is None:
        if unit_state.get("current_attempt"):
            attempt_id = unit_state["current_attempt"]
        elif unit_state["attempts"]:
            passed = [a for a in unit_state["attempts"] if a.get("outcome") == "passed"]
            attempt_id = passed[-1]["attempt_id"] if passed else unit_state["attempts"][-1]["attempt_id"]

    if attempt_id is not None:
        matching = [a for a in unit_state["attempts"] if a["attempt_id"] == attempt_id]
        if matching:
            att = matching[0]
            if candidate_id is None:
                candidate_id = att.get("candidate_id")
            elif att.get("candidate_id") and att["candidate_id"] != candidate_id:
                raise WorkUnitError("Candidate identity mismatch between attempt and result")

    unit_state["result"] = {
        "verifier_evidence": verifier_evidence,
        "candidate_id": candidate_id,
        "candidate_snapshot": candidate_snapshot,
        "attempt_id": attempt_id,
        "time": _now(),
        "trust_level": "INTERMEDIATE",  # Explicit: never TRUSTED.
    }
    unit_state["updated_at"] = _now()
    return unit_state


# --------------------------------------------------------------------------- checksum

def _state_checksum(state):
    """Compute a checksum of unit state (excluding the checksum field itself)."""
    data = {k: v for k, v in state.items() if k != "checksum"}
    return _digest(data)


def seal_unit_state(state):
    """Compute and set the integrity checksum on a unit state."""
    state["checksum"] = _state_checksum(state)
    return state


# --------------------------------------------------------------------------- plan-level container

def initial_work_units_section():
    """Return the initial (empty) work_units section for durable state."""
    return {
        "schema_version": SCHEMA_VERSION,
        "units": {},
        "plan_checksum": None,
    }


def add_unit_to_section(section, spec):
    """Add a validated unit spec to the work_units section."""
    validate_unit_spec(spec)
    uid = spec["unit_id"]
    if uid in section["units"]:
        raise WorkUnitError(f"Duplicate unit_id in section: {uid!r}")
    state = initial_unit_state(spec)
    seal_unit_state(state)
    section["units"][uid] = state
    _reseal_section(section)
    return section


def _reseal_section(section):
    """Recompute the plan-level checksum."""
    data = {"schema_version": section["schema_version"],
            "units": {k: v for k, v in sorted(section["units"].items())}}
    section["plan_checksum"] = _digest(data)


def validate_section(section):
    """Validate an entire work_units section loaded from durable state.

    Raises WorkUnitError on any integrity violation.
    """
    if not isinstance(section, dict):
        raise WorkUnitError("work_units section must be a dict")
    sv = section.get("schema_version")
    if type(sv) is not int or sv != SCHEMA_VERSION:
        raise WorkUnitError(
            f"Unsupported work_units schema version: {sv!r}"
        )
    if not isinstance(section.get("units"), dict):
        raise WorkUnitError("work_units.units must be a dict")

    # Validate each unit state
    ids = set()
    for uid, state in section["units"].items():
        if not isinstance(state, dict):
            raise WorkUnitError(f"Unit {uid!r} state must be a dict")
        if uid != state.get("unit_id"):
            raise WorkUnitError(f"Unit key {uid!r} does not match unit_id")
        validate_unit_state(state)
        ids.add(uid)

    # Validate cross-unit dependencies
    for uid, state in section["units"].items():
        for dep in state["spec"]["dependencies"]:
            if dep not in ids:
                raise WorkUnitError(
                    f"Unit {uid!r} depends on unknown unit {dep!r}"
                )
        if uid in state["spec"]["dependencies"]:
            raise WorkUnitError(f"Self-dependency in persisted unit: {uid!r}")

    # Check dependency cycles across the plan
    units_list = [s["spec"] for s in section["units"].values()]
    if units_list:
        _check_cycles(units_list)

    # Section-level lifecycle dependency validation
    for uid, state in section["units"].items():
        if state["status"] in DEPENDENT_ACTIVE_STATUSES:
            for dep in state["spec"]["dependencies"]:
                dep_state = section["units"][dep]
                if dep_state["status"] != "UNIT_VERIFIED":
                    raise WorkUnitError(
                        f"Unit {uid!r} is in state {state['status']!r} but prerequisite unit "
                        f"{dep!r} is in state {dep_state['status']!r} (must be UNIT_VERIFIED)"
                    )

    # Plan checksum
    if section["plan_checksum"] is not None:
        expected_data = {"schema_version": section["schema_version"],
                         "units": {k: v for k, v in sorted(section["units"].items())}}
        expected = _digest(expected_data)
        if section["plan_checksum"] != expected:
            raise WorkUnitError("work_units plan checksum mismatch (tampered data)")

    return section


# --------------------------------------------------------------------------- durable integration

def load_work_units(durable_state):
    """Load work_units from durable state, tolerating legacy runs.

    Legacy runs that have no work_units key return None (not corruption).
    Malformed work_units data raises WorkUnitError (fail closed).
    """
    if not isinstance(durable_state, dict):
        raise WorkUnitError("durable_state must be a dict")
    if "work_units" not in durable_state:
        return None  # Legacy run: no WorkUnits recorded. NOT corruption.
    section = durable_state["work_units"]
    return validate_section(section)


def persist_work_units(section, durable_state):
    """Write validated work_units section into durable state (in-memory).

    The caller must call store.commit() to persist to disk.

    TRUST INVARIANT: This function NEVER modifies:
    - status (top-level)
    - verified_progress
    - last_verified_checkpoint
    - last_failure_evidence

    It only sets the 'work_units' key.
    """
    if not isinstance(durable_state, dict):
        raise WorkUnitError("durable_state must be a dict")
    validate_section(section)

    # Defensive: snapshot the trust fields before and after.
    trust_before = _extract_trust_fields(durable_state)

    durable_state["work_units"] = copy.deepcopy(section)

    trust_after = _extract_trust_fields(durable_state)
    if trust_before != trust_after:
        raise WorkUnitError(
            "CRITICAL: work_units persistence altered trusted state fields"
        )

    return durable_state


def _extract_trust_fields(state):
    """Extract the fields that WorkUnits must NEVER modify."""
    return {
        "verified_progress": copy.deepcopy(state.get("verified_progress")),
        "last_verified_checkpoint": copy.deepcopy(state.get("last_verified_checkpoint")),
    }
