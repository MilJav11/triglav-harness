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


def validate_reviewer_policy(cfg):
    """Closed v1 contract; metadata detects declared HotPin, never proves a runtime."""
    resources = cfg.get("safe_qwen_runtime", {})
    review_enabled = type(resources) is dict and "review" in resources
    alias = cfg.get("roles", {}).get("review")
    metadata = cfg.get("model_metadata", {})
    metadata = metadata.get(alias, {}) if type(metadata) is dict else {}
    runtime = metadata.get("runtime") if type(metadata) is dict else None
    declared_hotpin = type(runtime) is str and runtime.strip().casefold() == "hotpin"
    if "safe_qwen_reviewer_policy" not in cfg:
        if review_enabled and declared_hotpin:
            raise ValueError("SafeQwen HotPin reviewer requires an explicit resource policy")
        return None
    policy = cfg["safe_qwen_reviewer_policy"]
    fields = {"version", "type", "hot_experts", "hot_experts_sha256",
              "min_available_ram_gb", "max_working_set_gb"}
    if type(policy) is not dict or set(policy) != fields:
        raise ValueError("Unsupported SafeQwen reviewer resource-policy fields")
    if not review_enabled:
        raise ValueError("SafeQwen reviewer resource policy requires an enabled review runtime")
    if type(policy["version"]) is not int or policy["version"] != 1:
        raise ValueError("Unsupported SafeQwen reviewer resource-policy version")
    if type(policy["type"]) is not str or policy["type"] != "hotpin":
        raise ValueError("Unsupported SafeQwen reviewer resource-policy type")
    for key, expected in (("min_available_ram_gb", 44), ("max_working_set_gb", 32)):
        if type(policy[key]) is not int or policy[key] != expected:
            raise ValueError("SafeQwen HotPin v1 requires 44 GiB free RAM and a 32 GiB hard maximum")
    name = policy["hot_experts"]
    if (type(name) is not str or not name.strip() or "\0" in name
            or name.startswith(("\\\\", "//"))):
        raise ValueError("SafeQwen HotPin expert asset must be a local file")
    path = Path(name)
    if path.drive and not path.is_absolute():
        raise ValueError("SafeQwen HotPin expert asset cannot be drive-relative")
    expected = policy["hot_experts_sha256"]
    if type(expected) is not str or re.fullmatch(r"[0-9a-f]{64}", expected) is None:
        raise ValueError("SafeQwen HotPin requires a trusted lowercase SHA-256")
    if cfg.get("review_profile") or cfg.get("review_profiles"):
        raise ValueError("SafeQwen HotPin resource policy cannot select legacy profiles")
    aliases = list(cfg.get("roles", {}).values())
    if len(aliases) != len(set(aliases)):
        raise ValueError("SafeQwen HotPin requires distinct model aliases for each role")
    return policy


def reviewer_expert_identity(cfg):
    """Verify actual expert bytes against the operator's trusted expected digest."""
    from environment import file_identity
    policy = validate_reviewer_policy(cfg)
    if policy is None:
        return None
    path = Path(policy["hot_experts"])
    if not path.is_absolute():
        raise ValueError("SafeQwen HotPin expert path must be resolved against operator configuration")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValueError("SafeQwen HotPin expert asset is missing or unavailable") from exc
    if str(resolved).startswith(("\\\\", "//")):
        raise ValueError("SafeQwen HotPin expert asset must resolve to a local file")
    value = file_identity(resolved)
    if "error" in value:
        raise ValueError("SafeQwen HotPin expert asset is unavailable or changed during read")
    if value["sha256"] != policy["hot_experts_sha256"]:
        raise ValueError("SafeQwen HotPin expert SHA-256 mismatch")
    return value


def configure_reviewer_policy(cfg, base):
    policy = validate_reviewer_policy(cfg)
    if policy is not None:
        path = Path(policy["hot_experts"])
        cfg["safe_qwen_reviewer_policy"] = {**policy, "hot_experts": str(
            (path if path.is_absolute() else base / path).resolve())}
        reviewer_expert_identity(cfg)


def require_reviewer_enforcement(cfg, role=None):
    """Preflight native support and bound expert bytes; live ownership is checked at dispatch."""
    if role not in (None, "review"):
        return
    if validate_reviewer_policy(cfg) is not None:
        import ctypes
        if os.name != "nt" or ctypes.sizeof(ctypes.c_void_p) != 8:
            raise RuntimeError("SafeQwen HotPin enforcement requires native x64 Windows Python")
        reviewer_expert_identity(cfg)


def is_safe_qwen_config(cfg):
    return type(cfg) is dict and (cfg.get("executor") == "safe_qwen" or any(
        key in cfg for key in ("safe_qwen_identity", "safe_qwen_runtime", "safe_qwen_reviewer_policy")))


def gateway_config(gateway):
    """Use Store provenance as well as the executor key; reject divergent live routing."""
    from durable import DurableError
    supervisor = gateway.supervisor
    state = getattr(getattr(supervisor, "store", None), "state", {})
    options = state.get("options", {})
    owned = options.get("config")
    recorded = options.get("work_unit_executor", {}).get("kind")
    if not (is_safe_qwen_config(gateway.config) or is_safe_qwen_config(owned) or recorded == "safe_qwen"):
        return None
    if supervisor is None or owned is None or gateway.config != owned:
        raise DurableError("SafeQwen Gateway differs from the pinned Store configuration")
    validate_pinned(owned)
    return owned


