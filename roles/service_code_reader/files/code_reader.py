#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""code_reader -- hand a one-time verification code from a mailbox to the browser filler, once.

WHY THIS EXISTS. A sign-up or login emails "your code is 482913". The agent's browser filler has
to type it, but the language model must never SEE it: a code in a tool result lands in the
transcript, the logs and the provider's request history. So the code goes mailbox -> this
sidecar -> the filler's HTTP call -> the page, and the model is only told "code filled: true".

THE PROPERTY, and where each half is enforced:
  * a code is HANDED OUT AT MOST ONCE   -> CodeStore.take() deletes what it returns
  * a code is HANDED OUT FOR AT MOST ttl -> CodeStore.take() / purge() drop anything older
  * only ALLOWLISTED senders count       -> sender_allowed(), exact address or "@domain"
  * the code is NEVER LOGGED             -> log_event() refuses a `code` field, and no call site
                                            passes one

stdlib only, on purpose: it runs in a stock python image with this one file mounted, so there is
nothing to pip-install, pin or audit. Configuration is a JSON file (no secrets) plus two secrets
from the environment (IMAP_PASSWORD, CODE_READER_TOKEN) -- never argv, never the config file.
"""

from __future__ import annotations

import email
import email.message
import email.policy
import email.utils
import hmac
import imaplib
import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

# Group 1 is the code. Ordered: the first pattern that matches wins, so the pattern that anchors
# on the WORD "code" comes before the looser one. That ordering is what makes a mail containing
# an order number AND a code ("Order 12345678 ... your code is 482913") pick the code.
#
# The gap between the keyword and the digits stops at sentence punctuation (. ! ?), not just at
# the next digit: "PIN reset requested. Ref 9999. Your code is 482913" must not hand out 9999,
# which a gap of "any 30 non-digits" would. A newline is still allowed
# in the gap, because "Your code:\n482913" is a common layout.
DEFAULT_CODE_PATTERNS = [
    r"(?i)\b(?:code|passcode|otp|pin)\b[^\d.!?]{0,30}?\b(\d{4,8})\b",
    r"(?i)\b(\d{4,8})\b\s+is\s+(?:your|the)\s+(?:[a-z-]+\s+){0,3}(?:code|passcode|otp|pin)\b",
]

# Bounds that make the poller's memory a constant rather than a function of mailbox size.
MAX_SEEN = 5000
MAX_BODY_CHARS = 20000


def log_event(**fields: Any) -> None:
    """One JSON line to stdout. A `code` field is refused outright."""
    if "code" in fields:
        raise ValueError("refusing to log a code")
    print(json.dumps(fields, sort_keys=True), flush=True)


# --------------------------------------------------------------------------------------------
# Pure decisions: no network, no clock of their own. These are what the tests pin down.
# --------------------------------------------------------------------------------------------


def sender_allowed(address: str, allowlist: list[str]) -> bool:
    """Exact address ("a@b.com") or whole-domain ("@b.com") match, case-insensitive.

    "@b.com" is the domain EXACTLY: it does not admit "x@evil-b.com", "x@b.com.evil.net" or
    "x@sub.b.com". A suffix/substring match here would be the guard that reads as strict and is
    not.
    """
    addr = address.strip().lower()
    if "@" not in addr:
        return False
    domain = addr.rsplit("@", 1)[1]
    for entry in allowlist:
        e = entry.strip().lower()
        if not e:
            continue
        if e.startswith("@"):
            if domain == e[1:]:
                return True
        elif addr == e:
            return True
    return False


def normalise_origin(value: str) -> str:
    """'https://Accounts.Example.com:8443/path' -> 'accounts.example.com'."""
    v = value.strip().lower()
    if "://" not in v:
        v = "//" + v
    return (urlparse(v).hostname or "").strip(".")


def origin_matches(sender_domain: str, origin: str) -> bool:
    """A code from noreply@mail.example.com satisfies a request for example.com, and the reverse.

    Label-boundary match only: 'badexample.com' is NOT 'example.com'.

    BOTH directions on purpose: mail comes from a subdomain of the site (mail.example.com) about
    as often as the site is a subdomain of the mail domain (accounts.example.com served by
    noreply@example.com). The cost, accepted as a residual,
    is that on a shared-tenant domain a request for tenant.platform.com can take the platform's
    own code -- and `purpose` is chosen by the caller's filler, never by a page.
    """
    s, o = sender_domain.strip(".").lower(), origin.strip(".").lower()
    if not s or not o:
        return False
    return s == o or s.endswith("." + o) or o.endswith("." + s)


def extract_code(text: str, patterns: list[str]) -> str | None:
    """First pattern wins; group 1 is the code. No match means NO code -- never 'the first
    number', which would hand the filler an order number as though it were a code."""
    for pat in patterns:
        m = re.search(pat, text)
        if m and m.group(1):
            return m.group(1)
    return None


@dataclass
class Entry:
    code: str
    sender: str
    origin_domain: str
    received_at: float
    stored_at: float


