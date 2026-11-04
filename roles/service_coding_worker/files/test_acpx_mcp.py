#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for acpx_mcp.py. Stdlib only, no network beyond loopback.

The end-to-end path (acpx-mcp -> real sessiond -> real acpx) is in test_worker_sessiond_acp.py's
LIVE group; this file proves the MCP surface itself: the tool list is exactly the six tools, each
tool maps to one sessiond call and nothing else, unknown arguments and malformed ids are refused
BEFORE sessiond is asked, a sessiond refusal reaches the client as isError (never as success), and
the HTTP layer refuses no token, a wrong token and a browser Origin. Each refusal is paired with a
near-miss that must pass.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import acpx_mcp as am  # noqa: E402

TOKEN = "t" * 32


class Recorder:
    def __init__(self, status=200, doc=None):
        self.calls = []
        self.status = status
        self.doc = {"ok": 1} if doc is None else doc

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        return self.status, self.doc


class TestRoute(unittest.TestCase):
    def test_exactly_the_six_tools(self):
        self.assertEqual(
            am.TOOL_NAMES,
            (
                "session_new",
                "session_prompt",
                "session_cancel",
                "session_list",
                "session_resume",
                "session_status",
            ),
        )
        for t in am.TOOLS:
            self.assertEqual(t["inputSchema"]["type"], "object")
            self.assertFalse(t["inputSchema"]["additionalProperties"])

    def test_each_tool_maps_to_one_sessiond_call(self):
        cases = {
            ("session_new", '{"agent":"claude","cwd":"/w/c1","prompt":"p"}'): (
                "POST",
                "/v1/acp/sessions",
                {"agent": "claude", "cwd": "/w/c1", "prompt": "p", "mcp": []},
            ),
            ("session_prompt", '{"session_id":"c1","prompt":"p"}'): (
                "POST",
                "/v1/sessions/c1/acp/prompt",
                {"prompt": "p"},
            ),
            ("session_cancel", '{"session_id":"c1"}'): (
                "POST",
                "/v1/sessions/c1/acp/cancel",
                {},
            ),
            ("session_list", "{}"): ("GET", "/v1/acp", None),
            ("session_resume", '{"session_id":"c1"}'): (
                "POST",
                "/v1/sessions/c1/acp/resume",
                {},
            ),
            ("session_resume", '{"session_id":"c1","prompt":"go"}'): (
                "POST",
                "/v1/sessions/c1/acp/resume",
                {"prompt": "go"},
            ),
            ("session_status", '{"session_id":"c1","history":3}'): (
                "GET",
                "/v1/sessions/c1/acp?history=3",
                None,
            ),
        }
        for (name, args), want in cases.items():
            self.assertEqual(am.route(name, json.loads(args)), want, name)

    def test_refused_before_sessiond_is_asked(self):
        bad = [
            ("session_new", {"agent": "gpt", "cwd": "/w", "prompt": "p"}),
            ("session_new", {"agent": "claude", "cwd": "", "prompt": "p"}),
            ("session_new", {"agent": "claude", "cwd": "/w", "prompt": "  "}),
            (
                "session_new",
                {"agent": "claude", "cwd": "/w", "prompt": "p", "mcp": "x"},
            ),
            ("session_prompt", {"session_id": "c1", "promt": "typo"}),
            ("session_prompt", {"session_id": "../c1", "prompt": "p"}),
            ("session_cancel", {"session_id": "c1/../../v1/sessions"}),
            ("session_cancel", {"session_id": "-rf"}),
            ("session_status", {"session_id": "c1", "history": 99}),
            ("session_status", {"session_id": "c1", "history": True}),
            ("session_delete", {"session_id": "c1"}),
        ]
        for name, args in bad:
            rec = Recorder()
            res = am.call_tool(rec, name, args)
            self.assertTrue(res["isError"], (name, args))
            self.assertEqual(rec.calls, [], (name, args))

    def test_a_sessiond_refusal_is_an_error_result(self):
        rec = Recorder(403, {"error": "cwd is not a worktree worker-sessiond created"})
        res = am.call_tool(rec, "session_new", {"agent": "claude", "cwd": "/tmp", "prompt": "p"})
        self.assertTrue(res["isError"])
        self.assertEqual(res["structuredContent"]["status"], 403)
        self.assertIn("worktree", res["structuredContent"]["error"])
        # near-miss: a 2xx is a success result carrying sessiond's document
        ok = am.call_tool(Recorder(200, {"cancelled": True}), "session_cancel", {"session_id": "c"})
        self.assertFalse(ok["isError"])
        self.assertTrue(ok["structuredContent"]["cancelled"])
        self.assertEqual(json.loads(ok["content"][0]["text"])["cancelled"], True)

    def test_a_failed_cancel_detail_is_passed_through(self):
        rec = Recorder(500, {"cancelled": True, "effects_ceased": False})
        res = am.call_tool(rec, "session_cancel", {"session_id": "c1"})
        self.assertTrue(res["isError"])
        self.assertFalse(res["structuredContent"]["detail"]["effects_ceased"])


