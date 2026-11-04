# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

DEFAULT_AUTHOR = "Collection maintainers"
from ansible_devkit.argspec.models import ArgumentSpec, ArgumentSpecs, OptionSpec

# NEVER put literal Jinja delimiters in a description (Ansible TEMPLATES the argspec).
#
# Which host, which state path, which gateway, which model aliases and which probe set are the
# caller's configuration (inventory). The defaults below are right for any guest running the Hermes
# image: its own paths, ports and env-var NAMES.

_O = {
    "hermes_learning_host_home": OptionSpec(
        type="str",
        required=True,
        description=[
            "Host path of the agent's state directory (the directory the container mounts as",
            "HERMES_HOME). The ledger and probe summaries are written under it for the mirror.",
        ],
    ),
    "hermes_learning_textfile_dir": OptionSpec(
        type="str",
        required=True,
        description=["Directory the node exporter's textfile collector reads."],
    ),
    "hermes_learning_probes": OptionSpec(
        type="list",
        elements="dict",
        required=True,
        description=[
            "The fixed probe set: each a dict of id, prompt and expect (contains, regex,",
            "not_contains, tool lists), optionally model and timeout_s. Tasks with a checkable",
            "outcome, written for the agent's own work. Judged on the host; the expectation never",
            "enters the container.",
        ],
    ),
    "hermes_learning_consolidate_gateway_url": OptionSpec(
        type="str",
        required=True,
        description=[
            "OpenAI-compatible base URL (ending /v1) the consolidation job calls for its one",
            "aux-model request, from inside the container.",
        ],
    ),
    "hermes_learning_consolidate_key_env": OptionSpec(
        type="str",
        required=True,
        description=[
            "NAME of the container environment variable holding the gateway key. The value is",
            "read inside the container and never seen by the host or this role.",
        ],
    ),
    "hermes_learning_container": OptionSpec(
        type="str", default="hermes", description=["The agent's container name."]
    ),
    "hermes_learning_container_python": OptionSpec(
        type="str",
        default="/opt/hermes/.venv/bin/python",
        description=["The agent's own interpreter inside the container."],
    ),
    "hermes_learning_container_workdir": OptionSpec(
        type="str",
        default="/opt/hermes",
        description=["Working directory inside the container (the app checkout)."],
    ),
    "hermes_learning_exec_user": OptionSpec(
        type="str",
        default="10000",
        description=[
            "uid the inner scripts run as, so anything staged is owned by the agent exactly as",
            "its own writes are. 10000 is the image's hermes uid.",
        ],
    ),
    "hermes_learning_home": OptionSpec(
        type="str",
        default="/opt/data",
        description=["HERMES_HOME as the container sees it."],
    ),
    "hermes_learning_exec_mode": OptionSpec(
        type="str",
        default="docker",
        choices=["docker", "local"],
        description=[
            "docker runs each inner script through docker exec. local runs it with the host",
            "interpreter against hermes_learning_host_home: the molecule scenario only.",
        ],
    ),
    "hermes_learning_hermes_cli": OptionSpec(
        type="str",
        default="hermes",
        description=["The hermes CLI inside the container (plugin install and read-back)."],
    ),
    "hermes_learning_install_dir": OptionSpec(
        type="str",
        default="/usr/local/lib/hermes-learning",
        description=["Where the scripts go."],
    ),
    "hermes_learning_config_dir": OptionSpec(
        type="str",
        default="/etc/hermes-learning",
        description=["config.json and probes.json. Secret-free by construction."],
    ),
    "hermes_learning_state_dir": OptionSpec(
        type="str",
        default="/var/lib/hermes-learning",
        description=["Root-only state: the weekly ledger, probe history, ping de-duplication."],
    ),
    "hermes_learning_dir": OptionSpec(
        type="str",
        default="learning",
        description=[
            "Directory under the state root for the mirror copies (ledger, probes, consolidation",
            "proposals). Add it to agent_state_mirror_paths to have it committed nightly.",
        ],
    ),
    "hermes_learning_recall_scratch": OptionSpec(
        type="str",
        default="/opt/data/learning/recall-scratch",
        description=[
            "Container path of the recall test's scratch store. Exclude it from the backup and",
            "the mirror: it holds test notes, not the user's memory.",
        ],
    ),
    "hermes_learning_skip_names_file": OptionSpec(
        type="str",
        default="",
        description=[
            "Path under the state root listing bundled skill names, left out of skills_total.",
        ],
    ),
    "hermes_learning_api_url": OptionSpec(
        type="str",
        default="http://127.0.0.1:8642",
        description=["The agent's API server as seen from inside its own container."],
    ),
    "hermes_learning_api_key_env": OptionSpec(
        type="str",
        default="API_SERVER_KEY",
        description=["NAME of the container env var holding the API server key."],
    ),
    "hermes_learning_probe_model": OptionSpec(
        type="str",
        required=True,
        description=[
            "Model (or gateway alias) the probe runs ask for; a cheap model keeps the nightly",
            "set affordable.",
        ],
    ),
    "hermes_learning_probe_timeout_s": OptionSpec(
        type="int",
        default=180,
        min_value=10,
        description=["Per-probe wait for a terminal run."],
    ),
    "hermes_learning_consolidate_model": OptionSpec(
        type="str",
        required=True,
        description=["Model (or gateway alias) for the consolidation request."],
    ),
    "hermes_learning_consolidate_max_loss": OptionSpec(
        type="float",
        default=0.25,
        min_value=0.0,
        max_value=1.0,
        description=[
            "The loss cap: a proposal leaving more than this fraction of prior MEMORY.md",
            "entries uncited is refused and never staged.",
        ],
    ),
    "hermes_learning_consolidate_min_entries": OptionSpec(
        type="int",
        default=8,
        min_value=1,
        description=["Below this many entries there is nothing worth consolidating."],
    ),
    "hermes_learning_ping_age_s": OptionSpec(
        type="int",
        default=86400,
        min_value=60,
        description=["A pending write older than this pings Slack (24 h by default)."],
    ),
    "hermes_learning_ping_repeat_s": OptionSpec(
        type="int",
        default=86400,
        min_value=60,
        description=["The same pending write is pinged again at most this often."],
    ),
    "hermes_learning_slack_token_env": OptionSpec(
        type="str",
        default="SLACK_BOT_TOKEN",
        description=["NAME of the container env var holding the Slack bot token."],
    ),
    "hermes_learning_slack_channel_env": OptionSpec(
        type="str",
        default="SLACK_HOME_CHANNEL",
        description=["NAME of the container env var holding the Slack channel id to post to."],
    ),
    "hermes_learning_claims_window_s": OptionSpec(
        type="int",
        default=90000,
        min_value=0,
        description=[
            "Window the weak-model guard reads state.db over for memory claims with no memory",
            "tool call. 0 disables the read.",
        ],
    ),
    "hermes_learning_enable_timers": OptionSpec(
        type="bool",
        default=True,
        description=["Enable and start the three timers. False only where there is no systemd."],
    ),
    "hermes_learning_unit_dir": OptionSpec(
        type="str",
        default="/etc/systemd/system",
        description=["Where the units are written."],
    ),
    "hermes_learning_hourly_oncalendar": OptionSpec(
        type="str",
        default="hourly",
        description=["Ledger, pending ages and the 24 h ping."],
    ),
    "hermes_learning_probes_oncalendar": OptionSpec(
        type="str", default="*-*-* 03:30:00", description=["The nightly probe set."]
    ),
    "hermes_learning_consolidate_oncalendar": OptionSpec(
        type="str",
        default="Sun *-*-* 04:00:00",
        description=["The weekly consolidation proposal."],
    ),
    "hermes_learning_stack_dir": OptionSpec(
        type="str",
        default="/opt/hermes",
        description=["The compose project directory restarted after a plugin install."],
    ),
    "hermes_learning_health_url": OptionSpec(
        type="str",
        default="http://127.0.0.1:8642/health",
        description=["Host-side URL that answers 200 once the agent is back after that restart."],
    ),
    "hermes_learning_memory_plugin": OptionSpec(
        type="str",
        default="",
        description=[
            "Catalog name of the memory provider plugin to install, pinned (empty: built-in",
            "memory only). Its activation is memory.provider in config.yaml, owned by",
            "service_hermes through hermes_config_owned.",
        ],
    ),
    "hermes_learning_memory_plugin_section": OptionSpec(
        type="str",
        default="",
        description=[
            "config.yaml section the plugin reads its settings from (plugins.<section>);",
            "Holographic's is hermes-memory-store.",
        ],
    ),
    "hermes_learning_memory_plugin_settings": OptionSpec(
        type="dict",
        default={},
        description=[
            "Scalar settings written under plugins.<section> with the agent's own",
            "`hermes config set`, read back with `hermes config get`. Env-var NAMES only, never",
            "a credential: the plugin reads the key from the variable it is told to.",
        ],
    ),
    "hermes_learning_memory_plugin_ref": OptionSpec(
        type="str",
        default="",
        description=[
            "The 40-character commit the plugin is installed at (hermes plugins install --ref)."
        ],
    ),
    "hermes_learning_memory_plugin_version": OptionSpec(
        type="str",
        default="",
        description=["The version label the read-back requires hermes plugins list to report."],
    ),
}

argument_specs = ArgumentSpecs(
    argument_specs={
        "main": ArgumentSpec(
            short_description="Install the Hermes learning loop: ledger, probe set, consolidation, ping",
            description=[
                "Installs a host script and three timers that read the agent's own state through",
                "its own interpreter (docker exec): an hourly ledger with pending-approval ages and",
                "a Slack ping for anything waiting over 24 h, a nightly fixed probe set through",
                "/v1/runs, and a weekly MEMORY.md consolidation proposal that is refused above a loss",
                "cap and otherwise staged for approval, never written. Optionally installs the",
                "memory provider plugin at a pinned commit. Metrics go to the textfile collector.",
            ],
            version_added="1.0",
            author=DEFAULT_AUTHOR,
            options=_O,
        ),
    }
)
