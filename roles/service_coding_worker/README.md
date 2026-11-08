# service_coding_worker

Turns a **VM** into a coding worker for an agent hub: every task card gets **one
git worktree and one sysbox dev container**, and Claude Code or Codex run
**inside that container**, routed through a LiteLLM gateway with the worker's
own virtual key, supervised by `worker-sessiond`, with `ttyd` for jump-in from a
browser. The tmux session `card-<id>` is only the viewing layer: its one job is
`docker exec -it card-<id> claude|codex`, so
`ssh <user>@<worker> -t tmux attach -t card-<id>` and the ttyd page attach to
the same agent.

It needs a real kernel (a VM, not an LXC guest): sysbox is what lets each card
run its own inner dockerd without `--privileged`.

## The container shape

| Property                                   | How it is true                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| ------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **The agent is sandboxed**                 | `docker run --runtime sysbox-runc`; sessiond **reads the runtime back** after the start and removes the container if it is not exactly `sysbox-runc` + `running` (a container that quietly came up under `runc` looks identical until the first nested `docker run`).                                                                                                                                                                                                                                                                                                                                                               |
| **It sees only the card's files**          | `container_mounts()` (pure, unit-tested): the card's **own clone** (`git clone --no-hardlinks`, hooks/fsmonitor off, origin set to the GitHub URL; the host repo's gitdir is never mounted, so the container cannot plant config or hooks that host git would run), `cards/<id>/data` (`/card`: MCP file, Codex home), `homes/<id>` (`/root`), and the agent tools read-only. `cards/<id>/host/` (run.sh, the prompt) is what sessiond writes and executes and is never mounted; every write into a shared dir is temp + `O_NOFOLLOW` + rename and refuses a symlinked directory. No work root, no `/etc/worker`, no docker socket. |
| **It has its own dockerd**                 | the image carries `docker.io`; the container's idle command starts `dockerd` when present. The role's read-back starts a throwaway sysbox container, waits for its inner dockerd and asserts a mapped uid namespace. **Its DNS:** a card's resolv.conf names docker's embedded `127.0.0.11`, and an inner dockerd that sees only a loopback resolver hands nested containers docker's public fallback resolvers, which a firewall allowing only local DNS drops. `worker_card_dns` writes the resolver the VM itself uses into the default image's `/etc/docker/daemon.json`; a `.devcontainer` image does not get it.              |
| **It can bootstrap the repo it is given**  | the default image carries `python3-venv` and a **pinned uv** (`worker_tools.uv_version` + `uv_sha256_linux_x64`): the role fetches the astral-sh/uv release tarball into the build context against the digest and the Dockerfile checks the same digest again before unpacking it (no piped installer). The converge's self-test container prints `uv --version` and imports `venv, ensurepip`, and the role asserts both, so "a card can build its repo's venv" is read from the image sessiond will launch, not from the Dockerfile.                                                                                              |
| **It cannot reach the VM or another card** | each card gets its **own docker bridge network** with `enable_icc=false` (created at launch, removed at `done`), and `worker-card-firewall.service` loads an nftables table that **drops INPUT from `docker0` and `br-*`** (sessiond, ttyd and sshd on the VM are unreachable from a card; FORWARD, i.e. egress, is untouched). The loaded table is read back by the role.                                                                                                                                                                                                                                                          |
| **Disk is not bounded**                    | `launch` refuses below 10 GB free on the state filesystem; there is **no per-card quota**. `done` removes root-made files via a throwaway container when `rmtree` cannot.                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| **Nothing is published**                   | no `-p`, ever; a nested container's published port stays inside the card.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| **It is bounded**                          | `--cpus`, `--memory` and an equal `--memory-swap` (`worker_card_cpus`/`_memory`), plus `--pids-limit`. sessiond **refuses to start a card** whose limit does not fit beside the running ones in the VM minus `worker_vm_reserve_mb` (limits, not usage).                                                                                                                                                                                                                                                                                                                                                                            |
| **Its image**                              | the repo's `.devcontainer` **`image:` pinned by digest** (`name@sha256:...`) when present, else the default image built from `templates/card-image.Dockerfile.j2`, named `worker_card_image_name` and tagged with the Dockerfile's hash. **`build:`/`dockerFile` is refused (422)**: `docker build` would run card-supplied steps (`RUN --network=host`, a `# syntax=` frontend) on the HOST daemon, outside sysbox. **Ignored on purpose:** devcontainer `features`, `mounts`, `forwardPorts`, lifecycle commands.                                                                                                                 |
| **The agent tools**                        | Node, Claude Code and Codex are installed once on the VM and mounted read-only at the same path, `PATH` points there. A bump is a converge, not an image rebuild. A devcontainer image must be glibc-based (the mounted Node is).                                                                                                                                                                                                                                                                                                                                                                                                   |
| **Credentials**                            | the container gets the variable NAMES listed in `agent.env` via `docker exec -e NAME` (values come from the sourced environment, never a command line). `~/.gitconfig` uses `gh auth git-credential`; broker mode writes a scoped token to the card's gh configuration.                                                                                                                                                                                                                                                                                                                                                                                    |

Card lifecycle, as sessiond implements it:

- `launch` creates the container (or starts an exited one) and the tmux session;
  a card in the queue holds a worktree and no memory.
- `pause` / `resume`: `docker pause` / `unpause` (no CPU while paused, memory
  kept). `resume` on a paused card with a live tmux client only unfreezes it:
  **the same process continues**. Otherwise it recreates a running or paused
  container (an old `docker exec` can outlive its client; a second agent beside
  it would be worse than a restart) or starts an exited one, and relaunches with
  `claude --resume <session_id>` / `codex resume --last`. The agent's session
  files live in the card's mounted HOME and `CODEX_HOME`, so they survive the
  recreation.
- `stop`: remove the container first, then the tmux session, and confirm
  **both** plus the process tree; the workspace stays. `done`: stop, remove the
  worktree, the card's directories and any devcontainer image; the branch stays
  (it is the PR's head).
- `poll-pr`: reads the PR's inline comments, reviews and conversation plus its
  checks through `gh`, drops everything not from an `author_association` of
  OWNER/MEMBER/COLLABORATOR or a login in `worker_pr_reviewer_logins` / the repo
  owner (prompt injection into a credentialed agent), and types what is **new**
  into the same session as one line, with control characters and newlines
  stripped. The cursor moves only after a successful send; a peek moves nothing;
  a stopped card is never typed into or woken. Red CI is reported once per head
  commit.
- **sessiond polls on its own** every `worker_pr_poll_seconds` (300; 0 = off,
  leaving it to the client's own heartbeat), over every running or paused card,
  and **wakes a paused card** that has new TRUSTED review items
  (`poll-pr {wake: true}` is the same thing on demand): unpaused when its pane
  is alive, so the same process gets the text; otherwise recreated with
  `claude --resume <session_id> <feedback>`, so the feedback is the resumed
  session's first prompt. Only `paused` wakes (a stopped card was terminated by
  a person), only review items wake (red CI alone does not), and an untrusted
  comment is dropped before it can wake anything. A card with no open PR is
  skipped quietly.
- **paused too long**: `GET /v1/sessions` carries `paused_for_seconds` and
  `paused_too_long` per card (true past a day, `null` when the pause time is
  unknown), and `hermes_worker_paused_too_long_cards` counts them.
- `pr-ready`: `gh pr ready` **only when every check passed**. Red, pending, no
  checks at all (unless `worker_pr_allow_no_checks`) and an unreadable check
  list all refuse with 409 and the failing names, and `ready` is read back (a
  `gh` that exits 0 and changes nothing is a 500).

A `daemon.json` change restarts Docker, which stops running cards (sysbox does
not support `live-restore`); `resume` brings them back.

**Stated limits.**

- **The PR gate is cooperative.** A GitHub credential is available to every card (it
  must push its own branch), so a card CAN run `gh pr ready`/`merge` itself.
  `pr-ready` binds an agent that uses it, nothing more. The real gate is a
  repository ruleset (required checks and a required review on the default
  branch), which is a GitHub setting, not something this role can enforce.
- **ttyd's credential is on its command line** (`-c`, visible to any user on the
  VM) and crosses the network to the reverse proxy over plain HTTP unless the
  proxy runs on the VM. ttyd offers `--ssl`; this role does not configure it.
- **`.devcontainer` builds are refused**, so no card-supplied build step runs on
  the host daemon.
- **No egress filter**: a card can reach anything the VM can.
- **The client that reaches the worker over ssh is effectively root on the VM**
  (the worker user is in `docker`), so that client is trusted.
- **The VM's own dockerd**: `worker_docker_bip` and
  `worker_docker_address_pools` set its bridge address and the pool each card's
  network is carved from, for a network whose addresses would collide with
  docker's defaults. Empty leaves docker's own.

## Operating it

| To...                   | How                                                                                                                                                                                                        |
| ----------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Jump into a session** | `ssh <worker_user>@<worker> -t tmux attach -t card-<id>`, or the ttyd page with `?arg=<id>` (ttyd runs `worker-attach`) behind a reverse proxy that adds `worker_ttyd_credential`                          |
| **List every session**  | `GET /v1/sessions` on sessiond (state, agent, repo, branch, MCPs); scrubbed `tail` once `worker_card_tail_enabled` is on                                                                                   |
| **Add an MCP**          | add an entry to `worker_mcp_catalogue` (review it like code), put its credential in `worker_extra_env`, converge; a card asks for it by name at `launch`. A name not in the catalogue **refuses to start** |

## worker-sessiond

A stdlib-only HTTP service (bearer token) that owns the per-card lifecycle. An
agent hub on another machine makes its own worktrees locally, and an ssh
terminal backend does not move them, so the worktree is made here.

| call                                                              | behaviour                                                                                                                                                                                                      |
| ----------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `POST /v1/sessions {card_id, repo, branch?}`                      | worktree at `work_root/worktrees/<card>`; **one session per card** (409 on a duplicate); `repo` must belong to the configured namespace and not be excluded; card id is a whitelist                                                                     |
| `POST /v1/sessions/<card>/launch {agent, prompt, mcp?, resume?}`  | per-card MCP file rendered from the **catalogue only**, the card's container, then a tmux session `card-<id>` whose job is `docker exec -it` into it                                                           |
| `POST /v1/sessions/<card>/send {text}`                            | typed into the pane literally, then Enter                                                                                                                                                                      |
| `POST /v1/sessions/<card>/stop`                                   | removes the container, kills the session **and confirms**: `stopped: true` only if the container is gone, no process of the pane's tree survives and tmux no longer lists it; otherwise 500 with the survivors |
| `POST /v1/sessions/<card>/pause`, `/resume {agent?}`, `/done`     | `docker pause` / `unpause` (or recreate and `--resume`) / stop and remove the workspace; see the container shape above                                                                                         |
| `POST /v1/sessions/<card>/poll-pr {deliver?, wake?}`, `/pr-ready` | GitHub review comments and checks into the same session; mark ready only while CI is green                                                                                                                     |
| `GET /v1/sessions`, `POST /v1/reconcile`                          | `sessions.json` joined with `tmux`; a record that says running with no tmux session becomes `vanished`                                                                                                         |
| `GET /v1/sessions/<card>/tail`                                    | scrubbed pane text; **403 until `worker_card_tail_enabled`**                                                                                                                                                   |
| `GET /v1/metrics`                                                 | the stuck-card metrics (Prometheus text), see below                                                                                                                                                            |

Known limits: `stop` records the pane's process tree just before the kill, so a
process that detached itself earlier (setsid + double fork) is outside it; the
scrubber is a denylist of credential shapes, which is why the tail is off by
default; Codex's per-card `config.toml` forwards MCP credentials by NAME only —
`env_vars` for stdio (each value must be `${KEY}` under its own key, gated),
`bearer_token_env_var` / `env_http_headers` for HTTP (a header that neither form
can express is refused rather than dropped, since a dropped Authorization header
looks like a broken MCP server).

### What a converge manages, and what it does not

What sessiond does at run time — make a worktree, start a card's container,
launch, stop or resume a session — is **application runtime activity** inside
the guest the role deploys, not configuration management, so no play drives it.
That holds only while sessiond touches **the data it owns inside that guest**
(`work_root`, its own containers and tmux sessions): a new port, a mount outside
its root, a credential or another machine is platform state, and belongs in this
role's inputs and a reviewed change. A converge of this role reconciles the
platform and never the cards' sessions or worktrees.

## Inputs

All in [`meta/argument_specs.yml`](meta/argument_specs.yml) (generated from
`meta/argument_specs.py`, as is `defaults/main.yml`). Which host, user, repos,
models, listeners and MCP servers is installation configuration and comes from
inventory; the role's defaults are generic.

Credentials live in `0600` files owned by their user (`agent.env`,
`sessiond.env`, `ttyd.env`); sessiond's own token is popped from its environment
before it starts tmux, so a pane does not INHERIT it. **That is hygiene, not
isolation**: sessiond, ttyd and every agent run as the same user, so an agent in
a pane can read `/proc/<sessiond pid>/environ` (`os.environ.pop` does not change
it), read the ttyd credential off ttyd's command line, or simply
`tmux kill-server`. Within one uid nothing protects one card from another; the
boundary that holds is the guest. Card-level isolation would mean sessiond and
ttyd under their own user and a tmux socket per card, which this role does not
provide.

## Example

```yaml
- name: Deploy a coding worker
  hosts: coding_worker
  become: true
  roles:
    - role: richard_lynch.services.service_coding_worker
      vars:
        worker_user: coder
        worker_git_user_name: "coding-worker"
        worker_git_user_email: "coding-worker@example.com"
        worker_ssh_authorized_key: "ssh-ed25519 AAAA... operator@example.com"
        worker_tools:
          claude_code_version: "2.0.0"
          codex_version: "0.50.0"
          node_version: "22.11.0"
          node_sha256_linux_x64: "<64 hex>"
          gh_version: "2.60.0"
          gh_sha256_linux_amd64: "<64 hex>"
          uv_version: "0.5.0"
          uv_sha256_linux_x64: "<64 hex>"
        worker_sysbox: { version: "0.7.1", sha256_linux_amd64: "<64 hex>" }
        worker_repo_owner: example-org
        worker_repos_exclude: [archived-repo]
        worker_repo_base_branches: {example-repo: main}
        worker_litellm_url: "http://gateway.example.com:4000"
        worker_litellm_api_key: "{{ vault_worker_litellm_key }}"
        worker_claude_model: coder
        worker_codex_model: coder
        worker_github_token: "{{ vault_worker_github_token }}"
        worker_sessiond_token: "{{ vault_worker_sessiond_token }}"
        worker_ttyd_credential: "operator:{{ vault_worker_ttyd_password }}"
```

In practice set these in the worker's host_vars rather than inline. The worker accepts
any repository in `worker_repo_owner`, except names in `worker_repos_exclude`. This is a deliberate
installation choice: namespace plus explicit exclude list, declared in inventory. The exclude list
is required; an empty list explicitly excludes nothing. It clones a
repository on demand, fetches it for later cards, and uses the remote default branch unless
`worker_repo_base_branches` supplies an override.

Shared full repository caches live at `work_root/repos/<name>`; card copies live at
`work_root/worktrees/<card>`. Earlier caches at `work_root/<name>` are no longer used
and may be removed after checking they are not needed.

## Per-card tokens from a broker

Run the broker on a different host from the worker. That host holds the GitHub App private
key; the worker holds only the broker bearer. The App needs contents and pull requests
write, checks and statuses read, and implicit metadata read. Actions read is opt-in: grant the App that permission first, then set `coder_token_broker_actions_read: true`. Each card receives a one-hour token scoped to
its one repository in its gh configuration, refreshed before expiry. Sessiond's own GitHub
calls use the same per-repository tokens. Without a broker URL, the legacy `worker_github_token`
is forwarded to cards instead. A compromised worker host can use its broker bearer to request
any repository in the namespace; the broker logs every mint with the card and repository.

The broker entry point accepts the App ID, private key, bearer, listen host and optional
port, plus the same owner and exclusions as the worker:

```yaml
- name: Deploy a token broker
  hosts: broker
  become: true
  tasks:
    - name: Install the broker
      ansible.builtin.include_role:
        name: richard_lynch.services.service_coding_worker
        tasks_from: token_broker
      vars:
        coder_token_broker_app_id: "{{ vault_app_id }}"
        coder_token_broker_private_key: "{{ vault_app_private_key }}"
        coder_token_broker_bearer_token: "{{ vault_broker_bearer }}"
        coder_token_broker_listen_host: "<listen-address>"
        coder_token_broker_listen_port: 7693
        worker_repo_owner: example-org
        worker_repos_exclude: [archived-repo]
```

Set `worker_token_broker_url` to the broker's HTTP(S) URL and
`worker_token_broker_bearer` to the same bearer on the worker. `worker_github_host`
defaults to `github.com`.


## What a card's agent may run (the Bash allowlist)

`worker_claude_settings` (the role default is `{}`, i.e. no policy) is written
by sessiond, at **every** launch and resume, as the card container's **managed
settings**: the card's `policy/` directory (sessiond's own, mounted nowhere
else) is bind-mounted **read-only** at `/etc/claude-code`. Managed rules outrank
user, project and `--settings`, and the card's root can read the file but cannot
write, remove, unmount or remount it, so a deny cannot be removed from inside
the card; anything the agent CAN write only adds allows, which a deny still
beats. A container made before this mount existed (no `hermes.worker.policy`
label) is recreated at its next launch. Launch and ACP execution require a
container runtime. The policy is not written into the card HOME: the
agent's Claude Code creates `~/.claude` as the container's root, a uid sessiond
cannot write as.

