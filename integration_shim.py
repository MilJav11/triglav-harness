"""Controller integration seam; imports harness only when execute is called.

Use an explicitly supplied, Manager-owned Gateway. No model lifecycle tool is
ever exposed to the model. Success is recorded only after worker and cleanup pass.
"""
import hashlib
import json
from pathlib import Path
import time
import traceback
import uuid

from safe_qwen_worker import Policy, SafeQwenWorker
from tool_protocol import Denied
from safe_qwen_worker import ToolSession
from urllib.parse import urlsplit

def error_diagnostic(exc, stage):
    # Reuse the controller's existing credential redactor; never persist prompts,
    # raw response bodies or reasoning. Fixed-size frames retain the failure site.
    from critic_executor import sanitize_evidence
    chain, seen = [], set()
    current = exc
    while current is not None and id(current) not in seen and len(chain) < 4:
        seen.add(id(current))
        chain.append({"error_type": type(current).__name__,
                      "message": sanitize_evidence(str(current))[:1000],
                      "code": getattr(current, "code", None),
                      "http_status": getattr(current, "status", None),
                      "frames": [{"file": Path(f.filename).name, "line": f.lineno, "function": f.name}
                                 for f in traceback.extract_tb(current.__traceback__)[-12:]]})
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    return {"stage": stage, "error_type": type(exc).__name__, "causes": chain}

