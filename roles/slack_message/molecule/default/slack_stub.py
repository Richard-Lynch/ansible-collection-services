# SPDX-License-Identifier: GPL-3.0-or-later
"""A local stand-in for the five Slack Web API methods slack_message calls, plus the upload URL.

Stdlib only. Every request is appended to a JSONL log so verify.yml can assert what the ROLE
sent (channel, comment, the uploaded bytes' sha256), not what this stub answered. Behaviour that
a case needs to provoke is keyed on the ids the case sends:
  user UNOTFOUND   -> conversations.open answers {"ok": false, "error": "user_not_found"}
  channel CNOSHARE -> completeUploadExternal is ok, but files.info never shows the share
A bearer other than stub-token-credential answers {"ok": false, "error": "invalid_auth"}.

It binds an ephemeral port and writes it to --port-file, so a leftover stub from an earlier run
can never answer for this one; POST /shutdown stops it, and --lifetime is the backstop.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = "stub-token-credential"
STATE = {"seq": 0, "uploads": {}, "shared": {}}
LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    log_path = ""
    port = 0

    def log_message(self, *args):  # keep molecule output clean
        pass

    def _record(self, entry):
        with LOCK, open(self.log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")

    def _reply(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        path = self.path
        if path == "/shutdown":
            self._reply(200, b"bye", "text/plain")
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return None
        if path.startswith("/upload/"):
            fid = path.rsplit("/", 1)[1]
            self._record(
                {
                    "method": "upload",
                    "file_id": fid,
                    "bytes": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "content_type": self.headers.get("Content-Type"),
                }
            )
            declared = STATE["uploads"].get(fid, {}).get("length")
            if declared is None or int(declared) != len(raw):
                return self._reply(400, b"length mismatch", "text/plain")
            STATE["uploads"][fid]["received"] = True
            return self._reply(200, f"OK - {len(raw)}".encode(), "text/plain")

        method = path.rsplit("/", 1)[-1]
        if "json" in (self.headers.get("Content-Type") or ""):
            body = json.loads(raw or b"{}")
        else:
            body = {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode()).items()}
        authed = self.headers.get("Authorization") == f"Bearer {TOKEN}"
        self._record(
            {
                "method": method,
                "authed": authed,
                "auth": self.headers.get("Authorization"),
                "body": body,
            }
        )
        if not authed:
            return self._reply(200, {"ok": False, "error": "invalid_auth"})

        if method == "conversations.open":
            if body.get("users") == "UNOTFOUND":
                return self._reply(200, {"ok": False, "error": "user_not_found"})
            return self._reply(200, {"ok": True, "channel": {"id": "D" + body["users"][1:]}})
        if method == "chat.postMessage":
            return self._reply(200, {"ok": True, "channel": body["channel"], "ts": "1.000001"})
        if method == "files.getUploadURLExternal":
            with LOCK:
                STATE["seq"] += 1
                fid = f"F{STATE['seq']:04d}"
            STATE["uploads"][fid] = {"length": body.get("length"), "name": body.get("filename")}
            url = f"http://127.0.0.1:{self.port}/upload/{fid}"
            return self._reply(200, {"ok": True, "upload_url": url, "file_id": fid})
        if method == "files.completeUploadExternal":
            files = body.get("files") or []
            if not all(STATE["uploads"].get(f["id"], {}).get("received") for f in files):
                return self._reply(200, {"ok": False, "error": "file_upload_not_found"})
            for f in files:
                if body.get("channel_id") != "CNOSHARE":
                    STATE["shared"][f["id"]] = body.get("channel_id")
            return self._reply(
                200,
                {"ok": True, "files": [{"id": f["id"], "title": f.get("title")} for f in files]},
            )
        if method == "files.info":
            fid = body.get("file")
            if fid not in STATE["uploads"]:
                return self._reply(200, {"ok": False, "error": "file_not_found"})
            ch = STATE["shared"].get(fid)
            shares = {"private": {ch: [{"ts": "1.000002"}]}} if ch else {}
            return self._reply(200, {"ok": True, "file": {"id": fid, "shares": shares}})
        return self._reply(200, {"ok": False, "error": "unknown_method"})


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port-file", required=True)
    p.add_argument("--log", required=True)
    p.add_argument("--lifetime", type=int, default=300)
    a = p.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    Handler.log_path, Handler.port = a.log, srv.server_address[1]
    with open(a.port_file, "w", encoding="utf-8") as fh:
        fh.write(str(Handler.port))
    timer = threading.Timer(a.lifetime, srv.shutdown)
    timer.daemon = True
    timer.start()
    srv.serve_forever()


if __name__ == "__main__":
    main()
