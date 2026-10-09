"""One strict JSON operation per response; no dynamic tool registration."""
import copy
import json
import re

TOOLS = ("read_files", "edit_file", "submit")
MAX_OPERATION_CHARS = 70000
MAX_TEXT_CHARS = 32768
MAX_FILES = 8
MAX_ACTIONS = 32

class Denied(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)

def text_schema(limit=MAX_TEXT_CHARS):
    return {"type": "string", "maxLength": limit}
def obj(properties):
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}

OPERATION_SCHEMA = {"oneOf": [
    obj({"tool": {"const": "read_files"}, "arguments": obj({
        "paths": {"type": "array", "items": text_schema(240), "minItems": 1, "maxItems": MAX_FILES}})}),
    obj({"tool": {"const": "edit_file"}, "arguments": obj({
        "path": text_schema(240),
        "before_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "old_text": {**text_schema(), "minLength": 1}, "new_text": text_schema()})}),
    obj({"tool": {"const": "submit"}, "arguments": obj({"summary": text_schema(1000)})})
]}

def schema():
    return copy.deepcopy(OPERATION_SCHEMA)

def _pairs(pairs):
    value = {}
    for k, v in pairs:
        if k in value:
            raise Denied("MALFORMED_JSON")
        value[k] = v
    return value

def _keys(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        raise Denied("MALFORMED_OPERATION")

def _string(value, limit, nonempty=False):
    if type(value) is not str or len(value) > limit or (nonempty and not value):
        raise Denied("MALFORMED_OPERATION")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise Denied("MALFORMED_OPERATION") from None

def parse(raw):
    if type(raw) is not str or len(raw) > MAX_OPERATION_CHARS:
        raise Denied("MALFORMED_JSON")
    try:
        value = json.loads(raw, object_pairs_hook=_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(Denied("MALFORMED_JSON")))
    except (ValueError, RecursionError):
        raise Denied("MALFORMED_JSON") from None
    _keys(value, ("tool", "arguments"))
    if type(value["tool"]) is not str or value["tool"] not in TOOLS:
        raise Denied("UNKNOWN_TOOL")
    args = value["arguments"]
    if value["tool"] == "read_files":
        _keys(args, ("paths",))
        paths = args["paths"]
        if type(paths) is not list or not 1 <= len(paths) <= MAX_FILES:
            raise Denied("MALFORMED_OPERATION")
        for p in paths:
            _string(p, 240, True)
        if len(set(paths)) != len(paths):
            raise Denied("MALFORMED_OPERATION")
    elif value["tool"] == "edit_file":
        _keys(args, ("path", "before_sha256", "old_text", "new_text"))
        _string(args["path"], 240, True)
        _string(args["before_sha256"], 64)
        if re.fullmatch(r"[0-9a-f]{64}", args["before_sha256"]) is None:
            raise Denied("MALFORMED_OPERATION")
        _string(args["old_text"], MAX_TEXT_CHARS, True)
        _string(args["new_text"], MAX_TEXT_CHARS)
    else:
        _keys(args, ("summary",))
        _string(args["summary"], 1000)
    return value
