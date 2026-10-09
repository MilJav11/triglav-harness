"""Bounded automatic WorkUnit planning for Wave 3.

Wave 3 architecture:
- High-level task → bounded planner proposal → controller validation → canonical WorkUnit plan.
- Planner is UNTRUSTED: proposal is never a persisted executable WorkUnit directly.
- Controller assigns canonical IDs, validates DAG, enforces scope/verifier/mode policy.
- Planning lifecycle is durable: crash cannot reset attempt counters or skip validation.
- Canonical accepted plan feeds the existing Wave 2 WorkUnitScheduler unchanged.

TRUST RULE:
  PLAN_ACCEPTED is UNTRUSTED planning state only.
  It NEVER implies UNIT_VERIFIED, MILESTONE_READY, top-level VERIFIED,
  verified_progress, last_verified_checkpoint, or trusted completion.
  A planner proposes work.  It NEVER proves work.

AUTHORITY MODEL:
  Planner may propose:
    - objective per unit
    - dependency relationships (using local proposal aliases)
    - mode (chosen from controller-approved set)
    - scope narrowing (only WITHIN parent scope)
    - verifier IDs (chosen from controller-approved registry)

  Controller owns everything else:
    - canonical WorkUnit IDs
    - executable verifier commands
    - trust levels / result state
    - attempt state / budget ceilings
    - persistence format
    - final plan immutability after execution begins
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from typing import Any

import work_units
from work_units import WorkUnitError

# --------------------------------------------------------------------------- constants

WAVE3_SCHEMA_VERSION = 1

# Hard ceiling: planner cannot exceed this.  Controller rejects oversized plans.
MAX_PLAN_UNITS = 8

# Global planner call ceiling: across initial, schema correction, and replan combined
MAX_PLANNER_CALLS = 2

# Canonical ID format: wu-NNN (zero-padded to 3 digits minimum)
CANONICAL_ID_PREFIX = "wu-"
CANONICAL_ID_PATTERN = re.compile(r"^wu-\d{3,}$")

# Planning lifecycle phases (stored under work_unit_planner.phase in durable state)
PLANNING_PHASES = (
    "PLANNING",           # initial: request to planner sent / about to be sent
    "PROPOSAL_RECEIVED",  # planner returned a response (may be structurally invalid)
    "VALIDATING",         # response parsed; controller validation in progress
    "PLAN_ACCEPTED",      # canonical plan written; ready for Wave 2
    "PLAN_REJECTED",      # plan rejected; not replannable (terminal failure)
)

# Planner failure classification codes (machine-readable; never raw exceptions)
FAILURE_CODES = (
    "malformed_schema",
    "empty_plan",
    "too_many_units",
    "duplicate_alias",
    "duplicate_dependency",
    "unknown_dependency",
    "self_dependency",
    "cycle_detected",
    "unsupported_mode",
    "scope_outside_parent",
    "scope_absolute_or_traversal",
    "scope_drive_qualified",
    "unknown_verifier",
    "verifier_coverage_missing",
    "planner_timeout",
    "planner_error",
    "budget_exhausted",
    "second_schema_correction_refused",
    "second_replan_refused",
    "replan_not_permitted",
    "plan_already_accepted",
    "unapproved_capability",
    "required_capability_removed",
    "unknown_purpose",
    "missing_purpose",
    "unapproved_purpose",
    "missing_capability_policy",
)

# Controller-approved modes (must match work_units.MODES)
APPROVED_MODES = work_units.MODES   # ("read_only", "mutation")


# --------------------------------------------------------------------------- errors

class PlannerError(WorkUnitError):
    """Fail closed.  No state mutation occurs on PlannerError."""

    def __init__(self, message: str, code: str = "planner_error", fields: tuple = ()):
        super().__init__(message)
        self.code = code if code in FAILURE_CODES else "planner_error"
        self.fields = list(fields)


class PlannerTimeout(PlannerError):
    def __init__(self, message: str = "Planner timeout"):
        super().__init__(message, "planner_timeout")


class PlannerBudgetExhausted(PlannerError):
    def __init__(self, message: str = "Budget exhausted before planning"):
        super().__init__(message, "budget_exhausted")


# --------------------------------------------------------------------------- helpers

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False).encode("utf-8")
    ).hexdigest()


def _canonical_id(index: int) -> str:
    """Assign deterministic canonical WorkUnit ID from 1-based sequence index."""
    return f"wu-{index:03d}"


# --------------------------------------------------------------------------- proposal schema

# The proposal unit schema: what the planner may return per unit.
# Field names follow existing project conventions.
PROPOSAL_UNIT_FIELDS = frozenset({
    "alias",        # local planner-assigned alias (NOT the canonical ID)
    "objective",    # human-readable purpose
    "dependencies", # list of alias strings (planner-local, rewritten to canonical IDs)
    "mode",         # selected from approved modes
    "scope",        # dict with allowed_paths / forbidden_paths (only narrowing allowed)
    "verifier_ids", # list of verifier IDs from approved registry
    "required_capabilities", # optional: planner may reference controller-provided capability IDs
    "purpose",      # optional: controller-approved purpose ID
    "purpose_id",   # optional: alias for purpose
})

PROPOSAL_REQUIRED_FIELDS = frozenset({
    "alias", "objective", "dependencies", "mode", "scope", "verifier_ids",
})


def _validate_proposal_unit_schema(unit: Any, index: int) -> None:
    """Validate the raw structural shape of a single proposal unit.

    Raises PlannerError on any structural defect.
    Does NOT check semantic validity (scope containment, verifier IDs, etc.).
    """
    if not isinstance(unit, dict):
        raise PlannerError(
            f"Proposal unit {index} must be a dict",
            "malformed_schema", [f"units[{index}]"]
        )

    missing = PROPOSAL_REQUIRED_FIELDS - unit.keys()
    if missing:
        raise PlannerError(
            f"Proposal unit {index} missing required fields: {sorted(missing)}",
            "malformed_schema", [f"units[{index}].{f}" for f in sorted(missing)]
        )

    extra = unit.keys() - PROPOSAL_UNIT_FIELDS
    if extra:
        raise PlannerError(
            f"Proposal unit {index} has unexpected fields: {sorted(extra)}",
            "malformed_schema", [f"units[{index}].{f}" for f in sorted(extra)]
        )

    # alias: non-empty string, safe identifier pattern
    alias = unit["alias"]
    if not isinstance(alias, str) or not alias or len(alias) > 80:
        raise PlannerError(
            f"Proposal unit {index} alias must be a non-empty string <= 80 chars",
            "malformed_schema", [f"units[{index}].alias"]
        )
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", alias):
        raise PlannerError(
            f"Proposal unit {index} alias {alias!r} contains invalid characters",
            "malformed_schema", [f"units[{index}].alias"]
        )

    # objective
    objective = unit["objective"]
    if not isinstance(objective, str) or not objective.strip() or len(objective) > 2000:
        raise PlannerError(
            f"Proposal unit {index} objective must be a non-empty string <= 2000 chars",
            "malformed_schema", [f"units[{index}].objective"]
        )

    # dependencies: list of strings
    deps = unit["dependencies"]
    if not isinstance(deps, list):
        raise PlannerError(
            f"Proposal unit {index} dependencies must be a list",
            "malformed_schema", [f"units[{index}].dependencies"]
        )
    for d in deps:
        if not isinstance(d, str) or not d:
            raise PlannerError(
                f"Proposal unit {index} dependency item must be a non-empty string",
                "malformed_schema", [f"units[{index}].dependencies"]
            )
    if len(deps) != len(set(deps)):
        raise PlannerError(
            f"Proposal unit {index} has duplicate dependencies",
            "duplicate_dependency", [f"units[{index}].dependencies"]
        )

    # mode
    mode = unit["mode"]
    if not isinstance(mode, str):
        raise PlannerError(
            f"Proposal unit {index} mode must be a string",
            "malformed_schema", [f"units[{index}].mode"]
        )

    # scope: structural check only
    scope = unit["scope"]
    if not isinstance(scope, dict):
        raise PlannerError(
            f"Proposal unit {index} scope must be a dict",
            "malformed_schema", [f"units[{index}].scope"]
        )
    if set(scope) != {"allowed_paths", "forbidden_paths"}:
        raise PlannerError(
            f"Proposal unit {index} scope must have exactly allowed_paths and forbidden_paths",
            "malformed_schema", [f"units[{index}].scope"]
        )
    for key in ("allowed_paths", "forbidden_paths"):
        if not isinstance(scope[key], list):
            raise PlannerError(
                f"Proposal unit {index} scope.{key} must be a list",
                "malformed_schema", [f"units[{index}].scope.{key}"]
            )
        for p in scope[key]:
            if not isinstance(p, str) or not p:
                raise PlannerError(
                    f"Proposal unit {index} scope.{key} item must be a non-empty string",
                    "malformed_schema", [f"units[{index}].scope.{key}"]
                )

    # verifier_ids
    vids = unit["verifier_ids"]
    if not isinstance(vids, list):
        raise PlannerError(
            f"Proposal unit {index} verifier_ids must be a list",
            "malformed_schema", [f"units[{index}].verifier_ids"]
        )
    for v in vids:
        if not isinstance(v, str) or not v:
            raise PlannerError(
                f"Proposal unit {index} verifier_ids item must be a non-empty string",
                "malformed_schema", [f"units[{index}].verifier_ids"]
            )

    # required_capabilities (optional list of strings)
    if "required_capabilities" in unit:
        rcaps = unit["required_capabilities"]
        if not isinstance(rcaps, list):
            raise PlannerError(
                f"Proposal unit {index} required_capabilities must be a list",
                "malformed_schema", [f"units[{index}].required_capabilities"]
            )
        for c in rcaps:
            if not isinstance(c, str) or not c:
                raise PlannerError(
                    f"Proposal unit {index} required_capabilities item must be a non-empty string",
                    "malformed_schema", [f"units[{index}].required_capabilities"]
                )

    # purpose / purpose_id (optional controller purpose selection)
    for pkey in ("purpose", "purpose_id"):
        if pkey in unit:
            pval = unit[pkey]
            if not isinstance(pval, str) or not pval or len(pval) > 80:
                raise PlannerError(
                    f"Proposal unit {index} {pkey} must be a non-empty string <= 80 chars",
                    "malformed_schema", [f"units[{index}].{pkey}"]
                )
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", pval):
                raise PlannerError(
                    f"Proposal unit {index} {pkey} {pval!r} contains invalid characters",
                    "malformed_schema", [f"units[{index}].{pkey}"]
                )


def validate_proposal_schema(proposal: Any) -> list[dict]:
    """Structural validation of a raw planner proposal.

    Returns the list of proposal units.
    Raises PlannerError with code 'malformed_schema' or 'empty_plan' or 'too_many_units'.
    """
    if not isinstance(proposal, dict):
        raise PlannerError("Proposal must be a dict", "malformed_schema", ["root"])

    units = proposal.get("units")
    if not isinstance(units, list):
        raise PlannerError("Proposal must contain a 'units' list", "malformed_schema", ["units"])

    # Reject extra keys at top level
    extra_keys = set(proposal.keys()) - {"units"}
    if extra_keys:
        raise PlannerError(
            f"Proposal has unexpected top-level fields: {sorted(extra_keys)}",
            "malformed_schema", sorted(extra_keys)
        )

    if len(units) == 0:
        raise PlannerError("Plan must contain at least one unit", "empty_plan", ["units"])

    if len(units) > MAX_PLAN_UNITS:
        raise PlannerError(
            f"Plan exceeds maximum of {MAX_PLAN_UNITS} units (got {len(units)}); "
            "do not silently truncate",
            "too_many_units", ["units"]
        )

    for i, unit in enumerate(units):
        _validate_proposal_unit_schema(unit, i)

    return units


# --------------------------------------------------------------------------- purpose / capability policy helpers

def _extract_allowed_purposes(capability_policy: Any) -> dict[str, Any] | None:
    if not isinstance(capability_policy, dict):
        return None
    for k in ("allowed_unit_purposes", "allowed_purposes", "purposes"):
        if k in capability_policy and isinstance(capability_policy[k], dict):
            return capability_policy[k]
    meta_keys = {
        "required_capabilities", "capabilities", "paths", "aliases",
        "default", "semantic_coverage_required", "require_capability_policy"
    }
    if not any(k in capability_policy for k in meta_keys) and capability_policy:
        return capability_policy
    return None


def _resolve_purpose_capabilities(pval: Any) -> list[str]:
    if isinstance(pval, str):
        return [pval]
    if isinstance(pval, (list, tuple, set)):
        return [c for c in pval if isinstance(c, str)]
    if isinstance(pval, dict):
        for k in ("required_capabilities", "capabilities"):
            v = pval.get(k)
            if isinstance(v, str):
                return [v]
            if isinstance(v, (list, tuple, set)):
                return [c for c in v if isinstance(c, str)]
    return []


# --------------------------------------------------------------------------- planner context (controller-created)

def build_planner_context(
    *,
    task: str,
    parent_scope: dict,
    approved_verifiers: dict,
    max_units: int = MAX_PLAN_UNITS,
    planning_constraints: dict | None = None,
    task_evidence: dict | None = None,
    capability_policy: Any | None = None,
) -> dict:
    """Build the bounded controller-created input for the planner.

    The context is a plain dict that may be serialized and passed to
    a planner implementation.  It intentionally omits:
    - durable state internals / secrets
    - executable verifier commands (planner gets IDs + description only)
    - trust evidence
    - filesystem paths outside parent_scope
    """
    if not isinstance(task, str) or not task.strip():
        raise PlannerError("task must be a non-empty string", "planner_error")
    if not isinstance(parent_scope, dict):
        raise PlannerError("parent_scope must be a dict", "planner_error")
    if not isinstance(approved_verifiers, dict):
        raise PlannerError("approved_verifiers must be a dict", "planner_error")

    # Expose only verifier IDs + human-readable descriptions (NOT executable commands)
    verifier_manifest = {}
    all_approved_caps = set()
    for vid, defn in approved_verifiers.items():
        if not isinstance(vid, str) or not isinstance(defn, dict):
            raise PlannerError(f"Invalid verifier registry entry for {vid!r}", "planner_error")
        v_entry = {
            "id": vid,
            "description": defn.get("description", "deterministic controller verifier"),
        }
        if "capabilities" in defn:
            v_entry["capabilities"] = list(defn["capabilities"])
            for c in defn["capabilities"]:
                if isinstance(c, str):
                    all_approved_caps.add(c)
        verifier_manifest[vid] = v_entry

    allowed_purposes_for_ctx = None
    if capability_policy is not None:
        if isinstance(capability_policy, str):
            all_approved_caps.add(capability_policy)
        elif isinstance(capability_policy, (list, tuple, set)):
            for c in capability_policy:
                if isinstance(c, str):
                    all_approved_caps.add(c)
        elif isinstance(capability_policy, dict):
            allowed_p = _extract_allowed_purposes(capability_policy)
            if allowed_p:
                allowed_purposes_for_ctx = copy.deepcopy(allowed_p)
                for pid, pdefn in allowed_p.items():
                    for c in _resolve_purpose_capabilities(pdefn):
                        all_approved_caps.add(c)
            for k in ("required_capabilities", "capabilities", "default"):
                val = capability_policy.get(k)
                if isinstance(val, str):
                    all_approved_caps.add(val)
                elif isinstance(val, (list, tuple, set)):
                    for c in val:
                        if isinstance(c, str):
                            all_approved_caps.add(c)
            for path_pat, p_caps in capability_policy.get("paths", {}).items():
                for c in _resolve_purpose_capabilities(p_caps):
                    all_approved_caps.add(c)
            for ali, a_caps in capability_policy.get("aliases", {}).items():
                for c in _resolve_purpose_capabilities(a_caps):
                    all_approved_caps.add(c)

    bounded_max = min(max_units, MAX_PLAN_UNITS)

    ctx: dict = {
        "schema_version": WAVE3_SCHEMA_VERSION,
        "task": task.strip(),
        "parent_scope": copy.deepcopy(parent_scope),
        "approved_verifier_ids": sorted(verifier_manifest.keys()),
        "verifier_manifest": verifier_manifest,
        "approved_capabilities": sorted(all_approved_caps),
        "approved_modes": list(APPROVED_MODES),
        "max_units": bounded_max,
        "planning_constraints": copy.deepcopy(planning_constraints or {}),
        "task_evidence": copy.deepcopy(task_evidence or {}),
        "capability_policy": copy.deepcopy(capability_policy) if capability_policy is not None else None,
    }
    if allowed_purposes_for_ctx:
        ctx["allowed_unit_purposes"] = allowed_purposes_for_ctx
    return ctx


# --------------------------------------------------------------------------- semantic validation (pure / deterministic)

def _validate_scope_narrowing(
    proposal_scope: dict,
    parent_scope: dict,
    alias: str,
    index: int,
) -> dict:
    """Validate that proposal scope only NARROWS parent scope, never broadens it.

    Returns normalized (allowed_paths, forbidden_paths) dict.
    Raises PlannerError on any violation.
    """
    prop_allowed_raw = proposal_scope.get("allowed_paths", [])
    prop_forbidden_raw = proposal_scope.get("forbidden_paths", [])

    if not prop_allowed_raw:
        raise PlannerError(
            f"Proposal unit {alias!r}: scope.allowed_paths must not be empty",
            "scope_outside_parent",
            [f"units[{index}].scope.allowed_paths"],
        )

    parent_allowed_raw = parent_scope.get("allowed_paths", [])
    if not parent_allowed_raw:
        raise PlannerError(
            "Parent scope allowed_paths must not be empty; planner cannot invent unrestricted scope",
            "scope_outside_parent",
        )

    # Normalize parent scope
    try:
        parent_allowed = [work_units.normalize_entry(p) for p in parent_allowed_raw]
        parent_forbidden = [work_units.normalize_entry(p) for p in parent_scope.get("forbidden_paths", [])]
    except WorkUnitError as exc:
        raise PlannerError(f"Parent scope invalid: {exc}", "scope_outside_parent") from exc

    # Normalize proposal paths
    norm_allowed = []
    for p in prop_allowed_raw:
        # Check absolute, drive-qualified, or traversal before generic parsing
        p_clean = p.replace("\\", "/")
        if p_clean.startswith("/") or re.match(r"^[A-Za-z]:", p_clean) or p_clean.startswith("//"):
            raise PlannerError(
                f"Proposal unit {alias!r}: scope path {p!r} is absolute or drive-qualified",
                "scope_absolute_or_traversal",
                [f"units[{index}].scope.allowed_paths"],
            )
        parts = [part for part in p_clean.split("/") if part not in ("", ".")]
        if any(part == ".." or ":" in part for part in parts):
            raise PlannerError(
                f"Proposal unit {alias!r}: scope path {p!r} contains traversal or invalid segment",
                "scope_absolute_or_traversal",
                [f"units[{index}].scope.allowed_paths"],
            )
        try:
            norm_p = work_units.normalize_entry(p)
        except WorkUnitError as exc:
            raise PlannerError(
                f"Proposal unit {alias!r}: invalid scope allowed path {p!r}: {exc}",
                "scope_outside_parent",
                [f"units[{index}].scope.allowed_paths"],
            ) from exc
        norm_allowed.append(norm_p)

    norm_forbidden = []
    for p in prop_forbidden_raw:
        p_clean = p.replace("\\", "/")
        if p_clean.startswith("/") or re.match(r"^[A-Za-z]:", p_clean) or p_clean.startswith("//"):
            raise PlannerError(
                f"Proposal unit {alias!r}: forbidden scope path {p!r} is absolute or drive-qualified",
                "scope_absolute_or_traversal",
                [f"units[{index}].scope.forbidden_paths"],
            )
        parts = [part for part in p_clean.split("/") if part not in ("", ".")]
        if any(part == ".." or ":" in part for part in parts):
            raise PlannerError(
                f"Proposal unit {alias!r}: forbidden scope path {p!r} contains traversal or invalid segment",
                "scope_absolute_or_traversal",
                [f"units[{index}].scope.forbidden_paths"],
            )
        try:
            norm_p = work_units.normalize_entry(p)
        except WorkUnitError as exc:
            raise PlannerError(
                f"Proposal unit {alias!r}: invalid scope forbidden path {p!r}: {exc}",
                "scope_outside_parent",
                [f"units[{index}].scope.forbidden_paths"],
            ) from exc
        norm_forbidden.append(norm_p)

    # Check scope containment: proposal allowed paths must be within parent allowed paths
    for p in norm_allowed:
        if not any(work_units.covers(pa, p) for pa in parent_allowed):
            raise PlannerError(
                f"Proposal unit {alias!r}: allowed path {p!r} escapes parent scope",
                "scope_outside_parent",
                [f"units[{index}].scope.allowed_paths"],
            )

    # Proposed allowed paths must not overlap parent forbidden paths
    for p in norm_allowed:
        for pf in parent_forbidden:
            if work_units.overlaps(pf, p):
                raise PlannerError(
                    f"Proposal unit {alias!r}: allowed path {p!r} overlaps parent forbidden path {pf!r}",
                    "scope_outside_parent",
                    [f"units[{index}].scope.allowed_paths"],
                )

    # Proposed allowed and forbidden must not conflict
    for a in norm_allowed:
        for f in norm_forbidden:
            if work_units.overlaps(a, f):
                raise PlannerError(
                    f"Proposal unit {alias!r}: allowed path {a!r} overlaps forbidden path {f!r}",
                    "scope_outside_parent",
                    [f"units[{index}].scope"],
                )

    return {
        "allowed_paths": norm_allowed,
        "forbidden_paths": norm_forbidden,
    }


def resolve_unit_required_capabilities(
    unit: dict,
    index: int,
    capability_policy: Any,
    approved_verifiers: dict,
    alias: str,
    controller_limits: dict | None = None,
) -> list[str]:
    """Deterministically determine controller-owned required capabilities for a proposal unit (Fix C, Fix 2 & Case 2G).

    AUTHORITY INVARIANT:
    Required capabilities are determined exclusively by controller-owned structured policy.
    They must NEVER be inferred from planner objective prose or regex matching on objective.
    The planner may select only from controller-approved bounded purposes/capability classes.

    CASE 2G INVARIANT:
    For Wave 3 auto-planned mutation units:
    - semantic verification authority must come from controller-owned capability policy.
    - if no applicable controller capability policy exists, the unit is NOT_PROVEN and the plan must be rejected.
    - it is NOT acceptable to silently fall back to required_capabilities = [] for an auto-planned mutation unit.
    - omitting purpose on a mutation unit when policy uses purposes must NOT silently accept the plan.
    - supplying a purpose that has no matching controller capability policy must NOT silently accept the plan.
    - read-only units preserve their existing validated semantics (path coverage) unless the policy explicitly requires capabilities for them.
    """
    mode = unit.get("mode", "mutation")
    is_mutation = (mode == "mutation")

    # 1. Collect all known approved capabilities from registry and policy
    known_capabilities: set[str] = set()
    for vid, defn in approved_verifiers.items():
        if isinstance(defn, dict):
            for c in defn.get("capabilities", []):
                if isinstance(c, str):
                    known_capabilities.add(c)
    if capability_policy is not None:
        if isinstance(capability_policy, str):
            known_capabilities.add(capability_policy)
        elif isinstance(capability_policy, (list, tuple, set)):
            for c in capability_policy:
                if isinstance(c, str):
                    known_capabilities.add(c)
        elif isinstance(capability_policy, dict):
            for k in ("required_capabilities", "capabilities", "default"):
                val = capability_policy.get(k)
                if isinstance(val, str):
                    known_capabilities.add(val)
                elif isinstance(val, (list, tuple, set)):
                    for c in val:
                        if isinstance(c, str):
                            known_capabilities.add(c)
            for p_caps in capability_policy.get("paths", {}).values():
                for c in _resolve_purpose_capabilities(p_caps):
                    known_capabilities.add(c)
            for a_caps in capability_policy.get("aliases", {}).values():
                for c in _resolve_purpose_capabilities(a_caps):
                    known_capabilities.add(c)
            allowed_p = _extract_allowed_purposes(capability_policy)
            if allowed_p:
                for pdefn in allowed_p.values():
                    for c in _resolve_purpose_capabilities(pdefn):
                        known_capabilities.add(c)

    # 2. Check for planner tampering / unapproved capability injection early
    proposed_caps_raw = unit.get("required_capabilities")
    if proposed_caps_raw is not None:
        if not isinstance(proposed_caps_raw, list):
            raise PlannerError(
                f"Proposal unit {alias!r}: required_capabilities must be a list",
                "malformed_schema",
                [f"units[{index}].required_capabilities"],
            )
        proposed_caps = set(proposed_caps_raw)
        unapproved = proposed_caps - known_capabilities
        if unapproved:
            raise PlannerError(
                f"Proposal unit {alias!r} specifies unapproved capability IDs: {sorted(unapproved)}",
                "unapproved_capability",
                [f"units[{index}].required_capabilities"],
            )

    # 3. Case 2G: Auto-planned mutation unit REQUIRES an applicable controller capability policy
    if is_mutation and not capability_policy:
        raise PlannerError(
            f"Auto-planned mutation unit {alias!r} requires controller capability policy",
            "verifier_coverage_missing",
            [f"units[{index}]"],
        )

    # 4. Derive required capabilities from controller policy
    required: set[str] = set()
    allowed_purposes = _extract_allowed_purposes(capability_policy) if capability_policy is not None else None

    if allowed_purposes:
        for pid, pdefn in allowed_purposes.items():
            for c in _resolve_purpose_capabilities(pdefn):
                known_capabilities.add(c)

        selected_purpose = unit.get("purpose") or unit.get("purpose_id")
        if selected_purpose is not None:
            if not isinstance(selected_purpose, str) or not selected_purpose:
                raise PlannerError(
                    f"Proposal unit {alias!r} purpose must be a non-empty string",
                    "malformed_schema",
                    [f"units[{index}].purpose"],
                )
            if selected_purpose not in allowed_purposes:
                raise PlannerError(
                    f"Proposal unit {alias!r} specifies unknown purpose {selected_purpose!r}; "
                    f"allowed purposes: {sorted(allowed_purposes.keys())}",
                    "unknown_purpose",
                    [f"units[{index}].purpose"],
                )
            for c in _resolve_purpose_capabilities(allowed_purposes[selected_purpose]):
                required.add(c)
        else:
            # Purpose omitted: for mutation unit, this MUST fail closed
            if is_mutation:
                raise PlannerError(
                    f"Proposal unit {alias!r} missing required purpose selection; "
                    f"allowed purposes: {sorted(allowed_purposes.keys())}",
                    "missing_purpose",
                    [f"units[{index}].purpose"],
                )

    if capability_policy is not None and not allowed_purposes:
        selected_purpose = unit.get("purpose") or unit.get("purpose_id")
        if selected_purpose is not None:
            raise PlannerError(
                f"Proposal unit {alias!r} specifies purpose {selected_purpose!r} but controller capability policy defines no approved purposes",
                "unknown_purpose",
                [f"units[{index}].purpose"],
            )

        if isinstance(capability_policy, str):
            required.add(capability_policy)
        elif isinstance(capability_policy, (list, tuple, set)):
            for c in capability_policy:
                if isinstance(c, str):
                    required.add(c)
        elif callable(capability_policy):
            res = capability_policy(unit)
            if isinstance(res, str):
                required.add(res)
            elif isinstance(res, (list, tuple, set)):
                for c in res:
                    if isinstance(c, str):
                        required.add(c)
        elif isinstance(capability_policy, dict):
            if "required_capabilities" in capability_policy:
                rc = capability_policy["required_capabilities"]
                if isinstance(rc, str):
                    required.add(rc)
                elif isinstance(rc, (list, tuple, set)):
                    required.update(rc)
            if "capabilities" in capability_policy:
                cp = capability_policy["capabilities"]
                if isinstance(cp, str):
                    required.add(cp)
                elif isinstance(cp, (list, tuple, set)):
                    required.update(cp)
            # NO REGEX MATCHING ON OBJECTIVE TEXT! Objective is descriptive prose only.
            paths = unit.get("scope", {}).get("allowed_paths", [])
            for path_pat, caps in capability_policy.get("paths", {}).items():
                if any(work_units.covers(path_pat, p) for p in paths):
                    if isinstance(caps, (list, tuple, set)):
                        required.update(caps)
                    elif isinstance(caps, str):
                        required.add(caps)
            eff_alias = unit.get("alias") or unit.get("unit_id") or alias
            if eff_alias and eff_alias in capability_policy.get("aliases", {}):
                ali_caps = capability_policy["aliases"][eff_alias]
                if isinstance(ali_caps, (list, tuple, set)):
                    required.update(ali_caps)
                elif isinstance(ali_caps, str):
                    required.add(ali_caps)
            if not required and "default" in capability_policy:
                df = capability_policy.get("default", [])
                if isinstance(df, str):
                    required.add(df)
                elif isinstance(df, (list, tuple, set)):
                    required.update(df)

    for c in required:
        known_capabilities.add(c)

    # 4. Case 2G: Mutation unit MUST resolve non-empty required_capabilities
    if is_mutation and not required:
        raise PlannerError(
            f"Auto-planned mutation unit {alias!r} must resolve non-empty required capabilities from capability policy",
            "verifier_coverage_missing",
            [f"units[{index}]"],
        )

    # 6. Check proposed_caps against derived required capabilities
    if proposed_caps_raw is not None:
        proposed_caps = set(proposed_caps_raw)
        if required and not required.issubset(proposed_caps):
            removed = required - proposed_caps
            raise PlannerError(
                f"Proposal unit {alias!r} attempts to remove controller-required capability: {sorted(removed)}",
                "required_capability_removed",
                [f"units[{index}].required_capabilities"],
            )
        # Planner cannot invent capabilities beyond controller policy
        extra_proposed = proposed_caps - required
        if extra_proposed and not allowed_purposes:
            raise PlannerError(
                f"Proposal unit {alias!r} specifies unapproved capability IDs: {sorted(extra_proposed)}",
                "unapproved_capability",
                [f"units[{index}].required_capabilities"],
            )

    # 6. Wave 3 requirement: if unit explicitly requires semantic coverage, capability policy must not be absent
    semantic_required = (
        bool(unit.get("semantic_coverage_required"))
        or bool(unit.get("requires_semantic_coverage"))
        or (bool(controller_limits) and (
            bool(controller_limits.get("semantic_coverage_required"))
            or bool(controller_limits.get("require_capability_policy"))
        ))
    )
    if semantic_required and not required:
        raise PlannerError(
            f"Proposal unit {alias!r}: controller capability policy absent for unit that requires semantic coverage",
            "verifier_coverage_missing",
            [f"units[{index}]"],
        )

    return sorted(required)


def _validate_verifier_coverage(
    verifier_ids: list[str],
    mode: str,
    scope: dict,
    approved_verifiers: dict,
    alias: str,
    index: int,
    required_capabilities: list[str] | None = None,
) -> None:
    """Verify that the proposed verifier IDs are from the approved registry
    and provide adequate controller-owned coverage for mode, scope, and capabilities (Fix 7 & Fix C).
    """
    if not verifier_ids:
        raise PlannerError(
            f"Proposal unit {alias!r}: verifier_ids must not be empty",
            "verifier_coverage_missing",
            [f"units[{index}].verifier_ids"],
        )

    seen = set()
    for vid in verifier_ids:
        if vid in seen:
            raise PlannerError(
                f"Proposal unit {alias!r}: duplicate verifier_id {vid!r}",
                "verifier_coverage_missing",
                [f"units[{index}].verifier_ids"],
            )
        seen.add(vid)

        if vid not in approved_verifiers:
            raise PlannerError(
                f"Proposal unit {alias!r}: unknown verifier_id {vid!r}; "
                "planner may only choose from approved verifier IDs",
                "unknown_verifier",
                [f"units[{index}].verifier_ids"],
            )
        defn = approved_verifiers[vid]
        if not isinstance(defn, dict) or "argv" not in defn:
            raise PlannerError(
                f"Verifier {vid!r} has malformed registry definition",
                "unknown_verifier",
                [f"units[{index}].verifier_ids"],
            )

        # Check mode compatibility (Fix 7)
        v_modes = defn.get("modes")
        if v_modes is not None:
            if not isinstance(v_modes, (list, tuple, set)):
                raise PlannerError(
                    f"Verifier {vid!r} has malformed modes definition",
                    "unknown_verifier",
                    [f"units[{index}].verifier_ids"],
                )
            if mode not in v_modes:
                raise PlannerError(
                    f"Proposal unit {alias!r}: verifier {vid!r} does not support mode {mode!r}; "
                    f"supported modes: {list(v_modes)}",
                    "verifier_coverage_missing",
                    [f"units[{index}].verifier_ids", f"units[{index}].mode"],
                )

    # Check path / scope coverage (Fix 7)
    # Every path in scope["allowed_paths"] must be covered by at least one selected verifier
    allowed_paths = scope.get("allowed_paths", [])
    for p in allowed_paths:
        covered = False
        for vid in verifier_ids:
            defn = approved_verifiers[vid]
            v_paths = defn.get("paths")
            if v_paths is None:
                v_paths = defn.get("covered_paths")
            if v_paths is None:
                continue
            if not isinstance(v_paths, (list, tuple, set)):
                continue
            for vp in v_paths:
                if not isinstance(vp, str):
                    continue
                if vp in ("*", "."):
                    covered = True
                    break
                try:
                    norm_vp = work_units.normalize_entry(vp)
                    if work_units.covers(norm_vp, p):
                        covered = True
                        break
                except Exception:
                    if vp.lower() == p.lower():
                        covered = True
                        break
            if covered:
                break
        if not covered:
            raise PlannerError(
                f"Proposal unit {alias!r}: allowed path {p!r} is not covered by any approved verifier in {verifier_ids}",
                "verifier_coverage_missing",
                [f"units[{index}].verifier_ids", f"units[{index}].scope.allowed_paths"],
            )

    # Check capability coverage (Fix C)
    if required_capabilities:
        provided_caps: set[str] = set()
        for vid in verifier_ids:
            defn = approved_verifiers.get(vid, {})
            caps = defn.get("capabilities", [])
            if isinstance(caps, (list, tuple, set)):
                for c in caps:
                    if isinstance(c, str):
                        provided_caps.add(c)
            elif isinstance(caps, str):
                provided_caps.add(caps)

        missing_caps = [c for c in required_capabilities if c not in provided_caps]
        if missing_caps:
            raise PlannerError(
                f"Proposal unit {alias!r}: missing required capability coverage: {missing_caps}; "
                f"provided capabilities: {sorted(provided_caps)}",
                "verifier_coverage_missing",
                [f"units[{index}].verifier_ids", "capabilities"],
            )


def _topological_sort(
    units: list[dict],
    alias_to_index: dict[str, int],
) -> list[int]:
    """Deterministic topological sort of proposal units by dependency.

    Uses proposal order as stable tie-break for units with no dependency ordering.
    Returns list of indices into `units` in valid execution order.
    Raises PlannerError on cycle or unknown dependency.
    """
    n = len(units)
    in_degree = [0] * n
    reverse_adj: dict[int, list[int]] = {i: [] for i in range(n)}

    for i, unit in enumerate(units):
        deps = unit["dependencies"]
        if len(deps) != len(set(deps)):
            raise PlannerError(
                f"Unit {unit['alias']!r} has duplicate dependencies",
                "duplicate_dependency",
                [f"units[{i}].dependencies"],
            )
        for dep_alias in deps:
            if dep_alias not in alias_to_index:
                raise PlannerError(
                    f"Unit {unit['alias']!r} depends on unknown alias {dep_alias!r}",
                    "unknown_dependency",
                    [f"units[{i}].dependencies"],
                )
            dep_idx = alias_to_index[dep_alias]
            if dep_idx == i:
                raise PlannerError(
                    f"Unit {unit['alias']!r} has a self-dependency",
                    "self_dependency",
                    [f"units[{i}].dependencies"],
                )
            in_degree[i] += 1
            reverse_adj[dep_idx].append(i)

    # Kahn's algorithm: start with all units with in_degree == 0
    # Stable queue sorted by proposal index
    queue = sorted([i for i in range(n) if in_degree[i] == 0])
    result = []

    while queue:
        node = queue.pop(0)
        result.append(node)
        newly_ready = []
        for dependent in reverse_adj[node]:
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                newly_ready.append(dependent)
        newly_ready.sort()
        queue.extend(newly_ready)
        queue.sort()

    if len(result) != n:
        in_cycle = [units[i]["alias"] for i in range(n) if i not in result]
        raise PlannerError(
            f"Dependency cycle detected among: {in_cycle}",
            "cycle_detected",
            [f"units[{i}].dependencies" for i in range(n) if i not in result],
        )

    return result


# --------------------------------------------------------------------------- canonical plan construction

def validate_and_build_canonical_plan(
    proposal_units: list[dict],
    *,
    parent_scope: dict,
    approved_verifiers: dict,
    controller_limits: dict | None = None,
    capability_policy: Any | None = None,
) -> tuple[list[dict], list[str]]:
    """Convert validated proposal units to canonical controller-owned WorkUnit specs.

    This is the central authority function.  It:
    1. Validates duplicate aliases
    2. Validates modes
    3. Validates scope narrowing (never broadening)
    4. Validates verifier coverage from approved registry and controller capabilities
    5. Builds topological order (stable, deterministic)
    6. Assigns canonical IDs (wu-001, wu-002, ...)
    7. Rewrites alias-based dependencies to canonical IDs
    8. Builds final WorkUnit specs compatible with work_units.validate_unit_spec

    Returns (canonical_specs, canonical_sequence).
    Raises PlannerError on any violation.
    """
    if not proposal_units:
        raise PlannerError("Empty plan", "empty_plan", ["units"])

    if len(proposal_units) > MAX_PLAN_UNITS:
        raise PlannerError(
            f"Plan exceeds maximum of {MAX_PLAN_UNITS} units",
            "too_many_units", ["units"]
        )

    # --- Step 1: Check for duplicate aliases ---
    aliases_seen: set[str] = set()
    for i, unit in enumerate(proposal_units):
        alias = unit["alias"]
        if alias in aliases_seen:
            raise PlannerError(
                f"Duplicate proposal alias {alias!r}",
                "duplicate_alias",
                [f"units[{i}].alias"]
            )
        aliases_seen.add(alias)

    alias_to_index: dict[str, int] = {u["alias"]: i for i, u in enumerate(proposal_units)}

    # --- Step 2: Validate modes ---
    for i, unit in enumerate(proposal_units):
        mode = unit["mode"]
        if mode not in APPROVED_MODES:
            raise PlannerError(
                f"Proposal unit {unit['alias']!r}: unsupported mode {mode!r}; "
                f"approved modes are {list(APPROVED_MODES)}",
                "unsupported_mode",
                [f"units[{i}].mode"]
            )

    # --- Step 3: Validate scope narrowing ---
    for i, unit in enumerate(proposal_units):
        _validate_scope_narrowing(
            unit["scope"], parent_scope, unit["alias"], i
        )

    # --- Step 4: Validate verifier coverage and capabilities (Fix 7 & Fix C) ---
    eff_cap_policy = capability_policy or (controller_limits or {}).get("capability_policy") or (controller_limits or {}).get("required_capabilities")
    unit_req_caps = {}
    for i, unit in enumerate(proposal_units):
        req_caps = resolve_unit_required_capabilities(
            unit, i, eff_cap_policy, approved_verifiers, unit["alias"],
            controller_limits=controller_limits,
        )
        unit_req_caps[unit["alias"]] = req_caps
        _validate_verifier_coverage(
            unit["verifier_ids"], unit["mode"], unit["scope"], approved_verifiers, unit["alias"], i,
            required_capabilities=req_caps,
        )

    # --- Step 5: Topological sort (cycle detection, unknown deps, self deps) ---
    topo_order = _topological_sort(proposal_units, alias_to_index)

    # --- Step 6: Assign canonical IDs in topological order ---
    alias_to_canonical: dict[str, str] = {}
    for seq_pos, prop_idx in enumerate(topo_order):
        alias = proposal_units[prop_idx]["alias"]
        canonical_id = _canonical_id(seq_pos + 1)
        alias_to_canonical[alias] = canonical_id

    # --- Step 7: Build canonical WorkUnit specs ---
    canonical_specs: list[dict] = []
    for seq_pos, prop_idx in enumerate(topo_order):
        unit = proposal_units[prop_idx]
        alias = unit["alias"]
        canonical_id = alias_to_canonical[alias]

        # Rewrite dependency aliases to canonical IDs
        canonical_deps = []
        for dep_alias in unit["dependencies"]:
            dep_canonical = alias_to_canonical.get(dep_alias)
            if dep_canonical is None:
                raise PlannerError(
                    f"Unit {alias!r}: dependency alias {dep_alias!r} has no canonical mapping",
                    "unknown_dependency",
                    [f"units[{prop_idx}].dependencies"]
                )
            canonical_deps.append(dep_canonical)

        # Normalize scope
        norm_scope = _validate_scope_narrowing(
            unit["scope"], parent_scope, alias, prop_idx
        )

        # Build limits: controller-assigned conservative defaults
        limits = _build_canonical_limits(controller_limits)

        spec = {
            "unit_id": canonical_id,
            "objective": unit["objective"].strip(),
            "dependencies": canonical_deps,
            "mode": unit["mode"],
            "scope": norm_scope,
            "verifier_ids": list(unit["verifier_ids"]),
            "evidence_inputs": [],   # controller derives; planner cannot forge
            "limits": limits,
            "required_capabilities": unit_req_caps.get(alias, []),
        }
        purpose_val = unit.get("purpose") or unit.get("purpose_id")
        if purpose_val:
            spec["purpose"] = purpose_val

        # Validate against Wave 1 schema
        try:
            work_units.validate_unit_spec(spec)
        except WorkUnitError as exc:
            raise PlannerError(
                f"Canonical spec for {alias!r} ({canonical_id}) failed Wave 1 validation: {exc}",
                "malformed_schema",
                [canonical_id]
            ) from exc

        canonical_specs.append(spec)

    canonical_sequence = [spec["unit_id"] for spec in canonical_specs]

    # Final plan-level validation
    try:
        work_units.validate_unit_plan(
            canonical_specs,
            parent_scope=parent_scope,
            verifier_registry=approved_verifiers,
        )
    except WorkUnitError as exc:
        raise PlannerError(
            f"Canonical plan failed Wave 1 plan validation: {exc}",
            "malformed_schema",
        ) from exc

    return canonical_specs, canonical_sequence


def validate_canonical_plan(
    canonical_specs: list[dict],
    canonical_sequence: list[str],
    *,
    parent_scope: dict | None = None,
    approved_verifiers: dict | None = None,
    digest: str | None = None,
    capability_policy: Any | None = None,
) -> None:
    """Validate that an accepted plan adheres completely to controller canonical rules (Fix 1).

    Validates:
    - Canonical WorkUnit IDs follow controller canonical ID rules (wu-NNN pattern, >= 3 digits)
    - Every accepted WorkUnit validates under Wave 1 contracts
    - Accepted sequence is complete (1:1 match with specs, no duplicates, no unknown entries)
    - Order of canonical_specs matches canonical_sequence
    - Dependency graph is valid, acyclic, and dependencies reference accepted canonical IDs
    - All dependencies strictly precede the dependent unit in canonical_sequence
    - Canonical limits remain controller-owned (max_attempts <= 2, timeout_seconds <= 300)
    - Scope remains within original controller authority (narrowing of parent_scope)
    - Verifier IDs remain approved and provide required mode/scope coverage
    - Accepted-plan digest/provenance matches persisted canonical representation (if provided)

    Raises PlannerError or WorkUnitError on any violation (fails closed).
    """
    if not isinstance(canonical_specs, list) or not canonical_specs:
        raise PlannerError("Canonical plan must be a non-empty list of specs", "malformed_schema")
    if len(canonical_specs) > MAX_PLAN_UNITS:
        raise PlannerError(f"Canonical plan exceeds maximum of {MAX_PLAN_UNITS} units", "too_many_units")
    if not isinstance(canonical_sequence, list) or not canonical_sequence:
        raise PlannerError("Canonical sequence must be a non-empty list of IDs", "malformed_schema")
    if len(canonical_sequence) != len(canonical_specs):
        raise PlannerError("Canonical sequence length does not match specs length", "malformed_schema")
    if len(canonical_sequence) != len(set(canonical_sequence)):
        raise PlannerError("Canonical sequence contains duplicate unit IDs", "malformed_schema")

    spec_ids = []
    for idx, s in enumerate(canonical_specs):
        if not isinstance(s, dict):
            raise PlannerError(f"Spec at index {idx} must be a dict", "malformed_schema")
        uid = s.get("unit_id")
        if not isinstance(uid, str) or not CANONICAL_ID_PATTERN.match(uid):
            raise PlannerError(
                f"Unit ID {uid!r} is not a valid canonical ID; must match {CANONICAL_ID_PATTERN.pattern}",
                "malformed_schema"
            )
        spec_ids.append(uid)

    if set(canonical_sequence) != set(spec_ids):
        raise PlannerError("Canonical sequence IDs do not match canonical specs IDs", "malformed_schema")

    seen_ids: set[str] = set()
    id_to_seq_idx = {uid: idx for idx, uid in enumerate(canonical_sequence)}

    for idx, spec in enumerate(canonical_specs):
        uid = spec["unit_id"]
        if uid in seen_ids:
            raise PlannerError(f"Duplicate canonical unit ID: {uid!r}", "duplicate_alias")
        seen_ids.add(uid)

        # Sequence order consistency
        if canonical_sequence[idx] != uid:
            raise PlannerError(
                f"Spec order at {idx} ({uid}) does not match sequence order ({canonical_sequence[idx]})",
                "malformed_schema"
            )

        # Wave 1 spec contract validation
        try:
            work_units.validate_unit_spec(spec)
        except WorkUnitError as exc:
            raise PlannerError(f"Canonical spec {uid} failed Wave 1 validation: {exc}", "malformed_schema") from exc

        # Controller limits validation
        limits = spec.get("limits", {})
        if limits.get("max_attempts", 2) > 2 or limits.get("timeout_seconds", 300) > 300:
            raise PlannerError(f"Unit {uid} limits exceed controller bounds", "malformed_schema")

        # Dependency validation
        for dep in spec.get("dependencies", []):
            if dep not in id_to_seq_idx:
                raise PlannerError(f"Unit {uid} depends on unknown unit {dep!r}", "unknown_dependency")
            if dep == uid:
                raise PlannerError(f"Unit {uid} has self-dependency", "self_dependency")
            if id_to_seq_idx[dep] >= idx:
                raise PlannerError(
                    f"Unit {uid} dependency {dep} does not precede it in canonical sequence (cycle or order defect)",
                    "cycle_detected"
                )

        # Scope narrowing (if parent_scope provided)
        if parent_scope is not None:
            _validate_scope_narrowing(spec["scope"], parent_scope, uid, idx)

        # Verifier coverage and capability authority (Fix 1, Fix C & Case 2G)
        req_caps = spec.get("required_capabilities")
        if spec.get("mode") == "mutation":
            if req_caps is None or not isinstance(req_caps, list) or len(req_caps) == 0:
                raise PlannerError(
                    f"Canonical mutation unit {uid} must have non-empty required_capabilities",
                    "verifier_coverage_missing",
                )

        if capability_policy is not None:
            expected_caps = resolve_unit_required_capabilities(
                spec, idx, capability_policy, approved_verifiers or {}, uid
            )
            if req_caps is None or set(req_caps) != set(expected_caps):
                raise PlannerError(
                    f"Unit {uid} required_capabilities mismatch controller capability policy",
                    "planner_error"
                )

        if approved_verifiers is not None:
            _validate_verifier_coverage(
                spec["verifier_ids"], spec["mode"], spec["scope"], approved_verifiers, uid, idx,
                required_capabilities=req_caps,
            )

    # Wave 1 plan-level validation (DAG acyclicity, etc.)
    try:
        work_units.validate_unit_plan(
            canonical_specs,
            parent_scope=parent_scope,
            verifier_registry=approved_verifiers,
        )
    except WorkUnitError as exc:
        raise PlannerError(f"Canonical plan failed Wave 1 plan validation: {exc}", "malformed_schema") from exc

    # Digest binding check
    if digest is not None:
        expected = _digest({"specs": canonical_specs, "sequence": canonical_sequence})
        if digest != expected:
            raise PlannerError(
                f"Tampering detected: canonical plan digest mismatch: expected {expected!r}, got {digest!r}",
                "planner_error"
            )


def _build_canonical_limits(controller_limits: dict | None) -> dict:
    """Build conservative deterministic unit limits.

    Planner cannot choose or expand these.
    """
    defaults = {"max_attempts": 2, "timeout_seconds": 300}
    if controller_limits is None:
        return dict(defaults)
    result = dict(defaults)
    if "max_attempts" in controller_limits:
        val = controller_limits["max_attempts"]
        if isinstance(val, int) and not isinstance(val, bool) and 1 <= val <= defaults["max_attempts"]:
            result["max_attempts"] = val
    if "timeout_seconds" in controller_limits:
        val = controller_limits["timeout_seconds"]
        if isinstance(val, (int, float)) and not isinstance(val, bool) and 0 < val <= defaults["timeout_seconds"]:
            result["timeout_seconds"] = val
    return result


# --------------------------------------------------------------------------- planner protocol (injected abstraction)

class PlannerProtocol:
    """Abstract interface for a planner."""

    def plan(self, context: dict) -> dict:
        """Receive a bounded context; return a raw proposal dict."""
        raise NotImplementedError


class ScriptedPlanner(PlannerProtocol):
    """Deterministic scripted planner for Wave 3 testing.

    Controls exact planner behavior per call number.
    """

    def __init__(self, behaviors: list | None = None, default: dict | None = None):
        self._behaviors = list(behaviors or [])
        self._default = default or {"units": []}
        self._call_count = 0
        self.calls: list[dict] = []

    def plan(self, context: dict) -> dict:
        self._call_count += 1
        self.calls.append(copy.deepcopy(context))

        if self._behaviors:
            behavior = self._behaviors.pop(0)
        else:
            behavior = self._default

        if isinstance(behavior, BaseException):
            raise behavior
        if callable(behavior):
            return behavior(context)
        return copy.deepcopy(behavior)

    @property
    def call_count(self) -> int:
        return self._call_count


# --------------------------------------------------------------------------- durable planning state

def _initial_planning_state(task: str) -> dict:
    """Initial durable planning state section."""
    now_iso = _now()
    return {
        "schema_version": WAVE3_SCHEMA_VERSION,
        "task": task,
        "phase": "PLANNING",
        "planner_call_count": 0,
        "current_call_kind": None,
        "call_in_flight": False,
        "in_flight_timing": None,
        "schema_correction_used": False,
        "replan_used": False,
        "attempts": [],
        "current_proposal": None,
        "accepted_plan": None,
        "accepted_sequence": None,
        "proposal_digest": None,
        "rejection_reason": None,
        "planning_stage_started_at": now_iso,
        "planning_stage_budget_seconds": None,
        "planning_stage_consumed_seconds": 0.0,
        "enclosing_task_base_runtime": 0.0,
        "enclosing_task_budget_seconds": 3600.0,
        "planning_stage_timing": {
            "planning_stage_started_at": now_iso,
            "planning_stage_budget_seconds": None,
            "planning_stage_consumed_seconds": 0.0,
            "enclosing_task_base_runtime": 0.0,
            "enclosing_task_budget_seconds": 3600.0,
        },
        "started_at": now_iso,
        "updated_at": now_iso,
    }


def load_planning_state(durable_state: dict) -> dict | None:
    """Load work_unit_planner section from durable state, or None if absent."""
    if not isinstance(durable_state, dict):
        raise PlannerError("durable_state must be a dict", "planner_error")
    section = durable_state.get("work_unit_planner")
    if section is None:
        return None
    validate_planning_section(section, durable_state=durable_state)
    return section


def persist_planning_state(
    section: dict,
    store,
    manager_runtime: float | None = None,
    pre_write_guard: Any | None = None,
) -> None:
    """Persist updated planning state into durable store.

    TRUST INVARIANT: Never modifies:
    - verified_progress
    - last_verified_checkpoint
    """
    trust_before = {
        "verified_progress": copy.deepcopy(store.state.get("verified_progress")),
        "last_verified_checkpoint": copy.deepcopy(store.state.get("last_verified_checkpoint")),
    }
    updates: dict[str, Any] = {"work_unit_planner": copy.deepcopy(section)}
    if manager_runtime is not None and "manager" in store.state:
        mgr = copy.deepcopy(store.state["manager"])
        mgr["runtime_seconds"] = round(manager_runtime, 3)
        updates["manager"] = mgr
    store.commit(pre_write_guard=pre_write_guard, **updates)
    trust_after = {
        "verified_progress": copy.deepcopy(store.state.get("verified_progress")),
        "last_verified_checkpoint": copy.deepcopy(store.state.get("last_verified_checkpoint")),
    }
    if trust_before != trust_after:
        raise PlannerError(
            "CRITICAL: planning state persistence altered trusted state fields",
            "planner_error"
        )


# --------------------------------------------------------------------------- planning controller

class PlanningController:
    """Controller that runs the bounded planner loop.

    Enforces:
    - Planning budget from enclosing task budget
    - Max 1 schema correction
    - Max 1 task-level replan
    - Durable lifecycle (crash-safe attempt counting)
    - Controller authority over scope, verifiers, IDs, budgets
    """

    def __init__(
        self,
        store,
        planner: PlannerProtocol,
        *,
        parent_scope: dict,
        approved_verifiers: dict,
        task: str,
        controller_limits: dict | None = None,
        planning_timeout_seconds: float | None = None,
        planning_stage_budget_seconds: float | None = None,
        budgets: dict | None = None,
        clock=time.monotonic,
        base_runtime: float | None = None,
        capability_policy: Any | None = None,
    ):
        self.store = store
        self.planner = planner
        self.parent_scope = parent_scope
        self.approved_verifiers = approved_verifiers
        self.task = task
        self.controller_limits = controller_limits
        self.clock = clock
        self.process_clock_start = clock()
        self.started_at = self.process_clock_start

        if base_runtime is not None:
            self.base_runtime = float(base_runtime)
        else:
            self.base_runtime = float(store.state.get("manager", {}).get("runtime_seconds", 0.0))

        self.budgets = (
            budgets
            if budgets is not None
            else store.state.get("manager", {}).get("budgets", {"max_runtime_seconds": 3600})
        )

        # Fix A: Cumulative planning-stage budget
        sec = store.state.get("work_unit_planner")
        if planning_stage_budget_seconds is not None:
            self.planning_stage_budget_seconds = float(planning_stage_budget_seconds)
        elif planning_timeout_seconds is not None:
            self.planning_stage_budget_seconds = float(planning_timeout_seconds)
        elif sec and isinstance(sec, dict) and sec.get("planning_stage_budget_seconds") is not None:
            self.planning_stage_budget_seconds = float(sec["planning_stage_budget_seconds"])
        elif self.budgets and "planning_stage_budget_seconds" in self.budgets:
            self.planning_stage_budget_seconds = float(self.budgets["planning_stage_budget_seconds"])
        else:
            self.planning_stage_budget_seconds = None
        self.planning_timeout_seconds = self.planning_stage_budget_seconds

        # Consumed planning-stage runtime from persisted section
        if sec and isinstance(sec, dict):
            self.planning_stage_consumed_base = float(sec.get("planning_stage_consumed_seconds", 0.0))
        else:
            self.planning_stage_consumed_base = 0.0

        # Fix C: Capability policy
        self.capability_policy = (
            capability_policy
            if capability_policy is not None
            else (controller_limits or {}).get("capability_policy")
            or store.state.get("options", {}).get("capability_policy")
            or store.state.get("capability_policy")
            or (sec.get("capability_policy") if sec and isinstance(sec, dict) else None)
        )

        # Fix 5 & Fix A: Reconcile in-flight timing from any prior crashed planner run
        self.reconciliation_stop: BaseException | None = None
        try:
            self.reconcile_on_resume()
        except PlannerError as exc:
            self.reconciliation_stop = exc

    def reconcile_on_resume(self) -> None:
        """Conservatively reconcile unaccounted in-flight planner timing after crash (Fix 5 & Fix A)."""
        section = self.store.state.get("work_unit_planner")
        if not section or not isinstance(section, dict):
            return
        timing = section.get("in_flight_timing")
        if timing and isinstance(timing, dict):
            start_clock = timing.get("call_start_clock", self.process_clock_start)
            start_runtime = timing.get("call_start_runtime", self.base_runtime)
            start_planning = timing.get("call_start_planning_consumed", self.planning_stage_consumed_base)
            accounted_runtime = timing.get("accounted_runtime", self.base_runtime)
            accounted_planning = timing.get("accounted_planning_consumed", self.planning_stage_consumed_base)

            elapsed_in_stage = max(0.0, self.clock() - start_clock)

            total_observed_runtime = start_runtime + elapsed_in_stage
            already_accounted_runtime = max(self.base_runtime, accounted_runtime)
            unaccounted_runtime = max(0.0, total_observed_runtime - already_accounted_runtime)
            self.base_runtime = already_accounted_runtime + unaccounted_runtime

            total_observed_planning = start_planning + elapsed_in_stage
            already_accounted_planning = max(self.planning_stage_consumed_base, accounted_planning)
            unaccounted_planning = max(0.0, total_observed_planning - already_accounted_planning)
            self.planning_stage_consumed_base = already_accounted_planning + unaccounted_planning

            now = self.clock()
            self.process_clock_start = now
            self.started_at = now

            timing["accounted_runtime"] = round(self.base_runtime, 3)
            timing["accounted_planning_consumed"] = round(self.planning_stage_consumed_base, 3)
            section["in_flight_timing"] = timing
            section["call_in_flight"] = False
            self._sync_timing_section(section)
            self._persist_runtime()
            self._update_section(section)

            rem_task = self.remaining_task_budget()
            rem_plan = self.remaining_planning_budget()
            if rem_task <= 0 or (rem_plan is not None and rem_plan <= 0):
                self._record_attempt(
                    section,
                    kind=timing.get("call_kind", "planner_call"),
                    result="interrupted",
                    failure_code="budget_exhausted",
                    failure_detail="Budget exhausted during interrupted planner call",
                )
                self._update_section(
                    section,
                    phase="PLAN_REJECTED",
                    rejection_reason="budget_exhausted",
                )
                raise PlannerBudgetExhausted("Budget exhausted during interrupted planner call")

    def runtime(self) -> float:
        return self.base_runtime + max(0.0, self.clock() - self.process_clock_start)

    def cumulative_planning_consumed(self) -> float:
        return self.planning_stage_consumed_base + max(0.0, self.clock() - self.process_clock_start)

    def remaining_task_budget(self) -> float:
        max_runtime = self.budgets.get("max_runtime_seconds", 3600.0)
        return max(0.0, max_runtime - self.runtime())

    def remaining_planning_budget(self) -> float | None:
        if self.planning_stage_budget_seconds is None:
            return None
        return max(0.0, self.planning_stage_budget_seconds - self.cumulative_planning_consumed())

    def effective_planner_deadline(self) -> float:
        rem_task = self.remaining_task_budget()
        rem_plan = self.remaining_planning_budget()
        if rem_plan is not None:
            return max(0.0, min(rem_task, rem_plan))
        return max(0.0, rem_task)

    def _charge_elapsed_to_base(self) -> float:
        now = self.clock()
        elapsed = max(0.0, now - self.process_clock_start)
        self.base_runtime += elapsed
        self.planning_stage_consumed_base += elapsed
        self.process_clock_start = now
        self.started_at = now
        return elapsed

    def _check_planning_budget(self) -> None:
        self._charge_elapsed_to_base()
        if self.remaining_task_budget() <= 0:
            self._persist_runtime()
            raise PlannerBudgetExhausted("Task budget exhausted before/during planning")
        rem_plan = self.remaining_planning_budget()
        if rem_plan is not None and rem_plan <= 0:
            self._persist_runtime()
            raise PlannerBudgetExhausted("Planning stage budget exhausted")

    def _sync_timing_section(self, section: dict) -> None:
        now_runtime = round(self.runtime(), 3)
        now_planning = round(self.cumulative_planning_consumed(), 3)
        max_task = self.budgets.get("max_runtime_seconds", 3600.0)

        section["planning_stage_consumed_seconds"] = now_planning
        section["planning_stage_budget_seconds"] = self.planning_stage_budget_seconds
        section["enclosing_task_base_runtime"] = now_runtime
        section["enclosing_task_budget_seconds"] = max_task

        if "planning_stage_timing" not in section or not isinstance(section["planning_stage_timing"], dict):
            section["planning_stage_timing"] = {}
        timing = section["planning_stage_timing"]
        timing["planning_stage_started_at"] = section.get("planning_stage_started_at", section.get("started_at"))
        timing["planning_stage_budget_seconds"] = self.planning_stage_budget_seconds
        timing["planning_stage_consumed_seconds"] = now_planning
        timing["enclosing_task_base_runtime"] = now_runtime
        timing["enclosing_task_budget_seconds"] = max_task

    def _persist_runtime(self) -> None:
        if "manager" in self.store.state:
            mgr = copy.deepcopy(self.store.state["manager"])
            mgr["runtime_seconds"] = round(self.runtime(), 3)
            self.store.commit(manager=mgr)

    def _get_or_create_section(self) -> dict:
        existing = load_planning_state(self.store.state)
        if existing is not None:
            if self.planning_stage_budget_seconds is not None and existing.get("planning_stage_budget_seconds") is None:
                existing["planning_stage_budget_seconds"] = self.planning_stage_budget_seconds
            if self.approved_verifiers and existing.get("approved_verifiers") is None:
                existing["approved_verifiers"] = copy.deepcopy(self.approved_verifiers)
            if self.capability_policy is not None and existing.get("capability_policy") is None:
                existing["capability_policy"] = copy.deepcopy(self.capability_policy)
            self._sync_timing_section(existing)
            persist_planning_state(existing, self.store, self.runtime())
            return existing
        section = _initial_planning_state(self.task)
        section["planning_stage_budget_seconds"] = self.planning_stage_budget_seconds
        if self.approved_verifiers:
            section["approved_verifiers"] = copy.deepcopy(self.approved_verifiers)
        if self.capability_policy is not None:
            section["capability_policy"] = copy.deepcopy(self.capability_policy)
        self._sync_timing_section(section)
        persist_planning_state(section, self.store, self.runtime())
        return section

    def _update_section(self, section: dict, **updates) -> None:
        section.update(updates)
        self._sync_timing_section(section)
        section["updated_at"] = _now()
        persist_planning_state(section, self.store, self.runtime())

    def _record_attempt(
        self,
        section: dict,
        *,
        kind: str,
        result: str,
        failure_code: str | None = None,
        failure_detail: str | None = None,
    ) -> None:
        record = {
            "attempt_number": len(section["attempts"]) + 1,
            "kind": kind,
            "result": result,
            "failure_code": failure_code,
            "failure_detail": failure_detail,
            "time": _now(),
            "elapsed_seconds": round(self.cumulative_planning_consumed(), 3),
        }
        section["attempts"].append(record)
        self._update_section(section)

    def run(self) -> tuple[list[dict], list[str]]:
        """Execute bounded planner loop; return (canonical_specs, canonical_sequence)."""
        if self.reconciliation_stop is not None:
            raise self.reconciliation_stop

        self._check_planning_budget()
        section = self._get_or_create_section()

        # Fix 2: If work_units exists and execution has started, return accepted plan idempotently
        wu_section = self.store.state.get("work_units")
        if wu_section and isinstance(wu_section, dict):
            units = wu_section.get("units", {})
            execution_started = any(
                u.get("status") != "PROPOSED"
                or bool(u.get("attempts"))
                or u.get("result") is not None
                or u.get("current_attempt") is not None
                for u in units.values()
            )
            if execution_started:
                if section["phase"] != "PLAN_ACCEPTED":
                    raise PlannerError(
                        "Cannot plan WorkUnits after execution has already started",
                        "plan_already_accepted",
                    )
                accepted = section.get("accepted_plan")
                sequence = section.get("accepted_sequence")
                digest = section.get("proposal_digest")
                validate_canonical_plan(
                    accepted, sequence, parent_scope=self.parent_scope,
                    approved_verifiers=self.approved_verifiers, digest=digest,
                    capability_policy=self.capability_policy,
                )
                validate_planning_section(section, durable_state=self.store.state)
                return copy.deepcopy(accepted), list(sequence)

        # Idempotent resume: if already accepted, validate before returning (Fix 1)
        if section["phase"] == "PLAN_ACCEPTED":
            accepted = section["accepted_plan"]
            sequence = section["accepted_sequence"]
            digest = section.get("proposal_digest")
            if not accepted or not sequence or not digest:
                raise PlannerError(
                    "Durable state shows PLAN_ACCEPTED but plan data is missing",
                    "planner_error"
                )
            validate_canonical_plan(
                accepted, sequence, parent_scope=self.parent_scope,
                approved_verifiers=self.approved_verifiers, digest=digest,
                capability_policy=self.capability_policy,
            )
            validate_planning_section(section, durable_state=self.store.state)
            return copy.deepcopy(accepted), list(sequence)

        if section["phase"] == "PLAN_REJECTED":
            reason = section.get("rejection_reason") or "PLAN_REJECTED"
            if reason == "budget_exhausted":
                raise PlannerBudgetExhausted(f"Plan was previously rejected: {reason}")
            raise PlannerError(
                f"Plan was previously rejected: {reason}",
                reason if reason in FAILURE_CODES else "planner_error"
            )

        # Fix 3: Global call ceiling check before starting calls
        if section.get("planner_call_count", 0) >= MAX_PLANNER_CALLS:
            code = "second_replan_refused" if section.get("replan_used") else "second_schema_correction_refused"
            raise PlannerError(
                f"Global planner call ceiling of {MAX_PLANNER_CALLS} reached; planning refused",
                code,
            )

        # Build bounded context
        ctx = build_planner_context(
            task=self.task,
            parent_scope=self.parent_scope,
            approved_verifiers=self.approved_verifiers,
            max_units=MAX_PLAN_UNITS,
            planning_constraints=self.controller_limits,
            capability_policy=self.capability_policy,
        )

        # If a proposal was already received before a crash:
        proposal_units = None
        attempt_kind = "initial"
        if section["phase"] in ("PROPOSAL_RECEIVED", "VALIDATING") and section.get("current_proposal") is not None:
            try:
                proposal_units = validate_proposal_schema(section["current_proposal"])
            except PlannerError:
                proposal_units = None

        if proposal_units is None:
            # Call planner with schema correction handling
            proposal_units, attempt_kind = self._call_planner_with_schema_correction(ctx, section)

        # Semantic validation (with optional replan)
        canonical_specs, canonical_sequence = self._validate_with_optional_replan(
            proposal_units, ctx, section
        )

        # Persist accepted plan
        self._accept_plan(section, canonical_specs, canonical_sequence)
        return copy.deepcopy(canonical_specs), list(canonical_sequence)

    def _call_planner_with_schema_correction(
        self,
        ctx: dict,
        section: dict,
    ) -> tuple[list[dict], str]:
        self._check_planning_budget()
        self._update_section(section, phase="PLANNING")

        kind = "initial"
        raw_proposal = self._timed_planner_call(ctx, section, kind)
        self._update_section(section, phase="PROPOSAL_RECEIVED", current_proposal=copy.deepcopy(raw_proposal))

        try:
            proposal_units = validate_proposal_schema(raw_proposal)
            return proposal_units, kind
        except PlannerError as exc:
            if exc.code not in ("malformed_schema", "empty_plan", "too_many_units"):
                self._record_attempt(
                    section, kind=kind, result="validation_error",
                    failure_code=exc.code, failure_detail=str(exc)
                )
                raise

            # Fix 3: Schema error handling bounded by global call ceiling
            schema_correction_used = section.get("schema_correction_used", False)
            if schema_correction_used or section.get("replan_used") or section.get("planner_call_count", 0) >= MAX_PLANNER_CALLS:
                self._record_attempt(
                    section, kind=kind, result="schema_error",
                    failure_code=exc.code, failure_detail=str(exc)
                )
                self._record_attempt(
                    section, kind="schema_correction", result="refused",
                    failure_code="second_schema_correction_refused",
                    failure_detail="Schema correction unavailable (already used, replan used, or max calls reached)"
                )
                self._update_section(
                    section,
                    phase="PLAN_REJECTED",
                    rejection_reason="second_schema_correction_refused"
                )
                raise PlannerError(
                    "Second schema correction refused; only one schema correction is allowed and global ceiling is 2 calls",
                    "second_schema_correction_refused"
                ) from exc

            # Consume schema correction
            self._record_attempt(
                section, kind=kind, result="schema_error",
                failure_code=exc.code, failure_detail=str(exc)
            )
            self._update_section(section, schema_correction_used=True)
            self._check_planning_budget()

            correction_ctx = dict(ctx)
            correction_ctx["schema_correction"] = {
                "code": exc.code,
                "fields": exc.fields,
                "detail": str(exc)[:500],
                "instruction": (
                    "The previous response was structurally invalid. "
                    "Correct the schema and return a valid plan. "
                    "This is the only schema correction allowed."
                ),
            }

            correction_kind = "schema_correction"
            raw_correction = self._timed_planner_call(correction_ctx, section, correction_kind)
            self._update_section(section, current_proposal=copy.deepcopy(raw_correction))

            try:
                proposal_units = validate_proposal_schema(raw_correction)
                self._record_attempt(section, kind=correction_kind, result="accepted")
                return proposal_units, correction_kind
            except PlannerError as correction_exc:
                self._record_attempt(
                    section, kind=correction_kind, result="schema_error",
                    failure_code="second_schema_correction_refused",
                    failure_detail=str(correction_exc)
                )
                self._update_section(
                    section,
                    phase="PLAN_REJECTED",
                    rejection_reason="second_schema_correction_refused"
                )
                raise PlannerError(
                    f"Schema correction also failed: {correction_exc}",
                    "second_schema_correction_refused"
                ) from correction_exc

    def _timed_planner_call(
        self,
        ctx: dict,
        section: dict,
        kind: str,
    ) -> dict:
        # Fix 3: Global call ceiling check BEFORE call
        call_count = section.get("planner_call_count", 0)
        if call_count >= MAX_PLANNER_CALLS:
            code = "second_replan_refused" if section.get("replan_used") else "second_schema_correction_refused"
            self._record_attempt(
                section, kind=kind, result="refused",
                failure_code=code, failure_detail=f"Global planner call ceiling of {MAX_PLANNER_CALLS} reached"
            )
            self._update_section(section, phase="PLAN_REJECTED", rejection_reason=code)
            raise PlannerError(f"Global planner call ceiling of {MAX_PLANNER_CALLS} reached", code)

        # Fix 4 & Fix A: Charge any intervening elapsed time and check budgets BEFORE call
        self._charge_elapsed_to_base()
        eff_deadline = self.effective_planner_deadline()
        rem_task = self.remaining_task_budget()
        rem_plan = self.remaining_planning_budget()
        if eff_deadline <= 0 or rem_task <= 0 or (rem_plan is not None and rem_plan <= 0):
            self._record_attempt(
                section, kind=kind, result="budget_exhausted",
                failure_code="budget_exhausted", failure_detail="Budget exhausted before planner call"
            )
            self._update_section(section, phase="PLAN_REJECTED", rejection_reason="budget_exhausted")
            raise PlannerBudgetExhausted("Budget exhausted before planner call")

        # Fix 3, 5: Reserve call slot in durable state BEFORE invoking planner
        call_count += 1
        section["planner_call_count"] = call_count
        section["current_call_kind"] = kind
        section["call_in_flight"] = True

        call_start_clock = self.clock()
        call_start_runtime = self.runtime()
        call_start_planning = self.cumulative_planning_consumed()
        max_runtime = self.budgets.get("max_runtime_seconds", 3600)
        section["in_flight_timing"] = {
            "call_number": call_count,
            "call_kind": kind,
            "call_start_clock": call_start_clock,
            "call_start_runtime": call_start_runtime,
            "call_start_planning_consumed": call_start_planning,
            "accounted_runtime": call_start_runtime,
            "accounted_planning_consumed": call_start_planning,
            "max_runtime_seconds": max_runtime,
            "planning_stage_budget_seconds": self.planning_stage_budget_seconds,
        }
        self._update_section(section)

        # Fix 4 & Fix A: Context passed to planner with deadline capped by cumulative remaining authority
        call_ctx = copy.deepcopy(ctx)
        call_ctx["deadline_seconds"] = eff_deadline
        call_ctx["remaining_budget_seconds"] = eff_deadline
        call_ctx["remaining_task_budget_seconds"] = rem_task
        call_ctx["remaining_planning_budget_seconds"] = rem_plan if rem_plan is not None else eff_deadline

        try:
            result = self.planner.plan(call_ctx)
        except PlannerTimeout as exc:
            self._charge_elapsed_to_base()
            section["call_in_flight"] = False
            section["in_flight_timing"] = None
            self._persist_runtime()
            self._record_attempt(
                section, kind=kind, result="timeout",
                failure_code="planner_timeout", failure_detail=str(exc)
            )
            self._update_section(section, phase="PLAN_REJECTED", rejection_reason="planner_timeout")
            raise
        except PlannerBudgetExhausted as exc:
            self._charge_elapsed_to_base()
            section["call_in_flight"] = False
            section["in_flight_timing"] = None
            self._persist_runtime()
            self._record_attempt(
                section, kind=kind, result="budget_exhausted",
                failure_code="budget_exhausted", failure_detail=str(exc)
            )
            self._update_section(section, phase="PLAN_REJECTED", rejection_reason="budget_exhausted")
            raise
        except PlannerError as exc:
            self._charge_elapsed_to_base()
            section["call_in_flight"] = False
            section["in_flight_timing"] = None
            self._persist_runtime()
            self._record_attempt(
                section, kind=kind, result="error",
                failure_code=exc.code, failure_detail=str(exc)
            )
            raise
        except Exception as exc:
            self._charge_elapsed_to_base()
            section["call_in_flight"] = False
            section["in_flight_timing"] = None
            self._persist_runtime()
            self._record_attempt(
                section, kind=kind, result="error",
                failure_code="planner_error", failure_detail=str(exc)[:500]
            )
            self._update_section(section, phase="PLAN_REJECTED", rejection_reason="planner_error")
            raise PlannerError(
                f"Planner raised unexpected error: {type(exc).__name__}: {exc}",
                "planner_error"
            ) from exc

        # Fix 4 & Fix A: Immediately charge elapsed runtime after call returns
        elapsed = self._charge_elapsed_to_base()
        section["call_in_flight"] = False
        section["in_flight_timing"] = None
        self._persist_runtime()

        # Fix 4 & Fix A: Post-call budget check across BOTH budgets
        post_rem_task = self.remaining_task_budget()
        post_rem_plan = self.remaining_planning_budget()
        if post_rem_task <= 0 or (post_rem_plan is not None and post_rem_plan <= 0):
            self._record_attempt(
                section, kind=kind, result="budget_exhausted",
                failure_code="budget_exhausted",
                failure_detail=f"Budget exhausted during planner call ({elapsed:.3f}s elapsed)"
            )
            self._update_section(section, phase="PLAN_REJECTED", rejection_reason="budget_exhausted")
            raise PlannerBudgetExhausted("Budget exhausted during planner call")

        # Fix 3: Non-dict check (counts as planner call and is handled as malformed_schema)
        if not isinstance(result, dict):
            result = {
                "_raw_non_dict_response": True,
                "_original_type": type(result).__name__,
            }

        return result

    def _validate_with_optional_replan(
        self,
        proposal_units: list[dict],
        ctx: dict,
        section: dict,
    ) -> tuple[list[dict], list[str]]:
        self._charge_elapsed_to_base()
        self._check_planning_budget()
        self._update_section(section, phase="VALIDATING")

        try:
            canonical_specs, canonical_sequence = validate_and_build_canonical_plan(
                proposal_units,
                parent_scope=self.parent_scope,
                approved_verifiers=self.approved_verifiers,
                controller_limits=self.controller_limits,
                capability_policy=self.capability_policy,
            )
            self._charge_elapsed_to_base()
            self._check_planning_budget()
            return canonical_specs, canonical_sequence

        except PlannerError as exc:
            self._charge_elapsed_to_base()
            # Fix 8: Scope violations are strictly NOT replannable!
            # Case 2G: Missing capability policy is also strictly NOT replannable!
            replannable_codes = {
                "unknown_dependency",
                "duplicate_dependency",
                "self_dependency",
                "cycle_detected",
                "duplicate_alias",
                "unknown_verifier",
                "verifier_coverage_missing",
                "unsupported_mode",
                "empty_plan",
                "too_many_units",
            }
            if exc.code not in replannable_codes or (not self.capability_policy and exc.code in ("verifier_coverage_missing", "missing_capability_policy")):
                self._record_attempt(
                    section, kind="validation", result="validation_error",
                    failure_code=exc.code, failure_detail=str(exc)
                )
                self._update_section(
                    section,
                    phase="PLAN_REJECTED",
                    rejection_reason=exc.code
                )
                raise

            # Fix 3: Replan check bounded by global call ceiling
            replan_used = section.get("replan_used", False)
            if replan_used or section.get("schema_correction_used") or section.get("planner_call_count", 0) >= MAX_PLANNER_CALLS:
                self._record_attempt(
                    section, kind="validation", result="validation_error",
                    failure_code=exc.code, failure_detail=str(exc)
                )
                self._record_attempt(
                    section, kind="replan", result="refused",
                    failure_code="second_replan_refused",
                    failure_detail="Replan unavailable: maximum planner calls consumed or schema correction already used"
                )
                self._update_section(
                    section,
                    phase="PLAN_REJECTED",
                    rejection_reason="second_replan_refused"
                )
                raise PlannerError(
                    "Second replan refused; only one task-level replan is allowed and global ceiling is 2 calls",
                    "second_replan_refused"
                ) from exc

            # First replan
            self._record_attempt(
                section, kind="validation", result="validation_error",
                failure_code=exc.code, failure_detail=str(exc)
            )
            self._update_section(section, replan_used=True)
            self._check_planning_budget()

            replan_ctx = dict(ctx)
            replan_ctx["replan_evidence"] = {
                "code": exc.code,
                "fields": exc.fields,
                "detail": str(exc)[:500],
                "instruction": (
                    "The previous decomposition was rejected by the controller. "
                    "Produce a corrected plan using the same task and constraints. "
                    "This is the only replan allowed."
                ),
            }

            # Fix 3: Direct timed planner call for replan (Call 2)
            raw_replan = self._timed_planner_call(replan_ctx, section, "replan")
            self._update_section(section, current_proposal=copy.deepcopy(raw_replan))

            try:
                replan_units = validate_proposal_schema(raw_replan)
            except PlannerError as schema_exc:
                self._record_attempt(
                    section, kind="replan", result="schema_error",
                    failure_code="second_replan_refused",
                    failure_detail=f"Replan returned malformed schema: {schema_exc}",
                )
                self._update_section(
                    section,
                    phase="PLAN_REJECTED",
                    rejection_reason="second_replan_refused",
                )
                raise PlannerError(
                    f"Replan returned invalid schema and no further correction is allowed: {schema_exc}",
                    "second_replan_refused",
                ) from schema_exc

            try:
                canonical_specs, canonical_sequence = validate_and_build_canonical_plan(
                    replan_units,
                    parent_scope=self.parent_scope,
                    approved_verifiers=self.approved_verifiers,
                    controller_limits=self.controller_limits,
                    capability_policy=self.capability_policy,
                )
                self._charge_elapsed_to_base()
                self._check_planning_budget()
                self._record_attempt(section, kind="replan", result="accepted")
                return canonical_specs, canonical_sequence
            except PlannerError as replan_exc:
                self._charge_elapsed_to_base()
                self._record_attempt(
                    section, kind="replan", result="validation_error",
                    failure_code="second_replan_refused",
                    failure_detail=str(replan_exc)
                )
                self._update_section(
                    section,
                    phase="PLAN_REJECTED",
                    rejection_reason="second_replan_refused"
                )
                raise PlannerError(
                    f"Replan also failed validation: {replan_exc}",
                    "second_replan_refused"
                ) from replan_exc

    def _accept_plan(
        self,
        section: dict,
        canonical_specs: list[dict],
        canonical_sequence: list[str],
    ) -> None:
        # Pre-acceptance budget check
        self._charge_elapsed_to_base()
        self._check_planning_budget()

        plan_copy = copy.deepcopy(canonical_specs)
        seq_copy = list(canonical_sequence)
        digest = _digest({"specs": plan_copy, "sequence": seq_copy})

        # Fix 1 & Fix C: Validate canonical plan completeness, capability policy, and digest binding
        validate_canonical_plan(
            plan_copy,
            seq_copy,
            parent_scope=self.parent_scope,
            approved_verifiers=self.approved_verifiers,
            digest=digest,
            capability_policy=self.capability_policy,
        )

        # Fix B: Charge validation elapsed runtime to cumulative planning and task runtime
        self._charge_elapsed_to_base()

        # Fix B: Check BOTH budgets AGAIN before candidate acceptance
        rem_task = self.remaining_task_budget()
        rem_plan = self.remaining_planning_budget()
        if rem_task <= 0 or (rem_plan is not None and rem_plan <= 0):
            self._record_attempt(
                section, kind="acceptance", result="budget_exhausted",
                failure_code="budget_exhausted",
                failure_detail="Budget exhausted during canonical plan validation before acceptance",
            )
            self._update_section(section, phase="PLAN_REJECTED", rejection_reason="budget_exhausted")
            raise PlannerBudgetExhausted("Budget exhausted during canonical plan validation before acceptance")

        # Fix 1: Acceptance persistence under pre-write budget authority guard
        candidate_section = copy.deepcopy(section)
        candidate_section.update(
            phase="PLAN_ACCEPTED",
            accepted_plan=plan_copy,
            accepted_sequence=seq_copy,
            proposal_digest=digest,
            rejection_reason=None,
            updated_at=_now(),
        )
        if self.capability_policy is not None:
            candidate_section["capability_policy"] = copy.deepcopy(self.capability_policy)
        if self.approved_verifiers is not None:
            candidate_section["approved_verifiers"] = copy.deepcopy(self.approved_verifiers)
        self._sync_timing_section(candidate_section)

        def pre_write_guard(candidate_state: dict) -> None:
            # 1. Charge all elapsed runtime consumed by validation inside persistence
            self._charge_elapsed_to_base()
            # 2. Check remaining task budget and planning stage budget
            guard_rem_task = self.remaining_task_budget()
            guard_rem_plan = self.remaining_planning_budget()
            if guard_rem_task <= 0 or (guard_rem_plan is not None and guard_rem_plan <= 0):
                raise PlannerBudgetExhausted(
                    "Budget exhausted during persistence validation before authoritative acceptance write"
                )
            # 3. If within budget, update candidate state timing records consistently before atomic write
            if "work_unit_planner" in candidate_state:
                self._sync_timing_section(candidate_state["work_unit_planner"])
            if "manager" in candidate_state:
                candidate_state["manager"]["runtime_seconds"] = round(self.runtime(), 3)

        try:
            persist_planning_state(
                candidate_section,
                self.store,
                manager_runtime=self.runtime(),
                pre_write_guard=pre_write_guard,
            )
            section.update(candidate_section)
        except PlannerBudgetExhausted as exc:
            self._record_attempt(
                section, kind="acceptance", result="budget_exhausted",
                failure_code="budget_exhausted",
                failure_detail=f"Budget exhausted during persistence validation ({self.runtime():.3f}s runtime)",
            )
            self._update_section(section, phase="PLAN_REJECTED", rejection_reason="budget_exhausted")
            raise


# --------------------------------------------------------------------------- durable store integration

def validate_planning_section(section: Any, durable_state: dict | None = None) -> None:
    """Validate a work_unit_planner section from durable state (Fix 1).

    Called by durable.validate_state for integrity checking.
    Raises ValueError / PlannerError on defect.
    """
    if not isinstance(section, dict):
        raise ValueError("work_unit_planner section must be a dict")
    sv = section.get("schema_version")
    if sv != WAVE3_SCHEMA_VERSION:
        raise ValueError(f"Unsupported work_unit_planner schema_version: {sv!r}")
    phase = section.get("phase")
    if phase not in PLANNING_PHASES:
        raise ValueError(f"Invalid work_unit_planner phase: {phase!r}")
    if not isinstance(section.get("attempts"), list):
        raise ValueError("work_unit_planner.attempts must be a list")
    if not isinstance(section.get("schema_correction_used"), bool):
        raise ValueError("work_unit_planner.schema_correction_used must be bool")
    if not isinstance(section.get("replan_used"), bool):
        raise ValueError("work_unit_planner.replan_used must be bool")
    call_count = section.get("planner_call_count")
    if call_count is not None and (not isinstance(call_count, int) or isinstance(call_count, bool) or call_count < 0):
        raise ValueError("work_unit_planner.planner_call_count must be a non-negative int")

    if phase == "PLAN_ACCEPTED":
        accepted = section.get("accepted_plan")
        sequence = section.get("accepted_sequence")
        digest = section.get("proposal_digest")
        if not isinstance(accepted, list):
            raise ValueError("PLAN_ACCEPTED requires accepted_plan list")
        if not isinstance(sequence, list):
            raise ValueError("PLAN_ACCEPTED requires accepted_sequence list")
        if not isinstance(digest, str) or not digest:
            raise ValueError("PLAN_ACCEPTED requires non-empty proposal_digest str")

        # Fix 1 & Fix C: Validate complete canonical plan, capability policy, and digest binding
        parent_scope = durable_state.get("manager", {}).get("scope") if durable_state else None
        approved_verifiers = section.get("approved_verifiers") if section else None
        if approved_verifiers is None and durable_state:
            approved_verifiers = (
                durable_state.get("options", {}).get("verifier_registry")
                or durable_state.get("verifier_registry")
            )
        cap_policy = section.get("capability_policy") if section else None
        if cap_policy is None and durable_state:
            cap_policy = (
                durable_state.get("options", {}).get("capability_policy")
                or durable_state.get("capability_policy")
                or durable_state.get("manager", {}).get("capability_policy")
            )

        if any(isinstance(u, dict) and u.get("mode") == "mutation" for u in accepted) and not cap_policy:
            raise PlannerError(
                "Auto-planned mutation unit requires controller capability policy",
                "verifier_coverage_missing",
            )

        validate_canonical_plan(
            accepted,
            sequence,
            parent_scope=parent_scope,
            approved_verifiers=approved_verifiers,
            digest=digest,
            capability_policy=cap_policy,
        )

        # Fix 1: Enforce consistency between work_unit_planner and installed work_units section
        if durable_state and "work_units" in durable_state:
            wu_sec = durable_state["work_units"]
            if isinstance(wu_sec, dict):
                wu_seq = wu_sec.get("sequence", [])
                wu_units = wu_sec.get("units", {})
                if wu_seq != sequence:
                    raise WorkUnitError(
                        f"work_units sequence {wu_seq} does not match accepted_sequence {sequence}"
                    )
                if len(wu_units) != len(accepted):
                    raise WorkUnitError(
                        f"work_units unit count ({len(wu_units)}) does not match accepted plan count ({len(accepted)})"
                    )
                for idx, spec in enumerate(accepted):
                    uid = spec["unit_id"]
                    if uid not in wu_units:
                        raise WorkUnitError(f"work_units missing accepted unit {uid}")
                    wu_u = wu_units[uid]
                    if wu_u.get("spec") != spec:
                        raise WorkUnitError(f"work_units spec for {uid} does not match accepted spec")
                    if wu_u.get("ordinal") != idx:
                        raise WorkUnitError(f"work_units ordinal for {uid} ({wu_u.get('ordinal')}) does not match sequence index ({idx})")


# --------------------------------------------------------------------------- manager integration helpers

def build_work_units_section_from_plan(
    canonical_specs: list[dict],
    canonical_sequence: list[str],
) -> dict:
    """Convert accepted canonical plan into a work_units section for Wave 2 scheduling.

    Returns a validated, sealed work_units section compatible with WorkUnitScheduler.
    """
    import work_units as wu

    section = wu.initial_work_units_section()
    for idx, spec in enumerate(canonical_specs):
        uid = spec["unit_id"]
        wu.add_unit_to_section(section, spec)
        section["units"][uid]["ordinal"] = idx
        wu.seal_unit_state(section["units"][uid])

    section["sequence"] = list(canonical_sequence)
    wu._reseal_section(section)
    wu.validate_section(section)
    return section
