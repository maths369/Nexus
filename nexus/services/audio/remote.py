"""Remote audio worker client for SenseVoice transcription."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from .service import TranscriptionResult, TranscriptionSegment


class RemoteAudioWorkerClient:
    def __init__(self, base_url: str, *, timeout_seconds: float = 120.0):
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    def transcribe(
        self,
        audio_path: Path,
        language: str | None = None,
        diarize: bool = False,
    ) -> TranscriptionResult:
        payload = {
            "audio_path": str(audio_path),
            "language": language,
            "diarize": diarize,
        }
        with httpx.Client(timeout=self._timeout_seconds, trust_env=True) as client:
            response = client.post(f"{self._base_url}/audio/transcribe-path", json=payload)
            response.raise_for_status()
            data = response.json()
        return self._parse_result(data)

    @staticmethod
    def _parse_result(data: dict[str, Any]) -> TranscriptionResult:
        segments = [
            TranscriptionSegment(
                start=item.get("start"),
                end=item.get("end"),
                text=str(item.get("text") or ""),
                confidence=item.get("confidence"),
                speaker_id=item.get("speaker_id"),
                speaker_name=item.get("speaker_name"),
            )
            for item in data.get("segments", [])
        ]
        return TranscriptionResult(
            text=str(data.get("text") or ""),
            language=str(data.get("language") or "zh"),
            segments=segments,
            duration=float(data.get("duration") or 0.0),
        )
