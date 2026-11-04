# config_hermes_learning

A learning loop for a [Hermes](https://github.com/NousResearch/hermes-agent)
agent: a ledger of what the agent learned, a fixed probe set that says whether
it got better or only changed, a weekly memory consolidation that can never lose
more than a cap, a Slack reminder for anything waiting for approval, and the
pinned install of a catalog memory provider.

It is a `config_*` role: it configures one concern (learning) on a host that
runs Hermes in Docker; `service_hermes` deploys the agent. Every
installation-specific value (paths, the gateway, the model aliases, the probe
set) is an option the caller sets; nothing here names a host.

## How it reads the agent

Everything runs as **root on the host** from three systemd timers, but every
read of the agent's state is done by the **agent's own interpreter inside its
container**: each `files/inner_*.py` is piped to
`docker exec -i -u <agent uid> -w /opt/hermes hermes /opt/hermes/.venv/bin/python -`.
So the host never holds a credential: the API key, the gateway key and the Slack
token are read from the container's environment by name (`config.json` carries
env-var NAMES only), and anything staged is owned by the agent exactly as its
own writes are.

### What root writes, and where

Root's own state (the weekly ledger, probe history, ping de-duplication) lives
in `state_dir` (`/var/lib/hermes-learning`, 0700) and the metrics in the
textfile directory: both root-owned. The **mirror copies**
(`<state root>/learning/{ledger,probes,consolidation}/`) are the only writes
into the agent's tree, and that tree is writable by the agent, so a symlink it
plants there must never redirect a root write. So `agent_write` forks a child
that drops to the agent's uid/gid (`setgroups([])`, `setresgid`, `setresuid`)
before writing, and the write itself walks the path from an anchor fd with
`O_NOFOLLOW` at every component, requires each directory to be owned by the
writer, and renames a fresh `O_EXCL|O_NOFOLLOW` file over the name (a rename
replaces a link, it never writes through one); the walk, the rename and the uid
drop are `files/confined_io.py` (installed beside the script). The role creates
those directories as the agent's with `follow: false`, after a stat-and-assert
that none is a link or owned by anyone but root or the agent. Root reads nothing
from the agent's tree: every read is the inner scripts, as the agent.

| job           | timer     | what it does                                                                                                                                                                                                                                                                                                                                                   | writes                                                                                       |
| ------------- | --------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| `hourly`      | hourly    | pending approvals (`pending/{memory,skills}/*.json`) with ages; skill events from `skills/.curator_ledger.jsonl`; approved/rejected **inferred** for writes that vanished (Hermes keeps no record of which); the weak-model guard (assistant "I saved that" with no `memory`/`fact_store`/`skill_manage` call in the session); the **24 h Slack ping**         | `hermes_learning.prom`, root-only state, `learning/ledger/<week>.json` (counts and ids only) |
| `probes`      | 03:30     | the fixed probe set through `/v1/runs` on `hermes_learning_probe_model`; judged on the host, so an expected answer never enters the container; a probe that passed last run and fails now is posted to Slack with what is pending                                                                                                                              | `hermes_probe.prom`, probe history, `learning/probes/latest.json`                            |
| `consolidate` | Sun 04:00 | one `hermes_learning_consolidate_model` request proposes a `MEMORY.md` rewrite in which every new entry cites the prior entries it carries; **refused when more than `hermes_learning_consolidate_max_loss` (25 %) of prior entries are uncited**; otherwise staged as ONE pending memory batch that `/memory approve <id>` replays through Hermes's own store | a pending record, `learning/consolidation/<ts>.{json,proposed.md}`                           |

`hermes_learning.py recall <phase>` (a cold recall test against a scratch store)
and `init-store` are driven by the caller's plays, not timers.

A run that cannot read the agent (container down, wrong home, docker missing)
writes `hermes_learning_read_ok 0` / `hermes_probe_read_ok 0` and exits
non-zero. It never writes zeros that read as "nothing pending".

## The memory provider

With `hermes_learning_memory_plugin` set, `_memory_provider.yml` installs that
catalog plugin at an exact commit (`--ref`), writes its `plugins.<section>.*`
settings with the agent's own `hermes config set` (only when the read-back
differs), restarts the stack through compose once when either changed, requires
`hermes memory status` to name it, and opens the live store once so its database
exists before `config_agent_state` is told to back it up. Activation itself
(`memory.provider`) is a `hermes_config_owned` leaf, owned by `service_hermes`.

## Entrypoints and options

One entrypoint, `main`. Options are in `meta/argument_specs.yml`. Required: the
host path of the agent's state directory, the textfile directory, the probe set,
the gateway URL and key env-var name for consolidation, and the two model names.

```yaml
- name: Install the learning loop
  hosts: agent_hosts
  become: true
  tasks:
    - ansible.builtin.include_role:
        name: richard_lynch.services.config_hermes_learning
      vars:
        hermes_learning_host_home: /srv/hermes/data
        hermes_learning_textfile_dir: /var/lib/node_exporter/textfile_collector
        hermes_learning_consolidate_gateway_url: http://gateway.example.com:4000/v1
        hermes_learning_consolidate_key_env: GATEWAY_API_KEY
        hermes_learning_probe_model: example-cheap
        hermes_learning_consolidate_model: example-aux
        hermes_learning_probes:
          - id: p-arith
            prompt: "What is 17 multiplied by 23? Reply with the number only."
            expect: { regex: ['\b391\b'] }
```

## What it does not do

- It never writes `MEMORY.md` or any skill: consolidation only stages a pending
  batch for approval.
- It does not deploy or configure the agent itself (`service_hermes` does), and
  it does not back up the agent's state (`config_agent_state` does; add
  `learning/` to its mirror paths to have the ledger and probe summaries
  committed).
- It does not alert: it writes metrics for the caller's alert rules.

## Tests

- `tests/test_hermes_learning.py` (stdlib): the loss cap at and over 25 %, the
  24 h ping and its repeat window, probe judging, recall@5, approval inference,
  the cannot-tell paths, and the inner scripts run for real against a fixture
  home and a fake `/v1/runs` (a broken skill lowers the pass rate).
- `tests/test_confinement.py` (stdlib): a symlinked `learning/`,
  `learning/ledger`, `probes` or `consolidation` directory, or a symlinked file
  name, must change nothing outside the tree (hourly and consolidate end to end
  too); `..`/absolute paths and a foreign-owned directory are refused; the
  privilege drop happens before the write and a failed drop writes nothing. Each
  beside a near miss of ordinary files that must still be written.
- `molecule/default`: installs in `exec_mode: local` against a fixture home; the
  read-back must report the staged write as pending and over 24 h old; second
  converge `changed=0`; a converge over a planted `learning/ledger` link must
  stop at the HARD GATE with the outside directory unchanged.
