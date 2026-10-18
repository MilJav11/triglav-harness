"""Closed ACTIVE v2 Attempt 1 bridge. No grants, verifiers, retry or adoption.

The worker and its receipts have no authority. Only the registered live owner
can persist events from the reviewed controller's native/transport/tool seams.
Dormant identity-only v1 is neither used nor upgraded by this operation.
"""
import copy
from contextlib import contextmanager
import math
import os
from pathlib import Path
import threading
import time
import uuid

import durable
import environment
import work_units

KEY = "safe_qwen_active_recovery"
MARKER = "safe_qwen_active_event"
RESERVATION = "recovery/safe-qwen-active-v2.json"
MODEL_FIELDS = ("child_id run_id pid identity parent_pid parent_identity alias role containment boot executable command_sha256 token").split()
KINDS = {"INTENT", "RELEASE", "MODEL_INTENT", "MODEL_RESPONSE", "EDIT_INTENT", "EDIT_APPLIED", "TOOL_RESULT", "COMPLETE"}


def require(value):
    if not value:
        raise durable.DurableError("ACTIVE SafeQwen Attempt 1 contract refused")


def fields(value, names):
    require(type(value) is dict and set(value) == set(names.split()))


def sha(value):
    durable._repair_sha(value)


def sources():
    import supervised_safe_qwen as adapter
    result = adapter._sources()
    for name in ("safe_qwen_active_recovery.py", "durable.py", "terminal_run.py", "work_unit_scheduler.py"):
        result[name] = adapter.sha(Path(__file__).with_name(name).read_bytes())
    return result


def model_proof(state, child_id):
    child = state["supervision"]["children"][child_id]
    cfg = state["options"]["config"]
    require(child["purpose"] == "model_server" and child["role"] == "code"
            and child["alias"] == cfg["roles"]["code"] and child["containment"] == "delegated"
            and child["run_id"] == state["run_id"])
    return {k: child[k] for k in MODEL_FIELDS}


def project(data):
    """Deterministic parent ledger projection, never a worker DAG registration."""
    if data.get("schema_version") == 7:
        from safe_qwen_active_verification import dispatch_data
        return project(dispatch_data(data))
    if data.get("schema_version") == 6:
        from safe_qwen_active_dispatch import source_data
        return project(source_data(data))
    if data.get("schema_version") == 5:
        from safe_qwen_active_grant import grant_data
        return project(grant_data(data))
    if data.get("schema_version") == 4:
        from safe_qwen_active_grant import verification_data
        from safe_qwen_active_verification import project as verification_project
        return verification_project(verification_data(data))
    if data.get("schema_version") == 3:
        from safe_qwen_active_verification import project as verification_project
        return verification_project(data)
    return project_worker(data)


def project_worker(data):
    section = copy.deepcopy(data["source_section"])
    unit = section["units"][data["identity"]["unit_id"]]
    work_units.transition_unit(unit, "EXECUTING")
    att = work_units.create_attempt(unit, data["identity"]["attempt_id"])
    att["started_at"] = data["selected_at"]
    att["stage"] = {"INTENT": "ai_dispatch_intent", "RELEASED": "ai_dispatch_released", "COMPLETE": "ai_executor_complete_unverified"}[data["phase"]]
    att["ai_execution_id"] = data["identity"]["execution_id"]
    if data["result"] is not None:
        att["execution_result"] = {"active_v2_result_sha256": durable.digest(data["result"])}
        att["changed_paths"] = data["result"]["changed_paths"]
        att["timeout_info"] = {"elapsed_seconds": data["result"]["elapsed_seconds"]}
    unit["updated_at"] = data["updated_at"]
    work_units.seal_unit_state(unit)
    work_units._reseal_section(section)
    return section


