"""Layer 2: structural knowledge index backed by SQLite."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid5, NAMESPACE_URL


@dataclass
class PageNode:
    page_id: str
    relative_path: str
    title: str
    page_type: str = "note"
    parent_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    last_opened_at: datetime | None = None


class StructuralIndex:
    """Structural index for pages, links, block anchors, collections, and recents."""

    def __init__(self, db_path: Path):
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def upsert_page(self, node: PageNode) -> None:
        node.updated_at = datetime.now()
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """
                INSERT INTO pages (
                    page_id, relative_path, title, page_type, parent_id,
                    metadata, created_at, updated_at, last_opened_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(page_id) DO UPDATE SET
                    relative_path=excluded.relative_path,
                    title=excluded.title,
                    page_type=excluded.page_type,
                    parent_id=excluded.parent_id,
                    metadata=excluded.metadata,
                    updated_at=excluded.updated_at,
                    last_opened_at=excluded.last_opened_at
                """,
                (
                    node.page_id,
                    node.relative_path,
                    node.title,
                    node.page_type,
                    node.parent_id,
                    json.dumps(node.metadata, ensure_ascii=False),
                    node.created_at.isoformat(),
                    node.updated_at.isoformat(),
                    node.last_opened_at.isoformat() if node.last_opened_at else None,
                ),
            )

    def get_page(self, page_id: str) -> PageNode | None:
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM pages WHERE page_id = ?", (page_id,)).fetchone()
        return self._row_to_page(row) if row else None

    def get_page_by_path(self, relative_path: str) -> PageNode | None:
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM pages WHERE relative_path = ?",
                (relative_path,),
            ).fetchone()
        return self._row_to_page(row) if row else None

    def delete_page(self, page_id: str) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("DELETE FROM page_links WHERE source_page_id = ? OR target_page_id = ?", (page_id, page_id))
            conn.execute("DELETE FROM block_anchors WHERE page_id = ?", (page_id,))
            conn.execute("DELETE FROM collections WHERE page_id = ?", (page_id,))
            conn.execute("DELETE FROM pages WHERE page_id = ?", (page_id,))

    def list_children(self, parent_id: str | None = None) -> list[PageNode]:
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            if parent_id is None:
                rows = conn.execute(
                    "SELECT * FROM pages WHERE parent_id IS NULL ORDER BY updated_at DESC"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM pages WHERE parent_id = ? ORDER BY updated_at DESC",
                    (parent_id,),
                ).fetchall()
        return [self._row_to_page(row) for row in rows]

    def find_pages(self, query: str, limit: int = 10) -> list[PageNode]:
        pattern = f"%{query.strip()}%"
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM pages
                WHERE title LIKE ? OR relative_path LIKE ? OR page_id = ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (pattern, pattern, query.strip(), limit),
            ).fetchall()
        return [self._row_to_page(row) for row in rows]

    def record_link(
        self,
        *,
        source_page_id: str,
        target_page_id: str,
        link_type: str = "wikilink",
        anchor: str | None = None,
    ) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """
                INSERT INTO page_links (source_page_id, target_page_id, link_type, anchor)
                VALUES (?, ?, ?, ?)
                """,
                (source_page_id, target_page_id, link_type, anchor),
            )

    def clear_links(self, source_page_id: str) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("DELETE FROM page_links WHERE source_page_id = ?", (source_page_id,))

    def get_backlinks(self, page_id: str) -> list[dict[str, Any]]:
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT l.source_page_id, l.target_page_id, l.link_type, l.anchor,
                       p.title AS source_title, p.relative_path AS source_path
                FROM page_links l
                JOIN pages p ON p.page_id = l.source_page_id
                WHERE l.target_page_id = ?
                ORDER BY p.updated_at DESC
                """,
                (page_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def replace_block_anchors(self, page_id: str, anchors: list[dict[str, Any]]) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("DELETE FROM block_anchors WHERE page_id = ?", (page_id,))
            for anchor in anchors:
                conn.execute(
                    """
                    INSERT INTO block_anchors (anchor_id, page_id, label, block_type, offset, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        anchor.get("anchor_id"),
                        page_id,
                        anchor.get("label"),
                        anchor.get("block_type", "section"),
                        anchor.get("offset"),
                        datetime.now().isoformat(),
                    ),
                )

    def list_block_anchors(self, page_id: str) -> list[dict[str, Any]]:
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT anchor_id, label, block_type, offset, updated_at FROM block_anchors WHERE page_id = ? ORDER BY offset ASC",
                (page_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_recent_open(self, page_id: str) -> None:
        now = datetime.now().isoformat()
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "UPDATE pages SET last_opened_at = ?, updated_at = ? WHERE page_id = ?",
                (now, now, page_id),
            )

    def list_recent_pages(self, limit: int = 20) -> list[PageNode]:
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM pages
                WHERE last_opened_at IS NOT NULL
                ORDER BY last_opened_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._row_to_page(row) for row in rows]

    def upsert_collection(
        self,
        *,
        collection_id: str,
        page_id: str,
        name: str,
        schema: dict[str, Any],
    ) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """
                INSERT INTO collections (collection_id, page_id, name, schema_json, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(collection_id) DO UPDATE SET
                    page_id=excluded.page_id,
                    name=excluded.name,
                    schema_json=excluded.schema_json,
                    updated_at=excluded.updated_at
                """,
                (
                    collection_id,
                    page_id,
                    name,
                    json.dumps(schema, ensure_ascii=False),
                    datetime.now().isoformat(),
                ),
            )

    def get_collection(self, collection_id: str) -> dict[str, Any] | None:
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT collection_id, page_id, name, schema_json, updated_at FROM collections WHERE collection_id = ?",
                (collection_id,),
            ).fetchone()
        if not row:
            return None
        payload = dict(row)
        payload["schema"] = json.loads(payload.pop("schema_json") or "{}")
        return payload

    def list_collections(self, page_id: str | None = None) -> list[dict[str, Any]]:
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            if page_id:
                rows = conn.execute(
                    "SELECT collection_id, page_id, name, schema_json, updated_at FROM collections WHERE page_id = ? ORDER BY updated_at DESC",
                    (page_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT collection_id, page_id, name, schema_json, updated_at FROM collections ORDER BY updated_at DESC"
                ).fetchall()
        result = []
        for row in rows:
            payload = dict(row)
            payload["schema"] = json.loads(payload.pop("schema_json") or "{}")
            result.append(payload)
        return result

    def reset(self) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("DELETE FROM page_links")
            conn.execute("DELETE FROM block_anchors")
            conn.execute("DELETE FROM collections")
            conn.execute("DELETE FROM pages")

    def rebuild_from_vault(self, vault_root: Path) -> dict[str, int]:
        """
        Rebuild the full structural index from the canonical Vault filesystem.

        This is used after bulk imports or filesystem migrations, where the
        retrieval layer can be rebuilt from disk but the structural layer must
        re-adopt pages, links, and anchors.
        """
        root = Path(vault_root).resolve()
        markdown_files = [
            path for path in sorted(root.rglob("*.md"))
            if "_system" not in str(path)
        ]
        pdf_files = [
            path for path in sorted(root.rglob("*.pdf"))
            if "_system" not in str(path)
        ]

        page_cache: dict[str, PageNode] = {}
        markdown_contents: dict[str, str] = {}

        self.reset()

        for file_path in markdown_files:
            relative_path = file_path.relative_to(root).as_posix()
            content = file_path.read_text(encoding="utf-8")
            node = PageNode(
                page_id=self._page_id_for_path(relative_path),
                relative_path=relative_path,
                title=self._extract_title(content, fallback=file_path.stem),
                page_type=self._infer_page_type(relative_path, fallback="note"),
                metadata={
                    "source": "vault",
                    "rebuilt": True,
                    "file_modified": datetime.fromtimestamp(file_path.stat().st_mtime).isoformat(),
                },
            )
            self.upsert_page(node)
            page_cache[relative_path] = node
            markdown_contents[relative_path] = content

        for file_path in pdf_files:
            relative_path = file_path.relative_to(root).as_posix()
            node = PageNode(
                page_id=self._page_id_for_path(relative_path),
                relative_path=relative_path,
                title=file_path.stem,
                page_type="pdf",
                metadata={
                    "source": "vault",
                    "rebuilt": True,
                    "file_modified": datetime.fromtimestamp(file_path.stat().st_mtime).isoformat(),
                },
            )
            self.upsert_page(node)
            page_cache[relative_path] = node

        for relative_path, content in markdown_contents.items():
            node = page_cache[relative_path]
            self._refresh_page_links(node, content, page_cache)
            self.replace_block_anchors(node.page_id, self._extract_anchors(node.page_id, content))

        return {
            "pages": len(markdown_files) + len(pdf_files),
            "markdown_pages": len(markdown_files),
            "pdf_pages": len(pdf_files),
        }

    def _init_db(self) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS pages (
                    page_id TEXT PRIMARY KEY,
                    relative_path TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    page_type TEXT NOT NULL DEFAULT 'note',
                    parent_id TEXT,
                    metadata TEXT DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_opened_at TEXT
                );

                CREATE TABLE IF NOT EXISTS page_links (
                    link_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_page_id TEXT NOT NULL,
                    target_page_id TEXT NOT NULL,
                    link_type TEXT NOT NULL DEFAULT 'wikilink',
                    anchor TEXT
                );

                CREATE TABLE IF NOT EXISTS block_anchors (
                    anchor_id TEXT PRIMARY KEY,
                    page_id TEXT NOT NULL,
                    label TEXT NOT NULL,
                    block_type TEXT NOT NULL DEFAULT 'section',
                    offset INTEGER,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS collections (
                    collection_id TEXT PRIMARY KEY,
                    page_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    schema_json TEXT DEFAULT '{}',
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_pages_parent ON pages(parent_id);
                CREATE INDEX IF NOT EXISTS idx_pages_last_opened ON pages(last_opened_at DESC);
                CREATE INDEX IF NOT EXISTS idx_links_target ON page_links(target_page_id);
                CREATE INDEX IF NOT EXISTS idx_block_anchors_page ON block_anchors(page_id, offset);
                """
            )

    @staticmethod
    def _row_to_page(row: sqlite3.Row) -> PageNode:
        return PageNode(
            page_id=row["page_id"],
            relative_path=row["relative_path"],
            title=row["title"],
            page_type=row["page_type"],
            parent_id=row["parent_id"],
            metadata=json.loads(row["metadata"]) if row["metadata"] else {},
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            last_opened_at=(
                datetime.fromisoformat(row["last_opened_at"])
                if row["last_opened_at"]
                else None
            ),
        )

    @staticmethod
    def _page_id_for_path(relative_path: str) -> str:
        return str(uuid5(NAMESPACE_URL, f"nexus://vault/{relative_path}"))

    @staticmethod
    def _extract_title(content: str, fallback: str) -> str:
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                return stripped[2:].strip()
        return fallback

    @staticmethod
    def _infer_page_type(relative_path: str, fallback: str = "note") -> str:
        top = relative_path.split("/", 1)[0]
        mapping = {
            "journals": "journal",
            "meetings": "meeting",
            "strategy": "strategy",
            "rnd": "rnd",
            "inbox": "inbox",
            "life": "life",
            "pages": "note",
        }
        return mapping.get(top, fallback)

    @staticmethod
    def _extract_anchors(page_id: str, content: str) -> list[dict[str, Any]]:
        heading_pattern = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
        anchors: list[dict[str, Any]] = []
        for offset, match in enumerate(heading_pattern.finditer(content), start=1):
            label = match.group(2).strip()
            if not label:
                continue
            digest = hashlib.sha1(f"{page_id}:{label}:{offset}".encode("utf-8")).hexdigest()[:10]
            anchors.append(
                {
                    "anchor_id": f"{page_id}:{digest}",
                    "label": label,
                    "block_type": "section",
                    "offset": offset,
                }
            )
        return anchors

    def _refresh_page_links(
        self,
        node: PageNode,
        content: str,
        page_cache: dict[str, PageNode],
    ) -> None:
        wikilink_pattern = re.compile(r"\[\[([^\]]+)\]\]")
        page_ref_pattern = re.compile(r"\[[^\]]+\]\(page://([^)#]+)(?:#([^)]+))?\)")
        self.clear_links(node.page_id)

        title_map = {page.title.strip(): page for page in page_cache.values()}
        for raw_target in wikilink_pattern.findall(content):
            target_title = raw_target.strip()
            if not target_title:
                continue
            target = title_map.get(target_title)
            if target is None:
                continue
            self.record_link(
                source_page_id=node.page_id,
                target_page_id=target.page_id,
                link_type="wikilink",
            )

        seen_targets: set[tuple[str, str | None]] = set()
        for target_id, anchor in page_ref_pattern.findall(content):
            key = (target_id, anchor or None)
            if key in seen_targets:
                continue
            seen_targets.add(key)
            target = self.get_page(target_id)
            if target is None:
                continue
            self.record_link(
                source_page_id=node.page_id,
                target_page_id=target.page_id,
                link_type="page_ref",
                anchor=anchor or None,
            )
