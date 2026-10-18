"""Fixed data-only offline worker. Process facts come from controller handles.

Private standalone protocol; no arbitrary command, import name or Python source.
Not a sandbox against same-user interference. No scheduler or repair integration.
"""
import hashlib
import os
from pathlib import Path
import sys
import time

# -I excludes the candidate repository and PYTHONPATH from imports.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from offline_scripted_executor import (MAX_INPUT_BYTES, MAX_FILE_BYTES, OfflineExecutorError,
                                       _decode, _fields, _json, _require, _root, _target, validate_script)


def main():
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    _require(len(raw) <= MAX_INPUT_BYTES, "Oversized worker input")
    value = _decode(raw)
    _fields(value, "schema_version repository script allowed_paths forbidden_paths script_sha256")
    _require(type(value["schema_version"]) is int and value["schema_version"] == 1
             and type(value["repository"]) is str and Path(value["repository"]).is_absolute())
    validate_script(value["script"], value["allowed_paths"], value["forbidden_paths"])
    sha = hashlib.sha256(_json(value["script"])).hexdigest()
    _require(value["script_sha256"] == sha, "Script fingerprint mismatch")
    root = _root(value["repository"])
    completed, error = 0, None
    try:
        for action in value["script"]["actions"]:
            op = action["op"]
            if op in {"write", "replace"}:
                target = _target(root, action["path"])
                if op == "write":
                    content = action["content"].encode("utf-8")
                else:
                    _require(target.is_file() and target.stat().st_size <= MAX_FILE_BYTES, "Replacement target unavailable")
                    content = target.read_bytes()
                    old = action["old"].encode("utf-8")
                    _require(content.count(old) == 1, "Replacement needs exactly one match")
                    content = content.replace(old, action["new"].encode("utf-8"))
                    _require(len(content) <= MAX_FILE_BYTES, "Replacement file too large")
                target.parent.mkdir(parents=True, exist_ok=True)
                target = _target(root, action["path"])
                with target.open("wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
            elif op == "sleep":
                time.sleep(action["seconds"])
            elif op == "emit":
                sys.stderr.buffer.write(action["text"].encode("utf-8"))
                sys.stderr.buffer.flush()
            elif op == "fail":
                raise OfflineExecutorError(action["message"])
            completed += 1
    except Exception as exc:
        # Text is diagnostic only; receipt contains no process ownership claims.
        text = "".join(c if c.isprintable() or c in "\n\t" else " " for c in str(exc))
        error = text.encode("utf-8", errors="replace")[:512].decode("utf-8", errors="ignore")
    sys.stdout.buffer.write(_json(dict(schema_version=1, script_sha256=sha, status="failed" if error is not None else "ok",
                                      actions_completed=completed, error=error)))
    sys.stdout.buffer.flush()
    return 10 if error is not None else 0


if __name__ == "__main__":
    raise SystemExit(main())
