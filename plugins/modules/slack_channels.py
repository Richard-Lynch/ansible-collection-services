#!/usr/bin/python
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright: (c) 2026, Richard Lynch
# GNU General Public License v3.0+ (see https://www.gnu.org/licenses/gpl-3.0.txt)
"""Reconcile a declared list of Slack public channels against the workspace, as a bot.

Reads the workspace (auth.test for the bot's identity and GRANTED scopes, conversations.list,
conversations.members), computes a plan, and -- only when apply=true and not in check mode --
executes it in the one order Slack accepts:

    create | (join old, rename)  ->  join  ->  invite users  ->  purpose  ->  topic
           ->  guide (post + pin, or chat.update in place, or pin)  ->  bookmarks

A channel's GUIDE is one bot message, found on every run by its trailing marker
`<guide_marker_prefix><name>` (default `guide:<name>`): posted once and pinned, edited in place with chat.update when the declared text
drifts (never reposted), and unpinned + deleted only when the declaration drops it. BOOKMARKS are
matched by title: added or re-pointed, never removed (a bookmark nobody declared is reported).

then re-reads every declared channel with conversations.info / conversations.members and returns
the plan that REMAINS. The caller asserts that is empty; a step that answered ok and changed
nothing is caught there, not trusted.

What it will never do: archive, unarchive, kick, or touch a channel that is not declared. Undeclared
members are reported, not removed. A rename whose target AND source both exist is refused (merging
two channels is not a rename), as is a target that exists but is archived.

Slack answers HTTP 200 with {"ok": false, "error": ...} for nearly every failure, so the status code
proves nothing: every call is judged on `ok`, and the exact `error` (plus `needed`/`provided` on a
missing_scope) is carried into the result.
"""

from __future__ import annotations

DOCUMENTATION = r"""
module: slack_channels
short_description: Reconcile declared Slack public channels (create, rename, join, invite, purpose, topic)
description:
  - Reads the workspace as a bot, plans the steps that make the declared public channels match
    (create, rename, join, invite, purpose, topic, a pinned guide message, bookmarks), and runs
    them only when O(apply=true) and not in check mode.
  - After applying it re-reads every declared channel and returns the plan that remains, so a
    step that answered ok and changed nothing is visible rather than trusted.
  - It never archives, unarchives or removes members, and never touches an undeclared channel.
author: Richard Lynch (@Richard-Lynch)
options:
  token:
    description: Bot token (C(xoxb-)).
    type: str
    required: true
  api_url:
    description: Slack Web API base URL.
    type: str
    default: https://slack.com/api
  channels:
    description: Declared channels, each with at least C(name).
    type: list
    elements: dict
    required: true
  apply:
    description: Execute the plan; otherwise only report it.
    type: bool
    default: false
  admin_token:
    description:
      - Optional user token of a workspace admin, used for the steps planned as admin steps
        (renames and bookmark changes) when it is given.
      - Empty means no admin token.
    type: str
    default: ""
  guide_marker_prefix:
    description:
      - Prefix of the trailing marker that identifies the bot's guide message in each channel
        (the marker is the prefix followed by the channel name).
      - Changing it on a workspace that already has guides makes the next run treat them as
        absent and post new ones, so set it once and keep it.
    type: str
    default: "guide:"
"""

EXAMPLES = r"""
- name: Report what would change
  richard_lynch.services.slack_channels:
    token: "{{ slack_bot_token }}"
    channels:
      - name: builds
        purpose: CI results
        members: [U0123456789]

- name: Apply it
  richard_lynch.services.slack_channels:
    token: "{{ slack_bot_token }}"
    channels: "{{ declared_channels }}"
    apply: true
  no_log: true
"""

RETURN = r"""
plan:
  description: The steps needed to reach the declaration, as planned before acting.
  returned: when the workspace was read and the declaration is valid
  type: list
  elements: dict
refusals:
  description: Declarations that were refused as ambiguous (nothing is done for them).
  returned: when the workspace was read
  type: list
  elements: str
results:
  description: The outcome of each executed step.
  returned: when applied
  type: list
  elements: dict
remaining:
  description: The plan computed again from a fresh read after applying; empty when converged.
  returned: when applied
  type: list
  elements: dict
"""

