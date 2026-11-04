#!/usr/bin/env python
# SPDX-License-Identifier: GPL-3.0-or-later
"""Drive the staged managed-scope preflight through every refusal it claims.

Run by verify.yml with the role's staged directory (hermes_config_dir) and the script the ROLE
staged -- not a copy of it -- so a regression in what the role delivers is what fails.

For each case a scratch copy of the staged state is mutated in exactly one way, and the assertion
is on the EXACT set of checks that failed: a case that fails for two reasons, or for the wrong
one, proves nothing about the one it names. The positive control runs first on the unmutated copy.

Cases that cannot be exercised on this runner (an unreadable file as root) are reported as
CANNOT-TELL and counted, never skipped silently or passed.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

import yaml

# The names that re-point (or once re-pointed) a policy component; the runner's own env is cleared
# of every one so each case sets exactly what it names.
POLICY_ENV_NAMES = (
    "HERMES_MANAGED_DIR",
    "HERMES_POLICY_OWNED",
    "HERMES_POLICY_DATA_ROOT",
    "HUB_GATE_CONFIG",
)


def sha(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def main() -> int:
    staged, script, grammar_cases = sys.argv[1], sys.argv[2], sys.argv[3]
    me = os.getuid()
    work = tempfile.mkdtemp(prefix="preflight-")
    cannot_tell: list[str] = []
    failures: list[str] = []

    def fresh() -> dict:
        d = tempfile.mkdtemp(dir=work)
        shutil.copytree(f"{staged}/managed", f"{d}/managed")
        shutil.copytree(f"{staged}/policy", f"{d}/policy")
        os.chmod(f"{d}/managed/config.yaml", 0o644)
        return {
            "dir": f"{d}/managed",
            "sha": f"{d}/policy/managed.sha256",
            "owned": f"{d}/policy/owned-keys.json",
        }

    def run(
        p: dict, uid: int = me, env: dict | None = None, dotenv: list[str] | None = None
    ) -> tuple[int, list[str], str]:
        e = {k: v for k, v in os.environ.items() if k not in POLICY_ENV_NAMES}
        e.update(env or {})
        r = subprocess.run(
            [sys.executable, script, "--managed-dir", p["dir"], "--sha-file", p["sha"],
             "--owned", p["owned"], "--expect-uid", str(uid)]
            + [a for g in (dotenv or []) for a in ("--dotenv", g)],
            capture_output=True, text=True, env=e,
        )  # fmt: skip
        failed = [l.split()[1].rstrip(":") for l in r.stdout.splitlines() if l.startswith("FAIL")]
        return r.returncode, sorted(failed), r.stdout

    def expect(name: str, got: tuple[int, list[str], str], want: list[str]) -> None:
        rc, failed, out = got
        ok = failed == sorted(want) and (rc == 0) == (not want)
        print(
            f"{'ok  ' if ok else 'FAIL'} {name}: failed checks {failed} (want {sorted(want)}) rc={rc}"
        )
        if not ok:
            failures.append(name)
            print(out)

    def rewrite(p: dict, mutate, resha: bool) -> None:
        path = f"{p['dir']}/config.yaml"
        with open(path) as fh:
            doc = yaml.safe_load(fh)
        mutate(doc)
        with open(path, "w") as fh:
            yaml.safe_dump(doc, fh, sort_keys=True)
        if resha:  # keep the hash HONEST so only the key check can be what fails
            with open(p["sha"], "w") as fh:
                fh.write(f"{sha(path)}  managed/config.yaml\n")

    # ---- positive control: the staged state passes ----------------------------------------------
    p = fresh()
    expect("positive control (staged state as delivered)", run(p), [])

    # ---- the five claimed refusals + the owner/mode/key cases ----------------------------------
    p = fresh()
    os.remove(f"{p['dir']}/config.yaml")
    expect("missing managed config", run(p), ["exists"])

    p = fresh()
    with open(f"{p['dir']}/config.yaml", "w") as fh:
        fh.write("platform_toolsets: [unclosed\n  - : ::\n")
    # the hash also differs, and says so: the point is that PARSES is among the refusals
    rc, failed, out = run(p)
    ok = "parses" in failed and rc != 0
    print(f"{'ok  ' if ok else 'FAIL'} malformed managed config: failed checks {failed}")
    if not ok:
        failures.append("malformed")
        print(out)

    p = fresh()
    if me == 0:
        cannot_tell.append("unreadable managed config (running as root, chmod 000 does not bite)")
        print("CANNOT-TELL unreadable managed config: runner is root")
    else:
        os.chmod(f"{p['dir']}/config.yaml", 0)
        rc, failed, out = run(p)
        ok = "readable" in failed and rc != 0
        print(f"{'ok  ' if ok else 'FAIL'} unreadable managed config: failed checks {failed}")
        if not ok:
            failures.append("unreadable")
            print(out)

    p = fresh()
    with open(f"{p['dir']}/config.yaml", "a") as fh:
        fh.write("\n# edited by hand, still valid YAML and still the same keys\n")
    expect("wrong hash (valid YAML, same keys, one comment added)", run(p), ["hash"])

    p = fresh()
    expect(
        "HERMES_MANAGED_DIR pointing elsewhere",
        run(p, env={"HERMES_MANAGED_DIR": "/nonexistent"}),
        ["env"],
    )
    # near-miss: set to the pinned directory itself is fine
    expect(
        "HERMES_MANAGED_DIR set to the pinned directory",
        run(p, env={"HERMES_MANAGED_DIR": p["dir"]}),
        [],
    )

    # The hooks read none of these (fixed paths), so any of them in the environment is a tripwire.
    for name in POLICY_ENV_NAMES[1:]:
        expect(
            f"{name} set in the environment", run(p, env={name: "/opt/data/x.json"}), ["env-policy"]
        )
    expect(
        "near-miss: a name that only contains one (HUB_GATE_CONFIGX)",
        run(p, env={"HUB_GATE_CONFIGX": "/x"}),
        [],
    )

    p = fresh()
    expect("wrong owner expected", run(p, uid=me + 4242), ["owner"])

    p = fresh()
    os.chmod(f"{p['dir']}/config.yaml", 0o664)
    expect("group-writable mode", run(p), ["owner"])

    def drop_a_key(doc):
        doc["hooks_auto_accept"] = None
        del doc["hooks_auto_accept"]

    p = fresh()
    rewrite(p, drop_a_key, resha=True)
    expect("a tier-C key absent (hash kept honest)", run(p), ["keys"])

    def flip_value(doc):
        doc["platform_toolsets"]["api_server"] = doc["platform_toolsets"]["api_server"] + [
            "terminal"
        ]

    p = fresh()
    rewrite(p, flip_value, resha=True)
    expect("a tier-C value widened: terminal re-added (hash kept honest)", run(p), ["keys"])

    def extra_unowned(doc):
        doc["display"] = {"compact": True}

    p = fresh()
    rewrite(p, extra_unowned, resha=True)
    expect("an extra key is not a violation (near-miss for the two above)", run(p), [])

    # The user layer: a tier-C leaf upstream reads RAW (skills.*) lives in config.yaml,
    # not the managed file, so its absence from the managed file is correct -- and the same leaf
    # WITHOUT `layer: user` must still be refused, or the skip would be a hole.
    def add_owned(p: dict, entry: dict) -> None:
        with open(p["owned"]) as fh:
            doc = json.load(fh)
        doc["owned"] = [e for e in doc["owned"] if e["key"] != entry["key"]] + [entry]
        with open(p["owned"], "w") as fh:
            json.dump(doc, fh)

    p = fresh()
    add_owned(p, {"key": "skills.probe_dirs", "tier": "C", "value": ["/x"], "layer": "user"})
    expect("a user-layer tier-C leaf absent from the managed file", run(p), [])
    p = fresh()
    add_owned(p, {"key": "skills.probe_dirs", "tier": "C", "value": ["/x"]})
    expect("the same leaf in the managed layer, absent (must refuse)", run(p), ["keys"])

    p = fresh()
    with open(p["owned"]) as fh:
        owned = json.load(fh)
    owned["owned"] = [e for e in owned["owned"] if e.get("tier") != "C"]
    with open(p["owned"], "w") as fh:
        json.dump(owned, fh)
    expect("no tier-C leaf at all: the key check must not pass vacuously", run(p), ["keys"])

    # The .env override: Hermes loads $HERMES_HOME/.env with override=True AFTER the
    # preflight, so a HERMES_MANAGED_DIR line there re-points the scope with os.environ clean.
    p = fresh()
    home = tempfile.mkdtemp(dir=work)
    os.makedirs(f"{home}/profiles/coder")
    with open(f"{home}/.env", "w") as fh:
        fh.write("SOME_KEY=1\n")
    with open(f"{home}/profiles/coder/.env", "w") as fh:
        fh.write("OTHER=2\n")
    globs = [f"{home}/.env", f"{home}/profiles/*/.env"]
    expect("dotenv near-miss: .env files without the override", run(p, dotenv=globs), [])
    with open(f"{home}/profiles/coder/.env", "a") as fh:
        fh.write("export HERMES_MANAGED_DIR=/tmp/empty\n")
    expect("dotenv override in a profile .env", run(p, dotenv=globs), ["dotenv"])

    # The forms a raw `HERMES_MANAGED_DIR=` prefix match used to miss, end to end through the
    # script (one file each, so each verdict is about exactly that form), then the near-miss.
    for label, body in [
        ("double-quoted name", '"HERMES_MANAGED_DIR"=/tmp/empty\n'),
        ("single-quoted name", "'HERMES_MANAGED_DIR'=/tmp/empty\n"),
        ("spaces around =", "  HERMES_MANAGED_DIR = /tmp/empty\n"),
        ("export with a tab", "export\tHERMES_MANAGED_DIR=/tmp/empty\n"),
    ]:
        with open(f"{home}/profiles/coder/.env", "w") as fh:
            fh.write("OTHER=2\n" + body)
        expect(f"dotenv override, {label}", run(p, dotenv=globs), ["dotenv"])
    with open(f"{home}/profiles/coder/.env", "wb") as fh:
        fh.write("HERMES_MANAGED_DIR=/tmp/empty\n".encode("utf-16"))
    expect(
        "dotenv a UTF-16 .env is refused, not read as clean",
        run(p, dotenv=globs),
        ["dotenv"],
    )
    # ... and what the HOOK PROCESS loads: interpreter and loader variables, HOME
    for name in POLICY_ENV_NAMES[1:] + ("PYTHONPATH", "LD_PRELOAD", "HOME"):
        with open(f"{home}/profiles/coder/.env", "w") as fh:
            fh.write(f"OTHER=2\n{name}=/opt/data/x.json\n")
        expect(f"dotenv {name} in a profile .env", run(p, dotenv=globs), ["dotenv"])
    with open(f"{home}/profiles/coder/.env", "w") as fh:
        fh.write("OTHER=2\nHERMES_MANAGED_DIRX=/tmp/empty\n")
    expect("dotenv near-miss: HERMES_MANAGED_DIRX passes", run(p, dotenv=globs), [])

    # The full grammar table, at function level (a container run of the same file uses the
    # production readers).
    g = subprocess.run(
        [sys.executable, grammar_cases, script],
        capture_output=True, text=True,
    )  # fmt: skip
    print(g.stdout.rstrip())
    if g.returncode != 0:
        failures.append("grammar cases")
        print(g.stderr)

    shutil.rmtree(work, ignore_errors=True)
    print(f"cannot-tell: {len(cannot_tell)} {cannot_tell}")
    if failures:
        print(f"FAILED: {failures}")
        return 1
    print("all preflight cases behaved")
    return 0


if __name__ == "__main__":
    sys.exit(main())
