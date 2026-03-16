from __future__ import annotations

from nexus.api import build_runtime
from nexus.shared import find_project_root, load_nexus_settings


def test_builtin_web_skill_is_visible_in_runtime_catalog():
    settings = load_nexus_settings(find_project_root())
    runtime = build_runtime(settings=settings)

    skill_ids = {skill["skill_id"] for skill in runtime.skill_manager.list_skills()}
    assert "web-research" in skill_ids

    descriptions = runtime.skill_manager.get_skill_descriptions()
    assert "web-research" in descriptions

    content = runtime.skill_manager.get_skill_content("web-research")
    assert "search_web" in content
    assert "browser_navigate" in content
    assert "browser_extract_text" in content

