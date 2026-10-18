"""ACTIVE v3 joins worker completion to the existing native invocation pipeline.

No verifier runner, classifier, grant, checkpoint or replay controller lives here.
The v2 worker evidence is immutable; this closed projection adds native facts.
"""
import copy
import hashlib
import math
from pathlib import Path
import threading
import time

import durable
import safe_qwen_active_recovery as active
import work_units
from offline_scripted_executor import _snapshot
from supervision import checksum
from retry_eligibility import validate_normalize_snapshot

require, fields = active.require, active.fields
EVENTS = ("invocation_intent", "invocation_bound", "invocation_release", "process_exit", "invocation_complete")
_LOADED_SOURCE = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
SECOND_MODE = "ACTIVE_REPAIR_VERIFY"


def contract():
    require(hashlib.sha256(Path(__file__).read_bytes()).hexdigest() == _LOADED_SOURCE)
    return dict(schema_version=1, invocation_schema_version=1,
                sources={Path(__file__).name: _LOADED_SOURCE})


def second_contract():
    return {**contract(), "schema_version": 2, "dispatch_schema_version": 1}


def dispatch_data(data):
    """Historical v6 validation view only; never a live upgrade or adoption."""
    value = dict(data)
    value.pop("second_verification_contract"); value.pop("second_verification")
    value.update(schema_version=6, mode="ACTIVE_REPAIR_DISPATCH")
    return value


def second_identity(data):
    from safe_qwen_active_dispatch import worker_identity
    i = worker_identity(data)
    i["started_clock"] += data["verification"]["elapsed_seconds"]
    return i


def second_binding(data, ref):
    d, i = data["dispatch"], data["identity"]
    return dict(attempt_id=d["attempt_id"], execution_id=d["execution_id"], authorization_id=d["authorization_id"],
                consumption_wal=copy.deepcopy(d["consumption_wal"]), dispatch_wal=copy.deepcopy(ref),
                dispatch_sha256=durable.digest(d), identity_sha256=durable.digest(i), controller_session=copy.deepcopy(i["session"]))


def second_project(state):
    from safe_qwen_active_dispatch import project as dispatch_project, allocated_section
    d = state[active.KEY]; dispatch = d["dispatch"]
    grant = state["repair_authorizations"][dispatch["authorization_id"]]
    section = dispatch_project(allocated_section(state), grant, dispatch)
    v = d["second_verification"]
    if v is None:
        return section
    i = second_identity(d)
    unit = section["units"][i["unit_id"]]; att = unit["attempts"][1]
    work_units.transition_unit(unit, "VERIFYING")
    att.update(started_at_clock=i["started_clock"], stage="verifying", candidate_snapshot=v["before"],
               verification_snapshot_before=v["before"], stage_timing=dict(stage="VERIFYING", unit_id=i["unit_id"],
               attempt_id=i["attempt_id"], stage_start_clock=v["started_clock"], stage_start_runtime=v["started_runtime"],
               attempt_elapsed_at_start=v["started_clock"] - i["started_clock"]))
    if v["phase"] == "COMPLETE":
        att["verification_snapshot_after"] = v["after"]
        work_units.complete_attempt(unit, i["attempt_id"], outcome=v["outcome"], execution_result=att["execution_result"],
            changed_paths=att["changed_paths"], verifier_result=results(v), failure_classification=v["failure_classification"],
            timeout_info={"elapsed_seconds": v["elapsed_seconds"]})
        att["completed_at"] = v["completed_at"]
        if v["outcome"] != "passed":
            work_units.transition_unit(unit, "UNIT_FAILED")
    unit["updated_at"] = d["updated_at"]
    work_units.seal_unit_state(unit); work_units._reseal_section(section)
    return section


