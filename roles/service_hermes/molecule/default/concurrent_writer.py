#!/usr/bin/env python
# SPDX-License-Identifier: GPL-3.0-or-later
"""A stand-in for the dashboard writing config.yaml while a converge merges it.

It does what the dashboard does and nothing more clever: load the file, change an UNOWNED key
(display.writes, a counter) and replace the file ATOMICALLY by rename (mkstemp beside + os.replace).
Every committed write is appended to --log, so after the run the last number the writer believes it
committed can be compared with what the file holds.

A lost update is detected from the inside: before each write it reads the counter back, and a value
LOWER than the last one it committed means someone replaced the file with an older copy of it.
On SIGTERM it finishes the write in flight, writes {"last": n, "anomalies": k} to --final and exits,
so "the writer has stopped" is a file that exists, not a sleep.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import tempfile
import time

import yaml

stop = False


def _term(*_):
    global stop
    stop = True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument("--final", required=True)
    ap.add_argument("--pidfile", required=True)
    ap.add_argument("--interval", type=float, default=0.002)
    a = ap.parse_args()
    signal.signal(signal.SIGTERM, _term)
    with open(a.pidfile, "w") as fh:
        fh.write(str(os.getpid()))
    last = 0
    anomalies = 0
    with open(a.log, "a", buffering=1) as log:
        while not stop:
            with open(a.config, encoding="utf-8") as fh:
                doc = yaml.safe_load(fh) or {}
            seen = (doc.get("display") or {}).get("writes", 0)
            if seen < last:
                anomalies += 1  # the file went BACKWARDS under us: our commit was overwritten
            n = max(seen, last) + 1
            doc.setdefault("display", {})["writes"] = n
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(a.config), prefix=".cfg-")
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                yaml.safe_dump(doc, out, sort_keys=False)
            os.chmod(tmp, 0o640)
            os.replace(tmp, a.config)
            last = n
            log.write(f"{n}\n")
            time.sleep(a.interval)
    with open(a.final, "w") as fh:
        json.dump({"last": last, "anomalies": anomalies}, fh)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
