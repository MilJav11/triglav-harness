"""Native Windows qualification; real handles, inert model process, fake HTTP."""
import copy
from contextlib import ExitStack
import math
import os
import json
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

import durable
import harness
import safe_qwen_active_recovery as active
import safe_qwen_active_verification as verification
import supervised_safe_qwen as adapter
import winprocess
import manager
from work_unit_scheduler import WorkUnitScheduler
from supervision import Supervisor
import test_safe_qwen_active_first as first


def _verification_diagnostic(data, observed, clock):
    """Closed, bounded fields only; never stringify state, WAL or exceptions."""
    def mapping(value): return value if type(value) is dict else {}
    def number(value):
        return round(value, 3) if type(value) in (int, float) and abs(value) <= 10**9 and math.isfinite(value) else None
    def boolean(value): return value if type(value) is bool else None
    def label(value, allowed): return value if type(value) is str and value in allowed else "unknown"
    data, observed = mapping(data), mapping(observed)
    identity = mapping(data.get("identity"))
    result = mapping(data.get("result"))
    cleanup = mapping(result.get("cleanup_proof"))
    v = mapping(data.get("verification"))
    native = mapping(mapping(mapping(v.get("observations")).get("check-1")).get("native"))
    started, deadline, clock = number(identity.get("started_clock")), number(identity.get("deadline")), number(clock)
    def remaining(name):
        end = number(identity.get(name))
        return number(end - clock) if end is not None and clock is not None else None
    value = dict(schema_version=1, fixture_budget_seconds=number(observed.get("fixture_budget")),
        shared_started_clock=started, shared_deadline=deadline,
        shared_budget_seconds=number(deadline - started) if deadline is not None and started is not None else None,
        remaining_shared_seconds=remaining("deadline"), remaining_unit_seconds=remaining("unit_deadline"),
        remaining_task_seconds=remaining("task_deadline"),
        worker_outcome=label(result.get("outcome"), {"executor_succeeded_unverified", "executor_failed_unverified"}),
        worker_empty_job=boolean(cleanup.get("empty_job")), worker_exit_code=number(cleanup.get("exit_code")),
        verification_phase=label(v.get("phase"), {"OBSERVING", "COMPLETE"}),
        verification_outcome=label(v.get("outcome"), {"passed", "failed", "timeout"}),
        verification_elapsed_seconds=number(v.get("elapsed_seconds")),
        verifier_termination_basis=label(native.get("termination_basis"),
            {"signaled_without_controller_termination", "controller_deadline", "controller_cancel"}),
        verifier_cleanup=label(native.get("cleanup"), {"observed_empty"}),
        guard_refused=boolean(observed.get("guard_refused")), guard_clock=number(observed.get("guard_clock")),
        guard_observation_incomplete=boolean(observed.get("guard_observation_incomplete")),
        guard_remaining_shared_seconds=number(observed.get("guard_remaining")),
        guard_remaining_task_budget=number(observed.get("guard_task_budget")),
        fault_event=label(observed.get("fault_event"), {"VERIFY_INTENT", "invocation_intent", "invocation_bound",
            "invocation_release", "process_exit", "invocation_complete", "VERIFY_OBSERVATION", "VERIFY_COMPLETE"}),
        fault_stage=label(observed.get("fault_stage"), {"before_append", "after_append"}),
        fault_hits=number(observed.get("fault_hits")),
        subsequent_writes_refused=boolean(observed.get("subsequent_writes_refused")),
        fault_native_cleanup_verified=boolean(observed.get("fault_native_cleanup_verified")))
    for name in ("verify_selected", "same_thread", "contract_matches", "worker_complete", "worker_succeeded",
                 "worker_cleanup_present", "network_idle", "cancelled"):
        value[name] = boolean(observed.get(name))
    payload = json.dumps(value, sort_keys=True, allow_nan=False)
    assert len(("VERIFICATION_DIAGNOSTIC " + payload).encode("utf-8")) <= 4096
    return payload


