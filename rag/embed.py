"""vLLM-served Qwen3-Embedding client for Urban-Dossier v2 RAG.

Embeddings come from a second vLLM instance hosting ``Qwen/Qwen3-Embedding-4B``
(or any OpenAI-compatible embedding model) on a separate port from the main
Nemotron-30B LLM instance. Both instances share GB10 unified memory — single
NVIDIA inference stack, no Ollama/HuggingFace runtime dependency.

Environment variables
---------------------
EMBEDDING_BASE_URL: OpenAI-compatible base URL. Default ``http://localhost:8001/v1``
EMBEDDING_MODEL:    Model id served by the vLLM embedding instance.
                    Default ``Qwen/Qwen3-Embedding-4B``.
EMBEDDING_API_KEY:  API key. Default ``not-needed`` (vLLM ignores it).
EMBEDDING_DIM:      Vector dimensionality. Default ``2560`` (Qwen3-Embedding-4B).
                    Override if you swap to Qwen3-Embedding-0.6B (1024) or
                    Qwen3-Embedding-8B (4096) or any other model.
EMBEDDING_TIMEOUT:  Per-request timeout in seconds. Default ``60``.
"""
from __future__ import annotations

import os
from typing import Iterable

import numpy as np
from openai import OpenAI, OpenAIError


DEFAULT_BASE_URL: str = "http://localhost:8001/v1"
DEFAULT_MODEL: str = "Qwen/Qwen3-Embedding-4B"
DEFAULT_API_KEY: str = "not-needed"
DEFAULT_DIM: int = 2560
DEFAULT_TIMEOUT: float = 60.0


class EmbeddingError(RuntimeError):
    """Raised when the embedding endpoint cannot be reached or returns malformed data."""


def _base_url() -> str:
    return os.environ.get("EMBEDDING_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def _model() -> str:
    return os.environ.get("EMBEDDING_MODEL", DEFAULT_MODEL)


def _api_key() -> str:
    return os.environ.get("EMBEDDING_API_KEY", DEFAULT_API_KEY)


def _dim() -> int:
    raw = os.environ.get("EMBEDDING_DIM")
    if raw is None:
        return DEFAULT_DIM
    try:
        value = int(raw)
        if value <= 0:
            raise ValueError
        return value
    except ValueError:
        return DEFAULT_DIM


def _timeout() -> float:
    raw = os.environ.get("EMBEDDING_TIMEOUT")
    if raw is None:
        return DEFAULT_TIMEOUT
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT


# Module-level dimension cache. Public callers reference EMBEDDING_DIM as a
# stable name; resolved once on import. Override via env var before import.
EMBEDDING_DIM: int = _dim()


_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(
            base_url=_base_url(),
            api_key=_api_key(),
            timeout=_timeout(),
        )
    return _client


def _post_embeddings(inputs: list[str]) -> list[list[float]]:
    """Call vLLM's OpenAI-compatible /v1/embeddings endpoint with batched input."""
    client = _get_client()
    try:
        response = client.embeddings.create(model=_model(), input=inputs)
    except OpenAIError as exc:
        raise EmbeddingError(
            f"vLLM embedding request failed at {_base_url()} "
            f"(model={_model()!r}): {exc}. Is the embedding vLLM instance up?"
        ) from exc

    data = response.data
    if len(data) != len(inputs):
        raise EmbeddingError(
            f"vLLM returned {len(data)} embeddings for {len(inputs)} inputs"
        )
    vectors = [item.embedding for item in data]
    for index, vector in enumerate(vectors):
        if len(vector) != EMBEDDING_DIM:
            raise EmbeddingError(
                f"Expected {EMBEDDING_DIM}-d vector, got {len(vector)} at index {index}. "
                f"Model {_model()!r} may not match EMBEDDING_DIM. "
                f"Set EMBEDDING_DIM env var to override."
            )
    return vectors


def embed_query(text: str) -> np.ndarray:
    """Embed a single query string into a float32 numpy vector.

    Returned vector is L2-normalized so cosine similarity equals inner product
    (matches FaissIndex/CuvsIndex with metric=inner_product).
    """
    if not isinstance(text, str):
        raise TypeError(f"embed_query expects str, got {type(text).__name__}")
    if not text.strip():
        raise ValueError("embed_query received empty text")

    vector = np.asarray(_post_embeddings([text])[0], dtype=np.float32)
    return _l2_normalize(vector)


def embed_documents(texts: list[str], batch_size: int = 32) -> np.ndarray:
    """Embed a list of documents into a (len(texts), EMBEDDING_DIM) float32 matrix.

    vLLM supports batched embedding requests natively, so each batch is one HTTP
    call (vs Ollama's per-prompt loop). Default batch_size=32 balances request
    size against vLLM's prefill scheduling.
    """
    if not isinstance(texts, list):
        raise TypeError(f"embed_documents expects list[str], got {type(texts).__name__}")
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    if not texts:
        return np.zeros((0, EMBEDDING_DIM), dtype=np.float32)

    for index, text in enumerate(texts):
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"embed_documents received empty/non-str at index {index}")

    rows: list[np.ndarray] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start:start + batch_size]
        vectors = _post_embeddings(batch)
        rows.append(np.asarray(vectors, dtype=np.float32))

    matrix = np.vstack(rows)
    return _l2_normalize_matrix(matrix)


def _l2_normalize(vector: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vector))
    if norm == 0.0:
        return vector
    return vector / norm


def _l2_normalize_matrix(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    return matrix / norms


def healthcheck() -> bool:
    """Quick liveness probe against the configured embedding endpoint."""
    try:
        client = _get_client()
        client.models.list()
    except OpenAIError:
        return False
    return True


__all__: Iterable[str] = (
    "EMBEDDING_DIM",
    "EmbeddingError",
    "embed_query",
    "embed_documents",
    "healthcheck",
)
