# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright: (c) 2026, Richard Lynch
# GNU General Public License v3.0+ (see https://www.gnu.org/licenses/gpl-3.0.txt)
"""Pure derivations for a Hermes coding profile, the Kanban toolset placement and a GitHub token check.

The filters, each a function so the converge, the molecule scenario and the unit tests agree on one
definition:

hermes_leaf_merge(existing_text, leaves)
    Set named leaves in a profile's config.yaml text and leave every OTHER key exactly as it is: a
    profile's config is also written by the agent and the dashboard, so a template that rewrites the
    whole file would erase their work on every converge. Returns the input text UNCHANGED when the
    merge changes nothing, so an unchanged converge is byte-identical (``changed=0``) rather than a
    re-serialisation that reorders keys.

hermes_coder_profile_leaves(spec)
    The coder profile's owned leaves, refusing the configurations that would make it a shell on the
    gateway. The trap this guards is SUBTRACTION/ADDITION BY OMISSION: ``platform_toolsets.<p>: [x]``
    REPLACES the platform's list, so a leaf that meant "add kanban" and was written as "kanban only"
    silently removes every other tool; a typo'd toolset name enables nothing and reads correctly. So
    every name must be in the declared ``known_toolsets`` (read from the running image, never typed
    from memory).

hermes_kanban_placement(by_platform, allowed, unreadable=None)
    The three-outcome verdict on where the kanban toolset is enabled: it must be on exactly the
    ``allowed`` platforms (Slack and the CLI) and OFF on every platform that was READ, in
    particular the API server, whose toolset floor removes everything shell-equivalent. A platform
    whose list could not be read is ``cannot_tell``, which is never counted as "off". It says
    nothing about a platform nobody read.

hermes_github_token_verdict(status, scopes_header)
    Three outcomes for the token the PR gate presents, from one authenticated GET /user:
    ``refused`` (dead token, or a classic token carrying a scope that can push, merge or run a
    workflow), ``classic-read`` (a classic token whose every scope is read-shaped) and
    ``fine-grained`` (GitHub sends NO X-OAuth-Scopes for a fine-grained PAT, so its permissions
    cannot be read from headers; the ruleset is the control). The third is recorded as what it
    is, never as a pass of the scope check.
"""

from __future__ import annotations

from typing import Any

import yaml
from ansible.errors import AnsibleFilterError

# Toolsets that would make the coder PROFILE a way into the gateway. Its terminal must be the ssh
# backend (a shell on the WORKER); code_execution runs Python on the gateway itself, browser drives a
# browser there, delegation spawns sub-agents that inherit these tools, cronjob schedules unattended
# runs, and connections manages chat-platform connections.
FORBIDDEN_FOR_CODER = frozenset(
    {
        "code_execution",
        "browser",
        "delegation",
        "cronjob",
        "connections",
        "computer_use",
    }
)
REQUIRED_FOR_CODER = frozenset({"terminal", "kanban"})
ALLOWED_APPROVAL_MODES = ("smart", "manual")


