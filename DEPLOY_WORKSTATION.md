# x86 NVIDIA Workstation Deployment

This is the validated production profile for the current Urban Dossier
workstation. It is independent from the GB10 configuration in
[`DEPLOY_DGX_SPARK.md`](DEPLOY_DGX_SPARK.md); do not copy GPU tuning values
between the two profiles.

Dataset layers, cleaning semantics, manifests, and publication gates are shared
with Mac and DGX Spark; see [`DATA_ARCHITECTURE.md`](DATA_ARCHITECTURE.md).

Base stack validated 2026-08-02; inference image and full Nano agent path
revalidated 2026-08-12:

- x86_64 Linux and RTX PRO 6000 Blackwell Workstation Edition;
- Docker Engine + NVIDIA Container Toolkit;
- vLLM 0.27.1 in a digest-pinned container (updated 2026-08-12 for the
  Nemotron 3.5 Lightning candidate; rollback digest for 0.23.0 recorded in
  `deploy/compose.gpu.yml`);
- NemoClaw 0.0.100, OpenShell 0.0.85, OpenClaw 2026.7.1;
- Python 3.12 with `uv`, Node.js 24;
- repository at `/mnt/data/Urban-Dossier`;
- mutable state at `/mnt/data/urban-dossier-state` on the second SSD.
- 18/18 raw datasets, 44/44 ready Parquet files, and NTA 2020 release 26B
  overview layers available to the frontend.

## 1. Storage layout

```text
/mnt/data/Urban-Dossier/                 Git checkout
/mnt/data/urban-dossier-state/datasets/raw/    downloaded source data
/mnt/data/urban-dossier-state/models/llm/      Nemotron model mount
/mnt/data/urban-dossier-state/models/embedding optional Qwen embedding model
/mnt/data/urban-dossier-state/hf-cache/         shared Hugging Face cache
/mnt/data/urban-dossier-state/runtime/          env files and Gateway token
```

Create the mutable directories once:

```bash
mkdir -p /mnt/data/urban-dossier-state/{datasets/raw,models/llm,models/embedding,hf-cache,runtime}
chmod 700 /mnt/data/urban-dossier-state/runtime
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
bash scripts/download_datasets.sh /mnt/data/urban-dossier-state/datasets/raw
```

The download catalog contains 18 source datasets. The backend consumes prepared
Parquet data from `/mnt/data/Urban-Dossier/data/ready`; raw CSV completion does
not by itself create those prepared files.

Audit the entire CSV snapshot with a strict full-file parse before cleaning:

```bash
.venv/bin/python scripts/audit_datasets.py \
  /mnt/data/urban-dossier-state/datasets/raw \
  --output /mnt/data/urban-dossier-state/datasets/manifests/raw-audit.json
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
  --output /mnt/data/urban-dossier-state/datasets/manifests/ready-audit.json
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

Build the agent-tool artifacts. All are reproducible local files, ignored by
Git, and must be generated on each deployment host:

```bash
# Pedestrian routing graph for POST /api/isochrone.
# Needs the build-only extras: uv pip install --python .venv/bin/python \
#   -r backend/preprocess_requirements.txt
.venv/bin/python backend/scripts/build_walking_graph.py \
  --pbf /mnt/data/urban-dossier-state/maps/source/NewYork.osm.pbf \
  --out /mnt/data/urban-dossier-state/maps/walk

# Empirical count->score curves for POST /api/simulate.
.venv/bin/python backend/scripts/fit_intervention_elasticity.py \
  --ready-root /mnt/data/Urban-Dossier/data/ready \
  --out /mnt/data/Urban-Dossier/data/cache/simulation/elasticity.json
```

Build the per-building choropleth. Three passes, run in order; the last needs
`tippecanoe` on PATH (`apt-get install tippecanoe`):

```bash
# 0. The basemap itself. Everything below stacks on top of it, and without
#    it the map renders as bare geometry on white -- Map.tsx's `openmaptiles`
#    source is served from this tileset via /tiles/{z}/{x}/{y}.pbf.
bash scripts/maps/build_nyc_mbtiles.sh

# 1. Footprints from the OSM extract already on disk for the walking graph.
.venv/bin/python backend/scripts/extract_building_footprints.py

# 2. Score them with the backend's own algorithm.
.venv/bin/python backend/scripts/score_buildings.py

