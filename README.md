# Urban Dossier

### *A Local is All You Need*

NYC neighborhood analysis system. Click anywhere on the map and get a data-driven dossier covering safety, transit, amenities, and building conditions, scored against the city, trended over time, and narrated by an on-device LLM. Fully offline.

Built for Spark Hack NYC 2026 (NVIDIA hackathon).

## Why DGX Spark

Urban Dossier is built around three workloads that must run simultaneously on one host. Only the GB10 platform can host all three; the other obvious candidates fail on either memory or ecosystem.

### Hardware comparison

| Capability | RTX 5090 (32 GiB) | Mac Studio M4 Max (128 GiB unified) | **DGX Spark GB10 (128 GiB unified)** |
|---|---|---|---|
| NVFP4 inference (Blackwell) | Yes | No | Yes |
| 128 GiB single-host memory | No | Yes | Yes |
| CUDA + RAPIDS (cuDF / cuML / cuVS) | Yes (x86_64) | No (no CUDA) | Yes (aarch64) |
| Result: hosts this project | No — runs out of memory | No — RAPIDS does not exist on Apple Silicon | Yes — only platform with all three |

Mac Studio can serve open-weight models through MLX or llama.cpp, so model size alone is not the argument. The data path of this project depends on the RAPIDS stack (cuDF for city-wide aggregation, cuML for DBSCAN clustering, cuVS for the RAG vector index), and those libraries do not target Apple Silicon. The 5090 has full RAPIDS but does not have the memory to co-tenant the model, the embedding model, and the dataset working set.

### Three pillars Urban Dossier requires simultaneously

- **Nemotron-30B-A3B-NVFP4** weights (~15 GiB) plus KV/Mamba state — the reasoning model behind reports, the agent loop, and pattern naming.
- **cuDF + cuVS dataset cache** (~30 GiB peak across 17 datasets) — city-wide aggregation, NTA rollups, and the RAG vector index live in GPU memory rather than spilling.
- **Qwen3-Embedding-4B + reranker** resident (~10 GiB) — embeddings for `/api/agent/ask` retrieval, served from the same vLLM stack as Nemotron.

Total resident floor: ~55 GiB. Peak during heavy aggregation: ~90 GiB. The 5090 fails on memory before the third pillar even loads. The Mac fails on the RAPIDS ecosystem the moment cuDF is imported.

### Key NVIDIA stack components in the data path

- **vLLM** serves both Nemotron-30B-A3B-NVFP4 (`:8000`) and Qwen3-Embedding-4B (`:8001`) — one inference stack, two model instances, no separate embedding daemon.
- **cuVS** is the default vector backend for the RAG corpus (FAISS-CPU is fallback only).
- **cuDF** runs in-process inside the FastAPI backend for heavy aggregation (city-wide overview, NTA rollups via `backend/scripts/build_overview_nta.py`).
- **cuML DBSCAN** clusters incident data (collisions, EMS, fire) for hotspot detection.
- **NemoClaw / OpenClaw** sandboxes the agent skill so tool dispatch can run with a least-privilege policy.
- All five components share the GB10 LPDDR5X unified memory pool (273 GB/s, no PCIe transfer between host and accelerator).

### Falsifiable evidence the judge can verify

Run during the peak of the demo:

```bash
nvidia-smi --query-gpu=memory.used --format=csv -l 5
```

Expected: total GPU memory used > 60 GiB. Anything above 32 GiB proves a 5090 cannot host this configuration. Anything in the 60-90 GiB band proves all three pillars are co-resident, which is the entire point of the GB10 platform choice.

### Data sovereignty (secondary, not the Spark Story)

Urban Dossier ingests raw NYC parcel records, housing-code violations, and EMS dispatch logs. Inference, scoring, agent execution, and report generation all run on-device, which is the only acceptable posture for analysis that touches tenant-level housing data. This is a property of any local-inference deployment and is not by itself an argument for DGX Spark — the argument above is.

## Quick Start

```bash
git clone https://github.com/Paradox07127/Urban-Dossier.git
cd Urban-Dossier
bash scripts/download_datasets.sh
bash scripts/vllm/start_vllm.sh --profile balanced &
nemoclaw onboard && bash scripts/install_skills.sh
cd backend && python -m venv .venv && source .venv/bin/activate \
  && pip install -r requirements.txt \
  && uvicorn urban_dossier_backend.app:app --host 0.0.0.0 --port 8090 --app-dir src &
cd .. && npm install \
  && (cd interactive-map-explorer && npm install && npm run build) \
  && node server.js
```

Open `http://localhost:3456`.

## How It Works

```
1. Download any NYC Open Data CSV
2. Tell NemoClaw: "prepare my data in ~/nyc_open_data/safety/"
3. NemoClaw skill auto-cleans, indexes, and scores the data
4. Urban Dossier map instantly uses the processed data
```

