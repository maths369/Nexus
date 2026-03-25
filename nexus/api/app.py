"""
FastAPI Application — Nexus HTTP/WebSocket 入口

路由:
  POST /feishu/webhook    — 飞书事件回调
  POST /weixin/login/start — 微信扫码登录初始化
  POST /weixin/login/wait  — 微信扫码登录确认轮询
  WS   /ws                — Web 前端 WebSocket
  GET  /health            — 健康检查
  GET  /health/providers  — Provider 健康状态
  GET  /runs/active       — 活跃 Run 列表
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
from contextlib import asynccontextmanager

from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.responses import StreamingResponse

from nexus.api.runtime import NexusRuntime, build_runtime, start_mesh_runtime, stop_mesh_runtime
from nexus.channel.adapter_feishu import FeishuAdapter, FeishuLongConnectionRunner
from nexus.channel.adapter_weixin import WeixinAdapter, WeixinLongPollRunner
from nexus.channel.types import ChannelType, InboundMessage, OutboundMessage
from nexus.channel.message_formatter import MessageFormatter
from nexus.orchestrator import Orchestrator
from nexus.shared import NexusSettings, load_nexus_settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 全局状态（lifespan 管理）
# ---------------------------------------------------------------------------

_runtime: NexusRuntime | None = None
_orchestrator: Orchestrator | None = None
_feishu_adapter: FeishuAdapter | None = None
_feishu_longconn_runner: FeishuLongConnectionRunner | None = None
_weixin_adapter: WeixinAdapter | None = None
_weixin_longpoll_runner: WeixinLongPollRunner | None = None
_settings: NexusSettings | None = None

# Feishu message_id 去重（防止 webhook 重复投递）
_DEDUP_TTL_SECONDS = 300  # 5 分钟内的重复消息会被忽略
_seen_message_ids: dict[str, float] = {}  # message_id → first_seen_timestamp
_seen_weixin_message_ids: dict[str, float] = {}


def _hub_reply_signature() -> str:
    host = socket.gethostname()
    pid = os.getpid()
    return f"[hub:{host}:{pid}]"


def _render_outbound_for_channel(channel: ChannelType, outbound: OutboundMessage) -> str:
    formatter = getattr(_orchestrator, "_formatter", None)
    if isinstance(formatter, MessageFormatter):
        return formatter.render_for_channel(channel, outbound)
    return MessageFormatter().render_for_channel(channel, outbound)


def _render_feishu_card(outbound: OutboundMessage) -> dict[str, Any]:
    formatter = getattr(_orchestrator, "_formatter", None)
    if isinstance(formatter, MessageFormatter):
        return formatter.render_feishu_card(outbound)
    return MessageFormatter().render_feishu_card(outbound)


class CreatePageRequest(BaseModel):
    title: str
    body: str = ""
    section: str = "pages"
    page_type: str = "note"
    parent_id: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


class UpdatePageRequest(BaseModel):
    relative_path: str
    content: str
    title: str | None = None


class AppendBlockRequest(BaseModel):
    relative_path: str
    block_markdown: str
    heading: str | None = None
    title: str | None = None


class ReplaceSectionRequest(BaseModel):
    relative_path: str
    heading: str
    body: str
    level: int = 2
    create_if_missing: bool = True
    title: str | None = None


class ChecklistRequest(BaseModel):
    relative_path: str
    items: list[str]
    heading: str | None = None


class TableRequest(BaseModel):
    relative_path: str
    headers: list[str]
    rows: list[list[str]]
    heading: str | None = None


class PageLinkRequest(BaseModel):
    relative_path: str
    target: str
    label: str | None = None
    heading: str | None = None


class DesktopMessageRequest(BaseModel):
    content: str
    sender_id: str = "desktop_default"
    session_id: str | None = None
    device_id: str = "mac"
    route_mode: str = "auto"  # "auto" | "hub" | "mac"


class VaultWriteRequest(BaseModel):
    path: str
    content: str


class VaultCreateRequest(BaseModel):
    path: str
    is_dir: bool = False


class EdgeJournalSyncRequest(BaseModel):
    node_id: str
    entries: list[dict[str, object]] = Field(default_factory=list)


class MaterializeTranscriptRequest(BaseModel):
    source_name: str
    transcript: str
    summary: str = ""
    action_items: list[str] = Field(default_factory=list)
    target_section: str = "meetings"
    title: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


class BrowserNavigateRequest(BaseModel):
    url: str


class BrowserExtractTextRequest(BaseModel):
    selector: str | None = None


class BrowserScreenshotRequest(BaseModel):
    path: str | None = None


class BrowserFillFormRequest(BaseModel):
    fields: dict[str, str]


class WeixinLoginStartRequest(BaseModel):
    account_id: str | None = None
    bot_type: str | None = None
    base_url: str | None = None


class WeixinLoginWaitRequest(BaseModel):
    session_key: str
    timeout_ms: int = 480000


def _require_runtime() -> NexusRuntime:
    if _runtime is None:
        raise RuntimeError("Runtime not initialized")
    return _runtime


def _build_feishu_inbound(
    event: dict[str, object],
    *,
    content: str,
    attachments: list[dict[str, object]] | None = None,
) -> InboundMessage:
    chat_id = str(event.get("chat_id") or "")
    channel_key = f"feishu:{chat_id}" if chat_id else "feishu"
    return InboundMessage(
        message_id=str(event.get("message_id") or ""),
        channel=ChannelType.FEISHU,
        sender_id=str(event.get("sender_user_id") or ""),
        content=content,
        metadata={
            "chat_id": chat_id,
            "chat_type": str(event.get("chat_type") or ""),
            "message_type": str(event.get("message_type") or "text"),
            "source": "feishu",
            "channel_key": channel_key,
        },
        attachments=[dict(item) for item in (attachments or [])],
    )


def _make_feishu_reply(chat_id: str):
    async def send_feishu_reply(outbound: OutboundMessage) -> None:
        if not _feishu_adapter or not _feishu_adapter.configured:
            logger.warning(
                "Feishu adapter not configured; dropping reply type=%s chat_id=%s",
                outbound.message_type.value,
                chat_id,
            )
            return
        try:
            card = _render_feishu_card(outbound)
            await _feishu_adapter.send_card(chat_id, card)
            logger.warning(
                "Feishu card reply %s [%s] to %s",
                _hub_reply_signature(),
                outbound.message_type.value,
                chat_id,
            )
            return
        except Exception:
            logger.exception(
                "Feishu card reply failed; falling back to text type=%s chat_id=%s",
                outbound.message_type.value,
                chat_id,
            )

        content = _render_outbound_for_channel(ChannelType.FEISHU, outbound)
        await _feishu_adapter.send_text(chat_id, content)
        logger.warning(
            "Feishu text fallback %s [%s] to %s: %s",
            _hub_reply_signature(),
            outbound.message_type.value,
            chat_id,
            content[:100],
        )

    return send_feishu_reply


def _build_weixin_inbound(event: dict[str, object], *, content: str) -> InboundMessage:
    account_id = str(event.get("account_id") or "").strip() or "default"
    sender_user_id = str(event.get("sender_user_id") or "").strip()
    sender_key = f"{account_id}:{sender_user_id}" if sender_user_id else account_id
    channel_key = f"weixin:{account_id}:{sender_user_id}" if sender_user_id else f"weixin:{account_id}"
    return InboundMessage(
        message_id=str(event.get("message_id") or ""),
        channel=ChannelType.WEIXIN,
        sender_id=sender_key,
        content=content,
        metadata={
            "account_id": account_id,
            "sender_user_id": sender_user_id,
            "message_type": str(event.get("message_type") or "text"),
            "source": "weixin",
            "channel_key": channel_key,
            "context_token": str(event.get("context_token") or ""),
            "session_id": str(event.get("session_id") or ""),
        },
        attachments=[],
    )


def _make_weixin_reply(account_id: str, to_user_id: str, context_token: str):
    async def send_weixin_reply(outbound: OutboundMessage) -> None:
        if not _weixin_adapter or not _weixin_adapter.configured:
            logger.warning(
                "Weixin adapter not configured; dropping reply type=%s account_id=%s to=%s",
                outbound.message_type.value,
                account_id,
                to_user_id,
            )
            return
        content = _render_outbound_for_channel(ChannelType.WEIXIN, outbound)
        await _weixin_adapter.send_text(
            account_id,
            to_user_id,
            content,
            context_token=context_token or None,
        )
        logger.warning(
            "Weixin reply %s [%s] via %s to %s: %s",
            _hub_reply_signature(),
            outbound.message_type.value,
            account_id,
            to_user_id,
            content[:100],
        )

    return send_weixin_reply


async def _dispatch_feishu_event(event: dict[str, object]) -> None:
    if not _orchestrator:
        raise RuntimeError("Orchestrator not initialized")
    if bool(event.get("ignored")):
        logger.warning(
            "Feishu message ignored: reason=%s chat_id=%s message_id=%s",
            event.get("reason"),
            event.get("chat_id"),
            event.get("message_id"),
        )
        return

    # ── message_id 去重 ──
    message_id = str(event.get("message_id") or "").strip()
    if message_id:
        now = time.monotonic()
        # 清理过期条目（惰性清理，每次最多扫描全量）
        expired = [
            mid for mid, ts in _seen_message_ids.items()
            if now - ts > _DEDUP_TTL_SECONDS
        ]
        for mid in expired:
            _seen_message_ids.pop(mid, None)
        # 检查是否重复
        if message_id in _seen_message_ids:
            logger.warning(
                "Feishu message deduplicated (already processed): message_id=%s chat_id=%s",
                message_id,
                event.get("chat_id"),
            )
            return
        _seen_message_ids[message_id] = now

    attachment_count = len(event.get("attachments") or []) if isinstance(event.get("attachments"), list) else 0
    content = str(event.get("text") or "").strip()
    logger.warning(
        "Feishu inbound dispatch start: message_id=%s sender=%s chat_id=%s type=%s attachments=%s text=%s",
        event.get("message_id"),
        event.get("sender_user_id"),
        event.get("chat_id"),
        event.get("message_type"),
        attachment_count,
        content[:200],
    )
    artifact_attachments: list[dict[str, object]] = []
    artifact_summary = ""
    attachments = event.get("attachments")
    if isinstance(attachments, list) and attachments:
        artifact_attachments, artifact_summary = await _ingest_feishu_artifacts(event, attachments)
    combined_content = "\n\n".join(part for part in [content, artifact_summary] if part).strip()
    if not combined_content:
        combined_content = "用户发送了附件，请基于附件摘要继续处理。"
    inbound = _build_feishu_inbound(
        event,
        content=combined_content,
        attachments=artifact_attachments,
    )
    try:
        await _orchestrator.handle_message(
            inbound,
            _make_feishu_reply(str(event.get("chat_id") or "")),
        )
    except Exception:
        logger.exception(
            "Feishu inbound dispatch failed: message_id=%s sender=%s chat_id=%s",
            event.get("message_id"),
            event.get("sender_user_id"),
            event.get("chat_id"),
        )
        raise
    logger.warning(
        "Feishu inbound dispatch completed: message_id=%s sender=%s chat_id=%s",
        event.get("message_id"),
        event.get("sender_user_id"),
        event.get("chat_id"),
    )


async def _dispatch_weixin_event(event: dict[str, object]) -> None:
    if not _orchestrator:
        raise RuntimeError("Orchestrator not initialized")
    if bool(event.get("ignored")):
        logger.warning(
            "Weixin message ignored: reason=%s account_id=%s sender=%s message_id=%s",
            event.get("reason"),
            event.get("account_id"),
            event.get("sender_user_id"),
            event.get("message_id"),
        )
        return

    message_id = str(event.get("message_id") or "").strip()
    dedup_key = f"{event.get('account_id')}:{message_id}" if message_id else ""
    if dedup_key:
        now = time.monotonic()
        expired = [
            mid for mid, ts in _seen_weixin_message_ids.items()
            if now - ts > _DEDUP_TTL_SECONDS
        ]
        for mid in expired:
            _seen_weixin_message_ids.pop(mid, None)
        if dedup_key in _seen_weixin_message_ids:
            logger.warning(
                "Weixin message deduplicated: account_id=%s message_id=%s",
                event.get("account_id"),
                message_id,
            )
            return
        _seen_weixin_message_ids[dedup_key] = now

    content = str(event.get("text") or "").strip()
    if not content:
        logger.warning(
            "Weixin empty text ignored: account_id=%s sender=%s message_id=%s",
            event.get("account_id"),
            event.get("sender_user_id"),
            event.get("message_id"),
        )
        return

    inbound = _build_weixin_inbound(event, content=content)
    try:
        await _orchestrator.handle_message(
            inbound,
            _make_weixin_reply(
                str(event.get("account_id") or ""),
                str(event.get("sender_user_id") or ""),
                str(event.get("context_token") or ""),
            ),
        )
    except Exception:
        logger.exception(
            "Weixin inbound dispatch failed: account_id=%s sender=%s message_id=%s",
            event.get("account_id"),
            event.get("sender_user_id"),
            event.get("message_id"),
        )
        raise
    logger.warning(
        "Weixin inbound dispatch completed: account_id=%s sender=%s message_id=%s",
        event.get("account_id"),
        event.get("sender_user_id"),
        event.get("message_id"),
    )


async def _ingest_feishu_artifacts(
    event: dict[str, object],
    attachments: list[object],
) -> tuple[list[dict[str, object]], str]:
    runtime = _require_runtime()
    if _feishu_adapter is None:
        return [], ""

    payloads: list[dict[str, object]] = []
    results = []
    lines = ["[附加资产摘要]"]
    for raw in attachments:
        if not isinstance(raw, dict):
            continue
        try:
            downloaded = await _feishu_adapter.download_attachment(
                message_id=str(event.get("message_id") or ""),
                attachment=raw,
            )
            result = await runtime.artifact_service.ingest_bytes(
                artifact_type=str(raw.get("attachment_type") or "file"),
                source="feishu",
                data=bytes(downloaded.get("bytes") or b""),
                filename=str(downloaded.get("file_name") or raw.get("file_name") or ""),
                mime_type=str(downloaded.get("mime_type") or raw.get("mime_type") or ""),
                metadata={
                    "chat_id": str(event.get("chat_id") or ""),
                    "message_id": str(event.get("message_id") or ""),
                    "sender_user_id": str(event.get("sender_user_id") or ""),
                    "attachment_type": str(raw.get("attachment_type") or "file"),
                },
            )
            results.append(result)
            payloads.append(result.attachment_payload())
            lines.append(f"- {result.summary_line()}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to ingest Feishu attachment: %s", exc, exc_info=True)
            name = str(raw.get("file_name") or raw.get("image_key") or raw.get("file_key") or "attachment")
            lines.append(f"- 资产 `{name}` 下载/物化失败：{exc}")
    if len(results) > 1:
        manifest = await runtime.artifact_service.create_batch_manifest(
            results,
            source="feishu",
            metadata={
                "chat_id": str(event.get("chat_id") or ""),
                "message_id": str(event.get("message_id") or ""),
                "sender_user_id": str(event.get("sender_user_id") or ""),
            },
        )
        if manifest.page_relative_path:
            lines.append(f"- 批量导入清单：`{manifest.page_relative_path}`")
    return payloads, "\n".join(lines) if len(lines) > 1 else ""


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    global _runtime, _orchestrator, _feishu_adapter, _feishu_longconn_runner
    global _weixin_adapter, _weixin_longpoll_runner, _settings

    _settings = load_nexus_settings()
    _runtime = build_runtime(settings=_settings)
    try:
        identity_stats = await _runtime.memory_manager.reindex_identity_documents(force=False)
        logger.info("Identity documents indexed at startup: %s", identity_stats)
    except Exception:
        logger.warning("Failed to index identity documents at startup", exc_info=True)
    await start_mesh_runtime(_runtime)

    # Wire TaskManager events to SSE push
    task_mgr = getattr(_runtime, "mesh_task_manager", None)
    if task_mgr is not None:
        from nexus.mesh.task_store import TaskEvent as _TaskEvent

        async def _on_any_task_event(event: _TaskEvent) -> None:
            # Find the session_id from the task
            task = task_mgr.store.get(event.task_id)
            if task is None:
                return
            await _push_task_event_to_session(task.session_id, {
                "type": f"task_{event.event_type}",
                "task_id": event.task_id,
                "content": event.content,
                "progress": event.progress,
                "metadata": event.metadata,
            })

        task_mgr.on_any_task_event(_on_any_task_event)

    _orchestrator = Orchestrator(
        session_router=_runtime.session_router,
        session_store=_runtime.session_store,
        context_window=_runtime.context_window,
        run_manager=_runtime.run_manager,
        formatter=MessageFormatter(),
        provider_gateway=_runtime.provider,
        search_config=_runtime.search_config,
        config_path=_runtime.settings.config_path,
        available_tools=_runtime.available_tools,
        skill_manager=_runtime.skill_manager,
        capability_manager=_runtime.capability_manager,
        task_router=getattr(_runtime, "mesh_task_router", None),
        mesh_registry=getattr(_runtime, "mesh_registry", None),
    )
    feishu_config = _settings.feishu_config()
    _feishu_adapter = FeishuAdapter(feishu_config) if feishu_config.get("enabled") else None
    subscription_mode = (
        _feishu_adapter.subscription_mode()
        if _feishu_adapter is not None and hasattr(_feishu_adapter, "subscription_mode")
        else str(feishu_config.get("subscription_mode", "webhook") or "webhook")
    )
    if _feishu_adapter is not None and subscription_mode == "long_connection":
        _feishu_longconn_runner = FeishuLongConnectionRunner(
            _feishu_adapter,
            on_message=_dispatch_feishu_event,
            ack_timeout_seconds=float(feishu_config.get("long_connection_ack_timeout_seconds", 2.5) or 2.5),
            auto_restart=bool(feishu_config.get("long_connection_auto_restart", True)),
            restart_initial_seconds=float(
                feishu_config.get("long_connection_restart_initial_seconds", 2.0) or 2.0
            ),
            restart_max_seconds=float(
                feishu_config.get("long_connection_restart_max_seconds", 60.0) or 60.0
            ),
            max_restarts=int(feishu_config.get("long_connection_max_restarts", 0) or 0),
            log_level=str(feishu_config.get("long_connection_log_level", "INFO") or "INFO"),
        )
        _feishu_longconn_runner.start(loop=asyncio.get_running_loop())

    weixin_config = _settings.weixin_config()
    _weixin_adapter = WeixinAdapter(weixin_config) if weixin_config.get("enabled") else None
    if _weixin_adapter is not None:
        _weixin_longpoll_runner = WeixinLongPollRunner(
            _weixin_adapter,
            on_message=_dispatch_weixin_event,
            long_poll_timeout_ms=int(weixin_config.get("long_poll_timeout_ms", 35000) or 35000),
            retry_delay_seconds=float(weixin_config.get("retry_delay_seconds", 2.0) or 2.0),
            backoff_delay_seconds=float(weixin_config.get("backoff_delay_seconds", 30.0) or 30.0),
            max_consecutive_failures=int(weixin_config.get("max_consecutive_failures", 3) or 3),
            session_expired_pause_seconds=float(
                weixin_config.get("session_expired_pause_seconds", 600.0) or 600.0
            ),
        )
        _weixin_longpoll_runner.start(loop=asyncio.get_running_loop())

    logger.info("Nexus runtime initialized, root=%s", _settings.root_dir)
    yield

    logger.info("Nexus runtime shutting down")
    if _feishu_longconn_runner is not None:
        _feishu_longconn_runner.shutdown()
    if _weixin_longpoll_runner is not None:
        _weixin_longpoll_runner.shutdown()
    if _runtime is not None:
        await stop_mesh_runtime(_runtime)
        await _runtime.background_manager.aclose()
        await _runtime.browser_service.aclose()
    if _feishu_adapter is not None:
        await _feishu_adapter.aclose()
    if _weixin_adapter is not None:
        await _weixin_adapter.aclose()
    _runtime = None
    _orchestrator = None
    _feishu_adapter = None
    _feishu_longconn_runner = None
    _weixin_adapter = None
    _weixin_longpoll_runner = None
    _settings = None


# ---------------------------------------------------------------------------
# FastAPI App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Nexus / 星策",
    version="0.1.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5174",
        "http://localhost:5175",
        "http://127.0.0.1:5175",
        "http://localhost:1420",
        "http://127.0.0.1:1420",
        "tauri://localhost",
        "https://tauri.localhost",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# 健康检查
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    """基础健康检查"""
    return {"status": "ok", "version": "0.1.0"}


@app.get("/health/providers")
async def health_providers():
    """Provider 健康状态"""
    if not _runtime:
        return JSONResponse(
            status_code=503,
            content={"error": "Runtime not initialized"},
        )
    snapshot = _runtime.provider.get_health_snapshot()
    return {"providers": snapshot}


@app.get("/health/browser")
async def health_browser():
    runtime = _require_runtime()
    try:
        payload = await runtime.browser_service.health()
        return {"available": True, "worker": payload}
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "error": str(exc)}


@app.get("/health/audio")
async def health_audio():
    runtime = _require_runtime()
    return {
        "available": runtime.audio_service.is_available(),
        "backend": runtime.audio_service.config.backend,
        "device": runtime.audio_service.config.sensevoice_device,
        "base_url": runtime.audio_service.config.base_url,
    }


@app.post("/browser/navigate")
async def browser_navigate(payload: BrowserNavigateRequest):
    runtime = _require_runtime()
    result = await runtime.browser_service.navigate(payload.url)
    return {"result": result}


@app.post("/browser/extract-text")
async def browser_extract_text(payload: BrowserExtractTextRequest):
    runtime = _require_runtime()
    result = await runtime.browser_service.extract_text(payload.selector)
    return {"result": result}


@app.post("/browser/screenshot")
async def browser_screenshot(payload: BrowserScreenshotRequest):
    runtime = _require_runtime()
    result = await runtime.browser_service.screenshot(payload.path)
    return {"result": result}


@app.post("/browser/fill-form")
async def browser_fill_form(payload: BrowserFillFormRequest):
    runtime = _require_runtime()
    result = await runtime.browser_service.fill_form(dict(payload.fields))
    return {"result": result}


# ---------------------------------------------------------------------------
# 飞书 Webhook
# ---------------------------------------------------------------------------

@app.post("/feishu/webhook")
async def feishu_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    飞书事件回调。

    处理:
    1. URL 验证（challenge）
    2. 消息接收事件 → Orchestrator
    """
    raw_body = await request.body()
    payload = json.loads(raw_body.decode("utf-8") or "{}")

    if not _orchestrator or not _runtime:
        return JSONResponse(
            status_code=503,
            content={"code": -1, "msg": "Runtime not ready"},
        )
    if not _settings or not _settings.feishu_config().get("enabled"):
        return JSONResponse(
            status_code=503,
            content={"code": -1, "msg": "Feishu channel disabled"},
        )
    if not _feishu_adapter:
        return JSONResponse(
            status_code=503,
            content={"code": -1, "msg": "Feishu adapter unavailable"},
        )

    verify_callback = getattr(_feishu_adapter, "verify_callback", None)
    if callable(verify_callback):
        ok, reason = verify_callback(
            headers=dict(request.headers),
            payload=payload,
            raw_body=raw_body,
        )
        if not ok:
            return JSONResponse(
                status_code=403,
                content={"code": -1, "msg": f"Feishu callback rejected: {reason}"},
            )

    is_url_verification = getattr(_feishu_adapter, "is_url_verification", None)
    extract_challenge = getattr(_feishu_adapter, "extract_challenge", None)
    if callable(is_url_verification):
        if is_url_verification(payload):
            return {"challenge": extract_challenge(payload) if callable(extract_challenge) else str(payload.get("challenge") or "")}
    elif "challenge" in payload:
        return {"challenge": payload["challenge"]}

    parse_message_event = getattr(_feishu_adapter, "parse_message_event", None)
    if callable(parse_message_event):
        event = parse_message_event(payload)
    else:
        header = payload.get("header", {})
        if header.get("event_type") != "im.message.receive_v1":
            return {"code": 0}
        message_payload = (payload.get("event") or {}).get("message", {})
        sender = (payload.get("event") or {}).get("sender", {})
        content_str = message_payload.get("content", "{}")
        try:
            content_json = json.loads(content_str)
            text = content_json.get("text", content_str)
        except (json.JSONDecodeError, TypeError):
            text = content_str
        event = {
            "ignored": False,
            "message_id": str(message_payload.get("message_id") or ""),
            "chat_id": str(message_payload.get("chat_id") or ""),
            "chat_type": str(message_payload.get("chat_type") or ""),
            "sender_user_id": str(((sender.get("sender_id") or {}).get("open_id")) or ""),
            "text": str(text or ""),
        }
    if not event:
        return {"code": 0}
    background_tasks.add_task(_dispatch_feishu_event, event)

    return {"code": 0}


