#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""A Hermes agent's learning loop, host side: ledger, probe set, consolidation and recall.

Runs on the guest as root from systemd timers, and asks the AGENT'S OWN interpreter inside the
container to read its state: each `inner_*.py` beside this file is piped to
`docker exec -i -u <uid> -w <workdir> <container> <python> - <json-args>` and answers one JSON
object on stdout. Nothing here holds a credential: the inner scripts read the API key, the
gateway key and the Slack token from the container's own environment, exactly as the agent does.

    hermes_learning.py hourly       ledger + pending-age metrics, then the 24 h Slack ping
    hermes_learning.py probes       the fixed probe set through /v1/runs; pass rate as metrics
    hermes_learning.py consolidate  propose a MEMORY.md rewrite, refuse a > cap loss, STAGE it
    hermes_learning.py recall ...   the cold recall test, driven by the caller's recall play
    hermes_learning.py init-store   open the memory provider once so its database exists

Every metric file is written atomically (tmp + rename) into the node exporter's textfile
directory. A run that could not read what it reports writes `..._read_ok 0` rather than zeros, so
"could not tell" never presents as "nothing pending".
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import confined_io  # shipped beside this script (the confined-write module, vendored)

HERE = Path(__file__).resolve().parent

# Skill-ledger actions (tools/skill_ledger.py at v0.22.5) bucketed for the weekly ledger.
ACTION_BUCKETS = {
    "create": "created",
    "edit": "patched",
    "patch": "patched",
    "write_file": "patched",
    "remove_file": "patched",
    "archive": "archived",
    "delete": "archived",
    "purge": "archived",
    "restore": "restored",
    "rollback": "restored",
}
SUBSYSTEMS = ("memory", "skills")


class Failed(Exception):
    """A run that must exit non-zero with a one-line reason."""


# ---------------------------------------------------------------------------------------------
# pure helpers (unit-tested in tests/test_hermes_learning.py)
# ---------------------------------------------------------------------------------------------


def iso_week(ts: float) -> str:
    y, w, _ = dt.datetime.fromtimestamp(ts, dt.timezone.utc).isocalendar()
    return f"{y}-W{w:02d}"


def parse_ts(value) -> float | None:
    """A ledger `ts` (ISO 8601) or a `created_at` (epoch) as epoch seconds; None if unreadable."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value:
        try:
            return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def entry_hash(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def pending_ages(pending: list[dict], now: float) -> dict[str, dict]:
    """Per subsystem: count and the oldest pending item's age in seconds (0 when none)."""
    out = {s: {"count": 0, "oldest_age_s": 0.0} for s in SUBSYSTEMS}
    for rec in pending:
        s = rec.get("subsystem")
        if s not in out:
            continue
        out[s]["count"] += 1
        created = parse_ts(rec.get("created_at"))
        if created is not None:
            out[s]["oldest_age_s"] = max(out[s]["oldest_age_s"], max(0.0, now - created))
    return out


def resolve_vanished(
    previous: dict[str, dict],
    current_ids: set[str],
    memory_hashes: set[str],
    skill_events: list[dict],
) -> list[dict]:
    """Pending records seen last run and gone now were approved or rejected. Hermes keeps no record
    of which (`/memory reject` only deletes the file), so the outcome is INFERRED: a memory write is
    approved if every entry it adds is now present; a skill write is approved if the skill ledger
    shows a mutation of that skill at or after the record was staged. Anything else is `rejected`.
    The ledger labels these `inferred` so nobody reads them as Hermes's own count."""
    out = []
    for pid, rec in previous.items():
        if pid in current_ids:
            continue
        outcome = "rejected"
        if rec.get("subsystem") == "memory":
            adds = rec.get("add_hashes") or []
            if adds and all(h in memory_hashes for h in adds):
                outcome = "approved"
        elif rec.get("subsystem") == "skills":
            name, staged = rec.get("skill"), parse_ts(rec.get("created_at")) or 0
            for ev in skill_events:
                if ev.get("skill") == name and (parse_ts(ev.get("ts")) or 0) >= staged:
                    outcome = "approved"
                    break
        out.append({"id": pid, "subsystem": rec.get("subsystem"), "outcome": outcome})
    return out