def validate(state):
    if KEY not in state:
        return
    require(type(state[KEY]) is dict)
    if state[KEY].get("schema_version") == 7:
        from safe_qwen_active_verification import validate_second
        return validate_second(state)
    if state[KEY].get("schema_version") == 6:
        from safe_qwen_active_dispatch import validate as validate_dispatch_mode
        return validate_dispatch_mode(state)
    if state[KEY].get("schema_version") == 5:
        from safe_qwen_active_grant import validate_allocation
        return validate_allocation(state)
    if state[KEY].get("schema_version") == 4:
        from safe_qwen_active_grant import validate as validate_grant_mode
        return validate_grant_mode(state)
    if state[KEY].get("schema_version") == 3:
        from safe_qwen_active_verification import validate as validate_verification
        return validate_verification(state)
    try:
        from terminal_run import recovery_worker_spec
        from safe_executor_binding import digest as pin_digest
        d = state[KEY]
        fields(d, "schema_version mode identity source_section snapshot_before files_before phase selected_at updated_at events process result")
        require(type(d["schema_version"]) is int and d["schema_version"] == 2 and d["mode"] == "ACTIVE_ATTEMPT1")
        require(not any(k in state for k in ("safe_qwen_recovery_identity", "repair_recovery", "repair_authorizations",
                                            "repair_dispatch", "repair_verification", "terminal_acceptance")))
        require(state["last_verified_checkpoint"] is None and state["verified_progress"] == [])
        i = d["identity"]
        fields(i, "id run_id unit_id attempt_id execution_id repository parent_sha256 worker worker_sha256 config_sha256 "
                  "model_pin_sha256 registry_sha256 scope_sha256 environment_sha256 budgets_sha256 session started_clock "
                  "task_deadline unit_deadline deadline implementations")
        require(type(i["id"]) is str and len(i["id"]) == 32 and all(c in "0123456789abcdef" for c in i["id"]))
        require(type(i["run_id"]) is str and i["run_id"] == state["run_id"] and type(i["unit_id"]) is str
                and i["repository"] == state["target_repository"] and type(i["repository"]) is str
                and i["attempt_id"] == i["unit_id"] + "-attempt-1"
                and i["execution_id"] == durable.digest([i["id"], i["run_id"], i["unit_id"], 1]))
        source = d["source_section"]
        work_units.validate_section(source)
        require(source["sequence"] == [i["unit_id"]] and set(source["units"]) == {i["unit_id"]})
        unit = source["units"][i["unit_id"]]
        require(unit["status"] == "READY" and unit["attempts"] == [] and unit["current_attempt"] is None and unit["result"] is None)
        cfg = state["options"]["config"]
        pin = cfg["safe_qwen_identity"]
        require(cfg["executor"] == "safe_qwen" and pin["version"] == 2
                and pin["sha256"] == pin_digest({k: v for k, v in pin.items() if k != "sha256"})
                and pin["configuration_sha256"] == pin_digest({k: v for k, v in cfg.items() if k != "safe_qwen_identity"})
                and state["options"]["work_unit_executor"] == {"kind": "safe_qwen", "identity_sha256": pin["sha256"]})
        require(durable.encoded(i["worker"]) == durable.encoded(recovery_worker_spec(unit["spec"], cfg)))
        for key, value in (("parent_sha256", unit["spec"]), ("worker_sha256", i["worker"]), ("config_sha256", cfg),
                           ("registry_sha256", state["options"]["verifier_registry"]),
                           ("scope_sha256", {"parent": unit["spec"]["scope"], "manager": state["manager"]["scope"], "worker": pin["scope"]}),
                           ("environment_sha256", state["environment"]), ("budgets_sha256", state["manager"]["budgets"])):
            sha(i[key]); require(i[key] == durable.digest(value))
        sha(i["model_pin_sha256"]); require(i["model_pin_sha256"] == pin["sha256"])
        require(durable.encoded(i["session"]) == durable.encoded(durable._repair_session(state)))
        for key in ("started_clock", "task_deadline", "unit_deadline", "deadline"):
            require(type(i[key]) in (int, float) and math.isfinite(i[key]) and i[key] > 0)
        require(i["unit_deadline"] == i["started_clock"] + unit["spec"]["limits"]["timeout_seconds"]
                and i["started_clock"] < i["deadline"] <= min(i["unit_deadline"], i["task_deadline"])
                and i["task_deadline"] - i["started_clock"] <= state["manager"]["budgets"]["max_runtime_seconds"])
        require(type(i["implementations"]) is dict and set(i["implementations"]) == set(sources()))
        for digest in i["implementations"].values(): sha(digest)
        require(type(d["selected_at"]) is str and bool(d["selected_at"]) and type(d["updated_at"]) is str)
        before = d["snapshot_before"]
        require(before["repository"] == i["repository"] and before["fingerprint"] == durable.digest({k: v for k, v in before.items() if k != "fingerprint"}))
        require(type(d["files_before"]) is dict and bool(d["files_before"]))
        require(type(d["events"]) is list and 1 <= len(d["events"]) <= 200)
        validate_worker_facts(state, d, i)
        require(durable.encoded(state["work_units"]) == durable.encoded(project(d)))
        require(len(durable.encoded(d)) <= 1048576)
    except (KeyError, TypeError, ValueError, IndexError, OverflowError, AttributeError) as exc:
        raise durable.DurableError("Invalid ACTIVE v2 SafeQwen history") from exc


