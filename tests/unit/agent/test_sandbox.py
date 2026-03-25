"""Sandbox Manager 单元测试"""

import asyncio
import pytest

from nexus.agent.sandbox import (
    DockerBackend,
    ExecutionResult,
    HostBackend,
    SandboxConfig,
    SandboxManager,
    SandboxType,
    WorkspaceAccess,
    _shell_quote,
)


# ---------------------------------------------------------------------------
# HostBackend 测试
# ---------------------------------------------------------------------------

class TestHostBackend:
    """宿主机执行后端"""

    @pytest.mark.asyncio
    async def test_simple_echo(self, tmp_path):
        config = SandboxConfig(workspace=tmp_path)
        backend = HostBackend(config)
        result = await backend.execute("echo hello")
        assert result.exit_code == 0
        assert "hello" in result.stdout

    @pytest.mark.asyncio
    async def test_nonzero_exit(self, tmp_path):
        config = SandboxConfig(workspace=tmp_path)
        backend = HostBackend(config)
        result = await backend.execute("exit 42")
        assert result.exit_code == 42

    @pytest.mark.asyncio
    async def test_timeout(self, tmp_path):
        config = SandboxConfig(workspace=tmp_path)
        backend = HostBackend(config)
        result = await backend.execute("sleep 10", timeout=1)
        assert result.timed_out

    @pytest.mark.asyncio
    async def test_workdir(self, tmp_path):
        config = SandboxConfig(workspace=tmp_path)
        backend = HostBackend(config)
        result = await backend.execute("pwd", workdir=str(tmp_path))
        assert str(tmp_path) in result.stdout

    @pytest.mark.asyncio
    async def test_env_injection(self, tmp_path):
        config = SandboxConfig(workspace=tmp_path, env={"MY_VAR": "hello123"})
        backend = HostBackend(config)
        result = await backend.execute("echo $MY_VAR")
        assert "hello123" in result.stdout

    @pytest.mark.asyncio
    async def test_sensitive_env_removed(self, tmp_path):
        """敏感环境变量应被移除"""
        import os
        os.environ["TEST_SECRET_KEY"] = "should_be_removed"
        try:
            config = SandboxConfig(workspace=tmp_path)
            backend = HostBackend(config)
            result = await backend.execute("echo $TEST_SECRET_KEY")
            # 变量被移除后 echo 输出空行
            assert "should_be_removed" not in result.stdout
        finally:
            del os.environ["TEST_SECRET_KEY"]

    def test_is_available(self, tmp_path):
        config = SandboxConfig(workspace=tmp_path)
        backend = HostBackend(config)
        assert backend.is_available()

    @pytest.mark.asyncio
    async def test_cleanup_is_noop(self, tmp_path):
        config = SandboxConfig(workspace=tmp_path)
        backend = HostBackend(config)
        await backend.cleanup()  # 不应抛出异常


# ---------------------------------------------------------------------------
# DockerBackend 测试 (单元测试 — 不依赖真实 Docker)
# ---------------------------------------------------------------------------

class TestDockerBackend:
    """Docker 后端单元测试"""

    def test_is_available_checks_docker_binary(self):
        config = SandboxConfig(sandbox_type=SandboxType.DOCKER)
        backend = DockerBackend(config)
        # 测试 is_available 不抛异常
        result = backend.is_available()
        assert isinstance(result, bool)

    def test_build_docker_run_cmd(self):
        from pathlib import Path
        config = SandboxConfig(
            sandbox_type=SandboxType.DOCKER,
            docker_image="python:3.11-slim",
            workspace=Path("/tmp/test"),
            workspace_access=WorkspaceAccess.RO,
            memory_limit="256m",
            cpu_limit=0.5,
            network_enabled=False,
        )
        backend = DockerBackend(config)
        cmd = backend._build_docker_run_cmd("echo hello")
        assert "python:3.11-slim" in cmd
        assert "--memory" in cmd
        assert "256m" in cmd
        assert "--network" in cmd
        assert "none" in cmd
        assert "/tmp/test" in cmd
        assert ":ro" in cmd

    def test_build_docker_run_cmd_rw_access(self):
        from pathlib import Path
        config = SandboxConfig(
            sandbox_type=SandboxType.DOCKER,
            workspace=Path("/tmp/test"),
            workspace_access=WorkspaceAccess.RW,
        )
        backend = DockerBackend(config)
        cmd = backend._build_docker_run_cmd("ls")
        assert ":rw" in cmd

    def test_build_docker_run_cmd_no_workspace(self):
        config = SandboxConfig(
            sandbox_type=SandboxType.DOCKER,
            workspace=None,
        )
        backend = DockerBackend(config)
        cmd = backend._build_docker_run_cmd("ls")
        # 不应有 -v 挂载
        assert "-v" not in cmd.split("--name")[0]  # 只检查 name 之前的部分


# ---------------------------------------------------------------------------
# SandboxManager 测试
# ---------------------------------------------------------------------------

class TestSandboxManager:
    """统一管理入口测试"""

    def test_create_host(self):
        mgr = SandboxManager.create("host")
        assert mgr.sandbox_type == SandboxType.HOST
        assert mgr.backend.is_available()

    def test_create_docker_fallback(self):
        """Docker 不可用时回退到 Host"""
        import shutil
        if shutil.which("docker"):
            pytest.skip("Docker 可用，无法测试回退")
        mgr = SandboxManager.create("docker")
        # 如果 Docker 不可用，应回退到 HostBackend
        assert isinstance(mgr.backend, HostBackend)

    @pytest.mark.asyncio
    async def test_execute_via_manager(self, tmp_path):
        mgr = SandboxManager.create("host", workspace=tmp_path)
        result = await mgr.execute("echo managed")
        assert result.exit_code == 0
        assert "managed" in result.stdout

    @pytest.mark.asyncio
    async def test_cleanup(self, tmp_path):
        mgr = SandboxManager.create("host", workspace=tmp_path)
        _ = mgr.backend  # 触发创建
        await mgr.cleanup()
        assert mgr._backend is None


# ---------------------------------------------------------------------------
# 辅助函数测试
# ---------------------------------------------------------------------------

class TestShellQuote:
    def test_simple_string(self):
        assert _shell_quote("hello") == "hello"

    def test_empty_string(self):
        assert _shell_quote("") == "''"

    def test_string_with_spaces(self):
        result = _shell_quote("hello world")
        assert "hello world" in result

    def test_string_with_quotes(self):
        result = _shell_quote("it's here")
        # 应该能正确处理单引号
        assert "it" in result


# ---------------------------------------------------------------------------
# SandboxConfig 测试
# ---------------------------------------------------------------------------

class TestSandboxConfig:
    def test_default_config(self):
        config = SandboxConfig()
        assert config.sandbox_type == SandboxType.HOST
        assert config.memory_limit == "512m"
        assert config.network_enabled is True
        assert config.reuse_container is True

    def test_custom_config(self):
        config = SandboxConfig(
            sandbox_type=SandboxType.DOCKER,
            docker_image="node:18",
            memory_limit="1g",
            network_enabled=False,
        )
        assert config.docker_image == "node:18"
        assert config.memory_limit == "1g"
        assert config.network_enabled is False
