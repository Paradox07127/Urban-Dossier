# DGX Spark Deployment Checklist — Urban Dossier v2

> **Independent deployment profile.** This GB10 checklist remains supported as
> a separate hardware path. The currently validated x86 workstation deployment
> is documented in [`DEPLOY_WORKSTATION.md`](DEPLOY_WORKSTATION.md). Do not copy
> the workstation's `gpu-memory-utilization`, container digest, or x86 kernel
> choice into this DGX profile without benchmarking on GB10.
>
> **Shared data contract.** DGX Spark no longer uses a separate flat/cleaned-CSV
> architecture. It must publish the same Bronze/Silver/Gold/Serving layers and
> manifests described in [`DATA_ARCHITECTURE.md`](DATA_ARCHITECTURE.md). GB10
> unified memory changes execution choices, not dataset schemas or score rules.

Target: NVIDIA GB10 Grace Blackwell (Acer Veriton GN100), ARM64, 128 GiB unified memory.
Run-through this list end-to-end after `git pull` on the box. Each section is independently checkable; do not skip ordering.

Current status (reviewed 2026-08-12; RAG items removed 2026-08-20): the
Nemotron launcher provides only `demo`, `balanced`, and `long-context`
profiles. The embedding profile this checklist used to require was never
implemented on ARM64, and no longer needs to be: RAG was retired on
2026-08-20 (see README § "RAG: retired"), so there is no second inference
service, no vector index, and no ingest step on this profile either.

---

## Phase 0 — Pre-flight

- [ ] `nvidia-smi` returns GB10, driver loaded
- [ ] `python3 --version` >= 3.12
- [ ] `node --version` >= 22
- [ ] At least 80 GiB free in `/` (model + index + Parquet)
- [ ] vLLM service NOT running yet (`pgrep -f vllm` empty), or already running on `:8000` (LLM) with the new profiles
- [ ] No legacy `ollama` process running (`pgrep ollama` empty)
- [ ] `URBAN_DOSSIER_RAW_DATA_ROOT` points to a categorized raw root containing all 18 CSVs
- [ ] Raw and ready manifests pass with 18/18 CSV and 44/44 Parquet files; do not reuse a workstation manifest
- [ ] Branch is `main`, latest pulled
- [ ] `git status` clean

---

## Phase 1 — System dependencies

### scipy (new, pattern detector)
- [ ] `pip install 'scipy>=1.11'` — confirm ARM64 wheel installs cleanly (manylinux_2_28_aarch64 or source build)

---

## Phase 2 — Python package installs

- [ ] `cd Urban-Dossier`
- [ ] `pip install -r backend/requirements.txt` (now includes scipy)
- [ ] `bash skills/urban_dossier_analyst/bootstrap.sh` (creates `.venv`, installs openai/httpx/pydantic)
- [ ] `cd interactive-map-explorer && npm install && npm run build && cd ..`
- [ ] `npm install` (root, for tile server)

### Shared data publication gate

- [ ] Follow [`DATA_ARCHITECTURE.md`](DATA_ARCHITECTURE.md) to download and strictly audit all 18 raw datasets
- [ ] Run all preprocessing specs into staging; do not create a duplicate `*.cleaned.csv` tree
- [ ] Generate baselines, optimize Parquet, and validate 44/44 ready files
- [ ] Atomically publish the validated directory to `URBAN_DOSSIER_READY_ROOT`
- [ ] Confirm `/api/coverage` reports `provider_ready=true`, 13 core datasets, and `ready_baselines_available=true`
- [ ] Treat `overview_ready=false` as a separate Gold artifact task, not as raw-download failure

---

## Phase 3 — vLLM (Nemotron 30B)

