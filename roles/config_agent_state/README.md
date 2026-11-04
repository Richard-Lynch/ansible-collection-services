# config_agent_state

Keeps an AI agent's state recoverable. Built for [Hermes](https://github.com/NousResearch/hermes-agent),
written so a second product can use it with inventory changes only (`agent_state_product`).

An agent's state comes in layers:

| layer | what | where it goes | how |
|---|---|---|---|
| 2 | persona, memory, learned skills, personal cron jobs, webhook routes, a **masked** config snapshot | a private git repo | nightly mirror, **scan blocks the commit from the first run** |
| 3 | the runtime tree: SQLite databases, OAuth state, vault | a mounted backup share | nightly **age-encrypted** tar, 14-day ring + the first of each month |

This role installs the jobs, runs them on demand, restores, and proves the restore works in an
isolated drill. It is deliberately a role of five entrypoints and one script
(`files/agent_state.py`): every property worth proving is a property of the code, and a script can be
run by the molecule scenario on real files exactly as it runs on the host.

The script runs as root over a tree the agent can write. Every read, rewrite and restore-write of a
file in that tree is an fd walk with `O_NOFOLLOW` at every component (`files/confined_io.py`,
installed beside the script), so a directory the agent swaps for a link after the walk listed it
redirects nothing.

## Entrypoints

| entrypoint | what it does | writes | gate |
|---|---|---|---|
| `main` | installs `age`, the script, two wrappers, the deploy key (0600) and two systemd units + timers; reads the timers back from systemd | the host | none (it is a converge) |
| `backup` | one backup now | one archive + manifest on the share, a metrics file | none; reports `changed` every run (each is a new archive) |
| `mirror` | layer 2 to git now | a commit + push if the scan is clean | the scan itself |
| `restore` | the stopped restore | the state root | `agent_state_restore_confirm` |
| `drill` | restore into an isolated scratch container, read back, tear down | a scratch directory and one container, both removed | `agent_state_drill_confirm` |
| `verify` | read-only: newest backup's age and ciphertext hash, mirror vs live files | nothing | n/a |

Options are in `meta/argument_specs.yml`, per entrypoint. Which host, path, repo and recipient is
the caller's configuration (inventory); the role has no installation-specific value in it.

```yaml
- name: Back up and mirror the agent's state
  hosts: agent_hosts
  become: true
  tasks:
    - ansible.builtin.include_role:
        name: richard_lynch.services.config_agent_state
      vars:
        agent_state_product: hermes
        agent_state_root: /srv/hermes/data
        agent_state_textfile_dir: /var/lib/node_exporter/textfile_collector
        agent_state_backup_target: /mnt/agent-state
        agent_state_backup_recipient: age1examplepublickeyexamplepublickeyexamplepublickey00
        agent_state_repo_url: git@git.example.com:example-org/agent-mind.git
        agent_state_deploy_key: "{{ lookup('ansible.builtin.env', 'AGENT_MIRROR_DEPLOY_KEY') }}"
        agent_state_mirror_paths: [SOUL.md, memories, skills, cron]
        agent_state_sqlite_files: [state.db]
```

## What a backup is

1. Each declared SQLite file is copied with SQLite's **online-backup API** and the copy is
   `PRAGMA integrity_check`ed. A database that fails the check **refuses the backup** and leaves no
   archive. A plain file copy of a live WAL-mode database silently loses whatever is still in the
   `-wal`, and a busy agent's `state.db` routinely has pages pending there.
2. The rest of the tree is tarred minus `agent_state_backup_exclude` (caches, logs, sandboxes,
   browser profiles), `zstd`-compressed, and `age`-encrypted to a **public** recipient. The private
   key stays in the caller's secret store and never lives on the host, so a compromised host can
   write backups and cannot read them.
3. A manifest (`<archive>.manifest.json`) records the ciphertext sha256, every member's sha256 and
   each table's row count. It names files and counts, never content.
4. The ring is pruned to the newest `agent_state_backup_keep` plus the first archive of each of the
   last `agent_state_backup_keep_monthly` months. Only names this role writes for this product are
   ever candidates; the share holds other people's files.
5. A last-success **timestamp** is written to the textfile collector.

## What a restore refuses

The order is fixed: stop; unpack into a stage directory beside the root; **validate while stopped**;
swap (the old tree is kept as `<root>.pre-restore-<epoch>`); quiet start; healthy; re-write layer 2
in place; read back; remove the overlay; restart; read back again. Validation, all before the live
tree is touched:

