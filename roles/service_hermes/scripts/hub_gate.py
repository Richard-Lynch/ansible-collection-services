#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""A pre_tool_call gate for Hermes: the SEND GATE and the PROTECTED-PATH rule.

Registered by service_hermes as a Hermes SHELL hook (config.yaml `hooks.pre_tool_call`, with
`fail_closed: true`), staged root-owned in the read-only policy mount beside the preflight, so the
agent can neither edit it nor its config. Hermes pipes one JSON payload on stdin per tool call:

    {"hook_event_name": "pre_tool_call", "tool_name": "...", "tool_input": {...}, "cwd": ..., ...}

and reads one JSON directive on stdout (hooks.md "JSON wire protocol"):

    {}                                                  no opinion, the tool runs
    {"action": "block",   "message": "..."}             the tool does not run
    {"action": "approve", "message": "...", "rule_key": "..."}
                                                        escalate to the human-approval gate
                                                        (Slack buttons on the gateway); denial,
                                                        timeout or a gate error FAILS CLOSED

THIS HOOK IS A BELT, NOT THE BOUNDARY. It reads the TEXT of a tool call, and text can always be
written so a reader of it is wrong (base64, a value read from a file, chr() arithmetic). The real
boundaries are structural and live elsewhere:

  * outbound mail: the CREDENTIAL. The Gmail consent must not grant a send-capable scope
    (gmail.send, gmail.compose, gmail.modify and https://mail.google.com/ all authorise
    users.messages.send), or the send-capable token must sit behind an approval-gated tool the
    agent cannot read. See the role README.
  * the subscription store: webhook_subscriptions.json is a read-only FILE mount
    (hermes_webhook_subscriptions_readonly), so a write fails with EROFS/EBUSY whatever this hook
    concluded.

