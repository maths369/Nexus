"""Standalone FastAPI app for GPU-backed transcription."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from pydantic import BaseModel

from nexus.knowledge import RetrievalIndex, StructuralIndex, VaultContentStore
from nexus.services.audio import AudioConfig, AudioService
from nexus.services.audio.diarization import DiarizationConfig, DiarizationEngine
from nexus.services.audio.voiceprint import VoiceprintStore
from nexus.services.document import DocumentEditorService, DocumentService
from nexus.shared import load_nexus_settings


def _resolve_audio_device(raw_device: str) -> str:
    candidate = str(raw_device or "auto").strip().lower()
    if candidate and candidate != "auto":
        return candidate
    try:
        import torch

        return "cuda:0" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


class TranscribePathRequest(BaseModel):
    audio_path: str
    language: str | None = None
    diarize: bool = False


def build_audio_worker_app() -> FastAPI:
    settings = load_nexus_settings()
    audio_settings = settings.audio_config()

    content_store = VaultContentStore(settings.vault_base_path)
    structural_index = StructuralIndex(settings.sqlite_dir / "knowledge.db")
    retrieval_index = RetrievalIndex(settings.sqlite_dir / "retrieval.db")
    document_service = DocumentService(content_store, structural_index, retrieval_index)
    document_editor = DocumentEditorService(document_service, structural_index)
    # -- diarization engine --
    diar_settings = audio_settings.get("diarization", {})
    diarization_engine: DiarizationEngine | None = None
    voiceprint_store: VoiceprintStore | None = None
    requested_device = str(audio_settings.get("sensevoice_device", "auto"))
    resolved_device = _resolve_audio_device(requested_device)
    if diar_settings.get("enabled", False):
        diarization_engine = DiarizationEngine(
            config=DiarizationConfig(
                enabled=True,
                vad_model=str(diar_settings.get("vad_model", "fsmn-vad")),
                embedding_model=str(diar_settings.get("embedding_model", "iic/speech_campplus_sv_zh-cn_16k-common")),
                device=resolved_device,
                min_speakers=int(diar_settings.get("min_speakers", 1)),
                max_speakers=int(diar_settings.get("max_speakers", 10)),
                clustering=str(diar_settings.get("clustering", "spectral")),
                similarity_threshold=float(diar_settings.get("similarity_threshold", 0.65)),
            )
        )
        voiceprints_dir = settings.vault_base_path / "_system" / "voiceprints"
        voiceprints_dir.mkdir(parents=True, exist_ok=True)
        voiceprint_store = VoiceprintStore(
            storage_dir=voiceprints_dir,
            similarity_threshold=float(diar_settings.get("similarity_threshold", 0.65)),
            embedding_extractor=diarization_engine,
        )

    audio_service = AudioService(
        content_store,
        retrieval_index,
        document_service,
        editor_service=document_editor,
        config=AudioConfig(
            backend=str(audio_settings.get("backend", "faster_whisper")),
            language=str(audio_settings.get("language", "auto")),
            sensevoice_model_dir=settings.resolve_path(
                audio_settings.get("sensevoice_model_dir"),
                "./models/sensevoice/SenseVoiceSmall",
            ),
            sensevoice_device=requested_device,
            faster_whisper_model=str(audio_settings.get("faster_whisper_model", "large-v3")),
            faster_whisper_compute_type=str(audio_settings.get("faster_whisper_compute_type", "float16")),
            preprocessing_enabled=bool(audio_settings.get("preprocessing_enabled", True)),
            preprocessing_backend=str(audio_settings.get("preprocessing_backend", "ffmpeg")),
            preprocessing_filters=str(
                audio_settings.get("preprocessing_filters", "highpass=f=120,lowpass=f=7600,afftdn,loudnorm")
            ),
            deepfilternet_model=str(audio_settings.get("deepfilternet_model", "DeepFilterNet3")),
            deepfilternet_post_filter=bool(audio_settings.get("deepfilternet_post_filter", True)),
            enhancement_target_rate=int(audio_settings.get("enhancement_target_rate", 48000)),
            asr_sample_rate=int(audio_settings.get("asr_sample_rate", 16000)),
            vad_enabled=bool(audio_settings.get("vad_enabled", False)),
            vad_threshold=float(audio_settings.get("vad_threshold", 0.45)),
            vad_min_speech_ms=int(audio_settings.get("vad_min_speech_ms", 200)),
            vad_min_silence_ms=int(audio_settings.get("vad_min_silence_ms", 400)),
            vad_speech_pad_ms=int(audio_settings.get("vad_speech_pad_ms", 120)),
            temp_directory=settings.resolve_path(
                audio_settings.get("temp_directory"),
                settings.vault_base_path / "_system" / "audio_temp",
            ),
            final_directory=settings.resolve_path(
                audio_settings.get("final_directory"),
                settings.vault_base_path / "_system" / "audio",
            ),
            transcript_directory=settings.resolve_path(
                audio_settings.get("transcript_directory"),
                settings.vault_base_path / "_system" / "transcripts",
            ),
            base_url=str(audio_settings.get("base_url", "http://127.0.0.1:8010")),
        ),
        diarization_engine=diarization_engine,
        voiceprint_store=voiceprint_store,
    )

    app = FastAPI(title="Nexus Audio Worker", version="0.1.0")

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "backend": audio_service.config.backend,
            "device": audio_service.config.sensevoice_device,
            "available": audio_service.is_available(),
        }

    @app.post("/audio/transcribe-path")
    async def transcribe_path(request: TranscribePathRequest):
        result = audio_service.transcribe_file(
            Path(request.audio_path),
            language=request.language,
            diarize=request.diarize,
        )
        return {
            "text": result.text,
            "language": result.language,
            "duration": result.duration,
            "segments": [
                {
                    "start": segment.start,
                    "end": segment.end,
                    "text": segment.text,
                    "confidence": segment.confidence,
                    "speaker_id": segment.speaker_id,
                    "speaker_name": segment.speaker_name,
                }
                for segment in result.segments
            ],
        }

    return app


app = build_audio_worker_app()
