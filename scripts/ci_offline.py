"""Run only reviewed offline CI checks; this is not a hostile-code sandbox."""
from __future__ import annotations

import ast
import contextlib
import hashlib
import io
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
MODULES = {"test_milestone_timeout": 10, "test_ci_offline": 35, "test_executor_protocol": 8, "test_planner_protocol": 14, "test_safe_qwen_worker": 39, "test_safe_qwen_integration": 40, "test_safe_qwen_hotpin_policy": 26, "test_safe_qwen_hotpin_native": 54, "test_safe_qwen_multi_unit": 15, "test_safe_qwen_multi_unit_prod": 2, "test_supervision.SupervisorLifecycleTests": 25,
           "test_durable.TerminalAcceptanceTests": 9,
           "test_durable.TerminalAcceptanceFaultRegressionTests": 10,
           "test_durable.CodexNineDefectRegressionTests": 9,
           "test_durable.CodexFourCorrectionsRegressionTests": 5,
           "test_durable.CodexTwoInvariantRegressionTests": 3,
           "test_durable.CodexCanonicalEqualityRegressionTests": 3,
           **{f"test_reviewer_wave6.TestReviewerWave6.{name}": 1 for name in (
               "test_42_final_verification_runs_after_approve",
               "test_43_old_verifier_pass_cannot_substitute_final_verification",
               "test_44_final_verification_failure_no_checkpoint",
               "test_45_scope_failure_during_final_verification_no_checkpoint",
               "test_46_candidate_changed_after_approve_no_checkpoint",
               "test_47_environment_changed_after_approve_no_checkpoint",
               "test_49_controller_creates_trusted_checkpoint_only_after_all_steps_pass",
               "test_50_trusted_checkpoint_updates_last_verified_checkpoint_and_verified_progress")},
           **{f"test_reviewer_wave6.TestControllerAuthorityM0B.{name}": 1 for name in (
               "test_01_null_token_simplenamespace_rejected",
               "test_02_fake_final_artifact_with_zero_executions_rejected",
               "test_03_verifier_registry_substitution_after_senior_approve_rejected",
               "test_04_missing_store_revision_rejected",
               "test_05_wrong_checkpoint_identity_rejected",
               "test_06_missing_filesystem_digest_rejected",
               "test_07_contradictory_run_id_or_verifier_hash_rejected",
               "test_08_wrong_reviewer_or_environment_digest_rejected",
               "test_09_authority_omitted_rejected",
               "test_10_arbitrary_dict_or_string_authority_rejected",
               "test_11_manager_authority_used_with_another_store_rejected",
               "test_12_missing_persisted_reviewer_artifact_despite_caller_approve_rejected",
               "test_13_reuse_after_store_revision_changes_rejected",
               "test_14_genuine_production_verifier_and_reviewer_accepted",
               "test_15_intermediate_checkpoints_and_lifecycle_preserved",
               "test_16_two_consecutive_rounds_and_resume_preserved",
               "test_17_no_diff_acceptance_preserved",
               "test_18_optional_critic_disabled_lifecycle",
               "test_19_two_verifier_id_argv_swap_rejected",
               "test_20_prior_final_attempt_replayed_with_new_metadata_rejected",
               "test_21_fake_attempt_start_end_without_wal_events_rejected",
               "test_22_attempt_identity_survives_reload_no_reuse",
               "test_23_extra_wal_event_after_approval_cannot_replace_identity",
               "test_24a_wrong_approval_event_type_rejected",
               "test_24b_corrupt_approval_payload_rejected",
               "test_25_supervisor_heartbeat_preserved",
               "test_26_semantic_mutation_in_heartbeat_interval_rejected",
               "test_26b_non_heartbeat_event_rejected",
               "test_27_real_two_verifier_production_execution_validates_mapping",
               "test_28_publisher_refuses_checkpoint_when_readiness_rejects",
               "test_29_old_attempt_with_rewritten_revision_rejected_by_wal_anchor",
               "test_30_duplicate_wal_sequence_for_two_verifiers_rejected",
               "test_31_missing_or_none_approval_payload_rejected",
               "test_32_arbitrary_supervision_mutation_disguised_as_heartbeat_rejected",
               "test_33_unknown_wal_event_without_revision_increment_rejected",
               "test_34_unrelated_wal_insertion_during_commit_projection_preserves_exact_receipt",
               "test_35_genuine_two_verifier_production_checkpoint_and_reload",
               "test_36_working_file_modified_before_pre_write_guard_refuses_checkpoint",
               "test_37_real_supervisor_heartbeat_succeeds",
               "test_38a_heartbeat_command_sha256_change_rejected",
               "test_38b_heartbeat_process_identity_change_rejected",
               "test_39_heartbeat_pid_executable_or_ownership_change_rejected",
               "test_40_heartbeat_unexpected_supervision_field_or_state_change_rejected")},
           **{f"test_reviewer_wave6.TestTerminalLifecycleM0C.{name}": 1 for name in (
               "test_a_genuine_successful_terminal_lifecycle",
               "test_b_cleanup_failure_prevents_acceptance",
               "test_c_supervisor_shutdown_incomplete",
               "test_d_gateway_unload_incomplete",
               "test_e_changed_candidate_after_cleanup",
               "test_f_wal_commit_failure",
               "test_g_reload_of_terminal_accepted_run",
               "test_h_recovery_from_interrupted_finalization",
               "test_i_intermediate_checkpoints_preserved",
               "test_j_no_diff_acceptance_preserved",
               "test_k_optional_critic_configuration_preserved")},
           **{f"test_reviewer_wave6.TestM0CFocusedCorrections.{name}": 1 for name in (
               "test_corr1_checkpoint_history_preserved_across_interrupted_finalization",
               "test_corr2_terminal_mode_persists_across_interruption_and_resume",
               "test_corr3_repeated_execute_idempotent_without_supervisor_or_commits",
               "test_corr4_workunit_entry_points_honor_terminal_acceptance")},
           **{f"test_reviewer_wave6.TestM0CRegressionRepair.{name}": 1 for name in (
               "test_repair1_checkpoint_reuse_requires_authoritative_controller_and_valid_readiness",
               "test_repair2_checkpoint_identity_consistency_with_collision_never_differs_from_final_evidence",
               "test_repair3_immutable_critic_disabled_evidence_across_interruption_and_resume",
               "test_repair4_terminal_report_consistency_after_resume",
               "test_regression_checkpoint_reuse_provenance_rejects_copied_artifact_and_preserves_legitimate_reuse",
               "test_regression_atomic_attempt_end_revision_with_heartbeat_at_boundary")},
           **{name: 1 for name in (
               "test_manager.LoopSuccessTests.test_multi_round_success_carries_regression_checks",
               "test_manager.InterruptionTests.test_resume_from_trusted_checkpoint_keeps_it_and_continues",
               "test_convergence.RoundConvergenceTests.test_verifier_evidence_bound_replan_preserves_contract_and_no_false_trust",
               "test_manager.LoopFailureTests.test_failed_verification_never_checkpoints",
               "test_manager.CriticReviewerTests.test_reviewer_rejection_with_passing_verification_cannot_checkpoint",
               "test_manager.CriticReviewerTests.test_reviewer_approval_with_failed_final_verification_cannot_checkpoint",
               "test_manager.InterruptionTests.test_completed_run_resume_is_idempotent",
               "test_manager.SupervisionAndDriftTests.test_unknown_model_child_blocks_resume_and_is_never_auto_resolved",
               "test_manager.SupervisionAndDriftTests.test_repository_head_drift_refuses_resume_without_model_or_state_change",
               "test_manager.LoopFailureTests.test_executor_modifying_a_forbidden_file_cannot_checkpoint",
               "test_manager.ExplicitThreeModelTests.test_bounded_multistep_autonomy_with_recovery_and_resume")}}
