#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for agent_dir.py: root ensuring directories in the agent's tree without following links.

Stdlib-only (CI's stdlib group), unprivileged: the agent uid and the anchor owner are both the
test's own uid, so every chown is a no-op and what is tested is WHICH directory gets touched.
Each refusal is paired with a near-miss that must pass, and every symlink case also asserts the
link's target was left exactly as it was -- refusing loudly after writing outside is the bug.

Run: python3 roles/service_hermes/scripts/test_agent_dir.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import agent_dir  # noqa: E402

ME, MYGID = os.getuid(), os.getgid()


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self._tmp.name)
        self.anchor = os.path.join(self.root, "hermes")
        self.outside = os.path.join(self.root, "outside")
        os.mkdir(self.anchor, 0o755)
        os.mkdir(self.outside, 0o755)
        with open(os.path.join(self.outside, "sentinel"), "w") as fh:
            fh.write("untouched\n")
        os.chmod(self.anchor, 0o755)

    def tearDown(self):
        self._tmp.cleanup()

    def run_ensure(self, rel, own=False, anchor_uid=ME):
        report = {"created": [], "repaired": []}
        agent_dir.ensure(self.anchor, anchor_uid, rel, ME, MYGID, 0o755, own, report)
        return report

    def assert_outside_untouched(self):
        self.assertEqual(sorted(os.listdir(self.outside)), ["sentinel"])
        with open(os.path.join(self.outside, "sentinel")) as fh:
            self.assertEqual(fh.read(), "untouched\n")


class Creates(Base):
    def test_missing_components_are_created_as_real_directories(self):
        r = self.run_ensure("data/scripts/estate")
        self.assertEqual(len(r["created"]), 3)
        p = os.path.join(self.anchor, "data/scripts/estate")
        self.assertTrue(os.path.isdir(p) and not os.path.islink(p))
        self.assertEqual(oct(os.stat(p).st_mode & 0o777), "0o755")

    def test_near_miss_existing_real_directories_are_no_change(self):
        os.makedirs(os.path.join(self.anchor, "data/scripts/estate"))
        r = self.run_ensure("data/scripts/estate", own=True)
        self.assertEqual(r, {"created": [], "repaired": []})


class RefusesSymlinks(Base):
    def test_leaf_symlink_out_of_the_tree_is_refused_and_not_followed(self):
        os.makedirs(os.path.join(self.anchor, "data/scripts"))
        os.symlink(self.outside, os.path.join(self.anchor, "data/scripts/estate"))
        with self.assertRaisesRegex(agent_dir.Refused, "not a real directory"):
            self.run_ensure("data/scripts/estate", own=True)
        self.assert_outside_untouched()

    def test_intermediate_symlink_is_refused_before_anything_is_created_through_it(
        self,
    ):
        os.makedirs(os.path.join(self.anchor, "data"))
        os.symlink(self.outside, os.path.join(self.anchor, "data/scripts"))
        with self.assertRaisesRegex(agent_dir.Refused, "data/scripts is not a real directory"):
            self.run_ensure("data/scripts/estate")
        self.assert_outside_untouched()

    def test_a_symlink_swapped_in_after_the_mkdir_is_refused(self):
        # The race: the agent replaces the freshly made directory with a link before root opens it.
        os.makedirs(os.path.join(self.anchor, "data"))
        real_mkdir = os.mkdir
        outside = self.outside

        def mkdir_then_swap(name, mode=0o777, *, dir_fd=None):
            real_mkdir(name, mode, dir_fd=dir_fd)
            os.rmdir(name, dir_fd=dir_fd)
            os.symlink(outside, name, dir_fd=dir_fd)

        with mock.patch.object(agent_dir.os, "mkdir", mkdir_then_swap):
            with self.assertRaisesRegex(agent_dir.Refused, "not a real directory"):
                self.run_ensure("data/scripts")
        self.assert_outside_untouched()

    def test_a_file_where_a_directory_belongs_is_refused(self):
        os.makedirs(os.path.join(self.anchor, "data"))
        open(os.path.join(self.anchor, "data/scripts"), "w").close()
        with self.assertRaisesRegex(agent_dir.Refused, "not a real directory"):
            self.run_ensure("data/scripts")

    def test_near_miss_a_symlink_INSIDE_a_real_leaf_is_not_the_leaf(self):
        # The agent's own links under scripts/ are its business; only the path walked is checked.
        os.makedirs(os.path.join(self.anchor, "data/scripts"))
        os.symlink(self.outside, os.path.join(self.anchor, "data/scripts/mine"))
        self.assertEqual(self.run_ensure("data/scripts", own=True), {"created": [], "repaired": []})
        self.assert_outside_untouched()


