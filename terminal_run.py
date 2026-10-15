"""Portable, fixed-WorkUnit SafeQwen launcher behind the public autonomous CLI.

Importing this module never starts a gateway, model or process. Gateway deployment
belongs to the operator; model lifecycle belongs to the existing Supervisor.
"""
import copy
from contextlib import nullcontext
from types import SimpleNamespace
import json
from pathlib import Path
import shlex

from integration_shim import SafeQwenExecutor
from safe_qwen_worker import Policy, ToolSession


def read_policy(path):
    from tool_protocol import Denied
    def unique(pairs):
        out = {}
        for k, v in pairs:
            if k in out:
                raise ValueError("Duplicate SafeQwen policy field")
            out[k] = v
        return out
    def nonfinite(_):
        raise ValueError("Nonfinite SafeQwen policy value")
    with Path(path).open("rb") as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError("SafeQwen policy too large")
    data = json.loads(raw, object_pairs_hook=unique, parse_constant=nonfinite)
    if type(data) is not dict or set(data) != {"readable", "writable", "mode", "max_actions", "unit_timeout_seconds"}:
        raise ValueError("Unsupported SafeQwen policy fields")
    for key in ("readable", "writable"):
        if type(data[key]) is not list or not all(type(x) is str for x in data[key]):
            raise ValueError("SafeQwen policy paths must be lists of exact file names")
    if not data["readable"] or len(data["readable"]) > 128:
        raise ValueError("SafeQwen requires 1..128 readable files")
    if data["mode"] not in ("read_only", "mutation") or (data["mode"] == "read_only" and data["writable"]):
        raise ValueError("Invalid SafeQwen execution mode")
    if type(data["max_actions"]) is not int or not 1 <= data["max_actions"] <= 32:
        raise ValueError("Invalid SafeQwen action bound")
    if type(data["unit_timeout_seconds"]) is not int or not 1 <= data["unit_timeout_seconds"] <= 600:
        raise ValueError("Invalid SafeQwen unit deadline")
    return data


def configure_worker(config, repository, data, config_path, *, critic=False, reviewer=False):
    from safe_executor_binding import configure_reviewer_policy, identity, validate_config
    cfg = copy.deepcopy(config)
    validate_config(cfg)
    if reviewer and not critic:
        raise ValueError("SafeQwen reviewer requires critic; reviewer-only WorkUnits are unsupported")
    active = {"code"} | ({"critic"} if critic else set()) | ({"review"} if reviewer else set())
    resources = cfg.get("safe_qwen_runtime")
    if type(resources) is not dict or set(resources) != active:
        raise ValueError("Declare runtime files for exactly the enabled roles")
    base = Path(config_path).resolve().parent
    configure_reviewer_policy(cfg, base)
    for role, files in resources.items():
        if role not in cfg["roles"] or type(files) is not dict or set(files) != {"binary", "model", "gateway_config"}:
            raise ValueError("Each enabled role requires binary, model and gateway_config identities")
        for kind, name in files.items():
            if type(name) is not str or not name or name.startswith(("\\\\", "//")):
                raise ValueError("Runtime resources must be local files")
            path = Path(name)
            path = path if path.is_absolute() else base / path
            if not path.is_file():
                raise ValueError("Configured runtime resource is missing")
            files[kind] = str(path.resolve(strict=True))
    # HotPin profile templates belong to a separate, explicitly provisioned route.
    if cfg.get("review_profiles"):
        raise ValueError("SafeQwen does not support implicit HotPin profile deployment; use dedicated runtime configuration")
    gateway = cfg.get("safe_qwen_gateway")
    if type(gateway) is not dict or set(gateway) != {"binary", "pid_file"}:
        raise ValueError("SafeQwen requires gateway binary and operator-owned PID file")
    for key, name in gateway.items():
        if type(name) is not str or not name or name.startswith(("\\\\", "//")):
            raise ValueError("Gateway ownership resources must be local files")
        path = Path(name)
        path = path if path.is_absolute() else base / path
        if not path.is_file():
            raise ValueError("Gateway ownership resource is missing")
        gateway[key] = str(path.resolve(strict=True))
    if len({files["gateway_config"] for files in resources.values()}) != 1:
        raise ValueError("All roles must use the same dedicated gateway configuration")
    cfg.pop("review_profiles", None)
    cfg["safe_qwen_policy"] = copy.deepcopy(data)
    readable, writable = tuple(data["readable"]), tuple(data["writable"])
    policy = Policy(Path(repository), readable, writable,
                    tuple(p for p in readable if p not in writable), readable)
    ToolSession(policy, audit=lambda record: None, max_actions=data["max_actions"])
    worker = SafeQwenExecutor(policy, cfg, max_actions=data["max_actions"])
    cfg["safe_qwen_identity"] = identity(worker)
    return worker, cfg


