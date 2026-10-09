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
        self.thread = None
        self.failure = None

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

    def register(self, purpose, executable, command, containment="job"):
        token = uuid.uuid4().hex
        child = dict(run_id=self.store.state["run_id"], child_id=f"{purpose}-{token[:12]}",
                     purpose=purpose, pid=None, controller_pid=os.getpid(),
                     executable=str(Path(executable).resolve()), command_sha256=checksum(command),
                     identity=None, started_at=self.clock(), token=token, state="STARTING",
                     boot=self.boot(), last_heartbeat=None, exit_code=None, termination=None,
                     containment=containment, job="Local\\harness-" + token, reason="launch_intent")
        self.change("process_registered", child)
        return child["child_id"]

    def child(self, child_id):
        return copy.deepcopy(self.store.state["supervision"]["children"][child_id])

    def started(self, child_id, pid):
        child = self.child(child_id)
        actual = self.inspect(pid)
        if actual is None:
            raise RuntimeError("Child exited before ownership could be established")
        if os.path.normcase(actual["executable"]) != os.path.normcase(child["executable"]):
            raise RuntimeError("Child executable differs from registered executable")
        child.update(pid=pid, identity=actual, state="RUNNING", last_heartbeat=self.clock(), reason="identity_verified")
        self.change("process_started", child)

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
        if config.get("executor") == "safe_qwen":
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
        self.heartbeat()
        def pulse():
            while not self.stop_event.wait(15):
                try:
                    self.heartbeat()
                except BaseException as exc:
                    self.failure = exc
                    return
        self.thread = threading.Thread(target=pulse, name="durable-heartbeat", daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop_event.set()
        self.thread.join()
        if self.failure and exc[0] is None:
            raise RuntimeError("Supervision heartbeat persistence failed") from self.failure


def controller_status(controller, boot, clock=time.time):
    if not controller:
        return {"heartbeat": "absent", "boot_session": "unknown"}
    changed = controller["boot"] != boot
    age = clock() - controller["heartbeat"]
    return {"heartbeat": "stale" if changed or age < 0 or age > 45 else "fresh",
            "boot_session": "changed" if changed else "same"}


def run_command(supervisor, command, cwd, timeout, maximum, *, progress=None,
                purpose="verification_command", env=None, stdin=None, opencode=False):
    """Gate actual work on a flushed identity record; the broker owns its job."""
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
