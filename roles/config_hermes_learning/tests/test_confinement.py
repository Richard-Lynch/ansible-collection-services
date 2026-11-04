#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""hermes_learning.py runs as ROOT and writes into a tree the AGENT can write: no write may follow
a link out of it.

The mirror copies (learning/ledger, learning/probes, learning/consolidation under host_home) are
the only writes into the agent's tree. Each case plants what the agent could plant (a symlinked
directory at any level, a symlinked file name) and requires that nothing outside the tree changes,
beside a near-miss of ordinary files and directories that must keep working. The privilege drop is
exercised with the id-changing calls faked (an unprivileged test cannot setresuid), which still
proves the write happens in a child AFTER the drop and never in the root parent.

Stdlib only. Run: python3 roles/config_hermes_learning/tests/test_confinement.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent / "files"))
sys.path.insert(0, str(TESTS))
import hermes_learning as hl  # noqa: E402
from test_hermes_learning import make_cfg  # noqa: E402

SECRET = "OUTSIDE-SECRET\n"


class Base(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.t = Path(self._t.name)
        self.home = self.t / "home"
        self.outside = self.t / "outside"
        (self.home / "memories").mkdir(parents=True)
        (self.home / "skills").mkdir()
        self.outside.mkdir()
        (self.outside / "secret.txt").write_text(SECRET)
        for k in ("HL_TEST_SLACK", "HL_TEST_CHAN", "HL_TEST_API_KEY", "HL_TEST_GW"):
            os.environ.pop(k, None)

    def outside_untouched(self):
        self.assertEqual(sorted(p.name for p in self.outside.iterdir()), ["secret.txt"])
        self.assertEqual((self.outside / "secret.txt").read_text(), SECRET)


class WriteConfined(Base):
    def test_near_miss_ordinary_tree_is_written(self):
        hl.write_confined(self.home, "learning/ledger/2026-W44.json", "one")
        f = self.home / "learning" / "ledger" / "2026-W44.json"
        self.assertEqual(f.read_text(), "one")
        self.assertEqual(f.stat().st_mode & 0o777, 0o644)
        # an existing regular file is replaced, and an existing real directory is reused
        hl.write_confined(self.home, "learning/ledger/2026-W44.json", "two")
        self.assertEqual(f.read_text(), "two")
        self.assertEqual([p.name for p in f.parent.iterdir()], ["2026-W44.json"])  # no tmp left

    def test_symlinked_subdirectory_is_refused(self):
        (self.home / "learning").mkdir()
        os.symlink(self.outside, self.home / "learning" / "ledger")
        with self.assertRaises(hl.Refused):
            hl.write_confined(self.home, "learning/ledger/2026-W44.json", "x")
        self.outside_untouched()

    def test_symlinked_learning_dir_is_refused(self):
        os.symlink(self.outside, self.home / "learning")
        with self.assertRaises(hl.Refused):
            hl.write_confined(self.home, "learning/ledger/2026-W44.json", "x")
        self.outside_untouched()

    def test_symlinked_file_is_replaced_never_written_through(self):
        (self.home / "learning" / "ledger").mkdir(parents=True)
        dest = self.home / "learning" / "ledger" / "2026-W44.json"
        os.symlink(self.outside / "secret.txt", dest)
        hl.write_confined(self.home, "learning/ledger/2026-W44.json", "mine")
        self.outside_untouched()
        self.assertFalse(dest.is_symlink())
        self.assertEqual(dest.read_text(), "mine")

    def test_dangling_symlinked_file_creates_nothing_outside(self):
        (self.home / "learning" / "probes").mkdir(parents=True)
        os.symlink(self.outside / "new.txt", self.home / "learning" / "probes" / "latest.json")
        hl.write_confined(self.home, "learning/probes/latest.json", "{}")
        self.outside_untouched()

    def test_dotdot_and_absolute_are_refused(self):
        for bad in (
            "../outside/x",
            "learning/../../outside/x",
            str(self.outside / "x"),
        ):
            with self.assertRaises(hl.Refused, msg=bad):
                hl.write_confined(self.home, bad, "x")
        self.outside_untouched()

    def test_symlinked_root_is_refused(self):
        os.symlink(self.outside, self.t / "linkhome")
        with self.assertRaises(hl.Refused):
            hl.write_confined(self.t / "linkhome", "x.json", "x")
        self.outside_untouched()

    def test_foreign_owned_directory_is_refused(self):
        (self.home / "learning").mkdir()
        with self.assertRaises(hl.Refused):
            hl.write_confined(self.home, "learning/x.json", "x", owner=os.geteuid() + 1)
        self.assertFalse((self.home / "learning" / "x.json").exists())
        # near miss: the right owner
        hl.write_confined(self.home, "learning/x.json", "x", owner=os.geteuid())
        self.assertTrue((self.home / "learning" / "x.json").is_file())


class Commands(Base):
    """The real commands, end to end (inner scripts run locally against the fixture home)."""

    def cfg(self):
        make_cfg(self.t)
        return str(self.t / "cfg.json")

    def test_hourly_with_symlinked_ledger_dir_writes_nothing_outside(self):
        (self.home / "learning").mkdir()
        os.symlink(self.outside, self.home / "learning" / "ledger")
        self.assertEqual(hl.main(["--config", self.cfg(), "hourly"]), 1)
        self.outside_untouched()

    def test_hourly_with_symlinked_learning_dir_writes_nothing_outside(self):
        os.symlink(self.outside, self.home / "learning")
        self.assertEqual(hl.main(["--config", self.cfg(), "hourly"]), 1)
        self.outside_untouched()

    def test_hourly_near_miss_ordinary_dirs(self):
        (self.home / "learning" / "ledger").mkdir(parents=True)
        self.assertEqual(hl.main(["--config", self.cfg(), "hourly"]), 0)
        self.assertEqual(len(list((self.home / "learning" / "ledger").glob("*.json"))), 1)

    def _memory(self, n):
        (self.home / "memories" / "MEMORY.md").write_text(
            "\n§\n".join(f"entry {i}" for i in range(n))
        )

    def test_consolidate_with_symlinked_record_dir_writes_nothing_outside(self):
        self._memory(8)
        prop = self.t / "prop.json"
        prop.write_text(json.dumps([{"content": "keep 0-4", "sources": [0, 1, 2, 3, 4]}]))
        (self.home / "learning").mkdir()
        os.symlink(self.outside, self.home / "learning" / "consolidation")
        args = ["--config", self.cfg(), "consolidate", "--proposal-file", str(prop)]
        self.assertNotEqual(hl.main(args), 0)
        self.outside_untouched()
        # near miss: a real directory takes the refusal record
        (self.home / "learning" / "consolidation").unlink()
        self.assertEqual(hl.main(args), 2)
        self.assertTrue(list((self.home / "learning" / "consolidation").glob("*.refused.json")))

    def test_agent_write_covers_every_mirror_dir(self):
        cfg = make_cfg(self.t)
        for sub in ("ledger", "probes", "consolidation"):
            (self.home / "learning").mkdir(exist_ok=True)
            link = self.home / "learning" / sub
            os.symlink(self.outside, link)
            with self.assertRaises(hl.Refused, msg=sub):
                hl.agent_write(cfg, f"{sub}/f.json", "x")
            link.unlink()
            hl.agent_write(cfg, f"{sub}/f.json", "x")  # near miss
            self.assertTrue((link / "f.json").is_file())
        self.outside_untouched()


class PrivilegeDrop(Base):
    """Under root in docker mode the write runs in a forked child after setgroups/setresgid/
    setresuid to the agent's ids. The id calls are faked; the fork and the write are real."""

    def setUp(self):
        super().setUp()
        self.log = self.t / "drop.log"
        self.uid, self.gid = os.getuid(), os.getgid()
        self.cfg = make_cfg(self.t, exec_mode="docker", exec_user=f"{self.uid}:{self.gid}")
        ids = {"euid": 0, "egid": 0}

        def rec(*a):
            with open(self.log, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(a) + "\n")

        def setgroups(g):
            rec("setgroups", g)

        def setresgid(r, e, s):
            rec("setresgid", r, e, s, os.path.exists(self.dest))
            ids["egid"] = e

        def setresuid(r, e, s):
            rec("setresuid", r, e, s, os.path.exists(self.dest))
            ids["euid"] = e

        self.dest = self.home / "learning" / "ledger" / "w.json"
        self.fakes = [
            mock.patch.object(os, "geteuid", lambda: ids["euid"]),
            mock.patch.object(os, "getegid", lambda: ids["egid"]),
            mock.patch.object(os, "setgroups", setgroups),
            mock.patch.object(os, "setresgid", setresgid, create=True),
            mock.patch.object(os, "setresuid", setresuid, create=True),
        ]
        for f in self.fakes:
            f.start()
            self.addCleanup(f.stop)

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(x) for x in self.log.read_text().splitlines()]

    def test_write_happens_after_the_drop(self):
        hl.agent_write(self.cfg, "ledger/w.json", "as agent")
        self.assertEqual(self.dest.read_text(), "as agent")
        u, g = self.uid, self.gid
        self.assertEqual(
            self.calls(),
            [
                ["setgroups", []],
                ["setresgid", g, g, g, False],
                [
                    "setresuid",
                    u,
                    u,
                    u,
                    False,
                ],  # nothing written before the drop completed
            ],
        )

    def test_failed_drop_writes_nothing(self):
        def refuse(*a):
            raise PermissionError("not permitted")

        with mock.patch.object(os, "setresuid", refuse, create=True):
            with self.assertRaises(hl.Refused):
                hl.agent_write(self.cfg, "ledger/w.json", "x")
        self.assertFalse(self.dest.exists())

    def test_symlink_is_still_refused_after_the_drop(self):
        (self.home / "learning").mkdir()
        os.symlink(self.outside, self.home / "learning" / "ledger")
        with self.assertRaises(hl.Refused):
            hl.agent_write(self.cfg, "ledger/w.json", "x")
        self.outside_untouched()

    def test_non_numeric_exec_user_is_refused(self):
        cfg = dict(self.cfg, exec_user="hermes")
        with self.assertRaises(hl.Refused):
            hl.agent_write(cfg, "ledger/w.json", "x")
        self.assertEqual(self.calls(), [])


