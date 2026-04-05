"""
Tool Registry — 运行时工具注册

包含基础工具和 Agent 能力工具:
- compact: 手动上下文压缩 (Layer 3)
- load_skill: 按需加载 Skill 完整内容 (Layer 2)
- todo_write: Agent 自我进度追踪
- dispatch_subagent: 子任务委派
- task_create/task_update/task_list/task_get: Task DAG 依赖编排
- background_run/check_background: 异步后台执行
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, TYPE_CHECKING
from urllib.parse import quote_plus, urljoin, urlparse

from nexus.agent.types import ToolDefinition, ToolRiskLevel
from nexus.knowledge import EpisodicMemory, KnowledgeIngestService, VaultContentStore
from nexus.services.audio import AudioService
from nexus.services.browser import BrowserService
from nexus.services.document import CollectionColumn, DocumentEditorService, DocumentService
from nexus.services.workspace import WorkspaceService

if TYPE_CHECKING:
    from nexus.evolution.audit import AuditLog
    from nexus.evolution.capability_manager import CapabilityManager
    from nexus.evolution.skill_manager import SkillManager
    from nexus.agent.todo import TodoManager
    from nexus.agent.subagent import SubagentRunner
    from nexus.agent.task_dag import TaskDAG
    from nexus.agent.background import BackgroundTaskManager
    from nexus.agent.system_run import SystemRunner
    from nexus.knowledge.memory_manager import MemoryManager
    from nexus.services.spreadsheet import SpreadsheetService


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False)


_VAULT_SECTION_ALIASES = {
    "pages",
    "inbox",
    "journals",
    "meetings",
    "strategy",
    "rnd",
    "life",
}
_GOOGLE_GROUNDING_REDIRECT_CACHE: dict[str, str] = {}


async def _perform_google_grounded_search(
    *,
    query: str,
    api_key: str,
    base_url: str,
    model: str,
    timeout_seconds: float = 20.0,
    max_output_tokens: int = 768,
) -> dict[str, Any]:
    import aiohttp

    endpoint = f"{base_url.rstrip('/')}/models/{model}:generateContent"
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": api_key,
    }
    payload = {
        "contents": [
            {
                "parts": [
                    {
                        "text": query,
                    }
                ]
            }
        ],
        "tools": [
            {
                "google_search": {},
            }
        ],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": int(max(128, max_output_tokens)),
        },
    }

    async with aiohttp.ClientSession(trust_env=True) as session:
        async with session.post(
            endpoint,
            headers=headers,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=timeout_seconds),
        ) as resp:
            payload = await resp.json(content_type=None)
            if resp.status >= 400:
                error = payload.get("error") if isinstance(payload, dict) else None
                status = str((error or {}).get("status") or "")
                message = str((error or {}).get("message") or payload)
                raise RuntimeError(f"Google grounded search error {resp.status} [{status}]: {message}")
            return payload


def _extract_domain(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").lower()
    except Exception:
        return ""


def _looks_like_google_grounding_redirect(url: str) -> bool:
    parsed = urlparse(str(url or "").strip())
    return (
        parsed.netloc.lower() == "vertexaisearch.cloud.google.com"
        and "/grounding-api-redirect/" in parsed.path
    )


async def _resolve_google_grounding_redirect_url(
    url: str,
    *,
    timeout_seconds: float = 10.0,
) -> str:
    import aiohttp

    candidate = str(url or "").strip()
    if not candidate or not _looks_like_google_grounding_redirect(candidate):
        return candidate
    cached = _GOOGLE_GROUNDING_REDIRECT_CACHE.get(candidate)
    if cached:
        return cached

    headers = {"User-Agent": "Mozilla/5.0"}
    async with aiohttp.ClientSession(trust_env=True, headers=headers) as session:
        for method in ("HEAD", "GET"):
            try:
                request = session.head if method == "HEAD" else session.get
                async with request(
                    candidate,
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=timeout_seconds),
                ) as resp:
                    location = str(resp.headers.get("Location") or "").strip()
                    if location:
                        resolved = urljoin(candidate, location)
                        _GOOGLE_GROUNDING_REDIRECT_CACHE[candidate] = resolved
                        return resolved
            except Exception:
                continue
    return candidate


def _build_google_grounded_prompt(query: str) -> str:
    prompt = str(query or "").strip()
    return (
        "Use Google Search to answer with current web information and sources. "
        "Prefer fresh, factual results. "
        f"Question: {prompt}"
    )


def _is_google_quota_error(exc: Exception) -> bool:
    message = str(exc or "").lower()
    return (
        "resource_exhausted" in message
        or "quota" in message
        or "rate limit" in message
        or "429" in message
    )


def _extract_google_text(payload: dict[str, Any]) -> str:
    candidates = list(payload.get("candidates") or [])
    if not candidates:
        return ""
    content = dict((candidates[0] or {}).get("content") or {})
    parts = list(content.get("parts") or [])
    texts = [str(part.get("text") or "").strip() for part in parts if isinstance(part, dict)]
    return "\n".join(text for text in texts if text).strip()


async def _format_google_grounded_payload(
    *,
    query: str,
    payload: dict[str, Any],
    max_chars: int,
) -> dict[str, Any]:
    candidates = list(payload.get("candidates") or [])
    metadata = dict((candidates[0] or {}).get("groundingMetadata") or {})
    raw_results = list(metadata.get("groundingChunks") or [])
    formatted_results: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for item in raw_results:
        web = dict((item or {}).get("web") or {})
        raw_url = str(web.get("uri") or "").strip()
        url = await _resolve_google_grounding_redirect_url(raw_url)
        title = str(web.get("title") or "").strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        formatted_results.append(
            {
                "rank": len(formatted_results) + 1,
                "title": title,
                "url": url,
                "raw_url": raw_url,
                "domain": _extract_domain(url),
                "snippet": "",
                "extra_snippets": [],
            }
        )

    answer = _extract_google_text(payload)
    text_lines = [answer] if answer else []
    if formatted_results:
        text_lines.append("")
        text_lines.append("Sources:")
        for item in formatted_results[:8]:
            text_lines.append(f"{item['rank']}. {item['title']} — {item['url']}")
    text = "\n".join(line for line in text_lines if line is not None).strip()
    if max_chars > 0:
        text = text[:max_chars]

    return {
        "provider": "google_grounded",
        "query": query,
        "answer": answer,
        "text": text,
        "results": formatted_results,
        "search_queries": [
            str(item).strip()
            for item in (metadata.get("webSearchQueries") or [])
            if str(item).strip()
        ],
        "total_results": len(formatted_results),
        "grounded": True,
    }


def build_tool_registry(
    *,
    content_store: VaultContentStore,
    document_service: DocumentService,
    document_editor: DocumentEditorService,
    memory: EpisodicMemory,
    ingest_service: KnowledgeIngestService,
    audio_service: AudioService,
    browser_service: BrowserService,
    spreadsheet_service: SpreadsheetService,
    workspace_service: WorkspaceService,
    skill_manager: SkillManager | None = None,
    capability_manager: CapabilityManager | None = None,
    todo_manager: TodoManager | None = None,
    subagent_runner: SubagentRunner | None = None,
    task_dag: TaskDAG | None = None,
    background_manager: BackgroundTaskManager | None = None,
    system_runner: SystemRunner | None = None,
    memory_manager: MemoryManager | None = None,
    audit_log: AuditLog | None = None,
    search_config: dict[str, Any] | None = None,
    allowlist: set[str] | None = None,
) -> list[ToolDefinition]:
    search_settings = search_config if isinstance(search_config, dict) else {}
    search_provider_settings = search_settings.setdefault("provider", {})
    google_grounded_settings = search_settings.setdefault("google_grounded", {})

    async def read_vault(relative_path: str) -> str:
        path = relative_path.strip()
        try:
            return content_store.read(path)
        except FileNotFoundError:
            raise FileNotFoundError(
                f"文件 '{path}' 不存在。"
                "不要猜测文件名——请先使用 find_vault_pages(query=关键词) "
                "或 list_vault_pages(section=目录名) 获取真实的文件路径。"
            ) from None

    async def search_vault(query: str, top_k: int = 5) -> str:
        return _json(await document_service.search(query, top_k=top_k))

    async def list_vault_pages(section: str = "", limit: int = 50) -> str:
        pages = document_service.list_page_summaries(section=section.strip().lstrip("/"), limit=limit)
        return _json(
            [
                {
                    "page_id": page.page_id,
                    "relative_path": page.relative_path,
                    "title": page.title,
                    "page_type": page.page_type,
                    "updated_at": page.updated_at.isoformat() if page.updated_at else None,
                }
                for page in pages
            ]
        )

    async def find_vault_pages(query: str, limit: int = 20) -> str:
        pages = document_service.find_pages(query.strip(), limit=limit)
        return _json(
            [
                {
                    "page_id": page.page_id,
                    "relative_path": page.relative_path,
                    "title": page.title,
                    "page_type": page.page_type,
                    "updated_at": page.updated_at.isoformat() if page.updated_at else None,
                }
                for page in pages
            ]
        )

    async def memory_search(query: str, limit: int = 5) -> str:
        if memory_manager is not None:
            results = await memory_manager.search(query, top_k=limit)
            return _json(results)
        return _json(await memory.recall(query, limit=limit))

    async def memory_write(
        summary: str,
        detail: str | None = None,
        kind: str = "fact",
        tags: list[str] | None = None,
        session_id: str | None = None,
        importance: int = 3,
    ) -> str:
        if memory_manager is not None:
            result = await memory_manager.save(
                summary=summary,
                detail=detail,
                kind=kind,
                tags=tags,
                importance=importance,
                session_id=session_id,
            )
            return _json(result)
        entry = await memory.record(
            kind=kind,
            summary=summary,
            detail=detail,
            tags=tags,
            session_id=session_id,
            importance=importance,
        )
        return _json({"entry_id": entry.entry_id, "kind": entry.kind, "summary": entry.summary})

    async def memory_suggest_evolution(task: str, min_occurrences: int = 3) -> str:
        if memory_manager is None:
            return _json({"suggestion": None, "reason": "memory_manager_unavailable"})
        suggestion = memory_manager.suggest_evolution_opportunity(
            task=task,
            min_occurrences=min_occurrences,
        )
        return _json({"suggestion": suggestion})

    async def knowledge_ingest(path: str = "", delta_only: bool = True) -> str:
        relative = path.strip().lstrip("/")
        if relative and content_store.resolve_path(relative).is_file():
            changed = await ingest_service.ingest_file(relative)
            return _json({"mode": "file", "path": relative, "changed": changed})
        return _json(ingest_service.ingest_directory(relative, delta_only=delta_only))

    async def audio_transcribe_path(
        audio_path: str,
        target_section: str = "meetings",
        title: str | None = None,
        materialize: bool = True,
        language: str | None = None,
        diarize: bool = False,
    ) -> str:
        if materialize:
            result = await audio_service.transcribe_and_materialize(
                audio_path=audio_path,
                target_section=target_section,
                title=title,
                language=language,
                diarize=diarize,
            )
            return _json(
                {
                    "mode": "materialized",
                    "transcript_path": result.transcript_path,
                    "page_path": result.page.relative_path,
                    "title": result.page.title,
                    "summary": result.summary,
                    "action_items": result.action_items,
                }
            )

        transcription = await asyncio.to_thread(
            audio_service.transcribe_file, Path(audio_path), language, diarize,
        )
        return _json(
            {
                "mode": "transcribed",
                "text": transcription.text,
                "language": transcription.language,
                "duration": transcription.duration,
                "segments": [
                    {
                        "start": segment.start,
                        "end": segment.end,
                        "text": segment.text,
                        "confidence": segment.confidence,
                        "speaker_id": segment.speaker_id,
                        "speaker_name": segment.speaker_name,
                    }
                    for segment in transcription.segments
                ],
            }
        )

    async def audio_materialize_transcript(
        source_name: str,
        transcript: str,
        summary: str = "",
        action_items: list[str] | None = None,
        target_section: str = "meetings",
        title: str | None = None,
    ) -> str:
        result = await audio_service.materialize_transcript(
            source_name=source_name,
            transcript=transcript,
            summary=summary,
            action_items=action_items or [],
            target_section=target_section,
            title=title,
        )
        return _json(
            {
                "transcript_path": result.transcript_path,
                "page_path": result.page.relative_path,
                "title": result.page.title,
                "summary": result.summary,
                "action_items": result.action_items,
            }
        )

    async def voiceprint_register(name: str, audio_path: str) -> str:
        """注册声纹到白名单。"""
        vp_store = getattr(audio_service, "_voiceprint_store", None)
        if vp_store is None:
            return _json({"error": "声纹服务未启用，请在配置中开启 diarization"})
        profile = await asyncio.to_thread(
            vp_store.register, name, Path(audio_path),
        )
        return _json({
            "status": "registered",
            "name": profile.name,
            "sample_count": profile.sample_count,
            "created_at": profile.created_at,
            "updated_at": profile.updated_at,
        })

    async def voiceprint_list() -> str:
        """列出已注册的声纹白名单。"""
        vp_store = getattr(audio_service, "_voiceprint_store", None)
        if vp_store is None:
            return _json({"error": "声纹服务未启用"})
        profiles = vp_store.list_profiles()
        return _json({
            "count": len(profiles),
            "profiles": [
                {"name": p.name, "sample_count": p.sample_count, "updated_at": p.updated_at}
                for p in profiles
            ],
        })

    async def voiceprint_delete(name: str) -> str:
        """从白名单中删除声纹。"""
        vp_store = getattr(audio_service, "_voiceprint_store", None)
        if vp_store is None:
            return _json({"error": "声纹服务未启用"})
        deleted = vp_store.delete(name)
        return _json({"deleted": deleted, "name": name})

    async def excel_list_sheets(excel_path: str) -> str:
        sheets = await asyncio.to_thread(spreadsheet_service.list_sheets, excel_path)
        return _json({"excel_path": excel_path, "sheets": sheets})

    async def excel_to_csv(
        excel_path: str,
        output_path: str | None = None,
        sheet_name: str | None = None,
        include_index: bool = False,
    ) -> str:
        target = await asyncio.to_thread(
            spreadsheet_service.excel_to_csv,
            excel_path,
            output_path=output_path,
            sheet_name=sheet_name,
            include_index=include_index,
        )
        return _json(
            {
                "excel_path": excel_path,
                "output_path": str(target),
                "sheet_name": sheet_name,
            }
        )

    async def create_note(
        title: str,
        body: str = "",
        section: str = "pages",
        page_type: str = "note",
    ) -> str:
        page = await document_service.create_page(
            title=title,
            body=body,
            section=section,
            page_type=page_type,
        )
        return _json(
            {
                "page_id": page.page_id,
                "relative_path": page.relative_path,
                "title": page.title,
                "page_type": page.page_type,
            }
        )

    async def write_vault(relative_path: str, content: str, title: str | None = None) -> str:
        page = await document_service.update_page(
            relative_path=relative_path.strip().lstrip("/"),
            content=content,
            title=title,
        )
        return _json({"relative_path": page.relative_path, "title": page.title})

    async def move_page(relative_path: str, new_relative_path: str) -> str:
        page = await document_service.move_page(
            relative_path=relative_path.strip().lstrip("/"),
            new_relative_path=new_relative_path.strip().lstrip("/"),
        )
        return _json(
            {
                "page_id": page.page_id,
                "old_path": relative_path,
                "new_path": page.relative_path,
                "title": page.title,
            }
        )

    async def delete_page(relative_path: str) -> str:
        page = await document_service.delete_page(
            relative_path=relative_path.strip().lstrip("/"),
        )
        return _json(
            {
                "page_id": page.page_id,
                "relative_path": page.relative_path,
                "title": page.title,
                "deleted": True,
            }
        )

    async def document_append_block(
        relative_path: str,
        block_markdown: str,
        heading: str | None = None,
        title: str | None = None,
    ) -> str:
        page = await document_editor.append_markdown_block(
            relative_path=relative_path.strip().lstrip("/"),
            block_markdown=block_markdown,
            heading=heading,
            title=title,
        )
        return _json({"relative_path": page.relative_path, "title": page.title, "mode": "append_block"})

    async def document_replace_section(
        relative_path: str,
        heading: str,
        body: str,
        level: int = 2,
        create_if_missing: bool = True,
        title: str | None = None,
    ) -> str:
        page = await document_editor.replace_section(
            relative_path=relative_path.strip().lstrip("/"),
            heading=heading,
            body=body,
            level=level,
            create_if_missing=create_if_missing,
            title=title,
        )
        return _json({"relative_path": page.relative_path, "title": page.title, "mode": "replace_section"})

    async def document_insert_checklist(
        relative_path: str,
        items: list[str],
        heading: str | None = None,
    ) -> str:
        page = await document_editor.insert_checklist(
            relative_path=relative_path.strip().lstrip("/"),
            items=items,
            heading=heading,
        )
        return _json({"relative_path": page.relative_path, "title": page.title, "mode": "checklist"})

    async def document_insert_table(
        relative_path: str,
        headers: list[str],
        rows: list[list[str]],
        heading: str | None = None,
    ) -> str:
        page = await document_editor.insert_table(
            relative_path=relative_path.strip().lstrip("/"),
            headers=headers,
            rows=rows,
            heading=heading,
        )
        return _json({"relative_path": page.relative_path, "title": page.title, "mode": "table"})

    async def document_insert_page_link(
        relative_path: str,
        target: str,
        label: str | None = None,
        heading: str | None = None,
    ) -> str:
        page = await document_editor.insert_page_link(
            relative_path=relative_path.strip().lstrip("/"),
            target=target,
            label=label,
            heading=heading,
        )
        return _json({"relative_path": page.relative_path, "title": page.title, "mode": "page_link"})

    async def document_create_database(
        title: str,
        section: str = "pages",
        owner_page: str | None = None,
        columns: list[dict[str, Any]] | None = None,
    ) -> str:
        normalized_columns = None
        if columns:
            normalized_columns = [
                CollectionColumn(
                    name=str(item.get("name") or "Column"),
                    column_type=str(item.get("column_type") or item.get("type") or "text"),
                    position=int(item.get("position") or idx),
                    config=dict(item.get("config") or {}),
                )
                for idx, item in enumerate(columns)
            ]
        result = await document_editor.create_database_page(
            title=title,
            section=section,
            owner_page=owner_page,
            columns=normalized_columns,
        )
        return _json(
            {
                "page_id": result.page.page_id,
                "relative_path": result.page.relative_path,
                "title": result.page.title,
                "collection_id": result.collection_id,
                "columns": [
                    {
                        "id": column.column_id,
                        "name": column.name,
                        "type": column.column_type,
                        "position": column.position,
                    }
                    for column in result.columns
                ],
            }
        )

    async def list_local_files(path: str = ".", pattern: str = "*", recursive: bool = False) -> str:
        requested = (path or ".").strip()
        normalized = requested.rstrip("/").replace("\\", "/")
        alias_candidate = Path(normalized).name if normalized not in {".", ""} else ""
        try:
            items = workspace_service.list_dir(requested, pattern=pattern, recursive=recursive)
            return _json([str(item) for item in items])
        except (FileNotFoundError, NotADirectoryError, ValueError) as exc:
            if alias_candidate in _VAULT_SECTION_ALIASES:
                pages = document_service.list_page_summaries(section=alias_candidate, limit=100)
                return _json(
                    {
                        "mode": "vault_section_alias",
                        "requested_path": requested,
                        "resolved_section": alias_candidate,
                        "note": (
                            "检测到你请求的是 Vault 分区路径；已返回该分区的页面清单。"
                            " 后续涉及页面盘点/同名页/删除页时，应优先使用 "
                            "list_vault_pages / find_vault_pages / delete_page。"
                        ),
                        "pages": [
                            {
                                "page_id": page.page_id,
                                "relative_path": page.relative_path,
                                "title": page.title,
                                "page_type": page.page_type,
                                "updated_at": page.updated_at.isoformat() if page.updated_at else None,
                            }
                            for page in pages
                        ],
                    }
                )
            raise ValueError(
                f"{exc}. 如果你要查询 Vault 中的页面，不要使用 list_local_files；"
                "请改用 list_vault_pages / find_vault_pages / read_vault / delete_page。"
            ) from exc

    async def code_read_file(path: str) -> str:
        return workspace_service.read_text(path)

    async def read_local_file(path: str) -> str:
        """兼容旧工具名，复用 code_read_file。"""
        return await code_read_file(path)

    async def browser_navigate(url: str) -> str:
        result = await browser_service.navigate(url)
        return _json(result)

    async def browser_extract_text(selector: str | None = None) -> str:
        result = await browser_service.extract_text(selector)
        return _json(result)

    async def browser_screenshot(path: str | None = None) -> str:
        result = await browser_service.screenshot(path)
        return _json(result)

    async def browser_fill_form(fields: dict[str, Any]) -> str:
        result = await browser_service.fill_form(fields)
        return _json(result)

    def _search_default_provider() -> str:
        provider = str(search_provider_settings.get("primary") or "google_grounded").strip().lower()
        return provider if provider in {"google_grounded", "bing", "duckduckgo"} else "google_grounded"

    def _search_fallback_providers() -> list[str]:
        raw_fallbacks = search_provider_settings.get("fallbacks") or []
        candidates = [
            str(item).strip().lower()
            for item in raw_fallbacks
            if str(item).strip().lower() in {"bing", "duckduckgo"}
        ]
        if not candidates:
            fallback = str(search_provider_settings.get("fallback") or "bing").strip().lower()
            candidates = [fallback] if fallback in {"bing", "duckduckgo"} else ["bing", "duckduckgo"]
        deduped: list[str] = []
        for item in candidates:
            if item not in deduped:
                deduped.append(item)
        if "duckduckgo" not in deduped:
            deduped.append("duckduckgo")
        return deduped

    def _google_grounded_runtime_config() -> dict[str, Any]:
        return {
            "enabled": bool(google_grounded_settings.get("enabled", True)),
            "api_key": str(google_grounded_settings.get("api_key") or ""),
            "base_url": str(
                google_grounded_settings.get("base_url")
                or "https://generativelanguage.googleapis.com/v1beta"
            ),
            "model": str(google_grounded_settings.get("model") or "gemini-2.5-flash"),
            "timeout_seconds": float(google_grounded_settings.get("timeout_seconds", 20) or 20),
            "max_output_tokens": int(google_grounded_settings.get("max_output_tokens", 768) or 768),
        }

    async def _browser_search(query: str, engine: str, max_chars: int) -> dict[str, Any]:
        normalized_engine = engine if engine in {"bing", "duckduckgo"} else "bing"
        if normalized_engine == "duckduckgo":
            url = f"https://duckduckgo.com/?q={quote_plus(query)}&ia=web"
        else:
            url = f"https://www.bing.com/search?q={quote_plus(query)}"
        page = await browser_service.navigate(url)
        extracted = await browser_service.extract_text()
        text = str(extracted.get("text") or "").strip()
        if max_chars > 0:
            text = text[:max_chars]
        return {
            "provider": normalized_engine,
            "query": query,
            "url": page.get("url"),
            "title": page.get("title"),
            "text": text,
            "results": [
                {
                    "rank": 1,
                    "title": str(page.get("title") or "").strip(),
                    "url": str(page.get("url") or "").strip(),
                    "domain": _extract_domain(str(page.get("url") or "")),
                    "snippet": text,
                    "extra_snippets": [],
                }
            ],
            "fallback_used": True,
        }

    async def _browser_search_with_fallbacks(
        *,
        query: str,
        engines: list[str],
        max_chars: int,
        fallback_reason: str,
        google_error: str | None = None,
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        for engine in engines:
            try:
                result = await _browser_search(query=query, engine=engine, max_chars=max_chars)
                result["fallback_reason"] = fallback_reason
                if google_error:
                    result["google_error"] = google_error
                return result
            except Exception as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        return await _browser_search(query=query, engine="bing", max_chars=max_chars)

    async def _structured_search(
        *,
        query: str,
        provider: str = "auto",
        count: int | None = None,
        freshness: str = "",
        country: str = "",
        search_lang: str = "",
        max_chars: int = 5000,
    ) -> dict[str, Any]:
        requested_provider = (provider or "auto").strip().lower()
        google_runtime = _google_grounded_runtime_config()
        selected_provider = requested_provider if requested_provider not in {"", "auto"} else _search_default_provider()

        if selected_provider == "google_grounded":
            google_key = google_runtime.get("api_key") or ""
            if google_runtime.get("enabled") and google_key:
                try:
                    payload = await _perform_google_grounded_search(
                        query=_build_google_grounded_prompt(query),
                        api_key=str(google_key),
                        base_url=str(google_runtime["base_url"]),
                        model=str(google_runtime["model"]),
                        timeout_seconds=float(google_runtime["timeout_seconds"]),
                        max_output_tokens=int(google_runtime["max_output_tokens"]),
                    )
                    result = await _format_google_grounded_payload(
                        query=query,
                        payload=payload,
                        max_chars=max_chars,
                    )
                    result["fallback_used"] = False
                    return result
                except Exception as exc:
                    fallback_reason = "google_quota_exhausted" if _is_google_quota_error(exc) else "google_grounded_failed"
                    return await _browser_search_with_fallbacks(
                        query=query,
                        engines=_search_fallback_providers(),
                        max_chars=max_chars,
                        fallback_reason=fallback_reason,
                        google_error=str(exc),
                    )
            selected_provider = _search_fallback_providers()[0]

        if selected_provider in {"bing", "duckduckgo"}:
            return await _browser_search(query=query, engine=selected_provider, max_chars=max_chars)

        result = await _browser_search_with_fallbacks(
            query=query,
            engines=_search_fallback_providers(),
            max_chars=max_chars,
            fallback_reason=(
            "google_grounded_not_configured"
            if requested_provider in {"auto", "google_grounded"}
            else "requested_browser_provider"
            ),
        )
        return result

    async def search_web(
        query: str,
        engine: str = "auto",
        max_chars: int = 3000,
    ) -> str:
        result = await _structured_search(
            query=query,
            provider=engine,
            max_chars=max_chars,
        )
        result["engine"] = result.get("provider")
        return _json(result)

    async def search_web_structured(
        query: str,
        provider: str = "auto",
        count: int = 8,
        freshness: str = "",
        country: str = "",
        search_lang: str = "",
        max_chars: int = 5000,
    ) -> str:
        result = await _structured_search(
            query=query,
            provider=provider,
            count=count,
            freshness=freshness,
            country=country,
            search_lang=search_lang,
            max_chars=max_chars,
        )
        return _json(result)

    tools = [
        ToolDefinition(
            name="read_vault",
            description="读取 Vault 中指定 Markdown 文件的内容。",
            parameters={
                "type": "object",
                "properties": {"relative_path": {"type": "string"}},
                "required": ["relative_path"],
            },
            handler=read_vault,
            risk_level=ToolRiskLevel.LOW,
            tags=["vault", "read"],
        ),
        ToolDefinition(
            name="search_vault",
            description="在知识库中搜索相关文档片段。",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "top_k": {"type": "integer", "default": 5},
                },
                "required": ["query"],
            },
            handler=search_vault,
            risk_level=ToolRiskLevel.LOW,
            tags=["vault", "search"],
        ),
        ToolDefinition(
            name="list_vault_pages",
            description="列出 Vault 中的 Markdown 页面，用于盘点文档、查看最近页面和确认页面路径。",
            parameters={
                "type": "object",
                "properties": {
                    "section": {"type": "string", "default": ""},
                    "limit": {"type": "integer", "default": 50},
                },
            },
            handler=list_vault_pages,
            risk_level=ToolRiskLevel.LOW,
            tags=["vault", "inventory"],
        ),
        ToolDefinition(
            name="find_vault_pages",
            description="按标题、相对路径或 page_id 查找 Vault 页面。适合同名页面排查、定位和去重。",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "default": 20},
                },
                "required": ["query"],
            },
            handler=find_vault_pages,
            risk_level=ToolRiskLevel.LOW,
            tags=["vault", "inventory", "search"],
        ),
        ToolDefinition(
            name="memory_search",
            description="语义搜索长期记忆。支持 FTS5 + 嵌入向量混合检索，自动应用时间衰减（近期记忆优先）。用于检索偏好、决策、项目状态和历史交互。",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索查询"},
                    "limit": {"type": "integer", "default": 5, "description": "返回条数上限"},
                },
                "required": ["query"],
            },
            handler=memory_search,
            risk_level=ToolRiskLevel.LOW,
            tags=["memory", "search"],
        ),
        ToolDefinition(
            name="memory_write",
            description="把重要偏好、决策或事实写入长期记忆。自动索引到语义检索系统，后续可通过 memory_search 语义召回。importance 1-5 (1=低, 5=关键决策)。",
            parameters={
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "记忆摘要（必填）"},
                    "detail": {"type": "string", "description": "详细内容"},
                    "kind": {
                        "type": "string",
                        "default": "fact",
                        "description": "类型: decision/preference/fact/project_state/context/workflow_success/workflow_failure/tool_pattern",
                    },
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "标签列表"},
                    "session_id": {"type": "string"},
                    "importance": {"type": "integer", "default": 3, "description": "重要度 1-5"},
                },
                "required": ["summary"],
            },
            handler=memory_write,
            risk_level=ToolRiskLevel.MEDIUM,
            tags=["memory", "write"],
        ),
        ToolDefinition(
            name="memory_suggest_evolution",
            description="检查某类任务是否已经形成重复成功模式，并给出 skill 演化建议。适合在任务收尾或做体系化沉淀时调用。",
            parameters={
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "任务描述"},
                    "min_occurrences": {"type": "integer", "default": 3},
                },
                "required": ["task"],
            },
            handler=memory_suggest_evolution,
            risk_level=ToolRiskLevel.LOW,
            tags=["memory", "evolution"],
        ),
        ToolDefinition(
            name="knowledge_ingest",
            description="重建或增量更新 Vault 的检索索引。",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "default": ""},
                    "delta_only": {"type": "boolean", "default": True},
                },
            },
            handler=knowledge_ingest,
            risk_level=ToolRiskLevel.MEDIUM,
            tags=["knowledge", "index"],
        ),
        ToolDefinition(
            name="audio_transcribe_path",
            description="转录本地音频文件；可选自动物化到 Vault 会议纪要/语音笔记页面。支持多说话人分离（diarize=true）。",
            parameters={
                "type": "object",
                "properties": {
                    "audio_path": {"type": "string"},
                    "target_section": {"type": "string", "default": "meetings"},
                    "title": {"type": "string"},
                    "materialize": {"type": "boolean", "default": True},
                    "language": {"type": "string"},
                    "diarize": {"type": "boolean", "default": False, "description": "启用说话人分离，自动识别不同发言人"},
                },
                "required": ["audio_path"],
            },
            handler=audio_transcribe_path,
            risk_level=ToolRiskLevel.MEDIUM,
            tags=["audio", "transcription", "vault", "diarization"],
        ),
        ToolDefinition(
            name="audio_materialize_transcript",
            description="将已有转录文本整理并保存到 Vault，生成可检索的会议纪要或语音笔记页面。",
            parameters={
                "type": "object",
                "properties": {
                    "source_name": {"type": "string"},
                    "transcript": {"type": "string"},
                    "summary": {"type": "string", "default": ""},
                    "action_items": {"type": "array", "items": {"type": "string"}},
                    "target_section": {"type": "string", "default": "meetings"},
                    "title": {"type": "string"},
                },
                "required": ["source_name", "transcript"],
            },
            handler=audio_materialize_transcript,
            risk_level=ToolRiskLevel.MEDIUM,
            tags=["audio", "transcription", "vault"],
        ),
        ToolDefinition(
            name="voiceprint_register",
            description="注册声纹到白名单：提供姓名和一段清晰语音（10-30秒），后续转录将自动识别该发言人。",
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "发言人显示名称"},
                    "audio_path": {"type": "string", "description": "音频样本路径（建议10-30秒清晰单人语音）"},
                },
                "required": ["name", "audio_path"],
            },
            handler=voiceprint_register,
            risk_level=ToolRiskLevel.LOW,
            tags=["audio", "voiceprint"],
        ),
        ToolDefinition(
            name="voiceprint_list",
            description="列出已注册的声纹白名单，显示所有已知发言人及采样次数。",
            parameters={"type": "object", "properties": {}},
            handler=voiceprint_list,
            risk_level=ToolRiskLevel.LOW,
            tags=["audio", "voiceprint"],
        ),
        ToolDefinition(
            name="voiceprint_delete",
            description="从声纹白名单中删除指定发言人。",
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "要删除的发言人名称"},
                },
                "required": ["name"],
            },
            handler=voiceprint_delete,
            risk_level=ToolRiskLevel.MEDIUM,
            tags=["audio", "voiceprint"],
        ),
        ToolDefinition(
            name="excel_list_sheets",
            description="列出 Excel 工作簿中的工作表名称。需要先启用 excel_processing capability。",
            parameters={
                "type": "object",
                "properties": {
                    "excel_path": {"type": "string", "description": "Excel 文件路径"},
                },
                "required": ["excel_path"],
            },
            handler=excel_list_sheets,
            risk_level=ToolRiskLevel.LOW,
            tags=["spreadsheet", "excel"],
        ),
        ToolDefinition(
            name="excel_to_csv",
            description="将 Excel 指定工作表转换为 CSV 文件。需要先启用 excel_processing capability。",
            parameters={
                "type": "object",
                "properties": {
                    "excel_path": {"type": "string", "description": "Excel 文件路径"},
                    "output_path": {"type": "string", "description": "输出 CSV 路径"},
                    "sheet_name": {"type": "string", "description": "工作表名称；留空则使用第一个工作表"},
                    "include_index": {"type": "boolean", "default": False},
                },
                "required": ["excel_path"],
            },
            handler=excel_to_csv,
            risk_level=ToolRiskLevel.LOW,
            tags=["spreadsheet", "excel", "csv"],
        ),
        ToolDefinition(
            name="create_note",
            description="在 Vault 中创建新的 Markdown 页面。",
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "body": {"type": "string", "default": ""},
                    "section": {"type": "string", "default": "pages"},
                    "page_type": {"type": "string", "default": "note"},
                },
                "required": ["title"],
            },
            handler=create_note,
            risk_level=ToolRiskLevel.MEDIUM,
            tags=["vault", "write"],
        ),
        ToolDefinition(
            name="write_vault",
            description="更新 Vault 中已有页面的内容。",
            parameters={
                "type": "object",
                "properties": {
                    "relative_path": {"type": "string"},
                    "content": {"type": "string"},
                    "title": {"type": "string"},
                },
                "required": ["relative_path", "content"],
            },
            handler=write_vault,
            risk_level=ToolRiskLevel.MEDIUM,
            tags=["vault", "write"],
        ),
        ToolDefinition(
            name="move_page",
            description="将 Vault 中的页面移动到新路径（可跨 section）。例如将 pages/meeting-notes.md 移动到 meetings/meeting-notes.md。",
            parameters={
                "type": "object",
                "properties": {
                    "relative_path": {
                        "type": "string",
                        "description": "当前页面的相对路径，如 pages/my-page.md",
                    },
                    "new_relative_path": {
                        "type": "string",
                        "description": "目标路径，如 meetings/my-page.md",
                    },
                },
                "required": ["relative_path", "new_relative_path"],
            },
            handler=move_page,
            risk_level=ToolRiskLevel.MEDIUM,
            tags=["vault", "write"],
        ),
        ToolDefinition(
            name="delete_page",
            description="删除 Vault 中指定页面。删除前会自动创建备份，适合同名页面清理和误创建页面回收。",
            parameters={
                "type": "object",
                "properties": {
                    "relative_path": {"type": "string"},
                },
                "required": ["relative_path"],
            },
            handler=delete_page,
            risk_level=ToolRiskLevel.MEDIUM,
            tags=["vault", "write", "delete"],
        ),
        ToolDefinition(
            name="document_append_block",
            description="以 Notion-style 方式向页面或指定 heading 追加 Markdown block。",
            parameters={
                "type": "object",
                "properties": {
                    "relative_path": {"type": "string"},
                    "block_markdown": {"type": "string"},
                    "heading": {"type": "string"},
                    "title": {"type": "string"},
                },
                "required": ["relative_path", "block_markdown"],
            },
            handler=document_append_block,
            risk_level=ToolRiskLevel.MEDIUM,
            tags=["document", "notion", "write"],
        ),
        ToolDefinition(
            name="document_replace_section",
            description="以结构化方式替换页面某个 section 的内容，可自动创建缺失 heading。",
            parameters={
                "type": "object",
                "properties": {
                    "relative_path": {"type": "string"},
                    "heading": {"type": "string"},
                    "body": {"type": "string"},
                    "level": {"type": "integer", "default": 2},
                    "create_if_missing": {"type": "boolean", "default": True},
                    "title": {"type": "string"},
                },
                "required": ["relative_path", "heading", "body"],
            },
            handler=document_replace_section,
            risk_level=ToolRiskLevel.MEDIUM,
            tags=["document", "notion", "write"],
        ),
        ToolDefinition(
            name="document_insert_checklist",
            description="向页面插入 Notion-style checklist block。",
            parameters={
                "type": "object",
                "properties": {
                    "relative_path": {"type": "string"},
                    "items": {"type": "array", "items": {"type": "string"}},
                    "heading": {"type": "string"},
                },
                "required": ["relative_path", "items"],
            },
            handler=document_insert_checklist,
            risk_level=ToolRiskLevel.MEDIUM,
            tags=["document", "notion", "checklist"],
        ),
        ToolDefinition(
            name="document_insert_table",
            description="向页面插入 Markdown table，作为 Notion-style 表格 block。",
            parameters={
                "type": "object",
                "properties": {
                    "relative_path": {"type": "string"},
                    "headers": {"type": "array", "items": {"type": "string"}},
                    "rows": {
                        "type": "array",
                        "items": {"type": "array", "items": {"type": "string"}},
                    },
                    "heading": {"type": "string"},
                },
                "required": ["relative_path", "headers", "rows"],
            },
            handler=document_insert_table,
            risk_level=ToolRiskLevel.MEDIUM,
            tags=["document", "notion", "table"],
        ),
        ToolDefinition(
            name="document_insert_page_link",
            description="向页面插入 page:// 引用链接，用于页面间关联。",
            parameters={
                "type": "object",
                "properties": {
                    "relative_path": {"type": "string"},
                    "target": {"type": "string"},
                    "label": {"type": "string"},
                    "heading": {"type": "string"},
                },
                "required": ["relative_path", "target"],
            },
            handler=document_insert_page_link,
            risk_level=ToolRiskLevel.MEDIUM,
            tags=["document", "notion", "link"],
        ),
        ToolDefinition(
            name="document_create_database",
            description="创建带 collection schema 的 Notion-style database 页面。",
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "section": {"type": "string", "default": "pages"},
                    "owner_page": {"type": "string"},
                    "columns": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "column_type": {"type": "string"},
                                "type": {"type": "string"},
                                "position": {"type": "integer"},
                            },
                            "required": ["name"],
                        },
                    },
                },
                "required": ["title"],
            },
            handler=document_create_database,
            risk_level=ToolRiskLevel.MEDIUM,
            tags=["document", "notion", "database"],
        ),
        ToolDefinition(
            name="list_local_files",
            description=(
                "列出工作区中的文件和目录。仅用于源码/工作区路径，不应用于 Vault 页面盘点。"
                " 如果传入 pages/journals/meetings/strategy/life/inbox/rnd 这类 Vault 分区名，"
                "会自动返回对应 Vault 分区的页面清单。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "default": "."},
                    "pattern": {"type": "string", "default": "*"},
                    "recursive": {"type": "boolean", "default": False},
                },
            },
            handler=list_local_files,
            risk_level=ToolRiskLevel.LOW,
            tags=["workspace", "read"],
        ),
        ToolDefinition(
            name="code_read_file",
            description="读取工作区中的代码或文本文件。",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            handler=code_read_file,
            risk_level=ToolRiskLevel.LOW,
            tags=["workspace", "read"],
        ),
        ToolDefinition(
            name="read_local_file",
            description="兼容旧工具名：读取工作区中的代码或文本文件。",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            handler=read_local_file,
            risk_level=ToolRiskLevel.LOW,
            tags=["workspace", "read", "compat"],
        ),
    ]

    if browser_service.enabled:
        tools.extend([
            ToolDefinition(
                name="browser_navigate",
                description="用浏览器 worker 打开指定网页并返回页面标题与 URL。",
                parameters={
                    "type": "object",
                    "properties": {"url": {"type": "string"}},
                    "required": ["url"],
                },
                handler=browser_navigate,
                risk_level=ToolRiskLevel.LOW,
                tags=["browser", "web"],
            ),
            ToolDefinition(
                name="browser_extract_text",
                description="提取当前网页正文文本；可选指定 CSS selector。",
                parameters={
                    "type": "object",
                    "properties": {"selector": {"type": "string"}},
                },
                handler=browser_extract_text,
                risk_level=ToolRiskLevel.LOW,
                tags=["browser", "web", "extract"],
            ),
            ToolDefinition(
                name="browser_screenshot",
                description="为当前网页截图；可选保存到指定路径。",
                parameters={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                },
                handler=browser_screenshot,
                risk_level=ToolRiskLevel.MEDIUM,
                tags=["browser", "web", "screenshot"],
            ),
            ToolDefinition(
                name="browser_fill_form",
                description="向当前网页表单填充字段，字段键为 CSS selector。",
                parameters={
                    "type": "object",
                    "properties": {
                        "fields": {
                            "type": "object",
                            "description": "selector -> value 映射，可选 __submit__ 指定提交按钮 selector",
                        }
                    },
                    "required": ["fields"],
                },
                handler=browser_fill_form,
                risk_level=ToolRiskLevel.MEDIUM,
                tags=["browser", "web", "form"],
            ),
            ToolDefinition(
                name="search_web",
                description="联网搜索工具。优先使用 Google grounded search；配额耗尽、未配置或失败时自动降级到 Bing 或 DuckDuckGo 网页搜索。",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "engine": {
                            "type": "string",
                            "enum": ["auto", "google_grounded", "bing", "duckduckgo"],
                            "default": "auto",
                        },
                        "max_chars": {"type": "integer", "default": 3000},
                    },
                    "required": ["query"],
                },
                handler=search_web,
                risk_level=ToolRiskLevel.LOW,
                tags=["browser", "web", "search"],
            ),
            ToolDefinition(
                name="search_web_structured",
                description="结构化联网搜索。优先走 Google grounded search，返回答案、搜索查询和来源 URL；若达到免费上限或失败则降级到浏览器搜索。",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "provider": {
                            "type": "string",
                            "enum": ["auto", "google_grounded", "bing", "duckduckgo"],
                            "default": "auto",
                        },
                        "count": {"type": "integer", "default": 8},
                        "freshness": {"type": "string", "default": ""},
                        "country": {"type": "string", "default": ""},
                        "search_lang": {"type": "string", "default": ""},
                        "max_chars": {"type": "integer", "default": 5000},
                    },
                    "required": ["query"],
                },
                handler=search_web_structured,
                risk_level=ToolRiskLevel.LOW,
                tags=["web", "search", "structured"],
            ),
        ])

    # ------------------------------------------------------------------
    # compact 工具 (Layer 3 手动压缩)
    # 注意: 实际压缩逻辑在 core.py 中拦截处理，此处 handler 仅作占位
    # ------------------------------------------------------------------
    async def _compact_placeholder(focus: str = "") -> str:
        return "上下文已压缩。"

    tools.append(ToolDefinition(
        name="compact",
        description="压缩当前对话上下文。当对话很长时主动调用，可指定需要保留的重点。",
        parameters={
            "type": "object",
            "properties": {
                "focus": {
                    "type": "string",
                    "description": "需要在压缩后保留的重点内容描述",
                },
            },
        },
        handler=_compact_placeholder,
        risk_level=ToolRiskLevel.LOW,
        tags=["agent", "context"],
    ))

    # ------------------------------------------------------------------
    # load_skill 工具 (Layer 2 按需加载)
    # ------------------------------------------------------------------
    if skill_manager is not None:
        async def load_skill(name: str) -> str:
            return skill_manager.get_skill_content(name)

        tools.append(ToolDefinition(
            name="load_skill",
            description="按名称加载专项技能的详细指令。遇到不熟悉的任务时先加载对应 skill。",
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "要加载的 skill 名称",
                    },
                },
                "required": ["name"],
            },
            handler=load_skill,
            risk_level=ToolRiskLevel.LOW,
            tags=["agent", "skill"],
        ))

    # ------------------------------------------------------------------
    # todo_write 工具 (进度追踪)
    # ------------------------------------------------------------------
    if todo_manager is not None:
        async def todo_write(items: list[dict[str, Any]]) -> str:
            return todo_manager.update(items)

        tools.append(ToolDefinition(
            name="todo_write",
            description="更新任务清单。用于追踪多步骤任务的进度。每个任务有 pending/in_progress/completed 三种状态，同时只能有一个 in_progress。",
            parameters={
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "content": {"type": "string", "description": "任务描述"},
                                "status": {
                                    "type": "string",
                                    "enum": ["pending", "in_progress", "completed"],
                                },
                                "activeForm": {
                                    "type": "string",
                                    "description": "进行中时的描述（如'正在编写测试'）",
                                },
                            },
                            "required": ["content", "status"],
                        },
                    },
                },
                "required": ["items"],
            },
            handler=todo_write,
            risk_level=ToolRiskLevel.LOW,
            tags=["agent", "planning"],
        ))

    # ------------------------------------------------------------------
    # dispatch_subagent 工具 (子任务委派)
    # ------------------------------------------------------------------
    if subagent_runner is not None:
        async def dispatch_subagent(
            prompt: str,
            description: str = "",
            spawn_mode: str = "run",
            session_id: str = "",
            _tool_context: dict[str, Any] | None = None,
        ) -> str:
            return await subagent_runner.dispatch(
                prompt=prompt,
                description=description,
                tools=tools,  # 传入当前工具列表（SubagentRunner 会过滤）
                spawn_mode=spawn_mode,
                session_id=session_id.strip() or None,
                parent_session_id=str((_tool_context or {}).get("session_id") or "") or None,
                parent_run_id=str((_tool_context or {}).get("run_id") or "") or None,
            )

        tools.append(ToolDefinition(
            name="dispatch_subagent",
            description="将子任务委派给独立的子Agent执行。子Agent拥有全新context，完成后只返回摘要。适用于独立性强、不需要主对话上下文的子任务。",
            parameters={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "子任务的详细指令",
                    },
                    "description": {
                        "type": "string",
                        "description": "子任务简短描述（用于日志）",
                    },
                    "spawn_mode": {
                        "type": "string",
                        "enum": ["run", "session"],
                        "description": "run=一次性执行后返回摘要；session=保留独立上下文并可通过 session_id 续跑。",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "当 spawn_mode=session 时，可选填写已有子代理 session_id 以继续该上下文。",
                    },
                },
                "required": ["prompt"],
            },
            handler=dispatch_subagent,
            risk_level=ToolRiskLevel.MEDIUM,
            tags=["agent", "subagent"],
        ))

    # ------------------------------------------------------------------
    # Task DAG 工具 (依赖编排)
    # ------------------------------------------------------------------
    if task_dag is not None:
        async def task_create(subject: str, description: str = "") -> str:
            return task_dag.create(subject=subject, description=description)

        async def task_update(
            task_id: int,
            status: str | None = None,
            add_blocked_by: list[int] | None = None,
            add_blocks: list[int] | None = None,
        ) -> str:
            return task_dag.update(
                task_id=task_id,
                status=status,
                add_blocked_by=add_blocked_by,
                add_blocks=add_blocks,
            )

        async def task_list() -> str:
            return task_dag.list_all()

        async def task_get(task_id: int) -> str:
            return task_dag.get(task_id=task_id)

        tools.extend([
            ToolDefinition(
                name="task_create",
                description="创建新任务并加入任务依赖图。",
                parameters={
                    "type": "object",
                    "properties": {
                        "subject": {"type": "string", "description": "任务标题"},
                        "description": {"type": "string", "description": "任务描述"},
                    },
                    "required": ["subject"],
                },
                handler=task_create,
                risk_level=ToolRiskLevel.LOW,
                tags=["agent", "task"],
            ),
            ToolDefinition(
                name="task_update",
                description="更新任务状态或依赖关系。完成任务时自动解除下游依赖。",
                parameters={
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "integer", "description": "任务ID"},
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed"],
                            "description": "新状态",
                        },
                        "add_blocked_by": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "description": "添加前置依赖任务ID列表",
                        },
                        "add_blocks": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "description": "添加后续依赖任务ID列表",
                        },
                    },
                    "required": ["task_id"],
                },
                handler=task_update,
                risk_level=ToolRiskLevel.LOW,
                tags=["agent", "task"],
            ),
            ToolDefinition(
                name="task_list",
                description="列出所有任务及其状态和依赖关系。",
                parameters={
                    "type": "object",
                    "properties": {},
                },
                handler=task_list,
                risk_level=ToolRiskLevel.LOW,
                tags=["agent", "task"],
            ),
            ToolDefinition(
                name="task_get",
                description="获取指定任务的详细信息。",
                parameters={
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "integer", "description": "任务ID"},
                    },
                    "required": ["task_id"],
                },
                handler=task_get,
                risk_level=ToolRiskLevel.LOW,
                tags=["agent", "task"],
            ),
        ])

    # ------------------------------------------------------------------
    # Background Task 工具 (异步后台执行)
    # ------------------------------------------------------------------
    if background_manager is not None:
        async def background_run(command: str) -> str:
            return await background_manager.submit(command)

        async def check_background(task_id: str | None = None) -> str:
            return background_manager.check(task_id=task_id)

        tools.extend([
            ToolDefinition(
                name="background_run",
                description="在后台异步执行 shell 命令，立即返回任务ID。适用于长时间运行的命令（编译、测试等），结果会在后续轮次自动推送。",
                parameters={
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "要在后台执行的 shell 命令",
                        },
                    },
                    "required": ["command"],
                },
                handler=background_run,
                risk_level=ToolRiskLevel.HIGH,
                tags=["agent", "background"],
            ),
            ToolDefinition(
                name="check_background",
                description="查询后台任务状态。不提供 task_id 则列出全部。",
                parameters={
                    "type": "object",
                    "properties": {
                        "task_id": {
                            "type": "string",
                            "description": "指定任务ID查询，留空则列出全部",
                        },
                    },
                },
                handler=check_background,
                risk_level=ToolRiskLevel.LOW,
                tags=["agent", "background"],
            ),
        ])

    # ------------------------------------------------------------------
    # 自进化工具 (Evolution)
    # Agent 发现能力缺口时可以创建/更新指令型 Skill
    # ------------------------------------------------------------------
    if capability_manager is not None:
        async def capability_list_available() -> str:
            return _json(capability_manager.list_capabilities())

        async def capability_status(capability_id: str) -> str:
            return _json(capability_manager.get_status(capability_id))

        async def capability_enable(capability_id: str) -> str:
            result = await capability_manager.enable(capability_id, actor="agent")
            return _json(
                {
                    "success": result.success,
                    "reason": result.reason,
                    "capability_id": capability_id,
                }
            )

        async def capability_create(
            capability_id: str,
            name: str,
            description: str,
            packages: list[str],
            imports: list[str],
            tools: list[str] | None = None,
            skill_hint: str = "",
        ) -> str:
            result = capability_manager.create(
                capability_id=capability_id,
                name=name,
                description=description,
                packages=packages,
                imports=imports,
                tools=tools or [],
                skill_hint=skill_hint,
                actor="agent",
            )
            return _json(
                {
                    "success": result.success,
                    "reason": result.reason,
                    "capability_id": capability_id,
                }
            )

        async def capability_register(
            capability_id: str,
            name: str,
            description: str,
            packages: list[str],
            imports: list[str],
            tools: list[str] | None = None,
            skill_hint: str = "",
            auto_promote: bool = True,
        ) -> str:
            result = capability_manager.register(
                capability_id=capability_id,
                name=name,
                description=description,
                packages=packages,
                imports=imports,
                tools=tools or [],
                skill_hint=skill_hint,
                actor="agent",
                auto_promote=auto_promote,
            )
            return _json(result)

        async def capability_stage(capability_id: str) -> str:
            result = capability_manager.stage(capability_id, actor="agent")
            return _json(
                {
                    "success": result.success,
                    "reason": result.reason,
                    "capability_id": capability_id,
                }
            )

        async def capability_verify(capability_id: str, staged: bool = True) -> str:
            result = capability_manager.verify(capability_id, staged=staged)
            return _json(
                {
                    "passed": result.passed,
                    "summary": result.summary,
                    "checks": [
                        {
                            "name": check.name,
                            "passed": check.passed,
                            "message": check.message,
                            "details": check.details,
                        }
                        for check in result.checks
                    ],
                    "capability_id": capability_id,
                    "staged": staged,
                }
            )

        async def capability_promote(capability_id: str) -> str:
            result = capability_manager.promote(capability_id, actor="agent")
            return _json(
                {
                    "success": result.success,
                    "reason": result.reason,
                    "backup_id": result.backup_id,
                    "capability_id": capability_id,
                }
            )

        async def capability_rollback(capability_id: str) -> str:
            result = capability_manager.rollback(capability_id, actor="agent")
            return _json(
                {
                    "success": result.success,
                    "reason": result.reason,
                    "backup_id": result.backup_id,
                    "capability_id": capability_id,
                }
            )

        tools.extend([
            ToolDefinition(
                name="capability_list_available",
                description="列出当前可安装的受控 runtime capabilities。",
                parameters={"type": "object", "properties": {}},
                handler=capability_list_available,
                risk_level=ToolRiskLevel.LOW,
                tags=["evolution", "capability"],
            ),
            ToolDefinition(
                name="capability_status",
                description="查询某个 capability 是否已启用，以及启用后会提供哪些工具。",
                parameters={
                    "type": "object",
                    "properties": {
                        "capability_id": {"type": "string", "description": "能力ID"},
                    },
                    "required": ["capability_id"],
                },
                handler=capability_status,
                risk_level=ToolRiskLevel.LOW,
                tags=["evolution", "capability"],
            ),
            ToolDefinition(
                name="capability_enable",
                description="启用一个受控 capability。只允许安装白名单能力，不接受任意 package。",
                parameters={
                    "type": "object",
                    "properties": {
                        "capability_id": {"type": "string", "description": "能力ID"},
                    },
                    "required": ["capability_id"],
                },
                handler=capability_enable,
                risk_level=ToolRiskLevel.MEDIUM,
                tags=["evolution", "capability"],
            ),
            ToolDefinition(
                name="capability_create",
                description="创建一个新的 staged capability manifest。不会直接生效，后续需要 verify 和 promote。",
                parameters={
                    "type": "object",
                    "properties": {
                        "capability_id": {"type": "string", "description": "新能力ID（snake_case）"},
                        "name": {"type": "string", "description": "能力名称"},
                        "description": {"type": "string", "description": "能力描述"},
                        "packages": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "运行该能力所需安装包",
                        },
                        "imports": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "启用后应能 import 的模块名",
                        },
                        "tools": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "该能力暴露的稳定工具名",
                        },
                        "skill_hint": {"type": "string", "description": "关联 skill id（可选）"},
                    },
                    "required": ["capability_id", "name", "description", "packages", "imports"],
                },
                handler=capability_create,
                risk_level=ToolRiskLevel.MEDIUM,
                tags=["evolution", "capability"],
            ),
            ToolDefinition(
                name="capability_register",
                description="创建并注册一个正式 capability。会先创建 staged manifest，再验证；默认验证通过后自动 promote。",
                parameters={
                    "type": "object",
                    "properties": {
                        "capability_id": {"type": "string", "description": "新能力ID（snake_case）"},
                        "name": {"type": "string", "description": "能力名称"},
                        "description": {"type": "string", "description": "能力描述"},
                        "packages": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "运行该能力所需安装包",
                        },
                        "imports": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "启用后应能 import 的模块名",
                        },
                        "tools": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "该能力暴露的稳定工具名",
                        },
                        "skill_hint": {"type": "string", "description": "关联 skill id（可选）"},
                        "auto_promote": {"type": "boolean", "description": "默认验证通过后自动 promote"},
                    },
                    "required": ["capability_id", "name", "description", "packages", "imports"],
                },
                handler=capability_register,
                risk_level=ToolRiskLevel.MEDIUM,
                tags=["evolution", "capability"],
            ),
            ToolDefinition(
                name="capability_stage",
                description="将一个已有 capability 复制到 staging，准备验证或修改。",
                parameters={
                    "type": "object",
                    "properties": {
                        "capability_id": {"type": "string", "description": "能力ID"},
                    },
                    "required": ["capability_id"],
                },
                handler=capability_stage,
                risk_level=ToolRiskLevel.MEDIUM,
                tags=["evolution", "capability"],
            ),
            ToolDefinition(
                name="capability_verify",
                description="验证 staged 或 active capability manifest 是否满足注册条件。",
                parameters={
                    "type": "object",
                    "properties": {
                        "capability_id": {"type": "string", "description": "能力ID"},
                        "staged": {"type": "boolean", "description": "默认验证 staged 版本"},
                    },
                    "required": ["capability_id"],
                },
                handler=capability_verify,
                risk_level=ToolRiskLevel.LOW,
                tags=["evolution", "capability"],
            ),
            ToolDefinition(
                name="capability_promote",
                description="将通过验证的 staged capability 提升为正式 capability，并写入审计。",
                parameters={
                    "type": "object",
                    "properties": {
                        "capability_id": {"type": "string", "description": "能力ID"},
                    },
                    "required": ["capability_id"],
                },
                handler=capability_promote,
                risk_level=ToolRiskLevel.MEDIUM,
                tags=["evolution", "capability"],
            ),
            ToolDefinition(
                name="capability_rollback",
                description="将 capability 回滚到最近一次备份，或删除最近新建的正式 capability。",
                parameters={
                    "type": "object",
                    "properties": {
                        "capability_id": {"type": "string", "description": "能力ID"},
                    },
                    "required": ["capability_id"],
                },
                handler=capability_rollback,
                risk_level=ToolRiskLevel.MEDIUM,
                tags=["evolution", "capability"],
            ),
        ])

    if skill_manager is not None:
        async def skill_list_installable(query: str = "") -> str:
            return _json(skill_manager.list_installable_skills(query=query))

        async def skill_install(skill_id: str) -> str:
            return _json(await skill_manager.install_from_catalog(skill_id, actor="agent"))

        async def skill_import_local(
            path: str,
            install: bool = True,
            skill_id: str | None = None,
        ) -> str:
            return _json(
                await skill_manager.import_from_local(
                    path,
                    actor="agent",
                    install=install,
                    skill_id=skill_id,
                )
            )

        async def skill_import_remote(
            repo: str,
            path: str,
            ref: str = "main",
            install: bool = True,
            skill_id: str | None = None,
        ) -> str:
            return _json(
                await skill_manager.import_from_remote(
                    repo,
                    path,
                    ref=ref,
                    actor="agent",
                    install=install,
                    skill_id=skill_id,
                )
            )

        async def skill_search_remote(
            query: str,
            repo: str | None = None,
            ref: str | None = None,
            limit: int = 10,
        ) -> str:
            return _json(
                await skill_manager.search_remote_skills(
                    query,
                    repo=repo,
                    ref=ref,
                    limit=limit,
                )
            )

        async def skill_search_clawhub(
            query: str,
            limit: int = 10,
            base_url: str | None = None,
        ) -> str:
            return _json(
                await skill_manager.search_clawhub_skills(
                    query,
                    limit=limit,
                    base_url=base_url,
                )
            )

        async def skill_import_clawhub(
            slug: str,
            version: str | None = None,
            install: bool = True,
            skill_id: str | None = None,
            base_url: str | None = None,
        ) -> str:
            return _json(
                await skill_manager.import_from_clawhub(
                    slug,
                    version=version,
                    actor="agent",
                    install=install,
                    skill_id=skill_id,
                    base_url=base_url,
                )
            )

        async def skill_create(
            skill_id: str,
            name: str,
            description: str,
            body: str,
            tags: str = "",
        ) -> str:
            result = skill_manager.create_skill(
                skill_id=skill_id,
                name=name,
                description=description,
                body=body,
                tags=tags,
            )
            return _json({"success": result.success, "reason": result.reason})

        async def skill_update(
            skill_id: str,
            body: str | None = None,
            description: str | None = None,
            tags: str | None = None,
        ) -> str:
            result = skill_manager.update_skill(
                skill_id=skill_id,
                body=body,
                description=description,
                tags=tags,
            )
            return _json({
                "success": result.success,
                "reason": result.reason,
                "backup_id": result.backup_id,
            })

        async def skill_list_installed() -> str:
            return _json(skill_manager.list_skills())

        tools.extend([
            ToolDefinition(
                name="skill_list_installable",
                description=(
                    "列出可安装的受管 Skill 扩展包。遇到当前任务缺少现成能力时，先用它根据任务描述搜索匹配扩展。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "当前任务或能力缺口的简短描述，用于匹配 installable skills",
                        },
                    },
                },
                handler=skill_list_installable,
                risk_level=ToolRiskLevel.LOW,
                tags=["evolution", "skill", "registry"],
            ),
            ToolDefinition(
                name="skill_install",
                description=(
                    "从 installable skill registry 安装一个受管 Skill。安装成功后它会成为正式运行时对象，可在后续会话继续复用。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "skill_id": {
                            "type": "string",
                            "description": "installable skill 的 ID",
                        },
                    },
                    "required": ["skill_id"],
                },
                handler=skill_install,
                risk_level=ToolRiskLevel.MEDIUM,
                tags=["evolution", "skill", "install"],
            ),
            ToolDefinition(
                name="skill_import_local",
                description=(
                    "从本地目录或 SKILL.md 文件导入一个 skill bundle 到受管 registry。可选地立即安装为正式 Skill。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "本地 skill 目录路径，或该 skill 的 SKILL.md 文件路径",
                        },
                        "install": {
                            "type": "boolean",
                            "description": "导入后是否立即安装到正式 skills 目录",
                        },
                        "skill_id": {
                            "type": "string",
                            "description": "可选：覆盖导入后的 skill_id（默认取 frontmatter.id 或目录名）",
                        },
                    },
                    "required": ["path"],
                },
                handler=skill_import_local,
                risk_level=ToolRiskLevel.MEDIUM,
                tags=["evolution", "skill", "import"],
            ),
            ToolDefinition(
                name="skill_import_remote",
                description=(
                    "从 GitHub 仓库导入一个远程 skill bundle 到受管 registry。可选地立即安装为正式 Skill。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "repo": {
                            "type": "string",
                            "description": "GitHub 仓库，格式 owner/repo",
                        },
                        "path": {
                            "type": "string",
                            "description": "仓库内 skill 目录路径，例如 skills/iso-13485-certification",
                        },
                        "ref": {
                            "type": "string",
                            "description": "可选：分支、tag 或 commit，默认 main",
                        },
                        "install": {
                            "type": "boolean",
                            "description": "导入后是否立即安装到正式 skills 目录",
                        },
                        "skill_id": {
                            "type": "string",
                            "description": "可选：覆盖导入后的 skill_id（默认取 frontmatter.id 或目录名）",
                        },
                    },
                    "required": ["repo", "path"],
                },
                handler=skill_import_remote,
                risk_level=ToolRiskLevel.MEDIUM,
                tags=["evolution", "skill", "import", "remote"],
            ),
            ToolDefinition(
                name="skill_search_remote",
                description=(
                    "从配置好的远程 GitHub skill 源搜索匹配的 Skill，返回 repo/path/ref 候选，"
                    "便于后续调用 skill_import_remote 导入。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "当前任务或能力缺口的描述，用于匹配远程 skill",
                        },
                        "repo": {
                            "type": "string",
                            "description": "可选：仅搜索指定 GitHub 仓库，格式 owner/repo",
                        },
                        "ref": {
                            "type": "string",
                            "description": "可选：指定远程仓库分支、tag 或 commit",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "返回候选数量上限，默认 10，最大 50",
                        },
                    },
                    "required": ["query"],
                },
                handler=skill_search_remote,
                risk_level=ToolRiskLevel.LOW,
                tags=["evolution", "skill", "search", "remote"],
            ),
            ToolDefinition(
                name="skill_search_clawhub",
                description=(
                    "从 ClawHub registry 搜索匹配的 Skill，返回 slug/version 等候选，"
                    "便于后续调用 skill_import_clawhub 导入。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "当前任务或能力缺口的描述，用于匹配 ClawHub skill",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "返回候选数量上限，默认 10，最大 50",
                        },
                        "base_url": {
                            "type": "string",
                            "description": "可选：覆盖默认 ClawHub registry 地址",
                        },
                    },
                    "required": ["query"],
                },
                handler=skill_search_clawhub,
                risk_level=ToolRiskLevel.LOW,
                tags=["evolution", "skill", "search", "registry", "clawhub"],
            ),
            ToolDefinition(
                name="skill_import_clawhub",
                description=(
                    "从 ClawHub registry 下载并导入一个远程 skill bundle 到受管 registry。"
                    "可选地立即安装为正式 Skill。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "slug": {
                            "type": "string",
                            "description": "ClawHub skill slug",
                        },
                        "version": {
                            "type": "string",
                            "description": "可选：指定要导入的版本",
                        },
                        "install": {
                            "type": "boolean",
                            "description": "导入后是否立即安装到正式 skills 目录",
                        },
                        "skill_id": {
                            "type": "string",
                            "description": "可选：覆盖导入后的 skill_id（默认取 frontmatter.id 或 slug）",
                        },
                        "base_url": {
                            "type": "string",
                            "description": "可选：覆盖默认 ClawHub registry 地址",
                        },
                    },
                    "required": ["slug"],
                },
                handler=skill_import_clawhub,
                risk_level=ToolRiskLevel.MEDIUM,
                tags=["evolution", "skill", "import", "registry", "clawhub"],
            ),
            ToolDefinition(
                name="skill_create",
                description=(
                    "创建新的指令型 Skill。当你发现自己缺少某种能力时，"
                    "应主动创建 Skill 教会自己如何利用已有工具解决问题。"
                    "下次遇到相同问题时会自动加载此 Skill。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "skill_id": {
                            "type": "string",
                            "description": "技能ID（kebab-case，如 excel-processing）",
                        },
                        "name": {
                            "type": "string",
                            "description": "技能名称",
                        },
                        "description": {
                            "type": "string",
                            "description": "简短描述（会出现在 system prompt 中）",
                        },
                        "body": {
                            "type": "string",
                            "description": "完整的 Markdown 指令正文，教会自己如何处理此类任务",
                        },
                        "tags": {
                            "type": "string",
                            "description": "逗号分隔的标签",
                        },
                    },
                    "required": ["skill_id", "name", "description", "body"],
                },
                handler=skill_create,
                risk_level=ToolRiskLevel.MEDIUM,
                tags=["evolution", "skill"],
            ),
            ToolDefinition(
                name="skill_update",
                description="更新已有 Skill 的内容。自动备份旧版本。",
                parameters={
                    "type": "object",
                    "properties": {
                        "skill_id": {
                            "type": "string",
                            "description": "要更新的技能ID",
                        },
                        "body": {
                            "type": "string",
                            "description": "新的正文内容",
                        },
                        "description": {
                            "type": "string",
                            "description": "新的描述",
                        },
                        "tags": {
                            "type": "string",
                            "description": "新的标签",
                        },
                    },
                    "required": ["skill_id"],
                },
                handler=skill_update,
                risk_level=ToolRiskLevel.MEDIUM,
                tags=["evolution", "skill"],
            ),
            ToolDefinition(
                name="skill_list_installed",
                description="列出所有已安装的正式 Skill 运行时对象，返回结构化清单。",
                parameters={
                    "type": "object",
                    "properties": {},
                },
                handler=skill_list_installed,
                risk_level=ToolRiskLevel.LOW,
                tags=["evolution", "skill"],
            ),
        ])

    if audit_log is not None:
        async def evolution_audit(
            action: str | None = None,
            limit: int = 10,
        ) -> str:
            entries = audit_log.query(action=action, limit=limit)
            if not entries:
                return "无进化记录。"
            lines: list[str] = []
            for e in entries:
                status = "✓" if e.success else "✗"
                ts = e.timestamp.strftime("%m-%d %H:%M")
                lines.append(
                    f"{status} [{ts}] {e.action} → {e.target}"
                    f" (by {e.actor})"
                )
                if e.error:
                    lines.append(f"  error: {e.error}")
            return "\n".join(lines)

        tools.append(ToolDefinition(
            name="evolution_audit",
            description="查询自进化审计日志，了解最近的 Skill 安装/创建/配置变更记录。",
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "按操作类型过滤（如 skill_created, skill_installed）",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 10,
                        "description": "返回条数上限",
                    },
                },
            },
            handler=evolution_audit,
            risk_level=ToolRiskLevel.LOW,
            tags=["evolution", "audit"],
        ))

    # ------------------------------------------------------------------
    # 记忆增强工具 (MemoryManager)
    # ------------------------------------------------------------------
    if memory_manager is not None:
        async def memory_read_identity() -> str:
            """读取 Agent 身份(SOUL.md) + 用户画像(USER.md)"""
            soul = memory_manager.read_soul()
            user = memory_manager.read_user_profile()
            parts: list[str] = []
            if soul:
                parts.append(f"=== SOUL.md ===\n{soul}")
            else:
                parts.append("=== SOUL.md ===\n（尚未创建）")
            if user:
                parts.append(f"=== USER.md ===\n{user}")
            else:
                parts.append("=== USER.md ===\n（尚未创建）")
            return "\n\n".join(parts)

        async def memory_update_user(section: str, content: str) -> str:
            """更新用户画像的某个维度"""
            return await memory_manager.update_user_profile(section, content)

        async def memory_update_soul(content: str) -> str:
            """更新 Agent 身份描述"""
            return await memory_manager.update_soul(content)

        async def memory_daily_log(content: str, date: str | None = None) -> str:
            """追加内容到当天（或指定日期）的记忆日志"""
            return await memory_manager.append_daily_journal(content, date=date)

        async def memory_read_journal(date: str | None = None) -> str:
            """读取指定日期的记忆日志"""
            result = memory_manager.read_daily_journal(date)
            if not result:
                return f"日期 {date or '今天'} 无记忆日志。"
            return result

        async def memory_list_journals(limit: int = 30) -> str:
            """列出最近的记忆日志"""
            return _json(memory_manager.list_journals(limit))

        async def memory_reindex() -> str:
            """重建记忆、身份、日志与 Vault 文档的检索索引"""
            result = await memory_manager.reindex_all_retrieval_sources()
            return _json(result)

        async def memory_sync() -> str:
            """按 manifest/hash 增量同步所有记忆相关检索源"""
            result = await memory_manager.sync_retrieval_sources(delta_only=True, include_vault=True)
            return _json(result)

        tools.extend([
            ToolDefinition(
                name="memory_read_identity",
                description="读取 Agent 身份(SOUL.md) 和用户画像(USER.md)。用于了解自己的身份设定和已积累的用户偏好。",
                parameters={"type": "object", "properties": {}},
                handler=memory_read_identity,
                risk_level=ToolRiskLevel.LOW,
                tags=["memory", "identity"],
            ),
            ToolDefinition(
                name="memory_update_user",
                description="更新用户画像 USER.md 的某个维度。当观察到用户的新偏好或习惯时主动调用。例如 section='工作风格', content='偏好简洁直接的沟通'。",
                parameters={
                    "type": "object",
                    "properties": {
                        "section": {
                            "type": "string",
                            "description": "画像维度（如：工作风格、技术偏好、沟通习惯、常用工具）",
                        },
                        "content": {
                            "type": "string",
                            "description": "该维度的描述内容",
                        },
                    },
                    "required": ["section", "content"],
                },
                handler=memory_update_user,
                risk_level=ToolRiskLevel.MEDIUM,
                tags=["memory", "identity", "write"],
            ),
            ToolDefinition(
                name="memory_update_soul",
                description="更新 Agent 身份描述 SOUL.md。用于持久化 Agent 的人格特征、价值观和行为准则。",
                parameters={
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "完整的 SOUL.md 内容（Markdown 格式）",
                        },
                    },
                    "required": ["content"],
                },
                handler=memory_update_soul,
                risk_level=ToolRiskLevel.MEDIUM,
                tags=["memory", "identity", "write"],
            ),
            ToolDefinition(
                name="memory_daily_log",
                description="追加内容到记忆日志。用于记录当天的重要事件、交互摘要、决策记录。每天自动归档为独立文件。",
                parameters={
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "要追加的日志内容（Markdown 格式）",
                        },
                        "date": {
                            "type": "string",
                            "description": "日期（YYYY-MM-DD），默认今天",
                        },
                    },
                    "required": ["content"],
                },
                handler=memory_daily_log,
                risk_level=ToolRiskLevel.MEDIUM,
                tags=["memory", "journal", "write"],
            ),
            ToolDefinition(
                name="memory_read_journal",
                description="读取指定日期的记忆日志。不指定日期则读取今天的。",
                parameters={
                    "type": "object",
                    "properties": {
                        "date": {
                            "type": "string",
                            "description": "日期（YYYY-MM-DD），默认今天",
                        },
                    },
                },
                handler=memory_read_journal,
                risk_level=ToolRiskLevel.LOW,
                tags=["memory", "journal"],
            ),
            ToolDefinition(
                name="memory_list_journals",
                description="列出最近的记忆日志文件。",
                parameters={
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "default": 30},
                    },
                },
                handler=memory_list_journals,
                risk_level=ToolRiskLevel.LOW,
                tags=["memory", "journal"],
            ),
            ToolDefinition(
                name="memory_reindex",
                description="重建记忆、身份、日志与 Vault 文档的检索索引。当记忆搜索质量不佳或索引明显失真时使用。",
                parameters={"type": "object", "properties": {}},
                handler=memory_reindex,
                risk_level=ToolRiskLevel.MEDIUM,
                tags=["memory", "index"],
            ),
            ToolDefinition(
                name="memory_sync",
                description="按 hash/manifest 增量同步记忆相关索引。适合在对话前或批量写入文档后刷新 Memory/RAG。",
                parameters={"type": "object", "properties": {}},
                handler=memory_sync,
                risk_level=ToolRiskLevel.MEDIUM,
                tags=["memory", "sync"],
            ),
        ])

    # ── system_run: 受控 shell 执行 ──
    if system_runner is not None:
        async def system_run_exec(
            command: str,
            workdir: str | None = None,
            timeout: int | None = None,
        ) -> str:
            result = await system_runner.run(
                command,
                workdir=workdir,
                timeout=timeout,
                actor="agent",
            )
            return _json(result)

        tools.append(ToolDefinition(
            name="system_run",
            description=(
                "执行 shell 命令（pip install、python 脚本、curl、git 等）。"
                "可用于安装依赖、执行脚本、配置集成、下载文件等自进化操作。"
                "所有执行均有审计记录。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "要执行的 shell 命令",
                    },
                    "workdir": {
                        "type": "string",
                        "description": "工作目录（默认项目根目录）",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "超时秒数（默认 600s，设 0 表示不限）",
                    },
                },
                "required": ["command"],
            },
            handler=system_run_exec,
            risk_level=ToolRiskLevel.MEDIUM,
            tags=["evolution", "system"],
        ))

    # ── BMAD 项目管理工具 ──

    _PROJECT_BASE = "projects"

    _PROJECT_DIRS = [
        "planning",
        "implementation",
        "implementation/stories",
    ]

    _ARTIFACT_CHAIN = {
        "product-brief": {"path": "planning/product-brief.md", "depends": [], "phase": "Analysis"},
        "prd": {"path": "planning/prd.md", "depends": ["product-brief"], "phase": "Planning"},
        "ux-spec": {"path": "planning/ux-spec.md", "depends": ["prd"], "phase": "Planning"},
        "architecture": {"path": "planning/architecture.md", "depends": ["prd"], "phase": "Solutioning"},
        "epics": {"path": "implementation/epics.md", "depends": ["prd", "architecture"], "phase": "Solutioning"},
        "project-context": {"path": "project-context.md", "depends": ["architecture"], "phase": "Solutioning"},
        "sprint-status": {"path": "implementation/sprint-status.yaml", "depends": ["epics"], "phase": "Implementation"},
    }

    async def project_init(name: str) -> str:
        """初始化 BMAD 项目目录结构"""
        slug = name.strip().lower().replace(" ", "-")
        base = f"{_PROJECT_BASE}/{slug}"
        created = []
        for d in _PROJECT_DIRS:
            dir_path = f"{base}/{d}"
            try:
                await document_service.update_page(
                    relative_path=f"{dir_path}/.gitkeep",
                    content="",
                    title=None,
                )
                created.append(dir_path)
            except Exception:
                pass
        # 创建 project-context.md 骨架
        ctx_path = f"{base}/project-context.md"
        ctx_content = f"# {name} — Project Context\n\n## 技术栈\n\n（待填写）\n\n## 编码规范\n\n（待填写）\n\n## 实施规则\n\n（待填写）\n"
        await document_service.update_page(
            relative_path=ctx_path,
            content=ctx_content,
            title=f"{name} — Project Context",
        )
        return _json({
            "project": slug,
            "base_path": base,
            "created_dirs": created,
            "next_step": "使用 bmad-pm (load_skill bmad-pm) 创建 PRD，或使用 bmad-analyst 做需求分析",
        })

    async def project_status(name: str) -> str:
        """检查 BMAD 项目的产出物完成状态"""
        slug = name.strip().lower().replace(" ", "-")
        base = f"{_PROJECT_BASE}/{slug}"
        results = []
        completed_phases: set[str] = set()
        for artifact, spec in _ARTIFACT_CHAIN.items():
            path = f"{base}/{spec['path']}"
            try:
                content = content_store.read(path)
                exists = bool(content and content.strip())
            except Exception:
                exists = False
            deps_met = all(
                any(r["artifact"] == d and r["exists"] for r in results)
                for d in spec["depends"]
            ) if spec["depends"] else True
            results.append({
                "artifact": artifact,
                "path": path,
                "exists": exists,
                "deps_met": deps_met,
                "phase": spec["phase"],
            })
            if exists:
                completed_phases.add(spec["phase"])

        # 推断当前阶段
        all_phases = ["Analysis", "Planning", "Solutioning", "Implementation"]
        current_phase = "Analysis"
        for phase in all_phases:
            phase_artifacts = [r for r in results if r["phase"] == phase]
            if all(r["exists"] for r in phase_artifacts):
                idx = all_phases.index(phase)
                if idx + 1 < len(all_phases):
                    current_phase = all_phases[idx + 1]
            else:
                current_phase = phase
                break

        return _json({
            "project": slug,
            "current_phase": current_phase,
            "artifacts": results,
            "completed_phases": sorted(completed_phases),
        })

    async def project_next(name: str) -> str:
        """推荐 BMAD 项目的下一步操作"""
        slug = name.strip().lower().replace(" ", "-")
        base = f"{_PROJECT_BASE}/{slug}"
        # 按产出物链顺序找第一个缺失的
        for artifact, spec in _ARTIFACT_CHAIN.items():
            path = f"{base}/{spec['path']}"
            try:
                content = content_store.read(path)
                exists = bool(content and content.strip())
            except Exception:
                exists = False
            if not exists:
                skill_map = {
                    "product-brief": ("bmad-pm", "CP (创建 Product Brief) 或使用 bmad-analyst 做市场/领域研究"),
                    "prd": ("bmad-pm", "CP (创建 PRD)"),
                    "ux-spec": ("bmad-ux-designer", "CU (创建 UX 设计)"),
                    "architecture": ("bmad-architect", "CA (创建架构)"),
                    "epics": ("bmad-pm", "CE (创建 Epic/Story 列表)"),
                    "project-context": ("bmad-architect", "生成 project-context.md"),
                    "sprint-status": ("bmad-sm", "SP (Sprint 规划)"),
                }
                skill_id, action = skill_map.get(artifact, ("", ""))
                return _json({
                    "project": slug,
                    "next_artifact": artifact,
                    "missing_path": f"{base}/{spec['path']}",
                    "recommended_skill": skill_id,
                    "recommended_action": action,
                    "instruction": f"用 `load_skill {skill_id}` 加载对应 Skill，然后执行 {action}",
                })
        # 所有产出物都存在
        return _json({
            "project": slug,
            "next_artifact": "stories",
            "recommended_skill": "bmad-create-story",
            "recommended_action": "创建下一个 Story 文件",
            "instruction": "用 `load_skill bmad-create-story` 创建下一个开发 Story",
        })

    tools.extend([
        ToolDefinition(
            name="project_init",
            description=(
                "初始化 BMAD 项目目录结构（planning/ + implementation/ + project-context.md）。"
                "用于开始一个新的软件项目。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "项目名称（如 'nexus-mesh', 'my-app'）",
                    },
                },
                "required": ["name"],
            },
            handler=project_init,
            risk_level=ToolRiskLevel.LOW,
            tags=["bmad", "project"],
        ),
        ToolDefinition(
            name="project_status",
            description=(
                "检查 BMAD 项目的产出物完成状态。"
                "显示 PRD、架构、Epic、Story 等各阶段产出物是否已完成。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "项目名称",
                    },
                },
                "required": ["name"],
            },
            handler=project_status,
            risk_level=ToolRiskLevel.LOW,
            tags=["bmad", "project"],
        ),
        ToolDefinition(
            name="project_next",
            description=(
                "推荐 BMAD 项目的下一步操作。"
                "根据产出物链分析当前进度，推荐应该使用哪个 Skill 做什么。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "项目名称",
                    },
                },
                "required": ["name"],
            },
            handler=project_next,
            risk_level=ToolRiskLevel.LOW,
            tags=["bmad", "project"],
        ),
    ])

    # ── Coding Agent 基础工具 ──

    async def file_edit(
        path: str,
        old_text: str,
        new_text: str,
    ) -> str:
        """对文件进行精确的局部编辑（search-and-replace 语义）。"""
        resolved = workspace_service.resolve(path)
        if not resolved.is_file():
            raise FileNotFoundError(f"文件不存在: {path}")

        content = resolved.read_text(encoding="utf-8")
        count = content.count(old_text)
        if count == 0:
            # 返回匹配失败的上下文信息帮助 LLM 调试
            lines = content.splitlines()
            preview = "\n".join(lines[:30]) if len(lines) > 30 else content
            return _json({
                "success": False,
                "error": "old_text 在文件中未找到，请检查空格、缩进和换行是否完全匹配",
                "file_lines": len(lines),
                "file_preview_first_30_lines": preview,
            })
        if count > 1:
            return _json({
                "success": False,
                "error": f"old_text 在文件中出现了 {count} 次，请提供更长的上下文使其唯一",
                "match_count": count,
            })

        new_content = content.replace(old_text, new_text, 1)
        workspace_service.write_text(path, new_content)
        return _json({
            "success": True,
            "path": str(resolved),
            "chars_removed": len(old_text),
            "chars_added": len(new_text),
        })

    tools.append(ToolDefinition(
        name="file_edit",
        description=(
            "对文件进行精确的局部编辑。使用 search-and-replace 语义：提供要替换的原始文本和新文本。"
            "old_text 必须在文件中唯一匹配（包括空格和换行）。"
            "比整文件重写更高效、更安全。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要编辑的文件路径",
                },
                "old_text": {
                    "type": "string",
                    "description": "要被替换的原始文本（必须完全匹配，包括空格和换行）",
                },
                "new_text": {
                    "type": "string",
                    "description": "替换后的新文本",
                },
            },
            "required": ["path", "old_text", "new_text"],
        },
        handler=file_edit,
        risk_level=ToolRiskLevel.MEDIUM,
        tags=["workspace", "coding", "write"],
    ))

    async def file_write(
        path: str,
        content: str,
    ) -> str:
        """创建或覆盖文件。"""
        resolved = workspace_service.write_text(path, content)
        return _json({
            "success": True,
            "path": str(resolved),
            "chars": len(content),
        })

    async def write_local_file(path: str, content: str) -> str:
        """兼容旧工具名，复用 file_write。"""
        return await file_write(path, content)

    tools.append(ToolDefinition(
        name="file_write",
        description=(
            "创建新文件或完整覆盖已有文件。"
            "对于已有文件的局部修改，优先使用 file_edit。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "文件路径（相对路径基于工作区根目录）",
                },
                "content": {
                    "type": "string",
                    "description": "文件的完整内容",
                },
            },
            "required": ["path", "content"],
        },
        handler=file_write,
        risk_level=ToolRiskLevel.MEDIUM,
        tags=["workspace", "coding", "write"],
    ))

    tools.append(ToolDefinition(
        name="write_local_file",
        description=(
            "兼容旧工具名：创建新文件或完整覆盖已有文件。"
            "对于已有文件的局部修改，优先使用 file_edit。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "文件路径（相对路径基于工作区根目录）",
                },
                "content": {
                    "type": "string",
                    "description": "文件的完整内容",
                },
            },
            "required": ["path", "content"],
        },
        handler=write_local_file,
        risk_level=ToolRiskLevel.MEDIUM,
        tags=["workspace", "coding", "write", "compat"],
    ))

    if system_runner is not None:
        async def file_search(
            pattern: str,
            path: str = ".",
            include: str = "",
            max_results: int = 30,
        ) -> str:
            """在工作区中搜索文件内容或文件名。"""
            # 判断是内容搜索还是文件名搜索
            cmds = []
            if any(c in pattern for c in ["*", "?", "**/"]):
                # glob 模式 → find 文件名
                cmd = f"find {_shell_escape(path)} -name {_shell_escape(pattern)} -type f 2>/dev/null | head -n {max_results}"
                cmds.append(cmd)
            else:
                # 文本内容搜索 → grep
                include_flag = f"--include={_shell_escape(include)}" if include else ""
                cmd = (
                    f"grep -rn {include_flag} --color=never "
                    f"-m {max_results} {_shell_escape(pattern)} {_shell_escape(path)} 2>/dev/null "
                    f"| head -n {max_results}"
                )
                cmds.append(cmd)

            result = await system_runner.run(
                cmds[0],
                actor="agent",
                timeout=30,
            )
            return _json({
                "matches": result["stdout"].strip() if result["stdout"] else "(无匹配)",
                "exit_code": result["exit_code"],
            })

        tools.append(ToolDefinition(
            name="file_search",
            description=(
                "在工作区中搜索。支持两种模式：\n"
                "1. 内容搜索：pattern 为文本/正则，在文件内容中搜索（等同 grep -rn）\n"
                "2. 文件名搜索：pattern 包含 * 或 ? 时，按文件名 glob 搜索（等同 find）\n"
                "可通过 include 参数限制文件类型，如 '*.py'。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "搜索模式（文本/正则 或 glob 文件名模式）",
                    },
                    "path": {
                        "type": "string",
                        "default": ".",
                        "description": "搜索起始路径（默认工作区根目录）",
                    },
                    "include": {
                        "type": "string",
                        "description": "限制文件类型，如 '*.py'、'*.ts'",
                    },
                    "max_results": {
                        "type": "integer",
                        "default": 30,
                        "description": "最大返回结果数",
                    },
                },
                "required": ["pattern"],
            },
            handler=file_search,
            risk_level=ToolRiskLevel.LOW,
            tags=["workspace", "coding", "search"],
        ))

    if allowlist is None:
        return tools
    return [tool for tool in tools if tool.name in allowlist]


def _shell_escape(s: str) -> str:
    """安全转义 shell 参数"""
    if not s:
        return "''"
    return "'" + s.replace("'", "'\"'\"'") + "'"
