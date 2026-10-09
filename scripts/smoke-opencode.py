"""Live executor qualification. This script writes ONLY intentionally buggy fixtures.
Planner and optional first failed candidate are controlled; all solution edits are local Qwen.
Requires a healthy, exclusive llama-swap at 9292 and the qualified OpenCode 1.18.30."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import durable
import harness
import manager
from opencode_executor import OpenCodeExecutor

BUG = """def subtotal(items):
    return sum(price + quantity for price, quantity in items)

def total(items, discount_percent):
    return subtotal(items) * (1 + discount_percent / 100)
"""
TESTS = """import unittest
from cart import subtotal, total
class CartTests(unittest.TestCase):
    def test_subtotal(self):
        self.assertEqual(subtotal([(10, 3), (4, 2)]), 38)
        self.assertEqual(subtotal([]), 0)
    def test_discount(self):
        self.assertEqual(total([(10, 3), (4, 2)], 50), 19)
        self.assertEqual(total([(10, 3)], 0), 30)
"""
TASK = """Repair cart.py only. Never edit tests, Git metadata, install packages or access the network.
Required sequence: inspect cart.py and test_cart.py, then run
python -m unittest test_cart.CartTests.test_subtotal -v to observe failure.
Fix ONLY subtotal first; leave total untouched until the targeted test passes.
Then run python -m unittest -v and observe the remaining failure.
Make a focused repair to total and rerun the full suite to PASS. Inspect the final diff.
Perform real file edits and commands. Your work is an untrusted candidate."""
CHECK = ["python", "-m", "unittest", "-v"]


class Planner:
    """Fixed validated plan; executor qualification does not retest model planning."""
    def plan(self, context, tier):
        value = dict(step_id="cart-repair", goal="Repair cart arithmetic using the required targeted-failure/PASS then full-failure/repair/PASS sequence", rationale="repair the observed arithmetic failures",
            scope={"allowed_paths": ["cart.py"], "forbidden_paths": ["test_cart.py"]},
            acceptance_checks=["check-1"], risk="low", needs_reviewer=False,
            estimated_effort="small", completion_signal="the pinned tests pass")
        evidence = context.get("recovery_evidence")
        if evidence:
            value["recovery_from"] = {"round": evidence["round"], "fingerprint": evidence["fingerprint"]}
        return value


class ControlledFirstCandidate:
    def __init__(self, live):
        self.live, self.calls = live, 0
    def set_deadline(self, value):
        self.live.set_deadline(value)
    def execute(self, step, context, repo):
        self.calls += 1
        if self.calls == 1:
            repo.write("cart.py", BUG + "# Controlled unacceptable first candidate; no solution edits.\n")
            repo.log.emit("controlled_candidate", trusted=False, completed=True)
            return {"classification": "EXECUTOR_CHANGED", "trusted": False}
        return self.live.execute(step, context, repo)


class Forbidden:
    def __getattr__(self, name):
        raise AssertionError("review agents are not required by this low-risk seam fixture")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--recovery", action="store_true")
    parser.add_argument("--false-success", action="store_true")
    args = parser.parse_args()
    name = "opencode-" + ("recovery-" if args.recovery else "false-success-" if args.false_success else "live-") + uuid.uuid4().hex[:12]
    directory = ROOT / "runs" / name
    repo_path = directory / "repo"
    repo_path.mkdir(parents=True)
    for name_, text in (("cart.py", BUG), ("test_cart.py", TESTS), (".gitignore", "__pycache__/\n")):
        (repo_path / name_).write_text(text, encoding="utf-8")
    def git(*argv):
        return subprocess.run(["git", "-C", str(repo_path), "-c", f"safe.directory={repo_path}",
            "-c", "user.name=Executor qualification", "-c", "user.email=fixture@localhost", *argv],
            check=True, capture_output=True)
    git("init", "-q")
    git("add", ".")
    git("commit", "-qm", "intentional failing qualification fixture")
    baseline = subprocess.run(CHECK, cwd=repo_path, capture_output=True, text=True)
    (directory / "initial-tests.json").write_text(json.dumps({
        "exit_code": baseline.returncode, "stdout": baseline.stdout, "stderr": baseline.stderr}, indent=2))
    assert baseline.returncode != 0
    test_hash = hashlib.sha256((repo_path / "test_cart.py").read_bytes()).hexdigest()
    config = harness.load_config(ROOT / "config/harness.json")
    config["executor"] = "opencode"
    config["heartbeat_seconds"] = 10
    log = harness.EventLog(directory / "preflight.jsonl", config=config)
    repo = harness.Repository(repo_path, config, log)
    store = durable.Store(directory / "controller", "qualification")
    manager.create_run(store, repo, TASK, [CHECK], config, criteria=["cart arithmetic passes"],
                       allow=["cart.py"], forbid=["test_cart.py"], budgets={"max_rounds": 2 if args.recovery else 1})
    def agents(repo, gateway, config, log):
        live = OpenCodeExecutor(gateway, config, log)
        return manager.Agents(Planner(), ControlledFirstCandidate(live) if args.recovery or args.false_success else live,
                              Forbidden(), Forbidden())
    report = manager.execute(store, agents=agents, progress=True)
    unchanged = hashlib.sha256((repo_path / "test_cart.py").read_bytes()).hexdigest() == test_hash
    summary = {"report": report, "tests_unchanged": unchanged,
               "mode": "controlled-first-live-recovery" if args.recovery else "controlled-false-success" if args.false_success else "live-executor",
               "episodes": [json.loads(p.read_text()) for p in sorted(store.directory.glob("opencode/*/result.json"))]}
    (directory / "qualification.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (directory / "candidate.diff").write_bytes(git("diff", "--", "cart.py").stdout)
    print(json.dumps({"directory": str(directory), "status": report["status"], "tests_unchanged": unchanged}))
    if args.false_success:
        assert report["rounds"][0]["reason"] == "VERIFICATION_FAILED" and not report["rounds"][0]["trusted"]
        assert report["last_verified_checkpoint"] is None and unchanged
        return
    assert report["status"] == "COMPLETED" and unchanged, "See qualification.json for failed evidence"
    if args.recovery:
        assert report["rounds"][0]["reason"] == "VERIFICATION_FAILED"
        assert not report["rounds"][0]["trusted"] and report["rounds"][1]["trusted"]
    else:
        exits = summary["episodes"][0]["metadata"]["command_exits"]
        assert 1 in exits and 0 in exits, "Live failure/PASS command evidence missing"


if __name__ == "__main__":
    main()
