"""Deterministic tests for Wave 2 WorkUnit architecture.

Covers all 19 required scenarios:
1. two sequential successful units
2. dependency blocks dependent unit until prerequisite verified
3. deterministic ordering of independent units
4. read-only unit succeeds without diff when verifier passes
5. read-only unit fails on unexpected write
6. mutation unit executes and verifies
7. executor success + verifier failure does not verify unit
8. first attempt fail + bounded repair success
9. repair failure exhausts unit
10. third attempt impossible
11. repair cannot expand scope
12. timeout consumes budget appropriately
13. later mutation causes conservative revalidation
14. stale unit evidence cannot produce milestone-ready
15. crash/reload preserves attempts and consumed budget
16. cleanup failure blocks continuation
17. WorkUnit completion does not mutate top-level trusted fields
18. all units verified means only milestone-ready/intermediate state
19. legacy non-WorkUnit Manager behavior remains compatible
"""

import copy
import subprocess
import time
import unittest
from pathlib import Path

import durable
import harness
import manager
import work_units
from work_units import (
    WorkUnitError,
    validate_unit_plan,
    validate_section,
    load_work_units,
)
from work_unit_scheduler import (
    WorkUnitScheduler,
    WorkUnitSchedulingError,
    ScriptedExecutor,
    is_milestone_ready,
    is_structurally_valid_snapshot,
    validate_sequence_integrity,
    MILESTONE_READY,
    WORK_UNITS_COMPLETE,
    MILESTONE_FAILED,
)
from test_harness import CONFIG


# --------------------------------------------------------------------------- helpers & fixture

TASK = "Wave 2 WorkUnit task"
COMMANDS = [["python", "-m", "unittest", "test_calculator.py"]]


def make_spec(
    unit_id="unit-1",
    objective="Implement helper",
    dependencies=None,
    mode="mutation",
    paths=None,
    forbidden=None,
    verifier_ids=None,
    limits=None,
):
    return {
        "unit_id": unit_id,
        "objective": objective,
        "dependencies": dependencies if dependencies is not None else [],
        "mode": mode,
        "scope": {
            "allowed_paths": paths if paths is not None else ["calculator.py"],
            "forbidden_paths": forbidden if forbidden is not None else [],
        },
        "verifier_ids": verifier_ids if verifier_ids is not None else ["check-calc"],
        "evidence_inputs": [],
        "limits": limits if limits is not None else {"max_attempts": 2, "timeout_seconds": 60},
    }


