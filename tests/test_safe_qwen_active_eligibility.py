"""Real Windows worker/verifier handles, deterministic fake Gateway, no AI."""
import copy
import os
import threading
import unittest
from unittest.mock import patch

import durable
import retry_eligibility as policy
import safe_qwen_active_recovery as active
import safe_qwen_active_verification as verification
import safe_qwen_active_eligibility as bridge
import test_safe_qwen_active_verification as fixtures
from test_supervised_safe_qwen import wire


@unittest.skipUnless(os.name == "nt", "Native Windows worker/verifier required")
class ActiveEligibilityTests(unittest.TestCase):
    setUp = fixtures.ActiveVerificationTests.setUp
    stop_fixture = fixtures.ActiveVerificationTests.stop_fixture
    servers = fixtures.ActiveVerificationTests.servers
    close_supervisor = fixtures.ActiveVerificationTests.close_supervisor
    reload = fixtures.ActiveVerificationTests.reload
    prepare_timeout_fixture = fixtures.ActiveVerificationTests.prepare_timeout_fixture

    def diagnose(self, **kwargs):
        return self.scheduler.diagnose_safe_qwen_recovery_first("safe-qwen-task", gateway=self.gateway, **kwargs)

    def fail_first(self):
        self.responses = [wire(self.submit)]

    def assert_no_authority(self):
        state = self.reload().state
        self.assertEqual(len(state["work_units"]["units"]["safe-qwen-task"]["attempts"]), 1)
        for key in ("repair_recovery", "repair_authorizations", "repair_dispatch", "terminal_acceptance"):
            self.assertNotIn(key, state)
        self.assertIsNone(state["last_verified_checkpoint"])
        self.assertIsNone(self.store._safe_qwen_active_controller)

    def test_genuine_failure_allows_diagnostics_without_authority_or_wal_change(self):
        self.fail_first()
        classify = policy.classify_retry_eligibility
        calls = []
        def observed(unit, **kwargs):
            calls.append(copy.deepcopy(unit))
            self.assertEqual(self.reload().state[active.KEY]["verification"]["phase"], "COMPLETE")
            return classify(unit, **kwargs)
        with patch.object(policy, "classify_retry_eligibility", side_effect=observed):
            result = self.diagnose()
        diagnostic = result["diagnostic"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(diagnostic["decision"]["decision"], "ALLOW", diagnostic)
        self.assertEqual(diagnostic["decision"]["reason_code"], policy.ALLOW_VERIFIER_FAILURE)
        self.assertTrue(diagnostic["diagnostic_only"])
        self.assertFalse(diagnostic["persisted"])
        data = result["active_recovery"]
        self.assertEqual(calls[0]["attempts"][0]["pre_attempt_snapshot"], data["snapshot_before"])
        for event, ref in diagnostic["sources"].items():
            record = self.store.records[ref["sequence"] - 1]
            self.assertEqual(record["sha256"], ref["sha256"])
            self.assertEqual(record[active.MARKER]["event"], event)
        self.assertEqual([r[active.MARKER]["event"] for r in self.store.records if active.MARKER in r][-1], "VERIFY_COMPLETE")
        self.assert_no_authority()

    def test_passing_verification_denies_retry(self):
        result = self.diagnose()
        self.assertEqual(result["active_recovery"]["verification"]["outcome"], "passed")
        self.assertEqual(result["diagnostic"]["decision"]["decision"], "DENY")
        self.assert_no_authority()

    def test_native_timeout_drains_verifier_and_descendant(self):
        result = self.diagnose(timeout_seconds=60)
        self.assertEqual(result["active_recovery"]["verification"]["outcome"], "timeout")
        self.assertEqual(result["diagnostic"]["decision"]["reason_code"], policy.DENY_TIMEOUT)
        import json
        import winprocess
        self.assertIsNone(winprocess.identity(json.loads(self.marker.read_text())["pid"]))
        self.assert_no_authority()

    def test_completed_v3_cannot_be_adopted_or_reclassified(self):
        self.fail_first()
        captured = []
        original = bridge.run
        def once(owner):
            result = original(owner); captured.append(owner)
            with self.assertRaises(durable.DurableError): original(owner)
            return result
        with patch.object(bridge, "run", side_effect=once): self.diagnose()
        with self.store.mutex:  # Exclude legitimate periodic heartbeat appends.
            before = (self.store.directory / "events.jsonl").read_bytes()
            with self.assertRaises(durable.DurableError): original(captured[0])
            with self.assertRaises(durable.DurableError): self.diagnose()
            self.assertEqual((self.store.directory / "events.jsonl").read_bytes(), before)
        self.assert_no_authority()

    def test_existing_v3_has_no_diagnostic_selection(self):
        original = verification.run
        def verified(owner):
            original(owner)
            with self.assertRaises(durable.DurableError): bridge.run(owner)
        with patch.object(verification, "run", side_effect=verified):
            result = self.scheduler.verify_safe_qwen_recovery_first("safe-qwen-task", gateway=self.gateway)
        self.assertNotIn("diagnostic", result)
        self.assert_no_authority()

    def test_caller_dictionary_is_not_controller_authority(self):
        with self.assertRaises(durable.DurableError): bridge.run({"schema_version": 3})
        self.assertNotIn(active.KEY, self.store.state)

    def test_dormant_identity_is_not_upgraded_to_diagnostics(self):
        self.store.select_safe_qwen_recovery_identity(self.scheduler, "safe-qwen-task")
        with patch.object(policy, "classify_retry_eligibility", side_effect=AssertionError("Dormant identity")) as classify:
            with self.assertRaises(durable.DurableError): self.diagnose()
            classify.assert_not_called()
        self.assertNotIn(active.KEY, self.reload().state)
        self.assertEqual(self.store.state["work_units"]["units"]["safe-qwen-task"]["attempts"], [])

    def refuse_before_classifier(self, change):
        self.fail_first()
        run = bridge.run
        def injected(owner):
            change(owner)
            return run(owner)
        with patch.object(bridge, "run", side_effect=injected), \
                patch.object(policy, "classify_retry_eligibility", side_effect=AssertionError("Untrusted classification")) as classify:
            with self.assertRaises(durable.DurableError): self.diagnose()
            classify.assert_not_called()
        self.assert_no_authority()

    def test_candidate_drift_refuses_classification(self):
        self.refuse_before_classifier(lambda owner: (self.repo.root / "calculator.py").write_text("foreign candidate\n"))

    def test_scope_drift_refuses_classification(self):
        def change(owner):
            scope = copy.deepcopy(self.scheduler.parent_scope)
            scope["allowed_paths"].append("foreign.py")
            self.scheduler.parent_scope = scope
        self.refuse_before_classifier(change)

    def test_foreign_controller_refuses_classification(self):
        self.refuse_before_classifier(lambda owner: setattr(self.store, "_safe_qwen_active_controller", None))

    def test_wrong_thread_refuses_classification(self):
        self.refuse_before_classifier(lambda owner: setattr(owner, "thread_id", threading.get_ident() + 1))

    def test_foreign_session_refuses_classification(self):
        self.refuse_before_classifier(lambda owner: owner.identity["session"].update(manager_session="foreign"))

    def test_forged_native_wal_reference_refuses_classification(self):
        def change(owner):
            owner.data["verification"]["observations"]["check-1"]["binding"]["wal"]["invocation_release"]["sha256"] = "0" * 64
        self.refuse_before_classifier(change)

    def test_missing_native_wal_reference_refuses_classification(self):
        def change(owner):
            del owner.data["verification"]["observations"]["check-1"]["binding"]["wal"]["invocation_release"]
        self.refuse_before_classifier(change)

    def test_index_drift_refuses_classification(self):
        def change(owner):
            import subprocess
            subprocess.run(["git", "-C", str(self.repo.root), "update-index", "--assume-unchanged", "calculator.py"],
                           check=True, capture_output=True)
        self.refuse_before_classifier(change)

    def test_environment_drift_refuses_classification(self):
        fingerprint = durable.current_fingerprint
        changed = []
        original = bridge.run
        def drift(*args, **kwargs):
            result = fingerprint(*args, **kwargs)
            if changed: result["runtime"]["python"]["sha256"] = "0" * 64
            return result
        def injected(owner):
            changed.append(True)
            return original(owner)
        self.fail_first()
        with patch.object(bridge, "run", side_effect=injected), patch("durable.current_fingerprint", side_effect=drift), \
                patch.object(policy, "classify_retry_eligibility", side_effect=AssertionError("Drifted environment")) as classify:
            with self.assertRaises(durable.DurableError): self.diagnose()
            classify.assert_not_called()
        self.assert_no_authority()

    def test_exhausted_shared_budget_returns_frozen_deny(self):
        original = bridge.run
        self.fail_first()
        def exhausted(owner):
            with patch.object(self.scheduler, "remaining_task_budget", return_value=0): return original(owner)
        with patch.object(bridge, "run", side_effect=exhausted): result = self.diagnose()
        self.assertEqual(result["diagnostic"]["decision"]["reason_code"], policy.DENY_INSUFFICIENT_BUDGET)
        self.assert_no_authority()

    def test_wrong_config_refuses_classification(self):
        def change(owner):
            config = copy.deepcopy(self.gateway.config)
            config["roles"]["code"] = "foreign-model"
            self.gateway.config = config
        self.refuse_before_classifier(change)

    def test_uncertain_completion_never_classifies(self):
        append = self.store.append
        persisted = []
        self.fail_first()
        def uncertain(event, **value):
            result = append(event, **value)
            if (value.get(active.MARKER) or {}).get("event") == "VERIFY_COMPLETE":
                persisted.append((self.store.directory / "events.jsonl").read_bytes())
                raise OSError("Uncertain verification commit")
            return result
        with patch.object(self.store, "append", side_effect=uncertain), \
                patch.object(policy, "classify_retry_eligibility", side_effect=AssertionError("Ambiguous history")) as classify:
            with self.assertRaises(OSError): self.diagnose()
            classify.assert_not_called()
        self.assertTrue(self.store._repair_write_failed)
        self.assertEqual((self.store.directory / "events.jsonl").read_bytes(), persisted[-1])
        self.assert_no_authority()

    def test_swallowed_classifier_wal_error_never_returns_diagnostic(self):
        self.fail_first()
        classify = policy.classify_retry_eligibility
        original = bridge.run
        persisted = []
        def injected(owner):
            def reconcile():
                # Reproduce a Supervisor append which reaches disk but raises
                # before state publication. The frozen classifier catches it.
                append = self.store.append
                def uncertain(event, **value):
                    result = append(event, **value)
                    persisted.append((self.store.directory / "events.jsonl").read_bytes())
                    raise OSError("Uncertain Supervisor write")
                with patch.object(self.store, "append", side_effect=uncertain): self.sup.heartbeat()
            def swallowed(unit, **kwargs):
                with patch.object(self.sup, "reconcile", side_effect=reconcile): return classify(unit, **kwargs)
            with patch.object(policy, "classify_retry_eligibility", side_effect=swallowed): return original(owner)
        with patch.object(bridge, "run", side_effect=injected), self.assertRaises(durable.DurableError): self.diagnose()
        self.assertTrue(self.store._repair_write_failed)
        self.assertEqual((self.store.directory / "events.jsonl").read_bytes(), persisted[-1])
        self.assert_no_authority()

    def test_drift_during_classifier_discards_allow(self):
        self.fail_first()
        classify = policy.classify_retry_eligibility
        def changed(unit, **kwargs):
            result = classify(unit, **kwargs)
            self.assertTrue(result.is_allowed())
            (self.repo.root / "calculator.py").write_text("# drift after classifier\n")
            return result
        with patch.object(policy, "classify_retry_eligibility", side_effect=changed), self.assertRaises(durable.DurableError): self.diagnose()
        self.assert_no_authority()

    def test_crash_before_diagnostic_and_torn_tail_cannot_resume(self):
        self.fail_first()
        with patch.object(bridge, "run", side_effect=RuntimeError("Controller stopped before diagnostics")):
            with self.assertRaises(RuntimeError): self.diagnose()
        self.assert_no_authority()
        with self.store.mutex:
            before = (self.store.directory / "events.jsonl").read_bytes()
            with self.assertRaises(durable.DurableError): self.diagnose()
            self.assertEqual((self.store.directory / "events.jsonl").read_bytes(), before)
        self.sup.__exit__(None, None, None)
        with (self.store.directory / "events.jsonl").open("ab") as stream: stream.write(b'{"sequence":')
        with self.assertRaises(durable.DurableError): self.reload()

    def test_actual_edit_failure_supplies_all_four_canonical_snapshots(self):
        import json
        import winprocess
        edit = json.loads(self.edit)
        edit["arguments"]["new_text"] = "return a + b"
        self.responses = [wire(json.dumps(edit)), wire(self.submit)]
        launch = winprocess.ControllerVerifier.launch
        launched = []
        def created(native, *args):
            result = launch(native, *args); launched.append(native.pid); return result
        with patch.object(winprocess.ControllerVerifier, "launch", created): result = self.diagnose()
        self.assertEqual(result["diagnostic"]["decision"]["decision"], "ALLOW", result["diagnostic"])
        d = result["active_recovery"]
        self.assertNotEqual(d["snapshot_before"], d["result"]["candidate"])
        self.assertEqual(d["result"]["changed_paths"], ["calculator.py"])
        self.assertEqual(d["verification"]["before"], d["result"]["candidate"])
        self.assertEqual(d["verification"]["before"], d["verification"]["after"])
        self.assertEqual(launched, [d["process"]["pid"], d["verification"]["observations"]["check-1"]["native"]["verifier"]["pid"]])
        self.assertTrue(all(winprocess.identity(pid) is None for pid in launched))
        self.assert_no_authority()
