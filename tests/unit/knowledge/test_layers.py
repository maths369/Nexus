from __future__ import annotations

import asyncio

from nexus.knowledge import EpisodicMemory, RetrievalIndex, StructuralIndex, VaultContentStore
from nexus.knowledge.structural import PageNode


def test_vault_content_store_creates_expected_directories(tmp_path):
    store = VaultContentStore(tmp_path / "vault")

    assert (store.base_path / "pages").is_dir()
    assert (store.base_path / "journals").is_dir()
    assert (store.base_path / "_system" / "transcripts").is_dir()

    page = store.create_page(
        title="市场分析",
        body="这是测试页面。",
        section="strategy",
    )
    assert page.relative_path.startswith("strategy/")
    assert "市场分析" in store.read(page.relative_path)


def test_structural_index_tracks_pages_links_and_recents(tmp_path):
    index = StructuralIndex(tmp_path / "knowledge.db")
    page_a = PageNode(page_id="a", relative_path="pages/a.md", title="A")
    page_b = PageNode(page_id="b", relative_path="pages/b.md", title="B", parent_id="a")

    index.upsert_page(page_a)
    index.upsert_page(page_b)
    index.record_link(source_page_id="b", target_page_id="a")
    index.mark_recent_open("a")
    index.replace_block_anchors("a", [{"anchor_id": "a:intro", "label": "Intro", "offset": 1}])

    children = index.list_children("a")
    backlinks = index.get_backlinks("a")
    recents = index.list_recent_pages()
    anchors = index.list_block_anchors("a")

    assert len(children) == 1
    assert children[0].page_id == "b"
    assert backlinks[0]["source_page_id"] == "b"
    assert recents[0].page_id == "a"
    assert anchors[0]["label"] == "Intro"


def test_retrieval_and_episodic_memory_are_separate_layers(tmp_path):
    retrieval = RetrievalIndex(tmp_path / "retrieval.db")
    memory = EpisodicMemory(tmp_path / "episodic.jsonl")

    asyncio.run(
        retrieval.index_document(
            source="strategy/demo.md",
            content="Nexus 负责产品战略与研发管理。",
            metadata={"page_type": "strategy"},
        )
    )
    asyncio.run(
        memory.record(
            kind="decision",
            summary="优先保持 Vault 作为规范知识源",
            detail="不要把知识主存迁到数据库",
            tags=["vault", "architecture"],
        )
    )

    retrieval_hits = asyncio.run(retrieval.search("产品战略", top_k=3))
    recalled = asyncio.run(memory.recall("Vault 规范知识源", limit=3))

    assert retrieval_hits
    assert retrieval_hits[0].source == "strategy/demo.md"
    assert recalled
    assert "Vault" in recalled[0]


def test_retrieval_manifest_skips_unchanged_documents(tmp_path):
    retrieval = RetrievalIndex(tmp_path / "retrieval.db")

    first = asyncio.run(
        retrieval.index_document(
            source="pages/demo.md",
            content="# 标题\n\n同一份内容",
            metadata={"title": "标题"},
        )
    )
    second = asyncio.run(
        retrieval.index_document(
            source="pages/demo.md",
            content="# 标题\n\n同一份内容",
            metadata={"title": "标题"},
        )
    )

    assert first > 0
    assert second == 0
    snapshot = retrieval.manifest_snapshot()
    assert snapshot["pages/demo.md"]["total_chunks"] == first


def test_structural_index_rebuilds_from_vault_filesystem(tmp_path):
    vault = tmp_path / "vault"
    store = VaultContentStore(vault)
    store.write("pages/demo.md", "# Demo\n\nSee [[Strategy]].\n", create_if_missing=True)
    store.write("strategy/strategy.md", "# Strategy\n\n## Goals\n\nKeep Vault canonical.\n", create_if_missing=True)
    (vault / "pages" / "paper.pdf").write_bytes(b"%PDF-1.4\n")

    index = StructuralIndex(tmp_path / "knowledge.db")
    stats = index.rebuild_from_vault(vault)

    demo = index.get_page_by_path("pages/demo.md")
    strategy = index.get_page_by_path("strategy/strategy.md")
    pdf = index.get_page_by_path("pages/paper.pdf")

    assert stats["pages"] == 3
    assert demo is not None
    assert strategy is not None
    assert pdf is not None
    backlinks = index.get_backlinks(strategy.page_id)
    anchors = index.list_block_anchors(strategy.page_id)
    assert backlinks
    assert backlinks[0]["source_page_id"] == demo.page_id
    assert anchors
    assert anchors[0]["label"] == "Strategy"