def validate_worker_facts(state, d, i):
    """Common closed worker facts; caller separately binds its logical ledger."""
    cfg = state["options"]["config"]
    pin = cfg["safe_qwen_identity"]
    before = d["snapshot_before"]
    expected = copy.deepcopy(d["files_before"])
    phase, pending, edit, response, models = None, None, None, None, 0
    process, previous_clock = None, i["started_clock"]
    for number, row in enumerate(d["events"], 1):
        fields(row, "number kind clock data")
        require(type(row["number"]) is int and row["number"] == number and type(row["kind"]) is str and row["kind"] in KINDS
                and type(row["clock"]) in (int, float) and math.isfinite(row["clock"]) and row["clock"] >= i["started_clock"])
        require(row["clock"] >= previous_clock)
        previous_clock = row["clock"]
        kind, value = row["kind"], row["data"]
        if kind != "COMPLETE": require(row["clock"] < i["deadline"])
        if kind == "INTENT":
            fields(value, "execution_id")
            require(number == 1 and value["execution_id"] == i["execution_id"]); phase = "INTENT"
        elif kind == "RELEASE":
            fields(value, "pid identity job membership session")
            fields(value["identity"], "created executable")
            require(phase == "INTENT" and process is None and type(value["pid"]) is int and value["pid"] > 0
                    and type(value["identity"]["created"]) is str and value["identity"]["created"].isdigit()
                    and value["identity"]["executable"] == os.path.normcase(state["environment"]["runtime"]["python"]["path"])
                    and value["membership"] is True and type(value["session"]) is str and len(value["session"]) == 32
                    and all(c in "0123456789abcdef" for c in value["session"])
                    and value["job"] == "Local\\safe-qwen-" + value["session"])
            process, phase = value, "RELEASED"
        elif kind == "MODEL_INTENT":
            fields(value, "request_number request_sha256 child_id model_sha256 files_sha256")
            require(phase == "RELEASED" and pending is None and edit is None
                    and type(value["request_number"]) is int and value["request_number"] == models + 1
                    and value["files_sha256"] == durable.digest(expected))
            sha(value["request_sha256"]); sha(value["model_sha256"])
            require(type(value["child_id"]) is str and value["model_sha256"] == durable.digest(model_proof(state, value["child_id"])))
            models += 1; pending, response = "MODEL", None
            require(models <= min(32, cfg["safe_qwen_policy"]["max_actions"]))
        elif kind == "MODEL_RESPONSE":
            fields(value, "request_number content_sha256")
            require(pending == "MODEL" and type(value["request_number"]) is int and value["request_number"] == models)
            sha(value["content_sha256"]); response, pending = value["content_sha256"], "TOOL"
        elif kind == "EDIT_INTENT":
            fields(value, "action path before_sha256 after_sha256 after_size content_sha256")
            path = value["path"]
            require(pending == "TOOL" and edit is None and value["action"] == models and type(value["action"]) is int
                    and type(path) is str and path in pin["scope"]["writable"] and path in expected
                    and len(expected[path]) == 3 and value["before_sha256"] == expected[path][0]
                    and value["content_sha256"] == response and type(value["after_size"]) is int and 0 < value["after_size"] <= 65536)
            sha(value["after_sha256"]); edit = value
        elif kind == "EDIT_APPLIED":
            fields(value, "action path after_sha256")
            require(edit is not None and value == {k: edit[k] for k in ("action", "path", "after_sha256")})
            expected[edit["path"]] = [edit["after_sha256"], edit["after_size"], expected[edit["path"]][2]]
            edit = None
        elif kind == "TOOL_RESULT":
            fields(value, "action ok tool")
            require(pending == "TOOL" and edit is None and type(value["action"]) is int and value["action"] == models
                    and type(value["ok"]) is bool and value["tool"] in (None, "read_files", "edit_file", "submit"))
            pending, response = None, None
        else:
            fields(value, "result_sha256")
            require(number == len(d["events"]) and phase in ("INTENT", "RELEASED") and d["result"] is not None
                    and value["result_sha256"] == durable.digest(d["result"]))
            phase = "COMPLETE"
    require(d["phase"] == phase and d["process"] == process)
    if phase == "COMPLETE":
        result = d["result"]
        fields(result, "outcome evidence candidate files_after changed_paths elapsed_seconds cleanup_proof")
        require(result["outcome"] in ("executor_succeeded_unverified", "executor_failed_unverified")
                and type(result["elapsed_seconds"]) in (int, float) and math.isfinite(result["elapsed_seconds"]) and result["elapsed_seconds"] >= 0)
        require(result["files_after"] == expected and result["changed_paths"] == sorted(p for p in expected if expected[p] != d["files_before"][p]))
        candidate = result["candidate"]
        require(candidate["repository"] == i["repository"] and candidate["fingerprint"] == durable.digest({k: v for k, v in candidate.items() if k != "fingerprint"}))
        require(all(candidate[k] == before[k] for k in ("head", "branch", "git_directory", "git_identity", "index_sha256")))
        evidence = result["evidence"]
        require(type(evidence) is dict and len(durable.encoded(evidence)) <= 65536 and evidence["trusted"] is False
                and evidence["unit_id"] == i["unit_id"] and evidence["attempt_number"] == 1
                and evidence["process"] == (None if process is None else {k: process[k] for k in ("pid", "identity")}))
        import supervised_safe_qwen as adapter
        require(evidence["schema_version"] == 1 and type(evidence["schema_version"]) is int
                and evidence["source"] == "controller_native_handles" and evidence["repository"] == i["repository"]
                and evidence["configuration_sha256"] == adapter.sha(adapter._json(cfg)) and evidence["model"] == cfg["roles"]["code"]
                and evidence["sources"] == {k: i["implementations"][k] for k in adapter._sources()}
                and evidence["before_snapshot_sha256"] == adapter.sha(adapter._json(d["files_before"]))
                and evidence["after_snapshot_sha256"] == adapter.sha(adapter._json(result["files_after"]))
                and type(evidence["model_requests"]) is int and evidence["model_requests"] == models
                and evidence["scope_verified"] is True and evidence["changed_paths"] == result["changed_paths"])
        transport = evidence["transport"]
        require(transport["kind"] == "owned_gateway" and transport["run_id"] == i["run_id"] and transport["attempt_id"] == i["attempt_id"])
        if process is not None:
            require(evidence["session"] == process["session"] and evidence["job"] == process["job"]
                    and evidence["identity_verified"] is True and evidence["execution_started"] is True)
        if transport["model"] is not None:
            proof = model_proof(state, transport["model"]["child_id"])
            require(transport["model"] == proof and transport["parent"] == {"pid": proof["parent_pid"], "identity": proof["parent_identity"]})
        if result["outcome"] == "executor_succeeded_unverified":
            require(pending is None and edit is None and evidence["success"] is True and process is not None
                    and evidence["cleanup"] == "observed_empty" and evidence["scope_verified"] is True
                    and evidence["transport"]["model_cleanup"] == "observed_idle"
                    and result["cleanup_proof"] == {"pid": process["pid"], "exit_code": 0, "empty_job": True})
            intents = [r["data"] for r in d["events"] if r["kind"] == "MODEL_INTENT"]
            require(len(transport["requests"]) == models and evidence["observed_exit_code"] == 0)
            for intent, request in zip(intents, transport["requests"]):
                require(request["wire_valid"] is True and request["number"] == intent["request_number"]
                        and request["child_id"] == intent["child_id"] and request["request_sha256"] == intent["request_sha256"]
                        and request["deadline"] == i["deadline"] and 0 < request["timeout_seconds"] <= cfg["request_timeout_seconds"])
    else: require(d["result"] is None)


