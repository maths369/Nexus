from __future__ import annotations

import asyncio
import sys
from pathlib import Path
import yaml

from nexus.evolution import AuditLog, CapabilityManager


class _FakeProc:
    def __init__(self, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b""):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.killed = False

    async def communicate(self):
        return self._stdout, self._stderr

    def kill(self):
        self.killed = True

    async def wait(self):
        return self.returncode


def test_list_capabilities_includes_excel(tmp_path):
    mgr = CapabilityManager(
        capabilities_dir=_project_capabilities_dir(),
        audit=AuditLog(tmp_path / "audit.db"),
    )
    ids = {item["capability_id"] for item in mgr.list_capabilities()}
    assert "excel_processing" in ids


def test_capability_status_unknown(tmp_path):
    mgr = CapabilityManager(
        capabilities_dir=_project_capabilities_dir(),
        audit=AuditLog(tmp_path / "audit.db"),
    )
    status = mgr.get_status("nope")
    assert status["known"] is False
    assert status["enabled"] is False


def test_enable_capability_success(monkeypatch, tmp_path):
    mgr = CapabilityManager(
        capabilities_dir=_project_capabilities_dir(),
        audit=AuditLog(tmp_path / "audit.db"),
    )
    calls = {"enabled_checks": 0}

    def fake_is_enabled(spec):
        calls["enabled_checks"] += 1
        return calls["enabled_checks"] >= 2

    async def fake_create_subprocess_exec(*args, **kwargs):
        return _FakeProc(returncode=0)

    monkeypatch.setattr(mgr, "_is_enabled", fake_is_enabled)
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    result = asyncio.run(mgr.enable("excel_processing"))
    assert result.success is True

    entries = mgr._audit.query(action="capability_enabled")
    assert len(entries) == 1
    assert entries[0].target == "excel_processing"


def test_enable_capability_failure_is_audited(monkeypatch, tmp_path):
    mgr = CapabilityManager(
        capabilities_dir=_project_capabilities_dir(),
        audit=AuditLog(tmp_path / "audit.db"),
    )

    async def fake_create_subprocess_exec(*args, **kwargs):
        return _FakeProc(returncode=1, stderr=b"pip failed")

    monkeypatch.setattr(mgr, "_is_enabled", lambda spec: False)
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    result = asyncio.run(mgr.enable("excel_processing"))
    assert result.success is False
    assert "pip failed" in result.reason

    entries = mgr._audit.query(action="capability_enable_failed")
    assert len(entries) == 1
    assert entries[0].target == "excel_processing"


def test_capability_manager_falls_back_to_current_interpreter_for_missing_python(tmp_path):
    mgr = CapabilityManager(
        capabilities_dir=_project_capabilities_dir(),
        python_executable="/definitely/missing/python",
        audit=AuditLog(tmp_path / "audit.db"),
    )
    assert mgr._python == sys.executable


def test_list_capabilities_reports_manifest_path(tmp_path):
    mgr = CapabilityManager(
        capabilities_dir=_project_capabilities_dir(),
        audit=AuditLog(tmp_path / "audit.db"),
    )
    items = {item["capability_id"]: item for item in mgr.list_capabilities()}
    assert items["excel_processing"]["manifest_path"].endswith("capabilities/excel_processing/CAPABILITY.yaml")


def test_capability_manager_loads_manifests_from_disk(tmp_path):
    capabilities_dir = tmp_path / "capabilities"
    manifest_dir = capabilities_dir / "custom_processing"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "CAPABILITY.yaml").write_text(
        "\n".join(
            [
                "id: custom_processing",
                "name: Custom Processing",
                "description: custom capability",
                "packages:",
                "  - samplepkg>=1.0.0",
                "imports:",
                "  - json",
                "tools:",
                "  - custom_tool",
                "skill_hint: custom-skill",
            ]
        ),
        encoding="utf-8",
    )
    mgr = CapabilityManager(
        capabilities_dir=capabilities_dir,
        audit=AuditLog(tmp_path / "audit.db"),
    )
    status = mgr.get_status("custom_processing")
    assert status["known"] is True
    assert status["tools"] == ["custom_tool"]
    assert status["manifest_path"].endswith("custom_processing/CAPABILITY.yaml")