class SafeQwenExecutor:
    def __init__(self, policy, config, *, gateway=None, log=None, max_actions=32):
        self.policy, self.config = policy, config
        self.gateway, self.log, self.max_actions = gateway, log, max_actions
        self.deadline = None

    def bind_gateway(self, gateway):
        self.gateway = gateway

    def set_deadline(self, deadline):
        self.deadline = deadline

    def execute(self, unit_spec, context, repo):
        from manager import ExecutorError
        started = time.monotonic()
        gw = self.gateway
        store = getattr(repo, "store", None)
        episode = uuid.uuid4().hex
        prefix = f"safe_qwen/{episode}"
        stage = "initialization"
        receipt = None
        invocation_error = cleanup_error = None
        previous = None
        lifecycle_started = False
        requests = sequence = 0

        def audit(record):
            nonlocal sequence
            sequence += 1
            store.artifact(f"{prefix}/actions/{sequence:04d}.json", record)

        def transport(messages, operation_schema, remaining):
            nonlocal requests
            requests += 1
            request_started = time.monotonic()
            request_stage = "request_setup"
            diagnostic = {"request_number": requests, "model": self.config["roles"]["code"], "role": "code",
                          "endpoint": "/v1/chat/completions", "status": "started",
                          "wire_valid": None, "trusted": False}
            try:
                request_timeout = gw.bounded_timeout(min(remaining, self.config["request_timeout_seconds"]))
                payload = {"model": gw.config["roles"]["code"], "messages": messages,
                           "max_tokens": 4096, "temperature": 0, "seed": 42, "stream": False,
                           "response_format": {"type": "json_schema", "json_schema": {
                               "name": "safe_qwen_operation", "strict": True, "schema": operation_schema}}}
                # No OpenAI 'tools' field or generic execution schema.
                gw.log.emit("model_request_start", role="code", model=payload["model"],
                            phase="safe_qwen_worker", timeout_seconds=request_timeout)
                request_stage = "transport"
                with gw.log.operation("REQUEST", "safe Qwen operation", request_timeout, role="code"):
                    wire = gw.request("POST", "/v1/chat/completions", payload, timeout=request_timeout)
                request_stage = "response_validation"
                valid = (type(wire) is dict and type(wire.get("choices")) is list
                         and len(wire["choices"]) == 1)
                choice = wire["choices"][0] if valid else None
                valid = valid and type(choice) is dict and choice.get("finish_reason") == "stop"
                message = choice.get("message") if valid else None
                valid = (valid and type(message) is dict and message.get("role") == "assistant"
                         and not message.get("tool_calls") and not message.get("function_call")
                         and not message.get("refusal") and type(message.get("content")) is str
                         and len(message["content"]) <= 70000)
                diagnostic.update(wire_valid=bool(valid), status="response_received" if valid else "malformed_response")
                if valid:
                    diagnostic["content_sha256"] = hashlib.sha256(message["content"].encode("utf-8")).hexdigest()
                    diagnostic["content_chars"] = len(message["content"])
                request_stage = "ram_guard"
                gw.check_active_ram()
                request_stage = "telemetry"
                diagnostic["duration_seconds"] = round(time.monotonic() - request_started, 3)
                store.artifact(f"{prefix}/requests/{requests:04d}.json", diagnostic)
                # EventLog.show_progress requires model, phase and duration_seconds.
                gw.log.emit("model_request", role="code", model=payload["model"],
                            phase="safe_qwen_worker", duration_seconds=diagnostic["duration_seconds"],
                            wire_valid=bool(valid), finish_reason="stop" if valid else None)
                return message["content"] if valid else "{}"
            except Exception as exc:
                diagnostic.update(status="failed", duration_seconds=round(time.monotonic() - request_started, 3),
                                  failure=error_diagnostic(exc, request_stage))
                exc.diagnostic = diagnostic
                store.artifact(f"{prefix}/requests/{requests:04d}.json", diagnostic)
                # 'error' is intentionally removed by DurableLog. Structured bounded
                # diagnostics and error_type survive that existing filter.
                gw.log.emit("model_request_failed", role="code", model=self.config["roles"]["code"], phase="safe_qwen_worker",
                            duration_seconds=diagnostic["duration_seconds"], error_type=type(exc).__name__,
                            diagnostic=diagnostic)
                raise  # No transport retries or executor fallback.

        try:
            if (gw is None or store is None or gw.supervisor is None
                    or gw.supervisor.store is not store
                    or gw.supervisor.store.state["target_repository"] != str(repo.root)):
                raise ExecutorError("Native worker requires this run's supervised Gateway", "MODEL_OWNERSHIP_REQUIRED")
            if (Path(store.directory).resolve().is_relative_to(Path(repo.root).resolve())
                    or Path(repo.root).resolve() != self.policy.root):
                raise ExecutorError("Policy/Store repository mismatch", "MODEL_OWNERSHIP_REQUIRED")
            url = urlsplit(self.config["base_url"])
            if (url.scheme != "http" or url.hostname != "127.0.0.1" or not url.port
                    or url.username or url.password or url.path not in ("", "/") or url.query or url.fragment
                    or self.config["base_url"].rstrip("/") != gw.config["base_url"].rstrip("/")
                    or self.config["roles"]["code"] != gw.config["roles"]["code"]):
                raise ExecutorError("Native model/endpoint differs from owned Gateway", "MODEL_ROUTING_MISMATCH")
            if self.config.get("executor") == "safe_qwen":
                from terminal_run import fixed_unit
                expected = fixed_unit(unit_spec["objective"], self.config, self, unit_spec["verifier_ids"])
                if (unit_spec["unit_id"] != expected["unit_id"] or unit_spec["mode"] != expected["mode"]
                        or unit_spec["limits"] != expected["limits"]
                        or any(sorted(unit_spec["scope"][key]) != sorted(expected["scope"][key])
                               for key in ("allowed_paths", "forbidden_paths"))):
                    raise ExecutorError("SafeQwen fixed WorkUnit contract mismatch", "CONTRACT_MISMATCH")
            # Validate every required/frozen file before any model lifecycle operation.
            ToolSession(self.policy, audit=audit, max_actions=self.max_actions)
            timeout = min(unit_spec["limits"]["timeout_seconds"], self.config.get("timeout_seconds", 600))
            if self.deadline is not None:
                timeout = min(timeout, self.deadline - time.monotonic())
            if timeout <= 0:
                raise TimeoutError("Native worker deadline elapsed")
            previous = gw.execution_deadline
            deadline = time.monotonic() + timeout
            if previous is not None:
                deadline = min(previous, deadline)
            worker = SafeQwenWorker(self.policy, transport=transport, audit=audit,
                                    log=self.log, max_actions=self.max_actions)
            worker.set_deadline(deadline)
            store.artifact(f"{prefix}/policy.json", {
                "protocol": 1, "readable": self.policy.readable, "writable": self.policy.writable,
                "frozen": self.policy.frozen, "required": self.policy.required,
                "max_actions": self.max_actions, "trusted": False})
            if self.log:
                self.log.emit("safe_qwen_attempt_start", unit_id=unit_spec["unit_id"],
                              attempt=context.get("attempt_number", 1), episode_id=episode)
            stage = "model_start"
            lifecycle_started = True
            gw.set_deadline(deadline)
            gw.switch("code")  # Controller retains delegated-model supervision.
            stage = "worker_execution"
            receipt = worker.execute(unit_spec, context, repo)
        except BaseException as exc:
            invocation_error = exc
        finally:
            if lifecycle_started:
                try:
                    gw.unload()
                except BaseException as exc:
                    cleanup_error = exc
                finally:
                    try:
                        gw.set_deadline(previous)
                    except BaseException as exc:
                        cleanup_error = cleanup_error or exc

        elapsed = round(time.monotonic() - started, 3)
        if invocation_error is not None or cleanup_error is not None:
            primary = invocation_error if invocation_error is not None else cleanup_error
            failure = {"unit_id": unit_spec.get("unit_id"), "attempt_number": context.get("attempt_number", 1),
                       "episode_id": episode, "exit_code": 1, "trusted": False, "submitted": False,
                       "duration_seconds": elapsed, "model_requests": requests,
                       "error": type(primary).__name__, "code": getattr(primary, "code", None),
                       "failure": error_diagnostic(primary, stage if invocation_error is not None else "cleanup"),
                       "request_diagnostic": getattr(primary, "diagnostic", None),
                       "cleanup": "failed" if cleanup_error is not None else "passed" if lifecycle_started else "not_started",
                       "cleanup_failure": error_diagnostic(cleanup_error, "cleanup") if cleanup_error is not None else None}
            primary.diagnostic = failure
            # No evidence write through an invalid or absent Store/repository binding.
            if (store is not None and gw is not None and getattr(gw, "supervisor", None) is not None
                    and gw.supervisor.store is store and Path(repo.root).resolve() == self.policy.root
                    and not Path(store.directory).resolve().is_relative_to(self.policy.root)):
                try:
                    store.artifact(f"{prefix}/failure.json", failure)
                    store.artifact(f"{prefix}/result.json", failure)
                    if self.log:
                        self.log.emit("safe_qwen_attempt_end", **failure)
                except BaseException as evidence_error:
                    evidence_error.diagnostic = failure
                    raise evidence_error from primary
            if cleanup_error is not None:
                if invocation_error is not None and not isinstance(invocation_error, Exception):
                    invocation_error.add_note("CLEANUP_FAILED: native model teardown unproven")
                    raise invocation_error from cleanup_error
                error = ExecutorError("Native worker model cleanup unproven; see structured failure evidence", "CLEANUP_FAILED")
                error.diagnostic = failure
                raise error from primary
            raise primary

        receipt.update(episode_id=episode, duration_seconds=elapsed, model_requests=requests, cleanup="passed")
        try:
            store.artifact(f"{prefix}/result.json", receipt)
            if self.log:
                self.log.emit("safe_qwen_attempt_end", **receipt)
        except BaseException as exc:
            failure = {**receipt, "exit_code": 1, "submitted": False,
                       "failure": error_diagnostic(exc, "terminal_evidence")}
            exc.diagnostic = failure
            try:
                store.artifact(f"{prefix}/failure.json", failure)
                store.artifact(f"{prefix}/result.json", failure)
            except BaseException:
                exc.add_note("Terminal evidence persistence failed; candidate remains untrusted")
            raise
        return receipt
