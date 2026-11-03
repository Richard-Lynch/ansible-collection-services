# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

from ansible_devkit.argspec.models import ArgumentSpec, ArgumentSpecs, OptionSpec
DEFAULT_AUTHOR = "Collection maintainers"

argument_specs = ArgumentSpecs(
    argument_specs={
        "main": ArgumentSpec(
            short_description="Write one arr-stack service's docker-compose.yml (compose layer only)",
            description=[
                "Writes <arr_config_base_dir>/<service>/docker-compose.yml for one service, without",
                "ever starting, restarting or recreating the container as a side effect. Covers",
                "services that share the linuxserver-image / PUID-PGID-TZ / config+media bind",
                "mount / Traefik-label shape (Sonarr, Radarr, Prowlarr, SABnzbd, each optionally",
                "with an exportarr sidecar; Jellyfin, Jellyseerr, qBittorrent with a VPN sibling",
                "passed through arr_service.raw_extra_services, and the like).",
                "An app with its own database (Jellystat) is stack_arr_jellystat's job.",
            ],
            version_added="1.0",
            author=DEFAULT_AUTHOR,
            options={
                "arr_service_name": OptionSpec(
                    type="str",
                    required=True,
                    description=[
                        "Service key — the <arr_config_base_dir>/<name> directory and the default",
                        "container_name. Keep it the service's proper name even when its existing",
                        "on-host directory is named differently; arr_service.compose_path_override",
                        "names that directory.",
                    ],
                ),
                "arr_service": OptionSpec(
                    type="dict",
                    required=True,
                    description=["Per-service definition. See README for the full field list."],
                    options={
                        "image": OptionSpec(
                            type="str",
                            required=True,
                            description=[
                                "Fully pinned image reference (tag or digest). Never 'latest'."
                            ],
                        ),
                        "container_name": OptionSpec(
                            type="str",
                            required=False,
                            description=["Defaults to arr_service_name if omitted."],
                        ),
                        "compose_path_override": OptionSpec(
                            type="str",
                            required=False,
                            description=[
                                "Existing on-host directory name, ONLY when it differs from",
                                "arr_service_name (for example a directory created with a typo). The",
                                "compose file is written there, so the role reconciles the directory",
                                "in use instead of creating a second one beside it. Container and",
                                "sidecar names still use arr_service_name.",
                            ],
                        ),
                        "command": OptionSpec(type="list", elements="str", required=False),
                        "config_volume": OptionSpec(
                            type="dict",
                            required=False,
                            description=[
                                "Host path + container path + optional mode for the config bind mount."
                            ],
                            options={
                                "host_path": OptionSpec(
                                    type="str", required=False, default="./config"
                                ),
                                "container_path": OptionSpec(
                                    type="str", required=False, default="/config"
                                ),
                                "mode": OptionSpec(type="str", required=False),
                            },
                        ),
                        "extra_volumes": OptionSpec(
                            type="list",
                            elements="str",
                            required=False,
                            description=[
                                "Additional raw `host:container[:mode]` bind mounts (media data, docker.sock, etc)."
                            ],
                        ),
                        "environment": OptionSpec(
                            type="dict",
                            required=False,
                            description=[
                                "Extra/overriding environment beyond the common PUID/PGID/UMASK/TZ set."
                            ],
                        ),
                        "devices": OptionSpec(type="list", elements="str", required=False),
                        "group_add": OptionSpec(type="list", elements="str", required=False),
                        "network_mode": OptionSpec(
                            type="str",
                            required=False,
                            description=[
                                "e.g. 'service:gluetun' for qbittorrent. Mutually exclusive with joining arr_network in compose."
                            ],
                        ),
                        "traefik": OptionSpec(
                            type="dict",
                            required=False,
                            description=["Traefik label inputs."],
                            options={
                                "router_name": OptionSpec(type="str", required=False),
                                "service_port": OptionSpec(type="int", required=False),
                                "hosts": OptionSpec(type="list", elements="str", required=False),
                                "entrypoints": OptionSpec(
                                    type="str", required=False, default="websecure"
                                ),
                                "extra_labels": OptionSpec(type="dict", required=False),
                            },
                        ),
                        "exportarr": OptionSpec(
                            type="dict",
                            required=False,
                            description=[
                                "Optional exportarr sidecar (sonarr/radarr/prowlarr/sabnzbd)."
                            ],
                            options={
                                "image": OptionSpec(type="str", required=True),
                                "arg": OptionSpec(
                                    type="str", required=True, description=["e.g. 'sonarr'."]
                                ),
                                "port": OptionSpec(type="int", required=True),
                                "target_url": OptionSpec(type="str", required=True),
                                "api_key": OptionSpec(
                                    type="str",
                                    required=True,
                                    description=[
                                        "Already-RESOLVED API key value. Resolve it in the CALLING PLAY (for",
                                        "example richard_lynch.common.onepassword_cached) and pass the plain",
                                        "value down; this role never calls a secret lookup itself, which keeps",
                                        "it testable offline in molecule.",
                                    ],
                                ),
                                "api_key_env_var": OptionSpec(
                                    type="str", required=False, default="API_KEY"
                                ),
                            },
                        ),
                        "raw_extra_services": OptionSpec(
                            type="list",
                            elements="dict",
                            required=False,
                            description=[
                                "Additional raw compose service blocks merged in verbatim (name + full",
                                "service dict) for one-off siblings that don't fit the linuxserver mold",
                                "— e.g. a VPN client container a torrent client shares the network of.",
                                "Rendered as-is via to_nice_yaml.",
                            ],
                        ),
                    },
                ),
                "arr_network": OptionSpec(
                    type="str",
                    required=False,
                    default="traefik",
                    description=["Shared external docker network name the services join."],
                ),
                "arr_exportarr_bind_address": OptionSpec(
                    type="str",
                    required=False,
                    default="127.0.0.1",
                    description=[
                        "Host address the exportarr sidecar's metrics port is published on. exportarr",
                        "serves its metrics without authentication, so the default keeps it on loopback;",
                        "set the address a remote scraper reaches, or an empty string for every",
                        "interface (a Docker-published port is not filtered by the host firewall).",
                    ],
                ),
                "arr_config_base_dir": OptionSpec(
                    type="str",
                    required=False,
                    default="/opt",
                    description=[
                        "Parent directory; the role writes /opt/<arr_service_name>/docker-compose.yml."
                    ],
                ),
                "arr_common_puid": OptionSpec(type="int", required=False, default=1000),
                "arr_common_pgid": OptionSpec(type="int", required=False, default=1000),
                "arr_common_umask": OptionSpec(type="str", required=False, default="002"),
                "arr_common_tz": OptionSpec(type="str", required=False, default="Etc/UTC"),
            },
        ),
    }
)