def update_ledger(
    ledger: dict,
    *,
    now: float,
    skill_events: list[dict],
    resolved: list[dict],
    claim_gaps: int,
    review_runs: int,
) -> dict:
    """Fold one run into the weekly ledger. Counts accumulate per ISO week; nothing is re-counted
    because skill events are filtered by the caller to those newer than the last run."""
    weeks = ledger.setdefault("weeks", {})
    for ev in skill_events:
        ts = parse_ts(ev.get("ts")) or now
        wk = weeks.setdefault(iso_week(ts), {})
        bucket = ACTION_BUCKETS.get(str(ev.get("action")), "other")
        key = f"skills_{bucket}"
        wk[key] = wk.get(key, 0) + 1
        actor_key = f"by_{ev.get('actor') or 'unknown'}"
        wk[actor_key] = wk.get(actor_key, 0) + 1
    wk = weeks.setdefault(iso_week(now), {})
    for r in resolved:
        key = f"{r['subsystem']}_{r['outcome']}_inferred"
        wk[key] = wk.get(key, 0) + 1
    wk["claim_gaps"] = wk.get("claim_gaps", 0) + int(claim_gaps)
    wk["review_runs"] = wk.get("review_runs", 0) + int(review_runs)
    return ledger


def rejection_ratio(week: dict) -> float | None:
    rej = sum(v for k, v in week.items() if k.endswith("_rejected_inferred"))
    app = sum(v for k, v in week.items() if k.endswith("_approved_inferred"))
    return None if rej + app == 0 else rej / (rej + app)


def due_pings(
    pending: list[dict],
    pinged: dict[str, float],
    *,
    now: float,
    age_s: float,
    repeat_s: float,
) -> list[dict]:
    """Pending records older than age_s that were not pinged in the last repeat_s."""
    out = []
    for rec in pending:
        created = parse_ts(rec.get("created_at"))
        if created is None or now - created < age_s:
            continue
        last = pinged.get(rec.get("id", ""), 0.0)
        if now - last >= repeat_s:
            out.append(rec)
    return out


def ping_text(due: list[dict], now: float) -> str:
    lines = [f"Hermes: {len(due)} learning change(s) waiting over 24 h for approval:"]
    for rec in due[:20]:
        age_h = (now - (parse_ts(rec.get("created_at")) or now)) / 3600
        summary = str(rec.get("summary") or "").replace("\n", " ")[:100]
        lines.append(f"- {rec.get('subsystem')} {rec.get('id')} ({age_h:.0f} h): {summary}")
    lines.append("Review: /memory pending, /skills pending (approve <id> | reject <id>).")
    return "\n".join(lines)


def check_probe(probe: dict, output: str, tool_names: list[str]) -> tuple[bool, str]:
    """A probe passes when its output matches every check. Checks are deliberately simple and
    deterministic: `contains` (case-insensitive substring), `regex` (Python re, searched),
    `not_contains`, and `tool` (a tool the run must have called)."""
    import re

    text = output or ""
    for needle in probe.get("expect", {}).get("contains", []):
        if needle.lower() not in text.lower():
            return False, "missing-expected-text"
    for pattern in probe.get("expect", {}).get("regex", []):
        if not re.search(pattern, text):
            return False, "regex-not-matched"
    for needle in probe.get("expect", {}).get("not_contains", []):
        if needle.lower() in text.lower():
            return False, "forbidden-text"
    for tool in probe.get("expect", {}).get("tool", []):
        if tool not in tool_names:
            return False, "expected-tool-not-called"
    return True, "ok"


def count_entries(text: str, delimiter: str = "\n§\n") -> list[str]:
    """MEMORY.md entries (tools/memory_tool_store.py ENTRY_DELIMITER at v0.22.5)."""
    return [e.strip() for e in (text or "").split(delimiter) if e.strip()]


def loss_check(old: list[str], proposal: list[dict], cap: float) -> tuple[bool, float, list[int]]:
    """The loss cap. Each proposed entry names the prior entries it carries (`sources`, 0-based). A
    prior entry no proposed entry cites is DROPPED. Refuse when dropped / prior > cap. A proposal
    citing an index that does not exist is refused outright (it cannot be checked)."""
    if not old:
        return False, 0.0, []
    cited: set[int] = set()
    for item in proposal:
        for i in item.get("sources", []):
            if not isinstance(i, int) or i < 0 or i >= len(old):
                return False, 1.0, list(range(len(old)))
            cited.add(i)
    dropped = [i for i in range(len(old)) if i not in cited]
    frac = len(dropped) / len(old)
    return frac <= cap, frac, dropped