def _plain(value: Any) -> Any:
    """Plain builtins only. Ansible hands a filter its own str subclasses (tagged/unsafe text), and
    ``yaml.safe_dump`` REFUSES a str subclass ("cannot represent an object") even though it prints as
    an ordinary string. A unit test with literals cannot see this; the molecule scenario did."""
    if isinstance(value, dict):
        return {_plain(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    if isinstance(value, str):
        return str(value)
    raise AnsibleFilterError(f"cannot use a {type(value).__name__} in a config leaf")


def _segments(key: Any) -> list[str]:
    segs = key.split(".") if isinstance(key, str) else list(key)
    if not segs or not all(isinstance(s, str) and s for s in segs):
        raise AnsibleFilterError(f"hermes_leaf_merge: bad key {key!r}")
    return segs


def hermes_leaf_merge(existing_text: Any, leaves: Any) -> str:
    if existing_text is None:
        existing_text = ""
    if not isinstance(existing_text, str):
        raise AnsibleFilterError("hermes_leaf_merge: existing_text must be a string")
    if not isinstance(leaves, (list, tuple)):
        raise AnsibleFilterError("hermes_leaf_merge: leaves must be a list")
    existing_text = str(existing_text)
    leaves = _plain(list(leaves))
    try:
        current = yaml.safe_load(existing_text) if existing_text.strip() else {}
    except yaml.YAMLError as e:
        raise AnsibleFilterError(f"hermes_leaf_merge: the existing file is not valid YAML: {e}")
    if current is None:
        current = {}
    if not isinstance(current, dict):
        raise AnsibleFilterError("hermes_leaf_merge: the existing file is not a mapping")
    merged = yaml.safe_load(yaml.safe_dump(current))  # deep copy via the same parser
    seen: set[tuple[str, ...]] = set()
    for leaf in leaves:
        if not isinstance(leaf, dict) or "key" not in leaf or "value" not in leaf:
            raise AnsibleFilterError(f"hermes_leaf_merge: a leaf needs key and value, got {leaf!r}")
        segs = _segments(leaf["key"])
        if tuple(segs) in seen:
            raise AnsibleFilterError(f"hermes_leaf_merge: {'.'.join(segs)} is listed twice")
        seen.add(tuple(segs))
        cur = merged
        for s in segs[:-1]:
            nxt = cur.get(s)
            if nxt is None:
                nxt = cur[s] = {}
            if not isinstance(nxt, dict):
                raise AnsibleFilterError(
                    f"hermes_leaf_merge: {'.'.join(segs)} nests under {s!r}, which is a "
                    f"{type(nxt).__name__}, not a mapping; refusing to replace it"
                )
            cur = nxt
        cur[segs[-1]] = leaf["value"]
    if merged == current:
        return existing_text
    return yaml.safe_dump(merged, sort_keys=False, default_flow_style=False)


def hermes_leaf_get(text: Any, key: Any, default: Any = None) -> Any:
    """Read one leaf back out of a config.yaml text. A SEPARATE code path from hermes_leaf_merge on
    purpose: the read-back after a write must not share the code that wrote, or it would agree with
    a wrong write."""
    if not isinstance(text, str):
        raise AnsibleFilterError("hermes_leaf_get: text must be a string")
    text = str(text)
    try:
        cur = yaml.safe_load(text) if text.strip() else {}
    except yaml.YAMLError as e:
        raise AnsibleFilterError(f"hermes_leaf_get: not valid YAML: {e}")
    for seg in _segments(key):
        if not isinstance(cur, dict) or seg not in cur:
            return default
        cur = cur[seg]
    return cur


def _names(value: Any, what: str) -> list[str]:
    if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) and v for v in value):
        raise AnsibleFilterError(f"{what} must be a list of non-empty strings, got {value!r}")
    return list(value)


