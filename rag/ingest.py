"""Catalog ingestion: chunk + embed + persist to a vector index.

This module reads ``catalog.json`` (the curated 18-dataset corpus), splits each entry
into 3-5 semantic chunks following the Vanna 2.0 DDL+doc+SQL pattern, embeds them via
the vLLM-served embedding model (``rag.embed`` -> Qwen3-Embedding-4B by default),
and writes the result to the active vector backend (cuVS on DGX Spark, FAISS-CPU as
dev fallback) plus a metadata sidecar.

Environment variables
---------------------
None directly; embedding behaviour is controlled by ``rag.embed``
(EMBEDDING_BASE_URL, EMBEDDING_MODEL, EMBEDDING_DIM). Vector backend is selected
by ``rag.vector_index.create_index()``.

Module is also runnable as a script::

    python -m rag.ingest <catalog.json> --index-dir <dir>
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
from pydantic import BaseModel, Field, ValidationError

from rag.embed import EMBEDDING_DIM, embed_documents
from rag.vector_index import CuvsIndex, create_index


_FAISS_FILENAME: str = "corpus.faiss"
_CUVS_FILENAME: str = "corpus.cuvs"


def _index_filename_for(index: object) -> str:
    """Pick the on-disk filename matching the index backend."""
    return _CUVS_FILENAME if isinstance(index, CuvsIndex) else _FAISS_FILENAME


class ColumnSpec(BaseModel):
    """A single column entry in a catalog dataset."""

    name: str
    type: str
    description: str


class JoinKey(BaseModel):
    """A join edge from one dataset to another."""

    to_dataset: str
    via_columns: list[str]
    method: str
    note: str | None = None


class SampleQuery(BaseModel):
    """An example intent + SQL pair for the dataset."""

    intent: str
    sql: str


class CatalogEntry(BaseModel):
    """Schema for a single dataset entry in catalog.json."""

    dataset_id: str
    dataset_name: str
    description: str
    primary_key: str
    core_columns: list[ColumnSpec] = Field(default_factory=list)
    gotchas: list[str] = Field(default_factory=list)
    join_keys: list[JoinKey] = Field(default_factory=list)
    sample_queries: list[SampleQuery] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    row_count_estimate: str | None = None
    update_cadence: str | None = None
    source_url: str | None = None


def build_catalog(catalog_path: str | Path) -> list[CatalogEntry]:
    """Load and validate ``catalog.json`` into a list of typed entries."""
    path = Path(catalog_path)
    if not path.exists():
        raise FileNotFoundError(f"Catalog not found at {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"Catalog must be a JSON array, got {type(raw).__name__}")

    entries: list[CatalogEntry] = []
    for index, item in enumerate(raw):
        try:
            entries.append(CatalogEntry.model_validate(item))
        except ValidationError as exc:
            raise ValueError(f"Catalog entry [{index}] failed validation:\n{exc}") from exc
    return entries


def _format_columns(columns: list[ColumnSpec]) -> str:
    if not columns:
        return "(no columns documented)"
    lines = [f"- {col.name} ({col.type}): {col.description}" for col in columns]
    return "\n".join(lines)


def _format_joins(joins: list[JoinKey]) -> str:
    if not joins:
        return "(no join edges declared)"
    lines = []
    for edge in joins:
        cols = ", ".join(edge.via_columns)
        note = f" Note: {edge.note}" if edge.note else ""
        lines.append(f"- joins to {edge.to_dataset} via [{cols}] using {edge.method}.{note}")
    return "\n".join(lines)


def _format_samples(samples: list[SampleQuery]) -> str:
    if not samples:
        return "(no sample queries)"
    lines = []
    for sample in samples:
        lines.append(f"- intent: {sample.intent}\n  sql: {sample.sql}")
    return "\n".join(lines)


def _split_columns(columns: list[ColumnSpec], max_per_chunk: int = 6) -> list[list[ColumnSpec]]:
    if not columns:
        return []
    return [columns[i : i + max_per_chunk] for i in range(0, len(columns), max_per_chunk)]


def chunk_catalog_entry(entry: dict[str, Any] | CatalogEntry) -> list[dict[str, Any]]:
    """Split a single catalog entry into 3-5 ~500-token chunks.

    Chunks emitted:
      1. ``overview``  - name, description, categories, row count, cadence, gotchas
      2. ``columns_<i>`` - one chunk per ~6-column group with type + description
      3. ``joins``     - join graph and primary key
      4. ``samples``   - intent + SQL examples (only if the entry has any)
    """
    if isinstance(entry, dict):
        validated = CatalogEntry.model_validate(entry)
    else:
        validated = entry

    chunks: list[dict[str, Any]] = []
    base_meta = {
        "dataset_id": validated.dataset_id,
        "dataset_name": validated.dataset_name,
        "categories": validated.categories,
        "source_url": validated.source_url,
    }

    overview_lines = [
        f"Dataset: {validated.dataset_name} (id: {validated.dataset_id})",
        f"Description: {validated.description}",
        f"Categories: {', '.join(validated.categories) if validated.categories else '(none)'}",
        f"Primary key: {validated.primary_key}",
        f"Row count estimate: {validated.row_count_estimate or '(unknown)'}",
        f"Update cadence: {validated.update_cadence or '(unknown)'}",
        f"Source URL: {validated.source_url or '(none)'}",
    ]
    if validated.gotchas:
        overview_lines.append("Field-level gotchas:")
        overview_lines.extend(f"- {item}" for item in validated.gotchas)
    chunks.append(
        {
            **base_meta,
            "chunk_id": f"{validated.dataset_id}__overview",
            "kind": "overview",
            "content": "\n".join(overview_lines),
        }
    )

    column_groups = _split_columns(validated.core_columns)
    for index, group in enumerate(column_groups):
        chunks.append(
            {
                **base_meta,
                "chunk_id": f"{validated.dataset_id}__columns_{index}",
                "kind": "columns",
                "content": (
                    f"Dataset: {validated.dataset_name} (id: {validated.dataset_id})\n"
                    f"Column group {index + 1}/{len(column_groups)}:\n"
                    f"{_format_columns(group)}"
                ),
            }
        )

    chunks.append(
        {
            **base_meta,
            "chunk_id": f"{validated.dataset_id}__joins",
            "kind": "joins",
            "content": (
                f"Dataset: {validated.dataset_name} (id: {validated.dataset_id})\n"
                f"Primary key: {validated.primary_key}\n"
                f"Join graph:\n{_format_joins(validated.join_keys)}"
            ),
        }
    )

    if validated.sample_queries:
        chunks.append(
            {
                **base_meta,
                "chunk_id": f"{validated.dataset_id}__samples",
                "kind": "samples",
                "content": (
                    f"Dataset: {validated.dataset_name} (id: {validated.dataset_id})\n"
                    f"Sample SQL patterns:\n{_format_samples(validated.sample_queries)}"
                ),
            }
        )

    return chunks


def ingest_corpus(catalog_path: str | Path, index_dir: str | Path) -> Path:
    """Load catalog, chunk, embed, and write a FAISS index to ``index_dir``.

    Returns the path to the persisted FAISS index file. The metadata sidecar lives
    next to it as ``<index>.meta.json``. Raises ``EmbeddingError`` if the
    embedding service is unreachable.
    """
    entries = build_catalog(catalog_path)
    chunks: list[dict[str, Any]] = []
    for entry in entries:
        chunks.extend(chunk_catalog_entry(entry))

    if not chunks:
        raise ValueError("No chunks produced from catalog; nothing to ingest.")

    texts = [chunk["content"] for chunk in chunks]
    vectors = embed_documents(texts)
    if vectors.shape != (len(chunks), EMBEDDING_DIM):
        raise RuntimeError(
            f"Embedding shape mismatch: got {vectors.shape}, expected ({len(chunks)}, {EMBEDDING_DIM})"
        )

    index = create_index(dim=EMBEDDING_DIM)
    index.add(vectors.astype(np.float32), chunks)

    out_dir = Path(index_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    index_path = out_dir / _index_filename_for(index)
    index.save(index_path)
    return index_path


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build the Urban-Dossier RAG corpus index.")
    parser.add_argument("catalog", type=Path, help="Path to catalog.json")
    parser.add_argument(
        "--index-dir",
        type=Path,
        default=Path("./index"),
        help="Directory to write the FAISS index and metadata sidecar (default: ./index)",
    )
    return parser


def main() -> None:
    """CLI entry point: ``python -m rag.ingest catalog.json --index-dir ./index/``."""
    args = _build_arg_parser().parse_args()
    index_path = ingest_corpus(args.catalog, args.index_dir)
    print(f"Wrote FAISS index to {index_path}")
    print(f"Metadata sidecar: {index_path}.meta.json")


if __name__ == "__main__":
    main()