class RefusesAnchorAndPath(Base):
    def test_anchor_owned_by_someone_else_is_refused(self):
        with self.assertRaisesRegex(agent_dir.Refused, "anchor .* is owned by uid"):
            self.run_ensure("data", anchor_uid=ME + 1)

    def test_group_writable_anchor_is_refused(self):
        os.chmod(self.anchor, 0o775)
        with self.assertRaisesRegex(agent_dir.Refused, "group/other-writable"):
            self.run_ensure("data")

    def test_symlinked_anchor_is_refused(self):
        link = os.path.join(self.root, "anchorlink")
        os.symlink(self.anchor, link)
        report = {"created": [], "repaired": []}
        with self.assertRaisesRegex(agent_dir.Refused, "not a real directory"):
            agent_dir.ensure(link, ME, "data", ME, MYGID, 0o755, False, report)

    def test_dotdot_and_absolute_paths_are_refused(self):
        for rel in ("../outside", "data/../../outside", "/etc"):
            with self.assertRaisesRegex(agent_dir.Refused, "relative to the anchor"):
                self.run_ensure(rel)
        self.assert_outside_untouched()


@unittest.skipUnless(os.geteuid() == 0, "needs root to own a directory as another uid")
class Ownership(Base):
    def test_component_owned_by_a_third_uid_is_refused(self):
        p = os.path.join(self.anchor, "data")
        os.mkdir(p)
        os.chown(p, 4242, 4242)
        report = {"created": [], "repaired": []}
        with self.assertRaisesRegex(agent_dir.Refused, "owned by uid 4242"):
            agent_dir.ensure(self.anchor, 0, "data/scripts", 10000, 10000, 0o755, False, report)

    def test_root_owned_leaf_is_repaired_to_the_agent_by_fd(self):
        os.makedirs(os.path.join(self.anchor, "data/scripts"))
        report = {"created": [], "repaired": []}
        agent_dir.ensure(self.anchor, 0, "data/scripts", 10000, 10000, 0o755, True, report)
        self.assertEqual(os.stat(os.path.join(self.anchor, "data/scripts")).st_uid, 10000)


