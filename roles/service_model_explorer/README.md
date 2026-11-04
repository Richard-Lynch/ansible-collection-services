# service_model_explorer

Serves the **model discovery app** — a read-only UI listing every model a
LiteLLM gateway can reach, labelled by _how_ it is reachable — as static files
behind a pinned nginx, usually on the same host as the gateway.

## The problem it exists to solve

A LiteLLM gateway can **route** far more models than it can **enumerate**:

| source                      | meaning                                             |
| --------------------------- | --------------------------------------------------- |
| `GET /v1/models`            | what Open WebUI can put in its picker               |
| OpenRouter's catalogue      | what `openrouter/*` routes                          |
| `litellm_openrouter_models` | what is declared explicitly, so in the picker today |

`openrouter/*` **routes** every OpenRouter model and **enumerates none of
them**: OpenRouter has no provider model-info class on LiteLLM v1.104.2
(`get_provider_model_info()` → `None`, no cost-map entries), so the wildcard
contributes nothing to `/v1/models`, while `anthropic/*` and `openai/*` _do_
expand. The only enumerated `openrouter/` ids are the ones declared explicitly.

So a model can work perfectly and be invisible to the picker. The app shows both
populations in one list and labels every row.

## Inputs

See [`meta/argument_specs.yml`](meta/argument_specs.yml). The one required
option is `model_explorer_catalogue_src`: the path **on the controller** of the
catalogue snapshot (`catalogue.json`) the app serves. Everything else has a
generic default (container name, pinned nginx image, config directory, Docker
network, host port, timezone, and an optional `model_explorer_dist_dir` for a
bundle built somewhere other than the role's `files/app/dist`).

### The catalogue snapshot

The snapshot is the caller's data. Its shape is defined (and validated in the
browser) by [`files/app/src/schemas.ts`](files/app/src/schemas.ts):

```json
{
  "generated_at": "2026-01-01T00:00:00Z",
  "gateway_host": "gateway.example.com",
  "counts": {
    "enumerated": 2,
    "openrouter_catalogue": 1,
    "declared_picker": 1
  },
  "enumerated": ["anthropic/claude-example", "openrouter/vendor/model-a"],
  "declared_picker": ["openrouter/vendor/model-a"],
  "openrouter": [
    { "id": "vendor/model-a", "name": "Model A", "context_length": 128000 }
  ]
}
```

Produce it from the gateway's `GET /v1/models` (with the master key, on the
gateway's loopback, under `no_log`, writing out model **ids only**) and
OpenRouter's public, unauthenticated `/api/v1/models`. `generated_at` should
mean "when the data last changed", so an unchanged export keeps the previous
timestamp and stays idempotent.

## Usage

```yaml
- name: Deploy the model discovery app
  hosts: gateway
  tasks:
    - name: Serve the model explorer
      ansible.builtin.include_role:
        name: richard_lynch.services.service_model_explorer
      vars:
        model_explorer_catalogue_src: "{{ inventory_dir }}/files/catalogue.json"
        model_explorer_network: gateway-net
```

Build the bundle on the controller first (the deployed artefact is exactly what
was reviewed, and the host needs no node toolchain):

```bash
cd roles/service_model_explorer/files/app && npm ci && npm run build
```

For `npm run dev`, set `MODEL_EXPLORER_CATALOGUE` to a snapshot path; the dev
server serves it at the same `/catalogue.json` the deployed app fetches.

`dist/` and `node_modules/` are build artefacts, so the role **hard-gates** on a
built bundle rather than deploying an empty directory that nginx would serve as
404s, and separately on the snapshot existing and being non-empty — "you did not
build" and "there is no catalogue" are different mistakes, and the second would
deploy an app whose every load says "could not load".

The bundle is synchronised with `delete`, so a rebuild's content-hashed assets
do not accumulate on the host. The snapshot is shipped on its own (excluded from
the sync), so refreshing it needs no rebuild.

## The write path — and why it is a file, not an API call

**"Add to Open WebUI" stages a model; an Ansible play applies it.** The checkbox
renders the desired-state block for `litellm_openrouter_models`; a human pastes
it into the inventory, commits, and converges. The app **mutates nothing and
holds no credential**.