def consolidation_payload(old: list[str], proposal: list[dict], target: str = "memory") -> dict:
    """The staged write, in the shape tools/memory_tool.py `apply_memory_pending` replays: one atomic
    batch that removes every prior entry (each pinned by `matched_entry`, so approval applies to
    exactly the entry the approver saw) and adds the proposed ones."""
    ops = [{"action": "remove", "old_text": e, "matched_entry": e} for e in old]
    ops += [{"action": "add", "content": str(item["content"]).strip()} for item in proposal]
    return {"action": "batch", "target": target, "operations": ops}


def recall_at_k(results: dict[str, list[str]], expected: dict[str, str], k: int = 5) -> int:
    """The count of questions whose expected note is among the top-k results."""
    return sum(1 for q, note in expected.items() if note in (results.get(q) or [])[:k])


def prom_number(value: float) -> str:
    """A sample value with NO precision lost. `{value:g}` keeps six
    significant digits, so a Unix timestamp became 10,000-second steps -- 1793335500 printed as
    1.79334e+09, i.e. 4,500 s in the FUTURE -- and `time() - ts` goes negative, so a staleness
    rule on the run timestamp can never fire. repr() is
    Python's shortest round-trip form, so the value Prometheus parses is the value written."""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int) or (isinstance(value, float) and value.is_integer()):
        return str(int(value))
    return repr(float(value))


def prom(lines: list[tuple[str, dict, float]], helps: dict[str, str]) -> str:
    out, seen = [], set()
    for name, labels, value in lines:
        if name not in seen and name in helps:
            out.append(f"# HELP {name} {helps[name]}")
            out.append(f"# TYPE {name} gauge")
            seen.add(name)
        lab = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
        num = prom_number(value)
        out.append(f"{name}{{{lab}}} {num}" if lab else f"{name} {num}")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------------------------


def atomic_write(path: Path, text: str, mode: int = 0o644) -> None:
    """Root-owned destinations ONLY (state_dir, textfile_dir). Never call this for a path under
    host_home: that tree is the agent's, and this follows links. Use agent_write there."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------------------------
# writes into the AGENT'S tree (host_home/<learning_dir>/...)
#
# This script runs as root, and host_home is writable by the agent (uid 10000). A symlink the
# agent plants anywhere on a write path (learning/, learning/ledger/, or the file name itself)
# would otherwise turn a root write into an arbitrary file write on the guest. Two layers:
#   1. In docker mode under root, the write happens in a forked child that has DROPPED to the
#      agent's uid/gid, so it can only ever do what the agent could already do itself.
#   2. Regardless of who writes, the path is walked from an anchor fd with O_NOFOLLOW at every
#      component (dir_fd-relative, no path re-resolution), each directory must be owned by the
#      writer, and the file is created O_EXCL|O_NOFOLLOW and renamed over the name (a rename
#      replaces a symlink at the destination; it never writes through it).
# The root-owned state (state.json, ledger.json, probes history) stays under state_dir.
# ---------------------------------------------------------------------------------------------


class Refused(Failed):
    """A write into the agent's tree that would leave it, or could not be proven not to."""


def safe_rel(rel: str) -> str:
    if not rel or os.path.isabs(rel) or ".." in Path(rel).parts:
        raise Refused(f"path must be relative with no '..': {rel!r}")
    return rel


def write_confined(
    root: str | Path, rel: str, text: str, mode: int = 0o644, owner: int | None = None
) -> None:
    """Write root/rel atomically without following a link at ANY component below root.

    Missing directories are created (0755). Every directory below root must be owned by `owner`
    (default: the effective uid), so a directory the writer did not make is refused too.
    The walk, the O_EXCL temp file and the rename are confined_io's (shipped beside this script)."""
    safe_rel(rel)
    owner = os.geteuid() if owner is None else owner
    try:
        confined_io.write_file(root, rel, text, mode, create_dirs=True, owners=[owner], fsync=False)
    except confined_io.Refused as exc:
        raise Refused(f"{rel}: {exc}") from exc
    except OSError as exc:
        raise Refused(f"{rel}: {type(exc).__name__}: {exc.strerror}") from exc


def agent_ids(cfg: dict) -> tuple[int, int]:
    """exec_user as numeric (uid, gid); gid defaults to the uid, as the role's file tasks do."""
    u, _, g = str(cfg.get("exec_user", "")).partition(":")
    if not u.isdigit() or (g and not g.isdigit()):
        raise Refused(f"exec_user must be a numeric uid[:gid], got {cfg.get('exec_user')!r}")
    return int(u), int(g or u)


def run_as(uid: int, gid: int, fn) -> None:
    """Run fn() in a forked child that has dropped to uid/gid for good (confined_io.run_as). The
    child's failure is raised here as Refused with its reason."""
    try:
        confined_io.run_as(uid, gid, fn)
    except confined_io.Refused as exc:
        raise Refused(f"write {exc}") from exc