**Never run git on the VM against a card's clone.** The agent controls its
`.git/config` (fsmonitor, hooks, pager, external diff, clean filters), so that
is the agent's choice of command run as whoever runs git. sessiond runs git only
at `create`, before the agent exists; read a card's git state by running git
INSIDE the card's container. `test_worker_sessiond.py` checks that PR polling never runs git against a card clone, even when
its configuration contains commands. `{card_branch}` and
`{card_worktree}` in a rule become that card's branch and absolute clone path.
With `worker_claude_permission_mode: dontAsk`, a command the list does not allow
is DENIED and the agent reads the denial; under `acceptEdits` the same command
PROMPTS, and an unattended card then waits for nobody.

The role and sessiond both refuse `bypassPermissions`,
`skipDangerousModePermissionPrompt`, and an allow rule admitting every Bash
command. What the rules actually MATCH is only knowable from Claude Code's own
engine, so prove a policy live: run `claude -p` in a real card against a list of
commands that must be allowed and denied.

The list is not the security boundary: `make` and a venv's `python` execute code
the agent can write. It stops the dangerous GitHub verbs and credential reads;
the container, the PR-scoped token and the repository's ruleset are the
boundary.

## ssh: the forwarded variables and the extra key

A client that reaches this guest over ssh (an agent hub's ssh terminal backend)
can forward `WORKER_SESSIOND_URL`, `WORKER_SESSIOND_TOKEN` and `WORKER_TERM_URL`
with ssh `SendEnv`. So:

- `worker_ssh_accept_env` renders `/etc/ssh/sshd_config.d/50-worker-env.conf`
  (`AcceptEnv` those names, names only, no value) and reloads sshd (reload, not
  restart: live ssh sessions and the tmux panes attached through them survive).
  **Every name must match `WORKER_[A-Z0-9_]+`**: accepting `PATH` or
  `LD_PRELOAD` would let a login choose the environment of every command, so the
  prefix is a gate, refused with its own message and proven able to fire by
  molecule. Empty removes the file. The read-back is `sshd -T`: the _effective_
  configuration must list every name, which also proves the drop-in is included
  at all (a present-but-ignored drop-in would be a silent no-op).
- `worker_extra_authorized_keys` lists more public keys for the worker user: the
  client's own key goes here rather than sharing a human's jump-in key. A
  private key is refused outright.
- `worker_coder_ctl_src` installs a client helper script (`coder_ctl.py`) from
  the controller as `/usr/local/bin/coder_ctl`, for an ssh terminal backend that
  copies nothing to the remote. The read-back is `coder_ctl --help` naming
  `list`.

The supervisor's token is therefore a line in the client's environment and a
file on no worker disk, and an agent that `worker-sessiond` starts is not a
child of the ssh session that carries it. Within the one worker uid a process
can still read another's `/proc/<pid>/environ`: this is hygiene against
inheritance, as above, not isolation between cards.

## The stuck-card metrics

`GET /v1/metrics` renders, from a fresh reconcile and tmux's own
`session_activity`:

| series                                                                      | meaning                                                                                                                                                                                                               |
| --------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `hermes_worker_metrics_timestamp_seconds`                                   | when the file was written. **The heartbeat of the exporter itself**: if sessiond or the puller dies, every other series keeps its last value for ever, which reads as a healthy idle worker. Alert on this one's age. |
| `hermes_worker_sessions{state}`                                             | every state is emitted every time (0 included), so a vanished series means the exporter stopped, never "none in that state"                                                                                           |
| `hermes_worker_sessions_disagree`                                           | records tmux does not have (`vanished`: a worker restart killed it) **plus** tmux `card-*` sessions with no record (an orphan nobody can list or stop)                                                                |
| `hermes_worker_stuck_cards`, `_oldest_idle_seconds`, `_stuck_after_seconds` | `running` sessions whose pane has produced no output for longer than `worker_stuck_after_seconds` (1800). An agent waiting at a prompt looks the same, which is the other thing the alert is for                      |
| `hermes_worker_running_activity_unknown`                                    | running sessions whose activity could not be read. **Not** counted as stuck (a missing field must not page); counted here so the unknown is visible                                                                   |

**A root timer files them, not sessiond.** sessiond runs as the agents' own
user; a textfile-collector directory that user can write is one an agent can
forge any host metric into (`node_*` included). So `worker-metrics.timer` (every
30 s) runs `worker_metrics_pull.py` as root: it fetches `/v1/metrics` with the
token from sessiond's own `0600` file, validates it **is** the exposition
(required series present, trailing newline, every line a sample), and writes it
atomically `0644` into `worker_metrics_textfile`. On any failure it exits
non-zero and **leaves the previous file alone**, so the timestamp ages and the
staleness alert fires; deleting the file would make every series vanish, which
an alert on a value cannot see, and writing zeros would read as a healthy
worker. The converge's last read-backs run the puller once and assert the file
is fresh, carries the series an alert reads, and is `root:0644`.

**The token goes to whatever owns the port.** sessiond runs as the agents' own
uid, so any agent can kill it and bind its port before systemd restarts it; the
puller then presents the bearer token to that process and files what it serves
as root. So the puller **refuses any sample outside the `hermes_worker_*`
family** (a forged `node_*` or `up` line fails the whole pull and the previous
file stays), and the exposure is the token, not the collector. A unix socket
with an `SO_PEERCRED` uid check would close that too; it is not built.

The role ships no alert rules: they belong to the caller (stuck cards, sessions
disagreeing, paused too long, stale metrics). Keep a paused-too-long rule's
threshold equal to `DEFAULT_PAUSED_TOO_LONG_SECONDS` in `worker_sessiond.py`;
the suite checks the two agree when `WORKER_ALERT_RULES_FILE` names the rule
file.

## Protocol mode (`worker_acp_enabled`)

A card can run its agent as an **ACP session under `acpx`** (pinned) instead of
a tmux pane, in the **same** per-card sysbox container. What is installed and
where:

- `acpx`, `@agentclientprotocol/claude-agent-acp` and
  `@agentclientprotocol/codex-acp`, at the versions in `worker_tools`, into the
  Node prefix every card container bind-mounts read-only. The card's
  `~/.acpx/config.json` (written by sessiond) points acpx's `claude`/`codex` at
  those binaries, so acpx never `npx -y`s a package from inside a card. The
  read-back asserts `acpx --version` and `npm ls -g` against the pins.
- worker-sessiond's `/v1/acp` endpoints (see the module docstring):
  `session_new` needs a card that `create` made and never launched, and a `cwd`
  that IS its worktree (exact normalised path: not a subdirectory, not a path
  sessiond did not create: 403); `prompt` queues with `--no-wait` (the prompt on
  stdin, never argv); `resume` unpauses, starts or recreates the container and
  acpx reconnects with `session/load` (whether it really continued is
  `same_session` in the status, because acpx silently falls back to a NEW
  session when loading fails). The session is BOUND at the first turn the agent
  received, not at `sessions new`: Claude's adapter persists nothing for an
  unprompted session, so the first prompt's `session/resume` answers -32002 and
  acpx runs that turn in a fresh session. `same_session` is true when acpx holds
  the bound session now AND every `session/prompt` in its journal went to it;
  `acp_session_id_at_new` keeps the unprompted id for the record. `send` and the
  PR loop queue a prompt on a protocol card, so comment steering works
  unchanged.
- **The cancel is confirmed, never assumed** (`acp_cancel`): ACP
  `session/cancel`; then every turn the card submitted must have a `turn_result`
  in acpx's own session journal; then a sweep of the container's process table:
  anything that is not the agent's own chain (acpx queue owner -> adapter ->
  CLI, a contiguous chain of agent binaries), the container's entry process or
  the inner docker daemon's tree is a tool the agent left running and is
  SIGKILLed and read back; if the turns do not settle inside
  `worker_acp_cancel_budget_seconds` (4 s), the process table cannot be read, or
  a leftover survives, the container is **killed** (the session stays resumable:
  its state lives in the mounted HOME). acpx can report `cancelled` and settle
  the turn while the agent's tool keeps running, which is why the sweep exists.
  Limits: nested containers the agent started under the inner dockerd keep
  running (stop/done remove them), and a stdio MCP server whose binary is not an
  agent binary is swept at a cancel unless listed in `worker_acp_keep_comms`.
