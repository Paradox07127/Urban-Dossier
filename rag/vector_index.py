"""Vector index abstraction with cuVS (NVIDIA GPU) as primary, FAISS-CPU fallback.

The corpus for Urban-Dossier v2 is small (~18 datasets x ~5 chunks ~= 90 vectors at
2560 dims for Qwen3-Embedding-4B). cuVS brute_force is exact, trivially filterable,
and avoids the build-cost of approximate indices like CAGRA/IVF that only pay off
at >100k vectors.

Backend selection
-----------------
``create_index(dim)`` returns:
  - ``CuvsIndex`` when the cuvs Python package is importable (DGX Spark target)
  - ``FaissIndex`` otherwise (Mac/dev fallback)

The selection is logged once at module load. Override with ``prefer_gpu=False``
to force CPU FAISS even when cuvs is available (useful for A/B benchmarks).
"""
from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

try:
    import faiss  # type: ignore
except ImportError as _faiss_exc:
    raise ImportError(
        "faiss-cpu is required (FAISS is the dev fallback when cuvs is unavailable). "
        "Install via 'pip install faiss-cpu==1.8.0'."
    ) from _faiss_exc

try:
    import cuvs  # type: ignore  # noqa: F401
    from cuvs.neighbors import brute_force  # type: ignore
    _CUVS_AVAILABLE: bool = True
except ImportError:
    _CUVS_AVAILABLE = False
    brute_force = None  # type: ignore[assignment]


FILTER_OVERSAMPLE: int = 5


class VectorIndex(ABC):
    """Abstract base for swappable vector indices."""

    @abstractmethod
    def add(self, vectors: np.ndarray, metadata: list[dict]) -> None:
        """Insert ``vectors`` (N x D float32) with parallel ``metadata`` list."""

    @abstractmethod
    def search(
        self,
        query: np.ndarray,
        top_k: int,
        filter: dict | None = None,
    ) -> list[tuple[float, dict]]:
        """Return ``top_k`` (score, metadata) pairs ranked by descending similarity."""

    @abstractmethod
    def save(self, path: str | Path) -> None:
        """Persist the index and its metadata sidecar to disk under ``path``."""

    @classmethod
    @abstractmethod
    def load(cls, path: str | Path) -> "VectorIndex":
        """Restore an index previously saved via :meth:`save`."""

    @property
    @abstractmethod
    def backend(self) -> str:
        """Human-readable backend identifier for logs."""


def cuvs_available() -> bool:
    """Return True if the cuVS Python package was importable at module load."""
    return _CUVS_AVAILABLE


def create_index(dim: int, prefer_gpu: bool = True) -> VectorIndex:
    """Factory: return cuVS GPU index when available, else FAISS-CPU fallback.

    Logs the selection once per process. Pass ``prefer_gpu=False`` to force CPU
    even when cuVS is importable (A/B benchmarks).
    """
    if prefer_gpu and _CUVS_AVAILABLE:
        logger.info("VectorIndex backend: CuvsIndex (cuVS brute_force, GPU)")
        return CuvsIndex(dim)
    if prefer_gpu and not _CUVS_AVAILABLE:
        logger.warning(
            "VectorIndex backend: FaissIndex (CPU fallback) - cuvs not importable. "
            "Install via 'pip install cuvs-cu13' or 'conda install -c rapidsai cuvs' on DGX Spark."
        )
    else:
        logger.info("VectorIndex backend: FaissIndex (CPU, prefer_gpu=False)")
    return FaissIndex(dim)


def _matches_filter(meta: dict, filter_: dict | None) -> bool:
    """Return True if every (key, value) in ``filter_`` matches ``meta``.

    A list/tuple/set value in the filter is treated as a membership test.
    """
    if not filter_:
        return True
    for key, expected in filter_.items():
        actual = meta.get(key)
        if isinstance(expected, (list, tuple, set)):
            if actual not in expected:
                return False
        else:
            if actual != expected:
                return False
    return True


def _coerce_query(query: np.ndarray, dim: int) -> np.ndarray:
    if query.ndim == 1:
        query = query[np.newaxis, :]
    if query.shape[1] != dim:
        raise ValueError(f"Query dim {query.shape[1]} does not match index dim {dim}")
    if query.dtype != np.float32:
        query = query.astype(np.float32)
    return np.ascontiguousarray(query)