You bring your own datasets, the agent prepares them, the map visualizes them. The NemoClaw `prep-data` skill handles any CSV without hand-written pipelines.

The `backend/scripts/preprocess_*.py` files are quick-verification scripts for the demo datasets listed below. The NemoClaw skill replaces this entire step in real usage.

## Service Dependency Chain

```
vLLM (:8000)  ←  Python Backend (:8090)  ←  Node Frontend (:3456)  →  Browser
                        ↑
                  NemoClaw Sandbox
                  (OpenClaw Agent)
```

- **vLLM** serves the local Nemotron 30B model. All LLM calls (report narratives, agent chat) go through it.
- **NemoClaw Sandbox** runs an OpenClaw agent inside a hardened sandbox. The frontend's "Deep Report", "Poster", and "Chat" features (`/api/agent/*`) require it.
- **Python Backend** (FastAPI) handles data queries, scoring, and report generation. Calls vLLM for narratives and NemoClaw for agent features.
- **Node Frontend** (Express) serves the React app, offline map tiles, and proxies API requests to the Python backend.

## Deploy

### Prerequisites

- DGX Spark (or similar GPU machine) with NVIDIA drivers
- Python 3.12+, Node.js 22+, Docker
- NemoClaw CLI installed (`curl -fsSL https://www.nvidia.com/nemoclaw.sh | bash`)

### Step 0 — Clone and download datasets

```bash
git clone https://github.com/Paradox07127/Urban-Dossier.git
cd Urban-Dossier
bash scripts/download_datasets.sh   # downloads all 17 datasets to ~/nyc_open_data/
```

### Step 1 — Inference server (vLLM + Nemotron 30B)

```bash
bash scripts/vllm/start_vllm.sh --profile balanced
```

See `scripts/vllm/README.md` for profile choices (demo / balanced / long-context) and the KV-cache math behind each.

Verify: `curl -s http://localhost:8000/v1/models`

### Step 2 — NemoClaw sandbox (OpenClaw agent)

Required for Deep Report, Poster, and Chat features.

```bash
# First-time setup:
nemoclaw onboard

# Install skills into the sandbox:
cp -r skills/nemoclaw-user-prep-data /path/to/nemoclaw/skills/
cp -r skills/blocksense-report /path/to/nemoclaw/skills/
cp -r skills/blocksense-poster /path/to/nemoclaw/skills/
cd /path/to/nemoclaw/skills/nemoclaw-user-prep-data && bash bootstrap.sh
cd /path/to/nemoclaw/skills/blocksense-report && bash bootstrap.sh
cd /path/to/nemoclaw/skills/blocksense-poster && bash bootstrap.sh

# Verify sandbox is running:
nemoclaw status                 # nemoshell should show Phase: Ready
nemoclaw nemoshell status       # detailed sandbox + policy info
```

### Step 3 — Python backend (:8090)

```bash
cd Urban-Dossier/backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn urban_dossier_backend.app:app \
  --host 0.0.0.0 --port 8090 --log-level info \
  --app-dir src

# Verify:
curl -s http://localhost:8090/api/health
```

### Step 4 — Node frontend + tile server (:3456)

```bash
cd Urban-Dossier

# Install Node dependencies:
npm install

# Build the React frontend (first time or after code changes):
cd interactive-map-explorer && npm install && npm run build && cd ..

# Start the server:
node server.js

# Verify:
curl -s http://localhost:3456/api/health
```

### Step 5 — Open browser

Navigate to `http://<machine-ip>:3456`.

### Shutdown (reverse order)

```bash
# Stop Node frontend
kill $(cat urban_dossier_node.pid)

# Stop Python backend
kill $(cat urban_dossier_backend.pid)

# Stop NemoClaw sandbox (state persists across restarts)
nemoclaw nemoshell destroy

# vLLM runs as a system service on DGX Spark; leave it up
```

## Datasets

We use 17 NYC Open Data datasets. One-command download:

```bash
bash scripts/download_datasets.sh          # all datasets → ~/nyc_open_data/
bash scripts/download_datasets.sh /my/dir  # or custom path
```

Already downloaded files are skipped. Full list below:

### Safety

