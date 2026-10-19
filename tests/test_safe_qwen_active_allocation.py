"""ACTIVE v5 consumes/allocates continuously; no Attempt 2 effects or live AI."""
import copy
import os
import threading
import unittest
from unittest.mock import patch

import durable
import safe_qwen_active_recovery as active
import safe_qwen_active_grant as grants
import test_safe_qwen_active_grant as fixtures


def _allocation_diagnostic(state, observed, clock):
    """Closed diagnostic schema; null numbers and 'unknown' enums mean unproven."""
    import json
    import math

    def mapping(value): return value if type(value) is dict else {}
    def choice(value, allowed): return value if type(value) is str and value in allowed else "unknown"
    def number(value):
        return value if type(value) in (int, float) and abs(value) <= 1e12 and math.isfinite(value) else None
    def boolean(value): return value if type(value) is bool else None
    def remaining(deadline, now):
        deadline, now = number(deadline), number(now)
        return number(deadline - now) if deadline is not None and now is not None else None
    state, observed = mapping(state), mapping(observed)
    data = mapping(state.get(active.KEY)); identity = mapping(data.get("identity"))
    result = mapping(data.get("result")); worker = mapping(result.get("evidence"))
    cleanup = mapping(result.get("cleanup_proof")); verification = mapping(data.get("verification"))
    native = mapping(mapping(mapping(verification.get("observations")).get("check-1")).get("native"))
    grants = mapping(state.get("repair_authorizations"))
    grant = mapping(next(iter(grants.values()))) if len(grants) == 1 else {}
    authorization = choice(grant.get("state"), {"GRANTED", "CONSUMED"})
    if "repair_authorizations" not in state: authorization = "absent"
    decision = choice(observed.get("decision"), {"ALLOW", "DENY"})
    calls = observed.get("policy_calls")
    disposition = ("consumed" if authorization == "CONSUMED" else "created" if authorization == "GRANTED"
                   else "rejected" if authorization == "absent" and decision == "DENY"
                   else "never_allocated" if authorization == "absent" and type(calls) is int and calls == 0
                   else "unknown")
    value = dict(schema_version=1, policy_calls=number(calls), policy_decision=decision,
        policy_reason_code=choice(observed.get("reason_code"), {
            "ALLOW_VERIFIER_FAILURE", "DENY_ATTEMPTS_EXHAUSTED", "DENY_SCOPE_VIOLATION", "DENY_TIMEOUT",
            "DENY_UNVERIFIED_MUTATIONS", "DENY_ENVIRONMENT_DRIFT", "DENY_PROCESS_NOT_IDLE",
            "DENY_MISSING_EVIDENCE", "DENY_CONTRADICTORY_EVIDENCE", "DENY_INSUFFICIENT_BUDGET",
            "DENY_UNKNOWN_FAILURE", "DENY_DEFAULT"}),
        authorization_state=authorization, authorization_disposition=disposition,
        shared_started_clock=number(identity.get("started_clock")), shared_deadline=number(identity.get("deadline")),
        shared_budget_seconds=remaining(identity.get("deadline"), identity.get("started_clock")),
        policy_clock=number(observed.get("policy_clock")), observed_clock=number(clock),
        remaining_at_policy_seconds=remaining(identity.get("deadline"), observed.get("policy_clock")),
        remaining_at_observation_seconds=remaining(identity.get("deadline"), clock),
        verification_outcome=choice(verification.get("outcome"), {"passed", "failed", "timeout"}),
        verification_elapsed_seconds=number(verification.get("elapsed_seconds")),
        verifier_termination_basis=choice(native.get("termination_basis"), {
            "signaled_without_controller_termination", "controller_deadline", "controller_cancel"}),
        verifier_exit_code=number(native.get("observed_exit_code")),
        verifier_cleanup=choice(native.get("cleanup"), {"observed_empty", "not_observed", "incomplete"}),
        worker_outcome=choice(result.get("outcome"), {"executor_succeeded_unverified", "executor_failed_unverified"}),
        worker_termination_basis=choice(worker.get("termination_basis"), {
            "not_launched", "executing", "controller_cancel", "controller_deadline",
            "controller_exception", "signaled_without_controller_termination"}),
        worker_exit_code=number(worker.get("observed_exit_code")),
        worker_cleanup=choice(worker.get("cleanup"), {"observed_empty", "not_observed", "incomplete"}),
        worker_cleanup_exit_code=number(cleanup.get("exit_code")), worker_empty_job=boolean(cleanup.get("empty_job")))
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    return encoded if len(encoded) <= 4000 else '{"diagnostic_truncated":true}'


