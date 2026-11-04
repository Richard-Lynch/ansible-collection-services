#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Print {"integrity": ..., "sessions": N} for a sqlite file; N is 0 when the table is absent."""

import json
import sqlite3
import sys

c = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
try:
    n = c.execute("SELECT count(*) FROM sessions").fetchone()[0]
except sqlite3.OperationalError:
    n = 0
integ = [r[0] for r in c.execute("PRAGMA integrity_check")]
print(json.dumps({"integrity": integ[0] if integ else "none", "sessions": n}))
