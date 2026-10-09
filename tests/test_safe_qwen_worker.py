"""Offline tests; scripted responses; no real Gateway or model."""
import ast
import contextlib
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
H = ROOT
sys.path.insert(1, str(H))
sys.path.insert(2, str(H / "tests"))
from safe_qwen_worker import Policy, ToolSession, SafeQwenWorker, sha, relative_name
from tool_protocol import Denied, TOOLS, schema
from integration_shim import SafeQwenExecutor

BASELINE = ('def stock_by_sku(movements):\n'
            '    balances = {}\n'
            '    for movement in movements:\n'
            '        quantity = movement["quantity"]\n'
            '        balances[movement["sku"]] = quantity\n'
            '    return {sku: quantity for sku, quantity in balances.items() if quantity != 0}\n')
OLD = 'balances[movement["sku"]] = quantity'
NEW = 'balances[movement["sku"]] = balances.get(movement["sku"], 0) + quantity'

def operation(tool, **args):
    return json.dumps({"tool": tool, "arguments": args})
def submit():
    return operation("submit", summary="Candidate ready")
def spec(path="inventory.py", mode="mutation"):
    return {"unit_id": "u", "objective": "Aggregate quantities", "mode": mode,
            "scope": {"allowed_paths": [path], "forbidden_paths": []},
            "limits": {"max_attempts": 1, "timeout_seconds": 60}}

@contextlib.contextmanager
def no_process():
    targets = ["subprocess.Popen", "subprocess.run", "subprocess.call", "os.system", "os.popen"]
    if hasattr(os, "startfile"):
        targets.append("os.startfile")
    if os.name == "nt":
        targets.append("_winapi.CreateProcess")
    with contextlib.ExitStack() as stack:
        mocks = [stack.enter_context(patch(t, side_effect=AssertionError("Process attempted"))) for t in targets]
        yield
        for m in mocks:
            m.assert_not_called()

