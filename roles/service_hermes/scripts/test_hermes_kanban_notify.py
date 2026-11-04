#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for hermes_kanban_notify.py (Kanban -> Slack card threads). Stdlib only, offline.

A real SQLite board with upstream's column names (tasks / task_events / task_comments, read at
v0.22.5) and a fake sender that records every post. The properties that matter are negative:
a restart must NOT repost a root or a reply, a failed post must NOT advance past its event, a
title must NOT be able to forge a mention or attach a file, and `notify: off` must silence a card.

Run: python3 roles/service_hermes/scripts/test_hermes_kanban_notify.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import hermes_kanban_notify as kn  # noqa: E402

SCHEMA = """
CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT, assignee TEXT,
                    status TEXT NOT NULL, created_at INTEGER NOT NULL DEFAULT 0,
                    completion_contract TEXT);
CREATE TABLE task_comments (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                            author TEXT NOT NULL, body TEXT NOT NULL, created_at INTEGER NOT NULL DEFAULT 0);
CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, run_id INTEGER,
                          kind TEXT NOT NULL, payload TEXT, created_at INTEGER NOT NULL DEFAULT 0);
"""

CFG = {
    "assignees": ["coder"],
    "events": list(kn.EVENTS),
    "ping": ["blocked", "needs_input"],
    "owner_user_id": "U0OWNER",
    "default_channel": "C0CODING",
    "channels": {"coder": "C0CODING"},
    "console_url_template": "https://console.example/kanban?card={card_id}",
    "attach_url_template": "https://term.example/?arg={card_id}",
}


class FakeSlack:
    def __init__(self, fail_on: int | None = None):
        self.posts: list[tuple[str, str | None, str]] = []
        self.fail_on = fail_on  # 1-based index of the post that fails

    def __call__(self, channel, thread_ts, text):
        if self.fail_on is not None and len(self.posts) + 1 == self.fail_on:
            self.fail_on = None
            raise kn.SendError("simulated slack outage")
        self.posts.append((channel, thread_ts, text))
        return f"1700000000.{len(self.posts):06d}"


