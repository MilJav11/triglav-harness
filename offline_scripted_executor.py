"""Windows offline actions; standalone API plus a private WAL-bound entry.

run_script() accepts a closed v1 data protocol and exact file allowlist. Its fixed
worker runs in a controller-owned kill-on-close Job, never in this process.
This is lifecycle containment, NOT a sandbox against malicious same-user code.
The default API supplies no scheduler, WAL, grant or retry authority. The private
entry requires a registered uninterrupted Store controller, never action JSON.
There is no historical resume/adoption API.
Execution timeout excludes cleanup (10s drain + 10s exit wait, with a further
10s close fallback on faults) and bounded filesystem inspection.
Exact relative ASCII file names only; forbidden entries also exclude subtrees.
Repository snapshots include metadata and ignored files, reject reparse points,
and are limited to 4096 entries / 128 MiB. No rollback or terminal acceptance.
"""
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time
import uuid
from contextlib import nullcontext

import winprocess

MAX_INPUT_BYTES = 65536
MAX_ACTIONS = 64
MAX_FILE_BYTES = 32768
MAX_RECEIPT_BYTES = 2048
MAX_DIAGNOSTIC_BYTES = 4096
MAX_TREE_ENTRIES = 4096
MAX_TREE_BYTES = 128 * 1024 * 1024
MAX_EVIDENCE_BYTES = 32768


class OfflineExecutorError(RuntimeError):
    """Refusal or uncertain evidence. Never permission to retry or dispatch."""
    def __init__(self, message, evidence=None):
        super().__init__(message)
        self.evidence = evidence


def _require(ok, message="Invalid offline action protocol"):
    if not ok:
        raise OfflineExecutorError(message)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "Duplicate JSON field")
        result[key] = value
    return result


def _decode(raw):
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                          parse_constant=lambda _: _require(False))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise OfflineExecutorError("Malformed complete JSON") from exc


def _fields(value, names):
    _require(type(value) is dict and set(value) == set(names.split()))


def _relative(value):
    _require(type(value) is str and 0 < len(value) <= 240, "Invalid relative file path")
    parts = value.split("/")
    for part in parts:
        _require(re.fullmatch(r"[A-Za-z0-9_.-]+", part) is not None
                 and part not in {".", ".."} and not part.endswith(".")
                 and not part.lower().startswith(".git")
                 and part.split(".")[0].upper() not in
                 {"CON", "PRN", "AUX", "NUL", *("COM" + str(i) for i in range(1, 10)),
                  *("LPT" + str(i) for i in range(1, 10))}, "Unsafe or protected file path")
    return value


def _scope(allowed, forbidden):
    for values in (allowed, forbidden):
        _require(type(values) is list and len(values) <= MAX_ACTIONS)
        for path in values:
            _relative(path)
        _require(len({p.lower() for p in values}) == len(values), "Ambiguous scope")
    _require(bool(allowed), "An explicit file allowlist is required")
    return allowed, forbidden


def _permitted(path, allowed, forbidden):
    path = path.lower()
    return (path in {p.lower() for p in allowed}
            and not any(path == p.lower() or path.startswith(p.lower() + "/") for p in forbidden))


def validate_script(script, allowed_paths, forbidden_paths):
    """Shape validation only: caller data carries no runtime authorization.

    v1 actions: write(path,content), replace(path,old,new) with exactly one match,
    fail(message), sleep(seconds), emit(text). No command, callback or import op.
    Strings are UTF-8, <=32 KiB each; complete input is <=64 KiB; <=64 actions.
    """
    _scope(allowed_paths, forbidden_paths)
    _fields(script, "schema_version actions")
    _require(type(script["schema_version"]) is int and script["schema_version"] == 1)
    actions = script["actions"]
    _require(type(actions) is list and 0 < len(actions) <= MAX_ACTIONS)
    shapes = {"write": "op path content", "replace": "op path old new",
              "fail": "op message", "sleep": "op seconds", "emit": "op text"}
    for action in actions:
        _require(type(action) is dict and type(action.get("op")) is str and action["op"] in shapes)
        _fields(action, shapes[action["op"]])
        if "path" in action:
            _relative(action["path"])
            _require(_permitted(action["path"], allowed_paths, forbidden_paths), "Action outside allowed scope")
        if action["op"] == "sleep":
            value = action["seconds"]
            _require(type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 3600)
        for key in ("content", "old", "new", "message", "text"):
            if key in action:
                value = action[key]
                _require(type(value) is str and "\0" not in value)
                try:
                    _require(len(value.encode("utf-8")) <= MAX_FILE_BYTES)
                except UnicodeError as exc:
                    raise OfflineExecutorError("Invalid UTF-8 action text") from exc
        if action["op"] == "replace":
            _require(bool(action["old"]), "Replacement requires a nonempty exact match")
    _require(len(_json(script)) <= MAX_INPUT_BYTES, "Oversized action input")


