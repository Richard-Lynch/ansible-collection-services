# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright: (c) 2026, Richard Lynch
# GNU General Public License v3.0+ (see https://www.gnu.org/licenses/gpl-3.0.txt)
"""Policy derivations for a Hermes deployment: the toolset floor and the managed-scope document.

Both are pure functions so the converge, the preflight's fixtures and the unit tests agree on one
definition (the same reason hermes_owned_projection is a filter).

THE TOOLSET FLOOR. Read from the daemon's own /v1/toolsets, the API-server platform's default
toolset is the FULL core set minus a few (terminal, file, code_execution and browser among the
enabled ones; computer_use, clarify and tts are disabled for api_server even though
`hermes tools list` shows them enabled for the CLI), not the small list the docs show. So the floor
cannot be "turn on what we want"; it has to SUBTRACT, explicitly, from a declared copy of the core
set. Subtraction by omission is a filter that protects something by omission: a typo'd name
subtracts nothing and the dangerous toolset stays enabled while the expression reads correctly. So
this filter refuses a subtracted name that is not in the core set, and asserts on its own result
that nothing it was told to remove is still there.

THE MANAGED DOCUMENT. Hermes has no built-in list of pinned keys; the overlay is a generic
deep-merge of whatever config.yaml the admin places in the managed directory. The pinned set is
therefore the caller's: every owned leaf of tier C. The file is world-readable (0644, root-owned),
so a tier-C leaf flagged secret is refused rather than written.

TWO LAYERS FOR TIER C. Upstream `save_config` (v0.22.x) DROPS every managed leaf from the user
config.yaml on every save (`_strip_managed_keys_for_save`), because the overlay re-applies it at
load. Its callers include the dashboard's config and provider pages,
`hermes plugins install --enable`, `hermes tools enable` and `hermes mcp`; `hermes config set` is NOT
one (it round-trips the user file). That holds only for consumers that load THROUGH the overlay.
Some read config.yaml RAW and never see it: `agent/skill_utils` (every `skills.*` key),
`pm/plugins_state` (`plugins.*`, `memory.*`), `tools/website_policy` (`security.website_blocklist`).
A managed pin on such a key is in force until the first save_config writer, then silently gone
(for example, shared skills disappear after a `hermes plugins install --enable`). So a tier-C leaf
is either:

* `layer: managed` (the default): pinned in the overlay, and NOT projected into the user file, which
  upstream strips on every save anyway (projecting it there made every tier-A write look like drift
  and restart the gateway on the next converge);
* `layer: user`: kept in the user config.yaml by the merge and NOT pinned (a pin would get it
  stripped). Its write protection is the settings service's tier-C refusal and the policy hook, and
  a probe reads it in the view its consumer uses.

A managed-layer tier-C leaf under a raw-read prefix is REFUSED here: it would read as pinned and be
lost on the next save.
"""

from __future__ import annotations

from typing import Any

from ansible.errors import AnsibleFilterError


def _names(value: Any, what: str) -> list[str]:
    if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) and v for v in value):
        raise AnsibleFilterError(f"{what} must be a list of non-empty strings, got {value!r}")
    return list(value)


def hermes_toolset_floor(core: Any, subtract: Any) -> list[str]:
    core_l = _names(core, "hermes_toolset_floor: core")
    sub_l = _names(subtract, "hermes_toolset_floor: subtract")
    if len(set(core_l)) != len(core_l):
        raise AnsibleFilterError("hermes_toolset_floor: core lists a toolset twice")
    unknown = [s for s in sub_l if s not in core_l]
    if unknown:
        raise AnsibleFilterError(
            f"hermes_toolset_floor: {unknown} are not in the core set, so subtracting them "
            "removes nothing and the toolset they name would stay enabled. Fix the name, or "
            "refresh hermes_toolsets_core from `hermes tools list` if the image changed it."
        )
    result = [t for t in core_l if t not in set(sub_l)]
    still = [s for s in sub_l if s in result]
    if still:  # unreachable unless the filter itself is broken; the assertion IS the guard
        raise AnsibleFilterError(f"hermes_toolset_floor: {still} survived the subtraction")
    if not result:
        raise AnsibleFilterError("hermes_toolset_floor: the floor leaves no toolset at all")
    return result


def hermes_toolset_leaves(core: Any, subtract_by_platform: Any) -> list[dict]:
    """One tier-C owned leaf per platform: platform_toolsets.<platform> = core minus subtract."""
    if not isinstance(subtract_by_platform, dict):
        raise AnsibleFilterError("hermes_toolset_leaves: subtract must map platform -> names")
    out = []
    for platform in sorted(subtract_by_platform):
        out.append(
            {
                "key": ["platform_toolsets", platform],
                "value": hermes_toolset_floor(core, subtract_by_platform[platform]),
                "tier": "C",
            }
        )
    return out


_LAYERS = ("managed", "user")


def _layer(leaf: dict) -> str:
    layer = leaf.get("layer", "managed")
    if layer not in _LAYERS:
        raise AnsibleFilterError(f"{leaf.get('key')!r}: layer {layer!r} not in {list(_LAYERS)}")
    return layer


