"""
Sandbox Manager — 沙箱化代码执行

对标 OpenClaw 的 Sandbox 后端:
- host: 直接在宿主机执行（当前默认，保持兼容）
- docker: 在 Docker 容器中执行（隔离文件系统 + 网络）

沙箱对 SystemRunner 和 BackgroundTaskManager 透明:
通过注入 SandboxBackend 实例，替换底层的 subprocess 调用。

用法:
    backend = SandboxManager.create("docker", workspace=Path("/project"))
    result = await backend.execute("pip install pandas", timeout=60)
"""

from __future__ import annotations

import abc
import asyncio
import logging
import os
import shutil
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class SandboxType(str, Enum):
    """沙箱类型"""
    HOST = "host"        # 宿主机直接执行
    DOCKER = "docker"    # Docker 容器隔离


class WorkspaceAccess(str, Enum):
    """工作区访问模式 (对标 OpenClaw workspace-access)"""
    NONE = "none"   # 无访问
    RO = "ro"       # 只读
    RW = "rw"       # 读写


@dataclass
class ExecutionResult:
    """命令执行结果"""
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


@dataclass
class SandboxConfig:
    """沙箱配置"""
    sandbox_type: SandboxType = SandboxType.HOST
    # Docker 镜像
    docker_image: str = "python:3.11-slim"
    # 工作区挂载
    workspace: Path | None = None
    workspace_access: WorkspaceAccess = WorkspaceAccess.RW
    # 资源限制
    memory_limit: str = "512m"
    cpu_limit: float = 1.0
    # 网络
    network_enabled: bool = True
    # 额外挂载 (host_path -> container_path)
    extra_mounts: dict[str, str] = field(default_factory=dict)
    # 环境变量
    env: dict[str, str] = field(default_factory=dict)
    # 容器保活（复用容器而非每次创建）
    reuse_container: bool = True


# ---------------------------------------------------------------------------
# 沙箱后端抽象
# ---------------------------------------------------------------------------

class SandboxBackend(abc.ABC):
    """沙箱执行后端"""

    @abc.abstractmethod
    async def execute(
        self,
        command: str,
        *,
        workdir: str | None = None,
        timeout: int = 600,
        env: dict[str, str] | None = None,
    ) -> ExecutionResult:
        """在沙箱中执行命令"""

    @abc.abstractmethod
    async def cleanup(self) -> None:
        """清理沙箱资源"""

    @abc.abstractmethod
    def is_available(self) -> bool:
        """检查后端是否可用"""


# ---------------------------------------------------------------------------
# Host 后端 — 直接在宿主机执行
# ---------------------------------------------------------------------------