import html  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import time  # noqa: E402
import urllib.error  # noqa: E402
import urllib.parse  # noqa: E402
import urllib.request  # noqa: E402

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,79}$")
USER_RE = re.compile(r"^[UW][A-Z0-9]{2,}$")
CHANNEL_RE = re.compile(r"^C[A-Z0-9]{2,}$")

# Bot-token scope each step needs (Slack Web API method pages). conversations.invite and the
# setters accept channels:manage for a bot; join is channels:join; reading is channels:read.
SCOPES = {
    "read": "channels:read",
    "guide_post": "chat:write",
    "guide_update": "chat:write",
    "guide_pin": "pins:write",
    "guide_remove": "pins:write",
    "bookmark_add": "bookmarks:write",
    "bookmark_edit": "bookmarks:write",
    "create": "channels:manage",
    "rename": "channels:manage",
    "join": "channels:join",
    "invite": "channels:manage",
    "purpose": "channels:manage",
    "topic": "channels:manage",
}


# Reading the guides needs these as well (pins.list, conversations.history).
GUIDE_READ_SCOPES = ("channels:history", "pins:read")
# User-token scope for the steps done as the workspace admin.
ADMIN_SCOPES = {
    "rename": "channels:write",
    "bookmark_add": "bookmarks:write",
    "bookmark_edit": "bookmarks:write",
}
GUIDE_MAX = 2900  # a section block's text is capped at 3000 characters
URL_RE = re.compile(r"^https://[^\s<>|]+$")


class SlackError(Exception):
    def __init__(self, method: str, body: dict):
        self.method = method
        self.body = body
        detail = body.get("error", "unknown")
        if body.get("needed"):
            detail += f" (needed {body.get('needed')}, provided {body.get('provided', '')})"
        super().__init__(f"{method}: {detail}")


class Slack:
    def __init__(self, token: str, api_url: str, sleep=time.sleep):
        self.token = token
        self.api_url = api_url.rstrip("/")
        self.sleep = sleep
        self.calls: list[str] = []

    def call(self, method: str, params: dict | None = None, post: bool = False):
        """One Web API call; returns (body, headers). Retries a 429 per Retry-After, three times."""
        data = None
        url = f"{self.api_url}/{method}"
        if post:
            data = json.dumps(params or {}).encode()
        elif params:
            url += "?" + urllib.parse.urlencode(params)
        for dummy in range(4):
            req = urllib.request.Request(url, data=data, method="POST" if post else "GET")
            req.add_header("Authorization", f"Bearer {self.token}")
            if post:
                req.add_header("Content-Type", "application/json; charset=utf-8")
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    body = json.loads(resp.read().decode() or "{}")
                    headers = {k.lower(): v for k, v in resp.headers.items()}
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    self.sleep(int(exc.headers.get("Retry-After", "1")))
                    continue
                raise SlackError(method, {"error": f"http_{exc.code}"}) from exc
            except (urllib.error.URLError, OSError, ValueError) as exc:
                # Unreachable, timed out, or not JSON: a reason, never a traceback the no_log'd
                # task would hide behind an empty report.
                raise SlackError(
                    method, {"error": f"unreachable_or_unparseable ({type(exc).__name__}: {exc})"}
                ) from exc
            self.calls.append(method)
            if not body.get("ok"):
                raise SlackError(method, body)
            return body, headers
        raise SlackError(method, {"error": "rate_limited (429 after retries)"})

    def paged(self, method: str, params: dict, key: str) -> list:
        out, cursor = [], ""
        while True:
            p = dict(params, limit=200)
            if cursor:
                p["cursor"] = cursor
            body, dummy = self.call(method, p)
            out.extend(body.get(key, []))
            cursor = (body.get("response_metadata") or {}).get("next_cursor", "")
            if not cursor:
                return out


