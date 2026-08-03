# Urban Dossier

### *A Local is All You Need*

Urban Dossier is a local-first NYC neighborhood analysis system. It combines
deterministic scoring over NYC Open Data, an offline MapLibre interface, and a
locally served Nemotron model for grounded narrative analysis.

The project began at Spark Hack NYC 2026 and now supports multiple deployment
profiles instead of assuming one NVIDIA machine.

## Deployment profiles

| Profile | Role | Inference | Data/vector path | Status |
| --- | --- | --- | --- | --- |
| `cuda-x86` | Primary production/workstation | Docker vLLM + Nemotron NVFP4 | DuckDB/Parquet; optional cuVS | Validated 2026-08-02 |
| `dgx-spark` | GB10 deployment | DGX-specific vLLM launcher | RAPIDS/cuVS-capable | Preserved independently |
| `mac` | Development and UI work | Optional MLX/llama.cpp endpoint | DuckDB + CPU vector index | Development profile |
| `test` | CI/contract checks | Stub or disabled | Small fixtures | Planned baseline |

Do not copy performance parameters between the x86 workstation and DGX Spark.
They have different memory architecture, kernels, container paths, and tuning
history.

- x86 workstation: [`DEPLOY_WORKSTATION.md`](DEPLOY_WORKSTATION.md)
- DGX Spark: [`DEPLOY_DGX_SPARK.md`](DEPLOY_DGX_SPARK.md)
- Shared dataset contract: [`DATA_ARCHITECTURE.md`](DATA_ARCHITECTURE.md)
- architecture and roadmap: [`PROJECT_PLAN.md`](PROJECT_PLAN.md)

## Current x86 workstation stack

```text
Browser
  -> Node / MapLibre (:3456)
  -> FastAPI (:8090)
       |-> DuckDB + Parquet/H3 deterministic analysis
       |-> OpenClaw Gateway (:18789, loopback SSH forward)
             -> urban-dossier dedicated agent
             -> OpenShell inference route
             -> vLLM (:8000)
             -> NVIDIA Nemotron-3-Nano-30B-A3B-NVFP4
```

Current validated runtime:

- NemoClaw `0.0.100`, OpenShell `0.0.85`, OpenClaw `2026.7.1`;
- vLLM `0.23.0`, digest-pinned Docker image;
- Node.js 24 and Python 3.12 environment managed with `uv`;
- FastAPI runs as a user-level systemd service;
- the production agent uses a minimal workspace, no Skills, and only the
  `session_status` tool.

The Gateway remains inside the NemoClaw-managed OpenShell container. FastAPI's
persistent HTTP client removes per-turn CLI startup without bypassing the
OpenShell security boundary.

## Quick start: x86 workstation

The complete first-time procedure is in
[`DEPLOY_WORKSTATION.md`](DEPLOY_WORKSTATION.md). For an already-onboarded
workstation:

```bash
cd /mnt/data/Urban-Dossier

# LLM only. The embedding service is optional and is not part of the current
# frontend/backend critical path.
docker compose \
  --env-file /mnt/data/urban-dossier/runtime/gpu.env \
  -f deploy/compose.gpu.yml up -d llm

# Reconcile the dedicated agent and restore its Gateway forward/token.
bash scripts/configure_openclaw_agent.sh

# Start the persistent backend.
systemctl --user enable --now urban-dossier-backend.service

# Validate all active components.
bash scripts/health-check.sh
```

Frontend development/build:

```bash
npm install
npm --prefix interactive-map-explorer install
npm --prefix interactive-map-explorer run build
node server.js
```

Open `http://<workstation-lan-ip>:3456`. The service binds for LAN use; do not
use `127.0.0.1` on a different computer unless an SSH local forward is active.

## Data

The repository catalog currently defines 18 source datasets across safety,
transit, amenities, buildings, and PLUTO location reference data.

```bash
# Workstation production location
bash scripts/download_datasets.sh /mnt/data/urban-dossier/datasets/raw
```

Processed Parquet files used by the backend live under `data/ready/` in the
repository. Raw downloads live outside Git on the second SSD. Dataset source
IDs, URLs, filenames, and resume behavior are encoded in
[`scripts/download_datasets.sh`](scripts/download_datasets.sh); semantic RAG
metadata is in [`rag/catalog.json`](rag/catalog.json).

The overview map additionally needs NYC Planning's official NTA 2020 boundary
and generated Gold score layers:

```bash
# Download and validate release 26B boundary + official metadata.
bash scripts/maps/download_nta_2020.sh

# Build the four H3 layers, then aggregate them into NTA display layers.
.venv/bin/python backend/scripts/build_overview_tiles.py \
  --ready-root data/ready --overview-root data/cache/overview
.venv/bin/python backend/scripts/build_overview_nta.py \
  --nta-path data/boundaries/nta_2020.geojson \
  --overview-root data/cache/overview
```

