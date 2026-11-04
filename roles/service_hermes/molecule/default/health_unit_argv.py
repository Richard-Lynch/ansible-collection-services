#!/usr/bin/env python
# SPDX-License-Identifier: GPL-3.0-or-later
"""Parse a RENDERED hermes-health-textfile.service's ExecStart through the probe's REAL parser.

Usage: health_unit_argv.py <unit file> <scripts dir> <expected hook command>...

The expected hook commands contain spaces (interpreter, -I -B, script), so the unit must hand each
to the probe as ONE argument. Rendered unquoted, systemd splits it and argparse exits 2 on every
timer run, the textfile goes stale and the hooks/gateway gauges freeze at their last values.

The ExecStart line is split twice and both must agree: by shlex (POSIX shell rules) and by the
stricter systemd.service(5) rule, which honours a quote ONLY when it opens an item (after unquoted
whitespace or at the start) and closes it before whitespace or end of line. A mid-word quote
(--hooks-expect="a b") is a single item under shlex but is split by systemd, so shlex alone would
pass a unit systemd breaks. The argv after the interpreter and script then goes through
hermes_health_textfile.build_parser() -- the parser the timer runs -- and --hooks-expect must come
back as exactly the expected commands. Exit 0 when it does, 1 with the reason otherwise.
"""

from __future__ import annotations

import importlib.util
import os
import shlex
import sys

PREFIX = [
    "/usr/bin/env",
    "python3",
    "/usr/local/lib/hermes-health/hermes_health_textfile.py",
]


def systemd_split(line: str) -> list[str]:
    """Split as systemd.service(5) does, for the subset this unit uses (no escapes, no specifiers)."""
    out: list[str] = []
    i, n = 0, len(line)
    while i < n:
        while i < n and line[i] in " \t":
            i += 1
        if i >= n:
            break
        if line[i] in "\"'":
            q = line[i]
            j = line.find(q, i + 1)
            if j < 0:
                raise ValueError(f"unterminated {q} at column {i}")
            if j + 1 < n and line[j + 1] not in " \t":
                raise ValueError(f"closing {q} at column {j} is not followed by whitespace")
            out.append(line[i + 1 : j])
            i = j + 1
        else:
            j = i
            while j < n and line[j] not in " \t":
                j += 1
            out.append(line[i:j])
            i = j
    return out


def main(argv: list[str]) -> int:
    unit, scripts, expected = argv[0], argv[1], argv[2:]
    with open(unit, encoding="utf-8") as fh:
        lines = [ln for ln in fh.read().splitlines() if ln.startswith("ExecStart=")]
    if len(lines) != 1:
        print(f"FAIL: expected one ExecStart= line, found {len(lines)}")
        return 1
    line = lines[0][len("ExecStart=") :]
    for ch in "%$\\":
        if ch in line:
            print(f"FAIL: ExecStart carries {ch!r}, which systemd expands")
            return 1
    try:
        sd = systemd_split(line)
    except ValueError as exc:
        print(f"FAIL: systemd would not split ExecStart cleanly: {exc}")
        return 1
    sh = shlex.split(line)
    if sd != sh:
        print(
            f"FAIL: systemd and shlex split ExecStart differently:\n  systemd={sd}\n  shlex  ={sh}"
        )
        return 1
    if sd[: len(PREFIX)] != PREFIX:
        print(f"FAIL: ExecStart does not start with {PREFIX}: {sd[:3]}")
        return 1
    spec = importlib.util.spec_from_file_location(
        "hermes_health_textfile", os.path.join(scripts, "hermes_health_textfile.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    try:
        ns = mod.build_parser().parse_args(sd[len(PREFIX) :])
    except SystemExit as exc:
        print(f"FAIL: the probe's own argparse refused the rendered argv (exit {exc.code})")
        return 1
    if ns.hooks_expect != expected:
        print(f"FAIL: --hooks-expect parsed as {ns.hooks_expect!r}, expected {expected!r}")
        return 1
    print(f"OK: {len(expected)} hook command(s) reach the probe whole")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
