#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for hub_gate.py: the send gate and the protected-path rule.

Stdlib-only. Every case that must escalate or block is paired with a near-miss
that must pass, because a gate that always asks and one that never asks both "work" on one case.
The process-level cases run the real script with a real stdin, because exit code 2 IS the
fail-closed contract with Hermes.

Run: python3 roles/service_hermes/scripts/test_hub_gate.py
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
sys.path.insert(0, HERE)

import hub_gate  # noqa: E402


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


CFG = {"allowed": {"owner@example.com"}, "protected": ["webhook_subscriptions.json"]}


def _term(cmd: str) -> dict:
    return {
        "hook_event_name": "pre_tool_call",
        "tool_name": "terminal",
        "tool_input": {"command": cmd},
    }


class SendGate(unittest.TestCase):
    def test_gmail_send_to_a_new_recipient_escalates_keyed_on_the_recipient(self):
        d = hub_gate.decide(
            _term('$GAPI gmail send --to stranger@corp.com --subject "Hi" --body "x"'),
            CFG,
        )
        self.assertEqual(d["action"], "approve")
        self.assertEqual(d["rule_key"], "send-gate:stranger@corp.com")
        self.assertIn("stranger@corp.com", d["message"])

    def test_gmail_send_to_the_owner_only_passes(self):
        d = hub_gate.decide(
            _term("$GAPI gmail send --to owner@example.com --subject s --body b"), CFG
        )
        self.assertEqual(d, {})

    def test_one_stranger_among_allowed_recipients_still_escalates(self):
        d = hub_gate.decide(
            _term("gapi gmail send --to owner@example.com --cc x@y.org --subject s"),
            CFG,
        )
        self.assertEqual(d["action"], "approve")
        self.assertIn("x@y.org", d["rule_key"])

    def test_reply_and_forward_and_drafts_send_are_sends(self):
        for cmd in (
            "gapi gmail reply --message-id 1 --body ok",
            "gapi gmail forward --to a@b.co --message-id 1",
            "gapi gmail drafts send --id r-1",
            "himalaya message send < /tmp/m.eml",
            "himalaya -a work message reply 42",
            "printf 'x' | sendmail -t",
            "swaks -t a@b.co --server smtp.b.co",
            "echo hi | mail -s subject a@b.co",
            "python3 -c 'import smtplib; s=smtplib.SMTP(\"h\")'",
            "curl --url smtps://smtp.gmail.com:465 --mail-rcpt a@b.co",
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(hub_gate.decide(_term(cmd), CFG).get("action"), "approve")

    def test_reads_on_the_narrow_allowlist_are_not_sends(self):
        for cmd in (
            "gapi gmail search 'is:unread newer_than:1d'",
            '$GAPI gmail search "from:boss@company.com newer_than:1d" --max 10',
            "gapi gmail get --id 1",
            "$GAPI gmail labels",
            "$GAPI gmail modify 18c --add-labels Label_7 --remove-labels UNREAD",
            "python3 /opt/data/skills/google-workspace/scripts/google_api.py gmail get 18c",
            "himalaya message read 42",
            "himalaya -a work envelope list",
            "grep -c sendmailer notes.txt",
            "gapi gmail search is:unread | head -5",
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(hub_gate.decide(_term(cmd), CFG), {})

    def test_commands_that_never_mention_mail_pass(self):
        for cmd in (
            "ls -la",
            "git log --oneline -5",
            "python3 -c 'print(1 + 1)'",
            "curl -s https://api.github.com/repos/x/y/issues",
            "echo 'call me later' > todo.txt",
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(hub_gate.decide(_term(cmd), CFG), {})

    def test_a_mention_off_the_allowlist_asks_even_when_it_is_not_a_send(self):
        # The cost of the broad rule, pinned so it is a decision and not an accident: these ask.
        for cmd in (
            "echo 'mail me later' > todo.txt",
            "gapi gmail draft create --to stranger@corp.com --subject s --body b",
            "gapi gmail labels apply --id 1 --label triage/reply",
            "grep smtp /etc/postfix/main.cf > /tmp/out",
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(hub_gate.decide(_term(cmd), CFG).get("action"), "approve")


class SendGateBypasses(unittest.TestCase):
    """Each of these defeats a plain regex over the raw command. Every one MUST ask."""

    def _asks(self, cmd, tool="terminal"):
        p = {
            "tool_name": tool,
            "tool_input": {"command" if tool != "execute_code" else "code": cmd},
        }
        return hub_gate.decide(p, CFG).get("action") == "approve"

    def test_quoted_and_concatenated_verbs_ask(self):
        for cmd in (
            "gapi gmail 'send' --to stranger@corp.com --subject s",
            'gapi gm"ail" se""nd --to stranger@corp.com',
            "gapi 'gm''ail' send --to stranger@corp.com",
            "gapi gm\\ail s\\end --to stranger@corp.com",
            "gapi gm${EMPTY}ail send --to stranger@corp.com",
            "sendmail -t < /tmp/m.eml",
            "/usr/sbin/'send'mail stranger@corp.com < /tmp/m",
            "env X=1 msmtp stranger@corp.com < m.eml",
            "printf x | mu''tt -s hi stranger@corp.com",
            "$'\\x73endmail' stranger@corp.com < m",
            "s=sendmail; $s stranger@corp.com < m",
        ):
            with self.subTest(cmd=cmd):
                self.assertTrue(self._asks(cmd))

    def test_python_smtplib_in_every_spelling_asks(self):
        for cmd in (
            'python3 -c \'import smtplib; smtplib.SMTP("h").sendmail("a", ["b@c.de"], "x")\'',
            "python3 -c \"__import__('smt'+'plib').SMTP('h')\"",
            "python3 -c \"__import__('smt' 'plib').SMTP('h')\"",
            "python3 -c \"__import__('\\x73mtplib').SMTP('h')\"",
            "python3 - <<'EOF'\nimport smtplib\nsmtplib.SMTP('h')\nEOF",
        ):
            with self.subTest(cmd=cmd):
                self.assertTrue(self._asks(cmd))
        for code in (
            "import smtplib\nsmtplib.SMTP('h').sendmail('a@b.co')",
            "m = __import__('smt' + 'plib')",
            "import importlib; importlib.import_module('s' 'mtp' 'lib')",
            "import email.message",
        ):
            with self.subTest(code=code):
                self.assertTrue(self._asks(code, tool="execute_code"))

    def test_curl_to_smtp_or_a_mail_api_asks(self):
        for cmd in (
            "curl --url 'smtps://smtp.gmail.com:465' --mail-rcpt a@b.co -T m.eml",
            "curl smtp://relay:25 --mail-from x@y.z --mail-rcpt a@b.co --upload-file m",
            "curl -X POST https://api.sendgrid.com/v3/mail/send -d @body.json",
            "curl https://api.sparkpost.com/api/v1/transmissions -d @b.json",
            "curl -X POST 'https://gmail.googleapis.com/gmail/v1/users/me/messages/send' -d @r.json",
            "cat m.eml > /dev/tcp/relay/25",
        ):
            with self.subTest(cmd=cmd):
                self.assertTrue(self._asks(cmd))

    def test_an_allowlisted_read_chained_with_a_send_asks(self):
        for cmd in (
            "gapi gmail search x; gapi gmail send --to stranger@corp.com",
            "gapi gmail search x && sendmail stranger@corp.com < m",
            "gapi gmail search $(sendmail stranger@corp.com < m)",
            "gapi gmail search `sendmail stranger@corp.com < m`",
            "cat notes.txt | sendmail -t",
            "gapi gmail search x\nsendmail stranger@corp.com < m",
        ):
            with self.subTest(cmd=cmd):
                self.assertTrue(self._asks(cmd))


class Recipients(unittest.TestCase):
    """The recipient set is the UNION of the flags and every address in every form of the text."""

    def test_an_address_outside_the_flags_is_still_a_recipient(self):
        d = hub_gate.decide(
            _term("gapi gmail send --to owner@example.com --body 'cc stranger@corp.com'"),
            CFG,
        )
        self.assertEqual(d["action"], "approve")
        self.assertIn("stranger@corp.com", d["rule_key"])

    def test_a_quoted_or_split_address_is_reassembled(self):
        for cmd in (
            "gapi gmail send --to owner@example.com --bcc 'stran''ger@corp.com'",
            "gapi gmail send --to owner@example.com --bcc \"stranger@\"'corp.com'",
        ):
            with self.subTest(cmd=cmd):
                d = hub_gate.decide(_term(cmd), CFG)
                self.assertEqual(d["action"], "approve")
                self.assertIn("stranger@corp.com", d["rule_key"])

    def test_a_recipient_the_gate_cannot_see_asks_even_beside_the_owner(self):
        for cmd in (
            'gapi gmail send --to owner@example.com --bcc "$X"',
            "gapi gmail send --to owner@example.com --bcc $(cat /tmp/who)",
            "gapi gmail send --to owner@example.com --bcc team",
            "gapi gmail send --to owner@example.com < /tmp/more",
            "sendmail owner@example.com < /tmp/m.eml",
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(hub_gate.decide(_term(cmd), CFG).get("action"), "approve")

    def test_execute_code_to_the_owner_still_asks(self):
        p = {
            "tool_name": "execute_code",
            "tool_input": {
                "code": "import smtplib\nsmtplib.SMTP('h').sendmail('owner@example.com')"
            },
        }
        self.assertEqual(hub_gate.decide(p, CFG)["action"], "approve")

    def test_a_plain_send_to_the_owner_in_any_form_passes(self):
        for cmd in (
            "$GAPI gmail send --to owner@example.com --subject s --body b",
            "$GAPI gmail send --to=Owner@Example.com --subject 'Daily digest' --body ok",
            "$GAPI gmail send --to '\"Me\" <owner@example.com>' --subject s --body b",
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(hub_gate.decide(_term(cmd), CFG), {})

    def test_an_unreadable_recipient_asks_rather_than_passes(self):
        d = hub_gate.decide(_term("himalaya message send < /tmp/m.eml"), CFG)
        self.assertEqual(d["rule_key"], "send-gate:unparsed")

    def test_execute_code_smtplib_is_a_send(self):
        p = {
            "tool_name": "execute_code",
            "tool_input": {"code": "import smtplib\nsmtplib.SMTP('h').sendmail('a@b.co')"},
        }
        self.assertEqual(hub_gate.decide(p, CFG)["action"], "approve")

    def test_a_send_tool_by_name_is_gated_even_without_a_command(self):
        p = {
            "tool_name": "send_message",
            "tool_input": {"target": "email:x@y.org", "message": "hi"},
        }
        self.assertEqual(hub_gate.decide(p, CFG)["rule_key"], "send-gate:x@y.org")

    def test_a_send_tool_to_the_owner_passes(self):
        p = {
            "tool_name": "send_message",
            "tool_input": {"target": "email:Owner@Example.com", "message": "hi"},
        }
        self.assertEqual(hub_gate.decide(p, CFG), {})

    def test_unrelated_tools_pass(self):
        for p in (
            {
                "tool_name": "web_search",
                "tool_input": {"query": "send mail via smtplib"},
            },
            {
                "tool_name": "read_file",
                "tool_input": {"path": "/opt/data/webhook_subscriptions.json"},
            },
        ):
            with self.subTest(tool=p["tool_name"]):
                self.assertEqual(hub_gate.decide(p, CFG), {})


class ProtectedPaths(unittest.TestCase):
    def test_write_file_to_the_subscriptions_file_is_blocked(self):
        p = {
            "tool_name": "write_file",
            "tool_input": {
                "path": "/opt/data/webhook_subscriptions.json",
                "content": "{}",
            },
        }
        d = hub_gate.decide(p, CFG)
        self.assertEqual(d["action"], "block")
        self.assertIn("hermes webhook subscribe", d["message"])

    def test_patch_to_it_by_a_relative_path_is_blocked(self):
        p = {
            "tool_name": "patch",
            "tool_input": {
                "path": "webhook_subscriptions.json",
                "old_string": "a",
                "new_string": "b",
            },
        }
        self.assertEqual(hub_gate.decide(p, CFG)["action"], "block")

    def test_write_file_to_a_near_name_passes(self):
        p = {
            "tool_name": "write_file",
            "tool_input": {
                "path": "/opt/data/notes/webhook_subscriptions.json.md",
                "content": "",
            },
        }
        self.assertEqual(hub_gate.decide(p, CFG), {})

    def test_terminal_writes_are_blocked(self):
        for cmd in (
            "jq '.r.toolsets=[\"terminal\"]' webhook_subscriptions.json > /tmp/x && mv /tmp/x webhook_subscriptions.json",
            "echo '{}' > ~/.hermes/webhook_subscriptions.json",
            "sed -i 's/a/b/' /opt/data/webhook_subscriptions.json",
            "python3 -c \"open('/opt/data/webhook_subscriptions.json','w').write('{}')\"",
            "cp /tmp/evil.json /opt/data/webhook_subscriptions.json",
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(hub_gate.decide(_term(cmd), CFG)["action"], "block")

    def test_obfuscated_and_indirect_terminal_writes_are_blocked(self):
        for cmd in (
            "echo '{}' > /opt/data/webhook_sub''scriptions.json",
            "python3 -c \"open('/opt/data/webhook_sub'+'scriptions.json','w').write('{}')\"",
            "cp /tmp/evil.json /opt/data/webhook_*.json",
            "mv /tmp/x /opt/data/webhook_subscript?ons.json",
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(hub_gate.decide(_term(cmd), CFG)["action"], "block")

    def test_a_symlink_to_the_store_is_the_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = os.path.join(tmp, "webhook_subscriptions.json")
            with open(store, "w", encoding="utf-8") as fh:
                fh.write("{}")
            link = os.path.join(tmp, "notes.json")
            os.symlink(store, link)
            p = {
                "tool_name": "write_file",
                "tool_input": {"path": link, "content": "{}"},
            }
            self.assertEqual(hub_gate.decide(p, CFG)["action"], "block")
            rel = {
                "tool_name": "patch",
                "cwd": tmp,
                "tool_input": {
                    "path": "notes.json",
                    "old_string": "{",
                    "new_string": "[",
                },
            }
            self.assertEqual(hub_gate.decide(rel, CFG)["action"], "block")
            t = {
                "tool_name": "terminal",
                "cwd": tmp,
                "tool_input": {"command": "echo '{}' > notes.json"},
            }
            self.assertEqual(hub_gate.decide(t, CFG)["action"], "block")
            # Near-miss: a real file beside it, not a link, is not the store.
            other = os.path.join(tmp, "other.json")
            with open(other, "w", encoding="utf-8") as fh:
                fh.write("{}")
            ok = {
                "tool_name": "write_file",
                "tool_input": {"path": other, "content": "{}"},
            }
            self.assertEqual(hub_gate.decide(ok, CFG), {})

    def test_broad_globs_and_near_names_are_not_the_store(self):
        for cmd in (
            "rm -f /tmp/build/*",
            "cp notes/*.md /tmp/",
            "echo x > /opt/data/notes/webhooks.md",
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(hub_gate.decide(_term(cmd), CFG), {})

    def test_terminal_read_passes(self):
        for cmd in (
            "cat /opt/data/webhook_subscriptions.json",
            "hermes webhook list",
            "hermes webhook subscribe watch-repo --prompt 'x'",
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(hub_gate.decide(_term(cmd), CFG), {})


class Process(unittest.TestCase):
    """The real script, a real config file and a real stdin: the contract Hermes sees."""

    def _run(self, payload, config):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "hub_gate.json")
            if config is not None:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(config if isinstance(config, str) else json.dumps(config))
            return subprocess.run(
                [sys.executable, os.path.join(HERE, "hub_gate.py"), "--config", path],
                input=payload,
                capture_output=True,
                text=True,
                timeout=30,
            )

    GOOD = {
        "allowed_recipients": ["owner@example.com"],
        "protected_paths": ["webhook_subscriptions.json"],
    }

    def test_no_environment_override(self):
        # Hermes loads the agent-writable data/.env into the hook's environment with override=True,
        # so no variable may re-point the gate's config.
        with open(os.path.join(HERE, "hub_gate.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("os.environ", src)
        self.assertNotIn("getenv", src)

    def test_planted_config_variable_is_ignored(self):
        # A planted HUB_GATE_CONFIG naming an allow-everyone config must not stop the escalation.
        with tempfile.TemporaryDirectory() as tmp:
            planted = os.path.join(tmp, "planted.json")
            with open(planted, "w", encoding="utf-8") as fh:
                json.dump({"allowed_recipients": ["a@b.co"], "protected_paths": ["x"]}, fh)
            good = os.path.join(tmp, "hub_gate.json")
            with open(good, "w", encoding="utf-8") as fh:
                json.dump(self.GOOD, fh)
            r = subprocess.run(
                [sys.executable, os.path.join(HERE, "hub_gate.py"), "--config", good],
                input=json.dumps(_term("gapi gmail send --to a@b.co --subject s")),
                capture_output=True,
                text=True,
                env=dict(os.environ, HUB_GATE_CONFIG=planted),
                timeout=30,
            )
        self.assertEqual((r.returncode, json.loads(r.stdout)["action"]), (0, "approve"))

    def test_planted_pythonpath_cannot_load_a_fake_module(self):
        # Run as REGISTERED (the isolated interpreter): a fake json.py on a planted PYTHONPATH that
        # answers "no opinion" is never imported, so the send still escalates.
        with tempfile.TemporaryDirectory() as tmp:
            good = os.path.join(tmp, "hub_gate.json")
            with open(good, "w", encoding="utf-8") as fh:
                json.dump(self.GOOD, fh)
            argv = registered_argv("_automations_leaves.yml", "hub_gate.py")
            r = subprocess.run(
                argv + ["--config", good],
                input=json.dumps(_term("gapi gmail send --to a@b.co --subject s")),
                capture_output=True,
                text=True,
                env=planted_pythonpath(tmp),
                timeout=30,
            )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout).get("action"), "approve", (argv, r.stdout))

    def test_escalation_round_trip(self):
        r = self._run(json.dumps(_term("gapi gmail send --to a@b.co --subject s")), self.GOOD)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(json.loads(r.stdout)["action"], "approve")

    def test_no_opinion_round_trip(self):
        r = self._run(json.dumps(_term("ls -la")), self.GOOD)
        self.assertEqual((r.returncode, json.loads(r.stdout)), (0, {}))

    def test_missing_config_fails_closed(self):
        r = self._run(json.dumps(_term("ls")), None)
        self.assertEqual(r.returncode, 2)
        self.assertIn("blocking", r.stderr)

    def test_empty_protected_list_fails_closed(self):
        r = self._run(json.dumps(_term("ls")), {"allowed_recipients": [], "protected_paths": []})
        self.assertEqual(r.returncode, 2)

    def test_garbage_stdin_fails_closed(self):
        r = self._run("not json", self.GOOD)
        self.assertEqual(r.returncode, 2)

    def test_stderr_never_carries_the_tool_arguments(self):
        secret_body = "BODY-CANARY-PLAIN"
        r = self._run(json.dumps(_term(f"gapi gmail send --to a@b.co --body {secret_body}")), None)
        self.assertNotIn(secret_body, r.stderr + r.stdout)
        r = self._run(
            json.dumps(_term(f"gapi gmail send --to a@b.co --body {secret_body}")),
            self.GOOD,
        )
        self.assertNotIn(secret_body, r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=1)
