# SPDX-License-Identifier: GPL-3.0-or-later
# Runs INSIDE the Hermes container, as the agent's uid, with the agent's interpreter, fed on stdin by
# hermes_learning.py. Stdlib only and READ-ONLY: it opens state.db with mode=ro and never writes
# under HERMES_HOME. Answers one JSON object on stdout.
#
# Facts it relies on, read from the v0.22.5 source:
#   pending/{memory,skills}/<id>.json    tools/write_approval.py stage_write (id, subsystem, action,
#                                        summary, origin, created_at, payload)
#   skills/.curator_ledger.jsonl         tools/skill_ledger.py append_entry (id, ts, actor, action, skill)
#   memories/{MEMORY,USER}.md            entries joined by "\n§\n" (tools/memory_tool_store.py)
#   state.db messages                    session_id, role, content, tool_calls, tool_name, timestamp
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

args = json.loads(sys.argv[1])
home = Path(args.get("home") or os.environ.get("HERMES_HOME", "/opt/data"))
since = args.get("since")
window = float(args.get("claims_window_s") or 0)
if not home.is_dir() or not (home / "skills").is_dir():
    # A wrong or unmounted home would otherwise answer "nothing pending, no skills": refuse, so
    # the host writes read_ok 0 (cannot tell) instead of zeros that read as a healthy idle agent.
    print(json.dumps({"error": f"{home} is not an agent home (no skills/)"}))
    sys.exit(3)
out = {
    "pending": [],
    "skill_events": [],
    "memory_hashes": [],
    "skills_total": 0,
    "claim_gaps_window": 0,
    "claim_gap_sessions": [],
    "review_runs_new": 0,
}


def h(text):
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


for sub in ("memory", "skills"):
    d = home / "pending" / sub
    for p in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            rec = json.loads(p.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict):
            continue
        payload = rec.get("payload") or {}
        ops = payload.get("operations") if payload.get("action") == "batch" else [payload]
        adds = [
            h(op.get("content") or "")
            for op in (ops or [])
            if (op or {}).get("action") in ("add", "replace") and (op.get("content") or "").strip()
        ]
        out["pending"].append(
            {
                "id": rec.get("id") or p.stem,
                "subsystem": sub,
                "created_at": rec.get("created_at"),
                "origin": rec.get("origin"),
                "action": rec.get("action"),
                "summary": str(rec.get("summary") or "")[:200],
                "skill": payload.get("name") if sub == "skills" else None,
                "add_hashes": adds,
            }
        )
        if rec.get("origin") == "background_review" and since is not None:
            try:
                if float(rec.get("created_at") or 0) > float(since):
                    out["review_runs_new"] += 1
            except (TypeError, ValueError):
                pass

for name in ("MEMORY.md", "USER.md"):
    f = home / "memories" / name
    if f.is_file():
        for entry in f.read_text(encoding="utf-8").split("\n§\n"):
            if entry.strip():
                out["memory_hashes"].append(h(entry))

skip = set()
snf = args.get("skip_names_file") or ""
if snf and (home / snf).is_file():
    for line in (home / snf).read_text(encoding="utf-8").splitlines():
        if line.strip():
            skip.add(line.split(":", 1)[0].strip())
skills = home / "skills"
if skills.is_dir():
    for md in skills.rglob("SKILL.md"):
        parts = md.relative_to(skills).parts
        if parts and parts[0].startswith("."):
            continue
        if skip.intersection(parts):
            continue
        out["skills_total"] += 1

ledger = skills / ".curator_ledger.jsonl"
newest = since
if ledger.is_file():
    for line in ledger.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        ts = row.get("ts")
        if not isinstance(row, dict) or not ts:
            continue
        if since is None or ts > since:
            out["skill_events"].append(
                {k: row.get(k) for k in ("id", "ts", "actor", "action", "skill")}
            )
            newest = ts if newest is None or ts > newest else newest
out["newest_event_ts"] = newest

# The weak-model guard: an assistant turn that CLAIMS a memory update, in a
# session where no memory-writing tool was called. Counted, never quoted.
CLAIM = re.compile(
    r"(?i)\b(i(?:'ve| have)? (?:saved|stored|noted|remembered)|i'll remember|"
    r"(?:updated|added to) (?:my )?memory|saved (?:that|this) to memory)\b"
)
WRITERS = ("memory", "fact_store", "skill_manage")
db = home / "state.db"
if window > 0 and db.is_file():
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        cutoff = time.time() - window
        rows = con.execute(
            "SELECT session_id, role, content, tool_calls, tool_name, timestamp FROM messages "
            "WHERE timestamp >= ?",
            (cutoff,),
        ).fetchall()
        con.close()
        claims, wrote = {}, set()
        for sid, role, content, tool_calls, tool_name, ts in rows:
            if tool_name in WRITERS or any(f'"{w}"' in (tool_calls or "") for w in WRITERS):
                wrote.add(sid)
            if role == "assistant" and content and CLAIM.search(content):
                claims.setdefault(sid, []).append(ts)
        gaps = [sid for sid in claims if sid not in wrote]
        out["claim_gaps_window"] = len(gaps)
        out["claim_gap_sessions"] = sorted(h(str(sid))[:16] for sid in gaps)
    except sqlite3.Error as exc:
        out["claims_error"] = type(exc).__name__

out["slack_configured"] = bool(os.environ.get(args.get("token_env") or "SLACK_BOT_TOKEN")) and bool(
    os.environ.get(args.get("channel_env") or "SLACK_HOME_CHANNEL")
)
print(json.dumps(out))
