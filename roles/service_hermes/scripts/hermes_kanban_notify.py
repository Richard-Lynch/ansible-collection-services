#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Kanban -> Slack card threads. Runs on the Hermes GUEST, as root, from
a systemd timer. Stdlib only.

ONE THREAD PER CARD. The first event of a card in scope posts a root message naming the card (id,
title, repo, assignee, a console link and the term. attach link); every later state change is a
reply in that thread: created, started, blocked (with its reason), needs input, PR opened, review,
done, terminated, unblocked, moved, archived.

WHY A POLLER, AND WHY HERE (read at v0.22.5 source, the running version):
  * Upstream's gateway notifier (gateway/kanban_watchers_notifier.py) delivers only "terminal"
    kinds (completed, blocked, status, review, ...) into a subscription's EXISTING chat or thread.
    It cannot post the root, never sees created / claimed / a PR, and its notify+wake modes wake
    an agent turn. Not enough on its own, and nothing to add to it from outside.
  * Plugin hooks (kanban_task_claimed/completed/blocked, on_kanban_task_updated) fire in whichever
    PROCESS committed the write (dispatcher, worker, dashboard), carry no adapter handle and have
    no created or PR hook, so a plugin would still need its own delivery and cross-process dedup.
  * task_events is the board's append-only log with a monotonic id: upstream's own notifier is a
    cursor poller over it. This is the same shape, owned by us.
The DB is read from the host bind mount with sqlite ``mode=ro``. Delivery is Hermes's OWN Slack
adapter through its public CLI: ``docker exec -i -u <uid> <container> hermes send --to
slack:<channel>[:<thread_ts>] --json --file -``. The bot token never leaves the container and this
script holds no Slack credential. The thread map and the cursor live in a root-owned state file
OUTSIDE the agent's tree, so the agent cannot rewrite which thread a card posts to.

IDEMPOTENT: the state file is written atomically after EVERY successful post, so a restart or a
killed tick never reposts a root and never re-sends a reply. A failed post stops the tick at that
event (the cursor stays before it) and exits 1; the next tick retries from there. A first run with
no state starts at the board's newest event: history is never replayed.

TOGGLES (one line, in the card body or in any card comment; the LAST one wins):
  notify: off                         nothing more for this card (the root stays)
  notify: on                          on, for a card outside the default scope too
  notify: review, done                only these events from now on
  ping me on review, blocked          @-mention the owner on these events ("ping: off" = never)
Defaults (inventory, hermes_kanban_notify): cards assigned to the listed profiles are on, with the
listed events, and blocked / needs_input ping the owner.

UNTRUSTED TEXT: a card's title, body and comments can carry an issue's text. A title and a
block reason are clipped to one line, `<`/`>` are replaced so no `<!channel>` or link can be
forged, and Hermes's `MEDIA:` directive is defused so a title cannot attach a container file. A
body is read only for the toggle lines and a strictly validated repo slug; it is never posted.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

EVENTS = (
    "created",
    "started",
    "blocked",
    "needs_input",
    "pr",
    "review",
    "done",
    "terminated",
    "unblocked",
    "moved",
    "archived",
)
BATCH = 500

