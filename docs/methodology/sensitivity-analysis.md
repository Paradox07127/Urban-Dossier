# Sensitivity analysis

Generated 2026-08-12 by `backend/scripts/run_sensitivity_analysis.py` (EXPANSION_PLAN item 1.4). 1,000 Monte Carlo draws over 7,194 H3 r9 cells, seed 20260811.

Each draw simultaneously perturbs sub-metric weights (x U(0.75, 1.25)), switches the normalization (published percentile / min-max / z-score, rebuilt from raw values), toggles the configured overlap metric, and switches the missing-data rule (renormalize vs impute citywide mean). Design constants follow the collected sources: COINr noisy weights at 0.25, CDC PLACES' 1,000-draw 95% interval, the OECD/JRC handbook's method-substitution and exclusion tests.

## Headline numbers

- Median 95% interval width on the 0-100 score: **24.55** points (95th percentile of widths: 39.19). Holding normalization at the production choice narrows the median width to **7.34** -- the difference is the price of the normalization method itself, the rest is weights, the flagged metrics and the missing-data rule.
- Mean absolute rank shift (median-of-draws vs nominal): **550** places out of 7,194 (7.6% of the ranking).
- `collision_transport` was removed in v3.8 and is not toggled in these draws.
- Dropping `311_sanitation` moves it by **0.71** (95th percentile 1.77).
- Imputation vs renormalization: mean absolute difference **7.29** points.
- Citywide mean composite under each normalization: percentile 53.02, minmax 47.35, zscore 50.03 -- the level differences are why scores must state their method version.

## What this licenses

Per-cell intervals and rank ranges are in `data/ready/analysis/sensitivity_cells.parquet` (untracked, regenerable with the seed). A published score can now carry its interval, and a rank claim ('safer than X% of the city') its range -- the acceptance criterion for item 1.4. The live toggle effect above quantifies the retained sanitation/rodent construct decision. The cross-category sanitation/housing overlap is excluded because building has zero overall weight; making that weight non-zero first requires exposure adjustment or a shared-construct cap and a fresh sensitivity run.

Publication is atomic: `sensitivity_cells.parquet` is paired with `sensitivity_cells.manifest.json`, which records methodology version, draw count, seed, row/schema checks, artifact SHA-256 and every input score-table SHA-256. The API fails closed when either file or any input snapshot changes. Its public headline maps the production-normalization 95% interval across fixed 20-point tiers; the point estimate remains secondary detail.

## Stated limits

- ZIP-grain metrics (EMS, fire, parks and HVI) are repeated over H3 cells through the nearest-parcel ZIP lookup used by production. Their registry and UI grain remains ZIP; this allocation is only the explicit cell-level surface required for a like-for-like composite.
- `restaurant_context`'s inspection-quality adjustment exists only in the percentile branch; min-max and z rebuild from counts alone.
- Absence from a sparse risk table (`aep`: 586 cells citywide) is treated as missing, as production does. Whether absence should score as good news for count-of-bad-thing metrics is an open product question this analysis surfaces but does not settle.
- The `building` category's weight is 0.0 nominally and stays 0 under multiplicative noise -- the degenerate case the weight-sensitivity literature warns about. Deciding building's status (PROJECT_PLAN P0-02) is prerequisite to including it here meaningfully.
