"""One real supervised Manager round: Qwen -> Nemotron -> GPT-OSS -> final verifier.

Requires the dedicated gateway started by start-swap.ps1, with no model resident.
The planner is pinned to one bounded step so this tests the model/review lifecycle
deterministically. Executor, critic, reviewer, verifier and trust gates are production
adapters. All fixture files and durable evidence are retained in a new runs directory.
"""
import json
import sys
import threading
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import durable
import harness
import manager
import supervision
import winprocess
from importlib import import_module

git = import_module("smoke-autonomous").git
TASK = "Fix eligibility.py: for integer ages, is_adult(age) must return the boolean age >= 18. Preserve tests."
TESTS = ("import unittest\nfrom eligibility import is_adult\n\n"
         "class EligibilityTests(unittest.TestCase):\n"
         "    def test_ages(self):\n"
         "        for age in (-1, 0, 17, 18, 19, 120):\n"
         "            with self.subTest(age=age):\n"
         "                self.assertIs(is_adult(age), age >= 18)\n")
CHECK = ["python", "-m", "unittest", "test_eligibility"]


class PinnedPlanner:
    def plan(self, context, tier):
        if tier != "executor":
            raise AssertionError("This medium-risk fixture does not need senior planning")
        return {"step_id": "boundary", "goal": TASK, "rationale": "Repair the integer boundary",
                "scope": {"allowed_paths": ["eligibility.py"], "forbidden_paths": ["test_eligibility.py"]},
                "acceptance_checks": ["check-1"], "risk": "medium", "needs_reviewer": True,
                "estimated_effort": "small", "completion_signal": "Pinned boundary tests pass"}


def main():
    config_path = ROOT / "config" / "harness.json"
    config = harness.load_config(config_path)
    workspace = ROOT / "runs" / ("three-model-smoke-" + uuid.uuid4().hex[:12])
    repo_path = workspace / "repo"
    repo_path.mkdir(parents=True)
    print(f"Evidence: {workspace}", flush=True)
    report = {"passed": False, "evidence": str(workspace), "planner": "pinned deterministic step"}
    preflight = harness.EventLog(workspace / "preflight.jsonl")
    probe = harness.Gateway(config, preflight)
    running, servers = probe.running(), winprocess.servers()
    if running or servers:
        raise RuntimeError("Three-model smoke requires an idle dedicated gateway and no native model server")
    (repo_path / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    (repo_path / "eligibility.py").write_text("def is_adult(age):\n    return age > 18\n", encoding="utf-8")
    (repo_path / "test_eligibility.py").write_text(TESTS, encoding="utf-8")
    git(repo_path, "init", "-q")
    git(repo_path, "add", ".")
    git(repo_path, "commit", "-qm", "baseline: pinned integer boundary tests")
    repo = harness.Repository(repo_path, config, preflight)
    store = durable.Store(workspace / "runs", "three-model-smoke")
    manager.create_run(store, repo, TASK, [CHECK], config, reviewer=True, critic=True,
                       criteria=["Integer age boundary is correct"], budgets={"max_rounds": 1},
                       allow=["eligibility.py"], forbid=["test_eligibility.py"], config_path=config_path)

    def agents(repo_, gateway_, config_, log_):
        real = manager.default_agents(repo_, gateway_, config_, log_)
        real.planner = PinnedPlanner()
        return real

    stop = threading.Event()
    peak = {"servers": 0, "samples": 0}

    def sample():
        while not stop.is_set():
            peak["servers"] = max(peak["servers"], len(winprocess.servers()))
            peak["samples"] += 1
            stop.wait(0.5)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    failure = None
    try:
        manager.execute(store, agents=agents, progress=True)
    except BaseException as exc:
        failure = f"{type(exc).__name__}: {str(exc)[:500]}"
    finally:
        stop.set()
        sampler.join()
    store.load()
    state, events = store.state, store.records
    idle = True
    try:
        supervision.Supervisor(store).require_idle()
    except durable.DurableError:
        idle = False
    running, servers = probe.running(), winprocess.servers()
    children = list(state["supervision"]["children"].values())
    starts = [e["role"] for e in events if e["event"] == "model_started"]
    lifecycle = [{"event": e["event"], "role": e.get("role"), "time": e["time"]}
                 for e in events if e["event"] in {"model_started", "model_unloaded", "test_result", "review_result"}]
    expected = ["model_started", "test_result", "model_unloaded", "model_started", "model_unloaded",
                "model_started", "review_result", "model_unloaded", "test_result"]
    # Executor tool commands may exist; test_result is emitted only by the Manager verifier.
    order_ok = [e["event"] for e in lifecycle] == expected and starts == ["code", "critic", "review"]
    checkpoint = critic = review = completion = None
    verifications = []
    trusted = state["last_verified_checkpoint"]
    if trusted:
        checkpoint = store.read_evidence(trusted["reference"])
        verifications = [store.read_evidence(ref) for ref in checkpoint["verification"]]
        critic = store.read_evidence(checkpoint["critic"]) if checkpoint["critic"] else None
        review = store.read_evidence(checkpoint["reviewer"]) if checkpoint["reviewer"] else None
    if state["manager"]["completion"]:
        completion = store.read_evidence(state["manager"]["completion"]["reference"])
    final_ok = bool(len(verifications) == 2 and review and
                    verifications[0]["time"] <= review["time"] <= verifications[-1]["time"] and
                    all(v["passed"] and v["snapshot"] == checkpoint["snapshot"] and
                        [c["argv"] for c in v["commands"]] == [CHECK, ["git", "diff", "--check"]] and
                        all(c["exit_code"] == 0 for c in v["commands"]) for v in verifications))
    tests_preserved = (repo_path / "test_eligibility.py").read_text(encoding="utf-8") == TESTS
    models = [c for c in children if c["purpose"] == "model_server"]
    report.update(failure=failure, final_status=state["status"], rounds=state["manager"]["rounds"],
                  completion=completion, trusted_checkpoint=trusted, critic=critic, reviewer=review,
                  lifecycle=lifecycle, load_order=starts, lifecycle_order_passed=order_ok,
                  final_deterministic_verification=final_ok, verification_references=(checkpoint or {}).get("verification", []),
                  tests_preserved=tests_preserved, supervisor_idle=idle, final_children=children,
                  gateway_running_after=running, native_servers_after=servers,
                  peak_native_servers_sampled=peak["servers"], samples=peak["samples"])
    report["passed"] = bool(failure is None and state["status"] == "COMPLETED" and
                            completion and completion["all_proven"] and checkpoint and
                            all(checkpoint["gates"][name] is True for name, _ in manager.GATE_NAMES) and
                            critic and critic["status"] == "completed" and critic["advisory_only"] is True and
                            review and review["approved"] is True and final_ok and order_ok and tests_preserved and
                            idle and not running and not servers and peak["servers"] == 1 and
                            len(models) == 3 and children and all(c["state"] == "EXITED" for c in children))
    durable.atomic_write(workspace / "smoke-report.json", durable.encoded(report))
    print(json.dumps(report, indent=2), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
