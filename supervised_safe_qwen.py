"""Standalone, single-attempt SafeQwen process supervision.

The default entry accepts only scripted Gateway responses. A separate explicit
entry binds the existing owned Gateway and active first attempt. The fixed child
has no transport, Store, WAL, scheduler, grant, checkpoint or adoption authority.
Windows Job containment is NOT an OS sandbox against malicious same-user code.
Execution deadlines include IPC/tool work; local bounded filesystem operations
are checked before/after, not forcibly interrupted inside a filesystem syscall.
Cleanup has a separate 10s Job drain and 10s retained-process wait allowance.
"""
import copy
from contextlib import nullcontext
import ctypes
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import uuid

import winprocess
from integration_shim import operation_content, operation_payload, SafeQwenExecutor
from offline_scripted_executor import _snapshot, _diagnostics
from safe_qwen_worker import Policy, ToolSession, sha, MAX_HISTORY_CHARS
from tool_protocol import schema, parse, Denied

MAX_FRAME_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_INPUT_BYTES = 1024 * 1024
MAX_EVIDENCE_BYTES = 65536
KINDS = {"START", "MODEL", "RESPONSE", "TOOL", "TOOL_RESULT", "RESULT", "ERROR"}


class AdapterError(RuntimeError):
    def __init__(self, message, evidence=None):
        super().__init__(message)
        self.evidence = evidence


def _require(value, message="Invalid supervised worker protocol"):
    if not value:
        raise AdapterError(message)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("ascii")


def _fields(value, fields):
    _require(type(value) is dict and set(value) == set(fields.split()))


def _decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result, "Duplicate JSON field")
            result[key] = value
        return result
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                          parse_constant=lambda _: _require(False, "Nonfinite JSON"))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise AdapterError("Malformed JSON frame") from exc


def _frame(session, sequence, kind, payload):
    value = dict(version=1, session=session, sequence=sequence, kind=kind, payload=payload)
    raw = _json(value)
    _require(len(raw) <= MAX_FRAME_BYTES, "Oversized IPC frame")
    return raw + b"\n"


def _message(raw, session, sequence):
    _require(len(raw) <= MAX_FRAME_BYTES, "Oversized IPC frame")
    value = _decode(raw)
    _fields(value, "version session sequence kind payload")
    _require(type(value["version"]) is int and value["version"] == 1
             and type(value["sequence"]) is int and value["sequence"] == sequence
             and type(value["session"]) is str and value["session"] == session
             and type(value["kind"]) is str and value["kind"] in KINDS,
             "Stale, foreign or out-of-order IPC frame")
    return value["kind"], value["payload"]


def _sources():
    return {name: sha(Path(__file__).with_name(name).read_bytes()) for name in (
        "supervised_safe_qwen.py", "supervised_safe_qwen_worker.py", "safe_qwen_worker.py",
        "tool_protocol.py", "integration_shim.py", "winprocess.py", "offline_scripted_executor.py",
        "safe_executor_binding.py", "work_units.py", "harness.py", "supervision.py")}


_LOADED_SOURCES = _sources()


class ScriptedGateway:
    """Closed offline fixture: configured wire semantics, zero sockets/models.

    Responses are JSON data, never callables, exceptions or executable commands.
    An exact instance is required; a live Gateway or subclass is refused.
    """
    def __init__(self, config, responses):
        from safe_executor_binding import validate_config
        _require(type(config) is dict and type(responses) is list and 1 <= len(responses) <= 32)
        raw = _json(dict(config=config, responses=responses))
        _require(len(raw) <= MAX_INPUT_BYTES, "Oversized scripted Gateway input")
        value = _decode(raw)
        validate_config(value["config"])
        self.config, self.responses = value["config"], value["responses"]
        self.used = False
        self._lock = threading.Lock()


class _Reader:
    """Nonblocking native pipe observation; no uninterruptible reader thread."""
    def __init__(self, stream):
        import msvcrt
        self.stream, self.handle = stream, msvcrt.get_osfhandle(stream.fileno())
        self.buffer, self.total, self.closed = b"", 0, False
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.PeekNamedPipe.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong,
                                              ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        self.kernel.PeekNamedPipe.restype = ctypes.c_int

    def poll(self):
        if b"\n" not in self.buffer and not self.closed:
            available = ctypes.c_ulong()
            if not self.kernel.PeekNamedPipe(self.handle, None, 0, None, ctypes.byref(available), None):
                _require(ctypes.get_last_error() in (109, 232), "Native pipe observation failed")
                self.closed = True
            elif available.value:
                raw = os.read(self.stream.fileno(), min(65536, available.value))
                self.total += len(raw)
                _require(self.total <= MAX_TOTAL_BYTES, "IPC cumulative byte limit")
                self.buffer += raw
        if b"\n" in self.buffer:
            raw, self.buffer = self.buffer.split(b"\n", 1)
            _require(len(raw) <= MAX_FRAME_BYTES, "Oversized IPC frame")
            return raw
        _require(len(self.buffer) <= MAX_FRAME_BYTES, "Oversized IPC frame")
        return None


