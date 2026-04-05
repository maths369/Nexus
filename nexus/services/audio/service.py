"""Audio service for local ASR transcription and knowledge materialization."""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

try:
    import soundfile as sf  # type: ignore[reportMissingImports]
except Exception:
    sf = None  # type: ignore[assignment]

from nexus.knowledge import RetrievalIndex, VaultContentStore
from nexus.services.document import DocumentEditorService, DocumentPageResult, DocumentService

if TYPE_CHECKING:
    from .diarization import DiarizationEngine
    from .voiceprint import VoiceprintStore

logger = logging.getLogger(__name__)


@dataclass
class AudioConfig:
    backend: str = "faster_whisper"
    language: str | None = None
    sensevoice_model_dir: Path | None = None
    sensevoice_device: str = "auto"
    faster_whisper_model: str = "large-v3"
    faster_whisper_compute_type: str = "float16"
    preprocessing_enabled: bool = True
    preprocessing_backend: str = "ffmpeg"
    preprocessing_filters: str = "highpass=f=120,lowpass=f=7600,afftdn,loudnorm"
    deepfilternet_model: str = "DeepFilterNet3"
    deepfilternet_post_filter: bool = True
    enhancement_target_rate: int = 48000
    asr_sample_rate: int = 16000
    vad_enabled: bool = False
    vad_threshold: float = 0.45
    vad_min_speech_ms: int = 200
    vad_min_silence_ms: int = 400
    vad_speech_pad_ms: int = 120
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
    speaker_id: str | None = None
    speaker_name: str | None = None


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


@dataclass
class SpeechRegion:
    start: float
    end: float


@dataclass
class PreparedAudioInput:
    audio_path: Path
    speech_regions: list[SpeechRegion] = field(default_factory=list)
    cleanup_paths: list[Path] = field(default_factory=list)
    sample_rate: int = 16000