class EnsureDirs(Base):
    """The learning directories are made BY THE AGENT and never re-owned: a directory already at a
    target path that the agent does not own (a root-owned one it renamed into place) is refused and
    left exactly as it was, beside the near-miss of an agent-owned one, which is reused."""

    RELS = ["learning", "learning/ledger", "learning/probes"]

    def test_near_miss_agent_owned_dirs_are_reused_and_missing_ones_made(self):
        (self.home / "learning").mkdir(mode=0o755)
        out = hl.ensure_dirs_confined(self.home, self.RELS, os.geteuid())
        self.assertEqual(out["created"], ["learning/ledger", "learning/probes"])
        for rel in self.RELS:
            st = (self.home / rel).stat()
            self.assertEqual((st.st_uid, st.st_mode & 0o777), (os.geteuid(), 0o755), rel)

    def test_a_dir_the_agent_does_not_own_is_refused_and_never_chowned(self):
        # Owned by the test's uid, which here plays root: the "agent" is another uid.
        (self.home / "learning").mkdir(mode=0o700)
        before = (self.home / "learning").stat()
        with mock.patch("os.fchown") as fchown, mock.patch("os.chown") as chown:
            with self.assertRaisesRegex(hl.Refused, "never re-owned"):
                hl.ensure_dirs_confined(self.home, self.RELS, os.geteuid() + 4242)
        fchown.assert_not_called()
        chown.assert_not_called()
        after = (self.home / "learning").stat()
        self.assertEqual((after.st_uid, after.st_mode), (before.st_uid, before.st_mode))
        self.assertFalse((self.home / "learning" / "ledger").exists())

    def test_a_link_at_a_target_is_refused(self):
        (self.home / "learning").symlink_to(self.outside)
        with self.assertRaises(hl.Refused):
            hl.ensure_dirs_confined(self.home, self.RELS, os.geteuid())
        self.outside_untouched()

    def test_mode_is_corrected_by_the_owner(self):
        (self.home / "learning").mkdir(mode=0o700)
        out = hl.ensure_dirs_confined(self.home, ["learning"], os.geteuid())
        self.assertEqual(out["repaired"], ["learning"])
        self.assertEqual((self.home / "learning").stat().st_mode & 0o777, 0o755)

    def test_as_root_the_walk_runs_in_the_dropped_child_only(self):
        cfg = make_cfg(self.t, exec_mode="docker", exec_user="4242")
        seen = []

        def fake_run_as(uid, gid, fn):
            seen.append((uid, gid))  # the walk is fn: it is NOT run in the root parent

        with (
            mock.patch("os.geteuid", return_value=0),
            mock.patch.object(hl.confined_io, "run_as", side_effect=fake_run_as),
            mock.patch("os.fchown") as fchown,
            mock.patch.object(hl, "ensure_dirs_confined") as walk,
        ):
            self.assertEqual(hl.cmd_ensure_dirs(cfg, self.RELS), 0)
        self.assertEqual(seen, [(4242, 4242)])
        walk.assert_not_called()
        fchown.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=1)