def fixture(root, run_id="wave2-test-run"):
    """Create a git repo, Store, and Repository for testing."""
    repo_path = root / "repo"
    if not repo_path.exists():
        repo_path.mkdir(parents=True)
        (repo_path / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
        (repo_path / "calculator.py").write_text("def multiply(a, b):\n    return 0\n", encoding="utf-8")
        (repo_path / "test_calculator.py").write_text(
            "import unittest\nfrom calculator import multiply\n"
            "class T(unittest.TestCase):\n"
            "    def test_product(self): self.assertEqual(multiply(2, 3), 6)\n"
            "if __name__ == '__main__': unittest.main()\n",
            encoding="utf-8",
        )
        (repo_path / "test_helper.py").write_text(
            "import unittest\nclass T(unittest.TestCase):\n"
            "    def test_h(self):\n"
            "        import helper\n"
            "        self.assertEqual(helper.get_value(), 42)\n"
            "if __name__ == '__main__': unittest.main()\n",
            encoding="utf-8",
        )
        (repo_path / "test_main.py").write_text(
            "import unittest\nclass T(unittest.TestCase):\n"
            "    def test_m(self):\n"
            "        import main\n"
            "        self.assertEqual(main.run(), 100)\n"
            "if __name__ == '__main__': unittest.main()\n",
            encoding="utf-8",
        )
        (repo_path / "test_readonly.py").write_text(
            "import unittest, os\nclass T(unittest.TestCase):\n"
            "    def test_ro(self):\n"
            "        self.assertTrue(os.path.exists('calculator.py'))\n"
            "if __name__ == '__main__': unittest.main()\n",
            encoding="utf-8",
        )

        def git(*args):
            return subprocess.run(
                ["git", "-C", str(repo_path), "-c", f"safe.directory={repo_path}",
                 "-c", "user.name=W2 test", "-c", "user.email=w2@localhost", *args],
                check=True, capture_output=True,
            )

        git("init", "-q")
        git("add", ".")
        git("commit", "-qm", "baseline")

    config = copy.deepcopy(CONFIG)
    config.update(max_retries=0, roles={**CONFIG["roles"], "critic": "llm-critic"})
    log = harness.EventLog(root / f"{run_id}-preflight.jsonl")
    repo = harness.Repository(repo_path, config, log)
    store = durable.Store(root / "runs", run_id)
    return repo, store, config


# Standard verifier registry used across tests
def build_verifier_registry():
    return {
        "check-calc": {"argv": ["python", "-m", "unittest", "test_calculator.py"]},
        "check-helper": {"argv": ["python", "-m", "unittest", "test_helper.py"]},
        "check-main": {"argv": ["python", "-m", "unittest", "test_main.py"]},
        "check-readonly": {"argv": ["python", "-m", "unittest", "test_readonly.py"]},
    }


# --------------------------------------------------------------------------- tests

class TestWave2Scenarios(unittest.TestCase):

    # 1. two sequential successful units
    def test_01_two_sequential_successful_units(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, store, config = fixture(root, "run-01")
            registry = build_verifier_registry()

            unit1 = make_spec(
                unit_id="unit-1",
                objective="Create helper",
                dependencies=[],
                paths=["helper.py"],
                verifier_ids=["check-helper"],
            )
            unit2 = make_spec(
                unit_id="unit-2",
                objective="Create main",
                dependencies=["unit-1"],
                paths=["main.py"],
                verifier_ids=["check-main"],
            )

            manager.create_run(
                store, repo, TASK, COMMANDS, config,
                criteria=["Implement multiply"],
                work_units=[unit1, unit2],
                verifier_registry=registry,
            )

            executor = ScriptedExecutor()
            executor.set_behavior("unit-1", {"writes": {"helper.py": "def get_value():\n    return 42\n"}})
            executor.set_behavior("unit-2", {"writes": {"main.py": "import helper\ndef run():\n    return helper.get_value() + 58\n"}})

            agents = manager.Agents(None, executor, None, None)
            res = manager.execute_work_units(store, agents=agents, verifier_registry=registry)

            self.assertEqual(res["status"], MILESTONE_READY)
            self.assertTrue(res["work_units_complete"])

            section = load_work_units(store.state)
            self.assertEqual(section["units"]["unit-1"]["status"], "UNIT_VERIFIED")
            self.assertEqual(section["units"]["unit-2"]["status"], "UNIT_VERIFIED")
            self.assertEqual(len(section["units"]["unit-1"]["attempts"]), 1)
            self.assertEqual(len(section["units"]["unit-2"]["attempts"]), 1)

    # 2. dependency blocks dependent unit until prerequisite verified
    def test_02_dependency_blocks_dependent_unit_until_prerequisite_verified(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, store, config = fixture(root, "run-02")
            registry = build_verifier_registry()

            unit1 = make_spec(unit_id="unit-1", paths=["helper.py"], verifier_ids=["check-helper"])
            unit2 = make_spec(unit_id="unit-2", dependencies=["unit-1"], paths=["main.py"], verifier_ids=["check-main"])

            manager.create_run(
                store, repo, TASK, COMMANDS, config,
                criteria=["Task"],
                work_units=[unit1, unit2],
                verifier_registry=registry,
            )

            scheduler = WorkUnitScheduler(store, repo, verifier_registry=registry)
            section = scheduler.get_section()

            # Before unit-1 is verified, unit-2 MUST NOT be READY
            ready = scheduler.evaluate_readiness(section)
            ready_ids = [u["unit_id"] for u in ready]
            self.assertIn("unit-1", ready_ids)
            self.assertNotIn("unit-2", ready_ids)
            self.assertEqual(section["units"]["unit-2"]["status"], "PENDING")

            # Try to transition unit-2 directly to READY: Wave 1 validate_section blocks it
            with self.assertRaises(WorkUnitError):
                work_units.transition_unit(section["units"]["unit-2"], "READY")
                validate_section(section)

    # 3. deterministic ordering of independent units
    def test_03_deterministic_ordering_of_independent_units(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, store, config = fixture(root, "run-03")
            registry = build_verifier_registry()

            unit_b = make_spec(unit_id="unit-b", paths=["b.py"], verifier_ids=["check-readonly"])
            unit_a = make_spec(unit_id="unit-a", paths=["a.py"], verifier_ids=["check-readonly"])

            # Insertion order is [unit-b, unit-a]
            manager.create_run(
                store, repo, TASK, COMMANDS, config,
                criteria=["Task"],
                work_units=[unit_b, unit_a],
                verifier_registry=registry,
            )

            scheduler = WorkUnitScheduler(store, repo, verifier_registry=registry)
            section = scheduler.get_section()

            ready = scheduler.evaluate_readiness(section)
            # Must stably match plan insertion order [unit-b, unit-a]
            self.assertEqual([u["unit_id"] for u in ready], ["unit-b", "unit-a"])

            # Verify idempotence / no randomness across multiple calls
            for _ in range(5):
                ready_again = scheduler.evaluate_readiness(section)
                self.assertEqual([u["unit_id"] for u in ready_again], ["unit-b", "unit-a"])

    # 4. read-only unit succeeds without diff when verifier passes
    def test_04_read_only_unit_succeeds_without_diff_when_verifier_passes(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, store, config = fixture(root, "run-04")
            registry = build_verifier_registry()

            unit_ro = make_spec(
                unit_id="unit-ro",
                mode="read_only",
                paths=["calculator.py"],
                verifier_ids=["check-readonly"],
            )

            manager.create_run(
                store, repo, TASK, COMMANDS, config,
                criteria=["Task"],
                work_units=[unit_ro],
                verifier_registry=registry,
            )

            executor = ScriptedExecutor(default_behavior="success_read_only")
            agents = manager.Agents(None, executor, None, None)
            res = manager.execute_work_units(store, agents=agents, verifier_registry=registry)

            self.assertEqual(res["status"], MILESTONE_READY)
            section = load_work_units(store.state)
            unit_state = section["units"]["unit-ro"]
            self.assertEqual(unit_state["status"], "UNIT_VERIFIED")
            self.assertEqual(unit_state["attempts"][0]["changed_paths"], [])

    # 5. read-only unit fails on unexpected write
    def test_05_read_only_unit_fails_on_unexpected_write(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, store, config = fixture(root, "run-05")
            registry = build_verifier_registry()

            unit_ro = make_spec(
                unit_id="unit-ro",
                mode="read_only",
                paths=["calculator.py"],
                verifier_ids=["check-readonly"],
            )

            manager.create_run(
                store, repo, TASK, COMMANDS, config,
                criteria=["Task"],
                work_units=[unit_ro],
                verifier_registry=registry,
            )

            executor = ScriptedExecutor(default_behavior="unexpected_write")
            agents = manager.Agents(None, executor, None, None)
            res = manager.execute_work_units(store, agents=agents, verifier_registry=registry)

            self.assertEqual(res["status"], MILESTONE_FAILED)
            section = load_work_units(store.state)
            unit_state = section["units"]["unit-ro"]
            self.assertEqual(unit_state["status"], "UNIT_FAILED")
            self.assertEqual(unit_state["attempts"][0]["failure_classification"], "unexpected_write")

    # 6. mutation unit executes and verifies
    def test_06_mutation_unit_executes_and_verifies(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, store, config = fixture(root, "run-06")
            registry = build_verifier_registry()

            unit = make_spec(
                unit_id="unit-mut",
                mode="mutation",
                paths=["calculator.py"],
                verifier_ids=["check-calc"],
            )

            manager.create_run(
                store, repo, TASK, COMMANDS, config,
                criteria=["Fix multiply"],
                work_units=[unit],
                verifier_registry=registry,
            )

            executor = ScriptedExecutor()
            executor.set_behavior("unit-mut", {
                "writes": {"calculator.py": "def multiply(a, b):\n    return a * b\n"}
            })

            agents = manager.Agents(None, executor, None, None)
            res = manager.execute_work_units(store, agents=agents, verifier_registry=registry)

            self.assertEqual(res["status"], MILESTONE_READY)
            section = load_work_units(store.state)
            self.assertEqual(section["units"]["unit-mut"]["status"], "UNIT_VERIFIED")
            self.assertIn("calculator.py", section["units"]["unit-mut"]["attempts"][0]["changed_paths"])

    # 7. executor success + verifier failure does not verify unit
    def test_07_executor_success_verifier_failure_does_not_verify_unit(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, store, config = fixture(root, "run-07")
            registry = build_verifier_registry()

            unit = make_spec(
                unit_id="unit-vfail",
                mode="mutation",
                paths=["calculator.py"],
                verifier_ids=["check-calc"],
            )

            manager.create_run(
                store, repo, TASK, COMMANDS, config,
                criteria=["Fix multiply"],
                work_units=[unit],
                verifier_registry=registry,
            )

            # Executor writes bad implementation (multiply returns 99) -> verifier fails
            executor = ScriptedExecutor()
            executor.set_behavior("unit-vfail", {
                "writes": {"calculator.py": "def multiply(a, b):\n    return 99\n"}
            })

            agents = manager.Agents(None, executor, None, None)
            res = manager.execute_work_units(store, agents=agents, verifier_registry=registry)

            self.assertEqual(res["status"], MILESTONE_FAILED)
            section = load_work_units(store.state)
            unit_state = section["units"]["unit-vfail"]
            self.assertNotEqual(unit_state["status"], "UNIT_VERIFIED")
            self.assertEqual(unit_state["status"], "UNIT_FAILED")
            self.assertIsNone(unit_state["result"])

    # 8. first attempt fail + bounded repair success
    def test_08_first_attempt_fail_bounded_repair_success(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, store, config = fixture(root, "run-08")
            registry = build_verifier_registry()

            unit = make_spec(
                unit_id="unit-repair",
                mode="mutation",
                paths=["calculator.py"],
                verifier_ids=["check-calc"],
            )

            manager.create_run(
                store, repo, TASK, COMMANDS, config,
                criteria=["Fix multiply"],
                work_units=[unit],
                verifier_registry=registry,
            )

            executor = ScriptedExecutor()
            # Attempt 1 fails (wrong calculation)
            executor.set_behavior("unit-repair", {
                "writes": {"calculator.py": "def multiply(a, b):\n    return 99\n"}
            }, attempt=1)
            # Attempt 2 succeeds (repair)
            executor.set_behavior("unit-repair", {
                "writes": {"calculator.py": "def multiply(a, b):\n    return a * b\n"}
            }, attempt=2)

            agents = manager.Agents(None, executor, None, None)
            res = manager.execute_work_units(store, agents=agents, verifier_registry=registry)

            self.assertEqual(res["status"], MILESTONE_READY)
            section = load_work_units(store.state)
            unit_state = section["units"]["unit-repair"]
            self.assertEqual(unit_state["status"], "UNIT_VERIFIED")
            self.assertEqual(len(unit_state["attempts"]), 2)
            self.assertEqual(unit_state["attempts"][0]["outcome"], "failed")
            self.assertEqual(unit_state["attempts"][1]["outcome"], "passed")

            # Verify that Attempt 2 received failure evidence from Attempt 1
            calls = executor.get_attempts("unit-repair")
            self.assertEqual(len(calls), 2)
            self.assertIsNotNone(calls[1]["context"].get("repair_evidence"))
            self.assertEqual(calls[1]["context"]["repair_evidence"]["attempt"], 1)

    # 9. repair failure exhausts unit
    def test_09_repair_failure_exhausts_unit(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, store, config = fixture(root, "run-09")
            registry = build_verifier_registry()

            unit = make_spec(
                unit_id="unit-exhaust",
                mode="mutation",
                paths=["calculator.py"],
                verifier_ids=["check-calc"],
            )

            manager.create_run(
                store, repo, TASK, COMMANDS, config,
                criteria=["Fix multiply"],
                work_units=[unit],
                verifier_registry=registry,
            )

            executor = ScriptedExecutor()
            # Attempt 1 fails
            executor.set_behavior("unit-exhaust", {
                "writes": {"calculator.py": "def multiply(a, b):\n    return 1\n"}
            }, attempt=1)
            # Attempt 2 also fails
            executor.set_behavior("unit-exhaust", {
                "writes": {"calculator.py": "def multiply(a, b):\n    return 2\n"}
            }, attempt=2)

            agents = manager.Agents(None, executor, None, None)
            res = manager.execute_work_units(store, agents=agents, verifier_registry=registry)

            self.assertEqual(res["status"], MILESTONE_FAILED)
            section = load_work_units(store.state)
            unit_state = section["units"]["unit-exhaust"]
            self.assertEqual(unit_state["status"], "UNIT_FAILED")
            self.assertEqual(len(unit_state["attempts"]), 2)
            self.assertEqual(unit_state["attempts"][0]["outcome"], "failed")
            self.assertEqual(unit_state["attempts"][1]["outcome"], "failed")

    # 10. third attempt impossible
    def test_10_third_attempt_impossible(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, store, config = fixture(root, "run-10")
            registry = build_verifier_registry()

            unit = make_spec(unit_id="unit-no3", paths=["calculator.py"], limits={"max_attempts": 2})

            manager.create_run(
                store, repo, TASK, COMMANDS, config,
                criteria=["Fix multiply"],
                work_units=[unit],
                verifier_registry=registry,
            )

            scheduler = WorkUnitScheduler(store, repo, verifier_registry=registry)
            section = scheduler.get_section()
            unit_state = section["units"]["unit-no3"]

            work_units.transition_unit(unit_state, "VALIDATED")
            work_units.transition_unit(unit_state, "READY")
            work_units.transition_unit(unit_state, "EXECUTING")
            work_units.create_attempt(unit_state, "att-1")
            work_units.complete_attempt(unit_state, "att-1", outcome="failed")

            work_units.transition_unit(unit_state, "REPAIR_PENDING")
            work_units.transition_unit(unit_state, "READY")
            work_units.transition_unit(unit_state, "EXECUTING")
            work_units.create_attempt(unit_state, "att-2")
            work_units.complete_attempt(unit_state, "att-2", outcome="failed")

            # Attempt 3 cannot enter REPAIR_PENDING or create_attempt
            with self.assertRaises(WorkUnitError):
                work_units.transition_unit(unit_state, "REPAIR_PENDING")

            with self.assertRaises(WorkUnitError):
                work_units.create_attempt(unit_state, "att-3")

            # Must transition to UNIT_FAILED
            work_units.transition_unit(unit_state, "UNIT_FAILED")
            self.assertEqual(unit_state["status"], "UNIT_FAILED")

    # 11. repair cannot expand scope
    def test_11_repair_cannot_expand_scope(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, store, config = fixture(root, "run-11")
            registry = build_verifier_registry()

            unit = make_spec(
                unit_id="unit-scope",
                mode="mutation",
                paths=["calculator.py"],
                forbidden=["secret/"],
                verifier_ids=["check-calc"],
            )

            manager.create_run(
                store, repo, TASK, COMMANDS, config,
                criteria=["Fix multiply"],
                work_units=[unit],
                verifier_registry=registry,
            )

            executor = ScriptedExecutor()
            # Attempt 1: normal failure
            executor.set_behavior("unit-scope", "failure", attempt=1)
            # Attempt 2 (repair): tries to write outside allowed scope
            executor.set_behavior("unit-scope", "scope_violation", attempt=2)

            agents = manager.Agents(None, executor, None, None)
            res = manager.execute_work_units(store, agents=agents, verifier_registry=registry)

            self.assertEqual(res["status"], MILESTONE_FAILED)
            section = load_work_units(store.state)
            unit_state = section["units"]["unit-scope"]
            self.assertEqual(unit_state["status"], "UNIT_FAILED")
            self.assertEqual(unit_state["attempts"][1]["failure_classification"], "scope_violation")

    # 12. timeout consumes budget appropriately
    def test_12_timeout_consumes_budget_appropriately(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, store, config = fixture(root, "run-12")
            registry = build_verifier_registry()

            unit = make_spec(
                unit_id="unit-timeout",
                mode="mutation",
                paths=["calculator.py"],
                verifier_ids=["check-calc"],
                limits={"max_attempts": 2, "timeout_seconds": 2},
            )

            manager.create_run(
                store, repo, TASK, COMMANDS, config,
                criteria=["Task"],
                budgets={"max_runtime_seconds": 10},
                work_units=[unit],
                verifier_registry=registry,
            )

            executor = ScriptedExecutor()
            # Attempt 1 times out
            executor.set_behavior("unit-timeout", "timeout", attempt=1)
            # Attempt 2 also times out
            executor.set_behavior("unit-timeout", "timeout", attempt=2)

            simulated_clock = [100.0]

            def fake_clock():
                t = simulated_clock[0]
                simulated_clock[0] += 3.0  # advance 3s each call
                return t

            agents = manager.Agents(None, executor, None, None)
            res = manager.execute_work_units(store, agents=agents, clock=fake_clock, verifier_registry=registry)

            self.assertIn(res["status"], (MILESTONE_FAILED, "BUDGET_EXHAUSTED"))
            section = load_work_units(store.state)
            unit_state = section["units"]["unit-timeout"]
            self.assertEqual(unit_state["attempts"][0]["outcome"], "timeout")

    # 13. later mutation causes conservative revalidation
    def test_13_later_mutation_causes_conservative_revalidation(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, store, config = fixture(root, "run-13")
            registry = build_verifier_registry()

            unit1 = make_spec(unit_id="unit-1", paths=["helper.py"], verifier_ids=["check-helper"])
            unit2 = make_spec(unit_id="unit-2", dependencies=["unit-1"], paths=["main.py"], verifier_ids=["check-main"])

            manager.create_run(
                store, repo, TASK, COMMANDS, config,
                criteria=["Task"],
                work_units=[unit1, unit2],
                verifier_registry=registry,
            )

            executor = ScriptedExecutor()
            executor.set_behavior("unit-1", {"writes": {"helper.py": "def get_value(): return 42\n"}})
            executor.set_behavior("unit-2", {"writes": {"main.py": "import helper\ndef run(): return helper.get_value() + 58\n"}})

            agents = manager.Agents(None, executor, None, None)
            scheduler = WorkUnitScheduler(store, repo, verifier_registry=registry, agents=agents)
            res = scheduler.run_sequence()

            self.assertEqual(res["status"], MILESTONE_READY)

            # Manually invoke revalidation to confirm it checks all units against final state
            section = scheduler.get_section()
            passed, err = scheduler.revalidate_milestone(section)
            self.assertTrue(passed)
            self.assertIsNone(err)

    # 14. stale unit evidence cannot produce milestone-ready
    def test_14_stale_unit_evidence_cannot_produce_milestone_ready(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, store, config = fixture(root, "run-14")
            registry = build_verifier_registry()

            unit1 = make_spec(unit_id="unit-1", paths=["helper.py"], verifier_ids=["check-helper"])
            # unit-2 also allowed on helper.py, but corrupts it while passing its own check
            unit2 = make_spec(unit_id="unit-2", dependencies=["unit-1"], paths=["main.py", "helper.py"], verifier_ids=["check-main"])

            manager.create_run(
                store, repo, TASK, COMMANDS, config,
                criteria=["Task"],
                work_units=[unit1, unit2],
                verifier_registry=registry,
            )

            executor = ScriptedExecutor()
            # unit-1 creates valid helper.py
            executor.set_behavior("unit-1", {"writes": {"helper.py": "def get_value(): return 42\n"}})
            # unit-2 creates main.py that passes check-main, BUT breaks helper.py (returns 0 instead of 42)
            executor.set_behavior("unit-2", {
                "writes": {
                    "helper.py": "def get_value(): return 0\n",
                    "main.py": "def run(): return 100\n",
                }
            })

            agents = manager.Agents(None, executor, None, None)
            scheduler = WorkUnitScheduler(store, repo, verifier_registry=registry, agents=agents)
            res = scheduler.run_sequence()

            # Conservative revalidation MUST reject milestone-ready because helper.py was broken!
            self.assertEqual(res["status"], MILESTONE_FAILED)
            self.assertEqual(res["reason"], "STALE_VERIFICATION")
            self.assertIn("unit-1", res["error"])

    # 15. crash/reload preserves attempts and consumed budget
    def test_15_crash_reload_preserves_attempts_and_consumed_budget(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, store, config = fixture(root, "run-15")
            registry = build_verifier_registry()

            unit = make_spec(
                unit_id="unit-crash",
                mode="mutation",
                paths=["calculator.py"],
                verifier_ids=["check-calc"],
            )

            manager.create_run(
                store, repo, TASK, COMMANDS, config,
                criteria=["Fix multiply"],
                work_units=[unit],
                verifier_registry=registry,
            )

            executor = ScriptedExecutor()
            executor.set_behavior("unit-crash", "failure", attempt=1)
            executor.set_behavior("unit-crash", {
                "writes": {"calculator.py": "def multiply(a, b):\n    return a * b\n"}
            }, attempt=2)

            scheduler = WorkUnitScheduler(store, repo, verifier_registry=registry, agents=manager.Agents(None, executor, None, None))
            section = scheduler.get_section()
            ready = scheduler.evaluate_readiness(section)
            # Execute Attempt 1 (which fails)
            att1_res = scheduler.execute_attempt(ready[0], section, attempt_num=1)
            self.assertFalse(att1_res["success"])

            # Simulate CRASH: close store, open fresh Store instance from disk and load
            del scheduler
            del store

            new_store = durable.Store(root / "runs", "run-15")
            new_store.load()

            reloaded_section = load_work_units(new_store.state)
            self.assertIsNotNone(reloaded_section)
            reloaded_unit = reloaded_section["units"]["unit-crash"]

            # Verify Attempt 1 was preserved
            self.assertEqual(len(reloaded_unit["attempts"]), 1)
            self.assertEqual(reloaded_unit["attempts"][0]["outcome"], "failed")
            self.assertEqual(reloaded_unit["attempts"][0]["attempt_id"], "unit-crash-attempt-1")

            # Now continue with fresh scheduler; it must execute Attempt 2 (repair)
            new_scheduler = WorkUnitScheduler(
                new_store, repo, verifier_registry=registry,
                agents=manager.Agents(None, executor, None, None)
            )
            res = new_scheduler.run_sequence()

            self.assertEqual(res["status"], MILESTONE_READY)
            final_section = load_work_units(new_store.state)
            final_unit = final_section["units"]["unit-crash"]
            self.assertEqual(final_unit["status"], "UNIT_VERIFIED")
            self.assertEqual(len(final_unit["attempts"]), 2)

    # 16. cleanup failure blocks continuation
    def test_16_cleanup_failure_blocks_continuation(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, store, config = fixture(root, "run-16")
            registry = build_verifier_registry()

            unit = make_spec(unit_id="unit-clean", paths=["calculator.py"])

            manager.create_run(
                store, repo, TASK, COMMANDS, config,
                criteria=["Task"],
                work_units=[unit],
                verifier_registry=registry,
            )

            # Simulate supervisor reporting an active running helper child
            class FakeSupervisor:
                def reconcile(self):
                    return [{"child_id": "helper-1234", "state": "RUNNING", "purpose": "helper", "reason": "active"}]

            scheduler = WorkUnitScheduler(
                store, repo, verifier_registry=registry, supervisor=FakeSupervisor()
            )

            with self.assertRaises(manager.Stop) as ctx:
                scheduler.guard_supervision()

            self.assertEqual(ctx.exception.status, "HUMAN_ACTION_REQUIRED")
            self.assertEqual(ctx.exception.reason, "CHILD_STILL_RUNNING")

    # 17. WorkUnit completion does not mutate top-level trusted fields
    def test_17_work_unit_completion_does_not_mutate_top_level_trusted_fields(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, store, config = fixture(root, "run-17")
            registry = build_verifier_registry()

            unit = make_spec(
                unit_id="unit-trust",
                mode="mutation",
                paths=["calculator.py"],
                verifier_ids=["check-calc"],
            )

            manager.create_run(
                store, repo, TASK, COMMANDS, config,
                criteria=["Fix multiply"],
                work_units=[unit],
                verifier_registry=registry,
            )

            # Record trusted fields before WorkUnit execution
            status_before = store.state["status"]
            checkpoint_before = store.state["last_verified_checkpoint"]
            progress_before = copy.deepcopy(store.state["verified_progress"])

            executor = ScriptedExecutor()
            executor.set_behavior("unit-trust", {
                "writes": {"calculator.py": "def multiply(a, b):\n    return a * b\n"}
            })

            agents = manager.Agents(None, executor, None, None)
            res = manager.execute_work_units(store, agents=agents, verifier_registry=registry)
            self.assertEqual(res["status"], MILESTONE_READY)

            # Assert trusted fields are UNTOUCHED
            self.assertNotEqual(store.state["status"], "VERIFIED")
            self.assertEqual(store.state["last_verified_checkpoint"], checkpoint_before)
            self.assertIsNone(store.state["last_verified_checkpoint"])
            self.assertEqual(store.state["verified_progress"], progress_before)
            self.assertEqual(store.state["verified_progress"], [])

    # 18. all units verified means only milestone-ready/intermediate state
    def test_18_all_units_verified_means_only_milestone_ready_intermediate_state(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, store, config = fixture(root, "run-18")
            registry = build_verifier_registry()

            unit1 = make_spec(unit_id="unit-1", paths=["helper.py"], verifier_ids=["check-helper"])
            unit2 = make_spec(unit_id="unit-2", dependencies=["unit-1"], paths=["main.py"], verifier_ids=["check-main"])

            manager.create_run(
                store, repo, TASK, COMMANDS, config,
                criteria=["Task"],
                work_units=[unit1, unit2],
                verifier_registry=registry,
            )

            executor = ScriptedExecutor()
            executor.set_behavior("unit-1", {"writes": {"helper.py": "def get_value(): return 42\n"}})
            executor.set_behavior("unit-2", {"writes": {"main.py": "import helper\ndef run(): return helper.get_value() + 58\n"}})

            agents = manager.Agents(None, executor, None, None)
            res = manager.execute_work_units(store, agents=agents, verifier_registry=registry)

            # Must return MILESTONE_READY, not a trusted checkpoint
            self.assertEqual(res["status"], MILESTONE_READY)
            self.assertTrue(res["work_units_complete"])

            section = load_work_units(store.state)
            # All individual units have trust_level INTERMEDIATE
            for uid, u in section["units"].items():
                self.assertEqual(u["status"], "UNIT_VERIFIED")
                self.assertEqual(u["result"]["trust_level"], "INTERMEDIATE")

            # Top-level status is not VERIFIED or COMPLETED
            self.assertNotIn(store.state["status"], ("VERIFIED", "COMPLETED"))

    # 19. legacy non-WorkUnit Manager behavior remains compatible
    def test_19_legacy_non_work_unit_manager_behavior_compatible(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, store, config = fixture(root, "run-19")

            # Legacy create_run without work_units
            manager.create_run(
                store, repo, TASK, COMMANDS, config,
                criteria=["Implement multiply"],
            )

            # Legacy runs have no work_units key
            self.assertNotIn("work_units", store.state)
            self.assertIsNone(load_work_units(store.state))
            self.assertEqual(store.state["status"], "CREATED")
            self.assertEqual(store.state["manager"]["phase"], "READY")


class FakeClock:
    def __init__(self, start=0.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class MockSupervisor:
    def __init__(self, fail_at_call=None):
        self.call_count = 0
        self.fail_at_call = fail_at_call

    def reconcile(self):
        self.call_count += 1
        if self.fail_at_call is not None and self.call_count >= self.fail_at_call:
            return [{"child_id": "child-mock", "state": "UNKNOWN", "reason": "mock_failure", "purpose": "tool"}]
        return []

    def close(self):
        pass


class TestWave2AuditHardening(unittest.TestCase):
    """Targeted regression tests for all 11 Codex audit defects."""

    # ------------------------------------------------------------------------- Fix 1: Contract Immutability
    def test_audit_fix01_executor_cannot_mutate_allowed_paths(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, store, config = fixture(root, "audit-fix01-allowed")
            registry = build_verifier_registry()
            unit = make_spec(unit_id="unit-1", paths=["calculator.py"], verifier_ids=["check-calc"])
            manager.create_run(store, repo, TASK, COMMANDS, config, criteria=["Fix"], work_units=[unit], verifier_registry=registry)

            executor = ScriptedExecutor(default_behavior="mutate_allowed_paths")
            agents = manager.Agents(None, executor, None, None)
            res = manager.execute_work_units(store, agents=agents, verifier_registry=registry)
            self.assertEqual(res["status"], MILESTONE_FAILED)
            self.assertEqual(res["reason"], "contract_divergence")
            section = load_work_units(store.state)
            self.assertEqual(section["units"]["unit-1"]["spec"]["scope"]["allowed_paths"], ["calculator.py"])

    def test_audit_fix01_executor_cannot_mutate_forbidden_paths(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, store, config = fixture(root, "audit-fix01-forbidden")
            registry = build_verifier_registry()
            unit = make_spec(unit_id="unit-1", paths=["calculator.py"], forbidden=["secrets/"], verifier_ids=["check-calc"])
            manager.create_run(store, repo, TASK, COMMANDS, config, criteria=["Fix"], work_units=[unit], verifier_registry=registry)

            executor = ScriptedExecutor(default_behavior="mutate_forbidden_paths")
            agents = manager.Agents(None, executor, None, None)
            res = manager.execute_work_units(store, agents=agents, verifier_registry=registry)
            self.assertEqual(res["status"], MILESTONE_FAILED)
            self.assertEqual(res["reason"], "contract_divergence")
            section = load_work_units(store.state)
            self.assertEqual(section["units"]["unit-1"]["spec"]["scope"]["forbidden_paths"], ["secrets/"])

    def test_audit_fix01_executor_cannot_mutate_objective(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, store, config = fixture(root, "audit-fix01-obj")
            registry = build_verifier_registry()
            unit = make_spec(unit_id="unit-1", objective="Original objective", paths=["calculator.py"], verifier_ids=["check-calc"])
            manager.create_run(store, repo, TASK, COMMANDS, config, criteria=["Fix"], work_units=[unit], verifier_registry=registry)

            executor = ScriptedExecutor(default_behavior="mutate_objective")
            agents = manager.Agents(None, executor, None, None)
            res = manager.execute_work_units(store, agents=agents, verifier_registry=registry)
            self.assertEqual(res["status"], MILESTONE_FAILED)
            self.assertEqual(res["reason"], "contract_divergence")

    def test_audit_fix01_executor_cannot_mutate_dependencies(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, store, config = fixture(root, "audit-fix01-deps")
            registry = build_verifier_registry()
            unit1 = make_spec(unit_id="unit-1", paths=["helper.py"], verifier_ids=["check-helper"])
            unit2 = make_spec(unit_id="unit-2", dependencies=["unit-1"], paths=["calculator.py"], verifier_ids=["check-calc"])
            manager.create_run(store, repo, TASK, COMMANDS, config, criteria=["Fix"], work_units=[unit1, unit2], verifier_registry=registry)

            executor = ScriptedExecutor()
            executor.set_behavior("unit-1", {"writes": {"helper.py": "def get_value(): return 42\n"}})
            executor.set_behavior("unit-2", "mutate_dependencies")
            agents = manager.Agents(None, executor, None, None)
            res = manager.execute_work_units(store, agents=agents, verifier_registry=registry)
            self.assertEqual(res["status"], MILESTONE_FAILED)
            self.assertEqual(res["reason"], "contract_divergence")

    def test_audit_fix01_executor_cannot_mutate_verifier_ids(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, store, config = fixture(root, "audit-fix01-vids")
            registry = build_verifier_registry()
            unit = make_spec(unit_id="unit-1", paths=["calculator.py"], verifier_ids=["check-calc"])
            manager.create_run(store, repo, TASK, COMMANDS, config, criteria=["Fix"], work_units=[unit], verifier_registry=registry)

            executor = ScriptedExecutor(default_behavior="mutate_verifier_ids")
            agents = manager.Agents(None, executor, None, None)
            res = manager.execute_work_units(store, agents=agents, verifier_registry=registry)
            self.assertEqual(res["status"], MILESTONE_FAILED)
            self.assertEqual(res["reason"], "contract_divergence")

    def test_audit_fix01_executor_cannot_mutate_limits(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, store, config = fixture(root, "audit-fix01-limits")
            registry = build_verifier_registry()
            unit = make_spec(unit_id="unit-1", paths=["calculator.py"], verifier_ids=["check-calc"])
            manager.create_run(store, repo, TASK, COMMANDS, config, criteria=["Fix"], work_units=[unit], verifier_registry=registry)

            executor = ScriptedExecutor(default_behavior="mutate_limits")
            agents = manager.Agents(None, executor, None, None)
            res = manager.execute_work_units(store, agents=agents, verifier_registry=registry)
            self.assertEqual(res["status"], MILESTONE_FAILED)
            self.assertEqual(res["reason"], "contract_divergence")

    # ------------------------------------------------------------------------- Fix 2: Unconditional Scope Inspection
    def test_audit_fix02_illegal_write_and_exception_fails_closed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, store, config = fixture(root, "audit-fix02-write-exc")
            registry = build_verifier_registry()
            unit = make_spec(unit_id="unit-1", paths=["calculator.py"], verifier_ids=["check-calc"])
            manager.create_run(store, repo, TASK, COMMANDS, config, criteria=["Fix"], work_units=[unit], verifier_registry=registry)

            executor = ScriptedExecutor()
            executor.set_behavior("unit-1", "write_and_raise", attempt=1)
            executor.set_behavior("unit-1", "success_mutation", attempt=2)  # Should NEVER be called!
            agents = manager.Agents(None, executor, None, None)
            res = manager.execute_work_units(store, agents=agents, verifier_registry=registry)

            self.assertEqual(res["status"], MILESTONE_FAILED)
            self.assertEqual(res["reason"], "scope_violation")
            section = load_work_units(store.state)
            self.assertEqual(section["units"]["unit-1"]["status"], "UNIT_FAILED")
            self.assertEqual(len(section["units"]["unit-1"]["attempts"]), 1)  # No repair allowed!

    def test_audit_fix02_read_only_write_and_exception_fails_closed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, store, config = fixture(root, "audit-fix02-ro-exc")
            registry = build_verifier_registry()
            unit = make_spec(unit_id="unit-1", mode="read_only", paths=["calculator.py"], verifier_ids=["check-readonly"])
            manager.create_run(store, repo, TASK, COMMANDS, config, criteria=["Fix"], work_units=[unit], verifier_registry=registry)

            executor = ScriptedExecutor()
            executor.set_behavior("unit-1", "read_only_write_and_raise", attempt=1)
            agents = manager.Agents(None, executor, None, None)
            res = manager.execute_work_units(store, agents=agents, verifier_registry=registry)

            self.assertEqual(res["status"], MILESTONE_FAILED)
            self.assertEqual(res["reason"], "unexpected_write")
            section = load_work_units(store.state)
            self.assertEqual(section["units"]["unit-1"]["status"], "UNIT_FAILED")
            self.assertEqual(len(section["units"]["unit-1"]["attempts"]), 1)

    def test_audit_fix02_timeout_after_illegal_write_fails_closed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, store, config = fixture(root, "audit-fix02-timeout-write")
            registry = build_verifier_registry()
            unit = make_spec(unit_id="unit-1", paths=["calculator.py"], verifier_ids=["check-calc"])
            manager.create_run(store, repo, TASK, COMMANDS, config, criteria=["Fix"], work_units=[unit], verifier_registry=registry)

            executor = ScriptedExecutor()
            executor.set_behavior("unit-1", "timeout_after_write", attempt=1)
            agents = manager.Agents(None, executor, None, None)
            res = manager.execute_work_units(store, agents=agents, verifier_registry=registry)

            self.assertEqual(res["status"], MILESTONE_FAILED)
            self.assertEqual(res["reason"], "scope_violation")
            section = load_work_units(store.state)
            self.assertEqual(section["units"]["unit-1"]["status"], "UNIT_FAILED")
            self.assertEqual(len(section["units"]["unit-1"]["attempts"]), 1)

    def test_audit_fix02_repair_cannot_legitimize_previous_illegal_mutation(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, store, config = fixture(root, "audit-fix02-no-legitimize")
            registry = build_verifier_registry()
            unit = make_spec(unit_id="unit-1", paths=["calculator.py"], verifier_ids=["check-calc"])
            manager.create_run(store, repo, TASK, COMMANDS, config, criteria=["Fix"], work_units=[unit], verifier_registry=registry)

            executor = ScriptedExecutor()
            # Attempt 1 writes outside scope
            executor.set_behavior("unit-1", "scope_violation", attempt=1)
            # Attempt 2 writes valid calculator.py
            executor.set_behavior("unit-1", {"writes": {"calculator.py": "def multiply(a, b): return a * b\n"}}, attempt=2)
            agents = manager.Agents(None, executor, None, None)
            res = manager.execute_work_units(store, agents=agents, verifier_registry=registry)

            # Repair is refused; unit is marked UNIT_FAILED immediately on attempt 1
            self.assertEqual(res["status"], MILESTONE_FAILED)
            self.assertEqual(len(executor.get_attempts("unit-1")), 1)
            section = load_work_units(store.state)
            self.assertEqual(section["units"]["unit-1"]["status"], "UNIT_FAILED")

    # ------------------------------------------------------------------------- Fix 3: Shared Budgets & Deadlines
    def test_audit_fix03_task_budget_exhaustion_before_verification(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, store, config = fixture(root, "audit-fix03-task-budget")
            registry = build_verifier_registry()
            unit = make_spec(unit_id="unit-1", paths=["calculator.py"], verifier_ids=["check-calc"])
            manager.create_run(store, repo, TASK, COMMANDS, config, criteria=["Fix"], work_units=[unit],
                               budgets={"max_runtime_seconds": 10}, verifier_registry=registry)

            clock = FakeClock()
            def slow_exec(r, u, c):
                r.write("calculator.py", "def multiply(a, b): return a * b\n")
                clock.advance(11.0)
                return "Slow execution"

            executor = ScriptedExecutor()
            executor.set_behavior("unit-1", slow_exec)
            agents = manager.Agents(None, executor, None, None)
            res = manager.execute_work_units(store, agents=agents, clock=clock, verifier_registry=registry)

            self.assertEqual(res["status"], "BUDGET_EXHAUSTED")
            section = load_work_units(store.state)
            self.assertNotEqual(section["units"]["unit-1"]["status"], "UNIT_VERIFIED")

    def test_audit_fix03_final_revalidation_budget_exhaustion(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, store, config = fixture(root, "audit-fix03-reval-budget")
            registry = build_verifier_registry()
            unit = make_spec(unit_id="unit-1", paths=["calculator.py"], verifier_ids=["check-calc"])
            manager.create_run(store, repo, TASK, COMMANDS, config, criteria=["Fix"], work_units=[unit],
                               budgets={"max_runtime_seconds": 10}, verifier_registry=registry)

            clock = FakeClock()
            def step_exec(r, u, c):
                r.write("calculator.py", "def multiply(a, b): return a * b\n")
                clock.advance(8.0)
                return "Done"

            executor = ScriptedExecutor()
            executor.set_behavior("unit-1", step_exec)
            agents = manager.Agents(None, executor, None, None)

            scheduler = WorkUnitScheduler(store, repo, verifier_registry=registry, clock=clock, agents=agents,
                                          budgets={"max_runtime_seconds": 10})
            sec = scheduler.get_section()
            scheduler.evaluate_readiness(sec)
            res_unit = scheduler.execute_unit(sec["units"]["unit-1"], sec)
            self.assertTrue(res_unit["success"])
            clock.advance(3.0)  # Total 11.0s > 10.0s

            # run_sequence checks budget during conservative revalidation
            res = scheduler.run_sequence()
            self.assertEqual(res["status"], "BUDGET_EXHAUSTED")

    def test_audit_fix03_multi_attempt_unit_budget_exhaustion(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, store, config = fixture(root, "audit-fix03-unit-budget")
            registry = build_verifier_registry()
            unit = make_spec(unit_id="unit-1", paths=["calculator.py"], verifier_ids=["check-calc"],
                             limits={"max_attempts": 2, "timeout_seconds": 10})
            manager.create_run(store, repo, TASK, COMMANDS, config, criteria=["Fix"], work_units=[unit],
                               budgets={"max_runtime_seconds": 100}, verifier_registry=registry)

            clock = FakeClock()
            def attempt1(r, u, c):
                clock.advance(6.0)
                from manager import ExecutorError
                raise ExecutorError("fail 1", "EXECUTOR_ERROR")

            def attempt2(r, u, c):
                clock.advance(6.0)
                r.write("calculator.py", "def multiply(a, b): return a * b\n")
                return "Done"

            executor = ScriptedExecutor()
            executor.set_behavior("unit-1", attempt1, attempt=1)
            executor.set_behavior("unit-1", attempt2, attempt=2)
            agents = manager.Agents(None, executor, None, None)
            res = manager.execute_work_units(store, agents=agents, clock=clock, verifier_registry=registry)

            self.assertEqual(res["status"], MILESTONE_FAILED)
            section = load_work_units(store.state)
            self.assertEqual(section["units"]["unit-1"]["status"], "UNIT_FAILED")

    def test_audit_fix03_persisted_runtime_charges_each_stage(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, store, config = fixture(root, "audit-fix03-runtime-charge")
            registry = build_verifier_registry()
            unit = make_spec(unit_id="unit-1", paths=["calculator.py"], verifier_ids=["check-calc"])
            manager.create_run(store, repo, TASK, COMMANDS, config, criteria=["Fix"], work_units=[unit], verifier_registry=registry)

            clock = FakeClock()
            def exec_fn(r, u, c):
                clock.advance(4.5)
                r.write("calculator.py", "def multiply(a, b): return a * b\n")
                return "Done"

            executor = ScriptedExecutor()
            executor.set_behavior("unit-1", exec_fn)
            agents = manager.Agents(None, executor, None, None)
            res = manager.execute_work_units(store, agents=agents, clock=clock, verifier_registry=registry)

            self.assertEqual(res["status"], MILESTONE_READY)
            persisted_runtime = store.state["manager"]["runtime_seconds"]
            self.assertGreaterEqual(persisted_runtime, 4.5)

    # ------------------------------------------------------------------------- Fix 4: Safe Invalidation
    def test_audit_fix04_revalidation_failure_does_not_leave_milestone_ready(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, store, config = fixture(root, "audit-fix04-reval-fail")
            registry = build_verifier_registry()
            unit1 = make_spec(unit_id="unit-1", paths=["helper.py"], verifier_ids=["check-helper"])
            unit2 = make_spec(unit_id="unit-2", dependencies=["unit-1"], paths=["helper.py", "calculator.py"], verifier_ids=["check-calc"])
            manager.create_run(store, repo, TASK, COMMANDS, config, criteria=["Fix"], work_units=[unit1, unit2], verifier_registry=registry)

            executor = ScriptedExecutor()
            executor.set_behavior("unit-1", {"writes": {"helper.py": "def get_value(): return 42\n"}})
            executor.set_behavior("unit-2", {"writes": {"helper.py": "def get_value(): return 0\n",
                                                        "calculator.py": "def multiply(a, b): return a * b\n"}})
            agents = manager.Agents(None, executor, None, None)
            res = manager.execute_work_units(store, agents=agents, verifier_registry=registry)

            self.assertEqual(res["status"], MILESTONE_FAILED)
            self.assertEqual(res["reason"], "STALE_VERIFICATION")
            self.assertNotEqual(store.state["manager"]["milestone_status"], MILESTONE_READY)
            self.assertEqual(store.state["manager"]["milestone_status"], MILESTONE_FAILED)

    def test_audit_fix04_candidate_mutation_after_milestone_ready_invalidates(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, store, config = fixture(root, "audit-fix04-mutate-cand")
            registry = build_verifier_registry()
            unit = make_spec(unit_id="unit-1", paths=["calculator.py"], verifier_ids=["check-calc"])
            manager.create_run(store, repo, TASK, COMMANDS, config, criteria=["Fix"], work_units=[unit], verifier_registry=registry)

            executor = ScriptedExecutor()
            executor.set_behavior("unit-1", {"writes": {"calculator.py": "def multiply(a, b): return a * b\n"}})
            agents = manager.Agents(None, executor, None, None)
            res = manager.execute_work_units(store, agents=agents, verifier_registry=registry)

            self.assertEqual(res["status"], MILESTONE_READY)
            self.assertTrue(is_milestone_ready(store, repo))

            # Tamper candidate repo after milestone ready
            (repo.root / "calculator.py").write_text("def multiply(a, b): return 0\n", encoding="utf-8")
            self.assertFalse(is_milestone_ready(store, repo))

    # ------------------------------------------------------------------------- Fix 5: Supervision Boundaries
    def test_audit_fix05_supervision_check_at_all_seven_lifecycle_points(self):
        import tempfile
        for boundary_idx in range(1, 8):
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                repo, store, config = fixture(root, f"audit-fix05-pt{boundary_idx}")
                registry = build_verifier_registry()
                unit = make_spec(unit_id="unit-1", paths=["calculator.py"], verifier_ids=["check-calc"])
                manager.create_run(store, repo, TASK, COMMANDS, config, criteria=["Fix"], work_units=[unit], verifier_registry=registry)

                executor = ScriptedExecutor()
                executor.set_behavior("unit-1", {"writes": {"calculator.py": "def multiply(a, b): return a * b\n"}})
                agents = manager.Agents(None, executor, None, None)

                mock_sup = MockSupervisor(fail_at_call=boundary_idx)
                scheduler = WorkUnitScheduler(store, repo, verifier_registry=registry, supervisor=mock_sup, agents=agents)
                res = scheduler.run_sequence()

                self.assertEqual(res["status"], "HUMAN_ACTION_REQUIRED", f"Failed to catch boundary {boundary_idx}")
                self.assertEqual(res["reason"], "UNKNOWN_CHILD", f"Failed reason at boundary {boundary_idx}")

    # ------------------------------------------------------------------------- Fix 6: Sequence Order Preservation
    def test_audit_fix06_sequence_order_preserved_across_wal_reload(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, store, config = fixture(root, "audit-fix06-seq")
            registry = build_verifier_registry()
            unit_z = make_spec(unit_id="unit-z", paths=["calculator.py"], verifier_ids=["check-calc"])
            unit_a = make_spec(unit_id="unit-a", paths=["helper.py"], verifier_ids=["check-helper"])

            # Insertion order: unit-z before unit-a
            manager.create_run(store, repo, TASK, COMMANDS, config, criteria=["Fix"], work_units=[unit_z, unit_a], verifier_registry=registry)

            # Reload fresh store instance from disk
            reloaded_store = durable.Store(root / "runs", "audit-fix06-seq")
            reloaded_store.load()
            section = load_work_units(reloaded_store.state)
            self.assertEqual(section.get("sequence"), ["unit-z", "unit-a"])

            scheduler = WorkUnitScheduler(reloaded_store, repo, verifier_registry=registry, agents=manager.Agents(None, ScriptedExecutor(), None, None))
            ready = scheduler.evaluate_readiness(section)
            self.assertEqual([u["unit_id"] for u in ready], ["unit-z", "unit-a"])

    # ------------------------------------------------------------------------- Fix 7: Crash / Resume Reconciliation
    def test_audit_fix07_crash_in_executing_reconciled_to_interrupted(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, store, config = fixture(root, "audit-fix07-executing")
            registry = build_verifier_registry()
            unit = make_spec(unit_id="unit-1", paths=["calculator.py"], verifier_ids=["check-calc"])
            manager.create_run(store, repo, TASK, COMMANDS, config, criteria=["Fix"], work_units=[unit], verifier_registry=registry)

            # Simulate crash during EXECUTING
            section = load_work_units(store.state)
            u = section["units"]["unit-1"]
            work_units.transition_unit(u, "VALIDATED")
            work_units.transition_unit(u, "READY")
            work_units.transition_unit(u, "EXECUTING")
            work_units.create_attempt(u, "unit-1-attempt-1")
            work_units.seal_unit_state(u)
            work_units._reseal_section(section)
            store.commit(work_units=section)

            executor = ScriptedExecutor()
            executor.set_behavior("unit-1", {"writes": {"calculator.py": "def multiply(a, b): return a * b\n"}})
            agents = manager.Agents(None, executor, None, None)
            scheduler = WorkUnitScheduler(store, repo, verifier_registry=registry, agents=agents)

            reconciled_section = scheduler.get_section()
            self.assertEqual(reconciled_section["units"]["unit-1"]["status"], "READY")
            self.assertEqual(reconciled_section["units"]["unit-1"]["attempts"][0]["failure_classification"], "crash_interrupted")

            res = scheduler.run_sequence()
            self.assertEqual(res["status"], MILESTONE_READY)

    def test_audit_fix07_crash_in_verifying_reruns_verifiers_without_executor(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, store, config = fixture(root, "audit-fix07-verifying")
            registry = build_verifier_registry()
            unit = make_spec(unit_id="unit-1", paths=["calculator.py"], verifier_ids=["check-calc"])
            manager.create_run(store, repo, TASK, COMMANDS, config, criteria=["Fix"], work_units=[unit], verifier_registry=registry)

            repo.write("calculator.py", "def multiply(a, b): return a * b\n")
            section = load_work_units(store.state)
            u = section["units"]["unit-1"]
            work_units.transition_unit(u, "VALIDATED")
            work_units.transition_unit(u, "READY")
            work_units.transition_unit(u, "EXECUTING")
            work_units.create_attempt(u, "unit-1-attempt-1")
            work_units.complete_attempt(u, "unit-1-attempt-1", outcome="passed", changed_paths=["calculator.py"])
            work_units.transition_unit(u, "VERIFYING")
            work_units.seal_unit_state(u)
            work_units._reseal_section(section)
            store.commit(work_units=section)

            executor = ScriptedExecutor()
            agents = manager.Agents(None, executor, None, None)
            scheduler = WorkUnitScheduler(store, repo, verifier_registry=registry, agents=agents)

            self.assertEqual(executor.calls, [])
            section_resumed = scheduler.get_section()
            self.assertEqual(section_resumed["units"]["unit-1"]["status"], "UNIT_VERIFIED")

    def test_audit_fix07_reconstruct_repair_evidence_from_attempt_1(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, store, config = fixture(root, "audit-fix07-repair-evidence")
            registry = build_verifier_registry()
            unit = make_spec(unit_id="unit-1", paths=["calculator.py"], verifier_ids=["check-calc"])
            manager.create_run(store, repo, TASK, COMMANDS, config, criteria=["Fix"], work_units=[unit], verifier_registry=registry)

            section = load_work_units(store.state)
            u = section["units"]["unit-1"]
            work_units.transition_unit(u, "VALIDATED")
            work_units.transition_unit(u, "READY")
            work_units.transition_unit(u, "EXECUTING")
            work_units.create_attempt(u, "unit-1-attempt-1")
            work_units.complete_attempt(u, "unit-1-attempt-1", outcome="failed", failure_classification="syntax_error", changed_paths=["calculator.py"])
            work_units.transition_unit(u, "REPAIR_PENDING")
            work_units.transition_unit(u, "READY")
            work_units.seal_unit_state(u)
            work_units._reseal_section(section)
            store.commit(work_units=section)

            captured_context = []
            def repair_exec(r, u, c):
                captured_context.append(copy.deepcopy(c))
                r.write("calculator.py", "def multiply(a, b): return a * b\n")
                return "Repaired"

            executor = ScriptedExecutor()
            executor.set_behavior("unit-1", repair_exec, attempt=2)
            agents = manager.Agents(None, executor, None, None)
            res = manager.execute_work_units(store, agents=agents, verifier_registry=registry)

            self.assertEqual(res["status"], MILESTONE_READY)
            self.assertTrue(len(captured_context) > 0)
            rep_evidence = captured_context[0]["repair_evidence"]
            self.assertIsNotNone(rep_evidence)
            self.assertEqual(rep_evidence["attempt"], 1)
            self.assertEqual(rep_evidence["failure_classification"], "syntax_error")

    def test_audit_fix07_prepare_resume_succeeds_on_work_units_run(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, store, config = fixture(root, "audit-fix07-prep-resume")
            registry = build_verifier_registry()
            unit = make_spec(unit_id="unit-1", paths=["calculator.py"], verifier_ids=["check-calc"])
            manager.create_run(store, repo, TASK, COMMANDS, config, criteria=["Fix"], work_units=[unit], verifier_registry=registry)

            scoped_repo = manager.ScopedRepository(repo.root, config, harness.EventLog(root / "test.jsonl"), store)
            scoped_repo.write("calculator.py", "def multiply(a, b): return a * b\n")
            action = manager.prepare_resume(store)
            self.assertEqual(action, "continue")

    # ------------------------------------------------------------------------- Fix 8: Terminal Failure Halts
    def test_audit_fix08_terminal_unit_failure_halts_scheduling_independent_unit(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, store, config = fixture(root, "audit-fix08-halt")
            registry = build_verifier_registry()
            unit_a = make_spec(unit_id="unit-a", paths=["calculator.py"], verifier_ids=["check-calc"])
            unit_b = make_spec(unit_id="unit-b", paths=["helper.py"], verifier_ids=["check-helper"])

            manager.create_run(store, repo, TASK, COMMANDS, config, criteria=["Fix"], work_units=[unit_a, unit_b], verifier_registry=registry)

            executor = ScriptedExecutor()
            executor.set_behavior("unit-a", "failure", attempt=1)
            executor.set_behavior("unit-a", "failure", attempt=2)
            executor.set_behavior("unit-b", {"writes": {"helper.py": "def get_value(): return 42\n"}})
            agents = manager.Agents(None, executor, None, None)
            res = manager.execute_work_units(store, agents=agents, verifier_registry=registry)

            self.assertEqual(res["status"], MILESTONE_FAILED)
            self.assertEqual(len(executor.get_attempts("unit-b")), 0)
            section = load_work_units(store.state)
            self.assertEqual(section["units"]["unit-a"]["status"], "UNIT_FAILED")
            self.assertNotEqual(section["units"]["unit-b"]["status"], "UNIT_VERIFIED")

    # ------------------------------------------------------------------------- Fix 9: Environment Fingerprint
    def test_audit_fix09_environment_identity_tracks_work_units_files(self):
        import tempfile
        import environment
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, store, config = fixture(root, "audit-fix09-env")
            fp = environment.capture(config, repo.root, COMMANDS)
            source = fp.get("source", {})
            self.assertIn("work_units.py", source)
            self.assertIn("work_unit_scheduler.py", source)
            self.assertIsNotNone(source["work_units.py"].get("sha256"))
            self.assertIsNotNone(source["work_unit_scheduler.py"].get("sha256"))

    # ------------------------------------------------------------------------- Fix 10: Require Compatible Executor
    def test_audit_fix10_missing_or_incompatible_executor_rejected_before_attempt(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, store, config = fixture(root, "audit-fix10-exec")
            registry = build_verifier_registry()
            unit = make_spec(unit_id="unit-1", paths=["calculator.py"], verifier_ids=["check-calc"])
            manager.create_run(store, repo, TASK, COMMANDS, config, criteria=["Fix"], work_units=[unit], verifier_registry=registry)

            with self.assertRaises(WorkUnitSchedulingError):
                manager.execute_work_units(store, agents=None, verifier_registry=registry)

            incompatible = manager.Agents(None, object(), None, None)
            with self.assertRaises(WorkUnitSchedulingError):
                manager.execute_work_units(store, agents=incompatible, verifier_registry=registry)

            section = load_work_units(store.state)
            self.assertEqual(len(section["units"]["unit-1"]["attempts"]), 0)

    # ------------------------------------------------------------------------- Fix 11: Preserve Stop Status
    def test_audit_fix11_scheduler_stop_status_preserved_in_conclusion(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, store, config = fixture(root, "audit-fix11-stop")
            registry = build_verifier_registry()
            unit = make_spec(unit_id="unit-1", paths=["calculator.py"], verifier_ids=["check-calc"])
            manager.create_run(store, repo, TASK, COMMANDS, config, criteria=["Fix"], work_units=[unit], verifier_registry=registry)

            executor = ScriptedExecutor()
            agents = manager.Agents(None, executor, None, None)

            mock_sup = MockSupervisor(fail_at_call=1)
            with manager.Supervisor(store) as supervisor:
                log = harness.EventLog(root / "test.jsonl")
                loop = manager.ManagerLoop(store, mock_sup, repo, None, agents, log, time.monotonic)
                report = loop.run()
                self.assertEqual(report["status"], "HUMAN_ACTION_REQUIRED")
                self.assertEqual(store.state["manager"]["stop"]["reason"], "UNKNOWN_CHILD")


    # ------------------------------------------------------------------------- Fix A: Shared Unit Budget Includes Current Attempt
    def test_audit_fix_a_shared_unit_budget_includes_current_attempt(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, store, config = fixture(root, "audit-fix-a")
            registry = build_verifier_registry()
            registry["check-calc"]["timeout_seconds"] = 30.0
            unit = make_spec(
                unit_id="unit-budget",
                paths=["calculator.py"],
                verifier_ids=["check-calc"],
                limits={"max_attempts": 2, "timeout_seconds": 10.0},
            )
            manager.create_run(
                store, repo, TASK, COMMANDS, config,
                criteria=["Fix"],
                budgets={"max_runtime_seconds": 100.0},
                work_units=[unit],
                verifier_registry=registry,
            )

            current_time = [0.0]

            def fake_clock():
                return current_time[0]

            observed_verifier_timeouts = []
            orig_repo_execute = repo.execute

            def tracking_repo_execute(argv, timeout=None):
                if timeout is not None:
                    observed_verifier_timeouts.append(timeout)
                current_time[0] += 6.0
                return orig_repo_execute(argv, timeout=timeout)

            repo.execute = tracking_repo_execute

            def executor_fn(repo_arg, spec, ctx):
                current_time[0] += 6.0
                repo_arg.write("calculator.py", "def multiply(a, b): return a * b\n")
                return "Mutated"

            executor = ScriptedExecutor()
            executor.set_behavior("unit-budget", executor_fn)
            agents = manager.Agents(None, executor, None, None)

            scheduler = WorkUnitScheduler(
                store, repo, verifier_registry=registry, agents=agents, clock=fake_clock
            )
            res = scheduler.run_sequence()

            self.assertTrue(len(observed_verifier_timeouts) > 0)
            self.assertLessEqual(observed_verifier_timeouts[0], 4.0)

            self.assertNotEqual(res["status"], MILESTONE_READY)
            self.assertEqual(res["status"], "BUDGET_EXHAUSTED")
            section = load_work_units(store.state)
            unit_state = section["units"]["unit-budget"]
            self.assertNotEqual(unit_state["status"], "UNIT_VERIFIED")

    # ------------------------------------------------------------------------- Fix B: Runtime Preserved on All Exit Paths
    def test_audit_fix_b_revalidation_timeout_preserves_runtime(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, store, config = fixture(root, "audit-fix-b-1")
            registry = build_verifier_registry()
            unit = make_spec(unit_id="unit-1", paths=["calculator.py"], verifier_ids=["check-calc"])
            manager.create_run(
                store, repo, TASK, COMMANDS, config,
                criteria=["Fix"],
                budgets={"max_runtime_seconds": 10.0},
                work_units=[unit],
                verifier_registry=registry,
            )
            current_time = [0.0]

            def fake_clock():
                return current_time[0]

            def executor_fn(repo_arg, spec, ctx):
                current_time[0] += 2.0
                repo_arg.write("calculator.py", "def multiply(a, b): return a * b\n")
                return "Mutated"

            executor = ScriptedExecutor()
            executor.set_behavior("unit-1", executor_fn)
            agents = manager.Agents(None, executor, None, None)

            orig_execute = repo.execute

            def reval_execute(argv, timeout=None):
                current_time[0] = 11.0
                return orig_execute(argv, timeout=timeout)

            repo.execute = reval_execute

            scheduler = WorkUnitScheduler(
                store, repo, verifier_registry=registry, agents=agents, clock=fake_clock
            )
            res = scheduler.run_sequence()

            self.assertEqual(res["status"], "BUDGET_EXHAUSTED")
            persisted_runtime = store.state["manager"]["runtime_seconds"]
            self.assertGreaterEqual(persisted_runtime, 10.0)

    def test_audit_fix_b_verifier_exhaustion_preserves_runtime(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, store, config = fixture(root, "audit-fix-b-2")
            registry = build_verifier_registry()
            unit = make_spec(
                unit_id="unit-1",
                paths=["calculator.py"],
                verifier_ids=["check-calc"],
                limits={"max_attempts": 2, "timeout_seconds": 5.0},
            )
            manager.create_run(
                store, repo, TASK, COMMANDS, config,
                criteria=["Fix"],
                budgets={"max_runtime_seconds": 50.0},
                work_units=[unit],
                verifier_registry=registry,
            )
            current_time = [0.0]

            def fake_clock():
                return current_time[0]

            def executor_fn(repo_arg, spec, ctx):
                current_time[0] += 5.5
                repo_arg.write("calculator.py", "def multiply(a, b): return a * b\n")
                return "Mutated"

            executor = ScriptedExecutor()
            executor.set_behavior("unit-1", executor_fn)
            agents = manager.Agents(None, executor, None, None)

            scheduler = WorkUnitScheduler(
                store, repo, verifier_registry=registry, agents=agents, clock=fake_clock
            )
            res = scheduler.run_sequence()

            self.assertEqual(res["status"], "BUDGET_EXHAUSTED")
            persisted_runtime = store.state["manager"]["runtime_seconds"]
            self.assertGreaterEqual(persisted_runtime, 5.0)

    def test_audit_fix_b_crashed_attempt_retains_consumed_runtime(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, store, config = fixture(root, "audit-fix-b-3")
            registry = build_verifier_registry()
            unit = make_spec(
                unit_id="unit-1",
                paths=["calculator.py"],
                verifier_ids=["check-calc"],
                limits={"max_attempts": 2, "timeout_seconds": 10.0},
            )
            manager.create_run(
                store, repo, TASK, COMMANDS, config,
                criteria=["Fix"],
                budgets={"max_runtime_seconds": 50.0},
                work_units=[unit],
                verifier_registry=registry,
            )
            current_time = [0.0]

            def fake_clock():
                return current_time[0]

            scheduler = WorkUnitScheduler(
                store, repo, verifier_registry=registry,
                agents=manager.Agents(None, ScriptedExecutor(), None, None),
                clock=fake_clock,
            )
            section = scheduler.get_section()
            ready = scheduler.evaluate_readiness(section)
            unit_to_run = ready[0]
            work_units.transition_unit(unit_to_run, "EXECUTING")

            att_id = "unit-1-attempt-1"
            work_units.create_attempt(unit_to_run, att_id)
            att_rec = unit_to_run["attempts"][-1]
            att_rec["pre_attempt_snapshot"] = durable.snapshot(repo)
            att_rec["started_at_clock"] = fake_clock()
            att_rec["started_at_runtime"] = 0.0
            scheduler._persist_unit(section, unit_to_run)

            current_time[0] = 6.0
            repo.write("calculator.py", "def multiply(a, b): return a * b\n")

            del scheduler
            del store

            new_store = durable.Store(root / "runs", "audit-fix-b-3")
            new_store.load()

            def repair_executor_fn(repo_arg, spec, ctx):
                current_time[0] += 6.0
                repo_arg.write("calculator.py", "def multiply(a, b): return a * b\n")
                return "Mutated"

            repair_exec = ScriptedExecutor()
            repair_exec.set_behavior("unit-1", repair_executor_fn)
            new_agents = manager.Agents(None, repair_exec, None, None)

            new_scheduler = WorkUnitScheduler(
                new_store, repo, verifier_registry=registry,
                agents=new_agents, clock=fake_clock,
            )

            reloaded_sec = load_work_units(new_store.state)
            u1 = reloaded_sec["units"]["unit-1"]
            self.assertEqual(u1["attempts"][0]["timeout_info"]["elapsed_seconds"], 6.0)

            res = new_scheduler.run_sequence()
            self.assertNotEqual(res["status"], MILESTONE_READY)
            self.assertIn(res["status"], (MILESTONE_FAILED, "BUDGET_EXHAUSTED"))
            self.assertEqual(res["reason"], "executor_timeout")
            self.assertGreaterEqual(new_store.state["manager"]["runtime_seconds"], 10.0)

    def test_audit_fix_b_interrupted_verifier_runtime_recovery_budget_exhaustion(self):
        """Exact audit regression for Fix B:
        - budget = 10
        - executor consumes 6
        - verifier consumes 6
        - crash/reload
        - verify recovery halts without MILESTONE_READY
        - verify enclosing runtime reflects >= 12 seconds (or budget exhaustion)
        - verify no double-counting when no interruption occurs or multiple reloads occur
        """
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_id = "audit-fix-b-interrupted-verifier"
            repo, store, config = fixture(root, run_id)
            registry = build_verifier_registry()
            unit = make_spec(
                unit_id="unit-1",
                paths=["calculator.py"],
                verifier_ids=["check-calc"],
                limits={"max_attempts": 2, "timeout_seconds": 60.0},
            )
            manager.create_run(
                store, repo, TASK, COMMANDS, config,
                criteria=["Fix"],
                budgets={"max_runtime_seconds": 10.0},
                work_units=[unit],
                verifier_registry=registry,
            )
            current_time = [0.0]

            def fake_clock():
                return current_time[0]

            def executor_fn(repo_arg, spec, ctx):
                current_time[0] += 6.0
                repo_arg.write("calculator.py", "def multiply(a, b): return a * b\n")
                return "Mutated"

            executor = ScriptedExecutor()
            executor.set_behavior("unit-1", executor_fn)
            agents = manager.Agents(None, executor, None, None)

            # Verifier starts, consumes 6.0s, and simulated crash occurs before verifier returns
            orig_execute = repo.execute

            class SimulatedCrash(BaseException):
                pass

            def verifier_crash_execute(argv, timeout=None):
                current_time[0] += 6.0
                raise SimulatedCrash("Process crashed inside verifier execution")

            repo.execute = verifier_crash_execute

            scheduler = WorkUnitScheduler(
                store, repo, verifier_registry=registry, agents=agents, clock=fake_clock
            )

            # Run sequence until crash occurs during verification
            with self.assertRaises(SimulatedCrash):
                scheduler.run_sequence()

            # At crash time, persisted runtime on disk was only 6.0 (from executor)
            self.assertEqual(store.state["manager"]["runtime_seconds"], 6.0)

            # Drop references simulating process exit
            del scheduler
            del store

            # Reload Store and WAL in a new process instance
            new_store = durable.Store(root / "runs", run_id)
            new_store.load()

            # Restore working execute for the verifier
            repo.execute = orig_execute

            # New scheduler resumes with clock at 12.0s
            new_scheduler = WorkUnitScheduler(
                new_store, repo, verifier_registry=registry, agents=agents, clock=fake_clock
            )

            # Recovery halts without MILESTONE_READY due to budget exhaustion
            res = new_scheduler.run_sequence()
            self.assertEqual(res["status"], "BUDGET_EXHAUSTED")
            self.assertFalse(is_milestone_ready(new_store, repo))

            # Verify enclosing runtime reflects >= 12.0 seconds
            reloaded_runtime = new_store.state["manager"]["runtime_seconds"]
            self.assertGreaterEqual(reloaded_runtime, 12.0)

            # Verify no double-counting on multiple subsequent reloads
            third_store = durable.Store(root / "runs", run_id)
            third_store.load()
            third_scheduler = WorkUnitScheduler(
                third_store, repo, verifier_registry=registry, agents=agents, clock=fake_clock
            )
            self.assertAlmostEqual(third_store.state["manager"]["runtime_seconds"], reloaded_runtime)

    def test_audit_fix_b_clean_run_no_double_counting_on_reload(self):
        """Verify that when no interruption occurs, reload does not double count runtime."""
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_id = "audit-fix-b-clean-run"
            repo, store, config = fixture(root, run_id)
            registry = build_verifier_registry()
            unit = make_spec(
                unit_id="unit-1",
                paths=["calculator.py"],
                verifier_ids=["check-calc"],
            )
            manager.create_run(
                store, repo, TASK, COMMANDS, config,
                criteria=["Fix"],
                budgets={"max_runtime_seconds": 30.0},
                work_units=[unit],
                verifier_registry=registry,
            )
            current_time = [0.0]

            def fake_clock():
                return current_time[0]

            def executor_fn(repo_arg, spec, ctx):
                current_time[0] += 3.0
                repo_arg.write("calculator.py", "def multiply(a, b): return a * b\n")
                return "Mutated"

            executor = ScriptedExecutor()
            executor.set_behavior("unit-1", executor_fn)
            agents = manager.Agents(None, executor, None, None)

            orig_execute = repo.execute

            def timed_execute(argv, timeout=None):
                current_time[0] += 2.0
                return orig_execute(argv, timeout=timeout)

            repo.execute = timed_execute

            scheduler = WorkUnitScheduler(
                store, repo, verifier_registry=registry, agents=agents, clock=fake_clock
            )
            res = scheduler.run_sequence()
            self.assertEqual(res["status"], MILESTONE_READY)

            persisted_runtime = store.state["manager"]["runtime_seconds"]
            self.assertGreaterEqual(persisted_runtime, 5.0)

            # Reload into new scheduler and advance clock
            current_time[0] += 10.0
            reloaded_store = durable.Store(root / "runs", run_id)
            reloaded_store.load()
            reloaded_scheduler = WorkUnitScheduler(
                reloaded_store, repo, verifier_registry=registry, agents=agents, clock=fake_clock
            )
            # Active stage timing was cleared on clean completion; runtime must not increase on idle reload
            self.assertAlmostEqual(reloaded_store.state["manager"]["runtime_seconds"], persisted_runtime)

    # ------------------------------------------------------------------------- Fix C: Crash Illegal Mutation Fails Closed
    def test_audit_fix_c_crash_illegal_mutation_fails_closed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, store, config = fixture(root, "audit-fix-c")
            registry = build_verifier_registry()
            unit = make_spec(
                unit_id="unit-scope",
                paths=["calculator.py"],
                verifier_ids=["check-calc"],
            )
            manager.create_run(
                store, repo, TASK, COMMANDS, config,
                criteria=["Fix"],
                work_units=[unit],
                verifier_registry=registry,
            )

            scheduler = WorkUnitScheduler(
                store, repo, verifier_registry=registry,
                agents=manager.Agents(None, ScriptedExecutor(), None, None),
            )
            section = scheduler.get_section()
            ready = scheduler.evaluate_readiness(section)
            u = ready[0]
            work_units.transition_unit(u, "EXECUTING")

            att_id = "unit-scope-attempt-1"
            work_units.create_attempt(u, att_id)
            att_rec = u["attempts"][-1]
            att_rec["pre_attempt_snapshot"] = durable.snapshot(repo)
            scheduler._persist_unit(section, u)

            repo.write("escape.txt", "illegal mutation\n")

            del scheduler
            del store

            new_store = durable.Store(root / "runs", "audit-fix-c")
            new_store.load()

            new_scheduler = WorkUnitScheduler(
                new_store, repo, verifier_registry=registry,
                agents=manager.Agents(None, ScriptedExecutor(), None, None),
            )
            reloaded_sec = load_work_units(new_store.state)
            reloaded_u = reloaded_sec["units"]["unit-scope"]

            self.assertEqual(reloaded_u["status"], "UNIT_FAILED")
            self.assertEqual(reloaded_u["attempts"][0]["failure_classification"], "scope_violation")

            res = new_scheduler.run_sequence()
            self.assertEqual(res["status"], MILESTONE_FAILED)
            self.assertFalse(is_milestone_ready(new_store, repo))

    # ------------------------------------------------------------------------- Fix D: Guarded Verification in Crash Recovery
    def test_audit_fix_d_verifying_crash_with_active_child_blocks_verification(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, store, config = fixture(root, "audit-fix-d")
            registry = build_verifier_registry()
            unit = make_spec(unit_id="unit-1", paths=["calculator.py"], verifier_ids=["check-calc"])
            manager.create_run(store, repo, TASK, COMMANDS, config, criteria=["Fix"], work_units=[unit], verifier_registry=registry)

            repo.write("calculator.py", "def multiply(a, b): return a * b\n")
            section = load_work_units(store.state)
            u = section["units"]["unit-1"]
            work_units.transition_unit(u, "VALIDATED")
            work_units.transition_unit(u, "READY")
            work_units.transition_unit(u, "EXECUTING")
            work_units.create_attempt(u, "unit-1-attempt-1")
            u["attempts"][-1]["pre_attempt_snapshot"] = durable.snapshot(repo)
            u["attempts"][-1]["changed_paths"] = ["calculator.py"]
            work_units.transition_unit(u, "VERIFYING")
            work_units.seal_unit_state(u)
            work_units._reseal_section(section)
            store.commit(work_units=section)

            mock_sup = MockSupervisor(fail_at_call=1)
            executor = ScriptedExecutor()
            agents = manager.Agents(None, executor, None, None)

            scheduler = WorkUnitScheduler(
                store, repo, verifier_registry=registry, supervisor=mock_sup, agents=agents
            )
            res = scheduler.run_sequence()

            self.assertEqual(res["status"], "HUMAN_ACTION_REQUIRED")
            self.assertEqual(res["reason"], "UNKNOWN_CHILD")
            sec = load_work_units(store.state)
            self.assertNotEqual(sec["units"]["unit-1"]["status"], "UNIT_VERIFIED")

    # ------------------------------------------------------------------------- Fix E: Sequence Integrity Validation
    def test_audit_fix_e_sequence_integrity_tampering_fails_closed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, store, config = fixture(root, "audit-fix-e")
            registry = build_verifier_registry()
            unit1 = make_spec(unit_id="unit-1", paths=["calculator.py"], verifier_ids=["check-calc"])
            unit2 = make_spec(unit_id="unit-2", dependencies=["unit-1"], paths=["calculator.py"], verifier_ids=["check-calc"])
            manager.create_run(store, repo, TASK, COMMANDS, config, criteria=["Fix"], work_units=[unit1, unit2], verifier_registry=registry)

            # 1. Missing sequence container
            section = load_work_units(store.state)
            sec_missing = copy.deepcopy(section)
            del sec_missing["sequence"]
            with self.assertRaises(WorkUnitSchedulingError):
                validate_sequence_integrity(sec_missing)

            # 2. Duplicate unit IDs in sequence
            sec_dup = copy.deepcopy(section)
            sec_dup["sequence"] = ["unit-1", "unit-1"]
            with self.assertRaises(WorkUnitSchedulingError):
                validate_sequence_integrity(sec_dup)

            # 3. Unknown unit ID in sequence
            sec_unk = copy.deepcopy(section)
            sec_unk["sequence"] = ["unit-1", "unit-phantom"]
            with self.assertRaises(WorkUnitSchedulingError):
                validate_sequence_integrity(sec_unk)

            # 4. Missing required unit from sequence
            sec_miss = copy.deepcopy(section)
            sec_miss["sequence"] = ["unit-1"]
            with self.assertRaises(WorkUnitSchedulingError):
                validate_sequence_integrity(sec_miss)

            # 5. Ordinal / sequence index divergence
            sec_ord = copy.deepcopy(section)
            sec_ord["units"]["unit-1"]["ordinal"] = 99
            with self.assertRaises(WorkUnitSchedulingError):
                validate_sequence_integrity(sec_ord)

    # ------------------------------------------------------------------------- Fix F: Milestone Readiness Candidate Binding
    def test_audit_fix_f_milestone_readiness_candidate_binding(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, store, config = fixture(root, "audit-fix-f")
            registry = build_verifier_registry()
            unit = make_spec(unit_id="unit-1", paths=["calculator.py"], verifier_ids=["check-calc"])
            manager.create_run(store, repo, TASK, COMMANDS, config, criteria=["Fix"], work_units=[unit], verifier_registry=registry)

            executor = ScriptedExecutor()
            executor.set_behavior("unit-1", {"writes": {"calculator.py": "def multiply(a, b): return a * b\n"}})
            agents = manager.Agents(None, executor, None, None)
            scheduler = WorkUnitScheduler(store, repo, verifier_registry=registry, agents=agents)
            res = scheduler.run_sequence()

            self.assertEqual(res["status"], MILESTONE_READY)
            self.assertTrue(is_milestone_ready(store, repo))

            # Case 1: milestone_candidate_snapshot is missing -> False
            mgr = copy.deepcopy(store.state["manager"])
            saved_snap = mgr["milestone_candidate_snapshot"]
            del mgr["milestone_candidate_snapshot"]
            store.commit(manager=mgr)
            self.assertFalse(is_milestone_ready(store, repo))

            # Restore snapshot
            mgr["milestone_candidate_snapshot"] = saved_snap
            store.commit(manager=mgr)
            self.assertTrue(is_milestone_ready(store, repo))

            # Case 2: milestone_candidate_fingerprint is missing -> False
            saved_fp = mgr["milestone_candidate_fingerprint"]
            del mgr["milestone_candidate_fingerprint"]
            store.commit(manager=mgr)
            self.assertFalse(is_milestone_ready(store, repo))

            # Restore fingerprint
            mgr["milestone_candidate_fingerprint"] = saved_fp
            store.commit(manager=mgr)
            self.assertTrue(is_milestone_ready(store, repo))

            # Case 3: Working-tree candidate mutations with unchanged HEAD -> False
            repo.write("untracked.txt", "tampered working tree\n")
            self.assertFalse(is_milestone_ready(store, repo))

            # Remove untracked file to restore clean working tree
            (repo.root / "untracked.txt").unlink()
            self.assertTrue(is_milestone_ready(store, repo))

            # Case 4: Missing unit candidate_snapshot -> False
            sec = copy.deepcopy(store.state["work_units"])
            u1_snap = copy.deepcopy(sec["units"]["unit-1"]["result"]["candidate_snapshot"])
            del sec["units"]["unit-1"]["result"]["candidate_snapshot"]
            work_units.seal_unit_state(sec["units"]["unit-1"])
            work_units._reseal_section(sec)
            store.commit(work_units=sec)
            self.assertFalse(is_milestone_ready(store, repo))

            # Case 5: Malformed unit candidate_snapshot -> False
            sec["units"]["unit-1"]["result"]["candidate_snapshot"] = {"head": 123}
            work_units.seal_unit_state(sec["units"]["unit-1"])
            work_units._reseal_section(sec)
            store.commit(work_units=sec)
            self.assertFalse(is_milestone_ready(store, repo))

            sec["units"]["unit-1"]["result"]["candidate_snapshot"] = "not-a-dict"
            work_units.seal_unit_state(sec["units"]["unit-1"])
            work_units._reseal_section(sec)
            store.commit(work_units=sec)
            self.assertFalse(is_milestone_ready(store, repo))

            # Case 6: Unit candidate_snapshot with altered file hash -> False
            corrupted_snap = copy.deepcopy(u1_snap)
            corrupted_snap["files"]["calculator.py"]["sha256"] = "0" * 64
            sec["units"]["unit-1"]["result"]["candidate_snapshot"] = corrupted_snap
            work_units.seal_unit_state(sec["units"]["unit-1"])
            work_units._reseal_section(sec)
            store.commit(work_units=sec)
            self.assertFalse(is_milestone_ready(store, repo))

            # Case 7: Unit candidate_id mismatch -> False
            sec["units"]["unit-1"]["result"]["candidate_snapshot"] = copy.deepcopy(u1_snap)
            sec["units"]["unit-1"]["result"]["candidate_id"] = "mismatched-head-hash"
            work_units.seal_unit_state(sec["units"]["unit-1"])
            work_units._reseal_section(sec)
            store.commit(work_units=sec)
            self.assertFalse(is_milestone_ready(store, repo))

            # Case 8: Unit trust_level mismatch (not INTERMEDIATE) -> False
            sec["units"]["unit-1"]["result"]["candidate_id"] = saved_fp
            # Restore through a sealed commit; caller payloads are detached
            # from published Store state after commit.
            work_units.seal_unit_state(sec["units"]["unit-1"])
            work_units._reseal_section(sec)
            store.commit(work_units=sec)
            # Directly mutate state without commit to test is_milestone_ready trust enforcement
            store.state["work_units"]["units"]["unit-1"]["result"]["trust_level"] = "TRUSTED"
            self.assertFalse(is_milestone_ready(store, repo))

            # Restore correct intermediate trust level -> True
            store.state["work_units"]["units"]["unit-1"]["result"]["trust_level"] = "INTERMEDIATE"
            store.state["work_units"]["units"]["unit-1"]["result"]["candidate_snapshot"] = copy.deepcopy(u1_snap)
            self.assertTrue(is_milestone_ready(store, repo))

    def test_audit_fix_f_multi_unit_sequence_rebinding_to_final_candidate_snapshot(self):
        """Verify that multi-unit sequences rebind all units to the final candidate snapshot."""
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, store, config = fixture(root, "audit-fix-f-multi")
            registry = build_verifier_registry()

            unit1 = make_spec(
                unit_id="unit-1",
                objective="Create helper",
                dependencies=[],
                paths=["helper.py"],
                verifier_ids=["check-helper"],
            )
            unit2 = make_spec(
                unit_id="unit-2",
                objective="Create main",
                dependencies=["unit-1"],
                paths=["main.py"],
                verifier_ids=["check-main"],
            )

            manager.create_run(
                store, repo, TASK, COMMANDS, config,
                criteria=["Fix"],
                work_units=[unit1, unit2],
                verifier_registry=registry,
            )

            executor = ScriptedExecutor()
            executor.set_behavior("unit-1", {"writes": {"helper.py": "def get_value():\n    return 42\n"}})
            executor.set_behavior("unit-2", {"writes": {"main.py": "import helper\ndef run():\n    return helper.get_value() + 58\n"}})

            agents = manager.Agents(None, executor, None, None)
            scheduler = WorkUnitScheduler(store, repo, verifier_registry=registry, agents=agents)
            res = scheduler.run_sequence()

            self.assertEqual(res["status"], MILESTONE_READY)
            self.assertTrue(is_milestone_ready(store, repo))

            section = store.state["work_units"]
            u1_res = section["units"]["unit-1"]["result"]
            u2_res = section["units"]["unit-2"]["result"]
            mgr = store.state["manager"]
            final_snap = mgr["milestone_candidate_snapshot"]
            final_fp = mgr["milestone_candidate_fingerprint"]

            # Both units are UNIT_VERIFIED and trust_level is INTERMEDIATE
            self.assertEqual(section["units"]["unit-1"]["status"], "UNIT_VERIFIED")
            self.assertEqual(section["units"]["unit-2"]["status"], "UNIT_VERIFIED")
            self.assertEqual(u1_res["trust_level"], "INTERMEDIATE")
            self.assertEqual(u2_res["trust_level"], "INTERMEDIATE")

            # Crucial Fix F assertion: Both units rebound to identical final candidate snapshot
            self.assertEqual(u1_res["candidate_id"], final_fp)
            self.assertEqual(u2_res["candidate_id"], final_fp)
            self.assertEqual(u1_res["candidate_snapshot"], final_snap)
            self.assertEqual(u2_res["candidate_snapshot"], final_snap)
            self.assertTrue(is_structurally_valid_snapshot(u1_res["candidate_snapshot"]))
            self.assertTrue(is_structurally_valid_snapshot(u2_res["candidate_snapshot"]))

            # Tampering unit-1's rebound snapshot invalidates milestone readiness
            tampered_sec = copy.deepcopy(section)
            tampered_sec["units"]["unit-1"]["result"]["candidate_snapshot"]["files"]["helper.py"]["sha256"] = "1" * 64
            work_units.seal_unit_state(tampered_sec["units"]["unit-1"])
            work_units._reseal_section(tampered_sec)
            store.commit(work_units=tampered_sec)
            self.assertFalse(is_milestone_ready(store, repo))

    # ------------------------------------------------------------------------- Fix G: Active VERIFYING Crash Recovery
    def test_audit_fix_g_active_verifying_crash_resumes_same_attempt(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, store, config = fixture(root, "audit-fix-g")
            registry = build_verifier_registry()
            unit = make_spec(unit_id="unit-1", paths=["calculator.py"], verifier_ids=["check-calc"])
            manager.create_run(store, repo, TASK, COMMANDS, config, criteria=["Fix"], work_units=[unit], verifier_registry=registry)

            repo.write("calculator.py", "def multiply(a, b): return a * b\n")
            section = load_work_units(store.state)
            u = section["units"]["unit-1"]
            work_units.transition_unit(u, "VALIDATED")
            work_units.transition_unit(u, "READY")
            work_units.transition_unit(u, "EXECUTING")
            work_units.create_attempt(u, "unit-1-attempt-1")
            u["attempts"][-1]["pre_attempt_snapshot"] = durable.snapshot(repo)
            u["attempts"][-1]["changed_paths"] = ["calculator.py"]
            work_units.transition_unit(u, "VERIFYING")
            work_units.seal_unit_state(u)
            work_units._reseal_section(section)
            store.commit(work_units=section)

            del store
            new_store = durable.Store(root / "runs", "audit-fix-g")
            new_store.load()

            executor = ScriptedExecutor()
            agents = manager.Agents(None, executor, None, None)

            new_scheduler = WorkUnitScheduler(
                new_store, repo, verifier_registry=registry, agents=agents
            )

            # Verification of the SAME candidate succeeds without rerunning executor
            self.assertEqual(len(executor.calls), 0)

            # Attempt count remains 1 (no unnecessary second attempt)
            sec = load_work_units(new_store.state)
            u_res = sec["units"]["unit-1"]
            self.assertEqual(len(u_res["attempts"]), 1)
            self.assertEqual(u_res["attempts"][0]["attempt_id"], "unit-1-attempt-1")
            self.assertEqual(u_res["status"], "UNIT_VERIFIED")

            res = new_scheduler.run_sequence()
            self.assertEqual(res["status"], MILESTONE_READY)

    # ------------------------------------------------------------------------- Fix H: Rejection of Missing Agents in execute()
    def test_audit_fix_h_manager_execute_rejects_missing_workunit_agents(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, store, config = fixture(root, "audit-fix-h")
            registry = build_verifier_registry()
            unit = make_spec(unit_id="unit-1", paths=["calculator.py"], verifier_ids=["check-calc"])
            manager.create_run(store, repo, TASK, COMMANDS, config, criteria=["Fix"], work_units=[unit], verifier_registry=registry)

            with self.assertRaises(WorkUnitSchedulingError):
                manager.execute(store, agents=None)

            section = load_work_units(store.state)
            self.assertEqual(len(section["units"]["unit-1"]["attempts"]), 0)


if __name__ == "__main__":
    unittest.main()