def validate(channels: list) -> list[str]:
    """Refuse a declaration that cannot mean one thing. Returns the problems (empty = valid)."""
    problems, seen = [], set()
    # `prompt` is not Slack's: the module ignores it and the caller may hand it to whatever else reads
    # the declaration (an agent's per-channel instruction, say), so the human guide and that text
    # can be declared side by side.
    allowed = {
        "name",
        "id",
        "purpose",
        "topic",
        "members",
        "rename_from",
        "guide",
        "bookmarks",
        "prompt",
    }
    for i, ch in enumerate(channels):
        if not isinstance(ch, dict):
            problems.append(f"channels[{i}] is not a mapping")
            continue
        name = ch.get("name", "")
        extra = sorted(set(ch) - allowed)
        if extra:
            problems.append(f"{name or i}: unknown key(s) {extra}")
        for field in ("name", "rename_from"):
            v = ch.get(field)
            if (field == "name" or v is not None) and not (isinstance(v, str) and NAME_RE.match(v)):
                problems.append(f"{name or i}: {field} {v!r} is not a Slack channel name (no '#')")
        for field in ("purpose", "topic"):
            v = ch.get(field)
            if v is not None and (not isinstance(v, str) or len(v) > 250):
                problems.append(f"{name}: {field} must be a string of at most 250 characters")
        guide = ch.get("guide")
        if guide is not None and (
            not isinstance(guide, str) or not guide.strip() or len(guide) > GUIDE_MAX
        ):
            problems.append(
                f"{name}: guide must be non-empty text of at most {GUIDE_MAX} characters"
            )
        bms = ch.get("bookmarks", [])
        if not isinstance(bms, list) or not all(
            isinstance(b, dict)
            and set(b) == {"title", "link"}
            and isinstance(b["title"], str)
            and 0 < len(b["title"]) <= 100
            and isinstance(b["link"], str)
            and URL_RE.match(b["link"])
            for b in bms
        ):
            problems.append(
                f"{name}: bookmarks must be [{{title, link: https://...}}], got {bms!r}"
            )
        elif len({b["title"] for b in bms}) != len(bms):
            problems.append(f"{name}: two bookmarks share a title (titles identify them)")
        cid = ch.get("id")
        if cid is not None and not (isinstance(cid, str) and CHANNEL_RE.match(cid)):
            problems.append(f"{name}: id {cid!r} is not a Slack channel id (C...)")
        members = ch.get("members", [])
        if not isinstance(members, list) or not all(
            isinstance(m, str) and USER_RE.match(m) for m in members
        ):
            problems.append(f"{name}: members must be Slack user ids (U.../W...), got {members!r}")
        for n in (name, ch.get("rename_from")):
            if n and n in seen:
                problems.append(f"{n}: named twice (as a name or a rename_from)")
            if n:
                seen.add(n)
    return problems


