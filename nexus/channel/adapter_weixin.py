"""
Weixin adapter backed by the official Tencent Weixin OpenClaw plugin package.

Nexus keeps the HTTP/API surface and orchestration flow, while a separate Node
host process loads `@tencent-weixin/openclaw-weixin` and exposes a small local
HTTP bridge. This module is only the Python-side client + long-poll runner.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

import httpx

logger = logging.getLogger(__name__)

DEFAULT_PLUGIN_HOST_BASE_URL = "http://127.0.0.1:18101"
DEFAULT_LONG_POLL_TIMEOUT_MS = 35_000
DEFAULT_LOGIN_TIMEOUT_MS = 480_000
SESSION_EXPIRED_ERRCODE = -14


class _HttpClientProtocol(Protocol):
    async def post(self, url: str, **kwargs: Any) -> Any: ...
    async def get(self, url: str, **kwargs: Any) -> Any: ...
    async def aclose(self) -> None: ...


@dataclass(slots=True)
class WeixinAccount:
    account_id: str
    token: str = ""
    base_url: str = ""
    user_id: str = ""
    enabled: bool = True

    @property
    def configured(self) -> bool:
        return bool(self.token.strip())


class WeixinAdapter:
    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        client: _HttpClientProtocol | None = None,
    ) -> None:
        cfg = config or {}
        self._enabled = bool(cfg.get("enabled", False))
        self._state_dir = Path(str(cfg.get("state_dir") or "./data/weixin")).expanduser().resolve()
        self._plugin_state_dir = Path(
            str(cfg.get("plugin_state_dir") or (self._state_dir / "plugin-host"))
        ).expanduser().resolve()
        self._plugin_host_base_url = str(
            cfg.get("plugin_host_base_url") or DEFAULT_PLUGIN_HOST_BASE_URL
        ).rstrip("/")
        self._default_account_id = str(cfg.get("default_account_id") or "default").strip() or "default"
        self._client = client or httpx.AsyncClient(timeout=60.0, trust_env=True)
        self._context_dir = self._state_dir / "accounts"
        self._context_dir.mkdir(parents=True, exist_ok=True)
        self._plugin_accounts_dir = self._plugin_state_dir / "openclaw-weixin" / "accounts"

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def configured(self) -> bool:
        return self._enabled

    @property
    def default_account_id(self) -> str:
        return self._default_account_id

    @property
    def state_dir(self) -> Path:
        return self._state_dir

    @property
    def plugin_state_dir(self) -> Path:
        return self._plugin_state_dir

    @property
    def plugin_host_base_url(self) -> str:
        return self._plugin_host_base_url

    def _context_file(self, account_id: str) -> Path:
        return self._context_dir / f"{account_id}.context.json"

    def list_account_ids(self) -> list[str]:
        if not self._plugin_accounts_dir.exists():
            return []
        account_ids: list[str] = []
        for file_path in sorted(self._plugin_accounts_dir.glob("*.json")):
            if file_path.name.endswith(".sync.json") or file_path.name.endswith(".context-tokens.json"):
                continue
            account_ids.append(file_path.stem)
        return account_ids

    def load_account(self, account_id: str) -> WeixinAccount | None:
        file_path = self._plugin_accounts_dir / f"{account_id}.json"
        if not file_path.exists():
            return None
        try:
            payload = json.loads(file_path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("Failed to read Weixin plugin account file: %s", file_path, exc_info=True)
            return None
        return WeixinAccount(
            account_id=account_id,
            token=str(payload.get("token") or ""),
            base_url=str(payload.get("baseUrl") or payload.get("base_url") or ""),
            user_id=str(payload.get("userId") or payload.get("user_id") or ""),
            enabled=True,
        )

    def _load_context_tokens(self, account_id: str) -> dict[str, str]:
        file_path = self._context_file(account_id)
        if not file_path.exists():
            return {}
        try:
            payload = json.loads(file_path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("Failed to read Weixin context token file: %s", file_path, exc_info=True)
            return {}
        if not isinstance(payload, dict):
            return {}
        return {
            str(user_id): str(token)
            for user_id, token in payload.items()
            if str(user_id).strip() and str(token).strip()
        }

    def get_context_token(self, account_id: str, peer_id: str) -> str | None:
        token = self._load_context_tokens(account_id).get(peer_id)
        return token if token and token.strip() else None

    def set_context_token(self, account_id: str, peer_id: str, context_token: str) -> None:
        if not peer_id.strip() or not context_token.strip():
            return
        payload = self._load_context_tokens(account_id)
        payload[peer_id] = context_token
        self._context_file(account_id).write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _post_json(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        response = await self._client.post(
            f"{self._plugin_host_base_url}{path}",
            json=payload or {},
        )
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict) and int(data.get("code") or 0) != 0:
            raise RuntimeError(str(data.get("msg") or f"Plugin host request failed: {path}"))
        return data

    async def start_login(
        self,
        *,
        account_id: str | None = None,
        bot_type: str | None = None,
        base_url: str | None = None,
    ) -> dict[str, Any]:
        response = await self._post_json(
            "/login/start",
            {
                "account_id": account_id or self._default_account_id,
                "bot_type": bot_type,
                "base_url": base_url,
            },
        )
        payload = dict(response.get("data") or {})
        if "sessionKey" in payload and "session_key" not in payload:
            payload["session_key"] = payload["sessionKey"]
        if "qrcodeUrl" in payload and "qrcode_url" not in payload:
            payload["qrcode_url"] = payload["qrcodeUrl"]
        if "accountId" in payload and "account_id" not in payload:
            payload["account_id"] = payload["accountId"]
        return payload

    async def wait_for_login(
        self,
        session_key: str,
        *,
        timeout_ms: int = DEFAULT_LOGIN_TIMEOUT_MS,
    ) -> dict[str, Any]:
        response = await self._post_json(
            "/login/wait",
            {
                "session_key": session_key,
                "timeout_ms": timeout_ms,
            },
        )
        payload = dict(response.get("data") or {})
        field_aliases = {
            "accountId": "account_id",
            "baseUrl": "base_url",
            "userId": "user_id",
            "botToken": "bot_token",
            "sessionKey": "session_key",
        }
        for source, target in field_aliases.items():
            if source in payload and target not in payload:
                payload[target] = payload[source]
        return payload

    async def get_updates(
        self,
        account_id: str,
        *,
        get_updates_buf: str = "",
        timeout_ms: int = DEFAULT_LONG_POLL_TIMEOUT_MS,
    ) -> dict[str, Any]:
        del get_updates_buf
        response = await self._post_json(
            "/updates/poll",
            {
                "account_id": account_id,
                "timeout_ms": timeout_ms,
            },
        )
        return dict(response.get("data") or {})

    def parse_update_message(self, account_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(payload, dict):
            return None
        if "account_id" in payload and "text" in payload:
            sender_user_id = str(payload.get("sender_user_id") or "").strip()
            context_token = str(payload.get("context_token") or "").strip()
            if sender_user_id and context_token:
                self.set_context_token(account_id, sender_user_id, context_token)
            return dict(payload)
        return None

    async def send_text(
        self,
        account_id: str,
        to_user_id: str,
        text: str,
        *,
        context_token: str | None = None,
    ) -> dict[str, Any]:
        response = await self._post_json(
            "/messages/send_text",
            {
                "account_id": account_id,
                "to_user_id": to_user_id,
                "text": text,
                "context_token": context_token or self.get_context_token(account_id, to_user_id),
            },
        )
        return dict(response.get("data") or {})

    def status_snapshot(self) -> dict[str, Any]:
        accounts = []
        for account_id in self.list_account_ids():
            account = self.load_account(account_id)
            if account is None:
                continue
            accounts.append(
                {
                    "account_id": account.account_id,
                    "configured": account.configured,
                    "enabled": account.enabled,
                    "base_url": account.base_url,
                    "user_id": account.user_id,
                }
            )
        return {
            "enabled": self._enabled,
            "state_dir": str(self._state_dir),
            "plugin_state_dir": str(self._plugin_state_dir),
            "plugin_host_base_url": self._plugin_host_base_url,
            "accounts": accounts,
        }


class WeixinLongPollRunner:
    def __init__(
        self,
        adapter: WeixinAdapter,
        *,
        on_message: Callable[[dict[str, Any]], Awaitable[None]],
        long_poll_timeout_ms: int = DEFAULT_LONG_POLL_TIMEOUT_MS,
        retry_delay_seconds: float = 2.0,
        backoff_delay_seconds: float = 30.0,
        max_consecutive_failures: int = 3,
        session_expired_pause_seconds: float = 600.0,
    ) -> None:
        self._adapter = adapter
        self._on_message = on_message
        self._long_poll_timeout_ms = max(5000, int(long_poll_timeout_ms or DEFAULT_LONG_POLL_TIMEOUT_MS))
        self._retry_delay_seconds = max(0.2, float(retry_delay_seconds or 2.0))
        self._backoff_delay_seconds = max(self._retry_delay_seconds, float(backoff_delay_seconds or 30.0))
        self._max_consecutive_failures = max(1, int(max_consecutive_failures or 3))
        self._session_expired_pause_seconds = max(10.0, float(session_expired_pause_seconds or 600.0))
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._stopping = False

    def start(self, *, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._stopping = False
        for account_id in self._adapter.list_account_ids():
            self.ensure_account(account_id)

    def ensure_account(self, account_id: str) -> None:
        if self._loop is None or self._stopping or not account_id.strip():
            return
        task = self._tasks.get(account_id)
        if task is not None and not task.done():
            return
        self._tasks[account_id] = self._loop.create_task(self._run_account(account_id))

    def shutdown(self) -> None:
        self._stopping = True
        for task in self._tasks.values():
            task.cancel()

    def status(self) -> dict[str, Any]:
        return {
            "running": not self._stopping,
            "accounts": {
                account_id: {
                    "running": not task.done(),
                    "cancelled": task.cancelled(),
                }
                for account_id, task in self._tasks.items()
            },
        }

    async def _run_account(self, account_id: str) -> None:
        consecutive_failures = 0
        while not self._stopping:
            account = self._adapter.load_account(account_id)
            if account is None or not account.enabled or not account.configured:
                return
            try:
                payload = await self._adapter.get_updates(
                    account_id,
                    timeout_ms=self._long_poll_timeout_ms,
                )
                if int(payload.get("errcode") or payload.get("ret") or 0) == SESSION_EXPIRED_ERRCODE:
                    logger.warning(
                        "Weixin session expired for account=%s, pausing for %.1fs",
                        account_id,
                        self._session_expired_pause_seconds,
                    )
                    await asyncio.sleep(self._session_expired_pause_seconds)
                    continue
                if int(payload.get("ret") or 0) != 0 or int(payload.get("errcode") or 0) != 0:
                    consecutive_failures += 1
                    logger.warning(
                        "Weixin updates poll failed account=%s ret=%s errcode=%s errmsg=%s",
                        account_id,
                        payload.get("ret"),
                        payload.get("errcode"),
                        payload.get("errmsg"),
                    )
                    await asyncio.sleep(
                        self._backoff_delay_seconds
                        if consecutive_failures >= self._max_consecutive_failures
                        else self._retry_delay_seconds
                    )
                    if consecutive_failures >= self._max_consecutive_failures:
                        consecutive_failures = 0
                    continue

                consecutive_failures = 0
                for raw_event in payload.get("events") or []:
                    event = self._adapter.parse_update_message(account_id, raw_event)
                    if not event or bool(event.get("ignored")):
                        continue
                    await self._on_message(event)
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                raise
            except Exception:
                consecutive_failures += 1
                logger.exception("Weixin long-poll loop failed for account=%s", account_id)
                await asyncio.sleep(
                    self._backoff_delay_seconds
                    if consecutive_failures >= self._max_consecutive_failures
                    else self._retry_delay_seconds
                )
                if consecutive_failures >= self._max_consecutive_failures:
                    consecutive_failures = 0
