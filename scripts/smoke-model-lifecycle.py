"""ONE bounded real-model smoke of the supervised Gateway.switch -> unload path (Qwen coder role only).

Requires the dedicated llama-swap gateway started by scripts/start-swap.ps1 with NO model loaded.
Uses the production configuration and the production Gateway/DurableLog/Supervisor; it starts no server
itself and never signals any process. On ambiguity it stops, leaves evidence, and reports.
"""
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import durable
import harness
import supervision
import winprocess

ROLE = "code"


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", f"safe.directory={repo}", "-c", "user.name=Smoke",
                    "-c", "user.email=smoke@localhost", *args], check=True, capture_output=True)


def children(store):
    return {c["child_id"]: {"purpose": c["purpose"], "state": c["state"], "reason": c["reason"],
                            "pid": c["pid"], "termination": c["termination"], "containment": c["containment"]}
            for c in store.state["supervision"]["children"].values()}


def main():
    config = harness.load_config(ROOT / "config" / "harness.json")
    report = {"role": ROLE, "alias": config["roles"][ROLE], "steps": [], "passed": False}
    workspace = ROOT / "runs" / ("model-lifecycle-smoke-" + uuid.uuid4().hex[:12])
    repo_path = workspace / "repo"
    repo_path.mkdir(parents=True)
    report["evidence"] = str(workspace)

    def step(name, **fields):
        report["steps"].append({"step": name, "time": round(time.monotonic() - t0, 2), **fields})
        print(f"[{report['steps'][-1]['time']:7.2f}s] {name} {json.dumps(fields)[:300]}", flush=True)

    t0 = time.monotonic()
    preflight = harness.EventLog(workspace / "preflight.jsonl")
    gateway_probe = harness.Gateway(config, preflight)
    running = gateway_probe.running()  # raises if the dedicated gateway is down: start it first
    before_servers = winprocess.servers()
    ram = harness.available_ram_gb()
    step("preflight", running=running, native_servers=before_servers, free_ram_gb=ram,
         required_gb=config["code_min_available_ram_gb"])
    if running or before_servers or (ram is not None and ram < config["code_min_available_ram_gb"]):
        step("ABORT_PREFLIGHT_UNSAFE")
        return 2

    (repo_path / "t.py").write_text("x = 1\n")
    git(repo_path, "init", "-q")
    git(repo_path, "add", ".")
    git(repo_path, "commit", "-qm", "baseline")
    repo = harness.Repository(repo_path, config, preflight)
    store = durable.Store(workspace / "runs", "model-smoke")
    store.create(repo, "model lifecycle smoke", [["git", "status"]], config, config_path=ROOT / "config" / "harness.json")

    peak = {"servers": 0, "samples": 0}
    stop = threading.Event()

    def sample():
        while not stop.is_set():
            peak["servers"] = max(peak["servers"], len(winprocess.servers()))
            peak["samples"] += 1
            stop.wait(0.5)
    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    failure = None
    with supervision.Supervisor(store) as supervisor:
        log = durable.DurableLog(store)
        gateway = harness.Gateway(config, log)
        gateway.supervisor = supervisor
        try:
            gateway.switch(ROLE)
            live = winprocess.servers()
            step("switch_complete", native_servers=live, gateway_running=gateway.running(), children=children(store))
            answer = gateway.chat(ROLE, [{"role": "user", "content": "Reply with exactly: LOCAL_OK"}], "smoke", 32)
            report["inference"] = {"performed": True, "max_tokens": 32, "reply_contains_LOCAL_OK": "LOCAL_OK" in answer}
            step("tiny_inference", **report["inference"])
        except BaseException as exc:
            failure = f"{type(exc).__name__}: {str(exc)[:300]}"
            step("FAILURE_BEFORE_UNLOAD", error=failure, children=children(store))
        finally:
            try:
                gateway.unload()  # guarded: refuses if delegated ownership is not proven; never PID-kills
                step("unload_complete", children=children(store))
            except BaseException as exc:
                failure = failure or f"unload {type(exc).__name__}: {str(exc)[:300]}"
                step("UNLOAD_REFUSED_OR_FAILED", error=str(exc)[:300], children=children(store))
    stop.set()
    sampler.join()
    after_running = gateway_probe.running()
    after_servers = winprocess.servers()
    final = children(store)
    models = [c for c in final.values() if c["purpose"] == "model_server"]
    other = [c for c in final.values() if c["purpose"] != "model_server"]
    idle_ok = True
    try:
        supervisor.require_idle()
    except durable.DurableError as exc:
        idle_ok = False
        step("require_idle_refused", error=str(exc)[:300])
    report.update(failure=failure, final_children=final, gateway_running_after=after_running,
                  native_servers_after=after_servers, peak_native_servers_sampled=peak["servers"],
                  samples=peak["samples"], free_ram_gb_after=harness.available_ram_gb(),
                  ledger_events=[r["transition"]["event"] for r in store.records
                                 if r["event"] == "state_committed" and r.get("transition")
                                 and r["transition"]["event"].startswith(("process_", "model_"))])
    report["passed"] = bool(
        failure is None and not after_running and not after_servers and idle_ok
        and len(models) == 1 and models[0]["state"] == "EXITED" and models[0]["termination"] == "forced"
        and not other and peak["servers"] == 1 and report["inference"]["reply_contains_LOCAL_OK"])
    durable.atomic_write(workspace / "smoke-report.json", durable.encoded(report))
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
