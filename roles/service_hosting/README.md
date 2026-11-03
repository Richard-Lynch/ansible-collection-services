# service_hosting

A small app-hosting platform on one Docker host: Traefik with the **docker provider** behind a
**read-only docker-socket-proxy**, **one Traefik, one entrypoint and one docker network per tier**
(a DEV container cannot reach the PROD Traefik), a **host firewall** (`hosting-firewall`: a
container egress block and a source allowlist on the published tier ports), and a **gated
`deploy_app` entrypoint** that turns an app definition into a running, routed container.

Tiers are declared by the caller (`hosting_tiers`), not here. A typical pair:

| tier | hostnames | who deploys |
|---|---|---|
| `dev` | `<name>.dev.example.com` | anyone allowed to run the deploy play, freely |
| `prod` | `<name>.apps.example.com` | the same play, but only for a definition already merged on `hosting_merge_ref` |

Publishing an app needs no per-app DNS or tunnel step when a wildcard name for each tier already
points at the host: the Traefik labels on the app's compose are the whole publication.

**Platform: Debian or Ubuntu.** The `main` entrypoint installs `iptables` with
`ansible.builtin.apt` (the egress firewall is `DOCKER-USER` rules), so it needs an apt-based host
with Docker Engine already installed. There is no OS assertion; on another family that task fails.

## Entrypoints

* `main` — converge the platform (firewall, private registry logins, socket proxy, a Traefik per
  tier, networks, tier roots). **Never removes or restarts an app**, so a converge keeps every
  existing DEV and PROD app.
* `deploy_app` — validate one app definition, write `<tier root>/<name>/compose.yml`, `compose up`,
  and read the app back through Traefik on the host. Preview unless `hosting_deploy_apply` is true.
* `assert_merged` — the PROD gate: refuses unless the definition equals, as parsed YAML, the file it
  names as its `source` on `hosting_merge_ref` (a real `git fetch` + `git show`). The merge is the
  approval; a local edit, an unmerged branch and a stale checkout all fail the comparison.
* `assert_target` — the TARGET gate, run from a play whose hosts line is `all`: refuses, with every
  reason printed, a run with ANY host limit or a `target=` naming another host, and a hosting host
  missing from the inventory. A limit is refused outright because each shape of it exits green
  while skipping a gate (a limit to the hosting host skips the controller-side PROD merge gate; to
  localhost, the deploy; to another host, every play).
* `remove_app` — bring one app down and move its directory aside (see below).

Inputs are declared in [`meta/argument_specs.py`](meta/argument_specs.py); the argspec `.yml`,
`defaults/` and the resolver are **generated** from it.

## The app definition

```yaml
hosting_app_def:        # the file's one key; the deploy play reads it as inert data
  name: hello            # becomes hello.<tier suffix>
  tier: dev              # dev | prod
  port: 80               # the container port Traefik targets
  service: web           # which compose service is routed
  health_path: /         # optional, default /
  expect_status: 200     # optional, default 200
  compose:               # a compose project: services (+ optional volumes / networks)
    services:
      web:
        image: nginx:1.27-alpine
  secrets:               # optional, ONLY on a tier declared `secrets: true` (PROD)
    api_key: { item: service/x/key, field: password, service: web, uid: 10001 }
```

`secrets:` names secret-store items; `deploy_app` resolves them on the controller with
`richard_lynch.common.onepassword_cached`, from the vault named by `hosting_secrets_vault` (an app
with secrets is refused while it is unset), and writes each to `<tier root>/<name>/secrets/<key>`
(0400, owner `uid`), mounted into that one service at `/run/secrets/<key>` by a compose `secrets:`
block the platform writes. The `hosting_secret_files` filter derives the paths; the app cannot name
one.

## The gate (what is refused, and why)