def plan(declared: list, live: list, members: dict, bot_id: str) -> tuple[list, list, list]:
    """Pure: declared channels + live state -> (actions, refusals, state).

    live: conversations.list entries (id, name, is_archived, is_member, purpose, topic).
    members: channel id -> list of member user ids (absent = not read, e.g. a channel to create).
    Each action is {"op", "channel", "id"?, ...}; ops appear in execution order per channel.
    """
    by_name = {c["name"]: c for c in live}
    actions, refusals, state = [], [], []
    for ch in declared:
        name, old = ch["name"], ch.get("rename_from")
        target, source = by_name.get(name), by_name.get(old) if old else None
        row = {"name": name, "id": None, "exists": False, "via": None}
        if target and target.get("is_archived"):
            refusals.append(
                f"#{name} exists but is ARCHIVED ({target['id']}); unarchiving is the owner's call"
            )
            state.append(dict(row, id=target["id"], archived=True))
            continue
        if target and source and not source.get("is_archived"):
            refusals.append(
                f"#{name} and its rename_from #{old} BOTH exist ({target['id']}, {source['id']}); refusing to pick one"
            )
            state.append(dict(row, id=target["id"], exists=True))
            continue
        ch_actions = []
        if target:
            cur, row = target, dict(row, id=target["id"], exists=True, via="exists")
        elif source and source.get("is_archived"):
            refusals.append(
                f"rename_from #{old} is ARCHIVED ({source['id']}); unarchiving is the owner's call"
            )
            state.append(dict(row, id=source["id"], archived=True))
            continue
        elif source:
            cur, row = source, dict(row, id=source["id"], via=f"rename from #{old}")
            if not source.get("is_member"):
                ch_actions.append({"op": "join", "channel": old, "id": source["id"]})
            ch_actions.append({"op": "rename", "channel": name, "from": old, "id": source["id"]})
        else:
            cur, row = None, dict(row, via="create")
            ch_actions.append({"op": "create", "channel": name})

        # A declared id is what everything downstream (cron and route targets) was written
        # against: the name must still resolve to THAT channel, and an id cannot be created.
        if ch.get("id") and row["id"] != ch["id"]:
            refusals.append(
                f"#{name} is declared as {ch['id']} but resolves to "
                f"{row['id'] or 'nothing (it would be CREATED with a new id)'}"
            )
            state.append(row)
            continue
        is_member = bool(cur and cur.get("is_member")) or any(
            a["op"] in ("create", "join") for a in ch_actions
        )
        if not is_member:
            ch_actions.append({"op": "join", "channel": name, "id": row["id"]})
        # A channel about to be created will hold exactly its creator, the bot.
        have = {bot_id} if cur is None else set(members.get(row["id"], []))
        want = [m for m in ch.get("members", []) if m != bot_id]
        missing = [m for m in want if m not in have]
        if missing:
            ch_actions.append({"op": "invite", "channel": name, "id": row["id"], "users": missing})
        for field in ("purpose", "topic"):
            want_v = ch.get(field)
            have_v = ((cur or {}).get(field) or {}).get("value", "")
            # Slack hands topic and purpose back with & < > escaped, so "<repo>" would drift forever
            if want_v is not None and _norm(want_v) != _norm(have_v):
                ch_actions.append(
                    {
                        "op": field,
                        "channel": name,
                        "id": row["id"],
                        "value": want_v,
                        "was": have_v,
                    }
                )
        undeclared = sorted(have - set(want) - {bot_id}) if row["id"] in members else []
        row.update(
            # The LIVE membership, before any planned create or join.
            is_member=bool(cur and cur.get("is_member")),
            purpose=((cur or {}).get("purpose") or {}).get("value", ""),
            topic=((cur or {}).get("topic") or {}).get("value", ""),
            undeclared_members=undeclared,
        )
        state.append(row)
        actions.extend(ch_actions)
    return actions, refusals, state


DEFAULT_MARKER_PREFIX = "guide:"
# Set by run() from the guide_marker_prefix option; module functions read it through marker().
_marker_prefix = DEFAULT_MARKER_PREFIX


def marker(name: str) -> str:
    return f"{_marker_prefix}{name}"


def guide_text(name: str, guide: str) -> str:
    """The fallback `text` of the guide message: what is compared on every run."""
    return f":pushpin: *How to use #{name}*\n\n{guide.strip()}\n\n{marker(name)}"


def guide_body(name: str, guide: str) -> str:
    """The section block's mrkdwn: what is compared on every run (Slack keeps it verbatim, while
    the message's `text` comes back with its newlines flattened when blocks are present)."""
    return f":pushpin: *How to use #{name}*\n\n{guide.strip()}"


def _block_body(message: dict):
    for b in message.get("blocks") or []:
        if b.get("type") == "section":
            return (b.get("text") or {}).get("text")
    return None


def guide_blocks(name: str, guide: str) -> list:
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":pushpin: *How to use #{name}*\n\n{guide.strip()}",
            },
        },
        {"type": "context", "elements": [{"type": "mrkdwn", "text": marker(name)}]},
    ]


def _norm(text: str) -> str:
    # Slack returns &, < and > escaped in `text`.
    return html.unescape(text or "").strip()


CHAN_LINK_RE = re.compile(r"<#(C[A-Z0-9]+)(?:\|([^>]*))?>")
URL_LINK_RE = re.compile(r"<(https?://[^|>]+)>")


def _as_written(text: str, id2name: dict) -> str:
    """Undo what Slack does to mrkdwn it stores: `#general` becomes `<#C0123456789>` and a bare URL
    becomes `<https://...>`. A `<url|label>` the author wrote is left alone."""
    text = CHAN_LINK_RE.sub(
        lambda m: "#" + (m.group(2) or id2name.get(m.group(1), m.group(1))), _norm(text)
    )
    return URL_LINK_RE.sub(lambda m: m.group(1), text)


