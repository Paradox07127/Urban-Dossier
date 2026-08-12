# NYCCAS modeled nitric oxide

Status: **published as context, zero composite weight** in methodology 3.9.0.
This is the first EXPANSION_PLAN 1.5 source to pass the full registration and
publication path. It is not an observed H3-level measurement and is not a
regulatory air-quality reading.

## Source and vintage

- Official dataset: NYC Community Air Survey Air Pollution Rasters,
  `q68s-8qxv`.
- Snapshot: `AnnAvg_1_16_300m.zip`, 8,362,692 bytes, SHA-256
  `7297bc43683d9d7476a8cc6469a58efd13512e6f07cc1ae1cc663bc93499bfdd`.
- Data dictionary: `Data-Dictionary_NYCCAS_March_2026.xlsx`, SHA-256
  `0fd57f8e7c95a7130366d70a5d4e96291100fc7c1bed5a4d3bd2fd7ffd9b4dbe`.
- Selected surface: year 16 nitric oxide (`aa16_no300m/noAA16300m.tif`),
  modeled annual average for December 2023 through December 2024, in ppb.
- Native grid: 157 by 156 pixels, 984 feet (approximately 300 m), New York
  Long Island State Plane, EPSG:2263. Explicit source nodata is -9999.

The raw manifest, archive, dictionary and boundary hashes are checked before
the raster is opened. The pipeline reads the exact archive member and never
extracts the zip tree.

## Spatial and scoring method

The analysis population is every H3 r9 centroid inside the authoritative 2020
NTA land polygons. Each WGS84 centroid is transformed to EPSG:2263 and receives
the value of its containing native raster pixel. There is no interpolation,
area-weighted redistribution or smoothing. H3 r9 is an index and query grain;
it does not increase the roughly 300 m precision of the NYCCAS model, so nearby
cells commonly share a source-pixel estimate.

Of 7,414 land centroids, 7,413 have a valid source value (99.986% coverage).
One centroid falls on official nodata and remains missing. The observed model
range is 3.559208–42.462723 ppb, with median 7.686120 ppb. The public relative
score is the reverse empirical percentile over the 7,413 valid cells: lower
modeled NO receives a higher 0–100 score. Missing is never converted to zero.

The published table is
`data/ready/environment/nyccas_no_scores_h3.parquet`: 7,413 unique rows with
`h3_r9`, `raw_count` (the predicted ppb value) and `score`. Its SHA-256 is
`a10419e2ee7b29bb7269578b4b97d30ec6df2b8ee23fa42dcceadb25a391029c`.
The adjacent manifest pins methodology 3.9.0, source and boundary hashes,
schema, row count, byte size and artifact hash. Runtime detail, coverage,
correlation and sensitivity paths all fail closed when that pair is invalid.

## Registration decision

NYCCAS NO is registered in the new `environment` category so it is queryable,
shown in the point detail card and disclosed on `/methodology`. The category is
context-only, not map-driving or detail-rankable, and contributes exactly 0%
to `overall` in 3.9.0. The UI therefore labels the number as a relative modeled
NO score and does not present it as a measured concentration or health claim.

On the stable 7,194-cell public-composite population, the metric-aware
Spearman audit found its largest existing relationship with collision count
(rho +0.58), followed by facilities (+0.52) and sanitation (+0.48). None
crosses the predeclared |rho| 0.70 review threshold, but the
positive pattern is consistent with a shared activity/density baseline. Zero
overall weight prevents this context layer from silently double-counting that
baseline. The 1,000-draw sensitivity publication lists the score table as a
validated input while its zero weight leaves the 7,194-cell public-composite
population and score unchanged.

Before environment receives any non-zero overall or priority weight, the
product needs an explicit six-dimension weighting decision, a fresh correlation
review with the other environmental/health candidates, and a regenerated
sensitivity publication. One modeled pollutant is not a defensible proxy for
the whole environmental dimension.

## Reproduction

```bash
PYTHONPATH=.:backend/src:backend/scripts python \
  backend/scripts/preprocess_nyccas_no.py \
  --raw-dir /mnt/data/urban-dossier-state/datasets/raw-expansion/nyccas \
  --boundary /mnt/data/Urban-Dossier/data/boundaries/nta_2020.geojson \
  --ready-root /mnt/data/Urban-Dossier/data/ready
```

Then regenerate `metric-correlations.*` and `sensitivity-analysis.*` with the
two scripts under `backend/scripts`. The parquet and manifest are external
ready-layer publications; this document and the reproducible code are tracked.