The alternative — POSTing to the gateway's `/model/new` — is rejected for three
reasons:

1. **The repo is the source of truth.** `litellm_openrouter_models` is curated
   by hand on purpose: choosing which models a human sees is a preference, not a
   mirror of an upstream catalogue. Writing to the gateway's Postgres instead
   puts configuration where a rebuild does not reproduce it.
2. **No credential in the browser.** `/model/new` needs an admin credential, and
   the master key is the admin-UI password and spends real money. A scoped
   virtual key would narrow that but would still mean a writable gateway
   credential served from a static file.
3. **It stays reviewable.** `store_model_in_db: true` means a `/model/new`
   registration persists across restarts, so mistakes made that way would be
   durable _and_ invisible to the repo.

The cost is honest: it is not one click. That friction is the same friction that
keeps the declared list reviewable, and the UI says so rather than implying the
checkbox deployed anything.

## This service holds NO credential

- the role declares **no credential options at all**;
- nothing in the compose project, the nginx config or the served bundle carries
  a key, and molecule **asserts** that
  (`NO CREDENTIAL may reach the rendered compose project`), so a later "just
  fetch `/v1/models` from the browser" change has to argue for itself;
- the only credential near this feature is the gateway master key, used by
  whatever produces the snapshot, and never written into it.

## How you know the catalogue is stale

The snapshot is refreshed by whoever produces it; nothing in this role schedules
that. **The page says how old it is, in words**, computed from `generated_at`:

| Age              | Renders as                                    | Colour |
| ---------------- | --------------------------------------------- | ------ |
| same day         | `refreshed today`                             | muted  |
| 1–6 days         | `N days old`                                  | muted  |
| 7–29 days        | `N days old — consider re-running the export` | amber  |
| 30+ days         | same, bold                                    | red    |
| unparseable date | `snapshot date unreadable — treat as stale`   | red    |

An unreadable date is treated as **stale**, never fresh: failing towards "this
might be old" is the honest direction.

## Honest failure states

The app never renders an empty list as a stand-in for a failed read:

- the snapshot is parsed through **zod**, so an unexpected shape is an error
  with a path;
- a failed fetch, a non-JSON body or a schema mismatch renders **"could not load
  the catalogue"**, naming the cause and explicitly denying that it means no
  models exist;
- an empty _filter result_ says so, and names the filters as the cause;
- `catalogue.json` is served `no-store`, because a cached old snapshot would
  present retired models as available with nothing indicating anything was
  wrong.

And three shapes in real gateway data are handled rather than papered over:

- **`/v1/models` can return duplicate ids.** Deduplicated, and the UI _says_ so,
  so its count never silently disagrees with the export's.
- **Router models price at `-1`** (no fixed rate). Taken literally that renders
  `$-1000000/M` and sorts them above every free model under "cheapest first";
  mapped to _unknown_.
- **`openrouter/openrouter/auto` is correct, not a doubled prefix.** Some models
  have `openrouter` as their literal vendor; "fixing" it would produce a 400 for
  a model that works.

## Observability notes

- nginx logs every request to the container's stdout, so any container log
  shipper picks it up.
- **Do not scrape `/metrics`.** The SPA fallback serves `index.html` for every
  unknown path, so a scrape job would sit permanently `up` while scraping a web
  page — a target that looks healthy and measures nothing.

## What this role does NOT do

- **It does not build the app** — see above.
- **It does not talk to the gateway** or produce the snapshot.
- **It does not prove a model works.** A catalogue entry is not a working route:
  prove a new id completes a real request before declaring it.

## Testing

```bash
cd roles/service_model_explorer && molecule test
```

Render-only, so coverage is honestly **partial**: it proves the templates render
correct, parseable artifacts for two different callers, not that the container
runs. Live behaviour is asserted by the role's own read-backs, which check the
served page is _the app_ (not merely a 200 — nginx's welcome page is a 200 too)
and that the served catalogue parses and carries models.

The scenario renders **two variants with entirely different values**
specifically to catch a template that hardcodes one installation's values — a
single render cannot tell a correct template from one that ignores its
variables.
