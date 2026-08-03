# Urban Dossier Preprocessing Scripts

These scripts implement the `v3.7.8` ready-first path.

The cross-platform layer contract, publication gate, and DGX/workstation
adapter boundary are defined in [`../../DATA_ARCHITECTURE.md`](../../DATA_ARCHITECTURE.md).

## Goal

For each direction-specific raw dataset, produce:

1. an indexed parquet with normalized columns and H3 where applicable
2. a score table used by runtime scoring
3. a quarterly trend table where applicable

## Environment

Expected raw input root:

- `URBAN_DOSSIER_RAW_DATA_ROOT`
- defaults to `~/nyc_open_data`

The preprocessing path reads the immutable categorized CSV snapshot directly
(`safety/`, `transit/`, `amenities/`, and so on). It does not create a second
29 GB tree of `*.cleaned.csv` files. Normalization, filtering, H3 indexing, and
type conversion happen while producing the ready Parquet layer.

Expected ready output root:

- `URBAN_DOSSIER_READY_ROOT`
- defaults to `./data/ready` relative to the repo root

Install preprocessing dependencies with:

```bash
uv pip install --python .venv/bin/python -r backend/preprocess_requirements.txt
```

Conda is not required for the workstation preprocessing path. The production
`urban-dossier` OpenClaw agent is intentionally analysis-only; dataset ingestion
must run as an explicit host job so input, output, and failures remain auditable.

## Implemented now

- safety collisions
- safety rodent
- safety 311
- safety EMS
- safety Fire
- amenities restaurants
- amenities parks
- amenities trees
- amenities LinkNYC
- amenities toilets
- amenities facilities
- transit subway
- transit bus shelters
- building violations
- building AEP
- location PLUTO index
- baseline generation
- bike routes and Open Streets WKT line-vertex extraction

The workstation publication uses ZSTD level 3, 250,000-row Parquet row groups,
dictionary encoding, statistics, and atomic `.part` replacement. Override these
only after benchmarking with `URBAN_DOSSIER_PARQUET_*` environment variables.

Validate the immutable raw snapshot and the complete ready publication with:

```bash
.venv/bin/python scripts/audit_datasets.py /mnt/data/urban-dossier/datasets/raw \
  --output /mnt/data/urban-dossier/datasets/manifests/raw-audit.json

.venv/bin/python scripts/validate_ready_parquet.py /path/to/data/ready \
  --output /mnt/data/urban-dossier/datasets/manifests/ready-audit.json
```

## Gold overview and NTA publication

The overview is a separate publication gate: valid raw and ready layers do not
by themselves make `overview_ready=true`. From the repository root, download
the official boundary and build H3 before NTA:

```bash
bash scripts/maps/download_nta_2020.sh

.venv/bin/python backend/scripts/build_overview_tiles.py \
  --ready-root data/ready --overview-root data/cache/overview

.venv/bin/python backend/scripts/build_overview_nta.py \
  --nta-path data/boundaries/nta_2020.geojson \
  --overview-root data/cache/overview
```

`build_overview_tiles.py` writes the four `overview_*_h3_r8.parquet` layers.
`build_overview_nta.py` spatially assigns H3 centroids to NYC Planning polygons
and writes Parquet plus compact JSON for Node. It accepts the current official
`NTA2020`, `NTAName`, `BoroName`, and `NTAType` properties as well as legacy
lowercase snapshots. Zones without a directly scored H3 cell remain absent;
do not silently fill them without recording an imputation method.

## Pensar-oriented smoke test

Use `pensar_smoke_test.py` after starting the backend to validate the hardening assumptions that matter for the
"Least Likely to get Hacked" prize:

- invalid overview categories are rejected
- invalid radii are rejected
- watchlist requests are capped
- optional demo token protection works when enabled

Example:

```bash
python backend/scripts/pensar_smoke_test.py
```

To test token-protected mode:

```bash
export URBAN_DOSSIER_DEMO_TOKEN=your-token
python backend/scripts/pensar_smoke_test.py
```