# ---------------------------------------------------------------------------
# 微信 Long Poll / 登录
# ---------------------------------------------------------------------------

@app.get("/weixin/status")
async def weixin_status():
    if not _settings or not _settings.weixin_config().get("enabled"):
        return {
            "enabled": False,
            "adapter": None,
            "runner": None,
        }
    return {
        "enabled": True,
        "adapter": _weixin_adapter.status_snapshot() if _weixin_adapter is not None else None,
        "runner": _weixin_longpoll_runner.status() if _weixin_longpoll_runner is not None else None,
    }


@app.post("/weixin/login/start")
async def weixin_login_start(payload: WeixinLoginStartRequest):
    if not _settings or not _settings.weixin_config().get("enabled"):
        return JSONResponse(
            status_code=503,
            content={"code": -1, "msg": "Weixin channel disabled"},
        )
    if _weixin_adapter is None:
        return JSONResponse(
            status_code=503,
            content={"code": -1, "msg": "Weixin adapter unavailable"},
        )
    try:
        result = await _weixin_adapter.start_login(
            account_id=payload.account_id,
            bot_type=payload.bot_type,
            base_url=payload.base_url,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Weixin login start failed")
        return JSONResponse(
            status_code=500,
            content={"code": -1, "msg": f"Weixin login start failed: {exc}"},
        )
    return {"code": 0, "data": result}


@app.post("/weixin/login/wait")
async def weixin_login_wait(payload: WeixinLoginWaitRequest):
    if not _settings or not _settings.weixin_config().get("enabled"):
        return JSONResponse(
            status_code=503,
            content={"code": -1, "msg": "Weixin channel disabled"},
        )
    if _weixin_adapter is None:
        return JSONResponse(
            status_code=503,
            content={"code": -1, "msg": "Weixin adapter unavailable"},
        )
    try:
        result = await _weixin_adapter.wait_for_login(
            payload.session_key,
            timeout_ms=payload.timeout_ms,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Weixin login wait failed")
        return JSONResponse(
            status_code=500,
            content={"code": -1, "msg": f"Weixin login wait failed: {exc}"},
        )
    if bool(result.get("connected")) and _weixin_longpoll_runner is not None:
        _weixin_longpoll_runner.ensure_account(str(result.get("account_id") or ""))
    return {"code": 0, "data": result}


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Web 前端 WebSocket 端点"""
    await websocket.accept()
    ws_id = str(id(websocket))
    logger.info(f"WebSocket connected: {ws_id}")

    if not _orchestrator:
        await websocket.close(code=1013, reason="Runtime not ready")
        return

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({
                    "type": "error", "content": "Invalid JSON"
                })
                continue

            msg_type = data.get("type", "")
            if msg_type == "ping":
                await websocket.send_json({"type": "pong"})
                continue

            if msg_type != "message":
                continue

            inbound = InboundMessage(
                message_id=f"ws_{ws_id}_{data.get('seq', 0)}",
                channel=ChannelType.WEB,
                sender_id=data.get("sender_id", f"web_{ws_id}"),
                content=data.get("content", ""),
            )

            async def ws_reply(outbound: OutboundMessage) -> None:
                await websocket.send_json({
                    "type": outbound.message_type.value,
                    "session_id": outbound.session_id,
                    "content": outbound.content,
                })

            await _orchestrator.handle_message(inbound, ws_reply)

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected: {ws_id}")
    except Exception as e:
        logger.error(f"WebSocket error: {e}", exc_info=True)
        try:
            await websocket.close(code=1011)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 活跃 Run 查询
# ---------------------------------------------------------------------------

@app.get("/runs/active")
async def active_runs():
    """查看当前活跃的 Run"""
    if not _runtime:
        return JSONResponse(
            status_code=503,
            content={"error": "Runtime not initialized"},
        )
    runs = await _runtime.run_store.get_active_runs()
    return {
        "runs": [
            {
                "run_id": r.run_id,
                "session_id": r.session_id,
                "status": r.status.value,
                "task": r.task[:100],
                "model": r.model,
                "attempt_count": r.attempt_count,
                "created_at": r.created_at.isoformat(),
            }
            for r in runs
        ]
    }


@app.get("/mesh/nodes")
async def mesh_nodes():
    runtime = _require_runtime()
    registry = getattr(runtime, "mesh_registry", None)
    if registry is None:
        return JSONResponse(
            status_code=503,
            content={"error": "Mesh runtime not initialized"},
        )

    nodes = []
    for card in registry.list_nodes(online_only=False):
        status = registry.get_node_status(card.node_id)
        nodes.append(
            {
                "node_id": card.node_id,
                "display_name": card.display_name,
                "node_type": card.node_type.value,
                "online": bool(status.online) if status else False,
                "active_tasks": int(status.active_tasks) if status else 0,
                "current_load": float(status.current_load) if status else 0.0,
                "last_heartbeat": float(status.last_heartbeat) if status else 0.0,
                "capabilities": sorted(card.capability_ids()),
            }
        )
    return {"nodes": nodes, "count": len(nodes)}


@app.get("/documents/pages")
async def list_documents(section: str = "", limit: int = 200):
    runtime = _require_runtime()
    scan_limit = max(limit * 20, 5000)
    paths = runtime.document_service.list_pages(section=section, limit=scan_limit)
    pages = []
    for relative_path in paths:
        page = runtime.structural_index.get_page_by_path(relative_path)
        pages.append(
            {
                "relative_path": relative_path,
                "page_id": page.page_id if page else "",
                "title": page.title if page else relative_path,
                "page_type": page.page_type if page else "note",
                "updated_at": page.updated_at.isoformat() if page else None,
            }
        )
    pages.sort(key=lambda item: item["updated_at"] or "", reverse=True)
    limited_pages = pages[:limit]
    return {"pages": limited_pages, "count": len(limited_pages)}


@app.get("/documents/recent")
async def recent_documents(limit: int = 20):
    runtime = _require_runtime()
    pages = runtime.structural_index.list_recent_pages(limit=limit)
    return {
        "pages": [
            {
                "page_id": page.page_id,
                "relative_path": page.relative_path,
                "title": page.title,
                "page_type": page.page_type,
                "last_opened_at": page.last_opened_at.isoformat() if page.last_opened_at else None,
            }
            for page in pages
        ]
    }


@app.get("/documents/page")
async def get_document_page(path: str):
    runtime = _require_runtime()
    page = runtime.structural_index.get_page_by_path(path)
    if page is None:
        return JSONResponse(status_code=404, content={"error": f"Page not found: {path}"})
    content = runtime.document_service.read_page(path)
    backlinks = runtime.structural_index.get_backlinks(page.page_id)
    anchors = runtime.structural_index.list_block_anchors(page.page_id)
    collections = runtime.structural_index.list_collections(page.page_id)
    return {
        "page": {
            "page_id": page.page_id,
            "relative_path": page.relative_path,
            "title": page.title,
            "page_type": page.page_type,
            "metadata": page.metadata,
            "content": content,
            "backlinks": backlinks,
            "anchors": anchors,
            "collections": collections,
        }
    }


@app.post("/documents/page")
async def create_document_page(payload: CreatePageRequest):
    runtime = _require_runtime()
    page = await runtime.document_service.create_page(
        title=payload.title,
        body=payload.body,
        section=payload.section,
        page_type=payload.page_type,
        parent_id=payload.parent_id,
        metadata=dict(payload.metadata),
    )
    return {"page": page.__dict__}


@app.post("/documents/page/update")
async def update_document_page(payload: UpdatePageRequest):
    runtime = _require_runtime()
    page = await runtime.document_service.update_page(
        relative_path=payload.relative_path,
        content=payload.content,
        title=payload.title,
    )
    return {"page": page.__dict__}


@app.delete("/documents/page")
async def delete_document_page(path: str):
    runtime = _require_runtime()
    try:
        page = await runtime.document_service.delete_page(relative_path=path)
    except FileNotFoundError:
        return JSONResponse(status_code=404, content={"error": f"Page not found: {path}"})
    return {"page": page.__dict__, "deleted": True}


@app.post("/documents/edit/append")
async def append_document_block(payload: AppendBlockRequest):
    runtime = _require_runtime()
    page = await runtime.document_editor.append_markdown_block(
        relative_path=payload.relative_path,
        block_markdown=payload.block_markdown,
        heading=payload.heading,
        title=payload.title,
    )
    return {"page": page.__dict__}


@app.post("/documents/edit/replace-section")
async def replace_document_section(payload: ReplaceSectionRequest):
    runtime = _require_runtime()
    page = await runtime.document_editor.replace_section(
        relative_path=payload.relative_path,
        heading=payload.heading,
        body=payload.body,
        level=payload.level,
        create_if_missing=payload.create_if_missing,
        title=payload.title,
    )
    return {"page": page.__dict__}


@app.post("/documents/edit/checklist")
async def insert_document_checklist(payload: ChecklistRequest):
    runtime = _require_runtime()
    page = await runtime.document_editor.insert_checklist(
        relative_path=payload.relative_path,
        items=payload.items,
        heading=payload.heading,
    )
    return {"page": page.__dict__}


@app.post("/documents/edit/table")
async def insert_document_table(payload: TableRequest):
    runtime = _require_runtime()
    page = await runtime.document_editor.insert_table(
        relative_path=payload.relative_path,
        headers=payload.headers,
        rows=payload.rows,
        heading=payload.heading,
    )
    return {"page": page.__dict__}


@app.post("/documents/edit/page-link")
async def insert_document_page_link(payload: PageLinkRequest):
    runtime = _require_runtime()
    page = await runtime.document_editor.insert_page_link(
        relative_path=payload.relative_path,
        target=payload.target,
        label=payload.label,
        heading=payload.heading,
    )
    return {"page": page.__dict__}


@app.post("/audio/materialize")
async def materialize_audio_transcript(payload: MaterializeTranscriptRequest):
    runtime = _require_runtime()
    result = await runtime.audio_service.materialize_transcript(
        source_name=payload.source_name,
        transcript=payload.transcript,
        summary=payload.summary,
        action_items=payload.action_items,
        target_section=payload.target_section,
        title=payload.title,
        metadata=dict(payload.metadata),
    )
    return {
        "transcript_path": result.transcript_path,
        "summary": result.summary,
        "action_items": result.action_items,
        "page": result.page.__dict__,
    }


# ---------------------------------------------------------------------------
# Edge Journal Sync
# ---------------------------------------------------------------------------


@app.post("/edge/journal/sync")
async def edge_journal_sync(payload: EdgeJournalSyncRequest):
    """Receive execution journal entries from edge nodes."""
    runtime = _require_runtime()
    journal_store = getattr(runtime, "edge_journal_store", None)
    if journal_store is None:
        return JSONResponse(
            status_code=503,
            content={"error": "Edge journal store not available"},
        )
    accepted_ids = journal_store.ingest(
        node_id=payload.node_id,
        entries=[dict(e) for e in payload.entries],
    )
    return {
        "accepted": len(accepted_ids),
        "entry_ids": accepted_ids,
    }


@app.get("/edge/journal")
async def edge_journal_list(node_id: str = "", limit: int = 50):
    """List edge journal entries stored on the Hub."""
    runtime = _require_runtime()
    journal_store = getattr(runtime, "edge_journal_store", None)
    if journal_store is None:
        return JSONResponse(
            status_code=503,
            content={"error": "Edge journal store not available"},
        )
    entries = journal_store.list_entries(node_id=node_id, limit=limit)
    return {"entries": entries, "count": len(entries)}


# ---------------------------------------------------------------------------
# Desktop Channel (SSE)
# ---------------------------------------------------------------------------

@app.post("/desktop/message")
async def desktop_message(payload: DesktopMessageRequest):
    """Desktop App 对话端点 — 返回 SSE 流."""
    if not _orchestrator or not _runtime:
        return JSONResponse(status_code=503, content={"error": "Runtime not ready"})

    channel_key = f"desktop:{payload.device_id}"
    seq = int(asyncio.get_event_loop().time() * 1000)
    inbound = InboundMessage(
        message_id=f"desktop_{payload.device_id}_{seq}",
        channel=ChannelType.DESKTOP,
        sender_id=payload.sender_id,
        content=payload.content,
        metadata={
            "device_id": payload.device_id,
            "channel_key": channel_key,
            "source": "desktop",
            "route_mode": payload.route_mode,
        },
    )

    queue: asyncio.Queue[OutboundMessage | None] = asyncio.Queue()

    async def sse_reply(outbound: OutboundMessage) -> None:
        await queue.put(outbound)

    async def run_task():
        try:
            await _orchestrator.handle_message(inbound, sse_reply)
        except Exception as exc:
            logger.exception("Desktop message failed")
            error_msg = OutboundMessage(
                session_id="",
                message_type=OutboundMessageType("error"),
                content=str(exc),
            )
            await queue.put(error_msg)
        finally:
            await queue.put(None)  # sentinel

    async def event_generator():
        task = asyncio.create_task(run_task())
        try:
            while True:
                msg = await queue.get()
                if msg is None:
                    break
                data = json.dumps({
                    "type": msg.message_type.value,
                    "session_id": msg.session_id,
                    "content": msg.content,
                    "metadata": msg.metadata,
                }, ensure_ascii=False)
                yield f"data: {data}\n\n"
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Async Task Stream (SSE) — pushes task events from mesh dispatch
# ---------------------------------------------------------------------------

# Global dict mapping session_id -> list of SSE queues for that session
_task_event_queues: dict[str, list[asyncio.Queue]] = {}


def _register_task_event_queue(session_id: str, queue: asyncio.Queue) -> None:
    _task_event_queues.setdefault(session_id, []).append(queue)


def _unregister_task_event_queue(session_id: str, queue: asyncio.Queue) -> None:
    queues = _task_event_queues.get(session_id, [])
    if queue in queues:
        queues.remove(queue)
    if not queues:
        _task_event_queues.pop(session_id, None)


async def _push_task_event_to_session(session_id: str, event_data: dict) -> None:
    """Push a task event to all SSE streams listening on this session."""
    for q in _task_event_queues.get(session_id, []):
        try:
            await q.put(event_data)
        except Exception:
            pass


@app.get("/tasks/stream/{session_id}")
async def task_event_stream(session_id: str):
    """SSE stream for async task events on a session.

    Desktop connects to this after receiving a 'task dispatched' response.
    Events: task_dispatched, task_acknowledged, task_progress, task_completed, task_failed.
    """
    queue: asyncio.Queue[dict | None] = asyncio.Queue()
    _register_task_event_queue(session_id, queue)

    async def generator():
        try:
            while True:
                event = await asyncio.wait_for(queue.get(), timeout=600.0)
                if event is None:
                    break
                data = json.dumps(event, ensure_ascii=False)
                yield f"data: {data}\n\n"
        except asyncio.TimeoutError:
            yield f"data: {json.dumps({'type': 'timeout', 'content': 'Stream timeout'})}\n\n"
        finally:
            _unregister_task_event_queue(session_id, queue)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/tasks/{task_id}")
async def get_task_status(task_id: str):
    """Get current status of an async task."""
    if not _runtime:
        return JSONResponse(status_code=503, content={"error": "Runtime not ready"})
    task_mgr = getattr(_runtime, "mesh_task_manager", None)
    if task_mgr is None:
        return JSONResponse(status_code=404, content={"error": "TaskManager not available"})
    task = task_mgr.store.get(task_id)
    if task is None:
        return JSONResponse(status_code=404, content={"error": f"Task {task_id} not found"})
    return JSONResponse(content=task.to_dict())


class CreateTaskRequest(BaseModel):
    task_description: str
    target_node: str
    session_id: str = ""
    source_type: str = "api"
    source_id: str = ""
    constraints: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: float = 600.0


@app.post("/tasks")
async def create_task(req: CreateTaskRequest):
    """Submit a new async task to a mesh node."""
    if not _runtime:
        return JSONResponse(status_code=503, content={"error": "Runtime not ready"})
    task_mgr = getattr(_runtime, "mesh_task_manager", None)
    if task_mgr is None:
        return JSONResponse(status_code=404, content={"error": "TaskManager not available"})
    task = await task_mgr.submit_task(
        session_id=req.session_id,
        source_type=req.source_type,
        source_id=req.source_id,
        target_node=req.target_node,
        task_description=req.task_description,
        constraints=req.constraints or None,
        timeout_seconds=req.timeout_seconds,
    )
    return JSONResponse(
        status_code=201,
        content={"task_id": task.task_id, "status": task.status.value},
    )


# ---------------------------------------------------------------------------
# Vault CRUD (for Desktop file tree & editor)
# ---------------------------------------------------------------------------

def _vault_root() -> Path:
    """Return the vault root directory from runtime settings."""
    runtime = _require_runtime()
    return Path(runtime.settings.root_dir) / "vault"


def _safe_vault_path(relative: str) -> Path:
    """Resolve a relative path within vault, prevent traversal."""
    root = _vault_root()
    resolved = (root / relative).resolve()
    if not str(resolved).startswith(str(root.resolve())):
        raise ValueError(f"Path traversal detected: {relative}")
    return resolved


def _build_file_tree(directory: Path, base: Path, depth: int = 0, max_depth: int = 3) -> list[dict]:
    """Recursively build file tree for a directory."""
    if not directory.is_dir() or depth > max_depth:
        return []
    items = []
    try:
        entries = sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except PermissionError:
        return []
    for entry in entries:
        if entry.name.startswith(".") or entry.name == "__pycache__":
            continue
        rel = str(entry.relative_to(base))
        node = {
            "id": rel,
            "name": entry.name,
            "path": rel,
            "is_dir": entry.is_dir(),
            "children": _build_file_tree(entry, base, depth + 1, max_depth) if entry.is_dir() else [],
        }
        items.append(node)
    return items


@app.get("/vault/tree")
async def vault_tree(path: str = "", depth: int = 3):
    """Return vault directory tree as JSON."""
    root = _vault_root()
    target = _safe_vault_path(path) if path else root
    if not target.exists():
        return JSONResponse(status_code=404, content={"error": f"Path not found: {path}"})
    tree = _build_file_tree(target, root, max_depth=depth)
    return {"tree": tree, "root": str(root)}


@app.get("/vault/read")
async def vault_read(path: str):
    """Read a file from the vault."""
    file_path = _safe_vault_path(path)
    if not file_path.is_file():
        return JSONResponse(status_code=404, content={"error": f"File not found: {path}"})
    try:
        content = file_path.read_text(encoding="utf-8")
        return {"path": path, "content": content}
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@app.put("/vault/write")
async def vault_write(payload: VaultWriteRequest):
    """Write content to a vault file."""
    file_path = _safe_vault_path(payload.path)
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(payload.content, encoding="utf-8")
        return {"path": payload.path, "success": True}
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@app.post("/vault/create")
async def vault_create(payload: VaultCreateRequest):
    """Create a file or directory in the vault."""
    target = _safe_vault_path(payload.path)
    try:
        if payload.is_dir:
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                target.write_text("", encoding="utf-8")
        return {"path": payload.path, "success": True}
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@app.delete("/vault/delete")
async def vault_delete(path: str):
    """Delete a file or directory from the vault."""
    import shutil
    target = _safe_vault_path(path)
    if not target.exists():
        return JSONResponse(status_code=404, content={"error": f"Not found: {path}"})
    try:
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        return {"path": path, "success": True}
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})
