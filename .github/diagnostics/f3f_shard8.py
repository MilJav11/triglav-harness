"""One reviewed F3F diagnostic, never qualification or a hostile-code sandbox.

Hosted entry only. The workflow pins this file before executing it. Existing
manifest bytes, qualification IDs, CAPSULE, guards and test deadlines are reused.
Private child logs/WAL stay outside the artifact directory. Only evidence.json
(at most 4096 ASCII bytes) is published; PASS means non-reproduction.

One-shot conditions are not a durable replay ledger: deleting/recreating the
diagnostic branch can create another attempt-1 push run. Manager control of that
branch is still required. Runner loss/cancellation may prevent cleanup evidence;
KILL_ON_JOB_CLOSE is containment, not proof that an empty Job was observed.
The 720s operational deadline includes setup and publication; GitHub cancellation
latency/host failure cannot be bounded by this Python process. Missing evidence
must never be treated as cleanup success or as a completed diagnostic.
"""

import builtins
import ctypes
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid


ROOT = Path(__file__).resolve().parents[2]
TARGET = ("test_safe_qwen_active_dispatch.ActiveDispatchTests."
          "test_heartbeat_advances_during_attempt_two_model_wait")
REPOSITORY = "MilJav11/triglav-harness"
REF = "refs/heads/codex/f3f-shard8-diagnostic"
BASE = "716136188823844c678c2d4e882f6188a245197f"
MANIFEST_SHA = "0c3013b553035710a3a89e0b65cc4fa4c470f34f4ccaadf2669398f74160ff74"
CASE_LIMIT = 690
TOTAL_LIMIT = 720
CLEANUP_RESERVE = 30  # drain(10), retained-handle wait(10), close fallback(10)
UPLOAD_RESERVE = 60  # remaining job time, including evidence serialization
MAX_EVIDENCE = 4096
MAX_PRIVATE_TAIL = 32768
OUTPUT = ROOT / "runs/ci/f3f-shard8"
EXCEPTIONS = {n for n, c in vars(builtins).items()
              if isinstance(c, type) and issubclass(c, BaseException)} | {
                  "AdapterError", "DurableError", "Denied", "Stop",
                  "OfflineExecutorError", "PolicyViolation", "CoverageError"}
FILES = {"supervised_safe_qwen.py", "safe_qwen_active_dispatch.py",
         "safe_qwen_active_recovery.py", "safe_executor_binding.py", "winprocess.py",
         "harness.py", "supervision.py", "integration_shim.py"}
FUNCTIONS = {"_require", "require", "_run_worker", "check", "_model_check",
             "_verify_handles", "request", "fast_check", "live", "guard",
             "worker_contract", "require_owner", "validate_pinned", "event",
             "validate", "running", "begin", "finish"}


def require(value):
    if not value:
        raise ValueError("F3F closed admission/evidence check failed")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def object_file(path, limit):
    with Path(path).open("rb") as stream:
        raw = stream.read(limit + 1)
    require(len(raw) <= limit)
    value = json.loads(raw)
    require(type(value) is dict)
    return value


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True,
                                   stderr=subprocess.DEVNULL, timeout=5).strip()


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1e12 else None


def choice(value, allowed):
    return value if type(value) is str and value in allowed else None


def boolean(value):
    return value if type(value) is bool else None


def _assertion_payloads(lines):
    """One exact outcome assertion, with at most its four unittest diff lines."""
    headers = [i for i, line in enumerate(lines) if line.startswith("AssertionError:")]
    if len(headers) != 1:
        return ()
    index = headers[0]
    header, separator, raw = lines[index].partition(" : ")
    if header != ("AssertionError: 'executor_failed_unverified' != "
                  "'executor_succeeded_unverified'"):
        return ()
    markers = sum(line.startswith(" : ") for line in lines)
    if separator:
        return (raw,) if markers == 0 else ()
    block = lines[index + 1:index + 6]
    if (len(block) != 5 or markers != 1 or not block[4].startswith(" : ")
            or block[:4] != ["- executor_failed_unverified", "?          ^^^^",
                             "+ executor_succeeded_unverified", "?          ^^^^^^^"]):
        return ()
    return (block[4][3:],)


def _unique_json_pairs(pairs):
    value = {}
    for key, item in pairs:
        require(key not in value)
        value[key] = item
    return value