class AudioService:
    """
    Audio pipeline entry for Nexus.

    Scope kept intentionally tight:
    1. Local ASR backends (faster-whisper / SenseVoice)
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
        diarization_engine: DiarizationEngine | None = None,
        voiceprint_store: VoiceprintStore | None = None,
    ):
        self._content = content_store
        self._retrieval = retrieval_index
        self._documents = document_service
        self._editor = editor_service
        self._config = self._normalize_config(config)
        self._transcriber = transcriber
        self._diarization = diarization_engine
        self._voiceprint_store = voiceprint_store
        self._sensevoice_model = None
        self._sensevoice_postprocess = None
        self._sensevoice_regex = re.compile(r"<\|.*?\|>")
        self._sensevoice_model_initialized = False
        self._faster_whisper_model = None
        self._faster_whisper_model_initialized = False
        self._deepfilternet_model = None
        self._deepfilternet_state = None
        self._deepfilternet_initialized = False
        self._silero_vad_model = None
        self._silero_vad_initialized = False
        self._cjk_char_regex = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")

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
        backend = self._config.backend.lower()
        if backend == "faster_whisper":
            return self._faster_whisper_model is not None
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
        diarize: bool = False,
    ) -> AudioMaterializationResult:
        audio_file = Path(audio_path).expanduser().resolve()
        transcription = await asyncio.to_thread(self.transcribe_file, audio_file, language, diarize)

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

    def transcribe_file(
        self,
        audio_path: Path,
        language: str | None = None,
        diarize: bool = False,
    ) -> TranscriptionResult:
        prepared_input = PreparedAudioInput(audio_path=audio_path, sample_rate=self._config.asr_sample_rate)
        if self._transcriber is not None:
            result = self._transcriber(audio_path, language)
        else:
            self._lazy_init_models()
            backend = self._config.backend.lower()
            try:
                prepared_input = self._prepare_audio_input(audio_path, backend=backend)
                if backend == "faster_whisper":
                    if self._faster_whisper_model is None:
                        raise RuntimeError("faster-whisper model not available")
                    result = self._transcribe_with_faster_whisper(prepared_input, language=language)
                else:
                    if self._sensevoice_model is None:
                        raise RuntimeError("SenseVoice model not available")
                    result = self._transcribe_with_sensevoice(prepared_input.audio_path, language=language)
            finally:
                self._cleanup_prepared_audio(prepared_input, original_path=audio_path)

        if diarize and self._diarization is not None:
            result = self._apply_diarization(audio_path, result)

        return result

    def _apply_diarization(self, audio_path: Path, result: TranscriptionResult) -> TranscriptionResult:
        """Run diarization and align speaker labels onto transcription segments."""
        try:
            diar_result = self._diarization.diarize(
                audio_path,
                voiceprint_store=self._voiceprint_store,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Diarization failed for %s, returning plain transcription: %s", audio_path, exc)
            return result

        if not diar_result.segments:
            return result

        # 对齐策略：对每个转录 segment，找时间重叠最大的 diarization segment
        TOLERANCE = 0.3  # 容差（秒）
        for tseg in result.segments:
            if tseg.start is None or tseg.end is None:
                continue
            best_overlap = 0.0
            best_speaker: str | None = None
            best_confidence = 0.0
            for dseg in diar_result.segments:
                overlap_start = max(tseg.start, dseg.start - TOLERANCE)
                overlap_end = min(tseg.end, dseg.end + TOLERANCE)
                overlap = max(0.0, overlap_end - overlap_start)
                if overlap > best_overlap:
                    best_overlap = overlap
                    best_speaker = dseg.speaker_id
                    best_confidence = dseg.confidence
            if best_speaker is not None:
                tseg.speaker_id = best_speaker
                # 如果 speaker_id 不是 "speaker_N" 格式，说明已匹配到白名单
                if not best_speaker.startswith("speaker_"):
                    tseg.speaker_name = best_speaker

        return result

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
        device = self._resolve_device("sensevoice")
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

    def _initialize_faster_whisper_model(self) -> None:
        if self._faster_whisper_model_initialized:
            return
        try:
            from faster_whisper import WhisperModel

            model_name = self._config.faster_whisper_model or "large-v3"
            device = self._resolve_device("faster_whisper")
            compute_type = self._config.faster_whisper_compute_type or "float16"
            self._faster_whisper_model = WhisperModel(
                model_name,
                device=device,
                compute_type=compute_type,
            )
            logger.info(
                "faster-whisper model loaded: %s on %s (%s)",
                model_name,
                device,
                compute_type,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to load faster-whisper model: %s", exc)
            self._faster_whisper_model = None
        finally:
            self._faster_whisper_model_initialized = True

    def _initialize_deepfilternet(self) -> None:
        if self._deepfilternet_initialized:
            return
        try:
            from df import init_df

            model_name = (self._config.deepfilternet_model or "").strip() or None
            self._deepfilternet_model, self._deepfilternet_state, _ = init_df(
                model_name,
                post_filter=bool(self._config.deepfilternet_post_filter),
                log_level="ERROR",
                log_file=None,
                config_allow_defaults=True,
            )
            logger.info(
                "DeepFilterNet model loaded: %s (post_filter=%s)",
                model_name or "default",
                self._config.deepfilternet_post_filter,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to load DeepFilterNet: %s", exc)
            self._deepfilternet_model = None
            self._deepfilternet_state = None
        finally:
            self._deepfilternet_initialized = True

    def _initialize_silero_vad(self) -> None:
        if self._silero_vad_initialized:
            return
        try:
            from silero_vad import load_silero_vad

            self._silero_vad_model = load_silero_vad()
            logger.info("Silero VAD model loaded")
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to load Silero VAD: %s", exc)
            self._silero_vad_model = None
        finally:
            self._silero_vad_initialized = True

    def _lazy_init_models(self) -> None:
        if self._transcriber is not None:
            return
        backend = self._config.backend.lower()
        if backend == "faster_whisper":
            if not self._faster_whisper_model_initialized:
                self._initialize_faster_whisper_model()
            return
        if backend not in {"sensevoice", "sensevoice_remote", "remote"}:
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

    def _transcribe_with_faster_whisper(
        self,
        prepared_input: PreparedAudioInput,
        *,
        language: str | None = None,
    ) -> TranscriptionResult:
        if self._faster_whisper_model is None:
            raise RuntimeError("faster-whisper model not available")
        if prepared_input.speech_regions:
            return self._transcribe_with_faster_whisper_regions(prepared_input, language=language)
        lang = self._normalize_language(language)
        try:
            segments_iter, info = self._run_faster_whisper_transcribe(
                prepared_input.audio_path,
                language=lang,
                vad_filter=True,
            )
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"faster-whisper inference failed: {exc}") from exc

        segments: list[TranscriptionSegment] = []
        text_parts: list[str] = []
        for segment in list(segments_iter):
            text = str(getattr(segment, "text", "") or "").strip()
            if not text:
                continue
            text_parts.append(text)
            segments.append(
                TranscriptionSegment(
                    start=self._safe_float(getattr(segment, "start", None)),
                    end=self._safe_float(getattr(segment, "end", None)),
                    text=text,
                )
            )

        detected_language = str(getattr(info, "language", "") or lang or self._config.language or "auto")
        return TranscriptionResult(
            text=self._merge_transcript_text(text_parts),
            language=detected_language,
            segments=segments,
            duration=self._get_audio_duration(prepared_input.audio_path),
        )

    def _transcribe_with_faster_whisper_regions(
        self,
        prepared_input: PreparedAudioInput,
        *,
        language: str | None = None,
    ) -> TranscriptionResult:
        if sf is None:
            logger.warning("soundfile unavailable; falling back to full-audio transcription without external VAD")
            return self._transcribe_with_faster_whisper(
                PreparedAudioInput(audio_path=prepared_input.audio_path, sample_rate=prepared_input.sample_rate),
                language=language,
            )
        waveform, sample_rate = self._read_audio_waveform(
            prepared_input.audio_path,
            sample_rate=prepared_input.sample_rate,
        )
        cleanup_paths: list[Path] = []
        segments: list[TranscriptionSegment] = []
        text_parts: list[str] = []
        lang = self._normalize_language(language)
        detected_language = lang or self._config.language or "auto"

        try:
            for index, region in enumerate(prepared_input.speech_regions):
                start_sample = max(0, int(region.start * sample_rate))
                end_sample = min(len(waveform), int(region.end * sample_rate))
                if end_sample <= start_sample:
                    continue
                chunk = waveform[start_sample:end_sample]
                if len(chunk) == 0:
                    continue
                chunk_path = self._config.temp_directory / f"vad-{uuid.uuid4().hex}-{index}.wav"
                sf.write(str(chunk_path), chunk, sample_rate)
                cleanup_paths.append(chunk_path)
                try:
                    chunk_segments, info = self._run_faster_whisper_transcribe(
                        chunk_path,
                        language=lang,
                        vad_filter=False,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Segment transcription failed for %s[%s]: %s", prepared_input.audio_path, index, exc)
                    continue
                detected_language = str(getattr(info, "language", "") or detected_language)
                for segment in list(chunk_segments):
                    text = str(getattr(segment, "text", "") or "").strip()
                    if not text:
                        continue
                    text_parts.append(text)
                    start = self._safe_float(getattr(segment, "start", None))
                    end = self._safe_float(getattr(segment, "end", None))
                    segments.append(
                        TranscriptionSegment(
                            start=(region.start + start) if start is not None else region.start,
                            end=(region.start + end) if end is not None else region.end,
                            text=text,
                        )
                    )
        finally:
            for chunk_path in cleanup_paths:
                try:
                    chunk_path.unlink(missing_ok=True)
                except Exception:
                    logger.warning("Failed to remove chunked audio %s", chunk_path, exc_info=True)

        if not text_parts:
            logger.warning(
                "External VAD produced no usable transcription for %s; retrying full audio",
                prepared_input.audio_path,
            )
            return self._transcribe_with_faster_whisper(
                PreparedAudioInput(audio_path=prepared_input.audio_path, sample_rate=prepared_input.sample_rate),
                language=language,
            )

        return TranscriptionResult(
            text=self._merge_transcript_text(text_parts),
            language=detected_language,
            segments=segments,
            duration=self._get_audio_duration(prepared_input.audio_path),
        )

    def _run_faster_whisper_transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None,
        vad_filter: bool,
    ):
        return self._faster_whisper_model.transcribe(
            str(audio_path),
            beam_size=5,
            vad_filter=vad_filter,
            word_timestamps=False,
            condition_on_previous_text=False,
            language=language,
            temperature=0.0,
        )

    def _prepare_audio_input(self, audio_path: Path, *, backend: str) -> PreparedAudioInput:
        prepared = PreparedAudioInput(audio_path=audio_path, sample_rate=self._config.asr_sample_rate)
        if backend != "faster_whisper":
            return prepared
        if not self._config.preprocessing_enabled:
            return prepared
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            logger.warning("ffmpeg not found; skipping audio preprocessing for %s", audio_path)
            return prepared
        preprocessing_backend = str(self._config.preprocessing_backend or "deepfilternet").strip().lower()
        try:
            if preprocessing_backend == "deepfilternet":
                return self._prepare_audio_with_deepfilternet(audio_path, ffmpeg=ffmpeg)
            return self._prepare_audio_with_ffmpeg(audio_path, ffmpeg=ffmpeg)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Audio preprocessing failed for %s: %s", audio_path, exc)
            return prepared

    def _prepare_audio_with_ffmpeg(self, audio_path: Path, *, ffmpeg: str) -> PreparedAudioInput:
        prepared = self._config.temp_directory / f"prep-{uuid.uuid4().hex}.wav"
        self._run_ffmpeg_prepare(
            ffmpeg=ffmpeg,
            input_path=audio_path,
            output_path=prepared,
            sample_rate=self._config.asr_sample_rate,
            filters=self._config.preprocessing_filters,
        )
        speech_regions = self._detect_speech_regions(prepared) if self._config.vad_enabled else []
        return PreparedAudioInput(
            audio_path=prepared,
            speech_regions=speech_regions,
            cleanup_paths=[prepared],
            sample_rate=self._config.asr_sample_rate,
        )

    def _prepare_audio_with_deepfilternet(self, audio_path: Path, *, ffmpeg: str) -> PreparedAudioInput:
        df_input = self._config.temp_directory / f"df-input-{uuid.uuid4().hex}.wav"
        df_output = self._config.temp_directory / f"df-output-{uuid.uuid4().hex}.wav"
        asr_input = self._config.temp_directory / f"df-asr-{uuid.uuid4().hex}.wav"
        self._run_ffmpeg_prepare(
            ffmpeg=ffmpeg,
            input_path=audio_path,
            output_path=df_input,
            sample_rate=self._config.enhancement_target_rate,
            filters="",
        )
        self._enhance_with_deepfilternet(df_input, df_output)
        self._run_ffmpeg_prepare(
            ffmpeg=ffmpeg,
            input_path=df_output,
            output_path=asr_input,
            sample_rate=self._config.asr_sample_rate,
            filters=self._config.preprocessing_filters,
        )
        speech_regions = self._detect_speech_regions(asr_input) if self._config.vad_enabled else []
        return PreparedAudioInput(
            audio_path=asr_input,
            speech_regions=speech_regions,
            cleanup_paths=[df_input, df_output, asr_input],
            sample_rate=self._config.asr_sample_rate,
        )

    def _run_ffmpeg_prepare(
        self,
        *,
        ffmpeg: str,
        input_path: Path,
        output_path: Path,
        sample_rate: int,
        filters: str,
    ) -> None:
        command = [
            ffmpeg,
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(input_path),
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
        ]
        filters = str(filters or "").strip()
        if filters:
            command.extend(["-af", filters])
        command.append(str(output_path))
        try:
            subprocess.run(command, check=True, capture_output=True)
        except Exception:
            output_path.unlink(missing_ok=True)
            raise

    def _enhance_with_deepfilternet(self, input_path: Path, output_path: Path) -> None:
        if sf is None:
            raise RuntimeError("soundfile is required for DeepFilterNet preprocessing")
        self._initialize_deepfilternet()
        if self._deepfilternet_model is None or self._deepfilternet_state is None:
            raise RuntimeError("DeepFilterNet model not available")
        try:
            import numpy as np
            import torch
            from df import enhance

            waveform, sample_rate = sf.read(str(input_path), dtype="float32", always_2d=True)
            if sample_rate != self._config.enhancement_target_rate:
                raise RuntimeError(
                    f"DeepFilterNet expects {self._config.enhancement_target_rate}Hz input, got {sample_rate}Hz"
                )
            audio_tensor = torch.from_numpy(waveform.T.copy())
            enhanced = enhance(
                self._deepfilternet_model,
                self._deepfilternet_state,
                audio_tensor,
                pad=True,
            )
            enhanced_np = enhanced.detach().cpu().numpy()
            if enhanced_np.ndim == 1:
                enhanced_np = enhanced_np[:, None]
            else:
                enhanced_np = enhanced_np.T
            enhanced_np = np.clip(enhanced_np, -1.0, 1.0)
            sf.write(str(output_path), enhanced_np, sample_rate)
        except Exception:
            output_path.unlink(missing_ok=True)
            raise

    def _detect_speech_regions(self, audio_path: Path) -> list[SpeechRegion]:
        self._initialize_silero_vad()
        if self._silero_vad_model is None:
            return []
        try:
            from silero_vad import get_speech_timestamps, read_audio

            sample_rate = self._config.asr_sample_rate
            waveform = read_audio(str(audio_path), sampling_rate=sample_rate)
            timestamps = get_speech_timestamps(
                waveform,
                self._silero_vad_model,
                threshold=self._config.vad_threshold,
                sampling_rate=sample_rate,
                min_speech_duration_ms=self._config.vad_min_speech_ms,
                max_speech_duration_s=float(self._config.chunk_seconds),
                min_silence_duration_ms=self._config.vad_min_silence_ms,
                speech_pad_ms=self._config.vad_speech_pad_ms,
                return_seconds=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Silero VAD failed for %s: %s", audio_path, exc)
            return []
        regions: list[SpeechRegion] = []
        for timestamp in timestamps:
            start = float(timestamp.get("start", 0)) / float(self._config.asr_sample_rate)
            end = float(timestamp.get("end", 0)) / float(self._config.asr_sample_rate)
            if end > start:
                regions.append(SpeechRegion(start=start, end=end))
        return regions

    @staticmethod
    def _cleanup_prepared_audio(prepared_input: PreparedAudioInput, *, original_path: Path) -> None:
        seen: set[Path] = set()
        for candidate in [prepared_input.audio_path, *prepared_input.cleanup_paths]:
            if candidate == original_path or candidate in seen:
                continue
            seen.add(candidate)
            try:
                candidate.unlink(missing_ok=True)
            except Exception:
                logger.warning("Failed to remove prepared audio %s", candidate, exc_info=True)

    def _resolve_device(self, backend: str) -> str:
        configured = str(self._config.sensevoice_device or "auto").strip().lower()
        if configured and configured != "auto":
            if backend == "faster_whisper" and configured.startswith("cuda"):
                return "cuda"
            return configured
        has_cuda = False
        try:
            import torch

            has_cuda = bool(torch.cuda.is_available())
        except Exception:
            has_cuda = False
        if backend == "faster_whisper":
            return "cuda" if has_cuda else "cpu"
        return "cuda:0" if has_cuda else "cpu"

    def _normalize_language(self, language: str | None) -> str | None:
        candidate = str(language or self._config.language or "").strip().lower()
        if not candidate or candidate == "auto":
            return None
        return candidate

    def _merge_transcript_text(self, text_parts: list[str]) -> str:
        merged = ""
        for raw_part in text_parts:
            part = str(raw_part or "").strip()
            if not part:
                continue
            if not merged:
                merged = part
                continue
            if self._needs_join_space(merged[-1], part[0]):
                merged += " "
            merged += part
        return merged.strip()

    def _needs_join_space(self, previous: str, current: str) -> bool:
        if previous.isspace() or current.isspace():
            return False
        if self._is_cjk(previous) or self._is_cjk(current):
            return False
        if previous in {"(", "[", "{", "“", '"', "'", "/", "-"}:
            return False
        if current in {")", "]", "}", "”", '"', "'", ",", ".", "!", "?", ":", ";", "/", "-"}:
            return False
        return True

    def _is_cjk(self, value: str) -> bool:
        return bool(self._cjk_char_regex.search(value))

    @staticmethod
    def _read_audio_waveform(audio_path: Path, *, sample_rate: int) -> tuple[Any, int]:
        if sf is None:
            raise RuntimeError("soundfile unavailable")
        waveform, detected_sample_rate = sf.read(str(audio_path), dtype="float32", always_2d=False)
        if detected_sample_rate != sample_rate:
            raise RuntimeError(f"Expected {sample_rate}Hz audio, got {detected_sample_rate}Hz")
        if getattr(waveform, "ndim", 1) > 1:
            waveform = waveform.mean(axis=1)
        return waveform, detected_sample_rate

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
            has_speakers = any(seg.speaker_id or seg.speaker_name for seg in segments)
            for idx, segment in enumerate(segments, start=1):
                timestamp = AudioService._format_segment_timestamp(segment, idx)
                speaker_label = segment.speaker_name or segment.speaker_id or ""
                if has_speakers and speaker_label:
                    lines.append(f"{idx}. [{timestamp}] **[{speaker_label}]** {segment.text}")
                else:
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
                            "speaker": segment.speaker_name or segment.speaker_id or None,
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
            faster_whisper_model=config.faster_whisper_model,
            faster_whisper_compute_type=config.faster_whisper_compute_type,
            preprocessing_enabled=bool(config.preprocessing_enabled),
            preprocessing_backend=str(config.preprocessing_backend or "deepfilternet"),
            preprocessing_filters=str(config.preprocessing_filters or ""),
            deepfilternet_model=str(config.deepfilternet_model or "DeepFilterNet3"),
            deepfilternet_post_filter=bool(config.deepfilternet_post_filter),
            enhancement_target_rate=int(config.enhancement_target_rate or 48000),
            asr_sample_rate=int(config.asr_sample_rate or 16000),
            vad_enabled=bool(config.vad_enabled),
            vad_threshold=float(config.vad_threshold or 0.45),
            vad_min_speech_ms=int(config.vad_min_speech_ms or 200),
            vad_min_silence_ms=int(config.vad_min_silence_ms or 400),
            vad_speech_pad_ms=int(config.vad_speech_pad_ms or 120),
            temp_directory=Path(config.temp_directory or "./vault/_system/audio_temp"),
            final_directory=Path(config.final_directory or "./vault/_system/audio"),
            transcript_directory=Path(config.transcript_directory or "./vault/_system/transcripts"),
            chunk_seconds=config.chunk_seconds,
            base_url=config.base_url,
        )
