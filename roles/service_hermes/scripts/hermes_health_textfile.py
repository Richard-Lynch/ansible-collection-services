#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Write hermes_gateway_healthy for the node exporter's textfile collector.

Reads the dashboard's /api/status -- unauthenticated by upstream design, so this needs NO
credential on the host -- and reports 1 only when the daemon itself says the gateway is running
(`gateway_running: true` AND `gateway_state: running`). Everything else is 0, including "nothing
answered": a container that is down, restart-looping on a refused preflight, or a dashboard whose
gateway died all read as unhealthy, which is what a gateway-unhealthy alert fires on.

The probe's own timestamp is written too, so a timer that stops running leaves a visibly stale
series rather than a frozen 1. Atomic write (temp + rename) so a scrape never sees half a file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request


def probe(url: str, timeout: float) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - loopback URL
            doc = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - every failure to read is "unhealthy", named
        return 0, type(exc).__name__
    if not isinstance(doc, dict):
        return 0, "not-a-mapping"
    ok = doc.get("gateway_running") is True and doc.get("gateway_state") == "running"
    return (1 if ok else 0), str(doc.get("gateway_state"))


def hooks(container: str, user: str, expect: list[str], timeout: float) -> tuple[int, int]:
    """(probe_ok, registered): how many EXPECTED hook commands `hermes hooks list` shows allowed.

    "Allowed" is the consent the gateway needs to register a configured hook in a non-TTY run
    (hooks_auto_accept records it at registration), so an expected command missing from that list,
    or present as "not allowlisted", is a hook that denies nothing. A probe that
    cannot run is probe_ok 0 AND registered 0: it fails towards the alert, never towards "fine".
    """
    try:
        res = subprocess.run(
            ["docker", "exec", "--user", user, container, "hermes", "hooks", "list"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0, 0
    if res.returncode != 0:
        return 0, 0
    allowed = allowed_commands(res.stdout)
    return 1, sum(1 for cmd in expect if cmd in allowed)


# `- <command>[ matcher='<re>'] (timeout=<n>s, <mark> allowed)`. The COMMAND is everything before the
# matcher or the parenthesis, so a command with arguments (an interpreter, -I -B, then the script) is
# compared whole: the first word alone would be the interpreter, the same for every hook.
_HOOK_LINE = re.compile(r"- (?P<cmd>.+?)(?: matcher='.*')? \(timeout=[^)]*\)$")


def allowed_commands(text: str) -> set[str]:
    """The hook commands `hermes hooks list` shows as allowed (never 'not allowlisted')."""
    allowed = set()
    for line in text.splitlines():
        s = line.strip()
        m = _HOOK_LINE.match(s)
        if m and "allowed)" in s and "not allowlisted" not in s:
            allowed.add(m.group("cmd"))
    return allowed


def build_parser() -> argparse.ArgumentParser:
    """The CLI, apart from main() so a test can parse a rendered unit's ExecStart through it.

    --hooks-expect takes ONE whole hook command, which contains spaces (the interpreter, -I -B,
    the script), so the systemd unit must pass each as a single quoted argument. The molecule
    scenario renders the unit and parses its ExecStart with this parser to prove it does.
    """
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--url", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--timeout", type=float, default=5.0)
    ap.add_argument("--hooks-container", default="")
    ap.add_argument("--hooks-user", default="hermes")
    ap.add_argument("--hooks-expect", action="append", default=[])
    return ap


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    healthy, state = probe(a.url, a.timeout)
    body = (
        "# HELP hermes_gateway_healthy 1 when /api/status reports the gateway running.\n"
        "# TYPE hermes_gateway_healthy gauge\n"
        f"hermes_gateway_healthy {healthy}\n"
        "# HELP hermes_gateway_probe_timestamp_seconds When the gateway health was last probed.\n"
        "# TYPE hermes_gateway_probe_timestamp_seconds gauge\n"
        f"hermes_gateway_probe_timestamp_seconds {int(time.time())}\n"
    )
    if a.hooks_container:
        ok, registered = hooks(a.hooks_container, a.hooks_user, a.hooks_expect, 30.0)
        body += (
            "# HELP hermes_hooks_expected Shell hooks the role declares (hermes_policy_hook_enabled).\n"
            "# TYPE hermes_hooks_expected gauge\n"
            f"hermes_hooks_expected {len(a.hooks_expect)}\n"
            "# HELP hermes_hooks_registered Declared hooks `hermes hooks list` shows allowed.\n"
            "# TYPE hermes_hooks_registered gauge\n"
            f"hermes_hooks_registered {registered}\n"
            "# HELP hermes_hooks_probe_ok 1 when `hermes hooks list` could be read at all.\n"
            "# TYPE hermes_hooks_probe_ok gauge\n"
            f"hermes_hooks_probe_ok {ok}\n"
        )
    tmp = f"{a.out}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(body)
    os.chmod(tmp, 0o644)
    os.replace(tmp, a.out)
    print(f"hermes_gateway_healthy={healthy} state={state}")
    if a.hooks_container:
        print(f"hermes_hooks_registered={registered} expected={len(a.hooks_expect)} probe_ok={ok}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
