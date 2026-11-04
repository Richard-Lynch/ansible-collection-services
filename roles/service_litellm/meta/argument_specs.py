# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

DEFAULT_AUTHOR = "Collection maintainers"
from ansible_devkit.argspec.models import ArgumentSpec, ArgumentSpecs, OptionSpec

# NEVER put literal Jinja delimiters in a description. Ansible TEMPLATES the argspec, so a
# documentation mention of a Jinja tag is parsed as real Jinja and fails the whole role at
# validate_argument_spec time, before any task runs.

argument_specs = ArgumentSpecs(
    argument_specs={
        "main": ArgumentSpec(
            short_description="Deploy the LiteLLM gateway, Open WebUI and Postgres as one stack",
            description=[
                "Deploys three containers as a single compose project: the LiteLLM proxy, its",
                "Postgres database, and Open WebUI pointed at the proxy over the compose-internal",
                "network.",
                "Provider entries are rendered only for providers whose API key was resolved",
                "non-empty, and the role HARD GATES on at least one provider being present -- an",
                "empty api_key loads fine and only fails at request time, which is a silent",
                "misconfiguration rather than a failure.",
            ],
            version_added="1.0",
            author=DEFAULT_AUTHOR,
            options={
                # ---- identity / placement -------------------------------------------------
                "litellm_container_name": OptionSpec(
                    type="str", required=False, description=["LiteLLM container name."]
                ),
                "litellm_config_dir": OptionSpec(
                    type="str",
                    required=False,
                    description=["Host directory holding the rendered compose project and config."],
                ),
                "litellm_network": OptionSpec(
                    type="str",
                    required=False,
                    description=[
                        "External Docker network the stack joins (for example one shared with a",
                        "metrics scraper or a reverse proxy). Created by the role if absent.",
                    ],
                ),
                # ---- images (always pinned) -----------------------------------------------
                "litellm_image": OptionSpec(
                    type="str",
                    required=False,
                    description=["LiteLLM proxy image. Pinned -- never a floating tag."],
                ),
                "litellm_postgres_image": OptionSpec(
                    type="str",
                    required=False,
                    description=["Postgres image for the stack's own database. Pinned."],
                ),
                "litellm_open_webui_image": OptionSpec(
                    type="str",
                    required=False,
                    description=["Open WebUI image. Pinned."],
                ),
                # ---- ports ----------------------------------------------------------------
                "litellm_host_port": OptionSpec(
                    type="int",
                    required=False,
                    description=[
                        "Host port mapped to the proxy's container port 4000. A reverse proxy in",
                        "front of it is the caller's to configure.",
                    ],
                ),
                "litellm_open_webui_bind_address": OptionSpec(
                    type="str",
                    required=False,
                    default="127.0.0.1",
                    description=[
                        "Host address Open WebUI's port is published on. Loopback by default: a",
                        "Docker-published port bypasses the host firewall, and until its first",
                        "admin exists Open WebUI lets anyone who reaches it sign up. Set the",
                        "address a reverse proxy reaches it on, or an empty string for every",
                        "interface (right only for a host whose NICs are all internal and",
                        "firewalled).",
                    ],
                ),
                "litellm_open_webui_host_port": OptionSpec(
                    type="int",
                    required=False,
                    description=[
                        "Host port mapped to Open WebUI's container port 8080. A reverse proxy in",
                        "front of it is the caller's to configure.",
                    ],
                ),
                # ---- secrets (no defaults; supplied by the caller from its secret store) ----
                "litellm_master_key": OptionSpec(
                    type="str",
                    required=True,
                    description=[
                        "Proxy admin key (LITELLM_MASTER_KEY). Must carry the sk- prefix; the role",
                        "asserts it does. Supplied by the calling play, never a default.",
                    ],
                ),
                "litellm_salt_key": OptionSpec(
                    type="str",
                    required=True,
                    description=[
                        "LITELLM_SALT_KEY -- encrypts provider credentials at rest in Postgres.",
                        "IMMUTABLE once any model has been stored: changing it makes every stored",
                        "key undecryptable. Create once, never rotate.",
                    ],
                ),
                "litellm_postgres_password": OptionSpec(
                    type="str",
                    required=True,
                    description=[
                        "Password for the stack-local Postgres role that LiteLLM connects as."
                    ],
                ),
                "litellm_open_webui_secret_key": OptionSpec(
                    type="str",
                    required=True,
                    description=[
                        "Open WebUI session-signing key (WEBUI_SECRET_KEY). Rotating it invalidates",
                        "every active browser session.",
                    ],
                ),
                # ---- provider keys (optional; absent means that provider is not rendered) ---
                "litellm_anthropic_api_key": OptionSpec(
                    type="str",
                    required=False,
                    description=[
                        "Anthropic API key. Empty string omits the Anthropic entry from model_list",
                        "entirely rather than rendering one that cannot authenticate.",
                    ],
                ),
                "litellm_openai_api_key": OptionSpec(
                    type="str",
                    required=False,
                    description=["OpenAI API key. Empty string omits the OpenAI entry."],
                ),
                "litellm_gemini_api_key": OptionSpec(
                    type="str",
                    required=False,
                    description=["Google Gemini API key. Empty string omits the Gemini entry."],
                ),
                "litellm_openrouter_api_key": OptionSpec(
                    type="str",
                    required=False,
                    description=[
                        "OpenRouter API key. A NATIVE provider, so it adds an openrouter wildcard",
                        "to model_list and does satisfy the at-least-one-provider gate -- unlike",
                        "the TypeSafe key below. Empty string omits the entry.",
                    ],
                ),
                "litellm_elevenlabs_api_key": OptionSpec(
                    type="str",
                    required=False,
                    description=[
                        "ElevenLabs API key. A NATIVE provider on v1.104.2 (`LlmProviders.ELEVENLABS`",
                        "is in provider_list, with cost-map models spanning modes audio_speech and",
                        "audio_transcription), so",
                        "it joins model_list as an elevenlabs wildcard and routes",
                        "/v1/audio/speech. Empty string omits the entry.",
                        "Note LiteLLM maps the OpenAI voice names (alloy, sage, onyx, ...) onto",
                        "ElevenLabs voice ids, so an OpenAI TTS client needs no change to use it.",
                    ],
                ),
                # ---- web search (a SEPARATE registry from the LLM providers above) ----------
                "litellm_brave_search_api_key": OptionSpec(
                    type="str",
                    required=False,
                    description=[
                        "Brave Search API key. Wires a SEARCH TOOL, not a model: search",
                        "providers live in their own registry on v1.104.2 (brave appears nowhere",
                        "in provider_list and has no cost-map entry, but is listed by",
                        "/search_tools/ui/available_providers as 'Brave Search'), and are",
                        "configured through the top-level search_tools block rather than",
                        "model_list. Serves POST /v1/search. Empty string omits the block",
                        "entirely, leaving the gateway with no search tool rather than one that",
                        "cannot authenticate.",
                    ],
                ),
                "litellm_brave_search_tool_name": OptionSpec(
                    type="str",
                    required=False,
                    description=[
                        "Name the Brave search tool is addressed by, i.e. the",
                        "POST /v1/search/<name> path segment and the search_tool_name field in a",
                        "body sent to /v1/search. A generic default is right for any caller.",
                    ],
                ),
                # ---- MCP gateway -------------------------------------------------------------
                "litellm_mcp_context7_enabled": OptionSpec(
                    type="bool",
                    required=False,
                    description=[
                        "Register the hosted Context7 documentation MCP server with the",
                        "gateway's MCP block. HTTP transport and NO credential, so it needs",
                        "neither a secret nor a derived image. The endpoint answers MCP",
                        "initialize and lists the tools",
                        "resolve-library-id and query-docs.",
                        "Defaults to false — an MCP server is reachable tool execution, so it is",
                        "opt-in from inventory rather than on by default.",
                    ],
                ),
                "litellm_mcp_context7_url": OptionSpec(
                    type="str",
                    required=False,
                    description=[
                        "URL of the Context7 MCP endpoint. A vendor-hosted address rather than an",
                        "installation fact, so it carries a default — overridable from inventory",
                        "if the vendor moves it.",
                    ],
                ),
                "litellm_typesafe_api_key": OptionSpec(
                    type="str",
                    required=False,
                    description=[
                        "TypeSafe AI (Jev) API key. Jev is a PASS-THROUGH upstream, not a chat",
                        "model: it returns typed decisions from its own endpoint, so it adds no",
                        "model_list entry and does not count towards the at-least-one-provider",
                        "gate. Empty string leaves the pass-through route unconfigured.",
                    ],
                ),
                # ---- local (Ollama) upstream, off by default -------------------------------
                "litellm_ollama_enabled": OptionSpec(
                    type="bool",
                    required=False,
                    description=[
                        "Render an Ollama upstream entry. Defaults to false: an entry with no",
                        "Ollama host behind it would advertise models that cannot answer. Enable",
                        "from inventory once one exists.",
                    ],
                ),
                "litellm_ollama_api_base": OptionSpec(
                    type="str",
                    required=False,
                    description=[
                        "Base URL of the Ollama host, used only when the Ollama upstream is enabled.",
                        "Installation fact -- supply it from inventory, not from a role default.",
                    ],
                ),
                "litellm_ollama_models": OptionSpec(
                    type="list",
                    elements="str",
                    required=False,
                    description=[
                        "Explicit Ollama model names to expose. Ollama has no wildcard support, so",
                        "each model is listed. Only used when the Ollama upstream is enabled.",
                    ],
                ),
                "litellm_redis_uid": OptionSpec(
                    type="int",
                    required=False,
                    description=[
                        "Numeric uid the Redis container writes its data as. The redis SERVER",
                        "runs as root in the official image, but it forks a CHILD to write the",
                        "RDB snapshot and that child runs as the `redis` user -- so a root-owned",
                        "data directory looks writable to every manual check and still fails",
                        "every background save with 'Permission denied'. 999 in the official",
                        "alpine image; an option rather than a constant so a different image can",
                        "say otherwise.",
                    ],
                ),
                "litellm_redis_gid": OptionSpec(
                    type="int",
                    required=False,
                    description=[
                        "Numeric gid for the Redis data directory. See litellm_redis_uid.",
                    ],
                ),
                "litellm_openrouter_models": OptionSpec(
                    type="list",
                    elements="str",
                    required=False,
                    description=[
                        "OpenRouter model ids to list EXPLICITLY, in addition to the wildcard.",
                        "The wildcard routes every OpenRouter model, but cannot ENUMERATE any of",
                        "them: OpenRouter has no provider model-info class on this build, so it",
                        "contributes zero ids to GET /v1/models. A client that builds a model",
                        "picker from that endpoint -- Open WebUI does -- therefore cannot offer an",
                        "OpenRouter model at all, however well it routes. Each id listed here is",
                        "rendered as its own model_list entry so it becomes selectable.",
                        "Does NOT replace the wildcard, which stays and keeps every unlisted model",
                        "reachable by name. A new upstream model is routable the day it ships and",
                        "becomes pickable when someone adds it here -- so this list can only ever",
                        "lag a preference, never break a route.",
                        "Empty list renders no explicit entries, which is the right default for a",
                        "caller that does not use a picker.",
                    ],
                ),
                "litellm_model_aliases": OptionSpec(
                    type="list",
                    elements="dict",
                    required=False,
                    default=[],
                    description=[
                        "Stable local model NAMES that clients ask for instead of a vendor id",
                        "Each item is a dict of name (the alias clients send), model",
                        "(provider/model, whose provider must have a key configured) and",
                        "optionally fallback (the name of ANOTHER alias LiteLLM retries on an",
                        "upstream failure; it must route to a different provider, or the",
                        "fallback fails with the thing it is covering). Each renders its own",
                        "model_list entry beside the provider wildcards, which stay. Re-pointing",
                        "an alias is a one-line change here and no client moves.",
                    ],
                ),
                # ---- behaviour -------------------------------------------------------------
                "litellm_num_retries": OptionSpec(
                    type="int",
                    required=False,
                    description=["Router retry count for a failed upstream request."],
                ),
                "litellm_request_timeout": OptionSpec(
                    type="int",
                    required=False,
                    description=["Per-request timeout in seconds passed to the router."],
                ),
                "litellm_drop_params": OptionSpec(
                    type="bool",
                    required=False,
                    description=[
                        "Drop OpenAI parameters an upstream does not support instead of erroring.",
                        "Keeps a single client contract working across providers.",
                    ],
                ),
                "litellm_telemetry": OptionSpec(
                    type="bool",
                    required=False,
                    description=[
                        "Allow LiteLLM's own outbound usage telemetry. Defaults to false."
                    ],
                ),
                "litellm_disable_env_credential_login": OptionSpec(
                    type="bool",
                    required=False,
                    description=[
                        "Refuse the environment-credential console login, under which the master",
                        "key doubles as a proxy-admin UI password. Defaults to true. Needs a real",
                        "admin account to exist first (created from an invitation link).",
                    ],
                ),
                "litellm_store_model_in_db": OptionSpec(
                    type="bool",
                    required=False,
                    description=[
                        "Let models added through the admin UI persist in Postgres. These are the",
                        "rows the salt key encrypts.",
                    ],
                ),
                # ---- Open WebUI ------------------------------------------------------------
                "litellm_open_webui_container_name": OptionSpec(
                    type="str", required=False, description=["Open WebUI container name."]
                ),
                "litellm_open_webui_enable_signup": OptionSpec(
                    type="bool",
                    required=False,
                    description=[
                        "Allow self-service account creation in Open WebUI. Defaults to false so a",
                        "LAN-reachable UI does not accept new accounts after the first admin exists.",
                    ],
                ),
                "litellm_open_webui_name": OptionSpec(
                    type="str",
                    required=False,
                    description=[
                        "Name Open WebUI shows in its title bar and sign-in page (WEBUI_NAME).",
                    ],
                ),
                # ---- branding (LiteLLM admin UI) --------------------------------------------
                "litellm_ui_logo_path": OptionSpec(
                    type="str",
                    required=False,
                    description=[
                        "Logo for the LiteLLM admin UI sidebar (light mode). Sets UI_LOGO_PATH,",
                        "a plain env var read by /get_image with no premium_user gate (verified",
                        "v1.104.2). Either an http(s) URL -- fetched by the BROWSER, which",
                        "matters for an internal-only UI -- or a path readable inside the",
                        "litellm container, e.g. a file mounted under litellm_config_dir. Empty",
                        "renders nothing and the bundled LiteLLM logo is served.",
                    ],
                ),
                "litellm_ui_logo_path_dark": OptionSpec(
                    type="str",
                    required=False,
                    description=[
                        "Dark-mode variant of litellm_ui_logo_path (UI_LOGO_PATH_DARK). Falls",
                        "back to litellm_ui_logo_path when empty.",
                    ],
                ),
                "litellm_ui_favicon_path": OptionSpec(
                    type="str",
                    required=False,
                    description=[
                        "Favicon for the LiteLLM admin UI (LITELLM_FAVICON_URL). Same URL/path",
                        "rules as litellm_ui_logo_path. Empty serves the bundled favicon.",
                    ],
                ),
                # ---- branding (Open WebUI) --------------------------------------------------
                "litellm_open_webui_logo_path": OptionSpec(
                    type="str",
                    required=False,
                    description=[
                        "HOST file bind-mounted read-only over Open",
                        "WebUI's static/logo.png. Open WebUI has no logo env var (verified",
                        "v0.11.4) and wipes+recopies its own static assets on every process",
                        "start, so a file merely placed at that path does not survive a",
                        "restart -- only a read-only mount does, because OWUI's own copy step",
                        "catches the write failure and logs rather than erroring. Empty omits",
                        "the mount and the bundled Open WebUI logo is served. Note Open",
                        "WebUI's LICENSE permits replacing its branding only for deployments",
                        "with 50 or fewer end users, or under an enterprise license.",
                    ],
                ),
                "litellm_open_webui_favicon_path": OptionSpec(
                    type="str",
                    required=False,
                    description=[
                        "HOST file bind-mounted read-only over Open WebUI's",
                        "static/favicon.png. Same mechanism and licensing note as",
                        "litellm_open_webui_logo_path. Empty omits the mount.",
                    ],
                ),
                # ---- postgres --------------------------------------------------------------
                # ---- redis (shared worker state) -------------------------------------------
                "litellm_redis_enabled": OptionSpec(
                    type="bool",
                    required=False,
                    description=[
                        "Run a Redis alongside the proxy for state shared across its workers.",
                        "Defaults to true: without it each worker keeps its own rate limits,",
                        "budgets and router state, so a limit is enforced once per worker and",
                        "spend can overshoot by roughly the worker count.",
                    ],
                ),
                "litellm_redis_container_name": OptionSpec(
                    type="str", required=False, description=["Redis container name."]
                ),
                "litellm_redis_image": OptionSpec(
                    type="str", required=False, description=["Redis image. Pinned."]
                ),
                "litellm_redis_password": OptionSpec(
                    type="str",
                    required=False,
                    description=[
                        "Password for the stack-local Redis. Required when Redis is enabled;",
                        "the role asserts it is present rather than running an unauthenticated",
                        "Redis holding every budget counter.",
                    ],
                ),
                "litellm_postgres_container_name": OptionSpec(
                    type="str", required=False, description=["Postgres container name."]
                ),
                "litellm_postgres_user": OptionSpec(
                    type="str", required=False, description=["Postgres role LiteLLM connects as."]
                ),
                "litellm_postgres_db": OptionSpec(
                    type="str", required=False, description=["Database name for LiteLLM's schema."]
                ),
                "litellm_timezone": OptionSpec(
                    type="str", required=False, description=["TZ passed to every container."]
                ),
            },
        ),
        "open_webui_connections": ArgumentSpec(
            short_description="Converge extra OpenAI-compatible connections in a running Open WebUI",
            description=[
                "Adds or updates each declared connection in Open WebUI's own connection list",
                "through its admin API, leaving every connection it does not declare untouched --",
                "including the LiteLLM one the stack is deployed with.",
                "Open WebUI persists connections in its database once saved, after which the",
                "OPENAI_API_BASE_URL(S) environment variables are ignored, so the compose file",
                "cannot carry them; the API is the only place a change takes effect.",
                "Reports the difference by default and writes only with",
                "litellm_open_webui_connections_apply, then reads the result back from the API.",
            ],
            version_added="1.1",
            author=DEFAULT_AUTHOR,
            options={
                "litellm_open_webui_container_name": OptionSpec(
                    type="str", required=False, description=["Open WebUI container name."]
                ),
                "litellm_open_webui_connections": OptionSpec(
                    type="list",
                    elements="dict",
                    required=True,
                    description=[
                        "Connections to converge, matched to Open WebUI's list by url. A url not",
                        "yet present is appended; a present one has its key and config replaced.",
                    ],
                    options={
                        "name": OptionSpec(
                            type="str",
                            required=True,
                            description=["Label for reports only; Open WebUI does not store it."],
                        ),
                        "url": OptionSpec(
                            type="str",
                            required=True,
                            description=["OpenAI-compatible base URL, ending in /v1."],
                        ),
                        "key": OptionSpec(
                            type="str",
                            required=True,
                            no_log=True,
                            description=["Bearer key Open WebUI presents to that URL."],
                        ),
                        "model_ids": OptionSpec(
                            type="list",
                            elements="str",
                            required=False,
                            description=[
                                "Model ids Open WebUI may use from this connection. Empty means",
                                "every model the connection lists.",
                            ],
                        ),
                        "prefix_id": OptionSpec(
                            type="str",
                            required=False,
                            description=[
                                "Prefix Open WebUI puts on this connection's model ids. Empty",
                                "keeps the upstream ids.",
                            ],
                        ),
                    },
                ),
                "litellm_open_webui_connections_apply": OptionSpec(
                    type="bool",
                    required=False,
                    description=[
                        "Write the merged connection list. False reports the difference only.",
                    ],
                ),
            },
        ),
        "open_webui_task_model": ArgumentSpec(
            short_description="Set Open WebUI's external task model, and no other task setting",
            description=[
                "Open WebUI runs title, tag and follow-up generation for chats with an external",
                "model on task.model.external; empty means the chat's own model. The task config",
                "API replaces every task setting at once, so this reads the config, changes that",
                "one key, writes it back and asserts every other key read back unchanged.",
                "Reports by default; writes only with litellm_open_webui_task_model_apply.",
            ],
            version_added="1.1",
            author=DEFAULT_AUTHOR,
            options={
                "litellm_open_webui_container_name": OptionSpec(
                    type="str", required=False, description=["Open WebUI container name."]
                ),
                "litellm_open_webui_task_model_external": OptionSpec(
                    type="str",
                    required=True,
                    description=[
                        "Model id for background tasks, as Open WebUI lists it in /api/models",
                        "(refused if it is not listed).",
                    ],
                ),
                "litellm_open_webui_task_model_apply": OptionSpec(
                    type="bool",
                    required=False,
                    description=["Write the value. False reports the difference only."],
                ),
            },
        ),
    }
)
