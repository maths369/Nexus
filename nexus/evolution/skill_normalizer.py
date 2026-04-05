from __future__ import annotations

import base64
import hashlib
import json
import shutil
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import yaml


@dataclass(frozen=True)
class NormalizedSkillBundle:
    bundle_dir: Path
    manifest_path: Path
    archive_integrity: str


class SkillNormalizer:
    def normalize_clawhub_archive(
        self,
        archive_path: str | Path,
        output_root: str | Path,
        *,
        slug: str,
        integrity: str | None = None,
    ) -> NormalizedSkillBundle:
        archive = Path(archive_path).expanduser().resolve()
        if not archive.exists():
            raise FileNotFoundError(f"ClawHub archive not found: {archive}")

        actual_integrity = self._compute_integrity(archive.read_bytes())
        if integrity and integrity.strip() and integrity.strip() != actual_integrity:
            raise ValueError("ClawHub archive integrity mismatch")

        output_dir = Path(output_root).expanduser().resolve()
        extract_dir = output_dir / "extracted"
        normalized_dir = output_dir / "normalized" / slug
        shutil.rmtree(output_dir, ignore_errors=True)
        extract_dir.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(archive) as zf:
            self._extract_zip_safely(zf, extract_dir)

        bundle_root = self._find_skill_bundle_root(extract_dir)
        shutil.copytree(bundle_root, normalized_dir)

        lower_skill_md = normalized_dir / "skill.md"
        upper_skill_md = normalized_dir / "SKILL.md"
        if not upper_skill_md.exists() and lower_skill_md.exists():
            lower_skill_md.rename(upper_skill_md)

        if not upper_skill_md.exists():
            raise FileNotFoundError(f"Missing SKILL.md after normalization: {normalized_dir}")

        self._validate_supported_openclaw_install_specs(upper_skill_md)
        return NormalizedSkillBundle(
            bundle_dir=normalized_dir,
            manifest_path=upper_skill_md,
            archive_integrity=actual_integrity,
        )

    @staticmethod
    def _compute_integrity(payload: bytes) -> str:
        digest = hashlib.sha256(payload).digest()
        return f"sha256-{base64.b64encode(digest).decode('ascii')}"

    @staticmethod
    def _extract_zip_safely(zf: zipfile.ZipFile, extract_dir: Path) -> None:
        for info in zf.infolist():
            member = PurePosixPath(info.filename)
            if not info.filename or info.filename.endswith("/"):
                continue
            if member.is_absolute() or ".." in member.parts:
                raise ValueError(f"Unsafe archive path: {info.filename}")
            mode = (info.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode):
                raise ValueError(f"Symlinks are not allowed in ClawHub archives: {info.filename}")
            target = (extract_dir / Path(*member.parts)).resolve()
            if extract_dir.resolve() not in target.parents and target != extract_dir.resolve():
                raise ValueError(f"Archive extraction escaped target dir: {info.filename}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)

    @staticmethod
    def _find_skill_bundle_root(extract_dir: Path) -> Path:
        direct_candidates = [
            extract_dir,
            *(child for child in sorted(extract_dir.iterdir()) if child.is_dir()),
        ]
        for candidate in direct_candidates:
            if (candidate / "SKILL.md").exists() or (candidate / "skill.md").exists():
                return candidate

        manifest_candidates = sorted(
            path.parent
            for path in extract_dir.rglob("*")
            if path.is_file() and path.name in {"SKILL.md", "skill.md"}
        )
        if len(manifest_candidates) == 1:
            return manifest_candidates[0]
        if not manifest_candidates:
            raise FileNotFoundError(f"No SKILL.md or skill.md found in archive: {extract_dir}")
        raise ValueError(f"Archive contains multiple skill manifests: {extract_dir}")

    def _validate_supported_openclaw_install_specs(self, manifest_path: Path) -> None:
        meta = self._parse_frontmatter(manifest_path.read_text(encoding="utf-8"))
        metadata_raw = meta.get("metadata")
        openclaw: dict[str, Any] | None = None
        if isinstance(metadata_raw, dict):
            openclaw = metadata_raw.get("openclaw")
        elif isinstance(metadata_raw, str):
            try:
                parsed = json.loads(metadata_raw)
            except (json.JSONDecodeError, TypeError):
                parsed = None
            if isinstance(parsed, dict):
                openclaw = parsed.get("openclaw")

        if not isinstance(openclaw, dict):
            return

        install_specs = openclaw.get("install") or []
        if not isinstance(install_specs, list):
            raise ValueError("OpenClaw install metadata must be a list")

        errors: list[str] = []
        for item in install_specs:
            if not isinstance(item, dict):
                errors.append("OpenClaw install spec must be an object")
                continue
            kind = str(item.get("kind") or item.get("type") or "").strip().lower()
            if kind == "uv":
                if not str(item.get("package") or "").strip():
                    errors.append("OpenClaw uv install spec requires package")
            elif kind == "brew":
                if not str(item.get("formula") or "").strip():
                    errors.append("OpenClaw brew install spec requires formula")
            elif kind == "node":
                if not str(item.get("package") or "").strip():
                    errors.append("OpenClaw node install spec requires package")
            elif kind == "go":
                if not str(item.get("module") or "").strip():
                    errors.append("OpenClaw go install spec requires module")
            else:
                errors.append(f"Unsupported OpenClaw install kind: {kind or '<missing>'}")
        if errors:
            raise ValueError("; ".join(errors))

    @staticmethod
    def _parse_frontmatter(text: str) -> dict[str, Any]:
        if not text.startswith("---\n"):
            return {}
        parts = text.split("\n---\n", 1)
        if len(parts) != 2:
            return {}
        raw = parts[0].removeprefix("---\n")
        try:
            payload = yaml.safe_load(raw) or {}
        except yaml.YAMLError:
            return {}
        return payload if isinstance(payload, dict) else {}
