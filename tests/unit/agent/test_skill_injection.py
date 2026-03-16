"""Skill 运行时注入测试 — 两层注入 (描述 + 按需加载)"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from nexus.evolution import AuditLog, Sandbox, SkillManager


def _make_skill(skills_dir: Path, skill_id: str, *, name: str = "", desc: str = "", body: str = "", tags: str = "") -> Path:
    """创建一个测试 skill 目录"""
    skill_path = skills_dir / skill_id
    skill_path.mkdir(parents=True, exist_ok=True)

    # 创建 SKILL.md
    frontmatter_parts = []
    if name:
        frontmatter_parts.append(f"name: {name}")
    if desc:
        frontmatter_parts.append(f"description: {desc}")
    if tags:
        frontmatter_parts.append(f"tags: {tags}")

    if frontmatter_parts:
        frontmatter = "---\n" + "\n".join(frontmatter_parts) + "\n---\n"
    else:
        frontmatter = ""

    (skill_path / "SKILL.md").write_text(
        f"{frontmatter}{body or f'{skill_id} skill body content'}",
        encoding="utf-8",
    )
    (skill_path / "main.py").write_text("def run():\n    return 'ok'\n", encoding="utf-8")
    return skill_path


def _make_manager(tmp_path: Path) -> tuple[SkillManager, Path]:
    """创建 SkillManager + skills 目录"""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    sandbox = Sandbox(tmp_path / "staging")
    audit = AuditLog(tmp_path / "audit.db")
    return SkillManager(skills_dir, sandbox, audit), skills_dir


# ---------------------------------------------------------------------------
# Layer 1: get_skill_descriptions
# ---------------------------------------------------------------------------

def test_skill_descriptions_empty(tmp_path):
    """无 skill 时返回空字符串"""
    manager, _ = _make_manager(tmp_path)
    assert manager.get_skill_descriptions() == ""


def test_skill_descriptions_with_skills(tmp_path):
    """列出所有 skill 的名称和描述"""
    manager, skills_dir = _make_manager(tmp_path)
    _make_skill(skills_dir, "pdf-tool", name="pdf", desc="处理 PDF 文件", tags="文档")
    _make_skill(skills_dir, "code-review", name="code-review", desc="代码审查", tags="开发")

    descriptions = manager.get_skill_descriptions()
    assert "pdf" in descriptions
    assert "处理 PDF 文件" in descriptions
    assert "code-review" in descriptions
    assert "代码审查" in descriptions
    assert "[文档]" in descriptions


def test_skill_descriptions_fallback_to_json(tmp_path):
    """没有 SKILL.md 时从 skill.json 读取"""
    manager, skills_dir = _make_manager(tmp_path)
    skill_path = skills_dir / "simple-tool"
    skill_path.mkdir()
    (skill_path / "skill.json").write_text(
        json.dumps({"name": "simple", "description": "简单工具"}),
        encoding="utf-8",
    )
    (skill_path / "main.py").write_text("pass\n", encoding="utf-8")

    descriptions = manager.get_skill_descriptions()
    assert "simple" in descriptions
    assert "简单工具" in descriptions


# ---------------------------------------------------------------------------
# Layer 2: get_skill_content
# ---------------------------------------------------------------------------

def test_skill_content_found(tmp_path):
    """成功加载 skill 完整内容"""
    manager, skills_dir = _make_manager(tmp_path)
    _make_skill(
        skills_dir, "pdf-tool",
        name="pdf",
        desc="处理 PDF 文件",
        body="## 步骤\n1. 打开文件\n2. 解析内容\n3. 返回结果",
    )

    content = manager.get_skill_content("pdf-tool")
    assert '<skill name="pdf-tool">' in content
    assert "步骤" in content
    assert "打开文件" in content
    assert "</skill>" in content


def test_skill_content_not_found(tmp_path):
    """skill 不存在时返回错误"""
    manager, _ = _make_manager(tmp_path)
    content = manager.get_skill_content("nonexistent")
    assert "Error" in content
    assert "nonexistent" in content


def test_skill_content_json_fallback(tmp_path):
    """没有 SKILL.md 时从 skill.json 加载"""
    manager, skills_dir = _make_manager(tmp_path)
    skill_path = skills_dir / "json-only"
    skill_path.mkdir()
    (skill_path / "skill.json").write_text(
        '{"name": "json-skill", "version": "1.0"}',
        encoding="utf-8",
    )

    content = manager.get_skill_content("json-only")
    assert '<skill name="json-only">' in content
    assert "json-skill" in content


# ---------------------------------------------------------------------------
# Frontmatter 解析
# ---------------------------------------------------------------------------

def test_parse_frontmatter():
    """YAML frontmatter 正确解析"""
    text = "---\nname: my-skill\ndescription: 做某事\ntags: utils, helper\n---\n这是正文。"
    meta, body = SkillManager._parse_frontmatter(text)
    assert meta["name"] == "my-skill"
    assert meta["description"] == "做某事"
    assert meta["tags"] == "utils, helper"
    assert body == "这是正文。"


def test_parse_frontmatter_no_delimiter():
    """没有 frontmatter 分隔符时返回全文作为 body"""
    text = "这是全部内容，没有 frontmatter。"
    meta, body = SkillManager._parse_frontmatter(text)
    assert meta == {}
    assert body == text


def test_skill_descriptions_exclude_backups(tmp_path):
    """描述列表排除备份目录"""
    manager, skills_dir = _make_manager(tmp_path)
    _make_skill(skills_dir, "active-tool", name="active", desc="活跃工具")

    # 模拟备份目录
    backup = skills_dir / "old-tool.uninstalled.abc12345"
    backup.mkdir()
    (backup / "SKILL.md").write_text("---\nname: old\n---\nold", encoding="utf-8")

    descriptions = manager.get_skill_descriptions()
    assert "active" in descriptions
    assert "old" not in descriptions
