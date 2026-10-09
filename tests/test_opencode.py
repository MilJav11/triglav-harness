"""Real controller/repository gates; fake only OpenCode process/model work."""
import copy
import io
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import environment
import harness
import manager
import opencode_executor as oc
import supervision
import winprocess
from test_manager import (ManagerCase, RepairPlanner, step, ADD_OK, wrong, Critic, Reviewer,
                          clean_critic, Clock)
from test_supervision import MemoryStore, IDENTITY


def finished(session="ses_test"):
    return {"exit_code": 0, "termination": "natural", "metadata":
            {"session_id": session, "finished": True, "error": False, "protocol_error": False}}


class OpenCodeTrustTests(ManagerCase):
    def adapter(self, candidates, result=None, clock=None):
        self.process_calls = []
        candidates = list(candidates)
        def runner(supervisor, argv, cwd, timeout, maximum, **kwargs):
            self.process_calls.append((argv, cwd, timeout, kwargs))
            if "--version" in argv:
                return {"exit_code": 0, "stdout": oc.VERSION}
            candidate = candidates.pop(0)
            if candidate is not None:
                data = {"calc.py": candidate} if isinstance(candidate, str) else candidate
                for name, text in data.items():
                    (cwd / name).write_text(text, encoding="utf-8")
            return copy.deepcopy(result or finished("ses_episode" + str(len(self.process_calls))))
        self.addCleanup(patch.stopall)
        patch("opencode_executor.executable", return_value=sys.executable).start()
        patch.object(self.gateway, "switch", side_effect=lambda role: self.gateway.chat(role), create=True).start()
        return oc.OpenCodeExecutor(self.gateway, self.config(), harness.EventLog(self.root / "oc.jsonl"),
                                   runner=runner, clock=clock or __import__("time").monotonic)

    def test_false_success_is_untrusted_without_checkpoint_and_records_failure(self):
        self.make(budgets={"max_rounds": 1})
        result = self.run_loop(RepairPlanner([step()]), self.adapter([wrong("false-success")]))
        self.assertFalse(result["rounds"][0]["trusted"])
        self.assertEqual(result["rounds"][0]["reason"], "VERIFICATION_FAILED")
        self.assertIsNone(result["last_verified_checkpoint"])
        self.assertEqual(result["rounds"][0]["verification"], "failed")
        self.assertEqual(self.state()["manager"]["last_failure"]["reason"], "VERIFICATION_FAILED")

    def test_fresh_recovery_receives_durable_failure_and_preserves_candidate(self):
        self.make(budgets={"max_rounds": 2})
        result = self.run_loop(RepairPlanner([step(), step("repair")]), self.adapter([wrong("first"), ADD_OK]))
        self.assertEqual(result["status"], "COMPLETED")
        self.assertFalse(result["rounds"][0]["trusted"])
        self.assertTrue(result["rounds"][1]["trusted"])
        calls = [c for c in self.process_calls if c[3].get("opencode")]
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0][3]["env"]["OPENCODE_CONFIG"], calls[1][3]["env"]["OPENCODE_CONFIG"])
        self.assertIn("VERIFICATION_FAILED", calls[1][3]["stdin"])
        self.assertIn("test_add", calls[1][3]["stdin"])
        self.assertIn("at most 120 lines", calls[0][3]["stdin"])
        self.assertTrue(all("--session" not in c[0] and "--continue" not in c[0] for c in calls))
        self.assertTrue(all(c[1] == self.path.resolve() for c in calls))
        self.assertEqual(calls[0][0][1:], ["run", "--pure", "--format", "json", "--model", oc.MODEL])

    def test_scope_bypass_remains_untrusted(self):
        self.make(budgets={"max_rounds": 1})
        result = self.run_loop(RepairPlanner([step()]),
                               self.adapter([{"calc.py": ADD_OK, "escape.txt": "outside"}]))
        self.assertEqual(result["rounds"][0]["reason"], "SCOPE_VIOLATION")
        self.assertIsNone(result["last_verified_checkpoint"])


    def test_ignored_source_write_is_detected(self):
        with (self.path / ".gitignore").open("a") as stream:
            stream.write("ignored.txt\n")
        self.git("add", ".")
        self.git("commit", "-qm", "ignore fixture")
        self.make(budgets={"max_rounds": 1})
        result = self.run_loop(RepairPlanner([step()]), self.adapter([{"calc.py": ADD_OK, "ignored.txt": "outside"}]))
        self.assertEqual(result["rounds"][0]["reason"], "SCOPE_VIOLATION")
        self.assertIsNone(result["last_verified_checkpoint"])

    def test_passing_no_change_uses_verified_existing(self):
        (self.path / "calc.py").write_text(ADD_OK)
        self.git("add", ".")
        self.git("commit", "-qm", "passing baseline")
        self.make()
        result = self.run_loop(RepairPlanner([step()]), self.adapter([None]))
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["rounds"][0]["implementation"], "verified_existing")

    def test_critic_reviewer_final_verifier_precede_checkpoint(self):
        self.make(critic=True, reviewer=True, review_policy="three-model")
        result = self.run_loop(RepairPlanner([step()]), self.adapter([ADD_OK]),
                               Critic([clean_critic()]), Reviewer([(True, "approved")]))
        self.assertEqual(result["status"], "COMPLETED")
        phases = [e.get("detail") for e in self.reload().records if e["event"] == "manager_event"]
        self.assertLess(phases.index("round 1 CRITIQUING"), phases.index("round 1 REVIEWING"))
        self.assertEqual(phases.count("round 1 VERIFYING"), 2)
        self.assertGreaterEqual(len(self.state()["evidence"]["verifier"]), 2)

    def test_reviewer_rejection_prevents_checkpoint(self):
        self.make(critic=True, reviewer=True, review_policy="three-model", budgets={"max_rounds": 1})
        result = self.run_loop(RepairPlanner([step()]), self.adapter([ADD_OK]),
                               Critic([clean_critic()]), Reviewer([(False, "reject")]))
        self.assertEqual(result["rounds"][0]["reason"], "REVIEWER_REJECTED")
        self.assertIsNone(result["last_verified_checkpoint"])

    def test_final_verification_failure_prevents_checkpoint(self):
        self.make(critic=True, reviewer=True, review_policy="three-model", budgets={"max_rounds": 1})
        reviewer = Reviewer([(True, "approved")])
        original = reviewer.review
        original_unload = self.gateway.unload
        armed = {"value": False}
        def approve(*args):
            armed["value"] = True
            return original(*args)
        reviewer.review = approve
        def unload():
            if armed["value"]:
                (self.path / "calc.py").write_text(wrong("after-review"))
                armed["value"] = False
            original_unload()
        self.gateway.unload = unload
        result = self.run_loop(RepairPlanner([step()]), self.adapter([ADD_OK]), Critic([clean_critic()]), reviewer)
        self.assertEqual(result["rounds"][0]["reason"], "FINAL_VERIFICATION_FAILED")
        self.assertIsNone(result["last_verified_checkpoint"])

    def test_timeout_is_untrusted_and_controller_deadline_is_preserved(self):
        self.make(budgets={"max_rounds": 1, "round_timeout_seconds": 3})
        adapter = self.adapter([None], {"exit_code": None, "termination": "forced", "metadata": {}})
        result = self.run_loop(RepairPlanner([step()]), adapter)
        self.assertEqual(result["rounds"][0]["reason"], "OPENCODE_TIMEOUT")
        self.assertIsNone(result["last_verified_checkpoint"])
        self.assertLessEqual([c for c in self.process_calls if c[3].get("opencode")][0][2], 3)
        self.assertIsNotNone(adapter.deadline)

    def test_cleanup_failure_stops_without_recovery(self):
        self.make(budgets={"max_rounds": 2})
        adapter = self.adapter([])
        adapter.runner = Mock(side_effect=RuntimeError("job still running"))
        result = self.run_loop(RepairPlanner([step()]), adapter)
        self.assertEqual(result["status"], "HUMAN_ACTION_REQUIRED")
        self.assertIsNone(result["last_verified_checkpoint"])
        self.assertEqual(adapter.runner.call_count, 1)

    def test_wrong_pin_does_not_launch_session(self):
        self.make(budgets={"max_rounds": 1})
        adapter = self.adapter([])
        adapter.runner = Mock(return_value={"exit_code": 0, "stdout": "9.0"})
        result = self.run_loop(RepairPlanner([step()]), adapter)
        self.assertEqual(result["rounds"][0]["reason"], "OPENCODE_START_FAILED")
        self.assertEqual(adapter.runner.call_count, 1)

    def test_protocol_and_exit_failure_mapping(self):
        for response, expected in (({"exit_code": 1, "metadata": {}}, "OPENCODE_EXIT_ERROR"),
                                   ({"exit_code": 0, "metadata": {}}, "OPENCODE_PROTOCOL_ERROR")):
            self.make(run_id=expected, budgets={"max_rounds": 1})
            result = self.run_loop(RepairPlanner([step()]), self.adapter([None], response))
            self.assertEqual(result["rounds"][0]["reason"], expected)
            self.assertIsNone(result["last_verified_checkpoint"])

    def test_expired_absolute_deadline_never_starts_process(self):
        clock = Clock()
        self.make()
        adapter = self.adapter([], clock=clock)
        adapter.set_deadline(0)
        repo = manager.ScopedRepository(self.path, self.config(), harness.EventLog(self.root / "deadline.jsonl"), self.store)
        context = manager.build_context(self.store, role="executor", step=step())
        with self.assertRaises(manager.ExecutorError) as error:
            adapter.execute(step(), context, repo)
        self.assertEqual(error.exception.reason, "OPENCODE_TIMEOUT")
        self.assertEqual(adapter.deadline, 0)
        self.assertEqual(self.process_calls, [])

    def test_invalid_snapshot_path_never_starts_process_or_model(self):
        self.make()
        adapter = self.adapter([])
        repo = manager.ScopedRepository(self.path, self.config(), harness.EventLog(self.root / "path.jsonl"), self.store)
        context = manager.build_context(self.store, role="executor", step=step())
        with patch("opencode_executor.episode_environment", side_effect=manager.ExecutorError(
                "snapshot path too long", "OPENCODE_START_FAILED")):
            with self.assertRaises(manager.ExecutorError) as error:
                adapter.execute(step(), context, repo)
        self.assertEqual(error.exception.reason, "OPENCODE_START_FAILED")
        self.assertEqual(self.process_calls, [])
        self.gateway.switch.assert_not_called()

    def test_legacy_default_and_selection(self):
        self.assertIsInstance(manager.default_agents(Mock(), self.gateway, self.config(), Mock()).executor,
                              manager.ModelExecutor)
        self.assertIsInstance(manager.default_agents(Mock(), self.gateway, {**self.config(), "executor": "opencode"}, Mock()).executor,
                              oc.OpenCodeExecutor)


