"""ONE small real autonomous smoke of the bounded Manager loop (Qwen coder role; no GPT-OSS, no Nemotron).

Requires the dedicated llama-swap gateway started by scripts/start-swap.ps1 with NO model loaded.
Target: a throwaway fixture repository created under runs/ -- never this repository. The production
Gateway, Supervisor, DurableLog, verifier broker and Manager loop are used. Qwen plans and executes.

Round 1 is made to fail deterministically: after the real executor finishes its first attempt, this script
appends a one-line fault to the allowed file. The loop must then (a) see the verifier fail, (b) refuse to
checkpoint, and (c) drive a repair round from the deterministic failure evidence. The smoke proves the
Manager loop, not model quality. Nothing is pushed, merged or installed, and recover-model is never used.
"""
import json
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import durable
import harness
import manager
import supervision
import winprocess

FAULT = "slugify = None  # SMOKE-INJECTED-FAULT: remove this line; it shadows the real function\n"
STUB = '"""Small text helpers."""\n'
TEST_SLUG = ('import unittest\nfrom textutil import slugify\n\n\nclass SlugifyTests(unittest.TestCase):\n'
             '    def test_basic(self):\n        self.assertEqual(slugify("Hello World"), "hello-world")\n\n'
             '    def test_punctuation_and_spaces(self):\n        self.assertEqual(slugify("  A  quick, brown fox!  "), "a-quick-brown-fox")\n')
TEST_WORDS = ('import unittest\nfrom textutil import word_count\n\n\nclass WordCountTests(unittest.TestCase):\n'
              '    def test_count(self):\n        self.assertEqual(word_count("one two  three"), 3)\n\n'
              '    def test_empty(self):\n        self.assertEqual(word_count("   "), 0)\n')
TASK = ("Implement two functions in textutil.py so the provided tests pass: slugify(text) returns lowercase words "
        "joined by single hyphens with punctuation removed; word_count(text) returns the number of "
        "whitespace-separated words. Do not edit the tests.")
CRITERIA = [{"id": "SLUGIFY", "text": "slugify(text) behaves as specified", "checks": ["check-1"]},
            {"id": "WORDCOUNT", "text": "word_count(text) behaves as specified", "checks": ["check-2"]}]


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", f"safe.directory={repo}", "-c", "user.name=Smoke",
                    "-c", "user.email=smoke@localhost", *args], check=True, capture_output=True)


class FaultOnce:
    """Wraps the real executor; injects the round-1 fault after the real model attempt, exactly once."""
    def __init__(self, real, record):
        self.real, self.record = real, record

    def execute(self, step, context, repo):
        summary = self.real.execute(step, context, repo)
        if not self.record["injected"]:
            current = repo.read("textutil.py") if (repo.root / "textutil.py").exists() else STUB
            repo.write("textutil.py", current.rstrip("\n") + "\n" + FAULT)
            self.record["injected"] = True
        return summary