def validate_history(records, read_evidence, tail=b"", reservation_exists=False):
    previous, selected = {}, None
    for record in records:
        marker = record.get(MARKER)
        require(marker is None or type(marker) is dict)
        if marker is not None:
            fields(marker, "event execution_id")
            require(type(marker["event"]) is str and type(marker["execution_id"]) is str)
        require(record["event"] == "state_committed" or marker is None)
        if record["event"] != "state_committed": continue
        state = record["state"]
        data = state.get(KEY)
        old = previous.get(KEY)
        if data is None:
            require(old is None and selected is None and marker is None)
        else:
            validate(state)
            if old is None:
                require(selected is None and previous and marker == {"event": "INTENT", "execution_id": data["identity"]["execution_id"]}
                        and data["source_section"] == previous["work_units"] and len(data["events"]) == 1)
                selected = copy.deepcopy(data)
                require(read_evidence({"path": RESERVATION, "sha256": durable.digest(selected)}) == selected)
            elif marker is None:
                require(durable.encoded(old) == durable.encoded(data))
            elif marker.get("event", "").startswith("AI_"):
                from safe_qwen_active_dispatch import validate_step as validate_dispatch_step
                validate_dispatch_step(previous, state, marker, records)
            elif marker.get("event", "").startswith(("VERIFY_", "VERIFY2_")):
                from safe_qwen_active_verification import validate_step
                validate_step(previous, state, marker, records)
            else:
                require(old["phase"] != "COMPLETE" and len(data["events"]) == len(old["events"]) + 1
                        and data["events"][:-1] == old["events"]
                        and marker == {"event": data["events"][-1]["kind"], "execution_id": data["identity"]["execution_id"]})
                for k in ("schema_version", "mode", "identity", "source_section", "snapshot_before", "files_before", "selected_at"):
                    require(durable.encoded(data[k]) == durable.encoded(old[k]))
                if data["schema_version"] in (3, 4, 5, 6, 7):
                    require(data["verification"] is None and old["verification"] is None
                            and data["verification_contract"] == old["verification_contract"])
                if data["schema_version"] in (4, 5, 6, 7):
                    require(data["grant_contract"] == old["grant_contract"])
                if data["schema_version"] in (5, 6, 7):
                    require(data["allocation_contract"] == old["allocation_contract"])
                if data["schema_version"] in (6, 7):
                    require(data["dispatch_contract"] == old["dispatch_contract"] and data["dispatch"] is None and old["dispatch"] is None)
            if data["schema_version"] == 7:
                require(data["second_verification_contract"] == selected["second_verification_contract"])
                if old is None:
                    require(data["second_verification"] is None)
                elif marker is None or not marker["event"].startswith("VERIFY2_"):
                    require(data["second_verification"] == old["second_verification"])
            if marker is not None:
                fields(record, "sequence time event previous sha256 state transition safe_qwen_active_event")
                require(record["transition"] is None)
                ignored = {"revision", "updated_at", KEY, "work_units", "manager"}
                require({k: v for k, v in state.items() if k not in ignored} == {k: v for k, v in previous.items() if k not in ignored})
                manager = copy.deepcopy(state["manager"])
                manager["runtime_seconds"] = previous["manager"]["runtime_seconds"]
                require(manager == previous["manager"] and state["manager"]["runtime_seconds"] >= previous["manager"]["runtime_seconds"])
            else:
                if state["work_units"] != previous["work_units"]:
                    # The shared repair history validator checks the complete atomic
                    # transaction, adjacency, immutable source and exact allocation.
                    require(data["schema_version"] in (5, 6, 7) and record.get("repair_authorization") == {
                        "event": durable._REPAIR_CONSUME_EVENT, "authorization_id": "repair-" + data["identity"]["id"],
                        "attempt_id": data["allocation_contract"]["attempt_id"]})
        previous = state
    require(bool(selected) == bool(reservation_exists) and not ((selected is not None or reservation_exists) and tail))


def owns_record(store, event, value):
    owner = getattr(store, "_safe_qwen_active_controller", None)
    return (type(owner) is _Controller and owner.store is store and not owner.closed
            and owner.pending is not None and durable.encoded(owner.pending) == durable.encoded((event, value)))


