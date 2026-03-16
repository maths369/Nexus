from __future__ import annotations

import asyncio

from nexus.evolution import AuditLog, Sandbox, SkillManager


def _write_installable_skill(base, skill_id: str, *, keywords: list[str] | None = None) -> None:
    skill_dir = base / skill_id
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.yaml").write_text(
        "\n".join(
            [
                f"id: {skill_id}",
                f"name: {skill_id}",
                "description: 测试 installable skill",
                "tags:",
                "  - test",
                "keywords:",
                *[f"  - {item}" for item in (keywords or [])],
                "packages: []",
                "verify_imports:",
                "  - json",
                "verify_commands: []",
                "install_commands: []",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"name: {skill_id}\n"
        "description: 测试 installable skill\n"
        "tags: test\n"
        "---\n\n"
        "# Test Skill\n\n"
        "Use this skill when the task matches the registry entry.\n",
        encoding="utf-8",
    )


def _make_manager(tmp_path):
    skills_dir = tmp_path / "skills"
    catalog_dir = tmp_path / "skill_registry"
    sandbox = Sandbox(tmp_path / "staging")
    audit = AuditLog(tmp_path / "audit.db")
    return SkillManager(
        skills_dir,
        sandbox,
        audit,
        catalog_dir=catalog_dir,
    ), catalog_dir


def test_list_installable_skills_returns_registry_entries(tmp_path):
    manager, catalog_dir = _make_manager(tmp_path)
    _write_installable_skill(catalog_dir, "office-conversion", keywords=["ppt", "pdf", "转换"])
    _write_installable_skill(catalog_dir, "api-integration", keywords=["api", "webhook"])

    items = manager.list_installable_skills()
    skill_ids = {item["skill_id"] for item in items}

    assert "office-conversion" in skill_ids
    assert "api-integration" in skill_ids


def test_list_installable_skills_query_ranks_matching_entry(tmp_path):
    manager, catalog_dir = _make_manager(tmp_path)
    _write_installable_skill(catalog_dir, "office-conversion", keywords=["ppt", "pdf", "转换"])
    _write_installable_skill(catalog_dir, "database-ops", keywords=["sql", "数据库"])

    items = manager.list_installable_skills(query="把PPT转换为PDF")

    assert items
    assert items[0]["skill_id"] == "office-conversion"
    assert items[0]["match_score"] > 0


def test_install_from_catalog_installs_skill_bundle(tmp_path):
    manager, catalog_dir = _make_manager(tmp_path)
    _write_installable_skill(catalog_dir, "office-conversion", keywords=["ppt", "pdf", "转换"])

    result = asyncio.run(manager.install_from_catalog("office-conversion", actor="agent"))

    assert result["success"] is True
    assert result["installed"] is True
    assert (tmp_path / "skills" / "office-conversion" / "SKILL.md").exists()
    entries = manager._audit.query(action="skill_installed")  # noqa: SLF001
    assert any(entry.target == "office-conversion" for entry in entries)


def test_install_from_catalog_missing_skill_returns_failure(tmp_path):
    manager, _ = _make_manager(tmp_path)

    result = asyncio.run(manager.install_from_catalog("missing-skill", actor="agent"))

    assert result["success"] is False
    assert "not found" in result["reason"]
