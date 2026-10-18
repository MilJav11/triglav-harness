"""Real Windows offline qualification. No models, sockets or acceptance gates.

Fault injection changes controller/native test seams, never authenticates JSON.
All candidate repositories are disposable Git fixtures.
"""
import copy
import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

import durable
import manager
import offline_scripted_executor as offline
import supervision
import winprocess
import work_units
from work_unit_scheduler import WorkUnitScheduler, WorkUnitSchedulingError
import test_policy_b_shadow as fixtures
import test_offline_repair_dispatch as dispatch_tests
REPAIR = dispatch_tests.REPAIR


@unittest.skipUnless(os.name == "nt", "Actual native Windows processes required")
class OfflineRepairVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.addCleanup(patch.stopall)
        for name in ("harness.Gateway", "durable.Gateway", "socket.socket", "socket.create_connection"):
            patch(name, side_effect=AssertionError("No AI/network")).start()
        patch.dict(os.environ, {"PYTHONDONTWRITEBYTECODE": "1", "GIT_OPTIONAL_LOCKS": "0"}).start()
        self.prepare()

    def prepare(self, *, unit_timeout=120, **kwargs):
        kwargs.setdefault("timeout", 60)
        original = fixtures.make_spec
        def bounded_spec(*args, **kw):
            spec = original(*args, **kw)
            spec["limits"]["timeout_seconds"] = unit_timeout
            return spec
        with patch.object(fixtures, "make_spec", bounded_spec):
            self.repo, self.store, self.sup, self.scheduler, self.executor = fixtures.make_shadow_fixture(self.root, **kwargs)

    reload = dispatch_tests.OfflineRepairDispatchTests.reload
    assert_gone = dispatch_tests.OfflineRepairDispatchTests.assert_gone
    def assert_no_adoption(self, store=None):
        loaded = store or self.reload()
        dispatch_tests.OfflineRepairDispatchTests.assert_no_adoption(self, loaded)
        wal = (loaded.directory / "events.jsonl").read_bytes()
        scheduler = WorkUnitScheduler(loaded, self.repo, supervisor=supervision.Supervisor(loaded),
                                      agents=manager.Agents(None, self.executor, None, None))
        with patch.object(winprocess.ControllerVerifier, "launch", side_effect=AssertionError("No qualification adoption")), \
                self.assertRaises(manager.Stop):
            scheduler.qualify_offline_repair_recovery("unit-1", REPAIR)
        self.assertEqual((loaded.directory / "events.jsonl").read_bytes(), wal)

    def qualify(self, script=None):
        return self.scheduler.qualify_offline_repair_recovery("unit-1", copy.deepcopy(REPAIR if script is None else script), timeout_seconds=20)

    def second(self, state=None):
        return (state or self.store.state)["work_units"]["units"]["unit-1"]["attempts"][1]

    def test_real_failure_repair_native_pass_order_and_no_acceptance(self):
        launches, resumes = [], []
        launch, resume = winprocess.ControllerVerifier.launch, winprocess.ControllerVerifier.resume
        def tracked_launch(native, argv, cwd, streams):
            if "repair_verification" in self.store.state:
                loaded = self.reload()
                self.assertEqual(loaded.state["repair_verification"]["phase"], "OBSERVING")
                children = loaded.state["supervision"]["children"].values()
                intent = [c for c in children if c.get("invocation", {}).get("phase") == "INTENT"]
                self.assertEqual(len(intent), 1)
                self.assertEqual(intent[0]["invocation"]["attempt_id"], "unit-1-attempt-2")
            result = launch(native, argv, cwd, streams)
            launches.append(native.pid)
            return result
        def tracked_resume(native, identity):
            if "repair_verification" in self.store.state:
                children = self.reload().state["supervision"]["children"].values()
                child = next(c for c in children if c["pid"] == native.pid)
                self.assertEqual(child["identity"], identity)
                self.assertEqual(child["invocation"]["phase"], "RELEASED")
            resumes.append(native.pid)
            return resume(native, identity)
        with patch.object(winprocess.ControllerVerifier, "launch", tracked_launch), patch.object(winprocess.ControllerVerifier, "resume", tracked_resume):
            result = self.qualify()
        self.assertEqual(result["status"], "HUMAN_ACTION_REQUIRED")
        self.assertTrue(result["verification"]["verification_complete"])
        self.assertEqual(result["verification"]["outcome"], "passed")
        loaded = self.reload()
        unit = loaded.state["work_units"]["units"]["unit-1"]
        self.assertEqual(len(unit["attempts"]), 2)
        self.assertEqual([a["outcome"] for a in unit["attempts"]], ["failed", "passed"])
        self.assertEqual(unit["status"], "VERIFYING")
        self.assertIsNone(unit["result"])
        self.assertIsNone(unit["current_attempt"])
        self.assertEqual(len(launches), 5)  # Two first verifiers, repair worker, two second verifiers.
        self.assertEqual(resumes, launches)
        for pid in launches:
            self.assert_gone(pid)
        self.assertEqual(len(self.executor.calls), 1)
        self.assertIn("a * b", (self.repo.root / "calculator.py").read_text())
        consumed = next(r for r in loaded.records if (r.get("repair_authorization") or {}).get("event") == "repair_authorization_consumed")
        self.assertEqual(unit["attempts"][0], consumed["state"]["work_units"]["units"]["unit-1"]["attempts"][0])
        att = unit["attempts"][1]
        self.assertEqual(att["execution_result"]["dispatch_evidence"], loaded.state["repair_dispatch"]["evidence"])
        obs = att["repair_observation"]
        self.assertEqual(obs["candidate_snapshot"], obs["verification_snapshot_before"])
        self.assertEqual(obs["candidate_snapshot"], obs["verification_snapshot_after"])
        self.assertEqual(obs["candidate_fs_snapshot"], obs["verification_fs_snapshot_after"])
        self.assertEqual(loaded.state["verified_progress"], [])
        self.assertIsNone(loaded.state["last_verified_checkpoint"])
        self.assertFalse(loaded.is_terminal_accepted())
        with self.assertRaises(durable.DurableError):
            loaded.commit_terminal("forbidden")
        self.assert_no_adoption(loaded)
        with patch.object(winprocess.ControllerVerifier, "launch", side_effect=AssertionError("No third attempt")):
            with self.assertRaises(manager.Stop):
                self.qualify()

    def test_second_deterministic_failure_is_terminal_and_cannot_create_third(self):
        self.qualify({"schema_version": 1, "actions": [{"op": "emit", "text": "fixed no-op"}]})
        loaded = self.reload()
        unit = loaded.state["work_units"]["units"]["unit-1"]
        self.assertEqual([a["outcome"] for a in unit["attempts"]], ["failed", "failed"])
        self.assertEqual(unit["status"], "UNIT_FAILED")
        self.assertEqual(self.second()["failure_classification"], "verification_failed")
        self.assertEqual(loaded.state["repair_verification"]["phase"], "COMPLETE")
        with self.assertRaises(work_units.WorkUnitError):
            work_units.transition_unit(copy.deepcopy(unit), "REPAIR_PENDING")
        self.assert_no_adoption(loaded)

    def test_candidate_environment_budget_and_controller_drift_after_dispatch_refuse_verification(self):
        for kind in ("candidate", "environment", "budget", "session", "reload"):
            with self.subTest(kind=kind):
                self.root = Path(self.temp.name) / kind
                self.root.mkdir()
                self.prepare(single=True)
                original = self.scheduler._verify_offline_repair
                extras = []
                def fault(controller):
                    if kind == "candidate": self.repo.write("calculator.py", "# foreign candidate\n")
                    elif kind == "environment":
                        extra = patch("environment.compare", return_value={"decision": "UNSAFE"})
                        extra.start(); extras.append(extra)
                    elif kind == "budget": self.scheduler.clock = lambda: controller.mode["task_deadline"] + 1
                    elif kind == "session": controller.mode["controller_session"]["controller_sha256"] = "0" * 64
                    else: self.store.load()
                    return original(controller)
                try:
                    with patch.object(self.scheduler, "_verify_offline_repair", side_effect=fault), \
                            self.assertRaises((WorkUnitSchedulingError, durable.DurableError)):
                        self.qualify()
                finally:
                    for extra in extras: extra.stop()
                loaded = self.reload()
                self.assertNotIn("repair_verification", loaded.state)
                self.assertEqual(loaded.state["repair_dispatch"]["phase"], "COMPLETE")
                children = loaded.state["supervision"]["children"].values()
                self.assertFalse(any(c.get("invocation", {}).get("attempt_id") == "unit-1-attempt-2" for c in children))
                self.assert_no_adoption(loaded)

    def test_candidate_drift_after_invocation_intent_never_launches_verifier(self):
        original, launch = self.store.append, winprocess.ControllerVerifier.launch
        second_launches = []
        def fault(event, **fields):
            result = original(event, **fields)
            t = fields.get("transition") or {}
            if t.get("event") == "invocation_intent" and "repair_verification" in fields.get("state", {}):
                self.repo.write("calculator.py", "# changed after durable intent\n")
            return result
        def tracked(native, argv, cwd, streams):
            if "repair_verification" in self.store.state: second_launches.append(argv)
            return launch(native, argv, cwd, streams)
        with patch.object(self.store, "append", side_effect=fault), patch.object(winprocess.ControllerVerifier, "launch", tracked), \
                self.assertRaises(supervision.VerifierEvidenceError):
            self.qualify()
        self.assertEqual(second_launches, [])
        self.assertEqual(self.reload().state["repair_verification"]["phase"], "OBSERVING")
        self.assert_no_adoption()

    def test_real_cumulative_budget_expiry_prevents_second_verifier_intent(self):
        self.root = Path(self.temp.name) / "expired"; self.root.mkdir()
        self.prepare(single=True, unit_timeout=30)
        original = self.scheduler._verify_offline_repair
        def expire(controller):
            self.assertIs(self.scheduler.clock, time.monotonic)
            grant = self.store.state["repair_authorizations"]["repair-" + controller.mode["recovery_id"]]
            deadline = min(controller.mode["task_deadline"], grant["budget"]["unit_deadline"])
            while time.monotonic() <= deadline:
                time.sleep(min(.05, max(.001, deadline - time.monotonic())))
            return original(controller)
        with patch.object(self.scheduler, "_verify_offline_repair", side_effect=expire), \
                self.assertRaisesRegex(WorkUnitSchedulingError, "cumulative budget exhausted"):
            self.qualify()
        loaded = self.reload()
        self.assertEqual(loaded.state["repair_dispatch"]["result"]["outcome"], "executor_succeeded")
        self.assertNotIn("repair_verification", loaded.state)
        self.assertFalse(any(c.get("invocation", {}).get("attempt_id") == "unit-1-attempt-2"
                             for c in loaded.state["supervision"]["children"].values()))
        self.assertIs(self.scheduler.clock, time.monotonic)
        self.assertEqual(len(loaded.state["work_units"]["units"]["unit-1"]["attempts"]), 2)
        self.assert_no_adoption(loaded)

    def test_empty_directory_scope_mutation_cannot_pass(self):
        self.root = Path(self.temp.name) / "directory"; self.root.mkdir()
        self.prepare(single=True, scripts={"test_calculator.py":
            "from pathlib import Path\nimport unittest\nimport calculator\n"
            "if calculator.multiply(2,3)==6: Path('outside-scope').mkdir()\n"
            "class Check(unittest.TestCase):\n def test_value(self): self.assertEqual(calculator.multiply(2,3),6)\n"})
        self.qualify()
        self.assertEqual(self.second()["outcome"], "failed")
        self.assertEqual(self.second()["failure_classification"], "verifier_tampered_repo")
        self.assertEqual(self.store.state["work_units"]["units"]["unit-1"]["status"], "UNIT_FAILED")
        self.assert_no_adoption()

    def test_scope_mutation_during_second_verification_fails_terminally(self):
        # Fixed verifier fixture branches on the repaired source, then contaminates
        # an allowed file. Even an allowed write invalidates verification evidence.
        self.root = Path(self.temp.name) / "scope"; self.root.mkdir()
        self.prepare(single=True, scripts={"test_calculator.py":
            "from pathlib import Path\nimport unittest\nimport calculator\n"
            "if calculator.multiply(2,3)==6: Path('calculator.py').write_text('# contaminated\\n')\n"
            "class Check(unittest.TestCase):\n def test_value(self): self.assertEqual(calculator.multiply(2,3),6)\n"})
        self.qualify()
        unit = self.reload().state["work_units"]["units"]["unit-1"]
        self.assertEqual(unit["status"], "UNIT_FAILED")
        self.assertEqual(unit["attempts"][1]["failure_classification"], "verifier_tampered_repo")
        self.assertEqual(len(unit["attempts"]), 2)
        self.assert_no_adoption()

    def test_native_second_timeout_drains_descendant(self):
        self.root = Path(self.temp.name) / "timeout"; self.root.mkdir()
        marker = self.root / "descendant.json"
        self.prepare(single=True, timeout=25, scripts={"test_calculator.py":
            "import calculator,unittest,subprocess,sys,time,json\nfrom pathlib import Path\n"
            "if calculator.multiply(2,3)==6:\n"
            " p=subprocess.Popen([sys.executable,'-I','-B','-c','import time; time.sleep(120)'],close_fds=True)\n"
            f" Path({str(marker)!r}).write_text(json.dumps({{'pid':p.pid}}))\n"
            " time.sleep(120)\n"
            "class Check(unittest.TestCase):\n def test_value(self): self.assertEqual(calculator.multiply(2,3),6)\n"})
        self.qualify()
        self.assert_gone(json.loads(marker.read_text())["pid"])
        self.assertEqual(self.second()["outcome"], "timeout")
        self.assertEqual(self.store.state["work_units"]["units"]["unit-1"]["status"], "UNIT_FAILED")
        obs = self.second()["repair_observation"]["observations"]["check-calc"]["native"]
        self.assertEqual(obs["cleanup"], "observed_empty")
        self.assert_gone(obs["verifier"]["pid"])
        self.assert_no_adoption()

    def test_ambiguous_commits_never_resume_or_append_again(self):
        for boundary in ("STARTED", "INTENT", "BOUND", "RELEASED", "EXIT", "NATIVE_COMPLETE", "COMPLETE"):
            with self.subTest(boundary=boundary):
                self.root = Path(self.temp.name) / boundary; self.root.mkdir()
                self.prepare(single=True)
                original, resume = durable.atomic_write, winprocess.ControllerVerifier.resume
                failures, resumed = [], []
                def fault(path, payload):
                    if Path(path).name == "state.json":
                        state = json.loads(payload)
                        v = state.get("repair_verification")
                        t = state.get("supervision_event") or {}
                        child = state.get("supervision", {}).get("children", {}).get(t.get("child_id"), {})
                        is_second = child.get("invocation", {}).get("attempt_id") == "unit-1-attempt-2"
                        hit = v and ((boundary in {"STARTED", "COMPLETE"} and v["phase"] == boundary)
                            or (boundary not in {"STARTED", "COMPLETE"} and is_second and t.get("event") == {
                                "INTENT": "invocation_intent", "BOUND": "invocation_bound", "RELEASED": "invocation_release",
                                "EXIT": "process_exit", "NATIVE_COMPLETE": "invocation_complete"}[boundary]))
                        if hit:
                            failures.append((self.store.directory / "events.jsonl").read_bytes())
                            raise OSError("uncertain qualification commit")
                    return original(path, payload)
                def tracked(native, identity):
                    if "repair_verification" in self.store.state: resumed.append(native.pid)
                    return resume(native, identity)
                with patch("durable.atomic_write", side_effect=fault), patch.object(winprocess.ControllerVerifier, "resume", tracked), \
                        self.assertRaises((OSError, supervision.VerifierEvidenceError)):
                    self.qualify()
                self.assertEqual(len(failures), 1)
                self.assertEqual((self.store.directory / "events.jsonl").read_bytes(), failures[0])
                self.assertTrue(self.store._repair_write_failed)
                self.assertEqual(len(resumed), 0 if boundary in {"STARTED", "INTENT", "BOUND", "RELEASED"} else 1)
                for c in self.reload().state["supervision"]["children"].values():
                    if c["pid"]: self.assert_gone(c["pid"])
                self.assert_no_adoption()

    def test_torn_verification_wal_refuses_replay_without_truncation(self):
        original = self.store.append
        def fault(event, **fields):
            if "repair_verification" in fields:
                with (self.store.directory / "events.jsonl").open("ab") as stream:
                    stream.write(b'{"sequence":'); stream.flush(); os.fsync(stream.fileno())
                raise OSError("torn verification intent")
            return original(event, **fields)
        with patch.object(self.store, "append", side_effect=fault), self.assertRaises(OSError): self.qualify()
        wal = (self.store.directory / "events.jsonl").read_bytes()
        with self.assertRaises(durable.DurableError): self.reload()
        self.assertEqual((self.store.directory / "events.jsonl").read_bytes(), wal)

    def test_closed_schema_native_references_and_history_refuse_tampering(self):
        self.qualify()
        state = self.reload().state
        for field, value in (("schema_version", True), ("phase", "ACCEPTED"), ("run_id", "foreign"),
                ("candidate_fingerprint", "0" * 64), ("tree_sha256", "0" * 64),
                ("dispatch_evidence", {}), ("checked_clock", float("nan")), ("attempt_sha256", "0" * 64)):
            altered = copy.deepcopy(state); altered["repair_verification"][field] = value
            with self.subTest(field=field), self.assertRaises(durable.DurableError): durable.validate_state(altered)
        records = copy.deepcopy(self.store.records)
        last = records[-1]
        last["state"]["repair_verification"]["dispatch_wal"] = {k: records[0][k] for k in ("sequence", "sha256")}
        with self.assertRaises(durable.DurableError): durable._validate_repair_history(records, self.store.read_evidence)
        for field, value in (("child_id", "foreign"), ("invocation_id", "foreign"), ("wal", {})):
            altered = copy.deepcopy(state)
            a = self.second(altered); a["repair_observation"]["observations"]["check-calc"]["binding"][field] = value
            altered["repair_verification"]["attempt_sha256"] = durable.digest(a)
            work_units.seal_unit_state(altered["work_units"]["units"]["unit-1"])
            work_units._reseal_section(altered["work_units"])
            with self.subTest(binding=field), self.assertRaises(durable.DurableError): durable.validate_state(altered)
        altered = copy.deepcopy(state)
        a = self.second(altered); a["repair_observation"]["observations"]["check-calc"]["native"]["cleanup"] = "not_observed"
        altered["repair_verification"]["attempt_sha256"] = durable.digest(a)
        work_units.seal_unit_state(altered["work_units"]["units"]["unit-1"])
        work_units._reseal_section(altered["work_units"])
        with self.assertRaises(durable.DurableError): durable.validate_state(altered)
        for field, value in (("after_fs_snapshot", []), ("after_tree_sha256", None), ("native", {})):
            altered = copy.deepcopy(state)
            a = self.second(altered); a["repair_observation"]["observations"]["check-calc"][field] = value
            altered["repair_verification"]["attempt_sha256"] = durable.digest(a)
            work_units.seal_unit_state(altered["work_units"]["units"]["unit-1"])
            work_units._reseal_section(altered["work_units"])
            with self.subTest(observation=field), self.assertRaises(durable.DurableError): durable.validate_state(altered)
        with self.assertRaises(durable.DurableError): self.store.commit(repair_verification=state["repair_verification"])
        with self.assertRaises(durable.DurableError): self.store._persist_repair_verification({})

    def test_duplicate_concurrent_and_ordinary_invocation_cannot_enter_second_sequence(self):
        original = self.scheduler._run_policy_b_shadow_verification
        refusals = []
        def sequence(unit, section, attempt_id, elapsed):
            if attempt_id.endswith("-2"):
                controller = self.scheduler._repair_controller
                def duplicate():
                    try: self.scheduler._verify_offline_repair(controller)
                    except WorkUnitSchedulingError as exc: refusals.append(exc)
                thread = threading.Thread(target=duplicate)
                thread.start(); thread.join(15)
                self.assertFalse(thread.is_alive())
                with self.assertRaises(manager.Stop):
                    supervision._VerifierInvocation(self.scheduler, "unit-1", attempt_id, "check-calc")
                with self.assertRaises(durable.DurableError):
                    supervision._VerifierInvocation(self.scheduler, "unit-1", attempt_id, "check-calc", _repair_controller={})
            return original(unit, section, attempt_id, elapsed)
        with patch.object(self.scheduler, "_run_policy_b_shadow_verification", side_effect=sequence): self.qualify()
        self.assertEqual(len(refusals), 1)
        self.assertEqual(self.second()["outcome"], "passed")

    def test_invalid_actions_refused_before_attempt_one(self):
        wal = (self.store.directory / "events.jsonl").read_bytes()
        for script in (None, {}, {"schema_version": 1, "actions": [{"op": "shell", "command": "python"}]},
                       {"schema_version": 1, "actions": [{"op": "emit", "text": "x" * 8192}]}):
            scheduler = WorkUnitScheduler(self.store, self.repo, supervisor=self.sup,
                                          agents=manager.Agents(None, self.executor, None, None))
            with self.assertRaises((WorkUnitSchedulingError, offline.OfflineExecutorError)):
                scheduler.qualify_offline_repair_recovery("unit-1", script)
            self.assertEqual((self.store.directory / "events.jsonl").read_bytes(), wal)
        self.assertEqual(self.executor.calls, [])

    def test_real_controller_crashes_at_verification_boundaries_never_relaunch(self):
        child_script = r'''
import os,sys,json,time
from pathlib import Path
from unittest.mock import patch
from test_offline_repair_verification import OfflineRepairVerificationTests, REPAIR
import winprocess
t=OfflineRepairVerificationTests('test_invalid_actions_refused_before_attempt_one')
phase=sys.argv[2]
t.root=Path(sys.argv[1])
scripts=None
descendant=t.root/'descendant.json'
if phase=='RUNNING':
    scripts={'test_calculator.py':
        'import calculator,unittest,subprocess,sys,time,json\nfrom pathlib import Path\n'
        'if calculator.multiply(2,3)==6:\n'
        " p=subprocess.Popen([sys.executable,'-I','-B','-c','import time; time.sleep(120)'],close_fds=True)\n"
        +f" Path({str(descendant)!r}).write_text(json.dumps({{'pid':p.pid}}))\n"
        +' time.sleep(120)\n'
        +'class Check(unittest.TestCase):\n def test_value(self): self.assertEqual(calculator.multiply(2,3),6)\n'}
t.prepare(single=True,scripts=scripts)
append,launch,resume=t.store.append,winprocess.ControllerVerifier.launch,winprocess.ControllerVerifier.resume
second=[]
def crash_append(event,**fields):
    value=append(event,**fields)
    state=fields.get('state',{})
    v=state.get('repair_verification')
    transition=fields.get('transition') or {}
    if v and ((phase in {'STARTED','COMPLETE'} and (fields.get('repair_verification') or {}).get('event')=='repair_verification_'+phase.lower()) or
              (phase in {'INTENT','BOUND','RELEASED','NATIVE_COMPLETE'} and transition.get('event')=={'INTENT':'invocation_intent','BOUND':'invocation_bound','RELEASED':'invocation_release','NATIVE_COMPLETE':'invocation_complete'}[phase])):
        os._exit(73)
    return value
def crash_launch(native,argv,cwd,streams):
    value=launch(native,argv,cwd,streams)
    if 'repair_verification' in t.store.state:
        second.append(native.pid)
        Path(t.root,'child.json').write_text(json.dumps({'pid':native.pid}))
        if phase=='CREATED': os._exit(73)
    return value
def crash_resume(native,identity):
    value=resume(native,identity)
    if native.pid in second and phase=='RUNNING':
        until=time.monotonic()+10
        while not descendant.exists() and time.monotonic()<until: time.sleep(.02)
        if not descendant.exists(): raise AssertionError('No observed running descendant')
        os._exit(73)
    return value
with patch('harness.Gateway',side_effect=AssertionError('No models')),patch('durable.Gateway',side_effect=AssertionError('No models')),patch('socket.socket',side_effect=AssertionError('No network')),patch('socket.create_connection',side_effect=AssertionError('No network')),patch.object(t.store,'append',side_effect=crash_append),patch.object(winprocess.ControllerVerifier,'launch',crash_launch),patch.object(winprocess.ControllerVerifier,'resume',crash_resume):
    t.qualify()
'''
        for phase in ("STARTED", "INTENT", "CREATED", "BOUND", "RELEASED", "RUNNING", "NATIVE_COMPLETE", "COMPLETE"):
            with self.subTest(phase=phase):
                root = Path(self.temp.name) / phase; root.mkdir()
                process = subprocess.run([sys.executable, "-B", "-c", child_script, str(root), phase], capture_output=True, timeout=180)
                self.assertEqual(process.returncode, 73, process.stderr.decode(errors="replace"))
                marker = root / "child.json"
                if marker.exists(): self.assert_gone(json.loads(marker.read_text())["pid"])
                descendant = root / "descendant.json"
                if phase == "RUNNING": self.assert_gone(json.loads(descendant.read_text())["pid"])
                self.store = durable.Store(root / "runs", "shadow-test")
                loaded = self.reload()
                self.assertEqual(loaded.state["repair_verification"]["phase"], "COMPLETE" if phase == "COMPLETE" else "STARTED" if phase == "STARTED" else "OBSERVING")
                self.assertEqual(len(loaded.state["work_units"]["units"]["unit-1"]["attempts"]), 2)
                self.repo.root = Path(loaded.state["target_repository"])
                self.assert_no_adoption(loaded)


