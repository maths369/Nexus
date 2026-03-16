from __future__ import annotations

import asyncio
from pathlib import Path

from nexus.knowledge import RetrievalIndex, StructuralIndex, VaultContentStore
from nexus.services.audio import AudioConfig, AudioService, TranscriptionResult, TranscriptionSegment
from nexus.services.document import CollectionColumn, DocumentEditorService, DocumentService


def test_document_service_indexes_page_and_builds_backlinks(tmp_path):
    content = VaultContentStore(tmp_path / "vault")
    structural = StructuralIndex(tmp_path / "knowledge.db")
    retrieval = RetrievalIndex(tmp_path / "retrieval.db")
    service = DocumentService(content, structural, retrieval)

    page_a = asyncio.run(
        service.create_page(title="战略主文档", body="主文档内容", section="strategy")
    )
    asyncio.run(
        service.create_page(
            title="研发计划",
            body=f"关联到 [[{page_a.title}]] 和 [战略页](page://{page_a.page_id})\n\n## 计划\n内容",
            section="rnd",
        )
    )

    backlinks = structural.get_backlinks(page_a.page_id)
    anchors = structural.list_block_anchors(page_a.page_id)
    search_hits = asyncio.run(service.search("研发计划", top_k=3))

    assert backlinks
    assert backlinks[0]["source_title"] == "研发计划"
    assert search_hits
    assert anchors
    assert anchors[0]["label"] == "战略主文档"


def test_document_editor_supports_notion_style_content_operations(tmp_path):
    content = VaultContentStore(tmp_path / "vault")
    structural = StructuralIndex(tmp_path / "knowledge.db")
    retrieval = RetrievalIndex(tmp_path / "retrieval.db")
    documents = DocumentService(content, structural, retrieval)
    editor = DocumentEditorService(documents, structural)

    page = asyncio.run(documents.create_page(title="周计划", body="# 周计划\n\n## 今日重点\n\n- A", section="pages"))
    asyncio.run(editor.insert_checklist(relative_path=page.relative_path, items=["跟进 API 方案", "更新迁移计划"], heading="行动项"))
    asyncio.run(editor.insert_table(relative_path=page.relative_path, headers=["任务", "状态"], rows=[["迁移", "进行中"]], heading="追踪表"))
    db_page = asyncio.run(
        editor.create_database_page(
            title="项目数据库",
            columns=[CollectionColumn(name="Title", column_type="page", position=0), CollectionColumn(name="Owner", position=1)],
        )
    )
    asyncio.run(editor.insert_page_link(relative_path=page.relative_path, target=f"page://{db_page.page.page_id}", heading="参考"))

    page_content = documents.read_page(page.relative_path)
    db_content = documents.read_page(db_page.page.relative_path)
    backlinks = structural.get_backlinks(db_page.page.page_id)
    collections = structural.list_collections(db_page.page.page_id)

    assert "- [ ] 跟进 API 方案" in page_content
    assert "| 任务 | 状态 |" in page_content
    assert "page://" in page_content
    assert "<database-block" in db_content
    assert backlinks
    assert collections[0]["schema"]["columns"][0]["type"] == "page"


def test_audio_service_materializes_transcript_into_vault_and_retrieval(tmp_path):
    content = VaultContentStore(tmp_path / "vault")
    structural = StructuralIndex(tmp_path / "knowledge.db")
    retrieval = RetrievalIndex(tmp_path / "retrieval.db")
    documents = DocumentService(content, structural, retrieval)
    editor = DocumentEditorService(documents, structural)
    audio = AudioService(content, retrieval, documents, editor_service=editor)

    result = asyncio.run(
        audio.materialize_transcript(
            source_name="weekly-sync.m4a",
            transcript="今天讨论了 API 传输方案和知识架构。",
            summary="会议确认先收敛架构，再分阶段迁移。",
            action_items=["整理迁移阶段", "补充 API 方案"],
            target_section="meetings",
        )
    )

    transcript_text = content.read(result.transcript_path)
    page_text = documents.read_page(result.page.relative_path)
    search_hits = asyncio.run(retrieval.search("API 传输方案", top_k=3))

    assert "weekly-sync.m4a" in transcript_text
    assert "<summary-block>" in page_text
    assert "<transcript-block" in page_text
    assert search_hits


def test_audio_service_can_transcribe_and_materialize_with_injected_transcriber(tmp_path):
    content = VaultContentStore(tmp_path / "vault")
    structural = StructuralIndex(tmp_path / "knowledge.db")
    retrieval = RetrievalIndex(tmp_path / "retrieval.db")
    documents = DocumentService(content, structural, retrieval)
    editor = DocumentEditorService(documents, structural)

    def fake_transcriber(path: Path, language: str | None) -> TranscriptionResult:
        return TranscriptionResult(
            text="请把飞书 API 传输方案拆成接入层和协议层。",
            language=language or "zh",
            segments=[TranscriptionSegment(start=0.0, end=12.0, text="请把飞书 API 传输方案拆成接入层和协议层。")],
            duration=12.0,
        )

    async def fake_summarizer(text: str) -> dict[str, object]:
        return {
            "summary": "先定义接入层，再定义消息传输协议。",
            "action_items": ["整理事件模型", "补接口约束"],
            "title": "飞书 API 传输讨论",
        }

    audio = AudioService(
        content,
        retrieval,
        documents,
        editor_service=editor,
        config=AudioConfig(temp_directory=tmp_path / "tmp", final_directory=tmp_path / "audio", transcript_directory=tmp_path / "transcripts"),
        transcriber=fake_transcriber,
    )
    audio_file = tmp_path / "sample.wav"
    audio_file.write_bytes(b"fake")

    result = asyncio.run(
        audio.transcribe_and_materialize(
            audio_path=audio_file,
            summarizer=fake_summarizer,
            metadata={"source": "test"},
        )
    )

    page_text = documents.read_page(result.page.relative_path)
    retrieval_hits = asyncio.run(retrieval.search("消息传输协议", top_k=3))

    assert result.summary == "先定义接入层，再定义消息传输协议。"
    assert "<audio-block" in page_text
    assert "<summary-block>" in page_text
    assert retrieval_hits


def test_document_service_can_list_find_and_delete_pages(tmp_path):
    content = VaultContentStore(tmp_path / "vault")
    structural = StructuralIndex(tmp_path / "knowledge.db")
    retrieval = RetrievalIndex(tmp_path / "retrieval.db")
    documents = DocumentService(content, structural, retrieval)

    first = asyncio.run(documents.create_page(title="日志 2026-03-11", body="A", section="pages"))
    second = asyncio.run(documents.create_page(title="日志 2026-03-11", body="B", section="pages"))
    third = asyncio.run(documents.create_page(title="周会纪要", body="C", section="meetings"))

    listed = documents.list_page_summaries(section="pages", limit=10)
    found = documents.find_pages("日志 2026-03-11", limit=10)

    assert {item.relative_path for item in listed} >= {first.relative_path, second.relative_path}
    assert [item.title for item in found].count("日志 2026-03-11") == 2

    deleted = asyncio.run(documents.delete_page(relative_path=first.relative_path))

    found_after_delete = documents.find_pages("日志 2026-03-11", limit=10)
    assert deleted.relative_path == first.relative_path
    assert not content.exists(first.relative_path)
    assert structural.get_page_by_path(first.relative_path) is None
    assert all(item.relative_path != first.relative_path for item in found_after_delete)
    assert structural.get_page_by_path(third.relative_path) is not None
