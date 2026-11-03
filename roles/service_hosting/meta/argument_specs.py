# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

DEFAULT_AUTHOR = "Collection maintainers"
from ansible_devkit.argspec.models import ArgumentSpec, ArgumentSpecs, OptionSpec

# NEVER put literal Jinja delimiters in a description. Ansible TEMPLATES the argspec, so a
# documentation mention of a Jinja tag is parsed as real Jinja and fails the whole role at
# validate_argument_spec time, before any task runs.

# Shared by both entrypoints: the tier table is estate configuration (THE THIRD RULE), so it is
# REQUIRED with no default -- a role default for "which suffix, which port" would be estate config
# wearing a default's clothing.
_TIERS = OptionSpec(
    type="dict",
    required=True,
    description=[
        "The tiers this hosting guest serves, keyed by tier name (dev, prod).",
        "Each tier declares: suffix (the hostname suffix, apps are name.suffix), entrypoint_port",
        "(the Traefik entrypoint port cloudflared sends that tier to), network (the docker",
        "network only that tier's apps join), bridge (a STABLE linux bridge name for it, at most 15",
        "characters, so the firewall can name it), root (where that tier's apps live on the guest),",
        "and the 1Password item names of the tier's Access service token. Optional: reap (true:",
        "apps expire by ttl_hours) and secrets (true: an app may declare platform secrets, 1Password",
        "items delivered as 0400 files; only for a tier whose definitions are owner-merged).",
        "Installation configuration: set by the caller (inventory), never defaulted here.",
    ],
)

# Shared by main (the firewall) and deploy_app (the compose): both must agree which app a grant is for.
_EGRESS_ALLOW = OptionSpec(
    type="list",
    elements="dict",
    required=True,
    description=[
        "Exceptions to the container egress block, each a GRANT TO ONE CONTAINER: app, service (the",
        "compose service granted), subnet (at least a /29, for the app's own network), address (the",
        "service's pinned address, in the lower half of subnet, past the gateway), cidr and ports",
        "(where it may go), and optionally tier (default prod) and proto (default tcp). The app's",
        "default network goes on a stable bridge named hst-a- plus the app name with that subnet,",
        "the service is pinned to address, and the firewall allows the grant from that bridge AND",
        "that source only, so no other container holds it. An incomplete grant is refused. DNS to",
        "the guest's own resolvers is always allowed.",
        "REQUIRED with no default (estate configuration; an empty list grants nothing). Being",
        "required also keeps the caller's value on a repeat call of the role in one play: the",
        "deploy play runs assert_target first, and a defaulted option is rebound to its default",
        "on the second call, which would silently drop every grant from the deployed compose.",
    ],
)