def hermes_coder_profile_leaves(spec: Any) -> list[dict]:
    if not isinstance(spec, dict):
        raise AnsibleFilterError("hermes_coder_profile_leaves: spec must be a mapping")
    spec = _plain(spec)
    missing = [
        k
        for k in (
            "toolsets",
            "known_toolsets",
            "approvals_mode",
            "approvals_deny",
            "terminal",
        )
        if k not in spec
    ]
    if missing:
        raise AnsibleFilterError(f"hermes_coder_profile_leaves: spec lacks {missing}")
    toolsets = _names(spec["toolsets"], "toolsets")
    known = _names(spec["known_toolsets"], "known_toolsets")
    if not known:
        raise AnsibleFilterError(
            "hermes_coder_profile_leaves: known_toolsets is empty. Read it off the running image "
            "(`hermes tools list`); it is never typed from memory."
        )
    if len(set(toolsets)) != len(toolsets):
        raise AnsibleFilterError("hermes_coder_profile_leaves: a toolset is listed twice")
    unknown = [t for t in toolsets if t not in known]
    if unknown:
        raise AnsibleFilterError(
            f"hermes_coder_profile_leaves: {unknown} are not in known_toolsets, so enabling them "
            "enables nothing and the profile would read as configured. Fix the name, or refresh "
            "known_toolsets from the image if it changed."
        )
    forbidden = sorted(FORBIDDEN_FOR_CODER.intersection(toolsets))
    if forbidden:
        raise AnsibleFilterError(
            f"hermes_coder_profile_leaves: {forbidden} would make the coder profile a way into the "
            "gateway (the worker is where code runs); refusing."
        )
    lacking = sorted(REQUIRED_FOR_CODER.difference(toolsets))
    if lacking:
        raise AnsibleFilterError(f"hermes_coder_profile_leaves: the coder profile needs {lacking}")
    mode = spec["approvals_mode"]
    if mode not in ALLOWED_APPROVAL_MODES:
        raise AnsibleFilterError(
            f"hermes_coder_profile_leaves: approvals.mode {mode!r} is not one of "
            f"{list(ALLOWED_APPROVAL_MODES)}; 'off' would let the coder run anything unasked"
        )
    deny = _names(spec["approvals_deny"], "approvals_deny")
    joined = "\n".join(deny)
    for needle, why in (
        ("rm -rf", "a recursive delete"),
        ("push --force", "a force push"),
    ):
        if needle not in joined:
            raise AnsibleFilterError(
                f"hermes_coder_profile_leaves: approvals_deny has no entry covering {why} ({needle!r})"
            )
    term = spec["terminal"]
    if not isinstance(term, dict) or term.get("backend") != "ssh":
        raise AnsibleFilterError(
            "hermes_coder_profile_leaves: terminal.backend must be 'ssh' (a shell on the worker). "
            "'local' would be a shell in the gateway container, next to every other profile's secrets. "
            "The ssh target keys are the caller's configuration, named as the running image names "
            "them; an empty terminal block is refused rather than guessed."
        )
    # ONE leaf PER PLATFORM, never just ``cli``. The profile is cloned from ``default``, whose
    # ``platform_toolsets`` carries a list for every chat platform (slack and the others hold
    # code_execution, browser, delegation and cronjob: a shell on the gateway). A leaf for the CLI
    # alone would leave every other route into this profile with the source's wider list.
    # ``platforms`` defaults to the CLI (a Kanban worker is spawned as a non-interactive CLI
    # run); the caller declares every platform it can read back and the role additionally sweeps
    # any platform key the clone carried that is not named here (hermes_profile_platform_toolsets).
    platforms = _names(spec.get("platforms", ["cli"]), "platforms")
    if not platforms:
        raise AnsibleFilterError("hermes_coder_profile_leaves: platforms is empty")
    if len(set(platforms)) != len(platforms):
        raise AnsibleFilterError("hermes_coder_profile_leaves: a platform is listed twice")
    leaves: list[dict] = [{"key": ["platform_toolsets", p], "value": toolsets} for p in platforms]
    leaves += [
        {"key": ["approvals", "mode"], "value": mode},
        {"key": ["approvals", "deny"], "value": deny},
    ]
    for k in sorted(term):
        leaves.append({"key": ["terminal", k], "value": term[k]})
    ext = spec.get("external_dirs")
    if ext:
        leaves.append({"key": ["skills", "external_dirs"], "value": _names(ext, "external_dirs")})
    return leaves


def hermes_kanban_placement(by_platform: Any, allowed: Any, unreadable: Any = None) -> dict:
    if not isinstance(by_platform, dict):
        raise AnsibleFilterError(
            "hermes_kanban_placement: by_platform must map platform -> list or null"
        )
    by_platform = _plain(by_platform)
    allowed_l = _names(_plain(allowed), "allowed")
    if not allowed_l:
        raise AnsibleFilterError("hermes_kanban_placement: allowed is empty")
    unread = _names(_plain(unreadable) if unreadable else [], "unreadable")
    enabled, unexpected, missing, cannot = [], [], [], list(unread)
    for platform in sorted(by_platform):
        tools = by_platform[platform]
        if tools is None or platform in unread:
            cannot.append(platform)
            continue
        on = "kanban" in _names(tools, f"toolsets of {platform}")
        if on:
            enabled.append(platform)
        if on and platform not in allowed_l:
            unexpected.append(platform)
    for platform in allowed_l:
        if platform in unread:
            continue
        if platform not in by_platform:
            cannot.append(platform)  # asked for and never read: not "off", not "on"
        elif by_platform[platform] is not None and platform not in enabled:
            missing.append(platform)
    verdict = "cannot-tell" if cannot else ("ok" if not unexpected and not missing else "wrong")
    return {
        "verdict": verdict,
        "enabled_on": enabled,
        "unexpected_on": unexpected,
        "missing_on": missing,
        "cannot_tell": sorted(set(cannot)),
    }