def _plain(path):
    info = path.lstat()
    _require(not stat.S_ISLNK(info.st_mode)
             and not getattr(info, "st_file_attributes", 0) & 0x400, "Symlink/reparse point refused")
    return info


def _root(repository):
    path = Path(repository).absolute()
    _require(not str(path).startswith(("\\\\", "//")), "Network repository paths refused")
    # Inspect ancestors before resolve() could erase an escape.
    for parent in (*reversed(path.parents), path):
        _require(stat.S_ISDIR(_plain(parent).st_mode), "Repository ancestor is not a directory")
    _require(path == path.resolve(strict=True), "Repository alias refused")
    _require((path / ".git").exists(), "An existing Git repository is required")
    return path


def _target(root, relative):
    _relative(relative)
    target = root.joinpath(*relative.split("/"))
    for path in (*reversed(target.parents), target):
        if path == root or root in path.parents:
            if path.exists() or path.is_symlink():
                info = _plain(path)
                if path != root:
                    _require(path.name in {entry.name for entry in path.parent.iterdir()},
                             "Noncanonical path spelling refused")
                _require(stat.S_ISREG(info.st_mode) if path == target else stat.S_ISDIR(info.st_mode),
                         "Non-regular target or parent")
                if path == target:
                    _require(info.st_nlink == 1, "Hard-linked mutation target refused")
    _require(target.resolve().is_relative_to(root), "Path escapes repository")
    return target


def _snapshot(root):
    """No Git commands, no followed links; includes ignored files and .git metadata."""
    root = _root(root)  # Recheck root/ancestors on post-execution inspection too.
    result, pending, total, names = {}, [root], 0, set()
    while pending:
        directory = pending.pop()
        for path in sorted(directory.iterdir()):
            info = _plain(path)
            relative = path.relative_to(root).as_posix()
            _require(len(relative) <= 240 and relative.lower() not in names, "Ambiguous repository paths")
            names.add(relative.lower())
            _require(len(result) < MAX_TREE_ENTRIES, "Repository entry bound exceeded")
            if stat.S_ISDIR(info.st_mode):
                result[relative] = ["directory", info.st_mode]
                pending.append(path)
            else:
                _require(stat.S_ISREG(info.st_mode), "Non-regular repository entry")
                total += info.st_size
                _require(total <= MAX_TREE_BYTES, "Repository byte bound exceeded")
                with path.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                _require(path.stat().st_size == info.st_size, "Repository changed while hashing")
                result[relative] = [digest, info.st_size, info.st_mode]
    return result


def _source_identity():
    return {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
            for name in ("offline_scripted_executor.py", "offline_scripted_executor_worker.py", "winprocess.py")}


def _diagnostics(stream, maximum):
    stream.seek(0, 2)
    size = stream.tell()
    stream.seek(max(0, size - maximum))
    text = stream.read(maximum).decode("utf-8", errors="replace")
    text = "".join(c if c.isprintable() or c in "\n\t" else " " for c in text)
    text = text.encode("utf-8")[:maximum].decode("utf-8", errors="ignore")
    return {"text": text, "bytes_observed": size, "truncated": size > maximum}


