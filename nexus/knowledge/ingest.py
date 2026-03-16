"""Knowledge ingest pipeline for Vault and external text sources."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .content import VaultContentStore
from .retrieval import RetrievalIndex


@dataclass
class IngestStats:
    files_processed: int = 0
    chunks_created: int = 0
    errors: int = 0
    files_skipped: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "files_processed": self.files_processed,
            "chunks_created": self.chunks_created,
            "errors": self.errors,
            "files_skipped": self.files_skipped,
        }


class KnowledgeIngestService:
    """
    Formal ingest entrypoint for Phase 3.

    Scope intentionally kept small:
    1. Vault markdown/pdf reindex
    2. Single-file ingest
    3. External text materialization for retrieval
    4. No legacy async task manager / UI progress baggage
    """

    def __init__(self, content_store: VaultContentStore, retrieval_index: RetrievalIndex):
        self._content = content_store
        self._retrieval = retrieval_index

    async def ingest_text(
        self,
        *,
        source: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        force: bool = False,
    ) -> int:
        return await self._retrieval.index_document(
            source=source,
            content=content,
            metadata=metadata or {},
            force=force,
        )

    async def ingest_file(self, relative_path: str, *, force: bool = False) -> bool:
        source_path = self._content.resolve_path(relative_path)
        content = self._content.read(relative_path)
        metadata = {
            "source": "vault",
            "file_modified": source_path.stat().st_mtime,
            "filename": source_path.name,
            "title": self._content.extract_title(content, fallback=source_path.stem),
        }
        chunks = await self._retrieval.index_document(
            source=relative_path,
            content=content,
            metadata=metadata,
            force=force,
        )
        return chunks > 0 or self._retrieval.has_same_hash(relative_path, self._retrieval.compute_content_hash(content))

    def ingest_directory(self, relative_path: str = "", *, delta_only: bool = True) -> dict[str, int]:
        target_dir = self._content.resolve_path(relative_path or ".")
        stats = self._retrieval.reindex_vault(target_dir, delta_only=delta_only)
        return {
            "files_processed": stats.get("files_processed", 0),
            "chunks_created": stats.get("chunks_created", 0),
            "errors": stats.get("errors", 0),
            "files_skipped": stats.get("files_skipped", 0),
        }

    def reindex_all(self) -> dict[str, int]:
        return self.ingest_directory("", delta_only=False)

    def discover_sources(self, relative_dir: str = "", limit: int = 500) -> list[str]:
        target_dir = self._content.resolve_path(relative_dir or ".")
        markdown_files = [
            path.relative_to(self._content.base_path).as_posix()
            for path in sorted(target_dir.rglob("*.md"))
            if "_system" not in str(path)
        ]
        pdf_files = [
            path.relative_to(self._content.base_path).as_posix()
            for path in sorted(target_dir.rglob("*.pdf"))
            if "_system" not in str(path)
        ]
        return (markdown_files + pdf_files)[:limit]