def validate_second(state):
    from safe_qwen_active_dispatch import validate as validate_dispatch
    try:
        d = state[active.KEY]
        require(type(d["schema_version"]) is int and d["schema_version"] == 7 and d["mode"] == SECOND_MODE)
        c = d["second_verification_contract"]
        fields(c, "schema_version invocation_schema_version dispatch_schema_version sources")
        require(type(c["schema_version"]) is int and c["schema_version"] == 2
                and type(c["invocation_schema_version"]) is int and c["invocation_schema_version"] == 1
                and type(c["dispatch_schema_version"]) is int and c["dispatch_schema_version"] == 1)
        fields(c["sources"], Path(__file__).name)
        durable._repair_sha(c["sources"][Path(__file__).name])
        require(c["sources"] == d["verification_contract"]["sources"])
        view = dict(state); view[active.KEY] = dispatch_data(d)
        v = d["second_verification"]
        if v is not None:
            view["work_units"] = second_project({**state, active.KEY: {**d, "second_verification": None}})
            view["supervision"] = {**state["supervision"], "children": {
                cid: ch for cid, ch in state["supervision"]["children"].items()
                if ch.get("invocation", {}).get("attempt_id") != d["allocation_contract"]["attempt_id"]}}
        validate_dispatch(view)  # Closed v6, immutable Attempt 1 and consumed allocation.
        if v is None:
            return
        require(len(durable.encoded(d)) <= 8 * 1024 * 1024 and v["schema_version"] == 2 and type(v["schema_version"]) is int)
        b = v["binding"]
        fields(b, "attempt_id execution_id authorization_id consumption_wal dispatch_wal dispatch_sha256 identity_sha256 controller_session")
        durable._repair_ref(b["dispatch_wal"])
        require(b == second_binding(d, b["dispatch_wal"]))
        require(state["work_units"] == second_project(state))
        native = {k: value for k, value in v.items() if k != "binding"}; native["schema_version"] = 1
        validate_native(state, {**d, "identity": second_identity(d), "result": d["dispatch"]["result"]}, native,
                        prior_attempt_id=d["identity"]["attempt_id"])
        require(v["started_clock"] >= d["dispatch"]["events"][-1]["clock"])
    except (KeyError, ValueError, TypeError, IndexError, AttributeError, OverflowError) as exc:
        raise durable.DurableError("Invalid ACTIVE v7 native Attempt 2 verification") from exc


def require_second_owner(owner):
    from safe_qwen_active_dispatch import require_owner
    require_owner(owner)
    require(owner.second_verify_selected and owner.data["schema_version"] == 7
            and owner.data["second_verification_contract"] == owner.second_verification_contract == second_contract())


def completion_ref(owner):
    d = owner.data["dispatch"]
    matches = [r for r in owner.store.records if r.get(active.MARKER) == dict(event="AI_COMPLETE", execution_id=d["execution_id"])]
    require(len(matches) == 1)
    row = matches[0]
    require(row["state"][active.KEY]["dispatch"] == d and row["state"]["run_id"] == owner.identity["run_id"])
    return {k: row[k] for k in ("sequence", "sha256")}


