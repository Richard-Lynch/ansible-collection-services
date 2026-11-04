# SPDX-License-Identifier: GPL-3.0-or-later
"""agent_state.py runs as ROOT over a tree the AGENT can write: no operation may follow a link.

Stdlib-only. Run: python3 roles/config_agent_state/tests/test_confinement.py
"""

# suite-requires-python: 3.12 -- agent_state.py refuses older interpreters (tarfile extraction filters)

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

SRC = Path(
    os.environ.get("AGENT_STATE_SRC")  # mutation proofs point this at a deliberately broken copy
    or Path(__file__).resolve().parents[1] / "files" / "agent_state.py"
)
# agent_state.py imports confined_io from its own directory when run; a copy loaded from elsewhere
# (the mutation proofs) finds the role's vendored one here.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "files"))
spec = importlib.util.spec_from_file_location("agent_state", SRC)
S = importlib.util.module_from_spec(spec)
spec.loader.exec_module(S)

SECRET = "OUTSIDE-SECRET\n"


def call(fn, **kw) -> dict:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(Namespace(**kw))
    return json.loads(buf.getvalue().strip().splitlines()[-1])


class Base(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        t = Path(self._t.name)
        self.root = t / "state"
        self.outside = t / "outside"
        self.root.mkdir()
        self.outside.mkdir()
        (self.outside / "secret.txt").write_text(SECRET)
        (self.root / "mem").mkdir()
        (self.root / "mem" / "real.txt").write_text("real\n")
        # planted by the agent:
        os.symlink(self.outside / "secret.txt", self.root / "mem" / "leak.txt")
        os.symlink(self.outside, self.root / "mem" / "linkdir")
        os.symlink(self.outside, self.root / "viadir")


class Confined(Base):
    def test_refuses_dotdot_and_absolute(self):
        for bad in ("../x", "mem/../../x", "/etc/passwd"):
            with self.assertRaises(S.Refused, msg=bad):
                S.confined(self.root, bad)

    def test_refuses_symlink_leaf_and_parent(self):
        with self.assertRaises(S.Refused):
            S.confined(self.root, "mem/leak.txt")
        with self.assertRaises(S.Refused):
            S.confined(self.root, "viadir/secret.txt")

    def test_refuses_link_even_when_it_points_inside_root(self):
        # resolve() alone would pass this; the per-component symlink check is what refuses it.
        os.symlink(self.root / "mem" / "real.txt", self.root / "mem" / "inner.txt")
        os.symlink(self.root / "mem", self.root / "innerdir")
        with self.assertRaises(S.Refused):
            S.confined(self.root, "mem/inner.txt")
        with self.assertRaises(S.Refused):
            S.confined(self.root, "innerdir/real.txt")

    def test_accepts_real_path_even_if_absent(self):
        self.assertEqual(S.confined(self.root, "mem/real.txt"), self.root / "mem" / "real.txt")
        S.confined(self.root, "mem/new/file")


class SyncTree(Base):
    def test_links_not_copied_and_reported(self):
        repo = Path(self._t.name) / "repo"
        repo.mkdir()
        skipped: list[str] = []
        written = S.sync_tree(self.root, repo, ["mem", "viadir"], [], skipped)
        self.assertEqual(written, ["mem/real.txt"])
        self.assertFalse((repo / "mem" / "leak.txt").exists())
        self.assertFalse((repo / "mem" / "linkdir").exists())
        self.assertFalse((repo / "viadir").exists())
        self.assertEqual(
            sorted(skipped), sorted(["mem/leak.txt", "mem/linkdir", "viadir"]), skipped
        )
        for p in repo.rglob("*"):
            if p.is_file():
                self.assertNotIn("OUTSIDE", p.read_text())

    def test_raced_leaf_swap_not_followed(self):
        # walk_files saw a regular file; the agent swaps it for a link before the copy.
        repo = Path(self._t.name) / "repo"
        repo.mkdir()
        with self.assertRaises(OSError):
            S.copy_nofollow(self.root / "mem" / "leak.txt", repo / "x")


class Rewrite(Base):
    def test_does_not_write_through_links(self):
        before = (self.outside / "secret.txt").stat().st_mtime_ns
        r = call(S.cmd_rewrite, root=str(self.root), paths=["mem", "viadir"])
        self.assertEqual(r["rewritten"], 1)
        self.assertIn("mem/leak.txt", r["skipped_symlinks"])
        self.assertIn("viadir", r["skipped_symlinks"])
        self.assertEqual((self.outside / "secret.txt").stat().st_mtime_ns, before)


class MirrorDiff(Base):
    def test_links_not_hashed_or_listed(self):
        repo = Path(self._t.name) / "repo"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        r = call(
            S.cmd_mirror_diff,
            root=str(self.root),
            repo_dir=str(repo),
            paths=["mem", "viadir"],
            exclude=[],
            skip_names_file="",
        )
        self.assertEqual(r["missing_in_mirror"], ["mem/real.txt"])
        self.assertEqual(r["differing"], [])
        self.assertIn("mem/leak.txt", r["skipped_symlinks"])
        self.assertIn("viadir", r["skipped_symlinks"])


class MindRestore(Base):
    def _remote(self, files: dict[str, str], links: dict[str, str] | None = None) -> str:
        rem = Path(self._t.name) / "remote"
        rem.mkdir()
        run = lambda *c: subprocess.run(  # noqa: E731
            ["git", "-C", str(rem), "-c", "user.name=t", "-c", "user.email=t@t", *c],
            check=True,
            capture_output=True,
        )
        run("init", "-q", "-b", "main")
        for rel, body in files.items():
            (rem / rel).parent.mkdir(parents=True, exist_ok=True)
            (rem / rel).write_text(body)
        for rel, tgt in (links or {}).items():
            (rem / rel).parent.mkdir(parents=True, exist_ok=True)
            os.symlink(tgt, rem / rel)
        run("add", ".")
        run("commit", "-q", "-m", "m")
        return str(rem)

    def _restore(self, url, paths):
        return call(
            S.cmd_mind_restore,
            root=str(self.root),
            remote_url=url,
            ref="HEAD",
            ssh_key=None,
            paths=paths,
        )

    def test_does_not_write_through_planted_leaf_link(self):
        url = self._remote({"mind/a.md": "from-mirror\n"})
        (self.root / "mind").mkdir()
        os.symlink(self.outside / "secret.txt", self.root / "mind" / "a.md")
        r = self._restore(url, ["mind"])
        self.assertEqual(r["restored"], 1)
        self.assertEqual((self.outside / "secret.txt").read_text(), SECRET)

    def test_does_not_traverse_planted_parent_link(self):
        url = self._remote({"viadir/pwn.md": "x\n"})
        with self.assertRaises(S.Refused):
            self._restore(url, ["viadir"])
        self.assertFalse((self.outside / "pwn.md").exists())

    def test_clone_symlink_skipped(self):
        url = self._remote({"mind/ok.md": "ok\n"}, {"mind/evil.md": "/etc/passwd"})
        r = self._restore(url, ["mind"])
        self.assertEqual(r["restored"], 1)
        self.assertIn("mind/evil.md", r["skipped_symlinks"])
        self.assertFalse((self.root / "mind" / "evil.md").exists())

    def test_refuses_dotdot_path(self):
        url = self._remote({"mind/ok.md": "ok\n"})
        with self.assertRaises(S.Refused):
            self._restore(url, ["../outside"])


class MirrorOnRemoteHead(unittest.TestCase):
    """A commit pushed to the mirror from elsewhere (a seed commit) must not wedge the mirror."""

    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        t = Path(self._t.name)
        self.root, self.repo, self.remote, self.other = (
            t / "state",
            t / "repo",
            t / "remote.git",
            t / "other",
        )
        (self.root / "mem").mkdir(parents=True)
        (self.root / "mem" / "a.md").write_text("one\n")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.remote)], check=True)

    def _mirror(self) -> dict:
        return call(
            S.cmd_mirror,
            root=str(self.root),
            repo_dir=str(self.repo),
            remote_url=str(self.remote),
            branch="main",
            paths=["mem"],
            exclude=[],
            skip_names_file="",
            config_file=None,
            mask_pattern=[],
            scan_pattern=[],
            ssh_key=None,
            product="hermes",
            author_name="t",
            author_email="t@localhost",
            metrics_file=None,
        )

    def _seed_from_elsewhere(self, body: str) -> str:
        g = ["git", "-C", str(self.other)]
        subprocess.run(["git", "clone", "-q", str(self.remote), str(self.other)], check=True)
        (self.other / "mem" / "a.md").write_text(body)
        subprocess.run(
            g + ["-c", "user.name=o", "-c", "user.email=o@x", "commit", "-qam", "seed"], check=True
        )
        subprocess.run(g + ["push", "-q", "origin", "HEAD:main"], check=True)
        return subprocess.run(
            g + ["rev-parse", "HEAD"], capture_output=True, text=True
        ).stdout.strip()

    def _remote_log(self) -> list[str]:
        r = subprocess.run(
            ["git", "-C", str(self.remote), "log", "--format=%H", "main"],
            capture_output=True,
            text=True,
        )
        return r.stdout.split()

    def test_mirrors_on_top_of_a_seed_pushed_from_elsewhere(self):
        # MUST PASS: before the fetch, this second mirror died on a non-fast-forward push.
        self._mirror()
        seed = self._seed_from_elsewhere("seeded\n")
        (self.root / "mem" / "a.md").write_text("learned since\n")
        self._mirror()
        log = self._remote_log()
        self.assertEqual(log[1], seed)  # the seed is the parent, not discarded
        show = subprocess.run(
            ["git", "-C", str(self.remote), "show", "main:mem/a.md"], capture_output=True, text=True
        )
        self.assertEqual(show.stdout, "learned since\n")

    def test_live_equal_to_the_seed_commits_nothing(self):
        # NEAR-MISS: the restore wrote the seed to the live tree, so the mirror has nothing to add.
        self._mirror()
        seed = self._seed_from_elsewhere("seeded\n")
        (self.root / "mem" / "a.md").write_text("seeded\n")
        self._mirror()
        self.assertEqual(self._remote_log()[0], seed)


