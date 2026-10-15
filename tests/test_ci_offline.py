"""Exercise the offline CI boundary without launching prohibited operations."""
from pathlib import Path
import sys
import tempfile
import unittest

from scripts.ci_offline import (
    OfflineViolation,
    ProcessPolicy,
    ROOT,
    TEST_ADD_SOURCE,
    TEST_SUB_SOURCE,
    TEST_CALCULATOR_W6_SOURCE,
    make_trusted_wave6_fixture,
    make_trusted_wave2_fixture,
    make_trusted_manager_fixture,
)


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fixture = Path(self.temp.name)
        (self.fixture / ".git").mkdir()
        (self.fixture / "calc.py").write_text("def add(a, b): return 0\ndef sub(a, b): return 0\n", encoding="utf-8")
        (self.fixture / "test_add.py").write_text(TEST_ADD_SOURCE, encoding="utf-8")
        (self.fixture / "test_sub.py").write_text(TEST_SUB_SOURCE, encoding="utf-8")
        (self.fixture / "calculator.py").write_text("def multiply(a, b): return 0\n", encoding="utf-8")
        (self.fixture / "test_calculator.py").write_text(TEST_CALCULATOR_W6_SOURCE, encoding="utf-8")
        self.policy = ProcessPolicy(ROOT, self.fixture)
        self.policy.register_trusted_fixture(self.fixture, self.policy._registration_token)

    def denied(self, function, *args):
        with self.assertRaises(OfflineViolation):
            function(*args)

    def test_network_events_fail_closed(self):
        for event in ("socket.connect", "socket.bind", "socket.getaddrinfo", "socket.__new__"):
            self.denied(self.policy.audit, event, ())

        self.policy.audit("socket.gethostname", ())

    def test_gateway_is_denied(self):
        self.denied(self.policy.gateway)

    def test_shell_launch_is_denied(self):
        self.denied(self.policy.audit, "os.system", ("echo unsafe",))
        shell = str(self.policy.version_shell)
        self.policy.process([shell, "/c", "ver"], ROOT)
        self.denied(self.policy.process, [shell, "/c", "echo unsafe"], ROOT)

    def test_unknown_executable_is_denied(self):
        self.denied(self.policy.process, ["llama-server.exe"], self.fixture)

    def test_python_inline_code_is_denied(self):
        self.denied(self.policy.process, [sys.executable, "-c", "print(1)"], self.fixture)

    def test_external_directory_is_denied(self):
        self.denied(self.policy.process, [str(self.policy.git), "status"], self.fixture.parent)

    def test_git_network_and_checkout_writes_are_denied(self):
        for verb in ("fetch", "push", "clone"):
            self.denied(self.policy.process, [str(self.policy.git), verb], self.fixture)
        self.denied(self.policy.process, [str(self.policy.git), "commit"], ROOT)

    def test_fixture_git_and_real_checkout_head_are_allowed(self):
        self.policy.process([str(self.policy.git), "-C", str(self.fixture), "commit"])
        self.policy.process([str(self.policy.git), "rev-parse", "HEAD"], ROOT)
        self.assertEqual(self.policy.head_probes, 1)
        self.policy.process([str(self.policy.git), "symbolic-ref", "-q", "HEAD"], ROOT)
        self.denied(self.policy.process, [str(self.policy.git), "symbolic-ref", "HEAD", "refs/heads/other"], ROOT)

    def test_reviewed_verifier_and_broker_are_allowed(self):
        self.policy.verifier([sys.executable, "-m", "unittest", "test_add"], self.fixture)
        self.policy.verifier([sys.executable, "-m", "unittest", "test_sub"], self.fixture)
        self.policy.verifier([sys.executable, "-m", "unittest", "test_calculator.py"], self.fixture)
        self.policy.verifier([sys.executable, "-m", "unittest", "test_missing_module"], self.fixture)
        self.policy.verifier([str(self.policy.git), "-c", f"safe.directory={self.fixture.resolve()}",
                              "-c", "diff.external=", "diff", "--no-ext-diff",
                              "--no-textconv", "--check"], self.fixture)
        self.policy.process([sys.executable, str(ROOT / "supervision_worker.py"),
                             str(self.fixture / "broker")], ROOT)

    def test_unreviewed_verifier_is_denied(self):
        self.denied(self.policy.verifier, [sys.executable, "-m", "unittest", "test_unreviewed"], self.fixture)
        self.denied(self.policy.verifier, [sys.executable, "-m", "unittest", "test_mul"], self.fixture)

    def test_unittest_discovery_is_denied(self):
        self.denied(self.policy.verifier, [sys.executable, "-m", "unittest"], self.fixture)
        self.denied(self.policy.verifier, [sys.executable, "-m", "unittest", "discover"], self.fixture)
        self.denied(self.policy.verifier, [sys.executable, "-m", "unittest", "discover", "-s", "tests"], self.fixture)

    def test_arbitrary_test_sub_and_fixture_variants_are_denied(self):
        self.denied(self.policy.verifier, [sys.executable, "-m", "unittest", "test_sub.py"], self.fixture)
        self.denied(self.policy.verifier, [sys.executable, "-m", "unittest", "test_sub", "-v"], self.fixture)
        self.denied(self.policy.verifier, [sys.executable, "-m", "unittest", "test_sub_extra"], self.fixture)
        self.denied(self.policy.verifier, [sys.executable, "-m", "pytest", "test_sub"], self.fixture)

    def test_verifier_outside_disposable_fixture_is_denied(self):
        self.denied(self.policy.verifier, [sys.executable, "-m", "unittest", "test_add"], ROOT)
        self.denied(self.policy.verifier, [sys.executable, "-m", "unittest", "test_sub"], ROOT)
        self.denied(self.policy.verifier, [sys.executable, "-m", "unittest", "test_add"], self.fixture.parent)

    def test_unregistered_process_launch_is_denied(self):
        self.denied(self.policy.process, [sys.executable, str(self.fixture / "arbitrary.py")], self.fixture)
        self.denied(self.policy.process, [sys.executable, "-m", "http.server"], self.fixture)

    def test_unrelated_fixture_with_malicious_sub_is_denied(self):
        # 10.A: Unrelated temporary fixture with malicious test_sub.py
        unrelated = self.fixture / "unrelated_a"
        unrelated.mkdir()
        malicious = (
            "import unittest\n"
            "class MaliciousSub(unittest.TestCase):\n"
            "    def test_sub(self):\n"
            "        self.attack_marker = True\n"
        )
        (unrelated / "test_sub.py").write_text(malicious, encoding="utf-8")
        self.denied(self.policy.verifier, [sys.executable, "-m", "unittest", "test_sub"], unrelated)

    def test_unrelated_fixture_with_malicious_missing_module_is_denied(self):
        # 10.B: Unrelated temporary fixture with malicious test_missing_module.py
        unrelated = self.fixture / "unrelated_b"
        unrelated.mkdir()
        malicious = (
            "import unittest\n"
            "class MaliciousMissing(unittest.TestCase):\n"
            "    def test_exploit(self):\n"
            "        self.attack_marker = True\n"
        )
        (unrelated / "test_missing_module.py").write_text(malicious, encoding="utf-8")
        self.denied(self.policy.verifier, [sys.executable, "-m", "unittest", "test_missing_module"], unrelated)

    def test_previously_authorized_fixture_with_changed_content_is_denied(self):
        # 10.C: Previously authorized fixture with changed executable module content
        fixture_c = self.fixture / "fixture_c"
        fixture_c.mkdir()
        (fixture_c / ".git").mkdir()
        (fixture_c / "calc.py").write_text("def add(a, b): return 0\ndef sub(a, b): return 0\n", encoding="utf-8")
        (fixture_c / "test_add.py").write_text(TEST_ADD_SOURCE, encoding="utf-8")
        (fixture_c / "test_sub.py").write_text(TEST_SUB_SOURCE, encoding="utf-8")
        self.policy.register_trusted_fixture(fixture_c, self.policy._registration_token)
        self.policy.verifier([sys.executable, "-m", "unittest", "test_sub"], fixture_c)
        tampered = (
            "import unittest\n"
            "class TamperedSub(unittest.TestCase):\n"
            "    def test_sub(self):\n"
            "        pass\n"
        )
        (fixture_c / "test_sub.py").write_text(tampered, encoding="utf-8")
        self.denied(self.policy.verifier, [sys.executable, "-m", "unittest", "test_sub"], fixture_c)

    def test_unapproved_module_resolution_is_denied(self):
        # 10.D: Approved name and arguments but unapproved module resolution
        fixture_d = self.fixture / "fixture_d"
        fixture_d.mkdir()
        (fixture_d / ".git").mkdir()
        (fixture_d / "calc.py").write_text("def add(a, b): return 0\ndef sub(a, b): return 0\n", encoding="utf-8")
        (fixture_d / "test_add.py").write_text(TEST_ADD_SOURCE, encoding="utf-8")
        (fixture_d / "test_sub.py").write_text(TEST_SUB_SOURCE, encoding="utf-8")
        self.policy.register_trusted_fixture(fixture_d, self.policy._registration_token)

        # 1. Package directory shadowing module
        pkg = fixture_d / "test_sub"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("# shadow\n", encoding="utf-8")
        self.denied(self.policy.verifier, [sys.executable, "-m", "unittest", "test_sub"], fixture_d)
        (pkg / "__init__.py").unlink()
        pkg.rmdir()

        # 2. Subdirectory execution attempting parent resolution
        sub_dir = fixture_d / "nested"
        sub_dir.mkdir()
        self.denied(self.policy.verifier, [sys.executable, "-m", "unittest", "test_sub"], sub_dir)

        # 3. Compiled/extension artifact shadowing
        pyc = fixture_d / "test_sub.pyc"
        pyc.write_bytes(b"fake bytecode")
        self.denied(self.policy.verifier, [sys.executable, "-m", "unittest", "test_sub"], fixture_d)
        pyc.unlink()

        # 4. Attacker supplying missing module in authorized fixture
        (fixture_d / "test_missing_module.py").write_text("import unittest\n", encoding="utf-8")
        self.denied(self.policy.verifier, [sys.executable, "-m", "unittest", "test_missing_module"], fixture_d)

    def test_legitimate_manager_fixtures_are_authorized_and_verified(self):
        # 10.E: The actual legitimate Manager fixtures still run successfully
        legit = self.fixture / "legit_manager"
        legit.mkdir()
        (legit / ".git").mkdir()
        (legit / "calc.py").write_text("def add(a, b): return 0\ndef sub(a, b): return 0\n", encoding="utf-8")
        (legit / "test_add.py").write_text(TEST_ADD_SOURCE, encoding="utf-8")
        (legit / "test_sub.py").write_text(TEST_SUB_SOURCE, encoding="utf-8")
        self.policy.register_trusted_fixture(legit, self.policy._registration_token)
        self.policy.verifier([sys.executable, "-m", "unittest", "test_add"], legit)
        self.policy.verifier([sys.executable, "-m", "unittest", "test_sub"], legit)
        self.policy.verifier([sys.executable, "-m", "unittest", "test_missing_module"], legit)
        self.policy.verifier([str(self.policy.git), "-c", f"safe.directory={legit.resolve()}",
                              "-c", "diff.external=", "diff", "--no-ext-diff",
                              "--no-textconv", "--check"], legit)

    def test_unapproved_candidate_calc_mutation_is_denied(self):
        # P1-A / P1-B: Arbitrary dependency mutation after authorization
        fixture_mut = self.fixture / "fixture_mut"
        fixture_mut.mkdir()
        (fixture_mut / ".git").mkdir()
        (fixture_mut / "calc.py").write_text("def add(a, b): return 0\ndef sub(a, b): return 0\n", encoding="utf-8")
        (fixture_mut / "test_add.py").write_text(TEST_ADD_SOURCE, encoding="utf-8")
        (fixture_mut / "test_sub.py").write_text(TEST_SUB_SOURCE, encoding="utf-8")
        self.policy.register_trusted_fixture(fixture_mut, self.policy._registration_token)
        self.policy.verifier([sys.executable, "-m", "unittest", "test_add"], fixture_mut)
        (fixture_mut / "calc.py").write_text("def add(a, b):\n    import os; os.system('echo pwn')\n", encoding="utf-8")
        self.denied(self.policy.verifier, [sys.executable, "-m", "unittest", "test_add"], fixture_mut)

    def test_local_unittest_runner_replacement_is_denied(self):
        # P1-B: Local unittest.py runner replacement
        fixture_runner = self.fixture / "fixture_runner"
        fixture_runner.mkdir()
        (fixture_runner / ".git").mkdir()
        (fixture_runner / "calc.py").write_text("def add(a, b): return 0\ndef sub(a, b): return 0\n", encoding="utf-8")
        (fixture_runner / "test_add.py").write_text(TEST_ADD_SOURCE, encoding="utf-8")
        (fixture_runner / "test_sub.py").write_text(TEST_SUB_SOURCE, encoding="utf-8")
        self.policy.register_trusted_fixture(fixture_runner, self.policy._registration_token)
        (fixture_runner / "unittest.py").write_text("# fake unittest runner\n", encoding="utf-8")
        self.denied(self.policy.verifier, [sys.executable, "-m", "unittest", "test_add"], fixture_runner)

    def test_dependency_package_shadowing_is_denied(self):
        # P1-B: Dependency package shadowing (calc/__init__.py)
        fixture_pkg = self.fixture / "fixture_pkg"
        fixture_pkg.mkdir()
        (fixture_pkg / ".git").mkdir()
        (fixture_pkg / "calc.py").write_text("def add(a, b): return 0\ndef sub(a, b): return 0\n", encoding="utf-8")
        (fixture_pkg / "test_add.py").write_text(TEST_ADD_SOURCE, encoding="utf-8")
        (fixture_pkg / "test_sub.py").write_text(TEST_SUB_SOURCE, encoding="utf-8")
        self.policy.register_trusted_fixture(fixture_pkg, self.policy._registration_token)
        pkg = fixture_pkg / "calc"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("# shadow calc\n", encoding="utf-8")
        self.denied(self.policy.verifier, [sys.executable, "-m", "unittest", "test_add"], fixture_pkg)

    def test_test_calculator_package_shadowing_is_denied(self):
        # P1-B: test_calculator package shadowing
        fixture_calc = self.fixture / "fixture_calc"
        fixture_calc.mkdir()
        (fixture_calc / ".git").mkdir()
        (fixture_calc / "calculator.py").write_text("def multiply(a, b): return 0\n", encoding="utf-8")
        (fixture_calc / "test_calculator.py").write_text(TEST_CALCULATOR_W6_SOURCE, encoding="utf-8")
        self.policy.register_trusted_fixture(fixture_calc, self.policy._registration_token)
        pkg = fixture_calc / "test_calculator"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("# shadow test_calculator\n", encoding="utf-8")
        self.denied(self.policy.verifier, [sys.executable, "-m", "unittest", "test_calculator.py"], fixture_calc)

    def test_unchecked_bytecode_execution_is_denied(self):
        # P1-B: Unchecked __pycache__ bytecode execution
        fixture_cache = self.fixture / "fixture_cache"
        fixture_cache.mkdir()
        (fixture_cache / ".git").mkdir()
        (fixture_cache / "calc.py").write_text("def add(a, b): return 0\ndef sub(a, b): return 0\n", encoding="utf-8")
        (fixture_cache / "test_add.py").write_text(TEST_ADD_SOURCE, encoding="utf-8")
        (fixture_cache / "test_sub.py").write_text(TEST_SUB_SOURCE, encoding="utf-8")
        self.policy.register_trusted_fixture(fixture_cache, self.policy._registration_token)
        pycache = fixture_cache / "__pycache__"
        pycache.mkdir()
        (pycache / "calc.cpython-312.pyc").write_bytes(b"fake bytecode")
        self.denied(self.policy.verifier, [sys.executable, "-m", "unittest", "test_add"], fixture_cache)

    def test_unexpected_file_in_fixture_is_denied(self):
        # P1-B: Unexpected executable or data files in fixture directory
        fixture_extra = self.fixture / "fixture_extra"
        fixture_extra.mkdir()
        (fixture_extra / ".git").mkdir()
        (fixture_extra / "calc.py").write_text("def add(a, b): return 0\ndef sub(a, b): return 0\n", encoding="utf-8")
        (fixture_extra / "test_add.py").write_text(TEST_ADD_SOURCE, encoding="utf-8")
        (fixture_extra / "test_sub.py").write_text(TEST_SUB_SOURCE, encoding="utf-8")
        self.policy.register_trusted_fixture(fixture_extra, self.policy._registration_token)
        (fixture_extra / "extra_payload.py").write_text("print('extra')\n", encoding="utf-8")
        self.denied(self.policy.verifier, [sys.executable, "-m", "unittest", "test_add"], fixture_extra)

    def test_copied_approved_profile_without_trusted_registration_is_denied(self):
        # Ownership contract: a complete copied approved fixture profile
        unrelated = self.fixture / "unrelated_copied"
        unrelated.mkdir()
        (unrelated / ".git").mkdir()
        (unrelated / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
        (unrelated / "calc.py").write_text("def add(a, b): return 0\ndef sub(a, b): return 0\n", encoding="utf-8")
        (unrelated / "test_add.py").write_text(TEST_ADD_SOURCE, encoding="utf-8")
        (unrelated / "test_sub.py").write_text(TEST_SUB_SOURCE, encoding="utf-8")
        self.denied(self.policy.verifier, [sys.executable, "-m", "unittest", "test_add"], unrelated)

    def test_fake_git_repository_metadata_without_token_is_denied(self):
        # Ownership contract: fake Git repository metadata and self-registration attempt
        fake_git = self.fixture / "fake_git_meta"
        fake_git.mkdir()
        (fake_git / ".git").mkdir()
        (fake_git / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
        (fake_git / "calc.py").write_text("def add(a, b): return 0\ndef sub(a, b): return 0\n", encoding="utf-8")
        (fake_git / "test_add.py").write_text(TEST_ADD_SOURCE, encoding="utf-8")
        (fake_git / "test_sub.py").write_text(TEST_SUB_SOURCE, encoding="utf-8")
        self.denied(self.policy.register_trusted_fixture, fake_git, "untrusted_token")
        self.denied(self.policy.verifier, [sys.executable, "-m", "unittest", "test_add"], fake_git)

    def test_unrelated_repository_initialization_does_not_grant_registration(self):
        # Ownership contract: unrelated Repository initialization
        unrelated_repo = self.fixture / "unrelated_repo_init"
        unrelated_repo.mkdir()
        (unrelated_repo / ".git").mkdir()
        (unrelated_repo / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
        (unrelated_repo / "calculator.py").write_text("def multiply(a, b): return 0\n", encoding="utf-8")
        (unrelated_repo / "test_calculator.py").write_text(TEST_CALCULATOR_W6_SOURCE, encoding="utf-8")
        self.assertNotIn(unrelated_repo, self.policy.authorized_fixtures)
        self.denied(self.policy.verifier, [sys.executable, "-m", "unittest", "test_calculator.py"], unrelated_repo)

    def test_valid_trusted_fixture_construction_is_authorized(self):
        # Ownership contract: valid trusted fixture construction via capability token
        valid_dir = self.fixture / "valid_trusted"
        valid_dir.mkdir()
        (valid_dir / ".git").mkdir()
        (valid_dir / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
        (valid_dir / "calc.py").write_text("def add(a, b): return 0\ndef sub(a, b): return 0\n", encoding="utf-8")
        (valid_dir / "test_add.py").write_text(TEST_ADD_SOURCE, encoding="utf-8")
        (valid_dir / "test_sub.py").write_text(TEST_SUB_SOURCE, encoding="utf-8")
        self.policy.register_trusted_fixture(valid_dir, self.policy._registration_token)
        self.assertIn(valid_dir, self.policy.authorized_fixtures)
        self.policy.verifier([sys.executable, "-m", "unittest", "test_add"], valid_dir)

    def test_content_mutation_after_registration_is_denied(self):
        # Ownership contract: content mutation after registration
        mut_dir = self.fixture / "valid_then_mutated"
        mut_dir.mkdir()
        (mut_dir / ".git").mkdir()
        (mut_dir / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
        (mut_dir / "calc.py").write_text("def add(a, b): return 0\ndef sub(a, b): return 0\n", encoding="utf-8")
        (mut_dir / "test_add.py").write_text(TEST_ADD_SOURCE, encoding="utf-8")
        (mut_dir / "test_sub.py").write_text(TEST_SUB_SOURCE, encoding="utf-8")
        self.policy.register_trusted_fixture(mut_dir, self.policy._registration_token)
        (mut_dir / "calc.py").write_text("def add(a, b):\n    import os; os.system('pwn')\n", encoding="utf-8")
        self.denied(self.policy.verifier, [sys.executable, "-m", "unittest", "test_add"], mut_dir)

    def test_wave6_rejects_existing_repository_before_helper_execution(self):
        # 11A: Wave 6 rejects an existing complete copied approved repository before helper execution
        root = self.fixture / "wave6_preexisting"
        root.mkdir()
        repo_dir = root / "repo"
        repo_dir.mkdir()
        (repo_dir / ".git").mkdir()
        (repo_dir / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
        (repo_dir / "calculator.py").write_text("def multiply(a, b):\n    return 0\n", encoding="utf-8")
        (repo_dir / "test_calculator.py").write_text(TEST_CALCULATOR_W6_SOURCE, encoding="utf-8")

        helper_called = False

        def fake_wave6_helper(r, *args, **kwargs):
            nonlocal helper_called
            helper_called = True
            class FakeRepo:
                root = r / "repo"
            return FakeRepo(), None, None

        wrapper = make_trusted_wave6_fixture(self.policy, fake_wave6_helper, self.policy._registration_token)
        self.denied(wrapper, root)
        self.assertFalse(helper_called, "Helper must not be executed when repository pre-exists")
        self.assertNotIn(repo_dir, self.policy.authorized_fixtures)

    def test_wave2_rejects_existing_repository_before_helper_execution(self):
        # 11B: Wave 2 rejects an existing complete copied approved repository before helper execution
        root = self.fixture / "wave2_preexisting"
        root.mkdir()
        repo_dir = root / "repo"
        repo_dir.mkdir()
        (repo_dir / ".git").mkdir()
        (repo_dir / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
        (repo_dir / "calculator.py").write_text("def multiply(a, b):\n    return 0\n", encoding="utf-8")
        (repo_dir / "test_calculator.py").write_text(TEST_CALCULATOR_W6_SOURCE, encoding="utf-8")

        helper_called = False

        def fake_wave2_helper(r, *args, **kwargs):
            nonlocal helper_called
            helper_called = True
            class FakeRepo:
                root = r / "repo"
            return FakeRepo(), None, None

        wrapper = make_trusted_wave2_fixture(self.policy, fake_wave2_helper, self.policy._registration_token)
        self.denied(wrapper, root)
        self.assertFalse(helper_called, "Helper must not be executed when repository pre-exists")
        self.assertNotIn(repo_dir, self.policy.authorized_fixtures)

    def test_wave6_and_wave2_accept_fresh_fixture_and_register_path(self):
        # 11C: Both wrappers accept a fresh legitimate fixture and register the correct path
        root6 = self.fixture / "fresh_wave6"
        root6.mkdir()
        def legitimate_wave6_helper(r, *args, **kwargs):
            repo_path = r / "repo"
            repo_path.mkdir()
            (repo_path / ".git").mkdir()
            (repo_path / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
            (repo_path / "calculator.py").write_text("def multiply(a, b):\n    return 0\n", encoding="utf-8")
            (repo_path / "test_calculator.py").write_text(TEST_CALCULATOR_W6_SOURCE, encoding="utf-8")
            class FakeRepo:
                root = repo_path
            return FakeRepo(), None, None

        wrapper6 = make_trusted_wave6_fixture(self.policy, legitimate_wave6_helper, self.policy._registration_token)
        repo6, _, _ = wrapper6(root6)
        self.assertIn(repo6.root, self.policy.authorized_fixtures)
        self.assertEqual(self.policy.authorized_fixtures[repo6.root], "wave6_calc")

        root2 = self.fixture / "fresh_wave2"
        root2.mkdir()
        def legitimate_wave2_helper(r, *args, **kwargs):
            repo_path = r / "repo"
            repo_path.mkdir()
            (repo_path / ".git").mkdir()
            (repo_path / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
            (repo_path / "calculator.py").write_text("def multiply(a, b):\n    return 0\n", encoding="utf-8")
            (repo_path / "test_calculator.py").write_text(TEST_CALCULATOR_W6_SOURCE, encoding="utf-8")
            class FakeRepo:
                root = repo_path
            return FakeRepo(), None, None

        wrapper2 = make_trusted_wave2_fixture(self.policy, legitimate_wave2_helper, self.policy._registration_token)
        repo2, _, _ = wrapper2(root2)
        self.assertIn(repo2.root, self.policy.authorized_fixtures)
        self.assertEqual(self.policy.authorized_fixtures[repo2.root], "wave6_calc")

    def test_failed_construction_never_registers_fixture(self):
        # 11D: Failed construction never registers a fixture
        root = self.fixture / "failed_construction"
        root.mkdir()
        def failing_helper(r, *args, **kwargs):
            repo_path = r / "repo"
            repo_path.mkdir()
            (repo_path / ".git").mkdir()
            (repo_path / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
            (repo_path / "calculator.py").write_text("def multiply(a, b):\n    return 0\n", encoding="utf-8")
            (repo_path / "test_calculator.py").write_text(TEST_CALCULATOR_W6_SOURCE, encoding="utf-8")
            raise RuntimeError("Simulated construction failure")

        wrapper = make_trusted_wave6_fixture(self.policy, failing_helper, self.policy._registration_token)
        with self.assertRaises(RuntimeError):
            wrapper(root)
        self.assertNotIn(root / "repo", self.policy.authorized_fixtures)

    def test_existing_fixture_content_unchanged_when_adoption_refused(self):
        # 11E: Existing fixture content remains unchanged when adoption is refused
        root = self.fixture / "existing_canary"
        root.mkdir()
        repo_dir = root / "repo"
        repo_dir.mkdir()
        canary = repo_dir / "canary.txt"
        canary.write_text("pristine_canary_bytes", encoding="utf-8")
        calc = repo_dir / "calculator.py"
        calc.write_text("# pristine calc", encoding="utf-8")

        def mutating_helper(r, *args, **kwargs):
            (r / "repo" / "canary.txt").write_text("MUTATED", encoding="utf-8")
            (r / "repo" / "calculator.py").write_text("# MUTATED", encoding="utf-8")
            class FakeRepo:
                root = r / "repo"
            return FakeRepo(), None, None

        wrapper = make_trusted_wave6_fixture(self.policy, mutating_helper, self.policy._registration_token)
        self.denied(wrapper, root)
        self.assertEqual(canary.read_text(encoding="utf-8"), "pristine_canary_bytes")
        self.assertEqual(calc.read_text(encoding="utf-8"), "# pristine calc")
        self.assertNotIn(repo_dir, self.policy.authorized_fixtures)
