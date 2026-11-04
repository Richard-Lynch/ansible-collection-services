# service_litellm

Deploys **LiteLLM** — an OpenAI-compatible gateway in front of several LLM
providers — together with its **Postgres** database, an optional **Redis** for
state shared across the proxy's workers, and an **Open WebUI** chat frontend, as
a single docker-compose stack.

One role rather than two because the containers are one deployable unit: Open
WebUI is configured against the proxy's compose-internal address, so splitting
them would mean a role that cannot converge without the other and a cross-role
variable carrying a container name.

## What it gives you

|                               |                                                                                                                                                                                               |
| ----------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `http://<host>:4000`          | OpenAI-compatible API — point any OpenAI client at it                                                                                                                                         |
| `http://<host>:4000/ui`       | LiteLLM admin UI (virtual keys, spend, model management)                                                                                                                                      |
| `http://<host>:4000/metrics/` | Prometheus metrics — on v1.104.2 they **require the master key** as a bearer token. Note the **trailing slash**: `/metrics` answers 307 and Prometheus does not follow redirects on a scrape. |
| `http://<host>:8089`          | Open WebUI chat frontend, pre-wired to the proxy                                                                                                                                              |

Beyond chat, the same endpoint serves `/v1/embeddings`,
`/v1/images/generations`, `/v1/audio/speech`, `/v1/audio/transcriptions`,
`/v1/moderations`, `/v1/responses`, `/v1/files`, `/v1/batches` and
`/v1/vector_stores` with no extra configuration. See
[Non-chat capabilities](#non-chat-capabilities-what-works-and-why-none-of-it-is-configured-here).

Every model the gateway enumerates appears in Open WebUI automatically: the UI
treats the proxy as its OpenAI backend
(`OPENAI_API_BASE_URL=http://litellm:4000/v1`), so adding a provider needs no
change on the UI side.

## Inputs

See [`meta/argument_specs.yml`](meta/argument_specs.yml) for every option of the
three entrypoints (`main`, `open_webui_connections`, `open_webui_task_model`).

Four options are **required and have no default**, because they are credentials
the calling play supplies from its secret store: `litellm_master_key`,
`litellm_salt_key`, `litellm_postgres_password`,
`litellm_open_webui_secret_key`. `litellm_redis_password` is required too while
`litellm_redis_enabled` is true (the default).

`litellm_ollama_api_base` also has no default deliberately — the address of an
Ollama host is an installation fact, so it belongs in inventory, and enabling
Ollama without it fails a preflight assert rather than rendering an entry that
points nowhere.

## Minimal usage

```yaml
- name: Deploy the LiteLLM gateway
  ansible.builtin.include_role:
    name: richard_lynch.services.service_litellm
  vars:
    litellm_master_key: "{{ vault_litellm_master_key }}" # must start with sk-
    litellm_salt_key: "{{ vault_litellm_salt_key }}" # create once, never rotate
    litellm_postgres_password: "{{ vault_litellm_postgres_password }}"
    litellm_redis_password: "{{ vault_litellm_redis_password }}"
    litellm_open_webui_secret_key: "{{ vault_open_webui_secret_key }}"
    litellm_anthropic_api_key: "{{ vault_anthropic_api_key | default('') }}"
    litellm_network: gateway-net
    litellm_open_webui_name: "Example LLM"
  no_log: true
```

## Providers

A provider block is rendered **only if its key resolved non-empty**. This
matters: a `model_list` entry with an empty `api_key` loads fine and fails at
_request_ time, so an absent key would present as a working proxy that errors on
use. Resolve a provider you have not onboarded yet to `''`, and the role omits
it.

The role **hard-gates on at least one provider being configured** — a gateway
with an empty `model_list` answers every request with a routing error, and that
is worth failing the play for.

Wildcard model names (`anthropic/*`, `openai/*`, `gemini/*`, `openrouter/*`,
`elevenlabs/*`) are used per provider, so a new upstream model is usable the day
it ships with no config change. Ollama is listed explicitly because Ollama has
no wildcard support.

### Making OpenRouter models pickable

The `openrouter/*` wildcard **routes** every OpenRouter model but **enumerates
none**: OpenRouter has no provider model-info class on LiteLLM v1.104.2, so it
contributes nothing to `GET /v1/models`, and Open WebUI builds its picker from
exactly that endpoint. Each id in `litellm_openrouter_models` is rendered as its
own `model_list` entry beside the wildcard, which makes it selectable. The
wildcard stays, so the list can only ever lag a preference, never break a route.

### Stable aliases

`litellm_model_aliases` declares local model **names** that clients ask for
instead of a vendor id (`{name, model, fallback?}`), so re-pointing a model is a
one-line inventory change and no client moves. The role refuses an alias whose
provider has no key, and a `fallback` that is not another alias on a
**different** provider (a fallback on the same provider fails with the thing it
covers).

### Ollama is wired but OFF by default

`litellm_ollama_enabled` defaults to `false`: a rendered entry with no Ollama
host behind it would advertise models that cannot answer. Enable it from
inventory, with `litellm_ollama_api_base` and `litellm_ollama_models`, once an
Ollama host exists.

## Non-chat capabilities: what works, and why none of it is configured here

The gateway is not chat-only. Embeddings, image generation, text-to-speech,
transcription, moderation, tool calling, the Responses API, files, batches and
vector stores **all work**, and **none of them needs a config change** — worth
writing down, because the instinct on finding a capability list is to go and
wire each one.

They work because `model_list` carries a **wildcard per provider**. A wildcard
is not chat-scoped: LiteLLM resolves the concrete model name against its own
cost map at request time and picks the mode from there. So
`openai/text-embedding-3-small` routes to `/v1/embeddings`,
`openai/gpt-image-1-mini` to `/v1/images/generations`, and
`openai/gpt-4o-mini-tts` to `/v1/audio/speech`, all from the single wildcard
entry. `/model_group/info` reports each one's inferred `mode`, and every one of
these models is listed by `/v1/models`, so clients discover them without help.

**Adding explicit per-model entries would therefore add no capability** — only a
second, hand-maintained list of model names to drift out of date. That is why
this role has no `litellm_embedding_models`-style option. The wildcard is the
feature.

What it does mean is that the capabilities are **untested by construction** —
nothing in the deploy proves an embedding still returns a vector. Cover that
with a separate, opt-in check that _uses_ each billed capability and asserts on
what came back (a vector of the right dimension, PNG magic bytes, a TTS→STT
round trip, a tool call whose arguments parse); keep it off a timer, since it
spends real money.

### Web search: a search TOOL, not a model

Brave Search is wired by `litellm_brave_search_api_key`, and the thing worth
knowing is that it is **not a provider**. On v1.104.2 search providers live in a
registry of their own:

- `brave` appears **nowhere** in `litellm.provider_list` and has **no cost-map
  entry** — so grepping the installed package for it reads as "unsupported";
- `/search_tools/ui/available_providers` nonetheless lists it as **"Brave
  Search"**.

So it is configured through a **top-level `search_tools:` block** (read by
`config.get("search_tools")` in `proxy_server.py`), with the schema
`search_tool_name` + `litellm_params.search_provider` from
`litellm/types/search.py` — _not_ a `model_list` entry. The proxy serves it at
**`POST /v1/search/<name>`**, and also accepts the name in the body at
`POST /v1/search`.

One trap: a generic `auth_type: api_key` would be **wrong** for Brave. That form
sends `Authorization: ApiKey ...`, while Brave reads **`X-Subscription-Token`**.
The config therefore references `os.environ/BRAVE_API_KEY` and lets
`BraveSearchConfig.validate_environment` build the header itself.

### Text to speech via ElevenLabs

`litellm_elevenlabs_api_key` adds an `elevenlabs/*` wildcard to `model_list`,
because ElevenLabs **is** a native provider: `LlmProviders.ELEVENLABS` is in
`provider_list`, and the cost map carries `elevenlabs/*` models whose modes are
`audio_speech` and `audio_transcription`. LiteLLM maps the **OpenAI voice
names** (`alloy`, `sage`, `onyx`, …) onto ElevenLabs voice ids, so an existing
OpenAI TTS client reaches it by changing only the model name.

A key scoped for synthesis only (without `user_subscription_read`) answers
`GET /v1/user/subscription` with **401 `missing_permissions`** while synthesis
works — do not read that 401 as a dead key; assert on returned audio instead.

### The MCP gateway: one server, and why only one

`litellm_mcp_context7_enabled` registers the hosted **Context7** documentation
server. The block is a **dict keyed by server name** —
`load_servers_from_config` iterates `.items()`, so a list does not merely look
different, it fails to load — with fields from `MCPServer` in
`litellm/types/mcp_server/mcp_server_manager.py` (`transport` ∈ sse/http/stdio,
`auth_type` ∈ none/api_key/bearer_token/…).

Context7 is `transport: http` + `auth_type: none`: hosted, no credential, so no
secret plumbing and no image change. It answers MCP `initialize` and lists
`resolve-library-id` and `query-docs`. Probe any new server the same way first:
**an entry pointing at a dead URL registers fine and simply exposes no tools**,
which is indistinguishable from an unconfigured gateway.

| Not registered                                                   | Why not                                                                                                                                                                                                                           |
| ---------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Brave**                                                        | **No hosted MCP endpoint exists.** Brave's own server defaults to `stdio`, and its HTTP transport is one you run yourself — there is no URL to point `mcp_servers` at. Brave _web search_ above is a separate, working mechanism. |
| servers with real blast radius (source control, home automation) | Tool execution against repositories or a house should wait for per-virtual-key scoping (`x-mcp-servers`) rather than sit behind the master key, which every client necessarily holds.                                             |
| anything `stdio`                                                 | A `stdio` server is executed **inside** the litellm container, which ships no `node`/`npx`. That needs a derived image.                                                                                                           |

**What the deploy proves, and what it does not.** The role reads
`/v1/mcp/server` back and hard-fails if `context7` is not registered — but that
is **registration only**. It does not prove a tool can be _invoked_; that needs
an MCP session against the upstream.

### Other capabilities

| Capability                     | State                                                                                                                                                             |
| ------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Rerank                         | Routes, but needs a rerank provider key (Cohere/Jina); without one `/v1/rerank` returns "no healthy deployments" — correct behaviour for an unconfigured feature. |
| Guardrails / prompt management | **Runtime tenancy state**, created through the admin API, not deployment config — the same boundary that keeps virtual keys and budgets out of this role.         |
| Vector stores                  | Work. Deliberately **not pre-created**: a vector store holds data, and which corpora exist is a decision, not infrastructure.                                     |

`general_settings.public_routes` is **Enterprise-gated and silently no-ops** on
the free tier — `route_in_additonal_public_routes` returns False before it ever
reads the config. Treat any LiteLLM feature as premium-gated until proven
otherwise **on the build you run**, because the failure mode is a config that is
accepted and ignored rather than rejected.

## Branding

Every branding option defaults to empty, which renders nothing — each UI keeps
its own upstream look. `litellm_open_webui_name` sets Open WebUI's `WEBUI_NAME`
(default `Open WebUI`).

**LiteLLM admin UI** (`litellm_ui_logo_path`, `litellm_ui_logo_path_dark`,
`litellm_ui_favicon_path`) sets `UI_LOGO_PATH` / `UI_LOGO_PATH_DARK` /
`LITELLM_FAVICON_URL` as plain container env vars. In v1.104.2's
`proxy_server.py` (`/get_image`, `/get_favicon`) these are bare `os.getenv()`
reads with **no `premium_user` gate**, so they work on the free tier. An
`http(s)` value is fetched by the **browser**, not the proxy — for an
internal-only UI that means the browser makes a request out to the public
internet on every page load. For a **local** path, the role mounts
`<litellm_config_dir>/branding` onto `/app/branding:ro` whenever any of the
three options is set: set e.g. `litellm_ui_logo_path: /app/branding/logo.png`
and place the file at `<litellm_config_dir>/branding/logo.png` on the host (the
caller's job; the role creates only the mount).

**Open WebUI** (`litellm_open_webui_logo_path`,
`litellm_open_webui_favicon_path`) has **no** logo/favicon env var in v0.11.4:
on every process start it deletes everything under `STATIC_DIR` and re-copies
the bundled assets, so a file placed at `static/logo.png` is wiped on the next
restart. These options bind-mount a **host file** read-only directly over that
one asset path; OWUI's own copy step only logs the failed overwrite, so the
custom file survives. The role asserts each configured path exists **on the
target host** before deploy — Docker would otherwise silently bind-mount an
empty directory at a missing source path.

**Open WebUI's own `LICENSE`** permits removing or replacing its name/logo
branding (including `WEBUI_NAME`) only for a deployment with **50 or fewer end
users**, or under an enterprise license. Check that condition before setting any
of these.

## Open WebUI connections beyond LiteLLM (`tasks_from: open_webui_connections`)

A second entrypoint adds other OpenAI-compatible backends (for example an
agent's API) to Open WebUI's model picker without touching the LiteLLM
connection the stack deploys with.

**Why not the compose env.** Open WebUI 0.11 seeds every setting into its
`config` table on first boot and reads `openai.*` from there, so
`OPENAI_API_BASE_URLS` is ignored once the instance exists.
`ENABLE_PERSISTENT_CONFIG=false` would make the environment win for _every_
setting and throw away the administrator's admin-UI choices. The entrypoint uses
`POST /openai/config/update` instead, the same call the admin UI makes. It is
live without a restart.

**How it authenticates.** It mints a two-minute session JWT _inside_ the Open
WebUI container from that container's own `WEBUI_SECRET_KEY`, for the
earliest-created admin, and calls the API on loopback. The signing key never
leaves the host and no standing admin credential exists. This works because Open
WebUI's token is an HS256 JWT carrying only the user id, checked against a
revocation list only when Redis is configured for Open WebUI (`utils/auth.py` at
v0.11.4). Re-read that before bumping the image.

**What it changes.** It matches connections by URL. A new URL is appended, and a
present one has its key replaced and its config keys merged. Every other
connection keeps its index, URL, key and config, and a HARD GATE checks that
before anything is written (Open WebUI keys configs by index, so moving the
LiteLLM connection would hand it another connection's config). It reports by
default and writes only with `litellm_open_webui_connections_apply: true`. It
then reads back the connection list and `/api/models`, and fails if a declared
model id is not in the picker.

**Who sees the model.** Open WebUI shows a model that has no `model` table row
**to admins only**, and refuses it to everyone else (`get_filtered_models` /
`check_model_access`). The entrypoint creates no model row, so a connection
added here stays admin-only until someone grants access in the admin UI. That
matters for an agent backend that executes tools: an agent's interactive
approval prompts (for example streamed `approval.request` events) are not
rendered by Open WebUI, so a turn that needs one stalls and is then refused.

**Background tasks: `tasks_from: open_webui_task_model`.** Open WebUI runs
title, tag and follow-up generation on `task.model.external`. Upstream leaves it
empty, which means the chat's own model, so a chat with an agent model would
also start agent turns for those prompts. This sibling entrypoint sets that one
key to `litellm_open_webui_task_model_external` (typically a cheap alias). The
id must appear in `/api/models`. `POST /api/v1/tasks/config/update` replaces
every task setting at once, so the entrypoint writes the full config it just
read with only that key changed, and a HARD GATE fails if any other key reads
back different. It reports by default and writes only with
`litellm_open_webui_task_model_apply: true`.

## Safety notes

**`litellm_salt_key` is IMMUTABLE.** It encrypts provider credentials at rest in
Postgres. If it changes, every key already stored becomes undecryptable and
there is no recovery but re-entering them through the admin UI. Create it once,
never rotate it, and have the calling play use a create-if-absent secret step
rather than a generate-or-fail one.

**This endpoint spends real money.** It holds live provider credentials and will
bill anyone who reaches it with the master key. If you publish it, put an
authenticating proxy in front (a token-only gate, so no browser session can
reach `/ui`, whose password is the master key unless
`litellm_disable_env_credential_login` is true), give every client its own
scoped **virtual key** (budgeted, revocable alone) and never the master key.
Verify a publish from **off-network**: with split-horizon DNS an on-LAN check
never passes through the gate it means to test.

**`litellm_disable_env_credential_login` (default true) needs an admin account
first.** It stops the master key doubling as a proxy-admin console login.
LiteLLM refuses a password on `/user/new`, so the account is created from an
invitation link; set the option false only to recover a lost account.

**Open WebUI's first account cannot be created by this role** — Open WebUI only
accepts it through its own signup endpoint, and `ENABLE_SIGNUP: false` stops
every account _after_ the first. Until that first admin exists the signup page
is open to anything that can reach the UI, so create it immediately after the
first deploy — and again after wiping its `data/open-webui` volume.

**`/health` is not a health check here.** It requires auth _and_ issues a real,
billed API call to every configured model on every hit — upstream's own
docstring says _"USE `/health/liveliness` to health check the proxy"_. The role
and the compose healthcheck both use `/health/liveliness`, which is unprotected
and free. Never point a monitor at `/health`.

**There is no host allowlist on Open WebUI.** It has no `*_ALLOWED_HOSTS`
variable; the nearest equivalent is `CORS_ALLOW_ORIGIN`, which is
**semicolon**-separated and needs only one origin because FastAPI serves the
built frontend and the backend from the same origin. A comma-joined value fails
its scheme/netloc validation outright.

**Data directories are owned per service.** Redis and Postgres servers run as
root but write through child processes running as the image's own user, so a
root-owned data directory passes every manual write test and still fails every
background save / every query. The role owns each directory by the uid/gid in
`litellm_redis_uid`/`_gid` and `litellm_postgres_uid`/`_gid` (999 and 70 in the
alpine images).

## What this role deliberately does NOT do

- **Create a DNS name, a reverse-proxy route or an authenticating gateway.**
  Those belong to the calling play. A `service_*` role does not own its
  service's _uses_.
- **Register Prometheus scrape targets, alert rules or uptime monitors.** If you
  monitor the gateway, probe **`/health/liveliness`** — not the bare host (the
  API, which 404s an unauthenticated GET) and never `/health`.
- **Create the host, install Docker or configure networking.**
- **Back up the Postgres volume.** Spend history and virtual keys live there;
  back the host or the volume up by other means.
- **Manage virtual keys or budgets.** Those are runtime state in Postgres,
  created through the admin UI or API. Ansible owns the deployment, not the
  tenancy.

## Which version is actually running

`litellm_image` is the DESIRED version; a changed pin changes nothing until the
play is re-run. To read what is really running, read the image of the live proxy
container AND the `litellm` package version inside it, and fail rather than
report a blank. Two obvious reads are wrong on v1.104.2:
`docker ps --filter ancestor=ghcr.io/berriai/litellm` (no tag) matches
**nothing** against a running tagged container, and `/health/readiness` carries
no `litellm_version` field.

## Testing

`molecule/default` is **render-only** and honest about it: this role needs a
Docker daemon, a live Postgres and a reachable proxy. The scenario renders the
templates for **two** variants — all providers set, and only one set — and
asserts structurally (`from_yaml`-parsed, not just substring-matched) that:

- images are pinned, every service exists on the external network, both ports
  map
- LiteLLM depends on a **healthy** Postgres (`pg_isready`), so it cannot race
  its DB on a cold boot
- Open WebUI points at the proxy by service name and has signup disabled
- the healthcheck uses `/health/liveliness`, never the billed `/health`
- unconfigured providers are **omitted entirely**, and no entry carries an empty
  `api_key`
- `config.yaml` references credentials as `os.environ/NAME` only — no literal
  secret in a file that lands `0640` on the host
- `app.env` exports exactly the configured providers
- `docker-compose.yml` (also `0640`) carries **no credential**: the Postgres
  password, Open WebUI's backend key (the master key) and secret key, and the
  Redis password are each in a `0600` env file the service names by `env_file`
  (`postgres.env`, `open-webui.env`, `redis.env`); Redis takes `requirepass`
  from `redis/redis.conf` (`0600`, the redis uid's, a read-only directory
  mount), so the password is on no command line (`ps`, `docker inspect`), and
  its healthcheck authenticates through `REDISCLI_AUTH`

Both directions are tested on purpose: rendering a single variant cannot
distinguish a correct conditional from an always-true one.

The `open_webui_connections` merge is tested by running the role's own
`tasks/_owui_connections_merge.yml` over stub lists in three cases: append,
already-converged (no change) and replace-in-place.

The live converge is proven by the role's own read-backs: every container
running (reported as `<name>=<status>`, with `ABSENT` for one that does not
exist), a 200 from `/health/liveliness`, a **non-empty parsed** `/v1/models` (so
a provider block that failed to load cannot present as "no models yet"), the
search tool and MCP server registered by name when configured, and a 200 from
Open WebUI. The compose tasks deliberately carry **no `wait:`**: it would impose
a second, stricter deadline in front of those retrying read-backs and fail the
play while a cold-boot migration is still legitimately running.
