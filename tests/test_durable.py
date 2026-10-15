"""Deterministic persistence, crash-window, and real Git/workflow regressions."""
import argparse
import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import durable
import harness
from test_harness import CONFIG


TASK = "Implement multiply"
COMMANDS = [["python", "-m", "unittest", "discover"]]
FIX = "def multiply(a, b):\n    return a * b\n"


class FakeGateway:
    """Only model responses are simulated; file writes, Git and tests are real."""
    def __init__(self, config, log):
        self.active_role = None
        self.calls = []
        self.actions = iter([
            {"action": "read_file", "path": "calculator.py"},
            {"action": "write_file", "path": "calculator.py", "content": FIX},
            {"action": "done", "summary": "HIDDEN_MODEL_SUMMARY"},
        ])

    def unload(self):
        self.calls.append("unload")

    def chat(self, role, messages, phase, max_tokens=1300):
        self.calls.append((role, phase))
        if phase == "plan":
            return "HIDDEN_MODEL_PLAN"
        if phase == "review":
            return '{"approved":true,"findings":"HIDDEN_REVIEW_TEXT"}'
        if phase == "critic":
            return json.dumps({"findings": [], "uncertainties": [], "evidence_reviewed": ["tests and diff"],
                               "summary": "No concrete defects found"})
        return json.dumps(next(self.actions))