def diagnostic(path):
    """Project only the existing F3D assertion JSON, never exception prose.

    No raw stdout, traceback, stderr, prompt, response, receipt or WAL is echoed.
    Read a bounded tail; a missing/truncated/unrecognised projection stays unknown.
    """
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(max(0, size - MAX_PRIVATE_TAIL))
        tail = stream.read(MAX_PRIVATE_TAIL).decode("ascii", "replace")
    lines = tail.splitlines()
    if size > MAX_PRIVATE_TAIL:
        lines = lines[1:]  # Never treat a truncated first line as an assertion.
    for raw in _assertion_payloads(lines):
        try:
            require(raw.isascii() and len(raw) <= MAX_EVIDENCE)
            src = json.loads(raw, object_pairs_hook=_unique_json_pairs,
                             parse_constant=lambda _: require(False))
            require(type(src) is dict and src.get("outcome") == "executor_failed_unverified"
                    and {"termination_basis", "protocol_failure", "transport_failure", "causes",
                         "worker_codes", "observed_exit_code", "cleanup", "cleanup_exit_code",
                         "cleanup_empty_job", "native_membership", "elapsed_seconds",
                         "duration_seconds", "started_clock", "requests", "stderr_bytes_observed",
                         "stderr_truncated"} <= src.keys())
        except (ValueError, TypeError, RecursionError):
            continue
        value = {"outcome": choice(src.get("outcome"), {
            "executor_failed_unverified", "executor_succeeded_unverified"}),
            "termination_basis": choice(src.get("termination_basis"), {
                "not_launched", "executing", "controller_cancel", "controller_deadline",
                "controller_exception", "signaled_without_controller_termination"}),
            "protocol_failure": choice(src.get("protocol_failure"), EXCEPTIONS),
            "transport_failure": choice(src.get("transport_failure"), EXCEPTIONS),
            "cleanup": choice(src.get("cleanup"), {"observed_empty", "not_observed"})}
        for key in ("observed_exit_code", "cleanup_exit_code", "elapsed_seconds",
                    "duration_seconds", "started_clock", "stderr_bytes_observed"):
            value[key] = number(src.get(key))
        for key in ("cleanup_empty_job", "native_membership", "stderr_truncated"):
            value[key] = boolean(src.get(key))
        codes = src.get("worker_codes")
        value["worker_codes"] = [choice(v, EXCEPTIONS) for v in codes[:4]] if type(codes) is list else []
        requests = src.get("requests")
        value["requests"] = [{k: number(r.get(k)) for k in ("number", "deadline", "timeout_seconds")}
                             for r in requests[:4] if type(r) is dict] if type(requests) is list else []
        causes = src.get("causes")
        value["causes"] = []
        for cause in causes[:4] if type(causes) is list else []:
            if type(cause) is not dict:
                continue
            frames = cause.get("frames")
            value["causes"].append({"error_type": choice(cause.get("error_type"), EXCEPTIONS),
                "code": choice(cause.get("code"), {"TIMEOUT", "WORKER_FAILED", "ACTION_LIMIT"}),
                "frames": [{"file": choice(f.get("file"), FILES),
                            "function": choice(f.get("function"), FUNCTIONS),
                            "line": number(f.get("line"))}
                           for f in frames[-4:] if type(f) is dict] if type(frames) is list else []})
        return value
    return None


