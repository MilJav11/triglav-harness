"""Bounded fresh-controller, reboot, and drift smoke; no inference servers."""
import argparse
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import durable
import environment
import supervision
import winprocess
from test_durable import fixture, FakeGateway


def wait_file(path, proc=None):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if path.exists():
            return
        if proc and proc.poll() is not None:
            raise AssertionError(f"Broker exited early: {proc.returncode}")
        time.sleep(0.1)
    raise AssertionError(f"Timed out waiting for handshake: {path.name}")


def child(mode, workspace):
    store = durable.Store(workspace / "runs", "test-run")
    store.load()
    sup = supervision.Supervisor(store)
    if mode == "start":
        command = [sys.executable, "-c",
            "import pathlib,subprocess,sys,time; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)']); "
            "pathlib.Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(120)", str(workspace / "descendant.pid")]
        child_id = sup.register("helper", sys.executable, command)
        record = sup.child(child_id)
        directory = store.directory / "processes" / child_id
        directory.mkdir(parents=True)
        durable.atomic_write(directory / "spec.json", durable.encoded(dict(command=command, cwd=str(workspace),
                              timeout=120, token=record["token"], job=record["job"])))
        proc = subprocess.Popen([sys.executable, str(ROOT / "supervision_worker.py"), str(directory)],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=0x08000000)
        sup.started(child_id, proc.pid)
        durable.atomic_write(directory / "go.json", durable.encoded({"token": record["token"]}))
        wait_file(workspace / "descendant.pid", proc)
        sup.heartbeat()
        durable.atomic_write(workspace / "child.json", durable.encoded({"child_id": child_id, "pid": proc.pid,
                              "identity": winprocess.identity(proc.pid), "boot": winprocess.boot_identity()}))
        os._exit(73)
    record = json.loads((workspace / "child.json").read_text())
    if mode == "recover":
        # Exercise the public resume decision before explicit owner-scoped cleanup.
        args = argparse.Namespace(command="resume", run_id="test-run", quiet=True,
                                  revalidate_unverified=False, revalidate_environment=False)
        with patch("durable.Gateway", side_effect=AssertionError("duplicate workflow")):
            try:
                durable.cli(args, workspace)
            except durable.DurableError as exc:
                assert "RUNNING" in str(exc), str(exc)
            else:
                raise AssertionError("Resume duplicated a live verifier")
        store.load()
        current = sup.reconcile()[0]
        assert current["identity"] == record["identity"] and current["pid"] == record["pid"]
        sup.terminate(record["child_id"])
        assert winprocess.identity(record["pid"]) is None
        descendant = int((workspace / "descendant.pid").read_text())
        deadline = time.monotonic() + 10
        while winprocess.identity(descendant) is not None and time.monotonic() < deadline:
            time.sleep(0.1)
        assert winprocess.identity(descendant) is None
        durable.atomic_write(workspace / "cleanup.json", durable.encoded({"broker_pid": record["pid"],
            "descendant_pid": descendant, "broker_absent": True, "descendant_absent": True,
            "termination": store.state["supervision"]["children"][record["child_id"]]["termination"]}))
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", choices=["start", "recover"])
    parser.add_argument("--workspace", type=Path)
    args = parser.parse_args()
    if args.child:
        return child(args.child, args.workspace)
    workspace = ROOT / "runs" / ("supervision-smoke-" + uuid.uuid4().hex[:12])
    workspace.mkdir(parents=True)
    repo, store, _ = fixture(workspace)
    command = [sys.executable, str(Path(__file__).resolve()), "--workspace", str(workspace)]
    try:
        start = subprocess.run(command + ["--child", "start"], capture_output=True, timeout=30)
        (workspace / "start.stderr.txt").write_bytes(start.stderr)
        assert start.returncode == 73, start.stderr.decode()
        recover = subprocess.run(command + ["--child", "recover"], capture_output=True, timeout=30)
        (workspace / "recover.stderr.txt").write_bytes(recover.stderr)
        assert recover.returncode == 0, recover.stderr.decode()
    finally:
        # Cleanup on assertion failures still uses durable ownership and job membership.
        store.load()
        sup = supervision.Supervisor(store)
        for record in sup.reconcile():
            if record["state"] in {"RUNNING", "STOPPING"}:
                sup.terminate(record["child_id"])
    store.load()
    with patch("durable.Gateway", FakeGateway):
        durable.execute(store)
    checkpoint = copy.deepcopy(store.state["last_verified_checkpoint"])
    sup = supervision.Supervisor(store, boot=lambda: "simulated-old-boot")
    old = sup.register("helper", sys.executable, [])
    sup.started(old, os.getpid())
    resume_args = argparse.Namespace(command="resume", run_id="test-run", quiet=True,
                                     revalidate_unverified=False, revalidate_environment=False)
    with patch("winprocess.terminate_owned_job", side_effect=AssertionError("old PID signaled")) as kill, \
         patch("durable.Gateway", side_effect=AssertionError("checkpoint must reconstruct")):
        result = durable.cli(resume_args, workspace)
    kill.assert_not_called()
    store.load()
    assert result["status"] == "COMPLETED" and store.state["last_verified_checkpoint"] == checkpoint
    assert store.state["supervision"]["children"][old]["reason"] == "LOST_AFTER_REBOOT"

    # An actual controlled small execution component changes, with stable config structure.
    component = workspace / "execution-profile.json"
    component.write_text('{"profile":"one"}')
    old_capture = durable.current_fingerprint
    def capture(store):
        value = old_capture(store)
        value["configs"]["smoke_execution_profile"] = environment.file_identity(component)
        value["sha256"] = supervision.checksum({k: v for k, v in value.items() if k != "sha256"})
        return value
    store.commit(environment=capture(store))
    component.write_text('{"profile":"two"}')
    with patch("durable.current_fingerprint", capture), patch("durable.Gateway", side_effect=AssertionError("silent continuation")):
        try:
            durable.cli(resume_args, workspace)
        except durable.DurableError as exc:
            assert "revalidation required" in str(exc), str(exc)
        else:
            raise AssertionError("Environment drift silently continued")
    report = {"passed": True, "smoke_1": "controller exit 73; fresh resume blocked duplicate; exact owned job terminated",
              "smoke_2": "simulated old boot; zero signals; same trusted checkpoint reconstructed",
              "smoke_3": "controlled profile file changed; revalidation required; zero model calls",
              "cleanup": json.loads((workspace / "cleanup.json").read_text()), "evidence": str(workspace)}
    durable.atomic_write(workspace / "smoke-report.json", durable.encoded(report))
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
