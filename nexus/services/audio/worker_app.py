"""Standalone FastAPI app for GPU-backed SenseVoice transcription."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from pydantic import BaseModel

from nexus.knowledge import RetrievalIndex, StructuralIndex, VaultContentStore
from nexus.services.audio import AudioConfig, AudioService
from nexus.services.document import DocumentEditorService, DocumentService
from nexus.shared import load_nexus_settings


class TranscribePathRequest(BaseModel):
    audio_path: str
    language: str | None = None


def build_audio_worker_app() -> FastAPI:
    settings = load_nexus_settings()
    audio_settings = settings.audio_config()

    content_store = VaultContentStore(settings.vault_base_path)
    structural_index = StructuralIndex(settings.sqlite_dir / "knowledge.db")
    retrieval_index = RetrievalIndex(settings.sqlite_dir / "retrieval.db")
    document_service = DocumentService(content_store, structural_index, retrieval_index)
    document_editor = DocumentEditorService(document_service, structural_index)
    audio_service = AudioService(
        content_store,
        retrieval_index,
        document_service,
        editor_service=document_editor,
        config=AudioConfig(
            backend=str(audio_settings.get("backend", "sensevoice")),
            language=str(audio_settings.get("language", "zh")),
            sensevoice_model_dir=settings.resolve_path(
                audio_settings.get("sensevoice_model_dir"),
                "./models/sensevoice/SenseVoiceSmall",
            ),
            sensevoice_device=str(audio_settings.get("sensevoice_device", "cpu")),
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
        result = audio_service.transcribe_file(Path(request.audio_path), language=request.language)
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
                }
                for segment in result.segments
            ],
        }

    return app


app = build_audio_worker_app()
