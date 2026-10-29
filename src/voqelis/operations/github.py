"""Opt-in GitHub App client restricted to the private Operations repository."""

from __future__ import annotations

import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import jwt

from .export import issue_title

REPO = "0w1Max/voqelis-ops"
BASE = "https://api.github.com"
API_VERSION = "2026-03-10"


@dataclass(frozen=True, slots=True)
class GitHubAppConfig:
    app_id: int
    installation_id: int
    private_key_path: Path

    @classmethod
    def from_env(cls) -> GitHubAppConfig | None:
        enabled = os.getenv("OPERATIONS_GITHUB_EXPORT_ENABLED", "false").lower()
        if enabled not in {"true", "false"}:
            raise ValueError("OPERATIONS_GITHUB_EXPORT_ENABLED must be true or false")
        if enabled == "false":
            return None
        try:
            app_id = int(os.environ["OPERATIONS_GITHUB_APP_ID"])
            installation_id = int(os.environ["OPERATIONS_GITHUB_INSTALLATION_ID"])
            path = Path(os.environ["OPERATIONS_GITHUB_KEY_FILE"])
        except (KeyError, ValueError) as exc:
            raise ValueError("Incomplete GitHub App configuration") from exc
        if app_id <= 0 or installation_id <= 0 or not path.is_absolute():
            raise ValueError("Invalid GitHub App configuration")
        # Fail closed on configuration, without ever printing secret material.
        private_key_bytes(path)
        return cls(app_id=app_id, installation_id=installation_id, private_key_path=path)


def private_key_bytes(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        mode = os.fstat(fd)
        if not stat.S_ISREG(mode.st_mode) or stat.S_IMODE(mode.st_mode) != 0o600:
            raise ValueError("GitHub App private key must be a 0600 regular file")
        if mode.st_size <= 0 or mode.st_size > 16_384:
            raise ValueError("Invalid GitHub App key size")
        with os.fdopen(os.dup(fd), "rb") as stream:
            return stream.read(16_385)
    finally:
        os.close(fd)


class GitHubExportFailure(Exception):
    """Public error codes only: no network response, token or exception details."""

    def __init__(self, code: str, *, uncertain: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.uncertain = uncertain


class GitHubIssuesClient:
    def __init__(self, config: GitHubAppConfig, client: httpx.AsyncClient) -> None:
        self.config = config
        self.client = client

    async def _installation_token(self) -> str:
        try:
            now = int(time.time())
            signed = jwt.encode(
                {"iat": now - 60, "exp": now + 480, "iss": str(self.config.app_id)},
                private_key_bytes(self.config.private_key_path),
                algorithm="RS256",
            )
            response = await self.client.post(
                f"{BASE}/app/installations/{self.config.installation_id}/access_tokens",
                headers={
                    "Authorization": f"Bearer {signed}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": API_VERSION,
                },
                json={"repositories": ["voqelis-ops"], "permissions": {"issues": "write"}},
            )
            if response.status_code != 201:
                raise GitHubExportFailure("APP_AUTH_FAILED")
            payload = response.json()
            if (
                not isinstance(payload, dict)
                or not isinstance(payload.get("permissions"), dict)
                or payload["permissions"].get("issues") != "write"
                or not isinstance(payload.get("repositories"), list)
                or not any(
                    r.get("full_name") == REPO
                    for r in payload["repositories"]
                    if isinstance(r, dict)
                )
            ):
                raise GitHubExportFailure("APP_SCOPE_INVALID")
            token = payload.get("token")
            if not isinstance(token, str) or not token:
                raise GitHubExportFailure("APP_TOKEN_INVALID")
            return token
        except GitHubExportFailure:
            raise
        except (OSError, ValueError, jwt.PyJWTError, httpx.HTTPError) as exc:
            raise GitHubExportFailure("APP_AUTH_FAILED") from exc

    @staticmethod
    def _headers(token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
        }

    async def create_issue(self, marker: str, body: str) -> int:
        title = issue_title(marker)
        token = await self._installation_token()
        try:
            response = await self.client.post(
                f"{BASE}/repos/{REPO}/issues",
                headers=self._headers(token),
                json={"title": title, "body": body},
            )
        except httpx.HTTPError as exc:
            raise GitHubExportFailure("CREATE_OUTCOME_UNKNOWN", uncertain=True) from exc
        if response.status_code != 201:
            if response.status_code >= 500 or response.status_code in {408, 429}:
                raise GitHubExportFailure("CREATE_OUTCOME_UNKNOWN", uncertain=True)
            raise GitHubExportFailure("GITHUB_REJECTED")
        try:
            number = response.json()["number"]
            if type(number) is not int or number <= 0:
                raise ValueError("Invalid Issue response")
            return number
        except (ValueError, KeyError, TypeError) as exc:
            raise GitHubExportFailure("CREATE_OUTCOME_UNKNOWN", uncertain=True) from exc

    async def reconcile(self, marker: str) -> int | None:
        issue_title(marker)
        token = await self._installation_token()
        try:
            response = await self.client.get(
                f"{BASE}/search/issues",
                headers=self._headers(token),
                params={"q": f'repo:{REPO} in:body "{marker}"', "per_page": 100},
            )
            if response.status_code != 200:
                raise GitHubExportFailure("RECONCILE_UNAVAILABLE", uncertain=True)
            items = response.json()["items"]
            if not isinstance(items, list):
                raise ValueError("Invalid search response")
            exact = [
                item["number"] for item in items
                if isinstance(item, dict)
                and item.get("repository_url") == f"{BASE}/repos/{REPO}"
                and isinstance(item.get("body"), str)
                and f"Export marker: {marker}\n" in item["body"]
                and type(item.get("number")) is int
                and item["number"] > 0
            ]
            if len(exact) == 1:
                return exact[0]
            if len(exact) > 1:
                raise GitHubExportFailure("MULTIPLE_ISSUES", uncertain=True)
            return None
        except GitHubExportFailure:
            raise
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise GitHubExportFailure("RECONCILE_UNAVAILABLE", uncertain=True) from exc


def make_http_client() -> httpx.AsyncClient:
    # Fixed API host, no redirects, proxies or ambient credentials.
    return httpx.AsyncClient(
        timeout=httpx.Timeout(12.0, connect=4.0),
        follow_redirects=False,
        trust_env=False,
    )
