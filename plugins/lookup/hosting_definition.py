# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright: (c) 2026, Richard Lynch
# GNU General Public License v3.0+ (see https://www.gnu.org/licenses/gpl-3.0.txt)
"""Lookup: read an app definition passed as an extra-vars FILE as DATA, never as a template.

Why this exists: ansible-core treats an extra-vars file (`-e @file`) as TRUSTED and templates its
values on use, so a definition written by someone other than the operator could run
`{{ lookup('pipe', ...) }}` on the controller before any gate saw it.

So the file is still passed with `-e @file`, but its single top-level key is `hosting_app_def`, a
name nothing in the play ever references: Ansible templates lazily, so an unreferenced variable is
never evaluated. This lookup finds the file in the CLI's own extra-vars list, reads the RAW text,
parses it with yaml.safe_load, refuses any string (key or value, at any depth) containing a Jinja
delimiter, and returns the inner mapping. The returned strings are untrusted data, so they are not
templated again either.

Refusals: more or fewer than one @file; a path other than <prefix>/<name>/vars.yml; a command-line
key=value other than the ones the deploy and remove plays take; a file with any top-level key but
hosting_app_def; a YAML tag (safe_load refuses them); Jinja delimiters anywhere.

Tests: `lookup('hosting_definition', path=..., extra_vars=[...], root=..., prefix=...)` takes the
same inputs explicitly, so a test exercises the real code rather than a copy of it.
"""

from __future__ import annotations

DOCUMENTATION = """
  name: hosting_definition
  short_description: Read a hosting app definition from the CLI's extra-vars file as inert data
  author: Richard Lynch (@Richard-Lynch)
  description:
    - Finds the one C(@file) in the command line's extra-vars, reads it as raw text and parses it
      with a safe YAML loader, refusing any Jinja delimiter anywhere in it, so a definition written
      by someone else is never templated on the controller.
    - The file must be C(<prefix>/<name>/vars.yml) with exactly one top-level key,
      C(hosting_app_def); the inner mapping is returned.
  options:
    path:
      description: Read this file instead of finding it in the CLI arguments (tests).
      type: str
    extra_vars:
      description: The extra-vars list to inspect instead of the CLI's own (tests).
      type: list
      elements: str
    root:
      description: Repository root that app definition paths are relative to (default the working directory).
      type: str
    prefix:
      description:
        - Directory, relative to O(root), that holds one C(<name>/vars.yml) per app. Required.
        - Any other path is refused.
      type: str
      required: true
"""

EXAMPLES = """
- name: Read the definition passed as -e @apps/<name>/vars.yml
  ansible.builtin.set_fact:
    hosting_app: "{{ lookup('richard_lynch.services.hosting_definition', root=playbook_dir, prefix='apps') }}"
"""

RETURN = """
  _raw:
    description: The definition's inner mapping (one element).
    type: list
    elements: dict
"""

import os  # noqa: E402
import re  # noqa: E402

import yaml  # noqa: E402

from ansible.errors import AnsibleLookupError  # noqa: E402
from ansible.plugins.lookup import LookupBase  # noqa: E402

_JINJA = ("{{", "{%", "{#", "}}", "%}", "#}")
_NAME = r"[a-z][a-z0-9-]{1,28}[a-z0-9]"
# The only key=value pairs the deploy and remove plays accept on the command line: the apply and
# confirm gates, and `target`. `target` is accepted HERE so that service_hosting's assert_target
# entrypoint can read it and REFUSE a target naming any host but the hosting guest, with the reason
# printed, instead of this lookup failing on an unexpected key.
_ALLOWED_KV = {"hosting_deploy_apply", "hosting_remove_confirm", "target"}
TOP_KEY = "hosting_app_def"


def _strings(node, where=""):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _strings(k, where + "/<key>")
            yield from _strings(v, where + "/" + str(k))
    elif isinstance(node, (list, tuple)):
        for i, v in enumerate(node):
            yield from _strings(v, where + "[%d]" % i)
    elif isinstance(node, str):
        yield where, node


