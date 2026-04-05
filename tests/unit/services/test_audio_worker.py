from __future__ import annotations

import importlib

import yaml

from nexus.shared import load_nexus_settings


def test_build_audio_worker_app_wires_voiceprint_store_to_diarization_engine(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "app.yaml").write_text(
        yaml.safe_dump(
            {
                "audio": {
                    "diarization": {
                        "enabled": True,
                        "similarity_threshold": 0.7,
                    }
                }
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    worker_module = importlib.import_module("nexus.services.audio.worker_app")
    captured: dict[str, object] = {}

    class DummyAudioService:
        def __init__(self, *args, diarization_engine=None, voiceprint_store=None, config=None, **kwargs):  # noqa: ANN002, ANN003
            captured["diarization_engine"] = diarization_engine
            captured["voiceprint_store"] = voiceprint_store
            self.config = config

        def is_available(self) -> bool:
            return True

    monkeypatch.setattr(worker_module, "load_nexus_settings", lambda: load_nexus_settings(tmp_path))
    monkeypatch.setattr(worker_module, "AudioService", DummyAudioService)

    app = worker_module.build_audio_worker_app()

    assert app is not None
    assert captured["diarization_engine"] is not None
    assert captured["voiceprint_store"] is not None
    assert captured["voiceprint_store"]._embedding_extractor is captured["diarization_engine"]  # noqa: SLF001
