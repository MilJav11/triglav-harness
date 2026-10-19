"""Dormant authorization schema/replay tests; no authenticated runtime grants.

The source WAL, Supervisor identities and native verifier observations are real
offline controller records. The future decision/grant envelopes are SYNTHETIC:
only this test writes them directly to its disposable ledger. Production Store
APIs have no issuer, and replay creates no permission/capability for dispatch.
"""
import copy
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import durable
import manager
import supervision
import work_units
from work_unit_scheduler import ScriptedExecutor, WorkUnitScheduler
from test_verifier_invocations import make_fixture


def wal_ref(record):
    return {"sequence": record["sequence"], "sha256": record["sha256"]}


def synthetic_record(records, event, **fields):
    """Test-only raw WAL construction, deliberately outside Store write APIs."""
    record = dict(sequence=len(records) + 1, time=durable.now(), event=event,
                  previous=records[-1]["sha256"] if records else None, **copy.deepcopy(fields))
    record["sha256"] = durable.digest(record)
    records.append(record)
    return record


@unittest.skipUnless(os.name == "nt", "Real retained Windows verifier handles required")
class RepairAuthorizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.seed_temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.seed_temp.cleanup)
        with patch("harness.Gateway", side_effect=AssertionError("No models")), \
                patch("durable.Gateway", side_effect=AssertionError("No models")), \
                patch("socket.socket", side_effect=AssertionError("No network")), \
                patch.dict(os.environ, {"PYTHONDONTWRITEBYTECODE": "1", "GIT_OPTIONAL_LOCKS": "0"}):
            cls.repo, store, sup, scheduler = make_fixture(Path(cls.seed_temp.name))
            # Set the future repair-capable spec BEFORE native intent is frozen.
            section = scheduler.get_section()
            unit = section["units"]["unit-1"]
            unit["spec"]["limits"]["max_attempts"] = 2
            cls.repo.write("calculator.py", "def multiply(a, b): return a + b\n")
            candidate = durable.snapshot(cls.repo)
            unit["attempts"][0]["pre_attempt_snapshot"] = candidate
            scheduler._persist_unit(section, unit)
            sup.heartbeat()  # Actual controller process/boot identity, no thread.
            result = scheduler.invoke_native_verifier("unit-1", "unit-1-attempt-1", "check-calc")
            if result["exit_code"] != 1:
                raise AssertionError("Expected actual deterministic unittest failure")
            section = scheduler.get_section()
            unit = section["units"]["unit-1"]
            attempt = unit["attempts"][0]
            attempt.update(candidate_snapshot=candidate, verification_snapshot_before=candidate,
                           verification_snapshot_after=durable.snapshot(cls.repo))
            work_units.complete_attempt(unit, attempt["attempt_id"], outcome="failed",
                                        failure_classification="verification_failed",
                                        verifier_result={"check-calc": {"exit_code": 1, "passed": False,
                                                                      "controller_evidence": result["controller_evidence"]}},
                                        timeout_info={"elapsed_seconds": 6.0})
            scheduler._persist_unit(section, unit)
            child = next(iter(store.state["supervision"]["children"].values()))
            evidence = store.artifact(f"repair-evidence/{attempt['attempt_id']}/{child['token']}.json",
                                      result["controller_evidence"])
            session, budget = durable._repair_context(store.state, unit, attempt)
            cls.grant = dict(schema_version=1, state="GRANTED", run_id=store.state["run_id"],
                             unit_id="unit-1", source_attempt_id=attempt["attempt_id"], authorization_id="repair-1",
                             candidate_fingerprint=candidate["fingerprint"], spec_sha256=durable.digest(unit["spec"]),
                             registry_sha256=durable.digest(store.state["options"]["verifier_registry"]),
                             source_wal=wal_ref(store.records[-1]), policy_wal=None,
                             policy_b=dict(policy="B", eligible=True, decision="ALLOW",
                                           reason_code="ALLOW_VERIFIER_FAILURE", failed_verifiers=["check-calc"]),
                             native_evidence={"check-calc": dict(child_id=child["child_id"], invocation_id=child["token"],
                                 wal={r["transition"]["event"]: wal_ref(r) for r in store.records
                                      if (r.get("transition") or {}).get("child_id") == child["child_id"]
                                      and r["transition"]["event"] in durable._INVOCATION_EVENTS}, evidence=evidence)},
                             budget=budget, recovery_session=session)
            cls.seed_directory = store.directory
            cls.base_records = copy.deepcopy(store.records)
            cls.base_state = copy.deepcopy(store.state)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = durable.Store(Path(self.temp.name), self.base_state["run_id"])
        shutil.copytree(self.seed_directory, self.store.directory)
        self.store.load()
        self.records = copy.deepcopy(self.base_records)
        self.grant = copy.deepcopy(type(self).grant)
        self.addCleanup(patch.stopall)
        patch("harness.Gateway", side_effect=AssertionError("No models")).start()
        patch("durable.Gateway", side_effect=AssertionError("No models")).start()
        patch("socket.socket", side_effect=AssertionError("No network")).start()
        patch("socket.create_connection", side_effect=AssertionError("No network")).start()

    def envelopes(self, grant=None):
        grant = copy.deepcopy(grant or self.grant)
        decision = {k: grant[k] for k in ("schema_version", "run_id", "unit_id", "source_attempt_id",
                                         "source_wal", "policy_b", "recovery_session")}
        policy_record = synthetic_record(self.records, "repair_policy_b_decision", decision=decision)
        grant["policy_wal"] = wal_ref(policy_record)
        state = copy.deepcopy(self.records[-2]["state"])
        state.update(revision=state["revision"] + 1, updated_at=durable.now(),
                     repair_authorizations={grant["authorization_id"]: grant})
        synthetic_record(self.records, "state_committed", state=state, transition=None,
                         repair_authorization={"event": "repair_authorization_granted",
                                               "authorization_id": grant["authorization_id"]})
        return state

    def write_records(self, *, cache=False, tail=b""):
        # Rehash modified records so failures exercise semantic replay validation.
        previous = None
        for seq, record in enumerate(self.records, 1):
            record.update(sequence=seq, previous=previous)
            record.pop("sha256", None)
            record["sha256"] = durable.digest(record)
            previous = record["sha256"]
        (self.store.directory / "events.jsonl").write_bytes(
            b"".join(durable.encoded(r) + b"\n" for r in self.records) + tail)
        if cache:
            durable.atomic_write(self.store.directory / "state.json", durable.encoded(self.records[-1]["state"]))

    def rejected(self):
        before = copy.deepcopy(self.store.state)
        ledger = (self.store.directory / "events.jsonl").read_bytes()
        with self.assertRaises(durable.DurableError):
            self.store.load()
        self.assertEqual(self.store.state, before)
        self.assertEqual((self.store.directory / "events.jsonl").read_bytes(), ledger)

    def test_canonical_record_reload_stale_cache_and_detached_result(self):
        expected = self.envelopes()
        self.write_records()
        result = self.store.load()
        self.assertEqual(result, expected)
        result["repair_authorizations"].clear()
        self.assertEqual(len(self.store.state["repair_authorizations"]), 1)
        self.store.repair_cache()
        self.assertEqual(self.store.load(), expected)

    def test_closed_schema_missing_fields(self):
        self.envelopes()
        for key in self.grant:
            with self.subTest(key=key):
                state = copy.deepcopy(self.records[-1]["state"])
                del state["repair_authorizations"]["repair-1"][key]
                with self.assertRaises(durable.DurableError):
                    durable.validate_state(state)

    def test_closed_schema_wrong_types_and_malformed_values(self):
        self.envelopes()
        values = {"schema_version": True, "state": "CONSUMED", "run_id": 1, "unit_id": None,
                  "source_attempt_id": [], "authorization_id": {}, "candidate_fingerprint": "x",
                  "spec_sha256": 1, "registry_sha256": None, "source_wal": {"sequence": True, "sha256": "0" * 64},
                  "policy_wal": [], "policy_b": {}, "native_evidence": [], "budget": {}, "recovery_session": {}}
        for key, value in values.items():
            with self.subTest(key=key):
                state = copy.deepcopy(self.records[-1]["state"])
                state["repair_authorizations"]["repair-1"][key] = value
                with self.assertRaises(durable.DurableError):
                    durable.validate_state(state)
        state = copy.deepcopy(self.records[-1]["state"])
        state["repair_authorizations"]["repair-1"]["extra"] = True
        with self.assertRaises(durable.DurableError):
            durable.validate_state(state)

    def test_nested_fields_are_closed_and_type_exact(self):
        self.envelopes()
        valid = self.records[-1]["state"]
        paths = [("policy_b", "eligible"), ("policy_b", "decision"), ("policy_b", "failed_verifiers"),
                 ("recovery_session", "manager_session"), ("recovery_session", "controller_sha256"),
                 ("budget", "unit_deadline"), ("source_wal", "sequence"),
                 ("native_evidence", "check-calc", "invocation_id"),
                 ("native_evidence", "check-calc", "evidence", "path"),
                 ("native_evidence", "check-calc", "wal", "invocation_intent", "sequence")]
        for path in paths:
            for wrong in (None, True, [], {}):
                with self.subTest(path=path, wrong=wrong):
                    state = copy.deepcopy(valid)
                    value = state["repair_authorizations"]["repair-1"]
                    for key in path[:-1]:
                        value = value[key]
                    value[path[-1]] = wrong
                    if path == ("policy_b", "eligible") and wrong is True:
                        continue  # The sole valid boolean.
                    with self.assertRaises(durable.DurableError):
                        durable.validate_state(state)
        for section in ("policy_b", "budget", "recovery_session", "source_wal", "policy_wal"):
            state = copy.deepcopy(valid)
            state["repair_authorizations"]["repair-1"][section]["extra"] = 0
            with self.assertRaises(durable.DurableError):
                durable.validate_state(state)

    def test_cache_only_json_cannot_reconstruct_a_grant(self):
        state = self.envelopes()
        durable.atomic_write(self.store.directory / "state.json", durable.encoded(state))
        self.rejected()

    def test_hash_chain_corruption_is_rejected(self):
        self.envelopes()
        self.write_records()
        ledger = self.store.directory / "events.jsonl"
        ledger.write_bytes(ledger.read_bytes().replace(b'"authorization_id": "repair-1"',
                                                     b'"authorization_id": "repair-X"'))
        self.rejected()

    def test_wal_envelopes_reject_extra_fields_and_wrong_types(self):
        self.envelopes()
        original = copy.deepcopy(self.records)
        for index in (-2, -1):
            for key, value in (("extra", True), ("time", False)):
                with self.subTest(index=index, key=key):
                    self.records = copy.deepcopy(original)
                    self.records[index][key] = value
                    self.write_records()
                    self.rejected()

    def test_foreign_run_unit_attempt_and_candidate_spec_registry(self):
        for key, value in (("run_id", "foreign-run"), ("unit_id", "foreign-unit"),
                           ("source_attempt_id", "unit-1-attempt-2"), ("candidate_fingerprint", "0" * 64),
                           ("spec_sha256", "0" * 64), ("registry_sha256", "0" * 64)):
            with self.subTest(key=key):
                self.records = copy.deepcopy(self.base_records)
                grant = copy.deepcopy(self.grant)
                grant[key] = value
                self.envelopes(grant)
                self.write_records()
                self.rejected()

    def test_duplicate_unit_grant_conflicting_id_and_replayed_marker(self):
        self.envelopes()
        original = copy.deepcopy(self.records)
        for mutation in ("duplicate", "conflict", "replayed_marker", "deleted"):
            with self.subTest(mutation=mutation):
                self.records = copy.deepcopy(original)
                state = copy.deepcopy(self.records[-1]["state"])
                marker = None
                if mutation == "duplicate":
                    grant = copy.deepcopy(state["repair_authorizations"]["repair-1"])
                    grant["authorization_id"] = "repair-2"
                    state["repair_authorizations"]["repair-2"] = grant
                elif mutation == "conflict":
                    state["repair_authorizations"]["repair-1"]["budget"]["unit_elapsed_seconds"] += 1
                elif mutation == "deleted":
                    del state["repair_authorizations"]
                else:
                    marker = self.records[-1]["repair_authorization"]
                state["revision"] += 1
                fields = {"repair_authorization": marker} if marker else {}
                synthetic_record(self.records, "state_committed", state=state, transition=None, **fields)
                self.write_records()
                self.rejected()

    def test_bad_source_policy_and_invocation_wal_references(self):
        for key in ("source", "policy", "invocation", "invocation_id", "child_id"):
            with self.subTest(key=key):
                self.records = copy.deepcopy(self.base_records)
                self.envelopes()
                grant = self.records[-1]["state"]["repair_authorizations"]["repair-1"]
                binding = grant["native_evidence"]["check-calc"]
                if key in ("source", "policy"):
                    grant[key + "_wal"]["sha256"] = "0" * 64
                elif key == "invocation":
                    binding["wal"]["invocation_complete"] = copy.deepcopy(binding["wal"]["invocation_intent"])
                else:
                    binding[key] = "foreign"
                self.write_records()
                self.rejected()

    def test_missing_changed_and_foreign_native_evidence(self):
        self.envelopes()
        self.write_records()
        path = self.store.directory / self.grant["native_evidence"]["check-calc"]["evidence"]["path"]
        raw = path.read_bytes()
        for content in (None, b"{}", raw.replace(b'controller_native_handles', b'foreign_native_handles')):
            with self.subTest(content=content is None):
                if content is None:
                    path.unlink()
                else:
                    path.write_bytes(content)
                self.rejected()
        path.write_bytes(raw)

    def test_evidence_with_valid_hash_but_wrong_process_is_refused(self):
        self.envelopes()
        grant = self.records[-1]["state"]["repair_authorizations"]["repair-1"]
        ref = grant["native_evidence"]["check-calc"]["evidence"]
        native = self.store.read_evidence(ref)
        native["verifier"]["pid"] += 1
        ref["sha256"] = durable.digest(native)
        (self.store.directory / ref["path"]).write_bytes(durable.encoded(native))
        self.write_records()
        self.rejected()

    def test_stale_session_budget_deadline_and_policy_deny(self):
        for key in ("session", "accounting", "deadline", "runtime", "deny"):
            with self.subTest(key=key):
                self.records = copy.deepcopy(self.base_records)
                grant = copy.deepcopy(self.grant)
                if key == "session":
                    grant["recovery_session"]["manager_session"] += 1
                elif key == "accounting":
                    grant["budget"]["accounting_sha256"] = "0" * 64
                elif key == "deadline":
                    grant["budget"]["unit_deadline"] += 1
                elif key == "runtime":
                    grant["budget"]["task_runtime_seconds"] = float("inf")
                    self.envelopes()
                    self.records[-1]["state"]["repair_authorizations"]["repair-1"] = grant
                    with self.assertRaises(durable.DurableError):
                        durable.validate_state(self.records[-1]["state"])
                    continue
                else:
                    grant["policy_b"]["decision"] = "DENY"
                self.envelopes(grant)
                self.write_records()
                self.rejected()

    def test_torn_extension_or_decision_without_grant_is_rejected(self):
        self.envelopes()
        original = copy.deepcopy(self.records)
        for boundary in ("decision", "grant", "after_grant"):
            with self.subTest(boundary=boundary):
                self.records = copy.deepcopy(original)
                if boundary == "decision":
                    self.records.pop()
                    tail = b""
                elif boundary == "grant":
                    tail = durable.encoded(self.records.pop())[:100]
                else:
                    tail = b'{"sequence":'
                self.write_records(tail=tail)
                self.rejected()

    def test_grant_without_decision_or_without_transition_is_rejected(self):
        self.envelopes()
        original = copy.deepcopy(self.records)
        for key in ("decision", "transition"):
            self.records = copy.deepcopy(original)
            if key == "decision":
                self.records.pop(-2)
            else:
                del self.records[-1]["repair_authorization"]
            self.write_records()
            self.rejected()

    def test_consumed_event_or_state_is_rejected(self):
        self.envelopes()
        original = copy.deepcopy(self.records)
        for key in ("event", "state"):
            self.records = copy.deepcopy(original)
            if key == "event":
                synthetic_record(self.records, "repair_authorization_consumed", authorization_id="repair-1")
            else:
                self.records[-1]["state"]["repair_authorizations"]["repair-1"]["state"] = "CONSUMED"
            self.write_records()
            self.rejected()

    def test_consumed_cannot_be_smuggled_through_supervision_transition(self):
        before = (self.store.directory / "events.jsonl").read_bytes()
        with self.assertRaises(durable.DurableError):
            self.store.commit(supervision_event={"event": "repair_authorization_consumed"})
        self.assertEqual((self.store.directory / "events.jsonl").read_bytes(), before)
        self.envelopes()
        state = copy.deepcopy(self.records[-1]["state"])
        state["revision"] += 1
        synthetic_record(self.records, "state_committed", state=state,
                         transition={"event": "repair_authorization_consumed"})
        self.write_records()
        self.rejected()

    def test_store_apis_and_guard_cannot_issue_granted(self):
        state = self.envelopes()
        grants = state["repair_authorizations"]
        before = (self.store.directory / "events.jsonl").read_bytes()
        operations = [lambda: self.store.commit(repair_authorizations=grants),
                      lambda: self.store.append("repair_policy_b_decision", decision=self.records[-2]["decision"]),
                      lambda: self.store.append("state_committed", state=state, transition=None),
                      lambda: self.store.append_record("repair_authorization_granted", authorization=grants),
                      lambda: self.store.commit(pre_write_guard=lambda value: value.update(repair_authorizations=grants)),
                      lambda: self.store.commit_terminal("cannot-issue", repair_authorizations=grants)]
        for operation in operations:
            with self.assertRaises(durable.DurableError):
                operation()
            self.assertEqual((self.store.directory / "events.jsonl").read_bytes(), before)

    def test_reload_never_allocates_attempt_two_or_dispatches(self):
        self.envelopes()
        self.write_records()
        self.store.load()
        before = (self.store.directory / "events.jsonl").read_bytes()
        executor = ScriptedExecutor()
        with patch.object(executor, "execute", side_effect=AssertionError("No executor")), \
                patch.object(self.repo, "execute", side_effect=AssertionError("No verifier")), \
                patch("winprocess.ControllerVerifier", side_effect=AssertionError("No native relaunch")):
            scheduler = WorkUnitScheduler(self.store, self.repo, supervisor=supervision.Supervisor(self.store))
            self.assertEqual(scheduler.reconciliation_stop.reason, "REPAIR_AUTHORIZATION_DORMANT")
            self.assertEqual(scheduler.run_sequence()["status"], "HUMAN_ACTION_REQUIRED")
            section = scheduler.get_section()
            with self.assertRaises(manager.Stop):
                scheduler.execute_attempt(section["units"]["unit-1"], section, 2)
            with self.assertRaises(manager.Stop):
                scheduler.invoke_native_verifier("unit-1", "unit-1-attempt-1", "check-calc")
            with self.assertRaises(manager.Stop):
                scheduler.revalidate_milestone(section)
            scheduler.reconciliation_stop = None
            with self.assertRaises(manager.Stop):
                supervision._VerifierInvocation(scheduler, "unit-1", "unit-1-attempt-1", "check-calc")
        self.assertEqual(len(self.store.state["work_units"]["units"]["unit-1"]["attempts"]), 1)
        self.assertEqual((self.store.directory / "events.jsonl").read_bytes(), before)

    def test_failed_cache_commit_preserves_grant_and_latches_writer(self):
        self.envelopes()
        self.write_records()
        self.store.load()
        original = copy.deepcopy(self.store.state["repair_authorizations"])
        with patch("durable.atomic_write", side_effect=OSError("cache interrupted")), self.assertRaises(OSError):
            self.store.commit(current_step="awaiting repair review")
        with self.assertRaises(durable.DurableError):
            self.store.commit(current_step="must reload first")
        self.store.load()
        self.assertEqual(self.store.state["repair_authorizations"], original)
        self.assertEqual(self.store.state["current_step"], "awaiting repair review")

    def test_failed_wal_flush_latches_writer_and_reload_validates_bytes(self):
        self.envelopes()
        self.write_records()
        self.store.load()
        with patch("durable.os.fsync", side_effect=OSError("flush interrupted")), self.assertRaises(OSError):
            self.store.commit(current_step="flush outcome unknown")
        before = (self.store.directory / "events.jsonl").read_bytes()
        with self.assertRaises(durable.DurableError):
            self.store.commit(current_step="must reload first")
        self.assertEqual((self.store.directory / "events.jsonl").read_bytes(), before)
        self.store.load()
        self.assertEqual(self.store.state["current_step"], "flush outcome unknown")

    def test_shadow_allow_never_creates_a_grant(self):
        from test_policy_b_shadow import make_shadow_fixture
        with tempfile.TemporaryDirectory() as temp:
            repo, store, sup, scheduler, executor = make_shadow_fixture(Path(temp), single=True)
            result = scheduler.observe_policy_b_shadow("unit-1")
            self.assertEqual(result["decision"]["decision"], "ALLOW")
            self.assertNotIn("repair_authorizations", store.load())
            self.assertEqual(len(executor.calls), 1)
        self.envelopes()
        self.records[-1]["state"]["work_units"]["units"]["unit-1"]["attempts"][0]["policy_b_shadow"] = {"decision": "ALLOW"}
        self.write_records()
        self.rejected()

    def test_m0_commit_and_torn_tail_behavior_are_unchanged(self):
        self.assertNotIn("repair_authorizations", self.store.state)
        self.store.commit(current_step="M0 unchanged")
        with (self.store.directory / "events.jsonl").open("ab") as stream:
            stream.write(b'{"torn":')
        self.assertEqual(self.store.load()["current_step"], "M0 unchanged")
        self.assertTrue(self.store.tail)
        self.store.repair_cache()
        self.assertFalse(self.store.tail)
        self.assertNotIn("repair_authorizations", self.store.load())


if __name__ == "__main__":
    unittest.main()