class TestCatalogueEntry(unittest.TestCase):
    """A client's MCP catalogue entry for acpx-mcp pins the tool list it may use; it must be THIS
    server's list, or a client would be told about tools that do not exist (or miss one that does).
    ACPX_MCP_CATALOGUE_FILE names that catalogue (unset: skipped; a missing file fails). Read with
    a regex (this suite is stdlib-only): the entry keeps tools_include on one line."""

    CAT = os.environ.get("ACPX_MCP_CATALOGUE_FILE", "")

    @unittest.skipUnless(CAT, "ACPX_MCP_CATALOGUE_FILE not set: no client catalogue to check")
    def test_tools_include_equals_the_server(self):
        import re

        text = Path(self.CAT).read_text()
        entry = text[text.index("- name: acpx-mcp") :]
        m = re.search(r"(?m)^    tools_include: \[([^\]]*)\]", entry)
        self.assertIsNotNone(m, "acpx-mcp entry has no one-line tools_include")
        self.assertEqual(tuple(t.strip() for t in m.group(1).split(",")), am.TOOL_NAMES)


class TestRpc(unittest.TestCase):
    def test_initialize_negotiates(self):
        r = am.handle_rpc(
            Recorder(),
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-03-26"},
            },
        )
        self.assertEqual(r["result"]["protocolVersion"], "2025-03-26")
        r = am.handle_rpc(
            Recorder(),
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "1999-01-01"},
            },
        )
        self.assertEqual(r["result"]["protocolVersion"], am.PROTOCOL_VERSIONS[0])
        self.assertIn("tools", r["result"]["capabilities"])

    def test_notification_gets_no_answer_and_unknowns_are_errors(self):
        self.assertIsNone(
            am.handle_rpc(Recorder(), {"jsonrpc": "2.0", "method": "notifications/initialized"})
        )
        self.assertEqual(
            am.handle_rpc(Recorder(), {"jsonrpc": "2.0", "id": 2, "method": "x"})["error"]["code"],
            -32601,
        )
        self.assertEqual(am.handle_rpc(Recorder(), {"id": 2})["error"]["code"], -32600)
        r = am.handle_rpc(
            Recorder(),
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "nope"},
            },
        )
        self.assertEqual(r["error"]["code"], -32602)

    def test_tools_list(self):
        r = am.handle_rpc(Recorder(), {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual([t["name"] for t in r["result"]["tools"]], list(am.TOOL_NAMES))


class TestHttp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rec = Recorder(200, {"sessions": []})
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), am.make_handler(cls.rec, TOKEN))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def post(self, body, headers=None):
        h = {"Content-Type": "application/json"}
        h.update(headers or {})
        req = urllib.request.Request(
            self.url + "/mcp", data=json.dumps(body).encode(), method="POST", headers=h
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = r.read()
                return r.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            return e.code, None

    LIST = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "session_list", "arguments": {}},
    }

    def test_auth(self):
        self.assertEqual(self.post(self.LIST)[0], 401)
        self.assertEqual(self.post(self.LIST, {"Authorization": "Bearer wrong"})[0], 401)
        status, doc = self.post(self.LIST, {"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(status, 200)
        self.assertFalse(doc["result"]["isError"])

    def test_a_browser_origin_is_refused_even_with_the_token(self):
        h = {"Authorization": f"Bearer {TOKEN}", "Origin": "http://evil.example"}
        self.assertEqual(self.post(self.LIST, h)[0], 403)

    def test_notification_is_202_and_batch_answers_each_request(self):
        auth = {"Authorization": f"Bearer {TOKEN}"}
        self.assertEqual(
            self.post({"jsonrpc": "2.0", "method": "notifications/initialized"}, auth)[0],
            202,
        )
        status, doc = self.post(
            [
                {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            ],
            auth,
        )
        self.assertEqual(status, 200)
        self.assertEqual([d["id"] for d in doc], [1, 2])

    def test_get_is_405_and_healthz_is_open(self):
        req = urllib.request.Request(self.url + "/mcp", method="GET")
        with self.assertRaises(urllib.error.HTTPError) as c:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(c.exception.code, 405)
        with urllib.request.urlopen(self.url + "/healthz", timeout=5) as r:
            self.assertEqual(json.loads(r.read()), {"ok": True})


class TestMain(unittest.TestCase):
    def test_refuses_to_start_without_two_distinct_tokens(self):
        import os

        for env in (
            {},
            {"ACPX_MCP_TOKEN": TOKEN},
            {"ACPX_MCP_TOKEN": TOKEN, "WORKER_SESSIOND_TOKEN": TOKEN},
        ):
            old = dict(os.environ)
            os.environ.pop("ACPX_MCP_TOKEN", None)
            os.environ.pop("WORKER_SESSIOND_TOKEN", None)
            os.environ.update(env)
            try:
                self.assertEqual(am.main(["--port", "0"]), 2, env.keys())
            finally:
                os.environ.clear()
                os.environ.update(old)


if __name__ == "__main__":
    unittest.main(verbosity=1)
