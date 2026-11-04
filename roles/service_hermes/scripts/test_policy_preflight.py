#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the policy preflight's `venv` check.

`-I -B` does not stop a venv's own pyvenv.cfg and site-packages .pth files from being read and
executed at interpreter start, so the preflight refuses to start the gateway unless the venv the
gateway and hooks run from is owned by the expected uid and not writable by the agent. These cover
both directions: the stat predicate (each way a file can be agent-writable, and the near-misses
that must pass), the path resolution (realpaths, every .pth, a layout it cannot resolve), and the
check as run() reports it.

Stdlib only. Run: python3 roles/service_hermes/scripts/test_policy_preflight.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import policy_preflight as pf  # noqa: E402

AGENT_UID, AGENT_GID = 10000, 10000


def st(uid=0, gid=0, mode=0o644):
    return os.stat_result((0o100000 | mode, 0, 0, 1, uid, gid, 0, 0, 0, 0))


class AgentCanWrite(unittest.TestCase):
    def why(self, **kw):
        return pf.agent_can_write(st(**kw), 0, AGENT_UID, {AGENT_GID})

    def test_root_owned_not_writable_passes(self):
        for mode in (0o644, 0o755, 0o555, 0o700):
            self.assertEqual(self.why(mode=mode), "", oct(mode))

    def test_near_miss_group_write_for_a_group_the_agent_is_not_in_passes(self):
        self.assertEqual(self.why(gid=50, mode=0o664), "")

    def test_owned_by_someone_else_is_refused(self):
        self.assertIn("uid=1000", self.why(uid=1000))

    def test_owned_by_the_agent_is_refused_even_when_it_is_the_expected_uid(self):
        why = pf.agent_can_write(st(uid=AGENT_UID), AGENT_UID, AGENT_UID, {AGENT_GID})
        self.assertIn("agent's own uid", why)

    def test_world_writable_is_refused(self):
        self.assertIn("world-writable", self.why(mode=0o646))

    def test_group_writable_by_the_agent_gid_is_refused(self):
        self.assertIn("group-writable", self.why(gid=AGENT_GID, mode=0o664))
        # a supplementary group counts as much as the primary one
        why = pf.agent_can_write(st(gid=77, mode=0o775), 0, AGENT_UID, {AGENT_GID, 77})
        self.assertIn("group-writable", why)


def make_venv(root: str, sites=1, pyvenv=True) -> dict:
    venv = os.path.join(root, ".venv")
    os.makedirs(os.path.join(venv, "bin"))
    real_py = os.path.join(root, "real-python")
    with open(real_py, "w") as fh:
        fh.write("")
    os.symlink(real_py, os.path.join(venv, "bin", "python"))
    if pyvenv:
        with open(os.path.join(venv, "pyvenv.cfg"), "w") as fh:
            fh.write("home = /usr/bin\n")
    site = None
    for i in range(sites):
        site = os.path.join(venv, "lib", f"python3.{10 + i}", "site-packages")
        os.makedirs(site)
        for name in ("a.pth", "b.pth"):
            with open(os.path.join(site, name), "w") as fh:
                fh.write("")
    return {"real_py": real_py, "site": site, "venv": venv}


class VenvPaths(unittest.TestCase):
    def test_every_startup_path_resolved(self):
        with tempfile.TemporaryDirectory() as d:
            d = os.path.realpath(d)
            v = make_venv(d)
            paths, problem = pf.venv_paths(d)
            self.assertEqual(problem, "")
            self.assertEqual(
                paths,
                [
                    v["real_py"],  # the interpreter's REALPATH, not the link
                    os.path.join(v["venv"], "pyvenv.cfg"),
                    v["site"],
                    os.path.join(v["site"], "a.pth"),
                    os.path.join(v["site"], "b.pth"),
                ],
            )

    def test_a_layout_it_cannot_resolve_is_a_problem_not_a_pass(self):
        with tempfile.TemporaryDirectory() as d:
            make_venv(d, pyvenv=False)
            self.assertIn("pyvenv.cfg is missing", pf.venv_paths(d)[1])
        with tempfile.TemporaryDirectory() as d:
            make_venv(d, sites=2)
            self.assertIn("found 2", pf.venv_paths(d)[1])
        with tempfile.TemporaryDirectory() as d:
            self.assertIn("python is missing", pf.venv_paths(d)[1])


def run_venv_check(root: str, expect_uid: int, agent_uid: int):
    args = types.SimpleNamespace(
        expect_dir="/nonexistent",
        managed_dir="/nonexistent",
        consumer_root=root,
        dotenv=[],
        sha_file="/nonexistent",
        owned="/nonexistent",
        expect_uid=expect_uid,
    )
    with (
        mock.patch.object(pf.os, "getuid", return_value=agent_uid),
        mock.patch.dict(os.environ, {}, clear=False),
    ):
        os.environ.pop("HERMES_MANAGED_DIR", None)
        results = pf.run(args)
    return [r for r in results if r[1] == "venv"]


class RunReportsTheVenvCheck(unittest.TestCase):
    """The check as run() reports it. Files here are owned by the test's own uid, so that uid plays
    the expected owner and a different uid plays the agent."""

    def test_passes_when_owned_by_the_expected_uid_and_not_agent_writable(self):
        with tempfile.TemporaryDirectory() as d:
            make_venv(d)
            for dirpath, dirs, files in os.walk(d):
                for n in dirs + files:
                    p = os.path.join(dirpath, n)
                    if not os.path.islink(p):
                        os.chmod(p, 0o755 if os.path.isdir(p) else 0o644)
            res = run_venv_check(d, os.getuid(), AGENT_UID)
            self.assertEqual(len(res), 1)
            self.assertTrue(res[0][0], res[0][2])
            self.assertIn("5 venv startup paths", res[0][2])

    def test_a_world_writable_pth_is_refused_by_name(self):
        with tempfile.TemporaryDirectory() as d:
            v = make_venv(d)
            os.chmod(os.path.join(v["site"], "b.pth"), 0o666)
            ok, _name, detail = run_venv_check(d, os.getuid(), AGENT_UID)[0]
            self.assertFalse(ok)
            self.assertIn("b.pth: mode=0666 is world-writable", detail)
            self.assertNotIn("a.pth", detail)

    def test_files_owned_by_the_wrong_uid_are_refused(self):
        with tempfile.TemporaryDirectory() as d:
            make_venv(d)
            ok, _name, detail = run_venv_check(d, os.getuid() + 1, AGENT_UID)[0]
            self.assertFalse(ok)
            self.assertIn("pyvenv.cfg: uid=", detail)

    def test_files_owned_by_the_agent_are_refused(self):
        with tempfile.TemporaryDirectory() as d:
            make_venv(d)
            ok, _name, detail = run_venv_check(d, os.getuid(), os.getuid())[0]
            self.assertFalse(ok)
            self.assertIn("agent's own uid", detail)

    def test_an_unresolvable_venv_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            ok, _name, detail = run_venv_check(d, os.getuid(), AGENT_UID)[0]
            self.assertFalse(ok)
            self.assertIn("missing", detail)

    def test_near_miss_no_consumer_root_means_no_venv_check(self):
        # molecule and the controller run without --consumer-root; there is no consumer venv then.
        self.assertEqual(run_venv_check(None, 0, AGENT_UID), [])


if __name__ == "__main__":
    result = unittest.main(exit=False, verbosity=1).result
    sys.exit(0 if result.wasSuccessful() else 1)
