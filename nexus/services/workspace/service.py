"""
Workspace Service — 安全的文件系统访问层

职责:
1. 路径安全校验（allowlist + symlink 防逃逸）
2. 基本文件操作（read / write / list / exists / copy / move）
3. 写入安全策略（禁止覆盖敏感文件）

迁移来源: macos-ai-assistant 的 VaultService + 代码工作区安全策略
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

# 禁止写入的文件模式
_DENY_WRITE_PATTERNS = {
    ".env",
    ".env.local",
    ".env.production",
    "credentials.json",
    "secrets.yaml",
    "id_rsa",
    "id_ed25519",
    ".git/config",
}

# 禁止写入的扩展名
_DENY_WRITE_EXTENSIONS = {
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".keystore",
}


class WorkspaceService:
    """
    安全的文件系统访问层。

    只允许在 allowed_roots 白名单目录内操作文件。
    所有路径在解析后都会经过安全检查，防止路径穿越攻击。
    """

    def __init__(self, allowed_roots: list[Path]):
        self._allowed_roots = [Path(root).resolve() for root in allowed_roots]

    # ------------------------------------------------------------------
    # 路径安全
    # ------------------------------------------------------------------

    def resolve(self, path: str | Path) -> Path:
        """
        解析路径并验证安全性。

        1. 展开 ~ 和环境变量
        2. 解析 symlink 到真实路径
        3. 检查是否在 allowed_roots 内

        Raises:
            ValueError: 路径不在允许的根目录内
        """
        raw_path = Path(path).expanduser()
        if not raw_path.is_absolute():
            raw_path = self._allowed_roots[0] / raw_path
        target = raw_path.resolve()
        for root in self._allowed_roots:
            if target == root or str(target).startswith(str(root) + "/"):
                return target
        raise ValueError(f"Path not allowed: {target}")

    def _check_write_safety(self, path: Path) -> None:
        """
        写入安全检查：拒绝覆盖敏感文件。

        Raises:
            PermissionError: 目标文件是敏感文件，不允许写入
        """
        name = path.name
        if name in _DENY_WRITE_PATTERNS:
            raise PermissionError(f"Writing to '{name}' is denied by security policy")
        if path.suffix.lower() in _DENY_WRITE_EXTENSIONS:
            raise PermissionError(f"Writing to '{path.suffix}' files is denied by security policy")
        # 检查路径中是否包含 .git 目录（保护 git 仓库）
        parts = path.parts
        if ".git" in parts and name != ".gitignore":
            raise PermissionError("Writing inside .git directory is denied")
        if path.exists() and path.is_file() and self._is_probably_binary(path):
            raise PermissionError(f"Overwriting binary file is denied: {path.name}")

    @staticmethod
    def _is_probably_binary(path: Path) -> bool:
        sample = path.read_bytes()[:1024]
        if b"\x00" in sample:
            return True
        if not sample:
            return False
        text_chars = bytes(range(32, 127)) + b"\n\r\t\f\b"
        non_text = sum(byte not in text_chars for byte in sample)
        return (non_text / len(sample)) > 0.30

    @property
    def allowed_roots(self) -> list[Path]:
        return list(self._allowed_roots)

    # ------------------------------------------------------------------
    # 读取操作
    # ------------------------------------------------------------------

    def read_text(self, path: str | Path) -> str:
        """读取文本文件"""
        return self.resolve(path).read_text(encoding="utf-8")

    def read_bytes(self, path: str | Path) -> bytes:
        """读取二进制文件"""
        return self.resolve(path).read_bytes()

    # ------------------------------------------------------------------
    # 写入操作
    # ------------------------------------------------------------------

    def write_text(self, path: str | Path, content: str, *, mkdir: bool = True) -> Path:
        """
        写入文本文件。

        Args:
            path: 目标路径
            content: 文本内容
            mkdir: 是否自动创建父目录

        Returns:
            写入后的绝对路径
        """
        target = self.resolve(path)
        self._check_write_safety(target)
        if mkdir:
            target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        logger.debug("write_text: %s (%d chars)", target, len(content))
        return target

    def write_bytes(self, path: str | Path, data: bytes, *, mkdir: bool = True) -> Path:
        """写入二进制文件"""
        target = self.resolve(path)
        self._check_write_safety(target)
        if mkdir:
            target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        logger.debug("write_bytes: %s (%d bytes)", target, len(data))
        return target

    # ------------------------------------------------------------------
    # 查询操作
    # ------------------------------------------------------------------

    def exists(self, path: str | Path) -> bool:
        """检查路径是否存在"""
        try:
            return self.resolve(path).exists()
        except ValueError:
            return False

    def file_exists(self, path: str | Path) -> bool:
        """兼容更直白的文件存在性查询 API。"""
        return self.exists(path)

    def is_file(self, path: str | Path) -> bool:
        """检查是否为文件"""
        try:
            return self.resolve(path).is_file()
        except ValueError:
            return False

    def is_dir(self, path: str | Path) -> bool:
        """检查是否为目录"""
        try:
            return self.resolve(path).is_dir()
        except ValueError:
            return False

    def list_dir(
        self,
        path: str | Path,
        *,
        pattern: str = "*",
        recursive: bool = False,
    ) -> list[Path]:
        """
        列出目录内容。

        Args:
            path: 目录路径
            pattern: glob 模式 (default: "*")
            recursive: 是否递归

        Returns:
            匹配的路径列表（相对于 path）
        """
        target = self.resolve(path)
        if not target.is_dir():
            raise NotADirectoryError(f"Not a directory: {target}")
        if recursive:
            return sorted(target.rglob(pattern))
        return sorted(target.glob(pattern))

    def file_size(self, path: str | Path) -> int:
        """获取文件大小（字节）"""
        return self.resolve(path).stat().st_size

    # ------------------------------------------------------------------
    # 文件操作
    # ------------------------------------------------------------------

    def copy(self, src: str | Path, dst: str | Path, *, mkdir: bool = True) -> Path:
        """
        复制文件。

        两端路径都必须在 allowed_roots 内。
        """
        src_path = self.resolve(src)
        dst_path = self.resolve(dst)
        self._check_write_safety(dst_path)
        if not src_path.is_file():
            raise FileNotFoundError(f"Source file not found: {src_path}")
        if mkdir:
            dst_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src_path), str(dst_path))
        logger.debug("copy: %s → %s", src_path, dst_path)
        return dst_path

    def move(self, src: str | Path, dst: str | Path, *, mkdir: bool = True) -> Path:
        """
        移动/重命名文件。

        两端路径都必须在 allowed_roots 内。
        """
        src_path = self.resolve(src)
        dst_path = self.resolve(dst)
        self._check_write_safety(dst_path)
        if not src_path.exists():
            raise FileNotFoundError(f"Source not found: {src_path}")
        if mkdir:
            dst_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src_path), str(dst_path))
        logger.debug("move: %s → %s", src_path, dst_path)
        return dst_path

    def mkdir(self, path: str | Path) -> Path:
        """创建目录（含父目录）"""
        target = self.resolve(path)
        target.mkdir(parents=True, exist_ok=True)
        return target
