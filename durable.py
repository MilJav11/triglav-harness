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
    def commit(self, status=None, pre_write_guard=None, **fields):
        state = copy.deepcopy(self.state)
        # Published state and WAL records must not share a caller's mutable
        # scheduler/Manager payload between its transition and sealing steps.
        state.update(copy.deepcopy(fields))
        state.update(status=status or state["status"], updated_at=now(), revision=state["revision"] + 1)
        validate_state(state)
        if pre_write_guard is not None:
            pre_write_guard(state)
        # The flushed WAL is the commit point. state.json is an atomic materialized view.
        self.append("state_committed", state=state, transition=fields.get("supervision_event"))
        self.state = state
        atomic_write(self.directory / "state.json", encoded(state) + b"\n")

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
            raw = (self.directory / "events.jsonl").read_bytes()
            lines = raw.split(b"\n")
            self.tail = lines.pop()  # An unterminated final record is never committed.
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
            if (not commits or (cached is not None and (cached["revision"] > len(commits)
                    or cached != commits[cached["revision"] - 1]))):
                raise DurableError("State and event evidence disagree")
            self.state, self.records = commits[-1], records
            if (self.directory / "original-task.txt").read_bytes().decode("utf-8") != self.state["original_task"]:
                raise DurableError("Original task artifact does not match state")
            checkpoint = self.state["last_verified_checkpoint"]
            if checkpoint:
                validate_checkpoint_evidence(self, self.read_evidence(checkpoint["reference"]))
            return self.state
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


def validate_checkpoint_evidence(store, checkpoint):
    """Resolve every required checkpoint artifact, including both Wave 6 reviews."""
    try:
        references = checkpoint["verification"]
        if not isinstance(references, list) or not references:
            raise DurableError("Missing checkpoint verification evidence")
        for reference in references:
            store.read_evidence(reference)
        wave6 = "milestone_id" in checkpoint or bool(
            store.state.get("work_units") and store.state.get("options", {}).get("reviewer"))
        bound_reviews = {}
        final_payloads = [store.read_evidence(ref) for ref in references]
        for role in ("critic", "reviewer"):
            reference = checkpoint.get(role)
            policy = checkpoint.get("policy", {})
            required = wave6 or (
                role == "reviewer" and policy.get("reviewer", "required") == "required"
            ) or (
                role == "critic" and (policy.get("critic") == "run" or (
                    "policy" not in checkpoint and store.state.get("options", {}).get("critic")))
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
        else:
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
