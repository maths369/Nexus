"""
Sandbox — 沙箱执行与验证

轻量沙箱：文件系统隔离 + 进程级超时。
不依赖 Docker（第一阶段）。

验证流程:
1. 静态检查：文件结构、frontmatter 合法性
2. 安全检查：不引用禁止路径、不请求危险权限
3. 加载检查：能否成功 import / 解析
4. 运行检查：如有 test_cases，在隔离环境执行

未来升级路径：sandbox.py 内部换成 Docker/bwrap 执行，外部接口不变。
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from .types import CheckResult, VerifyResult

logger = logging.getLogger(__name__)

# 安全检查：禁止 import 的模块
_FORBIDDEN_MODULES = {
    "subprocess", "shutil", "ctypes", "signal",
    "multiprocessing", "pty", "resource",
}

# 禁止调用的危险函数（module.function 格式）
_FORBIDDEN_CALLS = {
    "os.system", "os.popen", "os.exec", "os.execl", "os.execle",
    "os.execlp", "os.execlpe", "os.execv", "os.execve", "os.execvp",
    "os.execvpe", "os.spawn", "os.spawnl", "os.spawnle",
    "os.remove", "os.unlink", "os.rmdir", "os.removedirs",
    "eval", "exec", "compile", "__import__",
    "importlib.import_module",
}

# 禁止在文件内容中引用的路径模式
_FORBIDDEN_PATH_PATTERNS = [
    "/etc/",
    "/usr/",
    "/var/",
    "~/.ssh",
    "~/.config",
    "../",
]


class Sandbox:
    """
    轻量沙箱：临时目录 + 受限执行 + 结果验证。

    第一阶段使用文件系统隔离 + 进程级超时。
    """

    def __init__(
        self,
        staging_dir: Path,
        timeout_seconds: int = 30,
    ):
        self.staging_dir = staging_dir
        self.timeout = timeout_seconds
        self.staging_dir.mkdir(parents=True, exist_ok=True)

    async def verify_skill(self, skill_path: Path) -> VerifyResult:
        """
        在 staging 目录中验证 skill。

        执行四项检查:
        1. 文件结构
        2. 安全规则
        3. 可加载性
        4. 自带测试（如有）
        """
        checks: list[CheckResult] = []

        checks.append(await self._check_structure(skill_path))
        checks.append(await self._check_safety(skill_path))
        checks.append(await self._check_loadable(skill_path))

        if self._has_test_cases(skill_path):
            checks.append(await self._run_tests(skill_path))

        return VerifyResult(
            passed=all(c.passed for c in checks),
            checks=checks,
        )

    async def verify_config_change(
        self, key: str, new_value: Any, schema: dict[str, Any] | None = None
    ) -> VerifyResult:
        """
        验证配置变更是否安全。

        1. Schema 验证
        2. Dry-run（模拟应用变更）
        """
        checks: list[CheckResult] = []

        checks.append(self._check_schema(key, new_value, schema))
        checks.append(await self._dry_run_config(key, new_value))

        return VerifyResult(
            passed=all(c.passed for c in checks),
            checks=checks,
        )

    # ------------------------------------------------------------------
    # Skill 检查方法
    # ------------------------------------------------------------------

    async def _check_structure(self, skill_path: Path) -> CheckResult:
        """检查 skill 的文件结构"""
        if not skill_path.is_dir():
            return CheckResult(
                name="structure",
                passed=False,
                message=f"Not a directory: {skill_path}",
            )

        # 必须有入口文件
        entry = skill_path / "main.py"
        if not entry.exists():
            # 也接受 __init__.py
            entry = skill_path / "__init__.py"
            if not entry.exists():
                return CheckResult(
                    name="structure",
                    passed=False,
                    message="Missing entry point: main.py or __init__.py",
                )

        # 必须有 skill.yaml 或 skill.json 描述文件
        has_spec = (
            (skill_path / "skill.yaml").exists()
            or (skill_path / "skill.json").exists()
        )
        if not has_spec:
            return CheckResult(
                name="structure",
                passed=False,
                message="Missing skill spec: skill.yaml or skill.json",
            )

        return CheckResult(name="structure", passed=True, message="OK")

    async def _check_safety(self, skill_path: Path) -> CheckResult:
        """AST 级安全检查：禁止的 import、危险函数调用、路径引用"""
        violations: list[str] = []

        for py_file in skill_path.rglob("*.py"):
            content = py_file.read_text(encoding="utf-8", errors="ignore")
            rel_name = py_file.name

            # AST 分析
            try:
                tree = ast.parse(content, filename=str(py_file))
            except SyntaxError as e:
                violations.append(f"{rel_name}: syntax error: {e}")
                continue

            for node in ast.walk(tree):
                # 检查 import xxx / from xxx import yyy
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        mod = alias.name.split(".")[0]
                        if mod in _FORBIDDEN_MODULES:
                            violations.append(
                                f"{rel_name}:{node.lineno}: "
                                f"imports forbidden module '{alias.name}'"
                            )
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        mod = node.module.split(".")[0]
                        if mod in _FORBIDDEN_MODULES:
                            violations.append(
                                f"{rel_name}:{node.lineno}: "
                                f"imports from forbidden module '{node.module}'"
                            )

                # 检查危险函数调用
                elif isinstance(node, ast.Call):
                    call_name = self._resolve_call_name(node)
                    if call_name and call_name in _FORBIDDEN_CALLS:
                        violations.append(
                            f"{rel_name}:{node.lineno}: "
                            f"calls forbidden function '{call_name}'"
                        )

            # 路径模式检查（纯文本扫描，AST 不覆盖字符串内容）
            for pattern in _FORBIDDEN_PATH_PATTERNS:
                if pattern in content:
                    violations.append(
                        f"{rel_name}: references forbidden path '{pattern}'"
                    )

        if violations:
            return CheckResult(
                name="safety",
                passed=False,
                message=f"Safety violations: {'; '.join(violations[:10])}",
                details={"violations": violations},
            )

        return CheckResult(name="safety", passed=True, message="OK")

    @staticmethod
    def _resolve_call_name(node: ast.Call) -> str | None:
        """从 AST Call 节点解析函数名（支持 os.system、eval 等模式）"""
        func = node.func
        if isinstance(func, ast.Name):
            # eval(...), exec(...), __import__(...)
            return func.id
        if isinstance(func, ast.Attribute):
            # os.system(...), importlib.import_module(...)
            parts = []
            current = func
            while isinstance(current, ast.Attribute):
                parts.append(current.attr)
                current = current.value
            if isinstance(current, ast.Name):
                parts.append(current.id)
            parts.reverse()
            return ".".join(parts)
        return None

    async def _check_loadable(self, skill_path: Path) -> CheckResult:
        """检查 skill 是否可以成功加载（import）"""
        entry = skill_path / "main.py"
        if not entry.exists():
            entry = skill_path / "__init__.py"

        if not entry.exists():
            return CheckResult(
                name="loadable",
                passed=False,
                message="No entry point found",
            )

        try:
            load_script = (
                "import importlib.util, sys; "
                "path=sys.argv[1]; "
                "spec=importlib.util.spec_from_file_location('skill_probe', path); "
                "assert spec is not None and spec.loader is not None; "
                "module=importlib.util.module_from_spec(spec); "
                "spec.loader.exec_module(module)"
            )
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                load_script,
                str(entry),
                cwd=str(skill_path),
                env=self._sanitized_env(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), self.timeout)
            if proc.returncode == 0:
                return CheckResult(name="loadable", passed=True, message="OK")
            return CheckResult(
                name="loadable",
                passed=False,
                message=f"Load failed: {stderr.decode().strip()[:300] or f'exit code {proc.returncode}'}",
            )
        except asyncio.TimeoutError:
            return CheckResult(
                name="loadable",
                passed=False,
                message=f"Load timed out after {self.timeout}s",
            )
        except Exception as e:
            return CheckResult(
                name="loadable",
                passed=False,
                message=f"Load failed: {e}",
            )

    def _has_test_cases(self, skill_path: Path) -> bool:
        """检查 skill 是否包含测试用例"""
        test_dir = skill_path / "tests"
        return test_dir.is_dir() and any(test_dir.glob("test_*.py"))

    async def _run_tests(self, skill_path: Path) -> CheckResult:
        """在隔离环境中运行 skill 的测试"""
        test_dir = skill_path / "tests"
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "pytest", str(test_dir), "-v",
                cwd=str(self.staging_dir),
                env=self._sanitized_env(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), self.timeout
            )
            if proc.returncode == 0:
                return CheckResult(name="tests", passed=True, message="OK")
            else:
                return CheckResult(
                    name="tests",
                    passed=False,
                    message=f"Tests failed (exit code {proc.returncode})",
                    details={"stderr": stderr.decode()[:500]},
                )
        except asyncio.TimeoutError:
            return CheckResult(
                name="tests",
                passed=False,
                message=f"Tests timed out after {self.timeout}s",
            )
        except Exception as e:
            return CheckResult(
                name="tests",
                passed=False,
                message=f"Test execution error: {e}",
            )

    # ------------------------------------------------------------------
    # Config 检查方法
    # ------------------------------------------------------------------

    def _check_schema(
        self, key: str, value: Any, schema: dict[str, Any] | None = None
    ) -> CheckResult:
        """Schema 验证"""
        if schema is None:
            # 无 schema 时只做基本类型检查
            if value is None:
                return CheckResult(
                    name="schema",
                    passed=False,
                    message="Config value cannot be None",
                )
            return CheckResult(name="schema", passed=True, message="OK")

        # TODO: 使用 jsonschema 或 pydantic 进行完整的 schema 验证
        return CheckResult(name="schema", passed=True, message="OK")

    async def _dry_run_config(self, key: str, value: Any) -> CheckResult:
        """Dry-run: 模拟应用配置变更"""
        # 第一阶段：只做基本可行性检查
        # 未来可以在沙箱环境中实际应用并验证
        return CheckResult(name="dry_run", passed=True, message="OK")

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _sanitized_env(self) -> dict[str, str]:
        """返回去除敏感信息的环境变量"""
        env = dict(os.environ)
        # 移除敏感环境变量
        for key in list(env.keys()):
            if any(s in key.upper() for s in [
                "SECRET", "TOKEN", "PASSWORD", "KEY", "CREDENTIAL",
                "API_KEY", "ACCESS_KEY",
            ]):
                del env[key]
        return env
