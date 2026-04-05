from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from nexus.evolution import AuditLog, Sandbox, SkillManager
from nexus.evolution.clawhub_client import ClawHubArchiveDownload


def _write_installable_skill(
    base,
    skill_id: str,
    *,
    keywords: list[str] | None = None,
    install_commands: list[str] | None = None,
) -> None:
    """统一写入 SKILL.md（单文件模式，元数据全部在 frontmatter 中）。"""
    skill_dir = base / skill_id
    skill_dir.mkdir(parents=True)
    kw_lines = "".join(f"  - {item}\n" for item in (keywords or []))
    install_lines = "".join(f"  - {item}\n" for item in (install_commands or []))
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"name: {skill_id}\n"
        "description: 测试 installable skill\n"
        "tags:\n"
        "  - test\n"
        "keywords:\n"
        f"{kw_lines}"
        "verify_imports:\n"
        "  - json\n"
        + ("install_commands:\n" + install_lines if install_commands else "")
        + "---\n\n"
        "# Test Skill\n\n"
        "Use this skill when the task matches the registry entry.\n",
        encoding="utf-8",
    )


def _make_manager(tmp_path, *, remote_sources=None, clawhub_client=None):
    skills_dir = tmp_path / "skills"
    catalog_dir = tmp_path / "skill_registry"
    sandbox = Sandbox(tmp_path / "staging")
    audit = AuditLog(tmp_path / "audit.db")
    return SkillManager(
        skills_dir,
        sandbox,
        audit,
        catalog_dir=catalog_dir,
        remote_sources=remote_sources,
        clawhub_client=clawhub_client,
    ), catalog_dir


class _FakeClawHubClient:
    def __init__(
        self,
        *,
        search_results: list[dict[str, object]] | None = None,
        detail: dict[str, object] | None = None,
        archive_path: Path | None = None,
        base_url: str = "https://clawhub.example.com",
    ) -> None:
        self._search_results = list(search_results or [])
        self._detail = detail or {}
        self._archive_path = archive_path
        self._base_url = base_url
        self.search_calls: list[dict[str, object]] = []
        self.detail_calls: list[dict[str, object]] = []
        self.download_calls: list[dict[str, object]] = []

    def resolve_base_url(self, base_url: str | None = None) -> str:
        return base_url or self._base_url

    async def search_skills(self, query: str, *, limit: int = 10, base_url: str | None = None) -> list[dict[str, object]]:
        self.search_calls.append({"query": query, "limit": limit, "base_url": base_url})
        return list(self._search_results)

    async def get_skill_detail(self, slug: str, *, base_url: str | None = None) -> dict[str, object]:
        self.detail_calls.append({"slug": slug, "base_url": base_url})
        return dict(self._detail)

    async def download_skill_archive(
        self,
        slug: str,
        *,
        version: str | None = None,
        tag: str | None = None,
        base_url: str | None = None,
    ) -> ClawHubArchiveDownload:
        self.download_calls.append({
            "slug": slug,
            "version": version,
            "tag": tag,
            "base_url": base_url,
        })
        assert self._archive_path is not None
        payload = self._archive_path.read_bytes()
        integrity = "sha256-" + base64.b64encode(hashlib.sha256(payload).digest()).decode("ascii")
        return ClawHubArchiveDownload(archive_path=self._archive_path, integrity=integrity)


def _make_clawhub_archive(
    base: Path,
    slug: str,
    *,
    manifest_name: str = "skill.md",
    metadata_block: str = "",
) -> Path:
    archive_dir = base / f"{slug}-archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = archive_dir / f"{slug}.zip"
    body = (
        "---\n"
        f"id: {slug}\n"
        f"name: {slug}\n"
        "description: ClawHub imported skill\n"
        f"{metadata_block}"
        "---\n\n"
        "# Imported Skill\n\n"
        "Use this skill when imported from ClawHub.\n"
    )
    with zipfile.ZipFile(archive_path, "w") as zf:
        zf.writestr(f"{slug}/{manifest_name}", body)
        zf.writestr(f"{slug}/references/example.md", "reference")
    return archive_path


