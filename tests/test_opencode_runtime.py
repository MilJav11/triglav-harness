"""Pinned OpenCode + scripted HTTP inference, no models or llama-swap required.
Opt in with HARNESS_TEST_OPENCODE_RUNTIME=1. Normal suite still covers Git/trust gates.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

import opencode_executor as oc
import supervision
from test_manager import git_fixture, step, ADD_OK
from test_supervision import MemoryStore


@unittest.skipUnless(os.name == "nt" and os.environ.get("HARNESS_TEST_OPENCODE_RUNTIME") == "1",
                     "opt-in deterministic pinned Windows OpenCode runtime")
class OpenCodeRuntimeTests(unittest.TestCase):
    def episode(self, *, legacy=False, previous_repair=False):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate, git = git_fixture(root)
            # A tool result large enough to cross the old 4096-token headroom.
            (candidate / "large.txt").write_text("context line abcdefghijklmnopqrstuvwxyz\n" * 450)
            git("add", ".")
            git("commit", "-qm", "large read fixture")
            head = git("rev-parse", "HEAD").stdout
            index = (candidate / ".git/index").read_bytes()
            store = MemoryStore()
            store.directory = root / "controller"
            store.directory.mkdir()
            directory = store.directory / "opencode" / ("a" * 32)
            directory.mkdir(parents=True)
            env = oc.episode_environment(directory, data_directory=store.directory / "oc" / ("a" * 16))
            env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env["PATH"]
            context = {"scope": {"forbidden_paths": []},
                       "checks": [{"argv": ["python", "-m", "pytest", "-q", "test_add.py"]}]}
            settings = oc.configuration({"base_url": "http://127.0.0.1:9292", "roles": {"code": "llm-code"}},
                                         step(), context, type("Repo", (), {"path": lambda _, p: candidate / p})())
            if legacy:
                settings["provider"]["local"]["models"]["llm-code"]["limit"].pop("input")
                settings.pop("compaction")
            elif previous_repair:
                settings["compaction"]["reserved"] = 8192
            requests, compacted, oversized = [], [], []
            actions = ["read", "edit", "bash", "stop"]

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def do_POST(self):
                    request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    # OpenCode also makes an auxiliary title request. Distinguish
                    # it from compaction so it cannot satisfy the regression gate.
                    title_request = "Generate a title for this conversation:" in json.dumps(request["messages"])
                    if not title_request:
                        requests.append(request)
                    if title_request:
                        action = "title"
                    elif not request.get("tools"):
                        compacted.append(request)
                        action = "summary"
                    elif actions[0] == "edit" and not compacted:
                        # Reproduce the backend rejection when the old usage threshold
                        # permits another coding request after the large read.
                        oversized.append(request)
                        self.send_response(400)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(json.dumps({"error": {"message":
                            "request (34168 tokens) exceeds the available context size (32768 tokens)",
                            "type": "exceed_context_size_error"}}).encode())
                        return
                    else:
                        action = actions.pop(0)
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    if action in {"summary", "stop", "title"}:
                        delta = {"role": "assistant", "content":
                                 "Read completed. Implement calc.add then run test_add." if action == "summary" else "Done."}
                        reason = "stop"
                    else:
                        arguments = {
                            "read": {"filePath": str(candidate / "large.txt")},
                            "edit": {"filePath": str(candidate / "calc.py"), "oldString": "return 0", "newString": "return a + b"},
                            "bash": {"command": "python -m pytest -q test_add.py", "description": "Run acceptance test"},
                        }[action]
                        if action == "edit":
                            arguments["oldString"] = "def add(a, b):\n    return 0"
                            arguments["newString"] = "def add(a, b):\n    return a + b"
                        delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": "call_" + action,
                                 "type": "function", "function": {"name": action, "arguments": json.dumps(arguments)}}]}
                        reason = "tool_calls"
                    usage = {"prompt_tokens": 19123 if action == "read" else 900,
                             "completion_tokens": 100, "total_tokens": 19223 if action == "read" else 1000}
                    for choices, tokens in [([{ "index": 0, "delta": delta, "finish_reason": None}], None),
                                            ([{ "index": 0, "delta": {}, "finish_reason": reason}], usage)]:
                        event = {"id": "chatcmpl-test", "object": "chat.completion.chunk", "created": 1,
                                 "model": "llm-code", "choices": choices}
                        if tokens:
                            event["usage"] = tokens
                        self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
                    self.wfile.write(b"data: [DONE]\n\n")

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                # Test-only routing to scripted inference; production routing stays pinned.
                settings["provider"]["local"]["options"]["baseURL"] = f"http://127.0.0.1:{server.server_port}/v1"
                (directory / "config/opencode.json").write_text(json.dumps(settings))
                binary = oc.executable({})
                pin = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=15, check=True)
                self.assertEqual(pin.stdout.strip(), oc.VERSION)
                result = supervision.run_command(supervision.Supervisor(store),
                    [binary, "run", "--pure", "--format", "json", "--model", oc.MODEL], candidate, 45, 0,
                    purpose="opencode_executor", env=env, stdin="Implement calc.add. Read in chunks of at most 120 lines. Run python -m pytest -q test_add.py.",
                    opencode=True)
            finally:
                server.shutdown()
                server.server_close()
            self.assertEqual(result["termination"], "natural", result)
            self.assertEqual(result["exit_code"], int(legacy or previous_repair), result)
            self.assertTrue(result["metadata"]["finished"], result)
            self.assertFalse(result["metadata"]["protocol_error"], result)
            self.assertEqual(len(compacted), 1)
            self.assertEqual(len(oversized), int(legacy or previous_repair))
            self.assertEqual(result["metadata"]["error"], legacy or previous_repair)
            self.assertEqual(result["metadata"]["tools"].get("edit"), 1)
            self.assertEqual(result["metadata"]["command_exits"], [0])
            self.assertEqual((candidate / "calc.py").read_text(), ADD_OK)
            self.assertFalse((candidate / ".pytest_cache").exists())
            self.assertEqual(git("rev-parse", "HEAD").stdout, head)
            self.assertEqual((candidate / ".git/index").read_bytes(), index)
            # Actual OpenCode snapshot tracking must see the edit, without warnings.
            data = Path(env["XDG_DATA_HOME"]) / "opencode"
            log = (data / "log/opencode.log").read_text()
            self.assertNotIn("failed to list snapshot files", log)
            self.assertNotIn("'$GIT_DIR' too big", log)
            snapshots = list((data / "snapshot").glob("*/*"))
            self.assertEqual(len(snapshots), 1)
            tree = subprocess.run(["git", "--git-dir", str(snapshots[0]), "write-tree"], check=True,
                                  capture_output=True, text=True).stdout.strip()
            tracked = subprocess.run(["git", "--git-dir", str(snapshots[0]), "show", tree + ":calc.py"],
                                     check=True, capture_output=True, text=True).stdout
            self.assertEqual(tracked, ADD_OK)
            self.assertTrue(all(r["max_tokens"] == 4096 for r in requests))
            # Compaction serializes/truncates old outputs and the resumed request
            # doesn't replay the entire large read.
            resumed = requests[-3]
            if not (legacy or previous_repair):
                self.assertLess(len(json.dumps(resumed["messages"])), 20000)
            child = list(store.state["supervision"]["children"].values())[0]
            self.assertEqual(child["state"], "EXITED")
            return len(requests)

    def test_early_compaction_completes_coding_and_verification_without_overflow(self):
        self.assertEqual(self.episode(), 5)

    def test_legacy_threshold_reproduces_overflow_and_remains_untrusted(self):
        self.assertEqual(self.episode(legacy=True), 6)

    def test_previous_repair_threshold_reproduces_live_overflow(self):
        self.assertEqual(self.episode(previous_repair=True), 6)
