"""Diagnostic-only Policy B bridge for one uninterrupted ACTIVE v3 controller.

No new WAL schema: the closed v3 history remains immutable. A returned decision
references its durable sources but is neither persisted authority nor reusable.
"""
import copy
import hashlib
from pathlib import Path
import threading
import time

import durable
import retry_eligibility as policy
import safe_qwen_active_recovery as active
import safe_qwen_active_verification as verification
from offline_scripted_executor import _snapshot

require = active.require
_SOURCES = {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
            for name in (Path(__file__).name, "retry_eligibility.py")}


def contract():
    require(all(hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() == value
                for name, value in _SOURCES.items()))
    return dict(schema_version=1, policy="B", sources=copy.deepcopy(_SOURCES))


def _guard(owner):
    # A matching dictionary or a reloaded controller can never reach the frozen
    # classifier through this entry point. live() validates the actual WAL too.
    require(type(owner) is active._Controller)
    owner.live()
    d, i, scheduler = owner.data, owner.identity, owner.scheduler
    if d["schema_version"] in (4, 5, 6, 7):
        from safe_qwen_active_grant import require_owner
        require_owner(owner.store, owner)
        require(owner.grant_started)
    else:
        require(owner.diagnostic_selected and owner.diagnostic_started and owner.diagnostic_contract == contract()
                and d["schema_version"] == 3 and d["mode"] == "ACTIVE_ATTEMPT1_VERIFY")
    require(owner.verify_selected and threading.get_ident() == owner.thread_id
            and owner.network_thread is None and not owner.cancel_event.is_set()
            and d["verification_contract"] == verification.contract()
            and d["phase"] == "COMPLETE" and owner.cleanup == d["result"]["cleanup_proof"]
            and owner.cleanup is not None and d["result"]["outcome"] == "executor_succeeded_unverified"
            and d["verification"]["phase"] == "COMPLETE"
            and i["session"] == durable._repair_session(owner.store.state))
    require(scheduler.budgets == owner.store.state["manager"]["budgets"]
            and scheduler.parent_scope == owner.store.state["manager"]["scope"]
            and scheduler.verifier_registry == owner.store.state["options"]["verifier_registry"])
    scheduler.supervisor.require_idle()
    v = d["verification"]
    require(scheduler._shadow_snapshot() == v["after"] == d["result"]["candidate"]
            and _snapshot(owner.repo.root) == v["files_after"] == d["result"]["files_after"])
    unit = owner.store.state["work_units"]["units"][i["unit_id"]]
    require(len(unit["attempts"]) == 1 and unit["current_attempt"] is None
            and unit["attempts"][0]["attempt_id"] == i["attempt_id"]
            and unit["attempts"][0]["ai_execution_id"] == i["execution_id"])
    now = time.monotonic()
    budgets = dict(remaining_unit_budget=min(scheduler.remaining_unit_budget(unit, now - i["started_clock"], i["attempt_id"]),
                                            i["unit_deadline"] - now, i["deadline"] - now),
                   remaining_task_budget=min(scheduler.remaining_task_budget(), i["task_deadline"] - now, i["deadline"] - now))
    return unit, budgets


def _references(owner):
    d, records = owner.data, owner.store.records
    refs = {}
    for event in ("INTENT", "RELEASE", "COMPLETE", "VERIFY_INTENT", "VERIFY_COMPLETE"):
        matches = [r for r in records if r.get(active.MARKER) ==
                   {"event": event, "execution_id": owner.identity["execution_id"]}]
        require(len(matches) == 1)
        r = matches[0]
        refs[event] = dict(sequence=r["sequence"], sha256=r["sha256"])
    require([r["sequence"] for r in refs.values()] == sorted(r["sequence"] for r in refs.values()))
    completed = records[refs["VERIFY_COMPLETE"]["sequence"] - 1]["state"][active.KEY]
    require(durable.encoded(completed) == durable.encoded(d))
    # validate_history (inside live) authenticates every native reference before
    # we label a detached native record for the classifier's closed v1 schema.
    return refs


def _evaluate(owner):
    """Internal fresh evaluation from a registered, explicitly selected owner."""
    unit, budgets = _guard(owner)
    refs = _references(owner)
    d, i, v = owner.data, owner.identity, owner.data["verification"]
    projected = copy.deepcopy(unit)
    att = projected["attempts"][0]
    att["pre_attempt_snapshot"] = copy.deepcopy(d["snapshot_before"])
    for vid, row in v["observations"].items():
        child = owner.store.state["supervision"]["children"][row["binding"]["child_id"]]
        inv = child["invocation"]
        att["verifier_result"][vid]["controller_evidence"] = {
            **copy.deepcopy(row["native"]), "run_id": child["run_id"],
            "unit_id": inv["unit_id"], "attempt_id": inv["attempt_id"]}
    revision, sequence = owner.store.state["revision"], len(owner.store.records)
    decision = policy.classify_retry_eligibility(
        projected, policy=policy.POLICY_B, supervisor=owner.scheduler.supervisor, require_idle=True,
        verifier_registry=owner.scheduler.verifier_registry, environment_status="SAFE", **budgets,
        candidate_snapshot=d["result"]["candidate"], verification_snapshot_before=v["before"],
        verification_snapshot_after=v["after"], repo_mutated_during_verification=False,
        context={"run_id": i["run_id"]})
    # The frozen classifier catches exceptions. Refuse any swallowed uncertain
    # write, changed history, drift or exhausted budget before returning ALLOW.
    require(owner.store.state["revision"] == revision and len(owner.store.records) == sequence)
    _, fresh_budgets = _guard(owner)
    require(_references(owner) == refs)
    if decision.is_allowed(): require(all(value >= 1 for value in fresh_budgets.values()))
    return decision, projected, budgets, refs


def classify_for_grant(owner):
    from safe_qwen_active_grant import require_owner
    require_owner(owner.store, owner)
    require(owner.grant_started and owner.pending is None)
    return _evaluate(owner)[0]


def run(owner):
    require(type(owner) is active._Controller)
    with owner.store.mutex:
        require(owner.diagnostic_selected and not owner.diagnostic_started and not owner.grant_selected)
        owner.diagnostic_started = True  # Single evaluation, including refusals.
        decision, projected, budgets, refs = _evaluate(owner)
        i, v = owner.identity, owner.data["verification"]
        result = dict(schema_version=1, diagnostic_only=True, persisted=False,
                      run_id=i["run_id"], unit_id=i["unit_id"], attempt_id=i["attempt_id"], execution_id=i["execution_id"],
                      session=copy.deepcopy(i["session"]), contract=copy.deepcopy(owner.diagnostic_contract),
                      sources=refs, native_bindings={vid: copy.deepcopy(row["binding"]) for vid, row in v["observations"].items()},
                      candidate_fingerprint=v["after"]["fingerprint"], projected_evidence_sha256=durable.digest(projected),
                      budgets=budgets, decision=decision.to_dict())
        require(len(durable.encoded(result)) <= 65536)
        return result
