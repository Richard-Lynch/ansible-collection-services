# service_postgres

Deploys **PostgreSQL** (plus a Prometheus `postgres_exporter`, and optionally pgAdmin) as a Docker
Compose project, and ensures **per-service databases and their owner roles** exist.

## What it does

- Renders the compose file and `postgresql.conf` into `/opt/<postgres_container_name>` (through
  `richard_lynch.common.deployment_config`), and the credential env files in their own `no_log`
  task at `postgres_env_file_mode` (default `0600`).
- Ensures the docker network `postgres_network` exists (attachable, so other compose projects on
  the host can join it and reach the database as `<container>:5432`), and brings the project up;
  restarts it when any rendered file changed.
- **Refuses the placeholder password**: `postgres_password` must be supplied by the caller.
- Waits for `pg_isready`, then idempotently creates each `postgres_databases` entry's **role**
  (`CREATE ROLE … LOGIN` if absent) and **database** (`createdb -O owner` if absent). Values reach
  psql as variables and are quoted server-side, never concatenated into SQL.
- Publishes the database on `postgres_bind_address`, the exporter on
  `postgres_exporter_bind_address` and pgAdmin on `pgadmin_bind_address`, each `127.0.0.1` by
  default: a Docker-published port is not filtered by the host firewall, and the exporter has no
  authentication. Containers on `postgres_network` reach the database whatever these are set to.

The `verify` entrypoint (`tasks_from: verify`) runs on the target host and waits for the port (on
`postgres_verify_host`: the bind address, or loopback when the port is on every interface), installs the PostgreSQL client
library that `community.postgresql` needs (`psycopg2-binary`, via `richard_lynch.common.setup_pip`)
and asserts that the bootstrap database answers a query.

## Inputs

See [`meta/argument_specs.yml`](meta/argument_specs.yml); defaults are in
[`defaults/main.yml`](defaults/main.yml). Key ones:

- `postgres_password` — superuser password (required in practice; the placeholder is rejected).
- `postgres_databases` — `[{name, owner, password}]` per-service databases.
- `postgres_db` / `postgres_user` (default `postgres`), `postgres_network` (default `postgres`).
- `postgres_bind_address`, `postgres_exporter_bind_address`, `pgadmin_bind_address` (default
  loopback; `""` publishes on every interface).
- `postgres_exporter_enabled` (default true), `pgadmin_enabled` (default false).
- `postgres_exporter_image` and `pgadmin_image` are NOT pinned by default (the exporter's tag is
  implicit `latest`, pgAdmin's is `latest`): set both to a tag or digest you have tested.
- `postgres_env_file_mode` (default `0600`).

## Usage

```yaml
- hosts: db
  become: true
  vars:
    postgres_password: "{{ lookup('richard_lynch.common.onepassword_cached', 'postgres-superuser',
                                  field='password', vault='Example-Vault') }}"
    postgres_databases:
      - name: grafana
        owner: grafana
        password: "{{ grafana_db_password }}"
  roles:
    - role: richard_lynch.services.service_postgres
```

## Testing

`molecule/default/` renders the compose file from sample values and asserts the network wiring,
the exporter's presence and that pgAdmin is off by default. It runs offline: no Docker daemon.
