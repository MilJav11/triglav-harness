"""Live bounded WorkUnit executor adapter for Cline CLI and local Qwen model.

Wave 4 architecture:
- Adapts the untrusted Cline CLI worker to the Wave 2 WorkUnit executor interface.
- Worker packet contains bounded instructions and scope constraints.
- Worker output is strictly untrusted and bounded.
- Controller owns process execution, Windows job containment, and deterministic verification.
- Output text from worker has ZERO trust value and cannot produce UNIT_VERIFIED.
"""

from __future__ import annotations

import codecs
import collections
import contextlib
import copy
import ctypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import uuid

import winprocess
from manager import ExecutorError

# --------------------------------------------------------------------------- constants

DEFAULT_CLINE_EXECUTOR = r"C:\AI\tools\cline-cli-3.0.68\package\bin\cline.exe"
DEFAULT_MODEL = "llm-code"
DEFAULT_PROVIDER = "local-qwen"
DEFAULT_BASE_URL = "http://127.0.0.1:9292/v1"
DEFAULT_API_KEY = "local"

MAX_OUTPUT_CHARS = 16384
MAX_PROMPT_CHARS = 24000


# --------------------------------------------------------------------------- helpers

def cline_executable(config: dict | None = None) -> str | None:
    """Resolve the Cline executable path from config, default path, or PATH."""
    config = config or {}
    custom = config.get("cline", {}).get("executable") or config.get("executable")
    if custom:
        found = shutil.which(custom) or (Path(custom).resolve() if Path(custom).exists() else None)
        return str(found) if found else None
    if Path(DEFAULT_CLINE_EXECUTOR).exists():
        return str(Path(DEFAULT_CLINE_EXECUTOR).resolve())
    found = shutil.which("cline")
    return str(Path(found).resolve()) if found else None


def normalize_base_url(url: str | None) -> str:
    """Normalize base URL so /v1 is deterministically present when targeting OpenAI-compatible gateway."""
    if not url:
        return DEFAULT_BASE_URL
    u = url.rstrip("/")
    if not u.endswith("/v1"):
        u = f"{u}/v1"
    return u


def generate_providers_json(config: dict | None = None) -> dict:
    """Generate the providers.json configuration payload for Cline."""
    config = config or {}
    cline_cfg = config.get("cline", {})
    provider = cline_cfg.get("provider", config.get("provider", DEFAULT_PROVIDER))
    model = (
        cline_cfg.get("model")
        or config.get("model")
        or config.get("roles", {}).get("code")
        or DEFAULT_MODEL
    )
    raw_url = cline_cfg.get("base_url", config.get("base_url", DEFAULT_BASE_URL))
    base_url = normalize_base_url(raw_url)
    api_key = cline_cfg.get("api_key", config.get("api_key", DEFAULT_API_KEY))

    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    return {
        "version": 1,
        "lastUsedProvider": provider,
        "modes": {},
        "providers": {
            provider: {
                "settings": {
                    "provider": provider,
                    "apiKey": api_key,
                    "model": model,
                    "protocol": "openai-chat",
                    "client": "openai-compatible",
                    "maxTokens": 4096,
                    "contextWindow": 28672,
                    "baseUrl": base_url,
                    "timeout": 180000,
                    "reasoning": {
                        "enabled": False,
                        "effort": "none",
                    },
                    "modelCatalog": {
                        "loadLatestOnInit": False,
                        "includeClineCloudModels": False,
                        "loadPrivateOnAuth": False,
                    },
                },
                "updatedAt": now_iso,
                "tokenSource": "manual",
            }
        },
    }


def generate_models_json(config: dict | None = None) -> dict:
    """Generate the models.json configuration payload for Cline."""
    config = config or {}
    cline_cfg = config.get("cline", {})
    provider = cline_cfg.get("provider", config.get("provider", DEFAULT_PROVIDER))
    model = (
        cline_cfg.get("model")
        or config.get("model")
        or config.get("roles", {}).get("code")
        or DEFAULT_MODEL
    )
    raw_url = cline_cfg.get("base_url", config.get("base_url", DEFAULT_BASE_URL))
    base_url = normalize_base_url(raw_url)
    return {
        "version": 1,
        "providers": {
            provider: {
                "models": {
                    model: {
                        "id": model,
                        "name": "Local Qwen coding model",
                        "contextWindow": 32768,
                        "maxInputTokens": 28672,
                        "maxTokens": 4096,
                        "temperature": 0.1,
                        "supportsReasoning": False,
                        "supportsVision": False,
                        "capabilities": [
                            "tools",
                            "streaming",
                        ],
                        "inputPrice": 0,
                        "outputPrice": 0,
                    }
                },
                "provider": {
                    "name": "Isolated local Qwen",
                    "baseUrl": base_url,
                    "defaultModelId": model,
                    "client": "openai-compatible",
                    "protocol": "openai-chat",
                    "capabilities": [
                        "tools",
                        "streaming",
                    ],
                },
            }
        },
    }