READ_ONLY_GIT = {"rev-parse", "status", "ls-files", "diff", "show", "log", "ls-tree", "cat-file", "symbolic-ref"}
FIXTURE_GIT = READ_ONLY_GIT | {"init", "add", "commit", "hash-object", "write-tree"}

TEST_ADD_SOURCE = (
    "import unittest\n"
    "from calc import add\n"
    "class T(unittest.TestCase):\n"
    "    def test_add(self): self.assertEqual(add(2, 3), 5)\n"
)
TEST_SUB_SOURCE = (
    "import unittest\n"
    "from calc import sub\n"
    "class T(unittest.TestCase):\n"
    "    def test_sub(self): self.assertEqual(sub(5, 3), 2)\n"
)
TEST_CALCULATOR_W6_SOURCE = (
    "import unittest\nfrom calculator import multiply\n"
    "class T(unittest.TestCase):\n"
    "    def test_product(self): self.assertEqual(multiply(2, 3), 6)\n"
    "if __name__ == '__main__': unittest.main()\n"
)
TEST_CALCULATOR_TIMEOUT_SOURCE = (
    "import time, unittest\n"
    "class Slow(unittest.TestCase):\n"
    "    def test_over_budget(self): time.sleep(10)\n"
)


def content_digest(data: bytes) -> str:
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