# 3. Bake the scores into a vector tileset.
.venv/bin/python backend/scripts/build_building_tiles.py

# 4. The plateau the 3D view stands the city on.
.venv/bin/python backend/scripts/build_plateau_dem.py

# Node serves all three from the repo root; the tilesets live in state.
# The basemap's link name is the one the tile route expects -- it is not
# cosmetic, and renaming it serves 404s for every basemap tile.
ln -sf /mnt/data/urban-dossier-state/maps/output/new-york-openmaptiles.mbtiles \
  osm-2020-02-10-v3.11_new-york_new-york.mbtiles
ln -sf /mnt/data/urban-dossier-state/maps/output/building-scores.mbtiles \
  building-scores.mbtiles
ln -sf /mnt/data/urban-dossier-state/maps/output/nyc-plateau-dem.mbtiles \
  nyc-plateau-dem.mbtiles
```

The plateau is Terrain-RGB, not geometry: the five boroughs are encoded at a
constant 260 m and everything past the shoreline at 0, so MapLibre's terrain
raises the city and the step at the coast becomes the model's cut edge. It has
to be elevation rather than an extruded slab because terrain is the only thing
that carries the *basemap* up with it -- fill-extrusion is the only layer type
that can be given a height, and symbols cannot be lifted at all, so a slab
built from geometry means a city model with no street names on it. 150 tiles,
0.24 MB, about a second to build.

The validated workstation build extracted 1,506,922 footprints in 400 s,
scored them in ~170 s and produced an 85.8 MB tileset in 14 s.

That tileset carries two layers, joined with `tile-join`:

| layer | zoom | contents | size |
|---|---|---|---|
| `building_massing` | 10-12 | 10,765 buildings over 25 m | 0.19-0.30 MB/zoom |
| `building_scores` | 13-16 | all 1,086,257 buildings | 14.7-26.1 MB/zoom |

The split exists for the 3D view, which needs the skyline while the whole city
is on screen -- 1.09M prisms in the four tiles that cover NYC at z10 is not
something to hand a browser. It is cheap because NYC's heights are skewed: a
median of 7.9 m but 3,006 buildings over 50 m, and a 7.9 m rowhouse is
sub-pixel at z10 anyway.

Two tippecanoe runs rather than per-feature `tippecanoe` minzoom blocks:
tippecanoe 2.49 accepts those blocks and then emits nearly empty tiles. A
50k-feature sample went from 6.12 MB to 0.36 MB with them, and one midtown z16
tile from 2,860 to 188 bytes, with no warning on stderr. Check
`tiles_by_zoom` in `building_tiles.manifest.json` after any change here -- the
failure is silent and the feature count in the log stays correct.

Heights come from OSM: 1,066,125 measured, 923 derived from `building:levels`
at 3.5 m per storey, 19,209 defaulted to 8 m. Each feature carries
`height_known` so the view can distinguish a measured tower from a guessed one.

Scoring is fast because the score is a function of the H3 r9 cell, not of the
building: `_h3_cells_for_radius` derives its k-ring from `latlng_to_cell(...,
9)`, so the backend returns the same numbers for every point inside a cell.
The pass therefore evaluates 15,141 cells rather than 1.5M buildings, and
`backend/tests/test_building_scores_match_backend.py` asserts the baked value
equals what `DirectQueryDataProvider` reports for the same coordinate. That
equality is the point: the colour on the map and the number in the detail panel
have to be the same claim.

The whole step is optional. `/api/building-tiles/status` reports whether the
tileset is being served and the client falls back to its previous client-side
colouring when it is not, so a host without tippecanoe still gets a working
map.

Both the overview cells and the building pass are clipped to
`data/boundaries/nta_2020.geojson`, which is the city's own land partition and
contains no water polygons, so its union is the coastline. Without it the H3
grid runs up to 4.7 km offshore and scores open water -- 170 amenities cells
sat off land, 134 of them below 40, painting the harbour the same red as an
underserved block -- and the bbox-cut OSM extract hands New Jersey and Nassau
County buildings a score borrowed from the nearest NYC cell through its k-ring.
Cells under 3% land are dropped outright; buildings are kept within the land
union buffered by 100 m, which retains the Hudson and East River piers (real
NYC buildings sitting past a shoreline drawn at the bulkhead) while excluding
the far bank. If the boundary file is missing both passes log a warning and
skip clipping rather than fail.

The validated workstation build produced 2,109,327 walking nodes and 2,432,374
edges from the 146 MB extract in about 41 s, stored as 53 MB of Parquet. The
graph is deliberately **not** loaded into the FastAPI process: each isochrone
request selects only the nodes inside a bounding box with DuckDB and runs
Dijkstra on that subgraph, which keeps the resident footprint flat and answers
a 10-minute isochrone in about one second.

The elasticity fit reports a Spearman correlation per intervention. On the
validated snapshot `bike_lane`, `bus_stop`, `toilet` and `linknyc` fit at 1.0
because the published score is a rank transform of the asset count, while
`park` fits at 0.45 — `parks_access` scores on total acreage, so park *count*
is only a weak proxy. `/api/simulate` marks that projection `"quality":
"weak"` and attaches a warning rather than presenting it with equal
confidence.

`download_nta_2020.sh` publishes atomically only after validating the GeoJSON,
262 unique NTA codes, polygon geometry, ArcGIS layer definition, and official
metadata PDF. Its manifest records release 26B, source URLs, byte sizes, field
names, and SHA-256 hashes. These generated files are deliberately ignored by
Git and must be reproduced on each deployment host.

## 4. Configure and start vLLM

```bash
cp deploy/gpu.env.example /mnt/data/urban-dossier-state/runtime/gpu.env

