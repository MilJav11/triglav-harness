"""Narrow Windows trusted-fixture policy proof. No live Gateway or OS sandbox claim."""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

import offline_scripted_executor as offline
import winprocess
from scripts.ci_supplemental_policy import Fixture, PolicyViolation, SupplementalPolicy

ROOT = Path(__file__).resolve().parents[1]
PYTHON = str(Path(sys.executable).resolve())
WORKER_SHA = "52030a71e782db713d7cc95cd280cdb1a8981cd8acaf9c78f64e7b3a30c06fe6"
WINPROCESS_SHA = "8dc43c9e0e223691ecb5c79f30dd765299aea7f41e624e4913b2f119f3863c56"
OFFLINE_SHA = "55a1c5e0bf6e12461d828dda4afc5bf62b6bf8cdcec29cc64d16d62e2701e85d"
SAFE_QWEN_PINS = {
    "supervised_safe_qwen.py": "8d89637c5acd0fed4b114028deb2d7d38a8e5caceab47135022bced9fa1c87c0",
    "supervised_safe_qwen_worker.py": "7b4a7feb2692994ac01fb21d87cb1b2e03b7d1e27ad7e126c93d691e32b2f2ad",
    "safe_qwen_worker.py": "23bb2f14d347dd3ec87f75a2a47b52035b46ca241b3975018cad60b3aba161b7",
    "tool_protocol.py": "489b8c238ea335508d3647a7fd9eba1b17acf8f68e346ff19e43b0c3ba318c2a",
    "integration_shim.py": "5a9427e87e205c717d5b2efd5f07f587a4a07415e541683cab97714de1a25116",
    "winprocess.py": WINPROCESS_SHA,
    "offline_scripted_executor.py": OFFLINE_SHA,
    "safe_executor_binding.py": "de5746ff7e4693a4639ef4ba5ba9d2ecb1f5dfd8b139302ef9635df94a3c8aa1",
    "work_units.py": "410b6c70f7378f40c64015d670ccf1bdf555ac3df9abd2e03570e8be10c69fb8",
    "harness.py": "b97b6964a5b4617393263799e59311ce3bc2409cf93a2a32b87e166cf9d9d3ea",
    "supervision.py": "063c1725727aad0e73165353e4621dccb04a8285e1efeb2f3f9ca3a98bb60ffc",
}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


