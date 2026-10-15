"""Production offline integration test for SafeQwen multi-work-unit lifecycle and negative regression gate.

Exercises the complete production pipeline with zero live inference:
  Manager
   -> WorkUnitScheduler
   -> SafeQwenExecutor
   -> deterministic subprocess verification
   -> critic (ScriptedCritic)
   -> senior reviewer (ScriptedReviewer)
   -> final verification (all 3 commands = 15 tests)
   -> terminal acceptance
   -> fresh Store reload

Validates:
1. Production multi-unit full lifecycle acceptance (Unit 1: 10 tests, Unit 2: 5 tests, Final: 15 tests).
2. Negative acceptance test: Unit 2 addition regression detected by production verification gate;
   terminal acceptance refused, failing gate recorded, no trusted checkpoint created.
"""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

import contextlib
import durable
import manager
import safe_executor_binding as binding
import terminal_run as launch
import work_units
from critic_executor import ScriptedCritic
from reviewer_executor import ScriptedReviewer
from safe_qwen_worker import sha
from test_harness import CONFIG
from test_safe_qwen_worker import operation, submit


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



FIXTURES_DIR = ROOT / "tests" / "fixtures" / "live002"
FROZEN_TEST_CALCULATOR = (FIXTURES_DIR / "test_calculator.py").read_text(encoding="utf-8")
BASELINE_CALCULATOR = (FIXTURES_DIR / "calculator.py").read_text(encoding="utf-8")


