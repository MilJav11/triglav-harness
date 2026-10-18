"""Real Windows qualification with data-only scripted responses and owned fixtures.

Fault workers are fixed test programs injected at the native launch seam. They
are not supported adapter inputs, authenticated receipts or a security sandbox.
No live Gateway, model, scheduler, Store mutation or repair dispatch is used.
"""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import supervised_safe_qwen as adapter
import winprocess
from safe_qwen_worker import Policy, sha
from tool_protocol import schema

ROOT = Path(__file__).resolve().parents[1]
BEFORE = b"def multiply(a, b): return a + b\n"
AFTER = b"def multiply(a, b): return a * b\n"


def op(tool, **arguments):
    return json.dumps(dict(tool=tool, arguments=arguments))


def wire(content):
    return {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": content}}]}


@unittest.skipUnless(os.name == "nt", "Requires real Windows Job and retained process handles")
class SupervisedSafeQwenTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True, capture_output=True)
        (self.repo / "calculator.py").write_bytes(BEFORE)
        (self.repo / "test_calculator.py").write_text("# frozen deterministic tests\n")
        self.policy = Policy(self.repo, ("calculator.py", "test_calculator.py"), ("calculator.py",),
                             ("test_calculator.py",), ("calculator.py", "test_calculator.py"))
        self.unit = dict(unit_id="safe-qwen-task", objective="Implement multiplication", dependencies=[],
                         mode="mutation", scope=dict(allowed_paths=["calculator.py"], forbidden_paths=["test_calculator.py"]),
                         verifier_ids=["frozen-check"], evidence_inputs=[], limits=dict(max_attempts=1, timeout_seconds=10))
        self.cfg = dict(executor="safe_qwen", base_url="http://127.0.0.1:9292", roles={"code": "configured-local-code"},
                        timeout_seconds=10, request_timeout_seconds=5, max_actions=8)
        self.audit = self.root / "audit"
        self.submit = op("submit", summary="Candidate ready; no acceptance claim")
        self.edit = op("edit_file", path="calculator.py", before_sha256=sha(BEFORE), old_text="a + b", new_text="a * b")
        self.addCleanup(patch.stopall)
        for name in ("socket.socket", "socket.create_connection", "harness.Gateway",
                     "durable.Gateway", "work_unit_scheduler.WorkUnitScheduler.execute_attempt",
                     "durable.Store._issue_repair_grant", "durable.Store._consume_repair_grant",
                     "durable.Store.commit_terminal"):
            patch(name, side_effect=AssertionError("Forbidden model/network/recovery/acceptance call")).start()

    def run_worker(self, responses=None, **kwargs):
        gateway = kwargs.pop("gateway", adapter.ScriptedGateway(self.cfg, [wire(v) for v in (responses or [self.submit])]))
        return adapter.run_scripted_worker(kwargs.pop("policy", self.policy), kwargs.pop("unit_spec", self.unit),
                                           gateway=gateway, audit_directory=kwargs.pop("audit_directory", self.audit),
                                           timeout_seconds=kwargs.pop("timeout_seconds", 5), **kwargs)

    def assert_gone(self, pid):
        end = time.monotonic() + 5
        while winprocess.identity(pid) is not None and time.monotonic() < end:
            time.sleep(0.01)
        self.assertIsNone(winprocess.identity(pid))

    def assert_clean(self, evidence):
        self.assertEqual(evidence["source"], "controller_native_handles")
        self.assertEqual(evidence["cleanup"], "observed_empty")
        self.assertFalse(evidence["trusted"])
        self.assertLessEqual(len(adapter._json(evidence)), adapter.MAX_EVIDENCE_BYTES)
        if evidence["process"] is not None:
            self.assert_gone(evidence["process"]["pid"])

    def records(self):
        return [json.loads(line)["record"] for line in (self.audit / "audit.jsonl").read_text().splitlines()]

    def fault_worker(self, body, *, read_start=True):
        worker = self.root / "fixed_fault.py"
        worker.write_text("import json, os, sys, time, subprocess\nfrom pathlib import Path\n"
                          f"sys.path.insert(0,{str(ROOT)!r})\n"
                          "from supervised_safe_qwen import _frame\nfrom tool_protocol import schema\n"
                          + ("start=json.loads(sys.stdin.buffer.readline())\ns=start['session']\n" if read_start else "") + body)
        original = winprocess.ControllerVerifier.launch
        def launch(native, command, cwd, streams):
            return original(native, [str(Path(sys.executable).resolve()), "-I", "-B", str(worker)], cwd, streams)
        return patch.object(winprocess.ControllerVerifier, "launch", launch)

    def success_fixture(self, prefix="", suffix=""):
        return (prefix + "\nsys.stdout.buffer.write(_frame(s,1,'MODEL',{'messages':[{'role':'system','content':'fixture'},"
                "{'role':'user','content':'fixture'}],'schema':schema()}));sys.stdout.buffer.flush()\n"
                "response=json.loads(sys.stdin.buffer.readline())\n"
                "sys.stdout.buffer.write(_frame(s,3,'TOOL',{'raw':response['payload']['content']}));sys.stdout.buffer.flush()\n"
                "result=json.loads(sys.stdin.buffer.readline())\n"
                "receipt={'unit_id':'safe-qwen-task','attempt_number':1,'exit_code':0,'trusted':False,'submitted':True,"
                "'actions':1,'duration_seconds':0.01}\n"
                "sys.stdout.buffer.write(_frame(s,5,'RESULT',receipt));sys.stdout.buffer.flush()\n" + suffix)

    def test_read_exact_edit_submit_and_actual_state(self):
        evidence = self.run_worker([op("read_files", paths=["calculator.py", "test_calculator.py"]), self.edit, self.submit])
        self.assertTrue(evidence["success"])
        self.assertTrue(evidence["identity_verified"])
        self.assertTrue(evidence["scope_verified"])
        self.assertEqual(evidence["model_requests"], 3)
        self.assertEqual(len(evidence["operations"]), 3)
        self.assertEqual(evidence["changed_paths"], ["calculator.py"])
        self.assertEqual((self.repo / "calculator.py").read_bytes(), AFTER)
        self.assertEqual(evidence["receipt"]["attempt_number"], 1)
        self.assertFalse(evidence["receipt"]["trusted"])
        self.assert_clean(evidence)

    def test_edit_approval_is_fsynced_before_replace(self):
        original = os.replace
        checked = []
        def replace(source, target):
            self.assertEqual(Path(target).read_bytes(), BEFORE)
            self.assertEqual(self.records()[-1]["phase"], "intent")
            self.assertEqual(self.records()[-1]["after_sha256"], sha(AFTER))
            checked.append(True)
            return original(source, target)
        with patch("safe_qwen_worker.os.replace", replace):
            self.run_worker([self.edit, self.submit])
        self.assertEqual(checked, [True])

    def test_failed_edit_approval_prevents_mutation(self):
        original = os.fsync
        def fail(fd):
            if self.audit.exists() and (self.audit / "audit.jsonl").exists() and self.records()[-1].get("phase") == "intent":
                raise OSError("Fixed audit failure")
            return original(fd)
        with patch("supervised_safe_qwen.os.fsync", fail), self.assertRaises(adapter.AdapterError) as caught:
            self.run_worker([self.edit, self.submit])
        self.assertEqual((self.repo / "calculator.py").read_bytes(), BEFORE)
        self.assert_clean(caught.exception.evidence)

    def test_scope_frozen_stale_and_protected_edits_refused(self):
        cases = [(dict(path="outside.py", before_sha256=sha(BEFORE)), "SCOPE_DENIED"),
                 (dict(path="test_calculator.py", before_sha256="0" * 64), "FROZEN_FILE"),
                 (dict(path="calculator.py", before_sha256="0" * 64), "STALE_FILE"),
                 (dict(path="../outside.py", before_sha256=sha(BEFORE)), "PATH_DENIED"),
                 (dict(path=".git/config", before_sha256="0" * 64), "PATH_DENIED")]
        for index, (args, code) in enumerate(cases):
            with self.subTest(code=code):
                evidence = self.run_worker([op("edit_file", **args, old_text="a + b", new_text="a * b"), self.submit],
                                           audit_directory=self.root / f"audit-{index}")
                self.assertEqual(evidence["operations"][0]["code"], code)
                self.assertEqual(evidence["changed_paths"], [])
                self.assert_clean(evidence)

    def test_read_only_cannot_edit(self):
        unit = copy.deepcopy(self.unit)
        unit.update(mode="read_only", scope=dict(allowed_paths=list(self.policy.readable), forbidden_paths=[]))
        result = self.run_worker([self.edit, self.submit], unit_spec=unit)
        self.assertEqual(result["operations"][0]["code"], "SCOPE_DENIED")
        self.assertEqual((self.repo / "calculator.py").read_bytes(), BEFORE)

    def test_single_attempt_and_no_authority_context(self):
        unit = copy.deepcopy(self.unit)
        unit["limits"]["max_attempts"] = 2
        with self.assertRaises(adapter.AdapterError):
            self.run_worker(unit_spec=unit)
        self.assertFalse(self.audit.exists())
        with self.assertRaises(TypeError):
            self.run_worker(context={"grant": {}, "attempt_number": 2})

    def test_live_gateway_and_callbacks_refused(self):
        class Foreign:
            pass
        with self.assertRaises(adapter.AdapterError):
            self.run_worker(gateway=Foreign())
        with self.assertRaises((TypeError, ValueError)):
            adapter.ScriptedGateway(self.cfg, [lambda: self.submit])

    def test_configured_model_alias_preserved(self):
        self.cfg["roles"]["code"] = "future-compatible-local-model"
        result = self.run_worker()
        self.assertEqual(result["model"], "future-compatible-local-model")
        self.assertEqual([r["model"] for r in self.records() if r.get("event") == "model_request"],
                         ["future-compatible-local-model"])

    def test_gateway_cannot_be_reused_or_adopted(self):
        gateway = adapter.ScriptedGateway(self.cfg, [wire(self.submit)])
        self.run_worker(gateway=gateway)
        with self.assertRaises(adapter.AdapterError):
            self.run_worker(gateway=gateway, audit_directory=self.root / "again")
        self.assertFalse((self.root / "again").exists())

    def test_malformed_wire_uses_existing_refusal_and_action_budget(self):
        gateway = adapter.ScriptedGateway(self.cfg, [{"choices": []}, wire(self.submit)])
        evidence = self.run_worker(gateway=gateway)
        self.assertEqual(evidence["operations"][0]["code"], "MALFORMED_OPERATION")
        self.assertEqual(evidence["changed_paths"], [])

    def test_oversized_scripted_input_refused_before_launch(self):
        with self.assertRaises(adapter.AdapterError):
            adapter.ScriptedGateway(self.cfg, [wire("x" * (adapter.MAX_INPUT_BYTES + 1))])
        self.assertFalse(self.audit.exists())

    def test_deadline_before_launch(self):
        clock = iter([100.0])
        fake_time = SimpleNamespace(monotonic=lambda: next(clock, 101.0), sleep=time.sleep)
        with patch.object(adapter, "time", fake_time), self.assertRaises(adapter.AdapterError) as caught:
            self.run_worker(timeout_seconds=0.1)
        self.assertIsNone(caught.exception.evidence["process"])
        self.assertEqual(caught.exception.evidence["termination_basis"], "controller_deadline")

    def test_cancel_before_launch(self):
        event = threading.Event()
        event.set()
        with self.assertRaises(adapter.AdapterError) as caught:
            self.run_worker(cancel_event=event)
        self.assertFalse(caught.exception.evidence["execution_started"])

    def test_hung_child_hard_deadline_and_cleanup(self):
        with self.fault_worker("time.sleep(60)\n"), self.assertRaises(adapter.AdapterError) as caught:
            self.run_worker(timeout_seconds=0.5)
        self.assertEqual(caught.exception.evidence["termination_basis"], "controller_deadline")
        self.assertLess(caught.exception.evidence["duration_seconds"], 5)
        self.assert_clean(caught.exception.evidence)

    def test_hung_child_cannot_block_controller_ipc_write(self):
        unit = copy.deepcopy(self.unit)
        unit["objective"] = "\u03bb" * 2000  # Valid objective; escaped START exceeds pipe capacity.
        with self.fault_worker("time.sleep(60)\n", read_start=False), self.assertRaises(adapter.AdapterError) as caught:
            self.run_worker(unit_spec=unit, timeout_seconds=0.5)
        self.assertEqual(caught.exception.evidence["termination_basis"], "controller_deadline")
        self.assertLess(caught.exception.evidence["duration_seconds"], 5)
        self.assert_clean(caught.exception.evidence)

    def test_source_drift_before_launch_is_refused_without_editing_source(self):
        current = adapter._sources()
        current["safe_qwen_worker.py"] = "0" * 64
        with patch.object(adapter, "_sources", return_value=current), self.assertRaises(adapter.AdapterError):
            self.run_worker()
        self.assertFalse(self.audit.exists())

    def test_cancellation_during_execution(self):
        event = threading.Event()
        timer = threading.Timer(0.4, event.set)
        timer.start()
        try:
            with self.fault_worker("time.sleep(60)\n"), self.assertRaises(adapter.AdapterError) as caught:
                self.run_worker(cancel_event=event)
        finally:
            timer.cancel()
        self.assertEqual(caught.exception.evidence["termination_basis"], "controller_cancel")
        self.assert_clean(caught.exception.evidence)

    def test_worker_crash(self):
        with self.fault_worker("os._exit(7)\n"), self.assertRaises(adapter.AdapterError) as caught:
            self.run_worker()
        self.assertEqual(caught.exception.evidence["observed_exit_code"], 7)
        self.assert_clean(caught.exception.evidence)

    def test_native_identity_conflict_prevents_resume(self):
        original = winprocess.ControllerVerifier.launch
        def launch(native, *args):
            original(native, *args)
            native.identity = dict(created="0", executable=native.identity["executable"])
        with patch.object(winprocess.ControllerVerifier, "launch", launch), self.assertRaises(adapter.AdapterError) as caught:
            self.run_worker()
        self.assertFalse(caught.exception.evidence["execution_started"])
        self.assert_clean(caught.exception.evidence)

    def test_missing_native_identity_prevents_resume(self):
        original = winprocess.ControllerVerifier.launch
        def launch(native, *args):
            original(native, *args)
            native.identity = None
        with patch.object(winprocess.ControllerVerifier, "launch", launch), self.assertRaises(adapter.AdapterError) as caught:
            self.run_worker()
        self.assertFalse(caught.exception.evidence["execution_started"])
        self.assert_clean(caught.exception.evidence)

    def test_malformed_stale_duplicate_and_conflicting_frames(self):
        bodies = ["sys.stdout.buffer.write(b'{bad}\\n');sys.stdout.buffer.flush()\n",
                  "sys.stdout.buffer.write(_frame('0'*32,1,'MODEL',{}));sys.stdout.buffer.flush()\n",
                  "sys.stdout.buffer.write(_frame(s,0,'MODEL',{}));sys.stdout.buffer.flush()\n",
                  "sys.stdout.buffer.write(_frame(s,True,'MODEL',{}));sys.stdout.buffer.flush()\n",
                  "sys.stdout.buffer.write(b'{\"version\":1,\"version\":1}\\n');sys.stdout.buffer.flush()\n",
                  "sys.stdout.buffer.write(b'{');sys.stdout.buffer.flush()\n",
                  self.success_fixture(suffix="sys.stdout.buffer.write(_frame(s,5,'RESULT',receipt));sys.stdout.buffer.flush()\n")]
        for index, body in enumerate(bodies):
            with self.subTest(index=index), self.fault_worker(body), self.assertRaises(adapter.AdapterError) as caught:
                self.run_worker(audit_directory=self.root / f"bad-{index}")
            self.assert_clean(caught.exception.evidence)

    def test_oversized_worker_output_is_bounded(self):
        with self.fault_worker("sys.stdout.buffer.write(b'x'*(4*1024*1024+10));sys.stdout.buffer.flush();time.sleep(60)\n"), \
                self.assertRaises(adapter.AdapterError) as caught:
            self.run_worker()
        self.assertIn("Oversized", str(caught.exception))
        self.assert_clean(caught.exception.evidence)

    def test_worker_cannot_substitute_tool_operation(self):
        body = ("sys.stdout.buffer.write(_frame(s,1,'MODEL',{'messages':[{'role':'system','content':'x'},"
                "{'role':'user','content':'x'}],'schema':schema()}));sys.stdout.buffer.flush()\n"
                "sys.stdin.buffer.readline()\n"
                "sys.stdout.buffer.write(_frame(s,3,'TOOL',{'raw':'{}'}));sys.stdout.buffer.flush()\n")
        with self.fault_worker(body), self.assertRaises(adapter.AdapterError) as caught:
            self.run_worker([self.edit])
        self.assertEqual((self.repo / "calculator.py").read_bytes(), BEFORE)
        self.assert_clean(caught.exception.evidence)

    def test_worker_receipt_cannot_claim_success_without_submit(self):
        body = "sys.stdout.buffer.write(_frame(s,1,'RESULT',{'trusted':True}));sys.stdout.buffer.flush()\n"
        with self.fault_worker(body), self.assertRaises(adapter.AdapterError) as caught:
            self.run_worker()
        self.assertFalse(caught.exception.evidence["success"])

    def test_worker_direct_mutation_is_detected_not_sandboxed(self):
        body = self.success_fixture(prefix="Path('calculator.py').write_text('unapproved')\n")
        with self.fault_worker(body), self.assertRaises(adapter.AdapterError) as caught:
            self.run_worker()
        self.assertFalse(caught.exception.evidence["scope_verified"])
        self.assertFalse(caught.exception.evidence["success"])
        self.assert_clean(caught.exception.evidence)

    def test_descendants_drained_on_normal_failure_timeout_and_cancel(self):
        for index, mode in enumerate(("normal", "failure", "timeout", "cancel")):
            marker = self.root / f"descendant-{mode}.json"
            prefix = ("child=subprocess.Popen([sys.executable,'-I','-B','-c','import time;time.sleep(60)'],"
                      "stdin=subprocess.DEVNULL,close_fds=True)\n"
                      f"Path({str(marker)!r}).write_text(json.dumps({{'pid':child.pid}}))\n")
            body = self.success_fixture(prefix=prefix) if mode == "normal" else prefix + ("os._exit(7)\n" if mode == "failure" else "time.sleep(60)\n")
            event = threading.Event()
            def cancel():
                end = time.monotonic() + 5
                while not marker.exists() and time.monotonic() < end:
                    time.sleep(0.01)
                event.set()
            thread = threading.Thread(target=cancel) if mode == "cancel" else None
            if thread:
                thread.start()
            try:
                with self.subTest(mode=mode), self.fault_worker(body):
                    if mode == "normal":
                        result = self.run_worker(audit_directory=self.root / f"desc-audit-{index}")
                    else:
                        with self.assertRaises(adapter.AdapterError) as caught:
                            self.run_worker(timeout_seconds=1 if mode == "timeout" else 5, cancel_event=event,
                                            audit_directory=self.root / f"desc-audit-{index}")
                        result = caught.exception.evidence
                self.assertTrue(marker.exists())
                self.assert_gone(json.loads(marker.read_text())["pid"])
                self.assert_clean(result)
            finally:
                if thread:
                    thread.join(6)

    def test_controller_interruption_drains_child(self):
        original = winprocess.ControllerVerifier.wait
        interrupted = []
        def wait(native, timeout):
            if timeout == 0 and not interrupted:
                interrupted.append(True)
                raise KeyboardInterrupt()
            return original(native, timeout)
        with self.fault_worker("time.sleep(60)\n"), patch.object(winprocess.ControllerVerifier, "wait", wait), \
                self.assertRaises(KeyboardInterrupt) as caught:
            self.run_worker()
        self.assert_clean(caught.exception.adapter_evidence)

    def test_controller_death_kills_worker_and_descendant(self):
        marker = self.root / "death.json"
        fault = self.root / "death_worker.py"
        fault.write_text("import json,os,sys,subprocess,time\nfrom pathlib import Path\n"
                         "sys.stdin.buffer.readline()\n"
                         "p=subprocess.Popen([sys.executable,'-I','-B','-c','import time;time.sleep(60)'],close_fds=True)\n"
                         f"Path({str(marker)!r}).write_text(json.dumps({{'worker':os.getpid(),'descendant':p.pid}}))\n"
                         "time.sleep(60)\n")
        controller = self.root / "controller.py"
        controller.write_text("import sys\nfrom pathlib import Path\n"
                              f"sys.path.insert(0,{str(ROOT)!r})\n"
                              "import supervised_safe_qwen as a,winprocess\nfrom safe_qwen_worker import Policy\n"
                              "original=winprocess.ControllerVerifier.launch\n"
                              f"def launch(n,c,cwd,streams):return original(n,[sys.executable,'-I','-B',{str(fault)!r}],cwd,streams)\n"
                              "winprocess.ControllerVerifier.launch=launch\n"
                              f"p=Policy(Path({str(self.repo)!r}),('calculator.py','test_calculator.py'),('calculator.py',),('test_calculator.py',),('calculator.py','test_calculator.py'))\n"
                              f"a.run_scripted_worker(p,{self.unit!r},gateway=a.ScriptedGateway({self.cfg!r},[{wire(self.submit)!r}]),"
                              f"audit_directory={str(self.root / 'death-audit')!r},timeout_seconds=10)\n")
        native = winprocess.ControllerVerifier("Local\\safe-qwen-test-" + self.root.name)
        try:
            with tempfile.TemporaryFile() as inp, tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
                native.launch([str(Path(sys.executable).resolve()), "-I", "-B", str(controller)], self.root, (inp, out, err))
                native.resume(native.identity)
                end = time.monotonic() + 5
                while not marker.exists() and time.monotonic() < end and native.wait(0) is None:
                    time.sleep(0.01)
                self.assertTrue(marker.exists())
                identities = json.loads(marker.read_text())
                # Terminate only the retained controller handle, leaving its Job
                # open: the worker's own kill-on-close Job must cause cleanup.
                self.assertTrue(native.k.TerminateProcess(native.process, 137))
                self.assertEqual(native.wait(5), 137)
                self.assert_gone(identities["worker"])
                self.assert_gone(identities["descendant"])
        finally:
            native.terminate_job()
            native.drain(10)
            native.close()
        self.assertEqual((self.repo / "calculator.py").read_bytes(), BEFORE)

    def test_action_limit_has_no_second_attempt(self):
        self.cfg["max_actions"] = 1
        with self.assertRaises(adapter.AdapterError) as caught:
            self.run_worker(["{}"])
        self.assertEqual(caught.exception.evidence["model_requests"], 1)
        self.assertEqual(caught.exception.evidence["attempt_number"], 1)
        self.assert_clean(caught.exception.evidence)

    def test_cancellation_during_approval_prevents_edit(self):
        event = threading.Event()
        original = os.fsync
        def sync(fd):
            result = original(fd)
            if self.audit.exists() and (self.audit / "audit.jsonl").exists() and self.records()[-1].get("phase") == "intent":
                event.set()
            return result
        with patch("supervised_safe_qwen.os.fsync", sync), self.assertRaises(adapter.AdapterError) as caught:
            self.run_worker([self.edit, self.submit], cancel_event=event)
        self.assertEqual((self.repo / "calculator.py").read_bytes(), BEFORE)
        self.assert_clean(caught.exception.evidence)

    def test_concurrent_fixture_use_allows_exactly_one_child(self):
        gateway = adapter.ScriptedGateway(self.cfg, [wire(self.submit)])
        barrier = threading.Barrier(2)
        outcomes = []
        def run(index):
            barrier.wait(timeout=5)
            try:
                outcomes.append(self.run_worker(gateway=gateway, audit_directory=self.root / f"concurrent-{index}"))
            except adapter.AdapterError as exc:
                outcomes.append(exc)
        threads = [threading.Thread(target=run, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(sum(type(v) is dict and v["success"] for v in outcomes), 1)
        self.assertEqual(sum(type(v) is adapter.AdapterError for v in outcomes), 1)

    def test_conflicting_success_receipt_is_refused(self):
        body = self.success_fixture().replace("'actions':1", "'actions':2")
        with self.fault_worker(body), self.assertRaises(adapter.AdapterError) as caught:
            self.run_worker()
        self.assertFalse(caught.exception.evidence["success"])
        self.assert_clean(caught.exception.evidence)

    def test_diagnostics_truncated_and_bounded(self):
        body = self.success_fixture(prefix="sys.stderr.write('x'*10000);sys.stderr.flush()\n")
        with self.fault_worker(body):
            result = self.run_worker()
        self.assertLessEqual(len(adapter._json(result["diagnostics"])), 5000)
        self.assertTrue(result["diagnostics"]["truncated"])
        self.assert_clean(result)

    def test_audit_tampering_is_refused(self):
        original = winprocess.ControllerVerifier.wait
        tampered = []
        def wait(native, timeout):
            result = original(native, timeout)
            if result == 0 and not tampered:
                tampered.append(True)
                (self.audit / "audit.jsonl").write_text('{}\n')
            return result
        with patch.object(winprocess.ControllerVerifier, "wait", wait), self.assertRaises(adapter.AdapterError) as caught:
            self.run_worker()
        self.assertFalse(caught.exception.evidence["success"])
        self.assert_clean(caught.exception.evidence)

    def test_audit_path_inside_repository_or_reuse_refused(self):
        with self.assertRaises(adapter.AdapterError):
            self.run_worker(audit_directory=self.repo / "audit")
        self.assertFalse((self.repo / "audit").exists())
        self.audit.mkdir()
        with self.assertRaises(adapter.AdapterError):
            self.run_worker()

    def test_no_store_wal_grant_or_checkpoint_payload_to_child(self):
        original = adapter._frame
        starts = []
        def frame(session, seq, kind, payload):
            if kind == "START":
                starts.append(copy.deepcopy(payload))
                self.assertEqual(set(payload), {"unit", "repository", "deadline", "max_actions", "policy"})
            return original(session, seq, kind, payload)
        with patch.object(adapter, "_frame", frame):
            self.run_worker()
        self.assertEqual(len(starts), 1)
        self.assertEqual(set(starts[0]["unit"]), set(self.unit))
        self.assertFalse((self.repo / "events.jsonl").exists())
        self.assertFalse((self.repo / "checkpoints").exists())


if __name__ == "__main__":
    unittest.main()
