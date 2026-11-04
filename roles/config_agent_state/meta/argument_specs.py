# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

DEFAULT_AUTHOR = "Collection maintainers"
from ansible_devkit.argspec.models import ArgumentSpec, ArgumentSpecs, OptionSpec

# NEVER put literal Jinja delimiters in a description (Ansible TEMPLATES the argspec).
#
# One definition per option, composed into each entrypoint, so the same name can never mean two
# things. `required` differs per entrypoint and is applied by _pick(). Which host, which paths,
# which repo, which recipient: ALL of it is the caller's configuration (inventory). The few
# defaults below are right for any caller (a retention count, a calendar, a binary name).

_O = {
    "agent_state_product": OptionSpec(
        type="str",
        default="hermes",
        # A label, not a list of supported products: the archive name parser takes exactly this
        # alphabet (agent_state.py ARCHIVE_RE), so any product name a caller uses round-trips.
        pattern=r"^[a-z0-9-]+$",
        description=[
            "Which agent product this state belongs to (the role is built for Hermes and keeps",
            "every product-specific value an option). It names the archives and the metrics",
            "label so a second product can share a backup share without colliding. Lowercase",
            "letters, digits and hyphens.",
        ],
    ),
    "agent_state_root": OptionSpec(
        type="str",
        description=[
            "Absolute path of the agent's state directory on this host (the directory the",
            "container bind-mounts as its data volume).",
        ],
    ),
    "agent_state_python": OptionSpec(
        type="str",
        default="python3",
        description=[
            "Interpreter that runs agent_state.py. It must be 3.12 or newer (tarfile extraction",
            "filters); the script refuses an older one rather than extracting unfiltered.",
        ],
    ),
    "agent_state_install_dir": OptionSpec(
        type="str",
        default="/usr/local/lib/agent-state",
        description=["Where the role installs agent_state.py on the host."],
    ),
    "agent_state_packages": OptionSpec(
        type="list",
        elements="str",
        default=["age", "zstd", "git"],
        description=[
            "OS packages the mechanics need. The role asserts each binary is present afterwards",
            "whether or not it installed anything.",
        ],
    ),
    "agent_state_textfile_dir": OptionSpec(
        type="str",
        description=[
            "Directory the node exporter's textfile collector reads. The backup and mirror jobs",
            "write a last-success TIMESTAMP there (the alert computes age from it, so a job that",
            "stops running cannot leave a frozen 'age' behind).",
        ],
    ),
    "agent_state_mirror_paths": OptionSpec(
        type="list",
        elements="str",
        description=[
            "Layer-2 paths, relative to the state root, mirrored to git: persona, memory, skills,",
            "cron jobs, webhook routes. Files only; never the runtime layer.",
        ],
    ),
    "agent_state_mirror_exclude": OptionSpec(
        type="list",
        elements="str",
        default=[],
        description=[
            "fnmatch patterns (matched per path and per component) left out of the mirror."
        ],
    ),
    "agent_state_mirror_skip_names_file": OptionSpec(
        type="str",
        default="",
        description=[
            "File (relative to the root) listing names, one per line (``name`` or ``name:hash``),",
            "that the mirror leaves out wherever they appear as a path component. For Hermes this is",
            "``skills/.bundled_manifest``: the image's own skills return with the image, and their",
            "sample addresses and ``api_key=`` lines would block the secret scan on every run.",
            "Empty excludes nothing.",
        ],
    ),
    "agent_state_config_file": OptionSpec(
        type="str",
        default="",
        description=[
            "Optional config file (relative to the root) mirrored as a MASKED snapshot: any line",
            "whose key looks like a credential has its value replaced. Empty means none.",
        ],
    ),
    "agent_state_mask_patterns": OptionSpec(
        type="list",
        elements="str",
        default=[],
        description=["Extra regexes of config KEY names to mask, added to the built-in set."],
    ),
    "agent_state_scan_patterns": OptionSpec(
        type="list",
        elements="str",
        default=[],
        description=[
            "Extra regexes the pre-commit scan blocks on, added to the built-in secret and PII",
            "shapes. The scan blocks from the very first run.",
        ],
    ),
    "agent_state_repo_url": OptionSpec(
        type="str",
        description=["Remote URL of the (private) mirror repository."],
    ),
    "agent_state_repo_branch": OptionSpec(
        type="str", default="main", description=["Branch the mirror pushes to."]
    ),
    "agent_state_deploy_key": OptionSpec(
        type="str",
        no_log=True,
        description=[
            "PRIVATE deploy key for the mirror repository (OpenSSH format), read from a secret",
            "store by the play. Written 0600 on the host; never printed.",
        ],
    ),
    "agent_state_key_dir": OptionSpec(
        type="str",
        default="/etc/agent-state",
        description=["Directory (0700) holding the deploy key on the host."],
    ),
    "agent_state_mirror_dir": OptionSpec(
        type="str",
        default="/var/lib/agent-state/mirror",
        description=[
            "Working clone the mirror commits in. Rebuildable; holds no state of its own."
        ],
    ),
    "agent_state_git_author_name": OptionSpec(
        type="str", default="agent-state-mirror", description=["Commit author name."]
    ),
    "agent_state_git_author_email": OptionSpec(
        type="str",
        default="agent-state-mirror@localhost",
        description=["Commit author email."],
    ),
    "agent_state_backup_target": OptionSpec(
        type="str",
        description=[
            "Absolute path of the mounted backup share directory. The job REFUSES if it is not an",
            "existing directory, so an unmounted share cannot silently fill the host's disk.",
        ],
    ),
    "agent_state_backup_recipient": OptionSpec(
        type="str",
        description=[
            "age PUBLIC key (age1...) the archives are encrypted to. The private key stays in a",
            "secret store and never lives on the host.",
        ],
    ),
    "agent_state_backup_keep": OptionSpec(
        type="int",
        default=14,
        min_value=1,
        description=["Newest archives kept (the ring)."],
    ),
    "agent_state_backup_keep_monthly": OptionSpec(
        type="int",
        default=12,
        min_value=0,
        description=["How many months' FIRST archive are kept beyond the ring."],
    ),
    "agent_state_backup_exclude": OptionSpec(
        type="list",
        elements="str",
        default=[],
        description=[
            "fnmatch patterns left out of the runtime tar (caches, logs, sandboxes, browser",
            "profiles). A pattern ending in a slash matches a directory name.",
        ],
    ),
    "agent_state_sqlite_files": OptionSpec(
        type="list",
        elements="str",
        default=[],
        description=[
            "SQLite files (relative to the root) copied with the online-backup API instead of a",
            "file copy, then integrity-checked. A live database copied as a file loses its WAL.",
        ],
    ),
    "agent_state_enable_timers": OptionSpec(
        type="bool",
        default=True,
        description=[
            "Enable and start the systemd timers. False only where there is no systemd (the",
            "molecule scenario); the units are still rendered and read back.",
        ],
    ),
    "agent_state_unit_dir": OptionSpec(
        type="str",
        default="/etc/systemd/system",
        description=["Where unit files are written."],
    ),
    "agent_state_backup_oncalendar": OptionSpec(
        type="str",
        default="*-*-* 02:00:00",
        description=["OnCalendar of the nightly backup."],
    ),
    "agent_state_mirror_oncalendar": OptionSpec(
        type="str",
        default="*-*-* 02:30:00",
        description=["OnCalendar of the nightly mirror."],
    ),
    "agent_state_backup_label": OptionSpec(
        type="str",
        default="manual",
        pattern="^[A-Za-z0-9._-]{1,48}$",
        description=["Label in the archive name, e.g. pre-upgrade."],
    ),
    # ---- restore ---------------------------------------------------------------------------
    "agent_state_restore_confirm": OptionSpec(
        type="bool",
        default=False,
        description=["Gate: a restore replaces live state and stops the stack. Must be true."],
    ),
    "agent_state_restore_from": OptionSpec(
        type="str",
        description=[
            "Archive file name inside the backup target (runtime or both scope). The play takes",
            "it from a vars file, never from the command line.",
        ],
    ),
    "agent_state_restore_scope": OptionSpec(
        type="str",
        default="runtime",
        choices=["mind", "runtime", "both"],
        description=[
            "mind: only the mirror paths, from git. runtime: the archive. both: the archive,",
            "then the mirror paths over it (layer 2 is newer than last night's tar).",
        ],
    ),
    "agent_state_restore_ref": OptionSpec(
        type="str",
        default="HEAD",
        description=["Git ref the mind scope restores from."],
    ),
    "agent_state_restore_identity": OptionSpec(
        type="str",
        no_log=True,
        description=[
            "age PRIVATE key (AGE-SECRET-KEY-...) read from a secret store for this run. Written to a",
            "0600 temp file for the decrypt and removed in an always block.",
        ],
    ),
    "agent_state_restore_quiet_start": OptionSpec(
        type="bool",
        default=True,
        description=[
            "Start with the quiet-start overlay (platforms, cron dispatch and webhooks off) until",
            "the read-back passes, so nothing can act on half-restored state.",
        ],
    ),
    "agent_state_quiet_start_env": OptionSpec(
        type="dict",
        default={},
        description=[
            "Environment the quiet-start overlay sets. Which variables disable a product's",
            "platforms, cron dispatch and webhook listener is product knowledge declared in",
            "inventory; an empty dict with quiet start on is REFUSED, not treated as 'nothing to",
            "disable'.",
        ],
    ),
    "agent_state_required_paths": OptionSpec(
        type="list",
        elements="str",
        default=[],
        description=["Paths that must exist in the restored tree or the restore is refused."],
    ),
    "agent_state_stack_managed": OptionSpec(
        type="bool",
        default=True,
        description=[
            "Stop and start the compose stack around the restore. False only where there is no",
            "docker daemon (the molecule scenario).",
        ],
    ),
    "agent_state_stack_project_dir": OptionSpec(
        type="str",
        default="",
        description=["Compose project directory of the agent stack (required when managed)."],
    ),
    "agent_state_health_url": OptionSpec(
        type="str",
        default="",
        description=["URL that answers 200 once the agent is healthy (required when managed)."],
    ),
    "agent_state_health_retries": OptionSpec(
        type="int", default=30, min_value=1, description=["Health polls, 5 s apart."]
    ),
    "agent_state_readback_checks": OptionSpec(
        type="list",
        elements="dict",
        default=[],
        description=[
            "Read-backs run after the restore: dicts of name, url, status (default 200) and",
            "contains (optional substring of the body). Declared per product in inventory.",
        ],
    ),
    "agent_state_readback_api_key": OptionSpec(
        type="str",
        default="",
        no_log=True,
        description=["Bearer key the read-backs present, when the API needs one."],
    ),
    "agent_state_stack_service": OptionSpec(
        type="str",
        default="",
        description=["Compose service name of the agent (required for quiet start)."],
    ),
    "agent_state_stack_compose_file": OptionSpec(
        type="str",
        default="docker-compose.yml",
        description=["Compose file name inside the project directory."],
    ),
    # ---- drill -----------------------------------------------------------------------------
    "agent_state_drill_confirm": OptionSpec(
        type="bool", default=False, description=["Gate for the isolated restore drill."]
    ),
    "agent_state_drill_port": OptionSpec(
        type="int",
        min_value=1024,
        max_value=65535,
        description=[
            "Port the agent LISTENS on inside the drill container (it must match the product env",
            "in agent_state_drill_env). Nothing is published on the host: the drill is reached by",
            "its address on an internal docker network.",
        ],
    ),
    "agent_state_drill_dir": OptionSpec(
        type="str",
        default="/var/tmp/agent-state-drill",
        description=["Scratch directory the drill restores into. Removed afterwards."],
    ),
    "agent_state_drill_image": OptionSpec(
        type="str",
        description=["The pinned image reference (repo:tag@sha256:...) the drill runs."],
    ),
    "agent_state_drill_command": OptionSpec(
        type="list",
        elements="str",
        default=[],
        description=[
            "The container command the drill runs (the compose `command:`). Empty leaves the image's",
            "own default, which for an agent image is usually an INTERACTIVE shell that exits at once",
            "without a TTY (Hermes prints its banner and shuts down). Set it to what the real",
            "stack runs (Hermes: gateway run).",
        ],
    ),
    "agent_state_drill_env": OptionSpec(
        type="dict",
        default={},
        description=[
            "Scratch environment for the drill container. Any key that looks like a credential is",
            "REFUSED: the drill must hold no Slack, mail or API tokens and no spendable key.",
        ],
    ),
    "agent_state_drill_scratch_secrets": OptionSpec(
        type="list",
        elements="str",
        default=[],
        description=[
            "Environment names that receive a RANDOM value generated for this run only (never read",
            "from a secret store, never stored). The drill container needs some credential to start its",
            "API; it gets a throwaway nobody else holds. Names here are exempt from the",
            "credential-name refusal that applies to agent_state_drill_env.",
        ],
    ),
    "agent_state_drill_readback_key_env": OptionSpec(
        type="str",
        default="",
        description=[
            "Which scratch secret the drill's read-backs present as a bearer key (empty: none).",
        ],
    ),
    "agent_state_drill_data_path": OptionSpec(
        type="str",
        default="/opt/data",
        description=["Path inside the container where the state directory is mounted."],
    ),
    "agent_state_drill_health_path": OptionSpec(
        type="str",
        default="/health",
        description=["Path on the drill container that answers 200 once the agent is up."],
    ),
    "agent_state_drill_archive": OptionSpec(
        type="str",
        default="latest",
        description=["Archive to drill: a file name, or latest."],
    ),
    "agent_state_drill_readbacks": OptionSpec(
        type="list",
        elements="dict",
        default=[],
        description=["Read-backs against the drill container (same shape as the restore's)."],
    ),
    "agent_state_verify_max_age_seconds": OptionSpec(
        type="int",
        default=129600,
        min_value=1,
        description=["verify fails when the newest backup is older than this (36 h)."],
    ),
}


