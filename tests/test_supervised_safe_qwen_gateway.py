"""Owned transport qualification: exact Gateway, fake HTTP, inert native fixtures.

The Python fixture process is not a model server and does no inference. Native
PID/creation/parent/exit evidence is real; configured model bytes and HTTP replies
are synthetic. This does not qualify a live gateway/model deployment.
"""
import copy
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import unittest
import urllib.request
from types import SimpleNamespace
from unittest.mock import patch

import durable
import harness
import manager
import supervised_safe_qwen as adapter
import winprocess
import work_units
from supervision import Supervisor
from safe_qwen_worker import sha
import test_safe_qwen_integration as integration_tests
from test_supervised_safe_qwen import op, wire

REAL_REQUEST = harness.Gateway.request


@unittest.skipUnless(os.name == "nt", "Real native Windows process ownership required")
class OwnedGatewayTests(unittest.TestCase):
    def setUp(self):
        fixture = integration_tests.IntegrationTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        binary = str(Path(sys.executable).resolve())
        fixture.cfg["safe_qwen_runtime"]["code"]["binary"] = binary
        fixture.cfg["safe_qwen_gateway"]["binary"] = binary
        (fixture.resource / "gateway.pid").write_text(str(os.getpid()))
        (fixture.resource / "gateway.json").write_text('models:\n  operator-qwen:\n    cmd: >-\n      "' + binary +
                                                      '" --model "' + str(fixture.resource / "fixture-model.bin") + '"\n')
        self.executor, self.cfg = fixture.prepared()
        self.store, self.root = fixture.store, fixture.root
        self.sup = Supervisor(self.store)
        self.sup.__enter__()
        self.assertTrue(self.sup.worker_started.wait(2))
        self.addCleanup(self.sup.__exit__, None, None, None)
        self.log = durable.DurableLog(self.store)
        self.repo = manager.ScopedRepository(fixture.repo.root, self.cfg, self.log, self.store)
        self.gateway = harness.Gateway(self.cfg, self.log)
        self.gateway.supervisor = self.sup
        self.executor.bind_gateway(self.gateway)
        durable.bind_work_unit_executor(self.store, self.executor)
        section = copy.deepcopy(self.store.state["work_units"])
        unit = section["units"]["safe-qwen-task"]
        if unit["status"] == "PROPOSED":
            work_units.transition_unit(unit, "VALIDATED")
        if unit["status"] != "READY":
            work_units.transition_unit(unit, "READY")
        work_units.transition_unit(unit, "EXECUTING")
        work_units.create_attempt(unit, "safe-qwen-task-attempt-1")
        work_units.seal_unit_state(unit)
        work_units._reseal_section(section)
        self.store.commit(work_units=section)
        self.unit = copy.deepcopy(unit["spec"])
        self.audit = self.root / "owned-audit"
        self.submit = op("submit", summary="Candidate ready")
        self.before = (self.repo.root / "calculator.py").read_bytes()
        self.edit = op("edit_file", path="calculator.py", before_sha256=sha(self.before),
                       old_text="return 0", new_text="return a * b")
        self.responses = [wire(self.edit), wire(self.submit)]
        self.proc = None
        self.events = []
        self.on_request = None
        self.request_error = self.cleanup_error = None
        self.addCleanup(self.stop_fixture)
        self.patches = []
        def start(target, **kwargs):
            item = patch(target, **kwargs)
            self.patches.append(item)
            self.addCleanup(item.stop)
            return item.start()
        start("socket.socket", side_effect=AssertionError("No network"))
        start("socket.create_connection", side_effect=AssertionError("No network"))
        start("work_unit_scheduler.WorkUnitScheduler.execute_attempt", side_effect=AssertionError("No scheduler"))
        start("durable.Store._issue_repair_grant", side_effect=AssertionError("No grants"))
        start("durable.Store._consume_repair_grant", side_effect=AssertionError("No repair"))
        start("durable.Store.commit_terminal", side_effect=AssertionError("No acceptance"))
        start("harness.available_ram_gb", return_value=1024)
        start("harness.servers", new=self.servers)
        start("winprocess.servers", new=self.servers)
        owner = self
        def request(gateway, method, path, payload=None, timeout=10, **kwargs):
            owner.assertIs(gateway, owner.gateway)
            owner.events.append((method, path, copy.deepcopy(payload), timeout, kwargs))
            if path == "/running":
                return [{"model": owner.cfg["roles"]["code"], "state": "ready"}] if owner.servers() else []
            if path.startswith("/upstream/"):
                owner.assertIsNone(owner.proc)
                owner.proc = subprocess.Popen([sys.executable, "-I", "-B", "-c", "import time;time.sleep(60)"],
                                               cwd=owner.root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                               stderr=subprocess.DEVNULL, close_fds=True)
                return {}
            if path == "/api/models/unload":
                if owner.cleanup_error:
                    raise owner.cleanup_error
                owner.stop_fixture()
                return {}
            owner.assertEqual((method, path), ("POST", "/v1/chat/completions"))
            owner.assertGreater(timeout, 0)
            owner.assertLessEqual(timeout, owner.cfg["request_timeout_seconds"])
            owner.assertEqual(kwargs["max_response_bytes"], adapter.MAX_RESPONSE_BYTES)
            if owner.request_error:
                raise owner.request_error
            if owner.on_request:
                owner.on_request()
            return owner.responses.pop(0)
        start("harness.Gateway.request", new=request)

    def stop_fixture(self):
        if self.proc is not None:
            if self.proc.poll() is None:
                self.proc.terminate()
            self.proc.wait(timeout=5)
            self.proc = None

    def servers(self):
        return [] if self.proc is None or self.proc.poll() is not None else [
            dict(pid=self.proc.pid, parent_pid=os.getpid(), path=str(Path(sys.executable).resolve()))]

    def run_worker(self, **kwargs):
        return adapter.run_owned_gateway_worker(kwargs.pop("executor", self.executor), kwargs.pop("unit_spec", self.unit),
                                                kwargs.pop("repo", self.repo), gateway=kwargs.pop("gateway", self.gateway),
                                                audit_directory=kwargs.pop("audit_directory", self.audit),
                                                timeout_seconds=kwargs.pop("timeout_seconds", 10), **kwargs)

    def assert_clean(self, result, *, model=True):
        self.assertEqual(result["cleanup"], "observed_empty")
        self.assertIsNone(winprocess.identity(result["process"]["pid"]))
        if model:
            self.assertEqual(result["transport"]["model_cleanup"], "observed_idle")
            self.assertIsNone(winprocess.identity(result["transport"]["model"]["pid"]))
        self.assertIsNone(self.gateway.execution_deadline)
        self.assertIsNone(self.gateway.execution_cancel_event)
        self.assertIsNone(self.gateway.execution_response_limit)
        self.assertLessEqual(len(adapter._json(result)), adapter.MAX_EVIDENCE_BYTES)

    def test_owned_wire_native_identity_edits_and_cleanup(self):
        result = self.run_worker()
        self.assertTrue(result["success"])
        self.assertEqual(result["transport"]["run_id"], self.store.state["run_id"])
        self.assertEqual(result["transport"]["attempt_id"], "safe-qwen-task-attempt-1")
        self.assertEqual(result["transport"]["model"]["parent_pid"], os.getpid())
        requests = [event for event in self.events if event[1] == "/v1/chat/completions"]
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0][2]["model"], "operator-qwen")
        self.assertEqual(requests[0][2]["response_format"]["json_schema"]["name"], "safe_qwen_operation")
        self.assertNotIn("tools", requests[0][2])
        self.assertIn("return a * b", (self.repo.root / "calculator.py").read_text())
        self.assertFalse(result["trusted"])
        self.assert_clean(result)

    def test_wrong_gateway_supervisor_store_run_and_unit_refused(self):
        variants = [dict(gateway=object()), dict(executor=object()), dict(unit_spec={**self.unit, "unit_id": "foreign"})]
        for args in variants:
            with self.subTest(args=list(args)), self.assertRaises((adapter.AdapterError, KeyError)):
                self.run_worker(**args)
        original = self.gateway.supervisor
        self.gateway.supervisor = Supervisor(self.store)
        with self.assertRaises(adapter.AdapterError):
            self.run_worker()
        self.gateway.supervisor = original
        self.store.state["run_id"] = "foreign"
        with self.assertRaises(Exception):
            self.run_worker()
        self.store.state["run_id"] = self.store.directory.name
        self.assertEqual(self.events, [])

    def test_unbound_executor_refused(self):
        self.store._effective_work_unit_executor = object()
        with self.assertRaises(adapter.AdapterError):
            self.run_worker()
        self.assertEqual(self.events, [])

    def test_foreign_store_instance_refused(self):
        other = durable.Store(self.store.directory.parent, self.store.directory.name)
        other.load()
        original = self.repo.store
        self.repo.store = other
        try:
            with self.assertRaises(adapter.AdapterError):
                self.run_worker()
        finally:
            self.repo.store = original
        self.assertEqual(self.events, [])

    def test_completed_first_attempt_refused(self):
        section = copy.deepcopy(self.store.state["work_units"])
        unit = section["units"][self.unit["unit_id"]]
        work_units.complete_attempt(unit, unit["current_attempt"], outcome="interrupted")
        work_units.transition_unit(unit, "UNIT_FAILED")
        work_units.seal_unit_state(unit)
        work_units._reseal_section(section)
        self.store.commit(work_units=section)
        with self.assertRaises(adapter.AdapterError):
            self.run_worker()
        self.assertEqual(self.events, [])

    def test_closed_controller_refused(self):
        with patch.object(self.sup, "closed", True), self.assertRaises(adapter.AdapterError):
            self.run_worker()
        self.assertEqual(self.events, [])

    def test_unproven_gateway_parent_never_calls_model_or_cleanup_api(self):
        original = winprocess.identity
        observed = []
        def identity(pid):
            if pid == os.getpid():
                observed.append(pid)
                if len(observed) == 3:  # After two controller preflights, before startup.
                    return None
            return original(pid)
        with patch("winprocess.identity", identity), self.assertRaises(adapter.AdapterError) as caught:
            self.run_worker()
        self.assertEqual(self.events, [])
        self.assertIsNone(caught.exception.evidence["process"])
        self.assertEqual(caught.exception.evidence["transport"]["model_cleanup"], "not_started")

    def test_model_selection_pin_change_refused(self):
        original = self.executor.config
        self.executor.config = {**self.cfg, "roles": {**self.cfg["roles"], "code": "foreign-alias"}}
        try:
            with self.assertRaises(adapter.AdapterError):
                self.run_worker()
        finally:
            self.executor.config = original
        self.assertEqual(self.events, [])

    def test_child_has_no_transport_or_store_authority(self):
        original = adapter._frame
        starts = []
        def frame(session, sequence, kind, payload):
            if kind == "START":
                starts.append(copy.deepcopy(payload))
            return original(session, sequence, kind, payload)
        with patch.object(adapter, "_frame", frame):
            result = self.run_worker()
        self.assertEqual(len(starts), 1)
        self.assertEqual(set(starts[0]), {"unit", "repository", "deadline", "max_actions", "policy"})
        self.assertEqual(starts[0]["unit"]["limits"]["max_attempts"], 1)
        self.assert_clean(result)

    def test_concurrent_owned_entry_creates_exactly_one_worker_and_model_fixture(self):
        barrier = threading.Barrier(2)
        outcomes = []
        def run(index):
            barrier.wait(5)
            try:
                outcomes.append(self.run_worker(audit_directory=self.root / f"concurrent-{index}"))
            except adapter.AdapterError as exc:
                outcomes.append(exc)
        threads = [threading.Thread(target=run, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(15)
            self.assertFalse(thread.is_alive())
        self.assertEqual(sum(type(v) is dict and v["success"] for v in outcomes), 1)
        self.assertEqual(sum(type(v) is adapter.AdapterError for v in outcomes), 1)
        self.assertEqual(sum(e[1].startswith('/upstream/') for e in self.events), 1)

    def test_second_attempt_limit_refused(self):
        unit = copy.deepcopy(self.unit)
        unit["limits"]["max_attempts"] = 2
        with self.assertRaises(Exception):
            self.run_worker(unit_spec=unit)
        self.assertEqual(self.events, [])

    def test_duplicate_entry_and_scripted_entry_cannot_adopt_binding(self):
        self.run_worker()
        self.events.clear()
        with self.assertRaises(adapter.AdapterError):
            self.run_worker(audit_directory=self.root / "again")
        self.assertEqual(self.events, [])
        with self.assertRaises(adapter.AdapterError):
            adapter.run_scripted_worker(self.executor.policy, self.unit, gateway=self.gateway, audit_directory=self.root / "wrong")

    def test_endpoint_config_drift_before_operation_refused(self):
        self.gateway.config = {**self.cfg, "base_url": "http://192.0.2.1:9333"}
        with self.assertRaises(adapter.AdapterError):
            self.run_worker()
        self.assertEqual(self.events, [])

    def test_endpoint_drift_during_request_prevents_edit_and_retargeted_unload(self):
        self.on_request = lambda: setattr(self.gateway, "config", {**self.cfg, "base_url": "http://127.0.0.1:9444"})
        with self.assertRaises(adapter.AdapterError) as caught:
            self.run_worker()
        self.assertEqual((self.repo.root / "calculator.py").read_bytes(), self.before)
        self.assertEqual(caught.exception.evidence["transport"]["model_cleanup"], "not_observed")
        self.assertNotIn("/api/models/unload", [e[1] for e in self.events])
        self.assert_clean(caught.exception.evidence, model=False)

    def test_native_model_exit_during_request_refused(self):
        self.on_request = self.stop_fixture
        with self.assertRaises(adapter.AdapterError) as caught:
            self.run_worker()
        self.assertEqual((self.repo.root / "calculator.py").read_bytes(), self.before)
        self.assertFalse(caught.exception.evidence["success"])
        self.assert_clean(caught.exception.evidence, model=False)

    def test_conflicting_native_model_observation_refused(self):
        original = self.servers
        def foreign():
            entries = original()
            if entries:
                entries[0]["parent_pid"] += 1
            return entries
        with patch("winprocess.servers", foreign), self.assertRaises(adapter.AdapterError) as caught:
            self.run_worker()
        self.assertEqual((self.repo.root / "calculator.py").read_bytes(), self.before)
        self.assertFalse(caught.exception.evidence["execution_started"])

    def test_model_request_failure_has_bounded_diagnostics_and_cleanup(self):
        self.request_error = RuntimeError("Authorization: Bearer abcdefghijklmnopqrstuvwxyz")
        with self.assertRaises(adapter.AdapterError) as caught:
            self.run_worker()
        evidence = caught.exception.evidence
        self.assertIn("owned_gateway_request", json.dumps(evidence["transport"]["failure"]))
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", json.dumps(evidence))
        self.assert_clean(evidence)

    def test_cleanup_failure_cannot_report_success(self):
        self.cleanup_error = RuntimeError("Fixed unload failure")
        with self.assertRaises(adapter.AdapterError) as caught:
            self.run_worker()
        self.assertFalse(caught.exception.evidence["success"])
        self.assertIn("cleanup_failure", caught.exception.evidence["transport"])
        self.assert_clean(caught.exception.evidence, model=False)

    def test_conflicting_parent_handle_binding_refuses_cleanup_api(self):
        original = adapter._OwnedGateway.check
        def check(binding, **kwargs):
            original(binding, **kwargs)
            if binding.started and not kwargs.get("model"):
                k, handle, identity = binding.handles["parent"]
                binding.handles["parent"] = (k, handle, {**identity, "created": "0"})
        with patch.object(adapter._OwnedGateway, "check", check), self.assertRaises(adapter.AdapterError) as caught:
            self.run_worker()
        self.assertNotIn("/api/models/unload", [e[1] for e in self.events])
        self.assertFalse(caught.exception.evidence["success"])
        self.assert_clean(caught.exception.evidence, model=False)

    def test_malformed_response_refused_before_tool(self):
        self.responses = [{"choices": []}]
        with self.assertRaises(adapter.AdapterError) as caught:
            self.run_worker()
        self.assertEqual((self.repo.root / "calculator.py").read_bytes(), self.before)
        self.assert_clean(caught.exception.evidence)

    def test_oversized_content_refused_before_tool(self):
        self.responses = [wire("x" * 70001)]
        with self.assertRaises(adapter.AdapterError) as caught:
            self.run_worker()
        self.assertEqual(caught.exception.evidence["operations"], [])
        self.assert_clean(caught.exception.evidence)

    def test_oversized_wire_refused_before_tool(self):
        self.responses = [wire("x" * (adapter.MAX_RESPONSE_BYTES + 1))]
        with self.assertRaises(adapter.AdapterError) as caught:
            self.run_worker()
        self.assertEqual(caught.exception.evidence["operations"], [])
        self.assert_clean(caught.exception.evidence)

    def test_cancellation_during_request_refuses_edit(self):
        event = threading.Event()
        self.on_request = event.set
        with self.assertRaises(adapter.AdapterError) as caught:
            self.run_worker(cancel_event=event)
        self.assertEqual((self.repo.root / "calculator.py").read_bytes(), self.before)
        self.assert_clean(caught.exception.evidence)

    def test_deadline_after_response_refuses_edit(self):
        self.on_request = lambda: time.sleep(0.6)
        with self.assertRaises(adapter.AdapterError) as caught:
            self.run_worker(timeout_seconds=0.5)
        self.assertEqual((self.repo.root / "calculator.py").read_bytes(), self.before)
        self.assert_clean(caught.exception.evidence)

    def test_controller_policy_still_denies_frozen_edit(self):
        self.responses = [wire(op("edit_file", path="test_calculator.py", before_sha256="0" * 64, old_text="x", new_text="y")), wire(self.submit)]
        result = self.run_worker()
        self.assertEqual(result["operations"][0]["code"], "FROZEN_FILE")
        self.assertEqual(result["changed_paths"], [])
        self.assert_clean(result)

    def test_previous_deadline_is_not_extended(self):
        previous = time.monotonic() + 10
        self.gateway.set_deadline(previous)
        result = self.run_worker()
        self.assertEqual(self.gateway.execution_deadline, previous)
        self.assertLessEqual(result["transport"]["requests"][0]["deadline"], previous)


class GatewayBoundedIOTests(unittest.TestCase):
    def gateway(self):
        return harness.Gateway({"base_url": "http://127.0.0.1:9292"}, None)

    def test_response_read_uses_hard_byte_limit(self):
        sizes = []
        class Response(io.BytesIO):
            def read(self, size=-1):
                sizes.append(size)
                return super().read(size)
        opener = SimpleNamespace(open=lambda *args, **kwargs: Response(b"x" * 33))
        with patch("urllib.request.build_opener", return_value=opener):
            with self.assertRaises(ValueError):
                REAL_REQUEST(self.gateway(), "GET", "/running", max_response_bytes=32)
        self.assertEqual(sizes, [33])

    def test_bounded_transport_refuses_redirects_and_proxies(self):
        captured = []
        def build(*handlers):
            captured.extend(handlers)
            return SimpleNamespace(open=lambda *args, **kwargs: io.BytesIO(b"{}"))
        with patch("urllib.request.build_opener", build):
            self.assertEqual(REAL_REQUEST(self.gateway(), "GET", "/running", max_response_bytes=32), {})
        redirect = next(h for h in captured if isinstance(h, urllib.request.HTTPRedirectHandler))
        with self.assertRaises(ValueError):
            redirect.redirect_request(None, None, None, None, None, "http://192.0.2.1/")
        proxy = next(h for h in captured if isinstance(h, urllib.request.ProxyHandler))
        self.assertEqual(proxy.proxies, {})

    def test_cancelled_request_never_opens_connection(self):
        event = threading.Event()
        event.set()
        with patch("urllib.request.urlopen", side_effect=AssertionError("No network")), self.assertRaises(TimeoutError):
            REAL_REQUEST(self.gateway(), "GET", "/running", cancel_event=event)

    def test_cancel_and_deadline_interrupt_native_transport_socket_without_network(self):
        for mode in ("cancel", "deadline"):
            with self.subTest(mode=mode):
                closed = threading.Event()
                event = threading.Event()
                class Body(io.BytesIO):
                    def read(self, size=-1):
                        if not closed.wait(2):
                            raise AssertionError("Transport socket was not interrupted")
                        return b""
                class Socket:
                    def sendall(self, data):
                        pass
                    def makefile(self, *args):
                        return Body(b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\n")
                    def shutdown(self, how):
                        closed.set()
                    def close(self):
                        pass
                def connect(connection):
                    connection.sock = Socket()
                gateway = self.gateway()
                gateway.set_deadline(time.monotonic() + (1 if mode == "cancel" else 0.2))
                timer = threading.Timer(0.2, event.set)
                timer.start()
                try:
                    with patch("http.client.HTTPConnection.connect", connect), patch("socket.create_connection", side_effect=AssertionError("No network")):
                        with self.assertRaises((OSError, TimeoutError, ValueError)):
                            REAL_REQUEST(gateway, "GET", "/running", timeout=1, max_response_bytes=32,
                                         cancel_event=event if mode == "cancel" else None)
                    self.assertTrue(closed.is_set())
                finally:
                    timer.cancel()


if __name__ == "__main__":
    unittest.main()