The gate is a collection filter (`hosting_violations`, in `plugins/filter/hosting.py`) so it is
testable with fixtures that must be refused **and** near-misses that must pass. It reports
**every** reason at once. Service keys are an **allowlist**: a compose feature the gate does not
know is refused until someone reads it and adds it. Refused: a `privileged` service,
`network_mode`/`pid`/`ipc`/`userns_mode`, `cap_add`, `devices`, `security_opt`, `sysctls`, `ports`
(Traefik is the only way in), `build`, `env_file`, `container_name`, a Docker-socket mount in either
volume syntax, **any host bind mount** (a bind under a directory the app writes lets it plant a
symlink there; state goes in a named volume or tmpfs), any value containing `$` in a volume or
image (compose interpolates `${X:-..}` before it mounts, so the gate would be judging a different
path from the one Docker uses), a `mem_limit`/`cpus` above `hosting_max_mem_limit`/
`hosting_max_cpus`, `oom_*`, `gpus` and `deploy.resources.reservations`, any `traefik.*`/`hosting.*`
label (routing labels are generated from `name` and `port`, so an app cannot claim another hostname
or entrypoint), an image off the allowlist or not pinned (floating tag, no tag), top-level
`include`/`configs`/`secrets`, a volume with `driver_opts`, an `external` network, and a name that
is malformed, reserved or collides with a platform network.

What the platform injects (the app cannot set these): the Traefik labels, the tier network,
`no-new-privileges`, a restart policy and a memory/CPU limit if none was declared. Every `$` in the
app's own values is escaped to `$$` so compose keeps it literal.

**Two gates, not one.** After the first gate, `deploy_app` writes `compose.candidate.yml`, asks
`docker compose config --format json` what it RESOLVES to, and judges that model with
`hosting_resolved_violations` before promoting the candidate to `compose.yml`. The first gate reads
the text, the second reads what Docker will run.

**The definition is data.** A deploy play that receives the definition as `-e @file` gets a file
Ansible templates as trusted code. So the file's one key (`hosting_app_def`) is never referenced;
the `hosting_definition` lookup re-reads the file as raw text and refuses any Jinja delimiter (also
after YAML parsing, for `\x7b\x7b`), a second top-level key, any other `-e`, and any path but
`<prefix>/<name>/vars.yml` (its `prefix` option; `hosting_source_prefix` is the same directory for
`assert_merged`).

**Image allowlist semantics:** an entry ending in `:` names a repository (`nginx:` allows
`nginx:1.27` and `nginx@sha256:...`, not `nginxevil/x:1`); any other entry is a plain prefix.

## Removal and the TTL reaper

`main` installs `/usr/local/sbin/hosting-reaper` and `hosting-reaper.timer` (every
`hosting_reaper_interval`). On a tier declared `reap: true` (DEV), an app whose `ttl_hours` > 0 is
removed once that many hours have passed since its record (`hosting.json`) last CHANGED; `ttl_hours`
0 never expires, and a TTL on a non-reaping tier is refused by the gate. Removal is
`docker compose down --remove-orphans`, a daemon read-back that no container of the project is left,
then the directory moves to `<hosting_reaped_root>/<tier>/<name>-<stamp>`. The `remove_app`
entrypoint runs the same script with `--remove`, and refuses a non-reaping tier, so a PROD app
cannot be removed that way.

Every run writes `hosting_reaper.prom` to `hosting_textfile_dir` (a node_exporter textfile
collector directory): `hosting_reaper_last_run_timestamp_seconds`, `hosting_reaper_last_run_success`,
`hosting_reaper_reaped_last_run`, `hosting_app_ttl_hours{tier,app}` and
`hosting_app_expires_timestamp_seconds{tier,app}`. Alert rules over them belong to the caller.

## Private registries

`hosting_registries` lists the registries `main` logs in to before anything is pulled, so a PROD
app may run a PRIVATE image. Each entry names the registry `host`, the `username`, the secret-store
`item`/`field`/`vault` holding a pull-only token, and a `probe_image`: a private image on that
registry, pinned by tag or digest.

