# roots-mcp

A zero-dependency, single-file Python MCP server (stdio) that discovers
the client's workspace through **MCP roots**, requested via the
[SEP-2322](https://modelcontextprotocol.io/seps/2322-multi-round-trip-requests)
`InputRequiredResult` envelope on the 2026-07-28 stateless core.

The demo tool, `workspace_stats`, asks the client for its declared
roots (`roots/list` - the directories the client says the server may
see), then walks them read-only and reports file/dir counts, a
per-extension breakdown, and the largest files. Nested roots are
de-duplicated so nothing is counted twice, symlinks are never followed,
and walking stops at a file cap.

## The flow

```
client -> tools/call(id=1, workspace_stats{extensions?})
server -> {resultType: "input_required",
           inputRequests: {"roots": {"method": "roots/list"}},
           requestState: "..."}                  # request ends
client -> tools/call(id=2, inputResponses={"roots": ListRootsResult})
server -> {resultType: "complete", structuredContent: {stats}}
```

## What it demonstrates, mapped to the specs

- **Roots through the round-trip envelope (2026-07-28 roots spec +
  SEP-2322):** `roots/list` is delivered inside
  `InputRequiredResult.inputRequests`, and the client's
  `ListRootsResult` comes back in `inputResponses` on a brand-new
  request - the stateless pattern, no SSE stream required. The
  request carries no params; the tool's own arguments round-trip in a
  signed `requestState`.
- **Capability gating:** the server reads
  `_meta.io.modelcontextprotocol/clientCapabilities.roots` on every
  call and never asks a client that did not declare roots - it answers
  with a plain-text fallback instead, per the spec's "check for roots
  capability before usage".
- **`file://` only:** the spec requires root URIs to be `file://` in the
  current spec version; any other scheme is rejected with a tool error
  naming the offending URIs. Percent-decoding and Windows drive-letter
  URIs (`file:///C:/...`) are handled.
- **Root boundaries respected (spec security section):** the server
  only reads inside declared roots, writes nowhere, does not follow
  symlinks (`os.walk` never descends them, sizes come from `lstat`),
  and stops at a configurable cap (`ROOTS_MCP_MAX_FILES`, default
  50000) with an explicit `truncated` flag.
- **Nested-root de-duplication:** declaring both `repo/` and
  `repo/sub/` counts each file once - containment is decided on
  `realpath` so equivalent spellings collapse.
- **Conventions:** bad arguments are tool-level `isError` results
  (SEP-1303); malformed protocol input gets JSON-RPC errors; tool names
  follow SEP-986; legacy `initialize` answered alongside stateless
  `server/discover` (SEP-2575).

## Run it

```sh
python3 roots_mcp.py
```

```json
{
  "mcpServers": {
    "roots": {
      "command": "python3",
      "args": ["/path/to/roots_mcp.py"]
    }
  }
}
```

The interactive flow needs a client that declares the `roots`
capability; without it the server says so in plain text instead of
asking.

## Tests

17 stdlib `unittest` tests drive the real server over stdio and against
real temp directories: both handshakes, envelope shape (roots/list with
no params), argument round-trip through `requestState`, capability
gating, argument validation, state tamper, wrong/malformed responses,
basic stats (counts, bytes, extensions, largest), nested-root
de-duplication, multi-root walks, requested-extension counts, non-file
URI rejection, empty roots, missing roots, symlink-loop termination,
and the truncation cap. CI runs them on Ubuntu 24.04 and Windows,
Python 3.9 and 3.13.

```sh
python3 -m unittest -v
```

Honest limits: read-only statistics only - the server never edits the
workspace; `roots/list_changed` notifications are not used (a stats
snapshot can go stale; re-call the tool); walking huge trees is capped,
not streamed; no interop test against a hosted MCP client. Gap note:
GitHub search on 2026-11-08 found no existing repository for an MCP
`roots/list` server implementation in Python.

## Sources

- [MCP specification 2026-07-28 - Roots](https://modelcontextprotocol.io/specification/2026-07-28/client/roots) (capability shape, roots/list, file:// requirement, security guidance)
- [SEP-2322: Multi Round-Trip Requests](https://modelcontextprotocol.io/seps/2322-multi-round-trip-requests) (InputRequiredResult/inputResponses/requestState)
- [SEP-2575: Stateless MCP](https://modelcontextprotocol.io/seps/2575-stateless-mcp), [SEP-1303](https://modelcontextprotocol.io/seps/1303-input-validation-errors-as-tool-execution-errors), [SEP-986](https://modelcontextprotocol.io/seps/986-specify-format-for-tool-names)
- All fetched 2026-11-08.

## License

MIT
