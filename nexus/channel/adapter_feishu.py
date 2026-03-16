"""
Feishu channel adapter with minimal production-safe outbound support.

Scope:
1. Internal-app tenant token refresh
2. Send text / interactive card messages
3. Parse webhook / long-connection message events
4. Keep the surface small; do not pull in legacy monolith behavior
"""

from __future__ import annotations

import asyncio
from concurrent.futures import TimeoutError as FutureTimeoutError
import hashlib
import hmac
import json
import logging
import re
import threading
import time
from typing import Any, Awaitable, Callable, Protocol

import httpx

logger = logging.getLogger(__name__)
_MENTION_RE = re.compile(r"@_user_\d+\s*")


class _HttpClientProtocol(Protocol):
    async def post(self, url: str, **kwargs: Any) -> Any: ...
    async def get(self, url: str, **kwargs: Any) -> Any: ...


class FeishuAdapter:
    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        client: _HttpClientProtocol | None = None,
    ):
        cfg = config or {}
        self._app_id = str(cfg.get("app_id", "") or "")
        self._app_secret = str(cfg.get("app_secret", "") or "")
        self._verification_token = str(cfg.get("verification_token", "") or "")
        self._encrypt_key = str(cfg.get("encrypt_key", "") or "")
        self._base_url = str(cfg.get("base_url", "https://open.feishu.cn/open-apis")).rstrip("/")
        self._subscription_mode = str(cfg.get("subscription_mode", "webhook") or "webhook")
        self._require_mention_in_group = bool(cfg.get("require_mention_in_group", False))
        self._bot_open_id = str(cfg.get("bot_open_id", "") or "")
        self._client = client or httpx.AsyncClient(timeout=20.0)
        self._access_token: str | None = None
        self._access_token_expires_at: float = 0.0

    @property
    def configured(self) -> bool:
        return bool(self._app_id and self._app_secret)

    def subscription_mode(self) -> str:
        raw = str(self._subscription_mode or "webhook").strip().lower()
        if raw in {"long_connection", "long-connection", "longconnection", "ws", "websocket"}:
            return "long_connection"
        return "webhook"

    def verify_callback(self, *, headers: dict[str, str], payload: dict[str, Any], raw_body: bytes) -> tuple[bool, str]:
        expected_token = self._verification_token.strip()
        if expected_token:
            token = str(
                payload.get("token")
                or ((payload.get("header") or {}).get("token") if isinstance(payload.get("header"), dict) else "")
                or ""
            ).strip()
            if token != expected_token:
                return False, "verification_token_mismatch"

        if self._encrypt_key and payload.get("challenge"):
            timestamp = headers.get("X-Lark-Request-Timestamp") or headers.get("x-lark-request-timestamp") or ""
            nonce = headers.get("X-Lark-Request-Nonce") or headers.get("x-lark-request-nonce") or ""
            signature = headers.get("X-Lark-Signature") or headers.get("x-lark-signature") or ""
            if timestamp and nonce and signature:
                body_text = raw_body.decode("utf-8", errors="replace")
                candidate = f"{timestamp}{nonce}{self._encrypt_key}{body_text}"
                expected = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
                if not hmac.compare_digest(expected, signature):
                    return False, "signature_mismatch"
        return True, "ok"

    @staticmethod
    def is_url_verification(payload: dict[str, Any]) -> bool:
        return str(payload.get("type") or "").strip() == "url_verification" or "challenge" in payload

    @staticmethod
    def extract_challenge(payload: dict[str, Any]) -> str:
        return str(payload.get("challenge") or "")

    def parse_message_event(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
        if str(header.get("event_type") or "").strip() != "im.message.receive_v1":
            return None
        event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
        sender_id = sender.get("sender_id") if isinstance(sender.get("sender_id"), dict) else {}

        message_id = str(message.get("message_id") or "").strip()
        chat_id = str(message.get("chat_id") or "").strip()
        message_type = str(message.get("message_type") or "").strip().lower()
        mentions = message.get("mentions") if isinstance(message.get("mentions"), list) else []
        chat_type = str(message.get("chat_type") or "").strip().lower() or "unknown"
        sender_type = str(sender.get("sender_type") or "").strip().lower() or "unknown"
        sender_user_id = str(
            sender_id.get("open_id")
            or sender_id.get("user_id")
            or sender_id.get("union_id")
            or ""
        ).strip()
        if not message_id or not chat_id:
            return None
        content_payload = self._extract_message_payload(message.get("content"))
        if message_type in {"image", "file", "audio", "media"}:
            attachments = self._extract_attachments(
                message_type=message_type,
                message_id=message_id,
                content=content_payload,
            )
            if not attachments:
                return {
                    "ignored": True,
                    "reason": "empty_attachment_message",
                    "message_id": message_id,
                    "chat_id": chat_id,
                }
            return {
                "ignored": False,
                "event_id": str(header.get("event_id") or "").strip(),
                "message_id": message_id,
                "chat_id": chat_id,
                "chat_type": chat_type,
                "sender_type": sender_type,
                "sender_user_id": sender_user_id,
                "text": "",
                "raw_text": "",
                "mentions": mentions,
                "create_time": str(message.get("create_time") or ""),
                "message_type": message_type,
                "attachments": attachments,
            }
        if message_type != "text":
            return {
                "ignored": True,
                "reason": f"unsupported_message_type:{message_type or 'unknown'}",
                "message_id": message_id,
                "chat_id": chat_id,
            }
        text = self._extract_message_text(content_payload)
        if not text:
            return {
                "ignored": True,
                "reason": "empty_text_message",
                "message_id": message_id,
                "chat_id": chat_id,
            }
        cleaned = _MENTION_RE.sub(" ", text)
        cleaned = " ".join(cleaned.split())
        if not cleaned:
            return {
                "ignored": True,
                "reason": "text_only_mentions",
                "message_id": message_id,
                "chat_id": chat_id,
            }
        require_mention = bool(self.__dict__.get("_require_mention_in_group", False))
        if require_mention and chat_type in {"group", "chat"}:
            bot_open_id = str(self.__dict__.get("_bot_open_id", "") or "").strip()
            if not self._is_bot_mentioned(mentions, bot_open_id):
                return {
                    "ignored": True,
                    "reason": "mention_required",
                    "message_id": message_id,
                    "chat_id": chat_id,
                }
        return {
            "ignored": False,
            "event_id": str(header.get("event_id") or "").strip(),
            "message_id": message_id,
            "chat_id": chat_id,
            "chat_type": chat_type,
            "sender_type": sender_type,
            "sender_user_id": sender_user_id,
            "text": cleaned.strip(),
            "raw_text": text,
            "mentions": mentions,
            "create_time": str(message.get("create_time") or ""),
            "message_type": "text",
            "attachments": [],
        }

    def parse_long_connection_message_event(self, data: Any) -> dict[str, Any] | None:
        payload = self._to_payload_dict(data)
        if not payload:
            return None
        parsed = self.parse_message_event(payload)
        if parsed:
            return parsed
        event = payload.get("event") if isinstance(payload.get("event"), dict) else None
        if not event and isinstance(payload.get("data"), dict):
            maybe_event = payload["data"].get("event")
            if isinstance(maybe_event, dict):
                event = maybe_event
        if not event:
            if isinstance(payload.get("message"), dict):
                event = {
                    "sender": payload.get("sender") if isinstance(payload.get("sender"), dict) else {},
                    "message": payload.get("message"),
                }
            elif isinstance(payload.get("data"), dict):
                data_obj = payload["data"]
                if isinstance(data_obj.get("message"), dict):
                    event = {
                        "sender": data_obj.get("sender") if isinstance(data_obj.get("sender"), dict) else {},
                        "message": data_obj.get("message"),
                    }
        if not event:
            return None
        normalized = {
            "header": {
                "event_type": "im.message.receive_v1",
                "event_id": str(
                    payload.get("event_id")
                    or ((payload.get("header") or {}).get("event_id") if isinstance(payload.get("header"), dict) else "")
                    or ""
                ).strip(),
            },
            "event": event,
        }
        return self.parse_message_event(normalized)

    @staticmethod
    def _to_payload_dict(data: Any) -> dict[str, Any]:
        if isinstance(data, dict):
            return dict(data)
        if data is None:
            return {}
        try:
            import lark_oapi as lark  # type: ignore

            serialized = lark.JSON.marshal(data)
            if isinstance(serialized, dict):
                return dict(serialized)
            if isinstance(serialized, str):
                decoded = json.loads(serialized)
                if isinstance(decoded, dict):
                    return decoded
        except Exception:
            pass
        try:
            maybe_dict = getattr(data, "__dict__", None)
            if isinstance(maybe_dict, dict):
                return dict(maybe_dict)
        except Exception:
            pass
        return {}

    @staticmethod
    def _extract_message_text(content: Any) -> str:
        if isinstance(content, dict):
            return str(content.get("text") or "").strip()
        if not isinstance(content, str):
            return str(content or "").strip()
        raw = content.strip()
        if not raw:
            return ""
        try:
            parsed = json.loads(raw)
        except Exception:
            return raw
        if isinstance(parsed, dict):
            return str(parsed.get("text") or "").strip()
        return raw

    @staticmethod
    def _extract_message_payload(content: Any) -> dict[str, Any]:
        if isinstance(content, dict):
            return dict(content)
        if not isinstance(content, str):
            return {}
        raw = content.strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except Exception:
            return {"text": raw}
        return parsed if isinstance(parsed, dict) else {"text": raw}

    @staticmethod
    def _extract_attachments(
        *,
        message_type: str,
        message_id: str,
        content: dict[str, Any],
    ) -> list[dict[str, Any]]:
        attachment: dict[str, Any] = {
            "attachment_type": message_type,
            "message_id": message_id,
        }
        if message_type == "image":
            image_key = str(content.get("image_key") or content.get("file_key") or "").strip()
            if not image_key:
                return []
            attachment.update(
                {
                    "image_key": image_key,
                    "file_name": str(content.get("file_name") or content.get("image_name") or f"{image_key}.png"),
                    "resource_type": "image",
                }
            )
            return [attachment]
        file_key = str(content.get("file_key") or content.get("media_id") or "").strip()
        if not file_key:
            return []
        attachment.update(
            {
                "file_key": file_key,
                "file_name": str(
                    content.get("file_name")
                    or content.get("name")
                    or f"{message_type}-{file_key}"
                ),
                "resource_type": "audio" if message_type == "audio" else ("media" if message_type == "media" else "file"),
            }
        )
        return [attachment]

    @staticmethod
    def _is_bot_mentioned(mentions: Any, bot_open_id: str) -> bool:
        if not isinstance(mentions, list) or not mentions:
            return False
        if not bot_open_id:
            return True
        for mention in mentions:
            if not isinstance(mention, dict):
                continue
            mention_id = mention.get("id") if isinstance(mention.get("id"), dict) else {}
            open_id = str(mention_id.get("open_id") or "").strip()
            if open_id and open_id == bot_open_id:
                return True
        return False

    async def aclose(self) -> None:
        close = getattr(self._client, "aclose", None)
        if callable(close):
            await close()

    async def send_text(self, chat_id: str, text: str) -> None:
        if not chat_id:
            raise ValueError("chat_id is required")
        token = await self.refresh_token()
        payload = {
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }
        response = await self._client.post(
            f"{self._base_url}/im/v1/messages",
            params={"receive_id_type": "chat_id"},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json=payload,
        )
        data = self._parse_response(response)
        if data.get("code", 0) != 0:
            raise RuntimeError(
                f"Feishu send_text failed code={data.get('code')} msg={data.get('msg')}"
            )

    async def download_attachment(
        self,
        *,
        message_id: str,
        attachment: dict[str, Any],
    ) -> dict[str, Any]:
        token = await self.refresh_token()
        attachment_type = str(attachment.get("attachment_type") or "file").strip().lower()
        headers = {"Authorization": f"Bearer {token}"}
        if attachment_type == "image":
            image_key = str(attachment.get("image_key") or "").strip()
            if not image_key:
                raise ValueError("image_key is required")
            response = await self._client.get(
                f"{self._base_url}/im/v1/images/{image_key}",
                headers=headers,
            )
        else:
            file_key = str(attachment.get("file_key") or "").strip()
            if not file_key:
                raise ValueError("file_key is required")
            response = await self._client.get(
                f"{self._base_url}/im/v1/messages/{message_id}/resources/{file_key}",
                params={"type": str(attachment.get("resource_type") or "file")},
                headers=headers,
            )
        data = self._parse_binary_response(response)
        fallback_name = str(attachment.get("file_name") or "").strip()
        data["file_name"] = data.get("file_name") or fallback_name or self._default_attachment_filename(attachment)
        return data

    async def send_card(self, chat_id: str, card: dict[str, Any]) -> None:
        if not chat_id:
            raise ValueError("chat_id is required")
        token = await self.refresh_token()
        payload = {
            "receive_id": chat_id,
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
        }
        response = await self._client.post(
            f"{self._base_url}/im/v1/messages",
            params={"receive_id_type": "chat_id"},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json=payload,
        )
        data = self._parse_response(response)
        if data.get("code", 0) != 0:
            raise RuntimeError(
                f"Feishu send_card failed code={data.get('code')} msg={data.get('msg')}"
            )

    async def refresh_token(self) -> str:
        if not self.configured:
            raise RuntimeError("Feishu adapter is not configured")
        now = time.time()
        if self._access_token and now < self._access_token_expires_at:
            return self._access_token

        response = await self._client.post(
            f"{self._base_url}/auth/v3/tenant_access_token/internal",
            json={
                "app_id": self._app_id,
                "app_secret": self._app_secret,
            },
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        data = self._parse_response(response)
        token = str(data.get("tenant_access_token") or "")
        if not token:
            raise RuntimeError(f"Feishu token refresh failed: {data}")

        expire_seconds = int(data.get("expire", 7200) or 7200)
        self._access_token = token
        self._access_token_expires_at = now + max(60, expire_seconds - 120)
        return token

    @staticmethod
    def _parse_response(response: Any) -> dict[str, Any]:
        if hasattr(response, "raise_for_status"):
            response.raise_for_status()
        if hasattr(response, "json"):
            return response.json()
        raise RuntimeError("Unsupported HTTP client response")

    @staticmethod
    def _parse_binary_response(response: Any) -> dict[str, Any]:
        if hasattr(response, "raise_for_status"):
            response.raise_for_status()
        content = bytes(getattr(response, "content", b"") or b"")
        headers = getattr(response, "headers", {}) or {}
        content_type = str(headers.get("content-type") or "").strip() or None
        disposition = str(headers.get("content-disposition") or "").strip()
        file_name = None
        if disposition:
            match = re.search(r'filename=\"?([^\";]+)\"?', disposition)
            if match:
                file_name = match.group(1).strip()
        return {
            "bytes": content,
            "mime_type": content_type,
            "file_name": file_name,
        }

    @staticmethod
    def _default_attachment_filename(attachment: dict[str, Any]) -> str:
        attachment_type = str(attachment.get("attachment_type") or "file").strip().lower()
        if attachment_type == "image":
            key = str(attachment.get("image_key") or attachment.get("message_id") or "image")
            return f"{key}.png"
        key = str(attachment.get("file_key") or attachment.get("message_id") or attachment_type)
        return f"{attachment_type}-{key}"


class FeishuLongConnectionRunner:
    """Minimal Feishu long-connection runner for Nexus."""

    def __init__(
        self,
        adapter: FeishuAdapter,
        *,
        on_message: Callable[[dict[str, Any]], Awaitable[None]],
        ack_timeout_seconds: float = 2.5,
        auto_restart: bool = True,
        restart_initial_seconds: float = 2.0,
        restart_max_seconds: float = 60.0,
        max_restarts: int = 0,
        log_level: str = "INFO",
    ) -> None:
        self._adapter = adapter
        self._on_message = on_message
        self._ack_timeout_seconds = max(0.1, float(ack_timeout_seconds or 2.5))
        self._auto_restart = bool(auto_restart)
        self._restart_initial_seconds = max(0.5, float(restart_initial_seconds or 2.0))
        self._restart_max_seconds = max(self._restart_initial_seconds, float(restart_max_seconds or 60.0))
        self._max_restarts = max(0, int(max_restarts or 0))
        self._log_level_name = str(log_level or "INFO").upper()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._client: Any | None = None
        self._lark: Any | None = None
        self._thread_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._running = False
        self._last_error: str | None = None
        self._start_ts = 0.0
        self._restart_count = 0
        self._consecutive_failures = 0
        self._last_restart_ts = 0.0

    def status(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "running": self._running,
            "thread_alive": bool(self._thread and self._thread.is_alive()),
            "last_error": self._last_error,
            "restart_count": self._restart_count,
            "consecutive_failures": self._consecutive_failures,
        }

    def start(self, *, loop: asyncio.AbstractEventLoop) -> None:
        if self._running:
            return
        if self._adapter.subscription_mode() != "long_connection":
            logger.info("Feishu long connection runner skipped (mode=%s)", self._adapter.subscription_mode())
            return
        if not self._adapter.configured:
            raise RuntimeError("Feishu adapter is not configured")
        try:
            import lark_oapi as lark  # type: ignore
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("Missing dependency: lark-oapi") from exc
        self._loop = loop
        self._lark = lark
        self._stop_event.clear()
        self._running = True
        self._last_error = None
        self._start_ts = time.time()
        self._restart_count = 0
        self._consecutive_failures = 0
        self._last_restart_ts = 0.0
        self._thread = threading.Thread(target=self._run_supervisor, name="nexus-feishu-longconn", daemon=True)
        self._thread.start()
        logger.info("Feishu long connection runner started")

    def shutdown(self) -> None:
        self._stop_event.set()
        client = self._client
        if client is not None and hasattr(client, "stop"):
            try:
                client.stop()
            except Exception:
                pass
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=2.0)
        self._running = False
        self._client = None

    def _build_client(self) -> Any:
        lark = self._lark
        if lark is None:
            raise RuntimeError("lark sdk not initialized")
        log_level = getattr(lark.LogLevel, self._log_level_name, getattr(lark.LogLevel, "INFO", None))
        if log_level is None:
            log_level = getattr(lark.LogLevel, "INFO")
        handler_builder = lark.EventDispatcherHandler.builder("", "")
        event_handler = handler_builder.register_p2_im_message_receive_v1(self._on_message_event).build()
        return lark.ws.Client(
            self._adapter._app_id,  # noqa: SLF001
            self._adapter._app_secret,  # noqa: SLF001
            event_handler=event_handler,
            log_level=log_level,
        )

    def _run_client_once(self) -> float:
        ws_loop: asyncio.AbstractEventLoop | None = None
        started = time.time()
        try:
            import lark_oapi.ws.client as lark_ws_client  # type: ignore

            ws_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(ws_loop)
            lark_ws_client.loop = ws_loop
            client = self._build_client()
            with self._thread_lock:
                self._client = client
                self._running = True
            client.start()
        finally:
            with self._thread_lock:
                self._running = False
                self._client = None
            if ws_loop is not None:
                try:
                    ws_loop.close()
                except Exception:
                    pass
        return max(0.0, time.time() - started)

    def _next_restart_delay(self) -> float:
        exponent = max(0, self._consecutive_failures)
        delay = self._restart_initial_seconds * (2 ** exponent)
        return min(self._restart_max_seconds, delay)

    def _run_supervisor(self) -> None:
        while not self._stop_event.is_set():
            had_error = False
            session_seconds = 0.0
            try:
                session_seconds = self._run_client_once()
                self._last_error = None
            except Exception as exc:  # noqa: BLE001
                had_error = True
                self._last_error = str(exc)
                logger.warning("Feishu long connection client stopped with error: %s", exc)
            if self._stop_event.is_set():
                break
            if session_seconds >= 30.0 and not had_error:
                self._consecutive_failures = 0
            else:
                self._consecutive_failures = min(self._consecutive_failures + 1, 20)
            if not self._auto_restart:
                logger.warning("Feishu long connection exited; auto-restart disabled")
                break
            if self._max_restarts > 0 and self._restart_count >= self._max_restarts:
                logger.warning("Feishu long connection reached max restarts: %s", self._max_restarts)
                break
            delay = self._next_restart_delay()
            self._restart_count += 1
            self._last_restart_ts = time.time()
            logger.warning(
                "Feishu long connection restarting in %.1fs (restart=%s, consecutive_failures=%s)",
                delay,
                self._restart_count,
                self._consecutive_failures,
            )
            if self._stop_event.wait(delay):
                break
        with self._thread_lock:
            self._running = False

    def _on_message_event(self, data: Any) -> None:
        loop = self._loop
        if loop is None:
            return
        event = self._adapter.parse_long_connection_message_event(data)
        if not event:
            payload_preview = self._adapter._to_payload_dict(data)  # noqa: SLF001
            logger.warning("Feishu long connection ignored payload=%s", str(payload_preview)[:800])
            return
        logger.warning(
            "Feishu long connection received: message_id=%s sender=%s chat_id=%s type=%s text=%s",
            event.get("message_id"),
            event.get("sender_user_id"),
            event.get("chat_id"),
            event.get("message_type"),
            str(event.get("text") or "")[:200],
        )
        future = asyncio.run_coroutine_threadsafe(self._on_message(event), loop)
        try:
            future.result(timeout=self._ack_timeout_seconds)
        except FutureTimeoutError:
            logger.warning(
                "Feishu long connection dispatch timed out (>%ss), continue async",
                self._ack_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Feishu long connection dispatch failed: %s", exc)