class Excluded(unittest.TestCase):
    def test_a_multi_segment_directory_pattern_excludes_what_is_under_it(self):
        # MUST MATCH: the segment-only test never did, so these two excluded nothing.
        self.assertTrue(S.excluded("home/.pki/nssdb/cert9.db", ["home/.pki/"]))
        self.assertTrue(
            S.excluded("learning/recall-scratch/memory_store.db", ["learning/recall-scratch/"])
        )
        self.assertTrue(S.excluded("home/.pki/x", ["home/.pki/"]))  # the walk's directory probe

    def test_near_misses_stay_included(self):
        self.assertFalse(S.excluded("home/.pkix/cert9.db", ["home/.pki/"]))
        self.assertFalse(S.excluded("other/home/.pki/cert9.db", ["home/.pki/"]))
        self.assertFalse(S.excluded("learning/probes/latest.json", ["learning/recall-scratch/"]))
        # single-segment patterns keep matching anywhere, as before
        self.assertTrue(S.excluded("a/logs/x.log", ["logs/"]))
        self.assertFalse(S.excluded("logs", ["logs/"]))


class RacedParentSwap(Base):
    """The agent swaps a directory for a link AFTER walk_files listed it and BEFORE root uses the
    file. A path check (confined(), is_symlink()) is already over by then; only the fd walk
    (confined_io) sees the swap. Each case fails on the path-based implementation it replaced."""

    def setUp(self):
        super().setUp()
        (self.outside / "real.txt").write_text(SECRET)
        self.before = {p.name: p.stat().st_mtime_ns for p in self.outside.iterdir()}
        real_walk = self.real_walk = S.walk_files

        def walk_then_swap(root, rel, skipped):
            found = real_walk(root, rel, skipped)
            # (mind-restore walks the CLONE; the swap is still in the live tree it writes to)
            if not (self.root / "mem").is_symlink():
                os.rename(self.root / "mem", self.root / "mem.orig")
                os.symlink(self.outside, self.root / "mem")
            return found

        patcher = mock.patch.object(S, "walk_files", walk_then_swap)
        patcher.start()
        self.addCleanup(patcher.stop)

    def outside_untouched(self):
        self.assertEqual(sorted(p.name for p in self.outside.iterdir()), ["real.txt", "secret.txt"])
        for p in self.outside.iterdir():
            self.assertEqual(p.read_text(), SECRET)
            self.assertEqual(p.stat().st_mtime_ns, self.before[p.name])

    def test_sync_tree_does_not_read_through_the_swap(self):
        repo = Path(self._t.name) / "repo"
        repo.mkdir()
        with self.assertRaises(S.Refused):
            S.sync_tree(self.root, repo, ["mem"], [], [])
        for p in repo.rglob("*"):
            if p.is_file():
                self.assertNotIn("OUTSIDE", p.read_text())

    def test_rewrite_does_not_open_through_the_swap(self):
        with self.assertRaises(S.Refused):
            call(S.cmd_rewrite, root=str(self.root), paths=["mem"])
        self.outside_untouched()

    def test_mind_restore_does_not_write_through_a_swap_after_the_check(self):
        # The path-based restore checked the destination's directory with confined() and then
        # opened the file by NAME: a swap after the last check redirected the write. The fd walk
        # has no such gap (no check precedes the use), so the swap below never even fires for it;
        # either way the outcome required is the same: nothing outside the tree changes.
        url = MindRestore._remote(self, {"mem/real.txt": "from-mirror\n"})
        real_confined, calls = S.confined, []

        def confined_then_swap(root, rel):
            got = real_confined(root, rel)
            if Path(root) == self.root:
                calls.append(rel)
            if len(calls) == 2 and not (self.root / "mem").is_symlink():
                os.rename(self.root / "mem", self.root / "mem.orig")
                os.symlink(self.outside, self.root / "mem")
            return got

        with (
            mock.patch.object(S, "walk_files", self.real_walk),
            mock.patch.object(S, "confined", confined_then_swap),
        ):
            try:
                MindRestore._restore(self, url, ["mem"])
            except S.Refused:
                pass
        self.outside_untouched()


if __name__ == "__main__":
    unittest.main()