class Process(Base):
    def _cli(self, *extra):
        return subprocess.run(
            [
                sys.executable,
                os.path.join(HERE, "agent_dir.py"),
                "--anchor",
                self.anchor,
                "--anchor-uid",
                str(ME),
                "--uid",
                str(ME),
                "--gid",
                str(MYGID),
                *extra,
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_refusal_exits_2_and_reports(self):
        os.makedirs(os.path.join(self.anchor, "data"))
        os.symlink(self.outside, os.path.join(self.anchor, "data/scripts"))
        p = self._cli("--own", "data/scripts")
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertIn("REFUSED", p.stderr)
        self.assertIn("refused", json.loads(p.stdout))
        self.assert_outside_untouched()

    def test_near_miss_success_exits_0_and_is_idempotent(self):
        first = self._cli("--own", "data/scripts", "--ensure", "data/scripts/estate")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertTrue(json.loads(first.stdout)["changed"])
        second = self._cli("--own", "data/scripts", "--ensure", "data/scripts/estate")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertFalse(json.loads(second.stdout)["changed"])


class Touch(Base):
    """--touch: the audit log the shipper must find before its first line. `copy: force=false` as
    root resolved the path by name, so a planted data/logs link redirected the create."""

    def setUp(self):
        super().setUp()
        os.makedirs(os.path.join(self.anchor, "data"))
        self.log = os.path.join(self.anchor, "data", "logs", "tool-audit.jsonl")

    def touch(self):
        report = {"created": [], "repaired": []}
        agent_dir.touch(
            self.anchor, ME, "data/logs/tool-audit.jsonl", ME, MYGID, 0o755, 0o644, report
        )
        return report

    def test_creates_parent_and_empty_file_with_the_mode(self):
        r = self.touch()
        self.assertIn(self.log, r["created"])
        self.assertEqual(os.path.getsize(self.log), 0)
        self.assertEqual(os.stat(self.log).st_mode & 0o777, 0o644)

    def test_near_miss_an_existing_trail_is_never_truncated_or_restamped(self):
        self.touch()
        with open(self.log, "w") as fh:
            fh.write('{"tool": "x"}\n')
        os.utime(self.log, (1_000_000, 1_000_000))
        self.assertEqual(self.touch(), {"created": [], "repaired": []})
        with open(self.log) as fh:
            self.assertEqual(fh.read(), '{"tool": "x"}\n')
        self.assertEqual(os.stat(self.log).st_mtime, 1_000_000)

    def test_link_at_the_name_is_refused_not_followed(self):
        os.makedirs(os.path.dirname(self.log))
        os.symlink(os.path.join(self.outside, "planted"), self.log)
        with self.assertRaisesRegex(agent_dir.Refused, "symlink"):
            self.touch()
        self.assert_outside_untouched()

    def test_link_at_the_parent_is_refused(self):
        os.symlink(self.outside, os.path.join(self.anchor, "data", "logs"))
        with self.assertRaisesRegex(agent_dir.Refused, "not a real directory"):
            self.touch()
        self.assert_outside_untouched()

    def test_a_directory_at_the_name_is_refused(self):
        os.makedirs(self.log)
        with self.assertRaisesRegex(agent_dir.Refused, "not a regular file"):
            self.touch()


class SetLine(Base):
    """--set-line: the data/.env edit `lineinfile` used to make, which realpath()s its dest and so
    appended to (and chowned) whatever a planted link pointed at."""

    def setUp(self):
        super().setUp()
        os.makedirs(os.path.join(self.anchor, "data"))
        self.env = os.path.join(self.anchor, "data", ".env")

    def set(self, value="k1", key="API_SERVER_KEY", rel="data/.env"):
        report = {"created": [], "repaired": []}
        agent_dir.set_line(self.anchor, ME, rel, key, value, ME, MYGID, 0o755, 0o600, report)
        return report

    def read(self):
        with open(self.env) as fh:
            return fh.read()

    def test_creates_absent_file_owned_and_0600(self):
        r = self.set()
        self.assertEqual(r["created"], [self.env])
        self.assertEqual(self.read(), "API_SERVER_KEY=k1\n")
        self.assertEqual(os.stat(self.env).st_mode & 0o777, 0o600)

    def test_replaces_every_key_line_once_and_keeps_the_rest(self):
        with open(self.env, "w") as fh:
            fh.write("A=1\nAPI_SERVER_KEY=old\nB=2\nAPI_SERVER_KEY=older\nAPI_SERVER_KEY_X=keep\n")
        os.chmod(self.env, 0o600)
        self.set("new")
        self.assertEqual(self.read(), "A=1\nAPI_SERVER_KEY=new\nB=2\nAPI_SERVER_KEY_X=keep\n")

    def test_idempotent_writes_nothing_the_second_time(self):
        self.set()
        ino = os.stat(self.env).st_ino
        self.assertEqual(self.set(), {"created": [], "repaired": []})
        self.assertEqual(os.stat(self.env).st_ino, ino)
        os.chmod(self.env, 0o644)  # mode drift alone is a change
        self.assertEqual(self.set()["repaired"], [self.env])
        self.assertEqual(os.stat(self.env).st_mode & 0o777, 0o600)

    def test_link_at_the_name_is_refused_not_followed(self):
        os.symlink(os.path.join(self.outside, "sentinel"), self.env)
        with self.assertRaisesRegex(agent_dir.Refused, "symlink"):
            self.set()
        self.assert_outside_untouched()
        self.assertTrue(os.path.islink(self.env))

    def test_link_at_the_parent_is_refused(self):
        os.rmdir(os.path.join(self.anchor, "data"))
        os.symlink(self.outside, os.path.join(self.anchor, "data"))
        with self.assertRaisesRegex(agent_dir.Refused, "not a real directory"):
            self.set()
        self.assert_outside_untouched()

    def test_fifo_and_bad_key_refused(self):
        os.mkfifo(self.env)
        with self.assertRaisesRegex(agent_dir.Refused, "not a regular file"):
            self.set()
        os.unlink(self.env)
        for key, value in (("A=B", "v"), ("", "v"), ("K", "two\nlines")):
            with self.assertRaises(agent_dir.Refused, msg=key):
                self.set(value, key)

    def _cli(self, env_value):
        env = dict(os.environ)
        env.pop("AGENT_DIR_VALUE", None)
        if env_value is not None:
            env["AGENT_DIR_VALUE"] = env_value
        return subprocess.run(
            [
                sys.executable,
                os.path.join(HERE, "agent_dir.py"),
                "--anchor",
                self.anchor,
                "--anchor-uid",
                str(ME),
                "--uid",
                str(ME),
                "--gid",
                str(MYGID),
                "--set-line",
                "data/.env",
                "--key",
                "API_SERVER_KEY",
            ],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )

    def test_cli_value_from_environment_never_echoed(self):
        p = self._cli("s3cret-value-xyz")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(json.loads(p.stdout)["changed"])
        self.assertNotIn("s3cret-value-xyz", p.stdout + p.stderr)
        self.assertEqual(self.read(), "API_SERVER_KEY=s3cret-value-xyz\n")
        self.assertFalse(json.loads(self._cli("s3cret-value-xyz").stdout)["changed"])

    def test_cli_without_the_value_refuses(self):
        p = self._cli(None)
        self.assertEqual(p.returncode, 2)
        self.assertFalse(os.path.exists(self.env))


class WriteFile(Base):
    """--write-file: a role-owned file (the dashboard theme) inside the agent's tree, written
    whole. Same walk and same refusals as --set-line."""

    def setUp(self):
        super().setUp()
        os.makedirs(os.path.join(self.anchor, "data"))
        self.dest = os.path.join(self.anchor, "data", "dashboard-themes", "readable.yaml")

    def write(self, content="name: readable\n", rel="data/dashboard-themes/readable.yaml"):
        report = {"created": [], "repaired": []}
        agent_dir.write_file(self.anchor, ME, rel, content, ME, MYGID, 0o755, 0o644, report)
        return report

    def read(self):
        with open(self.dest) as fh:
            return fh.read()

    def test_creates_parent_and_file_with_the_mode(self):
        r = self.write()
        self.assertEqual(r["created"], [os.path.dirname(self.dest), self.dest])
        self.assertEqual(self.read(), "name: readable\n")
        self.assertEqual(os.stat(self.dest).st_mode & 0o777, 0o644)

    def test_replaces_the_whole_content(self):
        self.write("name: old\nextra: 1\n")
        self.assertEqual(self.write("name: readable\n")["repaired"], [self.dest])
        self.assertEqual(self.read(), "name: readable\n")

    def test_near_miss_unchanged_content_writes_nothing(self):
        self.write()
        ino = os.stat(self.dest).st_ino
        self.assertEqual(self.write(), {"created": [], "repaired": []})
        self.assertEqual(os.stat(self.dest).st_ino, ino)

    def test_link_at_the_name_is_refused_not_followed(self):
        self.write()
        os.unlink(self.dest)
        os.symlink(os.path.join(self.outside, "sentinel"), self.dest)
        with self.assertRaisesRegex(agent_dir.Refused, "symlink"):
            self.write()
        self.assert_outside_untouched()
        self.assertTrue(os.path.islink(self.dest))

    def test_link_at_the_parent_is_refused(self):
        os.symlink(self.outside, os.path.join(self.anchor, "data", "dashboard-themes"))
        with self.assertRaisesRegex(agent_dir.Refused, "not a real directory"):
            self.write()
        self.assert_outside_untouched()

    def test_cli_content_from_environment(self):
        env = dict(os.environ)
        env["AGENT_DIR_CONTENT"] = "name: readable\n"
        argv = [
            sys.executable,
            os.path.join(HERE, "agent_dir.py"),
            "--anchor",
            self.anchor,
            "--anchor-uid",
            str(ME),
            "--uid",
            str(ME),
            "--gid",
            str(MYGID),
            "--write-file",
            "data/dashboard-themes/readable.yaml",
            "--file-mode",
            "0644",
        ]
        p = subprocess.run(argv, capture_output=True, text=True, check=False, env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(json.loads(p.stdout)["changed"])
        self.assertEqual(self.read(), "name: readable\n")
        env.pop("AGENT_DIR_CONTENT")
        p = subprocess.run(argv, capture_output=True, text=True, check=False, env=env)
        self.assertEqual(p.returncode, 2)


class SeedAndRead(Base):
    """--write-file --seed writes only an absent file; --read-file reads by the walk and creates
    nothing. Both refuse a link at the name or the parent, and the read never returns the text of
    a file outside the tree."""

    def setUp(self):
        super().setUp()
        os.makedirs(os.path.join(self.anchor, "data", "profiles", "coder"))
        self.rel = "data/profiles/coder/SOUL.md"
        self.dest = os.path.join(self.anchor, self.rel)

    def seed(self, content):
        report = {"created": [], "repaired": []}
        agent_dir.write_file(
            self.anchor, ME, self.rel, content, ME, MYGID, 0o755, 0o644, report, seed=True
        )
        return report

    def read(self, rel=None):
        report = {"created": [], "repaired": []}
        agent_dir.read_file(self.anchor, ME, rel or self.rel, ME, report)
        return report

    def test_seed_creates_an_absent_file(self):
        self.assertEqual(self.seed("first\n")["created"], [self.dest])
        with open(self.dest) as fh:
            self.assertEqual(fh.read(), "first\n")

    def test_near_miss_seed_leaves_an_existing_file_alone(self):
        with open(self.dest, "w") as fh:
            fh.write("curated\n")
        self.assertEqual(self.seed("other\n"), {"created": [], "repaired": []})
        with open(self.dest) as fh:
            self.assertEqual(fh.read(), "curated\n")

    def test_seed_refuses_a_link_at_the_name(self):
        os.symlink(os.path.join(self.outside, "sentinel"), self.dest)
        with self.assertRaisesRegex(agent_dir.Refused, "symlink"):
            self.seed("x\n")
        self.assert_outside_untouched()

    def test_read_returns_the_text(self):
        with open(self.dest, "w") as fh:
            fh.write("a: 1\n")
        r = self.read()
        self.assertEqual((r["exists"], r["content"]), (True, "a: 1\n"))

    def test_near_miss_read_of_an_absent_file_creates_nothing(self):
        r = self.read("data/profiles/missing/config.yaml")
        self.assertEqual((r["exists"], r["created"]), (False, []))
        self.assertFalse(os.path.exists(os.path.join(self.anchor, "data/profiles/missing")))

    def test_read_refuses_a_link_at_the_name_and_returns_nothing(self):
        os.symlink(os.path.join(self.outside, "sentinel"), self.dest)
        report = {"created": [], "repaired": []}
        with self.assertRaisesRegex(agent_dir.Refused, "symlink"):
            agent_dir.read_file(self.anchor, ME, self.rel, ME, report)
        self.assertNotIn("content", report)

    def test_read_refuses_a_link_at_the_parent(self):
        os.rmdir(os.path.join(self.anchor, "data", "profiles", "coder"))
        os.symlink(self.outside, os.path.join(self.anchor, "data", "profiles", "coder"))
        with self.assertRaisesRegex(agent_dir.Refused, "not a real directory"):
            self.read("data/profiles/coder/sentinel")

    def test_cli_read(self):
        with open(self.dest, "w") as fh:
            fh.write("hello\n")
        argv = [sys.executable, os.path.join(HERE, "agent_dir.py"), "--anchor", self.anchor,
                "--anchor-uid", str(ME), "--uid", str(ME), "--gid", str(MYGID),
                "--read-file", self.rel]  # fmt: skip
        p = subprocess.run(argv, capture_output=True, text=True, check=False)
        self.assertEqual(p.returncode, 0, p.stderr)
        out = json.loads(p.stdout)
        self.assertEqual((out["changed"], out["exists"], out["content"]), (False, True, "hello\n"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
