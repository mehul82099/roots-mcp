#!/usr/bin/env python3
"""roots-mcp - a zero-dependency MCP server (stdio) that discovers the
client's workspace through MCP roots, requested via the SEP-2322
InputRequiredResult envelope on the 2026-07-28 stateless core.

Demo tool workspace_stats: on first call the server asks the client for
its roots (roots/list - the directories the client says it may see),
then walks them read-only and returns statistics: file/dir counts, per
-extension breakdown, largest files, with nested roots de-duplicated so
nothing is counted twice.

  client -> tools/call(id=1, workspace_stats{extensions?})
  server -> {resultType: "input_required",
             inputRequests: {"roots": {"method": "roots/list"}},
             requestState: <signed blob>}        (request ends)
  client -> tools/call(id=2, inputResponses={"roots": ListRootsResult})
  server -> final CallToolResult with the stats

Spec rules honored (2026-07-28 roots spec, SEP-2322):
- roots/list is only requested when the client declares the `roots`
  capability in _meta.io.modelcontextprotocol/clientCapabilities;
  otherwise a plain-text fallback result, never an ask.
- Only file:// URIs are walked - the spec requires file:// in the
  current spec version; any other scheme is rejected with a tool error.
- Root boundaries are respected: the server only ever reads inside the
  declared roots, never writes anywhere, never follows symlinks, and
  stops after a file cap (default 50000, ROOTS_MCP_MAX_FILES to tune).
- Nested roots are de-duplicated: if one declared root contains
  another, the inner one is skipped so files are not double counted.
- Stateless: the tool arguments travel in requestState, base64url JSON
  signed with HMAC-SHA256 (per-process random key); tampered or foreign
  state fails with -32602.
- Errors: bad arguments are tool execution errors (SEP-1303); protocol
  problems are JSON-RPC errors; tool names follow SEP-986.

Run: python3 roots_mcp.py
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import urllib.parse

PROTOCOL_VERSION = "2026-07-28"
LEGACY_VERSIONS = {"2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"}
SERVER_INFO = {"name": "roots-mcp", "version": "1.0.0"}

ERR_INVALID_PARAMS = -32602
ERR_METHOD_NOT_FOUND = -32601
ERR_PARSE = -32700

CAP_KEY = "io.modelcontextprotocol/clientCapabilities"
MAX_FILES = int(os.environ.get("ROOTS_MCP_MAX_FILES", 50_000))
MAX_EXT = 10
MAX_LARGEST = 5
MAX_EXTENSIONS_ARG = 50

_STATE_KEY = secrets.token_bytes(32)


def _b64e(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def mint_state(payload):
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(_STATE_KEY, body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def read_state(blob):
    try:
        body, sig = blob.split(".", 1)
    except ValueError:
        raise ValueError("malformed")
    expect = hmac.new(_STATE_KEY, body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expect):
        raise ValueError("signature mismatch")
    payload = json.loads(_b64d(body))
    if not isinstance(payload, dict):
        raise ValueError("bad payload")
    return payload


def _input_required(state):
    return {"resultType": "input_required",
            "inputRequests": {"roots": {"method": "roots/list"}},
            "requestState": state}


def _complete(text, structured):
    return {"resultType": "complete",
            "content": [{"type": "text", "text": text}],
            "structuredContent": structured}


def _tool_error(text):
    return {"resultType": "complete",
            "content": [{"type": "text", "text": text}], "isError": True}


def _client_supports_roots(meta):
    return "roots" in ((meta or {}).get(CAP_KEY, {}))


def uri_to_path(uri):
    """file:// URI -> local path. Raises ValueError for anything else."""
    parsed = urllib.parse.urlparse(uri)
    if parsed.scheme != "file":
        raise ValueError(f"not a file:// URI: {uri}")
    if parsed.netloc not in ("", "localhost"):
        raise ValueError(f"remote file authority not supported: {uri}")
    path = urllib.parse.unquote(parsed.path)
    if re.match(r"^/[A-Za-z]:[/\\]", path):  # Windows drive: /C:/...
        path = path[1:]
    return path


def dedupe_roots(paths):
    """Drop roots contained inside another root (avoid double counting)."""
    resolved = []
    for p in paths:
        try:
            resolved.append(os.path.realpath(p))
        except OSError:
            resolved.append(p)
    keep = []
    for i, candidate in enumerate(resolved):
        contained = False
        for j, other in enumerate(resolved):
            if i != j and (candidate == other
                           or candidate.startswith(other + os.sep)):
                if candidate == other and j > i:
                    continue  # identical duplicate: keep the first
                contained = True
                break
        if not contained:
            keep.append(paths[i])
    return keep


def walk_roots(paths, max_files=MAX_FILES):
    total_files = 0
    total_dirs = 0
    total_bytes = 0
    by_ext = {}
    largest = []  # (size, path)
    truncated = False
    skipped_roots = []
    for root in paths:
        if not os.path.isdir(root):
            skipped_roots.append(root)
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            total_dirs += 1
            for name in filenames:
                full = os.path.join(dirpath, name)
                try:
                    size = os.lstat(full).st_size  # no symlink following
                except OSError:
                    continue
                total_files += 1
                total_bytes += size
                ext = os.path.splitext(name)[1].lower() or "(none)"
                by_ext[ext] = by_ext.get(ext, 0) + 1
                largest.append((size, full))
                if total_files >= max_files:
                    truncated = True
                    break
            if truncated:
                break
        if truncated:
            break
    largest.sort(key=lambda item: item[0], reverse=True)
    top_ext = sorted(by_ext.items(), key=lambda kv: kv[1], reverse=True)
    return {
        "total_files": total_files,
        "total_dirs": total_dirs,
        "total_bytes": total_bytes,
        "top_extensions": [{"extension": e, "files": c}
                           for e, c in top_ext[:MAX_EXT]],
        "largest_files": [{"path": p, "bytes": s}
                          for s, p in largest[:MAX_LARGEST]],
        "truncated": truncated,
        "skipped_roots": skipped_roots,
    }