def generate_feature_flags_json() -> dict:
    """Generate offline feature-flags.json preventing unwanted network onboarding."""
    return {
        "version": 2,
        "updatedAt": int(time.time() * 1000),
        "userId": None,
        "flagsPayload": {
            "featureFlags": {
                "ext-cline-pass": True,
                "code-onboarding-github": False,
                "CLINE_COMPOSIO_BETA": True,
                "code-cloud-agents": False,
            },
            "featureFlagPayloads": {},
        },
    }


def build_worker_prompt(unit_spec: dict, context: dict, *, remaining_seconds: float | None = None) -> str:
    """Construct a deterministic, controller-owned worker prompt packet.

    Contains only what Cline/Qwen needs:
    - canonical unit_id
    - objective
    - mode
    - allowed paths/scope
    - forbidden paths
    - attempt number
    - bounded remaining runtime
    - concise implementation constraints
    - verifier descriptions (informational only)
    - deterministic repair failure packet on attempt 2

    Does NOT expose trusted fields, checkpoints, or authority to alter verifiers.
    """
    uid = unit_spec["unit_id"]
    objective = unit_spec["objective"]
    mode = unit_spec.get("mode", "mutation")
    allowed = unit_spec.get("scope", {}).get("allowed_paths", [])
    forbidden = unit_spec.get("scope", {}).get("forbidden_paths", [])
    attempt_num = context.get("attempt_number", 1)
    repair_evidence = context.get("repair_evidence")

    lines = [
        f"=== WORKUNIT TASK PACKET ===",
        f"Unit ID: {uid}",
        f"Attempt: {attempt_num} of 2",
        f"Mode: {mode}",
        f"Objective: {objective}",
    ]

    if remaining_seconds is not None:
        lines.append(f"Remaining Time Budget: {remaining_seconds:.1f} seconds")

    lines.append("\n=== SCOPE CONSTRAINTS ===")
    lines.append(f"Allowed Paths (modifications permitted ONLY here): {allowed}")
    lines.append(f"Forbidden Paths (MUST NEVER touch or modify): {forbidden}")

    lines.append("\n=== IMPLEMENTATION CONSTRAINTS ===")
    lines.append("1. Perform ONLY the implementation for this specific WorkUnit.")
    lines.append("2. Modify ONLY files within the allowed paths.")
    lines.append("3. NEVER touch, create, or alter files in forbidden paths or outside allowed scope.")
    lines.append("4. Make the minimal, correct code edits needed to satisfy the objective.")
    lines.append("5. Do not modify unrelated repository files or expand the task scope.")
    lines.append("6. Do not claim task completion, mark the unit verified, or create checkpoints.")
    lines.append("7. Do not touch harness, controller, or orchestration metadata.")
    lines.append("8. Verification is performed independently by the controller after you exit.")
    lines.append("9. When code changes for this attempt are complete, stop immediately.")

    # Verifier descriptions (informational only, no raw commands or authority)
    verifiers = unit_spec.get("verifier_ids", [])
    if verifiers:
        lines.append(f"\nRegistered Verifiers: {verifiers} (will run deterministically after exit)")

    # Repair failure packet (ONLY on repair attempt, attempt 2)
    if attempt_num > 1 and repair_evidence:
        lines.append("\n=== REPAIR ATTEMPT DETAILS ===")
        lines.append("Previous attempt failed. Controller failure diagnostics:")
        lines.append(f"- Outcome: {repair_evidence.get('outcome')}")
        lines.append(f"- Failure Classification: {repair_evidence.get('failure_classification')}")
        if repair_evidence.get("changed_paths"):
            lines.append(f"- Changed paths detected by controller: {repair_evidence.get('changed_paths')}")
        v_res = repair_evidence.get("verifier_result")
        if isinstance(v_res, dict):
            for vid, vdata in v_res.items():
                if isinstance(vdata, dict) and not vdata.get("passed"):
                    lines.append(f"- Verifier {vid} failed (exit code {vdata.get('exit_code')})")
                    v_out = (vdata.get("stderr") or vdata.get("stdout") or "")[:500]
                    if v_out:
                        lines.append(f"  Verifier output: {v_out.strip()}")
        lines.append("Fix the implementation to address the deterministic failure while strictly respecting scope.")

    prompt_text = "\n".join(lines)
    if len(prompt_text) > MAX_PROMPT_CHARS:
        raise ExecutorError("Worker prompt exceeds maximum bounded length", "PROMPT_TOO_LARGE")

    return prompt_text