@unittest.skipUnless(os.name == "nt", "Native Windows Job/identity observations required")
class ActiveVerificationTests(unittest.TestCase):
    stop_fixture = first.ActiveFirstTests.stop_fixture
    servers = first.ActiveFirstTests.servers
    close_supervisor = first.ActiveFirstTests.close_supervisor
    reload = first.ActiveFirstTests.reload

    # One semantic/WAL scenario envelope: worker + up to two native verifiers
    # (each still capped at 30s) + durable validation/cleanup margin. It is frozen
    # BEFORE either ledger. Intentional deadline/death scenarios retain originals.
    SCENARIO_BUDGET = 120
    DEADLINE_SCENARIOS = frozenset({
        "test_native_timeout_drains_verifier_and_descendant",
        "test_actual_controller_death_during_native_verification_never_relaunches",
        "test_cumulative_budget_exhausted_after_worker_prevents_verification",
    })

    def setUp(self):
        # Other test families borrow this setup; do not change their reviewed
        # configuration, caller budgets or inert processes.
        selected = type(self) is ActiveVerificationTests and self._testMethodName not in self.DEADLINE_SCENARIOS
        self._verification_budget = self.SCENARIO_BUDGET if selected else None
        self._verification_observed = {"guard_refused": False}
        create = manager.create_run
        def fixture(store, repo, *args, **kwargs):
            if self._testMethodName == "test_complete_sequence_observes_both_registered_verifiers":
                kwargs["verifier_registry"] = copy.deepcopy(kwargs["verifier_registry"])
                kwargs["verifier_registry"]["check-2"] = copy.deepcopy(kwargs["verifier_registry"]["check-1"])
                kwargs["work_units"] = copy.deepcopy(kwargs["work_units"])
                kwargs["work_units"][0]["verifier_ids"] = ["check-1", "check-2"]
            if self._testMethodName == "test_native_timeout_drains_verifier_and_descendant":
                self.marker = repo.root.parent / "verifier-descendant.json"
                if not getattr(self, "timeout_fixture_prepared", False):
                    self.prepare_timeout_fixture(repo)
                kwargs["verifier_registry"] = copy.deepcopy(kwargs["verifier_registry"])
                kwargs["verifier_registry"]["check-1"]["timeout_seconds"] = 8
            return create(store, repo, *args, **kwargs)
        with ExitStack() as stack:
            if selected:
                import test_safe_qwen_integration as integration
                prepared = integration.IntegrationTests.prepared
                def bounded(fixture, *args, **kwargs):
                    fixture.cfg["timeout_seconds"] = self._verification_budget
                    fixture.data["unit_timeout_seconds"] = self._verification_budget
                    fixture.path.write_text(json.dumps(fixture.cfg))
                    fixture.policy_path.write_text(json.dumps(fixture.data))
                    return prepared(fixture, *args, **kwargs)
                stack.enter_context(patch.object(integration.IntegrationTests, "prepared", bounded))
            stack.enter_context(patch("manager.create_run", side_effect=fixture))
            if self._testMethodName == "test_v2_completed_worker_is_never_upgraded":
                # This test compares exact WAL bytes across a ~16s window on slow runners; isolate it
                # from periodic (15s) heartbeat WAL growth. Real controller behavior is unchanged.
                orig_init = Supervisor.__init__
                def sup_init(sup_self, *args, **kwargs):
                    orig_init(sup_self, *args, **kwargs)
                    sup_self._pulse_interval = 600
                stack.enter_context(patch.object(Supervisor, "__init__", sup_init))
            first.ActiveFirstTests.setUp(self)
        if selected:
            self.assertEqual(self.cfg["timeout_seconds"], self._verification_budget)
            self.assertEqual(self.worker_spec["limits"]["timeout_seconds"], self._verification_budget)
            import subprocess
            popen = subprocess.Popen
            def inert_server(argv, *args, **kwargs):
                if type(argv) is list and argv and argv[-1] == "import time;time.sleep(60)":
                    argv = [*argv[:-1], f"import time;time.sleep({self._verification_budget + 30})"]
                return popen(argv, *args, **kwargs)
            item = patch("subprocess.Popen", new=inert_server)
            item.start(); self.addCleanup(item.stop)

    def run_first(self, **kwargs):
        if self._verification_budget is not None:
            kwargs.setdefault("timeout_seconds", self._verification_budget)
        try:
            return first.ActiveFirstTests.run_first(self, **kwargs)
        finally:
            if type(self) is ActiveVerificationTests:
                self.report_verification()

    def report_verification(self, owner=None):
        self._verification_observed["fixture_budget"] = self.cfg["timeout_seconds"]
        data = owner.data if owner is not None else self.store.state.get(active.KEY)
        print("VERIFICATION_DIAGNOSTIC " + _verification_diagnostic(data, self._verification_observed, time.monotonic()), flush=True)

    def prepare_timeout_fixture(self, repo):
        import subprocess
        import tempfile
        # An owned disposable fixture commit BEFORE either controller run is
        # created. Never bypass the real clean-repository precondition.
        self.assertEqual(repo.root.name, "repo")
        self.assertTrue(repo.root.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()))
        (repo.root / "test_calculator.py").write_text(
                    "import unittest,subprocess,sys,time,json\nfrom pathlib import Path\n"
                    "class Check(unittest.TestCase):\n def test_hang(self):\n"
                    "  p=subprocess.Popen([sys.executable,'-I','-B','-c','import time;time.sleep(120)'],close_fds=True)\n"
                    f"  Path({str(self.marker)!r}).write_text(json.dumps({{'pid':p.pid}}))\n  time.sleep(120)\n")
        for argv in (("add", "test_calculator.py"), ("commit", "-qm", "offline native timeout fixture")):
            subprocess.run(["git", "-C", str(repo.root), "-c", f"safe.directory={repo.root}",
                            "-c", "user.name=Offline fixture", "-c", "user.email=fixture@localhost", *argv],
                           check=True, capture_output=True)
        self.timeout_fixture_prepared = True

    def run_verified(self, **kwargs):
        if self._verification_budget is not None:
            kwargs.setdefault("timeout_seconds", self._verification_budget)
        guard, owners = verification.guard, []
        def observed_guard(owner, *args):
            if not owners: owners.append(owner)
            try:
                return guard(owner, *args)
            except BaseException:
                self._verification_observed["guard_refused"] = True
                try:
                    now = time.monotonic()
                    self._verification_observed.update(guard_clock=now,
                        guard_remaining=owner.identity["deadline"] - now,
                        guard_task_budget=owner.scheduler.remaining_task_budget(),
                        verify_selected=owner.verify_selected, same_thread=threading.get_ident() == owner.thread_id,
                        contract_matches=owner.data["verification_contract"] == verification.contract(),
                        worker_complete=owner.data["phase"] == "COMPLETE",
                        worker_succeeded=owner.data["result"]["outcome"] == "executor_succeeded_unverified",
                        worker_cleanup_present=owner.cleanup is not None, network_idle=owner.network_thread is None,
                        cancelled=owner.cancel_event.is_set())
                except BaseException:
                    # Diagnostics must never replace the original guard error.
                    self._verification_observed["guard_observation_incomplete"] = True
                raise
        try:
            with patch.object(verification, "guard", new=observed_guard):
                return self.scheduler.verify_safe_qwen_recovery_first("safe-qwen-task", gateway=self.gateway, **kwargs)
        finally:
            self.report_verification(owners[0] if owners else None)

    def test_first_native_pass_is_intermediate_only(self):
        data = self.run_verified()["active_recovery"]
        self.assertEqual(data["schema_version"], 3)
        v = data["verification"]
        self.assertEqual(v["phase"], "COMPLETE")
        self.assertEqual(v["outcome"], "passed")
        self.assertEqual(v["before"], v["after"])
        loaded = self.reload()
        unit = loaded.state["work_units"]["units"]["safe-qwen-task"]
        self.assertEqual(len(unit["attempts"]), 1)
        self.assertEqual(unit["status"], "VERIFYING")
        self.assertIsNone(unit["current_attempt"])
        self.assertEqual(unit["attempts"][0]["outcome"], "passed")
        self.assertIsNotNone(unit["attempts"][0]["completed_at"])
        self.assertIsNone(unit["result"])
        self.assertIsNone(loaded.state["last_verified_checkpoint"])
        self.assertIsNone(loaded._safe_qwen_active_controller)
        for key in ("repair_authorizations", "repair_recovery", "terminal_acceptance"):
            self.assertNotIn(key, loaded.state)

    def test_first_native_failure_retains_genuine_facts_without_grant(self):
        from test_supervised_safe_qwen import wire
        self.responses = [wire(self.submit)]  # Original deterministic defect stays.
        data = self.run_verified()["active_recovery"]
        self.assertEqual(data["verification"]["outcome"], "failed")
        self.assertEqual(data["verification"]["failure_classification"], "verification_failed")
        row = data["verification"]["observations"]["check-1"]
        self.assertNotEqual(row["native"]["observed_exit_code"], 0)
        self.assertEqual(row["native"]["cleanup"], "observed_empty")
        self.assertEqual(set(row["binding"]["wal"]), set(durable._INVOCATION_EVENTS))
        self.assertTrue(row["output"]["stderr"])
        self.assertNotIn("repair_authorizations", self.reload().state)

    def test_native_intent_identity_and_release_are_durable_before_effects(self):
        launch, resume = winprocess.ControllerVerifier.launch, winprocess.ControllerVerifier.resume
        launches, resumes = [], []
        def created(native, *args):
            data = self.reload().state[active.KEY]
            if data.get("verification") is not None:
                self.assertEqual(data["verification"]["phase"], "OBSERVING")
                child = next(c for c in self.store.state["supervision"]["children"].values() if "invocation" in c)
                self.assertEqual(child["invocation"]["phase"], "INTENT")
                self.assertEqual(child["invocation"]["candidate_fingerprint"], data["result"]["candidate"]["fingerprint"])
            result = launch(native, *args); launches.append(native.pid); return result
        def released(native, identity):
            loaded = self.reload()
            matches = [c for c in loaded.state["supervision"]["children"].values() if c["pid"] == native.pid]
            if matches:
                self.assertEqual(matches[0]["invocation"]["phase"], "RELEASED")
                self.assertEqual(matches[0]["identity"], identity)
            resumes.append(native.pid); return resume(native, identity)
        with patch.object(winprocess.ControllerVerifier, "launch", created), patch.object(winprocess.ControllerVerifier, "resume", released):
            data = self.run_verified()["active_recovery"]
        self.assertEqual(len(launches), 2); self.assertEqual(resumes, launches)
        row = data["verification"]["observations"]["check-1"]
        for event, ref in row["binding"]["wal"].items():
            record = self.store.records[ref["sequence"] - 1]
            self.assertEqual(record["sha256"], ref["sha256"])
            self.assertEqual(record["transition"], {"event": event, "child_id": row["binding"]["child_id"]})

    def test_native_timeout_drains_verifier_and_descendant(self):
        data = self.run_verified(timeout_seconds=60)["active_recovery"]
        self.assertEqual(data["verification"]["outcome"], "timeout")
        row = data["verification"]["observations"]["check-1"]
        self.assertEqual(row["native"]["termination"], "timeout")
        self.assertEqual(row["native"]["cleanup"], "observed_empty")
        self.assertIsNone(winprocess.identity(row["native"]["verifier"]["pid"]))
        self.assertIsNone(winprocess.identity(json.loads(self.marker.read_text())["pid"]))
        self.assertEqual(self.reload().state[active.KEY]["verification"], data["verification"])

    def test_complete_sequence_observes_both_registered_verifiers(self):
        data = self.run_verified()["active_recovery"]
        self.assertEqual(set(data["verification"]["observations"]), {"check-1", "check-2"})
        self.assertEqual(data["verification"]["outcome"], "passed")
        self.assertEqual(data["verification"]["before"], data["verification"]["after"])
        self.assertEqual(len(self.reload().state["work_units"]["units"]["safe-qwen-task"]["attempts"]), 1)

    def test_actual_controller_death_during_native_verification_never_relaunches(self):
        import sys
        import tempfile
        root = Path(__file__).resolve().parents[1]
        selected, released = self.root / "death-selected.json", self.root / "death-released.json"
        scratch = self.root / "death-fixtures"; scratch.mkdir()
        script = self.root / "native-controller-death.py"
        script.write_text("import sys,json,time,tempfile\nfrom pathlib import Path\n"
                          f"sys.path[:0]=[{str(root)!r},{str(root/'tests')!r}]\n"
                          f"tempfile.tempdir={str(scratch)!r}\n"
                          "from test_safe_qwen_active_verification import ActiveVerificationTests\n"
                          "from supervision import _VerifierInvocation\n"
                          "t=ActiveVerificationTests('test_native_timeout_drains_verifier_and_descendant');t.setUp()\n"
                          f"Path({str(selected)!r}).write_text(json.dumps({{'directory':str(t.store.directory),'marker':str(t.marker)}}))\n"
                          "original=_VerifierInvocation.resume\n"
                          "def resumed(inv,native):\n original(inv,native)\n"
                          f" Path({str(released)!r}).write_text(json.dumps({{'pid':native.pid}}))\n"
                          " time.sleep(60)\n"
                          "_VerifierInvocation.resume=resumed\n"
                          "t.run_verified(timeout_seconds=60)\n")
        native = winprocess.ControllerVerifier("Local\\active-native-death-" + self.root.name)
        try:
            with tempfile.TemporaryFile() as inp, tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
                native.launch([str(Path(sys.executable).resolve()), "-I", "-B", str(script)], self.root, (inp, out, err))
                native.resume(native.identity)
                end = time.monotonic() + 40
                descendant = None
                while time.monotonic() < end and native.wait(0) is None:
                    if selected.exists() and released.exists():
                        info = json.loads(selected.read_text())
                        marker = Path(info["marker"])
                        if marker.exists(): descendant = json.loads(marker.read_text())["pid"]; break
                    time.sleep(0.02)
                err.seek(0)
                self.assertIsNotNone(descendant, err.read(8192).decode(errors="replace"))
                verifier = json.loads(released.read_text())["pid"]
                self.assertTrue(native.k.TerminateProcess(native.process, 137))
                self.assertEqual(native.wait(5), 137)
                # Outer fixture Job remains OPEN. The verifier's own Job must
                # kill both retained native processes on controller death.
                end = time.monotonic() + 5
                while any(winprocess.identity(pid) is not None for pid in (verifier, descendant)) and time.monotonic() < end: time.sleep(0.01)
                for pid in (verifier, descendant): self.assertIsNone(winprocess.identity(pid))
        finally:
            native.terminate_job(); native.drain(10); native.close()
        directory = Path(info["directory"])
        wal = directory / "events.jsonl"; before = wal.read_bytes()
        loaded = durable.Store(directory.parent, directory.name)
        try: loaded.load()
        except durable.DurableError:
            self.assertFalse(before.endswith(b"\n")); self.assertEqual(wal.read_bytes(), before); return
        self.assertEqual(loaded.state[active.KEY]["verification"]["phase"], "OBSERVING")
        self.assertIsNone(loaded._safe_qwen_active_controller)
        scheduler = WorkUnitScheduler(loaded, self.repo, supervisor=Supervisor(loaded))
        with self.assertRaises(durable.DurableError): scheduler.verify_safe_qwen_recovery_first("safe-qwen-task", gateway=self.gateway)
        self.assertEqual(wal.read_bytes(), before)

    def test_v2_completed_worker_is_never_upgraded(self):
        self.run_first()
        before = (self.store.directory / "events.jsonl").read_bytes()
        with self.assertRaises(durable.DurableError): self.run_verified()
        self.assertEqual((self.store.directory / "events.jsonl").read_bytes(), before)
        self.assertEqual(self.reload().state[active.KEY]["schema_version"], 2)

    def test_replay_cannot_upgrade_v2_selection_mid_execution(self):
        self.run_first()
        records = copy.deepcopy(self.store.records)
        upgrade = next(r for r in records if r.get(active.MARKER, {}).get("event") == "RELEASE")
        d = upgrade["state"][active.KEY]
        d.update(schema_version=3, mode="ACTIVE_ATTEMPT1_VERIFY", verification_contract=verification.contract(), verification=None)
        # Individually consistent data cannot alter the original selection.
        active.validate(upgrade["state"])
        with self.assertRaises(durable.DurableError): active.validate_history(records, self.store.read_evidence, reservation_exists=True)

    def test_unrelated_commit_during_pending_owner_write_is_rejected(self):
        observed = []
        append = self.store.append
        def checked(kind, **fields):
            if (fields.get(active.MARKER) or {}).get("event") == "INTENT":
                owner = self.store._safe_qwen_active_controller
                self.assertIsNotNone(owner.pending)
                with self.assertRaises(durable.DurableError): owner.guard_state_commit(self.store.state, {"event": "controller_heartbeat"})
                observed.append(True)
            return append(kind, **fields)
        with patch.object(self.store, "append", side_effect=checked): self.run_verified()
        self.assertEqual(observed, [True])

    def test_reload_refuses_worker_verifier_grant_and_checkpoint(self):
        self.run_verified(); self.sup.__exit__(None, None, None)
        loaded = self.reload(); before = (self.store.directory / "events.jsonl").read_bytes()
        scheduler = WorkUnitScheduler(loaded, self.repo, supervisor=Supervisor(loaded))
        with self.assertRaises(durable.DurableError): scheduler.verify_safe_qwen_recovery_first("safe-qwen-task", gateway=self.gateway)
        with self.assertRaises(manager.Stop): scheduler.invoke_native_verifier("safe-qwen-task", "safe-qwen-task-attempt-1", "check-1")
        with self.assertRaises(durable.DurableError): loaded._begin_repair_recovery(scheduler, "safe-qwen-task")
        with self.assertRaises(durable.DurableError): first.REAL_TERMINAL(loaded, "blocked", controller=loaded._controller_token)
        section = loaded.state["work_units"]
        for number in (1, 2, 3):
            with self.assertRaises(manager.Stop): first.REAL_EXECUTE(scheduler, section["units"]["safe-qwen-task"], section, number)
        self.assertEqual((self.store.directory / "events.jsonl").read_bytes(), before)

    def fail_boundary(self, event, *, before=False):
        append = self.store.append
        persisted = []
        reached = []
        def fault(kind, **fields):
            marker = fields.get(active.MARKER) or fields.get("transition") or {}
            hit = marker.get("event") == event
            if hit and before:
                reached.append("before_append")
                self._verification_observed.update(fault_event=event, fault_stage="before_append", fault_hits=len(reached))
                raise OSError("Before required verification WAL")
            result = append(kind, **fields)
            if hit:
                persisted.append((self.store.directory / "events.jsonl").read_bytes())
                reached.append("after_append")
                self._verification_observed.update(fault_event=event, fault_stage="after_append", fault_hits=len(reached))
                raise OSError("After required verification WAL")
            return result
        pids = []
        launch = winprocess.ControllerVerifier.launch
        def created(native, *args):
            result = launch(native, *args); pids.append(native.pid); return result
        with patch.object(self.store, "append", side_effect=fault), patch.object(winprocess.ControllerVerifier, "launch", created), \
                self.assertRaises((OSError, adapter.AdapterError, RuntimeError)):
            self.run_verified()
        self.assertEqual(reached, ["before_append" if before else "after_append"])
        self.assertTrue(self.store._repair_write_failed)
        for pid in pids: self.assertIsNone(winprocess.identity(pid))
        if persisted: self.assertEqual((self.store.directory / "events.jsonl").read_bytes(), persisted[-1])
        frozen = (self.store.directory / "events.jsonl").read_bytes()
        with self.assertRaises(durable.DurableError): self.store.append("after_uncertain_verification")
        with self.assertRaises(durable.DurableError): self.sup.heartbeat()
        self.assertTrue((self.store.directory / "events.jsonl").read_bytes() == frozen, "Refused writes changed WAL")
        loaded = self.reload()
        self.assertIsNone(loaded._safe_qwen_active_controller)
        self.assertEqual(len(loaded.state["work_units"]["units"]["safe-qwen-task"]["attempts"]), 1)
        self.assertNotIn("repair_authorizations", loaded.state)
        self.assertIsNone(loaded.state["last_verified_checkpoint"])
        self._verification_observed.update(subsequent_writes_refused=True, fault_native_cleanup_verified=True)
        self.report_verification()
        return loaded

    def test_crash_before_verification_intent(self): self.fail_boundary("VERIFY_INTENT", before=True)
    def test_uncertain_verification_intent(self): self.fail_boundary("VERIFY_INTENT")
    def test_uncertain_native_intent(self): self.fail_boundary("invocation_intent")
    def test_uncertain_native_binding(self): self.fail_boundary("invocation_bound")
    def test_uncertain_native_release(self): self.fail_boundary("invocation_release")
    def test_uncertain_native_exit(self): self.fail_boundary("process_exit")
    def test_uncertain_native_complete(self): self.fail_boundary("invocation_complete")
    def test_uncertain_observation(self): self.fail_boundary("VERIFY_OBSERVATION")
    def test_uncertain_verification_complete(self): self.fail_boundary("VERIFY_COMPLETE")

    def test_candidate_drift_after_worker_refuses_verifier(self):
        complete = active._Controller.complete
        def drift(owner, evidence):
            result = complete(owner, evidence)
            (self.repo.root / "calculator.py").write_text("# foreign candidate\n")
            return result
        with patch.object(active._Controller, "complete", drift), self.assertRaises(durable.DurableError): self.run_verified()
        self.assertFalse(any("invocation" in c for c in self.store.state["supervision"]["children"].values()))

    def test_environment_drift_after_worker_refuses_verifier(self):
        complete, fingerprint = active._Controller.complete, durable.current_fingerprint
        changed = []
        def completed(owner, evidence):
            result = complete(owner, evidence); changed.append(True); return result
        def drift(*args, **kwargs):
            value = fingerprint(*args, **kwargs)
            if changed: value["runtime"]["python"]["sha256"] = "0" * 64
            return value
        with patch.object(active._Controller, "complete", completed), patch("durable.current_fingerprint", drift), \
                self.assertRaises(durable.DurableError): self.run_verified()
        self.assertFalse(any("invocation" in c for c in self.store.state["supervision"]["children"].values()))

    def test_candidate_drift_after_native_intent_prevents_creation(self):
        append = self.store.append
        def drift(kind, **fields):
            result = append(kind, **fields)
            if (fields.get("transition") or {}).get("event") == "invocation_intent":
                (self.repo.root / "calculator.py").write_text("# foreign candidate\n")
            return result
        import supervision
        with patch.object(self.store, "append", side_effect=drift), self.assertRaises(supervision.VerifierEvidenceError): self.run_verified()
        child = next(c for c in self.reload().state["supervision"]["children"].values() if "invocation" in c)
        self.assertEqual(child["invocation"]["phase"], "INTENT"); self.assertIsNone(child["pid"])

    def test_no_verification_after_unconfirmed_worker_model_cleanup(self):
        self.cleanup_error = RuntimeError("Fixture model cleanup refused")
        with self.assertRaises(adapter.AdapterError): self.run_verified()
        self.assertFalse(any("invocation" in c for c in self.store.state["supervision"]["children"].values()))

    def test_closed_native_evidence_and_wal_references_refuse_forgery(self):
        self.run_verified(); state = self.reload().state
        for key, value in (("dispatch_result_sha256", "0" * 64), ("execution_id", "foreign"), ("outcome", "passed!")):
            s = copy.deepcopy(state); s[active.KEY]["verification"][key] = value
            with self.subTest(key=key), self.assertRaises(durable.DurableError): active.validate(s)
        s = copy.deepcopy(state)
        s[active.KEY]["verification"]["observations"]["check-1"]["native"]["observed_exit_code"] = True
        with self.assertRaises(durable.DurableError): active.validate(s)
        records = copy.deepcopy(self.store.records)
        r = next(r for r in records if r.get(active.MARKER, {}).get("event") == "VERIFY_OBSERVATION")
        r["state"][active.KEY]["verification"]["observations"]["check-1"]["binding"]["wal"]["invocation_release"]["sha256"] = "0" * 64
        with self.assertRaises(durable.DurableError): active.validate_history(records, self.store.read_evidence, reservation_exists=True)

    def test_torn_verification_tail_is_not_recoverable_m0_tail(self):
        self.run_verified(); self.sup.__exit__(None, None, None)
        wal = self.store.directory / "events.jsonl"
        with wal.open("ab") as stream: stream.write(b'{"sequence":')
        before = wal.read_bytes()
        with self.assertRaises(durable.DurableError): self.reload()
        self.assertEqual(wal.read_bytes(), before)

    def test_cumulative_budget_exhausted_after_worker_prevents_verification(self):
        complete = active._Controller.complete
        changed = []
        remaining = self.scheduler.remaining_task_budget
        def completed(owner, evidence):
            result = complete(owner, evidence); changed.append(True); return result
        with patch.object(active._Controller, "complete", completed), patch.object(self.scheduler, "remaining_task_budget", side_effect=lambda *a: 0 if changed else remaining(*a)), \
                self.assertRaises(durable.DurableError): self.run_verified()
        self.assertIsNone(self.reload().state[active.KEY]["verification"])
        self.assertFalse(any("invocation" in c for c in self.store.state["supervision"]["children"].values()))

    def test_conflicting_commit_during_model_request_stops_before_mutation(self):
        def conflicting():
            with self.assertRaises(durable.DurableError): self.store.commit(current_step="foreign controller mutation")
        self.on_request = conflicting
        with self.assertRaises(adapter.AdapterError): self.run_verified()
        self.assertNotEqual(self.store.state["current_step"], "foreign controller mutation")
        self.assertEqual((self.repo.root / "calculator.py").read_bytes(), self.before)

    def test_scope_change_during_verifier_cannot_seal_a_pass(self):
        from supervision import _VerifierInvocation
        complete = _VerifierInvocation.complete
        def mutated(inv):
            result = complete(inv)
            (self.repo.root / "unapproved.txt").write_text("foreign verifier mutation")
            return result
        with patch.object(_VerifierInvocation, "complete", mutated), self.assertRaises(durable.DurableError): self.run_verified()
        data = self.reload().state[active.KEY]
        self.assertEqual(data["verification"]["phase"], "OBSERVING")
        self.assertEqual(data["verification"]["observations"], {})

    def test_wrong_controller_cannot_use_native_verification_hook(self):
        from supervision import _VerifierInvocation
        with self.assertRaises(durable.DurableError):
            _VerifierInvocation(self.scheduler, "safe-qwen-task", "safe-qwen-task-attempt-1", "check-1", _active_controller={})
        self.assertNotIn(active.KEY, self.store.state)

    def pulse_frequently(self):
        self.sup.__exit__(None, None, None)
        self.sup = Supervisor(self.store)
        self.sup._pulse_interval = 1
        self.sup.__enter__()
        self.assertTrue(self.sup.worker_started.wait(2))
        self.scheduler.supervisor = self.gateway.supervisor = self.sup

    def test_heartbeat_progress_during_startup_and_model_request(self):
        self.pulse_frequently()
        from test_supervised_safe_qwen import wire
        self.responses = [wire(self.submit)]
        original = harness.Gateway.request
        samples = []
        def request(gateway, method, path, *args, **kwargs):
            self.assertFalse(self.store.mutex._is_owned(), "Network operation held WAL mutex")
            if path.startswith("/upstream/") or path == "/v1/chat/completions":
                def count():
                    return sum((r.get("transition") or {}).get("event") == "controller_heartbeat" for r in self.store.records)
                before = count()
                time.sleep(2.25)
                self.assertGreater(count(), before)
                samples.append(path)
            return original(gateway, method, path, *args, **kwargs)
        with patch("harness.Gateway.request", new=request): self.run_first()
        self.assertEqual(len(samples), 2)
        self.assertIsNone(self.sup.failure)

    def test_heartbeat_uncertainty_discards_inflight_response_and_prevents_edit(self):
        self.pulse_frequently()
        append = self.store.append
        def uncertain(event, **value):
            row = value.get("transition") or {}
            hit = (threading.get_ident() == self.sup.thread.ident and row.get("event") == "controller_heartbeat"
                   and any(r["kind"] == "MODEL_INTENT" for r in self.store.state.get(active.KEY, {}).get("events", [])))
            result = append(event, **value)
            if hit: raise OSError("Uncertain heartbeat fsync")
            return result
        # Poll the controller's native cancellation event while fake HTTP waits.
        def wait_failure():
            end = time.monotonic() + 5
            while not self.store._repair_write_failed and time.monotonic() < end: time.sleep(0.01)
            self.assertTrue(self.store._repair_write_failed)
        self.on_request = wait_failure
        import supervised_safe_qwen as adapter
        with patch.object(self.store, "append", side_effect=uncertain), self.assertRaises(adapter.AdapterError): self.run_first()
        self.assertEqual((self.repo.root / "calculator.py").read_bytes(), self.before)
        self.assertTrue(self.store._repair_write_failed)
        self.assertIsNotNone(self.sup.failure)
        self.assertEqual(self.reload().state[active.KEY]["events"][-1]["kind"], "MODEL_INTENT")
