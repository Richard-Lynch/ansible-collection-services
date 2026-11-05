# service_quickr

Deploys **quickR** — the habit tracker behind the PWA and the iOS app — as one
compose project of three containers, plus a nightly `pg_dump` timer:

| container           | image                                                                 | published                            |
| ------------------- | --------------------------------------------------------------------- | ------------------------------------ |
| `<project>-db`      | `postgres` (pinned)                                                   | no — compose network only            |
| `<project>-backend` | quickR's FastAPI backend, pulled from the registry                    | no — compose network only            |
| `<project>-web`     | quickR's nginx web origin: serves the PWA, proxies `/api/` and `/mcp` | **yes**, `quickr_web_host_port` → 80 |

The web origin is the single address every client uses — the PWA in a browser,
the iOS app and MCP clients. Postgres and the API port are never published,
unlike quickR's own development compose (which publishes 5433 and 8100 with a
hardcoded password).

## Inputs

See [`meta/argument_specs.py`](meta/argument_specs.py) (the `.yml` and
`defaults/main.yml` are generated from it). Required:

- `quickr_backend_image`, `quickr_frontend_image` — fully-qualified and pinned
  by **tag and digest**: `name:<tag>@sha256:<digest>`. quickR's release workflow
  tags each `main` build with its full git sha; pin both to the same one, each with
  the digest the registry serves for it. The digest is what is pulled.
- `quickr_postgres_password` — at least 16 characters.

Optional: APNs (`quickr_apns_team_id`, `quickr_apns_key_id`,
`quickr_apns_private_key`, `quickr_apns_topic`), registry login
(`quickr_registry_username`/`_password`), and the backup schedule and retention.

## Usage

```yaml
- ansible.builtin.include_role:
    name: richard_lynch.services.service_quickr
  vars:
    quickr_backend_image: "ghcr.io/example/quickr-backend:<sha>@sha256:<digest>"
    quickr_frontend_image: "ghcr.io/example/quickr-frontend:<sha>@sha256:<digest>"
    quickr_postgres_password:
      "{{ lookup('richard_lynch.common.onepassword_cached',
      'quickr-postgres', field='password') }}"
  no_log: true
```

## What it refuses to get wrong

- **A floating tag, a missing digest, or two different builds.** Both images
  must carry an explicit tag and a digest, and the same tag: the PWA bundle in the web image calls the API
  in the backend image, and a mixed pair fails per request rather than at
  deploy.
- **Half-configured APNs.** With some but not all of team id / key id / key, the
  backend starts with APNs off and says nothing. The role refuses a partial set,
  checks the key's PEM header (without printing it), and after deploy asserts
  the **running** backend's `GET /api/v1/push/config` reports `apns_enabled`
  exactly as configured.
- **A healthy stack that the iOS app cannot use.** `/api/v1/health` answering is
  not enough: the app syncs from `GET /api/v1/changes` and keys its local store
  on the `X-QuickR-Server-Id` header. quickR builds from before the iOS work
  answer `/health` perfectly and have neither, so the role asserts the header is
  present.
- **The password in the wrong file.** The compose file is rendered `0640` by
  `deployment_config`; the password goes only into `settings` (`0600`), the env
  file of both the db and the backend. It is URL-encoded in the database URL,
  `/` included.

## The APNs key

Written to `<config_dir>/secrets/apns.p8`, mode `0440`, group-owned by the
backend's gid (`quickr_app_gid`, 10001 in quickR's image) — so not
world-readable. The **directory** is mounted read-only, not the file: a
file-level bind mount pins the inode, and Ansible writes atomically, so a
rotated key would never reach a running container. A changed key restarts the
backend, which loads it at startup. Turning APNs off removes the directory.

## Backups

`/usr/local/sbin/quickr-pgdump`, run by `quickr-pgdump.timer`
(`Persistent=true`), writes `quickr-<UTC timestamp>.dump` (`pg_dump -Fc`) into
`quickr_backup_dir` and deletes dumps older than `quickr_backup_keep_days`. A
dump is written under a `.partial` name and kept only after `pg_restore --list`
has read it back, so a truncated dump never replaces a good one.

Every run, good or bad, also writes `quickr_pgdump.prom` into
`quickr_backup_metrics_dir` (a node_exporter-style textfile collector directory, default
`/var/lib/node_exporter/textfile_collector`), atomically:
`quickr_pgdump_last_run_success` (1/0), `quickr_pgdump_last_run_timestamp_seconds`,
`quickr_pgdump_last_success_timestamp_seconds` and `quickr_pgdump_last_success_size_bytes`
(both carried over a failed run). The file is replaced only by a COMPLETE write, so a full disk
keeps the previous one. The unit has `TimeoutStartSec=1h`: a hung `pg_dump` is TERMed, and the
script records that as a failed run. Alert on a failed run, on a last success older than the
schedule allows, and on the metrics never having been written. The molecule scenario RUNS the
rendered script against a stub `docker` (pass, fail, empty, unreadable, partial write, TERM, no
metrics dir) under dash, a minimal `/bin/sh`.

This is a LOCAL copy. It exists so a whole-machine backup of the host carries a consistent
logical dump, not only a live data directory, which a database cannot be restored from reliably.
Point an offsite backup at `quickr_backup_dir`.

To dump now: `systemctl start quickr-pgdump.service`. To
restore into a fresh stack:
`docker exec -i <project>-db pg_restore -U quickr -d quickr --clean < <file>.dump`.

## Updating

Bump both image references (tag and digest) in inventory and re-run the deploy.
`recreate: auto` replaces only the containers whose config changed, and the backend's entrypoint
runs `alembic upgrade head` on start. Migrations are forward-only, so a rollback
across a schema change needs the last dump.

## What it deliberately does NOT do

- **Build anything.** Images come from the registry, pinned.
- **Publish anything.** No traefik labels, DNS or tunnel ingress — the play that
  calls this role owns those.
- **Manage Web Push (VAPID) keys.** quickR generates a pair on first start and
  keeps it in the database, which the dump covers.

## Testing

`molecule/default` is render-only (partial coverage, honestly labelled): it
renders the role's own templates for two different callers' values, and asserts
that only the web port is published, the password is absent from the compose
file and URL-encoded in the settings file, APNs appears exactly when enabled, and
none of the first caller's values leaks into the second render. The running
behaviour is asserted by the role's read-backs on the host.

It also includes the **real role** four times with bad image references (a tag
with no digest, a digest with no tag, a floating tag, two different builds) and
requires each to be refused by the image gate that names it.