def ensure_dirs_confined(root: str | Path, rels: list[str], owner: int) -> dict:
    """Every rel exists under root as a real directory OWNED BY `owner`, mode 0755, reached
    without following a link. Created ones are made by the caller's own uid; an existing one owned
    by anyone else (a root-owned directory the agent renamed into place, a legacy one) is REFUSED,
    never re-owned. Returns {"created": [...], "repaired": [...]} (repaired = mode corrected)."""
    out: dict = {"created": [], "repaired": []}
    for rel in rels:
        safe_rel(rel)
        try:
            fd = confined_io.open_dir(
                root, rel, create=True, mode=0o755, owners=[owner], created=out["created"]
            )
        except confined_io.Refused as exc:
            raise Refused(
                f"{rel}: {exc}. A directory in the agent's tree is made by the agent and never"
                " re-owned by root: inspect it, and remove or re-own it deliberately before"
                " converging again."
            ) from exc
        try:
            if os.fstat(fd).st_mode & 0o7777 != 0o755:
                os.fchmod(fd, 0o755)
                out["repaired"].append(rel)
        finally:
            os.close(fd)
    return out


def cmd_ensure_dirs(cfg: dict, rels: list[str]) -> int:
    """The role's learning directories, made AS THE AGENT (root only forks and drops), so root
    does no chown or chmod in the agent's tree and the agent can do nothing it could not already."""
    root = cfg["host_home"]
    if os.geteuid() == 0 and cfg.get("exec_mode") != "local":
        uid, gid = agent_ids(cfg)

        def child() -> None:
            sys.stdout.write(json.dumps(ensure_dirs_confined(root, rels, uid)) + "\n")
            sys.stdout.flush()

        run_as(uid, gid, child)
    else:  # unprivileged (tests), or the molecule fixture home: confined, as ourselves
        print(json.dumps(ensure_dirs_confined(root, rels, os.geteuid())))
    return 0


def agent_write(cfg: dict, rel: str, text: str, mode: int = 0o644) -> None:
    """Write host_home/<learning_dir>/<rel> as the agent, never through a link."""
    full = f"{cfg['learning_dir']}/{safe_rel(rel)}"
    root = cfg["host_home"]
    if os.geteuid() == 0 and cfg.get("exec_mode") != "local":
        uid, gid = agent_ids(cfg)
        run_as(uid, gid, lambda: write_confined(root, full, text, mode, owner=uid))
    else:  # unprivileged (tests), or the molecule fixture home: confined, as ourselves
        write_confined(root, full, text, mode)


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def run_inner(cfg: dict, name: str, args: dict, timeout: int = 600) -> dict:
    """Run inner_<name>.py with the agent's interpreter and return its JSON answer."""
    script = (HERE / f"inner_{name}.py").read_text(encoding="utf-8")
    payload = json.dumps(args)
    if cfg.get("exec_mode") == "local":  # molecule and unit tests: no docker, a fixture HERMES_HOME
        cmd = [cfg.get("local_python", sys.executable), "-", payload]
        env = {**os.environ, "HERMES_HOME": cfg["host_home"]}
    else:
        cmd = [
            "docker",
            "exec",
            "-i",
            "-u",
            str(cfg["exec_user"]),
            "-w",
            cfg["container_workdir"],
            cfg["container"],
            cfg["container_python"],
            "-",
            payload,
        ]
        env = None
    try:
        proc = subprocess.run(
            cmd, input=script, capture_output=True, text=True, timeout=timeout, env=env
        )
    except subprocess.TimeoutExpired as exc:
        raise Failed(f"inner_{name} timed out after {timeout}s") from exc
    except OSError as exc:  # docker or the interpreter missing: cannot tell, never "nothing"
        raise Failed(f"inner_{name} could not start: {type(exc).__name__}") from exc
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")]
    if proc.returncode != 0 or not lines:
        tail = (proc.stderr or "").strip().splitlines()[-3:]
        raise Failed(f"inner_{name} rc={proc.returncode}: {' | '.join(tail) or 'no JSON answer'}")
    try:
        return json.loads(lines[-1])
    except ValueError as exc:
        raise Failed(f"inner_{name} answered unparseable JSON") from exc


