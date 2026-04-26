"""Cross-encoder reranking wrapper around BAAI/bge-reranker-v2-m3.

Reranking only runs over the ~20 candidates returned by the vector search, so a
CPU CrossEncoder is fast enough on DGX Spark and does not contend with vLLM/Nemotron
on the GPU.

Environment variables
---------------------
RERANKER_MODEL: Override the HuggingFace model id. Default ``BAAI/bge-reranker-v2-m3``.
RERANKER_DEVICE: Torch device to run on. Default ``cpu``.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from rag.retrieve import RetrievedChunk


DEFAULT_MODEL: str = "BAAI/bge-reranker-v2-m3"
DEFAULT_DEVICE: str = "cpu"

_model: Any | None = None


def _load_model() -> Any:
    """Lazily instantiate the CrossEncoder. Cached at module level."""
    global _model
    if _model is not None:
        return _model
    try:
        from sentence_transformers import CrossEncoder  # type: ignore
    except ImportError as exc:  # pragma: no cover - surfaced at first call
        raise ImportError(
            "sentence-transformers is required for reranking. "
            "Install via 'pip install sentence-transformers==3.0.1'."
        ) from exc

    model_name = os.environ.get("RERANKER_MODEL", DEFAULT_MODEL)
    device = os.environ.get("RERANKER_DEVICE", DEFAULT_DEVICE)
    _model = CrossEncoder(model_name, device=device)
    return _model


def rerank(
    query: str,
    candidates: list["RetrievedChunk"],
    top_k: int,
) -> list["RetrievedChunk"]:
    """Re-rank ``candidates`` against ``query`` and return the top ``top_k``.

    The original ``RetrievedChunk.score`` is replaced by the reranker logit so that
    downstream code sees a single, comparable score field per result.
    """
    if top_k < 1:
        raise ValueError("top_k must be >= 1")
    if not candidates:
        return []
    if not query or not query.strip():
        raise ValueError("rerank received empty query")

    model = _load_model()
    pairs = [[query, candidate.content] for candidate in candidates]
    scores = model.predict(pairs)

    enriched = []
    for candidate, score in zip(candidates, scores):
        enriched.append(
            type(candidate)(
                chunk_id=candidate.chunk_id,
                dataset_id=candidate.dataset_id,
                content=candidate.content,
                score=float(score),
                metadata=candidate.metadata,
            )
        )

    enriched.sort(key=lambda item: item.score, reverse=True)
    return enriched[:top_k]
