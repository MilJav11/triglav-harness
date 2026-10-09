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
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

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

    def unload(self):
        self.events.append("unload")
        self.loaded = None

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