# --------------------------------------------------------------------------- scoped process execution

def _stream_pipe_reader(stream, max_chars: int) -> tuple[str, bool]:
    """Read a pipe stream with character-based truncation and strictly bounded memory (Fix 7, Wave 4 Repair).

    Decodes UTF-8 incrementally, tracks exact decoded character count, and maintains
    a character-based tail bounded to max_chars characters.
    """
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    chunk_size = 4096
    char_chunks = collections.deque()
    retained_chars = 0
    total_chars = 0

    try:
        while True:
            data = stream.read(chunk_size)
            if not data:
                break
            text_chunk = decoder.decode(data)
            if text_chunk:
                total_chars += len(text_chunk)
                char_chunks.append(text_chunk)
                retained_chars += len(text_chunk)
                while retained_chars - len(char_chunks[0]) >= max_chars:
                    retained_chars -= len(char_chunks.popleft())
        final_chunk = decoder.decode(b"", final=True)
        if final_chunk:
            total_chars += len(final_chunk)
            char_chunks.append(final_chunk)
            retained_chars += len(final_chunk)
            while retained_chars - len(char_chunks[0]) >= max_chars:
                retained_chars -= len(char_chunks.popleft())
    finally:
        try:
            stream.close()
        except Exception:
            pass

    full_retained = "".join(char_chunks)
    tail = full_retained[-max_chars:] if len(full_retained) > max_chars else full_retained
    truncated = total_chars > max_chars
    return tail, truncated


@contextlib.contextmanager
def _hook_create_process(hook_fn):
    """Context manager for temporarily hooking _winapi.CreateProcess on Windows (Wave 4 Repair).

    Guarantees restoration of the original _winapi.CreateProcess on EVERY exit path,
    including exceptions during setup, Popen, containment failure, timeout, or interruption.
    Valid under the current sequential single-worker invariant.
    """
    if os.name != "nt":
        yield
        return
    import _winapi
    orig = _winapi.CreateProcess
    try:
        _winapi.CreateProcess = hook_fn
        yield
    finally:
        _winapi.CreateProcess = orig