docker compose \
  --env-file /mnt/data/urban-dossier-state/runtime/gpu.env \
  -f deploy/compose.gpu.yml pull llm

docker compose \
  --env-file /mnt/data/urban-dossier-state/runtime/gpu.env \
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

The 2026-08-12 vLLM 0.27.1 upgrade was regression-tested with the incumbent
Nano model: C1 output throughput was 308.5 tok/s (309 tok/s on the previous
0.23 baseline), the Gateway smoke test returned `gateway-route-ok`, and the
five dedicated-agent NemoClaw tests passed. Lightning and Super remain
explicit candidate services; starting them does not change the production
model or the sandbox's configured model name. Follow
[`MODEL_CANDIDATES.md`](MODEL_CANDIDATES.md) before running or promoting one.

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
  --env-file /mnt/data/urban-dossier-state/runtime/gpu.env \
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

## 6. Start the stack

### The everyday path: one command

```bash
scripts/start_stack.sh          # backend :8090 + frontend :3456
scripts/start_stack.sh --llm    # ... and the production vLLM on :8000
scripts/start_stack.sh --status # report only, change nothing
```

This installs `ud-backend-noagent.service`, `ud-frontend.service` and
`ud-stack.target` from `deploy/systemd/` into `~/.config/systemd/user/`,
starts them, and probes every endpoint. It is idempotent — run it again after
a reboot, a `git pull`, or a unit-file edit.

`ud-backend-noagent` is the same FastAPI app as the unit in the next section
with `URBAN_DOSSIER_AGENT_ENABLED=0`, which drops the OpenClaw gateway from
the startup path. Use it whenever you are working on the map, the data plane
or the frontend; use the full unit below only when you actually need the
agent sandbox. Both bind 8090, so only one of them runs at a time.

Two failures this path exists to prevent, both of which cost a session:

- **The frontend must not run under `/usr/bin/node`.** `better-sqlite3` is a
  native addon and loads only under the Node ABI it was built against; this
  host has four Node installs and the one first on `PATH` is the wrong one.
  `scripts/run_frontend.sh` probes for a Node that can require the addon.
- **The topology has to live on disk.** It used to be reconstructed from
  memory as a pair of `systemd-run` command lines, which meant it vanished on
  reboot and came back subtly different each time.

### Install the persistent full-stack (agent) service

```bash
cp deploy/backend.env.example /mnt/data/urban-dossier-state/runtime/backend.env
chmod 600 /mnt/data/urban-dossier-state/runtime/backend.env

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
scripts/run_frontend.sh   # NOT `node server.js` -- see section 6
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
  --env-file /mnt/data/urban-dossier-state/runtime/gpu.env \
  -f deploy/compose.gpu.yml stop
```

These operations preserve models, datasets, sandbox state, and Docker volumes.
