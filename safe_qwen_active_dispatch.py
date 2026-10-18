"""ACTIVE v6: one continuous, WAL-bound second worker, never verification.

No historical allocation, diagnostic packet, worker receipt or JSON selector
supplies authority. Only the registered controller selected before Attempt 1
may enter this lifecycle. Native launch, tools and transport remain in the
reviewed adapter; this module binds their existing controller observations.
"""
import copy
import hashlib
from pathlib import Path
import threading
import time

import durable
import safe_qwen_active_recovery as active
import safe_qwen_active_grant as grants
import work_units
from offline_scripted_executor import _snapshot

MODE = "ACTIVE_REPAIR_DISPATCH"
SOURCES = ("safe_qwen_active_dispatch.py", "repair_evidence.py", "supervised_safe_qwen.py", "supervised_safe_qwen_worker.py")
_LOADED = {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in SOURCES}
require, fields = active.require, active.fields


def contract():
    require(all(hashlib.sha256(Path(__file__).with_name(k).read_bytes()).hexdigest() == v for k, v in _LOADED.items()))
    return dict(schema_version=1, worker_context_version=1, repair_evidence_version=2, sources=copy.deepcopy(_LOADED))


def source_data(data):
    value = dict(data)
    value.pop("dispatch_contract"); value.pop("dispatch")
    value.update(schema_version=5, mode="ACTIVE_REPAIR_ALLOCATE")
    return value


def source_section(data):
    return active.project(source_data(data))


def worker_identity(data):
    return {**data["identity"], "attempt_id": data["allocation_contract"]["attempt_id"],
            "execution_id": data["allocation_contract"]["execution_id"]}


def project(section, grant, dispatch):
    """Only this closed execution lifecycle can change the allocated Attempt 2."""
    value = copy.deepcopy(section)
    unit = value["units"][grant["unit_id"]]
    att = unit["attempts"][1]
    att["stage"] = {"INTENT": "ai_dispatch_intent", "RELEASED": "ai_dispatch_released",
                    "COMPLETE": "ai_executor_complete_unverified"}[dispatch["phase"]]
    if dispatch["phase"] == "COMPLETE":
        r = dispatch["result"]
        elapsed = r["elapsed_seconds"] - unit["attempts"][0]["timeout_info"]["elapsed_seconds"]
        require(elapsed >= 0)
        result = dict(ai_dispatch_result_sha256=durable.digest(r), execution_id=dispatch["execution_id"])
        if r["outcome"] == "executor_failed_unverified":
            work_units.complete_attempt(unit, att["attempt_id"], outcome="error", execution_result=result,
                changed_paths=r["changed_paths"], failure_classification="executor_failed", timeout_info={"elapsed_seconds": elapsed})
            att["completed_at"] = dispatch["updated_at"]
            work_units.transition_unit(unit, "UNIT_FAILED")
        else:
            att.update(execution_result=result, changed_paths=r["changed_paths"], timeout_info={"elapsed_seconds": elapsed})
    unit["updated_at"] = dispatch["updated_at"]
    work_units.seal_unit_state(unit); work_units._reseal_section(value)
    return value


def allocated_section(state):
    d = state[active.KEY]
    grant = state["repair_authorizations"]["repair-" + d["identity"]["id"]]
    return durable._allocated_repair_section(source_section(d), grant, grant["consumption"])


def gateway_parent(state):
    """Original native Gateway owner, derived from immutable Supervisor facts."""
    d = state[active.KEY]
    request = next(row["data"] for row in d["events"] if row["kind"] == "MODEL_INTENT")
    child = active.model_proof(state, request["child_id"])
    return dict(pid=child["parent_pid"], identity=copy.deepcopy(child["parent_identity"]))


