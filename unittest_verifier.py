"""Structured Python unittest verifier adapter for TRIGLAV.

Provides a standalone, machine-readable unittest runner producing
authoritative deterministic test outcome records.

Schema v1 contracts:
- schema_version = 1
- runner = "unittest"
- tests_run
- failure_count
- error_count
- skipped_count
- outcome_kind: "passed" | "assertion_failure" | "execution_error" | "discovery_error"
- failed_test_ids: list[str] (bounded to 10)
"""
from __future__ import annotations

import functools
import inspect
import json
import os
from pathlib import Path
import sys
from typing import Any
import unittest

SCHEMA_VERSION = 1
RUNNER_NAME = "unittest"
MAX_FAILED_TEST_IDS = 10

OUTCOME_PASSED = "passed"
OUTCOME_ASSERTION_FAILURE = "assertion_failure"
OUTCOME_EXECUTION_ERROR = "execution_error"
OUTCOME_DISCOVERY_ERROR = "discovery_error"


class _PhaseTracker:
    def __init__(self) -> None:
        self.active_phase: str | None = None
        self.last_exception_phase: str | None = None


class StructuredTestResult(unittest.TestResult):
    """Custom TestResult tracking precise per-failure events and errors."""

    def __init__(self, tracker: _PhaseTracker) -> None:
        super().__init__()
        self.tracker = tracker
        self.executed_test_ids: list[str] = []
        self.seen_executed_tids: set[str] = set()
        self.has_duplicate_executed_ids: bool = False
        self.failure_events: list[dict[str, Any]] = []
        self.error_events: list[dict[str, Any]] = []
        self.discovery_errors: list[tuple[str, Any]] = []
        self.subtest_failures: list[tuple[str, Any]] = []
        self.has_unexpected_success = False

    def _get_test_id(self, test: Any) -> str:
        if hasattr(test, "id") and callable(test.id):
            try:
                return str(test.id())
            except Exception:
                pass
        return str(test)

    def _is_discovery_failure(self, test: Any) -> bool:
        cls_name = getattr(test.__class__, "__name__", "")
        if cls_name == "_FailedTest":
            return True
        tid = self._get_test_id(test)
        return "unittest.loader._FailedTest" in tid

    def startTest(self, test: Any) -> None:
        super().startTest(test)
        tid = self._get_test_id(test)
        self.executed_test_ids.append(tid)
        if tid in self.seen_executed_tids:
            self.has_duplicate_executed_ids = True
        self.seen_executed_tids.add(tid)

    def addFailure(self, test: Any, err: Any) -> None:
        super().addFailure(test, err)
        tid = self._get_test_id(test)
        phase = self.tracker.last_exception_phase or self.tracker.active_phase or "fixture"
        self.tracker.last_exception_phase = None
        self.tracker.active_phase = None
        self.failure_events.append({"test": test, "test_id": tid, "phase": phase, "err": err})

    def addError(self, test: Any, err: Any) -> None:
        super().addError(test, err)
        tid = self._get_test_id(test)
        if self._is_discovery_failure(test):
            self.discovery_errors.append((tid, err))
        phase = self.tracker.last_exception_phase or self.tracker.active_phase or "fixture"
        self.tracker.last_exception_phase = None
        self.tracker.active_phase = None
        self.error_events.append({"test": test, "test_id": tid, "phase": phase, "err": err})

    def addSubTest(self, test: Any, subtest: Any, err: Any) -> None:
        super().addSubTest(test, subtest, err)
        if err is not None:
            sub_id = self._get_test_id(subtest)
            self.subtest_failures.append((sub_id, err))

    def addUnexpectedSuccess(self, test: Any) -> None:
        super().addUnexpectedSuccess(test)
        self.has_unexpected_success = True


