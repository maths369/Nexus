from __future__ import annotations

from pathlib import Path

import yaml

from nexus.services.vault import VaultManagerService
from nexus.shared.config import load_nexus_settings


def _write_config(root: Path, vault_base: str = "./vault") -> Path:
    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "app.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "vault": {"base_path": vault_base},
                "audio": {
                    "temp_directory": "./vault/_system/audio_temp",
                    "final_directory": "./vault/_system/audio",
                    "transcript_directory": "./vault/_system/transcripts",
                },
                "evolution": {"sandbox": {"vault_root": "./vault"}},
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return config_path


def test_create_under_creates_vault_and_updates_config(tmp_path):
    _write_config(tmp_path)
    sqlite_dir = tmp_path / "data" / "sqlite"
    sqlite_dir.mkdir(parents=True, exist_ok=True)
    (sqlite_dir / "knowledge.db").write_text("old", encoding="utf-8")
    settings = load_nexus_settings(tmp_path)
    manager = VaultManagerService(settings)

    result = manager.create_under(tmp_path / "alt")

    new_root = tmp_path / "alt" / "vault"
    assert result.new_root == new_root.resolve()
    assert (new_root / "pages").is_dir()
    raw = yaml.safe_load((tmp_path / "config" / "app.yaml").read_text(encoding="utf-8"))
    assert raw["vault"]["base_path"] == str(new_root.resolve())
    assert raw["audio"]["transcript_directory"] == str((new_root / "_system" / "transcripts").resolve())
    assert not (sqlite_dir / "knowledge.db").exists()
    assert list(sqlite_dir.glob("knowledge.db.bak-*"))


def test_create_under_allows_existing_empty_directory(tmp_path):
    _write_config(tmp_path)
    target_root = tmp_path / "empty-vault"
    target_root.mkdir(parents=True, exist_ok=True)
    settings = load_nexus_settings(tmp_path)
    manager = VaultManagerService(settings)

    result = manager.create_under(target_root, exact=True)

    assert result.new_root == target_root.resolve()
    assert (target_root / "pages").is_dir()


def test_migrate_to_copy_switches_root_and_preserves_source(tmp_path):
    _write_config(tmp_path)
    source_root = tmp_path / "vault"
    source_root.mkdir(parents=True, exist_ok=True)
    (source_root / "pages").mkdir(parents=True, exist_ok=True)
    (source_root / "pages" / "demo.md").write_text("# Demo\n\nHello\n", encoding="utf-8")

    settings = load_nexus_settings(tmp_path)
    manager = VaultManagerService(settings)
    result = manager.migrate_to(tmp_path / "migrated-vault", mode="copy")

    assert result.old_root == source_root.resolve()
    assert (tmp_path / "migrated-vault" / "pages" / "demo.md").exists()
    assert (source_root / "pages" / "demo.md").exists()
    raw = yaml.safe_load((tmp_path / "config" / "app.yaml").read_text(encoding="utf-8"))
    assert raw["vault"]["base_path"] == str((tmp_path / "migrated-vault").resolve())


def test_migrate_to_rejects_nested_target(tmp_path):
    _write_config(tmp_path)
    source_root = tmp_path / "vault"
    source_root.mkdir(parents=True, exist_ok=True)
    settings = load_nexus_settings(tmp_path)
    manager = VaultManagerService(settings)

    try:
        manager.migrate_to(source_root / "nested-target", mode="copy")
    except ValueError as exc:
        assert "Nested source/target" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_import_legacy_vault_filters_system_files_and_maps_sections(tmp_path):
    _write_config(tmp_path)
    target_root = tmp_path / "vault"
    target_root.mkdir(parents=True, exist_ok=True)
    source_root = tmp_path / "legacy-vault"
    (source_root / "pages").mkdir(parents=True, exist_ok=True)
    (source_root / "meetings").mkdir(parents=True, exist_ok=True)
    (source_root / "_system").mkdir(parents=True, exist_ok=True)
    (source_root / "日志").mkdir(parents=True, exist_ok=True)
    (source_root / "pages" / "demo.md").write_text("# Demo\n\ncontent\n", encoding="utf-8")
    (source_root / "meetings" / "m1.md").write_text("# Meeting\n\nnotes\n", encoding="utf-8")
    (source_root / "日志" / "2026-01-01.md").write_text("# 日志\n\nhello\n", encoding="utf-8")
    (source_root / "_system" / "index.db").write_text("binary", encoding="utf-8")
    (source_root / "plans.json").write_text("{}", encoding="utf-8")

    settings = load_nexus_settings(tmp_path)
    manager = VaultManagerService(settings)
    result = manager.import_legacy_vault(source_root)

    assert result.files_copied == 3
    assert result.files_skipped == 2
    assert (target_root / "pages" / "imports" / "macos-ai-assistant" / "demo.md").exists()
    assert (target_root / "meetings" / "imports" / "macos-ai-assistant" / "m1.md").exists()
    assert (target_root / "journals" / "imports" / "macos-ai-assistant" / "日志" / "2026-01-01.md").exists()
    assert not (target_root / "_system" / "index.db").exists()
    assert result.summary_note_path.exists()
