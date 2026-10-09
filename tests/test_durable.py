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


if __name__ == "__main__":
    unittest.main()
