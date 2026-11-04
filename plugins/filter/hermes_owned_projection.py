# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright: (c) 2026, Richard Lynch
# GNU General Public License v3.0+ (see https://www.gnu.org/licenses/gpl-3.0.txt)
"""Owned-leaf projection for Hermes' config.yaml.

The caller owns a declared set of LEAVES of the agent's config.yaml; the agent and the dashboard
write the rest. A converge must therefore MERGE: re-assert the owned leaves and carry every other
key through untouched. This module is that merge, as pure functions, so the converge, the probe,
a settings service's test fixture and an end-to-end test all decide "drift" the same way.

Semantics:

* A key is a dotted path ("model.default") or, when a segment itself contains a dot, a list of
  segments (["providers", "my.gw", "api"]).
* Only LEAVES are owned, never parents. An owned value is a scalar or a LIST; a list is replaced
  whole. A dict value is refused: owning a parent would silently delete its unowned children.
* An owned leaf under an unowned parent keeps its siblings; an unowned leaf under a path that
  contains owned leaves is kept.
* tier "B" and "C": the live value is set to the declared one when it differs.
* tier "A-range": the declared value is a DEFAULT. The live value is left alone while it is
  inside `allowed` (a list of permitted values, or {"min": n, "max": n}), and is reset only when
  it is missing or outside the bound.
* `retired` keys are the one way a leaf is removed; a parent emptied by that is pruned.
* `seeds` are NOT owned: each is written only when its key is MISSING, and never compared again,
  so a value the dashboard or the agent later sets wins for good (the dashboard theme). A seed is
  absent from owned-keys.json by construction, so nothing refuses a write to it. Its key must not
  also be owned or retired.
* Leaves flagged `secret: true` are merged like any other but are masked in `diff`.

The projection never mutates its inputs.
"""

from __future__ import annotations

import copy
from typing import Any

from ansible.errors import AnsibleFilterError

_MISSING = object()
_TIERS = ("A-range", "B", "C")
_MASK = "<secret>"


def _segments(key: Any) -> list[str]:
    if isinstance(key, str):
        segs = key.split(".")
    elif isinstance(key, (list, tuple)):
        segs = list(key)
    else:
        raise AnsibleFilterError(f"hermes_owned_projection: key must be a string or list: {key!r}")
    if not segs or any(not isinstance(s, str) or s == "" for s in segs):
        raise AnsibleFilterError(f"hermes_owned_projection: empty path segment in key {key!r}")
    return segs


def _get(doc: Any, segs: list[str]) -> Any:
    cur = doc
    for s in segs:
        if not isinstance(cur, dict) or s not in cur:
            return _MISSING
        cur = cur[s]
    return cur


def _set(doc: dict, segs: list[str], value: Any) -> None:
    cur = doc
    for s in segs[:-1]:
        nxt = cur.get(s)
        if not isinstance(nxt, dict):
            # A scalar (or nothing) where a mapping is needed: the owned leaf wins.
            nxt = {}
            cur[s] = nxt
        cur = nxt
    cur[segs[-1]] = copy.deepcopy(value)


def _delete(doc: dict, segs: list[str]) -> bool:
    chain = [doc]
    for s in segs[:-1]:
        nxt = chain[-1].get(s)
        if not isinstance(nxt, dict):
            return False
        chain.append(nxt)
    if segs[-1] not in chain[-1]:
        return False
    del chain[-1][segs[-1]]
    # Prune parents this removal emptied (never the document root).
    for depth in range(len(segs) - 1, 0, -1):
        if chain[depth]:
            break
        del chain[depth - 1][segs[depth - 1]]
    return True


