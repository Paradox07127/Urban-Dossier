# DGX Spark Deployment Checklist — Urban Dossier v2

Target: NVIDIA GB10 Grace Blackwell (Acer Veriton GN100), ARM64, 128 GiB unified memory.
Run-through this list end-to-end after `git pull` on the box. Each section is independently checkable; do not skip ordering.

---

## Phase 0 — Pre-flight

- [ ] `nvidia-smi` returns GB10, driver loaded
- [ ] `python3 --version` >= 3.12
- [ ] `node --version` >= 22
- [ ] At least 80 GiB free in `/` (model + index + Parquet)
- [ ] vLLM service NOT running yet (`pgrep -f vllm` empty), or already running on `:8000` (LLM) and `:8001` (embeddings) with the new profiles
- [ ] No legacy `ollama` process running (`pgrep ollama` empty) — embeddings now go through vLLM, see Phase 1
- [ ] `~/nyc_open_data/` exists with all 18 CSVs (`bash scripts/download_datasets.sh` if not)
- [ ] Branch is `main`, latest pulled
- [ ] `git status` clean

---

## Phase 1 — System dependencies

### Embedding model (Qwen3-Embedding-4B served by a second vLLM instance)
- [ ] Download model: `huggingface-cli download Qwen/Qwen3-Embedding-4B`
- [ ] Start: `bash scripts/vllm/start_vllm.sh --profile embedding &`
       (this profile to be added to `start_vllm.sh`; binds `:8001`, `--task embed`)
- [ ] Verify: `curl -s http://localhost:8001/v1/models | jq '.data[].id'` returns `Qwen/Qwen3-Embedding-4B`
- [ ] Verify dim by sending a test embedding request:
      `curl -s http://localhost:8001/v1/embeddings -H 'Content-Type: application/json' -d '{"model":"Qwen/Qwen3-Embedding-4B","input":"test"}' | jq '.data[0].embedding | length'`
- [ ] Confirm the returned dimension matches `rag/embed.py`'s expected width before running ingest

### cuVS (required — default GPU vector backend)
- [ ] Try `pip install cuvs-cu13`
- [ ] If the wheel is not available, try `conda install -c rapidsai cuvs` (RAPIDS conda channel)
- [ ] If both fail, code falls back to FAISS-CPU but Spark Story score will suffer — fix this on hardware before demo. Do not ship the demo on FAISS-CPU.

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

## Phase 4 — RAG index build (one-time)

- [ ] `PYTHONPATH=. python -m rag.ingest rag/catalog.json --index-dir rag/index/ 2>&1 | tee rag/ingest.log`
- [ ] Wait ~30 seconds (75 chunks × 1 vLLM embedding call each, sequential against `:8001`)
- [ ] Verify index files created: `ls rag/index/` should show the cuVS index files + metadata sidecar JSON (FAISS-CPU fallback writes `.faiss` instead — that path is a demo failure, not a pass)
- [ ] **Verify cuVS was selected at runtime**: `grep "VectorIndex backend" rag/ingest.log` must show `cuvs` (not `faiss-cpu`). If it shows `faiss-cpu`, return to Phase 1 cuVS install before continuing.
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

> Note (in-process cuDF): cuDF is loaded directly inside the backend Python venv (`import cudf`); the legacy Docker HTTP service has been removed. No separate `docker compose up cudf` step is required and no port needs to be opened for it.

---

## Phase 6 — Backend (FastAPI)

- [ ] `cd backend`
- [ ] `URBAN_DOSSIER_GPU_ACCEL=1 uvicorn urban_dossier_backend.app:app --host 0.0.0.0 --port 8090 --log-level info --app-dir src &`
- [ ] Verify: `curl -s http://localhost:8090/api/health` returns 200, `gpu.cuda_available: true`
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
- [ ] **cuML DBSCAN throughput on 100K collisions.** Run the city-wide hotspot job over a 100,000-row collisions slice, time the DBSCAN call only (not loading), record rows-per-second. Log file: `docs/perf/cuml-dbscan-100k.log`.
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

- [ ] Fix README dataset count: 17 → 18 (preprocess_common.SPECS is the truth)
- [ ] Add a `## Master Agent Skill` section to README pointing to `skills/urban_dossier_analyst/SKILL.md`
- [ ] Add a `## RAG Pipeline` section pointing to `rag/README.md`
- [ ] Update `## Quick Start` to include the second vLLM instance (`--profile embedding`) on `:8001`
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
