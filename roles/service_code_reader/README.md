# service_code_reader

A small sidecar that reads **one-time verification codes** out of a dedicated mailbox and hands each
to the agent's browser filler **exactly once**, so the language model never sees the code.

Sign-ups and logins email "your code is 482913". If the model read that, it would sit in the
transcript, the logs and the provider's request history. Instead the filler asks this service for
the code, types it into the page, and tells the model only `filled: true`.

The role is the filler-free half: it needs no browser and can be built and tested before one
exists.

## What it does

- Polls an IMAP mailbox every `code_reader_poll_seconds` (15 s).
- Accepts mail only from `code_reader_sender_allowlist`: an exact address (`noreply@example.com`)
  or `@example.com` for that domain **exactly** (not its subdomains). Everyone else is ignored.
- Extracts a 4-8 digit code near the word *code*/*passcode*/*otp*/*pin* (or `NNNNNN is your code`).
  No match means no code: it never falls back to "the first number", which would be an order number.
- Keeps each code for `code_reader_ttl_seconds` (600 s) and **deletes it when served**.
- `GET /code?purpose=<origin>` with `Authorization: Bearer <token>` returns `{"code": "..."}` or
  404. A code from `noreply@mail.example.com` satisfies a request for `example.com` (label-bounded).
- `GET /healthz` (no token) is 200 only after a successful IMAP login and poll.
- Logs `{event, sender, origin, received_at}` and nothing else. The logger refuses a `code` field.

## Inputs

Declared in [`meta/argument_specs.yml`](meta/argument_specs.yml) (generated from
`meta/argument_specs.py`, as are `defaults/*.yml` and the two `tasks/_*.yml` resolver/guard files).

Required, with no default because each depends on the installation or is a credential:
`code_reader_imap_host`, `code_reader_imap_user`, `code_reader_imap_password` (no_log),
`code_reader_sender_allowlist`, `code_reader_token` (no_log, >= 32 chars) and
`code_reader_network` (the docker network the agent's container is on).

```yaml
- name: Verification-code reader beside the agent
  ansible.builtin.include_role:
    name: richard_lynch.services.service_code_reader
  vars:
    code_reader_imap_host: imap.example.com
    code_reader_imap_user: codes@example.com
    code_reader_imap_password: "{{ vault_codes_mailbox_password }}"
    code_reader_sender_allowlist: ["noreply@example.com", "@accounts.example.org"]
    code_reader_token: "{{ vault_code_reader_token }}"
    code_reader_network: agent-net
```

## Entrypoints

| entrypoint | what |
|---|---|
| `main` | gates, render, converge, restart only on a real change, then read the sidecar's own `/healthz` |
| `probe` | read-only: reports HEALTHY / UNHEALTHY / CANNOT TELL from `/healthz`. Never requests a code (that would consume one) |

## Design notes worth knowing

- **Loopback only, by a positive test.** The port is published on `code_reader_bind`, and the role
  refuses anything but `127.x` or `::1`. A Docker-published port bypasses the host firewall and the
  only authentication is a bearer token. The list of right answers is two; the list of wrong ones is
  open-ended, so the gate tests for the right ones.
- **Sibling containers use the docker network, not the host loopback.** `127.0.0.1` inside the
  Hermes container is Hermes's own loopback, so the filler calls `http://code-reader:8645`. The
  sidecar listens on all interfaces *inside* its container; the host-facing exposure is the compose
  `ports:` line.
- **No credential in the compose file or config.** The IMAP password and token go into a `0600`
  env file rendered from `templates/` (not `files/`, where `deployment_config` would render it a
  second time at the shared mode). `config.json` carries no secret.
- **A directory mount, not two file mounts.** A file-level bind pins the inode and the renderer
  replaces files atomically, so a changed config would never reach a running container.
- **The sender check trusts a WELL-FORMED `From:` header.** The header is read with the
  `email.policy.default` address parser, never `parseaddr()`: a header with parse defects, more
  than one address, or more than one `From` is refused outright, because `parseaddr()` read
  `From: noreply@example.com <attacker@evil.com>` as the allowlisted address and stored the
  attacker's code. A well-formed forged header still passes: that stops casual noise, not a
  forger whose mail the provider accepts for an allowlisted domain (which that domain's DMARC
  policy decides), and the worst a forgery achieves is the filler typing a wrong code. That is an
  accepted residual risk. Do not widen the allowlist to a domain that lets arbitrary people send
  mail.
- **Origin matching goes both ways on purpose** (`noreply@mail.example.com` serves a request for
  `example.com`, and `noreply@example.com` serves `accounts.example.com`), label-bounded. On a
  shared-tenant domain a request for `tenant.platform.com` could take the platform's own code;
  the `purpose` is chosen by the filler under its own gate, never by a page.
- **Serving a code drops the older codes for the same origin.** After a "resend" the predecessor
  is stale; without this a second `GET` would get it instead of a 404.
- **The env file is single-quoted** so compose takes the values literally (`$` is otherwise
  interpolated and ` #` starts a comment), and the preflight refuses a value containing a single
  quote, which that quoting cannot carry. The container runs as `nobody` (`user: 65534:65534`):
  it reads `/app` (rendered 0644) and writes stdout only.
- **`code_reader_code_patterns` are tried BEFORE the built-ins**, as the argspec says, and never
  replace them. The built-in gap between the keyword and the digits stops at sentence
  punctuation, so `PIN reset requested. Ref 9999. Your code is 482913` yields `482913`.
- **Stdlib only.** It runs in a stock pinned `python` image with one file mounted; nothing to
  `pip install` or audit.

## Tests

| suite | proves |
|---|---|
| `python3 roles/service_code_reader/files/test_code_reader.py` | allowlist (and near-misses), extraction (two numbers picks the one after *code*; a bare number is not a code), one-shot, TTL boundary, bearer required, a refused call does not consume a code, no code value in any log line, poll failure never logs server text. Fake IMAP; real HTTP server on an ephemeral port |
| `molecule test` (render-only) | the role's *real* gates refuse a public bind, `::`, a LAN address, a floating image, a bare-domain allowlist entry, a short token and a password carrying a single quote — each for the reason it names — and pass `127.x`/`::1`; compose publishes loopback only and runs as `nobody`; no credential in compose or config; env file is `0600` and single-quoted; a credential-shape scanner passes, where the checkout has one |

Molecule is partial coverage by design (no Docker daemon or mailbox on a CI runner): it proves
the gates and templates, not that the sidecar runs. That is asserted on a real host by the
role's `/healthz` read-back.

## Configuration versus run-time activity

The role manages the sidecar's platform: its container, port, mount, network and credentials.
What the running service does — reading one mailbox and serving one value — is its own run-time
activity and is never converged. It does not touch platform state; a port, a mount or a
credential changes only through this role's inputs.
