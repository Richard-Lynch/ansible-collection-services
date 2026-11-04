#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the gateway post_tool_call audit hook.

Runs the hook the way Hermes does -- a subprocess with the payload on stdin, in the exact wire shape
agent/shell_hooks._serialize_payload produces at v0.22.5 -- and checks the one property that
matters: a value planted in ANY argument-bearing field never reaches the audit file, while the
fields the audit exists for (tool, profile, duration, outcome) always do. Every refusal has a
near-miss that must pass.

Stdlib only. A suite runner can run it through test_hermes_policy_hook.py (the gateway hooks' suite), which loads
this module's cases; it also runs on its own.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(HERE, "hermes_audit_hook.py")
sys.path.insert(0, HERE)

import hermes_audit_hook as audit  # noqa: E402

CANARY = "AUDIT-CANARY-7f3a9c"


def payload(**over):
    """A post_tool_call payload as Hermes serialises it, carrying the canary in every free field."""
    base = {
        "hook_event_name": "post_tool_call",
        "tool_name": "terminal",
        "tool_input": {"command": f"echo {CANARY}", "nested": {"deep": [CANARY]}},
        "session_id": "20261030_041500_ab12cd",
        "cwd": f"/opt/data/{CANARY}",
        "profile": "default",
        "extra": {
            "result": json.dumps({"output": CANARY}),
            "task_id": "task-1",
            "tool_call_id": "call_abc123",
            "turn_id": "turn-1",
            "api_request_id": "req-1",
            "duration_ms": 42,
            "status": "ok",
            "error_type": None,
            "error_message": f"bad argument {CANARY}",
            "middleware_trace": [{"name": "x", "args": CANARY}],
            "unknown_future_field": CANARY,
        },
    }
    for k, v in over.items():
        if k == "extra":
            base["extra"].update(v)
        else:
            base[k] = v
    return base


def registered_argv(task_file: str, script: str) -> list[str]:
    """The hook command exactly as the role registers it (tasks/<task_file>), mapped onto this
    checkout: the image's interpreter becomes this interpreter, the policy mount this directory. A
    bare script (an `env python3` shebang) runs as the shebang would, with no flags."""
    with open(os.path.join(os.path.dirname(HERE), "tasks", task_file), encoding="utf-8") as fh:
        text = fh.read()
    m = re.search(r"'command': '([^']*/" + re.escape(script) + ")'", text)
    if m is None:
        raise AssertionError(f"no registered command for {script} in tasks/{task_file}")
    argv = shlex.split(m.group(1))
    if argv[0].endswith(".py"):
        argv = [sys.executable] + argv
    return [
        (
            sys.executable
            if a == "/opt/hermes/.venv/bin/python"
            else a.replace("/opt/hermes-policy/", HERE + "/")
        )
        for a in argv
    ]


def planted_pythonpath(tmp: str) -> dict:
    """A data/.env line the agent could write: PYTHONPATH at a json.py that answers 'allow'."""
    d = os.path.join(tmp, "planted-site")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "json.py"), "w", encoding="utf-8") as fh:
        fh.write("import sys\nsys.stdout.write('{}\\n')\nsys.exit(0)\n")
    return dict(os.environ, PYTHONPATH=d)