HELPS = {
    "hermes_learning_read_ok": "1 if the last ledger run could read the agent's state",
    "hermes_learning_pending": "Learning writes staged for approval, per subsystem",
    "hermes_learning_pending_oldest_age_seconds": "Age of the oldest pending write (0 when none)",
    "hermes_learning_skills_total": "Skills in the agent's skills tree (bundled excluded)",
    "hermes_learning_week_events": "This ISO week's learning events, by kind",
    "hermes_learning_rejection_ratio": "This week's inferred rejected / resolved (absent when none resolved)",
    "hermes_learning_claim_gaps_window": "Memory-update claims with no memory tool call, in the window",
    "hermes_learning_ledger_last_success_timestamp_seconds": "When the ledger last ran cleanly",
    "hermes_learning_ping_configured": "1 if a Slack token and channel are visible to the ping",
    "hermes_learning_ping_last_sent_timestamp_seconds": "When the 24 h pending ping last posted",
}


def cmd_hourly(cfg: dict) -> int:
    state_dir = Path(cfg["state_dir"])
    tdir = Path(cfg["textfile_dir"])
    state = load_json(state_dir / "state.json", {})
    ledger = load_json(state_dir / "ledger.json", {})
    now = time.time()
    try:
        got = run_inner(
            cfg,
            "ledger",
            {
                "home": cfg["home"],
                "since": state.get("last_ledger_ts"),
                "claims_window_s": cfg.get("claims_window_s", 90000),
                "skip_names_file": cfg.get("skip_names_file", ""),
                "token_env": cfg["ping"]["token_env"],
                "channel_env": cfg["ping"]["channel_env"],
            },
        )
    except Failed as exc:
        atomic_write(
            tdir / "hermes_learning.prom",
            prom([("hermes_learning_read_ok", {}, 0)], HELPS),
        )
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1

    pending = got.get("pending", [])
    events = got.get("skill_events", [])
    resolved = resolve_vanished(
        state.get("pending", {}),
        {p["id"] for p in pending},
        set(got.get("memory_hashes", [])),
        events,
    )
    seen_gaps = set(state.get("claim_gap_sessions", []))
    new_gaps = [g for g in got.get("claim_gap_sessions", []) if g not in seen_gaps]
    ledger = update_ledger(
        ledger,
        now=now,
        skill_events=events,
        resolved=resolved,
        claim_gaps=len(new_gaps),
        review_runs=got.get("review_runs_new", 0),
    )
    ages = pending_ages(pending, now)
    week = ledger["weeks"].get(iso_week(now), {})

    # The ping: pending items older than 24 h, each at most once per repeat window.
    pinged = state.get("pinged", {})
    due = due_pings(
        pending,
        pinged,
        now=now,
        age_s=cfg["ping"]["age_s"],
        repeat_s=cfg["ping"]["repeat_s"],
    )
    ping_cfg, ping_sent = (
        got.get("slack_configured", False),
        state.get("ping_last_sent", 0),
    )
    ping_error = ""
    if due and ping_cfg:
        try:
            ans = run_inner(
                cfg,
                "ping",
                {
                    "text": ping_text(due, now),
                    "token_env": cfg["ping"]["token_env"],
                    "channel_env": cfg["ping"]["channel_env"],
                },
                timeout=60,
            )
            if ans.get("ok"):
                ping_sent = now
                for rec in due:
                    pinged[rec["id"]] = now
            else:
                ping_error = str(ans.get("error", "slack refused"))
        except Failed as exc:
            ping_error = str(exc)

    lines = [
        ("hermes_learning_read_ok", {}, 1),
        ("hermes_learning_skills_total", {}, got.get("skills_total", 0)),
        ("hermes_learning_claim_gaps_window", {}, got.get("claim_gaps_window", 0)),
        ("hermes_learning_ledger_last_success_timestamp_seconds", {}, now),
        ("hermes_learning_ping_configured", {}, 1 if ping_cfg else 0),
        ("hermes_learning_ping_last_sent_timestamp_seconds", {}, ping_sent),
    ]
    for s in SUBSYSTEMS:
        lines.append(("hermes_learning_pending", {"subsystem": s}, ages[s]["count"]))
        lines.append(
            (
                "hermes_learning_pending_oldest_age_seconds",
                {"subsystem": s},
                ages[s]["oldest_age_s"],
            )
        )
    for k, v in sorted(week.items()):
        lines.append(("hermes_learning_week_events", {"kind": k}, v))
    ratio = rejection_ratio(week)
    if ratio is not None:
        lines.append(("hermes_learning_rejection_ratio", {}, ratio))
    atomic_write(tdir / "hermes_learning.prom", prom(lines, HELPS))

    state.update(
        {
            "last_ledger_ts": got.get("newest_event_ts") or state.get("last_ledger_ts"),
            "pending": {
                p["id"]: {k: p.get(k) for k in ("subsystem", "created_at", "skill", "add_hashes")}
                for p in pending
            },
            "pinged": {pid: t for pid, t in pinged.items() if pid in {p["id"] for p in pending}},
            "ping_last_sent": ping_sent,
            # a gap session is counted once; it can reappear only inside the 25 h window
            "claim_gap_sessions": sorted(seen_gaps.union(new_gaps))[-500:],
        }
    )
    atomic_write(state_dir / "state.json", json.dumps(state, indent=1), 0o600)
    atomic_write(state_dir / "ledger.json", json.dumps(ledger, indent=1, sort_keys=True), 0o600)
    # The mirror copy (the state repository, via config_agent_state): counts and names only, no summaries,
    # so a memory write's free text never reaches git through the ledger.
    agent_write(
        cfg,
        f"ledger/{iso_week(now)}.json",
        json.dumps(
            {
                "week": iso_week(now),
                "counts": week,
                "pending": {s: ages[s]["count"] for s in SUBSYSTEMS},
            },
            indent=1,
            sort_keys=True,
        ),
    )
    print(
        json.dumps(
            {
                "ok": True,
                "pending": {s: ages[s]["count"] for s in SUBSYSTEMS},
                "resolved": len(resolved),
                "events": len(events),
                "pinged": len(due) if ping_sent == now else 0,
                "ping_configured": ping_cfg,
                "ping_error": ping_error,
            }
        )
    )
    return 0


