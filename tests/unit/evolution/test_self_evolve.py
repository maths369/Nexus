"""自进化能力测试 — Agent 创建/更新/列表 Skill 的闭环"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from nexus.evolution import AuditLog, Sandbox, SkillManager
from nexus.evolution.types import ChangeResult


def _make_manager(tmp_path) -> SkillManager:
    """构建用于测试的 SkillManager"""
    skills_dir = tmp_path / "skills"
    sandbox = Sandbox(tmp_path / "staging")
    audit = AuditLog(tmp_path / "audit.db")
    return SkillManager(skills_dir, sandbox, audit)


# ---------------------------------------------------------------------------
# create_skill — 创建指令型 Skill
# ---------------------------------------------------------------------------

def test_create_skill_success(tmp_path):
    """Agent 成功创建指令型 Skill"""
    mgr = _make_manager(tmp_path)

    result = mgr.create_skill(
        skill_id="excel-processing",
        name="Excel 处理",
        description="使用 pandas/openpyxl 处理 Excel 文件",
        body="# Excel 处理\n\n当用户需要处理 Excel 时，使用 background_run 执行 Python 脚本。\n\n## 步骤\n1. 确认文件路径\n2. 用 pandas 读取\n3. 转换/分析",
        tags="data, excel, pandas",
    )

    assert result.success
    assert "已创建" in result.reason

    # 文件应存在
    skill_md = tmp_path / "skills" / "excel-processing" / "SKILL.md"
    assert skill_md.exists()

    # 内容应包含 frontmatter
    content = skill_md.read_text(encoding="utf-8")
    assert "name: Excel 处理" in content
    assert "description: 使用 pandas/openpyxl 处理 Excel 文件" in content
    assert "data" in content and "excel" in content and "pandas" in content
    assert "background_run" in content


def test_create_skill_shows_in_list(tmp_path):
    """创建的 Skill 应出现在 list_skills 中"""
    mgr = _make_manager(tmp_path)

    mgr.create_skill(
        skill_id="csv-tool",
        name="CSV 工具",
        description="处理 CSV 文件",
        body="# CSV\n使用 Python csv 模块。",
    )

    skills = mgr.list_skills()
    ids = [s["skill_id"] for s in skills]
    assert "csv-tool" in ids


def test_create_skill_injectable_via_layer1(tmp_path):
    """创建后 Layer 1 应能注入描述"""
    mgr = _make_manager(tmp_path)

    mgr.create_skill(
        skill_id="json-transform",
        name="JSON 转换",
        description="在 JSON/YAML/TOML 格式间转换",
        body="# JSON 转换\n用 Python 内置 json 模块。",
        tags="data, json",
    )

    descriptions = mgr.get_skill_descriptions()
    assert "JSON 转换" in descriptions
    assert "在 JSON/YAML/TOML 格式间转换" in descriptions


def test_create_skill_injectable_via_layer2(tmp_path):
    """创建后 Layer 2 应能加载完整内容"""
    mgr = _make_manager(tmp_path)

    mgr.create_skill(
        skill_id="web-scraper",
        name="网页抓取",
        description="使用浏览器工具抓取网页数据",
        body="# 网页抓取\n\n## 使用 browser_navigate\n先导航到目标页面。\n\n## 使用 browser_extract_text\n提取关键信息。",
    )

    content = mgr.get_skill_content("web-scraper")
    assert "<skill" in content
    assert "browser_navigate" in content
    assert "browser_extract_text" in content


def test_create_skill_duplicate_rejected(tmp_path):
    """重复创建应被拒绝"""
    mgr = _make_manager(tmp_path)

    mgr.create_skill(
        skill_id="dup-skill",
        name="重复技能",
        description="测试",
        body="# 内容",
    )

    result = mgr.create_skill(
        skill_id="dup-skill",
        name="重复技能2",
        description="测试2",
        body="# 新内容",
    )
    assert not result.success
    assert "已存在" in result.reason


def test_create_skill_invalid_id_rejected(tmp_path):
    """非法 skill_id 被拒绝"""
    mgr = _make_manager(tmp_path)

    # 太短
    result = mgr.create_skill(
        skill_id="ab",
        name="短ID",
        description="测试",
        body="# 内容",
    )
    assert not result.success
    assert "无效" in result.reason

    # 包含大写
    result = mgr.create_skill(
        skill_id="Excel-Tool",
        name="大写ID",
        description="测试",
        body="# 内容",
    )
    assert not result.success

    # 以连字符开头
    result = mgr.create_skill(
        skill_id="-bad-start",
        name="坏开头",
        description="测试",
        body="# 内容",
    )
    assert not result.success


def test_create_skill_audit_recorded(tmp_path):
    """创建操作应记录审计日志"""
    mgr = _make_manager(tmp_path)
    audit = mgr._audit

    mgr.create_skill(
        skill_id="audit-test",
        name="审计测试",
        description="测试审计",
        body="# 内容",
    )

    entries = audit.query(action="skill_created")
    assert len(entries) == 1
    assert entries[0].target == "audit-test"
    assert entries[0].actor == "agent"


# ---------------------------------------------------------------------------
# update_skill — 更新已有 Skill
# ---------------------------------------------------------------------------

def test_update_skill_body(tmp_path):
    """更新 Skill 正文"""
    mgr = _make_manager(tmp_path)

    mgr.create_skill(
        skill_id="updatable",
        name="可更新",
        description="原始描述",
        body="# 原始内容",
    )

    result = mgr.update_skill(
        skill_id="updatable",
        body="# 新内容\n\n增加了更多步骤。",
    )
    assert result.success
    assert result.backup_id  # 应有备份ID

    # 验证新内容
    content = mgr.get_skill_content("updatable")
    assert "新内容" in content
    assert "更多步骤" in content


def test_update_skill_preserves_unmodified_fields(tmp_path):
    """只更新指定字段，保留其他字段"""
    mgr = _make_manager(tmp_path)

    mgr.create_skill(
        skill_id="preserve-test",
        name="保留测试",
        description="原始描述",
        body="# 原始内容",
        tags="original",
    )

    # 只更新 description
    mgr.update_skill(skill_id="preserve-test", description="新描述")

    # body 应保留
    content = mgr.get_skill_content("preserve-test")
    assert "原始内容" in content

    # description 应更新
    skill_md = tmp_path / "skills" / "preserve-test" / "SKILL.md"
    text = skill_md.read_text(encoding="utf-8")
    assert "description: 新描述" in text


def test_update_skill_creates_backup(tmp_path):
    """更新应创建备份文件"""
    mgr = _make_manager(tmp_path)

    mgr.create_skill(
        skill_id="backup-test",
        name="备份测试",
        description="测试",
        body="# V1",
    )

    result = mgr.update_skill(skill_id="backup-test", body="# V2")

    # 备份文件应存在
    skill_dir = tmp_path / "skills" / "backup-test"
    backups = list(skill_dir.glob("SKILL.md.bak.*"))
    assert len(backups) == 1
    assert "V1" in backups[0].read_text(encoding="utf-8")


def test_update_skill_nonexistent_rejected(tmp_path):
    """更新不存在的 Skill 被拒绝"""
    mgr = _make_manager(tmp_path)

    result = mgr.update_skill(skill_id="nonexistent", body="# 新内容")
    assert not result.success
    assert "不存在" in result.reason


def test_update_skill_audit_recorded(tmp_path):
    """更新操作应记录审计日志"""
    mgr = _make_manager(tmp_path)
    audit = mgr._audit

    mgr.create_skill(
        skill_id="audit-update",
        name="审计更新",
        description="测试",
        body="# 原始",
    )

    mgr.update_skill(skill_id="audit-update", body="# 新版")

    entries = audit.query(action="skill_updated")
    assert len(entries) == 1
    assert entries[0].target == "audit-update"
    assert "body" in entries[0].details["updated_fields"]


# ---------------------------------------------------------------------------
# 闭环场景测试
# ---------------------------------------------------------------------------

def test_full_evolution_loop(tmp_path):
    """
    完整闭环: 创建 → 列表可见 → Layer1 注入 → Layer2 加载 → 更新 → 再加载
    模拟 Agent 发现能力缺口 → 创建 Skill → 使用 → 改进
    """
    mgr = _make_manager(tmp_path)

    # Step 1: Agent 创建 Skill
    result = mgr.create_skill(
        skill_id="image-analysis",
        name="图片分析",
        description="通过 Python PIL 分析图片元数据和基本属性",
        body="# 图片分析\n\n用 `background_run` 执行:\n```python\nfrom PIL import Image\nimg = Image.open(path)\nprint(img.size, img.format)\n```",
        tags="image, analysis",
    )
    assert result.success

    # Step 2: 列表应可见
    skills = mgr.list_skills()
    assert any(s["skill_id"] == "image-analysis" for s in skills)

    # Step 3: Layer 1 注入
    descs = mgr.get_skill_descriptions()
    assert "图片分析" in descs

    # Step 4: Layer 2 完整加载
    content = mgr.get_skill_content("image-analysis")
    assert "PIL" in content
    assert "background_run" in content

    # Step 5: Agent 改进 Skill（加入更多方法）
    result = mgr.update_skill(
        skill_id="image-analysis",
        body=(
            "# 图片分析 v2\n\n"
            "## 基本信息\n"
            "```python\nfrom PIL import Image\n```\n\n"
            "## EXIF 数据\n"
            "```python\nfrom PIL.ExifTags import TAGS\n```\n\n"
            "## OCR 文字识别\n"
            "如需 OCR，安装 pytesseract。"
        ),
    )
    assert result.success

    # Step 6: 再次加载应包含新内容
    content_v2 = mgr.get_skill_content("image-analysis")
    assert "EXIF" in content_v2
    assert "OCR" in content_v2


def test_multiple_skills_coexist(tmp_path):
    """多个 Agent 创建的 Skill 共存"""
    mgr = _make_manager(tmp_path)

    for i, (sid, name, desc) in enumerate([
        ("pdf-extract", "PDF 提取", "从 PDF 中提取文本"),
        ("excel-convert", "Excel 转换", "Excel 与 CSV 互转"),
        ("api-caller", "API 调用", "通用 REST API 调用模式"),
    ]):
        mgr.create_skill(
            skill_id=sid, name=name, description=desc,
            body=f"# {name}\n内容...",
        )

    skills = mgr.list_skills()
    assert len(skills) == 3

    descs = mgr.get_skill_descriptions()
    assert "PDF 提取" in descs
    assert "Excel 转换" in descs
    assert "API 调用" in descs
