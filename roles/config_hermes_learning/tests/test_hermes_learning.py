#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for roles/config_hermes_learning/files/hermes_learning.py (stdlib only, offline).

The host script and the inner scripts run for real: `exec_mode: local` runs each inner script with
this interpreter against a fixture HERMES_HOME instead of `docker exec`, and the probe runner talks
to an in-process fake of Hermes's /v1/runs. Each guard has a case that must FAIL and a near-miss that
must PASS.

    python3 roles/config_hermes_learning/tests/test_hermes_learning.py
"""

from __future__ import annotations

import http.server
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

FILES = Path(__file__).resolve().parent.parent / "files"
sys.path.insert(0, str(FILES))
import hermes_learning as hl  # noqa: E402

DELIM = "\n§\n"


def make_cfg(root: Path, **over) -> dict:
    home = root / "home"
    cfg = {
        "exec_mode": "local",
        "local_python": sys.executable,
        "home": str(home),
        "host_home": str(home),
        "textfile_dir": str(root / "textfile"),
        "state_dir": str(root / "state"),
        "probes_file": str(root / "probes.json"),
        "learning_dir": "learning",
        "skip_names_file": "",
        "api_url": "http://127.0.0.1:1",
        "api_key_env": "HL_TEST_API_KEY",
        "probe_model": "example-cheap",
        "probe_timeout_s": 10,
        "claims_window_s": 0,
        "recall_scratch_home": str(root / "scratch"),
        "consolidate": {
            "gateway_url": "http://127.0.0.1:1/v1",
            "key_env": "HL_TEST_GW",
            "model": "example-aux",
            "max_loss": 0.25,
            "min_entries": 4,
        },
        "ping": {
            "age_s": 86400,
            "repeat_s": 86400,
            "token_env": "HL_TEST_SLACK",
            "channel_env": "HL_TEST_CHAN",
        },
    }
    cfg.update(over)
    (root / "cfg.json").write_text(json.dumps(cfg))
    return cfg


def stage(home: Path, sub: str, pid: str, created_at: float, payload: dict, summary="x") -> None:
    d = home / "pending" / sub
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{pid}.json").write_text(
        json.dumps(
            {
                "id": pid,
                "subsystem": sub,
                "action": payload.get("action"),
                "summary": summary,
                "origin": "foreground",
                "created_at": created_at,
                "payload": payload,
            }
        )
    )


def prom_value(path: Path, name: str, labels: str = "") -> float | None:
    want = f"{name}{{{labels}}}" if labels else name
    for line in path.read_text().splitlines():
        if line.startswith(want + " "):
            return float(line.split()[-1])
    return None


class PureFunctions(unittest.TestCase):
    def test_pending_ages_oldest(self):
        now = 1_000_000.0
        got = hl.pending_ages(
            [
                {"subsystem": "memory", "created_at": now - 10},
                {"subsystem": "memory", "created_at": now - 90000},
                {"subsystem": "skills", "created_at": now - 5},
            ],
            now,
        )
        self.assertEqual(got["memory"], {"count": 2, "oldest_age_s": 90000})
        self.assertEqual(got["skills"]["count"], 1)

    def test_due_pings_fail_and_near_miss(self):
        now = 1_000_000.0
        old = {"id": "a", "created_at": now - 86400}
        young = {
            "id": "b",
            "created_at": now - 86399,
        }  # near miss: one second short of 24 h
        due = hl.due_pings([old, young], {}, now=now, age_s=86400, repeat_s=86400)
        self.assertEqual([r["id"] for r in due], ["a"])
        # pinged an hour ago: not again until the repeat window passes
        self.assertEqual(
            hl.due_pings([old], {"a": now - 3600}, now=now, age_s=86400, repeat_s=86400),
            [],
        )
        self.assertEqual(
            len(hl.due_pings([old], {"a": now - 86400}, now=now, age_s=86400, repeat_s=86400)),
            1,
        )

    def test_loss_cap_refuses_over_25_percent(self):
        old = [f"e{i}" for i in range(8)]
        # 3 of 8 uncited = 37.5 %: refused
        bad = [
            {"content": "a", "sources": [0, 1, 2]},
            {"content": "b", "sources": [3, 4]},
        ]
        ok, frac, dropped = hl.loss_check(old, bad, 0.25)
        self.assertFalse(ok)
        self.assertEqual(dropped, [5, 6, 7])
        # near miss: exactly 2 of 8 = 25 % is at the cap, allowed
        near = [
            {"content": "a", "sources": [0, 1, 2, 3]},
            {"content": "b", "sources": [4, 5]},
        ]
        ok, frac, _ = hl.loss_check(old, near, 0.25)
        self.assertTrue(ok)
        self.assertAlmostEqual(frac, 0.25)

    def test_loss_cap_refuses_invented_index_and_empty(self):
        self.assertFalse(
            hl.loss_check(["a", "b"], [{"content": "x", "sources": [0, 1, 2]}], 0.25)[0]
        )
        self.assertFalse(hl.loss_check(["a"], [{"content": "x", "sources": ["0"]}], 0.25)[0])
        self.assertFalse(hl.loss_check([], [], 0.25)[0])

    def test_payload_pins_every_removed_entry(self):
        p = hl.consolidation_payload(["a", "b"], [{"content": " ab ", "sources": [0, 1]}])
        self.assertEqual(p["action"], "batch")
        removes = [o for o in p["operations"] if o["action"] == "remove"]
        self.assertEqual([o["matched_entry"] for o in removes], ["a", "b"])
        self.assertEqual(p["operations"][-1], {"action": "add", "content": "ab"})

    def test_check_probe(self):
        probe = {
            "expect": {
                "contains": ["391"],
                "not_contains": ["I cannot"],
                "regex": [r"\b391\b"],
                "tool": [],
            }
        }
        self.assertEqual(hl.check_probe(probe, "The answer is 391.", []), (True, "ok"))
        self.assertFalse(hl.check_probe(probe, "The answer is 3910.", [])[0])  # boundary near miss
        self.assertFalse(hl.check_probe(probe, "391, but I cannot be sure", [])[0])
        self.assertEqual(
            hl.check_probe({"expect": {"tool": ["skill_view"]}}, "x", ["web"])[1],
            "expected-tool-not-called",
        )

    def test_recall_at_k(self):
        res = {"q1": ["N1", "N2"], "q2": ["N9", "N8", "N7", "N6", "N5", "N2"], "q3": []}
        # q2's hit is 6th, so it does not count
        self.assertEqual(hl.recall_at_k(res, {"q1": "N2", "q2": "N2", "q3": "N3"}), 1)

    def test_resolve_vanished_infers(self):
        prev = {
            "m1": {"subsystem": "memory", "add_hashes": [hl.entry_hash("fact")]},
            "m2": {"subsystem": "memory", "add_hashes": [hl.entry_hash("other")]},
            "s1": {"subsystem": "skills", "skill": "deploy", "created_at": 100.0},
            "keep": {"subsystem": "memory", "add_hashes": []},
        }
        events = [{"skill": "deploy", "ts": "1970-01-01T00:02:00+00:00"}]
        got = {
            r["id"]: r["outcome"]
            for r in hl.resolve_vanished(prev, {"keep"}, {hl.entry_hash("fact")}, events)
        }
        self.assertEqual(got, {"m1": "approved", "m2": "rejected", "s1": "approved"})

    def test_prom_keeps_a_timestamp_to_the_second(self):
        # MUST FAIL under the old `{value:g}`: six significant digits put this 4,500 s ahead.
        ts = 1793335500.25
        text = hl.prom([("t_seconds", {}, ts)], {})
        self.assertEqual(float(text.split()[-1]), ts)
        self.assertNotIn("e+", text)
        # Near-misses: small numbers, ratios, integers and booleans stay exact and readable.
        self.assertEqual(hl.prom_number(0.5), "0.5")
        self.assertEqual(hl.prom_number(1.0), "1")
        self.assertEqual(hl.prom_number(14), "14")
        self.assertEqual(hl.prom_number(True), "1")
        self.assertEqual(float(hl.prom_number(2 / 3)), 2 / 3)

    def test_rejection_ratio(self):
        self.assertIsNone(hl.rejection_ratio({}))
        self.assertEqual(
            hl.rejection_ratio({"memory_rejected_inferred": 1, "skills_approved_inferred": 3}),
            0.25,
        )


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        (self.home / "memories").mkdir(parents=True)
        (self.home / "skills" / "deploy").mkdir(parents=True)
        (self.home / "skills" / "deploy" / "SKILL.md").write_text("---\nname: deploy\n---\n")
        (self.home / "skills" / ".archive" / "old").mkdir(parents=True)
        (self.home / "skills" / ".archive" / "old" / "SKILL.md").write_text("x")
        self.addCleanup(self.tmp.cleanup)
        for k in ("HL_TEST_SLACK", "HL_TEST_CHAN", "HL_TEST_API_KEY", "HL_TEST_GW"):
            os.environ.pop(k, None)

    def cfgpath(self) -> str:
        return str(self.root / "cfg.json")

    def test_hourly_metrics_and_mirror_copy(self):
        now = time.time()
        stage(
            self.home,
            "memory",
            "m1",
            now - 90000,
            {"action": "add", "target": "memory", "content": "the fact"},
            summary="user email bob@example.com",
        )
        stage(self.home, "skills", "s1", now - 60, {"action": "patch", "name": "deploy"})
        (self.home / "skills" / ".curator_ledger.jsonl").write_text(
            json.dumps(
                {
                    "id": "1",
                    "ts": "2026-10-29T10:00:00+00:00",
                    "actor": "agent",
                    "action": "create",
                    "skill": "deploy",
                }
            )
            + "\n"
        )
        cfg = make_cfg(self.root)
        self.assertEqual(hl.main(["--config", self.cfgpath(), "hourly"]), 0)
        p = Path(cfg["textfile_dir"]) / "hermes_learning.prom"
        self.assertEqual(prom_value(p, "hermes_learning_read_ok"), 1)
        self.assertEqual(prom_value(p, "hermes_learning_pending", 'subsystem="memory"'), 1)
        self.assertGreaterEqual(
            prom_value(p, "hermes_learning_pending_oldest_age_seconds", 'subsystem="memory"'),
            90000,
        )
        self.assertEqual(prom_value(p, "hermes_learning_skills_total"), 1)  # .archive is not live
        self.assertEqual(prom_value(p, "hermes_learning_ping_configured"), 0)
        week = json.loads(next((self.home / "learning" / "ledger").glob("*.json")).read_text())
        self.assertNotIn(
            "bob@example.com", json.dumps(week)
        )  # the mirror copy carries no summaries
        # approve m1 (its text now in MEMORY.md), reject s1: the second run infers both
        (self.home / "memories" / "MEMORY.md").write_text("the fact")
        for f in (self.home / "pending").rglob("*.json"):
            f.unlink()
        self.assertEqual(hl.main(["--config", self.cfgpath(), "hourly"]), 0)
        led = json.loads((Path(cfg["state_dir"]) / "ledger.json").read_text())
        wk = led["weeks"][hl.iso_week(time.time())]
        self.assertEqual(wk.get("memory_approved_inferred"), 1)
        self.assertEqual(wk.get("skills_rejected_inferred"), 1)
        self.assertEqual(prom_value(p, "hermes_learning_pending", 'subsystem="memory"'), 0)

    def test_hourly_unreadable_state_reports_read_ok_zero(self):
        # a home with no skills/ (wrong path, unmounted volume) must read as cannot-tell, not zero
        cfg = make_cfg(self.root, home=str(self.root / "nope"))
        self.assertEqual(hl.main(["--config", self.cfgpath(), "hourly"]), 1)
        p = Path(cfg["textfile_dir"]) / "hermes_learning.prom"
        self.assertEqual(prom_value(p, "hermes_learning_read_ok"), 0)
        self.assertIsNone(prom_value(p, "hermes_learning_pending", 'subsystem="memory"'))
        # so must a missing interpreter (docker absent on the host, say)
        make_cfg(self.root, local_python="/nonexistent/python")
        self.assertEqual(hl.main(["--config", self.cfgpath(), "hourly"]), 1)
        # near miss: the real home reads fine
        make_cfg(self.root)
        self.assertEqual(hl.main(["--config", self.cfgpath(), "hourly"]), 0)
        self.assertEqual(prom_value(p, "hermes_learning_read_ok"), 1)

    def _memory(self, n):
        (self.home / "memories" / "MEMORY.md").write_text(
            DELIM.join(f"entry {i}" for i in range(n))
        )

    def test_consolidate_stages_within_cap(self):
        self._memory(8)
        prop = self.root / "prop.json"
        prop.write_text(
            json.dumps(
                [
                    {"content": "merged 0-3", "sources": [0, 1, 2, 3]},
                    {"content": "merged 4-5", "sources": [4, 5]},
                ]
            )
        )  # 2 of 8 dropped = 25 %: at the cap
        make_cfg(self.root)
        before = (self.home / "memories" / "MEMORY.md").read_bytes()
        args = ["--config", self.cfgpath(), "consolidate", "--proposal-file", str(prop)]
        self.assertEqual(hl.main(args), 0)
        recs = list((self.home / "pending" / "memory").glob("*.json"))
        self.assertEqual(len(recs), 1)
        rec = json.loads(recs[0].read_text())
        self.assertEqual(rec["payload"]["action"], "batch")
        self.assertTrue(rec["summary"].startswith("consolidation:"))
        # MEMORY.md is never written by the job
        self.assertEqual((self.home / "memories" / "MEMORY.md").read_bytes(), before)
        self.assertTrue(list((self.home / "learning" / "consolidation").glob("*.proposed.md")))
        # a second run does not stack a second proposal
        self.assertEqual(hl.main(args), 0)
        self.assertEqual(len(list((self.home / "pending" / "memory").glob("*.json"))), 1)

    def test_consolidate_refuses_over_cap(self):
        self._memory(8)
        prop = self.root / "prop.json"
        prop.write_text(json.dumps([{"content": "keep 0-4", "sources": [0, 1, 2, 3, 4]}]))  # 3/8
        make_cfg(self.root)
        args = ["--config", self.cfgpath(), "consolidate", "--proposal-file", str(prop)]
        self.assertEqual(hl.main(args), 2)
        self.assertFalse((self.home / "pending" / "memory").exists())
        self.assertTrue(list((self.home / "learning" / "consolidation").glob("*.refused.json")))

    def test_consolidate_skips_small_memory(self):
        self._memory(2)
        make_cfg(self.root)
        self.assertEqual(hl.main(["--config", self.cfgpath(), "consolidate"]), 0)
        self.assertFalse((self.home / "pending").exists())


class FakeRuns(http.server.BaseHTTPRequestHandler):
    answers: dict = {}
    runs: dict = {}

    def log_message(self, *a):
        pass

    def _json(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.headers.get("Authorization") != "Bearer k":
            return self._json(401, {"error": "bad key"})
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        rid = f"run_{len(self.runs)}"
        self.runs[rid] = body["input"]
        self._json(202, {"run_id": rid, "status": "queued"})

    def do_GET(self):
        parts = self.path.strip("/").split("/")
        rid = parts[2]
        if parts[-1] == "events":
            data = b'data: {"event": "tool.started", "tool": "skill_view"}\n\n'
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            return self.wfile.write(data)
        prompt = self.runs[rid]
        self._json(
            200,
            {
                "status": "completed",
                "output": self.answers.get(prompt, "no idea"),
                "usage": {"total_tokens": 7},
            },
        )


class Probes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "home").mkdir()
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeRuns)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.shutdown)
        os.environ["HL_TEST_API_KEY"] = "k"
        self.addCleanup(os.environ.pop, "HL_TEST_API_KEY", None)
        (self.root / "probes.json").write_text(
            json.dumps(
                [
                    {
                        "id": "p-math",
                        "prompt": "17*23?",
                        "expect": {"regex": [r"\b391\b"]},
                    },
                    {
                        "id": "p-skill",
                        "prompt": "canary?",
                        "expect": {"contains": ["heliotrope"], "tool": ["skill_view"]},
                    },
                ]
            )
        )
        self.cfg = make_cfg(self.root, api_url=f"http://127.0.0.1:{self.srv.server_port}")

    def run_probes(self):
        self.assertEqual(hl.main(["--config", str(self.root / "cfg.json"), "probes"]), 0)
        return Path(self.cfg["textfile_dir"]) / "hermes_probe.prom"

    def test_pass_then_broken_skill_lowers_rate(self):
        FakeRuns.answers = {
            "17*23?": "391",
            "canary?": "The canary word is heliotrope.",
        }
        p = self.run_probes()
        self.assertEqual(prom_value(p, "hermes_probe_pass_ratio"), 1)
        self.assertEqual(prom_value(p, "hermes_probe_tokens"), 14)
        FakeRuns.answers["canary?"] = "The canary word is marigold."  # the skill was broken
        p = self.run_probes()
        self.assertEqual(prom_value(p, "hermes_probe_pass_ratio"), 0.5)
        self.assertEqual(prom_value(p, "hermes_probe_result", 'probe="p-skill"'), 0)
        hist = (Path(self.cfg["state_dir"]) / "probes-history.jsonl").read_text().splitlines()
        self.assertEqual(json.loads(hist[-1])["failed"], ["p-skill"])

    def _probe_json(self):
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(hl.main(["--config", str(self.root / "cfg.json"), "probes"]), 0)
        return json.loads([l for l in buf.getvalue().splitlines() if l.startswith("{")][-1])

    def test_a_break_masked_by_a_recovery_is_still_a_regression(self):
        # MUST FLAG: one probe recovers while another breaks, so the ratio is
        # unchanged and a ratio-based check reports nothing.
        FakeRuns.answers = {"17*23?": "no idea", "canary?": "The canary word is heliotrope."}
        first = self._probe_json()
        self.assertEqual(first["failed"], ["p-math"])
        FakeRuns.answers = {"17*23?": "391", "canary?": "The canary word is marigold."}
        second = self._probe_json()
        self.assertEqual(second["passed"], first["passed"])
        self.assertTrue(second["regressed"])
        self.assertEqual(second["newly_failed"], ["p-skill"])
        # Near-miss: a pure improvement is not a regression.
        FakeRuns.answers = {"17*23?": "391", "canary?": "The canary word is heliotrope."}
        third = self._probe_json()
        self.assertFalse(third["regressed"])
        self.assertEqual(third["newly_failed"], [])

    def test_wrong_key_fails_every_probe_not_the_run(self):
        os.environ["HL_TEST_API_KEY"] = "wrong"
        FakeRuns.answers = {"17*23?": "391"}
        p = self.run_probes()
        self.assertEqual(prom_value(p, "hermes_probe_read_ok"), 1)
        self.assertEqual(prom_value(p, "hermes_probe_passed"), 0)

    def test_empty_probe_set_is_refused(self):
        (self.root / "probes.json").write_text("[]")
        self.assertEqual(hl.main(["--config", str(self.root / "cfg.json"), "probes"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=1)
