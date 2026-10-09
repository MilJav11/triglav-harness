"""Run only reviewed offline CI checks; this is not a hostile-code sandbox."""
from __future__ import annotations

import ast
import contextlib
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
MODULES = {"test_milestone_timeout": 10, "test_ci_offline": 10, "test_executor_protocol": 8, "test_planner_protocol": 14, "test_safe_qwen_worker": 39, "test_safe_qwen_integration": 40,
           **{f"test_reviewer_wave6.TestReviewerWave6.{name}": 1 for name in (
               "test_42_final_verification_runs_after_approve",
               "test_43_old_verifier_pass_cannot_substitute_final_verification",
               "test_44_final_verification_failure_no_checkpoint",
               "test_45_scope_failure_during_final_verification_no_checkpoint",
               "test_46_candidate_changed_after_approve_no_checkpoint",
               "test_47_environment_changed_after_approve_no_checkpoint",
               "test_49_controller_creates_trusted_checkpoint_only_after_all_steps_pass",
               "test_50_trusted_checkpoint_updates_last_verified_checkpoint_and_verified_progress")}}
READ_ONLY_GIT = {"rev-parse", "status", "ls-files", "diff", "show", "log", "ls-tree", "cat-file", "symbolic-ref"}
FIXTURE_GIT = READ_ONLY_GIT | {"init", "add", "commit", "hash-object", "write-tree"}


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

    def verifier(self, command, cwd):
        executable = shutil.which(str(command[0])) or str(command[0])
        if (Path(executable).resolve() == self.git
                and list(command[1:]) == ["-c", f"safe.directory={Path(cwd).resolve()}",
                                          "-c", "diff.external=", "diff",
                                          "--no-ext-diff", "--no-textconv", "--check"]
                and Path(cwd).resolve().is_relative_to(self.temporary)):
            return
        if (Path(executable).resolve() != self.python
                or list(command[1:]) not in (["-m", "unittest", "test_add"], ["-m", "unittest", "test_calculator.py"])
                or not Path(cwd).resolve().is_relative_to(self.temporary)):
            self.fail("Only the reviewed addition/calculator and Git whitespace fixture verifiers are allowed")

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


def syntax_check():
    files = sorted(ROOT.rglob("*.py"))
    for path in files:
        if ".git" not in path.relative_to(ROOT).parts:
            ast.parse(path.read_bytes(), filename=str(path.relative_to(ROOT)))
    print(f"AST: {len(files)} Python files parsed", flush=True)


def main():
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.dont_write_bytecode = True
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

        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(tempfile, "tempdir", temporary))
            for name in ("request", "switch", "unload", "running"):
                stack.enter_context(patch.object(harness.Gateway, name, policy.gateway))
            stack.enter_context(patch.object(supervision, "run_command", guarded_verifier))
            stack.enter_context(patch.object(durable, "run_command", guarded_verifier))
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
