"""Private gated command/executor broker. No unrestricted CLI entry point."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from durable import atomic_write, encoded
from winprocess import OwnedJob


def main(directory):
    spec = json.loads((directory / "spec.json").read_text())
    job = OwnedJob(spec["job"])
    deadline = time.monotonic() + 15
    while not (directory / "go.json").exists():
        if time.monotonic() >= deadline:
            return 74  # Controller died before authorizing work; nothing launched.
        time.sleep(0.1)
    if json.loads((directory / "go.json").read_text())["token"] != spec["token"]:
        return 75
    if "deadline" in spec:
        spec["timeout"] = max(0, min(spec["timeout"], spec["deadline"] - time.monotonic()))
    termination, code, metadata = "natural", None, None
    try:
        if spec.get("opencode"):
            from opencode_executor import run_episode_process
            code, termination, metadata = run_episode_process(spec)
        else:
            with (directory / "stdout").open("wb") as out, (directory / "stderr").open("wb") as err:
                proc = subprocess.Popen(spec["command"], cwd=spec["cwd"], stdin=subprocess.DEVNULL,
                                        stdout=out, stderr=err, shell=False, close_fds=True)
                try:
                    code = proc.wait(timeout=spec["timeout"])
                except subprocess.TimeoutExpired:
                    termination = "forced"
                    err.write(b"Command timed out; owned process tree terminated\n")
    except OSError:
        termination = "launch_failed"
    atomic_write(directory / "exit.json", encoded({"token": spec["token"], "exit_code": code,
                 "termination": termination, "descendant_cleanup": "job_close", "metadata": metadata}))
    # Kernel cleanup includes all descendants, including ones whose root already exited.
    job.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1])))