def run_hook(data, out_path):
    stdin = data if isinstance(data, str) else json.dumps(data)
    return subprocess.run(
        [sys.executable, HOOK, "--out", out_path],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


class AuditHookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = os.path.join(self.tmp.name, "tool-audit.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def lines(self):
        with open(self.out, encoding="ascii") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def raw(self):
        with open(self.out, encoding="ascii") as fh:
            return fh.read()

    # --- the property: value-free ---------------------------------------------------------------

    def test_canary_in_every_free_field_is_absent(self):
        r = run_hook(payload(), self.out)
        self.assertEqual(r.returncode, 0)
        self.assertNotIn(CANARY, self.raw())

    def test_canary_absent_on_error_and_blocked_paths(self):
        run_hook(payload(extra={"status": "error", "error_type": "ValueError"}), self.out)
        run_hook(payload(extra={"status": "blocked", "error_type": "tool_blocked"}), self.out)
        self.assertNotIn(CANARY, self.raw())
        self.assertEqual([r["outcome"] for r in self.lines()], ["error", "blocked"])

    def test_record_carries_only_the_allowlisted_keys(self):
        run_hook(payload(extra={"status": "error", "error_type": "ValueError"}), self.out)
        (rec,) = self.lines()
        allowed = {
            "ts",
            "event",
            "tool",
            "profile",
            "session_id",
            "tool_call_id",
            "duration_ms",
            "outcome",
            "error_type",
        }
        self.assertLessEqual(set(rec), allowed)
        for forbidden in (
            "tool_input",
            "args",
            "result",
            "error_message",
            "cwd",
            "middleware_trace",
        ):
            self.assertNotIn(forbidden, rec)

    def test_free_text_in_an_identity_field_is_replaced_not_copied(self):
        # A tool name or error_type that carries text (spaces, quotes, the canary with a space) is
        # "invalid"; the value itself must not survive.
        run_hook(
            payload(
                tool_name=f"terminal {CANARY}",
                profile=f"p {CANARY}",
                session_id=f"s {CANARY}",
                extra={"error_type": f"bad {CANARY}", "status": f"ok {CANARY}"},
            ),
            self.out,
        )
        (rec,) = self.lines()
        self.assertNotIn(CANARY, self.raw())
        self.assertEqual(rec["tool"], "invalid")
        self.assertEqual(rec["profile"], "invalid")
        self.assertEqual(rec["session_id"], "invalid")
        self.assertEqual(rec["error_type"], "invalid")
        self.assertEqual(rec["outcome"], "unknown")

    def test_non_string_identity_fields_are_not_stringified(self):
        run_hook(payload(tool_name={"x": CANARY}, profile=[CANARY]), self.out)
        (rec,) = self.lines()
        self.assertNotIn(CANARY, self.raw())
        self.assertEqual((rec["tool"], rec["profile"]), ("invalid", "invalid"))

    # --- near-misses: the fields the audit exists for always arrive ----------------------------

    def test_the_audit_fields_arrive(self):
        r = run_hook(payload(), self.out)
        self.assertEqual(r.returncode, 0)
        (rec,) = self.lines()
        self.assertEqual(rec["event"], "tool_call")
        self.assertEqual(rec["tool"], "terminal")
        self.assertEqual(rec["profile"], "default")
        self.assertEqual(rec["duration_ms"], 42)
        self.assertEqual(rec["outcome"], "ok")
        self.assertEqual(rec["session_id"], "20261030_041500_ab12cd")
        self.assertEqual(rec["tool_call_id"], "call_abc123")
        self.assertTrue(rec["ts"].endswith("Z"))
        self.assertNotIn("error_type", rec)

    def test_mcp_tool_names_pass(self):
        run_hook(payload(tool_name="mcp__hub_settings__get_setting"), self.out)
        self.assertEqual(self.lines()[0]["tool"], "mcp__hub_settings__get_setting")

    def test_missing_fields_are_unknown_not_invented(self):
        run_hook({"hook_event_name": "post_tool_call"}, self.out)
        (rec,) = self.lines()
        self.assertEqual(
            (rec["tool"], rec["profile"], rec["outcome"]),
            ("unknown", "unknown", "unknown"),
        )
        self.assertNotIn("duration_ms", rec)

    def test_bad_durations_are_dropped(self):
        for d in (True, -1, "42", 10**12):
            build = audit.build_record(payload(extra={"duration_ms": d}))
            self.assertNotIn("duration_ms", build, d)
        self.assertEqual(audit.build_record(payload(extra={"duration_ms": 7.9}))["duration_ms"], 7)

    # --- it must never get in the way ------------------------------------------------------------

    def test_prints_nothing_and_exits_zero(self):
        # Empty stdout is "no directive" to Hermes; anything else is parsed as one.
        r = run_hook(payload(), self.out)
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_garbage_stdin_is_ignored(self):
        for junk in ("", "not json", "[1,2]", "null"):
            r = run_hook(junk, self.out)
            self.assertEqual((r.returncode, r.stdout), (0, ""), junk)
        self.assertFalse(os.path.exists(self.out))

    def test_other_events_write_nothing(self):
        run_hook(payload(hook_event_name="pre_tool_call"), self.out)
        self.assertFalse(os.path.exists(self.out))

    def test_unwritable_destination_still_exits_zero(self):
        r = run_hook(payload(), os.path.join(self.tmp.name, "no", "such", "dir", "a.jsonl"))
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_rotates_past_the_cap(self):
        with open(self.out, "wb") as fh:
            fh.write(b"x" * audit.MAX_BYTES)
        run_hook(payload(), self.out)
        self.assertTrue(os.path.exists(self.out + ".1"))
        self.assertEqual(len(self.lines()), 1)

    def test_concurrent_calls_do_not_interleave(self):
        with ThreadPoolExecutor(max_workers=12) as pool:
            list(
                pool.map(
                    lambda i: run_hook(payload(tool_call_id=f"c{i}"), self.out),
                    range(24),
                )
            )
        recs = self.lines()  # json.loads on every line: an interleaved write would raise here
        self.assertEqual(len(recs), 24)

    def test_planted_pythonpath_cannot_silence_the_trail(self):
        # Run as REGISTERED (the isolated interpreter): a fake json.py on a planted PYTHONPATH that
        # exits 0 before writing is never imported, so the call is still recorded.
        argv = registered_argv("_audit_hook_leaf.yml", "hermes_audit_hook.py")
        r = subprocess.run(
            argv + ["--out", self.out],
            input=json.dumps(payload()),
            capture_output=True,
            text=True,
            env=planted_pythonpath(self.tmp.name),
            timeout=30,
            check=False,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.lines()), 1, argv)

    def test_no_environment_override(self):
        # Nothing the agent can put in its environment may redirect the trail.
        with open(HOOK, encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("os.environ", src)
        self.assertNotIn("getenv", src)


if __name__ == "__main__":
    result = unittest.main(exit=False, verbosity=1).result
    sys.exit(0 if result.wasSuccessful() else 1)
