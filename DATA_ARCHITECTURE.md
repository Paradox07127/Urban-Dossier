# Urban Dossier Data Architecture

> Shared contract for `mac`, `cuda-x86`, and `dgx-spark`. Hardware profiles may
> choose different execution adapters, but they must publish the same logical
> datasets, schemas, score semantics, and audit manifests.

Baseline: v3.7.8 data pipeline, updated 2026-08-02.

## 1. Changes from the original DGX layout

The original DGX checklist assumed a flat `~/nyc_open_data` directory, a second
tree of `*.cleaned.csv` files, optional in-process cuDF for serving, and cuVS as
a deployment success criterion. That is no longer the project contract.

The current architecture changes are:

1. Raw downloads are immutable, categorized CSV snapshots rather than a flat
   working directory.
2. Every snapshot must pass schema and full-file parsing before publication;
   malformed or superseded downloads go to `quarantine/`.
3. Normalization writes directly from raw CSV to typed Parquet. The duplicate
   multi-gigabyte `*.cleaned.csv` tree has been removed.
4. The ready layer has a stable Parquet physical contract: ZSTD level 3,
   250,000-row row groups, dictionary encoding, row-group statistics, and
   atomic `.part` replacement.
5. Online FastAPI queries use compact Gold tables first and ready indexed
   Parquet for evidence. Raw CSV is provenance/fallback, not the normal Agent
   query path.
6. DuckDB is the reference serving engine on every profile. RAPIDS/cuDF is an
   optional batch adapter selected only after a platform-local benchmark.
7. The catalog/document vector index is independent from analytical facts.
   Current small-corpus retrieval does not require cuVS on DGX or x86.

These are data-contract changes, not x86-only tuning changes. A DGX Spark build
must reproduce them before its data layer is considered compatible.

## 2. Canonical layers

```text
Bronze
  immutable categorized CSV
  source/download metadata
  raw-audit.json
  quarantine for failed or superseded exports
      |
      v
Silver
  normalized typed indexed Parquet
  NYC coordinate clipping
  H3 r9 and/or ZIP keys
  status and business-validity filters
      |
      v
Gold
  H3/ZIP score tables
  quarterly trends
  baselines
  overview cells/vector tiles
      |
      v
Serving
  DuckDB reads Gold first and Silver for evidence
  FastAPI owns deterministic analysis semantics
  Agent receives compact evidence plus provenance
```

Parquet is the canonical analytical interchange format. It is not Agent
memory. Dataset descriptions, field meanings, update cadence, source URLs, and
sample queries remain in `rag/catalog.json`. Analysis results must eventually
carry `snapshot_id`, methodology version, and evidence references.

## 3. Portable storage layout

The physical root is profile-specific; the relative structure is shared:

```text
<state-root>/datasets/raw/
  safety/
  environment/
  quality_of_life/
  transit/
  amenities/
  buildings/

<state-root>/datasets/manifests/
  raw-audit.json
  ready-audit.json

<state-root>/datasets/quarantine/

<repo>/data/ready/
  safety/
  transit/
  amenities/
  building/
  location/
  baselines/

<repo>/data/boundaries/
  nta_2020.geojson
  nta_2020.layer.json
  nta_2020_metadata_26B.pdf
  nta_2020.manifest.json
```

Recommended roots:

| Profile | Raw state | Published ready layer |
| --- | --- | --- |
| x86 workstation | `/mnt/data/urban-dossier/datasets/raw` | `/mnt/data/Urban-Dossier/data/ready` |
| DGX Spark | `${HOME}/urban-dossier-data/datasets/raw` or dedicated NVMe | `<repo>/data/ready` |
| Mac | external/local data root | `<repo>/data/ready` |

Set `URBAN_DOSSIER_RAW_DATA_ROOT` and `URBAN_DOSSIER_READY_ROOT` explicitly in
each deployment profile. Do not encode workstation paths into the DGX profile.

For example, from a DGX checkout:

```bash
export URBAN_DOSSIER_STATE_ROOT="${HOME}/urban-dossier-data"
export URBAN_DOSSIER_RAW_DATA_ROOT="${URBAN_DOSSIER_STATE_ROOT}/datasets/raw"
export URBAN_DOSSIER_READY_STAGING="${URBAN_DOSSIER_STATE_ROOT}/data/ready-staging"
export URBAN_DOSSIER_READY_ROOT="${PWD}/data/ready"
```

## 4. Snapshot ingestion and validation

Download into the selected raw root:

```bash
bash scripts/download_datasets.sh "${URBAN_DOSSIER_RAW_DATA_ROOT}"
```

The catalog currently contains 18 exact source datasets. A completed download
is not publishable until the full raw audit passes:

```bash
.venv/bin/python scripts/audit_datasets.py \
  "${URBAN_DOSSIER_RAW_DATA_ROOT}" \
  --output "${URBAN_DOSSIER_STATE_ROOT}/datasets/manifests/raw-audit.json"
```

The expected result is 18 actual CSVs with status counts `ok=18`,
`incompatible=0`, `invalid=0`, and `missing=0`.

Large Socrata tables that cannot complete a bulk export must use
`scripts/download_socrata_snapshot.py`. It validates every page, enforces a
strictly increasing key, checkpoints progress, and rejects snapshot metadata
drift. A `.part` file must never be renamed into the raw tree before a strict
full-file parse succeeds.

The Gold NTA rollup uses NYC Planning's official 2020 NTA polygon layer. Fetch
the current boundary, ArcGIS layer definition, and official metadata with:

```bash
bash scripts/maps/download_nta_2020.sh
```

The current pinned release is 26B (May 2026). The downloader requests WGS84
GeoJSON, validates polygon geometry and unique NTA codes, and records source
URLs, hashes, sizes, field names, and feature count in
`data/boundaries/nta_2020.manifest.json`. Downloaded boundary artifacts remain
outside Git and must be reproduced on each deployment host.

## 5. Cleaning and publication

Install the CPU reference preprocessing environment with `uv`:

```bash
uv pip install --python .venv/bin/python -r backend/preprocess_requirements.txt
```

Run the processors against a staging directory:

```bash
for dataset in \
  safety_collisions safety_rodent safety_311 safety_ems safety_fire \
  amenities_restaurants amenities_parks amenities_trees \
  amenities_linknyc amenities_toilets amenities_facilities \
  transit_subway transit_bus transit_bike_routes transit_open_streets \
  building_violations building_aep location_pluto
do
  .venv/bin/python backend/scripts/preprocess_common.py "${dataset}" \
    --raw-root "${URBAN_DOSSIER_RAW_DATA_ROOT}" \
    --ready-root "${URBAN_DOSSIER_READY_STAGING}"
done

.venv/bin/python backend/scripts/preprocess_common.py baselines \
  --ready-root "${URBAN_DOSSIER_READY_STAGING}"
```

Normalize and validate the physical Parquet publication:

```bash
.venv/bin/python scripts/optimize_parquet.py \
  "${URBAN_DOSSIER_READY_STAGING}"

.venv/bin/python scripts/validate_ready_parquet.py \
  "${URBAN_DOSSIER_READY_STAGING}" \
  --output "${URBAN_DOSSIER_STATE_ROOT}/datasets/manifests/ready-audit.json"
```

Publication is an atomic directory rename only after `ready-audit.json` reports
44/44 expected Parquet files, zero invalid files, zero missing/extra files, and
zero `.part` files. Retain the previous published directory until the backend
smoke test passes so rollback is a rename rather than a re-ingest.

## 6. Shared cleaning semantics

These rules are shared by Mac, x86, and DGX Spark:

| Dataset | Published rule |
| --- | --- |
| 311 safety | `RODENT`, `SANITATION CONDITION`, and `UNSANITARY CONDITION` |
| Rodent | positive/active findings only |
| Street trees | `status = Alive` |
| LinkNYC | `Installation Status = Live` |
| Public restrooms | `Status = Operational` |
| Bike routes | `status = Current` |
| Restaurants | access is distinct `CAMIS`; `inspection_count` is the quality denominator |
| Housing violations | exclude closed violations |
| AEP | exclude discharged buildings |
| Point/line data | clip to the configured NYC bounding box before H3 indexing |

Source replacements recorded in the current catalog include MTA subway
entrances `i9wp-a4ja`, bus shelters `t4f2-8md7`, bike routes `mzxg-pwib`, Parks
Properties `enfh-gkve`, and Public Restrooms `i7jb-7jku`.

## 7. Platform execution adapters

| Concern | Mac | x86 workstation | DGX Spark |
| --- | --- | --- | --- |
| Online queries | DuckDB | DuckDB | DuckDB reference path |
| Batch dataframe | pandas/Polars | isolated RAPIDS container when justified | RAPIDS ARM64/container/conda only after GB10 validation |
| GPU serving requirement | none | none for data API | none for data API |
| Vector index for current catalog | CPU exact/FAISS | CPU exact/FAISS is sufficient | CPU exact/FAISS is sufficient |
| Large future vector corpus | CPU HNSW/remote option | evaluate cuVS | evaluate cuVS |
| Distributed dataframe | not used | not used | not used on single GB10 unless profiling proves need |

Unified memory on DGX Spark can reduce CPU/GPU transfer costs, but it does not
change dataset schemas, score definitions, manifests, or publication gates.
Do not turn cuDF/cuVS availability into a correctness condition.

## 8. Current validated reference snapshot

The x86 workstation snapshot validated on 2026-08-02 contains:

- 18/18 raw CSVs;
- 82,376,420 raw rows and 33.58 GB;
- 44/44 ready Parquet files and 290,015,868 bytes;
- NYC Planning NTA 2020 release 26B with 262 validated boundary features;
- four H3 r8 overview layers (1,171-1,232 cells) and four NTA score layers
  (248-251 directly scored NTAs, depending on category);
- ZSTD/250,000-row-group compatibility tested with
  `nvcr.io/nvidia/rapidsai/base:26.06-cuda13-py3.12`;
- ready-first FastAPI preview latency of approximately 0.45-0.47 seconds after
  warmup, compared with 2.2-2.5 seconds when evidence queries scanned raw CSV.

This is a reference result, not a manifest that can be copied to DGX. The DGX
host must generate its own manifests from its own downloaded snapshot.

## 9. Next data work

1. Decide how the UI should represent NTAs without a directly scored H3 cell
   (currently 11-14 boundary features depending on category): leave unfilled,
   show an explicit no-data style, or publish a separately marked imputation.
2. Sort or coarsely partition large indexed facts by `h3_r9` and
   `event_date` to improve row-group pruning. Do not directory-partition by the
   full high-cardinality H3 value.
3. Replace full pandas materialization for recurring large refreshes with
   DuckDB or Polars streaming; benchmark cuDF-Polars independently per CUDA
   profile.
4. Add GeoParquet metadata for line/polygon sources before adopting cuSpatial.
5. Embed catalog/methodology documents, not millions of structured fact rows.