PROBE_HELPS = {
    "hermes_probe_read_ok": "1 if the last probe run reached the agent at all",
    "hermes_probe_total": "Probes in the fixed set at the last run",
    "hermes_probe_passed": "Probes that passed at the last run",
    "hermes_probe_pass_ratio": "passed / total at the last run",
    "hermes_probe_result": "1 pass / 0 fail, per probe, at the last run",
    "hermes_probe_tokens": "Tokens the last run used, summed over probes (0 when unreported)",
    "hermes_probe_tool_calls": "Tool calls the last run made, summed over probes",
    "hermes_probe_run_last_success_timestamp_seconds": "When a probe run last completed",
}


def cmd_probes(cfg: dict) -> int:
    state_dir, tdir = Path(cfg["state_dir"]), Path(cfg["textfile_dir"])
    probes = load_json(Path(cfg["probes_file"]), None)
    if not isinstance(probes, list) or not probes:
        raise Failed(f"probe set {cfg['probes_file']} is missing or empty")
    now = time.time()
    try:
        got = run_inner(
            cfg,
            "probes",
            {
                "api_url": cfg["api_url"],
                "key_env": cfg["api_key_env"],
                "model": cfg["probe_model"],
                "timeout_s": cfg["probe_timeout_s"],
                "probes": probes,
            },
            timeout=cfg["probe_timeout_s"] * len(probes) + 60,
        )
    except Failed as exc:
        atomic_write(
            tdir / "hermes_probe.prom",
            prom([("hermes_probe_read_ok", {}, 0)], PROBE_HELPS),
        )
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    results = []
    by_id = {p["id"]: p for p in probes}
    for r in got.get("results", []):
        probe = by_id.get(r.get("id"))
        if probe is None:
            continue
        if r.get("error"):
            ok, why = False, f"run-{r['error']}"
        else:
            ok, why = check_probe(probe, r.get("output", ""), r.get("tools", []))
        results.append(
            {
                "id": probe["id"],
                "pass": ok,
                "reason": why,
                "tokens": r.get("tokens", 0),
                "tool_calls": len(r.get("tools", [])),
                "seconds": r.get("seconds", 0),
            }
        )
    passed = sum(1 for r in results if r["pass"])
    total = len(probes)
    ratio = passed / total if total else 0.0
    lines = [
        ("hermes_probe_read_ok", {}, 1),
        ("hermes_probe_total", {}, total),
        ("hermes_probe_passed", {}, passed),
        ("hermes_probe_pass_ratio", {}, ratio),
        ("hermes_probe_tokens", {}, sum(r["tokens"] for r in results)),
        ("hermes_probe_tool_calls", {}, sum(r["tool_calls"] for r in results)),
        ("hermes_probe_run_last_success_timestamp_seconds", {}, now),
    ]
    lines += [("hermes_probe_result", {"probe": r["id"]}, 1 if r["pass"] else 0) for r in results]
    atomic_write(tdir / "hermes_probe.prom", prom(lines, PROBE_HELPS))

    history = state_dir / "probes-history.jsonl"
    prev = None
    if history.exists():
        tail = history.read_text(encoding="utf-8").strip().splitlines()[-1:]
        prev = json.loads(tail[0]) if tail else None
    run = {
        "ts": now,
        "passed": passed,
        "total": total,
        "ratio": ratio,
        "failed": [r["id"] for r in results if not r["pass"]],
        "reasons": {r["id"]: r["reason"] for r in results if not r["pass"]},
    }
    state_dir.mkdir(parents=True, exist_ok=True)
    with history.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(run) + "\n")
    os.chmod(history, 0o600)
    agent_write(
        cfg,
        "probes/latest.json",
        json.dumps(run, indent=1, sort_keys=True),
    )
    # A regression is surfaced where the approvals are: the same Slack
    # channel as the pending ping, naming the failing probes and what is waiting for a tap.
    # A REGRESSION is a probe that passed last run and fails now -- not "the ratio fell". The ratio
    # is noise-sensitive in the wrong direction: one probe breaking while a flaky one recovers
    # leaves the ratio unchanged, and the break would go unreported. A probe recovering never
    # hides one that broke.
    newly_failed = (
        sorted(set(run["failed"]) - set(prev.get("failed", []))) if prev is not None else []
    )
    regressed = bool(newly_failed)
    note = ""
    if regressed:
        try:
            led = run_inner(
                cfg,
                "ledger",
                {
                    "home": cfg["home"],
                    "since": None,
                    "claims_window_s": 0,
                    "skip_names_file": cfg.get("skip_names_file", ""),
                    "token_env": cfg["ping"]["token_env"],
                    "channel_env": cfg["ping"]["channel_env"],
                },
            )
            if led.get("slack_configured"):
                pend = led.get("pending", [])
                text = (
                    f"Hermes probe set REGRESSED: {prev.get('passed')}/{prev.get('total')} -> "
                    f"{passed}/{total}. Newly failing: {', '.join(newly_failed[:15])}. "
                    f"Failing: {', '.join(run['failed'][:15])}."
                )
                if pend:
                    text += "\n" + ping_text(pend, now).split("\n", 1)[1]
                run_inner(
                    cfg,
                    "ping",
                    {
                        "text": text,
                        "token_env": cfg["ping"]["token_env"],
                        "channel_env": cfg["ping"]["channel_env"],
                    },
                    timeout=60,
                )
                note = "regression posted"
        except Failed as exc:
            note = f"regression not posted: {exc}"
    print(
        json.dumps(
            {
                "ok": True,
                "passed": passed,
                "total": total,
                "failed": run["failed"],
                "regressed": regressed,
                "newly_failed": newly_failed,
                "note": note,
            }
        )
    )
    return 0