def test_create_capability_writes_staged_manifest(tmp_path):
    mgr = CapabilityManager(
        capabilities_dir=tmp_path / "capabilities",
        staging_dir=tmp_path / "staging" / "capabilities",
        backups_dir=tmp_path / "backups" / "capabilities",
        skills_dir=tmp_path / "skills",
        audit=AuditLog(tmp_path / "audit.db"),
    )

    result = mgr.create(
        capability_id="ppt_processing",
        name="PPT Processing",
        description="read and summarize ppt files",
        packages=["python-pptx>=1.0.0"],
        imports=["pptx"],
        tools=["ppt_extract_text"],
        skill_hint="ppt-processing",
        actor="agent",
    )

    assert result.success is True
    manifest_path = tmp_path / "staging" / "capabilities" / "ppt_processing" / "CAPABILITY.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    assert manifest["id"] == "ppt_processing"
    assert manifest["tools"] == ["ppt_extract_text"]
    entries = mgr._audit.query(action="capability_created")
    assert len(entries) == 1


def test_register_capability_creates_verifies_and_promotes(tmp_path):
    mgr = CapabilityManager(
        capabilities_dir=tmp_path / "capabilities",
        staging_dir=tmp_path / "staging" / "capabilities",
        backups_dir=tmp_path / "backups" / "capabilities",
        skills_dir=tmp_path / "skills",
        audit=AuditLog(tmp_path / "audit.db"),
    )

    result = mgr.register(
        capability_id="ppt_processing",
        name="PPT Processing",
        description="read and summarize ppt files",
        packages=["python-pptx>=1.0.0"],
        imports=["pptx"],
        tools=["ppt_extract_text"],
        actor="agent",
        auto_promote=True,
    )

    assert result["created"] is True
    assert result["verified"] is True
    assert result["promoted"] is True
    assert (tmp_path / "capabilities" / "ppt_processing" / "CAPABILITY.yaml").exists()


def test_stage_existing_capability_copies_to_staging(tmp_path):
    mgr = CapabilityManager(
        capabilities_dir=_project_capabilities_dir(),
        staging_dir=tmp_path / "staging" / "capabilities",
        backups_dir=tmp_path / "backups" / "capabilities",
        audit=AuditLog(tmp_path / "audit.db"),
    )

    result = mgr.stage("excel_processing", actor="agent")
    assert result.success is True
    assert (tmp_path / "staging" / "capabilities" / "excel_processing" / "CAPABILITY.yaml").exists()


