"""Hub-side store for edge node execution journal entries."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class EdgeJournalStore:
    """Persists execution journal entries received from edge nodes.

    Stored as JSON files under ``{store_dir}/{node_id}/{entry_id}.json``.
    """

    def __init__(self, store_dir: Path) -> None:
        self._store_dir = store_dir
        self._store_dir.mkdir(parents=True, exist_ok=True)
        self._entries: list[dict[str, Any]] = []
        self._seen_ids: set[str] = set()
        self._load_existing()

    def _load_existing(self) -> None:
        """Load existing entries from disk on startup."""
        for node_dir in sorted(self._store_dir.iterdir()):
            if not node_dir.is_dir():
                continue
            for entry_file in sorted(node_dir.glob("*.json")):
                try:
                    data = json.loads(entry_file.read_text())
                    entry_id = str(data.get("entry_id") or "")
                    if entry_id and entry_id not in self._seen_ids:
                        self._seen_ids.add(entry_id)
                        self._entries.append(data)
                except Exception:
                    logger.warning("Failed to load journal entry: %s", entry_file, exc_info=True)

    def ingest(self, *, node_id: str, entries: list[dict[str, Any]]) -> list[str]:
        """Accept journal entries from an edge node. Returns list of accepted entry IDs."""
        accepted: list[str] = []
        node_dir = self._store_dir / node_id
        node_dir.mkdir(parents=True, exist_ok=True)

        for raw in entries:
            entry_id = str(raw.get("entry_id") or "")
            if not entry_id:
                continue
            if entry_id in self._seen_ids:
                # Already have this entry — still report it as accepted so edge marks it synced
                accepted.append(entry_id)
                continue

            entry = dict(raw)
            entry["node_id"] = node_id
            entry["synced_at"] = time.time()

            try:
                path = node_dir / f"{entry_id}.json"
                path.write_text(json.dumps(entry, ensure_ascii=False, indent=2))
            except Exception:
                logger.warning("Failed to persist journal entry %s", entry_id, exc_info=True)
                continue

            self._seen_ids.add(entry_id)
            self._entries.append(entry)
            accepted.append(entry_id)

        return accepted

    def list_entries(
        self,
        *,
        node_id: str = "",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return recent journal entries, optionally filtered by node_id."""
        filtered = self._entries
        if node_id:
            filtered = [e for e in filtered if e.get("node_id") == node_id]
        return list(reversed(filtered[-limit:]))

    def entry_count(self, node_id: str = "") -> int:
        if node_id:
            return sum(1 for e in self._entries if e.get("node_id") == node_id)
        return len(self._entries)
