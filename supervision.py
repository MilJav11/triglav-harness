"""Durable process ownership and conservative recovery (no model content)."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

import winprocess

VERSION = 1
STATES = {"STARTING", "RUNNING", "STOPPING", "EXITED", "LOST", "STALE", "UNKNOWN", "RESOLVED"}
RESOLUTION_VERSION = 1
# The only resolution: the operator attests, and current evidence confirms, that no model server exists.
RESOLUTIONS = {"model_not_running"}
RECOVERABLE_REASONS = {"launch_identity_unproven", "delegated_launch_ambiguous"}
MAX_REASON = 200


def checksum(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def initial():
    return {"version": VERSION, "controller": None, "children": {}}


def validate(value, run_id):
    from durable import DurableError
    def require(condition):
        if not condition:
            raise ValueError("invalid supervision")
    try:
        require(type(value["version"]) is int and value["version"] == VERSION and isinstance(value["children"], dict))
        controller = value["controller"]
        if controller is not None:
            require(type(controller["pid"]) is int and controller["pid"] > 0)
            require(isinstance(controller["boot"], str) and controller["boot"])
            require(type(controller["heartbeat"]) in (int, float))
            require(isinstance(controller["identity"], dict))
        for key, child in value["children"].items():
            require(child["child_id"] == key and child["run_id"] == run_id)
            require(child["state"] in STATES)
            require(child["purpose"] in {"verification_command", "model_server", "helper", "opencode_executor"})
            require(isinstance(child["token"], str) and re.fullmatch(r"[0-9a-f]{32}", child["token"]))
            require(key == child["purpose"] + "-" + child["token"][:12])
            require(isinstance(child["boot"], str) and child["boot"])
            require(isinstance(child["executable"], str) and child["executable"])
            require(isinstance(child["command_sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", child["command_sha256"]))
            require(type(child["started_at"]) in (int, float))
            require(type(child["controller_pid"]) is int)
            require(child["pid"] is None or (type(child["pid"]) is int and child["pid"] > 0))
            require(child["identity"] is None or (isinstance(child["identity"], dict)
                and isinstance(child["identity"]["created"], str) and child["identity"]["created"]
                and isinstance(child["identity"]["executable"], str) and child["identity"]["executable"]))
            require(child["last_heartbeat"] is None or type(child["last_heartbeat"]) in (int, float))
            require(child["exit_code"] is None or type(child["exit_code"]) is int)
            require(child["termination"] in (None, "natural", "forced", "launch_failed"))
            require(child["containment"] in {"job", "delegated"})
            require(child["job"] == "Local\\harness-" + child["token"])
            require(child["state"] != "RUNNING" or (child["pid"] and child["identity"]))
            resolution = child.get("resolution")
            if child["state"] == "RESOLVED":
                require(child["purpose"] == "model_server" and isinstance(resolution, dict))
                require(resolution["version"] == RESOLUTION_VERSION and resolution["resolution"] in RESOLUTIONS)
                require(type(resolution["time"]) in (int, float) and resolution["prior_state"] == "UNKNOWN")
                require(isinstance(resolution["prior_reason"], str) and resolution["prior_reason"] in RECOVERABLE_REASONS)
                require(type(resolution["revision"]) is int and resolution["revision"] > 0)
                require(valid_reason(resolution["reason"]))
            else:
                require(resolution is None)
    except (ValueError, KeyError, TypeError):
        raise DurableError("Corrupt supervision state; automatic recovery refused") from None


def valid_reason(reason):
    return (isinstance(reason, str) and 0 < len(reason) <= MAX_REASON
            and reason == reason.strip() and reason.isprintable())


class Supervisor:
    def __init__(self, store, clock=time.time, boot=None, inspect=None):
        self.store, self.clock = store, clock
        self.boot = boot or winprocess.boot_identity
        self.inspect = inspect or winprocess.identity
        self.stop_event = threading.Event()
        self.worker_started = threading.Event()
        self.startup_ack = self.worker_started
        self.thread = None
        self.failure = None
        self.failures = []
        self.entered = False
        self.closed = False
        self.shutdown_attempted = False
        self.state = "never_started"

    def _record_failure(self, exc):
        if exc is not None:
            owner = getattr(self.store, "_safe_qwen_active_controller", None)
            if owner is not None and owner.scheduler.supervisor is self:
                owner.persistence_failed()
            if not any(f is exc for f in self.failures):
                self.failures.append(exc)
            if self.failure is None:
                self.failure = exc

    def change(self, event, child=None, detail=None, **fields):
        # Event and its state transition share one WAL commit point.
        with self.store.mutex:
            value = copy.deepcopy(self.store.state["supervision"])
            if child:
                value["children"][child["child_id"]] = child
            value.update(fields)
            transition = {"event": event, "child_id": child["child_id"] if child else None}
            if detail:
                transition["detail"] = detail
            self.store.commit(supervision=value, supervision_event=transition)

    def register(self, purpose, executable, command, containment="job", *, invocation=None):
        token = uuid.uuid4().hex
        child = dict(run_id=self.store.state["run_id"], child_id=f"{purpose}-{token[:12]}",
                     purpose=purpose, pid=None, controller_pid=os.getpid(),
                     executable=str(Path(executable).resolve()), command_sha256=checksum(command),
                     identity=None, started_at=self.clock(), token=token, state="STARTING",
                     boot=self.boot(), last_heartbeat=None, exit_code=None, termination=None,
                     containment=containment, job="Local\\harness-" + token, reason="launch_intent")
        if invocation is not None:
            if not isinstance(invocation, _VerifierInvocation) or invocation.supervisor is not self:
                raise ValueError("Invocation registration requires controller-owned context")
            child["invocation"] = copy.deepcopy(invocation.intent)
        self.change("invocation_intent" if invocation is not None else "process_registered", child)
        return child["child_id"]

    def child(self, child_id):
        return copy.deepcopy(self.store.state["supervision"]["children"][child_id])

    def started(self, child_id, pid, *, native=None):
        child = self.child(child_id)
        actual = self.inspect(pid)
        if actual is None:
            raise RuntimeError("Child exited before ownership could be established")
        if os.path.normcase(actual["executable"]) != os.path.normcase(child["executable"]):
            raise RuntimeError("Child executable differs from registered executable")
        if "invocation" in child:
            if not isinstance(native, winprocess.ControllerVerifier) or native.pid != pid or actual != native.identity:
                raise RuntimeError("Invocation requires retained native identity")
            native.verify(actual)
            child["invocation"]["phase"] = "BOUND"
        child.update(pid=pid, identity=actual, state="RUNNING", last_heartbeat=self.clock(), reason="identity_verified")
        self.change("invocation_bound" if "invocation" in child else "process_started", child)

    def exited(self, child_id, code, termination="natural"):
        child = self.child(child_id)
        child.update(state="EXITED", exit_code=code, termination=termination, reason="exit_observed")
        self.change("process_exit", child)

    def require_no_ambiguous_model(self, children=None):
        """An earlier uncertain delegated launch blocks any further model launch or cleanup."""
        from durable import DurableError
        children = self.reconcile() if children is None else children
        ambiguous = [c for c in children if c["purpose"] == "model_server"
                     and c["state"] in {"STARTING", "UNKNOWN"}]
        if ambiguous:
            raise DurableError(f"Model startup/cleanup is ambiguous ({ambiguous[0]['child_id']}: "
                               f"{ambiguous[0]['reason']}); an empty gateway does not prove it resolved; "
                               "continuation refused")

    def model_starting(self, role):
        from durable import DurableError
        import environment
        self.require_no_ambiguous_model()
        config = self.store.state["options"]["config"]
        from safe_executor_binding import is_safe_qwen_config
        if is_safe_qwen_config(config) or self.store.state["options"].get("work_unit_executor", {}).get("kind") == "safe_qwen":
            from safe_executor_binding import model_starting
            return model_starting(self, role)
        text = (environment.ROOT / "runs/swap-profile.yaml").read_text(encoding="utf-8")
        alias = config["roles"][role]
        match = re.search(r"(?m)^  " + re.escape(alias) + r':\s*\n\s+cmd: >-\s*\n\s+"([^"\n]+)"', text)
        if not match:
            raise DurableError("Model launch executable cannot be established")
        parent_pid = int((environment.ROOT / "runs/swap.pid").read_text().strip())
        parent = self.inspect(parent_pid)
        expected = os.path.normcase(str((environment.ROOT / "tools/llama-swap/bin/llama-swap.exe").resolve()))
        if not parent or os.path.normcase(parent["executable"]) != expected:
            raise DurableError("Gateway process identity cannot be established")
        child_id = self.register("model_server", match[1], {"alias": alias, "profile": config.get("review_profile"),
                                "rendered_config_sha256": hashlib.sha256(text.encode()).hexdigest()}, "delegated")
        child = self.child(child_id)
        child.update(parent_pid=parent_pid, parent_identity=parent, role=role, alias=alias)
        self.change("model_launch_intent", child)
        return child_id

    def model_observed(self, child_id, processes):
        child = self.child(child_id)
        if (len(processes) != 1 or processes[0].get("parent_pid") != child["parent_pid"]
                or self.inspect(child["parent_pid"]) != child["parent_identity"]):
            child.update(state="UNKNOWN", reason="delegated_launch_ambiguous")
            self.change("process_identity_mismatch", child)
            raise RuntimeError("Delegated model ownership cannot be established; no cleanup authorized")
        self.started(child_id, processes[0]["pid"])

    def guard_model_unload(self, running, processes):
        from durable import DurableError
        children = self.reconcile()
        models = [c for c in children if c["purpose"] == "model_server"]
        self.require_no_ambiguous_model(children)
        if not running and not processes:
            if any(c["state"] in {"RUNNING", "STOPPING"} for c in models):
                raise DurableError("Model startup/cleanup is ambiguous despite empty gateway; continuation refused")
            return
        owned = [c for c in models if c["state"] == "RUNNING"]
        if (len(owned) != 1 or len(processes) != 1 or processes[0]["pid"] != owned[0]["pid"]
                or self.inspect(owned[0]["parent_pid"]) != owned[0]["parent_identity"]
                or any(not isinstance(r, dict) or r.get("model") != owned[0]["alias"] for r in running)):
            raise DurableError("Model unload refused: delegated ownership not proven; inspect dedicated gateway")
        child = owned[0]
        child.update(state="STOPPING", termination="forced", reason="gateway_unload_requested")
        self.change("process_stopping", child)

    def models_unloaded(self):
        for child in list(self.store.state["supervision"]["children"].values()):
            if child["purpose"] == "model_server" and child["state"] in {"RUNNING", "STOPPING"}:
                if self.inspect(child["pid"]) is not None:
                    raise RuntimeError("Model exit has not been observed")
                self.exited(child["child_id"], None, "forced")

    def observe(self, child, live_unload=False):
        """Observe one child. `live_unload` is set only by the live controller's own heartbeat.

        A delegated model the harness itself asked to stop (STOPPING) is expected to vanish; the heartbeat
        must not turn that into LOST and steal the terminal transition from models_unloaded(), which
        alone finalizes it after gateway/native emptiness is verified. Every other observer (resume,
        status, require_idle, reconcile) is unchanged, so an orphaned STOPPING record still becomes LOST.
        """
        value = copy.deepcopy(child)
        if value["state"] in {"EXITED", "STALE", "LOST", "RESOLVED"}:
            return value
        if value["boot"] != self.boot():
            value.update(state="LOST", reason="LOST_AFTER_REBOOT")
            return value
        if not value["pid"] or not value["identity"]:
            value.update(state="UNKNOWN", reason="launch_identity_unproven")
            return value
        try:
            actual = self.inspect(value["pid"])
        except (OSError, RuntimeError):
            value.update(state="UNKNOWN", reason="inspection_denied")
            return value
        if actual is None:
            if live_unload and value["state"] == "STOPPING" and value["purpose"] == "model_server":
                value.update(reason="stop_requested_exit_seen")
            else:
                value.update(state="LOST", reason="exit_unobserved")
        elif actual != value["identity"]:
            value.update(state="STALE", reason="PID_REUSED")
        else:
            # STOPPING survives controller death; do not pretend cleanup completed.
            value.update(state="STOPPING" if value["state"] == "STOPPING" else "RUNNING",
                         reason="identity_verified", last_heartbeat=self.clock())
        return value

    def reconcile(self, persist=True):
        children = [self.observe(child) for child in self.store.state["supervision"]["children"].values()]
        if persist:
            for child in children:
                old = self.child(child["child_id"])
                if child != old:
                    event = ("boot_session_changed" if child["reason"] == "LOST_AFTER_REBOOT" else
                             "process_identity_mismatch" if child["state"] == "STALE" else
                             "process_lost" if child["state"] == "LOST" else "resume_reconciliation")
                    self.change(event, child)
        return children

    def resolve_model(self, run_id, child_id, resolution, reason, confirmed, running, processes):
        """Explicit operator resolution of one ambiguous delegated model record.

        Never automatic, never signals a process, never touches checkpoints or work state.
        `running`/`processes` are the caller's fresh gateway and native-server observations.
        """
        from durable import DurableError
        if confirmed is not True:
            raise DurableError("Model recovery requires explicit --confirm; nothing changed")
        if run_id != self.store.state["run_id"]:
            raise DurableError("Model recovery run ID does not match the loaded durable run")
        if resolution not in RESOLUTIONS:
            raise DurableError("Unknown recovery resolution; nothing changed")
        if not valid_reason(reason):
            raise DurableError(f"Recovery reason must be 1-{MAX_REASON} printable characters without edge whitespace")
        if child_id not in self.store.state["supervision"]["children"]:
            raise DurableError("Recovery target child is not recorded in this run")
        if self.child(child_id)["purpose"] != "model_server":
            raise DurableError("Recovery applies only to delegated model_server children")
        if self.child(child_id)["state"] == "RESOLVED":
            raise DurableError("Model child is already operator-resolved; nothing changed")
        # Persist the observed UNKNOWN before resolving so the ledger shows refusal preceded intervention.
        self.reconcile()
        child = self.child(child_id)
        if child["state"] != "UNKNOWN":
            raise DurableError(f"Model child is {child['state']} ({child['reason']}), not an ambiguous UNKNOWN "
                               "record; manual recovery refused")
        if child["reason"] not in RECOVERABLE_REASONS:
            raise DurableError(f"UNKNOWN reason {child['reason']} is not eligible for manual recovery")
        if not isinstance(running, list) or not isinstance(processes, list) or running or processes:
            raise DurableError("Live gateway model or native server present; ownership cannot be attributed, "
                               "manual recovery refused")
        record = {"version": RESOLUTION_VERSION, "resolution": resolution, "reason": reason, "time": self.clock(),
                  "prior_state": child["state"], "prior_reason": child["reason"],
                  "revision": self.store.state["revision"]}
        child.update(state="RESOLVED", reason="operator_resolved", resolution=record)
        self.change("operator_model_resolution", child, detail={k: record[k] for k in
                    ("resolution", "reason", "prior_state", "prior_reason")})
        return self.child(child_id)

    def require_idle(self):
        from durable import DurableError
        children = self.reconcile()
        blocked = [c for c in children if c["state"] in {"STARTING", "RUNNING", "STOPPING", "UNKNOWN"}]
        if blocked:
            c = blocked[0]
            raise DurableError(f"PROCESS_RECOVERY: {c['child_id']} {c['state']} ({c['reason']}); "
                               "automatic resume refused; wait for owned child or inspect ambiguous ownership"
                               + ("; an operator may resolve a verified-absent model with recover-model"
                                  if c["purpose"] == "model_server" and c["state"] == "UNKNOWN" else ""))

    def terminate(self, child_id):
        from durable import DurableError
        child = self.observe(self.child(child_id))
        if child["state"] not in {"RUNNING", "STOPPING"} or child["containment"] != "job":
            raise DurableError("Refusing termination: live job ownership is not proven")
        child.update(state="STOPPING", termination="forced")
        self.change("process_stopping", child)
        # Rechecks boot and identity on a retained process handle, and job membership.
        winprocess.terminate_owned_job(child, self.boot)
        self.exited(child_id, 137, "forced")

    def heartbeat(self):
        with self.store.mutex:
            self._heartbeat()

    def _heartbeat(self):
        controller = {"pid": os.getpid(), "identity": self.inspect(os.getpid()),
                      "boot": self.boot(), "heartbeat": self.clock()}
        self.change("controller_heartbeat", controller=controller)
        for child in list(self.store.state["supervision"]["children"].values()):
            if child["state"] in {"RUNNING", "STOPPING"}:
                self.change("process_heartbeat", self.observe(child, live_unload=True))

    def __enter__(self):
        if self.entered or self.thread is not None:
            raise RuntimeError("Supervisor cannot be re-entered")

        self.closed = False
        self.shutdown_attempted = False
        self.failure = None
        self.failures = []
        self.stop_event.clear()
        self.worker_started.clear()
        self.thread = None
        self.state = "starting"
        start_returned = False
        try:
            self.heartbeat()
            def pulse():
                self.worker_started.set()
                while not self.stop_event.wait(getattr(self, "_pulse_interval", 15)):
                    try:
                        self.heartbeat()
                    except BaseException as exc:
                        self._record_failure(exc)
                        return
            thread = threading.Thread(target=pulse, name="durable-heartbeat", daemon=True)
            self.thread = thread
            thread.start()
            start_returned = True
            self.entered = True
            self.state = "running"
            return self
        except BaseException as exc:
            self.entered = False
            self._record_failure(exc)
            self.stop_event.set()
            if self.thread is not None:
                self.state = "startup_failed_uncertain"
                if self.worker_started.is_set() or start_returned:
                    try:
                        if self.thread.is_alive():
                            self.thread.join(timeout=getattr(self, "_startup_cleanup_timeout", 0.1))
                    except BaseException as join_err:
                        self._record_failure(join_err)
                    try:
                        if self.thread.is_alive():
                            self._record_failure(RuntimeError("Supervision heartbeat thread failed to stop"))
                        else:
                            self.state = "startup_failed_terminated"
                    except BaseException as alive_err:
                        self._record_failure(alive_err)
            else:
                self.state = "startup_failed_before_thread"
            raise

    def shutdown(self, *exc, timeout=5.0):
        if not self.entered and self.thread is None:
            raise RuntimeError("Supervisor was not entered")
        if self.closed:
            return
        self.shutdown_attempted = True
        self.stop_event.set()
        if self.thread is not None:
            can_join = self.worker_started.is_set() or self.entered
            if can_join:
                try:
                    alive = self.thread.is_alive()
                except BaseException as alive_err:
                    self._record_failure(alive_err)
                    alive = True
                if alive:
                    try:
                        self.thread.join(timeout=timeout)
                    except BaseException as join_err:
                        self._record_failure(join_err)
                    try:
                        if self.thread.is_alive():
                            self._record_failure(RuntimeError("Supervision heartbeat thread failed to stop"))
                    except BaseException as alive_err:
                        self._record_failure(alive_err)
        has_failure = (self.failure is not None) or (len(self.failures) > 0)
        if has_failure:
            self.state = "shutdown_failed"
            primary_failure = self.failure if self.failure is not None else self.failures[0]
            if not exc or exc[0] is None:
                raise RuntimeError("Supervision heartbeat persistence failed") from primary_failure
            if len(exc) > 1 and exc[1] is not None:
                try:
                    setattr(exc[1], "cleanup_error", primary_failure)
                except BaseException:
                    pass
                try:
                    setattr(exc[1], "cleanup_failures", list(self.failures))
                except BaseException:
                    pass
                try:
                    note_fn = getattr(exc[1], "add_note", None)
                    if callable(note_fn):
                        note_fn(f"Supervision heartbeat persistence failed: {primary_failure}")
                except BaseException:
                    pass
            return
        if self.thread is not None and not (self.worker_started.is_set() or self.entered):
            raise RuntimeError("Supervision heartbeat startup unconfirmed")
        self.closed = True
        self.state = "closed"

    def __exit__(self, *exc):
        if not self.entered and self.thread is None:
            raise RuntimeError("Supervisor was not entered")
        if self.closed:
            return
        self.shutdown(*exc)


def controller_status(controller, boot, clock=time.time):
    if not controller:
        return {"heartbeat": "absent", "boot_session": "unknown"}
    changed = controller["boot"] != boot
    age = clock() - controller["heartbeat"]
    return {"heartbeat": "stale" if changed or age < 0 or age > 45 else "fresh",
            "boot_session": "changed" if changed else "same"}


def validate_invocations(state, previous=None, transition=None):
    from durable import DurableError
    try:
        _validate_invocations(state, previous, transition)
    except (KeyError, ValueError, TypeError, IndexError, AttributeError) as exc:
        raise DurableError("Invalid verifier invocation; continuation refused") from exc


def _validate_invocations(state, previous=None, transition=None):
    """Validate the opt-in extension using existing child, plan and WAL records.

    A child token is the unique invocation ID. Process identity is stored ONLY
    in the existing Supervisor fields. This is consistency checking, not a new
    authority token or an authentication boundary against same-user tampering.
    """
    import math
    from durable import DurableError, digest
    children = state.get("supervision", {}).get("children", {})
    prior = (previous or {}).get("supervision", {}).get("children", {})
    seen = set()
    lifecycle_changes = 0
    phases = {"INTENT", "BOUND", "RELEASED", "COMPLETE"}
    events = {"INTENT": "invocation_intent", "BOUND": "invocation_bound",
              "RELEASED": "invocation_release", "COMPLETE": "invocation_complete"}
    def require(ok):
        if not ok:
            raise DurableError("Invalid or replayed verifier invocation; continuation refused")
    for cid, child in children.items():
        inv = child.get("invocation")
        if inv is None:
            require("invocation" not in child)
            continue
        require(type(inv) is dict and set(inv) == {
            "phase", "unit_id", "attempt_id", "verifier_id", "registry_sha256",
            "spec_sha256", "intended_argv", "resolved_argv", "candidate_fingerprint", "deadline"})
        require(child["purpose"] == "verification_command" and child["containment"] == "job")
        require(all(other["job"] != child["job"] for key, other in children.items() if key != cid))
        require(type(inv["phase"]) is str and inv["phase"] in phases)
        require(all(type(inv[k]) is str and inv[k] for k in ("unit_id", "attempt_id", "verifier_id")))
        require(all(type(inv[k]) is str and re.fullmatch(r"[0-9a-f]{64}", inv[k])
                    for k in ("registry_sha256", "spec_sha256", "candidate_fingerprint")))
        require(type(inv["deadline"]) in (int, float) and math.isfinite(inv["deadline"]) and inv["deadline"] > 0)
        registry = state["options"].get("verifier_registry")
        require(type(registry) is dict and digest(registry) == inv["registry_sha256"])
        unit = state.get("work_units", {}).get("units", {}).get(inv["unit_id"])
        require(type(unit) is dict and digest(unit["spec"]) == inv["spec_sha256"])
        require(inv["verifier_id"] in unit["spec"]["verifier_ids"])
        defn = registry.get(inv["verifier_id"])
        require(type(defn) is dict and inv["intended_argv"] == defn.get("argv"))
        for key in ("intended_argv", "resolved_argv"):
            require(type(inv[key]) is list and bool(inv[key]) and
                    all(type(arg) is str and arg and "\0" not in arg for arg in inv[key]))
        require(Path(inv["resolved_argv"][0]).is_absolute())
        require(inv["intended_argv"][1:] == inv["resolved_argv"][1:])
        require(checksum(inv["resolved_argv"]) == child["command_sha256"])
        require(os.path.normcase(str(Path(inv["resolved_argv"][0]).resolve())) ==
                os.path.normcase(child["executable"]))
        attempts = unit["attempts"]
        matching = [a for a in attempts if a["attempt_id"] == inv["attempt_id"]]
        require(len(matching) == 1)
        attempt = matching[0]
        second = attempt is not attempts[0]
        if len(attempts) != 1:
            # Attempt 1 must remain immutable and COMPLETE beside Attempt 2.
            # Second invocations require the separately selected qualification
            # lifecycle; every earlier operation keeps its original restriction.
            from durable import validate_repair_authorizations, _validate_repair_allocation
            validate_repair_authorizations(state)
            active = state.get("safe_qwen_active_recovery", {})
            ai = active.get("schema_version") in (5, 6, 7)
            if ai:
                from safe_qwen_active_grant import allocation_mode
                mode = allocation_mode(state)
                # Only immutable, already COMPLETE Attempt 1 invocations may
                # coexist with this allocated AI Attempt 2. No verifier launch.
                require((not second and inv["phase"] == "COMPLETE") or (second and active["schema_version"] == 7))
            else:
                mode = state["repair_recovery"]
            _validate_repair_allocation(state, state["repair_authorizations"]["repair-" + mode["recovery_id"]])
            require(len(attempts) == 2)
            if second:
                if ai:
                    v = active["second_verification"]
                    require(v is not None and v["binding"]["attempt_id"] == inv["attempt_id"]
                            and active["identity"]["unit_id"] == inv["unit_id"]
                            and v["before"]["fingerprint"] == inv["candidate_fingerprint"])
                    require(inv["deadline"] <= active["identity"]["deadline"])
                else:
                    v = state.get("repair_verification", {})
                    require(mode.get("operation") == "OFFLINE_VERIFY" and v.get("attempt_id") == inv["attempt_id"]
                            and v.get("unit_id") == inv["unit_id"] and v.get("candidate_fingerprint") == inv["candidate_fingerprint"])
                grant = state["repair_authorizations"]["repair-" + mode["recovery_id"]]
                require(inv["deadline"] <= min(mode["task_deadline"], grant["budget"]["unit_deadline"]))
                if inv["phase"] != "COMPLETE":
                    require(v["phase"] == "OBSERVING")
            else:
                require(inv["phase"] == "COMPLETE")
        else:
            require(not second)
        if inv["phase"] != "COMPLETE":
            require(unit["status"] == "VERIFYING" and unit["current_attempt"] == inv["attempt_id"]
                    and attempt["completed_at"] is None)
        key = (inv["unit_id"], inv["attempt_id"], inv["verifier_id"])
        require(key not in seen)
        seen.add(key)
        if inv["phase"] == "INTENT":
            require(child["pid"] is None and child["identity"] is None)
        else:
            require(child["pid"] is not None and child["identity"] is not None)
        if inv["phase"] == "COMPLETE":
            require(child["state"] == "EXITED" and type(child["exit_code"]) is int
                    and child["termination"] in {"natural", "forced"})
        if previous is not None:
            old = prior.get(cid)
            old_inv = old.get("invocation") if old else None
            if old_inv is None:
                require(old is None and inv["phase"] == "INTENT" and child["state"] == "STARTING")
                if second:
                    observations = v["observations"] if ai else attempt["repair_observation"]["observations"]
                    order = unit["spec"]["verifier_ids"]
                    require(len(observations) < len(order) and inv["verifier_id"] == order[len(observations)])
                    require(inv["deadline"] > v["checked_clock"])
            else:
                require({k: v for k, v in inv.items() if k != "phase"} ==
                        {k: v for k, v in old_inv.items() if k != "phase"})
                before, after = old_inv["phase"], inv["phase"]
                require(before == after or (before, after) in {
                    ("INTENT", "BOUND"), ("BOUND", "RELEASED"), ("RELEASED", "COMPLETE")})
                for k in ("token", "job", "boot", "controller_pid", "executable", "command_sha256"):
                    require(child[k] == old[k])
                if before != "INTENT":
                    require(child["pid"] == old["pid"] and child["identity"] == old["identity"])
                if before == "COMPLETE":
                    require(child == old)
            if old_inv is None or old_inv["phase"] != inv["phase"]:
                lifecycle_changes += 1
                require(transition == {"event": events[inv["phase"]], "child_id": cid})
    for cid, child in prior.items():
        if "invocation" in child:
            require(cid in children and "invocation" in children[cid])
    if transition and transition.get("event") in events.values():
        require(lifecycle_changes == 1)


def _validated_store(store):
    """Read existing WAL authority; never treat matching caller dictionaries as proof."""
    from durable import Store, DurableError, encoded
    if not isinstance(store, Store):
        raise DurableError("Invocation requires a genuine durable Store")
    loaded = Store(store.directory.parent, store.directory.name)
    loaded.load()
    if (loaded.tail or encoded(loaded.state) != encoded(store.state)
            or encoded(loaded.records) != encoded(store.records)):
        raise DurableError("Invocation WAL/state mismatch or incomplete WAL tail")


class _VerifierInvocation:
    """Ephemeral controller context; cannot be reconstructed from JSON for resume."""
    def __init__(self, scheduler, unit_id, attempt_id, verifier_id, *, _repair_controller=None, _active_controller=None):
        import math
        import shutil
        from durable import DurableError, digest, snapshot
        from harness import safe_command
        self.scheduler, self.supervisor = scheduler, scheduler.supervisor
        self.store, self.repo, self.clock = scheduler.store, scheduler.repo, scheduler.clock
        self.child_id = None
        self.repair_controller = _repair_controller
        self.active_controller = _active_controller
        if _active_controller is not None:
            from safe_qwen_active_recovery import _Controller
            if (type(_active_controller) is not _Controller or _repair_controller is not None
                    or getattr(self.store, "_safe_qwen_active_controller", None) is not _active_controller
                    or _active_controller.scheduler is not scheduler or not _active_controller.verify_selected):
                raise DurableError("Native verification requires registered ACTIVE v3 controller")
        if not isinstance(self.supervisor, Supervisor) or self.supervisor.store is not self.store:
            raise DurableError("Invocation requires this run's Supervisor")
        with self.store.mutex:
            _validated_store(self.store)
            if (str(self.repo.root) != self.store.state["target_repository"] or
                    self.store.directory.is_relative_to(Path(self.repo.root).resolve())):
                raise DurableError("Invocation repository/Store binding mismatch")
            if _active_controller is not None:
                from safe_qwen_active_verification import guard
                guard(_active_controller)
            elif _repair_controller is None:
                scheduler.guard_supervision()
            else:
                scheduler._guard_repair_verification(_repair_controller)
            if scheduler.reconciliation_stop is not None:
                raise DurableError("Invocation blocked by recovery reconciliation")
            section = scheduler.get_section()
            unit = section["units"].get(unit_id)
            active_second = _active_controller is not None and _active_controller.second_verification_started
            second = _repair_controller is not None or active_second
            index = 1 if second else 0
            if (not unit or unit["status"] != "VERIFYING" or unit["current_attempt"] != attempt_id
                    or len(unit["attempts"]) != (2 if second else 1) or unit["attempts"][index]["attempt_id"] != attempt_id
                    or unit["attempts"][index]["completed_at"] is not None
                    or (_repair_controller is not None and (_repair_controller.verification_context["unit_id"] != unit_id
                                    or _repair_controller.verification_context["attempt_id"] != attempt_id))
                    or (active_second and _active_controller.data["second_verification"] is None)
                    or verifier_id not in unit["spec"]["verifier_ids"]):
                raise DurableError("Invocation requires the active first attempt and registered verifier")
            self.registry = copy.deepcopy(self.store.state["options"].get("verifier_registry"))
            if type(self.registry) is not dict or self.registry != scheduler.verifier_registry:
                raise DurableError("Invocation requires the approved WAL registry")
            definition = self.registry.get(verifier_id)
            if not definition or not safe_command(definition.get("argv")):
                raise DurableError("Invocation command is not approved")
            intended = copy.deepcopy(definition["argv"])
            executable = shutil.which(intended[0])
            if not executable:
                raise DurableError("Invocation executable unavailable")
            resolved = [str(Path(executable).resolve()), *intended[1:]]
            manager = self.store.state.get("manager", {})
            if scheduler.budgets != manager.get("budgets"):
                raise DurableError("Invocation budgets differ from WAL")
            timing = unit["attempts"][index].get("stage_timing")
            if (not timing or timing.get("stage") != "VERIFYING" or timing.get("unit_id") != unit_id
                    or timing.get("attempt_id") != attempt_id):
                raise DurableError("Invocation lacks durable verification timing")
            now = self.clock()
            self.start_clock = now
            start = timing.get("stage_start_clock")
            elapsed = timing.get("attempt_elapsed_at_start")
            if any(type(n) not in (int, float) or not math.isfinite(n) or n < 0 for n in (now, start, elapsed)) or now < start:
                raise DurableError("Invocation timing is ambiguous")
            remaining = scheduler.remaining_unit_budget(unit, elapsed + now - start, attempt_id)
            limits = [remaining, scheduler.remaining_task_budget(now), definition.get("timeout_seconds", 30.0)]
            if _active_controller is not None:
                limits.extend([_active_controller.identity[k] - now for k in ("task_deadline", "unit_deadline", "deadline")])
            if _repair_controller is not None:
                grant = self.store.state["repair_authorizations"]["repair-" + _repair_controller.mode["recovery_id"]]
                limits.extend([_repair_controller.mode["task_deadline"] - now, grant["budget"]["unit_deadline"] - now])
            if any(type(n) not in (int, float) or not math.isfinite(n) or n <= 0 for n in limits):
                raise DurableError("Invocation cumulative budget exhausted or invalid")
            for child in self.store.state["supervision"]["children"].values():
                inv = child.get("invocation")
                if inv and ((inv["unit_id"], inv["attempt_id"], inv["verifier_id"]) ==
                            (unit_id, attempt_id, verifier_id) or inv["phase"] != "COMPLETE"):
                    raise DurableError("Duplicate or uncertain invocation; relaunch refused")
            self.intent = dict(phase="INTENT", unit_id=unit_id, attempt_id=attempt_id,
                               verifier_id=verifier_id, registry_sha256=digest(self.registry),
                               spec_sha256=digest(unit["spec"]), intended_argv=intended,
                               resolved_argv=resolved, candidate_fingerprint=snapshot(self.repo)["fingerprint"],
                               deadline=now + min(limits))
            self.child_id = self.supervisor.register("verification_command", resolved[0], resolved, invocation=self)

    def resume(self, native):
        import math
        from durable import DurableError, snapshot
        def check_deadline():
            now = self.clock()
            if (type(now) not in (int, float) or not math.isfinite(now)
                    or now < self.start_clock or now >= self.intent["deadline"]):
                raise DurableError("Invocation clock/deadline ambiguous or exhausted")
        with self.store.mutex:
            _validated_store(self.store)
            if self.active_controller is not None:
                from safe_qwen_active_verification import guard
                guard(self.active_controller, self.child_id)
            if self.repair_controller is not None:
                self.scheduler._guard_repair_verification(self.repair_controller, self.child_id)
            check_deadline()
            child = self.supervisor.child(self.child_id)
            if (child["invocation"]["phase"] != "BOUND" or child["state"] != "RUNNING"
                    or child["boot"] != self.supervisor.boot()
                    or snapshot(self.repo)["fingerprint"] != self.intent["candidate_fingerprint"]):
                raise DurableError("Invocation stale or unbound; resume refused")
            native.verify(child["identity"])
            child["invocation"]["phase"] = "RELEASED"
            self.supervisor.change("invocation_release", child)
            # WAL/cache failure above leaves the process suspended. Never retry
            # resume after an uncertain commit or a controller interruption.
            check_deadline()
            if self.active_controller is not None:
                guard(self.active_controller, self.child_id)
                check_deadline()
            if self.repair_controller is not None:
                self.scheduler._guard_repair_verification(self.repair_controller, self.child_id)
                check_deadline()
            native.resume(child["identity"])

    def complete(self):
        with self.store.mutex:
            child = self.supervisor.child(self.child_id)
            child["invocation"]["phase"] = "COMPLETE"
            self.supervisor.change("invocation_complete", child)


class VerifierEvidenceError(RuntimeError):
    """Observation failed closed; attached facts never authorize continuation."""
    def __init__(self, message, evidence):
        super().__init__(message)
        self.evidence = copy.deepcopy(evidence)


def _run_observed_verifier(supervisor, command, intended_argv, cwd, timeout, maximum,
                           progress=None, cancel_event=None, invocation=None):
    """Opt-in direct native lifecycle observation. No worker JSON is consumed.

    The controller creates a suspended verifier and its own kill-on-close Job.
    There is no broker in this topology. Returned facts are not retry approval;
    without the optional controller invocation, these facts remain unbound.
    "natural" means signaled without controller termination, not proof against
    external same-user termination or evidence that test assertions are honest.
    """
    import math
    import shutil
    import tempfile
    from contextlib import nullcontext
    if os.name != "nt":
        raise ValueError("Native verifier observation requires Windows")
    for name, argv in (("command", command), ("intended_argv", intended_argv)):
        if (not isinstance(argv, list) or not argv or
                any(not isinstance(a, str) or not a or "\0" in a for a in argv)):
            raise ValueError(f"{name} must be an explicit nonempty argv list")
    command, intended_argv = list(command), list(intended_argv)
    if not Path(command[0]).is_absolute() or command[1:] != intended_argv[1:]:
        raise ValueError("Resolved command requires an absolute executable and identical arguments")
    intended_executable = (intended_argv[0] if Path(intended_argv[0]).is_absolute()
                           else shutil.which(intended_argv[0]))
    if (not intended_executable or os.path.normcase(str(Path(intended_executable).resolve())) !=
            os.path.normcase(str(Path(command[0]).resolve()))):
        raise ValueError("Intended and resolved verifier executables disagree")
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Verifier timeout must be finite and positive")
    if type(maximum) is not int or maximum <= 0:
        raise ValueError("Verifier output bound must be a positive integer")
    child_id = (invocation.child_id if invocation is not None else
                supervisor.register("verification_command", command[0], command))
    child = supervisor.child(child_id)
    evidence = dict(schema_version=1, source="controller_native_handles", child_id=child_id,
                    broker=None, verifier=None, intended_argv=intended_argv,
                    intended_command_sha256=checksum(intended_argv), resolved_argv=command,
                    resolved_command_sha256=checksum(command), job=child["job"],
                    termination="launch_failed", observed_exit_code=None, cleanup="not_observed",
                    execution_started=False)
    start = time.monotonic()
    native = None
    with open(os.devnull, "rb") as inp, tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            try:
                second = invocation is not None and invocation.repair_controller is not None
                active = invocation.active_controller if invocation is not None else None
                with supervisor.store.mutex if second or active is not None else nullcontext():
                    if active is not None:
                        from safe_qwen_active_verification import guard
                        guard(active, child_id)
                        if (supervisor.child(child_id)["invocation"]["phase"] != "INTENT"
                                or time.monotonic() >= invocation.intent["deadline"]):
                            raise RuntimeError("ACTIVE verifier launch authority expired")
                    if second:
                        invocation.scheduler._guard_repair_verification(invocation.repair_controller, child_id)
                        if (supervisor.child(child_id)["invocation"]["phase"] != "INTENT"
                                or time.monotonic() >= invocation.intent["deadline"]):
                            raise RuntimeError("Qualification launch authority expired")
                    native = winprocess.ControllerVerifier(child["job"])
                    native.launch(command, cwd, (inp, out, err))
            except OSError:
                if native is None or native.pid is not None:
                    raise  # Containment/identity failure is not a mere launch failure.
            else:
                evidence["verifier"] = dict(pid=native.pid, identity=copy.deepcopy(native.identity))
                if invocation is None:
                    supervisor.started(child_id, native.pid)
                else:
                    supervisor.started(child_id, native.pid, native=native)
                if supervisor.child(child_id)["identity"] != native.identity:
                    raise RuntimeError("Supervisor and retained verifier identity disagree")
                evidence["termination"] = "natural"
                evidence["termination_basis"] = "signaled_without_controller_termination"
                deadline = (start + timeout if invocation is None else
                            min(start + timeout, invocation.intent["deadline"]))
                context = (progress.operation("COMMAND", "native verifier observation", timeout)
                           if progress is not None else nullcontext())
                with context:
                    while True:
                        code = native.wait(0)
                        if code is not None:
                            evidence["observed_exit_code"] = code
                            if time.monotonic() >= deadline:
                                evidence.update(termination="timeout", termination_basis="controller_deadline")
                            break
                        if cancel_event is not None and cancel_event.is_set():
                            evidence.update(termination="forced", termination_basis="controller_cancel")
                            break
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            evidence.update(termination="timeout", termination_basis="controller_deadline")
                            break
                        if not evidence["execution_started"]:
                            if invocation is None:
                                native.resume(native.identity)
                            else:
                                invocation.resume(native)
                            evidence["execution_started"] = True
                            continue
                        code = native.wait(min(0.05, remaining))
                        if code is not None:
                            evidence["observed_exit_code"] = code
                            if time.monotonic() >= deadline:
                                evidence.update(termination="timeout", termination_basis="controller_deadline")
                            break
            # Kill lingering descendants even when the verifier root exited naturally.
            native.terminate_job()
            native.drain()
            evidence["cleanup"] = "observed_empty"
            if native.process is not None and evidence["observed_exit_code"] is None:
                evidence["observed_exit_code"] = native.wait(10)
                if evidence["observed_exit_code"] is None:
                    raise RuntimeError("Drained job without signaled verifier handle")
            supervisor.exited(child_id, evidence["observed_exit_code"],
                              "forced" if evidence["termination"] in {"timeout", "forced"}
                              else evidence["termination"])
        except BaseException as exc:
            evidence["termination"] = "observation_failed"
            evidence["cleanup"] = "incomplete"
            try:
                if native is not None:
                    native.terminate_job()
                    native.drain()
                    evidence["cleanup"] = "observed_empty"
                    if native.process is not None:
                        evidence["observed_exit_code"] = native.wait(10)
            except BaseException as cleanup_exc:
                evidence["cleanup_error"] = type(cleanup_exc).__name__
            # No EXITED claim or success return after contradictory/missing observation.
            raise VerifierEvidenceError("Native verifier observation failed closed", evidence) from exc
        finally:
            if native is not None:
                native.close()
        outputs, sizes = [], []
        for stream in (out, err):
            stream.seek(0, 2)
            sizes.append(stream.tell())
            stream.seek(max(0, stream.tell() - maximum * 4))
            outputs.append(stream.read().decode("utf-8", errors="replace")[-maximum:])
    if invocation is not None:
        invocation.complete()
    return dict(exit_code=evidence["observed_exit_code"] if evidence["termination"] == "natural" else None,
                termination=evidence["termination"], stdout=outputs[0], stderr=outputs[1],
                output_truncated=any(size > maximum for size in sizes),
                duration_seconds=round(time.monotonic() - start, 2), controller_evidence=evidence)


def run_command(supervisor, command, cwd, timeout, maximum, *, progress=None,
                purpose="verification_command", env=None, stdin=None, opencode=False,
                observe_verifier=False, intended_argv=None, cancel_event=None):
    """Gate actual work on a flushed identity record; the broker owns its job."""
    if observe_verifier is not False:
        if observe_verifier is not True or purpose != "verification_command" or opencode or env is not None or stdin is not None:
            raise ValueError("Native observation is an explicit verifier-only opt-in")
        return _run_observed_verifier(supervisor, command, intended_argv, cwd, timeout, maximum,
                                     progress, cancel_event)
    if intended_argv is not None or cancel_event is not None:
        raise ValueError("Native verifier options require observe_verifier=True")
    from durable import atomic_write, encoded
    from contextlib import nullcontext
    def observe(label, operation, budget, alive=None):
        return progress.operation(label, operation, budget, alive=alive, alive_subject="broker") if progress is not None else nullcontext()
    child_id = supervisor.register(purpose, sys.executable, command)
    child = supervisor.child(child_id)
    directory = supervisor.store.directory / "processes" / child_id
    directory.mkdir(parents=True)
    spec = dict(command=command, cwd=str(cwd), timeout=timeout, token=child["token"], job=child["job"])
    if purpose == "opencode_executor":
        spec["deadline"] = time.monotonic() + timeout
    if opencode:
        spec.update(env=env, stdin=stdin, opencode=True)
    atomic_write(directory / "spec.json", encoded(spec))
    start = time.monotonic()
    try:
        proc = subprocess.Popen([sys.executable, str(Path(__file__).with_name("supervision_worker.py")), str(directory)],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=0x08000000 if os.name == "nt" else 0)
    except OSError:
        supervisor.exited(child_id, None, "launch_failed")
        raise
    # If persistence fails, no gate is released; broker times out without starting work.
    supervisor.started(child_id, proc.pid)
    atomic_write(directory / "go.json", encoded({"token": child["token"]}))
    try:
        with observe("OPENCODE" if purpose == "opencode_executor" else "COMMAND",
                     ("heartbeat" if purpose == "opencode_executor" else "run_command") + " (broker cleanup reserve=30s)", timeout,
                     alive=lambda: proc.poll() is None):
            proc.wait(timeout=timeout + 30)
    except BaseException as exc:
        # Includes interruption: never leave an owned executor/command tree behind.
        with observe("CLEANUP", "verification broker termination", 10, alive=lambda: proc.poll() is None):
            supervisor.terminate(child_id)
        with observe("CLEANUP", "verification broker exit wait", 10, alive=lambda: proc.poll() is None):
            proc.wait(timeout=10)
        if isinstance(exc, subprocess.TimeoutExpired):
            raise RuntimeError("Owned process broker exceeded its bounded deadline") from exc
        raise
    with observe("CLEANUP", "verification job drain", 10):
        winprocess.wait_job_empty(child["job"])
    try:
        receipt = json.loads((directory / "exit.json").read_text())
        if (receipt["token"] != child["token"] or (opencode and receipt.get("descendant_cleanup") != "job_close")
                or receipt["termination"] not in {"natural", "forced", "launch_failed"}
                or not (receipt["exit_code"] is None or type(receipt["exit_code"]) is int)):
            raise ValueError()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError("Owned process receipt/ownership could not be established") from exc
    supervisor.exited(child_id, receipt["exit_code"], receipt["termination"])
    if opencode:
        return dict(exit_code=receipt["exit_code"] if receipt["termination"] == "natural" else None,
                    termination=receipt["termination"], metadata=receipt.get("metadata", {}),
                    duration_seconds=round(time.monotonic() - start, 2))
    outputs = []
    sizes = []
    for name in ("stdout", "stderr"):
        with (directory / name).open("rb") as stream:
            stream.seek(0, 2)
            sizes.append(stream.tell())
            stream.seek(max(0, stream.tell() - maximum * 4))
            outputs.append(stream.read().decode("utf-8", errors="replace")[-maximum:])
    return dict(exit_code=receipt["exit_code"] if receipt["termination"] == "natural" else None,
                termination=receipt["termination"], stdout=outputs[0], stderr=outputs[1], output_truncated=any(s > maximum for s in sizes),
                duration_seconds=round(time.monotonic() - start, 2))