def main():
    config = harness.load_config(ROOT / "config" / "harness.json")
    workspace = ROOT / "runs" / ("autonomous-smoke-" + uuid.uuid4().hex[:12])
    repo_path = workspace / "repo"
    repo_path.mkdir(parents=True)
    report = {"passed": False, "evidence": str(workspace), "target": str(repo_path), "steps": []}
    t0 = time.monotonic()

    def step(name, **fields):
        report["steps"].append({"step": name, "time": round(time.monotonic() - t0, 2), **fields})
        print(f"[{report['steps'][-1]['time']:7.2f}s] {name} {json.dumps(fields, default=str)[:300]}", flush=True)

    preflight = harness.EventLog(workspace / "preflight.jsonl")
    probe = harness.Gateway(config, preflight)
    running, servers, ram = probe.running(), winprocess.servers(), harness.available_ram_gb()
    step("preflight", running=running, native_servers=servers, free_ram_gb=ram, required_gb=config["code_min_available_ram_gb"])
    if running or servers or (ram is not None and ram < config["code_min_available_ram_gb"]):
        step("ABORT_PREFLIGHT_UNSAFE")
        return 2

    (repo_path / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    for name, text in (("textutil.py", STUB), ("test_slugify.py", TEST_SLUG), ("test_wordcount.py", TEST_WORDS)):
        (repo_path / name).write_text(text, encoding="utf-8")
    git(repo_path, "init", "-q")
    git(repo_path, "add", ".")
    git(repo_path, "commit", "-qm", "baseline: stub and pinned tests")
    repo = harness.Repository(repo_path, config, preflight)
    commands = [["python", "-m", "unittest", "test_slugify"], ["python", "-m", "unittest", "test_wordcount"]]
    store = durable.Store(workspace / "runs", "autonomous-smoke")
    manager.create_run(store, repo, TASK, commands, config, criteria=[json.dumps(c) for c in CRITERIA],
                       budgets={"max_rounds": 5, "max_runtime_seconds": 1500, "max_step_retries": 2,
                                "max_consecutive_failed_rounds": 3, "max_stall_rounds": 2, "round_timeout_seconds": 600},
                       allow=["textutil.py"], forbid=["test_slugify.py", "test_wordcount.py"],
                       config_path=ROOT / "config" / "harness.json")
    step("run_created", run_id="autonomous-smoke", reviewer=False, critic=False, allow=["textutil.py"])

    peak = {"servers": 0, "samples": 0}
    stop = threading.Event()

    def sample():
        while not stop.is_set():
            peak["servers"] = max(peak["servers"], len(winprocess.servers()))
            peak["samples"] += 1
            stop.wait(0.5)
    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    fault = {"injected": False}

    def agents(repo_, gateway_, config_, log_):
        real = manager.default_agents(repo_, gateway_, config_, log_)
        real.executor = FaultOnce(real.executor, fault)
        return real
    failure = None
    try:
        result = manager.execute(store, agents=agents, progress=True)
    except BaseException as exc:
        failure = f"{type(exc).__name__}: {str(exc)[:300]}"
        result = None
        step("LOOP_RAISED", error=failure)
    stop.set()
    sampler.join()

    store.load()
    state = store.state
    events = store.records
    requests = [r for r in events if r["event"] == "model_request"]
    by_model = {}
    for r in requests:
        by_model[r["model"]] = by_model.get(r["model"], 0) + 1
    selected = [r["role"] for r in events if r["event"] == "model_selected"]
    rounds = [{k: r[k] for k in ("round", "kind", "step_id", "risk", "implementation", "verification", "critic",
                                 "reviewer", "trusted", "outcome", "reason", "attempt")} for r in state["manager"]["rounds"]]
    completion = None
    if state["manager"]["completion"]:
        completion = store.read_evidence(state["manager"]["completion"]["reference"])
    final_children = {c["child_id"]: {"purpose": c["purpose"], "state": c["state"], "reason": c["reason"],
                                      "termination": c["termination"]}
                      for c in state["supervision"]["children"].values()}
    idle_ok = True
    try:
        supervision.Supervisor(store).require_idle()
    except durable.DurableError as exc:
        idle_ok = False
        step("require_idle_refused", error=str(exc)[:300])
    after_running, after_servers = probe.running(), winprocess.servers()
    checkpoints = sorted(p.name for p in (store.directory / "checkpoints").iterdir())
    final_source = (repo_path / "textutil.py").read_text(encoding="utf-8")
    report.update(
        failure=failure, final_status=state["status"], stop=state["manager"]["stop"], rounds=rounds,
        rounds_used=len(rounds), fault_injected=fault["injected"], models_requests_by_model=by_model,
        models_selected_in_order=selected, checkpoints=checkpoints, verified_progress=state["verified_progress"],
        completion=completion, final_children=final_children, gateway_running_after=after_running,
        native_servers_after=after_servers, peak_native_servers_sampled=peak["servers"], samples=peak["samples"],
        free_ram_gb_after=harness.available_ram_gb(), supervisor_idle=idle_ok,
        fault_line_remaining="SMOKE-INJECTED-FAULT" in final_source, final_textutil=final_source,
        counters=state["manager"]["counters"], runtime_seconds=state["manager"]["runtime_seconds"])
    models = [c for c in final_children.values() if c["purpose"] == "model_server"]
    report["passed"] = bool(
        failure is None and state["status"] == "COMPLETED" and len(rounds) >= 2 and rounds[0]["trusted"] is False
        and rounds[0]["reason"] == "VERIFICATION_FAILED" and any(r["trusted"] for r in rounds)
        and completion and completion["all_proven"] and checkpoints and fault["injected"]
        and not report["fault_line_remaining"] and set(selected) == {"code"}
        and not after_running and not after_servers and idle_ok and peak["servers"] <= 1
        and models and all(c["state"] == "EXITED" for c in models))
    durable.atomic_write(workspace / "smoke-report.json", durable.encoded(report))
    print(json.dumps({k: v for k, v in report.items() if k not in {"steps", "final_textutil"}}, indent=2, default=str))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
