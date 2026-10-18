"""Real Windows workers/native verification; fake Gateway, zero inference."""
import copy
import json
import os
import unittest
from unittest.mock import patch

import durable
import harness
import safe_qwen_active_recovery as active
import safe_qwen_active_dispatch as dispatch
import test_safe_qwen_active_allocation as fixtures
import test_safe_qwen_integration as integration
from test_supervised_safe_qwen import wire


@unittest.skipUnless(os.name == "nt", "Real Windows native evidence required")
class ActiveDispatchTests(unittest.TestCase):
    prepare_timeout_fixture = fixtures.ActiveAllocationTests.prepare_timeout_fixture
    stop_fixture = fixtures.ActiveAllocationTests.stop_fixture
    servers = fixtures.ActiveAllocationTests.servers
    close_supervisor = fixtures.ActiveAllocationTests.close_supervisor
    reload = fixtures.ActiveAllocationTests.reload

    def setUp(self):
        prepared = integration.IntegrationTests.prepared
        def bounded(fixture, *args, **kwargs):
            # Explicit finite shared budget for TWO executions, pinned before
            # either run/identity exists; never extend an active deadline.
            fixture.cfg["timeout_seconds"] = 180
            fixture.data["unit_timeout_seconds"] = 180
            if self._testMethodName == "test_heartbeat_advances_during_attempt_two_model_wait":
                fixture.cfg["request_timeout_seconds"] = 30
            fixture.path.write_text(json.dumps(fixture.cfg))
            fixture.policy_path.write_text(json.dumps(fixture.data))
            return prepared(fixture, *args, **kwargs)
        with patch.object(integration.IntegrationTests, "prepared", bounded): fixtures.ActiveAllocationTests.setUp(self)
        import subprocess
        popen = subprocess.Popen
        def inert_server(argv, *args, **kwargs):
            if type(argv) is list and argv[-1] == "import time;time.sleep(60)":
                argv = [*argv[:-1], "import time;time.sleep(240)"]
            return popen(argv, *args, **kwargs)
        item = patch("subprocess.Popen", side_effect=inert_server)
        item.start(); self.addCleanup(item.stop)
        self.responses = [wire(self.submit), wire(self.edit), wire(self.submit)]

    def run_dispatch(self):
        return self.scheduler.dispatch_safe_qwen_repair_recovery("safe-qwen-task", gateway=self.gateway, timeout_seconds=180)

    def test_one_second_worker_repairs_file_and_stops_unverified(self):
        import winprocess
        launch = winprocess.ControllerVerifier.launch
        launches, packets = [], []
        def launched(native, *args):
            state = self.reload().state
            if len(launches) == 2:
                self.assertEqual(state[active.KEY]["dispatch"]["phase"], "INTENT")
                self.assertEqual(next(iter(state["repair_authorizations"].values()))["state"], "CONSUMED")
            result = launch(native, *args); launches.append(native.pid); return result
        request = harness.Gateway.request
        def observed(gateway, method, path, payload=None, **kwargs):
            self.assertFalse(self.store.mutex._is_owned())
            if path == "/v1/chat/completions":
                packet = json.loads(payload["messages"][1]["content"])
                packets.append(packet)
                state = self.reload().state
                worker = state[active.KEY].get("dispatch") or state[active.KEY]
                self.assertEqual(worker["events"][-1]["kind"], "MODEL_INTENT")
            return request(gateway, method, path, payload, **kwargs)
        with patch.object(winprocess.ControllerVerifier, "launch", launched), patch.object(harness.Gateway, "request", observed):
            result = self.run_dispatch()
        state = self.reload().state; d = state[active.KEY]; execution = result["dispatch"]
        self.assertEqual((d["schema_version"], d["mode"]), (6, dispatch.MODE))
        self.assertEqual(execution["phase"], "COMPLETE")
        evidence = execution["result"]["evidence"]
        self.assertEqual(execution["result"]["outcome"], "executor_succeeded_unverified",
                         {k: evidence.get(k) for k in ("protocol_failure", "termination_basis", "transport", "diagnostics")})
        self.assertEqual(len(launches), 3)  # A1 worker, A1 verifier, A2 worker.
        self.assertTrue(all(winprocess.identity(pid) is None for pid in launches))
        self.assertEqual(execution["process"]["pid"], launches[2])
        self.assertIn("return a * b", (self.repo.root / "calculator.py").read_text())
        self.assertIsNone(packets[0]["repair_evidence"])
        self.assertEqual(packets[1]["repair_evidence"], execution["repair_evidence"])
        self.assertEqual(packets[1]["max_attempts"], 1)
        self.assertFalse(execution["repair_evidence"]["trusted"])
        self.assertEqual(execution["repair_evidence"]["authority"], "none")
        unit = state["work_units"]["units"]["safe-qwen-task"]
        self.assertEqual(len(unit["attempts"]), 2)
        self.assertEqual(unit["status"], "EXECUTING")
        self.assertEqual(unit["attempts"][1]["stage"], "ai_executor_complete_unverified")
        self.assertIsNone(unit["attempts"][1]["verifier_result"])
        self.assertIsNone(unit["result"])
        self.assertIsNone(state["last_verified_checkpoint"])
        self.assertEqual(state["verified_progress"], [])
        grant = next(iter(state["repair_authorizations"].values()))
        prior = self.store.records[grant["consumption"]["grant_wal"]["sequence"] - 1]["state"]
        self.assertEqual(unit["attempts"][0], prior["work_units"]["units"][unit["unit_id"]]["attempts"][0])
        self.assertEqual(len(packets), 3)

    def test_passing_first_attempt_does_not_allocate_or_dispatch(self):
        self.responses = [wire(self.edit), wire(self.submit)]
        result = self.run_dispatch()
        self.assertIsNone(result["authorization"])
        state = self.reload().state
        self.assertNotIn("repair_authorizations", state)
        self.assertIsNone(state[active.KEY]["dispatch"])
        self.assertEqual(len(state["work_units"]["units"]["safe-qwen-task"]["attempts"]), 1)

    def test_second_executor_failure_is_terminal(self):
        self.responses = [wire(self.submit)] + [wire("malformed")] * 4
        result = self.run_dispatch()
        self.assertEqual(result["dispatch"]["result"]["outcome"], "executor_failed_unverified")
        unit = self.reload().state["work_units"]["units"]["safe-qwen-task"]
        self.assertEqual(unit["status"], "UNIT_FAILED")
        self.assertEqual(len(unit["attempts"]), 2)
        self.assertEqual(unit["attempts"][1]["outcome"], "error")

    def test_heartbeat_advances_during_attempt_two_model_wait(self):
        import time
        sampled = []
        def delayed():
            self.assertFalse(self.store.mutex._is_owned())
            if self.store.state[active.KEY]["dispatch"] is not None and not sampled:
                def count(): return sum((r.get("transition") or {}).get("event") == "controller_heartbeat" for r in self.store.records)
                before = count()
                time.sleep(20)  # Below the preselected 30s request timeout.
                self.assertGreater(count(), before)
                sampled.append(True)
        self.on_request = delayed
        result = self.run_dispatch()
        self.assertEqual(sampled, [True])
        self.assertEqual(result["dispatch"]["result"]["outcome"], "executor_succeeded_unverified")
        self.assertIsNone(self.sup.failure)

    def test_no_replay_duplicate_dispatch_third_attempt_or_verification(self):
        import manager
        import supervision
        import work_unit_scheduler as scheduler_module
        import test_safe_qwen_active_first as first
        owners = []
        execute = dispatch.run
        def single(owner):
            owners.append(owner)
            result = execute(owner)
            with self.assertRaises(durable.DurableError): execute(owner)
            return result
        with patch.object(dispatch, "run", side_effect=single): self.run_dispatch()
        self.sup.__exit__(None, None, None)
        loaded = self.reload()
        self.assertIsNone(loaded._safe_qwen_active_controller)
        before = (loaded.directory / "events.jsonl").read_bytes()
        scheduler = scheduler_module.WorkUnitScheduler(loaded, self.repo, supervisor=supervision.Supervisor(loaded))
        section = scheduler.get_section(); unit = section["units"]["safe-qwen-task"]
        for operation in (lambda: execute(owners[0]), lambda: execute(loaded.state[active.KEY]),
                          lambda: scheduler.dispatch_safe_qwen_repair_recovery(unit["unit_id"], gateway=self.gateway),
                          lambda: first.REAL_EXECUTE(scheduler, unit, section, 3),
                          lambda: scheduler.invoke_native_verifier(unit["unit_id"], unit["current_attempt"], "check-1"),
                          lambda: first.REAL_TERMINAL(loaded, "forbidden")):
            with self.assertRaises((durable.DurableError, manager.Stop)): operation()
        import work_units
        with self.assertRaises(work_units.WorkUnitError): work_units.create_attempt(copy.deepcopy(unit), "safe-qwen-task-attempt-3")
        self.assertEqual((loaded.directory / "events.jsonl").read_bytes(), before)

    def test_controller_approves_edit_and_native_identity_before_resume(self):
        import winprocess
        from pathlib import Path
        resume, replace = winprocess.ControllerVerifier.resume, os.replace
        approved = []
        def resumed(native, identity):
            state = self.reload().state
            d = state[active.KEY]["dispatch"]
            if d is not None:
                self.assertEqual(d["process"]["pid"], native.pid)
                self.assertEqual(d["process"]["identity"], identity)
                self.assertTrue(d["process"]["membership"])
                self.assertEqual(d["events"][-1]["kind"], "RELEASE")
                approved.append("release")
            return resume(native, identity)
        def changed(source, target):
            if Path(target) == self.repo.root / "calculator.py":
                d = self.reload().state[active.KEY]["dispatch"]
                self.assertEqual(d["events"][-1]["kind"], "EDIT_INTENT")
                import hashlib
                self.assertEqual(hashlib.sha256(Path(target).read_bytes()).hexdigest(), d["events"][-1]["data"]["before_sha256"])
                self.assertEqual(hashlib.sha256(Path(source).read_bytes()).hexdigest(), d["events"][-1]["data"]["after_sha256"])
                approved.append("edit")
            return replace(source, target)
        with patch.object(winprocess.ControllerVerifier, "resume", resumed), patch("os.replace", new=changed): self.run_dispatch()
        self.assertEqual(approved, ["release", "edit"])

    def test_scope_policy_refuses_unauthorized_model_edit(self):
        from test_supervised_safe_qwen import op
        bad = op("edit_file", path="test_calculator.py", before_sha256="0" * 64, old_text="x", new_text="y")
        original = (self.repo.root / "test_calculator.py").read_bytes()
        self.responses = [wire(self.submit)] + [wire(bad)] * 4
        result = self.run_dispatch()
        self.assertEqual(result["dispatch"]["result"]["outcome"], "executor_failed_unverified")
        self.assertEqual((self.repo.root / "test_calculator.py").read_bytes(), original)
        self.assertEqual((self.repo.root / "calculator.py").read_bytes(), self.before)
