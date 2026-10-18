"""Dormant native failure-summary prerequisite; diagnostic data, never authority.

No executor, Gateway, classifier, grant, allocation, WAL write or replay API.
Only a currently registered uninterrupted recovery controller may collect facts.
Output is untrusted prompt material; a valid packet grants no tool permissions.
Current observations retain native exit facts, not assertion stdout/stderr. This
module reports that limitation explicitly and never invents missing output.
"""
import copy
import json
import os
import re
import time

import durable
import environment
import winprocess
from critic_executor import sanitize_evidence
from supervision import _validated_store

MAX_BYTES = 8192
EVENTS = ("invocation_intent", "invocation_bound", "invocation_release", "process_exit", "invocation_complete")


class RepairEvidenceError(ValueError):
    pass


def _require(condition):
    if not condition:
        raise RepairEvidenceError("Incomplete, stale or foreign native failure evidence")


def _fields(value, fields):
    _require(type(value) is dict and set(value) == set(fields.split()))


def _sha(value):
    _require(type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None)


def _bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("ascii")


def encode_repair_evidence(packet):
    """Closed v1 diagnostic encoding; schema validity is NOT authentication."""
    if type(packet) is dict and type(packet.get("schema_version")) is int and packet["schema_version"] == 2:
        return _encode_v2(packet)
    _fields(packet, "schema_version kind trusted authority run_id unit_id source_attempt_id candidate_fingerprint "
                    "spec_sha256 registry_sha256 controller_session source_wal total_failed_verifiers "
                    "included_failed_verifiers omitted_failed_verifiers truncated assertion_output_available failures")
    _require(type(packet["schema_version"]) is int and packet["schema_version"] == 1
             and type(packet["kind"]) is str and packet["kind"] == "native_verifier_failure_summary"
             and packet["trusted"] is False and type(packet["authority"]) is str
             and packet["authority"] == "none" and packet["assertion_output_available"] is False)
    for key in ("run_id", "unit_id", "source_attempt_id"):
        _require(type(packet[key]) is str and 1 <= len(packet[key]) <= 100)
    _require(packet["source_attempt_id"] == packet["unit_id"] + "-attempt-1")
    for key in ("candidate_fingerprint", "spec_sha256", "registry_sha256"):
        _sha(packet[key])
    _fields(packet["controller_session"], "manager_session recovery_sha256 controller_sha256")
    _require(type(packet["controller_session"]["manager_session"]) is int and packet["controller_session"]["manager_session"] >= 0)
    for key in ("recovery_sha256", "controller_sha256"):
        _sha(packet["controller_session"][key])
    def reference(value):
        _fields(value, "sequence sha256")
        _require(type(value["sequence"]) is int and value["sequence"] > 0)
        _sha(value["sha256"])
    reference(packet["source_wal"])
    counts = [packet[k] for k in ("total_failed_verifiers", "included_failed_verifiers", "omitted_failed_verifiers")]
    _require(all(type(v) is int and v >= 0 for v in counts) and counts[0] > 0 and counts[1] > 0
             and counts[0] == counts[1] + counts[2] and type(packet["truncated"]) is bool
             and packet["truncated"] == (counts[2] > 0)
             and type(packet["failures"]) is list and len(packet["failures"]) == counts[1])
    seen = set()
    for row in packet["failures"]:
        _fields(row, "verifier_id verifier_id_sha256 child_id invocation_id exit_code termination diagnostic native_wal")
        _require(type(row["verifier_id"]) is str and 1 <= len(row["verifier_id"]) <= 100
                 and type(row["child_id"]) is str and 1 <= len(row["child_id"]) <= 100
                 and type(row["invocation_id"]) is str and re.fullmatch(r"[0-9a-f]{32}", row["invocation_id"]) is not None
                 and row["child_id"] == "verification_command-" + row["invocation_id"][:12]
                 and row["child_id"] not in seen and type(row["exit_code"]) is int
                 and 0 < row["exit_code"] <= 0xffffffff and row["termination"] == "natural"
                 and type(row["diagnostic"]) is str and len(row["diagnostic"]) <= 512
                 and all(c.isprintable() for c in row["diagnostic"])
                 and row["diagnostic"] == sanitize_evidence(row["diagnostic"])
                 and row["verifier_id"] == sanitize_evidence(row["verifier_id"]))
        _sha(row["verifier_id_sha256"])
        seen.add(row["child_id"])
        _fields(row["native_wal"], " ".join(EVENTS))
        positions = []
        for event in EVENTS:
            reference(row["native_wal"][event])
            positions.append(row["native_wal"][event]["sequence"])
        _require(positions == sorted(set(positions)) and positions[-1] <= packet["source_wal"]["sequence"])
    raw = _bytes(packet)
    _require(len(raw) <= MAX_BYTES)
    return raw



