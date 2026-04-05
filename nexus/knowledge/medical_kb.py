"""Medical-device knowledge vault layout and helper utilities."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

MEDICAL_KB_ROOT = "knowledge/medical-device-engineering"
MEDICAL_KB_SYNC_SCOPE = "feishu+vault"

MEDICAL_KB_SECTION_DIRS = {
    "index": f"{MEDICAL_KB_ROOT}/00_导航与索引",
    "regulation": f"{MEDICAL_KB_ROOT}/01_法规与标准",
    "device": f"{MEDICAL_KB_ROOT}/02_设备知识库",
    "discipline": f"{MEDICAL_KB_ROOT}/03_工程学科",
    "tools": f"{MEDICAL_KB_ROOT}/04_决策支持工具",
    "learning": f"{MEDICAL_KB_ROOT}/05_学习路径",
    "work": f"{MEDICAL_KB_ROOT}/06_工作记录",
}

MEDICAL_KB_WORK_DIRS = {
    "adr": f"{MEDICAL_KB_SECTION_DIRS['work']}/技术决策记录",
    "meeting": f"{MEDICAL_KB_SECTION_DIRS['work']}/会议纪要",
    "question": f"{MEDICAL_KB_SECTION_DIRS['work']}/待深化问题库",
    "weekly": f"{MEDICAL_KB_SECTION_DIRS['work']}/对话周报",
    "conflict": f"{MEDICAL_KB_SECTION_DIRS['work']}/同步冲突",
}

CARRY_FORWARD_RELATIVE_PATHS = [
    "_system/memory",
    "_system/voiceprints",
    "_system/heartbeat.md",
]

L3_FOLDER_ALIASES = {
    "adr": "adr",
    "decision": "adr",
    "技术决策记录": "adr",
    "技术决策记录(adr)": "adr",
    "技术决策记录（adr）": "adr",
    "meeting": "meeting",
    "meeting_notes": "meeting",
    "会议纪要": "meeting",
    "question": "question",
    "questions": "question",
    "open_question": "question",
    "待深化问题库": "question",
    "weekly": "weekly",
    "weekly_summary": "weekly",
    "周报": "weekly",
    "对话周报": "weekly",
    "conflict": "conflict",
    "同步冲突": "conflict",
}

L4_SECTION_ALIASES = {
    "00": "index",
    "00_导航与索引": "index",
    "index": "index",
    "导航": "index",
    "01": "regulation",
    "01_法规与标准": "regulation",
    "regulation": "regulation",
    "法规": "regulation",
    "标准": "regulation",
    "02": "device",
    "02_设备知识库": "device",
    "device": "device",
    "设备": "device",
    "03": "discipline",
    "03_工程学科": "discipline",
    "discipline": "discipline",
    "工程": "discipline",
    "学科": "discipline",
    "04": "tools",
    "04_决策支持工具": "tools",
    "tools": "tools",
    "工具": "tools",
    "05": "learning",
    "05_学习路径": "learning",
    "learning": "learning",
    "学习": "learning",
}


@dataclass(frozen=True)
class MedicalKbTemplate:
    relative_path: str
    title: str
    body: str
    kb_level: str
    promotion_state: str = "template"


def build_kb_id(relative_path: str) -> str:
    normalized = relative_path.strip().lower()
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]
    return f"kb-{digest}"


def build_sync_metadata(
    *,
    relative_path: str,
    kb_level: str,
    source_channel: str,
    source_session_id: str = "",
    feishu_doc_token: str = "",
    promotion_state: str = "working",
    kb_id: str | None = None,
) -> dict[str, str]:
    return {
        "kb_id": kb_id or build_kb_id(relative_path),
        "kb_level": kb_level,
        "sync_scope": MEDICAL_KB_SYNC_SCOPE,
        "feishu_doc_token": feishu_doc_token,
        "source_channel": source_channel,
        "source_session_id": source_session_id,
        "promotion_state": promotion_state,
    }


def render_markdown_document(
    *,
    title: str,
    body: str,
    metadata: dict[str, Any] | None = None,
) -> str:
    lines: list[str] = []
    stripped_body = body.strip()
    if not stripped_body.startswith("# "):
        lines.extend([f"# {title}", ""])
    if metadata:
        lines.append("<!-- metadata:")
        for key, value in metadata.items():
            lines.append(f"{key}: {value}")
        lines.extend(["-->", ""])
    if stripped_body:
        lines.append(stripped_body)
    return "\n".join(lines).rstrip() + "\n"


def slugify_title(title: str) -> str:
    stripped = re.sub(r"\s+", "-", title.strip())
    stripped = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]", "", stripped)
    return stripped[:96] or "untitled"


def normalize_l3_folder(value: str | None) -> str | None:
    key = str(value or "").strip().lower()
    return L3_FOLDER_ALIASES.get(key)


def normalize_l4_section(value: str | None) -> str | None:
    key = str(value or "").strip().lower()
    return L4_SECTION_ALIASES.get(key)


def l3_relative_path(folder: str, title: str) -> str:
    directory = MEDICAL_KB_WORK_DIRS[folder]
    return f"{directory}/{slugify_title(title)}.md"


def l4_relative_path(section: str, title: str) -> str:
    directory = MEDICAL_KB_SECTION_DIRS[section]
    return f"{directory}/{slugify_title(title)}.md"


def weekly_summary_relative_path(date: datetime | None = None) -> str:
    current = date or datetime.utcnow()
    iso_year, iso_week, _ = current.isocalendar()
    return f"{MEDICAL_KB_WORK_DIRS['weekly']}/{iso_year}-W{iso_week:02d}.md"


def conflict_relative_path(title: str, session_id: str, date: datetime | None = None) -> str:
    current = date or datetime.utcnow()
    safe_title = slugify_title(title or "同步冲突")
    return (
        f"{MEDICAL_KB_WORK_DIRS['conflict']}/"
        f"{current.strftime('%Y%m%d-%H%M%S')}-{safe_title}-{session_id[:8] or 'session'}.md"
    )


def all_medical_kb_directories() -> list[str]:
    return [
        MEDICAL_KB_ROOT,
        *MEDICAL_KB_SECTION_DIRS.values(),
        *MEDICAL_KB_WORK_DIRS.values(),
    ]


def build_scaffold_documents() -> list[MedicalKbTemplate]:
    return [
        MedicalKbTemplate(
            relative_path=f"{MEDICAL_KB_ROOT}/README.md",
            title="医疗器械工程知识库",
            kb_level="L4",
            body=(
                "## 目标\n\n"
                "- 这里存放可检索、可问答、可同步到飞书的正式医疗器械工程知识。\n"
                "- 原始对话、附件、会议转录仍留在 `inbox/`、`meetings/`、`journals/`。\n\n"
                "## 记忆分层\n\n"
                "- L0: `sessions.db` 保存原始会话。\n"
                "- L1: `_system/memory/USER.md` 与 `_system/memory/SOUL.md`。\n"
                "- L2: `_system/memory/episodic.jsonl`。\n"
                "- L3: `06_工作记录/`。\n"
                "- L4: `00_导航与索引/` 到 `05_学习路径/`。\n"
            ),
        ),
        MedicalKbTemplate(
            relative_path=f"{MEDICAL_KB_SECTION_DIRS['index']}/总览.md",
            title="知识库总览",
            kb_level="L4",
            body=(
                "## 分区说明\n\n"
                "- `01_法规与标准`: 标准条款解释、限值、适用性判断。\n"
                "- `02_设备知识库`: 设备原理、模块设计、测试方法。\n"
                "- `03_工程学科`: 方法论、流程、工程能力模型。\n"
                "- `04_决策支持工具`: Checklist、矩阵、评审模板。\n"
                "- `05_学习路径`: 学习笔记、课程化内容。\n"
                "- `06_工作记录`: ADR、会议纪要、待深化问题与周报。\n"
            ),
        ),
        MedicalKbTemplate(
            relative_path=f"{MEDICAL_KB_SECTION_DIRS['regulation']}/README.md",
            title="法规与标准",
            kb_level="L4",
            body="## 收录范围\n\n- IEC/ISO/YY/FDA/MDR 等法规标准条款解释与适用性判断。\n",
        ),
        MedicalKbTemplate(
            relative_path=f"{MEDICAL_KB_SECTION_DIRS['device']}/README.md",
            title="设备知识库",
            kb_level="L4",
            body="## 收录范围\n\n- 呼吸机、麻醉机、监护类设备的原理、模块、测试与故障模式。\n",
        ),
        MedicalKbTemplate(
            relative_path=f"{MEDICAL_KB_SECTION_DIRS['discipline']}/README.md",
            title="工程学科",
            kb_level="L4",
            body="## 收录范围\n\n- 系统工程、风险管理、V&V、质量工程、可靠性工程等通用方法。\n",
        ),
        MedicalKbTemplate(
            relative_path=f"{MEDICAL_KB_SECTION_DIRS['tools']}/README.md",
            title="决策支持工具",
            kb_level="L4",
            body="## 收录范围\n\n- Checklist、评审模板、判定矩阵、估算模板与工作表。\n",
        ),
        MedicalKbTemplate(
            relative_path=f"{MEDICAL_KB_SECTION_DIRS['learning']}/README.md",
            title="学习路径",
            kb_level="L4",
            body="## 收录范围\n\n- 学习地图、概念梳理、课程化内容与训练题。\n",
        ),
        MedicalKbTemplate(
            relative_path=f"{MEDICAL_KB_SECTION_DIRS['work']}/README.md",
            title="工作记录",
            kb_level="L3",
            body=(
                "## 用途\n\n"
                "- 这里保存尚未沉淀为正式知识的中间层文档。\n"
                "- 周报、ADR、会议纪要、待深化问题先进入这里，再决定是否提升到 L4。\n"
            ),
        ),
        MedicalKbTemplate(
            relative_path=f"{MEDICAL_KB_WORK_DIRS['adr']}/README.md",
            title="技术决策记录",
            kb_level="L3",
            body="## 用途\n\n- 记录设计取舍、边界条件、影响评估与后续动作。\n",
        ),
        MedicalKbTemplate(
            relative_path=f"{MEDICAL_KB_WORK_DIRS['meeting']}/README.md",
            title="会议纪要",
            kb_level="L3",
            body="## 用途\n\n- 保存从会议与对话中提炼出的结构化纪要。\n",
        ),
        MedicalKbTemplate(
            relative_path=f"{MEDICAL_KB_WORK_DIRS['question']}/README.md",
            title="待深化问题库",
            kb_level="L3",
            body="## 用途\n\n- 保存仍待验证、待补证据或待跨标准确认的问题。\n",
        ),
        MedicalKbTemplate(
            relative_path=f"{MEDICAL_KB_WORK_DIRS['weekly']}/README.md",
            title="对话周报",
            kb_level="L3",
            body="## 用途\n\n- 汇总飞书对话中重复出现的主题、决策和待办。\n",
        ),
        MedicalKbTemplate(
            relative_path=f"{MEDICAL_KB_WORK_DIRS['conflict']}/README.md",
            title="同步冲突",
            kb_level="L3",
            body="## 用途\n\n- 保存 Vault/飞书 双边同步与提升过程中的冲突记录，避免静默覆盖。\n",
        ),
    ]


def path_sort_key(relative_path: str) -> tuple[str, str]:
    path = Path(relative_path)
    return path.parent.as_posix(), path.name
