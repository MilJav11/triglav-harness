"""Parent-boundary proof policy for explicitly reviewed, trusted CI fixtures.

Not a hostile-code sandbox or an OS network boundary. Registration is a trusted
test-author capability, never model input. Callers supply reviewed source hashes
and import dependencies; a path under TEMP is not registration. Isolated Python
children do not inherit these audit hooks. Their code/launches are trusted, not
recursively enforced. No complete 848-case profile catalogue is provided here.
"""
from contextlib import contextmanager, ExitStack
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import sys
import threading
from unittest.mock import patch
import uuid


class PolicyViolation(RuntimeError):
    pass


@dataclass(frozen=True, eq=False)
class Fixture:
    root: Path
    identity: tuple


@dataclass(frozen=True)
class _Rule:
    profile: str
    boundary: str
    fixture: Fixture
    argv: tuple
    cwd: Path
    directories: tuple
    sources: tuple
    limit: int


def _absolute(path):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise PolicyViolation("Absolute non-traversing path required")
    for part in (path, *path.parents):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise PolicyViolation("Aliased/reparse path refused")
    return path


def _directory(path):
    path = _absolute(path)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise PolicyViolation("Directory required")
    return path, (info.st_dev, info.st_ino)


def _file(path):
    path = _absolute(path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise PolicyViolation("Regular single-link source required")
    raw = path.read_bytes()
    after = path.lstat()
    identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)
    if identity(before) != identity(after):
        raise PolicyViolation("Source changed while hashing")
    return path, identity(after), hashlib.sha256(raw).hexdigest()


def _unittest_tree(cwd, pinned):
    if any(p not in pinned for p in cwd.glob("*.py")):
        raise PolicyViolation("Unpinned unittest fixture source")
    for path in cwd.iterdir():
        if path.is_dir() and path.name != ".git":
            raise PolicyViolation("Unreviewed unittest package/cache")
        if path.name in {"unittest.py", "sitecustomize.py", "usercustomize.py"} or path.suffix.lower() in {".pth", ".pyc", ".pyo", ".pyd", ".pyw"}:
            raise PolicyViolation("Unreviewed unittest runner/import hook")