def reviewer_owned_child(gateway, cfg, running, processes):
    """Observe without granting cleanup authority; retain the exact launch child binding."""
    from durable import DurableError
    supervisor = gateway.supervisor
    children = supervisor.reconcile()
    supervisor.require_no_ambiguous_model(children)
    active = [c for c in children if c["purpose"] == "model_server"
              and c["state"] in {"STARTING", "UNKNOWN", "RUNNING", "STOPPING"}]
    if (len(active) != 1 or active[0]["state"] != "RUNNING"
            or active[0]["child_id"] != gateway.active_model_child_id):
        raise DurableError("HotPin reviewer owned process is unavailable or ambiguous")
    child = active[0]
    alias = cfg["roles"]["review"]
    expected = os.path.normcase(str(Path(cfg["safe_qwen_runtime"]["review"]["binary"]).resolve()))
    parent_expected = os.path.normcase(str(Path(cfg["safe_qwen_gateway"]["binary"]).resolve()))
    if (child.get("role") != "review" or child.get("alias") != alias
            or child.get("containment") != "delegated" or child.get("boot") != supervisor.boot()
            or child.get("run_id") != supervisor.store.state["run_id"]
            or not child.get("identity") or not child["identity"].get("created")
            or os.path.normcase(child["executable"]) != expected
            or child["identity"].get("executable") != expected
            or not child.get("parent_identity")
            or child["parent_identity"].get("executable") != parent_expected
            or supervisor.inspect(child["pid"]) != child["identity"]
            or supervisor.inspect(child["parent_pid"]) != child["parent_identity"]
            or len(processes) != 1 or processes[0].get("pid") != child["pid"]
            or processes[0].get("parent_pid") != child["parent_pid"]
            or os.path.normcase(str(Path(processes[0].get("path", "")).resolve())) != expected
            or len(running) != 1 or type(running[0]) is not dict
            or running[0].get("model") != alias or running[0].get("state") != "ready"):
        raise DurableError("HotPin reviewer process/parent/alias identity mismatch")
    return child


def enforce_reviewer(gateway, running):
    """Gate every HotPin review on pinned resources, owned handles and hard-cap readback."""
    import environment
    import winprocess
    from durable import DurableError, current_fingerprint
    cfg = gateway_config(gateway)
    policy = validate_reviewer_policy(cfg) if cfg is not None else None
    if policy is None:
        return
    require_reviewer_enforcement(cfg, "review")
    drift = environment.compare(gateway.supervisor.store.state["environment"],
                                current_fingerprint(gateway.supervisor.store))
    if drift["decision"] in {"UNSAFE", "REVALIDATION_REQUIRED"}:
        raise DurableError("SafeQwen runtime changed before reviewer inference")
    child = reviewer_owned_child(gateway, cfg, running, winprocess.servers())
    inherited = winprocess.hotpin_environment(child["pid"], expected_identity=child["identity"])
    if (type(inherited) is not str or not inherited or inherited.startswith(("\\\\", "//"))
            or not Path(inherited).is_absolute()
            or os.path.normcase(str(Path(inherited).resolve(strict=True))) !=
               os.path.normcase(policy["hot_experts"])):
        raise DurableError("HotPin reviewer did not inherit the bound expert asset")
    reviewer_expert_identity(cfg)
    limit = winprocess.limit_working_set(child["pid"], policy["max_working_set_gb"],
                                        expected_identity=child["identity"])
    if (type(limit) is not dict or limit.get("pid") != child["pid"]
            or type(limit.get("flags")) is not int or not limit["flags"] & 4 or limit["flags"] & 8
            or type(limit.get("maximum_gb")) not in (int, float)
            or not math.isfinite(limit["maximum_gb"]) or not 0 < limit["maximum_gb"] <= 32):
        raise DurableError("HotPin reviewer hard working-set maximum is unproven")
    reviewer_expert_identity(cfg)
    if reviewer_owned_child(gateway, cfg, gateway.running(), winprocess.servers())["identity"] != child["identity"]:
        raise DurableError("HotPin reviewer identity changed during native enforcement")
    gateway.log.emit("hotpin_review_gate", child_id=child["child_id"], pid=child["pid"],
                     process_identity_sha256=digest(child["identity"]),
                     policy_sha256=digest(policy), expert_sha256=policy["hot_experts_sha256"],
                     maximum_gb=limit["maximum_gb"], flags=limit["flags"])


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
    if cfg.get("review_profiles"):
        raise ValueError("SafeQwen does not support implicit HotPin profile deployment; use dedicated runtime configuration")
    if "multi_work_unit" in cfg and not isinstance(cfg["multi_work_unit"], bool):
        raise ValueError("SafeQwen multi_work_unit must be a boolean")
    validate_reviewer_policy(cfg)
    if tool_protocol.TOOLS != ("read_files", "edit_file", "submit"):
        raise ValueError("SafeQwen tool inventory refused")


def identity(worker):
    if type(worker) is not SafeQwenExecutor:
        raise ValueError("Wrong SafeQwen executor type")
    validate_config(worker.config)
    reviewer_expert_identity(worker.config)
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
    reviewer_expert_identity(cfg)
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
    expert = reviewer_expert_identity(cfg)
    if expert is not None:
        value["runtime"]["safe_qwen:review:hot_experts"] = expert
        value["runtime"]["safe_qwen:review:resource_policy"] = {
            "sha256": digest(cfg["safe_qwen_reviewer_policy"])}
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
    cfg = supervisor.store.state["options"]["config"]
    require_reviewer_enforcement(cfg, role)
    supervisor.require_no_ambiguous_model()
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