_OWNED_ATTEMPTS = set()
_OWNED_LOCK = threading.Lock()
MAX_RESPONSE_BYTES = 512 * 1024


class _OwnedGateway:
    """One controller-owned Attempt 1 binding, never constructed from IPC data."""
    def __init__(self, executor, unit_spec, repo, gateway, cancel_event, *, _recovery=None):
        import durable
        import harness
        import manager
        from supervision import Supervisor
        from safe_executor_binding import validate_pinned
        _require(type(repo) is manager.ScopedRepository and type(repo.store) is durable.Store
                 and type(gateway) is harness.Gateway and type(gateway.supervisor) is Supervisor,
                 "Exact owned Gateway, Supervisor, Store and repository required")
        _require(type(executor) is SafeQwenExecutor and getattr(repo.store, "_effective_work_unit_executor", None) is executor,
                 "Actual bound SafeQwen executor required")
        self.executor, self.repo, self.gateway = executor, repo, gateway
        self.recovery = _recovery
        if _recovery is not None:
            from safe_qwen_active_recovery import _Controller
            _require(type(_recovery) is _Controller and getattr(repo.store, "_safe_qwen_active_controller", None) is _recovery,
                     "Registered ACTIVE controller required")
        self.store, self.supervisor = repo.store, gateway.supervisor
        self.config, self.unit = copy.deepcopy(executor.config), _decode(_json(unit_spec))
        self.cancel_event = cancel_event
        self.used, self._lock = False, threading.Lock()
        self.started = False
        self.finishing = False
        self.control_started = False
        self.deadline = None
        self.parent = self.model = None
        self.handles = {}
        self.previous = (gateway.execution_deadline, gateway.execution_response_limit, gateway.execution_cancel_event)
        self.run_id = self.store.state["run_id"]
        self.controller = copy.deepcopy(self.store.state["supervision"]["controller"])
        self.details = dict(kind="owned_gateway", run_id=self.run_id, attempt_id=None,
                            parent=None, model=None, requests=[], model_cleanup="not_started", failure=None)
        with self.store.mutex:
            self.check()
            validate_pinned(self.config, executor)
            if self.recovery is not None and self.recovery.repairing():
                # The v6 intent and exact ACTIVE candidate already exist. The
                # legacy Manager resume projection does not describe these edits.
                from safe_qwen_active_dispatch import require_owner
                require_owner(self.recovery)
                self.recovery.worker_contract(self.unit, self)
            else:
                manager.validate_repository(self.store, repo)
            self.supervisor.require_idle()
            _require(gateway.active_role is None and gateway.active_model_child_id is None,
                     "Historical model state cannot be adopted")
            unit = self.store.state["work_units"]["units"][self.unit["unit_id"]]
            aid = unit["current_attempt"]
            first_record = next(r for r in self.store.records
                                if r.get("event") == "state_committed" and any(a["attempt_id"] == aid for a in r["state"].get("work_units", {}).get("units", {}).get(self.unit["unit_id"], {}).get("attempts", [])))
            original = first_record["state"]["supervision"]["controller"]
            _require(all(original[k] == self.controller[k] for k in ("pid", "identity", "boot")),
                     "Historical attempt allocation cannot be adopted")
            key = (str(self.store.directory), aid, first_record["sha256"])
            with _OWNED_LOCK:
                _require(key not in _OWNED_ATTEMPTS and len(_OWNED_ATTEMPTS) < 4096,
                         "Owned attempt already reserved or controller reservation bound exhausted")
                _OWNED_ATTEMPTS.add(key)
            self.details["attempt_id"] = aid

    def check(self, *, model=False):
        import durable
        import environment
        from supervision import _validated_store
        from safe_executor_binding import gateway_config, validate_pinned
        s, g, sup, e = self.store, self.gateway, self.supervisor, self.executor
        with s.mutex:
            self.fast_check()
            _validated_store(s)
            _require(s.state["run_id"] == self.run_id and s.directory.name == self.run_id
                     and s.state["target_repository"] == str(e.policy.root) == str(self.repo.root)
                     and not s.directory.resolve().is_relative_to(e.policy.root)
                     and self.repo.config == self.config and gateway_config(g) == self.config,
                     "Run/repository/Gateway ownership mismatch")
            if self.recovery is not None and self.recovery.repairing():
                from safe_qwen_active_dispatch import require_owner
                require_owner(self.recovery)
                _require(self.recovery.data["dispatch"]["phase"] in ("INTENT", "RELEASED"), "Closed repair dispatch")
            else:
                _require(not any(k in s.state for k in ("repair_recovery", "repair_authorizations", "repair_dispatch", "repair_verification")),
                         "Repair states are ineligible for this first-attempt transport")
            validate_pinned(self.config, e)
            if self.recovery is not None:
                self.recovery.worker_contract(self.unit, self)
            else:
                from terminal_run import validate_safe_qwen_unit_contract
                validate_safe_qwen_unit_contract(self.unit, self.config, e, s)
                unit = s.state["work_units"]["units"][self.unit["unit_id"]]
                _require(unit["spec"] == self.unit and self.unit["limits"]["max_attempts"] == 1
                         and unit["status"] == "EXECUTING" and len(unit["attempts"]) == 1
                         and unit["current_attempt"] == unit["attempts"][0]["attempt_id"]
                         and unit["attempts"][0]["completed_at"] is None
                         and unit["attempts"][0]["stage"] == "executing", "Active first attempt required")
            controller = s.state["supervision"]["controller"]
            _require(type(self.controller) is dict and all(controller[k] == self.controller[k] for k in ("pid", "identity", "boot"))
                     and controller["pid"] == os.getpid() and controller["identity"] == winprocess.identity(os.getpid())
                     and controller["boot"] == winprocess.boot_identity()
                     and sup.entered and not sup.closed and sup.failure is None
                     and sup.thread is not None and sup.thread.is_alive() and sup.worker_started.is_set(),
                     "Uninterrupted native controller ownership required")
            drift = environment.compare(s.state["environment"], durable.current_fingerprint(s, executor=e))
            _require(drift["decision"] in ("MATCH", "INFORMATIONAL"), "Pinned runtime/environment changed")
            if model and self.recovery is None:
                self._model_check()
        if model and self.recovery is not None:
            self._model_check()

    def fast_check(self):
        if self.recovery is not None:
            _require(not self.recovery.closed and not self.store._repair_write_failed
                     and getattr(self.store, "_safe_qwen_active_controller", None) is self.recovery,
                     "ACTIVE controller stopped or WAL outcome uncertain")
        _require(self.gateway.supervisor is self.supervisor and self.supervisor.store is self.store
                 and self.repo.store is self.store and self.executor.gateway is self.gateway
                 and getattr(self.store, "_effective_work_unit_executor", None) is self.executor
                 and self.gateway.config == self.config and self.executor.config == self.config
                 and self.store.state["options"]["config"] == self.config,
                 "Transport/configuration selection changed")
        if self.started:
            _require(self.gateway.execution_deadline == self.deadline
                     and self.gateway.execution_response_limit == MAX_RESPONSE_BYTES
                     and self.gateway.execution_cancel_event is self.cancel_event,
                     "Controller transport bounds changed")

    def _retain(self, label, pid, identity):
        k = winprocess._kernel()
        handle = k.OpenProcess(0x1000 | 0x100000, False, pid)
        _require(bool(handle), "Native model/parent handle unavailable")
        self.handles[label] = (k, handle, copy.deepcopy(identity))
        self._verify_handles()

    def _verify_handles(self):
        for k, handle, identity in self.handles.values():
            _require(k.WaitForSingleObject(handle, 0) == 258 and winprocess._handle_identity(k, handle) == identity,
                     "Retained model/parent identity is stale")

    def begin(self, deadline):
        _require(not self.finishing, "Completed transport cannot start a model")
        self.check()
        if self.previous[0] is not None:
            _require(type(self.previous[0]) in (int, float) and math.isfinite(self.previous[0]))
            deadline = min(deadline, self.previous[0])
        self.deadline = deadline
        self.gateway.set_deadline(deadline)
        self.gateway.execution_response_limit = MAX_RESPONSE_BYTES
        self.gateway.execution_cancel_event = self.cancel_event
        self.started = True  # Even partial model startup requires guarded cleanup.
        self.details["model_cleanup"] = "not_observed"
        path = Path(self.config["safe_qwen_gateway"]["pid_file"])
        with path.open("rb") as stream:
            raw = stream.read(33)
        _require(len(raw) <= 32 and raw.strip().isdigit(), "Gateway PID binding unavailable")
        pid = int(raw)
        identity = winprocess.identity(pid)
        _require(identity is not None and identity["executable"] == os.path.normcase(self.config["safe_qwen_gateway"]["binary"]),
                 "Gateway native identity mismatch")
        self.parent = dict(pid=pid, identity=identity)
        if self.recovery is not None and self.recovery.repairing():
            from safe_qwen_active_dispatch import gateway_parent
            _require(self.parent == gateway_parent(self.store.state), "Gateway owner changed between logical attempts")
        self.details["parent"] = copy.deepcopy(self.parent)
        self._retain("parent", pid, identity)
        self.control_started = True
        self.gateway.switch("code")
        self.check(model=True)

    def _model_check(self):
        g, s = self.gateway, self.supervisor
        self._verify_handles()
        children = s.reconcile(persist=False)
        models = [c for c in children if c["purpose"] == "model_server" and c["state"] not in ("EXITED", "RESOLVED")]
        _require(len(models) == 1 and models[0]["state"] == "RUNNING", "Model ownership ambiguous")
        c = models[0]
        expected = os.path.normcase(self.config["safe_qwen_runtime"]["code"]["binary"])
        _require(c["child_id"] == g.active_model_child_id and g.active_role == "code"
                 and c["run_id"] == self.run_id and c["role"] == "code" and c["alias"] == self.config["roles"]["code"]
                 and c["containment"] == "delegated" and c["boot"] == self.controller["boot"]
                 and c["identity"]["executable"] == expected and os.path.normcase(c["executable"]) == expected
                 and c["parent_pid"] == self.parent["pid"] and c["parent_identity"] == self.parent["identity"]
                 and winprocess.identity(c["pid"]) == c["identity"] and winprocess.identity(c["parent_pid"]) == c["parent_identity"],
                 "Foreign/stale delegated model identity")
        frozen = {k: copy.deepcopy(c[k]) for k in ("child_id", "run_id", "pid", "identity", "parent_pid", "parent_identity",
                                                  "alias", "role", "containment", "boot", "executable", "command_sha256", "token")}
        _require(self.model is None or self.model == frozen, "Model ownership changed during attempt")
        if self.model is None:
            self.model = frozen
            self.details["model"] = copy.deepcopy(frozen)
            self._retain("model", c["pid"], c["identity"])
        processes = winprocess.servers()
        running = g.running()
        _require(len(processes) == 1 and processes[0]["pid"] == c["pid"]
                 and processes[0]["parent_pid"] == c["parent_pid"]
                 and os.path.normcase(str(Path(processes[0]["path"]).resolve())) == expected
                 and len(running) == 1 and type(running[0]) is dict
                 and running[0].get("model") == c["alias"] and running[0].get("state") == "ready",
                 "Gateway/native model observation mismatch")
        self.fast_check()
        self._verify_handles()
        if self.recovery is not None:
            with self.store.mutex:
                current = self.supervisor.child(c["child_id"])
                _require(current["state"] == "RUNNING" and {k: current[k] for k in frozen} == frozen,
                         "Model ownership changed during Gateway observation")
                self.recovery.live(budget=True)

    def request(self, payload, number):
        from integration_shim import error_diagnostic
        try:
            _require(not self.finishing, "Cleanup cannot authorize a model request")
            self.check(model=True)
            remaining = self.deadline - time.monotonic()
            _require(remaining > 0 and not (self.cancel_event and self.cancel_event.is_set()), "Model request deadline/cancellation")
            timeout = self.gateway.bounded_timeout(min(remaining, self.config["request_timeout_seconds"]))
            row = dict(number=number, child_id=self.model["child_id"], request_sha256=sha(_json(payload)),
                       deadline=self.deadline, timeout_seconds=timeout)
            self.details["requests"].append(row)
            wire = self.gateway.request("POST", "/v1/chat/completions", payload, timeout=timeout,
                                        max_response_bytes=MAX_RESPONSE_BYTES, cancel_event=self.cancel_event)
            self.check(model=True)
            _require(time.monotonic() < self.deadline and not (self.cancel_event and self.cancel_event.is_set()), "Model response deadline/cancellation")
            _require(len(_json(wire)) <= MAX_RESPONSE_BYTES and operation_content(wire) is not None,
                     "Malformed or oversized model response")
            self.gateway.check_active_ram()
            row["wire_valid"] = True
            return wire
        except BaseException as exc:
            self.details["failure"] = error_diagnostic(exc, "owned_gateway_request")
            raise

    def finish(self):
        from integration_shim import error_diagnostic
        self.finishing = True  # Cleanup cannot authorize model requests, edits or resume.
        try:
            if self.started and self.control_started:
                # Cancellation and the attempt deadline never cancel cleanup.
                # Ownership drift refuses model operations; no retargeted unload.
                self.check()
                k, handle, identity = self.handles["parent"]
                _require(k.WaitForSingleObject(handle, 0) == 258 and winprocess._handle_identity(k, handle) == identity,
                         "Gateway parent ownership lost before cleanup")
                self.gateway.execution_cancel_event = None
                self.gateway.unload()
                model = self.handles.get("model")
                _require(model is None or model[0].WaitForSingleObject(model[1], 0) == 0,
                         "Model exit not observed on retained handle")
                self.supervisor.require_idle()
                self.details["model_cleanup"] = "observed_idle"
            elif self.started:
                self.details["model_cleanup"] = "not_started"
        except BaseException as exc:
            self.details["cleanup_failure"] = error_diagnostic(exc, "owned_gateway_cleanup")
            raise
        finally:
            for k, handle, _ in self.handles.values():
                k.CloseHandle(handle)
            self.handles.clear()
            self.gateway.set_deadline(self.previous[0])
            self.gateway.execution_response_limit, self.gateway.execution_cancel_event = self.previous[1:]


