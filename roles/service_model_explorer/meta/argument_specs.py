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
            short_description="Run the model-discovery app's container image, pinned by digest",
            description=[
                "Deploys the model-explorer image (nginx serving the built app; source and",
                "releases at github.com/Richard-Lynch/model-explorer) with Docker Compose, by",
                "DIGEST, and mounts the caller's catalogue snapshot read-only beside it.",
                "Nothing is built here and no node toolchain is needed anywhere.",
                "The app is READ-ONLY and holds NO credential. It reads one JSON snapshot",
                "of the gateway's models; adding a model to Open WebUI's picker is staged",
                "in the UI and applied by an Ansible play, never by the browser.",
            ],
            version_added="1.0",
            author=DEFAULT_AUTHOR,
            options={
                "model_explorer_image": OptionSpec(
                    type="str",
                    required=True,
                    description=[
                        "The image to run, as name@sha256:<64 hex>: a DIGEST, never a tag, so",
                        "what runs is exactly the image a release recorded. Each model-explorer",
                        "release names its digest in its notes and in image-digest.txt. There is",
                        "no default: which release a host runs is the caller's decision.",
                    ],
                ),
                "model_explorer_catalogue_src": OptionSpec(
                    type="str",
                    required=True,
                    description=[
                        "Path ON THE CONTROLLER of the catalogue snapshot (catalogue.json) the",
                        "app serves. It is the caller's data — produced by reading the",
                        "gateway's GET /v1/models and OpenRouter's public catalogue — and is",
                        "copied to the host on its own, so refreshing it needs no new image.",
                    ],
                ),
                "model_explorer_container_name": OptionSpec(
                    type="str",
                    required=False,
                    default="model-explorer",
                    description=["Container name."],
                ),
                "model_explorer_config_dir": OptionSpec(
                    type="str",
                    required=False,
                    description=[
                        "Host directory holding the compose project and the catalogue directory."
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
                        "Host port mapped to the container's 8080 (the image runs nginx as a",
                        "non-root user). A reverse proxy in front of it is the caller's to",
                        "configure.",
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
