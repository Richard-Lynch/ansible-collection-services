# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for namespace validation and scoped token minting."""

import contextlib
import io
import json
import socket
import unittest

import coder_token_broker as broker
import worker_sessiond as sessiond


class NamespaceTest(unittest.TestCase):
    def test_resolvers_agree(self):
        cfg = {"repo_owner": "Example", "repos_exclude": ["Private", "Example/Secret"]}
        cases = {
            "other-org/x": False,
            "Example-evil/x": False,
            "Examplex/x": False,
            "../x": False,
            ".hidden": False,
            "x/y/z": False,
            "Example/x/": False,
            "Example/x ": False,
            "Еxample/x": False,
            "Example/х": False,
            "x..y": False,
            "x.git": False,
            "private": False,
            "eXample/PRIVATE": False,
            "SECRET": False,
            "": False,
            None: False,
            "x": True,
            "Example/x": True,
            "EXAMPLE/x": True,
            "my.repo-name_2": True,
        }
        for value, allowed in cases.items():
            with self.subTest(value=value):
                for module in (broker, sessiond):
                    if allowed:
                        name, url = module.resolve_repo(cfg, value)
                        self.assertEqual(url, f"https://github.com/Example/{name}.git")
                    else:
                        with self.assertRaises(module.RefusedError) as error:
                            module.resolve_repo(cfg, value)
                        self.assertEqual(error.exception.status, 422)
                        self.assertIn("Example", str(error.exception))


class MintTest(unittest.TestCase):
    def setUp(self):
        self.cfg = {"repo_owner": "Example", "app_id": "42", "private_key_file": "/key"}
        self.calls = []
        self.response = {
            "token": "test-token",
            "expires_at": "2030-01-01T00:00:00Z",
            "permissions": {
                "contents": "write",
                "pull_requests": "write",
                "checks": "read",
                "statuses": "read",
                "metadata": "read",
            },
            "repositories": [{"name": "repo", "full_name": "Example/repo"}],
        }

    def http(self, method, url, headers, body=None):
        self.calls.append((method, url, body))
        return {"id": 1, "account": {"login": "Example"}} if method == "GET" else self.response

    def test_scoped_request(self):
        instance = broker.Broker(self.cfg, http=self.http, sign=lambda key, message: b"signature")
        self.assertEqual(instance.mint("repo")["repo"], "Example/repo")
        self.assertEqual(
            self.calls[1][2],
            {
                "repositories": ["repo"],
                "permissions": {
                    "contents": "write",
                    "pull_requests": "write",
                    "checks": "read",
                    "statuses": "read",
                },
            },
        )

    def test_canonical_repository_casing_and_scope(self):
        self.cfg["repo_owner"] = "owner"
        self.response["repositories"] = [{"name": "Repo", "full_name": "Owner/Repo"}]

        def http(method, url, headers, body=None):
            if method == "GET":
                return {
                    "id": 1,
                    "account": {"login": "Owner"},
                    "full_name": "Owner/Repo",
                    "repository": {"full_name": "Owner/Repo"},
                }
            return self.response

        instance = broker.Broker(self.cfg, http=http, sign=lambda key, message: b"signature")
        self.assertEqual(instance.mint("owner/repo")["repo"], "owner/repo")
        self.response["repositories"] = [{"name": "other", "full_name": "owner/other"}]
        with self.assertRaises(broker.RefusedError):
            instance.mint("owner/repo")

    def test_server_family_selection(self):
        self.assertEqual(broker.server_class("127.0.0.1").address_family, socket.AF_INET)
        for host in ("::", "fd00::1"):
            self.assertEqual(broker.server_class(host).address_family, socket.AF_INET6)

    def test_broader_config_is_refused(self):
        self.cfg["permissions"] = {
            "contents": "write",
            "pull_requests": "write",
            "checks": "read",
            "statuses": "read",
            "administration": "write",
        }
        instance = broker.Broker(self.cfg, http=self.http, sign=lambda key, message: b"signature")
        with self.assertRaises(broker.RefusedError):
            instance.mint("repo")
        self.assertEqual(self.calls, [])

    def test_upstream_identity_and_invalid_request_refused(self):
        instance = broker.Broker(self.cfg, http=self.http, sign=lambda key, message: b"signature")
        with self.assertRaises(broker.RefusedError):
            instance.mint("Example/repo/")
        self.assertEqual(self.calls, [])
        self.response["repositories"][0]["full_name"] = "Other/repo"
        with self.assertRaises(broker.RefusedError):
            instance.mint("repo")
        instance.http = lambda *args: {"id": 1, "account": {"login": "Other"}}
        with self.assertRaises(broker.RefusedError):
            instance.mint("repo")

    def test_extra_permission_or_repository_refused(self):
        instance = broker.Broker(self.cfg, http=self.http, sign=lambda key, message: b"signature")
        self.response["permissions"]["administration"] = "write"
        with self.assertRaises(broker.RefusedError) as error:
            instance.mint("repo")
        self.assertEqual(error.exception.status, 502)
        del self.response["permissions"]["administration"]
        self.response["permissions"]["workflows"] = "write"
        with self.assertRaises(broker.RefusedError):
            instance.mint("repo")
        del self.response["permissions"]["workflows"]
        self.response["repositories"].append({"name": "other"})
        with self.assertRaises(broker.RefusedError) as error:
            instance.mint("repo")
        self.assertEqual(error.exception.status, 502)


class HandlerTest(unittest.TestCase):
    def request(self, bearer, repo="repo"):
        cfg = {"repo_owner": "Example", "app_id": "42", "private_key_file": "/key"}

        def http(method, url, headers, body=None):
            if method == "GET":
                return {"id": 1, "account": {"login": "Example"}}
            return {
                "token": "secret-token-value",
                "expires_at": "2030-01-01T00:00:00Z",
                "permissions": {
                    "contents": "write",
                    "pull_requests": "write",
                    "checks": "read",
                    "statuses": "read",
                    "metadata": "read",
                },
                "repositories": [{"name": "repo", "full_name": "Example/repo"}],
            }

        handler = broker.make_handler(
            broker.Broker(cfg, http=http, sign=lambda key, message: b"sig"), "bearer-secret"
        )
        request = handler.__new__(handler)
        body = json.dumps({"card": "card-1", "repo": repo}).encode()
        request.path = "/v1/token"
        request.headers = {"Content-Length": str(len(body)), "Authorization": bearer}
        request.rfile = io.BytesIO(body)
        request.wfile = io.BytesIO()
        request.client_address = ("local", 0)
        response = []
        request.send_response = response.append
        request.send_header = lambda *args: None
        request.end_headers = lambda: None
        log = io.StringIO()
        with contextlib.redirect_stdout(log):
            request.do_POST()
        return response[0], json.loads(request.wfile.getvalue()), log.getvalue()

    def test_auth_scope_and_logs(self):
        for auth in ("", "Bearer wrong"):
            self.assertEqual(self.request(auth)[0], 401)
        status, _body, log = self.request("Bearer bearer-secret", "other/repo")
        self.assertEqual(status, 422)
        status, body, log = self.request("Bearer bearer-secret")
        self.assertEqual(status, 200)
        self.assertEqual(body["token"], "secret-token-value")
        self.assertNotIn("secret-token-value", log)
        self.assertNotIn("bearer-secret", log)
        self.assertEqual(len(log.splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
