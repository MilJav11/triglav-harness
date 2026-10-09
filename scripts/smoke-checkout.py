"""Run the realistic fixture through the normal CLI with resource evidence."""
import contextlib
import sys
import threading
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import ROOT, EventLog, available_ram_gb, main
from winprocess import servers
from pin_evidence import locked_pages

log = EventLog(ROOT / "runs/checkout-resources.jsonl")
stop = threading.Event()
sampled = set()

def sample():
    while not stop.is_set():
        processes = servers()
        log.emit("resource_sample", free_gb=available_ram_gb(), processes=processes)
        for process in processes:
            if ("hotpin" in process.get("path", "").lower() and process.get("working_set_gb", 0) > 6
                    and process["pid"] not in sampled):
                log.emit("locked_page_evidence", **locked_pages(process["pid"]))
                sampled.add(process["pid"])
        stop.wait(1)

thread = threading.Thread(target=sample, daemon=True)
thread.start()
try:
    with (ROOT / "runs/checkout-smoke-output.txt").open("w", encoding="utf-8") as output:
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = main(["--review-profile", "review-safe", "run", "--repo", str(ROOT / "runs/checkout-smoke"),
                           "--task", "Fix the checkout defects according to README.md. Inspect README.md, money.py, checkout.py and test_checkout.py. Round each extended line (unit price times quantity) to cents with Decimal ROUND_HALF_UP before summing. Charge shipping once for a nonempty cart and zero for an empty cart. Preserve input lines and the existing tests; fix implementation in both modules. Run the real unittest suite.",
                           "--verify", "python -m unittest discover -v", "--reviewer"])
finally:
    stop.set()
    thread.join(10)
    log.emit("smoke_resources_finished", free_gb=available_ram_gb(), processes=servers())
print((ROOT / "runs/checkout-smoke-output.txt").read_text(encoding="utf-8"))
raise SystemExit(result)
