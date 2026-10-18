"""Fixed SafeQwen conversation child. Tool requests only; no repository edits.

The controller owns every tool result, edit approval and audit record. No Store,
WAL, grant, checkpoint, network transport or executable action data is accepted.
"""
import os
from pathlib import Path
import sys
from types import SimpleNamespace

# Isolated Python excludes the candidate repository and PYTHONPATH from imports.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from safe_qwen_worker import SafeQwenWorker, Policy
from supervised_safe_qwen import (_decode, _fields, _frame, _message, _require,
                                  MAX_FRAME_BYTES, MAX_TOTAL_BYTES)


class _Channel:
    def __init__(self):
        self.sequence, self.total = 0, 0
        self.session = None

    def receive(self, expected):
        raw = sys.stdin.buffer.readline(MAX_FRAME_BYTES + 2)
        self.total += len(raw)
        _require(self.total <= MAX_TOTAL_BYTES and raw.endswith(b"\n"), "Incomplete or oversized IPC input")
        if self.session is None:
            value = _decode(raw)
            self.session = value.get("session") if type(value) is dict else None
            _require(type(self.session) is str and len(self.session) == 32
                     and all(c in "0123456789abcdef" for c in self.session))
        kind, payload = _message(raw[:-1], self.session, self.sequence)
        _require(kind == expected, "Unexpected controller message")
        self.sequence += 1
        return payload

    def send(self, kind, payload):
        raw = _frame(self.session, self.sequence, kind, payload)
        self.total += len(raw)
        _require(self.total <= MAX_TOTAL_BYTES)
        sys.stdout.buffer.write(raw)
        sys.stdout.buffer.flush()
        self.sequence += 1

    def exchange(self, kind, payload, expected):
        self.send(kind, payload)
        return self.receive(expected)


class _RemoteSession:
    def __init__(self, channel):
        self.channel, self.actions, self.submitted = channel, 0, False

    def dispatch(self, raw):
        result = self.channel.exchange("TOOL", {"raw": raw}, "TOOL_RESULT")
        _require(type(result) is dict and type(result.get("ok")) is bool)
        self.actions += 1
        self.submitted = result.get("submitted") is True and result["ok"] is True
        return result


class _RemoteWorker(SafeQwenWorker):
    def _tool_session(self, policy):
        return _RemoteSession(self.channel)


def main():
    channel = _Channel()
    try:
        start = channel.receive("START")
        _fields(start, "unit repository deadline max_actions policy" + (" dispatch_context" if "dispatch_context" in start else ""))
        context = {"attempt_number": 1}  # Each invocation is still single-attempt.
        if "dispatch_context" in start:
            from supervised_safe_qwen import _json
            c = start["dispatch_context"]
            _fields(c, "schema_version attempt_id execution_id repair_evidence")
            _require(type(c["schema_version"]) is int and c["schema_version"] == 1
                     and c["attempt_id"] == start["unit"]["unit_id"] + "-attempt-2"
                     and type(c["execution_id"]) is str and len(c["execution_id"]) == 64
                     and all(x in "0123456789abcdef" for x in c["execution_id"]))
            packet = c["repair_evidence"]
            _require(type(packet) is dict and type(packet.get("schema_version")) is int and packet["schema_version"] == 2
                     and packet.get("trusted") is False and packet.get("authority") == "none"
                     and len(_json(packet)) <= 8192)
            context["repair_evidence"] = packet
        _fields(start["policy"], "readable writable frozen required")
        _require(Path(start["repository"]).resolve(strict=True) == Path.cwd().resolve(strict=True))
        policy = Policy(Path(start["repository"]), **{k: tuple(v) for k, v in start["policy"].items()})
        def transport(messages, operation_schema, remaining):
            response = channel.exchange("MODEL", dict(messages=messages, schema=operation_schema), "RESPONSE")
            _fields(response, "content")
            _require(type(response["content"]) is str and len(response["content"]) <= 70000)
            return response["content"]
        worker = _RemoteWorker(policy, transport=transport, audit=None, max_actions=start["max_actions"])
        worker.channel = channel
        worker.set_deadline(start["deadline"])
        receipt = worker.execute(start["unit"], context, SimpleNamespace(root=policy.root))
        channel.send("RESULT", receipt)
        return 0
    except BaseException as exc:
        if channel.session is not None:
            try:
                code = "TIMEOUT" if isinstance(exc, TimeoutError) else "ACTION_LIMIT" if getattr(exc, "code", None) == "ACTION_LIMIT" else "WORKER_FAILED"
                channel.send("ERROR", {"code": code})
            except BaseException:
                pass
        sys.stderr.write("SafeQwen worker failed: " + type(exc).__name__[:80] + "\n")
        return 10


if __name__ == "__main__":
    sys.exit(main())
