# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

DEFAULT_AUTHOR = "Collection maintainers"
from ansible_devkit.argspec.models import ArgumentSpec, ArgumentSpecs, OptionSpec

# NEVER put literal Jinja delimiters in a description (Ansible templates the argspec).

argument_specs = ArgumentSpecs(
    argument_specs={
        "main": ArgumentSpec(
            short_description="The reviewed MCP catalogue and its per-client enablement",
            description=[
                "Validates the reviewed MCP catalogue (every server an agent client may ever load)",
                "and renders it, with the declared per-client enablement, as one JSON document the",
                "settings service and the probe read. Adding a server is a PR to the catalogue;",
                "enabling a catalogued server for a client is live, through the settings service.",
                "Which servers exist, who reviewed them and which client may load them are the",
                "caller's configuration; the role knows how to check and render them.",
            ],
            version_added="1.0",
            author=DEFAULT_AUTHOR,
            options={
                "mcp_servers": OptionSpec(
                    type="list",
                    elements="dict",
                    required=True,
                    min_length=1,
                    description=[
                        "Catalogue entries: name, transport (http or stdio), url and headers, or",
                        "command, args and env; tools_include (required, non-empty); clients (the",
                        "profiles that may enable it); env_names (credentials the client's .env must",
                        "carry, referenced only as dollar-brace NAME); destinations; reviewed_by",
                        "(a human; pending is refused).",
                    ],
                ),
                "mcp_clients": OptionSpec(
                    type="dict",
                    required=False,
                    default={},
                    description=[
                        "Declared enablement: client profile name to the list of catalogue names it",
                        "starts with. The live enablement moves within the catalogue afterwards",
                        "(tier A-range) and is reported, never reverted, by the probe.",
                    ],
                ),
                "mcp_clients_file": OptionSpec(
                    type="str",
                    required=True,
                    min_length=1,
                    description=[
                        "Absolute path on the target of the rendered catalogue document",
                        "(mcp-catalogue.json). Root-owned, 0644, read-only to the agent.",
                    ],
                ),
            },
        ),
    }
)
