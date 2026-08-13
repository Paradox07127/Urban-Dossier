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

Current status (reviewed 2026-08-12): the Nemotron launcher provides only
`demo`, `balanced`, and `long-context` profiles. The Qwen embedding profile
described below is still unimplemented and has not been validated on ARM64.
Do not copy the x86 Docker compose service into this checklist or run the
nonexistent `--profile embedding`; complete that launcher work and its GB10
smoke test before enabling RAG ingestion on this profile.

---

## Phase 0 — Pre-flight

- [ ] `nvidia-smi` returns GB10, driver loaded
- [ ] `python3 --version` >= 3.12
- [ ] `node --version` >= 22
- [ ] At least 80 GiB free in `/` (model + index + Parquet)
- [ ] vLLM service NOT running yet (`pgrep -f vllm` empty), or already running on `:8000` (LLM) and `:8001` (embeddings) with the new profiles
- [ ] No legacy `ollama` process running (`pgrep ollama` empty) — embeddings now go through vLLM, see Phase 1
- [ ] `URBAN_DOSSIER_RAW_DATA_ROOT` points to a categorized raw root containing all 18 CSVs
- [ ] Raw and ready manifests pass with 18/18 CSV and 44/44 Parquet files; do not reuse a workstation manifest
- [ ] Branch is `main`, latest pulled
- [ ] `git status` clean

---

## Phase 1 — System dependencies

### Embedding model (Qwen3-Embedding-4B served by a second vLLM instance)
- [ ] Download model: `huggingface-cli download Qwen/Qwen3-Embedding-4B`
- [ ] Add a dedicated ARM64 embedding launcher/profile that binds `:8001` and
      uses vLLM's pooling/embedding runner; `start_vllm.sh` does not currently
      implement this profile
- [ ] Start the new profile only after its dry-run output and vLLM version
      compatibility have been reviewed on GB10
- [ ] Verify: `curl -s http://localhost:8001/v1/models | jq '.data[].id'` returns `Qwen/Qwen3-Embedding-4B`
- [ ] Verify dim by sending a test embedding request:
      `curl -s http://localhost:8001/v1/embeddings -H 'Content-Type: application/json' -d '{"model":"Qwen/Qwen3-Embedding-4B","input":"test"}' | jq '.data[0].embedding | length'`
- [ ] Confirm the returned dimension matches `rag/embed.py`'s expected width before running ingest

### cuVS (optional for a future large vector corpus)
- [ ] Try `pip install cuvs-cu13`
- [ ] If the wheel is not available, try `conda install -c rapidsai cuvs` (RAPIDS conda channel)
- [ ] For the current small catalog corpus, CPU exact/FAISS is valid; require cuVS only after a measured scale or latency benefit

### scipy (new, pattern detector)
- [ ] `pip install 'scipy>=1.11'` — confirm ARM64 wheel installs cleanly (manylinux_2_28_aarch64 or source build)

---

## Phase 2 — Python package installs

- [ ] `cd Urban-Dossier`
- [ ] `pip install -r backend/requirements.txt` (now includes scipy)
- [ ] `pip install -r rag/requirements.txt` (faiss-cpu, sentence-transformers, etc.)
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

## Phase 4 — Optional RAG index build

