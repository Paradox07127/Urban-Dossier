# x86 NVIDIA Workstation Deployment

This is the validated production profile for the current Urban Dossier
workstation. It is independent from the GB10 configuration in
[`DEPLOY_DGX_SPARK.md`](DEPLOY_DGX_SPARK.md); do not copy GPU tuning values
between the two profiles.

Dataset layers, cleaning semantics, manifests, and publication gates are shared
with Mac and DGX Spark; see [`DATA_ARCHITECTURE.md`](DATA_ARCHITECTURE.md).

Validated 2026-08-02:

- x86_64 Linux and RTX PRO 6000 Blackwell Workstation Edition;
- Docker Engine + NVIDIA Container Toolkit;
- vLLM 0.23.0 in a digest-pinned container;
- NemoClaw 0.0.100, OpenShell 0.0.85, OpenClaw 2026.7.1;
- Python 3.12 with `uv`, Node.js 24;
- repository at `/mnt/data/Urban-Dossier`;
- mutable state at `/mnt/data/urban-dossier` on the second SSD.
- 18/18 raw datasets, 44/44 ready Parquet files, and NTA 2020 release 26B
  overview layers available to the frontend.

## 1. Storage layout

```text
/mnt/data/Urban-Dossier/                 Git checkout
/mnt/data/urban-dossier/datasets/raw/    downloaded source data
/mnt/data/urban-dossier/models/llm/      Nemotron model mount
/mnt/data/urban-dossier/models/embedding optional Qwen embedding model
/mnt/data/urban-dossier/hf-cache/         shared Hugging Face cache
/mnt/data/urban-dossier/runtime/          env files and Gateway token
```

Create the mutable directories once:

```bash
mkdir -p /mnt/data/urban-dossier/{datasets/raw,models/llm,models/embedding,hf-cache,runtime}
chmod 700 /mnt/data/urban-dossier/runtime
```

## 2. Clone and Python/Node environments

```bash
cd /mnt/data
gh repo clone Paradox07127/Urban-Dossier
cd /mnt/data/Urban-Dossier

uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r backend/requirements.txt pytest

npm install
npm --prefix interactive-map-explorer install
```

`uv` is the preferred host Python environment manager. Conda is not required
for the backend or vLLM containers. A DGX-specific RAPIDS/cuVS install may still
use conda if that platform's wheel support requires it.

## 3. Download data

```bash
bash scripts/download_datasets.sh /mnt/data/urban-dossier/datasets/raw
```

The download catalog contains 18 source datasets. The backend consumes prepared
Parquet data from `/mnt/data/Urban-Dossier/data/ready`; raw CSV completion does
not by itself create those prepared files.

Audit the entire CSV snapshot with a strict full-file parse before cleaning:

```bash
.venv/bin/python scripts/audit_datasets.py \
  /mnt/data/urban-dossier/datasets/raw \
  --output /mnt/data/urban-dossier/datasets/manifests/raw-audit.json
```

The workstation data plane is deliberately layered:

- immutable source CSV plus a download/audit manifest for provenance;
- typed, normalized ZSTD Parquet with H3/ZIP keys for analytical facts;
- compact H3/ZIP scores, trends, and baselines for online Agent queries;
- DuckDB for the always-on query path and an isolated RAPIDS container for
  optional GPU batch exploration.

Parquet is the canonical analytical interchange format, not the Agent memory
format. The Agent should query compact score/evidence tables first and drill
into normalized facts only when it needs supporting rows. Dataset definitions,
column meanings, update cadence, and source URLs remain in `rag/catalog.json`.

The validated RAPIDS workstation profile is
`nvcr.io/nvidia/rapidsai/base:26.06-cuda13-py3.12`. It reads the generated ZSTD
Parquet files with cuDF on the RTX PRO 6000 Blackwell. Keep RAPIDS outside the
backend venv so CUDA libraries do not increase the FastAPI/OpenClaw resident
footprint. Dask-RAPIDS and GPUDirect Storage are not justified for the current
single-GPU, sub-gigabyte ready layer; introduce them only after profiling a
repeatable multi-gigabyte I/O or out-of-core bottleneck.

Before publication, normalize row groups and validate every file:

```bash
.venv/bin/python scripts/optimize_parquet.py /path/to/data/ready
.venv/bin/python scripts/validate_ready_parquet.py /path/to/data/ready \
  --output /mnt/data/urban-dossier/datasets/manifests/ready-audit.json
```

Download the official NYC Planning boundary and build the Gold overview layer
after the ready publication passes validation:

```bash
cd /mnt/data/Urban-Dossier

bash scripts/maps/download_nta_2020.sh

.venv/bin/python backend/scripts/build_overview_tiles.py \
  --ready-root /mnt/data/Urban-Dossier/data/ready \
  --overview-root /mnt/data/Urban-Dossier/data/cache/overview

.venv/bin/python backend/scripts/build_overview_nta.py \
  --nta-path /mnt/data/Urban-Dossier/data/boundaries/nta_2020.geojson \
  --overview-root /mnt/data/Urban-Dossier/data/cache/overview
```

`download_nta_2020.sh` publishes atomically only after validating the GeoJSON,
262 unique NTA codes, polygon geometry, ArcGIS layer definition, and official
metadata PDF. Its manifest records release 26B, source URLs, byte sizes, field
names, and SHA-256 hashes. These generated files are deliberately ignored by
Git and must be reproduced on each deployment host.

## 4. Configure and start vLLM