class _Controller:
    """Ephemeral registered owner; never returned to a caller or reconstructed."""
    def __init__(self, scheduler, unit_id, gateway, timeout_seconds, cancel_event, *, verify=False, diagnose=False, grant=False, allocate=False, dispatch=False, verify_second=False):
        from work_unit_scheduler import WorkUnitScheduler
        from integration_shim import SafeQwenExecutor
        from supervision import Supervisor, _validated_store
        from safe_executor_binding import validate_pinned, gateway_config
        from terminal_run import recovery_worker_spec, validate_safe_qwen_unit_contract
        import harness
        import manager
        require(type(scheduler) is WorkUnitScheduler and type(unit_id) is str
                and type(scheduler.store) is durable.Store and scheduler.clock is time.monotonic
                and type(gateway) is harness.Gateway and type(scheduler.supervisor) is Supervisor
                and gateway.supervisor is scheduler.supervisor and gateway.supervisor.store is scheduler.store
                and (cancel_event is None or type(cancel_event) is threading.Event)
                and type(timeout_seconds) in (int, float) and math.isfinite(timeout_seconds) and 0 < timeout_seconds <= 600)
        self.scheduler, self.store, self.repo, self.gateway = scheduler, scheduler.store, scheduler.repo, gateway
        self.executor = getattr(self.store, "_effective_work_unit_executor", None)
        self.closed, self.pending, self.binding, self.cleanup = False, None, None, None
        self.thread_id = threading.get_ident()
        self.network_thread = None
        self.native_binding = None
        self.cancel_event, self.loaded_sources = cancel_event if cancel_event is not None else threading.Event(), sources()
        require(type(verify) is bool)
        self.verify_selected = verify
        require(type(diagnose) is bool and (not diagnose or verify))
        self.diagnostic_selected, self.diagnostic_started = diagnose, False
        require(type(grant) is bool and (not grant or (verify and not diagnose)))
        self.grant_selected, self.grant_started, self.grant_writing = grant, False, False
        require(type(allocate) is bool and (not allocate or (grant and verify and not diagnose)))
        self.allocation_selected, self.allocation_started, self.allocation_contract = allocate, False, None
        require(type(dispatch) is bool and (not dispatch or allocate))
        self.dispatch_selected, self.dispatch_started, self.dispatch_contract = dispatch, False, None
        require(type(verify_second) is bool and (not verify_second or dispatch))
        self.second_verify_selected = verify_second
        self.second_verification_started = False
        self.second_verification_contract = None
        self.dispatch_execution_started = False
        self.allocation_record = None
        self.grant_record = None
        self.grant_contract = None
        self.diagnostic_contract = None
        if diagnose:
            from safe_qwen_active_eligibility import contract
            self.diagnostic_contract = contract()
        require(not getattr(self.store, "_repair_write_failed", False)
                and getattr(self.store, "_safe_qwen_active_controller", None) is None
                and not any(k in self.store.state for k in (KEY, "safe_qwen_recovery_identity", "repair_recovery", "repair_authorizations"))
                and not (self.store.directory / RESERVATION).exists())
        _validated_store(self.store)
        require(type(self.executor) is SafeQwenExecutor and type(self.repo) is manager.ScopedRepository
                and self.repo.store is self.store and self.repo.root == self.executor.policy.root
                and self.executor.gateway is gateway and gateway.active_role is None and gateway.active_model_child_id is None)
        cfg = self.store.state["options"]["config"]
        validate_pinned(cfg, self.executor)
        require(self.repo.config == cfg == gateway_config(gateway)
                and scheduler.budgets == self.store.state["manager"]["budgets"]
                and scheduler.parent_scope == self.store.state["manager"]["scope"]
                and scheduler.verifier_registry == self.store.state["options"]["verifier_registry"])
        sup = scheduler.supervisor
        require(sup.entered and not sup.closed and sup.failure is None and sup.thread is not None and sup.thread.is_alive()
                and sup.worker_started.is_set())
        sup.require_idle(); sup.heartbeat()
        live = self.store.state["supervision"]["controller"]
        require(live["pid"] == os.getpid() and live["identity"] == durable.winprocess.identity(os.getpid())
                and live["boot"] == durable.winprocess.boot_identity())
        manager.validate_repository(self.store, self.repo)
        require(environment.compare(self.store.state["environment"], durable.current_fingerprint(self.store, executor=self.executor))["decision"] in ("MATCH", "INFORMATIONAL"))
        section = self.store.state["work_units"]
        unit = section["units"][unit_id]
        require(section["sequence"] == [unit_id] and unit["status"] == "READY" and unit["attempts"] == [])
        worker = recovery_worker_spec(unit["spec"], cfg)
        validate_safe_qwen_unit_contract(worker, cfg, self.executor, self.store)
        started = scheduler.clock()
        task_deadline = started + scheduler.remaining_task_budget(started)
        unit_deadline = started + unit["spec"]["limits"]["timeout_seconds"]
        deadline = min(task_deadline, unit_deadline, started + timeout_seconds, started + cfg["timeout_seconds"])
        if gateway.execution_deadline is not None: deadline = min(deadline, gateway.execution_deadline)
        require(deadline > started and not (cancel_event and cancel_event.is_set()))
        token = uuid.uuid4().hex
        i = dict(id=token, run_id=self.store.state["run_id"], unit_id=unit_id, attempt_id=unit_id + "-attempt-1",
                 execution_id=durable.digest([token, self.store.state["run_id"], unit_id, 1]), repository=str(self.repo.root),
                 parent_sha256=durable.digest(unit["spec"]), worker=worker, worker_sha256=durable.digest(worker),
                 config_sha256=durable.digest(cfg), model_pin_sha256=cfg["safe_qwen_identity"]["sha256"],
                 registry_sha256=durable.digest(scheduler.verifier_registry),
                 scope_sha256=durable.digest({"parent": unit["spec"]["scope"], "manager": scheduler.parent_scope,
                                              "worker": cfg["safe_qwen_identity"]["scope"]}),
                 environment_sha256=durable.digest(self.store.state["environment"]), budgets_sha256=durable.digest(scheduler.budgets),
                 session=durable._repair_session(self.store.state), started_clock=started, task_deadline=task_deadline,
                 unit_deadline=unit_deadline, deadline=deadline, implementations=self.loaded_sources)
        stamp = durable.now()
        from offline_scripted_executor import _snapshot
        candidate = durable.snapshot(self.repo)
        self.data = dict(schema_version=2, mode="ACTIVE_ATTEMPT1", identity=i, source_section=copy.deepcopy(section),
                         snapshot_before=candidate, files_before=_snapshot(self.repo.root), phase="INTENT", selected_at=stamp,
                         updated_at=stamp, events=[dict(number=1, kind="INTENT", clock=started, data={"execution_id": i["execution_id"]})],
                         process=None, result=None)
        self.identity = copy.deepcopy(i)
        if verify:
            from safe_qwen_active_verification import contract
            self.data.update(schema_version=3, mode="ACTIVE_ATTEMPT1_VERIFY", verification_contract=contract(), verification=None)
        if grant:
            from safe_qwen_active_grant import contract
            self.grant_contract = contract()
            self.data.update(schema_version=4, mode="ACTIVE_ATTEMPT1_GRANT", grant_contract=copy.deepcopy(self.grant_contract))
        if allocate:
            from safe_qwen_active_grant import allocation_contract
            self.allocation_contract = allocation_contract(i)
            self.data.update(schema_version=5, mode="ACTIVE_REPAIR_ALLOCATE", allocation_contract=copy.deepcopy(self.allocation_contract))
        if dispatch:
            from safe_qwen_active_dispatch import contract, MODE
            self.dispatch_contract = contract()
            self.data.update(schema_version=6, mode=MODE, dispatch_contract=copy.deepcopy(self.dispatch_contract), dispatch=None)

        if verify_second:
            from safe_qwen_active_verification import second_contract, SECOND_MODE
            self.second_verification_contract = second_contract()
            self.data.update(schema_version=7, mode=SECOND_MODE,
                             second_verification_contract=copy.deepcopy(self.second_verification_contract), second_verification=None)

    def repairing(self):
        return self.dispatch_selected and self.data.get("dispatch") is not None

    def worker_state(self):
        return self.data["dispatch"] if self.repairing() else self.data

    def worker_context(self):
        require(self.repairing())
        from safe_qwen_active_dispatch import require_owner
        require_owner(self)
        d = self.data["dispatch"]
        return dict(schema_version=1, attempt_id=d["attempt_id"], execution_id=d["execution_id"],
                    repair_evidence=copy.deepcopy(d["repair_evidence"]))

    def live(self, *, budget=False):
        from supervision import _validated_store
        from safe_executor_binding import validate_pinned, gateway_config
        require(type(self) is _Controller and getattr(self.store, "_safe_qwen_active_controller", None) is self
                and not self.closed and not self.store._repair_write_failed and self.pending is None
                and self.scheduler.store is self.store and self.scheduler.repo is self.repo
                and self.gateway.supervisor is self.scheduler.supervisor and self.scheduler.supervisor.store is self.store)
        _validated_store(self.store)
        require(durable.encoded(self.store.state[KEY]) == durable.encoded(self.data)
                and self.data["identity"] == self.identity and self.loaded_sources == sources())
        cfg, sup = self.store.state["options"]["config"], self.scheduler.supervisor
        validate_pinned(cfg, self.executor)
        live = self.store.state["supervision"]["controller"]
        require(live["pid"] == os.getpid() and live["identity"] == durable.winprocess.identity(os.getpid())
                and live["boot"] == durable.winprocess.boot_identity()
                and sup.entered and not sup.closed and sup.failure is None and sup.thread.is_alive()
                and self.repo.store is self.store and str(self.repo.root) == self.identity["repository"]
                and self.repo.config == self.executor.config == gateway_config(self.gateway) == cfg
                and getattr(self.store, "_effective_work_unit_executor", None) is self.executor
                and self.executor.gateway is self.gateway
                and environment.compare(self.store.state["environment"], durable.current_fingerprint(self.store, executor=self.executor))["decision"] in ("MATCH", "INFORMATIONAL"))
        if self.data["phase"] == "INTENT":
            from offline_scripted_executor import _snapshot
            require(durable.snapshot(self.repo) == self.data["snapshot_before"]
                    and _snapshot(self.repo.root) == self.data["files_before"])
        if budget:
            require(time.monotonic() < self.identity["deadline"] and not (self.cancel_event and self.cancel_event.is_set())
                    and self.worker_state()["phase"] != "COMPLETE")

    @contextmanager
    def boundary(self):
        # Short native release / local mutation boundaries remain exclusive.
        with self.store.mutex:
            self.live(budget=True)
            yield

    @contextmanager
    def network_boundary(self):
        # The durable intent already exists. Reserve the controller operation,
        # not the WAL mutex, during bounded transport I/O. Only Supervisor state
        # changes may interleave; every response is revalidated before use.
        with self.store.mutex:
            self.live(budget=True)
            require(self.network_thread is None and threading.get_ident() == self.thread_id)
            self.network_thread = self.thread_id
        try:
            yield
            with self.store.mutex:
                self.live(budget=True)
        finally:
            with self.store.mutex:
                self.network_thread = None

    def guard_state_commit(self, proposed, transition):
        # Called only for records which did NOT match the pending owner write.
        require(self.pending is None)
        # No second controller may change execution state between an intent and
        # its effect. Heartbeats and this controller's Supervisor lifecycle are
        # the only permitted interleaving state commits.
        current = self.store.state
        ignored = {"revision", "updated_at", "supervision", "supervision_event"}
        require(durable.encoded({k: v for k, v in proposed.items() if k not in ignored}) ==
                durable.encoded({k: v for k, v in current.items() if k not in ignored}))
        require(type(transition) is dict)
        pulse = self.scheduler.supervisor.thread
        tid = threading.get_ident()
        require(tid == self.thread_id or (pulse is not None and tid == pulse.ident
                and transition.get("event") in {"controller_heartbeat", "process_heartbeat"}))

    def worker_contract(self, unit, binding):
        self.live()
        if self.repairing():
            from safe_qwen_active_dispatch import require_owner
            require_owner(self)
            from safe_qwen_active_dispatch import check_repository
            # Repository drift stops effects; it must not prevent safe unload
            # of the same retained, owned model after native worker cleanup.
            if not binding.finishing:
                check_repository(self)
            require(self.scheduler.budgets == self.store.state["manager"]["budgets"]
                    and self.scheduler.parent_scope == self.store.state["manager"]["scope"]
                    and self.scheduler.verifier_registry == self.store.state["options"]["verifier_registry"])
            require(binding.recovery is self and binding.store is self.store and binding.gateway is self.gateway
                    and durable.encoded(unit) == durable.encoded(self.identity["worker"])
                    and self.data["dispatch"]["phase"] in ("INTENT", "RELEASED")
                    and self.store.state["work_units"]["units"][self.identity["unit_id"]]["current_attempt"] == self.data["allocation_contract"]["attempt_id"])
            return
        require(binding.recovery is self and binding.store is self.store and binding.gateway is self.gateway
                and durable.encoded(unit) == durable.encoded(self.identity["worker"])
                and self.store.state["work_units"] == project(self.data)
                and self.data["phase"] in ("INTENT", "RELEASED"))

    def write(self, data, *, event=None):
        state = copy.deepcopy(self.store.state)
        state.update({KEY: copy.deepcopy(data), "work_units": project(data), "revision": state["revision"] + 1, "updated_at": durable.now()})
        state["manager"]["runtime_seconds"] = max(state["manager"]["runtime_seconds"], self.scheduler.runtime())
        second = event is not None and event.startswith("VERIFY2_")
        if second:
            from safe_qwen_active_verification import second_project
            state["work_units"] = second_project(state)
        marker = {"event": event or data["events"][-1]["kind"],
                  "execution_id": data["allocation_contract"]["execution_id"] if second else self.identity["execution_id"]}
        self.pending = ("state_committed", dict(state=state, transition=None, **{MARKER: marker}))
        try:
            durable.validate_state(state)
            self.store.append(*self.pending[:1], **self.pending[1])
            self.store.state = copy.deepcopy(state)
            self.data = copy.deepcopy(data)
            durable.atomic_write(self.store.directory / "state.json", durable.encoded(state) + b"\n")
        except BaseException:
            self.persistence_failed()
            raise
        finally:
            self.pending = None

    def persistence_failed(self):
        self.closed = True
        self.store._repair_write_failed = True
        self.cancel_event.set()

    def event(self, kind, value):
        if self.repairing():
            from safe_qwen_active_dispatch import event
            return event(self, kind, value)
        self.live(budget=kind != "COMPLETE")
        data = copy.deepcopy(self.data)
        data["events"].append(dict(number=len(data["events"]) + 1, kind=kind, clock=time.monotonic(), data=copy.deepcopy(value)))
        data["updated_at"] = durable.now()
        if kind == "RELEASE": data.update(process=copy.deepcopy(value), phase="RELEASED")
        self.write(data)

    def audit(self, record, native, evidence, pending_raw):
        """Only called inside the reviewed adapter; no caller-installed hooks."""
        from safe_qwen_worker import sha as text_sha
        from tool_protocol import parse
        self.live()
        event = record.get("event")
        if event == "launch_intent":
            require(self.worker_state()["phase"] == "INTENT" and len(self.worker_state()["events"]) == 1
                    and evidence["sources"] == {k: self.loaded_sources[k] for k in evidence["sources"]})
        elif event == "release":
            require(self.binding is not None and native is not None and self.worker_state()["process"] is None)
            native.verify(native.identity)
            self.native_binding = (native, native.process, native.job.handle)
            self.event("RELEASE", dict(pid=native.pid, identity=copy.deepcopy(native.identity), job=evidence["job"],
                                       membership=True, session=evidence["session"]))
        elif event == "model_request_intent":
            # No Gateway I/O under the intent/mutation mutex. The owned binding
            # verifies the Gateway outside this short boundary before request.
            self.binding.fast_check()
            self.binding._verify_handles()
            require(self.binding.model == model_proof(self.store.state, record["child_id"]))
            from offline_scripted_executor import _snapshot
            self.event("MODEL_INTENT", dict(request_number=record["number"], request_sha256=record["request_sha256"],
                                            child_id=record["child_id"], model_sha256=durable.digest(self.binding.model),
                                            files_sha256=durable.digest(_snapshot(self.repo.root))))
        elif event == "model_request":
            require(record["wire_valid"] is True and type(pending_raw) is str)
            self.event("MODEL_RESPONSE", dict(request_number=record["number"], content_sha256=text_sha(pending_raw.encode())))
        elif record.get("phase") == "intent":
            args = parse(pending_raw)["arguments"]
            require(record["path"] == args["path"])
            _, before = self.executor.policy.read(record["path"], write=True)
            require(text_sha(before) == record["before_sha256"] == args["before_sha256"] and before.decode().count(args["old_text"]) == 1)
            after = before.decode().replace(args["old_text"], args["new_text"], 1).encode()
            require(text_sha(after) == record["after_sha256"])
            self.event("EDIT_INTENT", dict(action=record["action"], path=record["path"], before_sha256=record["before_sha256"],
                                          after_sha256=record["after_sha256"], after_size=len(after), content_sha256=text_sha(pending_raw.encode())))
        elif record.get("phase") == "applied":
            self.event("EDIT_APPLIED", {k: record[k] for k in ("action", "path", "after_sha256")})
        elif "action" in record:
            self.event("TOOL_RESULT", dict(action=record["action"], ok=record["ok"], tool=record.get("tool")))

    def native_cleaned(self, native, code):
        # Observe retained handles while they still exist, not a returned receipt.
        require(self.worker_state()["process"] is not None and native.pid == self.worker_state()["process"]["pid"]
                and native.identity == self.worker_state()["process"]["identity"] and self.native_binding is not None
                and self.native_binding[0] is native and self.native_binding[1:] == (native.process, native.job.handle))
        # Membership was verified on these retained handles before release.
        # Exited processes leave their Job; cleanup proves exit + empty Job,
        # rather than attempting the live pre-resume membership check again.
        native.drain(timeout=0)
        require(native.wait(0) == code)
        self.cleanup = dict(pid=native.pid, exit_code=code, empty_job=True)

    def complete(self, evidence):
        if self.repairing():
            from safe_qwen_active_dispatch import complete
            return complete(self, evidence)
        from offline_scripted_executor import _snapshot
        with self.store.mutex:
            self.live()
            candidate = durable.snapshot(self.repo)
            after = _snapshot(self.repo.root)
            elapsed = time.monotonic() - self.identity["started_clock"]
            success = (evidence["success"] is True and self.cleanup is not None
                       and time.monotonic() < min(self.identity["unit_deadline"], self.identity["task_deadline"]))
            result = dict(outcome="executor_succeeded_unverified" if success else "executor_failed_unverified",
                          evidence=copy.deepcopy(evidence), candidate=candidate, files_after=after,
                          changed_paths=sorted(p for p in self.data["files_before"] if self.data["files_before"][p] != after.get(p)),
                          elapsed_seconds=elapsed, cleanup_proof=copy.deepcopy(self.cleanup))
            data = copy.deepcopy(self.data)
            data.update(result=result, phase="COMPLETE", updated_at=durable.now())
            data["events"].append(dict(number=len(data["events"]) + 1, kind="COMPLETE", clock=time.monotonic(),
                                       data={"result_sha256": durable.digest(result)}))
            self.write(data)


