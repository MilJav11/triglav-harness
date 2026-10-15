"""Deterministic tests for Wave 6: Senior Reviewer Layer & Final Verification for Trusted Checkpoint.

Validates the full 50-item test matrix:
1. reviewer cannot run before CRITIC_REVIEWED
2. stale critic blocks reviewer
3. reviewer runs after fresh CRITIC_REVIEWED
4. deterministic reviewer packet stable
5. packet contains critic evidence
6. packet contains verifier evidence
7. packet contains candidate binding
8. packet excludes checkpoint authority
9. packet bounded
10. credentials redacted
11. complete evidence hashes retained despite bounded excerpts
12. valid APPROVE parses
13. valid REJECT parses
14. APPROVE alone does not create checkpoint
15. APPROVE alone does not set VERIFIED
16. REJECT creates no checkpoint
17. malformed JSON terminal fail-closed
18. invalid decision fail-closed
19. duplicate keys fail-closed
20. unknown fields fail-closed
21. hidden reasoning wrapper fail-closed
22. forbidden trust fields fail-closed
23. oversized response fail-closed
24. timeout fail-closed
25. transport retry bounded
26. parse failure not retried
27. retry reservation survives restart
28. duplicate Manager execution idempotent
29. candidate mutation invalidates reviewer
30. critic change invalidates reviewer
31. verifier evidence change invalidates reviewer
32. environment change invalidates reviewer
33. reviewer identity change invalidates reviewer
34. same alias / different endpoint invalidates reviewer
35. generic reviewer adapter works
36. reviewer cannot mutate repo
37. ignored/.git mutation fails closed
38. cleanup failure prevents APPROVE acceptance
39. reviewer cannot grant worker attempt
40. reviewer cannot alter critic state
41. reviewer cannot alter verifier authority
42. final verification runs AFTER APPROVE
43. old verifier PASS cannot substitute final verification
44. final verification failure => no checkpoint
45. scope failure during final verification => no checkpoint
46. candidate changed after APPROVE => no checkpoint
47. environment changed after APPROVE => no checkpoint
48. verifier authority change => no checkpoint
49. controller creates trusted checkpoint only after all steps pass
50. trusted checkpoint updates last_verified_checkpoint and verified_progress
"""

import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
import uuid
from unittest.mock import patch
from pathlib import Path

import supervision

import critic_executor as ce
from critic_executor import (
    CRITIC_CLEAN,
    CRITIC_FINDINGS,
    CriticExecutor,
    ScriptedCritic,
)
import durable
from durable import digest, snapshot
import environment
import harness
import manager
from manager import Agents, create_run, execute, ManagerLoop
from test_harness import CONFIG
import work_unit_scheduler as wus
from work_unit_scheduler import ScriptedExecutor, WorkUnitScheduler
import work_units as wu

import reviewer_executor as re
from reviewer_executor import (
    REVIEWER_APPROVED,
    REVIEWER_REJECTED,
    REVIEWER_INCONCLUSIVE,
    REVIEWER_FAILED,
    REVIEWER_TIMEOUT,
    REVIEWER_UNPARSEABLE,
    REVIEWER_IN_PROGRESS,
    ReviewerCleanupError,
    ReviewerExecutionError,
    ReviewerExecutor,
    ReviewerParseError,
    ScriptedReviewer,
    build_reviewer_packet,
    create_trusted_checkpoint,
    effective_reviewer_identity,
    execute_final_verification,
    is_reviewer_stale,
    parse_reviewer_response,
    validate_acceptance_readiness,
)

TASK = "Wave 6 Senior Reviewer test task"
COMMANDS = [["python", "-m", "unittest", "test_calculator.py"]]

def build_verifier_registry():
    return {
        "check-calc": {
            "argv": ["python", "-m", "unittest", "test_calculator.py"],
            "timeout_seconds": 30,
        }
    }

class FakeGateway:
    def __init__(self, *args):
        self.events, self.loaded, self.peak = [], None, 0
        self.active_role = None
        self.active_model_child_id = None

    def unload(self):
        self.events.append("unload")
        self.loaded = None
        self.active_role = None
        self.active_model_child_id = None

    def running(self):
        return [self.loaded] if self.loaded else []

    def chat(self, role, messages=None, phase="x", max_tokens=1):
        self.loaded = role
        return "{}"

    def switch(self, role):
        self.active_role = role

def make_spec(
    unit_id="unit-1",
    objective="Implement multiply in calculator.py",
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

def setup_test_fixture(root, run_id="wave6-test-run"):
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

        def git(*args):
            return subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo_path),
                    "-c",
                    f"safe.directory={repo_path}",
                    "-c",
                    "user.name=W6 test",
                    "-c",
                    "user.email=w6@localhost",
                    *args,
                ],
                check=True,
                capture_output=True,
            )

        git("init", "-q")
        git("add", ".")
        git("commit", "-qm", "baseline")

    config = copy.deepcopy(CONFIG)
    config.update(
        max_retries=0,
        roles={**CONFIG["roles"], "critic": "llm-critic", "review": "llm-review"},
        model_metadata={
            "llm-critic": {
                "display_name": "Nemotron-3.5-Lightning-30B-A3B-Q4_K_M",
                "runtime": "llama.cpp",
            },
            "llm-review": {
                "display_name": "GPT-OSS-120B-Q4_K_M",
                "runtime": "HotPin",
            },
        },
        critic={"min_available_ram_gb": 30, "request_timeout_seconds": 180, "max_input_chars": 6000},
        reviewer={"min_available_ram_gb": 44, "request_timeout_seconds": 300, "max_input_chars": 8000},
    )
    log = harness.EventLog(root / f"{run_id}-preflight.jsonl")
    repo = harness.Repository(repo_path, config, log)
    store = durable.Store(root / "runs", run_id)
    return repo, store, config