def _encode_v2(packet):
    try:
        base = copy.deepcopy(packet)
        base.update(schema_version=1, assertion_output_available=False)
        available = False
        for row in base["failures"]:
            output = row.pop("assertion_output")
            _fields(output, "stdout stderr")
            for stream in output.values():
                _fields(stream, "available text truncated")
                _require(type(stream["available"]) is bool and type(stream["truncated"]) is bool)
                if stream["available"]:
                    _require(type(stream["text"]) is str and len(stream["text"]) <= 512
                             and stream["text"] == sanitize_evidence(stream["text"])
                             and all(c.isprintable() for c in stream["text"]))
                    available = True
                else:
                    _require(stream["text"] is None and stream["truncated"] is False)
        _require(type(packet["assertion_output_available"]) is bool and packet["assertion_output_available"] == available)
        encode_repair_evidence(base)
        raw = _bytes(packet)
        _require(len(raw) <= MAX_BYTES)
        return raw
    except (KeyError, TypeError, ValueError, AttributeError, IndexError, RecursionError) as exc:
        raise RepairEvidenceError("Malformed v2 diagnostic packet") from exc


def _pack(identity, failures, *, version=1):
    """Bound whole JSON by omitting complete rows, never slicing encoded bytes."""
    _require(type(version) is int and version in (1, 2))
    packet = {**copy.deepcopy(identity), "schema_version": version, "kind": "native_verifier_failure_summary",
              "trusted": False, "authority": "none", "assertion_output_available": False,
              "total_failed_verifiers": len(failures), "included_failed_verifiers": 0,
              "omitted_failed_verifiers": len(failures), "truncated": bool(failures), "failures": []}
    for row in failures:
        trial = copy.deepcopy(packet)
        trial["failures"].append(row)
        trial["included_failed_verifiers"] += 1
        trial["omitted_failed_verifiers"] -= 1
        trial["truncated"] = trial["omitted_failed_verifiers"] > 0
        if version == 2:
            trial["assertion_output_available"] = any(s["available"] for r in trial["failures"] for s in r["assertion_output"].values())
        if len(_bytes(trial)) > MAX_BYTES:
            break
        packet = trial
    encode_repair_evidence(packet)
    return packet


def _output(output):
    """Sanitize complete bounded streams; explicitly represent unavailable data."""
    value = {}
    for name in ("stdout", "stderr"):
        text = output.get(name) if type(output) is dict else None
        if type(text) is not str:
            value[name] = dict(available=False, text=None, truncated=False)
        else:
            clean = " ".join("".join(c if c.isprintable() else " " for c in sanitize_evidence(text)).split())
            excerpt = sanitize_evidence(clean[:512])[:512]
            value[name] = dict(available=True, text=excerpt,
                               truncated=bool(output.get("truncated")) or len(clean) > 512)
    return value


def collect_ai_repair_evidence(controller):
    """Authenticated ACTIVE v6 facts only; never a grant or dispatch capability."""
    from safe_qwen_active_dispatch import guard, diagnostic_packet
    from safe_qwen_active_recovery import _Controller
    store = controller.store if type(controller) is _Controller else None
    _require(type(store) is durable.Store)
    with store.mutex:
        guard(controller, before=True)
        source = copy.deepcopy(store.records[-1])
        packet = diagnostic_packet(store.state)
        encode_repair_evidence(packet)
        guard(controller, before=True)
        _require(store.records[-1] == source)
        return packet


