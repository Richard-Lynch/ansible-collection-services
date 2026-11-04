# SPDX-License-Identifier: GPL-3.0-or-later
# Runs INSIDE the Hermes container as the agent's uid (the cold recall test). It loads the
# memory provider the agent is CONFIGURED with, through Hermes's own loader
# (plugins/memory load_memory_provider at v0.22.5), and points it at a SCRATCH home so the test's
# 20 notes never enter the user's memory. The provider reads its settings (embeddings on,
# through the gateway) from the live config exactly as the agent does; only the database
# file differs. The scratch directory is excluded from the backup and the mirror (inventory).
#
# Phases: init-live (open the LIVE store once so its database exists), seed, search, delete,
# restore, negative, cleanup. `search` is what runs again after the cold restart.
import json
import os
import shutil
import sys
import time
from pathlib import Path

args = json.loads(sys.argv[1])
phase = args["phase"]
spec = args.get("spec") or {}
sys.path.insert(0, os.getcwd())
# FIRST, as every Hermes entry point does (hermes_cli/main.py, run_agent.py): hermes_bootstrap
# activates PM's committed dependency environment, which is where a plugin's own requirements live
# (holographic needs numpy for search by meaning and HRR). Without it the provider runs on a bare
# interpreter ("numpy is not installed; words only") while the agent itself has numpy, and the
# recall test measures word matching alone. Absent in a unit-test
# fixture home, where there is no Hermes tree to bootstrap.
try:
    import hermes_bootstrap  # noqa: E402,F401
except ModuleNotFoundError as exc:
    if exc.name != "hermes_bootstrap":
        raise
from hermes_cli.config import cfg_get, load_config_readonly  # noqa: E402
from plugins.memory import load_memory_provider  # noqa: E402

name = cfg_get(load_config_readonly(), "memory", "provider", default="") or ""
if not name:
    print(json.dumps({"ok": False, "error": "memory.provider is not set (built-in memory only)"}))
    sys.exit(0)
provider = load_memory_provider(name)
if provider is None:
    print(json.dumps({"ok": False, "error": f"provider {name!r} did not load"}))
    sys.exit(0)

home = os.environ.get("HERMES_HOME", "/opt/data") if phase == "init-live" else args["scratch_home"]
scratch = Path(home)
mapping_file = scratch / "note_ids.json"
if phase == "seed" and scratch.exists() and args.get("scratch_home"):
    shutil.rmtree(scratch)
scratch.mkdir(parents=True, exist_ok=True)
# GUARD: the test notes must land in the scratch store, never the live one. A configured absolute
# db_path would ignore hermes_home, so ask the provider where it is about to write and refuse
# anything outside the scratch directory before a single note is added.
where = getattr(provider, "_db_path", None)
if phase != "init-live":
    target = where(str(scratch)) if callable(where) else None
    if not target or not str(Path(target).expanduser().resolve()).startswith(
        str(scratch.resolve()) + os.sep
    ):
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": f"provider would write to {target!r}, outside the scratch home",
                }
            )
        )
        sys.exit(0)
provider.initialize(
    "recall-test" if phase != "init-live" else "learning-init",
    hermes_home=str(scratch),
    agent_context="primary",
    platform="cli",
)


def tool(action, **kw):
    return json.loads(provider.handle_tool_call("fact_store", {"action": action, **kw}))


def catch_up(seconds=120):
    """Vectors are made after a turn on the provider's worker; drive that step directly until every
    fact has one (or the budget runs out) so the search measures meaning, not a cold index."""
    fn = getattr(provider, "_catch_up_vectors", None)
    deadline = time.time() + seconds
    stats = tool("stats")
    while fn is not None and time.time() < deadline:
        fn()
        stats = tool("stats")
        # Holographic 0.22.0 reports it as "search_by_meaning": "off", or {status, model,
        # facts_with_vector, waiting, never_sent, ...}; there is no "meaning" key, and reading one
        # stops this loop after ONE pass, so the search runs on whatever vectors that pass made.
        meaning = stats.get("search_by_meaning") if isinstance(stats, dict) else None
        if not isinstance(meaning, dict) or not meaning.get("waiting"):
            break
        time.sleep(1)
    return stats


# How each returned fact matched. Holographic (0.22.0) tags a result found by meaning with
# match="meaning"; together with stats.search_by_meaning (endpoint status and vector counts) it is the
# evidence of whether search by meaning was live: a 0 here means the score measured word matching alone.
match_kinds = {}


def search_all():
    ids = json.loads(mapping_file.read_text()) if mapping_file.is_file() else {}
    by_fact = {str(v): k for k, v in ids.items()}
    results = {}
    match_kinds.clear()
    for q in spec["questions"]:
        got = tool("search", query=q["text"], limit=5)
        rows = (got.get("results") or [])[:5]
        for r in rows:
            kind = str(r.get("match") or "unlabelled")
            match_kinds[kind] = match_kinds.get(kind, 0) + 1
        results[q["id"]] = [
            by_fact.get(str(r.get("fact_id")), f"fact:{r.get('fact_id')}") for r in rows
        ]
    return results


out = {"ok": True, "phase": phase, "provider": name}
if phase == "init-live":
    out["stats"] = tool("stats")
    out["db_exists"] = any(Path(home).glob("memory_store*.db"))
elif phase == "seed":
    ids = {}
    for note in spec["notes"]:
        got = tool("add", content=note["text"], keywords=note.get("keywords", []))
        if got.get("fact_id") is None:
            out.update(
                ok=False,
                error=f"note {note['id']} not stored: {str(got.get('error'))[:120]}",
            )
            break
        ids[note["id"]] = got["fact_id"]
    mapping_file.write_text(json.dumps(ids))
    out["stored"] = len(ids)
    out["stats"] = catch_up()
    out["results"] = search_all()
elif phase == "search":
    out["stats"] = catch_up(30)
    out["results"] = search_all()
elif phase in ("delete", "restore"):
    ids = json.loads(mapping_file.read_text())
    note = next(n for n in spec["notes"] if n["id"] == spec["delete_note"])
    if phase == "delete":
        out["removed"] = tool("remove", fact_id=ids[note["id"]])
        ids.pop(note["id"])
    else:
        got = tool("add", content=note["text"], keywords=note.get("keywords", []))
        ids[note["id"]] = got.get("fact_id")
        out["stats"] = catch_up(60)
    mapping_file.write_text(json.dumps(ids))
    out["results"] = search_all()
elif phase == "negative":
    # What the agent would be handed before a turn: the provider's own prefetch, relevance-gated.
    # Which seeded note(s) the injected text carries, by id (the notes are fictional test data), so
    # a failure names what was handed over -- a near neighbour by meaning, or a real answer.
    handed = {
        q["id"]: (provider.prefetch(q["text"], session_id="recall-test") or "").strip()
        for q in spec.get("absent", [])
    }
    out["prefetch"] = {qid: bool(text) for qid, text in handed.items()}
    out["prefetch_notes"] = {
        qid: [n["id"] for n in spec["notes"] if n["text"] in text] for qid, text in handed.items()
    }
elif phase == "cleanup":
    provider.shutdown()
    shutil.rmtree(scratch, ignore_errors=True)
    out["removed"] = not scratch.exists()
    print(json.dumps(out))
    sys.exit(0)
if match_kinds:
    out["match_kinds"] = dict(match_kinds)
provider.shutdown()
print(json.dumps(out, default=str))
