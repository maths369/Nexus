"""
Config Manager — 配置变更与回滚

每次配置变更自动经过沙箱验证，失败自动回滚。

流程:
1. 沙箱验证新值
2. 备份当前值
3. 应用变更
4. 健康检查
5. 如果健康检查失败，自动回滚
6. 审计记录
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .sandbox import Sandbox
from .audit import AuditLog
from .types import ChangeResult

logger = logging.getLogger(__name__)


class ConfigManager:
    """
    配置管理器：支持变更、备份、回滚。

    配置存储为 JSON 文件（config.json）。
    每次变更自动创建备份和审计记录。
    """

    def __init__(
        self,
        config_path: Path,
        backup_dir: Path,
        sandbox: Sandbox,
        audit: AuditLog,
        health_check_fn=None,
    ):
        self._config_path = config_path
        self._backup_dir = backup_dir
        self._sandbox = sandbox
        self._audit = audit
        self._health_check_fn = health_check_fn
        self._backup_dir.mkdir(parents=True, exist_ok=True)

        # 加载配置
        self._config: dict[str, Any] = {}
        if config_path.exists():
            self._config = json.loads(config_path.read_text(encoding="utf-8"))

    def get(self, key: str, default: Any = None) -> Any:
        """获取配置值"""
        keys = key.split(".")
        value = self._config
        for k in keys:
            if isinstance(value, dict):
                value = value.get(k)
            else:
                return default
            if value is None:
                return default
        return value

    def set(self, key: str, value: Any) -> None:
        """设置配置值（内部方法，不经过验证）"""
        keys = key.split(".")
        config = self._config
        for k in keys[:-1]:
            if k not in config or not isinstance(config[k], dict):
                config[k] = {}
            config = config[k]
        config[keys[-1]] = value
        self._save()

    async def update(self, key: str, value: Any) -> ChangeResult:
        """
        安全更新配置值。

        流程:
        1. 沙箱验证
        2. 备份当前值
        3. 应用变更
        4. 健康检查
        5. 审计记录
        """
        # Step 1: 沙箱验证
        verify = await self._sandbox.verify_config_change(key, value)
        if not verify.passed:
            self._audit.record(
                action="config_change_blocked",
                target=key,
                details={"reason": verify.summary, "new_value": str(value)},
                success=False,
            )
            return ChangeResult(success=False, reason=verify.summary)

        # Step 2: 备份当前值
        old_value = self.get(key)
        backup_id = self._save_backup(key, old_value)

        # Step 3: 应用变更
        self.set(key, value)

        # Step 4: 健康检查
        if self._health_check_fn:
            try:
                healthy = await self._health_check_fn()
            except Exception as e:
                logger.error(f"Health check error: {e}")
                healthy = False

            if not healthy:
                # 自动回滚
                self.set(key, old_value)
                self._audit.record(
                    action="config_auto_rollback",
                    target=key,
                    details={
                        "backup_id": backup_id,
                        "reason": "health check failed",
                    },
                    success=False,
                )
                return ChangeResult(
                    success=False,
                    reason="Health check failed, auto-rolled back",
                    backup_id=backup_id,
                )

        # Step 5: 审计
        self._audit.record(
            action="config_changed",
            target=key,
            details={
                "old_value": str(old_value),
                "new_value": str(value),
                "backup_id": backup_id,
            },
        )

        logger.info(f"Config updated: {key} = {value}")
        return ChangeResult(success=True, backup_id=backup_id)

    async def rollback(self, key: str) -> bool:
        """手动回滚到上一版本"""
        backup = self._get_latest_backup(key)
        if not backup:
            logger.warning(f"No backup found for config key: {key}")
            return False

        old_value = backup.get("value")
        self.set(key, old_value)

        self._audit.record(
            action="config_rollback",
            target=key,
            details={
                "restored_from": backup.get("backup_id", ""),
                "restored_value": str(old_value),
            },
        )

        logger.info(f"Config rolled back: {key}")
        return True

    # ------------------------------------------------------------------
    # 备份管理
    # ------------------------------------------------------------------

    def _save_backup(self, key: str, value: Any) -> str:
        """保存配置备份"""
        backup_id = str(uuid.uuid4())[:8]
        backup_file = self._backup_dir / f"{key.replace('.', '_')}_{backup_id}.json"
        backup_data = {
            "backup_id": backup_id,
            "key": key,
            "value": value,
            "timestamp": datetime.now().isoformat(),
        }
        backup_file.write_text(
            json.dumps(backup_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return backup_id

    def _get_latest_backup(self, key: str) -> dict[str, Any] | None:
        """获取指定 key 的最新备份"""
        prefix = key.replace(".", "_")
        backups = sorted(
            self._backup_dir.glob(f"{prefix}_*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not backups:
            return None
        return json.loads(backups[0].read_text(encoding="utf-8"))

    def _save(self) -> None:
        """将配置写入文件"""
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        self._config_path.write_text(
            json.dumps(self._config, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