def run_owned_gateway_worker(executor, unit_spec, repo, *, gateway, audit_directory,
                             timeout_seconds=30, cancel_event=None):
    """Explicit owned first-attempt transport. No scheduler/recovery activation.

    Requires the Manager's exact bound objects and a current-controller Attempt 1
    allocation in the validated WAL. This does not allocate or complete attempts.
    """
    try:
        owned = _OwnedGateway(executor, unit_spec, repo, gateway, cancel_event)
    except Exception as exc:
        from integration_shim import error_diagnostic
        raise AdapterError("Owned Gateway preflight refused", {
            "schema_version": 1, "success": False, "trusted": False, "process": None,
            "failure": error_diagnostic(exc, "owned_gateway_preflight")}) from exc
    return _run_worker(executor.policy, unit_spec, gateway=owned, audit_directory=audit_directory,
                       timeout_seconds=timeout_seconds, cancel_event=cancel_event)


def run_scripted_worker(policy, unit_spec, *, gateway, audit_directory,
                        timeout_seconds=30, cancel_event=None):
    """One standalone attempt. No caller callbacks, process command or live API.

    audit_directory must be a new directory outside the repository. Its exclusive
    append-only, hash-chained fsynced log records intent before ToolSession edits.
    This log is diagnostic evidence, never the production Store/WAL or authority.
    On failure AdapterError.evidence contains bounded native/controller facts.
    """
    _require(type(gateway) is ScriptedGateway, "Standalone entry requires ScriptedGateway")
    return _run_worker(policy, unit_spec, gateway=gateway, audit_directory=audit_directory,
                       timeout_seconds=timeout_seconds, cancel_event=cancel_event)