def guard_second(owner, child_id=None):
    require_second_owner(owner); owner.live()
    d, i = owner.data, second_identity(owner.data)
    dispatch = d["dispatch"]
    require(owner.second_verification_started and dispatch["phase"] == "COMPLETE"
            and dispatch["result"]["outcome"] == "executor_succeeded_unverified"
            and owner.cleanup is not None and owner.cleanup == dispatch["result"]["cleanup_proof"]
            and dispatch["result"]["evidence"]["cleanup"] == "observed_empty"
            and dispatch["result"]["evidence"]["transport"]["model_cleanup"] == "observed_idle"
            and owner.gateway.active_role is None and owner.gateway.active_model_child_id is None
            and owner.network_thread is None and not owner.cancel_event.is_set()
            and i["session"] == durable._repair_session(owner.store.state)
            and time.monotonic() < min(i[k] for k in ("deadline", "unit_deadline", "task_deadline")))
    ref = completion_ref(owner)
    v = d["second_verification"]
    if v is not None:
        require(v["phase"] == "OBSERVING" and v["binding"] == second_binding(d, ref))
    require(owner.scheduler.budgets == owner.store.state["manager"]["budgets"]
            and owner.scheduler.verifier_registry == owner.store.state["options"]["verifier_registry"]
            and owner.scheduler.parent_scope == owner.store.state["manager"]["scope"]
            and owner.scheduler._shadow_snapshot() == dispatch["result"]["candidate"]
            and _snapshot(owner.repo.root) == dispatch["result"]["files_after"])
    unit = owner.store.state["work_units"]["units"][i["unit_id"]]
    require(len(unit["attempts"]) == 2 and owner.scheduler.remaining_task_budget() > 0
            and owner.scheduler.remaining_unit_budget(unit, time.monotonic() - i["started_clock"], i["attempt_id"]) > 0)
    sup = owner.scheduler.supervisor
    if child_id is None:
        sup.require_idle()
    else:
        require(v is not None)
        ch = sup.child(child_id)
        require(ch["invocation"]["attempt_id"] == i["attempt_id"] and ch["invocation"]["unit_id"] == i["unit_id"]
                and ch["state"] in {"STARTING", "RUNNING"})
        require(not any(c["child_id"] != child_id and c["state"] in {"STARTING", "RUNNING", "STOPPING", "UNKNOWN"}
                        for c in owner.store.state["supervision"]["children"].values()))


def worker_data(data):
    value = copy.deepcopy(data)
    value.pop("verification"); value.pop("verification_contract")
    value.update(schema_version=2, mode="ACTIVE_ATTEMPT1")
    return value


def project(data):
    section = active.project_worker(worker_data(data))
    v = data["verification"]
    if v is None:
        return section
    i = data["identity"]
    unit = section["units"][i["unit_id"]]
    att = unit["attempts"][0]
    work_units.transition_unit(unit, "VERIFYING")
    att.update(started_at_clock=i["started_clock"], stage="verifying", candidate_snapshot=v["before"],
               verification_snapshot_before=v["before"], stage_timing=dict(stage="VERIFYING", unit_id=i["unit_id"],
               attempt_id=i["attempt_id"], stage_start_clock=v["started_clock"], stage_start_runtime=v["started_runtime"],
               attempt_elapsed_at_start=v["started_clock"] - i["started_clock"]))
    if v["phase"] == "COMPLETE":
        att["verification_snapshot_after"] = v["after"]
        work_units.complete_attempt(unit, i["attempt_id"], outcome=v["outcome"], execution_result=att["execution_result"],
                                    changed_paths=att["changed_paths"], verifier_result=results(v),
                                    failure_classification=v["failure_classification"], timeout_info={"elapsed_seconds": v["elapsed_seconds"]})
        att["completed_at"] = v["completed_at"]
    unit["updated_at"] = data["updated_at"]
    work_units.seal_unit_state(unit); work_units._reseal_section(section)
    return section


def results(v):
    return {vid: dict(argv=row["native"]["intended_argv"], exit_code=row["native"]["observed_exit_code"],
                      passed=row["native"]["termination"] == "natural" and row["native"]["observed_exit_code"] == 0,
                      controller_evidence=copy.deepcopy(row["native"])) for vid, row in v["observations"].items()}


def outcome(data, v):
    order = data["source_section"]["units"][data["identity"]["unit_id"]]["spec"]["verifier_ids"]
    timed_out = (v["checked_clock"] >= min(data["identity"][k] for k in ("deadline", "unit_deadline", "task_deadline"))
                 or any(r["native"]["termination"] == "timeout" for r in v["observations"].values()))
    failed = set(v["observations"]) != set(order) or any(not r["passed"] for r in results(v).values())
    return (("timeout", "verifier_timeout") if timed_out else ("failed", "verification_failed") if failed else ("passed", None))


