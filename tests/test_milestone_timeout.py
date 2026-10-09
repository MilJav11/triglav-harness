"""Focused offline milestone deadlines, including a real owned sleeping verifier."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import durable
import manager
import winprocess
from supervision import Supervisor
from manager import Stop
import work_unit_scheduler as scheduler_module
from work_unit_scheduler import ScriptedExecutor, WorkUnitScheduler
from test_reviewer_wave6 import make_spec, setup_test_fixture


class MilestoneTimeoutTests(unittest.TestCase):
    def scheduler(self, configured=30, remaining=600):
        scheduler = WorkUnitScheduler.__new__(WorkUnitScheduler)
        scheduler.repo = Mock()
        scheduler.repo.execute.return_value = {"exit_code": 0}
        scheduler.verifier_registry = {"check": {"argv": ["reviewed-fixture"], "timeout_seconds": configured}}
        scheduler.guard_supervision = Mock()
        scheduler.check_task_budget = Mock()
        scheduler.remaining_task_budget = Mock(return_value=remaining)
        scheduler.clock = Mock(return_value=0)
        scheduler._persist_runtime = Mock()
        scheduler._persist = Mock()
        section = {"sequence": ["unit"], "units": {"unit": {
            "ordinal": 0, "status": "UNIT_VERIFIED", "spec": {"verifier_ids": ["check"]}}}}
        return scheduler, section

    def revalidate(self, scheduler, section):
        # Only numerical boundary tests use synthetic repository snapshots.
        # The final test executes the real repository, scheduler and supervisor.
        with patch.object(scheduler_module, "snapshot", return_value={"head": "unchanged"}), \
             patch.object(scheduler_module.work_units, "_reseal_section"):
            return scheduler.revalidate_milestone(section)

    def test_configured_verifier_limit_caps_large_task_budget(self):
        scheduler, section = self.scheduler(30, 600)
        self.assertEqual(self.revalidate(scheduler, section), (True, None))
        scheduler.repo.execute.assert_called_once_with(["reviewed-fixture"], timeout=30)

    def test_remaining_task_budget_caps_verifier_limit(self):
        scheduler, section = self.scheduler(30, 2.5)
        self.assertEqual(self.revalidate(scheduler, section), (True, None))
        scheduler.repo.execute.assert_called_once_with(["reviewed-fixture"], timeout=2.5)

    def test_missing_verifier_timeout_keeps_existing_default(self):
        scheduler, section = self.scheduler()
        del scheduler.verifier_registry["check"]["timeout_seconds"]
        self.assertEqual(self.revalidate(scheduler, section), (True, None))
        scheduler.repo.execute.assert_called_once_with(["reviewed-fixture"], timeout=30.0)

    def test_nonpositive_verifier_budget_stops_before_launch(self):
        for budget in (0, -1):
            with self.subTest(budget=budget):
                scheduler, section = self.scheduler(budget)
                with self.assertRaises(Stop) as raised:
                    self.revalidate(scheduler, section)
                self.assertEqual(raised.exception.reason, "verifier_timeout")
                scheduler.repo.execute.assert_not_called()
                scheduler._persist_runtime.assert_called_once()
                scheduler._persist.assert_not_called()

    def test_expired_task_budget_stops_before_snapshot_or_launch(self):
        scheduler, section = self.scheduler()
        scheduler.check_task_budget.side_effect = Stop("BUDGET_EXHAUSTED", "max_total_runtime", "expired")
        with patch.object(scheduler_module, "snapshot") as snapshot:
            with self.assertRaises(Stop):
                scheduler.revalidate_milestone(section)
            snapshot.assert_not_called()
        scheduler.repo.execute.assert_not_called()

    def test_budget_expires_between_guard_and_remaining_read(self):
        for remaining in (0, -1):
            with self.subTest(remaining=remaining):
                scheduler, section = self.scheduler(remaining=remaining)
                with self.assertRaises(Stop) as raised:
                    self.revalidate(scheduler, section)
                self.assertEqual(raised.exception.reason, "revalidation_budget")
                scheduler.repo.execute.assert_not_called()
                scheduler._persist.assert_not_called()

    def test_verifier_failure_cannot_refresh_milestone(self):
        scheduler, section = self.scheduler()
        scheduler.repo.execute.return_value = {"exit_code": None, "termination": "forced"}
        passed, reason = self.revalidate(scheduler, section)
        self.assertFalse(passed)
        self.assertIn("failed on revalidation", reason)
        scheduler._persist.assert_not_called()

    def test_post_verifier_budget_expiry_cannot_refresh_milestone(self):
        scheduler, section = self.scheduler()
        scheduler.check_task_budget.side_effect = [None, None, Stop("BUDGET_EXHAUSTED", "max_total_runtime", "expired")]
        with self.assertRaises(Stop):
            self.revalidate(scheduler, section)
        scheduler.repo.execute.assert_called_once()
        scheduler._persist.assert_not_called()

    def test_candidate_mutation_still_rejects_revalidation(self):
        scheduler, section = self.scheduler()
        with patch.object(scheduler_module, "snapshot", side_effect=[{"head": "before"}, {"head": "after"}]):
            passed, reason = scheduler.revalidate_milestone(section)
        self.assertFalse(passed)
        self.assertIn("modified", reason)
        scheduler._persist.assert_not_called()

    def test_real_sleeping_verifier_is_terminated_and_cannot_refresh_milestone(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, store, config = setup_test_fixture(root, "milestone-timeout")
            command = ["python", "-m", "unittest", "test_calculator.py"]
            registry = {"check-calc": {"argv": command, "timeout_seconds": 30}}
            manager.create_run(store, repo, "Implement multiply", [command], config,
                               criteria=["multiply"], work_units=[make_spec()],
                               verifier_registry=registry, budgets={"max_runtime_seconds": 60})
            with Supervisor(store) as supervisor:
                repo = manager.ScopedRepository(repo.root, config, durable.DurableLog(store), store)
                executor = ScriptedExecutor()
                executor.set_behavior("unit-1", {"writes": {"calculator.py": "def multiply(a, b):\n    return a * b\n"}})
                scheduler = WorkUnitScheduler(store, repo, verifier_registry=registry,
                                              agents=manager.Agents(None, executor, None, None), supervisor=supervisor)
                section = scheduler.get_section()
                self.assertTrue(scheduler.execute_unit(section["units"]["unit-1"], section)["success"])
                section = scheduler.get_section()
                # Trusted disposable verifier deliberately exceeds the deadline. No socket,
                # model, shell, arbitrary child or external fixture is used.
                (repo.root / "test_calculator.py").write_text(
                    "import time, unittest\n"
                    "class Slow(unittest.TestCase):\n"
                    "    def test_over_budget(self): time.sleep(10)\n", encoding="utf-8")
                registry["check-calc"]["timeout_seconds"] = 1.0
                start = time.monotonic()
                passed, reason = scheduler.revalidate_milestone(section)
                elapsed = time.monotonic() - start
                self.assertFalse(passed)
                self.assertIn("failed on revalidation", reason)
                self.assertLess(elapsed, 8, "Verifier exceeded its one-second limit plus cleanup allowance")
                children = list(store.state["supervision"]["children"].values())
                self.assertTrue(children)
                self.assertTrue(all(child["state"] == "EXITED" for child in children))
                self.assertTrue(any(child["termination"] == "forced" for child in children))
                self.assertTrue(all(winprocess.identity(child["pid"]) != child["identity"] for child in children))
                self.assertFalse(store.state.get("last_verified_checkpoint"))
                self.assertFalse(store.state.get("verified_progress"))
