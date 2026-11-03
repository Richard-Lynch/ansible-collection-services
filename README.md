<!--
Copyright: (c) 2026, Richard Lynch
SPDX-License-Identifier: GPL-3.0-or-later
-->

# richard_lynch.services

Application, media and messaging roles: PostgreSQL with an exporter, a two-tier Docker Compose app-hosting platform with a reviewed-definition gate, arr-style media service compose files, Jellyfin hardware-transcode settings and a transcode-cache reaper, and Slack channel reconciliation and messaging.

## Roles

| Role | Description |
| ---- | ----------- |
| `jellyfin_transcode_config` | Configure Jellyfin's hardware transcode settings in encoding.xml |
| `jellyfin_transcode_reaper` | Install a systemd timer that reaps orphaned Jellyfin transcode segments |
| `service_hosting` | Traefik (docker provider) and the per-tier networks for app hosting |
| `service_postgres` | Deploy Postgres (+exporter) on the shared network with per-service DBs |
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
    version: "1.0.0"
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
`.github/workflows/` at tag `v0.2.3`. A release is a merged pull
request that bumps `version` in `galaxy.yml` and adds the matching
`CHANGELOG.md` section. `export.manifest` lists every tracked path; update it in
the same commit as any file added or removed.

## Licence

GPL-3.0-or-later. See [LICENSE](LICENSE).