def cmd_consolidate(cfg: dict, proposal_file: str | None = None) -> int:
    """The inner script reads MEMORY.md, asks the aux model for a rewrite (or takes
    --proposal-file in tests), applies the loss cap HERE, then stages the batch through the
    approval queue. Nothing is ever written to MEMORY.md by this job."""
    c = cfg["consolidate"]
    got = run_inner(
        cfg,
        "consolidate",
        {
            "home": cfg["home"],
            "mode": "propose",
            "gateway_url": c["gateway_url"],
            "key_env": c["key_env"],
            "model": c["model"],
            "min_entries": c["min_entries"],
            "proposal": load_json(Path(proposal_file), None) if proposal_file else None,
        },
        timeout=300,
    )
    if got.get("status") != "proposed":
        print(
            json.dumps(
                {
                    "ok": True,
                    "staged": False,
                    "status": got.get("status"),
                    "detail": got.get("detail", ""),
                }
            )
        )
        return 0
    old, proposal = got["entries"], got["proposal"]
    ok, frac, dropped = loss_check(old, proposal, float(c["max_loss"]))
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    summary = {
        "ts": stamp,
        "prior": len(old),
        "proposed": len(proposal),
        "dropped": len(dropped),
        "loss": round(frac, 3),
        "cap": c["max_loss"],
        "refused": not ok,
    }
    if not ok:
        agent_write(cfg, f"consolidation/{stamp}.refused.json", json.dumps(summary, indent=1))
        print(json.dumps({"ok": False, "staged": False, **summary}))
        return 2
    staged = run_inner(
        cfg,
        "consolidate",
        {
            "home": cfg["home"],
            "mode": "stage",
            "payload": consolidation_payload(old, proposal),
            "summary": f"consolidation: {len(old)} -> {len(proposal)} entries, "
            f"{len(dropped)} dropped ({frac:.0%}, cap {c['max_loss']:.0%})",
        },
    )
    summary["pending_id"] = staged.get("id")
    # The human-review file: the proposed MEMORY.md, in the mirror, beside its numbers.
    agent_write(
        cfg,
        f"consolidation/{stamp}.proposed.md",
        "\n§\n".join(p["content"] for p in proposal) + "\n",
    )
    agent_write(cfg, f"consolidation/{stamp}.json", json.dumps(summary, indent=1))
    print(json.dumps({"ok": True, "staged": True, **summary}))
    return 0


