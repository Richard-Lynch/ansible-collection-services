# config_hermes_profile

Provisions **one named Hermes profile** on the gateway host: the profile itself (created by
Hermes' own `hermes profile create`), a set of **config leaves**, the profile's `.env`, and a SOUL
seeded once. Call it once per profile (for example a coding profile, a settings profile, a
read-only profile).

`config_*` rather than `service_*`: it configures one cross-cutting concern (a profile) for many
kinds of caller, and deploys no service. The gateway itself is `service_hermes`.

## What it guarantees

| Property | How it is kept |
|---|---|
| **Nothing the agent wrote is erased.** A profile's `config.yaml` is also written by the agent and the dashboard. | Only the given **leaves** are set; every other key is kept (`richard_lynch.services.hermes_leaf_merge`). When nothing differs the file is not touched (the filter returns the input text, so no re-serialisation, `changed=0`). Molecule seeds `memory.write_approval` and a **sibling** of a leaf (`terminal.cwd`) and requires both to survive. |
| **A leaf is on disk as asked.** | Read back from the file by a **different code path** (`richard_lynch.services.hermes_leaf_get`), not by re-running the writer. |
| **Hermes sees the profile.** A leaf can be on disk under a key hermes ignores. | `hermes_profile_readbacks`: commands run through the hermes CLI after the write; an empty answer or a command that cannot run **fails the play**. |
| **The API server keeps serving.** Upstream behaviour: with a named profile present, an `API_SERVER_KEY` supplied only through the compose `env_file` is invisible and the API never **starts**. | Three things, because a probe of the running process cannot see a break that shows at the next start: (1) a **HARD GATE before the first named profile is created**: the default profile's `.env` (`HERMES_HOME/.env`) must carry an `API_SERVER_KEY=` line (name only, `no_log`); (2) the models list is read **before** (so a pre-existing break is not blamed on this change); (3) with `hermes_profile_compose_dir` set, the run that created the profile **restarts the gateway through compose** and the **after** probe is cold (retried while the API comes up). With no compose dir the after probe is warm and the role prints that it is. |
| **Credentials stay out of readable files and output.** | `.env` is `0600` and owned by the gateway uid (asserted). A value with a quote or newline is **refused** (a dotenv reader would not read it back as written); the refusal names the KEY only. Tasks that touch values are `no_log`. |
| **A profile holds only its own `.env`.** `--clone-from` copies the source's whole `.env`: the gateway's API key, chat tokens, provider keys. | In the run that **created** the profile the cloned `.env` is replaced by the managed lines alone; from then on other lines are kept (the agent and the dashboard may add their own). Molecule: a cloned `API_SERVER_KEY` line is gone from the created profile, and the existing profile's foreign line survives. |
| **No cloned platform keeps the source's toolset.** The clone carries `platform_toolsets.<p>` for every chat platform. | With `hermes_profile_platform_toolsets` set, every platform key already in the file is swept to that list before the caller's explicit leaves apply (`richard_lynch.services.hermes_platform_sweep_leaves`, unit-tested). A platform with no key at all is Hermes's default for it, which is why the caller declares every platform it can read back. |
| **A curated SOUL is never overwritten.** | `--seed` unless `_hp_created`: the SOUL is overwritten only in the run that created the profile (a clone brings the source's SOUL, which nobody curated for this one); `_hp_created` is reset at the top of every call, so a second profile in the same play cannot inherit the first's "created". |
| **Root never writes or reads THROUGH a link the agent planted.** The profile directory is under the gateway's data directory, which the agent (the gateway's uid) can write. | Every read and write in the profile, and the default profile's `.env` read, goes through `files/agent_dir.py`: an fd walk from the data directory's **parent** (root-owned, `hermes_profile_anchor_uid`, not group/other-writable) with `O_NOFOLLOW` at every component, a new file renamed over the name, values in the environment and never argv. A link at `.env`, `config.yaml`, `SOUL.md` or the profile directory itself is **refused**, never followed (`copy` would write through it and `lineinfile` always realpath()s its dest). The copy is byte-identical to service_hermes's tested `scripts/agent_dir.py` (`tests/test_agent_dir_copy.py`). Molecule plants all three links and requires each refused and its target untouched. |
| **Generate, don't apply.** | Without `hermes_profile_apply` the role reads, computes and prints whether `config.yaml` would change, and writes nothing. |

## Inputs

All in [`meta/argument_specs.yml`](meta/argument_specs.yml) (generated from
`meta/argument_specs.py`). Everything that is a fact about an installation (which profile,
container, uid, data directory, leaves, models URL and key) is **required, with no default**.

## Example

```yaml
- name: A coding profile on the gateway
  ansible.builtin.include_role:
    name: richard_lynch.services.config_hermes_profile
  vars:
    hermes_profile_name: coder
    hermes_profile_container: hermes
    hermes_profile_data_dir: /opt/hermes/data
    hermes_profile_uid: 10000
    hermes_profile_gid: 10000
    hermes_profile_models_url: http://127.0.0.1:8642/v1/models
    hermes_profile_api_key: "{{ vault_hermes_api_key }}"
    hermes_profile_create_args: [--clone-from, default, --no-alias, --description, Coding agent]
    hermes_profile_platform_toolsets: [terminal, kanban]
    hermes_profile_leaves: "{{ coder_spec | richard_lynch.services.hermes_coder_profile_leaves }}"
    hermes_profile_env:
      WORKER_SESSIOND_URL: http://worker.example.com:7690
    hermes_profile_compose_dir: /opt/hermes
    hermes_profile_readbacks:
      - name: the cli toolset is the declared one
        argv: [hermes, -p, coder, tools, list, --platform, cli]
        must_match: terminal
    hermes_profile_apply: true
```

## The leaf builder: `richard_lynch.services.hermes_coder_profile_leaves`

It **refuses** the configurations that would make a coding profile a way into the gateway, or a
profile that looks configured and is not:

- a toolset name not in the declared `known_toolsets` (a typo enables nothing and reads correctly);
- `code_execution`, `browser`, `delegation`, `cronjob`, `connections`, `computer_use` (each runs
  on, or reaches from, the gateway itself); `terminal` and `kanban` are required;
- an empty or duplicated `platforms` list: one `platform_toolsets.<p>` leaf is emitted per declared
  platform (default: the CLI alone), never only the CLI by omission;
- `approvals.mode` other than `smart` or `manual`; a deny list with no entry covering a recursive
  delete and a force push;
- `terminal.backend` other than `ssh` (a `local` backend is a shell in the gateway container, next
  to every other profile's secrets), or an **empty** terminal block.

## Known limits

- **The default profile's `API_SERVER_KEY` placement.** The role does not fix it: it **refuses to
  create the first named profile** until the key is a line in the default profile's `.env`, and the
  created run's after probe is cold only when `hermes_profile_compose_dir` is given.
  `service_hermes` writes the key into the default `.env` on every converge.
- **`hermes profile create`'s flags** (v0.21.x): `--clone-from SOURCE`, `--no-alias`,
  `--no-skills`, `--description`; from v0.22.x `--no-skills` is refused with any `--clone*`. Without
  a clone flag a new profile has no provider settings and cannot call a model.
- **The `file` toolset with an ssh terminal backend.** If Hermes' file tools run in the gateway
  container, a profile with `file` can rewrite its own `config.yaml` and read other profiles'
  `.env`. Leave `file` out of a confined profile's toolsets unless you have confirmed where it runs.
- **What molecule covers.** The file half (`tasks/_files.yml`): merge, idempotence, keep-others,
  `0600`, SOUL-once, the created-run `.env` prune, the platform sweep, preview-writes-nothing, four
  env refusals, three planted links refused. It does **not** cover `profile create`, the default-key gate, the compose restart,
  the models probes or the CLI read-backs, which need a live gateway container.

## Passing environment to an ssh terminal

A profile whose terminal is an **ssh session on another host** (`terminal.backend: ssh`) can hand
that session named environment variables without a token file on the remote host:

1. the names are lines in the profile's own `.env` on the gateway (`hermes_profile_env`, `0600`,
   written by this role);
2. `terminal.env_passthrough` lists them (Hermes sends them as `SendEnv`);
3. the remote sshd accepts exactly those names (`AcceptEnv`; `service_coding_worker` has
   `worker_ssh_accept_env` for this, with an `sshd -T` read-back).

Processes on the remote host that are not children of that ssh session never inherit the values.
Within one uid a process can still read another's `/proc/<pid>/environ`, so this is hygiene against
inheritance, not isolation. The ssh keypair and `known_hosts` for the remote host are the caller's
to provide.
