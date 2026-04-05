"""
Tool Profile Manager — 工具子集管理

对标 OpenClaw 的 tool profile 概念:
- minimal: 最小工具集（读取 + 搜索）
- coding: 编码工具集（读写 + 执行 + 搜索）
- messaging: 消息工具集（Channel 操作 + 文档）
- full: 全量工具集
- custom: 用户自定义（白名单 + 黑名单）

用法:
    profile = ToolProfile.coding()
    filtered = profile.filter(all_tools)
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass, field
from typing import Any

from .types import ToolDefinition, ToolRiskLevel

logger = logging.getLogger(__name__)


class ProfileName(str, enum.Enum):
    """内置 Profile 名称"""
    MINIMAL = "minimal"
    CODING = "coding"
    MESSAGING = "messaging"
    FULL = "full"
    CUSTOM = "custom"


# ---------------------------------------------------------------------------
# 内置 Profile 工具名称清单
# ---------------------------------------------------------------------------

# 最小集: 只读 + 搜索 + 记忆 + compact
_MINIMAL_TOOLS: frozenset[str] = frozenset({
    # 读取
    "list_local_files",
    "read_local_file",
    "code_read_file",
    "list_vault_pages",
    "find_vault_pages",
    # 搜索
    "search_web",
    "search_web_structured",
    "browser_navigate",
    "browser_extract_text",
    # 上下文管理
    "compact",
    # 记忆
    "memory_read_identity",
    "memory_read_journal",
    "memory_list_journals",
    # Skill (只读)
    "load_skill",
    "skill_list_installed",
    "skill_list_installable",
    # 任务 (只读)
    "task_list",
    "task_get",
    "todo_write",
})

# 编码集: minimal + 文件读写 + 代码执行 + 进化
_CODING_TOOLS: frozenset[str] = _MINIMAL_TOOLS | frozenset({
    # 文件写入
    "write_local_file",
    "file_write",
    "file_edit",
    "file_search",
    # 代码执行
    "system_run",
    "background_run",
    "check_background",
    # 进化
    "skill_create",
    "skill_update",
    "skill_install",
    "skill_search_remote",
    "skill_search_clawhub",
    "skill_import_local",
    "skill_import_remote",
    "skill_import_clawhub",
    "evolution_audit",
    # Capability
    "capability_list_available",
    "capability_status",
    "capability_enable",
    "capability_create",
    "capability_register",
    "capability_stage",
    "capability_verify",
    "capability_promote",
    "capability_rollback",
    # 任务管理
    "task_create",
    "task_update",
    # 委派
    "dispatch_subagent",
    # 浏览器 (完整)
    "browser_screenshot",
    "browser_fill_form",
    # Excel
    "excel_list_sheets",
    "excel_to_csv",
})

# 消息集: minimal + 文档操作 + 子代理
_MESSAGING_TOOLS: frozenset[str] = _MINIMAL_TOOLS | frozenset({
    # 文档操作
    "document_append_block",
    "document_replace_section",
    "document_insert_checklist",
    "document_insert_table",
    "document_insert_page_link",
    "document_create_database",
    "delete_page",
    # 子代理
    "dispatch_subagent",
    # 任务
    "task_create",
    "task_update",
    # 记忆 (写入)
    "memory_update_user",
    "memory_update_soul",
    "memory_daily_log",
    "memory_reindex",
    # 音频
    "audio_transcribe_path",
    "audio_materialize_transcript",
    # 声纹
    "voiceprint_register",
    "voiceprint_list",
    "voiceprint_delete",
})

# full: 不做过滤，返回全量
_FULL_TOOLS: frozenset[str] | None = None  # None 表示不过滤


# ---------------------------------------------------------------------------
# ToolProfile
# ---------------------------------------------------------------------------

@dataclass
class ToolProfile:
    """
    工具 Profile — 控制一次 Run 中可用的工具子集。

    可以通过内置 Profile 名称快速创建，也可以用 include/exclude 自定义。
    """
    name: str
    description: str = ""
    # 允许的工具名称 (None = 全部允许)
    include: frozenset[str] | None = None
    # 排除的工具名称 (优先于 include)
    exclude: frozenset[str] = field(default_factory=frozenset)
    # 补偿允许的工具名称（即使不在 include 中也可保留）
    also_allow: frozenset[str] = field(default_factory=frozenset)
    # 允许的最高风险等级 (None = 不限制)
    max_risk_level: ToolRiskLevel | None = None

    def filter(self, tools: list[ToolDefinition]) -> list[ToolDefinition]:
        """根据 Profile 过滤工具列表"""
        result = []
        for tool in tools:
            # 排除列表优先
            if tool.name in self.exclude:
                continue
            # also_allow 可补偿 include 收窄，但不绕过 exclude
            if tool.name in self.also_allow:
                result.append(tool)
                continue
            # 包含列表检查
            if self.include is not None and tool.name not in self.include:
                continue
            # 风险等级检查
            if self.max_risk_level is not None:
                if _risk_order(tool.risk_level) > _risk_order(self.max_risk_level):
                    continue
            result.append(tool)
        return result

    def merge(self, other: ToolProfile) -> ToolProfile:
        """合并两个 Profile (取交集语义: 同时在两个 Profile 中允许的工具)"""
        if self.include is None and other.include is None:
            merged_include = None
        elif self.include is None:
            merged_include = other.include
        elif other.include is None:
            merged_include = self.include
        else:
            merged_include = self.include & other.include

        return ToolProfile(
            name=f"{self.name}+{other.name}",
            description=f"Merged: {self.description} + {other.description}",
            include=merged_include,
            exclude=self.exclude | other.exclude,
            also_allow=self.also_allow | other.also_allow,
            max_risk_level=_min_risk(self.max_risk_level, other.max_risk_level),
        )

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典"""
        return {
            "name": self.name,
            "description": self.description,
            "include": sorted(self.include) if self.include else None,
            "exclude": sorted(self.exclude) if self.exclude else [],
            "also_allow": sorted(self.also_allow) if self.also_allow else [],
            "max_risk_level": self.max_risk_level.value if self.max_risk_level else None,
        }

    # ------------------------------------------------------------------
    # 工厂方法
    # ------------------------------------------------------------------

    @classmethod
    def minimal(cls) -> ToolProfile:
        """最小工具集 — 只读 + 搜索"""
        return cls(
            name=ProfileName.MINIMAL.value,
            description="只读工具集: 文件读取、搜索、记忆查询",
            include=_MINIMAL_TOOLS,
            max_risk_level=ToolRiskLevel.LOW,
        )

    @classmethod
    def coding(cls) -> ToolProfile:
        """编码工具集 — 读写 + 执行 + 进化"""
        return cls(
            name=ProfileName.CODING.value,
            description="编码工具集: 文件读写、代码执行、技能进化",
            include=_CODING_TOOLS,
        )

    @classmethod
    def messaging(cls) -> ToolProfile:
        """消息工具集 — 文档操作 + 子代理"""
        return cls(
            name=ProfileName.MESSAGING.value,
            description="消息工具集: 文档操作、记忆写入、子代理",
            include=_MESSAGING_TOOLS,
            max_risk_level=ToolRiskLevel.MEDIUM,
        )

    @classmethod
    def full(cls) -> ToolProfile:
        """全量工具集 — 不做过滤"""
        return cls(
            name=ProfileName.FULL.value,
            description="全量工具集: 所有可用工具",
            include=None,
        )

    @classmethod
    def custom(
        cls,
        *,
        include: set[str] | None = None,
        exclude: set[str] | None = None,
        also_allow: set[str] | None = None,
        max_risk_level: ToolRiskLevel | None = None,
    ) -> ToolProfile:
        """自定义工具集"""
        return cls(
            name=ProfileName.CUSTOM.value,
            description="自定义工具集",
            include=frozenset(include) if include else None,
            exclude=frozenset(exclude) if exclude else frozenset(),
            also_allow=frozenset(also_allow) if also_allow else frozenset(),
            max_risk_level=max_risk_level,
        )

    @classmethod
    def from_name(cls, name: str) -> ToolProfile:
        """根据名称创建 Profile"""
        factories = {
            ProfileName.MINIMAL.value: cls.minimal,
            ProfileName.CODING.value: cls.coding,
            ProfileName.MESSAGING.value: cls.messaging,
            ProfileName.FULL.value: cls.full,
        }
        factory = factories.get(name)
        if factory is None:
            raise ValueError(
                f"未知的 Profile 名称: '{name}'。"
                f"可选: {', '.join(factories.keys())}"
            )
        return factory()


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

_RISK_ORDER = {
    ToolRiskLevel.LOW: 0,
    ToolRiskLevel.MEDIUM: 1,
    ToolRiskLevel.HIGH: 2,
    ToolRiskLevel.CRITICAL: 3,
}


def _risk_order(level: ToolRiskLevel) -> int:
    return _RISK_ORDER.get(level, 99)


def _min_risk(
    a: ToolRiskLevel | None,
    b: ToolRiskLevel | None,
) -> ToolRiskLevel | None:
    """取更严格的风险等级上限"""
    if a is None:
        return b
    if b is None:
        return a
    return a if _risk_order(a) <= _risk_order(b) else b