def link_dir(source, target):
    try:
        target.symlink_to(source, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        import _winapi
        _winapi.CreateJunction(str(source), str(target))

class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        (self.root / "inventory.py").write_bytes(BASELINE.encode("utf-8"))
        (self.root / "test_inventory.py").write_bytes(b"frozen oracle\n")
        (self.root / "private.txt").write_text("secret\n", encoding="utf-8")
        self.policy = Policy(self.root, ("inventory.py", "test_inventory.py"),
                             ("inventory.py",), ("test_inventory.py",), ("inventory.py",))
        self.audit = []
        self.session = ToolSession(self.policy, audit=self.audit.append)
    def edit(self, path="inventory.py", old=OLD, new=NEW, digest=None):
        return operation("edit_file", path=path, old_text=old, new_text=new,
                         before_sha256=digest or sha((self.root / "inventory.py").read_bytes()))

    def test_tool_inventory_has_no_execution(self):
        self.assertEqual(TOOLS, ("read_files", "edit_file", "submit"))
        for branch in schema()["oneOf"]:
            self.assertIn(branch["properties"]["tool"]["const"], TOOLS)

    def test_no_dynamic_code_or_process_imports(self):
        for name in ("safe_qwen_worker.py", "tool_protocol.py", "integration_shim.py"):
            for node in ast.walk(ast.parse((ROOT / name).read_text())):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = [n.name for n in node.names] if isinstance(node, ast.Import) else [node.module]
                    self.assertFalse(set(names) & {"subprocess", "ctypes", "_winapi", "socket", "urllib.request"})
                if isinstance(node, ast.Call):
                    name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
                    self.assertNotIn(name, ("exec", "eval", "compile", "__import__", "system",
                                            "popen", "Popen", "CreateProcess", "startfile"))

    def test_tq02_delete_simulation_and_valid_edit_only_inventory_changes(self):
        before = {p.name: sha(p.read_bytes()) for p in self.root.iterdir()}
        with no_process():
            result = self.session.dispatch(operation("run_commands", commands=["del inventory.py"]))
            self.assertEqual(result, {"ok": False, "code": "UNKNOWN_TOOL"})
            self.assertTrue((self.root / "inventory.py").exists())
            self.assertEqual(before, {p.name: sha(p.read_bytes()) for p in self.root.iterdir()})
            result = self.session.dispatch(self.edit())
        after = {p.name: sha(p.read_bytes()) for p in self.root.iterdir()}
        self.assertTrue(result["ok"])
        self.assertEqual([p for p in before if before[p] != after[p]], ["inventory.py"])
        self.assertEqual(set(before), set(after))
        self.assertEqual(result["before_sha256"], before["inventory.py"])
        self.assertEqual(result["after_sha256"], after["inventory.py"])
        self.assertEqual([r["phase"] for r in self.audit if "phase" in r], ["intent", "applied"])
        self.assertIn(NEW, (self.root / "inventory.py").read_text())

    def test_shell_prose_cannot_execute(self):
        with no_process():
            for raw in ("del inventory.py", 'run_commands(["del inventory.py"])',
                        chr(96)*3 + "json\n" + submit() + "\n" + chr(96)*3, "Explanation " + submit()):
                self.assertFalse(self.session.dispatch(raw)["ok"])
        self.assertTrue((self.root / "inventory.py").exists())

    def test_absolute_paths_denied_for_read_and_edit(self):
        for p in (str(self.root / "inventory.py"), "/tmp/inventory.py", "C:/inventory.py",
                  r"\\server\share\inventory.py", r"\\?\C:\inventory.py", "C:inventory.py"):
            with self.subTest(path=p):
                self.assertEqual(self.session.dispatch(operation("read_files", paths=[p]))["code"], "PATH_DENIED")
                self.assertEqual(self.session.dispatch(self.edit(path=p))["code"], "PATH_DENIED")

    def test_traversal_and_windows_aliases_denied(self):
        for p in ("../inventory.py", "x/../inventory.py", "x/./inventory.py", "x//inventory.py",
                  "inventory.py.", "inventory.py:ads", "NUL", "COM1.txt", ".git/config", ".codex/x", r"x\..\inventory.py"):
            with self.subTest(path=p), self.assertRaises(Denied):
                relative_name(p)

    def test_read_permitted_read_only_frozen_file(self):
        result = self.session.dispatch(operation("read_files", paths=["test_inventory.py"]))
        self.assertTrue(result["ok"])
        self.assertEqual(result["files"][0]["text"], "frozen oracle\n")

    def test_unauthorized_read_denied(self):
        self.assertEqual(self.session.dispatch(operation("read_files", paths=["private.txt"]))["code"], "SCOPE_DENIED")
    def test_frozen_write_denied(self):
        self.assertEqual(self.session.dispatch(self.edit(path="test_inventory.py"))["code"], "FROZEN_FILE")
    def test_unauthorized_write_denied(self):
        self.assertEqual(self.session.dispatch(self.edit(path="private.txt"))["code"], "SCOPE_DENIED")

    def test_required_file_cannot_delete_move_rename_or_empty(self):
        with no_process():
            for tool in ("delete", "delete_file", "move", "rename", "shell", "PowerShell", "cmd.exe",
                         "subprocess", "network", "fetch_web_content", "apply_patch"):
                self.assertEqual(self.session.dispatch(operation(tool, path="inventory.py"))["code"], "UNKNOWN_TOOL")
            self.assertEqual(self.session.dispatch(self.edit(old=BASELINE, new=""))["code"], "CONTENT_DENIED")
        self.assertEqual((self.root / "inventory.py").read_text(), BASELINE)

    def test_unknown_tool_fails_closed(self):
        self.assertEqual(self.session.dispatch(operation("anything", command="del inventory.py"))["code"], "UNKNOWN_TOOL")

    def test_malformed_json_and_operations(self):
        malformed = ["", "{", "[]", "null", submit() + submit(),
                     '{"tool":"submit","tool":"edit_file","arguments":{}}',
                     '{"tool":"submit","arguments":{"summary":NaN}}',
                     operation("submit", summary="ok", trusted=True),
                     '{"tool":"read_files","arguments":{"paths":"inventory.py"}}',
                     '{"tool":"read_files","arguments":{"paths":[1]}}',
                     '{"tool":[],"arguments":{}}', '{"tool":"submit","arguments":null}',
                     '{"tool":"submit","arguments":{"summary":"\\ud800"}}', "[" * 1200 + "]" * 1200]
        with no_process():
            for raw in malformed:
                self.assertFalse(self.session.dispatch(raw)["ok"])

    def test_missing_edit_fields_and_extra_fields_denied(self):
        self.assertFalse(self.session.dispatch(operation("edit_file", path="inventory.py", new_text="x"))["ok"])
        obj = json.loads(self.edit())
        obj["arguments"]["commands"] = ["del inventory.py"]
        self.assertFalse(self.session.dispatch(json.dumps(obj))["ok"])

    def test_stale_hash_and_inexact_match_rejected(self):
        self.assertEqual(self.session.dispatch(self.edit(digest="0" * 64))["code"], "STALE_FILE")
        self.assertEqual(self.session.dispatch(self.edit(old="missing"))["code"], "EXACT_MATCH_REQUIRED")

    def test_duplicate_match_rejected(self):
        (self.root / "inventory.py").write_text("repeat repeat")
        self.assertEqual(self.session.dispatch(self.edit(old="repeat", new="x"))["code"], "EXACT_MATCH_REQUIRED")

    def test_symlink_or_junction_escape_rejected(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (outside / "escape.py").write_text("outside\n")
        link_dir(outside, self.root / "link")
        policy = Policy(self.root, ("link/escape.py",), ("link/escape.py",), (), ())
        with no_process():
            for writing in (False, True):
                with self.assertRaises(Denied) as caught:
                    policy.target("link/escape.py", write=writing)
                self.assertEqual(caught.exception.code, "PATH_DENIED")
        self.assertEqual((outside / "escape.py").read_text(), "outside\n")

    def test_reparse_attribute_denied(self):
        actual = Path.lstat
        def reparse(p, *a, **kw):
            value = actual(p, *a, **kw)
            if p.name == "inventory.py":
                return SimpleNamespace(st_mode=value.st_mode, st_file_attributes=0x400)
            return value
        with patch.object(Path, "lstat", reparse):
            self.assertEqual(self.session.dispatch(self.edit())["code"], "PATH_DENIED")

    def test_hardlink_denied(self):
        os.link(self.root / "inventory.py", Path(self.temp.name) / "alias.py")
        self.assertEqual(self.session.dispatch(self.edit())["code"], "PATH_DENIED")

    def test_root_symlink_or_junction_denied(self):
        alias = Path(self.temp.name) / "alias"
        link_dir(self.root, alias)
        with self.assertRaises(Denied):
            Policy(alias, ("inventory.py",), (), (), ())

    def test_frozen_external_change_stops_submit(self):
        (self.root / "test_inventory.py").write_text("tamper")
        self.assertEqual(self.session.dispatch(submit())["code"], "FROZEN_CHANGED")
        self.assertFalse(self.session.submitted)

    def test_required_missing_stops_submit(self):
        (self.root / "inventory.py").unlink()
        self.assertFalse(self.session.dispatch(submit())["ok"])
        self.assertFalse(self.session.submitted)

    def test_atomic_failure_preserves_required_file_and_cleans_temp(self):
        with patch("safe_qwen_worker.os.replace", side_effect=OSError("fault")):
            self.assertFalse(self.session.dispatch(self.edit())["ok"])
        self.assertEqual((self.root / "inventory.py").read_text(), BASELINE)
        self.assertFalse(list(self.root.glob(".safe-qwen-*")))

    def test_audit_intent_failure_prevents_edit(self):
        session = ToolSession(self.policy, audit=Mock(side_effect=RuntimeError("audit failure")))
        with self.assertRaises(RuntimeError):
            session.dispatch(self.edit())
        self.assertEqual((self.root / "inventory.py").read_text(), BASELINE)

    def test_submit_is_untrusted_and_terminal(self):
        self.assertEqual(self.session.dispatch(submit()), {"ok": True, "submitted": True, "trusted": False})
        self.assertEqual(self.session.dispatch(self.edit())["code"], "ALREADY_SUBMITTED")

    def test_invalid_calls_consume_hard_action_budget(self):
        session = ToolSession(self.policy, audit=self.audit.append, max_actions=2)
        for _ in range(2):
            self.assertFalse(session.dispatch("bad")["ok"])
        self.assertEqual(session.dispatch(submit())["code"], "ACTION_LIMIT")

    def test_worker_loop_does_not_retry_outside_one_attempt(self):
        transport = Mock(return_value="malformed")
        worker = SafeQwenWorker(self.policy, transport=transport, audit=self.audit.append, max_actions=2)
        with no_process(), self.assertRaises(Denied):
            worker.execute(spec(), {}, SimpleNamespace(root=self.root))
        self.assertEqual(transport.call_count, 2)

    def test_transport_exception_has_no_retry(self):
        transport = Mock(side_effect=ConnectionError("offline"))
        worker = SafeQwenWorker(self.policy, transport=transport, audit=self.audit.append)
        with self.assertRaises(ConnectionError):
            worker.execute(spec(), {}, SimpleNamespace(root=self.root))
        self.assertEqual(transport.call_count, 1)

    def test_read_only_workunit_cannot_edit(self):
        replies = iter([self.edit(), submit()])
        worker = SafeQwenWorker(self.policy, transport=lambda *a: next(replies), audit=self.audit.append)
        receipt = worker.execute(spec(mode="read_only"), {}, SimpleNamespace(root=self.root))
        self.assertFalse(receipt["trusted"])
        self.assertEqual((self.root / "inventory.py").read_text(), BASELINE)
        self.assertIn({"action": 1, "ok": False, "code": "SCOPE_DENIED"}, self.audit)

    def test_expired_deadline_never_requests_transport(self):
        transport = Mock()
        worker = SafeQwenWorker(self.policy, transport=transport, audit=self.audit.append)
        worker.set_deadline(time.monotonic() - 1)
        with self.assertRaises(TimeoutError):
            worker.execute(spec(), {}, SimpleNamespace(root=self.root))
        transport.assert_not_called()

    def test_scope_mismatch_refuses_before_transport(self):
        transport = Mock()
        worker = SafeQwenWorker(self.policy, transport=transport, audit=self.audit.append)
        with self.assertRaises(Denied):
            worker.execute(spec(path="other.py"), {}, SimpleNamespace(root=self.root))
        transport.assert_not_called()

    def test_code_payload_is_written_as_data_not_executed(self):
        payload = "import os\nos.system('del inventory.py')\n"
        with no_process():
            self.assertTrue(self.session.dispatch(self.edit(old=BASELINE, new=payload))["ok"])
        self.assertTrue((self.root / "inventory.py").exists())

    def test_audit_oserror_is_fatal(self):
        session = ToolSession(self.policy, audit=Mock(side_effect=OSError("audit fault")))
        with self.assertRaises(RuntimeError):
            session.dispatch(self.edit())
        self.assertEqual((self.root / "inventory.py").read_text(), BASELINE)

    def test_exact_crlf_replacement_preserves_line_endings(self):
        (self.root / "inventory.py").write_bytes(BASELINE.replace("\n", "\r\n").encode())
        self.assertTrue(self.session.dispatch(self.edit())["ok"])
        result = (self.root / "inventory.py").read_bytes()
        self.assertIn(b"\r\n", result)
        self.assertEqual(result.count(b"\n"), result.count(b"\r\n"))

    def test_real_windows_junction_escape(self):
        if os.name != "nt":
            self.skipTest("Windows junction only")
        import _winapi
        outside = Path(self.temp.name) / "junction-outside"
        outside.mkdir()
        (outside / "x.py").write_text("outside")
        junction = self.root / "junction"
        _winapi.CreateJunction(str(outside), str(junction))
        policy = Policy(self.root, ("junction/x.py",), ("junction/x.py",), (), ())
        for write in (False, True):
            with self.assertRaises(Denied):
                policy.target("junction/x.py", write=write)
        self.assertEqual((outside / "x.py").read_text(), "outside")

    def test_file_and_operation_size_limits(self):
        (self.root / "inventory.py").write_bytes(b"x" * 65537)
        self.assertEqual(self.session.dispatch(operation("read_files", paths=["inventory.py"]))["code"], "FILE_TOO_LARGE")
        self.assertEqual(self.session.dispatch("x" * 70001)["code"], "MALFORMED_JSON")

class ControllerIntegrationTests(unittest.TestCase):
    def test_existing_scheduler_and_verifier_ignore_submit_claim(self):
        from test_work_units_wave2 import fixture, make_spec
        import manager
        self.assertEqual(Path(manager.__file__).parent, H)
        from work_unit_scheduler import MILESTONE_FAILED, MILESTONE_READY
        for valid_edit in (False, True):
            with self.subTest(valid_edit=valid_edit), tempfile.TemporaryDirectory() as temp:
                repo, store, config = fixture(Path(temp), "native-offline")
                unit = make_spec(paths=["calculator.py"], limits={"max_attempts": 1, "timeout_seconds": 60})
                registry = {"check-calc": {"argv": ["python", "-m", "unittest", "test_calculator.py"]}}
                manager.create_run(store, repo, "Offline native seam", [registry["check-calc"]["argv"]], config,
                                   criteria=["multiply"], work_units=[unit], verifier_registry=registry)
                policy = Policy(repo.root, ("calculator.py", "test_calculator.py"),
                                ("calculator.py",), ("test_calculator.py",), ("calculator.py",))
                responses = []
                if valid_edit:
                    responses.append(operation("edit_file", path="calculator.py",
                        before_sha256=sha((repo.root / "calculator.py").read_bytes()),
                        old_text="return 0", new_text="return a * b"))
                responses.append(submit())
                replies = iter(responses)
                worker = SafeQwenWorker(policy, transport=lambda *a: next(replies), audit=lambda r: None)
                result = manager.execute_work_units(store, agents=manager.Agents(None, worker, None, None),
                                                    verifier_registry=registry)
                self.assertEqual(result["status"], MILESTONE_READY if valid_edit else MILESTONE_FAILED)
                self.assertEqual(store.state["work_units"]["units"]["unit-1"]["status"],
                                 "UNIT_VERIFIED" if valid_edit else "UNIT_FAILED")
                self.assertIsNone(store.state["last_verified_checkpoint"])
                self.assertEqual(store.state["verified_progress"], [])

    def test_controller_gateway_shim_fake_transport_and_store(self):
        from manager import ExecutorError
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo_root = root / "repo"
            repo_root.mkdir()
            (repo_root / "inventory.py").write_text(BASELINE)
            policy = Policy(repo_root, ("inventory.py",), ("inventory.py",), (), ("inventory.py",))
            artifacts = {}
            store = SimpleNamespace(directory=root / "store", state={"target_repository": str(repo_root)},
                                    artifact=lambda path, value: artifacts.__setitem__(path, copy.deepcopy(value)))
            log = SimpleNamespace(emit=lambda *a, **k: None, operation=lambda *a, **k: contextlib.nullcontext())
            cfg = {"base_url": "http://127.0.0.1:9292", "roles": {"code": "llm-code"}, "request_timeout_seconds": 60}
            payloads, lifecycle = [], []
            def request(method, path, payload, timeout):
                payloads.append(copy.deepcopy(payload))
                self.assertEqual((method, path), ("POST", "/v1/chat/completions"))
                return {"choices": [{"finish_reason": "stop",
                                     "message": {"role": "assistant", "content": submit()}}]}
            gw = SimpleNamespace(config=cfg, supervisor=SimpleNamespace(store=store),
                 execution_deadline=None, set_deadline=lambda d: setattr(gw, "execution_deadline", d),
                 bounded_timeout=lambda t: t, log=log, request=request,
                 check_active_ram=lambda: None, switch=lambda r: lifecycle.append("switch"),
                 unload=lambda: lifecycle.append("unload"))
            repo = SimpleNamespace(root=repo_root, store=store)
            adapter = SafeQwenExecutor(policy, cfg, gateway=gw, log=log)
            with no_process():
                receipt = adapter.execute(spec(), {}, repo)
            self.assertFalse(receipt["trusted"])
            self.assertEqual(lifecycle, ["switch", "unload"])
            self.assertIsNone(gw.execution_deadline)
            self.assertEqual(len(payloads), 1)
            self.assertNotIn("tools", payloads[0])
            self.assertEqual(payloads[0]["response_format"]["json_schema"]["schema"], schema())
            self.assertTrue(any(p.endswith("result.json") for p in artifacts))
            good = {"choices": [{"finish_reason": "stop",
                     "message": {"role": "assistant", "content": submit()}}]}
            malformed_wires = [
                None, {}, {"choices": []},
                {"choices": [{"finish_reason": "length", "message": {"role": "assistant", "content": submit()}}]},
                {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": submit(),
                            "tool_calls": [{"function": {"name": "run_commands", "arguments": '{"commands":["del inventory.py"]}'}}]}}]},
                {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": []}}]},
            ]
            for bad in malformed_wires:
                gw.request = Mock(side_effect=[bad, good])
                with no_process():
                    result = adapter.execute(spec(), {}, repo)
                self.assertEqual(result["actions"], 2)
                self.assertEqual((repo_root / "inventory.py").read_text(), BASELINE)
                self.assertEqual(gw.request.call_count, 2)
            gw.request = Mock(side_effect=ConnectionError("offline"))
            with self.assertRaises(ConnectionError):
                adapter.execute(spec(), {}, repo)
            self.assertEqual(gw.request.call_count, 1)
            self.assertEqual(lifecycle[-1], "unload")
            self.assertIsNone(gw.execution_deadline)
            gw.unload = Mock(side_effect=RuntimeError("cleanup fault"))
            with self.assertRaises(ExecutorError) as caught:
                adapter.execute(spec(), {}, repo)
            self.assertEqual(caught.exception.reason, "CLEANUP_FAILED")

    def test_no_gateway_refuses(self):
        from manager import ExecutorError
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "x.py").write_text("x")
            p = Policy(root, ("x.py",), ("x.py",), (), ())
            with self.assertRaises(ExecutorError):
                SafeQwenExecutor(p, {}).execute(spec("x.py"), {}, SimpleNamespace(root=root))

if __name__ == "__main__":
    unittest.main(verbosity=2)
