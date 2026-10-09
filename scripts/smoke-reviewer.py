"""Reusable live Senior Reviewer integration smoke for Wave 6.

Live success criterion:
real GPT-OSS review -> structured result -> if APPROVE, controller reruns deterministic verifier -> trusted checkpoint only after PASS.
"""
import copy
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import durable
from durable import digest, snapshot
import harness
from harness import EventLog, Gateway, load_config
import reviewer_executor as re
from reviewer_executor import (
    REVIEWER_APPROVED,
    ReviewerExecutor,
    execute_final_verification,
    create_trusted_checkpoint,
)
import work_unit_scheduler as wus
import work_units as wu
from manager import create_run, validate_repository as validate_manager_repository

TASK = "Implement multiply in calculator.py"
COMMANDS = [["python", "-m", "unittest", "test_calculator.py"]]

def run():
    print("=== LIVE GPT-OSS REVIEWER QUALIFICATION ===")
    config = load_config(ROOT / "config/harness.json")
    config["request_timeout_seconds"] = 300
    config["startup_timeout_seconds"] = 300

    evidence_dir = ROOT / "runs" / f"live-gpt-oss-qual-{int(time.time())}"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    repo_path = evidence_dir / "repo"
    repo_path.mkdir(parents=True, exist_ok=True)

    (repo_path / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    (repo_path / "calculator.py").write_text(
        "def multiply(a, b):\n    \"\"\"Multiply two numbers.\"\"\"\n    return a * b\n",
        encoding="utf-8",
    )
    (repo_path / "test_calculator.py").write_text(
        "import unittest\nfrom calculator import multiply\n"
        "class TestCalc(unittest.TestCase):\n"
        "    def test_multiply(self): self.assertEqual(multiply(2, 3), 6)\n"
        "if __name__ == '__main__': unittest.main()\n",
        encoding="utf-8",
    )

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(repo_path), "-c", f"safe.directory={repo_path}",
             "-c", "user.name=Live Review Test", "-c", "user.email=live@localhost", *args],
            check=True, capture_output=True, text=True,
        )

    git("init", "-q")
    git("add", ".")
    git("commit", "-qm", "baseline with multiply implemented and tested")

    log = EventLog(evidence_dir / "live_reviewer.jsonl")
    repo = harness.Repository(repo_path, config, log)
    store = durable.Store(evidence_dir / "runs", "live-qual")

    verifier_registry = {
        "check-calc": {
            "argv": ["python", "-m", "unittest", "test_calculator.py"],
            "timeout_seconds": 30,
        }
    }

    spec = {
        "unit_id": "unit-1",
        "objective": "Implement multiply in calculator.py",
        "dependencies": [],
        "mode": "mutation",
        "scope": {"allowed_paths": ["calculator.py"], "forbidden_paths": []},
        "verifier_ids": ["check-calc"],
        "evidence_inputs": [],
        "limits": {"max_attempts": 2, "timeout_seconds": 60},
    }

    create_run(
        store,
        repo,
        TASK,
        COMMANDS,
        config,
        criteria=["Multiply works"],
        work_units=[spec],
        verifier_registry=verifier_registry,
        critic=True,
        reviewer=True,
    )

    from manager import Agents
    from work_unit_scheduler import ScriptedExecutor, WorkUnitScheduler
    wu_exec = ScriptedExecutor()
    wu_exec.set_behavior("unit-1", {"writes": {"calculator.py": "def multiply(a, b):\n    return a * b\n"}})
    scheduler = WorkUnitScheduler(
        store,
        repo,
        verifier_registry=verifier_registry,
        agents=Agents(None, wu_exec, None, None),
        parent_scope={"allowed_paths": ["calculator.py"], "forbidden_paths": []},
    )
    wu_res = scheduler.run_sequence()
    assert wu_res["status"] == "MILESTONE_READY"

    import critic_executor as ce
    crit_exec = ce.CriticExecutor(store, repo, config, critic_adapter=ce.ScriptedCritic("clean"))
    crit_result = crit_exec.execute()
    assert crit_result["status"] == "CRITIC_CLEAN"
    assert not ce.is_critic_stale(store, repo, config)

    print("Preconditions established:")
    print("  WorkUnits: UNIT_VERIFIED")
    print("  Milestone: CRITIC_REVIEWED")
    print("  Current RAM:", harness.available_ram_gb(), "GiB")

    gateway = Gateway(config, log)

    print("\nInvoking live Senior Reviewer (GPT-OSS-120B via llama-swap)...")
    executor = ReviewerExecutor(
        store,
        repo,
        config,
        gateway=gateway,
        reviewer_adapter=None,  # Live inference via Gateway
        log=log,
    )

    t0 = time.monotonic()
    reviewer_result = executor.execute()
    elapsed = time.monotonic() - t0
    print(f"GPT-OSS returned structured result in {elapsed:.1f}s:")
    print(f"  Status: {reviewer_result.get('status')}")
    print(f"  Decision: {reviewer_result.get('decision')}")
    print(f"  Summary: {reviewer_result.get('summary')}")
    print(f"  Findings count: {len(reviewer_result.get('findings', []))}")
    print(f"  Model identity: {reviewer_result.get('model_identity')}")

    assert reviewer_result.get("status") in (
        REVIEWER_APPROVED,
        re.REVIEWER_REJECTED,
        re.REVIEWER_INCONCLUSIVE,
    ), f"Unexpected reviewer status: {reviewer_result.get('status')}"

    # Critical Trust Principle Check: APPROVE alone must not create checkpoint
    assert store.state.get("last_verified_checkpoint") is None, "CRITICAL: APPROVE must not create checkpoint!"
    assert len(store.state.get("verified_progress", [])) == 0, "CRITICAL: APPROVE must not advance verified progress!"
    print("\nTrust Separation Verified: Senior Reviewer alone did NOT create a checkpoint.")

    if reviewer_result.get("status") == REVIEWER_APPROVED:
        print("\nReviewer APPROVED. Proceeding to MANDATORY FINAL DETERMINISTIC VERIFICATION...")
        final_verif = execute_final_verification(store, repo, config)
        print("Final verification result:")
        print(f"  Passed: {final_verif.get('passed')}")
        print(f"  Verifiers run: {len(final_verif.get('evidence', {}).get('verifiers', []))}")
        assert final_verif.get("passed") is True, "Final deterministic verification failed!"

        print("\nFinal verification passed. Proceeding to TRUSTED CHECKPOINT CREATION...")
        ckpt = create_trusted_checkpoint(store, repo, final_verif, config)
        print("Trusted Checkpoint Created:")
        print(f"  Checkpoint ID: {ckpt.get('checkpoint_id')}")
        print(f"  Reference: {ckpt.get('reference')}")

        assert store.state.get("last_verified_checkpoint") is not None, "Missing last_verified_checkpoint!"
        assert len(store.state.get("verified_progress")) == 1, "Verified progress was not updated!"
        print(f"  Last verified checkpoint: {store.state['last_verified_checkpoint']['reference']}")
        print(f"  Verified progress count: {len(store.state['verified_progress'])}")

        # Resolve both durable review artifacts and exercise the restart validation path.
        checkpoint = ckpt["checkpoint"]
        assert checkpoint["critic"] == crit_result["artifact_ref"]
        assert checkpoint["reviewer"] == reviewer_result["artifact_ref"]
        reloaded = durable.Store(evidence_dir / "runs", "live-qual")
        reloaded.load()
        assert durable.validate_repository(reloaded, repo) == "trusted"
        assert validate_manager_repository(reloaded, repo) == "trusted"
        print("  Checkpoint reload and both durable review bindings: PASS")

    gateway.unload()
    print("\n=== LIVE GPT-OSS QUALIFICATION PASSED ===")
    return True

if __name__ == "__main__":
    success = run()
    sys.exit(0 if success else 1)
