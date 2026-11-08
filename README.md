<!--
Copyright: (c) 2026, Richard Lynch
SPDX-License-Identifier: GPL-3.0-or-later
-->

# richard_lynch.services

Application, media, messaging and AI-agent roles: PostgreSQL with an exporter, a two-tier Docker Compose app-hosting platform with a reviewed-definition gate, arr-style media service compose files, Jellyfin transcode settings and a cache reaper, Slack channel reconciliation and messaging, and a self-hosted AI agent stack: the Hermes agent with policy hooks, profiles, learning jobs, state backup and MCP clients, a LiteLLM gateway with Open WebUI, a sandboxed coding worker, a mailbox code reader and a model catalogue explorer.

## Roles

| Role | Description |
| ---- | ----------- |
| `config_agent_state` | Install the agent-state backup and mirror jobs and their timers |
| `config_hermes_learning` | Install the Hermes learning loop: ledger, probe set, consolidation, ping |
| `config_hermes_profile` | One named Hermes profile: config leaves, environment, SOUL |
| `config_mcp_servers` | The reviewed MCP catalogue and its per-client enablement |
| `jellyfin_transcode_config` | Configure Jellyfin's hardware transcode settings in encoding.xml |
| `jellyfin_transcode_reaper` | Install a systemd timer that reaps orphaned Jellyfin transcode segments |
| `service_code_reader` | Deploy the one-shot verification-code reader sidecar |
| `service_coding_worker` | Coding worker VM: one sysbox dev container per card, agents inside it |
| `service_hermes` | Deploy the Hermes Agent gateway and its web dashboard |
| `service_hosting` | Traefik (docker provider) and the per-tier networks for app hosting |
| `service_litellm` | Deploy the LiteLLM gateway, Open WebUI and Postgres as one stack |
| `service_model_explorer` | Run the model-discovery app's container image, pinned by digest |
| `service_postgres` | Deploy Postgres (+exporter) on the shared network with per-service DBs |
| `service_quickr` | Deploy quickR (Postgres, FastAPI backend, nginx web origin) |
| `slack_channels` | Reconcile a declared list of Slack public channels, as a bot |
| `slack_message` | Send a Slack message, optionally with a file, as a bot |
| `stack_arr_jellystat` | Write jellystat + jellystat-db compose (compose layer only) |
| `stack_arr_service` | Write one arr-stack service's docker-compose.yml (compose layer only) |

## Install

```sh
ansible-galaxy collection install richard_lynch.services
```

or pin it in `requirements.yml`:

```yaml
collections:
  - name: richard_lynch.services
    version: "2.1.0"
```

Requires ansible-core >=2.19.0.

## Development

```sh
# from a checkout at ansible_collections/richard_lynch/services
ansible-test sanity --docker
ansible-test units --docker
ansible-galaxy collection build
devkit conform                  # the repository standard (ansible-devkit)
devkit check all                # the static checks, configured by devkit.toml
```

CI, the disclosure gate and releases are the shared workflows of
[ansible-devkit](https://github.com/Richard-Lynch/ansible-devkit), called from
`.github/workflows/` at tag `v0.4.1`. A release is a merged pull
request that bumps `version` in `galaxy.yml` and adds the matching
`CHANGELOG.md` section. `export.manifest` lists every tracked path; update it in
the same commit as any file added or removed.

## Licence

GPL-3.0-or-later. See [LICENSE](LICENSE).
