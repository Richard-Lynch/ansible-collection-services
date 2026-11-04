#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Managed-scope preflight for Hermes.

WHY THIS EXISTS. Upstream treats a missing, unreadable or unparseable managed config as an EMPTY
OVERLAY (fail-open) and has no built-in list of pinned keys. So "tier C is pinned" is only true if
something checks it. This script is that check, and the key list it enforces is ours: every tier-C
leaf in owned-keys.json.

It exits non-zero unless ALL of these hold, and prints one PASS/FAIL line per check so a failure
names its cause rather than reading as "unhealthy":

  env       HERMES_MANAGED_DIR, if set, is the directory the role pinned (a subprocess cannot be
            pointed at an empty overlay elsewhere)
  parser    with --consumer-root, the consumer's own readers import: Hermes' `hermes_yaml` for the
            managed file and python-dotenv's parser for .env. If either is missing the check
            FAILS rather than falling back to a different grammar.
  dotenv    no agent-writable .env (--dotenv, repeatable: $HERMES_HOME/.env and profiles/*/.env)
            sets HERMES_MANAGED_DIR, or any name a policy component was ever re-pointable by
            (_KEYS: HERMES_POLICY_OWNED, HERMES_POLICY_DATA_ROOT, HUB_GATE_CONFIG -- the hooks no
            longer read them, and a line assigning one is refused as a tripwire), or a name that
            changes what a hook process loads: PYTHON*, LD_*, HOME (_LOADER_*). Hermes loads those files with override=True AFTER this check
            runs, so a line there would re-point the managed scope with the environment looking
            correct -- the "env override" case, and the one an agent could cause itself. Each file
            is read with the grammar Hermes uses (python-dotenv's parse_stream, utf-8-sig with a
            latin-1 fallback, as hermes_cli/env_loader.py does) AND an anchored line regex, and
            either one finding the key refuses. A file neither can read with certainty (a
            statement python-dotenv cannot parse, a UTF-16/32 BOM Hermes would re-encode, a NUL
            byte) is refused too: an unparseable .env is never a clean one.
  venv      with --consumer-root, the venv the gateway and every hook run from cannot be changed
            by the agent: the realpath of <root>/.venv/bin/python, pyvenv.cfg, the venv's purelib
            site-packages directory and each .pth file in it are owned by --expect-uid and are
            neither world-writable nor group-writable by a group this (agent) process is in. `-I -B`
            does not stop the venv's own pyvenv.cfg and .pth files from being read and executed at
            interpreter start, so their being root-owned is the ONLY wall in front of them. (This
            process has already started from that venv, so the check protects the gateway and
            hooks that start after it; it cannot vouch for a .pth that ran before it.)
  exists   <managed-dir>/config.yaml exists
  readable  it can be opened
  owner     it is owned by --expect-uid and is mode 0644 (not group/world writable)
  parses    it is YAML and a mapping
  hash      its sha256 equals the one the role recorded in --sha-file
  keys      every tier-C leaf of --owned is present with the declared value (no secret is ever
            compared: tier-C secrets are refused when the managed file is built)

THE GRAMMAR IS THE CONSUMER'S. In the container (--consumer-root /opt/hermes) the managed file is
parsed by Hermes' own hermes_yaml.safe_load (ruamel underneath; the image has NO PyYAML, so a
PyYAML import would refuse at `parses` with ModuleNotFoundError), and .env files by
python-dotenv. Without --consumer-root (molecule, the controller) it uses PyYAML and, if importable,
python-dotenv, else the anchored regex alone. Run by preflight-entry.sh as the container's main
program BEFORE main-wrapper.sh starts the gateway, as the unprivileged hermes user (the reader that
matters: a root-only file passes for root and is unreadable to the gateway).
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import io
import json
import os
import re
import stat
import sys

_MISSING = object()
_KEY = "HERMES_MANAGED_DIR"
# Every environment name that re-points (or once re-pointed) a policy component: the managed scope,
# the policy hook's owned-keys list and data root, the send gate's config. Hermes loads the
# agent-writable .env files with override=True, so a line assigning ANY of these is refused. The
# hooks read none of the last three (fixed paths, no override), so their presence is a tripwire.
_KEYS = (_KEY, "HERMES_POLICY_OWNED", "HERMES_POLICY_DATA_ROOT", "HUB_GATE_CONFIG")
# And every name that changes what the HOOK PROCESS loads or resolves, which the same .env reaches
# (Hermes hands its environment to each hook): any PYTHON* variable (the hooks run `python -I`,
# which ignores them, so this is a second wall), any LD_* loader variable (LD_PRELOAD and
# LD_LIBRARY_PATH run code before any interpreter flag is read: nothing but a refusal stops them),
# and HOME, through which the hooks expand a `~` path. PATH is not listed: every hook names its
# interpreter by absolute path and none spawns a program.
_LOADER_NAMES = ("HOME",)
_LOADER_PREFIXES = ("PYTHON", "LD_")
_NAME_RE = (
    "|".join(_KEYS + _LOADER_NAMES) + "|" + "|".join(p + "[A-Za-z0-9_]*" for p in _LOADER_PREFIXES)
)


def refused_name(name: str) -> bool:
    """True for a name no agent-writable .env may assign."""
    return name in _KEYS or name in _LOADER_NAMES or name.startswith(_LOADER_PREFIXES)


# Any line that ASSIGNS the key, in every form a dotenv reader might accept: leading space, `export`,
# a single- or double-quoted name, space before the `=`. Deliberately wider than python-dotenv (which
# would bind a double-quoted name literally, quotes included): a false refusal costs a restart, a
# missed override costs the managed scope. (?m) so `^` is each line, not only the first (ANCHOR-01).
_ASSIGN_RE = re.compile(r"(?m)^[ \t]*(?:export[ \t]+)?(['\"]?)(" + _NAME_RE + r")\1[ \t]*=")
_WIDE_BOMS = (b"\xff\xfe", b"\xfe\xff", b"\x00\x00\xfe\xff")


def load_consumer(root: str):
    """Put the consumer's tree first on sys.path and return (yaml_load, dotenv_parse_stream).

    Mirrors the image's own launcher: the repo root, then the venv's site-packages via addsitedir
    (so its .pth files apply). Raises ImportError if either reader is absent.
    """
    import site
    import sysconfig

    site.addsitedir(sysconfig.get_paths()["purelib"])
    sys.path.insert(0, root)
    import hermes_yaml  # noqa: E402 - the consumer's reader, by design
    from dotenv.parser import parse_stream  # noqa: E402

    return hermes_yaml.safe_load, parse_stream


def venv_paths(root: str) -> tuple[list[str], str]:
    """(paths, problem): what an interpreter from <root>/.venv reads and executes at startup.

    Each path is its realpath, since a root-owned symlink to an agent-writable file is that file.
    problem is non-empty when the layout cannot be resolved (no interpreter, no pyvenv.cfg, not
    exactly one site-packages), which is refused rather than read as "nothing to check".
    """
    venv = os.path.join(root, ".venv")
    py, cfg = os.path.join(venv, "bin", "python"), os.path.join(venv, "pyvenv.cfg")
    for p in (py, cfg):
        if not os.path.exists(p):
            return [], f"{p} is missing"
    sites = sorted(glob.glob(os.path.join(venv, "lib", "python*", "site-packages")))
    if len(sites) != 1:
        return (
            [],
            f"want exactly one {venv}/lib/python*/site-packages, found {len(sites)}",
        )
    paths = [py, cfg, sites[0]] + sorted(glob.glob(os.path.join(sites[0], "*.pth")))
    return [os.path.realpath(p) for p in paths], ""