- the ciphertext's sha256 equals the manifest (a flipped byte);
- `age` decrypts it (a wrong key, or a flipped byte **with the manifest hash rewritten to match**:
  only age's own authentication catches that one);
- every member's sha256 equals the manifest and there is no extra or missing member;
- every database passes `integrity_check` and has the manifest's row counts;
- every `agent_state_required_paths` entry exists.

A restore onto a tree that already **equals** the archive swaps nothing and reports `changed=false`
— equals, not contains: a live tree holding files the archive does not (the agent created skills
or memories since) is restored and the previous tree moved aside, because rolling back is exactly
that case. Excluded paths (`agent_state_backup_exclude`) and SQLite sidecars are not compared.
Directory entries are archived too (mode and owner), so a restore brings back a 0700 directory as
0700 and owned by the agent's uid, and an empty directory at all. Every file is staged and hashed
from the staged copy, so a file the agent writes mid-backup cannot leave the manifest describing
bytes the archive does not hold. A file shaped like a database (`*.db`, `*.sqlite*`) that is not in
`agent_state_sqlite_files` makes the backup **refuse**: a plain copy of a live database can omit
its WAL, and declaring it is one inventory line.

**Bundled skills.** `agent_state_mirror_skip_names_file` names a file of skill names (Hermes: `skills/.bundled_manifest`)
that are left out of the mirror wherever they appear as a path component. The image's own skills are the
vendor's and return with the image; their sample addresses and `api_key=` lines would block the scan on every
run. An agent-learned skill is not in the manifest and is mirrored. Edits made in place to a bundled skill are
not mirrored (they are in the layer-3 archive).

**Quiet start.** `agent_state_quiet_start_env` names the environment variables that switch off every
platform adapter, cron dispatch and the webhook listener on the pinned image. Which variables those
are is product knowledge the operator confirms against the pinned image; **an empty value makes the restore
refuse** rather than start the agent live on half-restored state.

**On failure** the stack is left as it is (stopped, or in quiet start if it got that far) and the
failure says what state that is. Nothing is re-enabled automatically.

## The drill

`drill` restores the newest (or a named) archive into `agent_state_drill_dir` and runs it under a
second container that is isolated **by construction**, rendered from templates and asserted by the
scenario as parsed structure:

- no `ports:` at all: nothing is published on any host interface;
- one docker network with `internal: true`: no route out, so no Slack, mail or model call (a
  model call is not needed to read back). Docker drops host-to-internal-network traffic too, so the
  read-back probes run inside the container (`docker exec`);
- an env file holding only `agent_state_drill_env`, whose **keys are refused if credential-shaped**,
  plus `agent_state_drill_scratch_secrets` which receive random values generated for the run and
  held nowhere else (the drill's API needs *some* key; it gets one nobody else has);
- the mirror deploy key is never mounted, and the production read-back key is never sent to it.

It then asserts the mirror gained no commit and tears down in an `always` block. **What it cannot
observe from this host:** a Slack message, a model-gateway spend line. Those hold by construction (no
route, no credential); checking them in logs or spend reports is the caller's, not this role's.

## Metrics

`agent_state_backup_last_success_timestamp_seconds{product=}`, `agent_state_backup_bytes`,
`agent_state_backup_files`, `agent_state_mirror_last_success_timestamp_seconds`,
`agent_state_mirror_scan_blocked` (1 while the scan is blocking a mirror). A blocked run keeps the
previous `..._mirror_last_success_timestamp_seconds` line, so a staleness rule can still see when
the mirror last worked; a mirror that has **never** succeeded has no such line, so that rule must
`or` on the blocked gauge (`absent_over_time` is the other honest form).

**A timestamp, not an age.** A textfile gauge holding an *age* is frozen at the value it had when
the job last ran, so a job that stops running leaves a healthy-looking number forever. A
**timestamp** has no such failure: the alert computes
`time() - agent_state_backup_last_success_timestamp_seconds`. Alert rules are the caller's; this
role writes metrics only.

## Tests (molecule `default`, real files, real `age`/`zstd`/`git`)

`prepare` takes one real backup. `converge` installs, mirrors to a bare git remote and restores into a
fresh tree; `idempotence` runs it again and requires `changed=0`. The sequence is bracketed by
`destroy` and `cleanup` at BOTH ends: `destroy` resets molecule's "already prepared" flag (which
outlives a passing run and would make the next run skip `prepare`, then fail on the archives the
last `verify` left), and `cleanup` removes the scratch tree, which lives under molecule's ephemeral
directory (`vars.yml`), not in `/tmp`. `verify` proves:

- **round trip**: the restored tree is byte-identical (`diff -r`) to the source, excluded directories
  are absent, and the restored database has all 400 rows while a plain `cp` of the live database has
  fewer (the fixture's rows live only in the WAL, so this is a real WAL-capture proof);
- **encrypted**: the archive is an `age` file and none of the state's plaintext is in it;
- **corrupt-backup refusals**, each for its own reason with the live tree and its marker file
  untouched and no stage/aside directory left: wrong identity, flipped byte, flipped byte with the
  manifest rehashed, a lying member hash, a lying row count; and a **corrupt source database**
  refuses the backup and leaves no archive;
- **the ring**: four backups into a keep-2 ring leave two (archive and manifest) and a foreign file
  untouched;
- **the mirror**: layer 2 reaches git, the runtime layer and the raw config do not, the config
  snapshot is masked; a **planted credential blocks the first mirror** (named by file and rule, value
  absent from the failure and the metrics, nothing staged or pushed, gauge = 1) and the same code
  commits once it is removed;
- **install artifacts**: key and directory modes, the deploy key read back with `ssh-keygen -y` (a
  value that is not a private key is refused at install, not at 02:30), no key body in any wrapper
  or unit, schedules, the recipient in the wrapper, and the nightly wrapper itself run end to end;
- **a restore onto a tree holding MORE than the archive** reports `changed=true` and the extra file
  is gone from the live tree (moved aside with the previous tree);
- **the mirror's last-success line survives a blocked run**, and the blocked gauge is 1;
- **an undeclared `*.db`** refuses the backup, naming the file;
- **the drill as the FIRST role call in a play**, with the drill env coming from play vars (the
  host_vars position, below `set_fact`), proves the rendered `drill.env` carries the inventory's
  keys — the double-resolver bug emptied it while every include-`vars:` test stayed green;
- **the drill's isolation** as parsed YAML, the refusal of a credential-named env key (and clean-up
  after it), a wrong-identity drill failing, the drill end to end on the real archive;
- **verify can fail**: a stale limit, an altered ciphertext, and a mirror whose work clone does not
  exist (reported as "cannot tell", never as "0 files differ").

Mutation checks: disabling the scan fails `verify` at the planted-credential
assertion; disabling the ciphertext hash check fails it at the flipped-byte case.

**What the scenario cannot prove**, and the drill on a real host is for: stopping and starting the
compose stack, the quiet-start overlay taking effect in the daemon, and the API read-backs. The
overlay and the refusal of an empty quiet-start env are tested as artifacts and gates only.

The scenario needs the real `age` binary on the test host.

## Implementation notes

- **A generated defaults file for a non-`main` entrypoint is never loaded** by Ansible (only
  `defaults/main.yml` is), so an option declared only for `backup` was undefined. Each entrypoint
  here loads its own `defaults/<entrypoint>.yml` with `include_vars` first.
- **A second `include_role` in one play ignores play-level vars** (the resolver's repeat-call
  branch rebinds defaults over them); only the include's own `vars:` mapping wins. The scenario
  passes its common inputs through a YAML anchor merged into every call.
- A task-level `vars:` does not override a `set_fact` the role made earlier in the play, so the
  scenario overrides with `set_fact` before rendering the overlay template.
- `changed_when` that parses the script's JSON must check `rc == 0` first, or a refusal surfaces as
  a `changed_when` evaluation error instead of the refusal.
- **Including the generated resolver TWICE in one entrypoint call** (drill.yml, then again from
  `_render_drill.yml`) takes the repeat-call branch the second time and rebinds every option to its
  `__default`, discarding host_vars. Every include-`vars:` test passes regardless, because include
  vars shadow the fact. The scenario runs the drill as the first role call of its own play with
  the env in play vars, and `_render_drill.yml` reads the rendered keys back from the file.

## Operator-supplied inputs

Three inputs come from the operator and may be blank in inventory until they exist; every
entrypoint refuses, naming the variable, while one is blank: a mounted backup share
(`agent_state_backup_target`), an age key pair (`agent_state_backup_recipient` is the public half;
the private half stays in a secret store and is passed only to `restore` and `drill` as
`agent_state_restore_identity`), and a private mirror repository with a write deploy key
(`agent_state_repo_url`, `agent_state_deploy_key`).

`restore` and `drill` are gated by `agent_state_restore_confirm` / `agent_state_drill_confirm`, so a
bare run of either reports and refuses rather than acting.
