<!--
Copyright: (c) 2026, Richard Lynch
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Changelog

All notable changes to this collection are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the collection
uses [Semantic Versioning](https://semver.org/).

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