def _receipt(stream, script_sha, action_count, exit_code):
    stream.seek(0)
    raw = stream.read(MAX_RECEIPT_BYTES + 1)
    _require(len(raw) <= MAX_RECEIPT_BYTES, "Oversized worker result")
    value = _decode(raw)
    _fields(value, "schema_version script_sha256 status actions_completed error")
    _require(type(value["schema_version"]) is int and value["schema_version"] == 1
             and value["script_sha256"] == script_sha
             and type(value["actions_completed"]) is int and 0 <= value["actions_completed"] <= action_count
             and type(value["status"]) is str, "Conflicting worker result")
    if value["status"] == "ok":
        _require(exit_code == 0 and value["actions_completed"] == action_count and value["error"] is None,
                 "Worker result contradicts native exit")
    else:
        _require(value["status"] == "failed" and exit_code == 10
                 and value["actions_completed"] < action_count
                 and type(value["error"]) is str and len(value["error"].encode("utf-8")) <= 512
                 and all(c.isprintable() or c in "\n\t" for c in value["error"]),
                 "Worker result contradicts native exit")
    return value


def run_script(repository, script, *, allowed_paths, forbidden_paths=None,
               timeout_seconds, cancel_event=None, maximum_diagnostic_bytes=2048):
    """Run once, standalone. Result success means exact scripted edits, not acceptance.

    Native/cleanup/protocol ambiguity raises OfflineExecutorError with evidence.
    Ordinary scripted failure, timeout and cancellation after launch return
    success=False. Preflight (including pre-cancellation) refuses with an error.
    BaseException (including controller interruption) cleans up then propagates.
    No persistence, scheduler entry point, arbitrary command or executable input.
    """
    return _run_script(repository, script, allowed_paths=allowed_paths, forbidden_paths=forbidden_paths,
                       timeout_seconds=timeout_seconds, cancel_event=cancel_event,
                       maximum_diagnostic_bytes=maximum_diagnostic_bytes)


