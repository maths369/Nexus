"""
Skill Manager — managed skill lifecycle and installable skill registry.

This module now serves two roles:
1. Manage installed instruction skills under `skills/`
2. Manage installable skill bundles from a catalog under `skill_registry/`

OpenClaw alignment:
- permanent extension is a managed object on disk
- discover -> install -> verify -> persist
- system_run is substrate, not the capability itself
"""

from __future__ import annotations

import asyncio
import importlib.util
import hashlib
import json
import logging
import os
import platform
import re
import shlex
import shutil
import sys
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from .audit import AuditLog
from .clawhub_client import ClawHubClient
from .sandbox import Sandbox
from .skill_normalizer import SkillNormalizer
from .types import ChangeResult, CheckResult, SkillSpec, VerifyResult

logger = logging.getLogger(__name__)

_SKILL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{1,62}[a-z0-9]$")
_PACKAGE_RE = re.compile(r"^[A-Za-z0-9_.-]+([<>=!~]=?.+)?$")
_IMPORT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_\\.]*$")
_GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_CLAWHUB_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_UNSAFE_SHELL_CHARS_RE = re.compile(r"[;&|><`\n\r]")
_SKILL_METADATA_DIR = ".nexus"
_SKILL_ORIGIN_FILE = "origin.json"
_SKILL_SOURCE_FILE = "source.json"
_SKILL_LOCK_FILE = "lock.json"
_ALLOWED_INSTALL_EXECUTABLES = {"brew", "go", "npm", "pnpm", "yarn"}


@dataclass(frozen=True)
class InstallableSkillSpec:
    skill_id: str
    name: str
    description: str
    tags: tuple[str, ...]
    keywords: tuple[str, ...]
    packages: tuple[str, ...]
    install_commands: tuple[str, ...]
    verify_imports: tuple[str, ...]
    verify_commands: tuple[str, ...]
    manifest_path: Path
    skill_dir: Path
    # OpenClaw eligibility — requires block
    requires_bins: tuple[str, ...] = ()
    requires_env: tuple[str, ...] = ()
    requires_os: tuple[str, ...] = ()
    unsupported_install_specs: tuple[str, ...] = ()


@dataclass(frozen=True)
class RemoteSkillSource:
    name: str
    repo: str
    ref: str = "main"
    roots: tuple[str, ...] = ()


