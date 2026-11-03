# stack_arr_jellystat

Writes `<jellystat_config_dir>/docker-compose.yml` for Jellystat: the `jellystat` app plus its own
`jellystat-db` PostgreSQL container.

## Why a separate role from stack_arr_service

Jellystat is a real two-container dependency (`depends_on`, `shm_size: 1gb`, and database
credentials shared between the app and its database), not a single linuxserver-shaped app container
with a config bind mount and an optional exportarr sidecar. Forcing it into `stack_arr_service`'s
model would bolt a database special case onto a role whose point is one uniform per-service shape.

## Compose layer only

Never touches `./data/postgres-data` (the database's own files) or `./data/backup-data`: both are
runtime state owned by PostgreSQL and Jellystat. Never starts, stops or recreates a container as a
side effect of writing the compose file; it sets `stack_arr_jellystat_changed` so the calling play
can decide.

## Image pins

- `jellystat`: pinned by **digest** by default, because upstream publishes no semver image tag.
- `jellystat-db`: `postgres:15.2` by default. Keep the major version the existing data directory
  was initialised with: PostgreSQL does not start on a data directory from another major version.

## Secrets

`jellystat_postgres_password` and `jellystat_jwt_secret` must be already-**resolved** plain strings
when they reach this role; resolve them in the calling play, for example:

```yaml
jellystat_postgres_password: >-
  {{ lookup('richard_lynch.common.onepassword_cached', '<item-id>',
            field='password', vault=my_vault) }}
jellystat_jwt_secret: >-
  {{ lookup('richard_lynch.common.onepassword_cached', '<item-id>',
            field='password', vault=my_vault) }}
```

The rendered compose file contains both values, so it is written `mode: "0600"` and the render task
is `no_log`.

## Inputs

See [`meta/argument_specs.py`](meta/argument_specs.py) (the `.yml` and `defaults/main.yml` are
generated from it): `jellystat_image`, `jellystat_db_image`, `jellystat_network` (default
`traefik`), `jellystat_config_dir` (default `/opt/jellystat`), `jellystat_postgres_user` (default
`postgres`), `jellystat_postgres_password` / `jellystat_jwt_secret` (required, resolved secrets),
`jellystat_traefik_hosts` (required: the hostnames the Traefik router matches).

## Testing

`molecule/default/` renders the compose from sample (fake) values and asserts both containers are
present, the images pinned, the database dependency wired, the secrets and the Host() rule
rendered. Entirely offline: no docker, no remote host, no secret store.
