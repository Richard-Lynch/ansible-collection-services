#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for code_reader.py. Run: python3 roles/service_code_reader/files/test_code_reader.py

Every refusal here has a near-miss that must PASS beside it, because a guard that was never shown
to fire (or never shown to let the right thing through) reads as protection and may be neither.
The IMAP server is a fake; the HTTP surface is a real server on an ephemeral loopback port.
"""

from __future__ import annotations

import contextlib
import imaplib
import importlib.util
import io
import json
import pathlib
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

HERE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("code_reader", HERE / "code_reader.py")
assert _spec and _spec.loader
cr = importlib.util.module_from_spec(_spec)
sys.modules["code_reader"] = cr
_spec.loader.exec_module(cr)

TOKEN = "t" * 40
NOW = 1_000_000.0
CFG = {
    "sender_allowlist": ["noreply@example.com", "@trusted.org"],
    "ttl_seconds": 600,
    "code_patterns": cr.DEFAULT_CODE_PATTERNS,
    "imap_host": "imap.invalid",
    "imap_port": 993,
    "imap_user": "codes@agent.invalid",
    "imap_folder": "INBOX",
}


def mail(sender: str, body: str, subject: str = "Your sign-in") -> bytes:
    return (
        f"From: Service <{sender}>\r\nTo: codes@agent.invalid\r\nSubject: {subject}\r\n"
        f"Content-Type: text/plain; charset=utf-8\r\n\r\n{body}\r\n"
    ).encode()


def captured(fn):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = fn()
    return result, buf.getvalue()


class SenderAllowlist(unittest.TestCase):
    def test_exact_and_domain_entries_match(self):
        al = CFG["sender_allowlist"]
        self.assertTrue(cr.sender_allowed("noreply@example.com", al))
        self.assertTrue(cr.sender_allowed("NoReply@Example.COM", al))
        self.assertTrue(cr.sender_allowed("anyone@trusted.org", al))

    def test_near_misses_are_refused(self):
        al = CFG["sender_allowlist"]
        for bad in (
            "other@example.com",  # exact entry is one address, not the domain
            "noreply@example.com.evil.net",
            "x@evil-trusted.org",
            "x@sub.trusted.org",  # "@domain" is that domain exactly
            "x@nottrusted.org",
            "no-at-sign",
            "",
        ):
            self.assertFalse(cr.sender_allowed(bad, al), bad)

    def test_empty_allowlist_admits_nobody(self):
        self.assertFalse(cr.sender_allowed("noreply@example.com", []))
        self.assertFalse(cr.sender_allowed("noreply@example.com", [""]))


class Extraction(unittest.TestCase):
    P = cr.DEFAULT_CODE_PATTERNS

    def test_code_after_the_word_code(self):
        self.assertEqual(cr.extract_code("Your verification code is 482913.", self.P), "482913")

    def test_two_numbers_picks_the_one_after_code(self):
        text = "Order 12345678 shipped. Your code is 482913 for account 998877."
        self.assertEqual(cr.extract_code(text, self.P), "482913")

    def test_code_before_the_phrase(self):
        self.assertEqual(cr.extract_code("482913 is your login code", self.P), "482913")

    def test_keyword_in_an_earlier_sentence_does_not_capture_that_sentence_s_number(
        self,
    ):
        # "PIN ... Ref 9999." picked 9999 when the gap spanned sentences.
        text = "PIN reset requested. Ref 9999. Your code is 482913"
        self.assertEqual(cr.extract_code(text, self.P), "482913")
        # Near-miss: a newline inside the gap is still fine ("Your code:\n482913").
        self.assertEqual(cr.extract_code("Your code:\n482913\n", self.P), "482913")

    def test_no_code_word_means_no_code(self):
        # A bare number is an order number, not a code. Returning it would be the silent guard.
        self.assertIsNone(cr.extract_code("Order 12345678 has shipped", self.P))

    def test_origin_matching_is_label_bounded(self):
        self.assertTrue(cr.origin_matches("mail.example.com", "example.com"))
        self.assertTrue(cr.origin_matches("example.com", "accounts.example.com"))
        self.assertFalse(cr.origin_matches("badexample.com", "example.com"))
        self.assertEqual(
            cr.normalise_origin("https://Accounts.Example.com:8443/x"),
            "accounts.example.com",
        )


class Store(unittest.TestCase):
    def _store(self, received=NOW):
        s = cr.CodeStore(600)
        s.add(cr.Entry("111111", "noreply@example.com", "example.com", received, received))
        return s

    def test_served_once(self):
        s = self._store()
        self.assertEqual(s.take("example.com", NOW + 1).code, "111111")
        self.assertIsNone(s.take("example.com", NOW + 2))  # the second GET is a 404

    def test_ttl_boundary(self):
        self.assertIsNotNone(self._store().take("example.com", NOW + 600))  # inside
        self.assertIsNone(self._store().take("example.com", NOW + 601))  # outside

    def test_purge_returns_what_expired(self):
        s = self._store()
        self.assertEqual(len(s.purge(NOW + 601)), 1)
        self.assertEqual(s.pending(), 0)

    def test_other_origin_does_not_consume_it(self):
        s = self._store()
        self.assertIsNone(s.take("other.org", NOW + 1))
        self.assertIsNotNone(s.take("example.com", NOW + 2))

    def test_newest_wins_and_the_superseded_code_is_dropped(self):
        s = cr.CodeStore(600)
        s.add(cr.Entry("old000", "noreply@example.com", "example.com", NOW, NOW))
        s.add(cr.Entry("new111", "noreply@example.com", "example.com", NOW + 5, NOW + 5))
        s.add(cr.Entry("other2", "noreply@other.org", "other.org", NOW + 5, NOW + 5))
        self.assertEqual(s.take("example.com", NOW + 6).code, "new111")
        # the stale "resend" predecessor must not be served by a second GET.
        self.assertIsNone(s.take("example.com", NOW + 7))
        # Near-miss: an unrelated origin's code is untouched by that drop.
        self.assertEqual(s.take("other.org", NOW + 8).code, "other2")


class Processing(unittest.TestCase):
    def test_allowlisted_sender_stores_one_code(self):
        s = cr.CodeStore(600)
        out, log = captured(
            lambda: cr.process_message(
                mail("noreply@example.com", "Your code is 482913"), NOW, CFG, s, NOW
            )
        )
        self.assertEqual(out, "stored")
        self.assertEqual(s.pending(), 1)
        self.assertNotIn("482913", log)

    def test_non_allowlisted_sender_stores_nothing(self):
        s = cr.CodeStore(600)
        out, log = captured(
            lambda: cr.process_message(
                mail("evil@attacker.net", "Your code is 482913"), NOW, CFG, s, NOW
            )
        )
        self.assertEqual(out, "ignored")
        self.assertEqual(s.pending(), 0)
        self.assertNotIn("482913", log)

    def test_from_headers_a_client_would_read_differently_are_refused(self):
        # parseaddr() reads the first of these as noreply@example.com, which would STORE a code
        # for an attacker's mail. Every shape here must be refused outright.
        bad_headers = (
            # the angle-addr is the real sender; the bare address is a decoy
            b"From: noreply@example.com <attacker@evil.com>\r\n",
            # two addresses
            b"From: noreply@example.com, attacker@evil.com\r\n",
            # two From headers
            b"From: noreply@example.com\r\nFrom: attacker@evil.com\r\n",
            # a group with no address
            b"From: undisclosed-recipients:;\r\n",
            # display name carrying the allowlisted address, real sender elsewhere
            b'From: "noreply@example.com" <attacker@evil.com>\r\n',
            # no From at all
            b"",
        )
        for hdr in bad_headers:
            s = cr.CodeStore(600)
            raw = hdr + b"Subject: hi\r\nContent-Type: text/plain\r\n\r\nYour code is 482913\r\n"
            out, log = captured(lambda: cr.process_message(raw, NOW, CFG, s, NOW))
            self.assertEqual(out, "ignored", hdr)
            self.assertEqual(s.pending(), 0, hdr)
            self.assertNotIn("482913", log)

    def test_well_formed_from_headers_still_pass(self):
        # Near-misses: the shapes real senders use must NOT be refused by the stricter parse.
        good_headers = (
            b"From: noreply@example.com\r\n",
            b"From: <noreply@example.com>\r\n",
            b'From: "Example Service" <noreply@example.com>\r\n',
            b"From: =?utf-8?q?Service_=C3=A9?= <NoReply@Example.com>\r\n",
            b"From: Example Service <noreply@example.com>\r\n",
        )
        for hdr in good_headers:
            s = cr.CodeStore(600)
            raw = hdr + b"Subject: hi\r\nContent-Type: text/plain\r\n\r\nYour code is 482913\r\n"
            out, _ = captured(lambda: cr.process_message(raw, NOW, CFG, s, NOW))
            self.assertEqual(out, "stored", hdr)
            self.assertEqual(s.pending(), 1, hdr)

    def test_stale_mail_is_expired_on_arrival(self):
        s = cr.CodeStore(600)
        out, _ = captured(
            lambda: cr.process_message(
                mail("noreply@example.com", "code 482913"), NOW - 601, CFG, s, NOW
            )
        )
        self.assertEqual(out, "expired")
        self.assertEqual(s.pending(), 0)

    def test_html_only_mail_is_read(self):
        raw = (
            b"From: noreply@example.com\r\nSubject: hi\r\nContent-Type: text/html\r\n\r\n"
            b"<p>Your <b>code</b> is <i>482913</i></p>"
        )
        s = cr.CodeStore(600)
        out, _ = captured(lambda: cr.process_message(raw, NOW, CFG, s, NOW))
        self.assertEqual(out, "stored")


class FakeImap:
    """Just enough IMAP4_SSL for Poller.poll_once. `flags` records STORE calls."""

    messages: dict[bytes, bytes] = {}
    flags: list[bytes] = []
    fail_login = False

    def __init__(self, *a, **k):
        pass

    def login(self, user, password):
        if FakeImap.fail_login:
            raise imaplib.IMAP4.error("LOGIN failed for secret-password-text")

    def select(self, folder):
        return "OK", [b"1"]

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            return "OK", [b" ".join(FakeImap.messages)]
        if cmd == "FETCH":
            uid = args[0]
            hdr = b'1 (UID 1 INTERNALDATE "01-Jan-2030 00:00:00 +0000" BODY[] {1}'
            return "OK", [(hdr, FakeImap.messages[uid]), b")"]
        if cmd == "STORE":
            FakeImap.flags.append(args[0])
            return "OK", [b""]
        raise AssertionError(cmd)

    def logout(self):
        return "BYE", []


class Polling(unittest.TestCase):
    def setUp(self):
        self._orig = imaplib.IMAP4_SSL
        imaplib.IMAP4_SSL = FakeImap  # type: ignore[misc,assignment]
        FakeImap.messages = {}
        FakeImap.flags = []
        FakeImap.fail_login = False

    def tearDown(self):
        imaplib.IMAP4_SSL = self._orig  # type: ignore[misc]

    def test_poll_stores_and_marks_seen_only_what_it_stored(self):
        FakeImap.messages = {
            b"1": mail("noreply@example.com", "Your code is 482913"),
            b"2": mail("evil@attacker.net", "Your code is 999999"),
        }
        store = cr.CodeStore(10**9)
        p = cr.Poller(CFG, "pw", store)
        captured(lambda: p.poll_once(1_893_456_000.0))
        self.assertEqual(store.pending(), 1)
        self.assertEqual(FakeImap.flags, [b"1"])  # the ignored mail is left untouched
        self.assertEqual(p.last_error, "")
        # A second poll must not re-add the same UIDs.
        captured(lambda: p.poll_once(1_893_456_015.0))
        self.assertEqual(store.pending(), 1)

    def test_login_failure_is_recorded_without_the_server_text(self):
        FakeImap.fail_login = True
        p = cr.Poller(CFG, "pw", cr.CodeStore(600))
        _, log = captured(lambda: p.poll_once(NOW))
        self.assertEqual(p.last_error, "error")
        self.assertNotIn("secret-password-text", log)
        self.assertNotIn("pw", json.loads(log.strip().splitlines()[-1]).values())


class Http(unittest.TestCase):
    def setUp(self):
        self.store = cr.CodeStore(600)
        self.poller = cr.Poller(CFG, "pw", self.store)
        self.poller.last_ok, self.poller.last_error = NOW, ""
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), cr.make_handler(TOKEN, self.store, self.poller)
        )
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def _get(self, path, token=TOKEN):
        req = urllib.request.Request(self.base + path)
        if token is not None:
            req.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def _add(self, code="482913", received=None):
        import time

        t = time.time() if received is None else received
        self.store.add(cr.Entry(code, "noreply@example.com", "example.com", t, t))

    def test_code_then_404(self):
        self._add()
        (status, body), log = captured(lambda: self._get("/code?purpose=https://example.com/login"))
        self.assertEqual((status, body), (200, {"code": "482913"}))
        self.assertNotIn("482913", log)
        self.assertEqual(self._get("/code?purpose=example.com")[0], 404)  # one-shot

    def test_expired_code_is_404(self):
        self._add(received=1.0)  # decades ago
        self.assertEqual(self._get("/code?purpose=example.com")[0], 404)

    def test_bearer_is_required(self):
        self._add()
        self.assertEqual(self._get("/code?purpose=example.com", token="wrong")[0], 401)
        self.assertEqual(self._get("/code?purpose=example.com", token=None)[0], 401)
        # ...and the refused calls did not consume the code.
        self.assertEqual(self._get("/code?purpose=example.com")[0], 200)

    def test_purpose_is_required(self):
        self.assertEqual(self._get("/code")[0], 400)

    def test_healthz_needs_no_token_and_reports_a_broken_poll(self):
        status, body = self._get("/healthz", token=None)
        self.assertEqual((status, body["ok"]), (200, True))
        self.poller.last_error = "error"
        status, body = self._get("/healthz", token=None)
        self.assertEqual((status, body["ok"]), (503, False))
        self.assertNotIn("code", json.dumps(body))


class Config(unittest.TestCase):
    def _write(self, tmp, obj):
        p = pathlib.Path(tmp) / "c.json"
        p.write_text(json.dumps(obj))
        return str(p)

    def test_empty_allowlist_refuses_to_start(self):
        import tempfile

        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(ValueError):
                cr.load_config(self._write(t, {"sender_allowlist": []}))

    def test_pattern_without_group_refused_and_defaults_applied_otherwise(self):
        import tempfile

        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(ValueError):
                cr.load_config(
                    self._write(t, {"sender_allowlist": ["a@b.c"], "code_patterns": ["\\d{6}"]})
                )
            cfg = cr.load_config(self._write(t, {"sender_allowlist": ["a@b.c"]}))
            self.assertEqual(cfg["code_patterns"], cr.DEFAULT_CODE_PATTERNS)

    def test_extra_patterns_come_before_the_built_ins_not_instead_of_them(self):
        import tempfile

        extra = r"(?i)token\s+(\d{6})"
        with tempfile.TemporaryDirectory() as t:
            cfg = cr.load_config(
                self._write(t, {"sender_allowlist": ["a@b.c"], "code_patterns": [extra]})
            )
        # The argspec says "tried before the built-ins", never instead of them.
        self.assertEqual(cfg["code_patterns"], [extra] + cr.DEFAULT_CODE_PATTERNS)
        self.assertEqual(cr.extract_code("token 555666", cfg["code_patterns"]), "555666")
        self.assertEqual(cr.extract_code("your code is 482913", cfg["code_patterns"]), "482913")

    def test_log_event_refuses_a_code_field(self):
        with self.assertRaises(ValueError):
            cr.log_event(event="x", code="123456")


if __name__ == "__main__":
    unittest.main(verbosity=1)