def check_gateway_parent(owner):
    proof = gateway_parent(owner.store.state)
    path = Path(owner.executor.config["safe_qwen_gateway"]["pid_file"])
    with path.open("rb") as stream:
        raw = stream.read(33)
    require(len(raw) <= 32 and raw.strip().isdigit() and int(raw) == proof["pid"]
            and durable.winprocess.identity(proof["pid"]) == proof["identity"])


def validate(state):
    try:
        d = state[active.KEY]
        require(type(d["schema_version"]) is int and d["schema_version"] == 6 and d["mode"] == MODE)
        c = d["dispatch_contract"]
        fields(c, "schema_version worker_context_version repair_evidence_version sources")
        require(all(type(c[k]) is int and c[k] == v for k, v in
                    (("schema_version", 1), ("worker_context_version", 1), ("repair_evidence_version", 2))))
        fields(c["sources"], " ".join(SOURCES))
        for sha in c["sources"].values(): durable._repair_sha(sha)
        view = dict(state); view[active.KEY] = source_data(d)
        if d["dispatch"] is not None:
            view["work_units"] = allocated_section(state)
        grants.validate_allocation(view)  # Immutable historical Attempt 1 + original allocation.
        if d["dispatch"] is not None:
            validate_dispatch(state)
    except (KeyError, ValueError, TypeError, IndexError, AttributeError, OverflowError) as exc:
        raise durable.DurableError("Invalid ACTIVE v6 dispatch history") from exc


def validate_dispatch(state):
    from repair_evidence import encode_repair_evidence
    d, dispatch = state[active.KEY], state[active.KEY]["dispatch"]
    i = worker_identity(d)
    aid = "repair-" + i["id"]
    g = state["repair_authorizations"][aid]
    require(g["state"] == "CONSUMED" and g["schema_version"] == 2)
    fields(dispatch, "schema_version execution_id attempt_id authorization_id consumption_wal repair_evidence repair_evidence_sha256 "
                     "snapshot_before files_before started_clock phase updated_at events process result")
    require(type(dispatch["schema_version"]) is int and dispatch["schema_version"] == 1
            and dispatch["execution_id"] == i["execution_id"] and dispatch["attempt_id"] == i["attempt_id"]
            and dispatch["authorization_id"] == aid and dispatch["snapshot_before"] == d["verification"]["after"]
            and dispatch["files_before"] == d["verification"]["files_after"])
    durable._repair_ref(dispatch["consumption_wal"])
    packet = dispatch["repair_evidence"]
    raw = encode_repair_evidence(packet)
    require(packet["schema_version"] == 2 and hashlib.sha256(raw).hexdigest() == dispatch["repair_evidence_sha256"]
            and packet["source_wal"] == g["source_wal"] and packet["controller_session"] == i["session"]
            and packet["run_id"] == i["run_id"] and packet["unit_id"] == i["unit_id"]
            and packet["source_attempt_id"] == d["identity"]["attempt_id"]
            and packet["candidate_fingerprint"] == g["candidate_fingerprint"]
            and packet["spec_sha256"] == i["parent_sha256"] and packet["registry_sha256"] == i["registry_sha256"])
    expected = diagnostic_packet(state)
    require(packet == expected)  # Actual immutable native observations, never caller JSON.
    require(type(dispatch["started_clock"]) in (int, float)
            and g["consumption"]["checked_clock"] <= dispatch["started_clock"] < i["deadline"]
            and type(dispatch["events"]) is list and 1 <= len(dispatch["events"]) <= 200)
    active.validate_worker_facts(state, dispatch, i)
    parent = gateway_parent(state)
    for row in dispatch["events"]:
        if row["kind"] == "MODEL_INTENT":
            child = state["supervision"]["children"][row["data"]["child_id"]]
            require(dict(pid=child["parent_pid"], identity=child["parent_identity"]) == parent)
    require(dispatch["events"][0]["clock"] == dispatch["started_clock"])
    if dispatch["result"] is not None:
        r = dispatch["result"]
        require(r["elapsed_seconds"] == dispatch["events"][-1]["clock"] - i["started_clock"]
                and r["evidence"]["repair_evidence_sha256"] == dispatch["repair_evidence_sha256"])
        # Ambiguous cleanup cannot be sealed as a completed execution.
        require(r["evidence"]["transport"]["model_cleanup"] in ("observed_idle", "not_started")
                and (dispatch["process"] is None or (r["evidence"]["cleanup"] == "observed_empty"
                     and r["cleanup_proof"] == dict(pid=dispatch["process"]["pid"],
                         exit_code=r["evidence"]["observed_exit_code"], empty_job=True))))
    require(state["work_units"] == project(allocated_section(state), g, dispatch))


