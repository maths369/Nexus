from __future__ import annotations

import asyncio
import sys
import yaml

from nexus.api import build_runtime
from nexus.agent.tool_profiles import ToolProfile
from nexus.agent.types import ToolRiskLevel
from nexus.provider import ProviderConfig
from nexus.shared import find_project_root, load_nexus_settings


def test_build_runtime_creates_core_stores_and_services(tmp_path):
    runtime = build_runtime(
        tmp_path,
        primary_provider=ProviderConfig(name="qwen", model="qwen-max"),
    )

    assert runtime.paths.vault.is_dir()
    assert runtime.paths.sqlite.is_dir()
    assert runtime.paths.skill_registry.is_dir()
    assert runtime.paths.capabilities.is_dir()
    assert runtime.session_store.get_recent_sessions("user-x") == []
    assert runtime.document_service is not None
    assert runtime.ingest_service is not None
    assert runtime.document_editor is not None
    assert runtime.audio_service is not None
    assert runtime.provider.get_provider().name == "qwen"
    assert runtime.session_manager is not None
    assert runtime.heartbeat_engine is not None
    assert runtime.transcript_writer is not None
    assert runtime.transcript_store is not None
    assert runtime.subagent_registry is not None
    assert (runtime.paths.vault / "_system" / "heartbeat.md").exists()
    assert runtime.run_manager._transcript_writer is runtime.transcript_writer  # noqa: SLF001


