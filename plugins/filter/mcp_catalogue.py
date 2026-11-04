# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright: (c) 2026, Richard Lynch
# GNU General Public License v3.0+ (see https://www.gnu.org/licenses/gpl-3.0.txt)
"""The reviewed MCP catalogue (the filter behind the config_mcp_servers role).

`mcp_catalogue_document(servers, clients)` validates the catalogue the caller declares and
returns the document its consumers (a settings service, a probe) read (`mcp-catalogue.json`):

    {"servers": {name: {"transport", "config", "clients", "env_names", "destinations",
                        "reviewed_by", "tools_include"}},
     "enabled": {client: [names]}}

`config` is the exact block a Hermes client renders under `mcp_servers.<name>`, so what the
service enables is what the reviewer read. The refusals are the ways a catalogue reads as
reviewed and is not:

  * an entry with no reviewer, or `reviewed_by: pending` (how a proposed entry arrives -- a
    human must name themselves before it can converge);
  * a literal credential in a header or env value: only `${NAME}` references are allowed, and
    every name referenced must be listed in `env_names` (so enable_mcp can check it is placed);
  * an http transport without a URL, a stdio one without a command, an unknown transport;
  * no `tools_include` (an absent include is "every tool the server offers", which nobody reviewed);
  * a declared enablement naming a server the catalogue lacks, or a client the entry does not allow.
"""

from __future__ import annotations

import re
from typing import Any

try:
    from ansible.errors import AnsibleFilterError
except ImportError:  # unit tests without ansible on the path

    class AnsibleFilterError(Exception):  # type: ignore[no-redef]
        pass


NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,40}$")
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
REF_RE = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")
TRANSPORTS = ("http", "stdio")


def _fail(msg: str) -> None:
    raise AnsibleFilterError(f"mcp_catalogue: {msg}")


def _only_refs(name: str, where: str, value: Any, env_names: list[str]) -> None:
    if not isinstance(value, str):
        _fail(f"{name}: {where} must be a string")
    refs = REF_RE.findall(value)
    rest = REF_RE.sub("", value)
    # Only a scheme word may surround a reference ("Bearer ${X}"); anything else is a literal value.
    if rest.strip() not in ("", "Bearer", "Token", "Basic"):
        _fail(
            f"{name}: {where} carries a literal value; only ${{NAME}} references are allowed "
            "(the value lives in the client's .env, placed by a converge from the secret store)"
        )
    for ref in refs:
        if ref not in env_names:
            _fail(f"{name}: {where} references ${{{ref}}}, which is not listed in env_names")


def mcp_catalogue_document(servers: Any, clients: Any = None) -> dict:
    if not isinstance(servers, list) or not servers:
        _fail("the catalogue must be a non-empty list of servers")
    out: dict = {}
    for i, s in enumerate(servers):
        if not isinstance(s, dict):
            _fail(f"entry {i} is not a mapping")
        name = s.get("name")
        if not isinstance(name, str) or not NAME_RE.match(name):
            _fail(f"entry {i}: name {name!r} is not a catalogue name ([a-z][a-z0-9_-]*)")
        if name in out:
            _fail(f"{name} is declared twice")
        reviewer = s.get("reviewed_by")
        if (
            not isinstance(reviewer, str)
            or not reviewer.strip()
            or reviewer.strip().lower() == "pending"
        ):
            _fail(
                f"{name}: reviewed_by is {reviewer!r}; a human reviews the entry and names themselves"
            )
        env_names = s.get("env_names", [])
        if not isinstance(env_names, list) or not all(
            isinstance(n, str) and ENV_NAME_RE.match(n) for n in env_names
        ):
            _fail(f"{name}: env_names must be a list of UPPER_SNAKE names")
        transport = s.get("transport")
        if transport not in TRANSPORTS:
            _fail(f"{name}: transport {transport!r} is not one of {TRANSPORTS}")
        config: dict = {}
        if transport == "http":
            url = s.get("url")
            if not isinstance(url, str) or not re.match(r"^https?://[^\s/]+", url):
                _fail(f"{name}: an http server needs a url")
            if REF_RE.search(url) or re.search(r"[?&](key|token|api_key)=", url, re.I):
                _fail(f"{name}: the url must not carry a credential")
            config["url"] = url
            headers = s.get("headers", {})
            if not isinstance(headers, dict):
                _fail(f"{name}: headers must be a mapping")
            for h, v in headers.items():
                _only_refs(name, f"header {h}", v, env_names)
            if headers:
                config["headers"] = dict(headers)
        else:
            command = s.get("command")
            if not isinstance(command, str) or not command:
                _fail(f"{name}: a stdio server needs a command")
            args = s.get("args", [])
            if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
                _fail(f"{name}: args must be a list of strings")
            config["command"] = command
            if args:
                config["args"] = list(args)
            env = s.get("env", {})
            if not isinstance(env, dict):
                _fail(f"{name}: env must be a mapping")
            for k, v in env.items():
                _only_refs(name, f"env {k}", v, env_names)
            if env:
                config["env"] = dict(env)
        include = s.get("tools_include")
        if (
            not isinstance(include, list)
            or not include
            or not all(isinstance(t, str) and t for t in include)
        ):
            _fail(
                f"{name}: tools_include must list the reviewed tools (an absent include loads every tool)"
            )
        config["tools"] = {"include": list(include)}
        allowed = s.get("clients")
        if (
            not isinstance(allowed, list)
            or not allowed
            or not all(isinstance(c, str) and NAME_RE.match(c) for c in allowed)
        ):
            _fail(f"{name}: clients must list the profiles that may enable it")
        out[name] = {
            "transport": transport,
            "config": config,
            "clients": list(allowed),
            "env_names": list(env_names),
            "destinations": list(s.get("destinations", [])),
            "reviewed_by": reviewer.strip(),
        }
    enabled: dict = {}
    for client, names in (clients or {}).items():
        if not isinstance(names, list):
            _fail(f"enabled.{client} must be a list")
        for n in names:
            if n not in out:
                _fail(f"enabled.{client} names {n}, which is not in the catalogue")
            if client not in out[n]["clients"]:
                _fail(
                    f"enabled.{client} names {n}, which is catalogued only for {out[n]['clients']}"
                )
        enabled[client] = sorted(set(names))
    return {"servers": out, "enabled": enabled}


class FilterModule:
    def filters(self):
        return {"mcp_catalogue_document": mcp_catalogue_document}