def diagnostic_packet(state):
    """Pure validation/formatting of already authenticated immutable WAL facts."""
    from repair_evidence import _pack, _output
    from critic_executor import sanitize_evidence
    d = state[active.KEY]; i = d["identity"]
    g = state["repair_authorizations"]["repair-" + i["id"]]
    rows = []
    for vid in d["source_section"]["units"][i["unit_id"]]["spec"]["verifier_ids"]:
        observed = d["verification"]["observations"][vid]
        native, binding = observed["native"], observed["binding"]
        ref = g["native_evidence"][vid]
        require(binding == {k: ref[k] for k in ("child_id", "invocation_id", "wal")}
                and native["termination"] == "natural" and native["cleanup"] == "observed_empty")
        if native["observed_exit_code"]:
            display = sanitize_evidence(vid)
            rows.append(dict(verifier_id=display, verifier_id_sha256=durable.digest(vid), child_id=binding["child_id"],
                invocation_id=binding["invocation_id"], exit_code=native["observed_exit_code"], termination="natural",
                diagnostic=sanitize_evidence(f"Verifier {display} failed after native descendant cleanup."),
                native_wal=copy.deepcopy(binding["wal"]), assertion_output=_output(observed.get("output"))))
    identity = dict(run_id=i["run_id"], unit_id=i["unit_id"], source_attempt_id=i["attempt_id"],
        candidate_fingerprint=g["candidate_fingerprint"], spec_sha256=i["parent_sha256"], registry_sha256=i["registry_sha256"],
        controller_session=copy.deepcopy(i["session"]), source_wal=copy.deepcopy(g["source_wal"]))
    return _pack(identity, rows, version=2)


def check_repository(owner):
    """Recheck Git/index identity and exactly the edits already observed in WAL."""
    dispatch = owner.data["dispatch"]
    expected = copy.deepcopy(dispatch["files_before"])
    intents = {}
    for row in dispatch["events"]:
        value = row["data"]
        if row["kind"] == "EDIT_INTENT": intents[value["action"]] = value
        elif row["kind"] == "EDIT_APPLIED":
            edit = intents[value["action"]]
            expected[edit["path"]] = [edit["after_sha256"], edit["after_size"], expected[edit["path"]][2]]
    before, current = dispatch["snapshot_before"], owner.scheduler._shadow_snapshot()
    require(all(before[k] == current[k] for k in ("repository", "head", "branch", "git_directory", "git_identity", "index_sha256"))
            and _snapshot(owner.repo.root) == expected)


def require_owner(owner):
    require(type(owner) is active._Controller and owner.dispatch_selected and owner.data["schema_version"] in (6, 7)
            and owner.data["mode"] == {6: MODE, 7: "ACTIVE_REPAIR_VERIFY"}[owner.data["schema_version"]] and owner.store._safe_qwen_active_controller is owner
            and not owner.closed and not owner.store._repair_write_failed and threading.get_ident() == owner.thread_id
            and owner.data["dispatch_contract"] == owner.dispatch_contract == contract())