- **acpx-mcp** (`files/acpx_mcp.py`, `worker-acpx-mcp.service`): an MCP server
  (streamable HTTP, JSON responses) with exactly six tools (`session_new`,
  `session_prompt`, `session_cancel`, `session_list`, `session_resume`,
  `session_status`) mapped one-to-one onto those endpoints. It holds its own
  token (`worker_acpx_mcp_token`, what a client presents) and sessiond's (for
  the loopback hop), so a client can drive ACP sessions and nothing else
  sessiond does. A browser Origin is refused. A client's MCP catalogue entry for
  it should list exactly these tools; the suite checks that when
  `ACPX_MCP_CATALOGUE_FILE` names the catalogue.
- **Permissions**: a protocol session has no person at a prompt, so whatever
  acpx does not approve is denied (`--non-interactive-permissions deny`).
  `worker_acp_permissions` defaults to `approve-reads`; `approve-all` lets the
  agent edit and run commands inside its sysbox container.
- **MCP in protocol mode**: acpx's MCP format carries literal header values, so
  a catalogue entry with headers is refused for a protocol card (422; use
  interactive mode); a stdio entry's `${KEY}` env is inherited from the
  forwarded agent environment rather than written.
- **Preference**: sessiond reports `preferred_mode: protocol`
  (`GET /v1/acp/capabilities`) only when `/etc/worker/acp-accepted.json` says
  `passed` at EXACTLY the pinned versions. Write that file from an acceptance
  test of the cancel on the VM, and remove it on a failure, so a version bump
  returns new cards to tmux until the test runs again.

