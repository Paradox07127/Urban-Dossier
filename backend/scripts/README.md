# Urban Dossier Preprocessing Scripts

These scripts implement the `v3.7.8` ready-first path.

## Goal

For each direction-specific dataset, produce:

1. an indexed parquet with normalized columns and H3 where applicable
2. a score table used by runtime scoring
3. a quarterly trend table where applicable

## Environment

Expected raw input root:

- `URBAN_DOSSIER_RAW_DATA_ROOT`
- defaults to `~/nyc_open_data`

Expected ready output root:

- `URBAN_DOSSIER_READY_ROOT`
- defaults to `./data/ready` relative to the repo root

Install preprocessing dependencies with:

```bash
pip install -r backend/preprocess_requirements.txt
```

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

## Deferred for the next pass

These need geometry-aware preprocessing rather than point/ZIP-only normalization:

- bike routes
- open streets polygons/segments

The current backend runtime can still use direct fallback queries while those scripts are being added.

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
