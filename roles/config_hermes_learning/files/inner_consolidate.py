# SPDX-License-Identifier: GPL-3.0-or-later
# Runs INSIDE the Hermes container as the agent's uid. Two modes:
#
#   propose  read MEMORY.md, ask the aux model (through the configured gateway, with the agent's own
#            gateway key from the container env) for a consolidated rewrite in which every new entry
#            names the prior entries it carries; answer both lists. NOTHING is written.
#   stage    write ONE pending memory record (tools/write_approval.py's file format at v0.22.5) that
#            the user approves or rejects with /memory approve|reject <id>. MEMORY.md itself is
#            never touched here: the approval replays the batch through Hermes's own store.
#
# The loss cap is applied on the HOST between the two modes (hermes_learning.py loss_check), so a
# model that drops facts cannot stage its own rewrite.
import json
import os
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

args = json.loads(sys.argv[1])
home = Path(args.get("home") or os.environ.get("HERMES_HOME", "/opt/data"))
DELIM = "\n§\n"
mem = home / "memories" / "MEMORY.md"
pending_dir = home / "pending" / "memory"


def entries():
    if not mem.is_file():
        return []
    return [e.strip() for e in mem.read_text(encoding="utf-8").split(DELIM) if e.strip()]


def already_pending():
    for p in pending_dir.glob("*.json") if pending_dir.is_dir() else []:
        try:
            if str(json.loads(p.read_text(encoding="utf-8-sig")).get("summary", "")).startswith(
                "consolidation:"
            ):
                return p.stem
        except (OSError, ValueError):
            continue
    return None


PROMPT = (
    "You consolidate an AI agent's long-term memory. Below are numbered entries. Merge duplicates and "
    "near-duplicates, tighten wording, and drop ONLY entries that are fully superseded by another entry. "
    'Never invent facts. Answer with JSON only: {"entries": [{"content": str, "sources": [int, ...]}]} '
    "where sources lists the 0-based numbers of EVERY prior entry whose information the new entry carries."
)

mode = args.get("mode")
if mode == "propose":
    old = entries()
    if len(old) < int(args.get("min_entries") or 0):
        print(json.dumps({"status": "too-small", "detail": f"{len(old)} entries"}))
        sys.exit(0)
    pid = already_pending()
    if pid:
        print(json.dumps({"status": "already-pending", "detail": pid}))
        sys.exit(0)
    proposal = args.get("proposal")
    if proposal is None:
        key = os.environ.get(args.get("key_env") or "", "")
        if not key:
            print(json.dumps({"status": "no-gateway-key", "detail": args.get("key_env")}))
            sys.exit(0)
        listing = "\n".join(f"{i}: {e}" for i, e in enumerate(old))
        body = {
            "model": args["model"],
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": PROMPT},
                {"role": "user", "content": listing},
            ],
        }
        req = urllib.request.Request(
            args["gateway_url"].rstrip("/") + "/chat/completions",
            method="POST",
            data=json.dumps(body).encode(),
        )
        req.add_header("Authorization", f"Bearer {key}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                text = json.loads(resp.read().decode())["choices"][0]["message"]["content"]
            proposal = json.loads(text[text.find("{") : text.rfind("}") + 1])["entries"]
        except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
            print(json.dumps({"status": "model-failed", "detail": type(exc).__name__}))
            sys.exit(0)
    clean = [
        {"content": str(p.get("content", "")).strip(), "sources": list(p.get("sources") or [])}
        for p in proposal
        if isinstance(p, dict) and str(p.get("content", "")).strip()
    ]
    print(json.dumps({"status": "proposed", "entries": old, "proposal": clean}))
elif mode == "stage":
    payload = args["payload"]
    pinned = [
        op["matched_entry"] for op in payload.get("operations", []) if op.get("action") == "remove"
    ]
    if pinned != entries():
        print(
            json.dumps(
                {"status": "memory-changed", "detail": "MEMORY.md changed since the proposal"}
            )
        )
        sys.exit(3)
    pid = uuid.uuid4().hex[:8]
    record = {
        "id": pid,
        "subsystem": "memory",
        "action": "batch",
        "summary": args["summary"],
        "origin": "consolidation",
        "created_at": time.time(),
        "payload": payload,
    }
    pending_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=pending_dir, prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(record, fh)
    os.replace(tmp, pending_dir / f"{pid}.json")
    print(json.dumps({"status": "staged", "id": pid}))
else:
    print(json.dumps({"status": "bad-mode"}))
    sys.exit(2)
