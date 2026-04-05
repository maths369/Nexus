"""Notion-style content editing service built on top of DocumentService."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from nexus.knowledge import PageNode, StructuralIndex
from nexus.services.document.service import DocumentPageResult, DocumentService

_HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)


@dataclass
class CollectionColumn:
    name: str
    column_type: str = "text"
    position: int = 0
    config: dict[str, Any] = field(default_factory=dict)
    column_id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])


@dataclass
class DatabasePageResult:
    page: DocumentPageResult
    collection_id: str
    columns: list[CollectionColumn]


class DocumentEditorService:
    """Service-side content editing primitives required before the Web channel migrates."""

    def __init__(self, document_service: DocumentService, structural_index: StructuralIndex):
        self._documents = document_service
        self._structural = structural_index

    async def append_markdown_block(
        self,
        *,
        relative_path: str,
        block_markdown: str,
        heading: str | None = None,
        title: str | None = None,
    ) -> DocumentPageResult:
        content = self._documents.read_page(relative_path)
        updated = self._append_into_content(content, block_markdown.strip(), heading=heading)
        return await self._documents.update_page(
            relative_path=relative_path,
            content=updated,
            title=title,
        )

    async def replace_section(
        self,
        *,
        relative_path: str,
        heading: str,
        body: str,
        level: int = 2,
        create_if_missing: bool = True,
        title: str | None = None,
    ) -> DocumentPageResult:
        content = self._documents.read_page(relative_path)
        updated = self._replace_section(content, heading, body.strip(), level=level, create_if_missing=create_if_missing)
        return await self._documents.update_page(
            relative_path=relative_path,
            content=updated,
            title=title,
        )

    async def insert_checklist(
        self,
        *,
        relative_path: str,
        items: list[str],
        heading: str | None = None,
    ) -> DocumentPageResult:
        block = "\n".join(f"- [ ] {item}" for item in items if item.strip())
        return await self.append_markdown_block(relative_path=relative_path, block_markdown=block, heading=heading)

    async def insert_table(
        self,
        *,
        relative_path: str,
        headers: list[str],
        rows: list[list[str]],
        heading: str | None = None,
    ) -> DocumentPageResult:
        header_line = "| " + " | ".join(headers) + " |"
        divider_line = "| " + " | ".join(["---"] * len(headers)) + " |"
        row_lines = ["| " + " | ".join(row) + " |" for row in rows]
        block = "\n".join([header_line, divider_line, *row_lines])
        return await self.append_markdown_block(relative_path=relative_path, block_markdown=block, heading=heading)

    async def insert_page_link(
        self,
        *,
        relative_path: str,
        target: str,
        label: str | None = None,
        heading: str | None = None,
    ) -> DocumentPageResult:
        page = self.resolve_page_reference(target)
        if page is None:
            raise FileNotFoundError(f"Unknown page reference: {target}")
        block = self.render_page_link(page, label=label)
        return await self.append_markdown_block(relative_path=relative_path, block_markdown=block, heading=heading)

    async def create_database_page(
        self,
        *,
        title: str,
        section: str = "pages",
        parent_id: str | None = None,
        owner_page: str | None = None,
        columns: list[CollectionColumn] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> DatabasePageResult:
        collection_id = uuid.uuid4().hex
        columns = self._normalize_columns(columns)
        page_body = "\n\n".join([
            f"# {title}",
            self.render_database_block(collection_id=collection_id, owner_page=owner_page),
        ])
        page = await self._documents.create_page(
            title=title,
            body=page_body,
            section=section,
            page_type="database",
            parent_id=parent_id,
            metadata={
                "collection_id": collection_id,
                "owner_page": owner_page or "",
                **(metadata or {}),
            },
        )
        self._structural.upsert_collection(
            collection_id=collection_id,
            page_id=page.page_id,
            name=title,
            schema={
                "owner_page": owner_page or "",
                "columns": [
                    {
                        "id": column.column_id,
                        "name": column.name,
                        "type": column.column_type,
                        "position": column.position,
                        "config": column.config,
                    }
                    for column in columns
                ],
            },
        )
        return DatabasePageResult(page=page, collection_id=collection_id, columns=columns)

    def resolve_page_reference(self, reference: str) -> PageNode | None:
        ref = reference.strip()
        if not ref:
            return None
        if ref.startswith("page://"):
            return self._structural.get_page(ref.replace("page://", "", 1))
        page = self._structural.get_page(ref)
        if page is not None:
            return page
        page = self._structural.get_page_by_path(ref)
        if page is not None:
            return page
        candidates = self._structural.find_pages(ref, limit=5)
        if not candidates:
            return None
        lowered = ref.lower()
        for candidate in candidates:
            if candidate.title.lower() == lowered or candidate.relative_path.lower() == lowered:
                return candidate
        return candidates[0]

    @staticmethod
    def render_page_link(page: PageNode, label: str | None = None) -> str:
        text = label or f"📄 {page.title}"
        return f"[{text}](page://{page.page_id})"

    @staticmethod
    def render_database_block(*, collection_id: str, owner_page: str | None = None) -> str:
        owner_attr = f' data-owner-page="{owner_page}"' if owner_page else ""
        return f'<database-block data-id="{collection_id}"{owner_attr}></database-block>'

    @staticmethod
    def render_audio_block(src: str) -> str:
        return f'<audio-block src="{src}"></audio-block>'

    @staticmethod
    def render_image_block(src: str, alt: str | None = None) -> str:
        alt_attr = alt or "image"
        return f'<image-block src="{src}" alt="{alt_attr}"></image-block>'

    @staticmethod
    def render_file_block(src: str, label: str | None = None) -> str:
        text = label or src
        return f'<file-block src="{src}" label="{text}"></file-block>'

    @staticmethod
    def render_summary_block(text: str) -> str:
        payload = text.strip() or "暂无总结"
        return f"<summary-block>\n\n{payload}\n\n</summary-block>"

    @staticmethod
    def render_transcript_segment(
        *, index: int, timestamp: str, text: str, speaker: str | None = None,
    ) -> str:
        body = text.strip()
        speaker_attr = f' speaker="{speaker}"' if speaker else ""
        return (
            f'<transcript-segment index="{index}" timestamp="{timestamp}"{speaker_attr}>\n\n'
            f"{body}\n\n"
            "</transcript-segment>"
        )

    def render_transcript_block(
        self,
        *,
        audio_path: str | None = None,
        transcript: str,
        segments: list[dict[str, Any]] | None = None,
    ) -> str:
        opening = f'<transcript-block audio-path="{audio_path or ""}">' 
        lines = [opening, ""]
        if segments:
            for idx, segment in enumerate(segments, start=1):
                timestamp = str(segment.get("timestamp") or f"{idx:02d}:00")
                text = str(segment.get("text") or "").strip()
                if not text:
                    continue
                speaker = segment.get("speaker") or None
                lines.append(self.render_transcript_segment(
                    index=idx, timestamp=timestamp, text=text, speaker=speaker,
                ))
                lines.append("")
        else:
            lines.append(transcript.strip())
            lines.append("")
        lines.append("</transcript-block>")
        return "\n".join(lines)

    @staticmethod
    def _normalize_columns(columns: list[CollectionColumn] | None) -> list[CollectionColumn]:
        if columns:
            return sorted(columns, key=lambda item: item.position)
        return [
            CollectionColumn(name="Title", column_type="page", position=0),
            CollectionColumn(name="Status", column_type="select", position=1),
            CollectionColumn(name="Owner", column_type="text", position=2),
        ]

    @staticmethod
    def _append_into_content(content: str, block: str, heading: str | None = None) -> str:
        text = content.rstrip()
        if not heading:
            return f"{text}\n\n{block}\n"
        found = DocumentEditorService._find_heading_bounds(text, heading)
        if found is None:
            return f"{text}\n\n## {heading}\n\n{block}\n"
        _, end = found
        prefix = text[:end].rstrip()
        suffix = text[end:].lstrip("\n")
        body = f"{prefix}\n\n{block}"
        if suffix:
            body = f"{body}\n\n{suffix}"
        return body.rstrip() + "\n"

    @staticmethod
    def _replace_section(
        content: str,
        heading: str,
        body: str,
        *,
        level: int = 2,
        create_if_missing: bool = True,
    ) -> str:
        text = content.rstrip()
        found = DocumentEditorService._find_heading_bounds(text, heading)
        heading_line = f"{'#' * level} {heading}"
        replacement = f"{heading_line}\n\n{body.strip()}"
        if found is None:
            if not create_if_missing:
                raise ValueError(f"Heading not found: {heading}")
            return f"{text}\n\n{replacement}\n"
        start, end = found
        prefix = text[:start].rstrip()
        suffix = text[end:].lstrip("\n")
        merged = replacement
        if prefix:
            merged = f"{prefix}\n\n{merged}"
        if suffix:
            merged = f"{merged}\n\n{suffix}"
        return merged.rstrip() + "\n"

    @staticmethod
    def _find_heading_bounds(content: str, heading: str) -> tuple[int, int] | None:
        target = heading.strip().lower()
        matches = list(_HEADING_PATTERN.finditer(content))
        for idx, match in enumerate(matches):
            if match.group(2).strip().lower() != target:
                continue
            current_level = len(match.group(1))
            start = match.start()
            end = len(content)
            for later in matches[idx + 1 :]:
                if len(later.group(1)) <= current_level:
                    end = later.start()
                    break
            return start, end
        return None