def fixed_unit(task, cfg, worker, verifier_ids):
    data = cfg["safe_qwen_policy"]
    return {"unit_id": "safe-qwen-task", "objective": task, "dependencies": [], "mode": data["mode"],
            "scope": {"allowed_paths": list(worker.policy.writable if data["mode"] == "mutation" else worker.policy.readable),
                      "forbidden_paths": list(worker.policy.frozen) if data["mode"] == "mutation" else []},
            "verifier_ids": verifier_ids, "evidence_inputs": [],
            "limits": {"max_attempts": 1, "timeout_seconds": data["unit_timeout_seconds"]}}


def fixed_two_units(task1, vids1, task2, vids2, cfg, worker, *, id1="unit-1-multiply", id2="unit-2-scale"):
    data = cfg["safe_qwen_policy"]
    timeout = data["unit_timeout_seconds"]
    scope = {
        "allowed_paths": list(worker.policy.writable if data["mode"] == "mutation" else worker.policy.readable),
        "forbidden_paths": list(worker.policy.frozen if data["mode"] == "mutation" else []),
    }
    unit1 = {
        "unit_id": id1,
        "objective": task1,
        "dependencies": [],
        "mode": data["mode"],
        "scope": copy.deepcopy(scope),
        "verifier_ids": list(vids1),
        "evidence_inputs": [],
        "limits": {"max_attempts": 1, "timeout_seconds": timeout},
    }
    unit2 = {
        "unit_id": id2,
        "objective": task2,
        "dependencies": [id1],
        "mode": data["mode"],
        "scope": copy.deepcopy(scope),
        "verifier_ids": list(vids2),
        "evidence_inputs": [],
        "limits": {"max_attempts": 1, "timeout_seconds": timeout},
    }
    return [unit1, unit2]


