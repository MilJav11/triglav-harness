"""Real Qwen planning/executor and a slow pinned check in a disposable Git target.

Uses production Manager, verifier, supervision and checkpoint gates. Only the
terminal cadence and smaller smoke budgets differ. No critic/reviewer request,
raw wire capture, harness edits, operator-report task or harness commit.
"""
import io
import json
import subprocess
import sys
import tempfile
import threading
import uuid
from contextlib import redirect_stderr
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import durable
import harness as h
import manager as m
import supervision

CHECK = ["python", "-m", "unittest", "test_add"]
SEED = "def add(a, b):\n    return 0\n"
TEST = ("import time, unittest\nfrom calc import add\n"
        "class AdditionTest(unittest.TestCase):\n"
        "    def test_add(self):\n"
        "        time.sleep(4)\n"
        "        self.assertEqual(add(2, 3), 5)\n")
TASK = ("Perform one mechanical literal replacement in calc.py: replace 'return 0' with 'return a + b'. "
        "Read calc.py, use replace_text, run python -m unittest test_add, then done after PASS. "
        "Only calc.py may change; preserve test_add.py. This is a tiny disposable observability smoke.")


class TerminalCapture(io.TextIOBase):
    def __init__(self, terminal):
        self.terminal, self.parts, self.size = terminal, [], 0
        self.lock = threading.Lock()

    def write(self, text):
        with self.lock:
            self.terminal.write(text)
            remaining = max(0, 100_000 - self.size)
            self.parts.append(text[:remaining]) if remaining else None
            self.size += min(remaining, len(text))
        return len(text)

    def flush(self):
        self.terminal.flush()


def main():
    config = h.load_config(ROOT / "config/harness.json")
    config.update(heartbeat_seconds=2, max_actions=6)
    out = ROOT / "runs" / ("heartbeat-smoke-" + uuid.uuid4().hex[:12])
    out.mkdir()
    terminal = TerminalCapture(sys.stderr)
    log = h.EventLog(out / "preflight.jsonl", True, config)
    gateway = h.Gateway(config, log)
    if gateway.running() or h.servers():
        raise RuntimeError("Smoke requires an idle dedicated gateway and no native model server")
    report = {"passed": False, "evidence": str(out), "three_model_exercised": False}
    print("Evidence:", out, flush=True)
    with redirect_stderr(terminal):
        try:
            with tempfile.TemporaryDirectory(prefix="target-", dir=out) as target:
                path = Path(target).resolve()
                if not path.is_relative_to(out.resolve()):
                    raise RuntimeError("Unsafe temporary target")
                for name, text in ((".gitignore", "__pycache__/\n"), ("calc.py", SEED), ("test_add.py", TEST)):
                    (path / name).write_text(text, encoding="utf-8")
                for argv in (["init", "-q"], ["add", "."], ["commit", "-qm", "baseline: slow fixed addition test"]):
                    subprocess.run(["git", "-C", str(path), "-c", f"safe.directory={path}",
                                    "-c", "user.name=Heartbeat smoke", "-c", "user.email=smoke@localhost", *argv],
                                   check=True, capture_output=True)
                store = durable.Store(out / "state", "heartbeat")
                m.create_run(store, h.Repository(path, config, log), TASK, [CHECK], config,
                             criteria=["Addition passes the pinned test"],
                             budgets={"max_rounds": 1, "max_runtime_seconds": 300, "round_timeout_seconds": 180},
                             allow=["calc.py"], forbid=["test_add.py"])
                def agents(repo_, gateway_, config_, log_):
                    gateway_.log = log_
                    return m.default_agents(repo_, gateway_, config_, log_)
                m.execute(store, agents=agents, gateway=gateway, progress=True)
                store.load()
                supervision.Supervisor(store).require_idle()
                actions = [r["action"] for r in store.records if r["event"] == "agent_action"]
                requests = [r["phase"] for r in store.records if r["event"] == "model_request"]
                report.update(status=store.state["status"], actions=actions, requests=requests,
                              trusted_checkpoint=store.state["last_verified_checkpoint"] is not None,
                              tests_preserved=(path / "test_add.py").read_text(encoding="utf-8") == TEST,
                              supervisor_idle=True,
                              heartbeat_in_durable_evidence=any(r["event"] == "operation_heartbeat" for r in store.records))
            report["target_removed"] = not path.exists()
        except Exception as exc:
            report["error_class"] = type(exc).__name__
            if "store" in locals():
                report["status"] = store.state["status"]
        finally:
            try:
                gateway.unload()
                report["cleanup"] = {"gateway_running": gateway.running(), "servers": h.servers()}
            except Exception as exc:
                report["cleanup_error_class"] = type(exc).__name__
            report["target_removed"] = "path" in locals() and not path.exists()
    text = "".join(terminal.parts)
    (out / "terminal.log").write_text(text, encoding="utf-8")
    report["command_heartbeats"] = sum("COMMAND" in line and "still running" in line for line in text.splitlines())
    report["request_heartbeats"] = sum("REQUEST" in line and "still running" in line for line in text.splitlines())
    report["passed"] = bool(report.get("status") == "COMPLETED" and report.get("trusted_checkpoint")
                            and report.get("tests_preserved") and report.get("target_removed")
                            and report.get("supervisor_idle") and report["command_heartbeats"]
                            and "manager_plan" in report.get("requests", []) and "implement" in report.get("requests", [])
                            and "run_command" in report.get("actions", [])
                            and not report.get("heartbeat_in_durable_evidence")
                            and report.get("cleanup") == {"gateway_running": [], "servers": []})
    (out / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