def _run_script(repository, script, *, allowed_paths, forbidden_paths, timeout_seconds,
                cancel_event=None, maximum_diagnostic_bytes=2048, _dispatch=None):
    _require(os.name == "nt", "Windows native execution required")
    _require(type(timeout_seconds) in (int, float) and math.isfinite(timeout_seconds)
             and 0 < timeout_seconds <= 3600, "Finite execution timeout required")
    _require(type(maximum_diagnostic_bytes) is int and 0 < maximum_diagnostic_bytes <= MAX_DIAGNOSTIC_BYTES)
    if cancel_event is not None:
        import threading
        _require(type(cancel_event) is threading.Event, "A controller-owned threading.Event is required")
    forbidden_paths = [] if forbidden_paths is None else forbidden_paths
    validate_script(script, allowed_paths, forbidden_paths)
    script, allowed_paths, forbidden_paths = copy.deepcopy((script, allowed_paths, forbidden_paths))
    root = _root(repository)
    before = _snapshot(root)
    # Preserve source bytes for exact post-edit verification, never worker claims.
    expected = {}
    for action in script["actions"]:
        if "path" in action:
            target = _target(root, action["path"])
            if action["path"] not in expected:
                if target.exists():
                    _require(target.stat().st_size <= MAX_FILE_BYTES, "Target file too large")
                    with target.open("rb") as stream:
                        expected[action["path"]] = stream.read(MAX_FILE_BYTES + 1)
                    _require(len(expected[action["path"]]) <= MAX_FILE_BYTES, "Target file grew during capture")
                else:
                    expected[action["path"]] = None
    sources = _source_identity()
    script_sha = hashlib.sha256(_json(script)).hexdigest()
    payload = _json(dict(schema_version=1, repository=str(root), script=script,
                         allowed_paths=allowed_paths, forbidden_paths=forbidden_paths, script_sha256=script_sha))
    _require(len(payload) <= MAX_INPUT_BYTES, "Oversized complete action input")
    job_name = "Local\\triglav-offline-" + uuid.uuid4().hex
    evidence = dict(schema_version=1, source="controller_native_handles", operation="offline_scripted_actions",
                    repository=str(root), request_sha256=hashlib.sha256(payload).hexdigest(),
                    before_snapshot_sha256=hashlib.sha256(_json(before)).hexdigest(), after_snapshot_sha256=None,
                    script_sha256=script_sha, sources_sha256=sources, process=None, job=job_name,
                    identity_verified=False, execution_started=False, termination_basis="not_started",
                    observed_exit_code=None, cleanup="not_observed", execution_seconds=0.0,
                    duration_seconds=0.0, diagnostics=None, action_result=None, changed_paths=[],
                    changed_path_count=0, scope_verified=False, exact_edits_verified=False, success=False)
    native, error, started = None, None, time.monotonic()
    deadline = started + timeout_seconds
    if _dispatch is not None:
        _require(type(_dispatch) is _ControllerDispatch)
        _dispatch.check_request(root, script, allowed_paths, forbidden_paths)
        deadline = min(deadline, _dispatch.deadline)
    evidence["deadline_monotonic"] = deadline
    with tempfile.TemporaryFile() as inp, tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        inp.write(payload)
        inp.seek(0)
        try:
            _require(not (cancel_event and cancel_event.is_set()), "Cancelled before launch")
            _require(time.monotonic() < deadline, "Deadline exhausted before launch")
            if _dispatch is not None:
                _dispatch.intent(evidence)
            with (_dispatch.store.mutex if _dispatch is not None else nullcontext()):
                if _dispatch is not None:
                    _dispatch.guard()
                    _require(_dispatch.data["phase"] == "INTENT", "Creation requires this live intent")
                native = winprocess.ControllerVerifier(job_name)  # Native handles only; no verifier authority.
                if _dispatch is not None:
                    _dispatch.native = native
                command = [str(Path(sys.executable).resolve()), "-I", "-B",
                           str(Path(__file__).with_name("offline_scripted_executor_worker.py").resolve())]
                native.launch(command, root, (inp, out, err))
            identity = copy.deepcopy(native.identity)
            _require(type(native.pid) is int and native.pid > 0 and type(identity) is dict
                     and set(identity) == {"created", "executable"}
                     and type(identity["created"]) is str and identity["created"].isdigit()
                     and identity["executable"] == os.path.normcase(command[0]), "Missing/conflicting native identity")
            evidence["process"] = {"pid": native.pid, "identity": identity}
            native.verify(identity)
            evidence["identity_verified"] = True
            _require(sources == _source_identity() and before == _snapshot(root), "Input drift before resume")
            if cancel_event and cancel_event.is_set():
                evidence["termination_basis"] = "controller_cancel"
            elif time.monotonic() >= deadline:
                evidence["termination_basis"] = "controller_deadline"
            else:
                if _dispatch is None:
                    native.resume(identity)
                else:
                    _dispatch.bind_and_resume()
                evidence["execution_started"] = True
                while True:
                    code = native.wait(0)
                    if code is not None:
                        _require(type(code) is int and 0 <= code <= 0xffffffff, "Invalid native exit evidence")
                        evidence["observed_exit_code"] = code
                        evidence["termination_basis"] = ("controller_deadline" if time.monotonic() >= deadline
                                                         else "signaled_without_controller_termination")
                        break
                    if cancel_event and cancel_event.is_set():
                        evidence["termination_basis"] = "controller_cancel"
                        break
                    if time.monotonic() >= deadline:
                        evidence["termination_basis"] = "controller_deadline"
                        break
                    native.wait(min(0.02, max(0, deadline - time.monotonic())))
        except BaseException as exc:
            error = exc
            evidence["termination_basis"] = "controller_exception"
        finally:
            evidence["execution_seconds"] = round(time.monotonic() - started, 6)
            if native is not None:
                try:
                    native.terminate_job()
                    native.drain(timeout=10)
                    if native.process is not None:
                        # Job accounting can reach zero just before the process
                        # handle signals. Require the retained handle too.
                        code = native.wait(10)
                        _require(type(code) is int and 0 <= code <= 0xffffffff, "Missing native exit evidence")
                        if evidence["observed_exit_code"] is not None:
                            _require(evidence["observed_exit_code"] == code, "Conflicting native exit evidence")
                        evidence["observed_exit_code"] = code
                    evidence["cleanup"] = "observed_empty"
                except BaseException as exc:
                    error = OfflineExecutorError("Native cleanup/exit evidence could not be established")
                    error.__cause__ = exc
                finally:
                    try:
                        native.close()
                    except BaseException as exc:
                        evidence["cleanup"] = "not_observed"
                        error = OfflineExecutorError("Native handle close failed")
                        error.__cause__ = exc
        evidence["diagnostics"] = _diagnostics(err, maximum_diagnostic_bytes)
        try:
            after = _snapshot(root)
            evidence["after_snapshot_sha256"] = hashlib.sha256(_json(after)).hexdigest()
            changed = sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))
            evidence["changed_paths"] = changed[:MAX_ACTIONS]
            evidence["changed_path_count"] = len(changed)
            directories = {parent.as_posix() for p in expected for parent in Path(p).parents if str(parent) != "."}
            violations = [p for p in changed if not (
                (_permitted(p, allowed_paths, forbidden_paths) and p in expected)
                or (p in directories and p not in before and after.get(p, [None])[0] == "directory"
                    and _permitted(p, [p], forbidden_paths)))]
            _require(not violations, "Post-execution scope violation")
            evidence["scope_verified"] = True
            if error is None and evidence["termination_basis"] == "signaled_without_controller_termination":
                result = _receipt(out, script_sha, len(script["actions"]), evidence["observed_exit_code"])
                evidence["action_result"] = result
                for action in script["actions"][:result["actions_completed"]]:
                    if action["op"] == "write":
                        expected[action["path"]] = action["content"].encode("utf-8")
                    elif action["op"] == "replace":
                        old, new = action["old"].encode("utf-8"), action["new"].encode("utf-8")
                        data = expected[action["path"]]
                        _require(data is not None and data.count(old) == 1, "Ambiguous replacement result")
                        expected[action["path"]] = data.replace(old, new)
                for path, content in expected.items():
                    target = _target(root, path)
                    _require((content is None and not target.exists()) or
                             (content is not None and target.exists() and target.stat().st_size == len(content)
                              and target.read_bytes() == content), "Scripted edits disagree with repository")
                evidence["exact_edits_verified"] = True
                evidence["success"] = result["status"] == "ok"
        except BaseException as exc:
            if error is None:
                error = exc
        evidence["duration_seconds"] = round(time.monotonic() - started, 6)
        _require(len(_json(evidence)) <= MAX_EVIDENCE_BYTES, "Evidence bound exceeded")
        if _dispatch is not None:
            _dispatch.finish(evidence, error)
        if error is not None:
            if not isinstance(error, Exception):
                error.offline_evidence = evidence
                raise error
            raise OfflineExecutorError(str(error)[:512], evidence) from error
        return evidence