def test_build_runtime_wires_voiceprint_store_to_diarization_engine(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "app.yaml").write_text(
        yaml.safe_dump(
            {
                "audio": {
                    "diarization": {
                        "enabled": True,
                        "similarity_threshold": 0.72,
                    }
                }
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    settings = load_nexus_settings(tmp_path)
    runtime = build_runtime(
        settings=settings,
        primary_provider=ProviderConfig(name="qwen", model="qwen-max"),
    )

    assert runtime.audio_service._diarization is not None  # noqa: SLF001
    assert runtime.audio_service._voiceprint_store is not None  # noqa: SLF001
    assert runtime.audio_service._voiceprint_store._embedding_extractor is runtime.audio_service._diarization  # noqa: SLF001
    assert (runtime.paths.vault / "_system" / "voiceprints").is_dir()


def test_build_runtime_wires_core_agent_capabilities():
    settings = load_nexus_settings(find_project_root())
    runtime = build_runtime(
        settings=settings,
        primary_provider=ProviderConfig(name="qwen", model="qwen-max"),
    )

    tool_names = {tool.name for tool in runtime.available_tools}
    assert "compact" in tool_names
    assert "load_skill" in tool_names
    assert "skill_list_installable" in tool_names
    assert "skill_install" in tool_names
    assert "skill_search_remote" in tool_names
    assert "skill_import_local" in tool_names
    assert "skill_import_remote" in tool_names
    assert "todo_write" in tool_names
    assert "dispatch_subagent" in tool_names
    assert "task_create" in tool_names
    assert "task_update" in tool_names
    assert "task_list" in tool_names
    assert "task_get" in tool_names
    assert "background_run" in tool_names
    assert "check_background" in tool_names
    assert "system_run" in tool_names
    assert "read_local_file" in tool_names
    assert "code_read_file" in tool_names
    assert "write_local_file" in tool_names
    assert "file_write" in tool_names
    assert "file_edit" in tool_names
    assert "file_search" in tool_names
    assert "audio_transcribe_path" in tool_names
    assert "audio_materialize_transcript" in tool_names
    assert "voiceprint_register" in tool_names
    assert "voiceprint_list" in tool_names
    assert "voiceprint_delete" in tool_names
    assert "list_vault_pages" in tool_names
    assert "find_vault_pages" in tool_names
    assert "document_append_block" in tool_names
    assert "document_replace_section" in tool_names
    assert "document_insert_checklist" in tool_names
    assert "document_insert_table" in tool_names
    assert "document_insert_page_link" in tool_names
    assert "document_create_database" in tool_names
    assert "delete_page" in tool_names
    browser_tools = {
        "browser_navigate",
        "browser_extract_text",
        "browser_screenshot",
        "browser_fill_form",
    }
    if settings.browser_enabled:
        assert browser_tools <= tool_names
        assert {"search_web", "search_web_structured"} <= tool_names
    else:
        assert browser_tools.isdisjoint(tool_names)
        assert {"search_web", "search_web_structured"}.isdisjoint(tool_names)
    assert "skill_create" in tool_names
    assert "skill_update" in tool_names
    assert "skill_list_installed" in tool_names
    assert "evolution_audit" in tool_names
    assert "capability_list_available" in tool_names
    assert "capability_status" in tool_names
    assert "capability_enable" in tool_names
    assert "capability_create" in tool_names
    assert "capability_register" in tool_names
    assert "capability_stage" in tool_names
    assert "capability_verify" in tool_names
    assert "capability_promote" in tool_names
    assert "capability_rollback" in tool_names
    assert "excel_list_sheets" in tool_names
    assert "excel_to_csv" in tool_names

    assert runtime.attempt_builder._skill_manager is runtime.skill_manager  # noqa: SLF001
    assert runtime.run_manager._compressor is runtime.compressor  # noqa: SLF001
    assert runtime.run_manager._todo_manager is runtime.todo_manager  # noqa: SLF001
    assert runtime.run_manager._background_manager is runtime.background_manager  # noqa: SLF001
    assert runtime.run_manager._capability_promotion_advisor is runtime.capability_promotion_advisor  # noqa: SLF001
    assert runtime.run_manager._skill_manager is runtime.skill_manager  # noqa: SLF001


def test_project_runtime_allowlist_exposes_evolution_tools():
    settings = load_nexus_settings(find_project_root())
    runtime = build_runtime(
        settings=settings,
        primary_provider=ProviderConfig(name="qwen", model="qwen-max"),
    )

    tool_names = {tool.name for tool in runtime.available_tools}
    assert {
        "skill_list_installable",
        "skill_install",
        "skill_search_remote",
        "skill_import_local",
        "skill_import_remote",
        "skill_create",
        "skill_update",
        "skill_list_installed",
        "evolution_audit",
    } <= tool_names
    assert {
        "capability_list_available",
        "capability_status",
        "capability_enable",
        "capability_create",
        "capability_register",
        "capability_stage",
        "capability_verify",
        "capability_promote",
        "capability_rollback",
    } <= tool_names
    assert {"list_vault_pages", "find_vault_pages", "delete_page"} <= tool_names
    assert {
        "memory_read_identity",
        "memory_update_user",
        "memory_update_soul",
        "memory_daily_log",
        "memory_read_journal",
        "memory_list_journals",
        "memory_reindex",
        "memory_sync",
        "memory_suggest_evolution",
    } <= tool_names

    assert runtime.attempt_builder._memory_manager is runtime.memory_manager  # noqa: SLF001
    assert runtime.compressor._memory_flush_callback is not None  # noqa: SLF001
    assert runtime.run_manager._memory_manager is runtime.memory_manager  # noqa: SLF001


def test_project_runtime_coding_profile_keeps_actual_code_tools():
    settings = load_nexus_settings(find_project_root())
    runtime = build_runtime(
        settings=settings,
        primary_provider=ProviderConfig(name="qwen", model="qwen-max"),
    )

    coding_names = {tool.name for tool in ToolProfile.coding().filter(runtime.available_tools)}
    assert {
        "list_local_files",
        "code_read_file",
        "file_write",
        "file_edit",
        "file_search",
        "system_run",
        "background_run",
        "check_background",
        "dispatch_subagent",
    } <= coding_names


def test_project_runtime_uses_sanitized_evolution_python():
    settings = load_nexus_settings(find_project_root())
    runtime = build_runtime(
        settings=settings,
        primary_provider=ProviderConfig(name="qwen", model="qwen-max"),
    )

    assert runtime.capability_manager._python == settings.evolution_python_executable  # noqa: SLF001
    assert runtime.capability_manager._python == sys.executable  # noqa: SLF001


def test_project_runtime_loads_capabilities_from_manifests():
    settings = load_nexus_settings(find_project_root())
    runtime = build_runtime(
        settings=settings,
        primary_provider=ProviderConfig(name="qwen", model="qwen-max"),
    )

    capabilities = {item["capability_id"]: item for item in runtime.capability_manager.list_capabilities()}
    assert "excel_processing" in capabilities
    assert capabilities["excel_processing"]["manifest_path"].endswith(
        "capabilities/excel_processing/CAPABILITY.yaml"
    )


def test_project_runtime_loads_installable_skills_from_registry():
    settings = load_nexus_settings(find_project_root())
    runtime = build_runtime(
        settings=settings,
        primary_provider=ProviderConfig(name="qwen", model="qwen-max"),
    )

    installable = {item["skill_id"]: item for item in runtime.skill_manager.list_installable_skills()}
    assert "office-conversion" in installable
    assert "notebooklm-integration" in installable
    assert installable["office-conversion"]["manifest_path"].endswith(
        "skill_registry/office-conversion/SKILL.md"
    )


def test_project_runtime_matches_notebooklm_query_to_installable_skill():
    settings = load_nexus_settings(find_project_root())
    runtime = build_runtime(
        settings=settings,
        primary_provider=ProviderConfig(name="qwen", model="qwen-max"),
    )

    matched = [item["skill_id"] for item in runtime.skill_manager.list_installable_skills(query="连接NotebookLM")]
    assert "notebooklm-integration" in matched


def test_list_local_files_can_fallback_to_vault_section_alias(tmp_path):
    runtime = build_runtime(
        tmp_path,
        primary_provider=ProviderConfig(name="qwen", model="qwen-max"),
    )
    asyncio.run(
        runtime.document_service.create_page(
            title="日志 2026-03-11",
            body="# 日志 2026-03-11\n\n内容",
            section="pages",
        )
    )

    tool_map = {tool.name: tool for tool in runtime.available_tools}
    payload = asyncio.run(tool_map["list_local_files"].handler(path="pages"))

    assert '"mode": "vault_section_alias"' in payload
    assert "日志 2026-03-11" in payload


def test_build_runtime_disables_risk_controls_in_testing_mode(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "app.yaml").write_text(
        yaml.safe_dump(
            {
                "tool_policy": {
                    "enabled": True,
                    "testing_disable_risk_controls": True,
                    "allowlist": ["read_vault"],
                }
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    settings = load_nexus_settings(tmp_path)
    runtime = build_runtime(
        settings=settings,
        primary_provider=ProviderConfig(name="qwen", model="qwen-max"),
    )

    assert settings.disable_risk_controls_for_testing is True
    assert runtime.tools_policy._whitelist is None  # noqa: SLF001
    assert runtime.tools_policy._auto_approve == {  # noqa: SLF001
        ToolRiskLevel.LOW,
        ToolRiskLevel.MEDIUM,
        ToolRiskLevel.HIGH,
        ToolRiskLevel.CRITICAL,
    }
