"""Layer 3a: retrieval index with chunking, manifest dedupe, and optional embeddings."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


@dataclass
class RetrievalResult:
    source: str
    content: str
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)


class RetrievalIndex:
    """
    Retrieval index for document chunks.

    What is migrated from the legacy system:
    1. content-hash manifest dedupe
    2. boundary-aware chunking with overlap
    3. semantic hook point for embeddings

    What is intentionally *not* migrated:
    1. heavy Chroma-only coupling
    2. unrelated design metadata indexing
    3. UI-specific indexing side effects
    """

    def __init__(
        self,
        db_path: Path,
        chunk_size: int = 800,
        chunk_overlap: int = 120,
        embedding_function: Callable[[list[str]], list[list[float]]] | None = None,
        manifest_path: Path | None = None,
    ):
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap
        self._embedding_function = embedding_function
        self._manifest_path = manifest_path or self._db_path.with_name(f"{self._db_path.stem}.manifest.jsonl")
        self._manifest_cache = self._load_manifest()
        self._init_db()

    def manifest_snapshot(self) -> dict[str, dict[str, Any]]:
        return dict(self._manifest_cache)

    def has_same_hash(self, source: str, content_hash: str) -> bool:
        entry = self._manifest_cache.get(source)
        if not entry:
            return False
        return entry.get("content_hash") == content_hash

    @staticmethod
    def compute_content_hash(content: str) -> str:
        return hashlib.md5(content.encode("utf-8")).hexdigest()

    async def index_document(
        self,
        *,
        source: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        force: bool = False,
    ) -> int:
        metadata = dict(metadata or {})
        content_hash = self.compute_content_hash(content)
        if not force and self.has_same_hash(source, content_hash):
            return 0

        header_context = self._build_header_context(source, content, metadata)
        chunks = self._chunk_text(content, header_context=header_context)
        embeddings = self._embed_chunks(chunks) if chunks else None

        with sqlite3.connect(self._db_path) as conn:
            conn.execute("DELETE FROM retrieval_chunks WHERE source = ?", (source,))
            self._fts_delete_by_source(conn, source)
            for idx, chunk in enumerate(chunks):
                chunk_id = self._chunk_id(source, idx, chunk)
                row_meta = {
                    **metadata,
                    "path": source,
                    "chunk_index": idx,
                    "total_chunks": len(chunks),
                    "content_hash": content_hash,
                    "indexed_at": datetime.utcnow().isoformat(),
                }
                conn.execute(
                    """
                    INSERT INTO retrieval_chunks (
                        chunk_id, source, chunk_index, content, metadata, content_hash, embedding_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk_id,
                        source,
                        idx,
                        chunk,
                        json.dumps(row_meta, ensure_ascii=False),
                        content_hash,
                        json.dumps(embeddings[idx]) if embeddings else None,
                    ),
                )
                self._fts_insert(conn, chunk_id, chunk)

        self._update_manifest(source, content_hash, metadata, len(chunks))
        return len(chunks)

    async def remove_document(self, source: str) -> None:
        with sqlite3.connect(self._db_path) as conn:
            self._fts_delete_by_source(conn, source)
            conn.execute("DELETE FROM retrieval_chunks WHERE source = ?", (source,))
        if source in self._manifest_cache:
            del self._manifest_cache[source]
            self._persist_manifest()

    async def search(self, query: str, top_k: int = 5, min_score: float = 0.0) -> list[RetrievalResult]:
        query_text = query.strip()
        if not query_text:
            return []

        query_embedding = self._embed_query(query_text)

        # 优先使用 FTS5（有结果才用，否则降级）
        fts_results = self._fts_search(query_text, top_k=top_k * 3)

        if fts_results:
            # FTS5 可用：用 FTS 候选集 + 精排
            scored: list[RetrievalResult] = []
            for row in fts_results:
                content = row["content"]
                fts_rank = row.get("rank", 0.0)
                # FTS5 rank 是负数，值越小越好，转为正分
                lexical_score = max(0.0, 1.0 - abs(fts_rank) / 20.0)
                semantic_score = self._semantic_score(query_embedding, row.get("embedding_json"))
                score = max(lexical_score, semantic_score) if semantic_score > 0 else lexical_score
                if score < min_score or score <= 0:
                    continue
                metadata = json.loads(row["metadata"]) if row.get("metadata") else {}
                scored.append(
                    RetrievalResult(
                        source=row["source"],
                        content=content,
                        score=round(score, 6),
                        metadata=metadata,
                    )
                )
            scored.sort(key=lambda item: item.score, reverse=True)
            return scored[:top_k]

        # FTS5 不可用：降级到全表扫描
        query_tokens = self._tokenize(query_text)
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM retrieval_chunks ORDER BY source, chunk_index"
            ).fetchall()

        scored = []
        for row in rows:
            content = row["content"]
            doc_tokens = self._tokenize(content)
            lexical_score = self._lexical_score(query_text, query_tokens, content, doc_tokens)
            semantic_score = self._semantic_score(query_embedding, row["embedding_json"])
            score = max(lexical_score, semantic_score) if semantic_score > 0 else lexical_score
            if score < min_score or score <= 0:
                continue
            metadata = json.loads(row["metadata"]) if row["metadata"] else {}
            scored.append(
                RetrievalResult(
                    source=row["source"],
                    content=content,
                    score=round(score, 6),
                    metadata=metadata,
                )
            )

        scored.sort(key=lambda item: item.score, reverse=True)
        return scored[:top_k]

    async def build_context_pack(self, query: str, top_k: int = 5) -> str:
        results = await self.search(query, top_k=top_k)
        if not results:
            return ""
        return "\n\n---\n\n".join(f"[{item.source}]\n{item.content}" for item in results)

    def get_stats(self) -> dict[str, Any]:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS chunks, COUNT(DISTINCT source) AS docs FROM retrieval_chunks"
            ).fetchone()
        return {
            "chunks": row[0],
            "documents": row[1],
            "manifest_entries": len(self._manifest_cache),
            "embedding_enabled": self._embedding_function is not None,
        }

    def reindex_vault(self, vault_path: Path, delta_only: bool = False) -> dict[str, int]:
        stats = {"files_processed": 0, "chunks_created": 0, "errors": 0, "files_skipped": 0}
        vault_root = Path(vault_path)
        markdown_files = list(vault_root.rglob("*.md"))
        pdf_files = [p for p in vault_root.rglob("*.pdf") if "_system" not in str(p)]

        for pdf_path in pdf_files:
            try:
                from pypdf import PdfReader
            except Exception:
                stats["files_skipped"] += 1
                break
            try:
                reader = PdfReader(str(pdf_path))
                content = "\n\n".join((page.extract_text() or "") for page in reader.pages).strip()
                if not content:
                    stats["files_skipped"] += 1
                    continue
                rel = pdf_path.relative_to(vault_root).as_posix()
                content_hash = self.compute_content_hash(content)
                if delta_only and self.has_same_hash(rel, content_hash):
                    stats["files_skipped"] += 1
                    continue
                metadata = {
                    "source": "pdf",
                    "pages": len(reader.pages),
                    "file_modified": datetime.fromtimestamp(pdf_path.stat().st_mtime).isoformat(),
                    "filename": pdf_path.name,
                }
                stats["chunks_created"] += self._index_sync(rel, content, metadata, force=True)
                stats["files_processed"] += 1
            except Exception:
                stats["errors"] += 1

        for file_path in markdown_files:
            if "_system" in str(file_path):
                continue
            try:
                content = file_path.read_text(encoding="utf-8")
                rel = file_path.relative_to(vault_root).as_posix()
                content_hash = self.compute_content_hash(content)
                if delta_only and self.has_same_hash(rel, content_hash):
                    stats["files_skipped"] += 1
                    continue
                metadata = self._extract_frontmatter(content)
                metadata.setdefault(
                    "file_modified",
                    datetime.fromtimestamp(file_path.stat().st_mtime).isoformat(),
                )
                metadata.setdefault("source", metadata.get("source", "vault"))
                stats["chunks_created"] += self._index_sync(rel, content, metadata, force=True)
                stats["files_processed"] += 1
            except Exception:
                stats["errors"] += 1

        return stats

    def _index_sync(self, source: str, content: str, metadata: dict[str, Any], force: bool = False) -> int:
        return self._index_document_sync(source=source, content=content, metadata=metadata, force=force)

    def _index_document_sync(
        self,
        *,
        source: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        force: bool = False,
    ) -> int:
        metadata = dict(metadata or {})
        content_hash = self.compute_content_hash(content)
        if not force and self.has_same_hash(source, content_hash):
            return 0
        header_context = self._build_header_context(source, content, metadata)
        chunks = self._chunk_text(content, header_context=header_context)
        embeddings = self._embed_chunks(chunks) if chunks else None
        with sqlite3.connect(self._db_path) as conn:
            self._fts_delete_by_source(conn, source)
            conn.execute("DELETE FROM retrieval_chunks WHERE source = ?", (source,))
            for idx, chunk in enumerate(chunks):
                chunk_id = self._chunk_id(source, idx, chunk)
                row_meta = {
                    **metadata,
                    "path": source,
                    "chunk_index": idx,
                    "total_chunks": len(chunks),
                    "content_hash": content_hash,
                    "indexed_at": datetime.utcnow().isoformat(),
                }
                conn.execute(
                    """
                    INSERT INTO retrieval_chunks (
                        chunk_id, source, chunk_index, content, metadata, content_hash, embedding_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk_id,
                        source,
                        idx,
                        chunk,
                        json.dumps(row_meta, ensure_ascii=False),
                        content_hash,
                        json.dumps(embeddings[idx]) if embeddings else None,
                    ),
                )
                self._fts_insert(conn, chunk_id, chunk)
        self._update_manifest(source, content_hash, metadata, len(chunks))
        return len(chunks)

    def _load_manifest(self) -> dict[str, dict[str, Any]]:
        manifest: dict[str, dict[str, Any]] = {}
        if not self._manifest_path.exists():
            return manifest
        for line in self._manifest_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except Exception:
                continue
            path = str(entry.get("path") or "")
            if path:
                manifest[path] = entry
        return manifest

    def _persist_manifest(self) -> None:
        self._manifest_path.parent.mkdir(parents=True, exist_ok=True)
        with self._manifest_path.open("w", encoding="utf-8") as handle:
            for entry in self._manifest_cache.values():
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _update_manifest(self, source: str, content_hash: str, metadata: dict[str, Any], chunks: int) -> None:
        self._manifest_cache[source] = {
            "path": source,
            "content_hash": content_hash,
            "capture_date": metadata.get("capture_date") or metadata.get("date"),
            "version": metadata.get("version"),
            "source": metadata.get("source"),
            "file_modified": metadata.get("file_modified"),
            "total_chunks": chunks,
            "updated_at": datetime.utcnow().isoformat(),
        }
        self._persist_manifest()

    def _init_db(self) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS retrieval_chunks (
                    chunk_id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    metadata TEXT DEFAULT '{}',
                    content_hash TEXT,
                    embedding_json TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_retrieval_source
                    ON retrieval_chunks(source, chunk_index);
                """
            )
            # FTS5 虚拟表（如果不存在则创建）
            try:
                conn.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS retrieval_fts
                    USING fts5(chunk_id, content, tokenize='unicode61')
                    """
                )
            except Exception:
                pass  # FTS5 不可用时降级到全表扫描

    def _build_header_context(self, source: str, content: str, metadata: dict[str, Any]) -> str:
        title = metadata.get("title") or self._extract_h1(content)
        date_str = metadata.get("date") or metadata.get("capture_date") or self._extract_date(content)
        header = f"[File: {source}]"
        if title:
            header += f" [Title: {title}]"
        if date_str:
            header += f" [Date: {date_str}]"
        return f"{header}\n\n"

    def _chunk_text(self, content: str, header_context: str = "") -> list[str]:
        clean_text = self._sanitize_content(content).strip()
        if not clean_text:
            return []

        effective_size = max(160, self._chunk_size - len(header_context))
        if len(clean_text) <= effective_size:
            return [header_context + clean_text]

        chunks: list[str] = []
        start = 0
        while start < len(clean_text):
            end = min(len(clean_text), start + effective_size)
            chunk_content = clean_text[start:end]
            if end < len(clean_text):
                last_para = chunk_content.rfind("\n\n")
                last_sent = max(chunk_content.rfind("。"), chunk_content.rfind(". "))
                if last_para > effective_size // 2:
                    chunk_content = chunk_content[:last_para]
                elif last_sent > effective_size // 2:
                    chunk_content = chunk_content[: last_sent + 1]
            chunk_content = chunk_content.strip()
            if chunk_content:
                chunks.append(header_context + chunk_content)
            step = len(chunk_content) - self._chunk_overlap
            if step <= 0:
                step = 1
            start += step
            if len(chunks) > 10000:
                break
        return chunks

    @staticmethod
    def _sanitize_content(text: str) -> str:
        def url_replacer(match: re.Match[str]) -> str:
            url = match.group(0)
            if len(url) < 40:
                return url
            return f"[URL: {url[:36]}...]"

        return re.sub(r'https?://[^\s<>"]+|www\.[^\s<>"]+', url_replacer, text)

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        tokens = set(re.findall(r"[0-9a-zA-Z\u4e00-\u9fff]+", text.lower()))
        return {token for token in tokens if len(token) > 1}

    @staticmethod
    def _lexical_score(
        query_text: str,
        query_tokens: set[str],
        content: str,
        doc_tokens: set[str],
    ) -> float:
        if query_text and query_text.lower() in content.lower():
            return 1.0
        if not doc_tokens or not query_tokens:
            return 0.0
        overlap = query_tokens & doc_tokens
        if not overlap:
            return 0.0
        precision = len(overlap) / max(len(doc_tokens), 1)
        recall = len(overlap) / max(len(query_tokens), 1)
        return (2 * precision * recall) / max(precision + recall, 1e-6)

    def _embed_query(self, query: str) -> list[float] | None:
        if not self._embedding_function:
            return None
        try:
            vectors = self._embedding_function([query])
            return vectors[0] if vectors else None
        except Exception:
            return None

    def _embed_chunks(self, chunks: list[str]) -> list[list[float]] | None:
        if not self._embedding_function or not chunks:
            return None
        try:
            return self._embedding_function(chunks)
        except Exception:
            return None

    @staticmethod
    def _semantic_score(query_embedding: list[float] | None, embedding_json: str | None) -> float:
        if not query_embedding or not embedding_json:
            return 0.0
        try:
            doc_embedding = json.loads(embedding_json)
        except Exception:
            return 0.0
        denom = (
            math.sqrt(sum(x * x for x in query_embedding))
            * math.sqrt(sum(x * x for x in doc_embedding))
        ) or 1.0
        return max(0.0, sum(x * y for x, y in zip(query_embedding, doc_embedding)) / denom)

    @staticmethod
    def _extract_h1(content: str) -> str:
        match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
        return match.group(1).strip() if match else ""

    @staticmethod
    def _extract_date(content: str) -> str:
        match = re.search(r"\d{4}-\d{2}-\d{2}", content[:500])
        return match.group(0) if match else ""

    @staticmethod
    def _extract_frontmatter(content: str) -> dict[str, Any]:
        if not content.startswith("---\n"):
            return {}
        end_idx = content.find("\n---\n", 4)
        if end_idx < 0:
            return {}
        block = content[4:end_idx]
        try:
            import yaml  # type: ignore

            return yaml.safe_load(block) or {}
        except Exception:
            metadata: dict[str, Any] = {}
            for line in block.splitlines():
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                metadata[key.strip()] = value.strip()
            return metadata

    # ------------------------------------------------------------------
    # FTS5 helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _fts_insert(conn: sqlite3.Connection, chunk_id: str, content: str) -> None:
        """向 FTS5 虚拟表插入一条记录"""
        try:
            conn.execute(
                "INSERT INTO retrieval_fts (chunk_id, content) VALUES (?, ?)",
                (chunk_id, content),
            )
        except Exception:
            pass  # FTS5 不可用时静默跳过

    @staticmethod
    def _fts_delete_by_source(conn: sqlite3.Connection, source: str) -> None:
        """删除某个 source 在 FTS5 虚拟表中的所有记录"""
        try:
            # 通过子查询找到对应 chunk_id，再从 FTS 删除
            chunk_ids = conn.execute(
                "SELECT chunk_id FROM retrieval_chunks WHERE source = ?",
                (source,),
            ).fetchall()
            for (cid,) in chunk_ids:
                conn.execute(
                    "DELETE FROM retrieval_fts WHERE chunk_id = ?",
                    (cid,),
                )
        except Exception:
            pass  # FTS5 不可用时静默跳过

    def _fts_search(self, query_text: str, top_k: int = 15) -> list[dict[str, Any]] | None:
        """
        使用 FTS5 全文搜索。

        返回匹配行的列表（含 rank），如果 FTS5 不可用则返回 None。
        """
        # 构建 FTS5 查询
        # unicode61 tokenizer 将 CJK 字符逐字拆分，所以 CJK 也要逐字处理
        raw_tokens = re.findall(r"[0-9a-zA-Z]+|[\u4e00-\u9fff]", query_text.lower())
        if not raw_tokens:
            return None
        # 对每个 token 加双引号避免 FTS 语法冲突，用 OR 连接
        fts_query = " OR ".join(f'"{t}"' for t in raw_tokens)

        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """
                    SELECT
                        f.chunk_id,
                        f.content,
                        f.rank,
                        c.source,
                        c.metadata,
                        c.embedding_json
                    FROM retrieval_fts f
                    JOIN retrieval_chunks c ON c.chunk_id = f.chunk_id
                    WHERE retrieval_fts MATCH ?
                    ORDER BY f.rank
                    LIMIT ?
                    """,
                    (fts_query, top_k),
                ).fetchall()
                return [dict(row) for row in rows]
        except Exception:
            return None  # FTS5 不可用，返回 None 触发降级

    @staticmethod
    def _chunk_id(source: str, idx: int, chunk: str) -> str:
        digest = hashlib.sha1(f"{source}:{idx}:{chunk}".encode("utf-8")).hexdigest()
        return digest
