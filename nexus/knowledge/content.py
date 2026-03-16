"""Layer 1: canonical Vault content storage."""

from __future__ import annotations

import re
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

_DEFAULT_DIRS = [
    "pages",
    "journals",
    "meetings",
    "inbox",
    "strategy",
    "rnd",
    "life",
    "_system/audio",
    "_system/transcripts",
    "_system/artifacts",
    "_system/backups",
    "_system/memory",
]


@dataclass
class VaultPage:
    page_id: str
    relative_path: str
    title: str
    body: str
    page_type: str = "note"
    metadata: dict[str, Any] = field(default_factory=dict)


class VaultContentStore:
    """Safe Markdown-first content store for Nexus vault."""

    def __init__(self, base_path: Path, ensure_directories: bool = True):
        self._base_path = Path(base_path)
        if ensure_directories:
            for item in _DEFAULT_DIRS:
                (self._base_path / item).mkdir(parents=True, exist_ok=True)

    @property
    def base_path(self) -> Path:
        return self._base_path

    def resolve_path(self, relative_path: str) -> Path:
        target = (self._base_path / relative_path).resolve()
        if not str(target).startswith(str(self._base_path.resolve())):
            raise ValueError("Attempted to access outside of vault root")
        return target

    def list_markdown(self, relative_dir: str = "", limit: int = 500) -> list[str]:
        target_dir = self.resolve_path(relative_dir or ".")
        if not target_dir.exists():
            return []
        files: list[str] = []
        for path in sorted(target_dir.rglob("*.md")):
            rel = path.relative_to(self._base_path)
            files.append(str(rel))
            if len(files) >= limit:
                break
        return files

    def exists(self, relative_path: str) -> bool:
        return self.resolve_path(relative_path).exists()

    def read(self, relative_path: str) -> str:
        return self.resolve_path(relative_path).read_text(encoding="utf-8")

    def write(self, relative_path: str, content: str, create_if_missing: bool = True) -> Path:
        target = self.resolve_path(relative_path)
        if not target.exists() and not create_if_missing:
            raise FileNotFoundError(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(target, content)
        return target

    def move(self, relative_path: str, new_relative_path: str, overwrite: bool = False) -> Path:
        source = self.resolve_path(relative_path)
        if not source.exists():
            raise FileNotFoundError(relative_path)
        target = self.resolve_path(new_relative_path)
        if target.exists() and not overwrite:
            raise FileExistsError(new_relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        source.replace(target)
        return target

    def delete(self, relative_path: str) -> None:
        target = self.resolve_path(relative_path)
        if not target.exists():
            raise FileNotFoundError(relative_path)
        if target.is_dir():
            raise ValueError("Deleting directories is not supported")
        target.unlink()

    def backup(self, relative_path: str) -> Path:
        target = self.resolve_path(relative_path)
        if not target.exists():
            raise FileNotFoundError(relative_path)
        timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        sanitized = relative_path.replace("/", "__")
        backup_name = f"{sanitized}--{timestamp}{target.suffix or '.bak'}"
        backup_path = self.resolve_path(f"_system/backups/{backup_name}")
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, backup_path)
        return backup_path

    def create_page(
        self,
        *,
        title: str,
        body: str = "",
        section: str = "pages",
        page_type: str = "note",
        filename: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> VaultPage:
        page_id = str(uuid.uuid4())
        slug = filename or self._slugify(title)
        relative_path = self._ensure_unique_relative_path(f"{section}/{slug}.md")
        content = self._render_markdown(title=title, body=body, metadata=metadata)
        self.write(relative_path, content, create_if_missing=True)
        return VaultPage(
            page_id=page_id,
            relative_path=relative_path,
            title=title,
            body=body,
            page_type=page_type,
            metadata=metadata or {},
        )

    def extract_title(self, content: str, fallback: str = "") -> str:
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                return stripped[2:].strip()
        return fallback

    def _ensure_unique_relative_path(self, relative_path: str) -> str:
        target = self.resolve_path(relative_path)
        if not target.exists():
            return relative_path
        path = Path(relative_path)
        for idx in range(1, 1000):
            candidate = path.with_name(f"{path.stem}-{idx}{path.suffix}")
            if not self.resolve_path(candidate.as_posix()).exists():
                return candidate.as_posix()
        return path.with_name(f"{path.stem}-{uuid.uuid4().hex[:8]}{path.suffix}").as_posix()

    @staticmethod
    def _slugify(value: str) -> str:
        value = value.strip()
        normalized = re.sub(r"\s+", "-", value)
        normalized = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]", "", normalized)
        return normalized[:80] or f"page-{uuid.uuid4().hex[:8]}"

    @staticmethod
    def _render_markdown(title: str, body: str, metadata: dict[str, Any] | None = None) -> str:
        body = body.strip()
        if body.startswith("# "):
            header = []
        else:
            header = [f"# {title}", ""]
        if metadata:
            header.append("<!-- metadata:")
            for key, value in metadata.items():
                header.append(f"{key}: {value}")
            header.append("-->")
            header.append("")
        return "\n".join(header + ([body] if body else [])) + "\n"

    @staticmethod
    def _atomic_write(target: Path, content: str) -> None:
        temp = target.with_suffix(target.suffix + ".tmp")
        temp.write_text(content, encoding="utf-8")
        temp.replace(target)
