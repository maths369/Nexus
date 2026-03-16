from __future__ import annotations

from argparse import Namespace

import yaml

from nexus.__main__ import cmd_memory_status, cmd_vault_create, cmd_vault_import, cmd_vault_status


def test_cmd_vault_status_prints_current_root(monkeypatch, tmp_path, capsys):
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "app.yaml").write_text(
        yaml.safe_dump({"vault": {"base_path": "./vault"}}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    (tmp_path / "vault").mkdir()

    monkeypatch.setattr("nexus.__main__.find_project_root", lambda: tmp_path)

    cmd_vault_status(Namespace())

    out = capsys.readouterr().out
    assert "vault_root" in out
    assert str((tmp_path / "vault").resolve()) in out


def test_cmd_vault_create_updates_config(monkeypatch, tmp_path, capsys):
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "app.yaml").write_text(
        yaml.safe_dump(
            {
                "vault": {"base_path": "./vault"},
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
    monkeypatch.setattr("nexus.__main__.find_project_root", lambda: tmp_path)

    cmd_vault_create(
        Namespace(path=str(tmp_path / "storage"), name="vault", exact=False, no_switch=False)
    )

    out = capsys.readouterr().out
    assert "new_root" in out
    assert (tmp_path / "storage" / "vault" / "pages").is_dir()


class _FakeBrowserService:
    async def aclose(self) -> None:
        return None


class _FakeMemory:
    def describe(self):
        return {"entry_count": 3, "compression": {"enabled": False}}


class _FakeCompressor:
    def describe(self):
        return {"enabled": True, "layers": {"micro_compact": {"enabled": True}}}


class _FakeRuntime:
    def __init__(self, tmp_path):
        self.paths = type("Paths", (), {"vault": tmp_path / "vault"})()
        self.episodic_memory = _FakeMemory()
        self.compressor = _FakeCompressor()
        self.browser_service = _FakeBrowserService()


def test_cmd_memory_status_prints_memory_and_compression(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("nexus.__main__.find_project_root", lambda: tmp_path)
    monkeypatch.setattr("nexus.__main__.load_nexus_settings", lambda *_args, **_kwargs: object())
    monkeypatch.setattr("nexus.api.runtime.build_runtime", lambda **_kwargs: _FakeRuntime(tmp_path))

    cmd_memory_status(Namespace())

    out = capsys.readouterr().out
    assert "episodic_memory" in out
    assert "context_compression" in out


class _FakeStructuralIndex:
    def rebuild_from_vault(self, vault_root):
        return {"pages": 3, "markdown_pages": 3, "pdf_pages": 0}


class _FakeIngestService:
    def reindex_all(self):
        return {"files_processed": 3, "chunks_created": 7, "errors": 0, "files_skipped": 0}


class _FakeRuntimeForImport:
    def __init__(self, tmp_path):
        self.paths = type("Paths", (), {"vault": tmp_path / "vault"})()
        self.structural_index = _FakeStructuralIndex()
        self.ingest_service = _FakeIngestService()
        self.background_manager = None
        self.browser_service = None


def test_cmd_vault_import_prints_import_and_reindex_stats(monkeypatch, tmp_path, capsys):
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "app.yaml").write_text(
        yaml.safe_dump({"vault": {"base_path": "./vault"}}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    (tmp_path / "vault").mkdir()
    source_root = tmp_path / "legacy"
    (source_root / "pages").mkdir(parents=True, exist_ok=True)
    (source_root / "pages" / "demo.md").write_text("# Demo\n\nHello\n", encoding="utf-8")

    monkeypatch.setattr("nexus.__main__.find_project_root", lambda: tmp_path)
    monkeypatch.setattr("nexus.api.runtime.build_runtime", lambda **_kwargs: _FakeRuntimeForImport(tmp_path))

    cmd_vault_import(
        Namespace(source=str(source_root), target=None, label="legacy-demo")
    )

    out = capsys.readouterr().out
    assert "files_copied" in out
    assert "structural_rebuild" in out
    assert "retrieval_reindex" in out
