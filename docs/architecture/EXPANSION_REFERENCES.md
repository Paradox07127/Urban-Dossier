# Expansion architecture references and decisions

> Reviewed: 2026-08-12. Primary and official sources are preferred. These
> references constrain implementation; they are not decorative bibliography.

## Composite indicators and missing data

- [OECD/JRC Handbook on Constructing Composite Indicators](https://www.oecd.org/en/publications/handbook-on-constructing-composite-indicators-methodology-and-user-guide_9789264043466-en.html)
  treats missing-data handling, multivariate analysis, uncertainty and
  sensitivity as explicit construction stages. It warns that complete-case
  deletion is unbiased only under a missing-completely-at-random assumption.
- [COINr missing data and imputation](https://bluefoxr.github.io/COINrDoc/missing-data-and-imputation.html)
  separates availability from the composite result and notes that excessive
  missingness can make an analysis meaningless.
- [COINr sensitivity analysis](https://bluefoxr.github.io/COINrDoc/sensitivity-analysis.html)
  distinguishes output uncertainty (score/rank distributions) from
  sensitivity (which modelling choices drive that uncertainty).

Decision: Urban Dossier publishes both unweighted `n/N` and weighted evidence
coverage. Missingness never silently becomes zero and coverage does not alter
the point estimate. Sensitivity artifacts publish both score and rank ranges.

## Deterministic visualization

- [Vega-Lite](https://vega.github.io/vega-lite/docs/) is a declarative JSON
  grammar compiled to Vega; its [view specification](https://vega.github.io/vega-lite/docs/spec.html)
  supports an explicit schema reference.
- [vega-embed](https://github.com/vega/vega-embed) is the official
  framework-independent browser embedder.
- [MapLibre GL JS](https://maplibre.org/maplibre-gl-js/docs/) renders
  data-driven style expressions on the GPU. Its style-layer implementation
  tracks global-state references so a global value can invalidate dependent
  layers without per-feature state mutation.

Decision: the backend owns data, class breaks, palette identifiers and
ChartSpec JSON. React mounts specs but does not derive analytical values.
MapLibre consumes presentation properties; it does not recompute scores.

## Controlled DuckDB analysis

- [DuckDB security guidance](https://duckdb.org/docs/current/operations_manual/securing_duckdb/overview)
  explicitly says untrusted SQL has the power of shell/Python code and
  recommends isolation, timeouts, resource constraints, disabled external
  access and extension restrictions.
- Prepared statements protect untrusted **values**, not an untrusted query
  structure. A string-prefix `SELECT` check is not a sufficient read-only
  authorization boundary.

Decision: planned text-to-SQL will target an allowlisted semantic query model,
compile that model to SQL locally, and execute it in a restricted process.
The model will not submit arbitrary SQL directly to the production DuckDB
connection. Query structure, identifiers and aggregates must all pass local
validation; values use parameters.

## Dataset discovery and quarantine

- [Socrata Discovery](https://dev.socrata.com/docs/other/discovery) supplies
  catalog metadata.
- [SODA queries](https://dev.socrata.com/docs/queries/) support bounded,
  ordered retrieval used by the existing keyset downloader.

Decision: discovery metadata is exploratory until a snapshot completes
row/schema/hash audit. Failed or changing downloads remain quarantined and
cannot replace the current published snapshot.

## Model selection

External benchmark claims are candidate-screening evidence only. Production
selection requires the repository's fixed business evaluation set, including
expected tool sequence, evidence grounding, refusal/out-of-scope behavior,
latency and memory. No model switch is valid before EXPANSION_PLAN 4.1.