def fixture(root):
    repo_path = root / "repo"
    repo_path.mkdir()
    (repo_path / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    (repo_path / "calculator.py").write_text("def multiply(a, b):\n    return 0\n", encoding="utf-8")
    (repo_path / "test_calculator.py").write_text(
        "import unittest\nfrom calculator import multiply\nclass T(unittest.TestCase):\n"
        "    def test_product(self): self.assertEqual(multiply(2, 3), 6)\n", encoding="utf-8")
    def git(*args):
        return subprocess.run(["git", "-C", str(repo_path), "-c", f"safe.directory={repo_path}",
                               "-c", "user.name=Durable test", "-c", "user.email=test@localhost", *args],
                              check=True, capture_output=True)
    git("init", "-q")
    git("add", ".")
    git("commit", "-qm", "baseline")
    config = copy.deepcopy(CONFIG)
    config.update(max_retries=0, roles={**CONFIG["roles"], "critic": "llm-critic"})
    log = harness.EventLog(root / "preflight.jsonl")
    repo = harness.Repository(repo_path, config, log)
    store = durable.Store(root / "runs", "test-run")
    store.create(repo, TASK, COMMANDS, config)
    return repo, store, git


def crash_child(root, phase):
    root = Path(root)
    store = durable.Store(root / "runs", "test-run")
    store.load()
    original_write = durable.DurableRepository.write
    original_verify = durable.DurableWorkflow.verify
    original_atomic = durable.atomic_write
    original_append = durable.Store.append

    def write(self, *args):
        original_write(self, *args)
        if phase == "after_write":
            os._exit(73)

    def verify(self, *args):
        if phase == "during_verify":
            self.store.commit("VERIFYING", current_step="verification")
            os._exit(73)
        return original_verify(self, *args)

    def atomic(path, data):
        if phase == "after_commit" and path.name == "state.json" and json.loads(data)["status"] == "VERIFIED":
            os._exit(73)
        original_atomic(path, data)

    def append(self, event, **fields):
        if phase == "before_commit" and event == "state_committed" and fields["state"]["status"] == "VERIFIED":
            os._exit(73)
        return original_append(self, event, **fields)

    with patch("durable.Gateway", FakeGateway), patch.object(durable.DurableRepository, "write", write), \
         patch.object(durable.DurableWorkflow, "verify", verify), patch("durable.atomic_write", atomic), \
         patch.object(durable.Store, "append", append):
        if phase == "before_implementation":
            with patch.object(durable.DurableWorkflow, "implement", side_effect=lambda *a: os._exit(73)):
                durable.execute(store)
        elif phase == "after_verification":
            with patch.object(durable.DurableWorkflow, "checkpoint", side_effect=lambda *a: os._exit(73)):
                durable.execute(store)
        else:
            durable.execute(store)


class DurableTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo, self.store, self.git = fixture(self.root)

    def run_task(self, **kwargs):
        with patch("durable.Gateway", FakeGateway):
            return durable.execute(self.store, **kwargs)

    def reload(self):
        store = durable.Store(self.root / "runs", "test-run")
        store.load()
        return store

    def resume(self, revalidate=False):
        args = argparse.Namespace(command="resume", run_id="test-run", quiet=True,
                                  revalidate_unverified=revalidate)
        return durable.cli(args, self.root)

    def test_valid_initial_schema_and_task(self):
        state = self.reload().state
        durable.validate_state(state)
        self.assertEqual(state["status"], "CREATED")
        self.assertEqual(state["verified_progress"], [])
        self.assertEqual(state["remaining_work"], [TASK])
        self.assertEqual((self.store.directory / "original-task.txt").read_text(), TASK)

    def test_multiline_task_preserves_windows_line_endings(self):
        task = "Fix the product.\r\nKeep the existing test.\r\n"
        store = durable.Store(self.root / "runs", "multiline")
        store.create(self.repo, task, COMMANDS, self.store.state["options"]["config"])
        recovered = durable.Store(self.root / "runs", "multiline")
        self.assertEqual(recovered.load()["original_task"], task)

    def test_atomic_write_failure_keeps_previous_state(self):
        path = self.store.directory / "state.json"
        before = path.read_bytes()
        with patch("durable.os.replace", side_effect=OSError("disk")), self.assertRaises(OSError):
            durable.atomic_write(path, b"invalid replacement")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(path.parent.glob(".durable-*")), [])

    def test_live_verifier_process_blocks_resume(self):
        identity = durable.process_identity(os.getpid())
        self.assertIsNotNone(identity)
        self.store.commit(active_command={"phase": "running", "pid": os.getpid(), "identity": identity})
        with self.assertRaisesRegex(durable.DurableError, "still running"):
            self.resume()

    def test_reused_pid_is_not_an_orphan(self):
        self.store.commit(active_command={"phase": "running", "pid": os.getpid(), "identity": "old-process"})
        self.assertEqual(durable.validate_repository(self.store, self.repo), "baseline")

    def test_uncertain_command_launch_refuses_resume(self):
        self.store.commit(active_command={"phase": "launching", "argv": COMMANDS[0]})
        with self.assertRaisesRegex(durable.DurableError, "child identity is uncertain"):
            self.resume(True)

    def test_finished_short_command_allows_recovery(self):
        self.store.commit(active_command={"phase": "running", "pid": os.getpid(), "identity": None})
        self.assertEqual(durable.validate_repository(self.store, self.repo), "baseline")

    def test_atomic_flush_precedes_replace(self):
        order = []
        real_replace = os.replace
        with patch("durable.os.fsync", side_effect=lambda fd: order.append("flush")), \
             patch("durable.os.replace", side_effect=lambda a, b: (order.append("replace"), real_replace(a, b))):
            durable.atomic_write(self.root / "sample.json", b"{}")
        self.assertEqual(order[:2], ["flush", "replace"])

    def test_only_successful_gates_create_checkpoint(self):
        result = self.run_task()
        store = self.reload()
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(len(store.state["verified_progress"]), 1)
        checkpoint = store.read_evidence(store.state["last_verified_checkpoint"]["reference"])
        for ref in checkpoint["verification"]:
            evidence = store.read_evidence(ref)
            self.assertTrue(evidence["passed"])
            self.assertEqual([r["exit_code"] for r in evidence["commands"]], [0, 0])
        self.assertTrue(store.read_evidence(checkpoint["reviewer"])["approved"])
        self.assertTrue(checkpoint["snapshot"]["status"])

    def test_failed_verification_never_becomes_trusted(self):
        with patch.object(FakeGateway, "chat", side_effect=lambda *a, **kw:
                          'plan' if a[2] == "plan" else '{"action":"done","summary":"done"}'):
            with self.assertRaises(durable.DurableError):
                self.run_task()
        state = self.reload().state
        self.assertEqual(state["status"], "FAILED")
        self.assertIsNone(state["last_verified_checkpoint"])
        self.assertEqual(state["verified_progress"], [])
        self.assertIsNotNone(state["last_failure_evidence"])

    def test_reviewer_rejection_never_becomes_trusted(self):
        original = FakeGateway.chat
        def chat(self, role, messages, phase, max_tokens=1300):
            if phase == "review":
                return '{"approved":false,"findings":"incomplete"}'
            return original(self, role, messages, phase, max_tokens)
        with patch.object(FakeGateway, "chat", chat), self.assertRaises(durable.DurableError):
            self.run_task()
        self.assertIsNone(self.reload().state["last_verified_checkpoint"])

    def test_final_verifier_failure_never_becomes_trusted(self):
        original = durable.DurableWorkflow.verify
        count = 0
        def verify(workflow, commands):
            nonlocal count
            count += 1
            passed, evidence = original(workflow, commands)
            return (False, "final verification failed") if count == 2 else (passed, evidence)
        with patch.object(durable.DurableWorkflow, "verify", verify), self.assertRaises(durable.DurableError):
            self.run_task()
        self.assertIsNone(self.reload().state["last_verified_checkpoint"])

    def test_cleanup_failure_blocks_checkpoint(self):
        calls = 0
        def unload(gateway):
            nonlocal calls
            calls += 1
            if calls >= 3:
                raise TimeoutError("not unloaded")
        with patch.object(FakeGateway, "unload", unload), self.assertRaises(TimeoutError):
            self.run_task()
        self.assertIsNone(self.reload().state["last_verified_checkpoint"])

    def test_unverified_claim_cannot_manufacture_checkpoint(self):
        workflow = durable.DurableWorkflow(self.repo, Mock(), CONFIG, Mock(), self.store)
        with self.assertRaises(durable.DurableError):
            workflow.checkpoint({"status": "passed"})

    def test_resume_checkpoint_does_not_call_models(self):
        self.run_task()
        with patch("durable.Gateway", side_effect=AssertionError("no models")):
            result = self.resume()
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["round_number"], 1)
        self.assertEqual(self.reload().state["recovery"]["mode"], "trusted")

    def test_resume_created_restarts_from_saved_task(self):
        with patch("durable.Gateway", FakeGateway):
            result = self.resume()
        self.assertEqual(result["status"], "COMPLETED")

    def test_repository_mismatch_refused(self):
        other_root = self.root / "other"
        other_root.mkdir()
        other, _, _ = fixture(other_root)
        with self.assertRaisesRegex(durable.DurableError, "identity mismatch"):
            durable.validate_repository(self.store, other)

    def test_head_change_refused(self):
        self.git("commit", "--allow-empty", "-qm", "manual")
        with self.assertRaisesRegex(durable.DurableError, "HEAD/branch changed"):
            self.resume()

    def test_branch_change_refused(self):
        self.git("switch", "-qc", "other")
        with self.assertRaisesRegex(durable.DurableError, "HEAD/branch changed"):
            self.resume()

    def test_manual_dirty_change_refused_without_overwrite(self):
        self.run_task()
        (self.repo.root / "calculator.py").write_text("manual edit\n")
        with self.assertRaisesRegex(durable.DurableError, "Unexpected dirty/manual"):
            self.resume(True)
        self.assertEqual((self.repo.root / "calculator.py").read_text(), "manual edit\n")

    def test_untracked_change_refused(self):
        (self.repo.root / "manual.txt").write_text("manual")
        with self.assertRaisesRegex(durable.DurableError, "Unexpected dirty/manual"):
            self.resume()

    def test_staged_change_refused(self):
        self.run_task()
        self.git("add", "calculator.py")
        with self.assertRaisesRegex(durable.DurableError, "Unexpected dirty/manual"):
            self.resume()

    def test_missing_repository_fails_safely(self):
        moved = self.root / "moved"
        self.repo.root.rename(moved)
        with self.assertRaises(FileNotFoundError):
            self.resume()
        self.assertEqual(self.reload().state["status"], "CREATED")

    def test_corrupt_state_fails_safely(self):
        for value in ("{broken", "null", "[]", "{}"):
            (self.store.directory / "state.json").write_text(value)
            with self.subTest(value=value), self.assertRaises(durable.DurableError):
                self.reload()

    def test_missing_initial_cache_recovers_only_from_committed_ledger(self):
        (self.store.directory / "state.json").unlink()
        self.assertEqual(self.reload().state["status"], "CREATED")

    def test_missing_state_and_ledger_fail_safely(self):
        (self.store.directory / "state.json").unlink()
        (self.store.directory / "events.jsonl").unlink()
        with self.assertRaises(durable.DurableError):
            self.reload()

    def test_unknown_schema_fails_safely(self):
        state = copy.deepcopy(self.store.state)
        state["schema_version"] = 100
        (self.store.directory / "state.json").write_bytes(durable.encoded(state))
        with self.assertRaisesRegex(durable.DurableError, "Unsupported durable schema"):
            self.reload()

    def test_malformed_fields_fail_safely(self):
        for key, value in (("round_number", "1"), ("options", {}), ("status", "DONE"), ("verified_progress", ["done"])):
            state = copy.deepcopy(self.store.state)
            state[key] = value
            with self.subTest(key=key), self.assertRaises(durable.DurableError):
                durable.validate_state(state)

    def test_state_and_events_agree(self):
        self.run_task()
        store = self.reload()
        commits = [r["state"] for r in store.records if r["event"] == "state_committed"]
        self.assertEqual(commits[-1], store.state)
        self.assertEqual(commits[-1], json.loads((store.directory / "state.json").read_text()))
        first_verified = next(i for i, r in enumerate(store.records)
                              if r["event"] == "state_committed" and r["state"]["status"] == "VERIFIED")
        self.assertTrue(any(r["event"] == "workflow_result" and r["status"] == "passed"
                            for r in store.records[:first_verified]))

    def test_wal_ahead_of_cache_recovers(self):
        with patch("durable.atomic_write", side_effect=OSError("disk full")), self.assertRaises(OSError):
            self.store.commit("PLANNING", current_step="planning")
        store = self.reload()
        self.assertEqual(store.state["status"], "PLANNING")
        store.repair_cache()
        self.assertEqual(json.loads((store.directory / "state.json").read_text()), store.state)

    def test_partial_event_tail_is_preserved_then_recovered(self):
        with (self.store.directory / "events.jsonl").open("ab") as stream:
            stream.write(b'{"partial":')
        store = self.reload()
        self.assertEqual(store.tail, b'{"partial":')
        store.repair_cache()
        debris = list(store.directory.glob("torn-event-*.bin"))
        self.assertEqual(debris[0].read_bytes(), b'{"partial":')
        self.assertEqual(self.reload().state, store.state)

    def test_corrupt_complete_event_refused(self):
        with (self.store.directory / "events.jsonl").open("ab") as stream:
            stream.write(b'{"partial":\n')
        with self.assertRaises(durable.DurableError):
            self.reload()

    def test_manually_modified_state_refused(self):
        state = copy.deepcopy(self.store.state)
        state["original_task"] = "other task"
        (self.store.directory / "state.json").write_bytes(durable.encoded(state))
        with self.assertRaisesRegex(durable.DurableError, "disagree"):
            self.reload()

    def test_modified_evidence_refused(self):
        self.run_task()
        checkpoint = self.store.read_evidence(self.store.state["last_verified_checkpoint"]["reference"])
        (self.store.directory / checkpoint["verification"][0]["path"]).write_text("{}")
        with self.assertRaisesRegex(durable.DurableError, "checksum mismatch"):
            self.resume()

    def test_no_model_reasoning_or_free_text_persisted(self):
        self.run_task()
        log = durable.DurableLog(self.store)
        log.emit("model_request", usage={"reasoning_content": "HIDDEN_TELEMETRY"}, timings={"thinking": "HIDDEN"})
        for path in self.store.directory.rglob("*"):
            if path.is_file():
                self.assertNotIn("HIDDEN", path.read_text(encoding="utf-8"), str(path))

    def test_critic_is_advisory_and_senior_reviewer_preserved(self):
        options = copy.deepcopy(self.store.state["options"])
        options.update(critic=True, reviewer=True)
        self.store.commit(options=options)
        gateway = FakeGateway(options["config"], None)
        with patch("durable.Gateway", return_value=gateway):
            durable.execute(self.store)
        self.assertLess(gateway.calls.index(("critic", "critic")), gateway.calls.index(("review", "review")))
        self.assertEqual(gateway.calls[gateway.calls.index(("critic", "critic")) + 1], "unload")
        checkpoint = self.store.read_evidence(self.store.state["last_verified_checkpoint"]["reference"])
        self.assertEqual(checkpoint["critic_status"], "completed")
        self.assertEqual(self.store.read_evidence(checkpoint["reviewer"])["role"], "review")

    def test_advisory_failure_can_pass_with_senior_approval(self):
        options = copy.deepcopy(self.store.state["options"])
        options.update(critic=True, reviewer=True)
        self.store.commit(options=options)
        original = FakeGateway.chat
        def chat(self, role, messages, phase, max_tokens=1300):
            if phase == "critic":
                raise TimeoutError("HIDDEN_HTTP_RESPONSE")
            return original(self, role, messages, phase, max_tokens)
        with patch.object(FakeGateway, "chat", chat):
            self.assertEqual(self.run_task()["status"], "COMPLETED")
        checkpoint = self.store.read_evidence(self.store.state["last_verified_checkpoint"]["reference"])
        self.assertEqual(checkpoint["critic_status"], "failed")

    def test_changed_files_during_review_invalidate_gates(self):
        original = FakeGateway.chat
        path = self.repo.root / "manual.txt"
        def chat(gateway, role, messages, phase, max_tokens=1300):
            if phase == "review":
                path.write_text("external change")
            return original(gateway, role, messages, phase, max_tokens)
        with patch.object(FakeGateway, "chat", chat), self.assertRaises(durable.DurableError):
            self.run_task()
        self.assertIsNone(self.reload().state["last_verified_checkpoint"])

    def test_kernel_lock_excludes_other_writer_and_releases(self):
        path = self.root / "lock"
        with durable.exclusive_lock(path):
            with self.assertRaises(durable.DurableError):
                with durable.exclusive_lock(path):
                    pass
        with durable.exclusive_lock(path):
            pass

    def test_run_id_path_traversal_rejected(self):
        for run_id in ("../escape", "x/y", "C:\\escape", ".", ""):
            with self.assertRaises(durable.DurableError):
                durable.Store(self.root, run_id)

    def test_status_needs_no_config_models_or_repository(self):
        self.repo.root.rename(self.root / "moved")
        with patch("harness.ROOT", self.root), patch("harness.load_config", side_effect=AssertionError), \
             patch("durable.Gateway", side_effect=AssertionError), redirect_stdout(io.StringIO()):
            self.assertEqual(harness.main(["status", "--run-id", "test-run"]), 0)

    def test_status_during_active_writer(self):
        args = argparse.Namespace(command="status", run_id="test-run")
        with durable.exclusive_lock(self.root / "runs" / ".durable-controller.lock"):
            self.assertEqual(durable.cli(args, self.root)["status"], "CREATED")

    def test_keyboard_interrupt_keeps_unverified_work(self):
        with patch.object(durable.DurableWorkflow, "verify", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.run_task()
        self.assertEqual(self.reload().state["status"], "INTERRUPTED")
        with self.assertRaisesRegex(durable.DurableError, "UNVERIFIED"):
            self.resume()
        with patch("durable.Gateway", FakeGateway):
            result = self.resume(True)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["round_number"], 2)
        self.assertIsNotNone(self.reload().state["last_failure_evidence"])

    def test_actual_process_death_windows(self):
        for phase in ("before_implementation", "after_write", "during_verify", "after_verification",
                      "before_commit", "after_commit"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                repo, store, _ = fixture(root)
                result = subprocess.run([sys.executable, "-c",
                    "import sys; sys.path.insert(0, 'tests'); from test_durable import crash_child; crash_child(sys.argv[1], sys.argv[2])",
                    str(root), phase], cwd=harness.ROOT, capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 73, result.stderr.decode())
                recovered = durable.Store(root / "runs", "test-run")
                recovered.load()
                trusted = phase == "after_commit"
                self.assertEqual(bool(recovered.state["last_verified_checkpoint"]), trusted)
                expected = "trusted" if trusted else "baseline" if phase == "before_implementation" else "unverified"
                self.assertEqual(durable.validate_repository(recovered, repo), expected)
                args = argparse.Namespace(command="resume", run_id="test-run", quiet=True, revalidate_unverified=True)
                with patch("durable.Gateway", FakeGateway):
                    resumed = durable.cli(args, root)
                self.assertEqual(resumed["status"], "COMPLETED")


class TerminalAcceptanceTests(unittest.TestCase):
    # Offline test-fixture controller value — registered via register_controller_authority.
    CTRL = "triglav-ctrl-test"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo, self.store, self.git = fixture(self.root)
        # Register the test controller authority for offline test fixture.
        self.store.register_controller_authority(self.CTRL)

    def tearDown(self):
        self.temp.cleanup()

    def _prepare_verified_state(self, store, repo):
        critic_ref = store.artifact("critic/1.json", {"findings": [], "summary": "clean"})
        rev_ref = store.artifact("reviewer/1.json", {"approved": True, "decision": "APPROVE"})
        ckpt = {
            "round": 1,
            "verification": [store.artifact("verif/1.json", {"passed": True, "exit_code": 0})],
            "cleanup_passed": True,
            "policy": {"critic": "disabled", "reviewer": "disabled"},
            "critic": critic_ref,
            "reviewer": rev_ref,
            "reviewer_decision": "APPROVE",
        }
        ckpt_ref = store.artifact("checkpoints/0001.json", ckpt)
        current = durable.snapshot(repo)
        store.commit(
            "VERIFIED",
            current_step="checkpoint_accepted",
            current_git_head=current["head"],
            last_verified_checkpoint={"reference": ckpt_ref, "snapshot": current},
            verified_progress=[{"task": store.state["original_task"], "checkpoint": ckpt_ref}],
            remaining_work=[],
            unverified_work={},
        )
        return ckpt_ref, current

    def test_a_exact_successful_terminal_commit(self):
        ckpt_ref, current = self._prepare_verified_state(self.store, self.repo)
        res = self.store.commit_terminal("acc-test-a", controller=self.CTRL, status="COMPLETED")
        self.assertEqual(res["status"], "COMPLETED")
        self.assertEqual(res["acceptance_id"], "acc-test-a")
        self.assertEqual(res["controller"], self.CTRL)
        self.assertEqual(res["revision"], 3)
        self.assertEqual(res["wal_sequence"], 3)
        self.assertTrue(self.store.is_terminal_accepted())

        wal_lines = [json.loads(l) for l in (self.store.directory / "events.jsonl").read_bytes().split(b"\n") if l.strip()]
        last_wal = wal_lines[-1]
        self.assertEqual(last_wal["event"], "state_committed")
        self.assertTrue(last_wal.get("terminal"))
        self.assertEqual(last_wal.get("acceptance_id"), "acc-test-a")
        self.assertEqual(last_wal.get("controller"), self.CTRL)
        self.assertEqual(last_wal["state"]["status"], "COMPLETED")
        self.assertEqual(last_wal["state"]["terminal_acceptance"]["acceptance_id"], "acc-test-a")

        seal_file = self.store.directory / "acceptance" / "acc-test-a.json"
        self.assertTrue(seal_file.exists())
        seal_data = json.loads(seal_file.read_text(encoding="utf-8"))
        self.assertEqual(seal_data["acceptance_id"], "acc-test-a")
        self.assertEqual(seal_data["target_revision"], 3)
        self.assertEqual(seal_data["state_digest"], durable.digest(last_wal["state"]))
        self.assertEqual(seal_data["accepted_state"], last_wal["state"])

        reloaded = durable.Store(self.root / "runs", "test-run")
        reloaded.load()
        self.assertEqual(reloaded.state["status"], "COMPLETED")
        self.assertEqual(reloaded.state["revision"], 3)
        self.assertEqual(reloaded.state["terminal_acceptance"]["acceptance_id"], "acc-test-a")
        self.assertTrue(reloaded.is_terminal_accepted())


    def test_b_wal_append_succeeds_cache_write_fails_acceptance_authoritative(self):
        ckpt_ref, current = self._prepare_verified_state(self.store, self.repo)
        original_atomic_write = durable.atomic_write

        def failing_atomic_write(path, data):
            if path.name == "state.json":
                raise OSError("Simulated disk error during cache write")
            return original_atomic_write(path, data)

        with patch("durable.atomic_write", side_effect=failing_atomic_write):
            res = self.store.commit_terminal("acc-test-b", controller=self.CTRL, status="COMPLETED")

        self.assertEqual(res["status"], "COMPLETED")
        self.assertTrue(self.store.is_terminal_accepted())

        cached_disk = json.loads((self.store.directory / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(cached_disk["revision"], 2)

        reloaded = durable.Store(self.root / "runs", "test-run")
        reloaded.load()
        self.assertEqual(reloaded.state["status"], "COMPLETED")
        self.assertEqual(reloaded.state["revision"], 3)
        self.assertEqual(reloaded.state["terminal_acceptance"]["acceptance_id"], "acc-test-b")

    def test_c_wal_append_raises_after_durable_write_reconciles_acceptance(self):
        ckpt_ref, current = self._prepare_verified_state(self.store, self.repo)
        original_append = durable.Store.append

        def append_and_raise(store_self, event, **fields):
            original_append(store_self, event, **fields)
            raise RuntimeError("Simulated transient post-fsync crash")

        with patch.object(durable.Store, "append", append_and_raise):
            res = self.store.commit_terminal("acc-test-c", controller=self.CTRL, status="COMPLETED")

        self.assertEqual(res["status"], "COMPLETED")
        self.assertEqual(res["acceptance_id"], "acc-test-c")
        self.assertTrue(self.store.is_terminal_accepted())

        reloaded = durable.Store(self.root / "runs", "test-run")
        reloaded.load()
        self.assertEqual(reloaded.state["status"], "COMPLETED")
        self.assertEqual(reloaded.state["revision"], 3)
        self.assertEqual(reloaded.state["terminal_acceptance"]["acceptance_id"], "acc-test-c")

    def test_d_append_fails_before_acceptance_no_false_completion(self):
        ckpt_ref, current = self._prepare_verified_state(self.store, self.repo)

        def pre_append_raise(store_self, event, **fields):
            raise OSError("Simulated disk error before WAL write")

        with patch.object(durable.Store, "append", pre_append_raise):
            with self.assertRaises(durable.DurableError) as ctx:
                self.store.commit_terminal("acc-test-d", controller=self.CTRL, status="COMPLETED")
            self.assertIn("failed before durable WAL commit", str(ctx.exception))

        self.assertEqual(self.store.state["status"], "VERIFIED")
        self.assertFalse(self.store.is_terminal_accepted())

        reloaded = durable.Store(self.root / "runs", "test-run")
        reloaded.load()
        self.assertEqual(reloaded.state["status"], "VERIFIED")
        self.assertFalse(reloaded.is_terminal_accepted())

    def test_e_same_status_different_transaction_reconciliation_rejects(self):
        ckpt_ref, current = self._prepare_verified_state(self.store, self.repo)
        original_append = durable.Store.append

        def append_different_tx_and_raise(store_self, event, **fields):
            fields_copy = dict(fields)
            fields_copy["acceptance_id"] = "acc-DIFFERENT"
            original_append(store_self, event, **fields_copy)
            raise RuntimeError("Simulated crash")

        with patch.object(durable.Store, "append", append_different_tx_and_raise):
            with self.assertRaises(durable.DurableError) as ctx:
                self.store.commit_terminal("acc-test-e", controller=self.CTRL, status="COMPLETED")
            self.assertIn("Conflicting terminal transaction in WAL", str(ctx.exception))

        self.assertFalse(self.store.is_terminal_accepted())

    def test_f_unreadable_or_incomplete_wal_stops_safely(self):
        ckpt_ref, current = self._prepare_verified_state(self.store, self.repo)

        def append_torn_wal_and_raise(store_self, event, **fields):
            with (store_self.directory / "events.jsonl").open("ab") as s:
                s.write(b'{"corrupt_partial_json":')
                s.flush()
            raise RuntimeError("Simulated crash after torn write")

        with patch.object(durable.Store, "append", append_torn_wal_and_raise):
            with self.assertRaises(durable.DurableError) as ctx:
                self.store.commit_terminal("acc-test-f", controller=self.CTRL, status="COMPLETED")
            self.assertTrue("Torn or incomplete WAL" in str(ctx.exception) or "Corrupt" in str(ctx.exception))

        self.assertFalse(self.store.is_terminal_accepted())

    def test_g_sealed_artifact_without_wal_acceptance_no_trusted_completion(self):
        ckpt_ref, current = self._prepare_verified_state(self.store, self.repo)
        orphan_seal = {
            "schema_version": durable.TERMINAL_ACCEPTANCE_SCHEMA_VERSION,
            "acceptance_id": "orphan-seal-1",
            "run_id": "test-run",
            "controller": "triglav-ctrl",
            "parent_revision": 2,
            "target_revision": 3,
            "wal_sequence": 3,
            "previous_record_hash": self.store.records[-1]["sha256"],
            "state_digest": "0" * 64,
            "accepted_state": {},
            "time": durable.now(),
        }
        (self.store.directory / "acceptance").mkdir(parents=True, exist_ok=True)
        self.store.artifact("acceptance/orphan-seal-1.json", orphan_seal)

        reloaded = durable.Store(self.root / "runs", "test-run")
        reloaded.load()
        self.assertEqual(reloaded.state["status"], "VERIFIED")
        self.assertFalse(reloaded.is_terminal_accepted())

        tampered_state = copy.deepcopy(reloaded.state)
        tampered_state["status"] = "COMPLETED"
        tampered_state["revision"] = 3
        tampered_state["terminal_acceptance"] = {
            "schema_version": durable.TERMINAL_ACCEPTANCE_SCHEMA_VERSION,
            "acceptance_id": "orphan-seal-1",
            "controller": "triglav-ctrl",
            "seal_path": "acceptance/orphan-seal-1.json",
        }
        (self.store.directory / "state.json").write_text(json.dumps(tampered_state), encoding="utf-8")

        with self.assertRaises(durable.DurableError):
            durable.Store(self.root / "runs", "test-run").load()

    def test_h_accepted_terminal_state_rejects_later_contradictory_commits(self):
        ckpt_ref, current = self._prepare_verified_state(self.store, self.repo)
        self.store.commit_terminal("acc-test-h", controller=self.CTRL, status="COMPLETED")
        self.assertTrue(self.store.is_terminal_accepted())

        with self.assertRaises(durable.DurableError) as ctx1:
            self.store.commit("FAILED")
        self.assertIn("already accepted", str(ctx1.exception))

        with self.assertRaises(durable.DurableError) as ctx2:
            self.store.commit("COMPLETED")
        self.assertIn("already accepted", str(ctx2.exception))

        with self.assertRaises(durable.DurableError) as ctx3:
            self.store.commit_terminal("acc-test-h-again")
        self.assertIn("already accepted", str(ctx3.exception))

        reloaded = durable.Store(self.root / "runs", "test-run")
        reloaded.load()
        with self.assertRaises(durable.DurableError):
            reloaded.commit("FAILED")

    def test_i_historical_and_intermediate_store_behavior_compatible(self):
        ckpt_ref, current = self._prepare_verified_state(self.store, self.repo)
        self.assertFalse(self.store.is_terminal_accepted())

        self.store.commit("READY", current_step="ready_for_next")
        self.assertEqual(self.store.state["status"], "READY")
        self.assertEqual(self.store.state["revision"], 3)

        reloaded = durable.Store(self.root / "runs", "test-run")
        reloaded.load()
        self.assertEqual(reloaded.state["status"], "READY")
        self.assertEqual(reloaded.state["revision"], 3)
        self.assertFalse(reloaded.is_terminal_accepted())


class TerminalAcceptanceFaultRegressionTests(unittest.TestCase):
    """Regression tests for the eight P0 correctness defects found by Codex review.

    Each test uses actual Store operations, real filesystem artifacts and a fresh
    Store.load() to confirm the persisted outcome rather than relying on exceptions
    alone.
    """
    CTRL = "triglav-ctrl-regression"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo, self.store, self.git = fixture(self.root)
        self.store.register_controller_authority(self.CTRL)

    def tearDown(self):
        self.temp.cleanup()

    def _prepare_verified_state(self, store, repo):
        critic_ref = store.artifact("critic/1.json", {"findings": [], "summary": "clean"})
        rev_ref = store.artifact("reviewer/1.json", {"approved": True, "decision": "APPROVE"})
        ckpt = {
            "round": 1,
            "verification": [store.artifact("verif/1.json", {"passed": True, "exit_code": 0})],
            "cleanup_passed": True,
            "policy": {"critic": "disabled", "reviewer": "disabled"},
            "critic": critic_ref,
            "reviewer": rev_ref,
            "reviewer_decision": "APPROVE",
        }
        ckpt_ref = store.artifact("checkpoints/0001.json", ckpt)
        current = durable.snapshot(repo)
        store.commit(
            "VERIFIED",
            current_step="checkpoint_accepted",
            current_git_head=current["head"],
            last_verified_checkpoint={"reference": ckpt_ref, "snapshot": current},
            verified_progress=[{"task": store.state["original_task"], "checkpoint": ckpt_ref}],
            remaining_work=[],
            unverified_work={},
        )
        return ckpt_ref, current

    # --- Defect 1: WAL write succeeds but fsync raises —
    #     buffered-but-not-fsynced record must NOT establish durable acceptance ---
    def test_p1_fsync_failure_after_wal_write_blocks_acceptance(self):
        """Simulate a successful OS write followed by fsync raising inside reconcile.

        The reconcile re-reads the disk WAL and finds the record — but then attempts
        an fsync to confirm durability.  If that fsync raises, acceptance must be
        blocked; the store must NOT be marked terminal.
        """
        self._prepare_verified_state(self.store, self.repo)
        original_append = durable.Store.append

        def append_and_raise(store_self, event, **fields):
            original_append(store_self, event, **fields)
            raise RuntimeError("Simulated post-fsync crash inside append")

        original_os_fsync = os.fsync

        fsync_calls = []

        def fsync_and_maybe_fail(fd):
            fsync_calls.append(fd)
            # Let the first (from append) succeed; fail the reconcile-time fsync
            if len(fsync_calls) > 1:
                raise OSError("Simulated fsync failure during reconcile confirmation")
            return original_os_fsync(fd)

        with patch.object(durable.Store, "append", append_and_raise), \
             patch("durable.os.fsync", side_effect=fsync_and_maybe_fail):
            with self.assertRaises(durable.DurableError) as ctx:
                self.store.commit_terminal("acc-p1", controller=self.CTRL, status="COMPLETED")
            self.assertIn("Cannot confirm WAL durability", str(ctx.exception))

        # The store instance must NOT have marked acceptance
        self.assertFalse(self.store.is_terminal_accepted())
        self.assertEqual(self.store.state["status"], "VERIFIED")

        # The WAL record IS on disk, but alone it did NOT establish acceptance in commit_terminal
        wal_bytes = (self.store.directory / "events.jsonl").read_bytes()
        wal_records = [json.loads(l) for l in wal_bytes.split(b"\n") if l.strip()]
        terminal_records = [r for r in wal_records if r.get("terminal") is True]
        self.assertEqual(len(terminal_records), 1,
                         "WAL should contain the written terminal record even after fsync failure")

        # Automatic retry with the same ID must be blocked (prohibit automatic retry)
        with self.assertRaises(durable.DurableError) as ctx_retry:
            self.store.commit_terminal("acc-p1", controller=self.CTRL, status="COMPLETED")
        self.assertIn("seal already exists", str(ctx_retry.exception))

    # --- Defect 2: reconcile incomplete proof — wrong controller in WAL must reject ---
    def test_p2_reconcile_rejects_mismatched_controller_in_wal(self):
        """After append raises, reconcile finds a WAL record with wrong controller.

        The reconcile must reject it rather than treating it as confirmed acceptance.
        """
        self._prepare_verified_state(self.store, self.repo)
        original_append = durable.Store.append

        def append_wrong_controller_and_raise(store_self, event, **fields):
            fields_copy = dict(fields)
            fields_copy["controller"] = "WRONG-CONTROLLER"
            original_append(store_self, event, **fields_copy)
            raise RuntimeError("Simulated crash after wrong-controller write")

        with patch.object(durable.Store, "append", append_wrong_controller_and_raise):
            with self.assertRaises(durable.DurableError) as ctx:
                self.store.commit_terminal("acc-p2", controller=self.CTRL, status="COMPLETED")
            self.assertIn("Controller mismatch", str(ctx.exception))

        self.assertFalse(self.store.is_terminal_accepted())

        reloaded = durable.Store(self.root / "runs", "test-run")
        with self.assertRaises(durable.DurableError) as ctx_load:
            reloaded.load()
        self.assertIn("controller mismatch", str(ctx_load.exception).lower())


    # --- Defect 2 (continued): missing terminal flag in WAL record ---
    def test_p2b_reconcile_rejects_missing_terminal_flag_in_wal(self):
        self._prepare_verified_state(self.store, self.repo)
        original_append = durable.Store.append

        def append_no_terminal_flag_and_raise(store_self, event, **fields):
            fields_copy = dict(fields)
            fields_copy.pop("terminal", None)  # strip the terminal flag
            original_append(store_self, event, **fields_copy)
            raise RuntimeError("Simulated crash after no-terminal-flag write")

        with patch.object(durable.Store, "append", append_no_terminal_flag_and_raise):
            with self.assertRaises(durable.DurableError) as ctx:
                self.store.commit_terminal("acc-p2b", controller=self.CTRL, status="COMPLETED")
            self.assertIn("terminal=True flag", str(ctx.exception))

        self.assertFalse(self.store.is_terminal_accepted())

    # --- Defect 3: load() rejects tampered controller in seal vs WAL ---
    def test_p3_load_rejects_controller_mismatch_between_seal_and_wal(self):
        """A successfully committed terminal run's seal is tampered to have a different
        controller.  Store.load() must reject it.
        """
        self._prepare_verified_state(self.store, self.repo)
        self.store.commit_terminal("acc-p3", controller=self.CTRL, status="COMPLETED")

        # Tamper the seal file — change the controller field.
        seal_path = self.store.directory / "acceptance" / "acc-p3.json"
        seal_data = json.loads(seal_path.read_text(encoding="utf-8"))
        seal_data["controller"] = "EVIL-CONTROLLER"
        # Write tampered data directly (bypassing atomic_write to avoid sha256 tracking)
        seal_path.write_text(json.dumps(seal_data), encoding="utf-8")

        with self.assertRaises(durable.DurableError) as ctx:
            reloaded = durable.Store(self.root / "runs", "test-run")
            reloaded.load()
        self.assertIn("mismatch", str(ctx.exception).lower())

    # --- Defect 3 (continued): load() rejects wrong previous_record_hash in seal ---
    def test_p3b_load_rejects_wrong_previous_hash_in_seal(self):
        """Tamper the previous_record_hash in the seal.  load() must reject it."""
        self._prepare_verified_state(self.store, self.repo)
        self.store.commit_terminal("acc-p3b", controller=self.CTRL, status="COMPLETED")

        seal_path = self.store.directory / "acceptance" / "acc-p3b.json"
        seal_data = json.loads(seal_path.read_text(encoding="utf-8"))
        seal_data["previous_record_hash"] = "a" * 64  # tampered hash
        seal_path.write_text(json.dumps(seal_data), encoding="utf-8")

        with self.assertRaises(durable.DurableError) as ctx:
            durable.Store(self.root / "runs", "test-run").load()
        self.assertIn("previous record hash", str(ctx.exception).lower())

    # --- Defect 4: orphan seal blocks second commit_terminal attempt ---
    def test_p4_orphan_seal_blocks_second_commit_attempt(self):
        """If a seal file is written but the WAL append fails (e.g. disk full),
        the orphan seal must block any subsequent commit_terminal with the same ID.
        """
        self._prepare_verified_state(self.store, self.repo)

        def raise_before_wal(store_self, event, **fields):
            raise OSError("Simulated disk full before WAL write")

        with patch.object(durable.Store, "append", raise_before_wal):
            with self.assertRaises(durable.DurableError):
                self.store.commit_terminal("acc-p4", controller=self.CTRL, status="COMPLETED")

        # Seal was written before the append attempt
        seal_path = self.store.directory / "acceptance" / "acc-p4.json"
        self.assertTrue(seal_path.exists(), "Orphan seal must exist after failed commit")
        self.assertFalse(self.store.is_terminal_accepted())

        # Second attempt with same ID must fail with "seal already exists"
        with self.assertRaises(durable.DurableError) as ctx:
            self.store.commit_terminal("acc-p4", controller=self.CTRL, status="COMPLETED")
        self.assertIn("seal already exists", str(ctx.exception))
        self.assertFalse(self.store.is_terminal_accepted())

        # Reload must confirm non-accepted (WAL has no terminal record)
        reloaded = durable.Store(self.root / "runs", "test-run")
        reloaded.load()
        self.assertFalse(reloaded.is_terminal_accepted())
        self.assertEqual(reloaded.state["status"], "VERIFIED")

    # --- Defect 5: torn WAL prevents automatic second attempt from succeeding ---
    def test_p5_torn_wal_prevents_false_completion_on_second_attempt(self):
        """After a torn WAL write, the first attempt raises.  A second attempt with
        a NEW acceptance ID must also fail (because the WAL is corrupt/torn).
        """
        self._prepare_verified_state(self.store, self.repo)

        def write_torn_and_raise(store_self, event, **fields):
            # Directly append garbage bytes to simulate a torn write.
            with (store_self.directory / "events.jsonl").open("ab") as s:
                s.write(b'{"torn_partial":')
                s.flush()
            raise RuntimeError("Simulated crash after torn WAL write")

        with patch.object(durable.Store, "append", write_torn_and_raise):
            with self.assertRaises(durable.DurableError) as ctx:
                self.store.commit_terminal("acc-p5-first", controller=self.CTRL, status="COMPLETED")
            self.assertTrue(
                "Torn or incomplete WAL" in str(ctx.exception) or "Corrupt" in str(ctx.exception)
            )

        self.assertFalse(self.store.is_terminal_accepted())

        # Second attempt with a new acceptance ID must also fail because WAL is torn
        with self.assertRaises(durable.DurableError) as ctx2:
            self.store.commit_terminal("acc-p5-second", controller=self.CTRL, status="COMPLETED")
        self.assertTrue(
            "Torn or incomplete WAL" in str(ctx2.exception) or "Corrupt" in str(ctx2.exception)
        )
        self.assertFalse(self.store.is_terminal_accepted())


    # --- Defect 6: pre-write guard that calls store.commit() must be rejected ---
    def test_p6_guard_that_mutates_wal_position_is_rejected(self):
        """A pre_write_guard that calls store.commit() shifts the WAL sequence.

        commit_terminal must detect that and refuse to proceed with stale facts.
        """
        self._prepare_verified_state(self.store, self.repo)

        store = self.store

        def guard_mutates_wal(candidate_state):
            # Reentrant call to store.commit() inside the guard.
            store.commit("READY", current_step="guard_injection")

        with self.assertRaises(durable.DurableError) as ctx:
            self.store.commit_terminal(
                "acc-p6", controller=self.CTRL, status="COMPLETED",
                pre_write_guard=guard_mutates_wal,
            )
        self.assertIn("mutated WAL position", str(ctx.exception))
        # The store should NOT be in a terminal state.
        self.assertFalse(self.store.is_terminal_accepted())

    # --- Defect 7: arbitrary caller-authored controller must be rejected ---
    def test_p7_arbitrary_controller_dict_not_authorized(self):
        """Passing an arbitrary dict or string that was not registered via
        register_controller_authority must raise.
        """
        self._prepare_verified_state(self.store, self.repo)

        # Arbitrary string — not the instance token, not whitelisted
        with self.assertRaises(durable.DurableError) as ctx:
            self.store.commit_terminal("acc-p7a", controller="arbitrary-string", status="COMPLETED")
        self.assertIn("not registered", str(ctx.exception))
        self.assertFalse(self.store.is_terminal_accepted())

        # Arbitrary dict — ditto
        with self.assertRaises(durable.DurableError) as ctx2:
            self.store.commit_terminal("acc-p7b", controller={"evil": "dict"}, status="COMPLETED")
        self.assertIn("not registered", str(ctx2.exception))
        self.assertFalse(self.store.is_terminal_accepted())

        # Instance token obtained properly — must succeed
        token = self.store.register_controller_authority()
        res = self.store.commit_terminal("acc-p7c", controller=token, status="COMPLETED")
        self.assertEqual(res["acceptance_id"], "acc-p7c")
        self.assertTrue(self.store.is_terminal_accepted())

    # --- Defect 8: caller mutation of returned result must not alter self.state ---
    def test_p8_caller_mutation_of_result_does_not_corrupt_store_state(self):
        """commit_terminal returns a deep copy of state.  Mutating the returned dict
        must not change self.state, and a subsequent FAILED commit must still be
        rejected.
        """
        self._prepare_verified_state(self.store, self.repo)
        res = self.store.commit_terminal("acc-p8", controller=self.CTRL, status="COMPLETED")

        # Mutate the returned state dict
        res["state"]["status"] = "FAILED"
        res["state"]["terminal_acceptance"] = None

        # Store's own state must be unaffected
        self.assertEqual(self.store.state["status"], "COMPLETED")
        self.assertTrue(self.store.is_terminal_accepted())
        self.assertIsNotNone(self.store.state.get("terminal_acceptance"))

        # Attempting to commit FAILED after terminal acceptance must still be rejected
        with self.assertRaises(durable.DurableError) as ctx:
            self.store.commit("FAILED")
        self.assertIn("already accepted", str(ctx.exception))

        # Reload must confirm COMPLETED
        reloaded = durable.Store(self.root / "runs", "test-run")
        reloaded.load()
        self.assertEqual(reloaded.state["status"], "COMPLETED")
        self.assertTrue(reloaded.is_terminal_accepted())


class CodexNineDefectRegressionTests(unittest.TestCase):
    """The nine concrete fault scenarios from the latest independent Codex review."""
    CTRL = "triglav-ctrl-codex-nine"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo, self.store, self.git = fixture(self.root)
        self.store.register_controller_authority(self.CTRL)

    def tearDown(self):
        self.temp.cleanup()

    def _prepare_verified_state(self, store, repo):
        critic_ref = store.artifact("critic/1.json", {"findings": [], "summary": "clean"})
        rev_ref = store.artifact("reviewer/1.json", {"approved": True, "decision": "APPROVE"})
        ckpt = {
            "round": 1,
            "verification": [store.artifact("verif/1.json", {"passed": True, "exit_code": 0})],
            "cleanup_passed": True,
            "policy": {"critic": "disabled", "reviewer": "disabled"},
            "critic": critic_ref,
            "reviewer": rev_ref,
            "reviewer_decision": "APPROVE",
        }
        ckpt_ref = store.artifact("checkpoints/0001.json", ckpt)
        current = durable.snapshot(repo)
        store.commit(
            "VERIFIED",
            current_step="checkpoint_accepted",
            current_git_head=current["head"],
            last_verified_checkpoint={"reference": ckpt_ref, "snapshot": current},
            verified_progress=[{"task": store.state["original_task"], "checkpoint": ckpt_ref}],
            remaining_work=[],
            unverified_work={},
        )
        return ckpt_ref, current

    # Test 1: Durability - WAL fsync fails, reconciliation fsync fails, load must not report completion without WAL reflush
    def test_1_wal_fsync_and_reconcile_failure_blocks_load_acceptance(self):
        self._prepare_verified_state(self.store, self.repo)
        original_append = durable.Store.append

        def append_and_raise(store_self, event, **fields):
            original_append(store_self, event, **fields)
            raise RuntimeError("Post-write append failure")

        original_fsync = os.fsync
        fsync_calls = []

        def fsync_fail_on_reconcile_and_load(fd):
            fsync_calls.append(fd)
            if len(fsync_calls) >= 2:
                raise OSError("Simulated fsync failure")
            return original_fsync(fd)

        with patch.object(durable.Store, "append", append_and_raise), \
             patch("durable.os.fsync", side_effect=fsync_fail_on_reconcile_and_load):
            with self.assertRaises(durable.DurableError):
                self.store.commit_terminal("acc-d1", controller=self.CTRL, status="COMPLETED")

            reloaded = durable.Store(self.root / "runs", "test-run")
            with self.assertRaises(durable.DurableError) as ctx:
                reloaded.load()
            self.assertIn("wal durability", str(ctx.exception).lower())

    # Test 2: Exact Evidence - Reconciliation must reject a changed on-disk seal digest
    def test_2_reconciliation_rejects_changed_seal_digest(self):
        self._prepare_verified_state(self.store, self.repo)
        original_append = durable.Store.append

        def append_tamper_seal_and_raise(store_self, event, **fields):
            seal_file = store_self.directory / "acceptance" / "acc-d2.json"
            seal_file.write_bytes(seal_file.read_bytes() + b"   \n")
            original_append(store_self, event, **fields)
            raise RuntimeError("Post-append failure to trigger reconcile")

        with patch.object(durable.Store, "append", append_tamper_seal_and_raise):
            with self.assertRaises(durable.DurableError) as ctx:
                self.store.commit_terminal("acc-d2", controller=self.CTRL, status="COMPLETED")
            self.assertTrue(
                "digest" in str(ctx.exception).lower() or "encoding" in str(ctx.exception).lower() or "bytes" in str(ctx.exception).lower()
            )

    # Test 3: Exact Evidence - Fresh Store.load() must reject WAL seal reference pointing to missing or incorrect artifact
    def test_3_load_rejects_missing_or_incorrect_seal_artifact(self):
        self._prepare_verified_state(self.store, self.repo)
        self.store.commit_terminal("acc-d3", controller=self.CTRL, status="COMPLETED")

        # Case A: Missing seal file
        seal_file = self.store.directory / "acceptance" / "acc-d3.json"
        seal_bytes = seal_file.read_bytes()
        seal_file.unlink()

        reloaded = durable.Store(self.root / "runs", "test-run")
        with self.assertRaises(durable.DurableError) as ctx1:
            reloaded.load()
        self.assertIn("missing", str(ctx1.exception).lower())

        # Case B: Incorrect artifact (tampered bytes / digest mismatch)
        seal_file.write_bytes(seal_bytes + b"  \n")
        reloaded2 = durable.Store(self.root / "runs", "test-run")
        with self.assertRaises(durable.DurableError) as ctx2:
            reloaded2.load()
        self.assertTrue(
            "digest" in str(ctx2.exception).lower() or "encoding" in str(ctx2.exception).lower() or "bytes" in str(ctx2.exception).lower()
        )

    # Test 4: Uncertain Attempts - Orphan acceptance seal must prevent normal READY continuation or new-ID terminal attempt
    def test_4_orphan_seal_prevents_ready_continuation_and_new_terminal_attempt(self):
        self._prepare_verified_state(self.store, self.repo)
        orphan_seal = {
            "schema_version": durable.TERMINAL_ACCEPTANCE_SCHEMA_VERSION,
            "acceptance_id": "orphan-d4",
            "run_id": "test-run",
            "controller": self.CTRL,
            "parent_revision": 2,
            "target_revision": 3,
            "wal_sequence": 3,
            "previous_record_hash": self.store.records[-1]["sha256"],
            "state_digest": "0" * 64,
            "accepted_state": {},
            "time": durable.now(),
        }
        (self.store.directory / "acceptance").mkdir(parents=True, exist_ok=True)
        self.store.artifact("acceptance/orphan-d4.json", orphan_seal)

        reloaded = durable.Store(self.root / "runs", "test-run")
        reloaded.load()

        with self.assertRaises(durable.DurableError) as ctx1:
            reloaded.commit("READY")
        self.assertIn("orphan", str(ctx1.exception).lower())

        with self.assertRaises(durable.DurableError) as ctx2:
            reloaded.commit_terminal("acc-d4-new", controller=self.CTRL, status="COMPLETED")
        self.assertIn("orphan", str(ctx2.exception).lower())

    # Test 5: Uncertain Attempts - Competing seal creation exclusive, no overwrite
    def test_5_competing_seal_creation_exclusive_no_overwrite(self):
        self._prepare_verified_state(self.store, self.repo)
        (self.store.directory / "acceptance").mkdir(parents=True, exist_ok=True)
        seal_path = self.store.directory / "acceptance" / "acc-d5.json"
        competing_bytes = b'{"preexisting": "competing seal created in window"}\n'

        original_artifact = durable.Store.artifact
        original_exclusive = getattr(durable.Store, "exclusive_artifact", None)

        def inject_competing_creation(store_self, relative, data):
            # A competing process creates the file in the creation window
            seal_path.write_bytes(competing_bytes)
            if original_exclusive is not None:
                return original_exclusive(store_self, relative, data)
            # Legacy code calls artifact() which uses atomic_write / os.replace
            return original_artifact(store_self, relative, data)

        target_method = "exclusive_artifact" if original_exclusive else "artifact"
        with patch.object(durable.Store, target_method, inject_competing_creation):
            with self.assertRaises(durable.DurableError):
                self.store.commit_terminal("acc-d5", controller=self.CTRL, status="COMPLETED")

        self.assertEqual(seal_path.read_bytes(), competing_bytes, "Existing seal bytes must never be overwritten")



    # Test 6: Reentrant Mutation - Pre-write guard mutating parent revision rejected
    def test_6_pre_write_guard_mutating_parent_revision_rejected(self):
        self._prepare_verified_state(self.store, self.repo)
        store = self.store

        def malicious_guard(candidate_state):
            store.state["revision"] = 102

        with self.assertRaises(durable.DurableError) as ctx:
            self.store.commit_terminal("acc-d6", controller=self.CTRL, status="COMPLETED", pre_write_guard=malicious_guard)
        self.assertTrue("mutat" in str(ctx.exception).lower() or "revision" in str(ctx.exception).lower())

    # Test 7: Authority - Remove _allow_test_controller bypass
    def test_7_unregistered_controller_rejected_without_bypass(self):
        self.assertFalse(hasattr(self.store, "_allow_test_controller"), "Store must not expose _allow_test_controller bypass")
        self._prepare_verified_state(self.store, self.repo)
        with self.assertRaises(durable.DurableError):
            self.store.commit_terminal("acc-d7", controller="unregistered-str", status="COMPLETED")

    # Test 8: Authority - Ordinary commit cannot fabricate terminal acceptance
    def test_8_ordinary_commit_cannot_fabricate_terminal_acceptance(self):
        self._prepare_verified_state(self.store, self.repo)
        valid_term_payload = {
            "schema_version": durable.TERMINAL_ACCEPTANCE_SCHEMA_VERSION,
            "acceptance_id": "fake",
            "controller": self.CTRL,
            "seal_path": "acceptance/fake.json",
        }
        with self.assertRaises(durable.DurableError) as ctx:
            self.store.commit("COMPLETED", terminal_acceptance=valid_term_payload)
        self.assertTrue("ordinary commit" in str(ctx.exception).lower() or "terminal acceptance" in str(ctx.exception).lower())


    # Test 9: Immutability - Mutating load() returned dict cannot alter terminal latch
    def test_9_mutating_load_return_dict_cannot_alter_terminal_latch(self):
        self._prepare_verified_state(self.store, self.repo)
        self.store.commit_terminal("acc-d9", controller=self.CTRL, status="COMPLETED")

        reloaded = durable.Store(self.root / "runs", "test-run")
        loaded_dict = reloaded.load()

        loaded_dict["terminal_acceptance"] = None
        loaded_dict["status"] = "VERIFIED"

        self.assertTrue(reloaded.is_terminal_accepted())

        with self.assertRaises(durable.DurableError) as ctx:
            reloaded.commit("FAILED")
        self.assertIn("already accepted", str(ctx.exception).lower())


class CodexFourCorrectionsRegressionTests(unittest.TestCase):
    """The five focused fault regressions for Codex M0-A final four corrections."""
    CTRL = "triglav-ctrl-codex-four"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo, self.store, self.git = fixture(self.root)
        self.store.register_controller_authority(self.CTRL)

    def tearDown(self):
        self.temp.cleanup()

    def _prepare_verified_state(self, store, repo):
        critic_ref = store.artifact("critic/1.json", {"findings": [], "summary": "clean"})
        rev_ref = store.artifact("reviewer/1.json", {"approved": True, "decision": "APPROVE"})
        ckpt = {
            "round": 1,
            "verification": [store.artifact("verif/1.json", {"passed": True, "exit_code": 0})],
            "cleanup_passed": True,
            "policy": {"critic": "disabled", "reviewer": "disabled"},
            "critic": critic_ref,
            "reviewer": rev_ref,
            "reviewer_decision": "APPROVE",
        }
        ckpt_ref = store.artifact("checkpoints/0001.json", ckpt)
        current = durable.snapshot(repo)
        store.commit(
            status="VERIFIED",
            last_verified_checkpoint={"round": 1, "reference": ckpt_ref},
            verified_progress=["round 1 completed"],
            current_git_head=current["head"],
        )

    # Test A: Atomic Publication - Three failed WAL fsync attempts; Store.load() must not publish acceptance
    def test_a_three_failed_wal_fsync_attempts_no_acceptance_published(self):
        self._prepare_verified_state(self.store, self.repo)
        self.store.commit_terminal("acc-a", controller=self.CTRL, status="COMPLETED")

        reloaded = durable.Store(self.root / "runs", "test-run")

        fsync_calls = 0
        def failing_fsync(fd):
            nonlocal fsync_calls
            fsync_calls += 1
            raise OSError(5, "Simulated I/O fsync failure")

        with patch("os.fsync", failing_fsync):
            for attempt in range(1, 4):
                with self.assertRaises(durable.DurableError) as ctx:
                    reloaded.load()
                self.assertIn("wal durability", str(ctx.exception).lower())
                # Must NOT publish acceptance
                self.assertFalse(reloaded.is_terminal_accepted(), f"Attempt {attempt} must not set _terminal_accepted")
                self.assertTrue(reloaded.state is None or reloaded.state.get("status") != "COMPLETED",
                                f"Attempt {attempt} must not publish accepted state")

        self.assertEqual(fsync_calls, 3)
        # Seal and WAL files must remain intact (retaining unresolved evidence)
        seal_path = self.root / "runs" / "test-run" / "acceptance" / "acc-a.json"
        self.assertTrue(seal_path.exists())
        wal_path = self.root / "runs" / "test-run" / "events.jsonl"
        self.assertTrue(wal_path.exists())

    # Test B: Same-File WAL Verification - WAL file replaced between validation and reflush; no false completion
    def test_b_wal_file_replaced_between_validation_and_reflush_rejected(self):
        self._prepare_verified_state(self.store, self.repo)
        self.store.commit_terminal("acc-b", controller=self.CTRL, status="COMPLETED")

        reloaded = durable.Store(self.root / "runs", "test-run")
        wal_path = self.root / "runs" / "test-run" / "events.jsonl"

        original_fsync = os.fsync
        def fsync_after_replacement(fd):
            wal_path.write_bytes(b'{"different": "file"}\n')
            return original_fsync(fd)

        with patch("os.fsync", side_effect=fsync_after_replacement):
            with self.assertRaises(durable.DurableError) as ctx:
                reloaded.load()
            self.assertTrue(
                "identity" in str(ctx.exception).lower()
                or "replaced" in str(ctx.exception).lower()
                or "instability" in str(ctx.exception).lower()
                or "disagree" in str(ctx.exception).lower()
            )
            self.assertFalse(reloaded.is_terminal_accepted())

    # Test C: Post-Guard Revalidation - Ordinary pre-write guard injects terminal metadata; commit refuses
    def test_c_ordinary_pre_write_guard_injecting_terminal_metadata_rejected(self):
        self._prepare_verified_state(self.store, self.repo)

        def injecting_guard(cand_state):
            cand_state["terminal_acceptance"] = {
                "schema_version": durable.TERMINAL_ACCEPTANCE_SCHEMA_VERSION,
                "acceptance_id": "injected-c",
                "controller": self.CTRL,
                "seal_path": "acceptance/injected-c.json",
            }

        with self.assertRaises(durable.DurableError) as ctx:
            self.store.commit("READY", pre_write_guard=injecting_guard)
        self.assertTrue(
            "terminal" in str(ctx.exception).lower()
            or "guard" in str(ctx.exception).lower()
        )
        self.assertFalse(self.store.is_terminal_accepted())
        self.assertIsNone(self.store.state.get("terminal_acceptance"))

    # Test D: Post-Guard Revalidation - Terminal pre-write guard changes controller/transaction binding; commit refuses
    def test_d_terminal_pre_write_guard_changing_binding_rejected(self):
        self._prepare_verified_state(self.store, self.repo)

        def tampering_guard(cand_state):
            cand_state["terminal_acceptance"]["controller"] = "tampered-unauthorized-controller"

        with self.assertRaises(durable.DurableError) as ctx:
            self.store.commit_terminal("acc-d", controller=self.CTRL, status="COMPLETED", pre_write_guard=tampering_guard)
        self.assertTrue(
            "guard" in str(ctx.exception).lower()
            or "binding" in str(ctx.exception).lower()
            or "controller" in str(ctx.exception).lower()
            or "mutat" in str(ctx.exception).lower()
        )
        self.assertFalse(self.store.is_terminal_accepted())

    # Test E: Controller Alias Isolation - Returned controller receipt mutated; Store state remains unchanged
    def test_e_returned_receipt_mutated_store_state_intact(self):
        ctrl_dict = {"name": "controller-e", "version": 1}
        self.store.register_controller_authority(ctrl_dict)
        self._prepare_verified_state(self.store, self.repo)

        receipt = self.store.commit_terminal("acc-e", controller=ctrl_dict, status="COMPLETED")

        # Mutate receipt and its nested objects
        receipt["status"] = "FAILED"
        receipt["acceptance_id"] = "mutated-id"
        receipt["revision"] = 999
        if isinstance(receipt.get("controller"), dict):
            receipt["controller"]["name"] = "tampered-controller"
        if isinstance(receipt.get("seal_reference"), dict):
            receipt["seal_reference"]["path"] = "tampered-seal"
            receipt["seal_reference"]["sha256"] = "0" * 64
        receipt["state"]["status"] = "FAILED"
        receipt["state"]["terminal_acceptance"] = None

        # Caller mutating input ctrl_dict after commit
        ctrl_dict["name"] = "tampered-input"

        # Authoritative Store.state must remain untouched
        self.assertEqual(self.store.state["status"], "COMPLETED")
        self.assertTrue(self.store.is_terminal_accepted())
        self.assertIsNotNone(self.store.state.get("terminal_acceptance"))
        self.assertEqual(self.store.state["terminal_acceptance"]["acceptance_id"], "acc-e")
        self.assertEqual(self.store.state["terminal_acceptance"]["controller"], {"name": "controller-e", "version": 1})
        self.assertEqual(self.store.state["terminal_acceptance"]["seal_path"], "acceptance/acc-e.json")


class CodexTwoInvariantRegressionTests(unittest.TestCase):
    """The three focused regressions for Codex M0-A two-invariant closure."""
    CTRL = "triglav-ctrl-two-invariants"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo, self.store, self.git = fixture(self.root)
        self.store.register_controller_authority(self.CTRL)

    def tearDown(self):
        self.temp.cleanup()

    def _prepare_verified_state(self, store, repo):
        critic_ref = store.artifact("critic/1.json", {"findings": [], "summary": "clean"})
        rev_ref = store.artifact("reviewer/1.json", {"approved": True, "decision": "APPROVE"})
        ckpt = {
            "round": 1,
            "verification": [store.artifact("verif/1.json", {"passed": True, "exit_code": 0})],
            "cleanup_passed": True,
            "policy": {"critic": "disabled", "reviewer": "disabled"},
            "critic": critic_ref,
            "reviewer": rev_ref,
            "reviewer_decision": "APPROVE",
        }
        ckpt_ref = store.artifact("checkpoints/0001.json", ckpt)
        current = durable.snapshot(repo)
        store.commit(
            status="VERIFIED",
            last_verified_checkpoint={"round": 1, "reference": ckpt_ref},
            verified_progress=["round 1 completed"],
            current_git_head=current["head"],
        )

    # Regression 1: Equal-length WAL rewrite during Store.load() fsync must be rejected
    def test_1_equal_length_wal_rewrite_during_load_fsync_rejected(self):
        self._prepare_verified_state(self.store, self.repo)
        self.store.commit_terminal("acc-inv1", controller=self.CTRL, status="COMPLETED")

        reloaded = durable.Store(self.root / "runs", "test-run")
        wal_path = self.root / "runs" / "test-run" / "events.jsonl"

        original_fsync = os.fsync
        def rewrite_equal_length(fd):
            cur_bytes = wal_path.read_bytes()
            # Mutate bytes while maintaining exact equal length
            mutated = cur_bytes.replace(b'"round_number"', b'"round_mutate"')
            if mutated == cur_bytes:
                mutated = cur_bytes[:-3] + b"XYZ\n"
            assert len(mutated) == len(cur_bytes), "Test requires exact equal length rewrite"
            wal_path.write_bytes(mutated)
            return original_fsync(fd)

        with patch("os.fsync", side_effect=rewrite_equal_length):
            with self.assertRaises(durable.DurableError) as ctx:
                reloaded.load()
            self.assertTrue(
                "byte" in str(ctx.exception).lower()
                or "changed" in str(ctx.exception).lower()
                or "instability" in str(ctx.exception).lower()
                or "disagree" in str(ctx.exception).lower()
            )
            self.assertFalse(reloaded.is_terminal_accepted())

    # Regression 2: Equal-length WAL rewrite during terminal reconciliation fsync must be rejected
    def test_2_equal_length_wal_rewrite_during_reconcile_fsync_rejected(self):
        self._prepare_verified_state(self.store, self.repo)
        wal_path = self.root / "runs" / "test-run" / "events.jsonl"

        original_append = durable.Store.append
        original_fsync = os.fsync
        in_reconcile = False

        def append_and_raise(store_self, event, **fields):
            nonlocal in_reconcile
            original_append(store_self, event, **fields)
            in_reconcile = True
            raise RuntimeError("Simulated crash forcing reconciliation")

        def rewrite_equal_length_reconcile(fd):
            if in_reconcile:
                cur_bytes = wal_path.read_bytes()
                mutated = cur_bytes.replace(b'"round_number"', b'"round_mutate"')
                if mutated == cur_bytes:
                    mutated = cur_bytes[:-3] + b"XYZ\n"
                assert len(mutated) == len(cur_bytes), "Test requires exact equal length rewrite"
                wal_path.write_bytes(mutated)
            return original_fsync(fd)

        with patch.object(durable.Store, "append", append_and_raise):
            with patch("os.fsync", side_effect=rewrite_equal_length_reconcile):
                with self.assertRaises(durable.DurableError) as ctx:
                    self.store.commit_terminal("acc-inv2", controller=self.CTRL, status="COMPLETED")
                self.assertTrue(
                    "byte" in str(ctx.exception).lower()
                    or "changed" in str(ctx.exception).lower()
                    or "reconciliation" in str(ctx.exception).lower()
                )
                self.assertFalse(self.store.is_terminal_accepted())

    # Regression 3: Terminal guard changes original_task without violating state schema
    def test_3_terminal_guard_mutates_original_task_rejected(self):
        self._prepare_verified_state(self.store, self.repo)
        original_task_before = self.store.state["original_task"]

        def guard_tampering_task(cand_state):
            # Change original_task to another valid string (preserves state schema)
            cand_state["original_task"] = "arbitrary different task injected by guard"

        with self.assertRaises(durable.DurableError) as ctx:
            self.store.commit_terminal(
                "acc-inv3", controller=self.CTRL, status="COMPLETED",
                pre_write_guard=guard_tampering_task
            )
        self.assertTrue(
            "mutat" in str(ctx.exception).lower()
            or "guard" in str(ctx.exception).lower()
            or "proposed" in str(ctx.exception).lower()
        )
        self.assertFalse(self.store.is_terminal_accepted())
        self.assertEqual(self.store.state["original_task"], original_task_before)


class CodexCanonicalEqualityRegressionTests(unittest.TestCase):
    """Regression tests for canonical byte equality and int/bool type confusion."""
    CTRL = {"name": "triglav-ctrl-canonical", "version": 1}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo, self.store, self.git = fixture(self.root)
        self.store.register_controller_authority(self.CTRL)

    def tearDown(self):
        self.temp.cleanup()

    def _prepare_verified_state(self, store, repo):
        critic_ref = store.artifact("critic/1.json", {"findings": [], "summary": "clean"})
        rev_ref = store.artifact("reviewer/1.json", {"approved": True, "decision": "APPROVE"})
        ckpt = {
            "round": 1,
            "verification": [store.artifact("verif/1.json", {"passed": True, "exit_code": 0})],
            "cleanup_passed": True,
            "policy": {"critic": "disabled", "reviewer": "disabled"},
            "critic": critic_ref,
            "reviewer": rev_ref,
            "reviewer_decision": "APPROVE",
        }
        ckpt_ref = store.artifact("checkpoints/0001.json", ckpt)
        current = durable.snapshot(repo)
        store.commit(
            status="VERIFIED",
            last_verified_checkpoint={"round": 1, "reference": ckpt_ref},
            verified_progress=["round 1 completed"],
            current_git_head=current["head"],
        )

    # Regression 1: Pre-write guard replaces controller {"version": 1} with True
    def test_1_terminal_guard_controller_int_bool_mutation_rejected(self):
        self._prepare_verified_state(self.store, self.repo)
        wal_count_before = len(self.store.records)

        def guard_mutating_int_to_bool(cand_state):
            # Mutate controller version from 1 to True
            cand_state["terminal_acceptance"]["controller"]["version"] = True

        with self.assertRaises(durable.DurableError) as ctx:
            self.store.commit_terminal(
                "acc-canon1",
                controller=self.CTRL,
                status="COMPLETED",
                pre_write_guard=guard_mutating_int_to_bool,
            )
        self.assertTrue(
            "mutat" in str(ctx.exception).lower()
            or "controller" in str(ctx.exception).lower()
            or "proposed" in str(ctx.exception).lower()
        )
        self.assertFalse(self.store.is_terminal_accepted())
        self.assertEqual(len(self.store.records), wal_count_before)
        self.assertFalse(any(r.get("terminal") is True for r in self.store.records))
        self.assertEqual(self.store.state["status"], "VERIFIED")

        reloaded = durable.Store(self.root / "runs", "test-run")
        reloaded.load()
        self.assertFalse(reloaded.is_terminal_accepted())
        self.assertEqual(reloaded.state["status"], "VERIFIED")

    # Regression 2: Persisted controller int vs bool canonical mismatch rejected on load
    def test_2_persisted_controller_int_bool_canonical_mismatch_rejected_on_load(self):
        self._prepare_verified_state(self.store, self.repo)
        self.store.commit_terminal("acc-canon2", controller=self.CTRL, status="COMPLETED")

        # Seal file has {"version": 1}. Tamper seal JSON replacing 1 with true
        seal_path = self.root / "runs" / "test-run" / "acceptance" / "acc-canon2.json"
        raw_seal = seal_path.read_bytes()
        tampered_seal = raw_seal.replace(b'"version": 1', b'"version": true')
        self.assertNotEqual(raw_seal, tampered_seal, "Replacement must succeed")
        seal_path.write_bytes(tampered_seal)

        reloaded = durable.Store(self.root / "runs", "test-run")
        with self.assertRaises(durable.DurableError) as ctx:
            reloaded.load()
        self.assertFalse(reloaded.is_terminal_accepted())

    # Regression 3: Reconcile rejects controller int vs bool canonical mismatch
    def test_3_reconciliation_rejects_controller_int_bool_canonical_mismatch(self):
        self._prepare_verified_state(self.store, self.repo)
        original_append = durable.Store.append

        def append_tamper_seal_and_crash(store_self, event, **fields):
            original_append(store_self, event, **fields)
            # Tamper the seal file so controller has True instead of 1
            seal_file = store_self.directory / "acceptance" / "acc-canon3.json"
            raw_seal = seal_file.read_bytes()
            tampered_seal = raw_seal.replace(b'"version": 1', b'"version": true')
            seal_file.write_bytes(tampered_seal)
            raise RuntimeError("Post-append crash triggering reconciliation")

        with patch.object(durable.Store, "append", append_tamper_seal_and_crash):
            with self.assertRaises(durable.DurableError) as ctx:
                self.store.commit_terminal("acc-canon3", controller=self.CTRL, status="COMPLETED")
            self.assertFalse(self.store.is_terminal_accepted())


if __name__ == "__main__":
    unittest.main()
