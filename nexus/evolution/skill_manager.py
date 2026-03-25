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
import os
from dataclasses import dataclass, field
import importlib.util
import logging
import platform
import re
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any

import yaml

from .audit import AuditLog
from .sandbox import Sandbox
from .types import ChangeResult, CheckResult, SkillSpec, VerifyResult

logger = logging.getLogger(__name__)

_SKILL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{1,62}[a-z0-9]$")
_PACKAGE_RE = re.compile(r"^[A-Za-z0-9_.-]+([<>=!~]=?.+)?$")
_IMPORT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_\\.]*$")


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
    ):
        self._skills_dir = Path(skills_dir)
        self._sandbox = sandbox
        self._audit = audit
        self._catalog_dir = Path(catalog_dir) if catalog_dir is not None else None
        self._system_runner = system_runner
        self._python = self._resolve_python(python_executable)
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

        installed_dir = self._skills_dir / skill_id
        if installed_dir.exists():
            return {
                "success": True,
                "skill_id": skill_id,
                "reason": "Skill already installed",
                "installed": True,
                "verify_summary": "",
                "installed_path": str(installed_dir),
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

        verify_result = self._verify_installable_bundle(staging_dir, spec)
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

        install_result = await self._install_bundle_dependencies(spec, actor=actor)
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

        runtime_verify = await self._verify_runtime_requirements(spec)
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

        backup_id: str | None = None
        if installed_dir.exists():
            backup_id = str(uuid.uuid4())[:8]
            backup_dir = self._skills_dir / f"{skill_id}.backup.{backup_id}"
            installed_dir.rename(backup_dir)

        shutil.move(str(staging_dir), str(installed_dir))
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
        """从 SKILL.md frontmatter 加载可安装技能元数据。

        支持两种格式：
        1. OpenClaw 格式 — metadata.openclaw 块（可直接从 OpenClaw 导入）
        2. Nexus 原生格式 — 顶层 packages/verify_imports 等字段
        """
        text = skill_md_path.read_text(encoding="utf-8")
        data, _ = self._parse_frontmatter(text)
        skill_id = str(data.get("id") or skill_md_path.parent.name).strip()

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
                    continue
                kind = str(spec.get("kind") or spec.get("type") or "").strip().lower()
                if kind == "uv" and spec.get("package"):
                    packages.append(str(spec["package"]).strip())
                elif kind == "brew" and spec.get("formula"):
                    install_commands.append(f"brew install {spec['formula']}")
                elif kind == "node" and spec.get("package"):
                    install_commands.append(f"npm install -g {spec['package']}")
                elif kind == "go" and spec.get("module"):
                    install_commands.append(f"go install {spec['module']}")

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
                manifest_path=skill_md_path,
                skill_dir=skill_md_path.parent,
                requires_bins=requires_bins,
                requires_env=requires_env,
                requires_os=requires_os,
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
            manifest_path=skill_md_path,
            skill_dir=skill_md_path.parent,
        )

    def _score_installable(self, spec: InstallableSkillSpec, query: str) -> float:
        if not query:
            return 0.0
        q = query.strip().lower()
        compact_q = re.sub(r"\s+", "", q)
        combined = " ".join(
            [spec.skill_id, spec.name, spec.description, *spec.tags, *spec.keywords]
        ).lower()
        compact_combined = re.sub(r"\s+", "", combined)
        score = 0.0
        if compact_q and compact_q in compact_combined:
            score += 8.0
        for keyword in spec.keywords:
            if keyword and keyword in compact_q:
                score += 4.0
        tokens = [item for item in re.findall(r"[a-z0-9_-]+|[\u4e00-\u9fff]+", compact_q) if len(item) > 0]
        for token in tokens:
            if token in compact_combined:
                score += 1.5
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
                passed=all(command.strip() for command in spec.install_commands),
                message="" if all(command.strip() for command in spec.install_commands) else "install_commands contain empty items",
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
            if self._system_runner is None:
                reason = "system runner unavailable for install_commands"
                self._audit.record(
                    action="skill_install_failed",
                    target=spec.skill_id,
                    actor=actor,
                    details={"phase": "install_commands"},
                    success=False,
                    error=reason,
                )
                return {"success": False, "reason": reason}
            for command in spec.install_commands:
                result = await self._system_runner.run(
                    command,
                    workdir=str(spec.skill_dir),
                    timeout=0,
                    actor=actor,
                )
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
        self._audit.record(
            action="skill_installed",
            target=skill_id,
            details={"path": str(install_path)},
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
                return f"<skill name=\"{skill_id}\">\n{skill_yaml.read_text(encoding='utf-8')}\n</skill>"
            skill_json = skill_path / "skill.json"
            if skill_json.exists():
                return f"<skill name=\"{skill_id}\">\n{skill_json.read_text(encoding='utf-8')}\n</skill>"
            return f"Error: skill '{skill_id}' 没有 SKILL.md 或 skill spec"

        text = skill_md.read_text(encoding="utf-8")
        _, body = self._parse_frontmatter(text)
        return f"<skill name=\"{skill_id}\">\n{body}\n</skill>"

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