The validated workstation snapshot contains 262 boundary features, four H3 r8
layers with 1,171-1,232 cells, and four directly scored NTA layers with
248-251 zones. The remaining 11-14 NTAs have no directly scored H3 cell and
must be presented as no-data unless an explicitly marked imputation policy is
introduced. All files under `data/` are reproducible local artifacts and are
ignored by Git. See [`DATA_ARCHITECTURE.md`](DATA_ARCHITECTURE.md) for the
publication contract and provenance rules.

## Dedicated agent

The production `urban-dossier` agent receives already-computed score/evidence
context from FastAPI and performs text analysis only. It does not query raw
files or execute arbitrary tools.

- declarative roster: [`deploy/openclaw/agents.yaml`](deploy/openclaw/agents.yaml)
- minimal workspace: [`deploy/openclaw/urban-dossier/`](deploy/openclaw/urban-dossier/)
- setup/recovery guide: [`deploy/openclaw/README.md`](deploy/openclaw/README.md)
- reconciliation script: [`scripts/configure_openclaw_agent.sh`](scripts/configure_openclaw_agent.sh)

The broader skills under `skills/` remain development/legacy capabilities.
They are deliberately not loaded by the production dedicated agent. Future
data onboarding should be exposed as a separately authorized job or agent,
not added back into the user-facing analysis prompt.

## vLLM workstation profile

The x86 production source of truth is
[`deploy/compose.gpu.yml`](deploy/compose.gpu.yml), with overrides copied from
[`deploy/gpu.env.example`](deploy/gpu.env.example).

```text
LLM_GPU_MEMORY_UTILIZATION=0.45
LLM_MAX_MODEL_LEN=32768
LLM_MAX_NUM_SEQS=8
LLM_MAX_BATCHED_TOKENS=32768
LLM_KV_CACHE_DTYPE=fp8
LLM_MOE_BACKEND=flashinfer_cutlass
```

On the RTX PRO 6000 Blackwell workstation this uses about 40.8 GiB at steady
state and leaves a 1,300,889-token KV cache. Reducing batched tokens to 8192
lowered measured throughput, so 32768 remains the default. FP8 KV is retained
for performance, but must be compared with BF16 on a fixed Urban Dossier answer
quality set before a final production release.

The scripts in `scripts/vllm/` are the separate DGX Spark launch profile. See
[`scripts/vllm/README.md`](scripts/vllm/README.md) before using them.

## RAG and vector index status

RAG is not currently on the dedicated agent's critical path. Its index adapter
supports cuVS and FAISS:

- `cuVS`: preferred for a validated CUDA deployment and larger indexes;
- `faiss-cpu`: valid Mac/test/small-corpus fallback, not required merely to run
  the current frontend, deterministic backend, or dedicated agent.

The optional embedding vLLM service is declared in `deploy/compose.gpu.yml` but
is intentionally not started by the default LLM-only command. See
[`rag/README.md`](rag/README.md) before enabling it.

## Architecture rules

- React displays state; it must not own scoring rules.
- Node serves assets, tiles, and proxy routes; it must not recompute scores.
- FastAPI is the application boundary and source of analysis truth.
- DuckDB/Parquet/H3 own deterministic metrics and evidence.
- The LLM explains supplied evidence; it does not invent or recalculate scores.
- OpenShell remains the policy/network isolation boundary for the agent.
- GPU libraries are adapters. Domain behavior must remain testable without a
  CUDA device.

Known architectural follow-up: `/api/agent/chat` now uses the dedicated agent,
but it should ultimately converge with the structured `/api/agent/ask`
contract so session, trace, evidence, and artifacts have one public API.

## Service endpoints

| Service | Endpoint | Exposure |
| --- | --- | --- |
| Frontend/Node | `:3456` | LAN during development/demo |
| FastAPI | `127.0.0.1:8090` | proxied by Node |
| vLLM LLM | `:8000` | loopback/Docker bridge policy |
| OpenClaw Gateway | `127.0.0.1:18789` | authenticated loopback forward |
| vLLM embeddings | `127.0.0.1:8001` | optional, currently stopped |

## Configuration

- workstation backend runtime: [`deploy/backend.env.example`](deploy/backend.env.example)
- workstation GPU runtime: [`deploy/gpu.env.example`](deploy/gpu.env.example)
- portable backend development sample: [`backend/.env.example`](backend/.env.example)
- persistent service: [`deploy/systemd/urban-dossier-backend.service`](deploy/systemd/urban-dossier-backend.service)

Runtime secrets and downloaded model/data files belong under
`/mnt/data/urban-dossier/`, not in Git. The OpenClaw Gateway bearer token is
stored in a mode-`0600` runtime file and must never be copied into documentation
or a systemd unit.

## Verification

```bash
# Agent implementation tests
PYTHONPATH=backend/src .venv/bin/pytest -q \
  backend/tests/test_agent_service_nemoclaw.py

# Authenticated OpenResponses route, without printing the token
.venv/bin/python scripts/test_openclaw_gateway.py

# Compose resolution
docker compose \
  --env-file /mnt/data/urban-dossier/runtime/gpu.env \
  -f deploy/compose.gpu.yml config
```

## License

MIT
