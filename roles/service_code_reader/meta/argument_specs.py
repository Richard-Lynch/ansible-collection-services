# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

DEFAULT_AUTHOR = "Collection maintainers"
from ansible_devkit.argspec.models import ArgumentSpec, ArgumentSpecs, OptionSpec

# NEVER put literal Jinja delimiters in a description: Ansible TEMPLATES the argspec, so a
# documentation mention of a Jinja tag fails the whole role at validate time.

_SHARED = {
    "code_reader_container_name": OptionSpec(
        type="str",
        required=False,
        default="code-reader",
        description=["Container name."],
    ),
    "code_reader_config_dir": OptionSpec(
        type="str",
        required=False,
        default="/opt/code-reader",
        description=["Host directory holding the rendered compose project and config."],
    ),
    "code_reader_network": OptionSpec(
        type="str",
        required=True,
        min_length=1,
        description=[
            "External docker network to join (created if absent): the one the agent's",
            "container is on. Required, because the right network depends on how the agent is",
            "deployed. The browser filler reaches the",
            "sidecar over this network by container name, because a loopback address inside",
            "the agent's own container is that container's loopback, not the host's.",
        ],
    ),
    "code_reader_image": OptionSpec(
        type="str",
        required=False,
        default="python:3.12.7-slim",
        description=[
            "Pinned base image. The sidecar is one stdlib-only Python file mounted into it,",
            "so there is nothing to build. A floating tag is rejected by the role's gate.",
        ],
    ),
    "code_reader_bind": OptionSpec(
        type="str",
        required=False,
        default="127.0.0.1",
        description=[
            "Host address the sidecar's port is PUBLISHED on. Must be loopback: this service",
            "hands out login codes, and its only authentication is a bearer token. The role",
            "refuses anything but 127.x or ::1.",
        ],
    ),
    "code_reader_port": OptionSpec(
        type="int",
        required=False,
        default=8645,
        min_value=1,
        max_value=65535,
        description=["Port the sidecar listens on (container) and publishes (host loopback)."],
    ),
    "code_reader_timezone": OptionSpec(
        type="str",
        required=False,
        default="Etc/UTC",
        description=["Container timezone."],
    ),
    "code_reader_imap_host": OptionSpec(
        type="str",
        required=True,
        min_length=1,
        description=["IMAP server for the mailbox that receives the codes (TLS, port below)."],
    ),
    "code_reader_imap_port": OptionSpec(
        type="int", required=False, default=993, description=["IMAP over TLS port."]
    ),
    "code_reader_imap_user": OptionSpec(
        type="str",
        required=True,
        min_length=1,
        description=["IMAP login for the code mailbox."],
    ),
    "code_reader_imap_password": OptionSpec(
        type="str",
        required=True,
        no_log=True,
        min_length=1,
        description=["IMAP password. Rendered only into a 0600 env file, never into compose."],
    ),
    "code_reader_imap_folder": OptionSpec(
        type="str",
        required=False,
        default="INBOX",
        description=["Mailbox folder to watch."],
    ),
    "code_reader_poll_seconds": OptionSpec(
        type="int",
        required=False,
        default=15,
        min_value=5,
        max_value=300,
        description=["Seconds between IMAP polls."],
    ),
    "code_reader_sender_allowlist": OptionSpec(
        type="list",
        elements="str",
        required=True,
        min_length=1,
        description=[
            "Senders whose mail may supply a code: an exact address, or an at-sign followed by",
            "a domain to admit that domain exactly (not its subdomains). Mail from anyone else",
            "is ignored. Deliberately required with no default: an empty list would accept",
            "codes from anyone, and the right value depends on the services you sign up to.",
        ],
    ),
    "code_reader_ttl_seconds": OptionSpec(
        type="int",
        required=False,
        default=600,
        min_value=60,
        max_value=3600,
        description=["How long a received code stays available. Each code is also one-shot."],
    ),
    "code_reader_code_patterns": OptionSpec(
        type="list",
        elements="str",
        required=False,
        default=[],
        description=[
            "Extra regular expressions, group 1 being the code, tried before the built-ins.",
            "Empty means the built-in patterns only (digits 4-8 near the word code).",
        ],
    ),
    "code_reader_token": OptionSpec(
        type="str",
        required=True,
        no_log=True,
        min_length=32,
        description=[
            "Bearer token the filler presents on every code request. Rendered only into a",
            "0600 env file.",
        ],
    ),
}

argument_specs = ArgumentSpecs(
    argument_specs={
        "main": ArgumentSpec(
            short_description="Deploy the one-shot verification-code reader sidecar",
            description=[
                "Deploys a small stdlib-only service that polls a dedicated mailbox over IMAP,",
                "keeps each verification code from an allowlisted sender for a short TTL, and",
                "hands it out exactly once to the browser filler over HTTP with a bearer token.",
                "The language model never sees the code: the filler asks for it and types it.",
            ],
            version_added="1.0",
            author=DEFAULT_AUTHOR,
            options=dict(_SHARED),
        ),
        "probe": ArgumentSpec(
            short_description="Read-only health check of the code reader",
            description=[
                "Reads the sidecar's own health endpoint on the host loopback and reports. Sets",
                "nothing, requests no code, and never needs the bearer token.",
            ],
            version_added="1.0",
            author=DEFAULT_AUTHOR,
            options={
                k: _SHARED[k]
                for k in (
                    "code_reader_container_name",
                    "code_reader_bind",
                    "code_reader_port",
                )
            },
        ),
    }
)