class TestReviewerWave6(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo, self.store, self.config = setup_test_fixture(self.root)
        self.registry = build_verifier_registry()

    def tearDown(self):
        self.tmp.cleanup()

    def _create_critic_reviewed_run(self, run_id="cr-run", critic_findings=False, reviewer_option=True):
        store = durable.Store(self.root / "runs", run_id)
        spec = make_spec()
        create_run(
            store,
            self.repo,
            TASK,
            COMMANDS,
            self.config,
            criteria=["Multiply works"],
            work_units=[spec],
            verifier_registry=self.registry,
            critic=True,
            reviewer=reviewer_option,
        )
        executor = ScriptedExecutor()
        executor.set_behavior(
            "unit-1",
            {"writes": {"calculator.py": "def multiply(a, b):\n    return a * b\n"}},
        )
        scheduler = WorkUnitScheduler(
            store,
            self.repo,
            verifier_registry=self.registry,
            agents=Agents(None, executor, None, None),
            parent_scope={"allowed_paths": ["calculator.py"], "forbidden_paths": []},
        )
        res = scheduler.run_sequence()
        self.assertEqual(res["status"], "MILESTONE_READY")

        preset = "findings" if critic_findings else "clean"
        crit = CriticExecutor(store, self.repo, self.config, critic_adapter=ScriptedCritic(preset))
        crit_res = crit.execute()
        self.assertIn(crit_res["status"], (CRITIC_CLEAN, CRITIC_FINDINGS))
        self.assertEqual(store.state.get("manager", {}).get("milestone_status"), "CRITIC_REVIEWED")
        return store

    # 1. reviewer cannot run before CRITIC_REVIEWED
    def test_01_reviewer_cannot_run_before_critic_reviewed(self):
        store = durable.Store(self.root / "runs", "test-01")
        spec = make_spec()
        create_run(
            store,
            self.repo,
            TASK,
            COMMANDS,
            self.config,
            criteria=["Multiply works"],
            work_units=[spec],
            verifier_registry=self.registry,
            critic=True,
            reviewer=True,
        )
        # Not yet executed or critic-reviewed
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        with self.assertRaises(ReviewerExecutionError) as ctx:
            rev.execute()
        self.assertIn("not UNIT_VERIFIED", str(ctx.exception))

    # 2. stale critic blocks reviewer
    def test_02_stale_critic_blocks_reviewer(self):
        store = self._create_critic_reviewed_run("test-02")
        # Mutate the repository candidate after critic review
        self.repo.write("calculator.py", "def multiply(a, b):\n    return a * b # mutated\n")
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        with self.assertRaises(ReviewerExecutionError) as ctx:
            rev.execute()
        self.assertTrue(
            "critic review is stale" in str(ctx.exception)
            or "candidate repository changed" in str(ctx.exception)
        )

    # 3. reviewer runs after fresh CRITIC_REVIEWED
    def test_03_reviewer_runs_after_fresh_critic_reviewed(self):
        store = self._create_critic_reviewed_run("test-03")
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        res = rev.execute()
        self.assertEqual(res["status"], REVIEWER_APPROVED)
        self.assertEqual(res["decision"], "APPROVE")
        self.assertEqual(res["finding_count"], 0)

    # 4. deterministic reviewer packet stable
    def test_04_deterministic_reviewer_packet_stable(self):
        store = self._create_critic_reviewed_run("test-04")
        p1 = build_reviewer_packet(store, self.repo, self.config)
        p2 = build_reviewer_packet(store, self.repo, self.config)
        self.assertEqual(p1["packet_digest"], p2["packet_digest"])
        self.assertEqual(p1["review_binding"], p2["review_binding"])

    # 5. packet contains critic evidence
    def test_05_packet_contains_critic_evidence(self):
        store = self._create_critic_reviewed_run("test-05", critic_findings=True)
        packet = build_reviewer_packet(store, self.repo, self.config)
        self.assertIn("critic_evidence", packet)
        self.assertEqual(packet["critic_evidence"]["status"], CRITIC_FINDINGS)
        self.assertGreater(packet["critic_evidence"]["finding_count"], 0)

    # 6. packet contains verifier evidence
    def test_06_packet_contains_verifier_evidence(self):
        store = self._create_critic_reviewed_run("test-06")
        packet = build_reviewer_packet(store, self.repo, self.config)
        self.assertIn("deterministic_verifiers", packet)
        self.assertGreater(len(packet["deterministic_verifiers"]), 0)
        self.assertEqual(packet["deterministic_verifiers"][0]["outcome"], "PASSED")

    # 7. packet contains candidate binding
    def test_07_packet_contains_candidate_binding(self):
        store = self._create_critic_reviewed_run("test-07")
        packet = build_reviewer_packet(store, self.repo, self.config)
        self.assertIn("candidate_binding", packet)
        self.assertIn("candidate_fingerprint", packet["candidate_binding"])
        self.assertIn("candidate_snapshot_digest", packet["candidate_binding"])
        self.assertEqual(packet["candidate_binding"]["candidate_snapshot_digest"], digest(snapshot(self.repo)))

    # 8. packet excludes checkpoint authority
    def test_08_packet_excludes_checkpoint_authority(self):
        store = self._create_critic_reviewed_run("test-08")
        packet = build_reviewer_packet(store, self.repo, self.config)
        text = json.dumps(packet)
        for forbidden in ("trusted_checkpoint", "checkpoint_id", "approved_as_checkpoint"):
            self.assertNotIn(f'"{forbidden}"', text)

    # 9. packet bounded
    def test_09_packet_bounded(self):
        store = self._create_critic_reviewed_run("test-09")
        cfg = copy.deepcopy(self.config)
        cfg["reviewer"]["max_input_chars"] = 5500
        packet = build_reviewer_packet(store, self.repo, cfg)
        size = re._input_size(packet)
        self.assertLessEqual(size, 5500)

    # 10. credentials redacted
    def test_10_credentials_redacted(self):
        val = {
            "token": "secret_abc",
            "authorization": "Bearer secret_tok_123",
            "api_key": "sk-proj-1234567890abcdef12345678",
            "normal_field": "safe",
        }
        sanitized = re.sanitize_evidence(val)
        self.assertEqual(sanitized["token"], "[REDACTED]")
        self.assertEqual(sanitized["api_key"], "[REDACTED]")
        self.assertIn("[REDACTED]", sanitized["authorization"])
        self.assertEqual(sanitized["normal_field"], "safe")

        store = self._create_critic_reviewed_run("test-10")
        packet = build_reviewer_packet(store, self.repo, self.config)
        packet_with_cred = re.sanitize_evidence({
            "secret_key": "sk-proj-1234567890abcdef12345678",
            "review": packet,
        })
        self.assertEqual(packet_with_cred["secret_key"], "[REDACTED]")

    # 11. complete evidence hashes retained despite bounded excerpts
    def test_11_complete_evidence_hashes_retained_despite_bounded_excerpts(self):
        store = self._create_critic_reviewed_run("test-11")
        cfg_small = copy.deepcopy(self.config)
        cfg_small["reviewer"]["max_input_chars"] = 5500
        cfg_large = copy.deepcopy(self.config)
        cfg_large["reviewer"]["max_input_chars"] = 12000
        p_small = build_reviewer_packet(store, self.repo, cfg_small)
        p_large = build_reviewer_packet(store, self.repo, cfg_large)
        # Bounded excerpt text may differ, but review binding digest is based on un-excerpted hashes
        self.assertEqual(
            p_small["review_binding"]["candidate_snapshot_digest"],
            p_large["review_binding"]["candidate_snapshot_digest"],
        )

    # 12. valid APPROVE parses
    def test_12_valid_approve_parses(self):
        raw = json.dumps({
            "decision": "APPROVE",
            "findings": [],
            "summary": "Looks good.",
        })
        parsed = parse_reviewer_response(raw)
        self.assertEqual(parsed["decision"], "APPROVE")
        self.assertEqual(parsed["findings"], [])
        self.assertEqual(parsed["summary"], "Looks good.")

    # 13. valid REJECT parses
    def test_13_valid_reject_parses(self):
        raw = json.dumps({
            "decision": "REJECT",
            "findings": [
                {
                    "finding_id": "RF1",
                    "severity": "BLOCKER",
                    "title": "Bug",
                    "description": "Critical error found",
                    "evidence_refs": ["candidate_diff"],
                    "confidence": "HIGH",
                }
            ],
            "summary": "Rejected.",
        })
        parsed = parse_reviewer_response(raw)
        self.assertEqual(parsed["decision"], "REJECT")
        self.assertEqual(len(parsed["findings"]), 1)

    # 14. APPROVE alone does not create checkpoint
    def test_14_approve_alone_does_not_create_checkpoint(self):
        store = self._create_critic_reviewed_run("test-14")
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        res = rev.execute()
        self.assertEqual(res["status"], REVIEWER_APPROVED)
        self.assertIsNone(store.state.get("last_verified_checkpoint"))
        self.assertEqual(store.state.get("verified_progress"), [])

    # 15. APPROVE alone does not set VERIFIED
    def test_15_approve_alone_does_not_set_verified(self):
        store = self._create_critic_reviewed_run("test-15")
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev.execute()
        self.assertNotEqual(store.state.get("status"), "VERIFIED")
        self.assertNotEqual(store.state.get("manager", {}).get("phase"), "VERIFIED")

    # 16. REJECT creates no checkpoint
    def test_16_reject_creates_no_checkpoint(self):
        store = self._create_critic_reviewed_run("test-16")
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("reject"))
        res = rev.execute()
        self.assertEqual(res["status"], REVIEWER_REJECTED)
        self.assertIsNone(store.state.get("last_verified_checkpoint"))

    # 17. malformed JSON terminal fail-closed
    def test_17_malformed_json_terminal_fail_closed(self):
        store = self._create_critic_reviewed_run("test-17")
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("malformed_json"))
        res = rev.execute()
        self.assertEqual(res["status"], REVIEWER_UNPARSEABLE)
        self.assertEqual(res["attempt_count"], 1)  # Not retried

    # 18. invalid decision fail-closed
    def test_18_invalid_decision_fail_closed(self):
        store = self._create_critic_reviewed_run("test-18")
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("invalid_decision"))
        res = rev.execute()
        self.assertEqual(res["status"], REVIEWER_UNPARSEABLE)

    # 19. duplicate keys fail-closed
    def test_19_duplicate_keys_fail_closed(self):
        raw = '{"decision": "APPROVE", "decision": "REJECT", "findings": [], "summary": "dup"}'
        with self.assertRaises(ReviewerParseError) as ctx:
            parse_reviewer_response(raw)
        self.assertIn("Duplicate reviewer JSON field", str(ctx.exception))

    # 20. unknown fields fail-closed
    def test_20_unknown_fields_fail_closed(self):
        raw = json.dumps({
            "decision": "APPROVE",
            "findings": [],
            "summary": "ok",
            "extra_field": "unauthorized",
        })
        with self.assertRaises(ReviewerParseError) as ctx:
            parse_reviewer_response(raw)
        self.assertIn("Unexpected or missing", str(ctx.exception))

    # 21. hidden reasoning wrapper fail-closed
    def test_21_hidden_reasoning_wrapper_fail_closed(self):
        store = self._create_critic_reviewed_run("test-21")
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("hidden_reasoning"))
        res = rev.execute()
        self.assertEqual(res["status"], REVIEWER_UNPARSEABLE)

    # 22. forbidden trust fields fail-closed
    def test_22_forbidden_trust_fields_fail_closed(self):
        store = self._create_critic_reviewed_run("test-22")
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("unexpected_trusted_fields"))
        res = rev.execute()
        self.assertEqual(res["status"], REVIEWER_UNPARSEABLE)

    # 23. oversized response fail-closed
    def test_23_oversized_response_fail_closed(self):
        store = self._create_critic_reviewed_run("test-23")
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("oversized_output"))
        res = rev.execute()
        self.assertEqual(res["status"], REVIEWER_UNPARSEABLE)

    # 24. timeout fail-closed
    def test_24_timeout_fail_closed(self):
        store = self._create_critic_reviewed_run("test-24")
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("timeout"))
        res = rev.execute()
        self.assertEqual(res["status"], REVIEWER_TIMEOUT)

    # 25. transport retry bounded
    def test_25_transport_retry_bounded(self):
        store = self._create_critic_reviewed_run("test-25")
        mock = ScriptedReviewer("transport_error")
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=mock)
        res = rev.execute()
        self.assertEqual(res["status"], REVIEWER_FAILED)
        self.assertEqual(res["attempt_count"], 2)  # 1 initial + 1 retry = 2 attempts
        self.assertEqual(len(mock.calls), 2)

    # 26. parse failure not retried
    def test_26_parse_failure_not_retried(self):
        store = self._create_critic_reviewed_run("test-26")
        mock = ScriptedReviewer("malformed_json")
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=mock)
        res = rev.execute()
        self.assertEqual(res["status"], REVIEWER_UNPARSEABLE)
        self.assertEqual(res["attempt_count"], 1)
        self.assertEqual(len(mock.calls), 1)

    # 27. retry reservation survives restart
    def test_27_retry_reservation_survives_restart(self):
        store = self._create_critic_reviewed_run("test-27")
        packet = build_reviewer_packet(store, self.repo, self.config)
        identity = effective_reviewer_identity(self.config)
        key = digest({"packet_digest": packet["packet_digest"], "model_identity": identity})
        # Simulate interrupted reserved attempt
        record = {
            "review_id": "rev-interrupted",
            "attempt_count": 1,
            "attempts": [{"number": 1, "status": "reserved"}],
            "status": REVIEWER_IN_PROGRESS,
            "review_packet_digest": packet["packet_digest"],
            "model_identity": identity,
        }
        mgr = copy.deepcopy(store.state.get("manager", {}))
        mgr.setdefault("reviewer_executions", {})[key] = record
        store.commit(manager=mgr)

        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        res = rev.execute()
        self.assertEqual(res["status"], REVIEWER_FAILED)
        self.assertIn("Interrupted senior reviewer invocation", res["raw_diagnostics"])

    # 28. duplicate Manager execution idempotent
    def test_28_duplicate_manager_execution_idempotent(self):
        store = self._create_critic_reviewed_run("test-28")
        mock = ScriptedReviewer("approve")
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=mock)
        res1 = rev.execute()
        res2 = rev.execute()
        self.assertEqual(res1["review_id"], res2["review_id"])
        self.assertEqual(len(mock.calls), 1)

    # 29. candidate mutation invalidates reviewer
    def test_29_candidate_mutation_invalidates_reviewer(self):
        store = self._create_critic_reviewed_run("test-29")
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev.execute()
        self.assertFalse(is_reviewer_stale(store, self.repo, self.config))
        # Candidate changes
        self.repo.write("calculator.py", "# new code\n")
        self.assertTrue(is_reviewer_stale(store, self.repo, self.config))

    # 30. critic change invalidates reviewer
    def test_30_critic_change_invalidates_reviewer(self):
        store = self._create_critic_reviewed_run("test-30")
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev.execute()
        self.assertFalse(is_reviewer_stale(store, self.repo, self.config))
        # Alter critic review in manager
        mgr = copy.deepcopy(store.state.get("manager", {}))
        mgr["critic_review"]["summary"] = "Tampered critic summary"
        store.commit(manager=mgr)
        self.assertTrue(is_reviewer_stale(store, self.repo, self.config))

    # 31. verifier evidence change invalidates reviewer
    def test_31_verifier_evidence_change_invalidates_reviewer(self):
        store = self._create_critic_reviewed_run("test-31")
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev.execute()
        self.assertFalse(is_reviewer_stale(store, self.repo, self.config))
        # Alter work unit verifier evidence in a valid sealed way
        wu_sec = copy.deepcopy(store.state.get("work_units", {}))
        unit = wu_sec["units"]["unit-1"]
        unit["result"]["verifier_evidence"]["verifiers"]["check-calc"]["stdout"] = "new stdout evidence"
        wu.seal_unit_state(unit)
        wu._reseal_section(wu_sec)
        store.commit(work_units=wu_sec)
        self.assertTrue(is_reviewer_stale(store, self.repo, self.config))

    # 32. environment change invalidates reviewer
    def test_32_environment_change_invalidates_reviewer(self):
        store = self._create_critic_reviewed_run("test-32")
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev.execute()
        self.assertFalse(is_reviewer_stale(store, self.repo, self.config))
        # Change environment with valid checksum
        env = copy.deepcopy(store.state.get("environment", {}))
        env["harness_head"] = "0000000000000000000000000000000000000000"
        env["sha256"] = environment.checksum({k: v for k, v in env.items() if k != "sha256"})
        store.commit(environment=env)
        self.assertTrue(is_reviewer_stale(store, self.repo, self.config))

    # 33. reviewer identity change invalidates reviewer
    def test_33_reviewer_identity_change_invalidates_reviewer(self):
        store = self._create_critic_reviewed_run("test-33")
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev.execute()
        self.assertFalse(is_reviewer_stale(store, self.repo, self.config))
        # Change reviewer model
        cfg_b = copy.deepcopy(self.config)
        cfg_b["roles"]["review"] = "llm-review-b"
        cfg_b["model_metadata"]["llm-review-b"] = {
            "display_name": "Reviewer-Model-B",
            "runtime": "vllm",
        }
        self.assertTrue(is_reviewer_stale(store, self.repo, cfg_b))

    # 34. same alias / different endpoint invalidates reviewer
    def test_34_same_alias_different_endpoint_invalidates_reviewer(self):
        store = self._create_critic_reviewed_run("test-34")
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev.execute()
        self.assertFalse(is_reviewer_stale(store, self.repo, self.config))
        # Same alias, different base_url
        cfg_endpoint = copy.deepcopy(self.config)
        cfg_endpoint["base_url"] = "http://127.0.0.1:9393"
        self.assertTrue(is_reviewer_stale(store, self.repo, cfg_endpoint))

    # 35. generic reviewer adapter works
    def test_35_generic_reviewer_adapter_works(self):
        store = self._create_critic_reviewed_run("test-35")
        # Custom adapter with callable protocol
        def custom_adapter(packet):
            return json.dumps({
                "decision": "APPROVE",
                "findings": [],
                "summary": "Custom adapter approval",
            })
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=custom_adapter)
        res = rev.execute()
        self.assertEqual(res["status"], REVIEWER_APPROVED)
        self.assertEqual(res["summary"], "Custom adapter approval")

    # 36. reviewer cannot mutate repo
    def test_36_reviewer_cannot_mutate_repo(self):
        store = self._create_critic_reviewed_run("test-36")
        def mutating_adapter(packet):
            # Illegally write to repo
            p = Path(self.repo.root) / "leak.txt"
            p.write_text("illegal mutation\n")
            return json.dumps({"decision": "APPROVE", "findings": [], "summary": "ok"})
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=mutating_adapter)
        with self.assertRaises(ReviewerExecutionError) as ctx:
            rev.execute()
        self.assertIn("illegally mutated repository files", str(ctx.exception))

    # 37. ignored/.git mutation fails closed
    def test_37_ignored_or_git_mutation_fails_closed(self):
        store = self._create_critic_reviewed_run("test-37")
        def git_mutating_adapter(packet):
            git_file = Path(self.repo.root) / ".git" / "tamper.tmp"
            git_file.write_text("tamper\n")
            return json.dumps({"decision": "APPROVE", "findings": [], "summary": "ok"})
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=git_mutating_adapter)
        with self.assertRaises(ReviewerExecutionError):
            rev.execute()

    # 38. cleanup failure prevents APPROVE acceptance
    def test_38_cleanup_failure_prevents_approve_acceptance(self):
        store = self._create_critic_reviewed_run("test-38")
        class MockGatewayWithBadCleanup:
            def switch(self, role): pass
            def chat(self, role, msgs, phase, max_tokens):
                return json.dumps({"decision": "APPROVE", "findings": [], "summary": "ok"})
            def unload(self):
                raise RuntimeError("Failed to unload model")
        rev = ReviewerExecutor(store, self.repo, self.config, gateway=MockGatewayWithBadCleanup())
        res = rev.execute()
        self.assertEqual(res["status"], REVIEWER_FAILED)
        self.assertEqual(res["parse_status"], "cleanup_failed")

    # 39. reviewer cannot grant worker attempt
    def test_39_reviewer_cannot_grant_worker_attempt(self):
        store = self._create_critic_reviewed_run("test-39")
        wu_before = copy.deepcopy(store.state["work_units"])
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("reject"))
        rev.execute()
        wu_after = store.state["work_units"]
        self.assertEqual(wu_before, wu_after)

    # 40. reviewer cannot alter critic state
    def test_40_reviewer_cannot_alter_critic_state(self):
        store = self._create_critic_reviewed_run("test-40")
        critic_before = copy.deepcopy(store.state["manager"]["critic_review"])
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("reject"))
        rev.execute()
        critic_after = store.state["manager"]["critic_review"]
        self.assertEqual(critic_before, critic_after)

    # 41. reviewer cannot alter verifier authority
    def test_41_reviewer_cannot_alter_verifier_authority(self):
        store = self._create_critic_reviewed_run("test-41")
        reg_before = copy.deepcopy(store.state.get("options", {}).get("verifier_registry"))
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev.execute()
        reg_after = store.state.get("options", {}).get("verifier_registry")
        self.assertEqual(reg_before, reg_after)

    # 42. final verification runs AFTER APPROVE
    def test_42_final_verification_runs_after_approve(self):
        store = self._create_critic_reviewed_run("test-42")
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev.execute()
        final_res = execute_final_verification(store, self.repo, self.config)
        self.assertTrue(final_res["passed"])
        self.assertIn("final-verif-", final_res["reference"]["path"])

    # 43. old verifier PASS cannot substitute final verification
    def test_43_old_verifier_pass_cannot_substitute_final_verification(self):
        store = self._create_critic_reviewed_run("test-43")
        # Attempt to create checkpoint with fake old verifier pass
        with self.assertRaises(ReviewerExecutionError) as ctx:
            create_trusted_checkpoint(store, self.repo, {"passed": False, "reference": None})
        self.assertIn("final verification did not pass", str(ctx.exception))

    # 44. final verification failure => no checkpoint
    def test_44_final_verification_failure_no_checkpoint(self):
        store = self._create_critic_reviewed_run("test-44")
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev.execute()
        # Tamper with implementation so verifier fails
        self.repo.write("calculator.py", "def multiply(a, b):\n    return -999\n")
        # Note: changing the file also makes candidate snapshot mismatch
        with self.assertRaises(ReviewerExecutionError):
            execute_final_verification(store, self.repo, self.config)

    # 45. scope failure during final verification => no checkpoint
    def test_45_scope_failure_during_final_verification_no_checkpoint(self):
        store = self._create_critic_reviewed_run("test-45")
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev.execute()
        # Add a forbidden path that covers calculator.py
        mgr = copy.deepcopy(store.state.get("manager", {}))
        mgr["scope"] = {"allowed_paths": [], "forbidden_paths": ["calculator.py"]}
        store.commit(manager=mgr)
        with self.assertRaises(ReviewerExecutionError) as ctx:
            execute_final_verification(store, self.repo, self.config)
        self.assertIn("Scope violation", str(ctx.exception))

    # 46. candidate changed after APPROVE => no checkpoint
    def test_46_candidate_changed_after_approve_no_checkpoint(self):
        store = self._create_critic_reviewed_run("test-46")
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev.execute()
        # Change file after APPROVE
        self.repo.write("calculator.py", "def multiply(a, b):\n    return a * b # modified\n")
        with self.assertRaises(ReviewerExecutionError) as ctx:
            execute_final_verification(store, self.repo, self.config)
        self.assertIn("Candidate snapshot changed", str(ctx.exception))

    # 47. environment changed after APPROVE => no checkpoint
    def test_47_environment_changed_after_approve_no_checkpoint(self):
        store = self._create_critic_reviewed_run("test-47")
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev.execute()
        # Change environment
        env = copy.deepcopy(store.state.get("environment", {}))
        env["harness_head"] = "ffffffffffffffffffffffffffffffffffffffff"
        env["sha256"] = environment.checksum({k: v for k, v in env.items() if k != "sha256"})
        store.commit(environment=env)
        with self.assertRaises(ReviewerExecutionError) as ctx:
            execute_final_verification(store, self.repo, self.config)
        self.assertIn("Reviewer result is stale", str(ctx.exception))

    # 48. verifier authority change => no checkpoint
    def test_48_verifier_authority_change_no_checkpoint(self):
        store = self._create_critic_reviewed_run("test-48")
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev.execute()
        # Alter verifier registry
        opts = copy.deepcopy(store.state.get("options", {}))
        opts["verifier_registry"]["check-calc"]["argv"] = ["tampered-cmd"]
        store.commit(options=opts)
        # Final verification attempts to run new authority
        final_res = execute_final_verification(store, self.repo, self.config)
        self.assertFalse(final_res["passed"])

    # 49. controller creates trusted checkpoint only after all steps pass
    def test_49_controller_creates_trusted_checkpoint_only_after_all_steps_pass(self):
        store = self._create_critic_reviewed_run("test-49")
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev.execute()
        final_res = execute_final_verification(store, self.repo, self.config)
        self.assertTrue(final_res["passed"])
        ckpt_res = create_trusted_checkpoint(store, self.repo, final_res, self.config)
        self.assertIn("ckpt-milestone-", ckpt_res["checkpoint_id"])
        self.assertIsNotNone(store.state.get("last_verified_checkpoint"))

    # 50. trusted checkpoint updates last_verified_checkpoint and verified_progress
    def test_50_trusted_checkpoint_updates_last_verified_checkpoint_and_verified_progress(self):
        store = self._create_critic_reviewed_run("test-50")
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev.execute()
        final_res = execute_final_verification(store, self.repo, self.config)
        create_trusted_checkpoint(store, self.repo, final_res, self.config)

        lvc = store.state.get("last_verified_checkpoint")
        self.assertIsNotNone(lvc)
        self.assertEqual(lvc["snapshot"]["head"], snapshot(self.repo)["head"])

        vp = store.state.get("verified_progress")
        self.assertEqual(len(vp), 1)
        self.assertEqual(vp[0]["task"], TASK)
        self.assertEqual(vp[0]["checkpoint"], lvc["reference"])

    # 51. full manager execution integration test
    def test_51_full_manager_wave6_flow(self):
        store = durable.Store(self.root / "runs", "test-51")
        spec = make_spec()
        create_run(
            store,
            self.repo,
            TASK,
            COMMANDS,
            self.config,
            criteria=["Multiply works"],
            work_units=[spec],
            verifier_registry=self.registry,
            critic=True,
            reviewer=True,
        )
        executor = ScriptedExecutor()
        executor.set_behavior(
            "unit-1",
            {"writes": {"calculator.py": "def multiply(a, b):\n    return a * b\n"}},
        )
        critic = ScriptedCritic("clean")
        reviewer = ScriptedReviewer("approve")
        agents = Agents(None, executor, critic, reviewer)
        log = harness.EventLog(self.root / "test-51-manager.jsonl")

        outcome = execute(store, agents=agents, gateway=FakeGateway())
        self.assertEqual(outcome["status"], "COMPLETED")
        self.assertEqual(outcome["stop"]["reason"], "ALL_CRITERIA_PROVEN")
        self.assertIsNotNone(store.state.get("last_verified_checkpoint"))
        self.assertEqual(len(store.state.get("verified_progress")), 1)

    # 52. INCONCLUSIVE decision fails closed without checkpoint
    def test_52_inconclusive_decision_fails_closed_no_checkpoint(self):
        store = self._create_critic_reviewed_run("test-52")
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("inconclusive"))
        res = rev.execute()
        self.assertEqual(res["status"], REVIEWER_INCONCLUSIVE)
        self.assertEqual(res["decision"], "INCONCLUSIVE")
        self.assertIsNone(store.state.get("last_verified_checkpoint"))
        with self.assertRaises(ReviewerExecutionError) as ctx:
            execute_final_verification(store, self.repo, self.config)
        self.assertIn("only run after reviewer APPROVE", str(ctx.exception))

    # 53. checkpoint creation is idempotent
    def test_53_checkpoint_creation_is_idempotent(self):
        store = self._create_critic_reviewed_run("test-53")
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev.execute()
        final_res = execute_final_verification(store, self.repo, self.config)
        ckpt1 = create_trusted_checkpoint(store, self.repo, final_res, self.config)
        ckpt2 = create_trusted_checkpoint(store, self.repo, final_res, self.config)
        self.assertEqual(ckpt1["checkpoint_id"], ckpt2["checkpoint_id"])
        self.assertEqual(ckpt1["reference"], ckpt2["reference"])
        self.assertEqual(len(store.state.get("verified_progress")), 1)

    # 54. crash during reviewer call never creates approval on reload
    def test_54_restart_during_reviewer_call_never_becomes_approval(self):
        store = self._create_critic_reviewed_run("test-54")
        class CrashError(BaseException): pass
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer(rules={1: CrashError("simulated crash")}))
        with self.assertRaises(CrashError):
            rev.execute()
        reloaded = durable.Store(self.root / "runs", "test-54")
        reloaded.load()
        mgr = reloaded.state.get("manager", {})
        self.assertNotEqual(mgr.get("reviewer_status"), REVIEWER_APPROVED)
        self.assertIsNone(reloaded.state.get("last_verified_checkpoint"))

    def _checkpoint_repair_fixture(self, run_id):
        store = self._create_critic_reviewed_run(run_id)
        reviewer = ReviewerExecutor(store, self.repo, self.config,
                                    reviewer_adapter=ScriptedReviewer("approve"))
        result = reviewer.execute()
        self.assertEqual(reviewer.execute(), result)
        critic = store.state["manager"]["critic_review"]
        self.assertEqual(CriticExecutor(store, self.repo, self.config,
                         critic_adapter=ScriptedCritic("clean")).execute(), critic)
        final = execute_final_verification(store, self.repo, self.config)
        return store, final

    def test_checkpoint_repair_reload_validates_both_durable_review_refs(self):
        store, final = self._checkpoint_repair_fixture("checkpoint-reload")
        checkpoint = create_trusted_checkpoint(store, self.repo, final, self.config)["checkpoint"]
        for role, key in (("critic", "critic_review"), ("reviewer", "reviewer_result")):
            result = store.state["manager"][key]
            self.assertEqual(checkpoint[role], result["artifact_ref"])
            self.assertEqual(store.read_review_evidence(result), checkpoint[role])
            self.assertIn(checkpoint[role], store.state["evidence"][role])
        reloaded = durable.Store(self.root / "runs", store.state["run_id"])
        reloaded.load()
        self.assertEqual(durable.validate_repository(reloaded, self.repo), "trusted")
        self.assertEqual(manager.validate_repository(reloaded, self.repo), "trusted")
        self.assertEqual(create_trusted_checkpoint(reloaded, self.repo, final, self.config)["checkpoint"], checkpoint)

    def _assert_checkpoint_review_artifact_fails_closed(self, role, delete):
        store, final = self._checkpoint_repair_fixture(f"checkpoint-{role}-{delete}")
        checkpoint = create_trusted_checkpoint(store, self.repo, final, self.config)["checkpoint"]
        reloaded = durable.Store(self.root / "runs", store.state["run_id"])
        reloaded.load()
        path = store.directory / checkpoint[role]["path"]
        if delete:
            path.unlink()
        else:
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["summary"] = "tampered"
            path.write_text(json.dumps(payload), encoding="utf-8")
        for validator in (durable.validate_repository, manager.validate_repository):
            with self.assertRaises(durable.DurableError):
                validator(reloaded, self.repo)
        with self.assertRaises(durable.DurableError):
            durable.Store(self.root / "runs", store.state["run_id"]).load()
        # The idempotent checkpoint path must also reject damaged evidence.
        with self.assertRaises(durable.DurableError):
            create_trusted_checkpoint(reloaded, self.repo, final, self.config)

    def test_checkpoint_repair_tampered_critic_fails_closed(self):
        self._assert_checkpoint_review_artifact_fails_closed("critic", False)

    def test_checkpoint_repair_deleted_critic_fails_closed(self):
        self._assert_checkpoint_review_artifact_fails_closed("critic", True)

    def test_checkpoint_repair_tampered_reviewer_fails_closed(self):
        self._assert_checkpoint_review_artifact_fails_closed("reviewer", False)

    def test_checkpoint_repair_deleted_reviewer_fails_closed(self):
        self._assert_checkpoint_review_artifact_fails_closed("reviewer", True)

    def test_checkpoint_repair_missing_review_refs_prevent_creation(self):
        store, final = self._checkpoint_repair_fixture("checkpoint-missing-refs")
        original = copy.deepcopy(store.state["manager"])
        for role, key in (("critic", "critic_review"), ("reviewer", "reviewer_result")):
            for invalid in (None, "missing", {}, {"path": "missing.json", "sha256": "0" * 64}):
                with self.subTest(role=role, invalid=invalid):
                    mgr = copy.deepcopy(original)
                    if invalid == "missing":
                        mgr[key].pop("artifact_ref")
                    else:
                        mgr[key]["artifact_ref"] = invalid
                    store.commit(manager=mgr)
                    with self.assertRaises(durable.DurableError):
                        create_trusted_checkpoint(store, self.repo, final, self.config)
                    self.assertIsNone(store.state["last_verified_checkpoint"])
                    self.assertEqual(list((store.directory / "checkpoints").glob("*.json")), [])

    def test_checkpoint_repair_null_review_refs_rejected_on_reload(self):
        store, final = self._checkpoint_repair_fixture("checkpoint-null-refs")
        checkpoint = create_trusted_checkpoint(store, self.repo, final, self.config)["checkpoint"]
        for role in ("critic", "reviewer"):
            for missing in (False, True):
                with self.subTest(role=role, missing=missing):
                    broken = copy.deepcopy(checkpoint)
                    if missing:
                        broken.pop(role)
                    else:
                        broken[role] = None
                    ref = store.artifact("checkpoints/broken.json", broken)
                    store.commit(last_verified_checkpoint={"reference": ref, "snapshot": broken["snapshot"]})
                    for validator in (durable.validate_repository, manager.validate_repository):
                        with self.assertRaises(durable.DurableError):
                            validator(store, self.repo)
                    with self.assertRaises(durable.DurableError):
                        durable.Store(self.root / "runs", store.state["run_id"]).load()

    def test_live_qualification_cached_critic_manager_continuation(self):
        # Exact live failure: canonical fixture reaches CRITIC_REVIEWED, then a
        # fresh Manager invocation revalidates the scheduler before reusing critic.
        for findings in (False, True):
            with self.subTest(findings=findings):
                self.repo, _, self.config = setup_test_fixture(self.root / f"continuation-findings-{findings}")
                store = self._create_critic_reviewed_run(
                    f"live-cached-continuation-{findings}", critic_findings=findings)
                original = copy.deepcopy(store.state["manager"]["critic_review"])
                reloaded = durable.Store(self.root / "runs", store.state["run_id"])
                reloaded.load()
                critic = ScriptedCritic(rules={1: AssertionError("Cached critic must not invoke model")})
                def approve(packet):
                    self.assertEqual(reloaded.state["manager"]["milestone_status"], "CRITIC_REVIEWED")
                    self.assertEqual(reloaded.state["manager"]["critic_review"], original)
                    self.assertEqual(critic.calls, [])
                    self.assertIsNone(reloaded.state["last_verified_checkpoint"])
                    self.assertEqual(reloaded.state["verified_progress"], [])
                    return json.dumps({"decision": "APPROVE", "findings": [], "summary": "Approved"})
                reviewer = ScriptedReviewer(rules={1: approve})
                result = execute(reloaded, agents=Agents(None, ScriptedExecutor(), critic, reviewer),
                                 gateway=FakeGateway())
                self.assertEqual(result["status"], "COMPLETED")
                self.assertEqual(len(critic.calls), 0)
                self.assertEqual(len(reviewer.calls), 1)
                self.assertEqual(reloaded.state["manager"]["critic_review"], original)
                self.assertEqual(len(reloaded.state["verified_progress"]), 1)

    def _revalidate_cached_critic_fixture(self, store):
        result = WorkUnitScheduler(
            store, self.repo, verifier_registry=self.registry,
            agents=Agents(None, ScriptedExecutor(), None, None),
            parent_scope={"allowed_paths": ["calculator.py"], "forbidden_paths": []},
        ).run_sequence()
        self.assertEqual(result["status"], "MILESTONE_READY")
        self.assertEqual(store.state["manager"]["milestone_status"], "MILESTONE_READY")

    def test_cached_critic_continuation_changed_bindings_require_new_review(self):
        for change in ("candidate", "evidence", "model"):
            with self.subTest(change=change):
                self.repo, _, self.config = setup_test_fixture(self.root / f"continuation-change-{change}")
                store = self._create_critic_reviewed_run(f"cached-stale-{change}")
                original = copy.deepcopy(store.state["manager"]["critic_review"])
                if change == "candidate":
                    self.repo.write("calculator.py", "def multiply(a, b):\n    # changed candidate\n    return a * b\n")
                self._revalidate_cached_critic_fixture(store)
                cfg = copy.deepcopy(self.config)
                if change == "evidence":
                    section = copy.deepcopy(store.state["work_units"])
                    unit = section["units"]["unit-1"]
                    unit["result"]["verifier_evidence"]["verifiers"]["check-calc"]["stdout"] = "changed evidence"
                    wu.seal_unit_state(unit)
                    wu._reseal_section(section)
                    store.commit(work_units=section)
                elif change == "model":
                    cfg["model_metadata"]["llm-critic"]["display_name"] = "replacement-critic"
                    options = copy.deepcopy(store.state["options"])
                    options["config"] = cfg
                    store.commit(options=options)
                self.assertTrue(ce.is_critic_stale(store, self.repo, cfg))
                reviewer = ScriptedReviewer("approve")
                with self.assertRaises(ReviewerExecutionError):
                    ReviewerExecutor(store, self.repo, cfg, reviewer_adapter=reviewer).execute()
                self.assertEqual(reviewer.calls, [])
                def new_review(packet):
                    self.assertEqual(store.state["manager"]["milestone_status"], "MILESTONE_READY")
                    return json.dumps({"findings": [], "summary": "Fresh review"})
                critic = ScriptedCritic(rules={1: new_review})
                result = CriticExecutor(store, self.repo, cfg, critic_adapter=critic).execute()
                self.assertEqual(len(critic.calls), 1)
                self.assertNotEqual(result["review_packet_digest"], original["review_packet_digest"])
                self.assertEqual(store.state["manager"]["milestone_status"], "CRITIC_REVIEWED")
                self.assertFalse(ce.is_critic_stale(store, self.repo, cfg))

    def test_cached_critic_continuation_incomplete_units_cannot_restore(self):
        for status in ("UNIT_FAILED", "PROPOSED"):
            with self.subTest(status=status):
                self.repo, _, self.config = setup_test_fixture(self.root / f"continuation-status-{status}")
                store = self._create_critic_reviewed_run(f"cached-incomplete-{status}")
                self._revalidate_cached_critic_fixture(store)
                section = copy.deepcopy(store.state["work_units"])
                unit = section["units"]["unit-1"]
                unit["status"] = status
                wu.seal_unit_state(unit)
                wu._reseal_section(section)
                store.commit(work_units=section)
                critic = ScriptedCritic("clean")
                with self.assertRaises(ce.CriticExecutionError):
                    CriticExecutor(store, self.repo, self.config, critic_adapter=critic).execute()
                self.assertEqual(critic.calls, [])
                self.assertEqual(store.state["manager"]["milestone_status"], "MILESTONE_READY")
                reviewer = ScriptedReviewer("approve")
                with self.assertRaises(ReviewerExecutionError):
                    ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=reviewer).execute()
                self.assertEqual(reviewer.calls, [])

    def test_cached_critic_continuation_damaged_artifact_cannot_restore(self):
        store = self._create_critic_reviewed_run("cached-damaged-artifact")
        self._revalidate_cached_critic_fixture(store)
        ref = store.state["manager"]["critic_review"]["artifact_ref"]
        path = store.directory / ref["path"]
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["summary"] = "tampered durable critic evidence"
        path.write_text(json.dumps(payload), encoding="utf-8")
        critic = ScriptedCritic("clean")
        with self.assertRaises(durable.DurableError):
            CriticExecutor(store, self.repo, self.config, critic_adapter=critic).execute()
        self.assertEqual(critic.calls, [])
        self.assertEqual(store.state["manager"]["milestone_status"], "MILESTONE_READY")

    def _completed_wave6_manager_run(self, run_id):
        store = durable.Store(self.root / "runs", run_id)
        create_run(
            store, self.repo, TASK, COMMANDS, self.config,
            criteria=["Multiply works"], work_units=[make_spec()],
            verifier_registry=self.registry, critic=True, reviewer=True,
        )
        worker = ScriptedExecutor()
        worker.set_behavior("unit-1", {
            "writes": {"calculator.py": "def multiply(a, b):\n    return a * b\n"},
        })
        agents = Agents(None, worker, ScriptedCritic("clean"), ScriptedReviewer("approve"))
        self.assertEqual(execute(store, agents=agents, gateway=FakeGateway())["status"], "COMPLETED")
        reloaded = durable.Store(self.root / "runs", run_id)
        reloaded.load()
        return reloaded, agents

    def test_completed_wave6_manager_reruns_are_terminal_after_reload(self):
        from unittest.mock import patch
        store, agents = self._completed_wave6_manager_run("completed-reruns")
        trusted = copy.deepcopy(store.state["last_verified_checkpoint"])
        progress = copy.deepcopy(store.state["verified_progress"])
        evidence = copy.deepcopy(store.state["evidence"])
        reviews = copy.deepcopy({
            key: store.state["manager"][key]
            for key in ("milestone_status", "critic_status", "critic_review",
                        "reviewer_status", "reviewer_result")
        })
        checkpoints = {
            path.name: path.read_bytes()
            for path in (store.directory / "checkpoints").glob("*.json")
        }
        critic_calls, reviewer_calls = len(agents.critic.calls), len(agents.reviewer.calls)
        self.assertEqual((critic_calls, reviewer_calls), (1, 1))
        with patch.object(ManagerLoop, "run_work_units") as scheduler, \
                patch.object(ManagerLoop, "run_critic") as critic, \
                patch.object(ManagerLoop, "run_reviewer") as reviewer, \
                patch.object(ManagerLoop, "run_final_verification") as final, \
                patch.object(ManagerLoop, "create_trusted_checkpoint") as checkpoint:
            for rerun in (1, 2):
                with self.subTest(rerun=rerun):
                    result = execute(store, agents=agents, gateway=FakeGateway())
                    self.assertEqual(result["status"], "COMPLETED")
                    self.assertEqual(store.state["status"], "COMPLETED")
                    self.assertEqual(store.state["last_verified_checkpoint"], trusted)
                    self.assertEqual(store.state["verified_progress"], progress)
                    self.assertEqual(store.state["evidence"], evidence)
                    self.assertEqual({key: store.state["manager"][key] for key in reviews}, reviews)
                    self.assertEqual({
                        path.name: path.read_bytes()
                        for path in (store.directory / "checkpoints").glob("*.json")
                    }, checkpoints)
                    self.assertEqual(len(checkpoints), 1)
                    self.assertEqual((len(agents.critic.calls), len(agents.reviewer.calls)),
                                     (critic_calls, reviewer_calls))
                    for gate in (scheduler, critic, reviewer, final, checkpoint):
                        gate.assert_not_called()

    def test_completed_wave6_changed_candidate_cannot_use_terminal_shortcut(self):
        from unittest.mock import patch
        store, agents = self._completed_wave6_manager_run("completed-candidate-drift")
        trusted = copy.deepcopy(store.state["last_verified_checkpoint"])
        self.repo.write("calculator.py", "def multiply(a, b):\n    return 0\n")
        with patch.object(ManagerLoop, "run_work_units") as scheduler:
            result = execute(store, agents=agents, gateway=FakeGateway())
        self.assertEqual(result["status"], "HUMAN_ACTION_REQUIRED")
        self.assertEqual(result["stop"]["reason"], "REPOSITORY_DRIFT")
        self.assertEqual(store.state["last_verified_checkpoint"], trusted)
        scheduler.assert_not_called()

    def test_completed_wave6_tampered_evidence_cannot_use_terminal_shortcut(self):
        from unittest.mock import patch
        store, agents = self._completed_wave6_manager_run("completed-evidence-drift")
        trusted = store.read_evidence(store.state["last_verified_checkpoint"]["reference"])
        path = store.directory / trusted["reviewer"]["path"]
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["summary"] = "changed after reload"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with patch.object(ManagerLoop, "run_work_units") as scheduler:
            result = execute(store, agents=agents, gateway=FakeGateway())
        self.assertEqual(result["status"], "HUMAN_ACTION_REQUIRED")
        self.assertEqual(result["stop"]["reason"], "REPOSITORY_DRIFT")
        scheduler.assert_not_called()

    def test_completed_wave6_changed_environment_cannot_use_terminal_shortcut(self):
        from unittest.mock import patch
        store, agents = self._completed_wave6_manager_run("completed-environment-drift")
        options = copy.deepcopy(store.state["options"])
        options["config"]["max_retries"] += 1
        store.commit(options=options)
        with patch.object(ManagerLoop, "run_work_units") as scheduler:
            with self.assertRaisesRegex(durable.DurableError, "ENVIRONMENT_DRIFT"):
                execute(store, agents=agents, gateway=FakeGateway())
        scheduler.assert_not_called()

    def test_completed_wave6_ignored_candidate_change_cannot_use_terminal_shortcut(self):
        from unittest.mock import patch
        store, agents = self._completed_wave6_manager_run("completed-ignored-drift")
        trusted = copy.deepcopy(store.state["last_verified_checkpoint"])
        ignored = self.repo.root / "__pycache__" / "changed.pyc"
        ignored.parent.mkdir(exist_ok=True)
        ignored.write_bytes(b"changed candidate after final verification")
        # Git's snapshot alone remains trusted; Wave 6 also binds ignored/protected files.
        self.assertEqual(manager.validate_repository(store, self.repo), "trusted")
        with patch.object(ManagerLoop, "run_work_units") as scheduler:
            result = execute(store, agents=agents, gateway=FakeGateway())
        self.assertEqual(result["status"], "HUMAN_ACTION_REQUIRED")
        self.assertEqual(result["stop"]["reason"], "REPOSITORY_DRIFT")
        self.assertEqual(store.state["last_verified_checkpoint"], trusted)
        scheduler.assert_not_called()