def check_text(text, label="definition"):
    """Parse RAW text as data and refuse Jinja anywhere in it. Returns the inner mapping."""
    for delim in _JINJA:
        if delim in text:
            raise ValueError(
                "%s contains the Jinja delimiter %r in its raw text; app definitions are data, "
                "never templates" % (label, delim)
            )
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(
            "%s is not plain YAML (tags and anchors to objects are refused): %s" % (label, exc)
        )
    if not isinstance(doc, dict) or set(doc) != {TOP_KEY}:
        raise ValueError(
            "%s must have exactly one top-level key, %s (found %s)"
            % (label, TOP_KEY, sorted(doc) if isinstance(doc, dict) else type(doc).__name__)
        )
    # The raw-text check above misses a delimiter spelled with a YAML escape in a double-quoted
    # scalar ("\x7b\x7b"), which only exists after parsing: so check the parsed strings too.
    for where, s in _strings(doc[TOP_KEY]):
        for delim in _JINJA:
            if delim in s:
                raise ValueError(
                    "%s: the value at %s contains the Jinja delimiter %r once parsed"
                    % (label, where, delim)
                )
    inner = doc[TOP_KEY]
    if not isinstance(inner, dict):
        raise ValueError("%s: %s must be a mapping" % (label, TOP_KEY))
    return inner


def path_re(prefix):
    """The one path shape a definition may have: <prefix>/<name>/vars.yml, relative."""
    prefix = (prefix or "").strip("/")
    if not prefix or os.path.isabs(prefix) or ".." in prefix.split("/"):
        raise ValueError("prefix must be a relative directory inside root (got %r)" % prefix)
    return re.compile(r"^%s/%s/vars\.yml$" % (re.escape(prefix), _NAME)), prefix


def pick_file(extra_vars, prefix="<prefix>"):
    """The one @file among the CLI extra-vars; refuse everything else that could carry config."""
    files = []
    for entry in extra_vars or ():
        for token in str(entry).split():
            if token.startswith("@"):
                files.append(token[1:])
            elif "=" in token and not token.startswith(("{", "[")):
                key = token.split("=", 1)[0]
                if key not in _ALLOWED_KV:
                    raise ValueError(
                        "command-line variable %r is refused; the deploy play takes only %s and one "
                        "@%s/<name>/vars.yml" % (key, sorted(_ALLOWED_KV), prefix)
                    )
            else:
                raise ValueError(
                    "extra-vars entry %r is refused (only @file and key=value)" % token
                )
    if len(files) != 1:
        raise ValueError(
            "exactly one @%s/<name>/vars.yml extra-vars file is required (found %d)"
            % (prefix, len(files))
        )
    return files[0]


class LookupModule(LookupBase):
    def run(self, terms, variables=None, **kwargs):
        root = kwargs.get("root") or os.getcwd()
        path = kwargs.get("path")
        try:
            pattern, prefix = path_re(kwargs.get("prefix"))
            if path is None:
                extra = kwargs.get("extra_vars")
                if extra is None:
                    from ansible import context

                    extra = context.CLIARGS.get("extra_vars") or ()
                path = pick_file(extra, prefix)
            rel = os.path.normpath(path)
            if os.path.isabs(rel):
                rel = os.path.relpath(rel, root)
            if not pattern.match(rel):
                raise ValueError(
                    "%r is not %s/<name>/vars.yml; the deploy play reads nothing else"
                    % (path, prefix)
                )
            full = os.path.join(root, rel)
            if os.path.realpath(full) != os.path.join(os.path.realpath(root), rel):
                raise ValueError("%s is a symlink or resolves outside the repository" % rel)
            with open(full, encoding="utf-8") as fh:
                text = fh.read()
            return [check_text(text, rel)]
        except (ValueError, OSError) as exc:
            raise AnsibleLookupError("hosting_definition refused: %s" % exc)
