"""Offline verification for SafeQwen opt-in multi-work-unit enablement and security regressions.

Proves:
1. Legacy single-unit mode still works.
2. Two registered distinct units accepted; Unit 1 holds two verifier IDs.
3. Unregistered third unit rejected.
4. Duplicate unit IDs rejected.
5. Unit 2 cannot execute before Unit 1 reaches UNIT_VERIFIED in durable state.
6. Invalid dependency rejected.
7. Scope violations fail closed.
8. Frozen tests cannot be modified.
9. Each unit remains limited to one attempt.
10. Source identity and security checks remain enforced.
11. Regression A: Real WAL Unit 1 = PROPOSED, forged memory Unit 1 = UNIT_VERIFIED -> Unit 2 rejected.
12. Regression B: Forged SimpleNamespace with plausible registered plan -> rejected.
13. Regression C: Genuine persisted Unit 1 VERIFIED -> Unit 2 permitted through production executor.
14. Regression D: Altered verifier or scope in incoming unit specification -> rejected.
15. Production executor receipt records unit_id and persists to durable Store artifact.

Verifier configuration per task specification:
  Unit 1 (unit-1-multiply):
    check-multiply    = python -m unittest test_calculator.RequiredMultiplicationTests  (6 tests)
    check-add-regression = python -m unittest test_calculator.ExistingAdditionTests      (4 tests)
  Unit 2 (unit-2-scale):
    check-scale       = python -m unittest test_calculator.RequiredScaleTests            (5 tests)
  Final verification uses all three verifier IDs -> 15 test cases total.
"""
import copy
import json
from pathlib import Path
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
from test_reviewer_wave6 import setup_test_fixture
from test_safe_qwen_worker import submit


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



class SafeQwenMultiUnitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo, self.store, self.cfg = setup_test_fixture(self.root, "safe-multi-unit")
        self.cfg.update(executor="safe_qwen", startup_timeout_seconds=30, timeout_seconds=60)
        self.cfg["model_metadata"][self.cfg["roles"]["review"]] = {
            "display_name": "Offline generic reviewer", "runtime": "llama.cpp"}
        self.cfg["roles"]["code"] = "operator-qwen"
        self.cfg["base_url"] = "http://127.0.0.1:9333"

        self.resource = self.root / "runtime"
        self.resource.mkdir()
        for name in ("server.bin", "fixture-model.bin", "gateway.json"):
            (self.resource / name).write_text("offline fixture only")
        (self.resource / "gateway.bin").write_text("offline gateway identity only")
        (self.resource / "gateway.pid").write_text("12345\n")
        (self.resource / "gateway.json").write_text(
            'models:\n  operator-qwen:\n    cmd: >-\n      "' +
            (self.resource / "server.bin").as_posix() + '" --model "' +
            (self.resource / "fixture-model.bin").as_posix() + '"\n')
        self.cfg["safe_qwen_gateway"] = {"binary": "runtime/gateway.bin", "pid_file": "runtime/gateway.pid"}
        self.cfg["safe_qwen_runtime"] = {"code": self.files()}

        self.data = {
            "readable": ["calculator.py", "test_calculator.py"],
            "writable": ["calculator.py"],
            "mode": "mutation",
            "max_actions": 4,
            "unit_timeout_seconds": 60,
        }
        self.path = self.root / "operator-config.json"
        self.path.write_text(json.dumps(self.cfg))
        self.policy_path = self.root / "policy.json"
        self.policy_path.write_text(json.dumps(self.data))

        # Three class-based verifier commands covering all 15 tests across two units.
        # Unit 1 carries check-multiply (6 tests) + check-add-regression (4 tests) = 10 tests.
        # Unit 2 carries check-scale (5 tests).
        # All three appear in frozen_policy for final milestone verification = 15 tests total.
        self.cmd_all = ["python", "-m", "unittest", "test_calculator.py"]
        self.cmd_multiply = ["python", "-m", "unittest", "test_calculator.RequiredMultiplicationTests"]
        self.cmd_add = ["python", "-m", "unittest", "test_calculator.ExistingAdditionTests"]
        self.cmd_scale = ["python", "-m", "unittest", "test_calculator.RequiredScaleTests"]

    def files(self):
        return {"binary": "runtime/server.bin", "model": "runtime/fixture-model.bin", "gateway_config": "runtime/gateway.json"}

    def configure(self, multi_unit=False):
        cfg = copy.deepcopy(self.cfg)
        if multi_unit:
            cfg["multi_work_unit"] = True
        return launch.configure_worker(cfg, self.repo.root, self.data, self.path)

    def prepared(self, multi_unit=False):
        worker, cfg = self.configure(multi_unit=multi_unit)
        if multi_unit:
            # Unit 1: two verifiers (10 tests), Unit 2: one verifier (5 tests)
            registry = {
                "check-multiply": {"argv": self.cmd_multiply, "timeout_seconds": 30},
                "check-add-regression": {"argv": self.cmd_add, "timeout_seconds": 30},
                "check-scale": {"argv": self.cmd_scale, "timeout_seconds": 30},
            }
            units = launch.fixed_two_units(
                "Implement multiply", ["check-multiply", "check-add-regression"],
                "Implement scale", ["check-scale"],
                cfg, worker,
            )
        else:
            registry = {"check-all": {"argv": self.cmd_all, "timeout_seconds": 30}}
            units = [launch.fixed_unit("Implement multiply", cfg, worker, list(registry))]
        manager.create_run(
            self.store, self.repo, "Implement calc", [self.cmd_all], cfg,
            criteria=["calc"], work_units=units, verifier_registry=registry,
            allow=["calculator.py"], forbid=["test_calculator.py"], executor=worker
        )
        return worker, cfg, units, registry

    def execute_worker(self, worker, unit_spec, cfg, store=None):
        target_store = store if store is not None else self.store
        gw = ScriptedGateway(cfg, [submit()])
        gw.supervisor = SimpleNamespace(store=target_store)
        worker.bind_gateway(gw)
        repo = SimpleNamespace(root=self.repo.root, store=target_store)
        return worker.execute(unit_spec, {"attempt_number": 1}, repo)

    def mark_unit_verified(self, store, unit_id):
        wu = copy.deepcopy(store.state["work_units"])
        unit = wu["units"][unit_id]
        if unit["status"] == "PROPOSED":
            work_units.transition_unit(unit, "VALIDATED")
        if unit["status"] in ("VALIDATED", "PENDING"):
            work_units.transition_unit(unit, "READY")
        if unit["status"] == "READY":
            work_units.transition_unit(unit, "EXECUTING")
        work_units.create_attempt(unit, "att-1", candidate_id="cand-1")
        work_units.complete_attempt(unit, "att-1", outcome="passed",
                                    candidate_id="cand-1",
                                    verifier_result={"passed": True, "exit_code": 0})
        if unit["status"] == "EXECUTING":
            work_units.transition_unit(unit, "VERIFYING")
        v_ids = unit["spec"]["verifier_ids"]
        work_units.set_unit_result(
            unit,
            verifier_evidence={"passed": True, **{vid: {"passed": True} for vid in v_ids}},
            candidate_id="cand-1",
            attempt_id="att-1"
        )
        work_units.transition_unit(unit, "UNIT_VERIFIED")
        work_units.seal_unit_state(unit)
        work_units._reseal_section(wu)
        store.commit(work_units=wu)

    # 1. Legacy single-unit mode still works
    def test_01_legacy_single_unit_mode_still_works(self):
        worker, cfg, units, registry = self.prepared(multi_unit=False)
        self.assertFalse(cfg.get("multi_work_unit", False))
        unit = units[0]
        self.assertEqual(unit["unit_id"], "safe-qwen-task")

        # Fixed unit passes contract validation
        launch.validate_safe_qwen_unit_contract(unit, cfg, worker, self.store)

        # Wrong unit_id raises CONTRACT_MISMATCH in legacy mode via production executor
        bad_unit = copy.deepcopy(unit)
        bad_unit["unit_id"] = "unit-1-multiply"
        with self.assertRaises(manager.ExecutorError) as ctx:
            self.execute_worker(worker, bad_unit, cfg, self.store)
        self.assertEqual(ctx.exception.reason, "CONTRACT_MISMATCH")

    # 2. Two registered distinct units accepted; Unit 1 holds two verifier IDs
    def test_02_two_registered_distinct_units_accepted(self):
        worker, cfg, units, registry = self.prepared(multi_unit=True)
        self.assertTrue(cfg.get("multi_work_unit"))
        self.assertEqual(len(units), 2)
        self.assertEqual(units[0]["unit_id"], "unit-1-multiply")
        self.assertEqual(units[1]["unit_id"], "unit-2-scale")

        # Unit 1 carries two verifier IDs (10 tests total)
        self.assertEqual(sorted(units[0]["verifier_ids"]),
                         sorted(["check-multiply", "check-add-regression"]))
        # Unit 2 carries one verifier ID (5 tests)
        self.assertEqual(units[1]["verifier_ids"], ["check-scale"])

        # All three verifier IDs registered in registry
        self.assertIn("check-multiply", registry)
        self.assertIn("check-add-regression", registry)
        self.assertIn("check-scale", registry)

        # Unit 1 contract check succeeds
        launch.validate_safe_qwen_unit_contract(units[0], cfg, worker, self.store)

        # Durably commit Unit 1 verified into Store/WAL
        self.mark_unit_verified(self.store, "unit-1-multiply")

        # Unit 2 contract check succeeds once Unit 1 is durably verified
        launch.validate_safe_qwen_unit_contract(units[1], cfg, worker, self.store)

    # 3. Unregistered third unit rejected
    def test_03_unregistered_third_unit_rejected(self):
        worker, cfg, units, registry = self.prepared(multi_unit=True)
        unregistered = copy.deepcopy(units[0])
        unregistered["unit_id"] = "unit-3-unauthorized"
        with self.assertRaises(manager.ExecutorError) as ctx:
            launch.validate_safe_qwen_unit_contract(unregistered, cfg, worker, self.store)
        self.assertEqual(ctx.exception.reason, "CONTRACT_MISMATCH")
        self.assertIn("not in authorized run sequence", str(ctx.exception))

    # 4. Duplicate unit IDs rejected
    def test_04_duplicate_unit_ids_rejected(self):
        worker, cfg = self.configure(multi_unit=True)
        registry = {
            "check-multiply": {"argv": self.cmd_multiply, "timeout_seconds": 30},
            "check-add-regression": {"argv": self.cmd_add, "timeout_seconds": 30},
        }
        u1 = {
            "unit_id": "duplicate-id", "objective": "task1", "dependencies": [], "mode": "mutation",
            "scope": {"allowed_paths": ["calculator.py"], "forbidden_paths": ["test_calculator.py"]},
            "verifier_ids": ["check-multiply", "check-add-regression"], "evidence_inputs": [],
            "limits": {"max_attempts": 1, "timeout_seconds": 60}
        }
        u2 = copy.deepcopy(u1)
        with self.assertRaises(work_units.WorkUnitError):
            work_units.validate_unit_plan(
                [u1, u2],
                parent_scope={"allowed_paths": ["calculator.py"], "forbidden_paths": []},
                verifier_registry=registry
            )

    # 5. Unit 2 cannot execute before Unit 1 reaches UNIT_VERIFIED
    def test_05_unit_2_cannot_execute_before_unit_1_verified(self):
        worker, cfg, units, registry = self.prepared(multi_unit=True)
        # Unit 1 is still PROPOSED
        self.assertEqual(self.store.state["work_units"]["units"]["unit-1-multiply"]["status"], "PROPOSED")
        with self.assertRaises(manager.ExecutorError) as ctx:
            launch.validate_safe_qwen_unit_contract(units[1], cfg, worker, self.store)
        self.assertEqual(ctx.exception.reason, "CONTRACT_MISMATCH")
        self.assertIn("must be UNIT_VERIFIED in durable state", str(ctx.exception))

        # Durably commit Unit 1 verified into Store/WAL
        self.mark_unit_verified(self.store, "unit-1-multiply")

        # Unit 2 passes contract validation
        launch.validate_safe_qwen_unit_contract(units[1], cfg, worker, self.store)

    # 6. Invalid dependency rejected
    def test_06_invalid_dependency_rejected(self):
        worker, cfg = self.configure(multi_unit=True)
        registry = {
            "check-multiply": {"argv": self.cmd_multiply, "timeout_seconds": 30},
            "check-add-regression": {"argv": self.cmd_add, "timeout_seconds": 30},
            "check-scale": {"argv": self.cmd_scale, "timeout_seconds": 30},
        }
        units = launch.fixed_two_units(
            "Mult", ["check-multiply", "check-add-regression"],
            "Scale", ["check-scale"],
            cfg, worker
        )
        units[0]["dependencies"] = ["ghost-unit"]
        with self.assertRaises(work_units.WorkUnitError):
            work_units.validate_unit_plan(
                units,
                parent_scope={"allowed_paths": ["calculator.py"], "forbidden_paths": []},
                verifier_registry=registry
            )

    # 7. Scope violations fail closed
    def test_07_scope_violations_fail_closed(self):
        worker, cfg, units, registry = self.prepared(multi_unit=True)

        # Alter allowed paths to exceed worker policy scope
        tampered = copy.deepcopy(units[0])
        tampered["scope"]["allowed_paths"] = ["calculator.py", "outside.py"]
        with self.assertRaises(manager.ExecutorError) as ctx:
            launch.validate_safe_qwen_unit_contract(tampered, cfg, worker, self.store)
        self.assertEqual(ctx.exception.reason, "CONTRACT_MISMATCH")

        # Unfreeze forbidden paths
        tampered2 = copy.deepcopy(units[0])
        tampered2["scope"]["forbidden_paths"] = []
        with self.assertRaises(manager.ExecutorError) as ctx:
            launch.validate_safe_qwen_unit_contract(tampered2, cfg, worker, self.store)
        self.assertEqual(ctx.exception.reason, "CONTRACT_MISMATCH")

    # 8. Frozen tests cannot be modified
    def test_08_frozen_tests_cannot_be_modified(self):
        worker, cfg = self.configure(multi_unit=True)
        registry = {
            "check-multiply": {"argv": self.cmd_multiply, "timeout_seconds": 30},
            "check-add-regression": {"argv": self.cmd_add, "timeout_seconds": 30},
            "check-scale": {"argv": self.cmd_scale, "timeout_seconds": 30},
        }
        units = launch.fixed_two_units(
            "Mult", ["check-multiply", "check-add-regression"],
            "Scale", ["check-scale"],
            cfg, worker
        )
        # Attempting to put test_calculator.py in allowed_paths
        tampered = copy.deepcopy(units[0])
        tampered["scope"]["allowed_paths"] = ["test_calculator.py"]
        with self.assertRaises(work_units.WorkUnitError):
            work_units.validate_unit_plan(
                [tampered],
                parent_scope={"allowed_paths": ["calculator.py"], "forbidden_paths": ["test_calculator.py"]},
                verifier_registry=registry
            )

    # 9. Each unit remains limited to one attempt
    def test_09_each_unit_remains_limited_to_one_attempt(self):
        worker, cfg, units, registry = self.prepared(multi_unit=True)
        units[0]["limits"]["max_attempts"] = 2

        with self.assertRaises(manager.ExecutorError) as ctx:
            launch.validate_safe_qwen_unit_contract(units[0], cfg, worker, self.store)
        self.assertEqual(ctx.exception.reason, "CONTRACT_MISMATCH")
        self.assertIn("limited to single attempt", str(ctx.exception))

    # 10. Source identity/security checks remain enforced
    def test_10_source_identity_and_security_checks_enforced(self):
        worker, cfg = self.configure(multi_unit=True)
        pin = binding.validate_pinned(cfg, worker)
        self.assertEqual(pin["name"], "SafeQwenExecutor")
        self.assertEqual(pin["version"], 2)

        # Tampering with cfg invalidates configuration_sha256
        tampered_cfg = copy.deepcopy(cfg)
        tampered_cfg["base_url"] = "http://127.0.0.1:9999"
        with self.assertRaises(ValueError):
            binding.validate_pinned(tampered_cfg, worker)

        # Non-boolean multi_work_unit is rejected
        bad_type_cfg = copy.deepcopy(cfg)
        bad_type_cfg["multi_work_unit"] = "invalid_string"
        with self.assertRaises(ValueError):
            binding.validate_config(bad_type_cfg)

    # 11. Mandatory Regression A: Real WAL Unit 1 = PROPOSED, forged memory = UNIT_VERIFIED -> rejected
    def test_11_regression_a_forged_memory_unit1_rejected(self):
        worker, cfg, units, registry = self.prepared(multi_unit=True)
        # Verify genuine WAL state has Unit 1 as PROPOSED
        reloaded = durable.Store(self.store.directory.parent, self.store.directory.name)
        reloaded.load()
        self.assertEqual(reloaded.state["work_units"]["units"]["unit-1-multiply"]["status"], "PROPOSED")

        # Forged in-memory store.state claims Unit 1 is UNIT_VERIFIED without durable WAL commit
        self.store.state["work_units"]["units"]["unit-1-multiply"]["status"] = "UNIT_VERIFIED"

        # Production executor entrypoint MUST reject execution of dependent Unit 2
        with self.assertRaises(manager.ExecutorError) as ctx:
            self.execute_worker(worker, units[1], cfg, self.store)
        self.assertEqual(ctx.exception.reason, "CONTRACT_MISMATCH")
        self.assertIn("disagrees with durable WAL authority", str(ctx.exception))

    # 12. Mandatory Regression B: Forged SimpleNamespace with plausible registered plan -> rejected
    def test_12_regression_b_forged_simplenamespace_rejected(self):
        worker, cfg, units, registry = self.prepared(multi_unit=True)
        fake_store = SimpleNamespace(
            directory=self.store.directory,
            state=copy.deepcopy(self.store.state),
            records=copy.deepcopy(self.store.records),
        )
        with self.assertRaises(manager.ExecutorError) as ctx:
            self.execute_worker(worker, units[0], cfg, fake_store)
        self.assertEqual(ctx.exception.reason, "CONTRACT_MISMATCH")
        self.assertIn("requires genuine durable Store authority", str(ctx.exception))

    # 13. Mandatory Regression C: Genuine persisted Unit 1 VERIFIED -> Unit 2 permitted
    def test_13_regression_c_genuine_persisted_unit1_verified_permits_unit2(self):
        worker, cfg, units, registry = self.prepared(multi_unit=True)
        # Genuinely commit Unit 1 as UNIT_VERIFIED into durable Store/WAL
        self.mark_unit_verified(self.store, "unit-1-multiply")

        # Confirm reload has UNIT_VERIFIED
        reloaded = durable.Store(self.store.directory.parent, self.store.directory.name)
        reloaded.load()
        self.assertEqual(reloaded.state["work_units"]["units"]["unit-1-multiply"]["status"], "UNIT_VERIFIED")

        # Production executor MUST permit Unit 2
        receipt = self.execute_worker(worker, units[1], cfg, self.store)
        self.assertEqual(receipt["cleanup"], "passed")
        self.assertEqual(receipt["unit_id"], "unit-2-scale")

    # 14. Mandatory Regression D: Altered verifier or scope in incoming unit specification -> rejected
    def test_14_regression_d_altered_verifier_or_scope_rejected(self):
        worker, cfg, units, registry = self.prepared(multi_unit=True)

        # D1: Alter verifier IDs in incoming spec
        bad_verif_unit = copy.deepcopy(units[0])
        bad_verif_unit["verifier_ids"] = ["tampered-verifier"]
        with self.assertRaises(manager.ExecutorError) as ctx:
            self.execute_worker(worker, bad_verif_unit, cfg, self.store)
        self.assertEqual(ctx.exception.reason, "CONTRACT_MISMATCH")

        # D2: Alter scope in incoming spec (exceed allowed paths)
        bad_scope_unit = copy.deepcopy(units[0])
        bad_scope_unit["scope"]["allowed_paths"] = ["calculator.py", "unauthorized.py"]
        with self.assertRaises(manager.ExecutorError) as ctx:
            self.execute_worker(worker, bad_scope_unit, cfg, self.store)
        self.assertEqual(ctx.exception.reason, "CONTRACT_MISMATCH")

        # D3: Alter scope in incoming spec (unfreeze forbidden paths)
        unfrozen_unit = copy.deepcopy(units[0])
        unfrozen_unit["scope"]["forbidden_paths"] = []
        with self.assertRaises(manager.ExecutorError) as ctx:
            self.execute_worker(worker, unfrozen_unit, cfg, self.store)
        self.assertEqual(ctx.exception.reason, "CONTRACT_MISMATCH")

    # 15. Production executor receipt records unit_id and persists to Store artifact
    def test_15_production_receipt_includes_unit_id_and_persists(self):
        worker, cfg, units, registry = self.prepared(multi_unit=True)
        receipt = self.execute_worker(worker, units[0], cfg, self.store)
        self.assertEqual(receipt["unit_id"], "unit-1-multiply")
        self.assertEqual(receipt["cleanup"], "passed")
        # Check artifact written to Store
        run_artifacts = list(self.store.directory.glob("**/result.json"))
        self.assertTrue(len(run_artifacts) >= 1)
        persisted_receipt = json.loads(run_artifacts[0].read_text(encoding="utf-8"))
        self.assertEqual(persisted_receipt["unit_id"], "unit-1-multiply")
        self.assertEqual(persisted_receipt["cleanup"], "passed")


if __name__ == "__main__":
    unittest.main()