def agent_can_write(st: os.stat_result, owner_uid: int, agent_uid: int, agent_gids) -> str:
    """Why the agent (agent_uid, agent_gids) could change a file with this stat, or ''.

    Mode bits only (no ACLs: the image sets none). The owner must be owner_uid and must not be the
    agent; no world write; group write only for a group the agent is not in.
    """
    mode = stat.S_IMODE(st.st_mode)
    if st.st_uid != owner_uid:
        return f"uid={st.st_uid} (want {owner_uid})"
    if st.st_uid == agent_uid:
        return f"owned by the agent's own uid {agent_uid}"
    if mode & stat.S_IWOTH:
        return f"mode={mode:04o} is world-writable"
    if mode & stat.S_IWGRP and st.st_gid in set(agent_gids):
        return f"mode={mode:04o} is group-writable by the agent's gid {st.st_gid}"
    return ""


def scan_dotenv(raw: bytes, parse_stream) -> tuple[str, str]:
    """Classify one .env: ('planted'|'unparseable'|'clean', detail).

    parse_stream is python-dotenv's parser, or None when it is unavailable (regex only).
    """
    if raw.startswith(_WIDE_BOMS) or b"\x00" in raw:
        # Hermes re-encodes a UTF-16 file before loading it; a byte scan of UTF-8 would miss the key.
        return "unparseable", "a UTF-16/32 BOM or a NUL byte"
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")  # what env_loader falls back to, so it is what gets loaded
    hit = _ASSIGN_RE.search(text)
    if hit:
        return "planted", "a line assigns " + hit.group(2)
    if parse_stream is None:
        return "clean", "regex only (python-dotenv not importable here)"
    for binding in parse_stream(io.StringIO(text)):
        if binding.error:
            return (
                "unparseable",
                f"python-dotenv cannot parse {binding.original.string[:40]!r}",
            )
        # value None is a bare name with no `=`: env_loader skips it, so it re-points nothing.
        if binding.key is not None and refused_name(binding.key) and binding.value is not None:
            return "planted", f"python-dotenv binds {binding.key}"
    return "clean", "python-dotenv and the line regex"


