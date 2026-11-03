<!--
Copyright: (c) 2026, Richard Lynch
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Changelog

All notable changes to this collection are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the collection
uses [Semantic Versioning](https://semver.org/).

## [1.0.0] — 2026-11-03

### Added

- Role `service_postgres`: PostgreSQL with a Prometheus postgres_exporter (published on loopback by default) and optional pgAdmin as a Compose project; credential env files rendered no_log at 0600; per-service databases and owner roles created idempotently; a verify entrypoint that installs the client library and queries the server.
- Role `service_hosting` with filters `hosting_*` and lookup `hosting_definition`: a two-tier (DEV/PROD) Docker Compose hosting platform behind Traefik and a Docker socket proxy. App definitions are read as inert data and judged by an allowlist gate before anything is written; PROD deploys only a definition merged on a named git ref; per-app egress exceptions are pinned to one container's address; expired DEV apps are reaped on a timer.
- Roles `stack_arr_service` and `stack_arr_jellystat`: one Compose file per arr-style media service (optional exportarr sidecar, published on loopback by default) and for Jellystat with its own database; compose layer only, never the services' own config volumes, never a restart.
- Roles `jellyfin_transcode_config` (hardware-transcode settings in encoding.xml, diff first, backed-up write) and `jellyfin_transcode_reaper` (a systemd timer that removes orphaned transcode segments).
- Roles `slack_channels` (module `slack_channels`: reconcile declared public channels, guides, bookmarks and membership, with a read-back) and `slack_message` (a bot message or file to a DM or channel, with a read-back).
- Every installation-specific value is an option the caller sets: hostnames, paths of reviewed definitions, credentials, networks and bind addresses.