def _pick(required: list[str], optional: list[str]) -> dict[str, OptionSpec]:
    out = {}
    for name in required:
        out[name] = _O[name].model_copy(update={"required": True, "default": None})
    for name in optional:
        out[name] = _O[name]
    return out


_COMMON = ["agent_state_product", "agent_state_python", "agent_state_install_dir"]
_MIRROR_OPT = [
    "agent_state_mirror_exclude",
    "agent_state_mirror_skip_names_file",
    "agent_state_config_file",
    "agent_state_mask_patterns",
    "agent_state_scan_patterns",
    "agent_state_repo_branch",
    "agent_state_key_dir",
    "agent_state_mirror_dir",
    "agent_state_git_author_name",
    "agent_state_git_author_email",
]
_BACKUP_OPT = [
    "agent_state_backup_keep",
    "agent_state_backup_keep_monthly",
    "agent_state_backup_exclude",
    "agent_state_sqlite_files",
]

argument_specs = ArgumentSpecs(
    argument_specs={
        "main": ArgumentSpec(
            short_description="Install the agent-state backup and mirror jobs and their timers",
            description=[
                "Installs the mechanics, renders the nightly backup and mirror units, and reads",
                "them back. Layer 2 (persona, memory, skills, jobs) goes to git with a",
                "pre-commit secret and PII scan that blocks from the first run; layer 3 (the",
                "runtime tree) goes to the backup share as an age-encrypted tar with SQLite",
                "copied through the online-backup API. Converging this changes no agent state.",
            ],
            version_added="1.0",
            author=DEFAULT_AUTHOR,
            options={
                **_pick(
                    [
                        "agent_state_root",
                        "agent_state_mirror_paths",
                        "agent_state_repo_url",
                        "agent_state_deploy_key",
                        "agent_state_backup_target",
                        "agent_state_backup_recipient",
                        "agent_state_textfile_dir",
                    ],
                    _COMMON
                    + _MIRROR_OPT
                    + _BACKUP_OPT
                    + [
                        "agent_state_packages",
                        "agent_state_enable_timers",
                        "agent_state_unit_dir",
                        "agent_state_backup_oncalendar",
                        "agent_state_mirror_oncalendar",
                    ],
                ),
            },
        ),
        "backup": ArgumentSpec(
            short_description="Take one encrypted state backup now",
            description=[
                "SQLite files via the online-backup API (integrity-checked; a corrupt database",
                "REFUSES the backup), tar, zstd, age, sha256 manifest, ring prune, last-success",
                "timestamp metric. A new archive each run, so it reports changed every time.",
            ],
            version_added="1.0",
            author=DEFAULT_AUTHOR,
            options=_pick(
                [
                    "agent_state_root",
                    "agent_state_backup_target",
                    "agent_state_backup_recipient",
                    "agent_state_textfile_dir",
                ],
                _COMMON + _BACKUP_OPT + ["agent_state_backup_label"],
            ),
        ),
        "mirror": ArgumentSpec(
            short_description="Mirror layer-2 state to git now (scan blocks the commit)",
            description=[
                "Syncs the mirror paths and a masked config snapshot into the work clone, scans",
                "every file, and only if the scan is clean commits and pushes. A finding fails",
                "the entrypoint, stages nothing and reports file, rule and line (never a value).",
            ],
            version_added="1.0",
            author=DEFAULT_AUTHOR,
            options=_pick(
                [
                    "agent_state_root",
                    "agent_state_mirror_paths",
                    "agent_state_repo_url",
                    "agent_state_deploy_key",
                    "agent_state_textfile_dir",
                ],
                _COMMON + _MIRROR_OPT,
            ),
        ),
        "restore": ArgumentSpec(
            short_description="Restore agent state while the stack is stopped",
            description=[
                "Stop the stack; unpack into a stage directory; validate while stopped (ciphertext",
                "sha, member sha, integrity_check, row counts, required paths); swap atomically,",
                "keeping the old tree aside; quiet start; wait healthy; re-write layer-2 files;",
                "read-back; remove the overlay; restart; read-back again. A corrupt or tampered",
                "archive is refused BEFORE anything is touched.",
            ],
            version_added="1.0",
            author=DEFAULT_AUTHOR,
            options=_pick(
                [
                    "agent_state_restore_confirm",
                    "agent_state_root",
                    "agent_state_restore_identity",
                    "agent_state_mirror_paths",
                ],
                _COMMON
                + [
                    "agent_state_backup_target",
                    "agent_state_restore_from",
                    "agent_state_restore_scope",
                    "agent_state_restore_ref",
                    "agent_state_restore_quiet_start",
                    "agent_state_quiet_start_env",
                    "agent_state_required_paths",
                    "agent_state_stack_managed",
                    "agent_state_stack_project_dir",
                    "agent_state_stack_service",
                    "agent_state_stack_compose_file",
                    "agent_state_health_url",
                    "agent_state_health_retries",
                    "agent_state_readback_checks",
                    "agent_state_readback_api_key",
                    "agent_state_repo_url",
                    "agent_state_deploy_key",
                    "agent_state_key_dir",
                ],
            ),
        ),
        "drill": ArgumentSpec(
            short_description="Restore the latest backup into an isolated scratch container",
            description=[
                "Restores into a scratch directory under a second container that is isolated by",
                "construction (no published port, internal network with no egress, scratch env",
                "with no credential, no deploy key), runs the read-backs, asserts the mirror",
                "gained no commit during the window, and tears everything down in an always block.",
            ],
            version_added="1.0",
            author=DEFAULT_AUTHOR,
            options=_pick(
                [
                    "agent_state_drill_confirm",
                    "agent_state_backup_target",
                    "agent_state_restore_identity",
                    "agent_state_drill_image",
                    "agent_state_drill_port",
                ],
                _COMMON
                + [
                    "agent_state_drill_dir",
                    "agent_state_drill_env",
                    "agent_state_drill_command",
                    "agent_state_drill_scratch_secrets",
                    "agent_state_drill_readback_key_env",
                    "agent_state_drill_data_path",
                    "agent_state_drill_health_path",
                    "agent_state_drill_archive",
                    "agent_state_drill_readbacks",
                    "agent_state_required_paths",
                    "agent_state_mirror_dir",
                    "agent_state_health_retries",
                    "agent_state_stack_managed",
                ],
            ),
        ),
        "verify": ArgumentSpec(
            short_description="Read-only: backup age, manifest check, mirror vs live",
            description=[
                "Reports the newest archive's age and ciphertext hash against its manifest, and how",
                "many live mirror-path files differ from the mirror's HEAD. Fails when the newest",
                "backup is older than the limit or its hash does not match.",
            ],
            version_added="1.0",
            author=DEFAULT_AUTHOR,
            options=_pick(
                [
                    "agent_state_root",
                    "agent_state_mirror_paths",
                    "agent_state_backup_target",
                ],
                _COMMON
                + [
                    "agent_state_mirror_exclude",
                    "agent_state_mirror_skip_names_file",
                    "agent_state_mirror_dir",
                    "agent_state_verify_max_age_seconds",
                ],
            ),
        ),
    }
)
