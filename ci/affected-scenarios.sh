#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# affected-scenarios.sh — which molecule scenarios does a change actually need?
#
# Prints a JSON object:
#   {"all": bool, "reason": str, "count": N, "roles": [role names...],
#    "playbook_scenarios": ["playbooks/<dir>/molecule/<name>", ...]}
# `count` is roles + playbook_scenarios: the number of scenarios the matrix will run.
#
# PLAYBOOK-LEVEL SCENARIOS (playbooks/*/molecule/<name>/) are easy to leave unselected, in which case
# their verify assertions never run in CI (MOLVERIFY-05). They test a playbook or a role through
# fakes and their dependencies are not mapped, so they FAIL OPEN as a group: all of them whenever
# anything non-inert changes under roles/ or playbooks/, and on every ALL.
#
# WHY. Running every scenario serially on every PR costs minutes, of which a one-role change
# needs a few SECONDS. The scenarios are independent, so the loop is pure waste — but selecting
# a subset is exactly where a gate quietly stops covering things, so the rules below are written
# to over-select and the failure mode is deliberate.
#
# FAILS OPEN TO "RUN EVERYTHING", never to "run nothing". A docs-only detector that fails the
# other way (a SIGPIPE makes a large change report docs_only=true) skips every test and scanner
# and goes green in seconds. Anything this
# script cannot classify with certainty selects ALL:
#
#   * no diff resolved, a git error, an empty base          -> ALL
#   * a shared path changed (ci/, .github/, Makefile, the   -> ALL
#     lint configs, requirements, the repo's own collections/)
#   * a role changed whose name maps to no scenario         -> ALL (the mapping may be stale)
#   * anything under roles/ that is not roles/<name>/...    -> ALL
#
# A role with a scenario selects THAT scenario. A role WITHOUT one selects nothing extra — there
# is no scenario to run — but it is still reported, so "changed a role, ran no scenario" is
# visible rather than silent.
#
# Usage:
#   ci/affected-scenarios.sh                       # diff origin/main...HEAD
#   ci/affected-scenarios.sh BASE HEAD             # diff BASE..HEAD
#   CHANGED_FILES="a b c" ci/affected-scenarios.sh # explicit list (testing)
set -uo pipefail

# DEVKIT_ROOT (the root rule of ansible_devkit/checks/_devkit_config.py) wins, so the selector answers for
# the repo being checked even when this script is shipped from somewhere else; otherwise the repo
# this script sits in.
REPO_ROOT="${DEVKIT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$REPO_ROOT"

emit_all() {
  # $1 = reason. Lists every role that HAS a scenario, so the matrix is explicit either way.
  python3 - "$1" <<'PY'
import glob, json, os, re, subprocess, sys
roles = subprocess.run(
    "ls -d roles/*/molecule/default 2>/dev/null | cut -d/ -f2 | sort",
    shell=True, capture_output=True, text=True).stdout.split()
pbs = sorted(os.path.dirname(m) for m in glob.glob("playbooks/*/molecule/*/molecule.yml")
             if not re.search(r"^# ci-exempt:[ \t]*\S", open(m).read(), re.M))
print(json.dumps({"all": True, "reason": sys.argv[1], "count": len(roles) + len(pbs),
                  "roles": roles, "playbook_scenarios": pbs}))
PY
  exit 0
}

# --- resolve the changed files ------------------------------------------------------------------
# `${CHANGED_FILES+set}` distinguishes SET-BUT-EMPTY from UNSET, which `-n` cannot. An explicitly
# empty list means "the caller resolved a diff and it was empty" — a case that must fail open on
# its own terms, not silently fall through to `git diff` and report whatever the working tree
# happens to hold. That fallthrough made an empty-list test report "global path changed:
# .ansible-lint.yml": the right answer (ALL) for entirely the wrong reason, which is the shape
# that hides a broken selector.
if [ -n "${CHANGED_FILES+set}" ]; then
  changed="$(printf '%s\n' $CHANGED_FILES)"