def test_list_installable_skills_returns_registry_entries(tmp_path):
    manager, catalog_dir = _make_manager(tmp_path)
    _write_installable_skill(catalog_dir, "office-conversion", keywords=["ppt", "pdf", "转换"])
    _write_installable_skill(catalog_dir, "api-integration", keywords=["api", "webhook"])

    items = manager.list_installable_skills()
    skill_ids = {item["skill_id"] for item in items}

    assert "office-conversion" in skill_ids
    assert "api-integration" in skill_ids


def test_list_installable_skills_query_ranks_matching_entry(tmp_path):
    manager, catalog_dir = _make_manager(tmp_path)
    _write_installable_skill(catalog_dir, "office-conversion", keywords=["ppt", "pdf", "转换"])
    _write_installable_skill(catalog_dir, "database-ops", keywords=["sql", "数据库"])

    items = manager.list_installable_skills(query="把PPT转换为PDF")

    assert items
    assert items[0]["skill_id"] == "office-conversion"
    assert items[0]["match_score"] > 0


def test_list_installable_skills_matches_hyphenated_skill_id(tmp_path):
    manager, catalog_dir = _make_manager(tmp_path)
    _write_installable_skill(catalog_dir, "iso-13485-certification")

    items = manager.list_installable_skills(query="ISO 13485")

    assert items
    assert items[0]["skill_id"] == "iso-13485-certification"


def test_install_from_catalog_installs_skill_bundle(tmp_path):
    manager, catalog_dir = _make_manager(tmp_path)
    _write_installable_skill(catalog_dir, "office-conversion", keywords=["ppt", "pdf", "转换"])

    result = asyncio.run(manager.install_from_catalog("office-conversion", actor="agent"))

    assert result["success"] is True
    assert result["installed"] is True
    assert (tmp_path / "skills" / "office-conversion" / "SKILL.md").exists()
    entries = manager._audit.query(action="skill_installed")  # noqa: SLF001
    assert any(entry.target == "office-conversion" for entry in entries)


def test_install_from_catalog_missing_skill_returns_failure(tmp_path):
    manager, _ = _make_manager(tmp_path)

    result = asyncio.run(manager.install_from_catalog("missing-skill", actor="agent"))

    assert result["success"] is False
    assert "not found" in result["reason"]


def test_import_from_local_populates_registry_and_installs(tmp_path):
    manager, catalog_dir = _make_manager(tmp_path)
    downloads = tmp_path / "downloads"
    _write_installable_skill(
        downloads,
        "iso-13485-certification",
        keywords=["iso", "13485", "qms", "medical"],
    )

    result = asyncio.run(
        manager.import_from_local(
            downloads / "iso-13485-certification" / "SKILL.md",
            actor="agent",
            install=True,
        )
    )

    assert result["success"] is True
    assert result["imported"] is True
    assert result["installed"] is True
    assert (catalog_dir / "iso-13485-certification" / "SKILL.md").exists()
    assert (tmp_path / "skills" / "iso-13485-certification" / "SKILL.md").exists()
    entries = manager._audit.query(action="skill_imported")  # noqa: SLF001
    assert any(entry.target == "iso-13485-certification" for entry in entries)


def test_import_from_remote_uses_fetched_bundle(tmp_path, monkeypatch: pytest.MonkeyPatch):
    manager, catalog_dir = _make_manager(tmp_path)
    remote_source = tmp_path / "remote-source"
    _write_installable_skill(
        remote_source,
        "iso-13485-certification",
        keywords=["iso", "13485", "qms"],
    )
    bundle_dir = remote_source / "iso-13485-certification"

    async def _fake_fetch(repo: str, skill_path: str, ref: str, fetch_root):
        assert repo == "FreedomIntelligence/OpenClaw-Medical-Skills"
        assert skill_path == "skills/iso-13485-certification"
        assert ref == "main"
        return bundle_dir

    monkeypatch.setattr(manager, "_fetch_remote_skill_bundle", _fake_fetch)

    result = asyncio.run(
        manager.import_from_remote(
            "FreedomIntelligence/OpenClaw-Medical-Skills",
            "skills/iso-13485-certification",
            actor="agent",
        )
    )

    assert result["success"] is True
    assert result["imported"] is True
    assert result["installed"] is False
    assert (catalog_dir / "iso-13485-certification" / "SKILL.md").exists()
    entries = manager._audit.query(action="skill_imported")  # noqa: SLF001
    assert any(entry.target == "iso-13485-certification" for entry in entries)


