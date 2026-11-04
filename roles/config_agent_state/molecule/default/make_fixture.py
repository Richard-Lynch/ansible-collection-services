#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build the state-root fixtures the config_agent_state scenario runs against.

Real files, on purpose: a SQLite database whose newest rows live ONLY in the -wal (so a plain
file copy provably loses them), a tree with directories that must be excluded, and a second
database that is genuinely corrupt. Usage: make_fixture.py <workdir>
"""

import os
import sqlite3
import sys
from pathlib import Path


def write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def wal_db(path: Path, rows: int) -> None:
    """A WAL database that exits WITHOUT checkpointing: rows live only in state.db-wal."""
    c = sqlite3.connect(path)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA wal_autocheckpoint=0")
    c.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, body TEXT)")
    c.execute("CREATE TABLE boards (id INTEGER PRIMARY KEY, name TEXT)")
    c.executemany("INSERT INTO sessions(body) VALUES (?)", [(f"session-{i}",) for i in range(rows)])
    c.executemany("INSERT INTO boards(name) VALUES (?)", [("alpha",), ("beta",), ("gamma",)])
    c.commit()
    os._exit(0)  # no close(), so no checkpoint: the pages stay in the WAL


def main(work: Path) -> None:
    src = work / "src"
    write(src / "SOUL.md", "# persona\nA plain, dry assistant.\n")
    write(src / "USER.md", "# user\nPrefers short answers.\n")
    write(src / "memories" / "MEMORY.md", "remember: the printer is on the second shelf\n")
    write(src / "skills" / "triage" / "SKILL.md", "# triage\nSort the inbox.\n")
    write(src / "cron" / "jobs.json", '{"jobs": []}\n')
    write(src / "webhook_subscriptions.json", "{}\n")
    write(
        src / "config.yaml",
        "model:\n  default: some-model\napi_key: placeholder-value-1234567890\n"
        "dashboard:\n  theme: dark\n",
    )
    write(src / "oauth" / "token.json", '{"kind": "oauth-state"}\n')
    # Directory entries: a 0700 directory and an EMPTY one must survive a restore (modes and empty
    # directories are not carried by file-only archives).
    write(src / "private" / "note.txt", "owner-only\n")
    os.chmod(src / "private", 0o700)
    (src / "emptydir").mkdir(parents=True, exist_ok=True)
    # Must be EXCLUDED from the runtime tar:
    write(src / "cache" / "blob.bin", "cache-data-must-not-be-archived\n")
    write(src / "logs" / "agent.log", "log-line-must-not-be-archived\n")
    write(src / "sandboxes" / "s1" / "x", "sandbox-must-not-be-archived\n")
    write(src / "browser-profiles" / "p" / "c", "profile-must-not-be-archived\n")

    pid = os.fork()
    if pid == 0:
        wal_db(src / "state.db", 400)
    os.waitpid(pid, 0)

    # A root holding a database that is NOT declared: the backup must refuse it.
    und = work / "src_undeclared"
    write(und / "SOUL.md", "x\n")
    c = sqlite3.connect(und / "state.db")  # the DECLARED one, so only other.db is the fault
    c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    c.commit()
    c.close()
    c = sqlite3.connect(und / "other.db")
    c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    c.commit()
    c.close()

    # A genuinely corrupt database: valid header, stomped interior pages.
    bad = work / "src_corrupt"
    write(bad / "SOUL.md", "x\n")
    c = sqlite3.connect(bad / "state.db")
    c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, body TEXT)")
    c.executemany("INSERT INTO t(body) VALUES (?)", [("row-" + "z" * 200,) for _ in range(500)])
    c.commit()
    c.close()
    raw = bytearray((bad / "state.db").read_bytes())
    for i in range(8192, len(raw) - 4096, 1):
        raw[i] = 0xFF
    (bad / "state.db").write_bytes(bytes(raw))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
