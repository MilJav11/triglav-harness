"""Explicit SafeQwen source/configuration binding; no global patching or model I/O."""
import copy
import hashlib
import json
import math
import os
import re
from pathlib import Path

import integration_shim
import safe_qwen_worker
import tool_protocol
from integration_shim import SafeQwenExecutor
from safe_qwen_worker import Policy

ROOT = Path(__file__).resolve().parent
SOURCES = {name: ROOT / name for name in (
    "safe_qwen_worker.py", "tool_protocol.py", "integration_shim.py",
    "terminal_run.py", "safe_executor_binding.py")}
for module in (integration_shim, safe_qwen_worker, tool_protocol):
    if Path(module.__file__).resolve() != ROOT / (module.__name__ + ".py"):
        raise ValueError("SafeQwen module shadowing refused")


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                    allow_nan=False).encode()).hexdigest()


LOADED = {name: file_sha(path) for name, path in SOURCES.items()}


def validate_config(cfg):
    from urllib.parse import urlsplit
    u = urlsplit(cfg["base_url"])
    if (u.scheme != "http" or u.hostname != "127.0.0.1" or not u.port
            or u.username or u.password or u.path not in ("", "/") or u.query or u.fragment):
        raise ValueError("SafeQwen requires a dedicated loopback llama-swap gateway")
    if cfg.get("executor") != "safe_qwen":
        raise ValueError("SafeQwen must be explicitly selected")
    for role, alias in cfg["roles"].items():
        if role not in ("code", "critic", "review") or type(alias) is not str or not alias.strip():
            raise ValueError("Unsupported role/alias")
    if "code" not in cfg["roles"]:
        raise ValueError("Missing code model alias")
    for name in ("timeout_seconds", "request_timeout_seconds", "startup_timeout_seconds",
                 "shutdown_timeout_seconds"):
        value = cfg.get(name, 600)
        if type(value) not in (float, int) or not math.isfinite(value) or not 0 < value <= 3600:
            raise ValueError("SafeQwen timeout must be finite and in (0, 3600]")
    if tool_protocol.TOOLS != ("read_files", "edit_file", "submit"):
        raise ValueError("SafeQwen tool inventory refused")


def identity(worker):
    if type(worker) is not SafeQwenExecutor:
        raise ValueError("Wrong SafeQwen executor type")
    validate_config(worker.config)
    if type(worker.max_actions) is not int or not 1 <= worker.max_actions <= 32:
        raise ValueError("SafeQwen action bound refused")
    hashes = {name: file_sha(path) for name, path in SOURCES.items()}
    if hashes != LOADED:
        raise ValueError("SafeQwen source changed since import; restart with reviewed pins")
    cfg = {k: v for k, v in worker.config.items() if k != "safe_qwen_identity"}
    p = worker.policy
    value = {"version": 2, "name": "SafeQwenExecutor", "sources": hashes,
             "configuration_sha256": digest(cfg), "tools": list(tool_protocol.TOOLS),
             "operation_schema": tool_protocol.schema(), "max_actions": worker.max_actions,
             "scope": {"root": str(p.root), "readable": sorted(p.readable),
                       "writable": sorted(p.writable), "frozen": sorted(p.frozen),
                       "required": sorted(p.required)}}
    return {**value, "sha256": digest(value)}


def validate_pinned(cfg, executor=None):
    validate_config(cfg)
    pin = cfg.get("safe_qwen_identity")
    if (type(pin) is not dict or pin.get("version") != 2
            or pin.get("sha256") != digest({k: v for k, v in pin.items() if k != "sha256"})
            or pin.get("configuration_sha256") != digest({k: v for k, v in cfg.items() if k != "safe_qwen_identity"})
            or pin.get("sources") != {name: file_sha(path) for name, path in SOURCES.items()}
            or pin.get("sources") != LOADED or pin.get("tools") != list(tool_protocol.TOOLS)
            or pin.get("operation_schema") != tool_protocol.schema()):
        raise ValueError("SafeQwen pinned source/configuration identity mismatch")
    if executor is not None and identity(executor) != pin:
        raise ValueError("Effective SafeQwen executor differs from pinned identity")
    return pin


def restore_worker(cfg):
    pin = validate_pinned(cfg)
    p = pin["scope"]
    policy = Policy(Path(p["root"]), *(tuple(p[k]) for k in ("readable", "writable", "frozen", "required")))
    worker = SafeQwenExecutor(policy, copy.deepcopy(cfg), max_actions=pin["max_actions"])
    validate_pinned(cfg, worker)
    return worker


