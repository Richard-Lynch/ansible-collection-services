# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

DEFAULT_AUTHOR = "Collection maintainers"
from ansible_devkit.argspec.models import ArgumentSpec, ArgumentSpecs, OptionSpec

# NEVER put literal Jinja delimiters in a description (Ansible templates the argspec).

# Who receives the message, which bot sends it and what it says are the CALLER's facts: required,
# no default. The only defaults are ones that are right for any caller.
argument_specs = ArgumentSpecs(
    argument_specs={
        "main": ArgumentSpec(
            short_description="Send a Slack message, optionally with a file, as a bot",
            description=[
                "Posts text to a Slack user (opening the DM) or a channel with a bot token, or",
                "uploads one file with the text as its comment via the external-upload flow",
                "(files.getUploadURLExternal, POST of the bytes, files.completeUploadExternal).",
                "Every Slack response is asserted ok, and an upload is read back with files.info",
                "until the file is shared in the target conversation. Runs on the controller.",
            ],
            version_added="1.0",
            author=DEFAULT_AUTHOR,
            options={
                "slack_message_target": OptionSpec(
                    type="str",
                    required=True,
                    pattern="^[UWCGD][A-Z0-9]{2,}$",
                    description=[
                        "Slack id to send to. A user id (U or W prefix) opens or reuses the DM with",
                        "that user; a conversation id (C, G or D prefix) is posted to directly.",
                    ],
                ),
                "slack_message_text": OptionSpec(
                    type="str",
                    required=True,
                    min_length=1,
                    description=["Message text. With a file it is the file's initial comment."],
                ),
                "slack_message_file": OptionSpec(
                    type="str",
                    default="",
                    description=[
                        "Controller path of a file to upload. Empty sends a text-only message."
                    ],
                ),
                "slack_message_title": OptionSpec(
                    type="str",
                    default="",
                    description=["Title shown for the file. Empty uses the file's basename."],
                ),
                "slack_message_token_item": OptionSpec(
                    type="str",
                    required=True,
                    description=[
                        "1Password item holding the bot token (xoxb), read with",
                        "richard_lynch.common.onepassword_cached. Title with slashes reads empty: use the id",
                        "if a title read comes back empty.",
                    ],
                ),
                "slack_message_token_field": OptionSpec(
                    type="str",
                    default="credential",
                    description=["Field of that item holding the token."],
                ),
                "slack_message_op_vault": OptionSpec(
                    type="str",
                    required=True,
                    description=["1Password vault holding the token item."],
                ),
                "slack_message_api_url": OptionSpec(
                    type="str",
                    default="https://slack.com/api",
                    description=[
                        "Base URL of the Slack Web API. Only a test points it anywhere else."
                    ],
                ),
                "slack_message_share_wait_s": OptionSpec(
                    type="int",
                    default=30,
                    min_value=0,
                    max_value=600,
                    description=[
                        "How long to wait for an uploaded file to show as shared in the target",
                        "conversation (Slack shares it asynchronously after completeUploadExternal).",
                    ],
                ),
            },
        ),
    }
)