- [ ] Confirm `MODEL_PATH=/model` (or wherever Nemotron NVFP4 weights live)
- [ ] `bash scripts/vllm/start_vllm.sh --dry-run --profile balanced` (sanity check resolved command)
- [ ] `bash scripts/vllm/start_vllm.sh --profile balanced &`
- [ ] Wait for "Application startup complete" in logs (~2 min for CUDA graphs warmup if not `--enforce-eager`)
- [ ] Verify: `curl -s http://localhost:8000/v1/models | jq '.data[].id'`
- [ ] Smoke test inference: `curl -s http://localhost:8000/v1/chat/completions -H 'Content-Type: application/json' -d '{"model":"<model_id>","messages":[{"role":"user","content":"hi"}],"max_tokens":10}'`
- [ ] **Verify JSON mode**: `curl ... -d '{"model":"...","messages":[...],"response_format":{"type":"json_object"}}'` returns valid JSON. If not supported, pattern_detector Layer 3 will degrade — check logs.
- [ ] Set `URBAN_DOSSIER_MODEL` env var to the actual model id (do NOT leave at "auto" if not supported)
- [ ] Watch GPU memory: `nvidia-smi -l 5` should show ~70% utilization (matches `--gpu-memory-utilization 0.7`), leaving ~38 GiB headroom for cuDF + agent

---

## Phase 5 — NemoClaw skill registration

- [ ] `nemoclaw onboard` (if not already done)
- [ ] `cp -r skills/urban_dossier_analyst /path/to/nemoclaw/skills/`
- [ ] `cp -r skills/nemoclaw-user-prep-data /path/to/nemoclaw/skills/`
- [ ] `cp -r skills/blocksense-report /path/to/nemoclaw/skills/`
- [ ] `cp -r skills/blocksense-poster /path/to/nemoclaw/skills/`
- [ ] `cp -r skills/prep-data-{discover,clean,report} /path/to/nemoclaw/skills/`
- [ ] Run each skill's `bootstrap.sh` if present
- [ ] Verify: `nemoclaw status` shows Phase: Ready
- [ ] Verify: `nemoclaw nemoshell status` lists all skills including `urban-dossier-analyst`

> Data execution note: DuckDB is the shared reference serving path. Keep cuDF
> outside the critical FastAPI environment until a GB10 benchmark shows a
> benefit for the actual query. RAPIDS may run as an isolated batch adapter;
> its availability is not a data-correctness requirement.

---

## Phase 6 — Backend (FastAPI)

- [ ] `cd backend`
- [ ] Start Uvicorn with explicit `URBAN_DOSSIER_RAW_DATA_ROOT` and `URBAN_DOSSIER_READY_ROOT`; do not require `URBAN_DOSSIER_GPU_ACCEL=1`
- [ ] Verify: `/api/health` returns 200 and `provider_ready: true`; GPU availability is reported separately and may be false for the reference DuckDB environment
- [ ] Smoke test pattern detector (calls into Nemotron + scipy): hit `POST /api/analyze-point` with a known dense Brooklyn point — check log for "Pattern detector" entries, no exceptions
- [ ] Smoke test agent endpoint: `curl -X POST http://localhost:8090/api/agent/ask -H 'Content-Type: application/json' -d '{"message":"How many rodent inspections are in the dataset?","max_iterations":3}'`
- [ ] Expected: response includes `tools_called` with a `query_dataset` invocation and `evidence` citing the dataset. A first call with a wrong `dataset_id` is acceptable — the error carries `available_datasets` and the agent is expected to re-issue.
- [ ] If `503: skill module not found` — check `Phase 5` was completed, restart uvicorn (no hot-reload for sys.path-injected modules)

> Note (in-process tool dispatcher): the agent loop tool dispatcher prefers in-process Python calls (~µs latency) when the FastAPI backend module is importable, falling back to HTTP only when the agent runs inside a NemoClaw sandbox process that cannot import the backend. To confirm the in-process path is live, grep the backend log for `dispatcher=in-process` after the smoke test above.

---

## Phase 7 — Frontend (Node tile server + React)

- [ ] `cd Urban-Dossier`
- [ ] `node server.js &` (starts `:3456`)
- [ ] Verify: `curl -s http://localhost:3456/api/health`
- [ ] Open browser: `http://<dgx-ip>:3456`
- [ ] Click anywhere on map — confirm full pipeline (overview → detail → report) renders

---

## Phase 8 — Mac → DGX port forwarding (for browser demo)

On Mac:
- [ ] `ssh -L 3456:localhost:3456 -L 8090:localhost:8090 -L 8000:localhost:8000 user@<dgx-ip>`
- [ ] Open Mac browser at `http://localhost:3456`

---

## Phase 9 — Integration smoke tests (end-to-end)