argument_specs = ArgumentSpecs(
    argument_specs={
        "main": ArgumentSpec(
            short_description="Traefik (docker provider) and the per-tier networks for app hosting",
            description=[
                "Converges the hosting platform on a Docker guest: a read-only docker-socket-proxy,",
                "Traefik with the docker provider and one entrypoint per tier, and one docker",
                "network per tier. Apps are NOT deployed here; the deploy_app entrypoint does that,",
                "and a converge of this entrypoint never removes an app.",
                "Routing is by container labels written by the deploy_app entrypoint, so publishing",
                "an app needs no DNS, tunnel or Cloudflare step once the wildcard edge exists.",
            ],
            version_added="1.0",
            author=DEFAULT_AUTHOR,
            options={
                "hosting_tiers": _TIERS,
                "hosting_root": OptionSpec(
                    type="str",
                    default="/opt/hosting",
                    description=["Host directory holding the platform's own compose project."],
                ),
                "hosting_traefik_image": OptionSpec(
                    type="str",
                    default="traefik:v3.6.25",
                    description=[
                        "Traefik image. A floating tag (latest and friends) is refused. Must be 3.6.1 or newer:",
                        "v3.3 speaks Docker API 1.24 and a current Docker Engine (minimum API 1.40) rejects it,",
                        "so the docker provider never starts and every Host answers 404.",
                    ],
                ),
                "hosting_socket_proxy_image": OptionSpec(
                    type="str",
                    default="tecnativa/docker-socket-proxy:0.3.0",
                    description=[
                        "Image of the read-only Docker API proxy Traefik talks to instead of the",
                        "real socket. A floating tag is refused.",
                    ],
                ),
                "hosting_log_level": OptionSpec(
                    type="str",
                    default="INFO",
                    choices=["DEBUG", "INFO", "WARN", "ERROR"],
                    description=["Traefik log level."],
                ),
                "hosting_traefik_memory": OptionSpec(
                    type="str",
                    default="256m",
                    description=["Memory limit for each tier's Traefik container."],
                ),
                "hosting_ingress_sources": OptionSpec(
                    type="list",
                    elements="str",
                    required=True,
                    description=[
                        "Addresses allowed to open NEW connections to a published tier port: the",
                        "host that runs cloudflared. Everything else is dropped, because Docker's",
                        "published ports otherwise bypass every other rule and would let any LAN",
                        "host reach an app with no Cloudflare Access in the path.",
                        "Installation configuration: set by the caller (inventory).",
                    ],
                ),
                "hosting_blocked_egress_cidrs": OptionSpec(
                    type="list",
                    elements="str",
                    default=[
                        "10.0.0.0/8",
                        "192.168.0.0/16",
                        "169.254.0.0/16",
                        "100.64.0.0/10",
                    ],
                    description=[
                        "Destination ranges a container may not open connections to: the private",
                        "ranges, link-local and carrier-grade NAT (which includes a tailnet).",
                        "Internet egress stays open. Docker's own 172.16/12 pool is not listed so",
                        "containers of one app can still talk to each other.",
                    ],
                ),
                "hosting_reaper_interval": OptionSpec(
                    type="str",
                    default="15min",
                    description=[
                        "How often the reaper timer runs (a systemd time span). A tier declared",
                        "with reap true has its apps removed once their ttl_hours have passed.",
                    ],
                ),
                "hosting_reaped_root": OptionSpec(
                    type="str",
                    default="/srv/apps/.reaped",
                    description=[
                        "Where a removed app's directory is moved (tier/name-stamp). Named volumes",
                        "are kept: neither the reaper nor remove_app deletes an app's data.",
                    ],
                ),
                "hosting_textfile_dir": OptionSpec(
                    type="str",
                    default="/var/lib/node_exporter/textfile_collector",
                    description=[
                        "The node exporter textfile directory the reaper writes its metrics to.",
                        "Must match the directory the host's metrics agent collects.",
                    ],
                ),
                "hosting_egress_allow": _EGRESS_ALLOW,
                "hosting_registries": OptionSpec(
                    type="list",
                    elements="dict",
                    default=[],
                    description=[
                        "Private registries the guest logs in to before anything is pulled, so an app",
                        "may run an image that is not public. Each entry: host (the registry, e.g.",
                        "ghcr.io), username, item and field (the 1Password item holding a pull-only",
                        "token, and its field), vault, and probe_image: a PRIVATE image on that",
                        "registry, pinned, that the converge proves it can read with the login and",
                        "cannot read without it. The credential is stored in the connecting user's",
                        "docker config, 0600 in a 0700 directory.",
                        "Installation configuration: set by the caller (inventory).",
                    ],
                    options={
                        "host": OptionSpec(
                            type="str",
                            required=True,
                            description=["Registry host, no scheme."],
                        ),
                        "username": OptionSpec(
                            type="str",
                            required=True,
                            description=["Registry user name."],
                        ),
                        "item": OptionSpec(
                            type="str",
                            required=True,
                            description=["1Password item holding the token."],
                        ),
                        "field": OptionSpec(
                            type="str",
                            default="password",
                            description=["Field of the item that holds the token."],
                        ),
                        "vault": OptionSpec(
                            type="str",
                            required=True,
                            description=["1Password vault holding the item."],
                        ),
                        "probe_image": OptionSpec(
                            type="str",
                            required=True,
                            description=[
                                "A private image on this registry, pinned by tag or digest,",
                                "that the login must make readable.",
                            ],
                        ),
                    },
                ),
            },
        ),
        "assert_merged": ArgumentSpec(
            short_description="Refuse unless an app definition is exactly what is on the merge ref",
            description=[
                "The PROD gate. A PROD app may only be deployed when its definition is byte-for-byte",
                "(as parsed YAML) what exists on hosting_merge_ref, i.e. what was reviewed and merged.",
                "Fetches the ref, reads the file the definition names as its source, and refuses if the",
                "file is absent there, is not the conventional path for the app, or differs from the",
                "definition being deployed. A local edit, an unmerged branch and a stale checkout all",
                "fail the comparison.",
            ],
            version_added="1.0",
            author=DEFAULT_AUTHOR,
            options={
                "hosting_app": OptionSpec(
                    type="dict",
                    required=True,
                    description=["The app definition being deployed; must carry source."],
                ),
                "hosting_repo_root": OptionSpec(
                    type="str",
                    required=True,
                    description=["Path of the git checkout the definition belongs to."],
                ),
                "hosting_merge_ref": OptionSpec(
                    type="str",
                    default="origin/main",
                    description=["Remote ref that means merged: remote/branch."],
                ),
                "hosting_source_prefix": OptionSpec(
                    type="str",
                    required=True,
                    description=[
                        "Directory in the repo where reviewed app definitions live, with a",
                        "trailing slash: a PROD definition must carry source <prefix><name>/vars.yml.",
                    ],
                ),
            },
        ),
        "assert_target": ArgumentSpec(
            short_description="Refuse a deploy aimed at any host other than the hosting guest",
            description=[
                "The TARGET gate of the deploy and remove plays. Refuses, naming every reason at once,",
                "when the run was retargeted: a host limit (--limit / LIMIT=) of any kind, or a target",
                "extra var naming a different host, or a hosting guest absent from the inventory.",
                "A limit is refused outright because each shape of it defeats a different gate: a",
                "limit to the guest skips the controller-side PROD merge gate, a limit to localhost",
                "skips the deploy and exits green, and a limit to another host skips every play.",
                "Run it from a play whose hosts line is all, so no limit can skip the gate itself.",
            ],
            version_added="1.1",
            author=DEFAULT_AUTHOR,
            options={
                "hosting_target_guest": OptionSpec(
                    type="str",
                    required=True,
                    description=["The one inventory host a deploy may touch (the hosting guest)."],
                ),
                "hosting_inventory_hosts": OptionSpec(
                    type="list",
                    elements="str",
                    required=True,
                    description=["Every host in the inventory (the caller passes groups all)."],
                ),
                "hosting_requested_limit": OptionSpec(
                    type="str",
                    default="",
                    description=[
                        "The run's host limit, empty when none (the caller passes ansible_limit)."
                    ],
                ),
                "hosting_requested_target": OptionSpec(
                    type="str",
                    default="",
                    description=["A target extra var if one was passed, empty when none."],
                ),
            },
        ),
        "deploy_app": ArgumentSpec(
            short_description="Validate an app definition and converge it onto its tier",
            description=[
                "Takes one app definition (name, tier, port, routed service and a compose project),",
                "refuses it with every reason if it breaks the gate (privileged, host networking, a",
                "socket mount, a host bind outside the app's own data directory, a published port,",
                "a routing label, an image off the allowlist or not pinned), writes the compose with",
                "Traefik labels the PLATFORM generates, brings it up and reads the app back through",
                "Traefik on the guest.",
                "Generate-don't-apply: it validates and reports unless hosting_deploy_apply is true.",
            ],
            version_added="1.0",
            author=DEFAULT_AUTHOR,
            options={
                "hosting_tiers": _TIERS,
                "hosting_egress_allow": _EGRESS_ALLOW,
                "hosting_app": OptionSpec(
                    type="dict",
                    required=True,
                    description=[
                        "The app definition: name, tier, port (the container port Traefik targets),",
                        "service (which compose service is routed), compose (a compose project as",
                        "a mapping with a services key), and optionally health_path, expect_status",
                        "and ttl_hours.",
                    ],
                ),
                "hosting_image_allowlist": OptionSpec(
                    type="list",
                    elements="str",
                    required=True,
                    description=[
                        "Image reference prefixes an app may run. Installation configuration:",
                        "extended by a reviewed change to the caller's inventory, never by the app.",
                    ],
                ),
                "hosting_secrets_vault": OptionSpec(
                    type="str",
                    description=[
                        "The 1Password vault the app's platform secrets are read from (passed as",
                        "vault= to richard_lynch.common.onepassword_cached). Needed only by an app",
                        "that declares secrets; such an app is refused before anything is written",
                        "when this is unset.",
                    ],
                ),
                "hosting_deploy_apply": OptionSpec(
                    type="bool",
                    default=False,
                    description=[
                        "Generate-don't-apply. False validates and reports the compose that would",
                        "be written; true writes it, brings it up and reads it back.",
                    ],
                ),
                "hosting_default_mem_limit": OptionSpec(
                    type="str",
                    default="512m",
                    description=[
                        "Memory limit injected into any service that declares none, so one app",
                        "cannot take all of the host's memory.",
                    ],
                ),
                "hosting_max_mem_limit": OptionSpec(
                    type="str",
                    default="1g",
                    description=[
                        "The largest memory limit any one service may declare. A service that",
                        "asks for more is refused: the injected default is a default, and without",
                        "a ceiling one app could take the whole guest.",
                    ],
                ),
                "hosting_max_cpus": OptionSpec(
                    type="float",
                    default=2.0,
                    description=["The largest CPU limit any one service may declare."],
                ),
                "hosting_default_cpus": OptionSpec(
                    type="float",
                    default=1.0,
                    description=["CPU limit injected into any service that declares none."],
                ),
                "hosting_readback_timeout_s": OptionSpec(
                    type="int",
                    default=60,
                    min_value=5,
                    max_value=600,
                    description=[
                        "How long to wait for the app to answer through Traefik on the guest.",
                    ],
                ),
            },
        ),
        "remove_app": ArgumentSpec(
            short_description="Remove one app from a reaping (DEV) tier and prove it gone",
            description=[
                "Runs the reaper's explicit removal for one app: compose down, the daemon read back",
                "for any container left, the directory moved under hosting_reaped_root, and Traefik",
                "asked for the app's Host (404 when the router is gone). Refuses a tier that is not",
                "declared with reap true, so a PROD app cannot be removed here.",
                "Generate-don't-apply: previews unless hosting_remove_confirm is true.",
            ],
            version_added="1.0",
            author=DEFAULT_AUTHOR,
            options={
                "hosting_tiers": _TIERS,
                "hosting_app": OptionSpec(
                    type="dict",
                    required=True,
                    description=["The app definition; only its name and tier are read."],
                ),
                "hosting_remove_confirm": OptionSpec(
                    type="bool",
                    default=False,
                    description=["False previews the removal; true removes the app."],
                ),
            },
        ),
    }
)
