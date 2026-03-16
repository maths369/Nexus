"""Tests for MemoryManager — long-term memory orchestration."""

import pytest
from pathlib import Path

from nexus.knowledge.memory import EpisodicMemory
from nexus.knowledge.retrieval import RetrievalIndex
from nexus.knowledge.memory_manager import MemoryManager, MEMORY_SOURCE_PREFIX, IDENTITY_SOURCE_PREFIX


@pytest.fixture
def vault_path(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "_system" / "memory" / "journals").mkdir(parents=True)
    return vault


@pytest.fixture
def memory(vault_path: Path) -> EpisodicMemory:
    return EpisodicMemory(vault_path / "_system" / "memory" / "episodic.jsonl")


@pytest.fixture
def retrieval(tmp_path: Path) -> RetrievalIndex:
    return RetrievalIndex(tmp_path / "retrieval.db")


@pytest.fixture
def manager(memory: EpisodicMemory, retrieval: RetrievalIndex, vault_path: Path) -> MemoryManager:
    return MemoryManager(
        memory=memory,
        retrieval=retrieval,
        vault_path=vault_path,
        half_life_days=30.0,
    )


@pytest.mark.asyncio
async def test_save_writes_to_episodic_and_retrieval(
    manager: MemoryManager, memory: EpisodicMemory, retrieval: RetrievalIndex,
):
    result = await manager.save(
        summary="用户偏好 Python 3.11",
        detail="在讨论技术选型时明确表示偏好 Python 3.11",
        kind="preference",
        tags=["python", "技术偏好"],
        importance=4,
    )
    assert result["entry_id"]
    assert result["indexed"] is True

    # 验证 EpisodicMemory
    entries = memory.list_recent(limit=10)
    assert len(entries) == 1
    assert entries[0].summary == "用户偏好 Python 3.11"
    assert entries[0].importance == 4

    # 验证 RetrievalIndex
    stats = retrieval.get_stats()
    assert stats["chunks"] >= 1


@pytest.mark.asyncio
async def test_semantic_search_finds_saved_memory(manager: MemoryManager):
    await manager.save(
        summary="项目使用 FastAPI 作为后端框架",
        kind="fact",
        tags=["fastapi", "backend"],
        importance=3,
    )
    await manager.save(
        summary="前端使用 React + TipTap 编辑器",
        kind="fact",
        tags=["react", "frontend"],
        importance=3,
    )

    results = await manager.search("FastAPI backend", top_k=5)
    assert len(results) >= 1
    # 第一个结果应该匹配 FastAPI
    assert "FastAPI" in results[0]["content"]


@pytest.mark.asyncio
async def test_soul_read_write(manager: MemoryManager):
    # 初始为空
    assert manager.read_soul() == ""

    # 写入
    await manager.update_soul("# 测试身份\n我是测试用的 Agent。")
    soul = manager.read_soul()
    assert "测试身份" in soul


@pytest.mark.asyncio
async def test_user_profile_section_update(manager: MemoryManager):
    # 写入新 section
    await manager.update_user_profile("技术偏好", "偏好 Python + TypeScript")
    profile = manager.read_user_profile()
    assert "技术偏好" in profile
    assert "Python + TypeScript" in profile

    # 更新同一 section
    await manager.update_user_profile("技术偏好", "偏好 Rust + Go")
    profile = manager.read_user_profile()
    assert "Rust + Go" in profile

    # 添加另一个 section
    await manager.update_user_profile("工作风格", "喜欢自动化")
    profile = manager.read_user_profile()
    assert "工作风格" in profile
    assert "喜欢自动化" in profile

    results = await manager._retrieval.search("Rust Go", top_k=5)
    assert any(item.source == f"{IDENTITY_SOURCE_PREFIX}user_full" for item in results)


@pytest.mark.asyncio
async def test_daily_journal_append_and_read(manager: MemoryManager):
    await manager.append_daily_journal("完成了记忆系统的实现", date="2026-03-12")
    await manager.append_daily_journal("通过了所有测试", date="2026-03-12")

    journal = manager.read_daily_journal("2026-03-12")
    assert "完成了记忆系统的实现" in journal
    assert "通过了所有测试" in journal
    assert "# 记忆日志 2026-03-12" in journal


