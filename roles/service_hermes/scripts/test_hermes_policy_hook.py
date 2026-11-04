#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the gateway pre_tool_call policy hook.

Runs the hook the way Hermes does -- a subprocess with the payload on stdin -- and checks the
wire shape (`{"action": "block", "message": ...}` + exit 2 to block, `{}` + exit 0 to allow), plus
the decision table directly. Every refusal has a near-miss that must pass, and a shared key table
asserts (in the settings service's own suite, which ships that policy) the hook and the service
agree on which keys are live-writable.

Stdlib only.
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

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(HERE, "hermes_policy_hook.py")

OWNED = {
    "owned": [
        {"key": "approvals.mode", "tier": "C", "value": "smart"},
        {"key": "hooks_auto_accept", "tier": "C", "value": True},
        {"key": "providers.example-gateway.base_url", "tier": "B", "value": "x"},
        {
            "key": "auxiliary.background_review.model",
            "tier": "A-range",
            "value": "cheap-model",
            "allowed": ["cheap-model"],
        },
    ]
}

TMP = tempfile.mkdtemp()
DATA = os.path.join(TMP, "data")
os.makedirs(os.path.join(DATA, "profiles", "coder"), exist_ok=True)
os.makedirs(os.path.join(DATA, "workspace"), exist_ok=True)
OWNED_PATH = os.path.join(TMP, "owned-keys.json")
with open(OWNED_PATH, "w") as fh:
    json.dump(OWNED, fh)
sys.path.insert(0, HERE)
import hermes_policy_hook as hook  # noqa: E402

hook.OWNED, hook.DATA = OWNED_PATH, DATA


def run(payload, env_extra=None):
    env = dict(os.environ, **(env_extra or {}))
    p = subprocess.run(
        [sys.executable, HOOK, "--owned", OWNED_PATH, "--data-root", DATA],
        input=payload if isinstance(payload, str) else json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    return p.returncode, json.loads(p.stdout or "null")


def call(tool, **args):
    return {
        "hook_event_name": "pre_tool_call",
        "tool_name": tool,
        "tool_input": args,
        "cwd": DATA,
        "profile": "default",
        "session_id": "s",
        "extra": {},
    }


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


class Wire(unittest.TestCase):
    def test_block_shape_and_exit_2(self):
        rc, out = run(call("write_file", path=f"{DATA}/.env", content="X=1"))
        self.assertEqual(rc, 2)
        self.assertEqual(out["action"], "block")
        self.assertIn("protected", out["message"])

    def test_allow_shape(self):
        rc, out = run(call("write_file", path=f"{DATA}/workspace/notes.md", content="hi"))
        self.assertEqual((rc, out), (0, {}))

    def test_garbage_payload_fails_closed(self):
        rc, out = run("not json")
        self.assertEqual(rc, 2)
        self.assertEqual(out["action"], "block")

    def test_settings_writes_ask_reads_do_not(self):
        rc, out = run(call("mcp__hub_settings__set_setting", key="display.x", value=1))
        self.assertEqual((rc, out["action"]), (0, "approve"))
        self.assertIn("display.x", out["rule_key"])
        rc, out = run(call("mcp__hub_settings__get_setting", key="display.x"))
        self.assertEqual((rc, out), (0, {}))  # near-miss: a read is free
        rc, out = run(call("mcp__other__set_setting", key="x"))
        self.assertEqual((rc, out), (0, {}))  # near-miss: another server's tool of the same name

    def test_unrelated_tool_allowed(self):
        self.assertEqual(run(call("web_search", query="x"))[0], 0)


class Paths(unittest.TestCase):
    def blocked(self, tool, **a):
        return bool(hook.decide(call(tool, **a)))

    def test_protected_files_in_every_profile(self):
        for p in (
            ".env",
            ".env.local",
            "config.yaml",
            "plugins/x/plugin.yaml",
            "hooks/a.py",
            "webhook_subscriptions.json",
            "shell-hooks-allowlist.json",
            "policy/owned-keys.json",
            "profiles/coder/.env",
            "profiles/coder/config.yaml",
            "profiles/coder/hooks/x.sh",
        ):
            self.assertTrue(self.blocked("write_file", path=f"{DATA}/{p}"), p)
        self.assertTrue(self.blocked("write_file", path="/opt/hermes-policy/hermes_policy_hook.py"))
        self.assertTrue(self.blocked("write_file", path="/etc/hermes/config.yaml"))

    def test_relative_and_dotdot_resolve(self):
        self.assertTrue(self.blocked("write_file", path="workspace/../.env"))
        self.assertTrue(self.blocked("write_file", path="config.yaml"))

    def test_symlink_to_protected_is_protected(self):
        link = os.path.join(DATA, "workspace", "innocent.txt")
        if not os.path.lexists(link):
            os.symlink(os.path.join(DATA, ".env"), link)
        self.assertTrue(self.blocked("write_file", path=link))

    def test_patch_file_headers(self):
        self.assertTrue(
            self.blocked(
                "patch",
                patch="*** Begin Patch\n*** Update File: config.yaml\n@@\n-a\n+b\n",
            )
        )
        self.assertFalse(
            self.blocked("patch", patch="*** Begin Patch\n*** Update File: workspace/x.py\n")
        )

    def test_near_misses_pass(self):
        for p in (
            "workspace/env.md",
            "workspace/config.yaml.bak",
            "skills/my-skill/SKILL.md",
            "memories/MEMORY.md",
            "profiles/coder/skills/x/SKILL.md",
            "profiles/coder/workspace/config.yaml",
        ):
            self.assertFalse(self.blocked("write_file", path=f"{DATA}/{p}"), p)
        self.assertFalse(self.blocked("write_file", path="/tmp/config.yaml"))

    def test_the_image_venv_and_code_root_are_protected(self):
        # pyvenv.cfg and site-packages .pth files run in every interpreter, -I -B or not.
        for p in (
            "/opt/hermes/.venv/pyvenv.cfg",
            "/opt/hermes/.venv/lib/python3.12/site-packages/evil.pth",
            "/opt/hermes/.venv/bin/python",
            "/opt/hermes/hermes_yaml.py",
            "/opt/hermes/../hermes/.venv/pyvenv.cfg",
        ):
            self.assertTrue(self.blocked("write_file", path=p), p)
        self.assertTrue(
            self.blocked(
                "patch",
                patch="*** Begin Patch\n*** Add File: /opt/hermes/.venv/x.pth\n",
            )
        )
        self.assertTrue(
            Shell().blocked(
                "echo 'import os' > /opt/hermes/.venv/lib/python3.12/site-packages/a.pth"
            )
        )

    def test_near_miss_sibling_names_and_reads_of_the_code_root_pass(self):
        for p in (
            "/opt/hermes-data/x",
            "/opt/hermesx/y",
            "/opt/data/workspace/notes.md",
        ):
            self.assertFalse(self.blocked("write_file", path=p), p)
        for cmd in ("cat /opt/hermes/pyproject.toml", "ls /opt/hermes/.venv/lib"):
            self.assertFalse(Shell().blocked(cmd), cmd)

    def test_a_data_root_inside_a_protected_root_keeps_its_own_rules(self):
        # The host layout (/opt/hermes/data): the agent's ordinary data writes must still pass and
        # the DATA rules (.env, config.yaml...) must still bite, with no blanket refusal from the
        # /opt/hermes root that contains it.
        saved = hook.DATA
        hook.DATA = "/opt/hermes/data"
        try:
            self.assertFalse(self.blocked("write_file", path="/opt/hermes/data/workspace/a.md"))
            self.assertTrue(self.blocked("write_file", path="/opt/hermes/data/.env"))
            self.assertTrue(
                self.blocked("write_file", path="/opt/hermes/data/profiles/c/config.yaml")
            )
            self.assertTrue(self.blocked("write_file", path="/opt/hermes/.venv/pyvenv.cfg"))
            self.assertTrue(self.blocked("write_file", path="/opt/hermes/data/../.venv/x.pth"))
        finally:
            hook.DATA = saved

    def test_a_link_in_data_to_the_venv_is_the_venv(self):
        d = tempfile.mkdtemp()
        root = os.path.join(d, "code")
        os.makedirs(os.path.join(root, ".venv"))
        saved_roots, saved_data = hook.ABS_PROTECTED, hook.DATA
        hook.ABS_PROTECTED, hook.DATA = (root,), os.path.join(d, "data")
        os.makedirs(hook.DATA)
        link = os.path.join(hook.DATA, "innocent.cfg")
        os.symlink(os.path.join(root, ".venv", "pyvenv.cfg"), link)
        try:
            self.assertTrue(self.blocked("write_file", path=link))
            self.assertFalse(self.blocked("write_file", path=os.path.join(hook.DATA, "plain.txt")))
        finally:
            hook.ABS_PROTECTED, hook.DATA = saved_roots, saved_data


class Shell(unittest.TestCase):
    def blocked(self, cmd, tool="terminal"):
        key = "code" if tool == "execute_code" else "command"
        return hook.decide(call(tool, **{key: cmd}))

    def test_config_set_owned_and_reserved_blocked(self):
        for cmd in (
            "hermes config set approvals.mode off",
            "hermes -p coder config set approvals.mode off",
            "cd /x && hermes config set platform_toolsets.slack web",
            "hermes config set OPENAI_API_KEY sk-x",
            "hermes config set providers.example-gateway.base_url http://evil/v1",
            "hermes config set auxiliary.background_review.model gpt-5",  # A-range: the service decides bounds
            "hermes config unset hooks_auto_accept",
            "hermes config edit",
            "hermes hooks revoke /opt/hermes-policy/hermes_policy_hook.py",
            "hermes plugins install evil",
            "hermes mcp add evil --url http://x",
        ):
            self.assertTrue(self.blocked(cmd), cmd)

    def test_config_set_agent_tier_passes(self):
        for cmd in (
            "hermes config set display.compact true",
            "hermes -p coder config set compression.enabled true",
            "hermes config get approvals.mode",
            "hermes hooks list",
            "hermes mcp list",
            "echo hermes config",
        ):
            self.assertFalse(self.blocked(cmd), cmd)

    def test_shell_write_to_protected_blocked(self):
        for cmd in (
            f"echo X=1 >> {DATA}/.env",
            f"sed -i s/a/b/ {DATA}/config.yaml",
            f"cp /tmp/x {DATA}/profiles/coder/.env",
            "rm -rf /opt/hermes-policy",
            f"ln -sf /tmp/h {DATA}/hooks/h.py",
        ):
            self.assertTrue(self.blocked(cmd), cmd)
        self.assertTrue(self.blocked(f"open('{DATA}/.env','a').write('X=1')", tool="execute_code"))

    def test_shell_reads_pass(self):
        for cmd in (
            f"cat {DATA}/config.yaml",
            f"ls {DATA}/plugins",
            "grep -c x /tmp/a > /tmp/b",
        ):
            self.assertFalse(self.blocked(cmd), cmd)

    def test_unreadable_owned_list_blocks_config_set_only(self):
        saved = hook.OWNED
        hook.OWNED = os.path.join(TMP, "missing.json")
        try:
            self.assertTrue(hook.hermes_cli_problem("hermes config set display.x 1", None))
            # the path rules still work without it
            self.assertFalse(self.blocked("ls /tmp"))
        finally:
            hook.OWNED = saved


class NoEnvironmentOverride(unittest.TestCase):
    """Hermes loads the agent-writable data/.env into the hook's environment with override=True,
    so nothing the hook protects may be re-pointed through an environment variable."""

    def test_source_reads_no_environment(self):
        with open(HOOK, encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("os.environ", src)
        self.assertNotIn("getenv", src)

    def test_planted_variables_change_nothing(self):
        # A planted owned-keys list naming nothing and a planted data root elsewhere: with an
        # override the .env write below would be allowed; without one it is still blocked.
        empty = os.path.join(TMP, "planted-owned.json")
        with open(empty, "w") as fh:
            json.dump({"owned": []}, fh)
        planted = {
            "HERMES_POLICY_OWNED": empty,
            "HERMES_POLICY_DATA_ROOT": "/nonexistent",
        }
        p = subprocess.run(
            [sys.executable, HOOK],
            input=json.dumps(call("write_file", path="/opt/data/.env", content="X=1")),
            capture_output=True,
            text=True,
            env=dict(os.environ, **planted),
            timeout=10,
        )
        self.assertEqual(p.returncode, 2, p.stdout)

    def test_planted_pythonpath_cannot_load_a_fake_module(self):
        # Run as REGISTERED: the isolated interpreter ignores PYTHONPATH, so the protected write is
        # still blocked. Registered as a bare `env python3` script, the fake json.py answers allow.
        argv = registered_argv("_config_merge.yml", "hermes_policy_hook.py")
        p = subprocess.run(
            argv + ["--owned", OWNED_PATH, "--data-root", DATA],
            input=json.dumps(call("write_file", path=f"{DATA}/.env", content="X=1")),
            capture_output=True,
            text=True,
            env=planted_pythonpath(TMP),
            timeout=10,
        )
        self.assertEqual(p.returncode, 2, (argv, p.stdout, p.stderr))

    def test_near_miss_an_unrelated_variable_changes_nothing(self):
        argv = registered_argv("_config_merge.yml", "hermes_policy_hook.py")
        p = subprocess.run(
            argv + ["--owned", OWNED_PATH, "--data-root", DATA],
            input=json.dumps(call("write_file", path=f"{DATA}/workspace/n.md", content="hi")),
            capture_output=True,
            text=True,
            env=dict(os.environ, GH_TOKEN="x"),
            timeout=10,
        )
        self.assertEqual((p.returncode, json.loads(p.stdout)), (0, {}), p.stderr)

    def test_near_miss_the_test_flags_do_repoint(self):
        # The same write under the flagged data root is blocked, and outside it is allowed: the
        # flags are honoured, so the test above passes because the env is ignored, not by accident.
        self.assertEqual(run(call("write_file", path=f"{DATA}/.env", content="X=1"))[0], 2)
        self.assertEqual(run(call("write_file", path=f"{TMP}/elsewhere.txt", content="X"))[0], 0)


# The post_tool_call audit hook is the other gateway shell hook, and its cases run HERE so one
# runner line for this suite covers them (test_hermes_audit_hook.py also runs on its own).
from test_hermes_audit_hook import AuditHookTests  # noqa: E402,F401

if __name__ == "__main__":
    result = unittest.main(exit=False, verbosity=1).result
    sys.exit(0 if result.wasSuccessful() else 1)