def collect_repair_evidence(controller):
    """Read genuine completed native Attempt 1 facts; do not issue any permission.

    Not connected to recovery, dispatch or worker inputs in this prerequisite.
    Caller JSON and a reloaded Store cannot supply the registered capability.
    """
    _require(type(controller) is durable._RepairController)
    store, scheduler = controller.store, controller.scheduler
    with store.mutex:
        store._require_repair_controller(controller)
        _validated_store(store)
        state, mode = store.state, controller.mode
        _require(scheduler.store is store and scheduler._repair_controller is controller
                 and scheduler.clock is time.monotonic and scheduler.supervisor.store is store
                 and not any(k in state for k in ("repair_authorizations", "repair_dispatch", "repair_verification")))
        live = state["supervision"]["controller"]
        _require(live["pid"] == os.getpid() and live["identity"] == winprocess.identity(os.getpid())
                 and live["boot"] == winprocess.boot_identity()
                 and durable._repair_session(state) == mode["controller_session"]
                 and time.monotonic() < mode["task_deadline"])
        unit = state["work_units"]["units"][mode["unit_id"]]
        _require(len(unit["attempts"]) == 1 and unit["status"] == "VERIFYING")
        attempt = unit["attempts"][0]
        observation = attempt.get("repair_observation")
        _require(type(observation) is dict and "policy_b_shadow" not in attempt
                 and attempt["attempt_id"] == mode["source_attempt_id"]
                 and attempt["completed_at"] is not None and attempt["outcome"] == "failed"
                 and attempt["failure_classification"] == "verification_failed"
                 and observation["phase"] == "OBSERVATIONS_COMPLETE" and observation["verification_complete"] is True
                 and observation["decision"] is None and observation["observations"] == controller.observations
                 and set(controller.observations) == set(unit["spec"]["verifier_ids"]))
        _require(durable.digest(unit["spec"]) == mode["spec_sha256"]
                 and durable.digest(state["options"]["verifier_registry"]) == mode["registry_sha256"]
                 and scheduler.verifier_registry == state["options"]["verifier_registry"]
                 and scheduler.budgets == state["manager"]["budgets"] and scheduler.parent_scope == state["manager"]["scope"])
        candidate = attempt["candidate_snapshot"]
        _require(candidate == attempt["verification_snapshot_before"] == attempt["verification_snapshot_after"]
                 and scheduler._shadow_snapshot() == candidate
                 and scheduler._shadow_fs_snapshot() == observation["candidate_fs_snapshot"]
                 and scheduler.remaining_unit_budget(unit, time.monotonic() - attempt["started_at_clock"], attempt["attempt_id"]) > 0
                 and scheduler.remaining_task_budget() > 0)
        drift = environment.compare(state["environment"], durable.current_fingerprint(store, executor=scheduler.agents.executor))
        _require(drift["decision"] in ("MATCH", "INFORMATIONAL"))
        _require(all(c["state"] in ("EXITED", "RESOLVED") for c in scheduler.supervisor.reconcile(persist=False)))
        source = store.records[-1]
        _require(source["event"] == "state_committed" and source["state"] == state)
        rows = []
        for vid in unit["spec"]["verifier_ids"]:
            observed = controller.observations[vid]
            binding, native = observed["binding"], observed["native"]
            child = state["supervision"]["children"][binding["child_id"]]
            inv = child["invocation"]
            _require(inv["phase"] == "COMPLETE" and child["run_id"] == state["run_id"]
                     and inv["unit_id"] == mode["unit_id"] and inv["attempt_id"] == attempt["attempt_id"]
                     and inv["verifier_id"] == vid and inv["candidate_fingerprint"] == candidate["fingerprint"]
                     and inv["spec_sha256"] == mode["spec_sha256"] and inv["registry_sha256"] == mode["registry_sha256"]
                     and binding["invocation_id"] == child["token"] and child["state"] == "EXITED"
                     and native["cleanup"] == "observed_empty" and native["execution_started"] is True
                     and native["termination"] == "natural" and native["child_id"] == child["child_id"]
                     and native["job"] == child["job"] and native["verifier"] == {"pid": child["pid"], "identity": child["identity"]}
                     and type(child["exit_code"]) is int and native["observed_exit_code"] == child["exit_code"]
                     and native["intended_argv"] == inv["intended_argv"]
                     and native["resolved_argv"] == inv["resolved_argv"])
            records = [r for r in store.records if (r.get("transition") or {}).get("child_id") == child["child_id"]
                       and (r.get("transition") or {}).get("event") in EVENTS]
            _require([r["transition"]["event"] for r in records] == list(EVENTS)
                     and binding["wal"] == {r["transition"]["event"]: r["sequence"] for r in records})
            result = attempt["verifier_result"][vid]
            _require(result["controller_evidence"] == native and result["exit_code"] == child["exit_code"]
                     and type(result["passed"]) is bool and result["passed"] == (child["exit_code"] == 0))
            if child["exit_code"] != 0:
                display = sanitize_evidence(vid)
                rows.append(dict(verifier_id=display, verifier_id_sha256=durable.digest(vid), child_id=child["child_id"],
                                 invocation_id=child["token"], exit_code=child["exit_code"], termination="natural",
                                 diagnostic=sanitize_evidence(f"Verifier {display} exited with code {child['exit_code']} after native descendant cleanup."),
                                 native_wal={r["transition"]["event"]: {k: r[k] for k in ("sequence", "sha256")} for r in records}))
        identity = dict(run_id=state["run_id"], unit_id=mode["unit_id"], source_attempt_id=attempt["attempt_id"],
                        candidate_fingerprint=candidate["fingerprint"], spec_sha256=mode["spec_sha256"], registry_sha256=mode["registry_sha256"],
                        controller_session=copy.deepcopy(mode["controller_session"]),
                        source_wal={k: source[k] for k in ("sequence", "sha256")})
        packet = _pack(identity, rows)
        _validated_store(store)
        store._require_repair_controller(controller)
        _require(store.records[-1] == source and scheduler._shadow_snapshot() == candidate
                 and scheduler._shadow_fs_snapshot() == observation["candidate_fs_snapshot"]
                 and time.monotonic() < mode["task_deadline"]
                 and scheduler.remaining_unit_budget(unit, time.monotonic() - attempt["started_at_clock"], attempt["attempt_id"]) > 0
                 and scheduler.remaining_task_budget() > 0)
        drift = environment.compare(state["environment"], durable.current_fingerprint(store, executor=scheduler.agents.executor))
        _require(drift["decision"] in ("MATCH", "INFORMATIONAL"))
        return packet
