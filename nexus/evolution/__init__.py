"""
Evolution Module — 受控自我进化

紧凑模块（~700 行，5 个文件），不是独立 Runtime。
安全从第一天就有：任何 skill 安装和 config 变更都经过沙箱验证。

模块结构:
  - skill_manager.py   — 安装/卸载/列表
  - config_manager.py  — 配置变更/回滚
  - sandbox.py         — 沙箱执行与验证
  - audit.py           — 变更审计日志（SQLite）
  - types.py           — 数据结构
"""

from .audit import AuditLog
from .capability_manager import CapabilityManager
from .capability_promotion import CapabilityPromotionAdvisor, CapabilityPromotionSuggestion
from .config_manager import ConfigManager
from .sandbox import Sandbox
from .skill_manager import SkillManager
from .types import AuditEntry, ChangeResult, CheckResult, EvolutionAction, SkillSpec, VerifyResult

__all__ = [
    "AuditEntry",
    "AuditLog",
    "CapabilityManager",
    "CapabilityPromotionAdvisor",
    "CapabilityPromotionSuggestion",
    "ChangeResult",
    "CheckResult",
    "ConfigManager",
    "EvolutionAction",
    "Sandbox",
    "SkillManager",
    "SkillSpec",
    "VerifyResult",
]
