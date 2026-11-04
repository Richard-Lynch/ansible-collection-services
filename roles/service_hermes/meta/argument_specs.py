# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

DEFAULT_AUTHOR = "Collection maintainers"
from ansible_devkit.argspec.models import ArgumentSpec, ArgumentSpecs, OptionSpec

# NEVER put literal Jinja delimiters in a description. Ansible TEMPLATES the argspec, so a
# documentation mention of a Jinja tag is parsed as real Jinja and fails the whole role at
# validate_argument_spec time, before any task runs. (Same note as service_litellm.)

argument_specs = ArgumentSpecs(
    argument_specs={
        "main": ArgumentSpec(
            short_description="Deploy the Hermes Agent gateway and its web dashboard",
            description=[
                "Deploys Nous Research's Hermes Agent as a single container: the supervised",
                "gateway plus its built-in web dashboard, with all mutable state on a host",
                "bind-mount so the image stays stateless and upgrades by changing the tag.",
                "Hermes normally configures itself through an INTERACTIVE wizard that writes",
                "its .env. This role renders that .env from inventory and your secret store instead,",
                "which is what makes the deploy unattended and safe to run twice -- the",
                "container's own cont-init hook only SEEDS a missing .env, so a managed file is",
                "never clobbered.",
                "The dashboard FAILS CLOSED on a non-loopback bind with no auth provider, so",
                "basic-auth credentials are required rather than optional: upstream removed the",
                "insecure escape hatch because unauthenticated dashboards were being abused to",
                "run commands on the host.",
            ],
            version_added="1.0",
            author=DEFAULT_AUTHOR,
            options={
                # ---- identity / placement -------------------------------------------------
                "hermes_container_name": OptionSpec(
                    type="str",
                    required=False,
                    default="hermes",
                    description=["Hermes container name."],
                ),
                "hermes_config_dir": OptionSpec(
                    type="str",
                    required=False,
                    default="/opt/hermes",
                    description=[
                        "Host directory holding the rendered compose project.",
                        "The agent's own state lives in a data/ subdirectory of this path.",
                    ],
                ),
                "hermes_network": OptionSpec(
                    type="str",
                    required=True,
                    description=[
                        "Name of the pre-existing external docker network to attach to.",
                    ],
                ),
                # THE PIN IS A PAIR, and both halves are required with NO default: which version
                # a deployment runs is the caller's configuration, and a role default would make
                # a caller who forgot to declare one run somebody else's version. The compose
                # file uses `image: repo:tag@digest`, so a retagged image cannot run -- the
                # digest is the proof, the tag is the label.
                "hermes_image_repo": OptionSpec(
                    type="str",
                    required=False,
                    default="nousresearch/hermes-agent",
                    description=[
                        "Registry repository of the published image (no tag, no digest).",
                        "NOT the upstream repo's own compose file, which declares a local",
                        "`build: .` tag that resolves to nothing in any registry.",
                    ],
                ),
                "hermes_image_tag": OptionSpec(
                    type="str",
                    required=True,
                    description=[
                        "Release tag, e.g. v0.21.6. A floating tag (latest, stable, main, edge,",
                        "canary) is refused: on this project they point at a different digest",
                        "than the newest versioned release.",
                    ],
                ),
                "hermes_image_digest": OptionSpec(
                    type="str",
                    required=True,
                    description=[
                        "Content digest of the tag, sha256: followed by 64 hex characters (the",
                        "multi-arch index digest). Refused when absent or malformed: a tag with",
                        "no digest can be retagged under a running deployment.",
                    ],
                ),
                # The uid/gid the image's internal `hermes` user runs as. Config written into
                # the state directory must be owned by it, not by root, or the agent cannot read
                # its own configuration and silently falls back to seeded defaults. These are
                # IMAGE facts (right for any caller of this image), so they belong here rather
                # than in inventory -- but they are options rather than hardcoded so a future
                # image that changes them needs no code change.
                "hermes_state_uid": OptionSpec(
                    type="int",
                    required=False,
                    default=10000,
                    description=[
                        "Numeric uid the container's hermes user runs as, and therefore the",
                        "owner of config written into its state directory.",
                    ],
                ),
                "hermes_state_gid": OptionSpec(
                    type="int",
                    required=False,
                    default=10000,
                    description=["Numeric gid matching hermes_state_uid."],
                ),
                "hermes_timezone": OptionSpec(
                    type="str",
                    required=False,
                    default="Etc/UTC",
                    description=["TZ passed to the container."],
                ),
                # ---- ports ----------------------------------------------------------------
                # Both default to upstream's own numbers so a caller that changes nothing gets
                # the documented behaviour.
                "hermes_api_host_port": OptionSpec(
                    type="int",
                    required=False,
                    # Upstream's documented port for the gateway API server, on both sides of
                    # the publish. Right for any caller, so it belongs here.
                    default=8642,
                    min_value=1,
                    max_value=65535,
                    description=[
                        "Host port published for the gateway's OpenAI-compatible API server.",
                    ],
                ),
                "hermes_api_bind_address": OptionSpec(
                    type="str",
                    required=False,
                    default="127.0.0.1",
                    description=[
                        "Guest address the API server's port is published on. Loopback by default: a",
                        "Docker-published port bypasses the host firewall, and this listener",
                        "accepts credentials. Set the address a reverse proxy reaches it on, or",
                        "an empty string for every guest interface (right only for a guest whose",
                        "NICs are all internal and behind its own firewall).",
                    ],
                ),
                "hermes_dashboard_bind_address": OptionSpec(
                    type="str",
                    required=False,
                    default="127.0.0.1",
                    description=[
                        "Guest address the dashboard's port is published on. Loopback by default: a",
                        "Docker-published port bypasses the host firewall, and this listener",
                        "accepts credentials. Set the address a reverse proxy reaches it on, or",
                        "an empty string for every guest interface (right only for a guest whose",
                        "NICs are all internal and behind its own firewall).",
                    ],
                ),
                "hermes_dashboard_host_port": OptionSpec(
                    type="int",
                    required=False,
                    default=9119,
                    min_value=1,
                    max_value=65535,
                    description=["Host port published for the web dashboard."],
                ),
                # ---- model provider: an OpenAI-compatible gateway -------------------------
                # Hermes reaches a gateway through a `providers.<name>` entry in its own
                # config.yaml, NOT through an environment variable: there is no generic chat
                # OPENAI_BASE_URL in this project. Verified against upstream's
                # configuring-models.md rather than assumed.
                "hermes_gateway_url": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    description=[
                        "OpenAI-compatible base URL of the gateway all model traffic routes",
                        "through, including the /v1 suffix. The address of a gateway is the",
                        "caller's to set, so there is no default -- empty means render no provider",
                        "entry.",
                    ],
                ),
                "hermes_gateway_api_key": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    no_log=True,
                    description=[
                        "Key presented to the gateway. On a LiteLLM gateway this must be a",
                        "scoped VIRTUAL key, never the master key -- the master key is also the",
                        "gateway's admin-UI password.",
                    ],
                ),
                "hermes_gateway_key_env": OptionSpec(
                    type="str",
                    required=False,
                    default="HERMES_LITELLM_API_KEY",
                    description=[
                        "Name of the environment variable holding the gateway key. config.yaml",
                        "carries only this NAME (providers.<name>.key_env); the value is rendered",
                        "into app.env (0600) and is never in config.yaml.",
                    ],
                ),
                "hermes_gateway_provider_name": OptionSpec(
                    type="str",
                    required=False,
                    default="gateway",
                    description=[
                        "Name the provider entry is registered under, and the value of",
                        "model.provider. Any name works -- the entry defines the endpoint.",
                    ],
                ),
                "hermes_gateway_session_affinity_header": OptionSpec(
                    type="str",
                    required=False,
                    # LiteLLM's own header name. Right for any caller fronted by LiteLLM, and
                    # harmless to a gateway that ignores it; empty disables it entirely.
                    default="x-litellm-session-id",
                    description=[
                        "Name of the header carrying Hermes's conversation id upstream, so a",
                        "session-aware gateway can correlate an agent loop instead of treating",
                        "every turn as a new conversation and re-sending the whole history.",
                        "Empty sends no session identifier at all.",
                    ],
                ),
                "hermes_gateway_discover_models": OptionSpec(
                    type="bool",
                    required=False,
                    default=True,
                    description=[
                        "Whether to query the gateway's own model listing. False uses only the",
                        "declared ids, which suits a gateway whose listing is incomplete.",
                    ],
                ),
                "hermes_gateway_models": OptionSpec(
                    type="list",
                    elements="str",
                    required=False,
                    default=[],
                    description=[
                        "Model ids to declare on the provider entry. Empty relies on discovery.",
                        "Which models to surface is the caller's configuration.",
                    ],
                ),
                "hermes_tool_loop_hard_stop": OptionSpec(
                    type="bool",
                    required=False,
                    default=True,
                    description=[
                        "Hard-stop a runaway tool loop in unattended sessions. True is both",
                        "upstream's default and the right one for an agent that spends money per",
                        "request -- a runaway loop should stop rather than warn.",
                    ],
                ),
                "hermes_default_model": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    description=[
                        "Model id written to the agent's config as its default. Note upstream no",
                        "longer reads a model from .env, so this is rendered into config.yaml.",
                    ],
                ),
                # ---- dashboard auth -------------------------------------------------------
                "hermes_dashboard_enabled": OptionSpec(
                    type="bool",
                    required=False,
                    default=True,
                    description=[
                        "Enable the supervised dashboard service. It is registered but stays",
                        "down unless this is true.",
                    ],
                ),
                # ---- the Webapp (Hermes Desktop in a browser) ---------------------------
                # Served by the GATEWAY on its own listener (gateway/webapp_listener.py; absent
                # from v0.22.5 and earlier releases). config.yaml's `webapp:`
                # block turns it on; a wildcard bind or a public_url engages the DASHBOARD's
                # sign-in providers, so it rides on hermes_dashboard_* auth and has none of its own.
                "hermes_webapp_enabled": OptionSpec(
                    type="bool",
                    required=False,
                    default=False,
                    description=[
                        "Serve the Webapp from the gateway (config.yaml webapp.enabled), bound",
                        "0.0.0.0 and published on hermes_webapp_host_port. Requires the dashboard's",
                        "basic auth (hermes_dashboard_enabled + a password): upstream refuses a",
                        "non-loopback Webapp with no sign-in provider. Needs an image that ships",
                        "gateway/webapp_listener.py.",
                    ],
                ),
                "hermes_webapp_bind_address": OptionSpec(
                    type="str",
                    required=False,
                    default="127.0.0.1",
                    description=[
                        "Guest address the Webapp's port is published on. Loopback by default: a",
                        "Docker-published port bypasses the host firewall, and this listener",
                        "accepts credentials. Set the address a reverse proxy reaches it on, or",
                        "an empty string for every guest interface (right only for a guest whose",
                        "NICs are all internal and behind its own firewall).",
                    ],
                ),
                "hermes_webapp_host_port": OptionSpec(
                    type="int",
                    required=False,
                    default=9120,
                    min_value=1,
                    max_value=65535,
                    description=[
                        "The Webapp listener's port, inside the container and published on the",
                        "host (upstream's default, 9120).",
                    ],
                ),
                "hermes_webapp_public_url": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    description=[
                        "webapp.public_url: the https:// address a reverse proxy serves the Webapp",
                        "at. Upstream accepts requests for that one outside hostname and builds",
                        "sign-in callbacks on it. Empty renders no public_url.",
                    ],
                ),
                # ---- dashboard theme ------------------------------------------------------
                "hermes_dashboard_theme_enabled": OptionSpec(
                    type="bool",
                    required=False,
                    default=True,
                    description=[
                        "Deliver the role's dashboard theme (templates/dashboard-theme.yaml.j2)",
                        "to data/dashboard-themes/<hermes_dashboard_theme_name>.yaml, where the",
                        "dashboard discovers it as a selectable theme. The FILE is the role's",
                        "(rewritten on drift); which theme is ACTIVE is the user's choice in the",
                        "dashboard, see hermes_dashboard_theme_seed.",
                    ],
                ),
                "hermes_dashboard_theme_name": OptionSpec(
                    type="str",
                    required=False,
                    default="readable",
                    min_length=1,
                    description=[
                        "Name (selection key) of the delivered theme, and its file name under",
                        "data/dashboard-themes/. Its label is the name capitalised.",
                    ],
                ),
                "hermes_dashboard_theme_seed": OptionSpec(
                    type="str",
                    required=False,
                    default="readable",
                    description=[
                        "Theme name written to dashboard.theme in config.yaml ONLY when that key",
                        "is absent. A theme chosen in the dashboard UI (which writes the same key)",
                        "is never overwritten, so this is a first-boot default, not a pin. Empty",
                        "seeds nothing.",
                    ],
                ),
                "hermes_dashboard_username": OptionSpec(
                    type="str",
                    required=False,
                    # A username is not a secret and "admin" is right for any caller; the
                    # PASSWORD is what must come from the caller's secret store.
                    default="admin",
                    description=["Dashboard basic-auth username."],
                ),
                "hermes_dashboard_password": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    no_log=True,
                    description=[
                        "Dashboard basic-auth password. Required whenever the dashboard is",
                        "enabled: the bind is non-loopback so Hermes refuses to start the",
                        "dashboard without an auth provider.",
                    ],
                ),
                "hermes_dashboard_auth_secret": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    no_log=True,
                    description=[
                        "Session-signing secret, so dashboard logins survive a restart. Without",
                        "it every container restart invalidates every session.",
                    ],
                ),
                # ---- observability read ----------------------------------------------------
                "hermes_observability_prometheus_url": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    description=[
                        "Prometheus base URL the agent queries (PROMETHEUS_URL in app.env).",
                        "Blank = not rendered. Read-only by construction: Prometheus has no",
                        "write path on its query API.",
                    ],
                ),
                "hermes_observability_loki_url": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    description=[
                        "Loki base URL the agent queries (LOKI_URL in app.env). Blank = not",
                        "rendered.",
                    ],
                ),
                "hermes_observability_grafana_url": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    description=[
                        "Grafana base URL (GRAFANA_URL in app.env). Rendered only together with",
                        "the token below; a URL with no token would be a half-configured read.",
                    ],
                ),
                "hermes_observability_grafana_token": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    no_log=True,
                    description=[
                        "Token of a Grafana VIEWER service account (GRAFANA_TOKEN in app.env).",
                        "Never an admin token.",
                    ],
                ),
                # ---- Slack Socket Mode -------------------------------------------------
                "hermes_slack_enabled": OptionSpec(
                    type="bool",
                    required=False,
                    default=False,
                    description=[
                        "Connect the gateway to Slack over Socket Mode (outbound WebSocket, no",
                        "inbound port). Off by default: a chat platform is an inbound path into",
                        "an agent that executes tools, so it is the caller's decision.",
                    ],
                ),
                "hermes_slack_bot_token": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    no_log=True,
                    description=["Bot User OAuth token, xoxb- (SLACK_BOT_TOKEN in app.env)."],
                ),
                "hermes_slack_app_token": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    no_log=True,
                    description=[
                        "App-level token with connections:write, xapp- (SLACK_APP_TOKEN).",
                    ],
                ),
                "hermes_slack_allow_from": OptionSpec(
                    type="list",
                    elements="str",
                    required=False,
                    default=[],
                    description=[
                        "Slack member ids (U...) allowed to talk to the agent",
                        "(SLACK_ALLOWED_USERS). Required non-empty when Slack is enabled:",
                        "upstream denies everyone with no allowlist, and an empty list here",
                        "would read as configured while answering nobody.",
                    ],
                ),
                "hermes_slack_home_channel": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    description=[
                        "Channel or DM id for scheduled deliveries (SLACK_HOME_CHANNEL).",
                        "Blank = not rendered.",
                    ],
                ),
                # ---- config.yaml ownership ---------------------------------------------
                "hermes_config_owned": OptionSpec(
                    type="list",
                    elements="dict",
                    required=False,
                    default=[],
                    description=[
                        "Leaves of the agent's config.yaml the caller owns, each a dict of key",
                        "(dotted path, or a list of segments when a segment contains a dot),",
                        "value (a scalar or a list; never a mapping), tier (A-range, B or C)",
                        "and allowed (required for A-range: a list of permitted values or a",
                        "min/max mapping). Converge MERGES these onto the live file and carries",
                        "every other key through untouched; an A-range leaf is reset only when",
                        "it is outside allowed. Secret-free by construction: credentials are",
                        "never listed here. The role also owns the provider and model wiring it",
                        "renders itself; listing the same leaf here is refused.",
                    ],
                ),
                # ---- the toolset floor ---------------------------------------------------
                "hermes_toolsets_core": OptionSpec(
                    type="list",
                    elements="str",
                    required=False,
                    default=[],
                    description=[
                        "The toolset names a platform enables BY DEFAULT at the pinned image,",
                        "read from the daemon's own /v1/toolsets (not from `hermes tools list`,",
                        "which reports the CLI platform: some toolsets it lists as enabled are",
                        "disabled on api_server). It is a fact about the pinned tag, so it is",
                        "declared with the pin and re-read on every bump. Empty with a non-empty",
                        "subtract map is refused: nothing could be subtracted from it.",
                    ],
                ),
                "hermes_toolsets_subtract": OptionSpec(
                    type="dict",
                    required=False,
                    default={},
                    description=[
                        "Per platform, the toolsets to REMOVE from the core set; the remainder",
                        "becomes the tier-C leaf platform_toolsets.<platform>. Every subtracted",
                        "name must exist in the core set: a name that matches nothing would",
                        "subtract nothing and leave that toolset enabled, so it is refused.",
                    ],
                ),
                "hermes_api_server_default_floor": OptionSpec(
                    type="list",
                    elements="str",
                    required=False,
                    default=[
                        "terminal",
                        "file",
                        "code_execution",
                        "browser",
                        "delegation",
                        "cronjob",
                    ],
                    description=[
                        "The toolsets removed from the api_server platform when the API server is on",
                        "and hermes_toolsets_subtract carries no api_server entry. Non-empty on",
                        "purpose: a caller that forgets to declare a floor gets the safe one, not the",
                        "full set (these are shell-equivalent for anyone holding the API key).",
                    ],
                ),
                "hermes_api_server_full_control": OptionSpec(
                    type="bool",
                    required=False,
                    default=False,
                    description=[
                        "Explicit opt-out of the api_server floor: when true the API platform keeps",
                        "every toolset it enables (nothing removed), for a deployment that controls",
                        "access at the edge (for example an access proxy in front of the API key).",
                        "False by default, so an absent flag is the floor, never a silent",
                        "fail-open. The subtract mechanism stays the tool for partitioning access",
                        "per agent.",
                    ],
                ),
                # ---- caller skills, delivered read-only ---------------------------------
                "hermes_skills_source": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    description=[
                        "Controller-side directory of caller-authored skills (one subdirectory",
                        "per skill, each holding a SKILL.md). Copied root-owned 0444 and mounted",
                        "READ-ONLY into the container, because skill_manage has no external-dir",
                        "check at the pin: the mount is the only protection. Empty delivers none.",
                    ],
                ),
                "hermes_skills_mount_path": OptionSpec(
                    type="str",
                    required=False,
                    default="/opt/hermes-skills",
                    description=[
                        "Container path of the read-only skills mount, also the single",
                        "entry the role owns in skills.external_dirs.",
                    ],
                ),
                # ---- uid remap + managed scope enforcement --------------------------------
                "hermes_uid": OptionSpec(
                    type="int",
                    required=False,
                    default=10000,
                    description=[
                        "HERMES_UID for the image's root bootstrap, which remaps its hermes user and",
                        "drops to it (upstream's supported path; compose `user:` is not). 10000 is",
                        "the image's own hermes uid, so no file changes owner. HERMES_GID is",
                        "hermes_state_gid, and the role refuses a hermes_state_uid that differs from",
                        "this, since the files it chowns and the process that reads them must agree.",
                    ],
                ),
                "hermes_managed_enforce": OptionSpec(
                    type="bool",
                    required=False,
                    default=False,
                    description=[
                        "Enforce the managed scope: mount it, pin HERMES_MANAGED_DIR, and make",
                        "the policy preflight the container's main program, so the gateway does",
                        "not start unless the preflight passes. Off stages the files only.",
                    ],
                ),
                "hermes_raw_read_prefixes": OptionSpec(
                    type="list",
                    elements="str",
                    required=False,
                    default=[
                        "skills",
                        "plugins",
                        "memory",
                        "security.website_blocklist",
                    ],
                    description=[
                        "Config key prefixes the pinned Hermes image reads RAW from config.yaml,",
                        "bypassing the managed overlay (v0.22.5: agent/skill_utils, pm/plugins_state,",
                        "tools/website_policy). A tier-C leaf under one must be declared",
                        "layer: user, since save_config strips managed leaves from the file;",
                        "the managed document refuses it otherwise. Re-read on every image bump.",
                    ],
                ),
                "hermes_managed_mount_dir": OptionSpec(
                    type="str",
                    required=False,
                    default="/etc/hermes-managed",
                    description=[
                        "Container directory the managed scope is mounted at (read-only). Not",
                        "/etc/hermes: a mount there hides files the image keeps in it.",
                    ],
                ),
                "hermes_preflight_textfile_dir": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    description=[
                        "Host directory the node exporter's textfile collector reads. The",
                        "preflight writes hermes_preflight_ok there and the role installs the",
                        "minute timer that writes hermes_gateway_healthy. Empty exports nothing.",
                    ],
                ),
                # ---- Kanban -> Slack card threads ------------------------------------------
                "hermes_kanban_notify": OptionSpec(
                    type="dict",
                    required=False,
                    default={},
                    description=[
                        "Kanban -> Slack card threads: a guest timer that tails the board's",
                        "task_events and posts one thread per card through `hermes send`.",
                        "Keys: default_channel (id, required when set), channels",
                        "(assignee -> channel id), assignees (profiles whose cards are on by",
                        "default), events (the default event set), ping (events that mention",
                        "owner_user_id), console_url_template / attach_url_template",
                        "({card_id} replaced), interval_seconds (default 30). Empty = not",
                        "installed; a card's own `notify:` / `ping me on` lines override it.",
                    ],
                ),
                "hermes_config_retired_keys": OptionSpec(
                    type="list",
                    elements="str",
                    required=False,
                    default=[],
                    description=[
                        "Dotted config.yaml leaves the converge REMOVES. The only way the role",
                        "ever deletes a key it does not own.",
                    ],
                ),
                # ---- automations: the webhook listener --------------------------------------
                "hermes_webhook_enabled": OptionSpec(
                    type="bool",
                    required=False,
                    default=False,
                    description=[
                        "Run the gateway's webhook listener and publish its port. Off by",
                        "default: a route turns an HTTP POST into a Slack message or an agent",
                        "run, so it exists only where the caller decided to have one.",
                    ],
                ),
                "hermes_webhook_port": OptionSpec(
                    type="int",
                    required=False,
                    default=8644,
                    description=["Listener port, the same inside the container and on the guest."],
                ),
                "hermes_webhook_bind_address": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    description=[
                        "Guest address the port is published on. Empty publishes on every",
                        "guest interface, which is right for a guest whose only NIC is on an",
                        "internal network behind its own firewall; set an address on a guest with",
                        "a public interface, since a Docker-published port bypasses the host",
                        "firewall.",
                    ],
                ),
                "hermes_webhook_secret": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    no_log=True,
                    description=[
                        "Global HMAC secret every static route falls back to (WEBHOOK_SECRET in",
                        "app.env, never config.yaml). At least 32 characters when the listener",
                        "is enabled; upstream refuses to start a route with no secret at all.",
                    ],
                ),
                "hermes_webhook_routes": OptionSpec(
                    type="dict",
                    required=False,
                    default={},
                    description=[
                        "Static routes, name to route (upstream's platforms.webhook.extra.routes",
                        "schema). Rendered as TIER-C owned leaves, so neither the agent nor the",
                        "dashboard can widen a route's toolsets or repoint its delivery. A route",
                        "may not carry `secret`: it uses the global one, or `secret_env` names",
                        "an app.env variable the route reads as an env reference.",
                    ],
                ),
                "hermes_webhook_subscriptions_readonly": OptionSpec(
                    type="bool",
                    required=False,
                    # ON is the safe state: upstream honours a hand-written `toolsets` on a
                    # dynamic route, so a writable store is a self-grant of terminal.
                    default=True,
                    description=[
                        "With the listener on, mount a root-owned, empty webhook_subscriptions.json",
                        "READ-ONLY over the agent's copy, so no tool (and no `hermes webhook",
                        "subscribe`) can create or widen a dynamic route; every route is then a",
                        "static, tier-C one. The converge refuses while the agent's file still",
                        "holds entries (move them into hermes_webhook_routes first). False keeps",
                        "dynamic routes, with the send gate and the converge's toolsets repair as",
                        "the only controls.",
                    ],
                ),
                "hermes_webhook_retired_routes": OptionSpec(
                    type="list",
                    elements="str",
                    required=False,
                    default=[],
                    description=[
                        "Route names the converge removes from config.yaml. A route dropped from",
                        "hermes_webhook_routes alone would stay live: the merge keeps every key",
                        "it does not own.",
                    ],
                ),
                "hermes_webhook_route_secrets": OptionSpec(
                    type="dict",
                    required=False,
                    default={},
                    no_log=True,
                    description=[
                        "Per-route secrets, app.env variable name to value, for the routes",
                        "whose secret_env names them (a sender that holds its own secret, such",
                        "as Home Assistant's rest_command).",
                    ],
                ),
                # ---- automations: seeded cron jobs and their scripts -----------------------
                "hermes_cron_seed": OptionSpec(
                    type="list",
                    elements="dict",
                    required=False,
                    default=[],
                    description=[
                        "Seeded cron jobs (name, schedule, prompt or script, no_agent, deliver,",
                        "model, skills, enabled). Each is created PAUSED when absent and edited",
                        "back when its owned fields drift; enabling one is a separate, explicit",
                        "step, never the converge. Jobs whose name is not here are personal and",
                        "are never touched.",
                    ],
                ),
                "hermes_cron_paused_reason": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    description=[
                        "The --paused-reason a seeded job is created with. Empty uses the",
                        "filter's own generic text (hermes_cron_argv).",
                    ],
                ),
                "hermes_cron_scripts_source": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    description=[
                        "Controller-side directory of the caller's cron scripts, staged root-owned",
                        "0644 in <hermes_config_dir>/estate-scripts (outside the agent's tree)",
                        "and mounted read-only at the agent's scripts/estate/, so the agent can",
                        "run them and not rewrite them. Empty delivers none.",
                    ],
                ),
                # The Kanban PR completion contract shells out to `gh api` INSIDE the gateway
                # container, and the published image ships no gh: the call fails and upstream
                # classes it "infra" ("GitHub acceptance evidence unavailable or incomplete"), so
                # no contract card can complete. Which release runs is the caller's, hence no
                # default.
                "hermes_gh_cli_version": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    description=[
                        "GitHub CLI release (e.g. 2.103.3) provided to the gateway container",
                        "read-only at /usr/local/bin/gh, for the Kanban PR completion",
                        "contract's evidence reads. Staged root-owned under",
                        "<hermes_config_dir>/tools (outside the agent's tree) from the",
                        "official release tarball, verified by hermes_gh_cli_sha256. The",
                        "credential is NOT here: gh presents the assignee profile's own",
                        "GH_TOKEN. Empty provides none.",
                    ],
                ),
                "hermes_gh_cli_sha256": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    description=[
                        "sha256 of gh_<version>_linux_<arch>.tar.gz, from the release's",
                        "checksums file. Required when hermes_gh_cli_version is set.",
                    ],
                ),
                "hermes_gh_cli_arch": OptionSpec(
                    type="str",
                    required=False,
                    default="amd64",
                    choices=["amd64", "arm64"],
                    description=["Release architecture of the gh tarball (the guest's)."],
                ),
                "hermes_cron_scripts_config": OptionSpec(
                    type="dict",
                    required=False,
                    default={},
                    description=[
                        "Non-secret settings for the cron scripts, rendered to",
                        "estate-scripts/automations.json, seen by the agent as",
                        "scripts/estate/automations.json (a cron script does not inherit the",
                        "gateway's environment). Never a credential: those are env names.",
                    ],
                ),
                "hermes_cron_env_passthrough": OptionSpec(
                    type="list",
                    elements="str",
                    required=False,
                    default=[],
                    description=[
                        "app.env variable names passed to cron scripts (and the agent's",
                        "terminal) through terminal.env_passthrough, a tier-C leaf.",
                    ],
                ),
                "hermes_cron_env": OptionSpec(
                    type="dict",
                    required=False,
                    default={},
                    no_log=True,
                    description=[
                        "Values for the passthrough variables, written to app.env (0600).",
                    ],
                ),
                # ---- the email adapter: the agent's own mailbox -----------------------------
                "hermes_email_enabled": OptionSpec(
                    type="bool",
                    required=False,
                    default=False,
                    description=[
                        "Run the gateway's email adapter on the agent's OWN mailbox (IMAP poll +",
                        "SMTP reply). Mail from an allowed sender becomes a chat turn. Never a",
                        "person's own mailbox: the agent gets a dedicated one.",
                    ],
                ),
                "hermes_email_address": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    description=["The agent's address."],
                ),
                "hermes_email_password": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    no_log=True,
                    description=["The mailbox's app password (EMAIL_PASSWORD in app.env)."],
                ),
                "hermes_email_imap_host": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    description=["IMAP host of the mailbox."],
                ),
                "hermes_email_smtp_host": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    description=["SMTP host of the mailbox."],
                ),
                "hermes_email_imap_port": OptionSpec(
                    type="int",
                    required=False,
                    default=993,
                    description=["IMAP port (SSL)."],
                ),
                "hermes_email_smtp_port": OptionSpec(
                    type="int",
                    required=False,
                    default=587,
                    description=["SMTP port (STARTTLS)."],
                ),
                "hermes_email_allowed_users": OptionSpec(
                    type="list",
                    elements="str",
                    required=False,
                    default=[],
                    description=[
                        "Senders whose mail becomes a turn. Required non-empty when enabled:",
                        "open access would let anyone on the internet drive the agent.",
                    ],
                ),
                "hermes_email_authserv_id": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    description=[
                        "The receiving server's authserv-id (the token before the first ; of the",
                        "topmost Authentication-Results header). Upstream DROPS every message",
                        "needing an authenticated From when it is unset, so it is required.",
                    ],
                ),
                # ---- the send gate and protected paths ------------------------------------
                "hermes_hub_gate_enabled": OptionSpec(
                    type="bool",
                    required=False,
                    default=False,
                    description=[
                        "Register the gate's pre_tool_call shell hook (scripts/hub_gate.py) with",
                        "fail_closed: an outbound email to a recipient not on the allowlist is",
                        "escalated for approval, and a write to a protected path is",
                        "refused. Needs hermes_managed_enforce: the hook and its config live in",
                        "the read-only policy mount.",
                    ],
                ),
                "hermes_hub_gate_allowed_recipients": OptionSpec(
                    type="list",
                    elements="str",
                    required=False,
                    default=[],
                    description=[
                        "Addresses a send may go to without an approval. Empty",
                        "means every send asks.",
                    ],
                ),
                "hermes_hub_gate_protected_paths": OptionSpec(
                    type="list",
                    elements="str",
                    required=False,
                    default=["webhook_subscriptions.json"],
                    description=[
                        "File names (fnmatch) the agent's write tools and terminal may not",
                        "write. The dynamic-subscription store is the reason this exists.",
                    ],
                ),
                "hermes_hub_gate_matcher": OptionSpec(
                    type="str",
                    required=False,
                    default="terminal|process_manage|execute_code|write_file|patch|send_message|.*mail.*",
                    description=[
                        "Regex of tool names the gate sees. A tool outside it is never gated,",
                        "so a new send-capable tool must be added here.",
                    ],
                ),
                # ---- API server -----------------------------------------------------------
                "hermes_api_server_enabled": OptionSpec(
                    type="bool",
                    required=False,
                    # OFF is the safe state you get without thinking about it.
                    default=False,
                    description=[
                        "Expose the gateway's OpenAI-compatible API server. OFF by default: it",
                        "lets any caller holding its key drive the agent, which executes tools.",
                    ],
                ),
                "hermes_api_server_key": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    no_log=True,
                    description=[
                        "Bearer key for the API server. Upstream enforces a minimum of 8",
                        "characters; this role gates on 32 to keep a guessable key out.",
                    ],
                ),
                # ---- policy hook and settings service -------------------------------------
                "hermes_policy_hook_enabled": OptionSpec(
                    type="bool",
                    required=False,
                    default=False,
                    description=[
                        "Register the gateway pre_tool_call policy hook (fail_closed) for every",
                        "profile, as a tier-C leaf in the managed scope. Needs",
                        "hermes_managed_enforce (the hook script lives in the read-only policy",
                        "mount) and hooks_auto_accept true among the owned leaves.",
                    ],
                ),
                "hermes_policy_hook_timeout": OptionSpec(
                    type="int",
                    required=False,
                    default=10,
                    min_value=1,
                    max_value=60,
                    description=[
                        "Seconds the hook may run per tool call. A timeout BLOCKS the call",
                        "(fail_closed), so this bounds the cost of a hung hook.",
                    ],
                ),
                "hermes_audit_hook_enabled": OptionSpec(
                    type="bool",
                    required=False,
                    default=False,
                    description=[
                        "Register the gateway post_tool_call AUDIT hook for every profile, as",
                        "a tier-C leaf in the managed scope: one value-free JSON line per tool call",
                        "(tool, profile, session and call ids, duration, outcome; never arguments,",
                        "results or error text) appended to logs/tool-audit.jsonl under the data",
                        "volume. Shipping that file to Loki is the calling play's log source. Needs",
                        "hermes_managed_enforce and hooks_auto_accept, like the policy hook.",
                    ],
                ),
                "hermes_audit_hook_timeout": OptionSpec(
                    type="int",
                    required=False,
                    default=5,
                    min_value=1,
                    max_value=60,
                    description=[
                        "Seconds the audit hook may run per tool call. post_tool_call is an",
                        "observer event, so a timeout costs one audit line, never the call.",
                    ],
                ),
                "hermes_settings_service_enabled": OptionSpec(
                    type="bool",
                    required=False,
                    default=False,
                    description=[
                        "Run the settings service (typed settings operations over MCP, its own",
                        "policy check) as a sidecar of the Hermes container, from the same image,",
                        "so writes go through upstream's own hermes config set.",
                    ],
                ),
                "hermes_settings_service_src": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    description=[
                        "Directory on the controller that holds the settings service's Python",
                        "package (a hermes_settings/ directory with __init__.py, __main__.py,",
                        "backends.py, catalogue.py, mcp_http.py, policy.py and service.py). The",
                        "service is the caller's own application, so the role ships no copy of it.",
                        "Required when hermes_settings_service_enabled is true.",
                    ],
                ),
                "hermes_settings_service_container_name": OptionSpec(
                    type="str",
                    required=False,
                    default="hermes-settings",
                    min_length=1,
                    description="Container (and docker-network DNS) name of the settings service.",
                ),
                "hermes_settings_service_port": OptionSpec(
                    type="int",
                    required=False,
                    default=8650,
                    min_value=1024,
                    max_value=65535,
                    description=[
                        "Port the settings service listens on, inside the docker network and",
                        "published on the guest's LOOPBACK only (never another interface).",
                    ],
                ),
                "hermes_settings_service_token": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    no_log=True,
                    description=[
                        "Bearer token clients present to the settings service (at least 32",
                        "characters). Written to a 0400 file mounted into the sidecar only.",
                    ],
                ),
                "hermes_settings_github_token": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    no_log=True,
                    description=[
                        "GitHub token the service opens tier-B pull requests with (contents and",
                        "pull requests on hermes_settings_github_repo). Empty: tier B answers",
                        "pr-unavailable and changes nothing live.",
                    ],
                ),
                "hermes_settings_github_repo": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    description="owner/name of the configuration repository tier-B PRs are opened on.",
                ),
                "hermes_settings_profiles": OptionSpec(
                    type="list",
                    elements="str",
                    required=False,
                    default=["default"],
                    description=[
                        "Profile names the settings service may read and write (default plus the",
                        "named profiles). A name not in this list is refused.",
                    ],
                ),
                "hermes_hooks_expected": OptionSpec(
                    type="list",
                    elements="str",
                    required=False,
                    default=[],
                    description=[
                        "Hook commands that MUST be registered. Non-empty: the health timer also",
                        "writes hermes_hooks_expected and hermes_hooks_registered (from hermes hooks",
                        "list), which a hooks-missing alert reads. Declared separately from",
                        "hermes_policy_hook_enabled on purpose, so a run that drops the hook still",
                        "expects it and the alert fires. Each entry is the WHOLE command as",
                        "hermes hooks list prints it (the role registers the image's interpreter, -I",
                        "-B, then the script).",
                    ],
                ),
            },
        ),
    }
)
