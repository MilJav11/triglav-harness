"""Sequential, sampled live evidence. Run from the harness root."""
import argparse
import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import ROOT, EventLog, Gateway, available_ram_gb, load_config
from winprocess import servers, hotpin_environment
from pin_evidence import locked_pages


def measure(gateway, role, label, log):
    gateway.unload()
    if servers():
        raise RuntimeError("Refusing measurement with pre-existing llama-server")
    stop = threading.Event()
    samples = []
    def sample():
        while not stop.is_set():
            point = dict(elapsed_seconds=round(time.monotonic() - start, 3),
                         free_gb=available_ram_gb(), processes=servers())
            samples.append(point)
            log.emit("resource_sample", label=label, **point)
            stop.wait(1)
    before = available_ram_gb()
    start = time.monotonic()
    thread = threading.Thread(target=sample, daemon=True)
    thread.start()
    result = dict(label=label, role=role, free_before_gb=before)
    try:
        gateway.switch(role)
        result["startup_seconds"] = round(time.monotonic() - start, 3)
        result["processes"] = servers()
        if role == "review":
            result["hotpin_environment"] = [hotpin_environment(p["pid"]) for p in servers()]
            result["locked_pages"] = [locked_pages(p["pid"]) for p in servers()]
        request_start = time.monotonic()
        answer = gateway.chat(role, [{"role": "user", "content": "Reply with exactly: LOCAL_OK"}],
                              "measurement", 512 if role == "review" else 32)
        result.update(request_seconds=round(time.monotonic() - request_start, 3), answer=answer,
                      free_active_gb=available_ram_gb(), active_processes=servers())
        if answer.strip() != "LOCAL_OK":
            raise RuntimeError(f"Unexpected smoke answer: {answer}")
        result["status"] = "passed"
    except Exception as exc:
        result.update(status="failed", error=str(exc))
        raise
    finally:
        stop.set()
        thread.join(10)
        result["minimum_sampled_free_gb"] = min(s["free_gb"] for s in samples)
        result["peak_sampled_ws_gb"] = max((p.get("working_set_gb", 0) for s in samples for p in s["processes"]), default=0)
        result["peak_sampled_private_gb"] = max((p.get("private_gb", 0) for s in samples for p in s["processes"]), default=0)
        unload_start = time.monotonic()
        try:
            gateway.unload()
            result.update(unload_seconds=round(time.monotonic() - unload_start, 3),
                          free_after_gb=available_ram_gb(), remaining_processes=servers())
            if result["remaining_processes"]:
                raise RuntimeError("Orphan llama-server after unload")
        except Exception as exc:
            result.update(status="failed", unload_error=str(exc))
            raise
        finally:
            log.emit("measurement_result", **result)
            print(json.dumps(result), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    parser.add_argument("--profile", choices=["review-safe", "review-performance"])
    parser.add_argument("--cycles", type=int, default=1)
    parser.add_argument("--review-only", action="store_true")
    args = parser.parse_args()
    log = EventLog(ROOT / "runs" / f"measure-{args.label}.jsonl")
    config = load_config(ROOT / "config/harness.json")
    if args.profile:
        config["review_profile"] = args.profile
    gateway = Gateway(config, log)
    for cycle in range(1, args.cycles + 1):
        if not args.review_only:
            measure(gateway, "code", f"{args.label}-{cycle}-code", log)
        measure(gateway, "review", f"{args.label}-{cycle}-review", log)
