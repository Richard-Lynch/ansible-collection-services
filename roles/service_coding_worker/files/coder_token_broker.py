#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Mint repository-scoped installation tokens for authorized workers."""

import base64
import datetime
import hmac
import json
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


class RefusedError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def resolve_repo(cfg: dict, repo: Any) -> tuple[str, str]:
    """Resolve a repository within the configured owner namespace."""
    # Deliberate installation choice: namespace plus an explicit exclude list.
    owner = cfg.get("repo_owner", "")
    message = f"repository is not permitted in owner namespace {owner}"
    if (
        not isinstance(repo, str)
        or not isinstance(owner, str)
        or not re.fullmatch(r"[A-Za-z0-9-]+", owner)
    ):
        raise RefusedError(message, 422)
    parts = repo.split("/")
    if len(parts) == 2:
        if not re.fullmatch(r"[A-Za-z0-9-]+", parts[0]) or parts[0].casefold() != owner.casefold():
            raise RefusedError(message, 422)
        name = parts[1]
    elif len(parts) == 1:
        name = parts[0]
    else:
        raise RefusedError(message, 422)
    if (
        not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", name)
        or ".." in name
        or name.startswith((".", "-"))
        or name.lower().endswith(".git")
    ):
        raise RefusedError(message, 422)
    for excluded in cfg.get("repos_exclude", []):
        if not isinstance(excluded, str):
            continue
        pieces = excluded.split("/")
        if len(pieces) == 1 or (len(pieces) == 2 and pieces[0].casefold() == owner.casefold()):
            if pieces[-1].casefold() == name.casefold():
                raise RefusedError(message, 422)
    return name, f"https://{cfg.get('github_host', 'github.com')}/{owner}/{name}.git"


def matches_full_name(value: Any, owner: str, name: str) -> bool:
    if not isinstance(value, str):
        return False
    parts = value.split("/")
    return (
        len(parts) == 2
        and parts[0].isascii()
        and parts[0].casefold() == owner.casefold()
        and parts[1].isascii()
        and parts[1].casefold() == name.casefold()
    )


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def openssl_sign(key: str, message: bytes) -> bytes:
    result = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", key],
        input=message,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return result.stdout


def github_http(method: str, url: str, headers: dict, body: Any = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


class Broker:
    def __init__(self, cfg: dict, http=github_http, sign=openssl_sign):
        self.cfg = cfg
        self.http = http
        self.sign = sign

    def mint(self, repo: Any) -> dict:
        name, _url = resolve_repo(self.cfg, repo)
        owner = self.cfg["repo_owner"]
        permissions = {
            "contents": "write",
            "pull_requests": "write",
            "checks": "read",
            "statuses": "read",
        }
        if self.cfg.get("permissions", permissions) != permissions:
            raise RefusedError("invalid broker permissions", 500)
        try:
            now = int(time.time())
            header = b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
            payload = b64(
                json.dumps({"iat": now - 60, "exp": now + 540, "iss": self.cfg["app_id"]}).encode()
            )
            message = f"{header}.{payload}".encode()
            jwt = message.decode() + "." + b64(self.sign(self.cfg["private_key_file"], message))
            headers = {
                "Authorization": "Bearer " + jwt,
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
            api = self.cfg.get("api_url", "https://api.github.com").rstrip("/")
            installation = self.http(
                "GET",
                f"{api}/repos/{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(name, safe='')}/installation",
                headers,
            )
            if (
                not installation["account"]["login"].isascii()
                or installation["account"]["login"].casefold() != owner.casefold()
            ):
                raise ValueError("installation owner")
            if (
                "full_name" in installation
                and not matches_full_name(installation["full_name"], owner, name)
            ) or (
                "repository" in installation
                and not matches_full_name(installation["repository"]["full_name"], owner, name)
            ):
                raise ValueError("installation repository")
            result = self.http(
                "POST",
                f"{api}/app/installations/{int(installation['id'])}/access_tokens",
                headers,
                {"repositories": [name], "permissions": permissions},
            )
            actual = result["permissions"]
            allowed = dict(permissions, metadata="read")
            if (
                not isinstance(actual, dict)
                or any(actual.get(k) != v for k, v in permissions.items())
                or any(allowed.get(k) != v for k, v in actual.items())
            ):
                raise ValueError("permissions")
            repositories = result["repositories"]
            if (
                not isinstance(repositories, list)
                or len(repositories) != 1
                or not isinstance(repositories[0].get("name"), str)
                or not repositories[0]["name"].isascii()
                or repositories[0]["name"].casefold() != name.casefold()
                or not matches_full_name(repositories[0].get("full_name"), owner, name)
            ):
                raise ValueError("repositories")
            if (
                not isinstance(result["token"], str)
                or not result["token"]
                or not isinstance(result["expires_at"], str)
            ):
                raise ValueError("token")
            return {
                "token": result["token"],
                "expires_at": result["expires_at"],
                "repo": f"{owner}/{name}",
                "permissions": actual,
            }
        except Exception:
            raise RefusedError("token broker upstream request failed", 502) from None


def make_handler(broker: Broker, bearer: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, status: int, body: dict, card: str = "", repo: str = ""):
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            print(
                json.dumps(
                    {
                        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        "remote_addr": self.client_address[0],
                        "card": card,
                        "repo": repo,
                        "outcome": "ok" if status < 400 else "refused",
                        "http_status": status,
                    }
                ),
                flush=True,
            )

        def do_GET(self):
            if self.path == "/healthz":
                self.respond(200, {"ok": True})
            else:
                self.respond(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/v1/token":
                self.respond(404, {"error": "not found"})
                return
            supplied = self.headers.get("Authorization", "")
            if not hmac.compare_digest(supplied.encode(), ("Bearer " + bearer).encode()):
                self.respond(401, {"error": "unauthorized"})
                return
            card = ""
            repo = ""
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 65536:
                    raise ValueError()
                data = json.loads(self.rfile.read(length))
                candidate = data.get("card")
                if not isinstance(candidate, str) or not re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", candidate
                ):
                    raise ValueError()
                card = candidate
                name, _url = resolve_repo(broker.cfg, data.get("repo"))
                repo = f"{broker.cfg['repo_owner']}/{name}"
                result = broker.mint(repo)
                self.respond(200, result, card, repo)
            except RefusedError as exc:
                self.respond(
                    exc.status,
                    {
                        "error": (
                            str(exc)
                            if exc.status == 422
                            else "token broker upstream request failed"
                        )
                    },
                    card,
                    repo,
                )
            except (ValueError, TypeError, AttributeError):
                self.respond(400, {"error": "invalid request"}, card, repo)

    return Handler


class IPv6ThreadingHTTPServer(ThreadingHTTPServer):
    address_family = socket.AF_INET6


def server_class(host: str):
    return IPv6ThreadingHTTPServer if ":" in host else ThreadingHTTPServer


def main():
    cfg = json.loads(Path(sys.argv[1]).read_text())
    bearer = Path(cfg["bearer_token_file"]).read_text().strip()
    if not bearer:
        raise SystemExit("bearer token is empty")
    server_class(cfg["listen_host"])(
        (cfg["listen_host"], int(cfg["listen_port"])), make_handler(Broker(cfg), bearer)
    ).serve_forever()


if __name__ == "__main__":
    main()
