#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""A stub of the slice of Slack's Web API that slack_channels uses, for molecule and the unit test.

Not a mock of the module's calls: it holds a workspace (channels with creator, members, purpose,
topic, archived) and enforces the rules the real API does that the role depends on:

  * every answer is HTTP 200 with {"ok": false, "error": ...} on failure, never a 4xx;
  * each method checks its bot scope and answers missing_scope with needed/provided;
  * invite, rename, setPurpose and setTopic need the caller to be a MEMBER (not_in_channel);
  * rename is allowed only to the channel's creator or an admin (not_authorized), which is the
    restriction that decides whether a bot can rename a channel a human made;
  * conversations.list pages two channels at a time, so a reader that ignores next_cursor misses
    channels.

An optional "user" in the state is a second identity, a workspace admin's USER token
(ADMIN_TOKEN), whose write methods need channels:write and who passes the rename rule as an admin.

GET /_state returns the workspace for verify; nothing else is outside Slack's shape.

    stub_slack.py <port> <state.json>     # state: {"bot": "U...", "scopes": [...], "admin": false,
                                          #         "channels": [{"id","name","creator","members",...}]}
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

TOKEN = "xoxb-stub-token"
NEEDS = {
    "auth.test": None,
    "conversations.list": "channels:read",
    "conversations.members": "channels:read",
    "conversations.info": "channels:read",
    "conversations.create": "channels:manage",
    "conversations.rename": "channels:manage",
    "conversations.join": "channels:join",
    "conversations.invite": "channels:manage",
    "conversations.setPurpose": "channels:manage",
    "conversations.setTopic": "channels:manage",
    "conversations.history": "channels:history",
    "chat.postMessage": "chat:write",
    "chat.update": "chat:write",
    "chat.delete": "chat:write",
    "pins.add": "pins:write",
    "pins.remove": "pins:write",
    "pins.list": "pins:read",
    "bookmarks.list": "bookmarks:read",
    "bookmarks.add": "bookmarks:write",
    "bookmarks.edit": "bookmarks:write",
}
# A USER token (xoxp-) holds user scopes: the write methods take channels:write instead.
NEEDS_USER = dict(
    NEEDS,
    **{m: "channels:write" for m, s in NEEDS.items() if s == "channels:manage"},
)
ADMIN_TOKEN = "xoxp-stub-admin"


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _linked(blocks: list, channels: list) -> list:
    """Like Slack: `#name` of an existing channel and a bare URL in mrkdwn come back as links."""
    import copy as _copy
    import re as _re

    out = _copy.deepcopy(blocks)
    for b in out:
        t = (b.get("text") or {}).get("text")
        if t is None:
            continue
        for c in channels:
            t = _re.sub(r"(?<![<\w])#" + _re.escape(c["name"]) + r"(?![\w-])", f"<#{c['id']}>", t)
        t = _re.sub(r"(?<![<|])(https?://[^\s<>|]+)", r"<\1>", t)
        b["text"]["text"] = t
    return out


def _flat(p: dict) -> str:
    # Like Slack: with blocks present the returned `text` has its newlines flattened.
    return p["text"].replace("\n", " ") if p.get("blocks") else p["text"]


