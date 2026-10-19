"""Real Windows worker, inert model process and deterministic fake HTTP only.

No live Gateway/server is used. Model bytes and replies are fixtures; native
worker/model/parent PID, creation, ownership and cleanup observations are real.
"""
import copy
import os
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import durable
import harness
import manager
import safe_qwen_active_recovery as active
import supervised_safe_qwen as adapter
import terminal_run
import winprocess
import work_units
from supervision import Supervisor
from work_unit_scheduler import WorkUnitScheduler
import test_supervised_safe_qwen_gateway as fixtures

REAL_TERMINAL = durable.Store.commit_terminal
REAL_EXECUTE = WorkUnitScheduler.execute_attempt


@unittest.skipUnless(os.name == "nt", "Real Windows handles and Job containment required")
class ActiveFirstTests(unittest.TestCase):
    stop_fixture = fixtures.OwnedGatewayTests.stop_fixture
    servers = fixtures.OwnedGatewayTests.servers

    def setUp(self):
        fixtures.OwnedGatewayTests.setUp(self)
        self.sup.__exit__(None, None, None)
        # A NEW ledger, selected before any attempt. The legacy fixture ledger
        # is not reused, upgraded or adopted by the recovery operation.
        self.store = durable.Store(self.store.directory.parent, "active-first")
        parent = terminal_run.fixed_unit("Implement multiply", self.cfg, self.executor, ["check-1"])
        parent["limits"]["max_attempts"] = 2
        manager.create_run(self.store, self.fixture.repo, "Implement multiply", [self.fixture.command], self.cfg,
                           criteria=["multiply"], work_units=[parent],
                           verifier_registry={"check-1": {"argv": self.fixture.command, "timeout_seconds": 30}},
                           allow=["calculator.py"], forbid=["test_calculator.py"], executor=self.executor)
        section = copy.deepcopy(self.store.state["work_units"])
        unit = section["units"][parent["unit_id"]]
        work_units.transition_unit(unit, "VALIDATED"); work_units.transition_unit(unit, "READY")
        work_units.seal_unit_state(unit); work_units._reseal_section(section)
        self.store.commit(work_units=section)
        self.sup = Supervisor(self.store)
        self.sup.__enter__()
        self.assertTrue(self.sup.worker_started.wait(2))
        self.addCleanup(self.close_supervisor)
        self.repo = manager.ScopedRepository(self.fixture.repo.root, self.cfg, durable.DurableLog(self.store), self.store)
        self.gateway.supervisor = self.sup
        self.scheduler = WorkUnitScheduler(self.store, self.repo, supervisor=self.sup, agents=SimpleNamespace(executor=self.executor))
        self.worker_spec = terminal_run.recovery_worker_spec(parent, self.cfg)

    def close_supervisor(self):
        if self.store._repair_write_failed:
            error = RuntimeError("Expected uncertain-WAL stop")
            self.sup.__exit__(RuntimeError, error, None)
            self.assertFalse(self.sup.thread.is_alive())
        else:
            self.sup.__exit__(None, None, None)

    def run_first(self, **kwargs):
        return self.scheduler.observe_safe_qwen_recovery_first("safe-qwen-task", gateway=self.gateway, **kwargs)

    def reload(self):
        store = durable.Store(self.store.directory.parent, self.store.directory.name)
        store.load()
        return store

    def test_valid_first_attempt_is_single_unverified_execution(self):
        result = self.run_first()
        data = result["active_recovery"]
        self.assertEqual(data["phase"], "COMPLETE")
        self.assertEqual(data["result"]["outcome"], "executor_succeeded_unverified")
        self.assertEqual(data["identity"]["worker"]["limits"]["max_attempts"], 1)
        self.assertIn("return a * b", (self.repo.root / "calculator.py").read_text())
        loaded = self.reload()
        unit = loaded.state["work_units"]["units"]["safe-qwen-task"]
        self.assertEqual(unit["spec"]["limits"]["max_attempts"], 2)
        self.assertEqual(len(unit["attempts"]), 1)
        self.assertEqual(unit["status"], "EXECUTING")
        self.assertIsNone(unit["attempts"][0]["verifier_result"])
        self.assertEqual(unit["attempts"][0]["stage"], "ai_executor_complete_unverified")
        for key in ("safe_qwen_recovery_identity", "repair_authorizations", "repair_dispatch", "repair_verification", "terminal_acceptance"):
            self.assertNotIn(key, loaded.state)
        self.assertIsNone(loaded.state["last_verified_checkpoint"])
        self.assertIsNone(loaded._safe_qwen_active_controller)
        self.assertEqual(data, loaded.state[active.KEY])
        self.assertIsNone(winprocess.identity(data["process"]["pid"]))
        self.assertEqual(data["result"]["cleanup_proof"], {"pid": data["process"]["pid"], "exit_code": 0, "empty_job": True})

    def test_wal_precedes_gateway_native_release_and_exact_hash_mutation(self):
        request, launch, resume, replace = harness.Gateway.request, winprocess.ControllerVerifier.launch, winprocess.ControllerVerifier.resume, os.replace
        launches, resumes, edits = [], [], []
        def http(gateway, method, path, payload=None, **kwargs):
            state = self.reload().state
            self.assertIn(active.KEY, state)
            if path == "/v1/chat/completions":
                event = state[active.KEY]["events"][-1]
                self.assertEqual(event["kind"], "MODEL_INTENT")
                self.assertEqual(event["data"]["request_sha256"], adapter.sha(adapter._json(payload)))
            return request(gateway, method, path, payload, **kwargs)
        def created(native, command, cwd, streams):
            self.assertEqual(self.reload().state[active.KEY]["phase"], "INTENT")
            result = launch(native, command, cwd, streams); launches.append(native.pid); return result
        def released(native, identity):
            data = self.reload().state[active.KEY]
            self.assertEqual(data["phase"], "RELEASED")
            self.assertEqual(data["process"]["pid"], native.pid)
            self.assertEqual(data["process"]["identity"], identity)
            self.assertTrue(data["process"]["membership"])
            resumes.append(native.pid); return resume(native, identity)
        def changed(source, target):
            if Path(target) == self.repo.root / "calculator.py":
                row = self.reload().state[active.KEY]["events"][-1]
                self.assertEqual(row["kind"], "EDIT_INTENT")
                self.assertEqual(row["data"]["before_sha256"], adapter.sha(Path(target).read_bytes()))
                self.assertEqual(row["data"]["after_sha256"], adapter.sha(Path(source).read_bytes()))
                edits.append(str(target))
            return replace(source, target)
        with patch("harness.Gateway.request", new=http), patch.object(winprocess.ControllerVerifier, "launch", created), \
                patch.object(winprocess.ControllerVerifier, "resume", released), patch("os.replace", new=changed):
            self.run_first()
        self.assertEqual(len(launches), 1); self.assertEqual(resumes, launches); self.assertEqual(len(edits), 1)

    def test_no_replay_grant_verifier_checkpoint_or_second_attempt(self):
        self.run_first()
        self.sup.__exit__(None, None, None)
        loaded = self.reload()
        before = (self.store.directory / "events.jsonl").read_bytes()
        scheduler = WorkUnitScheduler(loaded, self.repo, supervisor=Supervisor(loaded))
        self.assertEqual(scheduler.reconciliation_stop.reason, "SAFEQWEN_ACTIVE_REVIEW_REQUIRED")
        with self.assertRaises(durable.DurableError):
            scheduler.observe_safe_qwen_recovery_first("safe-qwen-task", gateway=self.gateway)
        with self.assertRaises(adapter.AdapterError):
            adapter.run_owned_gateway_worker(self.executor, self.worker_spec, self.repo, gateway=self.gateway,
                                            audit_directory=self.root / "forbidden")
        section = loaded.state["work_units"]
        for number in (1, 2, 3):
            with self.assertRaises(manager.Stop): REAL_EXECUTE(scheduler, section["units"]["safe-qwen-task"], section, number)
        with self.assertRaises(manager.Stop): scheduler.revalidate_milestone(section)
        with self.assertRaises(durable.DurableError): REAL_TERMINAL(loaded, "forbidden", controller=loaded._controller_token)
        with self.assertRaises(durable.DurableError): loaded._begin_repair_recovery(scheduler, "safe-qwen-task")
        self.assertEqual((self.store.directory / "events.jsonl").read_bytes(), before)

    def test_frozen_v1_is_not_upgraded(self):
        identity = self.store.select_safe_qwen_recovery_identity(self.scheduler, "safe-qwen-task")
        with self.assertRaises(durable.DurableError): self.run_first()
        self.assertEqual(self.reload().state["safe_qwen_recovery_identity"], identity)
        self.assertEqual(self.events, [])

    def test_wrong_gateway_config_or_session_refuses_before_model(self):
        with self.assertRaises(durable.DurableError):
            self.scheduler.observe_safe_qwen_recovery_first("safe-qwen-task", gateway=object())
        self.executor.config["roles"]["code"] = "foreign"
        with self.assertRaises((durable.DurableError, ValueError)): self.run_first()
        self.assertNotIn(active.KEY, self.store.state); self.assertEqual(self.events, [])

    def test_json_cannot_select_or_persist_an_active_identity(self):
        for action in (lambda: self.store.commit(**{active.KEY: {"schema_version": 2}}),
                       lambda: self.store.append("state_committed", state=self.store.state, transition=None, **{active.MARKER: {"event": "INTENT"}}),
                       lambda: active.run({}, "safe-qwen-task", gateway=self.gateway)):
            with self.assertRaises((durable.DurableError, AttributeError)): action()
        self.assertEqual(self.events, [])

    def fault(self, kind, after=True):
        append = self.store.append
        launches, resumes = [], []
        launch, resume = winprocess.ControllerVerifier.launch, winprocess.ControllerVerifier.resume
        def write(event, **value):
            marker = value.get(active.MARKER)
            hit = marker is not None and marker["event"] == kind
            if hit and not after: raise OSError("before required WAL")
            result = append(event, **value)
            if hit: raise OSError("uncertain required WAL")
            return result
        def created(native, *args):
            result = launch(native, *args); launches.append(native.pid); return result
        def released(native, identity):
            resumes.append(native.pid); return resume(native, identity)
        with patch.object(self.store, "append", side_effect=write), patch.object(winprocess.ControllerVerifier, "launch", created), \
                patch.object(winprocess.ControllerVerifier, "resume", released), self.assertRaises((OSError, adapter.AdapterError)):
            self.run_first()
        self.assertTrue(self.store._repair_write_failed)
        self.assertEqual((self.repo.root / "calculator.py").read_bytes(), self.before)
        for pid in launches: self.assertIsNone(winprocess.identity(pid))
        before = (self.store.directory / "events.jsonl").read_bytes()
        with self.assertRaises(durable.DurableError): self.store.commit(status="HUMAN_ACTION_REQUIRED")
        self.assertEqual((self.store.directory / "events.jsonl").read_bytes(), before)
        if not after:
            with self.assertRaises(durable.DurableError): self.reload()
        else:
            loaded = self.reload()
            self.assertIsNone(loaded._safe_qwen_active_controller)
            self.assertEqual(loaded.state[active.KEY]["events"][-1]["kind"], kind)
        return launches, resumes

    def test_crash_before_initial_intent_never_launches(self):
        launches, resumes = self.fault("INTENT", after=False)
        self.assertEqual((launches, resumes, self.events), ([], [], []))

    def test_uncertain_initial_intent_never_launches(self):
        launches, resumes = self.fault("INTENT")
        self.assertEqual((launches, resumes, self.events), ([], [], []))

    def test_uncertain_native_release_never_resumes(self):
        launches, resumes = self.fault("RELEASE")
        self.assertEqual(len(launches), 1); self.assertEqual(resumes, [])
        self.assertFalse(any(e[1] == "/v1/chat/completions" for e in self.events))

    def test_uncertain_model_intent_prevents_model_post(self):
        self.fault("MODEL_INTENT")
        self.assertFalse(any(e[1] == "/v1/chat/completions" for e in self.events))

    def test_uncertain_edit_intent_prevents_mutation_and_more_requests(self):
        self.fault("EDIT_INTENT")
        self.assertEqual(sum(e[1] == "/v1/chat/completions" for e in self.events), 1)

    def test_forbidden_edit_has_no_mutation_or_edit_authority(self):
        from test_supervised_safe_qwen import op, wire
        self.responses = [wire(op("edit_file", path="test_calculator.py", before_sha256="0" * 64, old_text="x", new_text="y")), wire(self.submit)]
        data = self.run_first()["active_recovery"]
        self.assertEqual(data["result"]["changed_paths"], [])
        self.assertFalse(any(row["kind"].startswith("EDIT_") for row in data["events"]))

    def test_cancel_during_request_does_not_apply_response(self):
        import threading
        event = threading.Event()
        self.on_request = event.set
        with self.assertRaises(adapter.AdapterError): self.run_first(cancel_event=event)
        self.assertEqual((self.repo.root / "calculator.py").read_bytes(), self.before)
        data = self.reload().state[active.KEY]
        self.assertEqual(data["result"]["outcome"], "executor_failed_unverified")
        self.assertIsNone(winprocess.identity(data["process"]["pid"]))

    def test_expired_budget_refuses_before_gateway(self):
        with patch.object(self.scheduler, "remaining_task_budget", return_value=0), self.assertRaises(durable.DurableError): self.run_first()
        self.assertNotIn(active.KEY, self.store.state); self.assertEqual(self.events, [])

    def test_candidate_drift_after_initial_wal_refuses_even_gateway_setup(self):
        append = self.store.append
        def drift(event, **fields):
            result = append(event, **fields)
            if fields.get(active.MARKER, {}).get("event") == "INTENT":
                (self.repo.root / "calculator.py").write_bytes(b"# manual change after selection\n")
            return result
        with patch.object(self.store, "append", side_effect=drift), self.assertRaises((durable.DurableError, adapter.AdapterError)):
            self.run_first()
        self.assertEqual(self.events, [])
        self.assertEqual(self.reload().state[active.KEY]["phase"], "INTENT")

    def test_environment_drift_before_selection_refuses_all_dispatch(self):
        original = durable.current_fingerprint
        def drift(*args, **kwargs):
            value = copy.deepcopy(original(*args, **kwargs))
            value["runtime"]["python"]["sha256"] = "0" * 64
            return value
        with patch("durable.current_fingerprint", new=drift), self.assertRaises(durable.DurableError): self.run_first()
        self.assertNotIn(active.KEY, self.store.state); self.assertEqual(self.events, [])

    def test_hung_worker_deadline_drains_native_descendant(self):
        import threading
        import time
        from test_supervised_safe_qwen import SupervisedSafeQwenTests
        marker = self.root / "active-descendant.json"
        # Five seconds leaves almost no time after the real WAL/native release
        # sequence. Bound this fixture at 15s, including startup; never reset it.
        timeout = 15
        body = ("p=subprocess.Popen([sys.executable,'-I','-B','-c','import time;time.sleep(60)'],close_fds=True)\n"
                "from winprocess import identity\n"
                "observed={'worker':{'pid':os.getpid(),'identity':identity(os.getpid())},"
                "'descendant':{'pid':p.pid,'identity':identity(p.pid)},'spawned_clock':time.monotonic()}\n"
                f"Path({str(marker)!r}).write_text(json.dumps(observed))\n"
                "time.sleep(60)\n")
        observed, failures = [], []
        stop = threading.Event()
        def observe_live():
            try:
                while not stop.wait(0.01):
                    if not marker.exists(): continue
                    try: row = json.loads(marker.read_text())
                    except json.JSONDecodeError: continue
                    identities = {key: winprocess.identity(row[key]["pid"]) for key in ("worker", "descendant")}
                    observed.append((row, identities, time.monotonic()))
                    return
            except Exception as exc: failures.append(exc)
        observer = threading.Thread(target=observe_live)
        observer.start()
        try:
            with SupervisedSafeQwenTests.fault_worker(self, body), self.assertRaises(adapter.AdapterError) as caught:
                self.run_first(timeout_seconds=timeout)
        finally:
            stop.set(); observer.join(5)
        self.assertFalse(observer.is_alive())
        self.assertEqual(failures, [])
        self.assertTrue(marker.exists())
        self.assertEqual(len(observed), 1)
        row, identities, observed_clock = observed[0]
        self.assertEqual(row, json.loads(marker.read_text()))
        self.assertNotEqual(row["worker"]["pid"], row["descendant"]["pid"])
        for key in ("worker", "descendant"):
            self.assertIsNotNone(identities[key])
            self.assertEqual(identities[key], row[key]["identity"])
            self.assertIsNone(winprocess.identity(row[key]["pid"]))
        evidence = caught.exception.evidence
        self.assertEqual(evidence["process"], row["worker"])
        self.assertTrue(evidence["identity_verified"])
        self.assertTrue(evidence["execution_started"])
        self.assertEqual(evidence["termination_basis"], "controller_deadline")
        self.assertEqual(evidence["observed_exit_code"], 137)
        self.assertEqual(evidence["cleanup"], "observed_empty")
        data = self.reload().state[active.KEY]
        self.assertAlmostEqual(data["identity"]["deadline"] - data["identity"]["started_clock"], timeout)
        self.assertLessEqual(data["identity"]["started_clock"], row["spawned_clock"])
        self.assertLessEqual(row["spawned_clock"], observed_clock)
        self.assertLess(observed_clock, data["identity"]["deadline"])
        self.assertEqual(data["phase"], "COMPLETE")
        self.assertEqual(data["result"]["outcome"], "executor_failed_unverified")
        self.assertEqual(data["result"]["changed_paths"], [])
        self.assertGreater(data["result"]["elapsed_seconds"], timeout)

    def test_candidate_drift_during_model_request_refuses_tool(self):
        self.on_request = lambda: (self.repo.root / "calculator.py").write_bytes(b"# foreign candidate\n")
        with self.assertRaises(adapter.AdapterError) as caught: self.run_first()
        self.assertNotIn(b"return a * b", (self.repo.root / "calculator.py").read_bytes())
        self.assertFalse(any(r["kind"] == "EDIT_INTENT" for r in self.reload().state[active.KEY]["events"]))
        self.assertFalse(caught.exception.evidence["scope_verified"])
        self.assertIsNone(winprocess.identity(caught.exception.evidence["process"]["pid"]))

    def test_config_and_session_drift_during_request_stop_before_edit(self):
        self.on_request = lambda: setattr(self.gateway, "supervisor", Supervisor(self.store))
        original = self.gateway.supervisor
        try:
            with self.assertRaises(adapter.AdapterError) as caught: self.run_first()
            self.assertEqual((self.repo.root / "calculator.py").read_bytes(), self.before)
            self.assertIsNone(winprocess.identity(caught.exception.evidence["process"]["pid"]))
        finally:
            self.gateway.supervisor = original

    def test_closed_schema_and_hash_valid_identity_forgery_refuse(self):
        self.run_first()
        state = self.reload().state
        variants = [("schema_version", True), ("schema_version", 1), ("mode", "SAFEQWEN_IDENTITY_ONLY"), ("foreign", 1)]
        for key, value in variants:
            changed = copy.deepcopy(state); changed[active.KEY][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(durable.DurableError): active.validate(changed)
        for key, value in (("run_id", "foreign"), ("attempt_id", "safe-qwen-task-attempt-2"),
                           ("model_pin_sha256", "0" * 64), ("config_sha256", "0" * 64),
                           ("session", {}), ("worker_sha256", "0" * 64)):
            changed = copy.deepcopy(state); changed[active.KEY]["identity"][key] = value
            with self.subTest(key=key), self.assertRaises(durable.DurableError): active.validate(changed)
        records = copy.deepcopy(self.store.records)
        row = next(r for r in records if r.get(active.MARKER, {}).get("event") == "RELEASE")
        row.pop(active.MARKER)
        with self.assertRaises(durable.DurableError): active.validate_history(records, self.store.read_evidence, reservation_exists=True)

    def test_torn_tail_or_missing_reservation_never_recovers_authority(self):
        self.run_first(); self.sup.__exit__(None, None, None)
        wal = self.store.directory / "events.jsonl"
        original = wal.read_bytes()
        with wal.open("ab") as stream: stream.write(b'{"seq')
        with self.assertRaises(durable.DurableError): self.reload()
        self.assertEqual(wal.read_bytes(), original + b'{"seq')
        wal.write_bytes(original)
        reservation = self.store.directory / active.RESERVATION
        saved = reservation.read_bytes()
        reservation.write_bytes(b"{}\n")
        try:
            with self.assertRaises(durable.DurableError): self.reload()
        finally:
            reservation.write_bytes(saved)

    def test_duplicate_reentrant_call_cannot_create_another_execution(self):
        calls = []
        def duplicate():
            with self.assertRaises(durable.DurableError): self.run_first()
            calls.append(True)
        self.on_request = duplicate
        self.run_first()
        self.assertEqual(calls, [True, True])
        self.assertEqual(len(self.store.state["work_units"]["units"]["safe-qwen-task"]["attempts"]), 1)

    def test_concurrent_controllers_persist_exactly_one_attempt(self):
        import threading
        barrier = threading.Barrier(2)
        results, failures = [], []
        def start():
            barrier.wait(timeout=5)
            try: results.append(self.run_first())
            except BaseException as exc: failures.append(exc)
        threads = [threading.Thread(target=start) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(40)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(results), 1)
        self.assertEqual(len(failures), 1); self.assertIsInstance(failures[0], durable.DurableError)
        self.assertEqual(len(self.store.state["work_units"]["units"]["safe-qwen-task"]["attempts"]), 1)

    def test_actual_controller_death_cannot_relaunch_worker_or_descendant(self):
        import sys
        import tempfile
        import time
        root = Path(__file__).resolve().parents[1]
        marker, selected = self.root / "death-native.json", self.root / "death-selected.json"
        scratch = self.root / "crash-fixtures"; scratch.mkdir()
        body = ("p=subprocess.Popen([sys.executable,'-I','-B','-c','import time;time.sleep(60)'],close_fds=True)\n"
                f"Path({str(marker)!r}).write_text(json.dumps({{'worker':os.getpid(),'descendant':p.pid}}))\n"
                "time.sleep(60)\n")
        script = self.root / "active-controller-crash.py"
        script.write_text("import os,sys,json,tempfile\nfrom pathlib import Path\n"
                          f"sys.path[:0]=[{str(root)!r},{str(root/'tests')!r}]\n"
                          f"tempfile.tempdir={str(scratch)!r}\n"
                          "from test_safe_qwen_active_first import ActiveFirstTests\n"
                          "from test_supervised_safe_qwen import SupervisedSafeQwenTests\n"
                          "t=ActiveFirstTests('runTest');t.setUp()\n"
                          f"Path({str(selected)!r}).write_text(json.dumps({{'directory':str(t.store.directory)}}))\n"
                          f"with SupervisedSafeQwenTests.fault_worker(t,{body!r}): t.run_first(timeout_seconds=30)\n")
        native = winprocess.ControllerVerifier("Local\\active-first-death-" + self.root.name)
        try:
            with tempfile.TemporaryFile() as inp, tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
                native.launch([str(Path(sys.executable).resolve()), "-I", "-B", str(script)], self.root, (inp, out, err))
                native.resume(native.identity)
                end = time.monotonic() + 20
                while not marker.exists() and time.monotonic() < end and native.wait(0) is None: time.sleep(0.02)
                err.seek(0)
                self.assertTrue(marker.exists(), err.read(8192).decode(errors="replace"))
                identities = json.loads(marker.read_text())
                # Retain the OUTER Job without terminating it: the worker's OWN
                # kill-on-close Job must prove cleanup after controller death.
                self.assertTrue(native.k.TerminateProcess(native.process, 137))
                self.assertEqual(native.wait(5), 137)
                end = time.monotonic() + 5
                while any(winprocess.identity(pid) is not None for pid in identities.values()) and time.monotonic() < end: time.sleep(0.01)
                for pid in identities.values(): self.assertIsNone(winprocess.identity(pid))
        finally:
            # Separately drain the inert fake MODEL process owned by the outer
            # fixture Job. This is not worker cleanup or model inference.
            native.terminate_job(); native.drain(10); native.close()
        directory = Path(json.loads(selected.read_text())["directory"])
        wal = directory / "events.jsonl"; before = wal.read_bytes()
        loaded = durable.Store(directory.parent, directory.name)
        try: loaded.load()
        except durable.DurableError:
            # A concurrently interrupted Supervisor write is an ambiguous tail.
            self.assertFalse(before.endswith(b"\n"), "Complete history must remain replay-valid after controller death")
            self.assertEqual(wal.read_bytes(), before)
            return
        self.assertIsNone(loaded._safe_qwen_active_controller)
        self.assertEqual(loaded.state[active.KEY]["phase"], "RELEASED")
        scheduler = WorkUnitScheduler(loaded, self.repo, supervisor=Supervisor(loaded))
        with self.assertRaises(durable.DurableError):
            scheduler.observe_safe_qwen_recovery_first("safe-qwen-task", gateway=self.gateway)
        self.assertEqual(wal.read_bytes(), before)

    def test_uncertain_response_prevents_tool_dispatch(self):
        self.fault("MODEL_RESPONSE")
        self.assertEqual(sum(e[1] == "/v1/chat/completions" for e in self.events), 1)

    def test_uncertain_completion_keeps_result_historical_without_relaunch(self):
        append = self.store.append
        def uncertain(event, **fields):
            result = append(event, **fields)
            if fields.get(active.MARKER, {}).get("event") == "COMPLETE": raise OSError("Uncertain completion")
            return result
        with patch.object(self.store, "append", side_effect=uncertain), self.assertRaises(adapter.AdapterError): self.run_first()
        self.assertTrue(self.store._repair_write_failed)
        loaded = self.reload()
        self.assertEqual(loaded.state[active.KEY]["phase"], "COMPLETE")
        self.assertIsNone(loaded._safe_qwen_active_controller)
        with self.assertRaises(durable.DurableError): self.run_first()
