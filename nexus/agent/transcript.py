"""JSONL run snapshot writer for audit and replay."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .types import AttemptConfig, Run, RunEvent


@dataclass
class TranscriptEntry:
    kind: str
    ts: float
    content: str = ""
    metadata: dict[str, Any] | None = None


class TranscriptWriter:
    """Write completed run snapshots as JSONL files."""

    def __init__(self, base_dir: Path):
        self._base_dir = Path(base_dir)

    def write_run_snapshot(
        self,
        *,
        run: Run,
        attempt: AttemptConfig,
        events: list[RunEvent],
        tool_profile: str | None = None,
    ) -> Path:
        session_dir = self._base_dir / self._sanitize_component(run.session_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        file_path = session_dir / f"{self._sanitize_component(run.run_id)}.jsonl"

        entries: list[TranscriptEntry] = [
            TranscriptEntry(
                kind="meta",
                ts=run.created_at.timestamp(),
                metadata={
                    "session_id": run.session_id,
                    "run_id": run.run_id,
                    "status": run.status.value,
                    "model": run.model,
                    "tool_profile": tool_profile,
                    "attempt_count": run.attempt_count,
                    "metadata": dict(run.metadata),
                },
            ),
            TranscriptEntry(
                kind="system_prompt",
                ts=run.created_at.timestamp(),
                content=attempt.system_prompt,
            ),
        ]

        for message in attempt.messages:
            role = str(message.get("role") or "").strip().lower()
            content = self._normalize_content(message.get("content"))
            if role == "user":
                entries.append(TranscriptEntry(kind="user_message", ts=run.created_at.timestamp(), content=content))
            elif role == "assistant":
                entries.append(TranscriptEntry(kind="assistant_message", ts=run.created_at.timestamp(), content=content))
            elif role == "tool":
                entries.append(
                    TranscriptEntry(
                        kind="tool_context",
                        ts=run.created_at.timestamp(),
                        content=content,
                        metadata={"tool_call_id": message.get("tool_call_id")},
                    )
                )

        for event in events:
            entry = self._event_to_entry(event)
            if entry is not None:
                entries.append(entry)

        entries.append(
            TranscriptEntry(
                kind="final_output",
                ts=run.updated_at.timestamp(),
                content=run.result or "",
                metadata={"error": run.error},
            )
        )

        with file_path.open("w", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")
        return file_path

    @staticmethod
    def _sanitize_component(value: str) -> str:
        safe = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in str(value))
        return safe or "unknown"

    @staticmethod
    def _normalize_content(content: Any) -> str:
        if isinstance(content, str):
            return content
        if content is None:
            return ""
        return json.dumps(content, ensure_ascii=False)

    def _event_to_entry(self, event: RunEvent) -> TranscriptEntry | None:
        ts = event.timestamp.timestamp()
        data = dict(event.data)
        event_type = event.event_type

        if event_type == "tool_call":
            return TranscriptEntry(
                kind="tool_call",
                ts=ts,
                content=str(data.get("tool") or ""),
                metadata={
                    "call_id": data.get("call_id"),
                    "arguments": data.get("arguments"),
                    "iteration": data.get("iteration"),
                },
            )
        if event_type == "tool_result":
            return TranscriptEntry(
                kind="tool_result",
                ts=ts,
                content=str(data.get("tool") or ""),
                metadata=data,
            )
        if event_type == "llm_response":
            return TranscriptEntry(
                kind="assistant_response",
                ts=ts,
                content="",
                metadata=data,
            )
        if event_type == "status_change":
            return TranscriptEntry(
                kind="status_change",
                ts=ts,
                content="",
                metadata=data,
            )
        if event_type == "context_compacted":
            return TranscriptEntry(
                kind="compression",
                ts=ts,
                content=str(data.get("focus") or ""),
                metadata=data,
            )
        if event_type in {
            "tool_blocked",
            "tool_loop_blocked",
            "tool_loop_warning",
            "approval_requested",
            "approval_granted",
            "approval_rejected",
            "workflow_memory_saved",
            "memory_evolution_suggested",
            "capability_promotion_suggested",
            "auto_skill_selected",
            "auto_skill_installed",
            "auto_extension_retry_scheduled",
            "auto_capability_enabled",
        }:
            return TranscriptEntry(
                kind=event_type,
                ts=ts,
                content="",
                metadata=data,
            )
        return None