def hermes_platform_sweep_leaves(
    existing_text: Any, toolsets: Any, explicit_leaves: Any = None
) -> list[dict]:
    """Leaves that set EVERY ``platform_toolsets.<p>`` key already in ``existing_text`` to
    ``toolsets``, except the platforms ``explicit_leaves`` already name (those apply on top). Empty
    ``toolsets`` means no sweep. A clone copies the source profile's list for every chat platform,
    so a leaf for one platform leaves every other route into the profile with the source's wider
    list; this closes the ones the caller did not know about. Names only: the values are replaced.
    """
    if existing_text is None:
        existing_text = ""
    if not isinstance(existing_text, str):
        raise AnsibleFilterError("hermes_platform_sweep_leaves: existing_text must be a string")
    wanted = _names(_plain(toolsets) if toolsets else [], "toolsets")
    if not wanted:
        return []
    present = hermes_leaf_get(str(existing_text), ["platform_toolsets"], {})
    if not isinstance(present, dict):
        return []
    named: set[str] = set()
    for leaf in _plain(list(explicit_leaves or [])):
        if not isinstance(leaf, dict) or "key" not in leaf:
            raise AnsibleFilterError(
                f"hermes_platform_sweep_leaves: a leaf needs a key, got {leaf!r}"
            )
        segs = _segments(leaf["key"])
        if len(segs) == 2 and segs[0] == "platform_toolsets":
            named.add(segs[1])
    return [
        {"key": ["platform_toolsets", str(p)], "value": list(wanted)}
        for p in present
        if str(p) not in named
    ]


# Classic-token scopes that can write to a repository, merge, or run a workflow. `repo` can push
# and merge. `public_repo` is `repo` for public
# repositories; `workflow` edits Actions. Any `write:*`/`admin:*`/`delete*` scope is a write.
WRITE_SHAPED_SCOPES = frozenset({"repo", "public_repo", "workflow", "gist", "delete_repo"})


def hermes_github_token_verdict(status: Any, scopes_header: Any) -> dict:
    try:
        code = int(status)
    except (TypeError, ValueError):
        raise AnsibleFilterError(f"hermes_github_token_verdict: status {status!r} is not an int")
    header = "" if scopes_header is None else str(scopes_header)
    scopes = [s.strip() for s in header.split(",") if s.strip()]
    if code == 401:
        return {
            "outcome": "refused",
            "reason": "GitHub answered 401: the token is dead or malformed",
            "write_scopes": [],
        }
    if code != 200:
        return {
            "outcome": "refused",
            "reason": f"GitHub answered HTTP {code}, not 200",
            "write_scopes": [],
        }
    if not scopes:
        return {
            "outcome": "fine-grained",
            "reason": "no X-OAuth-Scopes header: a fine-grained PAT, whose permissions cannot be "
            "read from headers; the repository ruleset is the only control on merges",
            "write_scopes": [],
        }
    bad = sorted(
        s
        for s in scopes
        if s in WRITE_SHAPED_SCOPES or s.startswith(("write:", "admin:", "delete"))
    )
    if bad:
        return {
            "outcome": "refused",
            "reason": f"classic token carries write-shaped scopes {bad}: it can push, merge or "
            "run a workflow, and it would run unattended from Kanban cards",
            # Named, so a caller that knowingly accepts a write-capable token can tell this
            # refusal from a dead one: a 401 carries an EMPTY list and can never be excepted.
            "write_scopes": bad,
        }
    return {
        "outcome": "classic-read",
        "reason": f"classic token, read-shaped scopes {scopes}",
        "write_scopes": [],
    }


class FilterModule:
    def filters(self):
        return {
            "hermes_leaf_merge": hermes_leaf_merge,
            "hermes_leaf_get": hermes_leaf_get,
            "hermes_coder_profile_leaves": hermes_coder_profile_leaves,
            "hermes_kanban_placement": hermes_kanban_placement,
            "hermes_platform_sweep_leaves": hermes_platform_sweep_leaves,
            "hermes_github_token_verdict": hermes_github_token_verdict,
        }
