"""Pinned OpenCode coding episodes. All output and edits remain untrusted."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
import uuid

VERSION = "1.18.30"
MODEL = "local/llm-code"
MAX_PROMPT = 24000
MAX_EVENT = 262144
# llama.cpp is qualified at 32768. OpenCode 1.18.30 checks LAST response usage,
# not the next request with new tool results. reserve applies only with limit.input.
MODEL_CONTEXT = 32768
MODEL_OUTPUT = 4096
MODEL_INPUT = MODEL_CONTEXT - MODEL_OUTPUT
COMPACTION_RESERVE = 12288  # compact at 16384 before the observed 19223-token large-read transition
COMPACTION_TAIL = 2048
# Git for Windows setup_explicit_git_dir rejects strlen(gitdir) > PATH_MAX - 40.
SNAPSHOT_GITDIR_MAX = 220
SHELL = "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"


def executable(config):
    value = config.get("opencode", {}).get("executable", "opencode")
    found = shutil.which(value)
    if not found:
        from manager import ExecutorError
        raise ExecutorError("Pinned OpenCode executable unavailable", "OPENCODE_START_FAILED")
    return str(Path(found).resolve())


def configuration(config, step, context, repo):
    """Use the qualified provider and explicit permission denies, never --yolo."""
    from manager import normalize_entry
    if config["base_url"].rstrip("/") != "http://127.0.0.1:9292" or config["roles"]["code"] != "llm-code":
        raise ValueError("OpenCode requires qualified localhost:9292 / llm-code routing")
    edits = {"*": "deny"}
    for entry in step["scope"]["allowed_paths"]:
        entry = normalize_entry(entry)
        # OpenCode asks about paths relative to its Git worktree.
        repo.path(entry.rstrip("/"))
        edits[entry + "*" if entry.endswith("/") else entry] = "allow"
    for entry in step["scope"]["forbidden_paths"] + context["scope"]["forbidden_paths"]:
        entry = normalize_entry(entry)
        edits[entry + "*" if entry.endswith("/") else entry] = "deny"
    edits[".git/*"] = "deny"
    commands = {"*": "deny", "git diff*": "allow", "git status*": "allow"}
    for check in context["checks"]:
        # Prefix patterns are permission policy, NOT shell isolation.
        argv = check["argv"]
        prefix = argv[:3] if len(argv) >= 3 and argv[1] == "-m" and argv[2] in {"unittest", "pytest"} else argv
        commands[subprocess.list2cmdline(prefix) + "*"] = "allow"
    return {
        "$schema": "https://opencode.ai/config.json",
        "model": MODEL, "small_model": MODEL, "enabled_providers": ["local"],
        "autoupdate": False, "share": "disabled", "plugin": [], "mcp": {},
        "lsp": False, "formatter": False, "shell": SHELL, "snapshot": True,
        "compaction": {"auto": True, "reserved": COMPACTION_RESERVE,
                       "preserve_recent_tokens": COMPACTION_TAIL},
        "provider": {"local": {"npm": "@ai-sdk/openai-compatible", "name": "Local llama-swap",
            "options": {"baseURL": "http://127.0.0.1:9292/v1", "apiKey": "local", "timeout": 900000},
            "models": {"llm-code": {"name": "Qwen3-Coder-30B-A3B-Instruct-Q4_K_M",
                "tool_call": True, "limit": {"context": MODEL_CONTEXT, "input": MODEL_INPUT,
                                             "output": MODEL_OUTPUT},
                "options": {"temperature": 0.1}}}}},
        "permission": {"*": "deny", "read": "allow", "glob": "allow", "grep": "allow",
            "list": "allow", "edit": edits, "bash": commands, "external_directory": "deny",
            "task": "deny", "webfetch": "deny", "websearch": "deny"}}


def episode_environment(directory, *, data_directory=None):
    from manager import ExecutorError
    data_directory = (data_directory or directory / "xdg-data").resolve()
    # Pinned OpenCode appends opencode/snapshot/<project SHA1>/<worktree SHA1>.
    # Check UTF-8 bytes: Git uses strlen, not Python's character count.
    gitdir = data_directory / "opencode" / "snapshot" / ("0" * 40) / ("0" * 40)
    if len(str(gitdir).encode("utf-8")) > SNAPSHOT_GITDIR_MAX:
        raise ExecutorError("OpenCode snapshot path exceeds Windows Git limit; use a shorter controller evidence path",
                            "OPENCODE_START_FAILED")
    # Do not inherit provider keys, OpenCode overrides, plugins or arbitrary Python hooks.
    keep = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP",
            "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA"}
    env = {k: v for k, v in os.environ.items() if k.upper() in keep}
    for key, folder in (("XDG_CONFIG_HOME", "xdg-config"), ("XDG_DATA_HOME", "xdg-data"),
                        ("XDG_CACHE_HOME", "xdg-cache"), ("XDG_STATE_HOME", "xdg-state"),
                        ("OPENCODE_TEST_HOME", "home"), ("OPENCODE_CONFIG_DIR", "config")):
        path = data_directory if key == "XDG_DATA_HOME" else directory / folder
        path.mkdir(parents=True)
        env[key] = str(path)
    env.update(OPENCODE_CONFIG=str(directory / "config/opencode.json"),
               OPENCODE_DISABLE_AUTOUPDATE="true", OPENCODE_DISABLE_MODELS_FETCH="true",
               OPENCODE_DISABLE_LSP_DOWNLOAD="true", OPENCODE_DISABLE_PROJECT_CONFIG="true",
               PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1",
               PYTEST_ADDOPTS="-p no:cacheprovider")
    return env


def collect_events(stream, summary):
    """Bound JSON lines and retain only metadata. Never interpret model editing actions."""
    while True:
        raw = stream.readline(MAX_EVENT + 1)
        if not raw:
            break
        if len(raw) > MAX_EVENT or not raw.endswith(b"\n"):
            summary["protocol_error"] = True
            while raw and not raw.endswith(b"\n"):
                raw = stream.readline(MAX_EVENT + 1)
            continue
        try:
            event = json.loads(raw)
            kind, session = event["type"], event["sessionID"]
            if not isinstance(session, str) or not re.fullmatch(r"ses_[A-Za-z0-9]+", session):
                raise ValueError()
            if summary["session_id"] not in (None, session):
                raise ValueError()
            summary["session_id"] = session
            summary["events"] += 1
            if kind == "error":
                summary["error"] = True
            if kind == "step_finish":
                summary["finished"] = event.get("part", {}).get("reason") == "stop"
            if kind == "tool_use":
                part = event.get("part", {})
                tool = part.get("tool")
                if tool in {"read", "edit", "write", "bash", "glob", "grep", "list"}:
                    summary["tools"][tool] = summary["tools"].get(tool, 0) + 1
                if tool == "bash":
                    code = part.get("state", {}).get("metadata", {}).get("exit")
                    if type(code) is int:
                        summary["command_exits"] = (summary["command_exits"] + [code])[-32:]
        except (ValueError, KeyError, TypeError, AttributeError):
            summary["protocol_error"] = True


def run_episode_process(spec):
    """Called ONLY inside the identity-gated Windows broker and its kill-on-close job."""
    summary = dict(session_id=None, events=0, finished=False, error=False,
                   protocol_error=False, tools={}, command_exits=[])
    proc = subprocess.Popen(spec["command"], cwd=spec["cwd"], env=spec["env"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, shell=False, close_fds=True)
    def discard():
        while proc.stderr.read(8192):
            pass
    readers = [threading.Thread(target=collect_events, args=(proc.stdout, summary), daemon=True),
               threading.Thread(target=discard, daemon=True)]
    for reader in readers:
        reader.start()
    # Bounded prompt fits the pipe; communicate via writer so blocked stdin cannot defeat timeout.
    def feed():
        try:
            proc.stdin.write(spec["stdin"].encode("utf-8"))
            proc.stdin.close()
        except (OSError, ValueError):
            pass
    threading.Thread(target=feed, daemon=True).start()
    try:
        code = proc.wait(timeout=spec["timeout"])
    except subprocess.TimeoutExpired:
        return None, "forced", summary
    for reader in readers:
        reader.join(timeout=1)
    if any(reader.is_alive() for reader in readers):
        summary["protocol_error"] = True
    return code, "natural", summary


def repository_inventory(repo):
    """Include ignored source files; refuse links or an unbounded candidate tree."""
    from harness import generated_artifact
    from manager import ExecutorError
    files, total = {}, 0
    for base, directories, names in os.walk(repo.root, followlinks=False):
        directories[:] = [n for n in directories if n.lower() != ".git"]
        for name in directories + names:
            relative = (Path(base) / name).relative_to(repo.root).as_posix()
            if relative.lower() == ".git" or generated_artifact(relative):
                continue
            path = repo.path(relative)  # Reparse points/escapes fail closed, including ignored files.
            if path.is_file():
                total += path.stat().st_size
                if total > 256 * 1024 * 1024 or len(files) >= 10000:
                    raise ExecutorError("Use a bounded disposable source checkout (<=10000 files / 256MiB)",
                                        "OPENCODE_START_FAILED")
                with path.open("rb") as stream:
                    files[relative] = hashlib.file_digest(stream, "sha256").hexdigest()
        directories[:] = [n for n in directories
                          if not generated_artifact((Path(base) / n).relative_to(repo.root).as_posix())]
    return files


class OpenCodeExecutor:
    def __init__(self, gateway, config, log, *, runner=None, clock=time.monotonic):
        from supervision import run_command
        self.gateway, self.config, self.log = gateway, config, log
        self.runner, self.clock = runner or run_command, clock
        self.deadline = None

    def set_deadline(self, deadline):
        self.deadline = deadline

    def execute(self, step, context, repo):
        from durable import atomic_write, encoded, snapshot
        from manager import ExecutorError, changed_paths, contract_text
        if os.name != "nt" or not hasattr(repo, "supervisor"):
            raise ExecutorError("OpenCode requires the Windows durable controller", "OPENCODE_START_FAILED")
        if repo.store.directory.resolve().is_relative_to(repo.root):
            raise ExecutorError("Controller evidence must be outside the candidate repository", "OPENCODE_START_FAILED")
        if self.config.get("opencode", {}).get("version", VERSION) != VERSION:
            raise ExecutorError("Only qualified OpenCode " + VERSION + " is supported", "OPENCODE_START_FAILED")
        budget = self.config.get("opencode", {}).get("timeout_seconds", 600)
        if type(budget) is not int or not 1 <= budget <= 1800:
            raise ValueError("OpenCode episode timeout must be 1..1800 seconds")
        deadline = min(self.deadline if self.deadline is not None else float("inf"), self.clock() + budget)
        def remaining():
            value = deadline - self.clock()
            if value <= 0:
                raise ExecutorError("OpenCode episode deadline elapsed", "OPENCODE_TIMEOUT")
            return value
        binary = executable(self.config)
        settings = configuration(self.config, step, context, repo)
        prompt = contract_text(step, context) + (
            "\nInspect the candidate and perform the coding/test/repair loop within this bounded step. "
            "Use grep to locate relevant symbols. Read source in chunks: always supply an explicit read limit "
            "of at most 120 lines and an offset; avoid default whole-file reads. Consume each chunk before "
            "requesting more source, so the local 32768-token context remains useful through compaction. "
            "Your completion is candidate-only; the controller independently verifies and decides trust.")
        if len(prompt) > MAX_PROMPT:
            raise ExecutorError("Execution contract exceeds bounded input", "OPENCODE_PROTOCOL_ERROR")
        episode = uuid.uuid4().hex
        directory = repo.store.directory / "opencode" / episode
        directory.mkdir(parents=True)
        # Keep runtime data episode-local and outside the candidate, but avoid the
        # long evidence path plus OpenCode's two 40-character snapshot hashes.
        env = episode_environment(directory, data_directory=repo.store.directory / "oc" / episode[:16])
        atomic_write(directory / "config/opencode.json", encoded(settings))
        atomic_write(directory / "prompt.txt", prompt.encode("utf-8"))
        before = snapshot(repo)
        inventory = repository_inventory(repo)
        self.log.status("opencode_status", state="starting")
        self.log.emit("opencode_episode_start", episode_id=episode, fresh_session=True,
                      version=VERSION, model=MODEL, prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
                      timeout_seconds=round(remaining(), 3))
        try:
            pin = self.runner(repo.supervisor, [binary, "--version"], repo.root,
                              min(15, remaining()), 100, progress=self.log, purpose="opencode_executor")
            if pin["exit_code"] != 0 or pin["stdout"].strip() != VERSION:
                raise ExecutorError("OpenCode executable does not match qualified version " + VERSION,
                                    "OPENCODE_START_FAILED")
            # Existing Gateway owns startup, RAM, model identities and one-model-at-a-time policy.
            self.gateway.switch("code")
            remaining()
            self.log.status("opencode_status", state="running")
            result = self.runner(repo.supervisor,
                [binary, "run", "--pure", "--format", "json", "--model", MODEL],
                repo.root, remaining(), 0, progress=self.log, purpose="opencode_executor",
                env=env, stdin=prompt, opencode=True)
        except OSError:
            raise ExecutorError("OpenCode process could not start", "OPENCODE_START_FAILED") from None
        except RuntimeError as exc:
            if isinstance(exc, ExecutorError):
                raise
            raise RuntimeError("PROCESS_CLEANUP_FAILED: OpenCode ownership/receipt could not be established") from exc
        after = snapshot(repo)
        observed = repository_inventory(repo)
        from manager import path_permitted
        changes = sorted(p for p in inventory.keys() | observed.keys() if inventory.get(p) != observed.get(p))
        violations = [p for p in changes if not path_permitted(p, step["scope"], context["scope"]["forbidden_paths"])
                      or (p not in before["files"] and p not in after["files"])]
        # Ignored source changes cannot be bound to the existing trusted Git snapshot, even if allowed.
        changed = bool(changed_paths(before, after))
        metadata = result.get("metadata", {})
        classification = ("OPENCODE_START_FAILED" if result.get("termination") == "launch_failed" else
                          "OPENCODE_TIMEOUT" if result.get("termination") == "forced" else
                          "OPENCODE_EXIT_ERROR" if result["exit_code"] != 0 else
                          "OPENCODE_PROTOCOL_ERROR" if metadata.get("protocol_error") or metadata.get("error")
                              or not metadata.get("finished") or not metadata.get("session_id") else
                          "EXECUTOR_CHANGED" if changed else "EXECUTOR_NO_CHANGE")
        receipt = dict(episode_id=episode, classification=classification, exit_code=result["exit_code"],
                       changed=changed, metadata=metadata, trusted=False, scope_violations=violations[:20])
        repo.store.artifact("opencode/" + episode + "/result.json", receipt)
        self.log.emit("opencode_episode_end", **receipt)
        self.log.status("opencode_status", state="completed", classification=classification)
        if violations:
            raise ExecutorError("Changed paths outside the step scope (restore them): " + ", ".join(violations[:10]),
                                "SCOPE_VIOLATION")
        if classification.startswith("OPENCODE_"):
            raise ExecutorError("OpenCode episode terminated: " + classification, classification)
        if any(before[k] != after[k] for k in ("head", "branch", "git_identity", "index_sha256")):
            raise RuntimeError("Repository identity mismatch after OpenCode episode; candidate remains untrusted")
        return receipt