def test_import_from_remote_install_writes_origin_and_lock(tmp_path, monkeypatch: pytest.MonkeyPatch):
    manager, _catalog_dir = _make_manager(tmp_path)
    remote_source = tmp_path / "remote-source"
    _write_installable_skill(
        remote_source,
        "iso-13485-certification",
        keywords=["iso", "13485", "qms"],
    )
    bundle_dir = remote_source / "iso-13485-certification"

    async def _fake_fetch(repo: str, skill_path: str, ref: str, fetch_root):
        assert repo == "FreedomIntelligence/OpenClaw-Medical-Skills"
        assert skill_path == "skills/iso-13485-certification"
        assert ref == "main"
        return bundle_dir

    monkeypatch.setattr(manager, "_fetch_remote_skill_bundle", _fake_fetch)

    result = asyncio.run(
        manager.import_from_remote(
            "FreedomIntelligence/OpenClaw-Medical-Skills",
            "skills/iso-13485-certification",
            actor="agent",
            install=True,
        )
    )

    origin_path = tmp_path / "skills" / "iso-13485-certification" / ".nexus" / "origin.json"
    lock_path = tmp_path / "skills" / "lock.json"
    origin = json.loads(origin_path.read_text(encoding="utf-8"))
    lock = json.loads(lock_path.read_text(encoding="utf-8"))

    assert result["success"] is True
    assert result["installed"] is True
    assert origin["source_type"] == "github"
    assert origin["repo"] == "FreedomIntelligence/OpenClaw-Medical-Skills"
    assert origin["ref"] == "main"
    assert origin["remote_path"] == "skills/iso-13485-certification"
    assert origin["content_fingerprint"]
    assert lock["skills"]["iso-13485-certification"]["content_fingerprint"] == origin["content_fingerprint"]


def test_list_skills_reports_origin_and_drift(tmp_path):
    manager, _catalog_dir = _make_manager(tmp_path)

    created = manager.create_skill(
        "workflow-memory",
        name="Workflow Memory",
        description="记录重复成功工作流。",
        body="# Workflow Memory\n\n将重复成功经验沉淀为 skill。",
    )
    assert created.success is True

    items = {item["skill_id"]: item for item in manager.list_skills()}
    assert items["workflow-memory"]["origin_source_type"] == "generated"
    assert items["workflow-memory"]["drift_detected"] is False

    skill_md = tmp_path / "skills" / "workflow-memory" / "SKILL.md"
    skill_md.write_text(skill_md.read_text(encoding="utf-8") + "\n补充新的执行建议。\n", encoding="utf-8")

    updated = {item["skill_id"]: item for item in manager.list_skills()}
    assert updated["workflow-memory"]["drift_detected"] is True


def test_installable_skill_blocks_unsafe_install_commands(tmp_path):
    manager, catalog_dir = _make_manager(tmp_path)
    _write_installable_skill(
        catalog_dir,
        "unsafe-installer",
        keywords=["unsafe"],
        install_commands=["brew install pandoc && rm -rf /"],
    )

    result = asyncio.run(manager.install_from_catalog("unsafe-installer", actor="agent"))

    assert result["success"] is False
    assert "unsafe shell syntax" in result["reason"]