def validate(state):
    try:
        d, key = state[active.KEY], active.KEY
        require(len(durable.encoded(d)) <= 4 * 1024 * 1024)
        base = worker_data(d)
        fields(d, " ".join([*base, "verification_contract", "verification"]))
        require(type(d["schema_version"]) is int and d["schema_version"] == 3 and d["mode"] == "ACTIVE_ATTEMPT1_VERIFY")
        c = d["verification_contract"]
        fields(c, "schema_version invocation_schema_version sources")
        require(type(c["schema_version"]) is int and c["schema_version"] == 1
                and type(c["invocation_schema_version"]) is int and c["invocation_schema_version"] == 1)
        fields(c["sources"], Path(__file__).name)
        durable._repair_sha(c["sources"][Path(__file__).name])
        require(1 <= len(d["identity"]["worker"]["verifier_ids"]) <= 32)
        view = copy.deepcopy(state)
        view[key], view["work_units"] = base, active.project_worker(base)
        active.validate(view)  # Original closed v2 worker history, unchanged.
        require(durable.encoded(state["work_units"]) == durable.encoded(project(d)))
        v = d["verification"]
        if v is None:
            return
        validate_native(state, d, v)
    except (KeyError, TypeError, ValueError, IndexError, OverflowError, AttributeError) as exc:
        raise durable.DurableError("Invalid ACTIVE native verification history") from exc


def validate_native(state, d, v, *, prior_attempt_id=None):
    fields(v, "schema_version phase execution_id dispatch_result_sha256 before files_before started_clock started_runtime "
              "observations after files_after checked_clock completed_at outcome failure_classification elapsed_seconds")
    i = d["identity"]
    require(type(v["schema_version"]) is int and v["schema_version"] == 1 and v["phase"] in {"OBSERVING", "COMPLETE"}
            and d["phase"] == "COMPLETE" and d["result"]["outcome"] == "executor_succeeded_unverified"
            and v["execution_id"] == i["execution_id"] and v["dispatch_result_sha256"] == durable.digest(d["result"])
            and v["before"] == d["result"]["candidate"] and v["files_before"] == d["result"]["files_after"])
    _, error = validate_normalize_snapshot(v["before"], "ACTIVE verification candidate")
    require(error is None)
    for k in ("started_clock", "started_runtime", "checked_clock"):
        require(type(v[k]) in (int, float) and math.isfinite(v[k]) and v[k] >= 0)
    require(i["started_clock"] <= v["started_clock"] <= v["checked_clock"]
            and v["started_clock"] < min(i[k] for k in ("deadline", "unit_deadline", "task_deadline"))
            and v["started_runtime"] <= state["manager"]["runtime_seconds"])
    require(type(v["observations"]) is dict)
    order = d["source_section"]["units"][i["unit_id"]]["spec"]["verifier_ids"]
    require(set(v["observations"]) == set(order[:len(v["observations"])]))
    for vid, row in v["observations"].items():
        fields(row, "binding native after files_after output")
        b, n = row["binding"], row["native"]
        fields(b, "child_id invocation_id wal"); fields(b["wal"], " ".join(EVENTS))
        for ref in b["wal"].values(): durable._repair_ref(ref)
        child = state["supervision"]["children"][b["child_id"]]
        inv = child["invocation"]
        require(child["run_id"] == i["run_id"] and child["state"] == "EXITED" and inv["phase"] == "COMPLETE"
                and b["invocation_id"] == child["token"] and inv["unit_id"] == i["unit_id"] and inv["attempt_id"] == i["attempt_id"]
                and inv["verifier_id"] == vid and inv["candidate_fingerprint"] == v["before"]["fingerprint"]
                and inv["registry_sha256"] == i["registry_sha256"] and inv["spec_sha256"] == i["parent_sha256"]
                and inv["deadline"] <= min(i[k] for k in ("deadline", "unit_deadline", "task_deadline")))
        fields(n, "schema_version source child_id broker verifier intended_argv intended_command_sha256 resolved_argv "
                  "resolved_command_sha256 job termination termination_basis observed_exit_code cleanup execution_started")
        require(type(n["schema_version"]) is int and n["schema_version"] == 1 and n["source"] == "controller_native_handles"
                and n["child_id"] == child["child_id"] and n["broker"] is None and n["cleanup"] == "observed_empty"
                and n["execution_started"] is True and n["verifier"] == {k: child[k] for k in ("pid", "identity")}
                and n["job"] == child["job"] and type(n["observed_exit_code"]) is int and n["observed_exit_code"] == child["exit_code"]
                and n["intended_argv"] == inv["intended_argv"] and n["resolved_argv"] == inv["resolved_argv"]
                and n["intended_command_sha256"] == checksum(inv["intended_argv"])
                and n["resolved_command_sha256"] == child["command_sha256"]
                and (n["termination"], n["termination_basis"], child["termination"]) in {
                    ("natural", "signaled_without_controller_termination", "natural"),
                    ("timeout", "controller_deadline", "forced"), ("forced", "controller_cancel", "forced")}
                and row["after"] == v["before"] and row["files_after"] == v["files_before"])
        fields(row["output"], "stdout stderr truncated duration_seconds")
        require(all(type(row["output"][k]) is str and len(row["output"][k]) <= 8192 for k in ("stdout", "stderr"))
                and type(row["output"]["truncated"]) is bool and len(durable.encoded(row)) <= 65536
                and type(row["output"]["duration_seconds"]) in (int, float)
                and math.isfinite(row["output"]["duration_seconds"]) and row["output"]["duration_seconds"] >= 0)
    if v["phase"] == "COMPLETE":
        require(v["after"] == v["before"] and v["files_after"] == v["files_before"] and type(v["completed_at"]) is str
                and (v["outcome"], v["failure_classification"]) == outcome(d, v)
                and type(v["elapsed_seconds"]) in (int, float)
                and v["elapsed_seconds"] == v["checked_clock"] - i["started_clock"])
    else:
        require(all(v[k] is None for k in ("after", "files_after", "completed_at", "outcome", "failure_classification", "elapsed_seconds")))
    # Ordinary invocation entry cannot create unrelated/unsequenced children.
    relevant = [ch for ch in state["supervision"]["children"].values() if "invocation" in ch
                and ch["invocation"]["attempt_id"] != prior_attempt_id]
    require(len(relevant) <= len(order) and len(relevant) <= len(v["observations"]) + 1)
    for ch in relevant:
        inv = ch["invocation"]
        require(inv["unit_id"] == i["unit_id"] and inv["attempt_id"] == i["attempt_id"]
                and inv["verifier_id"] in order[:len(v["observations"]) + 1]
                and inv["candidate_fingerprint"] == v["before"]["fingerprint"])
    if v["phase"] == "COMPLETE": require(len(relevant) == len(v["observations"]))