class OpenCodeBoundaryTests(unittest.TestCase):
    def test_pinned_provider_permissions_and_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            context = {"scope": {"forbidden_paths": ["src/private/"]},
                       "checks": [{"argv": ["python", "-m", "unittest"]}]}
            config = {"base_url": "http://127.0.0.1:9292", "roles": {"code": "llm-code"}}
            settings = oc.configuration(config, step(paths=("src/",), forbidden=("src/no.py",)), context, Mock())
            self.assertEqual(settings["provider"]["local"]["options"]["baseURL"], config["base_url"] + "/v1")
            self.assertEqual(settings["model"], settings["small_model"])
            self.assertFalse(settings["autoupdate"])
            self.assertEqual(settings["permission"]["edit"], {
                "*": "deny", "src/*": "allow", "src/no.py": "deny", "src/private/*": "deny", ".git/*": "deny"})
            self.assertEqual(settings["permission"]["external_directory"], "deny")
            with patch.dict(os.environ, {"OPENAI_API_KEY": "SECRET", "OPENCODE_CONFIG_CONTENT": "UNSAFE", "PYTEST_ADDOPTS": "--collect-only"}):
                env = oc.episode_environment(Path(temporary))
            self.assertNotIn("SECRET", json.dumps(env))
            self.assertNotIn("OPENCODE_CONFIG_CONTENT", env)
            self.assertEqual(env["OPENCODE_DISABLE_PROJECT_CONFIG"], "true")
            self.assertEqual(env["PYTEST_ADDOPTS"], "-p no:cacheprovider")

    def test_pytest_episode_runs_assertions_without_unscoped_cache_writes(self):
        from test_manager import git_fixture
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate, git = git_fixture(root)
            (candidate / "pytest.ini").write_text("[pytest]\n")
            test = candidate / "test_cache.py"
            test.write_text("def test_assertion():\n    assert True\n")
            git("add", ".")
            git("commit", "-qm", "pytest fixture")
            env = oc.episode_environment(root / "episode")
            repo = type("Repo", (), {"root": candidate, "path": lambda _, p: candidate / p})()
            for expected, assertion in ((0, "True"), (1, "False")):
                test.write_text("def test_assertion():\n    assert " + assertion + "\n")
                before = oc.repository_inventory(repo)
                result = subprocess.run([sys.executable, "-m", "pytest", "-q", "test_cache.py"],
                                        cwd=candidate, env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                self.assertIn("1 passed" if expected == 0 else "1 failed", result.stdout)
                self.assertEqual(oc.repository_inventory(repo), before)
                self.assertFalse((candidate / ".pytest_cache").exists())

    def test_snapshot_path_budget_fails_before_launch(self):
        self.assertEqual(oc.SNAPSHOT_GITDIR_MAX, 220)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "episode"
            directory.mkdir()
            # Non-ASCII paths must be measured in bytes, as in Git's strlen.
            data = Path(temporary) / ("é" * 100)
            with self.assertRaises(manager.ExecutorError) as error:
                oc.episode_environment(directory, data_directory=data)
            self.assertEqual(error.exception.reason, "OPENCODE_START_FAILED")
            self.assertFalse(data.exists())
            self.assertEqual(list(directory.iterdir()), [])

    @unittest.skipUnless(os.name == "nt", "Windows Git explicit path limit")
    def test_windows_git_reproduces_legacy_snapshot_path_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            gitdir = str(Path(temporary) / ("x" * 180))
            self.assertGreater(len(gitdir.encode("utf-8")), oc.SNAPSHOT_GITDIR_MAX)
            result = subprocess.run(["git", "-c", "core.longpaths=true", "--git-dir", gitdir, "status"],
                                    cwd=temporary, capture_output=True, text=True)
            self.assertEqual(result.returncode, 128)
            self.assertIn("'$GIT_DIR' too big", result.stderr)

    def test_compact_data_path_preserves_snapshot_change_tracking(self):
        from test_manager import git_fixture
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate, git = git_fixture(root)
            controller = root / "controller"
            episode = controller / "opencode" / ("a" * 32)
            episode.mkdir(parents=True)
            env = oc.episode_environment(episode, data_directory=controller / "oc" / ("a" * 16))
            snapshot = Path(env["XDG_DATA_HOME"]) / "opencode" / "snapshot" / ("b" * 40) / ("c" * 40)
            self.assertLessEqual(len(str(snapshot).encode("utf-8")), oc.SNAPSHOT_GITDIR_MAX)
            self.assertTrue(snapshot.is_relative_to(controller))
            self.assertFalse(snapshot.is_relative_to(candidate))
            baseline = git("rev-parse", "HEAD").stdout
            index = (candidate / ".git/index").read_bytes()
            snapshot.mkdir(parents=True)
            def snap(*args):
                return subprocess.run(["git", "-c", "core.longpaths=true", "--git-dir", str(snapshot),
                                       "--work-tree", str(candidate), *args], cwd=candidate,
                                      env=env, check=True, capture_output=True).stdout
            snap("init", "-q")
            snap("add", "--all")
            before = snap("write-tree").decode().strip()
            (candidate / "calc.py").write_text(ADD_OK)
            (candidate / "new.txt").write_text("new")
            (candidate / "test_sub.py").unlink()
            self.assertIn(b"calc.py", snap("diff-files", "--name-only"))
            self.assertIn(b"new.txt", snap("ls-files", "--others", "--exclude-standard"))
            snap("add", "--all")
            after = snap("write-tree").decode().strip()
            self.assertNotEqual(before, after)
            self.assertEqual(set(snap("diff", "--name-only", before, after).decode().splitlines()),
                             {"calc.py", "new.txt", "test_sub.py"})
            self.assertEqual(git("rev-parse", "HEAD").stdout, baseline)
            self.assertEqual((candidate / ".git/index").read_bytes(), index)

    def test_event_reduction_keeps_only_bounded_metadata(self):
        events = [{"type": "tool_use", "sessionID": "ses_test", "part": {
                    "tool": "bash", "state": {"metadata": {"exit": 1}, "output": "SECRET"}}},
                  {"type": "text", "sessionID": "ses_test", "part": {"text": "SECRET"}},
                  {"type": "step_finish", "sessionID": "ses_test", "part": {"reason": "stop"}}]
        summary = dict(session_id=None, events=0, finished=False, error=False,
                       protocol_error=False, tools={}, command_exits=[])
        oc.collect_events(io.BytesIO(("".join(json.dumps(e) + "\n" for e in events)).encode()), summary)
        self.assertTrue(summary["finished"])
        self.assertEqual(summary["command_exits"], [1])
        self.assertNotIn("SECRET", json.dumps(summary))
        oc.collect_events(io.BytesIO(b"x" * (oc.MAX_EVENT + 2) + b"\n"), summary)
        self.assertTrue(summary["protocol_error"])

    def test_heartbeat_changes_terminal_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            log = harness.EventLog(Path(temporary) / "log.jsonl", progress=True)
            with patch("harness.print") as output:
                log.status("opencode_status", state="running")
                log.status("operation_heartbeat", label="OPENCODE", operation="heartbeat", elapsed=20, timeout=30, alive=True)
            self.assertFalse(log.path.exists())
            self.assertIn("OPENCODE", str(output.call_args_list))

    def test_adapter_and_binary_drift_are_detected(self):
        from test_harness import CONFIG
        with patch("opencode_executor.executable", return_value=sys.executable):
            a = environment.capture({**CONFIG, "executor": "opencode"}, Path.cwd(), [["git", "status"]])
        self.assertIn("opencode_executor.py", a["source"])
        self.assertIn("opencode", a["runtime"])
        b = copy.deepcopy(a)
        b["runtime"]["opencode"]["sha256"] = "f" * 64
        b["sha256"] = supervision.checksum({k: v for k, v in b.items() if k != "sha256"})
        self.assertEqual(environment.compare(a, b)["decision"], "UNSAFE")

    def test_real_windows_broker_timeout_drains_descendants(self):
        if os.name != "nt":
            self.skipTest("Windows Job Object qualification")
        with tempfile.TemporaryDirectory() as temporary:
            store = MemoryStore()
            store.directory = Path(temporary)
            supervisor = supervision.Supervisor(store)
            command = [sys.executable, "-c",
                "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']); time.sleep(60)"]
            result = supervision.run_command(supervisor, command, Path.cwd(), 0.5, 0,
                        purpose="opencode_executor", env=os.environ.copy(), stdin="bounded", opencode=True)
            self.assertEqual(result["termination"], "forced")
            child = list(store.state["supervision"]["children"].values())[0]
            self.assertEqual(child["state"], "EXITED")
            winprocess.wait_job_empty(child["job"])


    def test_natural_exit_still_drains_owned_descendants(self):
        if os.name != "nt":
            self.skipTest("Windows Job Object qualification")
        with tempfile.TemporaryDirectory() as temporary:
            store = MemoryStore()
            store.directory = Path(temporary)
            supervisor = supervision.Supervisor(store)
            code = (
                "import json,subprocess,sys; "
                "subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'], "
                "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
                "print(json.dumps({'type':'step_finish','sessionID':'ses_test','part':{'reason':'stop'}}))")
            result = supervision.run_command(supervisor, [sys.executable, "-c", code], Path.cwd(), 5, 0,
                        purpose="opencode_executor", env=os.environ.copy(), stdin="bounded", opencode=True)
            self.assertEqual(result["exit_code"], 0)
            self.assertTrue(result["metadata"]["finished"])
            child = list(store.state["supervision"]["children"].values())[0]
            self.assertEqual(child["termination"], "natural")
            winprocess.wait_job_empty(child["job"])

    def test_crashed_executor_still_drains_job(self):
        if os.name != "nt":
            self.skipTest("Windows Job Object qualification")
        with tempfile.TemporaryDirectory() as temporary:
            store = MemoryStore()
            store.directory = Path(temporary)
            supervisor = supervision.Supervisor(store)
            result = supervision.run_command(supervisor, [sys.executable, "-c", "raise SystemExit(7)"],
                        Path.cwd(), 5, 0, purpose="opencode_executor",
                        env=os.environ.copy(), stdin="bounded", opencode=True)
            self.assertEqual(result["exit_code"], 7)
            child = list(store.state["supervision"]["children"].values())[0]
            winprocess.wait_job_empty(child["job"])

    def test_cleanup_failure_cannot_return_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = MemoryStore()
            store.directory = Path(temporary)
            supervisor = supervision.Supervisor(store, boot=lambda: "test", inspect=lambda pid: IDENTITY)
            proc = Mock(pid=123)
            def wait(timeout):
                child = list(store.state["supervision"]["children"].values())[0]
                directory = store.directory / "processes" / child["child_id"]
                (directory / "exit.json").write_text(json.dumps({
                    "token": child["token"], "exit_code": 0, "termination": "natural",
                    "descendant_cleanup": "job_close", "metadata": finished()["metadata"]}))
            proc.wait.side_effect = wait
            with patch("supervision.subprocess.Popen", return_value=proc), \
                 patch("winprocess.wait_job_empty", side_effect=RuntimeError("still running")):
                with self.assertRaisesRegex(RuntimeError, "still running"):
                    supervision.run_command(supervisor, [sys.executable], Path.cwd(), 1, 0,
                                            purpose="opencode_executor", env={}, stdin="x", opencode=True)
            self.assertNotEqual(list(store.state["supervision"]["children"].values())[0]["state"], "EXITED")
