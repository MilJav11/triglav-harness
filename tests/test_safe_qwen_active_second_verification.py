"""ACTIVE v7: real Windows workers/Jobs/verifiers, deterministic fake Gateway only."""
from contextlib import ExitStack
import copy
import json
import math
import os
from pathlib import Path
import time
import unittest
from unittest.mock import patch

import durable
import manager
import safe_qwen_active_recovery as active
import safe_qwen_active_verification as verification
import supervision
import winprocess
import work_units
import work_unit_scheduler
import test_safe_qwen_active_dispatch as fixtures
from test_supervised_safe_qwen import wire


_ALLOWED_OUTCOMES = frozenset({"passed", "failed", "timeout"})
_ALLOWED_VERIFIERS = frozenset({"check-1", "check-2"})
_ALLOWED_TERMINATIONS = frozenset({"natural", "timeout", "forced", "observation_failed", "launch_failed"})
_ALLOWED_TERMINATION_BASES = frozenset({
    "signaled_without_controller_termination",
    "controller_deadline",
    "controller_cancel",
})
_ALLOWED_CLEANUPS = frozenset({"observed_empty", "incomplete", "not_observed"})


def _allow(val, allowed, default="UNKNOWN"):
    return val if type(val) is str and val in allowed else default


def _num(val, digits=3):
    return round(val, digits) if type(val) in (int, float) and math.isfinite(val) else "UNKNOWN"


