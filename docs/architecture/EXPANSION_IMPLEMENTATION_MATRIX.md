# EXPANSION_PLAN implementation matrix

> Audit date: 2026-08-12  
> Working branch: `worktree-expansion-platform`  
> Status vocabulary: **done** means the plan's acceptance criterion has
> executable evidence; **partial** means useful code exists but the acceptance
> criterion is not yet proven; **not started** means no product implementation.

This is the execution ledger for [EXPANSION_PLAN.md](../../EXPANSION_PLAN.md).
The planning document describes intent; this file records current evidence and
must be updated in the same commit that changes an item's status.

## 1. Composite score

| Item | Status | Current evidence | Missing proof / next gate |
| --- | --- | --- | --- |
| 1.1 MetricDefinition registry | done | `metrics.py`, `/api/metrics`, registry contract tests, methodology UI | Keep Node's explicit version gates synchronized by test |
| 1.2 Explicit missing data | done | Point `score_coverage`; H3/NTA `coverage_n`, `coverage_total`, `coverage_fraction`, and weighted `coverage_ratio`; map opacity and NTA popup disclose thin evidence | Re-run overview H3 then NTA build after every source/weight change |
| 1.3 Correlation analysis | partial | Reproducible Spearman report; copied collision metric removed; rodent changed to inspection-positive rate | Decide the remaining 311/housing overlap and record the weight decision |
| 1.4 Sensitivity pipeline | partial | 1,000-draw offline artifact, API score/rank intervals, UI interval band | Replace the headline pseudo-precise point score with a documented public tier; add artifact snapshot/version invalidation |
| 1.5 Dataset expansion | partial | Eight raw snapshots and manifests exist under the external state root | No expansion dataset has completed quarantine → ready schema → MetricDefinition → correlation → sensitivity → publication |
| 1.6 Public methodology page | partial | In-app methodology panel reads `/api/metrics` | Needs a routable/shareable page, dataset vintage/coverage tables, and automated code-version equality check at render time |

### 1.2 coverage contract

- `coverage_n / coverage_total`: unweighted source availability.
- `coverage_fraction`: the explicit numeric form of `n/N`.
- `coverage_ratio`: source availability weighted by the same registry
  weights used by the score.
- Coverage never changes the score. It changes disclosure and presentation
  opacity only.
- NTA count coverage is the ratio of summed metric-cell observations; NTA
  weighted coverage is the mean member-cell weighted coverage.

## 2. Visualization and map

| Item | Status | Current evidence | Missing proof / next gate |
| --- | --- | --- | --- |
| 2.1 ChartSpec + VegaChart | not started | No Vega dependency or ChartSpec schema | Split analysis state from `App.tsx`; deterministic backend spec with `code_ref`; offline renderer |
| 2.2 Rich score cards | partial | Hand-built histogram, uncertainty band and sparklines | Render distribution marker, composition and trends through ChartSpec |
| 2.3 Delta map + compare workbench | partial | Backend owns point deltas; compare bar consumes one comparison response | No spatial delta layer or compare charts |
| 2.4 Server breaks/colors + bivariate | not started | Building domains are server-published; overview uses fixed bands | General class-break service, palette contract, color-vision checks, 3×3 bivariate map |
| 2.5 Timeline | not started | Quarterly data exists | Period-key API contract and MapLibre global-state animation |
| 2.6 Offline HTML export | not started | Existing report templates are not the required Vega self-contained export | Inline data, runtime, method version and generated timestamp |

## 3. Agent and analysis workflow

| Item | Status | Current evidence | Missing proof / next gate |
| --- | --- | --- | --- |
| 3.1 One `/ask` contract | done | FastAPI, Node and React use `/api/agent/ask`; response carries trace/evidence/session | Remove stale comments and keep contract test in full CI |
| 3.2 Typed tool registry | partial | Eight Pydantic argument models and local validation | Availability must be derived from implementation gates; currently experimental tools are exposed prematurely |
| 3.3 Payload policy | not started | No `schema_only/schema+aggregates/+sample` field or AnalysisRun record | Define enum, persisted audit record, enforcement tests |
| 3.4 Intent router | not started | Prompt-only routing | Deterministic local router and out-of-scope short circuit |
| 3.5 Socrata ingestion | partial | Keyset snapshot downloader with partial-file quarantine | Discovery catalog traversal, watermark state, publish transaction and quarantine index |
| 3.6 Profile/semantic inference | partial | Prep-data skills contain profile/clean logic | Controlled backend job, lexical identifier protection tests and NYC spatial-role inference |
| 3.7 Controlled text-to-SQL | not started | Generic dataset filters are deterministic, but no NL→SQL executor | Parsed SELECT-only plan, allowlisted semantic layer, isolated DuckDB process, timeout/resource limits and one repair pass |
| 3.8 Catalog RAG | partial | Ingest/embed/index/retrieve/rerank code and 18-entry catalog | No built index; runtime retrieval is not release-gated; no end-to-end dataset recall evaluation |
| 3.9 Legacy skill consolidation | partial | Several former stubs now call real endpoints | `find_similar` is a watchlist approximation; tool header/docs are stale; unqualified tools remain model-visible |

## 4. Local models

| Item | Status | Current evidence | Missing proof / next gate |
| --- | --- | --- | --- |
| 4.1 Fixed business evaluation set | not started | Unit tests are not a model/business trajectory benchmark | Versioned 20–30 case corpus, expected tools/evidence, replay runner |
| 4.2 FP8/BF16 KV A/B | not started | None | Run on 4.1 with paired quality analysis |
| 4.3 Three-model benchmark | not started | Planning estimates only | Measured tool success, P95, throughput and memory |
| 4.4 Qwen3-VL pilot | not started | None | Co-residency and map screenshot task set |
| 4.5 Production switch | not started | Nano remains default | Parser/reasoning configuration plus all contract/eval gates |

## 5. Performance

| Item | Status | Current evidence | Missing proof / trigger |
| --- | --- | --- | --- |
| 5.1 NVFP4 backend audit | not started | No captured startup log audit | Next LLM start |
| 5.2 vLLM sleep mode | not started | None | Model fleet decision |
| 5.3 Streaming preprocessing | not started | Main preprocessors still materialize pandas | Expansion refresh measurement crosses trigger |
| 5.4 cuVS threshold | deferred by design | Catalog remains tiny | Re-evaluate above one million vectors |
| 5.5 cuSpatial documentation | done | Architecture docs retain GeoParquet, remove cuSpatial motivation | None |
| 5.6 System disk cleanup | operational, not code-complete | Plan reports root pressure | Requires explicit host-maintenance scope |

## Release gates

An item can move to **done** only when all applicable evidence exists:

1. A typed contract and implementation.
2. Success, validation failure, no-data and timeout tests.
3. A publication/version or artifact invalidation rule.
4. Frontend presentation of errors and missingness.
5. End-to-end runtime evidence, not only a unit test.
6. Documentation and this matrix updated in the same commit.