THE SEND GATE. A plain regex over the raw command is bypassed by quoting, so the command is
normalised before anything is matched: shlex-split and
unquoted, quote-joined (`'sm''tp'`, `"sm"+"tp"`, `"sm" "tp"`), backslash- and escape-decoded
(`\\x73mtp`, `$'\\x73'`), and `${...}`/`$VAR` expansions dropped. A terminal or execute_code call
whose text, in ANY of those forms, MENTIONS mail (gmail, mail, smtp, sendmail, msmtp, mutt,
himalaya, swaks, or a mail API such as sendgrid/mailgun/postmark/sparkpost, or a curl to an
smtp(s):// URL) is a send that needs approval, unless EVERY shell segment that mentions mail is on
a NARROW read-only allowlist (`$GAPI gmail search|get|labels`, `gmail modify` with label flags
only, `himalaya envelope|folder|account list` and `message read`, and grep/cat-style readers with
no redirection). Asking when unsure is the design: a false ask costs one tap, a false pass is mail
nobody approved.

A send skips the tap ONLY when every recipient is allowlisted AND the gate can see every
recipient: a single `$GAPI gmail send|forward` segment, no shell expansion or substitution, no
stdin redirection, every --to/--cc/--bcc value parsing to addresses, and every address anywhere in
the text (raw, unquoted and decoded -- the UNION, never a subset) on the allowlist. execute_code
never skips: a Python recipient can come from anywhere. A reply has no recipient in its text and
always asks.

THE PROTECTED-PATH RULE. A write_file / patch whose path -- or whose
os.path.realpath, so a symlink to the store is the store -- matches a protected name is BLOCKED.
A terminal or code command that names one (in any normalised form above, through a glob that
matches it, or through a token whose realpath is one) and has a writing shape is BLOCKED.

What this hook can NOT see, said plainly: a send from a tool outside the matcher in config.yaml,
and a send whose mail-ness is computed at run time (base64, a file's contents). That is why the
credential is the boundary.

Never prints tool arguments to stderr or stdout beyond the recipient addresses it decided on.
"""

from __future__ import annotations

import codecs
import fnmatch
import json
import os
import re
import shlex
import sys
from typing import Any

# A FIXED PATH, deliberately with no environment override: Hermes loads the agent-writable data/.env
# into the hook's environment with override=True, so a variable naming the config would let a
# planted line swap in an allowlist of every recipient. `--config` is for the unit tests only
# (Hermes passes no arguments).
CONFIG_PATH = "/opt/hermes-policy/hub_gate.json"

_WRITE_TOOLS = {"write_file", "patch"}
_SHELL_TOOLS = {"terminal", "process_manage"}
_CODE_TOOLS = {"execute_code"}
_COMMAND_TOOLS = _SHELL_TOOLS | _CODE_TOOLS
_SEND_TOOLS = {"send_message", "send_email", "email_send", "gmail_send"}

# A MENTION of mail, matched against every normalised form of the command (all lower-cased).
# Deliberately substring-broad: `mail` covers email, sendmail, mailx, gmail; `smtp` covers smtplib,
# smtps://, aiosmtplib. The mail APIs without "mail" in their name are listed by name.
_MAIL_MENTION = re.compile(
    r"mail|smtp|mutt|himalaya|swaks|sendgrid|postmark|sparkpost|mandrill|sendinblue|brevo"
    r"|mailjet|amazonses|\bses\b|/dev/tcp|/dev/udp"
)
_ADDR_RE = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_RCPT_FLAGS = {"--to", "--cc", "--bcc", "-t", "-c", "-b"}
_GAPI_PROGRAMS = {"$gapi", "${gapi}", "gapi", "google_api.py", "gws"}
_READERS = {
    "grep",
    "egrep",
    "fgrep",
    "rg",
    "cat",
    "head",
    "tail",
    "wc",
    "ls",
    "less",
    "stat",
    "file",
}
_HIMALAYA_READ = {
    ("envelope", "list"),
    ("folder", "list"),
    ("account", "list"),
    ("message", "read"),
}
_HIMALAYA_VALUE_FLAGS = {
    "-a",
    "--account",
    "-o",
    "--output",
    "-c",
    "--config",
    "-f",
    "--folder",
}
_GMAIL_READ = {"search", "get", "labels"}
_SUBST = re.compile(r"\$\(|`|<\(|>\(")
_SEGMENT_OPS = {";", "&&", "||", "|", "&", "|&", ";;"}
_REDIRECTS = re.compile(r"^\d*(?:>|>>|<|<<|<<<|>&|&>|<>)$")

# A command that names a protected file is a WRITE when it also has one of these shapes.
_WRITE_SHAPES = re.compile(
    r">|\btee\b|\bsed\s+(?:-\S*\s+)*-i|\b(?:mv|cp|rm|install|ln|truncate|dd|chmod|chown|touch|rsync)\b"
    r"|\b(?:python3?|perl|ruby|node|jq\s+.*>)\b|\bopen\s*\(|write_text|write_bytes|\.write\(|\brename\b|\breplace\(",
    re.I | re.S,
)


def _load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as fh:
        cfg = json.load(fh)
    if not isinstance(cfg, dict):
        raise ValueError("hub_gate config is not a mapping")
    allowed = cfg.get("allowed_recipients", [])
    protected = cfg.get("protected_paths", [])
    if not isinstance(allowed, list) or not isinstance(protected, list) or not protected:
        # An empty protected list would make the path rule vacuous: refuse rather than pass.
        raise ValueError(
            "hub_gate config needs a list allowed_recipients and a non-empty protected_paths"
        )
    return {
        "allowed": {str(a).strip().lower() for a in allowed if str(a).strip()},
        "protected": [str(p) for p in protected],
    }


def _command_text(tool_input: Any) -> str:
    if not isinstance(tool_input, dict):
        return str(tool_input or "")
    parts = [
        str(tool_input.get(k))
        for k in ("command", "code", "input", "script", "args")
        if tool_input.get(k)
    ]
    return "\n".join(parts)


# ---- normalisation ---------------------------------------------------------------------------
def _tokens(text: str) -> list[str] | None:
    """shlex tokens with shell operators split out, or None when the text does not parse."""
    try:
        lex = shlex.shlex(text, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        lex.commenters = ""
        return list(lex)
    except ValueError:
        return None


def _decode_escapes(text: str) -> str:
    try:
        return codecs.decode(text.encode("latin-1", "backslashreplace"), "unicode_escape")
    except Exception:  # an undecodable escape: the other forms still stand
        return text


def forms(text: str) -> list[str]:
    """Every normalised form of `text`, lower-cased. A matcher must be run against ALL of them."""
    out = [text]
    toks = _tokens(text)
    if toks is not None:
        out.append(" ".join(toks))
    # Join adjacent string literals: 'sm''tp', "sm"+"tp", "sm" "tp", 'sm' + "tp".
    joined = re.sub(r"[\"']\s*\+?\s*[\"']", "", text)
    # Drop parameter expansions that an unset variable makes empty: gm${x}ail, gm$x'ail'.
    joined = re.sub(r"\$\{[^}]*\}|\$[A-Za-z_][A-Za-z0-9_]*(?=[A-Za-z0-9_.'\"])", "", joined)
    out.append(joined)
    out.append(re.sub(r"[\"'\\]", "", joined))
    out.append(_decode_escapes(text))
    out.append(re.sub(r"[\"'\\]", "", _decode_escapes(joined)))
    seen: list[str] = []
    for f in out:
        f = f.lower()
        if f not in seen:
            seen.append(f)
    return seen


def mentions_mail(text: str) -> bool:
    return any(_MAIL_MENTION.search(f) for f in forms(text))


def _segments(toks: list[str]) -> list[list[str]]:
    segs: list[list[str]] = [[]]
    for t in toks:
        if t in _SEGMENT_OPS:
            segs.append([])
        else:
            segs[-1].append(t)
    return [s for s in segs if s]


def _program(seg: list[str]) -> tuple[str, list[str]]:
    """The program a segment runs (env assignments and an interpreter prefix skipped)."""
    i = 0
    while i < len(seg) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", seg[i]):
        i += 1
    rest = seg[i:]
    if not rest:
        return "", []
    prog = rest[0]
    # `python3 /path/google_api.py gmail ...` is the skill, not an arbitrary interpreter run.
    if (
        os.path.basename(prog).startswith("python")
        and len(rest) > 1
        and rest[1].endswith("google_api.py")
    ):
        return "google_api.py", rest[2:]
    base = prog.lower() if prog.startswith("$") else os.path.basename(prog).lower()
    return base, rest[1:]


def _segment_is_read_only(seg: list[str]) -> bool:
    """True only for the narrow allowlist of mail READS. Anything unrecognised is not."""
    if any(_REDIRECTS.match(t) for t in seg):
        return False
    prog, args = _program(seg)
    if prog in _GAPI_PROGRAMS:
        if len(args) < 2 or args[0].lower() != "gmail":
            return False
        verb = args[1].lower()
        if verb in _GMAIL_READ:
            return not (verb == "labels" and len(args) > 2 and args[2].lower() not in {"list"})
        if verb == "modify":
            # Label changes only: every flag must be a label flag.
            flags = [a for a in args[2:] if a.startswith("-")]
            return all(f.split("=")[0] in {"--add-labels", "--remove-labels"} for f in flags)
        return False
    if prog == "himalaya":
        words: list[str] = []
        skip = False
        for a in args:
            if skip:
                skip = False
                continue
            if a in _HIMALAYA_VALUE_FLAGS:
                skip = True
                continue
            if a.startswith("-"):
                continue
            words.append(a.lower())
        return tuple(words[:2]) in _HIMALAYA_READ
    if prog in _READERS:
        return True
    return False


def send_needs_check(tool: str, text: str) -> bool:
    """A command tool call that mentions mail and is not entirely on the read-only allowlist."""
    if not mentions_mail(text):
        return False
    if tool in _CODE_TOOLS:
        return True  # no shell parse of Python: any mention asks
    if _SUBST.search(text):
        return True
    for line in text.splitlines() or [text]:
        toks = _tokens(line)
        if toks is None:
            return True  # unparseable shell that mentions mail asks
        for seg in _segments(toks):
            if mentions_mail(" ".join(seg)) and not _segment_is_read_only(seg):
                return True
    # Every line parsed and every mail-mentioning segment is a read. A mention spread across lines
    # (a quoted newline) fails shlex on one of the lines above and asks.
    return False


# ---- recipients ------------------------------------------------------------------------------
def recipients(text: str) -> list[str]:
    """The UNION of every address in every normalised form of the text. Never a subset."""
    found: list[str] = []
    for f in forms(text):
        for addr in _ADDR_RE.findall(f):
            a = addr.strip().strip("'").lower()
            if a not in found:
                found.append(a)
    return sorted(found)


def _flag_values_parse(args: list[str]) -> bool:
    """Every recipient flag carries a value that is wholly addresses (or `Name <addr>`)."""
    i = 0
    while i < len(args):
        a = args[i]
        name, eq, val = a.partition("=")
        if name.lower() in _RCPT_FLAGS:
            if not eq:
                if i + 1 >= len(args):
                    return False
                val = args[i + 1]
                i += 1
            for item in val.split(","):
                item = item.strip()
                m = re.fullmatch(r"(?:[^<>]*<)?\s*([^<>\s]+)\s*>?", item)
                if not m or not _ADDR_RE.fullmatch(m.group(1)):
                    return False
        i += 1
    return True


def _send_is_fully_visible(tool: str, text: str) -> bool:
    """Can the gate see every recipient of this send? Only a single plain `$GAPI gmail send`."""
    if tool not in _SHELL_TOOLS or _SUBST.search(text) or "\n" in text.strip():
        return False
    toks = _tokens(text)
    if toks is None:
        return False
    segs = _segments(toks)
    if len(segs) != 1 or len(segs[0]) != len(toks):
        return False
    seg = segs[0]
    if any(_REDIRECTS.match(t) for t in seg):
        return False
    prog, args = _program(seg)
    if prog not in _GAPI_PROGRAMS or len(args) < 2 or args[0].lower() != "gmail":
        return False
    if args[1].lower() not in {"send", "forward"}:
        return False
    # No expansion anywhere but the program token itself: `--bcc "$X"` hides a recipient.
    if any("$" in t for t in args):
        return False
    return _flag_values_parse(args[2:])


# ---- protected paths -------------------------------------------------------------------------
def _resolve(path: str, cwd: str) -> str:
    p = os.path.expanduser(path)
    if not os.path.isabs(p):
        p = os.path.join(cwd or os.getcwd(), p)
    return os.path.realpath(p)


def _is_protected(path: str, protected: list[str], cwd: str = "") -> str | None:
    """The path, or its realpath (a symlink to the store IS the store), matches a protected name."""
    for candidate in (path.rstrip("/"), _resolve(path, cwd)):
        base = os.path.basename(candidate)
        for pattern in protected:
            if fnmatch.fnmatch(base, pattern) or fnmatch.fnmatch(candidate, pattern):
                return pattern
    return None


def _names_protected(text: str, protected: list[str], cwd: str = "") -> str | None:
    for f in forms(text):
        for pattern in protected:
            literal = pattern.replace("*", "").lower()
            if literal and literal in f:
                return pattern
    toks = _tokens(text) or text.split()
    for t in toks:
        if t in _SEGMENT_OPS or _REDIRECTS.match(t):
            continue
        base = os.path.basename(t.rstrip("/"))
        for pattern in protected:
            # A glob in the command that would expand to the protected name: `webhook_*.json`.
            # A bare `*` is left to the read-only mount (it would block every `rm *`).
            literal = re.sub(r"[*?]|\[[^]]*\]", "", base)
            if (
                literal != base
                and len(literal) >= 3
                and fnmatch.fnmatch(pattern.replace("*", "x"), base)
            ):
                return pattern
        if "/" in t or os.path.lexists(_resolve(t, cwd) if t else ""):
            hit = _is_protected(t, protected, cwd) if t else None
            if hit:
                return hit
    return None


def decide(payload: dict, cfg: dict) -> dict:
    tool = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input") or {}
    cwd = str(payload.get("cwd") or "")

    # ---- protected paths -----------------------------------------------------------------
    if tool in _WRITE_TOOLS and isinstance(tool_input, dict):
        path = str(tool_input.get("path") or tool_input.get("file_path") or "")
        hit = _is_protected(path, cfg["protected"], cwd) if path else None
        if hit:
            return {
                "action": "block",
                "message": (
                    f"{os.path.basename(path)} is a protected path ({hit}): webhook subscriptions are changed with "
                    "`hermes webhook subscribe/remove`, and a route's toolsets only in the deployment's config."
                ),
            }
    text = _command_text(tool_input) if tool in _COMMAND_TOOLS else ""
    if text:
        hit = _names_protected(text, cfg["protected"], cwd)
        if hit and any(_WRITE_SHAPES.search(f) for f in forms(text)):
            return {
                "action": "block",
                "message": (
                    f"This command writes a protected path ({hit}). Use `hermes webhook subscribe/remove`; a route's "
                    "toolsets are deployment config."
                ),
            }

    # ---- send gate -----------------------------------------------------------------------
    if tool in _SEND_TOOLS:
        rcpts = recipients(json.dumps(tool_input, sort_keys=True))
        visible = True
    elif text and send_needs_check(tool, text):
        rcpts = recipients(text)
        visible = _send_is_fully_visible(tool, text)
    else:
        return {}
    if visible and rcpts and all(r in cfg["allowed"] for r in rcpts):
        return {}
    shown = ", ".join(rcpts) if rcpts else "a recipient the gate could not read"
    key = (
        ",".join(rcpts)
        if rcpts and visible
        else ("unparsed" if not rcpts else "unparsed:" + ",".join(rcpts))
    )
    return {
        "action": "approve",
        "message": f"Send gate: {tool} may send email to {shown}. Approve to run this once.",
        "rule_key": f"send-gate:{key}",
    }


def main(argv: list[str] | None = None) -> int:
    global CONFIG_PATH  # noqa: PLW0603 - the test-only flag rebinds the fixed path
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) == 2 and args[0] == "--config":
        CONFIG_PATH = args[1]
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            raise ValueError("payload is not a JSON object")
        cfg = _load_config()
        directive = decide(payload, cfg)
    except Exception as exc:  # fail CLOSED: exit 2 blocks the tool, the reason goes to stderr
        sys.stderr.write(f"hub_gate could not decide ({type(exc).__name__}); blocking\n")
        return 2
    sys.stdout.write(json.dumps(directive) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
