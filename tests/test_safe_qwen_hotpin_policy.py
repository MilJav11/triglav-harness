"""Offline HotPin contract tests: tiny fake assets, no native enforcement or models."""
import copy
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import durable
import environment
import harness
import manager
import safe_executor_binding as binding
import terminal_run as launch
import test_safe_qwen_integration as integration


class HotPinPolicyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = integration.IntegrationTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.expert = self.fixture.resource / "fake-experts.txt"
        self.expert.write_bytes(b"offline expert fixture A\n")
        self.policy = {
            "version": 1, "type": "hotpin", "hot_experts": "runtime/fake-experts.txt",
            "hot_experts_sha256": hashlib.sha256(self.expert.read_bytes()).hexdigest(),
            "min_available_ram_gb": 44, "max_working_set_gb": 32}
        self.fixture.cfg["safe_qwen_reviewer_policy"] = copy.deepcopy(self.policy)
        self.fixture.cfg["model_metadata"][self.fixture.cfg["roles"]["review"]]["runtime"] = "HotPin"

    def configure(self):
        return self.fixture.configure(critic=True, reviewer=True)

    def prepared(self):
        return self.fixture.prepared(critic=True, reviewer=True)

    def cli_config(self):
        self.fixture.cfg["safe_qwen_runtime"].update(
            critic=self.fixture.files(), review=self.fixture.files())
        return self.fixture.cfg

    def test_valid_policy_resolves_against_operator_config_without_mutating_input(self):
        before = copy.deepcopy(self.fixture.cfg)
        worker, cfg = self.configure()
        self.assertEqual(Path(cfg["safe_qwen_reviewer_policy"]["hot_experts"]), self.expert.resolve())
        self.assertEqual(self.fixture.cfg, before)
        self.assertEqual(binding.restore_worker(cfg).policy, worker.policy)
        self.assertEqual(cfg["safe_qwen_reviewer_policy"]["max_working_set_gb"], 32)
        self.assertEqual(cfg["safe_qwen_reviewer_policy"]["min_available_ram_gb"], 44)

    def test_absolute_local_expert_path_is_supported(self):
        self.fixture.cfg["safe_qwen_reviewer_policy"]["hot_experts"] = str(self.expert.resolve())
        self.configure()

    def test_unknown_or_missing_fields_are_refused(self):
        invalid = [{**self.policy, "enforcement_complete": True}]
        invalid += [{k: v for k, v in self.policy.items() if k != key} for key in self.policy]
        invalid += [None, [], "hotpin"]
        for policy in invalid:
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                self.fixture.cfg["safe_qwen_reviewer_policy"] = policy
                self.configure()

    def test_unsupported_versions_and_types_are_refused(self):
        for key, values in (("version", (True, 1.0, 0, 2, "1", None)),
                            ("type", (True, 1, "HotPin", "standard", "", None))):
            for value in values:
                self.fixture.cfg["safe_qwen_reviewer_policy"] = {**self.policy, key: value}
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    self.configure()

    def test_resource_values_are_exact_integers(self):
        for key in ("min_available_ram_gb", "max_working_set_gb"):
            for value in (True, None, "32", 32.0, 44.0, 0, -1, 31, 33, 43, 45,
                          float("nan"), float("inf")):
                self.fixture.cfg["safe_qwen_reviewer_policy"] = {**self.policy, key: value}
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    self.configure()

    def test_expected_hash_format_is_strict(self):
        for value in (None, 1, True, "", "0" * 63, "0" * 65, "g" * 64, "A" * 64):
            self.fixture.cfg["safe_qwen_reviewer_policy"] = {**self.policy, "hot_experts_sha256": value}
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.configure()

    def test_invalid_nonlocal_and_missing_assets_are_refused(self):
        for value in (None, 1, "", " ", "bad\0path", "//server/share/expert.txt",
                      "\\\\server\\share\\expert.txt", "C:relative.txt", "runtime/missing.txt", "runtime"):
            self.fixture.cfg["safe_qwen_reviewer_policy"] = {**self.policy, "hot_experts": value}
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.configure()

    def test_hash_mismatch_is_refused(self):
        self.fixture.cfg["safe_qwen_reviewer_policy"]["hot_experts_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            self.configure()

    def test_deleted_expert_is_refused_at_configuration(self):
        self.expert.unlink()
        with self.assertRaisesRegex(ValueError, "missing or unavailable"):
            self.configure()

    def test_content_identity_is_strongly_bound_to_environment(self):
        worker, cfg = self.prepared()
        fp = self.fixture.store.state["environment"]
        asset = fp["runtime"]["safe_qwen:review:hot_experts"]
        self.assertEqual(asset["sha256"], self.policy["hot_experts_sha256"])
        self.assertEqual(Path(asset["path"]), self.expert.resolve())
        self.assertEqual(fp["runtime"]["safe_qwen:review:resource_policy"]["sha256"],
                         binding.digest(cfg["safe_qwen_reviewer_policy"]))
        self.assertEqual(binding.identity(worker), cfg["safe_qwen_identity"])

    def test_changed_policy_cannot_reuse_configuration_pin(self):
        worker, cfg = self.configure()
        cfg["safe_qwen_reviewer_policy"]["hot_experts_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "pinned source/configuration"):
            binding.validate_pinned(cfg)

    def test_valid_configuration_drift_is_unsafe(self):
        worker, cfg = self.prepared()
        self.fixture.cfg["base_url"] = "http://127.0.0.1:9444"
        other, changed = self.configure()
        after = environment.capture(changed, self.fixture.repo.root, [self.fixture.command], executor=other)
        result = environment.compare(self.fixture.store.state["environment"], after)
        self.assertEqual(result["decision"], "UNSAFE")
        self.assertTrue(any(c["field"] == "config_sha256" for c in result["changes"]))

    def test_restore_refuses_changed_bytes_even_with_same_size_and_timestamp(self):
        worker, cfg = self.configure()
        original = self.expert.stat()
        self.expert.write_bytes(b"offline expert fixture B\n")
        os.utime(self.expert, ns=(original.st_atime_ns, original.st_mtime_ns))
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            binding.restore_worker(cfg)

    def test_restore_refuses_expert_deletion(self):
        worker, cfg = self.configure()
        self.expert.unlink()
        with self.assertRaisesRegex(ValueError, "missing or unavailable"):
            binding.restore_worker(cfg)

    def test_resume_refuses_changed_expert_before_controller_execution(self):
        self.prepared()
        self.expert.write_bytes(b"offline expert fixture B\n")
        reloaded = durable.Store(self.fixture.root / "runs", self.fixture.store.state["run_id"])
        reloaded.load()
        with patch.object(manager, "execute") as execute, self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            launch.resume(reloaded, self.fixture.args())
        execute.assert_not_called()

    def test_declared_hotpin_without_policy_is_refused(self):
        del self.fixture.cfg["safe_qwen_reviewer_policy"]
        with self.assertRaisesRegex(ValueError, "requires an explicit resource policy"):
            self.configure()

    def test_policy_without_enabled_review_is_refused(self):
        with self.assertRaisesRegex(ValueError, "requires an enabled review runtime"):
            self.fixture.configure()

    def test_legacy_profiles_remain_refused(self):
        self.fixture.cfg["review_profiles"] = {"review-safe": {"hot_experts": str(self.expert)}}
        with self.assertRaisesRegex(ValueError, "implicit HotPin profile"):
            self.configure()

    def test_legacy_profile_selection_conflicts_with_explicit_policy(self):
        self.fixture.cfg["review_profile"] = "review-safe"
        with self.assertRaisesRegex(ValueError, "cannot select legacy profiles"):
            self.configure()

    def test_code_only_and_generic_reviewer_remain_supported(self):
        del self.fixture.cfg["safe_qwen_reviewer_policy"]
        worker, code = self.fixture.configure()
        binding.require_reviewer_enforcement(code)
        self.fixture.cfg["model_metadata"][self.fixture.cfg["roles"]["review"]]["runtime"] = "llama.cpp"
        worker, generic = self.configure()
        binding.require_reviewer_enforcement(generic)
        self.assertIsNone(binding.reviewer_expert_identity(generic))
        self.assertEqual(binding.restore_worker(generic).policy, worker.policy)

    def test_dry_run_reports_required_native_enforcement(self):
        cfg = self.cli_config()
        result = manager.cli(self.fixture.args(critic=True, reviewer=True, review_policy="three-model"),
                             self.fixture.root, cfg)
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["reviewer_resource_policy"]["native_enforcement"], "required_before_inference")
        self.assertEqual(result["reviewer_resource_policy"]["dispatch"], "subject_to_live_native_gates")

    def test_failed_native_preflight_refuses_cli_before_gateway_or_run_creation(self):
        cfg = self.cli_config()
        with patch.object(harness, "Gateway") as gateway, patch.object(launch, "execute_bound") as execute:
            with patch.object(durable, "Store") as store, patch.object(binding, "require_reviewer_enforcement",
                    side_effect=RuntimeError("native preflight unavailable")):
                with self.assertRaisesRegex(RuntimeError, "native preflight"):
                    manager.cli(self.fixture.args(critic=True, reviewer=True, dry_run=False), self.fixture.root, cfg)
        gateway.assert_not_called()
        execute.assert_not_called()
        store.assert_not_called()

    def test_bound_execution_delegates_to_existing_manager_after_native_preflight(self):
        worker, cfg = self.prepared()
        result = {"stop": {"cleanup": "passed"}}
        with patch.object(manager, "execute", return_value=result) as execute:
            self.assertEqual(launch.execute_bound(self.fixture.store, worker), result)
        execute.assert_called_once()

    def test_gateway_gate_cannot_be_bypassed_by_cached_ready_role(self):
        worker, cfg = self.configure()
        gateway = harness.Gateway(cfg, None)
        gateway.active_role = "review"
        with patch.object(gateway, "request") as request, patch.object(gateway, "running") as running:
            with patch.object(gateway, "unload") as unload, self.assertRaisesRegex(durable.DurableError, "pinned Store"):
                gateway._switch("review")
        request.assert_not_called()
        running.assert_not_called()
        unload.assert_not_called()

    def test_unsupported_native_platform_refuses_supervisor_dispatch(self):
        self.prepared()
        supervisor = SimpleNamespace(store=self.fixture.store, require_no_ambiguous_model=Mock(), register=Mock())
        with patch("ctypes.sizeof", return_value=4), self.assertRaisesRegex(RuntimeError, "native x64 Windows"):
            binding.model_starting(supervisor, "review")
        supervisor.register.assert_not_called()
        supervisor.require_no_ambiguous_model.assert_not_called()

    def test_configuration_flag_cannot_bypass_pinned_identity(self):
        worker, cfg = self.configure()
        cfg["hotpin_native_enforcement_complete"] = True
        with self.assertRaisesRegex(ValueError, "pinned source/configuration"):
            binding.validate_pinned(cfg)
