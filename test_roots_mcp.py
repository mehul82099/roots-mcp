import base64
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

SERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "roots_mcp.py")
CAPS = {"io.modelcontextprotocol/clientCapabilities": {"roots": {}}}


class Server:
    def __init__(self, env_extra=None):
        env = dict(os.environ)
        env.update(env_extra or {})
        self.proc = subprocess.Popen(
            [sys.executable, SERVER], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.next_id = 0

    def request(self, method, params=None, meta=None):
        self.next_id += 1
        msg = {"jsonrpc": "2.0", "id": self.next_id, "method": method,
               "params": params or {}}
        if meta is not None:
            msg["_meta"] = meta
        self.proc.stdin.write(json.dumps(msg).encode() + b"\n")
        self.proc.stdin.flush()
        reply = json.loads(self.proc.stdout.readline().decode())
        assert reply.get("id") == self.next_id, f"bad id: {reply}"
        return reply

    def result(self, method, params=None, meta=None):
        msg = self.request(method, params, meta)
        assert "result" in msg, f"error from {method}: {msg}"
        return msg["result"]

    def call(self, arguments=None, meta=CAPS):
        return self.result("tools/call",
                           {"name": "workspace_stats",
                            "arguments": arguments or {}}, meta)

    def close(self):
        self.proc.kill()
        self.proc.wait(timeout=5)


def roots_result(paths):
    return {"roots": [{"uri": pathlib.Path(p).as_uri(),
                       "name": pathlib.Path(p).name} for p in paths]}


class Workspace:
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="roots-mcp-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.s = Server()
        self.addCleanup(self.s.close)

    def make(self, rel, content=b"x"):
        full = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as fh:
            fh.write(content)
        return full

    def uri(self, rel=""):
        return pathlib.Path(os.path.join(self.tmp, rel)).as_uri()


class HandshakeTests(unittest.TestCase):
    def test_handshakes_and_errors(self):
        s = Server()
        try:
            d = s.result("server/discover")
            self.assertEqual(d["protocolVersion"], "2026-07-28")
            self.assertEqual(d["serverInfo"]["name"], "roots-mcp")
            r = s.result("initialize", {"protocolVersion": "2025-06-18"})
            self.assertEqual(r["protocolVersion"], "2025-06-18")
            self.assertEqual(s.request("nope")["error"]["code"], -32601)
            self.assertEqual(s.request("tools/call", {"name": "nope"})
                             ["error"]["code"], -32602)
        finally:
            s.close()


class EnvelopeTests(Workspace, unittest.TestCase):
    def test_first_call_envelope(self):
        r = self.s.call()
        self.assertEqual(r["resultType"], "input_required")
        self.assertEqual(list(r["inputRequests"]), ["roots"])
        self.assertEqual(r["inputRequests"]["roots"]["method"],
                         "roots/list")
        self.assertNotIn("params", r["inputRequests"]["roots"])
        payload = json.loads(base64.urlsafe_b64decode(
            r["requestState"].split(".", 1)[0] + "=="))
        self.assertEqual(payload, {"extensions": None})

    def test_args_roundtrip_in_state(self):
        r = self.s.call({"extensions": [".py"]})
        payload = json.loads(base64.urlsafe_b64decode(
            r["requestState"].split(".", 1)[0] + "=="))
        self.assertEqual(payload["extensions"], [".py"])

    def test_no_capability_no_ask(self):
        for meta in (None, {}, {"io.modelcontextprotocol/"
                                "clientCapabilities": {}}):
            r = self.s.call(meta=meta)
            self.assertEqual(r["resultType"], "complete")
            self.assertNotIn("inputRequests", r)
            self.assertEqual(r["structuredContent"]["roots"],
                             "unsupported")

    def test_bad_extensions_arg(self):
        for bad in (["py"], ["."], [".toolongextensionname"], [1],
                    "py", [".py"] * 51):
            r = self.s.call({"extensions": bad})
            self.assertTrue(r.get("isError"), f"accepted {bad!r}")

    def test_tampered_state(self):
        r1 = self.s.call()
        forged = r1["requestState"].split(".", 1)[0] + "." + "0" * 64
        msg = self.s.request("tools/call", {
            "name": "workspace_stats",
            "arguments": {"inputResponses": {"roots": {"roots": []}},
                          "requestState": forged}}, CAPS)
        self.assertEqual(msg["error"]["code"], -32602)

    def test_wrong_response_key(self):
        r1 = self.s.call()
        msg = self.s.request("tools/call", {
            "name": "workspace_stats",
            "arguments": {"inputResponses": {"wrong": {"roots": []}},
                          "requestState": r1["requestState"]}}, CAPS)
        self.assertEqual(msg["error"]["code"], -32602)

    def test_malformed_roots_result(self):
        r1 = self.s.call()
        for bad in ({"roots": "x"}, {"roots": [{}]},
                    {"roots": [{"uri": 5}]}, "string"):
            msg = self.s.request("tools/call", {
                "name": "workspace_stats",
                "arguments": {"inputResponses": {"roots": bad},
                              "requestState": r1["requestState"]}}, CAPS)
            self.assertIn("error", msg, f"accepted {bad!r}")


class WalkTests(Workspace, unittest.TestCase):
    def run_stats(self, paths, args=None):
        r1 = self.s.call(args or {})
        return self.s.call({
            "inputResponses": {"roots": roots_result(paths)},
            "requestState": r1["requestState"]})

    def test_basic_stats(self):
        self.make("src/app.py", b"print(1)\n")
        self.make("src/util.py", b"print(2)\n")
        self.make("docs/readme.md", b"# hi\n")
        self.make("big.bin", b"z" * 4096)
        r = self.run_stats([self.tmp])
        self.assertEqual(r["resultType"], "complete")
        stats = r["structuredContent"]["stats"]
        self.assertEqual(stats["total_files"], 4)
        expected = sum(os.path.getsize(os.path.join(d, f))
                       for d, _dn, fn in os.walk(self.tmp) for f in fn)
        self.assertEqual(stats["total_bytes"], expected)
        exts = {e["extension"]: e["files"]
                for e in stats["top_extensions"]}
        self.assertEqual(exts[".py"], 2)
        self.assertEqual(exts[".md"], 1)
        self.assertEqual(stats["largest_files"][0]["bytes"], 4096)
        self.assertFalse(stats["truncated"])

    def test_nested_roots_deduped(self):
        self.make("a/x.py")
        self.make("a/b/y.py")
        inner = os.path.join(self.tmp, "a", "b")
        outer = os.path.join(self.tmp, "a")
        r = self.run_stats([inner, outer])
        stats = r["structuredContent"]["stats"]
        self.assertEqual(stats["total_files"], 2)  # not 3
        self.assertEqual(stats["roots_skipped_as_nested"], 1)
        self.assertEqual(stats["roots_walked"], [outer])

    def test_multiple_roots(self):
        self.make("one/x.py")
        self.make("two/y.py")
        r = self.run_stats([os.path.join(self.tmp, "one"),
                            os.path.join(self.tmp, "two")])
        stats = r["structuredContent"]["stats"]
        self.assertEqual(stats["total_files"], 2)
        self.assertEqual(len(stats["roots_walked"]), 2)

    def test_requested_extensions(self):
        self.make("x.py")
        self.make("y.py")
        self.make("z.md")
        r = self.run_stats([self.tmp], {"extensions": [".py", ".txt"]})
        stats = r["structuredContent"]["stats"]
        got = {e["extension"]: e["files"]
               for e in stats["requested_extensions"]}
        self.assertEqual(got, {".py": 2, ".txt": 0})

    def test_non_file_uri_rejected(self):
        r1 = self.s.call()
        r = self.s.call({
            "inputResponses": {"roots": {"roots": [
                {"uri": "https://example.com/repo"}]}},
            "requestState": r1["requestState"]})
        self.assertTrue(r["isError"])
        self.assertIn("file://", r["content"][0]["text"])

    def test_empty_roots(self):
        r = self.run_stats([])
        self.assertEqual(r["structuredContent"]["stats"], None)
        self.assertIn("no roots", r["content"][0]["text"])

    def test_missing_root_skipped(self):
        self.make("real/x.py")
        r = self.run_stats([os.path.join(self.tmp, "real"),
                            os.path.join(self.tmp, "ghost")])
        stats = r["structuredContent"]["stats"]
        self.assertEqual(stats["total_files"], 1)
        self.assertEqual(len(stats["skipped_roots"]), 1)

    @unittest.skipUnless(hasattr(os, "symlink"), "needs symlink support")
    def test_symlink_loop_terminates(self):
        self.make("d/x.py")
        os.symlink(os.path.join(self.tmp, "d"),
                   os.path.join(self.tmp, "d", "loop"))
        r = self.run_stats([self.tmp])
        self.assertEqual(r["structuredContent"]["stats"]["total_files"], 1)


class CapTests(unittest.TestCase):
    def test_file_cap_truncates(self):
        tmp = tempfile.mkdtemp(prefix="roots-mcp-cap-")
        self.addCleanup(shutil.rmtree, tmp, True)
        for i in range(8):
            with open(os.path.join(tmp, f"f{i}.py"), "w") as fh:
                fh.write("x")
        s = Server({"ROOTS_MCP_MAX_FILES": "3"})
        try:
            r1 = s.result("tools/call", {"name": "workspace_stats",
                                         "arguments": {}}, CAPS)
            r2 = s.result("tools/call", {
                "name": "workspace_stats",
                "arguments": {
                    "inputResponses": {"roots": roots_result([tmp])},
                    "requestState": r1["requestState"]}}, CAPS)
            stats = r2["structuredContent"]["stats"]
            self.assertTrue(stats["truncated"])
            self.assertEqual(stats["total_files"], 3)
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()