def plan_extras(
    declared: list, state: list, extras: dict, bot_id: str, id2name: dict | None = None
) -> tuple[list, list]:
    """Pure: guides and bookmarks -> (actions, notes).

    extras: channel id -> {"guides": [{"ts", "text", "pinned"}] (the bot's marker messages,
    newest first), "bookmarks": [{"id", "title", "link"}] or None (not readable)}. A channel about
    to be created has no entry and gets a fresh post. A channel whose extras could not be read
    (the bot is not a member yet) is left for the second round.
    """
    rows = {r["name"]: r for r in state}
    actions, notes = [], []
    for ch in declared:
        name = ch["name"]
        row = rows.get(name)
        if row is None or row.get("archived"):
            continue
        cid = row.get("id")
        new = row.get("via") == "create"
        ex = extras.get(cid) if cid else None
        if not new and ex is None:
            if ch.get("guide") or ch.get("bookmarks"):
                notes.append(
                    f"#{name}: guide/bookmarks not readable yet (bot not a member); planned after the join"
                )
            continue
        guides = [] if new else ex["guides"]
        row["guide"] = (
            {"ts": guides[0]["ts"], "pinned": guides[0]["pinned"], "count": len(guides)}
            if guides
            else None
        )
        want = ch.get("guide")
        if want:
            text = guide_text(name, want)
            if not guides:
                actions.append(
                    {"op": "guide_post", "channel": name, "id": cid, "value": text, "guide": want}
                )
            else:
                keep = guides[0]
                if keep.get("body") is not None:
                    drift = _as_written(keep["body"], id2name or {}) != _norm(
                        guide_body(name, want)
                    )
                else:  # no blocks (posted by hand?): compare the text, whitespace-insensitively
                    drift = " ".join(_norm(keep["text"]).split()) != " ".join(_norm(text).split())
                if drift:
                    actions.append(
                        {
                            "op": "guide_update",
                            "channel": name,
                            "id": cid,
                            "ts": keep["ts"],
                            "value": text,
                            "guide": want,
                            # what Slack holds, so a drift that is only Slack rewriting the text
                            # is visible in the report rather than an endless update
                            "was": _norm(keep.get("body") or keep["text"]),
                        }
                    )
                if not keep["pinned"]:
                    actions.append(
                        {"op": "guide_pin", "channel": name, "id": cid, "ts": keep["ts"]}
                    )
                for extra in guides[1:]:
                    actions.append(
                        {
                            "op": "guide_remove",
                            "channel": name,
                            "id": cid,
                            "ts": extra["ts"],
                            "pinned": extra["pinned"],
                            "why": "duplicate",
                        }
                    )
        else:
            for old in guides:
                actions.append(
                    {
                        "op": "guide_remove",
                        "channel": name,
                        "id": cid,
                        "ts": old["ts"],
                        "pinned": old["pinned"],
                        "why": "no guide declared",
                    }
                )
        want_bm = ch.get("bookmarks", [])
        if want_bm:
            live_bm = [] if new else ex["bookmarks"]
            if live_bm is None:
                notes.append(f"#{name}: bookmarks declared but no token can read them")
                continue
            by_title = {b["title"]: b for b in live_bm}
            for b in want_bm:
                cur = by_title.get(b["title"])
                if cur is None:
                    actions.append(
                        {
                            "op": "bookmark_add",
                            "channel": name,
                            "id": cid,
                            "title": b["title"],
                            "link": b["link"],
                        }
                    )
                elif cur.get("link") != b["link"]:
                    actions.append(
                        {
                            "op": "bookmark_edit",
                            "channel": name,
                            "id": cid,
                            "bookmark_id": cur["id"],
                            "title": b["title"],
                            "link": b["link"],
                            "was": cur.get("link"),
                        }
                    )
            row["undeclared_bookmarks"] = sorted(set(by_title) - {b["title"] for b in want_bm})
    return actions, notes


