#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for hermes_health_textfile.py's reading of `hermes hooks list`.

The hooks are registered as an interpreter, its flags and the script, so the command the probe
compares is the WHOLE command: comparing the first word would count the interpreter, the same for
every hook, and report every expected hook registered once any one is. Stdlib only.

Run: python3 roles/service_hermes/scripts/test_hermes_health_textfile.py
"""

from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import hermes_health_textfile as h  # noqa: E402

PY = "/opt/hermes/.venv/bin/python -I -B"
POLICY = f"{PY} /opt/hermes-policy/hermes_policy_hook.py"
AUDIT = f"{PY} /opt/hermes-policy/hermes_audit_hook.py"
GATE = f"{PY} /opt/hermes-policy/hub_gate.py"

# The shape `hermes hooks list` prints (measured on the gateway, commands made generic).
LISTING = f"""Configured shell hooks (3 total):

  [post_tool_call]
    - {AUDIT} (timeout=5s, ✓ allowed)
      approved_at: 2026-01-01T00:00:00Z

  [pre_tool_call]
    - {POLICY} matcher='^(write_file|patch|terminal)$' (timeout=5s, ✓ allowed)
      approved_at: 2026-01-01T00:00:00Z
    - {GATE} matcher='terminal|.*mail.*' (timeout=10s, ✗ not allowlisted)
"""


class AllowedCommands(unittest.TestCase):
    def test_whole_commands_with_and_without_a_matcher(self):
        self.assertEqual(h.allowed_commands(LISTING), {AUDIT, POLICY})

    def test_near_miss_a_not_allowlisted_hook_is_not_counted(self):
        self.assertNotIn(GATE, h.allowed_commands(LISTING))

    def test_the_interpreter_alone_is_never_a_match(self):
        self.assertNotIn("/opt/hermes/.venv/bin/python", h.allowed_commands(LISTING))

    def test_a_bare_script_command_still_parses(self):
        line = "    - /opt/hermes-policy/hermes_audit_hook.py (timeout=5s, ✓ allowed)"
        self.assertEqual(h.allowed_commands(line), {"/opt/hermes-policy/hermes_audit_hook.py"})


if __name__ == "__main__":
    result = unittest.main(exit=False, verbosity=1).result
    sys.exit(0 if result.wasSuccessful() else 1)
