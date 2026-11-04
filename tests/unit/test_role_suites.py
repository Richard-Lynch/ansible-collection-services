# SPDX-License-Identifier: GPL-3.0-or-later
"""Run every role's own test suite under `ansible-test units`.

The roles' helper programs (hooks, gates, daemons, backup and learning jobs) carry their own
stdlib test suites beside them, under roles/<role>/{scripts,files,tests}/test_*.py. ansible-test
collects tests/unit only, so without this file those suites would ship and never run in CI: a
suite CI does not run is worse than none, because it reads as coverage.

Each suite is run as its own process, the way it is written to be run, and must exit 0. The set is
DISCOVERED, so a suite added to a role is run without editing this file; a role tree with no suite
at all fails, so a broken glob cannot pass by finding nothing.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SUITES = sorted(
    p
    for pattern in (
        "roles/*/scripts/test_*.py",
        "roles/*/files/test_*.py",
        "roles/*/tests/test_*.py",
    )
    for p in ROOT.glob(pattern)
)


def test_suites_were_found():
    assert SUITES, f"no role test suite found under {ROOT}/roles"


@pytest.mark.parametrize("suite", SUITES, ids=[str(p.relative_to(ROOT)) for p in SUITES])
def test_role_suite(suite):
    proc = subprocess.run(
        [sys.executable, str(suite)],
        cwd=str(suite.parent),
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    assert proc.returncode == 0, (
        f"{suite.relative_to(ROOT)} exited {proc.returncode}\n"
        f"--- stdout (tail) ---\n{proc.stdout[-4000:]}\n--- stderr (tail) ---\n{proc.stderr[-4000:]}"
    )
