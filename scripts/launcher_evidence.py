"""Read existing evidence through the controller's validators; never run/recover it."""
import argparse
import json
from pathlib import Path
import shlex
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import durable


def inspect_run(root, run_id, expected=None):
    runs = Path(root).resolve() / "runs"
    if getattr(runs.lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT or runs.is_symlink():
        raise ValueError("Unsafe canonical runs directory")
    store = durable.Store(runs, run_id)
    if (getattr(store.directory.lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or store.directory.resolve().parent != runs):
        raise ValueError("Unsafe run directory")
    # load validates WAL, checkpoint references and terminal transaction/seal.
    # It performs no cache repair, report generation, recovery or inference.
    state = store.load()
    if store.tail:
        raise ValueError("Incomplete WAL tail; inspect retained evidence")
    if store._orphan_seals:
        raise ValueError("Orphan acceptance seal; manual inspection required")
    if expected:
        scope = state["manager"]["scope"]
        if (Path(state["target_repository"]).resolve() != Path(expected["Repo"]).resolve()
                or state["original_task"] != expected["Task"]
                or sorted(scope["allowed_paths"]) != sorted(expected["AllowPaths"])
                or sorted(scope["forbidden_paths"]) != sorted(expected["ForbidPaths"])
                or bool(state["options"].get("critic")) != (expected["Mode"] == "ThreeModel")
                or bool(state["options"].get("reviewer")) != (expected["Mode"] == "ThreeModel")):
            raise ValueError("Invocation and persisted run identity disagree")
        command = expected["Verify"]
        argv = json.loads(command) if command.lstrip().startswith("[") else shlex.split(command)
        if state["options"]["commands"] != [argv]:
            raise ValueError("Invocation and persisted verifier disagree")
    manager = state.get("manager", {})
    rounds = manager.get("rounds", [])
    active = rounds[-1] if rounds else {}
    report_status = "MISSING (WAL used)"
    report_path = store.directory / "final-report.json"
    if report_path.exists():
        try:
            if report_path.stat().st_size > 4 * 1024 * 1024:
                raise ValueError("oversized report")
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report_status = ("recorded projection matches WAL status" if
                             report.get("run_id") == run_id and report.get("status") == state["status"]
                             else "STALE / MISMATCHED (WAL used)")
        except (OSError, ValueError, AttributeError):
            report_status = "MALFORMED (WAL used)"
    checkpoint = state.get("last_verified_checkpoint")
    stop = manager.get("stop") or {}
    failure = stop or manager.get("last_failure")
    if not failure and state.get("last_failure_evidence"):
        failure = store.read_evidence(state["last_failure_evidence"])
    units = state.get("work_units", {}).get("units", {})
    attempts = [a for unit in units.values() for a in unit.get("attempts", [])]
    paths = sorted({p for attempt in attempts for p in attempt.get("changed_paths", [])})
    verification = active.get("verification", "UNKNOWN")
    if units:
        verification = "; ".join(f"{uid}: {unit['status']}" for uid, unit in units.items())
    return dict(validated=True, run_id=run_id, target_repository=state["target_repository"],
                status=state["status"], phase=manager.get("phase", "UNKNOWN"), reason=stop.get("reason", "UNKNOWN"),
                verification=verification, changed_paths=paths or active.get("changed_paths", []),
                cleanup=stop.get("cleanup", "UNKNOWN"),
                checkpoint=checkpoint["reference"]["path"] if checkpoint else None,
                terminal_accepted=store.is_terminal_accepted(),
                evidence_directory=str(store.directory), report_status=report_status,
                failure=failure)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    try:
        expected = json.load(sys.stdin)
        result = inspect_run(args.root, args.run_id, expected)
    except Exception as exc:
        result = dict(validated=False, error=f"UNKNOWN: {type(exc).__name__}: {exc}")
    print(json.dumps(result, ensure_ascii=True, default=str))


if __name__ == "__main__":
    main()
