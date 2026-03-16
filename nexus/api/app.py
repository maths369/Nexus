"""
FastAPI Application — Nexus HTTP/WebSocket 入口

路由:
  POST /feishu/webhook    — 飞书事件回调
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
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from nexus.api.runtime import NexusRuntime, build_runtime, start_mesh_runtime, stop_mesh_runtime
from nexus.channel.adapter_feishu import FeishuAdapter, FeishuLongConnectionRunner
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
_settings: NexusSettings | None = None


def _feishu_reply_signature() -> str:
    host = socket.gethostname()
    pid = os.getpid()
    return f"[hub:{host}:{pid}]"


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
    return InboundMessage(
        message_id=str(event.get("message_id") or ""),
        channel=ChannelType.FEISHU,
        sender_id=str(event.get("sender_user_id") or ""),
        content=content,
        metadata={
            "chat_id": str(event.get("chat_id") or ""),
            "chat_type": str(event.get("chat_type") or ""),
            "message_type": str(event.get("message_type") or "text"),
            "source": "feishu",
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
        content = f"{_feishu_reply_signature()} {outbound.content}"
        await _feishu_adapter.send_text(chat_id, content)
        logger.warning(
            "Feishu reply [%s] to %s: %s",
            outbound.message_type.value,
            chat_id,
            content[:100],
        )

    return send_feishu_reply


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
    global _runtime, _orchestrator, _feishu_adapter, _feishu_longconn_runner, _settings

    _settings = load_nexus_settings()
    _runtime = build_runtime(settings=_settings)
    try:
        identity_stats = await _runtime.memory_manager.reindex_identity_documents(force=False)
        logger.info("Identity documents indexed at startup: %s", identity_stats)
    except Exception:
        logger.warning("Failed to index identity documents at startup", exc_info=True)
    await start_mesh_runtime(_runtime)

    _orchestrator = Orchestrator(
        session_router=_runtime.session_router,
        session_store=_runtime.session_store,
        context_window=_runtime.context_window,
        run_manager=_runtime.run_manager,
        formatter=MessageFormatter(),
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

    logger.info("Nexus runtime initialized, root=%s", _settings.root_dir)
    yield

    logger.info("Nexus runtime shutting down")
    if _feishu_longconn_runner is not None:
        _feishu_longconn_runner.shutdown()
    if _runtime is not None:
        await stop_mesh_runtime(_runtime)
        await _runtime.background_manager.aclose()
        await _runtime.browser_service.aclose()
    if _feishu_adapter is not None:
        await _feishu_adapter.aclose()
    _runtime = None
    _orchestrator = None
    _feishu_adapter = None
    _feishu_longconn_runner = None
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
