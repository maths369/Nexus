"""Browser worker facade backed by a subprocess and JSON-lines IPC."""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class BrowserServiceUnavailableError(RuntimeError):
    """Raised when browser automation is requested before the worker is configured."""


@dataclass
class BrowserWorkerConfig:
    enabled: bool = False
    command: list[str] = field(default_factory=list)
    workdir: Path | None = None
    startup_timeout_seconds: float = 10.0
    request_timeout_seconds: float = 30.0
    environment: dict[str, str] = field(default_factory=dict)


class BrowserService:
    """Playwright-oriented browser automation facade."""

    def __init__(self, config: BrowserWorkerConfig | None = None):
        self._config = config or BrowserWorkerConfig()
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self._config.enabled and bool(self._config.command)

    async def aclose(self) -> None:
        async with self._lock:
            if self._proc is None:
                return
            proc = self._proc
            self._proc = None
        try:
            await self._safe_request({"op": "close"}, timeout=5.0, allow_missing=True, process=proc)
        except Exception:
            pass
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()

    async def health(self) -> dict[str, Any]:
        self._ensure_enabled()
        return await self._request("health", {})

    async def navigate(self, url: str) -> dict[str, Any]:
        self._ensure_enabled()
        return await self._request("navigate", {"url": url})

    async def screenshot(self, path: str | Path | None = None) -> dict[str, Any]:
        self._ensure_enabled()
        payload: dict[str, Any] = {}
        if path is not None:
            payload["path"] = str(path)
        return await self._request("screenshot", payload)

    async def extract_text(self, selector: str | None = None) -> dict[str, Any]:
        self._ensure_enabled()
        payload: dict[str, Any] = {}
        if selector:
            payload["selector"] = selector
        return await self._request("extract_text", payload)

    async def fill_form(self, fields: dict[str, Any]) -> dict[str, Any]:
        self._ensure_enabled()
        return await self._request("fill_form", {"fields": fields})

    async def _request(self, op: str, params: dict[str, Any]) -> dict[str, Any]:
        proc = await self._ensure_started()
        response = await self._safe_request(
            {"id": uuid.uuid4().hex, "op": op, "params": params},
            timeout=self._config.request_timeout_seconds,
            process=proc,
        )
        if not response.get("ok", False):
            raise RuntimeError(str(response.get("error") or f"Browser worker failed: {op}"))
        return dict(response.get("result") or {})

    async def _ensure_started(self) -> asyncio.subprocess.Process:
        self._ensure_enabled()
        async with self._lock:
            if self._proc is not None and self._proc.returncode is None:
                return self._proc

            self._proc = await asyncio.create_subprocess_exec(
                *self._config.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._config.workdir) if self._config.workdir else None,
                env={**self._base_env(), **self._config.environment},
            )
            try:
                await self._safe_request(
                    {"id": uuid.uuid4().hex, "op": "health", "params": {}},
                    timeout=self._config.startup_timeout_seconds,
                    process=self._proc,
                )
            except Exception:
                await self.aclose()
                raise
            return self._proc

    async def _safe_request(
        self,
        payload: dict[str, Any],
        *,
        timeout: float,
        process: asyncio.subprocess.Process,
        allow_missing: bool = False,
    ) -> dict[str, Any]:
        if process.stdin is None or process.stdout is None:
            raise BrowserServiceUnavailableError("Browser worker stdio is unavailable")

        async with self._request_lock:
            try:
                process.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
                await asyncio.wait_for(process.stdin.drain(), timeout=timeout)
                raw = await asyncio.wait_for(process.stdout.readline(), timeout=timeout)
            except asyncio.TimeoutError as exc:
                raise TimeoutError("Browser worker request timed out") from exc

        if not raw:
            if allow_missing:
                return {}
            stderr = await self._drain_stderr(process)
            raise RuntimeError(f"Browser worker exited unexpectedly: {stderr or 'no stderr'}")

        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid browser worker response: {raw!r}") from exc

    def _ensure_enabled(self) -> None:
        if not self.enabled:
            raise BrowserServiceUnavailableError("Browser worker is not configured")

    @staticmethod
    def _base_env() -> dict[str, str]:
        return {
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8",
            "NEXUS_BROWSER_HEADLESS": "1",
        }

    @staticmethod
    async def _drain_stderr(process: asyncio.subprocess.Process) -> str:
        if process.stderr is None:
            return ""
        chunks: list[bytes] = []
        while True:
            try:
                chunk = await asyncio.wait_for(process.stderr.readline(), timeout=0.05)
            except asyncio.TimeoutError:
                break
            if not chunk:
                break
            chunks.append(chunk)
            if len(chunks) >= 10:
                break
        return b"".join(chunks).decode("utf-8", errors="ignore").strip()


def default_browser_worker_command() -> list[str]:
    return [sys.executable, "-m", "nexus.services.browser.worker"]
