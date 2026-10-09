"""One deterministic durable CLI smoke: real tests/Git, fake model answers, actual process exit."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import durable
import harness
from test_durable import FakeGateway, fixture


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", choices=["start", "resume"])
    parser.add_argument("--workspace", type=Path)
    args = parser.parse_args()
    if args.child:
        workspace = args.workspace
        harness.ROOT = workspace
        if args.child == "start":
            # fixture's CREATED run is separate; the public CLI creates the smoke run.
            repo, store, _ = fixture(workspace)
            config = workspace / "config.json"
            config.write_text(json.dumps(store.state["options"]["config"]), encoding="utf-8")
            with patch("durable.Gateway", FakeGateway), patch("durable.finish", side_effect=lambda store: os._exit(73)):
                return harness.main(["--config", str(config), "long-run", "--quiet", "--repo", str(repo.root),
                                     "--task", "Implement multiply", "--verify", "python -m unittest discover"])
        run_id = (workspace / "run-id.txt").read_text()
        # No gateway mock: a trusted checkpoint must resume without model access.
        return harness.main(["resume", "--quiet", "--run-id", run_id])

    workspace = ROOT / "runs" / f"durable-smoke-{uuid.uuid4().hex[:12]}"
    workspace.mkdir(parents=True)
    command = [sys.executable, str(Path(__file__).resolve()), "--workspace", str(workspace)]
    start = subprocess.run(command + ["--child", "start"], capture_output=True, timeout=90)
    (workspace / "start.stdout.txt").write_bytes(start.stdout)
    (workspace / "start.stderr.txt").write_bytes(start.stderr)
    assert start.returncode == 73, start.stderr.decode()
    states = list((workspace / "runs").glob("*/state.json"))
    state = next(json.loads(p.read_text()) for p in states if p.parent.name != "test-run")
    assert state["status"] == "VERIFIED", state["status"]
    trusted = state["last_verified_checkpoint"]
    (workspace / "run-id.txt").write_text(state["run_id"])
    resume = subprocess.run(command + ["--child", "resume"], capture_output=True, timeout=30)
    (workspace / "resume.stdout.txt").write_bytes(resume.stdout)
    (workspace / "resume.stderr.txt").write_bytes(resume.stderr)
    assert resume.returncode == 0, resume.stderr.decode()
    store = durable.Store(workspace / "runs", state["run_id"])
    store.load()
    assert store.state["status"] == "COMPLETED"
    assert store.state["last_verified_checkpoint"] == trusted
    assert store.state["round_number"] == 1
    report = {"passed": True, "start_process_exit": start.returncode, "resume_process_exit": resume.returncode,
              "before_resume": "VERIFIED", "after_resume": "COMPLETED", "run_id": state["run_id"],
              "checkpoint_reconstructed": True, "real_verification": "python -m unittest discover; git diff --check",
              "models": "deterministic stand-in responses; no inference servers", "evidence": str(workspace)}
    durable.atomic_write(workspace / "smoke-report.json", durable.encoded(report) + b"\n")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
