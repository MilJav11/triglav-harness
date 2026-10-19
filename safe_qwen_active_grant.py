"""Closed ACTIVE grant selections: v4 issuance-only, v5 consume/allocation-only.

Reuse immutable native evidence and the shared grant transaction, never dispatch.
"""
import copy
import hashlib
from pathlib import Path
import threading

import durable
import safe_qwen_active_recovery as active

_SOURCES = {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
            for name in (Path(__file__).name, "safe_qwen_active_eligibility.py", "retry_eligibility.py")}


def contract():
    active.require(all(hashlib.sha256(Path(__file__).with_name(k).read_bytes()).hexdigest() == v for k, v in _SOURCES.items()))
    return dict(schema_version=1, authorization_schema_version=1, decision_schema_version=1, sources=copy.deepcopy(_SOURCES))


def verification_data(data):
    value = copy.deepcopy(data)
    value.pop("grant_contract")
    value.update(schema_version=3, mode="ACTIVE_ATTEMPT1_VERIFY")
    return value


def grant_data(data):
    """Read-only historical v4 view; never a live mode upgrade or authority."""
    value = dict(data)
    value.pop("allocation_contract")
    value.update(schema_version=4, mode="ACTIVE_ATTEMPT1_GRANT")
    return value


def allocation_contract(identity):
    return dict(schema_version=1, consumption_schema_version=2,
                source_execution_id=identity["execution_id"],
                attempt_id=identity["unit_id"] + "-attempt-2",
                execution_id=durable.digest([identity["id"], identity["run_id"], identity["unit_id"], 2]),
                worker_sha256=identity["worker_sha256"], sources=copy.deepcopy(_SOURCES))


def ai_binding(data):
    i, c = data["identity"], data["allocation_contract"]
    return dict(identity_sha256=durable.digest(i), allocation_contract_sha256=durable.digest(c),
                source_execution_id=i["execution_id"], execution_id=c["execution_id"], worker_sha256=i["worker_sha256"])


def allocation_mode(state):
    """Normalize only the closed v5 allocation selection for the shared validator."""
    d = state[active.KEY]
    active.require(type(d["schema_version"]) is int and d["schema_version"] in (5, 6, 7) and d["mode"] == {5: "ACTIVE_REPAIR_ALLOCATE", 6: "ACTIVE_REPAIR_DISPATCH", 7: "ACTIVE_REPAIR_VERIFY"}[d["schema_version"]])
    i, c = d["identity"], d["allocation_contract"]
    active.fields(c, "schema_version consumption_schema_version source_execution_id attempt_id execution_id worker_sha256 sources")
    active.require(type(c["schema_version"]) is int and c["schema_version"] == 1
                   and type(c["consumption_schema_version"]) is int and c["consumption_schema_version"] == 2)
    expected = allocation_contract(i)
    # Historical source hashes remain historical; only live owners compare pins.
    expected["sources"] = d["grant_contract"]["sources"]
    active.require(c == expected)
    return dict(recovery_id=i["id"], run_id=i["run_id"], unit_id=i["unit_id"], source_attempt_id=i["attempt_id"],
                started_clock=i["started_clock"], task_deadline=i["task_deadline"], controller_session=i["session"])


def validate_allocation(state):
    try:
        from safe_qwen_active_verification import project
        allocation_mode(state)
        d = state[active.KEY]
        # Validators only read this top-level view; the original v4 validator
        # makes its own detached worker/native view. Avoid copying it twice.
        view = dict(state)
        view[active.KEY] = grant_data(d)
        grants = state.get("repair_authorizations", {})
        if grants:
            aid = "repair-" + d["identity"]["id"]
            active.require(set(grants) == {aid})
            g = grants[aid]
            if g["state"] == "CONSUMED":
                durable._validate_repair_allocation(state, g)
                # Only after allocation is a historical source projection
                # necessary. Before it, reuse the exact original v4 validator
                # against the actual ledger, including its grant checks.
                source = project(verification_data(view[active.KEY]))
                view["work_units"] = source
                view["repair_authorizations"] = {aid: durable._granted_form(g)}
                expected = durable._allocated_repair_section(source, g, g["consumption"])
                active.require(durable.encoded(state["work_units"]) == durable.encoded(expected))
        validate(view)  # Original immutable worker/native/grant validation.
        active.require("repair_dispatch" not in state and "repair_verification" not in state)
    except (KeyError, TypeError, ValueError, IndexError, AttributeError) as exc:
        raise durable.DurableError("Invalid ACTIVE v5 allocation selection or history") from exc