def guard(owner, *, before=False):
    require_owner(owner); owner.live()
    require(owner.allocation_started and owner.allocation_record is not None and owner.network_thread is None
            and not owner.cancel_event.is_set() and owner.scheduler.clock is time.monotonic)
    d, i, state = owner.data, owner.identity, owner.store.state
    require(i["session"] == durable._repair_session(state) and d["verification"]["phase"] == "COMPLETE"
            and d["verification"]["outcome"] == "failed")
    require(owner.scheduler.parent_scope == state["manager"]["scope"]
            and owner.scheduler.budgets == state["manager"]["budgets"]
            and owner.scheduler.verifier_registry == state["options"]["verifier_registry"])
    if before:
        require(not owner.dispatch_started and d["dispatch"] is None
                and owner.store.records[-1] == owner.allocation_record
                and owner.gateway.active_role is None and owner.gateway.active_model_child_id is None)
        owner.scheduler.supervisor.require_idle()
        check_gateway_parent(owner)
        require(owner.scheduler._shadow_snapshot() == d["verification"]["after"]
                and _snapshot(owner.repo.root) == d["verification"]["files_after"])
    clock = time.monotonic()
    require(min(i[k] for k in ("deadline", "unit_deadline", "task_deadline")) - clock >= 1
            and owner.scheduler.remaining_task_budget(clock) >= 1)


def write(owner, dispatch):
    require_owner(owner)
    state = copy.deepcopy(owner.store.state)
    data = copy.deepcopy(owner.data); data["dispatch"] = dispatch
    state[active.KEY] = data
    g = state["repair_authorizations"][dispatch["authorization_id"]]
    state.update(work_units=project(allocated_section(state), g, dispatch),
                 revision=state["revision"] + 1, updated_at=dispatch["updated_at"])
    state["manager"]["runtime_seconds"] = max(state["manager"]["runtime_seconds"], owner.scheduler.runtime())
    marker = dict(event="AI_" + dispatch["events"][-1]["kind"], execution_id=dispatch["execution_id"])
    owner.pending = ("state_committed", dict(state=state, transition=None, safe_qwen_active_event=marker))
    try:
        durable.validate_state(state)
        from supervision import validate_invocations
        validate_invocations(state, owner.store.state, None)
        owner.store.append(owner.pending[0], **owner.pending[1])
        owner.store.state = copy.deepcopy(state); owner.data = copy.deepcopy(data)
        durable.atomic_write(owner.store.directory / "state.json", durable.encoded(state) + b"\n")
    except BaseException:
        owner.persistence_failed()
        raise
    finally:
        owner.pending = None


def event(owner, kind, value):
    owner.live(budget=kind != "COMPLETE")
    dispatch = copy.deepcopy(owner.data["dispatch"])
    dispatch["updated_at"] = durable.now()
    dispatch["events"].append(dict(number=len(dispatch["events"]) + 1, kind=kind, clock=time.monotonic(), data=value))
    if kind == "RELEASE": dispatch.update(process=value, phase="RELEASED")
    write(owner, dispatch)


def complete(owner, evidence):
    with owner.store.mutex:
        owner.live()
        dispatch = copy.deepcopy(owner.data["dispatch"])
        clock = time.monotonic()
        after = _snapshot(owner.repo.root)
        result = dict(outcome="executor_succeeded_unverified" if evidence["success"] else "executor_failed_unverified",
            evidence=copy.deepcopy(evidence), candidate=durable.snapshot(owner.repo), files_after=after,
            changed_paths=sorted(p for p in dispatch["files_before"] if dispatch["files_before"][p] != after.get(p)),
            elapsed_seconds=clock - owner.identity["started_clock"], cleanup_proof=copy.deepcopy(owner.cleanup))
        dispatch.update(phase="COMPLETE", result=result, updated_at=durable.now())
        dispatch["events"].append(dict(number=len(dispatch["events"]) + 1, kind="COMPLETE", clock=clock,
                                       data={"result_sha256": durable.digest(result)}))
        write(owner, dispatch)


