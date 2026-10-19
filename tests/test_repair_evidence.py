"""Dormant diagnostic prerequisite; real native failure, zero grants/Attempt 2.

Formatting fixtures are synthetic diagnostic data, not authenticated runtime
observations or grants. The collector tests use the actual live controller and
native verifier WAL, stopping before the existing grant issuance method runs.
"""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import durable
import repair_evidence as evidence
from test_policy_b_shadow import make_shadow_fixture


class Collected(Exception):
    pass


@unittest.skipUnless(os.name == "nt", "Actual native Windows verifier evidence required")
class NativeRepairEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(patch.stopall)
        for target in ("harness.Gateway", "durable.Gateway", "socket.socket", "socket.create_connection",
                       "durable.Store._consume_repair_grant", "durable.Store.commit_terminal"):
            patch(target, side_effect=AssertionError("No models, network, consumption or acceptance")).start()
        patch.dict(os.environ, {"PYTHONDONTWRITEBYTECODE": "1", "GIT_OPTIONAL_LOCKS": "0"}).start()
        self.repo, self.store, self.sup, self.scheduler, self.executor = make_shadow_fixture(Path(self.temp.name))
        self.packet = self.controller = None

    def collect(self, probe=None):
        def stop(controller):
            self.controller = controller
            wal = (self.store.directory / "events.jsonl").read_bytes()
            state = durable.encoded(self.store.state)
            records = durable.encoded(self.store.records)
            if probe is not None:
                probe(controller)
            else:
                self.packet = evidence.collect_repair_evidence(controller)
            self.assertEqual(wal, (self.store.directory / "events.jsonl").read_bytes())
            self.assertEqual(state, durable.encoded(self.store.state))
            self.assertEqual(records, durable.encoded(self.store.records))
            raise Collected()
        with patch.object(self.store, "_issue_repair_grant", side_effect=stop), \
                patch("retry_eligibility.classify_retry_eligibility", side_effect=AssertionError("Diagnostics do not classify/authorize")):
            with self.assertRaises(Collected):
                self.scheduler.authorize_repair_recovery("unit-1")
        self.assertEqual(len(self.executor.calls), 1)
        self.assertEqual(len(self.store.state["work_units"]["units"]["unit-1"]["attempts"]), 1)
        self.assertNotIn("repair_authorizations", self.store.state)
        self.assertNotIn("repair_dispatch", self.store.state)
        self.assertIsNone(self.store.state["last_verified_checkpoint"])
        return self.packet

    def test_real_native_failure_read_only_packet_and_exact_wal_references(self):
        packet = self.collect()
        encoded = evidence.encode_repair_evidence(packet)
        self.assertLessEqual(len(encoded), 8192)
        self.assertEqual(json.loads(encoded), packet)
        self.assertFalse(packet["trusted"])
        self.assertEqual(packet["authority"], "none")
        self.assertFalse(packet["assertion_output_available"])
        self.assertEqual(packet["total_failed_verifiers"], 1)
        self.assertEqual(packet["failures"][0]["verifier_id"], "check-calc")
        self.assertEqual(packet["failures"][0]["exit_code"], 1)
        self.assertFalse(packet["truncated"])
        for event, ref in packet["failures"][0]["native_wal"].items():
            record = self.store.get_record(ref["sequence"])
            self.assertEqual(ref["sha256"], record["sha256"])
            self.assertEqual(record["transition"]["event"], event)
        source = self.store.get_record(packet["source_wal"]["sequence"])
        self.assertEqual(packet["source_wal"]["sha256"], source["sha256"])

    def test_closed_controller_and_reloaded_store_cannot_collect(self):
        self.collect()
        wal = (self.store.directory / "events.jsonl").read_bytes()
        with self.assertRaises(durable.DurableError):
            evidence.collect_repair_evidence(self.controller)
        loaded = durable.Store(self.store.directory.parent, self.store.directory.name)
        loaded.load()
        fake = durable._RepairController(loaded, self.scheduler, loaded.state["repair_recovery"])
        with self.assertRaises(durable.DurableError):
            evidence.collect_repair_evidence(fake)
        self.assertEqual(wal, (self.store.directory / "events.jsonl").read_bytes())

    def test_json_cannot_supply_a_live_controller(self):
        for value in ({}, {"state": "GRANTED"}, {"decision": "ALLOW"}, None, object()):
            with self.subTest(value=type(value)), self.assertRaises(evidence.RepairEvidenceError):
                evidence.collect_repair_evidence(value)

    def test_shadow_allow_cannot_supply_diagnostics_provenance(self):
        result = self.scheduler.observe_policy_b_shadow("unit-1")
        self.assertEqual(result["decision"]["decision"], "ALLOW")
        with self.assertRaises(evidence.RepairEvidenceError):
            evidence.collect_repair_evidence(result)
        self.assertNotIn("repair_authorizations", self.store.state)

    def test_all_pass_is_not_failure_evidence(self):
        self.executor.set_behavior("unit-1", {"writes": {"calculator.py": "def multiply(a, b): return a * b\n"}})
        def probe(controller):
            with self.assertRaises(evidence.RepairEvidenceError):
                evidence.collect_repair_evidence(controller)
        self.collect(probe)

    def test_foreign_and_conflicting_observations_refused(self):
        def probe(controller):
            original = copy.deepcopy(controller.observations)
            for mutate in (lambda o: o.clear(),
                           lambda o: o["check-calc"]["binding"]["wal"].update(invocation_complete=1),
                           lambda o: o["check-calc"]["native"].update(observed_exit_code=0)):
                controller.observations = copy.deepcopy(original)
                mutate(controller.observations)
                with self.assertRaises(evidence.RepairEvidenceError):
                    evidence.collect_repair_evidence(controller)
            controller.observations = original
        self.collect(probe)

    def test_repository_and_environment_drift_refused(self):
        def probe(controller):
            path = self.repo.root / "calculator.py"
            before = path.read_bytes()
            path.write_text("# changed candidate\n")
            with self.assertRaises(evidence.RepairEvidenceError):
                evidence.collect_repair_evidence(controller)
            path.write_bytes(before)
            with patch("repair_evidence.environment.compare", return_value={"decision": "UNSAFE"}), \
                    self.assertRaises(evidence.RepairEvidenceError):
                evidence.collect_repair_evidence(controller)
        self.collect(probe)

    def test_failed_commit_latch_refuses_collection(self):
        def probe(controller):
            self.store._repair_write_failed = True
            try:
                with self.assertRaises(durable.DurableError):
                    evidence.collect_repair_evidence(controller)
            finally:
                self.store._repair_write_failed = False
        self.collect(probe)

    def test_torn_wal_refuses_collection_without_writing_or_truncating(self):
        def probe(controller):
            path = self.store.directory / "events.jsonl"
            before = path.read_bytes()
            with path.open("ab") as stream:
                stream.write(b'{"incomplete":')
            torn = path.read_bytes()
            with self.assertRaises(durable.DurableError):
                evidence.collect_repair_evidence(controller)
            self.assertEqual(path.read_bytes(), torn)
            # Restore only this deliberately injected disposable test tail.
            path.write_bytes(before)
        self.collect(probe)

    def test_expired_budget_refuses_collection(self):
        def probe(controller):
            with patch.object(self.scheduler, "remaining_task_budget", return_value=0), \
                    self.assertRaises(evidence.RepairEvidenceError):
                evidence.collect_repair_evidence(controller)
            with patch.object(self.scheduler, "remaining_task_budget", side_effect=[100, 0]), \
                    self.assertRaises(evidence.RepairEvidenceError):
                evidence.collect_repair_evidence(controller)
        self.collect(probe)

    def test_formatting_truncation_and_malformed_fields_are_not_authentication(self):
        packet = self.collect()
        for mutate in (lambda p: p.update(trusted=True), lambda p: p.update(schema_version=True),
                       lambda p: p.update(authorization_id="not-permitted"),
                       lambda p: p.update(included_failed_verifiers=99),
                       lambda p: p["failures"][0].update(exit_code=True),
                       lambda p: p["failures"][0].update(diagnostic="Authorization: Bearer abcdefghijklmnopqrstuvwxyz"),
                       lambda p: p["failures"][0]["native_wal"]["invocation_release"].update(sequence=1),
                       lambda p: p["failures"][0].update(diagnostic="x" * 8192)):
            bad = copy.deepcopy(packet)
            mutate(bad)
            with self.assertRaises(evidence.RepairEvidenceError):
                evidence.encode_repair_evidence(bad)
        # Synthetic rows test bounded encoding only; these are not native facts.
        row = packet["failures"][0]
        rows = []
        for index in range(20):
            item = copy.deepcopy(row)
            # Distinct child IDs as well as tokens (prefixes must differ).
            item["invocation_id"] = f"{index + 1:012x}" + "0" * 20
            item["child_id"] = "verification_command-" + item["invocation_id"][:12]
            rows.append(item)
        identity = {k: packet[k] for k in ("run_id", "unit_id", "source_attempt_id", "candidate_fingerprint",
                    "spec_sha256", "registry_sha256", "controller_session", "source_wal")}
        bounded = evidence._pack(identity, rows)
        self.assertTrue(bounded["truncated"])
        self.assertGreater(bounded["omitted_failed_verifiers"], 0)
        self.assertEqual(bounded["total_failed_verifiers"], 20)
        self.assertLessEqual(len(evidence.encode_repair_evidence(bounded)), 8192)


if __name__ == "__main__":
    unittest.main()
