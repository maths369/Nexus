"""Voiceprint registration, matching, and whitelist management.

Storage layout:
    vault/_system/voiceprints/
        index.json          - metadata for all profiles
        {slug}.npy          - L2-normalised embedding vector
        {slug}.json         - per-profile metadata
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class VoiceprintProfile:
    """Metadata for a registered speaker."""

    name: str  # display name (e.g. "杨磊")
    slug: str  # filesystem-safe identifier
    sample_count: int = 0
    created_at: str = ""
    updated_at: str = ""


class VoiceprintStore:
    """Manages a local directory of speaker voiceprints.

    Each registered speaker has:
        - ``{slug}.npy``  — averaged L2-normalised embedding
        - ``{slug}.json`` — profile metadata

    All .npy vectors are loaded into ``_index`` on first access for
    fast cosine-similarity matching.
    """

    def __init__(
        self,
        storage_dir: Path,
        *,
        similarity_threshold: float = 0.65,
        embedding_extractor: Any | None = None,
    ):
        self._dir = Path(storage_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._threshold = similarity_threshold
        self._embedding_extractor = embedding_extractor  # DiarizationEngine 或独立模型
        # in-memory index: slug -> (profile, embedding)
        self._index: dict[str, tuple[VoiceprintProfile, np.ndarray]] = {}
        self._loaded = False

    # -- public API ---------------------------------------------------------

    def register(
        self,
        name: str,
        audio_path: Path,
        *,
        embedding: np.ndarray | None = None,
    ) -> VoiceprintProfile:
        """Register a new speaker or add a sample to an existing one.

        Parameters
        ----------
        name:
            Display name (e.g. "杨磊").
        audio_path:
            Path to a clean audio sample (10-30 seconds recommended).
        embedding:
            Pre-computed embedding. If ``None``, will be extracted
            via the configured embedding extractor.
        """
        self._ensure_loaded()
        slug = self._name_to_slug(name)

        if embedding is None:
            embedding = self._extract_embedding(audio_path)
        embedding = self._normalize(embedding)

        now = datetime.now(timezone.utc).isoformat()
        existing = self._index.get(slug)

        if existing is not None:
            profile, old_emb = existing
            # 加权平均：越多样本权重越高
            n = profile.sample_count
            merged = (old_emb * n + embedding) / (n + 1)
            merged = self._normalize(merged)
            profile.sample_count = n + 1
            profile.updated_at = now
            self._index[slug] = (profile, merged)
        else:
            profile = VoiceprintProfile(
                name=name,
                slug=slug,
                sample_count=1,
                created_at=now,
                updated_at=now,
            )
            self._index[slug] = (profile, embedding)

        self._save_profile(slug)
        return self._index[slug][0]

    def identify(self, embedding: np.ndarray) -> tuple[str, float] | None:
        """Match an embedding against the whitelist.

        Returns ``(display_name, similarity)`` if above threshold,
        otherwise ``None``.
        """
        self._ensure_loaded()
        if not self._index:
            return None

        embedding = self._normalize(embedding)
        best_name: str | None = None
        best_sim = -1.0

        for _slug, (profile, stored_emb) in self._index.items():
            sim = float(np.dot(embedding, stored_emb))
            if sim > best_sim:
                best_sim = sim
                best_name = profile.name

        if best_name is not None and best_sim >= self._threshold:
            return best_name, best_sim
        return None

    def list_profiles(self) -> list[VoiceprintProfile]:
        """Return all registered voiceprint profiles."""
        self._ensure_loaded()
        return [p for p, _ in self._index.values()]

    def get_profile(self, name: str) -> VoiceprintProfile | None:
        """Lookup a profile by display name."""
        self._ensure_loaded()
        slug = self._name_to_slug(name)
        entry = self._index.get(slug)
        return entry[0] if entry else None

    def delete(self, name: str) -> bool:
        """Remove a speaker from the whitelist."""
        self._ensure_loaded()
        slug = self._name_to_slug(name)
        if slug not in self._index:
            return False
        del self._index[slug]
        npy_path = self._dir / f"{slug}.npy"
        json_path = self._dir / f"{slug}.json"
        npy_path.unlink(missing_ok=True)
        json_path.unlink(missing_ok=True)
        self._save_index()
        return True

    # -- embedding extraction -----------------------------------------------

    def _extract_embedding(self, audio_path: Path) -> np.ndarray:
        """Extract speaker embedding from an audio file."""
        if self._embedding_extractor is None:
            raise RuntimeError(
                "No embedding extractor configured. "
                "Either provide a pre-computed embedding or set embedding_extractor."
            )
        import soundfile as sf

        data, sample_rate = sf.read(str(audio_path), dtype="float32")
        if data.ndim > 1:
            data = data.mean(axis=1)

        result = self._embedding_extractor.generate(
            input=data,
            granularity="utterance",
            sample_rate=sample_rate,
        )

        if isinstance(result, list) and result:
            first = result[0]
            if isinstance(first, dict) and "spk_embedding" in first:
                return np.array(first["spk_embedding"], dtype=np.float32)
            return np.array(first, dtype=np.float32)
        if isinstance(result, np.ndarray):
            return result.flatten().astype(np.float32)
        raise RuntimeError(f"Unexpected embedding result type: {type(result)}")

    # -- persistence --------------------------------------------------------

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._load_all()
        self._loaded = True

    def _load_all(self) -> None:
        """Load all profiles and embeddings from disk."""
        for json_path in sorted(self._dir.glob("*.json")):
            if json_path.name == "index.json":
                continue
            slug = json_path.stem
            npy_path = self._dir / f"{slug}.npy"
            if not npy_path.exists():
                logger.warning("Voiceprint .npy missing for %s, skipping", slug)
                continue
            try:
                with open(json_path, encoding="utf-8") as f:
                    meta = json.load(f)
                profile = VoiceprintProfile(
                    name=meta.get("name", slug),
                    slug=slug,
                    sample_count=int(meta.get("sample_count", 1)),
                    created_at=str(meta.get("created_at", "")),
                    updated_at=str(meta.get("updated_at", "")),
                )
                emb = np.load(str(npy_path)).astype(np.float32)
                emb = self._normalize(emb)
                self._index[slug] = (profile, emb)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to load voiceprint %s: %s", slug, exc)

        logger.info("Loaded %d voiceprint profiles from %s", len(self._index), self._dir)

    def _save_profile(self, slug: str) -> None:
        """Persist a single profile (metadata + embedding) to disk."""
        entry = self._index.get(slug)
        if entry is None:
            return
        profile, emb = entry

        json_path = self._dir / f"{slug}.json"
        npy_path = self._dir / f"{slug}.npy"

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(asdict(profile), f, ensure_ascii=False, indent=2)
        np.save(str(npy_path), emb)

        self._save_index()

    def _save_index(self) -> None:
        """Write summary index.json with all profiles."""
        index_path = self._dir / "index.json"
        profiles = [asdict(p) for p, _ in self._index.values()]
        with open(index_path, "w", encoding="utf-8") as f:
            json.dump({"profiles": profiles, "count": len(profiles)}, f, ensure_ascii=False, indent=2)

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _normalize(vec: np.ndarray) -> np.ndarray:
        norm = np.linalg.norm(vec)
        if norm > 0:
            return vec / norm
        return vec

    @staticmethod
    def _name_to_slug(name: str) -> str:
        """Convert display name to filesystem-safe slug."""
        # 保留中文字符和字母数字
        slug = name.strip().lower()
        slug = re.sub(r"[^\w\u4e00-\u9fff]", "_", slug)
        slug = re.sub(r"_+", "_", slug).strip("_")
        return slug or "unknown"
