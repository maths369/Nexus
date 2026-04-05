"""Speaker diarization engine using FSMN-VAD + CAM++ embeddings + clustering.

Pipeline:
    1. VAD (FSMN-VAD) segments audio into speech regions
    2. CAM++ extracts speaker embeddings per region
    3. Spectral / AHC clustering assigns speaker labels
    4. Optional voiceprint matching resolves known speakers
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from .voiceprint import VoiceprintStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class DiarizedSegment:
    """A single speech segment with speaker attribution."""

    start: float
    end: float
    speaker_id: str  # "speaker_0" or resolved name
    text: str | None = None  # filled after transcription alignment
    confidence: float = 0.0  # voiceprint match confidence (0 if clustered)


@dataclass
class DiarizationResult:
    """Complete diarization output for one audio file."""

    segments: list[DiarizedSegment] = field(default_factory=list)
    num_speakers: int = 0
    speaker_map: dict[str, str] = field(default_factory=dict)  # cluster_id -> display name


@dataclass
class DiarizationConfig:
    """Configuration knobs for the diarization engine."""

    enabled: bool = True
    vad_model: str = "fsmn-vad"
    embedding_model: str = "iic/speech_campplus_sv_zh-cn_16k-common"
    device: str = "cpu"
    min_speakers: int = 1
    max_speakers: int = 10
    clustering: str = "spectral"  # "spectral" | "ahc"
    similarity_threshold: float = 0.65
    # VAD tuning
    vad_max_single_segment_time: int = 60000  # ms
    vad_speech_noise_thres: float = 0.8
    # minimum segment duration (seconds) to extract embedding
    min_segment_duration: float = 0.5


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class DiarizationEngine:
    """Orchestrates VAD -> embedding extraction -> clustering -> speaker labelling."""

    def __init__(self, config: DiarizationConfig | None = None):
        self._config = config or DiarizationConfig()
        self._vad_model: Any = None
        self._embedding_model: Any = None
        self._initialized = False

    @property
    def config(self) -> DiarizationConfig:
        return self._config

    def generate(self, *, input: Any, **kwargs: Any) -> Any:
        """Proxy speaker-embedding extraction for VoiceprintStore.

        Voiceprint registration only needs the CAM++ embedding model, not the
        full diarization pipeline. Exposing a ``generate`` method lets the
        engine act as a lazy embedding extractor without duplicating model
        loading logic.
        """
        self._lazy_init()
        return self._embedding_model.generate(input=input, **kwargs)

    # -- public API ---------------------------------------------------------

    def diarize(
        self,
        audio_path: Path,
        *,
        voiceprint_store: VoiceprintStore | None = None,
        num_speakers: int | None = None,
    ) -> DiarizationResult:
        """Run full diarization pipeline on *audio_path*.

        Parameters
        ----------
        audio_path:
            Path to the audio file (wav / mp3 / flac / m4a etc.)
        voiceprint_store:
            If provided, cluster representatives are matched against
            the whitelist to resolve known speaker names.
        num_speakers:
            Hint for exact number of speakers. ``None`` lets the
            algorithm decide (bounded by config min/max).
        """
        self._lazy_init()

        # Step 1 — VAD
        vad_segments = self._run_vad(audio_path)
        if not vad_segments:
            logger.info("VAD returned no speech segments for %s", audio_path)
            return DiarizationResult()

        # Step 2 — extract embeddings per segment
        embeddings, valid_segments = self._extract_embeddings(audio_path, vad_segments)
        if len(embeddings) == 0:
            logger.warning("No valid embeddings extracted from %s", audio_path)
            return DiarizationResult()

        # Step 3 — clustering
        n_speakers = num_speakers or self._estimate_num_speakers(embeddings)
        n_speakers = max(self._config.min_speakers, min(n_speakers, self._config.max_speakers))

        if n_speakers <= 1 or len(embeddings) <= 1:
            labels = np.zeros(len(embeddings), dtype=int)
            n_speakers = 1
        else:
            labels = self._cluster(embeddings, n_speakers)

        # Step 4 — build speaker map + resolve via voiceprint whitelist
        speaker_map = self._build_speaker_map(labels, embeddings, voiceprint_store)

        # Step 5 — assemble result
        segments: list[DiarizedSegment] = []
        for idx, seg in enumerate(valid_segments):
            cluster_id = f"speaker_{labels[idx]}"
            display_name = speaker_map.get(cluster_id, cluster_id)
            confidence = 0.0
            # If resolved from voiceprint, store the match confidence
            if display_name != cluster_id and voiceprint_store is not None:
                confidence = speaker_map.get(f"_conf_{cluster_id}", 0.0)  # type: ignore[arg-type]
            segments.append(
                DiarizedSegment(
                    start=seg[0],
                    end=seg[1],
                    speaker_id=display_name,
                    confidence=confidence,
                )
            )

        return DiarizationResult(
            segments=segments,
            num_speakers=n_speakers,
            speaker_map={k: v for k, v in speaker_map.items() if not k.startswith("_conf_")},
        )

    # -- VAD ----------------------------------------------------------------

    def _run_vad(self, audio_path: Path) -> list[tuple[float, float]]:
        """Return list of (start_sec, end_sec) speech regions."""
        result = self._vad_model.generate(
            input=str(audio_path),
            max_single_segment_time=self._config.vad_max_single_segment_time,
            speech_noise_thres=self._config.vad_speech_noise_thres,
        )
        segments: list[tuple[float, float]] = []
        if not result:
            return segments
        for item in result:
            if isinstance(item, dict) and "value" in item:
                # FunASR FSMN-VAD returns [{"key": ..., "value": [[start_ms, end_ms], ...]}]
                for pair in item["value"]:
                    if isinstance(pair, (list, tuple)) and len(pair) >= 2:
                        start_sec = pair[0] / 1000.0
                        end_sec = pair[1] / 1000.0
                        if end_sec - start_sec >= self._config.min_segment_duration:
                            segments.append((start_sec, end_sec))
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                start_sec = item[0] / 1000.0
                end_sec = item[1] / 1000.0
                if end_sec - start_sec >= self._config.min_segment_duration:
                    segments.append((start_sec, end_sec))
        return segments

    # -- Embedding extraction -----------------------------------------------

    def _extract_embeddings(
        self,
        audio_path: Path,
        vad_segments: list[tuple[float, float]],
    ) -> tuple[np.ndarray, list[tuple[float, float]]]:
        """Extract one embedding per VAD segment. Returns (N, D) array + valid segments."""
        import soundfile as sf

        data, sample_rate = sf.read(str(audio_path), dtype="float32")
        if data.ndim > 1:
            data = data.mean(axis=1)  # mono

        embeddings_list: list[np.ndarray] = []
        valid_segments: list[tuple[float, float]] = []

        for start_sec, end_sec in vad_segments:
            start_sample = int(start_sec * sample_rate)
            end_sample = int(end_sec * sample_rate)
            chunk = data[start_sample:end_sample]

            if len(chunk) < int(self._config.min_segment_duration * sample_rate):
                continue

            try:
                emb = self._embedding_model.generate(
                    input=chunk,
                    granularity="utterance",
                    sample_rate=sample_rate,
                )
                # CAM++ returns list of dicts with "spk_embedding" key
                if isinstance(emb, list) and emb:
                    first = emb[0]
                    if isinstance(first, dict) and "spk_embedding" in first:
                        vec = np.array(first["spk_embedding"], dtype=np.float32)
                    else:
                        vec = np.array(first, dtype=np.float32)
                elif isinstance(emb, np.ndarray):
                    vec = emb.flatten().astype(np.float32)
                else:
                    continue

                # L2 normalize
                norm = np.linalg.norm(vec)
                if norm > 0:
                    vec = vec / norm
                embeddings_list.append(vec)
                valid_segments.append((start_sec, end_sec))
            except Exception as exc:  # noqa: BLE001
                logger.debug("Embedding extraction failed for segment [%.1f-%.1f]: %s", start_sec, end_sec, exc)

        if not embeddings_list:
            return np.array([]), []
        return np.stack(embeddings_list), valid_segments

    # -- Clustering ---------------------------------------------------------

    def _cluster(self, embeddings: np.ndarray, n_speakers: int) -> np.ndarray:
        """Cluster embeddings into *n_speakers* groups."""
        if self._config.clustering == "ahc":
            return self._cluster_ahc(embeddings, n_speakers)
        return self._cluster_spectral(embeddings, n_speakers)

    @staticmethod
    def _cluster_spectral(embeddings: np.ndarray, n_speakers: int) -> np.ndarray:
        from sklearn.cluster import SpectralClustering

        # cosine similarity -> affinity matrix
        similarity = embeddings @ embeddings.T
        affinity = (similarity + 1.0) / 2.0  # map [-1,1] -> [0,1]
        np.fill_diagonal(affinity, 1.0)

        model = SpectralClustering(
            n_clusters=n_speakers,
            affinity="precomputed",
            random_state=42,
            assign_labels="kmeans",
        )
        return model.fit_predict(affinity)

    @staticmethod
    def _cluster_ahc(embeddings: np.ndarray, n_speakers: int) -> np.ndarray:
        from sklearn.cluster import AgglomerativeClustering

        model = AgglomerativeClustering(
            n_clusters=n_speakers,
            metric="cosine",
            linkage="average",
        )
        return model.fit_predict(embeddings)

    def _estimate_num_speakers(self, embeddings: np.ndarray) -> int:
        """Heuristic: eigenvalue gap on cosine similarity matrix."""
        if len(embeddings) <= 2:
            return len(embeddings)

        similarity = embeddings @ embeddings.T
        affinity = (similarity + 1.0) / 2.0
        np.fill_diagonal(affinity, 1.0)

        try:
            eigenvalues = np.sort(np.linalg.eigvalsh(affinity))[::-1]
            # find largest gap in top eigenvalues
            max_k = min(self._config.max_speakers, len(eigenvalues) - 1)
            gaps = np.diff(eigenvalues[:max_k + 1])
            # the gap is negative (eigenvalues are descending), largest drop
            best_k = int(np.argmin(gaps)) + 1
            return max(self._config.min_speakers, min(best_k, self._config.max_speakers))
        except Exception:  # noqa: BLE001
            return min(2, self._config.max_speakers)

    # -- Speaker map with voiceprint matching -------------------------------

    def _build_speaker_map(
        self,
        labels: np.ndarray,
        embeddings: np.ndarray,
        voiceprint_store: VoiceprintStore | None,
    ) -> dict[str, Any]:
        """Build cluster_id -> display_name map, optionally matching voiceprints."""
        unique_labels = sorted(set(int(x) for x in labels))
        speaker_map: dict[str, Any] = {}

        # compute representative embedding per cluster (centroid)
        centroids: dict[int, np.ndarray] = {}
        for label in unique_labels:
            mask = labels == label
            centroid = embeddings[mask].mean(axis=0)
            norm = np.linalg.norm(centroid)
            if norm > 0:
                centroid = centroid / norm
            centroids[label] = centroid

        if voiceprint_store is not None:
            # greedy 1:1 matching: highest similarity first
            matches: list[tuple[float, int, str]] = []
            for label, centroid in centroids.items():
                match = voiceprint_store.identify(centroid)
                if match is not None:
                    name, sim = match
                    matches.append((sim, label, name))

            # sort by similarity descending, resolve greedily
            matches.sort(reverse=True)
            used_names: set[str] = set()
            used_labels: set[int] = set()
            for sim, label, name in matches:
                if name in used_names or label in used_labels:
                    continue
                cluster_id = f"speaker_{label}"
                speaker_map[cluster_id] = name
                speaker_map[f"_conf_{cluster_id}"] = round(sim, 4)
                used_names.add(name)
                used_labels.add(label)

        # fill unmapped clusters with default labels
        for label in unique_labels:
            cluster_id = f"speaker_{label}"
            if cluster_id not in speaker_map:
                speaker_map[cluster_id] = cluster_id

        return speaker_map

    # -- Lazy initialization ------------------------------------------------

    def _lazy_init(self) -> None:
        if self._initialized:
            return
        try:
            from funasr import AutoModel

            logger.info("Loading VAD model: %s", self._config.vad_model)
            self._vad_model = AutoModel(
                model=self._config.vad_model,
                device=self._config.device,
                disable_update=True,
            )

            logger.info("Loading speaker embedding model: %s", self._config.embedding_model)
            self._embedding_model = AutoModel(
                model=self._config.embedding_model,
                device=self._config.device,
                disable_update=True,
            )
            self._initialized = True
            logger.info("Diarization engine initialized on %s", self._config.device)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to initialize diarization engine: %s", exc)
            raise RuntimeError(f"Diarization engine init failed: {exc}") from exc
