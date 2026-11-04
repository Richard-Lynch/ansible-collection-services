# service_hermes

Deploys [Hermes Agent](https://github.com/NousResearch/hermes-agent) — Nous
Research's self-hosted AI agent with persistent memory, chat-platform gateways
and tool execution — as a single supervised container with its web dashboard,
plus an optional settings-service sidecar.

Hermes bundles the gateway and the dashboard as two
[s6-overlay](https://github.com/just-containers/s6-overlay)-supervised services
_inside one image_, so there is nothing to split into a second compose service.

## What it gives you

- The **gateway** (`gateway run`), s6-supervised: if the agent process crashes,
  s6 restarts it within seconds without losing the container.
- The **web dashboard** (default port 9119), behind a mandatory auth gate.
- Optionally, the **OpenAI-compatible API server** (default port 8642) — **off
  by default**.
- All mutable state on one host bind-mount, so the image is stateless and an
  upgrade is a tag change rather than a migration.
- An **owned-leaf merge** of the agent's `config.yaml`: the caller owns named
  keys, the agent and the dashboard keep everything else.
- Optional hardening: a pinned **managed scope** checked by a preflight before
  the gateway starts, a fail-closed `pre_tool_call` **policy hook**, a
  value-free `post_tool_call` **audit hook**, a **send gate** for outbound mail
  and protected paths.
- Optional automations: the **webhook listener** with static routes, **seeded
  cron jobs** and their scripts, the agent's own **mailbox**, **Slack** (Socket
  Mode), **Kanban → Slack** card threads, and the GitHub CLI for the Kanban PR
  completion contract.

## Inputs

Everything is declared in [`meta/argument_specs.yml`](meta/argument_specs.yml)
(generated from `meta/argument_specs.py`; `defaults/main.yml` is generated too).
Required: `hermes_network`, `hermes_image_tag` and `hermes_image_digest`. The
credential-bearing options (`hermes_dashboard_password`,
`hermes_dashboard_auth_secret`, `hermes_gateway_api_key`,
`hermes_api_server_key`, the Slack, email, webhook, settings-service and
observability tokens) are all `no_log`.

## Minimal usage

```yaml
- name: Deploy Hermes Agent
  hosts: agent-host
  roles:
    - role: richard_lynch.services.service_hermes
      vars:
        hermes_network: agents # an existing external docker network
        hermes_image_tag: v0.22.5
        hermes_image_digest:
          "sha256:<64 hex characters of the tag's index digest>"
        hermes_gateway_url: "http://gateway.example.com:4000/v1"
        # A scoped LiteLLM VIRTUAL key for the agent. Never the gateway's master key.
        hermes_gateway_api_key: "{{ vault_hermes_gateway_key }}"
        hermes_default_model: example/model-id
        hermes_dashboard_password: "{{ vault_hermes_dashboard_password }}"
```

## The image pin is a pair: tag AND digest

`hermes_image_tag` and `hermes_image_digest` are both **required, with no role
default**: which version runs is the caller's decision, and a role default would
make a caller who forgot to declare one run somebody else's version.
`hermes_image_repo` defaults to `nousresearch/hermes-agent`. The compose file
renders `image: repo:tag@sha256:...`, so a retagged image cannot run: the digest
decides, the tag is the label a human reads. `_pin_gate.yml` refuses a floating
tag (`latest`, `stable`, `main`, `edge`, `canary`), a tag with no digest, a
digest that is not `sha256:` + 64 lowercase hex characters, a tag that smuggles
its own `@digest`, and a repo that already carries a tag. The digest is the
multi-arch **index** digest from the registry's tag listing.

Two things about this project's images are easy to get wrong:

1. **The repo's own `docker-compose.yml` does not name a published image.** It
   declares `build: .` with `image: hermes-agent`, a _local build tag_ that
   resolves to nothing in any registry. The published image is on Docker Hub.
2. **`stable` and `latest` are floating and can point at a different digest than
   the newest versioned release.** Upstream's own Docker guide says to use a
   versioned tag, or a digest, for an exact deployment pin.

## config.yaml is MERGED, not overwritten (owned leaves)

The role owns a declared set of **leaves** of the agent's `config.yaml`; the
dashboard and the agent write the rest, and a converge must not erase that. The
owned set is:

- the provider/model/guardrail **wiring** this role renders, taken from
  `templates/config.yaml.j2` and flattened into leaves (one source for it), and
- `hermes_config_owned` from inventory: `{key, value, tier, allowed}`, where
  `key` is a dotted path (or a list of segments when one contains a dot),
  `value` is a scalar or a list (never a mapping), and `tier` is `B`, `C`, or
  `A-range` (the value may move live inside `allowed`, a list of permitted
  values or `{min, max}`; it is reset only when missing or outside).
  `hermes_config_retired_keys` is the one way the role deletes a leaf it does
  not own.

The merge is the `richard_lynch.services.hermes_owned_projection` filter, so a
converge, a probe and the settings service decide "drift" the same way.
Semantics: an owned list is replaced whole; an owned leaf under an unowned
parent keeps its siblings; an unowned leaf next to owned ones is kept; owning a
leaf and its parent, or the same leaf twice (including a wiring leaf), is
refused. With no diff **nothing is written and nothing stops**. With a diff on a
running stack the role stops the stack, **re-reads** the file (anything written
before the stop is on disk now), merges again and writes via a temp-file rename;
the converge then starts it. A first deploy has no file and nothing to quiesce.
A file that exists and cannot be parsed **fails the converge** rather than
reading as empty. `policy/owned-keys.json` (key, tier, bounds, value; secret
values omitted) is rendered beside the compose project for probes and the
settings service to read.

Comments in `config.yaml` are not preserved; Hermes rewrites the file itself on
every boot.

## The dashboard theme: the file is the role's, the choice is the user's

`templates/dashboard-theme.yaml.j2` renders a readable theme (a dark teal
background, warm text, an Inter/system font stack and letter-spacing 0) named
`hermes_dashboard_theme_name`. Its `customCSS` removes the all-caps,
letter-spaced headings: it overrides `.text-display`, `.uppercase`, every
`tracking-*` class and the `--font-rules-*`/`--font-mondwest` display faces. The
role writes it to `data/dashboard-themes/<name>.yaml` through
`scripts/agent_dir.py --write-file` (`data/` is the agent's tree, so the write
walks by file descriptor and refuses a link). Drift is rewritten. The dashboard
discovers themes on each request, so no restart is needed.

Which theme is **active** is `dashboard.theme`, which the dashboard's picker
writes. The role **seeds** it with `hermes_dashboard_theme_seed` only when the
key is absent: written when missing and never compared again, and absent from
`owned-keys.json`, so the policy hook and the settings service never refuse the
UI's write. Every other `dashboard.*` key passes through unchanged.

## The toolset floor, read-only skills, the managed scope

**Full control is an explicit flag, not an empty list.** The default floor
(`hermes_api_server_default_floor`: terminal, file, code_execution, browser,
delegation, cronjob) is applied when the API server is on and
`hermes_toolsets_subtract` has no `api_server` entry.
`hermes_api_server_full_control: true` opts out (for a deployment whose access
control sits at the edge); an absent flag is the floor, so a forgotten setting
is never a silent fail-open. Without the flag, an explicit `api_server: []` is
refused as shell-equivalent.

**The floor subtracts.** The API-server platform enables most of its default set
(`terminal`, `file`, `code_execution`, `browser` among them).
`hermes_toolsets_core` is a declared copy of what the daemon enables at the
pinned tag — read it from the daemon's own `/v1/toolsets` before a floor is
applied, and re-read it on every image bump — and `hermes_toolsets_subtract`
names, per platform, what to remove. The remainder becomes the tier-C leaf
`platform_toolsets.<platform>`. A subtracted name that is not in the core set is
refused (it would subtract nothing and leave the toolset on), as is a floor that
leaves nothing. With the API server enabled the role requires a floor that
removes `terminal`, `file` and `code_execution`, unless full control is chosen.

**`hooks_auto_accept`.** A tier-C leaf declared in inventory. Without it a
configured gateway hook silently never registers (non-interactive runs need it).

**Read-only skills.** `hermes_skills_source` (a controller directory, one
subdirectory per skill holding a `SKILL.md`) is copied to
`<config_dir>/estate-skills` as 0444 files in 0755 directories and mounted `:ro`
at `hermes_skills_mount_path`; the mount path is the one tier-C entry the role
owns in `skills.external_dirs` (in the user layer, because upstream reads
`skills.*` raw from `config.yaml`). `skill_manage` has no external-dir check, so
the mount is the protection. A skill removed from the source is pruned; an empty
or missing source is REFUSED rather than read as "delete everything"; dotfiles
and bytecode in the source are never delivered.

**Managed scope + preflight.** Upstream has no pinned-key list and fails OPEN on
a missing managed file, so the list is the role's: every tier-C leaf.
`_managed_gate.yml` builds the document and refuses an empty pin or a secret
tier-C leaf (the file is world-readable), BEFORE the merge quiesces the stack,
so a refusal leaves the stack up; a rescue starts the stack again if a later
step fails. With `hermes_managed_enforce` on, compose mounts `managed/` and
`policy/` read-only (directory mounts, so a rename is seen), pins
`HERMES_MANAGED_DIR`, and makes `policy/preflight-entry.sh` s6's main program:
`/init` stays PID 1, the preflight runs as the `hermes` user, and only a pass
execs `main-wrapper.sh`. A refusal leaves the gateway down and writes
`hermes_preflight_ok 0` to `hermes_preflight_textfile_dir` for an alert to read;
the converge requires a pass newer than the container's start.

**The preflight parses with the consumer's grammar.** The image has no PyYAML,
so in the container the script imports Hermes' own `hermes_yaml` for the managed
file and python-dotenv's parser for `.env` files, and FAILS `parser` if either
is missing rather than falling back. The interpreter runs `-I`, because the
hermes user's site directory is agent-writable. A `.env` that sets
`HERMES_MANAGED_DIR` (in any spelling either parser accepts) is refused, and so
is one neither can read with certainty. `scripts/preflight_grammar_cases.py`
holds those fixtures in both directions.

## The policy hook and the settings service

Three independent layers keep the settings tiers, and each reads the one
`policy/owned-keys.json`:

1. **The managed scope** (tier C, above) plus the preflight.
2. **The settings service** (`hermes_settings_service_enabled`,
   `hermes_settings_service_src`): a sidecar from the same image that runs the
   caller's own settings application, typically applying tier A through
   upstream's own `hermes config set` and reading it back, turning tier B into
   a draft PR on `hermes_settings_github_repo`, and refusing tier C. The role
   ships no copy of that application: it delivers the `hermes_settings/`
   package found under `hermes_settings_service_src` (root-owned, read-only in
   the container) and runs it with `python -m hermes_settings`, so its code and
   the repository layout it assumes stay with the caller. **Neither this collection nor
   any of its dependencies provides that package**: the option is off by
   default, and the role refuses to enable it unless
   `hermes_settings_service_src/hermes_settings/__main__.py` exists on the
   controller. The policy hook (3) works without it.
3. **The `pre_tool_call` policy hook** (`scripts/hermes_policy_hook.py`,
   `hermes_policy_hook_enabled`): a tier-C leaf in the managed scope, so
   **every** profile carries it, `fail_closed: true`. It blocks file writes
   under any profile's `.env`, `config.yaml`, `plugins/`, `hooks/`,
   `webhook_subscriptions.json`, the policy directory and the managed scope;
   shell or `execute_code` writes that name those paths;
   `hermes config set|unset` on any key the service would not apply live;
   `hermes hooks revoke`, `plugins install|...`, `mcp add|...`. It **escalates**
   every settings-service write to the human approval gate, which fails closed
   where no human is present.

**Every hook the role registers runs the image's interpreter in isolated
mode**: `/opt/hermes/.venv/bin/python -I -B /opt/hermes-policy/<script>.py`,
never an `env python3` shebang. Hermes hands each hook its own environment,
which includes the agent-writable `data/.env` (loaded with `override=True`),
and its hook entries take no environment of their own (a hook spec is event,
command, matcher, timeout, fail_closed). `-I` ignores every `PYTHON*` variable
and keeps the script's and the working directory off `sys.path`; the hooks read
no path from the environment at all. What no interpreter flag can stop
(`LD_PRELOAD`, `LD_LIBRARY_PATH`), the managed-scope preflight refuses: a
`.env` that assigns any `PYTHON*` or `LD_*` name, `HOME`, or a name that once
re-pointed a policy component stops the gateway before it starts. That refusal
assumes a profile `.env` is read only at container start, so an `LD_*` line
planted while the gateway runs is refused at the next start; whether upstream
re-reads a profile `.env` at runtime is unverified. An
`hermes_hooks_expected` entry is the WHOLE command as `hermes hooks list` prints
it.

A hook that is configured but **not registered** denies nothing, so
`hermes_hooks_expected` is declared separately from the enable flag: the health
timer writes `hermes_hooks_registered` / `hermes_hooks_expected` from
`hermes hooks list` (an unreadable list writes 0, failing toward the alert), and
a converge with the hook enabled does not finish until every expected hook shows
allowed. Switching the hook OFF **retires** `hooks.pre_tool_call` from the user
layer.

The catalogue the service reads (`policy/mcp-catalogue.json`) is rendered by
`config_mcp_servers` before this role; with the service enabled, the role
refuses to continue without it.

## The tool-call audit hook

`hermes_audit_hook_enabled` registers `scripts/hermes_audit_hook.py` as a tier-C
leaf `hooks.post_tool_call` (no matcher: every tool, every profile, every
platform including `POST /v1/runs`). Per tool call it appends ONE JSON line to
`data/logs/tool-audit.jsonl`:

| field                        | from upstream's post_tool_call payload                                         |
| ---------------------------- | ------------------------------------------------------------------------------ |
| `ts`, `event`                | written by the hook (`event: tool_call`)                                       |
| `tool`, `profile`            | `tool_name`; `profile` (resolved by upstream at fire time)                     |
| `session_id`, `tool_call_id` | the run's ids, for correlating the calls of one run                            |
| `duration_ms`, `outcome`     | `extra.duration_ms`; `extra.status` (`ok`, `error`, `blocked`)                 |
| `error_type`                 | `extra.error_type` (an exception class name or upstream's label), when present |

It is built from an ALLOWLIST: the arguments, the result, `error_message`, the
working directory and the middleware trace are never written, and an identity
field carrying free text is written as `invalid`. It prints nothing and exits 0
on every path. Shipping the file to a log store is the calling play's job. Put
its command in `hermes_hooks_expected`.

## Automations, the send gate and the agent's mailbox

All off by default; each is an explicit inventory decision.

| piece                     | options                                                                                                                                            | what the role does                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| ------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Webhook listener**      | `hermes_webhook_enabled`, `_port`, `_bind_address`, `_secret`, `_routes`, `_retired_routes`, `_route_secrets`                                      | publishes the port; `WEBHOOK_SECRET` (and any `secret_env` value) in `app.env`; every route field a **tier-C** leaf under `platforms.webhook.extra.routes`, so `toolsets` and `deliver` cannot be changed from inside. Refuses a short secret, a route with an inline `secret:`, a `secret_env` with no value, a non-slug name and a `deliver_only` route delivering to `log`. A route dropped from the vars stays live until named in `_retired_routes`. Read back through the listener's own `/health`.                                                                 |
| **Send gate**             | `hermes_hub_gate_enabled`, `_allowed_recipients`, `_protected_paths`, `_matcher`                                                                   | stages `scripts/hub_gate.py` and its config in the read-only policy mount as a **fail-closed** `pre_tool_call` shell hook. A terminal or `execute_code` call that mentions mail in any normalised form (shlex-unquoted, quote-joined, escape-decoded) is escalated for approval unless every mail-mentioning segment is on a narrow read allowlist; a send skips approval only when every recipient is visible and allowlisted. A write to a protected path (also through a symlink) is blocked. Needs `hermes_managed_enforce`. **A belt, not the boundary**: see below. |
| **Seeded cron jobs**      | `hermes_cron_seed`, `hermes_cron_scripts_source`, `_scripts_config`, `hermes_cron_env_passthrough`, `hermes_cron_env`, `hermes_cron_paused_reason` | stages the scripts root-owned outside the agent's tree and mounts them read-only at `scripts/estate/`; creates each absent seeded job **paused** through `hermes cron create`, edits a drifted owned field back through `hermes cron edit`, and reads `cron/jobs.json` back. Never enables a job. Jobs not in the seed are never touched.                                                                                                                                                                                                                                 |
| **Dynamic subscriptions** | `hermes_webhook_subscriptions_readonly` (default **true**)                                                                                         | ON: mounts a root-owned empty store READ-ONLY over the agent's `webhook_subscriptions.json`, so nothing can create or widen a dynamic route; refuses while the agent's file still holds routes. OFF: a `toolsets` key on any entry is removed on every converge.                                                                                                                                                                                                                                                                                                          |
| **Agent mailbox**         | `hermes_email_*`                                                                                                                                   | the email adapter's `EMAIL_*` in `app.env`; refuses an enabled adapter with an empty allowlist or no `authserv_id` (upstream drops every message without it).                                                                                                                                                                                                                                                                                                                                                                                                             |

The comparison logic is in the `hermes_cron_seed_plan`, `hermes_cron_argv` and
`hermes_dynamic_routes_audit` filters, so the converge and any probe cannot
disagree. A cron schedule is written `every <N>m` or as a 5-field cron
expression: the daemon stores an interval as minutes (`every 1h` becomes
`every 60m`), and the plan normalises that.

**The real send boundary is the credential, not the hook.** The gate reads the
text of a tool call, and a recipient computed at run time is past any reader. So
the agent's mail credential must not carry a send-capable scope: for Gmail,
`users.messages.send` accepts `gmail.send`, `gmail.compose`, `gmail.modify` and
`https://mail.google.com/`, so give the agent's token `gmail.readonly` (plus
`gmail.labels`) and route sends through a separate approval-gated tool holding a
credential the agent cannot read.

## Kanban → Slack card threads

When `hermes_kanban_notify` is set, the role installs
`scripts/hermes_kanban_notify.py` and a 30 s systemd timer **on the guest**. The
script tails the board's `task_events` (sqlite, read-only) and gives each card
in scope ONE Slack thread: the root names the card, and each state change is a
reply. It posts through Hermes's own Slack adapter
(`docker exec ... hermes send --json`), so it holds no credential. Its cursor
and card → thread map are root-owned state written after every post, so a
restart never reposts. The read-back runs one tick and requires success and a
cursor.

## The GitHub CLI for the Kanban PR completion contract

A card created with `--completion-contract OWNER/REPO` completes only when
upstream's gate has read the PR's head, the branch's required checks and their
check-runs with `gh api`, run as a subprocess INSIDE this container with the
assignee profile's own `GH_TOKEN`. The published image ships no `gh`, so without
it every contract card fails as `infra` ("GitHub acceptance evidence unavailable
or incomplete"); a refused credential reads `auth`, not `infra`.
`hermes_gh_cli_version` + `hermes_gh_cli_sha256` stage the official release
tarball, sha256-verified, root-owned under `<hermes_config_dir>/tools`, and
FILE-mount the binary `:ro` at `/usr/local/bin/gh`. The version is in the source
path, so an upgrade recreates the container.

## Why the role renders `.env` and `config.yaml` instead of running `hermes setup`

Hermes's documented first-run path is an **interactive wizard** that writes
`/opt/data/.env` itself, which cannot be driven from a play and would not be
idempotent. Upstream's Docker guide states that API keys are read from
`/opt/data/.env` and that environment variables may be passed directly instead —
so rendering the environment is the supported unattended path. The container's
`cont-init` hook only **seeds a missing** `.env`, so the managed file is never
clobbered.

**`config.yaml` is owned by uid/gid 10000, not root.** The container seeds it as
`10000:10000` and its services drop to that user, so a root-owned `0600` file
would be unreadable — Hermes would fall back to seeded defaults and silently
ignore every setting.

## The dashboard's auth gate is not optional

The dashboard **stores API keys** and can drive tool execution, so it is a
remote-code-execution surface rather than a web app; upstream removed the
insecure escape hatch (`HERMES_DASHBOARD_INSECURE` is a no-op). Consequences
this role relies on:

- The dashboard binds `0.0.0.0` **inside** the container (required for the
  published port).
- On a non-loopback bind with **no auth provider**, Hermes **fails closed at
  startup**. The container stays up, so this presents as "the route 502s" —
  which is why the role gates on the password on the controller, before compose
  runs.
- The role asserts the dashboard does **not** answer `200` to an unauthenticated
  request.

## Model connection: an OpenAI-compatible gateway

**Hermes holds no provider credential at all.** All model traffic routes through
a gateway (LiteLLM, for example), presenting a scoped **virtual key** — one
provider key, one spend ledger, one rotation, one place where models are
configured. The mechanism is a `providers.<name>` entry in the agent's own
`config.yaml`:

```yaml
providers:
  gateway:
    api: http://gateway.example.com:4000/v1
    key_env: HERMES_LITELLM_API_KEY # the NAME; the value is in app.env (0600), never here
    session_affinity_header: x-litellm-session-id
model:
  provider: gateway
  default: example/model-id
  api_mode: chat_completions
```

Three things here are easy to get silently wrong:

1. **The key is `api:`, not `base_url:`.** `base_url` belongs to the _legacy_
   top-level `custom_providers:` list. In the `providers:` dict the key is
   `api:`, and using the wrong one is silent. Molecule asserts `api` is present
   **and** `base_url` absent.
2. **There is no generic chat `OPENAI_BASE_URL` environment variable** in this
   project, so an env-var approach is a no-op that _looks_ configured.
3. **`session_affinity_header` is worth setting**: without it a session-aware
   proxy treats nearly every request as a new conversation and re-sends the
   whole history each turn.

The merge retires an inline `providers.<name>.api_key` on every converge, and a
HARD GATE refuses a `data/.env` that carries the `key_env` variable (it is
loaded after the container environment and would win).

**Use a virtual key, never the gateway's master key**, and mint it outside the
deploy: LiteLLM's `/key/generate` is not idempotent, so minting inside a
converge would create a new spendable key on every run. When rotating, prove the
superseded key is rejected — changing what Hermes presents does not revoke what
the gateway accepts.

`hermes_default_model` is required. A gateway's `/v1/models` is not always the
full picture (some routed ids do not enumerate), so verify a model with a real
completion.

## The API server is off by default

A caller holding `API_SERVER_KEY` **drives the agent**, which executes tools.
Upstream's minimum key length is 8 characters; this role gates on **32**.

**`app.env` is the source of the key, and `data/.env` carries the SAME value.**
Upstream's stage2 hook generates an `API_SERVER_KEY` into `data/.env` on any
boot where the container env has none, and Hermes loads that file with
`override=True`, so a generated key would beat the one in `app.env`. And once a
named profile exists, the API server reads its key from the default profile's
`.env` and ignores the compose `env_file`. So the role writes the `app.env` key
into `data/.env` on every converge (one value, two files, rotated together), and
the cold `/v1/models` read-back after the restart proves the enforced key is the
declared one.

## Safety notes

- **Put an access gate in front of anything published.** Hermes holds a key that
  spends money and runs tools against whatever it can reach. This role publishes
  ports on the guest only; exposing them further is the caller's decision.
- **Safe to run twice.** The stack restarts only when its rendered config
  actually changed (`deployment_config_changed`, `app.env`, `data/.env`, the
  policy files).
- **`app.env` is rendered `0600`**, separately from the compose file's `0640`.
  Molecule asserts no credential placeholder ever appears in the compose file.
- **Disk.** The image is over a gigabyte compressed and bundles a full Chromium;
  a superseded tag is not removed by `docker image prune -f` while it is still
  tagged. Size the guest accordingly.

## What this role deliberately does NOT do

- **No chat platform by default.** Slack (Socket Mode) is the one wired: off
  unless `hermes_slack_enabled`, and then it needs both tokens (`xoxb-`,
  `xapp-`) and a non-empty `hermes_slack_allow_from` of member ids, or the role
  refuses. Upstream denies everyone when the allowlist is empty while the
  adapter still connects. Other platforms are not wired: each is its own inbound
  path into an agent that executes tools.
- **No OAuth or OIDC** for the dashboard; basic auth is upstream's
  zero-infrastructure option for a trusted network.
- **No metrics endpoint scrape or alert rules.** Hermes exposes no Prometheus
  endpoint (`/metrics` is the SPA's catch-all route); the textfile gauges above
  are the signals. Alerts belong to the calling play.
- **It does not converge what the agent does to itself.** Hermes writing its own
  memory, skills state, sessions or Kanban board is the application's runtime
  data inside its own guest, and a converge of this role never reconciles it.
  The image, listeners, `.env`, the owned leaves of `config.yaml`, credentials
  and the firewall are platform state and change only through the role.

## Testing

```sh
cd roles/service_hermes && molecule test
```

**Coverage is PARTIAL, honestly.** The scenario needs no Docker daemon: it
proves the templates render and that the owned-leaf merge, the pin gate, the
managed scope and its preflight, the policy files, the automations and the
delivery tasks behave on real files; it does not prove the agent runs. Running
state is proven by the role's own read-backs on a real guest.

Three compose variants are rendered, because an always-true guard is
indistinguishable from a correct one in a single render:

| Variant    | Dashboard | API server | Proves                                                           |
| ---------- | --------- | ---------- | ---------------------------------------------------------------- |
| `full`     | on        | on         | every block present; both ports published                        |
| `minimal`  | on        | off        | the API server block and port are **absent**                     |
| `headless` | off       | on         | the dashboard block, its port and the healthcheck are **absent** |

The merge play seeds a live config with unowned keys, stale owned leaves and an
in-bounds A-range leaf; `idempotence` requires `changed=0`; verify asserts on
parsed files that unowned keys survived, owned leaves were restored, the
in-bounds leaf was left alone, `owned-keys.json` carries no secret, a no-drift
run writes nothing, an unparseable file stops the converge untouched, and each
malformed image pin is refused. A concurrent-write fixture (`race_case.yml`)
runs a dashboard-style writer through the real merge, with a CONTROL that must
show a lost update, so the fixture cannot pass by being blind. The scripts have
their own unit tests (`scripts/test_*.py`), and the filters theirs.

## References

- Upstream repo: <https://github.com/NousResearch/hermes-agent> (MIT)
- Docker guide (ports, image channels, dashboard auth, where state lives):
  `website/docs/user-guide/docker.md` in that repo
