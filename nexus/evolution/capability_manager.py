"""Capability Manager — manifest-driven runtime capability registry."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
import importlib.util
import logging
from pathlib import Path
import re
import shutil
import sys
import uuid

import yaml

from .audit import AuditLog
from .types import ChangeResult, CheckResult, VerifyResult

logger = logging.getLogger(__name__)

_CAPABILITY_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_]{1,62}[a-z0-9]$")
_PACKAGE_RE = re.compile(r"^[A-Za-z0-9_.-]+([<>=!~]=?.+)?$")
_IMPORT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_\\.]*$")


@dataclass(frozen=True)
class CapabilitySpec:
    capability_id: str
    name: str
    description: str
    packages: tuple[str, ...]
    imports: tuple[str, ...]
    tools: tuple[str, ...]
    skill_hint: str
    manifest_path: Path


class CapabilityManager:
    """Manage formal capabilities as manifest-backed system objects."""

    def __init__(
        self,
        *,
        capabilities_dir: Path,
        audit: AuditLog,
        python_executable: str | None = None,
        staging_dir: Path | None = None,
        backups_dir: Path | None = None,
        skills_dir: Path | None = None,
    ):
        self._capabilities_dir = Path(capabilities_dir)
        self._staging_dir = Path(staging_dir or (self._capabilities_dir.parent / ".staging_capabilities"))
        self._backups_dir = Path(backups_dir or (self._capabilities_dir.parent / ".backups_capabilities"))
        self._skills_dir = Path(skills_dir) if skills_dir is not None else None
        self._audit = audit
        self._python = self._resolve_python(python_executable)

        self._capabilities_dir.mkdir(parents=True, exist_ok=True)
        self._staging_dir.mkdir(parents=True, exist_ok=True)
        self._backups_dir.mkdir(parents=True, exist_ok=True)

    def _resolve_python(self, candidate: str | None) -> str:
        if candidate:
            path = Path(candidate).expanduser()
            if path.exists():
                return str(path)
        return sys.executable

    def _manifest_path(self, capability_id: str, *, staged: bool = False) -> Path:
        base = self._staging_dir if staged else self._capabilities_dir
        return base / capability_id / "CAPABILITY.yaml"

    def _load_manifest(self, manifest_path: Path) -> CapabilitySpec:
        data = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        capability_id = str(data.get("id") or manifest_path.parent.name).strip()
        return CapabilitySpec(
            capability_id=capability_id,
            name=str(data.get("name") or capability_id).strip(),
            description=str(data.get("description") or "").strip(),
            packages=tuple(str(item).strip() for item in data.get("packages", []) or [] if str(item).strip()),
            imports=tuple(str(item).strip() for item in data.get("imports", []) or [] if str(item).strip()),
            tools=tuple(str(item).strip() for item in data.get("tools", []) or [] if str(item).strip()),
            skill_hint=str(data.get("skill_hint") or "").strip(),
            manifest_path=manifest_path,
        )

    def _iter_specs(self, *, staged: bool = False) -> list[CapabilitySpec]:
        base = self._staging_dir if staged else self._capabilities_dir
        if not base.exists():
            return []
        specs: list[CapabilitySpec] = []
        for manifest_path in sorted(base.glob("*/CAPABILITY.yaml")):
            try:
                specs.append(self._load_manifest(manifest_path))
            except Exception as exc:  # pragma: no cover - defensive; surfaced via status/verify
                logger.warning("failed to load capability manifest %s: %s", manifest_path, exc)
        return specs

    def _get_spec(self, capability_id: str, *, staged: bool = False) -> CapabilitySpec | None:
        manifest_path = self._manifest_path(capability_id, staged=staged)
        if not manifest_path.exists():
            return None
        return self._load_manifest(manifest_path)

    def _is_enabled(self, spec: CapabilitySpec) -> bool:
        return all(importlib.util.find_spec(module_name) is not None for module_name in spec.imports)

    def list_capabilities(self) -> list[dict[str, object]]:
        items: list[dict[str, object]] = []
        for spec in self._iter_specs():
            items.append(
                {
                    "capability_id": spec.capability_id,
                    "name": spec.name,
                    "description": spec.description,
                    "enabled": self._is_enabled(spec),
                    "packages": list(spec.packages),
                    "imports": list(spec.imports),
                    "tools": list(spec.tools),
                    "skill_hint": spec.skill_hint,
                    "manifest_path": str(spec.manifest_path),
                    "staged": self._manifest_path(spec.capability_id, staged=True).exists(),
                }
            )
        return items

    def get_status(self, capability_id: str) -> dict[str, object]:
        spec = self._get_spec(capability_id)
        staged_spec = self._get_spec(capability_id, staged=True)
        if spec is None and staged_spec is None:
            return {
                "capability_id": capability_id,
                "known": False,
                "enabled": False,
                "staged": False,
                "manifest_path": "",
                "staged_manifest_path": "",
                "packages": [],
                "imports": [],
                "tools": [],
                "skill_hint": "",
            }

        active_spec = spec or staged_spec
        assert active_spec is not None
        return {
            "capability_id": active_spec.capability_id,
            "known": True,
            "enabled": self._is_enabled(active_spec) if spec is not None else False,
            "staged": staged_spec is not None,
            "name": active_spec.name,
            "description": active_spec.description,
            "manifest_path": str(spec.manifest_path) if spec is not None else "",
            "staged_manifest_path": str(staged_spec.manifest_path) if staged_spec is not None else "",
            "packages": list(active_spec.packages),
            "imports": list(active_spec.imports),
            "tools": list(active_spec.tools),
            "skill_hint": active_spec.skill_hint,
        }

    async def enable(self, capability_id: str, *, actor: str = "system") -> ChangeResult:
        spec = self._get_spec(capability_id)
        if spec is None:
            return ChangeResult(success=False, reason=f"Unknown capability: {capability_id}")

        if self._is_enabled(spec):
            return ChangeResult(success=True, reason="Capability already enabled")

        args = [self._python, "-m", "pip", "install", *spec.packages]
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            reason = (stderr.decode("utf-8", errors="ignore") or stdout.decode("utf-8", errors="ignore")).strip()
            self._audit.record(
                action="capability_enable_failed",
                target=capability_id,
                actor=actor,
                details={"packages": list(spec.packages), "returncode": proc.returncode},
                success=False,
                error=reason,
            )
            return ChangeResult(success=False, reason=reason or f"pip install failed ({proc.returncode})")

        if not self._is_enabled(spec):
            reason = "Installed packages but capability imports are still unavailable"
            self._audit.record(
                action="capability_enable_failed",
                target=capability_id,
                actor=actor,
                details={"packages": list(spec.packages), "phase": "post_install_check"},
                success=False,
                error=reason,
            )
            return ChangeResult(success=False, reason=reason)

        self._audit.record(
            action="capability_enabled",
            target=capability_id,
            actor=actor,
            details={"packages": list(spec.packages)},
        )
        return ChangeResult(success=True, reason="Capability enabled")

    def create(
        self,
        *,
        capability_id: str,
        name: str,
        description: str,
        packages: list[str],
        imports: list[str],
        tools: list[str] | None = None,
        skill_hint: str = "",
        actor: str = "system",
    ) -> ChangeResult:
        validation_error = self._validate_spec_fields(
            capability_id=capability_id,
            name=name,
            description=description,
            packages=packages,
            imports=imports,
            tools=tools or [],
            skill_hint=skill_hint,
        )
        if validation_error:
            return ChangeResult(success=False, reason=validation_error)

        manifest_path = self._manifest_path(capability_id, staged=True)
        if manifest_path.exists():
            return ChangeResult(success=False, reason=f"Staged capability '{capability_id}' already exists")

        if self._manifest_path(capability_id).exists():
            return ChangeResult(success=False, reason=f"Capability '{capability_id}' already exists")

        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_manifest(
            manifest_path,
            capability_id=capability_id,
            name=name,
            description=description,
            packages=packages,
            imports=imports,
            tools=tools or [],
            skill_hint=skill_hint,
        )
        self._audit.record(
            action="capability_created",
            target=capability_id,
            actor=actor,
            details={"manifest_path": str(manifest_path)},
        )
        return ChangeResult(success=True, reason="Capability staged", backup_id=None)

    def register(
        self,
        *,
        capability_id: str,
        name: str,
        description: str,
        packages: list[str],
        imports: list[str],
        tools: list[str] | None = None,
        skill_hint: str = "",
        actor: str = "system",
        auto_promote: bool = True,
    ) -> dict[str, object]:
        create_result = self.create(
            capability_id=capability_id,
            name=name,
            description=description,
            packages=packages,
            imports=imports,
            tools=tools or [],
            skill_hint=skill_hint,
            actor=actor,
        )
        if not create_result.success:
            return {
                "capability_id": capability_id,
                "created": False,
                "verified": False,
                "promoted": False,
                "reason": create_result.reason,
                "verify_summary": "",
                "backup_id": create_result.backup_id,
            }

        verify_result = self.verify(capability_id, staged=True)
        promoted = False
        backup_id = create_result.backup_id
        reason = "Capability registered in staging"
        if verify_result.passed and auto_promote:
            promote_result = self.promote(capability_id, actor=actor)
            promoted = promote_result.success
            backup_id = promote_result.backup_id
            reason = promote_result.reason
        elif not verify_result.passed:
            reason = verify_result.summary

        return {
            "capability_id": capability_id,
            "created": True,
            "verified": verify_result.passed,
            "promoted": promoted,
            "reason": reason,
            "verify_summary": verify_result.summary,
            "backup_id": backup_id,
            "manifest_path": str(self._manifest_path(capability_id, staged=not promoted)),
        }

    def stage(self, capability_id: str, *, actor: str = "system") -> ChangeResult:
        source_dir = self._capabilities_dir / capability_id
        staged_dir = self._staging_dir / capability_id
        if staged_dir.exists():
            return ChangeResult(success=True, reason="Capability already staged")
        if not source_dir.exists():
            return ChangeResult(success=False, reason=f"Capability not found: {capability_id}")
        shutil.copytree(source_dir, staged_dir)
        self._audit.record(
            action="capability_staged",
            target=capability_id,
            actor=actor,
            details={"source": str(source_dir), "staged_path": str(staged_dir)},
        )
        return ChangeResult(success=True, reason="Capability staged")

    def verify(self, capability_id: str, *, staged: bool = True) -> VerifyResult:
        spec = self._get_spec(capability_id, staged=staged)
        location = "staged" if staged else "active"
        if spec is None:
            return VerifyResult(
                passed=False,
                checks=[CheckResult(name="manifest", passed=False, message=f"{location} capability not found")],
            )

        checks: list[CheckResult] = [
            CheckResult(
                name="id_format",
                passed=bool(_CAPABILITY_ID_RE.match(spec.capability_id)) and spec.capability_id == capability_id,
                message="" if bool(_CAPABILITY_ID_RE.match(spec.capability_id)) and spec.capability_id == capability_id else "Invalid capability id or manifest id mismatch",
            ),
            CheckResult(
                name="name",
                passed=bool(spec.name.strip()),
                message="" if spec.name.strip() else "Capability name is required",
            ),
            CheckResult(
                name="description",
                passed=bool(spec.description.strip()),
                message="" if spec.description.strip() else "Capability description is required",
            ),
            CheckResult(
                name="packages",
                passed=bool(spec.packages) and all(_PACKAGE_RE.match(item) for item in spec.packages),
                message="" if bool(spec.packages) and all(_PACKAGE_RE.match(item) for item in spec.packages) else "Packages must be non-empty and syntactically valid",
            ),
            CheckResult(
                name="imports",
                passed=bool(spec.imports) and all(_IMPORT_RE.match(item) for item in spec.imports),
                message="" if bool(spec.imports) and all(_IMPORT_RE.match(item) for item in spec.imports) else "Imports must be non-empty valid module paths",
            ),
            CheckResult(
                name="tools",
                passed=all(tool.strip() for tool in spec.tools),
                message="" if all(tool.strip() for tool in spec.tools) else "Tools list contains empty values",
            ),
        ]

        if self._skills_dir is not None and spec.skill_hint:
            skill_path = self._skills_dir / spec.skill_hint
            checks.append(
                CheckResult(
                    name="skill_hint",
                    passed=skill_path.is_dir(),
                    message="" if skill_path.is_dir() else f"Referenced skill '{spec.skill_hint}' does not exist",
                )
            )

        return VerifyResult(passed=all(check.passed for check in checks), checks=checks)

    def promote(self, capability_id: str, *, actor: str = "system") -> ChangeResult:
        verify_result = self.verify(capability_id, staged=True)
        if not verify_result.passed:
            return ChangeResult(success=False, reason=verify_result.summary)

        staged_dir = self._staging_dir / capability_id
        active_dir = self._capabilities_dir / capability_id
        backup_id: str | None = None
        created_new = not active_dir.exists()

        if active_dir.exists():
            backup_id = self._create_backup(capability_id, active_dir)
            shutil.rmtree(active_dir)

        shutil.copytree(staged_dir, active_dir)
        shutil.rmtree(staged_dir, ignore_errors=True)

        self._audit.record(
            action="capability_promoted",
            target=capability_id,
            actor=actor,
            details={
                "manifest_path": str(active_dir / "CAPABILITY.yaml"),
                "backup_id": backup_id,
                "created_new": created_new,
            },
        )
        return ChangeResult(success=True, reason="Capability promoted", backup_id=backup_id)

    def rollback(self, capability_id: str, *, actor: str = "system") -> ChangeResult:
        active_dir = self._capabilities_dir / capability_id
        backup_dirs = sorted(
            (self._backups_dir / capability_id).glob("*/"),
            key=lambda path: path.name,
            reverse=True,
        ) if (self._backups_dir / capability_id).exists() else []

        if backup_dirs:
            backup_dir = backup_dirs[0]
            if active_dir.exists():
                shutil.rmtree(active_dir)
            shutil.copytree(backup_dir, active_dir)
            self._audit.record(
                action="capability_rolled_back",
                target=capability_id,
                actor=actor,
                details={"restored_from": str(backup_dir)},
            )
            return ChangeResult(success=True, reason="Capability rolled back", backup_id=backup_dir.name)

        if active_dir.exists():
            shutil.rmtree(active_dir)
            self._audit.record(
                action="capability_rolled_back",
                target=capability_id,
                actor=actor,
                details={"removed": True, "reason": "no backup available"},
            )
            return ChangeResult(success=True, reason="Capability removed (no backup available)")

        return ChangeResult(success=False, reason=f"No active capability or backup found for {capability_id}")

    def _create_backup(self, capability_id: str, source_dir: Path) -> str:
        backup_id = datetime.utcnow().strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:8]
        backup_dir = self._backups_dir / capability_id / backup_id
        backup_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_dir, backup_dir)
        return backup_id

    def _validate_spec_fields(
        self,
        *,
        capability_id: str,
        name: str,
        description: str,
        packages: list[str],
        imports: list[str],
        tools: list[str],
        skill_hint: str,
    ) -> str | None:
        if not _CAPABILITY_ID_RE.match(capability_id):
            return "Capability id must be snake_case and 3-64 chars long"
        if not name.strip():
            return "Capability name is required"
        if not description.strip():
            return "Capability description is required"
        if not packages or not all(_PACKAGE_RE.match(item) for item in packages):
            return "Capability packages are required and must be syntactically valid"
        if not imports or not all(_IMPORT_RE.match(item) for item in imports):
            return "Capability imports are required and must be valid module names"
        if any(not tool.strip() for tool in tools):
            return "Capability tools list contains empty entries"
        if skill_hint and "/" in skill_hint:
            return "Capability skill_hint must be a skill id, not a path"
        return None

    def _write_manifest(
        self,
        manifest_path: Path,
        *,
        capability_id: str,
        name: str,
        description: str,
        packages: list[str],
        imports: list[str],
        tools: list[str],
        skill_hint: str,
    ) -> None:
        payload = {
            "id": capability_id,
            "name": name,
            "description": description,
            "packages": packages,
            "imports": imports,
            "tools": tools,
            "skill_hint": skill_hint,
        }
        manifest_path.write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