class Workspace:
    def __init__(self, state: dict):
        self.lock = threading.Lock()
        self.bot = state["bot"]
        self.scopes = list(state["scopes"])
        self.admin = bool(state.get("admin", False))
        # optional user token: {"id", "scopes", "admin"} -- the workspace admin's own identity
        self.user = state.get("user")
        self.channels = []
        for c in state["channels"]:
            self.channels.append(
                {
                    "id": c["id"],
                    "name": c["name"],
                    "creator": c.get("creator", "UOWNER"),
                    "members": list(c.get("members", [])),
                    "purpose": c.get("purpose", ""),
                    "topic": c.get("topic", ""),
                    "is_archived": bool(c.get("is_archived", False)),
                    # newest first, as conversations.history returns them; text stored ESCAPED
                    # the way Slack stores it (&amp; &lt; &gt;), so a reader that compares raw text
                    # sees drift that is not there
                    "messages": list(c.get("messages", [])),
                    "pins": list(c.get("pins", [])),
                    "bookmarks": list(c.get("bookmarks", [])),
                }
            )
        self.seq = 100
        self.ts = 1000000

    def view(self, c: dict, who: str | None = None) -> dict:
        return {
            "id": c["id"],
            "name": c["name"],
            "is_archived": c["is_archived"],
            "is_member": (who or self.bot) in c["members"],
            "creator": c["creator"],
            "purpose": {"value": c["purpose"]},
            "topic": {"value": c["topic"]},
        }

    def find(self, cid: str):
        return next((c for c in self.channels if c["id"] == cid), None)

    def caller(self, as_user: bool) -> tuple[str, list, bool, dict]:
        if as_user:
            return self.user["id"], self.user["scopes"], bool(self.user.get("admin")), NEEDS_USER
        return self.bot, self.scopes, self.admin, NEEDS

    def handle(self, method: str, p: dict, as_user: bool = False) -> dict:
        who, scopes, admin, needs = self.caller(as_user)
        need = needs.get(method, "unknown")
        if need == "unknown":
            return {"ok": False, "error": "unknown_method"}
        if need and need not in scopes:
            return {
                "ok": False,
                "error": "missing_scope",
                "needed": need,
                "provided": ",".join(scopes),
            }
        if method == "auth.test":
            return {"ok": True, "team": "stub", "team_id": "TSTUB", "user_id": who}
        if method == "conversations.list":
            live = [
                c
                for c in self.channels
                if p.get("exclude_archived") != "true" or not c["is_archived"]
            ]
            start = int(p.get("cursor") or 0)
            page = live[start : start + 2]
            nxt = str(start + 2) if start + 2 < len(live) else ""
            return {
                "ok": True,
                "channels": [self.view(c) for c in page],
                "response_metadata": {"next_cursor": nxt},
            }
        if method == "conversations.create":
            if any(c["name"] == p["name"] for c in self.channels):
                return {"ok": False, "error": "name_taken"}
            self.seq += 1
            c = {
                "id": f"CNEW{self.seq}",
                "name": p["name"],
                "creator": who,
                "members": [who],
                "purpose": "",
                "topic": "",
                "is_archived": False,
                "messages": [],
                "pins": [],
                "bookmarks": [],
            }
            self.channels.append(c)
            return {"ok": True, "channel": self.view(c)}
        c = self.find(p.get("channel") or p.get("channel_id") or "")
        if c is None:
            return {"ok": False, "error": "channel_not_found"}
        if method == "conversations.members":
            return {
                "ok": True,
                "members": list(c["members"]),
                "response_metadata": {"next_cursor": ""},
            }
        if method == "conversations.info":
            return {"ok": True, "channel": self.view(c)}
        if method == "bookmarks.list":
            return {"ok": True, "bookmarks": [dict(b) for b in c["bookmarks"]]}
        if method in ("conversations.history", "pins.list") and who not in c["members"]:
            return {"ok": False, "error": "not_in_channel"}
        if method == "conversations.history":
            return {
                "ok": True,
                "messages": [dict(m) for m in c["messages"]][: int(p.get("limit", 100))],
            }
        if method == "pins.list":
            items = [
                {"type": "message", "message": dict(m)}
                for m in c["messages"]
                if m["ts"] in c["pins"]
            ]
            return {"ok": True, "items": items}
        if c["is_archived"]:
            return {"ok": False, "error": "is_archived"}
        if method == "conversations.join":
            if who not in c["members"]:
                c["members"].append(who)
            return {"ok": True, "channel": self.view(c)}
        if who not in c["members"]:
            return {"ok": False, "error": "not_in_channel"}
        if method == "conversations.rename":
            if c["creator"] != who and not admin:
                return {"ok": False, "error": "not_authorized"}
            if any(o["name"] == p["name"] for o in self.channels if o is not c):
                return {"ok": False, "error": "name_taken"}
            c["name"] = p["name"]
        elif method == "conversations.invite":
            for u in p["users"].split(","):
                if u in c["members"]:
                    return {"ok": False, "error": "already_in_channel"}
                c["members"].append(u)
        elif method == "conversations.setPurpose":
            c["purpose"] = _escape(p["purpose"])
        elif method == "conversations.setTopic":
            c["topic"] = _escape(p["topic"])
        elif method == "chat.postMessage":
            self.ts += 1
            m = {
                "ts": f"{self.ts}.000100",
                "user": who,
                "text": _escape(_flat(p)),
                "blocks": _linked(p.get("blocks", []), self.channels),
            }
            if who == self.bot:
                m["bot_id"] = "BSTUB"
            c["messages"].insert(0, m)
            return {"ok": True, "ts": m["ts"], "channel": c["id"]}
        elif method in ("chat.update", "chat.delete"):
            m = next((m for m in c["messages"] if m["ts"] == p["ts"]), None)
            if m is None:
                return {"ok": False, "error": "message_not_found"}
            if m["user"] != who:
                return {
                    "ok": False,
                    "error": (
                        "cant_update_message" if method == "chat.update" else "cant_delete_message"
                    ),
                }
            if method == "chat.update":
                m.update(text=_escape(_flat(p)), blocks=_linked(p.get("blocks", []), self.channels))
            else:
                c["messages"].remove(m)
                if m["ts"] in c["pins"]:
                    c["pins"].remove(m["ts"])
        elif method == "pins.add":
            if p["timestamp"] in c["pins"]:
                return {"ok": False, "error": "already_pinned"}
            if not any(m["ts"] == p["timestamp"] for m in c["messages"]):
                return {"ok": False, "error": "message_not_found"}
            c["pins"].append(p["timestamp"])
        elif method == "pins.remove":
            if p["timestamp"] not in c["pins"]:
                return {"ok": False, "error": "not_pinned"}
            c["pins"].remove(p["timestamp"])
        elif method == "bookmarks.add":
            self.seq += 1
            c["bookmarks"].append(
                {"id": f"Bk{self.seq}", "title": p["title"], "link": p["link"], "type": p["type"]}
            )
        elif method == "bookmarks.edit":
            b = next((b for b in c["bookmarks"] if b["id"] == p["bookmark_id"]), None)
            if b is None:
                return {"ok": False, "error": "not_found"}
            b.update(title=p["title"], link=p["link"])
        return {"ok": True, "channel": self.view(c)}


def make_server(port: int, state: dict) -> ThreadingHTTPServer:
    ws = Workspace(state)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # quiet
            pass

        def reply(self, body: dict, headers: dict | None = None):
            raw = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def dispatch(self, params: dict):
            method = urlparse(self.path).path.rsplit("/", 1)[-1]
            if method == "_state":
                with ws.lock:
                    return self.reply({"ok": True, "channels": [dict(c) for c in ws.channels]})
            auth = self.headers.get("Authorization")
            as_user = bool(ws.user) and auth == f"Bearer {ADMIN_TOKEN}"
            if auth != f"Bearer {TOKEN}" and not as_user:
                return self.reply({"ok": False, "error": "invalid_auth"})
            with ws.lock:
                body = ws.handle(method, params, as_user)
            scopes = ws.user["scopes"] if as_user else ws.scopes
            extra = {"x-oauth-scopes": ",".join(scopes)} if method == "auth.test" else {}
            self.reply(body, extra)

        def do_GET(self):
            self.dispatch({k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()})

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            self.dispatch(json.loads(self.rfile.read(n) or b"{}"))

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


if __name__ == "__main__":
    with open(sys.argv[2]) as fh:
        make_server(int(sys.argv[1]), json.load(fh)).serve_forever()