def workspace_stats(arguments, meta):
    arguments = arguments or {}
    input_responses = arguments.get("inputResponses") or {}
    blob = arguments.get("requestState")

    state = {}
    if blob is not None:
        if not isinstance(blob, str):
            return ("error", "requestState must be a string.")
        try:
            state = read_state(blob)
        except (ValueError, json.JSONDecodeError):
            return ("error", "requestState is invalid or was not minted by "
                             "this server process. Start over without "
                             "requestState.")

    if not input_responses:
        extensions = arguments.get("extensions")
        if extensions is not None:
            if not isinstance(extensions, list) \
                    or len(extensions) > MAX_EXTENSIONS_ARG \
                    or not all(isinstance(e, str) and re.fullmatch(
                        r"\.[A-Za-z0-9]{1,16}", e) for e in extensions):
                return ("complete", _tool_error(
                    "extensions must be a list of up to "
                    f"{MAX_EXTENSIONS_ARG} entries like '.py'."))
        if not _client_supports_roots(meta):
            return ("complete", _complete(
                "This client does not declare MCP roots support, so "
                "workspace_stats cannot discover the workspace. Reconnect "
                "with a roots-capable client (capability: roots).",
                {"stats": None, "roots": "unsupported"}))
        return ("complete", _input_required(mint_state({
            "extensions": extensions})))

    if set(input_responses) != {"roots"}:
        return ("error", "inputResponses must answer exactly the 'roots' "
                         "key from inputRequests.")
    result = input_responses["roots"]
    if not isinstance(result, dict) or not isinstance(
            result.get("roots"), list):
        return ("error", "roots response must be an object with a roots "
                         "list.")
    paths = []
    bad = []
    for entry in result["roots"]:
        if not isinstance(entry, dict) or not isinstance(
                entry.get("uri"), str):
            return ("error", "each root needs a string uri.")
        try:
            paths.append(uri_to_path(entry["uri"]))
        except ValueError:
            bad.append(entry["uri"])
    if bad:
        return ("complete", _tool_error(
            "Only file:// roots can be walked (spec: uri MUST be a "
            "file:// URI). Unsupported: " + ", ".join(bad)))
    if not paths:
        return ("complete", _complete(
            "The client reported no roots; nothing to walk.",
            {"stats": None, "roots": []}))

    walked = dedupe_roots(paths)
    stats = walk_roots(walked)
    stats["roots_walked"] = walked
    stats["roots_skipped_as_nested"] = len(paths) - len(walked)
    wanted = state.get("extensions")
    if wanted:
        stats["requested_extensions"] = [
            {"extension": e, "files": next(
                (x["files"] for x in stats["top_extensions"]
                 if x["extension"] == e),
                _count_ext(walked, e))} for e in wanted]
    text = (f"{stats['total_files']} files in {stats['total_dirs']} "
            f"dirs across {len(walked)} root(s)"
            + (" (truncated at cap)" if stats["truncated"] else "") + ".")
    return ("complete", _complete(text, {"stats": stats}))


def _count_ext(paths, ext):
    count = 0
    for root in paths:
        for _dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                if name.lower().endswith(ext):
                    count += 1
    return count


TOOLS = [{
    "name": "workspace_stats",
    "description": "Walk the client's declared workspace roots (MCP "
                   "roots/list via SEP-2322 round-trip) read-only and "
                   "return file/dir counts, per-extension breakdown and "
                   "largest files. Nested roots are de-duplicated, "
                   "symlinks are not followed, and walking stops at a "
                   "file cap. Requires a client that declares the roots "
                   "capability.",
    "inputSchema": {"type": "object", "properties": {
        "extensions": {"type": "array",
                       "items": {"type": "string"},
                       "description": "Optional extensions like '.py' to "
                                      "count explicitly."},
        "inputResponses": {"type": "object"},
        "requestState": {"type": "string"}},
        "additionalProperties": True},
}]


def capabilities():
    return {"tools": {"listChanged": False}}


def handle_message(msg):
    if not isinstance(msg, dict) or "id" not in msg:
        return None
    rid = msg["id"]
    method = msg.get("method")
    params = msg.get("params") or {}

    if method == "server/discover":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "serverInfo": SERVER_INFO, "capabilities": capabilities()}}
    if method == "initialize":
        requested = params.get("protocolVersion", "")
        version = requested if requested in LEGACY_VERSIONS \
            or requested == PROTOCOL_VERSION else PROTOCOL_VERSION
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": version,
            "serverInfo": SERVER_INFO, "capabilities": capabilities()}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        if params.get("name") != "workspace_stats":
            return {"jsonrpc": "2.0", "id": rid, "error": {
                "code": ERR_INVALID_PARAMS,
                "message": f"Unknown tool '{params.get('name')}'."}}
        kind, payload = workspace_stats(params.get("arguments"),
                                        msg.get("_meta"))
        if kind == "error":
            return {"jsonrpc": "2.0", "id": rid, "error": {
                "code": ERR_INVALID_PARAMS, "message": payload}}
        return {"jsonrpc": "2.0", "id": rid, "result": payload}
    return {"jsonrpc": "2.0", "id": rid, "error": {
        "code": ERR_METHOD_NOT_FOUND, "message": f"Unknown method '{method}'."}}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            sys.stdout.write(json.dumps({
                "jsonrpc": "2.0", "id": None,
                "error": {"code": ERR_PARSE, "message": "Parse error"}})
                + "\n")
            sys.stdout.flush()
            continue
        response = handle_message(msg)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
