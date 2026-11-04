#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""The gateway `post_tool_call` audit hook: one VALUE-FREE JSON line per tool call.

Hermes runs this as a shell hook after every tool call in every profile and on every platform,
including API runs (`POST /v1/runs`): the call goes through model_tools.handle_function_call, which
emits post_tool_call with the call's identity, its duration and its outcome (v0.22.5). The hook is
declared in the managed scope (tier C), so no profile can drop it.

WHAT A RECORD CARRIES, and only this (an allowlist, never a denylist):

    ts           UTC, ISO 8601, written by this script
    event        "tool_call"
    tool         the tool's name
    profile      the profile that ran it (upstream resolves it at fire time)
    session_id   the session's id, for correlating the calls of one run
    tool_call_id the call's id
    duration_ms  integer
    outcome      ok | error | blocked (upstream's `status`), else "unknown"
    error_type   an exception class name or upstream's error label, else absent

Every field is sanitised to a narrow character set and length, so a field that unexpectedly carries
free text is replaced by "invalid" rather than copied. WHAT IS NEVER WRITTEN: the arguments
(`tool_input`), the result, upstream's `error_message` (an exception's text routinely quotes the
argument that broke it), the working directory and the middleware trace. A useful proof on a
live deployment plants a canary value in a real /v1/runs call and requires it ABSENT from every
log stream the agent produces.

Where it goes: one append-only file under the agent's data volume, for a log shipper on the guest
to pick up (the calling play's log source). One write() per line on an
O_APPEND descriptor, so concurrent calls (post_tool_call fires concurrently for parallel tool calls)
do not interleave. Rotated to `<file>.1` past a size cap, so the volume cannot fill.

It must never get in the way: post_tool_call is an observer event (upstream ignores its decision and
fails open), and this script additionally prints nothing and exits 0 on every path, so a full disk
or a malformed payload costs one audit line and never a tool call. Stdlib only; it runs in the Hermes
image's interpreter.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone

DEFAULT_PATH = "/opt/data/logs/tool-audit.jsonl"
MAX_BYTES = 20 * 1024 * 1024

_NAME = re.compile(r"^[A-Za-z0-9_.:\-]{1,128}$")
_ID = re.compile(r"^[A-Za-z0-9_.:\-]{1,128}$")
_ERROR_TYPE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,63}$")
OUTCOMES = {"ok", "error", "blocked"}


def _clean(value: object, pattern: re.Pattern, *, missing: str | None = None) -> str | None:
    """`value` when it is a short string of the allowed characters, "invalid" when it is anything
    else that is present, `missing` when it is absent or empty."""
    if value is None or value == "":
        return missing
    if isinstance(value, str) and pattern.match(value):
        return value
    return "invalid"


def _duration(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and 0 <= value < 10**9:
        return int(value)
    return None


def build_record(payload: object, now: datetime | None = None) -> dict | None:
    """The value-free record for one hook payload, or None when it is not a post_tool_call."""
    if not isinstance(payload, dict):
        return None
    if payload.get("hook_event_name") not in (None, "post_tool_call"):
        return None
    extra = payload.get("extra")
    if not isinstance(extra, dict):
        extra = {}
    status = extra.get("status")
    record = {
        "ts": (now or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z"),
        "event": "tool_call",
        "tool": _clean(payload.get("tool_name"), _NAME, missing="unknown"),
        "profile": _clean(payload.get("profile"), _NAME, missing="unknown"),
        "session_id": _clean(payload.get("session_id"), _ID),
        "tool_call_id": _clean(extra.get("tool_call_id"), _ID),
        "duration_ms": _duration(extra.get("duration_ms")),
        "outcome": status if isinstance(status, str) and status in OUTCOMES else "unknown",
    }
    error_type = _clean(extra.get("error_type"), _ERROR_TYPE)
    if error_type is not None:
        record["error_type"] = error_type
    return {k: v for k, v in record.items() if v is not None}


def _rotate_if_large(path: str) -> None:
    try:
        if os.path.getsize(path) >= MAX_BYTES:
            os.replace(path, path + ".1")
    except OSError:
        pass  # absent (first write) or a concurrent rotation won the race


def append(path: str, record: dict) -> None:
    line = (json.dumps(record, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")
    _rotate_if_large(path)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def main(argv=None, stdin=None) -> int:
    """Hermes runs this with NO arguments. `--out <path>` exists for the unit tests only; there is
    deliberately no environment override, so nothing the agent can put in its env redirects or
    silences the audit trail."""
    args = list(sys.argv[1:] if argv is None else argv)
    path = DEFAULT_PATH
    if len(args) == 2 and args[0] == "--out":
        path = args[1]
    stream = stdin if stdin is not None else sys.stdin
    try:
        payload = json.loads(stream.read() or "null")
    except (ValueError, OSError):
        return 0
    record = build_record(payload)
    if record is None:
        return 0
    try:
        append(path, record)
    except OSError:
        pass  # never fail a tool call over an audit line
    return 0


if __name__ == "__main__":
    sys.exit(main())