```bash
cp deploy/gpu.env.example /mnt/data/urban-dossier/runtime/gpu.env

docker compose \
  --env-file /mnt/data/urban-dossier/runtime/gpu.env \
  -f deploy/compose.gpu.yml pull llm

docker compose \
  --env-file /mnt/data/urban-dossier/runtime/gpu.env \
  -f deploy/compose.gpu.yml up -d llm
```

The LLM model directory must contain the checkpoint plus
`nano_v3_reasoning_parser.py` before startup. Current defaults:

```text
LLM_GPU_MEMORY_UTILIZATION=0.45
LLM_MAX_MODEL_LEN=32768
LLM_MAX_NUM_SEQS=8
LLM_MAX_BATCHED_TOKENS=32768
LLM_KV_CACHE_DTYPE=fp8
LLM_MOE_BACKEND=flashinfer_cutlass
```

Observed on this workstation:

| configuration | C1 TTFT P50 | C4 output throughput | steady VRAM |
| --- | ---: | ---: | ---: |
| 0.70 / batch 32768 | 142 ms | 669 tok/s | 68.8 GiB |
| **0.45 / batch 32768** | **143 ms** | **672 tok/s** | **40.8 GiB** |
| 0.45 / batch 8192 | 151 ms | 640 tok/s | about 40 GiB |

The selected profile leaves 1,300,889 KV-cache tokens and a theoretical 39.7
concurrent 32K requests. FP8 startup warns that the checkpoint does not provide
all q/prob scaling factors; run a BF16 quality A/B before final release rather
than treating the memory benchmark as an accuracy result.

Verify:

```bash
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/v1/models | python3 -m json.tool
```

The optional `embeddings` service is not currently part of the frontend/backend
critical path. Start it only after mounting its model and following
[`rag/README.md`](rag/README.md):

```bash
docker compose \
  --env-file /mnt/data/urban-dossier/runtime/gpu.env \
  -f deploy/compose.gpu.yml up -d embeddings
```

## 5. First-time NemoClaw/OpenClaw onboarding

This step is interactive because the compatible inference endpoint and provider
must be selected. Run it only when the sandbox does not exist or when rebuilding
from scratch:

```bash
nemoclaw onboard \
  --name urban-dossier-agent \
  --agent openclaw \
  --agents deploy/openclaw/agents.yaml
```

Choose the compatible endpoint backed by the local vLLM server and model
`nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4`. Direct GPU access inside the
sandbox is not needed: inference remains in the dedicated vLLM container.

After onboard, always reconcile the application-specific policy/workspace:

```bash
bash scripts/configure_openclaw_agent.sh
```

This applies the roster, uploads only `AGENTS.md` and `SOUL.md`, selects the
dedicated agent as the sandbox default, disables Tool Search and irrelevant
tools, enables authenticated OpenResponses, recovers the loopback forward, and
writes the Gateway token to a mode-0600 runtime file.

See [`deploy/openclaw/README.md`](deploy/openclaw/README.md) for the routing
workaround and recovery details.

## 6. Install the persistent FastAPI service

```bash
cp deploy/backend.env.example /mnt/data/urban-dossier/runtime/backend.env
chmod 600 /mnt/data/urban-dossier/runtime/backend.env

mkdir -p ~/.config/systemd/user
cp deploy/systemd/urban-dossier-backend.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now urban-dossier-backend.service
```

The unit runs `nemoclaw ... recover` and refreshes the protected Gateway token
before starting Uvicorn on `127.0.0.1:8090`.

Verify:

```bash
systemctl --user --no-pager status urban-dossier-backend.service
curl -fsS http://127.0.0.1:8090/api/agent/status | python3 -m json.tool
```

## 7. Frontend

```bash
npm --prefix interactive-map-explorer run build
node server.js
```

Open `http://<workstation-lan-ip>:3456` from another LAN device. `127.0.0.1`
always refers to the device running the browser, not the workstation.

## 8. Validation

```bash
PYTHONPATH=backend/src .venv/bin/pytest -q \
  backend/tests/test_agent_service_nemoclaw.py

.venv/bin/python scripts/test_openclaw_gateway.py
bash scripts/health-check.sh

# Data plane and frontend proxy must both report an available overview.
curl -fsS http://127.0.0.1:8090/api/coverage | python3 -m json.tool
curl -fsS 'http://127.0.0.1:3456/api/overview/nta-geojson?tag=general' \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(len(d["features"]), d["metadata"])'
```

The validated 26B snapshot returns `overview_ready: true`, all four overview
categories, and 251 directly scored features for the general NTA layer.
Safety and transit currently return 248; amenities returns 251. Unscored zones
are a coverage condition, not a failed boundary download.

Expected Agent status includes:

```json
{
  "enabled": true,
  "backend": "nemoclaw",
  "transport": "gateway",
  "agent_id": "urban-dossier"
}
```

## 9. Recovery and upgrades

After a reboot or stopped sandbox:

```bash
nemoclaw urban-dossier-agent recover
systemctl --user restart urban-dossier-backend.service
```

After a NemoClaw/OpenClaw rebuild:

```bash
nemoclaw urban-dossier-agent rebuild --yes
bash scripts/configure_openclaw_agent.sh
systemctl --user restart urban-dossier-backend.service
```

Before changing a container digest or vLLM version, record the old digest, pull
the candidate, run the 8K C1/C4 benchmark and real Agent smoke test, then update
`deploy/compose.gpu.yml`. Do not use floating `latest` in the production file.

## 10. Shutdown

```bash
systemctl --user stop urban-dossier-backend.service
nemoclaw urban-dossier-agent stop
docker compose \
  --env-file /mnt/data/urban-dossier/runtime/gpu.env \
  -f deploy/compose.gpu.yml stop
```

These operations preserve models, datasets, sandbox state, and Docker volumes.
