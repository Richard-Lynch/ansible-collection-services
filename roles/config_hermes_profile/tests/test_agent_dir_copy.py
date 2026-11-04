#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""The role's files/agent_dir.py must be the SAME file service_hermes tests.

agent_dir.py is delivered by `ansible.builtin.script`, which ships one file, so each role that needs
the confined walk carries its own copy. Only service_hermes's copy runs the symlink-attack suite
(scripts/test_agent_dir.py); a copy that drifted from it would be an untested confinement. Re-copy
with `make sync-confined-io`. Stdlib only.

Run: python3 roles/config_hermes_profile/tests/test_agent_dir_copy.py
"""

from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROLE = os.path.dirname(HERE)
COPY = os.path.join(ROLE, "files", "agent_dir.py")
CANONICAL = os.path.join(os.path.dirname(ROLE), "service_hermes", "scripts", "agent_dir.py")


class Copy(unittest.TestCase):
    def test_the_copy_is_byte_identical_to_the_tested_one(self):
        with open(COPY, "rb") as a, open(CANONICAL, "rb") as b:
            self.assertEqual(a.read(), b.read(), f"{COPY} drifted from {CANONICAL}")


if __name__ == "__main__":
    result = unittest.main(exit=False, verbosity=1).result
    sys.exit(0 if result.wasSuccessful() else 1)