- [ ] **Test 1 — direct backend**: click point, verify report renders with non-empty patterns list
- [ ] **Test 2 — dataset vocabulary**: agent ask "how many open housing violations are near here" → should reach `query_dataset` with `housing_violations` and cite it
- [ ] **Test 3 — Tool dispatch**: agent ask "score this neighborhood: 40.6892, -74.0445" → should call `score_neighborhood`, return numeric scores
- [ ] **Test 4 — Pattern detection (Layer 3)**: trigger an analyze-point on a known multi-issue area (e.g., Bushwick) → check pattern_detector log for LLM-named patterns vs auto-named
- [ ] **Test 5 — ReAct termination**: agent ask "compare Bushwick and Park Slope safety" → trace should show ≤8 iterations, terminate cleanly. (Tool 2 `compare_neighborhoods` currently raises NotImplementedError — agent should adapt and use 2× `score_neighborhood` instead)

---

## Phase 10 — Performance baseline capture (mandatory — paste into README before demo)

The following three measurements are required. Capture the raw numbers, paste them into the README "Why DGX Spark" section as falsifiable evidence, and link the raw log file from `docs/perf-baseline-YYYY-MM-DD.md`.

- [ ] **vLLM Nemotron P50 latency on a 256-token completion.** Issue 30 sequential `chat/completions` calls with `max_tokens=256` against `:8000`, sort the wall-clock times, record the P50. Log file: `docs/perf/vllm-256tok-p50.log`.
- [ ] **Data-engine comparison on published Parquet.** Compare DuckDB and the candidate RAPIDS path on the same projection/filter/groupby workload, including cold and warm latency. Log file: `docs/perf/data-engine-comparison.log`.
- [ ] **`nvidia-smi` GPU memory peak during a full `agent_loop` run with all 7 tools active.** Start `nvidia-smi --query-gpu=memory.used --format=csv -l 1 > docs/perf/nvsmi-agent-peak.log &`, run an agent query that exercises every tool (`score_neighborhood`, `compare_neighborhoods`, `query_dataset`, `find_similar_neighborhoods`, `walking_isochrone`, `simulate_intervention`, `search_address`), stop the logger, record the maximum value. This is the falsifiability number cited in README "Why DGX Spark".
- [ ] Save outputs to `docs/perf-baseline-YYYY-MM-DD.md` and update README with the three captured numbers.

---

## Known issues / hardware verification opens

### From Agent skill agent (B)
- [ ] `priority_order=["amenities","transit","safety"]` is hardcoded default — decide if it should be a tool argument
- [ ] `find_similar_neighborhoods` proxied through `/api/watchlist/run` (single-seed); v2.1 should add real `/api/similar`
- [ ] `DEMO_TOKEN` read at module import — if rotated mid-session, re-import or pass per-call

### From Backend agent (C)
- [ ] vLLM `response_format={"type":"json_object"}` support — verify (already in Phase 3)
- [ ] `URBAN_DOSSIER_MODEL` env var must be set, "auto" likely won't work with current backend code
- [ ] scipy ARM64 wheel install (Phase 1)
- [ ] Pattern detector LLM timeout default 8s — measure p99 to avoid Layer 3 starvation
- [ ] `parents[3]` path injection in `app.py` is brittle — any backend dir restructure breaks it
- [ ] `agent_loop` is a generic module name — risk of shadowing if anything else on sys.path defines it

---

## Documentation tasks (after deployment confirms working)

- [x] Dataset count and the shared 18-source contract are documented in `DATA_ARCHITECTURE.md`
- [ ] Add a `## Master Agent Skill` section to README pointing to `skills/urban_dossier_analyst/SKILL.md`
- [ ] Capture screenshot of agent ReAct trace, embed in README

---

## Rollback (if v2 breaks demo)

- [ ] Stop backend: `pkill -f 'urban_dossier_backend.app'`
- [ ] `git stash` v2 changes (or `git checkout <last-stable-tag>`)
- [ ] Restart with old `start.sh` flow — v1 path uses no scipy or skill loop, will Just Work

---

## Sign-off

- [ ] All 10 phases green
- [ ] Performance baseline captured
- [ ] Known issues triaged (fix or accept)
- [ ] Deployment notes appended to `docs/deployment-log-YYYY-MM-DD.md` for next time
