from __future__ import annotations

import sys
import types

import numpy as np

from nexus.services.audio.diarization import DiarizationConfig, DiarizationEngine
from nexus.services.audio.voiceprint import VoiceprintStore


class _StubEmbeddingExtractor:
    def __init__(self, vector: list[float]):
        self._vector = vector
        self.calls: list[dict[str, object]] = []

    def generate(self, **kwargs):  # noqa: ANN003
        self.calls.append(kwargs)
        return [{"spk_embedding": self._vector}]


def test_voiceprint_store_register_uses_configured_embedding_extractor(tmp_path, monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "soundfile",
        types.SimpleNamespace(
            read=lambda _path, dtype="float32": (np.array([0.1, -0.1, 0.2], dtype=np.float32), 16000)
        ),
    )

    extractor = _StubEmbeddingExtractor([1.0, 0.0, 0.0])
    store = VoiceprintStore(
        tmp_path / "voiceprints",
        similarity_threshold=0.65,
        embedding_extractor=extractor,
    )

    profile = store.register("杨磊", tmp_path / "sample.wav")

    assert profile.name == "杨磊"
    assert profile.sample_count == 1
    assert extractor.calls
    assert extractor.calls[0]["granularity"] == "utterance"
    assert extractor.calls[0]["sample_rate"] == 16000
    assert (tmp_path / "voiceprints" / f"{profile.slug}.npy").exists()
    assert (tmp_path / "voiceprints" / f"{profile.slug}.json").exists()
    assert (tmp_path / "voiceprints" / "index.json").exists()


def test_diarization_engine_can_proxy_embedding_generate():
    engine = DiarizationEngine(DiarizationConfig(enabled=True))
    extractor = _StubEmbeddingExtractor([0.0, 1.0, 0.0])
    engine._embedding_model = extractor  # noqa: SLF001
    engine._vad_model = object()  # noqa: SLF001
    engine._initialized = True  # noqa: SLF001

    result = engine.generate(
        input=np.array([0.2, 0.3], dtype=np.float32),
        granularity="utterance",
        sample_rate=16000,
    )

    assert result == [{"spk_embedding": [0.0, 1.0, 0.0]}]
    assert extractor.calls[0]["granularity"] == "utterance"
    assert extractor.calls[0]["sample_rate"] == 16000