@unittest.skipUnless(os.name == "nt", "Real Windows native evidence required")
class ActiveAllocationTests(unittest.TestCase):
    def setUp(self):
        if self._testMethodName != "test_heartbeat_progress_during_model_wait":
            return fixtures.ActiveGrantTests.setUp(self)
        import json
        import subprocess
        import test_safe_qwen_integration as integration
        self._heartbeat_budget = 120
        prepared = integration.IntegrationTests.prepared
        def bounded(fixture, *args, **kwargs):
            # Freeze one shared budget before configuration or run creation.
            fixture.cfg["timeout_seconds"] = self._heartbeat_budget
            fixture.data["unit_timeout_seconds"] = self._heartbeat_budget
            fixture.path.write_text(json.dumps(fixture.cfg))
            fixture.policy_path.write_text(json.dumps(fixture.data))
            return prepared(fixture, *args, **kwargs)
        with patch.object(integration.IntegrationTests, "prepared", bounded):
            fixtures.ActiveGrantTests.setUp(self)
        popen = subprocess.Popen
        def inert_server(argv, *args, **kwargs):
            if type(argv) is list and argv[-1] == "import time;time.sleep(60)":
                argv = [*argv[:-1], f"import time;time.sleep({self._heartbeat_budget + 60})"]
            return popen(argv, *args, **kwargs)
        item = patch("subprocess.Popen", side_effect=inert_server)
        item.start(); self.addCleanup(item.stop)
    prepare_timeout_fixture = fixtures.ActiveGrantTests.prepare_timeout_fixture
    stop_fixture = fixtures.ActiveGrantTests.stop_fixture
    servers = fixtures.ActiveGrantTests.servers
    close_supervisor = fixtures.ActiveGrantTests.close_supervisor
    reload = fixtures.ActiveGrantTests.reload
    failure = fixtures.ActiveGrantTests.failure

    def allocate(self, **kwargs):
        return self.scheduler.allocate_safe_qwen_repair_recovery("safe-qwen-task", gateway=self.gateway,
                                                                timeout_seconds=60, **kwargs)

    def test_consumed_and_exactly_one_unstarted_attempt(self):
        self.failure()
        import winprocess
        native_launch = winprocess.ControllerVerifier.launch
        launched = []
        def launch(native, *args):
            selected = self.reload().state[active.KEY]
            self.assertEqual((selected["schema_version"], selected["mode"]), (5, "ACTIVE_REPAIR_ALLOCATE"))
            self.assertEqual(selected["allocation_contract"]["attempt_id"], "safe-qwen-task-attempt-2")
            result = native_launch(native, *args)
            launched.append(native.pid)
            return result
        with patch.object(winprocess.ControllerVerifier, "launch", launch): result = self.allocate()
        g = result["authorization"]
        self.assertEqual((g["schema_version"], g["state"], g["consumption"]["schema_version"]), (2, "CONSUMED", 2))
        loaded = self.reload()
        d, c = loaded.state[active.KEY], g["consumption"]
        self.assertEqual((d["schema_version"], d["mode"]), (5, "ACTIVE_REPAIR_ALLOCATE"))
        unit = loaded.state["work_units"]["units"]["safe-qwen-task"]
        self.assertEqual(unit["spec"]["limits"]["max_attempts"], 2)
        self.assertEqual(d["identity"]["worker"]["limits"]["max_attempts"], 1)
        self.assertEqual(len(unit["attempts"]), 2)
        self.assertEqual((unit["status"], unit["current_attempt"]), ("EXECUTING", c["attempt_id"]))
        a2 = unit["attempts"][1]
        self.assertEqual(a2["stage"], "allocated")
        self.assertIsNone(a2["execution_result"])
        self.assertIsNone(a2["verifier_result"])
        self.assertIsNone(a2["completed_at"])
        self.assertNotEqual(a2["ai_execution_id"], d["identity"]["execution_id"])
        self.assertEqual(c["ai_binding"], grants.ai_binding(d))
        grant_record = loaded.records[c["grant_wal"]["sequence"] - 1]
        consumed = loaded.records[grant_record["sequence"]]
        self.assertEqual(consumed["repair_authorization"]["event"], "repair_authorization_consumed")
        self.assertEqual(consumed["previous"], grant_record["sha256"])
        self.assertEqual(unit["attempts"][0], grant_record["state"]["work_units"]["units"][unit["unit_id"]]["attempts"][0])
        self.assertEqual(d, grant_record["state"][active.KEY])
        self.assertGreaterEqual(loaded.state["manager"]["runtime_seconds"], grant_record["state"]["manager"]["runtime_seconds"])
        self.assertEqual(launched, [d["process"]["pid"], d["verification"]["observations"]["check-1"]["native"]["verifier"]["pid"]])
        self.assertTrue(all(winprocess.identity(pid) is None for pid in launched))
        self.assertIsNone(loaded.state["last_verified_checkpoint"])
        self.assertEqual(loaded.state["verified_progress"], [])
        self.assertNotIn("repair_dispatch", loaded.state)
        self.assertNotIn("repair_verification", loaded.state)
        self.assertIsNone(self.store._safe_qwen_active_controller)

    def test_passing_first_attempt_no_grant_or_allocation(self):
        self.assertIsNone(self.allocate()["authorization"])
        state = self.reload().state
        self.assertNotIn("repair_authorizations", state)
        self.assertEqual(len(state["work_units"]["units"]["safe-qwen-task"]["attempts"]), 1)

    def test_actual_failed_edit_retains_candidate_and_source(self):
        import json
        from test_supervised_safe_qwen import wire
        edit = json.loads(self.edit)
        edit["arguments"]["new_text"] = "return a + b"
        self.responses = [wire(json.dumps(edit)), wire(self.submit)]
        result = self.allocate()
        self.assertEqual(result["authorization"]["state"], "CONSUMED")
        d = result["active_recovery"]
        self.assertNotEqual(d["snapshot_before"], d["result"]["candidate"])
        self.assertEqual(d["result"]["changed_paths"], ["calculator.py"])
        self.assertEqual(d["verification"]["before"], d["verification"]["after"])
        self.assertIn("return a + b", (self.repo.root / "calculator.py").read_text())

    def test_policy_deny_has_no_grant(self):
        self.failure()
        issue = durable.Store._issue_ai_repair_grant
        def deny(store, owner):
            with patch.object(self.scheduler, "remaining_task_budget", return_value=0): return issue(store, owner)
        with patch.object(durable.Store, "_issue_ai_repair_grant", deny): result = self.allocate()
        self.assertIsNone(result["authorization"])
        self.assertNotIn("repair_authorizations", self.reload().state)

    def refuse_before_consume(self, change):
        self.failure()
        consume = durable.Store._consume_ai_repair_grant
        calls = []
        def changed(store, owner):
            self.assertEqual(owner.data["schema_version"], 5)
            self.assertEqual(next(iter(store.state["repair_authorizations"].values()))["state"], "GRANTED")
            calls.append(True)
            change(owner)
            return consume(store, owner)
        with patch.object(durable.Store, "_consume_ai_repair_grant", changed), \
                self.assertRaises((durable.DurableError, ValueError)):
            self.allocate()
        self.assertEqual(calls, [True])
        loaded = self.reload()
        self.assertEqual(len(loaded.state["work_units"]["units"]["safe-qwen-task"]["attempts"]), 1)
        self.assertEqual(next(iter(loaded.state["repair_authorizations"].values()))["state"], "GRANTED")
        self.assertIsNone(loaded._safe_qwen_active_controller)

    def test_stale_candidate_refuses(self):
        self.refuse_before_consume(lambda owner: (self.repo.root / "calculator.py").write_text("# stale candidate\n"))

    def test_stale_config_refuses(self):
        self.refuse_before_consume(lambda owner: owner.executor.config.update(timeout_seconds=1))

    def test_stale_registry_refuses(self):
        self.refuse_before_consume(lambda owner: owner.scheduler.verifier_registry["check-1"].update(timeout_seconds=1))

    def test_scope_drift_refuses(self):
        def drift(owner):
            owner.scheduler.parent_scope = copy.deepcopy(owner.scheduler.parent_scope)
            owner.scheduler.parent_scope["allowed_paths"].append("foreign.py")
        self.refuse_before_consume(drift)

    def test_environment_drift_refuses(self):
        def drift(owner):
            fingerprint = durable.current_fingerprint
            def changed(*args, **kwargs):
                value = fingerprint(*args, **kwargs)
                value["runtime"]["python"]["path"] = "C:/foreign/python.exe"
                return value
            item = patch("durable.current_fingerprint", side_effect=changed)
            item.start(); self.addCleanup(item.stop)
        self.refuse_before_consume(drift)

    def test_wrong_session_refuses(self):
        self.refuse_before_consume(lambda owner: owner.identity["session"].update(manager_session=999))

    def test_expired_cumulative_budget_refuses(self):
        def exhausted(owner):
            self.addCleanup(patch.stopall)
            patch.object(owner.scheduler, "remaining_task_budget", return_value=0).start()
        self.refuse_before_consume(exhausted)

    def test_foreign_native_reference_refuses(self):
        self.refuse_before_consume(lambda owner: owner.data["verification"]["observations"]["check-1"]["binding"]["wal"]["invocation_bound"].update(sha256="0" * 64))

    def test_controller_identity_refuses(self):
        self.refuse_before_consume(lambda owner: setattr(owner.gateway, "supervisor", None))

    def test_issuance_only_v4_cannot_consume_or_upgrade(self):
        self.failure()
        issue = durable.Store._issue_ai_repair_grant
        consume = durable.Store._consume_ai_repair_grant
        def issuance(store, owner):
            result = issue(store, owner)
            self.assertTrue(owner.closed)
            with self.assertRaises(durable.DurableError): consume(store, owner)
            return result
        with patch.object(durable.Store, "_issue_ai_repair_grant", issuance):
            result = self.scheduler.authorize_safe_qwen_repair_recovery("safe-qwen-task", gateway=self.gateway, timeout_seconds=60)
        self.assertEqual(result["authorization"]["state"], "GRANTED")
        with self.assertRaises(durable.DurableError): self.allocate()
        self.assertEqual(self.reload().state[active.KEY]["schema_version"], 4)

    def test_duplicate_concurrent_and_foreign_consumption(self):
        self.failure()
        consume = durable.Store._consume_ai_repair_grant
        errors, workers, captured = [], [], []
        def concurrent(store, owner):
            captured.append(owner)
            def other():
                try: consume(store, owner)
                except durable.DurableError: errors.append("refused")
            thread = threading.Thread(target=other)
            workers.append(thread); thread.start()
            # The other thread waits for the atomic controller section, then
            # sees the closed capability. Never wait for it holding the mutex.
            return consume(store, owner)
        with patch.object(durable.Store, "_consume_ai_repair_grant", concurrent): self.allocate()
        for thread in workers:
            thread.join(10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, ["refused"])
        with self.assertRaises(durable.DurableError): consume(self.store, captured[0])
        for value in (None, {}, self.reload().state[active.KEY], captured[0]):
            with self.assertRaises(durable.DurableError): consume(self.reload(), value)
        self.assertEqual(sum(r.get("repair_authorization", {}).get("event") == "repair_authorization_consumed" for r in self.store.records), 1)

    def fail_consumption(self, *, after=False, torn=False, cache=False):
        self.failure()
        append, write = self.store.append, durable.atomic_write
        captured = []
        def faulty(event, **value):
            hit = value.get("repair_authorization", {}).get("event") == "repair_authorization_consumed"
            if hit and not after:
                if torn:
                    with (self.store.directory / "events.jsonl").open("ab") as stream: stream.write(b'{"event":')
                captured.append((self.store.directory / "events.jsonl").read_bytes())
                raise OSError("Crash before atomic consumption")
            result = append(event, **value)
            if hit:
                captured.append((self.store.directory / "events.jsonl").read_bytes())
                if not cache: raise OSError("Uncertain atomic consumption commit")
            return result
        def failed_cache(path, content):
            if captured and path == self.store.directory / "state.json": raise OSError("Stale cache")
            return write(path, content)
        with patch.object(self.store, "append", side_effect=faulty), patch("durable.atomic_write", side_effect=failed_cache), \
                self.assertRaises(OSError): self.allocate()
        self.assertTrue(self.store._repair_write_failed)
        self.assertEqual((self.store.directory / "events.jsonl").read_bytes(), captured[-1])
        with self.assertRaises(durable.DurableError): self.store.append("diagnostic_after_uncertainty")
        if torn:
            with self.assertRaises(durable.DurableError): self.reload()
        else:
            loaded = self.reload()
            self.assertEqual(next(iter(loaded.state["repair_authorizations"].values()))["state"], "CONSUMED" if after else "GRANTED")
            self.assertEqual(len(loaded.state["work_units"]["units"]["safe-qwen-task"]["attempts"]), 2 if after else 1)
            self.assertIsNone(loaded._safe_qwen_active_controller)
            with self.assertRaises(durable.DurableError): loaded._consume_ai_repair_grant(loaded.state[active.KEY])

    def test_atomic_commit_failure_before_write(self): self.fail_consumption()
    def test_atomic_commit_uncertain_after_write(self): self.fail_consumption(after=True)
    def test_torn_atomic_write_refuses_reload(self): self.fail_consumption(torn=True)
    def test_stale_cache_replays_consumed_history_only(self): self.fail_consumption(after=True, cache=True)

    def test_real_controller_exit_before_and_after_atomic_commit(self):
        import json
        import sys
        import tempfile
        from pathlib import Path
        import winprocess
        root = Path(__file__).resolve().parents[1]
        for boundary, attempts in (("repair_authorization_granted", 1), ("repair_authorization_consumed", 2)):
            with self.subTest(boundary=boundary):
                scratch = self.root / boundary
                scratch.mkdir()
                selected = scratch / "selected.json"
                script = scratch / "controller-exit.py"
                script.write_text(
                    "import os,sys,json,tempfile\nfrom pathlib import Path\n"
                    f"sys.path[:0]=[{str(root)!r},{str(root/'tests')!r}]\n"
                    f"tempfile.tempdir={str(scratch)!r}\n"
                    "from test_safe_qwen_active_allocation import ActiveAllocationTests\n"
                    "t=ActiveAllocationTests('test_consumed_and_exactly_one_unstarted_attempt');t.setUp();t.failure()\n"
                    f"Path({str(selected)!r}).write_text(json.dumps({{'directory':str(t.store.directory)}}))\n"
                    "append=t.store.append\n"
                    "def crash(event,**fields):\n result=append(event,**fields)\n"
                    f" if fields.get('repair_authorization',{{}}).get('event')=={boundary!r}: os._exit(73)\n"
                    " return result\n"
                    "t.store.append=crash\nt.allocate()\n")
                native = winprocess.ControllerVerifier("Local\\ai-allocation-exit-" + scratch.name + "-" + self.root.name)
                try:
                    with tempfile.TemporaryFile() as inp, tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
                        native.launch([str(Path(sys.executable).resolve()), "-I", "-B", str(script)], scratch, (inp, out, err))
                        native.resume(native.identity)
                        code = native.wait(90)
                        err.seek(0)
                        self.assertEqual(code, 73, err.read(8192).decode(errors="replace"))
                        native.drain(10)
                finally:
                    native.terminate_job(); native.drain(10); native.close()
                directory = Path(json.loads(selected.read_text())["directory"])
                loaded = durable.Store(directory.parent, directory.name)
                wal = directory / "events.jsonl"; before = wal.read_bytes()
                loaded.load()
                self.assertEqual(len(loaded.state["work_units"]["units"]["safe-qwen-task"]["attempts"]), attempts)
                self.assertIsNone(loaded._safe_qwen_active_controller)
                with self.assertRaises(durable.DurableError): loaded._consume_ai_repair_grant(loaded.state[active.KEY])
                self.assertEqual(wal.read_bytes(), before)

    def test_no_restart_dispatch_third_attempt_or_checkpoint(self):
        self.failure(); self.allocate()
        self.sup.__exit__(None, None, None)
        loaded = self.reload()
        before = (loaded.directory / "events.jsonl").read_bytes()
        import manager
        import supervision
        import work_unit_scheduler as scheduler_module
        import test_safe_qwen_active_first as first
        scheduler = scheduler_module.WorkUnitScheduler(loaded, self.repo, supervisor=supervision.Supervisor(loaded))
        section = scheduler.get_section(); unit = section["units"]["safe-qwen-task"]
        # The base fixture replaces execute_attempt with a no-execution mock.
        # Restore the real bound method here to exercise its production gate.
        with patch.object(scheduler, "execute_attempt", first.REAL_EXECUTE.__get__(scheduler, type(scheduler))):
            for operation in (lambda: scheduler.allocate_safe_qwen_repair_recovery(unit["unit_id"], gateway=self.gateway),
                              lambda: scheduler.execute_attempt(unit, section, 2),
                              lambda: scheduler.execute_unit(unit, section),
                              lambda: scheduler.invoke_native_verifier(unit["unit_id"], unit["current_attempt"], "check-1"),
                              lambda: first.REAL_TERMINAL(loaded, "forbidden-checkpoint")):
                with self.assertRaises((durable.DurableError, manager.Stop)): operation()
        import work_units
        with self.assertRaises(work_units.WorkUnitError): work_units.create_attempt(copy.deepcopy(unit), unit["unit_id"] + "-attempt-3")
        self.assertEqual((loaded.directory / "events.jsonl").read_bytes(), before)

    def test_closed_consumption_identity_and_immutable_history(self):
        self.failure(); self.allocate()
        state = self.reload().state
        aid = next(iter(state["repair_authorizations"]))
        for key, value in (("schema_version", True), ("attempt_id", "foreign-attempt-2"),
                           ("controller_session", {}), ("grant_wal", {"sequence": 1, "sha256": "0" * 64}),
                           ("ai_binding", {}), ("extra", True)):
            changed = copy.deepcopy(state)
            changed["repair_authorizations"][aid]["consumption"][key] = value
            # WAL references are also authenticated by the history validator.
            with self.subTest(field=key), self.assertRaises(durable.DurableError):
                if key == "grant_wal":
                    records = copy.deepcopy(self.store.records)
                    next(r for r in reversed(records) if "repair_authorization" in r)["state"] = changed
                    durable._validate_repair_history(records, self.store.read_evidence)
                else: durable.validate_state(changed)
        for index, key, value in ((0, "changed_paths", ["foreign.py"]), (1, "stage", "executing"),
                                  (1, "ai_execution_id", "0" * 64)):
            changed = copy.deepcopy(state)
            unit = changed["work_units"]["units"]["safe-qwen-task"]
            unit["attempts"][index][key] = value
            import work_units
            work_units.seal_unit_state(unit); work_units._reseal_section(changed["work_units"])
            with self.subTest(attempt=index, field=key), self.assertRaises(durable.DurableError): durable.validate_state(changed)
        for key, value in (("schema_version", True), ("consumption_schema_version", 1), ("execution_id", "0" * 64),
                           ("source_execution_id", "0" * 64), ("worker_sha256", "0" * 64), ("extra", True)):
            changed = copy.deepcopy(state)
            changed[active.KEY]["allocation_contract"][key] = value
            with self.subTest(contract=key), self.assertRaises(durable.DurableError): durable.validate_state(changed)
        records = copy.deepcopy(self.store.records)
        final = next(r for r in reversed(records) if "repair_authorization" in r)
        final["state"][active.KEY]["schema_version"] = 4
        final["state"][active.KEY]["mode"] = "ACTIVE_ATTEMPT1_GRANT"
        final["state"][active.KEY].pop("allocation_contract")
        with self.assertRaises(durable.DurableError): active.validate_history(records, self.store.read_evidence, reservation_exists=True)

    def test_heartbeat_progress_during_model_wait(self):
        import test_safe_qwen_active_verification as native
        import safe_qwen_active_eligibility as eligibility
        import time
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
        self.run_first = lambda **kwargs: self.scheduler.allocate_safe_qwen_repair_recovery(
            "safe-qwen-task", gateway=self.gateway, **dict(kwargs, timeout_seconds=self._heartbeat_budget))
        self.pulse_frequently = lambda: native.ActiveVerificationTests.pulse_frequently(self)
        try:
            with patch.object(eligibility, "classify_for_grant", observed_classification):
                native.ActiveVerificationTests.test_heartbeat_progress_during_startup_and_model_request(self)
            self.assertEqual(next(iter(self.reload().state["repair_authorizations"].values()))["state"], "CONSUMED")
        finally:
            print("F3H_DIAGNOSTIC " + _allocation_diagnostic(self.store.state, observed, time.monotonic()), flush=True)

    def test_native_timeout_drains_verifier_and_descendant(self):
        import winprocess
        import json
        result = self.allocate()
        self.assertEqual(result["active_recovery"]["verification"]["outcome"], "timeout")
        self.assertIsNone(result["authorization"])
        self.assertNotIn("repair_authorizations", self.reload().state)
        proof = json.loads(self.marker.read_text())
        # Same real retained-handle/descendant fixture as the approved verifier.
        for pid in proof.values(): self.assertIsNone(winprocess.identity(pid))