def validate_step(previous, state, marker, records):
    d, old = state[active.KEY], previous[active.KEY]
    second = marker["event"].startswith("VERIFY2_")
    key, prefix = ("second_verification", "VERIFY2_") if second else ("verification", "VERIFY_")
    execution = d["allocation_contract"]["execution_id"] if second else d["identity"]["execution_id"]
    require(d["schema_version"] == old["schema_version"] and d["schema_version"] in (3, 4, 5, 6, 7) and old["phase"] == "COMPLETE"
            and (not second or d["schema_version"] == 7)
            and marker == {"event": marker["event"], "execution_id": execution})
    require(durable.encoded({k: v for k, v in d.items() if k not in {key, "updated_at"}}) ==
            durable.encoded({k: v for k, v in old.items() if k not in {key, "updated_at"}}))
    v, prior = d[key], old[key]
    if marker["event"] == prefix + "INTENT":
        require(prior is None and v["phase"] == "OBSERVING" and v["observations"] == {})
    else:
        require(prior is not None and prior["phase"] == "OBSERVING")
        fixed = {"schema_version", "execution_id", "dispatch_result_sha256", "before", "files_before", "started_clock", "started_runtime"}
        if second: fixed.add("binding")
        require(all(durable.encoded(v[k]) == durable.encoded(prior[k]) for k in fixed) and v["checked_clock"] >= prior["checked_clock"])
        if marker["event"] == prefix + "OBSERVATION":
            require(v["phase"] == "OBSERVING" and len(v["observations"]) == len(prior["observations"]) + 1
                    and all(v["observations"][k] == value for k, value in prior["observations"].items()))
        else:
            require(marker["event"] == prefix + "COMPLETE" and v["phase"] == "COMPLETE" and v["observations"] == prior["observations"])
    before = next(r["sequence"] for r in records if r.get("state") is state)
    if second:
        b = v["binding"]
        ref = b["dispatch_wal"]
        require(ref["sequence"] < before)
        record = records[ref["sequence"] - 1]
        require(record["sha256"] == ref["sha256"] and record.get(active.MARKER) == dict(event="AI_COMPLETE", execution_id=execution)
                and record["state"][active.KEY]["dispatch"] == d["dispatch"]
                and record["state"][active.KEY]["identity"] == d["identity"]
                and record["state"]["repair_authorizations"] == state["repair_authorizations"]
                and record["state"][active.KEY]["second_verification"] is None
                and v["started_runtime"] >= record["state"]["manager"]["runtime_seconds"])
        require(b["consumption_wal"]["sequence"] < ref["sequence"])
    for row in v["observations"].values():
        b = row["binding"]
        sequences = []
        for ev, ref in b["wal"].items():
            require(ref["sequence"] < before)
            record = records[ref["sequence"] - 1]
            require(record["sha256"] == ref["sha256"] and record.get("transition") == {"event": ev, "child_id": b["child_id"]})
            ch = record["state"]["supervision"]["children"][b["child_id"]]
            require(ch["token"] == b["invocation_id"] and ch["invocation"]["candidate_fingerprint"] == v["before"]["fingerprint"]
                    and ch["invocation"]["attempt_id"] == (d["allocation_contract"]["attempt_id"] if second else d["identity"]["attempt_id"])
                    and record["state"]["run_id"] == state["run_id"])
        for ev in EVENTS: sequences.append(b["wal"][ev]["sequence"])
        require(sequences == sorted(set(sequences)))


