# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

DEFAULT_AUTHOR = "Collection maintainers"
from ansible_devkit.argspec.models import ArgumentSpec, ArgumentSpecs, OptionSpec

# NEVER put literal Jinja delimiters in a description. Ansible TEMPLATES the argspec, so a
# documentation mention of a Jinja tag is parsed as real Jinja and fails the whole role at
# validate_argument_spec time, before any task runs.

argument_specs = ArgumentSpecs(
    argument_specs={
        "main": ArgumentSpec(
            short_description="Deploy quickR (Postgres, FastAPI backend, nginx web origin)",
            description=[
                "Deploys quickR as one compose project of three containers: Postgres, the",
                "FastAPI backend, and the nginx web origin that serves the PWA and proxies",
                "/api/ and /mcp to the backend. Only the web origin is published on the host;",
                "Postgres and the API port stay on the compose network.",
                "Images are PULLED, pinned by tag -- the role never builds. APNs is optional",
                "and all-or-nothing: the team id, key id and .p8 key must all be set, or none.",
                "Also installs a nightly pg_dump timer, so a whole-machine backup of the host",
                "carries a logically consistent dump and not only a live data directory.",
            ],
            version_added="1.0",
            author=DEFAULT_AUTHOR,
            options={
                # ---- images: required, because WHICH build runs is the caller's decision ----
                "quickr_backend_image": OptionSpec(
                    type="str",
                    required=True,
                    description=[
                        "Fully-qualified backend image pinned by tag AND digest,",
                        "name:<tag>@sha256:<digest>: the tag (the quickR build's full git sha) names",
                        "the build, the digest is what is pulled. A floating tag (latest, main,",
                        "stable, edge) or a missing digest is rejected by the role's own gate.",
                    ],
                ),
                "quickr_frontend_image": OptionSpec(
                    type="str",
                    required=True,
                    description=[
                        "Fully-qualified web-origin image (nginx serving the PWA), pinned the same",
                        "way (name:<tag>@sha256:<digest>). Its tag must equal quickr_backend_image's.",
                    ],
                ),
                "quickr_postgres_image": OptionSpec(
                    type="str",
                    required=False,
                    # quickR's own compose says
                    # postgres:18-alpine, which floats across minors; this is the same major,
                    # pinned. The major must not change without a dump/restore -- Postgres
                    # refuses to start on a data directory from another major.
                    default="postgres:18.6-alpine",
                    description=[
                        "Postgres image. Pinned. Changing the MAJOR version needs a dump and",
                        "restore -- Postgres will not start on another major's data directory.",
                    ],
                ),
                # ---- placement ----------------------------------------------------------
                "quickr_project_name": OptionSpec(
                    type="str",
                    required=False,
                    default="quickr",
                    description=[
                        "Compose project name, and the prefix of every container and of the",
                        "Postgres volume (named explicitly, so it is never project-prefixed).",
                    ],
                ),
                "quickr_config_dir": OptionSpec(
                    type="str",
                    required=False,
                    default="/opt/quickr",
                    description=["Host directory holding the rendered compose project."],
                ),
                "quickr_web_host_port": OptionSpec(
                    type="int",
                    required=False,
                    default=8080,
                    min_value=1,
                    max_value=65535,
                    description=[
                        "Host port published for the web origin (container port 80). The ONLY",
                        "port the project publishes.",
                    ],
                ),
                "quickr_timezone": OptionSpec(
                    type="str",
                    required=False,
                    default="Etc/UTC",
                    description=["TZ passed to the containers."],
                ),
                # ---- database -----------------------------------------------------------
                "quickr_postgres_password": OptionSpec(
                    type="str",
                    required=True,
                    no_log=True,
                    description=[
                        "Password for the quickr database role. Rendered into 0600 env files,",
                        "never into the compose file.",
                    ],
                ),
                "quickr_scheduler_enabled": OptionSpec(
                    type="bool",
                    required=False,
                    default=True,
                    description=[
                        "Run the backend's reminder scheduler (QUICKR_SCHEDULER_ENABLED).",
                    ],
                ),
                # ---- APNs (optional, all-or-nothing) -----------------------------------
                "quickr_apns_team_id": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    description=["Apple Developer Team ID. Empty turns APNs off."],
                ),
                "quickr_apns_key_id": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    description=["Key ID of the APNs .p8 key. Empty turns APNs off."],
                ),
                "quickr_apns_private_key": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    no_log=True,
                    description=[
                        "The APNs .p8 key itself (PEM text). Written to a 0440 file in a",
                        "directory mounted read-only into the backend; never into an",
                        "environment variable, the image or the compose file.",
                    ],
                ),
                "quickr_apns_topic": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    description=[
                        "APNs topic (the app's bundle id). Empty leaves the backend's own",
                        "default in force.",
                    ],
                ),
                "quickr_app_gid": OptionSpec(
                    type="int",
                    required=False,
                    # An IMAGE fact: quickR's backend Dockerfile creates `app` as 10001:10001.
                    default=10001,
                    description=[
                        "Numeric gid the backend container runs as. The APNs key is",
                        "group-owned by it, so the container can read a file that is not",
                        "world-readable.",
                    ],
                ),
                # ---- registry -----------------------------------------------------------
                "quickr_registry": OptionSpec(
                    type="str",
                    required=False,
                    default="ghcr.io",
                    description=["Registry the images are pulled from."],
                ),
                "quickr_registry_username": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    description=[
                        "Registry login. Empty skips the login, for public images.",
                    ],
                ),
                "quickr_registry_password": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    no_log=True,
                    description=[
                        "Registry token, e.g. a GitHub PAT with read:packages only.",
                    ],
                ),
                # ---- backups ------------------------------------------------------------
                "quickr_backup_dir": OptionSpec(
                    type="str",
                    required=False,
                    default="/opt/quickr/backups",
                    description=["Where the nightly pg_dump archives are written."],
                ),
                "quickr_backup_keep_days": OptionSpec(
                    type="int",
                    required=False,
                    default=14,
                    min_value=1,
                    description=["Dumps older than this many days are deleted."],
                ),
                "quickr_backup_on_calendar": OptionSpec(
                    type="str",
                    required=False,
                    default="*-*-* 02:15:00",
                    description=[
                        "systemd OnCalendar for the dump. Schedule it before any whole-machine",
                        "backup of the host, so that backup carries the night's dump.",
                    ],
                ),
                "quickr_backup_metrics_dir": OptionSpec(
                    type="str",
                    required=False,
                    default="/var/lib/node_exporter/textfile_collector",
                    description=[
                        "Where the dump writes quickr_pgdump.prom for a textfile collector.",
                        "Must match the collector's directory, or alerts on the backup metrics",
                        "see nothing. Skipped (with a warning) if it does not exist.",
                    ],
                ),
            },
        )
    }
)
