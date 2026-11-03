# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

from ansible_devkit.argspec.models import ArgumentSpec, ArgumentSpecs, OptionSpec
DEFAULT_AUTHOR = "Collection maintainers"

argument_specs = ArgumentSpecs(
    argument_specs={
        "main": ArgumentSpec(
            short_description=(
                "Install a systemd timer that reaps orphaned Jellyfin transcode segments"
            ),
            description=[
                "Jellyfin deletes a session's transcode segments when that session ends CLEANLY. A",
                "client that simply vanishes — closed laptop, killed app, dropped wifi — leaves",
                "them behind, and nothing ever comes back for them.",
                "",
                "Left alone they fill the disk the transcode directory lives on, and every service",
                "sharing that disk then fails (a database returning disk I/O errors, an API",
                "returning 500) while its container still reports healthy.",
                "",
                "RUNS ON THE HOST, NOT FROM THE CONTROL NODE. A cleanup that only happens when",
                "someone remembers to run Ansible is not a cleanup — the host has to look after",
                "itself between runs. Ansible installs the timer; systemd runs it.",
                "",
                "This is the half that handles ABANDONED sessions. Jellyfin's own throttling (see",
                "jellyfin_transcode_config) bounds how much a LIVE session writes. Neither",
                "alone is enough: throttling does nothing about orphans, and reaping does nothing",
                "about one session transcoding a whole film at disk speed.",
            ],
            version_added="1.0",
            author=DEFAULT_AUTHOR,
            options={
                "jtr_transcode_dir": OptionSpec(
                    type="str",
                    required=True,
                    description=[
                        "Jellyfin's TranscodingTempPath as seen from the HOST — the left side of",
                        "the container's config bind mount, not the /config/... path inside it.",
                        "No default: where the config volume lives is the installation's choice.",
                    ],
                ),
                "jtr_older_than_min": OptionSpec(
                    type="int",
                    required=False,
                    default=360,
                    min_value=60,
                    description=[
                        "Only reap segments older than this many minutes. 6h by default: long",
                        "enough that a paused-but-live session is never touched, short enough to",
                        "be useful. Floored at 60 — a shorter window risks deleting segments from",
                        "a session that is merely paused, and the reaper cannot tell the",
                        "difference.",
                    ],
                ),
                "jtr_on_calendar": OptionSpec(
                    type="str",
                    required=False,
                    default="*-*-* 04:00:00",
                    description=[
                        "systemd OnCalendar expression. Daily at 04:00 by default — outside",
                        "plausible viewing hours, so the 'skip while transcoding' guard rarely has",
                        "to fire. The timer is Persistent, so a run missed while the host was off",
                        "happens at next boot.",
                    ],
                ),
                "jtr_script_path": OptionSpec(
                    type="str",
                    required=False,
                    default="/usr/local/sbin/jellyfin-reap-transcodes",
                    description=["Where the reaper script is installed."],
                ),
                "jtr_state": OptionSpec(
                    type="str",
                    required=False,
                    default="present",
                    choices=["present", "absent"],
                    description=[
                        "'absent' removes the timer, unit and script. The cleanup is a host-level",
                        "capability, so retiring it should not mean leaving orphaned units behind.",
                    ],
                ),
            },
        )
    }
)
