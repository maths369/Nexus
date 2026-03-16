"""Audio service for SenseVoice-based transcription and knowledge materialization."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

try:
    import soundfile as sf  # type: ignore[reportMissingImports]
except Exception:
    sf = None  # type: ignore[assignment]

from nexus.knowledge import RetrievalIndex, VaultContentStore
from nexus.services.document import DocumentEditorService, DocumentPageResult, DocumentService

logger = logging.getLogger(__name__)


@dataclass
class AudioConfig:
    backend: str = "sensevoice"
    language: str | None = None
    sensevoice_model_dir: Path | None = None
    sensevoice_device: str = "cpu"
    temp_directory: Path | None = None
    final_directory: Path | None = None
    transcript_directory: Path | None = None
    chunk_seconds: int = 30
    base_url: str = "http://127.0.0.1:18000"


@dataclass
class TranscriptionSegment:
    start: float | None
    end: float | None
    text: str
    confidence: float | None = None


@dataclass
class TranscriptionResult:
    text: str
    language: str
    segments: list[TranscriptionSegment]
    duration: float = 0.0


@dataclass
class AudioMaterializationResult:
    transcript_path: str
    page: DocumentPageResult
    action_items: list[str] = field(default_factory=list)
    summary: str = ""


class AudioService:
    """
    Audio pipeline entry for Nexus.

    Scope kept intentionally tight:
    1. SenseVoice local path is the main path
    2. summarization is optional and must not block transcript persistence
    3. output always enters the knowledge layers
    """

    def __init__(
        self,
        content_store: VaultContentStore,
        retrieval_index: RetrievalIndex,
        document_service: DocumentService,
        *,
        editor_service: DocumentEditorService | None = None,
        config: AudioConfig | None = None,
        transcriber: Callable[[Path, str | None], TranscriptionResult] | None = None,
    ):
        self._content = content_store
        self._retrieval = retrieval_index
        self._documents = document_service
        self._editor = editor_service
        self._config = self._normalize_config(config)
        self._transcriber = transcriber
        self._sensevoice_model = None
        self._sensevoice_postprocess = None
        self._sensevoice_regex = re.compile(r"<\|.*?\|>")
        self._sensevoice_model_initialized = False

        for path in [
            self._config.temp_directory,
            self._config.final_directory,
            self._config.transcript_directory,
        ]:
            path.mkdir(parents=True, exist_ok=True)

    @property
    def config(self) -> AudioConfig:
        return self._config

    def is_available(self) -> bool:
        if self._transcriber is not None:
            return True
        self._lazy_init_models()
        return self._sensevoice_model is not None

    async def transcribe_and_materialize(
        self,
        *,
        audio_path: str | Path,
        target_section: str = "meetings",
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
        summarizer: Callable[[str], Awaitable[dict[str, Any] | str]] | None = None,
        language: str | None = None,
    ) -> AudioMaterializationResult:
        audio_file = Path(audio_path).expanduser().resolve()
        transcription = await asyncio.to_thread(self.transcribe_file, audio_file, language)

        summary = ""
        action_items: list[str] = []
        resolved_title = title
        if summarizer is not None:
            try:
                payload = await summarizer(transcription.text)
                if isinstance(payload, str):
                    summary = payload.strip()
                elif isinstance(payload, dict):
                    summary = str(payload.get("summary") or "").strip()
                    action_items = [
                        str(item).strip()
                        for item in (payload.get("action_items") or [])
                        if str(item).strip()
                    ]
                    resolved_title = str(payload.get("title") or resolved_title or "").strip() or title
            except Exception as exc:  # noqa: BLE001
                logger.warning("Audio summarizer failed for %s: %s", audio_file, exc)

        return await self.materialize_transcript(
            source_name=audio_file.name,
            transcript=transcription.text,
            summary=summary,
            action_items=action_items,
            target_section=target_section,
            title=resolved_title,
            metadata={
                "audio_path": str(audio_file),
                "language": transcription.language,
                "duration": transcription.duration,
                **(metadata or {}),
            },
            segments=transcription.segments,
            audio_artifact_path=str(audio_file),
        )

    def transcribe_file(self, audio_path: Path, language: str | None = None) -> TranscriptionResult:
        if self._transcriber is not None:
            return self._transcriber(audio_path, language)
        self._lazy_init_models()
        if self._sensevoice_model is None:
            raise RuntimeError("SenseVoice model not available")
        return self._transcribe_with_sensevoice(audio_path, language=language)

    async def materialize_transcript(
        self,
        *,
        source_name: str,
        transcript: str,
        summary: str = "",
        action_items: list[str] | None = None,
        target_section: str = "meetings",
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
        segments: list[TranscriptionSegment] | None = None,
        audio_artifact_path: str | None = None,
    ) -> AudioMaterializationResult:
        if not transcript.strip():
            raise ValueError("transcript cannot be empty")

        action_items = action_items or []
        title = title or self._default_title(source_name, target_section)
        transcript_path = self._build_transcript_path(source_name)
        raw_payload = self._render_transcript_markdown(
            source_name=source_name,
            transcript=transcript,
            summary=summary,
            action_items=action_items,
            metadata=metadata or {},
            segments=segments or [],
        )
        self._content.write(transcript_path, raw_payload, create_if_missing=True)
        await self._retrieval.index_document(
            source=transcript_path,
            content=raw_payload,
            metadata={"kind": "transcript", "source_name": source_name, **(metadata or {})},
        )

        page_body = self._render_page_body(
            transcript=transcript,
            summary=summary,
            action_items=action_items,
            transcript_path=transcript_path,
            audio_artifact_path=audio_artifact_path,
            segments=segments or [],
        )
        page = await self._documents.create_page(
            title=title,
            body=page_body,
            section=target_section,
            page_type="audio_note",
            metadata={
                "source_name": source_name,
                "transcript_path": transcript_path,
                **(metadata or {}),
            },
        )
        return AudioMaterializationResult(
            transcript_path=transcript_path,
            page=page,
            action_items=action_items,
            summary=summary,
        )

    def _initialize_sensevoice_model(self) -> None:
        if self._sensevoice_model_initialized:
            return
        model_dir = self._config.sensevoice_model_dir
        if model_dir is None:
            logger.info("SenseVoice model directory not configured.")
            self._sensevoice_model_initialized = True
            return
        model_path = Path(model_dir)
        if not model_path.exists():
            logger.warning("SenseVoice model directory does not exist: %s", model_dir)
            self._sensevoice_model_initialized = True
            return
        device = self._config.sensevoice_device
        try:
            from funasr import AutoModel
            from funasr.utils.postprocess_utils import rich_transcription_postprocess

            self._sensevoice_model = AutoModel(
                model=str(model_path.resolve()),
                device=device,
                disable_update=True,
            )
            self._sensevoice_postprocess = rich_transcription_postprocess
            logger.info("SenseVoice model loaded from %s on %s", model_path, device)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to load SenseVoice model: %s", exc)
            self._sensevoice_model = None
        finally:
            self._sensevoice_model_initialized = True

    def _lazy_init_models(self) -> None:
        if self._transcriber is not None:
            return
        if self._config.backend.lower() not in {"sensevoice", "sensevoice_remote", "remote"}:
            raise RuntimeError(f"Unsupported audio backend: {self._config.backend}")
        if not self._sensevoice_model_initialized:
            self._initialize_sensevoice_model()

    def _transcribe_with_sensevoice(
        self,
        audio_path: Path,
        *,
        language: str | None = None,
    ) -> TranscriptionResult:
        if self._sensevoice_model is None:
            raise RuntimeError("SenseVoice model not available")
        lang = language or self._config.language or "auto"
        try:
            result = self._sensevoice_model.generate(
                input=str(audio_path),
                language=lang,
                use_itn=False,
                batch_size_s=60,
            )
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"SenseVoice inference failed: {exc}") from exc

        segments: list[TranscriptionSegment] = []
        text_parts: list[str] = []
        if result:
            for item in result:
                raw_text = ""
                if isinstance(item, dict):
                    raw_text = str(item.get("text") or "").strip()
                elif isinstance(item, str):
                    raw_text = item.strip()
                if not raw_text:
                    continue
                cleaned = self._sensevoice_regex.sub("", raw_text)
                processed = (
                    self._sensevoice_postprocess(cleaned)
                    if callable(self._sensevoice_postprocess)
                    else cleaned
                ).strip()
                if not processed:
                    continue
                text_parts.append(processed)
                start_val = self._safe_float(item.get("start")) if isinstance(item, dict) else None
                end_val = self._safe_float(item.get("end")) if isinstance(item, dict) else None
                segments.append(
                    TranscriptionSegment(
                        start=start_val,
                        end=end_val,
                        text=processed,
                    )
                )

        return TranscriptionResult(
            text="".join(text_parts).strip(),
            language=lang,
            segments=segments,
            duration=self._get_audio_duration(audio_path),
        )

    def _build_transcript_path(self, source_name: str) -> str:
        timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        stem = Path(source_name).stem or "audio"
        return f"_system/transcripts/{timestamp}-{stem}.md"

    @staticmethod
    def _default_title(source_name: str, target_section: str) -> str:
        stem = Path(source_name).stem or "audio-note"
        prefix = "会议纪要" if target_section == "meetings" else "语音记录"
        return f"{prefix} {stem}"

    @staticmethod
    def _render_transcript_markdown(
        *,
        source_name: str,
        transcript: str,
        summary: str,
        action_items: list[str],
        metadata: dict[str, Any],
        segments: list[TranscriptionSegment],
    ) -> str:
        lines = [
            f"# Transcript {source_name}",
            "",
            f"- generated_at: {datetime.utcnow().isoformat()}",
        ]
        for key, value in metadata.items():
            lines.append(f"- {key}: {value}")
        if summary:
            lines.extend(["", "## Summary", "", summary.strip()])
        if action_items:
            lines.extend(["", "## Action Items", ""])
            lines.extend([f"- [ ] {item}" for item in action_items])
        if segments:
            lines.extend(["", "## Segments", ""])
            for idx, segment in enumerate(segments, start=1):
                timestamp = AudioService._format_segment_timestamp(segment, idx)
                lines.append(f"{idx}. [{timestamp}] {segment.text}")
        lines.extend(["", "## Transcript", "", transcript.strip(), ""])
        return "\n".join(lines)

    def _render_page_body(
        self,
        *,
        transcript: str,
        summary: str,
        action_items: list[str],
        transcript_path: str,
        audio_artifact_path: str | None,
        segments: list[TranscriptionSegment],
    ) -> str:
        lines = [f"原始转录：[[{transcript_path}]]", ""]
        if self._editor is not None:
            if audio_artifact_path:
                lines.extend(["## 录音文件", "", self._editor.render_audio_block(audio_artifact_path), ""])
            if summary:
                lines.extend(["## AI 总结", "", self._editor.render_summary_block(summary), ""])
            if action_items:
                lines.extend(["## 行动项", ""])
                lines.extend([f"- [ ] {item}" for item in action_items])
                lines.append("")
            lines.extend([
                "## 原始转录",
                "",
                self._editor.render_transcript_block(
                    audio_path=audio_artifact_path,
                    transcript=transcript,
                    segments=[
                        {
                            "timestamp": self._format_segment_timestamp(segment, idx),
                            "text": segment.text,
                        }
                        for idx, segment in enumerate(segments, start=1)
                    ] if segments else None,
                ),
                "",
            ])
            return "\n".join(lines)

        if summary:
            lines.extend(["## 摘要", "", summary.strip(), ""])
        if action_items:
            lines.extend(["## 行动项", ""])
            lines.extend([f"- [ ] {item}" for item in action_items])
            lines.append("")
        lines.extend(["## 转录正文", "", transcript.strip(), ""])
        return "\n".join(lines)

    @staticmethod
    def _format_segment_timestamp(segment: TranscriptionSegment, index: int) -> str:
        if segment.start is not None and segment.end is not None:
            return f"{int(segment.start // 60):02d}:{int(segment.start % 60):02d}-{int(segment.end // 60):02d}:{int(segment.end % 60):02d}"
        start = (index - 1) * 30
        end = index * 30
        return f"{start // 60:02d}:{start % 60:02d}-{end // 60:02d}:{end % 60:02d}"

    @staticmethod
    def _safe_float(value: Any) -> float | None:
        try:
            return float(value) if value is not None else None
        except Exception:
            return None

    @staticmethod
    def _get_audio_duration(audio_path: Path) -> float:
        if sf is not None:
            try:
                return float(sf.info(str(audio_path)).duration)
            except Exception:
                return 0.0
        return 0.0

    @staticmethod
    def _normalize_config(config: AudioConfig | None) -> AudioConfig:
        if config is None:
            return AudioConfig(
                temp_directory=Path("./vault/_system/audio_temp"),
                final_directory=Path("./vault/_system/audio"),
                transcript_directory=Path("./vault/_system/transcripts"),
            )
        return AudioConfig(
            backend=config.backend,
            language=config.language,
            sensevoice_model_dir=Path(config.sensevoice_model_dir) if config.sensevoice_model_dir else None,
            sensevoice_device=config.sensevoice_device,
            temp_directory=Path(config.temp_directory or "./vault/_system/audio_temp"),
            final_directory=Path(config.final_directory or "./vault/_system/audio"),
            transcript_directory=Path(config.transcript_directory or "./vault/_system/transcripts"),
            chunk_seconds=config.chunk_seconds,
            base_url=config.base_url,
        )
