#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""The gateway `pre_tool_call` policy hook -- the BELT.

Hermes runs this as a shell hook for every matched tool call in every profile (it is declared in
the managed scope, tier C, so no profile can drop it), pipes the call as JSON on stdin, and reads a
decision on stdout. It is registered with `fail_closed: true`: a crash, a timeout or garbage output
BLOCKS the call. It denies, for every profile:

  * a file write (write_file, patch) under a protected path: any profile's `.env`, `config.yaml`,
    `plugins/`, `hooks/`, `webhook_subscriptions.json`, `shell-hooks-allowlist.json`, the policy
    directory, the managed scope;
  * a terminal or execute_code call that names a protected path AND looks like a write;
  * `hermes config set|unset` on any key the settings service would not apply live (owned leaves,
    their parents and children, and the reserved families), and `hermes hooks revoke`,
    `hermes plugins install|enable|disable|remove`.

It is NOT the gate: the settings service checks policy itself, and must still refuse with this
hook deleted. A hook that is not registered denies nothing, which
is why the probe and the hooks-missing alert watch `hermes hooks list`.

Its key classifier is an independent copy of hermes_settings.policy (a shared test table keeps the
two agreeing). Stdlib only; runs in the Hermes image's interpreter.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys

# FIXED PATHS, deliberately with no environment override: the hook runs as a gateway subprocess,
# and Hermes loads the agent-writable data/.env into that environment with override=True, so a
# variable naming the owned-keys list or the data root would let a planted line repoint the guard
# itself. `--owned`/`--data-root` exist for the unit tests only (Hermes passes no arguments).
OWNED = "/opt/hermes-policy/owned-keys.json"
DATA = "/opt/data"
# /opt/hermes is the image's code root and venv: its pyvenv.cfg and site-packages .pth files run in
# every interpreter the gateway and the hooks start, and `-I -B` does not stop them. Root
# ownership is the wall (the preflight's `venv` check); this is the belt. The agent's own tree is
# DATA (/opt/data in the container, not under /opt/hermes), and a path under DATA is judged only
# by the DATA rules below, so protecting a root never blocks the agent's legitimate data writes.
# The cost: a WRITE-looking terminal command that names an /opt/hermes path, such as the venv
# interpreter by absolute path with a redirect, is refused; `python3` by name is not.
ABS_PROTECTED = (
    "/opt/hermes-policy",
    "/etc/hermes",
    "/etc/hermes-managed",
    "/opt/hermes",
)
HOME_PROTECTED = {
    ".env",
    "config.yaml",
    "plugins",
    "hooks",
    "webhook_subscriptions.json",
    "shell-hooks-allowlist.json",
    "policy",
}
WRITE_TOOLS = {"write_file", "patch"}
# The settings service's MCP tools as Hermes names them (mcp__<server>__<tool>). The WRITES are
# escalated to the human-approval gate -- "every write asks, every read is free" -- as a
# structural ask rather than a SOUL instruction. A non-interactive context with no approval bridge
# fails CLOSED upstream (tools/approval.py request_tool_approval), so a cron or webhook run of the
# settings profile cannot write at all.
SETTINGS_PREFIX = "mcp__hub_settings__"
SETTINGS_WRITES = {
    "set_setting",
    "enable_mcp",
    "disable_mcp",
    "add_mcp_server",
    "open_iaac_pr",
    "propose_soul_diff",
    "set_cron",
}
SHELL_TOOLS = {"terminal", "execute_code"}

C_FAMILIES = (
    "approvals",
    "security",
    "hooks",
    "hooks_auto_accept",
    "model.provider",
    "skills.write_approval",
    "skills.guard_agent_created",
    "memory.write_approval",
    "cron.allow_agent_scheduling",
)
B_FAMILIES = (
    "providers",
    "custom_providers",
    "model",
    "platform_toolsets",
    "toolsets",
    "agent.disabled_toolsets",
    "mcp_servers",
    "plugins",
    "platforms",
    "gateway",
    "dashboard",
    "network",
    "terminal",
    "profile_routes",
    "multiplex_profiles",
    "kanban",
    "webhook",
    "sessions",
    "tool_loop_guardrails",
    "skills.external_dirs",
    "browser",
    "code_execution",
    "delegation",
)
A_EXCEPTIONS = ("dashboard.theme", "dashboard.font")
SECRET_SEG = re.compile(r"(?i)(api_?key|token|secret|password|passwd|credential|private_?key)")
ENV_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")
# A shell command that writes: redirection, or a verb that changes a file.
WRITE_MARK = re.compile(
    r"(>|\btee\b|\bcp\b|\bmv\b|\brm\b|\bln\b|\bsed\s+-i|\bchmod\b|\bchown\b|\btruncate\b|\binstall\b|\bdd\b"
    r"|\brsync\b|\btouch\b|\bunlink\b|open\([^)]*['\"][wax+]|write_text|write_bytes|os\.remove|os\.rename"
    r"|os\.replace|shutil\.|\.unlink\(|\bperl\b|\bpython3?\b|\bawk\b)"
)
HERMES_CMD = re.compile(r"\bhermes\b")


