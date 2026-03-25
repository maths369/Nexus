from __future__ import annotations

import asyncio

from nexus.evolution import AuditLog, Sandbox, SkillManager


def _write_installable_skill(base, skill_id: str, *, keywords: list[str] | None = None) -> None:
    """统一写入 SKILL.md（单文件模式，元数据全部在 frontmatter 中）。"""
    skill_dir = base / skill_id
    skill_dir.mkdir(parents=True)
    kw_lines = "".join(f"  - {item}\n" for item in (keywords or []))
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"name: {skill_id}\n"
        "description: 测试 installable skill\n"
        "tags:\n"
        "  - test\n"
        "keywords:\n"
        f"{kw_lines}"
        "verify_imports:\n"
        "  - json\n"
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


def _write_openclaw_skill(base, skill_id: str) -> None:
    """写入 OpenClaw 格式的 SKILL.md。"""
    skill_dir = base / skill_id
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        '---\n'
        f'name: {skill_id}\n'
        f'description: OpenClaw test skill for {skill_id}\n'
        'metadata:\n'
        '  {\n'
        '    "openclaw":\n'
        '      {\n'
        '        "emoji": "📄",\n'
        '        "tags": ["pdf", "document"],\n'
        '        "requires": { "bins": ["nano-pdf"] },\n'
        '        "install":\n'
        '          [\n'
        '            {\n'
        '              "kind": "uv",\n'
        '              "package": "nano-pdf",\n'
        '              "bins": ["nano-pdf"],\n'
        '            },\n'
        '          ],\n'
        '      },\n'
        '  }\n'
        '---\n\n'
        '# Test OpenClaw Skill\n\n'
        'Use nano-pdf to edit PDFs.\n',
        encoding="utf-8",
    )


def test_openclaw_format_skill_is_parsed(tmp_path):
    """OpenClaw 格式的 SKILL.md 能被正确解析。"""
    manager, catalog_dir = _make_manager(tmp_path)
    _write_openclaw_skill(catalog_dir, "nano-pdf")

    items = manager.list_installable_skills()

    assert len(items) == 1
    skill = items[0]
    assert skill["skill_id"] == "nano-pdf"
    assert skill["tags"] == ["pdf", "document"]
    assert skill["packages"] == ["nano-pdf"]
    assert "which nano-pdf" in skill["verify_commands"]


def test_openclaw_format_query_matches(tmp_path):
    """OpenClaw 格式的技能可以通过关键词搜索到。"""
    manager, catalog_dir = _make_manager(tmp_path)
    _write_openclaw_skill(catalog_dir, "nano-pdf")
    _write_installable_skill(catalog_dir, "database-ops", keywords=["sql", "数据库"])

    items = manager.list_installable_skills(query="pdf document")

    assert items
    assert items[0]["skill_id"] == "nano-pdf"


def test_openclaw_and_nexus_formats_coexist(tmp_path):
    """OpenClaw 格式和 Nexus 原生格式可以共存。"""
    manager, catalog_dir = _make_manager(tmp_path)
    _write_openclaw_skill(catalog_dir, "nano-pdf")
    _write_installable_skill(catalog_dir, "office-conversion", keywords=["ppt", "pdf", "转换"])

    items = manager.list_installable_skills()
    skill_ids = {item["skill_id"] for item in items}

    assert "nano-pdf" in skill_ids
    assert "office-conversion" in skill_ids
