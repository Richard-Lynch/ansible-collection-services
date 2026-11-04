# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright: (c) 2026, Richard Lynch
# GNU General Public License v3.0+ (see https://www.gnu.org/licenses/gpl-3.0.txt)
"""Declared automations on Hermes: the cron seed and the dynamic webhook subscriptions.

Two populations share one cron store and one webhook listener:

* SEEDED jobs are declared by the caller (a cron seed list). The role installs each one PAUSED, a
  separate enable step resumes the ones declared `enabled: true`, and a probe reports a deleted,
  edited or paused seeded job as drift.
* PERSONAL jobs are the agent's and its users', created by chat. They are never touched here; a
  job is "seeded" only because its NAME is in the seed.

This module is the comparison, as pure functions, so the converge, the enable step and the probe
cannot disagree about what "drift" means.

    hermes_cron_seed_plan(jobs_doc, seed)
        -> {create: [seed...], edit: [{id, name, fields: {...}}], resume: [{id, name}],
            rows: [{check, verdict, detail}], duplicates: [name...]}

    hermes_dynamic_routes_audit(subs_doc)
        -> {flagged: [{name, toolsets}], repaired: <doc with toolsets removed>, count: n}

`jobs_doc` is cron/jobs.json as the daemon wrote it ({"jobs": [...]}); `subs_doc` is
webhook_subscriptions.json (a mapping of route name -> route). Neither input is mutated.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from ansible.errors import AnsibleFilterError

_NAME_RE = re.compile(r"\A[a-z0-9][a-z0-9-]{1,62}\Z")
# The fields the seed owns on a job, mapped to the key the daemon stores them under. Anything
# else on the record (last_run_at, next_run_at, state, origin...) is the scheduler's own.
_OWNED_FIELDS = (
    "schedule",
    "prompt",
    "script",
    "no_agent",
    "deliver",
    "model",
    "skills",
)
_SEED_KEYS = set(_OWNED_FIELDS) | {"name", "enabled", "provider", "purpose"}


def _schedule_text(job: dict) -> str:
    """The schedule as the user wrote it. The store keeps `schedule` as a dict at this pin
    ({kind, expr, display}); an older store kept the string. Compare on what a human typed."""
    sched = job.get("schedule")
    if isinstance(sched, dict):
        for key in ("display", "expr", "raw"):
            if sched.get(key):
                return str(sched[key]).strip()
        return ""
    return str(sched or "").strip()


_INTERVAL_RE = re.compile(
    r"\A(?:every\s+)?(\d+)\s*(m|min|mins|minutes?|h|hr|hrs|hours?|d|days?)\Z", re.I
)
_UNIT_MINUTES = {"m": 1, "h": 60, "d": 1440}


def _normalise_schedule(text: str) -> str:
    """The daemon stores an interval as `every <minutes>m` whatever was typed ("every 1h" is
    kept as "every 60m"), so a seed written in hours would drift for ever if compared raw. Cron
    expressions and day/time phrases keep the text as typed, so they pass through unchanged."""
    text = str(text).strip()
    m = _INTERVAL_RE.match(text)
    if not m:
        return text
    return f"every {int(m.group(1)) * _UNIT_MINUTES[m.group(2)[0].lower()]}m"


def _enabled(job: dict) -> bool:
    """Mirrors cron/jobs.py effective_job_state: enabled AND no pause marker."""
    state = str(job.get("state") or "").strip()
    return bool(job.get("enabled", True)) and state != "paused" and not job.get("paused_at")


def _validate_seed(seed: Any) -> list[dict]:
    if not isinstance(seed, list):
        raise AnsibleFilterError(
            f"hermes_cron_seed_plan: seed must be a list, got {type(seed).__name__}"
        )
    names: set[str] = set()
    out = []
    for i, entry in enumerate(seed):
        if not isinstance(entry, dict):
            raise AnsibleFilterError(f"hermes_cron_seed_plan: seed[{i}] is not a mapping")
        unknown = set(entry) - _SEED_KEYS
        if unknown:
            # A typo'd key would be silently ignored by the create and then never drift-checked.
            raise AnsibleFilterError(
                f"hermes_cron_seed_plan: seed[{i}] has unknown keys {sorted(unknown)}"
            )
        name = str(entry.get("name", ""))
        if not _NAME_RE.match(name):
            raise AnsibleFilterError(
                f"hermes_cron_seed_plan: seed[{i}] name {name!r} must be a lowercase slug (2-63 chars)"
            )
        if name in names:
            raise AnsibleFilterError(f"hermes_cron_seed_plan: seed name {name!r} is declared twice")
        names.add(name)
        if not str(entry.get("schedule", "")).strip():
            raise AnsibleFilterError(f"hermes_cron_seed_plan: seed {name!r} has no schedule")
        if entry.get("no_agent"):
            if not entry.get("script"):
                raise AnsibleFilterError(
                    f"hermes_cron_seed_plan: seed {name!r} is no_agent without a script"
                )
            if entry.get("prompt") or entry.get("model"):
                raise AnsibleFilterError(
                    f"hermes_cron_seed_plan: seed {name!r} is no_agent, so a prompt or model would never be used"
                )
        elif not entry.get("prompt") and not entry.get("script"):
            raise AnsibleFilterError(
                f"hermes_cron_seed_plan: seed {name!r} has neither a prompt nor a script"
            )
        elif not entry.get("model"):
            # A model is pinned on EVERY agent cron job; a job that follows the main model silently
            # moves to whatever the dashboard picks next.
            raise AnsibleFilterError(
                f"hermes_cron_seed_plan: seed {name!r} runs the agent but pins no model"
            )
        script = str(entry.get("script") or "")
        if script and (script.startswith("/") or ".." in script.split("/")):
            raise AnsibleFilterError(
                f"hermes_cron_seed_plan: seed {name!r} script {script!r} must be relative to the scripts dir"
            )
        out.append(entry)
    return out


def _desired(entry: dict) -> dict:
    return {
        "schedule": _normalise_schedule(entry["schedule"]),
        "prompt": str(entry.get("prompt") or "").strip(),
        "script": str(entry.get("script") or ""),
        "no_agent": bool(entry.get("no_agent", False)),
        "deliver": str(entry.get("deliver") or "local"),
        "model": str(entry.get("model") or ""),
        "skills": sorted(str(s) for s in (entry.get("skills") or [])),
    }


def _live(job: dict) -> dict:
    model = job.get("model")
    if isinstance(model, dict):  # some pins store {model, provider}
        model = model.get("model") or model.get("name")
    skills = job.get("skills") or ([job["skill"]] if job.get("skill") else [])
    return {
        "schedule": _normalise_schedule(_schedule_text(job)),
        "prompt": str(job.get("prompt") or "").strip(),
        "script": str(job.get("script") or ""),
        "no_agent": bool(job.get("no_agent", False)),
        "deliver": str(job.get("deliver") or "local"),
        "model": str(model or ""),
        "skills": sorted(str(s) for s in skills),
    }


def hermes_cron_seed_plan(jobs_doc: Any, seed: Any) -> dict:
    seed = _validate_seed(seed)
    if jobs_doc is None or jobs_doc == {} or jobs_doc == "":
        jobs: list = []
        store_readable = True  # no store yet is a first deploy, not an unreadable one
    elif isinstance(jobs_doc, dict) and isinstance(jobs_doc.get("jobs", []), list):
        jobs = [j for j in jobs_doc.get("jobs", []) if isinstance(j, dict)]
        store_readable = True
    else:
        jobs = []
        store_readable = False

    by_name: dict[str, list[dict]] = {}
    for job in jobs:
        by_name.setdefault(str(job.get("name") or ""), []).append(job)

    plan: dict[str, Any] = {
        "create": [],
        "edit": [],
        "resume": [],
        "rows": [],
        "duplicates": [],
    }
    for entry in seed:
        name = entry["name"]
        want = _desired(entry)
        if not store_readable:
            plan["rows"].append(
                {
                    "check": f"cron:{name}",
                    "verdict": "cannot-tell",
                    "detail": "cron/jobs.json did not parse as {jobs: [...]}",
                }
            )
            continue
        found = by_name.get(name, [])
        if len(found) > 1:
            # Two jobs under one seeded name: editing "the" job would pick one at random.
            plan["duplicates"].append(name)
            plan["rows"].append(
                {
                    "check": f"cron:{name}",
                    "verdict": "drift",
                    "detail": f"{len(found)} jobs named {name}; remove the extra by id",
                }
            )
            continue
        if not found:
            plan["create"].append(entry)
            plan["rows"].append(
                {
                    "check": f"cron:{name}",
                    "verdict": "drift",
                    "detail": "absent from the cron store",
                }
            )
            continue
        job = found[0]
        have = _live(job)
        diff = {k: want[k] for k in _OWNED_FIELDS if want[k] != have[k]}
        if diff:
            plan["edit"].append(
                {
                    "id": str(job.get("id", "")),
                    "name": name,
                    "fields": diff,
                    "was": {k: have[k] for k in diff},
                }
            )
        want_enabled = bool(entry.get("enabled", False))
        is_enabled = _enabled(job)
        if want_enabled and not is_enabled:
            plan["resume"].append({"id": str(job.get("id", "")), "name": name})
        problems = []
        if diff:
            problems.append("edited: " + ", ".join(sorted(diff)))
        if want_enabled != is_enabled:
            problems.append(
                "paused (declared enabled; run the enable step)"
                if want_enabled
                else "enabled but declared paused"
            )
        plan["rows"].append(
            {
                "check": f"cron:{name}",
                "verdict": "drift" if problems else "matches",
                "detail": (
                    "; ".join(problems)
                    if problems
                    else ("enabled" if is_enabled else "paused, as declared")
                ),
            }
        )
    return plan


PAUSED_REASON = "declared seed: paused until the enable step resumes it"


def hermes_cron_argv(
    entry: dict,
    action: str,
    provider: str = "",
    job_id: str = "",
    fields: Any = None,
    paused_reason: str = "",
) -> list:
    """The `hermes cron create|edit` argv for one seed entry (list form: no shell, no quoting).

    create: the whole job, PAUSED, with an auditable reason (`paused_reason`, else PAUSED_REASON)
            -- the converge never enables.
    edit:   only the drifted fields named in `fields` (the plan's edit entry), by job id.
    """
    entry = _validate_seed([entry])[0]
    want = _desired(entry)
    model_args = (
        (["--model", want["model"]] + (["--provider", provider] if provider else []))
        if want["model"]
        else []
    )
    if action == "create":
        argv = ["hermes", "cron", "create", str(entry["schedule"]).strip()]
        if want["prompt"]:
            argv.append(want["prompt"])
        argv += ["--name", entry["name"], "--deliver", want["deliver"]]
        if want["script"]:
            argv += ["--script", want["script"]]
        if want["no_agent"]:
            argv.append("--no-agent")
        argv += model_args
        for skill in want["skills"]:
            argv += ["--skill", skill]
        return argv + ["--paused", "--paused-reason", str(paused_reason or PAUSED_REASON)]
    if action == "edit":
        if not job_id:
            raise AnsibleFilterError("hermes_cron_argv: edit needs the job id")
        fields = dict(fields or {})
        if not fields:
            raise AnsibleFilterError("hermes_cron_argv: edit with no drifted fields")
        argv = ["hermes", "cron", "edit", str(job_id)]
        flag = {
            "schedule": "--schedule",
            "prompt": "--prompt",
            "deliver": "--deliver",
            "script": "--script",
        }
        for key in ("schedule", "prompt", "deliver", "script"):
            if key in fields:
                argv += [
                    flag[key],
                    str(entry["schedule"]).strip() if key == "schedule" else str(fields[key]),
                ]
        if "model" in fields:
            argv += model_args
        if "no_agent" in fields:
            argv.append("--no-agent" if fields["no_agent"] else "--agent")
        if "skills" in fields:
            if fields["skills"]:
                for skill in fields["skills"]:
                    argv += ["--skill", skill]
            else:
                argv.append("--clear-skills")
        return argv
    raise AnsibleFilterError(f"hermes_cron_argv: unknown action {action!r}")


def hermes_dynamic_routes_audit(subs_doc: Any) -> dict:
    """Flag every dynamic webhook subscription that carries `toolsets`.

    `hermes webhook subscribe` cannot set toolsets, but the adapter honours a hand-written one
    (toolsets_for_source reads the merged route table), so an entry carrying it is either a hand
    edit or a self-grant. The repair is to DROP the key, which returns the route to the default
    small toolset rather than deleting a subscription its user may want.
    """
    if subs_doc is None or subs_doc == {} or subs_doc == "":
        return {"flagged": [], "repaired": {}, "count": 0, "readable": True}
    if not isinstance(subs_doc, dict):
        return {"flagged": [], "repaired": subs_doc, "count": 0, "readable": False}
    repaired = copy.deepcopy(subs_doc)
    flagged = []
    for name, route in subs_doc.items():
        if isinstance(route, dict) and "toolsets" in route:
            flagged.append({"name": str(name), "toolsets": route.get("toolsets")})
            del repaired[name]["toolsets"]
    return {
        "flagged": sorted(flagged, key=lambda f: f["name"]),
        "repaired": repaired,
        "count": len(subs_doc),
        "readable": True,
    }


class FilterModule:
    def filters(self):
        return {
            "hermes_cron_seed_plan": hermes_cron_seed_plan,
            "hermes_dynamic_routes_audit": hermes_dynamic_routes_audit,
            "hermes_cron_argv": hermes_cron_argv,
        }