def _segs(key: str) -> list[str]:
    return key.split(".")


def _prefix(a: list[str], b: list[str]) -> bool:
    return len(a) <= len(b) and b[: len(a)] == a


def _family(key: str, table) -> bool:
    s = _segs(key)
    return any(_prefix(_segs(e), s) or _prefix(s, _segs(e)) for e in table)


def load_owned(path: str | None = None):
    with open(path or OWNED, encoding="utf-8") as fh:
        doc = json.load(fh)
    owned = doc["owned"]
    if not isinstance(owned, list):
        raise ValueError("owned is not a list")
    return owned


def key_is_live_writable(key: str, owned) -> bool:
    """True only when the settings service would apply this key LIVE (tier A) for some value."""
    if ENV_KEY.match(key) or any(SECRET_SEG.search(s) for s in _segs(key)):
        return False
    s = _segs(key)
    for leaf in owned:
        ls = _segs(str(leaf.get("key", "")))
        if ls == s:
            # A-range included: its BOUNDS are checked by the settings service, not here, so only
            # the service moves it.
            return False
        if _prefix(s, ls) or _prefix(ls, s):
            return False
    if _family(key, C_FAMILIES):
        return False
    if _family(key, B_FAMILIES) and not (len(s) >= 2 and _family(key, A_EXCEPTIONS)):
        return False
    return True


def protected_path(path: str, cwd: str = "") -> bool:
    if not isinstance(path, str) or not path:
        return False
    p = os.path.expanduser(path)
    if not os.path.isabs(p):
        p = os.path.join(cwd or DATA, p)
    # realpath: a symlink in a writable directory pointing at a protected file is that file. Both
    # forms are checked, so a root that is itself a symlink (macOS /etc) still matches.
    forms = {os.path.normpath(p), os.path.realpath(os.path.normpath(p))}
    roots = set(ABS_PROTECTED) | {os.path.realpath(r) for r in ABS_PROTECTED}
    data_roots = {os.path.normpath(DATA), os.path.realpath(DATA)}

    def in_data(f: str) -> bool:
        return any(f == d or f.startswith(d + "/") for d in data_roots)

    # A form inside DATA is judged by the DATA rules only, so a protected root that CONTAINS the
    # data root (the host layout, /opt/hermes/data) does not swallow it. Every form is still
    # judged: a link in DATA whose target is under a protected root has a target form outside DATA.
    if any(f == r or f.startswith(r + "/") for f in forms if not in_data(f) for r in roots):
        return True
    rels = [f[len(d) + 1 :] for f in forms for d in data_roots if f.startswith(d + "/")]

    def bad(name: str) -> bool:
        return name in HOME_PROTECTED or name.startswith(".env")

    for rel in rels:  # EVERY form: the link's own path may be harmless while its target is not
        parts = rel.split("/")
        if bad(parts[0]) or (parts[0] == "profiles" and len(parts) >= 3 and bad(parts[2])):
            return True
    return False


def patch_paths(text: str) -> list[str]:
    return re.findall(
        r"(?m)^\*\*\*\s+(?:Update|Add|Delete|Move to)(?:\s+File)?:\s*(\S+)", text or ""
    )


def _mentions_protected(text: str, cwd: str) -> str:
    for tok in re.findall(r"[~/A-Za-z0-9_.\-]+", text or ""):
        if ("/" in tok or tok.startswith(".env") or tok in HOME_PROTECTED) and protected_path(
            tok, cwd
        ):
            return tok
    return ""