def test_search_remote_skills_returns_scored_candidates(tmp_path, monkeypatch: pytest.MonkeyPatch):
    manager, _ = _make_manager(
        tmp_path,
        remote_sources=[
            {
                "name": "openclaw-medical",
                "repo": "FreedomIntelligence/OpenClaw-Medical-Skills",
                "ref": "main",
                "roots": ["skills"],
            }
        ],
    )
    remote_cache = tmp_path / "remote-cache"
    _write_installable_skill(
        remote_cache,
        "iso-13485-certification",
        keywords=["iso", "13485", "qms", "medical"],
    )
    spec = manager._load_installable_manifest(remote_cache / "iso-13485-certification" / "SKILL.md")  # noqa: SLF001

    async def _fake_search(source, query, *, limit=40):
        assert source.repo == "FreedomIntelligence/OpenClaw-Medical-Skills"
        assert source.ref == "main"
        assert source.roots == ("skills",)
        assert "ISO 13485" in query
        assert limit >= 40
        return [(spec, "skills/iso-13485-certification/SKILL.md")]

    monkeypatch.setattr(manager, "_search_remote_source", _fake_search)

    items = asyncio.run(manager.search_remote_skills("ISO 13485 QMS"))

    assert items
    assert items[0]["skill_id"] == "iso-13485-certification"
    assert items[0]["repo"] == "FreedomIntelligence/OpenClaw-Medical-Skills"
    assert items[0]["path"] == "skills/iso-13485-certification"
    assert items[0]["source_name"] == "openclaw-medical"
    assert items[0]["match_score"] > 0


def test_search_remote_skills_accepts_explicit_repo_without_config(tmp_path, monkeypatch: pytest.MonkeyPatch):
    manager, _ = _make_manager(tmp_path)
    remote_cache = tmp_path / "remote-explicit"
    _write_installable_skill(
        remote_cache,
        "risk-manager-iso-14971",
        keywords=["risk", "14971", "fmea"],
    )
    spec = manager._load_installable_manifest(remote_cache / "risk-manager-iso-14971" / "SKILL.md")  # noqa: SLF001

    async def _fake_search(source, query, *, limit=40):
        assert source.repo == "FreedomIntelligence/OpenClaw-Medical-Skills"
        assert source.ref == "main"
        assert "14971" in query
        return [(spec, "skills/risk-manager-iso-14971/SKILL.md")]

    monkeypatch.setattr(manager, "_search_remote_source", _fake_search)

    items = asyncio.run(
        manager.search_remote_skills(
            "14971 风险评估",
            repo="FreedomIntelligence/OpenClaw-Medical-Skills",
        )
    )

    assert items
    assert items[0]["skill_id"] == "risk-manager-iso-14971"
    assert items[0]["repo"] == "FreedomIntelligence/OpenClaw-Medical-Skills"


def test_search_clawhub_skills_returns_candidates(tmp_path):
    clawhub_client = _FakeClawHubClient(
        search_results=[
            {
                "score": 12.5,
                "slug": "iso-13485-certification",
                "displayName": "ISO 13485 Certification",
                "summary": "Medical QMS skill",
                "version": "1.2.3",
            }
        ]
    )
    manager, _ = _make_manager(tmp_path, clawhub_client=clawhub_client)

    items = asyncio.run(manager.search_clawhub_skills("ISO 13485", limit=5))

    assert items
    assert items[0]["skill_id"] == "iso-13485-certification"
    assert items[0]["slug"] == "iso-13485-certification"
    assert items[0]["version"] == "1.2.3"
    assert items[0]["source_type"] == "clawhub"
    assert items[0]["registry"] == "https://clawhub.example.com"


def test_import_from_clawhub_normalizes_lowercase_manifest_and_writes_origin(tmp_path):
    archive_path = _make_clawhub_archive(tmp_path, "obsidian-notes", manifest_name="skill.md")
    clawhub_client = _FakeClawHubClient(
        detail={
            "skill": {
                "slug": "obsidian-notes",
                "displayName": "Obsidian Notes",
            },
            "latestVersion": {
                "version": "2.0.1",
            },
            "owner": {
                "handle": "openclaw",
                "displayName": "OpenClaw",
            },
        },
        archive_path=archive_path,
    )
    manager, catalog_dir = _make_manager(tmp_path, clawhub_client=clawhub_client)

    result = asyncio.run(
        manager.import_from_clawhub(
            "obsidian-notes",
            actor="agent",
            install=True,
        )
    )

    origin_path = tmp_path / "skills" / "obsidian-notes" / ".nexus" / "origin.json"
    lock_path = tmp_path / "skills" / "lock.json"
    origin = json.loads(origin_path.read_text(encoding="utf-8"))
    lock = json.loads(lock_path.read_text(encoding="utf-8"))

    assert result["success"] is True
    assert result["installed"] is True
    assert (catalog_dir / "obsidian-notes" / "SKILL.md").exists()
    assert (tmp_path / "skills" / "obsidian-notes" / "SKILL.md").exists()
    assert origin["source_type"] == "clawhub"
    assert origin["slug"] == "obsidian-notes"
    assert origin["registry"] == "https://clawhub.example.com"
    assert origin["installed_version"] == "2.0.1"
    assert origin["publisher"] == "OpenClaw"
    assert origin["archive_integrity"].startswith("sha256-")
    assert lock["skills"]["obsidian-notes"]["slug"] == "obsidian-notes"
    assert lock["skills"]["obsidian-notes"]["installed_version"] == "2.0.1"