def validate_step(previous, state, marker, records):
    d, old = state[active.KEY], previous[active.KEY]
    require(d["schema_version"] == old["schema_version"] and d["schema_version"] in (6, 7)
            and {k: v for k, v in d.items() if k != "dispatch"} == {k: v for k, v in old.items() if k != "dispatch"})
    dispatch, prior = d["dispatch"], old["dispatch"]
    require(dispatch is not None and marker == dict(event="AI_" + dispatch["events"][-1]["kind"], execution_id=dispatch["execution_id"]))
    if prior is None:
        require(dispatch["phase"] == "INTENT" and len(dispatch["events"]) == 1)
        ref = dispatch["consumption_wal"]
        r = records[ref["sequence"] - 1]
        require(r["sequence"] < next(row["sequence"] for row in records if row.get("state") is state)
                and r["sha256"] == ref["sha256"] and r["state"]["repair_authorizations"] == state["repair_authorizations"]
                and r.get("repair_authorization") == dict(event=durable._REPAIR_CONSUME_EVENT,
                    authorization_id=dispatch["authorization_id"], attempt_id=dispatch["attempt_id"]))
        require(previous["work_units"] == r["state"]["work_units"])
    else:
        require(prior["phase"] != "COMPLETE" and len(dispatch["events"]) == len(prior["events"]) + 1
                and dispatch["events"][:-1] == prior["events"])
        mutable = {"events", "phase", "process", "result", "updated_at"}
        require({k: v for k, v in prior.items() if k not in mutable} == {k: v for k, v in dispatch.items() if k not in mutable})


def prepare(owner):
    from repair_evidence import collect_ai_repair_evidence, encode_repair_evidence
    require_owner(owner)
    with owner.store.mutex:
        guard(owner, before=True)
        packet = collect_ai_repair_evidence(owner)
        owner.dispatch_started = True
        i = worker_identity(owner.data); stamp = durable.now(); clock = time.monotonic()
        dispatch = dict(schema_version=1, execution_id=i["execution_id"], attempt_id=i["attempt_id"],
            authorization_id="repair-" + i["id"], consumption_wal={k: owner.allocation_record[k] for k in ("sequence", "sha256")},
            repair_evidence=packet, repair_evidence_sha256=hashlib.sha256(encode_repair_evidence(packet)).hexdigest(),
            snapshot_before=copy.deepcopy(owner.data["verification"]["after"]), files_before=copy.deepcopy(owner.data["verification"]["files_after"]),
            started_clock=clock, phase="INTENT", updated_at=stamp, process=None, result=None,
            events=[dict(number=1, kind="INTENT", clock=clock, data={"execution_id": i["execution_id"]})])
        write(owner, dispatch)  # Durable before binding, model startup or child creation.
        owner.cleanup = owner.native_binding = None


def run(owner):
    import supervised_safe_qwen as adapter
    require_owner(owner)
    require(not owner.dispatch_execution_started and owner.dispatch_started
            and owner.data["dispatch"] is not None and owner.data["dispatch"]["phase"] == "INTENT")
    owner.dispatch_execution_started = True  # Refusals cannot restart this operation.
    with owner.store.mutex:
        guard(owner)
        check_repository(owner)
    i = worker_identity(owner.data)
    binding = adapter._OwnedGateway(owner.executor, owner.identity["worker"], owner.repo, owner.gateway, owner.cancel_event, _recovery=owner)
    owner.binding = binding
    try:
        adapter._run_worker(owner.executor.policy, owner.identity["worker"], gateway=binding,
            audit_directory=owner.store.directory / "safe-qwen-active-audit" / i["execution_id"],
            timeout_seconds=min(600, owner.identity["deadline"] - time.monotonic()), cancel_event=owner.cancel_event)
    except adapter.AdapterError:
        # A durably observed deterministic executor failure is terminal. An
        # incomplete/ambiguous operation remains an error, never a retry path.
        require(not owner.store._repair_write_failed and owner.data["dispatch"]["phase"] == "COMPLETE")
    return copy.deepcopy(owner.data["dispatch"])