def validate(state):
    try:
        from safe_qwen_active_verification import validate as validate_verification
        d = state[active.KEY]
        active.require(type(d["schema_version"]) is int and d["schema_version"] == 4 and d["mode"] == "ACTIVE_ATTEMPT1_GRANT")
        active.fields(d["grant_contract"], "schema_version authorization_schema_version decision_schema_version sources")
        c = d["grant_contract"]
        active.require(all(type(c[k]) is int and c[k] == 1 for k in ("schema_version", "authorization_schema_version", "decision_schema_version")))
        active.fields(c["sources"], " ".join(_SOURCES))
        for value in c["sources"].values(): durable._repair_sha(value)
        view = copy.deepcopy(state)
        view[active.KEY] = verification_data(d)
        view.pop("repair_authorizations", None)
        validate_verification(view)  # Closed v3 worker/native projection, without upgrading history.
        if "repair_authorizations" in state:
            aid = "repair-" + d["identity"]["id"]
            active.require(set(state["repair_authorizations"]) == {aid})
            g = state["repair_authorizations"][aid]
            active.require(g["schema_version"] == 1 and g["state"] == "GRANTED"
                           and g["unit_id"] == d["identity"]["unit_id"] and g["source_attempt_id"] == d["identity"]["attempt_id"]
                           and d["verification"] is not None and d["verification"]["phase"] == "COMPLETE"
                           and d["verification"]["outcome"] == "failed")
    except (KeyError, TypeError, ValueError, IndexError, AttributeError) as exc:
        raise durable.DurableError("Invalid ACTIVE v4 grant selection or history") from exc


def require_owner(store, owner):
    active.require(type(owner) is active._Controller and getattr(store, "_safe_qwen_active_controller", None) is owner
                   and owner.store is store and not owner.closed and not store._repair_write_failed
                   and threading.get_ident() == owner.thread_id and owner.grant_selected and owner.verify_selected
                   and not owner.diagnostic_selected and owner.data["schema_version"] in (4, 5, 6, 7)
                   and owner.data["grant_contract"] == owner.grant_contract == contract())
    if owner.data["schema_version"] in (5, 6, 7):
        require_allocation_owner(store, owner)


def require_allocation_owner(store, owner):
    active.require(type(owner) is active._Controller and getattr(store, "_safe_qwen_active_controller", None) is owner
                   and owner.store is store and not owner.closed and not store._repair_write_failed
                   and threading.get_ident() == owner.thread_id and owner.allocation_selected
                   and owner.verify_selected and owner.grant_selected and not owner.diagnostic_selected
                   and owner.data["schema_version"] in (5, 6, 7) and owner.data["mode"] == {5: "ACTIVE_REPAIR_ALLOCATE", 6: "ACTIVE_REPAIR_DISPATCH", 7: "ACTIVE_REPAIR_VERIFY"}[owner.data["schema_version"]]
                   and owner.data["allocation_contract"] == owner.allocation_contract == allocation_contract(owner.identity))
    if owner.data["schema_version"] in (6, 7):
        from safe_qwen_active_dispatch import require_owner as require_dispatch_owner
        require_dispatch_owner(owner)


def owns_record(store, event, value):
    owner = getattr(store, "_safe_qwen_active_controller", None)
    if type(owner) is not active._Controller or not owner.grant_writing:
        return False
    require_owner(store, owner)
    return active.owns_record(store, event, value)
