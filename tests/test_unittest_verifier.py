"""Tests for structured unittest verifier adapter (Phase 3.1B-1)."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from unittest_verifier import (
    run_unittest_verifier,
    SCHEMA_VERSION,
    RUNNER_NAME,
    OUTCOME_PASSED,
    OUTCOME_ASSERTION_FAILURE,
    OUTCOME_EXECUTION_ERROR,
    OUTCOME_DISCOVERY_ERROR,
)


def _verifier_failure_diagnostic(result, async_teardown_reached, observed_exceptions=()):
    """Closed diagnostic projection, under 1024 ASCII bytes; no exception text."""
    import builtins
    import json

    result = result if type(result) is dict else {}
    exceptions = {name for name, cls in vars(builtins).items()
                  if isinstance(cls, type) and issubclass(cls, BaseException)}
    expected_id = "test_unittest_verifier.Fixtures.AsyncTearDownFailure.test_async_body"
    ids = result.get("failed_test_ids"); ids = ids[:4] if type(ids) is list else []
    observed = observed_exceptions[:4] if type(observed_exceptions) in (list, tuple) else ()
    safe_ids = []
    categories = [value for value in observed if type(value) is str and value in exceptions]
    for value in ids:
        category = value.removeprefix("execution_aborted: ") if type(value) is str else ""
        if category in exceptions:
            categories.append(category); safe_ids.append("execution_aborted: " + category)
        else:
            safe_ids.append(expected_id if type(value) is str and value == expected_id else "[REDACTED]")
    kind = result.get("outcome_kind")
    value = dict(outcome_kind=kind if type(kind) is str and kind in {"passed", "assertion_failure", "execution_error", "discovery_error"} else None,
        failed_test_ids=safe_ids, original_exception_categories=categories,
        async_teardown_reached=async_teardown_reached is True)
    for key in ("failure_count", "error_count", "tests_run"):
        count = result.get(key); value[key] = count if type(count) is int and 0 <= count <= 1000000 else None
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return encoded if len(encoded) <= 1024 else '{"diagnostic_truncated":true}'


class Fixtures:
    """Isolated unittest fixtures used by the adapter tests."""

    class Passing(unittest.TestCase):
        def test_pass_alpha(self):
            self.assertTrue(True)

        def test_pass_beta(self):
            self.assertEqual(2 + 2, 4)

    class AssertionFailureBody(unittest.TestCase):
        def test_pass(self):
            self.assertTrue(True)

        def test_assertion_failure_in_body(self):
            self.assertEqual(1, 2, "intentional test body assertion failure")

    class RuntimeErrorBody(unittest.TestCase):
        def test_pass(self):
            self.assertTrue(True)

        def test_runtime_error_in_body(self):
            raise RuntimeError("simulated runtime exception in test body")

    class AssertionFailureSetUp(unittest.TestCase):
        def setUp(self):
            self.assertEqual(10, 20, "setup assertion failure")

        def test_never_reached(self):
            pass

    class AssertionFailureTearDown(unittest.TestCase):
        def tearDown(self):
            self.assertEqual("expected", "actual", "teardown assertion failure")

        def test_runs_cleanly(self):
            self.assertTrue(True)

    class AssertionFailureCleanup(unittest.TestCase):
        def setUp(self):
            self.addCleanup(self._failing_cleanup)

        def _failing_cleanup(self):
            self.assertEqual("clean_a", "clean_b", "cleanup assertion failure")

        def test_body_passes(self):
            self.assertTrue(True)

    class ClassFixtureFailure(unittest.TestCase):
        @classmethod
        def setUpClass(cls):
            raise RuntimeError("class setup failed")

        def test_unreachable(self):
            pass

    class MixedFailureAndError(unittest.TestCase):
        def test_body_assertion(self):
            self.assertEqual(1, 99)

        def test_body_error(self):
            raise ValueError("boom")

    class SubtestPassing(unittest.TestCase):
        def test_with_passing_subtests(self):
            for i in range(3):
                with self.subTest(i=i):
                    self.assertEqual(i, i)

    class SubtestFailing(unittest.TestCase):
        def test_with_failing_subtest(self):
            for i in range(2):
                with self.subTest(i=i):
                    if i == 1:
                        self.assertEqual(i, 99, "subtest failed")

    class SkippedOnly(unittest.TestCase):
        @unittest.skip("all tests intentionally skipped in fixture")
        def test_skipped_one(self):
            pass

    class MixedSkippedAndPassing(unittest.TestCase):
        @unittest.skip("optional test skipped")
        def test_skipped(self):
            pass

        def test_active_pass(self):
            self.assertTrue(True)

    class UnexpectedSuccess(unittest.TestCase):
        @unittest.expectedFailure
        def test_unexpectedly_passing(self):
            self.assertTrue(True)

    class ManyFailures(unittest.TestCase):
        """Fixture producing 12 test body failures to verify bounding to 10."""
        def test_f01(self): self.fail("01")
        def test_f02(self): self.fail("02")
        def test_f03(self): self.fail("03")
        def test_f04(self): self.fail("04")
        def test_f05(self): self.fail("05")
        def test_f06(self): self.fail("06")
        def test_f07(self): self.fail("07")
        def test_f08(self): self.fail("08")
        def test_f09(self): self.fail("09")
        def test_f10(self): self.fail("10")
        def test_f11(self): self.fail("11")
        def test_f12(self): self.fail("12")

    # Regression fixture 1: Async tearDown failure
    class AsyncTearDownFailure(unittest.IsolatedAsyncioTestCase):
        async def test_async_body(self):
            self.assertTrue(True)

        async def asyncTearDown(self):
            self.assertEqual("clean_state", "dirty_state", "async teardown assertion failure")

    # Regression fixture 2: Duplicate test ID with different phase failures
    class DuplicateTestIdFixture(unittest.TestCase):
        invocation_count = 0

        def setUp(self):
            Fixtures.DuplicateTestIdFixture.invocation_count += 1
            if Fixtures.DuplicateTestIdFixture.invocation_count == 1:
                self.assertEqual(1, 2, "first execution setup failure")

        def test_duplicated(self):
            self.assertEqual(3, 4, "second execution body failure")

    # Regression fixture 3: Duplicate test ID where one execution passes and another fails
    class DuplicatePassAndFail(unittest.TestCase):
        invocation_count = 0

        def test_duplicated(self):
            Fixtures.DuplicatePassAndFail.invocation_count += 1
            if Fixtures.DuplicatePassAndFail.invocation_count == 2:
                self.assertEqual(1, 2, "second invocation fails")

    # Regression fixture 4: Partial execution suite (executes fewer tests than discovered)
    class PartialExecutionSuite(unittest.TestSuite):
        def run(self, result, debug=False):
            tests = list(self)
            if tests:
                tests[0](result)
            return result

    # Regression fixture 5: Extra execution suite (declares 1 test discovered, but executes 2)
    class ExtraExecutionSuite(unittest.TestSuite):
        def countTestCases(self):
            return 1


class TestUnittestVerifierAdapter(unittest.TestCase):
    """Authoritative test suite for the structured unittest verifier adapter."""

    def _assert_schema_valid(self, res: dict):
        self.assertIsInstance(res, dict)
        self.assertEqual(res["schema_version"], SCHEMA_VERSION)
        self.assertEqual(res["runner"], RUNNER_NAME)
        self.assertIsInstance(res["tests_run"], int)
        self.assertIsInstance(res["failure_count"], int)
        self.assertIsInstance(res["error_count"], int)
        self.assertIsInstance(res["skipped_count"], int)
        self.assertIsInstance(res["outcome_kind"], str)
        self.assertIn(
            res["outcome_kind"],
            (OUTCOME_PASSED, OUTCOME_ASSERTION_FAILURE, OUTCOME_EXECUTION_ERROR, OUTCOME_DISCOVERY_ERROR),
        )
        self.assertIsInstance(res["failed_test_ids"], list)
        self.assertLessEqual(len(res["failed_test_ids"]), 10)
        for tid in res["failed_test_ids"]:
            self.assertIsInstance(tid, str)

    # 1. Passing test
    def test_passing_test_fixture(self):
        res = run_unittest_verifier(Fixtures.Passing)
        self._assert_schema_valid(res)
        self.assertEqual(res["outcome_kind"], OUTCOME_PASSED)
        self.assertEqual(res["tests_run"], 2)
        self.assertEqual(res["failure_count"], 0)
        self.assertEqual(res["error_count"], 0)
        self.assertEqual(res["failed_test_ids"], [])

    # 2. Assertion failure in test body
    def test_assertion_failure_in_test_body(self):
        res = run_unittest_verifier(Fixtures.AssertionFailureBody)
        self._assert_schema_valid(res)
        self.assertEqual(res["outcome_kind"], OUTCOME_ASSERTION_FAILURE)
        self.assertEqual(res["tests_run"], 2)
        self.assertEqual(res["failure_count"], 1)
        self.assertEqual(res["error_count"], 0)
        self.assertEqual(len(res["failed_test_ids"]), 1)
        self.assertTrue(any("test_assertion_failure_in_body" in tid for tid in res["failed_test_ids"]))

    # 3. Import / discovery error
    def test_import_or_discovery_error(self):
        res = run_unittest_verifier("non_existent_module_xyz_12345")
        self._assert_schema_valid(res)
        self.assertEqual(res["outcome_kind"], OUTCOME_DISCOVERY_ERROR)
        self.assertEqual(res["tests_run"], 0)
        self.assertEqual(res["failure_count"], 0)
        self.assertGreaterEqual(res["error_count"], 1)

    def test_syntax_error_file_discovery_error(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            bad_file = tdp / "test_broken_syntax.py"
            bad_file.write_text("def def broken syntax !!", encoding="utf-8")
            res = run_unittest_verifier([str(bad_file)], cwd=tdp)
            self._assert_schema_valid(res)
            self.assertEqual(res["outcome_kind"], OUTCOME_DISCOVERY_ERROR)

    # 4. Runtime error in test body
    def test_runtime_error_in_test_body(self):
        res = run_unittest_verifier(Fixtures.RuntimeErrorBody)
        self._assert_schema_valid(res)
        self.assertEqual(res["outcome_kind"], OUTCOME_EXECUTION_ERROR)
        self.assertEqual(res["tests_run"], 2)
        self.assertEqual(res["failure_count"], 0)
        self.assertEqual(res["error_count"], 1)
        self.assertTrue(any("test_runtime_error_in_body" in tid for tid in res["failed_test_ids"]))

    # 5. Assertion failure in setUp
    def test_assertion_failure_in_setup(self):
        res = run_unittest_verifier(Fixtures.AssertionFailureSetUp)
        self._assert_schema_valid(res)
        self.assertEqual(res["outcome_kind"], OUTCOME_EXECUTION_ERROR)
        self.assertEqual(res["tests_run"], 1)
        self.assertEqual(res["failure_count"], 1)
        self.assertEqual(len(res["failed_test_ids"]), 1)

    # 6. Assertion failure in tearDown
    def test_assertion_failure_in_teardown(self):
        res = run_unittest_verifier(Fixtures.AssertionFailureTearDown)
        self._assert_schema_valid(res)
        self.assertEqual(res["outcome_kind"], OUTCOME_EXECUTION_ERROR)
        self.assertEqual(res["tests_run"], 1)
        self.assertEqual(res["failure_count"], 1)
        self.assertEqual(len(res["failed_test_ids"]), 1)

    # Cleanup assertion failure
    def test_assertion_failure_in_cleanup(self):
        res = run_unittest_verifier(Fixtures.AssertionFailureCleanup)
        self._assert_schema_valid(res)
        self.assertEqual(res["outcome_kind"], OUTCOME_EXECUTION_ERROR)
        self.assertEqual(res["tests_run"], 1)
        self.assertEqual(res["failure_count"], 1)

    # Class fixture failure
    def test_class_fixture_failure(self):
        res = run_unittest_verifier(Fixtures.ClassFixtureFailure)
        self._assert_schema_valid(res)
        self.assertEqual(res["outcome_kind"], OUTCOME_EXECUTION_ERROR)
        self.assertGreaterEqual(res["error_count"], 1)

    # 7. Mixed failure and error
    def test_mixed_failure_and_error(self):
        res = run_unittest_verifier(Fixtures.MixedFailureAndError)
        self._assert_schema_valid(res)
        self.assertEqual(res["outcome_kind"], OUTCOME_EXECUTION_ERROR)
        self.assertEqual(res["tests_run"], 2)
        self.assertEqual(res["failure_count"], 1)
        self.assertEqual(res["error_count"], 1)
        self.assertEqual(len(res["failed_test_ids"]), 2)

    # 8. Zero discovered tests
    def test_zero_discovered_tests(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            res = run_unittest_verifier(["discover", "-s", str(tdp), "-p", "test_empty_*.py"], cwd=tdp)
            self._assert_schema_valid(res)
            self.assertEqual(res["outcome_kind"], OUTCOME_DISCOVERY_ERROR)
            self.assertEqual(res["tests_run"], 0)

    # 9. Subtests (conservative handling)
    def test_subtests_passing(self):
        res = run_unittest_verifier(Fixtures.SubtestPassing)
        self._assert_schema_valid(res)
        self.assertEqual(res["outcome_kind"], OUTCOME_PASSED)
        self.assertEqual(res["tests_run"], 1)
        self.assertEqual(res["failure_count"], 0)
        self.assertEqual(res["error_count"], 0)

    def test_subtests_failing_treated_conservatively_as_execution_error(self):
        res = run_unittest_verifier(Fixtures.SubtestFailing)
        self._assert_schema_valid(res)
        self.assertEqual(res["outcome_kind"], OUTCOME_EXECUTION_ERROR)
        self.assertGreaterEqual(len(res["failed_test_ids"]), 1)

    # 10. Skipped tests
    def test_skipped_only_fixture_fails_closed(self):
        res = run_unittest_verifier(Fixtures.SkippedOnly)
        self._assert_schema_valid(res)
        self.assertEqual(res["outcome_kind"], OUTCOME_EXECUTION_ERROR)
        self.assertEqual(res["skipped_count"], 1)

    def test_mixed_skipped_and_passing(self):
        res = run_unittest_verifier(Fixtures.MixedSkippedAndPassing)
        self._assert_schema_valid(res)
        self.assertEqual(res["outcome_kind"], OUTCOME_PASSED)
        self.assertEqual(res["tests_run"], 2)
        self.assertEqual(res["skipped_count"], 1)
        self.assertEqual(res["failure_count"], 0)
        self.assertEqual(res["error_count"], 0)

    # Unexpected success
    def test_unexpected_success_treated_as_execution_error(self):
        res = run_unittest_verifier(Fixtures.UnexpectedSuccess)
        self._assert_schema_valid(res)
        self.assertEqual(res["outcome_kind"], OUTCOME_EXECUTION_ERROR)

    # Bounded failed_test_ids
    def test_failed_test_ids_bounded_to_10(self):
        res = run_unittest_verifier(Fixtures.ManyFailures)
        self._assert_schema_valid(res)
        self.assertEqual(res["outcome_kind"], OUTCOME_ASSERTION_FAILURE)
        self.assertEqual(res["failure_count"], 12)
        self.assertEqual(len(res["failed_test_ids"]), 10)

    # Argv parsing support
    def test_argv_with_python_m_unittest_prefix(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            test_file = tdp / "test_calc.py"
            test_file.write_text(
                "import unittest\n"
                "class TC(unittest.TestCase):\n"
                "    def test_one(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            argv = ["python", "-m", "unittest", "test_calc.py"]
            res = run_unittest_verifier(argv, cwd=tdp)
            self._assert_schema_valid(res)
            self.assertEqual(res["outcome_kind"], OUTCOME_PASSED)
            self.assertEqual(res["tests_run"], 1)

    # Fail closed on invalid targets input
    def test_invalid_targets_input(self):
        for invalid in (None, [], 123, False):
            res = run_unittest_verifier(invalid)
            self._assert_schema_valid(res)
            self.assertEqual(res["outcome_kind"], OUTCOME_DISCOVERY_ERROR)

    # -------------------------------------------------------------------------
    # Regression tests for reproduced security defects (Phase 3.1B-1)
    # -------------------------------------------------------------------------

    # Regression 1: Async tearDown assertion failure MUST return execution_error
    def test_async_teardown_failure_returns_execution_error(self):
        from unittest.mock import patch

        teardown_reached = False
        exception_categories = []
        original = Fixtures.AsyncTearDownFailure.asyncTearDown
        original_setup = Fixtures.AsyncTearDownFailure._callSetUp
        def observed_setup(case):
            try:
                return original_setup(case)
            except BaseException as exc:
                exception_categories.append(type(exc).__name__)
                raise
        async def observed_teardown(case):
            nonlocal teardown_reached
            teardown_reached = True
            try:
                return await original(case)
            except BaseException as exc:
                exception_categories.append(type(exc).__name__)
                raise
        with patch.object(Fixtures.AsyncTearDownFailure, "asyncTearDown", observed_teardown), \
                patch.object(Fixtures.AsyncTearDownFailure, "_callSetUp", observed_setup):
            res = run_unittest_verifier(Fixtures.AsyncTearDownFailure)
        self._assert_schema_valid(res)
        self.assertEqual(res["outcome_kind"], OUTCOME_EXECUTION_ERROR)
        self.assertEqual(res["tests_run"], 1)
        self.assertEqual(res["failure_count"], 1,
                         msg=_verifier_failure_diagnostic(res, teardown_reached, exception_categories))
        self.assertEqual(len(res["failed_test_ids"]), 1)

    # Regression 2: Duplicate test ID executing across different phases MUST return execution_error
    def test_duplicate_test_id_setup_and_body_failure_returns_execution_error(self):
        Fixtures.DuplicateTestIdFixture.invocation_count = 0
        s = unittest.TestSuite([
            Fixtures.DuplicateTestIdFixture("test_duplicated"),
            Fixtures.DuplicateTestIdFixture("test_duplicated"),
        ])
        res = run_unittest_verifier(s)
        self._assert_schema_valid(res)
        self.assertEqual(res["outcome_kind"], OUTCOME_EXECUTION_ERROR)
        self.assertEqual(res["failure_count"], 2)

    # Regression 3: Unsupported, malformed, or garbage CLI arguments MUST be rejected
    def test_unsupported_cli_arguments_rejected(self):
        # Discover with garbage flag
        res1 = run_unittest_verifier(["discover", "--garbage-flag"])
        self._assert_schema_valid(res1)
        self.assertEqual(res1["outcome_kind"], OUTCOME_DISCOVERY_ERROR)

        # Discover with garbage flag alongside valid arguments
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            res2 = run_unittest_verifier(["discover", "-s", str(tdp), "--garbage-flag", "-p", "test_*.py"], cwd=tdp)
            self._assert_schema_valid(res2)
            self.assertEqual(res2["outcome_kind"], OUTCOME_DISCOVERY_ERROR)

        # Non-discover target with garbage flag
        res3 = run_unittest_verifier(["--garbage-flag", "test_calc.py"])
        self._assert_schema_valid(res3)
        self.assertEqual(res3["outcome_kind"], OUTCOME_DISCOVERY_ERROR)

        # Missing argument for flag requiring value (-s)
        res4 = run_unittest_verifier(["discover", "-s"])
        self._assert_schema_valid(res4)
        self.assertEqual(res4["outcome_kind"], OUTCOME_DISCOVERY_ERROR)

    # -------------------------------------------------------------------------
    # Final acceptance regressions (Phase 3.1B-1 Final)
    # -------------------------------------------------------------------------

    # 1. Reject ignored unittest options (-k, -f, -b, -c, --durations) with discovery_error
    def test_rejected_ignored_options_k_f_b_c_durations(self):
        unsupported_discover_flags = [
            ["discover", "-k", "pattern"],
            ["discover", "-f"],
            ["discover", "-b"],
            ["discover", "-c"],
            ["discover", "--durations", "5"],
            ["discover", "--failfast"],
            ["discover", "--buffer"],
            ["discover", "--catch"],
            ["discover", "--locals"],
        ]
        for flags in unsupported_discover_flags:
            res = run_unittest_verifier(flags)
            self._assert_schema_valid(res)
            self.assertEqual(
                res["outcome_kind"],
                OUTCOME_DISCOVERY_ERROR,
                f"Expected discovery_error for unsupported flags: {flags}",
            )

        unsupported_target_flags = [
            ["-k", "pattern", "test_foo.py"],
            ["-f", "test_foo.py"],
            ["-b", "test_foo.py"],
            ["-c", "test_foo.py"],
            ["--durations", "5", "test_foo.py"],
        ]
        for flags in unsupported_target_flags:
            res = run_unittest_verifier(flags)
            self._assert_schema_valid(res)
            self.assertEqual(
                res["outcome_kind"],
                OUTCOME_DISCOVERY_ERROR,
                f"Expected discovery_error for unsupported flags: {flags}",
            )

    # 2. Reject incomplete test execution (fewer tests execute than were discovered)
    def test_incomplete_test_execution_returns_execution_error(self):
        # 2 tests discovered, but suite runner only executes 1
        suite = Fixtures.PartialExecutionSuite([
            Fixtures.Passing("test_pass_alpha"),
            Fixtures.Passing("test_pass_beta"),
        ])
        self.assertEqual(suite.countTestCases(), 2)

        res = run_unittest_verifier(suite)
        self._assert_schema_valid(res)
        self.assertEqual(res["outcome_kind"], OUTCOME_EXECUTION_ERROR)
        self.assertEqual(res["tests_run"], 1)

    # 3. Reject duplicate TestCase IDs across entire suite, including one pass and one fail
    def test_duplicate_test_case_id_one_pass_one_fail_returns_execution_error(self):
        Fixtures.DuplicatePassAndFail.invocation_count = 0
        suite = unittest.TestSuite([
            Fixtures.DuplicatePassAndFail("test_duplicated"),
            Fixtures.DuplicatePassAndFail("test_duplicated"),
        ])
        self.assertEqual(suite.countTestCases(), 2)

        res = run_unittest_verifier(suite)
        self._assert_schema_valid(res)
        self.assertEqual(res["outcome_kind"], OUTCOME_EXECUTION_ERROR)

    def test_duplicate_test_case_id_both_passing_returns_execution_error(self):
        suite = unittest.TestSuite([
            Fixtures.Passing("test_pass_alpha"),
            Fixtures.Passing("test_pass_alpha"),
        ])
        self.assertEqual(suite.countTestCases(), 2)

        res = run_unittest_verifier(suite)
        self._assert_schema_valid(res)
        self.assertEqual(res["outcome_kind"], OUTCOME_EXECUTION_ERROR)

    # 4. Final guard regression: 1 test discovered, 2 actually executed, one assertion failure
    def test_mismatched_test_execution_declared_fewer_than_executed_returns_execution_error(self):
        suite = Fixtures.ExtraExecutionSuite([
            Fixtures.Passing("test_pass_alpha"),
            Fixtures.AssertionFailureBody("test_assertion_failure_in_body"),
        ])
        self.assertEqual(suite.countTestCases(), 1)

        res = run_unittest_verifier(suite)
        self._assert_schema_valid(res)
        self.assertEqual(res["outcome_kind"], OUTCOME_EXECUTION_ERROR)
        self.assertEqual(res["tests_run"], 2)


if __name__ == "__main__":
    unittest.main()
