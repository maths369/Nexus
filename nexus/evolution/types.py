"""Evolution Module 数据类型定义"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


class EvolutionAction(str, enum.Enum):
    """进化操作类型"""
    SKILL_INSTALL = "skill_install"
    SKILL_UNINSTALL = "skill_uninstall"
    CONFIG_CHANGE = "config_change"
    CONFIG_ROLLBACK = "config_rollback"


class VerifyStatus(str, enum.Enum):
    PASSED = "passed"
    FAILED = "failed"


@dataclass
class CheckResult:
    """单项检查结果"""
    name: str
    passed: bool
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class VerifyResult:
    """验证结果（包含多项检查）"""
    passed: bool
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def summary(self) -> str:
        failed = [c for c in self.checks if not c.passed]
        if not failed:
            return "All checks passed"
        return "; ".join(f"{c.name}: {c.message}" for c in failed)


@dataclass
class ChangeResult:
    """变更结果"""
    success: bool
    reason: str = ""
    backup_id: str | None = None


@dataclass
class SkillSpec:
    """Skill 规范"""
    skill_id: str
    name: str
    description: str
    version: str = "0.1.0"
    entry_point: str = "main.py"  # 入口文件
    # Skill 声明的工具列表
    tools: list[dict[str, Any]] = field(default_factory=list)
    # 依赖
    dependencies: list[str] = field(default_factory=list)
    # 测试用例路径
    test_dir: str | None = None
    # 元数据
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AuditEntry:
    """审计记录"""
    entry_id: str
    action: str
    timestamp: datetime = field(default_factory=datetime.now)
    actor: str = "system"       # "system" | "user" | "agent"
    target: str = ""            # 操作目标（skill_id / config_key）
    details: dict[str, Any] = field(default_factory=dict)
    success: bool = True
    error: str | None = None