def setup_multi_unit_fixture(root, run_id="safe-multi-unit-prod"):
    repo_path = root / "repo"
    if not repo_path.exists():
        repo_path.mkdir(parents=True)
        (repo_path / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
        (repo_path / "calculator.py").write_text(BASELINE_CALCULATOR, encoding="utf-8")
        (repo_path / "test_calculator.py").write_text(FROZEN_TEST_CALCULATOR, encoding="utf-8")

        def git(*args):
            return subprocess.run(
                ["git", "-C", str(repo_path), "-c", f"safe.directory={repo_path}",
                 "-c", "user.name=Live002 Test", "-c", "user.email=live002@localhost", *args],
                check=True, capture_output=True
            )

        git("init", "-q")
        git("add", ".")
        git("commit", "-qm", "baseline")

    config = copy.deepcopy(CONFIG)
    config.update({
        "version": 1,
        "max_retries": 0,
        "startup_timeout_seconds": 30,
        "timeout_seconds": 60,
        "executor": "safe_qwen",
        "multi_work_unit": True,
        "terminal_acceptance": True,
        "base_url": "http://127.0.0.1:9333",
        "roles": {"code": "operator-qwen", "critic": "llm-critic", "review": "llm-review"},
        "model_metadata": {
            "operator-qwen": {"display_name": "Qwen3-Coder-30B", "runtime": "llama.cpp"},
            "llm-critic": {"display_name": "Nemotron-3.5-Lightning-30B", "runtime": "llama.cpp"},
            "llm-review": {"display_name": "GPT-OSS-120B", "runtime": "llama.cpp"},
        },
        "review_policy": "three-model",
        "critic": {"min_available_ram_gb": 30, "request_timeout_seconds": 180, "max_input_chars": 16000},
        "reviewer": {"min_available_ram_gb": 44, "request_timeout_seconds": 300, "max_input_chars": 20000},
    })

    resource = root / "runtime"
    resource.mkdir(parents=True, exist_ok=True)
    for name in ("server.bin", "fixture-model.bin"):
        (resource / name).write_text("offline fixture only", encoding="utf-8")
    (resource / "gateway.bin").write_text("offline gateway identity only", encoding="utf-8")
    (resource / "gateway.pid").write_text("12345\n", encoding="utf-8")
    (resource / "gateway.json").write_text(
        'models:\n  operator-qwen:\n    cmd: >-\n      "' +
        (resource / "server.bin").as_posix() + '" --model "' +
        (resource / "fixture-model.bin").as_posix() + '"\n',
        encoding="utf-8"
    )

    config["safe_qwen_gateway"] = {"binary": "runtime/gateway.bin", "pid_file": "runtime/gateway.pid"}
    runtime_code = {"binary": "runtime/server.bin", "model": "runtime/fixture-model.bin", "gateway_config": "runtime/gateway.json"}
    config["safe_qwen_runtime"] = {"code": runtime_code, "critic": runtime_code, "review": runtime_code}

    policy_data = {
        "readable": ["calculator.py", "test_calculator.py"],
        "writable": ["calculator.py"],
        "mode": "mutation",
        "max_actions": 4,
        "unit_timeout_seconds": 60,
    }

    config_path = root / "operator-config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    policy_path = root / "policy.json"
    policy_path.write_text(json.dumps(policy_data), encoding="utf-8")

    import harness
    log = harness.EventLog(root / f"{run_id}-preflight.jsonl")
    repo = harness.Repository(repo_path, config, log)
    store = durable.Store(root / "runs", run_id)
    return repo, store, config, policy_data, config_path


class SafeQwenMultiUnitProductionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

        self.cmd_multiply = ["python", "-m", "unittest", "test_calculator.RequiredMultiplicationTests"]
        self.cmd_add = ["python", "-m", "unittest", "test_calculator.ExistingAdditionTests"]
        self.cmd_scale = ["python", "-m", "unittest", "test_calculator.RequiredScaleTests"]

        self.registry = {
            "check-multiply": {"argv": self.cmd_multiply, "timeout_seconds": 30},
            "check-add-regression": {"argv": self.cmd_add, "timeout_seconds": 30},
            "check-scale": {"argv": self.cmd_scale, "timeout_seconds": 30},
        }

    def test_01_production_multi_unit_full_lifecycle_acceptance(self):
        repo, store, cfg, policy_data, cfg_path = setup_multi_unit_fixture(self.root, "prod-pos")
        worker, cfg = launch.configure_worker(cfg, repo.root, policy_data, cfg_path, critic=True, reviewer=True)

        units = launch.fixed_two_units(
            "Implement multiply in calculator.py", ["check-multiply", "check-add-regression"],
            "Implement scale in calculator.py", ["check-scale"],
            cfg, worker,
        )

        all_commands = [self.cmd_multiply, self.cmd_add, self.cmd_scale]
        manager.create_run(
            store, repo, "Implement calculator multi-unit", all_commands, cfg,
            criteria=["multiply", "add", "scale"],
            critic=True, reviewer=True, review_policy="three-model",
            allow=["calculator.py"], forbid=["test_calculator.py"],
            work_units=units, verifier_registry=self.registry, executor=worker,
            terminal_acceptance=True
        )

        # Build scripted model edits
        init_calc = (repo.root / "calculator.py").read_bytes()
        sha_init = sha(init_calc)

        # Unit 1: implement multiply (preserves add)
        u1_edit = operation(
            "edit_file",
            path="calculator.py",
            before_sha256=sha_init,
            old_text='raise NotImplementedError("Implement multiply without changing add or frozen tests")',
            new_text='return a * b'
        )
        u1_submit = submit()

        u1_calc = init_calc.decode("utf-8").replace(
            'raise NotImplementedError("Implement multiply without changing add or frozen tests")',
            'return a * b'
        ).encode("utf-8")
        sha_u1 = sha(u1_calc)

        # Unit 2: implement scale (calls multiply)
        u2_edit = operation(
            "edit_file",
            path="calculator.py",
            before_sha256=sha_u1,
            old_text='raise NotImplementedError("Implement scale using multiply without changing frozen tests")',
            new_text='return [multiply(v, factor) for v in values]'
        )
        u2_submit = submit()

        gw = ScriptedGateway(cfg, [u1_edit, u1_submit, u2_edit, u2_submit])

        def factory(repo_arg, gateway_arg, config_arg, log_arg):
            agents = launch.agents_factory(worker)(repo_arg, gateway_arg, config_arg, log_arg)
            agents.critic = ScriptedCritic("clean")
            agents.reviewer = ScriptedReviewer("approve")
            return agents

        # Execute full production flow
        result = manager.execute(store, agents=factory, gateway=gw, terminal_acceptance=True)

        # 1. Terminal acceptance is valid
        self.assertTrue(store.is_terminal_accepted())
        self.assertEqual(store.state.get("status"), "COMPLETED")

        # 2. Sequence integrity & dispatch ordering
        wu_section = store.state["work_units"]
        self.assertEqual(wu_section["sequence"], ["unit-1-multiply", "unit-2-scale"])
        self.assertEqual(wu_section["units"]["unit-1-multiply"]["ordinal"], 0)
        self.assertEqual(wu_section["units"]["unit-2-scale"]["ordinal"], 1)

        # 3. Unit 1 verification executes 6+4 = 10 tests
        u1 = wu_section["units"]["unit-1-multiply"]
        self.assertEqual(u1["status"], "UNIT_VERIFIED")
        self.assertEqual(len(u1["attempts"]), 1)
        self.assertEqual(u1["attempts"][0]["outcome"], "passed")
        u1_verifs = u1["result"]["verifier_evidence"]["verifiers"]
        self.assertTrue(u1_verifs["check-multiply"]["passed"])
        self.assertTrue(u1_verifs["check-add-regression"]["passed"])
        u1_mult_out = u1_verifs["check-multiply"].get("stdout", "") + u1_verifs["check-multiply"].get("stderr", "")
        u1_add_out = u1_verifs["check-add-regression"].get("stdout", "") + u1_verifs["check-add-regression"].get("stderr", "")
        self.assertIn("Ran 6 tests", u1_mult_out)
        self.assertIn("Ran 4 tests", u1_add_out)

        # 4. Unit 2 dependency & verification executes 5 tests
        u2 = wu_section["units"]["unit-2-scale"]
        self.assertEqual(u2["spec"]["dependencies"], ["unit-1-multiply"])
        self.assertEqual(u2["status"], "UNIT_VERIFIED")
        self.assertEqual(len(u2["attempts"]), 1)
        self.assertEqual(u2["attempts"][0]["outcome"], "passed")
        u2_verifs = u2["result"]["verifier_evidence"]["verifiers"]
        self.assertTrue(u2_verifs["check-scale"]["passed"])
        u2_scale_out = u2_verifs["check-scale"].get("stdout", "") + u2_verifs["check-scale"].get("stderr", "")
        self.assertIn("Ran 5 tests", u2_scale_out)

        # 5. Production executor receipts retain authentic unit IDs
        receipt_files = list(store.directory.glob("**/result.json"))
        receipts = {}
        for rf in receipt_files:
            data = json.loads(rf.read_text(encoding="utf-8"))
            if "unit_id" in data:
                receipts[data["unit_id"]] = data
        self.assertIn("unit-1-multiply", receipts)
        self.assertIn("unit-2-scale", receipts)
        self.assertEqual(receipts["unit-1-multiply"]["cleanup"], "passed")
        self.assertEqual(receipts["unit-2-scale"]["cleanup"], "passed")

        # 6. Final verification executes all 3 registered commands = 15 tests total
        checkpoint_ref = store.state["last_verified_checkpoint"]["reference"]
        checkpoint = store.read_evidence(checkpoint_ref)
        final_verif_ref = checkpoint["verification"][-1]
        final_verif = store.read_evidence(final_verif_ref)
        self.assertTrue(final_verif["passed"])

        final_verifs = final_verif["verifiers"]
        self.assertEqual(len(final_verifs), 3)
        final_argvs = [c["argv"] for c in final_verifs]
        self.assertIn(self.cmd_multiply, final_argvs)
        self.assertIn(self.cmd_add, final_argvs)
        self.assertIn(self.cmd_scale, final_argvs)

        # Check total tests: 6 + 4 + 5 = 15
        total_tests = 0
        for cmd_info in final_verifs:
            self.assertEqual(cmd_info["exit_code"], 0)
            out = cmd_info.get("stdout", "") + cmd_info.get("stderr", "")
            if "RequiredMultiplicationTests" in cmd_info["argv"][-1]:
                self.assertIn("Ran 6 tests", out)
                total_tests += 6
            elif "ExistingAdditionTests" in cmd_info["argv"][-1]:
                self.assertIn("Ran 4 tests", out)
                total_tests += 4
            elif "RequiredScaleTests" in cmd_info["argv"][-1]:
                self.assertIn("Ran 5 tests", out)
                total_tests += 5
        self.assertEqual(total_tests, 15)

        # 7. Final evidence recorded in WAL
        events = [r["event"] for r in store.records]
        self.assertIn("final_verification_attempt_started", events)
        self.assertIn("final_verification_attempt_ended", events)
        self.assertTrue(any(r.get("terminal") is True for r in store.records))

        # 8. Fresh Store reload validates acceptance
        reloaded = durable.Store(store.directory.parent, store.directory.name)
        reloaded.load()
        self.assertTrue(reloaded.is_terminal_accepted())
        self.assertIsNotNone(reloaded.state.get("last_verified_checkpoint"))
        self.assertEqual(reloaded.state["status"], "COMPLETED")

    def test_02_negative_addition_regression_blocks_terminal_acceptance(self):
        repo, store, cfg, policy_data, cfg_path = setup_multi_unit_fixture(self.root, "prod-neg")
        worker, cfg = launch.configure_worker(cfg, repo.root, policy_data, cfg_path, critic=True, reviewer=True)

        units = launch.fixed_two_units(
            "Implement multiply in calculator.py", ["check-multiply", "check-add-regression"],
            "Implement scale in calculator.py", ["check-scale"],
            cfg, worker,
        )

        all_commands = [self.cmd_multiply, self.cmd_add, self.cmd_scale]
        manager.create_run(
            store, repo, "Implement calculator multi-unit", all_commands, cfg,
            criteria=["multiply", "add", "scale"],
            critic=True, reviewer=True, review_policy="three-model",
            allow=["calculator.py"], forbid=["test_calculator.py"],
            work_units=units, verifier_registry=self.registry, executor=worker,
            terminal_acceptance=True
        )

        init_calc = (repo.root / "calculator.py").read_bytes()
        sha_init = sha(init_calc)

        # Unit 1: implement multiply cleanly
        u1_edit = operation(
            "edit_file",
            path="calculator.py",
            before_sha256=sha_init,
            old_text='raise NotImplementedError("Implement multiply without changing add or frozen tests")',
            new_text='return a * b'
        )
        u1_submit = submit()

        u1_calc = init_calc.decode("utf-8").replace(
            'raise NotImplementedError("Implement multiply without changing add or frozen tests")',
            'return a * b'
        ).encode("utf-8")
        sha_u1 = sha(u1_calc)

        # Unit 2: implements scale BUT ALSO breaks addition (regression)
        u2_edit_regress = operation(
            "edit_file",
            path="calculator.py",
            before_sha256=sha_u1,
            old_text='    return a + b',
            new_text='    return 0'
        )
        u2_calc_inter = u1_calc.decode("utf-8").replace('    return a + b', '    return 0').encode("utf-8")
        sha_u2_inter = sha(u2_calc_inter)

        u2_edit_scale = operation(
            "edit_file",
            path="calculator.py",
            before_sha256=sha_u2_inter,
            old_text='raise NotImplementedError("Implement scale using multiply without changing frozen tests")',
            new_text='return [multiply(v, factor) for v in values]'
        )
        u2_submit = submit()

        gw = ScriptedGateway(cfg, [u1_edit, u1_submit, u2_edit_regress, u2_edit_scale, u2_submit])

        def factory(repo_arg, gateway_arg, config_arg, log_arg):
            agents = launch.agents_factory(worker)(repo_arg, gateway_arg, config_arg, log_arg)
            agents.critic = ScriptedCritic("clean")
            agents.reviewer = ScriptedReviewer("approve")
            return agents

        # Execute flow: addition regression must be caught by deterministic verification
        result = manager.execute(store, agents=factory, gateway=gw, terminal_acceptance=True)

        # 1. Terminal acceptance is NOT committed
        self.assertFalse(store.is_terminal_accepted())
        self.assertEqual(store.state.get("status"), "FAILED")

        # 2. Exact failing gate is recorded in Store state
        stop = store.state.get("manager", {}).get("stop")
        self.assertIsNotNone(stop)
        self.assertEqual(stop["reason"], "STALE_VERIFICATION")
        self.assertIn("check-add-regression failed on revalidation", stop["message"])

        # 3. No forged successful checkpoint appears
        self.assertIsNone(store.state.get("last_verified_checkpoint"))

        # 4. Fresh Store reload validates failure & absence of terminal acceptance
        reloaded = durable.Store(store.directory.parent, store.directory.name)
        reloaded.load()
        self.assertFalse(reloaded.is_terminal_accepted())
        self.assertIsNone(reloaded.state.get("last_verified_checkpoint"))
        self.assertEqual(reloaded.state.get("status"), "FAILED")
        self.assertEqual(reloaded.state.get("manager", {}).get("stop", {}).get("reason"), "STALE_VERIFICATION")


if __name__ == "__main__":
    unittest.main()
