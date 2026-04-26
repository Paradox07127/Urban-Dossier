"""Urban-Dossier v2 RAG package.

Public API surface consumed by the ``urban-dossier-analyst`` agent skill and any
caller that needs to retrieve grounded NYC Open Data context.
"""
from rag.embed import embed_documents, embed_query
from rag.ingest import build_catalog, ingest_corpus
from rag.retrieve import RetrievedChunk, retrieve
from rag.vector_index import create_index, cuvs_available

__all__ = [
    "RetrievedChunk",
    "build_catalog",
    "create_index",
    "cuvs_available",
    "embed_documents",
    "embed_query",
    "ingest_corpus",
    "retrieve",
]
