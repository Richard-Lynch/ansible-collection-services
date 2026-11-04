#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""worker-metrics-pull: copy worker-sessiond's stuck-card metrics into the textfile collector.

Runs as ROOT from a systemd timer. sessiond runs as the agents' own user, and a collector
directory that user can write is one an agent can forge ANY host metric into (``node_*`` included),
so the unprivileged process serves the numbers and this one, which only root can run, files them.

Failure semantics are the point. If sessiond is down, refuses the token, or returns something that
is not the exposition it promised, this exits non-zero and LEAVES THE PREVIOUS FILE ALONE: the file's
own ``hermes_worker_metrics_timestamp_seconds`` then ages, and a staleness alert on it fires.
Deleting the file instead would make every series vanish, which an alert on a value cannot see.
Writing zeros on failure would be the worst option: it reads as a healthy, idle worker.

Stdlib only. Everything that decides is a pure function with a test.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

REQUIRED_SERIES = (
    "hermes_worker_metrics_timestamp_seconds",
    "hermes_worker_stuck_cards",
    "hermes_worker_sessions_disagree",
)
_SAMPLE = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*(\{[^}]*\})? -?[0-9]+(\.[0-9]+)?$")
# The ONLY family this puller files. sessiond runs as the agents' own uid, so any agent can kill it
# and bind its port before systemd restarts it; the puller then hands that agent the bearer token and
# files whatever it serves AS ROOT. Without this check a forged `node_*`/`up` line would land in the
# collector with root's authority, which is the exact forgery the root timer exists to prevent.
_NAME_PREFIX = "hermes_worker_"


def validate_exposition(text: str) -> None:
    """Raise ValueError unless ``text`` is textfile-collector exposition that carries the series
    the alerts read and NOTHING outside the ``hermes_worker_`` family. A 200 with an HTML error
    page, a proxy banner, an empty body or a forged host metric must not replace a good file."""
    if not text.strip():
        raise ValueError("empty body")
    names: set[str] = set()
    for n, line in enumerate(text.splitlines(), 1):
        if not line or line.startswith("#"):
            continue
        if not _SAMPLE.match(line):
            raise ValueError(f"line {n} is not a Prometheus sample: {line[:60]!r}")
        name = re.split(r"[ {]", line, 1)[0]
        if not name.startswith(_NAME_PREFIX):
            raise ValueError(
                f"line {n} is not a {_NAME_PREFIX}* series: {name!r} (a forged host metric?)"
            )
        names.add(name)
    missing = [r for r in REQUIRED_SERIES if r not in names]
    if missing:
        raise ValueError(f"missing required series: {missing}")
    if not text.endswith("\n"):
        raise ValueError("no trailing newline (a truncated body)")


def write_atomic(path: Path, text: str) -> None:
    """Temp file in the SAME directory, then rename: the collector reads on its own schedule and a
    half-written file parses as fewer series. 0644: it holds counts, and the collector's user must
    read it."""
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def fetch(url: str, token: str, timeout: float = 10.0) -> str:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(
        req, timeout=timeout
    ) as r:  # noqa: S310 - fixed http URL from config
        if r.status != 200:
            raise ValueError(f"sessiond answered {r.status}")
        return r.read().decode()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    # The same file the sessiond unit reads (EnvironmentFile), so there is one home for the token.
    token = os.environ.get("WORKER_SESSIOND_TOKEN", "")
    if len(token) < 16:
        sys.stderr.write("WORKER_SESSIOND_TOKEN missing or short; leaving the previous file\n")
        return 2
    try:
        text = fetch(args.url, token)
        validate_exposition(text)
    except (urllib.error.URLError, OSError, ValueError) as e:
        # str(e) of a URLError can carry the URL; the token is only ever in a header, never here.
        sys.stderr.write(f"not updating {args.out}: {type(e).__name__}: {e}\n")
        return 1
    write_atomic(Path(args.out), text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
