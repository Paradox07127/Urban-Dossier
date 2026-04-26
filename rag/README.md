# Urban-Dossier v2 RAG

Retrieval-augmented context for the `urban_dossier_analyst` agent. The corpus is the
18 NYC Open Data datasets the project ships with; each dataset is decomposed into
3-5 chunks (overview / column groups / join graph / sample SQL) and indexed for
on-device semantic search.

This package is a clean break from the v1 hardcoded fallback. There is no legacy
6-pair lookup; every retrieval goes through the embedding pipeline.

## Architecture

```mermaid
flowchart LR
    Q[User query] --> EMB[embed_query<br/>Qwen/Qwen3-Embedding-4B<br/>via vLLM :8001]
    EMB --> VEC{Vector index}
    VEC -->|CuvsIndex<br/>brute_force, GPU| RR[rerank<br/>BAAI/bge-reranker-v2-m3<br/>CrossEncoder]
    VEC -.->|FaissIndex<br/>IndexFlatIP, CPU fallback| RR
    RR --> CTX[Top-k RetrievedChunk]
    CTX --> NEMO[Nemotron-3 30B-A3B<br/>via vLLM :8000<br/>NVFP4 / FlashInfer MoE]
    NEMO --> A[Agent answer]

    subgraph "DGX Spark - 128 GB unified memory"
        EMB
        VEC
        RR
        NEMO
    end
```

A single vLLM serving stack hosts both the LLM (Nemotron-3 30B on `:8000`) and the
embedding model (Qwen3-Embedding-4B on `:8001`). Both share the GB10 unified
memory pool — no inter-process copies between embedding generation and LLM
context assembly.

## Setup

This package targets the DGX Spark runtime. The instructions below assume you
have already followed the parent `Urban-Dossier/README.md` Quick Start through
the Nemotron vLLM step.

### 1. Start the embedding vLLM instance (Qwen3-Embedding-4B)

```bash
# Download the model once (skip if cached)
huggingface-cli download Qwen/Qwen3-Embedding-4B

# Start a second vLLM instance on :8001 dedicated to embeddings.
# Add an `embedding` profile to scripts/vllm/start_vllm.sh, then:
bash scripts/vllm/start_vllm.sh --profile embedding &
```

Verify:

```bash
curl -s http://localhost:8001/v1/models | jq '.data[].id'
# Expect: "Qwen/Qwen3-Embedding-4B"
```

### 2. Install Python dependencies

```bash
cd Urban-Dossier
python -m venv .venv && source .venv/bin/activate
pip install -r rag/requirements.txt

# Make CuvsIndex the GPU default (preferred on DGX Spark):
pip install cuvs-cu13            # try pip wheel first
# or:
conda install -c rapidsai cuvs   # fallback if no aarch64 pip wheel
```

The first import of the reranker downloads `BAAI/bge-reranker-v2-m3` (~600 MB)
into `~/.cache/huggingface/hub/`.

## CLI usage

### Ingest

```bash
cd Urban-Dossier
PYTHONPATH=. python -m rag.ingest rag/catalog.json --index-dir rag/index
```

Outputs (filename depends on the active backend, decided at index build time):

- `rag/index/corpus.cuvs` + `corpus.cuvs.vectors.npy` + `corpus.cuvs.meta.json` — cuVS path
- `rag/index/corpus.faiss` + `corpus.faiss.meta.json` — FAISS-CPU fallback path

The selected backend is logged at INFO level (`VectorIndex backend: ...`).

### Query (from Python)

```python
from rag import retrieve

hits = retrieve(
    "weekend rodent complaints in Brooklyn",
    dataset_filter=["safety_311", "safety_rodent"],
    top_k=5,
)
for hit in hits:
    print(f"[{hit.score:.3f}] {hit.dataset_id} :: {hit.chunk_id}")
    print(hit.content[:160])
```

`dataset_filter=None` searches the whole 18-dataset corpus. Use the filter when
the planning step has already disambiguated which dataset the question targets.

### Tests

```bash
cd Urban-Dossier
PYTHONPATH=. python -m pytest rag/tests/ -q
```

The smoke tests stub the embedding HTTP layer via `unittest.mock`, so they pass
without a live embedding server.

## Integration with `urban_dossier_analyst`

The agent skill (in `Urban-Dossier/skills/urban_dossier_analyst/`) consumes the
public API:

```python
from rag import retrieve, RetrievedChunk

def plan_step(user_question: str, candidate_datasets: list[str]) -> str:
    chunks: list[RetrievedChunk] = retrieve(
        user_question,
        dataset_filter=candidate_datasets,
        top_k=5,
        rerank=True,
    )
    return "\n\n---\n".join(c.content for c in chunks)
```

Contract guarantees:

- Returns at most `top_k` results.
- Each result carries `dataset_id`, `chunk_id`, `score`, `content`, and full `metadata`.
- The `content` field is the raw chunk text, suitable for direct prompt injection.
- `score` is a comparable similarity (cosine after the vector index, reranker
  logit after rerank).

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `EMBEDDING_BASE_URL` | `http://localhost:8001/v1` | OpenAI-compatible base URL of the embedding vLLM instance |
| `EMBEDDING_MODEL` | `Qwen/Qwen3-Embedding-4B` | Model id served by the embedding vLLM instance |
| `EMBEDDING_API_KEY` | `not-needed` | API key (vLLM ignores) |
| `EMBEDDING_DIM` | `2560` | Vector dimension; override if you swap models (Qwen3-Embedding-0.6B = 1024, 8B = 4096) |
| `EMBEDDING_TIMEOUT` | `60` | Seconds per embedding HTTP call |
| `RERANKER_MODEL` | `BAAI/bge-reranker-v2-m3` | HF id for the cross-encoder |
| `RERANKER_DEVICE` | `cpu` | torch device for the reranker |
| `RAG_INDEX_DIR` | `./index` | Directory containing the vector index |
| `RAG_INDEX_FILENAME` | auto | `corpus.cuvs` if cuVS available, else `corpus.faiss` |
| `RAG_VECTOR_OVERSAMPLE` | `20` | Pre-rerank candidate count |
| `RAG_PREFER_GPU` | `1` | Set `0` to force FAISS-CPU even when cuVS is importable |

## NVIDIA stack components called out for the judges

- **NVIDIA GB10 Grace Blackwell** — target SoC; FP4 tensor cores accelerate the
  Nemotron NVFP4 weights and the Qwen embedding model on a single chip.
- **128 GB unified memory** — lets the 30B Nemotron weights, the embedding model,
  the cuVS index, and the cuDF dataset cache coexist in one address space (273
  GB/s LPDDR5X bandwidth, no PCIe transfers between CPU and GPU phases).
- **vLLM (single stack, two instances)** — `:8000` serves Nemotron-3 30B-A3B
  (NVFP4, FlashInfer MoE), `:8001` serves Qwen3-Embedding-4B. One inference
  engine, one set of NVIDIA optimizations.
- **NVIDIA cuVS** (`cuvs-cu13`) — GPU `brute_force` exact-KNN index in
  `vector_index.py`, the default backend whenever the package is importable.
  FAISS-CPU is kept only as a dev/Mac fallback.
- **NVIDIA NemoClaw / OpenClaw** — sandbox runtime for the agent skill that
  consumes this RAG layer.
- **BAAI/bge-reranker-v2-m3** — open-source CrossEncoder reranker (Apache 2.0,
  no telemetry, runs on CPU or `cuda:0` per `RERANKER_DEVICE`).