def extend_capture(value, cfg, executor=None):
    """Called directly by environment.capture only for the explicit SafeQwen route."""
    from environment import file_identity
    pin = validate_pinned(cfg, executor)
    for name, path in SOURCES.items():
        value["runtime"]["safe_qwen:" + name] = file_identity(path)
    value["runtime"]["safe_qwen:policy"] = {"sha256": pin["sha256"]}
    # Operator-supplied local runtime files, never discovered through private templates.
    for role, files in cfg["safe_qwen_runtime"].items():
        for kind, path in files.items():
            group = value["models"] if kind == "model" else value["runtime"]
            group[f"safe_qwen:{role}:{kind}"] = file_identity(path, strong=kind != "model")
            if "error" in group[f"safe_qwen:{role}:{kind}"]:
                raise ValueError("SafeQwen runtime identity unavailable")
    for kind, path in cfg["safe_qwen_gateway"].items():
        value["runtime"]["safe_qwen:gateway:" + kind] = file_identity(path)
        if "error" in value["runtime"]["safe_qwen:gateway:" + kind]:
            raise ValueError("SafeQwen gateway ownership identity unavailable")
    value["safe_qwen"] = {"version": 2, "identity_sha256": pin["sha256"], "tools": pin["tools"]}


def bind_executor(store, executor):
    from durable import DurableError
    cfg = store.state["options"]["config"]
    try:
        validate_pinned(cfg, executor)
        if type(executor) is not SafeQwenExecutor:
            raise ValueError("SafeQwen requires its actual executor")
        if executor.policy.root != Path(store.state["target_repository"]).resolve():
            raise ValueError("SafeQwen repository binding mismatch")
    except (ValueError, KeyError, OSError) as exc:
        raise DurableError("SafeQwen executor binding refused") from exc
    options = copy.deepcopy(store.state["options"])
    record = {"kind": "safe_qwen", "identity_sha256": cfg["safe_qwen_identity"]["sha256"]}
    previous = options.get("work_unit_executor")
    if previous is not None and previous != record:
        raise DurableError("SafeQwen durable executor identity changed")
    if previous is None:
        options["work_unit_executor"] = record
        store.commit(options=options)
    store._effective_work_unit_executor = executor


def model_starting(supervisor, role):
    """Retain delegated supervision using explicit local gateway ownership inputs.

    Only the controlled llama-swap `cmd: >-` / quoted executable and --model
    format is supported. No YAML interpreter, command expansion or process launch.
    """
    from durable import DurableError
    supervisor.require_no_ambiguous_model()
    cfg = supervisor.store.state["options"]["config"]
    validate_pinned(cfg)
    if role not in cfg["safe_qwen_runtime"]:
        raise DurableError("SafeQwen role has no pinned runtime")
    import environment
    from durable import current_fingerprint
    drift = environment.compare(supervisor.store.state["environment"], current_fingerprint(supervisor.store))
    if drift["decision"] in {"UNSAFE", "REVALIDATION_REQUIRED"}:
        raise DurableError("SafeQwen runtime changed before model dispatch")
    resources = cfg["safe_qwen_runtime"][role]
    path = Path(resources["gateway_config"])
    if path.stat().st_size > 1048576:
        raise DurableError("SafeQwen gateway configuration too large")
    text = path.read_text(encoding="utf-8")
    alias = cfg["roles"][role]
    blocks = re.findall(r'(?m)^  ' + re.escape(alias) + r':\s*\n\s+cmd: >-\s*\n([^\n]+)', text)
    if len(blocks) != 1:
        raise DurableError("SafeQwen gateway alias/command identity unavailable or ambiguous")
    command = blocks[0]
    executable = re.match(r'\s*"([^"\n]+)"', command)
    models = re.findall(r'--model\s+"([^"\n]+)"', command)
    if (executable is None or len(models) != 1
            or not Path(executable[1]).is_absolute() or not Path(models[0]).is_absolute()
            or executable[1].startswith(("\\\\", "//")) or models[0].startswith(("\\\\", "//"))
            or Path(executable[1]).resolve() != Path(resources["binary"]).resolve()
            or Path(models[0]).resolve() != Path(resources["model"]).resolve()):
        raise DurableError("SafeQwen declared model/binary differs from gateway command")
    with Path(cfg["safe_qwen_gateway"]["pid_file"]).open("rb") as stream:
        raw = stream.read(33)
    if len(raw) > 32 or not raw.strip().isdigit():
        raise DurableError("SafeQwen gateway PID unavailable")
    parent_pid = int(raw.strip())
    if not 1 <= parent_pid <= 4294967295:
        raise DurableError("SafeQwen gateway PID invalid")
    parent = supervisor.inspect(parent_pid)
    expected = os.path.normcase(str(Path(cfg["safe_qwen_gateway"]["binary"]).resolve()))
    if not parent or os.path.normcase(parent["executable"]) != expected:
        raise DurableError("SafeQwen gateway process identity cannot be established")
    child_id = supervisor.register("model_server", executable[1], {
        "alias": alias, "profile": cfg.get("review_profile"),
        "rendered_config_sha256": hashlib.sha256(text.encode()).hexdigest()}, "delegated")
    child = supervisor.child(child_id)
    child.update(parent_pid=parent_pid, parent_identity=parent, role=role, alias=alias)
    supervisor.change("model_launch_intent", child)
    return child_id
