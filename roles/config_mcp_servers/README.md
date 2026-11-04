# config_mcp_servers

Validates a **reviewed MCP catalogue** (every MCP server an agent client profile
may ever load) and renders it, with each client's declared enablement, as **one
JSON document** (`mcp_clients_file`, typically `mcp-catalogue.json`) for the
consumers that read it — a settings service, a probe, an agent profile.

It is a `config_*` role: one cross-cutting concern (which MCP servers exist and
which client may load them) for several kinds of caller.

## The two populations, and who changes each

| what | changed by |
| ---- | ---------- |
| the catalogue (`mcp_servers`: transport, URL or command, `tools_include`, destinations, which clients may enable it, who reviewed it) | a reviewed change to the caller's configuration. An entry with `reviewed_by: pending` is **refused** until a human names themselves |
| which catalogued servers a client has enabled | declared here as the starting set (`mcp_clients`); afterwards it may move within the catalogue at run time (for example through a settings service), and a probe reports it rather than this role reverting it |

## What it refuses (`richard_lynch.services.mcp_catalogue_document`, unit-tested)

- an entry nobody reviewed (`reviewed_by` empty or `pending`);
- a literal credential in a header, an env value or a URL query: only `${NAME}`
  references are allowed, and every referenced name must be in `env_names` (so
  a consumer can check it is placed in the client's environment before enabling);
- no `tools_include` (an absent include is "every tool the server offers", which
  nobody reviewed);
- an unknown transport, an http server with no URL, a stdio server with no
  command, a duplicate name;
- a declared enablement naming a server not in the catalogue, or a client the
  entry does not allow.

Nothing is written when any of these fires.

## Inputs

[`meta/argument_specs.yml`](meta/argument_specs.yml) (generated from
`meta/argument_specs.py`): `mcp_servers` (the catalogue, required),
`mcp_clients` (declared enablement), `mcp_clients_file` (absolute target path,
required). All three are the caller's configuration.

## Example

```yaml
- name: Render the MCP catalogue
  ansible.builtin.include_role:
    name: richard_lynch.services.config_mcp_servers
  vars:
    mcp_clients_file: /etc/agent-policy/mcp-catalogue.json
    mcp_servers:
      - name: docs
        transport: http
        url: https://mcp.example.com/mcp
        tools_include: [search-docs]
        clients: [default]
        destinations: [mcp.example.com]
        reviewed_by: alice
      - name: tickets
        transport: http
        url: https://tickets.example.com/mcp/
        headers: {Authorization: "Bearer ${MCP_TICKETS_TOKEN}"}
        env_names: [MCP_TICKETS_TOKEN]
        tools_include: [get_ticket]
        clients: [default]
        reviewed_by: alice
    mcp_clients:
      default: [docs]
```

## Side effects

Creates the document's directory (0755) and writes the document root-owned,
0644, then reads it back and fails unless it is the validated document.

## Not done here

- No compose stack for container-transport servers: the role renders the
  catalogue only.
- It does not render client-specific config files (`.mcp.json`, `config.toml`)
  and never changes a client's live enablement.

## Molecule

Renders a two-server catalogue through the role, checks the document, the mode
and idempotence, and proves two refusals (a `pending` entry, a literal token)
write nothing.