## Tests, and what each proves

- `files/test_worker_sessiond.py` (stdlib only; `WORKER_SESSIOND_REQUIRE_LIVE=1`
  makes a missing tmux a failure instead of a skipped group): card-id/branch
  whitelists, the MCP gate (unknown refused, near-miss accepted), the Codex
  render (bearer and header forwarding by name, a non-forwardable header and a
  mismatched stdio env name refused), the scrubber against credential canaries
  plus an ordinary-text near-miss, reconcile, the process tree, HTTP auth, and a
  **live group** against a real tmux server and git repo: one-per-card, launch,
  send, tail, and **stop with a background child that escaped the pane's process
  group**. It also covers the metrics (the threshold boundary, only `running`
  can be stuck, unknown activity counted apart, disagreement, clock skew, a
  valid exposition, and through real tmux the same session read an hour later is
  stuck), `worker_metrics_pull.py` (each defect refused by name, a failed pull
  leaves the previous file byte-for-byte, a wrong token writes nothing), and the
  container shape against **real tmux and git with a stub `docker` and `gh`**
  (the exact `docker run` argv, the runtime read-back refusing a `runc`
  container, the VM-fit refusal and its near-miss, pause/resume, stop and
  `done`, devcontainer images, the PR poll and `pr-ready`). It cannot prove
  sysbox itself.