HASH_TEST_ADD = content_digest(TEST_ADD_SOURCE.encode("utf-8"))
HASH_TEST_SUB = content_digest(TEST_SUB_SOURCE.encode("utf-8"))
HASH_TEST_CALC_W6 = content_digest(TEST_CALCULATOR_W6_SOURCE.encode("utf-8"))
HASH_TEST_CALC_TIMEOUT = content_digest(TEST_CALCULATOR_TIMEOUT_SOURCE.encode("utf-8"))
HASH_TEST_CALC_MULTI_UNIT = "df99c0ff4342e373fc3faba9b54429ac4eade03ca430afd340dce9074b6dd4a6"

APPROVED_MODULE_HASHES = {
    "test_add": {HASH_TEST_ADD},
    "test_sub": {HASH_TEST_SUB},
    "test_calculator.py": {HASH_TEST_CALC_W6, HASH_TEST_CALC_TIMEOUT},
}

APPROVED_MULTI_UNIT_TARGETS = {
    "test_calculator.RequiredMultiplicationTests",
    "test_calculator.ExistingAdditionTests",
    "test_calculator.RequiredScaleTests",
}

APPROVED_MULTI_UNIT_CALCULATOR_HASHES = {
    "fd2959512184ad8d7ce8f6346ebb09c3bc9e29505272243eeaa909b674276eda",  # baseline
    "8491cb5e6d30aa1c0534b075a2d8ffbf7dafc67f5b275b98ec6cf537d87a1b09",  # unit 1 (multiply)
    "6acf671b80b9f77ad55249ae59839a1443a86b44e631e0cd8e17e506285fc21e",  # unit 2 (scale)
    "1c82fb9ee7d7ea2763fa6d6eeb33cd8233ca008343a33b89c2855c593e9b5806",  # negative (broken add)
}

CALC_CANDIDATE_SOURCES = [
    # Baseline
    "def add(a, b):\n    return 0\n\ndef sub(a, b):\n    return 0\n",
    "def add(a, b): return 0\ndef sub(a, b): return 0\n",
    # Repaired
    "def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return 0\n",
    "def add(a, b): return a + b\ndef sub(a, b): return 0\n",
    'def add(a, b):\n    """Add."""\n    return a + b\n\ndef sub(a, b):\n    return 0\n',
    # Completed
    "def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a - b\n",
    "def add(a, b): return a + b\ndef sub(a, b): return a - b\n",
    # Deliberately failing attempts
    *(f"def add(a, b):\n    return 0  # attempt {tag}\n\ndef sub(a, b):\n    return 0\n"
      for tag in ("1", "2", "3", "4", "x")),
    *(f"def add(a, b):\n    return {n}\n\ndef sub(a, b):\n    return 0\n"
      for n in (0, 1, 2, 3)),
]

