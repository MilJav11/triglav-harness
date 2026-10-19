"""Trusted ACTIVE v4 issuance: real Windows processes, fake Gateway, no AI."""
import copy
import os
import unittest
from unittest.mock import patch

import durable
import retry_eligibility as policy
import safe_qwen_active_recovery as active
import safe_qwen_active_eligibility as eligibility
import safe_qwen_active_grant as grants
import test_safe_qwen_active_eligibility as fixtures
from test_supervised_safe_qwen import wire

REAL_CONSUME = durable.Store._consume_repair_grant


@unittest.skipUnless(os.name == "nt", "Real Windows native evidence required")
class ActiveGrantTests(unittest.TestCase):
    WAL_BUDGET = 120
    WAL_SCENARIOS = frozenset({
        "test_failure_before_decision", "test_partial_initial_decision_refuses_reload",
        "test_uncertain_decision_refuses_reload", "test_decision_without_grant_refuses_reload",
        "test_uncertain_grant_loads_history_only", "test_torn_grant_refuses_reload",
    })

    def setUp(self):
        self._grant_wal_budget = (self.WAL_BUDGET if type(self) is ActiveGrantTests
            and self._testMethodName in self.WAL_SCENARIOS else None)
        if self._testMethodName != "test_heartbeat_progress_during_owned_model_request" and self._grant_wal_budget is None:
            return fixtures.ActiveEligibilityTests.setUp(self)
        import json
        import subprocess
        import test_safe_qwen_integration as integration
        self._heartbeat_budget = self._grant_wal_budget or 120
        prepared = integration.IntegrationTests.prepared
        def bounded(fixture, *args, **kwargs):
            # Freeze the native/verification/grant scenario before either ledger.
            fixture.cfg["timeout_seconds"] = self._heartbeat_budget
            fixture.data["unit_timeout_seconds"] = self._heartbeat_budget
            fixture.path.write_text(json.dumps(fixture.cfg))
            fixture.policy_path.write_text(json.dumps(fixture.data))
            return prepared(fixture, *args, **kwargs)
        with patch.object(integration.IntegrationTests, "prepared", bounded):
            fixtures.ActiveEligibilityTests.setUp(self)
        popen = subprocess.Popen
        def inert_server(argv, *args, **kwargs):
            if type(argv) is list and argv[-1] == "import time;time.sleep(60)":
                argv = [*argv[:-1], f"import time;time.sleep({self._heartbeat_budget + 60})"]
            return popen(argv, *args, **kwargs)
        item = patch("subprocess.Popen", side_effect=inert_server)
        item.start(); self.addCleanup(item.stop)
        if self._grant_wal_budget is not None:
            self.assertEqual(self.cfg["timeout_seconds"], self._grant_wal_budget)
            self.assertEqual(self.worker_spec["limits"]["timeout_seconds"], self._grant_wal_budget)
    prepare_timeout_fixture = fixtures.ActiveEligibilityTests.prepare_timeout_fixture
    stop_fixture = fixtures.ActiveEligibilityTests.stop_fixture
    servers = fixtures.ActiveEligibilityTests.servers
    close_supervisor = fixtures.ActiveEligibilityTests.close_supervisor
    reload = fixtures.ActiveEligibilityTests.reload

    def issue(self, **kwargs):
        if self._grant_wal_budget is not None:
            kwargs.setdefault("timeout_seconds", self._grant_wal_budget)
        return self.scheduler.authorize_safe_qwen_repair_recovery("safe-qwen-task", gateway=self.gateway, **kwargs)

    def failure(self):
        self.responses = [wire(self.submit)]

    def assert_single(self):
        state = self.reload().state
        self.assertEqual(len(state["work_units"]["units"]["safe-qwen-task"]["attempts"]), 1)
        self.assertEqual(state["work_units"]["units"]["safe-qwen-task"]["status"], "VERIFYING")
        self.assertIsNone(state["last_verified_checkpoint"])
        self.assertEqual(state["verified_progress"], [])
        self.assertNotIn("repair_dispatch", state)
        self.assertIsNone(self.store._safe_qwen_active_controller)
        self.assertIsNone(self.store._repair_controller)

    def test_real_failure_issues_one_existing_granted_v1_pair(self):
        self.failure()
        import winprocess
        launch = winprocess.ControllerVerifier.launch
        launched = []
        def created(native, *args):
            result = launch(native, *args); launched.append(native.pid); return result
        classifier = policy.classify_retry_eligibility
        calls = []
        def fresh(unit, **kwargs):
            self.assertEqual(self.reload().state[active.KEY]["verification"]["phase"], "COMPLETE")
            calls.append(unit)
            return classifier(unit, **kwargs)
        with patch.object(policy, "classify_retry_eligibility", side_effect=fresh), \
                patch.object(winprocess.ControllerVerifier, "launch", created): result = self.issue()
        grant = result["authorization"]
        self.assertEqual(len(calls), 1)
        self.assertEqual((grant["schema_version"], grant["state"]), (1, "GRANTED"))
        self.assertEqual(result["active_recovery"]["schema_version"], 4)
        loaded = self.reload()
        self.assertEqual(loaded.state["repair_authorizations"], {grant["authorization_id"]: grant})
        source = loaded.records[grant["source_wal"]["sequence"] - 1]
        decision = loaded.records[grant["policy_wal"]["sequence"] - 1]
        commit = next(r for r in loaded.records if "repair_authorization" in r)
        self.assertEqual(source["sha256"], grant["source_wal"]["sha256"])
        self.assertEqual(decision["sha256"], grant["policy_wal"]["sha256"])
        self.assertEqual(source["sequence"] + 1, decision["sequence"])
        self.assertEqual(decision["sequence"] + 1, commit["sequence"])
        self.assertEqual(decision["event"], "repair_policy_b_decision")
        self.assertEqual(commit["repair_authorization"]["event"], "repair_authorization_granted")
        self.assertEqual(source["state"]["work_units"], commit["state"]["work_units"])
        self.assertEqual(source["state"][active.KEY], commit["state"][active.KEY])
        self.assertEqual(grant["authorization_id"], "repair-" + result["active_recovery"]["identity"]["id"])
        row = grant["native_evidence"]["check-1"]
        native = loaded.read_evidence(row["evidence"])
        self.assertEqual(native, result["active_recovery"]["verification"]["observations"]["check-1"]["native"])
        self.assertEqual(launched, [result["active_recovery"]["process"]["pid"], native["verifier"]["pid"]])
        self.assertTrue(all(winprocess.identity(pid) is None for pid in launched))
        self.assertEqual(result["active_recovery"]["identity"]["worker"]["limits"]["max_attempts"], 1)
        self.assert_single()

    def test_passing_verifier_has_no_grant(self):
        result = self.issue()
        self.assertIsNone(result["authorization"])
        self.assertNotIn("repair_authorizations", self.reload().state)
        self.assert_single()

    def test_exhausted_budget_deny_has_no_grant(self):
        self.failure()
        issue = durable.Store._issue_ai_repair_grant
        def exhausted(store, owner):
            with patch.object(self.scheduler, "remaining_task_budget", return_value=0): return issue(store, owner)
        with patch.object(durable.Store, "_issue_ai_repair_grant", exhausted): result = self.issue()
        self.assertIsNone(result["authorization"])
        self.assertNotIn("repair_authorizations", self.reload().state)
        self.assert_single()

    def test_old_diagnostic_allow_cannot_be_adopted(self):
        self.failure()
        result = self.scheduler.diagnose_safe_qwen_recovery_first("safe-qwen-task", gateway=self.gateway)
        self.assertEqual(result["diagnostic"]["decision"]["decision"], "ALLOW")
        with self.store.mutex:
            before = (self.store.directory / "events.jsonl").read_bytes()
            with self.assertRaises(durable.DurableError): self.issue()
            with self.assertRaises(durable.DurableError): self.store._issue_ai_repair_grant(result["diagnostic"])
            self.assertEqual((self.store.directory / "events.jsonl").read_bytes(), before)
        self.assertNotIn("repair_authorizations", self.reload().state)

    def test_reload_cannot_issue_consume_or_dispatch(self):
        self.failure(); result = self.issue()
        with self.store.mutex:
            before = (self.store.directory / "events.jsonl").read_bytes()
            with self.assertRaises(durable.DurableError): self.issue()
            with self.assertRaises(durable.DurableError): REAL_CONSUME(self.store, result["authorization"])
            loaded = self.reload()
            self.assertIsNone(loaded._safe_qwen_active_controller)
            with self.assertRaises(durable.DurableError): loaded._issue_ai_repair_grant(result["authorization"])
            import manager
            from supervision import Supervisor
            from work_unit_scheduler import WorkUnitScheduler
            scheduler = WorkUnitScheduler(loaded, self.repo, supervisor=Supervisor(loaded))
            self.assertEqual(scheduler.reconciliation_stop.reason, "SAFEQWEN_ACTIVE_REVIEW_REQUIRED")
            with self.assertRaises(manager.Stop): scheduler._refuse_invocation_replay()
            with self.assertRaises(durable.DurableError): loaded._begin_repair_recovery(scheduler, "safe-qwen-task")
            self.assertEqual((self.store.directory / "events.jsonl").read_bytes(), before)
        self.assert_single()

    def refuse_changed(self, change):
        self.failure()
        issue = durable.Store._issue_ai_repair_grant
        def changed(store, owner):
            change(owner)
            return issue(store, owner)
        with patch.object(durable.Store, "_issue_ai_repair_grant", changed), \
                patch.object(policy, "classify_retry_eligibility", side_effect=AssertionError("Invalid provenance")) as classifier:
            with self.assertRaises((durable.DurableError, ValueError)): self.issue()
            classifier.assert_not_called()
        self.assertNotIn("repair_authorizations", self.reload().state)
        self.assert_single()

    def test_forged_native_wal_reference_refuses(self):
        def change(owner):
            owner.data["verification"]["observations"]["check-1"]["binding"]["wal"]["invocation_bound"]["sha256"] = "0" * 64
        self.refuse_changed(change)

    def test_candidate_drift_refuses(self):
        self.refuse_changed(lambda owner: (self.repo.root / "calculator.py").write_text("# foreign candidate\n"))

    def test_foreign_session_refuses(self):
        self.refuse_changed(lambda owner: owner.identity["session"].update(manager_session=999))

    def test_config_drift_refuses(self):
        def change(owner):
            value = copy.deepcopy(self.gateway.config)
            value["roles"]["code"] = "foreign"
            self.gateway.config = value
        self.refuse_changed(change)

    def test_drift_during_classification_discards_allow(self):
        self.failure()
        classify = policy.classify_retry_eligibility
        def drift(unit, **kwargs):
            result = classify(unit, **kwargs)
            self.assertTrue(result.is_allowed())
            (self.repo.root / "calculator.py").write_text("# drift after classification\n")
            return result
        with patch.object(policy, "classify_retry_eligibility", side_effect=drift), self.assertRaises(durable.DurableError): self.issue()
        self.assertNotIn("repair_authorizations", self.reload().state)
        self.assert_single()

    def test_duplicate_and_foreign_thread_issuance_refuse(self):
        import threading
        self.failure()
        issue = durable.Store._issue_ai_repair_grant
        failures = []
        owners = []
        def foreign_issuance():
            store = self.store
            owner = store._safe_qwen_active_controller
            owners.append(owner)
            self.assertIsNotNone(owner)
            self.assertFalse(owner.closed)
            self.assertFalse(owner.grant_started)
            grants.require_owner(store, owner)
            def foreign():
                try: issue(store, owner)
                except durable.DurableError: failures.append(True)
            # The model-request callback runs outside the grant transaction.
            # Joining inside operation would hold the mutex needed by issue.
            thread = threading.Thread(target=foreign)
            thread.start(); thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertNotEqual(thread.ident, owner.thread_id)
            self.assertEqual(failures, [True])
            self.assertFalse(owner.closed)
            self.assertFalse(owner.grant_started)
            self.assertNotIn("repair_authorizations", store.state)
        self.on_request = foreign_issuance
        def operation(store, owner):
            self.assertEqual(owners, [owner])
            result = issue(store, owner)
            with self.assertRaises(durable.DurableError): issue(store, owner)
            with self.assertRaises(durable.DurableError): REAL_CONSUME(store, owner)
            return result
        with patch.object(durable.Store, "_issue_ai_repair_grant", operation): self.issue()
        self.assertEqual(failures, [True])
        self.assertEqual(sum("repair_authorization" in r for r in self.store.records), 1)
        self.assert_single()

    def test_concurrent_continuous_selection_refuses_second_owner(self):
        import threading
        self.failure()
        results = []
        def concurrent():
            def other():
                try: self.issue()
                except durable.DurableError: results.append("refused")
            thread = threading.Thread(target=other)
            thread.start(); thread.join(5)
            self.assertFalse(thread.is_alive())
        self.on_request = concurrent
        self.issue()
        self.assertEqual(results, ["refused"])
        self.assert_single()

    def fail_write(self, boundary, *, timing="after", torn=False):
        import json
        import time
        from test_safe_qwen_active_allocation import _allocation_diagnostic
        self.assertIn(boundary, ("decision", "grant"))
        self.assertIn(timing, ("before", "after"))
        self.assertIs(type(torn), bool)
        self.failure()
        append = self.store.append
        captured = []
        reached = []
        observed = {"policy_calls": 0}
        classify = eligibility.classify_for_grant
        expected_event = "repair_policy_b_decision" if boundary == "decision" else "state_committed"
        reload_refused = torn or (boundary == "decision" and timing == "after") or (boundary == "grant" and timing == "before")
        writes_refused, history_refused = False, None
        reload_disposition = "unknown"
        def classified(owner):
            observed["policy_calls"] += 1
            decision = classify(owner)
            observed.update(decision=decision.decision, reason_code=decision.reason_code, policy_clock=time.monotonic())
            return decision
        def capture(stage):
            captured.append((self.store.directory / "events.jsonl").read_bytes())
            reached.append((expected_event, stage))
            observed["fault_clock"] = time.monotonic()
        def faulty(event, **value):
            hit = (event == "repair_policy_b_decision" if boundary == "decision" else "repair_authorization" in value)
            if hit:
                self.assertEqual(event, expected_event)
                if boundary == "grant":
                    self.assertEqual(value["repair_authorization"]["event"], "repair_authorization_granted")
            if hit and timing == "before":
                if torn:
                    with (self.store.directory / "events.jsonl").open("ab") as stream:
                        stream.write(b'{"event":')
                capture("before")
                raise OSError("Fault before " + boundary)
            result = append(event, **value)
            if hit:
                capture("after")
                raise OSError("Fault after " + boundary)
            return result
        try:
            with patch.object(eligibility, "classify_for_grant", side_effect=classified), \
                    patch.object(self.store, "append", side_effect=faulty), self.assertRaises(OSError): self.issue()
            # An early DENY or unrelated OSError must not count as WAL coverage.
            self.assertEqual(len(captured), 1)
            self.assertEqual(reached, [(expected_event, timing)])
            self.assertEqual(observed["policy_calls"], 1)
            self.assertEqual(observed.get("decision"), "ALLOW")
            self.assertEqual(observed.get("reason_code"), policy.ALLOW_VERIFIER_FAILURE)
            self.assertTrue(self.store._repair_write_failed)
            self.assertIsNone(self.store._safe_qwen_active_controller)
            self.assertEqual((self.store.directory / "events.jsonl").read_bytes(), captured[-1])
            with self.assertRaises(durable.DurableError): self.store.append("after_uncertain_grant")
            with self.assertRaises(durable.DurableError): self.sup.heartbeat()
            self.assertTrue((self.store.directory / "events.jsonl").read_bytes() == captured[-1], "Refused writes changed WAL")
            writes_refused = True
            data = self.store.state[active.KEY]
            self.assertTrue(data["result"]["cleanup_proof"]["empty_job"])
            if self.sup.failure is not None:
                self.assertIsInstance(self.sup.failure, durable.DurableError)
                self.assertEqual(str(self.sup.failure), "Uncertain repair history write; reload required")
                for err in self.sup.failures:
                    self.assertIsInstance(err, durable.DurableError)
                    self.assertEqual(str(err), "Uncertain repair history write; reload required")
            if reload_refused:
                with self.assertRaises(durable.DurableError): self.reload()
                reload_disposition = "refused"
            else:
                loaded = self.reload()
                self.assertIsNone(loaded._safe_qwen_active_controller)
                self.assertEqual(len(loaded.state["work_units"]["units"]["safe-qwen-task"]["attempts"]), 1)
                self.assertEqual("repair_authorizations" in loaded.state, boundary == "grant")
                payload = next(iter(loaded.state.get("repair_authorizations", {}).values()), {})
                with self.assertRaises(durable.DurableError): loaded._issue_ai_repair_grant(payload)
                with self.assertRaises(durable.DurableError): REAL_CONSUME(loaded, payload)
                self.assertIsNone(loaded.state["last_verified_checkpoint"])
                self.assertNotIn("repair_dispatch", loaded.state)
                history_refused = True
                reload_disposition = "history_only" if boundary == "grant" else "pre_grant_history"
            self.assertTrue((self.store.directory / "events.jsonl").read_bytes() == captured[-1], "Reload or denied authority changed WAL")
        finally:
            # Reuse the reviewed closed serializer; no raw WAL/exception payload.
            diagnostic = json.loads(_allocation_diagnostic(self.store.state, observed, time.monotonic()))
            at_fault = json.loads(_allocation_diagnostic(self.store.state, observed, observed.get("fault_clock")))
            diagnostic.update(wal_boundary=boundary, wal_event=reached[-1][0] if reached else "unknown",
                wal_stage=reached[-1][1] if reached else "unknown", wal_torn=torn,
                wal_injection_count=len(captured), subsequent_writes_refused=writes_refused,
                reload_disposition=reload_disposition, history_authority_refused=history_refused,
                injection_clock=at_fault["observed_clock"], remaining_at_injection_seconds=at_fault["remaining_at_observation_seconds"])
            encoded = json.dumps(diagnostic, sort_keys=True, allow_nan=False)
            self.assertLessEqual(len(("GRANT_WAL_DIAGNOSTIC " + encoded).encode()), 4096)
            print("GRANT_WAL_DIAGNOSTIC " + encoded, flush=True)

    def test_failure_before_decision(self): self.fail_write("decision", timing="before")
    def test_partial_initial_decision_refuses_reload(self): self.fail_write("decision", timing="before", torn=True)
    def test_uncertain_decision_refuses_reload(self): self.fail_write("decision")
    def test_decision_without_grant_refuses_reload(self): self.fail_write("grant", timing="before")
    def test_uncertain_grant_loads_history_only(self): self.fail_write("grant")

    def test_native_timeout_drains_verifier_and_descendant(self):
        result = self.issue(timeout_seconds=60)
        self.assertEqual(result["active_recovery"]["verification"]["outcome"], "timeout")
        self.assertIsNone(result["authorization"])
        self.assertNotIn("repair_authorizations", self.reload().state)
        self.assert_single()

    def test_grant_cache_failure_loads_history_only(self):
        self.failure()
        write = durable.atomic_write
        def fail(path, content):
            if path == self.store.directory / "state.json" and "repair_authorizations" in self.store.state:
                raise OSError("Grant cache projection failed")
            return write(path, content)
        with patch("durable.atomic_write", side_effect=fail), self.assertRaises(OSError): self.issue()
        self.assertTrue(self.store._repair_write_failed)
        self.assertEqual(next(iter(self.reload().state["repair_authorizations"].values()))["state"], "GRANTED")
        self.assert_single()

    def test_heartbeat_progress_during_owned_model_request(self):
        import test_safe_qwen_active_verification as native
        import time
        # Deferred import reuses the reviewed closed serializer after this
        # fixture module is fully initialized; no import-time fixture execution.
        from test_safe_qwen_active_allocation import _allocation_diagnostic
        observed = {"policy_calls": 0}
        classify = eligibility.classify_for_grant
        def observed_classification(owner):
            observed["policy_calls"] += 1
            observed.pop("decision", None); observed.pop("reason_code", None)
            try:
                decision = classify(owner)
            finally:
                observed["policy_clock"] = time.monotonic()
            observed.update(decision=decision.decision, reason_code=decision.reason_code)
            return decision
        # Reuse the reviewed real heartbeat test; only the selected operation differs.
        self.run_first = lambda **kwargs: self.issue(**dict(kwargs, timeout_seconds=self._heartbeat_budget))
        self.pulse_frequently = lambda: native.ActiveVerificationTests.pulse_frequently(self)
        try:
            with patch.object(eligibility, "classify_for_grant", observed_classification):
                native.ActiveVerificationTests.test_heartbeat_progress_during_startup_and_model_request(self)
            self.assertEqual(next(iter(self.reload().state["repair_authorizations"].values()))["state"], "GRANTED")
            self.assert_single()
        finally:
            print("F3J_DIAGNOSTIC " + _allocation_diagnostic(self.store.state, observed, time.monotonic()), flush=True)

    def test_direct_json_and_old_versions_cannot_issue(self):
        with self.assertRaises(durable.DurableError): self.store._issue_ai_repair_grant({"mode": "ACTIVE_ATTEMPT1_GRANT"})
        with self.assertRaises(durable.DurableError):
            self.store.append("repair_policy_b_decision", decision={"decision": "ALLOW"})
        self.store.select_safe_qwen_recovery_identity(self.scheduler, "safe-qwen-task")
        with self.assertRaises(durable.DurableError): self.issue()
        self.assertNotIn(active.KEY, self.reload().state)

    def test_torn_grant_refuses_reload(self):
        self.fail_write("grant", timing="before", torn=True)

    def test_closed_contract_and_consumed_shape_refuse(self):
        self.failure(); self.issue()
        state = self.reload().state
        for key, value in (("schema_version", True), ("decision_schema_version", 2), ("extra", "foreign")):
            changed = copy.deepcopy(state)
            changed[active.KEY]["grant_contract"][key] = value
            with self.subTest(field=key), self.assertRaises(durable.DurableError): active.validate(changed)
        changed = copy.deepcopy(state)
        changed[active.KEY]["grant_contract"]["sources"].pop("retry_eligibility.py")
        with self.assertRaises(durable.DurableError): active.validate(changed)
        changed = copy.deepcopy(state)
        next(iter(changed["repair_authorizations"].values())).update(schema_version=2, state="CONSUMED")
        with self.assertRaises(durable.DurableError): active.validate(changed)
        changed = copy.deepcopy(state)
        changed[active.KEY]["verification"]["dispatch_result_sha256"] = "0" * 64
        with self.assertRaises(durable.DurableError): active.validate(changed)
        self.assert_single()

    def test_v3_history_cannot_upgrade_into_grant_mode(self):
        self.failure()
        self.scheduler.verify_safe_qwen_recovery_first("safe-qwen-task", gateway=self.gateway)
        records = copy.deepcopy(self.store.records)
        last = next(r for r in reversed(records) if r.get(active.MARKER, {}).get("event") == "VERIFY_COMPLETE")
        d = last["state"][active.KEY]
        d.update(schema_version=4, mode="ACTIVE_ATTEMPT1_GRANT", grant_contract=grants.contract())
        with self.assertRaises(durable.DurableError): active.validate_history(records, self.store.read_evidence, reservation_exists=True)
        self.assertNotIn("repair_authorizations", self.reload().state)