class _ControllerDispatch:
    """Ephemeral native adapter registered once to a live Store capability.

    No public hook/callback parameter. Script data never supplies lifecycle facts.
    Returned dictionaries cannot register this object or recreate its ownership.
    """
    def __init__(self, controller):
        self.controller, self.store = controller, controller.store
        self.deadline = min(controller.mode["task_deadline"],
                            self.store.state["repair_authorizations"]["repair-" + controller.mode["recovery_id"]]["budget"]["unit_deadline"],
                            time.monotonic() + controller.mode["offline_recipe"]["timeout_seconds"])
        self.native = self.evidence = self.last_record = self.data = self.pending = None

    def guard(self, *, candidate=True, budget=True):
        import durable
        import environment
        from supervision import _validated_store
        c, s = self.controller, self.store
        s._require_repair_controller(c)
        durable._repair_require(c.dispatch is self and (c.mode["schema_version"], c.mode.get("operation")) in
                               {(3, "OFFLINE_DISPATCH"), (4, "OFFLINE_VERIFY")} and c.allocation_record is not None)
        _validated_store(s)
        # Attempt 1 intentionally left a dirty candidate. Bind to its exact
        # native-verified snapshots below, not the original clean baseline.
        durable._repair_require(str(c.scheduler.repo.root) == s.state["target_repository"])
        c.scheduler.supervisor.require_idle()
        live = s.state["supervision"]["controller"]
        durable._repair_require(live["pid"] == os.getpid() and live["identity"] == winprocess.identity(os.getpid())
                               and live["boot"] == winprocess.boot_identity()
                               and c.scheduler.clock is time.monotonic
                               and c.scheduler.budgets == s.state["manager"]["budgets"]
                               and c.scheduler.parent_scope == s.state["manager"]["scope"]
                               and c.scheduler.verifier_registry == s.state["options"]["verifier_registry"]
                               and _source_identity() == c.mode["offline_sources"])
        unit = s.state["work_units"]["units"][c.mode["unit_id"]]
        first = unit["attempts"][0]
        durable._repair_require(len(unit["attempts"]) == 2 and first["repair_observation"]["observations"] == c.observations)
        drift = environment.compare(s.state["environment"], durable.current_fingerprint(s, executor=c.scheduler.agents.executor))
        durable._repair_require(drift["decision"] in {"MATCH", "INFORMATIONAL"})
        if candidate:
            durable._repair_require(c.scheduler._shadow_snapshot() == first["candidate_snapshot"]
                                   and c.scheduler._shadow_fs_snapshot() == first["repair_observation"]["candidate_fs_snapshot"])
        if budget:
            durable._repair_require(time.monotonic() < self.deadline)
        durable._validate_repair_allocation(s.state, s.state["repair_authorizations"]["repair-" + c.mode["recovery_id"]])

    def check_request(self, root, script, allowed, forbidden):
        with self.store.mutex:
            self.guard()
            r = self.controller.mode["offline_recipe"]
            _require(str(root) == self.store.state["target_repository"] and _json(script) == _json(r["script"])
                     and allowed == r["allowed_paths"] and forbidden == r["forbidden_paths"], "Foreign dispatch input")

    def intent(self, evidence):
        with self.store.mutex:
            self.guard()
            _require(self.data is None and "repair_dispatch" not in self.store.state, "Duplicate dispatch intent")
            self.evidence = evidence  # Internal observation object, never a returned executor payload.
            self.deadline = evidence["deadline_monotonic"]
            self.pending = "INTENT"
            self.store._advance_repair_dispatch(self.controller)

    def bind_and_resume(self):
        with self.store.mutex:
            self.guard()
            _require(type(self.native) is winprocess.ControllerVerifier and self.data["phase"] == "INTENT")
            self.native.verify(self.native.identity)
            self.pending = "BOUND"
            self.store._advance_repair_dispatch(self.controller)
            self.guard()
            self.native.verify(self.data["process"]["identity"])
            self.pending = "RELEASED"
            self.store._advance_repair_dispatch(self.controller)
            # Re-read authority AFTER both durable commit/cache boundaries and
            # hold the mutex through the only permitted ResumeThread call.
            self.guard()
            self.native.resume(self.data["process"]["identity"])

    def finish(self, evidence, error):
        c = self.controller
        if c.closed or self.store._repair_write_failed or self.data is None:
            return  # Never append after an uncertain required WAL boundary.
        with self.store.mutex:
            self.guard(candidate=False, budget=False)
            _require(evidence is self.evidence, "Foreign result cannot supply dispatch authority")
            # An unbound creation/identity failure remains an unadoptable intent.
            if self.data["phase"] == "INTENT" and evidence["process"] is not None:
                c.closed = True
                return
            self.pending = "COMPLETE"
            self.store._advance_repair_dispatch(c)

    def next_state(self):
        import durable
        c, s, phase = self.controller, self.store, self.pending
        self.guard(candidate=phase != "COMPLETE", budget=phase != "COMPLETE")
        old = s.state.get("repair_dispatch")
        _require((old is None and phase == "INTENT") or (old == self.data and (old["phase"], phase) in {
            ("INTENT", "BOUND"), ("BOUND", "RELEASED"), ("RELEASED", "COMPLETE"), ("INTENT", "COMPLETE"), ("BOUND", "COMPLETE")}),
            "Duplicate or out-of-order dispatch")
        grant = s.state["repair_authorizations"]["repair-" + c.mode["recovery_id"]]
        clock = c.scheduler.clock()
        if old is None:
            e = self.evidence
            d = dict(schema_version=1, dispatch_id="dispatch-" + c.mode["recovery_id"], phase=phase,
                     run_id=s.state["run_id"], unit_id=grant["unit_id"], source_attempt_id=grant["source_attempt_id"],
                     attempt_id=grant["consumption"]["attempt_id"], authorization_id=grant["authorization_id"],
                     consumption_wal={k: c.allocation_record[k] for k in ("sequence", "sha256")}, previous_wal=None,
                     controller_session=c.mode["controller_session"], candidate_fingerprint=grant["candidate_fingerprint"],
                     spec_sha256=grant["spec_sha256"], registry_sha256=grant["registry_sha256"],
                     environment_sha256=c.mode["environment_sha256"], repository=e["repository"],
                     request_sha256=e["request_sha256"], script_sha256=e["script_sha256"], sources_sha256=e["sources_sha256"],
                     before_snapshot_sha256=e["before_snapshot_sha256"], job=e["job"], deadline=self.deadline,
                     process=None, evidence=None, result=None)
        else:
            d = copy.deepcopy(old)
            d["previous_wal"] = {k: self.last_record[k] for k in ("sequence", "sha256")}
        d.update(phase=phase, checked_clock=clock, task_runtime_seconds=c.scheduler.runtime(clock), updated_at=durable.now())
        if phase == "BOUND":
            _require(type(self.native) is winprocess.ControllerVerifier)
            self.native.verify(self.native.identity)
            d["process"] = {"pid": self.native.pid, "identity": copy.deepcopy(self.native.identity)}
        if phase == "COMPLETE":
            e = self.evidence
            timed_out = (e["termination_basis"] == "controller_deadline"
                         or clock >= min(c.mode["task_deadline"], grant["budget"]["unit_deadline"]))
            outcome = "executor_timeout" if timed_out else "executor_succeeded" if e["success"] else "executor_failed"
            d["evidence"] = s.exclusive_artifact("dispatch-evidence/" + d["dispatch_id"] + ".json", e)
            first = s.state["work_units"]["units"][d["unit_id"]]["attempts"][0]
            d["result"] = dict(outcome=outcome, changed_paths=e["changed_paths"],
                               elapsed_seconds=clock - c.mode["started_clock"] - first["timeout_info"]["elapsed_seconds"])
        state = copy.deepcopy(s.state)
        state.update(repair_dispatch=d, revision=state["revision"] + 1, updated_at=d["updated_at"],
                     work_units=durable._repair_dispatch_section(c.allocation_record["state"]["work_units"], grant, d))
        state["manager"]["runtime_seconds"] = d["task_runtime_seconds"]
        self.data = copy.deepcopy(d)
        return state


