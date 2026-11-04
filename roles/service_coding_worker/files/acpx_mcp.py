#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""acpx-mcp: the coding worker's protocol-mode sessions as MCP tools.

An MCP server (streamable HTTP, JSON responses, stdlib only) that exposes exactly six tools over
worker-sessiond's protocol-mode API, so an MCP client (an agent, a console, an editor) can drive a
coding agent through ACP without being able to do anything else sessiond can do (no stop, no done,
no pr-ready, no raw tmux):

  session_new(agent, cwd, prompt, mcp?)  an ACP session for the card whose worktree IS cwd
  session_prompt(session_id, prompt)     queue a prompt on that session
  session_cancel(session_id)             ACP session/cancel, CONFIRMED by sessiond (effects cease)
  session_list()                         protocol sessions + whether protocol mode is accepted
  session_resume(session_id, prompt?)    continue the PRIOR session (paused, stopped, rebooted)
  session_status(session_id, history?)   live read: pending turns, same_session, last turns

``session_id`` is the card id. ``cwd`` must be a path worker-sessiond created: the
check is sessiond's, not this server's, so there is one place that decides it.

Two credentials, both popped from the environment at start (a child never inherits them):
ACPX_MCP_TOKEN is what an MCP client presents; WORKER_SESSIOND_TOKEN is what this server presents
to sessiond on loopback. A client holding the first cannot use the second's wider API.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional

SERVER_NAME = "acpx-mcp"
SERVER_VERSION = "1.0.0"
# Newest first. A client asking for a version this list has is answered in it; any other gets the
# newest, which the spec allows (the client then decides whether to continue).
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SESSION_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")  # = sessiond's CARD_ID_RE
MAX_BODY = 1_000_000

_ID = {
    "type": "string",
    "description": "The card id (session_new returns it as session_id).",
}
_PROMPT = {"type": "string", "description": "The prompt text.", "maxLength": 100000}

