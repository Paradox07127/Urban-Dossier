"""Top-level retrieve API for the Urban-Dossier v2 RAG pipeline.

Pipeline: ``embed_query`` -> vector index search (cuVS GPU on DGX Spark, FAISS-CPU
fallback) -> optional ``rerank`` -> trimmed ``list[RetrievedChunk]``. The agent
loop (``urban-dossier-analyst``) consumes this function as its only entry point.

Environment variables
---------------------
RAG_INDEX_DIR: Directory containing ``corpus`` index file and its sidecars.
    Default ``./index``.
RAG_INDEX_FILENAME: Override the index filename. Default ``corpus.faiss`` for
    FAISS or ``corpus.cuvs`` for cuVS. The loader auto-detects which is present.
RAG_VECTOR_OVERSAMPLE: Pre-rerank candidate count. Default 20.
RAG_PREFER_GPU: Set to ``0`` to force FAISS-CPU even when cuvs is importable.
    Default ``1``.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rag.embed import embed_query
from rag.vector_index import CuvsIndex, FaissIndex, VectorIndex, cuvs_available


DEFAULT_INDEX_DIR: str = "./index"
DEFAULT_OVERSAMPLE: int = 20
_FAISS_FILENAME: str = "corpus.faiss"
_CUVS_FILENAME: str = "corpus.cuvs"


@dataclass
class RetrievedChunk:
    """A single chunk returned by :func:`retrieve` with its similarity score."""

    chunk_id: str
    dataset_id: str
    content: str
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)


_index_cache: VectorIndex | None = None
_index_cache_path: Path | None = None


def _prefer_gpu() -> bool:
    return os.environ.get("RAG_PREFER_GPU", "1") != "0"


def _resolve_index_path() -> Path:
    base = Path(os.environ.get("RAG_INDEX_DIR", DEFAULT_INDEX_DIR))
    explicit = os.environ.get("RAG_INDEX_FILENAME")
    if explicit:
        return base / explicit
    # Prefer cuVS index file when GPU path is preferred and the file exists.
    if _prefer_gpu() and cuvs_available():
        cuvs_path = base / _CUVS_FILENAME
        if cuvs_path.exists() or Path(str(cuvs_path) + ".vectors.npy").exists():
            return cuvs_path
    return base / _FAISS_FILENAME


def _get_index() -> VectorIndex:
    """Return the vector index, loading and caching it on first call.

    Loads ``CuvsIndex`` when cuvs is importable and the cuVS sidecar files exist;
    falls back to ``FaissIndex`` otherwise. Selection mirrors what ingest wrote.
    """
    global _index_cache, _index_cache_path
    target = _resolve_index_path()
    if _index_cache is not None and _index_cache_path == target:
        return _index_cache

    if target.name == _CUVS_FILENAME and _prefer_gpu() and cuvs_available():
        _index_cache = CuvsIndex.load(target)
    else:
        if not target.exists():
            raise FileNotFoundError(
                f"Vector index not found at {target}. "
                "Run 'python -m rag.ingest catalog.json --index-dir <dir>' first."
            )
        _index_cache = FaissIndex.load(target)
    _index_cache_path = target
    return _index_cache


def _oversample() -> int:
    raw = os.environ.get("RAG_VECTOR_OVERSAMPLE")
    if raw is None:
        return DEFAULT_OVERSAMPLE
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_OVERSAMPLE
    return max(1, value)


def _to_retrieved_chunk(score: float, meta: dict[str, Any]) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=str(meta.get("chunk_id", "")),
        dataset_id=str(meta.get("dataset_id", "")),
        content=str(meta.get("content", "")),
        score=float(score),
        metadata=meta,
    )


def retrieve(
    query: str,
    dataset_filter: list[str] | None = None,
    top_k: int = 5,
    rerank: bool = True,
) -> list[RetrievedChunk]:
    """Embed the query, search the corpus, optionally rerank, and return top results.

    ``dataset_filter`` is applied as a metadata predicate on ``dataset_id`` and is
    used to disambiguate a question that should be scoped to a specific dataset.
    Passing ``None`` searches the whole corpus.
    """
    if not isinstance(query, str) or not query.strip():
        raise ValueError("retrieve received empty query")
    if top_k < 1:
        raise ValueError("top_k must be >= 1")

    index = _get_index()
    query_vec = embed_query(query)
    candidate_count = max(top_k, _oversample())
    metadata_filter: dict[str, Any] | None = None
    if dataset_filter:
        metadata_filter = {"dataset_id": list(dataset_filter)}

    raw_results = index.search(query_vec, top_k=candidate_count, filter=metadata_filter)
    candidates = [_to_retrieved_chunk(score, meta) for score, meta in raw_results]

    if not candidates:
        return []

    if rerank:
        from rag.rerank import rerank as _do_rerank
        return _do_rerank(query, candidates, top_k=top_k)

    return candidates[:top_k]


def reset_index_cache() -> None:
    """Drop the cached FAISS index. Used by tests that swap index files at runtime."""
    global _index_cache, _index_cache_path
    _index_cache = None
    _index_cache_path = None
