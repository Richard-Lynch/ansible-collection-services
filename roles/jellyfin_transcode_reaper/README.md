# jellyfin_transcode_reaper

Installs a systemd service and timer that delete Jellyfin transcode segments a session left
behind. Jellyfin removes a session's segments when it ends cleanly; a client that disappears (a
closed laptop, a killed app, a dropped connection) leaves them in the transcode directory, where
they accumulate until the disk is full.

The script runs on the host from the timer, with no control node involved, and:

- does nothing while an `ffmpeg` process is running (`pgrep -x ffmpeg`: matching the binary name,
  not the command line, because the Jellyfin server's own command line names ffmpeg too);
- deletes only `*.ts` and `*.m3u8` files directly in `jtr_transcode_dir` that are older than
  `jtr_older_than_min` minutes, never the directory itself or anything below it;
- logs what it removed and how much space it freed to the journal.

After installing, the role checks that the timer is enabled and has a next run scheduled.
`jtr_state: absent` removes the units and the script.

## Inputs

See [`meta/argument_specs.py`](meta/argument_specs.py): `jtr_transcode_dir`, `jtr_older_than_min`,
`jtr_on_calendar` (a systemd `OnCalendar` expression), `jtr_script_path`, `jtr_state`.

## Usage

```yaml
- hosts: media
  become: true
  roles:
    - role: richard_lynch.services.jellyfin_transcode_reaper
      jtr_transcode_dir: /srv/jellyfin/config/cache/transcodes
      jtr_on_calendar: "*-*-* 04:00:00"
```