def _instrument_test_case(test: unittest.TestCase, tracker: _PhaseTracker) -> None:
    """Instrument a single TestCase instance with phase tracking hooks."""
    if getattr(test, "_triglav_instrumented", False):
        return
    test._triglav_instrumented = True

    method_name = getattr(test, "_testMethodName", None)
    if not method_name or not hasattr(test, method_name):
        tid = getattr(test, "id", lambda: "")()
        method_name = tid.split(".")[-1] if tid else None

    # Instrument setUp
    orig_setUp = getattr(test, "setUp", None)
    if callable(orig_setUp):
        @functools.wraps(orig_setUp)
        def wrapped_setUp(*args: Any, **kwargs: Any) -> Any:
            tracker.active_phase = "setUp"
            tracker.last_exception_phase = None
            try:
                ret = orig_setUp(*args, **kwargs)
                tracker.active_phase = None
                return ret
            except:
                tracker.last_exception_phase = "setUp"
                tracker.active_phase = None
                raise

        test.setUp = wrapped_setUp

    # Instrument asyncSetUp (IsolatedAsyncioTestCase)
    orig_asyncSetUp = getattr(test, "asyncSetUp", None)
    if callable(orig_asyncSetUp):
        @functools.wraps(orig_asyncSetUp)
        async def wrapped_asyncSetUp(*args: Any, **kwargs: Any) -> Any:
            tracker.active_phase = "setUp"
            tracker.last_exception_phase = None
            try:
                ret = await orig_asyncSetUp(*args, **kwargs)
                tracker.active_phase = None
                return ret
            except:
                tracker.last_exception_phase = "setUp"
                tracker.active_phase = None
                raise

        test.asyncSetUp = wrapped_asyncSetUp

    # Instrument test method
    if method_name and hasattr(test, method_name):
        orig_method = getattr(test, method_name)
        if callable(orig_method):
            if inspect.iscoroutinefunction(orig_method):
                @functools.wraps(orig_method)
                async def wrapped_async_method(*args: Any, **kwargs: Any) -> Any:
                    tracker.active_phase = "test_body"
                    tracker.last_exception_phase = None
                    try:
                        ret = await orig_method(*args, **kwargs)
                        tracker.active_phase = None
                        return ret
                    except:
                        tracker.last_exception_phase = "test_body"
                        tracker.active_phase = None
                        raise

                setattr(test, method_name, wrapped_async_method)
            else:
                @functools.wraps(orig_method)
                def wrapped_method(*args: Any, **kwargs: Any) -> Any:
                    tracker.active_phase = "test_body"
                    tracker.last_exception_phase = None
                    try:
                        ret = orig_method(*args, **kwargs)
                        tracker.active_phase = None
                        return ret
                    except:
                        tracker.last_exception_phase = "test_body"
                        tracker.active_phase = None
                        raise

                setattr(test, method_name, wrapped_method)

    # Instrument tearDown
    orig_tearDown = getattr(test, "tearDown", None)
    if callable(orig_tearDown):
        @functools.wraps(orig_tearDown)
        def wrapped_tearDown(*args: Any, **kwargs: Any) -> Any:
            tracker.active_phase = "tearDown"
            tracker.last_exception_phase = None
            try:
                ret = orig_tearDown(*args, **kwargs)
                tracker.active_phase = None
                return ret
            except:
                tracker.last_exception_phase = "tearDown"
                tracker.active_phase = None
                raise

        test.tearDown = wrapped_tearDown

    # Instrument asyncTearDown (IsolatedAsyncioTestCase)
    orig_asyncTearDown = getattr(test, "asyncTearDown", None)
    if callable(orig_asyncTearDown):
        @functools.wraps(orig_asyncTearDown)
        async def wrapped_asyncTearDown(*args: Any, **kwargs: Any) -> Any:
            tracker.active_phase = "tearDown"
            tracker.last_exception_phase = None
            try:
                ret = await orig_asyncTearDown(*args, **kwargs)
                tracker.active_phase = None
                return ret
            except:
                tracker.last_exception_phase = "tearDown"
                tracker.active_phase = None
                raise

        test.asyncTearDown = wrapped_asyncTearDown

    # Instrument doCleanups
    orig_doCleanups = getattr(test, "doCleanups", None)
    if callable(orig_doCleanups):
        @functools.wraps(orig_doCleanups)
        def wrapped_doCleanups(*args: Any, **kwargs: Any) -> Any:
            tracker.active_phase = "cleanup"
            try:
                ret = orig_doCleanups(*args, **kwargs)
                tracker.active_phase = None
                return ret
            except:
                tracker.last_exception_phase = "cleanup"
                tracker.active_phase = None
                raise
            finally:
                tracker.active_phase = None

        test.doCleanups = wrapped_doCleanups


def _iter_tests(suite_or_test: Any) -> list[unittest.TestCase]:
    cases: list[unittest.TestCase] = []
    if isinstance(suite_or_test, unittest.TestCase):
        cases.append(suite_or_test)
    elif hasattr(suite_or_test, "__iter__"):
        for item in suite_or_test:
            cases.extend(_iter_tests(item))
    return cases


