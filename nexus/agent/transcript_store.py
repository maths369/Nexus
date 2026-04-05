"""Append-only transcript persistence for long-running sessions."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


class TranscriptStore:
    """Stores transcript snapshots as JSONL for replay/recovery."""

    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def append_snapshot(
        self,
        transcript_id: str,
        messages: list[dict[str, Any]],
        *,
        trigger: str = "auto_compact",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        target = self._path_for(transcript_id)
        snapshot_id = datetime.now().strftime("%Y%m%d%H%M%S%f")
        header = {
            "kind": "snapshot",
            "snapshot_id": snapshot_id,
            "trigger": trigger,
            "timestamp": datetime.now().isoformat(),
            "message_count": len(messages),
            "metadata": metadata or {},
        }
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(header, ensure_ascii=False, default=str) + "\n")
            for message in messages:
                handle.write(
                    json.dumps(
                        {
                            "kind": "message",
                            "snapshot_id": snapshot_id,
                            "message": message,
                        },
                        ensure_ascii=False,
                        default=str,
                    )
                    + "\n"
                )
        return str(target)

    def load_latest_snapshot(self, transcript_id: str) -> list[dict[str, Any]]:
        target = self._path_for(transcript_id)
        if not target.exists():
            return []
        active_snapshot_id = ""
        messages: list[dict[str, Any]] = []
        for line in target.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            payload = json.loads(line)
            kind = payload.get("kind")
            if kind == "snapshot":
                active_snapshot_id = str(payload.get("snapshot_id") or "")
                messages = []
                continue
            if kind == "message" and str(payload.get("snapshot_id") or "") == active_snapshot_id:
                message = payload.get("message")
                if isinstance(message, dict):
                    messages.append(message)
        return messages

    def _path_for(self, transcript_id: str) -> Path:
        safe_id = "".join(
            ch if ch.isalnum() or ch in {"-", "_", "."} else "_"
            for ch in transcript_id.strip() or "adhoc"
        )
        return self.base_dir / f"{safe_id}.jsonl"