TOOLS: list[dict] = [
    {
        "name": "session_new",
        "description": (
            "Start an ACP coding session (Claude Code or Codex) in the card whose workspace IS "
            "cwd. cwd must be the workspace worker-sessiond returned when the card was created; "
            "any other path is refused. One session per card."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent": {"type": "string", "enum": ["claude", "codex"]},
                "cwd": {"type": "string", "description": "The card's workspace path."},
                "prompt": _PROMPT,
                "mcp": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Extra catalogue MCP server names for the agent.",
                },
            },
            "required": ["agent", "cwd", "prompt"],
            "additionalProperties": False,
        },
    },
    {
        "name": "session_prompt",
        "description": "Queue a prompt on a running session (runs after any turn in flight).",
        "inputSchema": {
            "type": "object",
            "properties": {"session_id": _ID, "prompt": _PROMPT},
            "required": ["session_id", "prompt"],
            "additionalProperties": False,
        },
    },
    {
        "name": "session_cancel",
        "description": (
            "Cancel the session's current work: ACP session/cancel, then confirmed by the "
            "supervisor (turn settled, no tool left running, else the card's container is "
            "killed). Returns within 5 seconds. The session stays resumable."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"session_id": _ID},
            "required": ["session_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "session_list",
        "description": (
            "List protocol sessions, and whether protocol mode has passed its acceptance proof "
            "(preferred_mode)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "session_resume",
        "description": (
            "Continue a session's PRIOR conversation after a cancel, pause, stop or worker "
            "restart, optionally with a prompt. session_status's same_session says whether the "
            "agent really continued it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"session_id": _ID, "prompt": _PROMPT},
            "required": ["session_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "session_status",
        "description": (
            "Read one session live: state, busy, same_session, and with history=N the last N "
            "turn previews."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": _ID,
                "history": {"type": "integer", "minimum": 0, "maximum": 50},
            },
            "required": ["session_id"],
            "additionalProperties": False,
        },
    },
]
TOOL_NAMES = tuple(t["name"] for t in TOOLS)


class ToolError(Exception):
    """A tool call that did not happen; reported to the client as ``isError`` (not a protocol
    error), with the supervisor's status and reason."""

    def __init__(self, message: str, status: int = 400, detail: Any = None):
        super().__init__(message)
        self.status = status
        self.detail = detail


# --------------------------------------------------------------------------- sessiond client

Caller = Callable[[str, str, Optional[dict]], "tuple[int, Any]"]


def http_caller(base_url: str, token: str, timeout: float = 30.0) -> Caller:
    def call(method: str, path: str, body: Optional[dict] = None) -> tuple[int, Any]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(base_url.rstrip("/") + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
                status, raw = r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read().decode()
        except (urllib.error.URLError, OSError) as e:
            raise ToolError(f"worker-sessiond unreachable: {type(e).__name__}", 502)
        try:
            return status, json.loads(raw)
        except ValueError:
            return status, {"error": "worker-sessiond answered non-JSON"}

    return call


def _sid(args: dict) -> str:
    sid = args.get("session_id")
    if not isinstance(sid, str) or not SESSION_ID_RE.match(sid):
        raise ToolError("session_id must match [A-Za-z0-9][A-Za-z0-9_-]{0,63}", 400)
    return urllib.parse.quote(sid, safe="")


def _prompt(args: dict, required: bool = True) -> Optional[str]:
    p = args.get("prompt")
    if p is None and not required:
        return None
    if not isinstance(p, str) or (required and not p.strip()) or len(p) > 100_000:
        raise ToolError("prompt must be a non-empty string up to 100000 characters", 400)
    return p


def route(name: str, args: Any) -> tuple[str, str, Optional[dict]]:
    """(method, sessiond path, body) for one tool call. Pure, so the mapping is tested directly.
    Unknown keys are refused rather than ignored: a typo'd ``promt`` must not start an empty turn.
    """
    if not isinstance(args, dict):
        raise ToolError("arguments must be an object", 400)
    tool = next((t for t in TOOLS if t["name"] == name), None)
    if tool is None:
        raise ToolError(f"unknown tool {name!r}", 404)
    extra = sorted(set(args) - set(tool["inputSchema"]["properties"]))
    if extra:
        raise ToolError(f"unknown argument(s): {', '.join(extra)}", 400)
    if name == "session_new":
        agent, cwd, mcp = args.get("agent"), args.get("cwd"), args.get("mcp", [])
        if agent not in ("claude", "codex"):
            raise ToolError("agent must be claude or codex", 400)
        if not isinstance(cwd, str) or not cwd:
            raise ToolError("cwd must be the card's workspace path", 400)
        if not isinstance(mcp, list) or not all(isinstance(m, str) for m in mcp):
            raise ToolError("mcp must be a list of catalogue names", 400)
        body = {"agent": agent, "cwd": cwd, "prompt": _prompt(args), "mcp": mcp}
        return "POST", "/v1/acp/sessions", body
    if name == "session_prompt":
        return (
            "POST",
            f"/v1/sessions/{_sid(args)}/acp/prompt",
            {"prompt": _prompt(args)},
        )
    if name == "session_cancel":
        return "POST", f"/v1/sessions/{_sid(args)}/acp/cancel", {}
    if name == "session_list":
        return "GET", "/v1/acp", None
    if name == "session_resume":
        p = _prompt(args, required=False)
        return (
            "POST",
            f"/v1/sessions/{_sid(args)}/acp/resume",
            ({"prompt": p} if p else {}),
        )
    # session_status
    h = args.get("history", 0)
    if not isinstance(h, int) or isinstance(h, bool) or not 0 <= h <= 50:
        raise ToolError("history must be an integer 0..50", 400)
    return "GET", f"/v1/sessions/{_sid(args)}/acp?history={h}", None


def call_tool(caller: Caller, name: str, args: Any) -> dict:
    """An MCP ``tools/call`` result. Every outcome is a RESULT (never a raised protocol error), and
    a refusal carries ``isError: true`` with the supervisor's status: a client that only reads the
    text must still see that nothing happened."""
    try:
        method, path, body = route(name, args)
        status, doc = caller(method, path, body)
        if not 200 <= status < 300:
            raise ToolError(
                str(doc.get("error") if isinstance(doc, dict) and doc.get("error") else status),
                status,
                doc,
            )
        out = {"ok": True, **(doc if isinstance(doc, dict) else {"result": doc})}
        return {
            "content": [{"type": "text", "text": json.dumps(out)}],
            "structuredContent": out,
            "isError": False,
        }
    except ToolError as e:
        out = {"ok": False, "status": e.status, "error": str(e)}
        if isinstance(e.detail, dict):
            out["detail"] = e.detail
        return {
            "content": [{"type": "text", "text": json.dumps(out)}],
            "structuredContent": out,
            "isError": True,
        }


# --------------------------------------------------------------------------- JSON-RPC


def _err(mid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def handle_rpc(caller: Caller, msg: Any) -> Optional[dict]:
    """One JSON-RPC message -> its response, or None for a notification."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
        return _err(msg.get("id") if isinstance(msg, dict) else None, -32600, "invalid request")
    mid, method, params = msg.get("id"), msg["method"], msg.get("params") or {}
    if "id" not in msg:
        return None  # notifications (initialized, cancelled) need no answer
    if method == "initialize":
        asked = params.get("protocolVersion") if isinstance(params, dict) else None
        version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return {
            "jsonrpc": "2.0",
            "id": mid,
            "result": {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": (
                    "Coding-agent sessions on the hermes worker over ACP. Create the card "
                    "with worker-sessiond first and pass its workspace as cwd."
                ),
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            return _err(mid, -32602, "tools/call needs a tool name")
        if params["name"] not in TOOL_NAMES:
            return _err(mid, -32602, f"unknown tool {params['name']!r}")
        return {
            "jsonrpc": "2.0",
            "id": mid,
            "result": call_tool(caller, params["name"], params.get("arguments") or {}),
        }
    return _err(mid, -32601, f"method {method!r} not found")


def make_handler(caller: Caller, token: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = SERVER_NAME

        def log_message(self, fmt: str, *args: Any) -> None:  # no bodies, no headers
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

        def _send(self, status: int, body: Any = None) -> None:
            data = b"" if body is None else json.dumps(body).encode()
            self.send_response(status)
            if body is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authed(self) -> bool:
            got = self.headers.get("Authorization", "")
            return hmac.compare_digest(got.encode(), f"Bearer {token}".encode())

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/healthz":
                return self._send(200, {"ok": True})
            if self.path.split("?")[0] == "/mcp":
                # no server-initiated stream: the spec's answer for that is 405
                self.send_response(405)
                self.send_header("Allow", "POST")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            return self._send(404, {"error": "not found"})

        def do_DELETE(self) -> None:  # noqa: N802
            self._send(405, {"error": "sessions are stateless here"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path.split("?")[0] != "/mcp":
                return self._send(404, {"error": "not found"})
            if self.headers.get("Origin"):
                # no browser is a client of this server; an Origin is a DNS-rebinding attempt
                return self._send(403, {"error": "browser origins are refused"})
            if not self._authed():
                return self._send(401, {"error": "unauthorised"})
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > MAX_BODY:
                return self._send(400, _err(None, -32600, "body missing or too large"))
            try:
                msg = json.loads(self.rfile.read(n))
            except ValueError:
                return self._send(400, _err(None, -32700, "parse error"))
            try:
                if isinstance(msg, list):
                    out = [r for r in (handle_rpc(caller, m) for m in msg) if r is not None]
                    return self._send(200, out) if out else self._send(202)
                res = handle_rpc(caller, msg)
                return self._send(202) if res is None else self._send(200, res)
            except Exception as e:  # never a traceback (paths, env) to the client
                sys.stderr.write(f"unhandled: {type(e).__name__}\n")
                return self._send(500, _err(None, -32603, "internal error"))

    return Handler


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7691)
    ap.add_argument("--sessiond-url", default="http://127.0.0.1:7690")
    a = ap.parse_args(argv)
    token = os.environ.pop("ACPX_MCP_TOKEN", "")
    sd_token = os.environ.pop("WORKER_SESSIOND_TOKEN", "")
    if len(token) < 16 or len(sd_token) < 16:
        sys.stderr.write(
            "ACPX_MCP_TOKEN and WORKER_SESSIOND_TOKEN must both be set (16+ characters)\n"
        )
        return 2
    if hmac.compare_digest(token, sd_token):
        sys.stderr.write("ACPX_MCP_TOKEN must differ from WORKER_SESSIOND_TOKEN\n")
        return 2
    handler = make_handler(http_caller(a.sessiond_url, sd_token), token)
    ThreadingHTTPServer((a.bind, a.port), handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