def publish(report):
    raw = json.dumps(report, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii")
    if len(raw) > MAX_EVIDENCE:
        # Preserve the causal categories; drop bulky frames/requests explicitly.
        diag = report.get("executor")
        if type(diag) is dict:
            diag.pop("causes", None)
            diag.pop("requests", None)
        report["diagnostic_truncated"] = True
        raw = json.dumps(report, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii")
    require(len(raw) <= MAX_EVIDENCE)
    with (OUTPUT / "evidence.json").open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    sys.stdout.write(raw.decode("ascii"))
    sys.stdout.flush()


def execute(report, end):
    sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
    from scripts import ci_supplemental as ci
    from scripts.ci_supplemental_policy import SupplementalPolicy
    import winprocess

    manifest, manifest_sha = ci.load_manifest()
    require(manifest_sha == MANIFEST_SHA)
    ids = ci.selection_ids(manifest, "shard-8")
    require(len(ids) == 67 and ids[23] == TARGET and ids.count(TARGET) == 1)
    require(TARGET not in ci.selection_ids(manifest, "shard-8", True))
    require(manifest["case_budgets_seconds"][TARGET] == CASE_LIMIT)
    require(manifest["entry_profiles"][TARGET] == "fake-http-gateway")
    require(ci.checkout_head() == report["head"])
    if time.monotonic() >= end - CLEANUP_RESERVE - UPLOAD_RESERVE:
        report.update(outcome="GLOBAL_TIMEOUT", outer_timeout="GLOBAL_TIMEOUT")
        return

    run_id = uuid.uuid4().hex
    scratch = ROOT / "runs/f2p"
    scratch.mkdir(parents=True, exist_ok=True)
    policy = SupplementalPolicy(ROOT, scratch / run_id[:8])
    fixture = policy.new_fixture()
    temp = fixture.root / "t"
    temp.mkdir()
    helper = fixture.root / "case.py"
    helper.write_bytes(ci.CAPSULE)
    # All these files are private: only OUTPUT/evidence.json is an artifact.
    receipt = fixture.root / "child.json"
    stderr = fixture.root / "stderr.log"
    batch = os.environ["GITHUB_RUN_ID"] + ".1"
    argv = [str(policy.python), "-I", "-B", str(helper), str(ROOT), str(ci.MANIFEST),
            TARGET, str(receipt), manifest_sha, run_id, str(temp), report["head"],
            "shard-8", batch, "selection"]
    pins = {ROOT / p: h for p, h in manifest["source_sha256"].items()}
    pins.update({helper: digest(ci.CAPSULE), ci.MANIFEST: MANIFEST_SHA,
                 Path(__file__).resolve(): report["launcher_sha256"]})
    policy.register(fixture, profile="isolated_helper", boundary="native", argv=argv,
                    cwd=fixture.root, source_pins=pins, limit=1)
    native = winprocess.ControllerVerifier("Local\\ci-pilot-" + uuid.uuid4().hex)
    code = None
    timed_out = None
    try:
        with open(os.devnull, "rb") as inp, open(os.devnull, "wb") as out, stderr.open("xb") as err:
            with policy.guard():
                # Global envelope is additional to, never an extension of, the
                # unchanged test's internal 180s/30s deadlines and 20s wait.
                remaining = end - time.monotonic() - CLEANUP_RESERVE - UPLOAD_RESERVE
                if remaining <= 0:
                    timed_out = "GLOBAL_TIMEOUT"
                require(remaining > 0)
                native.launch(argv, fixture.root, (inp, out, err))
                identity = dict(native.identity)
                member = winprocess.W.BOOL()
                require(native.k.IsProcessInJob(native.process, native.job.handle, ctypes.byref(member))
                        and member.value and native.wait(0) is None)
                native.verify(identity)
                remaining = end - time.monotonic() - CLEANUP_RESERVE - UPLOAD_RESERVE
                if remaining <= 0:
                    timed_out = "GLOBAL_TIMEOUT"
                require(remaining > 0)
                wait = min(CASE_LIMIT, remaining)
                report["wait_limit_seconds"] = round(wait, 3)
                report["native_member"] = True
                native.resume(identity)
                report["executions"] = 1
                code = native.wait(wait)
                if code is None:
                    timed_out = "GLOBAL_TIMEOUT" if wait < CASE_LIMIT else "CASE_TIMEOUT"
    finally:
        # Always signal the retained Job, not an observed/reused PID. Each failure
        # remains explicit; closing the Job alone never implies observed_empty.
        try:
            native.terminate_job()
            native.drain(10)
            report["empty_job"] = True
            if native.process is not None:
                observed = native.wait(10)
                report["cleanup_exit_code"] = observed
                report["original_handle_exited"] = (observed is not None and
                    winprocess._handle_identity(native.k, native.process) is None)
                require(report["original_handle_exited"])
                if code is not None:
                    require(code == observed)
        except BaseException as exc:
            report["cleanup_error"] = choice(type(exc).__name__, EXCEPTIONS)
            report["outcome"] = "CLEANUP_FAILED"
        finally:
            try:
                native.close()
            except BaseException as exc:
                report["cleanup_error"] = choice(type(exc).__name__, EXCEPTIONS)
                report["outcome"] = "CLEANUP_FAILED"
        report["child_exit_code"] = code
        if timed_out:
            report["outer_timeout"] = timed_out
            if report["outcome"] != "CLEANUP_FAILED":
                report["outcome"] = timed_out
        if stderr.exists():
            report["executor"] = diagnostic(stderr)

    require(report["empty_job"] and report["original_handle_exited"])
    admissions = policy.events
    require(len(admissions) == 1 and admissions[0]["decision"] == "ALLOW"
            and admissions[0]["profile"] == "isolated_helper" and admissions[0]["launch_number"] == 1)
    ci.validate_sources(manifest)
    require(digest(ci.MANIFEST.read_bytes()) == MANIFEST_SHA and ci.checkout_head() == report["head"])
    if timed_out or report["outcome"] == "CLEANUP_FAILED":
        return
    child = object_file(receipt, MAX_PRIVATE_TAIL)
    for key, value in {"head": report["head"], "manifest_sha256": MANIFEST_SHA,
                       "test_id": TARGET, "run_id": run_id, "batch": batch,
                       "selection": "shard-8", "scope": "selection"}.items():
        require(child.get(key) == value)
    require(child.get("tests_run") == 1 and number(child.get("seconds")) is not None
            and child["seconds"] <= CASE_LIMIT)
    if code == 0 and child.get("events") == ["PASS"] and child.get("status") == "PASS":
        report["outcome"] = "NON_REPRODUCED"
    else:
        require(code == 1 and child.get("events") in (["FAIL"], ["ERROR"]) and child.get("status") == "ERROR")
        diag = report.get("executor") or {}
        internal = (diag.get("termination_basis") == "controller_deadline"
                    or "TimeoutError" in diag.get("worker_codes", [])
                    or any(c.get("code") == "TIMEOUT" for c in diag.get("causes", [])))
        report["outcome"] = "INTERNAL_TIMEOUT" if internal else "REPRODUCED_FAILURE"


def main():
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.mkdir()  # Never resume/adopt evidence from another execution.
    report = dict(schema_version=1, test_id=TARGET, outcome="BLOCKED", executions=0,
                  full_phase3_1_qualified=False, outer_timeout=None, executor=None,
                  empty_job=None, original_handle_exited=None, recursive_guards=False,
                  os_network_isolation=False, case_budget_seconds=CASE_LIMIT,
                  total_budget_seconds=TOTAL_LIMIT, cleanup_reserve_seconds=CLEANUP_RESERVE,
                  upload_reserve_seconds=UPLOAD_RESERVE)
    end = None
    try:
        require(os.name == "nt" and sys.version_info[:3] == (3, 12, 10)
                and ctypes.sizeof(ctypes.c_void_p) == 8 and len(sys.argv) == 1)
        require(os.environ.get("GITHUB_ACTIONS") == "true"
                and os.environ.get("GITHUB_REPOSITORY") == REPOSITORY
                and os.environ.get("GITHUB_EVENT_NAME") == "push"
                and os.environ.get("GITHUB_REF") == REF
                and os.environ.get("GITHUB_RUN_ATTEMPT") == "1")
        event = object_file(os.environ["GITHUB_EVENT_PATH"], 1048576)
        require(event.get("created") is True and event.get("deleted") is False and event.get("ref") == REF)
        head = os.environ.get("GITHUB_SHA", "")
        require(re.fullmatch("[0-9a-f]{40}", head) and event.get("after") == head)
        require(re.fullmatch("[0-9]+", os.environ.get("GITHUB_RUN_ID", "")))
        expected = os.environ.get("F3F_LAUNCHER_SHA256", "")
        require(re.fullmatch("[0-9a-f]{64}", expected))
        require(digest(Path(__file__).read_bytes()) == expected)
        report.update(head=head, launcher_sha256=expected, manifest_sha256=MANIFEST_SHA)
        # Workflow obtains this job's actual started_at via a read-only Actions
        # API request before checkout/setup. Setup consumes the same 720s budget.
        started = datetime.fromisoformat(os.environ["F3F_JOB_STARTED_AT"].replace("Z", "+00:00"))
        require(started.tzinfo is not None)
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        require(math.isfinite(elapsed) and elapsed >= 0)
        end = time.monotonic() + TOTAL_LIMIT - elapsed
        report["setup_seconds"] = round(elapsed, 3)
        if elapsed >= TOTAL_LIMIT - CLEANUP_RESERVE - UPLOAD_RESERVE:
            report.update(outcome="GLOBAL_TIMEOUT", outer_timeout="GLOBAL_TIMEOUT")
        require(elapsed < TOTAL_LIMIT - CLEANUP_RESERVE - UPLOAD_RESERVE)
        require(git("rev-parse", "HEAD") == head
                and git("rev-list", "--parents", "-n", "1", "HEAD").split() == [head, BASE]
                and git("status", "--porcelain", "--untracked-files=all") == "")
        allowed = {"scripts/ci_manifest.json", "scripts/ci_supplemental.py",
                   "tests/test_ci_supplemental_runner.py", "tests/test_safe_qwen_active_dispatch.py",
                   "tests/test_unittest_verifier.py", ".github/diagnostics/f3f_shard8.py",
                   ".github/workflows/f3f-shard8.yml"}
        require(set(git("diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").splitlines()) == allowed)
        execute(report, end)
    except BaseException as exc:
        report["error_category"] = choice(type(exc).__name__, EXCEPTIONS)
        # Do not relabel a cleanup failure or a known outer deadline as an
        # internal executor timeout. Unknown failure evidence stays BLOCKED.
    if end is not None:
        report["remaining_upload_seconds"] = round(max(0, end - time.monotonic()), 3)
        if time.monotonic() >= end - UPLOAD_RESERVE:
            report["outer_timeout"] = "GLOBAL_TIMEOUT"
            if report["outcome"] != "CLEANUP_FAILED":
                report["outcome"] = "GLOBAL_TIMEOUT"
    publish(report)
    return 0 if report["outcome"] == "NON_REPRODUCED" and "error_category" not in report else 1


if __name__ == "__main__":
    raise SystemExit(main())