class CodeStore:
    """Codes handed out once. The clock is a parameter so TTL is testable."""

    def __init__(self, ttl_seconds: int) -> None:
        self.ttl = ttl_seconds
        self._entries: list[Entry] = []
        self._lock = threading.Lock()

    def add(self, entry: Entry) -> None:
        with self._lock:
            self._entries.append(entry)

    def purge(self, now: float) -> list[Entry]:
        """Drop expired entries and return them so the caller can log the expiry."""
        with self._lock:
            keep = [e for e in self._entries if now - e.received_at <= self.ttl]
            gone = [e for e in self._entries if now - e.received_at > self.ttl]
            self._entries = keep
        return gone

    def take(self, origin: str, now: float) -> Entry | None:
        """Newest live code whose sender matches `origin`; REMOVED from the store on return."""
        with self._lock:
            live = [
                e
                for e in self._entries
                if now - e.received_at <= self.ttl and origin_matches(e.origin_domain, origin)
            ]
            if not live:
                return None
            chosen = max(live, key=lambda e: e.received_at)
            # Serving the newest SUPERSEDES the older codes for the same origin. After a "resend"
            # the older code is stale, and leaving it live would hand it to the next GET as though
            # it were fresh -- "a second GET is a 404" has to hold per origin, not per mail.
            self._entries = [e for e in self._entries if e not in live]
            return chosen

    def pending(self) -> int:
        with self._lock:
            return len(self._entries)


def message_text(msg: email.message.Message) -> str:
    """Subject plus the text/plain parts (HTML stripped to text only if there is no plain part)."""
    parts = [str(msg.get("Subject", ""))]
    plain: list[str] = []
    html: list[str] = []
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            payload = part.get_content()  # type: ignore[attr-defined]
        except Exception:  # undecodable part: skip it rather than lose the whole message
            continue
        (plain if ctype == "text/plain" else html).append(str(payload))
    if plain:
        parts.extend(plain)
    else:
        parts.extend(re.sub(r"<[^>]+>", " ", h) for h in html)
    return "\n".join(parts)[:MAX_BODY_CHARS]


def sender_address(msg: email.message.Message) -> str | None:
    """The one address in the From header, lower-cased -- or None when the header is not trusted.

    NOT parseaddr(): under email.policy.default the malformed header
    `From: noreply@example.com <attacker@evil.com>` is parsed as noreply@example.com with the
    angle-addr DROPPED and two defects recorded, while mail clients (and DMARC alignment) treat
    the sender as attacker@evil.com. With @example.com allowlisted that mail would get a code STORED,
    and since take() serves the newest code it would replace the genuine one.
    So the header is refused outright when it carries parse defects, when there is more than one
    From header, or when it holds anything other than exactly one address.
    """
    headers = msg.get_all("From") or []
    if len(headers) != 1:
        return None
    header = headers[0]
    if getattr(header, "defects", None):
        return None
    addresses = getattr(header, "addresses", ())
    if len(addresses) != 1:
        return None
    spec = addresses[0].addr_spec
    if "@" not in spec or not addresses[0].username or not addresses[0].domain:
        return None
    return spec.lower()