def _normalize_target(name: str, cwd: Path) -> str:
    path = Path(name)
    if not path.is_absolute():
        path = cwd / path
    if path.is_file() and path.suffix.lower() == ".py":
        try:
            rel = path.resolve().relative_to(cwd.resolve())
            return ".".join(rel.with_suffix("").parts)
        except ValueError:
            return str(path)
    return name


def _extract_clean_targets(raw_targets: Any) -> list[str]:
    if isinstance(raw_targets, str):
        targets = [raw_targets]
    elif isinstance(raw_targets, (list, tuple)):
        targets = [str(t) for t in raw_targets]
    else:
        return []

    clean = list(targets)
    if clean and (clean[0] == "python" or clean[0].endswith("python.exe")):
        clean.pop(0)
    if clean and clean[0] == "-m":
        clean.pop(0)
    if clean and clean[0] == "unittest":
        clean.pop(0)
    return clean


def _parse_unittest_args(args: list[str]) -> tuple[str, Any]:
    """Strictly validate and parse unittest arguments.

    Rejects any unconsumed, malformed, or unsupported option (including
    -k, -f, -b, -c, --durations) with an error.

    Returns:
    - ('discover', (start_dir, pattern, top_level_dir)) on valid discover arguments.
    - ('names', list[str]) on valid test target arguments.
    - ('error', err_msg) if any argument is unsupported, malformed, or unconsumed.
    """
    if not args:
        return ("error", "empty arguments")

    if args[0] == "discover":
        idx = 1
        start_dir = "."
        pattern = "test*.py"
        top_level_dir = None
        pos_args: list[str] = []

        while idx < len(args):
            arg = args[idx]
            if arg in ("-v", "--verbose", "-q", "--quiet"):
                idx += 1
            elif arg in ("-s", "--start-directory"):
                if idx + 1 >= len(args) or args[idx + 1].startswith("-"):
                    return ("error", f"missing argument for {arg}")
                start_dir = args[idx + 1]
                idx += 2
            elif arg in ("-p", "--pattern"):
                if idx + 1 >= len(args) or args[idx + 1].startswith("-"):
                    return ("error", f"missing argument for {arg}")
                pattern = args[idx + 1]
                idx += 2
            elif arg in ("-t", "--top-level-directory"):
                if idx + 1 >= len(args) or args[idx + 1].startswith("-"):
                    return ("error", f"missing argument for {arg}")
                top_level_dir = args[idx + 1]
                idx += 2
            elif arg.startswith("-"):
                return ("error", f"unsupported or unrecognized option: {arg}")
            else:
                pos_args.append(arg)
                idx += 1

        if len(pos_args) > 3:
            return ("error", f"too many positional arguments for discover: {pos_args}")
        if len(pos_args) >= 1:
            start_dir = pos_args[0]
        if len(pos_args) >= 2:
            pattern = pos_args[1]
        if len(pos_args) >= 3:
            top_level_dir = pos_args[2]

        return ("discover", (start_dir, pattern, top_level_dir))
    else:
        idx = 0
        test_names: list[str] = []
        while idx < len(args):
            arg = args[idx]
            if arg in ("-v", "--verbose", "-q", "--quiet"):
                idx += 1
            elif arg.startswith("-"):
                return ("error", f"unsupported or unrecognized option: {arg}")
            else:
                test_names.append(arg)
                idx += 1

        if not test_names:
            return ("error", "no test targets specified")
        return ("names", test_names)


