# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

DEFAULT_AUTHOR = "Collection maintainers"
from ansible_devkit.argspec.models import ArgumentSpec, ArgumentSpecs, OptionSpec

# NEVER put literal Jinja delimiters in a description. Ansible TEMPLATES the argspec, so a
# documentation mention of a Jinja tag is parsed as real Jinja and fails the whole role before any
# task runs. MCP env references are therefore described as a dollar sign plus a braced name.

argument_specs = ArgumentSpecs(
    argument_specs={
        "main": ArgumentSpec(
            short_description="Coding worker VM: one sysbox dev container per card, agents inside it",
            description=[
                "Turns a VM into a coding worker for an agent hub: Docker with the sysbox runtime, git,",
                "tmux, the Claude Code and Codex CLIs routed through a LiteLLM gateway with the",
                "worker's own virtual key, repo checkouts, worker-sessiond (the session supervisor:",
                "a worktree and a sysbox container per card, the agent running INSIDE the container",
                "under a tmux session named after the card; list, pause, resume, stop, done, the",
                "GitHub PR feedback loop and the CI gate), and ttyd for jump-in from a browser.",
                "It needs a real kernel (a VM, not an LXC guest): sysbox is what lets each card run",
                "its own inner dockerd without --privileged.",
                "Which host, user, repos, models and listeners are installation configuration and",
                "come from inventory; the role knows how, not which. No credential is written into a",
                "tracked or world-readable file: the agent environment is a 0600 file owned by the",
                "worker user.",
            ],
            version_added="1.0",
            author=DEFAULT_AUTHOR,
            options={
                # ---- identity ---------------------------------------------------------------
                "worker_user": OptionSpec(
                    type="str",
                    required=True,
                    description=[
                        "Unprivileged account the agents, tmux, sessiond and ttyd run as.",
                        "Created if absent. Its home directory is derived, never hardcoded.",
                    ],
                ),
                "worker_git_user_name": OptionSpec(
                    type="str",
                    required=True,
                    description=["git user.name for commits the agents make."],
                ),
                "worker_git_user_email": OptionSpec(
                    type="str",
                    required=True,
                    description=["git user.email for commits the agents make."],
                ),
                "worker_ssh_authorized_key": OptionSpec(
                    type="str",
                    required=True,
                    description=[
                        "Public key allowed to log in as the worker user. This is the jump-in path",
                        "from a laptop (ssh then tmux attach) and the key the coder profile uses.",
                    ],
                ),
                # ---- tools and repos --------------------------------------------------------
                "worker_tools": OptionSpec(
                    type="dict",
                    required=True,
                    description=[
                        "Pinned versions. Keys: claude_code_version, codex_version, node_version and",
                        "node_sha256_linux_x64 (the official tarball is verified against it);",
                        "gh_version and gh_sha256_linux_amd64 (the cli/cli release tarball; the",
                        "distro gh is too old for sessiond's PR loop); uv_version and",
                        "uv_sha256_linux_x64 (the astral-sh/uv release tarball baked into the",
                        "default card image, so a card can build the repo's venv); with worker_acp_enabled",
                        "also acpx_version, claude_agent_acp_version and codex_acp_version (the",
                        "ACP client and the two adapters protocol mode runs).",
                        "Floating tags are refused: the deployed agent would depend on WHEN the",
                        "play ran.",
                    ],
                ),
                "worker_repo_owner": OptionSpec(
                    type="str",
                    required=True,
                    description=["GitHub owner namespace allowed for cards."],
                ),
                "worker_repos_exclude": OptionSpec(
                    type="list",
                    elements="str",
                    required=True,
                    description=["Repositories excluded from the namespace."],
                ),
                "worker_repo_base_branches": OptionSpec(
                    type="dict",
                    default={},
                    description=["Optional repository base branch overrides."],
                ),
                "worker_github_host": OptionSpec(
                    type="str",
                    default="github.com",
                    description=["GitHub host for repository URLs."],
                ),
                "worker_work_root": OptionSpec(
                    type="str",
                    default="/srv/work",
                    description=[
                        "Repos live at work_root/repos/name; worktrees at work_root/worktrees/card."
                    ],
                ),
                # ---- model route ------------------------------------------------------------
                "worker_litellm_url": OptionSpec(
                    type="str",
                    required=True,
                    description=[
                        "LiteLLM base URL WITHOUT a /v1 suffix. Claude Code appends /v1/messages;",
                        "Codex gets the /v1 form from this value.",
                    ],
                ),
                "worker_litellm_api_key": OptionSpec(
                    type="str",
                    required=True,
                    no_log=True,
                    description=[
                        "The worker's own LiteLLM virtual key (its spend is its own line)."
                    ],
                ),
                "worker_claude_model": OptionSpec(
                    type="str",
                    required=True,
                    description=["Model alias Claude Code asks LiteLLM for."],
                ),
                "worker_codex_model": OptionSpec(
                    type="str",
                    required=True,
                    description=["Model alias Codex asks LiteLLM for."],
                ),
                "worker_codex_approval_policy": OptionSpec(
                    type="str",
                    default="on-request",
                    choices=["untrusted", "on-failure", "on-request", "never"],
                    description=[
                        "Codex approval_policy for card sessions. A card has no person at its",
                        "terminal, so anything but never can stop a turn on a prompt nobody",
                        "answers; never returns a refused action to the agent instead.",
                    ],
                ),
                "worker_codex_sandbox_mode": OptionSpec(
                    type="str",
                    default="workspace-write",
                    choices=["read-only", "workspace-write", "danger-full-access"],
                    description=[
                        "Codex sandbox_mode inside the card container. workspace-write keeps",
                        "the clone's .git read-only and turns network off, so a card that must",
                        "commit and push its own branch under approval_policy never cannot do",
                        "either. danger-full-access drops Codex's own sandbox and leaves the",
                        "card container (and the card's scoped GitHub token) as the boundary,",
                        "which is the boundary Claude Code runs under here too.",
                    ],
                ),
                "worker_codex_trust_card_worktree": OptionSpec(
                    type="bool",
                    default=False,
                    description=[
                        "Mark each card's clone as a trusted Codex project in that card's",
                        "config.toml. Codex matches trust on the exact path, and an untrusted",
                        "project runs read-only whatever the sandbox and approval options say",
                        "(the ACP adapter falls back to read-only, the interactive client asks",
                        "first). Trust also loads the repository's own .codex/config.toml.",
                    ],
                ),
                "worker_codex_reasoning_effort": OptionSpec(
                    type="str",
                    default="",
                    choices=["", "minimal", "low", "medium", "high", "xhigh"],
                    description=[
                        "Codex model_reasoning_effort. Empty leaves the line out, so the",
                        "model's own default applies.",
                    ],
                ),
                "worker_codex_stream_idle_timeout_ms": OptionSpec(
                    type="int",
                    default=7200000,
                    description=[
                        "How long Codex waits on a silent response stream from the gateway. A",
                        "long agent turn can idle for minutes behind a proxy, and Codex's own",
                        "default ends it early.",
                    ],
                ),
                "worker_codex_stream_max_retries": OptionSpec(
                    type="int",
                    default=5,
                    description=["Codex stream_max_retries for the gateway provider."],
                ),
                "worker_codex_request_max_retries": OptionSpec(
                    type="int",
                    default=4,
                    description=["Codex request_max_retries for the gateway provider."],
                ),
                "worker_claude_permission_mode": OptionSpec(
                    type="str",
                    default="acceptEdits",
                    choices=["default", "acceptEdits", "dontAsk"],
                    description=[
                        "Claude Code permission mode for card sessions. dontAsk DENIES anything",
                        "worker_claude_settings does not allow, where the other two PROMPT, and",
                        "a prompt in an unattended card waits for nobody. Bypass modes are not",
                        "offered: widening what an unattended agent may do is a reviewed",
                        "configuration change, not a role knob.",
                    ],
                ),
                "worker_claude_settings": OptionSpec(
                    type="dict",
                    default={},
                    description=[
                        "Claude Code settings sessiond writes per card as the container's MANAGED",
                        "settings (/etc/claude-code, mounted read-only), OVERWRITTEN at each",
                        "launch and resume. The",
                        "permissions allow and deny lists are the card's Bash allowlist; the",
                        "literal text {card_branch} in a rule becomes that card's branch. Empty",
                        "writes nothing. A bypass mode, or an allow rule admitting every Bash",
                        "command, is refused by the role and again by sessiond.",
                    ],
                ),
                "worker_subscription_login": OptionSpec(
                    type="bool",
                    default=False,
                    description=[
                        "Must stay false. The worker authenticates with its LiteLLM key only;",
                        "the role refuses true so a personal login cannot become a dependency.",
                    ],
                ),
                "worker_github_token": OptionSpec(
                    type="str",
                    default="",
                    no_log=True,
                    description=["Legacy GitHub token, used only without a broker."],
                ),
                "worker_token_broker_url": OptionSpec(
                    type="str",
                    default="",
                    description=["Token broker URL; empty selects legacy mode."],
                ),
                "worker_token_broker_bearer": OptionSpec(
                    type="str",
                    default="",
                    no_log=True,
                    description=["Bearer credential for the token broker."],
                ),
                "worker_extra_env": OptionSpec(
                    type="dict",
                    default={},
                    no_log=True,
                    description=[
                        "Extra NAME to value pairs for the agent environment: the credentials MCP",
                        "catalogue entries refer to. Names are upper-case with underscores.",
                    ],
                ),
                # ---- MCP --------------------------------------------------------------------
                "worker_mcp_catalogue": OptionSpec(
                    type="list",
                    elements="dict",
                    default=[],
                    description=[
                        "The REVIEWED MCP servers a card may ask for. Each item: name, transport",
                        "(http or stdio), reviewed_by, and url plus optional headers for http, or",
                        "command plus optional args and env for stdio. Header and env values must",
                        "be a dollar-brace reference such as a braced TOKEN name, never a literal.",
                        "A card requesting a name not listed here refuses to start.",
                    ],
                ),
                "worker_mcp_default_enabled": OptionSpec(
                    type="list",
                    elements="str",
                    default=[],
                    description=["Catalogue names every card gets without asking."],
                ),
                # ---- listeners --------------------------------------------------------------
                "worker_sessiond_bind": OptionSpec(
                    type="str",
                    default="127.0.0.1",
                    description=[
                        "Address worker-sessiond listens on. Loopback is right for any caller;",
                        "a caller on another guest sets a wider bind in inventory and relies on",
                        "the bearer token plus the guest firewall.",
                    ],
                ),
                "worker_sessiond_port": OptionSpec(
                    type="int",
                    default=7690,
                    min_value=1024,
                    max_value=65535,
                    description=["worker-sessiond port."],
                ),
                "worker_sessiond_token": OptionSpec(
                    type="str",
                    required=True,
                    no_log=True,
                    min_length=16,
                    description=["Bearer token every sessiond call except /healthz must present."],
                ),
                "worker_ttyd_bind": OptionSpec(
                    type="str",
                    default="127.0.0.1",
                    description=[
                        "Interface NAME or address ttyd listens on (ttyd accepts an interface name,",
                        "so a DHCP change needs no edit). Loopback by default.",
                    ],
                ),
                "worker_ttyd_port": OptionSpec(
                    type="int",
                    default=7681,
                    min_value=1024,
                    max_value=65535,
                    description=["ttyd port."],
                ),
                "worker_ttyd_title": OptionSpec(
                    type="str",
                    default="coding-worker",
                    pattern=r"\A[A-Za-z0-9._-]+\Z",
                    description=["Browser window title ttyd shows (its titleFixed client option)."],
                ),
                "worker_ttyd_credential": OptionSpec(
                    type="str",
                    required=True,
                    no_log=True,
                    min_length=12,
                    pattern=r"\A[^:]+:.+\Z",
                    description=[
                        "user:password for ttyd's own basic auth: the ORIGIN gate on the worker's",
                        "LAN interface. A human never types it: the reverse proxy in front of ttyd",
                        "adds it to each request after its own authentication passes, so it stops",
                        "only a direct connection to ttyd. It appears in the ttyd command line, so",
                        "it is not a secret from the agents.",
                    ],
                ),
                "worker_tmux_session_prefix": OptionSpec(
                    type="str",
                    default="card-",
                    description=["tmux sessions are named prefix plus the card id."],
                ),
                "worker_card_tail_enabled": OptionSpec(
                    type="bool",
                    default=False,
                    description=[
                        "Whether sessiond serves scrubbed pane text to the card. Leave it off until",
                        "the credential scrubber's canaries have passed on the target.",
                    ],
                ),
                "worker_extra_authorized_keys": OptionSpec(
                    type="list",
                    elements="str",
                    default=[],
                    description=[
                        "More public keys allowed to log in as the worker user, one entry each. The",
                        "gateway's coder profile reaches the worker over ssh with its OWN key, so it",
                        "is listed here and the human jump-in key stays a separate decision.",
                    ],
                ),
                "worker_coder_ctl_src": OptionSpec(
                    type="str",
                    default="",
                    description=[
                        "Controller path of the coder skill's coder_ctl.py, installed as",
                        "/usr/local/bin/coder_ctl. The gateway's ssh terminal backend copies nothing",
                        "to the worker, so the skill's own script is not here unless this installs it.",
                        "Empty installs nothing.",
                    ],
                ),
                "worker_ssh_accept_env": OptionSpec(
                    type="list",
                    elements="str",
                    default=[],
                    description=[
                        "Environment variable NAMES sshd accepts from a login (AcceptEnv), each of the",
                        "form WORKER_ plus upper-case letters, digits and underscores. The coder",
                        "profile forwards WORKER_SESSIOND_URL and _TOKEN this way (terminal.",
                        "env_passthrough), so the supervisor's token is never a file on the worker.",
                        "Empty removes the drop-in.",
                    ],
                ),
                "worker_metrics_textfile": OptionSpec(
                    type="str",
                    default="/var/lib/node_exporter/textfile_collector/hermes_worker.prom",
                    description=[
                        "File the root-run worker-metrics timer writes sessiond's stuck-card metrics to",
                        "(textfile-collector format). Must sit in the directory the guest's node exporter",
                        "textfile collector reads. An empty string installs no timer.",
                    ],
                ),
                "worker_stuck_after_seconds": OptionSpec(
                    type="int",
                    default=1800,
                    description=[
                        "A running card whose tmux pane has produced no output for longer than this",
                        "counts in hermes_worker_stuck_cards. It also describes an agent waiting at a",
                        "prompt, which is the other thing the alert is for.",
                    ],
                ),
                # ---- containers --------------------------------------------------------------
                "worker_container_runtime": OptionSpec(
                    type="str",
                    default="sysbox-runc",
                    choices=["sysbox-runc"],
                    description=[
                        "Docker runtime every card container is created with, read back after the",
                        "start. Only sysbox-runc is accepted: runc would give the agent the VM's",
                        "kernel namespaces and no inner dockerd without --privileged.",
                    ],
                ),
                "worker_sysbox": OptionSpec(
                    type="dict",
                    required=True,
                    description=[
                        "Pinned sysbox-ce package. Keys: version (x.y.z) and sha256_linux_amd64 (64",
                        "hex, from the release's own checksum list). Installed from the project's",
                        "release .deb; the role refuses another CPU and an unchecked download.",
                    ],
                ),
                "worker_card_cpus": OptionSpec(
                    type="float",
                    default=2,
                    description=["CPU limit of each card container (docker --cpus)."],
                ),
                "worker_card_memory": OptionSpec(
                    type="str",
                    default="2g",
                    description=[
                        "Memory limit of each card container, like 2g or 512m. It is also the swap",
                        "limit, so it is the whole budget. sessiond refuses to start a card whose",
                        "limit does not fit beside the running ones in the VM's memory.",
                    ],
                ),
                "worker_vm_reserve_mb": OptionSpec(
                    type="int",
                    default=1024,
                    description=[
                        "Memory the VM keeps for itself (sessiond, dockerd, tmux, ttyd, page cache)",
                        "when sessiond decides whether another card container fits.",
                    ],
                ),
                "worker_card_dns": OptionSpec(
                    type="list",
                    elements="str",
                    default=[],
                    description=[
                        "Nameservers for the INNER dockerd of the default card image (its",
                        "/etc/docker/daemon.json `dns`). A card sits on its own bridge, so its",
                        "resolv.conf names docker's embedded 127.0.0.11; an inner dockerd that sees",
                        "only a loopback nameserver gives nested containers docker's public",
                        "fallback resolvers, which a firewall that allows only local DNS drops.",
                        "IP literals, never loopback. Empty writes no daemon.json.",
                    ],
                ),
                "worker_card_image_name": OptionSpec(
                    type="str",
                    default="coding-worker-card",
                    pattern=r"\A[a-z0-9][a-z0-9._/-]*\Z",
                    description=[
                        "Repository name of the default card image. The tag is always the first 12",
                        "hex digits of the rendered Dockerfile's sha256, so a changed Dockerfile is",
                        "a new image and an unchanged one is never rebuilt.",
                    ],
                ),
                "worker_docker_bip": OptionSpec(
                    type="str",
                    default="",
                    description=[
                        "The VM dockerd's bridge address in CIDR form (daemon.json `bip`), for a",
                        "network whose addresses would collide with docker's default bridge. Empty",
                        "leaves docker's own default.",
                    ],
                ),
                "worker_docker_address_pools": OptionSpec(
                    type="list",
                    elements="dict",
                    default=[],
                    description=[
                        "The VM dockerd's address pools for user-defined networks (daemon.json",
                        "`default-address-pools`), each item base (a CIDR) and size (a prefix",
                        "length). Every card gets its own network, so a pool of small subnets",
                        "keeps many cards from exhausting the defaults. Empty leaves docker's own.",
                    ],
                ),
                "worker_card_base_image": OptionSpec(
                    type="str",
                    default="ubuntu:24.04",
                    description=[
                        "Base of the default card image (git, gh, Docker, build tools). Used when a",
                        "repo has no .devcontainer; the agent CLIs are mounted in from the VM, not",
                        "baked in, so a version bump never rebuilds the image.",
                    ],
                ),
                "worker_pr_allow_no_checks": OptionSpec(
                    type="bool",
                    default=False,
                    description=[
                        "Whether the PR gate may mark ready a PR with NO checks at all. False: a gate",
                        "that passes on nothing is a gate for a repo that forgot to configure CI.",
                    ],
                ),
                "worker_pr_reviewer_logins": OptionSpec(
                    type="list",
                    elements="str",
                    default=[],
                    description=[
                        "GitHub logins whose PR comments reach the agent besides the repo owner and",
                        "anyone GitHub marks OWNER, MEMBER or COLLABORATOR. Everyone else is dropped:",
                        "review text becomes keystrokes in a credentialed agent.",
                    ],
                ),
                "worker_pr_poll_seconds": OptionSpec(
                    type="int",
                    default=300,
                    min_value=0,
                    max_value=86400,
                    description=[
                        "How often worker-sessiond polls the open PR of every running or paused card",
                        "and types new TRUSTED review feedback into that card's session, resuming a",
                        "paused card first (a stopped one is never woken). 0 turns sessiond's own",
                        "poller off and leaves polling to the coder skill's heartbeat. About five",
                        "GitHub API calls per card per poll, against the worker's PAT.",
                    ],
                ),
                # ---- protocol mode: ACP sessions under acpx, and acpx-mcp ---------------------
                "worker_acp_enabled": OptionSpec(
                    type="bool",
                    default=False,
                    description=[
                        "Protocol mode: install the pinned acpx CLI and the Claude/Codex ACP",
                        "adapters into the read-only tools directory every card container mounts,",
                        "turn on worker-sessiond's ACP endpoints (session_new, prompt, a CONFIRMED",
                        "cancel, list, resume) and run acpx-mcp, the MCP server that exposes",
                        "exactly those to an MCP client. Off by default: it is a second way for an",
                        "unattended agent to run.",
                    ],
                ),
                "worker_acp_permissions": OptionSpec(
                    type="str",
                    default="approve-reads",
                    choices=["approve-reads", "approve-all", "deny-all"],
                    description=[
                        "acpx permission mode for protocol sessions, which have no person at a",
                        "prompt (requests acpx cannot approve are DENIED, never left waiting).",
                        "approve-reads lets the agent read and nothing else; approve-all lets it",
                        "edit and run commands inside its sysbox container. Widening it is a",
                        "reviewed inventory change.",
                    ],
                ),
                "worker_acp_cancel_budget_seconds": OptionSpec(
                    type="float",
                    default=4.0,
                    description=[
                        "How long a protocol cancel may spend confirming a COOPERATIVE stop (the",
                        "turn settled and no tool left running) before it kills the card's",
                        "container instead. Kept under 5 s so the whole cancel, kill included,",
                        "returns within 5 s.",
                    ],
                ),
                "worker_acp_keep_comms": OptionSpec(
                    type="list",
                    elements="str",
                    default=[],
                    description=[
                        "Extra process names (comm) a cancel's sweep treats as the agent's own",
                        "rather than as a tool left running, e.g. a stdio MCP server's binary.",
                    ],
                ),
                "worker_acpx_mcp_bind": OptionSpec(
                    type="str",
                    default="127.0.0.1",
                    description=[
                        "Address acpx-mcp listens on. A client on another guest (the agent) needs a",
                        "wider bind in inventory; the bearer token and the guest firewall gate it.",
                    ],
                ),
                "worker_acpx_mcp_port": OptionSpec(
                    type="int",
                    default=7691,
                    min_value=1024,
                    max_value=65535,
                    description=["acpx-mcp port (MCP streamable HTTP at /mcp)."],
                ),
                "worker_acpx_mcp_token": OptionSpec(
                    type="str",
                    default="",
                    no_log=True,
                    description=[
                        "Bearer token an MCP client presents to acpx-mcp. Required (16+",
                        "characters, different from the sessiond token) when protocol mode is on:",
                        "a client holding it can drive ACP sessions and nothing else sessiond does.",
                    ],
                ),
                "worker_pr_ignore_bots": OptionSpec(
                    type="bool",
                    default=True,
                    description=[
                        "Skip comments by Bot-typed GitHub accounts when feeding PR feedback to the",
                        "agent; the CI verdict already carries what the integrations know.",
                    ],
                ),
            },
        ),
        "token_broker": ArgumentSpec(
            short_description="Install the per-card token broker",
            description=[
                "Tokens request contents and pull requests write, checks and statuses read, plus",
                "implicit metadata read. Actions read is opt-in after granting the App that permission.",
            ],
            version_added="2.1",
            author=DEFAULT_AUTHOR,
            options={
                "coder_token_broker_app_id": OptionSpec(
                    type="str", required=True, description=["GitHub App ID."]
                ),
                "coder_token_broker_private_key": OptionSpec(
                    type="str", required=True, no_log=True, description=["GitHub App private key."]
                ),
                "coder_token_broker_bearer_token": OptionSpec(
                    type="str",
                    required=True,
                    no_log=True,
                    description=["Bearer credential for broker clients."],
                ),
                "coder_token_broker_actions_read": OptionSpec(
                    type="bool",
                    default=False,
                    description=["Request actions read after granting the App that permission."],
                ),
                "coder_token_broker_listen_host": OptionSpec(
                    type="str", required=True, description=["Broker listen address."]
                ),
                "coder_token_broker_listen_port": OptionSpec(
                    type="int", default=7693, description=["Broker listen port."]
                ),
                "worker_repo_owner": OptionSpec(
                    type="str",
                    required=True,
                    description=["GitHub owner namespace allowed for cards."],
                ),
                "worker_repos_exclude": OptionSpec(
                    type="list",
                    elements="str",
                    required=True,
                    description=["Repositories excluded from the namespace."],
                ),
            },
        ),
    }
)