- [ ] `PYTHONPATH=. python -m rag.ingest rag/catalog.json --index-dir rag/index/ 2>&1 | tee rag/ingest.log`
- [ ] Record ingest time for the current approximately 90 catalog chunks; batch embedding requests if the corpus grows
- [ ] Verify index files and metadata sidecar were created for the selected backend
- [ ] Record the selected backend; FAISS-CPU is acceptable for the current catalog, while cuVS requires a separate scale/latency benchmark
- [ ] Smoke test: `PYTHONPATH=. python -c "from rag import retrieve; r = retrieve('rodent complaints', top_k=3); [print(x.dataset_id, x.score) for x in r]"`
- [ ] Expected: at least one hit with `dataset_id == "safety_rodent"` ranking high

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
- [ ] Smoke test agent endpoint: `curl -X POST http://localhost:8090/api/agent/ask -H 'Content-Type: application/json' -d '{"message":"What datasets cover noise complaints?","max_iterations":3}'`
- [ ] Expected: response includes `tools_called` with `retrieve_dataset_docs` invocation, `evidence` cites at least one dataset
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
- [ ] **Test 2 — RAG retrieval**: agent ask "find me datasets about housing violations" → should cite `building_violations` and `building_aep`
- [ ] **Test 3 — Tool dispatch**: agent ask "score this neighborhood: 40.6892, -74.0445" → should call `score_neighborhood`, return numeric scores
- [ ] **Test 4 — Pattern detection (Layer 3)**: trigger an analyze-point on a known multi-issue area (e.g., Bushwick) → check pattern_detector log for LLM-named patterns vs auto-named
- [ ] **Test 5 — ReAct termination**: agent ask "compare Bushwick and Park Slope safety" → trace should show ≤8 iterations, terminate cleanly. (Tool 2 `compare_neighborhoods` currently raises NotImplementedError — agent should adapt and use 2× `score_neighborhood` instead)

---

## Phase 10 — Performance baseline capture (mandatory — paste into README before demo)

The following three measurements are required. Capture the raw numbers, paste them into the README "Why DGX Spark" section as falsifiable evidence, and link the raw log file from `docs/perf-baseline-YYYY-MM-DD.md`.

- [ ] **vLLM Nemotron P50 latency on a 256-token completion.** Issue 30 sequential `chat/completions` calls with `max_tokens=256` against `:8000`, sort the wall-clock times, record the P50. Log file: `docs/perf/vllm-256tok-p50.log`.
- [ ] **Data-engine comparison on published Parquet.** Compare DuckDB and the candidate RAPIDS path on the same projection/filter/groupby workload, including cold and warm latency. Log file: `docs/perf/data-engine-comparison.log`.
- [ ] **`nvidia-smi` GPU memory peak during a full `agent_loop` run with all 8 tools active.** Start `nvidia-smi --query-gpu=memory.used --format=csv -l 1 > docs/perf/nvsmi-agent-peak.log &`, run an agent query that exercises every tool (`score_neighborhood`, `compare_neighborhoods`, `query_dataset`, `find_similar_neighborhoods`, `walking_isochrone`, `simulate_intervention`, `search_address`, `retrieve_dataset_docs`), stop the logger, record the maximum value. This is the falsifiability number cited in README "Why DGX Spark".
- [ ] Save outputs to `docs/perf-baseline-YYYY-MM-DD.md` and update README with the three captured numbers.

---

## Known issues / hardware verification opens

### From RAG agent (A)
- [ ] Confirm vLLM `:8001` returns the expected embedding dimension for `Qwen/Qwen3-Embedding-4B` on ARM64 (already in Phase 1)
- [ ] Confirm `cuvs-cu13` is the right wheel name; might need RAPIDS conda channel
- [ ] Decide CrossEncoder device: `cuda:0` (shares with vLLM, may contend) vs `cpu` (safe default in code) — benchmark
- [ ] Embedding throughput: 75 sequential HTTP calls during ingest ~30s. If ingest grows, switch the rag client to vLLM's batched embeddings endpoint

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
- [ ] Add a `## RAG Pipeline` section pointing to `rag/README.md`
- [ ] After the DGX embedding launcher exists and passes its ARM64 smoke test,
      update Quick Start with the actual command for the second vLLM instance
      on `:8001`
- [ ] Capture screenshot of agent ReAct trace, embed in README

---

## Rollback (if v2 breaks demo)

- [ ] Stop backend: `pkill -f 'urban_dossier_backend.app'`
- [ ] Stop the embedding vLLM instance: `pkill -f 'vllm.*8001'` (leave the LLM instance on `:8000` running unless rolling that back too)
- [ ] `git stash` v2 changes (or `git checkout <last-stable-tag>`)
- [ ] Restart with old `start.sh` flow — v1 path uses no scipy/RAG/skill loop, will Just Work

---

## Sign-off

- [ ] All 10 phases green
- [ ] Performance baseline captured
- [ ] Known issues triaged (fix or accept)
- [ ] Deployment notes appended to `docs/deployment-log-YYYY-MM-DD.md` for next time
