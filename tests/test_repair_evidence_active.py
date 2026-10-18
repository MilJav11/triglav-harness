"""Synthetic v2 schema fixtures only; never authenticated runtime evidence."""
import copy
import json
import unittest

import repair_evidence as evidence
import safe_qwen_active_dispatch as dispatch


class ActiveEvidenceSchemaTests(unittest.TestCase):
    def packet(self, *, rows=1, output=None):
        identity = dict(run_id="synthetic", unit_id="unit", source_attempt_id="unit-attempt-1",
            candidate_fingerprint="a" * 64, spec_sha256="b" * 64, registry_sha256="c" * 64,
            controller_session=dict(manager_session=0, recovery_sha256="d" * 64, controller_sha256="e" * 64),
            source_wal=dict(sequence=6, sha256="f" * 64))
        failures = []
        for index in range(rows):
            token = f"{index + 1:032x}"
            # Distinct prefixes as well as tokens.
            token = f"{index + 1:012x}" + "0" * 20
            failures.append(dict(verifier_id="check", verifier_id_sha256="1" * 64,
                child_id="verification_command-" + token[:12], invocation_id=token, exit_code=1, termination="natural",
                diagnostic="Native verifier failed.", assertion_output=evidence._output(output),
                native_wal={event: dict(sequence=n, sha256=f"{n:064x}") for n, event in enumerate(evidence.EVENTS, 1)}))
        return evidence._pack(identity, failures, version=2)

    def test_complete_bounded_json_redacts_and_truncates(self):
        packet = self.packet(rows=20, output=dict(stdout="Authorization: Bearer abcdefghijklmnopqrstuvwxyz\n",
                                                stderr="assertion detail " * 2000, truncated=False))
        raw = evidence.encode_repair_evidence(packet)
        self.assertLessEqual(len(raw), 8192)
        self.assertEqual(json.loads(raw), packet)
        self.assertNotIn(b"abcdefghijklmnopqrstuvwxyz", raw)
        self.assertTrue(packet["truncated"])
        self.assertGreater(packet["omitted_failed_verifiers"], 0)
        self.assertTrue(packet["failures"][0]["assertion_output"]["stderr"]["truncated"])

    def test_missing_assertion_streams_are_explicitly_unavailable(self):
        packet = self.packet()
        self.assertFalse(packet["assertion_output_available"])
        self.assertEqual(packet["failures"][0]["assertion_output"], {
            k: dict(available=False, text=None, truncated=False) for k in ("stdout", "stderr")})

    def test_malformed_oversized_packets_never_supply_authority(self):
        packet = self.packet()
        mutations = [lambda p: p.update(trusted=True), lambda p: p.update(authority="grant"),
                     lambda p: p.update(failures=None), lambda p: p.update(failures={}),
                     lambda p: p["failures"][0]["assertion_output"]["stderr"].update(available=True, text="x" * 8193),
                     lambda p: p.update(extra=True), lambda p: p.update(schema_version=True)]
        for mutate in mutations:
            bad = copy.deepcopy(packet); mutate(bad)
            with self.subTest(mutate=mutate), self.assertRaises(evidence.RepairEvidenceError): evidence.encode_repair_evidence(bad)
        with self.assertRaises(evidence.RepairEvidenceError): evidence.collect_ai_repair_evidence(packet)
        import durable
        with self.assertRaises(durable.DurableError): dispatch.run(packet)
