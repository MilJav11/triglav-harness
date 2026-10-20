"""Focused negative and positive trust semantics unit tests for criteria status projection."""
import json
import pathlib
import tempfile
import unittest

from durable import Store, digest
from manager import evaluate_completion, criteria_status, write_report


class TrustSemanticsCriteriaTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp_dir.name)
        self.run_id = "test-run-trust"
        self.store = Store(self.root, self.run_id)
        self.store.directory.mkdir(parents=True, exist_ok=True)

        self.snapshot = {
            "branch": "refs/heads/master",
            "head": "a" * 40,
            "fingerprint": "f" * 40,
            "files": {"calc.py": {"mode": 438, "sha256": "1" * 64}}
        }
        self.manager_state = {
            "checks": [{"id": "check-1", "argv": ["python", "-m", "unittest", "test_calc.py"]}],
            "criteria": [{"id": "AC1", "text": "Task completed", "checks": ["check-1"]}],
            "proven_checks": [],
            "rounds": [],
            "stop": None,
            "counters": {"rounds_started": 1, "trusted_rounds": 1, "consecutive_failed_rounds": 0},
            "budgets": {"max_rounds": 6, "max_runtime_seconds": 3600},
            "runtime_seconds": 10.0,
            "completion": None,
        }
        self.store.state = {
            "run_id": self.run_id,
            "status": "READY",
            "revision": 1,
            "round_number": 0,
            "verified_progress": [],
            "remaining_work": [],
            "manager": self.manager_state,
            "last_verified_checkpoint": None,
        }

    def tearDown(self):
        self.temp_dir.cleanup()

    def _write_artifact(self, rel_path, data):
        full_path = self.store.directory / rel_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(data, indent=2)
        full_path.write_text(raw, encoding="utf-8")
        return {"path": rel_path, "sha256": digest(data)}

    # TEST 1: ALL_CRITERIA_PROVEN with missing proof -> NOT_PROVEN
    def test_all_criteria_proven_with_missing_proof_remains_not_proven(self):
        self.store.state["status"] = "COMPLETED"
        self.store.state["manager"]["stop"] = {
            "status": "COMPLETED",
            "reason": "ALL_CRITERIA_PROVEN",
            "message": "Fraudulent claim without checkpoint"
        }
        self.store.state["last_verified_checkpoint"] = None

        report = write_report(self.store)
        self.assertEqual(report["criteria"][0]["status"], "NOT_PROVEN")
        self.assertEqual(report["criteria"][0]["missing_checks"], ["check-1"])

        status = criteria_status(self.store.state["manager"], self.store)
        self.assertEqual(status[0]["status"], "NOT_PROVEN")

    # TEST 2: Failed or missing final verification -> NOT_PROVEN
    def test_failed_final_verification_remains_not_proven(self):
        verif_data = {
            "verification_id": "verif-1",
            "passed": False,  # Final verification failed!
            "candidate_snapshot_digest": digest(self.snapshot),
            "verifiers": [{"argv": ["python", "-m", "unittest", "test_calc.py"], "exit_code": 1, "passed": False}]
        }
        verif_ref = self._write_artifact("final_verification/final-verif.json", verif_data)

        ckpt_data = {
            "checkpoint_id": "ckpt-1",
            "snapshot": self.snapshot,
            "verification": [verif_ref]
        }
        ckpt_ref = self._write_artifact("checkpoints/0000.json", ckpt_data)

        self.store.state["status"] = "COMPLETED"
        self.store.state["last_verified_checkpoint"] = {"reference": ckpt_ref, "snapshot": self.snapshot}
        self.store.state["manager"]["stop"] = {"status": "COMPLETED", "reason": "ALL_CRITERIA_PROVEN"}

        report = write_report(self.store)
        self.assertEqual(report["criteria"][0]["status"], "NOT_PROVEN")
        self.assertEqual(report["criteria"][0]["missing_checks"], ["check-1"])

    def test_missing_final_verification_artifact_remains_not_proven(self):
        ckpt_data = {
            "checkpoint_id": "ckpt-1",
            "snapshot": self.snapshot,
            "verification": [{"path": "final_verification/missing.json", "sha256": "0" * 64}]
        }
        ckpt_ref = self._write_artifact("checkpoints/0000.json", ckpt_data)

        self.store.state["status"] = "COMPLETED"
        self.store.state["last_verified_checkpoint"] = {"reference": ckpt_ref, "snapshot": self.snapshot}
        self.store.state["manager"]["stop"] = {"status": "COMPLETED", "reason": "ALL_CRITERIA_PROVEN"}

        report = write_report(self.store)
        self.assertEqual(report["criteria"][0]["status"], "NOT_PROVEN")

    # TEST 3: Incomplete or mismatched verified checks -> NOT_PROVEN
    def test_incomplete_checks_remains_not_proven(self):
        # Manager requires both check-1 and check-2
        self.store.state["manager"]["checks"] = [
            {"id": "check-1", "argv": ["python", "-m", "unittest", "test_a.py"]},
            {"id": "check-2", "argv": ["python", "-m", "unittest", "test_b.py"]},
        ]
        self.store.state["manager"]["criteria"] = [
            {"id": "AC1", "text": "Task", "checks": ["check-1", "check-2"]}
        ]

        # Verification evidence only ran check-1
        verif_data = {
            "verification_id": "verif-1",
            "passed": True,
            "candidate_snapshot_digest": digest(self.snapshot),
            "verifiers": [{"argv": ["python", "-m", "unittest", "test_a.py"], "exit_code": 0, "passed": True}]
        }
        verif_ref = self._write_artifact("final_verification/final-verif.json", verif_data)

        ckpt_data = {
            "checkpoint_id": "ckpt-1",
            "snapshot": self.snapshot,
            "verification": [verif_ref]
        }
        ckpt_ref = self._write_artifact("checkpoints/0000.json", ckpt_data)

        self.store.state["status"] = "COMPLETED"
        self.store.state["last_verified_checkpoint"] = {"reference": ckpt_ref, "snapshot": self.snapshot}
        self.store.state["manager"]["stop"] = {"status": "COMPLETED", "reason": "ALL_CRITERIA_PROVEN"}

        report = write_report(self.store)
        self.assertEqual(report["criteria"][0]["status"], "NOT_PROVEN")
        self.assertEqual(report["criteria"][0]["missing_checks"], ["check-2"])

    def test_mismatched_verifier_command_remains_not_proven(self):
        # Verification ran test_wrong.py instead of test_calc.py
        verif_data = {
            "verification_id": "verif-1",
            "passed": True,
            "candidate_snapshot_digest": digest(self.snapshot),
            "verifiers": [{"argv": ["python", "-m", "unittest", "test_wrong.py"], "exit_code": 0, "passed": True}]
        }
        verif_ref = self._write_artifact("final_verification/final-verif.json", verif_data)

        ckpt_data = {
            "checkpoint_id": "ckpt-1",
            "snapshot": self.snapshot,
            "verification": [verif_ref]
        }
        ckpt_ref = self._write_artifact("checkpoints/0000.json", ckpt_data)

        self.store.state["status"] = "COMPLETED"
        self.store.state["last_verified_checkpoint"] = {"reference": ckpt_ref, "snapshot": self.snapshot}
        self.store.state["manager"]["stop"] = {"status": "COMPLETED", "reason": "ALL_CRITERIA_PROVEN"}

        report = write_report(self.store)
        self.assertEqual(report["criteria"][0]["status"], "NOT_PROVEN")
        self.assertEqual(report["criteria"][0]["missing_checks"], ["check-1"])

    # TEST 4: Legitimate fully verified completion -> PASS
    def test_legitimate_fully_verified_completion_passes(self):
        verif_data = {
            "verification_id": "verif-1",
            "passed": True,
            "candidate_snapshot_digest": digest(self.snapshot),
            "verifiers": [
                {"argv": ["python", "-m", "unittest", "test_calc.py"], "exit_code": 0, "passed": True, "verifier_id": "check-1"}
            ]
        }
        verif_ref = self._write_artifact("final_verification/final-verif.json", verif_data)

        ckpt_data = {
            "checkpoint_id": "ckpt-1",
            "snapshot": self.snapshot,
            "verification": [verif_ref]
        }
        ckpt_ref = self._write_artifact("checkpoints/0000.json", ckpt_data)

        self.store.state["status"] = "COMPLETED"
        self.store.state["last_verified_checkpoint"] = {"reference": ckpt_ref, "snapshot": self.snapshot}
        self.store.state["manager"]["stop"] = {"status": "COMPLETED", "reason": "ALL_CRITERIA_PROVEN"}

        report = write_report(self.store)
        self.assertEqual(report["criteria"][0]["status"], "PASS")
        self.assertEqual(report["criteria"][0]["missing_checks"], [])
        self.assertEqual(report["criteria"][0]["evidence"], [verif_ref, ckpt_ref])
        self.assertEqual(report["status"], "COMPLETED")

    # TEST 5: Working tree drifted after checkpoint -> NOT_PROVEN
    def test_working_tree_drift_after_checkpoint_remains_not_proven(self):
        verif_data = {
            "verification_id": "verif-1",
            "passed": True,
            "candidate_snapshot_digest": digest(self.snapshot),
            "verifiers": [
                {"argv": ["python", "-m", "unittest", "test_calc.py"], "exit_code": 0, "passed": True, "verifier_id": "check-1"}
            ]
        }
        verif_ref = self._write_artifact("final_verification/final-verif.json", verif_data)

        ckpt_data = {
            "checkpoint_id": "ckpt-1",
            "snapshot": self.snapshot,
            "verification": [verif_ref]
        }
        ckpt_ref = self._write_artifact("checkpoints/0000.json", ckpt_data)

        drifted_snapshot = dict(self.snapshot, fingerprint="drifted" * 5)
        self.store.state["status"] = "COMPLETED"
        # Checkpoint references original snapshot, but working tree has drifted
        self.store.state["last_verified_checkpoint"] = {"reference": ckpt_ref, "snapshot": drifted_snapshot}
        self.store.state["manager"]["stop"] = {"status": "COMPLETED", "reason": "ALL_CRITERIA_PROVEN"}

        eval_res = evaluate_completion(self.store, current=drifted_snapshot)
        self.assertFalse(eval_res["all_proven"])
        self.assertEqual(eval_res["criteria"][0]["status"], "NOT_PROVEN")

    # TEST 6: Conclude does not manufacture PASS without evidence
    def test_conclude_does_not_manufacture_pass_without_evidence(self):
        self.store.state["status"] = "ACTIVE"
        self.store.state["last_verified_checkpoint"] = None
        self.store.state["manager"]["proven_checks"] = []
        # Ensure criteria_status and write_report remain NOT_PROVEN
        status = criteria_status(self.store.state["manager"], self.store)
        self.assertEqual(status[0]["status"], "NOT_PROVEN")
        self.assertEqual(self.store.state["manager"]["proven_checks"], [])

    # TEST 7: Local historical run evaluation (cleanly skipped in CI if artifacts absent)
    def test_historical_run_evaluates_pass_if_present(self):
        historical_run_dir = pathlib.Path(r"C:\AI\experiments\triglav-phase-b-three-model\handoff_staging\run")
        if not (historical_run_dir / "state.json").exists():
            self.skipTest("Local historical run artifacts not present on clean/CI host")
        store = Store(historical_run_dir.parent, historical_run_dir.name)
        store.state = json.loads((historical_run_dir / "state.json").read_text("utf-8"))
        res = evaluate_completion(store)
        self.assertTrue(res["all_proven"])
        self.assertEqual(res["criteria"][0]["status"], "PASS")
        self.assertEqual(len(res["criteria"][0]["evidence"]), 2)


if __name__ == "__main__":
    unittest.main()
