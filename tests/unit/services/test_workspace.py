"""Workspace Service 测试 — 路径安全、读写操作、安全策略"""

from __future__ import annotations

import pytest
from pathlib import Path

from nexus.services.workspace import WorkspaceService


def _ws(tmp_path: Path) -> WorkspaceService:
    """创建一个以 tmp_path 为根的 WorkspaceService"""
    root = tmp_path / "workspace"
    root.mkdir()
    return WorkspaceService([root])


# ---------------------------------------------------------------------------
# 路径安全
# ---------------------------------------------------------------------------

def test_resolve_allows_path_within_root(tmp_path):
    ws = _ws(tmp_path)
    root = ws.allowed_roots[0]
    target = root / "subdir" / "file.txt"
    target.parent.mkdir(parents=True)
    target.touch()
    assert ws.resolve(target) == target.resolve()


def test_resolve_blocks_path_outside_root(tmp_path):
    ws = _ws(tmp_path)
    outside = tmp_path / "outside" / "secret.txt"
    outside.parent.mkdir(parents=True)
    outside.touch()
    with pytest.raises(ValueError, match="not allowed"):
        ws.resolve(outside)


def test_resolve_blocks_path_traversal(tmp_path):
    ws = _ws(tmp_path)
    root = ws.allowed_roots[0]
    # 试图通过 .. 逃逸
    traversal = root / ".." / ".." / "etc" / "passwd"
    with pytest.raises(ValueError, match="not allowed"):
        ws.resolve(traversal)


def test_resolve_allows_root_itself(tmp_path):
    ws = _ws(tmp_path)
    root = ws.allowed_roots[0]
    assert ws.resolve(root) == root.resolve()


def test_resolve_blocks_prefix_collision(tmp_path):
    """确保 /allowed_root_extra 不会被 /allowed_root 误放行"""
    root = tmp_path / "workspace"
    root.mkdir()
    # 创建一个名称是 root 的前缀但不同的目录
    collision = tmp_path / "workspace_extra"
    collision.mkdir()
    (collision / "file.txt").touch()

    ws = WorkspaceService([root])
    with pytest.raises(ValueError, match="not allowed"):
        ws.resolve(collision / "file.txt")


# ---------------------------------------------------------------------------
# 读写操作
# ---------------------------------------------------------------------------

def test_read_write_text(tmp_path):
    ws = _ws(tmp_path)
    root = ws.allowed_roots[0]
    target = root / "notes" / "hello.md"

    ws.write_text(target, "# Hello\n\nWorld")
    assert ws.read_text(target) == "# Hello\n\nWorld"


def test_read_write_bytes(tmp_path):
    ws = _ws(tmp_path)
    root = ws.allowed_roots[0]
    target = root / "data.bin"

    data = b"\x00\x01\x02\x03"
    ws.write_bytes(target, data)
    assert ws.read_bytes(target) == data


def test_write_creates_parent_dirs(tmp_path):
    ws = _ws(tmp_path)
    root = ws.allowed_roots[0]
    target = root / "deep" / "nested" / "dir" / "file.txt"

    ws.write_text(target, "content")
    assert target.exists()
    assert target.read_text() == "content"


# ---------------------------------------------------------------------------
# 写入安全策略
# ---------------------------------------------------------------------------

def test_write_blocks_env_file(tmp_path):
    ws = _ws(tmp_path)
    root = ws.allowed_roots[0]
    with pytest.raises(PermissionError, match="denied"):
        ws.write_text(root / ".env", "SECRET=oops")


def test_write_blocks_credentials_json(tmp_path):
    ws = _ws(tmp_path)
    root = ws.allowed_roots[0]
    with pytest.raises(PermissionError, match="denied"):
        ws.write_text(root / "credentials.json", "{}")


def test_write_blocks_pem_files(tmp_path):
    ws = _ws(tmp_path)
    root = ws.allowed_roots[0]
    with pytest.raises(PermissionError, match="denied"):
        ws.write_text(root / "server.pem", "cert data")


def test_write_blocks_git_internals(tmp_path):
    ws = _ws(tmp_path)
    root = ws.allowed_roots[0]
    with pytest.raises(PermissionError, match="denied"):
        ws.write_text(root / ".git" / "HEAD", "ref: refs/heads/main")


def test_write_allows_gitignore(tmp_path):
    """特例: .gitignore 在 .git 外应该允许写入"""
    ws = _ws(tmp_path)
    root = ws.allowed_roots[0]
    ws.write_text(root / ".gitignore", "*.pyc\n")
    assert ws.read_text(root / ".gitignore") == "*.pyc\n"


# ---------------------------------------------------------------------------
# 查询操作
# ---------------------------------------------------------------------------

def test_exists_and_is_file_and_is_dir(tmp_path):
    ws = _ws(tmp_path)
    root = ws.allowed_roots[0]

    (root / "file.txt").write_text("x")
    (root / "subdir").mkdir()

    assert ws.exists(root / "file.txt")
    assert ws.is_file(root / "file.txt")
    assert not ws.is_dir(root / "file.txt")

    assert ws.exists(root / "subdir")
    assert ws.is_dir(root / "subdir")
    assert not ws.is_file(root / "subdir")

    assert not ws.exists(root / "nope.txt")


def test_exists_returns_false_for_disallowed_path(tmp_path):
    ws = _ws(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").touch()
    # 不应该抛异常，而是返回 False
    assert not ws.exists(outside / "secret.txt")


def test_list_dir(tmp_path):
    ws = _ws(tmp_path)
    root = ws.allowed_roots[0]

    (root / "a.txt").touch()
    (root / "b.md").touch()
    (root / "sub").mkdir()
    (root / "sub" / "c.txt").touch()

    # 非递归
    items = ws.list_dir(root, pattern="*.txt")
    assert len(items) == 1
    assert items[0].name == "a.txt"

    # 递归
    items = ws.list_dir(root, pattern="*.txt", recursive=True)
    names = [p.name for p in items]
    assert "a.txt" in names
    assert "c.txt" in names


def test_file_size(tmp_path):
    ws = _ws(tmp_path)
    root = ws.allowed_roots[0]
    (root / "data.txt").write_text("hello")
    assert ws.file_size(root / "data.txt") == 5


# ---------------------------------------------------------------------------
# 文件操作
# ---------------------------------------------------------------------------

def test_copy_file(tmp_path):
    ws = _ws(tmp_path)
    root = ws.allowed_roots[0]
    src = root / "original.txt"
    dst = root / "copied.txt"
    src.write_text("content")

    ws.copy(src, dst)
    assert dst.read_text() == "content"
    assert src.exists()  # 原文件仍在


def test_move_file(tmp_path):
    ws = _ws(tmp_path)
    root = ws.allowed_roots[0]
    src = root / "original.txt"
    dst = root / "moved.txt"
    src.write_text("content")

    ws.move(src, dst)
    assert dst.read_text() == "content"
    assert not src.exists()  # 原文件已移走


def test_copy_blocks_cross_root(tmp_path):
    """不允许复制到 allowed_roots 之外"""
    ws = _ws(tmp_path)
    root = ws.allowed_roots[0]
    src = root / "file.txt"
    src.write_text("x")

    outside = tmp_path / "outside" / "stolen.txt"
    outside.parent.mkdir()
    with pytest.raises(ValueError, match="not allowed"):
        ws.copy(src, outside)


def test_mkdir_creates_nested_dir(tmp_path):
    ws = _ws(tmp_path)
    root = ws.allowed_roots[0]
    target = root / "a" / "b" / "c"
    ws.mkdir(target)
    assert target.is_dir()
