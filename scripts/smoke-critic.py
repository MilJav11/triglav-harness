"""One bounded three-model run in a fresh, retained disposable Git repository."""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import ROOT, EventLog, Gateway, available_ram_gb, load_config
from winprocess import servers


def run():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    evidence_dir = ROOT / "runs" / f"critic-e2e-{stamp}"
    evidence_dir.mkdir()  # Never overwrite earlier fixtures or evidence.
    repo = evidence_dir / "repo"
    repo.mkdir()
    log = EventLog(evidence_dir / "resources.jsonl")
    config = load_config(ROOT / "config/harness.json")
    config.update(max_actions=12, max_retries=0, request_timeout_seconds=300, startup_timeout_seconds=120)
    config_path = evidence_dir / "config.json"
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    gateway = Gateway(config, log)
    if gateway.running() or servers():
        raise RuntimeError("E2E requires an idle dedicated gateway and no llama-server processes")
    tests = ("import unittest\nfrom eligibility import is_adult\n\n"
             "class EligibilityTests(unittest.TestCase):\n"
             "    def test_minor(self): self.assertFalse(is_adult(17))\n"
             "    def test_boundary(self): self.assertTrue(is_adult(18))\n"
             "    def test_adult(self): self.assertTrue(is_adult(19))\n"
             "    def test_zero(self): self.assertFalse(is_adult(0))\n"
             "    def test_negative(self): self.assertFalse(is_adult(-1))\n"
             "    def test_large(self): self.assertTrue(is_adult(120))\n")
    (repo / "eligibility.py").write_text("def is_adult(age):\n    return age > 18\n", encoding="utf-8")
    (repo / "test_eligibility.py").write_text(tests, encoding="utf-8")
    (repo / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    def git(*args):
        return subprocess.run(["git", "-c", f"safe.directory={repo}", *args], cwd=repo,
                              check=True, capture_output=True, text=True)
    git("init", "-q")
    git("add", ".")
    git("-c", "user.name=Harness fixture", "-c", "user.email=fixture@localhost",
        "commit", "-qm", "Disposable boundary fixture")
    assert not git("status", "--porcelain").stdout
    before_logs = set((ROOT / "runs").glob("*.jsonl"))
    initial_ram = available_ram_gb()
    argv = [sys.executable, str(ROOT / "harness.py"), "--config", str(config_path),
            "--review-profile", "review-safe", "--critic", "run", "--repo", str(repo),
            "--task", "Fix eligibility.py: for integer age inputs, is_adult(age) must return the boolean "
            "age >= 18. Read eligibility.py and test_eligibility.py. Change only eligibility.py; preserve "
            "all tests. Non-integer inputs are outside the contract. Run the existing unittest suite.",
            "--verify", "python -m unittest discover -v", "--reviewer"]
    max_resident, minimum_ram, timed_out = 0, initial_ram, False
    started = time.monotonic()
    with (evidence_dir / "output.txt").open("w", encoding="utf-8") as output:
        process = subprocess.Popen(argv, cwd=ROOT, stdout=output, stderr=subprocess.STDOUT,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            while process.poll() is None:
                current, ram = servers(), available_ram_gb()
                log.emit("resource_sample", free_gb=ram, processes=current)
                max_resident = max(max_resident, len(current))
                minimum_ram = min(minimum_ram, ram) if ram is not None else minimum_ram
                if time.monotonic() - started > 900:
                    timed_out = True
                    process.kill()
                    process.wait(10)
                    break
                time.sleep(1)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(10)
            gateway.unload()  # Reconcile even if the controller timed out.
    final_processes, final_ram = servers(), available_ram_gb()
    log.emit("e2e_cleanup", free_gb=final_ram, processes=final_processes)
    new_logs = set((ROOT / "runs").glob("*.jsonl")) - before_logs
    events = []
    for path in sorted(new_logs):
        events.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())
    starts = [e["model"] for e in events if e["event"] == "model_started"]
    critics = [e for e in events if e["event"] == "critic_result"]
    reviews = [e for e in events if e["event"] == "review_result"]
    results = [e for e in events if e["event"] == "task_result"]
    tests_preserved = (repo / "test_eligibility.py").read_text(encoding="utf-8") == tests
    passed = (process.returncode == 0 and not timed_out and max_resident <= 1 and not final_processes
              and starts == ["llm-code", "llm-critic", "llm-review"] and len(critics) == 1
              and bool(reviews) and reviews[-1]["approved"] and bool(results)
              and results[-1]["status"] == "passed" and tests_preserved)
    summary = {"passed": passed, "exit_code": process.returncode, "timed_out": timed_out,
               "elapsed_seconds": round(time.monotonic() - started, 2), "model_starts": starts,
               "model_metadata": config["model_metadata"], "max_resident_servers": max_resident,
               "initial_ram_gb": initial_ram, "minimum_ram_gb": minimum_ram, "final_ram_gb": final_ram,
               "final_processes": final_processes, "tests_preserved": tests_preserved,
               "critic_results": critics, "senior_reviews": reviews,
               "controller_logs": [str(p.relative_to(ROOT)) for p in sorted(new_logs)]}
    (evidence_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (evidence_dir / "diff.patch").write_text(git("diff", "--no-ext-diff", "--no-textconv").stdout, encoding="utf-8")
    print(json.dumps(summary, indent=2))
    print(f"Evidence: {evidence_dir}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(run())