class Board(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.db = d / "kanban.db"
        self.state = d / "state" / "state.json"
        w = sqlite3.connect(self.db)
        w.executescript(SCHEMA)
        w.commit()
        self.w = w

    def tearDown(self):
        self.w.close()
        self.tmp.cleanup()

    # -- board writes, the way upstream records them --
    def card(self, tid, title="Fix the thing", assignee="coder", body="", contract=None):
        self.w.execute(
            "INSERT INTO tasks (id, title, body, assignee, status, completion_contract)"
            " VALUES (?,?,?,?, 'ready', ?)",
            (tid, title, body, assignee, contract),
        )
        self.event(tid, "created", {"assignee": assignee, "status": "ready"})

    def event(self, tid, kind, payload=None):
        self.w.execute(
            "INSERT INTO task_events (task_id, kind, payload) VALUES (?,?,?)",
            (tid, kind, json.dumps(payload) if payload is not None else None),
        )
        self.w.commit()

    def comment(self, tid, body, author="owner"):
        self.w.execute(
            "INSERT INTO task_comments (task_id, author, body) VALUES (?,?,?)",
            (tid, author, body),
        )
        self.event(tid, "commented", {"author": author, "len": len(body)})

    def tick(self, slack, cfg=CFG):
        conn = kn.open_board(str(self.db))
        try:
            return kn.tick(conn, cfg, self.state, slack)
        finally:
            conn.close()

    def prime(self, slack):
        """First run: records the board's newest event as the cursor and posts nothing."""
        st = self.tick(slack)
        self.assertEqual(slack.posts, [])
        return st


class Lifecycle(Board):
    def test_one_thread_per_card_root_then_replies(self):
        s = FakeSlack()
        self.prime(s)
        self.card("t_aaaa0001", body="repo: example-org/example-repo\nDo X")
        self.event("t_aaaa0001", "claimed", {"lock": "x"})
        self.event("t_aaaa0001", "heartbeat", {})
        self.event("t_aaaa0001", "blocked", {"reason": "tests red", "kind": "transient"})
        self.comment("t_aaaa0001", "PR: https://github.com/example-org/example-repo/pull/12 opened")
        self.event("t_aaaa0001", "review_requested", {})
        self.event("t_aaaa0001", "completed", {"summary": "ok"})
        self.tick(s)
        root = s.posts[0]
        self.assertEqual((root[0], root[1]), ("C0CODING", None))
        for want in (
            "t_aaaa0001",
            "Fix the thing",
            "example-org/example-repo",
            "coder",
            "https://console.example/kanban?card=t_aaaa0001",
            "https://term.example/?arg=t_aaaa0001",
        ):
            self.assertIn(want, root[2])
        replies = [p[2] for p in s.posts[1:]]
        root_ts = "1700000000.000001"
        self.assertTrue(all(p[1] == root_ts for p in s.posts[1:]), s.posts)
        self.assertEqual(
            replies,
            [
                "created (ready)",
                "started",
                "<@U0OWNER> blocked: tests red",
                "PR opened: https://github.com/example-org/example-repo/pull/12",
                "review requested",
                "done",
            ],
        )

    def test_restart_never_reposts(self):
        s = FakeSlack()
        self.prime(s)
        self.card("t_aaaa0002")
        self.event("t_aaaa0002", "claimed")
        self.tick(s)
        n = len(s.posts)
        self.tick(s)  # same state file, nothing new
        self.assertEqual(len(s.posts), n)
        self.event("t_aaaa0002", "completed")
        self.tick(s)
        self.assertEqual(len([p for p in s.posts if p[1] is None]), 1, "a second root was posted")
        self.assertEqual(s.posts[-1][2], "done")

    def test_first_run_does_not_replay_history(self):
        self.card("t_old00001")
        self.event("t_old00001", "completed")
        s = FakeSlack()
        self.prime(s)
        self.tick(s)
        self.assertEqual(s.posts, [])

    def test_failed_post_does_not_advance_and_retries(self):
        s = FakeSlack()
        self.prime(s)
        self.card("t_aaaa0003")
        self.event("t_aaaa0003", "claimed")
        s.fail_on = 3  # root ok, "created" ok, "started" fails
        with self.assertRaises(kn.SendError):
            self.tick(s)
        self.assertEqual(len(s.posts), 2)
        self.tick(s)
        self.assertEqual([p[2] for p in s.posts[1:]], ["created (ready)", "started"])
        self.assertEqual(len([p for p in s.posts if p[1] is None]), 1)

    def test_failed_root_is_retried_not_skipped(self):
        s = FakeSlack(fail_on=1)
        self.prime(s)
        self.card("t_aaaa0004")
        with self.assertRaises(kn.SendError):
            self.tick(s)
        self.tick(s)
        self.assertIsNone(s.posts[0][1])
        self.assertEqual(s.posts[1][2], "created (ready)")

    def test_terminate_and_needs_input(self):
        s = FakeSlack()
        self.prime(s)
        self.card("t_aaaa0005")
        self.event(
            "t_aaaa0005",
            "blocked",
            {"reason": "approve the migration?", "kind": "needs_input"},
        )
        self.event("t_aaaa0005", "reclaimed", {"manual": True, "reason": "owner stopped it"})
        self.event(
            "t_aaaa0005", "reclaimed", {"manual": False}
        )  # a stale reclaim is not a terminate
        self.tick(s)
        self.assertEqual(
            [p[2] for p in s.posts[1:]],
            [
                "created (ready)",
                "<@U0OWNER> needs your input: approve the migration?",
                "terminated: owner stopped it",
            ],
        )


class Scope(Board):
    def test_out_of_scope_card_is_silent(self):
        s = FakeSlack()
        self.prime(s)
        self.card("t_bbbb0001", assignee="default")
        self.event("t_bbbb0001", "completed")
        self.tick(s)
        self.assertEqual(s.posts, [])

    def test_notify_on_opts_a_card_in(self):
        s = FakeSlack()
        self.prime(s)
        self.card("t_bbbb0002", assignee="default", body="notify: slack #coding")
        self.tick(s)
        self.assertEqual(len(s.posts), 2)
        self.assertEqual(s.posts[0][0], "C0CODING")

    def test_notify_off_comment_silences(self):
        s = FakeSlack()
        self.prime(s)
        self.card("t_bbbb0003")
        self.tick(s)
        n = len(s.posts)
        self.comment("t_bbbb0003", "notify: off")
        self.event("t_bbbb0003", "claimed")
        self.event("t_bbbb0003", "completed")
        self.tick(s)
        self.assertEqual(len(s.posts), n)

    def test_last_directive_wins(self):
        s = FakeSlack()
        self.prime(s)
        self.card("t_bbbb0004", body="notify: off")
        self.comment("t_bbbb0004", "notify: on")
        self.event("t_bbbb0004", "completed")
        self.tick(s)
        self.assertEqual(s.posts[-1][2], "done")

    def test_event_list_and_ping_me(self):
        s = FakeSlack()
        self.prime(s)
        self.card("t_bbbb0005", body="notify: review, done\nping me on review")
        self.event("t_bbbb0005", "claimed")
        self.event("t_bbbb0005", "blocked", {"reason": "x"})
        self.event("t_bbbb0005", "review_requested")
        self.event("t_bbbb0005", "completed")
        self.tick(s)
        self.assertEqual([p[2] for p in s.posts[1:]], ["<@U0OWNER> review requested", "done"])

    def test_typo_does_not_silence(self):
        d = kn.directives(["notify: reveiw"])
        self.assertIsNone(d["notify"])
        self.assertEqual(kn.directives(["ping: off"])["ping"], [])


class Safety(Board):
    def test_title_cannot_forge_mentions_links_or_media(self):
        s = FakeSlack()
        self.prime(s)
        self.card(
            "t_cccc0001",
            title="<!channel> pwn <https://evil|x> MEDIA:/opt/data/.env [[as_document]]",
        )
        self.event("t_cccc0001", "blocked", {"reason": "<@U999> MEDIA:/etc/shadow\nline2"})
        self.tick(s)
        for _, _, text in s.posts:
            body = text.replace("<@U0OWNER>", "")
            self.assertNotIn("<", body)
            self.assertNotIn(">", body)
            self.assertNotRegex(body, r"(?i)media:")
            self.assertNotIn("[[", body)
        self.assertNotIn("\n", s.posts[-1][2])

    def test_repo_from_completion_contract_first(self):
        s = FakeSlack()
        self.prime(s)
        # The contract outranks a `repo:` line and a GitHub URL in the (untrusted) body.
        self.card(
            "t_eeee0001",
            body="repo: someone/else\nhttps://github.com/x/y",
            contract="example-org/example-repo",
        )
        self.tick(s)
        self.assertIn("repo: example-org/example-repo |", s.posts[0][2])

    def test_repo_precedence_without_an_owner_repo_contract(self):
        pr = "https://github.com/o/from-pr/pull/9"
        self.assertEqual(kn.repo_of({"completion_contract": pr}, []), "o/from-pr")
        self.assertEqual(
            kn.repo_of({"completion_contract": "local-only", "body": "repo: o/line"}, [pr]),
            "o/line",
        )
        self.assertEqual(
            kn.repo_of({"completion_contract": "local-only", "body": ""}, [pr]), "o/from-pr"
        )
        self.assertEqual(kn.repo_of({"completion_contract": "not a/valid/contract"}, []), "unknown")
        self.assertEqual(kn.repo_of({}, []), "unknown")

    def test_body_is_never_posted(self):
        s = FakeSlack()
        self.prime(s)
        self.card("t_cccc0002", body="SECRET-BODY-TEXT repo: not a line")
        self.tick(s)
        self.assertTrue(all("SECRET-BODY-TEXT" not in p[2] for p in s.posts))
        self.assertIn("repo: unknown", s.posts[0][2])

    def test_pr_url_posted_once(self):
        s = FakeSlack()
        self.prime(s)
        url = "https://github.com/o/r/pull/3"
        self.card("t_cccc0003")
        self.comment("t_cccc0003", url)
        self.comment("t_cccc0003", "again " + url)
        self.event("t_cccc0003", "pr_acceptance", {"pr_url": url, "ok": True})
        self.tick(s)
        self.assertEqual(sum(1 for p in s.posts if url in p[2]), 1)

    def test_corrupt_state_raises_instead_of_reposting(self):
        self.state.parent.mkdir(parents=True)
        self.state.write_text("{not json")
        with self.assertRaises(ValueError):
            self.tick(FakeSlack())


class Main(Board):
    def test_send_failure_exits_nonzero_and_writes_gauge(self):
        cfg = dict(CFG, board_db=str(self.db), container="hermes", uid=10000)
        cfg_path = Path(self.tmp.name) / "config.json"
        cfg_path.write_text(json.dumps(cfg))
        metrics = Path(self.tmp.name) / "kn.prom"
        kn.write_state(self.state, {"cursor": 0, "cards": {}})
        self.card("t_dddd0001")
        orig = kn.docker_sender
        kn.docker_sender = lambda c, u: FakeSlack(fail_on=1)
        try:
            rc = kn.main(
                [
                    "--config",
                    str(cfg_path),
                    "--state",
                    str(self.state),
                    "--metrics",
                    str(metrics),
                ]
            )
        finally:
            kn.docker_sender = orig
        self.assertEqual(rc, 1)
        self.assertIn("hermes_kanban_notify_ok 0", metrics.read_text())


if __name__ == "__main__":
    unittest.main(verbosity=1)
