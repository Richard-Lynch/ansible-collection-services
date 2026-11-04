#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Fixtures for the preflight's .env and YAML grammar, in both directions.

The .env check used to match a raw `HERMES_MANAGED_DIR=` prefix, so a quoted name, a space before
the `=`, or `export` with a tab slipped past it while python-dotenv -- which is what Hermes loads
.env files with -- would still have bound the key. Every form below that ASSIGNS the key must be
refused, every near-miss must pass, and a file the grammar cannot read must be refused rather than
reported clean.

Stdlib only, so it runs in three places:
  * molecule / the controller:   preflight_grammar_cases.py <policy_preflight.py>
    (no python-dotenv there: the regex path, reported as such)
  * inside the container:        ... <policy_preflight.py> --consumer-root /opt/hermes
    (Hermes' own hermes_yaml and python-dotenv; FAILS if either cannot be imported, so the
    production grammar is never silently replaced by the fallback)
A play can run the second form inside the live container as the hermes user.
"""

from __future__ import annotations

import argparse
import sys
import types

KEY = "HERMES_MANAGED_DIR"

# (name, bytes, verdict). `either` = refused, as planted OR unparseable (the grammars may differ on
# WHICH, never on whether).
DOTENV_CASES: list[tuple[str, bytes, str]] = [
    ("bare", b"A=1\nHERMES_MANAGED_DIR=/tmp/x\n", "planted"),
    ("double-quoted name", b'"HERMES_MANAGED_DIR"=/tmp/x\n', "planted"),
    ("single-quoted name", b"'HERMES_MANAGED_DIR'=/tmp/x\n", "planted"),
    ("spaces around =", b"  HERMES_MANAGED_DIR = /tmp/x\n", "planted"),
    ("export", b"export HERMES_MANAGED_DIR=/tmp/x\n", "planted"),
    (
        "export with a tab and spaces",
        b"export\t  HERMES_MANAGED_DIR\t= '/tmp/x'\n",
        "planted",
    ),
    ("last line, no newline", b"A=1\nHERMES_MANAGED_DIR=/tmp/x", "planted"),
    ("CRLF line endings", b"A=1\r\nHERMES_MANAGED_DIR=/tmp/x\r\n", "planted"),
    ("UTF-8 BOM before the key", b"\xef\xbb\xbfHERMES_MANAGED_DIR=/tmp/x\n", "planted"),
    (
        "not UTF-8 (latin-1 fallback)",
        b"A=caf\xe9\nHERMES_MANAGED_DIR=/tmp/x\n",
        "planted",
    ),
    (
        "UTF-16 (Hermes re-encodes it)",
        "HERMES_MANAGED_DIR=/tmp/x\n".encode("utf-16"),
        "unparseable",
    ),
    ("a NUL byte", b"A=1\x00\nB=2\n", "unparseable"),
    # near-misses: must PASS
    ("near-miss: HERMES_MANAGED_DIRX", b"HERMES_MANAGED_DIRX=/tmp/x\n", "clean"),
    ("near-miss: a prefixed name", b"X_HERMES_MANAGED_DIR=/tmp/x\n", "clean"),
    ("near-miss: a comment", b"# HERMES_MANAGED_DIR=/tmp/x\n", "clean"),
    ("near-miss: the key inside a value", b"NOTE=HERMES_MANAGED_DIR=/tmp/x\n", "clean"),
    ("near-miss: no assignment", b"HERMES_MANAGED_DIR\n", "clean"),
    ("near-miss: empty file", b"", "clean"),
    # The other names that re-point (or once re-pointed) a policy component: the policy hook's
    # owned-keys list and data root, and the send gate's config. Each refused, each with a near-miss.
    ("HERMES_POLICY_OWNED", b"HERMES_POLICY_OWNED=/opt/data/x.json\n", "planted"),
    ("HERMES_POLICY_DATA_ROOT, exported", b"export HERMES_POLICY_DATA_ROOT=/tmp\n", "planted"),
    ("HUB_GATE_CONFIG, quoted name", b'"HUB_GATE_CONFIG"=/opt/data/x.json\n', "planted"),
    ("HUB_GATE_CONFIG, spaces around =", b"  HUB_GATE_CONFIG = /opt/data/x.json\n", "planted"),
    ("near-miss: HUB_GATE_CONFIGX", b"HUB_GATE_CONFIGX=/x\n", "clean"),
    ("near-miss: a prefixed HERMES_POLICY_OWNED", b"X_HERMES_POLICY_OWNED=/x\n", "clean"),
    ("near-miss: HERMES_POLICY_DATA_ROOT in a comment", b"# HERMES_POLICY_DATA_ROOT=/x\n", "clean"),
    # What the hook PROCESS loads: interpreter and loader variables, and HOME (the hooks expand `~`).
    ("PYTHONPATH", b"PYTHONPATH=/opt/data/workspace\n", "planted"),
    ("PYTHONHOME, exported", b"export PYTHONHOME=/opt/data/py\n", "planted"),
    ("PYTHONSTARTUP, quoted name", b"'PYTHONSTARTUP'=/opt/data/s.py\n", "planted"),
    ("LD_PRELOAD", b"LD_PRELOAD=/opt/data/x.so\n", "planted"),
    ("LD_LIBRARY_PATH, spaces around =", b"  LD_LIBRARY_PATH = /opt/data/lib\n", "planted"),
    ("HOME", b"HOME=/tmp\n", "planted"),
    ("near-miss: an unrelated variable", b"GH_TOKEN=x\nWORKER_URL=http://w\n", "clean"),
    ("near-miss: a name ending in PYTHONPATH", b"MY_PYTHONPATH=/x\n", "clean"),
    ("near-miss: HOMEPAGE_URL", b"HOMEPAGE_URL=https://x\n", "clean"),
    ("near-miss: OLD_PRELOAD (not LD_)", b"OLD_PRELOAD=1\n", "clean"),
]
# Only python-dotenv can see these; the regex alone (no grammar) reports them clean, which is why
# the container run REQUIRES python-dotenv rather than accepting the fallback.
DOTENV_GRAMMAR_ONLY: list[tuple[str, bytes, str]] = [
    ("an unterminated quote", b'A="never closed\nB=2\n', "unparseable"),
]


def load(path: str) -> types.ModuleType:
    """The preflight under test, from a path or (`-`) from stdin, so the container run needs no file."""
    mod = types.ModuleType("policy_preflight")
    if path == "-":
        src = sys.stdin.read()
    else:
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
    exec(compile(src, "policy_preflight.py", "exec"), mod.__dict__)  # noqa: S102 - under test
    return mod


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("preflight")
    ap.add_argument("--consumer-root", default=None)
    args = ap.parse_args()
    pf = load(args.preflight)
    failures: list[str] = []

    parse_stream, yaml_load = None, None
    if args.consumer_root:
        try:
            yaml_load, parse_stream = pf.load_consumer(args.consumer_root)
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL consumer readers did not import from {args.consumer_root}: {exc!r}")
            return 1
        print(f"grammar: python-dotenv + hermes_yaml from {args.consumer_root}")
    else:
        try:
            from dotenv.parser import parse_stream  # type: ignore[no-redef]
        except ImportError:
            parse_stream = None
        print(f"grammar: {'python-dotenv' if parse_stream else 'line regex only'}")

    cases = DOTENV_CASES + (DOTENV_GRAMMAR_ONLY if parse_stream else [])
    for name, raw, want in cases:
        got, detail = pf.scan_dotenv(raw, parse_stream)
        ok = got == want
        print(f"{'ok  ' if ok else 'FAIL'} dotenv {name}: {got} (want {want}) -- {detail}")
        if not ok:
            failures.append(name)

    if yaml_load is not None:
        good = yaml_load("approvals:\n  mode: manual\nplatform_toolsets: {api_server: [web]}\n")
        ok = isinstance(good, dict) and good["approvals"]["mode"] == "manual"
        print(f"{'ok  ' if ok else 'FAIL'} yaml: a mapping parses with the consumer's reader")
        if not ok:
            failures.append("yaml good")
        try:
            yaml_load("approvals: [unterminated\n  mode: : off\n")
        except Exception as exc:  # noqa: BLE001
            print(f"ok   yaml: malformed input raises ({type(exc).__name__})")
        else:
            print("FAIL yaml: malformed input parsed without error")
            failures.append("yaml malformed")

    print(f"FAILED: {failures}" if failures else f"all {len(cases)} grammar cases behaved")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