class SkillManager:
    """
    Skill lifecycle manager.

    Directory structure:
      skills/                     — installed skills
      skill_registry/<id>/        — installable skill bundles
      staging/<skill-id>/         — temporary validation area
    """

    def __init__(
        self,
        skills_dir: Path,
        sandbox: Sandbox,
        audit: AuditLog,
        *,
        catalog_dir: Path | None = None,
        system_runner: Any | None = None,
        python_executable: str | None = None,
        remote_sources: list[dict[str, Any]] | None = None,
        clawhub_config: dict[str, Any] | None = None,
        clawhub_client: ClawHubClient | None = None,
    ):
        self._skills_dir = Path(skills_dir)
        self._sandbox = sandbox
        self._audit = audit
        self._catalog_dir = Path(catalog_dir) if catalog_dir is not None else None
        self._system_runner = system_runner
        self._python = self._resolve_python(python_executable)
        self._remote_sources = self._coerce_remote_sources(remote_sources)
        self._clawhub = clawhub_client or ClawHubClient.from_config(clawhub_config)
        self._skill_normalizer = SkillNormalizer()
        self._lock_path = self._skills_dir / _SKILL_LOCK_FILE
        self._skills_dir.mkdir(parents=True, exist_ok=True)
        if self._catalog_dir is not None:
            self._catalog_dir.mkdir(parents=True, exist_ok=True)

    def _resolve_python(self, candidate: str | None) -> str:
        if candidate:
            path = Path(candidate).expanduser()
            if path.exists():
                return str(path)
        return sys.executable

    # ------------------------------------------------------------------
    # Installable skill registry (OpenClaw-style control plane objects)
    # ------------------------------------------------------------------

    def list_installable_skills(self, query: str | None = None) -> list[dict[str, Any]]:
        specs = self._iter_installable_specs()
        scored: list[tuple[float, InstallableSkillSpec]] = []
        query_text = (query or "").strip()
        for spec in specs:
            score = self._score_installable(spec, query_text)
            if query_text and score <= 0:
                continue
            scored.append((score, spec))

        scored.sort(key=lambda item: (-item[0], item[1].skill_id))
        return [
            {
                "skill_id": spec.skill_id,
                "name": spec.name,
                "description": spec.description,
                "tags": list(spec.tags),
                "keywords": list(spec.keywords),
                "packages": list(spec.packages),
                "install_commands": list(spec.install_commands),
                "verify_imports": list(spec.verify_imports),
                "verify_commands": list(spec.verify_commands),
                "unsupported_install_specs": list(spec.unsupported_install_specs),
                "installed": (self._skills_dir / spec.skill_id).is_dir(),
                "manifest_path": str(spec.manifest_path),
                "match_score": float(score),
            }
            for score, spec in scored
        ]

    async def install_from_catalog(
        self,
        skill_id: str,
        *,
        actor: str = "system",
    ) -> dict[str, Any]:
        spec = self._get_installable_spec(skill_id)
        if spec is None:
            return {
                "success": False,
                "skill_id": skill_id,
                "reason": f"Installable skill not found: {skill_id}",
                "installed": False,
                "verify_summary": "",
                "installed_path": "",
            }

        staging_dir = self._sandbox.staging_dir / "installable_skills" / skill_id
        shutil.rmtree(staging_dir, ignore_errors=True)
        staging_dir.parent.mkdir(parents=True, exist_ok=True)

        try:
            shutil.copytree(spec.skill_dir, staging_dir)
        except Exception as exc:
            self._audit.record(
                action="skill_install_failed",
                target=skill_id,
                actor=actor,
                details={"phase": "copy", "source": str(spec.skill_dir)},
                success=False,
                error=str(exc),
            )
            return {
                "success": False,
                "skill_id": skill_id,
                "reason": f"Failed to copy skill bundle: {exc}",
                "installed": False,
                "verify_summary": "",
                "installed_path": "",
            }

        staged_spec = replace(
            spec,
            manifest_path=staging_dir / "SKILL.md",
            skill_dir=staging_dir,
        )

        verify_result = self._verify_installable_bundle(staging_dir, staged_spec)
        if not verify_result.passed:
            shutil.rmtree(staging_dir, ignore_errors=True)
            self._audit.record(
                action="skill_install_blocked",
                target=skill_id,
                actor=actor,
                details={
                    "phase": "static_verify",
                    "summary": verify_result.summary,
                    "checks": [
                        {
                            "name": item.name,
                            "passed": item.passed,
                            "message": item.message,
                        }
                        for item in verify_result.checks
                    ],
                },
                success=False,
                error=verify_result.summary,
            )
            return {
                "success": False,
                "skill_id": skill_id,
                "reason": verify_result.summary,
                "installed": False,
                "verify_summary": verify_result.summary,
                "installed_path": "",
            }

        install_result = await self._install_bundle_dependencies(staged_spec, actor=actor)
        if not install_result["success"]:
            shutil.rmtree(staging_dir, ignore_errors=True)
            return {
                "success": False,
                "skill_id": skill_id,
                "reason": install_result["reason"],
                "installed": False,
                "verify_summary": install_result["reason"],
                "installed_path": "",
            }

        runtime_verify = await self._verify_runtime_requirements(staged_spec)
        if not runtime_verify.passed:
            shutil.rmtree(staging_dir, ignore_errors=True)
            self._audit.record(
                action="skill_install_blocked",
                target=skill_id,
                actor=actor,
                details={
                    "phase": "runtime_verify",
                    "summary": runtime_verify.summary,
                    "checks": [
                        {
                            "name": item.name,
                            "passed": item.passed,
                            "message": item.message,
                        }
                        for item in runtime_verify.checks
                    ],
                },
                success=False,
                error=runtime_verify.summary,
            )
            return {
                "success": False,
                "skill_id": skill_id,
                "reason": runtime_verify.summary,
                "installed": False,
                "verify_summary": runtime_verify.summary,
                "installed_path": "",
            }

        installed_dir = self._skills_dir / skill_id
        backup_id: str | None = None
        if installed_dir.exists():
            backup_id = str(uuid.uuid4())[:8]
            backup_dir = self._skills_dir / f"{skill_id}.backup.{backup_id}"
            installed_dir.rename(backup_dir)

        shutil.move(str(staging_dir), str(installed_dir))
        origin_payload = self._write_skill_origin_and_lock(
            skill_id=skill_id,
            skill_dir=installed_dir,
            actor=actor,
            source_metadata=self._merge_skill_source_metadata(
                installed_dir,
                {
                    "source_type": "catalog",
                    "catalog_manifest_path": str(spec.manifest_path),
                    "catalog_skill_dir": str(spec.skill_dir),
                    "skill_id": skill_id,
                },
            ),
        )
        self._audit.record(
            action="skill_installed",
            target=skill_id,
            actor=actor,
            details={
                "path": str(installed_dir),
                "manifest_path": str(spec.manifest_path),
                "packages": list(spec.packages),
                "install_commands": list(spec.install_commands),
                "backup_id": backup_id,
                "origin_path": str(self._skill_origin_path(installed_dir)),
                "content_fingerprint": origin_payload.get("content_fingerprint", ""),
            },
        )
        return {
            "success": True,
            "skill_id": skill_id,
            "reason": "Skill installed from registry",
            "installed": True,
            "verify_summary": runtime_verify.summary,
            "installed_path": str(installed_dir),
            "backup_id": backup_id,
        }

    async def import_from_local(
        self,
        source_path: str | Path,
        *,
        actor: str = "system",
        install: bool = False,
        skill_id: str | None = None,
        source_label: str | None = None,
        source_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self._catalog_dir is None:
            return {
                "success": False,
                "skill_id": skill_id or "",
                "reason": "Skill registry unavailable",
                "imported": False,
                "installed": False,
                "catalog_path": "",
                "installed_path": "",
            }

        try:
            bundle_dir = self._resolve_skill_bundle_dir(Path(source_path))
            source_spec = self._prepare_installable_spec(bundle_dir, skill_id)
        except Exception as exc:
            reason = str(exc)
            self._audit.record(
                action="skill_import_failed",
                target=skill_id or Path(source_path).name,
                actor=actor,
                details={"phase": "resolve_local_source", "source": str(source_path)},
                success=False,
                error=reason,
            )
            return {
                "success": False,
                "skill_id": skill_id or "",
                "reason": reason,
                "imported": False,
                "installed": False,
                "catalog_path": "",
                "installed_path": "",
            }

        staging_root = self._sandbox.staging_dir / "importable_skills" / f"{source_spec.skill_id}-{uuid.uuid4().hex[:8]}"
        staging_dir = staging_root / source_spec.skill_id
        shutil.rmtree(staging_root, ignore_errors=True)

        try:
            shutil.copytree(bundle_dir, staging_dir)
            staged_spec = replace(
                source_spec,
                manifest_path=staging_dir / "SKILL.md",
                skill_dir=staging_dir,
            )
            self._write_skill_source_metadata(
                staging_dir,
                source_metadata
                or {
                    "source_type": "local",
                    "source_label": source_label or str(bundle_dir),
                    "source_path": str(bundle_dir),
                    "skill_id": staged_spec.skill_id,
                    "imported_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            verify_result = self._verify_installable_bundle(staging_dir, staged_spec)
            if not verify_result.passed:
                self._audit.record(
                    action="skill_import_failed",
                    target=staged_spec.skill_id,
                    actor=actor,
                    details={
                        "phase": "verify",
                        "source": source_label or str(bundle_dir),
                        "checks": [
                            {"name": c.name, "passed": c.passed, "message": c.message}
                            for c in verify_result.checks
                        ],
                    },
                    success=False,
                    error=verify_result.summary,
                )
                return {
                    "success": False,
                    "skill_id": staged_spec.skill_id,
                    "reason": f"Verification failed: {verify_result.summary}",
                    "imported": False,
                    "installed": False,
                    "catalog_path": "",
                    "installed_path": "",
                }

            catalog_path, backup_id = self._promote_catalog_bundle(staging_dir, staged_spec.skill_id)
            self._audit.record(
                action="skill_imported",
                target=staged_spec.skill_id,
                actor=actor,
                details={
                    "source": source_label or str(bundle_dir),
                    "catalog_path": str(catalog_path),
                    "backup_id": backup_id,
                },
            )

            result: dict[str, Any] = {
                "success": True,
                "skill_id": staged_spec.skill_id,
                "reason": "Skill imported to registry",
                "imported": True,
                "installed": False,
                "catalog_path": str(catalog_path),
                "installed_path": "",
                "backup_id": backup_id,
            }
            if install:
                install_result = await self.install_from_catalog(staged_spec.skill_id, actor=actor)
                result.update(
                    {
                        "success": bool(install_result.get("success", False)),
                        "reason": (
                            "Skill imported and installed"
                            if install_result.get("success")
                            else f"Skill imported but install failed: {install_result.get('reason', '')}"
                        ),
                        "installed": bool(install_result.get("installed", False)),
                        "installed_path": str(install_result.get("installed_path", "")),
                        "verify_summary": str(install_result.get("verify_summary", "")),
                        "install_backup_id": install_result.get("backup_id"),
                    }
                )
            return result
        except Exception as exc:
            reason = str(exc)
            self._audit.record(
                action="skill_import_failed",
                target=source_spec.skill_id,
                actor=actor,
                details={"phase": "import_local", "source": source_label or str(bundle_dir)},
                success=False,
                error=reason,
            )
            return {
                "success": False,
                "skill_id": source_spec.skill_id,
                "reason": reason,
                "imported": False,
                "installed": False,
                "catalog_path": "",
                "installed_path": "",
            }
        finally:
            shutil.rmtree(staging_root, ignore_errors=True)

    async def import_from_remote(
        self,
        repo: str,
        skill_path: str,
        *,
        ref: str = "main",
        actor: str = "system",
        install: bool = False,
        skill_id: str | None = None,
    ) -> dict[str, Any]:
        fetch_root = self._sandbox.staging_dir / "remote_skill_fetch" / uuid.uuid4().hex[:8]
        try:
            bundle_dir = await self._fetch_remote_skill_bundle(repo, skill_path, ref, fetch_root)
        except Exception as exc:
            reason = str(exc)
            self._audit.record(
                action="skill_import_failed",
                target=skill_id or Path(skill_path).name,
                actor=actor,
                details={"phase": "remote_fetch", "repo": repo, "path": skill_path, "ref": ref},
                success=False,
                error=reason,
            )
            return {
                "success": False,
                "skill_id": skill_id or "",
                "reason": reason,
                "imported": False,
                "installed": False,
                "catalog_path": "",
                "installed_path": "",
            }

        try:
            return await self.import_from_local(
                bundle_dir,
                actor=actor,
                install=install,
                skill_id=skill_id,
                source_label=f"github:{self._normalize_github_repo(repo)}@{ref}:{self._normalize_remote_skill_path(skill_path)}",
                source_metadata={
                    "source_type": "github",
                    "repo": self._normalize_github_repo(repo),
                    "ref": ref,
                    "remote_path": self._normalize_remote_skill_path(skill_path),
                    "source_label": f"github:{self._normalize_github_repo(repo)}@{ref}:{self._normalize_remote_skill_path(skill_path)}",
                    "imported_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        finally:
            shutil.rmtree(fetch_root, ignore_errors=True)

    async def import_from_clawhub(
        self,
        slug: str,
        *,
        version: str | None = None,
        tag: str | None = None,
        actor: str = "system",
        install: bool = False,
        skill_id: str | None = None,
        base_url: str | None = None,
    ) -> dict[str, Any]:
        normalized_slug = self._normalize_clawhub_slug(slug)
        fetch_root = self._sandbox.staging_dir / "clawhub_skill_fetch" / uuid.uuid4().hex[:8]
        archive_parent: Path | None = None
        try:
            detail = await self._clawhub.get_skill_detail(normalized_slug, base_url=base_url)
            skill_detail = detail.get("skill") if isinstance(detail, dict) else None
            if not isinstance(skill_detail, dict):
                raise RuntimeError(f'ClawHub skill "{normalized_slug}" not found')
            latest_version = ""
            latest_payload = detail.get("latestVersion") if isinstance(detail, dict) else None
            if isinstance(latest_payload, dict):
                latest_version = str(latest_payload.get("version") or "").strip()
            resolved_version = (version or latest_version or "").strip()
            archive = await self._clawhub.download_skill_archive(
                normalized_slug,
                version=resolved_version or None,
                tag=tag,
                base_url=base_url,
            )
            archive_parent = archive.archive_path.parent
            normalized_bundle = self._skill_normalizer.normalize_clawhub_archive(
                archive.archive_path,
                fetch_root,
                slug=normalized_slug,
                integrity=archive.integrity,
            )
        except Exception as exc:
            reason = str(exc)
            self._audit.record(
                action="skill_import_failed",
                target=skill_id or normalized_slug,
                actor=actor,
                details={
                    "phase": "clawhub_fetch",
                    "slug": normalized_slug,
                    "version": version or "",
                    "tag": tag or "",
                },
                success=False,
                error=reason,
            )
            return {
                "success": False,
                "skill_id": skill_id or normalized_slug,
                "reason": reason,
                "imported": False,
                "installed": False,
                "catalog_path": "",
                "installed_path": "",
            }

        owner = detail.get("owner") if isinstance(detail, dict) else {}
        owner_handle = str(owner.get("handle") or "") if isinstance(owner, dict) else ""
        owner_display_name = str(owner.get("displayName") or "") if isinstance(owner, dict) else ""
        publisher = owner_display_name or owner_handle
        source_version = (version or latest_version or "").strip()
        source_label = f"clawhub:{normalized_slug}@{source_version or 'latest'}"

        try:
            return await self.import_from_local(
                normalized_bundle.bundle_dir,
                actor=actor,
                install=install,
                skill_id=skill_id,
                source_label=source_label,
                source_metadata={
                    "source_type": "clawhub",
                    "source_label": source_label,
                    "registry": self._clawhub.resolve_base_url(base_url),
                    "slug": normalized_slug,
                    "requested_version": str(version or "").strip(),
                    "installed_version": source_version,
                    "publisher": publisher,
                    "owner_handle": owner_handle,
                    "owner_display_name": owner_display_name,
                    "archive_integrity": normalized_bundle.archive_integrity,
                    "skill_id": skill_id or normalized_slug,
                    "imported_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        finally:
            shutil.rmtree(fetch_root, ignore_errors=True)
            if archive_parent is not None:
                shutil.rmtree(archive_parent, ignore_errors=True)

    async def search_remote_skills(
        self,
        query: str,
        *,
        repo: str | None = None,
        ref: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        query_text = (query or "").strip()
        if not query_text:
            return []

        results: list[dict[str, Any]] = []
        capped_limit = max(1, min(int(limit or 10), 50))
        for source in self._iter_remote_sources(repo=repo, ref=ref):
            try:
                candidates = await self._search_remote_source(
                    source,
                    query_text,
                    limit=max(capped_limit * 8, 40),
                )
            except Exception as exc:
                self._audit.record(
                    action="skill_remote_search_failed",
                    target=source.repo,
                    actor="system",
                    details={
                        "phase": "remote_search",
                        "repo": source.repo,
                        "ref": source.ref,
                        "roots": list(source.roots),
                        "query": query_text,
                    },
                    success=False,
                    error=str(exc),
                )
                continue
            for spec, manifest_path in candidates:
                score = self._score_installable(spec, query_text)
                if score <= 0:
                    continue
                results.append(
                    {
                        "skill_id": spec.skill_id,
                        "name": spec.name,
                        "description": spec.description,
                        "tags": list(spec.tags),
                        "keywords": list(spec.keywords),
                        "repo": source.repo,
                        "ref": source.ref,
                        "path": str(PurePosixPath(manifest_path).parent),
                        "manifest_path": manifest_path,
                        "installed": (self._skills_dir / spec.skill_id).is_dir(),
                        "cataloged": self._get_installable_spec(spec.skill_id) is not None,
                        "source_name": source.name,
                        "match_score": float(score),
                    }
                )

        results.sort(
            key=lambda item: (
                -float(item["match_score"]),
                str(item["skill_id"]),
                str(item["repo"]),
                str(item["path"]),
            )
        )
        return results[:capped_limit]

    async def search_clawhub_skills(
        self,
        query: str,
        *,
        limit: int = 10,
        base_url: str | None = None,
    ) -> list[dict[str, Any]]:
        query_text = (query or "").strip()
        if not query_text:
            return []
        capped_limit = max(1, min(int(limit or 10), 50))
        try:
            payload = await self._clawhub.search_skills(
                query_text,
                limit=capped_limit,
                base_url=base_url,
            )
        except Exception as exc:
            self._audit.record(
                action="skill_remote_search_failed",
                target="clawhub",
                actor="system",
                details={
                    "phase": "clawhub_search",
                    "query": query_text,
                    "limit": capped_limit,
                    "base_url": self._clawhub.resolve_base_url(base_url),
                },
                success=False,
                error=str(exc),
            )
            raise

        results: list[dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            slug = str(item.get("slug") or "").strip()
            if not slug:
                continue
            results.append(
                {
                    "skill_id": slug,
                    "slug": slug,
                    "name": str(item.get("displayName") or slug).strip(),
                    "description": str(item.get("summary") or "").strip(),
                    "version": str(item.get("version") or "").strip(),
                    "updated_at": item.get("updatedAt"),
                    "source_name": "clawhub",
                    "source_type": "clawhub",
                    "registry": self._clawhub.resolve_base_url(base_url),
                    "installed": (self._skills_dir / slug).is_dir(),
                    "cataloged": self._get_installable_spec(slug) is not None,
                    "match_score": float(item.get("score") or 0.0),
                }
            )
        results.sort(key=lambda item: (-float(item["match_score"]), str(item["slug"])))
        return results[:capped_limit]

    def get_installable_skill(self, skill_id: str) -> dict[str, Any] | None:
        spec = self._get_installable_spec(skill_id)
        if spec is None:
            return None
        return {
            "skill_id": spec.skill_id,
            "name": spec.name,
            "description": spec.description,
            "tags": list(spec.tags),
            "keywords": list(spec.keywords),
            "packages": list(spec.packages),
            "install_commands": list(spec.install_commands),
            "verify_imports": list(spec.verify_imports),
            "verify_commands": list(spec.verify_commands),
            "unsupported_install_specs": list(spec.unsupported_install_specs),
            "installed": (self._skills_dir / spec.skill_id).is_dir(),
            "manifest_path": str(spec.manifest_path),
        }

    def _iter_installable_specs(self) -> list[InstallableSkillSpec]:
        if self._catalog_dir is None or not self._catalog_dir.exists():
            return []
        specs: list[InstallableSkillSpec] = []
        for skill_md_path in sorted(self._catalog_dir.glob("*/SKILL.md")):
            try:
                specs.append(self._load_installable_manifest(skill_md_path))
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("failed to load installable skill %s: %s", skill_md_path, exc)
        return specs

    def _get_installable_spec(self, skill_id: str) -> InstallableSkillSpec | None:
        if self._catalog_dir is None:
            return None
        skill_md_path = self._catalog_dir / skill_id / "SKILL.md"
        if not skill_md_path.exists():
            return None
        return self._load_installable_manifest(skill_md_path)

    def _load_installable_manifest(self, skill_md_path: Path) -> InstallableSkillSpec:
        text = skill_md_path.read_text(encoding="utf-8")
        return self._load_installable_manifest_text(text, manifest_path=skill_md_path)

    def _load_installable_manifest_text(
        self,
        text: str,
        *,
        manifest_path: Path,
    ) -> InstallableSkillSpec:
        """从 SKILL.md frontmatter 加载可安装技能元数据。

        支持两种格式：
        1. OpenClaw 格式 — metadata.openclaw 块（可直接从 OpenClaw 导入）
        2. Nexus 原生格式 — 顶层 packages/verify_imports 等字段
        """
        data, _ = self._parse_frontmatter(text)
        skill_id = str(data.get("id") or manifest_path.parent.name).strip()

        def _to_str_tuple(val: Any) -> tuple[str, ...]:
            if isinstance(val, list):
                return tuple(str(item).strip() for item in val if str(item).strip())
            if isinstance(val, str) and val.strip():
                return tuple(item.strip() for item in val.split(",") if item.strip())
            return ()

        # 检测 OpenClaw 格式：metadata.openclaw 块
        metadata_raw = data.get("metadata")
        openclaw: dict[str, Any] | None = None
        if isinstance(metadata_raw, dict):
            openclaw = metadata_raw.get("openclaw")
        elif isinstance(metadata_raw, str):
            # metadata 可能是 JSON 字符串（OpenClaw 的某些变体）
            try:
                import json
                parsed = json.loads(metadata_raw)
                if isinstance(parsed, dict):
                    openclaw = parsed.get("openclaw")
            except (json.JSONDecodeError, TypeError):
                pass

        if isinstance(openclaw, dict):
            # --- OpenClaw 格式 ---
            oc_tags = _to_str_tuple(openclaw.get("tags"))
            oc_requires = openclaw.get("requires") or {}
            oc_install = openclaw.get("install") or []

            # 从 install 列表提取 packages 和 install_commands
            packages: list[str] = []
            install_commands: list[str] = []
            verify_bins: list[str] = []
            unsupported_install_specs: list[str] = []

            # 提取 eligibility 字段
            requires_bins: tuple[str, ...] = ()
            requires_env: tuple[str, ...] = ()
            requires_os: tuple[str, ...] = ()

            if isinstance(oc_requires, dict):
                verify_bins.extend(_to_str_tuple(oc_requires.get("bins")))
                requires_bins = _to_str_tuple(oc_requires.get("bins"))
                requires_env = _to_str_tuple(oc_requires.get("env"))
                requires_os = _to_str_tuple(oc_requires.get("os"))

            for spec in (oc_install if isinstance(oc_install, list) else []):
                if not isinstance(spec, dict):
                    unsupported_install_specs.append("OpenClaw install spec must be an object")
                    continue
                kind = str(spec.get("kind") or spec.get("type") or "").strip().lower()
                if kind == "uv" and spec.get("package"):
                    packages.append(str(spec["package"]).strip())
                elif kind == "uv":
                    unsupported_install_specs.append("OpenClaw uv install spec requires package")
                elif kind == "brew" and spec.get("formula"):
                    install_commands.append(f"brew install {spec['formula']}")
                elif kind == "brew":
                    unsupported_install_specs.append("OpenClaw brew install spec requires formula")
                elif kind == "node" and spec.get("package"):
                    install_commands.append(f"npm install -g {spec['package']}")
                elif kind == "node":
                    unsupported_install_specs.append("OpenClaw node install spec requires package")
                elif kind == "go" and spec.get("module"):
                    install_commands.append(f"go install {spec['module']}")
                elif kind == "go":
                    unsupported_install_specs.append("OpenClaw go install spec requires module")
                else:
                    unsupported_install_specs.append(f"Unsupported OpenClaw install kind: {kind or '<missing>'}")

            # verify_commands：用 which 检测 bins
            verify_commands = [f"which {b}" for b in verify_bins]

            # 合并顶层 tags 和 openclaw tags 作为 keywords
            top_tags = _to_str_tuple(data.get("tags"))
            all_tags = oc_tags or top_tags

            return InstallableSkillSpec(
                skill_id=skill_id,
                name=str(data.get("name") or skill_id).strip(),
                description=str(data.get("description") or "").strip(),
                tags=all_tags,
                keywords=tuple(s.lower() for s in all_tags),
                packages=tuple(packages),
                install_commands=tuple(install_commands),
                verify_imports=(),
                verify_commands=tuple(verify_commands),
                manifest_path=manifest_path,
                skill_dir=manifest_path.parent,
                requires_bins=requires_bins,
                requires_env=requires_env,
                requires_os=requires_os,
                unsupported_install_specs=tuple(unsupported_install_specs),
            )

        # --- Nexus 原生格式 ---
        tags = _to_str_tuple(data.get("tags"))
        keywords = tuple(s.lower() for s in _to_str_tuple(data.get("keywords")))
        packages_t = _to_str_tuple(data.get("packages"))
        install_cmds = _to_str_tuple(data.get("install_commands"))
        verify_imports = _to_str_tuple(data.get("verify_imports"))
        verify_cmds = _to_str_tuple(data.get("verify_commands"))
        return InstallableSkillSpec(
            skill_id=skill_id,
            name=str(data.get("name") or skill_id).strip(),
            description=str(data.get("description") or "").strip(),
            tags=tags,
            keywords=keywords or tuple(s.lower() for s in tags),
            packages=packages_t,
            install_commands=install_cmds,
            verify_imports=verify_imports,
            verify_commands=verify_cmds,
            manifest_path=manifest_path,
            skill_dir=manifest_path.parent,
            unsupported_install_specs=(),
        )

    def _score_installable(self, spec: InstallableSkillSpec, query: str) -> float:
        if not query:
            return 0.0
        q = query.strip().lower()
        compact_q = re.sub(r"\s+", "", q)
        normalized_q = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", q)
        combined = " ".join(
            [spec.skill_id, spec.name, spec.description, *spec.tags, *spec.keywords]
        ).lower()
        compact_combined = re.sub(r"\s+", "", combined)
        normalized_combined = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", combined)
        score = 0.0
        if compact_q and compact_q in compact_combined:
            score += 8.0
        if normalized_q and normalized_q in normalized_combined:
            score += 8.0
        for keyword in spec.keywords:
            if keyword and keyword in compact_q:
                score += 4.0
        tokens = [item for item in re.findall(r"[a-z0-9_-]+|[\u4e00-\u9fff]+", compact_q) if len(item) > 0]
        for token in tokens:
            if token in compact_combined:
                score += 1.5
            for subtoken in re.findall(r"[a-z]+|\d+|[\u4e00-\u9fff]+", token):
                if len(subtoken) <= 1:
                    continue
                if subtoken in normalized_combined:
                    score += 1.0
        return score

    def _verify_installable_bundle(self, skill_dir: Path, spec: InstallableSkillSpec) -> VerifyResult:
        checks: list[CheckResult] = []
        checks.append(
            CheckResult(
                name="skill_id",
                passed=bool(_SKILL_ID_RE.match(spec.skill_id)),
                message="" if bool(_SKILL_ID_RE.match(spec.skill_id)) else "Skill id must be kebab-case and 3-64 chars long",
            )
        )
        skill_md = skill_dir / "SKILL.md"
        checks.append(
            CheckResult(
                name="skill_markdown",
                passed=skill_md.exists(),
                message="" if skill_md.exists() else "Installable skill bundle must contain SKILL.md",
            )
        )
        # skill.yaml 已废弃，元数据统一在 SKILL.md frontmatter 中
        if skill_md.exists():
            meta, body = self._parse_frontmatter(skill_md.read_text(encoding="utf-8"))
            checks.append(
                CheckResult(
                    name="frontmatter_name",
                    passed=bool(meta.get("name", "").strip()),
                    message="" if meta.get("name", "").strip() else "SKILL.md frontmatter must include name",
                )
            )
            checks.append(
                CheckResult(
                    name="frontmatter_description",
                    passed=bool(meta.get("description", "").strip()),
                    message="" if meta.get("description", "").strip() else "SKILL.md frontmatter must include description",
                )
            )
            checks.append(
                CheckResult(
                    name="skill_body",
                    passed=bool(body.strip()),
                    message="" if body.strip() else "SKILL.md body cannot be empty",
                )
            )
        checks.append(
            CheckResult(
                name="packages",
                passed=all(_PACKAGE_RE.match(item) for item in spec.packages),
                message="" if all(_PACKAGE_RE.match(item) for item in spec.packages) else "packages contain invalid specifiers",
            )
        )
        checks.append(
            CheckResult(
                name="verify_imports",
                passed=all(_IMPORT_RE.match(item) for item in spec.verify_imports),
                message="" if all(_IMPORT_RE.match(item) for item in spec.verify_imports) else "verify_imports contain invalid module names",
            )
        )
        checks.append(
            CheckResult(
                name="install_commands",
                passed=not self._collect_install_command_errors(spec.install_commands),
                message="; ".join(self._collect_install_command_errors(spec.install_commands)),
            )
        )
        checks.append(
            CheckResult(
                name="openclaw_install_specs",
                passed=not spec.unsupported_install_specs,
                message="; ".join(spec.unsupported_install_specs),
            )
        )
        return VerifyResult(passed=all(item.passed for item in checks), checks=checks)

    async def _install_bundle_dependencies(self, spec: InstallableSkillSpec, *, actor: str) -> dict[str, Any]:
        if spec.packages:
            # 优先使用 uv（对标 OpenClaw），回退到 pip
            if shutil.which("uv"):
                cmd = ["uv", "pip", "install", *spec.packages]
                installer = "uv"
            else:
                cmd = [self._python, "-m", "pip", "install", *spec.packages]
                installer = "pip"

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                reason = (stderr.decode("utf-8", errors="ignore") or stdout.decode("utf-8", errors="ignore")).strip()
                self._audit.record(
                    action="skill_install_failed",
                    target=spec.skill_id,
                    actor=actor,
                    details={"phase": f"{installer}_install", "packages": list(spec.packages), "returncode": proc.returncode},
                    success=False,
                    error=reason,
                )
                return {"success": False, "reason": reason or f"{installer} install failed ({proc.returncode})"}

        if spec.install_commands:
            for command in spec.install_commands:
                try:
                    argv = self._parse_install_command(command)
                except ValueError as exc:
                    reason = str(exc)
                    self._audit.record(
                        action="skill_install_failed",
                        target=spec.skill_id,
                        actor=actor,
                        details={"phase": "install_commands", "command": command},
                        success=False,
                        error=reason,
                    )
                    return {"success": False, "reason": reason}

                if self._system_runner is not None and hasattr(self._system_runner, "run_argv"):
                    result = await self._system_runner.run_argv(
                        argv,
                        workdir=str(spec.skill_dir),
                        timeout=0,
                        actor=actor,
                    )
                else:
                    exit_code, stdout, stderr = await self._run_process(argv, cwd=spec.skill_dir)
                    result = {
                        "exit_code": exit_code,
                        "stdout": stdout,
                        "stderr": stderr,
                        "timed_out": False,
                    }
                if result.get("exit_code") != 0 or result.get("timed_out"):
                    reason = (result.get("stderr") or result.get("stdout") or "install command failed").strip()
                    self._audit.record(
                        action="skill_install_failed",
                        target=spec.skill_id,
                        actor=actor,
                        details={"phase": "install_commands", "command": command},
                        success=False,
                        error=reason,
                    )
                    return {"success": False, "reason": reason}

        return {"success": True, "reason": "ok"}

    async def _verify_runtime_requirements(self, spec: InstallableSkillSpec) -> VerifyResult:
        checks: list[CheckResult] = []
        for module_name in spec.verify_imports:
            checks.append(
                CheckResult(
                    name=f"import:{module_name}",
                    passed=importlib.util.find_spec(module_name) is not None,
                    message="" if importlib.util.find_spec(module_name) is not None else f"Module '{module_name}' is unavailable after install",
                )
            )
        if spec.verify_commands:
            if self._system_runner is None:
                checks.append(
                    CheckResult(
                        name="verify_commands",
                        passed=False,
                        message="system runner unavailable for verify_commands",
                    )
                )
            else:
                for command in spec.verify_commands:
                    result = await self._system_runner.run(
                        command,
                        workdir=str(spec.skill_dir),
                        timeout=120,
                        actor="agent",
                    )
                    ok = result.get("exit_code") == 0 and not result.get("timed_out")
                    checks.append(
                        CheckResult(
                            name=f"verify:{command[:60]}",
                            passed=ok,
                            message="" if ok else (result.get("stderr") or result.get("stdout") or "verify command failed").strip(),
                        )
                    )
        if not checks:
            checks.append(CheckResult(name="runtime_verify", passed=True, message="No runtime verification required"))
        return VerifyResult(passed=all(item.passed for item in checks), checks=checks)

    # ------------------------------------------------------------------
    # Generic install/uninstall of local skill bundles
    # ------------------------------------------------------------------

    async def install(
        self,
        source_path: Path,
        skill_id: str | None = None,
    ) -> ChangeResult:
        if skill_id is None:
            skill_id = source_path.name

        logger.info("Installing skill: %s from %s", skill_id, source_path)
        staging_path = self._sandbox.staging_dir / skill_id
        shutil.rmtree(staging_path, ignore_errors=True)

        try:
            shutil.copytree(source_path, staging_path)
        except Exception as exc:
            self._audit.record(
                action="skill_install_failed",
                target=skill_id,
                details={"error": str(exc), "phase": "staging"},
                success=False,
            )
            return ChangeResult(success=False, reason=f"Staging failed: {exc}")

        verify_result = await self._verify_staged_skill(staging_path)
        if not verify_result.passed:
            shutil.rmtree(staging_path, ignore_errors=True)
            self._audit.record(
                action="skill_install_blocked",
                target=skill_id,
                details={
                    "reason": verify_result.summary,
                    "checks": [
                        {"name": c.name, "passed": c.passed, "message": c.message}
                        for c in verify_result.checks
                    ],
                },
                success=False,
            )
            return ChangeResult(success=False, reason=f"Verification failed: {verify_result.summary}")

        install_path = self._skills_dir / skill_id
        if install_path.exists():
            backup_id = str(uuid.uuid4())[:8]
            backup_path = self._skills_dir / f"{skill_id}.backup.{backup_id}"
            install_path.rename(backup_path)

        shutil.move(str(staging_path), str(install_path))
        self._write_skill_origin_and_lock(
            skill_id=skill_id,
            skill_dir=install_path,
            actor="system",
            source_metadata={
                "source_type": "local_bundle",
                "source_label": str(source_path),
                "source_path": str(source_path),
                "skill_id": skill_id,
                "installed_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        self._audit.record(
            action="skill_installed",
            target=skill_id,
            details={
                "path": str(install_path),
                "origin_path": str(self._skill_origin_path(install_path)),
            },
        )
        return ChangeResult(success=True)

    async def uninstall(self, skill_id: str) -> ChangeResult:
        skill_path = self._skills_dir / skill_id
        if not skill_path.exists():
            return ChangeResult(success=False, reason=f"Skill not found: {skill_id}")

        try:
            backup_id = str(uuid.uuid4())[:8]
            backup_path = self._skills_dir / f"{skill_id}.uninstalled.{backup_id}"
            skill_path.rename(backup_path)
            self._remove_skill_from_lock(skill_id)
            self._audit.record(
                action="skill_uninstalled",
                target=skill_id,
                details={"backup": str(backup_path)},
            )
            return ChangeResult(success=True)
        except Exception as exc:
            self._audit.record(
                action="skill_uninstall_failed",
                target=skill_id,
                details={"error": str(exc)},
                success=False,
            )
            return ChangeResult(success=False, reason=str(exc))

    def list_skills(self) -> list[dict[str, Any]]:
        skills: list[dict[str, Any]] = []
        for path in self._skills_dir.iterdir():
            if not path.is_dir() or path.name.startswith("."):
                continue
            if ".backup." in path.name or ".uninstalled." in path.name:
                continue
            meta = self._read_skill_metadata(path)
            skills.append(
                {
                    "skill_id": path.name,
                    "path": str(path),
                    "has_tests": (path / "tests").is_dir(),
                    "name": meta.get("name", path.name),
                    "description": meta.get("description", ""),
                    "tags": meta.get("tags", ""),
                    "install_source": meta.get("install_source", ""),
                    **self._describe_skill_origin(path),
                }
            )
        return skills

    def get_skill_path(self, skill_id: str) -> Path | None:
        path = self._skills_dir / skill_id
        return path if path.exists() else None

    # ------------------------------------------------------------------
    # Self-evolution: create/update instruction skills
    # ------------------------------------------------------------------

    def create_skill(
        self,
        skill_id: str,
        name: str,
        description: str,
        body: str,
        tags: str = "",
        *,
        keywords: list[str] | None = None,
        packages: list[str] | None = None,
        verify_imports: list[str] | None = None,
        verify_commands: list[str] | None = None,
        install_commands: list[str] | None = None,
    ) -> ChangeResult:
        if not _SKILL_ID_RE.match(skill_id):
            return ChangeResult(
                success=False,
                reason=(
                    f"无效 skill_id '{skill_id}'。"
                    "要求: 3-64字符，小写字母/数字/连字符，不以连字符开头或结尾。"
                ),
            )

        skill_dir = self._skills_dir / skill_id
        if skill_dir.exists():
            return ChangeResult(success=False, reason=f"Skill '{skill_id}' 已存在。如需更新请使用 update_skill。")

        install_command_errors = self._collect_install_command_errors(tuple(install_commands or ()))
        if install_command_errors:
            return ChangeResult(success=False, reason="; ".join(install_command_errors))

        fm: dict[str, Any] = {"name": name, "description": description}
        if tags:
            tag_list = [t.strip() for t in tags.split(",") if t.strip()]
            fm["tags"] = tag_list if len(tag_list) > 1 else tags
        if keywords:
            fm["keywords"] = keywords
        if packages:
            fm["packages"] = packages
        if verify_imports:
            fm["verify_imports"] = verify_imports
        if verify_commands:
            fm["verify_commands"] = verify_commands
        if install_commands:
            fm["install_commands"] = install_commands
        frontmatter = "---\n" + yaml.dump(fm, allow_unicode=True, default_flow_style=False, sort_keys=False).rstrip() + "\n---\n"
        skill_md_content = frontmatter + "\n" + body.strip() + "\n"

        try:
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(skill_md_content, encoding="utf-8")
            self._write_skill_origin_and_lock(
                skill_id=skill_id,
                skill_dir=skill_dir,
                actor="agent",
                source_metadata={
                    "source_type": "generated",
                    "source_label": "agent:create_skill",
                    "skill_id": skill_id,
                    "installed_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception as exc:
            if skill_dir.exists():
                shutil.rmtree(skill_dir, ignore_errors=True)
            self._audit.record(
                action="skill_create_failed",
                target=skill_id,
                actor="agent",
                details={"error": str(exc)},
                success=False,
            )
            return ChangeResult(success=False, reason=f"写入失败: {exc}")

        self._audit.record(
            action="skill_created",
            target=skill_id,
            actor="agent",
            details={
                "name": name,
                "description": description,
                "tags": tags,
                "body_length": len(body),
            },
        )
        return ChangeResult(success=True, reason=f"Skill '{skill_id}' 已创建。")

    def update_skill(
        self,
        skill_id: str,
        body: str | None = None,
        description: str | None = None,
        tags: str | None = None,
    ) -> ChangeResult:
        skill_dir = self._skills_dir / skill_id
        skill_md = skill_dir / "SKILL.md"
        if not skill_dir.exists():
            return ChangeResult(success=False, reason=f"Skill '{skill_id}' 不存在。")
        if not skill_md.exists():
            return ChangeResult(success=False, reason=f"Skill '{skill_id}' 没有 SKILL.md，无法更新。")

        old_text = skill_md.read_text(encoding="utf-8")
        old_meta, old_body = self._parse_frontmatter(old_text)
        new_body = body if body is not None else old_body

        # 保留旧 frontmatter 中的所有字段，仅覆盖指定字段
        fm: dict[str, Any] = dict(old_meta)
        if description is not None:
            fm["description"] = description
        if tags is not None:
            tag_list = [t.strip() for t in tags.split(",") if t.strip()]
            fm["tags"] = tag_list if len(tag_list) > 1 else tags
        if not fm.get("name"):
            fm["name"] = skill_id

        backup_id = str(uuid.uuid4())[:8]
        backup_path = skill_dir / f"SKILL.md.bak.{backup_id}"
        backup_path.write_text(old_text, encoding="utf-8")

        frontmatter = "---\n" + yaml.dump(fm, allow_unicode=True, default_flow_style=False, sort_keys=False).rstrip() + "\n---\n"
        skill_md.write_text(frontmatter + "\n" + new_body.strip() + "\n", encoding="utf-8")
        self._refresh_skill_origin_and_lock(skill_id=skill_id, skill_dir=skill_dir, actor="agent")

        self._audit.record(
            action="skill_updated",
            target=skill_id,
            actor="agent",
            details={
                "backup_id": backup_id,
                "updated_fields": [
                    name
                    for name, value in [("body", body), ("description", description), ("tags", tags)]
                    if value is not None
                ],
            },
        )
        return ChangeResult(success=True, reason=f"Skill '{skill_id}' 已更新。", backup_id=backup_id)

    # ------------------------------------------------------------------
    # Two-layer skill injection
    # ------------------------------------------------------------------

    def get_skill_descriptions(self) -> str:
        skills = self.list_skills()
        if not skills:
            return ""

        lines: list[str] = []
        for skill_info in skills:
            name = skill_info.get("name", skill_info["skill_id"])
            desc = skill_info.get("description", "无描述")
            tags = skill_info.get("tags", "")
            line = f"  - {name}: {desc}"
            if tags:
                line += f" [{tags}]"
            lines.append(line)
        return "\n".join(lines)

    def get_skill_content(self, skill_id: str) -> str:
        skill_path = self._skills_dir / skill_id
        if not skill_path.exists():
            available = [s["skill_id"] for s in self.list_skills()]
            return f"Error: 未找到 skill '{skill_id}'。可用 skills: {', '.join(available) if available else '无'}"

        skill_md = skill_path / "SKILL.md"
        if not skill_md.exists():
            skill_yaml = skill_path / "skill.yaml"
            if skill_yaml.exists():
                return (
                    f"<skill name=\"{skill_id}\">\n"
                    f"<location>{skill_yaml}</location>\n"
                    f"<root>{skill_path}</root>\n"
                    f"{skill_yaml.read_text(encoding='utf-8')}\n"
                    "</skill>"
                )
            skill_json = skill_path / "skill.json"
            if skill_json.exists():
                return (
                    f"<skill name=\"{skill_id}\">\n"
                    f"<location>{skill_json}</location>\n"
                    f"<root>{skill_path}</root>\n"
                    f"{skill_json.read_text(encoding='utf-8')}\n"
                    "</skill>"
                )
            return f"Error: skill '{skill_id}' 没有 SKILL.md 或 skill spec"

        text = skill_md.read_text(encoding="utf-8")
        _, body = self._parse_frontmatter(text)
        return (
            f"<skill name=\"{skill_id}\">\n"
            f"<location>{skill_md}</location>\n"
            f"<root>{skill_path}</root>\n"
            f"{body}\n"
            "</skill>"
        )

    # ------------------------------------------------------------------
    # OpenClaw eligibility (Step 5)
    # ------------------------------------------------------------------

    @staticmethod
    def check_eligibility(spec: InstallableSkillSpec) -> tuple[bool, list[str]]:
        """检查 skill 是否满足当前环境的运行条件。

        返回 (eligible, reasons)。eligible=False 时 reasons 列出不满足的条件。
        对标 OpenClaw：检查 requires.bins / requires.env / requires.os。
        """
        reasons: list[str] = []

        # requires.os — 检查操作系统
        if spec.requires_os:
            current_os = platform.system().lower()  # "darwin", "linux", "windows"
            os_aliases = {
                "macos": "darwin", "mac": "darwin", "osx": "darwin",
                "linux": "linux", "windows": "windows", "win": "windows",
            }
            normalized = {os_aliases.get(o.lower(), o.lower()) for o in spec.requires_os}
            if current_os not in normalized:
                reasons.append(f"需要 OS: {', '.join(spec.requires_os)}，当前: {platform.system()}")

        # requires.bins — 检查可执行文件
        for bin_name in spec.requires_bins:
            if shutil.which(bin_name) is None:
                reasons.append(f"缺少命令: {bin_name}")

        # requires.env — 检查环境变量
        for env_name in spec.requires_env:
            if not os.environ.get(env_name):
                reasons.append(f"缺少环境变量: {env_name}")

        return (len(reasons) == 0, reasons)

    def check_installed_skill_eligibility(self, skill_id: str) -> tuple[bool, list[str]]:
        """检查已安装 skill 的 eligibility。解析 SKILL.md frontmatter 中的 requires。"""
        skill_md = self._skills_dir / skill_id / "SKILL.md"
        if not skill_md.exists():
            return (True, [])  # 没有 SKILL.md 的 skill 默认 eligible
        text = skill_md.read_text(encoding="utf-8")
        meta, _ = self._parse_frontmatter(text)

        def _to_str_tuple(val: Any) -> tuple[str, ...]:
            if isinstance(val, list):
                return tuple(str(item).strip() for item in val if str(item).strip())
            if isinstance(val, str) and val.strip():
                return tuple(item.strip() for item in val.split(",") if item.strip())
            return ()

        # 从 metadata.openclaw.requires 或顶层 requires 提取
        requires: dict[str, Any] = {}
        metadata_raw = meta.get("metadata")
        if isinstance(metadata_raw, dict):
            openclaw = metadata_raw.get("openclaw")
            if isinstance(openclaw, dict):
                requires = openclaw.get("requires") or {}
        if not requires and isinstance(meta.get("requires"), dict):
            requires = meta["requires"]

        if not requires:
            return (True, [])

        dummy_spec = InstallableSkillSpec(
            skill_id=skill_id, name="", description="", tags=(), keywords=(),
            packages=(), install_commands=(), verify_imports=(), verify_commands=(),
            manifest_path=skill_md, skill_dir=skill_md.parent,
            requires_bins=_to_str_tuple(requires.get("bins")),
            requires_env=_to_str_tuple(requires.get("env")),
            requires_os=_to_str_tuple(requires.get("os")),
        )
        return self.check_eligibility(dummy_spec)

    # ------------------------------------------------------------------
    # OpenClaw prompt injection (Step 7)
    # ------------------------------------------------------------------

    # 对标 OpenClaw：所有 eligible skill 注入 system prompt，LLM 自行决定使用
    MAX_SKILLS_IN_PROMPT = 150
    MAX_PROMPT_CHARS = 30_000
    MAX_SKILL_FILE_SIZE = 256 * 1024  # 256KB

    def format_skills_for_prompt(self) -> str:
        """将所有 eligible 的已安装 skill 内容注入到 system prompt 中。

        对标 OpenClaw：
        - 所有 eligible skills 全部注入，LLM 决定使用哪些
        - 上限 150 个 skill 或 30,000 字符
        - 单个 SKILL.md > 256KB 的跳过
        """
        skills = self.list_skills()
        if not skills:
            return ""

        sections: list[str] = []
        total_chars = 0
        skill_count = 0

        for skill_info in skills:
            if skill_count >= self.MAX_SKILLS_IN_PROMPT:
                break

            skill_id = skill_info["skill_id"]

            # eligibility 检查
            eligible, reasons = self.check_installed_skill_eligibility(skill_id)
            if not eligible:
                logger.debug("Skill %s 不符合 eligibility: %s", skill_id, reasons)
                continue

            # 读取 SKILL.md 内容
            skill_md = self._skills_dir / skill_id / "SKILL.md"
            if not skill_md.exists():
                continue

            try:
                file_size = skill_md.stat().st_size
                if file_size > self.MAX_SKILL_FILE_SIZE:
                    logger.debug("Skill %s 文件过大 (%d bytes)，跳过", skill_id, file_size)
                    continue
                text = skill_md.read_text(encoding="utf-8")
            except Exception:
                logger.warning("读取 skill %s 失败", skill_id, exc_info=True)
                continue

            _, body = self._parse_frontmatter(text)
            if not body.strip():
                continue

            section = f"<skill name=\"{skill_id}\">\n{body.strip()}\n</skill>"

            if total_chars + len(section) > self.MAX_PROMPT_CHARS:
                break

            sections.append(section)
            total_chars += len(section)
            skill_count += 1

        if not sections:
            return ""

        header = (
            "# 已安装的 Skills\n\n"
            "以下是你可以使用的技能指令。根据用户的任务，选择合适的 skill 来指导你的执行。\n"
            "你不需要全部使用——根据任务需求选择最相关的。\n\n"
        )
        return header + "\n\n".join(sections)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _verify_staged_skill(self, skill_path: Path) -> VerifyResult:
        if (skill_path / "SKILL.md").exists():
            return self._verify_instruction_bundle(skill_path)
        return await self._sandbox.verify_skill(skill_path)

    def _verify_instruction_bundle(self, skill_path: Path) -> VerifyResult:
        checks: list[CheckResult] = []
        skill_md = skill_path / "SKILL.md"
        checks.append(
            CheckResult(
                name="skill_markdown",
                passed=skill_md.exists(),
                message="" if skill_md.exists() else "Missing SKILL.md",
            )
        )
        if skill_md.exists():
            meta, body = self._parse_frontmatter(skill_md.read_text(encoding="utf-8"))
            checks.extend(
                [
                    CheckResult(
                        name="name",
                        passed=bool(meta.get("name", "").strip()),
                        message="" if meta.get("name", "").strip() else "SKILL.md frontmatter must include name",
                    ),
                    CheckResult(
                        name="description",
                        passed=bool(meta.get("description", "").strip()),
                        message="" if meta.get("description", "").strip() else "SKILL.md frontmatter must include description",
                    ),
                    CheckResult(
                        name="body",
                        passed=bool(body.strip()),
                        message="" if body.strip() else "SKILL.md body cannot be empty",
                    ),
                ]
            )
        return VerifyResult(passed=all(item.passed for item in checks), checks=checks)

    def _read_skill_metadata(self, skill_path: Path) -> dict[str, str]:
        skill_md = skill_path / "SKILL.md"
        if skill_md.exists():
            text = skill_md.read_text(encoding="utf-8")
            raw_meta, _ = self._parse_frontmatter(text)
            meta: dict[str, str] = {}
            for key, val in raw_meta.items():
                if isinstance(val, list):
                    meta[key] = ", ".join(str(item) for item in val if str(item).strip())
                else:
                    meta[key] = str(val) if val is not None else ""
            if not meta.get("name"):
                meta["name"] = skill_path.name
            return meta

        skill_json = skill_path / "skill.json"
        if skill_json.exists():
            import json

            try:
                data = json.loads(skill_json.read_text(encoding="utf-8"))
                return {
                    "name": data.get("name", skill_path.name),
                    "description": data.get("description", ""),
                }
            except Exception:
                pass
        return {"name": skill_path.name}

    def _skill_metadata_dir(self, skill_dir: Path) -> Path:
        return skill_dir / _SKILL_METADATA_DIR

    def _skill_origin_path(self, skill_dir: Path) -> Path:
        return self._skill_metadata_dir(skill_dir) / _SKILL_ORIGIN_FILE

    def _skill_source_path(self, skill_dir: Path) -> Path:
        return self._skill_metadata_dir(skill_dir) / _SKILL_SOURCE_FILE

    def _write_json_file(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def _read_json_file(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, TypeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write_skill_source_metadata(self, skill_dir: Path, source_metadata: dict[str, Any]) -> None:
        payload = {
            **source_metadata,
            "skill_id": str(source_metadata.get("skill_id") or skill_dir.name),
        }
        self._write_json_file(self._skill_source_path(skill_dir), payload)

    def _merge_skill_source_metadata(self, skill_dir: Path, fallback: dict[str, Any]) -> dict[str, Any]:
        payload = dict(fallback)
        payload.update(self._read_json_file(self._skill_source_path(skill_dir)))
        return payload

    def _compute_skill_fingerprint(self, skill_dir: Path) -> str:
        hasher = hashlib.sha256()
        for file_path in sorted(skill_dir.rglob("*")):
            if not file_path.is_file():
                continue
            if _SKILL_METADATA_DIR in file_path.parts:
                continue
            rel = file_path.relative_to(skill_dir).as_posix()
            hasher.update(rel.encode("utf-8"))
            hasher.update(b"\0")
            hasher.update(file_path.read_bytes())
            hasher.update(b"\0")
        return hasher.hexdigest()

    def _describe_skill_origin(self, skill_dir: Path) -> dict[str, Any]:
        origin = self._read_json_file(self._skill_origin_path(skill_dir))
        if not origin:
            return {}
        recorded_fingerprint = str(origin.get("content_fingerprint") or "").strip()
        current_fingerprint = self._compute_skill_fingerprint(skill_dir) if recorded_fingerprint else ""
        return {
            "origin_path": str(self._skill_origin_path(skill_dir)),
            "origin_source_type": str(origin.get("source_type") or ""),
            "origin_source_label": str(origin.get("source_label") or ""),
            "origin_ref": str(origin.get("ref") or ""),
            "origin_registry": str(origin.get("registry") or ""),
            "origin_slug": str(origin.get("slug") or ""),
            "origin_installed_version": str(origin.get("installed_version") or ""),
            "content_fingerprint": recorded_fingerprint,
            "drift_detected": bool(
                recorded_fingerprint
                and current_fingerprint
                and current_fingerprint != recorded_fingerprint
            ),
        }

    def _load_lock_state(self) -> dict[str, Any]:
        if not self._lock_path.exists():
            return {"version": 1, "skills": {}}
        payload = self._read_json_file(self._lock_path)
        skills = payload.get("skills")
        if not isinstance(skills, dict):
            skills = {}
        return {"version": int(payload.get("version") or 1), "skills": skills}

    def _save_lock_state(self, payload: dict[str, Any]) -> None:
        self._write_json_file(self._lock_path, payload)

    def _upsert_lock_entry(self, skill_id: str, payload: dict[str, Any]) -> None:
        state = self._load_lock_state()
        state.setdefault("skills", {})[skill_id] = payload
        self._save_lock_state(state)

    def _remove_skill_from_lock(self, skill_id: str) -> None:
        state = self._load_lock_state()
        skills = state.setdefault("skills", {})
        if skill_id in skills:
            del skills[skill_id]
            self._save_lock_state(state)

    def _build_origin_payload(
        self,
        *,
        skill_id: str,
        skill_dir: Path,
        actor: str,
        source_metadata: dict[str, Any],
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        payload = {
            **source_metadata,
            "skill_id": skill_id,
            "installed_path": str(skill_dir),
            "manifest_path": str(skill_dir / "SKILL.md"),
            "installed_at": str(source_metadata.get("installed_at") or now),
            "updated_at": now,
            "installed_by": actor,
            "content_fingerprint": self._compute_skill_fingerprint(skill_dir),
        }
        if not payload.get("source_type"):
            payload["source_type"] = "catalog"
        if not payload.get("source_label"):
            payload["source_label"] = str(source_metadata.get("catalog_manifest_path") or skill_dir)
        return payload

    def _write_skill_origin_and_lock(
        self,
        *,
        skill_id: str,
        skill_dir: Path,
        actor: str,
        source_metadata: dict[str, Any],
    ) -> dict[str, Any]:
        payload = self._build_origin_payload(
            skill_id=skill_id,
            skill_dir=skill_dir,
            actor=actor,
            source_metadata=source_metadata,
        )
        self._write_json_file(self._skill_origin_path(skill_dir), payload)
        self._upsert_lock_entry(
            skill_id,
            {
                "skill_id": skill_id,
                "source_type": payload.get("source_type", ""),
                "source_label": payload.get("source_label", ""),
                "repo": payload.get("repo", ""),
                "ref": payload.get("ref", ""),
                "remote_path": payload.get("remote_path", ""),
                "registry": payload.get("registry", ""),
                "slug": payload.get("slug", ""),
                "installed_version": payload.get("installed_version", ""),
                "publisher": payload.get("publisher", ""),
                "archive_integrity": payload.get("archive_integrity", ""),
                "installed_at": payload.get("installed_at", ""),
                "updated_at": payload.get("updated_at", ""),
                "manifest_path": payload.get("manifest_path", ""),
                "content_fingerprint": payload.get("content_fingerprint", ""),
            },
        )
        return payload

    def _refresh_skill_origin_and_lock(self, *, skill_id: str, skill_dir: Path, actor: str) -> None:
        existing_origin = self._read_json_file(self._skill_origin_path(skill_dir))
        source_payload = existing_origin or {
            "source_type": "local",
            "source_label": str(skill_dir),
            "skill_id": skill_id,
        }
        if existing_origin.get("installed_at"):
            source_payload["installed_at"] = existing_origin["installed_at"]
        self._write_skill_origin_and_lock(
            skill_id=skill_id,
            skill_dir=skill_dir,
            actor=actor,
            source_metadata=source_payload,
        )

    def _collect_install_command_errors(self, commands: tuple[str, ...]) -> list[str]:
        errors: list[str] = []
        for command in commands:
            raw = str(command or "").strip()
            if not raw:
                errors.append("install_commands contain empty items")
                continue
            try:
                self._parse_install_command(raw)
            except ValueError as exc:
                errors.append(str(exc))
        return errors

    def _parse_install_command(self, command: str) -> list[str]:
        raw = str(command or "").strip()
        if not raw:
            raise ValueError("install command cannot be empty")
        if _UNSAFE_SHELL_CHARS_RE.search(raw):
            raise ValueError(f"install command contains unsafe shell syntax: {raw}")
        try:
            argv = shlex.split(raw, posix=True)
        except ValueError as exc:
            raise ValueError(f"install command parse failed: {exc}") from exc
        if not argv:
            raise ValueError("install command cannot be empty")
        executable = Path(argv[0]).name
        if executable not in _ALLOWED_INSTALL_EXECUTABLES:
            allowed = ", ".join(sorted(_ALLOWED_INSTALL_EXECUTABLES))
            raise ValueError(f"install command executable '{executable}' is not allowed; allowed: {allowed}")
        if executable == "brew" and (len(argv) < 3 or argv[1] != "install"):
            raise ValueError("brew install_commands must use 'brew install <formula>'")
        if executable == "npm" and argv[1:3] not in (["install", "-g"], ["install", "--global"]):
            raise ValueError("npm install_commands must use 'npm install -g <package>'")
        if executable == "pnpm" and argv[1:3] not in (["add", "-g"], ["add", "--global"]):
            raise ValueError("pnpm install_commands must use 'pnpm add -g <package>'")
        if executable == "yarn" and argv[1:3] not in (["global", "add"], ["add", "-g"]):
            raise ValueError("yarn install_commands must use 'yarn global add <package>' or 'yarn add -g <package>'")
        if executable == "go" and (len(argv) < 3 or argv[1] != "install"):
            raise ValueError("go install_commands must use 'go install <module>'")
        return argv

    @staticmethod
    def _normalize_clawhub_slug(slug: str) -> str:
        normalized = str(slug or "").strip()
        if not _CLAWHUB_SLUG_RE.match(normalized):
            raise ValueError(f"Invalid ClawHub slug: {slug}")
        return normalized

    def _resolve_skill_bundle_dir(self, source_path: Path) -> Path:
        source = source_path.expanduser()
        if not source.exists():
            raise FileNotFoundError(f"Skill source not found: {source}")
        resolved = source.resolve()
        if resolved.is_file():
            if resolved.name != "SKILL.md":
                raise ValueError("Skill source must be a skill directory or a SKILL.md file")
            resolved = resolved.parent
        skill_md = resolved / "SKILL.md"
        if not skill_md.exists():
            raise FileNotFoundError(f"Missing SKILL.md in skill source: {resolved}")
        return resolved

    def _prepare_installable_spec(self, skill_dir: Path, skill_id: str | None = None) -> InstallableSkillSpec:
        skill_md = skill_dir / "SKILL.md"
        spec = self._load_installable_manifest(skill_md)
        resolved_skill_id = (skill_id or spec.skill_id or skill_dir.name).strip()
        return replace(
            spec,
            skill_id=resolved_skill_id,
            manifest_path=skill_md,
            skill_dir=skill_dir,
        )

    def _promote_catalog_bundle(self, staged_skill_dir: Path, skill_id: str) -> tuple[Path, str | None]:
        if self._catalog_dir is None:
            raise RuntimeError("Skill registry unavailable")
        catalog_path = self._catalog_dir / skill_id
        backup_id: str | None = None
        if catalog_path.exists():
            backup_id = str(uuid.uuid4())[:8]
            backup_path = self._catalog_dir / f"{skill_id}.backup.{backup_id}"
            catalog_path.rename(backup_path)
        shutil.move(str(staged_skill_dir), str(catalog_path))
        return catalog_path, backup_id

    async def _run_process(
        self,
        argv: list[str],
        *,
        cwd: Path | None = None,
    ) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd) if cwd is not None else None,
        )
        stdout, stderr = await proc.communicate()
        return (
            proc.returncode,
            stdout.decode("utf-8", errors="ignore"),
            stderr.decode("utf-8", errors="ignore"),
        )

    @classmethod
    def _coerce_remote_sources(
        cls,
        sources: list[dict[str, Any]] | None,
    ) -> tuple[RemoteSkillSource, ...]:
        normalized: list[RemoteSkillSource] = []
        for idx, item in enumerate(sources or []):
            if not isinstance(item, dict):
                continue
            repo_raw = str(item.get("repo") or "").strip()
            if not repo_raw:
                continue
            repo = cls._normalize_github_repo(repo_raw)
            ref = str(item.get("ref") or "main").strip() or "main"
            roots_raw = item.get("roots") or []
            roots = tuple(
                PurePosixPath(str(root).strip().lstrip("/")).as_posix()
                for root in roots_raw
                if str(root).strip()
            )
            normalized.append(
                RemoteSkillSource(
                    name=str(item.get("name") or repo.split("/")[-1] or f"remote-{idx}").strip(),
                    repo=repo,
                    ref=ref,
                    roots=roots,
                )
            )
        return tuple(normalized)

    def _iter_remote_sources(
        self,
        *,
        repo: str | None = None,
        ref: str | None = None,
    ) -> tuple[RemoteSkillSource, ...]:
        if repo:
            normalized_repo = self._normalize_github_repo(repo)
            override_ref = (ref or "").strip()
            for source in self._remote_sources:
                if source.repo == normalized_repo:
                    return (
                        replace(
                            source,
                            ref=override_ref or source.ref,
                        ),
                    )
            return (
                RemoteSkillSource(
                    name=normalized_repo.split("/")[-1],
                    repo=normalized_repo,
                    ref=override_ref or "main",
                    roots=(),
                ),
            )
        return self._remote_sources

    async def _search_remote_source(
        self,
        source: RemoteSkillSource,
        query: str,
        *,
        limit: int = 40,
    ) -> list[tuple[InstallableSkillSpec, str]]:
        fetch_root = self._sandbox.staging_dir / "remote_skill_search" / uuid.uuid4().hex[:8]
        clone_dir = fetch_root / "repo"
        try:
            treeish = await self._clone_remote_repo_for_search(source.repo, source.ref, clone_dir)
            manifest_paths = await self._list_remote_skill_manifest_paths(clone_dir, treeish, source.roots)
            scored_paths = [
                (self._score_remote_manifest_path(manifest_path, query), manifest_path)
                for manifest_path in manifest_paths
            ]
            shortlisted_paths = [
                manifest_path
                for score, manifest_path in sorted(
                    scored_paths,
                    key=lambda item: (-item[0], item[1]),
                )
                if score > 0
            ]
            if shortlisted_paths:
                shortlisted_paths = shortlisted_paths[: max(1, limit)]
            else:
                shortlisted_paths = manifest_paths[: max(1, min(limit, len(manifest_paths)))]
            specs: list[tuple[InstallableSkillSpec, str]] = []
            for manifest_path in shortlisted_paths:
                text = await self._read_git_file(clone_dir, treeish, manifest_path)
                spec = self._load_installable_manifest_text(
                    text,
                    manifest_path=Path(manifest_path),
                )
                specs.append((spec, manifest_path))
            return specs
        finally:
            shutil.rmtree(fetch_root, ignore_errors=True)

    @staticmethod
    def _normalize_github_repo(repo: str) -> str:
        normalized = repo.strip().removeprefix("https://github.com/").removeprefix("http://github.com/")
        normalized = normalized.strip("/").removesuffix(".git")
        if not _GITHUB_REPO_RE.match(normalized):
            raise ValueError(f"Invalid GitHub repo: {repo}")
        return normalized

    @staticmethod
    def _normalize_remote_skill_path(skill_path: str) -> str:
        raw = skill_path.strip()
        if not raw:
            raise ValueError("Skill path is required")
        normalized = PurePosixPath(raw.lstrip("/"))
        if any(part in ("", ".", "..") for part in normalized.parts):
            raise ValueError(f"Invalid remote skill path: {skill_path}")
        return normalized.as_posix()

    async def _clone_remote_repo_for_search(
        self,
        repo: str,
        ref: str,
        clone_dir: Path,
    ) -> str:
        repo_name = self._normalize_github_repo(repo)
        git_bin = shutil.which("git")
        if not git_bin:
            raise RuntimeError("git is required for remote skill search")

        clone_dir.parent.mkdir(parents=True, exist_ok=True)
        clone_code, _, clone_err = await self._run_process(
            [
                git_bin,
                "clone",
                "--depth=1",
                "--filter=blob:none",
                "--no-checkout",
                f"https://github.com/{repo_name}.git",
                str(clone_dir),
            ]
        )
        if clone_code != 0:
            raise RuntimeError(f"git clone failed: {clone_err.strip()}")
        return await self._resolve_remote_treeish(clone_dir, ref)

    async def _list_remote_skill_manifest_paths(
        self,
        clone_dir: Path,
        treeish: str,
        roots: tuple[str, ...],
    ) -> list[str]:
        git_bin = shutil.which("git")
        if not git_bin:
            raise RuntimeError("git is required for remote skill search")
        ls_code, ls_out, ls_err = await self._run_process(
            [git_bin, "-C", str(clone_dir), "ls-tree", "-r", "--name-only", treeish]
        )
        if ls_code != 0:
            raise RuntimeError(f"git ls-tree failed: {ls_err.strip()}")

        manifests: list[str] = []
        normalized_roots = tuple(root.strip("/") for root in roots if root.strip("/"))
        for raw_path in ls_out.splitlines():
            candidate = raw_path.strip()
            if not candidate.endswith("/SKILL.md") and candidate != "SKILL.md":
                continue
            if normalized_roots and not any(
                candidate == root or candidate.startswith(f"{root}/")
                for root in normalized_roots
            ):
                continue
            manifests.append(candidate)
        return sorted(manifests)

    async def _read_git_file(
        self,
        clone_dir: Path,
        treeish: str,
        repo_path: str,
    ) -> str:
        git_bin = shutil.which("git")
        if not git_bin:
            raise RuntimeError("git is required for remote skill search")
        show_code, show_out, show_err = await self._run_process(
            [git_bin, "-C", str(clone_dir), "show", f"{treeish}:{repo_path}"]
        )
        if show_code != 0:
            raise RuntimeError(f"git show failed for {repo_path}: {show_err.strip()}")
        return show_out

    async def _resolve_remote_treeish(
        self,
        clone_dir: Path,
        ref: str,
    ) -> str:
        git_bin = shutil.which("git")
        if not git_bin:
            raise RuntimeError("git is required for remote skill import")
        requested_ref = ref.strip()
        if not requested_ref:
            return "HEAD"

        rev_code, rev_out, _ = await self._run_process(
            [git_bin, "-C", str(clone_dir), "rev-parse", "--verify", f"{requested_ref}^{{commit}}"]
        )
        if rev_code == 0 and rev_out.strip():
            return requested_ref

        fetch_code, _, fetch_err = await self._run_process(
            [git_bin, "-C", str(clone_dir), "fetch", "--depth=1", "origin", requested_ref]
        )
        if fetch_code != 0:
            raise RuntimeError(f"git fetch failed for ref {requested_ref}: {fetch_err.strip()}")
        return "FETCH_HEAD"

    def _score_remote_manifest_path(self, manifest_path: str, query: str) -> float:
        path_text = str(PurePosixPath(manifest_path).parent)
        q = (query or "").strip().lower()
        if not q:
            return 0.0
        compact_q = re.sub(r"\s+", "", q)
        normalized_q = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", q)
        combined = path_text.lower()
        compact_combined = re.sub(r"\s+", "", combined)
        normalized_combined = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", combined)
        score = 0.0
        if compact_q and compact_q in compact_combined:
            score += 8.0
        if normalized_q and normalized_q in normalized_combined:
            score += 8.0
        tokens = [item for item in re.findall(r"[a-z0-9_-]+|[\u4e00-\u9fff]+", compact_q) if item]
        for token in tokens:
            if token in compact_combined:
                score += 2.0
            for subtoken in re.findall(r"[a-z]+|\d+|[\u4e00-\u9fff]+", token):
                if len(subtoken) <= 1:
                    continue
                if subtoken in normalized_combined:
                    score += 1.5
        return score

    async def _fetch_remote_skill_bundle(
        self,
        repo: str,
        skill_path: str,
        ref: str,
        fetch_root: Path,
    ) -> Path:
        repo_name = self._normalize_github_repo(repo)
        bundle_path = self._normalize_remote_skill_path(skill_path)
        git_bin = shutil.which("git")
        if not git_bin:
            raise RuntimeError("git is required for remote skill import")

        clone_dir = fetch_root / "repo"
        fetch_root.mkdir(parents=True, exist_ok=True)

        clone_code, _, clone_err = await self._run_process(
            [
                git_bin,
                "clone",
                "--depth=1",
                "--filter=blob:none",
                "--no-checkout",
                "--sparse",
                f"https://github.com/{repo_name}.git",
                str(clone_dir),
            ]
        )
        if clone_code != 0:
            raise RuntimeError(f"git clone failed: {clone_err.strip()}")
        treeish = await self._resolve_remote_treeish(clone_dir, ref)

        sparse_code, _, sparse_err = await self._run_process(
            [git_bin, "-C", str(clone_dir), "sparse-checkout", "set", "--no-cone", bundle_path]
        )
        if sparse_code != 0:
            raise RuntimeError(f"git sparse-checkout failed: {sparse_err.strip()}")

        checkout_code, _, checkout_err = await self._run_process(
            [git_bin, "-C", str(clone_dir), "checkout", treeish]
        )
        if checkout_code != 0:
            raise RuntimeError(f"git checkout failed: {checkout_err.strip()}")

        fetched = clone_dir / bundle_path
        if fetched.is_file():
            fetched = fetched.parent
        fetched = self._resolve_skill_bundle_dir(fetched)
        return fetched

    @staticmethod
    def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
        match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
        if not match:
            return {}, text

        raw = match.group(1).strip()
        try:
            meta = yaml.safe_load(raw) or {}
        except yaml.YAMLError:
            # 降级：手工解析
            meta = {}
            for line in raw.splitlines():
                if ":" in line:
                    key, val = line.split(":", 1)
                    meta[key.strip()] = val.strip()
        if not isinstance(meta, dict):
            meta = {}
        return meta, match.group(2).strip()