def validate_safe_qwen_unit_contract(unit_spec, cfg, worker, store=None):
    from manager import ExecutorError
    if not cfg.get("multi_work_unit"):
        expected = fixed_unit(unit_spec.get("objective"), cfg, worker, unit_spec.get("verifier_ids"))
        if (unit_spec.get("unit_id") != expected["unit_id"] or unit_spec.get("mode") != expected["mode"]
                or unit_spec.get("limits") != expected["limits"]
                or any(sorted(unit_spec.get("scope", {}).get(key, [])) != sorted(expected["scope"][key])
                       for key in ("allowed_paths", "forbidden_paths"))):
            raise ExecutorError("SafeQwen fixed WorkUnit contract mismatch", "CONTRACT_MISMATCH")
        return

    import durable
    if not isinstance(store, durable.Store):
        raise ExecutorError("SafeQwen multi-unit execution requires genuine durable Store authority", "CONTRACT_MISMATCH")
    store_dir = getattr(store, "directory", None)
    if not isinstance(store_dir, Path) or not store_dir.is_dir():
        raise ExecutorError("SafeQwen multi-unit execution requires valid Store directory", "CONTRACT_MISMATCH")
    run_id = store_dir.name
    if not isinstance(store.state, dict) or store.state.get("run_id") != run_id:
        raise ExecutorError("Store run identity mismatch", "CONTRACT_MISMATCH")
    if store.state.get("target_repository") != str(worker.policy.root):
        raise ExecutorError("Store repository binding mismatch", "CONTRACT_MISMATCH")

    try:
        auth_store = durable.Store(store_dir.parent, run_id)
        auth_store.load()
    except Exception as exc:
        raise ExecutorError(f"Durable Store load/WAL validation failed: {exc}", "CONTRACT_MISMATCH") from exc

    if durable.encoded(store.state) != durable.encoded(auth_store.state):
        raise ExecutorError("In-memory store state disagrees with durable WAL authority", "CONTRACT_MISMATCH")
    if len(store.records) != len(auth_store.records) or (store.records and store.records[-1]["sha256"] != auth_store.records[-1]["sha256"]):
        raise ExecutorError("In-memory store records disagree with durable WAL ledger", "CONTRACT_MISMATCH")

    wu_section = auth_store.state.get("work_units")
    if not isinstance(wu_section, dict):
        raise ExecutorError("SafeQwen multi-unit execution requires registered work_units in Store", "CONTRACT_MISMATCH")
    sequence = wu_section.get("sequence", [])
    if len(sequence) != 2 or len(set(sequence)) != 2:
        raise ExecutorError("SafeQwen multi-unit mode requires exactly two distinct registered units", "CONTRACT_MISMATCH")
    uid = unit_spec.get("unit_id")
    if uid not in sequence:
        raise ExecutorError(f"SafeQwen unit {uid!r} is not in authorized run sequence", "CONTRACT_MISMATCH")

    registered_unit = wu_section.get("units", {}).get(uid)
    if not registered_unit or not registered_unit.get("spec"):
        raise ExecutorError(f"SafeQwen unit {uid!r} missing spec in run plan", "CONTRACT_MISMATCH")
    planned = registered_unit["spec"]

    limits = unit_spec.get("limits", {})
    if limits.get("max_attempts") != 1:
        raise ExecutorError("SafeQwen units limited to single attempt (max_attempts=1)", "CONTRACT_MISMATCH")
    data = cfg.get("safe_qwen_policy", {})
    if limits.get("timeout_seconds", 0) > data.get("unit_timeout_seconds", 600):
        raise ExecutorError("SafeQwen unit timeout exceeds policy deadline", "CONTRACT_MISMATCH")

    if (unit_spec.get("objective") != planned.get("objective")
            or unit_spec.get("mode") != planned.get("mode")
            or unit_spec.get("dependencies") != planned.get("dependencies")
            or unit_spec.get("verifier_ids") != planned.get("verifier_ids")
            or unit_spec.get("limits") != planned.get("limits")
            or sorted(unit_spec.get("scope", {}).get("allowed_paths", [])) != sorted(planned.get("scope", {}).get("allowed_paths", []))
            or sorted(unit_spec.get("scope", {}).get("forbidden_paths", [])) != sorted(planned.get("scope", {}).get("forbidden_paths", []))):
        raise ExecutorError("SafeQwen WorkUnit spec mismatch against authorized run plan", "CONTRACT_MISMATCH")

    if unit_spec.get("mode") != data.get("mode"):
        raise ExecutorError("SafeQwen unit mode mismatch with policy", "CONTRACT_MISMATCH")
    allowed = unit_spec.get("scope", {}).get("allowed_paths", [])
    forbidden = unit_spec.get("scope", {}).get("forbidden_paths", [])
    expected_allowed = worker.policy.writable if data.get("mode") == "mutation" else worker.policy.readable
    if not set(allowed).issubset(set(expected_allowed)):
        raise ExecutorError("SafeQwen unit allowed paths exceed worker policy scope", "CONTRACT_MISMATCH")
    if not set(worker.policy.frozen).issubset(set(forbidden)):
        raise ExecutorError("SafeQwen unit unfreezes worker policy frozen paths", "CONTRACT_MISMATCH")

    if uid == sequence[0]:
        if unit_spec.get("dependencies") != []:
            raise ExecutorError("First SafeQwen unit must have empty dependencies", "CONTRACT_MISMATCH")
    elif uid == sequence[1]:
        if unit_spec.get("dependencies") != [sequence[0]]:
            raise ExecutorError(f"Second SafeQwen unit must depend strictly on {sequence[0]!r}", "CONTRACT_MISMATCH")
        prereq = wu_section.get("units", {}).get(sequence[0])
        if not prereq or prereq.get("status") != "UNIT_VERIFIED":
            raise ExecutorError(f"Prerequisite unit {sequence[0]!r} must be UNIT_VERIFIED in durable state before {uid!r} execution", "CONTRACT_MISMATCH")

    v_registry = auth_store.state.get("verifier_registry") or auth_store.state.get("options", {}).get("verifier_registry") or {}
    vids = unit_spec.get("verifier_ids", [])
    if not vids or not all(vid in v_registry for vid in vids):
        raise ExecutorError("SafeQwen unit verifier IDs must be registered in verifier registry", "CONTRACT_MISMATCH")


def agents_factory(worker):
    def factory(repo, gateway, cfg, log):
        from manager import Agents
        worker.bind_gateway(gateway)
        worker.log = log
        return Agents(None, worker, None, None)
    return factory


def resume(store, args):
    import manager
    from safe_executor_binding import restore_worker
    cfg = store.state["options"]["config"]
    options = store.state["options"]
    if getattr(args, "reviewer", False) and not options.get("reviewer"):
        raise ValueError("SafeQwen role identity is pinned; enabling reviewer requires a new run")
    worker = restore_worker(cfg)
    manager.prepare_resume(store, revalidate_environment=args.revalidate_environment,
                           budgets=work_unit_budgets(args, cfg), executor=worker)
    # Even a completed reload goes through the real terminal checkpoint/environment gates.
    return execute_bound(store, worker, progress=not args.quiet)


