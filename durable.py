"""Opt-in durable execution. Disk state is controller evidence, never model memory."""

from __future__ import annotations

import copy
import ctypes
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
import functools
import shutil
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from harness import EventLog, Gateway, Repository, Workflow, model_identity, safe_command, command_timeout
import environment
from supervision import Supervisor, initial as initial_supervision, validate as validate_supervision, controller_status, run_command
import winprocess

SCHEMA_VERSION = 1
TERMINAL_ACCEPTANCE_SCHEMA_VERSION = 1
REPAIR_AUTHORIZATION_SCHEMA_VERSION = 1
# GRANTED v1 can become CONSUMED v2 only in the continuous allocation operation.
# The atomic transaction only allocates. Mode v3 dispatch has a separate WAL lifecycle.
# Issuance requires the explicit active controller operation, never JSON authority.
_REPAIR_DECISION_EVENT = "repair_policy_b_decision"
_REPAIR_GRANT_EVENT = "repair_authorization_granted"
_REPAIR_CONSUME_EVENT = "repair_authorization_consumed"
_INVOCATION_EVENTS = ("invocation_intent", "invocation_bound", "invocation_release",
                      "process_exit", "invocation_complete")
_SAFE_QWEN_IDENTITY = "safe_qwen_recovery_identity"
_SAFE_QWEN_RESERVATION = "recovery/safe-qwen-identity.json"
STATUSES = {"CREATED", "PLANNING", "EXECUTING", "VERIFYING", "REVIEWING",
            "CHECKPOINTING", "VERIFIED", "FAILED", "INTERRUPTED", "COMPLETED",
            # Bounded autonomous Manager runs only (see manager.py).
            "READY", "CRITIQUING", "BLOCKED", "BUDGET_EXHAUSTED", "STALLED", "HUMAN_ACTION_REQUIRED"}


class DurableError(ValueError):
    """Fail closed without modifying the target checkout."""


def now():
    return datetime.now(timezone.utc).isoformat()


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def atomic_write(path: Path, data: bytes):
    """Flush a sibling temporary file before replacing the complete destination."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".durable-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


@contextmanager
def exclusive_lock(path: Path):
    """Kernel lock, automatically released on death/reboot; never steal PID locks."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if path.stat().st_size == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise DurableError(f"Another durable operation holds {path}") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def git(repo: Repository, *args, optional=False):
    result = subprocess.run(["git", "-c", f"safe.directory={repo.root}", *args],
                            cwd=repo.root, capture_output=True, timeout=30)
    if result.returncode and not (optional and result.returncode == 1):
        raise DurableError(f"Cannot fingerprint repository: git {' '.join(args)} failed")
    return result.stdout.decode("utf-8", errors="strict")


def snapshot(repo: Repository):
    """Content hashes include the full index and every non-ignored working file."""
    head = git(repo, "rev-parse", "--verify", "HEAD").strip()
    git_dir = Path(git(repo, "rev-parse", "--absolute-git-dir").strip()).resolve()
    index = git(repo, "ls-files", "--stage", "-z")
    if any(item.startswith("160000 ") for item in index.split("\0")):
        raise DurableError("Durable runs do not yet support submodules")
    names = git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    files = {}
    for name in sorted(set(names.split("\0")) - {""}):
        path = repo.path(name)
        if not path.exists():
            files[name] = None
        elif not path.is_file():
            raise DurableError(f"Cannot fingerprint non-file: {name}")
        else:
            with path.open("rb") as stream:
                file_hash = hashlib.file_digest(stream, "sha256").hexdigest()
            files[name] = {"sha256": file_hash, "mode": stat.S_IMODE(path.stat().st_mode)}
    identity = git_dir.stat()
    result = {"repository": str(repo.root), "git_directory": str(git_dir),
              "git_identity": [identity.st_dev, identity.st_ino], "head": head,
              "branch": git(repo, "symbolic-ref", "-q", "HEAD", optional=True).strip(),
              "status": git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all"),
              "index_sha256": hashlib.sha256(index.encode()).hexdigest(), "files": files}
    result["fingerprint"] = digest(result)
    return result


def process_identity(pid):
    """PID plus creation token avoids mistaking a reused PID for an orphaned test."""
    if os.name == "nt":
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            if ctypes.get_last_error() == 87:  # ERROR_INVALID_PARAMETER: no such process.
                return None
            raise DurableError("Cannot establish whether a previous verifier process is alive")
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
                raise DurableError("Cannot read verifier process creation time")
            if times[1].dwHighDateTime or times[1].dwLowDateTime:
                return None
            return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        finally:
            kernel.CloseHandle(handle)
    path = Path(f"/proc/{pid}/stat")
    try:
        fields = path.read_text().rsplit(")", 1)[1].split()
        return None if fields[0] == "Z" else fields[19]
    except FileNotFoundError:
        return None


def check_previous_command(state):
    command = state.get("active_command")
    if not command:
        return
    if command["phase"] == "launching":
        raise DurableError("Interrupted during command launch; child identity is uncertain. Automatic resume refused. "
                           "Inspect/stop possible child processes before manually recovering this run.")
    if command["identity"] is not None and process_identity(command["pid"]) == command["identity"]:
        raise DurableError(f"Previous command process {command['pid']} is still running; wait for it to exit before resume")


def validate_state(state):
    if not isinstance(state, dict) or type(state.get("schema_version")) is not int:
        raise DurableError("Malformed durable state: missing schema_version")
    if state["schema_version"] != SCHEMA_VERSION:
        raise DurableError(f"Unsupported durable schema version: {state['schema_version']}")
    types = {"run_id": str, "created_at": str, "updated_at": str, "status": str,
             "original_task": str, "acceptance_criteria": list, "target_repository": str,
             "original_git_head": str, "current_git_head": str, "round_number": int,
             "revision": int, "verified_progress": list, "remaining_work": list,
             "current_step": str, "history": list, "evidence": dict, "models": dict,
             "recovery": dict, "options": dict, "baseline": dict, "unverified_work": dict}
    if any(type(state.get(key)) is not kind for key, kind in types.items()):
        raise DurableError("Malformed durable state: required field or type")
    if state["status"] not in STATUSES or state["revision"] < 1 or state["round_number"] < 0:
        raise DurableError("Malformed durable state: status/revision/round")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", state["run_id"]):
        raise DurableError("Invalid run ID")
    options = state["options"]
    commands = options.get("commands")
    if (not isinstance(commands, list) or not commands or not all(safe_command(c) for c in commands)
            or type(options.get("reviewer")) is not bool or type(options.get("critic")) is not bool
            or (options["critic"] and not options["reviewer"] and "manager" not in state)):
        raise DurableError("Malformed durable workflow options")
    if not isinstance(options.get("config"), dict):
        raise DurableError("Missing pinned run configuration")
    for key in ("last_verified_checkpoint", "last_failure_evidence"):
        if key not in state or (state[key] is not None and not isinstance(state[key], dict)):
            raise DurableError(f"Malformed durable state: {key}")
    checkpoint = state["last_verified_checkpoint"]
    if bool(checkpoint) != bool(state["verified_progress"]):
        raise DurableError("Verified progress has no checkpoint")
    if state["status"] in {"VERIFIED", "COMPLETED"} and not checkpoint:
        raise DurableError("Completed state has no checkpoint")
    if "supervision" in state or "environment" in state:
        if "supervision" not in state or "environment" not in state:
            raise DurableError("Incomplete supervision/environment extension")
        validate_supervision(state["supervision"], state["run_id"])
        environment.validate(state["environment"])
    if "manager" in state:
        import manager
        manager.validate(state["manager"], state)
    if "work_units" in state:
        import work_units
        work_units.validate_section(state["work_units"])
    from supervision import validate_invocations
    validate_invocations(state)
    if "work_unit_planner" in state:
        import work_unit_planner
        work_unit_planner.validate_planning_section(state["work_unit_planner"], durable_state=state)
    if "terminal_acceptance" in state:
        validate_terminal_acceptance(state["terminal_acceptance"], state["run_id"])
    validate_repair_authorizations(state)
    validate_safe_qwen_recovery_identity(state)
    from safe_qwen_active_recovery import validate as validate_active_first
    validate_active_first(state)


def validate_safe_qwen_recovery_identity(state):
    """Closed identity-only v1; neither worker execution nor repair authority.

    This slice deliberately keeps the parent READY and unattempted. Slots are
    immutable reservations, not allocated attempts and not additional DAG units.
    Replay checks historical pins, not current source files or live processes.
    """
    if _SAFE_QWEN_IDENTITY not in state:
        return
    try:
        from terminal_run import recovery_worker_spec
        from safe_executor_binding import digest as pin_digest
        i = state[_SAFE_QWEN_IDENTITY]
        _repair_fields(i, "schema_version kind identity_id run_id unit_id repository parent_spec_sha256 "
                       "worker_spec_sha256 scope_sha256 registry_sha256 config_sha256 model_pin_sha256 "
                       "environment_sha256 controller_session budgets_sha256 baseline_fingerprint "
                       "started_clock task_deadline unit_deadline invocations")
        _repair_require(type(i["schema_version"]) is int and i["schema_version"] == 1
                        and type(i["kind"]) is str and i["kind"] == "SAFEQWEN_IDENTITY_ONLY"
                        and type(i["identity_id"]) is str and re.fullmatch(r"[0-9a-f]{32}", i["identity_id"]))
        _repair_require(not any(k in state for k in ("repair_recovery", "repair_authorizations", "repair_dispatch",
                                                    "repair_verification", "terminal_acceptance")))
        uid = i["unit_id"]
        _repair_require(type(uid) is str and type(i["run_id"]) is str and i["run_id"] == state["run_id"]
                        and type(i["repository"]) is str and i["repository"] == state["target_repository"]
                        and state["work_units"]["sequence"] == [uid] and set(state["work_units"]["units"]) == {uid})
        unit = state["work_units"]["units"][uid]
        _repair_require(unit["status"] == "READY" and unit["attempts"] == []
                        and unit["current_attempt"] is None and unit["result"] is None
                        and state["last_verified_checkpoint"] is None and state["verified_progress"] == [])
        cfg = state["options"]["config"]
        pin = cfg["safe_qwen_identity"]
        _repair_require(cfg.get("executor") == "safe_qwen" and type(pin) is dict
                        and type(pin.get("version")) is int and pin["version"] == 2
                        and pin["sha256"] == pin_digest({k: v for k, v in pin.items() if k != "sha256"})
                        and pin["configuration_sha256"] == pin_digest({k: v for k, v in cfg.items() if k != "safe_qwen_identity"})
                        and pin["scope"]["root"] == i["repository"]
                        and state["options"]["work_unit_executor"] == {"kind": "safe_qwen", "identity_sha256": pin["sha256"]})
        worker = recovery_worker_spec(unit["spec"], cfg)
        for key, value in (("parent_spec_sha256", unit["spec"]), ("worker_spec_sha256", worker),
                           ("scope_sha256", {"parent": unit["spec"]["scope"], "manager": state["manager"]["scope"],
                                             "worker": pin["scope"]}),
                           ("registry_sha256", state["options"]["verifier_registry"]), ("config_sha256", cfg),
                           ("environment_sha256", state["environment"]), ("budgets_sha256", state["manager"]["budgets"])):
            _repair_sha(i[key])
            _repair_require(i[key] == digest(value))
        _repair_sha(i["model_pin_sha256"])
        _repair_require(i["model_pin_sha256"] == pin["sha256"])
        _repair_require(encoded(i["controller_session"]) == encoded(_repair_session(state)))
        _repair_sha(i["baseline_fingerprint"])
        for key in ("started_clock", "task_deadline", "unit_deadline"):
            _repair_require(type(i[key]) in (int, float) and math.isfinite(i[key]) and i[key] > 0)
        _repair_require(i["task_deadline"] > i["started_clock"]
                        and i["unit_deadline"] == i["started_clock"] + unit["spec"]["limits"]["timeout_seconds"]
                        and i["task_deadline"] - i["started_clock"] <= state["manager"]["budgets"]["max_runtime_seconds"])
        slots = i["invocations"]
        _repair_require(type(slots) is list and len(slots) == 2)
        for number, slot in enumerate(slots, 1):
            _repair_fields(slot, "execution_id attempt_id attempt_number worker_spec worker_spec_sha256")
            _repair_require(type(slot["attempt_number"]) is int and slot["attempt_number"] == number
                            and type(slot["attempt_id"]) is str and slot["attempt_id"] == f"{uid}-attempt-{number}"
                            and type(slot["execution_id"]) is str
                            and slot["execution_id"] == digest([i["identity_id"], i["run_id"], uid, number, i["worker_spec_sha256"]])
                            and encoded(slot["worker_spec"]) == encoded(worker)
                            and type(slot["worker_spec_sha256"]) is str and slot["worker_spec_sha256"] == digest(worker))
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise DurableError("Invalid SafeQwen recovery identity") from exc


def _validate_safe_qwen_identity_history(records, read_evidence, tail=b"", reservation_exists=False):
    """A separately flushed reservation makes even a torn initial commit fatal."""
    previous = {}
    selected = None
    for record in records:
        _repair_require(type(record["event"]) is str and not record["event"].startswith("safe_qwen_recovery_"))
        transition = record.get("transition")
        _repair_require(not (type(transition) is dict and type(transition.get("event")) is str
                             and transition["event"].startswith("safe_qwen_recovery_")))
        marker = record.get(_SAFE_QWEN_IDENTITY)
        _repair_require(record["event"] == "state_committed" or marker is None)
        if record["event"] != "state_committed":
            continue
        state = record["state"]
        identity = state.get(_SAFE_QWEN_IDENTITY)
        if identity is not None:
            validate_safe_qwen_recovery_identity(state)
            if selected is None:
                _repair_fields(record, "sequence time event previous sha256 state transition safe_qwen_recovery_identity")
                _repair_require(marker == {"event": "identity_selected", "sha256": digest(identity)}
                                and record["transition"] is None and previous)
                ignored = {"revision", "updated_at", _SAFE_QWEN_IDENTITY}
                _repair_require({k: v for k, v in previous.items() if k not in ignored} ==
                                {k: v for k, v in state.items() if k not in ignored})
                selected = identity
                _repair_require(read_evidence({"path": _SAFE_QWEN_RESERVATION, "sha256": digest(identity)}) == identity)
            else:
                _repair_require(encoded(identity) == encoded(selected) and marker is None)
                uid = identity["unit_id"]
                _repair_require(state["work_units"]["units"][uid] == previous["work_units"]["units"][uid])
        else:
            _repair_require(selected is None and marker is None)
        previous = state
    _repair_require(not ((selected is not None or reservation_exists) and tail)
                    and bool(selected) == bool(reservation_exists))


def _repair_require(condition):
    if not condition:
        raise DurableError("Invalid, stale or replayed repair authorization; continuation refused")


def _repair_fields(value, fields):
    _repair_require(type(value) is dict and set(value) == set(fields.split()))


def _repair_sha(value):
    _repair_require(type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None)


def _repair_ref(value):
    _repair_fields(value, "sequence sha256")
    _repair_require(type(value["sequence"]) is int and value["sequence"] > 0)
    _repair_sha(value["sha256"])


def _repair_context(state, unit, attempt):
    """Identity of the existing cumulative account and recovery controller."""
    manager = state["manager"]
    session = _repair_session(state)
    timing = attempt["stage_timing"]
    elapsed = attempt["timeout_info"]["elapsed_seconds"]
    runtime = manager["runtime_seconds"]
    timeout = unit["spec"]["limits"]["timeout_seconds"]
    for value in (elapsed, runtime, timeout, timing["stage_start_clock"], timing["attempt_elapsed_at_start"]):
        _repair_require(type(value) in (int, float) and math.isfinite(value) and value >= 0)
    _repair_require(elapsed < timeout and runtime < manager["budgets"]["max_runtime_seconds"])
    _repair_require(timing["stage"] == "VERIFYING" and timing["unit_id"] == unit["unit_id"]
                    and timing["attempt_id"] == attempt["attempt_id"])
    budget = {"accounting_sha256": digest({"budgets": manager["budgets"], "limits": unit["spec"]["limits"],
                                         "timing": timing, "runtime": runtime, "elapsed": elapsed}),
              "task_runtime_seconds": runtime, "unit_elapsed_seconds": elapsed,
              "unit_deadline": timing["stage_start_clock"] + timeout - timing["attempt_elapsed_at_start"]}
    _repair_require(budget["unit_deadline"] > timing["stage_start_clock"])
    return session, budget


def _repair_session(state):
    controller = state["supervision"]["controller"]
    _repair_require(type(controller) is dict and type(controller["identity"]) is dict)
    manager = state["manager"]
    _repair_require(type(manager["session"]) is int and manager["session"] >= 0)
    session = {"manager_session": manager["session"], "recovery_sha256": digest(state["recovery"]),
               "controller_sha256": digest({k: controller[k] for k in ("pid", "identity", "boot")})}
    return session


def validate_repair_recovery(state):
    if "repair_recovery" not in state:
        _repair_require("repair_dispatch" not in state and "repair_verification" not in state)
        return
    try:
        mode = state["repair_recovery"]
        allocation = type(mode) is dict and type(mode.get("schema_version")) is int and mode["schema_version"] in (2, 3, 4)
        dispatch = allocation and mode["schema_version"] in (3, 4)
        _repair_fields(mode, "schema_version mode recovery_id run_id unit_id source_attempt_id spec_sha256 "
                       "registry_sha256 controller_session budgets_sha256 environment_sha256 baseline_fingerprint "
                       "started_clock task_deadline" + (" operation" if allocation else "")
                       + (" offline_recipe offline_sources" if dispatch else ""))
        _repair_require(type(mode["schema_version"]) is int and mode["schema_version"] in (1, 2, 3, 4)
                        and type(mode["mode"]) is str and mode["mode"] == "ACTIVE")
        if allocation:
            _repair_require(type(mode["operation"]) is str and mode["operation"] ==
                            ({2: "ALLOCATE_ONLY", 3: "OFFLINE_DISPATCH", 4: "OFFLINE_VERIFY"}[mode["schema_version"]]))
        if mode["schema_version"] != 4:
            _repair_require("repair_verification" not in state)
        if dispatch:
            from offline_scripted_executor import validate_script, _json, OfflineExecutorError
            recipe = mode["offline_recipe"]
            _repair_fields(recipe, "script allowed_paths forbidden_paths timeout_seconds")
            try:
                validate_script(recipe["script"], recipe["allowed_paths"], recipe["forbidden_paths"])
            except OfflineExecutorError as exc:
                raise DurableError("Invalid frozen offline recipe") from exc
            _repair_require(len(_json(recipe)) <= 8192 and type(recipe["timeout_seconds"]) in (int, float)
                            and math.isfinite(recipe["timeout_seconds"]) and 0 < recipe["timeout_seconds"] <= 3600)
            _repair_fields(mode["offline_sources"], "offline_scripted_executor.py offline_scripted_executor_worker.py winprocess.py")
            for sha in mode["offline_sources"].values():
                _repair_sha(sha)
        else:
            _repair_require("repair_dispatch" not in state and "repair_verification" not in state)
        _repair_require(type(mode["recovery_id"]) is str and re.fullmatch(r"[0-9a-f]{32}", mode["recovery_id"]))
        _repair_require(mode["run_id"] == state["run_id"] and type(mode["run_id"]) is str
                        and type(mode["unit_id"]) is str and type(mode["source_attempt_id"]) is str
                        and mode["source_attempt_id"] == mode["unit_id"] + "-attempt-1")
        unit = state["work_units"]["units"][mode["unit_id"]]
        if dispatch:
            import work_units
            _repair_require(unit["spec"]["mode"] == "mutation"
                            and recipe["allowed_paths"] == unit["spec"]["scope"]["allowed_paths"]
                            and recipe["forbidden_paths"] == sorted(set(unit["spec"]["scope"]["forbidden_paths"]
                                                                       + state["manager"]["scope"]["forbidden_paths"]))
                            and all(any(work_units.covers(a, p) for a in state["manager"]["scope"]["allowed_paths"])
                                    for p in recipe["allowed_paths"]))
        _repair_require(unit["spec"]["limits"]["max_attempts"] == 2
                        and len(unit["attempts"]) <= (2 if allocation else 1))
        if len(unit["attempts"]) == 2:
            _validate_repair_allocation(state, state["repair_authorizations"]["repair-" + mode["recovery_id"]])
        else:
            _repair_require("repair_dispatch" not in state and "repair_verification" not in state)
        _repair_require(all("policy_b_shadow" not in a for u in state["work_units"]["units"].values()
                            for a in u["attempts"]))
        _repair_require(state["options"]["config"].get("executor") != "safe_qwen"
                        and state["options"].get("work_unit_executor", {}).get("kind") != "safe_qwen")
        for key, value in (("spec_sha256", unit["spec"]), ("registry_sha256", state["options"]["verifier_registry"]),
                           ("budgets_sha256", state["manager"]["budgets"]), ("environment_sha256", state["environment"])):
            _repair_sha(mode[key])
            _repair_require(mode[key] == digest(value))
        _repair_sha(mode["baseline_fingerprint"])
        _repair_require(encoded(mode["controller_session"]) == encoded(_repair_session(state)))
        for key in ("started_clock", "task_deadline"):
            _repair_require(type(mode[key]) in (int, float) and math.isfinite(mode[key]) and mode[key] > 0)
        _repair_require(mode["task_deadline"] > mode["started_clock"])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise DurableError("Invalid active recovery identity") from exc


