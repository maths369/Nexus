from __future__ import annotations

import asyncio
from pathlib import Path

from nexus.knowledge import RetrievalIndex, StructuralIndex, VaultContentStore
from nexus.services.artifact import ArtifactService
from nexus.services.audio import AudioConfig, AudioService, TranscriptionResult, TranscriptionSegment
from nexus.services.document import DocumentEditorService, DocumentService


def _build_services(tmp_path):
    content = VaultContentStore(tmp_path / "vault")
    structural = StructuralIndex(tmp_path / "knowledge.db")
    retrieval = RetrievalIndex(tmp_path / "retrieval.db")
    documents = DocumentService(content, structural, retrieval)
    editor = DocumentEditorService(documents, structural)
    return content, structural, retrieval, documents, editor


def test_artifact_service_materializes_image_into_capture_page(tmp_path):
    content, _, retrieval, documents, editor = _build_services(tmp_path)
    service = ArtifactService(content, documents, document_editor=editor)

    result = asyncio.run(
        service.ingest_bytes(
            artifact_type="image",
            source="feishu",
            data=b"\x89PNG\r\n",
            filename="capture.png",
            mime_type="image/png",
        )
    )

    assert result.status == "materialized"
    assert result.page_relative_path is not None
    page_text = documents.read_page(result.page_relative_path)
    assert "<image-block" in page_text
    hits = asyncio.run(retrieval.search("图片归档", top_k=3))
    assert hits


def test_artifact_service_materializes_image_with_ocr_text(tmp_path):
    content, _, retrieval, documents, editor = _build_services(tmp_path)
    service = ArtifactService(
        content,
        documents,
        document_editor=editor,
        image_text_extractor=lambda _path: ("这是截图中的订单号 A-1024", "stub-ocr"),
    )

    result = asyncio.run(
        service.ingest_bytes(
            artifact_type="image",
            source="feishu",
            data=b"\x89PNG\r\n",
            filename="screenshot.png",
            mime_type="image/png",
        )
    )

    page_text = documents.read_page(result.page_relative_path or "")
    assert "## OCR 文本" in page_text
    assert "订单号 A-1024" in page_text
    hits = asyncio.run(retrieval.search("订单号 A-1024", top_k=3))
    assert hits


def test_artifact_service_materializes_text_file_into_import_page(tmp_path):
    content, _, retrieval, documents, editor = _build_services(tmp_path)
    service = ArtifactService(content, documents, document_editor=editor)

    result = asyncio.run(
        service.ingest_bytes(
            artifact_type="file",
            source="feishu",
            data="第一行\n第二行".encode("utf-8"),
            filename="notes.txt",
            mime_type="text/plain",
        )
    )

    assert result.status == "materialized"
    assert result.page_relative_path is not None
    page_text = documents.read_page(result.page_relative_path)
    assert "第一行" in page_text
    hits = asyncio.run(retrieval.search("第二行", top_k=3))
    assert hits


def test_artifact_service_materializes_audio_via_audio_service(tmp_path):
    content, _, retrieval, documents, editor = _build_services(tmp_path)

    def fake_transcriber(path: Path, language: str | None) -> TranscriptionResult:
        return TranscriptionResult(
            text="今天讨论了输入资产管线。",
            language=language or "zh",
            segments=[
                TranscriptionSegment(
                    start=0.0,
                    end=6.0,
                    text="今天讨论了输入资产管线。",
                )
            ],
            duration=6.0,
        )

    audio = AudioService(
        content,
        retrieval,
        documents,
        editor_service=editor,
        config=AudioConfig(
            temp_directory=tmp_path / "tmp",
            final_directory=tmp_path / "audio",
            transcript_directory=tmp_path / "transcripts",
        ),
        transcriber=fake_transcriber,
    )
    service = ArtifactService(content, documents, document_editor=editor, audio_service=audio)

    result = asyncio.run(
        service.ingest_bytes(
            artifact_type="audio",
            source="feishu",
            data=b"fake-wav",
            filename="meeting.wav",
            mime_type="audio/wav",
        )
    )

    assert result.status == "materialized"
    assert result.transcript_relative_path is not None
    assert result.page_relative_path is not None
    transcript_text = content.read(result.transcript_relative_path)
    page_text = documents.read_page(result.page_relative_path)
    assert "输入资产管线" in transcript_text
    assert "<audio-block" in page_text


def test_artifact_service_creates_batch_manifest(tmp_path):
    content, _, _, documents, editor = _build_services(tmp_path)
    service = ArtifactService(content, documents, document_editor=editor)

    first = asyncio.run(
        service.ingest_bytes(
            artifact_type="file",
            source="feishu",
            data="第一份文件".encode("utf-8"),
            filename="one.txt",
            mime_type="text/plain",
        )
    )
    second = asyncio.run(
        service.ingest_bytes(
            artifact_type="image",
            source="feishu",
            data=b"\x89PNG\r\n",
            filename="two.png",
            mime_type="image/png",
        )
    )

    manifest = asyncio.run(service.create_batch_manifest([first, second], source="feishu"))

    assert manifest.status == "materialized"
    assert manifest.page_relative_path is not None
    page_text = documents.read_page(manifest.page_relative_path)
    assert "## 导入概览" in page_text
    assert "one.txt" in page_text
    assert "two.png" in page_text