def run_scoped_process(
    cmd: list[str], cwd: str | Path, timeout: float, *, env: dict | None = None,
    stdin: str | bytes | None = None, max_output_chars: int = MAX_OUTPUT_CHARS,
) -> dict:
    """Own the suspended worker, Job, pipes and readers through every exit path."""
    h_job = None
    k = None
    proc = None
    raw_process = raw_thread = None
    readers = []
    stdout_res, stderr_res = [], []
    start_time = time.monotonic()
    timed_out = cleanup_invoked = False
    failure = None
    cleanup_errors = []
    phase = "containment"

    def terminate_tree():
        nonlocal cleanup_invoked
        cleanup_invoked = True
        errors = []
        if h_job is not None:
            try:
                if not k.TerminateJobObject(h_job, 137):
                    raise ExecutorError("TerminateJobObject failed", "CLEANUP_FAILED")
                winprocess._wait_job_empty(k, h_job, timeout=5)
            except BaseException as exc:
                errors.append(exc)
        # Ownership transfers from the raw suspended handle to Popen. In both
        # phases, only a signaled native handle proves Windows termination.
        # Popen.kill() can cache an exit code during pending Job termination;
        # Popen.wait() then skips the native wait even while the process is alive.
        process_handle = raw_process
        if process_handle is None and proc is not None and os.name == "nt":
            process_handle = proc._handle
        if process_handle is not None:
            try:
                if k.WaitForSingleObject(process_handle, 0) != 0:
                    # Termination may already be pending (ACCESS_DENIED). Wait
                    # regardless of that request's result before releasing ownership.
                    k.TerminateProcess(process_handle, 137)
                if k.WaitForSingleObject(process_handle, 5000) != 0:
                    raise ExecutorError("Worker process termination not proven", "CLEANUP_FAILED")
            except BaseException as exc:
                errors.append(exc)
        if proc is not None:
            try:
                if os.name != "nt" and proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=5)
            except BaseException as exc:
                errors.append(exc)
        if errors:
            raise errors[0]

    def cleanup_action(action):
        # Keep releasing the remaining resources even if one cleanup action fails.
        try:
            action()
        except BaseException as exc:
            cleanup_errors.append(exc)

    def close_native(handle):
        if not k.CloseHandle(handle):
            raise ExecutorError("CloseHandle failed", "CLEANUP_FAILED")

    try:
        hooked_create_process = None
        if os.name == "nt":
            import _winapi
            from ctypes import wintypes as W
            k = winprocess._kernel()
            k.IsProcessInJob.argtypes = [W.HANDLE, W.HANDLE, ctypes.POINTER(W.BOOL)]
            k.IsProcessInJob.restype = W.BOOL
            k.ResumeThread.argtypes, k.ResumeThread.restype = [W.HANDLE], W.DWORD
            k.TerminateProcess.argtypes = [W.HANDLE, W.UINT]
            h_job = k.CreateJobObjectW(None, None)
            if not h_job:
                h_job = None
                raise ExecutorError("CreateJobObjectW failed", "CONTAINMENT_FAILED")

            class Basic(ctypes.Structure):
                _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                            ("flags", W.DWORD), ("min_ws", ctypes.c_size_t), ("max_ws", ctypes.c_size_t),
                            ("active", W.DWORD), ("affinity", ctypes.c_size_t),
                            ("priority", W.DWORD), ("scheduling", W.DWORD)]
            class Extended(ctypes.Structure):
                _fields_ = [("basic", Basic), ("io", ctypes.c_ulonglong * 6),
                            ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                            ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]
            info = Extended()
            info.basic.flags = 0x2000  # KILL_ON_JOB_CLOSE, no breakaway.
            if not k.SetInformationJobObject(h_job, 9, ctypes.byref(info), ctypes.sizeof(info)):
                raise ExecutorError("SetInformationJobObject failed", "CONTAINMENT_FAILED")
            original = _winapi.CreateProcess

            def hooked_create_process(*args):
                nonlocal raw_process, raw_thread, phase
                args = list(args)
                args[5] |= 0x00000004  # CREATE_SUSPENDED
                raw_process, raw_thread, pid, tid = original(*args)
                phase = "containment"
                if not k.AssignProcessToJobObject(h_job, raw_process):
                    raise ExecutorError("AssignProcessToJobObject failed", "CONTAINMENT_FAILED")
                member = W.BOOL()
                if not k.IsProcessInJob(raw_process, h_job, ctypes.byref(member)) or not member.value:
                    raise ExecutorError("IsProcessInJob verification failed", "CONTAINMENT_FAILED")
                if k.ResumeThread(raw_thread) == 0xFFFFFFFF:
                    raise ExecutorError("ResumeThread failed", "CONTAINMENT_FAILED")
                return raw_process, raw_thread, pid, tid

        phase = "launch"
        with _hook_create_process(hooked_create_process):
            proc = subprocess.Popen(
                cmd, cwd=str(cwd), env=env,
                stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                shell=False, close_fds=(os.name != "nt"),
            )
        # Popen now owns hp and has closed ht. Until this point we own both.
        raw_process = raw_thread = None
        phase = "worker"
        for stream, result in ((proc.stdout, stdout_res), (proc.stderr, stderr_res)):
            thread = threading.Thread(
                target=lambda stream=stream, result=result: result.extend(
                    _stream_pipe_reader(stream, max_output_chars)), daemon=True,
            )
            readers.append(thread)  # Own it even if start raises after starting it.
            thread.start()
        if stdin is not None:
            data = stdin.encode("utf-8") if isinstance(stdin, str) else stdin
            def write_input():
                try:
                    proc.stdin.write(data)
                except (OSError, BrokenPipeError):
                    pass
                finally:
                    proc.stdin.close()
            thread = threading.Thread(target=write_input, daemon=True)
            readers.append(thread)
            thread.start()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            terminate_tree()
        else:
            if h_job is not None:
                try:
                    winprocess._wait_job_empty(k, h_job, timeout=1)
                except Exception:
                    terminate_tree()
    except BaseException as exc:
        failure = exc
    finally:
        if failure is not None and (proc is not None or raw_process is not None):
            cleanup_action(terminate_tree)
        # Closing the Job also provides kill-on-close fallback if termination failed.
        if h_job is not None:
            cleanup_action(lambda: close_native(h_job))
        for handle in (raw_thread, raw_process):
            if handle is not None:
                cleanup_action(lambda handle=handle: close_native(handle))
        for thread in readers:
            if thread.ident is not None:
                cleanup_action(lambda thread=thread: thread.join(timeout=2))
            if thread.is_alive():
                cleanup_errors.append(RuntimeError("Worker stream thread still active"))
        if proc is not None:
            # Do not block indefinitely acquiring a stream lock from an active reader.
            if not any(thread.is_alive() for thread in readers):
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    if stream is not None:
                        cleanup_action(stream.close)
            if os.name == "nt":
                cleanup_action(proc._handle.Close)

    # Cancellation keeps its original identity, after all cleanup actions ran.
    cancellation = failure if failure is not None and not isinstance(failure, Exception) else next(
        (exc for exc in cleanup_errors if not isinstance(exc, Exception)), None)
    if cancellation is not None:
        if cleanup_errors:
            cancellation.add_note("CLEANUP_FAILED: worker teardown could not be fully proven")
            raise cancellation.with_traceback(cancellation.__traceback__) from BaseExceptionGroup(
                "Worker resource cleanup failed", cleanup_errors)
        raise cancellation.with_traceback(cancellation.__traceback__)
    if cleanup_errors:
        raise ExecutorError("Worker resource cleanup failed", "CLEANUP_FAILED") from cleanup_errors[0]
    if failure is not None:
        if isinstance(failure, ExecutorError):
            raise failure
        if phase != "launch" or proc is not None or raw_process is not None:
            raise ExecutorError("Worker lifecycle failed; attempt is terminal", "CLEANUP_FAILED") from failure
        return dict(exit_code=None, stdout="", stderr=f"Failed to start process: {failure}",
                    stdout_truncated=False, stderr_truncated=False, duration_seconds=0.0,
                    timed_out=False, cleanup_invoked=False, launch_failed=True)
    return dict(
        exit_code=137 if timed_out else proc.returncode,
        stdout=stdout_res[0] if stdout_res else "",
        stderr=stderr_res[0] if stderr_res else "",
        stdout_truncated=stdout_res[1] if stdout_res else False,
        stderr_truncated=stderr_res[1] if stderr_res else False,
        duration_seconds=round(time.monotonic() - start_time, 3),
        timed_out=timed_out, cleanup_invoked=cleanup_invoked, launch_failed=False,
    )