def validate_repair_authorizations(state):
    """Closed GRANTED v1 / allocated CONSUMED v2 schemas; never permissions.

    Only validated Store WAL history can carry this extension. Passing a JSON
    object through this validator proves shape/relationships, never permission.
    Absence retains the M0 schema and behavior, including its one-attempt gates.
    """
    validate_repair_recovery(state)
    if "repair_authorizations" not in state:
        return
    try:
        grants = state["repair_authorizations"]
        _repair_require(type(grants) is dict and bool(grants))
        seen = set()
        for aid, grant in grants.items():
            consumed = (type(grant) is dict and type(grant.get("schema_version")) is int
                        and grant["schema_version"] == 2 and grant.get("state") == "CONSUMED")
            _repair_fields(grant, "schema_version state run_id unit_id source_attempt_id authorization_id "
                           "candidate_fingerprint spec_sha256 registry_sha256 source_wal policy_b policy_wal "
                           "native_evidence budget recovery_session" + (" consumption" if consumed else ""))
            _repair_require(type(grant["schema_version"]) is int
                            and grant["schema_version"] == (2 if consumed else REPAIR_AUTHORIZATION_SCHEMA_VERSION))
            _repair_require(type(grant["state"]) is str and grant["state"] == ("CONSUMED" if consumed else "GRANTED"))
            for key in ("run_id", "unit_id", "source_attempt_id", "authorization_id"):
                _repair_require(type(grant[key]) is str and bool(grant[key]) and len(grant[key]) <= 160)
            _repair_require(type(aid) is str and aid == grant["authorization_id"]
                            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", aid) is not None)
            _repair_require(grant["run_id"] == state["run_id"])
            binding = (grant["unit_id"], grant["source_attempt_id"])
            _repair_require(binding not in seen)
            seen.add(binding)
            unit = state["work_units"]["units"][grant["unit_id"]]
            _repair_require(unit["unit_id"] == grant["unit_id"] and len(unit["attempts"]) == (2 if consumed else 1))
            if consumed:
                _validate_repair_allocation(state, grant)
            attempt = unit["attempts"][0]
            _repair_require(attempt["attempt_id"] == grant["source_attempt_id"]
                            and attempt["completed_at"] is not None and attempt["outcome"] == "failed"
                            and attempt["failure_classification"] == "verification_failed"
                            and (consumed or (unit["current_attempt"] is None
                                 and unit["status"] in {"VERIFYING", "REPAIR_PENDING"}))
                            and unit["spec"]["limits"]["max_attempts"] >= 2)
            # Shadow observation is permanently diagnostic, including ALLOW.
            _repair_require("policy_b_shadow" not in attempt)
            candidate = attempt["candidate_snapshot"]
            from retry_eligibility import validate_normalize_snapshot
            _, snapshot_error = validate_normalize_snapshot(candidate, "repair authorization candidate")
            _repair_require(snapshot_error is None)
            _repair_sha(grant["candidate_fingerprint"])
            _repair_require(candidate["fingerprint"] == grant["candidate_fingerprint"]
                            and digest({k: v for k, v in candidate.items() if k != "fingerprint"}) == candidate["fingerprint"]
                            and candidate == attempt["verification_snapshot_before"] == attempt["verification_snapshot_after"])
            for key, value in (("spec_sha256", unit["spec"]),
                               ("registry_sha256", state["options"]["verifier_registry"])):
                _repair_sha(grant[key])
                _repair_require(grant[key] == digest(value))
            for key in ("source_wal", "policy_wal"):
                _repair_ref(grant[key])
            policy = grant["policy_b"]
            _repair_fields(policy, "policy eligible decision reason_code failed_verifiers")
            _repair_require(type(policy["policy"]) is str and policy["policy"] == "B"
                            and policy["eligible"] is True and type(policy["decision"]) is str
                            and policy["decision"] == "ALLOW" and type(policy["reason_code"]) is str
                            and policy["reason_code"] == "ALLOW_VERIFIER_FAILURE")
            failed = policy["failed_verifiers"]
            _repair_require(type(failed) is list and bool(failed)
                            and all(type(v) is str for v in failed) and len(set(failed)) == len(failed))
            evidence = grant["native_evidence"]
            _repair_require(type(evidence) is dict and set(evidence) == set(unit["spec"]["verifier_ids"]))
            actual_failed = []
            for vid, binding in evidence.items():
                _repair_fields(binding, "child_id invocation_id wal evidence")
                _repair_require(type(binding["child_id"]) is str and type(binding["invocation_id"]) is str)
                child = state["supervision"]["children"][binding["child_id"]]
                inv = child["invocation"]
                _repair_require(child["token"] == binding["invocation_id"] and child["run_id"] == grant["run_id"]
                                and child["state"] == "EXITED" and child["termination"] == "natural"
                                and inv["phase"] == "COMPLETE" and inv["verifier_id"] == vid
                                and inv["unit_id"] == grant["unit_id"] and inv["attempt_id"] == grant["source_attempt_id"])
                for key in ("spec_sha256", "registry_sha256", "candidate_fingerprint"):
                    _repair_require(inv[key] == grant[key])
                if child["exit_code"] != 0:
                    actual_failed.append(vid)
                _repair_fields(binding["wal"], " ".join(_INVOCATION_EVENTS))
                for ref in binding["wal"].values():
                    _repair_ref(ref)
                _repair_fields(binding["evidence"], "path sha256")
                _repair_sha(binding["evidence"]["sha256"])
                _repair_require(binding["evidence"]["path"] ==
                                f"repair-evidence/{grant['source_attempt_id']}/{binding['invocation_id']}.json")
            _repair_require(failed == sorted(actual_failed))
            # Budget evidence is historical, not reset when runtime advances.
            # Issuance replay additionally checks this against source_wal.
            historical = copy.deepcopy(state)
            historical["manager"]["runtime_seconds"] = grant["budget"]["task_runtime_seconds"]
            session, budget = _repair_context(historical, unit, attempt)
            _repair_require(state["manager"]["runtime_seconds"] >= grant["budget"]["task_runtime_seconds"])
            _repair_fields(grant["recovery_session"], "manager_session recovery_sha256 controller_sha256")
            _repair_require(type(grant["recovery_session"]["manager_session"]) is int)
            for key in ("recovery_sha256", "controller_sha256"):
                _repair_sha(grant["recovery_session"][key])
            _repair_fields(grant["budget"], "accounting_sha256 task_runtime_seconds unit_elapsed_seconds unit_deadline")
            _repair_sha(grant["budget"]["accounting_sha256"])
            for key in ("task_runtime_seconds", "unit_elapsed_seconds", "unit_deadline"):
                value = grant["budget"][key]
                _repair_require(type(value) in (int, float) and math.isfinite(value) and value >= 0)
            _repair_require(grant["recovery_session"] == session and grant["budget"] == budget)
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise DurableError("Invalid repair authorization schema or binding") from exc


def _granted_form(grant):
    """Immutable historical value for comparison, never an issuance capability."""
    value = copy.deepcopy(grant)
    value.pop("consumption", None)
    value.update(schema_version=1, state="GRANTED")
    return value


def _allocated_repair_section(section, grant, consumption):
    """Use legal WorkUnit APIs to construct the one deterministic allocation."""
    import work_units
    section = copy.deepcopy(section)
    unit = section["units"][grant["unit_id"]]
    for status in ("REPAIR_PENDING", "READY", "EXECUTING"):
        work_units.transition_unit(unit, status)
    attempt = work_units.create_attempt(unit, consumption["attempt_id"])
    attempt.update(stage="allocated", started_at=consumption["allocated_at"], repair_authorization={
        "authorization_id": grant["authorization_id"], "grant_wal": copy.deepcopy(consumption["grant_wal"]),
        "granted_sha256": consumption["granted_sha256"]})
    if consumption["schema_version"] == 2:
        attempt["ai_execution_id"] = consumption["ai_binding"]["execution_id"]
        attempt["ai_worker_sha256"] = consumption["ai_binding"]["worker_sha256"]
    unit["updated_at"] = consumption["allocated_at"]
    work_units.seal_unit_state(unit)
    work_units._reseal_section(section)
    return section


def _validate_repair_allocation(state, grant):
    """Validate allocation and only its explicitly selected, closed extensions."""
    try:
        ai = state.get("safe_qwen_active_recovery", {}).get("schema_version") in (5, 6, 7)
        if ai:
            from safe_qwen_active_grant import allocation_mode, ai_binding
            mode = allocation_mode(state)
            _repair_require("repair_recovery" not in state and "repair_dispatch" not in state and "repair_verification" not in state)
        else:
            mode = state["repair_recovery"]
            _repair_require(type(mode["schema_version"]) is int and mode["schema_version"] in (2, 3, 4)
                            and mode["operation"] == {2: "ALLOCATE_ONLY", 3: "OFFLINE_DISPATCH", 4: "OFFLINE_VERIFY"}[mode["schema_version"]])
        _repair_require(type(grant["schema_version"]) is int and grant["schema_version"] == 2
                        and grant["state"] == "CONSUMED" and grant["run_id"] == mode["run_id"] == state["run_id"]
                        and grant["unit_id"] == mode["unit_id"] and grant["source_attempt_id"] == mode["source_attempt_id"]
                        and grant["authorization_id"] == "repair-" + mode["recovery_id"])
        c = grant["consumption"]
        _repair_fields(c, "schema_version attempt_id grant_wal granted_sha256 allocated_at checked_clock "
                       "task_runtime_seconds unit_elapsed_seconds task_deadline unit_deadline controller_session" + (" ai_binding" if ai else ""))
        _repair_require(type(c["schema_version"]) is int and c["schema_version"] == (2 if ai else 1)
                        and type(c["attempt_id"]) is str and c["attempt_id"] == mode["unit_id"] + "-attempt-2")
        if ai:
            _repair_require(c["ai_binding"] == ai_binding(state["safe_qwen_active_recovery"]))
            _repair_require(c["checked_clock"] < state["safe_qwen_active_recovery"]["identity"]["deadline"])
        _repair_ref(c["grant_wal"])
        _repair_sha(c["granted_sha256"])
        _repair_require(c["granted_sha256"] == digest(_granted_form(grant)))
        _repair_require(type(c["allocated_at"]) is str
                        and datetime.fromisoformat(c["allocated_at"]).tzinfo is not None)
        for key in ("checked_clock", "task_runtime_seconds", "unit_elapsed_seconds", "task_deadline", "unit_deadline"):
            _repair_require(type(c[key]) in (int, float) and math.isfinite(c[key]) and c[key] >= 0)
        runtime = state["manager"]["runtime_seconds"]
        maximum = state["manager"]["budgets"]["max_runtime_seconds"]
        _repair_require(type(runtime) in (int, float) and math.isfinite(runtime)
                        and maximum - c["task_runtime_seconds"] >= 1
                        and min(c["task_deadline"], c["unit_deadline"]) - c["checked_clock"] >= 1
                        and math.isclose(c["task_runtime_seconds"], maximum - (c["task_deadline"] - c["checked_clock"]),
                                         rel_tol=0, abs_tol=0.000001))
        _repair_require(c["task_deadline"] == mode["task_deadline"]
                        and c["unit_deadline"] == grant["budget"]["unit_deadline"]
                        and mode["started_clock"] <= c["checked_clock"] < min(c["task_deadline"], c["unit_deadline"])
                        and c["unit_elapsed_seconds"] == c["checked_clock"] - mode["started_clock"]
                        and c["unit_elapsed_seconds"] >= grant["budget"]["unit_elapsed_seconds"]
                        and grant["budget"]["task_runtime_seconds"] <= c["task_runtime_seconds"]
                        <= state["manager"]["runtime_seconds"]
                        and c["task_runtime_seconds"] < state["manager"]["budgets"]["max_runtime_seconds"]
                        and encoded(c["controller_session"]) == encoded(mode["controller_session"]) == encoded(grant["recovery_session"]))
        unit = state["work_units"]["units"][mode["unit_id"]]
        _repair_require(len(unit["attempts"]) == 2 and unit["attempts"][0]["completed_at"] is not None)
        attempt = unit["attempts"][1]
        expected = dict(attempt_id=c["attempt_id"], started_at=c["allocated_at"], completed_at=None,
                        outcome="interrupted", execution_result=None, changed_paths=[], verifier_result=None,
                        failure_classification=None, candidate_id=None, timeout_info=None, stage="allocated",
                        repair_authorization={"authorization_id": grant["authorization_id"],
                                              "grant_wal": c["grant_wal"], "granted_sha256": c["granted_sha256"]})
        if ai:
            expected.update(ai_execution_id=c["ai_binding"]["execution_id"], ai_worker_sha256=c["ai_binding"]["worker_sha256"])
        active = state.get("safe_qwen_active_recovery", {})
        if active.get("schema_version") == 7 and active.get("dispatch") is not None:
            from safe_qwen_active_verification import second_project
            _repair_require(state["work_units"] == second_project(state))
        elif active.get("schema_version") == 6 and active.get("dispatch") is not None:
            from safe_qwen_active_dispatch import allocated_section, project
            _repair_require(state["work_units"] == project(allocated_section(state), grant, active["dispatch"]))
        elif "repair_dispatch" in state:
            _validate_repair_dispatch(state)
            projected = _repair_dispatch_section(state["work_units"], grant, state["repair_dispatch"])
            if "repair_verification" in state:
                _validate_repair_verification(state)
            else:
                _repair_require(encoded(unit) == encoded(projected["units"][grant["unit_id"]]))
        else:
            _repair_require(unit["status"] == "EXECUTING" and unit["current_attempt"] == c["attempt_id"]
                            and encoded(attempt) == encoded(expected))
    except (KeyError, TypeError, ValueError, IndexError, OverflowError) as exc:
        raise DurableError("Invalid consumed repair allocation; dispatch is prohibited") from exc


def _repair_dispatch_section(section, grant, dispatch):
    """Deterministic Attempt 2 executor stages; success still awaits verification."""
    import work_units
    section = copy.deepcopy(section)
    unit = section["units"][grant["unit_id"]]
    c = grant["consumption"]
    unit["status"], unit["current_attempt"] = "EXECUTING", c["attempt_id"]
    att = dict(attempt_id=c["attempt_id"], started_at=c["allocated_at"], completed_at=None,
               outcome="interrupted", execution_result=None, changed_paths=[], verifier_result=None,
               failure_classification=None, candidate_id=None, timeout_info=None, stage="allocated",
               repair_authorization={"authorization_id": grant["authorization_id"],
                                     "grant_wal": c["grant_wal"], "granted_sha256": c["granted_sha256"]})
    unit["attempts"][1] = att
    att["stage"] = {"INTENT": "dispatch_intent", "BOUND": "dispatch_bound",
                    "RELEASED": "dispatch_released", "COMPLETE": "executor_complete"}[dispatch["phase"]]
    if dispatch["phase"] == "COMPLETE":
        result = dispatch["result"]
        att.update(execution_result={"dispatch_evidence": dispatch["evidence"]}, changed_paths=result["changed_paths"],
                   timeout_info={"elapsed_seconds": result["elapsed_seconds"],
                                 "stage_timeout": dispatch["deadline"] - c["checked_clock"]})
        if result["outcome"] != "executor_succeeded":
            work_units.complete_attempt(unit, c["attempt_id"], outcome="timeout" if result["outcome"] == "executor_timeout" else "failed",
                                        execution_result=att["execution_result"], changed_paths=att["changed_paths"],
                                        timeout_info=att["timeout_info"], failure_classification=result["outcome"])
            att["completed_at"] = dispatch["updated_at"]
            work_units.transition_unit(unit, "UNIT_FAILED")
    unit["updated_at"] = dispatch["updated_at"]
    work_units.seal_unit_state(unit)
    work_units._reseal_section(section)
    return section


def _validate_repair_dispatch(state, read_evidence=None):
    try:
        _check_repair_dispatch(state, read_evidence)
    except (KeyError, TypeError, ValueError, IndexError, OverflowError, UnicodeError) as exc:
        raise DurableError("Invalid offline dispatch lifecycle") from exc


def _check_repair_dispatch(state, read_evidence=None):
    """Closed consistency schema; only the registered live capability can write it."""
    d = state["repair_dispatch"]
    mode = state["repair_recovery"]
    _repair_require((mode["schema_version"], mode["operation"]) in {(3, "OFFLINE_DISPATCH"), (4, "OFFLINE_VERIFY")})
    _repair_fields(d, "schema_version dispatch_id phase run_id unit_id source_attempt_id attempt_id authorization_id "
                   "consumption_wal previous_wal controller_session candidate_fingerprint spec_sha256 registry_sha256 "
                   "environment_sha256 repository request_sha256 script_sha256 sources_sha256 before_snapshot_sha256 "
                   "job deadline checked_clock task_runtime_seconds updated_at process evidence result")
    _repair_require(type(d["schema_version"]) is int and d["schema_version"] == 1
                    and d["phase"] in {"INTENT", "BOUND", "RELEASED", "COMPLETE"}
                    and d["dispatch_id"] == "dispatch-" + mode["recovery_id"]
                    and d["authorization_id"] == "repair-" + mode["recovery_id"]
                    and d["run_id"] == state["run_id"] == mode["run_id"]
                    and d["unit_id"] == mode["unit_id"] and d["source_attempt_id"] == mode["source_attempt_id"]
                    and d["attempt_id"] == mode["unit_id"] + "-attempt-2")
    g = state["repair_authorizations"][d["authorization_id"]]
    _repair_require(g["state"] == "CONSUMED" and d["controller_session"] == mode["controller_session"]
                    and d["repository"] == state["target_repository"] and d["sources_sha256"] == mode["offline_sources"])
    for key in ("candidate_fingerprint", "spec_sha256", "registry_sha256"):
        _repair_require(d[key] == g[key])
    _repair_require(d["environment_sha256"] == mode["environment_sha256"])
    for key in ("request_sha256", "script_sha256", "before_snapshot_sha256"):
        _repair_sha(d[key])
    from offline_scripted_executor import _json
    recipe = mode["offline_recipe"]
    _repair_require(d["script_sha256"] == hashlib.sha256(_json(recipe["script"])).hexdigest())
    payload = dict(schema_version=1, repository=d["repository"], script=recipe["script"],
                   allowed_paths=recipe["allowed_paths"], forbidden_paths=recipe["forbidden_paths"], script_sha256=d["script_sha256"])
    _repair_require(d["request_sha256"] == hashlib.sha256(_json(payload)).hexdigest())
    _repair_ref(d["consumption_wal"])
    if d["previous_wal"] is not None:
        _repair_ref(d["previous_wal"])
    _repair_require(type(d["job"]) is str and re.fullmatch(r"Local\\triglav-offline-[0-9a-f]{32}", d["job"]))
    _repair_require(type(d["updated_at"]) is str and datetime.fromisoformat(d["updated_at"]).tzinfo is not None)
    for key in ("deadline", "checked_clock", "task_runtime_seconds"):
        _repair_require(type(d[key]) in (int, float) and math.isfinite(d[key]) and d[key] >= 0)
    _repair_require(g["consumption"]["checked_clock"] < d["deadline"] <= min(mode["task_deadline"], g["budget"]["unit_deadline"])
                    and d["deadline"] - d["checked_clock"] <= recipe["timeout_seconds"]
                    and d["checked_clock"] >= g["consumption"]["checked_clock"]
                    and g["consumption"]["task_runtime_seconds"] <= d["task_runtime_seconds"] <= state["manager"]["runtime_seconds"]
                    and math.isclose(d["task_runtime_seconds"], state["manager"]["budgets"]["max_runtime_seconds"]
                                     - (mode["task_deadline"] - d["checked_clock"]), rel_tol=0, abs_tol=0.000001))
    if d["phase"] != "COMPLETE":
        _repair_require(d["checked_clock"] < d["deadline"] and d["evidence"] is None and d["result"] is None)
    if d["phase"] in {"BOUND", "RELEASED"} or d["process"] is not None:
        p = d["process"]
        _repair_fields(p, "pid identity")
        _repair_fields(p["identity"], "created executable")
        _repair_require(type(p["pid"]) is int and p["pid"] > 0 and type(p["identity"]["created"]) is str
                        and p["identity"]["created"].isdigit() and p["identity"]["executable"] == os.path.normcase(str(Path(sys.executable).resolve())))
    if d["phase"] == "INTENT":
        _repair_require(d["process"] is None and d["previous_wal"] is None)
    if d["phase"] == "COMPLETE":
        _repair_fields(d["evidence"], "path sha256")
        _repair_sha(d["evidence"]["sha256"])
        _repair_require(d["evidence"]["path"] == "dispatch-evidence/" + d["dispatch_id"] + ".json")
        result = d["result"]
        _repair_fields(result, "outcome changed_paths elapsed_seconds")
        _repair_require(result["outcome"] in {"executor_succeeded", "executor_failed", "executor_timeout"}
                        and type(result["changed_paths"]) is list and len(result["changed_paths"]) <= 64
                        and all(type(p) is str and len(p) <= 240 for p in result["changed_paths"])
                        and type(result["elapsed_seconds"]) in (int, float) and math.isfinite(result["elapsed_seconds"])
                        and result["elapsed_seconds"] >= 0)
        first = state["work_units"]["units"][d["unit_id"]]["attempts"][0]
        _repair_require(result["elapsed_seconds"] == d["checked_clock"] - mode["started_clock"] - first["timeout_info"]["elapsed_seconds"])
        if read_evidence:
            evidence = read_evidence(d["evidence"])
            _repair_fields(evidence, "schema_version source operation repository request_sha256 before_snapshot_sha256 "
                           "after_snapshot_sha256 script_sha256 sources_sha256 process job identity_verified execution_started "
                           "termination_basis observed_exit_code cleanup execution_seconds duration_seconds diagnostics "
                           "action_result changed_paths changed_path_count scope_verified exact_edits_verified success deadline_monotonic")
            for key in ("request_sha256", "script_sha256", "sources_sha256", "before_snapshot_sha256", "repository", "job", "process"):
                _repair_require(evidence[key] == d[key])
            _repair_require(type(evidence["schema_version"]) is int and evidence["schema_version"] == 1
                            and evidence["source"] == "controller_native_handles"
                            and evidence["operation"] == "offline_scripted_actions" and evidence["deadline_monotonic"] == d["deadline"]
                            and len(_json(evidence)) <= 32768 and type(evidence["success"]) is bool
                            and evidence["changed_paths"] == result["changed_paths"])
            _repair_require(all(type(evidence[k]) is bool for k in
                                ("identity_verified", "execution_started", "scope_verified", "exact_edits_verified"))
                            and evidence["cleanup"] in {"observed_empty", "not_observed"}
                            and evidence["termination_basis"] in {"not_started", "controller_exception", "controller_deadline",
                                                                   "controller_cancel", "signaled_without_controller_termination"}
                            and (evidence["observed_exit_code"] is None or
                                 (type(evidence["observed_exit_code"]) is int and 0 <= evidence["observed_exit_code"] <= 0xffffffff))
                            and type(evidence["changed_path_count"]) is int
                            and evidence["changed_path_count"] >= len(result["changed_paths"]))
            for key in ("execution_seconds", "duration_seconds"):
                _repair_require(type(evidence[key]) in (int, float) and math.isfinite(evidence[key]) and evidence[key] >= 0)
            _repair_require(evidence["execution_seconds"] <= evidence["duration_seconds"])
            if evidence["after_snapshot_sha256"] is not None:
                _repair_sha(evidence["after_snapshot_sha256"])
            action = evidence["action_result"]
            if action is not None:
                _repair_fields(action, "schema_version script_sha256 status actions_completed error")
                count = len(recipe["script"]["actions"])
                _repair_require(type(action["schema_version"]) is int and action["schema_version"] == 1
                                and action["script_sha256"] == d["script_sha256"]
                                and type(action["actions_completed"]) is int and 0 <= action["actions_completed"] <= count)
                if action["status"] == "ok":
                    _repair_require(action["actions_completed"] == count and action["error"] is None and evidence["observed_exit_code"] == 0)
                else:
                    _repair_require(action["status"] == "failed" and action["actions_completed"] < count
                                    and type(action["error"]) is str and len(action["error"].encode("utf-8")) <= 512
                                    and all(c.isprintable() or c in "\n\t" for c in action["error"])
                                    and evidence["observed_exit_code"] == 10)
            timed_out = (evidence["termination_basis"] == "controller_deadline"
                         or d["checked_clock"] >= min(mode["task_deadline"], g["budget"]["unit_deadline"]))
            _repair_require(result["outcome"] == ("executor_timeout" if timed_out else
                            "executor_succeeded" if evidence["success"] else "executor_failed"))
            diagnostics = evidence["diagnostics"]
            _repair_fields(diagnostics, "text bytes_observed truncated")
            _repair_require(type(diagnostics["text"]) is str and len(diagnostics["text"].encode("utf-8")) <= 4096
                            and all(c.isprintable() or c in "\n\t" for c in diagnostics["text"])
                            and type(diagnostics["bytes_observed"]) is int and diagnostics["bytes_observed"] >= 0
                            and type(diagnostics["truncated"]) is bool)
            if result["outcome"] == "executor_succeeded":
                _repair_require(action is not None and action["status"] == "ok"
                                and evidence["success"] is True and evidence["cleanup"] == "observed_empty"
                                and evidence["identity_verified"] is True and evidence["execution_started"] is True
                                and evidence["scope_verified"] is True and evidence["exact_edits_verified"] is True
                                and evidence["termination_basis"] == "signaled_without_controller_termination"
                                and type(evidence["observed_exit_code"]) is int and evidence["observed_exit_code"] == 0
                                and d["checked_clock"] < min(mode["task_deadline"], g["budget"]["unit_deadline"]))


def _validate_repair_verification(state):
    """Closed offline qualification state; passing never promotes the unit."""
    try:
        from retry_eligibility import validate_normalize_snapshot
        from supervision import checksum
        m, d, v = state["repair_recovery"], state["repair_dispatch"], state["repair_verification"]
        _repair_require((m["schema_version"], m["operation"]) == (4, "OFFLINE_VERIFY")
                        and d["phase"] == "COMPLETE" and d["result"]["outcome"] == "executor_succeeded")
        _repair_fields(v, "schema_version phase run_id unit_id attempt_id dispatch_id dispatch_wal dispatch_evidence "
                       "controller_session candidate_fingerprint candidate_fs_sha256 tree_sha256 tree_snapshot checked_clock "
                       "task_runtime_seconds updated_at attempt_sha256")
        _repair_require(type(v["schema_version"]) is int and v["schema_version"] == 1
                        and v["phase"] in {"STARTED", "OBSERVING", "COMPLETE"})
        for k in ("run_id", "unit_id", "attempt_id", "dispatch_id"):
            _repair_require(type(v[k]) is str and v[k] == d[k])
        _repair_require(v["dispatch_evidence"] == d["evidence"] and v["controller_session"] == m["controller_session"])
        _repair_ref(v["dispatch_wal"])
        for k in ("candidate_fingerprint", "candidate_fs_sha256", "tree_sha256", "attempt_sha256"):
            _repair_sha(v[k])
        for k in ("checked_clock", "task_runtime_seconds"):
            _repair_require(type(v[k]) in (int, float) and math.isfinite(v[k]) and v[k] >= 0)
        _repair_require(v["checked_clock"] >= d["checked_clock"]
                        and d["task_runtime_seconds"] <= v["task_runtime_seconds"] <= state["manager"]["runtime_seconds"]
                        and math.isclose(v["task_runtime_seconds"], state["manager"]["budgets"]["max_runtime_seconds"]
                                         - (m["task_deadline"] - v["checked_clock"]), rel_tol=0, abs_tol=0.000001)
                        and type(v["updated_at"]) is str and datetime.fromisoformat(v["updated_at"]).tzinfo is not None)
        g = state["repair_authorizations"][d["authorization_id"]]
        unit = state["work_units"]["units"][v["unit_id"]]
        att = unit["attempts"][1]
        base = _repair_dispatch_section(state["work_units"], g, d)["units"][v["unit_id"]]["attempts"][1]
        mutable = {"stage", "completed_at", "outcome", "failure_classification", "verifier_result", "timeout_info"}
        extras = {"started_at_clock", "repair_observation"}
        if v["phase"] != "STARTED":
            extras.add("stage_timing")
        if v["phase"] == "COMPLETE":
            extras.update({"candidate_snapshot", "verification_snapshot_before", "verification_snapshot_after"})
        _repair_require(set(att) == set(base) | extras and digest(att) == v["attempt_sha256"]
                        and all(att[k] == base[k] for k in set(base) - mutable)
                        and type(att["started_at_clock"]) in (int, float)
                        and att["started_at_clock"] == m["started_clock"] + unit["attempts"][0]["timeout_info"]["elapsed_seconds"])
        o = att["repair_observation"]
        fields = {"policy", "mode", "phase", "registry_sha256", "candidate_snapshot", "candidate_fs_snapshot", "observations"}
        before = {"verification_snapshot_before", "verification_fs_snapshot_before", "verification_tree_before_sha256"}
        final = {"verification_snapshot_after", "verification_fs_snapshot_after", "verification_tree_after_sha256", "environment_comparison", "decision", "verification_complete"}
        _repair_require(set(o) in (fields, fields | before, fields | before | final)
                        and o["policy"] == "B" and o["mode"] == "ACTIVE" and o["registry_sha256"] == m["registry_sha256"]
                        and o["phase"] == {"STARTED": "STARTED", "OBSERVING": "OBSERVING", "COMPLETE": "OBSERVATIONS_COMPLETE"}[v["phase"]]
                        and type(o["observations"]) is dict
                        and set(o["observations"]) == set(unit["spec"]["verifier_ids"][:len(o["observations"])])
                        and digest(o["candidate_fs_snapshot"]) == v["candidate_fs_sha256"])
        _, error = validate_normalize_snapshot(o["candidate_snapshot"], "offline repair candidate")
        _repair_require(error is None and o["candidate_snapshot"]["fingerprint"] == v["candidate_fingerprint"])
        from offline_scripted_executor import _json, MAX_TREE_ENTRIES, MAX_TREE_BYTES
        import stat
        tree = v["tree_snapshot"]
        _repair_require(type(tree) is dict and 0 < len(tree) <= MAX_TREE_ENTRIES
                        and hashlib.sha256(_json(tree)).hexdigest() == v["tree_sha256"])
        fs, total, names = {}, 0, set()
        for path, entry in tree.items():
            _repair_require(type(path) is str and 0 < len(path) <= 240 and path.lower() not in names
                            and not Path(path).is_absolute() and "\\" not in path and ":" not in path
                            and all(p not in {"", ".", ".."} for p in path.split("/")) and type(entry) is list)
            names.add(path.lower())
            if len(entry) == 2 and entry[0] == "directory":
                _repair_require(type(entry[1]) is int and stat.S_ISDIR(entry[1]))
            else:
                _repair_require(len(entry) == 3 and type(entry[1]) is int and entry[1] >= 0
                                and type(entry[2]) is int and stat.S_ISREG(entry[2]))
                _repair_sha(entry[0])
                total += entry[1]
                fs[path] = dict(sha256=entry[0], size=entry[1], mode=entry[2])
        _repair_require(total <= MAX_TREE_BYTES and type(o["candidate_fs_snapshot"]) is dict and fs == o["candidate_fs_snapshot"])
        def validate_fs(value):
            _repair_require(type(value) is dict and 0 < len(value) <= MAX_TREE_ENTRIES)
            seen = set()
            for path, info in value.items():
                _repair_require(type(path) is str and 0 < len(path) <= 240 and path.lower() not in seen
                                and not Path(path).is_absolute() and "\\" not in path and ":" not in path
                                and all(p not in {"", ".", ".."} for p in path.split("/")))
                seen.add(path.lower())
                _repair_fields(info, "sha256 size mode")
                _repair_sha(info["sha256"])
                _repair_require(type(info["size"]) is int and info["size"] >= 0
                                and type(info["mode"]) is int and stat.S_ISREG(info["mode"]))
            _repair_require(sum(x["size"] for x in value.values()) <= MAX_TREE_BYTES)
        def validate_pair(canonical, filesystem):
            validate_fs(filesystem)
            for path, entry in canonical["files"].items():
                _repair_require((entry is None and path not in filesystem) or (entry is not None and path in filesystem
                                and entry["sha256"] == filesystem[path]["sha256"]
                                and entry["mode"] == stat.S_IMODE(filesystem[path]["mode"])))
        candidate, source = o["candidate_snapshot"], unit["attempts"][0]["candidate_snapshot"]
        _repair_require(all(candidate[k] == source[k] for k in ("repository", "git_directory", "git_identity", "head", "branch", "index_sha256")))
        validate_pair(candidate, fs)
        source_fs = unit["attempts"][0]["repair_observation"]["candidate_fs_snapshot"]
        mutations = sorted(p for p in fs.keys() | source_fs.keys() if fs.get(p) != source_fs.get(p))
        _repair_require(mutations == d["result"]["changed_paths"])
        for k in ("verification_snapshot_before", "verification_snapshot_after"):
            if k in o:
                _, error = validate_normalize_snapshot(o[k], k)
                _repair_require(error is None)
                validate_pair(o[k], o[k.replace("snapshot", "fs_snapshot")])
        for k in ("verification_tree_before_sha256", "verification_tree_after_sha256"):
            if k in o:
                _repair_sha(o[k])
        for vid, obs in o["observations"].items():
            _repair_fields(obs, "binding native after_snapshot after_fs_snapshot after_tree_sha256")
            _repair_sha(obs["after_tree_sha256"])
            b, n = obs["binding"], obs["native"]
            _repair_fields(b, "child_id invocation_id wal")
            _repair_require(type(b["child_id"]) is str and type(b["wal"]) is dict and set(b["wal"]) == set(_INVOCATION_EVENTS)
                            and all(type(x) is int and x > 0 for x in b["wal"].values()))
            child = state["supervision"]["children"][b["child_id"]]
            inv = child["invocation"]
            _repair_require(b["invocation_id"] == child["token"] and child["state"] == "EXITED" and inv["phase"] == "COMPLETE"
                            and inv["unit_id"] == v["unit_id"] and inv["attempt_id"] == v["attempt_id"]
                            and inv["verifier_id"] == vid and inv["candidate_fingerprint"] == v["candidate_fingerprint"]
                            and inv["spec_sha256"] == m["spec_sha256"] and inv["registry_sha256"] == m["registry_sha256"])
            _repair_fields(n, "schema_version source child_id broker verifier intended_argv intended_command_sha256 "
                           "resolved_argv resolved_command_sha256 job termination termination_basis observed_exit_code cleanup execution_started")
            _repair_require(type(n["schema_version"]) is int and n["schema_version"] == 1
                            and n["source"] == "controller_native_handles" and n["child_id"] == b["child_id"] and n["broker"] is None
                            and n["verifier"] == {"pid": child["pid"], "identity": child["identity"]}
                            and n["job"] == child["job"] and type(n["observed_exit_code"]) is int
                            and n["observed_exit_code"] == child["exit_code"] and n["cleanup"] == "observed_empty"
                            and n["execution_started"] is True and n["intended_argv"] == inv["intended_argv"]
                            and n["resolved_argv"] == inv["resolved_argv"] and n["resolved_command_sha256"] == child["command_sha256"]
                            and n["intended_command_sha256"] == checksum(inv["intended_argv"])
                            and (n["termination"], n["termination_basis"], child["termination"]) in {
                                ("natural", "signaled_without_controller_termination", "natural"),
                                ("timeout", "controller_deadline", "forced"), ("forced", "controller_cancel", "forced")})
            _, error = validate_normalize_snapshot(obs["after_snapshot"], "offline verifier after")
            _repair_require(error is None)
            validate_pair(obs["after_snapshot"], obs["after_fs_snapshot"])
        if v["phase"] == "STARTED":
            _repair_require(att == {**base, "started_at_clock": att["started_at_clock"], "repair_observation": o}
                            and not o["observations"] and set(o) == fields and unit["status"] == "EXECUTING"
                            and unit["current_attempt"] == v["attempt_id"])
        else:
            timing = att["stage_timing"]
            _repair_fields(timing, "stage unit_id attempt_id stage_start_clock stage_start_runtime attempt_elapsed_at_start")
            _repair_require(timing["stage"] == "VERIFYING" and timing["unit_id"] == v["unit_id"] and timing["attempt_id"] == v["attempt_id"])
            for k in ("stage_start_clock", "stage_start_runtime", "attempt_elapsed_at_start"):
                _repair_require(type(timing[k]) in (int, float) and math.isfinite(timing[k]) and timing[k] >= 0)
            _repair_require(timing["stage_start_clock"] <= v["checked_clock"]
                            and timing["attempt_elapsed_at_start"] == timing["stage_start_clock"] - att["started_at_clock"])
            if v["phase"] == "OBSERVING":
                _repair_require(unit["status"] == "VERIFYING" and unit["current_attempt"] == v["attempt_id"]
                                and att["completed_at"] is None and att["stage"] == "verifying"
                                and all(att[k] == base[k] for k in mutable - {"stage"}))
            else:
                _repair_require(set(o) == fields | before | final and o["decision"] is None
                                and type(o["verification_complete"]) is bool
                                and o["verification_complete"] == (set(o["observations"]) == set(unit["spec"]["verifier_ids"])))
                clean = all(o[k] == o["candidate_snapshot"] for k in ("verification_snapshot_before", "verification_snapshot_after"))
                clean = clean and all(o[k] == o["candidate_fs_snapshot"] for k in ("verification_fs_snapshot_before", "verification_fs_snapshot_after"))
                clean = clean and all(x["after_snapshot"] == o["candidate_snapshot"] and x["after_fs_snapshot"] == o["candidate_fs_snapshot"] for x in o["observations"].values())
                clean = clean and o["verification_tree_before_sha256"] == o["verification_tree_after_sha256"] == v["tree_sha256"]
                clean = clean and all(x["after_tree_sha256"] == v["tree_sha256"] for x in o["observations"].values())
                timed = any(x["native"]["termination"] == "timeout" for x in o["observations"].values())
                timed = timed or v["checked_clock"] >= min(m["task_deadline"], g["budget"]["unit_deadline"])
                passed = o["verification_complete"] and clean and all(x["native"]["termination"] == "natural" and x["native"]["observed_exit_code"] == 0 for x in o["observations"].values())
                outcome = "timeout" if timed else "passed" if passed else "failed"
                failure = "verifier_timeout" if timed else "verifier_tampered_repo" if not clean else "verification_failed" if not passed else None
                _repair_require(att["outcome"] == outcome and att["failure_classification"] == failure
                                and att["stage"] == "completed" and att["completed_at"] == v["updated_at"]
                                and unit["current_attempt"] is None and unit["status"] == ("VERIFYING" if outcome == "passed" else "UNIT_FAILED")
                                and type(att["verifier_result"]) is dict and set(att["verifier_result"]) == set(o["observations"])
                                and o["environment_comparison"]["decision"] in {"MATCH", "INFORMATIONAL"})
                for vid, entry in att["verifier_result"].items():
                    n = o["observations"][vid]["native"]
                    _repair_fields(entry, "argv exit_code passed controller_evidence")
                    _repair_require(entry["argv"] == n["intended_argv"] and type(entry["exit_code"]) is int
                                    and entry["exit_code"] == n["observed_exit_code"] and type(entry["passed"]) is bool
                                    and entry["passed"] == (n["termination"] == "natural" and n["observed_exit_code"] == 0)
                                    and entry["controller_evidence"] == n)
                _repair_fields(att["timeout_info"], "elapsed_seconds")
                _repair_require(type(att["timeout_info"]["elapsed_seconds"]) in (int, float)
                                and att["timeout_info"]["elapsed_seconds"] == v["checked_clock"] - att["started_at_clock"])
                for k in ("candidate_snapshot", "verification_snapshot_before", "verification_snapshot_after"):
                    _repair_require(att[k] == o[k])
    except (KeyError, TypeError, ValueError, IndexError, OverflowError) as exc:
        raise DurableError("Invalid offline repair verification lifecycle") from exc


def _validate_repair_history(records, read_evidence, tail=b""):
    """Validate the entire extension before Store.load publishes any state.

    Controller decision -> adjacent state_committed GRANTED transaction.
    Only an uninterrupted active controller may issue it. A pending decision, torn
    extension, duplicate transition or unproved consumption fails shut.
    """
    from supervision import checksum as supervision_checksum
    previous = {}
    latest_state_record = None
    pending = None
    extension = False
    sources = {}
    allocations = {}
    allocation_sections = {}
    dispatch_record = None
    def referenced(ref, before):
        _repair_ref(ref)
        _repair_require(ref["sequence"] < before and ref["sequence"] <= len(records))
        record = records[ref["sequence"] - 1]
        _repair_require(record["sha256"] == ref["sha256"])
        return record
    for record in records:
        event = record["event"]
        _repair_require(type(event) is str)
        _repair_require(not event.startswith("repair_") or event == _REPAIR_DECISION_EVENT)
        transition = record.get("transition")
        if type(transition) is dict:
            transition_event = transition.get("event")
            _repair_require(not (type(transition_event) is str and transition_event.startswith("repair_")))
        if event == _REPAIR_DECISION_EVENT:
            extension = True
            _repair_require(pending is None and latest_state_record is not None)
            _repair_fields(record, "sequence time event previous sha256 decision")
            _repair_require(type(record["sequence"]) is int and type(record["time"]) is str and bool(record["time"]))
            _repair_fields(record["decision"], "schema_version run_id unit_id source_attempt_id source_wal policy_b recovery_session")
            pending = record
            continue
        if pending is not None:
            _repair_require(event == "state_committed" and "repair_authorization" in record)
        if event != "state_committed":
            _repair_require("repair_authorization" not in record and "repair_dispatch" not in record and "repair_verification" not in record)
            continue
        state = record["state"]
        old_mode = previous.get("repair_recovery")
        mode = state.get("repair_recovery")
        mode_marker = record.get("repair_recovery")
        if old_mode is not None:
            _repair_require(encoded(mode) == encoded(old_mode) and mode_marker is None)
        elif mode is not None:
            extension = True
            _repair_fields(record, "sequence time event previous sha256 state transition repair_recovery")
            _repair_require(mode_marker == {"event": "repair_recovery_enabled", "recovery_id": mode["recovery_id"]}
                            and record.get("transition") is None)
            uid = mode["unit_id"]
            _repair_require(not previous["work_units"]["units"][uid]["attempts"]
                            and not state["work_units"]["units"][uid]["attempts"]
                            and previous["work_units"]["units"][uid]["status"] == "READY")
            ignored = {"revision", "updated_at", "repair_recovery"}
            _repair_require({k: v for k, v in previous.items() if k not in ignored} ==
                            {k: v for k, v in state.items() if k not in ignored})
        else:
            _repair_require(mode_marker is None)
        if mode is not None:
            extension = True
            attempts = state["work_units"]["units"][mode["unit_id"]]["attempts"]
            if attempts:
                att = attempts[0]
                _repair_require(att["attempt_id"] == mode["source_attempt_id"]
                                and att["started_at_clock"] == mode["started_clock"]
                                and att["pre_attempt_snapshot"]["fingerprint"] == mode["baseline_fingerprint"]
                                and att["repair_observation"]["mode"] == "ACTIVE")
        old = previous.get("repair_authorizations", {})
        new = state.get("repair_authorizations", {})
        _repair_require(set(old).issubset(new))
        changed = [aid for aid in old if encoded(old[aid]) != encoded(new[aid])]
        added = set(new) - set(old)
        marker = record.get("repair_authorization")
        if added:
            extension = True
            _repair_require(len(added) == 1 and pending is not None and not changed)
            _repair_fields(record, "sequence time event previous sha256 state transition repair_authorization")
            _repair_require(type(record["sequence"]) is int and type(record["time"]) is str and bool(record["time"]))
            aid = next(iter(added))
            grant = new[aid]
            _repair_require(grant["state"] == "GRANTED" and grant["schema_version"] == 1)
            _repair_require(marker == {"event": _REPAIR_GRANT_EVENT, "authorization_id": aid})
            source = referenced(grant["source_wal"], pending["sequence"])
            _repair_require(source is latest_state_record)
            source_unit = source["state"]["work_units"]["units"][grant["unit_id"]]
            source_session, source_budget = _repair_context(source["state"], source_unit, source_unit["attempts"][0])
            _repair_require(grant["recovery_session"] == source_session and grant["budget"] == source_budget
                            and source_unit["status"] == "VERIFYING")
            if mode is not None:
                _repair_require(grant["authorization_id"] == "repair-" + mode["recovery_id"]
                                and grant["unit_id"] == mode["unit_id"]
                                and source_unit["attempts"][0]["repair_observation"]["phase"] == "OBSERVATIONS_COMPLETE")
            sources[aid] = source["state"]
            _repair_require(referenced(grant["policy_wal"], record["sequence"]) is pending)
            expected = {k: grant[k] for k in ("schema_version", "run_id", "unit_id", "source_attempt_id",
                                             "source_wal", "policy_b", "recovery_session")}
            _repair_require(encoded(pending["decision"]) == encoded(expected))
            # Issuance must not allocate an attempt or change any execution fact.
            before = {k: v for k, v in previous.items() if k not in {"revision", "updated_at", "repair_authorizations"}}
            after = {k: v for k, v in state.items() if k not in {"revision", "updated_at", "repair_authorizations"}}
            _repair_require(before == after and record.get("transition") is None)
            for vid, binding in grant["native_evidence"].items():
                seqs = []
                for inv_event in _INVOCATION_EVENTS:
                    ref_record = referenced(binding["wal"][inv_event], source["sequence"])
                    _repair_require(ref_record["event"] == "state_committed" and ref_record.get("transition") ==
                                    {"event": inv_event, "child_id": binding["child_id"]})
                    seqs.append(ref_record["sequence"])
                _repair_require(seqs == sorted(set(seqs)))
                child = state["supervision"]["children"][binding["child_id"]]
                native = read_evidence(binding["evidence"])
                _repair_fields(native, "schema_version source child_id broker verifier intended_argv "
                               "intended_command_sha256 resolved_argv resolved_command_sha256 job termination "
                               "termination_basis observed_exit_code cleanup execution_started")
                _repair_fields(native["verifier"], "pid identity")
                _repair_require(type(native["verifier"]["pid"]) is int and native["broker"] is None
                                and native["termination_basis"] == "signaled_without_controller_termination"
                                and native["intended_command_sha256"] ==
                                supervision_checksum(child["invocation"]["intended_argv"]))
                _repair_require(type(native) is dict and native.get("child_id") == binding["child_id"]
                                and type(native.get("schema_version")) is int and native["schema_version"] == 1
                                and native.get("source") == "controller_native_handles"
                                and type(native.get("observed_exit_code")) is int
                                and native.get("job") == child["job"] and native.get("verifier") ==
                                {"pid": child["pid"], "identity": child["identity"]}
                                and native.get("observed_exit_code") == child["exit_code"]
                                and native.get("cleanup") == "observed_empty" and native.get("execution_started") is True
                                and native.get("termination") == "natural"
                                and native.get("resolved_command_sha256") == child["command_sha256"]
                                and native.get("intended_argv") == child["invocation"]["intended_argv"]
                                and native.get("resolved_argv") == child["invocation"]["resolved_argv"])
                entry = state["work_units"]["units"][grant["unit_id"]]["attempts"][0]["verifier_result"][vid]
                _repair_require(entry["controller_evidence"] == native and type(entry["exit_code"]) is int
                                and entry["exit_code"] == child["exit_code"]
                                and type(entry["passed"]) is bool and entry["passed"] == (child["exit_code"] == 0))
            pending = None
        elif changed:
            _repair_require(len(changed) == 1 and len(old) == len(new) == 1 and pending is None)
            _repair_fields(record, "sequence time event previous sha256 state transition repair_authorization")
            aid = changed[0]
            before, grant = old[aid], new[aid]
            _repair_require(before["state"] == "GRANTED" and before["schema_version"] == 1
                            and grant["state"] == "CONSUMED" and grant["schema_version"] == 2
                            and encoded(before) == encoded(_granted_form(grant)))
            _validate_repair_allocation(state, grant)
            c = grant["consumption"]
            _repair_require(marker == {"event": _REPAIR_CONSUME_EVENT, "authorization_id": aid, "attempt_id": c["attempt_id"]}
                            and record.get("transition") is None)
            grant_record = referenced(c["grant_wal"], record["sequence"])
            _repair_require(grant_record is latest_state_record and c["grant_wal"]["sequence"] + 1 == record["sequence"]
                            and grant_record.get("repair_authorization") == {"event": _REPAIR_GRANT_EVENT, "authorization_id": aid}
                            and c["granted_sha256"] == digest(before))
            expected = copy.deepcopy(previous)
            expected.update(repair_authorizations={aid: grant},
                            work_units=_allocated_repair_section(previous["work_units"], before, c),
                            revision=previous["revision"] + 1, updated_at=c["allocated_at"])
            expected["manager"]["runtime_seconds"] = c["task_runtime_seconds"]
            _repair_require(encoded(state) == encoded(expected))
            allocations[aid] = copy.deepcopy(state["work_units"]["units"][grant["unit_id"]]["attempts"][1])
            allocation_sections[aid] = copy.deepcopy(state["work_units"])
        else:
            _repair_require(marker is None and pending is None)
        prior_dispatch, dispatch = previous.get("repair_dispatch"), state.get("repair_dispatch")
        dispatch_marker = record.get("repair_dispatch")
        if encoded(dispatch) != encoded(prior_dispatch):
            _repair_require(dispatch is not None and not added and not changed and mode is not None)
            _repair_fields(record, "sequence time event previous sha256 state transition repair_dispatch")
            _validate_repair_dispatch(state, read_evidence)
            aid = dispatch["authorization_id"]
            _repair_require(aid in allocation_sections and record.get("transition") is None
                            and dispatch_marker == {"event": "repair_dispatch_" + dispatch["phase"].lower(),
                                                    "dispatch_id": dispatch["dispatch_id"]})
            consumed_record = referenced(dispatch["consumption_wal"], record["sequence"])
            _repair_require(consumed_record.get("repair_authorization") == {
                "event": _REPAIR_CONSUME_EVENT, "authorization_id": aid, "attempt_id": dispatch["attempt_id"]}
                and consumed_record["state"]["repair_authorizations"][aid] == new[aid])
            if prior_dispatch is None:
                _repair_require(dispatch["phase"] == "INTENT" and dispatch["previous_wal"] is None)
            else:
                _repair_require((prior_dispatch["phase"], dispatch["phase"]) in {
                    ("INTENT", "BOUND"), ("BOUND", "RELEASED"), ("RELEASED", "COMPLETE"),
                    ("INTENT", "COMPLETE"), ("BOUND", "COMPLETE")})
                _repair_require(referenced(dispatch["previous_wal"], record["sequence"]) is dispatch_record)
                mutable = {"phase", "previous_wal", "checked_clock", "task_runtime_seconds", "updated_at", "process", "evidence", "result"}
                _repair_require({k: v for k, v in prior_dispatch.items() if k not in mutable} ==
                                {k: v for k, v in dispatch.items() if k not in mutable}
                                and dispatch["checked_clock"] >= prior_dispatch["checked_clock"])
                if dispatch["phase"] != "BOUND":
                    _repair_require(dispatch["process"] == prior_dispatch["process"])
                if dispatch["phase"] == "COMPLETE" and prior_dispatch["phase"] != "RELEASED":
                    ev = read_evidence(dispatch["evidence"])
                    _repair_require(ev["execution_started"] is False and dispatch["result"]["outcome"] != "executor_succeeded")
            expected = copy.deepcopy(previous)
            expected.update(repair_dispatch=dispatch,
                            work_units=_repair_dispatch_section(allocation_sections[aid], new[aid], dispatch),
                            revision=previous["revision"] + 1, updated_at=dispatch["updated_at"])
            expected["manager"]["runtime_seconds"] = dispatch["task_runtime_seconds"]
            _repair_require(encoded(expected) == encoded(state))
            dispatch_record = record
        else:
            _repair_require(dispatch_marker is None)
        prior_v, v = previous.get("repair_verification"), state.get("repair_verification")
        vmarker = record.get("repair_verification")
        if encoded(v) != encoded(prior_v):
            _repair_fields(record, "sequence time event previous sha256 state transition repair_verification")
            _repair_require(v is not None and dispatch == prior_dispatch and dispatch_record is not None
                            and not added and not changed and record.get("transition") is None)
            _validate_repair_verification(state)
            _repair_require(vmarker == {"event": "repair_verification_" + v["phase"].lower(), "dispatch_id": dispatch["dispatch_id"]}
                            and referenced(v["dispatch_wal"], record["sequence"]) is dispatch_record
                            and v["tree_sha256"] == read_evidence(dispatch["evidence"])["after_snapshot_sha256"])
            uid = v["unit_id"]
            unit, old_unit = state["work_units"]["units"][uid], previous["work_units"]["units"][uid]
            a, old_a = unit["attempts"][1], old_unit["attempts"][1]
            o = a["repair_observation"]
            if prior_v is None:
                _repair_require(v["phase"] == "STARTED" and old_a["stage"] == "executor_complete"
                                and old_a["completed_at"] is None)
            else:
                _repair_require((prior_v["phase"], v["phase"]) in {
                    ("STARTED", "OBSERVING"), ("OBSERVING", "OBSERVING"), ("OBSERVING", "COMPLETE")})
                mutable_v = {"phase", "checked_clock", "task_runtime_seconds", "updated_at", "attempt_sha256"}
                _repair_require({k: x for k, x in v.items() if k not in mutable_v} ==
                                {k: x for k, x in prior_v.items() if k not in mutable_v}
                                and v["checked_clock"] >= prior_v["checked_clock"])
                old_o = old_a["repair_observation"]
                _repair_require(set(old_o).issubset(o) and all(o[k] == old_o[k] for k in old_o if k not in {"phase", "observations"})
                                and set(old_o["observations"]).issubset(o["observations"])
                                and all(o["observations"][k] == x for k, x in old_o["observations"].items()))
                if "stage_timing" in old_a:
                    _repair_require(a["stage_timing"] == old_a["stage_timing"])
            for vid, obs in o["observations"].items():
                seqs = []
                for ev in _INVOCATION_EVENTS:
                    seq = obs["binding"]["wal"][ev]
                    _repair_require(seq < record["sequence"] and seq <= len(records))
                    r = records[seq - 1]
                    _repair_require(r["event"] == "state_committed" and r.get("transition") == {
                        "event": ev, "child_id": obs["binding"]["child_id"]}
                        and seq > v["dispatch_wal"]["sequence"])
                    seqs.append(seq)
                _repair_require(seqs == sorted(set(seqs)))
                all_refs = [r["sequence"] for r in records[:record["sequence"] - 1]
                            if (r.get("transition") or {}).get("child_id") == obs["binding"]["child_id"]
                            and (r.get("transition") or {}).get("event") in _INVOCATION_EVENTS]
                _repair_require(all_refs == seqs)
            if v["phase"] == "COMPLETE":
                owned = [c for c in state["supervision"]["children"].values()
                         if (c.get("invocation") or {}).get("attempt_id") == v["attempt_id"]
                         and (c.get("invocation") or {}).get("unit_id") == uid]
                _repair_require(len(owned) == len(o["observations"]) and all(c["invocation"]["phase"] == "COMPLETE" for c in owned))
            # Qualification may alter only Attempt 2 and its legal status. No
            # result/checkpoint, another unit, sequence state or source mutation.
            projected_section = copy.deepcopy(previous["work_units"])
            projected_unit = projected_section["units"][uid]
            projected_unit.update(status=unit["status"], current_attempt=unit["current_attempt"], updated_at=v["updated_at"])
            projected_unit["attempts"][1] = copy.deepcopy(a)
            import work_units
            work_units.seal_unit_state(projected_unit)
            work_units._reseal_section(projected_section)
            expected = copy.deepcopy(previous)
            expected.update(repair_verification=v, work_units=projected_section,
                            revision=previous["revision"] + 1, updated_at=v["updated_at"])
            expected["manager"]["runtime_seconds"] = v["task_runtime_seconds"]
            _repair_require(encoded(state) == encoded(expected))
        else:
            _repair_require(vmarker is None)
            if v is not None:
                _repair_require(state["work_units"] == previous["work_units"])
        for aid, grant in new.items():
            historical = sources[aid]
            uid = grant["unit_id"]
            attempts = state["work_units"]["units"][uid]["attempts"]
            _repair_require(attempts[:1] == historical["work_units"]["units"][uid]["attempts"]
                            and state["work_units"]["units"][uid]["spec"] == historical["work_units"]["units"][uid]["spec"]
                            and state["manager"]["runtime_seconds"] >= previous.get("manager", {}).get("runtime_seconds", 0))
            if grant["state"] == "CONSUMED":
                expected_attempt = (_repair_dispatch_section(allocation_sections[aid], grant, dispatch)["units"][uid]["attempts"][1]
                                    if dispatch is not None else allocations[aid])
                _repair_require(aid in allocations and len(attempts) == 2)
                active = state.get("safe_qwen_active_recovery", {})
                if active.get("schema_version") == 7 and active.get("dispatch") is not None:
                    from safe_qwen_active_verification import second_project
                    _repair_require(state["work_units"] == second_project(state))
                elif active.get("schema_version") == 6 and active.get("dispatch") is not None:
                    from safe_qwen_active_dispatch import project
                    _repair_require(state["work_units"] == project(allocation_sections[aid], grant, active["dispatch"]))
                elif v is not None:
                    _validate_repair_verification(state)
                else:
                    _repair_require(encoded(attempts[1]) == encoded(expected_attempt))
            else:
                _repair_require(len(attempts) == 1)
        previous = state
        latest_state_record = record
    _repair_require(pending is None and not (extension and tail))


def validate_terminal_acceptance(term: dict, run_id: str):
    if not isinstance(term, dict):
        raise DurableError("Malformed terminal acceptance: not a dictionary")
    if type(term.get("schema_version")) is not int or term.get("schema_version") != TERMINAL_ACCEPTANCE_SCHEMA_VERSION:
        raise DurableError(f"Unsupported terminal acceptance schema: {term.get('schema_version')}")
    acceptance_id = term.get("acceptance_id")
    if not isinstance(acceptance_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", acceptance_id):
        raise DurableError(f"Invalid terminal acceptance ID: {acceptance_id}")
    controller = term.get("controller")
    if not isinstance(controller, (str, dict)) or not controller:
        raise DurableError("Malformed terminal acceptance: missing controller authority")
    seal_path = term.get("seal_path")
    if not isinstance(seal_path, str) or not seal_path:
        raise DurableError("Malformed terminal acceptance: missing seal_path")


def validate_terminal_transaction(records: list, wal_seq: int, seal_data: dict, wal_record: dict,
                                  state: dict, run_id: str, seal_ref: dict | None = None,
                                  run_dir: Path | None = None):
    """Shared validator for terminal WAL reconciliation and Store.load().

    Validates:
    - run identity
    - acceptance ID
    - registered controller binding
    - terminal flag
    - expected parent/target revision
    - expected WAL sequence
    - previous record hash
    - exact canonical seal path, actual bytes, and seal digest
    - complete accepted state
    - accepted state digest
    """
    if type(wal_seq) is not int or wal_seq <= 0 or wal_seq > len(records):
        raise DurableError("Invalid WAL sequence in terminal acceptance")

    if wal_record.get("event") != "state_committed":
        raise DurableError("Conflicting event in WAL at expected terminal sequence")

    if wal_record.get("terminal") is not True:
        raise DurableError("WAL record at terminal sequence is missing terminal=True flag")

    if type(seal_data.get("schema_version")) is not int or seal_data.get("schema_version") != TERMINAL_ACCEPTANCE_SCHEMA_VERSION:
        raise DurableError("Unsupported schema version in terminal acceptance seal")

    if seal_data.get("run_id") != run_id or state.get("run_id") != run_id:
        raise DurableError("Run ID mismatch in terminal acceptance")

    term = state.get("terminal_acceptance")
    if not term:
        raise DurableError("State missing terminal_acceptance dictionary")
    validate_terminal_acceptance(term, run_id)

    acceptance_id = term.get("acceptance_id")
    if seal_data.get("acceptance_id") != acceptance_id or wal_record.get("acceptance_id") != acceptance_id:
        raise DurableError(
            f"Conflicting terminal transaction in WAL: expected acceptance_id {acceptance_id}, "
            f"found {wal_record.get('acceptance_id')}"
        )

    canonical_rel = f"acceptance/{acceptance_id}.json"
    seal_rel = term.get("seal_path")
    if seal_rel != canonical_rel:
        raise DurableError(f"Non-canonical seal path in terminal acceptance: {seal_rel}")

    controller = term.get("controller")
    controller_bytes = encoded(controller)
    if encoded(seal_data.get("controller")) != controller_bytes or encoded(wal_record.get("controller")) != controller_bytes:
        raise DurableError("Controller mismatch across terminal state, seal, and WAL record")

    target_revision = state.get("revision")
    if type(target_revision) is not int:
        raise DurableError("Malformed target revision in terminal acceptance state")
    if type(seal_data.get("target_revision")) is not int or seal_data.get("target_revision") != target_revision:
        raise DurableError("Target revision mismatch in terminal acceptance seal")

    rec_state = wal_record.get("state")
    if not isinstance(rec_state, dict) or type(rec_state.get("revision")) is not int or rec_state.get("revision") != target_revision:
        raise DurableError("Target revision mismatch in WAL record state")

    if type(seal_data.get("parent_revision")) is not int or seal_data.get("parent_revision") != target_revision - 1:
        raise DurableError("Parent revision mismatch in terminal acceptance seal")

    if type(seal_data.get("wal_sequence")) is not int or seal_data.get("wal_sequence") != wal_seq or type(wal_record.get("sequence")) is not int or wal_record.get("sequence") != wal_seq:
        raise DurableError("WAL sequence mismatch in terminal acceptance seal or WAL record")

    expected_prev = records[wal_seq - 2]["sha256"] if wal_seq >= 2 else None
    if seal_data.get("previous_record_hash") != expected_prev or wal_record.get("previous") != expected_prev:
        raise DurableError("Previous record hash mismatch in terminal acceptance seal or WAL record")

    if run_dir is not None:
        seal_file = run_dir / canonical_rel
        if not seal_file.exists():
            raise DurableError(f"Missing terminal acceptance seal artifact at {canonical_rel}")
        try:
            raw_bytes = seal_file.read_bytes()
        except Exception as exc:
            raise DurableError(f"Cannot read terminal acceptance seal bytes: {exc}") from exc
        expected_bytes = encoded(seal_data) + b"\n"
        if raw_bytes != expected_bytes:
            raise DurableError("On-disk terminal acceptance seal bytes do not match canonical encoding or digest")

    seal_dig = digest(seal_data)
    if seal_ref is not None:
        if encoded(wal_record.get("terminal_seal")) != encoded(seal_ref):
            raise DurableError("Seal reference mismatch in WAL record")
        if seal_ref.get("sha256") != seal_dig or seal_ref.get("path") != canonical_rel:
            raise DurableError("Seal reference hash or path mismatch with canonical seal")
    else:
        wal_seal_ref = wal_record.get("terminal_seal")
        if not isinstance(wal_seal_ref, dict) or wal_seal_ref.get("sha256") != seal_dig or wal_seal_ref.get("path") != canonical_rel:
            raise DurableError("Terminal seal digest or path mismatch with WAL record reference")

    state_dig = digest(state)
    if seal_data.get("state_digest") != state_dig or digest(rec_state) != state_dig:
        raise DurableError("State digest mismatch in terminal acceptance seal or WAL record")

    state_bytes = encoded(state)
    if encoded(seal_data.get("accepted_state")) != state_bytes or encoded(rec_state) != state_bytes:
        raise DurableError("Accepted state payload mismatch in terminal acceptance seal or WAL record")




def synchronized(method):
    @functools.wraps(method)
    def wrapped(self, *args, **kwargs):
        with self.mutex:
            return method(self, *args, **kwargs)
    return wrapped


class _RepairController:
    """One uninterrupted controller operation; never deserialized from a run."""
    def __init__(self, store, scheduler, mode):
        self.store, self.scheduler, self.mode = store, scheduler, copy.deepcopy(mode)
        self.context = dict(unit_id=mode["unit_id"], attempt_id=mode["source_attempt_id"],
                            registry=copy.deepcopy(store.state["options"]["verifier_registry"]),
                            spec_sha256=mode["spec_sha256"], started_clock=mode["started_clock"])
        self.observations = {}
        self.record = None
        self.closed = False
        self.grant_record = None
        self.allocation_started = False
        self.allocation_record = None
        self.dispatch = None
        self.verification_context = None
        self.verification_observations = {}
        self.verification_section = None


class Store:
    def __init__(self, root: Path, run_id: str):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", run_id):
            raise DurableError("Invalid run ID")
        self.directory = root.resolve() / run_id
        if self.directory.is_symlink() or self.directory.resolve() != self.directory:
            raise DurableError("Run directory must not be a link")
        self.state = None
        self.records = []
        self.tail = b""
        self.mutex = threading.RLock()
        # Internal validated terminal acceptance latch and orphan seal list
        self._terminal_accepted = False
        self._orphan_seals = []
        # Registered controller identities bound to this Store instance.
        self._authorized_controllers = set()
        self._controller_token = uuid.uuid4().hex
        self._authorized_controllers.add(self._controller_token)
        self._repair_write_failed = False
        self._repair_controller = None
        self._safe_qwen_identity_pending = None
        self._safe_qwen_active_controller = None

    @synchronized
    def select_safe_qwen_recovery_identity(self, scheduler, unit_id):
        """Explicit pre-Attempt-1 selection only; return data, never a capability.

        The exclusive reservation precedes the WAL commit so partial selection
        cannot be mistaken for an ordinary M0 torn tail. No _RepairController is
        created, and no existing execution or authorization mode accepts this.
        """
        from work_unit_scheduler import WorkUnitScheduler
        from supervision import _validated_store
        from safe_executor_binding import validate_pinned
        from terminal_run import recovery_worker_spec, validate_safe_qwen_unit_contract
        from integration_shim import SafeQwenExecutor
        import manager
        _repair_require(type(scheduler) is WorkUnitScheduler and scheduler.store is self
                        and type(unit_id) is str and type(scheduler.supervisor) is Supervisor
                        and scheduler.supervisor.store is self and scheduler.clock is time.monotonic
                        and not self._repair_write_failed and self._safe_qwen_identity_pending is None
                        and not any(k in self.state for k in (_SAFE_QWEN_IDENTITY, "repair_recovery", "repair_authorizations"))
                        and not (self.directory / _SAFE_QWEN_RESERVATION).exists())
        _validated_store(self)
        sup = scheduler.supervisor
        _repair_require(sup.entered and not sup.closed and sup.failure is None and sup.thread is not None
                        and sup.thread.is_alive() and sup.worker_started.is_set())
        sup.require_idle()
        sup.heartbeat()
        live = self.state["supervision"]["controller"]
        _repair_require(live["pid"] == os.getpid() and live["identity"] == winprocess.identity(os.getpid())
                        and live["boot"] == winprocess.boot_identity())
        executor = getattr(self, "_effective_work_unit_executor", None)
        _repair_require(type(executor) is SafeQwenExecutor and scheduler.repo.store is self
                        and scheduler.repo.root == executor.policy.root
                        and scheduler.budgets == self.state["manager"]["budgets"]
                        and scheduler.parent_scope == self.state["manager"]["scope"]
                        and scheduler.verifier_registry == self.state["options"]["verifier_registry"])
        cfg = self.state["options"]["config"]
        validate_pinned(cfg, executor)
        _repair_require(scheduler.repo.config == cfg)
        manager.validate_repository(self, scheduler.repo)
        _repair_require(environment.compare(self.state["environment"], current_fingerprint(self, executor=executor))["decision"]
                        in ("MATCH", "INFORMATIONAL"))
        unit = self.state["work_units"]["units"][unit_id]
        worker = recovery_worker_spec(unit["spec"], cfg)
        validate_safe_qwen_unit_contract(worker, cfg, executor, self)
        started = scheduler.clock()
        identity = dict(schema_version=1, kind="SAFEQWEN_IDENTITY_ONLY", identity_id=uuid.uuid4().hex,
                        run_id=self.state["run_id"], unit_id=unit_id, repository=self.state["target_repository"],
                        parent_spec_sha256=digest(unit["spec"]), worker_spec_sha256=digest(worker),
                        scope_sha256=digest({"parent": unit["spec"]["scope"], "manager": self.state["manager"]["scope"],
                                             "worker": cfg["safe_qwen_identity"]["scope"]}),
                        registry_sha256=digest(self.state["options"]["verifier_registry"]), config_sha256=digest(cfg),
                        model_pin_sha256=cfg["safe_qwen_identity"]["sha256"], environment_sha256=digest(self.state["environment"]),
                        controller_session=_repair_session(self.state), budgets_sha256=digest(self.state["manager"]["budgets"]),
                        baseline_fingerprint=snapshot(scheduler.repo)["fingerprint"], started_clock=started,
                        task_deadline=started + scheduler.remaining_task_budget(started),
                        unit_deadline=started + unit["spec"]["limits"]["timeout_seconds"], invocations=[])
        for number in (1, 2):
            identity["invocations"].append(dict(execution_id=digest([identity["identity_id"], identity["run_id"], unit_id, number,
                                                                   identity["worker_spec_sha256"]]),
                                               attempt_id=f"{unit_id}-attempt-{number}", attempt_number=number,
                                               worker_spec=copy.deepcopy(worker), worker_spec_sha256=digest(worker)))
        state = copy.deepcopy(self.state)
        state.update(safe_qwen_recovery_identity=identity, revision=state["revision"] + 1, updated_at=now())
        validate_state(state)
        fields = dict(state=state, transition=None, safe_qwen_recovery_identity={"event": "identity_selected", "sha256": digest(identity)})
        self._safe_qwen_identity_pending = ("state_committed", fields)
        try:
            self.exclusive_artifact(_SAFE_QWEN_RESERVATION, identity)
            self.append("state_committed", **fields)
            self.state = copy.deepcopy(state)
            atomic_write(self.directory / "state.json", encoded(state) + b"\n")
        except BaseException:
            self._repair_write_failed = True
            raise
        finally:
            self._safe_qwen_identity_pending = None
        return copy.deepcopy(identity)

    def _require_repair_controller(self, controller):
        _repair_require(type(controller) is _RepairController and controller is self._repair_controller
                        and controller.store is self and not controller.closed and not self._repair_write_failed)
        if "repair_recovery" in self.state:
            _repair_require(encoded(controller.mode) == encoded(self.state["repair_recovery"]))

    @synchronized
    def _begin_repair_recovery(self, scheduler, unit_id, *, allocate=False, offline_recipe=None, verify=False):
        """Derive and persist opt-in before execution; selectors, never grant JSON."""
        from work_unit_scheduler import WorkUnitScheduler
        from supervision import _validated_store
        _repair_require(type(scheduler) is WorkUnitScheduler and scheduler.store is self
                        and isinstance(scheduler.supervisor, Supervisor) and scheduler.supervisor.store is self
                        and self._repair_controller is None and "repair_recovery" not in self.state
                        and "repair_authorizations" not in self.state and _SAFE_QWEN_IDENTITY not in self.state
                        and "safe_qwen_active_recovery" not in self.state)
        _repair_require(type(allocate) is bool)
        _repair_require(type(verify) is bool and (not verify or offline_recipe is not None))
        _validated_store(self)
        scheduler.supervisor.heartbeat()
        unit = self.state["work_units"]["units"][unit_id]
        _repair_require(unit["status"] == "READY" and not unit["attempts"]
                        and unit["spec"]["limits"]["max_attempts"] == 2)
        controller = self.state["supervision"]["controller"]
        _repair_require(controller["pid"] == os.getpid() and controller["identity"] == winprocess.identity(os.getpid())
                        and controller["boot"] == winprocess.boot_identity())
        started = scheduler.clock()
        mode = dict(schema_version=1, mode="ACTIVE", recovery_id=uuid.uuid4().hex, run_id=self.state["run_id"],
                    unit_id=unit_id, source_attempt_id=f"{unit_id}-attempt-1", spec_sha256=digest(unit["spec"]),
                    registry_sha256=digest(self.state["options"]["verifier_registry"]),
                    controller_session=_repair_session(self.state), budgets_sha256=digest(self.state["manager"]["budgets"]),
                    environment_sha256=digest(self.state["environment"]), baseline_fingerprint=snapshot(scheduler.repo)["fingerprint"],
                    started_clock=started, task_deadline=started + scheduler.remaining_task_budget(started))
        if allocate:
            mode.update(schema_version=2, operation="ALLOCATE_ONLY")
        if offline_recipe is not None:
            _repair_require(allocate)
            from offline_scripted_executor import _source_identity
            mode.update(schema_version=4 if verify else 3, operation="OFFLINE_VERIFY" if verify else "OFFLINE_DISPATCH", offline_recipe=copy.deepcopy(offline_recipe),
                        offline_sources=_source_identity())
        owner = _RepairController(self, scheduler, mode)
        self._repair_controller = owner
        state = copy.deepcopy(self.state)
        state.update(repair_recovery=mode, revision=state["revision"] + 1, updated_at=now())
        owner.record = ("state_committed", dict(state=state, transition=None,
                        repair_recovery={"event": "repair_recovery_enabled", "recovery_id": mode["recovery_id"]}))
        self._append_repair_controller_record(owner)
        return owner

    def _append_repair_controller_record(self, controller):
        """Write only the internally derived pending record of this capability."""
        self._require_repair_controller(controller)
        event, fields = controller.record
        try:
            if "state" in fields:
                validate_state(fields["state"])
                from supervision import validate_invocations
                validate_invocations(fields["state"], self.state, None)
            self.append(event, **fields)
            if "state" in fields:
                self.state = copy.deepcopy(fields["state"])
                atomic_write(self.directory / "state.json", encoded(self.state) + b"\n")
        except BaseException:
            self._repair_write_failed = True
            controller.closed = True
            raise
        finally:
            controller.record = None

    def _repair_native_evidence(self, attempt_id, observations):
        """One artifact/reference format for both trusted issuance controllers."""
        evidence = {}
        for vid, observation in observations.items():
            binding = copy.deepcopy(observation["binding"])
            refs = {}
            for event, value in binding["wal"].items():
                seq = value["sequence"] if type(value) is dict else value
                ref = {k: self.get_record(seq)[k] for k in ("sequence", "sha256")}
                _repair_require(type(value) is int or value == ref)
                refs[event] = ref
            binding["wal"] = refs
            binding["evidence"] = self.artifact(
                f"repair-evidence/{attempt_id}/{binding['invocation_id']}.json", observation["native"])
            evidence[vid] = binding
        return evidence

    def _make_granted_authorization(self, unit, decision, authorization_id, evidence):
        """Pure GRANTED v1 construction from already authenticated controller facts."""
        attempt = unit["attempts"][0]
        session, budget = _repair_context(self.state, unit, attempt)
        source = self.records[-1]
        _repair_require(source["event"] == "state_committed" and source["state"] == self.state)
        grant = dict(schema_version=1, state="GRANTED", run_id=self.state["run_id"], unit_id=unit["unit_id"],
                     source_attempt_id=attempt["attempt_id"], authorization_id=authorization_id,
                     candidate_fingerprint=attempt["candidate_snapshot"]["fingerprint"], spec_sha256=digest(unit["spec"]),
                     registry_sha256=digest(self.state["options"]["verifier_registry"]),
                     source_wal={k: source[k] for k in ("sequence", "sha256")}, native_evidence=evidence,
                     policy_b=dict(policy="B", eligible=True, decision="ALLOW", reason_code=decision.reason_code,
                                   failed_verifiers=sorted(decision.details["failed_verifiers"])),
                     budget=budget, recovery_session=session)
        return grant

    def _append_grant_record(self, controller, event, fields, *, ai=False):
        if not ai:
            controller.record = (event, fields)
            return self._append_repair_controller_record(controller)
        from safe_qwen_active_grant import require_owner
        require_owner(self, controller)
        _repair_require(controller.grant_writing and controller.grant_started and controller.pending is None)
        controller.pending = (event, fields)
        try:
            if "state" in fields:
                validate_state(fields["state"])
                from supervision import validate_invocations
                validate_invocations(fields["state"], self.state, None)
            self.append(event, **fields)
            if "state" in fields:
                self.state = copy.deepcopy(fields["state"])
                atomic_write(self.directory / "state.json", encoded(self.state) + b"\n")
        except BaseException:
            controller.persistence_failed()
            raise
        finally:
            controller.pending = None

    def _commit_granted_authorization(self, controller, grant, *, ai=False):
        """The existing adjacent decision/GRANTED transaction; no consumption."""
        policy = {k: grant[k] for k in ("schema_version", "run_id", "unit_id", "source_attempt_id",
                                       "source_wal", "policy_b", "recovery_session")}
        continuous = False
        try:
            self._append_grant_record(controller, _REPAIR_DECISION_EVENT, {"decision": policy}, ai=ai)
            grant["policy_wal"] = {k: self.records[-1][k] for k in ("sequence", "sha256")}
            state = copy.deepcopy(self.state)
            state.update(repair_authorizations={grant["authorization_id"]: grant},
                         revision=state["revision"] + 1, updated_at=now())
            fields = dict(state=state, transition=None,
                          repair_authorization={"event": _REPAIR_GRANT_EVENT, "authorization_id": grant["authorization_id"]})
            preview = dict(sequence=len(self.records) + 1, time=now(), event="state_committed",
                           previous=self.records[-1]["sha256"], **fields)
            preview["sha256"] = digest(preview)
            validate_state(state)
            _validate_repair_history([*self.records, preview], self.read_evidence)
            if ai:
                _repair_require(not controller.cancel_event.is_set() and time.monotonic() < min(
                    controller.identity[k] for k in ("deadline", "unit_deadline", "task_deadline")))
            self._append_grant_record(controller, "state_committed", fields, ai=ai)
            if ai and controller.allocation_selected:
                controller.grant_record = copy.deepcopy(self.records[-1])
                continuous = True
            if not ai and controller.mode.get("operation") in {"ALLOCATE_ONLY", "OFFLINE_DISPATCH", "OFFLINE_VERIFY"}:
                controller.grant_record = copy.deepcopy(self.records[-1])
                continuous = True
            return copy.deepcopy(grant)
        except BaseException:
            self._repair_write_failed = True
            raise
        finally:
            controller.closed = not continuous

    @synchronized
    def _issue_repair_grant(self, controller):
        """Existing offline issuance capability and semantics, unchanged."""
        self._require_repair_controller(controller)
        from supervision import _validated_store
        _validated_store(self)
        _repair_require("repair_authorizations" not in self.state)
        unit = self.state["work_units"]["units"][controller.mode["unit_id"]]
        attempt = unit["attempts"][0]
        _repair_require(attempt["repair_observation"]["observations"] == controller.observations)
        evidence = self._repair_native_evidence(attempt["attempt_id"], controller.observations)
        decision = controller.scheduler._fresh_repair_classification(controller)
        _repair_require(decision.is_allowed() and decision.reason_code == "ALLOW_VERIFIER_FAILURE")
        grant = self._make_granted_authorization(unit, decision, "repair-" + controller.mode["recovery_id"], evidence)
        _repair_require(grant["recovery_session"] == controller.mode["controller_session"])
        return self._commit_granted_authorization(controller, grant)

    @synchronized
    def _issue_ai_repair_grant(self, controller):
        """Preselected ACTIVE v4/v5 owner only; no payload or diagnostic input."""
        from safe_qwen_active_grant import require_owner
        from safe_qwen_active_eligibility import classify_for_grant, _guard
        require_owner(self, controller)
        _repair_require(not controller.grant_started and controller.pending is None)
        controller.grant_started = True
        try:
            _repair_require("repair_authorizations" not in self.state)
            decision = classify_for_grant(controller)
            if not decision.is_allowed():
                return None
            _repair_require(decision.reason_code == "ALLOW_VERIFIER_FAILURE")
            unit, _ = _guard(controller)
            attempt = unit["attempts"][0]
            _repair_require(attempt["outcome"] == "failed" and attempt["failure_classification"] == "verification_failed")
            evidence = self._repair_native_evidence(attempt["attempt_id"], controller.data["verification"]["observations"])
            _, budgets = _guard(controller)
            _repair_require(all(value >= 1 for value in budgets.values()))
            grant = self._make_granted_authorization(unit, decision, "repair-" + controller.identity["id"], evidence)
            _repair_require(grant["recovery_session"] == controller.identity["session"])
            controller.grant_writing = True
            return self._commit_granted_authorization(controller, grant, ai=True)
        finally:
            controller.closed = not (controller.allocation_selected and controller.grant_record is not None
                                     and not self._repair_write_failed)
            controller.grant_writing = False

    def _commit_repair_allocation(self, controller, grant, consumption, *, ai=False):
        consumed = copy.deepcopy(grant)
        consumed.update(schema_version=2, state="CONSUMED", consumption=consumption)
        state = copy.deepcopy(self.state)
        state.update(repair_authorizations={grant["authorization_id"]: consumed},
                     work_units=_allocated_repair_section(state["work_units"], grant, consumption),
                     revision=state["revision"] + 1, updated_at=consumption["allocated_at"])
        state["manager"]["runtime_seconds"] = consumption["task_runtime_seconds"]
        self._append_grant_record(controller, "state_committed", dict(state=state, transition=None,
            repair_authorization={"event": _REPAIR_CONSUME_EVENT, "authorization_id": grant["authorization_id"],
                                  "attempt_id": consumption["attempt_id"]}), ai=ai)
        return copy.deepcopy(consumed)

    @synchronized
    def _consume_ai_repair_grant(self, controller):
        """Live preselected v5 owner only. Atomic allocation, no external effects."""
        from safe_qwen_active_grant import require_allocation_owner, ai_binding
        from safe_qwen_active_eligibility import classify_for_grant, _guard
        require_allocation_owner(self, controller)
        _repair_require(controller.grant_record is not None and not controller.allocation_started
                        and controller.pending is None and controller.grant_started)
        controller.allocation_started = True
        continuous = False
        try:
            unit, budgets = _guard(controller)
            _repair_require(self.records[-1] == controller.grant_record and all(v >= 1 for v in budgets.values()))
            aid = "repair-" + controller.identity["id"]
            _repair_require(set(self.state["repair_authorizations"]) == {aid})
            grant = self.state["repair_authorizations"][aid]
            _repair_require(grant["state"] == "GRANTED" and grant["schema_version"] == 1)
            decision = classify_for_grant(controller)
            _repair_require(decision.is_allowed() and decision.reason_code == "ALLOW_VERIFIER_FAILURE")
            unit, budgets = _guard(controller)
            # The live guard checks the exact ACTIVE WAL-sealed candidate,
            # raw filesystem, initial Git/index identity and frozen scope. The
            # legacy Manager resume projection does not represent ACTIVE edits.
            check_previous_command(self.state)
            _repair_require(controller.scheduler.clock is time.monotonic
                            and controller.gateway.active_role is None and controller.gateway.active_model_child_id is None
                            and self.records[-1] == controller.grant_record and all(v >= 1 for v in budgets.values()))
            clock = controller.scheduler.clock()
            runtime = controller.scheduler.runtime(clock)
            i = controller.identity
            _repair_require(runtime >= self.state["manager"]["runtime_seconds"]
                            and min(i["deadline"], i["task_deadline"], grant["budget"]["unit_deadline"]) - clock >= 1)
            consumption = dict(schema_version=2, attempt_id=i["unit_id"] + "-attempt-2",
                grant_wal={k: controller.grant_record[k] for k in ("sequence", "sha256")}, granted_sha256=digest(grant),
                allocated_at=now(), checked_clock=clock, task_runtime_seconds=runtime,
                unit_elapsed_seconds=clock - i["started_clock"], task_deadline=i["task_deadline"],
                unit_deadline=grant["budget"]["unit_deadline"], controller_session=_repair_session(self.state),
                ai_binding=ai_binding(controller.data))
            controller.grant_writing = True
            consumed = self._commit_repair_allocation(controller, grant, consumption, ai=True)
            if controller.dispatch_selected:
                from safe_qwen_active_dispatch import require_owner
                require_owner(controller)
                controller.allocation_record = copy.deepcopy(self.records[-1])
                continuous = True
            return consumed
        except BaseException:
            controller.persistence_failed()
            raise
        finally:
            controller.closed = not continuous
            controller.grant_writing = False

    @synchronized
    def _consume_repair_grant(self, controller):
        """One continuous operation: atomic consumption/allocation, never launch.

        No grant selector or JSON payload is accepted. Issuance-only sessions
        close before this method, and loaded sessions never acquire a capability.
        """
        self._require_repair_controller(controller)
        _repair_require(controller.mode.get("operation") in {"ALLOCATE_ONLY", "OFFLINE_DISPATCH", "OFFLINE_VERIFY"}
                        and controller.grant_record is not None and not controller.allocation_started)
        controller.allocation_started = True
        continuous = False
        try:
            from supervision import _validated_store
            _validated_store(self)
            _repair_require(encoded(self.records[-1]) == encoded(controller.grant_record))
            aid = "repair-" + controller.mode["recovery_id"]
            _repair_require(set(self.state["repair_authorizations"]) == {aid})
            grant = self.state["repair_authorizations"][aid]
            _repair_require(grant["state"] == "GRANTED" and grant["schema_version"] == 1)
            unit = self.state["work_units"]["units"][grant["unit_id"]]
            _repair_require(unit["attempts"][0]["repair_observation"]["observations"] == controller.observations)
            decision = controller.scheduler._fresh_repair_classification(controller)
            _repair_require(decision.is_allowed() and decision.reason_code == "ALLOW_VERIFIER_FAILURE")
            self._require_repair_controller(controller)
            _validated_store(self)
            _repair_require(encoded(self.records[-1]) == encoded(controller.grant_record))
            clock = controller.scheduler.clock()
            runtime = controller.scheduler.runtime(clock)
            _repair_require(runtime >= self.state["manager"]["runtime_seconds"]
                            and min(controller.mode["task_deadline"], grant["budget"]["unit_deadline"]) - clock >= 1)
            c = dict(schema_version=1, attempt_id=grant["unit_id"] + "-attempt-2",
                     grant_wal={k: controller.grant_record[k] for k in ("sequence", "sha256")},
                     granted_sha256=digest(grant), allocated_at=now(), checked_clock=clock,
                     task_runtime_seconds=runtime, unit_elapsed_seconds=clock - controller.mode["started_clock"],
                     task_deadline=controller.mode["task_deadline"], unit_deadline=grant["budget"]["unit_deadline"],
                     controller_session=_repair_session(self.state))
            consumed = self._commit_repair_allocation(controller, grant, c)
            if controller.mode.get("operation") in {"OFFLINE_DISPATCH", "OFFLINE_VERIFY"}:
                controller.allocation_record = copy.deepcopy(self.records[-1])
                continuous = True
            return copy.deepcopy(consumed)
        finally:
            controller.closed = not continuous

    @synchronized
    def _advance_repair_dispatch(self, controller):
        """Only a registered live native adapter supplies lifecycle facts, never JSON."""
        from offline_scripted_executor import _ControllerDispatch
        self._require_repair_controller(controller)
        dispatch = controller.dispatch
        _repair_require(type(dispatch) is _ControllerDispatch and dispatch.controller is controller)
        try:
            state = dispatch.next_state()  # Reads retained native handles / internal observation, not returned data.
            controller.record = ("state_committed", dict(state=state, transition=None,
                repair_dispatch={"event": "repair_dispatch_" + state["repair_dispatch"]["phase"].lower(),
                                 "dispatch_id": state["repair_dispatch"]["dispatch_id"]}))
            self._append_repair_controller_record(controller)
            dispatch.last_record = copy.deepcopy(self.records[-1])
        except BaseException:
            controller.closed = True
            raise

    @synchronized
    def _persist_repair_verification(self, controller):
        """Persist only the live qualification sequence, never caller evidence."""
        self._require_repair_controller(controller)
        try:
            from supervision import _validated_store
            _validated_store(self)
            _repair_require(controller.mode.get("operation") == "OFFLINE_VERIFY"
                            and controller.verification_context is not None and controller.dispatch is not None
                            and controller.dispatch.last_record is not None)
            section = copy.deepcopy(controller.verification_section)
            unit = section["units"][controller.mode["unit_id"]]
            att = unit["attempts"][1]
            obs = att["repair_observation"]
            _repair_require(obs["observations"] == controller.verification_observations)
            clock = controller.scheduler.clock()
            stamp = now()
            unit["updated_at"] = stamp
            if att["completed_at"] is not None:
                att["completed_at"] = stamp
                att["timeout_info"] = {"elapsed_seconds": clock - att["started_at_clock"]}
                grant = self.state["repair_authorizations"][self.state["repair_dispatch"]["authorization_id"]]
                if clock >= min(controller.mode["task_deadline"], grant["budget"]["unit_deadline"]):
                    att.update(outcome="timeout", failure_classification="verifier_timeout")
                    unit["status"] = "UNIT_FAILED"
            import work_units
            work_units.seal_unit_state(unit)
            work_units._reseal_section(section)
            old = self.state.get("repair_verification")
            if old is None:
                controller.scheduler._guard_repair_verification(controller)
                d = self.state["repair_dispatch"]
                v = dict(schema_version=1, run_id=self.state["run_id"], unit_id=unit["unit_id"],
                         attempt_id=att["attempt_id"], dispatch_id=d["dispatch_id"],
                         dispatch_wal={k: controller.dispatch.last_record[k] for k in ("sequence", "sha256")},
                         dispatch_evidence=copy.deepcopy(d["evidence"]), controller_session=controller.mode["controller_session"],
                         candidate_fingerprint=obs["candidate_snapshot"]["fingerprint"],
                         candidate_fs_sha256=digest(obs["candidate_fs_snapshot"]),
                         tree_snapshot=copy.deepcopy(controller.verification_context["tree_snapshot"]),
                         tree_sha256=controller.dispatch.evidence["after_snapshot_sha256"])
            else:
                v = copy.deepcopy(old)
            v.update(phase="COMPLETE" if att["completed_at"] is not None else obs["phase"],
                     checked_clock=clock, task_runtime_seconds=controller.scheduler.runtime(clock),
                     updated_at=stamp, attempt_sha256=digest(att))
            state = copy.deepcopy(self.state)
            state.update(work_units=section, repair_verification=v, revision=state["revision"] + 1, updated_at=stamp)
            state["manager"]["runtime_seconds"] = v["task_runtime_seconds"]
            controller.record = ("state_committed", dict(state=state, transition=None,
                repair_verification={"event": "repair_verification_" + v["phase"].lower(), "dispatch_id": v["dispatch_id"]}))
            self._append_repair_controller_record(controller)
        except BaseException:
            controller.closed = True
            raise

    @synchronized
    def append(self, event, **fields):
        if self._repair_write_failed:
            raise DurableError("Uncertain repair history write; reload required")
        owner = self._repair_controller
        owned = (type(owner) is _RepairController and not owner.closed and owner.record is not None
                 and encoded(owner.record) == encoded((event, fields)))
        identity_owned = (self._safe_qwen_identity_pending is not None
                          and encoded(self._safe_qwen_identity_pending) == encoded((event, fields)))
        from safe_qwen_active_recovery import KEY as active_key, MARKER as active_marker, RESERVATION as active_reservation, owns_record, validate_history as validate_active_history
        active_owned = owns_record(self, event, fields)
        repair_owned = owned
        if active_owned and getattr(self._safe_qwen_active_controller, "grant_writing", False):
            from safe_qwen_active_grant import owns_record as owns_ai_grant_record
            repair_owned = owns_ai_grant_record(self, event, fields)
        if (active_key in fields or active_marker in fields) and not active_owned:
            raise DurableError("ACTIVE Attempt 1 events require the live controller")
        if (_SAFE_QWEN_IDENTITY in fields or (type(event) is str and event.startswith("safe_qwen_recovery_"))) and not identity_owned:
            raise DurableError("Recovery identity can only be derived by the controller selection operation")
        if ((type(event) is str and event.startswith("repair_")) or "repair_authorization" in fields
                or "repair_recovery" in fields or "repair_dispatch" in fields or "repair_verification" in fields) and not repair_owned:
            raise DurableError("Repair authorization issuance/consumption is not enabled")
        transition = fields.get("transition")
        transition_event = transition.get("event") if type(transition) is dict else None
        if type(transition_event) is str and transition_event.startswith("repair_"):
            raise DurableError("Repair lifecycle transitions are not enabled")
        if self.state and (active_key in self.state or _SAFE_QWEN_IDENTITY in self.state or "repair_authorizations" in self.state or "repair_recovery" in self.state):
            if self._repair_write_failed:
                raise DurableError("Uncertain repair history write; reload required")
            from supervision import _validated_store
            if not owned and not identity_owned and not active_owned:
                _validated_store(self)
        if event == "state_committed":
            prior = next((r["state"] for r in reversed(self.records) if r["event"] == event), {})
            proposed = fields.get("state", {})
            active_controller = getattr(self, "_safe_qwen_active_controller", None)
            if active_controller is not None and not active_owned:
                active_controller.guard_state_commit(proposed, fields.get("transition"))
            if not active_owned and encoded(proposed.get(active_key)) != encoded(prior.get(active_key)):
                raise DurableError("Ordinary append cannot create or change ACTIVE recovery")
            if not identity_owned and encoded(proposed.get(_SAFE_QWEN_IDENTITY)) != encoded(prior.get(_SAFE_QWEN_IDENTITY)):
                raise DurableError("Ordinary WAL append cannot select, change or remove recovery identity")
            validate_safe_qwen_recovery_identity(proposed)
            if not repair_owned and any(encoded(proposed.get(key)) != encoded(prior.get(key))
                                 for key in ("repair_authorizations", "repair_recovery", "repair_dispatch", "repair_verification")):
                raise DurableError("Ordinary WAL append cannot issue, change or remove repair authorization")
            validate_repair_authorizations(proposed)
        record = {"sequence": len(self.records) + 1, "time": now(), "event": event,
                  "previous": self.records[-1]["sha256"] if self.records else None, **fields}
        record["sha256"] = digest(record)
        repair_state = fields.get("state", self.state) or {}
        if active_key in repair_state or active_marker in fields or (self.directory / active_reservation).exists():
            validate_active_history([*self.records, record], self.read_evidence,
                                    reservation_exists=(self.directory / active_reservation).exists())
        if (_SAFE_QWEN_IDENTITY in repair_state or _SAFE_QWEN_IDENTITY in fields
                or (self.directory / _SAFE_QWEN_RESERVATION).exists()):
            _validate_safe_qwen_identity_history([*self.records, record], self.read_evidence,
                                                 reservation_exists=(self.directory / _SAFE_QWEN_RESERVATION).exists())
        if (event != _REPAIR_DECISION_EVENT and
                ("repair_recovery" in repair_state or "repair_authorizations" in repair_state)):
            _validate_repair_history([*self.records, record], self.read_evidence)
        try:
            with (self.directory / "events.jsonl").open("ab") as stream:
                stream.write(encoded(record) + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            if active_owned or identity_owned or (self.state and (active_key in self.state or _SAFE_QWEN_IDENTITY in self.state
                                                  or "repair_authorizations" in self.state or "repair_recovery" in self.state)):
                self._repair_write_failed = True
            raise
        self.records.append(record)
        return record["sequence"]

    @synchronized
    def append_record(self, event, **fields):
        seq = self.append(event, **fields)
        return copy.deepcopy(self.records[seq - 1])

    @synchronized
    def get_record(self, sequence: int):
        if isinstance(sequence, int) and 1 <= sequence <= len(self.records):
            return copy.deepcopy(self.records[sequence - 1])
        return None

    def is_terminal_accepted(self) -> bool:
        return self._terminal_accepted or bool(self.state and self.state.get("terminal_acceptance"))

    def _check_orphan_seals(self):
        acceptance_dir = self.directory / "acceptance"
        if not acceptance_dir.exists():
            self._orphan_seals = []
            return
        seals = [p.name for p in acceptance_dir.glob("*.json")]
        if not seals:
            self._orphan_seals = []
            return
        if self.is_terminal_accepted() and self.state and self.state.get("terminal_acceptance"):
            allowed_name = Path(self.state["terminal_acceptance"]["seal_path"]).name
            self._orphan_seals = [s for s in seals if s != allowed_name]
        else:
            self._orphan_seals = seals

    @synchronized
    def commit(self, status=None, pre_write_guard=None, **fields):
        if "safe_qwen_active_recovery" in fields or "safe_qwen_active_event" in fields:
            raise DurableError("Ordinary commit cannot create ACTIVE recovery")
        if _SAFE_QWEN_IDENTITY in fields:
            raise DurableError("Ordinary commit cannot select recovery identity")
        if any(key in fields for key in ("repair_authorizations", "repair_authorization", "repair_recovery", "repair_dispatch", "repair_verification")):
            raise DurableError("Repair authorization issuance/consumption is not enabled")
        if self.is_terminal_accepted():
            raise DurableError("Terminal transaction already accepted; further state commits rejected")
        if "terminal_acceptance" in fields:
            raise DurableError("Ordinary commit cannot fabricate terminal acceptance; use commit_terminal")
        self._check_orphan_seals()
        if self._orphan_seals:
            raise DurableError(f"Unresolved orphan acceptance seal(s) detected: {self._orphan_seals}; continuation blocked")
        state = copy.deepcopy(self.state)
        # Published state and WAL records must not share a caller's mutable
        # scheduler/Manager payload between its transition and sealing steps.
        state.update(copy.deepcopy(fields))
        state.update(status=status or state["status"], updated_at=now(), revision=state["revision"] + 1)
        if "reviewer_approval" not in fields:
            state.pop("reviewer_approval", None)
        validate_state(state)
        if pre_write_guard is not None:
            pre_write_guard(state)
            if state.get("terminal_acceptance") is not None:
                raise DurableError("Ordinary commit cannot inject terminal acceptance metadata in pre-write guard")
            validate_state(state)
        from supervision import validate_invocations
        validate_invocations(state, self.state, fields.get("supervision_event"))
        # The flushed WAL is the commit point. state.json is an atomic materialized view.
        rec_fields = {}
        if "reviewer_approval" in fields:
            rec_fields["reviewer_approval"] = copy.deepcopy(fields["reviewer_approval"])
        try:
            seq = self.append(
                "state_committed",
                state=state,
                transition=fields.get("supervision_event"),
                **rec_fields,
            )
        except BaseException:
            if "safe_qwen_active_recovery" in state or _SAFE_QWEN_IDENTITY in state or "repair_recovery" in state or "repair_authorizations" in state:
                self._repair_write_failed = True
            raise
        committed_record = copy.deepcopy(self.records[seq - 1]) if isinstance(seq, int) and 1 <= seq <= len(self.records) else copy.deepcopy(self.records[-1])
        self.state = state
        try:
            atomic_write(self.directory / "state.json", encoded(state) + b"\n")
        except BaseException:
            if "safe_qwen_active_recovery" in state or _SAFE_QWEN_IDENTITY in state or "repair_authorizations" in state or "repair_recovery" in state:
                self._repair_write_failed = True
            raise
        return copy.deepcopy(committed_record)


    def exclusive_artifact(self, relative: str, data: dict):
        target = self.directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = encoded(data) + b"\n"
        try:
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            fd = os.open(target, flags, 0o600)
        except FileExistsError as exc:
            raise DurableError(
                f"Terminal acceptance seal already exists at {relative}; exclusive creation refused"
            ) from exc
        except OSError as exc:
            raise DurableError(f"Cannot create seal artifact exclusively at {relative}: {exc}") from exc
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        except Exception as exc:
            raise DurableError(f"Failed to flush/fsync seal artifact at {relative}: {exc}") from exc
        return {"path": relative, "sha256": digest(data)}

    @synchronized
    def commit_terminal(self, acceptance_id: str, controller=None,
                        status: str = "COMPLETED", pre_write_guard=None, **fields):
        """Durably record authoritative terminal acceptance in the WAL.

        ``controller`` must be the instance's own controller token or a registered
        authority token from ``register_controller_authority()``. An arbitrary
        caller-supplied string or dict is not sufficient authority.
        """
        if ("safe_qwen_active_recovery" in self.state or "safe_qwen_active_recovery" in fields
                or _SAFE_QWEN_IDENTITY in self.state or _SAFE_QWEN_IDENTITY in fields
                or "repair_authorizations" in self.state or "repair_recovery" in self.state or "repair_authorizations" in fields
                or "repair_recovery" in fields
                or "repair_authorization" in fields):
            raise DurableError("Dormant repair authorization cannot produce terminal acceptance")
        if self.is_terminal_accepted():
            raise DurableError("Terminal transaction already accepted; further state commits rejected")
        if not isinstance(acceptance_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", acceptance_id):
            raise DurableError(f"Invalid acceptance ID: {acceptance_id}")

        # --- Defect 5: torn WAL prevents automatic second attempt ---
        if self.tail:
            raise DurableError("Torn event in WAL ledger must be repaired before commit")
        wal_path = self.directory / "events.jsonl"
        if wal_path.exists():
            wal_bytes = wal_path.read_bytes()
            if wal_bytes and not wal_bytes.endswith(b"\n"):
                raise DurableError("Torn or incomplete WAL detected; cannot commit terminal transaction")

        seal_relative_path = f"acceptance/{acceptance_id}.json"
        acceptance_path = self.directory / seal_relative_path
        if acceptance_path.exists():
            raise DurableError(
                f"Acceptance seal already exists: {acceptance_path}; terminal acceptance cannot be overwritten"
            )

        self._check_orphan_seals()
        if self._orphan_seals:
            raise DurableError(
                f"Unresolved orphan acceptance seal(s) detected: {sorted(self._orphan_seals)}; terminal acceptance blocked"
            )

        # --- Defect 7: bind controller to Store instance authority ---
        if controller is None:
            controller = self._controller_token
        else:
            controller = copy.deepcopy(controller)
        if not self._is_authorized_controller(controller):
            raise DurableError(
                "Terminal acceptance controller is not registered for this Store instance; "
                "use register_controller_authority() or the instance token."
            )

        # Snapshot WAL position and Store state BEFORE the pre-write guard runs.
        parent_revision = self.state["revision"]
        parent_state = copy.deepcopy(self.state)
        parent_canonical_bytes = encoded(self.state)
        target_revision = parent_revision + 1
        expected_wal_sequence = len(self.records) + 1
        previous_record_hash = self.records[-1]["sha256"] if self.records else None

        state = copy.deepcopy(self.state)
        state.update(copy.deepcopy(fields))
        state.update(status=status or state["status"], updated_at=now(), revision=target_revision)
        state["terminal_acceptance"] = {
            "schema_version": TERMINAL_ACCEPTANCE_SCHEMA_VERSION,
            "acceptance_id": acceptance_id,
            "controller": copy.deepcopy(controller),
            "seal_path": seal_relative_path,
        }
        validate_state(state)

        # Invariant 2: Snapshot complete proposed terminal state BEFORE executing pre-write guard
        proposed_snapshot = copy.deepcopy(state)
        proposed_canonical_bytes = encoded(state)

        # --- Defect 6 & Post-Guard Revalidation: re-check Store state, revision, and bindings ---
        if pre_write_guard is not None:
            pre_write_guard(state)
            if self.state["revision"] != parent_revision or encoded(self.state) != parent_canonical_bytes:
                raise DurableError(
                    "Pre-write guard mutated WAL position, Store parent revision, or state; terminal commit aborted"
                )
            if encoded(state) != proposed_canonical_bytes:
                raise DurableError(
                    "Pre-write guard mutated proposed terminal state; terminal commit aborted"
                )
            if state.get("run_id") != self.directory.name:
                raise DurableError(
                    "Pre-write guard mutated run identity; terminal commit aborted"
                )
            if type(state["revision"]) is not int or state["revision"] != target_revision:
                raise DurableError(
                    "Pre-write guard mutated target revision; terminal commit aborted"
                )
            post_guard_sequence = len(self.records) + 1
            post_guard_prev = self.records[-1]["sha256"] if self.records else None
            if post_guard_sequence != expected_wal_sequence or post_guard_prev != previous_record_hash:
                raise DurableError(
                    "Pre-write guard mutated WAL position; terminal commit aborted to prevent stale sequence facts"
                )
            term = state.get("terminal_acceptance")
            if not isinstance(term, dict) or term.get("acceptance_id") != acceptance_id:
                raise DurableError(
                    "Pre-write guard mutated terminal acceptance ID; terminal commit aborted"
                )
            if encoded(term.get("controller")) != encoded(controller):
                raise DurableError(
                    "Pre-write guard mutated terminal controller binding; terminal commit aborted"
                )
            if term.get("seal_path") != seal_relative_path:
                raise DurableError(
                    "Pre-write guard mutated terminal seal path; terminal commit aborted"
                )
            validate_terminal_acceptance(term, self.directory.name)
            validate_state(state)

        state_digest = digest(state)

        seal = {
            "schema_version": TERMINAL_ACCEPTANCE_SCHEMA_VERSION,
            "acceptance_id": acceptance_id,
            "run_id": self.directory.name,
            "controller": copy.deepcopy(controller),
            "parent_revision": parent_revision,
            "target_revision": target_revision,
            "wal_sequence": expected_wal_sequence,
            "previous_record_hash": previous_record_hash,
            "state_digest": state_digest,
            "accepted_state": copy.deepcopy(state),
            "time": now(),
        }

        # --- Defect 4 & 5: true exclusive creation, flush and fsync ---
        seal_ref = self.exclusive_artifact(seal_relative_path, seal)

        append_error = None
        try:
            self.append(
                "state_committed",
                state=state,
                acceptance_id=acceptance_id,
                controller=copy.deepcopy(controller),
                terminal=True,
                terminal_seal=seal_ref,
                transition=fields.get("supervision_event"),
            )
        except Exception as exc:
            append_error = exc

        if append_error is not None:
            reconciled = self._reconcile_terminal_wal(
                acceptance_id=acceptance_id,
                controller=controller,
                expected_wal_sequence=expected_wal_sequence,
                previous_record_hash=previous_record_hash,
                target_revision=target_revision,
                expected_state=state,
                state_digest=state_digest,
                seal_ref=seal_ref,
            )
            if not reconciled:
                raise DurableError(
                    f"Terminal acceptance failed before durable WAL commit: {append_error}"
                ) from append_error

        self.state = state
        self._terminal_accepted = True

        try:
            atomic_write(self.directory / "state.json", encoded(state) + b"\n")
        except Exception:
            pass

        receipt = {
            "status": state["status"],
            "acceptance_id": acceptance_id,
            "controller": copy.deepcopy(controller),
            "revision": target_revision,
            "wal_sequence": expected_wal_sequence,
            "seal_reference": copy.deepcopy(seal_ref),
            "state": copy.deepcopy(state),
        }
        return copy.deepcopy(receipt)

    accept_terminal = commit_terminal

    def register_controller_authority(self, authority: str | dict | None = None) -> str | dict:
        """Register an authorized controller token or identity bound to this Store instance."""
        if authority is None:
            token = f"controller-{uuid.uuid4().hex}"
            self._authorized_controllers.add(token)
            return token
        if not isinstance(authority, (str, dict)) or not authority:
            raise DurableError("Invalid controller authority specification")
        auth_copy = copy.deepcopy(authority)
        token = encoded(auth_copy).decode("utf-8") if isinstance(auth_copy, dict) else str(auth_copy)
        self._authorized_controllers.add(token)
        return copy.deepcopy(auth_copy)

    def _is_authorized_controller(self, value) -> bool:
        if value is None:
            return True
        try:
            token = encoded(value).decode("utf-8") if isinstance(value, dict) else str(value)
            return token in self._authorized_controllers
        except Exception:
            return False


    def _reconcile_terminal_wal(self, acceptance_id: str, controller, expected_wal_sequence: int,
                                previous_record_hash, target_revision: int, expected_state: dict,
                                state_digest: str, seal_ref: dict) -> bool:
        wal_path = self.directory / "events.jsonl"
        if not wal_path.exists():
            return False
        try:
            with wal_path.open("r+b") as wal_stream:
                fd = wal_stream.fileno()
                raw = wal_stream.read()

                lines = raw.split(b"\n")
                tail = lines.pop()
                if tail:
                    raise DurableError("Torn or incomplete WAL detected during terminal reconciliation")

                records = []
                for line in lines:
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except Exception as exc:
                        raise DurableError("Corrupt JSON in WAL during terminal reconciliation") from exc

                    checksum = record.pop("sha256", None)
                    if checksum != digest(record):
                        raise DurableError("Corrupt checksum in WAL during terminal reconciliation")
                    record["sha256"] = checksum

                    if record["sequence"] != len(records) + 1:
                        raise DurableError("Sequence mismatch in WAL during terminal reconciliation")
                    expected_prev = records[-1]["sha256"] if records else None
                    if record["previous"] != expected_prev:
                        raise DurableError("Hash chain broken in WAL during terminal reconciliation")
                    records.append(record)

                if len(records) < expected_wal_sequence:
                    return False

                target_record = records[expected_wal_sequence - 1]
                seal_path = self.directory / f"acceptance/{acceptance_id}.json"
                if not seal_path.exists():
                    raise DurableError("Missing terminal acceptance seal artifact during reconciliation")
                try:
                    raw_seal = seal_path.read_bytes()
                    seal_data = json.loads(raw_seal.decode("utf-8"))
                except Exception as exc:
                    raise DurableError(f"Unreadable terminal acceptance seal artifact: {exc}") from exc

                validate_terminal_transaction(
                    records=records,
                    wal_seq=expected_wal_sequence,
                    seal_data=seal_data,
                    wal_record=target_record,
                    state=expected_state,
                    run_id=self.directory.name,
                    seal_ref=seal_ref,
                    run_dir=self.directory,
                )

                # Same-file identity & content stability verification before reflush
                wal_stat_now = os.fstat(fd)
                try:
                    wal_stat_disk = os.stat(wal_path)
                except Exception as exc:
                    raise DurableError(f"Cannot stat WAL path on disk during terminal reconciliation: {exc}") from exc
                if (wal_stat_now.st_ino == 0 or wal_stat_disk.st_ino == 0 or
                    (wal_stat_now.st_ino, wal_stat_now.st_dev) != (wal_stat_disk.st_ino, wal_stat_disk.st_dev)):
                    raise DurableError("WAL file identity changed during terminal reconciliation; reconciliation aborted")
                if wal_stat_now.st_size != len(raw):
                    raise DurableError("WAL content instability detected during terminal reconciliation")

                try:
                    os.fsync(fd)
                except Exception as exc:
                    raise DurableError(
                        f"Cannot confirm WAL durability during terminal reconciliation: {exc}"
                    ) from exc

                # Re-verify identity & content stability after reflush
                wal_stat_after = os.fstat(fd)
                try:
                    wal_stat_disk_after = os.stat(wal_path)
                except Exception as exc:
                    raise DurableError(f"Cannot stat WAL path on disk after flush during terminal reconciliation: {exc}") from exc
                if (wal_stat_after.st_ino == 0 or wal_stat_disk_after.st_ino == 0 or
                    (wal_stat_after.st_ino, wal_stat_after.st_dev) != (wal_stat_disk_after.st_ino, wal_stat_disk_after.st_dev)):
                    raise DurableError("WAL file identity changed during terminal reconciliation; reconciliation aborted")
                if wal_stat_after.st_size != len(raw):
                    raise DurableError("WAL content instability detected during terminal reconciliation")

                # Invariant 1: Read complete bytes AGAIN through same descriptor and require byte-for-byte equality
                wal_stream.seek(0)
                raw_after = wal_stream.read()
                if raw_after != raw:
                    raise DurableError("WAL bytes changed during reconciliation fsync; terminal reconciliation aborted")

                self.records = records
                return True
        except DurableError:
            raise
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise DurableError(f"Cannot reconcile terminal WAL safely: {type(exc).__name__}") from exc

    def create(self, repo, task, commands, config, reviewer=False, critic=False, criteria=None, config_path=None,
               extra=None, executor=None):
        if not commands or not all(safe_command(c) for c in commands):
            raise DurableError("At least one allowlisted verification command is required")
        if critic and ((not reviewer and not extra) or not config.get("roles", {}).get("critic")):
            raise DurableError("Advisory critic requires a configured critic and senior reviewer")
        repo.require_clean()
        baseline = snapshot(repo)
        self.directory.mkdir(parents=True, exist_ok=False)
        (self.directory / "rounds").mkdir()
        (self.directory / "checkpoints").mkdir()
        atomic_write(self.directory / "original-task.txt", task.encode("utf-8"))
        self.state = {"schema_version": SCHEMA_VERSION, "run_id": self.directory.name,
                      "created_at": now(), "updated_at": now(), "revision": 0, "status": "CREATED",
                      "original_task": task, "acceptance_criteria": criteria or [],
                      "target_repository": str(repo.root), "original_git_head": baseline["head"],
                      "current_git_head": baseline["head"], "baseline": baseline, "round_number": 0,
                      "verified_progress": [], "remaining_work": [task], "last_verified_checkpoint": None,
                      "current_step": "not_started", "last_failure_evidence": None, "history": [],
                      "evidence": {"verifier": [], "critic": [], "reviewer": []},
                      "models": {role: model_identity(config, alias) for role, alias in config["roles"].items()},
                      "recovery": {"resume_count": 0}, "unverified_work": {"observed": baseline},
                      "active_command": None,
                      "supervision": initial_supervision(),
                      "environment": environment.capture(config, repo.root, commands, config_path, executor=executor),
                      "environment_config_path": str(Path(config_path).resolve()) if config_path else None,
                      "options": {"commands": commands, "reviewer": reviewer, "critic": critic,
                                  "config": copy.deepcopy(config)}}
        if extra:
            self.state.update(copy.deepcopy(extra))
        self.commit()

    def load(self):
        try:
            try:
                cached = json.loads((self.directory / "state.json").read_text(encoding="utf-8"))
            except FileNotFoundError:
                cached = None  # Death between the initial WAL commit and first cache replacement.
            else:
                validate_state(cached)

            wal_path = self.directory / "events.jsonl"
            with wal_path.open("r+b") as wal_stream:
                fd = wal_stream.fileno()
                raw = wal_stream.read()

                lines = raw.split(b"\n")
                candidate_tail = lines.pop()  # An unterminated final record is never committed.
                records = []
                commits = []
                for line in lines:
                    record = json.loads(line)
                    checksum = record.pop("sha256")
                    if (checksum != digest(record) or record["sequence"] != len(records) + 1
                            or record["previous"] != (records[-1]["sha256"] if records else None)):
                        raise DurableError("Corrupt durable event ledger")
                    record["sha256"] = checksum
                    records.append(record)
                    if record["event"] == "state_committed":
                        state = record["state"]
                        validate_state(state)
                        from supervision import validate_invocations
                        validate_invocations(state, commits[-1] if commits else {}, record.get("transition"))
                        if state["revision"] != len(commits) + 1 or state["run_id"] != self.directory.name:
                            raise DurableError("Inconsistent durable state ledger")
                        commits.append(state)
                if (not commits or (cached is not None and (type(cached.get("revision")) is not int
                        or cached["revision"] > len(commits)
                        or encoded(cached) != encoded(commits[cached["revision"] - 1])))):
                    raise DurableError("State and event evidence disagree")

                candidate_state = commits[-1]
                from safe_qwen_active_recovery import validate_history as validate_active_history, RESERVATION as active_reservation
                validate_active_history(records, self.read_evidence, candidate_tail,
                                        reservation_exists=(self.directory / active_reservation).exists())
                _validate_safe_qwen_identity_history(records, self.read_evidence, candidate_tail,
                                                     reservation_exists=(self.directory / _SAFE_QWEN_RESERVATION).exists())
                _validate_repair_history(records, self.read_evidence, candidate_tail)
                if (self.directory / "original-task.txt").read_bytes().decode("utf-8") != candidate_state["original_task"]:
                    raise DurableError("Original task artifact does not match state")
                checkpoint = candidate_state["last_verified_checkpoint"]
                if checkpoint:
                    validate_checkpoint_evidence(self, self.read_evidence(checkpoint["reference"]), state=candidate_state)

                if candidate_state.get("terminal_acceptance"):
                    term = candidate_state["terminal_acceptance"]
                    validate_terminal_acceptance(term, self.directory.name)
                    canonical_rel = f"acceptance/{term['acceptance_id']}.json"
                    if term.get("seal_path") != canonical_rel:
                        raise DurableError(f"Non-canonical seal path in state: {term.get('seal_path')}")
                    seal_path = self.directory / canonical_rel
                    if not seal_path.exists():
                        raise DurableError("Missing terminal acceptance seal artifact")
                    try:
                        raw_bytes = seal_path.read_bytes()
                        seal_data = json.loads(raw_bytes.decode("utf-8"))
                    except Exception as exc:
                        raise DurableError("Unreadable terminal acceptance seal artifact") from exc

                    wal_seq = seal_data.get("wal_sequence")
                    if type(wal_seq) is not int or wal_seq <= 0 or wal_seq > len(records):
                        raise DurableError("Invalid WAL sequence in terminal acceptance seal")
                    wal_record = records[wal_seq - 1]
                    validate_terminal_transaction(
                        records=records,
                        wal_seq=wal_seq,
                        seal_data=seal_data,
                        wal_record=wal_record,
                        state=candidate_state,
                        run_id=self.directory.name,
                        run_dir=self.directory,
                    )

                    # Same-file identity & content stability verification before reflush
                    wal_stat_now = os.fstat(fd)
                    try:
                        wal_stat_disk = os.stat(wal_path)
                    except Exception as exc:
                        raise DurableError(f"Cannot stat WAL path on disk during terminal acceptance load: {exc}") from exc
                    if (wal_stat_now.st_ino == 0 or wal_stat_disk.st_ino == 0 or
                        (wal_stat_now.st_ino, wal_stat_now.st_dev) != (wal_stat_disk.st_ino, wal_stat_disk.st_dev)):
                        raise DurableError("WAL file identity changed or cannot be established; terminal acceptance aborted")
                    if wal_stat_now.st_size != len(raw):
                        raise DurableError("WAL content instability detected during terminal validation; terminal acceptance aborted")

                    # Reflush the SAME opened file descriptor
                    try:
                        os.fsync(fd)
                    except Exception as exc:
                        raise DurableError(f"Cannot confirm WAL durability during terminal acceptance load: {exc}") from exc

                    # Re-verify identity & content stability after reflush
                    wal_stat_after = os.fstat(fd)
                    try:
                        wal_stat_disk_after = os.stat(wal_path)
                    except Exception as exc:
                        raise DurableError(f"Cannot stat WAL path on disk after flush during terminal acceptance load: {exc}") from exc
                    if (wal_stat_after.st_ino == 0 or wal_stat_disk_after.st_ino == 0 or
                        (wal_stat_after.st_ino, wal_stat_after.st_dev) != (wal_stat_disk_after.st_ino, wal_stat_disk_after.st_dev)):
                        raise DurableError("WAL file identity changed or cannot be established; terminal acceptance aborted")
                    if wal_stat_after.st_size != len(raw):
                        raise DurableError("WAL content instability detected during terminal validation; terminal acceptance aborted")

                    # Invariant 1: Read complete bytes AGAIN through same descriptor and require byte-for-byte equality
                    wal_stream.seek(0)
                    raw_after = wal_stream.read()
                    if raw_after != raw:
                        raise DurableError("WAL bytes changed during fsync/reflush; terminal acceptance aborted")

                    candidate_terminal_accepted = True
                else:
                    candidate_terminal_accepted = False
                    for r in records:
                        if r.get("event") == "state_committed" and r.get("terminal") is True:
                            raise DurableError("Terminal WAL record missing terminal_acceptance in state")

            # Check orphan seals with candidate state
            seals_dir = self.directory / "acceptance"
            if seals_dir.exists():
                seals = [p.name for p in seals_dir.glob("*.json")]
            else:
                seals = []
            if candidate_terminal_accepted and candidate_state.get("terminal_acceptance"):
                allowed_name = Path(candidate_state["terminal_acceptance"]["seal_path"]).name
                orphan_seals = [s for s in seals if s != allowed_name]
            else:
                orphan_seals = seals

            # Atomically publish candidate state to Store instance only after all checks passed
            self.tail = candidate_tail
            self.records = records
            self.state = candidate_state
            self._terminal_accepted = candidate_terminal_accepted
            self._orphan_seals = orphan_seals
            self._repair_write_failed = False
            self._repair_controller = None
            self._safe_qwen_identity_pending = None
            self._safe_qwen_active_controller = None

            return copy.deepcopy(self.state)

        except DurableError:
            raise
        except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
            raise DurableError(f"Cannot load durable run safely: {type(exc).__name__}") from exc

    def repair_cache(self):
        if self.tail:
            # Preserve crash debris before removing only the incomplete append.
            atomic_write(self.directory / f"torn-event-{uuid.uuid4().hex}.bin", self.tail)
            path = self.directory / "events.jsonl"
            with path.open("r+b") as stream:
                stream.truncate(path.stat().st_size - len(self.tail))
                stream.flush()
                os.fsync(stream.fileno())
            self.tail = b""
            self.append("torn_event_preserved")
        atomic_write(self.directory / "state.json", encoded(self.state) + b"\n")

    def artifact(self, relative, data):
        atomic_write(self.directory / relative, encoded(data) + b"\n")
        return {"path": relative, "sha256": digest(data)}

    def read_evidence(self, reference):
        try:
            if (not isinstance(reference, dict)
                    or not isinstance(reference.get("path"), str)
                    or not isinstance(reference.get("sha256"), str)):
                raise DurableError("Missing or invalid durable evidence reference")
            path = (self.directory / reference["path"]).resolve()
            if not path.is_relative_to(self.directory) or path == self.directory:
                raise DurableError("Evidence path escapes run directory")
            value = json.loads(path.read_text(encoding="utf-8"))
            if digest(value) != reference["sha256"]:
                raise DurableError("Checkpoint/evidence checksum mismatch")
            return value
        except (OSError, ValueError, KeyError, TypeError) as exc:
            if isinstance(exc, DurableError):
                raise
            raise DurableError(f"Cannot read durable evidence safely: {type(exc).__name__}") from exc

    def read_review_evidence(self, result):
        if not isinstance(result, dict):
            raise DurableError("Missing durable review result")
        payload = dict(result)
        reference = payload.pop("artifact_ref", None)
        if self.read_evidence(reference) != payload:
            raise DurableError("Review result does not match durable evidence")
        return reference


class DurableLog(EventLog):
    """Do not persist prompts, plans, summaries, raw model errors or reasoning telemetry."""
    def __init__(self, store, progress=False):
        super().__init__(store.directory / "events.jsonl", progress, store.state["options"]["config"])
        self.store = store
        self.check_ids = {tuple(argv): f"check-{i}" for i, argv in enumerate(store.state["options"].get("commands", []), 1)}
        self.check_ids[("git", "diff", "--check")] = "diff-check"
        self.set_context(run_id=store.state.get("run_id"), round=store.state["round_number"])

    def emit(self, event, **fields):
        if event == "plan":
            fields = {"omitted": "model_plan"}
        else:
            fields = {k: v for k, v in fields.items()
                      if k not in {"text", "summary", "findings", "error", "reason", "usage", "timings"}}
        try:
            self.store.append(event, round_number=self.store.state["round_number"], **fields)
        except OSError as exc:
            raise RuntimeError("Durable evidence persistence failed") from exc
        if event == "retry":
            history = self.store.state["history"] + [{"round": self.store.state["round_number"],
                                                     "retry": fields["count"], "time": now()}]
            self.store.commit(history=history)
        if self.progress:
            # Display uses existing progress formatting but never the discarded free text.
            if event not in {"tool_error"}:
                self.show_progress(event, fields)


class DurableRepository(Repository):
    def __init__(self, path, config, log, store):
        self.store = store
        self.supervisor = Supervisor(store)
        super().__init__(path, config, log)

    def path(self, relative):
        path = super().path(relative)
        if path.resolve().is_relative_to(self.store.directory):
            raise DurableError("Agent tools cannot access durable controller evidence")
        return path

    def write(self, relative, content):
        super().write(relative, content)
        try:
            observed = snapshot(self)
            self.store.commit(current_git_head=observed["head"],
                              unverified_work={"observed": observed, "step": "file_written", "time": now()})
        except OSError as exc:
            raise RuntimeError("Durable file observation persistence failed") from exc

    def execute(self, argv, timeout=None):
        with self.log.context_for(check_id=self.log.check_ids.get(tuple(argv))):
            return self._execute(argv, timeout)

    def _execute(self, argv, timeout=None):
        if not safe_command(argv):
            raise ValueError("Command is outside the safe tool allowlist")
        command = list(argv)
        if command[0].lower().removesuffix(".exe") == "git":
            command = ["git", "-c", f"safe.directory={self.root}", "-c", "diff.external=", *argv[1:]]
            if "diff" in command:
                position = command.index("diff") + 1
                command[position:position] = ["--no-ext-diff", "--no-textconv"]
        executable = shutil.which(command[0])
        if not executable:
            raise DurableError("Verification executable unavailable")
        command[0] = executable
        timeout = command_timeout(argv, self.config) if timeout is None else timeout
        self.log.emit("command_start", argv=argv, timeout_seconds=timeout)
        result = run_command(self.supervisor, command, self.root, timeout, self.config["max_output_chars"], progress=self.log)
        result["argv"] = argv
        self.log.emit("command_result", **result)
        return result

    def process_started(self, pid):
        identity = process_identity(pid)
        # A very short-lived command may already have exited; its parent still waits and records its result.
        self.store.commit(active_command={"phase": "running", "pid": pid, "identity": identity, "time": now()})


class DurableWorkflow(Workflow):
    def __init__(self, repo, gateway, config, log, store, revalidate=False):
        super().__init__(repo, gateway, config, log)
        self.store, self.revalidate = store, revalidate
        self.verifications = []
        self.reviews = []
        self.critics = []

    def evidence(self, kind, value):
        number = len(self.store.state["evidence"][kind]) + 1
        reference = self.store.artifact(f"rounds/{self.store.state['round_number']:04d}/{kind}-{number:04d}.json", value)
        refs = copy.deepcopy(self.store.state["evidence"])
        refs[kind].append(reference)
        self.store.commit(evidence=refs)
        return reference

    def implement(self, task, feedback=""):
        self.store.commit("EXECUTING", current_step="revalidate_existing_work" if self.revalidate else "implementation")
        if self.revalidate:
            return "Existing unverified files submitted to all gates"
        return super().implement(task, feedback)

    def verify(self, commands):
        self.store.commit("VERIFYING", current_step="verification")
        before = snapshot(self.repo)
        start = len(self.store.records)
        passed, summary = super().verify(commands)
        after = snapshot(self.repo)
        passed = passed and before == after
        if before != after:
            summary += "\nRepository changed during verification; evidence is stale"
        results = [{k: r[k] for k in ("argv", "exit_code", "stdout", "stderr", "duration_seconds")}
                   for r in self.store.records[start:] if r["event"] == "command_result"]
        value = {"passed": passed, "snapshot": after, "commands": results, "time": now()}
        value["reference"] = self.evidence("verifier", value)
        self.verifications.append(value)
        return passed, summary

    def critique(self, task, evidence, diff):
        self.store.commit("REVIEWING", current_step="advisory_critic")
        value = super().critique(task, evidence, diff)
        self.critics.append({"result": value, "reference": self.evidence("critic", value)})
        return value

    def review(self, task, evidence, role, critic=None):
        self.store.commit("REVIEWING", current_step="authoritative_review")
        before = snapshot(self.repo)
        if not self.verifications or not self.verifications[-1]["passed"] or before != self.verifications[-1]["snapshot"]:
            return False, "Repository differs from verified contents"
        approved, findings = super().review(task, evidence, role, critic)
        after = snapshot(self.repo)
        approved = approved and before == after
        value = {"approved": approved, "role": role, "identity": model_identity(self.config, self.config["roles"][role]),
                 "snapshot": before, "time": now()}
        value["reference"] = self.evidence("reviewer", value)
        self.reviews.append(value)
        return approved, findings if before == after else "Repository changed during review"

    def checkpoint(self, result):
        current = snapshot(self.repo)
        expected = self.store.state["options"]["commands"] + [["git", "diff", "--check"]]
        if (result["status"] != "passed" or len(self.verifications) < 2 or not self.reviews
                or not self.reviews[-1]["approved"] or self.reviews[-1]["snapshot"] != current
                or not all(v["passed"] and v["snapshot"] == current for v in self.verifications[-2:])
                or any([r["argv"] for r in v["commands"]] != expected
                       or any(r["exit_code"] != 0 for r in v["commands"]) for v in self.verifications[-2:])
                or (self.store.state["options"]["critic"] and not self.critics)):
            raise DurableError("Authoritative evidence gates did not establish a checkpoint")
        self.store.commit("CHECKPOINTING", current_step="persist_checkpoint")
        checkpoint = {"schema_version": SCHEMA_VERSION, "run_id": self.store.state["run_id"],
                      "round": self.store.state["round_number"], "step": "bounded_workflow", "time": now(),
                      "snapshot": current, "verification": [v["reference"] for v in self.verifications[-2:]],
                      "reviewer": self.reviews[-1]["reference"],
                      "critic": self.critics[-1]["reference"] if self.critics else None,
                      "critic_status": result.get("critic", {}).get("status", "disabled"),
                      "cleanup_passed": True, "environment": self.store.state["environment"]}
        reference = self.store.artifact(f"checkpoints/{checkpoint['round']:04d}.json", checkpoint)
        if snapshot(self.repo) != current:
            raise DurableError("Repository changed while checkpointing; candidate remains untrusted")
        self.store.commit("VERIFIED", current_step="checkpoint_accepted", current_git_head=current["head"],
                          last_verified_checkpoint={"reference": reference, "snapshot": current},
                          verified_progress=[{"task": self.store.state["original_task"], "checkpoint": reference}],
                          remaining_work=[], unverified_work={}, environment_revalidation_pending=False)


def validate_checkpoint_evidence(store, checkpoint, state=None):
    """Resolve every required checkpoint artifact, including both Wave 6 reviews."""
    try:
        active_state = state if state is not None else (getattr(store, "state", None) or {})
        references = checkpoint["verification"]
        if not isinstance(references, list) or not references:
            raise DurableError("Missing checkpoint verification evidence")
        for reference in references:
            store.read_evidence(reference)
        wave6 = "milestone_id" in checkpoint or bool(
            active_state.get("work_units") and active_state.get("options", {}).get("reviewer"))
        bound_reviews = {}
        final_payloads = [store.read_evidence(ref) for ref in references]
        for role in ("critic", "reviewer"):
            reference = checkpoint.get(role)
            policy = checkpoint.get("policy", {})
            required = wave6 or (
                role == "reviewer" and policy.get("reviewer", "required") == "required"
            ) or (
                role == "critic" and (policy.get("critic") == "run" or (
                    "policy" not in checkpoint and active_state.get("options", {}).get("critic")))
            )
            if required or reference is not None:
                payload = store.read_evidence(reference)
                if wave6:
                    bound_reviews[role] = payload
                    result = dict(payload, artifact_ref=reference)
                    if digest(result) != checkpoint.get(f"{role}_digest"):
                        raise DurableError(f"Checkpoint {role} binding mismatch")
        # Older qualification artifacts remain readable, unchanged historical evidence.
        # New checkpoints explicitly bind both semantic reviews and final verification
        # to the same complete code evidence and candidate snapshot.
        if wave6 and "candidate_evidence_digest" in checkpoint:
            code_digest = checkpoint["candidate_evidence_digest"]
            candidate_digest = digest(checkpoint["snapshot"])
            if not isinstance(code_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", code_digest):
                raise DurableError("Invalid checkpoint candidate evidence digest")
            for role, payload in bound_reviews.items():
                if (payload.get("candidate_evidence_digest") != code_digest
                        or payload.get("candidate_evidence_complete") is not True
                        or payload.get("candidate_snapshot_digest") != candidate_digest):
                    raise DurableError(f"Checkpoint {role} candidate evidence mismatch")
            for payload in final_payloads:
                if (payload.get("candidate_evidence_digest") != code_digest
                        or payload.get("candidate_snapshot_digest") != candidate_digest
                        or not payload.get("passed")
                        or digest(payload) != checkpoint.get("final_verification_digest")):
                    raise DurableError("Checkpoint final candidate evidence mismatch")
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, DurableError):
            raise
        raise DurableError("Invalid checkpoint evidence") from exc


def validate_repository(store, repo):
    state = store.state
    check_previous_command(state)
    current = snapshot(repo)
    original = state["baseline"]
    if any(current[k] != original[k] for k in ("repository", "git_directory", "git_identity")):
        raise DurableError("Repository identity mismatch; restore the original checkout")
    trusted = state["last_verified_checkpoint"]
    expected = trusted["snapshot"] if trusted else original
    if current["head"] != expected["head"] or current["branch"] != expected["branch"]:
        raise DurableError("Git HEAD/branch changed; automatic resume refused")
    if trusted:
        checkpoint = store.read_evidence(trusted["reference"])
        if checkpoint["snapshot"] != expected:
            raise DurableError("Checkpoint state mismatch")
        validate_checkpoint_evidence(store, checkpoint)
    if current == expected:
        return "trusted" if trusted else "baseline"
    if trusted or current != state["unverified_work"].get("observed"):
        raise DurableError("Unexpected dirty/manual changes; automatic resume refused. Preserve the files and inspect "
                           "the run evidence; restore the exact recorded baseline/checkpoint before retrying. No files were reset.")
    return "unverified"


def finish(store):
    store.commit("COMPLETED", current_step="finished")
    report = {key: store.state[key] for key in ("run_id", "status", "verified_progress", "remaining_work",
                                               "last_verified_checkpoint", "last_failure_evidence", "round_number")}
    store.artifact("final-report.json", report)
    return report


def execute(store, progress=False, revalidate=False):
    drift = environment.compare(store.state["environment"], current_fingerprint(store))
    if drift["decision"] in {"UNSAFE", "REVALIDATION_REQUIRED"}:
        raise DurableError("ENVIRONMENT_DRIFT before execution: " + drift_diagnostic(drift))
    with Supervisor(store) as supervisor:
        result = _execute(store, progress, revalidate, supervisor)
        return result


def _execute(store, progress=False, revalidate=False, supervisor=None):
    state = store.state
    config = state["options"]["config"]
    log = DurableLog(store, progress)
    repo = DurableRepository(Path(state["target_repository"]), config, log, store)
    gateway = Gateway(config, log)
    if hasattr(gateway, "supervisor"):
        gateway.supervisor = supervisor
    # A crash may leave an empty round directory before its phase commit.
    existing = [int(p.name) for p in (store.directory / "rounds").iterdir() if p.name.isdigit()]
    number = max([state["round_number"], *existing]) + 1
    (store.directory / "rounds" / f"{number:04d}").mkdir(exist_ok=False)
    store.commit("PLANNING", round_number=number, current_step="planning")
    workflow = DurableWorkflow(repo, gateway, config, log, store, revalidate)
    try:
        # Clear residual model processes from an interrupted invocation before starting a new one.
        gateway.unload()
        task = state["original_task"]
        if state["acceptance_criteria"]:
            task += "\nAcceptance criteria:\n" + "\n".join(state["acceptance_criteria"])
        result = workflow.run(task, state["options"]["commands"], state["options"]["reviewer"],
                              critic=state["options"]["critic"])
        gateway.unload()
        if result["status"] != "passed":
            raise DurableError("Workflow gates failed; see round evidence")
        if supervisor.failure:
            raise DurableError("Heartbeat persistence failed; checkpoint refused")
        supervisor.require_idle()
        end_drift = environment.compare(store.state["environment"], current_fingerprint(store))
        if end_drift["decision"] in {"UNSAFE", "REVALIDATION_REQUIRED"}:
            raise DurableError("ENVIRONMENT_DRIFT during execution; checkpoint refused: " + drift_diagnostic(end_drift))
        workflow.checkpoint(result)
        return finish(store)
    except BaseException as exc:
        # A true kill cannot execute this handler: the preceding committed phase remains authoritative.
        cleanup = "passed"
        try:
            gateway.unload()
        except Exception:
            cleanup = "failed"
        failure = {"type": type(exc).__name__, "step": store.state["current_step"],
                   "round": number, "time": now(), "cleanup": cleanup,
                   "events_through": len(store.records), "evidence": copy.deepcopy(store.state["evidence"])}
        reference = store.artifact(f"rounds/{number:04d}/failure.json", failure)
        # Preserve prior trusted checkpoints even when their environment revalidation fails.
        store.commit("INTERRUPTED" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "FAILED",
                     last_failure_evidence=reference)
        raise


def cli(args, root, config=None):
    """Status does not contact models; resume uses the original pinned configuration."""
    runs = root / "runs"
    run_id = getattr(args, "run_id", None) or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:12]
    store = Store(runs, run_id)
    if args.command == "status":
        store.load()
        return status_report(store)
    # Serialize durable controllers sharing this gateway; OS releases this lock on process death.
    with exclusive_lock(runs / ".durable-controller.lock"):
        if args.command == "recover-model":
            return recover_model(store, runs, args)
        if args.command == "long-run":
            import shlex
            commands = [json.loads(s) if s.lstrip().startswith("[") else shlex.split(s) for s in args.verify]
            repo = Repository(args.repo, config, EventLog(runs / ".durable-preflight.jsonl"))
            store.create(repo, args.task, commands, config, args.reviewer, args.critic, args.acceptance,
                         getattr(args, "config", None))
            print(f"Durable run: {run_id}", flush=True)
        else:
            store.load()
            if "manager" in store.state:
                raise DurableError("This is an autonomous Manager run; use autonomous-resume --run-id " + run_id)
            repo = Repository(Path(store.state["target_repository"]), store.state["options"]["config"],
                              EventLog(runs / ".durable-preflight.jsonl"))
            mode = validate_repository(store, repo)
            store.repair_cache()
            if "supervision" not in store.state:
                raise DurableError("Legacy Phase 1 run has no environment/boot evidence; automatic continuation refused")
            Supervisor(store).require_idle()
            current_environment = current_fingerprint(store)
            drift = environment.compare(store.state["environment"], current_environment)
            store.commit(environment_drift=drift, supervision_event={"event": "environment_drift"})
            if drift["decision"] == "UNSAFE":
                raise DurableError("ENVIRONMENT_DRIFT: automatic continuation refused: " + drift_diagnostic(drift))
            revalidate_environment = drift["decision"] == "REVALIDATION_REQUIRED"
            if revalidate_environment and not getattr(args, "revalidate_environment", False):
                raise DurableError("ENVIRONMENT_DRIFT: revalidation required: " + drift_diagnostic(drift)
                                   + "; use --revalidate-environment to rerun all gates without implementation")
            if mode == "unverified" and not args.revalidate_unverified:
                raise DurableError("Recorded unfinished edits are UNVERIFIED. Inspect the evidence, then use "
                                   "resume --run-id " + run_id + " --revalidate-unverified to rerun all gates without implementation.")
            store.repair_cache()
            previous = store.state["status"]
            recovery = {"resume_count": store.state["recovery"]["resume_count"] + 1,
                        "previous_status": previous, "resumed_at": now(), "mode": mode,
                        "interrupted_step": store.state["current_step"]}
            if previous not in {"CREATED", "FAILED", "INTERRUPTED", "VERIFIED", "COMPLETED"}:
                reference = store.artifact(f"rounds/{store.state['round_number']:04d}/interruption-{uuid.uuid4().hex}.json",
                                           {"status": previous, "step": store.state["current_step"],
                                            "time": now(), "evidence": store.state["evidence"],
                                            "reason": "previous_process_ended_without_terminal_state"})
                store.commit("INTERRUPTED", last_failure_evidence=reference, recovery=recovery)
            else:
                store.commit(recovery=recovery)
            if revalidate_environment:
                store.commit(environment=current_environment, environment_revalidation_pending=True,
                             supervision_event={"event": "environment_fingerprint"})
            if store.state.get("environment_revalidation_pending"):
                if not getattr(args, "revalidate_environment", False):
                    raise DurableError("ENVIRONMENT_DRIFT: interrupted environment revalidation; --revalidate-environment required")
                mode = "unverified"
            if mode == "trusted":
                return finish(store)
        return execute(store, progress=not args.quiet,
                       revalidate=args.command == "resume" and mode == "unverified")


def recover_model(store, runs, args):
    """Explicit, audited resolution of one UNKNOWN model child; never resumes or signals anything."""
    store.load()
    store.repair_cache()
    if "supervision" not in store.state:
        raise DurableError("Legacy Phase 1 run has no supervision evidence; nothing to recover")
    gateway = Gateway(store.state["options"]["config"], EventLog(runs / ".durable-preflight.jsonl"))
    try:
        running = gateway.running()
    except (OSError, RuntimeError, ValueError) as exc:
        raise DurableError("Gateway state cannot be verified (start the dedicated gateway with no model loaded): "
                           + type(exc).__name__) from exc
    child = Supervisor(store).resolve_model(args.run_id, args.child_id, args.resolution, args.reason,
                                            bool(args.confirm), running, winprocess.servers())
    return {"run_id": store.state["run_id"], "child_id": child["child_id"], "state": child["state"],
            "resolution": child["resolution"]["resolution"], "run_status": store.state["status"],
            "next": ("autonomous-resume" if "manager" in store.state else "run resume")
                    + " --run-id " + store.state["run_id"] + "; all other gates still apply"}


def bind_work_unit_executor(store, executor):
    """Bind the controller's injected Wave 4 executor before fingerprints or recovery.

    The first live dispatch pins its binary. Scripted dispatch excludes that inactive
    binary, but retains the pin for a subsequent live dispatch. Other environment
    evidence and the existing comparison rules are unchanged.
    """
    kind = type(executor).__name__
    if kind in {"Agents", "SimpleNamespace"}:
        executor = getattr(executor, "executor", None)
        kind = type(executor).__name__
    if store.state.get("options", {}).get("config", {}).get("executor") == "safe_qwen" or kind == "SafeQwenExecutor":
        from safe_executor_binding import bind_executor
        return bind_executor(store, executor)
    if kind not in {"LiveWorkUnitExecutor", "ScriptedExecutor"}:
        return
    store._effective_work_unit_executor = executor
    options = copy.deepcopy(store.state.get("options", {}))
    options.pop("effective_executor", None)
    if "environment" not in store.state:  # Lightweight scheduler fixtures.
        store.state["options"] = options
        return
    baseline = copy.deepcopy(store.state["environment"])
    baseline["config"].pop("effective_executor", None)
    baseline["config_sha256"] = environment.checksum(baseline["config"])
    options["config"].pop("effective_executor", None)
    previous = baseline["runtime"].get("cline")
    if previous is not None and "work_unit_live_identity" not in options:
        options["work_unit_live_identity"] = copy.deepcopy(previous)
    if kind == "LiveWorkUnitExecutor":
        from live_work_unit_executor import cline_executable
        path = cline_executable(executor.config)
        identity = environment.file_identity(path) if path else {"error": "unavailable"}
        options.setdefault("work_unit_live_identity", identity)
        baseline["runtime"]["cline"] = copy.deepcopy(options["work_unit_live_identity"])
        options["work_unit_executor"] = {"kind": "live", "executable": path}
    else:
        baseline["runtime"].pop("cline", None)
        options["work_unit_executor"] = {"kind": "scripted"}
    baseline["sha256"] = environment.checksum({k: v for k, v in baseline.items() if k != "sha256"})
    if options != store.state["options"] or baseline != store.state["environment"]:
        store.commit(options=options, environment=baseline)


def current_fingerprint(store, executor=None):
    state = store.state
    cfg = copy.deepcopy(state["options"]["config"])
    effective = executor if executor is not None else getattr(store, "_effective_work_unit_executor", None)
    recorded = state["options"].get("work_unit_executor")
    if effective is None and recorded:
        # Read-only status/reload uses the last bound dispatch's actual binary path.
        if recorded["kind"] == "live":
            from live_work_unit_executor import LiveWorkUnitExecutor
            effective = LiveWorkUnitExecutor({"executable": recorded["executable"]})
        elif recorded["kind"] != "safe_qwen":
            from work_unit_scheduler import ScriptedExecutor
            effective = ScriptedExecutor()
    if effective is not None:
        cfg.pop("effective_executor", None)
    return environment.capture(cfg, state["target_repository"],
                               state["options"]["commands"], state.get("environment_config_path"),
                               executor=effective)


def drift_diagnostic(drift):
    return "; ".join(f"{c['field']} changed ({c['class']})" for c in drift["changes"][:8])


def status_report(store):
    state = copy.deepcopy(store.state)
    if "supervision" not in state:
        state["resume_decision"] = "LEGACY_EVIDENCE_REQUIRES_MANUAL_RECOVERY"
        return state
    try:
        boot = winprocess.boot_identity()
        children = Supervisor(store).reconcile(persist=False)
        controller = controller_status(state["supervision"]["controller"], boot)
        drift = environment.compare(state["environment"], current_fingerprint(store))
        repo = Repository(Path(state["target_repository"]), state["options"]["config"], None)
        try:
            if "manager" in state:
                import manager
                repo_mode = manager.validate_repository(store, repo)
            else:
                repo_mode = validate_repository(store, repo)
        except (OSError, ValueError):
            repo_mode = "UNSAFE"
        blocked = any(c["state"] in {"UNKNOWN", "STARTING", "RUNNING", "STOPPING"} for c in children)
        state["supervision_status"] = {"controller": controller,
            "children": [{k: c[k] for k in ("child_id", "purpose", "pid", "state", "reason", "termination")} for c in children],
            "environment": drift, "repository": repo_mode}
        state["resume_decision"] = ("REFUSED" if repo_mode == "UNSAFE" or drift["decision"] == "UNSAFE" else
            "WAIT_OR_INSPECT_CHILD" if blocked else "REVALIDATION_REQUIRED"
            if (repo_mode == "unverified" and "manager" not in state)
            or drift["decision"] == "REVALIDATION_REQUIRED" or state.get("environment_revalidation_pending") else "SAFE")
    except (OSError, ValueError, RuntimeError):
        state["resume_decision"] = "UNKNOWN_REFUSE"
    return state


def concise_status(state):
    report = state.get("supervision_status", {})
    controller = report.get("controller", {})
    checkpoint = state["last_verified_checkpoint"]
    lines = [f"RUN {state['run_id']}: {state['status']}",
             "checkpoint: " + (checkpoint["reference"]["path"] if checkpoint else "none"),
             f"CONTROLLER heartbeat: {controller.get('heartbeat', 'unknown')}; boot: {controller.get('boot_session', 'unknown')}"]
    children = report.get("children", [])
    active = [c for c in children if c["state"] not in {"EXITED", "RESOLVED"}]
    lines.append(f"CHILDREN: {len(children)} recorded; {len(children) - len(active)} exited/resolved")
    lines.extend(f"  {c['child_id']}: {c['state']} ({c['reason']})" for c in active[-8:])
    lines.append(f"ENVIRONMENT: {report.get('environment', {}).get('decision', 'UNKNOWN')}; repo: {report.get('repository', 'UNKNOWN')}")
    lines.extend("  " + change["field"] + ": " + change["class"]
                 for change in report.get("environment", {}).get("changes", [])[:8])
    if "manager" in state:
        import manager
        lines.extend(manager.concise_lines(state))
    lines.append("RESUME: " + state["resume_decision"])
    return "\n".join(lines)
