# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

DEFAULT_AUTHOR = "Collection maintainers"
from ansible_devkit.argspec.models import ArgumentSpec, ArgumentSpecs, OptionSpec

# NEVER put literal Jinja delimiters in a description (Ansible templates the argspec).

argument_specs = ArgumentSpecs(
    argument_specs={
        "main": ArgumentSpec(
            short_description="Reconcile a declared list of Slack public channels, as a bot",
            description=[
                "Reads the workspace with the bot token (auth.test for the bot and its granted",
                "scopes, conversations.list, conversations.members) and plans, per declared",
                "channel: create it, or rename it from rename_from; join the bot; invite the",
                "declared members; set purpose and topic. Report-only unless slack_channels_apply",
                "is true; then it acts and re-reads every channel with conversations.info, failing",
                "unless nothing remains to do. It never archives, unarchives or removes anyone.",
                "Which channels, which members and which token are the caller's configuration",
                "and have no default.",
            ],
            version_added="1.0",
            author=DEFAULT_AUTHOR,
            options={
                "slack_channels_token": OptionSpec(
                    type="str",
                    required=True,
                    no_log=True,
                    min_length=1,
                    description=[
                        "Bot token (xoxb-) the reconcile acts as. Resolve it from 1Password in the",
                        "calling play; never pass it on the command line.",
                    ],
                ),
                "slack_channels": OptionSpec(
                    type="list",
                    elements="dict",
                    required=True,
                    min_length=1,
                    description=[
                        "Declared public channels: name (no leading hash), optional purpose and",
                        "topic (left alone when omitted), members (user ids to invite; the bot",
                        "always joins), and optional rename_from (the channel's current name; a",
                        "rename keeps its id and history), and optional id (the channel id other",
                        "config is written against: refused unless the name resolves to it).",
                        "Undeclared members are reported only.",
                    ],
                ),
                "slack_channels_apply": OptionSpec(
                    type="bool",
                    default=False,
                    description=[
                        "Act on the plan. False reports it and changes nothing in Slack.",
                    ],
                ),
                "slack_channels_admin_token": OptionSpec(
                    type="str",
                    default="",
                    no_log=True,
                    description=[
                        "A workspace admin's USER token (xoxp-), used ONLY for the steps Slack",
                        "does not authorize a bot to take: renaming a channel a human made. Empty",
                        "means none; a planned rename is then reported as needing it.",
                    ],
                ),
                "slack_channels_guide_marker_prefix": OptionSpec(
                    type="str",
                    default="guide:",
                    min_length=1,
                    description=[
                        "Prefix of the trailing marker that identifies the bot's guide message in",
                        "each channel (the marker is this followed by the channel name). Set it",
                        "once: changing it on a workspace with guides makes them look absent, so",
                        "the next apply posts new ones beside the old.",
                    ],
                ),
                "slack_channels_api_url": OptionSpec(
                    type="str",
                    default="https://slack.com/api",
                    min_length=1,
                    description=[
                        "Slack Web API base URL. Only a test points it elsewhere.",
                    ],
                ),
            },
        ),
    }
)