def cli(args, root, config):
    import manager
    from durable import Store, exclusive_lock
    from harness import Repository, safe_command, verification_timeout
    if not getattr(args, "safeqwen_policy", None):
        raise ValueError("--executor safe_qwen requires --safeqwen-policy")
    data = read_policy(args.safeqwen_policy)
    worker, cfg = configure_worker(config, args.repo, data, args.config, critic=bool(args.critic), reviewer=args.reviewer)
    if (root / "runs").resolve().is_relative_to(worker.policy.root):
        raise ValueError("SafeQwen controller Store must be outside the target repository")
    if sorted(args.allow) != sorted(worker.policy.writable if data["mode"] == "mutation" else worker.policy.readable):
        raise ValueError("--allow must match the SafeQwen policy's exact effective file scope")
    if sorted(args.forbid) != sorted(worker.policy.frozen if data["mode"] == "mutation" else ()):
        raise ValueError("--forbid must match the SafeQwen policy's frozen file partition")
    if args.review_policy == "three-model" and not (args.critic and args.reviewer):
        raise ValueError("three-model requires critic and reviewer")
    commands = [json.loads(s) if s.lstrip().startswith("[") else shlex.split(s) for s in args.verify]
    if not commands or not all(safe_command(c) for c in commands):
        raise ValueError("Every verifier must be on the existing safe command allowlist")
    budgets = manager.resolve_budgets(work_unit_budgets(args, cfg), cfg)
    scope = {"allowed_paths": args.allow, "forbidden_paths": args.forbid}
    # Dry preflight emits no durable evidence and never constructs a Gateway.
    preflight_log = SimpleNamespace(check_ids={}, emit=lambda *a, **k: None,
                                    context_for=lambda **k: nullcontext(),
                                    operation=lambda *a, **k: nullcontext())
    repo = Repository(args.repo, cfg, preflight_log)
    repo.require_clean()
    summary = manager.dry_run_summary(commands, args.acceptance, budgets, scope,
                                      bool(args.critic), bool(args.reviewer), args.review_policy)
    registry = {f"check-{i}": {"argv": command, "timeout_seconds": verification_timeout(cfg)}
                for i, command in enumerate(commands, 1)}
    unit = fixed_unit(args.task, cfg, worker, list(registry))
    import work_units
    work_units.validate_unit_plan([unit], parent_scope=scope, verifier_registry=registry)
    summary["policy"] = {"work_unit": "operator-authored fixed scope; one worker attempt; deterministic verification",
                         "critic": "enabled" if args.critic else "disabled",
                         "reviewer": "enabled" if args.reviewer else "disabled",
                         "acceptance": "controller checkpoint after critic, reviewer and fresh final verification"
                         if args.critic and args.reviewer else "intermediate only; no trusted checkpoint"}
    if "safe_qwen_reviewer_policy" in cfg:
        summary["reviewer_resource_policy"] = {
            **cfg["safe_qwen_reviewer_policy"], "native_enforcement": "required_before_inference",
            "dispatch": "subject_to_live_native_gates"}
    if args.dry_run:
        return {**summary, "executor": "safe_qwen", "safe_qwen_identity": cfg["safe_qwen_identity"],
                "verification_timeout_seconds": verification_timeout(cfg)}
    from safe_executor_binding import require_reviewer_enforcement
    require_reviewer_enforcement(cfg)
    runs = root / "runs"
    store = Store(runs, manager.datetime_id())
    with exclusive_lock(runs / ".durable-controller.lock"):
        manager.create_run(store, repo, args.task, commands, cfg, criteria=args.acceptance,
                           critic=bool(args.critic), reviewer=args.reviewer, review_policy=args.review_policy,
                           budgets=work_unit_budgets(args, cfg), allow=args.allow, forbid=args.forbid,
                           config_path=args.config, work_units=[unit], verifier_registry=registry, executor=worker)
        print(f"Autonomous run: {store.state['run_id']}", flush=True)
        return execute_bound(store, worker, progress=not args.quiet)


def execute_bound(store, worker, *, progress=False):
    import manager
    from durable import DurableError
    from supervision import Supervisor
    from safe_executor_binding import require_reviewer_enforcement
    require_reviewer_enforcement(store.state["options"]["config"])
    result = manager.execute(store, agents=agents_factory(worker), progress=progress)
    if result.get("stop", {}).get("cleanup") != "passed":
        raise DurableError("SafeQwen controller cleanup unproven; inspect retained state before continuing")
    Supervisor(store).require_idle()
    return result


def work_unit_budgets(args, cfg):
    """Do not silently accept legacy round/model counters on the fixed-unit route."""
    import manager
    values = manager.budget_args(args)
    unsupported = {key for key, value in values.items() if value is not None and key != "max_runtime_seconds"}
    configured = cfg.get("manager", {}).get("budgets", {})
    if unsupported or set(configured) - {"max_runtime_seconds"}:
        raise ValueError("SafeQwen supports only --max-runtime-minutes from Manager budget flags; use fixed unit policy bounds")
    return {"max_runtime_seconds": values.get("max_runtime_seconds")}
