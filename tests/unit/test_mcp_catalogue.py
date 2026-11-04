# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the reviewed MCP catalogue (plugins/filter/mcp_catalogue.py).

Every refusal has a near-miss that must pass: a validator that refuses everything passes every
refusal test.

Run: python -m pytest tests/unit -q (from the collection root)
"""

from __future__ import annotations

import copy
import os
import sys

import pytest
from ansible.errors import AnsibleFilterError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "plugins", "filter"))

from mcp_catalogue import mcp_catalogue_document  # noqa: E402

GOOD = [
    {
        "name": "context7",
        "transport": "http",
        "url": "https://mcp.context7.com/mcp",
        "tools_include": ["resolve-library-id", "query-docs"],
        "clients": ["coder", "default"],
        "destinations": ["mcp.context7.com"],
        "reviewed_by": "alice",
    },
    {
        "name": "github-ro",
        "transport": "http",
        "url": "https://api.githubcopilot.com/mcp/",
        "headers": {"Authorization": "Bearer ${MCP_GITHUB_RO_API_KEY}"},
        "env_names": ["MCP_GITHUB_RO_API_KEY"],
        "tools_include": ["get_file_contents"],
        "clients": ["coder"],
        "reviewed_by": "alice",
    },
    {
        "name": "fs-ro",
        "transport": "stdio",
        "command": "mcp-fs",
        "args": ["--read-only"],
        "env": {"ROOT_TOKEN": "${FS_TOKEN}"},
        "env_names": ["FS_TOKEN"],
        "tools_include": ["read"],
        "clients": ["coder"],
        "reviewed_by": "alice",
    },
]


def bad(mutate, clients=None):
    servers = copy.deepcopy(GOOD)
    mutate(servers)
    with pytest.raises(AnsibleFilterError):
        mcp_catalogue_document(servers, clients)


def test_good_document_shape():
    doc = mcp_catalogue_document(
        copy.deepcopy(GOOD), {"coder": ["context7", "github-ro", "context7"]}
    )
    assert doc["enabled"] == {"coder": ["context7", "github-ro"]}
    assert doc["servers"]["context7"]["config"] == {
        "url": "https://mcp.context7.com/mcp",
        "tools": {"include": ["resolve-library-id", "query-docs"]},
    }
    assert doc["servers"]["github-ro"]["config"]["headers"] == {
        "Authorization": "Bearer ${MCP_GITHUB_RO_API_KEY}"
    }
    assert doc["servers"]["fs-ro"]["config"]["command"] == "mcp-fs"


@pytest.mark.parametrize("reviewer", [None, "", "  ", "pending", "PENDING"])
def test_unreviewed_refused(reviewer):
    bad(lambda s: s[0].__setitem__("reviewed_by", reviewer))


def test_literal_credential_in_header_refused():
    bad(lambda s: s[1]["headers"].__setitem__("Authorization", "Bearer ghp_abc123"))


def test_unlisted_reference_refused():
    bad(lambda s: s[1].__setitem__("env_names", []))


def test_credential_in_url_refused():
    bad(lambda s: s[0].__setitem__("url", "https://x.example/mcp?token=abc"))


def test_literal_stdio_env_refused():
    bad(lambda s: s[2]["env"].__setitem__("ROOT_TOKEN", "hunter2"))


def test_missing_include_refused():
    bad(lambda s: s[0].pop("tools_include"))
    bad(lambda s: s[0].__setitem__("tools_include", []))


def test_transport_shape_refused():
    bad(lambda s: s[0].__setitem__("transport", "sse"))
    bad(lambda s: s[0].pop("url"))
    bad(lambda s: s[2].pop("command"))


def test_names_refused():
    bad(lambda s: s[0].__setitem__("name", "Context 7"))
    bad(lambda s: s.append(copy.deepcopy(s[0])))


def test_enablement_must_match_catalogue_and_clients():
    with pytest.raises(AnsibleFilterError):
        mcp_catalogue_document(copy.deepcopy(GOOD), {"coder": ["nope"]})
    with pytest.raises(AnsibleFilterError):
        mcp_catalogue_document(copy.deepcopy(GOOD), {"settings": ["context7"]})


def test_near_misses_pass():
    s = copy.deepcopy(GOOD)
    s[1]["headers"]["X-Api-Key"] = "${MCP_GITHUB_RO_API_KEY}"  # a bare reference is fine
    s[0]["reviewed_by"] = "pending review by alice"  # a sentence containing 'pending' is a name
    doc = mcp_catalogue_document(s, {})
    assert doc["enabled"] == {}