elif [ $# -eq 2 ]; then
  changed="$(git diff --name-only "$1" "$2" 2>/dev/null)" || emit_all "git diff failed"
else
  changed="$(git diff --name-only origin/main...HEAD 2>/dev/null)" || emit_all "git diff failed"
fi

# An empty diff is NOT "nothing to test" — it is a diff we failed to resolve. Fail open.
[ -z "$changed" ] && emit_all "no changed files resolved"

# --- classify ------------------------------------------------------------------------------------
CHANGED="$changed" python3 <<'PY'
import glob, json, os, re, subprocess, sys

changed = [f for f in os.environ["CHANGED"].splitlines() if f.strip()]

have = set(subprocess.run(
    "ls -d roles/*/molecule/default 2>/dev/null | cut -d/ -f2",
    shell=True, capture_output=True, text=True).stdout.split())
# A scenario whose molecule.yml carries `# ci-exempt: <reason>` cannot run on CI's Ubuntu (e.g. it
# needs macOS's hdiutil). It is left out here, and check_molecule_verify.py requires the reason.
playbook_scenarios = sorted(
    os.path.dirname(m) for m in glob.glob("playbooks/*/molecule/*/molecule.yml")
    if not re.search(r"^# ci-exempt:[ \t]*\S", open(m).read(), re.M))


def emit_all(reason):
    print(json.dumps({"all": True, "reason": reason,
                      "count": len(have) + len(playbook_scenarios), "roles": sorted(have),
                      "playbook_scenarios": playbook_scenarios}))
    sys.exit(0)


# Paths that can change how ANY scenario behaves. Deliberately broad: the cost of listing one
# path too many is a full run that would have passed anyway. The generic Ansible layout is here;
# a repo's own shared trees (this one's lib/, tools/, scripts/) come from devkit.toml
# [molecule] selector_global_prefixes. An unreadable devkit.toml selects ALL — never fewer.
GLOBAL_PREFIXES = (
    "ci/", ".github/", "collections/",
    "inventories/", "group_vars/", "host_vars/",
)
if os.path.exists("devkit.toml"):
    try:
        import tomllib
        with open("devkit.toml", "rb") as fh:
            _extra = tomllib.load(fh).get("molecule", {}).get("selector_global_prefixes", [])
        if not (isinstance(_extra, list) and all(isinstance(p, str) for p in _extra)):
            raise ValueError("molecule.selector_global_prefixes must be a list of strings")
        GLOBAL_PREFIXES += tuple(_extra)
    except Exception as exc:  # noqa: BLE001 — fail open, with the reason on record
        emit_all(f"devkit.toml unreadable: {exc}")
GLOBAL_FILES = {
    "Makefile", "requirements.txt", "requirements.yml", "pyproject.toml",
    ".ansible-lint.yml", ".yamllint", "ansible.cfg", ".python-version",
}

# Inert by construction: a markdown file cannot change what a scenario asserts.
def inert(f):
    return f.endswith((".md", ".markdown")) or f in {"LICENSE", "CODEOWNERS"}


selected, roles_without = set(), set()
pbs_selected = False
for f in changed:
    if inert(f):
        continue
    if f.startswith(("roles/", "playbooks/")):
        pbs_selected = True
    if f in GLOBAL_FILES or f.startswith(GLOBAL_PREFIXES):
        emit_all(f"global path changed: {f}")
    if f.startswith("roles/"):
        parts = f.split("/")
        if len(parts) < 2 or not parts[1]:
            emit_all(f"unparseable role path: {f}")
        role = parts[1]
        if role in have:
            selected.add(role)
        else:
            # No scenario for this role. Not a reason to run everything — there is nothing to
            # run — but recorded so it is visible rather than silent.
            roles_without.add(role)
        continue
    # playbooks/ selects the playbook-level scenarios above; docs images, anything else: none.

reason = "selected by changed roles" if selected else "no role with a scenario changed"
if roles_without:
    reason += f" (no scenario: {', '.join(sorted(roles_without))})"
pbs = playbook_scenarios if pbs_selected else []
if pbs:
    reason += f"; {len(pbs)} playbook-level scenario(s) (roles/ or playbooks/ changed)"

print(json.dumps({"all": False, "reason": reason,
                  "count": len(selected) + len(pbs), "roles": sorted(selected),
                  "playbook_scenarios": pbs}))
PY
