# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for plugins/filter/hermes_policy.py: the toolset floor and the managed document.

Each must-fail has a near-miss that must pass.
Run: python -m pytest tests/unit -q (from the collection root)
"""

from __future__ import annotations

import os
import sys

import pytest
from ansible.errors import AnsibleFilterError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "plugins", "filter"))

from hermes_policy import (  # noqa: E402
    hermes_effective_rows,
    hermes_managed_document,
    hermes_toolset_floor,
    hermes_toolset_leaves,
    hermes_user_layer_leaves,
)

# The core set, as read from the daemon: what the api_server platform enables by default.
CORE = [
    "web", "browser", "terminal", "file", "code_execution", "vision", "image_gen", "tts",
    "skills", "todo", "memory", "session_search", "connections", "clarify", "delegation",
    "cronjob", "computer_use",
]  # fmt: skip
DANGEROUS = ["terminal", "file", "code_execution", "browser", "computer_use"]


def test_floor_removes_exactly_what_was_named_and_keeps_order():
    out = hermes_toolset_floor(CORE, DANGEROUS)
    assert not set(DANGEROUS) & set(out)
    assert out == [t for t in CORE if t not in DANGEROUS]


def test_a_misspelt_subtraction_is_refused_not_ignored():
    # 'terminall' matches nothing: unfiltered, the whole core set (terminal included) would stay.
    with pytest.raises(AnsibleFilterError, match="removes nothing"):
        hermes_toolset_floor(CORE, ["terminall"])
    # near-miss: the correct spelling is accepted
    assert "terminal" not in hermes_toolset_floor(CORE, ["terminal"])


def test_subtracting_nothing_is_allowed_but_subtracting_everything_is_not():
    assert hermes_toolset_floor(CORE, []) == CORE
    with pytest.raises(AnsibleFilterError, match="no toolset"):
        hermes_toolset_floor(["web"], ["web"])


def test_duplicate_core_and_bad_types_are_refused():
    # A floor that is non-empty after the subtraction, so ONLY the duplicate check can refuse:
    # ["web","web"] minus ["web"] would raise "no toolset" instead, and a bare raises would accept it.
    with pytest.raises(AnsibleFilterError, match="twice"):
        hermes_toolset_floor(["web", "web", "file"], ["file"])
    with pytest.raises(AnsibleFilterError):
        hermes_toolset_floor("web", [])
    with pytest.raises(AnsibleFilterError):
        hermes_toolset_floor(CORE, "terminal")  # a string would iterate to letters


def test_leaves_are_tier_c_per_platform_sorted():
    leaves = hermes_toolset_leaves(CORE, {"slack": ["terminal"], "api_server": DANGEROUS})
    assert [l["key"] for l in leaves] == [
        ["platform_toolsets", "api_server"],
        ["platform_toolsets", "slack"],
    ]
    assert all(l["tier"] == "C" for l in leaves)
    # slack lost only terminal; api_server lost all five: the two floors are independent
    assert "file" in leaves[1]["value"] and "file" not in leaves[0]["value"]
    assert "terminal" not in leaves[1]["value"]


def test_empty_floor_removes_nothing_and_nonempty_still_subtracts():
    """An empty floor (full tool control) is allowed; the mechanism must stay proven."""
    empty = hermes_toolset_leaves(CORE, {"api_server": []})
    assert empty[0]["key"] == ["platform_toolsets", "api_server"]
    assert empty[0]["value"] == CORE  # nothing removed, and the leaf is still pinned
    nonempty = hermes_toolset_leaves(CORE, {"api_server": ["terminal"]})
    assert nonempty[0]["value"] == [t for t in CORE if t != "terminal"]
    assert len(nonempty[0]["value"]) == len(empty[0]["value"]) - 1


def test_managed_document_carries_tier_c_only_and_nests():
    doc = hermes_managed_document(
        [
            {"key": "approvals.mode", "value": "manual", "tier": "C"},
            {"key": ["platform_toolsets", "api_server"], "value": ["web"], "tier": "C"},
            {"key": "sessions.auto_prune", "value": True, "tier": "B"},
            {"key": "curator.interval_days", "value": 14, "tier": "A-range", "allowed": [14]},
        ]
    )
    assert doc == {"approvals": {"mode": "manual"}, "platform_toolsets": {"api_server": ["web"]}}


def test_managed_document_refuses_a_secret_and_a_duplicate():
    with pytest.raises(AnsibleFilterError, match="world-readable"):
        hermes_managed_document(
            [{"key": "x.api_key", "value": "sk-1", "tier": "C", "secret": True}]
        )
    # near-miss: the same secret at tier B is simply not this filter's business
    assert (
        hermes_managed_document(
            [{"key": "x.api_key", "value": "sk-1", "tier": "B", "secret": True}]
        )
        == {}
    )
    with pytest.raises(AnsibleFilterError, match="twice"):
        hermes_managed_document(
            [{"key": "a.b", "value": 1, "tier": "C"}, {"key": ["a", "b"], "value": 2, "tier": "C"}]
        )


def test_managed_document_refuses_a_leaf_nested_under_a_leaf():
    # a.b is pinned as a VALUE, then a.b.c would have to nest under it: refused, not overwritten
    with pytest.raises(AnsibleFilterError, match="nests under a leaf"):
        hermes_managed_document(
            [{"key": "a.b", "value": 1, "tier": "C"}, {"key": "a.b.c", "value": 2, "tier": "C"}]
        )
    # near-miss: siblings under one parent nest fine
    assert hermes_managed_document(
        [{"key": "a.b", "value": 1, "tier": "C"}, {"key": "a.c", "value": 2, "tier": "C"}]
    ) == {"a": {"b": 1, "c": 2}}


# ---- the two layers of tier C ----------------------------------------------------------------------
RAW = ["skills", "plugins", "memory", "security.website_blocklist"]


def test_a_managed_pin_on_a_raw_read_key_is_refused():
    # MUST FAIL: upstream reads skills.* raw, and save_config strips managed leaves from the file,
    # so this pin would read as in place and be gone after the first `hermes config set`.
    with pytest.raises(AnsibleFilterError, match="layer: user"):
        hermes_managed_document(
            [{"key": "skills.external_dirs", "value": ["/opt/shared-skills"], "tier": "C"}], RAW
        )
    with pytest.raises(AnsibleFilterError, match="security.website_blocklist"):
        hermes_managed_document(
            [{"key": ["security", "website_blocklist", "enabled"], "value": True, "tier": "C"}], RAW
        )


def test_a_user_layer_leaf_is_not_pinned_and_a_near_miss_prefix_is():
    owned = [
        {"key": "skills.external_dirs", "value": ["/x"], "tier": "C", "layer": "user"},
        # Near misses: the prefix matches whole segments, so neither of these is raw-read.
        {"key": "skillsets.thing", "value": 1, "tier": "C"},
        {"key": "security.redact_secrets", "value": True, "tier": "C"},
        {"key": "approvals.mode", "value": "smart", "tier": "C"},
    ]
    doc = hermes_managed_document(owned, RAW)
    assert "skills" not in doc
    assert doc == {
        "skillsets": {"thing": 1},
        "security": {"redact_secrets": True},
        "approvals": {"mode": "smart"},
    }
    # Without the prefix list the old behaviour holds (every non-user tier-C leaf is pinned).
    assert hermes_managed_document(owned)["skillsets"] == {"thing": 1}


def test_an_unknown_layer_is_refused():
    with pytest.raises(AnsibleFilterError, match="layer"):
        hermes_managed_document([{"key": "a.b", "value": 1, "tier": "C", "layer": "both"}], RAW)


def test_user_layer_leaves_drop_only_managed_tier_c_when_enforced():
    owned = [
        {"key": "model.default", "value": "m", "tier": "B"},
        {"key": "approvals.mode", "value": "smart", "tier": "C"},
        {"key": "skills.external_dirs", "value": ["/x"], "tier": "C", "layer": "user"},
        {"key": "display.x", "value": 1, "tier": "A-range", "allowed": [1, 2]},
    ]
    keys = [leaf["key"] for leaf in hermes_user_layer_leaves(owned, True)]
    assert keys == ["model.default", "skills.external_dirs", "display.x"]
    # Not enforced: there is no overlay, so every leaf belongs in the file.
    assert hermes_user_layer_leaves(owned, False) == owned


# ---- probe rows from the effective-config probe --------------------------------------------------
def _doc(leaf_ok=True, skill_ok=True):
    return {
        "leaves": [
            {"key": "approvals.mode", "effective": True, "in_managed": True, "in_file": False,
             "consumer_views": ["config", "gateway"], "seen": {}},
            {"key": "skills.external_dirs", "effective": leaf_ok, "in_managed": False,
             "in_file": leaf_ok, "consumer_views": ["file"], "seen": {"file": "<missing>"}},
        ],
        "skills": [
            {"name": "coder", "loadable": skill_ok, "from_estate_root": skill_ok, "elsewhere": []},
        ],
    }  # fmt: skip


def test_effective_rows_name_every_leaf_and_skill():
    rows = hermes_effective_rows(_doc(), 0)
    assert [(r["check"], r["verdict"]) for r in rows] == [
        ("tier-c:approvals.mode", "matches"),
        ("tier-c:skills.external_dirs", "matches"),
        ("skill:coder", "matches"),
    ]


def test_effective_rows_drift_when_a_leaf_or_skill_is_out_of_force():
    # MUST FAIL (drift): skills.external_dirs stripped, shared skills gone.
    rows = {r["check"]: r["verdict"] for r in hermes_effective_rows(_doc(False, False), 0)}
    assert rows["tier-c:skills.external_dirs"] == "drift"
    assert rows["skill:coder"] == "drift"
    assert rows["tier-c:approvals.mode"] == "matches"


def test_effective_rows_never_pass_on_an_empty_or_failed_probe():
    # An empty loop must not read as "all in force": one cannot-tell row per half instead.
    for doc, rc in [({}, 1), ({}, 0), ({"leaves": [], "skills": []}, 0), (_doc(), 1)]:
        rows = hermes_effective_rows(doc, rc)
        assert [r["verdict"] for r in rows] == ["cannot-tell", "cannot-tell"], (doc, rc)
