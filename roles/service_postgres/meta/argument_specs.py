# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations
from ansible_devkit.argspec.models import ArgumentSpec, ArgumentSpecs, OptionSpec
DEFAULT_AUTHOR = "Collection maintainers"

argument_specs = ArgumentSpecs(
    argument_specs={
        "main": ArgumentSpec(
            short_description="Deploy Postgres (+exporter) on the shared network with per-service DBs",
            version_added="1.0",
            author=DEFAULT_AUTHOR,
            options={
                "postgres_container_name": OptionSpec(
                    type="str", required=False, description=["Container name."]
                ),
                "postgres_image": OptionSpec(
                    type="str", required=False, description=["Postgres image tag."]
                ),
                "postgres_host_port": OptionSpec(
                    type="int", required=False, description=["Host port → 5432."]
                ),
                "postgres_bind_address": OptionSpec(
                    type="str",
                    required=False,
                    description=[
                        "Host address the database port is published on (default loopback). Empty",
                        "string publishes on every interface, which the host firewall does not filter.",
                    ],
                ),
                "postgres_verify_host": OptionSpec(
                    type="str",
                    required=False,
                    description=[
                        "Address the verify entrypoint connects to, from the target host itself.",
                        "Default: postgres_bind_address, or 127.0.0.1 when that is empty or 0.0.0.0.",
                    ],
                ),
                "postgres_db": OptionSpec(
                    type="str", required=False, description=["Superuser bootstrap database."]
                ),
                "postgres_user": OptionSpec(
                    type="str", required=False, description=["Superuser role."]
                ),
                "postgres_password": OptionSpec(
                    type="str",
                    required=False,
                    description=[
                        "Superuser password, supplied by the caller (the placeholder default is rejected)."
                    ],
                ),
                "postgres_network": OptionSpec(
                    type="str", required=False, description=["Shared external docker network."]
                ),
                "postgres_databases": OptionSpec(
                    type="list",
                    elements="dict",
                    required=False,
                    description=["Per-service DBs to ensure: [{name, owner, password}]."],
                ),
                "postgres_settings": OptionSpec(
                    type="dict", required=False, description=["postgresql.conf settings."]
                ),
                "postgres_config_file_path": OptionSpec(
                    type="str",
                    required=False,
                    description=["Path to the rendered postgresql.conf."],
                ),
                "postgres_config_path": OptionSpec(
                    type="str", required=False, description=["Config dir."]
                ),
                "postgres_exporter_enabled": OptionSpec(
                    type="bool",
                    required=False,
                    default=True,
                    description=["Deploy postgres_exporter (Prometheus scrape)."],
                ),
                "postgres_exporter_image": OptionSpec(
                    type="str", required=False, description=["Exporter image."]
                ),
                "postgres_exporter_port": OptionSpec(
                    type="int", required=False, description=["Exporter host port."]
                ),
                "postgres_exporter_bind_address": OptionSpec(
                    type="str",
                    required=False,
                    description=[
                        "Host address the exporter port is published on (default loopback: the",
                        "exporter has no authentication). Empty string publishes on every interface.",
                    ],
                ),
                "postgres_env_file_mode": OptionSpec(
                    type="str",
                    required=False,
                    description=[
                        "Mode of the rendered env files, which hold credentials (default 0600)."
                    ],
                ),
                "pgadmin_enabled": OptionSpec(
                    type="bool",
                    required=False,
                    default=False,
                    description=["Deploy pgAdmin (off by default)."],
                ),
                "pgadmin_image": OptionSpec(
                    type="str", required=False, description=["pgAdmin image."]
                ),
                "pgadmin_container_name": OptionSpec(
                    type="str", required=False, description=["pgAdmin container name."]
                ),
                "pgadmin_host_port": OptionSpec(
                    type="int", required=False, description=["pgAdmin host port."]
                ),
                "pgadmin_bind_address": OptionSpec(
                    type="str",
                    required=False,
                    description=[
                        "Host address the pgAdmin port is published on (default loopback). Empty",
                        "string publishes on every interface.",
                    ],
                ),
                "pgadmin_default_email": OptionSpec(
                    type="str", required=False, description=["pgAdmin login email."]
                ),
                "pgadmin_default_password": OptionSpec(
                    type="str", required=False, description=["pgAdmin login password."]
                ),
            },
        )
    }
)
