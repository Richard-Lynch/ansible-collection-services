# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

DEFAULT_AUTHOR = "Collection maintainers"
from ansible_devkit.argspec.models import ArgumentSpec, ArgumentSpecs, OptionSpec

# NEVER put literal Jinja delimiters in a description (Ansible templates the argspec).

argument_specs = ArgumentSpecs(
    argument_specs={
        "main": ArgumentSpec(
            short_description="One named Hermes profile: config leaves, environment, SOUL",
            description=[
                "Provisions ONE named profile of a Hermes gateway on the gateway host. A profile's",
                "config.yaml is",
                "also written by the agent and the dashboard, so this role sets only the leaves it",
                "is given and keeps every other key, and writes nothing when nothing differs.",
                "Generate-don't-apply: without hermes_profile_apply it reads, computes and reports",
                "what it would change, and touches nothing.",
                "Which profile, which container, which uid and which leaves are the caller's",
                "configuration and come from inventory; the role knows how, not which.",
            ],
            version_added="1.0",
            author=DEFAULT_AUTHOR,
            options={
                "hermes_profile_name": OptionSpec(
                    type="str",
                    required=True,
                    pattern=r"^[a-z][a-z0-9_-]{0,30}$",
                    forbidden_values=["default"],
                    description=[
                        "The profile's name. Never 'default': that profile holds the API server key",
                        "and is the gateway's own, and this role must not rewrite it.",
                    ],
                ),
                "hermes_profile_container": OptionSpec(
                    type="str",
                    required=True,
                    min_length=1,
                    description="The gateway container the hermes CLI runs in.",
                ),
                "hermes_profile_data_dir": OptionSpec(
                    type="str",
                    required=True,
                    min_length=1,
                    description=[
                        "The host path of the gateway's data directory (its HERMES_HOME bind mount).",
                        "The profile lives at profiles/<name> beneath it.",
                        "Its PARENT is the anchor of every write and read the role makes in the",
                        "profile: the gateway can write under the data directory, so each file is",
                        "reached by a no-follow walk from that parent (files/agent_dir.py), never by",
                        "name. The parent must be a real directory owned by",
                        "hermes_profile_anchor_uid and not group- or other-writable.",
                    ],
                ),
                "hermes_profile_anchor_uid": OptionSpec(
                    type="int",
                    default=0,
                    min_value=0,
                    description=[
                        "Owner uid of the data directory's parent, the anchor of the confined walk.",
                        "Root on a normal host; a different owner is refused rather than trusted.",
                    ],
                ),
                "hermes_profile_uid": OptionSpec(
                    type="int",
                    required=True,
                    min_value=1,
                    description="Numeric uid the gateway runs as; every file written is owned by it.",
                ),
                "hermes_profile_gid": OptionSpec(
                    type="int",
                    required=True,
                    min_value=1,
                    description="Numeric gid the gateway runs as.",
                ),
                "hermes_profile_leaves": OptionSpec(
                    type="list",
                    elements="dict",
                    required=True,
                    min_length=1,
                    description=[
                        "Config leaves to set: each a mapping with key (dotted string or list of",
                        "segments) and value. Build them with the hermes_coder_profile_leaves filter.",
                    ],
                ),
                "hermes_profile_env": OptionSpec(
                    type="dict",
                    default={},
                    no_log=True,
                    description=[
                        "NAME to value pairs written to the profile's .env (mode 0600, one line each,",
                        "other lines kept). A value containing a quote or a newline is refused.",
                        "In the run that CREATES the profile the cloned .env is replaced by these",
                        "lines alone: a clone copies the source's whole .env (the gateway's API key,",
                        "chat tokens, provider keys), and a profile must hold only its own.",
                    ],
                ),
                "hermes_profile_platform_toolsets": OptionSpec(
                    type="list",
                    elements="str",
                    default=[],
                    description=[
                        "When non-empty, EVERY platform_toolsets.<platform> key already present in the",
                        "profile's config.yaml is set to this list, before the explicit leaves apply.",
                        "A clone carries the source profile's list for every chat platform, so a",
                        "leaf for one platform leaves every other route into the profile with the",
                        "source's wider toolset. Empty leaves the cloned keys as they are.",
                    ],
                ),
                "hermes_profile_compose_dir": OptionSpec(
                    type="str",
                    default="",
                    description=[
                        "The gateway's compose project directory. When set, the run that CREATES the",
                        "profile restarts the gateway through compose before the AFTER probe, so the",
                        "probe is COLD: the missing-key break shows only at the next start, and",
                        "a process that still holds its key passes a warm probe. Empty keeps the",
                        "AFTER probe warm, and the role says so in its output.",
                    ],
                ),
                "hermes_profile_soul": OptionSpec(
                    type="str",
                    default="",
                    description=[
                        "SOUL.md text, seeded ONCE: an existing SOUL.md is never overwritten (the",
                        "agent and its operator curate it afterwards), EXCEPT in the run that created the",
                        "profile, where a cloned SOUL is the source's and not curated for this profile.",
                        "Empty means do not seed.",
                    ],
                ),
                "hermes_profile_create_args": OptionSpec(
                    type="list",
                    elements="str",
                    default=[],
                    description=[
                        "Extra arguments for hermes profile create, one list item per argv element",
                        "(so a description with spaces needs no quoting). Flags of hermes v0.21.x:",
                        "--clone-from SOURCE, --no-alias, --no-skills, --description TEXT. Without a",
                        "clone flag the new profile starts with NO provider settings and cannot call a",
                        "model; with one it receives the source's config.yaml, .env, SOUL and skills.",
                    ],
                ),
                "hermes_profile_models_url": OptionSpec(
                    type="str",
                    required=True,
                    min_length=1,
                    description=[
                        "The gateway API's models URL (host:port/v1/models). Read before and after",
                        "creating a profile: once the first named profile exists, a gateway whose API key",
                        "comes only from the compose env_file stops serving at its next start.",
                    ],
                ),
                "hermes_profile_api_key": OptionSpec(
                    type="str",
                    required=True,
                    no_log=True,
                    description="The API server key presented to the models URL.",
                ),
                "hermes_profile_readbacks": OptionSpec(
                    type="list",
                    elements="dict",
                    default=[],
                    description=[
                        "Checks run through the hermes CLI after the write, against Hermes' own view.",
                        "Each: name, argv (a list), must_match (regex that stdout must contain) and",
                        "optionally must_not_match. A read-back that cannot run FAILS the play.",
                    ],
                ),
                "hermes_profile_apply": OptionSpec(
                    type="bool",
                    default=False,
                    description=[
                        "Write. False reports what would change and writes nothing (the default).",
                    ],
                ),
            },
        ),
    }
)