def test_import_from_clawhub_rejects_unsupported_openclaw_install_kind(tmp_path):
    metadata_block = (
        "metadata:\n"
        "  openclaw:\n"
        "    install:\n"
        "      - kind: pipx\n"
        "        package: unsupported-skill\n"
    )
    archive_path = _make_clawhub_archive(
        tmp_path,
        "unsupported-installer",
        metadata_block=metadata_block,
    )
    clawhub_client = _FakeClawHubClient(
        detail={
            "skill": {
                "slug": "unsupported-installer",
                "displayName": "Unsupported Installer",
            }
        },
        archive_path=archive_path,
    )
    manager, _ = _make_manager(tmp_path, clawhub_client=clawhub_client)

    result = asyncio.run(
        manager.import_from_clawhub(
            "unsupported-installer",
            actor="agent",
        )
    )

    assert result["success"] is False
    assert "Unsupported OpenClaw install kind" in result["reason"]


def _write_openclaw_skill(base, skill_id: str) -> None:
    """写入 OpenClaw 格式的 SKILL.md。"""
    skill_dir = base / skill_id
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        '---\n'
        f'name: {skill_id}\n'
        f'description: OpenClaw test skill for {skill_id}\n'
        'metadata:\n'
        '  {\n'
        '    "openclaw":\n'
        '      {\n'
        '        "emoji": "📄",\n'
        '        "tags": ["pdf", "document"],\n'
        '        "requires": { "bins": ["nano-pdf"] },\n'
        '        "install":\n'
        '          [\n'
        '            {\n'
        '              "kind": "uv",\n'
        '              "package": "nano-pdf",\n'
        '              "bins": ["nano-pdf"],\n'
        '            },\n'
        '          ],\n'
        '      },\n'
        '  }\n'
        '---\n\n'
        '# Test OpenClaw Skill\n\n'
        'Use nano-pdf to edit PDFs.\n',
        encoding="utf-8",
    )


def test_openclaw_format_skill_is_parsed(tmp_path):
    """OpenClaw 格式的 SKILL.md 能被正确解析。"""
    manager, catalog_dir = _make_manager(tmp_path)
    _write_openclaw_skill(catalog_dir, "nano-pdf")

    items = manager.list_installable_skills()

    assert len(items) == 1
    skill = items[0]
    assert skill["skill_id"] == "nano-pdf"
    assert skill["tags"] == ["pdf", "document"]
    assert skill["packages"] == ["nano-pdf"]
    assert "which nano-pdf" in skill["verify_commands"]


def test_openclaw_format_query_matches(tmp_path):
    """OpenClaw 格式的技能可以通过关键词搜索到。"""
    manager, catalog_dir = _make_manager(tmp_path)
    _write_openclaw_skill(catalog_dir, "nano-pdf")
    _write_installable_skill(catalog_dir, "database-ops", keywords=["sql", "数据库"])

    items = manager.list_installable_skills(query="pdf document")

    assert items
    assert items[0]["skill_id"] == "nano-pdf"


def test_openclaw_and_nexus_formats_coexist(tmp_path):
    """OpenClaw 格式和 Nexus 原生格式可以共存。"""
    manager, catalog_dir = _make_manager(tmp_path)
    _write_openclaw_skill(catalog_dir, "nano-pdf")
    _write_installable_skill(catalog_dir, "office-conversion", keywords=["ppt", "pdf", "转换"])

    items = manager.list_installable_skills()
    skill_ids = {item["skill_id"] for item in items}

    assert "nano-pdf" in skill_ids
    assert "office-conversion" in skill_ids
