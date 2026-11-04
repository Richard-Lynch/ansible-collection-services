# SPDX-License-Identifier: GPL-3.0-or-later
# Runs INSIDE the Hermes container. Posts one message to the configured Slack channel with the bot
# token the agent itself uses (SLACK_BOT_TOKEN / SLACK_HOME_CHANNEL by default; names passed in). The
# token never leaves the container; the answer carries only ok and Slack's error code.
import json
import os
import sys
import urllib.request

args = json.loads(sys.argv[1])
token = os.environ.get(args.get("token_env") or "SLACK_BOT_TOKEN", "")
channel = os.environ.get(args.get("channel_env") or "SLACK_HOME_CHANNEL", "")
if not token or not channel:
    print(json.dumps({"ok": False, "error": "slack-not-configured"}))
    sys.exit(0)
req = urllib.request.Request(
    "https://slack.com/api/chat.postMessage",
    method="POST",
    data=json.dumps({"channel": channel, "text": args["text"]}).encode(),
)
req.add_header("Authorization", f"Bearer {token}")
req.add_header("Content-Type", "application/json; charset=utf-8")
try:
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = json.loads(resp.read().decode())
    print(json.dumps({"ok": bool(body.get("ok")), "error": body.get("error", "")}))
except (OSError, ValueError) as exc:
    print(json.dumps({"ok": False, "error": type(exc).__name__}))
