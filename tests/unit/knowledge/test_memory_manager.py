"""Tests for MemoryManager — long-term memory orchestration."""

import json
from pathlib import Path

import pytest

from nexus.agent.types import RunEvent
from nexus.channel.session_store import SessionStore
from nexus.knowledge.memory import EpisodicMemory
from nexus.knowledge.memory_manager import (
    IDENTITY_SOURCE_PREFIX,
    JOURNAL_SOURCE_PREFIX,
    MEMORY_SOURCE_PREFIX,
    MemoryManager,
)
from nexus.knowledge.retrieval import RetrievalIndex
from nexus.knowledge.structural import StructuralIndex
from nexus.knowledge.content import VaultContentStore
from nexus.services.document import DocumentService


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


@pytest.fixture
def session_store(tmp_path: Path) -> SessionStore:
    return SessionStore(tmp_path / "sessions.db")


@pytest.fixture
def document_service(vault_path: Path, tmp_path: Path, retrieval: RetrievalIndex) -> DocumentService:
    return DocumentService(
        VaultContentStore(vault_path),
        StructuralIndex(tmp_path / "knowledge.db"),
        retrieval,
    )


class _MedicalPromotionProvider:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def chat_completion(self, **kwargs):
        self.calls.append(kwargs)
        payload = {
            "medical_relevant": True,
            "l2_memories": [
                {
                    "summary": "BF 类患者漏电流限值需要单独核对",
                    "detail": "本次飞书会话明确要求把 BF 类患者漏电流限值作为法规判断重点。",
                    "kind": "decision",
                    "tags": ["BF", "漏电流", "IEC60601-1"],
                    "importance": 5,
                }
            ],
            "l3_entries": [
                {
                    "folder": "adr",
                    "title": "BF 类患者漏电流判定路径",
                    "summary": "先查 IEC 60601-1，再映射内部测试项。",
                    "body_markdown": "## 背景\n\n需要统一 BF 类患者漏电流的判定路径。\n\n## 决策\n\n- 先核对 IEC 60601-1 原始条款。\n- 再映射到内部测试项与记录模板。\n",
                    "promotion_state": "working",
                }
            ],
            "l4_entries": [
                {
                    "section": "regulation",
                    "title": "BF 类患者漏电流限值",
                    "summary": "整理 BF 类患者漏电流的适用标准与判定口径。",
                    "body_markdown": "## 适用标准\n\n- IEC 60601-1\n\n## 判定口径\n\n- 以 BF 类患者漏电流限值为正式判定基线。\n",
                    "promotion_state": "published",
                }
            ],
            "weekly_summary": {
                "title": "飞书问答沉淀",
                "body_markdown": "- 本周持续出现 BF 类患者漏电流相关问题。\n- 已形成标准核对与内部映射的决策路径。\n",
            },
        }
        return {"message": {"content": f"```json\n{json.dumps(payload, ensure_ascii=False)}\n```"}}


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
async def test_sync_retrieval_sources_indexes_journals_and_vault_pages(
    manager: MemoryManager,
    retrieval: RetrievalIndex,
    vault_path: Path,
):
    project_dir = vault_path / "projects"
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "roadmap.md").write_text(
        "# 自我进化路线图\n\nMemory 检索优先，随后补 skill 演化闭环。\n",
        encoding="utf-8",
    )
    await manager.append_daily_journal("今天完成了 Memory 同步链路。", date="2026-03-20")

    result = await manager.sync_retrieval_sources(delta_only=False, include_vault=True)

    assert result["journals"]["files_processed"] == 1
    assert result["vault"]["files_processed"] == 1

    journal_hits = await retrieval.search("Memory 同步链路", top_k=5)
    assert any(item.source == f"{JOURNAL_SOURCE_PREFIX}2026-03-20" for item in journal_hits)

    vault_hits = await retrieval.search("自我进化路线图", top_k=5)
    assert any(item.source == "projects/roadmap.md" for item in vault_hits)


@pytest.mark.asyncio
async def test_build_prompt_context_formats_high_value_memories(manager: MemoryManager):
    await manager.save(
        summary="用户偏好结果先行",
        detail="输出时先给结论，再补推理。",
        kind="preference",
        importance=5,
    )
    await manager.save(
        summary="Excel 转 CSV 工作流成功",
        detail="优先用 excel_list_sheets 和 excel_to_csv。",
        kind="workflow_success",
        tags=["excel", "csv"],
        importance=4,
    )

    context = await manager.build_prompt_context("如何处理 Excel 转 CSV", top_k=5)

    assert "[偏好" in context
    assert "[成功经验" in context
    assert "excel_to_csv" in context