def process_message(
    raw: bytes,
    internal_date: float | None,
    cfg: dict[str, Any],
    store: CodeStore,
    now: float,
) -> str:
    """Parse one mail and store its code if it qualifies. Returns an outcome word for the log."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    sender = sender_address(msg)
    if sender is None:
        log_event(event="ignored", sender="(refused)", reason="From header not trusted")
        return "ignored"
    if not sender_allowed(sender, cfg["sender_allowlist"]):
        log_event(event="ignored", sender=sender or "(none)", reason="sender not allowlisted")
        return "ignored"
    received = internal_date if internal_date is not None else now
    if now - received > cfg["ttl_seconds"]:
        log_event(event="expired", sender=sender, received_at=int(received), reason="stale")
        return "expired"
    code = extract_code(message_text(msg), cfg["code_patterns"])
    if code is None:
        log_event(event="no_code", sender=sender, received_at=int(received))
        return "no_code"
    domain = sender.rsplit("@", 1)[1].lower()
    store.add(Entry(code, sender, domain, received, now))
    log_event(event="stored", sender=sender, origin=domain, received_at=int(received))
    return "stored"


# --------------------------------------------------------------------------------------------
# I/O: the IMAP poller and the HTTP surface.
# --------------------------------------------------------------------------------------------


class Poller:
    def __init__(self, cfg: dict[str, Any], password: str, store: CodeStore) -> None:
        self.cfg, self.password, self.store = cfg, password, store
        self.seen: set[bytes] = set()
        self.last_ok: float | None = None
        self.last_error: str = "never polled"

    def poll_once(self, now: float) -> None:
        cfg = self.cfg
        try:
            conn = imaplib.IMAP4_SSL(cfg["imap_host"], cfg["imap_port"], timeout=30)
            try:
                conn.login(cfg["imap_user"], self.password)
                conn.select(cfg["imap_folder"])
                typ, data = conn.uid("SEARCH", "UNSEEN")
                uids = (data[0] or b"").split() if typ == "OK" and data else []
                for uid in uids:
                    if uid in self.seen:
                        continue
                    typ, msg_data = conn.uid("FETCH", uid, "(INTERNALDATE BODY.PEEK[])")
                    if typ != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                        continue
                    header, raw = msg_data[0]
                    internal = None
                    parsed = imaplib.Internaldate2tuple(header)
                    if parsed:
                        internal = time.mktime(parsed)
                    outcome = process_message(raw, internal, cfg, self.store, now)
                    self.seen.add(uid)
                    if outcome == "stored":
                        conn.uid("STORE", uid, "+FLAGS", "(\\Seen)")
                if len(self.seen) > MAX_SEEN:
                    self.seen.clear()
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass
            self.last_ok, self.last_error = now, ""
        except Exception as exc:
            # The exception TEXT may quote server output; record its type only, never its message.
            self.last_error = type(exc).__name__
            log_event(event="poll_failed", error=self.last_error)

    def run_forever(
        self,
    ) -> None:  # pragma: no cover -- the loop; poll_once is what is tested
        while True:
            now = time.time()
            self.poll_once(now)
            for gone in self.store.purge(now):
                log_event(
                    event="expired",
                    sender=gone.sender,
                    origin=gone.origin_domain,
                    received_at=int(gone.received_at),
                    reason="ttl",
                )
            time.sleep(self.cfg["poll_seconds"])


def make_handler(token: str, store: CodeStore, poller: Poller | None) -> type:
    class Handler(BaseHTTPRequestHandler):
        server_version = "code-reader"

        def log_message(self, *args: Any) -> None:  # the default writes the QUERY STRING
            pass

        def _send(self, status: int, body: dict[str, Any]) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802 -- http.server's name
            url = urlparse(self.path)
            if url.path == "/healthz":
                ok = poller is not None and poller.last_ok is not None and not poller.last_error
                age = None
                if poller is not None and poller.last_ok is not None:
                    age = int(time.time() - poller.last_ok)
                self._send(
                    200 if ok else 503,
                    {
                        "ok": ok,
                        "last_poll_age_s": age,
                        "last_error": poller.last_error if poller else "no poller",
                        "pending": store.pending(),
                    },
                )
                return
            if url.path != "/code":
                self._send(404, {"error": "not found"})
                return
            supplied = self.headers.get("Authorization", "")
            if not hmac.compare_digest(supplied.encode(), ("Bearer " + token).encode()):
                self._send(401, {"error": "unauthorized"})
                return
            origin = normalise_origin((parse_qs(url.query).get("purpose") or [""])[0])
            if not origin:
                self._send(400, {"error": "purpose (an origin) is required"})
                return
            entry = store.take(origin, time.time())
            if entry is None:
                log_event(event="miss", origin=origin)
                self._send(404, {"error": "no code"})
                return
            log_event(
                event="served",
                sender=entry.sender,
                origin=origin,
                received_at=int(entry.received_at),
            )
            self._send(200, {"code": entry.code})

    return Handler


def load_config(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        cfg: dict[str, Any] = json.load(fh)
    # Extra patterns from the config are tried BEFORE the built-ins, as the argspec says; they
    # never REPLACE the built-ins.
    patterns = list(cfg.get("code_patterns") or []) + DEFAULT_CODE_PATTERNS
    for p in patterns:
        if re.compile(p).groups < 1:
            raise ValueError("every code pattern needs a capture group (group 1 is the code)")
    cfg["code_patterns"] = patterns
    if not cfg.get("sender_allowlist"):
        raise ValueError("sender_allowlist is empty: refusing to accept codes from anyone")
    return cfg


def main() -> int:  # pragma: no cover -- wiring
    cfg = load_config(os.environ.get("CODE_READER_CONFIG", "/config/config.json"))
    password = os.environ.get("IMAP_PASSWORD", "")
    token = os.environ.get("CODE_READER_TOKEN", "")
    if not password or len(token) < 32:
        print(
            "IMAP_PASSWORD missing or CODE_READER_TOKEN shorter than 32 chars",
            file=sys.stderr,
        )
        return 2
    store = CodeStore(cfg["ttl_seconds"])
    poller = Poller(cfg, password, store)
    threading.Thread(target=poller.run_forever, daemon=True).start()
    # Inside the container this listens on all interfaces so sibling containers on the docker
    # network can reach it; HOST exposure is decided by the compose `ports:` line (loopback only).
    server = ThreadingHTTPServer(
        (cfg.get("listen", "0.0.0.0"), cfg["port"]), make_handler(token, store, poller)
    )
    log_event(
        event="started",
        port=cfg["port"],
        ttl_seconds=cfg["ttl_seconds"],
        allowlist_entries=len(cfg["sender_allowlist"]),
    )
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