class TestControllerAuthorityM0B(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo, self.store, self.config = setup_test_fixture(self.root)
        self.registry = build_verifier_registry()

    def tearDown(self):
        self.tmp.cleanup()

    def _create_critic_reviewed_run(self, run_id="cr-run", critic_findings=False, reviewer_option=True, critic_option=True):
        store = durable.Store(self.root / "runs", run_id)
        spec = make_spec()
        create_run(
            store,
            self.repo,
            TASK,
            COMMANDS,
            self.config,
            criteria=["Multiply works"],
            work_units=[spec],
            verifier_registry=self.registry,
            critic=critic_option,
            reviewer=reviewer_option,
        )
        executor = ScriptedExecutor()
        executor.set_behavior(
            "unit-1",
            {"writes": {"calculator.py": "def multiply(a, b):\n    return a * b\n"}},
        )
        scheduler = WorkUnitScheduler(
            store,
            self.repo,
            verifier_registry=self.registry,
            agents=Agents(None, executor, None, None),
            parent_scope={"allowed_paths": ["calculator.py"], "forbidden_paths": []},
        )
        res = scheduler.run_sequence()
        self.assertEqual(res["status"], "MILESTONE_READY")

        if critic_option:
            preset = "findings" if critic_findings else "clean"
            crit = CriticExecutor(store, self.repo, self.config, critic_adapter=ScriptedCritic(preset))
            crit_res = crit.execute()
            self.assertIn(crit_res["status"], (CRITIC_CLEAN, CRITIC_FINDINGS))
            self.assertEqual(store.state.get("manager", {}).get("milestone_status"), "CRITIC_REVIEWED")
        return store

    # 1. Null-token SimpleNamespace & duck-typed wrapper -> rejected (Codex F)
    def test_01_null_token_simplenamespace_rejected(self):
        store = self._create_critic_reviewed_run("m0b-test-01")
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev.execute()
        final_verif = execute_final_verification(store, self.repo, self.config)

        # 1a. Null token SimpleNamespace
        fake_wrapper1 = types.SimpleNamespace(store=store, controller_authority=None)
        with self.assertRaises(ReviewerExecutionError) as ctx1:
            validate_acceptance_readiness(store, self.repo, final_verif, controller=fake_wrapper1)
        self.assertTrue(
            "Unauthorized wrapper object" in str(ctx1.exception)
            or "null controller_authority" in str(ctx1.exception)
        )

        # 1b. Duck-typed wrapper with store, run, mcommit
        fake_wrapper2 = types.SimpleNamespace(
            store=store,
            run=lambda: None,
            mcommit=lambda: None,
            controller_authority={"run_id": "m0b-test-01"},
        )
        with self.assertRaises(ReviewerExecutionError) as ctx2:
            validate_acceptance_readiness(store, self.repo, final_verif, controller=fake_wrapper2)
        self.assertIn("Unauthorized wrapper object", str(ctx2.exception))

    # 2. Fake repo.execute return, zero subprocess launches -> rejected (Codex A)
    def test_02_fake_final_artifact_with_zero_executions_rejected(self):
        store = self._create_critic_reviewed_run("m0b-test-02")
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev.execute()
        loop = ManagerLoop(store, None, self.repo, FakeGateway(), None, None)

        fake = {
            "argv": COMMANDS[0],
            "exit_code": 0,
            "stdout": "invented PASS",
            "stderr": "",
            "duration_seconds": 0,
        }
        with patch.object(loop.repo, "execute", return_value=fake):
            final = loop.run_final_verification()

        self.assertFalse(final["passed"])
        with self.assertRaises(ReviewerExecutionError) as ctx:
            loop.validate_acceptance_readiness(final)
        self.assertTrue(
            "did not pass" in str(ctx.exception)
            or "unrecorded execution rejected" in str(ctx.exception)
            or "command_result" in str(ctx.exception)
        )

    # 3. Verifier registry substitution during or after review -> rejected (Codex D)
    def test_03_verifier_registry_substitution_after_senior_approve_rejected(self):
        store = self._create_critic_reviewed_run("m0b-test-03")

        # 3a. Mutation DURING reviewer adapter invocation
        def mutating_adapter(packet):
            opts = copy.deepcopy(store.state.get("options", {}))
            opts["verifier_registry"]["check-calc"]["argv"] = ["git", "diff", "--check"]
            store.commit(options=opts)
            return json.dumps({"decision": "APPROVE", "findings": [], "summary": "Approved"})

        rev_bad = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=mutating_adapter)
        with self.assertRaises(ReviewerExecutionError) as ctx:
            rev_bad.execute()
        self.assertTrue(
            "mutated verifier registry" in str(ctx.exception)
            or "review evidence changed" in str(ctx.exception)
        )

        # 3b. Mutation AFTER senior approval
        (self.repo.root / "calculator.py").write_text("def multiply(a, b):\n    return 0\n", encoding="utf-8")
        store2 = self._create_critic_reviewed_run("m0b-test-03b")
        rev2 = ReviewerExecutor(store2, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev2.execute()
        loop2 = ManagerLoop(store2, None, self.repo, FakeGateway(), None, None)

        opts2 = copy.deepcopy(store2.state.get("options", {}))
        opts2["verifier_registry"]["check-calc"]["argv"] = ["python", "-c", "import sys; sys.exit(0)"]
        store2.commit(options=opts2)

        with self.assertRaises(ReviewerExecutionError) as ctx2:
            execute_final_verification(store2, self.repo, self.config)
        self.assertTrue(
            "Reviewer result is stale" in str(ctx2.exception)
            or "frozen" in str(ctx2.exception)
            or "did not pass" in str(ctx2.exception)
        )

    # 4. Replay of earlier command_result in new attempt & missing store_revision -> rejected (Codex B)
    def test_04_missing_store_revision_rejected(self):
        store = self._create_critic_reviewed_run("m0b-test-04")
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev.execute()
        loop = ManagerLoop(store, None, self.repo, FakeGateway(), None, None)
        valid_verif = loop.run_final_verification()
        self.assertTrue(valid_verif["passed"])

        # 4a. Genuine previous command_result reused in new attempt with advanced boundaries
        replayed = copy.deepcopy(valid_verif)
        replayed["evidence"]["final_attempt_id"] = f"attempt-{uuid.uuid4().hex}"
        replayed["evidence"]["attempt_start_wal_sequence"] = len(store.records)
        replayed["evidence"]["attempt_end_wal_sequence"] = len(store.records) + 5
        replayed["evidence"]["store_revision"] = store.state["revision"] + 1
        ref_replay = store.artifact("final_verification/replayed.json", replayed["evidence"])
        replayed["reference"] = ref_replay
        mgr = copy.deepcopy(store.state["manager"])
        mgr["final_verification"] = ref_replay
        store.commit(manager=mgr)
        with self.assertRaises(ReviewerExecutionError) as ctx1:
            loop.validate_acceptance_readiness(replayed)
        self.assertTrue(
            "outside attempt boundaries" in str(ctx1.exception)
            or "replay rejected" in str(ctx1.exception)
        )

        # 4b. Missing store_revision
        corrupt = copy.deepcopy(valid_verif)
        del corrupt["evidence"]["store_revision"]
        new_ref = store.artifact("final_verification/no_rev.json", corrupt["evidence"])
        corrupt["reference"] = new_ref
        mgr = copy.deepcopy(store.state["manager"])
        mgr["final_verification"] = new_ref
        store.commit(manager=mgr)
        with self.assertRaises(ReviewerExecutionError) as ctx2:
            loop.validate_acceptance_readiness(corrupt)
        self.assertTrue("Store revision mismatch" in str(ctx2.exception) or "Missing store revision" in str(ctx2.exception))

    # 5. Changed verifier ID / result digest / stdout / stderr & wrong checkpoint identity -> rejected (Codex C)
    def test_05_wrong_checkpoint_identity_rejected(self):
        store = self._create_critic_reviewed_run("m0b-test-05")
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev.execute()
        loop = ManagerLoop(store, None, self.repo, FakeGateway(), None, None)
        valid_verif = loop.run_final_verification()
        self.assertTrue(valid_verif["passed"])

        # 5a. Wrong checkpoint identity
        with self.assertRaises(ReviewerExecutionError) as ctx0:
            validate_acceptance_readiness(
                store, self.repo, valid_verif,
                controller=loop.controller_authority,
                checkpoint_id="wrong-checkpoint-id-999",
            )
        self.assertIn("Checkpoint identity mismatch", str(ctx0.exception))

        # 5b. Changed verifier_id
        bad_vid = copy.deepcopy(valid_verif)
        bad_vid["evidence"]["verifiers"][0]["verifier_id"] = "tampered-vid"
        bad_vid["evidence"]["store_revision"] = store.state["revision"] + 1
        ref1 = store.artifact("final_verification/bad_vid.json", bad_vid["evidence"])
        bad_vid["reference"] = ref1
        mgr = copy.deepcopy(store.state["manager"])
        mgr["final_verification"] = ref1
        store.commit(manager=mgr)
        with self.assertRaises(ReviewerExecutionError) as ctx1:
            loop.validate_acceptance_readiness(bad_vid)
        self.assertIn("was not present in frozen approved policy", str(ctx1.exception))

        # 5c. Changed result_digest
        bad_digest = copy.deepcopy(valid_verif)
        bad_digest["evidence"]["verifiers"][0]["result_digest"] = "0" * 64
        bad_digest["evidence"]["store_revision"] = store.state["revision"] + 1
        ref2 = store.artifact("final_verification/bad_digest.json", bad_digest["evidence"])
        bad_digest["reference"] = ref2
        mgr["final_verification"] = ref2
        store.commit(manager=mgr)
        with self.assertRaises(ReviewerExecutionError) as ctx2:
            loop.validate_acceptance_readiness(bad_digest)
        self.assertIn("result digest mismatch", str(ctx2.exception))

        # 5d. Changed stdout
        bad_out = copy.deepcopy(valid_verif)
        bad_out["evidence"]["verifiers"][0]["stdout"] = "tampered stdout"
        bad_out["evidence"]["store_revision"] = store.state["revision"] + 1
        ref3 = store.artifact("final_verification/bad_out.json", bad_out["evidence"])
        bad_out["reference"] = ref3
        mgr["final_verification"] = ref3
        store.commit(manager=mgr)
        with self.assertRaises(ReviewerExecutionError) as ctx3:
            loop.validate_acceptance_readiness(bad_out)
        self.assertIn("stdout/stderr mismatch", str(ctx3.exception))

    # 6. Additional WAL event while approval persists (Codex E) & missing filesystem digest -> rejected
    def test_06_missing_filesystem_digest_rejected(self):
        store = self._create_critic_reviewed_run("m0b-test-06")

        # 6a. Extra WAL event inserted before approval persistence
        store.records.append({
            "sequence": len(store.records) + 1,
            "time": durable.now(),
            "event": "notes",
            "notes": "extra event before reviewer approval",
            "sha256": "0" * 64,
        })
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        res = rev.execute()
        self.assertEqual(res["status"], REVIEWER_APPROVED)

        # Approval sequence and hash resolved correctly from real write
        approval_seq = res["approval_wal_sequence"]
        self.assertEqual(store.records[approval_seq - 1]["sha256"], res["approval_wal_hash"])

        loop = ManagerLoop(store, None, self.repo, FakeGateway(), None, None)
        final_verif = loop.run_final_verification()
        self.assertTrue(final_verif["passed"])

        receipt = loop.validate_acceptance_readiness(final_verif)
        self.assertTrue(receipt["ready"])

        # 6b. Missing filesystem digest
        corrupt = copy.deepcopy(final_verif)
        del corrupt["evidence"]["final_filesystem_snapshot_digest"]
        corrupt["evidence"]["store_revision"] = store.state["revision"] + 1
        new_ref = store.artifact("final_verification/no_fs.json", corrupt["evidence"])
        corrupt["reference"] = new_ref
        mgr = copy.deepcopy(store.state["manager"])
        mgr["final_verification"] = new_ref
        store.commit(manager=mgr)
        with self.assertRaises(ReviewerExecutionError) as ctx:
            loop.validate_acceptance_readiness(corrupt)
        self.assertIn("filesystem snapshot digest", str(ctx.exception))

    # 7. Contradictory run_id or verifier hash -> rejected
    def test_07_contradictory_run_id_or_verifier_hash_rejected(self):
        store = self._create_critic_reviewed_run("m0b-test-07")
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev.execute()
        final_verif = execute_final_verification(store, self.repo, self.config)
        loop = ManagerLoop(store, None, self.repo, FakeGateway(), None, None)

        # Corrupt evidence reference hash
        corrupt_verif = copy.deepcopy(final_verif)
        corrupt_verif["reference"]["sha256"] = "f" * 64
        with self.assertRaises(ReviewerExecutionError) as ctx:
            validate_acceptance_readiness(store, self.repo, corrupt_verif, controller=loop.controller_authority)
        self.assertIn("Fabricated final verification", str(ctx.exception))

        # Contradictory run_id
        bad_run = copy.deepcopy(final_verif)
        bad_run["evidence"]["run_id"] = "contradictory-run-999"
        bad_run["evidence"]["store_revision"] = store.state["revision"] + 1
        bad_ref = store.artifact("final_verification/bad_run.json", bad_run["evidence"])
        bad_run["reference"] = bad_ref
        mgr = copy.deepcopy(store.state["manager"])
        mgr["final_verification"] = bad_ref
        store.commit(manager=mgr)
        with self.assertRaises(ReviewerExecutionError) as ctx:
            validate_acceptance_readiness(store, self.repo, bad_run, controller=loop.controller_authority)
        self.assertIn("Run identity mismatch", str(ctx.exception))

    # 8. Wrong reviewer/critic/environment digest -> rejected
    def test_08_wrong_reviewer_or_environment_digest_rejected(self):
        store = self._create_critic_reviewed_run("m0b-test-08")
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev.execute()
        final_verif = execute_final_verification(store, self.repo, self.config)
        loop = ManagerLoop(store, None, self.repo, FakeGateway(), None, None)

        # Mutate candidate snapshot digest in reviewer result
        mgr = copy.deepcopy(store.state["manager"])
        persisted_rev = store.read_evidence(mgr["reviewer_result"]["artifact_ref"])
        persisted_rev["candidate_snapshot_digest"] = "stale-snapshot-digest-1234"
        new_ref = store.artifact(mgr["reviewer_result"]["artifact_ref"]["path"], persisted_rev)
        mgr["reviewer_result"]["candidate_snapshot_digest"] = "stale-snapshot-digest-1234"
        mgr["reviewer_result"]["artifact_ref"] = new_ref
        store.state["manager"] = mgr

        with self.assertRaises(ReviewerExecutionError) as ctx:
            validate_acceptance_readiness(store, self.repo, final_verif, controller=loop.controller_authority)
        self.assertTrue(
            "Reviewer approval snapshot does not match" in str(ctx.exception)
            or "Senior reviewer approval is stale" in str(ctx.exception)
        )

    # 9. Authority omitted -> rejected
    def test_09_authority_omitted_rejected(self):
        store = self._create_critic_reviewed_run("m0b-test-09")
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev.execute()
        final_verif = execute_final_verification(store, self.repo, self.config)
        with self.assertRaises(ReviewerExecutionError) as ctx:
            validate_acceptance_readiness(store, self.repo, final_verif, controller=None)
        self.assertIn("Controller authority is required", str(ctx.exception))

    # 10. Arbitrary dictionary/string authority -> rejected
    def test_10_arbitrary_dict_or_string_authority_rejected(self):
        store = self._create_critic_reviewed_run("m0b-test-10")
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev.execute()
        final_verif = execute_final_verification(store, self.repo, self.config)

        with self.assertRaises(TypeError):
            manager.bind_controller_authority(store, {"arbitrary": "dictionary"})
        with self.assertRaises(TypeError):
            manager.bind_controller_authority(store, "arbitrary-string")

        with self.assertRaises(ReviewerExecutionError) as ctx:
            validate_acceptance_readiness(store, self.repo, final_verif, controller={"arbitrary": "dictionary"})
        self.assertTrue(
            "unregistered or forged authority rejected" in str(ctx.exception)
            or "does not match run" in str(ctx.exception)
        )

    # 11. Cross-store authority -> rejected
    def test_11_manager_authority_used_with_another_store_rejected(self):
        store_a = self._create_critic_reviewed_run("m0b-test-11a")
        loop_a = ManagerLoop(store_a, None, self.repo, FakeGateway(), None, None)
        rev = ReviewerExecutor(store_a, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev.execute()
        final_verif = execute_final_verification(store_a, self.repo, self.config)

        (self.repo.root / "calculator.py").write_text("def multiply(a, b):\n    return 0\n", encoding="utf-8")
        store_b = self._create_critic_reviewed_run("m0b-test-11b")

        with self.assertRaises(ValueError) as ctx:
            manager.bind_controller_authority(store_b, loop_a)
        self.assertTrue(
            "different Store instance" in str(ctx.exception)
            or "not bound to this Store" in str(ctx.exception)
        )

        with self.assertRaises(ReviewerExecutionError) as ctx:
            validate_acceptance_readiness(store_b, self.repo, final_verif, controller=loop_a.controller_authority)
        self.assertTrue(
            "not authorized for this Store instance" in str(ctx.exception)
            or "does not match run" in str(ctx.exception)
        )

    # 12. Missing persisted reviewer artifact despite caller-supplied APPROVE -> rejected
    def test_12_missing_persisted_reviewer_artifact_despite_caller_approve_rejected(self):
        store = self._create_critic_reviewed_run("m0b-test-12")
        mgr = copy.deepcopy(store.state["manager"])
        mgr["reviewer_result"] = {
            "status": REVIEWER_APPROVED,
            "decision": "APPROVE",
            "artifact_ref": {"path": "milestones/nonexistent.json", "sha256": "0" * 64},
        }
        store.commit(manager=mgr)
        loop = ManagerLoop(store, None, self.repo, FakeGateway(), None, None)

        with self.assertRaises(ReviewerExecutionError) as ctx:
            validate_acceptance_readiness(store, self.repo, controller=loop.controller_authority)
        self.assertTrue(
            "Missing final verification" in str(ctx.exception)
            or "Reviewer evidence validation failed" in str(ctx.exception)
        )

    # 13. Reuse after Store revision changes -> rejected
    def test_13_reuse_after_store_revision_changes_rejected(self):
        store = self._create_critic_reviewed_run("m0b-test-13")
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev.execute()
        final_verif = execute_final_verification(store, self.repo, self.config)
        loop = ManagerLoop(store, None, self.repo, FakeGateway(), None, None)

        receipt = validate_acceptance_readiness(store, self.repo, final_verif, controller=loop.controller_authority)
        self.assertTrue(receipt["ready"])

        store.commit(notes="advance revision to break freshness")

        with self.assertRaises(ReviewerExecutionError) as ctx:
            validate_acceptance_readiness(store, self.repo, final_verif, controller=loop.controller_authority)
        self.assertIn("Store revision mismatch", str(ctx.exception))

    # 14. Genuine production verifier and reviewer evidence -> accepted
    def test_14_genuine_production_verifier_and_reviewer_accepted(self):
        store = self._create_critic_reviewed_run("m0b-test-14")
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev.execute()
        final_verif = execute_final_verification(store, self.repo, self.config)
        loop = ManagerLoop(store, None, self.repo, FakeGateway(), None, None)

        receipt = validate_acceptance_readiness(store, self.repo, final_verif, controller=loop.controller_authority)
        self.assertTrue(receipt["ready"])
        self.assertTrue(receipt["controller_authorized"])
        self.assertEqual(receipt["run_id"], "m0b-test-14")
        self.assertEqual(receipt["store_revision"], store.state["revision"])
        self.assertFalse(store.is_terminal_accepted())

    # 15. Intermediate checkpoints and lifecycle preserved
    def test_15_intermediate_checkpoints_and_lifecycle_preserved(self):
        store = self._create_critic_reviewed_run("m0b-test-15")
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev.execute()
        final_verif = execute_final_verification(store, self.repo, self.config)
        loop = ManagerLoop(store, None, self.repo, FakeGateway(), None, None)

        receipt = loop.validate_acceptance_readiness(final_verif)
        self.assertTrue(receipt["ready"])

        ckpt = loop.create_trusted_checkpoint(final_verif)
        self.assertIsNotNone(ckpt.get("reference"))
        self.assertEqual(store.state.get("last_verified_checkpoint", {}).get("reference"), ckpt["reference"])
        self.assertEqual(len(store.state.get("verified_progress", [])), 1)
        self.assertFalse(store.is_terminal_accepted())

    # 16. Two consecutive rounds and resume preserved
    def test_16_two_consecutive_rounds_and_resume_preserved(self):
        store = durable.Store(self.root / "runs", "m0b-test-16")
        spec1 = make_spec("unit-1", paths=["calculator.py"], verifier_ids=["check-calc"])
        create_run(
            store,
            self.repo,
            TASK,
            COMMANDS,
            self.config,
            criteria=["Multiply works"],
            work_units=[spec1],
            verifier_registry=self.registry,
            critic=True,
            reviewer=True,
        )
        executor = ScriptedExecutor()
        executor.set_behavior("unit-1", {"writes": {"calculator.py": "def multiply(a, b):\n    return a * b\n"}})
        agents = Agents(None, executor, ScriptedCritic("clean"), ScriptedReviewer("approve"))
        res = execute(store, agents=agents, gateway=FakeGateway())
        self.assertEqual(res["status"], "COMPLETED")
        self.assertEqual(len(store.state.get("verified_progress", [])), 1)

        # Resume verification with reloaded store
        reloaded = durable.Store(self.root / "runs", "m0b-test-16")
        reloaded.load()
        self.assertEqual(reloaded.state.get("last_verified_checkpoint"), store.state.get("last_verified_checkpoint"))
        loop = ManagerLoop(reloaded, None, self.repo, FakeGateway(), None, None)
        self.assertIsNotNone(loop.controller_authority)
        self.assertFalse(reloaded.is_terminal_accepted())

    # 17. NO_DIFF acceptance preserved
    def test_17_no_diff_acceptance_preserved(self):
        (self.repo.root / "calculator.py").write_text("def multiply(a, b):\n    return a * b\n", encoding="utf-8")
        subprocess.run([
            "git", "-C", str(self.repo.root),
            "-c", f"safe.directory={self.repo.root}",
            "-c", "user.name=W6 test",
            "-c", "user.email=w6@localhost",
            "commit", "-am", "correct baseline",
        ], check=True, capture_output=True)
        store = durable.Store(self.root / "runs", "m0b-test-17")
        spec = make_spec()
        create_run(
            store,
            self.repo,
            TASK,
            COMMANDS,
            self.config,
            criteria=["Multiply works"],
            work_units=[spec],
            verifier_registry=self.registry,
            critic=True,
            reviewer=True,
        )
        executor = ScriptedExecutor()
        executor.set_behavior("unit-1", {"writes": {}})
        agents = Agents(None, executor, ScriptedCritic("clean"), ScriptedReviewer("approve"))
        res = execute(store, agents=agents, gateway=FakeGateway())
        self.assertEqual(res["status"], "COMPLETED")
        self.assertEqual(len(store.state.get("verified_progress", [])), 1)
        self.assertFalse(store.is_terminal_accepted())

    # 18. Optional critic disabled lifecycle
    def test_18_optional_critic_disabled_lifecycle(self):
        store = self._create_critic_reviewed_run("m0b-test-18", critic_option=False)
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev_res = rev.execute()
        self.assertEqual(rev_res["status"], REVIEWER_APPROVED)

        final_verif = execute_final_verification(store, self.repo, self.config)
        self.assertTrue(final_verif["passed"])

        loop = ManagerLoop(store, None, self.repo, FakeGateway(), None, None)
        receipt = loop.validate_acceptance_readiness(final_verif)
        self.assertTrue(receipt["ready"])
        self.assertIsNone(receipt.get("critic_review_digest"))

        ckpt = loop.create_trusted_checkpoint(final_verif)
        self.assertIsNotNone(ckpt.get("reference"))
        self.assertFalse(store.is_terminal_accepted())

    # 19. Two-verifier ID/argv swap rejected
    def test_19_two_verifier_id_argv_swap_rejected(self):
        store = durable.Store(self.root / "runs", "m0b-test-19")
        registry = dict(self.registry)
        registry["check-style"] = {
            "argv": ["git", "diff", "--check"],
            "timeout_seconds": 30,
        }
        spec = make_spec(verifier_ids=["check-calc", "check-style"])
        create_run(
            store,
            self.repo,
            TASK,
            COMMANDS,
            self.config,
            criteria=["Multiply works"],
            work_units=[spec],
            verifier_registry=registry,
            critic=True,
            reviewer=True,
        )
        executor = ScriptedExecutor()
        executor.set_behavior(
            "unit-1",
            {"writes": {"calculator.py": "def multiply(a, b):\n    return a * b\n"}},
        )
        scheduler = WorkUnitScheduler(
            store,
            self.repo,
            verifier_registry=registry,
            agents=Agents(None, executor, None, None),
            parent_scope={"allowed_paths": ["calculator.py"], "forbidden_paths": []},
        )
        scheduler.run_sequence()
        crit = CriticExecutor(store, self.repo, self.config, critic_adapter=ScriptedCritic("clean"))
        crit.execute()
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev.execute()
        loop = ManagerLoop(store, None, self.repo, FakeGateway(), None, None)
        final_verif = loop.run_final_verification()
        self.assertTrue(final_verif["passed"])

        # 19a. Swap verifier IDs
        swapped = copy.deepcopy(final_verif)
        v0_id = swapped["evidence"]["verifiers"][0]["verifier_id"]
        v1_id = swapped["evidence"]["verifiers"][1]["verifier_id"]
        swapped["evidence"]["verifiers"][0]["verifier_id"] = v1_id
        swapped["evidence"]["verifiers"][1]["verifier_id"] = v0_id
        swapped["evidence"]["store_revision"] = store.state["revision"] + 1
        ref = store.artifact("final_verification/swapped_vids.json", swapped["evidence"])
        swapped["reference"] = ref
        mgr = copy.deepcopy(store.state["manager"])
        mgr["final_verification"] = ref
        store.commit(manager=mgr)

        with self.assertRaises(ReviewerExecutionError) as ctx1:
            loop.validate_acceptance_readiness(swapped)
        self.assertTrue(
            "command swap rejected" in str(ctx1.exception)
            or "does not match frozen approved definition" in str(ctx1.exception)
        )

        # 19b. Swap argv
        swapped_argv = copy.deepcopy(final_verif)
        v0_argv = swapped_argv["evidence"]["verifiers"][0]["argv"]
        v1_argv = swapped_argv["evidence"]["verifiers"][1]["argv"]
        swapped_argv["evidence"]["verifiers"][0]["argv"] = v1_argv
        swapped_argv["evidence"]["verifiers"][1]["argv"] = v0_argv
        swapped_argv["evidence"]["store_revision"] = store.state["revision"] + 1
        ref2 = store.artifact("final_verification/swapped_argv.json", swapped_argv["evidence"])
        swapped_argv["reference"] = ref2
        mgr["final_verification"] = ref2
        store.commit(manager=mgr)

        with self.assertRaises(ReviewerExecutionError) as ctx2:
            loop.validate_acceptance_readiness(swapped_argv)
        self.assertTrue(
            "command swap rejected" in str(ctx2.exception)
            or "does not match frozen approved definition" in str(ctx2.exception)
        )

    # 20. Genuine prior final attempt replayed with new metadata and Store revision -> rejected
    def test_20_prior_final_attempt_replayed_with_new_metadata_rejected(self):
        store = self._create_critic_reviewed_run("m0b-test-20")
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev.execute()
        loop = ManagerLoop(store, None, self.repo, FakeGateway(), None, None)
        valid_verif = loop.run_final_verification()
        self.assertTrue(valid_verif["passed"])

        store.commit(notes="new attempt triggered")
        curr_rev = store.state["revision"]

        # Attempt to synthesize a new attempt using old verifiers
        replayed = copy.deepcopy(valid_verif)
        new_attempt_id = f"attempt-{uuid.uuid4().hex}"
        replayed["evidence"]["final_attempt_id"] = new_attempt_id
        replayed["evidence"]["store_revision"] = curr_rev + 1
        replayed["evidence"]["attempt_start_wal_sequence"] = len(store.records)
        replayed["evidence"]["attempt_end_wal_sequence"] = len(store.records) + 5
        ref = store.artifact("final_verification/replayed_old_attempt.json", replayed["evidence"])
        replayed["reference"] = ref
        mgr = copy.deepcopy(store.state["manager"])
        mgr["final_verification"] = ref
        store.commit(manager=mgr)

        with self.assertRaises(ReviewerExecutionError) as ctx:
            loop.validate_acceptance_readiness(replayed)
        self.assertTrue(
            "outside attempt boundaries" in str(ctx.exception)
            or "replay rejected" in str(ctx.exception)
            or "unanchored attempt rejected" in str(ctx.exception)
        )

    # 21. Fake attempt start/end metadata without authoritative WAL events -> rejected
    def test_21_fake_attempt_start_end_without_wal_events_rejected(self):
        store = self._create_critic_reviewed_run("m0b-test-21")
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev.execute()
        loop = ManagerLoop(store, None, self.repo, FakeGateway(), None, None)
        valid_verif = loop.run_final_verification()

        # 21a. Fake attempt start sequence pointing to wrong event
        fake_start = copy.deepcopy(valid_verif)
        fake_start["evidence"]["attempt_start_wal_sequence"] = 1
        fake_start["evidence"]["store_revision"] = store.state["revision"] + 1
        ref1 = store.artifact("final_verification/fake_start.json", fake_start["evidence"])
        fake_start["reference"] = ref1
        mgr = copy.deepcopy(store.state["manager"])
        mgr["final_verification"] = ref1
        store.commit(manager=mgr)
        with self.assertRaises(ReviewerExecutionError) as ctx1:
            loop.validate_acceptance_readiness(fake_start)
        self.assertIn("not a final_verification_attempt_started event", str(ctx1.exception))

        # 21b. Fake attempt start hash
        fake_hash = copy.deepcopy(valid_verif)
        fake_hash["evidence"]["attempt_start_wal_hash"] = "0" * 64
        fake_hash["evidence"]["store_revision"] = store.state["revision"] + 1
        ref2 = store.artifact("final_verification/fake_hash.json", fake_hash["evidence"])
        fake_hash["reference"] = ref2
        mgr["final_verification"] = ref2
        store.commit(manager=mgr)
        with self.assertRaises(ReviewerExecutionError) as ctx2:
            loop.validate_acceptance_readiness(fake_hash)
        self.assertIn("WAL attempt start record hash mismatch", str(ctx2.exception))

        # 21c. Fake attempt end event
        fake_end = copy.deepcopy(valid_verif)
        fake_end["evidence"]["attempt_end_wal_sequence"] = len(store.records) + 1
        fake_end["evidence"]["store_revision"] = store.state["revision"] + 1
        ref3 = store.artifact("final_verification/fake_end.json", fake_end["evidence"])
        fake_end["reference"] = ref3
        mgr["final_verification"] = ref3
        store.commit(manager=mgr)
        with self.assertRaises(ReviewerExecutionError) as ctx3:
            loop.validate_acceptance_readiness(fake_end)
        self.assertIn("not a final_verification_attempt_ended event", str(ctx3.exception))

    # 22. Attempt identity remains valid after Store reload without accepting reuse in another attempt
    def test_22_attempt_identity_survives_reload_no_reuse(self):
        run_id = "m0b-test-22"
        store = self._create_critic_reviewed_run(run_id)
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev.execute()
        loop = ManagerLoop(store, None, self.repo, FakeGateway(), None, None)
        valid_verif = loop.run_final_verification()
        self.assertTrue(valid_verif["passed"])

        reloaded = durable.Store(self.root / "runs", run_id)
        reloaded.load()
        reloaded_loop = ManagerLoop(reloaded, None, self.repo, FakeGateway(), None, None)
        receipt = reloaded_loop.validate_acceptance_readiness(valid_verif)
        self.assertTrue(receipt["ready"])
        self.assertTrue(receipt["controller_authorized"])

        reloaded.commit(notes="new attempt or work unit advanced")
        with self.assertRaises(ReviewerExecutionError) as ctx:
            reloaded_loop.validate_acceptance_readiness(valid_verif)
        self.assertIn("Store revision mismatch", str(ctx.exception))

    # 23. Extra WAL event after approval commit cannot replace approval identity
    def test_23_extra_wal_event_after_approval_cannot_replace_identity(self):
        store = self._create_critic_reviewed_run("m0b-test-23")
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev_res = rev.execute()
        self.assertEqual(rev_res["status"], REVIEWER_APPROVED)
        approval_seq = rev_res["approval_wal_sequence"]
        approval_hash = rev_res["approval_wal_hash"]

        store.append_record("notes", notes="extra diagnostic record after approval")
        store.append_record("heartbeat", timestamp=durable.now())

        loop = ManagerLoop(store, None, self.repo, FakeGateway(), None, None)
        final_verif = loop.run_final_verification()
        self.assertTrue(final_verif["passed"])
        self.assertEqual(final_verif["evidence"]["approval_wal_sequence"], approval_seq)
        self.assertEqual(final_verif["evidence"]["approval_wal_hash"], approval_hash)

        receipt = loop.validate_acceptance_readiness(final_verif)
        self.assertTrue(receipt["ready"])

        tampered_verif = copy.deepcopy(final_verif)
        tampered_verif["evidence"]["approval_wal_sequence"] = approval_seq + 1
        tampered_verif["evidence"]["store_revision"] = store.state["revision"] + 1
        ref = store.artifact("final_verification/tampered_appr_seq.json", tampered_verif["evidence"])
        tampered_verif["reference"] = ref
        mgr = copy.deepcopy(store.state["manager"])
        mgr["final_verification"] = ref
        store.commit(manager=mgr)
        with self.assertRaises(ReviewerExecutionError) as ctx:
            loop.validate_acceptance_readiness(tampered_verif)
        self.assertIn("Final verification approval WAL sequence mismatch with senior reviewer approval", str(ctx.exception))

    # 24a. Wrong approval event type: point approval_wal_sequence to a command_result record
    def test_24a_wrong_approval_event_type_rejected(self):
        store = self._create_critic_reviewed_run("m0b-test-24a")
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev_res = rev.execute()
        loop = ManagerLoop(store, None, self.repo, FakeGateway(), None, None)
        final_verif = loop.run_final_verification()

        cmd_rec_seq = final_verif["evidence"]["verifiers"][0]["wal_sequence"]
        bad_type_verif = copy.deepcopy(final_verif)
        bad_type_verif["evidence"]["approval_wal_sequence"] = cmd_rec_seq
        bad_type_verif["evidence"]["approval_wal_hash"] = store.records[cmd_rec_seq - 1]["sha256"]
        bad_type_verif["evidence"]["store_revision"] = store.state["revision"] + 1
        ref1 = store.artifact("final_verification/bad_appr_type.json", bad_type_verif["evidence"])
        bad_type_verif["reference"] = ref1
        mgr = copy.deepcopy(store.state["manager"])
        mgr["final_verification"] = ref1
        store.commit(manager=mgr)
        with self.assertRaises(ReviewerExecutionError) as ctx1:
            loop.validate_acceptance_readiness(bad_type_verif)
        self.assertIn("Final verification approval WAL sequence mismatch with senior reviewer approval", str(ctx1.exception))

    # 24b. Corrupt approval payload in WAL
    def test_24b_corrupt_approval_payload_rejected(self):
        store = self._create_critic_reviewed_run("m0b-test-24b")
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev_res = rev.execute()
        loop = ManagerLoop(store, None, self.repo, FakeGateway(), None, None)
        final_verif = loop.run_final_verification()

        approval_seq = rev_res["approval_wal_sequence"]
        appr_rec = store.records[approval_seq - 1]
        appr_payload = appr_rec.get("reviewer_approval") or appr_rec.get("state", {}).get("reviewer_approval")
        appr_payload["candidate_snapshot_digest"] = "0" * 64
        with self.assertRaises(ReviewerExecutionError) as ctx2:
            loop.validate_acceptance_readiness(final_verif)
        self.assertIn("WAL approval event candidate snapshot digest mismatch", str(ctx2.exception))

    # 25. Genuine Supervisor heartbeat between evidence persistence and readiness -> preserved
    def test_25_supervisor_heartbeat_preserved(self):
        store = self._create_critic_reviewed_run("m0b-test-25")
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev.execute()
        loop = ManagerLoop(store, None, self.repo, FakeGateway(), None, None)
        final_verif = loop.run_final_verification()
        self.assertTrue(final_verif["passed"])

        sup = copy.deepcopy(store.state.get("supervision", supervision.initial()))
        sup["controller"] = {
            "pid": os.getpid(),
            "boot": "boot-fixture",
            "heartbeat": time.time(),
            "identity": {"created": "2026-01-01", "executable": sys.executable},
        }
        store.commit(
            supervision=sup,
            supervision_event={"event": "controller_heartbeat"},
        )
        self.assertGreater(store.state["revision"], final_verif["evidence"]["store_revision"])

        receipt = loop.validate_acceptance_readiness(final_verif)
        self.assertTrue(receipt["ready"])
        self.assertTrue(receipt["controller_authorized"])

    # 26. Actual semantic state mutation in heartbeat interval -> rejected
    def test_26_semantic_mutation_in_heartbeat_interval_rejected(self):
        store = self._create_critic_reviewed_run("m0b-test-26")
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev.execute()
        loop = ManagerLoop(store, None, self.repo, FakeGateway(), None, None)
        final_verif = loop.run_final_verification()
        self.assertTrue(final_verif["passed"])

        # 26a. Semantic state mutation: change options during heartbeat
        opts = copy.deepcopy(store.state.get("options", {}))
        opts["timeout"] = 999
        sup = copy.deepcopy(store.state.get("supervision", supervision.initial()))
        sup["controller"] = {
            "pid": os.getpid(),
            "boot": "boot-fixture",
            "heartbeat": time.time(),
            "identity": {"created": "2026-01-01", "executable": sys.executable},
        }
        store.commit(
            options=opts,
            supervision=sup,
            supervision_event={"event": "controller_heartbeat"},
        )
        with self.assertRaises(ReviewerExecutionError) as ctx1:
            loop.validate_acceptance_readiness(final_verif)
        self.assertIn("semantic state mutation in field 'options'", str(ctx1.exception))

    # 26b. Non-heartbeat event type
    def test_26b_non_heartbeat_event_rejected(self):
        store2 = self._create_critic_reviewed_run("m0b-test-26b")
        rev2 = ReviewerExecutor(store2, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev2.execute()
        loop2 = ManagerLoop(store2, None, self.repo, FakeGateway(), None, None)
        final_verif2 = loop2.run_final_verification()
        sup2 = copy.deepcopy(store2.state.get("supervision", supervision.initial()))
        sup2["controller"] = {
            "pid": os.getpid(),
            "boot": "boot-fixture",
            "heartbeat": time.time(),
            "identity": {"created": "2026-01-01", "executable": sys.executable},
        }
        store2.commit(
            supervision=sup2,
            supervision_event={"event": "unexpected_event"},
        )
        with self.assertRaises(ReviewerExecutionError) as ctx2:
            loop2.validate_acceptance_readiness(final_verif2)
        self.assertIn("semantic transaction 'unexpected_event' occurred after final verification", str(ctx2.exception))

    # 27. Real two-verifier production execution validates exact ID and argv mapping
    def test_27_real_two_verifier_production_execution_validates_mapping(self):
        store = durable.Store(self.root / "runs", "m0b-test-27")
        registry = dict(self.registry)
        registry["check-diff"] = {
            "argv": ["git", "diff", "--check"],
            "timeout_seconds": 30,
        }
        spec = make_spec(verifier_ids=["check-calc", "check-diff"])
        create_run(
            store,
            self.repo,
            TASK,
            COMMANDS,
            self.config,
            criteria=["Multiply works"],
            work_units=[spec],
            verifier_registry=registry,
            critic=True,
            reviewer=True,
        )
        executor = ScriptedExecutor()
        executor.set_behavior(
            "unit-1",
            {"writes": {"calculator.py": "def multiply(a, b):\n    return a * b\n"}},
        )
        scheduler = WorkUnitScheduler(
            store,
            self.repo,
            verifier_registry=registry,
            agents=Agents(None, executor, None, None),
            parent_scope={"allowed_paths": ["calculator.py"], "forbidden_paths": []},
        )
        scheduler.run_sequence()
        crit = CriticExecutor(store, self.repo, self.config, critic_adapter=ScriptedCritic("clean"))
        crit.execute()
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev.execute()

        loop = ManagerLoop(store, None, self.repo, FakeGateway(), None, None)
        final_verif = loop.run_final_verification()
        self.assertTrue(final_verif["passed"])

        receipt = loop.validate_acceptance_readiness(final_verif)
        self.assertTrue(receipt["ready"])
        self.assertTrue(receipt["controller_authorized"])
        self.assertEqual(receipt["run_id"], "m0b-test-27")

        verifiers = final_verif["evidence"]["verifiers"]
        self.assertEqual(len(verifiers), 2)
        vid_map = {v["verifier_id"]: v for v in verifiers}
        self.assertIn("check-calc", vid_map)
        self.assertIn("check-diff", vid_map)
        self.assertEqual(vid_map["check-calc"]["argv"], registry["check-calc"]["argv"])
        self.assertEqual(vid_map["check-diff"]["argv"], registry["check-diff"]["argv"])
        self.assertTrue(vid_map["check-calc"]["passed"])
        self.assertTrue(vid_map["check-diff"]["passed"])

    # 28. Publisher refuses checkpoint when readiness rejects invalid evidence (P0)
    def test_28_publisher_refuses_checkpoint_when_readiness_rejects(self):
        run_id = "m0b-test-28"
        store = self._create_critic_reviewed_run(run_id)
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev.execute()
        loop = ManagerLoop(store, None, self.repo, FakeGateway(), None, None)
        valid_verif = loop.run_final_verification()
        self.assertTrue(valid_verif["passed"])

        # Tamper: reuse old command results under a different final attempt identity
        tampered = copy.deepcopy(valid_verif)
        tampered["evidence"]["final_attempt_id"] = f"attempt-{uuid.uuid4().hex}"
        tampered["evidence"]["store_revision"] = store.state["revision"] + 1
        ref = store.artifact("final_verification/tampered_attempt.json", tampered["evidence"])
        tampered["reference"] = ref
        mgr = copy.deepcopy(store.state["manager"])
        mgr["final_verification"] = ref
        store.commit(manager=mgr)

        # Execute ZERO new final commands
        # 1. Readiness rejects the evidence
        with self.assertRaises(ReviewerExecutionError):
            loop.validate_acceptance_readiness(tampered)

        # 2. Checkpoint publisher MUST refuse checkpoint
        with self.assertRaises(ReviewerExecutionError):
            loop.create_trusted_checkpoint(tampered)

        # 3. Verify no trusted checkpoint was established in store
        self.assertNotEqual(store.state.get("manager", {}).get("milestone_status"), "TRUSTED_CHECKPOINT")
        self.assertIsNone(store.state.get("last_verified_checkpoint"))

        # 4. Verify reload remains untrusted
        reloaded = durable.Store(self.root / "runs", run_id)
        reloaded.load()
        self.assertFalse(reloaded.is_terminal_accepted())
        try:
            repo_status = durable.validate_repository(reloaded, self.repo)
        except durable.DurableError:
            repo_status = "untrusted_error"
        self.assertNotEqual(repo_status, "trusted")

    # 29. Genuine old attempt with rewritten revision rejected by WAL anchor (P1)
    def test_29_old_attempt_with_rewritten_revision_rejected_by_wal_anchor(self):
        run_id = "m0b-test-29"
        store = self._create_critic_reviewed_run(run_id)
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev.execute()
        loop = ManagerLoop(store, None, self.repo, FakeGateway(), None, None)
        valid_verif = loop.run_final_verification()
        self.assertTrue(valid_verif["passed"])

        # Semantic commit advances state
        opts = copy.deepcopy(store.state["options"])
        opts["timeout"] = 120
        store.commit(options=opts)
        new_rev = store.state["revision"]

        # Old artifact metadata rewritten to match new revision
        tampered = copy.deepcopy(valid_verif)
        tampered["evidence"]["store_revision"] = new_rev
        ref = store.artifact(valid_verif["reference"]["path"], tampered["evidence"])
        tampered["reference"] = ref
        mgr = copy.deepcopy(store.state["manager"])
        mgr["final_verification"] = ref
        store.commit(manager=mgr)

        # Readiness must reject: anchored to authoritative attempt-ended record in WAL
        with self.assertRaises(ReviewerExecutionError):
            loop.validate_acceptance_readiness(tampered)

        # Publisher must refuse checkpoint
        with self.assertRaises(ReviewerExecutionError):
            loop.create_trusted_checkpoint(tampered)

    # 30. Duplicate WAL sequence for two verifiers rejected (P1)
    def test_30_duplicate_wal_sequence_for_two_verifiers_rejected(self):
        run_id = "m0b-test-30"
        store = durable.Store(self.root / "runs", run_id)
        registry = dict(self.registry)
        registry["check-calc-2"] = {
            "argv": list(registry["check-calc"]["argv"]),
            "timeout_seconds": 30,
        }
        spec = make_spec(verifier_ids=["check-calc", "check-calc-2"])
        create_run(
            store,
            self.repo,
            TASK,
            COMMANDS,
            self.config,
            criteria=["Multiply works"],
            work_units=[spec],
            verifier_registry=registry,
            critic=True,
            reviewer=True,
        )
        executor = ScriptedExecutor()
        executor.set_behavior(
            "unit-1",
            {"writes": {"calculator.py": "def multiply(a, b):\n    return a * b\n"}},
        )
        scheduler = WorkUnitScheduler(
            store,
            self.repo,
            verifier_registry=registry,
            agents=Agents(None, executor, None, None),
            parent_scope={"allowed_paths": ["calculator.py"], "forbidden_paths": []},
        )
        scheduler.run_sequence()
        crit = CriticExecutor(store, self.repo, self.config, critic_adapter=ScriptedCritic("clean"))
        crit.execute()
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev.execute()

        loop = ManagerLoop(store, None, self.repo, FakeGateway(), None, None)
        final_verif = loop.run_final_verification()
        self.assertTrue(final_verif["passed"])
        self.assertEqual(len(final_verif["evidence"]["verifiers"]), 2)

        # Duplicate the first verifier's WAL sequence onto the second verifier
        tampered = copy.deepcopy(final_verif)
        seq0 = tampered["evidence"]["verifiers"][0]["wal_sequence"]
        hash0 = tampered["evidence"]["verifiers"][0]["wal_record_hash"]
        tampered["evidence"]["verifiers"][1]["wal_sequence"] = seq0
        tampered["evidence"]["verifiers"][1]["wal_record_hash"] = hash0
        tampered["evidence"]["store_revision"] = store.state["revision"] + 1
        ref = store.artifact("final_verification/dup_wal_seq.json", tampered["evidence"])
        tampered["reference"] = ref
        mgr = copy.deepcopy(store.state["manager"])
        mgr["final_verification"] = ref
        store.commit(manager=mgr)

        with self.assertRaises(ReviewerExecutionError) as ctx:
            loop.validate_acceptance_readiness(tampered)
        self.assertTrue(
            "Duplicate WAL execution sequence" in str(ctx.exception)
            or "distinct executions required" in str(ctx.exception)
            or "mismatch between artifact" in str(ctx.exception)
        )

    # 31. Missing or None approval payload in WAL rejected (P1)
    def test_31_missing_or_none_approval_payload_rejected(self):
        run_id = "m0b-test-31"
        store = self._create_critic_reviewed_run(run_id)
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev.execute()

        # Advance WAL with ordinary commit that has NO reviewer approval (reviewer_approval=None)
        # BEFORE running final verification, so bad_appr_seq < attempt_start:
        store.commit(notes="ordinary non-approval commit")
        bad_appr_seq = len(store.records)
        bad_appr_hash = store.records[-1]["sha256"]

        # Tamper the reviewer_result in state so its approval points to bad_appr_seq
        mgr = copy.deepcopy(store.state["manager"])
        rev_art = store.read_evidence(mgr["reviewer_result"]["artifact_ref"])
        rev_art["approval_wal_sequence"] = bad_appr_seq
        rev_art["approval_wal_hash"] = bad_appr_hash
        ref = store.artifact(mgr["reviewer_result"]["artifact_ref"]["path"], rev_art)
        mgr["reviewer_result"]["approval_wal_sequence"] = bad_appr_seq
        mgr["reviewer_result"]["approval_wal_hash"] = bad_appr_hash
        mgr["reviewer_result"]["artifact_ref"] = ref
        store.commit(manager=mgr)

        loop = ManagerLoop(store, None, self.repo, FakeGateway(), None, None)
        final_verif = loop.run_final_verification()

        with self.assertRaises(ReviewerExecutionError) as ctx:
            loop.validate_acceptance_readiness(final_verif)
        self.assertTrue(
            "Missing" in str(ctx.exception)
            or "authoritative reviewer approval payload" in str(ctx.exception)
        )

    # 32. Arbitrary supervision mutation disguised as heartbeat rejected (P1)
    def test_32_arbitrary_supervision_mutation_disguised_as_heartbeat_rejected(self):
        run_id = "m0b-test-32"
        store = self._create_critic_reviewed_run(run_id)
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev.execute()
        loop = ManagerLoop(store, None, self.repo, FakeGateway(), None, None)
        final_verif = loop.run_final_verification()
        self.assertTrue(final_verif["passed"])

        # Mutate supervision with unauthorized field labeled as controller_heartbeat
        sup = copy.deepcopy(store.state.get("supervision", supervision.initial()))
        sup["controller"] = {
            "pid": os.getpid(),
            "boot": "boot-fixture",
            "heartbeat": time.time(),
            "identity": {"created": "2026-01-01", "executable": sys.executable},
            "unauthorized_field": "malicious_injection",
        }
        store.commit(
            supervision=sup,
            supervision_event={"event": "controller_heartbeat"},
        )

        with self.assertRaises(ReviewerExecutionError) as ctx:
            loop.validate_acceptance_readiness(final_verif)
        self.assertTrue(
            "Supervision" in str(ctx.exception)
            or "supervision" in str(ctx.exception)
            or "mutated" in str(ctx.exception)
            or "heartbeat" in str(ctx.exception)
        )

    # 33. Unknown WAL event without revision increment rejected (P1)
    def test_33_unknown_wal_event_without_revision_increment_rejected(self):
        run_id = "m0b-test-33"
        store = self._create_critic_reviewed_run(run_id)
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev.execute()
        loop = ManagerLoop(store, None, self.repo, FakeGateway(), None, None)
        final_verif = loop.run_final_verification()
        self.assertTrue(final_verif["passed"])

        # Append unknown WAL event directly without calling store.commit (revision unchanged)
        store.append_record("unauthorized_audit_event", payload="tampered")
        self.assertEqual(store.state["revision"], final_verif["evidence"]["store_revision"])

        with self.assertRaises(ReviewerExecutionError) as ctx:
            loop.validate_acceptance_readiness(final_verif)
        self.assertTrue(
            "WAL event" in str(ctx.exception)
            or "non-heartbeat" in str(ctx.exception)
            or "Unknown" in str(ctx.exception)
        )

    # 34. Unrelated WAL insertion during commit projection preserves exact receipt (P1)
    def test_34_unrelated_wal_insertion_during_commit_projection_preserves_exact_receipt(self):
        run_id = "m0b-test-34"
        store = self._create_critic_reviewed_run(run_id)

        orig_atomic_write = durable.atomic_write

        def hooked_atomic_write(path, data):
            if "state.json" in str(path):
                store.records.append({
                    "sequence": 9999,
                    "event": "injected_interleaving_attack",
                    "time": durable.now(),
                    "sha256": "0" * 64,
                })
            return orig_atomic_write(path, data)

        with patch("durable.atomic_write", side_effect=hooked_atomic_write):
            receipt = store.commit(notes="genuine commit with interleaving attack")

        self.assertEqual(receipt["event"], "state_committed")
        self.assertNotEqual(receipt["sequence"], 9999)
        self.assertNotEqual(receipt["event"], "injected_interleaving_attack")

    # 35. Genuine two-verifier production execution checkpoint and reload
    def test_35_genuine_two_verifier_production_checkpoint_and_reload(self):
        run_id = "m0b-test-35"
        store = durable.Store(self.root / "runs", run_id)
        registry = dict(self.registry)
        registry["check-diff"] = {
            "argv": ["git", "diff", "--check"],
            "timeout_seconds": 30,
        }
        spec = make_spec(verifier_ids=["check-calc", "check-diff"])
        create_run(
            store,
            self.repo,
            TASK,
            COMMANDS,
            self.config,
            criteria=["Multiply works"],
            work_units=[spec],
            verifier_registry=registry,
            critic=True,
            reviewer=True,
        )
        executor = ScriptedExecutor()
        executor.set_behavior(
            "unit-1",
            {"writes": {"calculator.py": "def multiply(a, b):\n    return a * b\n"}},
        )
        scheduler = WorkUnitScheduler(
            store,
            self.repo,
            verifier_registry=registry,
            agents=Agents(None, executor, None, None),
            parent_scope={"allowed_paths": ["calculator.py"], "forbidden_paths": []},
        )
        scheduler.run_sequence()
        crit = CriticExecutor(store, self.repo, self.config, critic_adapter=ScriptedCritic("clean"))
        crit.execute()
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev.execute()

        loop = ManagerLoop(store, None, self.repo, FakeGateway(), None, None)
        final_verif = loop.run_final_verification()
        self.assertTrue(final_verif["passed"])

        ckpt_res = loop.create_trusted_checkpoint(final_verif)
        self.assertIn("ckpt-milestone-", ckpt_res["checkpoint_id"])

        reloaded = durable.Store(self.root / "runs", run_id)
        reloaded.load()
        self.assertEqual(durable.validate_repository(reloaded, self.repo), "trusted")

    # 36. Working-file modified before pre-write guard refuses checkpoint (Fix 1)
    def test_36_working_file_modified_before_pre_write_guard_refuses_checkpoint(self):
        run_id = "m0b-test-36"
        store = self._create_critic_reviewed_run(run_id)
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev.execute()
        loop = ManagerLoop(store, None, self.repo, FakeGateway(), None, None)
        final_verif = loop.run_final_verification()
        self.assertTrue(final_verif["passed"])

        orig_commit = store.commit
        calc_path = self.repo.root / "calculator.py"

        def hooked_commit(*args, **kwargs):
            if "pre_write_guard" in kwargs:
                # Mutate working file after readiness check but right before pre_write_guard.
                # HEAD must remain unchanged!
                calc_path.write_text("def multiply(a, b):\n    return a * b  # working file mutation without git commit\n", encoding="utf-8")
            return orig_commit(*args, **kwargs)

        # Publication must be refused by the pre_write_guard detecting candidate and fs mutation
        with patch.object(store, "commit", side_effect=hooked_commit):
            with self.assertRaises(durable.DurableError) as ctx:
                loop.create_trusted_checkpoint(final_verif)
        self.assertTrue(
            "Repository candidate snapshot" in str(ctx.exception)
            or "filesystem content mutated" in str(ctx.exception)
            or "Repository changed" in str(ctx.exception)
        )

        # Verify no trusted checkpoint established in store
        self.assertNotEqual(store.state.get("manager", {}).get("milestone_status"), "TRUSTED_CHECKPOINT")
        self.assertIsNone(store.state.get("last_verified_checkpoint"))

        # Verify Store reload does not trust it
        reloaded = durable.Store(self.root / "runs", run_id)
        reloaded.load()
        try:
            repo_status = durable.validate_repository(reloaded, self.repo)
        except durable.DurableError:
            repo_status = "untrusted_error"
        self.assertNotEqual(repo_status, "trusted")

    # 37. Real Supervisor.heartbeat() with legitimate value updates succeeds (Fix 2A)
    def test_37_real_supervisor_heartbeat_succeeds(self):
        run_id = "m0b-test-37"
        store = self._create_critic_reviewed_run(run_id)
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev.execute()
        loop = ManagerLoop(store, None, self.repo, FakeGateway(), None, None)
        final_verif = loop.run_final_verification()
        self.assertTrue(final_verif["passed"])

        # Execute genuine Supervisor.heartbeat()
        supervisor = supervision.Supervisor(store)
        supervisor.heartbeat()
        self.assertGreater(store.state["revision"], final_verif["evidence"]["store_revision"])

        # Readiness succeeds
        receipt = loop.validate_acceptance_readiness(final_verif)
        self.assertTrue(receipt["ready"])
        self.assertTrue(receipt["controller_authorized"])

        # Publication succeeds
        ckpt_res = loop.create_trusted_checkpoint(final_verif)
        self.assertIn("ckpt-milestone-", ckpt_res["checkpoint_id"])

        reloaded = durable.Store(self.root / "runs", run_id)
        reloaded.load()
        self.assertEqual(durable.validate_repository(reloaded, self.repo), "trusted")

    # 38a. Existing command_sha256 change rejected (Fix 2B)
    def test_38a_heartbeat_command_sha256_change_rejected(self):
        run_id = "m0b-test-38a"
        store = self._create_critic_reviewed_run(run_id)
        # Register a valid helper child before final verification
        supervisor = supervision.Supervisor(store)
        cid = supervisor.register("helper", sys.executable, ["python", "-c", "pass"])
        supervisor.started(cid, os.getpid())

        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev.execute()
        loop = ManagerLoop(store, None, self.repo, FakeGateway(), None, None)
        final_verif = loop.run_final_verification()
        self.assertTrue(final_verif["passed"])

        # Mutate command_sha256 under process_heartbeat
        sup = copy.deepcopy(store.state["supervision"])
        sup["children"][cid]["command_sha256"] = "f" * 64
        store.commit(
            supervision=sup,
            supervision_event={"event": "process_heartbeat", "child_id": cid},
        )
        with self.assertRaises(ReviewerExecutionError) as ctx1:
            loop.validate_acceptance_readiness(final_verif)
        self.assertIn("command_sha256", str(ctx1.exception))

    # 38b. Existing process identity change rejected (Fix 2B)
    def test_38b_heartbeat_process_identity_change_rejected(self):
        run_id = "m0b-test-38b"
        store = self._create_critic_reviewed_run(run_id)
        sup = supervision.Supervisor(store)
        cid = sup.register("helper", sys.executable, ["python", "-c", "pass"])
        sup.started(cid, os.getpid())
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev.execute()
        loop = ManagerLoop(store, None, self.repo, FakeGateway(), None, None)
        final_verif = loop.run_final_verification()
        self.assertTrue(final_verif["passed"])

        # Mutate process identity under process_heartbeat
        s = copy.deepcopy(store.state["supervision"])
        s["children"][cid]["identity"] = {"created": "99999", "executable": sys.executable}
        store.commit(
            supervision=s,
            supervision_event={"event": "process_heartbeat", "child_id": cid},
        )
        with self.assertRaises(ReviewerExecutionError) as ctx2:
            loop.validate_acceptance_readiness(final_verif)
        self.assertIn("identity", str(ctx2.exception))

    # 39. PID, executable or ownership change rejected (Fix 2C)
    def test_39_heartbeat_pid_executable_or_ownership_change_rejected(self):
        run_id = "m0b-test-39"
        store = self._create_critic_reviewed_run(run_id)
        sup = supervision.Supervisor(store)
        sup.heartbeat()

        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev.execute()
        loop = ManagerLoop(store, None, self.repo, FakeGateway(), None, None)
        final_verif = loop.run_final_verification()
        self.assertTrue(final_verif["passed"])

        # Mutate controller PID under controller_heartbeat
        sup_data = copy.deepcopy(store.state["supervision"])
        sup_data["controller"]["pid"] = 999999
        store.commit(
            supervision=sup_data,
            supervision_event={"event": "controller_heartbeat"},
        )
        with self.assertRaises(ReviewerExecutionError) as ctx:
            loop.validate_acceptance_readiness(final_verif)
        self.assertIn("PID", str(ctx.exception))

    # 40. Unexpected supervision field or child state change rejected (Fix 2D)
    def test_40_heartbeat_unexpected_supervision_field_or_state_change_rejected(self):
        run_id = "m0b-test-40"
        store = self._create_critic_reviewed_run(run_id)
        sup = supervision.Supervisor(store)
        cid = sup.register("helper", sys.executable, ["python", "-c", "pass"])
        sup.started(cid, os.getpid())

        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev.execute()
        loop = ManagerLoop(store, None, self.repo, FakeGateway(), None, None)
        final_verif = loop.run_final_verification()
        self.assertTrue(final_verif["passed"])

        # Mutate child state from RUNNING to EXITED under process_heartbeat
        s = copy.deepcopy(store.state["supervision"])
        s["children"][cid]["state"] = "EXITED"
        store.commit(
            supervision=s,
            supervision_event={"event": "process_heartbeat", "child_id": cid},
        )
        with self.assertRaises(ReviewerExecutionError) as ctx1:
            loop.validate_acceptance_readiness(final_verif)
        self.assertIn("state", str(ctx1.exception))


class TestTerminalLifecycleM0C(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo, self.store, self.config = setup_test_fixture(self.root)
        self.registry = build_verifier_registry()

    def tearDown(self):
        self.tmp.cleanup()

    def _create_terminal_run(self, run_id, critic=True, reviewer=True, terminal_acceptance=True, executor_writes=None):
        store = durable.Store(self.root / "runs", run_id)
        spec = make_spec()
        create_run(
            store,
            self.repo,
            TASK,
            COMMANDS,
            self.config,
            criteria=["Multiply works"],
            work_units=[spec],
            verifier_registry=self.registry,
            critic=critic,
            reviewer=reviewer,
            terminal_acceptance=terminal_acceptance,
        )
        executor = ScriptedExecutor()
        writes = executor_writes if executor_writes is not None else {"calculator.py": "def multiply(a, b):\n    return a * b\n"}
        executor.set_behavior("unit-1", {"writes": writes})
        critic_adapter = ScriptedCritic("clean") if critic else None
        reviewer_adapter = ScriptedReviewer("approve") if reviewer else None
        agents = Agents(None, executor, critic_adapter, reviewer_adapter)
        return store, agents

    # A. Genuine successful terminal lifecycle
    def test_a_genuine_successful_terminal_lifecycle(self):
        run_id = "m0c-test-a"
        store, agents = self._create_terminal_run(run_id)
        gw = FakeGateway()
        res = execute(store, agents=agents, gateway=gw, terminal_acceptance=True)
        self.assertEqual(res["status"], "COMPLETED")
        self.assertTrue(store.is_terminal_accepted())
        self.assertEqual(store.state["manager"]["milestone_status"], "TERMINAL_ACCEPTED")
        self.assertEqual(store.state["status"], "COMPLETED")
        self.assertEqual(len(store.state.get("verified_progress", [])), 1)

        # Confirm acceptance seal exists on disk
        seal_path = store.state["terminal_acceptance"]["seal_path"]
        self.assertTrue((store.directory / seal_path).exists())

        # Fresh reload confirms terminal acceptance
        reloaded = durable.Store(self.root / "runs", run_id)
        reloaded.load()
        self.assertTrue(reloaded.is_terminal_accepted())
        self.assertEqual(reloaded.state["status"], "COMPLETED")
        self.assertEqual(reloaded.state["manager"]["milestone_status"], "TERMINAL_ACCEPTED")

        # Subsequent loop run returns immediately
        loop = ManagerLoop(reloaded, None, self.repo, FakeGateway(), None, None)
        subsequent = loop.run()
        self.assertEqual(subsequent["status"], "COMPLETED")

    # B. Cleanup failure prevents acceptance
    def test_b_cleanup_failure_prevents_acceptance(self):
        run_id = "m0c-test-b"
        store, agents = self._create_terminal_run(run_id)

        class FailingGateway(FakeGateway):
            def __init__(self, *args):
                super().__init__(*args)
                self.unload_count = 0

            def unload(self):
                self.unload_count += 1
                if self.unload_count > 1:
                    raise RuntimeError("Gateway unload process crashed")
                super().unload()

        gw = FailingGateway()
        res = execute(store, agents=agents, gateway=gw, terminal_acceptance=True)
        self.assertEqual(res["status"], "FAILED")
        self.assertEqual(store.state["manager"]["stop"]["reason"], "CLEANUP_FAILED")
        self.assertFalse(store.is_terminal_accepted())
        # No trusted terminal checkpoint was committed
        self.assertIsNone(store.state.get("terminal_acceptance"))

    # C. Supervisor shutdown incomplete
    def test_c_supervisor_shutdown_incomplete(self):
        run_id = "m0c-test-c"
        store, agents = self._create_terminal_run(run_id)

        class IncompleteSupervisor(supervision.Supervisor):
            def shutdown(self, *exc, timeout=5.0):
                super().shutdown(*exc, timeout=timeout)
                self.closed = False

        with patch("manager.Supervisor", IncompleteSupervisor):
            res = execute(store, agents=agents, gateway=FakeGateway(), terminal_acceptance=True)
        self.assertEqual(res["status"], "FAILED")
        self.assertEqual(store.state["manager"]["stop"]["reason"], "SUPERVISOR_ACTIVE")
        self.assertFalse(store.is_terminal_accepted())

    # D. Gateway unload incomplete
    def test_d_gateway_unload_incomplete(self):
        run_id = "m0c-test-d"
        store, agents = self._create_terminal_run(run_id)

        class IncompleteGateway(FakeGateway):
            def running(self):
                return ["qwen-executor"]

        gw = IncompleteGateway()
        res = execute(store, agents=agents, gateway=gw, terminal_acceptance=True)
        self.assertEqual(res["status"], "FAILED")
        self.assertEqual(store.state["manager"]["stop"]["reason"], "GATEWAY_UNLOAD_INCOMPLETE")
        self.assertFalse(store.is_terminal_accepted())

    # E. Changed candidate after cleanup
    def test_e_changed_candidate_after_cleanup(self):
        run_id = "m0c-test-e"
        store, agents = self._create_terminal_run(run_id)
        calc_path = self.repo.root / "calculator.py"

        class TamperingGateway(FakeGateway):
            def unload(self):
                super().unload()
                calc_path.write_text("def multiply(a, b):\n    return -1\n", encoding="utf-8")

        gw = TamperingGateway()
        res = execute(store, agents=agents, gateway=gw, terminal_acceptance=True)
        self.assertEqual(res["status"], "HUMAN_ACTION_REQUIRED")
        self.assertEqual(store.state["manager"]["stop"]["reason"], "REPOSITORY_DRIFT")
        self.assertFalse(store.is_terminal_accepted())

    # F. WAL commit failure
    def test_f_wal_commit_failure(self):
        run_id = "m0c-test-f"
        store, agents = self._create_terminal_run(run_id)

        def failing_commit_terminal(*args, **kwargs):
            raise durable.DurableError("WAL terminal write I/O failure")

        with patch.object(store, "commit_terminal", failing_commit_terminal):
            with self.assertRaises(durable.DurableError):
                execute(store, agents=agents, gateway=FakeGateway(), terminal_acceptance=True)
        self.assertFalse(store.is_terminal_accepted())

    # G. Reload of terminal accepted run
    def test_g_reload_of_terminal_accepted_run(self):
        run_id = "m0c-test-g"
        store, agents = self._create_terminal_run(run_id)
        res = execute(store, agents=agents, gateway=FakeGateway(), terminal_acceptance=True)
        self.assertEqual(res["status"], "COMPLETED")
        self.assertTrue(store.is_terminal_accepted())

        reloaded = durable.Store(self.root / "runs", run_id)
        reloaded.load()
        self.assertTrue(reloaded.is_terminal_accepted())
        self.assertEqual(reloaded.state["status"], "COMPLETED")
        self.assertEqual(reloaded.state["manager"]["milestone_status"], "TERMINAL_ACCEPTED")
        self.assertEqual(manager.prepare_resume(reloaded), "completed")
        loop = ManagerLoop(reloaded, None, self.repo, FakeGateway(), None, None)
        report = loop.run()
        self.assertEqual(report["status"], "COMPLETED")

    # H. Recovery from interrupted finalization
    def test_h_recovery_from_interrupted_finalization(self):
        run_id = "m0c-test-h"
        store, agents = self._create_terminal_run(run_id)
        attempts = [0]
        orig_commit_terminal = store.commit_terminal

        def interrupt_once(*args, **kwargs):
            if attempts[0] == 0:
                attempts[0] += 1
                raise KeyboardInterrupt("Simulated interruption during finalization")
            return orig_commit_terminal(*args, **kwargs)

        with patch.object(store, "commit_terminal", interrupt_once):
            with self.assertRaises(KeyboardInterrupt):
                execute(store, agents=agents, gateway=FakeGateway(), terminal_acceptance=True)

        self.assertFalse(store.is_terminal_accepted())
        self.assertIsNotNone(store.state.get("last_verified_checkpoint"))

        # Resume the interrupted run
        action = manager.prepare_resume(store)
        self.assertIn(action, ("continue", "verify_only"))
        self.assertFalse(store.is_terminal_accepted())

        # Second execute invocation succeeds and commits terminal acceptance
        res = execute(store, agents=agents, gateway=FakeGateway(), terminal_acceptance=True)
        self.assertEqual(res["status"], "COMPLETED")
        self.assertTrue(store.is_terminal_accepted())
        self.assertEqual(store.state["manager"]["milestone_status"], "TERMINAL_ACCEPTED")

    # I. Intermediate checkpoints preserved
    def test_i_intermediate_checkpoints_preserved(self):
        run_id = "m0c-test-i"
        store, agents = self._create_terminal_run(run_id)
        res = execute(store, agents=agents, gateway=FakeGateway(), terminal_acceptance=True)
        self.assertEqual(res["status"], "COMPLETED")
        self.assertTrue(store.is_terminal_accepted())

        lvc = store.state.get("last_verified_checkpoint")
        self.assertIsNotNone(lvc)
        self.assertIn("reference", lvc)
        ckpt_data = store.read_evidence(lvc["reference"])
        durable.validate_checkpoint_evidence(store, ckpt_data)
        self.assertEqual(ckpt_data["run_id"], run_id)
        self.assertEqual(len(store.state.get("verified_progress", [])), 1)

        # Restore clean baseline for the second run
        (self.repo.root / "calculator.py").write_text("def multiply(a, b):\n    return 0\n", encoding="utf-8")
        # Intermediate checkpoint without terminal acceptance remains distinguishable
        m0b_run_id = "m0c-test-i-m0b"
        m0b_store, m0b_agents = self._create_terminal_run(m0b_run_id, terminal_acceptance=False)
        m0b_res = execute(m0b_store, agents=m0b_agents, gateway=FakeGateway(), terminal_acceptance=False)
        self.assertEqual(m0b_res["status"], "COMPLETED")
        self.assertFalse(m0b_store.is_terminal_accepted())
        self.assertIsNotNone(m0b_store.state.get("last_verified_checkpoint"))

    # J. NO_DIFF acceptance preserved
    def test_j_no_diff_acceptance_preserved(self):
        (self.repo.root / "calculator.py").write_text("def multiply(a, b):\n    return a * b\n", encoding="utf-8")
        subprocess.run([
            "git", "-C", str(self.repo.root),
            "-c", f"safe.directory={self.repo.root}",
            "-c", "user.name=W6 test",
            "-c", "user.email=w6@localhost",
            "commit", "-am", "correct baseline",
        ], check=True, capture_output=True)
        run_id = "m0c-test-j"
        store, agents = self._create_terminal_run(run_id, executor_writes={})
        res = execute(store, agents=agents, gateway=FakeGateway(), terminal_acceptance=True)
        self.assertEqual(res["status"], "COMPLETED")
        self.assertTrue(store.is_terminal_accepted())
        self.assertEqual(store.state["manager"]["milestone_status"], "TERMINAL_ACCEPTED")
        self.assertEqual(len(store.state.get("verified_progress", [])), 1)

    # K. Optional critic configuration preserved
    def test_k_optional_critic_configuration_preserved(self):
        run_id = "m0c-test-k"
        store, agents = self._create_terminal_run(run_id, critic=False)
        res = execute(store, agents=agents, gateway=FakeGateway(), terminal_acceptance=True)
        self.assertEqual(res["status"], "COMPLETED")
        self.assertTrue(store.is_terminal_accepted())
        self.assertEqual(store.state["manager"]["milestone_status"], "TERMINAL_ACCEPTED")
        self.assertEqual(store.state["manager"]["reviewer_result"]["decision"], "APPROVE")
        self.assertFalse(store.state["options"]["critic"])


class TestM0CFocusedCorrections(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo, self.store, self.config = setup_test_fixture(self.root)
        self.registry = build_verifier_registry()

    def tearDown(self):
        self.tmp.cleanup()

    def _create_terminal_run(self, run_id, critic=True, reviewer=True, terminal_acceptance=True, executor_writes=None):
        store = durable.Store(self.root / "runs", run_id)
        spec = make_spec()
        create_run(
            store,
            self.repo,
            TASK,
            COMMANDS,
            self.config,
            criteria=["Multiply works"],
            work_units=[spec],
            verifier_registry=self.registry,
            critic=critic,
            reviewer=reviewer,
            terminal_acceptance=terminal_acceptance,
        )
        executor = ScriptedExecutor()
        writes = executor_writes if executor_writes is not None else {"calculator.py": "def multiply(a, b):\n    return a * b\n"}
        executor.set_behavior("unit-1", {"writes": writes})
        critic_adapter = ScriptedCritic("clean") if critic else None
        reviewer_adapter = ScriptedReviewer("approve") if reviewer else None
        agents = Agents(None, executor, critic_adapter, reviewer_adapter)
        return store, agents

    # 1. CHECKPOINT HISTORY:
    # Resume after interrupted finalization must not overwrite an existing checkpoint artifact with different content.
    # Preserve original references and hashes. Use unique checkpoint identities or safe reuse of an identical existing checkpoint.
    # Verify all original checkpoint references still load correctly after resume.
    def test_corr1_checkpoint_history_preserved_across_interrupted_finalization(self):
        run_id = "m0c-corr-1-history"
        store, agents = self._create_terminal_run(run_id, terminal_acceptance=True)
        attempts = [0]
        orig_commit_terminal = store.commit_terminal

        def interrupt_once(*args, **kwargs):
            if attempts[0] == 0:
                attempts[0] += 1
                raise KeyboardInterrupt("Simulated interruption during finalization")
            return orig_commit_terminal(*args, **kwargs)

        with patch.object(store, "commit_terminal", interrupt_once):
            with self.assertRaises(KeyboardInterrupt):
                execute(store, agents=agents, gateway=FakeGateway())

        self.assertFalse(store.is_terminal_accepted())
        lvc_before = store.state.get("last_verified_checkpoint")
        self.assertIsNotNone(lvc_before)
        orig_ref = lvc_before["reference"]
        orig_data = store.read_evidence(orig_ref)
        orig_sha = orig_ref["sha256"]
        orig_path = store.directory / orig_ref["path"]
        self.assertTrue(orig_path.exists())
        self.assertEqual(durable.digest(orig_data), orig_sha)

        # Resume the run and execute to terminal completion
        action = manager.prepare_resume(store)
        self.assertIn(action, ("continue", "verify_only"))
        res = execute(store, agents=agents, gateway=FakeGateway())
        self.assertEqual(res["status"], "COMPLETED")
        self.assertTrue(store.is_terminal_accepted())

        # Verify original checkpoint reference still loads correctly without checksum error
        loaded_orig = store.read_evidence(orig_ref)
        self.assertEqual(loaded_orig, orig_data)
        self.assertEqual(durable.digest(loaded_orig), orig_sha)

        # Verify all checkpoint references in verified_progress are valid and preserved
        progress = store.state.get("verified_progress", [])
        self.assertGreaterEqual(len(progress), 1)
        for entry in progress:
            ckpt_ref = entry["checkpoint"]
            ckpt_data = store.read_evidence(ckpt_ref)
            durable.validate_checkpoint_evidence(store, ckpt_data)

    # 2. TERMINAL MODE PERSISTENCE:
    # An explicit terminal_acceptance=True must survive interruption and resume.
    # Resolve the effective mode from durable run configuration when no explicit override is supplied.
    # Preserve legacy default behavior.
    def test_corr2_terminal_mode_persists_across_interruption_and_resume(self):
        run_id = "m0c-corr-2-mode"
        store, agents = self._create_terminal_run(run_id, terminal_acceptance=True)
        self.assertTrue(store.state.get("options", {}).get("terminal_acceptance"))

        attempts = [0]
        orig_commit_terminal = store.commit_terminal

        def interrupt_once(*args, **kwargs):
            if attempts[0] == 0:
                attempts[0] += 1
                raise KeyboardInterrupt("Simulated interruption during finalization")
            return orig_commit_terminal(*args, **kwargs)

        with patch.object(store, "commit_terminal", interrupt_once):
            with self.assertRaises(KeyboardInterrupt):
                execute(store, agents=agents, gateway=FakeGateway())

        self.assertFalse(store.is_terminal_accepted())

        # Prepare resume without explicit terminal_acceptance argument
        action = manager.prepare_resume(store)
        self.assertIn(action, ("continue", "verify_only"))

        # Execute without passing terminal_acceptance explicitly (default None)
        res = execute(store, agents=agents, gateway=FakeGateway())
        self.assertEqual(res["status"], "COMPLETED")
        self.assertTrue(store.is_terminal_accepted())
        self.assertEqual(store.state["manager"]["milestone_status"], "TERMINAL_ACCEPTED")

        # Verify legacy default behavior: run without terminal_acceptance remains non-terminal
        (self.repo.root / "calculator.py").write_text("def multiply(a, b):\n    return 0\n", encoding="utf-8")
        store_legacy, agents_legacy = self._create_terminal_run("m0c-corr-2-legacy", terminal_acceptance=False)
        self.assertFalse(store_legacy.state.get("options", {}).get("terminal_acceptance", False))
        res_legacy = execute(store_legacy, agents=agents_legacy, gateway=FakeGateway())
        self.assertEqual(res_legacy["status"], "COMPLETED")
        self.assertFalse(store_legacy.is_terminal_accepted())
        self.assertEqual(store_legacy.state["manager"]["milestone_status"], "TRUSTED_CHECKPOINT")

    # 3. REPEATED EXECUTION:
    # Calling execute() on an already terminal accepted run must return its accepted result without starting Supervisor or attempting new Store commits.
    # Verify this using both the original Store and a freshly loaded Store.
    def test_corr3_repeated_execute_idempotent_without_supervisor_or_commits(self):
        run_id = "m0c-corr-3-repeated"
        store, agents = self._create_terminal_run(run_id, terminal_acceptance=True)
        res1 = execute(store, agents=agents, gateway=FakeGateway())
        self.assertEqual(res1["status"], "COMPLETED")
        self.assertTrue(store.is_terminal_accepted())

        orig_records_len = len(store.records)
        orig_revision = store.state["revision"]

        # Call execute() on original Store; assert Supervisor is NOT started and no commits occur
        with patch("supervision.Supervisor.__enter__", side_effect=AssertionError("Supervisor must not start on terminal run")):
            with patch.object(store, "commit", side_effect=AssertionError("Store commit must not be called on terminal run")):
                res2 = execute(store, agents=agents, gateway=FakeGateway())

        self.assertEqual(res2["status"], "COMPLETED")
        self.assertEqual(res2["run_id"], run_id)
        self.assertEqual(len(store.records), orig_records_len)
        self.assertEqual(store.state["revision"], orig_revision)

        # Call execute() on freshly loaded Store
        fresh_store = durable.Store(store.directory.parent, store.directory.name)
        fresh_store.load()
        self.assertTrue(fresh_store.is_terminal_accepted())
        fresh_records_len = len(fresh_store.records)
        fresh_revision = fresh_store.state["revision"]

        with patch("supervision.Supervisor.__enter__", side_effect=AssertionError("Supervisor must not start on fresh terminal run")):
            with patch.object(fresh_store, "commit", side_effect=AssertionError("Store commit must not be called on fresh terminal run")):
                res3 = execute(fresh_store, agents=agents, gateway=FakeGateway())

        self.assertEqual(res3["status"], "COMPLETED")
        self.assertEqual(res3["run_id"], run_id)
        self.assertEqual(len(fresh_store.records), fresh_records_len)
        self.assertEqual(fresh_store.state["revision"], fresh_revision)

    # 4. WORKUNIT ENTRY POINTS:
    # execute_work_units() and plan_and_execute_work_units() must honor terminal_acceptance=True.
    # Route eligible completion through the same validated terminal lifecycle as Manager.execute().
    # Do not silently return MILESTONE_READY when terminal acceptance was requested.
    def test_corr4_workunit_entry_points_honor_terminal_acceptance(self):
        from work_unit_planner import ScriptedPlanner

        # Part A: execute_work_units with terminal_acceptance=True finalizes terminal acceptance
        run_id_ewu = "m0c-corr-4-ewu"
        store_ewu, agents_ewu = self._create_terminal_run(run_id_ewu, terminal_acceptance=True)
        res_ewu = manager.execute_work_units(
            store_ewu,
            agents=agents_ewu,
            gateway=FakeGateway(),
            verifier_registry=self.registry,
            terminal_acceptance=True,
        )
        self.assertEqual(res_ewu["status"], "COMPLETED")
        self.assertTrue(store_ewu.is_terminal_accepted())
        self.assertEqual(store_ewu.state["manager"]["milestone_status"], "TERMINAL_ACCEPTED")

        # Part B: plan_and_execute_work_units with terminal_acceptance=True finalizes terminal acceptance
        (self.repo.root / "calculator.py").write_text("def multiply(a, b):\n    return 0\n", encoding="utf-8")
        run_id_pewu = "m0c-corr-4-pewu"
        store_pewu = durable.Store(self.root / "runs", run_id_pewu)
        registry_pewu = {
            "check-calc": {
                "argv": ["python", "-m", "unittest", "test_calculator.py"],
                "timeout_seconds": 30,
                "modes": ["mutation"],
                "paths": ["calculator.py"],
                "capabilities": ["calculator.multiply.correctness"],
            }
        }
        cap_policy = {
            "allowed_unit_purposes": {
                "calculator_correctness": {
                    "required_capabilities": ["calculator.multiply.correctness"]
                }
            }
        }
        create_run(
            store_pewu,
            self.repo,
            TASK,
            COMMANDS,
            self.config,
            criteria=["Multiply works"],
            allow=["calculator.py"],
            work_unit_planning=True,
            verifier_registry=registry_pewu,
            capability_policy=cap_policy,
            critic=True,
            reviewer=True,
            terminal_acceptance=True,
        )
        prop_unit = {
            "alias": "u1",
            "objective": "Implement multiply",
            "dependencies": [],
            "mode": "mutation",
            "purpose": "calculator_correctness",
            "scope": {"allowed_paths": ["calculator.py"], "forbidden_paths": []},
            "verifier_ids": ["check-calc"],
        }
        planner = ScriptedPlanner([{"units": [prop_unit]}])
        executor_pewu = ScriptedExecutor()
        executor_pewu.set_behavior("wu-001", {"writes": {"calculator.py": "def multiply(a, b):\n    return a * b\n"}})
        agents_pewu = Agents(None, executor_pewu, ScriptedCritic("clean"), ScriptedReviewer("approve"))

        res_pewu = manager.plan_and_execute_work_units(
            store_pewu,
            planner,
            agents=agents_pewu,
            gateway=FakeGateway(),
            verifier_registry=registry_pewu,
            capability_policy=cap_policy,
            terminal_acceptance=True,
        )
        self.assertEqual(res_pewu["status"], "COMPLETED")
        self.assertTrue(store_pewu.is_terminal_accepted())
        self.assertEqual(store_pewu.state["manager"]["milestone_status"], "TERMINAL_ACCEPTED")

        # Part C: Verify legacy default (terminal_acceptance=False) returns MILESTONE_READY
        (self.repo.root / "calculator.py").write_text("def multiply(a, b):\n    return 0\n", encoding="utf-8")
        run_id_leg = "m0c-corr-4-legacy"
        store_leg, agents_leg = self._create_terminal_run(run_id_leg, terminal_acceptance=False)
        res_leg = manager.execute_work_units(
            store_leg,
            agents=agents_leg,
            gateway=FakeGateway(),
            verifier_registry=self.registry,
            terminal_acceptance=False,
        )
        self.assertEqual(res_leg["status"], "MILESTONE_READY")
        self.assertFalse(store_leg.is_terminal_accepted())


class TestM0CRegressionRepair(unittest.TestCase):
    """Four reproduction and regression tests for M0-C correctness defects."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo, self.store, self.config = setup_test_fixture(self.root)
        self.registry = build_verifier_registry()

    def tearDown(self):
        self.tmp.cleanup()

    def _create_terminal_run(self, run_id, critic=True, reviewer=True, terminal_acceptance=True, executor_writes=None):
        store = durable.Store(self.root / "runs", run_id)
        spec = make_spec()
        create_run(
            store,
            self.repo,
            TASK,
            COMMANDS,
            self.config,
            criteria=["Multiply works"],
            work_units=[spec],
            verifier_registry=self.registry,
            critic=critic,
            reviewer=reviewer,
            terminal_acceptance=terminal_acceptance,
        )
        executor = ScriptedExecutor()
        writes = executor_writes if executor_writes is not None else {"calculator.py": "def multiply(a, b):\n    return a * b\n"}
        executor.set_behavior("unit-1", {"writes": writes})
        critic_adapter = ScriptedCritic("clean") if critic else None
        reviewer_adapter = ScriptedReviewer("approve") if reviewer else None
        agents = Agents(None, executor, critic_adapter, reviewer_adapter)
        return store, agents

    def _create_critic_reviewed_run(self, run_id="cr-run", critic_findings=False, reviewer_option=True, critic_option=True):
        store = durable.Store(self.root / "runs", run_id)
        spec = make_spec()
        create_run(
            store,
            self.repo,
            TASK,
            COMMANDS,
            self.config,
            criteria=["Multiply works"],
            work_units=[spec],
            verifier_registry=self.registry,
            critic=critic_option,
            reviewer=reviewer_option,
        )
        executor = ScriptedExecutor()
        executor.set_behavior(
            "unit-1",
            {"writes": {"calculator.py": "def multiply(a, b):\n    return a * b\n"}},
        )
        scheduler = WorkUnitScheduler(
            store,
            self.repo,
            verifier_registry=self.registry,
            agents=Agents(None, executor, None, None),
            parent_scope={"allowed_paths": ["calculator.py"], "forbidden_paths": []},
        )
        res = scheduler.run_sequence()
        self.assertEqual(res["status"], "MILESTONE_READY")

        if critic_option:
            preset = "findings" if critic_findings else "clean"
            crit = CriticExecutor(store, self.repo, self.config, critic_adapter=ScriptedCritic(preset))
            crit_res = crit.execute()
            self.assertIn(crit_res["status"], (CRITIC_CLEAN, CRITIC_FINDINGS))
            self.assertEqual(store.state.get("manager", {}).get("milestone_status"), "CRITIC_REVIEWED")
        return store

    def test_repair1_checkpoint_reuse_requires_authoritative_controller_and_valid_readiness(self):
        # Setup run with established trusted checkpoint
        run_id = "m0c-rep-1-auth"
        store = self._create_critic_reviewed_run(run_id)
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev.execute()
        final_res = execute_final_verification(store, self.repo, self.config)
        ctrl_auth = {
            "authority": "store_controller",
            "run_id": run_id,
            "token": getattr(store, "_controller_token", None),
        }
        store.register_controller_authority(ctrl_auth)
        ckpt1 = create_trusted_checkpoint(store, self.repo, final_res, self.config, controller=ctrl_auth)
        self.assertEqual(store.state["manager"]["milestone_status"], "TRUSTED_CHECKPOINT")
        self.assertIsNotNone(store.state.get("last_verified_checkpoint"))

        # Mutate manager milestone status to PENDING
        mgr_pending = copy.deepcopy(store.state["manager"])
        mgr_pending["milestone_status"] = "PENDING"
        store.commit(manager=mgr_pending)
        pre_rev = store.state["revision"]

        # Scenario 1A: Unregistered controller must be rejected even when checkpoint is reusable
        unauth_ctrl = {"authority": "unregistered", "run_id": run_id, "token": "evil_token"}
        with self.assertRaises(ReviewerExecutionError) as ctx:
            create_trusted_checkpoint(store, self.repo, final_res, self.config, controller=unauth_ctrl)
        self.assertIn("Controller authority is not authorized", str(ctx.exception))
        self.assertEqual(store.state["revision"], pre_rev, "Revision must not advance on unauthorized controller")
        self.assertNotEqual(store.state["manager"].get("milestone_status"), "TRUSTED_CHECKPOINT")

        # Scenario 1B: Invalid readiness evidence must be rejected even when checkpoint is reusable
        invalid_final_res = copy.deepcopy(final_res)
        invalid_final_res["evidence"]["candidate_snapshot_digest"] = "bad_hash_forged"
        with self.assertRaises(ReviewerExecutionError) as ctx2:
            create_trusted_checkpoint(store, self.repo, invalid_final_res, self.config, controller=ctrl_auth)
        self.assertEqual(store.state["revision"], pre_rev, "Revision must not advance on invalid readiness")
        self.assertNotEqual(store.state["manager"].get("milestone_status"), "TRUSTED_CHECKPOINT")

        # Scenario 1C: Genuine repeated publication returns same checkpoint reference with 0 mutations
        pre_wal_len = len(store.records)
        ckpt2 = create_trusted_checkpoint(store, self.repo, final_res, self.config, controller=ctrl_auth)
        self.assertEqual(ckpt1["checkpoint_id"], ckpt2["checkpoint_id"])
        self.assertEqual(ckpt1["reference"], ckpt2["reference"])
        self.assertEqual(store.state["revision"], pre_rev, "Store revision must remain unchanged on read-only reuse")
        self.assertEqual(len(store.records), pre_wal_len, "WAL length must remain unchanged on read-only reuse")

    def test_repair2_checkpoint_identity_consistency_with_collision_never_differs_from_final_evidence(self):
        run_id = "m0c-rep-2-collision"
        store = self._create_critic_reviewed_run(run_id)
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev.execute()

        # Simulate collision on disk: 0000.json exists with different snapshot content
        ckpt_dir = store.directory / "checkpoints"
        ckpt_dir.mkdir(parents=True, exist_ok=True)
        collision_file = ckpt_dir / "0000.json"
        collision_file.write_text(json.dumps({"collision": True, "checkpoint_id": f"ckpt-milestone-{run_id}"}), encoding="utf-8")

        # Run final verification and checkpoint creation
        final_res = execute_final_verification(store, self.repo, self.config)
        ctrl_auth = {
            "authority": "store_controller",
            "run_id": run_id,
            "token": getattr(store, "_controller_token", None),
        }
        store.register_controller_authority(ctrl_auth)

        # Fix 2 Check D: Any attempt to alter destination after verification fails validation
        with self.assertRaises(ReviewerExecutionError) as ctx_path:
            validate_acceptance_readiness(
                store, self.repo, final_res, config=self.config, controller=ctrl_auth,
                checkpoint_path="checkpoints/altered.json"
            )
        self.assertIn("Checkpoint path mismatch", str(ctx_path.exception))

        with self.assertRaises(ReviewerExecutionError) as ctx_id:
            validate_acceptance_readiness(
                store, self.repo, final_res, config=self.config, controller=ctrl_auth,
                checkpoint_id="altered_id"
            )
        self.assertIn("Checkpoint identity mismatch", str(ctx_id.exception))

        ckpt_res = create_trusted_checkpoint(store, self.repo, final_res, self.config, controller=ctrl_auth)

        # Fix 2 Check A: Attempt-start and attempt-end WAL contain identical checkpoint ID and path
        start_rec = store.records[final_res["evidence"]["attempt_start_wal_sequence"] - 1]
        end_rec = store.records[final_res["evidence"]["attempt_end_wal_sequence"] - 1]
        self.assertEqual(start_rec["checkpoint_id"], end_rec["checkpoint_id"],
                         "attempt-start and attempt-end WAL must have identical checkpoint_id")
        self.assertEqual(start_rec["checkpoint_path"], end_rec["checkpoint_path"],
                         "attempt-start and attempt-end WAL must have identical checkpoint_path")

        # Fix 2 Check B: Final evidence and published checkpoint use exact values decided before verification
        self.assertEqual(final_res["evidence"]["checkpoint_id"], start_rec["checkpoint_id"])
        self.assertEqual(final_res["evidence"]["checkpoint_path"], start_rec["checkpoint_path"])
        self.assertEqual(ckpt_res["checkpoint_id"], final_res["evidence"]["checkpoint_id"],
                         "Published checkpoint_id must match final verification evidence checkpoint_id")
        self.assertEqual(ckpt_res["reference"]["path"], final_res["evidence"]["checkpoint_path"],
                         "Published checkpoint path must match final verification evidence checkpoint_path")

        # Fix 2 Check C: Prior file 0000.json collision was handled before verification
        collision_data = json.loads(collision_file.read_text(encoding="utf-8"))
        self.assertTrue(collision_data.get("collision"))
        self.assertIn("_01", start_rec["checkpoint_id"])
        self.assertIn("_01", start_rec["checkpoint_path"])

    def test_repair3_immutable_critic_disabled_evidence_across_interruption_and_resume(self):
        run_id = "m0c-rep-3-disabled-critic"
        store, agents = self._create_terminal_run(run_id, critic=False)

        # Run milestone in non-terminal mode to establish checkpoint 1
        res1 = manager.execute_work_units(
            store,
            agents=agents,
            gateway=FakeGateway(),
            verifier_registry=self.registry,
            terminal_acceptance=False,
        )
        # Run reviewer, final verification and create checkpoint 1 with disabled critic
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev.execute()
        final_res1 = execute_final_verification(store, self.repo, self.config)
        ctrl_auth = {
            "authority": "store_controller",
            "run_id": run_id,
            "token": getattr(store, "_controller_token", None),
        }
        store.register_controller_authority(ctrl_auth)
        ckpt1_res = create_trusted_checkpoint(store, self.repo, final_res1, self.config, controller=ctrl_auth)
        critic_ref1 = ckpt1_res["checkpoint"].get("critic")
        self.assertIsNotNone(critic_ref1)
        # Verify initial evidence loads cleanly
        evidence1 = store.read_evidence(critic_ref1)
        self.assertEqual(evidence1["status"], "CRITIC_CLEAN")

        # Advance round to simulate new step / recovery creating second checkpoint
        store.commit(round_number=store.state["round_number"] + 1)
        final_res2 = execute_final_verification(store, self.repo, self.config)
        ctrl_auth = {
            "authority": "store_controller",
            "run_id": run_id,
            "token": getattr(store, "_controller_token", None),
        }
        store.register_controller_authority(ctrl_auth)
        ckpt2_res = create_trusted_checkpoint(store, self.repo, final_res2, self.config, controller=ctrl_auth)
        critic_ref2 = ckpt2_res["checkpoint"].get("critic")
        self.assertIsNotNone(critic_ref2)

        # Reload store fresh from disk and validate ALL historical checkpoint and evidence references
        reloaded = durable.Store(self.root / "runs", run_id)
        reloaded.load()
        # Historical reference from checkpoint 1 MUST still be readable and valid
        reloaded_evidence1 = reloaded.read_evidence(critic_ref1)
        self.assertEqual(reloaded_evidence1["status"], "CRITIC_CLEAN")
        # Reference from checkpoint 2 MUST also be readable and valid
        reloaded_evidence2 = reloaded.read_evidence(critic_ref2)
        self.assertEqual(reloaded_evidence2["status"], "CRITIC_CLEAN")

    def test_repair4_terminal_report_consistency_after_resume(self):
        run_id = "m0c-rep-4-report"
        store, agents = self._create_terminal_run(run_id, reviewer=False, terminal_acceptance=False)

        # Produce READY report in non-terminal mode without reviewer
        res1 = manager.execute(
            store,
            agents=agents,
            gateway=FakeGateway(),
            terminal_acceptance=False,
        )
        self.assertEqual(res1["status"], "READY")
        report_file = store.directory / "final-report.json"
        self.assertTrue(report_file.exists())
        cached_ready = json.loads(report_file.read_text(encoding="utf-8"))
        self.assertEqual(cached_ready["status"], "READY")

        # Configure reviewer and resume with terminal_acceptance=True
        store.commit(options={**store.state["options"], "reviewer": True, "terminal_acceptance": True})
        agents.reviewer = ScriptedReviewer("approve")
        res2 = manager.resume(
            store,
            agents=agents,
            gateway=FakeGateway(),
            terminal_acceptance=True,
        )
        self.assertEqual(res2["status"], "COMPLETED")
        self.assertTrue(store.is_terminal_accepted())

        # Calling execute() on original Store must return COMPLETED with terminal acceptance
        exec_orig = manager.execute(store, terminal_acceptance=True)
        self.assertEqual(exec_orig["status"], "COMPLETED")
        self.assertTrue(exec_orig.get("terminal_accepted"))

        # Calling execute() on freshly loaded Store must also return COMPLETED with terminal acceptance
        fresh_store = durable.Store(self.root / "runs", run_id)
        fresh_store.load()
        self.assertTrue(fresh_store.is_terminal_accepted())
        exec_fresh = manager.execute(fresh_store, terminal_acceptance=True)
        self.assertEqual(exec_fresh["status"], "COMPLETED")
        self.assertTrue(exec_fresh.get("terminal_accepted"))

    def test_regression_checkpoint_reuse_provenance_rejects_copied_artifact_and_preserves_legitimate_reuse(self):
        run_id = "m0c-reg-provenance"
        store = self._create_critic_reviewed_run(run_id)
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev.execute()
        final_res = execute_final_verification(store, self.repo, self.config)

        ctrl_auth = {
            "authority": "store_controller",
            "run_id": run_id,
            "token": getattr(store, "_controller_token", None),
        }
        store.register_controller_authority(ctrl_auth)

        # 1. Legitimately publish trusted checkpoint
        ckpt1 = create_trusted_checkpoint(store, self.repo, final_res, self.config, controller=ctrl_auth)
        legit_ref = ckpt1["reference"]
        self.assertEqual(legit_ref["path"], "checkpoints/0000.json")

        pre_rev = store.state["revision"]
        pre_wal_len = len(store.records)

        # 2. Copy approved checkpoint artifact to another path
        copied_path = "checkpoints/copied_0000.json"
        full_copied = store.directory / copied_path
        shutil.copy2(store.directory / legit_ref["path"], full_copied)
        copied_ref = {"path": copied_path, "sha256": legit_ref["sha256"]}

        # 3. Redirect last_verified_checkpoint to the copy
        store.state["last_verified_checkpoint"] = {
            "reference": copied_ref,
            "snapshot": snapshot(self.repo),
        }

        # 4. Attempting reuse with the copied reference MUST be rejected
        with self.assertRaises(ReviewerExecutionError) as ctx:
            create_trusted_checkpoint(store, self.repo, final_res, self.config, controller=ctrl_auth)
        self.assertIn("copied or substituted checkpoint rejected", str(ctx.exception).lower())

        # 5. Confirm NO Store mutation and NO new WAL records on rejected reuse
        self.assertEqual(store.state["revision"], pre_rev, "Store revision must not mutate on rejected reuse")
        self.assertEqual(len(store.records), pre_wal_len, "WAL records must not be appended on rejected reuse")

        # 6. Restore original legitimate reference in last_verified_checkpoint
        store.state["last_verified_checkpoint"] = {
            "reference": legit_ref,
            "snapshot": snapshot(self.repo),
        }

        # 7. Original legitimate reference must still be reusable without Store mutation
        ckpt2 = create_trusted_checkpoint(store, self.repo, final_res, self.config, controller=ctrl_auth)
        self.assertEqual(ckpt2["reference"], legit_ref)
        self.assertEqual(ckpt2["checkpoint_id"], ckpt1["checkpoint_id"])
        self.assertEqual(store.state["revision"], pre_rev, "Store revision must not mutate on valid reuse")
        self.assertEqual(len(store.records), pre_wal_len, "WAL length must not mutate on valid reuse")

    def test_regression_atomic_attempt_end_revision_with_heartbeat_at_boundary(self):
        run_id = "m0c-reg-atomic-rev"
        store = self._create_critic_reviewed_run(run_id)
        sup = supervision.Supervisor(store)

        # Execute reviewer
        rev = ReviewerExecutor(store, self.repo, self.config, reviewer_adapter=ScriptedReviewer("approve"))
        rev.execute()

        ctrl_auth = {
            "authority": "store_controller",
            "run_id": run_id,
            "token": getattr(store, "_controller_token", None),
        }
        store.register_controller_authority(ctrl_auth)

        # Trigger genuine heartbeat right before / during final verification
        sup.heartbeat()
        hb_rev_before = store.state["revision"]

        # Run final verification (protected with store.mutex for atomic revision and attempt-end WAL append)
        final_res = execute_final_verification(store, self.repo, self.config)

        # Trigger genuine heartbeat right at the publication boundary (after final verification commit)
        sup.heartbeat()
        hb_rev_after = store.state["revision"]
        self.assertGreater(hb_rev_after, hb_rev_before)

        # Check authoritative attempt-end record revision matches completion commit
        end_seq = final_res["evidence"]["attempt_end_wal_sequence"]
        end_rec = store.records[end_seq - 1]
        completion_rec = store.records[end_seq]
        self.assertEqual(end_rec["store_revision"], completion_rec["state"]["revision"],
                         "Authoritative WAL revision in attempt-end must equal completion commit revision")
        self.assertEqual(final_res["evidence"]["store_revision"], end_rec["store_revision"],
                         "Final verification evidence store_revision must match attempt-end record")

        # Readiness succeeds for genuine unchanged verification despite subsequent heartbeat
        receipt = validate_acceptance_readiness(store, self.repo, final_res, config=self.config, controller=ctrl_auth)
        self.assertTrue(receipt["ready"])
        self.assertTrue(receipt["controller_authorized"])

        # Store reload preserves the result
        fresh_store = durable.Store(self.root / "runs", run_id)
        fresh_store.load()
        fresh_store.register_controller_authority(ctrl_auth)
        fresh_receipt = validate_acceptance_readiness(fresh_store, self.repo, final_res, config=self.config, controller=ctrl_auth)
        self.assertTrue(fresh_receipt["ready"])

        # Unrelated semantic mutations remain rejected
        store.commit(remaining_work=["unauthorized_task_mutation"])
        with self.assertRaises(ReviewerExecutionError) as ctx_sem:
            validate_acceptance_readiness(store, self.repo, final_res, config=self.config, controller=ctrl_auth)
        self.assertTrue(
            "semantic transaction" in str(ctx_sem.exception) or "semantic state mutation" in str(ctx_sem.exception),
            f"Expected semantic rejection, got: {ctx_sem.exception}",
        )