CALCULATOR_CANDIDATE_SOURCES = [
    # Baseline / Failing
    "def multiply(a, b):\n    return 0\n",
    "def multiply(a, b): return 0\n",
    "def multiply(a,b): return 0\n",
    # Repaired / Completed
    "def multiply(a, b):\n    return a * b\n",
    "def multiply(a, b): return a * b\n",
    "def multiply(a,b): return a*b\n",
]

APPROVED_CALC_HASHES = {content_digest(s.encode("utf-8")) for s in CALC_CANDIDATE_SOURCES}
APPROVED_CALCULATOR_HASHES = {content_digest(s.encode("utf-8")) for s in CALCULATOR_CANDIDATE_SOURCES}


class OfflineViolation(RuntimeError):
    pass


class ProcessPolicy:
    def __init__(self, root: Path, temporary: Path):
        self.root = root.resolve()
        self.temporary = temporary.resolve()
        self.python = Path(sys.executable).resolve()
        git = shutil.which("git")
        if not git:
            raise OfflineViolation("Git is required")
        self.git = Path(git).resolve()
        self.version_shell = (Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe").resolve()
        self.denied = []
        self.head_probes = 0
        self.authorized_fixtures: dict[Path, str] = {}
        self._registration_token = object()

    def fail(self, reason):
        self.denied.append(reason)
        raise OfflineViolation(reason)

    def scope(self, path):
        path = Path(path).resolve()
        if path != self.root and not path.is_relative_to(self.temporary):
            self.fail("Process directory outside checkout or disposable fixtures")
        return path

    def process(self, command, cwd=None):
        if isinstance(command, str):
            command = [part.strip('"') for part in shlex.split(command, posix=False)]
        if not command:
            self.fail("Missing process command")
        executable = shutil.which(str(command[0])) or str(command[0])
        executable = Path(executable).resolve()
        directory = self.scope(cwd or Path.cwd())
        arguments = list(command[1:])
        if executable == self.git:
            index = 0
            while index < len(arguments) and arguments[index] in {"-c", "-C"}:
                if index + 1 >= len(arguments):
                    self.fail("Incomplete Git option")
                if arguments[index] == "-C":
                    directory = self.scope(arguments[index + 1])
                index += 2
            verb = arguments[index] if index < len(arguments) else ""
            allowed = READ_ONLY_GIT if directory == self.root else FIXTURE_GIT
            if verb not in allowed:
                self.fail("Git operation outside reviewed offline set")
            if verb == "symbolic-ref" and arguments[index:] != ["symbolic-ref", "-q", "HEAD"]:
                self.fail("Only the read-only branch identity query is allowed")
            if directory == self.root and arguments[index:] == ["rev-parse", "HEAD"]:
                self.head_probes += 1
            return
        # CPython 3.11 platform.win32_ver uses this fixed, read-only OS query.
        if executable == self.version_shell and arguments == ["/c", "ver"]:
            return
        if executable != self.python:
            self.fail("Executable outside reviewed Git/Python set")
        if (len(arguments) == 2
                and Path(arguments[0]).resolve() == ROOT / "supervision_worker.py"
                and Path(arguments[1]).resolve().is_relative_to(self.temporary)):
            return
        self.verifier([str(command[0]), *arguments], directory)

    def _validate_fixture_profile(self, directory: Path, for_verifier: bool = False) -> str | None:
        directory = Path(directory).resolve()
        if not directory.is_dir() or directory.is_symlink():
            if for_verifier:
                self.fail("Fixture path is not a valid directory")
            return None
        if not directory.is_relative_to(self.temporary) or directory == self.root:
            if for_verifier:
                self.fail("Fixture path outside disposable temporary tree")
            return None

        cur = directory
        while cur != self.temporary:
            if cur.is_symlink():
                if for_verifier:
                    self.fail("Symlink in fixture path hierarchy detected")
                return None
            cur = cur.parent

        for forbidden_name in ("unittest.py", "sitecustomize.py", "usercustomize.py"):
            if (directory / forbidden_name).exists():
                if for_verifier:
                    self.fail(f"Local runner/customization replacement file detected: {forbidden_name}")
                return None
        if any(directory.glob("*.pth")) or any(directory.glob("unittest*")):
            if for_verifier:
                self.fail("Unreviewed runner or import hook detected")
            return None

        if (directory / "__pycache__").exists():
            if for_verifier:
                self.fail("Cached bytecode directory detected in fixture")
            return None
        for ext in (".pyc", ".pyo", ".pyd", ".pyw"):
            if any(directory.glob(f"*{ext}")):
                if for_verifier:
                    self.fail(f"Compiled/extension artifact detected in fixture: {ext}")
                return None

        for item in directory.iterdir():
            if item.is_dir():
                if item.name != ".git":
                    if for_verifier:
                        self.fail(f"Unreviewed subdirectory or package shadowing detected: {item.name}")
                    return None
            elif item.is_symlink():
                if for_verifier:
                    self.fail(f"Symlink file detected in fixture: {item.name}")
                return None

        test_add = directory / "test_add.py"
        test_sub = directory / "test_sub.py"
        calc = directory / "calc.py"
        is_manager = (
            test_add.is_file() and not test_add.is_symlink()
            and test_sub.is_file() and not test_sub.is_symlink()
            and calc.is_file() and not calc.is_symlink()
            and content_digest(test_add.read_bytes()) == HASH_TEST_ADD
            and content_digest(test_sub.read_bytes()) == HASH_TEST_SUB
            and content_digest(calc.read_bytes()) in APPROVED_CALC_HASHES
            and not any(directory.glob("test_missing_module*"))
        )

        test_calc = directory / "test_calculator.py"
        calc_module = directory / "calculator.py"
        aux_files = {
            "test_helper.py": "2e9bb088845f57f573ee45ff71c512d663e94900d137665452005c8e12338bd4",
            "test_main.py": "efb23af36449030c79be9afba96574af7c96cb863638c3f5e5e04340452b4c04",
            "test_readonly.py": "eb779b7631375da5e6807ce3a31ccf53a82dd5007c1cd183978091dad0439fac",
        }
        aux_valid = all(
            (directory / name).is_file()
            and not (directory / name).is_symlink()
            and content_digest((directory / name).read_bytes()) == h
            for name, h in aux_files.items()
            if (directory / name).exists()
        )
        is_wave6 = (
            test_calc.is_file() and not test_calc.is_symlink()
            and calc_module.is_file() and not calc_module.is_symlink()
            and content_digest(test_calc.read_bytes()) in {HASH_TEST_CALC_W6, HASH_TEST_CALC_TIMEOUT}
            and content_digest(calc_module.read_bytes()) in APPROVED_CALCULATOR_HASHES
            and aux_valid
        )
        is_multi_unit = (
            test_calc.is_file() and not test_calc.is_symlink()
            and calc_module.is_file() and not calc_module.is_symlink()
            and content_digest(test_calc.read_bytes()) == HASH_TEST_CALC_MULTI_UNIT
            and content_digest(calc_module.read_bytes()) in APPROVED_MULTI_UNIT_CALCULATOR_HASHES
        )

        if is_manager and is_wave6:
            profile = "combined_calc"
            allowed_files = {".gitignore", "calc.py", "test_add.py", "test_sub.py", "calculator.py", "test_calculator.py"}
        elif is_manager:
            profile = "manager_calc"
            allowed_files = {".gitignore", "calc.py", "test_add.py", "test_sub.py"}
        elif is_wave6:
            profile = "wave6_calc"
            allowed_files = {".gitignore", "calculator.py", "test_calculator.py",
                             "test_helper.py", "test_main.py", "test_readonly.py"}
        elif is_multi_unit:
            profile = "multi_unit_calc"
            allowed_files = {".gitignore", "calculator.py", "test_calculator.py"}
        else:
            if for_verifier:
                self.fail("Fixture contents do not match any reviewed executable profile or candidate state")
            return None

        for item in directory.iterdir():
            if item.is_file() and item.name not in allowed_files:
                if for_verifier:
                    self.fail(f"Unexpected file detected in fixture: {item.name}")
                return None

        return profile

    def register_trusted_fixture(self, path: Path | str, token: object) -> Path:
        if token is not self._registration_token:
            self.fail("Unauthorized fixture registration: invalid registration token")
        directory = self.scope(path)
        profile = self._validate_fixture_profile(directory, for_verifier=False)
        if not profile:
            self.fail("Fixture does not match any reviewed trusted profile")
        self.authorized_fixtures[directory] = profile
        return directory

    def verifier(self, command, cwd):
        directory = self.scope(cwd)
        if directory == self.root:
            self.fail("Verifier execution targeting checkout root is denied")

        executable = shutil.which(str(command[0])) or str(command[0])
        executable = Path(executable).resolve()

        if (executable == self.git
                and list(command[1:]) == ["-c", f"safe.directory={directory}",
                                          "-c", "diff.external=", "diff",
                                          "--no-ext-diff", "--no-textconv", "--check"]):
            if directory not in self.authorized_fixtures:
                self.fail("Git diff verifier in unauthorized fixture is denied")
            self._validate_fixture_profile(directory, for_verifier=True)
            return

        if executable != self.python:
            self.fail("Verifier executable outside reviewed Python set")

        arguments = list(command[1:])
        if len(arguments) != 3 or arguments[:2] != ["-m", "unittest"]:
            self.fail("Only exact reviewed unittest verifier invocations are allowed")

        target = arguments[2]

        if directory not in self.authorized_fixtures:
            self.fail("Verifier execution in unauthorized fixture is denied")

        expected_profile = self.authorized_fixtures[directory]

        if expected_profile == "manager_calc":
            if target not in {"test_add", "test_sub", "test_missing_module"}:
                self.fail(f"Module {target} not allowed in manager_calc fixture")
        elif expected_profile == "wave6_calc":
            if target != "test_calculator.py":
                self.fail(f"Module {target} not allowed in wave6_calc fixture")
        elif expected_profile == "combined_calc":
            if target not in {"test_add", "test_sub", "test_calculator.py", "test_missing_module"}:
                self.fail(f"Module {target} not allowed in combined_calc fixture")
        elif expected_profile == "multi_unit_calc":
            if target not in APPROVED_MULTI_UNIT_TARGETS:
                self.fail(f"Target {target} not allowed in multi_unit_calc fixture")
        else:
            self.fail("Unrecognized fixture profile")

        current_profile = self._validate_fixture_profile(directory, for_verifier=True)
        if not current_profile or current_profile != expected_profile:
            self.fail("Fixture integrity compromised or profile mismatch at verifier launch")

        if target == "test_missing_module":
            if any(directory.glob("test_missing_module*")):
                self.fail("Attacker-supplied module matching expected missing name is prohibited")
            return

        if expected_profile == "multi_unit_calc":
            target_file = directory / "test_calculator.py"
        else:
            target_file = directory / ("test_calculator.py" if target == "test_calculator.py" else f"{target}.py")
        if not target_file.exists():
            self.fail(f"Approved test file missing from fixture: {target_file}")
        if not target_file.is_file() or target_file.is_symlink():
            self.fail(f"Approved test file must be a regular non-symlink file: {target_file}")

        digest = content_digest(target_file.read_bytes())
        expected_hashes = {HASH_TEST_CALC_MULTI_UNIT} if expected_profile == "multi_unit_calc" else APPROVED_MODULE_HASHES.get(target, set())
        if digest not in expected_hashes:
            self.fail(f"Test file content changed or does not match approved hash for {target}")

    def audit(self, event, args):
        # platform.uname reads the local hostname; this event performs no network I/O.
        if event.startswith("socket.") and event != "socket.gethostname":
            self.fail("Socket operation prohibited")
        if event in {"os.system", "os.startfile", "os.posix_spawn", "os.posix_spawnp"}:
            self.fail("Alternative process launch prohibited")
        if event == "subprocess.Popen":
            self.process(args[1], args[2])

    def gateway(self, *args, **kwargs):
        self.fail("Unmocked Gateway operation prohibited")


def make_trusted_wave6_fixture(policy: ProcessPolicy, orig_fixture, token: object):
    def trusted_wave6_fixture(root, *args, **kwargs):
        expected_repo = Path(root) / "repo"
        if os.path.lexists(expected_repo) or expected_repo.is_symlink() or expected_repo.exists():
            policy.fail(f"Adoption of pre-existing repository path is prohibited: {expected_repo}")
        res = orig_fixture(root, *args, **kwargs)
        repo = res[0]
        if not hasattr(repo, "root") or Path(repo.root).resolve() != expected_repo.resolve():
            policy.fail(f"Constructed repository path does not match expected {expected_repo}")
        policy.register_trusted_fixture(repo.root, token)
        return res
    return trusted_wave6_fixture


def make_trusted_wave2_fixture(policy: ProcessPolicy, orig_fixture, token: object):
    def trusted_wave2_fixture(root, *args, **kwargs):
        expected_repo = Path(root) / "repo"
        if os.path.lexists(expected_repo) or expected_repo.is_symlink() or expected_repo.exists():
            policy.fail(f"Adoption of pre-existing repository path is prohibited: {expected_repo}")
        res = orig_fixture(root, *args, **kwargs)
        repo = res[0]
        if not hasattr(repo, "root") or Path(repo.root).resolve() != expected_repo.resolve():
            policy.fail(f"Constructed repository path does not match expected {expected_repo}")
        policy.register_trusted_fixture(repo.root, token)
        return res
    return trusted_wave2_fixture


def make_trusted_multi_unit_fixture(policy: ProcessPolicy, orig_fixture, token: object):
    def trusted_multi_unit_fixture(root, *args, **kwargs):
        expected_repo = Path(root) / "repo"
        if os.path.lexists(expected_repo) or expected_repo.is_symlink() or expected_repo.exists():
            policy.fail(f"Adoption of pre-existing repository path is prohibited: {expected_repo}")
        res = orig_fixture(root, *args, **kwargs)
        repo = res[0]
        if not hasattr(repo, "root") or Path(repo.root).resolve() != expected_repo.resolve():
            policy.fail(f"Constructed repository path does not match expected {expected_repo}")
        policy.register_trusted_fixture(repo.root, token)
        return res
    return trusted_multi_unit_fixture


def make_trusted_manager_fixture(policy: ProcessPolicy, orig_fixture, token: object):
    def trusted_manager_fixture(root, *args, **kwargs):
        expected_repo = Path(root) / "repo"
        # Note: test_manager.git_fixture calls path.mkdir() without exist_ok,
        # which raises FileExistsError if path already exists.
        path, git = orig_fixture(root, *args, **kwargs)
        if Path(path).resolve() != expected_repo.resolve():
            policy.fail(f"Constructed repository path {path} does not match expected {expected_repo}")
        policy.register_trusted_fixture(path, token)
        return path, git
    return trusted_manager_fixture


def syntax_check():
    files = sorted(ROOT.rglob("*.py"))
    for path in files:
        if ".git" not in path.relative_to(ROOT).parts:
            ast.parse(path.read_bytes(), filename=str(path.relative_to(ROOT)))
    print(f"AST: {len(files)} Python files parsed", flush=True)


def main():
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.dont_write_bytecode = True
    os.environ.pop("PYTHONPATH", None)
    # Prevent workstation Git hooks/config/credentials from entering fixture tests.
    os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]
    os.environ["GIT_CONFIG_GLOBAL"] = os.devnull
    os.environ["GIT_CONFIG_SYSTEM"] = os.devnull
    os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
    os.environ["GIT_ATTR_NOSYSTEM"] = "1"
    os.environ["GIT_TERMINAL_PROMPT"] = "0"
    os.environ["COMSPEC"] = str(Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe")
    os.environ["GIT_CONFIG_COUNT"] = "3"
    for index, (key, value) in enumerate([
        ("core.hooksPath", os.devnull),
        ("commit.gpgSign", "false"),
        ("protocol.allow", "never"),
    ]):
        os.environ[f"GIT_CONFIG_KEY_{index}"] = key
        os.environ[f"GIT_CONFIG_VALUE_{index}"] = value
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "tests"))
    syntax_check()
    with tempfile.TemporaryDirectory(prefix="triglav-ci-") as temporary:
        policy = ProcessPolicy(ROOT, Path(temporary))
        sys.addaudithook(policy.audit)
        import harness
        import durable
        import supervision

        original_verifier = supervision.run_command

        def guarded_verifier(supervisor, command, cwd, *args, **kwargs):
            policy.verifier(command, cwd)
            return original_verifier(supervisor, command, cwd, *args, **kwargs)

        token = policy._registration_token

        import test_manager
        import test_reviewer_wave6
        import test_work_units_wave2
        import test_safe_qwen_multi_unit_prod

        orig_mgr_fixture = test_manager.git_fixture
        trusted_git_fixture = make_trusted_manager_fixture(policy, orig_mgr_fixture, token)

        orig_wave6_fixture = test_reviewer_wave6.setup_test_fixture
        trusted_wave6_fixture = make_trusted_wave6_fixture(policy, orig_wave6_fixture, token)

        orig_wave2_fixture = test_work_units_wave2.fixture
        trusted_wave2_fixture = make_trusted_wave2_fixture(policy, orig_wave2_fixture, token)

        orig_multi_unit_fixture = test_safe_qwen_multi_unit_prod.setup_multi_unit_fixture
        trusted_multi_unit_fixture = make_trusted_multi_unit_fixture(policy, orig_multi_unit_fixture, token)

        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(tempfile, "tempdir", temporary))
            for name in ("request", "switch", "unload", "running"):
                stack.enter_context(patch.object(harness.Gateway, name, policy.gateway))
            stack.enter_context(patch.object(supervision, "run_command", guarded_verifier))
            stack.enter_context(patch.object(durable, "run_command", guarded_verifier))
            stack.enter_context(patch.object(test_manager, "git_fixture", trusted_git_fixture))
            stack.enter_context(patch.object(test_reviewer_wave6, "setup_test_fixture", trusted_wave6_fixture))
            if "test_safe_qwen_integration" in sys.modules:
                stack.enter_context(patch.object(sys.modules["test_safe_qwen_integration"], "setup_test_fixture", trusted_wave6_fixture))
            stack.enter_context(patch.object(test_work_units_wave2, "fixture", trusted_wave2_fixture))
            stack.enter_context(patch.object(test_safe_qwen_multi_unit_prod, "setup_multi_unit_fixture", trusted_multi_unit_fixture))
            head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip()
            if len(head) != 40:
                raise OfflineViolation("A real committed checkout is required")
            print(f"Controller HEAD: {head} (real Git; no identity shim)", flush=True)
            for module, expected in MODULES.items():
                suite = unittest.defaultTestLoader.loadTestsFromName(module)
                if suite.countTestCases() != expected:
                    raise OfflineViolation(f"{module}: test count changed; review CI scope")
                result = unittest.TextTestRunner(verbosity=2).run(suite)
                if not result.wasSuccessful() or result.skipped:
                    return 1
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    harness.main(["--help"])
                except SystemExit as exc:
                    if exc.code != 0:
                        raise
            if "usage:" not in out.getvalue() or err.getvalue():
                raise OfflineViolation("Unexpected CLI help output")
            if policy.denied:
                raise OfflineViolation("A prohibited operation was attempted")
            print(f"PASS: {sum(MODULES.values())} reviewed offline cases; CLI help; "
                  f"{policy.head_probes} real HEAD probes; model calls 0", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
