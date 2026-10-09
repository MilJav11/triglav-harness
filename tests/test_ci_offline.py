"""Exercise the offline CI boundary without launching prohibited operations."""
from pathlib import Path
import sys
import tempfile
import unittest

from scripts.ci_offline import OfflineViolation, ProcessPolicy, ROOT


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fixture = Path(self.temp.name)
        self.policy = ProcessPolicy(ROOT, self.fixture)

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
        self.policy.verifier([sys.executable, "-m", "unittest", "test_calculator.py"], self.fixture)
        self.policy.verifier([str(self.policy.git), "-c", f"safe.directory={self.fixture.resolve()}",
                              "-c", "diff.external=", "diff", "--no-ext-diff",
                              "--no-textconv", "--check"], self.fixture)
        self.policy.process([sys.executable, str(ROOT / "supervision_worker.py"),
                             str(self.fixture / "broker")], ROOT)

    def test_unreviewed_verifier_is_denied(self):
        self.denied(self.policy.verifier, [sys.executable, "-m", "unittest", "test_sub"], self.fixture)
