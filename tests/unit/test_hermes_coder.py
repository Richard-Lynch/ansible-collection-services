# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the hermes_coder filters (plugins/filter/hermes_coder.py).

PURE LOGIC ONLY. Every refusal is paired with a near-miss that must PASS, because a filter that
refuses everything also passes a "must refuse" test.

Run: python -m pytest tests/unit -q (from the collection root)
"""

from __future__ import annotations

import copy
import os
import sys

import pytest
import yaml
from ansible.errors import AnsibleFilterError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "plugins", "filter"))

from hermes_coder import (  # noqa: E402
    hermes_coder_profile_leaves,
    hermes_github_token_verdict,
    hermes_kanban_placement,
    hermes_leaf_get,
    hermes_leaf_merge,
    hermes_platform_sweep_leaves,
)

KNOWN = [
    "browser", "code_execution", "connections", "cronjob", "delegation", "file", "image_gen",
    "memory", "session_search", "skills", "terminal", "todo", "vision", "web", "kanban",
]  # fmt: skip

GOOD_SPEC = {
    "toolsets": ["terminal", "file", "kanban", "todo"],
    "known_toolsets": KNOWN,
    "approvals_mode": "smart",
    "approvals_deny": ["rm -rf /*", "git push --force*"],
    "terminal": {"backend": "ssh", "host": "worker.example"},
    "external_dirs": ["/opt/shared-skills"],
}


# ----------------------------------------------------------------------------------- leaf merge


def test_merge_sets_leaves_and_keeps_every_other_key():
    existing = "model:\n  default: m\nmemory:\n  write_approval: true\nterminal:\n  cwd: /srv\n"
    out = hermes_leaf_merge(
        existing,
        [
            {"key": "terminal.backend", "value": "ssh"},
            {"key": ["approvals", "mode"], "value": "smart"},
        ],
    )
    d = yaml.safe_load(out)
    assert d["model"] == {"default": "m"}  # untouched
    assert d["memory"] == {"write_approval": True}  # an agent-written key survives
    assert d["terminal"] == {"cwd": "/srv", "backend": "ssh"}  # sibling kept
    assert d["approvals"] == {"mode": "smart"}


def test_merge_that_changes_nothing_returns_the_text_byte_for_byte():
    # Deliberately NOT in the order or style safe_dump would write, so a re-serialisation would show.
    existing = "zeta: 1\napprovals: {mode: smart}\nalpha: 2\n"
    out = hermes_leaf_merge(existing, [{"key": "approvals.mode", "value": "smart"}])
    assert out == existing
    # near-miss: a real change DOES rewrite
    assert hermes_leaf_merge(existing, [{"key": "approvals.mode", "value": "manual"}]) != existing


def test_merge_is_idempotent_on_its_own_output():
    leaves = [{"key": "a.b.c", "value": [1, 2]}, {"key": "x", "value": "y"}]
    once = hermes_leaf_merge("", leaves)
    assert hermes_leaf_merge(once, leaves) == once


def test_merge_refuses_to_nest_under_a_scalar_or_list():
    with pytest.raises(AnsibleFilterError, match="not a mapping"):
        hermes_leaf_merge("terminal: local\n", [{"key": "terminal.backend", "value": "ssh"}])
    with pytest.raises(AnsibleFilterError, match="not a mapping"):
        hermes_leaf_merge("a:\n  - 1\n", [{"key": "a.b", "value": 1}])
    # near-miss: a null intermediate is created, not refused
    assert yaml.safe_load(
        hermes_leaf_merge("terminal:\n", [{"key": "terminal.backend", "value": "ssh"}])
    )["terminal"] == {"backend": "ssh"}


def test_merge_refuses_bad_input():
    with pytest.raises(AnsibleFilterError, match="listed twice"):
        hermes_leaf_merge("", [{"key": "a", "value": 1}, {"key": ["a"], "value": 2}])
    with pytest.raises(AnsibleFilterError, match="bad key"):
        hermes_leaf_merge("", [{"key": "a..b", "value": 1}])
    with pytest.raises(AnsibleFilterError, match="needs key and value"):
        hermes_leaf_merge("", [{"key": "a"}])
    with pytest.raises(AnsibleFilterError, match="not valid YAML"):
        hermes_leaf_merge("a: [unclosed\n", [])
    with pytest.raises(AnsibleFilterError, match="not a mapping"):
        hermes_leaf_merge("- a\n- b\n", [])
    assert hermes_leaf_merge(None, [{"key": "a", "value": 1}]) == "a: 1\n"


def test_merge_keeps_a_false_value_a_null_is_not_dropped():
    out = hermes_leaf_merge(
        "", [{"key": "x.enabled", "value": False}, {"key": "x.n", "value": None}]
    )
    assert yaml.safe_load(out) == {"x": {"enabled": False, "n": None}}


def test_leaf_get_reads_what_merge_wrote_and_distinguishes_absent_from_false():
    text = hermes_leaf_merge("", [{"key": "a.b", "value": False}, {"key": "c", "value": [1, 2]}])
    assert hermes_leaf_get(text, "a.b", "ABSENT") is False  # a false value is not "absent"
    assert hermes_leaf_get(text, ["c"]) == [1, 2]
    assert hermes_leaf_get(text, "a.zzz", "ABSENT") == "ABSENT"
    assert hermes_leaf_get(text, "c.deeper", "ABSENT") == "ABSENT"  # descending through a list
    assert hermes_leaf_get("", "a") is None
    with pytest.raises(AnsibleFilterError):
        hermes_leaf_get(None, "a")


class _Tagged(str):
    """What Ansible actually passes: a str subclass. yaml.safe_dump refuses it."""


def test_merge_accepts_ansibles_str_subclasses_and_writes_plain_yaml():
    # Found by the molecule scenario, not by literals: 'cannot represent an object'.
    leaves = [
        {
            "key": [_Tagged("terminal"), _Tagged("backend")],
            "value": _Tagged("ssh"),
        },
        {"key": _Tagged("skills.external_dirs"), "value": [_Tagged("/opt/shared-skills")]},
    ]
    out = hermes_leaf_merge(_Tagged("memory:\n  x: 1\n"), leaves)
    assert "!!python" not in out
    assert yaml.safe_load(out) == {
        "memory": {"x": 1},
        "terminal": {"backend": "ssh"},
        "skills": {"external_dirs": ["/opt/shared-skills"]},
    }
    spec = {k: (_Tagged(v) if isinstance(v, str) else v) for k, v in GOOD_SPEC.items()}
    spec["toolsets"] = [_Tagged(t) for t in GOOD_SPEC["toolsets"]]
    assert leaves_by_key(hermes_coder_profile_leaves(spec))["approvals.mode"] == "smart"


# ------------------------------------------------------------------------------ coder profile


def leaves_by_key(leaves):
    return {".".join(leaf["key"]): leaf["value"] for leaf in leaves}


def test_good_spec_yields_the_expected_leaves():
    got = leaves_by_key(hermes_coder_profile_leaves(GOOD_SPEC))
    assert got["platform_toolsets.cli"] == ["terminal", "file", "kanban", "todo"]
    assert got["approvals.mode"] == "smart"
    assert got["terminal.backend"] == "ssh" and got["terminal.host"] == "worker.example"
    assert got["skills.external_dirs"] == ["/opt/shared-skills"]
    # with no `platforms` the CLI is the only platform leaf (the default, not an omission)
    assert [k for k in got if k.startswith("platform_toolsets.")] == ["platform_toolsets.cli"]


def test_every_declared_platform_gets_the_same_toolset_leaf():
    # The clone carries a list for every chat platform; a cli-only leaf would leave slack and the
    # API server with the source's wider list.
    spec = copy.deepcopy(GOOD_SPEC)
    spec["platforms"] = ["cli", "slack", "api_server"]
    got = leaves_by_key(hermes_coder_profile_leaves(spec))
    for p in ("cli", "slack", "api_server"):
        assert got[f"platform_toolsets.{p}"] == ["terminal", "file", "kanban", "todo"]
    # refusals: an empty list and a duplicate; near-miss: one platform that is not the CLI
    spec["platforms"] = []
    with pytest.raises(AnsibleFilterError, match="platforms is empty"):
        hermes_coder_profile_leaves(spec)
    spec["platforms"] = ["cli", "cli"]
    with pytest.raises(AnsibleFilterError, match="platform is listed twice"):
        hermes_coder_profile_leaves(spec)
    spec["platforms"] = ["slack"]
    assert "platform_toolsets.slack" in leaves_by_key(hermes_coder_profile_leaves(spec))


@pytest.mark.parametrize(
    "mutate, match",
    [
        (lambda s: s["toolsets"].append("terminall"), "not in known_toolsets"),
        (lambda s: s["toolsets"].append("code_execution"), "way into the gateway"),
        (lambda s: s["toolsets"].append("browser"), "way into the gateway"),
        (lambda s: s["toolsets"].remove("kanban"), "needs"),
        (lambda s: s["toolsets"].remove("terminal"), "needs"),
        (lambda s: s["toolsets"].append("file"), "twice"),
        (lambda s: s.update(approvals_mode="off"), "approvals.mode"),
        (lambda s: s.update(approvals_deny=["git push --force*"]), "recursive delete"),
        (lambda s: s.update(approvals_deny=["rm -rf /*"]), "force push"),
        (lambda s: s.update(terminal={"backend": "local"}), "must be 'ssh'"),
        (lambda s: s.update(terminal={}), "must be 'ssh'"),
        (lambda s: s.update(known_toolsets=[]), "known_toolsets is empty"),
        (lambda s: s.pop("terminal"), "lacks"),
    ],
)
def test_coder_profile_refusals(mutate, match):
    spec = copy.deepcopy(GOOD_SPEC)
    mutate(spec)
    with pytest.raises(AnsibleFilterError, match=match):
        hermes_coder_profile_leaves(spec)


def test_coder_profile_near_misses_pass():
    spec = copy.deepcopy(GOOD_SPEC)
    spec["approvals_mode"] = "manual"  # stricter is fine
    spec["toolsets"] = ["terminal", "kanban"]  # the minimum is fine
    spec.pop("external_dirs")
    got = leaves_by_key(hermes_coder_profile_leaves(spec))
    assert got["approvals.mode"] == "manual"
    assert "skills.external_dirs" not in got
    # an extra deny glob beyond the two required is fine
    spec["approvals_deny"] = spec["approvals_deny"] + ["curl * | sh"]
    hermes_coder_profile_leaves(spec)


def test_a_kanban_only_leaf_is_what_the_omission_trap_looks_like():
    # The reason known_toolsets exists: this list is VALID names, and it would still replace the
    # platform's whole tool list. The filter cannot know that; it can refuse what the profile must
    # never lack, which is the same protection from the other side.
    spec = copy.deepcopy(GOOD_SPEC)
    spec["toolsets"] = ["kanban"]
    with pytest.raises(AnsibleFilterError, match="needs"):
        hermes_coder_profile_leaves(spec)


# ------------------------------------------------------------------------------ kanban placement


def test_placement_ok_when_exactly_slack_and_cli_have_it():
    r = hermes_kanban_placement(
        {
            "slack": ["web", "kanban"],
            "cli": ["kanban", "terminal"],
            "api_server": ["web", "memory"],
        },
        ["slack", "cli"],
    )
    assert r["verdict"] == "ok"
    assert r["enabled_on"] == ["cli", "slack"]


def test_placement_wrong_when_the_api_server_has_it():
    r = hermes_kanban_placement(
        {"slack": ["kanban"], "cli": ["kanban"], "api_server": ["web", "kanban"]},
        ["slack", "cli"],
    )
    assert r["verdict"] == "wrong" and r["unexpected_on"] == ["api_server"]


def test_placement_wrong_when_an_allowed_platform_lacks_it():
    r = hermes_kanban_placement({"slack": ["web"], "cli": ["kanban"]}, ["slack", "cli"])
    assert r["verdict"] == "wrong" and r["missing_on"] == ["slack"]


def test_placement_unreadable_platform_is_cannot_tell_never_off():
    r = hermes_kanban_placement(
        {"slack": ["kanban"], "cli": None, "api_server": ["web"]}, ["slack", "cli"]
    )
    assert r["verdict"] == "cannot-tell" and r["cannot_tell"] == ["cli"]
    # an allowed platform that was never read at all is also cannot-tell
    r2 = hermes_kanban_placement({"slack": ["kanban"]}, ["slack", "cli"])
    assert r2["verdict"] == "cannot-tell" and r2["cannot_tell"] == ["cli"]
    # near-miss: an unreadable platform that is NOT expected to have it is still unknown, not ok
    r3 = hermes_kanban_placement(
        {"slack": ["kanban"], "cli": ["kanban"], "api_server": None}, ["slack", "cli"]
    )
    assert r3["verdict"] == "cannot-tell"


def test_placement_unreadable_list_is_cannot_tell_without_passing_null():
    # Ansible cannot carry a null through set_fact reliably, so the unreadable platforms are NAMED.
    r = hermes_kanban_placement(
        {"slack": ["kanban"], "cli": ["kanban"]}, ["slack", "cli"], ["api_server"]
    )
    assert r["verdict"] == "cannot-tell" and r["cannot_tell"] == ["api_server"]
    # an unreadable ALLOWED platform is cannot-tell, not "missing"
    r2 = hermes_kanban_placement({"slack": ["kanban"]}, ["slack", "cli"], ["cli"])
    assert r2["verdict"] == "cannot-tell" and r2["missing_on"] == []
    # near-miss: an empty unreadable list changes nothing
    r3 = hermes_kanban_placement(
        {"slack": ["kanban"], "cli": ["kanban"], "api_server": ["web"]}, ["slack", "cli"], []
    )
    assert r3["verdict"] == "ok"


def test_placement_refuses_bad_input():
    with pytest.raises(AnsibleFilterError):
        hermes_kanban_placement(["not", "a", "dict"], ["slack"])
    with pytest.raises(AnsibleFilterError, match="empty"):
        hermes_kanban_placement({"slack": []}, [])


# ------------------------------------------------------------------------------ github token


def test_token_verdict_refuses_a_classic_token_that_can_push_or_merge():
    for header in (
        "repo",
        "repo, read:org",
        "public_repo",
        "workflow",
        "write:packages",
        "admin:org",
    ):
        v = hermes_github_token_verdict(200, header)
        assert v["outcome"] == "refused", header
        assert "write-shaped" in v["reason"]


def test_token_verdict_accepts_a_classic_token_whose_scopes_are_all_read_shaped():
    v = hermes_github_token_verdict(200, "read:org, read:user")
    assert v["outcome"] == "classic-read"
    # near-miss: `repo` as a SUBSTRING of a read scope is not the `repo` scope
    assert hermes_github_token_verdict(200, "read:repo_hook")["outcome"] == "classic-read"


def test_token_verdict_names_a_fine_grained_token_as_its_own_outcome_not_a_pass():
    v = hermes_github_token_verdict(200, None)
    assert v["outcome"] == "fine-grained" and "cannot be read" in v["reason"]
    assert hermes_github_token_verdict("200", "")["outcome"] == "fine-grained"


def test_token_verdict_refuses_a_dead_token_and_a_non_200():
    assert hermes_github_token_verdict(401, "repo")["outcome"] == "refused"
    assert hermes_github_token_verdict(403, "")["outcome"] == "refused"
    assert hermes_github_token_verdict(-1, "")["outcome"] == "refused"
    with pytest.raises(AnsibleFilterError, match="not an int"):
        hermes_github_token_verdict("none", "")


def test_token_verdict_names_the_write_scopes_only_on_a_live_write_token():
    # A caller may knowingly accept ONE write-capable token, keyed on this list. A dead
    # token must never look exceptable, even when the header it came back with names `repo`.
    v = hermes_github_token_verdict(200, "repo, workflow, read:org")
    assert v["outcome"] == "refused" and v["write_scopes"] == ["repo", "workflow"]
    for status, header in ((401, "repo"), (403, "repo"), (200, "read:org"), (200, "")):
        assert hermes_github_token_verdict(status, header)["write_scopes"] == [], (status, header)


# ------------------------------------------------------------------------------ platform sweep

CLONED = "platform_toolsets:\n  cli: [web, code_execution]\n  slack: [web, browser, code_execution]\n  api_server: [web]\nmemory: {x: 1}\n"


def test_sweep_sets_every_cloned_platform_the_explicit_leaves_do_not_name():
    explicit = [{"key": ["platform_toolsets", "cli"], "value": ["terminal", "kanban"]}]
    got = leaves_by_key(hermes_platform_sweep_leaves(CLONED, ["terminal", "kanban"], explicit))
    assert got == {
        "platform_toolsets.slack": ["terminal", "kanban"],
        "platform_toolsets.api_server": ["terminal", "kanban"],
    }
    # and merged on top of the clone, no platform keeps the source's shell
    merged = yaml.safe_load(
        hermes_leaf_merge(
            CLONED,
            hermes_platform_sweep_leaves(CLONED, ["terminal", "kanban"], explicit) + explicit,
        )
    )
    assert all(v == ["terminal", "kanban"] for v in merged["platform_toolsets"].values())
    assert merged["memory"] == {"x": 1}


def test_sweep_is_a_no_op_with_no_toolsets_no_platform_map_or_an_empty_file():
    assert hermes_platform_sweep_leaves(CLONED, [], []) == []
    assert hermes_platform_sweep_leaves("memory: {x: 1}\n", ["terminal"], []) == []
    assert hermes_platform_sweep_leaves("", ["terminal"], []) == []
    # near-miss: a dotted-string explicit key names the platform just as a list key does
    got = hermes_platform_sweep_leaves(
        CLONED, ["terminal"], [{"key": "platform_toolsets.slack", "value": []}]
    )
    assert sorted(k["key"][1] for k in got) == ["api_server", "cli"]


def test_sweep_refuses_bad_input():
    with pytest.raises(AnsibleFilterError):
        hermes_platform_sweep_leaves(123, ["terminal"], [])
    with pytest.raises(AnsibleFilterError, match="needs a key"):
        hermes_platform_sweep_leaves(CLONED, ["terminal"], [{"value": 1}])