class HostBackend(SandboxBackend):
    """宿主机直接执行 — 保持与现有 SystemRunner 兼容"""

    def __init__(self, config: SandboxConfig) -> None:
        self._config = config

    async def execute(
        self,
        command: str,
        *,
        workdir: str | None = None,
        timeout: int = 600,
        env: dict[str, str] | None = None,
    ) -> ExecutionResult:
        exec_env = self._build_env(env)
        cwd = workdir or (str(self._config.workspace) if self._config.workspace else None)

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=cwd,
                env=exec_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=timeout if timeout > 0 else None,
                )
                return ExecutionResult(
                    exit_code=proc.returncode or 0,
                    stdout=stdout_bytes.decode("utf-8", errors="replace"),
                    stderr=stderr_bytes.decode("utf-8", errors="replace"),
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return ExecutionResult(
                    exit_code=-1,
                    stdout="",
                    stderr=f"命令超时 ({timeout}s)",
                    timed_out=True,
                )
        except Exception as e:
            return ExecutionResult(
                exit_code=-1,
                stdout="",
                stderr=f"执行失败: {e}",
            )

    async def cleanup(self) -> None:
        pass  # Host 后端无需清理

    def is_available(self) -> bool:
        return True

    def _build_env(self, extra: dict[str, str] | None) -> dict[str, str]:
        env = dict(os.environ)
        # 移除敏感变量
        for key in list(env.keys()):
            upper = key.upper()
            if any(s in upper for s in [
                "SECRET", "TOKEN", "PASSWORD", "CREDENTIAL",
                "API_KEY", "ACCESS_KEY", "PRIVATE_KEY",
            ]):
                del env[key]
        # 合并配置环境变量
        env.update(self._config.env)
        if extra:
            env.update(extra)
        return env


# ---------------------------------------------------------------------------
# Docker 后端 — 容器隔离执行
# ---------------------------------------------------------------------------

class DockerBackend(SandboxBackend):
    """Docker 容器隔离执行"""

    def __init__(self, config: SandboxConfig) -> None:
        self._config = config
        self._container_id: str | None = None
        self._session_id = uuid.uuid4().hex[:8]

    async def execute(
        self,
        command: str,
        *,
        workdir: str | None = None,
        timeout: int = 600,
        env: dict[str, str] | None = None,
    ) -> ExecutionResult:
        if self._config.reuse_container and self._container_id:
            return await self._exec_in_container(command, workdir=workdir, timeout=timeout, env=env)
        return await self._run_new_container(command, workdir=workdir, timeout=timeout, env=env)

    async def _run_new_container(
        self,
        command: str,
        *,
        workdir: str | None = None,
        timeout: int = 600,
        env: dict[str, str] | None = None,
    ) -> ExecutionResult:
        """启动新容器执行命令"""
        docker_cmd = self._build_docker_run_cmd(command, workdir=workdir, env=env)
        try:
            proc = await asyncio.create_subprocess_shell(
                docker_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=timeout + 10,  # Docker 启动开销
                )
                return ExecutionResult(
                    exit_code=proc.returncode or 0,
                    stdout=stdout_bytes.decode("utf-8", errors="replace"),
                    stderr=stderr_bytes.decode("utf-8", errors="replace"),
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return ExecutionResult(
                    exit_code=-1,
                    stdout="",
                    stderr=f"Docker 容器执行超时 ({timeout}s)",
                    timed_out=True,
                )
        except Exception as e:
            return ExecutionResult(
                exit_code=-1,
                stdout="",
                stderr=f"Docker 执行失败: {e}",
            )

    async def _exec_in_container(
        self,
        command: str,
        *,
        workdir: str | None = None,
        timeout: int = 600,
        env: dict[str, str] | None = None,
    ) -> ExecutionResult:
        """在已有容器中执行命令"""
        assert self._container_id is not None

        env_flags = ""
        all_env = {**self._config.env, **(env or {})}
        for k, v in all_env.items():
            env_flags += f" -e {_shell_quote(k)}={_shell_quote(v)}"

        workdir_flag = f" -w {_shell_quote(workdir)}" if workdir else ""

        exec_cmd = (
            f"docker exec{env_flags}{workdir_flag} "
            f"{self._container_id} /bin/sh -c {_shell_quote(command)}"
        )

        try:
            proc = await asyncio.create_subprocess_shell(
                exec_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=timeout if timeout > 0 else None,
                )
                return ExecutionResult(
                    exit_code=proc.returncode or 0,
                    stdout=stdout_bytes.decode("utf-8", errors="replace"),
                    stderr=stderr_bytes.decode("utf-8", errors="replace"),
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return ExecutionResult(
                    exit_code=-1,
                    stdout="",
                    stderr=f"Docker exec 超时 ({timeout}s)",
                    timed_out=True,
                )
        except Exception as e:
            return ExecutionResult(
                exit_code=-1,
                stdout="",
                stderr=f"Docker exec 失败: {e}",
            )

    async def start_container(self) -> str:
        """启动长运行容器供复用"""
        docker_cmd = self._build_docker_run_cmd(
            "sleep infinity",
            detach=True,
        )
        proc = await asyncio.create_subprocess_shell(
            docker_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"启动 Docker 容器失败: {stderr.decode()}")

        self._container_id = stdout.decode().strip()[:12]
        logger.info("Docker 容器已启动: %s", self._container_id)
        return self._container_id

    async def cleanup(self) -> None:
        """停止并删除容器"""
        if not self._container_id:
            return
        try:
            proc = await asyncio.create_subprocess_shell(
                f"docker rm -f {self._container_id}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            logger.info("Docker 容器已清理: %s", self._container_id)
        except Exception as e:
            logger.warning("清理 Docker 容器失败: %s", e)
        finally:
            self._container_id = None

    def is_available(self) -> bool:
        return shutil.which("docker") is not None

    def _build_docker_run_cmd(
        self,
        command: str,
        *,
        workdir: str | None = None,
        env: dict[str, str] | None = None,
        detach: bool = False,
    ) -> str:
        """构建 docker run 命令"""
        parts = ["docker", "run", "--rm"]

        if detach:
            parts.append("-d")

        # 容器名
        parts.extend(["--name", f"nexus-sandbox-{self._session_id}"])

        # 资源限制
        parts.extend(["--memory", self._config.memory_limit])
        parts.extend(["--cpus", str(self._config.cpu_limit)])

        # 网络
        if not self._config.network_enabled:
            parts.extend(["--network", "none"])

        # 工作区挂载
        ws = workdir or (str(self._config.workspace) if self._config.workspace else None)
        if ws:
            access = self._config.workspace_access
            mount_opt = "ro" if access == WorkspaceAccess.RO else "rw"
            parts.extend(["-v", f"{ws}:/workspace:{mount_opt}"])
            parts.extend(["-w", "/workspace"])

        # 额外挂载
        for host_path, container_path in self._config.extra_mounts.items():
            parts.extend(["-v", f"{host_path}:{container_path}"])

        # 环境变量
        all_env = {**self._config.env, **(env or {})}
        for k, v in all_env.items():
            parts.extend(["-e", f"{k}={v}"])

        # 镜像
        parts.append(self._config.docker_image)

        # 命令
        parts.extend(["/bin/sh", "-c", command])

        return " ".join(_shell_quote(p) for p in parts)


# ---------------------------------------------------------------------------
# SandboxManager — 统一管理入口
# ---------------------------------------------------------------------------

class SandboxManager:
    """
    沙箱管理器 — 创建和管理沙箱后端实例。

    职责:
    1. 根据配置创建合适的后端
    2. 管理后端生命周期
    3. 提供统一的执行接口
    """

    def __init__(self, config: SandboxConfig | None = None) -> None:
        self._config = config or SandboxConfig()
        self._backend: SandboxBackend | None = None

    @property
    def backend(self) -> SandboxBackend:
        """获取或创建后端"""
        if self._backend is None:
            self._backend = self._create_backend()
        return self._backend

    @property
    def sandbox_type(self) -> SandboxType:
        return self._config.sandbox_type

    def _create_backend(self) -> SandboxBackend:
        """根据配置创建后端"""
        if self._config.sandbox_type == SandboxType.DOCKER:
            backend = DockerBackend(self._config)
            if not backend.is_available():
                logger.warning("Docker 不可用，回退到 Host 后端")
                return HostBackend(self._config)
            return backend
        return HostBackend(self._config)

    async def execute(
        self,
        command: str,
        *,
        workdir: str | None = None,
        timeout: int = 600,
        env: dict[str, str] | None = None,
    ) -> ExecutionResult:
        """在沙箱中执行命令"""
        return await self.backend.execute(
            command,
            workdir=workdir,
            timeout=timeout,
            env=env,
        )

    async def cleanup(self) -> None:
        """清理所有沙箱资源"""
        if self._backend:
            await self._backend.cleanup()
            self._backend = None

    @classmethod
    def create(
        cls,
        sandbox_type: str = "host",
        *,
        workspace: Path | None = None,
        workspace_access: str = "rw",
        docker_image: str = "python:3.11-slim",
        memory_limit: str = "512m",
        cpu_limit: float = 1.0,
        network_enabled: bool = True,
    ) -> SandboxManager:
        """便捷工厂方法"""
        config = SandboxConfig(
            sandbox_type=SandboxType(sandbox_type),
            docker_image=docker_image,
            workspace=workspace,
            workspace_access=WorkspaceAccess(workspace_access),
            memory_limit=memory_limit,
            cpu_limit=cpu_limit,
            network_enabled=network_enabled,
        )
        return cls(config)


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _shell_quote(s: str) -> str:
    """安全的 shell 引用"""
    if not s:
        return "''"
    # 如果字符串只包含安全字符，不需要引用
    import re
    if re.match(r"^[a-zA-Z0-9._/:-]+$", s):
        return s
    # 用单引号包裹，转义内部的单引号
    return "'" + s.replace("'", "'\"'\"'") + "'"
