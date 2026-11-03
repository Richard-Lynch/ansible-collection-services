# jellyfin_transcode_config

Sets Jellyfin's hardware-transcode settings in its `encoding.xml`: the acceleration type, the
render device, which codecs may be encoded, segment throttling, segment retention and tone mapping.

Runs on the host that holds the bind-mounted Jellyfin config directory, and edits the file
directly (no `docker exec`).

- **Dry run by default.** Without `jtc_apply: true` it prints, per setting, the current value, the
  wanted value and whether it would change, and writes nothing.
- **Refuses to create the file.** Jellyfin writes `encoding.xml` on first start and keeps many more
  settings in it than this role manages, so a missing file is an error, not something to build.
- **Applying** backs the file up to `encoding.xml.bak-<timestamp>`, edits only the managed
  elements, restarts the container (`jtc_restart`, default true: Jellyfin reads the file at
  startup and would otherwise write its in-memory values back over the edit) and waits for it to
  run again. The restore command is printed.

Jellyfin also rewrites this file whenever a setting changes in its web UI, so once this role is
used it should be treated as the source of truth for the settings it manages.

## Inputs

See [`meta/argument_specs.py`](meta/argument_specs.py) (the `.yml` and `defaults/main.yml` are
generated from it). The usual ones: `jtc_device` (the render node, for example
`/dev/dri/renderD128`), `jtc_apply`, `jtc_allow_hevc`, `jtc_allow_av1`, `jtc_throttling`,
`jtc_tonemapping`, `jtc_container`, `jtc_config_path`.

## Usage

```yaml
- hosts: media
  become: true
  gather_facts: true   # the backup file name uses ansible_facts.date_time
  roles:
    - role: richard_lynch.services.jellyfin_transcode_config
      jtc_device: /dev/dri/renderD128
      jtc_apply: false
```