def _prefixes(raw_read_prefixes: Any) -> list[list[str]]:
    if raw_read_prefixes is None:
        return []
    return [p.split(".") for p in _names(raw_read_prefixes, "raw_read_prefixes")]


def hermes_managed_document(owned: Any, raw_read_prefixes: Any = None) -> dict:
    """The managed-scope config.yaml: every managed-layer tier-C leaf, nested, and nothing else."""
    if not isinstance(owned, list):
        raise AnsibleFilterError("hermes_managed_document: owned must be a list")
    raw = _prefixes(raw_read_prefixes)
    doc: dict = {}
    for leaf in owned:
        if not isinstance(leaf, dict) or leaf.get("tier") != "C":
            continue
        key = leaf["key"]
        segs = key.split(".") if isinstance(key, str) else list(key)
        if _layer(leaf) == "user":
            continue
        hit = next((p for p in raw if segs[: len(p)] == p), None)
        if hit is not None:
            raise AnsibleFilterError(
                f"hermes_managed_document: {'.'.join(segs)} is read RAW from config.yaml by upstream "
                f"(prefix {'.'.join(hit)}), so a managed pin is in force only until the first "
                "save_config strips it from the user file. Declare it `layer: user`."
            )
        if leaf.get("secret"):
            raise AnsibleFilterError(
                f"hermes_managed_document: {'.'.join(segs)} is a secret tier-C leaf; the managed "
                "file is world-readable, so a secret is carried in .env, never here"
            )
        cur = doc
        for s in segs[:-1]:
            nxt = cur.setdefault(s, {})
            if not isinstance(nxt, dict):
                raise AnsibleFilterError(
                    f"hermes_managed_document: {'.'.join(segs)} nests under a leaf"
                )
            cur = nxt
        if segs[-1] in cur:
            raise AnsibleFilterError(f"hermes_managed_document: {'.'.join(segs)} listed twice")
        cur[segs[-1]] = leaf["value"]
    return doc


def hermes_user_layer_leaves(owned: Any, managed_enforced: Any = True) -> list:
    """The owned leaves the merge projects into the USER config.yaml.

    With the managed scope enforced, a managed-layer tier-C leaf is the overlay's, not the file's:
    upstream strips it from the file on every save, so projecting it there only manufactures drift.
    Everything else (tiers A-range and B, and tier C declared `layer: user`) is projected. With the
    scope NOT enforced there is no overlay, so every leaf belongs in the file."""
    if not isinstance(owned, list):
        raise AnsibleFilterError("hermes_user_layer_leaves: owned must be a list")
    if not bool(managed_enforced):
        return list(owned)
    return [
        leaf
        for leaf in owned
        if not (isinstance(leaf, dict) and leaf.get("tier") == "C" and _layer(leaf) == "managed")
    ]


def hermes_effective_rows(doc: Any, rc: Any = 0) -> list:
    """Probe rows from an effective-config probe document: one `tier-c:<key>` row per leaf and
    one `skill:<name>` row per shared skill, each matches / drift. An unreadable probe, or one that
    returned no leaves or no skills, is ONE cannot-tell row for that half -- never zero rows, which
    a gate would read as a pass."""
    ok = str(rc).strip() == "0" and isinstance(doc, dict)
    leaves = (doc.get("leaves") or []) if ok else []
    skills = (doc.get("skills") or []) if ok else []
    rows: list[dict] = []
    if leaves:
        for leaf in leaves:
            where = (
                "managed"
                if leaf.get("in_managed")
                else ("file" if leaf.get("in_file") else "neither")
            )
            rows.append(
                {
                    "check": f"tier-c:{leaf['key']}",
                    "verdict": "matches" if leaf.get("effective") is True else "drift",
                    "detail": f"in force via {'/'.join(leaf.get('consumer_views') or [])}; "
                    f"pinned in {where}"
                    + (
                        ""
                        if leaf.get("effective") is True
                        else f"; consumer saw {leaf.get('seen')}"
                    ),
                }
            )
    else:
        rows.append(
            {
                "check": "tier-c",
                "verdict": "cannot-tell",
                "detail": f"effective-config probe rc={rc}, {len(leaves)} leaves",
            }
        )
    if skills:
        for sk in skills:
            good = sk.get("loadable") is True and sk.get("from_estate_root") is True
            rows.append(
                {
                    "check": f"skill:{sk['name']}",
                    "verdict": "matches" if good else "drift",
                    "detail": (
                        "loadable from the shared skills mount"
                        if good
                        else f"loadable={sk.get('loadable')} from_estate_root={sk.get('from_estate_root')} "
                        f"elsewhere={sk.get('elsewhere')}"
                    ),
                }
            )
    else:
        rows.append(
            {
                "check": "skill",
                "verdict": "cannot-tell",
                "detail": f"effective-config probe rc={rc}, {len(skills)} shared skills",
            }
        )
    return rows


class FilterModule:
    def filters(self):
        return {
            "hermes_toolset_floor": hermes_toolset_floor,
            "hermes_toolset_leaves": hermes_toolset_leaves,
            "hermes_managed_document": hermes_managed_document,
            "hermes_user_layer_leaves": hermes_user_layer_leaves,
            "hermes_effective_rows": hermes_effective_rows,
        }
