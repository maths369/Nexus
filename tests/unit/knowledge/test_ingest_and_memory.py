from __future__ import annotations

import asyncio

from nexus.knowledge import EpisodicMemory, KnowledgeIngestService, RetrievalIndex, VaultContentStore


def test_episodic_memory_supports_explicit_long_term_categories(tmp_path):
    memory = EpisodicMemory(tmp_path / "episodic.jsonl")

    asyncio.run(
        memory.remember_preference(
            summary="偏好用中文回复",
            detail="尤其是飞书工作流",
            tags=["language", "feishu"],
            session_id="sess-1",
        )
    )
    asyncio.run(
        memory.remember_decision(
            summary="Vault 继续作为规范知识源",
            detail="不要把知识主存迁到数据库",
            tags=["vault", "architecture"],
            session_id="sess-1",
        )
    )

    recalled = asyncio.run(memory.recall("Vault 规范知识源", limit=3))
    by_session = memory.list_entries_by_session("sess-1")
    sessions = memory.list_sessions()

    assert recalled
    assert "Vault" in recalled[0]
    assert len(by_session) == 2
    assert sessions[0]["session_id"] == "sess-1"


def test_ingest_service_indexes_single_file_and_directory(tmp_path):
    content = VaultContentStore(tmp_path / "vault")
    retrieval = RetrievalIndex(tmp_path / "retrieval.db")
    ingest = KnowledgeIngestService(content, retrieval)

    page = content.create_page(
        title="飞书 API 方案",
        body="定义接入层、协议层和回包策略。",
        section="rnd",
    )

    assert asyncio.run(ingest.ingest_file(page.relative_path)) is True

    hits = asyncio.run(retrieval.search("协议层", top_k=3))
    stats = ingest.ingest_directory("rnd", delta_only=True)
    sources = ingest.discover_sources("rnd")

    assert hits
    assert hits[0].source == page.relative_path
    assert stats["files_processed"] + stats["files_skipped"] >= 1
    assert page.relative_path in sources


def test_ingest_service_indexes_external_text_without_writing_vault(tmp_path):
    content = VaultContentStore(tmp_path / "vault")
    retrieval = RetrievalIndex(tmp_path / "retrieval.db")
    ingest = KnowledgeIngestService(content, retrieval)

    chunk_count = asyncio.run(
        ingest.ingest_text(
            source="external://meeting/live-summary",
            content="会议确认先做 Session Router，再做 Web channel。",
            metadata={"source": "external", "kind": "summary"},
        )
    )
    hits = asyncio.run(retrieval.search("Session Router", top_k=3))

    assert chunk_count > 0
    assert hits
    assert hits[0].source == "external://meeting/live-summary"