_NOTIFY_RE = re.compile(r"(?im)^[ \t>*_-]*notify[ \t]*:[ \t]*([^\n]+)$")
_PING_RE = re.compile(r"(?im)^[ \t>*_-]*ping(?:[ \t]+me)?(?:[ \t]+on)?[ \t]*:?[ \t]*([^\n]+)$")
_REPO_RE = re.compile(
    r"(?im)^[ \t>*_-]*repo[ \t]*:[ \t]*([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?)[ \t]*$"
)
_GH_REPO_RE = re.compile(
    r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?(?=[/\s)>\]]|$)"
)
_PR_RE = re.compile(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[0-9]+")
_CONTRACT_REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_ALIASES = {
    "pr_opened": "pr",
    "prs": "pr",
    "input": "needs_input",
    "approval": "needs_input",
    "needs_approval": "needs_input",
    "start": "started",
    "block": "blocked",
    "terminate": "terminated",
    "complete": "done",
    "completed": "done",
}


class SendError(Exception):
    pass


# ---------------------------------------------------------------- text safety


def safe(text: object, limit: int = 120) -> str:
    """One line, clipped, with nothing Slack or Hermes would interpret as a directive."""
    s = " ".join(str(text or "").split())
    s = s.replace("<", "‹").replace(">", "›")
    s = re.sub(r"(?i)media:", "media​:", s)
    s = s.replace("[[", "[​[")
    if len(s) > limit:
        s = s[: limit - 1].rstrip() + "…"
    return s


# ---------------------------------------------------------------- toggles


def _words(value: str) -> list[str]:
    out = []
    for w in re.split(r"[\s,]+", value.strip().lower()):
        w = w.strip(".;:")
        if w:
            out.append(_ALIASES.get(w, w))
    return out


def directives(texts: list[str]) -> dict:
    """Fold toggle lines in order (body first, then comments oldest->newest); the last wins.

    Returns {"notify": None | "off" | "on" | [events], "ping": None | [events]} -- None means
    "no directive, use the defaults". An unknown word is ignored, never an error: a typo must not
    silence a card, so a line naming no known event changes nothing.
    """
    notify: object = None
    ping: Optional[list[str]] = None
    for text in texts:
        for _, kind, value in sorted(
            [(m.start(), "notify", m.group(1)) for m in _NOTIFY_RE.finditer(text or "")]
            + [(m.start(), "ping", m.group(1)) for m in _PING_RE.finditer(text or "")]
        ):
            words = _words(value)
            if kind == "notify":
                if words[:1] in (["off"], ["none"], ["mute"]):
                    notify = "off"
                elif words[:1] in (["on"], ["all"], ["slack"]):
                    notify = "on"
                else:
                    known = [w for w in words if w in EVENTS]
                    if known:
                        notify = known
            else:
                if words[:1] in (["off"], ["none"], ["never"]):
                    ping = []
                else:
                    known = [w for w in words if w in EVENTS]
                    if known:
                        ping = known
    return {"notify": notify, "ping": ping}


def policy(card: dict, d: dict, cfg: dict) -> tuple[bool, set, set]:
    """(in scope, enabled events, ping events) for one card."""
    in_default = (card.get("assignee") or "") in set(cfg.get("assignees") or [])
    events = set(cfg.get("events") or EVENTS)
    ping = set(cfg.get("ping") or [])
    n = d["notify"]
    if n == "off":
        return False, set(), set()
    if isinstance(n, list):
        events = set(n)
    on = in_default or n is not None
    if d["ping"] is not None:
        ping = set(d["ping"])
    return on, events, ping


def repo_of(card: dict, pr_urls: list[str]) -> str:
    """The card's repo: its completion contract (coder cards are created with
    `--completion-contract OWNER/REPO`; upstream validates it as
    local-only, OWNER/REPO or an exact PR URL), then a `repo:` body line, then a PR URL seen on the
    card, then a GitHub URL in the body."""
    contract = str(card.get("completion_contract") or "")
    if _CONTRACT_REPO_RE.fullmatch(contract):
        return contract
    if _PR_RE.fullmatch(contract):
        return contract.split("/pull/")[0].replace("https://github.com/", "")
    body = card.get("body") or ""
    m = _REPO_RE.search(body)
    if m:
        return m.group(1)
    for url in pr_urls:
        return url.split("/pull/")[0].replace("https://github.com/", "")
    m = _GH_REPO_RE.search(body)
    if m:
        return m.group(1)
    return "unknown"


# ---------------------------------------------------------------- event -> message


def classify(ev: dict) -> Optional[tuple[str, str]]:
    """(event name, reply text) for an event kind that is a state change, else None."""
    kind = ev["kind"]
    p = ev.get("payload") or {}
    if kind == "created":
        return "created", f"created ({safe(p.get('status') or 'ready', 20)})"
    if kind == "claimed":
        return "started", "started"
    if kind == "blocked":
        reason = safe(p.get("reason") or "no reason given", 200)
        if p.get("kind") == "needs_input":
            return "needs_input", f"needs your input: {reason}"
        return "blocked", f"blocked: {reason}"
    if kind in ("gave_up", "crashed", "timed_out", "block_loop_detected"):
        return "blocked", f"blocked: the worker {kind.replace('_', ' ')}"
    if kind == "unblocked":
        return "unblocked", "unblocked"
    if kind == "review_requested":
        return "review", "review requested"
    if kind == "changes_requested":
        return (
            "review",
            f"changes requested: {safe(p.get('reason') or 'see the card', 200)}",
        )
    if kind == "completed":
        return "done", "done"
    if kind == "reclaimed" and p.get("manual"):
        return (
            "terminated",
            f"terminated: {safe(p.get('reason') or 'by the owner', 200)}",
        )
    if kind == "archived":
        return "archived", "archived"
    if kind == "status":
        col = str(p.get("status") or "")
        mapped = {
            "running": "started",
            "blocked": "blocked",
            "review": "review",
            "done": "done",
        }
        return mapped.get(col, "moved"), f"moved to {safe(col or '?', 20)}"
    return None


def root_text(card: dict, repo: str, cfg: dict) -> str:
    tid = card["id"]
    lines = [
        # Markdown bold: `hermes send` converts markdown to Slack mrkdwn, and a single-asterisk
        # `*x*` is markdown ITALIC there (it arrives as `_x_`).
        f"**Kanban card {tid}**: {safe(card.get('title'))}",
        f"repo: {safe(repo, 80)} | assignee: {safe(card.get('assignee') or 'unassigned', 40)}",
    ]
    if cfg.get("console_url_template"):
        lines.append("console: " + cfg["console_url_template"].replace("{card_id}", tid))
    if cfg.get("attach_url_template"):
        lines.append("attach: " + cfg["attach_url_template"].replace("{card_id}", tid))
    lines.append(
        "Replies here follow the card. Toggle with a card comment: `notify: off` / `ping me on review`."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------- board reads


def open_board(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def max_event_id(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM task_events").fetchone()[0])


def events_after(conn: sqlite3.Connection, cursor: int) -> list[dict]:
    out = []
    for r in conn.execute(
        "SELECT id, task_id, kind, payload FROM task_events WHERE id > ? ORDER BY id LIMIT ?",
        (cursor, BATCH),
    ):
        try:
            payload = json.loads(r["payload"]) if r["payload"] else {}
        except ValueError:
            payload = {}
        out.append(
            {
                "id": int(r["id"]),
                "task_id": r["task_id"],
                "kind": r["kind"],
                "payload": payload if isinstance(payload, dict) else {},
            }
        )
    return out


def card_of(conn: sqlite3.Connection, task_id: str) -> Optional[dict]:
    # SELECT *: completion_contract is a newer column; a board without it reads as no contract.
    r = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return dict(r) if r else None


def comments_of(conn: sqlite3.Connection, task_id: str) -> list[str]:
    return [
        r["body"]
        for r in conn.execute(
            "SELECT body FROM task_comments WHERE task_id = ? ORDER BY id", (task_id,)
        )
    ]


# ---------------------------------------------------------------- delivery


def docker_sender(container: str, uid: str) -> Callable[[str, Optional[str], str], str]:
    """send(channel, thread_ts, text) -> the posted message's ts, through `hermes send`."""

    def send(channel: str, thread_ts: Optional[str], text: str) -> str:
        target = f"slack:{channel}" + (f":{thread_ts}" if thread_ts else "")
        proc = subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                "-u",
                str(uid),
                container,
                "hermes",
                "send",
                "--to",
                target,
                "--json",
                "--file",
                "-",
            ],
            input=text,
            capture_output=True,
            text=True,
            timeout=60,
        )
        try:
            res = json.loads(proc.stdout or "{}")
        except ValueError:
            res = {}
        ts = res.get("message_id") if isinstance(res, dict) else None
        if proc.returncode != 0 or not res.get("success") or not ts:
            err = (res.get("error") if isinstance(res, dict) else None) or proc.stderr.strip()[
                -300:
            ]
            raise SendError(
                f"hermes send to {target} failed (rc={proc.returncode}): {err or 'no message_id'}"
            )
        return str(ts)

    return send


# ---------------------------------------------------------------- state


def read_state(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as fh:
        st = json.load(fh)  # corrupt state RAISES: an error beats re-posting every root
    if not isinstance(st, dict) or not isinstance(st.get("cursor"), int):
        raise ValueError(f"{path} is not a notifier state")
    st.setdefault("cards", {})
    return st


def write_state(path: Path, st: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(st, fh, sort_keys=True, indent=1)
    os.replace(tmp, path)


def write_metrics(path: Optional[str], ok: bool, st: Optional[dict]) -> None:
    if not path:
        return
    p = Path(path)
    lines = [
        "# HELP hermes_kanban_notify_ok 1 when the last Kanban -> Slack tick delivered everything due.",
        "# TYPE hermes_kanban_notify_ok gauge",
        f"hermes_kanban_notify_ok {1 if ok else 0}",
        "# HELP hermes_kanban_notify_cursor Last task_events id the notifier has handled.",
        "# TYPE hermes_kanban_notify_cursor gauge",
        f"hermes_kanban_notify_cursor {int((st or {}).get('cursor', -1))}",
    ]
    if ok:
        lines += [
            "# TYPE hermes_kanban_notify_last_success_timestamp_seconds gauge",
            f"hermes_kanban_notify_last_success_timestamp_seconds {int(time.time())}",
        ]
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, p)


# ---------------------------------------------------------------- the tick


def tick(
    conn: sqlite3.Connection,
    cfg: dict,
    state_path: Path,
    send: Callable[[str, Optional[str], str], str],
) -> dict:
    st = read_state(state_path)
    if st is None:
        st = {"cursor": max_event_id(conn), "cards": {}}
        write_state(state_path, st)
        return st
    owner = cfg.get("owner_user_id") or ""
    channels = cfg.get("channels") or {}
    while True:
        evs = events_after(conn, st["cursor"])
        if not evs:
            return st
        for ev in evs:
            card = card_of(conn, ev["task_id"])
            if card is None:  # deleted since: nothing to name
                st["cursor"] = ev["id"]
                continue
            cst = st["cards"].get(card["id"], {})
            comments = comments_of(conn, card["id"])
            on, enabled, ping = policy(card, directives([card.get("body") or ""] + comments), cfg)
            msgs: list[tuple[str, str]] = []
            if on:
                if ev["kind"] == "commented":
                    seen = set(cst.get("pr_urls") or [])
                    for c in comments:
                        for url in _PR_RE.findall(c or ""):
                            if url not in seen:
                                seen.add(url)
                                msgs.append(("pr", f"PR opened: {url}"))
                                cst.setdefault("pr_urls", []).append(url)
                elif ev["kind"] == "pr_acceptance" and _PR_RE.fullmatch(
                    str(ev["payload"].get("pr_url") or "")
                ):
                    url = ev["payload"]["pr_url"]
                    if url not in (cst.get("pr_urls") or []):
                        msgs.append(("pr", f"PR opened: {url}"))
                        cst.setdefault("pr_urls", []).append(url)
                else:
                    c = classify(ev)
                    if c:
                        msgs.append(c)
            if msgs and not cst.get("ts"):
                channel = channels.get(card.get("assignee") or "") or cfg["default_channel"]
                cst["channel"] = channel
                cst["ts"] = send(
                    channel,
                    None,
                    root_text(card, repo_of(card, cst.get("pr_urls") or []), cfg),
                )
                st["cards"][card["id"]] = cst
                write_state(state_path, st)
            for name, text in msgs:
                if name not in enabled:
                    continue
                if name in ping and owner:
                    text = f"<@{owner}> {text}"
                sent = cst.setdefault("sent", [])
                key = f"{ev['id']}:{name}:{text}"[:200]
                if key in sent:
                    continue
                send(cst["channel"], cst["ts"], text)
                sent.append(key)
                del sent[:-50]
                st["cards"][card["id"]] = cst
                write_state(state_path, st)
            if card["id"] in st["cards"] or cst.get("pr_urls"):
                st["cards"][card["id"]] = cst
            st["cursor"] = ev["id"]
            write_state(state_path, st)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", required=True)
    ap.add_argument("--state", required=True)
    ap.add_argument("--metrics", default="")
    args = ap.parse_args(argv)
    with open(args.config, encoding="utf-8") as fh:
        cfg = json.load(fh)
    st = None
    try:
        conn = open_board(cfg["board_db"])
        try:
            st = tick(
                conn,
                cfg,
                Path(args.state),
                docker_sender(cfg["container"], str(cfg["uid"])),
            )
        finally:
            conn.close()
    except (
        SendError,
        sqlite3.Error,
        OSError,
        ValueError,
        subprocess.TimeoutExpired,
    ) as exc:
        print(f"hermes-kanban-notify: {exc}", file=sys.stderr)
        write_metrics(args.metrics, False, st or _safe_state(Path(args.state)))
        return 1
    write_metrics(args.metrics, True, st)
    return 0


def _safe_state(path: Path) -> Optional[dict]:
    try:
        return read_state(path)
    except (OSError, ValueError):
        return None


if __name__ == "__main__":
    sys.exit(main())
