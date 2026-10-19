"""Deterministic WorkUnit scheduling and execution for Wave 2.

Wave 2 architecture:
- Fixed, pre-validated WorkUnit sequences (no model-generated decomposition).
- Deterministic sequential execution (no parallel scheduling).
- Injected / scripted agents for testing and execution.
- Controller-owned deterministic verifier execution.
- Bounded repair policy (maximum 2 executor attempts per unit: initial + 1 repair).
- Scope enforcement: mutation units must mutate within allowed scope; read-only units must not mutate.
- Invalidation handling: conservative revalidation of all units against final candidate state before milestone-ready.
- Trust boundary: UNIT_VERIFIED remains intermediate/untrusted. Does NOT set top-level VERIFIED,
  verified_progress, or last_verified_checkpoint.
- Milestone state: MILESTONE_READY / WORK_UNITS_COMPLETE.

Audited Hardening:
- Fix 1: Controller-owned immutable effective contract. Detached copies to executor. Divergence fails closed.
- Fix 2: Unconditional candidate mutation & scope inspection on all outcomes (success, error, timeout).
- Fix 3: Cumulative shared task & unit budget accounting with bounded stage timeouts.
- Fix 4: Safe invalidation of MILESTONE_READY bound to candidate repository fingerprint.
- Fix 5: Mandatory supervision checks at 7 acceptance boundaries.
- Fix 6: Explicit sequence ordering preserved across WAL / serialization.
- Fix 7: Crash/resume reconciliation for active attempts, VERIFYING units, and repair evidence.
- Fix 8: Terminal unit failure halts milestone scheduling immediately.
- Fix 10: Explicit WorkUnit-compatible executor required; no default model fallback.
- Fix 11: Exact stop statuses preserved in conclusions.
- Fix 12: Note: external process-tree containment is deferred to external executor integration.
- Fix A: Shared unit budget includes current attempt; bounded verifier timeouts and pre-acceptance check.
- Fix B: Failure and crash exit paths preserve consumed runtime.
- Fix C: Pre-attempt candidate binding verified on crash reload; illegal mutations fail closed.
- Fix D: Single unified guarded verification path for normal execution and crash recovery.
- Fix E: Fixed sequence integrity validation before scheduling and revalidation.
- Fix F: Full candidate binding required for is_milestone_ready().
- Fix G: Active VERIFYING crash recovery without re-execution or new attempt.
- Fix H: No default_agents() fallback in WorkUnit mode.
"""

from __future__ import annotations

import copy
import hashlib
import os
import time
from pathlib import Path

import durable
from durable import DurableError, snapshot
import work_units
from work_units import WorkUnitError
from manager import Stop, changed_paths, path_permitted, DEFAULT_BUDGETS

# --------------------------------------------------------------------------- constants

MILESTONE_READY = "MILESTONE_READY"
CRITIC_REVIEWED = "CRITIC_REVIEWED"
WORK_UNITS_COMPLETE = "WORK_UNITS_COMPLETE"
MILESTONE_FAILED = "MILESTONE_FAILED"
WORK_UNITS_FAILED = "WORK_UNITS_FAILED"

MAX_UNIT_ATTEMPTS = 2

FROZEN_KEYS = (
    "unit_id",
    "objective",
    "dependencies",
    "mode",
    "scope",
    "verifier_ids",
    "evidence_inputs",
    "limits",
)


# --------------------------------------------------------------------------- exceptions

class WorkUnitSchedulingError(WorkUnitError):
    """Raised when scheduling or execution fails closed."""


# --------------------------------------------------------------------------- scripted / injected agent

class ScriptedExecutor:
    """Deterministic scripted/injected agent for Wave 2 execution.

    Allows tests to control exact executor behavior per unit_id and attempt number:
    - preset strings:
        - "success_mutation": creates or mutates an allowed file within scope
        - "success_read_only": performs no writes, succeeds cleanly
        - "failure": raises ExecutorError (simulating model/execution failure)
        - "timeout": raises TimeoutError (simulating execution timeout)
        - "no_progress": makes no edits on a mutation unit (produces no diff)
        - "scope_violation": writes outside unit scope or to a forbidden path
        - "unexpected_write": writes a file during a read-only unit
        - "mutate_allowed_paths": appends an illegal path to unit_spec scope
        - "mutate_forbidden_paths": alters forbidden paths on unit_spec scope
        - "mutate_objective": alters objective on unit_spec
        - "mutate_dependencies": alters dependencies on unit_spec
        - "mutate_verifier_ids": alters verifier_ids on unit_spec
        - "mutate_limits": alters limits on unit_spec
        - "write_and_raise": writes outside scope and raises RuntimeError
        - "read_only_write_and_raise": writes a file in read-only and raises RuntimeError
        - "timeout_after_write": writes outside scope and raises TimeoutError
    - custom callable: fn(repo, unit_spec, context)
    - custom writes dict: {"writes": {relative_path: content}}
    """

    def __init__(self, default_behavior="success_mutation"):
        self.default_behavior = default_behavior
        self.rules = {}  # (unit_id, attempt_num) -> behavior or callable
        self.calls = []  # list of executed calls
        self.deadline = None

    def set_deadline(self, deadline):
        self.deadline = deadline

    def set_behavior(self, unit_id, behavior, attempt=None):
        """Set behavior for a specific unit and optional attempt number (1-based)."""
        self.rules[(unit_id, attempt)] = behavior

    def get_attempts(self, unit_id):
        """Return all call records for the given unit_id."""
        return [c for c in self.calls if c["unit_id"] == unit_id]

    def execute(self, unit_spec, context, repo):
        uid = unit_spec["unit_id"]
        attempt_num = context.get("attempt_number", 1)
        self.calls.append({
            "unit_id": uid,
            "attempt_number": attempt_num,
            "context": copy.deepcopy(context),
            "unit_spec": copy.deepcopy(unit_spec),
        })

        # Match specific (uid, attempt_num), then (uid, None), then default
        behavior = self.rules.get((uid, attempt_num), self.rules.get((uid, None), self.default_behavior))

        if callable(behavior):
            return behavior(repo, unit_spec, context)

        if isinstance(behavior, dict) and "writes" in behavior:
            for rel_path, content in behavior["writes"].items():
                repo.write(rel_path, content)
            return "Written files"

        # Contract mutation behaviors (testing Fix 1)
        if behavior == "mutate_allowed_paths":
            unit_spec["scope"]["allowed_paths"].append("escape.txt")
            return "Mutated allowed_paths"

        elif behavior == "mutate_forbidden_paths":
            unit_spec["scope"]["forbidden_paths"] = []
            return "Mutated forbidden_paths"

        elif behavior == "mutate_objective":
            unit_spec["objective"] = "Altered objective"
            return "Mutated objective"

        elif behavior == "mutate_dependencies":
            unit_spec["dependencies"] = []
            return "Mutated dependencies"

        elif behavior == "mutate_verifier_ids":
            unit_spec["verifier_ids"] = ["check-tampered"]
            return "Mutated verifier_ids"

        elif behavior == "mutate_limits":
            unit_spec["limits"]["max_attempts"] = 99
            return "Mutated limits"

        # Unconditional scope inspection behaviors (testing Fix 2)
        elif behavior == "write_and_raise":
            p = Path(repo.root) / "outside.txt"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("illegal write outside scope\n", encoding="utf-8")
            raise RuntimeError("Executor failed after writing outside scope")

        elif behavior == "read_only_write_and_raise":
            p = Path(repo.root) / "readonly_leak.txt"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("leak during read-only\n", encoding="utf-8")
            raise RuntimeError("Executor failed after writing in read-only")

        elif behavior == "timeout_after_write":
            p = Path(repo.root) / "outside.txt"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("illegal write outside scope\n", encoding="utf-8")
            raise TimeoutError("Executor timed out after illegal write")

        # Standard preset behaviors
        elif behavior == "success_mutation":
            allowed = unit_spec["scope"]["allowed_paths"]
            target = allowed[0]
            if target.endswith("/"):
                target = target + "candidate.py"
            repo.write(target, f"# Candidate implementation for {uid} (attempt {attempt_num})\n")
            return f"Mutated {target}"

        elif behavior == "success_read_only":
            return f"Read-only executed for {uid}"

        elif behavior == "no_progress":
            return "No progress made"

        elif behavior == "failure":
            from manager import ExecutorError
            raise ExecutorError(f"Scripted executor failure on unit {uid} attempt {attempt_num}", "EXECUTOR_ERROR")

        elif behavior == "timeout":
            raise TimeoutError(f"Scripted timeout on unit {uid} attempt {attempt_num}")

        elif behavior == "scope_violation":
            forbidden = unit_spec["scope"].get("forbidden_paths", [])
            target = forbidden[0] if forbidden else "leak_outside_scope.txt"
            if target.endswith("/"):
                target = target + "leak.txt"
            try:
                repo.write(target, "scope violation content\n")
            except (ValueError, DurableError):
                p = Path(repo.root) / target
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("scope violation content\n", encoding="utf-8")
            return f"Wrote {target}"

        elif behavior == "unexpected_write":
            target = "unexpected_leak.txt"
            try:
                repo.write(target, "unexpected write in read-only mode\n")
            except (ValueError, DurableError):
                p = Path(repo.root) / target
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("unexpected write in read-only mode\n", encoding="utf-8")
            return "Unexpected write"

        else:
            raise ValueError(f"Unknown scripted behavior: {behavior!r}")


# --------------------------------------------------------------------------- readiness & validation helpers

def capture_fs_snapshot(root: str | Path) -> dict[str, dict | None]:
    """Capture full filesystem snapshot of candidate repository covering all disk files.

    Includes:
    - tracked files
    - untracked files
    - ignored files (.gitignore, .git/info/exclude)
    - generated files (__pycache__, .pyd, .pyc, etc.)
    - protected metadata (.git/**)
    - in-repository controller directories (no blanket directory exclusion)
    """
    root_path = Path(root).resolve()
    snap: dict[str, dict | None] = {}

    for dirpath, _dirnames, filenames in os.walk(root_path):
        try:
            rel_dir = Path(dirpath).resolve().relative_to(root_path).as_posix()
        except Exception:
            rel_dir = Path(dirpath).as_posix()
        for fname in filenames:
            file_path = Path(dirpath) / fname
            try:
                rel_file = file_path.resolve().relative_to(root_path).as_posix()
            except Exception:
                rel_file = (Path(rel_dir) / fname).as_posix() if rel_dir != "." else fname

            try:
                st = file_path.stat()
                with file_path.open("rb") as f:
                    file_sha = hashlib.file_digest(f, "sha256").hexdigest() if hasattr(hashlib, "file_digest") else hashlib.sha256(f.read()).hexdigest()
                snap[rel_file] = {
                    "sha256": file_sha,
                    "size": st.st_size,
                    "mode": st.st_mode,
                }
            except OSError:
                snap[rel_file] = None

    return snap


def detect_fs_mutations(
    before: dict[str, dict | None],
    after: dict[str, dict | None],
    *,
    exempt_files: set[str] | tuple[str, ...] | list[str] | None = None,
) -> list[str]:
    """Detect created, deleted, or modified files between two filesystem snapshots.

    Optional exempt_files specifies exact file paths that are exempt from reporting
    (e.g. exact controller-created files). No blanket directory exemptions.
    """
    exempt = set(exempt_files) if exempt_files else set()
    mutated = set()
    all_keys = before.keys() | after.keys()
    for k in all_keys:
        if k in exempt:
            continue
        b_info = before.get(k)
        a_info = after.get(k)
        if b_info is None and a_info is not None:
            mutated.add(k)
        elif b_info is not None and a_info is None:
            mutated.add(k)
        elif b_info != a_info:
            mutated.add(k)
    return sorted(mutated)