@pytest.mark.asyncio
async def test_capture_workflow_outcome_supports_repeated_success_suggestion(manager: MemoryManager):
    task = "把 Excel 工作簿转换成 CSV 并保存到 vault"
    events = [
        RunEvent(
            event_id="evt-tool-call",
            run_id="run-1",
            event_type="tool_call",
            data={"call_id": "call-1", "tool": "excel_to_csv"},
        ),
        RunEvent(
            event_id="evt-tool-result",
            run_id="run-1",
            event_type="tool_result",
            data={"call_id": "call-1", "success": True, "output": "已生成 reports/result.md"},
        ),
    ]

    for idx in range(3):
        payload = await manager.capture_workflow_outcome(
            task=task,
            result="CSV 已生成，保存在 reports/result.md",
            events=events,
            success=True,
            session_id="session-1",
            run_id=f"run-{idx + 1}",
        )
        assert payload["saved"] == 1
        assert payload["successful_tools"] == ["excel_to_csv"]

    recent = manager._memory.list_recent(limit=5, kind="workflow_success")
    assert len(recent) == 3
    assert all(entry.metadata.get("task_signature") for entry in recent)
    assert all(entry.metadata.get("run_id") for entry in recent)

    suggestion = manager.suggest_evolution_opportunity(task=task)
    assert suggestion is not None
    assert suggestion["kind"] == "skill_candidate"
    assert suggestion["occurrence_count"] == 3
    assert "excel_to_csv" in suggestion["recommended_tools"]
    assert suggestion["suggested_skill_id"]


@pytest.mark.asyncio
async def test_rule_based_flush(manager: MemoryManager):
    messages = [
        {"role": "user", "content": "这是一个很重要的技术决策，我们决定使用 Qwen3-Max 作为主力模型"},
        {"role": "assistant", "content": "好的，已记录。"},
    ]
    result = await manager._rule_based_flush(messages)
    assert result["saved"] >= 1


@pytest.mark.asyncio
async def test_promote_session_to_medical_knowledge_writes_l2_l3_l4_and_weekly_summary(
    memory: EpisodicMemory,
    retrieval: RetrievalIndex,
    vault_path: Path,
    session_store: SessionStore,
    document_service: DocumentService,
):
    provider = _MedicalPromotionProvider()
    manager = MemoryManager(
        memory=memory,
        retrieval=retrieval,
        vault_path=vault_path,
        provider=provider,
        session_store=session_store,
        document_service=document_service,
    )
    session = session_store.create_session("user-1", "feishu:chat-1", summary="讨论 BF 漏电流")
    session_store.add_event(session.session_id, "user", "请把 BF 类患者漏电流限值和判定路径整理出来。")
    session_store.add_event(session.session_id, "assistant", "已整理，并形成法规核对与内部映射的决策路径。")

    result = await manager.promote_session_to_medical_knowledge(session_id=session.session_id)

    assert result["promoted"] is True
    assert result["l2_saved"] == 1
    assert result["l3_written"] == 1
    assert result["l4_written"] == 1
    assert result["weekly_updated"] is True
    entries = memory.list_recent(limit=5)
    assert entries[0].summary == "BF 类患者漏电流限值需要单独核对"
    assert (vault_path / "knowledge" / "medical-device-engineering" / "06_工作记录" / "技术决策记录" / "BF-类患者漏电流判定路径.md").exists()
    assert (vault_path / "knowledge" / "medical-device-engineering" / "01_法规与标准" / "BF-类患者漏电流限值.md").exists()
    weekly_files = list((vault_path / "knowledge" / "medical-device-engineering" / "06_工作记录" / "对话周报").glob("*.md"))
    assert weekly_files
    promoted_session = session_store.get_session(session.session_id)
    assert promoted_session is not None
    assert promoted_session.metadata["medical_kb_promotion"]["last_event_count"] == 2


@pytest.mark.asyncio
async def test_promote_session_to_medical_knowledge_preserves_conflicts(
    memory: EpisodicMemory,
    retrieval: RetrievalIndex,
    vault_path: Path,
    session_store: SessionStore,
    document_service: DocumentService,
):
    provider = _MedicalPromotionProvider()
    manager = MemoryManager(
        memory=memory,
        retrieval=retrieval,
        vault_path=vault_path,
        provider=provider,
        session_store=session_store,
        document_service=document_service,
    )
    existing_path = "knowledge/medical-device-engineering/01_法规与标准/BF-类患者漏电流限值.md"
    await document_service.materialize_page(
        relative_path=existing_path,
        content="# BF 类患者漏电流限值\n\n旧版本内容。\n",
        title="BF 类患者漏电流限值",
        backup_existing=False,
    )

    session = session_store.create_session("user-1", "feishu:chat-1", summary="再次讨论 BF 漏电流")
    session_store.add_event(session.session_id, "user", "再确认一次 BF 类患者漏电流限值，形成正式知识。")
    session_store.add_event(session.session_id, "assistant", "已给出更新版说明。")

    result = await manager.promote_session_to_medical_knowledge(session_id=session.session_id)

    assert result["conflicts"] >= 1
    incoming_variants = list((vault_path / "knowledge" / "medical-device-engineering" / "01_法规与标准").glob("BF-类患者漏电流限值-incoming-*.md"))
    conflict_records = list((vault_path / "knowledge" / "medical-device-engineering" / "06_工作记录" / "同步冲突").glob("*.md"))
    assert incoming_variants
    assert conflict_records


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


def test_parse_medical_promotion_payload():
    payload = MemoryManager._parse_medical_promotion_payload(
        '```json\n{"medical_relevant": true, "l2_memories": [], "l3_entries": [], "l4_entries": [], "weekly_summary": {}}\n```'
    )
    assert payload["medical_relevant"] is True
    assert payload["l2_memories"] == []
