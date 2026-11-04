# SPDX-License-Identifier: GPL-3.0-or-later
# Runs INSIDE the Hermes container (see hermes_learning.py). Sends each probe through the agent's own
# /v1/runs API on its loopback, waits for a terminal status, and answers the raw output, the tools
# the run called and its token usage. It JUDGES NOTHING: pass/fail is decided on the host against
# the probe set, so a probe's expected answer never enters the container.
#
# v0.22.5 facts: POST /v1/runs {input, model, session_id?} -> 202 {run_id, status}; GET
# /v1/runs/<id> -> {status, output, error, usage}; terminal statuses completed, failed, cancelled,
# interrupted (gateway/platforms/api_server_run_idempotency.py); GET /v1/runs/<id>/events carries
# tool.started events with a `tool` field.
import json
import os
import sys
import time
import urllib.error
import urllib.request

args = json.loads(sys.argv[1])
base = args["api_url"].rstrip("/")
key = os.environ.get(args.get("key_env") or "API_SERVER_KEY", "")
TERMINAL = {"completed", "failed", "cancelled", "interrupted"}


def call(method, path, body=None, timeout=30):
    req = urllib.request.Request(
        base + path, method=method, data=json.dumps(body).encode() if body is not None else None
    )
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def tools_of(run_id):
    names = []
    try:
        raw = call("GET", f"/v1/runs/{run_id}/events", timeout=10)
    except (urllib.error.URLError, OSError, TimeoutError):
        return names
    for line in raw.splitlines():
        line = line[5:].strip() if line.startswith("data:") else line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("event") == "tool.started" and ev.get("tool"):
            names.append(ev["tool"])
    return names


results = []
if not key:
    print(json.dumps({"results": [], "error": "no-api-key-in-container-env"}))
    sys.exit(0)
for probe in args["probes"]:
    started = time.time()
    rec = {"id": probe["id"]}
    try:
        body = {"input": probe["prompt"], "model": probe.get("model") or args["model"]}
        run = json.loads(call("POST", "/v1/runs", body))
        run_id = run.get("run_id")
        if not run_id:
            raise ValueError("no run_id")
        deadline = started + float(probe.get("timeout_s") or args.get("timeout_s") or 180)
        status = {}
        while time.time() < deadline:
            status = json.loads(call("GET", f"/v1/runs/{run_id}"))
            if status.get("status") in TERMINAL:
                break
            time.sleep(2)
        if status.get("status") != "completed":
            rec["error"] = str(status.get("status") or "timeout")
        out = status.get("output")
        if isinstance(out, list):  # Responses-style content parts
            out = " ".join(str(p.get("text", "")) if isinstance(p, dict) else str(p) for p in out)
        rec["output"] = str(out or "")
        usage = status.get("usage") or {}
        rec["tokens"] = int(
            usage.get("total_tokens")
            or (usage.get("input_tokens", 0) + usage.get("output_tokens", 0))
            or 0
        )
        rec["tools"] = tools_of(run_id)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        rec["error"] = type(exc).__name__
    rec["seconds"] = round(time.time() - started, 1)
    results.append(rec)
print(json.dumps({"results": results}))