def guard(owner, child_id=None):
    if getattr(owner, "second_verification_started", False):
        return guard_second(owner, child_id)
    owner.live()
    d, i = owner.data, owner.identity
    require(owner.verify_selected and threading.get_ident() == owner.thread_id and d["schema_version"] in (3, 4, 5, 6, 7)
            and d["verification_contract"] == contract() and d["phase"] == "COMPLETE" and owner.cleanup is not None
            and d["result"]["outcome"] == "executor_succeeded_unverified" and owner.network_thread is None
            and time.monotonic() < min(i[k] for k in ("deadline", "unit_deadline", "task_deadline"))
            and not owner.cancel_event.is_set())
    require(owner.scheduler.budgets == owner.store.state["manager"]["budgets"]
            and owner.scheduler.verifier_registry == owner.store.state["options"]["verifier_registry"]
            and owner.scheduler.parent_scope == owner.store.state["manager"]["scope"]
            and owner.scheduler._shadow_snapshot() == d["result"]["candidate"] and _snapshot(owner.repo.root) == d["result"]["files_after"])
    sup = owner.scheduler.supervisor
    unit = owner.store.state["work_units"]["units"][i["unit_id"]]
    require(owner.scheduler.remaining_task_budget() > 0 and owner.scheduler.remaining_unit_budget(
            unit, time.monotonic() - i["started_clock"], i["attempt_id"]) > 0)
    if child_id is None:
        sup.require_idle()
    else:
        child = sup.child(child_id)
        require(child["invocation"]["unit_id"] == i["unit_id"] and child["invocation"]["attempt_id"] == i["attempt_id"]
                and child["state"] in {"STARTING", "RUNNING"})
        require(not any(ch["child_id"] != child_id and ch["state"] in {"STARTING", "RUNNING", "STOPPING", "UNKNOWN"}
                        for ch in owner.store.state["supervision"]["children"].values()))