@unittest.skipUnless(os.name == "nt", "Real Windows Job and native process proof required")
class SupplementalPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.policy = SupplementalPolicy(ROOT, Path(self.temp.name) / "owned")
        self.fixture = self.policy.new_fixture()
        self.evidence = {}

    def helper(self, source="print('reviewed fixture')\n", boundary="native", args=(), limit=1, dependencies=None):
        path = self.fixture.root / "helper.py"
        path.write_bytes(source.encode("utf-8"))
        command = [PYTHON, "-I", "-B", str(path), *args]
        self.policy.register(self.fixture, profile="isolated_helper", boundary=boundary,
                             argv=command, cwd=self.fixture.root,
                             source_pins={path: sha(source.encode()), **(dependencies or {})}, limit=limit)
        return path, command

    def native(self, command, *, cwd=None, expected=0):
        n = winprocess.ControllerVerifier("Local\\supp-policy-" + uuid.uuid4().hex)
        try:
            with open(os.devnull, "rb") as inp, tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
                n.launch(command, cwd or self.fixture.root, (inp, out, err))
                identity = dict(n.identity)
                self.assertIsNone(n.wait(0))
                n.verify(identity)
                n.resume(identity)
                code = n.wait(10)
                self.assertEqual(code, expected)
                n.drain(10)
                self.assertIsNone(winprocess._handle_identity(n.k, n.process))
                self.evidence = dict(pid=n.pid, identity=identity, exit_code=code, cleanup="observed_empty",
                                     argv=command, policy_events=self.policy.events)
        finally:
            if n.process is not None and n.wait(0) is None:
                n.terminate_job(); n.drain(10)
            n.close()

    def test_approved_actual_offline_worker_edits_and_drains(self):
        repo = self.fixture.root / "repo"; repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
        (repo / "calculator.py").write_text("def multiply(a,b): return 0\n")
        pins = {ROOT / "offline_scripted_executor.py": OFFLINE_SHA,
                ROOT / "offline_scripted_executor_worker.py": WORKER_SHA,
                ROOT / "winprocess.py": WINPROCESS_SHA}
        self.assertEqual({p.name: h for p, h in pins.items()}, offline._source_identity())
        argv = [PYTHON, "-I", "-B", str(ROOT / "offline_scripted_executor_worker.py")]
        self.policy.register(self.fixture, profile="native_worker", boundary="native", argv=argv, cwd=repo, source_pins=pins)
        with self.policy.guard():
            result = offline.run_script(repo, {"schema_version": 1, "actions": [
                {"op": "write", "path": "calculator.py", "content": "def multiply(a,b): return a*b\n"}]},
                allowed_paths=["calculator.py"], timeout_seconds=10)
        self.assertTrue(result["success"])
        self.assertTrue(result["identity_verified"])
        self.assertEqual(result["cleanup"], "observed_empty")
        self.assertEqual(result["observed_exit_code"], 0)
        self.assertEqual((repo / "calculator.py").read_text(), "def multiply(a,b): return a*b\n")
        self.assertEqual(self.policy.events[0]["argv"], argv)
        self.evidence = result

    def test_actual_safe_qwen_worker_preserves_scripted_gateway_and_zero_inference(self):
        import supervised_safe_qwen as adapter
        from safe_qwen_worker import Policy
        from test_supervised_safe_qwen import op, wire
        repo = self.fixture.root / "repo"; repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
        before = b"def multiply(a, b): return a + b\n"
        (repo / "calculator.py").write_bytes(before)
        (repo / "test_calculator.py").write_bytes(b"# frozen fixture\n")
        policy = Policy(repo, ("calculator.py", "test_calculator.py"), ("calculator.py",),
                        ("test_calculator.py",), ("calculator.py", "test_calculator.py"))
        unit = dict(unit_id="safe-qwen-task", objective="Implement multiplication", dependencies=[], mode="mutation",
                    scope=dict(allowed_paths=["calculator.py"], forbidden_paths=["test_calculator.py"]),
                    verifier_ids=["frozen-check"], evidence_inputs=[], limits=dict(max_attempts=1,timeout_seconds=10))
        cfg = dict(executor="safe_qwen",base_url="http://127.0.0.1:9292",roles={"code":"configured-local-code"},
                   timeout_seconds=10,request_timeout_seconds=5,max_actions=8)
        gateway = adapter.ScriptedGateway(cfg, [wire(op("edit_file",path="calculator.py",before_sha256=sha(before),
                    old_text="a + b",new_text="a * b")), wire(op("submit",summary="offline candidate only"))])
        self.assertEqual(adapter._sources(), SAFE_QWEN_PINS)
        argv = [PYTHON,"-I","-B",str(ROOT / "supervised_safe_qwen_worker.py")]
        self.policy.register(self.fixture,profile="native_worker",boundary="native",argv=argv,cwd=repo,
                             source_pins={ROOT/name: h for name,h in SAFE_QWEN_PINS.items()})
        with self.policy.guard():
            result = adapter.run_scripted_worker(policy,unit,gateway=gateway,
                        audit_directory=self.fixture.root / "audit",timeout_seconds=10)
        self.assertTrue(result["success"])
        self.assertTrue(result["identity_verified"])
        self.assertEqual(result["cleanup"],"observed_empty")
        self.assertEqual(result["observed_exit_code"],0)
        self.assertEqual(result["model_requests"],2)  # Scripted messages, never inference.
        self.assertEqual((repo / "calculator.py").read_bytes(),b"def multiply(a, b): return a * b\n")
        self.assertEqual(self.policy.events[0]["argv"],argv)
        self.evidence = dict(native=result,live_model_calls=0,live_model_processes=0,
                             transport="exact ScriptedGateway, data-only responses")

    def test_f2_unregistered_native_command_refused_before_creation(self):
        marker = self.fixture.root / "forbidden.txt"
        argv = [PYTHON, "-I", "-B", "-c", f"from pathlib import Path;Path({str(marker)!r}).write_text('bad')"]
        with patch.object(winprocess, "_create_verifier_in_job", wraps=winprocess._create_verifier_in_job) as actual:
            with self.policy.guard(), self.assertRaises(PolicyViolation): self.native(argv)
            actual.assert_not_called()
        self.assertFalse(marker.exists())
        self.assertEqual(self.policy.events[-1]["decision"], "DENY")

    def test_popen_exact_script_and_observed_exit(self):
        path, argv = self.helper(boundary="popen")
        before = path.read_bytes()
        with self.policy.guard():
            result = subprocess.run(argv, cwd=self.fixture.root, check=True, capture_output=True, timeout=10)
        self.assertEqual(result.stdout.strip(), b"reviewed fixture")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.policy.events[0]["boundary"], "popen")
        self.assertEqual(self.policy.events[0]["argv"], argv)
        self.evidence = dict(exit_code=result.returncode, events=self.policy.events,
                             job_guarantee=False, note="Popen profile validates admission; caller owns lifecycle")

    def test_popen_original_reference_is_still_guarded_by_audit(self):
        popen = subprocess.Popen
        with self.policy.guard(), self.assertRaises(PolicyViolation):
            popen([PYTHON, "-I", "-B", "-c", "pass"], cwd=self.fixture.root)

    def test_wrong_reviewed_hash_is_refused_during_registration(self):
        script = self.fixture.root / "helper.py"; script.write_text("pass\n")
        with self.assertRaises(PolicyViolation):
            self.policy.register(self.fixture, profile="isolated_helper", boundary="native",
                argv=[PYTHON, "-I", "-B", str(script)], cwd=self.fixture.root, source_pins={script: "0" * 64})
        self.assertEqual(self.policy.rules, [])

    def test_script_drift_refused_at_both_parent_boundaries(self):
        path, argv = self.helper()
        self.policy.register(self.fixture, profile="isolated_helper", boundary="popen", argv=argv,
                             cwd=self.fixture.root, source_pins={path: sha(path.read_bytes())})
        path.write_text("raise AssertionError('drift must not execute')\n")
        with patch.object(winprocess, "_create_verifier_in_job", wraps=winprocess._create_verifier_in_job) as actual:
            with self.policy.guard():
                with self.assertRaises(PolicyViolation): self.native(argv)
                with self.assertRaises(PolicyViolation): subprocess.run(argv, cwd=self.fixture.root)
            actual.assert_not_called()
        self.assertEqual([e["decision"] for e in self.policy.events], ["DENY", "DENY"])

    def test_wrong_executable_cwd_argv_and_boundary_are_refused(self):
        _, argv = self.helper()
        other = self.policy.new_fixture()
        with self.policy.guard():
            for command, cwd in (([str(Path(os.environ["SystemRoot"]) / "System32/cmd.exe"), *argv[1:]], self.fixture.root),
                                 (argv, other.root), ([*argv, "--model", "not-authorized"], self.fixture.root)):
                with self.subTest(command=command, cwd=str(cwd)), self.assertRaises(PolicyViolation):
                    self.native(command, cwd=cwd)
            with self.assertRaises(PolicyViolation): subprocess.run(argv, cwd=self.fixture.root)
        self.assertTrue(all(e["decision"] == "DENY" for e in self.policy.events))

    def test_popen_executable_and_environment_overrides_refused(self):
        _, argv = self.helper(boundary="popen")
        with self.policy.guard():
            with self.assertRaises(PolicyViolation):
                subprocess.run(argv, cwd=self.fixture.root, executable=str(Path(os.environ["SystemRoot"]) / "System32/cmd.exe"))
            with self.assertRaises(PolicyViolation): subprocess.run(argv, cwd=self.fixture.root, env=dict(os.environ))
            with self.assertRaises(PolicyViolation): subprocess.run(argv, cwd=self.fixture.root, shell=True)

    def test_existing_or_foreign_fixture_cannot_be_adopted(self):
        with self.assertRaises(FileExistsError): SupplementalPolicy(ROOT, self.policy.root)
        forged = Fixture(self.fixture.root, self.fixture.identity)
        with self.assertRaises(PolicyViolation):
            self.policy.register(forged, profile="inline_fixture", boundary="native",
                argv=[PYTHON, "-I", "-B", "-c", "pass"], cwd=forged.root, source_pins={})

    def test_cwd_directory_replacement_is_refused(self):
        _, argv = self.helper()
        old = self.fixture.root.with_name(self.fixture.root.name + "-old")
        self.fixture.root.rename(old); self.fixture.root.mkdir()
        (self.fixture.root / "helper.py").write_bytes((old / "helper.py").read_bytes())
        with self.policy.guard(), self.assertRaises(PolicyViolation): self.native(argv)

    def test_hardlinked_script_is_refused(self):
        path = self.fixture.root / "helper.py"; path.write_text("pass\n")
        os.link(path, self.fixture.root / "alias.py")
        with self.assertRaises(PolicyViolation):
            self.policy.register(self.fixture, profile="isolated_helper", boundary="native",
                argv=[PYTHON, "-I", "-B", str(path)], cwd=self.fixture.root, source_pins={path: sha(path.read_bytes())})

    def test_registration_and_launch_budgets_are_closed(self):
        _, argv = self.helper()
        with self.assertRaises(PolicyViolation): self.policy.authorize("native", argv, self.fixture.root)
        with self.policy.guard():
            with self.assertRaises(PolicyViolation): self.policy.register(
                self.fixture, profile="inline_fixture", boundary="native", argv=[PYTHON,"-I","-B","-c","pass"],
                cwd=self.fixture.root, source_pins={})
            self.native(argv)
            with self.assertRaises(PolicyViolation): self.native(argv)

    def test_parent_sockets_and_unmocked_gateway_refuse_fake_override_survives(self):
        import harness
        original = harness.Gateway.request
        fake = harness.Gateway.__new__(harness.Gateway)
        with self.policy.guard():
            with self.assertRaises(PolicyViolation): socket.socket()
            with self.assertRaises(PolicyViolation): fake.request("GET", "/running")
            with patch.object(harness.Gateway, "request", return_value={"fake": True}):
                self.assertEqual(fake.request("GET", "/running"), {"fake": True})
        self.assertIs(harness.Gateway.request, original)

    def test_real_isolated_crash_helper_descendant_identity_and_job_drain(self):
        import _winapi
        marker = self.fixture.root / "descendant.json"
        source = ("import json,os,subprocess,sys\nfrom pathlib import Path\n"
                  f"sys.path.insert(0,{str(ROOT)!r})\nimport winprocess\n"
                  "sys.audit('socket.__new__',None,0,0,0)  # No actual socket is created.\n"
                  "p=subprocess.Popen([sys.executable,'-I','-B','-c','import time;time.sleep(120)'],"
                  "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,close_fds=True)\n"
                  f"Path({str(marker)!r}).write_text(json.dumps(dict(pid=p.pid,identity=winprocess.identity(p.pid))))\n"
                  "os._exit(73)\n")
        path, argv = self.helper(source, dependencies={ROOT / "winprocess.py": WINPROCESS_SHA})
        before = path.read_bytes()
        native = winprocess.ControllerVerifier("Local\\trusted-crash-" + uuid.uuid4().hex)
        child_handle = None
        try:
            with open(os.devnull,"rb") as inp, tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
                with self.policy.guard():
                    with self.assertRaises(PolicyViolation): sys.audit("socket.__new__",None,0,0,0)
                    native.launch(argv,self.fixture.root,(inp,out,err))
                    native.verify(native.identity)
                    self.assertIsNone(native.wait(0))
                    native.resume(native.identity)
                    self.assertEqual(native.wait(10),73)
                    child = json.loads(marker.read_text())
                    child_handle = native.k.OpenProcess(0x1000 | 0x100000,False,child["pid"])
                    self.assertTrue(child_handle)
                    self.assertEqual(winprocess._handle_identity(native.k,child_handle),child["identity"])
                    member = winprocess.W.BOOL()
                    self.assertTrue(native.k.IsProcessInJob(child_handle,native.job.handle,ctypes.byref(member)))
                    self.assertTrue(member.value)
                    self.assertEqual(_winapi.WaitForSingleObject(child_handle,0),258)
                    native.terminate_job(); native.drain(10)
                    self.assertEqual(_winapi.WaitForSingleObject(child_handle,10000),0)
                    self.assertEqual(_winapi.GetExitCodeProcess(child_handle),137)
                    self.assertIsNone(winprocess._handle_identity(native.k,child_handle))
                    self.assertEqual(native.wait(0),73)
                    self.assertEqual(path.read_bytes(),before)
                    self.assertEqual(len([e for e in self.policy.events if e["decision"]=="ALLOW"]),1)
                    self.evidence = dict(helper=dict(pid=native.pid,identity=native.identity,exit_code=73),
                        descendant=child,descendant_exit_code=137,descendant_in_native_job=True,
                        cleanup="observed_empty",helper_sha256=sha(before),unchanged_argv=argv,
                        child_inherits_audit_hooks=False,recursive_enforcement=False,
                        actual_network_calls=0,model_processes=0,policy_events=self.policy.events)
        finally:
            native.terminate_job(); native.drain(10); native.close()
            if child_handle: _winapi.CloseHandle(child_handle)

    def test_existing_invocation_wal_argv_and_release_order_are_unchanged(self):
        from test_verifier_invocations import make_fixture
        import supervision
        repo, store, sup, scheduler = make_fixture(self.fixture.root)
        command = [PYTHON,"-m","unittest","test_calculator.py"]
        pins = {p: sha(p.read_bytes()) for p in repo.root.glob("*.py")}
        self.policy.register(self.fixture,profile="unittest_fixture",boundary="native",argv=command,cwd=repo.root,source_pins=pins)
        launch, resume, actual_create = winprocess.ControllerVerifier.launch, winprocess.ControllerVerifier.resume, winprocess._create_verifier_in_job
        seen = []
        def created(native, argv, cwd, streams):
            intent = (store.directory / "events.jsonl").read_bytes()
            def exact(kernel, actual_argv, actual_cwd, inherited, job_handle):
                self.assertIs(actual_argv,argv)
                self.assertEqual(actual_argv,command)
                self.assertEqual((store.directory / "events.jsonl").read_bytes(),intent)
                seen.append(list(actual_argv))
                return actual_create(kernel,actual_argv,actual_cwd,inherited,job_handle)
            with patch.object(winprocess,"_create_verifier_in_job",exact), self.policy.guard():
                return launch(native,argv,cwd,streams)
        def released(native, identity):
            child = next(iter(store.state["supervision"]["children"].values()))
            self.assertEqual(child["invocation"]["phase"],"RELEASED")
            native.verify(identity)
            return resume(native,identity)
        with patch.object(winprocess.ControllerVerifier,"launch",created), patch.object(winprocess.ControllerVerifier,"resume",released):
            result = scheduler.invoke_native_verifier("unit-1","unit-1-attempt-1","check-calc")
        self.assertEqual(result["exit_code"],0)
        self.assertEqual(seen,[command])
        child = next(iter(store.state["supervision"]["children"].values()))
        self.assertEqual(child["invocation"]["resolved_argv"],command)
        self.assertEqual(child["command_sha256"],supervision.checksum(command))
        events = [r["transition"]["event"] for r in store.records if r.get("transition")]
        self.assertEqual(events,["invocation_intent","invocation_bound","invocation_release","process_exit","invocation_complete"])
        self.assertEqual(result["controller_evidence"]["cleanup"],"observed_empty")
        self.assertEqual(self.policy.events[0]["argv"],command)
        self.evidence = dict(native=result["controller_evidence"],events=events,argv=command,
                             policy_events=self.policy.events,wal_sha256=sha((store.directory/"events.jsonl").read_bytes()))

    def test_late_unittest_shadow_source_is_refused(self):
        source = self.fixture.root / "test_fixed.py"; source.write_text("import unittest\n")
        command = [PYTHON,"-m","unittest",source.name]
        self.policy.register(self.fixture,profile="unittest_fixture",boundary="native",argv=command,
                             cwd=self.fixture.root,source_pins={source:sha(source.read_bytes())})
        (self.fixture.root / "unittest.py").write_text("raise AssertionError('shadow')\n")
        with self.policy.guard(), self.assertRaises(PolicyViolation): self.native(command)

    def test_exact_inline_native_fixture_is_admitted(self):
        argv = [PYTHON, "-I", "-B", "-c", "print('exact reviewed inline fixture')"]
        self.policy.register(self.fixture, profile="inline_fixture", boundary="native",
                             argv=argv, cwd=self.fixture.root, source_pins={})
        with self.policy.guard(): self.native(argv)
        self.assertEqual(self.policy.events[0]["argv"], argv)

    def test_foreign_thread_cannot_register_profiles(self):
        errors = []
        def foreign():
            try: self.policy.new_fixture()
            except PolicyViolation as exc: errors.append(str(exc))
        thread = threading.Thread(target=foreign)
        thread.start(); thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertEqual(self.policy.rules, [])

    def test_concurrent_parent_launches_cannot_reuse_single_admission(self):
        _, argv = self.helper(boundary="popen")
        ready = threading.Barrier(3)
        outcomes = []
        def launch():
            ready.wait(5)
            try:
                result = subprocess.run(argv,cwd=self.fixture.root,capture_output=True,timeout=10,check=True)
                outcomes.append(("PASS",result.returncode))
            except PolicyViolation:
                outcomes.append(("DENIED",None))
        threads = [threading.Thread(target=launch) for _ in range(2)]
        with self.policy.guard():
            for thread in threads: thread.start()
            ready.wait(5)
            for thread in threads: thread.join(15)
        self.assertTrue(all(not t.is_alive() for t in threads))
        self.assertCountEqual(outcomes,[("PASS",0),("DENIED",None)])
        self.assertCountEqual([e["decision"] for e in self.policy.events],["ALLOW","DENY"])

    def test_real_isolated_controller_death_closes_nested_native_job(self):
        import _winapi
        marker, release = self.fixture.root / "live.json", self.fixture.root / "exit-now"
        source = ("import json,os,sys,tempfile,time\nfrom pathlib import Path\n"
                  f"sys.path.insert(0,{str(ROOT)!r})\nimport winprocess\n"
                  "n=winprocess.ControllerVerifier('Local\\\\nested-proof-'+str(os.getpid()))\n"
                  "i=open(os.devnull,'rb');o=tempfile.TemporaryFile();e=tempfile.TemporaryFile()\n"
                  "n.launch([sys.executable,'-I','-B','-c','import time;time.sleep(120)'],Path.cwd(),(i,o,e))\n"
                  "n.verify(n.identity);n.resume(n.identity)\n"
                  f"Path({str(marker)!r}).write_text(json.dumps(dict(pid=n.pid,identity=n.identity)))\n"
                  "deadline=time.monotonic()+15\n"
                  f"while not Path({str(release)!r}).exists():\n"
                  "    if time.monotonic()>deadline: raise RuntimeError('parent handshake timeout')\n"
                  "    time.sleep(.01)\n"
                  "os._exit(73)\n")
        path, argv = self.helper(source, dependencies={ROOT / "winprocess.py": WINPROCESS_SHA})
        raw = path.read_bytes()
        native = winprocess.ControllerVerifier("Local\\nested-controller-" + uuid.uuid4().hex)
        retained = None
        try:
            with open(os.devnull,"rb") as inp, tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
                with self.policy.guard():
                    native.launch(argv,self.fixture.root,(inp,out,err))
                    native.verify(native.identity); native.resume(native.identity)
                    deadline = time.monotonic() + 10
                    while not marker.exists() and time.monotonic() < deadline:
                        self.assertIsNone(native.wait(0)); time.sleep(.01)
                    self.assertTrue(marker.exists(), "real inner verifier must be observed before controller death")
                    child = json.loads(marker.read_text())
                    retained = native.k.OpenProcess(0x1000 | 0x100000,False,child["pid"])
                    self.assertTrue(retained)
                    self.assertEqual(winprocess._handle_identity(native.k,retained),child["identity"])
                    member = winprocess.W.BOOL()
                    self.assertTrue(native.k.IsProcessInJob(retained,native.job.handle,ctypes.byref(member)))
                    self.assertTrue(member.value)
                    self.assertEqual(_winapi.WaitForSingleObject(retained,0),258)
                    release.write_bytes(b"exit")
                    self.assertEqual(native.wait(10),73)
                    # No parent TerminateJobObject: inner owner's abrupt death must kill the verifier.
                    self.assertEqual(_winapi.WaitForSingleObject(retained,10000),0)
                    self.assertIsNone(winprocess._handle_identity(native.k,retained))
                    native.drain(10)
                    self.assertEqual(path.read_bytes(),raw)
                    self.assertEqual(len(self.policy.events),1)
                    self.evidence = dict(controller=dict(pid=native.pid,identity=native.identity,exit_code=73),
                        verifier=child,verifier_exit_code=_winapi.GetExitCodeProcess(retained),
                        contained_in_outer_job=True,cleanup="observed_empty",parent_terminated_job=False,
                        child_hooks_inherited=False,recursive_enforcement=False,policy_events=self.policy.events)
        finally:
            native.terminate_job(); native.drain(10); native.close()
            if retained: _winapi.CloseHandle(retained)
