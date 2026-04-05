from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from tempfile import mkdtemp
from typing import Any
from urllib.parse import urlencode, urljoin


DEFAULT_CLAWHUB_URL = "https://clawhub.ai"
DEFAULT_CLAWHUB_TIMEOUT_SECONDS = 20.0


class ClawHubRequestError(RuntimeError):
    def __init__(self, *, path: str, status: int, body: str) -> None:
        super().__init__(f"ClawHub {path} failed ({status}): {body}")
        self.path = path
        self.status = status
        self.body = body


@dataclass(frozen=True)
class ClawHubArchiveDownload:
    archive_path: Path
    integrity: str


def _read_non_empty_string(value: Any) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return None


def _extract_token_from_config(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    for key in ("accessToken", "authToken", "apiToken", "token"):
        token = _read_non_empty_string(value.get(key))
        if token:
            return token
    for key in ("auth", "session", "credentials", "user"):
        token = _extract_token_from_config(value.get(key))
        if token:
            return token
    return None


def _resolve_clawhub_config_paths(explicit_path: str | None = None) -> list[Path]:
    if explicit_path:
        return [Path(explicit_path).expanduser()]

    env_path = (
        os.environ.get("OPENCLAW_CLAWHUB_CONFIG_PATH", "").strip()
        or os.environ.get("CLAWHUB_CONFIG_PATH", "").strip()
        or os.environ.get("CLAWDHUB_CONFIG_PATH", "").strip()
    )
    if env_path:
        return [Path(env_path).expanduser()]

    xdg_config_home = os.environ.get("XDG_CONFIG_HOME", "").strip()
    config_home = Path(xdg_config_home) if xdg_config_home else Path.home() / ".config"
    xdg_path = config_home / "clawhub" / "config.json"
    if os.name == "posix" and sys_platform() == "darwin":
        return [
            Path.home() / "Library" / "Application Support" / "clawhub" / "config.json",
            xdg_path,
        ]
    return [xdg_path]


def sys_platform() -> str:
    import platform

    return platform.system().lower()


class ClawHubClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        token: str | None = None,
        token_env: str | None = None,
        timeout_seconds: float | None = None,
        config_path: str | None = None,
    ) -> None:
        self._base_url = self.resolve_base_url(base_url)
        self._token = _read_non_empty_string(token)
        self._token_env = _read_non_empty_string(token_env)
        self._timeout_seconds = float(timeout_seconds or DEFAULT_CLAWHUB_TIMEOUT_SECONDS)
        self._config_path = _read_non_empty_string(config_path)

    @classmethod
    def from_config(cls, config: dict[str, Any] | None) -> ClawHubClient:
        payload = config or {}
        return cls(
            base_url=_read_non_empty_string(payload.get("base_url")),
            token=_read_non_empty_string(payload.get("token")),
            token_env=_read_non_empty_string(payload.get("token_env")),
            timeout_seconds=float(payload.get("timeout_seconds") or DEFAULT_CLAWHUB_TIMEOUT_SECONDS),
            config_path=_read_non_empty_string(payload.get("config_path")),
        )

    @staticmethod
    def resolve_base_url(base_url: str | None = None) -> str:
        env_url = (
            os.environ.get("OPENCLAW_CLAWHUB_URL", "").strip()
            or os.environ.get("CLAWHUB_URL", "").strip()
            or DEFAULT_CLAWHUB_URL
        )
        return (base_url or env_url).rstrip("/") or DEFAULT_CLAWHUB_URL

    def _resolve_token(self) -> str | None:
        if self._token:
            return self._token

        env_candidates: list[str] = []
        if self._token_env:
            env_candidates.append(self._token_env)
        env_candidates.extend([
            "OPENCLAW_CLAWHUB_TOKEN",
            "CLAWHUB_TOKEN",
            "CLAWHUB_AUTH_TOKEN",
        ])
        for env_name in env_candidates:
            token = os.environ.get(env_name, "").strip()
            if token:
                return token

        for config_path in _resolve_clawhub_config_paths(self._config_path):
            try:
                payload = json.loads(config_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            token = _extract_token_from_config(payload)
            if token:
                return token
        return None

    async def _request_json(
        self,
        *,
        path: str,
        search: dict[str, str | None] | None = None,
        base_url: str | None = None,
    ) -> dict[str, Any]:
        import aiohttp

        query = urlencode(
            {
                key: value
                for key, value in (search or {}).items()
                if isinstance(value, str) and value.strip()
            }
        )
        request_path = path if not query else f"{path}?{query}"
        url = urljoin(f"{self.resolve_base_url(base_url)}/", request_path.lstrip("/"))
        headers: dict[str, str] = {}
        token = self._resolve_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"

        timeout = aiohttp.ClientTimeout(total=self._timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.get(url, headers=headers or None) as response:
                if response.status >= 400:
                    body = (await response.text()).strip()
                    raise ClawHubRequestError(
                        path=path,
                        status=response.status,
                        body=body or response.reason or f"HTTP {response.status}",
                    )
                payload = await response.json(content_type=None)
        if not isinstance(payload, dict):
            raise RuntimeError(f"Unexpected ClawHub payload for {request_path}")
        return payload

    async def search_skills(
        self,
        query: str,
        *,
        limit: int = 10,
        base_url: str | None = None,
    ) -> list[dict[str, Any]]:
        payload = await self._request_json(
            path="/api/v1/search",
            search={
                "q": (query or "").strip() or "*",
                "limit": str(max(1, min(int(limit or 10), 50))),
            },
            base_url=base_url,
        )
        results = payload.get("results")
        return results if isinstance(results, list) else []

    async def get_skill_detail(
        self,
        slug: str,
        *,
        base_url: str | None = None,
    ) -> dict[str, Any]:
        return await self._request_json(
            path=f"/api/v1/skills/{slug}",
            base_url=base_url,
        )

    async def download_skill_archive(
        self,
        slug: str,
        *,
        version: str | None = None,
        tag: str | None = None,
        base_url: str | None = None,
    ) -> ClawHubArchiveDownload:
        import aiohttp

        query = urlencode(
            {
                "slug": slug,
                **(
                    {"version": _read_non_empty_string(version)}
                    if _read_non_empty_string(version)
                    else {}
                ),
                **(
                    {"tag": _read_non_empty_string(tag)}
                    if not _read_non_empty_string(version) and _read_non_empty_string(tag)
                    else {}
                ),
            }
        )
        path = "/api/v1/download"
        request_path = path if not query else f"{path}?{query}"
        url = urljoin(f"{self.resolve_base_url(base_url)}/", request_path.lstrip("/"))
        headers: dict[str, str] = {}
        token = self._resolve_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        timeout = aiohttp.ClientTimeout(total=self._timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.get(url, headers=headers or None) as response:
                if response.status >= 400:
                    body = (await response.text()).strip()
                    raise ClawHubRequestError(
                        path=path,
                        status=response.status,
                        body=body or response.reason or f"HTTP {response.status}",
                    )
                payload = await response.read()
        archive_dir = Path(mkdtemp(prefix="nexus-clawhub-skill-"))
        archive_path = archive_dir / f"{slug}.zip"
        archive_path.write_bytes(payload)
        digest = hashlib.sha256(payload).digest()
        integrity = f"sha256-{base64.b64encode(digest).decode('ascii')}"
        return ClawHubArchiveDownload(archive_path=archive_path, integrity=integrity)