def is_structurally_valid_snapshot(snap: any) -> bool:
    """Validate snapshot object structural integrity (Fix F).
    - must have non-empty string 'head'
    - must have dict 'files' mapping path -> sha256/hash
    - must have string 'fingerprint'
    """
    if not isinstance(snap, dict):
        return False
    head = snap.get("head")
    if not isinstance(head, str) or not head.strip():
        return False
    fingerprint = snap.get("fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint.strip():
        return False
    files = snap.get("files")
    if not isinstance(files, dict):
        return False
    for path, info in files.items():
        if not isinstance(path, str):
            return False
        if info is not None:
            if not isinstance(info, dict):
                return False
            sha = info.get("sha256")
            if not isinstance(sha, str) or len(sha) != 64:
                return False
    return True


def is_milestone_ready(store: durable.Store, repo) -> bool:
    """Helper to check if store milestone status is valid, untampered, and fully bound (Fix 4, Fix F)."""
    mgr = store.state.get("manager", {})
    if mgr.get("milestone_status") not in (MILESTONE_READY, CRITIC_REVIEWED):
        return False
    saved_fp = mgr.get("milestone_candidate_fingerprint")
    if not saved_fp or not isinstance(saved_fp, str) or not saved_fp.strip():
        return False
    saved_snap = mgr.get("milestone_candidate_snapshot")
    if not saved_snap or not is_structurally_valid_snapshot(saved_snap):
        return False
    curr_snap = snapshot(repo)
    if not is_structurally_valid_snapshot(curr_snap):
        return False
    if curr_snap.get("head") != saved_fp:
        return False
    if curr_snap != saved_snap:
        return False
    section = store.state.get("work_units")
    if not section or not isinstance(section, dict):
        return False
    units = section.get("units", {})
    if not units or not isinstance(units, dict):
        return False
    sequence = section.get("sequence")
    if not isinstance(sequence, list) or len(sequence) == 0:
        return False
    for uid in sequence:
        unit = units.get(uid)
        if not unit or not isinstance(unit, dict):
            return False
        if unit.get("status") != "UNIT_VERIFIED":
            return False
        res = unit.get("result")
        if not res or not isinstance(res, dict):
            return False
        if res.get("trust_level") != "INTERMEDIATE":
            return False
        if res.get("candidate_id") != saved_fp:
            return False
        unit_snap = res.get("candidate_snapshot")
        if not unit_snap or not is_structurally_valid_snapshot(unit_snap):
            return False
        if unit_snap != saved_snap or unit_snap != curr_snap:
            return False
    return True


def validate_sequence_integrity(section: dict):
    """Validate fixed sequence integrity (Fix E).

    Controller must validate sequence container:
    - sequence must be a non-empty list of unique unit IDs
    - set(sequence) == set(units.keys()) (no missing units, no unknown units)
    - all unit specs must have ordinals consistent with sequence index
    """
    if not isinstance(section, dict):
        raise WorkUnitSchedulingError("Invalid work_units section: must be a dict")
    sequence = section.get("sequence")
    if not isinstance(sequence, list) or len(sequence) == 0:
        raise WorkUnitSchedulingError("Sequence must be a non-empty list of unit IDs")
    if len(sequence) != len(set(sequence)):
        raise WorkUnitSchedulingError("Sequence contains duplicate unit IDs")
    units = section.get("units")
    if not isinstance(units, dict) or len(units) == 0:
        raise WorkUnitSchedulingError("Units must be a non-empty mapping")
    if set(sequence) != set(units.keys()):
        missing = sorted(list(set(units.keys()) - set(sequence)))
        unknown = sorted(list(set(sequence) - set(units.keys())))
        raise WorkUnitSchedulingError(
            f"Sequence integrity violation: missing units {missing}, unknown units in sequence {unknown}"
        )
    for idx, uid in enumerate(sequence):
        unit = units[uid]
        ordinal = unit.get("ordinal")
        if ordinal is not None and ordinal != idx:
            raise WorkUnitSchedulingError(
                f"Sequence integrity violation: unit {uid!r} ordinal {ordinal} does not match sequence index {idx}"
            )


# --------------------------------------------------------------------------- WorkUnit scheduler

class WorkUnitScheduler:
    """Deterministic WorkUnit sequential scheduler and execution engine."""

    def __init__(
        self,
        store: durable.Store,
        repo,
        *,
        verifier_registry: dict | None = None,
        supervisor=None,
        clock=time.monotonic,
        agents=None,
        log=None,
        parent_scope: dict | None = None,
        budgets: dict | None = None,
    ):
        self.store = store
        self.repo = repo
        self.verifier_registry = (
            verifier_registry
            if verifier_registry is not None
            else store.state.get("options", {}).get("verifier_registry", {})
        )
        self.supervisor = supervisor
        self.clock = clock
        self._policy_b_shadow_context = None
        self._repair_controller = None

        # Fix 10 / Fix H: Require WorkUnit-compatible executor; validate if provided
        if agents is not None and (not hasattr(agents, "executor") or not callable(getattr(agents.executor, "execute", None))):
            raise WorkUnitSchedulingError(
                "WorkUnit execution requires an executor with a callable execute() method"
            )
        self.agents = agents
        if agents is not None:
            from durable import bind_work_unit_executor
            bind_work_unit_executor(self.store, agents.executor)
        self.log = log
        self.parent_scope = (
            parent_scope
            if parent_scope is not None
            else store.state.get("manager", {}).get("scope", {"allowed_paths": [], "forbidden_paths": []})
        )
        self.budgets = (
            budgets
            if budgets is not None
            else store.state.get("manager", {}).get("budgets", copy.deepcopy(DEFAULT_BUDGETS))
        )
        self.started_at = self.clock()
        self.base_runtime = store.state.get("manager", {}).get("runtime_seconds", 0.0)

        # Fix B, C, D, G: Reconcile active attempts or crash state on startup / reload
        self.reconciliation_stop = None
        try:
            self.reconcile_on_resume()
        except Stop as stop:
            self.reconciliation_stop = stop

    # ----------------------------------------------------------------------- helpers & budget (Fix 3, Fix A, Fix B)

    def runtime(self, now: float | None = None) -> float:
        if now is None:
            now = self.clock()
        return self.base_runtime + (now - self.started_at)

    def remaining_task_budget(self, now: float | None = None) -> float:
        max_runtime = self.budgets.get("max_runtime_seconds", 3600)
        return max(0.0, max_runtime - self.runtime(now=now))

    def check_task_budget(self, now: float | None = None):
        max_runtime = self.budgets.get("max_runtime_seconds", 3600)
        if self.runtime(now=now) >= max_runtime:
            self._persist_runtime()
            raise Stop("BUDGET_EXHAUSTED", "max_total_runtime",
                       f"Runtime {self.runtime(now=now):.3f}s exceeded max_runtime {max_runtime}s")

    def remaining_unit_budget(
        self,
        unit: dict,
        current_attempt_elapsed: float = 0.0,
        current_attempt_id: str | None = None,
    ) -> float | None:
        """Compute remaining budget for unit, including current attempt elapsed time (Fix A)."""
        timeout = unit["spec"]["limits"].get("timeout_seconds")
        if timeout is None:
            return None
        spent = sum(
            (a.get("timeout_info") or {}).get("elapsed_seconds", 0.0)
            for a in unit.get("attempts", [])
            if current_attempt_id is None or a.get("attempt_id") != current_attempt_id
        ) + current_attempt_elapsed
        return max(0.0, timeout - spent)

    def guard_supervision(self):
        """Supervision check enforced at mandatory lifecycle boundaries (Fix 5, Fix D)."""
        if ("safe_qwen_active_recovery" in self.store.state or "safe_qwen_recovery_identity" in self.store.state
                or "repair_authorizations" in self.store.state or "repair_recovery" in self.store.state):
            self._refuse_invocation_replay()
        if self.supervisor is not None:
            children = self.supervisor.reconcile()
            unknown = [c for c in children if c["state"] in ("UNKNOWN", "STARTING")]
            if unknown:
                c = unknown[0]
                self._persist_runtime()
                raise Stop("HUMAN_ACTION_REQUIRED", "UNKNOWN_CHILD",
                           f"{c['child_id']} is {c['state']} ({c['reason']}); process ownership is ambiguous")
            active = [c for c in children if c["state"] in ("RUNNING", "STOPPING")]
            if [c for c in active if c["purpose"] != "model_server"]:
                self._persist_runtime()
                raise Stop("HUMAN_ACTION_REQUIRED", "CHILD_STILL_RUNNING",
                           "A verification/helper child is still running; continuation blocked")

    def get_section(self) -> dict:
        # Work on a detached snapshot; the heartbeat must only ever observe
        # complete, validated commits, never an in-progress unit transition.
        with self.store.mutex:
            section = copy.deepcopy(work_units.load_work_units(self.store.state))
        if section is None:
            raise WorkUnitSchedulingError("No work_units section found in durable state")
        return section

    def _controller_exempt_files(self) -> set[str]:
        """Return the exact controller-owned file paths within repo.root.

        No blanket directory exemptions: only exact files created/managed by the controller.
        Arbitrary descendant files created by a worker within in-repository controller directories
        are NOT exempt and will be detected as scope violations.
        """
        exempt: set[str] = set()
        if hasattr(self.repo, "store") and hasattr(self.repo.store, "directory"):
            try:
                s_dir = Path(self.repo.store.directory).resolve()
                r_root = Path(self.repo.root).resolve()
                if s_dir.is_relative_to(r_root):
                    rel_base = s_dir.relative_to(r_root).as_posix()
                    # Only exact controller-created state and log files
                    for name in ("events.jsonl", "state.json", "original-task.txt"):
                        exempt.add(f"{rel_base}/{name}")
                    if hasattr(self.repo.store, "records"):
                        for rec in self.repo.store.records:
                            art = rec.get("artifact")
                            if isinstance(art, dict) and "path" in art:
                                exempt.add(f"{rel_base}/{art['path']}")
            except Exception:
                pass
        return exempt

    def _persist_runtime(self):
        """Atomically persist updated controller runtime into store (Fix B)."""
        manager = copy.deepcopy(self.store.state.get("manager"))
        if manager is not None:
            now_runtime = round(self.runtime(), 3)
            manager["runtime_seconds"] = now_runtime
            if "active_stage_timing" in manager and isinstance(manager["active_stage_timing"], dict):
                manager["active_stage_timing"]["accounted_runtime"] = now_runtime
            self.store.commit(manager=manager)

    def _persist(self, section: dict):
        """Validate, seal, and atomically commit the work_units section and updated runtime."""
        work_units.validate_section(section)
        manager = copy.deepcopy(self.store.state.get("manager"))
        if manager is not None:
            now_runtime = round(self.runtime(), 3)
            manager["runtime_seconds"] = now_runtime
            if "active_stage_timing" in manager and isinstance(manager["active_stage_timing"], dict):
                manager["active_stage_timing"]["accounted_runtime"] = now_runtime
            self.store.commit(work_units=section, manager=manager)
        else:
            self.store.commit(work_units=section)

    def _persist_unit(self, section: dict, unit: dict):
        work_units.seal_unit_state(unit)
        work_units._reseal_section(section)
        controller = self._repair_controller
        if controller is not None and controller.verification_context is not None:
            controller.verification_section = copy.deepcopy(section)
            self.store._persist_repair_verification(controller)
            return
        self._persist(section)

    # ----------------------------------------------------------------------- guarded verification (Fix D, Fix G, Fix A)

    def _observation_context(self):
        if self._repair_controller is not None:
            self.store._require_repair_controller(self._repair_controller)
            return self._repair_controller.verification_context or self._repair_controller.context
        return self._policy_b_shadow_context

    def _observation_key(self):
        return "repair_observation" if self._repair_controller is not None else "policy_b_shadow"

    def observe_safe_qwen_recovery_first(self, unit_id, *, gateway, timeout_seconds=30, cancel_event=None):
        """Explicit ACTIVE v2 before Attempt 1; stop unverified, without grants."""
        from safe_qwen_active_recovery import run
        return run(self, unit_id, gateway=gateway, timeout_seconds=timeout_seconds, cancel_event=cancel_event)

    def verify_safe_qwen_recovery_first(self, unit_id, *, gateway, timeout_seconds=30, cancel_event=None):
        """Select ACTIVE v3 before Attempt 1; native verification, then review."""
        from safe_qwen_active_recovery import run
        return run(self, unit_id, gateway=gateway, timeout_seconds=timeout_seconds, cancel_event=cancel_event, verify=True)

    def diagnose_safe_qwen_recovery_first(self, unit_id, *, gateway, timeout_seconds=30, cancel_event=None):
        """Select before Attempt 1: ACTIVE v3 verification and diagnostic Policy B only.

        The decision is returned, never persisted as authorization or resumed.
        Existing v3 selections cannot be adopted by this continuous operation.
        """
        from safe_qwen_active_recovery import run
        return run(self, unit_id, gateway=gateway, timeout_seconds=timeout_seconds,
                   cancel_event=cancel_event, verify=True, diagnose=True)

    def authorize_safe_qwen_repair_recovery(self, unit_id, *, gateway, timeout_seconds=30, cancel_event=None):
        """Select ACTIVE v4 before Attempt 1; stop at one GRANTED, never consume."""
        from safe_qwen_active_recovery import run
        return run(self, unit_id, gateway=gateway, timeout_seconds=timeout_seconds,
                   cancel_event=cancel_event, verify=True, grant=True)

    def allocate_safe_qwen_repair_recovery(self, unit_id, *, gateway, timeout_seconds=30, cancel_event=None):
        """Select ACTIVE v5 before Attempt 1; consume/allocate, never launch Attempt 2."""
        from safe_qwen_active_recovery import run
        return run(self, unit_id, gateway=gateway, timeout_seconds=timeout_seconds,
                   cancel_event=cancel_event, verify=True, grant=True, allocate=True)

    def dispatch_safe_qwen_repair_recovery(self, unit_id, *, gateway, timeout_seconds=30, cancel_event=None):
        """Select ACTIVE v6 before Attempt 1; one second worker, stop unverified."""
        from safe_qwen_active_recovery import run
        return run(self, unit_id, gateway=gateway, timeout_seconds=timeout_seconds,
                   cancel_event=cancel_event, verify=True, grant=True, allocate=True, dispatch=True)

    def qualify_safe_qwen_repair_recovery(self, unit_id, *, gateway, timeout_seconds=30, cancel_event=None):
        """Select ACTIVE v7 before Attempt 1; verify repaired Attempt 2, then review."""
        from safe_qwen_active_recovery import run
        return run(self, unit_id, gateway=gateway, timeout_seconds=timeout_seconds,
                   cancel_event=cancel_event, verify=True, grant=True, allocate=True, dispatch=True, verify_second=True)

    def authorize_repair_recovery(self, unit_id: str) -> dict:
        """Explicit controller opt-in BEFORE Attempt 1; stop at GRANTED or refusal.

        Accepts only a unit selector. No caller-supplied observations or grant
        payloads are accepted, and a loaded mode can never recreate this session.
        """
        return self._run_repair_recovery(unit_id, allocate=False)

    def allocate_repair_recovery(self, unit_id: str) -> dict:
        """Opt in BEFORE Attempt 1 to consume/allocate continuously, never dispatch.

        This cannot adopt a grant from authorize_repair_recovery() or a reload.
        """
        return self._run_repair_recovery(unit_id, allocate=True)

    def dispatch_offline_repair_recovery(self, unit_id: str, script: dict, *, timeout_seconds=30) -> dict:
        """Explicit BEFORE Attempt 1: fixed offline actions, one WAL-bound repair.

        This phase stops after executor evidence. No Attempt 2 verifier, acceptance,
        historical allocation adoption, live executor, or ordinary repair loop.
        v1 integration supports mutation WorkUnits with exact file scopes only.
        """
        if type(script) is not dict:
            raise WorkUnitSchedulingError("Offline dispatch requires a closed data-only script")
        return self._run_repair_recovery(unit_id, allocate=True, offline_script=script, offline_timeout=timeout_seconds)

    def _offline_first_actions(self, unit_id):
        from offline_scripted_executor import validate_script, _json
        if type(self.agents.executor) is not ScriptedExecutor or self.clock is not time.monotonic:
            raise WorkUnitSchedulingError("Offline dispatch requires actual ScriptedExecutor and native monotonic clock")
        executor = self.agents.executor
        behavior = executor.rules.get((unit_id, 1), executor.rules.get((unit_id, None), executor.default_behavior))
        if type(behavior) is not dict or set(behavior) != {"writes"} or type(behavior["writes"]) is not dict or not behavior["writes"]:
            raise WorkUnitSchedulingError("Offline Attempt 1 permits only fixed bounded file writes; no callback")
        actions = {"schema_version": 1, "actions": [dict(op="write", path=p, content=v) for p, v in behavior["writes"].items()]}
        validate_script(actions, list(behavior["writes"]), [])
        if len(_json(actions)) > 8192:
            raise WorkUnitSchedulingError("Offline Attempt 1 input exceeds 8 KiB")
        return copy.deepcopy(actions)

    def qualify_offline_repair_recovery(self, unit_id: str, script: dict, *, timeout_seconds=30) -> dict:
        """Select BEFORE Attempt 1: one offline repair and native qualification.

        Pass/fail evidence stops for review and grants no checkpoint authority.
        Existing dispatch-only operations cannot continue into this operation.
        """
        if type(script) is not dict:
            raise WorkUnitSchedulingError("Offline qualification requires a closed data-only script")
        return self._run_repair_recovery(unit_id, allocate=True, offline_script=script,
                                        offline_timeout=timeout_seconds, verify=True)

    def _run_repair_recovery(self, unit_id, *, allocate, offline_script=None, offline_timeout=30, verify=False):
        from supervision import Supervisor, _validated_store
        import environment
        if os.name != "nt":
            raise WorkUnitSchedulingError("Active recovery requires native Windows verification")
        if self.reconciliation_stop is not None:
            raise self.reconciliation_stop
        self._refuse_invocation_replay()
        try:
            with self.store.mutex:
                _validated_store(self.store)
                from manager import validate_repository
                validate_repository(self.store, self.repo)
                if (not isinstance(self.supervisor, Supervisor) or self.supervisor.store is not self.store
                        or self.agents is None or not callable(getattr(self.agents.executor, "execute", None))):
                    raise WorkUnitSchedulingError("Active recovery requires controller Supervisor and executor")
                self.supervisor.require_idle()
                if (self.budgets != self.store.state["manager"]["budgets"]
                        or self.parent_scope != self.store.state["manager"]["scope"]
                        or self.verifier_registry != self.store.state["options"]["verifier_registry"]):
                    raise WorkUnitSchedulingError("Active recovery contracts differ from WAL")
                drift = environment.compare(self.store.state["environment"],
                                            durable.current_fingerprint(self.store, executor=self.agents.executor))
                if drift["decision"] not in {"MATCH", "INFORMATIONAL"}:
                    raise WorkUnitSchedulingError("Active recovery environment preflight refused")
                section = self.get_section()
                unit = section["units"].get(unit_id)
                if not unit or unit["attempts"]:
                    raise WorkUnitSchedulingError("Active recovery requires an unattempted ready unit")
                recipe = first_actions = None
                if offline_script is not None:
                    from offline_scripted_executor import validate_script, _json
                    import math
                    if unit["spec"]["mode"] != "mutation":
                        raise WorkUnitSchedulingError("Offline repair dispatch requires a mutation WorkUnit")
                    if type(offline_timeout) not in (int, float) or not math.isfinite(offline_timeout) or not 0 < offline_timeout <= 3600:
                        raise WorkUnitSchedulingError("A finite offline timeout is required")
                    first_actions = self._offline_first_actions(unit_id)
                    allowed = copy.deepcopy(unit["spec"]["scope"]["allowed_paths"])
                    forbidden = sorted(set(unit["spec"]["scope"]["forbidden_paths"] + self.parent_scope.get("forbidden_paths", [])))
                    validate_script(offline_script, allowed, forbidden)
                    validate_script(first_actions, allowed, forbidden)
                    recipe = dict(script=copy.deepcopy(offline_script), allowed_paths=allowed, forbidden_paths=forbidden,
                                  timeout_seconds=offline_timeout)
                    if len(_json(recipe)) > 8192:
                        raise WorkUnitSchedulingError("Complete repair input exceeds 8 KiB")
                if unit not in self.evaluate_readiness(section):
                    raise WorkUnitSchedulingError("Active recovery requires an unattempted ready unit")
                self._repair_controller = (self.store._begin_repair_recovery(self, unit_id, allocate=True, offline_recipe=recipe, verify=verify) if recipe
                                          else self.store._begin_repair_recovery(self, unit_id, allocate=True) if allocate
                                          else self.store._begin_repair_recovery(self, unit_id))
                if first_actions is not None and first_actions != self._offline_first_actions(unit_id):
                    raise WorkUnitSchedulingError("Offline Attempt 1 recipe changed")
            result = self.execute_attempt(unit, section, 1)
            if not result.get("verification_complete"):
                raise WorkUnitSchedulingError("Recovery observations incomplete; no grant issued")
            if allocate:
                # Keep the reviewed grant and allocation adjacent even when the
                # Supervisor heartbeat is active. Both methods reuse this RLock.
                with self.store.mutex:
                    grant = self.store._issue_repair_grant(self._repair_controller)
                    grant = self.store._consume_repair_grant(self._repair_controller)
            else:
                grant = self.store._issue_repair_grant(self._repair_controller)
            if offline_script is not None:
                from offline_scripted_executor import _run_controller_script
                evidence = _run_controller_script(self._repair_controller)
                verification = None
                if verify and self.store.state["repair_dispatch"]["result"]["outcome"] == "executor_succeeded":
                    verification = self._verify_offline_repair(self._repair_controller)
                return {"repair_recovery": True, "authorization": grant, "executor_evidence": evidence,
                        **({"verification": verification} if verify else {}),
                        "dispatch": copy.deepcopy(self.store.state["repair_dispatch"]), "status": "HUMAN_ACTION_REQUIRED"}
            return {"repair_recovery": True, "authorization": grant, "status": "HUMAN_ACTION_REQUIRED"}
        finally:
            if self._repair_controller is not None:
                self._repair_controller.closed = True
            self._repair_controller = None
            self.reconciliation_stop = Stop("HUMAN_ACTION_REQUIRED", "REPAIR_RECOVERY_REVIEW_REQUIRED",
                                            "Offline qualification ended; independent review and final acceptance gates remain required" if verify else
                                            "Offline dispatch ended; native verification and acceptance require review" if offline_script is not None else
                                            "Active allocation ended; executor dispatch is disabled" if allocate else
                                            "Active recovery ended; no consumption or Attempt 2 is enabled")

    def _guard_repair_verification(self, controller, active_child_id=None):
        """Live authority and original cumulative deadlines, never replay data."""
        import environment
        import offline_scripted_executor as offline
        from supervision import _validated_store
        self.store._require_repair_controller(controller)
        _validated_store(self.store)
        state, mode = self.store.state, controller.mode
        context, adapter = controller.verification_context, controller.dispatch
        if (controller.scheduler is not self or mode.get("operation") != "OFFLINE_VERIFY"
                or context is None or adapter is None or adapter.last_record is None
                or adapter.last_record["state"]["repair_dispatch"] != state.get("repair_dispatch")
                or adapter.data != state["repair_dispatch"]
                or state["repair_dispatch"]["phase"] != "COMPLETE"
                or state["repair_dispatch"]["result"]["outcome"] != "executor_succeeded"):
            raise WorkUnitSchedulingError("No live completed dispatch for qualification")
        live = state["supervision"]["controller"]
        unit = state["work_units"]["units"][mode["unit_id"]]
        first = unit["attempts"][0]
        grant = state["repair_authorizations"]["repair-" + mode["recovery_id"]]
        if (self._repair_controller is not controller or self.clock is not time.monotonic
                or live["pid"] != os.getpid() or live["identity"] != durable.winprocess.identity(os.getpid())
                or live["boot"] != durable.winprocess.boot_identity()
                or self.supervisor.store is not self.store or str(self.repo.root) != state["target_repository"]
                or self.budgets != state["manager"]["budgets"] or self.parent_scope != state["manager"]["scope"]
                or self.verifier_registry != state["options"]["verifier_registry"]
                or durable.digest(unit["spec"]) != mode["spec_sha256"] or offline._source_identity() != mode["offline_sources"]
                or len(unit["attempts"]) != 2 or first["repair_observation"]["observations"] != controller.observations
                or unit["attempts"][1]["attempt_id"] != context["attempt_id"] or unit["attempts"][1]["completed_at"] is not None):
            raise WorkUnitSchedulingError("Qualification ownership/contracts changed")
        drift = environment.compare(state["environment"], durable.current_fingerprint(self.store, executor=self.agents.executor))
        if drift["decision"] not in {"MATCH", "INFORMATIONAL"}:
            raise WorkUnitSchedulingError("Qualification environment drift")
        if (self._shadow_snapshot() != context["candidate_snapshot"]
                or self._shadow_fs_snapshot() != context["candidate_fs_snapshot"]
                or hashlib.sha256(offline._json(offline._snapshot(Path(self.repo.root)))).hexdigest() != adapter.evidence["after_snapshot_sha256"]):
            raise WorkUnitSchedulingError("Qualification candidate/scope drift")
        if self.clock() >= min(mode["task_deadline"], grant["budget"]["unit_deadline"]):
            raise WorkUnitSchedulingError("Qualification cumulative budget exhausted")
        if active_child_id is None:
            self.supervisor.require_idle()
        else:
            own_child = self.supervisor.child(active_child_id)
            children = [self.supervisor.observe(c) for c in state["supervision"]["children"].values() if c["child_id"] != active_child_id]
            own = own_child if own_child["invocation"]["phase"] == "INTENT" else self.supervisor.observe(own_child)
            if (own["state"] not in {"STARTING", "RUNNING"}
                    or (own["state"] == "STARTING" and (own["invocation"]["phase"] != "INTENT" or own["pid"] is not None))
                    or own["invocation"]["attempt_id"] != context["attempt_id"]
                    or own["invocation"]["unit_id"] != context["unit_id"]
                    or any(c["child_id"] != active_child_id and c["state"] in {"STARTING", "RUNNING", "STOPPING", "UNKNOWN"} for c in children)):
                raise WorkUnitSchedulingError("Qualification process ownership ambiguous")

    def _verify_offline_repair(self, controller):
        """Seed once, then reuse the approved native sequence and observation binding."""
        with self.store.mutex:
            self.store._require_repair_controller(controller)
            if controller.verification_context is not None or "repair_verification" in self.store.state:
                raise WorkUnitSchedulingError("Qualification cannot be adopted or repeated")
            section = self.get_section()
            unit = section["units"][controller.mode["unit_id"]]
            att = unit["attempts"][1]
            # An effective budget origin: first + second elapsed time equals the
            # uninterrupted unit interval, including grant/dispatch overhead.
            # The historical first attempt's clock and evidence never change.
            att["started_at_clock"] = controller.mode["started_clock"] + unit["attempts"][0]["timeout_info"]["elapsed_seconds"]
            candidate, fs = self._shadow_snapshot(), self._shadow_fs_snapshot()
            controller.verification_context = {**copy.deepcopy(controller.context), "attempt_id": att["attempt_id"],
                "started_clock": att["started_at_clock"], "candidate_snapshot": candidate, "candidate_fs_snapshot": fs}
            self._guard_repair_verification(controller)
            from offline_scripted_executor import _snapshot
            controller.verification_context["tree_snapshot"] = _snapshot(Path(self.repo.root))
            att["repair_observation"] = dict(policy="B", mode="ACTIVE", phase="STARTED", observations={},
                registry_sha256=controller.mode["registry_sha256"], candidate_snapshot=candidate, candidate_fs_snapshot=fs)
            self._persist_unit(section, unit)  # Qualification intent before any native invocation.
        result = self._run_policy_b_shadow_verification(unit, section, att["attempt_id"], 0)
        completed = self.store.state["work_units"]["units"][unit["unit_id"]]["attempts"][1]
        return {**result, "outcome": completed["outcome"], "failure_classification": completed["failure_classification"]}

    def _fresh_repair_classification(self, controller):
        """Revalidate the live controller and every frozen input, then classify."""
        from supervision import _validated_store
        from retry_eligibility import classify_retry_eligibility
        import environment
        self.store._require_repair_controller(controller)
        _validated_store(self.store)
        self.supervisor.require_idle()
        state = self.store.state
        mode = state["repair_recovery"]
        durable.validate_repair_recovery(state)
        live = state["supervision"]["controller"]
        if (live["pid"] != os.getpid() or live["identity"] != durable.winprocess.identity(os.getpid())
                or live["boot"] != durable.winprocess.boot_identity()
                or self.budgets != state["manager"]["budgets"] or self.parent_scope != state["manager"]["scope"]
                or self.verifier_registry != state["options"]["verifier_registry"]):
            raise WorkUnitSchedulingError("Recovery controller/session/contracts changed")
        unit = copy.deepcopy(state["work_units"]["units"][mode["unit_id"]])
        att = unit["attempts"][0]
        candidate = att["candidate_snapshot"]
        if (unit["status"] != "VERIFYING" or att["completed_at"] is None
                or self._shadow_snapshot() != candidate
                or self._shadow_fs_snapshot() != att["repair_observation"]["candidate_fs_snapshot"]):
            raise WorkUnitSchedulingError("Recovery candidate/attempt changed")
        elapsed = self.clock() - att["started_at_clock"]
        if elapsed < 0 or self.clock() >= mode["task_deadline"]:
            raise WorkUnitSchedulingError("Recovery deadline exhausted or clock ambiguous")
        drift = environment.compare(state["environment"], durable.current_fingerprint(self.store, executor=self.agents.executor))
        if drift["decision"] not in {"MATCH", "INFORMATIONAL"}:
            raise WorkUnitSchedulingError("Recovery environment changed")
        for entry in att["verifier_result"].values():
            entry["controller_evidence"].update(run_id=state["run_id"], unit_id=mode["unit_id"],
                                                attempt_id=mode["source_attempt_id"])
        revision, sequence = state["revision"], len(self.store.records)
        decision = classify_retry_eligibility(
            unit, policy="B", supervisor=self.supervisor, require_idle=True, verifier_registry=self.verifier_registry,
            environment_status="SAFE", remaining_unit_budget=self.remaining_unit_budget(unit, elapsed, att["attempt_id"]),
            remaining_task_budget=self.remaining_task_budget(), candidate_snapshot=candidate,
            verification_snapshot_before=att["verification_snapshot_before"],
            verification_snapshot_after=att["verification_snapshot_after"], repo_mutated_during_verification=False,
            context={"run_id": state["run_id"]})
        if revision != self.store.state["revision"] or sequence != len(self.store.records):
            raise WorkUnitSchedulingError("Classifier changed WAL; grant refused")
        _validated_store(self.store)
        durable.validate_repair_recovery(self.store.state)
        live_after = self.store.state["supervision"]["controller"]
        drift_after = environment.compare(self.store.state["environment"], durable.current_fingerprint(
            self.store, executor=self.agents.executor))
        if (live_after["pid"] != os.getpid() or live_after["identity"] != durable.winprocess.identity(os.getpid())
                or live_after["boot"] != durable.winprocess.boot_identity()
                or self.budgets != self.store.state["manager"]["budgets"]
                or self.parent_scope != self.store.state["manager"]["scope"]
                or self.verifier_registry != self.store.state["options"]["verifier_registry"]
                or drift_after["decision"] not in {"MATCH", "INFORMATIONAL"}):
            raise WorkUnitSchedulingError("Recovery inputs changed during classification")
        if (self._shadow_snapshot() != candidate
                or self._shadow_fs_snapshot() != att["repair_observation"]["candidate_fs_snapshot"]
                or self.clock() >= mode["task_deadline"] or self.remaining_task_budget() < 1
                or self.remaining_unit_budget(unit, self.clock() - att["started_at_clock"], att["attempt_id"]) < 1):
            raise WorkUnitSchedulingError("Recovery candidate/budget changed during classification")
        return decision

    def observe_policy_b_shadow(self, unit_id: str) -> dict:
        """Trusted controller opt-in: execute one first attempt, diagnose, then stop.

        Never enters execute_unit's repair loop or accepts verification progress.
        The ephemeral context permits only this uninterrupted verifier sequence;
        the durable attempt marker refuses recovery even before native creation.
        """
        from supervision import Supervisor, _validated_store
        if os.name != "nt":
            raise WorkUnitSchedulingError("Policy B shadow observation requires Windows")
        if self.reconciliation_stop is not None:
            raise self.reconciliation_stop
        self._refuse_invocation_replay()
        try:
            with self.store.mutex:
                _validated_store(self.store)
                from manager import validate_repository
                validate_repository(self.store, self.repo)
                if not isinstance(self.supervisor, Supervisor) or self.supervisor.store is not self.store:
                    raise WorkUnitSchedulingError("Shadow observation requires this run's Supervisor")
                self.supervisor.require_idle()
                if self.agents is None or not callable(getattr(self.agents.executor, "execute", None)):
                    raise WorkUnitSchedulingError("Shadow observation requires an explicit executor")
                if (self.budgets != self.store.state["manager"]["budgets"]
                        or self.parent_scope != self.store.state["manager"]["scope"]):
                    raise WorkUnitSchedulingError("Shadow scope/budgets differ from approved WAL state")
                import environment
                drift = environment.compare(self.store.state["environment"], durable.current_fingerprint(
                    self.store, executor=self.agents.executor))
                if drift["decision"] not in {"MATCH", "INFORMATIONAL"}:
                    raise WorkUnitSchedulingError("Shadow environment preflight refused")
                registry = copy.deepcopy(self.store.state["options"].get("verifier_registry"))
                if type(registry) is not dict or registry != self.verifier_registry:
                    raise WorkUnitSchedulingError("Shadow registry differs from approved WAL registry")
                section = self.get_section()
                unit = section["units"].get(unit_id)
                if not unit or unit["attempts"] or unit not in self.evaluate_readiness(section):
                    raise WorkUnitSchedulingError("Shadow observation requires an unattempted ready WorkUnit")
                self._policy_b_shadow_context = {
                    "unit_id": unit_id, "attempt_id": f"{unit_id}-attempt-1", "registry": registry,
                    "spec_sha256": durable.digest(unit["spec"]), "started_clock": self.clock(),
                }
            result = self.execute_attempt(unit, section, 1)
            if not result.get("policy_b_shadow"):
                # Executor/scope failures are not verifier evidence. Preserve the
                # existing sealed failure; do not enter repair or manufacture it.
                return {"policy_b_shadow": True, "verification_complete": False,
                        "executor_failure": result}
            return result
        finally:
            self._policy_b_shadow_context = None
            self.reconciliation_stop = Stop("HUMAN_ACTION_REQUIRED", "INVOCATION_REVIEW_REQUIRED",
                                            "Policy B shadow observation requires independent review")

    def _shadow_snapshot(self):
        from retry_eligibility import validate_normalize_snapshot
        value = snapshot(self.repo)
        _, error = validate_normalize_snapshot(value, "controller capture")
        if error is not None:
            raise WorkUnitSchedulingError("Incomplete canonical shadow snapshot")
        return value

    def _shadow_fs_snapshot(self):
        value = capture_fs_snapshot(self.repo.root)
        if not value or any(info is None for info in value.values()):
            raise WorkUnitSchedulingError("Incomplete filesystem shadow snapshot")
        return value

    def _bind_shadow_observation(self, invocation, result, candidate):
        """Authenticate the pre-launch WAL chain, then adapt a detached native record.

        Invocation/token/sequence references stay outside the closed native schema.
        Classifier-required IDs label authenticated facts, never authenticate JSON.
        """
        from supervision import _validated_store
        with self.store.mutex:
            _validated_store(self.store)
            child = self.supervisor.child(invocation.child_id)
            intent = child["invocation"]
            expected = {**invocation.intent, "phase": "COMPLETE"}
            native = copy.deepcopy(result["controller_evidence"])
            if (intent != expected or intent["candidate_fingerprint"] != candidate["fingerprint"]
                    or child["run_id"] != self.store.state["run_id"] or child["state"] != "EXITED"
                    or native.get("child_id") != invocation.child_id or native.get("job") != child["job"]
                    or native.get("verifier") != {"pid": child["pid"], "identity": child["identity"]}
                    or native.get("observed_exit_code") != child["exit_code"]
                    or native.get("intended_argv") != intent["intended_argv"]
                    or native.get("resolved_argv") != intent["resolved_argv"]
                    or native.get("resolved_command_sha256") != child["command_sha256"]
                    or native.get("cleanup") != "observed_empty" or native.get("execution_started") is not True
                    or any(k in native for k in ("run_id", "unit_id", "attempt_id", "invocation_id", "verifier_id"))):
                raise WorkUnitSchedulingError("Foreign or incomplete native invocation observation")
            events = ("invocation_intent", "invocation_bound", "invocation_release", "process_exit", "invocation_complete")
            records = [r for r in self.store.records if (r.get("transition") or {}).get("child_id") == child["child_id"]
                       and (r.get("transition") or {}).get("event") in events]
            if [r["transition"]["event"] for r in records] != list(events):
                raise WorkUnitSchedulingError("Missing or replayed invocation WAL lifecycle")
            binding = {"child_id": child["child_id"], "invocation_id": child["token"],
                       "wal": {r["transition"]["event"]: r["sequence"] for r in records}}
            evidence = {**native, "run_id": child["run_id"], "unit_id": intent["unit_id"],
                        "attempt_id": intent["attempt_id"]}
            entry = {"argv": copy.deepcopy(intent["intended_argv"]), "exit_code": child["exit_code"],
                     "passed": native["termination"] == "natural" and child["exit_code"] == 0,
                     "controller_evidence": evidence}
            return {"binding": binding, "native": native}, entry

    def _run_policy_b_shadow_verification(self, unit, section, attempt_id, attempt_elapsed):
        import environment
        from retry_eligibility import classify_retry_eligibility, POLICY_B
        from supervision import _VerifierInvocation, _run_observed_verifier, _validated_store
        context = self._observation_context()
        if (context is None or context["unit_id"] != unit["unit_id"] or context["attempt_id"] != attempt_id
                or durable.digest(unit["spec"]) != context["spec_sha256"]):
            raise WorkUnitSchedulingError("Shadow sequence has no current controller context")
        att = next(a for a in unit["attempts"] if a["attempt_id"] == attempt_id)
        active = self._repair_controller is not None
        second = active and self._repair_controller.verification_context is not None
        shadow = att[self._observation_key()]
        self.supervisor.require_idle()
        candidate = shadow["candidate_snapshot"]
        work_units.transition_unit(unit, "VERIFYING")
        if second:
            att["stage"] = "verifying"
        started = self.clock()
        attempt_elapsed = started - att["started_at_clock"]
        att["stage_timing"] = dict(stage="VERIFYING", unit_id=unit["unit_id"], attempt_id=attempt_id,
                                   stage_start_clock=started, stage_start_runtime=self.runtime(),
                                   attempt_elapsed_at_start=attempt_elapsed)
        shadow["phase"] = "OBSERVING"
        self._persist_unit(section, unit)
        shadow["verification_snapshot_before"] = self._shadow_snapshot()
        fs_before = self._shadow_fs_snapshot()
        shadow["verification_fs_snapshot_before"] = fs_before
        if second:
            from offline_scripted_executor import _snapshot, _json
            def tree_hash():
                return hashlib.sha256(_json(_snapshot(Path(self.repo.root)))).hexdigest()
            shadow["verification_tree_before_sha256"] = tree_hash()
        results = {}
        contaminated = (shadow["verification_snapshot_before"] != candidate
                        or fs_before != shadow["candidate_fs_snapshot"])
        if second:
            contaminated = contaminated or shadow["verification_tree_before_sha256"] != self.store.state["repair_verification"]["tree_sha256"]
        self._persist_unit(section, unit)
        for vid in unit["spec"]["verifier_ids"]:
            if contaminated:
                break
            with self.store.mutex:
                _validated_store(self.store)
                if (self.store.state["options"]["verifier_registry"] != context["registry"]
                        or self.verifier_registry != context["registry"]
                        or self._shadow_snapshot() != candidate or self._shadow_fs_snapshot() != fs_before):
                    contaminated = True
                    break
                # This sequence retains its original context. Construction is
                # checked against it BEFORE any native child can be created.
                invocation = (_VerifierInvocation(self, unit["unit_id"], attempt_id, vid, _repair_controller=self._repair_controller)
                              if second else _VerifierInvocation(self, unit["unit_id"], attempt_id, vid))
                if (invocation.intent["registry_sha256"] != shadow["registry_sha256"]
                        or invocation.intent["spec_sha256"] != context["spec_sha256"]
                        or invocation.intent["candidate_fingerprint"] != candidate["fingerprint"]):
                    raise WorkUnitSchedulingError("Invocation diverged from frozen shadow sequence")
            result = _run_observed_verifier(
                self.supervisor, invocation.intent["resolved_argv"], invocation.intent["intended_argv"],
                self.repo.root, invocation.intent["deadline"] - self.clock(),
                self.repo.config["max_output_chars"], invocation=invocation)
            observation, entry = self._bind_shadow_observation(invocation, result, candidate)
            observation["after_snapshot"] = self._shadow_snapshot()
            observation["after_fs_snapshot"] = self._shadow_fs_snapshot()
            if second:
                observation["after_tree_sha256"] = tree_hash()
            shadow["observations"][vid] = observation
            if active:
                if second:
                    self._repair_controller.verification_observations = copy.deepcopy(shadow["observations"])
                else:
                    self._repair_controller.observations = copy.deepcopy(shadow["observations"])
            results[vid] = entry
            contaminated = (observation["after_snapshot"] != candidate or observation["after_fs_snapshot"] != fs_before)
            if second:
                contaminated = contaminated or observation["after_tree_sha256"] != shadow["verification_tree_before_sha256"]
            self._persist_unit(section, unit)
            if result["termination"] != "natural":
                break
        shadow["verification_snapshot_after"] = self._shadow_snapshot()
        shadow["verification_fs_snapshot_after"] = self._shadow_fs_snapshot()
        if second:
            shadow["verification_tree_after_sha256"] = tree_hash()
        contaminated = (contaminated or shadow["verification_snapshot_after"] != candidate
                        or shadow["verification_fs_snapshot_after"] != fs_before)
        if second:
            contaminated = contaminated or shadow["verification_tree_after_sha256"] != shadow["verification_tree_before_sha256"]
        elapsed = attempt_elapsed + self.clock() - started
        timed_out = any(o["native"]["termination"] == "timeout" for o in shadow["observations"].values())
        failed = len(results) != len(unit["spec"]["verifier_ids"]) or any(not r["passed"] for r in results.values())
        outcome = "timeout" if timed_out else "failed" if failed or contaminated else "passed"
        failure = ("verifier_timeout" if timed_out else "verifier_tampered_repo" if contaminated
                   else "verification_failed" if failed else None)
        drift = environment.compare(self.store.state["environment"], durable.current_fingerprint(
            self.store, executor=self.agents.executor))
        shadow["environment_comparison"] = drift
        if second:
            if drift["decision"] not in {"MATCH", "INFORMATIONAL"}:
                raise WorkUnitSchedulingError("Qualification environment changed during verification")
            grant = self.store.state["repair_authorizations"]["repair-" + self._repair_controller.mode["recovery_id"]]
            if self.clock() >= min(self._repair_controller.mode["task_deadline"], grant["budget"]["unit_deadline"]):
                outcome, failure = "timeout", "verifier_timeout"
        environment_status = "SAFE" if drift["decision"] in {"MATCH", "INFORMATIONAL"} else drift
        # Evaluate only a detached observation projection. This does not authorize
        # a WorkUnit state transition, repair, final verification or checkpoint.
        projected = copy.deepcopy(unit)
        next(a for a in projected["attempts"] if a["attempt_id"] == attempt_id).update(
            outcome=outcome, failure_classification=failure, verifier_result=results,
            candidate_snapshot=candidate, verification_snapshot_before=shadow["verification_snapshot_before"],
            verification_snapshot_after=shadow["verification_snapshot_after"],
            timeout_info={"elapsed_seconds": elapsed})
        with self.store.mutex:
            _validated_store(self.store)
            self.supervisor.require_idle()
            if (self._shadow_snapshot() != shadow["verification_snapshot_after"]
                    or self._shadow_fs_snapshot() != shadow["verification_fs_snapshot_after"]):
                raise WorkUnitSchedulingError("Candidate drift before shadow classification")
            elapsed = self.clock() - att["started_at_clock"]
            revision, sequence = self.store.state["revision"], len(self.store.records)
            decision = None if active else classify_retry_eligibility(
                projected, policy=POLICY_B, supervisor=self.supervisor, require_idle=True,
                verifier_registry=context["registry"], environment_status=environment_status,
                remaining_unit_budget=self.remaining_unit_budget(unit, elapsed, attempt_id),
                remaining_task_budget=self.remaining_task_budget(),
                candidate_snapshot=candidate, verification_snapshot_before=shadow["verification_snapshot_before"],
                verification_snapshot_after=shadow["verification_snapshot_after"],
                repo_mutated_during_verification=contaminated,
                context={"run_id": self.store.state["run_id"]})
            # The frozen classifier catches Supervisor errors. It must not hide
            # an uncertain WAL write and induce a diagnostic follow-up append.
            if self.store.state["revision"] != revision or len(self.store.records) != sequence:
                raise WorkUnitSchedulingError("Classifier changed WAL; diagnostic persistence refused")
            _validated_store(self.store)
            if (self._shadow_snapshot() != shadow["verification_snapshot_after"]
                    or self._shadow_fs_snapshot() != shadow["verification_fs_snapshot_after"]):
                raise WorkUnitSchedulingError("Candidate drift during shadow classification")
            if second:
                drift = environment.compare(self.store.state["environment"], durable.current_fingerprint(self.store, executor=self.agents.executor))
                if drift["decision"] not in {"MATCH", "INFORMATIONAL"} or tree_hash() != shadow["verification_tree_after_sha256"]:
                    raise WorkUnitSchedulingError("Qualification environment/tree drift before outcome seal")
            shadow.update(phase="OBSERVATIONS_COMPLETE" if active else "DIAGNOSTIC_COMPLETE",
                          decision=None if active else decision.to_dict(),
                          verification_complete=len(results) == len(unit["spec"]["verifier_ids"]))
            if active:
                att.update(candidate_snapshot=candidate, verification_snapshot_before=shadow["verification_snapshot_before"],
                           verification_snapshot_after=shadow["verification_snapshot_after"])
                results = copy.deepcopy(results)
                for vid, entry in results.items():
                    entry["controller_evidence"] = copy.deepcopy(shadow["observations"][vid]["native"])
            work_units.complete_attempt(unit, attempt_id, outcome=outcome,
                                        execution_result=att["execution_result"] if second else None,
                                        failure_classification=failure, verifier_result=results,
                                        changed_paths=att["changed_paths"],
                                        timeout_info={"elapsed_seconds": self.clock() - att["started_at_clock"]})
            if second and outcome != "passed":
                work_units.transition_unit(unit, "UNIT_FAILED")
            self._persist_unit(section, unit)
        return {"repair_recovery" if active else "policy_b_shadow": True,
                "decision": None if active else decision.to_dict(),
                "verification_complete": shadow["verification_complete"], "unit_id": unit["unit_id"]}

    def invoke_native_verifier(self, unit_id: str, attempt_id: str, verifier_id: str) -> dict:
        """Explicit foundation-only opt-in. No retry, result acceptance or resume.

        Requires an already WAL-persisted VERIFYING first attempt and its timing.
        All command, candidate and budget facts are derived by the controller;
        callers select identifiers only, never supply an invocation record.
        """
        from supervision import _VerifierInvocation, _run_observed_verifier
        if os.name != "nt":
            raise WorkUnitSchedulingError("Native invocation requires Windows")
        if self.reconciliation_stop is not None:
            raise self.reconciliation_stop
        try:
            invocation = _VerifierInvocation(self, unit_id, attempt_id, verifier_id)
            result = _run_observed_verifier(
                self.supervisor, invocation.intent["resolved_argv"], invocation.intent["intended_argv"],
                self.repo.root, invocation.intent["deadline"] - self.clock(),
                self.repo.config["max_output_chars"], invocation=invocation)
            self._persist_runtime()
            return result
        finally:
            # This slice cannot settle a WorkUnit or authorize another attempt.
            # On failure, keep durable stage timing/deadline and make no further
            # append after a possibly uncertain WAL write.
            self.reconciliation_stop = Stop("HUMAN_ACTION_REQUIRED", "INVOCATION_REVIEW_REQUIRED",
                                            "Native invocation requires review; WorkUnit integration is not enabled")

    def _refuse_invocation_replay(self):
        """Do not let legacy recovery/dispatch relaunch a foundation invocation."""
        if "safe_qwen_active_recovery" in self.store.state:
            raise Stop("HUMAN_ACTION_REQUIRED", "SAFEQWEN_ACTIVE_REVIEW_REQUIRED",
                       "ACTIVE Attempt 1 cannot be adopted, verified, repaired or replayed")
        if "safe_qwen_recovery_identity" in self.store.state:
            raise Stop("HUMAN_ACTION_REQUIRED", "SAFEQWEN_RECOVERY_IDENTITY_ONLY",
                       "Recovery identity reserves future invocations; execution is not authorized")
        if getattr(self.store, "_repair_write_failed", False):
            raise Stop("HUMAN_ACTION_REQUIRED", "REPAIR_RECOVERY_REPLAY_REFUSED", "Uncertain recovery WAL; no launch allowed")
        if (self.reconciliation_stop is not None and self.reconciliation_stop.reason in
                {"REPAIR_RECOVERY_REVIEW_REQUIRED", "REPAIR_RECOVERY_REPLAY_REFUSED"}):
            raise self.reconciliation_stop
        if "repair_authorizations" in self.store.state:
            raise Stop("HUMAN_ACTION_REQUIRED", "REPAIR_AUTHORIZATION_DORMANT",
                       "Persisted repair authorization requires review; consumption and dispatch are disabled")
        if "repair_recovery" in self.store.state:
            if self._repair_controller is None:
                raise Stop("HUMAN_ACTION_REQUIRED", "REPAIR_RECOVERY_REPLAY_REFUSED",
                           "Active recovery session cannot be reconstructed or replayed")
            self.store._require_repair_controller(self._repair_controller)
            return
        if (self.reconciliation_stop is not None and self.reconciliation_stop.reason in
                {"INVOCATION_REVIEW_REQUIRED", "INVOCATION_REPLAY_REFUSED"}):
            raise self.reconciliation_stop
        units = self.store.state.get("work_units", {}).get("units", {})
        if any("policy_b_shadow" in att for unit in units.values() for att in unit.get("attempts", [])):
            raise Stop("HUMAN_ACTION_REQUIRED", "INVOCATION_REPLAY_REFUSED",
                       "Shadow evidence cannot authorize automatic execution or verification replay")
        for child in self.store.state.get("supervision", {}).get("children", {}).values():
            inv = child.get("invocation")
            if inv is not None:
                unit = units.get(inv["unit_id"], {})
                if inv["phase"] != "COMPLETE" or unit.get("current_attempt") == inv["attempt_id"]:
                    raise Stop("HUMAN_ACTION_REQUIRED", "INVOCATION_REPLAY_REFUSED",
                               "Recorded invocation cannot authorize automatic verifier or executor relaunch")

    def _run_guarded_verification(
        self,
        unit: dict,
        section: dict,
        attempt_id: str,
        attempt_elapsed_so_far: float = 0.0,
        changed_paths: list[str] | None = None,
    ) -> dict:
        """Unified controller-owned guarded verification path for normal execution and recovery (Fix D, Fix G).

        Enforces:
        - Point 3 & Point 4 & Point 5 supervision checks
        - Cumulative unit and task budget enforcement (Fix A)
        - Subprocess timeout bounded by min(configured, unit_rem, task_rem)
        - Pre/post snapshot consistency (candidate contamination handling)
        - Atomic sealed evidence recording
        """
        if self._observation_context() is not None:
            return self._run_policy_b_shadow_verification(unit, section, attempt_id, attempt_elapsed_so_far)
        self._refuse_invocation_replay()
        uid = unit["unit_id"]
        frozen_spec = {k: copy.deepcopy(unit["spec"][k]) for k in FROZEN_KEYS if k in unit["spec"]}

        # Fix 5 & D: Point 3 - before verifier execution
        self.guard_supervision()
        self.check_task_budget()

        # Fix A: Budget check before running verifiers
        unit_rem = self.remaining_unit_budget(unit, current_attempt_elapsed=attempt_elapsed_so_far, current_attempt_id=attempt_id)
        task_rem = self.remaining_task_budget()
        if unit_rem is not None and unit_rem <= 0:
            self._persist_runtime()
            raise Stop("BUDGET_EXHAUSTED", "unit_timeout", f"Unit {uid} budget exhausted before verification")
        if task_rem <= 0:
            self._persist_runtime()
            raise Stop("BUDGET_EXHAUSTED", "max_total_runtime", "Task budget exhausted before verification")

        if unit["status"] != "VERIFYING":
            work_units.transition_unit(unit, "VERIFYING")

        # Record controller-owned verification-stage timing BEFORE verifier execution (Fix B)
        now_clock = self.clock()
        now_runtime = round(self.runtime(), 3)
        stage_timing = {
            "stage": "VERIFYING",
            "unit_id": uid,
            "attempt_id": attempt_id,
            "stage_start_clock": now_clock,
            "stage_start_runtime": now_runtime,
            "accounted_runtime": now_runtime,
            "attempt_elapsed_at_start": attempt_elapsed_so_far,
        }
        att = next((a for a in unit.get("attempts", []) if a.get("attempt_id") == attempt_id), None)
        if att is not None:
            att["stage_timing"] = copy.deepcopy(stage_timing)
        self._persist_unit(section, unit)

        mgr = copy.deepcopy(self.store.state.get("manager"))
        if mgr is not None:
            mgr["active_stage_timing"] = copy.deepcopy(stage_timing)
            mgr["runtime_seconds"] = now_runtime
            self.store.commit(manager=mgr)

        verifier_results = {}
        verifiers_passed = True
        fail_class = None
        v_before = snapshot(self.repo)

        for vid in frozen_spec["verifier_ids"]:
            self.check_task_budget()
            v_unit_rem = self.remaining_unit_budget(unit, current_attempt_elapsed=attempt_elapsed_so_far, current_attempt_id=attempt_id)
            v_task_rem = self.remaining_task_budget()
            defn = self.verifier_registry.get(vid)
            if not defn:
                verifiers_passed = False
                fail_class = "unknown_verifier"
                verifier_results[vid] = {"passed": False, "exit_code": 1, "error": f"Unknown verifier {vid}"}
                break

            timeout_candidates = [defn.get("timeout_seconds", 30.0), v_task_rem]
            if v_unit_rem is not None:
                timeout_candidates.append(v_unit_rem)
            v_timeout = min(timeout_candidates)
            if v_timeout <= 0:
                self._persist_runtime()
                raise Stop("BUDGET_EXHAUSTED", "verifier_timeout", f"No remaining budget for verifier {vid}")

            v_start = self.clock()
            res = self.repo.execute(defn["argv"], timeout=v_timeout)
            v_elapsed = self.clock() - v_start
            attempt_elapsed_so_far += v_elapsed
            self._persist_runtime()
            self.check_task_budget()

            verifier_results[vid] = {
                "argv": defn["argv"],
                "exit_code": res["exit_code"],
                "passed": res["exit_code"] == 0,
                "stdout": res.get("stdout", ""),
                "stderr": res.get("stderr", ""),
            }
            if res["exit_code"] != 0:
                verifiers_passed = False

        v_after = snapshot(self.repo)
        if v_before != v_after:
            verifiers_passed = False
            fail_class = "verifier_tampered_repo"

        # Clear active stage timing on completion of verification stage (Fix B)
        mgr = copy.deepcopy(self.store.state.get("manager"))
        if mgr is not None and "active_stage_timing" in mgr:
            del mgr["active_stage_timing"]
            mgr["runtime_seconds"] = round(self.runtime(), 3)
            self.store.commit(manager=mgr)
        if att is not None and "stage_timing" in att:
            del att["stage_timing"]
            self._persist_unit(section, unit)

        # Fix 5 & D: Point 4 - after verifier execution
        self.guard_supervision()

        timeout_info = {
            "elapsed_seconds": round(attempt_elapsed_so_far, 3),
        }

        att = next((a for a in unit.get("attempts", []) if a.get("attempt_id") == attempt_id), None)

        if not verifiers_passed:
            fail_class = fail_class or "verification_failed"
            if att is not None and att.get("completed_at") is None:
                work_units.complete_attempt(
                    unit,
                    attempt_id,
                    outcome="failed",
                    failure_classification=fail_class,
                    changed_paths=changed_paths or [],
                    verifier_result=verifier_results,
                    timeout_info=timeout_info,
                )
            max_att = min(frozen_spec["limits"].get("max_attempts", MAX_UNIT_ATTEMPTS), MAX_UNIT_ATTEMPTS)
            if len(unit["attempts"]) >= max_att:
                work_units.transition_unit(unit, "UNIT_FAILED")
            else:
                work_units.transition_unit(unit, "REPAIR_PENDING")
                work_units.transition_unit(unit, "READY")
            self._persist_unit(section, unit)
            return {
                "success": False,
                "outcome": "failed",
                "failure_classification": fail_class,
                "attempt_id": attempt_id,
                "changed_paths": changed_paths,
                "verifier_result": verifier_results,
                "error": "Verifier failed",
                "scope_violation": False,
            }

        # Fix A: Recompute remaining budget before UNIT_VERIFIED acceptance
        final_unit_rem = self.remaining_unit_budget(unit, current_attempt_elapsed=attempt_elapsed_so_far, current_attempt_id=attempt_id)
        final_task_rem = self.remaining_task_budget()
        unit_limit_timeout = frozen_spec["limits"].get("timeout_seconds")
        if unit_limit_timeout is not None and (final_unit_rem is not None and final_unit_rem <= 0):
            self._persist_runtime()
            raise Stop("BUDGET_EXHAUSTED", "unit_timeout", f"Unit {uid} budget exhausted before verification acceptance")
        if final_task_rem <= 0:
            self._persist_runtime()
            raise Stop("BUDGET_EXHAUSTED", "max_total_runtime", "Task budget exhausted before verification acceptance")

        # Fix 5 & D: Point 5 - immediately before UNIT_VERIFIED acceptance
        self.guard_supervision()

        # Deterministic verifiers all passed!
        evidence = {
            "passed": True,
            "verifiers": verifier_results,
        }
        if att is not None and att.get("completed_at") is None:
            work_units.complete_attempt(
                unit,
                attempt_id,
                outcome="passed",
                changed_paths=changed_paths or [],
                verifier_result=verifier_results,
                timeout_info=timeout_info,
            )
        work_units.set_unit_result(
            unit,
            verifier_evidence=evidence,
            candidate_id=v_after["head"],
            candidate_snapshot=v_after,
            attempt_id=attempt_id,
        )
        work_units.transition_unit(unit, "UNIT_VERIFIED")
        self._persist_unit(section, unit)

        return {
            "success": True,
            "outcome": "passed",
            "attempt_id": attempt_id,
            "changed_paths": changed_paths,
            "verifier_result": verifier_results,
        }

    # ----------------------------------------------------------------------- reconciliation (Fix 7, Fix B, C, D, G)

    def reconcile_on_resume(self):
        """Reconcile uncompleted attempts or crash states upon initialization (Fix B, C, D, G)."""
        self._refuse_invocation_replay()
        if "work_units" not in self.store.state:
            return
        section = self.get_section()
        validate_sequence_integrity(section)
        modified = False

        manager = copy.deepcopy(self.store.state.get("manager", {}))
        active_timing = manager.get("active_stage_timing")

        # Check for interrupted REVALIDATING stage (Fix B)
        if manager.get("milestone_status") == "REVALIDATING":
            if active_timing and active_timing.get("stage") == "REVALIDATING":
                start_clock = active_timing.get("stage_start_clock", self.started_at)
                start_runtime = active_timing.get("stage_start_runtime", self.base_runtime)
                accounted = active_timing.get("accounted_runtime", self.base_runtime)
                elapsed_in_stage = max(0.0, self.clock() - start_clock)
                total_observed = start_runtime + elapsed_in_stage
                already_accounted = max(self.base_runtime, accounted)
                unaccounted = max(0.0, total_observed - already_accounted)
                self.base_runtime = already_accounted + unaccounted
                self.started_at = self.clock()
                # Persist before budget decision
                manager["runtime_seconds"] = round(self.base_runtime, 3)
                if "active_stage_timing" in manager:
                    manager["active_stage_timing"]["accounted_runtime"] = round(self.base_runtime, 3)
                self.store.commit(manager=manager)

                task_rem = self.remaining_task_budget()
                if task_rem <= 0:
                    manager["milestone_status"] = MILESTONE_FAILED
                    if "active_stage_timing" in manager:
                        del manager["active_stage_timing"]
                    self.store.commit(manager=manager)
                    raise Stop("BUDGET_EXHAUSTED", "revalidation_budget", "Task budget exhausted during revalidation recovery")

        for uid, unit in section["units"].items():
            frozen_spec = {k: copy.deepcopy(unit["spec"][k]) for k in FROZEN_KEYS if k in unit["spec"]}
            active = [a for a in unit.get("attempts", []) if a.get("completed_at") is None]

            # Case 1: Unit was in VERIFYING when crash occurred (Fix D, Fix G, Fix B)
            if unit["status"] == "VERIFYING":
                att_id = unit.get("current_attempt") or (active[0]["attempt_id"] if active else unit["attempts"][-1]["attempt_id"])
                att = next((a for a in unit["attempts"] if a["attempt_id"] == att_id), unit["attempts"][-1])

                # Check pre-attempt snapshot integrity (Fix C & Wave 4 Scope)
                before = att.get("pre_attempt_snapshot")
                before_fs = att.get("pre_attempt_fs_snapshot")
                if before is not None or before_fs is not None:
                    scope_violation = False
                    changed = []
                    allowed = frozen_spec["scope"]["allowed_paths"] if frozen_spec["mode"] != "read_only" else []
                    forbidden = (list(frozen_spec["scope"].get("forbidden_paths", [])) + list(self.parent_scope.get("forbidden_paths", []))) if frozen_spec["mode"] != "read_only" else []
                    # Check filesystem mutations FIRST, before git
                    if before_fs is not None:
                        curr_fs = capture_fs_snapshot(self.repo.root)
                        exempt = self._controller_exempt_files()
                        fs_changed = detect_fs_mutations(before_fs, curr_fs, exempt_files=exempt)
                        changed = list(fs_changed)
                        if frozen_spec["mode"] == "read_only":
                            if len(changed) > 0:
                                scope_violation = True
                        else:
                            for p in changed:
                                p_norm = p.lower().replace("\\", "/")
                                if p_norm == ".git" or p_norm.startswith(".git/"):
                                    scope_violation = True
                                    break
                                if not any(work_units.covers(a, p) for a in allowed) or any(work_units.covers(f, p) for f in forbidden):
                                    scope_violation = True
                                    break
                    if not scope_violation and before is not None:
                        try:
                            curr_snap = snapshot(self.repo)
                            git_changed = changed_paths(before, curr_snap)
                            changed = sorted(set(changed) | set(git_changed))
                            if frozen_spec["mode"] == "read_only":
                                if len(changed) > 0:
                                    scope_violation = True
                            else:
                                for p in git_changed:
                                    p_norm = p.lower().replace("\\", "/")
                                    if p_norm == ".git" or p_norm.startswith(".git/"):
                                        scope_violation = True
                                        break
                                    if not any(work_units.covers(a, p) for a in allowed) or any(work_units.covers(f, p) for f in forbidden):
                                        scope_violation = True
                                        break
                        except Exception:
                            scope_violation = True
                    if scope_violation:
                        timeout_info = att.get("timeout_info") or {"elapsed_seconds": 0.0}
                        work_units.complete_attempt(
                            unit,
                            att_id,
                            outcome="failed",
                            failure_classification="scope_violation",
                            changed_paths=changed,
                            timeout_info=timeout_info,
                        )
                        work_units.transition_unit(unit, "UNIT_FAILED")
                        work_units.seal_unit_state(unit)
                        modified = True
                        continue

                # Conservative elapsed time accounting for interrupted verifier (Fix B)
                stage_timing = (
                    active_timing
                    if (active_timing and active_timing.get("stage") == "VERIFYING" and active_timing.get("attempt_id") == att_id)
                    else att.get("stage_timing")
                )

                if stage_timing:
                    start_clock = stage_timing.get("stage_start_clock", att.get("started_at_clock", self.started_at))
                    start_runtime = stage_timing.get("stage_start_runtime", self.base_runtime)
                    accounted = stage_timing.get("accounted_runtime", self.base_runtime)
                    elapsed_in_stage = max(0.0, self.clock() - start_clock)
                    total_observed = start_runtime + elapsed_in_stage
                    already_accounted = max(self.base_runtime, accounted)
                    unaccounted = max(0.0, total_observed - already_accounted)
                    att_start_elapsed = stage_timing.get(
                        "attempt_elapsed_at_start",
                        (att.get("timeout_info") or {}).get("elapsed_seconds", 0.0)
                    )
                    elapsed_so_far = att_start_elapsed + elapsed_in_stage
                else:
                    clock_spent = max(0.0, self.clock() - att.get("started_at_clock", self.clock()))
                    prior_elapsed = (att.get("timeout_info") or {}).get("elapsed_seconds", 0.0)
                    elapsed_so_far = max(prior_elapsed, clock_spent)
                    start_rt = att.get("started_at_runtime", 0.0)
                    total_observed = start_rt + elapsed_so_far
                    already_accounted = self.base_runtime
                    unaccounted = max(0.0, total_observed - already_accounted)

                # Step 2: Add ONLY the previously unaccounted portion to enclosing runtime
                self.base_runtime = already_accounted + unaccounted
                self.started_at = self.clock()

                # Step 3: Persist it BEFORE any recovery budget decision
                mgr = copy.deepcopy(self.store.state.get("manager", {}))
                mgr["runtime_seconds"] = round(self.base_runtime, 3)
                if "active_stage_timing" in mgr and isinstance(mgr["active_stage_timing"], dict):
                    mgr["active_stage_timing"]["accounted_runtime"] = round(self.base_runtime, 3)
                self.store.commit(manager=mgr)

                # Step 5: Recompute remaining enclosing budget and unit budget
                task_rem = self.remaining_task_budget()
                unit_rem = self.remaining_unit_budget(unit, current_attempt_elapsed=elapsed_so_far, current_attempt_id=att_id)

                # Step 6: If exhausted, fail with BUDGET_EXHAUSTED and DO NOT resume verifier
                if (task_rem <= 0) or (unit_rem is not None and unit_rem <= 0):
                    timeout_info = {
                        "elapsed_seconds": round(elapsed_so_far, 3),
                        "interrupted": True,
                    }
                    reason = "max_total_runtime" if task_rem <= 0 else "unit_timeout"
                    work_units.complete_attempt(
                        unit,
                        att_id,
                        outcome="timeout",
                        failure_classification="verifier_timeout",
                        changed_paths=att.get("changed_paths") or [],
                        timeout_info=timeout_info,
                    )
                    work_units.transition_unit(unit, "UNIT_FAILED")
                    work_units.seal_unit_state(unit)
                    work_units._reseal_section(section)
                    self._persist(section)

                    mgr = copy.deepcopy(self.store.state.get("manager", {}))
                    mgr["milestone_status"] = MILESTONE_FAILED
                    mgr["runtime_seconds"] = round(self.base_runtime, 3)
                    if "active_stage_timing" in mgr:
                        del mgr["active_stage_timing"]
                    self.store.commit(manager=mgr)

                    raise Stop("BUDGET_EXHAUSTED", reason, f"Budget exhausted during verifier recovery ({reason})")

                # Step 7: Only if budget remains may guarded verifier recovery continue
                self._run_guarded_verification(
                    unit,
                    section,
                    attempt_id=att_id,
                    attempt_elapsed_so_far=elapsed_so_far,
                    changed_paths=att.get("changed_paths"),
                )
                modified = True

            # Case 2: Unfinished attempt in EXECUTING (or other status)
            elif active or unit.get("current_attempt") is not None:
                att_id = unit.get("current_attempt") or active[0]["attempt_id"]
                att = next((a for a in unit["attempts"] if a["attempt_id"] == att_id), unit["attempts"][-1])

                # Conservative elapsed time accounting (Fix B)
                clock_spent = max(0.0, self.clock() - att.get("started_at_clock", self.clock()))
                prior_elapsed = (att.get("timeout_info") or {}).get("elapsed_seconds", 0.0)
                elapsed = max(prior_elapsed, clock_spent)
                timeout_info = {"elapsed_seconds": round(elapsed, 3), "interrupted": True}

                # Retain consumed execution runtime in manager runtime without double-counting (Fix B)
                if elapsed > 0:
                    start_rt = att.get("started_at_runtime", 0.0)
                    total_observed = start_rt + elapsed
                    already_accounted = self.base_runtime
                    unaccounted = max(0.0, total_observed - already_accounted)
                    self.base_runtime += unaccounted
                    self.started_at = self.clock()
                    self._persist_runtime()

                # Check pre-attempt snapshot for scope / read-only violations during interrupted attempt (Fix C & Wave 4 Scope)
                before = att.get("pre_attempt_snapshot")
                before_fs = att.get("pre_attempt_fs_snapshot")
                scope_violation = False
                changed = []
                allowed = frozen_spec["scope"]["allowed_paths"] if frozen_spec["mode"] != "read_only" else []
                forbidden = (list(frozen_spec["scope"].get("forbidden_paths", [])) + list(self.parent_scope.get("forbidden_paths", []))) if frozen_spec["mode"] != "read_only" else []
                if before is not None or before_fs is not None:
                    # Check filesystem mutations FIRST, before git
                    if before_fs is not None:
                        curr_fs = capture_fs_snapshot(self.repo.root)
                        exempt = self._controller_exempt_files()
                        fs_changed = detect_fs_mutations(before_fs, curr_fs, exempt_files=exempt)
                        changed = list(fs_changed)
                        if frozen_spec["mode"] == "read_only":
                            if len(changed) > 0:
                                scope_violation = True
                        else:
                            for p in changed:
                                p_norm = p.lower().replace("\\", "/")
                                if p_norm == ".git" or p_norm.startswith(".git/"):
                                    scope_violation = True
                                    break
                                if not any(work_units.covers(a, p) for a in allowed) or any(work_units.covers(f, p) for f in forbidden):
                                    scope_violation = True
                                    break
                    if not scope_violation and before is not None:
                        try:
                            curr_snap = snapshot(self.repo)
                            git_changed = changed_paths(before, curr_snap)
                            changed = sorted(set(changed) | set(git_changed))
                            if frozen_spec["mode"] == "read_only":
                                if len(changed) > 0:
                                    scope_violation = True
                            else:
                                for p in git_changed:
                                    p_norm = p.lower().replace("\\", "/")
                                    if p_norm == ".git" or p_norm.startswith(".git/"):
                                        scope_violation = True
                                        break
                                    if not any(work_units.covers(a, p) for a in allowed) or any(work_units.covers(f, p) for f in forbidden):
                                        scope_violation = True
                                        break
                        except Exception:
                            scope_violation = True

                if scope_violation:
                    work_units.complete_attempt(
                        unit,
                        att_id,
                        outcome="failed",
                        failure_classification="scope_violation",
                        changed_paths=changed,
                        timeout_info=timeout_info,
                    )
                    work_units.transition_unit(unit, "UNIT_FAILED")
                    work_units.seal_unit_state(unit)
                    modified = True
                    continue

                work_units.complete_attempt(
                    unit,
                    att_id,
                    outcome="failed",
                    failure_classification="crash_interrupted",
                    changed_paths=changed,
                    timeout_info=timeout_info,
                )
                max_att = min(frozen_spec["limits"].get("max_attempts", MAX_UNIT_ATTEMPTS), MAX_UNIT_ATTEMPTS)
                if unit["status"] == "EXECUTING":
                    if len(unit["attempts"]) < max_att:
                        work_units.transition_unit(unit, "REPAIR_PENDING")
                        work_units.transition_unit(unit, "READY")
                    else:
                        work_units.transition_unit(unit, "UNIT_FAILED")
                work_units.seal_unit_state(unit)
                modified = True

        if modified:
            work_units._reseal_section(section)
            self._persist(section)

    # ----------------------------------------------------------------------- scheduling (Fix 6, Fix 8, Fix E)

    def evaluate_readiness(self, section: dict) -> list[dict]:
        """Evaluate dependencies and update eligible units to READY.

        Fix 8: If ANY unit in the milestone is UNIT_FAILED, halt immediately. No new units become READY.
        Fix 6: Order ready units according to explicit persisted sequence.
        Fix E: Validate sequence integrity.
        """
        self._refuse_invocation_replay()
        validate_sequence_integrity(section)

        # Fix 8: Any terminal unit failure stops all scheduling
        if any(u["status"] == "UNIT_FAILED" for u in section["units"].values()):
            return []

        ready_units = []
        seq = section["sequence"]

        for uid, unit in section["units"].items():
            status = unit["status"]
            if status in ("UNIT_VERIFIED", "UNIT_FAILED"):
                continue

            if status in ("READY", "EXECUTING", "VERIFYING"):
                ready_units.append(unit)
                continue

            if status == "REPAIR_PENDING":
                work_units.transition_unit(unit, "READY")
                self._persist_unit(section, unit)
                ready_units.append(unit)
                continue

            if status in ("PROPOSED", "VALIDATED", "PENDING"):
                if status == "PROPOSED":
                    work_units.transition_unit(unit, "VALIDATED")

                # Dependency check
                all_deps_verified = True
                for dep in unit["spec"]["dependencies"]:
                    if dep not in section["units"]:
                        raise WorkUnitSchedulingError(f"Unit {uid!r} depends on unknown unit {dep!r}")
                    if section["units"][dep]["status"] != "UNIT_VERIFIED":
                        all_deps_verified = False
                        break

                if all_deps_verified:
                    work_units.validate_scope_containment(unit["spec"]["scope"], self.parent_scope)
                    for vid in unit["spec"]["verifier_ids"]:
                        work_units.validate_verifier_reference(vid, self.verifier_registry)

                    work_units.transition_unit(unit, "READY")
                    self._persist_unit(section, unit)
                    ready_units.append(unit)
                else:
                    if unit["status"] == "VALIDATED":
                        work_units.transition_unit(unit, "PENDING")
                        self._persist_unit(section, unit)

        # Fix 6: Deterministic order strictly following explicit sequence
        def unit_order(u):
            u_id = u["unit_id"]
            try:
                seq_idx = seq.index(u_id)
            except ValueError:
                seq_idx = 999999
            return (seq_idx, u.get("ordinal", 0), u_id)

        ready_units.sort(key=unit_order)
        return ready_units

    # ----------------------------------------------------------------------- execution (Fix 1, 2, 3, 5, A, B, C, D)

    def execute_attempt(self, unit: dict, section: dict, attempt_num: int, repair_evidence: dict | None = None) -> dict:
        """Execute a single attempt for a unit with immutable contract, unconditional scope check,
        cumulative shared budget accounting, and mandatory supervision boundaries.
        """
        self._refuse_invocation_replay()
        uid = unit["unit_id"]
        if self._repair_controller is not None:
            if attempt_num != 1 or uid != self._repair_controller.mode["unit_id"] or unit["attempts"]:
                raise WorkUnitSchedulingError("Active recovery permits only its unattempted Attempt 1")

        # Fix 1: Freeze controller-owned contract
        frozen_spec = {k: copy.deepcopy(unit["spec"][k]) for k in FROZEN_KEYS if k in unit["spec"]}

        # Fix 10: Fail closed before attempt creation if executor is absent or incompatible
        if self.agents is None or not hasattr(self.agents, "executor") or not callable(getattr(self.agents.executor, "execute", None)):
            raise WorkUnitSchedulingError(
                "WorkUnit execution requires an explicitly supplied WorkUnit-compatible executor with callable execute()"
            )

        max_attempts = min(frozen_spec["limits"].get("max_attempts", MAX_UNIT_ATTEMPTS), MAX_UNIT_ATTEMPTS)
        if len(unit["attempts"]) >= max_attempts:
            raise WorkUnitSchedulingError(
                f"Attempt budget exhausted for unit {uid!r} ({len(unit['attempts'])} >= {max_attempts})"
            )

        # Fix 3: Budget check before attempt
        now = self.clock()
        self.check_task_budget(now=now)
        unit_rem = self.remaining_unit_budget(unit)
        task_rem = self.remaining_task_budget(now=now)
        if unit_rem is not None and unit_rem <= 0:
            self._persist_runtime()
            raise Stop("BUDGET_EXHAUSTED", "unit_timeout", f"Unit {uid} budget exhausted")
        if task_rem <= 0:
            self._persist_runtime()
            raise Stop("BUDGET_EXHAUSTED", "max_total_runtime", "Task budget exhausted")

        # Fix 5: Point 1 - before executor attempt
        self.guard_supervision()

        if unit["status"] == "PROPOSED":
            work_units.transition_unit(unit, "VALIDATED")
        if unit["status"] in ("VALIDATED", "PENDING"):
            work_units.transition_unit(unit, "READY")
        if unit["status"] == "READY":
            work_units.transition_unit(unit, "EXECUTING")

        # Fix 7: Reconstruct repair evidence from previous attempt if not provided
        if attempt_num > 1 and repair_evidence is None and unit.get("attempts"):
            last_att = unit["attempts"][-1]
            repair_evidence = {
                "attempt": attempt_num - 1,
                "outcome": last_att.get("outcome"),
                "failure_classification": last_att.get("failure_classification"),
                "verifier_result": last_att.get("verifier_result"),
                "changed_paths": last_att.get("changed_paths", []),
                "error": last_att.get("execution_result"),
            }

        attempt_id = f"{uid}-attempt-{attempt_num}"
        work_units.create_attempt(unit, attempt_id)

        # Context provided to executor receives detached copies only
        context = {
            "unit_id": uid,
            "objective": frozen_spec["objective"],
            "dependencies": frozen_spec["dependencies"],
            "mode": frozen_spec["mode"],
            "scope": copy.deepcopy(frozen_spec["scope"]),
            "attempt_number": attempt_num,
            "repair_evidence": copy.deepcopy(repair_evidence),
        }

        before = self._shadow_snapshot() if self._observation_context() is not None else snapshot(self.repo)
        fs_before = capture_fs_snapshot(self.repo.root)

        # Set scoped repository confinement if applicable
        if hasattr(self.repo, "scope"):
            self.repo.scope = {
                "allowed_paths": frozen_spec["scope"]["allowed_paths"],
                "forbidden_paths": frozen_spec["scope"]["forbidden_paths"],
            }
            self.repo.run_forbidden = self.parent_scope.get("forbidden_paths", [])

        # Fix 3: stage timeout bounded by unit budget and task budget
        unit_limit_timeout = frozen_spec["limits"].get("timeout_seconds")
        timeout_candidates = [task_rem]
        if unit_rem is not None:
            timeout_candidates.append(unit_rem)
        stage_timeout = min(timeout_candidates)

        # Pre-persist timing and candidate binding BEFORE executor execution (Fix B, Fix C, Wave 4)
        attempt_record = unit["attempts"][-1]
        attempt_record["pre_attempt_snapshot"] = copy.deepcopy(before)
        attempt_record["pre_attempt_fs_snapshot"] = copy.deepcopy(fs_before)
        attempt_record["pre_attempt_fingerprint"] = before["head"]
        attempt_record["started_at_clock"] = self.clock()
        attempt_record["started_at_runtime"] = round(self.runtime(), 3)
        attempt_record["stage_timeout"] = round(stage_timeout, 3)
        observation_context = self._observation_context()
        if observation_context is not None:
            # Same first-attempt WAL transaction as the actual pre-executor capture.
            # Recovery must refuse even a crash before the first native intent.
            attempt_record[self._observation_key()] = {
                "policy": "B", "mode": "ACTIVE" if self._repair_controller is not None else "SHADOW",
                "phase": "STARTED", "observations": {},
                "registry_sha256": durable.digest(observation_context["registry"]),
            }
            attempt_record["started_at_clock"] = observation_context["started_clock"]
            stage_timeout = min(stage_timeout, self.remaining_unit_budget(
                unit, self.clock() - attempt_record["started_at_clock"], attempt_id))
            if stage_timeout <= 0:
                raise Stop("BUDGET_EXHAUSTED", "unit_timeout", "Shadow budget exhausted before executor")
            attempt_record["stage_timeout"] = stage_timeout
        self._persist_unit(section, unit)

        executor_error = None
        outcome = "interrupted"
        fail_class = None
        start_time = self.clock()
        attempt_elapsed = 0.0

        # Detached copy of spec passed to executor (Fix 1)
        executor_spec = copy.deepcopy(frozen_spec)
        executor_context = copy.deepcopy(context)

        try:
            executor = self.agents.executor
            if hasattr(executor, "set_deadline") and stage_timeout:
                executor.set_deadline(self.clock() + stage_timeout)

            executor.execute(executor_spec, executor_context, self.repo)
            attempt_elapsed = self.clock() - start_time
            if stage_timeout and attempt_elapsed > stage_timeout:
                raise TimeoutError(f"Executor exceeded stage timeout of {stage_timeout:.3f}s")
        except TimeoutError as exc:
            outcome = "timeout"
            fail_class = "executor_timeout"
            executor_error = exc
        except Exception as exc:
            from manager import ExecutorError
            if isinstance(exc, ExecutorError) and exc.reason == "EXECUTOR_TIMEOUT":
                outcome = "timeout"
                fail_class = "executor_timeout"
            elif isinstance(exc, ExecutorError) and exc.reason in ("CLEANUP_FAILED", "CONTAINMENT_FAILED"):
                outcome = "failed"
                fail_class = exc.reason.lower()
            else:
                outcome = "failed"
                fail_class = "executor_error"
            executor_error = exc
        finally:
            attempt_elapsed = self.clock() - start_time
            if hasattr(self.repo, "scope"):
                self.repo.scope = None
                self.repo.run_forbidden = []

        # Fix 1: Check for executor contract mutation / divergence
        if executor_spec != frozen_spec:
            outcome = "failed"
            fail_class = "contract_divergence"
            executor_error = WorkUnitSchedulingError(
                "Contract divergence: executor attempted to mutate immutable execution contract"
            )

        # Wave 4 Repair: Step 1 - Capture filesystem snapshot immediately after worker exit, before Git operations
        fs_after = capture_fs_snapshot(self.repo.root)
        exempt = self._controller_exempt_files()
        fs_changed = detect_fs_mutations(fs_before, fs_after, exempt_files=exempt)
        is_scope_violation = False
        scope_violations = []

        if frozen_spec["mode"] == "read_only":
            if len(fs_changed) > 0:
                is_scope_violation = True
                outcome = "failed"
                fail_class = "unexpected_write"
                executor_error = WorkUnitSchedulingError(f"Unexpected write from read-only unit: {fs_changed}")
        else:  # mutation mode
            allowed = frozen_spec["scope"]["allowed_paths"]
            forbidden = list(frozen_spec["scope"].get("forbidden_paths", [])) + list(self.parent_scope.get("forbidden_paths", []))
            for p in fs_changed:
                p_norm = p.lower().replace("\\", "/")
                # Protect .git/** unconditionally as protected repository metadata
                if p_norm == ".git" or p_norm.startswith(".git/"):
                    scope_violations.append(p)
                elif not any(work_units.covers(a, p) for a in allowed) or any(work_units.covers(f, p) for f in forbidden):
                    scope_violations.append(p)

            if scope_violations:
                is_scope_violation = True
                outcome = "failed"
                fail_class = "scope_violation"
                executor_error = WorkUnitSchedulingError(f"Scope violation: {scope_violations}")

        changed = list(fs_changed)

        # Step 2: ONLY IF NO filesystem scope violation was detected, perform Git operations!
        # Corrupt .git/index must deterministically fail as scope_violation, NOT raise DurableError.
        if not is_scope_violation and executor_error is None:
            try:
                after = self._shadow_snapshot() if self._observation_context() is not None else snapshot(self.repo)
                if self._observation_context() is not None:
                    attempt_record[self._observation_key()]["candidate_snapshot"] = copy.deepcopy(after)
                    attempt_record[self._observation_key()]["candidate_fs_snapshot"] = self._shadow_fs_snapshot()
                git_changed = changed_paths(before, after)
                changed = sorted(set(git_changed) | set(fs_changed))
                if frozen_spec["mode"] == "read_only":
                    if len(changed) > 0:
                        is_scope_violation = True
                        outcome = "failed"
                        fail_class = "unexpected_write"
                        executor_error = WorkUnitSchedulingError(f"Unexpected write from read-only unit: {changed}")
                else:
                    git_violations = []
                    for p in git_changed:
                        p_norm = p.lower().replace("\\", "/")
                        if p_norm == ".git" or p_norm.startswith(".git/"):
                            git_violations.append(p)
                        elif not any(work_units.covers(a, p) for a in allowed) or any(work_units.covers(f, p) for f in forbidden):
                            git_violations.append(p)
                    if git_violations:
                        is_scope_violation = True
                        outcome = "failed"
                        fail_class = "scope_violation"
                        executor_error = WorkUnitSchedulingError(f"Scope violation: {git_violations}")
            except Exception as exc:
                outcome = "failed"
                fail_class = "git_error"
                executor_error = exc

        # Record elapsed time on attempt (Fix 3, Fix A)
        timeout_info = {
            "elapsed_seconds": round(attempt_elapsed, 3),
            "stage_timeout": round(stage_timeout, 3),
        }

        # Handle failure (including scope violation or exception)
        if executor_error is not None:
            work_units.complete_attempt(
                unit,
                attempt_id,
                outcome=outcome,
                failure_classification=fail_class,
                changed_paths=changed,
                timeout_info=timeout_info,
            )
            self._persist_unit(section, unit)
            # Fix 3 & 5: Point 2 - immediately after executor return/raise
            self.guard_supervision()
            self.check_task_budget()
            return {
                "success": False,
                "outcome": outcome,
                "failure_classification": fail_class,
                "attempt_id": attempt_id,
                "changed_paths": changed,
                "error": str(executor_error),
                "scope_violation": is_scope_violation,
            }

        # Fix 3 & 5: Point 2 - immediately after executor return/raise
        self.guard_supervision()
        self.check_task_budget()

        # Update attempt elapsed time and changed paths before guarded verification (Fix A, D, B)
        attempt_record["changed_paths"] = changed
        attempt_record["timeout_info"] = timeout_info
        self._persist_unit(section, unit)
        self._persist_runtime()

        # Enter unified guarded verification path (Fix D, Fix G)
        return self._run_guarded_verification(
            unit,
            section,
            attempt_id=attempt_id,
            attempt_elapsed_so_far=attempt_elapsed,
            changed_paths=changed,
        )

    def execute_unit(self, unit: dict, section: dict) -> dict:
        """Execute a unit through initial attempt and bounded repair if needed.

        Bounded repair policy:
        - max 2 attempts total (1 initial + 1 repair)
        - repair receives failure evidence from attempt 1 (Fix 7)
        - repair cannot expand scope
        - scope violation fails closed immediately (no repair) (Fix 2)
        - third attempt is impossible
        """
        uid = unit["unit_id"]
        attempt_num = len(unit["attempts"]) + 1

        # Attempt 1 (initial)
        res = self.execute_attempt(unit, section, attempt_num=attempt_num)
        if res["success"]:
            return res

        # Fix 2 & Wave 4 Repair: Scope violation or containment/cleanup failure fails closed immediately: terminal failure, candidate contaminated, no repair
        if res.get("scope_violation") or res.get("failure_classification") in ("scope_violation", "cleanup_failed", "containment_failed"):
            if unit["status"] != "UNIT_FAILED":
                work_units.transition_unit(unit, "UNIT_FAILED")
                self._persist_unit(section, unit)
            return res

        # Check repair allowance
        max_attempts = min(unit["spec"]["limits"].get("max_attempts", MAX_UNIT_ATTEMPTS), MAX_UNIT_ATTEMPTS)
        if len(unit["attempts"]) >= max_attempts or unit["status"] == "UNIT_FAILED":
            if unit["status"] != "UNIT_FAILED":
                work_units.transition_unit(unit, "UNIT_FAILED")
                self._persist_unit(section, unit)
            return res

        # Transition to REPAIR_PENDING, then READY for repair
        if unit["status"] != "READY":
            if unit["status"] != "REPAIR_PENDING":
                work_units.transition_unit(unit, "REPAIR_PENDING")
                self._persist_unit(section, unit)
            work_units.transition_unit(unit, "READY")
            self._persist_unit(section, unit)

        # Fix 7: Reconstruct repair evidence from attempt 1
        repair_evidence = {
            "attempt": 1,
            "outcome": res["outcome"],
            "failure_classification": res["failure_classification"],
            "verifier_result": res.get("verifier_result"),
            "changed_paths": res.get("changed_paths", []),
            "error": res.get("error"),
        }

        repair_res = self.execute_attempt(unit, section, attempt_num=2, repair_evidence=repair_evidence)
        if repair_res["success"]:
            return repair_res

        # Repair failed; attempt allowance exhausted (2 >= 2) -> UNIT_FAILED
        if unit["status"] != "UNIT_FAILED":
            work_units.transition_unit(unit, "UNIT_FAILED")
            self._persist_unit(section, unit)
        return repair_res

    # ----------------------------------------------------------------------- revalidation (Fix 3, Fix 4, Fix 5, Fix E)

    def revalidate_milestone(self, section: dict) -> tuple[bool, str | None]:
        """Conservative safe revalidation of all units against current candidate state.

        Before treating the sequence as milestone-ready, revalidate all earlier
        unit verifiers against the candidate repository state. Stale verification evidence
        is rejected.
        """
        validate_sequence_integrity(section)

        # Fix 5: Point 6a - before conservative revalidation
        self.guard_supervision()
        self.check_task_budget()

        v_before = snapshot(self.repo)

        seq = section["sequence"]

        for uid in seq:
            unit = section["units"].get(uid)
            if unit is None:
                continue
            if unit["status"] != "UNIT_VERIFIED":
                return False, f"Unit {uid} is not UNIT_VERIFIED (status={unit['status']})"
            for vid in unit["spec"]["verifier_ids"]:
                # Bound conservative revalidation by verifier and remaining task budgets.
                self.check_task_budget()
                task_rem = self.remaining_task_budget()
                if task_rem <= 0:
                    self._persist_runtime()
                    raise Stop("BUDGET_EXHAUSTED", "revalidation_budget", "No remaining budget for revalidation")

                defn = self.verifier_registry.get(vid)
                if not defn:
                    return False, f"Unknown verifier {vid} during revalidation"

                v_timeout = min(defn.get("timeout_seconds", 30.0), task_rem)
                if v_timeout <= 0:
                    self._persist_runtime()
                    raise Stop("BUDGET_EXHAUSTED", "verifier_timeout", f"No remaining budget for verifier {vid}")

                v_start = self.clock()
                res = self.repo.execute(defn["argv"], timeout=v_timeout)
                v_elapsed = self.clock() - v_start
                self._persist_runtime()
                self.check_task_budget()

                if res["exit_code"] != 0:
                    return False, f"Stale verification: Unit {uid} verifier {vid} failed on revalidation"

        v_after = snapshot(self.repo)
        if v_before != v_after:
            return False, "Repository modified during conservative revalidation"

        # Fix F: Rebind each required unit's result to final candidate snapshot
        for uid in seq:
            unit = section["units"].get(uid)
            if unit is not None and "result" in unit and isinstance(unit["result"], dict):
                unit["result"]["candidate_id"] = v_after["head"]
                unit["result"]["candidate_snapshot"] = copy.deepcopy(v_after)
                unit["result"]["trust_level"] = "INTERMEDIATE"
                work_units.seal_unit_state(unit)
        work_units._reseal_section(section)
        self._persist(section)

        # Fix 5: Point 6b - after conservative revalidation
        self.guard_supervision()
        return True, None

    # ----------------------------------------------------------------------- main loop

    def run_sequence(self) -> dict:
        """Run the fixed WorkUnit sequence sequentially until completion or failure.

        Returns a dictionary with status:
        - MILESTONE_READY when all units pass and conservative revalidation succeeds.
        - MILESTONE_FAILED on failure.
        """
        if self.reconciliation_stop is not None:
            if self.reconciliation_stop.reason not in {"INVOCATION_REPLAY_REFUSED", "INVOCATION_REVIEW_REQUIRED",
                                                       "REPAIR_AUTHORIZATION_DORMANT", "REPAIR_RECOVERY_REPLAY_REFUSED",
                                                       "REPAIR_RECOVERY_REVIEW_REQUIRED"}:
                self._persist_runtime()
            return {
                "status": self.reconciliation_stop.status,
                "reason": self.reconciliation_stop.reason,
                "error": self.reconciliation_stop.message,
            }

        section = self.get_section()
        validate_sequence_integrity(section)

        try:
            while True:
                self.check_task_budget()
                self.guard_supervision()

                # Fix 8: Any terminal unit failure stops scheduling immediately
                if any(u["status"] == "UNIT_FAILED" for u in section["units"].values()):
                    self._persist_runtime()
                    failed_uids = [uid for uid, u in section["units"].items() if u["status"] == "UNIT_FAILED"]
                    return {
                        "status": MILESTONE_FAILED,
                        "reason": "UNIT_FAILED",
                        "failed_units": failed_uids,
                    }

                ready_units = self.evaluate_readiness(section)
                if not ready_units:
                    all_verified = all(u["status"] == "UNIT_VERIFIED" for u in section["units"].values())
                    if all_verified:
                        break
                    # No unit is ready, but not all verified: dependency deadlock
                    self._persist_runtime()
                    pending_uids = [uid for uid, u in section["units"].items() if u["status"] != "UNIT_VERIFIED"]
                    return {
                        "status": MILESTONE_FAILED,
                        "reason": "DEPENDENCY_DEADLOCK",
                        "pending_units": pending_uids,
                    }

                # Select the next eligible unit in stable deterministic order
                unit = ready_units[0]
                res = self.execute_unit(unit, section)
                if not res["success"]:
                    self._persist_runtime()
                    return {
                        "status": MILESTONE_FAILED,
                        "reason": res.get("failure_classification", "UNIT_FAILED"),
                        "unit_id": unit["unit_id"],
                        "error": res.get("error"),
                    }
        except Stop as stop:
            self._persist_runtime()
            return {
                "status": stop.status,
                "reason": stop.reason,
                "error": stop.message,
            }
        except Exception:
            self._persist_runtime()
            raise

        # Fix 4 & Fix B: Set status to REVALIDATING and record stage timing before conservative revalidation
        manager = copy.deepcopy(self.store.state.get("manager"))
        if manager is not None:
            now_runtime = round(self.runtime(), 3)
            manager["milestone_status"] = "REVALIDATING"
            manager["runtime_seconds"] = now_runtime
            manager["active_stage_timing"] = {
                "stage": "REVALIDATING",
                "stage_start_clock": self.clock(),
                "stage_start_runtime": now_runtime,
                "accounted_runtime": now_runtime,
            }
            self.store.commit(manager=manager)

        # Conservative revalidation across all units
        try:
            reval_passed, reval_error = self.revalidate_milestone(section)
        except Stop as stop:
            self._persist_runtime()
            return {
                "status": stop.status,
                "reason": stop.reason,
                "error": stop.message,
            }
        except Exception:
            self._persist_runtime()
            raise

        if not reval_passed:
            if manager is not None:
                manager["milestone_status"] = MILESTONE_FAILED
                manager["runtime_seconds"] = round(self.runtime(), 3)
                if "active_stage_timing" in manager:
                    del manager["active_stage_timing"]
                self.store.commit(manager=manager)
            return {
                "status": MILESTONE_FAILED,
                "reason": "STALE_VERIFICATION",
                "error": reval_error,
            }

        # Fix 3: Final budget check before MILESTONE_READY
        try:
            self.check_task_budget()
            # Fix 5: Point 7 - immediately before MILESTONE_READY persistence
            self.guard_supervision()
        except Stop as stop:
            self._persist_runtime()
            return {
                "status": stop.status,
                "reason": stop.reason,
                "error": stop.message,
            }

        # Fix 4 & Fix F: Bind MILESTONE_READY to candidate fingerprint and snapshot
        cand_snap = snapshot(self.repo)
        if manager is not None:
            manager["milestone_status"] = MILESTONE_READY
            manager["milestone_candidate_fingerprint"] = cand_snap["head"]
            manager["milestone_candidate_snapshot"] = cand_snap
            manager["runtime_seconds"] = round(self.runtime(), 3)
            if "active_stage_timing" in manager:
                del manager["active_stage_timing"]
            self.store.commit(manager=manager, current_step="milestone_ready")

        return {
            "status": MILESTONE_READY,
            "work_units_complete": True,
            "section": section,
        }