* The token is resolved with `richard_lynch.common.onepassword_cached` and passed to `docker_login`
  under `no_log`. It lands in the connecting user's `~/.docker/config.json` (derived from facts),
  which is set to 0600 in a 0700 directory: with no credential helper on a headless host, docker
  stores it base64-encoded, so the mode is the only protection.
* **The login is not the proof.** A token without read access to packages logs in and then fails
  every pull. So the converge reads `probe_image`'s manifest WITH the stored config (must succeed)
  and with an empty config (must be refused, `unauthorized`/`denied`). The second half is what makes
  the first mean something: a public probe image would pass with no login at all.
* Refused before any lookup: a host with a scheme or path, the same host twice, a probe image that
  is not on that host or is not pinned, and a missing or unknown key (also refused by the argspec).
* The login lives on the host, so `deploy_app` pulls with it; re-run `main` after rotating the
  token.

## Egress grants are per app

Each `hosting_egress_allow` entry is a grant to ONE container: `{app, service, subnet, address,
cidr, ports, tier (default prod), proto (default tcp)}`. `deploy_app` puts the granted app's
default network on a stable bridge, `hst-a-<app>` (network `<project>_egress`, that subnet, with
`ip_range` the upper half so a dynamic address can never take the pinned one), and pins `service`
to `address`. The firewall allows the grant only from that bridge AND that source, so the app's
other services do not hold it either. An app definition cannot set a network `name`,
`driver_opts` or `external` (the gate refuses them), so only the platform can put a container on
that bridge. An incomplete grant is refused, never widened. A grant that matched every bridge
(`docker0`, `br-+`, `hst-+`) would give every container on the host every grant, so the main
entrypoint asserts the kernel holds no egress grant on a wildcard bridge.

Switching an app to its egress bridge recreates its containers on a new network; the old
`<project>_default` network is left behind, empty, until removed.

## What it deliberately does NOT do

* Delete an app's DATA. The reaper and `remove_app` bring the project down and move its directory
  to `hosting_reaped_root`; named volumes stay until someone removes them on purpose.
* Isolate DEV apps from one another. A tier is ONE docker network, so two DEV apps can reach each
  other's routed service. Per-app networks are the fix if that matters.
* Restrict internet egress. Containers cannot open connections into the private ranges
  (`hosting_blocked_egress_cidrs`) except DNS to the host's resolvers and the
  `hosting_egress_allow` grants. The internet is open because apps need it.
* Decide WHO may deploy. That is the calling play: host pinning (`assert_target`) and the PROD
  merge check (`assert_merged`) are entrypoints it must call, not something `deploy_app` does on
  its own.

## Testing

`molecule/default` runs the **real** `deploy_app` entrypoint in preview mode (no Docker needed)
against 40+ fixtures that must be refused — each asserted to be refused *for the stated reason*, so
a role that cannot be found cannot "pass" by failing — and near-misses that must pass, then
verifies the rendered Traefik/compose artifacts and the compose built for an app. It also runs
`assert_merged` against a real bare git origin: identical-to-merged passes; edited-after-merge, a
locally added `privileged`, never-merged, a `source` pointing elsewhere and no `source` are each
refused for the stated reason. It runs `assert_target` through cases where no limit and a
`target=` naming the hosting host pass, and a target naming another host, a limit to the hosting
host, to localhost and to another host, a host absent from the inventory, and two reasons at once
are each refused for the stated reason. Each `hosting_registries` case runs through the role's
argspec and the real `tasks/_registries.yml` gate. The `hosting_definition` lookup is fed the
refused shapes directly (`lookup('pipe')`, a statement, escaped braces, a Jinja key, a YAML tag, two
files, smuggled config). The registry login, the two-sided manifest probe and a real converge need
a secret store, a registry and a Docker daemon, so they are checked on the host by the role's own
read-backs, including a negative test that the socket proxy answers 403 to a write.
