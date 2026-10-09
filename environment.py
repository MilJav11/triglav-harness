"""Versioned execution evidence. Model shards use metadata, never full rehashing."""
import hashlib
import json
import platform
from pathlib import Path
import re
import shutil
import subprocess
import sys

from supervision import checksum

VERSION = 1
ROOT = Path(__file__).resolve().parent


def file_identity(path, strong=True):
    path = Path(path).resolve()
    result = {"path": str(path)}
    try:
        stat = path.stat()
        result.update(size=stat.st_size, mtime_ns=stat.st_mtime_ns)
        if not path.is_file():
            return {**result, "error": "not_file"}
        if strong:
            with path.open("rb") as stream:
                result["sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
            after = path.stat()
            if (after.st_size, after.st_mtime_ns, after.st_ino) != (stat.st_size, stat.st_mtime_ns, stat.st_ino):
                return {**result, "error": "changed_during_read"}
        return result
    except OSError:
        return {**result, "error": "unavailable"}


def capture(config, repository, commands, config_path=None, root=ROOT, executor=None):
    root = Path(root)
    head = subprocess.run(["git", "-c", f"safe.directory={root}", "rev-parse", "HEAD"],
                          cwd=root, capture_output=True, timeout=15)
    if head.returncode or not head.stdout.strip():
        raise RuntimeError("Cannot establish harness Git HEAD; environment identity unavailable")
    source = {p.name: file_identity(p) for name in
              ("harness.py", "durable.py", "critic.py", "winprocess.py", "supervision.py",
               "supervision_worker.py", "environment.py", "manager.py", "recovery.py",
               "planner_protocol.py", "opencode_executor.py", "work_units.py", "work_unit_scheduler.py",
               "work_unit_planner.py", "live_work_unit_executor.py", "critic_executor.py",
               "reviewer_executor.py", "candidate_evidence.py") if (p := root / name).exists()}
    configs = {}
    runtime = {"python": file_identity(sys.executable)}
    for dll in sorted(Path(sys.executable).parent.glob("python*.dll")):
        runtime[str(dll)] = file_identity(dll)
    for name in sorted({c[0] for c in commands} | {"git"}):
        executable = shutil.which(name)
        runtime["command:" + name] = file_identity(executable) if executable else {"error": "unavailable"}
    effective_ex = executor or config.get("executor_instance") or config.get("agents")
    live_instance = None
    is_scripted = False
    if effective_ex is not None:
        ex_cls = type(effective_ex).__name__
        if ex_cls == "LiveWorkUnitExecutor":
            live_instance = effective_ex
        elif ex_cls in ("Agents", "SimpleNamespace"):
            inner = getattr(effective_ex, "executor", None)
            if inner is not None:
                inner_cls = type(inner).__name__
                if inner_cls == "LiveWorkUnitExecutor":
                    live_instance = inner
                elif inner_cls == "ScriptedExecutor":
                    is_scripted = True
        elif ex_cls == "ScriptedExecutor":
            is_scripted = True

    if is_scripted:
        is_live = False
    elif live_instance is not None:
        is_live = True
    elif effective_ex is not None:
        is_live = False
    else:
        is_live = (
            config.get("executor") in ("cline", "live_work_unit", "live")
            or config.get("effective_executor") in ("cline", "live_work_unit", "live", "LiveWorkUnitExecutor")
        )
    if effective_ex is None and config.get("executor", "custom") == "opencode":
        from opencode_executor import executable
        runtime["opencode"] = file_identity(executable(config))
    elif is_live:
        from live_work_unit_executor import cline_executable
        live_cfg = getattr(live_instance, "config", None) if live_instance is not None else None
        cline_path = cline_executable(live_cfg) if live_cfg is not None else cline_executable(config)
        runtime["cline"] = file_identity(cline_path) if cline_path else {"error": "unavailable"}
    models = {}
    # Parse only the local, controlled template's explicit command/model paths. Do not
    # execute YAML or interpolate commands. Unknown formats fail closed in preflight.
    template = root / "config/llama-swap.yaml"
    if "review_profiles" in config:
        for path in (template, root / "runs/swap-profile.yaml", root / "runs/swap-profile.json"):
            configs[str(path.resolve())] = file_identity(path)
        text = "\n".join(path.read_text(encoding="utf-8") for path in
                         (template, root / "runs/swap-profile.yaml") if path.exists())
        binaries = sorted(set(re.findall(r'"([^"\r\n]+\.exe)"', text)))
        paths = sorted(set(re.findall(r'--model\s+"([^"\r\n]+)"', text)))
        if not binaries or not paths:
            runtime["model_discovery"] = {"error": "unavailable"}
        for binary in binaries + [str(root / "tools/llama-swap/bin/llama-swap.exe")]:
            runtime[binary] = file_identity(binary)
            for dll in sorted(Path(binary).parent.glob("*.dll")):
                runtime[str(dll)] = file_identity(dll)
        for model in paths:
            path = Path(model)
            shards = sorted(path.parent.glob(re.sub(r"-\d{5}-of-\d{5}\.gguf$", "-*-of-*.gguf", path.name)))
            for shard in shards or [path]:
                models[str(shard)] = file_identity(shard, strong=False)
        for profile, values in config.get("review_profiles", {}).items():
            if values.get("hot_experts"):
                configs["expert:" + profile] = file_identity(values["hot_experts"])
    if config_path:
        configs["harness_config_source"] = file_identity(config_path)
    value = {"version": VERSION, "repository": str(Path(repository).resolve()),
             "os": {"system": platform.system(), "version": platform.version(), "machine": platform.machine()},
             "python_version": platform.python_version(), "harness_head": head.stdout.decode().strip(),
             "source": source, "config_sha256": checksum(config), "config": config,
             "commands_sha256": checksum(commands), "configs": configs, "runtime": runtime,
             "models": models, "profile": config.get("review_profile"),
             "execution_flags": {"optimize": sys.flags.optimize, "utf8_mode": sys.flags.utf8_mode,
                                 "isolated": sys.flags.isolated},
             "known_model_identity": config.get("model_metadata", {})}
    value["sha256"] = checksum(value)
    return value


def validate(value):
    from durable import DurableError
    def require(condition):
        if not condition:
            raise ValueError("invalid fingerprint")
    try:
        require(type(value["version"]) is int and value["version"] == VERSION)
        require(value["sha256"] == checksum({k: v for k, v in value.items() if k != "sha256"}))
        for key in ("config", "source", "configs", "runtime", "models", "os", "known_model_identity", "execution_flags"):
            require(isinstance(value[key], dict))
        for key in ("repository", "python_version", "harness_head", "config_sha256", "commands_sha256"):
            require(isinstance(value[key], str))
    except (KeyError, TypeError, ValueError):
        raise DurableError("Corrupt environment fingerprint") from None


def semantic(value):
    if isinstance(value, dict):
        return {k: semantic(v) for k, v in value.items() if k != "mtime_ns"}
    return value


def compare(before, after):
    validate(before)
    validate(after)
    changes = []
    def add(field, level, old, new):
        changes.append({"field": field, "class": level, "before": old, "after": new})
    for group in ("source", "runtime", "configs", "models"):
        for key in sorted(before[group].keys() | after[group].keys()):
            old, new = before[group].get(key), after[group].get(key)
            field = f"{group}:{key}"
            if new is None or "error" in new:
                add(field, "UNSAFE", old, new)
            elif old != new:
                informational = old is not None and semantic(old) == semantic(new) and "sha256" in new
                level = "INFORMATIONAL" if informational else "UNSAFE" if group in {"source", "runtime"} else "REVALIDATION_REQUIRED"
                if key == "harness_config_source" and not informational:
                    level = "UNSAFE"  # Never reinterpret a changed source under pinned semantics.
                add(field, level, old, new)
    for field in ("repository", "harness_head", "commands_sha256", "python_version", "os", "config_sha256",
                  "profile", "known_model_identity", "execution_flags"):
        old, new = before[field], after[field]
        if old != new:
            level = "UNSAFE" if field in {"repository", "harness_head", "commands_sha256", "execution_flags"} else "REVALIDATION_REQUIRED"
            if field == "python_version" and old.split(".")[:2] != new.split(".")[:2]:
                level = "UNSAFE"
            if field == "config_sha256":
                allowed = {"review_profile", "review_profiles", "model_metadata", "startup_timeout_seconds",
                           "shutdown_timeout_seconds", "request_timeout_seconds"}
                if {k: v for k, v in before["config"].items() if k not in allowed} != {k: v for k, v in after["config"].items() if k not in allowed}:
                    level = "UNSAFE"
            add(field, level, old, new)
    level = next((level for level in ("UNSAFE", "REVALIDATION_REQUIRED", "INFORMATIONAL")
                  if any(c["class"] == level for c in changes)), "MATCH")
    return {"decision": level, "changes": changes}