def read_extras(api: "Slack", bm: "Slack | None", declared: list, state: list, bot_id: str) -> dict:
    """The bot's guide messages (marker, newest first, with pin state) and the bookmarks, per
    existing declared channel the bot can read."""
    want = {c["name"] for c in declared if c.get("guide") is not None or c.get("bookmarks")}
    managed = {c["name"] for c in declared}
    out = {}
    for row in state:
        cid = row.get("id")
        if not cid or row.get("archived") or row.get("via") == "create" or not row.get("is_member"):
            continue
        if row["name"] not in managed:
            continue
        name = row["name"]
        pinned = {
            (i.get("message") or {}).get("ts")
            for i in api.call("pins.list", {"channel": cid})[0].get("items", [])
            if i.get("type") == "message"
        }
        hist = api.call("conversations.history", {"channel": cid, "limit": 200})[0].get(
            "messages", []
        )
        guides = [
            {
                "ts": m["ts"],
                "text": m.get("text", ""),
                "body": _block_body(m),
                "pinned": m["ts"] in pinned,
            }
            for m in hist
            if m.get("user") == bot_id
            if _norm(m.get("text", "")).endswith(marker(name))
        ]
        bms = None
        if bm is not None and name in want:
            bms = [
                {"id": b["id"], "title": b.get("title", ""), "link": b.get("link", "")}
                for b in bm.call("bookmarks.list", {"channel_id": cid})[0].get("bookmarks", [])
            ]
        out[cid] = {"guides": guides, "bookmarks": bms}
    return out


def needed_scopes(
    actions: list, admin_ops: frozenset = frozenset(), guides: bool = False
) -> list[str]:
    """Bot scopes the plan needs; ops in admin_ops run on the admin's user token instead."""
    ops = {a["op"] for a in actions} - set(admin_ops)
    reads = set(GUIDE_READ_SCOPES) if guides else set()
    return sorted({SCOPES["read"]} | reads | {SCOPES[op] for op in ops})


def survey(api: "Slack", bm: "Slack | None", declared: list, bot_id: str):
    """Read everything and plan: -> (actions, refusals, state, notes)."""
    live, members = read_state(api, declared)
    actions, refusals, state = plan(declared, live, members, bot_id)
    extras = read_extras(api, bm, declared, state, bot_id)
    ex_actions, notes = plan_extras(
        declared, state, extras, bot_id, {c["id"]: c["name"] for c in live}
    )
    return actions + ex_actions, refusals, state, notes


def read_state(api: Slack, declared: list):
    live = api.paged(
        "conversations.list",
        {"types": "public_channel", "exclude_archived": "false"},
        "channels",
    )
    names = {c["name"] for c in declared} | {
        c["rename_from"] for c in declared if c.get("rename_from")
    }
    members = {}
    for c in live:
        if c["name"] in names and not c.get("is_archived"):
            members[c["id"]] = api.paged("conversations.members", {"channel": c["id"]}, "members")
    return live, members