class SupplementalPolicy:
    """Fail-closed exact profiles at this trusted parent's two launch seams.

    Source pins must come from reviewed fixture definitions, not untrusted JSON
    or automatic adoption of candidate files. Other processes and direct calls
    bypassing these Python seams are outside the limited trusted-fixture model.
    File validation precedes creation; it is not atomic protection against a
    hostile same-user writer racing the filesystem or editing this policy.
    """
    _guard_owner = None
    PROFILES = {"native_worker", "isolated_helper", "inline_fixture", "unittest_fixture"}
    # SafeQwen's fixed child has IPC only; live transport remains in the parent.
    WORKERS = {"offline_scripted_executor_worker.py", "supervised_safe_qwen_worker.py"}

    def __init__(self, checkout, fresh_root):
        self.checkout, _ = _directory(checkout)
        self.root = Path(fresh_root)
        _directory(self.root.parent)
        self.root.mkdir()  # No adoption, even of an empty existing directory.
        self.root, self.root_identity = _directory(self.root)
        self.python, self.python_identity, self.python_sha256 = _file(Path(sys.executable).resolve())
        self.owner = (os.getpid(), threading.get_ident())
        self.fixtures, self.rules, self.uses, self.events = {}, [], {}, []
        self._admission = threading.RLock()
        self.active = self.hooked = False

    def _owner(self):
        if (os.getpid(), threading.get_ident()) != self.owner:
            raise PolicyViolation("Only the trusted fixture-author thread may register")

    def new_fixture(self):
        self._owner()
        path = self.root / uuid.uuid4().hex[:8]
        path.mkdir()
        path, identity = _directory(path)
        fixture = Fixture(path, identity)
        self.fixtures[id(fixture)] = fixture
        return fixture

    def _fixture(self, fixture):
        if type(fixture) is not Fixture or self.fixtures.get(id(fixture)) is not fixture:
            raise PolicyViolation("Unregistered fixture provenance")
        if _directory(self.root)[1] != self.root_identity or _directory(fixture.root)[1] != fixture.identity:
            raise PolicyViolation("Fixture directory identity drift")

    def register(self, fixture, *, profile, boundary, argv, cwd, source_pins, limit=1):
        """Freeze an exact trusted definition before entering guard()."""
        self._owner()
        if self.active:
            raise PolicyViolation("Cannot expand profiles during execution")
        self._fixture(fixture)
        if profile not in self.PROFILES or boundary not in {"native", "popen"}:
            raise PolicyViolation("Unknown closed launch profile")
        if type(limit) is not int or not 1 <= limit <= 64:
            raise PolicyViolation("Bounded trusted launch count required")
        if not isinstance(argv, (list, tuple)) or any(type(a) is not str or "\0" in a for a in argv):
            raise PolicyViolation("Exact text argv required")
        argv = tuple(argv)
        if not argv or argv[0] != str(self.python):
            raise PolicyViolation("Only the pinned Python executable is supported")
        cwd, _ = _directory(cwd)
        if not cwd.is_relative_to(fixture.root):
            raise PolicyViolation("cwd outside owned fixture")
        if type(source_pins) is not dict:
            raise PolicyViolation("Explicit reviewed source hashes required")
        sources = []
        for name, expected in source_pins.items():
            source = _file(name)
            if not (source[0].is_relative_to(fixture.root) or source[0].is_relative_to(self.checkout)):
                raise PolicyViolation("Source outside owned fixture/checkout")
            if type(expected) is not str or source[2] != expected:
                raise PolicyViolation("Reviewed source hash mismatch")
            sources.append(source)
        pinned = {s[0] for s in sources}
        if profile == "native_worker":
            if boundary != "native" or len(argv) != 4 or argv[1:3] != ("-I", "-B"):
                raise PolicyViolation("Wrong worker argv")
            entry = Path(argv[3])
            if entry.parent != self.checkout or entry.name not in self.WORKERS or entry not in pinned:
                raise PolicyViolation("Unreviewed worker entry")
        elif profile == "isolated_helper":
            if len(argv) < 4 or argv[1:3] != ("-I", "-B"):
                raise PolicyViolation("Wrong isolated helper argv")
            entry = Path(argv[3])
            if entry.suffix != ".py" or not entry.is_relative_to(fixture.root) or entry not in pinned:
                raise PolicyViolation("Unregistered isolated helper source")
        elif profile == "inline_fixture":
            if not (len(argv) == 5 and argv[1:4] == ("-I", "-B", "-c")):
                raise PolicyViolation("Only exact isolated inline fixtures supported")
            if source_pins:
                raise PolicyViolation("Inline code is pinned in the exact argv")
        else:
            if len(argv) != 4 or argv[1:3] != ("-m", "unittest"):
                raise PolicyViolation("Wrong reviewed unittest fixture argv")
            entry = cwd / argv[3]
            if Path(argv[3]).name != argv[3] or not argv[3].startswith("test_") or entry.suffix != ".py" or entry not in pinned:
                raise PolicyViolation("Unreviewed unittest target")
            _unittest_tree(cwd, pinned)
        directories = []
        for path in {cwd, *(s[0].parent for s in sources)}:
            while path.is_relative_to(fixture.root) or path.is_relative_to(self.checkout):
                directories.append(_directory(path))
                if path in {fixture.root, self.checkout}: break
                path = path.parent
        rule = _Rule(profile, boundary, fixture, argv, cwd, tuple(directories), tuple(sources), limit)
        if any((r.boundary, r.argv, r.cwd) == (boundary, argv, cwd) for r in self.rules):
            raise PolicyViolation("Duplicate launch registration")
        self.rules.append(rule)
        return rule

    def _deny(self, boundary, reason):
        self.events.append(dict(decision="DENY", boundary=boundary, reason=reason))
        raise PolicyViolation(reason)

    def authorize(self, boundary, command, cwd, *, executable=None, environment=None):
        with self._admission:
            return self._authorize(boundary, command, cwd, executable=executable, environment=environment)

    def _authorize(self, boundary, command, cwd, *, executable, environment):
        if not self.active or os.getpid() != self.owner[0]:
            self._deny(boundary, "No active trusted parent guard")
        try:
            directory, _ = _directory(cwd)
            matches = [r for r in self.rules if r.boundary == boundary and r.cwd == directory
                       and (command == subprocess.list2cmdline(r.argv) if isinstance(command, str)
                            else isinstance(command, (list, tuple)) and tuple(command) == r.argv)]
            if len(matches) != 1: raise PolicyViolation("Unregistered executable/argv/cwd")
            rule = matches[0]
            if executable is not None and Path(executable) != self.python:
                raise PolicyViolation("Executable override refused")
            if environment is not None:
                raise PolicyViolation("Unreviewed environment override refused")
            self._fixture(rule.fixture)
            if _file(self.python) != (self.python, self.python_identity, self.python_sha256):
                raise PolicyViolation("Python executable identity/hash drift")
            for path, identity in rule.directories:
                if _directory(path)[1] != identity: raise PolicyViolation("cwd/source directory identity drift")
            for source in rule.sources:
                if _file(source[0]) != source: raise PolicyViolation("Registered source identity/hash drift")
            if rule.profile == "unittest_fixture":
                _unittest_tree(rule.cwd, {s[0] for s in rule.sources})
            count = self.uses.get(id(rule), 0)
            if count >= rule.limit: raise PolicyViolation("Trusted launch count exhausted")
        except (PolicyViolation, OSError, TypeError, ValueError) as exc:
            self._deny(boundary, str(exc))
        self.uses[id(rule)] = count + 1  # No refund after uncertain creation.
        self.events.append(dict(decision="ALLOW", boundary=boundary, profile=rule.profile,
                                argv=list(rule.argv), cwd=str(rule.cwd),
                                executable_sha256=self.python_sha256,
                                fixture_identity=list(rule.fixture.identity), launch_number=count + 1,
                                sources={str(p): h for p, _, h in rule.sources}))

    def _audit(self, event, args):
        if not self.active: return
        if event.startswith("socket.") and event != "socket.gethostname":
            self._deny("parent_socket", "Parent socket operation refused")
        if event in {"os.system", "os.startfile", "os.posix_spawn", "os.posix_spawnp"}:
            self._deny("parent_process", "Alternative parent launch refused")
        if event == "subprocess.Popen":
            self.authorize("popen", args[1], args[2], executable=args[0], environment=args[3])

    @contextmanager
    def guard(self):
        self._owner()
        if SupplementalPolicy._guard_owner is not None:
            raise PolicyViolation("Overlapping parent policies refused")
        import harness
        import winprocess
        original = winprocess._create_verifier_in_job
        def create(kernel, command, cwd, inherited, job_handle):
            self.authorize("native", command, cwd)
            return original(kernel, command, cwd, inherited, job_handle)
        def gateway(*args, **kwargs):
            self._deny("parent_gateway", "Unmocked parent Gateway operation refused")
        if not self.hooked:
            sys.addaudithook(self._audit)
            self.hooked = True
        SupplementalPolicy._guard_owner = self
        try:
            with ExitStack() as stack:
                stack.enter_context(patch.object(winprocess, "_create_verifier_in_job", create))
                for name in ("request", "switch", "unload", "running"):
                    stack.enter_context(patch.object(harness.Gateway, name, gateway))
                self.active = True
                try: yield self
                finally: self.active = False
        finally:
            SupplementalPolicy._guard_owner = None
