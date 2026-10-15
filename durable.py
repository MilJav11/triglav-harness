"""Opt-in durable execution. Disk state is controller evidence, never model memory."""

from __future__ import annotations

import copy
import ctypes
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import threading
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
    if "work_unit_planner" in state:
        import work_unit_planner
        work_unit_planner.validate_planning_section(state["work_unit_planner"], durable_state=state)
    if "terminal_acceptance" in state:
        validate_terminal_acceptance(state["terminal_acceptance"], state["run_id"])


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

    @synchronized
    def append(self, event, **fields):
        record = {"sequence": len(self.records) + 1, "time": now(), "event": event,
                  "previous": self.records[-1]["sha256"] if self.records else None, **fields}
        record["sha256"] = digest(record)
        with (self.directory / "events.jsonl").open("ab") as stream:
            stream.write(encoded(record) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
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
        # The flushed WAL is the commit point. state.json is an atomic materialized view.
        rec_fields = {}
        if "reviewer_approval" in fields:
            rec_fields["reviewer_approval"] = copy.deepcopy(fields["reviewer_approval"])
        seq = self.append(
            "state_committed",
            state=state,
            transition=fields.get("supervision_event"),
            **rec_fields,
        )
        committed_record = copy.deepcopy(self.records[seq - 1]) if isinstance(seq, int) and 1 <= seq <= len(self.records) else copy.deepcopy(self.records[-1])
        self.state = state
        atomic_write(self.directory / "state.json", encoded(state) + b"\n")
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
                        if state["revision"] != len(commits) + 1 or state["run_id"] != self.directory.name:
                            raise DurableError("Inconsistent durable state ledger")
                        commits.append(state)
                if (not commits or (cached is not None and (type(cached.get("revision")) is not int
                        or cached["revision"] > len(commits)
                        or encoded(cached) != encoded(commits[cached["revision"] - 1])))):
                    raise DurableError("State and event evidence disagree")

                candidate_state = commits[-1]
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