def cmd_recall(cfg: dict, phase: str, notes_file: str) -> int:
    spec = load_json(Path(notes_file), None)
    if not isinstance(spec, dict) or not spec.get("notes") or not spec.get("questions"):
        raise Failed(f"{notes_file} must carry notes and questions")
    got = run_inner(
        cfg,
        "recall",
        {"phase": phase, "scratch_home": cfg["recall_scratch_home"], "spec": spec},
        timeout=900,
    )
    expected = {q["id"]: q["expect"] for q in spec["questions"]}
    score = recall_at_k(got.get("results", {}), expected) if "results" in got else None
    out = {**got, "recall_at_5": score, "questions": len(expected)}
    stats = got.get("stats") if isinstance(got.get("stats"), dict) else {}
    out.pop("stats", None)  # provider internals; the play needs only the scores and note ids
    # Whether search by meaning was live for this phase -- the one provider statistic that decides
    # whether a paraphrase can be recalled at all: "off", or {status ("ready" when the endpoint
    # answered), facts_with_vector, waiting, never_sent}. Holographic calls it search_by_meaning
    # (there is no "meaning" key; reading one reports null for every run).
    sbm = stats.get("search_by_meaning")
    out["meaning"] = (
        {k: sbm.get(k) for k in ("status", "model", "facts_with_vector", "waiting", "never_sent")}
        if isinstance(sbm, dict)
        else sbm
    )
    # The per-result match labels (inner_recall.py): with no "meaning" count, a miss is word
    # matching failing, not the provider's meaning search -- the difference between a recall
    # problem and a broken embeddings path.
    out["match_kinds"] = got.get("match_kinds")
    out["misses"] = [
        q
        for q, n in expected.items()
        if score is not None and n not in got["results"].get(q, [])[:5]
    ]
    if score is not None:
        atomic_write(
            Path(cfg["textfile_dir"]) / f"hermes_recall_{phase.replace('-', '_')}.prom",
            prom(
                [
                    ("hermes_recall_at5", {"phase": phase}, score),
                    ("hermes_recall_questions", {"phase": phase}, len(expected)),
                    (
                        "hermes_recall_last_run_timestamp_seconds",
                        {"phase": phase},
                        time.time(),
                    ),
                ],
                {
                    "hermes_recall_at5": "Questions whose expected note was in the top 5",
                    "hermes_recall_questions": "Questions asked",
                    "hermes_recall_last_run_timestamp_seconds": "When this phase last ran",
                },
            ),
        )
    print(json.dumps(out))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default="/etc/hermes-learning/config.json")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("hourly")
    sub.add_parser("probes")
    c = sub.add_parser("consolidate")
    c.add_argument("--proposal-file")
    r = sub.add_parser("recall")
    r.add_argument("phase", choices=["seed", "search", "delete", "restore", "negative", "cleanup"])
    r.add_argument("--notes", required=True)
    sub.add_parser("init-store")
    e = sub.add_parser("ensure-dirs")
    e.add_argument("rels", nargs="+")
    a = ap.parse_args(argv)
    cfg = load_json(Path(a.config), None)
    if not isinstance(cfg, dict):
        print(json.dumps({"ok": False, "error": f"config {a.config} unreadable"}))
        return 1
    try:
        if a.cmd == "hourly":
            return cmd_hourly(cfg)
        if a.cmd == "probes":
            return cmd_probes(cfg)
        if a.cmd == "consolidate":
            return cmd_consolidate(cfg, a.proposal_file)
        if a.cmd == "recall":
            return cmd_recall(cfg, a.phase, a.notes)
        if a.cmd == "ensure-dirs":
            return cmd_ensure_dirs(cfg, a.rels)
        if a.cmd == "init-store":
            print(
                json.dumps(
                    run_inner(cfg, "recall", {"phase": "init-live", "spec": {}}, timeout=120)
                )
            )
            return 0
    except Failed as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