| Dataset | Filename | NYC Open Data |
|---|---|---|
| Motor Vehicle Collisions | `safety/motor_vehicle_collisions.csv` | [Motor Vehicle Collisions - Crashes](https://data.cityofnewyork.us/Public-Safety/Motor-Vehicle-Collisions-Crashes/h9gi-nx95) |
| Rodent Inspections | `environment/rodent_inspections.csv` | [Rodent Inspection](https://data.cityofnewyork.us/Health/Rodent-Inspection/p937-wjvj) |
| 311 Service Requests | `quality_of_life/311_service_requests_2020_present.csv` | [311 Service Requests from 2010 to Present](https://data.cityofnewyork.us/Social-Services/311-Service-Requests-from-2010-to-Present/erm2-nwe9) |
| EMS Incident Dispatch | `safety/ems_incident_dispatch.csv` | [EMS Incident Dispatch Data](https://data.cityofnewyork.us/Public-Safety/EMS-Incident-Dispatch-Data/76xm-jjuj) |
| Fire Incident Dispatch | `safety/fire_incident_dispatch.csv` | [Fire Incident Dispatch Data](https://data.cityofnewyork.us/Public-Safety/Fire-Incident-Dispatch-Data/8m42-w767) |

### Transit

| Dataset | Filename | NYC Open Data |
|---|---|---|
| Subway Entrances | `transit/mta_subway_entrances_exits_2024.csv` | [MTA Subway Entrances and Exits](https://data.cityofnewyork.us/Transportation/Subway-Entrances/drex-xx56) |
| Bus Stop Shelters | `transit/bus_stop_shelters.csv` | [Bus Stop Shelters](https://data.cityofnewyork.us/Transportation/Bus-Stop-Shelters/qafz-7myz) |
| Bike Routes | `transit/nyc_bike_routes.csv` | [New York City Bike Routes](https://data.cityofnewyork.us/Transportation/New-York-City-Bike-Routes/7vsa-caz7) |
| Open Streets | `transit/open_streets_locations.csv` | [Open Streets Locations](https://data.cityofnewyork.us/Transportation/Open-Streets-Locations/uiay-nctu) |

### Amenities

| Dataset | Filename | NYC Open Data |
|---|---|---|
| Restaurant Inspections | `amenities/dohmh_restaurant_inspections.csv` | [DOHMH New York City Restaurant Inspection Results](https://data.cityofnewyork.us/Health/DOHMH-New-York-City-Restaurant-Inspection-Results/43nn-pn8j) |
| Parks Properties | `amenities/parks_properties.csv` | [Parks Properties](https://data.cityofnewyork.us/Recreation/Parks-Properties/k2ya-ucmv) |
| Street Trees | `amenities/street_trees.csv` | [2015 Street Tree Census](https://data.cityofnewyork.us/Environment/2015-Street-Tree-Census-Tree-Data/uvpi-gqnh) |
| LinkNYC Kiosks | `amenities/linknyc_kiosk_locations.csv` | [LinkNYC Kiosk Locations](https://data.cityofnewyork.us/Social-Services/LinkNYC-Kiosk-Locations/s4kf-3yrf) |
| Public Toilets | `amenities/public_toilets.csv` | [Directory Of Toilets In Public Parks](https://data.cityofnewyork.us/Recreation/Directory-Of-Toilets-In-Public-Parks/hjae-yuav) |
| Facilities Database | `amenities/facilities_database.csv` | [Facilities Database](https://data.cityofnewyork.us/City-Government/Facilities-Database/ji82-xba5) |

### Building

| Dataset | Filename | NYC Open Data |
|---|---|---|
| Housing Violations | `buildings/housing_code_violations.csv` | [Housing Maintenance Code Violations](https://data.cityofnewyork.us/Housing-Development/Housing-Maintenance-Code-Violations/wvxf-dwi5) |
| AEP Buildings | `buildings/buildings_aep.csv` | [AEP - Buildings](https://data.cityofnewyork.us/Housing-Development/AEP-Buildings/hcir-3275) |

### Location Reference

| Dataset | Filename | NYC Open Data |
|---|---|---|
| PLUTO | `buildings/pluto.csv` | [Primary Land Use Tax Lot Output (PLUTO)](https://data.cityofnewyork.us/City-Government/Primary-Land-Use-Tax-Lot-Output-PLUTO-/64uk-42ks) |

### Directory structure

```
~/nyc_open_data/
├── safety/
│   ├── motor_vehicle_collisions.csv
│   ├── ems_incident_dispatch.csv
│   └── fire_incident_dispatch.csv
├── environment/
│   └── rodent_inspections.csv
├── quality_of_life/
│   └── 311_service_requests_2020_present.csv
├── transit/
│   ├── mta_subway_entrances_exits_2024.csv
│   ├── bus_stop_shelters.csv
│   ├── nyc_bike_routes.csv
│   └── open_streets_locations.csv
├── amenities/
│   ├── dohmh_restaurant_inspections.csv
│   ├── parks_properties.csv
│   ├── street_trees.csv
│   ├── linknyc_kiosk_locations.csv
│   ├── public_toilets.csv
│   └── facilities_database.csv
└── buildings/
    ├── housing_code_violations.csv
    ├── buildings_aep.csv
    └── pluto.csv
```

## NemoClaw Skills

The `skills/` directory contains three agent skills for [NemoClaw](https://github.com/NVIDIA/NemoClaw) (OpenClaw). Copy them into NemoClaw's skills directory and the agent reads each `SKILL.md` and auto-triggers based on user input.

### nemoclaw-user-prep-data

**The core skill.** User points at a directory of CSVs, the agent autonomously profiles, assesses quality (20+ auto-detectors), proposes a cleaning plan, waits for user confirmation, executes, and delivers a data dictionary.

```
User: "Prepare my data in ~/nyc_open_data/safety/ for neighborhood analysis"

Agent:
  Phase 1 — Discovery: scans all CSVs, profiles with pandas (or GPU cuDF for large files)
  Phase 2 — Assessment: filters by relevance, detects quality issues, proposes cleaning plan
  >>> User confirms plan <<<
  Phase 3 — Execution: cleans via clean.py, validates before/after, crash-safe logging
  Phase 4 — Report: generates data dictionary, delivers summary
```

Supports 12 cleaning operations: `drop_column`, `rename_column`, `replace_values`, `cast_type`, `strip_whitespace`, `transform_case`, `drop_duplicates`, `drop_nulls`, `filter_rows`, `add_h3_index`, `transform_coords`, `polygon_centroid`.

Output goes to `data/ready/`. The Urban Dossier backend auto-discovers and uses it.

### blocksense-report

Deep analysis report. Takes `/api/analyze-point` output, generates per-dimension LLM narratives (safety, transit, amenities, building), renders offline HTML + Markdown.

```
User: "Generate a report for this area"
Agent: extracts segments → LLM narratives per dimension → renders report.html + report.md
```

### blocksense-poster

Printable community flyer. Three templates: portrait (A4 handout), horizontal (community board), analytical (with charts). All offline-safe, no CDN.

```
User: "Create a poster for this neighborhood"
Agent: extracts highlights → LLM headline → renders poster.html (print-ready)
```

## Architecture

```
urban-dossier/
├── server.js                       # Node tile server + API proxy (:3456)
├── public/                         # Static frontend + offline map tiles
├── interactive-map-explorer/       # React + MapLibre + TailwindCSS
│   └── dist/                       # Production build (served by server.js)
├── backend/
│   ├── src/urban_dossier_backend/  # FastAPI scoring engine (:8090)
│   │   ├── app.py                  # API endpoints
│   │   ├── service.py              # Core analysis pipeline
│   │   ├── report.py               # Staged LLM report generation
│   │   ├── agent_service.py        # NemoClaw/OpenClaw agent integration
│   │   └── providers/              # Data access (DuckDB over Parquet)
│   └── scripts/preprocess_*.py     # Demo data verification scripts
├── skills/                         # NemoClaw agent skills
│   ├── nemoclaw-user-prep-data/    # Data preparation pipeline
│   ├── blocksense-report/          # Deep analysis reports
│   └── blocksense-poster/          # Community flyers
└── data/ready/                     # Processed Parquet (auto-discovered)
```

**Frontend** (React + MapLibre GL) → **Node Proxy** (Express, offline MBTiles, CORS) → **Backend** (FastAPI, DuckDB over Parquet, percentile-rank scoring, staged LLM narrative) → **vLLM** (Nemotron 30B) + **NemoClaw** (OpenClaw agent for deep reports).

## Key Environment Variables

| Variable | Default | What |
|---|---|---|
| `URBAN_DOSSIER_DATA_MODE` | `direct` | `direct` reads from `data/ready/` Parquet |
| `URBAN_DOSSIER_READY_ROOT` | auto-detect | Path to preprocessed data |
| `URBAN_DOSSIER_RAW_DATA_ROOT` | `~/nyc_open_data` | Raw CSV location |
| `OPENAI_BASE_URL` | `http://localhost:8000/v1` | vLLM endpoint for LLM narratives |
| `URBAN_DOSSIER_MODEL` | `auto` | Model name (auto-detects from vLLM) |
| `URBAN_DOSSIER_USE_LLM` | `auto` | `0` runs template-only reports. **Diagnostic / CI-test only, not for production.** |
| `URBAN_DOSSIER_AGENT_BACKEND` | `nemoclaw` | `scripts` bypasses the agent and calls scripts directly. **Diagnostic / CI-test only, not for production.** |
| `URBAN_DOSSIER_AGENT_ENABLED` | `1` | `0` disables agent endpoints entirely |
| `URBAN_DOSSIER_DEMO_TOKEN` | (empty) | If set, all API requests require this token in header |
| `URBAN_DOSSIER_BACKEND_URL` | `http://127.0.0.1:8090` | Node proxy target (set in server.js) |

## License

MIT