def _classify_outcome(
    result: StructuredTestResult,
    total_discovered: int,
    has_duplicate_suite_ids: bool = False,
    suite_duplicate_ids: list[str] | None = None,
) -> tuple[str, int, int, int, int, list[str]]:
    tests_run = max(0, result.testsRun - len(result.discovery_errors))
    failure_count = len(result.failures)
    error_count = len(result.errors)
    skipped_count = len(result.skipped)

    failed_ids: list[str] = []
    seen_failure_tids: set[str] = set()
    has_duplicate_failed_tids = False

    for event in result.failure_events:
        tid = event["test_id"]
        if tid in seen_failure_tids:
            has_duplicate_failed_tids = True
        seen_failure_tids.add(tid)
        if tid not in failed_ids:
            failed_ids.append(tid)

    for event in result.error_events:
        tid = event["test_id"]
        if tid in seen_failure_tids:
            has_duplicate_failed_tids = True
        seen_failure_tids.add(tid)
        if tid not in failed_ids:
            failed_ids.append(tid)

    for sub_id, _ in result.subtest_failures:
        if sub_id not in failed_ids:
            failed_ids.append(sub_id)

    unexp_successes = getattr(result, "unexpectedSuccesses", [])
    if unexp_successes or result.has_unexpected_success:
        error_count = max(error_count, len(unexp_successes) or 1)
        for test in unexp_successes:
            tid = result._get_test_id(test)
            if tid not in failed_ids:
                failed_ids.append(tid)

    if suite_duplicate_ids:
        for tid in suite_duplicate_ids:
            if tid not in failed_ids:
                failed_ids.append(tid)

    bounded_failed_ids = failed_ids[:MAX_FAILED_TEST_IDS]

    # Discovery error takes precedence
    if result.discovery_errors or total_discovered == 0:
        return (
            OUTCOME_DISCOVERY_ERROR,
            tests_run,
            failure_count,
            max(error_count, 1 if total_discovered == 0 else len(result.discovery_errors)),
            skipped_count,
            bounded_failed_ids,
        )

    # Reject duplicate TestCase IDs across entire suite or executions
    if has_duplicate_suite_ids or result.has_duplicate_executed_ids or has_duplicate_failed_tids:
        return (
            OUTCOME_EXECUTION_ERROR,
            tests_run,
            failure_count,
            max(error_count, 1),
            skipped_count,
            bounded_failed_ids,
        )

    # Reject mismatched test execution (fewer or more tests execute than were discovered)
    if tests_run != total_discovered:
        return (
            OUTCOME_EXECUTION_ERROR,
            tests_run,
            failure_count,
            max(error_count, 1),
            skipped_count,
            bounded_failed_ids,
        )

    # Zero tests executed (or all tests skipped) fails closed as execution_error
    if tests_run == 0 or (tests_run <= skipped_count and failure_count == 0 and error_count == 0):
        return (
            OUTCOME_EXECUTION_ERROR,
            tests_run,
            failure_count,
            error_count,
            skipped_count,
            bounded_failed_ids,
        )

    # Non-assertion failures: runtime errors, unexpected successes, or subtest failures
    if error_count > 0 or result.has_unexpected_success or result.subtest_failures:
        return (
            OUTCOME_EXECUTION_ERROR,
            tests_run,
            failure_count,
            error_count,
            skipped_count,
            bounded_failed_ids,
        )

    # Failures present: check if ALL failure events were strictly in test_body
    if failure_count > 0:
        all_in_body = True
        for event in result.failure_events:
            if event.get("phase") != "test_body":
                all_in_body = False
                break
        if all_in_body:
            return (
                OUTCOME_ASSERTION_FAILURE,
                tests_run,
                failure_count,
                error_count,
                skipped_count,
                bounded_failed_ids,
            )
        else:
            return (
                OUTCOME_EXECUTION_ERROR,
                tests_run,
                failure_count,
                error_count,
                skipped_count,
                bounded_failed_ids,
            )

    # Clean pass
    return (
        OUTCOME_PASSED,
        tests_run,
        failure_count,
        error_count,
        skipped_count,
        bounded_failed_ids,
    )


