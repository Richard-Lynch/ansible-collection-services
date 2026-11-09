<!--
Copyright: (c) 2026, Richard Lynch
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Changelog

All notable changes to this collection are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the collection
uses [Semantic Versioning](https://semver.org/).

## [2.2.1] — 2026-11-09

### Added

- `service_coding_worker`: `worker_codex_trust_card_worktree` (default off) makes the session daemon mark each card's clone as a trusted Codex project in that card's `config.toml`. Codex matches trust on the exact path, and runs an untrusted project read-only whatever the approval and sandbox options say, so without it an unattended Codex card in protocol mode cannot write its clone.

## [2.2.0] — 2026-11-09

### Added

- `service_coding_worker`: `worker_codex_approval_policy` and `worker_codex_sandbox_mode` set Codex's approval policy and sandbox in the base `config.toml` (defaults unchanged: `on-request`, `workspace-write`), so an unattended card can run with `never` instead of stopping on a prompt nobody answers.
- `service_coding_worker`: `worker_codex_reasoning_effort` (empty leaves the model's default) and the gateway provider's `stream_idle_timeout_ms`, `stream_max_retries` and `request_max_retries`, so a long agent turn behind a proxy is not cut off early.

## [2.1.3] — 2026-11-08

### Fixed

- `service_coding_worker`'s live protocol-mode suite builds the session daemon's configuration for the owner-namespace repository model and seeds a local repository cache, so it runs instead of being refused when the pinned acpx is installed.

## [2.1.2] — 2026-11-08

### Added

- Card tokens can opt in to actions read via coder_token_broker_actions_read.

### Security

- The session daemon no longer runs git against a card's clone after the card starts. It reads the branch head as plain data, so repository configuration written inside a card cannot execute on the host.
- In container mode nothing the session daemon runs after a card starts uses a card-controlled directory (the clone, the card home or its data) as a working directory or repository: the tmux client starts in a supervisor-owned directory and every command for the card runs inside its container; a fallback directory removal runs in a throwaway container.

## [2.1.1] — 2026-11-08

### Fixed

- `service_coding_worker`'s session-daemon suite passes when run from the installed collection: the check that the molecule scenario declares the exclude list skips when `molecule/` is not present (the built collection does not ship it), instead of failing with a missing file.

## [2.1.0] — 2026-11-08

### Added

- `service_coding_worker` entry point `token_broker`: a small HTTP service, run on a host other than the worker, that holds a GitHub App private key and mints a one-hour installation token scoped to ONE repository (`contents: write`, `pull_requests: write`) for each card. It refuses repositories outside the configured owner or on the exclude list, refuses a token GitHub returns with broader permissions or repositories than asked for, and logs every mint with its card and repository (never the token).
- `worker_token_broker_url` / `worker_token_broker_bearer`: with a broker configured, sessiond mints a token per card for that card's repository, writes it to the card container's gh configuration (0600), refreshes it before it expires, and uses the same per-repository tokens for its own GitHub calls. No `GH_TOKEN` is written to the agent environment or forwarded into a container.

### Changed

- Repositories are an owner namespace (`worker_repo_owner`) with a required explicit exclude list (`worker_repos_exclude`) and per-repository base branch overrides (`worker_repo_base_branches`); sessiond clones a repository on demand the first time a card names it and fetches it on every later card, so each card branches from the current remote base. Names outside the owner (including near-miss owners), path-like names and excluded names are refused.
- `worker_github_token` is optional: it is the legacy credential, used only when no broker is configured.

### Removed

- **Breaking:** `worker_repos` and the converge-time clone of each listed repository. Set `worker_repo_owner` and `worker_repos_exclude` (and, where needed, `worker_repo_base_branches`) instead.

### Security

- Every agent start installs the managed Claude policy; containers refuse to start when it is missing. Card branches must stay in their own namespace and cannot be base branches. The broker validates repository identities independently and verifies upstream identity.
- With a broker, a card can push only to its own repository, for at most an hour, and the App private key is never on the worker host. The worker's broker bearer can still request a token for any repository in the namespace, so it is the worker's credential: keep it 0600 and watch the broker's mint log. Shared caches now live under work_root/repos; old caches are no longer used. Tokens include checks and statuses read, and refresh independently of PR polling. PR feedback is limited to the card branch commit in the same repository.

## [2.0.1] — 2026-11-05

### Fixed

- `service_model_explorer` deploys the image it is given. In 2.0.0 the compose project's source directory was passed to `richard_lynch.common.deployment_config` as a role_path expression, which Ansible evaluates inside the included role, so it named that role's directory: nothing was rendered, compose saw no change, and the previous container kept running. The path is now resolved in the role's own context (`tasks/_compose.yml`), and molecule runs that entrypoint for real and requires the rendered compose file to carry the caller's digest.
- `service_model_explorer` removes the files that versions before 2.0.0 installed, and only after every read-back has passed, so a failed converge never strips a container that is still serving from them. A host converged with 2.0.0 is repaired by running 2.0.1; no manual cleanup is needed.

## [2.0.0] — 2026-11-05

### Added

- `service_quickr`: deploys the quickR habit tracker (backend, web origin, Postgres) with Docker Compose from its public images on ghcr.io, pinned by tag AND digest (`name:<tag>@sha256:<digest>`, both images from the same build), with optional APNs push and a nightly pg_dump timer whose result is exported as textfile metrics.

### Changed

- BREAKING: `service_model_explorer` deploys the model-explorer container image (`ghcr.io/richard-lynch/model-explorer`, source at Richard-Lynch/model-explorer) by DIGEST: `model_explorer_image` is now required, must be `name@sha256:<64 hex>`, and has no default. The role no longer ships the app's source or copies a built bundle, so nothing is built and no node toolchain is needed. The catalogue snapshot is mounted as a read-only directory at `/srv/catalogue`, the container runs with a read-only root filesystem, and the read-back requires the image's own healthcheck to report healthy on the pinned digest. `model_explorer_dist_dir` is removed. On upgrade the role removes the `site/` and `nginx.conf` that earlier versions installed.
- Migrating from 1.x: set `model_explorer_image` in inventory to a released image DIGEST (`ghcr.io/richard-lynch/model-explorer@sha256:...`, from the model-explorer release's notes or its `image-digest.txt`), and delete `model_explorer_dist_dir` (and any bundle build step) from the caller. Nothing else changes for a caller; the role removes the files earlier versions installed on the host.
- The CI, release and disclose callers use ansible-devkit `v0.4.1`.

### Fixed

- `service_hosting` and `jellyfin_transcode_reaper` run to the end under `--check` on a correct host. Their read-backs (resolver nameservers, entrypoint probes, socket-proxy and cross-tier refusals, firewall rules, registry manifests, the reaper's state, the transcode timer and its log) were skipped in check mode, so the gates after them read nothing and failed. They now run under `--check` as well. The hosting reaper's metrics-freshness check is skipped under `--check`, because the run that refreshes the metrics is a write.
- The model explorer's healthcheck probes `127.0.0.1`, not `localhost`, so it cannot fail against a working server when `localhost` resolves to IPv6 first.

## [1.1.1] — 2026-11-04

### Added

- CI: the agent-tree check covers the AI-stack roles (tree roots declared)

## [1.1.0] — 2026-11-04

### Added

- Role `service_hermes`: Nous Research's Hermes Agent (gateway and dashboard in one supervised container) as a Docker Compose stack, routed through an OpenAI-compatible gateway: an owned-leaf merge of the agent's config.yaml (the caller owns named keys, the agent keeps the rest), a managed-policy scope with a pre-tool-call policy hook, an audit hook and a send gate, pinned images, seeded cron jobs and webhook routes, a dashboard theme, a health textfile exporter, Kanban notifications, and an optional settings-service sidecar that runs the caller's own settings application.
- Roles `config_hermes_profile` (one named agent profile: config leaves, environment and a SOUL, with read-backs), `config_hermes_learning` (a ledger, a nightly probe set and a weekly memory consolidation proposal as systemd timers, and an optional pinned memory provider), `config_agent_state` (age-encrypted backups with a retention ring, a masked git mirror a secret scan can block, a restore with read-backs and a restore drill) and `config_mcp_servers` (a reviewed MCP catalogue and per-client enablement rendered as one document).
- Role `service_litellm`: a LiteLLM gateway with PostgreSQL, optional Redis and an Open WebUI front end as one Compose stack; provider keys rendered no_log at 0600; Open WebUI connections and task model reconciled through its API.
- Role `service_coding_worker`: a coding-agent worker on a VM: one git worktree and one sysbox dev container per task card, Claude Code or Codex inside it routed through a LiteLLM gateway, supervised by a session daemon, with a ttyd view and an outbound firewall for the cards.
- Roles `service_code_reader` (hands one-time verification codes from a dedicated mailbox to a browser filler exactly once, so the model never sees them) and `service_model_explorer` (a static, read-only model-catalogue explorer behind a pinned nginx; the caller supplies the catalogue snapshot).
- Filters `hermes_*` and `mcp_catalogue_document` for the roles above, each with ansible-doc documentation and unit tests.
- Every installation-specific value is an option the caller sets: docker networks, gateway provider names, model aliases, theme and skills paths, image names, address pools, the settings application's source and the model catalogue snapshot.

## [1.0.0] — 2026-11-03

### Added

- Role `service_postgres`: PostgreSQL with a Prometheus postgres_exporter (published on loopback by default) and optional pgAdmin as a Compose project; credential env files rendered no_log at 0600; per-service databases and owner roles created idempotently; a verify entrypoint that installs the client library and queries the server.
- Role `service_hosting` with filters `hosting_*` and lookup `hosting_definition`: a two-tier (DEV/PROD) Docker Compose hosting platform behind Traefik and a Docker socket proxy. App definitions are read as inert data and judged by an allowlist gate before anything is written; PROD deploys only a definition merged on a named git ref; per-app egress exceptions are pinned to one container's address; expired DEV apps are reaped on a timer.
- Roles `stack_arr_service` and `stack_arr_jellystat`: one Compose file per arr-style media service (optional exportarr sidecar, published on loopback by default) and for Jellystat with its own database; compose layer only, never the services' own config volumes, never a restart.
- Roles `jellyfin_transcode_config` (hardware-transcode settings in encoding.xml, diff first, backed-up write) and `jellyfin_transcode_reaper` (a systemd timer that removes orphaned transcode segments).
- Roles `slack_channels` (module `slack_channels`: reconcile declared public channels, guides, bookmarks and membership, with a read-back) and `slack_message` (a bot message or file to a DM or channel, with a read-back).
- Every installation-specific value is an option the caller sets: hostnames, paths of reviewed definitions, credentials, networks and bind addresses.