# --------------------------------------------------------------------------- LiveWorkUnitExecutor

class LiveWorkUnitExecutor:
    """Production Live WorkUnit executor connecting Cline CLI + local Qwen to Wave 2 scheduler."""

    def __init__(
        self,
        config: dict | None = None,
        *,
        gateway=None,
        runner=None,
        clock=time.monotonic,
        log=None,
    ):
        self.config = copy.deepcopy(config or {})
        self.gateway = gateway
        self.runner = runner or run_scoped_process
        self.clock = clock
        self.log = log
        self.deadline = None

    def set_deadline(self, deadline: float):
        self.deadline = deadline

    def bind_gateway(self, gateway):
        """Bind the Manager-owned Gateway, including its live Supervisor."""
        # Runner injection remains offline unless its caller explicitly opts
        # into Gateway lifecycle testing. The production runner always binds.
        if self.runner is run_scoped_process or self.gateway is not None:
            self.gateway = gateway

    def _run_worker(self, cmd, repo, timeout, worker_env, model):
        gateway = self.gateway
        if gateway is None:
            if self.runner is run_scoped_process:
                raise ExecutorError("Live worker requires a controller-owned Gateway", "MODEL_OWNERSHIP_REQUIRED")
            # Deterministic runner injection does not launch a model.
            return self.runner(cmd, cwd=repo.root, timeout=timeout, env=worker_env)
        if self.runner is run_scoped_process and (gateway.supervisor is None
                or (hasattr(repo, "store") and gateway.supervisor.store is not repo.store)
                or gateway.supervisor.store.state["target_repository"] != str(repo.root)):
            raise ExecutorError("Live worker Gateway is not supervised by this run", "MODEL_OWNERSHIP_REQUIRED")
        cline_url = self.config.get("cline", {}).get("base_url", self.config.get("base_url", DEFAULT_BASE_URL))
        if (model != gateway.config["roles"]["code"]
                or normalize_base_url(cline_url) != normalize_base_url(gateway.config["base_url"])):
            raise ExecutorError("Cline model/endpoint differs from the owned Gateway", "MODEL_ROUTING_MISMATCH")
        deadline = self.clock() + timeout
        previous = gateway.execution_deadline
        gateway.set_deadline(min(previous, time.monotonic() + timeout) if previous is not None
                             else time.monotonic() + timeout)
        invocation_error = None
        try:
            # Reserve launch intent and observe the exact delegated native PID
            # before Cline can request inference. Never adopt a worker-found PID.
            gateway.switch("code")
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise TimeoutError("Live worker budget exhausted during model startup")
            return self.runner(cmd, cwd=repo.root, timeout=remaining, env=worker_env)
        except BaseException as exc:
            invocation_error = exc
            raise
        finally:
            try:
                gateway.unload()
            except Exception as exc:
                if invocation_error is not None and not isinstance(invocation_error, Exception):
                    invocation_error.add_note("CLEANUP_FAILED: worker model teardown could not be proven")
                    raise invocation_error from exc
                raise ExecutorError("Worker model cleanup could not be proven", "CLEANUP_FAILED") from exc
            finally:
                gateway.set_deadline(previous)

    def execute(self, unit_spec: dict, context: dict, repo) -> dict:
        """Execute a WorkUnit attempt using Cline CLI.

        Returns a bounded diagnostic receipt dict.
        Raises TimeoutError on timeout.
        Raises ExecutorError on process failure or non-zero exit code.
        """
        uid = unit_spec["unit_id"]
        attempt_num = context.get("attempt_number", 1)

        # 1. Budget & deadline calculation
        now = self.clock()
        remaining = (self.deadline - now) if self.deadline is not None else float("inf")
        unit_timeout = unit_spec.get("limits", {}).get("timeout_seconds")
        cfg_timeout = self.config.get("cline", {}).get("timeout_seconds", self.config.get("timeout_seconds", 600))
        candidates = [t for t in (remaining, unit_timeout, cfg_timeout) if t is not None]
        timeout = min(candidates) if candidates else 600.0

        if timeout <= 0:
            raise TimeoutError(f"Stage deadline elapsed before execution of unit {uid}")

        # 2. Locate Cline executable
        executable = cline_executable(self.config)
        if not executable and self.runner is run_scoped_process:
            raise ExecutorError("Cline executable not found", "CLINE_START_FAILED")
        executable = executable or DEFAULT_CLINE_EXECUTOR

        # 3. Create isolated episode directory strictly outside candidate repository
        episode_id = uuid.uuid4().hex
        store_dir = None
        if hasattr(repo, "store") and hasattr(repo.store, "directory"):
            try:
                s_path = Path(repo.store.directory).resolve()
                r_path = Path(repo.root).resolve()
                if not s_path.is_relative_to(r_path):
                    store_dir = s_path
            except Exception:
                pass
        if store_dir is not None:
            episode_dir = store_dir / "cline" / episode_id
        else:
            episode_dir = Path(tempfile.gettempdir()) / "local-ai-harness" / "cline" / episode_id

        config_dir = episode_dir / "config"
        data_dir = episode_dir / "data"
        (data_dir / "settings").mkdir(parents=True, exist_ok=True)
        (data_dir / "cache").mkdir(parents=True, exist_ok=True)
        config_dir.mkdir(parents=True, exist_ok=True)

        global_settings_file = data_dir / "settings" / "global-settings.json"
        global_settings_file.write_text("{}", encoding="utf-8")

        providers_payload = generate_providers_json(self.config)
        (data_dir / "settings" / "providers.json").write_text(
            json.dumps(providers_payload, indent=2), encoding="utf-8"
        )

        models_payload = generate_models_json(self.config)
        (data_dir / "settings" / "models.json").write_text(
            json.dumps(models_payload, indent=2), encoding="utf-8"
        )

        flags_payload = generate_feature_flags_json()
        (data_dir / "cache" / "feature-flags.json").write_text(
            json.dumps(flags_payload, indent=2), encoding="utf-8"
        )

        # 4. Construct worker packet prompt
        prompt = build_worker_prompt(unit_spec, context, remaining_seconds=round(timeout, 1))
        (episode_dir / "prompt.txt").write_text(prompt, encoding="utf-8")

        # 5. Build command list
        cline_cfg = self.config.get("cline", {})
        provider = cline_cfg.get("provider", self.config.get("provider", DEFAULT_PROVIDER))
        model = (
            cline_cfg.get("model")
            or self.config.get("model")
            or self.config.get("roles", {}).get("code")
            or DEFAULT_MODEL
        )

        cmd = [
            str(executable),
            "--config", str(config_dir),
            "--data-dir", str(data_dir),
            "--cwd", str(repo.root),
            "--provider", provider,
            "--model", model,
            "--yolo",
            "--json",
            "--timeout", str(int(timeout)),
            prompt,
        ]

        if self.log and hasattr(self.log, "emit"):
            self.log.emit(
                "cline_attempt_start",
                unit_id=uid,
                attempt=attempt_num,
                episode_id=episode_id,
                timeout=round(timeout, 3),
            )

        # 6. Execute process
        worker_env = dict(os.environ)
        # Fix 4: Sanitize ambient Cline settings overrides
        for key in list(worker_env.keys()):
            if key.upper().startswith("CLINE_"):
                del worker_env[key]
        worker_env["CLINE_DATA_DIR"] = str(data_dir)
        worker_env["CLINE_GLOBAL_SETTINGS_PATH"] = str(global_settings_file)
        worker_env["CLINE_MCP_SETTINGS_PATH"] = str(data_dir / "mcp_settings.json")
        worker_env["CLINE_NO_AUTO_UPDATE"] = "1"
        worker_env["NO_COLOR"] = "1"
        res = self._run_worker(cmd, repo, timeout, worker_env, model)

        if res.get("launch_failed"):
            raise ExecutorError(f"Failed to launch Cline process: {res.get('stderr')}", "CLINE_START_FAILED")

        if res.get("timed_out"):
            raise TimeoutError(f"Live worker timed out after {timeout:.3f}s")

        if res.get("exit_code") != 0:
            err_msg = res.get("stderr") or res.get("stdout") or "Unknown error"
            raise ExecutorError(
                f"Live worker exited with non-zero exit code {res.get('exit_code')}: {err_msg[:500]}",
                "CLINE_EXIT_ERROR",
            )

        # 7. Worker returned 0; produce untrusted bounded receipt
        raw_stdout = res.get("stdout", "") or ""
        raw_stderr = res.get("stderr", "") or ""
        stdout_truncated = res.get("stdout_truncated", False) or len(raw_stdout) > MAX_OUTPUT_CHARS
        stderr_truncated = res.get("stderr_truncated", False) or len(raw_stderr) > MAX_OUTPUT_CHARS
        stdout_tail = raw_stdout[-MAX_OUTPUT_CHARS:] if len(raw_stdout) > MAX_OUTPUT_CHARS else raw_stdout
        stderr_tail = raw_stderr[-MAX_OUTPUT_CHARS:] if len(raw_stderr) > MAX_OUTPUT_CHARS else raw_stderr

        receipt = {
            "unit_id": uid,
            "attempt_number": attempt_num,
            "exit_code": res.get("exit_code", 0),
            "duration_seconds": res.get("duration_seconds", 0.0),
            "stdout_tail": stdout_tail,
            "stderr_tail": stderr_tail,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "timed_out": False,
            "trusted": False,  # Model claims are NEVER trusted
        }

        if hasattr(repo, "store") and hasattr(repo.store, "artifact"):
            try:
                s_dir = Path(repo.store.directory).resolve()
                r_dir = Path(repo.root).resolve()
                if not s_dir.is_relative_to(r_dir):
                    repo.store.artifact(f"cline/{episode_id}/result.json", receipt)
            except Exception:
                pass

        if self.log and hasattr(self.log, "emit"):
            self.log.emit("cline_attempt_end", **receipt)

        return receipt
