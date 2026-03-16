from __future__ import annotations

from nexus.api import build_runtime
from nexus.shared import find_project_root, load_nexus_settings


def test_builtin_document_skills_are_visible_in_runtime_catalog():
    settings = load_nexus_settings(find_project_root())
    runtime = build_runtime(settings=settings)

    skill_ids = {skill["skill_id"] for skill in runtime.skill_manager.list_skills()}
    assert "page-authoring" in skill_ids
    assert "meeting-minutes" in skill_ids
    assert "knowledge-capture" in skill_ids
    assert "vault-page-management" in skill_ids

    descriptions = runtime.skill_manager.get_skill_descriptions()
    assert "page-authoring" in descriptions
    assert "meeting-minutes" in descriptions
    assert "knowledge-capture" in descriptions
    assert "vault-page-management" in descriptions

    content = runtime.skill_manager.get_skill_content("page-authoring")
    assert "document_append_block" in content
    assert "document_replace_section" in content
    assert "document_insert_checklist" in content

    vault_content = runtime.skill_manager.get_skill_content("vault-page-management")
    assert "find_vault_pages" in vault_content
    assert "delete_page" in vault_content