def _run_controller_script(controller):
    """Explicit trusted entry: no selector, action JSON, native handle or callback inputs."""
    import durable
    from supervision import _validated_store
    _require(type(controller) is durable._RepairController, "Live controller capability required")
    s = controller.store
    with s.mutex:
        s._require_repair_controller(controller)
        _require(controller.mode.get("operation") in {"OFFLINE_DISPATCH", "OFFLINE_VERIFY"} and controller.allocation_record is not None
                 and controller.dispatch is None and "repair_dispatch" not in s.state, "Dispatch cannot be adopted or repeated")
        _validated_store(s)
        controller.dispatch = dispatch = _ControllerDispatch(controller)
    try:
        recipe = controller.mode["offline_recipe"]
        return _run_script(s.state["target_repository"], recipe["script"], allowed_paths=recipe["allowed_paths"],
                           forbidden_paths=recipe["forbidden_paths"], timeout_seconds=recipe["timeout_seconds"], _dispatch=dispatch)
    finally:
        # Only the separately selected qualification operation may continue,
        # and only after this object's acknowledged successful outcome commit.
        controller.closed = not (not controller.closed and controller.mode.get("operation") == "OFFLINE_VERIFY"
                                 and dispatch.last_record is not None
                                 and dispatch.last_record["state"]["repair_dispatch"]["phase"] == "COMPLETE"
                                 and dispatch.last_record["state"]["repair_dispatch"]["result"]["outcome"] == "executor_succeeded")