- `files/test_worker_sessiond_acp.py` (with the REAL acpx installed at the
  pinned version in `ACPX_BIN`; `WORKER_ACP_REQUIRE_LIVE=1` makes a failed
  install red): acpx's result documents, the journal's turn results, the
  leftover sweep, the cwd restriction, the ACP MCP render, the acceptance
  marker, the exact argv (values never on a command line, the prompt on stdin)
  and the cancel ladder; and a LIVE group where a tool that keeps running stops
  within 5 s of `session_cancel`.
- `files/test_acpx_mcp.py`: exactly six tools; each maps to one sessiond call;
  unknown arguments, traversal-shaped ids and out-of-range values are refused
  before sessiond is asked; a sessiond refusal is `isError` (with a 2xx
  near-miss); no token, a wrong token and a browser Origin are refused.
- `molecule/default` (render-and-gate): the role's own `_gates.yml` passes a
  good input and **each bad input is refused by its own gate** (the rescue
  asserts the failure names the expected gate); the templates render under the
  role's real resolver and derivation for a loopback and a LAN-bind variant,
  with and without the docker network options; no credential appears in any 0644
  artifact; `worker-attach` refuses a hostile id and, as a near-miss, gives a
  _different_ refusal for a well-formed one; protocol mode renders OFF and ON.
- **Not proved by either:** that the agents run, that LiteLLM accepts their
  routes, that apt/npm succeed on the guest. The read-backs at the end of
  `tasks/main.yml` assert those on the live guest (versions, sessiond 200/401,
  ttyd 401/200, listeners, file modes).

## What it does not do

- It does not create the VM, its DNS name or the reverse proxy in front of ttyd.
- It does not install alert rules, an MCP catalogue's servers, or the agent hub
  that drives sessiond.
- It never converges cards, sessions or worktrees.
