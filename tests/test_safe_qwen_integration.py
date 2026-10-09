"""Offline SafeQwen route, real verifier/controller/checkpoint, scripted transport only."""
import contextlib
import copy
import io
import json
from pathlib import Path
import runpy
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import durable
import environment
import harness
import manager
import safe_executor_binding as binding
import terminal_run as launch
from critic_executor import ScriptedCritic
from reviewer_executor import ScriptedReviewer
from safe_qwen_worker import Denied, Policy, SafeQwenWorker, sha
from integration_shim import SafeQwenExecutor
from test_reviewer_wave6 import setup_test_fixture
from test_safe_qwen_worker import operation, submit, no_process


class ScriptedGateway:
    """No socket/process APIs; still exercises the real SafeQwen wire adapter."""
    def __init__(self, cfg, responses):
        self.config = cfg
        self.responses = iter(responses)
        self.supervisor = None
        self.execution_deadline = None
        self.active_role = None
        self.events = []
        self.log = SimpleNamespace(emit=lambda *a, **k: None,
                                   operation=lambda *a, **k: contextlib.nullcontext())
    def set_deadline(self, value):
        self.execution_deadline = value
    def bounded_timeout(self, value):
        return value
    def check_active_ram(self):
        pass
    def switch(self, role):
        self.events.append(("switch", role))
        self.active_role = role
    def unload(self):
        self.events.append(("unload", self.active_role))
        self.active_role = None
    def request(self, method, path, payload, timeout):
        self.events.append(("request", payload["model"]))
        result = next(self.responses)
        if isinstance(result, BaseException):
            raise result
        return {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": result}}]}


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo, self.store, self.cfg = setup_test_fixture(self.root, "safe-integration")
        self.cfg.update(executor="safe_qwen", startup_timeout_seconds=30, timeout_seconds=60)
        self.cfg["roles"]["code"] = "operator-qwen"
        self.cfg["base_url"] = "http://127.0.0.1:9333"
        self.resource = self.root / "runtime"
        self.resource.mkdir()
        for name in ("server.bin", "fixture-model.bin", "gateway.json"):
            (self.resource / name).write_text("offline fixture only")
        (self.resource / "gateway.bin").write_text("offline gateway identity only")
        (self.resource / "gateway.pid").write_text("12345\n")
        (self.resource / "gateway.json").write_text('models:\n  operator-qwen:\n    cmd: >-\n      "' +
            (self.resource / "server.bin").as_posix() + '" --model "' +
            (self.resource / "fixture-model.bin").as_posix() + '"\n')
        self.cfg["safe_qwen_gateway"] = {"binary": "runtime/gateway.bin", "pid_file": "runtime/gateway.pid"}
        self.cfg["safe_qwen_runtime"] = {"code": self.files()}
        self.data = {"readable": ["calculator.py", "test_calculator.py"], "writable": ["calculator.py"],
                     "mode": "mutation", "max_actions": 4, "unit_timeout_seconds": 60}
        self.path = self.root / "operator-config.json"
        self.path.write_text(json.dumps(self.cfg))
        self.policy_path = self.root / "policy.json"
        self.policy_path.write_text(json.dumps(self.data))
        self.command = ["python", "-m", "unittest", "test_calculator.py"]

    def files(self):
        return {"binary": "runtime/server.bin", "model": "runtime/fixture-model.bin", "gateway_config": "runtime/gateway.json"}

    def configure(self, critic=False, reviewer=False):
        cfg = copy.deepcopy(self.cfg)
        if critic:
            cfg["safe_qwen_runtime"]["critic"] = self.files()
        if reviewer:
            cfg["safe_qwen_runtime"]["review"] = self.files()
        return launch.configure_worker(cfg, self.repo.root, self.data, self.path, critic=critic, reviewer=reviewer)

    def replies(self, correct=True):
        replies = []
        if correct:
            replies.append(operation("edit_file", path="calculator.py",
                before_sha256=sha((self.repo.root / "calculator.py").read_bytes()),
                old_text="return 0", new_text="return a * b"))
        return replies + [submit()]

    def prepared(self, critic=False, reviewer=False):
        worker, cfg = self.configure(critic, reviewer)
        registry = {"check-1": {"argv": self.command, "timeout_seconds": 30}}
        unit = launch.fixed_unit("Implement multiply", cfg, worker, list(registry))
        manager.create_run(self.store, self.repo, "Implement multiply", [self.command], cfg,
                           criteria=["multiply"], work_units=[unit], verifier_registry=registry,
                           critic=critic, reviewer=reviewer, allow=["calculator.py"], forbid=["test_calculator.py"],
                           executor=worker)
        return worker, cfg

    def execute(self, critic=False, reviewer=False, correct=True, review="approve"):
        worker, cfg = self.prepared(critic, reviewer)
        gw = ScriptedGateway(cfg, self.replies(correct))
        def factory(repo, gateway, config, log):
            agents = launch.agents_factory(worker)(repo, gateway, config, log)
            agents.critic = ScriptedCritic("clean") if critic else None
            agents.reviewer = ScriptedReviewer(review) if reviewer else None
            return agents
        result = manager.execute(self.store, agents=factory, gateway=gw)
        return result, worker, cfg, gw, factory

    def args(self, **changes):
        value = dict(command="autonomous-run", executor="safe_qwen", config=self.path, repo=self.repo.root,
                     task="Implement multiply", verify=[json.dumps(self.command)], acceptance=["multiply"],
                     allow=["calculator.py"], forbid=["test_calculator.py"], critic=False, reviewer=False,
                     review_policy="risk", quiet=True, dry_run=True, safeqwen_policy=self.policy_path,
                     max_rounds=None, max_runtime_minutes=None, max_step_retries=None,
                     max_consecutive_failures=None, max_stall_rounds=None, round_timeout_minutes=None,
                     revalidate_environment=False)
        value.update(changes)
        return SimpleNamespace(**value)

    def test_configurable_alias_endpoint_and_source_pins(self):
        worker, cfg = self.configure()
        self.assertEqual(cfg["base_url"], "http://127.0.0.1:9333")
        self.assertEqual(cfg["roles"]["code"], "operator-qwen")
        self.assertEqual(set(binding.identity(worker)["sources"]), set(binding.SOURCES))
        self.assertEqual(binding.restore_worker(cfg).policy, worker.policy)
        self.assertFalse(self.path.read_text().find("safe_qwen_identity") >= 0)

    def test_invalid_endpoints_fail_before_gateway(self):
        for value in ("https://127.0.0.1:9333", "http://localhost:9333", "http://192.0.2.1:9333",
                      "http://127.0.0.1:9333@elsewhere", "http://127.0.0.1:9333/v1"):
            self.cfg["base_url"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.configure()

    def test_nonfinite_and_unbounded_config_fail(self):
        for value in (True, 0, -1, float("nan"), float("inf"), 3601):
            self.cfg["request_timeout_seconds"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.configure()

    def test_runtime_files_and_roles_required(self):
        for resources in ({}, {"code": {"binary": "missing"}}, {"code": self.files(), "review": self.files()}):
            self.cfg["safe_qwen_runtime"] = resources
            with self.assertRaises(ValueError):
                self.configure()

    def test_hotpin_implicit_profile_is_refused(self):
        self.cfg["review_profiles"] = {"review-safe": {"hot_experts": "operator-path"}}
        with self.assertRaises(ValueError):
            self.configure()

    def test_reviewer_without_critic_is_refused(self):
        with self.assertRaisesRegex(ValueError, "requires critic"):
            self.configure(reviewer=True)

    def test_policy_duplicate_unknown_nonfinite_and_type_bounds(self):
        values = ['{"mode":"mutation","mode":"read_only"}', '{}', '{"max_actions":NaN}']
        for key, value in (("max_actions", True), ("max_actions", 33), ("unit_timeout_seconds", 601),
                           ("readable", "calculator.py"), ("mode", "shell")):
            values.append(json.dumps({**self.data, key: value}))
        for value in values:
            self.policy_path.write_text(value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                launch.read_policy(self.policy_path)

    def test_policy_path_alias_and_outside_scope_fail(self):
        for paths in (["calculator.py", "Calculator.py"], ["../calculator.py"], [".git/config"], ["C:/outside.py"]):
            self.data["readable"] = paths
            with self.assertRaises((Denied, OSError)):
                self.configure()

    def test_unc_root_rejected_without_filesystem_access(self):
        for path in (r"\\server\share\repo", r"\\?\C:\repo", "//server/share/repo"):
            with patch("safe_qwen_worker._regular_components") as stat, self.assertRaises(Denied):
                Policy(Path(path), (), (), (), ())
            stat.assert_not_called()

    def test_case_insensitive_forbidden_scope_precedes_transport(self):
        worker, cfg = self.configure()
        unit = launch.fixed_unit("x", cfg, worker, ["check-1"])
        unit["scope"]["forbidden_paths"] = ["CALCULATOR.PY"]
        transport = Mock()
        pure = SafeQwenWorker(worker.policy, transport=transport, audit=lambda x: None)
        with self.assertRaises(Denied):
            pure.execute(unit, {}, self.repo)
        transport.assert_not_called()

    def test_dry_run_controller_route_no_model_no_runs(self):
        result = manager.cli(self.args(), self.root, self.cfg)
        self.assertTrue(result["dry_run"])
        self.assertFalse(result["models_contacted"])
        self.assertEqual(result["executor"], "safe_qwen")
        self.assertIn("fixed scope", result["policy"]["work_unit"])
        self.assertFalse((self.root / "runs").exists())

    def test_missing_policy_and_mismatched_scope_fail_closed(self):
        for args in (self.args(safeqwen_policy=None), self.args(allow=["other.py"]), self.args(forbid=[])):
            with self.assertRaises(ValueError):
                manager.cli(args, self.root, self.cfg)

    def test_cli_flag_on_default_route_is_refused(self):
        with self.assertRaisesRegex(ValueError, "requires --executor"):
            manager.cli(self.args(), self.root, {**self.cfg, "executor": "custom"})

    def test_cli_dry_run_output_and_exit_zero(self):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(harness, "ROOT", self.root), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = harness.main(["--config", str(self.path), "autonomous-run", "--executor", "safe_qwen",
                "--safeqwen-policy", str(self.policy_path), "--repo", str(self.repo.root), "--task", "multiply",
                "--verify", json.dumps(self.command), "--acceptance", "multiply", "--allow", "calculator.py",
                "--forbid", "test_calculator.py", "--dry-run", "--quiet"])
        self.assertEqual(status, 0, err.getvalue())
        self.assertEqual(json.loads(out.getvalue())["executor"], "safe_qwen")
        self.assertEqual(err.getvalue(), "")

    def test_no_critic_reviewer_means_intermediate_not_checkpoint(self):
        result, worker, cfg, gw, factory = self.execute()
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["stop"]["reason"], "MILESTONE_READY")
        self.assertIsNone(self.store.state["last_verified_checkpoint"])
        self.assertEqual([x for x in gw.events if x[0] == "request"], [("request", "operator-qwen")] * 2)

    def test_critic_only_never_creates_checkpoint(self):
        result, *_ = self.execute(critic=True)
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["stop"]["reason"], "CRITIC_CLEAN")
        self.assertIsNone(self.store.state["last_verified_checkpoint"])

    def test_false_submit_cannot_override_real_verifier(self):
        result, *_ = self.execute(correct=False)
        self.assertEqual(result["status"], "FAILED")
        self.assertIsNone(self.store.state["last_verified_checkpoint"])
        self.assertEqual(self.store.state["work_units"]["units"]["safe-qwen-task"]["status"], "UNIT_FAILED")

    def test_review_reject_never_accepts_candidate(self):
        result, *_ = self.execute(critic=True, reviewer=True, review="reject")
        self.assertNotEqual(result["status"], "COMPLETED")
        self.assertIsNone(self.store.state["last_verified_checkpoint"])

    def test_controller_acceptance_and_completed_reload_idempotence(self):
        result, worker, cfg, gw, factory = self.execute(critic=True, reviewer=True)
        self.assertEqual(result["status"], "COMPLETED")
        checkpoint = copy.deepcopy(self.store.state["last_verified_checkpoint"])
        self.assertIsNotNone(checkpoint)
        self.assertEqual(len(self.store.state["verified_progress"]), 1)
        reload = durable.Store(self.root / "runs", self.store.state["run_id"])
        reload.load()
        restored = binding.restore_worker(cfg)
        manager.prepare_resume(reload, executor=restored)
        # No worker/review/final redispatch on a fully bound terminal checkpoint.
        empty_gw = ScriptedGateway(cfg, [])
        result = manager.execute(reload, agents=launch.agents_factory(restored), gateway=empty_gw)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(reload.state["last_verified_checkpoint"], checkpoint)
        self.assertEqual([e for e in empty_gw.events if e[0] == "request"], [])

    def test_completed_repository_drift_refuses_shortcut(self):
        result, worker, cfg, gw, factory = self.execute(critic=True, reviewer=True)
        (self.repo.root / "calculator.py").write_text("def multiply(a,b): return 0\n")
        result = manager.execute(self.store, agents=factory, gateway=gw)
        self.assertEqual(result["status"], "HUMAN_ACTION_REQUIRED")
        self.assertEqual(result["stop"]["reason"], "REPOSITORY_DRIFT")

    def test_wrong_executor_refused_without_rebinding_fingerprint(self):
        worker, cfg = self.prepared()
        before = copy.deepcopy(self.store.state["environment"])
        from work_unit_scheduler import ScriptedExecutor
        with self.assertRaises(durable.DurableError):
            durable.bind_work_unit_executor(self.store, ScriptedExecutor())
        self.assertEqual(self.store.state["environment"], before)
        self.assertIs(self.store._effective_work_unit_executor, worker)

    def test_changed_configuration_or_source_pin_refused(self):
        worker, cfg = self.configure()
        for mutate in (lambda c: c.update(base_url="http://127.0.0.1:9444"),
                       lambda c: c["safe_qwen_identity"]["sources"].update({"tool_protocol.py": "0" * 64})):
            bad = copy.deepcopy(cfg)
            mutate(bad)
            with self.assertRaises(ValueError):
                binding.restore_worker(bad)

    def test_actual_source_drift_refused_without_editing_sources(self):
        worker, cfg = self.configure()
        original = binding.file_sha
        with patch.object(binding, "file_sha", side_effect=lambda p: "0" * 64 if Path(p).name == "tool_protocol.py" else original(p)):
            with self.assertRaises(ValueError):
                binding.validate_pinned(cfg, worker)

    def test_runtime_binary_drift_is_unsafe(self):
        worker, cfg = self.prepared()
        (self.resource / "server.bin").write_text("changed runtime")
        drift = environment.compare(self.store.state["environment"], durable.current_fingerprint(self.store))
        self.assertEqual(drift["decision"], "UNSAFE")
        with self.assertRaisesRegex(durable.DurableError, "ENVIRONMENT_DRIFT"):
            manager.execute(self.store, agents=launch.agents_factory(worker), gateway=ScriptedGateway(cfg, []))

    def test_resume_requires_actual_effective_executor_after_reload(self):
        self.prepared()
        reloaded = durable.Store(self.root / "runs", self.store.state["run_id"])
        reloaded.load()
        with self.assertRaises(durable.DurableError):
            manager.prepare_resume(reloaded)

    def test_keyboard_interrupt_unloads_and_restores_deadline(self):
        worker, cfg = self.prepared()
        gw = ScriptedGateway(cfg, [KeyboardInterrupt()])
        gw.supervisor = SimpleNamespace(store=self.store)
        gw.execution_deadline = time.monotonic() + 100
        previous = gw.execution_deadline
        worker.bind_gateway(gw)
        repo = SimpleNamespace(root=self.repo.root, store=self.store)
        unit = launch.fixed_unit("x", cfg, worker, ["check-1"])
        with self.assertRaises(KeyboardInterrupt):
            worker.execute(unit, {}, repo)
        self.assertEqual(gw.execution_deadline, previous)
        self.assertIsNone(gw.active_role)
        self.assertEqual(gw.events[-1][0], "unload")

    def test_start_failure_unloads_and_restores_deadline(self):
        worker, cfg = self.prepared()
        gw = ScriptedGateway(cfg, [])
        gw.supervisor = SimpleNamespace(store=self.store)
        gw.switch = Mock(side_effect=RuntimeError("startup interrupted"))
        worker.bind_gateway(gw)
        with self.assertRaises(RuntimeError):
            worker.execute(launch.fixed_unit("x", cfg, worker, ["check-1"]), {}, SimpleNamespace(root=self.repo.root, store=self.store))
        self.assertEqual(gw.events[-1][0], "unload")
        self.assertIsNone(gw.execution_deadline)

    def test_imports_do_not_start_models_network_or_processes(self):
        with no_process(), patch("socket.socket", side_effect=AssertionError("network")):
            for path in binding.SOURCES.values():
                runpy.run_path(str(path), run_name="safe_import_probe")

    def test_legacy_default_opencode_and_cline_selection_unchanged(self):
        from opencode_executor import OpenCodeExecutor
        from live_work_unit_executor import LiveWorkUnitExecutor
        for value, expected in (("custom", manager.ModelExecutor), ("opencode", OpenCodeExecutor), ("cline", LiveWorkUnitExecutor)):
            agents = manager.default_agents(self.repo, ScriptedGateway(self.cfg, []), {**self.cfg, "executor": value}, None)
            self.assertIsInstance(agents.executor, expected)
        # Ordinary custom fingerprints never include SafeQwen source/policy identities.
        cfg = {k: v for k, v in self.cfg.items() if not k.startswith("safe_qwen")}
        cfg["executor"] = "custom"
        fp = environment.capture(cfg, self.repo.root, [self.command])
        self.assertNotIn("safe_qwen", fp)
        self.assertFalse(any(k.startswith("safe_qwen:") for k in fp["runtime"]))

    def test_actual_cli_launcher_uses_controller_with_scripted_gateway(self):
        gw = ScriptedGateway(self.cfg, self.replies())
        original = manager.execute
        def controlled(store, **kwargs):
            gw.config = store.state["options"]["config"]
            return original(store, gateway=gw, **kwargs)
        with patch.object(manager, "execute", side_effect=controlled):
            result = manager.cli(self.args(dry_run=False), self.root, self.cfg)
        self.assertEqual(result["status"], "READY")
        run = next(p for p in (self.root / "runs").iterdir() if p.is_dir())
        reloaded = durable.Store(self.root / "runs", run.name)
        reloaded.load()
        self.assertEqual(reloaded.state["options"]["work_unit_executor"]["kind"], "safe_qwen")
        self.assertIsNone(reloaded.state["last_verified_checkpoint"])

    def test_actual_cli_resume_reconstructs_pinned_worker(self):
        worker, cfg = self.prepared()
        original = manager.execute
        def controlled(store, **kwargs):
            return original(store, gateway=ScriptedGateway(cfg, self.replies()), **kwargs)
        args = self.args(command="autonomous-resume", run_id=self.store.state["run_id"])
        with patch.object(manager, "execute", side_effect=controlled):
            result = manager.cli(args, self.root)
        self.assertEqual(result["status"], "READY")
        reloaded = durable.Store(self.root / "runs", self.store.state["run_id"])
        reloaded.load()
        self.assertEqual(len(reloaded.state["work_units"]["units"]["safe-qwen-task"]["attempts"]), 1)
        self.assertIsNone(reloaded.state["last_verified_checkpoint"])

    def test_read_only_cli_contract_and_no_edit(self):
        self.data.update(mode="read_only", writable=[])
        self.policy_path.write_text(json.dumps(self.data))
        result = manager.cli(self.args(allow=self.data["readable"], forbid=[]), self.root, self.cfg)
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["safe_qwen_identity"]["scope"]["writable"], [])
        worker, cfg = self.configure()
        unit = launch.fixed_unit("Read only", cfg, worker, ["check-1"])
        self.assertEqual(unit["scope"]["forbidden_paths"], [])
        self.assertEqual(set(worker.policy.frozen), set(self.data["readable"]))

    def test_unit_bounds_cannot_exceed_pinned_launcher_contract(self):
        worker, cfg = self.prepared()
        gw = ScriptedGateway(cfg, [])
        gw.supervisor = SimpleNamespace(store=self.store)
        worker.bind_gateway(gw)
        unit = launch.fixed_unit("x", cfg, worker, ["check-1"])
        unit["limits"]["max_attempts"] = 2
        with self.assertRaises(manager.ExecutorError) as failure:
            worker.execute(unit, {}, SimpleNamespace(root=self.repo.root, store=self.store))
        self.assertEqual(failure.exception.reason, "CONTRACT_MISMATCH")
        self.assertEqual(gw.events, [])

    def test_intermediate_dirty_resume_stays_fail_closed(self):
        self.execute()
        args = self.args(command="autonomous-resume", run_id=self.store.state["run_id"])
        before = (self.repo.root / "calculator.py").read_bytes()
        with self.assertRaisesRegex(durable.DurableError, "dirty/manual"):
            manager.cli(args, self.root)
        self.assertEqual((self.repo.root / "calculator.py").read_bytes(), before)

    def test_supervisor_configured_gateway_identity_and_lineage(self):
        import os
        from supervision import Supervisor
        worker, cfg = self.prepared()
        sup = Supervisor(self.store)
        # Match winprocess.identity: canonicalize Windows short/temp path aliases.
        parent = {"executable": os.path.normcase(str((self.resource / "gateway.bin").resolve())), "created": "fixture-parent"}
        sup.inspect = Mock(return_value=parent)
        child_id = sup.model_starting("code")
        child = sup.child(child_id)
        self.assertEqual(child["parent_pid"], 12345)
        self.assertEqual(child["parent_identity"], parent)
        self.assertEqual(child["containment"], "delegated")
        self.assertEqual(child["state"], "STARTING")
        # An unrelated parent cannot become an owned child or authorize cleanup.
        with self.assertRaises(RuntimeError):
            sup.model_observed(child_id, [{"parent_pid": 999, "pid": 88}])
        self.assertEqual(sup.child(child_id)["state"], "UNKNOWN")

    def test_supervisor_wrong_gateway_executable_fail_closed(self):
        from supervision import Supervisor
        self.prepared()
        sup = Supervisor(self.store)
        sup.inspect = Mock(return_value={"executable": "unrelated.exe", "created": "unrelated"})
        with self.assertRaises(durable.DurableError):
            sup.model_starting("code")
        self.assertEqual(self.store.state["supervision"]["children"], {})

    def test_supervisor_malformed_command_pid_or_role_fail_closed(self):
        from supervision import Supervisor
        original_command = (self.resource / "gateway.json").read_text()
        for change in ("command", "pid", "role"):
            (self.resource / "gateway.json").write_text(original_command)
            (self.resource / "gateway.pid").write_text("12345\n")
            worker, cfg = self.configure()
            if change == "command":
                (self.resource / "gateway.json").write_text("models: {}")
            elif change == "pid":
                (self.resource / "gateway.pid").write_text("not-a-pid")
            self.store = durable.Store(self.root / "runs", "ownership-" + change)
            registry = {"check-1": {"argv": self.command}}
            manager.create_run(self.store, self.repo, "x", [self.command], cfg, criteria=["x"],
                               work_units=[launch.fixed_unit("x", cfg, worker, ["check-1"])],
                               verifier_registry=registry, executor=worker)
            with self.assertRaises(durable.DurableError):
                Supervisor(self.store).model_starting("review" if change == "role" else "code")
            self.assertEqual(self.store.state["supervision"]["children"], {})

    def test_launcher_refuses_controller_cleanup_failure(self):
        worker, cfg = self.prepared(critic=True, reviewer=True)
        gw = ScriptedGateway(cfg, self.replies())
        original_unload = gw.unload
        def unload():
            if self.store.state.get("last_verified_checkpoint"):
                raise RuntimeError("injected final cleanup fault")
            return original_unload()
        gw.unload = unload
        original = manager.execute
        def controlled(store, **kwargs):
            factory = kwargs.pop("agents")
            def reviewed(repo, gateway, config, log):
                agents = factory(repo, gateway, config, log)
                agents.critic = ScriptedCritic("clean")
                agents.reviewer = ScriptedReviewer("approve")
                return agents
            return original(store, agents=reviewed, gateway=gw, **kwargs)
        with patch.object(manager, "execute", side_effect=controlled):
            with self.assertRaisesRegex(durable.DurableError, "cleanup unproven"):
                launch.execute_bound(self.store, worker)
        # Preserve the original controller's recorded diagnostic, never fabricate recovery.
        self.assertEqual(self.store.state["manager"]["stop"]["cleanup"], "failed")

    def test_legacy_budget_flags_are_refused_not_silently_ignored(self):
        for args in (self.args(max_model_invocations=["code=1"]), self.args(max_rounds=1),
                     self.args(round_timeout_minutes=1), self.args(max_step_retries=0)):
            with self.assertRaisesRegex(ValueError, "only --max-runtime"):
                manager.cli(args, self.root, self.cfg)
        self.cfg["manager"] = {"budgets": {"max_model_invocations": {"code": 1}}}
        with self.assertRaisesRegex(ValueError, "only --max-runtime"):
            manager.cli(self.args(), self.root, self.cfg)

    def test_target_cannot_contain_controller_store(self):
        with self.assertRaisesRegex(ValueError, "Store must be outside"):
            manager.cli(self.args(), self.repo.root, self.cfg)
        self.assertFalse((self.repo.root / "runs").exists())
