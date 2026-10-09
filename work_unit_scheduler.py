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
        self._persist(section)

    # ----------------------------------------------------------------------- guarded verification (Fix D, Fix G, Fix A)

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
        uid = unit["unit_id"]

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

        before = snapshot(self.repo)
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
                after = snapshot(self.repo)
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
