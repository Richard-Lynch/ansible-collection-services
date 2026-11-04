# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for plugins/filter/hermes_automations.py (cron seed drift, dynamic routes).

Each case that must report drift is paired with a near-miss that must not, because a comparison
that always says "drift" and one that never does both look fine from a single case.

Run: python -m pytest tests/unit -q (from the collection root)
"""

from __future__ import annotations

import copy
import os
import sys

import pytest
from ansible.errors import AnsibleFilterError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "plugins", "filter"))

from hermes_automations import (  # noqa: E402
    PAUSED_REASON,
    hermes_cron_argv,
    hermes_cron_seed_plan,
    hermes_dynamic_routes_audit,
)

SEED = [
    {
        "name": "watch-feed",
        "schedule": "every 5m",
        "script": "jobs/watch_feed.py",
        "no_agent": True,
        "deliver": "slack",
        "enabled": True,
    },
    {
        "name": "digest-inbox",
        "schedule": "every 15m",
        "script": "jobs/digest_precheck.py",
        "prompt": "Summarise the items listed above.",
        "model": "model-small",
        "skills": ["example-skill-a", "example-skill-b"],
        "deliver": "slack",
        "enabled": False,
    },
]


def _job(name, **kw):
    base = {"id": f"id-{name}", "name": name, "enabled": True, "state": "scheduled"}
    base.update(kw)
    return base


def _matching_store():
    return {
        "jobs": [
            _job(
                "watch-feed",
                schedule={"kind": "interval", "minutes": 5, "display": "every 5m"},
                script="jobs/watch_feed.py",
                no_agent=True,
                deliver="slack",
            ),
            _job(
                "digest-inbox",
                schedule={"kind": "interval", "minutes": 15, "display": "every 15m"},
                script="jobs/digest_precheck.py",
                prompt="Summarise the items listed above.",
                model="model-small",
                skills=["example-skill-b", "example-skill-a"],
                deliver="slack",
                enabled=False,
                state="paused",
                paused_at="2030-01-01T10:00:00",
            ),
            _job(
                "remind-friday",
                schedule={"kind": "cron", "expr": "0 9 * * 5", "display": "0 9 * * 5"},
                prompt="Remind me",
                deliver="slack",
            ),  # a personal job: never touched
        ]
    }


def _row(plan, name):
    return next(r for r in plan["rows"] if r["check"] == f"cron:{name}")


def test_matching_store_is_all_matches_and_ignores_personal_jobs():
    plan = hermes_cron_seed_plan(_matching_store(), SEED)
    assert [r["verdict"] for r in plan["rows"]] == ["matches", "matches"]
    assert plan["create"] == [] and plan["edit"] == [] and plan["resume"] == []
    assert len(plan["rows"]) == 2  # the personal job got no row


def test_missing_job_is_created_and_is_drift():
    store = _matching_store()
    store["jobs"] = [j for j in store["jobs"] if j["name"] != "digest-inbox"]
    plan = hermes_cron_seed_plan(store, SEED)
    assert [e["name"] for e in plan["create"]] == ["digest-inbox"]
    assert _row(plan, "digest-inbox")["verdict"] == "drift"
    assert _row(plan, "watch-feed")["verdict"] == "matches"


def test_first_deploy_with_no_store_creates_everything():
    for empty in (None, {}, ""):
        plan = hermes_cron_seed_plan(empty, SEED)
        assert [e["name"] for e in plan["create"]] == [
            "watch-feed",
            "digest-inbox",
        ]


def test_unparseable_store_is_cannot_tell_not_absent():
    # A store that is not {jobs: [...]} must never read as "nothing there" -> recreate everything.
    plan = hermes_cron_seed_plan(["not", "a", "store"], SEED)
    assert {r["verdict"] for r in plan["rows"]} == {"cannot-tell"}
    assert plan["create"] == []


def test_hand_edited_schedule_is_drift_with_an_edit():
    store = _matching_store()
    store["jobs"][0]["schedule"] = {
        "kind": "interval",
        "minutes": 60,
        "display": "every 60m",
    }
    plan = hermes_cron_seed_plan(store, SEED)
    assert plan["edit"] == [
        {
            "id": "id-watch-feed",
            "name": "watch-feed",
            "fields": {"schedule": "every 5m"},
            "was": {"schedule": "every 60m"},
        }
    ]
    assert "edited: schedule" in _row(plan, "watch-feed")["detail"]


def test_interval_in_hours_is_normalised_like_the_daemon_stores_it():
    seed = copy.deepcopy(SEED)
    seed[0]["schedule"] = "every 1h"
    store = _matching_store()
    store["jobs"][0]["schedule"] = {
        "kind": "interval",
        "minutes": 60,
        "display": "every 60m",
    }
    plan = hermes_cron_seed_plan(store, seed)
    assert _row(plan, "watch-feed")["verdict"] == "matches"


def test_skills_compare_as_a_set_not_an_order():
    plan = hermes_cron_seed_plan(_matching_store(), SEED)  # live order differs from the seed
    assert _row(plan, "digest-inbox")["verdict"] == "matches"


def test_declared_enabled_but_paused_is_drift_and_resumable():
    store = _matching_store()
    store["jobs"][0].update(enabled=False, state="paused")
    plan = hermes_cron_seed_plan(store, SEED)
    assert plan["resume"] == [{"id": "id-watch-feed", "name": "watch-feed"}]
    assert "enable step" in _row(plan, "watch-feed")["detail"]


def test_declared_paused_and_paused_is_matches():
    plan = hermes_cron_seed_plan(_matching_store(), SEED)
    assert _row(plan, "digest-inbox")["detail"] == "paused, as declared"
    assert {"id": "id-digest-inbox", "name": "digest-inbox"} not in plan["resume"]


def test_pause_marker_alone_counts_as_paused():
    store = _matching_store()
    store["jobs"][0]["paused_at"] = "2030-01-01T11:00:00"  # enabled flag still true
    plan = hermes_cron_seed_plan(store, SEED)
    assert _row(plan, "watch-feed")["verdict"] == "drift"


def test_duplicate_seeded_names_are_drift_and_never_edited():
    store = _matching_store()
    store["jobs"].append(_job("watch-feed", id="id-dup"))
    plan = hermes_cron_seed_plan(store, SEED)
    assert plan["duplicates"] == ["watch-feed"]
    assert all(e["name"] != "watch-feed" for e in plan["edit"])


@pytest.mark.parametrize(
    "bad, why",
    [
        (
            {"name": "x y", "schedule": "every 5m", "script": "a.py", "no_agent": True},
            "slug",
        ),
        (
            {"name": "ok-name", "schedule": "", "script": "a.py", "no_agent": True},
            "schedule",
        ),
        (
            {"name": "ok-name", "schedule": "every 5m", "no_agent": True},
            "without a script",
        ),
        (
            {
                "name": "ok-name",
                "schedule": "every 5m",
                "script": "a.py",
                "no_agent": True,
                "model": "model-small",
            },
            "never be used",
        ),
        ({"name": "ok-name", "schedule": "every 5m", "prompt": "hi"}, "pins no model"),
        (
            {
                "name": "ok-name",
                "schedule": "every 5m",
                "script": "/etc/passwd",
                "no_agent": True,
            },
            "relative",
        ),
        (
            {
                "name": "ok-name",
                "schedule": "every 5m",
                "script": "../x.py",
                "no_agent": True,
            },
            "relative",
        ),
        (
            {
                "name": "ok-name",
                "schedule": "every 5m",
                "prompt": "hi",
                "model": "m",
                "toolset": ["x"],
            },
            "unknown keys",
        ),
    ],
)
def test_bad_seed_entries_are_refused(bad, why):
    with pytest.raises(AnsibleFilterError, match=why):
        hermes_cron_seed_plan({}, [bad])


def test_a_name_declared_twice_is_refused():
    with pytest.raises(AnsibleFilterError, match="twice"):
        hermes_cron_seed_plan({}, [SEED[0], SEED[0]])


def test_inputs_are_not_mutated():
    store, seed = _matching_store(), copy.deepcopy(SEED)
    before = (copy.deepcopy(store), copy.deepcopy(seed))
    hermes_cron_seed_plan(store, seed)
    assert (store, seed) == before


# ---- dynamic webhook subscriptions -----------------------------------------------------------


def test_a_planted_toolsets_entry_is_flagged_and_repaired_without_deleting_the_route():
    subs = {
        "watch-repo": {"secret": "s" * 32, "prompt": "x", "toolsets": ["terminal"]},
        "plain": {"secret": "s" * 32, "prompt": "y"},
    }
    out = hermes_dynamic_routes_audit(subs)
    assert out["flagged"] == [{"name": "watch-repo", "toolsets": ["terminal"]}]
    assert "toolsets" not in out["repaired"]["watch-repo"]
    assert out["repaired"]["watch-repo"]["prompt"] == "x"
    assert out["repaired"]["plain"] == subs["plain"]
    assert "toolsets" in subs["watch-repo"]  # input untouched


def test_clean_subscriptions_flag_nothing():
    subs = {"plain": {"secret": "s" * 32, "prompt": "y"}}
    out = hermes_dynamic_routes_audit(subs)
    assert out["flagged"] == [] and out["repaired"] == subs and out["count"] == 1


def test_empty_toolsets_list_is_still_flagged():
    # [] is falsy upstream, but the key's PRESENCE is the hand edit the rule exists to catch.
    out = hermes_dynamic_routes_audit({"r": {"toolsets": []}})
    assert [f["name"] for f in out["flagged"]] == ["r"]


def test_no_file_is_readable_and_empty_but_a_list_is_not_readable():
    assert hermes_dynamic_routes_audit(None)["readable"] is True
    assert hermes_dynamic_routes_audit([1, 2])["readable"] is False


# ---- the create / edit argv ------------------------------------------------------------------


def test_create_argv_is_paused_and_complete():
    argv = hermes_cron_argv(SEED[1], "create", provider="example-gateway")
    assert argv[:5] == [
        "hermes",
        "cron",
        "create",
        "every 15m",
        "Summarise the items listed above.",
    ]
    assert argv[-3:] == ["--paused", "--paused-reason", PAUSED_REASON]
    assert ["--model", "model-small", "--provider", "example-gateway"] == argv[
        argv.index("--model") : argv.index("--model") + 4
    ]
    assert argv.count("--skill") == 2 and "--no-agent" not in argv


def test_create_argv_for_a_no_agent_job_has_no_prompt_and_no_model():
    argv = hermes_cron_argv(SEED[0], "create", provider="example-gateway")
    assert argv[3] == "every 5m" and argv[4] == "--name"
    assert "--no-agent" in argv and "--model" not in argv


def test_edit_argv_touches_only_the_drifted_fields():
    argv = hermes_cron_argv(SEED[0], "edit", job_id="abc", fields={"schedule": "every 5m"})
    assert argv == ["hermes", "cron", "edit", "abc", "--schedule", "every 5m"]


def test_edit_argv_restores_skills_and_mode():
    argv = hermes_cron_argv(
        SEED[1], "edit", job_id="j", fields={"skills": ["a"], "no_agent": False}
    )
    assert argv[-3:] == ["--agent", "--skill", "a"] or argv[-3:] == ["--skill", "a", "--agent"]
    cleared = hermes_cron_argv(SEED[1], "edit", job_id="j", fields={"skills": []})
    assert cleared[-1] == "--clear-skills"


def test_edit_without_an_id_or_fields_is_refused():
    with pytest.raises(AnsibleFilterError, match="job id"):
        hermes_cron_argv(SEED[0], "edit", fields={"schedule": "x"})
    with pytest.raises(AnsibleFilterError, match="no drifted"):
        hermes_cron_argv(SEED[0], "edit", job_id="j", fields={})


def test_the_plan_edit_feeds_the_argv():
    store = _matching_store()
    store["jobs"][0]["deliver"] = "local"
    edit = hermes_cron_seed_plan(store, SEED)["edit"][0]
    assert hermes_cron_argv(SEED[0], "edit", job_id=edit["id"], fields=edit["fields"]) == [
        "hermes",
        "cron",
        "edit",
        "id-watch-feed",
        "--deliver",
        "slack",
    ]
