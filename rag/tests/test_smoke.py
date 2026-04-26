"""Bare-minimum smoke tests for the Urban-Dossier RAG pipeline.

These tests run on Mac (CPU-only, ARM or x86). They do NOT exercise the cuVS
GPU path. Run with::

    PYTHONPATH=Urban-Dossier python -m pytest Urban-Dossier/rag/tests/ -q

Each test isolates itself: the embedding test stubs the OpenAI client so no
live vLLM embedding server is required.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

import rag.embed as embed_module
from rag.embed import EMBEDDING_DIM, embed_query
from rag.ingest import CatalogEntry, build_catalog, chunk_catalog_entry
from rag.vector_index import FaissIndex


CATALOG_PATH = Path(__file__).resolve().parents[1] / "catalog.json"


def _fake_embedding_response(vector: list[float]) -> SimpleNamespace:
    """Mimic the OpenAI SDK shape: response.data[0].embedding -> list[float]."""
    return SimpleNamespace(data=[SimpleNamespace(embedding=vector)])


def test_embed_query_returns_normalized_float32_vector() -> None:
    """Mock the vLLM embedding endpoint; assert embed_query returns a normalized vector.

    Dimension is whatever ``EMBEDDING_DIM`` resolves to at module load time —
    2560 for the default Qwen3-Embedding-4B, overridable via ``EMBEDDING_DIM``
    env var. The test stays dim-agnostic so swapping models doesn't break it.
    """
    fake_vector = [0.01 * i for i in range(EMBEDDING_DIM)]
    fake_client = MagicMock()
    fake_client.embeddings.create.return_value = _fake_embedding_response(fake_vector)
    embed_module._client = fake_client
    try:
        result = embed_query("how many open class C violations in the Bronx?")
    finally:
        embed_module._client = None
    fake_client.embeddings.create.assert_called_once()
    assert isinstance(result, np.ndarray)
    assert result.shape == (EMBEDDING_DIM,)
    assert result.dtype == np.float32
    norm = float(np.linalg.norm(result))
    assert norm == pytest.approx(1.0, abs=1e-5)


def test_faiss_index_add_search_roundtrip() -> None:
    """Add random vectors and confirm search returns the seeded top match."""
    rng = np.random.default_rng(seed=42)
    dim = 1024
    n = 16
    vectors = rng.standard_normal((n, dim)).astype(np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    metadata = [{"chunk_id": f"c{i}", "dataset_id": f"ds_{i % 4}", "content": f"chunk {i}"} for i in range(n)]

    index = FaissIndex(dim=dim)
    index.add(vectors, metadata)

    target_idx = 7
    results = index.search(vectors[target_idx], top_k=3)
    assert results, "search returned no results"
    top_score, top_meta = results[0]
    assert top_meta["chunk_id"] == f"c{target_idx}"
    assert top_score == pytest.approx(1.0, abs=1e-3)


def test_faiss_index_filter_by_dataset_id() -> None:
    """Post-filtering on dataset_id should respect the requested membership set."""
    rng = np.random.default_rng(seed=7)
    dim = 1024
    vectors = rng.standard_normal((20, dim)).astype(np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    metadata = [{"chunk_id": f"c{i}", "dataset_id": f"ds_{i % 5}"} for i in range(20)]

    index = FaissIndex(dim=dim)
    index.add(vectors, metadata)

    query = vectors[3]
    results = index.search(query, top_k=5, filter={"dataset_id": ["ds_0", "ds_1"]})
    assert results, "filtered search returned no results"
    for _, meta in results:
        assert meta["dataset_id"] in {"ds_0", "ds_1"}


def test_faiss_index_save_load_roundtrip(tmp_path: Path) -> None:
    """Persist a FAISS index and reload it; metadata must survive intact."""
    dim = 1024
    rng = np.random.default_rng(seed=1)
    vectors = rng.standard_normal((4, dim)).astype(np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    metadata = [{"chunk_id": f"c{i}", "dataset_id": "ds"} for i in range(4)]

    index = FaissIndex(dim=dim)
    index.add(vectors, metadata)
    path = tmp_path / "corpus.faiss"
    index.save(path)

    reloaded = FaissIndex.load(path)
    assert len(reloaded) == 4
    results = reloaded.search(vectors[2], top_k=1)
    assert results[0][1]["chunk_id"] == "c2"


def test_catalog_json_schema_validates() -> None:
    """All catalog.json entries must satisfy the CatalogEntry pydantic schema."""
    assert CATALOG_PATH.exists(), f"Expected catalog.json at {CATALOG_PATH}"
    raw = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    assert isinstance(raw, list)
    assert len(raw) >= 17, f"Catalog must cover at least 17 datasets; got {len(raw)}"
    seen_ids: set[str] = set()
    for item in raw:
        entry = CatalogEntry.model_validate(item)
        assert entry.dataset_id not in seen_ids, f"duplicate dataset_id: {entry.dataset_id}"
        seen_ids.add(entry.dataset_id)
        assert entry.core_columns, f"dataset {entry.dataset_id} has no documented columns"


def test_catalog_chunking_emits_3_to_5_chunks_per_entry() -> None:
    """Each catalog entry should produce 3-5 chunks (overview + columns + joins + samples)."""
    entries = build_catalog(CATALOG_PATH)
    for entry in entries:
        chunks = chunk_catalog_entry(entry)
        assert 3 <= len(chunks) <= 6, (
            f"{entry.dataset_id} produced {len(chunks)} chunks; expected 3-5 (6 allowed for big col groups)"
        )
        kinds = {chunk["kind"] for chunk in chunks}
        assert "overview" in kinds
        assert "joins" in kinds
        for chunk in chunks:
            assert chunk["dataset_id"] == entry.dataset_id
            assert chunk["chunk_id"].startswith(entry.dataset_id)