def execute(
    api: Slack, actions: list, admin: Slack | None = None, bm: Slack | None = None
) -> list[dict]:
    """Run actions in order; a failure skips every later step for the SAME declared channel.

    A rename's preceding join is keyed by the old name, so a failed join also fails the rename.
    """
    renamed_to = {a["from"]: a["channel"] for a in actions if a["op"] == "rename"}
    results, failed, created = [], set(), {}
    for a in actions:
        declared = renamed_to.get(a["channel"], a["channel"]) if a["op"] == "join" else a["channel"]
        chan_id = a.get("id") or created.get(a["channel"])
        if declared in failed:
            results.append(
                dict(a, ok=False, error="skipped: an earlier step for this channel failed")
            )
            continue
        try:
            if a["op"] == "create":
                body, dummy = api.call(
                    "conversations.create", {"name": a["channel"], "is_private": False}, post=True
                )
                chan_id = created[a["channel"]] = body["channel"]["id"]
            elif a["op"] == "join":
                api.call("conversations.join", {"channel": chan_id}, post=True)
            elif a["op"] == "rename":
                (admin or api).call(
                    "conversations.rename", {"channel": chan_id, "name": a["channel"]}, post=True
                )
            elif a["op"] == "invite":
                api.call(
                    "conversations.invite",
                    {"channel": chan_id, "users": ",".join(a["users"])},
                    post=True,
                )
            elif a["op"] == "purpose":
                api.call(
                    "conversations.setPurpose",
                    {"channel": chan_id, "purpose": a["value"]},
                    post=True,
                )
            elif a["op"] == "topic":
                api.call(
                    "conversations.setTopic", {"channel": chan_id, "topic": a["value"]}, post=True
                )
            elif a["op"] == "guide_post":
                body, dummy = api.call(
                    "chat.postMessage",
                    {
                        "channel": chan_id,
                        "text": a["value"],
                        "blocks": guide_blocks(a["channel"], a["guide"]),
                    },
                    post=True,
                )
                a = dict(a, ts=body["ts"])
                api.call("pins.add", {"channel": chan_id, "timestamp": body["ts"]}, post=True)
            elif a["op"] == "guide_update":
                api.call(
                    "chat.update",
                    {
                        "channel": chan_id,
                        "ts": a["ts"],
                        "text": a["value"],
                        "blocks": guide_blocks(a["channel"], a["guide"]),
                    },
                    post=True,
                )
            elif a["op"] == "guide_pin":
                api.call("pins.add", {"channel": chan_id, "timestamp": a["ts"]}, post=True)
            elif a["op"] == "guide_remove":
                if a.get("pinned"):
                    api.call("pins.remove", {"channel": chan_id, "timestamp": a["ts"]}, post=True)
                api.call("chat.delete", {"channel": chan_id, "ts": a["ts"]}, post=True)
            elif a["op"] in ("bookmark_add", "bookmark_edit"):
                if bm is None:
                    raise SlackError(a["op"], {"error": "no token holds bookmarks:write"})
                if a["op"] == "bookmark_add":
                    bm.call(
                        "bookmarks.add",
                        {
                            "channel_id": chan_id,
                            "title": a["title"],
                            "type": "link",
                            "link": a["link"],
                        },
                        post=True,
                    )
                else:
                    bm.call(
                        "bookmarks.edit",
                        {
                            "channel_id": chan_id,
                            "bookmark_id": a["bookmark_id"],
                            "title": a["title"],
                            "link": a["link"],
                        },
                        post=True,
                    )
            results.append(dict(a, id=chan_id, ok=True))
        except SlackError as exc:
            failed.add(declared)
            results.append(
                dict(
                    a,
                    id=chan_id,
                    ok=False,
                    error=str(exc),
                    slack_error=exc.body.get("error"),
                    needed=exc.body.get("needed", ""),
                    provided=exc.body.get("provided", ""),
                )
            )
    return results


