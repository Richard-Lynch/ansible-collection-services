# stack_arr_service

Writes **one** `<arr_config_base_dir>/<service>/docker-compose.yml` for a single
linuxserver.io-shaped media service (Sonarr, Radarr, Prowlarr, SABnzbd, Jellyfin, Jellyseerr,
qBittorrent and the like). A play calls it once per service, in a loop.

## Granularity: one parameterised role

Most arr-style services share one shape: a `linuxserver.io` (or equivalent) image,
`PUID`/`PGID`/`UMASK`/`TZ`, a config bind mount, zero or more extra bind mounts, Traefik routing
labels and, for the *arr apps, an optional `exportarr` Prometheus-metrics sidecar. Rather than one
near-identical role per service, this role takes a per-service `dict` and renders one compose file
per invocation:

- **an *arr app with metrics** — the common shape plus an `exportarr-<name>` sidecar
  (`arr_service.exportarr`).
- **a media server with hardware transcode** — the same shape plus `devices:` (for example
  `/dev/dri`), `group_add:` (video/render groups) and extra environment, all through the generic
  `arr_service.devices` / `arr_service.group_add` / `arr_service.environment` fields rather than a
  service-specific code path.
- **a client behind a VPN container** — `network_mode: service:<vpn>` instead of joining the shared
  network, plus sibling containers that do not fit the linuxserver shape at all (a VPN client with
  `cap_add`/`sysctls`/`healthcheck`), passed through verbatim with `arr_service.raw_extra_services`
  rather than forced into typed options used once.

A service with a real inter-container dependency (an app plus its own database, as Jellystat has)
does not fit this shape; `stack_arr_jellystat` covers that one. Reverse proxies and tunnels are
infrastructure, not app containers, and are out of scope here.

## Compose layer only — never touches config-volume contents

Each of these services owns runtime state in its bind-mounted config directory: `config.xml` API
keys, `sabnzbd.ini` (which holds keys and provider logins that appear in no compose file),
`settings.json`, and so on. That state is **authoritative**, not the compose environment, and some
of these applications rewrite their own config at runtime. This role **only ever writes the compose
file**: it never creates, templates or deletes anything under the config bind mount.

It also **never starts, stops or recreates a container**. Writing `docker-compose.yml` reports
`changed` (and sets `stack_arr_service_changed`) so the calling play can decide what to do;
recreating the container is a separate, explicitly gated step owned by the play, never an implicit
side effect of this role.

## Secrets

The `exportarr.api_key` value must already be **resolved** (a plain string) when it reaches this
role; the role never calls a secret lookup itself, which keeps `molecule test` fully offline.
Resolve it in the calling play, for example with `richard_lynch.common.onepassword_cached`. Look a
1Password item up **by its ID** when its title contains `/`: a title with a slash is read as `op://`
path segments and returns an empty string.

```yaml
sonarr_api_key: >-
  {{ lookup('richard_lynch.common.onepassword_cached', '<item-id>',
            field='password', vault=my_vault) }}
```

The rendered compose file contains the resolved value, so it is written `mode: "0600"` and the
render task is `no_log` unconditionally: `environment`, `extra_volumes` and `raw_extra_services`
are generic passthroughs any caller could put a secret in (a VPN sibling's private key, say), so
no_log cannot be conditioned on one known field.

## Image pins

`arr_service.image` and `arr_service.exportarr.image` are both required and must be fully pinned (a
tag or a digest). A floating tag such as `:latest`, or no tag at all, makes the rendered file mean a
different image on every pull, so a re-render that reports no change can still deploy new code.

## Inputs

See [`meta/argument_specs.py`](meta/argument_specs.py) (the `.yml` and `defaults/main.yml` are
generated from it). Key ones:

- `arr_service_name` (required) — the service key; also the `<arr_config_base_dir>/<name>`
  directory unless `arr_service.compose_path_override` names an existing on-host directory whose
  name differs (for example one created with a typo), so the role writes to the directory that is
  actually used instead of creating a second one beside it.
- `arr_service` (required) — per-service `dict`: `image`, `container_name`, `config_volume`,
  `extra_volumes`, `environment`, `devices`, `group_add`, `network_mode`, `traefik`, `exportarr`,
  `raw_extra_services`.
- `arr_service_svc_zone` (optional) — a DNS zone; when set, `<router>.<zone>` is appended to the
  service's Traefik `Host()` rule (opt out per service with `svc_dns: false`).
- `arr_network` (default `traefik`) — shared external docker network name.
- `arr_exportarr_bind_address` (default `127.0.0.1`) — host address the exportarr metrics port is
  published on; exportarr has no authentication. `""` publishes on every interface, which a
  Docker-published port does without passing the host firewall.
- `arr_common_puid` / `arr_common_pgid` / `arr_common_umask` / `arr_common_tz` — common environment
  (default 1000/1000/002/Etc/UTC).

## Testing

`molecule/default/` renders three representative services (one with an exportarr sidecar, one with
device passthrough, `group_add` and extra environment, one with `network_mode` and raw sibling
containers) from sample values and asserts on the rendered YAML: image pins, common environment,
Traefik labels, exportarr wiring and bind address, and the raw-sibling passthrough. Entirely
offline: no docker, no remote host, no secret store.
