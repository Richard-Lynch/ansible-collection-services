# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

DEFAULT_AUTHOR = "Collection maintainers"
from ansible_devkit.argspec.models import ArgumentSpec, ArgumentSpecs, OptionSpec

# NEVER put literal Jinja delimiters in a description. Ansible TEMPLATES the argspec, so a
# documentation mention of a Jinja tag is parsed as real Jinja and fails the whole role at
# validate_argument_spec time, before any task runs.

argument_specs = ArgumentSpecs(
    argument_specs={
        "main": ArgumentSpec(
            short_description="Serve the model-discovery app as static files behind nginx",
            description=[
                "Builds nothing at converge time: the app is a Vite bundle built on the",
                "controller from files/app; the built output and the caller's catalogue",
                "snapshot are copied to the host and served by a pinned nginx image.",
                "The app is READ-ONLY and holds NO credential. It reads one JSON snapshot",
                "of the gateway's models; adding a model to Open WebUI's picker is staged",
                "in the UI and applied by an Ansible play, never by the browser.",
            ],
            version_added="1.0",
            author=DEFAULT_AUTHOR,
            options={
                "model_explorer_catalogue_src": OptionSpec(
                    type="str",
                    required=True,
                    description=[
                        "Path ON THE CONTROLLER of the catalogue snapshot (catalogue.json) the",
                        "app serves. It is the caller's data — produced by reading the",
                        "gateway's GET /v1/models and OpenRouter's public catalogue — and is",
                        "copied to the host on its own, so refreshing it needs no rebuild.",
                    ],
                ),
                "model_explorer_dist_dir": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    description=[
                        "Path ON THE CONTROLLER of the built bundle (the output of",
                        "npm ci and npm run build in the role's files/app). Empty means the",
                        "role's own files/app/dist.",
                    ],
                ),
                "model_explorer_container_name": OptionSpec(
                    type="str",
                    required=False,
                    default="model-explorer",
                    description=["Container name."],
                ),
                "model_explorer_image": OptionSpec(
                    type="str",
                    required=False,
                    description=[
                        "nginx image serving the built bundle. Pinned -- never a floating tag:",
                        "a minor-only tag such as 1.27-alpine floats and would silently change",
                        "what is deployed.",
                    ],
                    default="nginx:1.29.3-alpine",
                ),
                "model_explorer_config_dir": OptionSpec(
                    type="str",
                    required=False,
                    description=[
                        "Host directory holding the compose project and the served bundle."
                    ],
                    default="/opt/model-explorer",
                ),
                "model_explorer_network": OptionSpec(
                    type="str",
                    required=False,
                    description=[
                        "External Docker network the container joins (for example one shared",
                        "with a reverse proxy). Created by the role if absent.",
                    ],
                    default="model-explorer",
                ),
                "model_explorer_host_port": OptionSpec(
                    type="int",
                    required=False,
                    description=[
                        "Host port mapped to the container's 80. A reverse proxy in front of it",
                        "is the caller's to configure.",
                    ],
                    default=8092,
                ),
                "model_explorer_timezone": OptionSpec(
                    type="str",
                    required=False,
                    default="Etc/UTC",
                    description=["Container timezone."],
                ),
            },
        )
    }
)
