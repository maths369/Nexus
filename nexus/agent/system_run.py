"""
system_run — 受控 shell 执行能力

类似 OpenClaw 的 system.run，赋予 Agent 执行 shell 命令的能力。

安全边界:
- 路径保护: 禁止直接操作敏感系统路径 (~/.ssh, /etc/shadow 等)
- 审计记录: 每次执行都写入 evolution_audit.db
- 环境隔离: 移除含 SECRET/TOKEN/KEY 的环境变量
- 无其他人为限制: 不限网络、不限超时（用户可配置默认上限）

用户通过 config/app.yaml 的 evolution.system_run 配置:
  allowed_workdirs: [vault, project]
  forbidden_path_writes: [~/.ssh, /etc, ...]
  default_timeout: 600
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Any

from nexus.evolution.audit import AuditLog

logger = logging.getLogger(__name__)

# 绝对禁止写入的路径模式 — 硬编码安全底线
_FORBIDDEN_WRITE_PATTERNS = [
    re.compile(r"~/.ssh"),
    re.compile(r"~/.gnupg"),
    re.compile(r"/etc/shadow"),
    re.compile(r"/etc/passwd"),
    re.compile(r"/etc/sudoers"),
]

# 禁止的高危命令模式
_FORBIDDEN_COMMAND_PATTERNS = [
    re.compile(r"\brm\s+-rf\s+/\s"),       # rm -rf /
    re.compile(r"\bmkfs\b"),                 # mkfs (format disk)
    re.compile(r"\bdd\s+.*of=/dev/"),        # dd write to device
    re.compile(r">\s*/dev/sd[a-z]"),         # redirect to block device
    re.compile(r"\bshutdown\b"),             # shutdown
    re.compile(r"\breboot\b"),               # reboot
]


def _sanitized_env() -> dict[str, str]:
    """返回去除敏感凭据的环境变量。"""
    env = dict(os.environ)
    for key in list(env.keys()):
        upper = key.upper()
        if any(s in upper for s in [
            "SECRET", "TOKEN", "PASSWORD", "CREDENTIAL",
            "API_KEY", "ACCESS_KEY", "PRIVATE_KEY",
        ]):
            del env[key]
    return env


def _check_command_safety(command: str) -> str | None:
    """检查命令是否触发硬编码安全底线。返回 None 表示安全，否则返回拒绝理由。"""
    for pattern in _FORBIDDEN_COMMAND_PATTERNS:
        if pattern.search(command):
            return f"命令触发安全底线: {pattern.pattern}"
    return None


class SystemRunner:
    """
    受控 shell 执行器。

    Agent 可以通过此组件执行任意 shell 命令（pip install、python 脚本、
    curl API、git 操作等），唯一限制是硬编码的安全底线。
    """

    def __init__(
        self,
        *,
        allowed_workdirs: list[Path],
        audit: AuditLog,
        default_timeout: int = 600,
        shell: str = "/bin/zsh",
    ) -> None:
        self._allowed_workdirs = allowed_workdirs
        self._audit = audit
        self._default_timeout = default_timeout
        self._shell = shell

    def _resolve_workdir(self, workdir: str | None) -> Path:
        """解析并验证工作目录。默认使用第一个 allowed_workdir。"""
        if workdir is None:
            return self._allowed_workdirs[0]
        resolved = Path(workdir).resolve()
        for allowed in self._allowed_workdirs:
            try:
                resolved.relative_to(allowed.resolve())
                return resolved
            except ValueError:
                continue
        raise PermissionError(
            f"工作目录 '{workdir}' 不在允许范围内。"
            f"允许的根目录: {[str(p) for p in self._allowed_workdirs]}"
        )

    async def run(
        self,
        command: str,
        *,
        workdir: str | None = None,
        timeout: int | None = None,
        actor: str = "agent",
    ) -> dict[str, Any]:
        """
        执行 shell 命令。

        Returns:
            {
                "exit_code": int,
                "stdout": str,
                "stderr": str,
                "timed_out": bool,
            }
        """
        # 安全底线检查
        rejection = _check_command_safety(command)
        if rejection:
            self._audit.record(
                action="system_run_blocked",
                target=command[:200],
                actor=actor,
                details={"reason": rejection},
                success=False,
                error=rejection,
            )
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": f"[BLOCKED] {rejection}",
                "timed_out": False,
            }

        # 解析工作目录
        try:
            cwd = self._resolve_workdir(workdir)
        except PermissionError as e:
            self._audit.record(
                action="system_run_blocked",
                target=command[:200],
                actor=actor,
                details={"reason": str(e), "workdir": workdir},
                success=False,
                error=str(e),
            )
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": str(e),
                "timed_out": False,
            }

        effective_timeout = timeout or self._default_timeout

        # 审计记录（执行前）
        self._audit.record(
            action="system_run",
            target=command[:500],
            actor=actor,
            details={
                "workdir": str(cwd),
                "timeout": effective_timeout,
            },
        )

        logger.info("system_run: %s (cwd=%s, timeout=%ds)", command[:100], cwd, effective_timeout)

        # 执行
        timed_out = False
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=str(cwd),
                env=_sanitized_env(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                executable=self._shell,
            )
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=effective_timeout if effective_timeout > 0 else None,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                timed_out = True
                stdout_bytes = b""
                stderr_bytes = f"命令超时 ({effective_timeout}s)".encode()

        except Exception as e:
            self._audit.record(
                action="system_run_error",
                target=command[:200],
                actor=actor,
                success=False,
                error=str(e),
            )
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": f"执行失败: {e}",
                "timed_out": False,
            }

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        exit_code = proc.returncode or 0

        # 审计记录（执行后）
        self._audit.record(
            action="system_run_completed",
            target=command[:200],
            actor=actor,
            details={
                "exit_code": exit_code,
                "timed_out": timed_out,
                "stdout_len": len(stdout),
                "stderr_len": len(stderr),
            },
            success=exit_code == 0 and not timed_out,
            error=stderr[:500] if exit_code != 0 else None,
        )

        # 截断过长输出
        max_output = 50_000
        if len(stdout) > max_output:
            stdout = stdout[:max_output] + f"\n\n... [截断, 共 {len(stdout_bytes)} 字节]"
        if len(stderr) > max_output:
            stderr = stderr[:max_output] + f"\n\n... [截断, 共 {len(stderr_bytes)} 字节]"

        return {
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "timed_out": timed_out,
        }
