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
from dataclasses import dataclass
import importlib.util
import logging
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
        for manifest_path in sorted(self._catalog_dir.glob("*/skill.yaml")):
            try:
                specs.append(self._load_installable_manifest(manifest_path))
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("failed to load installable skill manifest %s: %s", manifest_path, exc)
        return specs

    def _get_installable_spec(self, skill_id: str) -> InstallableSkillSpec | None:
        if self._catalog_dir is None:
            return None
        manifest_path = self._catalog_dir / skill_id / "skill.yaml"
        if not manifest_path.exists():
            return None
        return self._load_installable_manifest(manifest_path)

    def _load_installable_manifest(self, manifest_path: Path) -> InstallableSkillSpec:
        data = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        skill_id = str(data.get("id") or manifest_path.parent.name).strip()
        tags = tuple(str(item).strip() for item in data.get("tags", []) or [] if str(item).strip())
        keywords = tuple(str(item).strip().lower() for item in data.get("keywords", []) or [] if str(item).strip())
        packages = tuple(str(item).strip() for item in data.get("packages", []) or [] if str(item).strip())
        install_commands = tuple(str(item).strip() for item in data.get("install_commands", []) or [] if str(item).strip())
        verify_imports = tuple(str(item).strip() for item in data.get("verify_imports", []) or [] if str(item).strip())
        verify_commands = tuple(str(item).strip() for item in data.get("verify_commands", []) or [] if str(item).strip())
        return InstallableSkillSpec(
            skill_id=skill_id,
            name=str(data.get("name") or skill_id).strip(),
            description=str(data.get("description") or "").strip(),
            tags=tags,
            keywords=keywords,
            packages=packages,
            install_commands=install_commands,
            verify_imports=verify_imports,
            verify_commands=verify_commands,
            manifest_path=manifest_path,
            skill_dir=manifest_path.parent,
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
        checks.append(
            CheckResult(
                name="manifest",
                passed=(skill_dir / "skill.yaml").exists(),
                message="" if (skill_dir / "skill.yaml").exists() else "Installable skill bundle must contain skill.yaml",
            )
        )
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
            proc = await asyncio.create_subprocess_exec(
                self._python,
                "-m",
                "pip",
                "install",
                *spec.packages,
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
                    details={"phase": "pip_install", "packages": list(spec.packages), "returncode": proc.returncode},
                    success=False,
                    error=reason,
                )
                return {"success": False, "reason": reason or f"pip install failed ({proc.returncode})"}

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

        frontmatter = f"---\nname: {name}\ndescription: {description}\n"
        if tags:
            frontmatter += f"tags: {tags}\n"
        frontmatter += "---\n"
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
        new_name = old_meta.get("name", skill_id)
        new_desc = description if description is not None else old_meta.get("description", "")
        new_tags = tags if tags is not None else old_meta.get("tags", "")
        new_body = body if body is not None else old_body

        backup_id = str(uuid.uuid4())[:8]
        backup_path = skill_dir / f"SKILL.md.bak.{backup_id}"
        backup_path.write_text(old_text, encoding="utf-8")

        frontmatter = f"---\nname: {new_name}\ndescription: {new_desc}\n"
        if new_tags:
            frontmatter += f"tags: {new_tags}\n"
        frontmatter += "---\n"
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
            meta, _ = self._parse_frontmatter(text)
            if not meta.get("name"):
                meta["name"] = skill_path.name
            if (skill_path / "skill.yaml").exists():
                try:
                    yaml_meta = yaml.safe_load((skill_path / "skill.yaml").read_text(encoding="utf-8")) or {}
                    meta.setdefault("install_source", str(yaml_meta.get("install_source") or ""))
                except Exception:
                    pass
            return meta

        skill_yaml = skill_path / "skill.yaml"
        if skill_yaml.exists():
            data = yaml.safe_load(skill_yaml.read_text(encoding="utf-8")) or {}
            return {
                "name": str(data.get("name", skill_path.name)),
                "description": str(data.get("description", "")),
                "tags": ", ".join(str(item) for item in data.get("tags", []) or [] if str(item).strip()),
                "install_source": str(data.get("install_source") or ""),
            }

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
    def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
        match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
        if not match:
            return {}, text

        meta: dict[str, str] = {}
        for line in match.group(1).strip().splitlines():
            if ":" in line:
                key, val = line.split(":", 1)
                meta[key.strip()] = val.strip()
        return meta, match.group(2).strip()
