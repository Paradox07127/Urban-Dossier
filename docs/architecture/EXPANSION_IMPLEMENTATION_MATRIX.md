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
| 1.3 Correlation analysis | done | Reproducible metric-aware Spearman report; copied collision metric removed; rodent changed to inspection-positive rate; the 0.933 sanitation/housing overlap is registry-declared and weights are retained because housing's overall contribution is exactly zero | Before building receives any overall or priority weight, replace the shared activity-density count with an exposure-adjusted rate or cap one shared construct, then rerun correlation and sensitivity |
| 1.4 Sensitivity pipeline | done | Seeded 1,000-draw artifact; API score/rank intervals and histogram; atomic manifest validates 3.9.0, artifact schema/rows/SHA and all 14 input score-table snapshots with stat-driven cache invalidation; backend maps the production-method 95% interval to fixed 20-point public tiers, while UI and offline HTML demote the point estimate to secondary detail | Regenerate the parquet+manifest pair after any input, method or metric-registry change; never publish a tier from an unverified artifact |
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
| 2.1 ChartSpec + VegaChart | done | Versioned backend ChartSpec carries `code_ref` and methodology; local Vega bundle compiles all specs; Chromium smoke renders SVG with zero external requests; Compare state moved to a feature hook | Keep spec values backend-owned and add every new chart to the offline smoke path |
| 2.2 Rich score cards | done | Deterministic ChartSpecs cover every non-missing public score and quarter-keyed trend; the city histogram, containing-cell marker, midrank percentile and 95% sensitivity interval share the same H3 r9 analysis population; the no-artifact fallback uses the land-clipped H3 r8 overview without layering a mismatched interval; offline Chromium renders all charts with zero external requests | Keep grain and score-method labels explicit when adding distributions; never layer intervals from a different population |
| 2.3 Delta map + compare workbench | partial | Backend now publishes versioned two-radius GeoJSON, bbox, B-minus-A fields and a color-vision-safe PuOr diverging contract; MapLibre renders A baseline, B delta, connector and endpoints; workbench chips/legend and grouped ChartSpec consume the same response without frontend subtraction; Chromium verifies five features, four layers and the backend field expression | Add the planned `@maplibre/maplibre-gl-compare` swipe view as an auxiliary comparison mode; the primary delta-map acceptance gate is complete |
| 2.4 Server breaks/colors + bivariate | done | Versioned API publishes land-clipped H3 r8 quantile breaks, shared map/chart/legend colours and numerical CVD gates; backend-classified Safety × Transit GeoJSON uses the Stevens 3×3 matrix; offline Chromium verifies 985 joined cells, property-driven fill and zero external requests | Recompute and test the contract whenever overview artifacts or palettes change; add new metric pairs only when both populations and accessibility gates pass |
| 2.5 Timeline | done | P0-03 now preserves `{period, value, coverage, period_complete}` from Gold through trends/charts/evidence; anomaly and persistence disclose sample/missingness policies and exclude partial quarters; pattern Spearman uses a period-key inner join; versioned timeline GeoJSON publishes 20 real collision quarters over 1,154 land-clipped H3 r8 cells with per-period server classes; Chromium verifies a `global-state.timeline_period` expression, one-property tick mutation, one data request and zero external requests | Add signals to the UI only after their source-specific absence-means-zero and temporal-completeness policies are documented |
| 2.6 Offline HTML export | done | `POST /api/export/html` validates the current 3.9.0 ChartSpecs, rejects external data URLs and builds an escaped attachment with inline Vega, Vega-Lite, Vega-Embed, chart data, evidence, method version and server generation time; the detail UI downloads the Blob; Chromium opens those exact bytes from `file://`, renders three visible SVGs and observes zero HTTP(S) requests | Keep the runtime versions pinned to installed frontend dependencies and add new public charts to the same real-Blob offline gate |

## 3. Agent and analysis workflow

| Item | Status | Current evidence | Missing proof / next gate |
| --- | --- | --- | --- |
| 3.1 One `/ask` contract | done | FastAPI, Node and React use `/api/agent/ask`; response carries trace/evidence/session | Remove stale comments and keep contract test in full CI |
| 3.2 Typed tool registry | done | Eight stable Pydantic schemas validate arguments locally; each Agent run publishes only the artifact-gated subset, narrows simulation enums to fitted interventions, and exposes sanitized decisions in Agent status | Keep new tools closed until their implementation and artifact gate are both tested |
| 3.3 Payload policy | not started | No `schema_only/schema+aggregates/+sample` field or AnalysisRun record | Define enum, persisted audit record, enforcement tests |
| 3.4 Intent router | not started | Prompt-only routing | Deterministic local router and out-of-scope short circuit |
| 3.5 Socrata ingestion | partial | Keyset snapshot downloader with partial-file quarantine | Discovery catalog traversal, watermark state, publish transaction and quarantine index |
| 3.6 Profile/semantic inference | partial | Prep-data skills contain profile/clean logic | Controlled backend job, lexical identifier protection tests and NYC spatial-role inference |
| 3.7 Controlled text-to-SQL | not started | Generic dataset filters are deterministic, but no NL→SQL executor | Parsed SELECT-only plan, allowlisted semantic layer, isolated DuckDB process, timeout/resource limits and one repair pass |
| 3.8 Catalog RAG | partial | Ingest/embed/index/retrieve/rerank code and 18-entry catalog | No built index; runtime retrieval is not release-gated; no end-to-end dataset recall evaluation |
| 3.9 Legacy skill consolidation | partial | Former stubs call real endpoints; tool docs and the 18-source count now match runtime; unreleased tools are hidden | Replace the watchlist approximation with a dedicated score-vector similarity implementation and remove its legacy route |

## 4. Local models

| Item | Status | Current evidence | Missing proof / next gate |
| --- | --- | --- | --- |
| 4.1 Fixed business evaluation set | done | Versioned 24-case corpus covers four business intents; order-aware tool/evidence scorer supports live collection and deterministic JSONL replay, with corpus hash in each report | Run the fixed corpus for every 4.2/4.3 model configuration and retain result artifacts |
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