def _meta_path(base: Path) -> Path:
    suffix = base.suffix
    if suffix:
        return base.with_suffix(suffix + ".meta.json")
    return base.parent / f"{base.name}.meta.json"


class FaissIndex(VectorIndex):
    """Flat inner-product FAISS index. Cosine sim assumes vectors are L2-normalized.

    Used as the dev/CPU fallback when cuVS is not installed. On DGX Spark, the
    factory ``create_index()`` prefers ``CuvsIndex``.
    """

    def __init__(self, dim: int) -> None:
        self.dim: int = dim
        self._index = faiss.IndexFlatIP(dim)
        self._metadata: list[dict] = []

    @property
    def backend(self) -> str:
        return "faiss-cpu"

    def add(self, vectors: np.ndarray, metadata: list[dict]) -> None:
        """Insert vectors of shape (N, dim) with N parallel metadata dicts."""
        if vectors.ndim != 2:
            raise ValueError(f"vectors must be 2-D, got shape {vectors.shape}")
        if vectors.shape[1] != self.dim:
            raise ValueError(f"Vector dim {vectors.shape[1]} != index dim {self.dim}")
        if vectors.shape[0] != len(metadata):
            raise ValueError(
                f"vectors rows ({vectors.shape[0]}) must equal len(metadata) ({len(metadata)})"
            )
        if vectors.dtype != np.float32:
            vectors = vectors.astype(np.float32)
        self._index.add(np.ascontiguousarray(vectors))
        self._metadata.extend(metadata)

    def search(
        self,
        query: np.ndarray,
        top_k: int,
        filter: dict | None = None,
    ) -> list[tuple[float, dict]]:
        """Return ``top_k`` (cosine_score, metadata) pairs, descending."""
        if top_k < 1:
            raise ValueError("top_k must be >= 1")
        if not self._metadata:
            return []

        query = _coerce_query(query, self.dim)
        oversample = top_k * FILTER_OVERSAMPLE if filter else top_k
        oversample = min(oversample, len(self._metadata))
        scores, indices = self._index.search(query, oversample)

        results: list[tuple[float, dict]] = []
        for score, idx in zip(scores[0], indices[0]):
            if idx < 0:
                continue
            meta = self._metadata[int(idx)]
            if _matches_filter(meta, filter):
                results.append((float(score), meta))
                if len(results) >= top_k:
                    break
        return results

    def save(self, path: str | Path) -> None:
        """Persist FAISS index to ``{path}`` and metadata to ``{path}.meta.json``."""
        base = Path(path)
        base.parent.mkdir(parents=True, exist_ok=True)
        faiss.write_index(self._index, str(base))
        meta_path = _meta_path(base)
        meta_payload = {"dim": self.dim, "metadata": self._metadata, "backend": self.backend}
        meta_path.write_text(json.dumps(meta_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "FaissIndex":
        """Load a FAISS index and its metadata sidecar from ``path``."""
        base = Path(path)
        if not base.exists():
            raise FileNotFoundError(f"FAISS index not found at {base}")
        meta_path = _meta_path(base)
        if not meta_path.exists():
            raise FileNotFoundError(f"FAISS metadata sidecar not found at {meta_path}")

        raw = json.loads(meta_path.read_text(encoding="utf-8"))
        dim = int(raw.get("dim", 0))
        if dim <= 0:
            raise ValueError(f"Invalid dim {dim} in {meta_path}")

        instance = cls(dim=dim)
        instance._index = faiss.read_index(str(base))
        instance._metadata = list(raw.get("metadata", []))
        return instance

    def __len__(self) -> int:
        return len(self._metadata)


class CuvsIndex(VectorIndex):
    """NVIDIA cuVS brute_force index. Exact KNN on GPU; ideal for small corpora.

    brute_force is chosen over CAGRA/IVF because:
      - The Urban-Dossier RAG corpus is ~90 vectors; approximate indices add
        build cost for no recall benefit.
      - brute_force is exact (recall=1.0) and supports trivial post-filtering
        on metadata (the same pattern as FaissIndex.search).
      - For larger corpora (>100k vectors) swap to ``cuvs.neighbors.cagra``
        with the same VectorIndex interface.
    """

    def __init__(self, dim: int) -> None:
        if not _CUVS_AVAILABLE:
            raise RuntimeError(
                "cuvs is not installed. Install via 'pip install cuvs-cu13' "
                "or 'conda install -c rapidsai cuvs' on DGX Spark."
            )
        self.dim: int = dim
        self._vectors: list[np.ndarray] = []
        self._metadata: list[dict] = []
        self._index: Any | None = None

    @property
    def backend(self) -> str:
        return "cuvs-brute_force"

    def _ensure_built(self) -> None:
        if self._index is not None or not self._vectors:
            return
        matrix = np.vstack(self._vectors).astype(np.float32)
        params = brute_force.IndexParams(metric="inner_product")  # type: ignore[union-attr]
        self._index = brute_force.build(params, matrix)  # type: ignore[union-attr]

    def add(self, vectors: np.ndarray, metadata: list[dict]) -> None:
        """Stage vectors for the next brute_force build (cuVS is bulk-build)."""
        if vectors.ndim != 2 or vectors.shape[1] != self.dim:
            raise ValueError(f"vectors must be (N, {self.dim}); got {vectors.shape}")
        if vectors.shape[0] != len(metadata):
            raise ValueError("vectors rows must equal len(metadata)")
        self._vectors.append(np.ascontiguousarray(vectors.astype(np.float32)))
        self._metadata.extend(metadata)
        self._index = None

    def search(
        self,
        query: np.ndarray,
        top_k: int,
        filter: dict | None = None,
    ) -> list[tuple[float, dict]]:
        """Return ``top_k`` (score, metadata) pairs from the cuVS brute_force index."""
        if top_k < 1:
            raise ValueError("top_k must be >= 1")
        if not self._metadata:
            return []
        self._ensure_built()
        if self._index is None:
            return []

        query = _coerce_query(query, self.dim)
        oversample = top_k * FILTER_OVERSAMPLE if filter else top_k
        oversample = min(oversample, len(self._metadata))
        search_params = brute_force.SearchParams()  # type: ignore[union-attr]
        distances, indices = brute_force.search(  # type: ignore[union-attr]
            search_params, self._index, query, oversample,
        )
        distances_np = np.asarray(distances)
        indices_np = np.asarray(indices)

        results: list[tuple[float, dict]] = []
        for score, idx in zip(distances_np[0], indices_np[0]):
            if int(idx) < 0:
                continue
            meta = self._metadata[int(idx)]
            if _matches_filter(meta, filter):
                results.append((float(score), meta))
                if len(results) >= top_k:
                    break
        return results

    def save(self, path: str | Path) -> None:
        """Persist staged vectors + metadata. brute_force index is rebuilt on load."""
        base = Path(path)
        base.parent.mkdir(parents=True, exist_ok=True)
        if self._vectors:
            matrix = np.vstack(self._vectors).astype(np.float32)
        else:
            matrix = np.zeros((0, self.dim), dtype=np.float32)
        np.save(str(base) + ".vectors.npy", matrix)
        meta_path = _meta_path(base)
        meta_payload = {"dim": self.dim, "metadata": self._metadata, "backend": self.backend}
        meta_path.write_text(json.dumps(meta_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "CuvsIndex":
        """Restore a cuVS index from disk and rebuild the brute_force structure."""
        base = Path(path)
        meta_path = _meta_path(base)
        vectors_path = Path(str(base) + ".vectors.npy")
        if not meta_path.exists() or not vectors_path.exists():
            raise FileNotFoundError(f"Missing cuVS sidecar files near {base}")

        raw = json.loads(meta_path.read_text(encoding="utf-8"))
        dim = int(raw.get("dim", 0))
        if dim <= 0:
            raise ValueError(f"Invalid dim {dim} in {meta_path}")
        instance = cls(dim=dim)
        matrix = np.load(str(vectors_path))
        if matrix.size > 0:
            instance.add(matrix, list(raw.get("metadata", [])))
        return instance

    def __len__(self) -> int:
        return len(self._metadata)


__all__ = (
    "VectorIndex",
    "FaissIndex",
    "CuvsIndex",
    "create_index",
    "cuvs_available",
)
