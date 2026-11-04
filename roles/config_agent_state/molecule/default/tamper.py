#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Damage a COPY of a backup in one named way. Usage: tamper.py <mode> <archive>

Modes:
  flip-archive        flip a byte of the ciphertext; the manifest still states the old hash
  flip-and-rehash     flip a byte AND rewrite the manifest's ciphertext hash to match (an attacker
                      who can edit the manifest): only age's own authentication can catch this
  lie-member          change one member's sha256 in the manifest
  lie-rowcount        change a table's row count in the manifest
"""

import hashlib
import json
import sys
from pathlib import Path

mode, archive = sys.argv[1], Path(sys.argv[2])
mpath = Path(str(archive) + ".manifest.json")
manifest = json.loads(mpath.read_text())

if mode in ("flip-archive", "flip-and-rehash"):
    raw = bytearray(archive.read_bytes())
    raw[len(raw) // 2] ^= 0xFF
    archive.write_bytes(bytes(raw))
    if mode == "flip-and-rehash":
        manifest["archive"]["sha256"] = hashlib.sha256(bytes(raw)).hexdigest()
        mpath.write_text(json.dumps(manifest))
elif mode == "lie-member":
    manifest["files"][0]["sha256"] = "0" * 64
    mpath.write_text(json.dumps(manifest))
elif mode == "lie-rowcount":
    first = next(iter(manifest["sqlite"].values()))
    t = next(iter(first["tables"]))
    first["tables"][t] += 1
    mpath.write_text(json.dumps(manifest))
else:
    raise SystemExit(f"unknown mode {mode}")
