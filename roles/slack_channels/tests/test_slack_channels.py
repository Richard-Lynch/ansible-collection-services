#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""slack_channels: the plan, and a full apply + read-back against a stub workspace. Stdlib only.

    python3 roles/slack_channels/tests/test_slack_channels.py

The stub (stub_slack.py, beside this file) enforces Slack's scope, membership and rename rules, so a
case here fails if the module calls a method in the wrong order, ignores pagination, or treats an
HTTP 200 {"ok": false} as success.
"""

from __future__ import annotations

import copy
import sys
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
# The module is a collection plugin: plugins/modules under the collection root.
sys.path.insert(
    0, str(HERE.parents[2] / "plugins/modules")
)

import slack_channels as sc  # noqa: E402
import stub_slack  # noqa: E402

BOT, OWNER = "UBOT0001", "UOWNER01"
FULL = [
    "channels:read",
    "channels:manage",
    "channels:join",
    "channels:history",
    "chat:write",
    "pins:read",
    "pins:write",
]
BASE = {
    "bot": BOT,
    "scopes": FULL,
    "admin": False,
    "channels": [
        # made by the bot itself, so the bot may rename it
        {"id": "C001", "name": "lobby", "creator": BOT, "members": [BOT, OWNER]},
        # made by the owner: a bot rename is not_authorized
        {
            "id": "C002",
            "name": "lobby-config",
            "creator": OWNER,
            "members": [BOT, OWNER],
        },
        {"id": "C003", "name": "builds", "members": [OWNER]},
        {
            "id": "C004",
            "name": "backlog",
            "members": [BOT, OWNER, "UEXTRA01"],
            "purpose": "tasks",
        },
        {"id": "C005", "name": "diary", "members": [OWNER], "is_archived": True},
    ],
}


def serve(state):
    srv = stub_slack.make_server(0, copy.deepcopy(state))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def params(url, channels, apply):
    return {
        "token": stub_slack.TOKEN,
        "api_url": url,
        "channels": channels,
        "apply": apply,
    }


class Validate(unittest.TestCase):
    def test_refuses_a_hash_an_unknown_key_and_a_bad_member(self):
        problems = sc.validate(
            [
                {"name": "#alerts"},
                {"name": "x", "colour": 1},
                {"name": "y", "members": ["bob"]},
            ]
        )
        self.assertEqual(len(problems), 3, problems)
        self.assertTrue(sc.validate([{"name": "x", "id": "U123"}]))

    def test_refuses_a_name_used_twice(self):
        self.assertTrue(
            sc.validate([{"name": "general", "rename_from": "lobby"}, {"name": "lobby"}])
        )

    def test_accepts_the_inventory_shape(self):
        self.assertEqual(
            sc.validate(
                [
                    {
                        "name": "general",
                        "rename_from": "lobby",
                        "purpose": "p",
                        "members": [OWNER],
                    }
                ]
            ),
            [],
        )


class Plan(unittest.TestCase):
    live = [
        {"id": "C1", "name": "lobby", "is_member": False},
        {"id": "C2", "name": "ops2", "is_member": True},
        {"id": "C3", "name": "old", "is_member": True},
        {"id": "C9", "name": "gone", "is_archived": True},
    ]

    def ops(self, declared, members=None):
        actions, refusals, dummy = sc.plan(declared, self.live, members or {}, BOT)
        return [(a["op"], a["channel"]) for a in actions], refusals

    def test_rename_joins_the_old_channel_first(self):
        ops, dummy = self.ops([{"name": "general", "rename_from": "lobby"}], {"C1": [OWNER]})
        self.assertEqual(ops, [("join", "lobby"), ("rename", "general")])

    def test_both_names_existing_is_refused_not_guessed(self):
        ops, refusals = self.ops([{"name": "ops2", "rename_from": "old"}])
        self.assertEqual(ops, [])
        self.assertIn("BOTH exist", refusals[0])

    def test_archived_target_is_refused(self):
        dummy, refusals = self.ops([{"name": "gone"}])
        self.assertIn("ARCHIVED", refusals[0])

    def test_create_then_invite_never_invites_the_bot(self):
        ops, dummy = self.ops([{"name": "new", "members": [OWNER, BOT]}])
        self.assertEqual(ops, [("create", "new"), ("invite", "new")])

    def test_a_declared_id_must_be_the_channel_the_name_resolves_to(self):
        ops, refusals = self.ops([{"name": "ops2", "id": "C2"}], {"C2": [BOT]})
        self.assertEqual((ops, refusals), ([], []))
        dummy, refusals = self.ops([{"name": "ops2", "id": "C9X"}], {"C2": [BOT]})
        self.assertIn("declared as C9X but resolves to C2", refusals[0])

    def test_a_declared_id_is_never_created_again(self):
        ops, refusals = self.ops([{"name": "brand-new", "id": "C77"}])
        self.assertEqual(ops, [])
        self.assertIn("would be CREATED", refusals[0])

    def test_a_rename_keeps_the_declared_id(self):
        ops, refusals = self.ops(
            [{"name": "general", "rename_from": "lobby", "id": "C1"}], {"C1": [BOT]}
        )
        self.assertEqual((ops, refusals), ([("join", "lobby"), ("rename", "general")], []))

    def test_converged_channel_plans_nothing(self):
        ops, refusals = self.ops([{"name": "ops2", "members": [OWNER]}], {"C2": [BOT, OWNER]})
        self.assertEqual((ops, refusals), ([], []))

    def test_omitted_purpose_is_left_alone_declared_one_is_set(self):
        live = [
            {
                "id": "C2",
                "name": "x",
                "is_member": True,
                "purpose": {"value": "owner's"},
            }
        ]
        a, dummy, dummy = sc.plan([{"name": "x"}], live, {"C2": [BOT]}, BOT)
        self.assertEqual(a, [])
        a, dummy, dummy = sc.plan([{"name": "x", "purpose": "new"}], live, {"C2": [BOT]}, BOT)
        self.assertEqual([(x["op"], x["was"]) for x in a], [("purpose", "owner's")])


class Extras(unittest.TestCase):
    row = [{"name": "x", "id": "C1", "via": "exists", "is_member": True}]

    def ex(self, guides=(), bookmarks=()):
        return {"C1": {"guides": list(guides), "bookmarks": list(bookmarks)}}

    def ops(self, ch, extras):
        acts, dummy = sc.plan_extras(
            [dict({"name": "x"}, **ch)], copy.deepcopy(self.row), extras, BOT
        )
        return [a["op"] for a in acts]

    def test_first_run_posts(self):
        self.assertEqual(self.ops({"guide": "g"}, self.ex()), ["guide_post"])

    def test_unchanged_guide_does_nothing_even_though_slack_escaped_it(self):
        text = (
            sc.guide_text("x", "a & b <c>")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        self.assertEqual(
            self.ops({"guide": "a & b <c>"}, self.ex([{"ts": "1", "text": text, "pinned": True}])),
            [],
        )

    def test_drift_is_an_in_place_update_never_a_repost(self):
        live = [{"ts": "1", "text": sc.guide_text("x", "old"), "pinned": True}]
        self.assertEqual(self.ops({"guide": "new"}, self.ex(live)), ["guide_update"])

    def test_slacks_auto_links_are_not_drift(self):
        body = ":pushpin: *How to use <#C1>*\n\nSee <https://a.example> or #x, <https://b.example|here>."
        live = [{"ts": "1", "text": "", "body": body, "pinned": True}]
        acts, dummy = sc.plan_extras(
            [{"name": "x", "guide": "See https://a.example or #x, <https://b.example|here>."}],
            copy.deepcopy(self.row),
            self.ex(live),
            BOT,
            {"C1": "x"},
        )
        self.assertEqual(acts, [])

    def test_an_unpinned_guide_is_pinned_not_reposted(self):
        live = [{"ts": "1", "text": sc.guide_text("x", "g"), "pinned": False}]
        self.assertEqual(self.ops({"guide": "g"}, self.ex(live)), ["guide_pin"])

    def test_a_duplicate_is_removed_and_the_newest_kept(self):
        live = [
            {"ts": "2", "text": sc.guide_text("x", "g"), "pinned": True},
            {"ts": "1", "text": sc.guide_text("x", "g"), "pinned": True},
        ]
        acts, dummy = sc.plan_extras(
            [{"name": "x", "guide": "g"}], copy.deepcopy(self.row), self.ex(live), BOT
        )
        self.assertEqual([(a["op"], a["ts"]) for a in acts], [("guide_remove", "1")])

    def test_a_dropped_guide_is_unpinned_and_deleted(self):
        live = [{"ts": "1", "text": sc.guide_text("x", "g"), "pinned": True}]
        self.assertEqual(self.ops({}, self.ex(live)), ["guide_remove"])

    def test_bookmarks_added_repointed_and_never_removed(self):
        live = [
            {"id": "B1", "title": "Console", "link": "https://old"},
            {"id": "B2", "title": "Mine", "link": "https://m"},
        ]
        rows = copy.deepcopy(self.row)
        want = [{"title": "Console", "link": "https://new"}, {"title": "Plan", "link": "https://p"}]
        acts, dummy = sc.plan_extras(
            [{"name": "x", "bookmarks": want}], rows, self.ex(bookmarks=live), BOT
        )
        self.assertEqual([a["op"] for a in acts], ["bookmark_edit", "bookmark_add"])
        self.assertEqual(rows[0]["undeclared_bookmarks"], ["Mine"])

    def test_validation_of_guides_and_bookmarks(self):
        self.assertTrue(sc.validate([{"name": "x", "guide": ""}]))
        self.assertTrue(
            sc.validate([{"name": "x", "bookmarks": [{"title": "a", "link": "http://insecure"}]}])
        )
        self.assertTrue(
            sc.validate(
                [
                    {
                        "name": "x",
                        "bookmarks": [
                            {"title": "a", "link": "https://a"},
                            {"title": "a", "link": "https://b"},
                        ],
                    }
                ]
            )
        )
        self.assertEqual(
            sc.validate(
                [
                    {
                        "name": "x",
                        "guide": "g",
                        "prompt": "p",
                        "bookmarks": [{"title": "a", "link": "https://a"}],
                    }
                ]
            ),
            [],
        )


class AgainstStub(unittest.TestCase):
    declared = [
        {"name": "general", "rename_from": "lobby", "purpose": "main", "members": [OWNER]},
        {"name": "alerts", "purpose": "alerts", "topic": "t", "members": [OWNER]},
        {"name": "builds", "members": [OWNER]},
        {"name": "backlog", "members": [OWNER]},
    ]

    def test_report_only_changes_nothing_and_lists_scopes(self):
        srv, url = serve(BASE)
        try:
            out = sc.run(params(url, self.declared, False), False)
            self.assertFalse(out["changed"])
            self.assertEqual(out["scopes"], sorted(FULL))
            self.assertNotIn("results", out)
            self.assertEqual(
                [c["name"] for c in stub_state(url)],
                ["lobby", "lobby-config", "builds", "backlog", "diary"],
            )
        finally:
            srv.shutdown()
            srv.server_close()

    def test_apply_converges_and_a_second_run_is_a_no_op(self):
        srv, url = serve(BASE)
        try:
            out = sc.run(params(url, self.declared, True), False)
            self.assertTrue(out["changed"])
            self.assertEqual([r for r in out["results"] if not r["ok"]], [])
            self.assertEqual(out["remaining"], [])
            names = {c["name"]: c for c in stub_state(url)}
            self.assertEqual(names["general"]["id"], "C001")  # renamed, same id
            self.assertEqual(names["general"]["purpose"], "main")
            self.assertIn(OWNER, names["alerts"]["members"])
            self.assertEqual(names["alerts"]["topic"], "t")
            self.assertIn(BOT, names["builds"]["members"])  # joined
            self.assertIn("UEXTRA01", names["backlog"]["members"])  # never removed
            again = sc.run(params(url, self.declared, True), False)
            self.assertEqual((again["changed"], again["plan"]), (False, []))
        finally:
            srv.shutdown()
            srv.server_close()

    def test_rename_of_a_human_made_channel_fails_with_slacks_error(self):
        srv, url = serve(BASE)
        try:
            out = sc.run(
                params(url, [{"name": "general-config", "rename_from": "lobby-config"}], True),
                False,
            )
            failed = [r for r in out["results"] if not r["ok"]]
            self.assertEqual(failed[0]["slack_error"], "not_authorized")
            self.assertEqual(out["remaining"][0]["op"], "rename")
        finally:
            srv.shutdown()
            srv.server_close()

    def test_admin_user_token_renames_what_the_bot_may_not(self):
        state = dict(
            BASE, user={"id": OWNER, "scopes": ["channels:read", "channels:write"], "admin": True}
        )
        srv, url = serve(state)
        try:
            p = params(url, [{"name": "general-config", "rename_from": "lobby-config"}], True)
            report = sc.run(dict(p, apply=False), False)
            self.assertTrue(report["rename_needs_admin_token"])
            p["admin_token"] = stub_slack.ADMIN_TOKEN
            out = sc.run(p, False)
            self.assertEqual((out["admin_user_id"], out["admin_missing_scopes"]), (OWNER, []))
            self.assertEqual([r for r in out["results"] if not r["ok"]], [])
            self.assertEqual(out["remaining"], [])
            self.assertEqual(
                {c["id"]: c["name"] for c in stub_state(url)}["C002"], "general-config"
            )
        finally:
            srv.shutdown()
            srv.server_close()

    def test_a_refused_admin_token_stops_before_anything(self):
        srv, url = serve(BASE)  # no user identity: the admin token is invalid_auth
        try:
            p = dict(params(url, [{"name": "alerts"}], True), admin_token=stub_slack.ADMIN_TOKEN)
            out = sc.run(p, False)
            self.assertTrue(out["failed"])
            self.assertIn("admin token was refused: auth.test: invalid_auth", out["msg"])
            self.assertNotIn("alerts", [c["name"] for c in stub_state(url)])
        finally:
            srv.shutdown()
            srv.server_close()

    def test_guides_and_bookmarks_converge_update_in_place_and_retire(self):
        user = {
            "id": OWNER,
            "scopes": ["channels:read", "channels:write", "bookmarks:read", "bookmarks:write"],
            "admin": True,
        }
        srv, url = serve(dict(BASE, user=user))
        try:
            chans = [
                {
                    "name": "general",
                    "rename_from": "lobby",
                    "guide": "Just talk & follow up in #alerts or at https://x.example.",
                    "bookmarks": [{"title": "Console", "link": "https://console.example"}],
                },
                # general-config's marker must never be taken for general's (a prefix of it)
                {"name": "general-config", "rename_from": "lobby-config", "guide": "Settings."},
                {"name": "alerts", "guide": "Feed only."},
            ]
            p = dict(params(url, chans, True), admin_token=stub_slack.ADMIN_TOKEN)
            out = sc.run(p, False)
            self.assertEqual([r for r in out["results"] if not r["ok"]], [])
            self.assertEqual(out["remaining"], [])
            self.assertEqual(out["bookmarks_token"], "admin")
            ws = {c["name"]: c for c in stub_state(url)}
            for name in ("general", "general-config", "alerts"):
                self.assertEqual(len(ws[name]["messages"]), 1, name)
                self.assertEqual(ws[name]["pins"], [ws[name]["messages"][0]["ts"]], name)
            self.assertEqual(ws["general"]["bookmarks"][0]["link"], "https://console.example")
            first_ts = ws["general"]["messages"][0]["ts"]

            again = sc.run(p, False)
            self.assertEqual((again["changed"], again["plan"]), (False, []))

            chans[2]["topic"] = '"fix #1 in <repo>" & more'
            topical = sc.run(dict(p, channels=chans), False)
            self.assertEqual([r["op"] for r in topical["results"]], ["topic"])
            self.assertEqual(
                sc.run(dict(p, channels=chans), False)["plan"], []
            )  # escaped, not drift

            chans[0]["guide"] = "Just talk. Edited."
            edited = sc.run(dict(p, channels=chans), False)
            self.assertEqual([r["op"] for r in edited["results"]], ["guide_update"])
            ws = {c["name"]: c for c in stub_state(url)}
            self.assertEqual([m["ts"] for m in ws["general"]["messages"]], [first_ts])
            self.assertIn("Edited.", ws["general"]["messages"][0]["text"])

            del chans[2]["guide"]
            retired = sc.run(dict(p, channels=chans), False)
            self.assertEqual([r["op"] for r in retired["results"]], ["guide_remove"])
            ws = {c["name"]: c for c in stub_state(url)}
            self.assertEqual((ws["alerts"]["messages"], ws["alerts"]["pins"]), ([], []))
        finally:
            srv.shutdown()
            srv.server_close()

    def test_a_marker_is_matched_whole_never_as_a_prefix(self):
        state = copy.deepcopy(BASE)
        # in #backlog, a bot message carrying ANOTHER channel's marker that starts with "backlog"
        state["channels"][3]["messages"] = [
            {"ts": "5.1", "user": BOT, "text": "x\n\nguide:backlog-old"}
        ]
        srv, url = serve(state)
        try:
            out = sc.run(params(url, [{"name": "backlog", "guide": "g"}], False), False)
            self.assertEqual([a["op"] for a in out["plan"]], ["guide_post"])
        finally:
            srv.shutdown()
            srv.server_close()

    def test_the_marker_prefix_is_the_callers(self):
        state = copy.deepcopy(BASE)
        # a guide carrying a DIFFERENT prefix is not this caller's guide, so it is posted anew;
        # with the matching prefix the same message is found and left alone
        text = ":pushpin: *How to use #backlog*\n\ng\n\npinned-notes:backlog"
        state["channels"][3]["messages"] = [{"ts": "5.1", "user": BOT, "text": text}]
        state["channels"][3]["pins"] = ["5.1"]
        srv, url = serve(state)
        try:
            other = sc.run(params(url, [{"name": "backlog", "guide": "g"}], False), False)
            self.assertEqual([a["op"] for a in other["plan"]], ["guide_post"])
            p = dict(params(url, [{"name": "backlog", "guide": "g"}], False))
            p["guide_marker_prefix"] = "pinned-notes:"
            mine = sc.run(p, False)
            self.assertEqual(mine["plan"], [])
            p["guide_marker_prefix"] = "has space:"
            self.assertTrue(sc.run(p, False)["failed"])
        finally:
            sc._marker_prefix = sc.DEFAULT_MARKER_PREFIX  # every other test uses the default
            srv.shutdown()
            srv.server_close()

    def test_bookmarks_without_a_token_that_can_write_them_are_refused(self):
        srv, url = serve(BASE)
        try:
            out = sc.run(
                params(
                    url,
                    [{"name": "backlog", "bookmarks": [{"title": "a", "link": "https://a"}]}],
                    True,
                ),
                False,
            )
            self.assertTrue(out["failed"])
            self.assertIn("bookmarks:write", out["msg"])
        finally:
            srv.shutdown()
            srv.server_close()

    def test_missing_scope_is_reported_and_the_call_names_it(self):
        state = dict(BASE, scopes=["channels:read"])
        srv, url = serve(state)
        try:
            out = sc.run(params(url, [{"name": "alerts", "members": [OWNER]}], True), False)
            self.assertEqual(
                out["missing_scopes"], ["channels:history", "channels:manage", "pins:read"]
            )
            self.assertEqual(out["results"][0]["needed"], "channels:manage")
            # the create failed, so the invite for the same channel is skipped, not attempted
            self.assertIn("skipped", out["results"][1]["error"])
        finally:
            srv.shutdown()
            srv.server_close()

    def test_refusal_blocks_the_whole_apply(self):
        srv, url = serve(BASE)
        try:
            out = sc.run(params(url, [{"name": "diary"}, {"name": "alerts"}], True), False)
            self.assertTrue(out["failed"])
            self.assertNotIn("alerts", [c["name"] for c in stub_state(url)])
        finally:
            srv.shutdown()
            srv.server_close()

    def test_unreachable_api_is_a_reason_not_a_traceback(self):
        import socket

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        out = sc.run(params(f"http://127.0.0.1:{port}", self.declared, False), False)
        self.assertTrue(out["failed"])
        self.assertIn("unreachable_or_unparseable", out["msg"])

    def test_check_mode_never_acts(self):
        srv, url = serve(BASE)
        try:
            out = sc.run(params(url, self.declared, True), True)
            self.assertFalse(out["changed"])
            self.assertNotIn("general", [c["name"] for c in stub_state(url)])
        finally:
            srv.shutdown()
            srv.server_close()


def stub_state(url):
    import json
    import urllib.request

    with urllib.request.urlopen(f"{url}/_state") as r:
        return json.loads(r.read())["channels"]


if __name__ == "__main__":
    unittest.main(verbosity=1)
