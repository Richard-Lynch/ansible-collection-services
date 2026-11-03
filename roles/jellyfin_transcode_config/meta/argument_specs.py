# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

from ansible_devkit.argspec.models import ArgumentSpec, ArgumentSpecs, OptionSpec
DEFAULT_AUTHOR = "Collection maintainers"

argument_specs = ArgumentSpecs(
    argument_specs={
        "main": ArgumentSpec(
            short_description="Configure Jellyfin's hardware transcode settings in encoding.xml",
            description=[
                "Points Jellyfin at a specific GPU and enables hardware decode/encode, by editing",
                "the specific elements it owns in encoding.xml — never by templating the whole file,",
                "which holds ~45 settings Jellyfin manages itself.",
                "",
                "Generate-don't-apply. Backs up encoding.xml before writing, verifies every managed",
                "value against what is actually on disk afterwards, and restarts Jellyfin (which is",
                "REQUIRED — it reads this file only at startup and will otherwise overwrite it from",
                "its in-memory copy).",
                "",
                "NOTE this role and Jellyfin's web UI are two sources of truth for the same file.",
                "Once used, treat the role as authoritative for the settings it manages.",
            ],
            version_added="1.0",
            author=DEFAULT_AUTHOR,
            options={
                "jtc_device": OptionSpec(
                    type="str",
                    required=True,
                    description=[
                        "Render node of the GPU Jellyfin should use, e.g. '/dev/dri/renderD128'.",
                        "Required and never defaulted: a host with both an iGPU and a discrete card",
                        "has two nodes, and defaulting would silently pick the weaker one.",
                    ],
                ),
                "jtc_apply": OptionSpec(
                    type="bool",
                    required=False,
                    default=False,
                    description=[
                        "false (default) reports the diff and changes nothing. true writes the",
                        "settings and restarts Jellyfin.",
                    ],
                ),
                "jtc_accel_type": OptionSpec(
                    type="str",
                    required=False,
                    default="vaapi",
                    description=[
                        "Jellyfin's HardwareAccelerationType. 'vaapi' for Intel on Linux; 'none'",
                        "disables hardware transcode. QSV also exists but dispatches through the",
                        "same iHD driver on Linux, so vaapi is the direct path.",
                    ],
                ),
                "jtc_hw_encoding": OptionSpec(
                    type="bool",
                    required=False,
                    default=True,
                    description=["Enable hardware ENCODING as well as decoding."],
                ),
                "jtc_allow_hevc": OptionSpec(
                    type="bool",
                    required=False,
                    default=True,
                    description=[
                        "Allow HEVC output. Worth enabling on a card with HEVC encode, since it",
                        "roughly halves bitrate versus H.264 at equal quality — but H.264 remains",
                        "the safer default for maximum client compatibility, so Jellyfin still picks",
                        "per-client.",
                    ],
                ),
                "jtc_allow_av1": OptionSpec(
                    type="bool",
                    required=False,
                    default=False,
                    description=[
                        "Allow AV1 output. Default false: even on a GPU with AV1 encode, client",
                        "support for AV1 playback is still patchy, and an unplayable stream is worse",
                        "than a larger one. Enable deliberately.",
                    ],
                ),
                # --- transcode throttling ------------------------------------------------------
                # Jellyfin has NO maximum-cache-size setting in encoding.xml. Throttling is the
                # closest thing: with it on, ffmpeg pauses once it has transcoded far enough ahead
                # of the player instead of racing to write the whole file to disk. It bounds how
                # much ONE session can produce; it does nothing about sessions that end badly and
                # leave their segments behind, which is what jellyfin_transcode_reaper handles.
                # With throttling off, orphaned segments can fill the disk the transcode directory
                # lives on, and every service sharing that disk fails with it.
                "jtc_throttling": OptionSpec(
                    type="bool",
                    required=False,
                    default=True,
                    description=[
                        "Pause transcoding once far enough ahead of the player, rather than",
                        "transcoding the whole file as fast as the disk allows.",
                    ],
                ),
                "jtc_throttle_delay_seconds": OptionSpec(
                    type="int",
                    required=False,
                    default=180,
                    min_value=1,
                    description=[
                        "How far ahead, in seconds of playback, before throttling kicks in.",
                        "Jellyfin's own default; changing it trades disk churn against the risk of",
                        "a stall on a slow client.",
                    ],
                ),
                "jtc_segment_keep_seconds": OptionSpec(
                    type="int",
                    required=False,
                    default=720,
                    min_value=1,
                    description=[
                        "How many seconds of already-played segments to keep on disk behind the",
                        "player. Lower means less disk held per live session.",
                    ],
                ),
                "jtc_tonemapping": OptionSpec(
                    type="bool",
                    required=False,
                    default=True,
                    description=[
                        "Enable HDR->SDR tone mapping: HDR delivered to an SDR client without it is",
                        "the washed-out-picture complaint, and HDR HEVC 10-bit content is what",
                        "hardware transcode is most often enabled for.",
                    ],
                ),
                "jtc_vpp_tonemapping": OptionSpec(
                    type="bool",
                    required=False,
                    default=False,
                    description=[
                        "Intel VPP tone mapping. Faster than the OpenCL path but lower quality and",
                        "historically buggy on newer Arc; left off unless measured to be better.",
                    ],
                ),
                "jtc_restart": OptionSpec(
                    type="bool",
                    required=False,
                    default=True,
                    description=[
                        "Restart Jellyfin after writing. REQUIRED for the change to take effect —",
                        "Jellyfin caches encoding.xml at startup and rewrites it from memory when any",
                        "setting changes in the UI, which would silently revert this role's edits.",
                    ],
                ),
                "jtc_container": OptionSpec(
                    type="str",
                    required=False,
                    default="jellyfin",
                    description=["Container name to restart."],
                ),
                "jtc_config_path": OptionSpec(
                    type="str",
                    required=True,
                    description=[
                        "Path to encoding.xml ON THE TARGET HOST. It is a bind mount, so the",
                        "host-side path is authoritative and editable without docker exec.",
                        "No default: where the config volume lives is the installation's choice.",
                    ],
                ),
            },
        )
    }
)