@pytest.mark.asyncio
async def test_list_journals(manager: MemoryManager):
    await manager.append_daily_journal("日志1", date="2026-03-10")
    await manager.append_daily_journal("日志2", date="2026-03-11")

    journals = manager.list_journals(limit=10)
    assert len(journals) == 2
    dates = [j["date"] for j in journals]
    assert "2026-03-10" in dates
    assert "2026-03-11" in dates


@pytest.mark.asyncio
async def test_identity_context(manager: MemoryManager):
    await manager.update_soul("# 我是 Nexus")
    await manager.update_user_profile("风格", "简洁直接")

    context = manager.get_identity_context()
    assert "Agent 身份" in context
    assert "我是 Nexus" in context
    assert "用户画像" in context


@pytest.mark.asyncio
async def test_reindex_identity_documents_indexes_machine_summary_and_full_docs(
    manager: MemoryManager,
    retrieval: RetrievalIndex,
):
    manager._user_path.write_text(
        "# USER\n\n## MACHINE SUMMARY\n\n```yaml\nfocus: 医疗器械研发\npriority: 数字化转型\n```\n\n## Details\n\nLei Yang 用户画像。\n",
        encoding="utf-8",
    )
    manager._soul_path.write_text(
        "# SOUL\n\n## MACHINE SUMMARY\n\n```yaml\nrole: AI工作与执行中枢\nstyle: 结论优先\n```\n\n## Details\n\n我是星策。\n",
        encoding="utf-8",
    )

    result = await manager.reindex_identity_documents(force=True)
    assert result["documents_indexed"] == 4
    assert result["errors"] == 0

    stats = retrieval.get_stats()
    assert stats["documents"] >= 4

    summary_hits = await retrieval.search("医疗器械研发 数字化转型", top_k=5)
    assert any(item.source == f"{IDENTITY_SOURCE_PREFIX}user_summary" for item in summary_hits)

    soul_hits = await retrieval.search("AI工作与执行中枢 结论优先", top_k=5)
    assert any(item.source == f"{IDENTITY_SOURCE_PREFIX}soul_summary" for item in soul_hits)


@pytest.mark.asyncio
async def test_update_soul_reindexes_identity_full_document(manager: MemoryManager):
    await manager.update_soul("# SOUL\n\n我是测试用 Agent，负责战略支持。")

    hits = await manager._retrieval.search("战略支持", top_k=5)
    assert any(item.source == f"{IDENTITY_SOURCE_PREFIX}soul_full" for item in hits)


@pytest.mark.asyncio
async def test_reindex_all_memories(manager: MemoryManager, retrieval: RetrievalIndex):
    # 直接写入 EpisodicMemory（跳过索引）
    await manager._memory.record(kind="fact", summary="测试记忆1", importance=3)
    await manager._memory.record(kind="fact", summary="测试记忆2", importance=3)

    result = await manager.reindex_all_memories()
    assert result["indexed"] == 2
    assert result["errors"] == 0

    stats = retrieval.get_stats()
    assert stats["chunks"] >= 2


@pytest.mark.asyncio
async def test_rule_based_flush(manager: MemoryManager):
    messages = [
        {"role": "user", "content": "这是一个很重要的技术决策，我们决定使用 Qwen3-Max 作为主力模型"},
        {"role": "assistant", "content": "好的，已记录。"},
    ]
    result = await manager._rule_based_flush(messages)
    assert result["saved"] >= 1


@pytest.mark.asyncio
async def test_parse_memory_extraction():
    content = '''```json
[
  {"summary": "用户选择 Qwen3-Max", "kind": "decision", "importance": 5, "tags": ["llm"]},
  {"summary": "前端用 React", "kind": "fact", "importance": 3, "tags": ["frontend"]}
]
```'''
    result = MemoryManager._parse_memory_extraction(content)
    assert len(result) == 2
    assert result[0]["summary"] == "用户选择 Qwen3-Max"
    assert result[1]["kind"] == "fact"


@pytest.mark.asyncio
async def test_parse_memory_extraction_empty():
    result = MemoryManager._parse_memory_extraction("[]")
    assert result == []

    result = MemoryManager._parse_memory_extraction("没有值得保存的内容")
    assert result == []