def hermes_cli_problem(command: str, owned) -> str:
    """Why a terminal command's `hermes ...` use is refused, or ''."""
    for part in re.split(r"[;&|\n]+", command or ""):
        if not HERMES_CMD.search(part):
            continue
        try:
            argv = shlex.split(part)
        except ValueError:
            argv = part.split()
        if "hermes" not in [os.path.basename(a) for a in argv]:
            continue
        rest = argv[[os.path.basename(a) for a in argv].index("hermes") + 1 :]
        # drop global flags: -p/--profile NAME, --accept-hooks, ...
        words, skip = [], False
        for a in rest:
            if skip:
                skip = False
                continue
            if a in ("-p", "--profile"):
                skip = True
                continue
            if a.startswith("-"):
                continue
            words.append(a)
        if (
            len(words) >= 2
            and words[0] == "config"
            and words[1] in ("set", "unset", "edit", "reset", "migrate")
        ):
            if words[1] in ("edit", "reset", "migrate"):
                return f"`hermes config {words[1]}` rewrites the whole config; use the settings profile"
            key = words[2] if len(words) > 2 else ""
            if owned is None:
                return "the owned-keys list cannot be read, so no `hermes config set` is allowed"
            if not key_is_live_writable(key, owned):
                return (
                    f"`hermes config {words[1]} {key}` changes an owned or reserved key; ask the settings "
                    "profile, which applies tier A, opens a PR for tier B and refuses tier C"
                )
        if len(words) >= 2 and words[0] == "hooks" and words[1] in ("revoke",):
            return "revoking a hook's consent is a policy change; it is tier C"
        if (
            len(words) >= 2
            and words[0] == "plugins"
            and words[1] in ("install", "enable", "disable", "remove", "uninstall")
        ):
            return "the plugin list is tier B (a PR); plugins are not installed from inside"
        if len(words) >= 2 and words[0] == "mcp" and words[1] in ("add", "remove", "configure"):
            return "MCP servers come from the reviewed catalogue; ask the settings profile (enable_mcp / add_mcp_server)"
    return ""


def escalation(payload: dict) -> dict:
    """An approval directive for a settings-service write, else {}."""
    tool = str(payload.get("tool_name") or "")
    if tool.startswith(SETTINGS_PREFIX) and tool[len(SETTINGS_PREFIX) :] in SETTINGS_WRITES:
        op = tool[len(SETTINGS_PREFIX) :]
        args = payload.get("tool_input") or {}
        what = args.get("key") or args.get("server") or args.get("title") or args.get("name") or ""
        return {
            "action": "approve",
            "rule_key": f"hub-settings:{op}:{what}"[:120],
            "message": f"settings change: {op} {what}".strip(),
        }
    return {}


def decide(payload: dict) -> str:
    """'' to allow, else the block reason."""
    tool = payload.get("tool_name") or ""
    args = payload.get("tool_input") or {}
    if not isinstance(args, dict):
        args = {}
    cwd = str(payload.get("cwd") or "")
    if tool in WRITE_TOOLS:
        paths = [args.get("path"), args.get("file_path")]
        for k in ("patch", "content", "diff"):
            if isinstance(args.get(k), str):
                paths += patch_paths(args[k])
        for p in paths:
            if isinstance(p, str) and protected_path(p, cwd):
                return (
                    f"{p} is a protected path (config, .env, plugins, hooks, subscriptions, policy)"
                )
        return ""
    if tool in SHELL_TOOLS:
        text = str(args.get("command") or args.get("code") or "")
        try:
            owned = load_owned()
        except (OSError, ValueError, KeyError):
            owned = None
        why = hermes_cli_problem(text, owned)
        if why:
            return why
        hit = _mentions_protected(text, cwd)
        if hit and WRITE_MARK.search(text):
            return f"the command writes near the protected path {hit}"
        return ""
    return ""


def main(argv: list[str] | None = None) -> int:
    global OWNED, DATA  # noqa: PLW0603 - the test-only flags below rebind the two fixed paths
    args = list(sys.argv[1:] if argv is None else argv)
    while len(args) >= 2 and args[0] in ("--owned", "--data-root"):
        if args[0] == "--owned":
            OWNED = args[1]
        else:
            DATA = args[1]
        args = args[2:]
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("payload is not an object")
        reason = decide(payload)
    except Exception as exc:  # noqa: BLE001 - fail CLOSED with a reason, whatever went wrong
        print(
            json.dumps(
                {
                    "action": "block",
                    "message": f"policy hook error: {type(exc).__name__}",
                }
            )
        )
        return 2
    if reason:
        print(json.dumps({"action": "block", "message": f"[estate policy] {reason}"}))
        return 2
    try:
        ask = escalation(payload)
    except Exception as exc:  # noqa: BLE001
        print(
            json.dumps(
                {
                    "action": "block",
                    "message": f"policy hook error: {type(exc).__name__}",
                }
            )
        )
        return 2
    print(json.dumps(ask) if ask else "{}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