def run(params: dict, check_mode: bool, sleep=time.sleep) -> dict:
    global _marker_prefix
    prefix = params.get("guide_marker_prefix", DEFAULT_MARKER_PREFIX)
    if not isinstance(prefix, str) or not prefix or any(c.isspace() for c in prefix):
        return {
            "failed": True,
            "msg": f"guide_marker_prefix must be a non-empty string without whitespace (got {prefix!r})",
        }
    _marker_prefix = prefix
    declared = params["channels"]
    problems = validate(declared)
    if problems:
        return {
            "failed": True,
            "msg": "invalid channel declaration: " + "; ".join(problems),
        }
    api = Slack(params["token"], params["api_url"], sleep=sleep)
    out = {"changed": False}
    try:
        auth, headers = api.call("auth.test")
        out.update(
            team=auth.get("team", ""),
            team_id=auth.get("team_id", ""),
            bot_user_id=auth.get("user_id", ""),
            scopes=sorted(
                s.strip() for s in headers.get("x-oauth-scopes", "").split(",") if s.strip()
            ),
        )
        out.update(
            enterprise_id=auth.get("enterprise_id") or "",
            is_enterprise_install=bool(auth.get("is_enterprise_install")),
        )
    except SlackError as exc:
        return dict(out, failed=True, msg=f"could not read the workspace: {exc}")

    # The workspace admin's USER token, used for the one step a bot is not authorized to take
    # (renaming a channel a human made). Read once so the report shows whose it is and its scopes.
    admin = None
    if params.get("admin_token"):
        admin = Slack(params["admin_token"], params["api_url"], sleep=sleep)
        try:
            a_auth, a_headers = admin.call("auth.test")
        except SlackError as exc:
            return dict(out, failed=True, msg=f"the admin token was refused: {exc}")
        out.update(
            admin_user_id=a_auth.get("user_id", ""),
            admin_scopes=sorted(
                s.strip() for s in a_headers.get("x-oauth-scopes", "").split(",") if s.strip()
            ),
        )

    # Bookmarks need bookmarks:read/write, which the owner's user token holds and the bot may not.
    bm = None
    if admin and {"bookmarks:read", "bookmarks:write"} <= set(out["admin_scopes"]):
        bm = admin
    elif {"bookmarks:read", "bookmarks:write"} <= set(out["scopes"]):
        bm = api
    admin_ops = frozenset(
        ({"rename"} if admin else set())
        | ({"bookmark_add", "bookmark_edit"} if bm is admin and admin else set())
    )
    # Every declared channel's guide state is read (a removed guide must still be found).
    guides_declared = True
    try:
        actions, refusals, state, notes = survey(api, bm, declared, out["bot_user_id"])
    except SlackError as exc:
        return dict(out, failed=True, msg=f"could not read the workspace: {exc}")
    if any(c.get("bookmarks") for c in declared) and bm is None:
        refusals.append(
            "bookmarks are declared but neither token holds bookmarks:read and bookmarks:write"
        )
    out.update(
        plan=actions,
        refusals=refusals,
        notes=notes,
        state=state,
        bookmarks_token="admin" if bm is admin and admin else ("bot" if bm else "none"),
        needed_scopes=needed_scopes(actions, admin_ops, guides_declared),
        missing_scopes=sorted(
            set(needed_scopes(actions, admin_ops, guides_declared)) - set(out["scopes"])
        ),
        rename_needs_admin_token=any(a["op"] == "rename" for a in actions) and not admin,
    )
    if admin:
        out["admin_missing_scopes"] = sorted(
            {ADMIN_SCOPES[a["op"]] for a in actions if a["op"] in admin_ops}
            - set(out["admin_scopes"])
        )
    if not params["apply"] or check_mode:
        return out
    if refusals:
        # Acting on the rest would converge a workspace whose declaration is known to be wrong.
        return dict(out, failed=True, msg="refusing to apply: " + "; ".join(refusals))
    if not actions:
        return out

    results = execute(api, actions, admin, bm)
    try:
        # ROUND 2, only after a clean round 1: what a join or a create made readable for the first
        # time (a channel's guide and bookmarks) is planned and done now, not on the next run.
        if all(r["ok"] for r in results):
            more, dummy, dummy, dummy = survey(api, bm, declared, out["bot_user_id"])
            if more:
                results += execute(api, more, admin, bm)
        out.update(results=results, changed=any(r["ok"] for r in results))
        # Read-back: re-read the workspace from scratch and re-plan; what is left is drift.
        remaining, dummy, state2, dummy = survey(api, bm, declared, out["bot_user_id"])
        for row in state2:
            if row.get("id") and not row.get("archived"):
                info, dummy = api.call("conversations.info", {"channel": row["id"]})
                c = info["channel"]
                row["readback"] = {k: c.get(k) for k in ("id", "name", "is_member", "is_archived")}
                row["readback"].update(
                    purpose=c.get("purpose", {}).get("value", ""),
                    topic=c.get("topic", {}).get("value", ""),
                )
    except SlackError as exc:
        return dict(
            out,
            results=results,
            changed=True,
            failed=True,
            msg=f"applied, but the read-back failed: {exc}",
        )
    out.update(remaining=remaining, state_after=state2)
    return out


def main():  # pragma: no cover - exercised by molecule against a stub Slack
    from ansible.module_utils.basic import AnsibleModule

    module = AnsibleModule(
        argument_spec={
            "token": {"type": "str", "required": True, "no_log": True},
            "api_url": {"type": "str", "default": "https://slack.com/api"},
            "channels": {"type": "list", "elements": "dict", "required": True},
            "apply": {"type": "bool", "default": False},
            "admin_token": {"type": "str", "default": "", "no_log": True},
            "guide_marker_prefix": {"type": "str", "default": DEFAULT_MARKER_PREFIX},
        },
        supports_check_mode=True,
    )
    out = run(module.params, module.check_mode)
    if out.pop("failed", False):
        module.fail_json(**out)
    module.exit_json(**out)


if __name__ == "__main__":
    main()