def _run_worker(policy, unit_spec, *, gateway, audit_directory, timeout_seconds, cancel_event):
    from work_units import validate_unit_spec
    owned = type(gateway) is _OwnedGateway
    active = gateway.recovery if owned else None

    def boundary():
        return active.boundary() if active is not None else nullcontext()

    def network_boundary():
        return active.network_boundary() if active is not None else nullcontext()
    _require(os.name == "nt", "Native Windows required")
    _require(type(policy) is Policy and type(gateway) in (ScriptedGateway, _OwnedGateway) and not gateway.used,
             "A fresh scripted Gateway and exact controller policy are required")
    with gateway._lock:
        _require(not gateway.used, "Scripted Gateway already reserved by another controller")
        gateway.used = True  # Refused/uncertain operations cannot reuse this fixture.
    _require(type(timeout_seconds) in (int, float) and math.isfinite(timeout_seconds)
             and 0 < timeout_seconds <= 600, "Finite controller timeout required")
    if cancel_event is not None:
        _require(type(cancel_event) is threading.Event, "Cancellation must be a native Event")
    unit = _decode(_json(unit_spec))
    validate_unit_spec(unit)
    _require(type(unit["limits"]["max_attempts"]) is int and unit["limits"]["max_attempts"] == 1,
             "Only a single-attempt WorkUnit is supported")
    _require(type(unit["limits"]["timeout_seconds"]) in (int, float)
             and math.isfinite(unit["limits"]["timeout_seconds"])
             and 0 < unit["limits"]["timeout_seconds"] <= 600)
    _require(unit["mode"] in ("mutation", "read_only")
             and unit["scope"]["allowed_paths"] == list(policy.writable if unit["mode"] == "mutation" else policy.readable)
             and unit["scope"]["forbidden_paths"] == list(policy.frozen if unit["mode"] == "mutation" else ()),
             "Unit and controller policy differ")
    _require(len(_json(unit)) <= 16384 and len(policy.readable) <= 128, "Oversized worker contract")
    # Freeze all caller-owned data. Later caller mutation cannot change transport.
    cfg = copy.deepcopy(gateway.config)
    responses = [] if owned else copy.deepcopy(gateway.responses)
    from safe_executor_binding import validate_config
    validate_config(cfg)
    _require(len(_json(dict(config=cfg, responses=responses))) <= MAX_INPUT_BYTES)
    limit = cfg.get("max_actions", 32)
    _require(type(limit) is int and 1 <= limit <= 32)
    if active is not None:
        limit = min(limit, active.executor.max_actions)
    root = policy.root
    before = _snapshot(root)
    expected = copy.deepcopy(before)
    repair_context = active.worker_context() if active is not None and active.repairing() else None
    if repair_context is not None:
        from repair_evidence import encode_repair_evidence
        repair_raw = encode_repair_evidence(repair_context["repair_evidence"])
    sources = _sources()
    _require(sources == _LOADED_SOURCES, "Adapter source changed since import; restart with reviewed sources")
    for name in ("safe_qwen_worker", "tool_protocol", "integration_shim", "winprocess", "offline_scripted_executor"):
        _require(Path(sys.modules[name].__file__).resolve() == Path(__file__).with_name(name + ".py").resolve(),
                 "Adapter module shadowing refused")
    directory = Path(audit_directory).absolute()
    _require(not directory.is_relative_to(root) and not directory.exists(), "Audit directory must be new and external")
    for parent in (directory.parent, *directory.parent.parents):
        info = parent.lstat()
        _require(not parent.is_symlink() and not getattr(info, "st_file_attributes", 0) & 0x400,
                 "Audit directory alias refused")
    directory.mkdir()
    session_id = uuid.uuid4().hex
    started = time.monotonic()
    deadline = started + min(timeout_seconds, unit["limits"]["timeout_seconds"], cfg.get("timeout_seconds", 600))
    if owned and gateway.previous[0] is not None:
        deadline = min(deadline, gateway.previous[0])
    if active is not None:
        deadline = min(deadline, active.identity["deadline"])
    evidence = dict(schema_version=1, source="controller_native_handles", session=session_id,
                    unit_id=unit["unit_id"], attempt_number=1, trusted=False, success=False,
                    model=cfg["roles"]["code"], configuration_sha256=sha(_json(cfg)), sources=sources,
                    repository=str(root), before_snapshot_sha256=sha(_json(before)), after_snapshot_sha256=None,
                    job="Local\\safe-qwen-" + session_id, process=None, identity_verified=False,
                    execution_started=False, termination_basis="not_launched", observed_exit_code=None,
                    cleanup="not_observed", scope_verified=False, operations=[], model_requests=0,
                    receipt=None, protocol_failure=None, audit_sha256=None, duration_seconds=0)
    if repair_context is not None:
        evidence["repair_evidence_sha256"] = sha(repair_raw)
    native = error = writer = None
    sequence, state, pending_raw, receipt = 0, "MODEL", None, None
    audit_count, audit_hash, sent_bytes, audit_failed = 0, "0" * 64, 0, False

    def guard():
        if cancel_event is not None and cancel_event.is_set():
            evidence["termination_basis"] = "controller_cancel"
            raise AdapterError("Controller cancellation")
        if time.monotonic() >= deadline:
            evidence["termination_basis"] = "controller_deadline"
            raise AdapterError("Controller deadline exhausted")
        if owned:
            gateway.fast_check()

    def append(record):
        if active is not None:
            with active.store.mutex:
                active.live()
                append_local(record)
        else:
            append_local(record)

    def append_local(record):
        nonlocal audit_count, audit_hash, audit_failed
        _require(not audit_failed, "Prior audit write was uncertain")
        row = dict(session=session_id, sequence=audit_count, previous=audit_hash, record=record)
        raw = _json(row)
        _require(len(raw) <= 8192, "Audit record bound")
        try:
            with (directory / "audit.jsonl").open("ab", buffering=0) as log:
                _require(log.write(raw + b"\n") == len(raw) + 1, "Incomplete audit write")
                os.fsync(log.fileno())
        except BaseException:
            audit_failed = True
            if active is not None:
                active.persistence_failed()
            raise
        audit_count += 1
        audit_hash = sha(raw)
        if active is not None:
            active.audit(record, native, evidence, pending_raw)

    def audit(record):
        if record.get("phase") == "intent":
            guard()
            _require(_snapshot(root) == expected, "Repository drift before edit approval")
        append(record)
        if record.get("phase") == "intent":
            guard()  # A slow or cancelled approval write cannot permit mutation.
        if record.get("phase") == "applied":
            current = _snapshot(root)
            path = record["path"]
            proposed = copy.deepcopy(expected)
            _require(current[path][0] == record["after_sha256"] and current[path][2] == expected[path][2],
                     "Applied edit hash or file mode mismatch")
            proposed[path] = current[path]
            _require(current == proposed, "Unapproved mutation during edit")
            expected.clear()
            expected.update(proposed)

    effective = policy if unit["mode"] == "mutation" else Policy(root, policy.readable, (), policy.frozen, policy.required)
    tools = ToolSession(effective, audit=audit, max_actions=limit)
    r_child, w_parent = os.pipe()
    r_parent, w_child = os.pipe()
    with os.fdopen(r_child, "rb", buffering=0) as child_in, os.fdopen(w_child, "wb", buffering=0) as child_out, \
            os.fdopen(r_parent, "rb", buffering=0) as parent_in, os.fdopen(w_parent, "wb", buffering=0) as parent_out, \
            tempfile.TemporaryFile() as err:
        reader = _Reader(parent_in)

        def send(kind, payload):
            nonlocal sequence, sent_bytes, writer
            raw = _frame(session_id, sequence, kind, payload)
            sent_bytes += len(raw)
            _require(sent_bytes <= MAX_TOTAL_BYTES, "IPC cumulative byte limit")
            failures = []
            def write():
                try:
                    offset = 0
                    while offset < len(raw):
                        count = os.write(parent_out.fileno(), raw[offset:])
                        _require(count > 0, "Incomplete IPC write")
                        offset += count
                except BaseException as exc:
                    failures.append(exc)
            writer = threading.Thread(target=write, daemon=True)
            writer.start()
            while writer.is_alive():
                guard()
                writer.join(0.01)
            _require(not failures, "Worker IPC write failed")
            sequence += 1

        try:
            guard()
            append(dict(event="launch_intent", sources=sources, unit_sha256=sha(_json(unit))))
            if owned:
                with network_boundary():
                    gateway.begin(deadline)
                    guard()
            with boundary():
                guard()
                native = winprocess.ControllerVerifier(evidence["job"])
                command = [str(Path(sys.executable).resolve()), "-I", "-B",
                           str(Path(__file__).with_name("supervised_safe_qwen_worker.py").resolve())]
                native.launch(command, root, (child_in, child_out, err))
                child_in.close()
                child_out.close()
                identity = copy.deepcopy(native.identity)
                _fields(identity, "created executable")
                _require(type(native.pid) is int and native.pid > 0
                         and type(identity["created"]) is str and identity["created"].isdigit()
                         and identity["executable"] == os.path.normcase(command[0]), "Conflicting native identity")
                native.verify(identity)
                evidence.update(process=dict(pid=native.pid, identity=identity), identity_verified=True)
                _require(_sources() == sources and _snapshot(root) == before, "Source or repository drift before resume")
                guard()
                append(dict(event="release", process=evidence["process"], job=evidence["job"]))
                guard()
                native.resume(identity)
            evidence.update(execution_started=True, termination_basis="executing")
            start = dict(unit=unit, repository=str(root), deadline=deadline, max_actions=limit,
                         policy={k: list(getattr(policy, k)) for k in ("readable", "writable", "frozen", "required")})
            if repair_context is not None:
                start["dispatch_context"] = repair_context
            send("START", start)
            while True:
                guard()
                raw = reader.poll()
                if raw is not None:
                    kind, payload = _message(raw, session_id, sequence)
                    sequence += 1
                    if kind == "MODEL" and state == "MODEL":
                        _fields(payload, "messages schema")
                        messages = payload["messages"]
                        _require(payload["schema"] == schema() and type(messages) is list and 2 <= len(messages) <= 66)
                        for message in messages:
                            _fields(message, "role content")
                            _require(message["role"] in ("system", "user", "assistant") and type(message["content"]) is str)
                        _require(sum(len(m["content"]) for m in messages) <= MAX_HISTORY_CHARS)
                        index = evidence["model_requests"]
                        _require(index < (limit if owned else min(len(responses), limit)), "Model response budget exhausted")
                        request = operation_payload(cfg["roles"]["code"], messages, schema())
                        with boundary():
                            if owned:
                                _require(_snapshot(root) == expected, "Repository drift before model request")
                                append(dict(event="model_request_intent", number=index + 1, model=request["model"],
                                            child_id=gateway.model["child_id"], deadline=deadline,
                                            request_sha256=sha(_json(request))))
                                evidence["model_requests"] += 1
                        with network_boundary():
                            wire = gateway.request(request, index + 1) if owned else responses[index]
                        content = operation_content(wire)
                        pending_raw = content if content is not None else "{}"
                        append(dict(event="model_request", number=index + 1, model=request["model"],
                                    request_sha256=sha(_json(request)), wire_valid=content is not None))
                        if not owned:
                            evidence["model_requests"] += 1
                        send("RESPONSE", dict(content=pending_raw))
                        state = "TOOL"
                    elif kind == "TOOL" and state == "TOOL":
                        _fields(payload, "raw")
                        _require(type(payload["raw"]) is str and payload["raw"] == pending_raw,
                                 "Tool request differs from model response")
                        guard()
                        if owned:
                            gateway.check(model=True)
                        _require(_snapshot(root) == expected, "Repository drift before tool dispatch")
                        with boundary():
                            result = tools.dispatch(payload["raw"])
                        guard()
                        _require(_snapshot(root) == expected, "Unapproved repository mutation")
                        try:
                            tool = parse(payload["raw"])["tool"]
                        except Denied:
                            tool = None  # Diagnostic label only; ToolSession decided the operation.
                        evidence["operations"].append(dict(action=tools.actions, tool=tool,
                                                           raw_sha256=sha(payload["raw"].encode()),
                                                           ok=result["ok"], code=result.get("code")))
                        send("TOOL_RESULT", result)
                        state = "RESULT" if tools.submitted else "MODEL"
                    elif kind == "RESULT" and state == "RESULT":
                        _fields(payload, "unit_id attempt_number exit_code trusted submitted actions duration_seconds")
                        _require(payload["unit_id"] == unit["unit_id"] and type(payload["attempt_number"]) is int
                                 and payload["attempt_number"] == 1 and type(payload["exit_code"]) is int
                                 and payload["exit_code"] == 0 and payload["trusted"] is False
                                 and payload["submitted"] is True and type(payload["actions"]) is int
                                 and payload["actions"] == tools.actions and type(payload["duration_seconds"]) in (int, float)
                                 and math.isfinite(payload["duration_seconds"]) and 0 <= payload["duration_seconds"] <= timeout_seconds,
                                 "Conflicting worker receipt")
                        receipt, state = payload, "EXIT"
                    elif kind == "ERROR":
                        _fields(payload, "code")
                        _require(type(payload["code"]) is str and payload["code"] in ("WORKER_FAILED", "ACTION_LIMIT", "TIMEOUT"))
                        raise AdapterError("Worker refused: " + payload["code"])
                    else:
                        raise AdapterError("Duplicate or unexpected worker message")
                elif native.wait(0) is not None:
                    evidence["observed_exit_code"] = native.wait(0)
                    _require(not reader.buffer, "Incomplete IPC tail")
                    _require(state == "EXIT" and receipt is not None and evidence["observed_exit_code"] == 0,
                             "Worker exited without complete successful exchange")
                    evidence["termination_basis"] = "signaled_without_controller_termination"
                    break
                else:
                    time.sleep(0.005)
        except BaseException as exc:
            error = exc
            if owned:
                from integration_shim import error_diagnostic
                if gateway.details["failure"] is None:
                    gateway.details["failure"] = error_diagnostic(exc, "owned_gateway_lifecycle")
                if cancel_event is not None and cancel_event.is_set():
                    evidence["termination_basis"] = "controller_cancel"
                elif time.monotonic() >= deadline:
                    evidence["termination_basis"] = "controller_deadline"
            if evidence["termination_basis"] not in ("controller_cancel", "controller_deadline"):
                evidence["termination_basis"] = "controller_exception"
            evidence["protocol_failure"] = type(exc).__name__
        finally:
            child_in.close()
            child_out.close()
            if native is not None:
                try:
                    native.terminate_job()
                    native.drain(timeout=10)
                    code = native.wait(10) if native.process is not None else None
                    _require(type(code) is int and 0 <= code <= 0xffffffff, "Missing native exit evidence")
                    _require(evidence["observed_exit_code"] in (None, code), "Conflicting native exit evidence")
                    evidence.update(cleanup="observed_empty", observed_exit_code=code)
                    if active is not None and active.worker_state()["process"] is not None:
                        active.native_cleaned(native, code)
                except BaseException as exc:
                    error = exc
                finally:
                    try:
                        native.close()
                    except BaseException as exc:
                        evidence["cleanup"] = "not_observed"
                        error = exc
            if writer is not None:
                writer.join(1)
                if writer.is_alive():
                    error = AdapterError("IPC writer did not stop after native cleanup")
            try:
                # Descendants may inherit stdout. Inspect the final tail only
                # after the Job is empty; even a second valid receipt is refused.
                tail_deadline = time.monotonic() + 1
                while not reader.closed:
                    _require(reader.poll() is None, "Additional worker output after exit")
                    _require(time.monotonic() < tail_deadline, "IPC pipe did not close after native cleanup")
                _require(not reader.buffer, "Incomplete IPC tail")
            except BaseException as exc:
                error = error or exc
            evidence["diagnostics"] = _diagnostics(err, 4096)
            if owned:
                try:
                    gateway.finish()
                except BaseException as exc:
                    error = error or exc
                evidence["transport"] = copy.deepcopy(gateway.details)
            try:
                after = _snapshot(root)
                _require(after == expected and _sources() == sources, "Repository or source evidence mismatch")
                evidence.update(scope_verified=True, after_snapshot_sha256=sha(_json(after)))
                evidence["changed_paths"] = sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))
                append(dict(event="finished", termination=evidence["termination_basis"], cleanup=evidence["cleanup"],
                            after_snapshot_sha256=evidence["after_snapshot_sha256"]))
                with (directory / "audit.jsonl").open("rb") as log:
                    records = log.read(262145)
                _require(len(records) <= 262144 and records.endswith(b"\n"), "Audit log bound or torn tail")
                previous = "0" * 64
                rows = records.splitlines()
                _require(len(rows) == audit_count, "Audit record count mismatch")
                for index, raw in enumerate(rows):
                    row = _decode(raw)
                    _fields(row, "session sequence previous record")
                    _require(row["session"] == session_id and type(row["sequence"]) is int
                             and row["sequence"] == index and row["previous"] == previous,
                             "Audit chain mismatch")
                    previous = sha(raw)
                _require(previous == audit_hash, "Audit head mismatch")
            except BaseException as exc:
                error = error or exc
            evidence.update(audit_sha256=audit_hash, duration_seconds=round(time.monotonic() - started, 6))
            evidence["receipt"] = receipt
            evidence["success"] = error is None and evidence["cleanup"] == "observed_empty" and tools.submitted
            _require(len(_json(evidence)) <= MAX_EVIDENCE_BYTES, "Evidence byte bound")
            if active is not None and not active.closed and not active.store._repair_write_failed:
                try:
                    active.complete(evidence)
                except BaseException as exc:
                    error = error or exc
        if error is not None:
            if not isinstance(error, Exception):
                error.adapter_evidence = evidence
                raise error
            if owned:
                from critic_executor import sanitize_evidence
                message = sanitize_evidence(str(error))[:256]
            else:
                message = str(error)[:256]
            raise AdapterError(message, evidence) from error
        return evidence