def _get(doc, segs):
    cur = doc
    for s in segs:
        if not isinstance(cur, dict) or s not in cur:
            return _MISSING
        cur = cur[s]
    return cur


def run(args) -> list[tuple[bool, str, str]]:
    results: list[tuple[bool, str, str]] = []

    def check(ok: bool, name: str, detail: str) -> bool:
        results.append((ok, name, detail))
        return ok

    env_dir = os.environ.get("HERMES_MANAGED_DIR")
    if env_dir is not None and os.path.normpath(env_dir) != os.path.normpath(args.expect_dir):
        check(
            False,
            "env",
            f"HERMES_MANAGED_DIR={env_dir!r} but the role pins {args.expect_dir!r}",
        )
    else:
        check(True, "env", "HERMES_MANAGED_DIR is unset or the pinned directory")
    planted_env = [k for k in _KEYS[1:] if k in os.environ]
    check(
        not planted_env,
        "env-policy",
        (
            (
                f"{', '.join(planted_env)} set in the environment: nothing may re-point a policy component"
            )
            if planted_env
            else "no policy-component override name is set"
        ),
    )

    yaml_load, parse_stream, parser_error = None, None, None
    if args.consumer_root:
        try:
            yaml_load, parse_stream = load_consumer(args.consumer_root)
        except Exception as exc:  # noqa: BLE001 - any import failure is the finding
            parser_error = f"{type(exc).__name__}: {exc}"
        check(
            parser_error is None,
            "parser",
            parser_error or f"hermes_yaml and python-dotenv from {args.consumer_root}",
        )
        # This process runs AS the agent (preflight-entry.sh: s6-setuidgid hermes), so its own ids
        # are the ones that must not be able to write the venv.
        agent_gids = {os.getgid(), *os.getgroups()}
        paths, problem = venv_paths(args.consumer_root)
        writable = [problem] if problem else []
        for p in paths:
            try:
                why = agent_can_write(os.stat(p), args.expect_uid, os.getuid(), agent_gids)
            except OSError as exc:
                why = exc.strerror or type(exc).__name__
            if why:
                writable.append(f"{p}: {why}")
        check(
            not writable,
            "venv",
            (
                "; ".join(writable)
                if writable
                else f"{len(paths)} venv startup paths owned by uid {args.expect_uid}, not agent-writable"
            ),
        )
    else:
        try:
            import yaml

            yaml_load = yaml.safe_load
        except ImportError as exc:
            parser_error = f"PyYAML: {exc}"
        try:
            from dotenv.parser import parse_stream
        except ImportError:
            parse_stream = None  # the regex still runs; scan_dotenv says which grammar decided

    refused, read_with = [], set()
    for pattern in args.dotenv or []:
        for envfile in sorted(glob.glob(pattern)):
            try:
                with open(envfile, "rb") as fh:
                    raw = fh.read()
            except OSError:
                continue  # unreadable to the hermes user means unreadable to the agent's loader too
            if args.consumer_root and parse_stream is None:
                refused.append(f"{envfile} (no python-dotenv to read it with)")
                continue
            verdict, detail = scan_dotenv(raw, parse_stream)
            if verdict == "clean":
                read_with.add(detail)
            else:
                refused.append(f"{envfile} ({verdict}: {detail})")
    check(
        not refused,
        "dotenv",
        (
            f"refused {refused}"
            if refused
            else f"no .env re-points the managed scope ({'; '.join(sorted(read_with)) or 'none present'})"
        ),
    )

    path = os.path.join(args.managed_dir, "config.yaml")
    if not check(os.path.isfile(path), "exists", path):
        return results

    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        check(False, "readable", f"{path}: {exc.strerror}")
        return results
    check(True, "readable", path)

    st = os.stat(path)
    mode = stat.S_IMODE(st.st_mode)
    check(
        st.st_uid == args.expect_uid and mode == 0o644,
        "owner",
        f"uid={st.st_uid} (want {args.expect_uid}) mode={mode:04o} (want 0644)",
    )

    doc = None
    try:
        if yaml_load is None:
            raise ImportError(parser_error or "no YAML reader")
        # utf-8-sig: what hermes_cli/managed_scope.py decodes the managed file with.
        doc = yaml_load(raw.decode("utf-8-sig"))
        check(
            isinstance(doc, dict),
            "parses",
            (
                "a YAML mapping"
                if isinstance(doc, dict)
                else f"parsed to {type(doc).__name__}, not a mapping"
            ),
        )
    except Exception as exc:  # noqa: BLE001 - any parse failure is the finding
        check(False, "parses", f"{type(exc).__name__}: {str(exc)[:120]}")

    try:
        with open(args.sha_file, encoding="utf-8") as fh:
            want = fh.read().split()[0]
    except (OSError, IndexError) as exc:
        check(False, "hash", f"cannot read the recorded sha256 at {args.sha_file}: {exc}")
    else:
        have = hashlib.sha256(raw).hexdigest()
        check(have == want, "hash", f"file {have[:12]} vs recorded {want[:12]}")

    try:
        with open(args.owned, encoding="utf-8") as fh:
            owned = json.load(fh)["owned"]
    except (OSError, ValueError, KeyError) as exc:
        check(False, "keys", f"cannot read the owned-keys document {args.owned}: {exc}")
        return results
    # A `layer: user` tier-C leaf is read RAW by upstream (skills.*), so it lives in the user
    # config.yaml and is deliberately NOT pinned here (a pin gets it stripped by the next
    # save_config writer). The merge writes it and hub_probe judges it in its consumer's view.
    tier_c = [e for e in owned if e.get("tier") == "C" and e.get("layer", "managed") != "user"]
    # An empty pinned list would make this check vacuous; say so as a failure, never a pass.
    if not tier_c:
        check(
            False,
            "keys",
            "owned-keys.json carries no tier-C leaf, so there is nothing to pin",
        )
        return results
    bad = []
    for entry in tier_c:
        if entry.get("secret"):
            bad.append(f"{entry['key']} (secret: must not be a managed key)")
            continue
        have = _get(doc, entry["key"].split(".")) if isinstance(doc, dict) else _MISSING
        if have is _MISSING:
            bad.append(f"{entry['key']} absent")
        elif have != entry["value"]:
            bad.append(f"{entry['key']} = {have!r}, want {entry['value']!r}")
    check(not bad, "keys", f"{len(tier_c)} tier-C leaves" if not bad else "; ".join(bad))
    return results


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--managed-dir", default="/etc/hermes")
    ap.add_argument(
        "--expect-dir",
        default=None,
        help="directory the role pins (default: --managed-dir)",
    )
    ap.add_argument("--sha-file", required=True)
    ap.add_argument("--owned", required=True)
    ap.add_argument("--expect-uid", type=int, default=0)
    ap.add_argument(
        "--consumer-root",
        default=None,
        help="the consumer's code root (/opt/hermes): parse with ITS readers, failing if absent",
    )
    ap.add_argument(
        "--dotenv",
        action="append",
        help="glob of an agent-loaded .env to scan (repeatable)",
    )
    args = ap.parse_args(argv)
    if args.expect_dir is None:
        args.expect_dir = args.managed_dir
    results = run(args)
    for ok, name, detail in results:
        print(f"{'PASS' if ok else 'FAIL'} {name}: {detail}")
    failed = [n for ok, n, _ in results if not ok]
    print("preflight: ok" if not failed else f"preflight: REFUSED ({', '.join(failed)})")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
