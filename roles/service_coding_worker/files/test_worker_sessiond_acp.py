#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for worker-sessiond's PROTOCOL mode and acpx-mcp end to end.

Three groups, and what each can prove:

* PURE decisions: acpx's JSON results, the journal's turn results, the leftover-process sweep (in a
  container's pid namespace and on a bare host), the cwd restriction, the ACP MCP renderer, the
  acceptance marker. Always run.
* CONTAINER mode with a scripted runner standing in for docker and acpx: the exact argv sessiond
  asks for (values never on a command line, the prompt on stdin), and the cancel's escalation
  ladder (settled + swept, unreadable, never settles -> the container is killed, inside 5 s).
  Always run. It cannot prove sysbox or the real adapters: only an acceptance test on the worker
  VM can.
* LIVE: the REAL acpx CLI (pinned version) driving a mock ACP agent on this host, through the real
  sessiond HTTP handler AND acpx-mcp: session_new, a tool that keeps running, session_cancel
  ending it (the tick file stops) inside 5 s, an agent that IGNORES the cancel swept, resume on the
  same ACP session, and the cwd negative. Needs ``acpx`` (``ACPX_BIN`` or on PATH) and node; when
  absent the group is SKIPPED LOUDLY, and ``WORKER_ACP_REQUIRE_LIVE=1`` turns that into a failure.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import acpx_mcp as am  # noqa: E402
import worker_sessiond as ws  # noqa: E402

GIB = 1024**3


# ----------------------------------------------------------------------------------------- pure
class TestAcpxJson(unittest.TestCase):
    def test_picks_the_result_document_not_the_stream(self):
        out = "\n".join(
            [
                '{"jsonrpc":"2.0","method":"session/update","params":{}}',
                "not json",
                '{"action":"prompt_queued","acpxRecordId":"r","requestId":"q1"}',
            ]
        )
        self.assertEqual(ws.parse_acpx_json(out, "prompt_queued")["requestId"], "q1")
        self.assertEqual(ws.parse_acpx_json(out)["action"], "prompt_queued")

    def test_a_different_action_is_not_the_answer(self):
        out = '{"action":"cancel_result","cancelled":true}'
        self.assertIsNone(ws.parse_acpx_json(out, "prompt_queued"))
        self.assertIsNone(ws.parse_acpx_json(""))
        self.assertIsNone(ws.parse_acpx_json('["a"]\n{"other":1}'))

    def test_session_record(self):
        out = '{"schema":"acpx.session.v1","acpSessionId":"s1"}'
        self.assertEqual(ws.parse_acpx_json(out)["acpSessionId"], "s1")


class TestTurnResults(unittest.TestCase):
    J = "\n".join(
        [
            '{"schema":"acpx.session.journal.v1","type":"turn_result","request_id":"a",'
            '"result":{"status":"cancelled","stopReason":"cancelled"}}',
            '{"jsonrpc":"2.0","id":2,"result":{"stopReason":"cancelled"}}',
            '{"type":"turn_result", BROKEN',
            '{"type":"turn_result","request_id":"c","result":{"status":"completed"}}',
        ]
    )

    def test_only_requested_ids_that_settled(self):
        self.assertEqual(ws.turn_results(self.J, ["a", "b"]), {"a": "cancelled"})
        # near-miss: the other id that did settle is reported when asked for
        self.assertEqual(ws.turn_results(self.J, ["c"]), {"c": "completed"})

    def test_nothing_settled(self):
        self.assertEqual(ws.turn_results("", ["a"]), {})
        self.assertEqual(ws.turn_results(self.J, []), {})


# A card container's process table after a cancel, by pid namespace:
#   1 docker-init -> 7 sleep (entry); 1 -> 8 dockerd -> 9 containerd -> 10 containerd-shim -> 11 nginx
#   1 -> 20 node (acpx owner) -> 21 claude-agent-ac -> 22 node (claude cli) -> 30 bash (tool)
#       -> 31 node (tool's own node: a TOOL, not the agent) ; 30 -> 32 sleep
#   1 -> 40 python3 (a tool that daemonised itself and was reparented to init)
#   0 -> 50 ps (this supervisor's own docker exec)
ROWS = [
    (1, 0, "docker-init"),
    (7, 1, "sleep"),
    (8, 1, "dockerd"),
    (9, 8, "containerd"),
    (10, 9, "containerd-shim"),
    (11, 10, "nginx"),
    (20, 1, "node"),
    (21, 20, "claude-agent-ac"),
    (22, 21, "node"),
    (30, 22, "bash"),
    (31, 30, "node"),
    (32, 30, "sleep"),
    (40, 1, "python3"),
    (50, 0, "ps"),
]


class TestSessionContinuity(unittest.TestCase):
    # acpx's journal as a real worker writes it: the first prompt's session/resume of the
    # id `sessions new` returned answers -32002, acpx opens a fresh session and prompts THAT one.
    J = "\n".join(
        [
            '{"jsonrpc":"2.0","id":0,"method":"session/resume","params":{"sessionId":"s-new"}}',
            '{"jsonrpc":"2.0","id":0,"error":{"code":-32002,"message":"Resource not found"}}',
            '{"jsonrpc":"2.0","id":1,"method":"session/new","params":{"cwd":"/w"}}',
            '{"jsonrpc":"2.0","id":1,"result":{"sessionId":"s-live"}}',
            '{"schema":"acpx.session.journal.v1","type":"turn_started","request_id":"a"}',
            '{"jsonrpc":"2.0","id":2,"method":"session/prompt","params":{"sessionId":"s-live"}}',
            '{"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"s-live"}}',
            '{"jsonrpc":"2.0","method":"session/cancel","params":{"sessionId":"s-live"}}',
            '{"jsonrpc":"2.0","id":3,"method":"session/prompt", BROKEN',
            '{"jsonrpc":"2.0","id":4,"method":"session/prompt","params":{"sessionId":"s-live"}}',
        ]
    )

    def test_prompt_ids_are_only_the_prompts(self):
        self.assertEqual(ws.prompt_session_ids(self.J), ["s-live", "s-live"])
        self.assertEqual(ws.prompt_session_ids(""), [])

    def test_bound_at_the_first_prompt_not_at_sessions_new(self):
        # the failure P2b reported: the id from `sessions new` is never the one the turns ran in
        ids = ws.prompt_session_ids(self.J)
        self.assertEqual(ws.acp_continuity(None, "s-live", ids), ("s-live", True))

    def test_a_later_fallback_to_a_fresh_session_is_not_the_same_session(self):
        later = (
            self.J
            + '\n{"jsonrpc":"2.0","id":9,"method":"session/prompt","params":{"sessionId":"s-other"}}'
        )
        ids = ws.prompt_session_ids(later)
        self.assertEqual(ws.acp_continuity("s-live", "s-other", ids), ("s-live", False))
        # acpx holding another session now is a break even before it is prompted
        self.assertEqual(ws.acp_continuity("s-live", "s-other", ["s-live"]), ("s-live", False))

    def test_cannot_tell_is_none_never_true(self):
        self.assertEqual(ws.acp_continuity("s-live", "s-live", None), ("s-live", None))
        self.assertEqual(ws.acp_continuity(None, "s-live", []), (None, None))
        self.assertEqual(ws.acp_continuity(None, None, ["s-live"]), ("s-live", None))


class TestLeftovers(unittest.TestCase):
    def test_container_tools_and_daemonised_orphans_are_leftovers(self):
        self.assertEqual(ws.acp_leftovers(ROWS, 20, True), [30, 31, 32, 40])

    def test_near_miss_an_idle_session_leaves_nothing(self):
        idle = [r for r in ROWS if r[0] not in (30, 31, 32, 40)]
        self.assertEqual(ws.acp_leftovers(idle, 20, True), [])

    def test_only_the_first_sleep_under_init_is_the_entry(self):
        # a second sleep reparented to init is something a tool left behind
        self.assertIn(45, ws.acp_leftovers(ROWS + [(45, 1, "sleep")], 20, True))
        self.assertNotIn(7, ws.acp_leftovers(ROWS + [(45, 1, "sleep")], 20, True))

    def test_inner_docker_tree_is_not_the_session(self):
        self.assertNotIn(11, ws.acp_leftovers(ROWS, 20, True))

    def test_owner_gone_means_every_agent_process_is_a_leftover(self):
        # the owner exited (TTL) but its adapter did not: nothing is core any more
        self.assertEqual(ws.acp_leftovers(ROWS, None, True), [20, 21, 22, 30, 31, 32, 40])

    def test_a_kept_comm_is_respected(self):
        # bash joins the chain, and so does the node under it (its parent is now core)
        self.assertEqual(ws.acp_leftovers(ROWS, 20, True, keep_comms=("bash",)), [32, 40])

    def test_bare_host_considers_only_the_owners_tree(self):
        host = ROWS + [(99, 1, "launchd-thing")]
        self.assertEqual(ws.acp_leftovers(host, 20, False), [30, 31, 32])

    def test_parse_ps(self):
        self.assertEqual(
            ws.parse_ps_comm("  1     0 docker-init\n 22 21 node\nbad line\n 5 1 my tool\n"),
            [(1, 0, "docker-init"), (22, 21, "node"), (5, 1, "my tool")],
        )


class TestCwd(unittest.TestCase):
    R = {"c1": {"path": "/w/worktrees/c1"}, "c2": {"path": "/w/worktrees/c2"}}

    def test_exact_path_maps_to_its_card(self):
        self.assertEqual(ws.validate_acp_cwd(self.R, "/w/worktrees/c1"), "c1")
        self.assertEqual(ws.validate_acp_cwd(self.R, "/w/worktrees/c1/"), "c1")

    def test_anything_else_is_refused(self):
        for bad in [
            "/w/worktrees/c1/sub",
            "/w/worktrees",
            "/w",
            "/etc",
            "/tmp/x",
            "w/worktrees/c1",
            "",
            None,
            7,
        ]:
            with self.assertRaises(ws.RefusedError, msg=repr(bad)) as c:
                ws.validate_acp_cwd(self.R, bad)
            self.assertIn(c.exception.status, (403, 422))

    def test_dotdot_is_normalised_not_trusted(self):
        # normalises to c2's path: the card it NAMES, never c1's
        self.assertEqual(ws.validate_acp_cwd(self.R, "/w/worktrees/c1/../c2"), "c2")
        with self.assertRaises(ws.RefusedError):
            ws.validate_acp_cwd(self.R, "/w/worktrees/c1/../../etc")


class TestAcpMcpRender(unittest.TestCase):
    def test_entries(self):
        out = ws.render_acpx_mcp(
            [
                {"name": "a", "transport": "http", "url": "http://x/mcp"},
                {
                    "name": "b",
                    "transport": "stdio",
                    "command": "/bin/b",
                    "args": ["-x"],
                    "env": {"TOK": "${TOK}"},
                },
            ]
        )
        self.assertEqual(
            out["mcpServers"],
            [
                {"name": "a", "type": "http", "url": "http://x/mcp", "headers": []},
                {
                    "name": "b",
                    "type": "stdio",
                    "command": "/bin/b",
                    "args": ["-x"],
                    "env": [],
                },
            ],
        )
        self.assertNotIn("${TOK}", json.dumps(out))

    def test_headers_are_refused(self):
        with self.assertRaises(ws.RefusedError) as c:
            ws.render_acpx_mcp(
                [
                    {
                        "name": "g",
                        "transport": "http",
                        "url": "u",
                        "headers": {"Authorization": "Bearer ${T}"},
                    }
                ]
            )
        self.assertEqual(c.exception.status, 422)

    def test_a_renamed_env_reference_is_refused(self):
        with self.assertRaises(ws.RefusedError):
            ws.render_acpx_mcp(
                [
                    {
                        "name": "b",
                        "transport": "stdio",
                        "command": "c",
                        "env": {"A": "${B}"},
                    }
                ]
            )


class TestHomeConfigAndAcceptance(unittest.TestCase):
    def test_home_config(self):
        c = ws.acpx_home_config({"claude": ["/t/bin/claude-agent-acp"]}, "approve-all")
        self.assertEqual(c["agents"], {"claude": {"argv": ["/t/bin/claude-agent-acp"]}})
        self.assertEqual(c["defaultPermissions"], "approve-all")
        self.assertEqual(c["nonInteractivePermissions"], "deny")
        with self.assertRaises(ws.RefusedError):
            ws.acpx_home_config({}, "bypass")

    def test_acceptance(self):
        pins = {"acpx_version": "0.21.1", "claude_agent_acp_version": "0.99.1"}
        self.assertFalse(ws.acp_acceptance(None, pins)["accepted"])
        self.assertFalse(ws.acp_acceptance({"passed": False, **pins}, pins)["accepted"])
        drift = ws.acp_acceptance({"passed": True, **pins, "acpx_version": "0.20.0"}, pins)
        self.assertFalse(drift["accepted"])
        self.assertIn("acpx_version", drift["reason"])
        self.assertTrue(ws.acp_acceptance({"passed": True, **pins}, pins)["accepted"])
        # "true" the string is not True: the marker must say passed, typed
        self.assertFalse(ws.acp_acceptance({"passed": "true", **pins}, pins)["accepted"])


class TestReconcileProtocol(unittest.TestCase):
    def test_no_tmux_is_normal_for_a_protocol_card(self):
        rec = {"state": "running", "mode": "protocol", "container": "card-p"}
        out = ws.reconcile_records({"p": rec}, {}, "card-", 10.0, {"card-p": "running"})
        self.assertEqual(out["p"]["state"], "running")
        # near-miss: the SAME facts on an interactive card mean the session vanished
        rec_i = {"state": "running", "container": "card-p"}
        out = ws.reconcile_records({"p": rec_i}, {}, "card-", 10.0, {"card-p": "running"})
        self.assertEqual(out["p"]["state"], "vanished")

    def test_container_gone_is_vanished_and_paused_is_paused(self):
        rec = {"state": "running", "mode": "protocol", "container": "card-p"}
        self.assertEqual(
            ws.reconcile_records({"p": rec}, {}, "card-", 1.0, {})["p"]["state"],
            "vanished",
        )
        self.assertEqual(
            ws.reconcile_records({"p": rec}, {}, "card-", 1.0, {"card-p": "paused"})["p"]["state"],
            "paused",
        )
        # docker unreadable: nothing concluded
        self.assertEqual(
            ws.reconcile_records({"p": rec}, {}, "card-", 1.0, None)["p"]["state"],
            "running",
        )

    def test_metrics_do_not_count_a_protocol_card_as_unknown_activity(self):
        recs = {"p": {"state": "running", "mode": "protocol"}}
        text = ws.render_metrics(recs, {}, "card-", 100.0)
        self.assertIn("hermes_worker_running_activity_unknown 0", text)
        self.assertIn('hermes_worker_sessions{state="running"} 1', text)
        # near-miss: an interactive card with no tmux activity IS counted
        text = ws.render_metrics({"i": {"state": "running"}}, {}, "card-", 100.0)
        self.assertIn("hermes_worker_running_activity_unknown 1", text)


# ------------------------------------------------------------------------- container, scripted
class ScriptedRunner:
    """docker + acpx as a state machine. ``settle`` decides whether a cancel makes the turn appear
    in the journal; ``tool_survives`` whether a tool process is left behind after it."""

    def __init__(self):
        self.calls: list[dict] = []
        self.containers: dict[str, str] = {}
        self.journal: list[str] = []
        self.requests: list[str] = []
        self.settle = True
        self.tool_survives = False
        self.ps_works = True
        self.killed: list[str] = []
        self.n = 0
        # the session acpx holds; like Claude's adapter, the first prompt replaces the unprompted
        # session `sessions new` made (rebind_first), and a resume may fall back (set live_sid)
        self.live_sid = "s-1"
        self.rebind_first = False

    def __call__(self, cmd, input=None, timeout=None, **kw):  # noqa: A002
        self.calls.append({"cmd": list(cmd), "input": input})
        argv = list(cmd)
        if argv[:2] == ["bash", "-c"]:
            argv = argv[4:]  # bash -c <script> <envfile> <cmd...>
        ok = lambda out="": subprocess.CompletedProcess(argv, 0, out, "")  # noqa: E731
        no = lambda err="": subprocess.CompletedProcess(argv, 1, "", err)  # noqa: E731
        if argv[:2] == ["docker", "inspect"]:
            name = argv[-1]
            st = self.containers.get(name)
            if st is None:
                return no("No such object")
            return ok(st if argv[3] == "{{.State.Status}}" else f"sysbox-runc {st}")
        if argv[:2] == ["docker", "network"]:
            return ok()
        if argv[:2] == ["docker", "run"]:
            self.containers[argv[argv.index("--name") + 1]] = "running"
            return ok("id")
        if argv[:2] == ["docker", "kill"]:
            self.killed.append(argv[-1])
            self.containers[argv[-1]] = "exited"
            return ok()
        if argv[:2] == ["docker", "rm"]:
            self.containers.pop(argv[-1], None)
            return ok()
        if argv[:2] == ["docker", "start"]:
            self.containers[argv[-1]] = "running"
            return ok()
        if argv[:2] == ["docker", "unpause"]:
            self.containers[argv[-1]] = "running"
            return ok()
        if argv[:2] == ["docker", "ps"]:
            return ok("\n".join(f"{n}\t{s}" for n, s in self.containers.items()))
        if argv[:2] == ["docker", "exec"]:
            i = 2
            while argv[i].startswith("-"):
                i += 2 if argv[i] in ("-w", "-e") else 1
            inner = argv[i + 1 :]
            return self.inner(inner, input, ok, no)
        if argv[:1] == ["ps"] or argv[:1] == ["tmux"]:
            return ok("")
        return no(f"unscripted: {argv[:3]}")

    def inner(self, inner, stdin, ok, no):
        if inner[:1] == ["tail"]:
            return ok("\n".join(self.journal))
        if inner[:1] == ["ps"]:
            if not self.ps_works:
                return no("ps: not found")
            rows = [
                (1, 0, "docker-init"),
                (7, 1, "sleep"),
                (20, 1, "node"),
                (21, 20, "node"),
            ]
            if self.tool_survives:
                rows.append((30, 21, "bash"))
            rows.append((50, 0, "ps"))
            return ok("\n".join(f"{a} {b} {c}" for a, b, c in rows))
        if inner[:1] == ["kill"]:
            self.tool_survives = bool(os.environ.get("SCRIPTED_KILL_FAILS"))
            return ok()
        if inner[:1] != ["acpx"]:
            return no(f"unscripted inner: {inner[:2]}")
        verb = inner[inner.index("claude") + 1 :] if "claude" in inner else []
        if verb[:2] == ["sessions", "new"]:
            return ok(
                '{"action":"session_ensured","acpxRecordId":"rec-1234567","acpxSessionId":"s-1"}'
            )
        if verb[:1] == ["prompt"]:
            self.n += 1
            rid = f"req-{self.n}"
            self.requests.append(rid)
            if self.n == 1 and self.rebind_first:
                self.live_sid = "s-2"
            prompt = {"jsonrpc": "2.0", "id": self.n, "method": "session/prompt"}
            self.journal.append(json.dumps(dict(prompt, params={"sessionId": self.live_sid})))
            return ok(json.dumps({"action": "prompt_queued", "requestId": rid}))
        if verb[:1] == ["cancel"]:
            if self.settle:
                for rid in self.requests:
                    self.journal.append(
                        json.dumps(
                            {
                                "type": "turn_result",
                                "request_id": rid,
                                "result": {"status": "cancelled"},
                            }
                        )
                    )
            return ok('{"action":"cancel_result","cancelled":true}')
        if verb[:1] == ["status"]:
            return ok('{"action":"status_snapshot","status":"alive","pid":20}')
        if verb[:2] == ["sessions", "show"]:
            return ok(json.dumps({"schema": "acpx.session.v1", "acpSessionId": self.live_sid}))
        return no(f"unscripted acpx: {verb[:2]}")


class TestContainerProtocol(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sd-acp-"))
        self.wt = self.tmp / "work" / "worktrees" / "p1"
        self.wt.mkdir(parents=True)
        (self.tmp / "agent.env").write_text("ANTHROPIC_AUTH_TOKEN='sk-test-secret-value-1234'\n")
        self.cfg = {
            "state_dir": str(self.tmp / "state"),
            "work_root": str(self.tmp / "work"),
            "repos": [{"name": "lab"}],
            "container_runtime": "sysbox-runc",
            "container_image": "card:test",
            "container_tools_dir": "/opt/node",
            "card_memory": "2g",
            # The free-disk floor reads the RUNNER's real disk, so a small CI container (5 GB
            # measured under ansible-test units) refused every launch here. The floor has its own
            # case in test_worker_sessiond.py (test_09c); these cases are about the ACP session.
            "min_free_disk_gb": 0,
            "agent_env_file": str(self.tmp / "agent.env"),
            "acp_agents": {"claude": ["/opt/node/bin/claude-agent-acp"]},
            "acp_permissions": "approve-all",
            "acp_pins": {"acpx_version": "0.21.1"},
            "acp_acceptance_file": str(self.tmp / "accepted.json"),
            "acp_cancel_budget_seconds": 2.0,
        }
        self.r = ScriptedRunner()
        self.sup = ws.Supervisor(self.cfg, runner=self.r)
        self.sup._vm_memory_bytes = lambda: 16 * GIB
        self.sup._save(
            {
                "p1": {
                    "state": "created",
                    "repo": "lab",
                    "branch": "card/p1",
                    "path": str(self.wt),
                    "mode": "interactive",
                    "tmux": "card-p1",
                    "container": "card-p1",
                }
            }
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def acpx_calls(self):
        out = []
        for c in self.r.calls:
            cmd = c["cmd"]
            if "acpx" in cmd:
                out.append(c)
        return out

    def test_new_runs_acpx_inside_the_container_with_names_not_values(self):
        got = self.sup.acp_new(str(self.wt), "claude", "fix the thing")
        self.assertEqual(got["mode"], "protocol")
        self.assertEqual(got["request_id"], "req-1")
        self.assertEqual(self.r.containers["card-p1"], "running")
        new, prompt = self.acpx_calls()[:2]
        cmd = new["cmd"]
        self.assertEqual(cmd[:2], ["bash", "-c"])  # the env file is sourced in a CHILD shell
        d = cmd.index("docker")
        self.assertEqual(cmd[d : d + 3], ["docker", "exec", "-i"])
        self.assertIn("ANTHROPIC_AUTH_TOKEN", cmd)
        self.assertIn("CODEX_HOME=/card/codex", cmd)
        a = cmd.index("acpx")
        self.assertEqual(cmd[a + 1 : a + 3], ["--cwd", str(self.wt)])
        self.assertIn("--approve-all", cmd)
        self.assertEqual(cmd[cmd.index("--mcp-config") + 1], "/card/acp-mcp.json")
        self.assertEqual(cmd[-4:], ["sessions", "new", "--name", "card-p1"])
        self.assertEqual(prompt["cmd"][-6:], ["prompt", "-s", "card-p1", "--no-wait", "-f", "-"])
        self.assertEqual(prompt["input"], "fix the thing")  # on stdin, never argv
        for c in self.r.calls:
            self.assertNotIn("sk-test-secret-value-1234", json.dumps(c["cmd"]))
        cfg = json.loads((self.tmp / "state/homes/p1/.acpx/config.json").read_text())
        self.assertEqual(cfg["agents"]["claude"]["argv"], ["/opt/node/bin/claude-agent-acp"])

    def test_acp_managed_policy_installed_and_missing_refused(self):
        self.sup.cfg["claude_settings"] = {"permissions": {"deny": ["Bash(rm *)"]}}
        original = self.sup._install_policy
        self.sup._install_policy = lambda *args: None
        with self.assertRaises(ws.RefusedError) as error:
            self.sup.acp_new(str(self.wt), "claude", "x")
        self.assertEqual(error.exception.status, 500)
        self.assertEqual(self.r.containers, {})
        self.sup._install_policy = original
        self.sup.acp_new(str(self.wt), "claude", "x")
        policy = self.tmp / "state/cards/p1/policy/managed-settings.json"
        self.assertIn("Bash(rm *)", json.loads(policy.read_text())["permissions"]["deny"])

    def test_interactive_launch_installs_managed_policy(self):
        self.sup.cfg["claude_settings"] = {"permissions": {"deny": ["Bash(rm *)"]}}
        self.sup.tmux_live = lambda: {}
        self.sup.launch("p1", "claude", "x")
        policy = self.tmp / "state/cards/p1/policy/managed-settings.json"
        self.assertIn("Bash(rm *)", json.loads(policy.read_text())["permissions"]["deny"])

    def test_only_a_fresh_card_gets_a_session(self):
        self.sup.acp_new(str(self.wt), "claude", "x")
        with self.assertRaises(ws.RefusedError) as c:
            self.sup.acp_new(str(self.wt), "claude", "again")
        self.assertEqual(c.exception.status, 409)
        with self.assertRaises(ws.RefusedError) as c:
            self.sup.launch("p1", "claude", "tmux too")
        self.assertEqual(c.exception.status, 409)

    def test_a_cwd_sessiond_did_not_create_is_refused_before_anything_runs(self):
        for bad in ("/tmp", str(self.wt / "sub"), str(self.tmp / "work")):
            with self.assertRaises(ws.RefusedError) as c:
                self.sup.acp_new(bad, "claude", "x")
            self.assertEqual(c.exception.status, 403)
        self.assertEqual(self.r.containers, {})
        self.assertEqual(self.acpx_calls(), [])

    def test_cancel_settled_and_swept_keeps_the_container(self):
        self.sup.acp_new(str(self.wt), "claude", "x")
        self.r.tool_survives = True
        t0 = time.monotonic()
        res = self.sup.acp_cancel("p1")
        self.assertLess(time.monotonic() - t0, 5.0)
        self.assertTrue(res["settled"])
        self.assertEqual(res["swept"], [30])
        self.assertFalse(res["hard"])
        self.assertTrue(res["effects_ceased"])
        self.assertEqual(self.r.killed, [])
        self.assertEqual(self.sup._load()["p1"]["acp_requests"], [])

    def test_cancel_that_never_settles_kills_the_container_inside_the_budget(self):
        self.sup.acp_new(str(self.wt), "claude", "x")
        self.r.settle = False
        t0 = time.monotonic()
        res = self.sup.acp_cancel("p1")
        self.assertLess(time.monotonic() - t0, 5.0)
        self.assertFalse(res["settled"])
        self.assertTrue(res["hard"])
        self.assertTrue(res["effects_ceased"])
        self.assertEqual(self.r.killed, ["card-p1"])
        self.assertEqual(res["state"], "stopped")

    def test_cancel_with_an_unreadable_process_table_fails_towards_the_kill(self):
        self.sup.acp_new(str(self.wt), "claude", "x")
        self.r.ps_works = False
        res = self.sup.acp_cancel("p1")
        self.assertTrue(res["settled"])
        self.assertTrue(res["hard"])
        self.assertEqual(self.r.killed, ["card-p1"])

    def test_a_kill_that_does_not_take_is_a_500_not_a_cancel(self):
        self.sup.acp_new(str(self.wt), "claude", "x")
        self.r.settle = False
        orig = self.r.__call__

        def stubborn(cmd, input=None, timeout=None, **kw):  # noqa: A002
            if list(cmd)[:2] == ["docker", "kill"]:
                return subprocess.CompletedProcess(cmd, 1, "", "nope")
            return orig(cmd, input=input, timeout=timeout, **kw)

        self.sup.run = stubborn
        with self.assertRaises(ws.RefusedError) as c:
            self.sup.acp_cancel("p1")
        self.assertEqual(c.exception.status, 500)
        self.assertFalse(json.loads(str(c.exception))["effects_ceased"])

    def test_resume_after_a_hard_cancel_restarts_the_same_container(self):
        self.sup.acp_new(str(self.wt), "claude", "x")
        self.r.settle = False
        self.sup.acp_cancel("p1")
        self.assertEqual(self.r.containers["card-p1"], "exited")
        self.r.settle = True
        out = self.sup.acp_resume("p1", "carry on")
        self.assertEqual(out["from_state"], "stopped")
        self.assertIsNotNone(out["request_id"])
        starts = [c["cmd"] for c in self.r.calls if c["cmd"][:2] == ["docker", "start"]]
        # the EXITED container is started again (its inner docker state survives), not recreated
        self.assertEqual(starts, [["docker", "start", "card-p1"]])
        self.assertEqual(self.sup.acp_get("p1")["same_session"], True)

    def test_the_session_is_bound_where_the_first_turn_ran_and_a_later_fallback_is_caught(self):
        self.r.rebind_first = True
        self.sup.acp_new(str(self.wt), "claude", "remember HERON")
        st = self.sup.acp_get("p1")
        self.assertIs(st["same_session"], True, st)
        self.assertEqual(st["acp_session_id"], "s-2")
        self.assertEqual(st["acp_session_id_at_new"], "s-1")
        self.assertEqual(self.sup._load()["p1"]["acp_session_bound"], "s-2")  # persisted once
        self.sup.acp_cancel("p1")
        self.assertIs(self.sup.acp_get("p1")["same_session"], True)
        self.r.live_sid = "s-3"  # a resume that acpx could not load: context lost
        self.sup.acp_prompt("p1", "what was the codeword?")
        st = self.sup.acp_get("p1")
        self.assertIs(st["same_session"], False, st)
        self.assertEqual(st["acp_session_id"], "s-2")  # the binding never follows a fallback

    def test_an_unreadable_journal_cannot_say_same_session(self):
        self.sup.acp_new(str(self.wt), "claude", "x")
        self.sup._acp_journal = lambda card, rec: None
        self.assertIsNone(self.sup.acp_get("p1")["same_session"])

    def test_send_and_poll_feedback_on_a_protocol_card_queue_a_prompt(self):
        self.sup.acp_new(str(self.wt), "claude", "x")
        out = self.sup.send("p1", "review: rename the function")
        self.assertTrue(out["sent"])
        self.assertEqual(self.acpx_calls()[-1]["input"], "review: rename the function")

    def test_capabilities_prefer_protocol_only_once_accepted_at_these_versions(self):
        self.assertEqual(self.sup.acp_capabilities()["preferred_mode"], "interactive")
        Path(self.cfg["acp_acceptance_file"]).write_text(
            json.dumps({"passed": True, "acpx_version": "0.20.0"})
        )
        self.assertEqual(self.sup.acp_capabilities()["preferred_mode"], "interactive")
        Path(self.cfg["acp_acceptance_file"]).write_text(
            json.dumps({"passed": True, "acpx_version": "0.21.1"})
        )
        self.assertEqual(self.sup.acp_capabilities()["preferred_mode"], "protocol")


# ---------------------------------------------------------------------------------------- live
MOCK_AGENT = r'''#!__PY__
"""A minimal ACP agent: `TICK <file>` starts a shell loop writing the time to <file> (a tool that
keeps running) and the turn ends only on session/cancel; with IGNORE in the prompt the agent
reports the cancel but leaves the loop running (what a badly behaved adapter does)."""
import json, os, subprocess, sys, threading, uuid
lock = threading.Lock()
active = {}
# Like claude-agent-acp: a session that was never PROMPTED is not persisted, so
# loading it in a later process answers -32002 and acpx opens a fresh one for the first turn.
KNOWN = os.path.abspath(__file__) + ".prompted"
def prompted():
    try:
        return set(open(KNOWN).read().split())
    except OSError:
        return set()
def send(o):
    with lock:
        sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
def prompt(mid, p):
    sid = p["sessionId"]
    with lock:
        open(KNOWN, "a").write(sid + "\n")
    text = " ".join(x.get("text", "") for x in p.get("prompt", []) if x.get("type") == "text")
    send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": {
        "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "echo: " + text}}}})
    if not text.startswith("TICK "):
        send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}}); return
    proc = subprocess.Popen(["sh", "-c", "while :; do date +%s > " + text.split()[1] + "; sleep 0.3; done"])
    ev = threading.Event(); active[sid] = ev; ev.wait()
    if "IGNORE" not in text:
        proc.kill()
    send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "cancelled"}})
for line in sys.stdin:
    try:
        m = json.loads(line)
    except ValueError:
        continue
    meth, mid, p = m.get("method"), m.get("id"), m.get("params") or {}
    if meth == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": 1, "agentCapabilities": {"loadSession": True}, "authMethods": []}})
    elif meth == "session/new":
        send({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": str(uuid.uuid4())}})
    elif meth == "session/load":
        if p.get("sessionId") in prompted():
            send({"jsonrpc": "2.0", "id": mid, "result": {}})
        else:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32002, "message": "Resource not found"}})
    elif meth == "session/prompt":
        threading.Thread(target=prompt, args=(mid, p), daemon=True).start()
    elif meth == "session/cancel":
        ev = active.pop(p.get("sessionId"), None)
        if ev:
            ev.set()
    elif mid is not None:
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "no"}})
'''


def _find_acpx() -> str:
    return os.environ.get("ACPX_BIN") or shutil.which("acpx") or ""


@unittest.skipUnless(_find_acpx() and shutil.which("git"), "acpx not installed")
class TestLiveAcpx(unittest.TestCase):
    """The real acpx against a mock ACP agent, through sessiond's HTTP API AND acpx-mcp."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="sd-acp-live-"))
        agent = cls.tmp / "mock_agent.py"
        agent.write_text(MOCK_AGENT.replace("__PY__", sys.executable))
        agent.chmod(0o755)
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t")
        env.update(GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        repo = cls.tmp / "work" / "repos" / "lab"
        repo.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True, env=env)
        subprocess.run(
            ["git", "commit", "-q", "--allow-empty", "-m", "i"],
            cwd=repo,
            check=True,
            env=env,
        )
        cls.cfg = {
            "state_dir": str(cls.tmp / "state"),
            "work_root": str(cls.tmp / "work"),
            "repos": [{"name": "lab", "base_branch": "main"}],
            "acp_agents": {"claude": [str(agent)]},
            "acp_permissions": "approve-all",
            "acpx_bin": _find_acpx(),
            # the mock adapter is a python process; a real one is node (ACP_CORE_COMMS)
            "acp_keep_comms": ["python3", "Python", "python"],
        }
        cls.sup = ws.Supervisor(cls.cfg)
        cls.sd_token, cls.mcp_token = "s" * 24, "m" * 24
        cls.sd = ThreadingHTTPServer(("127.0.0.1", 0), ws.make_handler(cls.sup, cls.sd_token))
        threading.Thread(target=cls.sd.serve_forever, daemon=True).start()
        caller = am.http_caller(f"http://127.0.0.1:{cls.sd.server_port}", cls.sd_token)
        cls.mcp = ThreadingHTTPServer(("127.0.0.1", 0), am.make_handler(caller, cls.mcp_token))
        threading.Thread(target=cls.mcp.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        for r in cls.sup.list():
            try:
                cls.sup.stop(r["card_id"])
            except ws.RefusedError:
                pass
        cls.sd.shutdown()
        cls.mcp.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def tool(self, name, **args):
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": args},
        }
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.mcp.server_port}/mcp",
            data=json.dumps(body).encode(),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.mcp_token}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            res = json.loads(r.read())["result"]
        return res["isError"], res["structuredContent"]

    def tick_age(self, f: Path) -> float:
        return time.time() - int(f.read_text().strip() or 0)

    def wait_tick(self, f: Path):
        for _ in range(100):
            if f.exists() and f.read_text().strip():
                return
            time.sleep(0.1)
        self.fail("the tool never started")

    def test_01_new_cancel_effects_cease_resume_same_session(self):
        card = self.sup.create("live1", "lab")
        tick = self.tmp / "tick1"
        err, out = self.tool("session_new", agent="claude", cwd=card["path"], prompt=f"TICK {tick}")
        self.assertFalse(err, out)
        self.wait_tick(tick)
        time.sleep(1.0)
        self.assertLess(self.tick_age(tick), 1.5)  # positive control: the effect IS running
        t0 = time.monotonic()
        err, out = self.tool("session_cancel", session_id="live1")
        elapsed = time.monotonic() - t0
        self.assertFalse(err, out)
        self.assertLess(elapsed, 5.0)
        self.assertTrue(out["effects_ceased"])
        time.sleep(1.5)
        self.assertGreater(self.tick_age(tick), 1.2, "the tool kept running after the cancel")
        err, out = self.tool("session_resume", session_id="live1", prompt="remember me")
        self.assertFalse(err, out)
        for _ in range(50):
            _, st = self.tool("session_status", session_id="live1", history=10)
            if st.get("busy") is False:
                break
            time.sleep(0.2)
        self.assertTrue(st["same_session"], st)
        self.assertIn("echo: remember me", [h["text"] for h in st["history"]])
        # bound where the first turn ran, which the unprompted `sessions new` id never is
        self.assertNotEqual(st["acp_session_id"], st["acp_session_id_at_new"], st)

    def test_02_an_agent_that_ignores_the_cancel_is_swept(self):
        card = self.sup.create("live2", "lab")
        tick = self.tmp / "tick2"
        err, out = self.tool(
            "session_new",
            agent="claude",
            cwd=card["path"],
            prompt=f"TICK {tick} IGNORE",
        )
        self.assertFalse(err, out)
        self.wait_tick(tick)
        t0 = time.monotonic()
        err, out = self.tool("session_cancel", session_id="live2")
        self.assertLess(time.monotonic() - t0, 5.0)
        self.assertFalse(err, out)
        self.assertTrue(out["swept"], out)  # the loop the agent left behind was found and killed
        time.sleep(1.5)
        self.assertGreater(self.tick_age(tick), 1.2, "the ignored tool kept running")

    def test_03_cwd_not_created_by_sessiond_is_refused(self):
        err, out = self.tool("session_new", agent="claude", cwd=str(self.tmp), prompt="x")
        self.assertTrue(err)
        self.assertEqual(out["status"], 403)


def main() -> int:
    live = bool(_find_acpx())
    if not live:
        sys.stderr.write(
            "\n*** LIVE acpx group SKIPPED: acpx is not installed (set ACPX_BIN). The protocol "
            "cancel has then NOT been exercised against the real acpx CLI here. ***\n\n"
        )
    res = unittest.main(exit=False, verbosity=1).result
    if not res.wasSuccessful():
        return 1
    if not live and os.environ.get("WORKER_ACP_REQUIRE_LIVE"):
        sys.stderr.write("WORKER_ACP_REQUIRE_LIVE is set: failing rather than skipping\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