def run(owner, *, second=False):
    from supervision import _VerifierInvocation, _run_observed_verifier
    from critic_executor import sanitize_evidence
    key = "second_verification" if second else "verification"
    prefix = "VERIFY2_" if second else "VERIFY_"
    with owner.store.mutex:
        if second:
            require_second_owner(owner)
            require(not owner.second_verification_started)
            owner.second_verification_started = True
        guard(owner)
        require(owner.data[key] is None)
        i = second_identity(owner.data) if second else owner.identity
        source = owner.data["dispatch"] if second else owner.data
        now = time.monotonic()
        v = dict(schema_version=1, phase="OBSERVING", execution_id=i["execution_id"],
                 dispatch_result_sha256=durable.digest(source["result"]), before=owner.scheduler._shadow_snapshot(),
                 files_before=_snapshot(owner.repo.root), started_clock=now, started_runtime=owner.scheduler.runtime(), observations={},
                 after=None, files_after=None, checked_clock=now, completed_at=None, outcome=None, failure_classification=None, elapsed_seconds=None)
        if second:
            v["schema_version"] = 2
            v["binding"] = second_binding(owner.data, completion_ref(owner))
        d = copy.deepcopy(owner.data); d.update({key: v, "updated_at": durable.now()})
        owner.write(d, event=prefix + "INTENT")
    order = d["source_section"]["units"][owner.identity["unit_id"]]["spec"]["verifier_ids"]
    for vid in order:
        with owner.store.mutex:
            guard(owner)
            inv = _VerifierInvocation(owner.scheduler, i["unit_id"], i["attempt_id"], vid, _active_controller=owner)
        result = _run_observed_verifier(owner.scheduler.supervisor, inv.intent["resolved_argv"], inv.intent["intended_argv"], owner.repo.root,
                                       inv.intent["deadline"] - time.monotonic(), owner.repo.config["max_output_chars"],
                                       invocation=inv, cancel_event=owner.cancel_event)
        with owner.store.mutex:
            owner.live()
            row, entry = owner.scheduler._bind_shadow_observation(inv, result, v["before"])
            row["binding"]["wal"] = {ev: {"sequence": seq, "sha256": owner.store.records[seq - 1]["sha256"]}
                                     for ev, seq in row["binding"]["wal"].items()}
            row.update(after=owner.scheduler._shadow_snapshot(), files_after=_snapshot(owner.repo.root),
                       output=dict(stdout=sanitize_evidence(result["stdout"])[:8192], stderr=sanitize_evidence(result["stderr"])[:8192],
                                   truncated=result["output_truncated"] or len(result["stdout"]) > 8192 or len(result["stderr"]) > 8192,
                                   duration_seconds=result["duration_seconds"]))
            # Recheck scope/index/environment even for a failing or timed out test.
            require(row["after"] == v["before"] and row["files_after"] == v["files_before"])
            v["observations"][vid] = row; v["checked_clock"] = time.monotonic()
            d = copy.deepcopy(owner.data); d.update({key: copy.deepcopy(v), "updated_at": durable.now()})
            owner.write(d, event=prefix + "OBSERVATION")
        if result["termination"] != "natural": break
    with owner.store.mutex:
        owner.live(); owner.scheduler.supervisor.require_idle()
        v.update(after=owner.scheduler._shadow_snapshot(), files_after=_snapshot(owner.repo.root), checked_clock=time.monotonic(),
                 completed_at=durable.now(), phase="COMPLETE")
        v["outcome"], v["failure_classification"] = outcome({**owner.data, "identity": i}, v)
        v["elapsed_seconds"] = v["checked_clock"] - i["started_clock"]
        d = copy.deepcopy(owner.data); d.update({key: copy.deepcopy(v), "updated_at": durable.now()})
        owner.write(d, event=prefix + "COMPLETE")
