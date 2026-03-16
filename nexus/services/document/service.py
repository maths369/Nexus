"""Document Service built on top of the three-layer knowledge architecture."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from nexus.knowledge import PageNode, RetrievalIndex, StructuralIndex, VaultContentStore

_WIKILINK_PATTERN = re.compile(r"\[\[([^\]]+)\]\]")
_PAGE_LINK_PATTERN = re.compile(r"\[[^\]]+\]\(page://([^)#]+)(?:#([^)]+))?\)")
_HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)


@dataclass
class DocumentPageResult:
    page_id: str
    relative_path: str
    title: str
    page_type: str


@dataclass
class DocumentPageSummary:
    page_id: str
    relative_path: str
    title: str
    page_type: str
    updated_at: datetime | None = None


class DocumentService:
    """
    Notion-style document operations over Vault + structural index + retrieval.

    Migration intent:
    - Vault remains canonical source
    - Structural index tracks relations, anchors, and page metadata
    - Retrieval index tracks chunks for generation and recall
    """

    def __init__(
        self,
        content_store: VaultContentStore,
        structural_index: StructuralIndex,
        retrieval_index: RetrievalIndex,
    ):
        self._content = content_store
        self._structural = structural_index
        self._retrieval = retrieval_index

    async def create_page(
        self,
        *,
        title: str,
        body: str = "",
        section: str = "pages",
        page_type: str = "note",
        parent_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> DocumentPageResult:
        page = self._content.create_page(
            title=title,
            body=body,
            section=section,
            page_type=page_type,
            metadata=metadata,
        )
        node = PageNode(
            page_id=page.page_id,
            relative_path=page.relative_path,
            title=page.title,
            page_type=page.page_type,
            parent_id=parent_id,
            metadata=page.metadata,
        )
        self._structural.upsert_page(node)
        await self._reindex_page(node, self._content.read(page.relative_path))
        return DocumentPageResult(
            page_id=page.page_id,
            relative_path=page.relative_path,
            title=page.title,
            page_type=page.page_type,
        )

    async def update_page(
        self,
        *,
        relative_path: str,
        content: str,
        title: str | None = None,
        page_type: str | None = None,
        parent_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> DocumentPageResult:
        existing = self._structural.get_page_by_path(relative_path)
        if existing is None:
            raise FileNotFoundError(relative_path)

        self._content.backup(relative_path)
        self._content.write(relative_path, content, create_if_missing=False)

        updated = PageNode(
            page_id=existing.page_id,
            relative_path=relative_path,
            title=title or self._derive_title(content, fallback=existing.title),
            page_type=page_type or existing.page_type,
            parent_id=existing.parent_id if parent_id is None else parent_id,
            metadata=metadata or existing.metadata,
            created_at=existing.created_at,
            last_opened_at=existing.last_opened_at,
        )
        self._structural.upsert_page(updated)
        await self._reindex_page(updated, content)
        return DocumentPageResult(
            page_id=updated.page_id,
            relative_path=updated.relative_path,
            title=updated.title,
            page_type=updated.page_type,
        )

    async def move_page(
        self,
        *,
        relative_path: str,
        new_relative_path: str,
        parent_id: str | None = None,
    ) -> DocumentPageResult:
        existing = self._structural.get_page_by_path(relative_path)
        if existing is None:
            raise FileNotFoundError(relative_path)
        content = self._content.read(relative_path)
        self._content.move(relative_path, new_relative_path, overwrite=False)
        updated = PageNode(
            page_id=existing.page_id,
            relative_path=new_relative_path,
            title=existing.title,
            page_type=existing.page_type,
            parent_id=existing.parent_id if parent_id is None else parent_id,
            metadata=existing.metadata,
            created_at=existing.created_at,
            last_opened_at=existing.last_opened_at,
        )
        self._structural.upsert_page(updated)
        await self._retrieval.remove_document(relative_path)
        await self._reindex_page(updated, content)
        return DocumentPageResult(
            page_id=updated.page_id,
            relative_path=updated.relative_path,
            title=updated.title,
            page_type=updated.page_type,
        )

    async def delete_page(self, *, relative_path: str) -> DocumentPageResult:
        existing = self._structural.get_page_by_path(relative_path)
        if existing is None:
            raise FileNotFoundError(relative_path)
        self._content.backup(relative_path)
        self._content.delete(relative_path)
        self._structural.delete_page(existing.page_id)
        await self._retrieval.remove_document(relative_path)
        return DocumentPageResult(
            page_id=existing.page_id,
            relative_path=existing.relative_path,
            title=existing.title,
            page_type=existing.page_type,
        )

    def read_page(self, relative_path: str) -> str:
        page = self._structural.get_page_by_path(relative_path)
        if page is not None:
            self._structural.mark_recent_open(page.page_id)
        return self._content.read(relative_path)

    def list_pages(self, section: str = "", limit: int = 200) -> list[str]:
        return self._content.list_markdown(section, limit=limit)

    def list_page_summaries(self, section: str = "", limit: int = 200) -> list[DocumentPageSummary]:
        pages: list[DocumentPageSummary] = []
        for relative_path in self._content.list_markdown(section, limit=limit):
            page = self._structural.get_page_by_path(relative_path)
            if page is None:
                continue
            pages.append(
                DocumentPageSummary(
                    page_id=page.page_id,
                    relative_path=page.relative_path,
                    title=page.title,
                    page_type=page.page_type,
                    updated_at=page.updated_at,
                )
            )
        pages.sort(key=lambda item: item.updated_at or datetime.min, reverse=True)
        return pages[:limit]

    def find_pages(self, query: str, limit: int = 10) -> list[DocumentPageSummary]:
        return [
            DocumentPageSummary(
                page_id=page.page_id,
                relative_path=page.relative_path,
                title=page.title,
                page_type=page.page_type,
                updated_at=page.updated_at,
            )
            for page in self._structural.find_pages(query, limit=limit)
        ]

    async def search(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        results = await self._retrieval.search(query, top_k=top_k)
        return [
            {
                "source": result.source,
                "content": result.content,
                "score": result.score,
                "metadata": result.metadata,
            }
            for result in results
        ]

    async def _reindex_page(self, node: PageNode, content: str) -> None:
        await self._retrieval.index_document(
            source=node.relative_path,
            content=content,
            metadata={"page_id": node.page_id, "page_type": node.page_type, **node.metadata},
        )
        self._refresh_links(node.page_id, content)
        self._refresh_anchors(node.page_id, content)

    def _refresh_links(self, source_page_id: str, content: str) -> None:
        self._structural.clear_links(source_page_id)

        for raw_target in _WIKILINK_PATTERN.findall(content):
            target = raw_target.strip()
            if not target:
                continue
            candidates = self._structural.find_pages(target, limit=3)
            best = self._pick_link_candidate(target, candidates)
            if best:
                self._structural.record_link(
                    source_page_id=source_page_id,
                    target_page_id=best.page_id,
                    link_type="wikilink",
                )

        seen_targets: set[tuple[str, str | None]] = set()
        for target_id, anchor in _PAGE_LINK_PATTERN.findall(content):
            normalized_anchor = anchor or None
            key = (target_id, normalized_anchor)
            if key in seen_targets:
                continue
            seen_targets.add(key)
            target = self._structural.get_page(target_id)
            if target is None:
                continue
            self._structural.record_link(
                source_page_id=source_page_id,
                target_page_id=target.page_id,
                link_type="page_ref",
                anchor=normalized_anchor,
            )

    def _refresh_anchors(self, page_id: str, content: str) -> None:
        anchors = []
        for offset, match in enumerate(_HEADING_PATTERN.finditer(content), start=1):
            label = match.group(2).strip()
            if not label:
                continue
            anchors.append(
                {
                    "anchor_id": self._anchor_id(page_id, label, offset),
                    "label": label,
                    "block_type": "section",
                    "offset": offset,
                }
            )
        self._structural.replace_block_anchors(page_id, anchors)

    @staticmethod
    def _pick_link_candidate(target: str, candidates: list[PageNode]) -> PageNode | None:
        normalized = target.strip().lower()
        for candidate in candidates:
            if candidate.title.strip().lower() == normalized:
                return candidate
            if candidate.relative_path.strip().lower() == normalized:
                return candidate
            if candidate.page_id.strip().lower() == normalized:
                return candidate
        return candidates[0] if candidates else None

    @staticmethod
    def _derive_title(content: str, fallback: str) -> str:
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                return stripped[2:].strip() or fallback
        return fallback

    @staticmethod
    def _anchor_id(page_id: str, label: str, offset: int) -> str:
        slug = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff_-]+", "-", label).strip("-").lower() or "section"
        return f"{page_id}:{slug}:{offset}"