def _second_verification_diagnostic(case, result=None, error=None, wall_seconds=None):
    """Sanitized diagnostic (<=4096 bytes) extracted from returned in-memory evidence."""
    diag = {
        "schema_version": 1,
        "test_status": "FAIL" if error is not None else "PASS",
        "error_type": type(error).__name__ if error is not None else None,
        "wall_seconds": _num(wall_seconds, 3),
        "second_verification_outcome": "UNKNOWN",
        "verification_elapsed_seconds": "UNKNOWN",
        "controller_deadline": "UNKNOWN",
        "controller_deadline_margin_seconds": "UNKNOWN",
        "timeout_owner": "UNKNOWN",
        "verifier_observations": {},
    }
    try:
        active_data = {}
        store = getattr(case, "store", None)
        if store is not None:
            state = getattr(store, "state", None)
            if isinstance(state, dict):
                active_data = state.get(active.KEY) or {}

        v = None
        if isinstance(result, dict) and isinstance(result.get("verification"), dict):
            v = result["verification"]
        elif isinstance(active_data.get("second_verification"), dict):
            v = active_data["second_verification"]

        if isinstance(v, dict):
            diag["second_verification_outcome"] = _allow(v.get("outcome"), _ALLOWED_OUTCOMES)
            diag["verification_elapsed_seconds"] = _num(v.get("elapsed_seconds"), 3)

            observations = v.get("observations")
            if isinstance(observations, dict):
                for vid, row in observations.items():
                    if str(vid) not in _ALLOWED_VERIFIERS or not isinstance(row, dict):
                        continue
                    native = row.get("native") if isinstance(row.get("native"), dict) else {}
                    out = row.get("output") if isinstance(row.get("output"), dict) else {}
                    code = native.get("observed_exit_code")
                    diag["verifier_observations"][str(vid)] = {
                        "termination": _allow(native.get("termination"), _ALLOWED_TERMINATIONS),
                        "termination_basis": _allow(native.get("termination_basis"), _ALLOWED_TERMINATION_BASES),
                        "observed_exit_code": code if type(code) is int else "UNKNOWN",
                        "cleanup": _allow(native.get("cleanup"), _ALLOWED_CLEANUPS),
                        "duration_seconds": _num(out.get("duration_seconds"), 3),
                    }

            ident = active_data.get("identity") if isinstance(active_data.get("identity"), dict) else {}
            deadline = ident.get("deadline")
            checked = v.get("checked_clock")
            if type(deadline) in (int, float) and math.isfinite(deadline):
                diag["controller_deadline"] = _num(deadline, 3)
                if type(checked) in (int, float) and math.isfinite(checked):
                    margin = deadline - checked
                    diag["controller_deadline_margin_seconds"] = _num(margin, 3)

            outcome = diag["second_verification_outcome"]
            if outcome == "passed":
                diag["timeout_owner"] = "none"
            elif outcome == "timeout":
                margin_val = diag["controller_deadline_margin_seconds"]
                if isinstance(margin_val, (int, float)) and margin_val <= 0:
                    diag["timeout_owner"] = "controller_deadline"
                else:
                    diag["timeout_owner"] = "UNKNOWN"
            else:
                diag["timeout_owner"] = "UNKNOWN"
    except Exception as parse_exc:
        diag["diagnostic_extractor_error"] = type(parse_exc).__name__

    try:
        encoded = json.dumps(diag, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 4096:
            fallback = {
                "schema_version": 1,
                "test_status": diag["test_status"],
                "second_verification_outcome": diag["second_verification_outcome"],
                "truncated": True,
            }
            encoded = json.dumps(fallback, sort_keys=True, separators=(",", ":"))
        return encoded
    except Exception:
        return '{"schema_version":1,"test_status":"UNKNOWN","fallback":true}'



@unittest.skipUnless(os.name == "nt", "Real native Windows lifecycle required")
class SecondVerificationTests(unittest.TestCase):
    prepare_timeout_fixture = fixtures.ActiveDispatchTests.prepare_timeout_fixture
    stop_fixture = fixtures.ActiveDispatchTests.stop_fixture
    servers = fixtures.ActiveDispatchTests.servers
    reload = fixtures.ActiveDispatchTests.reload

    def close_supervisor(self):
        # Real WAL validation may still be in flight on this Windows host.
        # Wait for observed thread termination; never assert cleanup from timeout alone.
        if self.store._repair_write_failed:
            error = RuntimeError("Expected uncertain WAL stop")
            self.sup.shutdown(RuntimeError, error, None, timeout=60)
        else:
            self.sup.shutdown(timeout=60)
        self.assertFalse(self.sup.thread.is_alive())

    def setUp(self):
        create = manager.create_run
        import test_safe_qwen_integration as integration
        prepare = integration.IntegrationTests.prepared
        def bounded(fixture, *args, **kwargs):
            # Full real native sequence + repeated durable validation on Windows.
            # This larger offline fixture budget is frozen BEFORE either ledger exists.
            fixture.cfg["timeout_seconds"] = 600
            fixture.data["unit_timeout_seconds"] = 600
            fixture.path.write_text(json.dumps(fixture.cfg))
            fixture.policy_path.write_text(json.dumps(fixture.data))
            return prepare(fixture, *args, **kwargs)
        special = self._testMethodName in {"test_timeout_drains_native_descendant", "test_two_verifiers_take_complete_snapshots"}
        def prepared(store, repo, *args, **kwargs):
            if store.directory.name == "active-first":
                kwargs["verifier_registry"] = copy.deepcopy(kwargs["verifier_registry"])
                for row in kwargs["verifier_registry"].values(): row["timeout_seconds"] = 120
            if special and store.directory.name == "active-first":
                kwargs["verifier_registry"] = copy.deepcopy(kwargs["verifier_registry"])
                if self._testMethodName == "test_two_verifiers_take_complete_snapshots":
                    kwargs["work_units"] = copy.deepcopy(kwargs["work_units"])
                    kwargs["work_units"][0]["verifier_ids"] = ["check-1", "check-2"]
                    kwargs["verifier_registry"]["check-2"] = copy.deepcopy(kwargs["verifier_registry"]["check-1"])
                else:
                    import subprocess
                    self.marker = repo.root.parent / "second-descendant.json"
                    (repo.root / "test_calculator.py").write_text(
                        "import unittest,subprocess,sys,time,json,calculator\nfrom pathlib import Path\n"
                        "class Check(unittest.TestCase):\n def test_multiply(self):\n"
                        "  self.assertEqual(calculator.multiply(2,3),6)\n"
                        "  p=subprocess.Popen([sys.executable,'-I','-B','-c','import time;time.sleep(120)'],close_fds=True)\n"
                        f"  Path({str(self.marker)!r}).write_text(json.dumps({{'pid':p.pid}}))\n  time.sleep(120)\n")
                    # Disposable fixture, before this run exists; preserve clean-repository gates.
                    self.assertTrue(repo.root.resolve().is_relative_to(Path(__import__('tempfile').gettempdir()).resolve()))
                    for argv in (("add", "test_calculator.py"), ("commit", "-qm", "offline second verification timeout fixture")):
                        subprocess.run(["git", "-C", str(repo.root), "-c", f"safe.directory={repo.root}",
                                        "-c", "user.name=Offline fixture", "-c", "user.email=fixture@localhost", *argv],
                                       check=True, capture_output=True)
                    kwargs["verifier_registry"]["check-1"]["timeout_seconds"] = 90
            return create(store, repo, *args, **kwargs)
        isolate_snapshot_heartbeat = self._testMethodName == "test_two_verifiers_take_complete_snapshots"
        patches = [
            patch("manager.create_run", side_effect=prepared),
            patch.object(integration.IntegrationTests, "prepared", bounded),
        ]
        if isolate_snapshot_heartbeat:
            # Isolate two-verifier snapshot semantics from periodic heartbeat churn.
            # Real native verifiers and Job containment are preserved, while eliminating
            # background heartbeat WAL inflation during multi-verifier snapshot evaluation.
            orig_init = supervision.Supervisor.__init__
            def sup_init(sup_self, *args, **kwargs):
                orig_init(sup_self, *args, **kwargs)
                sup_self._pulse_interval = 600
            patches.append(patch.object(supervision.Supervisor, "__init__", sup_init))

        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            fixtures.ActiveDispatchTests.setUp(self)
        if isolate_snapshot_heartbeat:
            self.sup._pulse_interval = 600
        import subprocess
        popen = subprocess.Popen
        def inert_model(argv, *args, **kwargs):
            if type(argv) is list and argv[-1] == "import time;time.sleep(60)":
                argv = [*argv[:-1], "import time;time.sleep(900)"]
            return popen(argv, *args, **kwargs)
        item = patch("subprocess.Popen", side_effect=inert_model)
        item.start(); self.addCleanup(item.stop)

    def run_verified(self):
        return self.scheduler.qualify_safe_qwen_repair_recovery("safe-qwen-task", gateway=self.gateway, timeout_seconds=600)

    def test_repaired_file_passes_with_exact_wal_and_native_bindings(self):
        import _winapi
        launches, resumes, owners = [], [], []
        observed = {}
        launch, resume = winprocess.ControllerVerifier.launch, winprocess.ControllerVerifier.resume
        drain = winprocess.ControllerVerifier.drain
        def created(native, *args):
            owners.append(id(self.store._safe_qwen_active_controller))
            state = self.reload().state
            if len(launches) == 3:
                v = state[active.KEY]["second_verification"]
                self.assertEqual(v["phase"], "OBSERVING")
                self.assertIsNone(self.proc)  # Inert owned model is already unloaded.
                self.assertTrue(any((c.get("invocation") or {}).get("phase") == "INTENT"
                                    for c in state["supervision"]["children"].values()))
            result = launch(native, *args)
            # Retain the original process object, never reattach a historical PID.
            # Query/synchronize only; no inherited process or Job handles.
            current = _winapi.GetCurrentProcess()
            handle = _winapi.DuplicateHandle(current, native.process, current, 0x1000 | 0x100000, False, 0)
            self.addCleanup(_winapi.CloseHandle, handle)
            self.assertFalse(os.get_handle_inheritable(handle))
            self.assertEqual(winprocess._handle_identity(native.k, handle), native.identity)
            observed[native] = dict(handle=handle, process=dict(pid=native.pid, identity=copy.deepcopy(native.identity)), drained=False)
            launches.append(native.pid)
            return result
        def drained(native, *args, **kwargs):
            result = drain(native, *args, **kwargs)  # Actual Job emptiness, including descendants.
            self.assertEqual(_winapi.WaitForSingleObject(observed[native]["handle"], 0), 0)
            observed[native]["drained"] = True
            return result
        def released(native, identity):
            if len(launches) == 4:
                children = self.reload().state["supervision"]["children"].values()
                child = next(c for c in children if c["pid"] == native.pid)
                self.assertEqual(child["invocation"]["phase"], "RELEASED")
                self.assertEqual(child["identity"], identity)
                native.verify(identity)
            resumes.append(native.pid); return resume(native, identity)
        with patch.object(winprocess.ControllerVerifier, "launch", created), patch.object(winprocess.ControllerVerifier, "resume", released), \
                patch.object(winprocess.ControllerVerifier, "drain", drained):
            result = self.run_verified()
        state = self.reload().state; d = state[active.KEY]; v = result["verification"]
        self.assertEqual((d["schema_version"], d["mode"]), (7, verification.SECOND_MODE))
        self.assertEqual(d["verification"]["outcome"], "failed")
        self.assertEqual((v["phase"], v["outcome"]), ("COMPLETE", "passed"))
        self.assertEqual(v, d["second_verification"])
        self.assertEqual(v["before"], v["after"])
        self.assertEqual(v["files_before"], v["files_after"])
        self.assertTrue(any(p.startswith(".git/") for p in v["files_before"]))
        self.assertIn("return a * b", (self.repo.root / "calculator.py").read_text())
        self.assertEqual(len(launches), 4)  # Two logical workers + two native verifiers.
        self.assertEqual(resumes, launches)
        self.assertEqual(len(set(owners)), 1, "One uninterrupted registered controller owns all four launches")
        evidence = [d["result"]["evidence"], d["verification"]["observations"]["check-1"]["native"],
                    d["dispatch"]["result"]["evidence"], v["observations"]["check-1"]["native"]]
        self.assertEqual(len(observed), 4)
        for (native, observation), facts in zip(observed.items(), evidence):
            self.assertEqual(observation["process"], facts.get("process", facts.get("verifier")))
            self.assertTrue(observation["drained"])
            self.assertEqual(facts["cleanup"], "observed_empty")
            self.assertEqual(_winapi.WaitForSingleObject(observation["handle"], 0), 0)
            self.assertEqual(_winapi.GetExitCodeProcess(observation["handle"]), facts["observed_exit_code"])
            self.assertIsNone(winprocess._handle_identity(native.k, observation["handle"]))
        ref = v["binding"]["dispatch_wal"]
        record = self.store.records[ref["sequence"] - 1]
        self.assertEqual(ref["sha256"], record["sha256"])
        self.assertEqual(record[active.MARKER], dict(event="AI_COMPLETE", execution_id=v["execution_id"]))
        self.assertEqual(v["binding"]["consumption_wal"], d["dispatch"]["consumption_wal"])
        for row in v["observations"].values():
            self.assertEqual(row["native"]["cleanup"], "observed_empty")
            self.assertEqual(row["after"], v["before"])
            self.assertEqual(row["files_after"], v["files_before"])
            for ev, native_ref in row["binding"]["wal"].items():
                r = self.store.records[native_ref["sequence"] - 1]
                self.assertEqual(r["sha256"], native_ref["sha256"])
                self.assertEqual(r["transition"], dict(event=ev, child_id=row["binding"]["child_id"]))
                self.assertGreater(native_ref["sequence"], ref["sequence"])
        unit = state["work_units"]["units"]["safe-qwen-task"]
        self.assertEqual(len(unit["attempts"]), 2)
        self.assertEqual(unit["status"], "VERIFYING")
        self.assertIsNone(unit["current_attempt"])
        self.assertEqual(unit["attempts"][1]["outcome"], "passed")
        self.assertEqual(d["identity"]["worker"]["limits"]["max_attempts"], 1)
        consumed = next(iter(state["repair_authorizations"].values()))
        grant_state = self.store.records[consumed["consumption"]["grant_wal"]["sequence"] - 1]["state"]
        self.assertEqual(unit["attempts"][0], grant_state["work_units"]["units"][unit["unit_id"]]["attempts"][0])
        self.assertIsNone(unit["result"])
        self.assertIsNone(state["last_verified_checkpoint"])
        self.assertEqual(state["verified_progress"], [])
        self.assertNotIn("terminal_acceptance", state)

    def test_second_verifier_failure_is_terminal_no_third_attempt(self):
        self.responses = [wire(self.submit), wire(self.submit)]  # A2 succeeds without fixing the actual defect.
        result = self.run_verified()
        self.assertEqual(result["verification"]["outcome"], "failed")
        unit = self.reload().state["work_units"]["units"]["safe-qwen-task"]
        self.assertEqual(unit["status"], "UNIT_FAILED")
        self.assertEqual(len(unit["attempts"]), 2)
        self.assertIsNone(unit["current_attempt"])
        with self.assertRaises(work_units.WorkUnitError):
            work_units.create_attempt(copy.deepcopy(unit), "safe-qwen-task-attempt-3")

    def test_timeout_drains_native_descendant(self):
        result = self.run_verified(); v = result["verification"]
        self.assertEqual(v["outcome"], "timeout")
        row = v["observations"]["check-1"]
        self.assertEqual(row["native"]["cleanup"], "observed_empty")
        self.assertEqual(row["native"]["termination"], "timeout")
        self.assertIsNone(winprocess.identity(row["native"]["verifier"]["pid"]))
        self.assertTrue(self.marker.exists())
        self.assertIsNone(winprocess.identity(json.loads(self.marker.read_text())["pid"]))
        self.assertEqual(self.reload().state["work_units"]["units"]["safe-qwen-task"]["status"], "UNIT_FAILED")

    def test_two_verifiers_take_complete_snapshots(self):
        t0 = time.monotonic()
        result = None
        error = None
        try:
            result = self.run_verified()
            v = result["verification"]
            self.assertEqual(list(v["observations"]), ["check-1", "check-2"])
            self.assertEqual(v["outcome"], "passed")
            self.assertEqual(v["before"], v["after"])
            for row in v["observations"].values():
                self.assertEqual(row["after"], v["before"])
                self.assertEqual(row["files_after"], v["files_before"])
        except BaseException as exc:
            error = exc
            raise
        finally:
            try:
                wall = time.monotonic() - t0
                diag = _second_verification_diagnostic(self, result=result, error=error, wall_seconds=wall)
                print(f"SECOND_VERIFICATION_DIAGNOSTIC {diag}", flush=True)
            except BaseException:
                pass


    def test_malformed_evidence_foreign_and_stale_wal_are_refused(self):
        self.run_verified(); state = self.reload().state
        for key, value in (("execution_id", "foreign"), ("dispatch_result_sha256", "0" * 64),
                           ("files_after", {}), ("outcome", "accepted")):
            bad = copy.deepcopy(state); bad[active.KEY]["second_verification"][key] = value
            with self.subTest(key=key), self.assertRaises(durable.DurableError): active.validate(bad)
        bad = copy.deepcopy(state)
        bad[active.KEY]["second_verification"]["observations"]["check-1"]["native"]["observed_exit_code"] = True
        with self.assertRaises(durable.DurableError): active.validate(bad)
        for foreign in (True, False):
            records = copy.deepcopy(self.store.records)
            r = next(r for r in records if r.get(active.MARKER, {}).get("event") == "VERIFY2_INTENT")
            b = r["state"][active.KEY]["second_verification"]["binding"]
            if foreign: b["dispatch_wal"]["sha256"] = "0" * 64
            else: b["dispatch_wal"] = copy.deepcopy(b["consumption_wal"])
            # Keep all local checks coherent: history must reject the actual WAL reference.
            with self.subTest(foreign=foreign), self.assertRaises(durable.DurableError):
                verification.validate_step(records[r["sequence"] - 2]["state"], r["state"], r[active.MARKER], records)

    def test_replay_never_launches_verification_or_checkpoint(self):
        owners = []; run = verification.run
        def observed(owner, **kwargs):
            if kwargs.get("second"): owners.append(owner)
            return run(owner, **kwargs)
        with patch.object(verification, "run", side_effect=observed): self.run_verified()
        self.close_supervisor()
        loaded = self.reload(); wal = loaded.directory / "events.jsonl"; before = wal.read_bytes()
        scheduler = work_unit_scheduler.WorkUnitScheduler(loaded, self.repo, supervisor=supervision.Supervisor(loaded))
        unit = loaded.state["work_units"]["units"]["safe-qwen-task"]
        import test_safe_qwen_active_first as first
        with patch.object(winprocess.ControllerVerifier, "launch", side_effect=AssertionError("Replay attempted native creation")):
            for op in (lambda: run(owners[0], second=True), lambda: run(loaded.state[active.KEY], second=True),
                       lambda: scheduler.qualify_safe_qwen_repair_recovery(unit["unit_id"], gateway=self.gateway),
                       lambda: scheduler.invoke_native_verifier(unit["unit_id"], unit["attempts"][1]["attempt_id"], "check-1"),
                       lambda: first.REAL_EXECUTE(scheduler, unit, loaded.state["work_units"], 3),
                       lambda: first.REAL_TERMINAL(loaded, "forbidden")):
                with self.assertRaises((durable.DurableError, manager.Stop, AttributeError)): op()
        self.assertEqual(wal.read_bytes(), before)

    def refuse_before_second(self, mutation):
        self.responses = [wire(self.submit), wire(self.submit)]
        run = verification.run
        def changed(owner, **kwargs):
            if kwargs.get("second"): mutation(owner)
            return run(owner, **kwargs)
        created = []; launch = winprocess.ControllerVerifier.launch
        def launched(native, *args):
            result = launch(native, *args); created.append(native.pid); return result
        with patch.object(verification, "run", side_effect=changed), patch.object(winprocess.ControllerVerifier, "launch", launched), \
                self.assertRaises((durable.DurableError, ValueError)):
            self.run_verified()
        self.assertEqual(len(created), 3)
        self.assertTrue(all(winprocess.identity(pid) is None for pid in created))
        self.assertIsNone(self.reload().state[active.KEY]["second_verification"])

    def test_candidate_drift_refuses_second_verification(self):
        self.refuse_before_second(lambda owner: (self.repo.root / "calculator.py").write_text("# drift\n"))

    def test_index_drift_refuses_second_verification(self):
        def drift(owner):
            import subprocess
            subprocess.run(["git", "-C", str(self.repo.root), "-c", f"safe.directory={self.repo.root}",
                            "update-index", "--chmod=+x", "calculator.py"],
                           check=True, capture_output=True)
        self.refuse_before_second(drift)

    def test_scope_drift_refuses_second_verification(self):
        def drift(owner):
            owner.scheduler.parent_scope = copy.deepcopy(owner.scheduler.parent_scope)
            owner.scheduler.parent_scope["allowed_paths"].append("foreign.py")
        self.refuse_before_second(drift)

    def test_exhausted_budget_refuses_second_verification(self):
        def exhausted(owner):
            item = patch.object(owner.scheduler, "remaining_task_budget", return_value=0)
            item.start(); self.addCleanup(item.stop)
        self.refuse_before_second(exhausted)

    def test_unclean_worker_ownership_refuses_second_verification(self):
        self.refuse_before_second(lambda owner: setattr(owner, "cleanup", None))

    def test_environment_drift_refuses_second_verification(self):
        fingerprint = durable.current_fingerprint
        def drift(owner):
            def changed(*args, **kwargs):
                value = fingerprint(*args, **kwargs)
                value["runtime"]["python"]["sha256"] = "0" * 64
                return value
            item = patch("durable.current_fingerprint", side_effect=changed)
            item.start(); self.addCleanup(item.stop)
        self.refuse_before_second(drift)

    def test_old_v6_dispatch_has_no_verification_authority(self):
        owners = []; execute = __import__('safe_qwen_active_dispatch').run
        def observed(owner):
            owners.append(owner); return execute(owner)
        with patch("safe_qwen_active_dispatch.run", side_effect=observed):
            self.scheduler.dispatch_safe_qwen_repair_recovery("safe-qwen-task", gateway=self.gateway, timeout_seconds=600)
        self.assertEqual(self.reload().state[active.KEY]["schema_version"], 6)
        with self.assertRaises(durable.DurableError): verification.run(owners[0], second=True)
        with self.assertRaises(durable.DurableError): self.run_verified()

    def test_drift_between_verifiers_cannot_seal_success(self):
        complete = supervision._VerifierInvocation.complete
        def mutate(inv):
            result = complete(inv)
            if inv.intent["attempt_id"].endswith("-2"):
                (self.repo.root / "foreign.txt").write_text("foreign verifier mutation\n")
            return result
        with patch.object(supervision._VerifierInvocation, "complete", mutate), self.assertRaises(durable.DurableError): self.run_verified()
        v = self.reload().state[active.KEY]["second_verification"]
        self.assertEqual(v["phase"], "OBSERVING")
        self.assertEqual(v["observations"], {})