def run(scheduler, unit_id, *, gateway, timeout_seconds=30, cancel_event=None, verify=False, diagnose=False, grant=False, allocate=False, dispatch=False, verify_second=False):
    """Explicit continuous first-attempt operation, selected before allocation."""
    import supervised_safe_qwen as adapter
    from manager import Stop
    owner = None
    try:
        with scheduler.store.mutex:
            owner = _Controller(scheduler, unit_id, gateway, timeout_seconds, cancel_event, verify=verify, diagnose=diagnose, grant=grant, allocate=allocate, dispatch=dispatch, verify_second=verify_second)
            scheduler.store._safe_qwen_active_controller = owner
            try:
                scheduler.store.exclusive_artifact(RESERVATION, owner.data)
                owner.write(owner.data)
            except BaseException:
                owner.persistence_failed()
                raise
        directory = owner.store.directory / "safe-qwen-active-audit"
        directory.mkdir(exist_ok=False)
        binding = adapter._OwnedGateway(owner.executor, owner.identity["worker"], owner.repo, gateway, owner.cancel_event, _recovery=owner)
        owner.binding = binding
        adapter._run_worker(owner.executor.policy, owner.identity["worker"], gateway=binding,
                            audit_directory=directory / owner.identity["execution_id"], timeout_seconds=timeout_seconds, cancel_event=owner.cancel_event)
        require(owner.data["phase"] == "COMPLETE")
        if owner.verify_selected:
            from safe_qwen_active_verification import run as verify_first
            verify_first(owner)
        result = {"status": "HUMAN_ACTION_REQUIRED", "active_recovery": copy.deepcopy(owner.data)}
        if owner.diagnostic_selected:
            from safe_qwen_active_eligibility import run as diagnose_first
            result["diagnostic"] = diagnose_first(owner)
        if owner.grant_selected:
            # Only short local validation/WAL operations occur here. Keep the
            # grant and atomic allocation adjacent, excluding heartbeat writes.
            with owner.store.mutex:
                result["authorization"] = owner.store._issue_ai_repair_grant(owner)
                if owner.allocation_selected and result["authorization"] is not None:
                    result["authorization"] = owner.store._consume_ai_repair_grant(owner)
                    if owner.dispatch_selected:
                        from safe_qwen_active_dispatch import prepare
                        prepare(owner)  # Local WAL intent before releasing the atomic boundary.
            if owner.dispatch_selected and result["authorization"] is not None:
                from safe_qwen_active_dispatch import run as dispatch_second
                result["dispatch"] = dispatch_second(owner)
                if owner.second_verify_selected and result["dispatch"]["result"]["outcome"] == "executor_succeeded_unverified":
                    from safe_qwen_active_verification import run as verify_repair
                    verify_repair(owner, second=True)
                    result["verification"] = copy.deepcopy(owner.data["second_verification"])
                result["active_recovery"] = copy.deepcopy(owner.data)
        return result
    finally:
        if owner is not None:
            owner.closed = True
            scheduler.store._safe_qwen_active_controller = None
            scheduler.reconciliation_stop = Stop("HUMAN_ACTION_REQUIRED", "SAFEQWEN_ACTIVE_REVIEW_REQUIRED",
                "Attempt 2 native verification stopped for independent review" if verify_second else
                "Attempt 2 executor stopped UNVERIFIED; native verification and final gates remain blocked" if dispatch else
                "Attempt 1 bridge stopped; verification, grants and Attempt 2 remain blocked")