def _leaves(doc: Any, prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    """Leaf paths of a document. A list or scalar is a leaf; an empty mapping is a leaf too."""
    if isinstance(doc, dict) and doc:
        out: list[tuple[str, ...]] = []
        for k, v in doc.items():
            out.extend(_leaves(v, prefix + (str(k),)))
        return out
    return [prefix] if prefix else []


def _within(live: Any, allowed: Any) -> bool:
    if isinstance(allowed, dict):
        try:
            lo, hi = allowed.get("min"), allowed.get("max")
            return (lo is None or live >= lo) and (hi is None or live <= hi)
        except TypeError:
            return False
    return live in allowed


def _validate(owned: Any) -> list[dict]:
    if not isinstance(owned, list):
        raise AnsibleFilterError("hermes_owned_projection: owned must be a list of leaves")
    seen: dict[tuple[str, ...], int] = {}
    out = []
    for i, item in enumerate(owned):
        if not isinstance(item, dict) or "key" not in item or "value" not in item:
            raise AnsibleFilterError(
                f"hermes_owned_projection: owned[{i}] needs 'key' and 'value': {sorted(item) if isinstance(item, dict) else item!r}"
            )
        segs = _segments(item["key"])
        tier = item.get("tier", "B")
        if tier not in _TIERS:
            raise AnsibleFilterError(
                f"hermes_owned_projection: {'.'.join(segs)}: tier {tier!r} not in {list(_TIERS)}"
            )
        if isinstance(item["value"], dict):
            raise AnsibleFilterError(
                f"hermes_owned_projection: {'.'.join(segs)}: an owned value must be a scalar or a "
                "list. Owning a mapping would delete its unowned children; own its leaves."
            )
        if tier == "A-range" and not item.get("allowed"):
            raise AnsibleFilterError(
                f"hermes_owned_projection: {'.'.join(segs)}: tier A-range needs 'allowed'"
            )
        tup = tuple(segs)
        if tup in seen:
            raise AnsibleFilterError(
                f"hermes_owned_projection: {'.'.join(segs)} is owned twice (owned[{seen[tup]}] and owned[{i}])"
            )
        seen[tup] = i
        out.append({**item, "_segs": segs, "tier": tier})
    keys = list(seen)
    for a in keys:
        for b in keys:
            if a != b and b[: len(a)] == a:
                raise AnsibleFilterError(
                    f"hermes_owned_projection: {'.'.join(a)} is owned as a leaf and is also the "
                    f"parent of {'.'.join(b)}; parents are never owned"
                )
    return out


def _validate_seeds(seeds: Any, leaves: list[dict], retired: list) -> list[tuple[list[str], Any]]:
    if not isinstance(seeds, list):
        raise AnsibleFilterError("hermes_owned_projection: seeds must be a list of {key, value}")
    owned = {tuple(leaf["_segs"]) for leaf in leaves}
    gone = {tuple(_segments(k)) for k in retired}
    out = []
    for i, item in enumerate(seeds):
        if not isinstance(item, dict) or "key" not in item or "value" not in item:
            raise AnsibleFilterError(f"hermes_owned_projection: seeds[{i}] needs 'key' and 'value'")
        segs = _segments(item["key"])
        if isinstance(item["value"], dict):
            raise AnsibleFilterError(
                f"hermes_owned_projection: seed {'.'.join(segs)}: a seed value must be a scalar or a list"
            )
        if tuple(segs) in owned or tuple(segs) in gone:
            raise AnsibleFilterError(
                f"hermes_owned_projection: seed {'.'.join(segs)} is also owned or retired; a key is one of the three"
            )
        out.append((segs, item["value"]))
    return out


def hermes_owned_projection(live: Any, owned: Any, retired: Any = None, seeds: Any = None) -> dict:
    if live is None:
        live = {}
    if not isinstance(live, dict):
        raise AnsibleFilterError(
            f"hermes_owned_projection: live config must be a mapping, got {type(live).__name__}"
        )
    leaves = _validate(owned)
    retired = list(retired or [])
    seed_items = _validate_seeds(list(seeds or []), leaves, retired)
    doc = copy.deepcopy(live)
    diff: list[dict] = []

    for leaf in leaves:
        segs = leaf["_segs"]
        want = leaf["value"]
        have = _get(doc, segs)
        if leaf["tier"] == "A-range" and have is not _MISSING and _within(have, leaf["allowed"]):
            continue
        if have is not _MISSING and have == want:
            continue
        secret = bool(leaf.get("secret"))
        diff.append(
            {
                "key": ".".join(segs),
                "tier": leaf["tier"],
                "action": "add" if have is _MISSING else "set",
                "live": (
                    _MASK
                    if secret and have is not _MISSING
                    else (None if have is _MISSING else have)
                ),
                "want": _MASK if secret else want,
            }
        )
        _set(doc, segs, want)

    for segs, want in seed_items:
        if _get(doc, segs) is _MISSING:
            diff.append(
                {"key": ".".join(segs), "tier": "seed", "action": "add", "live": None, "want": want}
            )
            _set(doc, segs, want)

    for key in retired:
        segs = _segments(key)
        if _get(doc, segs) is not _MISSING:
            diff.append(
                {
                    "key": ".".join(segs),
                    "tier": "retired",
                    "action": "remove",
                    "live": _MASK,
                    "want": None,
                }
            )
            _delete(doc, segs)

    owned_set = {tuple(leaf["_segs"]) for leaf in leaves}
    unowned = sorted(".".join(p) for p in _leaves(doc) if p not in owned_set)
    return {
        "projection": doc,
        "diff": diff,
        "unowned_keys": unowned,
        "changed": bool(diff),
    }


def hermes_config_leaves(doc: Any, secret_names: Any = None, tier: str = "B") -> list[dict]:
    """Flatten a rendered config fragment into owned leaves (tier B), flagging secret ones.

    A leaf whose LAST segment is in `secret_names` is marked `secret: true`. Keys come back as
    segment lists so a provider name containing a dot survives.
    """
    if not isinstance(doc, dict):
        raise AnsibleFilterError("hermes_config_leaves: expects a mapping")
    secrets = set(secret_names or [])
    out = []
    for path in _leaves(doc):
        value = _get(doc, list(path))
        if isinstance(value, dict):  # an empty mapping is a leaf; own it as such is refused
            continue
        item = {"key": list(path), "value": value, "tier": tier}
        if path[-1] in secrets:
            item["secret"] = True
        out.append(item)
    return out


def hermes_owned_keys_document(owned: Any) -> list[dict]:
    """The public face of the owned set, for owned-keys.json: sorted, and secret values omitted.

    Everything that reads the file (a probe, a settings service, a console) learns key,
    tier and bounds from here, so a leaf cannot be 'managed' in one place and free in another.
    """
    out = []
    for leaf in _validate(owned):
        entry = {"key": ".".join(leaf["_segs"]), "tier": leaf["tier"]}
        if leaf.get("secret"):
            entry["secret"] = True
        else:
            entry["value"] = leaf["value"]
        if leaf.get("allowed"):
            entry["allowed"] = leaf["allowed"]
        # Only the non-default layer is written, so every existing document stays byte-identical.
        # The preflight skips a `user` tier-C leaf (it is not in the managed file by design).
        if leaf.get("layer") == "user":
            entry["layer"] = "user"
        out.append(entry)
    return sorted(out, key=lambda e: e["key"])


class FilterModule:
    def filters(self):
        return {
            "hermes_owned_projection": hermes_owned_projection,
            "hermes_config_leaves": hermes_config_leaves,
            "hermes_owned_keys_document": hermes_owned_keys_document,
        }