def test_verify_staged_capability_checks_skill_hint(tmp_path):
    capabilities_dir = tmp_path / "capabilities"
    staging_dir = tmp_path / "staging" / "capabilities"
    skills_dir = tmp_path / "skills"
    (skills_dir / "ppt-processing").mkdir(parents=True)
    staged_manifest = staging_dir / "ppt_processing" / "CAPABILITY.yaml"
    staged_manifest.parent.mkdir(parents=True)
    staged_manifest.write_text(
        yaml.safe_dump(
            {
                "id": "ppt_processing",
                "name": "PPT Processing",
                "description": "read ppt",
                "packages": ["python-pptx>=1.0.0"],
                "imports": ["pptx"],
                "tools": ["ppt_extract_text"],
                "skill_hint": "ppt-processing",
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    mgr = CapabilityManager(
        capabilities_dir=capabilities_dir,
        staging_dir=staging_dir,
        backups_dir=tmp_path / "backups" / "capabilities",
        skills_dir=skills_dir,
        audit=AuditLog(tmp_path / "audit.db"),
    )

    result = mgr.verify("ppt_processing", staged=True)
    assert result.passed is True


def test_promote_moves_staged_manifest_to_active(tmp_path):
    staging_dir = tmp_path / "staging" / "capabilities"
    staged_manifest = staging_dir / "ppt_processing" / "CAPABILITY.yaml"
    staged_manifest.parent.mkdir(parents=True)
    staged_manifest.write_text(
        yaml.safe_dump(
            {
                "id": "ppt_processing",
                "name": "PPT Processing",
                "description": "read ppt",
                "packages": ["python-pptx>=1.0.0"],
                "imports": ["pptx"],
                "tools": ["ppt_extract_text"],
                "skill_hint": "",
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    mgr = CapabilityManager(
        capabilities_dir=tmp_path / "capabilities",
        staging_dir=staging_dir,
        backups_dir=tmp_path / "backups" / "capabilities",
        audit=AuditLog(tmp_path / "audit.db"),
    )

    result = mgr.promote("ppt_processing", actor="agent")
    assert result.success is True
    assert (tmp_path / "capabilities" / "ppt_processing" / "CAPABILITY.yaml").exists()
    assert not staged_manifest.exists()
    entries = mgr._audit.query(action="capability_promoted")
    assert len(entries) == 1


def test_promote_existing_capability_creates_backup(tmp_path):
    capabilities_dir = tmp_path / "capabilities"
    active_manifest = capabilities_dir / "ppt_processing" / "CAPABILITY.yaml"
    active_manifest.parent.mkdir(parents=True)
    active_manifest.write_text(
        yaml.safe_dump(
            {
                "id": "ppt_processing",
                "name": "Old PPT Processing",
                "description": "old version",
                "packages": ["python-pptx>=0.9.0"],
                "imports": ["pptx"],
                "tools": [],
                "skill_hint": "",
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    staged_manifest = tmp_path / "staging" / "capabilities" / "ppt_processing" / "CAPABILITY.yaml"
    staged_manifest.parent.mkdir(parents=True)
    staged_manifest.write_text(
        yaml.safe_dump(
            {
                "id": "ppt_processing",
                "name": "New PPT Processing",
                "description": "new version",
                "packages": ["python-pptx>=1.0.0"],
                "imports": ["pptx"],
                "tools": ["ppt_extract_text"],
                "skill_hint": "",
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    mgr = CapabilityManager(
        capabilities_dir=capabilities_dir,
        staging_dir=tmp_path / "staging" / "capabilities",
        backups_dir=tmp_path / "backups" / "capabilities",
        audit=AuditLog(tmp_path / "audit.db"),
    )

    result = mgr.promote("ppt_processing", actor="agent")
    assert result.success is True
    assert result.backup_id is not None
    backup_manifest = tmp_path / "backups" / "capabilities" / "ppt_processing" / result.backup_id / "CAPABILITY.yaml"
    assert backup_manifest.exists()


def test_rollback_restores_latest_backup(tmp_path):
    capabilities_dir = tmp_path / "capabilities"
    backup_dir = tmp_path / "backups" / "capabilities" / "ppt_processing" / "20260313220000-abcdef12"
    backup_dir.mkdir(parents=True)
    (backup_dir / "CAPABILITY.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "ppt_processing",
                "name": "Backup PPT Processing",
                "description": "backup version",
                "packages": ["python-pptx>=0.9.0"],
                "imports": ["pptx"],
                "tools": [],
                "skill_hint": "",
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    active_manifest = capabilities_dir / "ppt_processing" / "CAPABILITY.yaml"
    active_manifest.parent.mkdir(parents=True)
    active_manifest.write_text(
        yaml.safe_dump(
            {
                "id": "ppt_processing",
                "name": "Broken PPT Processing",
                "description": "broken",
                "packages": ["broken>=1.0.0"],
                "imports": ["broken"],
                "tools": [],
                "skill_hint": "",
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    mgr = CapabilityManager(
        capabilities_dir=capabilities_dir,
        staging_dir=tmp_path / "staging" / "capabilities",
        backups_dir=tmp_path / "backups" / "capabilities",
        audit=AuditLog(tmp_path / "audit.db"),
    )

    result = mgr.rollback("ppt_processing", actor="agent")
    assert result.success is True
    restored = yaml.safe_load(active_manifest.read_text(encoding="utf-8"))
    assert restored["name"] == "Backup PPT Processing"


def _project_capabilities_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "capabilities"