def run_unittest_verifier(
    targets: str | list[str] | type[unittest.TestCase] | unittest.TestSuite | None = None,
    *,
    cwd: str | Path | None = None,
) -> dict[str, Any]:
    """Execute approved unittest targets and return structured outcome record.

    Guarantees:
    - Never scrapes stdout/stderr keywords.
    - Uses real unittest TestResult objects and per-failure event phase tracking.
    - Classifies assertion_failure ONLY when tests ran and failures occurred strictly in test-body.
    - Fails closed on discovery errors, setup/teardown errors, runtime exceptions,
      subtest failures, unsupported CLI arguments, duplicate failure events, and zero executed tests.
    """
    cwd_path = Path(cwd).resolve() if cwd else Path.cwd().resolve()
    cwd_str = str(cwd_path)
    if cwd_str not in sys.path:
        sys.path.insert(0, cwd_str)

    loader = unittest.defaultTestLoader
    tracker = _PhaseTracker()
    suite: unittest.TestSuite | None = None
    total_discovered = 0

    try:
        if targets is None:
            return {
                "schema_version": SCHEMA_VERSION,
                "runner": RUNNER_NAME,
                "tests_run": 0,
                "failure_count": 0,
                "error_count": 1,
                "skipped_count": 0,
                "outcome_kind": OUTCOME_DISCOVERY_ERROR,
                "failed_test_ids": [],
            }

        if isinstance(targets, type) and issubclass(targets, unittest.TestCase):
            suite = loader.loadTestsFromTestCase(targets)
        elif isinstance(targets, unittest.TestSuite):
            suite = targets
        elif isinstance(targets, unittest.TestCase):
            suite = unittest.TestSuite([targets])
        else:
            clean = _extract_clean_targets(targets)
            if not clean:
                return {
                    "schema_version": SCHEMA_VERSION,
                    "runner": RUNNER_NAME,
                    "tests_run": 0,
                    "failure_count": 0,
                    "error_count": 1,
                    "skipped_count": 0,
                    "outcome_kind": OUTCOME_DISCOVERY_ERROR,
                    "failed_test_ids": [],
                }

            cmd_kind, parsed_data = _parse_unittest_args(clean)
            if cmd_kind == "error":
                return {
                    "schema_version": SCHEMA_VERSION,
                    "runner": RUNNER_NAME,
                    "tests_run": 0,
                    "failure_count": 0,
                    "error_count": 1,
                    "skipped_count": 0,
                    "outcome_kind": OUTCOME_DISCOVERY_ERROR,
                    "failed_test_ids": [f"unsupported_arguments: {parsed_data}"],
                }

            if cmd_kind == "discover":
                start_dir, pattern, top_level_dir = parsed_data
                suite = loader.discover(start_dir=start_dir, pattern=pattern, top_level_dir=top_level_dir)
            else:
                test_names = parsed_data
                norm_names = [_normalize_target(t, cwd_path) for t in test_names]
                suite = loader.loadTestsFromNames(norm_names)

        total_discovered = suite.countTestCases()
    except Exception as exc:
        return {
            "schema_version": SCHEMA_VERSION,
            "runner": RUNNER_NAME,
            "tests_run": 0,
            "failure_count": 0,
            "error_count": 1,
            "skipped_count": 0,
            "outcome_kind": OUTCOME_DISCOVERY_ERROR,
            "failed_test_ids": [f"discovery_failure: {exc.__class__.__name__}"],
        }

    # Check for duplicate test IDs across the entire suite
    suite_test_ids: list[str] = []
    suite_duplicate_ids: list[str] = []
    seen_suite_tids: set[str] = set()
    for test in _iter_tests(suite):
        tid = test.id() if hasattr(test, "id") and callable(test.id) else str(test)
        if tid in seen_suite_tids:
            if tid not in suite_duplicate_ids:
                suite_duplicate_ids.append(tid)
        seen_suite_tids.add(tid)
        suite_test_ids.append(tid)
    has_duplicate_suite_ids = len(suite_duplicate_ids) > 0

    # Instrument test cases for phase tracking
    for test in _iter_tests(suite):
        _instrument_test_case(test, tracker)

    result = StructuredTestResult(tracker)
    try:
        suite.run(result)
    except Exception as exc:
        return {
            "schema_version": SCHEMA_VERSION,
            "runner": RUNNER_NAME,
            "tests_run": result.testsRun,
            "failure_count": len(result.failures),
            "error_count": max(1, len(result.errors)),
            "skipped_count": len(result.skipped),
            "outcome_kind": OUTCOME_EXECUTION_ERROR,
            "failed_test_ids": [f"execution_aborted: {exc.__class__.__name__}"],
        }

    kind, tests_run, fail_cnt, err_cnt, skip_cnt, failed_ids = _classify_outcome(
        result,
        total_discovered=total_discovered,
        has_duplicate_suite_ids=has_duplicate_suite_ids,
        suite_duplicate_ids=suite_duplicate_ids,
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "runner": RUNNER_NAME,
        "tests_run": tests_run,
        "failure_count": fail_cnt,
        "error_count": err_cnt,
        "skipped_count": skip_cnt,
        "outcome_kind": kind,
        "failed_test_ids": failed_ids,
    }


if __name__ == "__main__":
    outcome = run_unittest_verifier(sys.argv[1:])
    print(json.dumps(outcome, indent=2))
    sys.exit(0 if outcome["outcome_kind"] == OUTCOME_PASSED else 1)
