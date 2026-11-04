# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the Hermes owned-leaf merge (plugins/filter/hermes_owned_projection.py).

Each case pairs a thing that must change with a near-miss that must not, because a merge that
always rewrites and one that never does are indistinguishable from a single case.

Run: python -m pytest tests/unit -q (from the collection root)
"""

from __future__ import annotations

import copy
import os
import sys

import pytest
from ansible.errors import AnsibleFilterError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "plugins", "filter"))

from hermes_owned_projection import (  # noqa: E402
    hermes_config_leaves,
    hermes_owned_keys_document,
    hermes_owned_projection,
)

LIVE = {
    "_config_version": 50,  # the agent's own bookkeeping: unowned, must survive
    "display": {"compact": True, "skin": "mono"},  # a dashboard write
    "model": {"default": "old/model", "provider": "example-gateway", "agent_added": 1},
    "curator": {"interval_days": 7},
    "platform_toolsets": {"api_server": ["web", "terminal"]},
}


def owned(**kw):
    return {
        "key": kw.pop("key"),
        "value": kw.pop("value"),
        "tier": kw.pop("tier", "B"),
        **kw,
    }


def test_unowned_keys_survive_and_owned_leaf_is_restored():
    out = hermes_owned_projection(LIVE, [owned(key="model.default", value="new/model")])
    assert out["changed"] is True
    assert out["projection"]["model"]["default"] == "new/model"
    # siblings under the same parent and unrelated branches are untouched
    assert out["projection"]["model"]["agent_added"] == 1
    assert out["projection"]["display"] == LIVE["display"]
    assert out["projection"]["_config_version"] == 50
    assert [d["key"] for d in out["diff"]] == ["model.default"]


def test_second_projection_is_a_no_op():
    leaves = [owned(key="model.default", value="new/model")]
    first = hermes_owned_projection(LIVE, leaves)
    second = hermes_owned_projection(first["projection"], leaves)
    assert second["changed"] is False
    assert second["diff"] == []
    assert second["projection"] == first["projection"]


def test_no_drift_when_live_already_matches():
    out = hermes_owned_projection(LIVE, [owned(key="model.default", value="old/model")])
    assert out["changed"] is False


def test_input_is_not_mutated():
    before = copy.deepcopy(LIVE)
    hermes_owned_projection(LIVE, [owned(key="model.default", value="x")])
    assert LIVE == before


def test_owned_list_is_replaced_whole():
    out = hermes_owned_projection(LIVE, [owned(key="platform_toolsets.api_server", value=["web"])])
    assert out["projection"]["platform_toolsets"]["api_server"] == ["web"]


def test_owned_leaf_under_missing_parent_creates_it_and_keeps_siblings_elsewhere():
    out = hermes_owned_projection(LIVE, [owned(key="sessions.auto_prune", value=True)])
    assert out["projection"]["sessions"] == {"auto_prune": True}
    assert out["diff"][0]["action"] == "add"
    assert out["projection"]["display"] == LIVE["display"]


def test_scalar_where_a_mapping_is_needed_is_replaced_by_the_owned_leaf():
    out = hermes_owned_projection(
        {"sessions": "garbage"}, [owned(key="sessions.auto_prune", value=True)]
    )
    assert out["projection"]["sessions"] == {"auto_prune": True}


def test_a_range_inside_bounds_is_left_alone():
    leaf = owned(
        key="curator.interval_days",
        value=14,
        tier="A-range",
        allowed={"min": 3, "max": 30},
    )
    out = hermes_owned_projection(LIVE, [leaf])
    assert out["changed"] is False
    assert out["projection"]["curator"]["interval_days"] == 7


def test_a_range_outside_bounds_is_reset_to_the_declared_value():
    leaf = owned(
        key="curator.interval_days",
        value=14,
        tier="A-range",
        allowed={"min": 3, "max": 30},
    )
    out = hermes_owned_projection({"curator": {"interval_days": 90}}, [leaf])
    assert out["projection"]["curator"]["interval_days"] == 14
    assert out["changed"] is True


def test_a_range_list_membership_and_missing_value():
    leaf = owned(
        key="auxiliary.review.model",
        value="model-small",
        tier="A-range",
        allowed=["model-small", "model-mid"],
    )
    assert (
        hermes_owned_projection({"auxiliary": {"review": {"model": "model-mid"}}}, [leaf])[
            "changed"
        ]
        is False
    )
    assert (
        hermes_owned_projection({"auxiliary": {"review": {"model": "gpt-9"}}}, [leaf])["changed"]
        is True
    )
    assert (
        hermes_owned_projection({}, [leaf])["projection"]["auxiliary"]["review"]["model"]
        == "model-small"
    )


def test_a_range_without_allowed_is_refused():
    with pytest.raises(AnsibleFilterError, match="needs 'allowed'"):
        hermes_owned_projection({}, [owned(key="a.b", value=1, tier="A-range")])


def test_owned_mapping_value_is_refused():
    with pytest.raises(AnsibleFilterError, match="scalar or a list"):
        hermes_owned_projection({}, [owned(key="model", value={"default": "x"})])


def test_parent_and_child_both_owned_is_refused():
    with pytest.raises(AnsibleFilterError, match="parents are never owned"):
        hermes_owned_projection(
            {}, [owned(key="model", value="x"), owned(key="model.default", value="y")]
        )


def test_duplicate_key_is_refused():
    with pytest.raises(AnsibleFilterError, match="owned twice"):
        hermes_owned_projection({}, [owned(key="a.b", value=1), owned(key="a.b", value=2)])


def test_bad_tier_and_empty_segment_are_refused():
    with pytest.raises(AnsibleFilterError, match="tier"):
        hermes_owned_projection({}, [owned(key="a.b", value=1, tier="D")])
    with pytest.raises(AnsibleFilterError, match="empty path segment"):
        hermes_owned_projection({}, [owned(key="a..b", value=1)])


def test_segment_list_keeps_a_dotted_provider_name_whole():
    leaf = owned(key=["providers", "my.gw", "api"], value="http://x/v1")
    out = hermes_owned_projection({}, [leaf])
    assert out["projection"] == {"providers": {"my.gw": {"api": "http://x/v1"}}}


def test_non_mapping_live_is_refused_not_treated_as_empty():
    # An unparseable/odd live file must stop the converge, never read as "nothing there".
    with pytest.raises(AnsibleFilterError, match="must be a mapping"):
        hermes_owned_projection(["not", "a", "mapping"], [owned(key="a.b", value=1)])
    assert hermes_owned_projection(None, [owned(key="a.b", value=1)])["projection"] == {
        "a": {"b": 1}
    }


def test_retired_key_is_removed_and_emptied_parent_pruned():
    out = hermes_owned_projection({"old": {"flag": True}, "keep": 1}, [], retired=["old.flag"])
    assert out["projection"] == {"keep": 1}
    assert out["changed"] is True
    # a retired key that is already absent is not drift
    assert hermes_owned_projection({"keep": 1}, [], retired=["old.flag"])["changed"] is False


def test_secret_leaf_is_merged_but_masked_in_the_diff():
    leaf = owned(key="providers.gw.api_key", value="sk-supersecret", secret=True)
    out = hermes_owned_projection({"providers": {"gw": {"api_key": "sk-old"}}}, [leaf])
    assert out["projection"]["providers"]["gw"]["api_key"] == "sk-supersecret"
    flat = repr(out["diff"])
    assert "sk-supersecret" not in flat and "sk-old" not in flat


def test_unowned_keys_lists_names_only_and_excludes_owned():
    out = hermes_owned_projection(LIVE, [owned(key="model.default", value="old/model")])
    assert "model.default" not in out["unowned_keys"]
    assert "model.agent_added" in out["unowned_keys"]
    assert "display.skin" in out["unowned_keys"]
    assert all(isinstance(k, str) for k in out["unowned_keys"])
    # a list value is ONE leaf, not one per element
    assert "platform_toolsets.api_server" in out["unowned_keys"]


def test_config_leaves_flattens_and_flags_secrets():
    doc = {
        "providers": {"my.gw": {"api": "http://x/v1", "api_key": "sk-1", "models": ["a", "b"]}},
        "model": {"default": "m"},
    }
    leaves = {tuple(i["key"]): i for i in hermes_config_leaves(doc, ["api_key"])}
    assert leaves[("providers", "my.gw", "api_key")]["secret"] is True
    assert "secret" not in leaves[("providers", "my.gw", "api")]
    assert leaves[("providers", "my.gw", "models")]["value"] == ["a", "b"]
    # round trip: projecting the flattened leaves onto {} rebuilds the document
    assert hermes_owned_projection({}, list(leaves.values()))["projection"] == doc


def test_owned_keys_document_is_sorted_and_omits_secret_values():
    doc = hermes_owned_keys_document(
        [
            owned(key="providers.gw.api_key", value="sk-supersecret", secret=True),
            owned(key="a.z", value=1),
            owned(
                key="curator.interval_days", value=14, tier="A-range", allowed={"min": 3, "max": 30}
            ),
        ]
    )
    assert [d["key"] for d in doc] == ["a.z", "curator.interval_days", "providers.gw.api_key"]
    assert "sk-supersecret" not in repr(doc)
    assert "value" not in doc[2] and doc[2]["secret"] is True
    assert doc[1]["allowed"] == {"min": 3, "max": 30}


def test_owned_keys_document_records_only_the_user_layer():
    doc = hermes_owned_keys_document(
        [
            {"key": "skills.external_dirs", "value": ["/x"], "tier": "C", "layer": "user"},
            {"key": "approvals.mode", "value": "smart", "tier": "C"},
        ]
    )
    assert doc == [
        {"key": "approvals.mode", "tier": "C", "value": "smart"},
        {"key": "skills.external_dirs", "tier": "C", "value": ["/x"], "layer": "user"},
    ]


# ---- seeds: written when missing, never compared again (the dashboard theme) -----------------
_SEED = [{"key": "dashboard.theme", "value": "classic"}]


def test_seed_is_added_when_the_key_is_missing_and_keeps_siblings():
    r = hermes_owned_projection({"dashboard": {"font": "inter"}}, [], [], _SEED)
    assert r["projection"]["dashboard"] == {"font": "inter", "theme": "classic"}
    assert r["diff"] == [
        {"key": "dashboard.theme", "tier": "seed", "action": "add", "live": None, "want": "classic"}
    ]
    assert "dashboard.theme" in r["unowned_keys"]


def test_near_miss_a_seed_never_overwrites_a_value_already_there():
    # A user picked a theme in the UI: the seed is not a pin and reports no drift.
    r = hermes_owned_projection({"dashboard": {"theme": "default-large"}}, [], [], _SEED)
    assert r["projection"]["dashboard"]["theme"] == "default-large"
    assert r["diff"] == [] and r["changed"] is False


def test_seeded_projection_is_idempotent():
    first = hermes_owned_projection({}, [], [], _SEED)["projection"]
    assert hermes_owned_projection(first, [], [], _SEED)["changed"] is False


def test_seed_that_is_also_owned_or_retired_is_refused():
    owned = [{"key": "dashboard.theme", "value": "x", "tier": "B"}]
    with pytest.raises(AnsibleFilterError, match="also owned or retired"):
        hermes_owned_projection({}, owned, [], _SEED)
    with pytest.raises(AnsibleFilterError, match="also owned or retired"):
        hermes_owned_projection({}, [], ["dashboard.theme"], _SEED)


def test_seed_mapping_value_and_bad_shape_are_refused():
    with pytest.raises(AnsibleFilterError, match="scalar or a list"):
        hermes_owned_projection({}, [], [], [{"key": "dashboard", "value": {"theme": "x"}}])
    with pytest.raises(AnsibleFilterError, match="needs 'key' and 'value'"):
        hermes_owned_projection({}, [], [], [{"key": "dashboard.theme"}])
