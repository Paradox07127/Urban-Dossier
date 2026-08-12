# Sensitivity analysis

Generated 2026-08-11 by `backend/scripts/run_sensitivity_analysis.py` (EXPANSION_PLAN item 1.4). 1,000 Monte Carlo draws over 7,196 H3 r9 cells, seed 20260811.

Each draw simultaneously perturbs sub-metric weights (x U(0.75, 1.25)), switches the normalization (published percentile / min-max / z-score, rebuilt from raw counts), toggles the two metrics the correlation report flagged, and switches the missing-data rule (renormalize vs impute citywide mean). Design constants follow the collected sources: COINr noisy weights at 0.25, CDC PLACES' 1,000-draw 95% interval, the OECD/JRC handbook's method-substitution and exclusion tests.

## Headline numbers

- Median 95% interval width on the 0-100 score: **33.08** points (95th percentile of widths: 57.6). Holding normalization at the production choice narrows the median width to **10.0** -- the difference is the price of the normalization method itself, the rest is weights, the flagged metrics and the missing-data rule.
- Mean absolute rank shift (median-of-draws vs nominal): **596** places out of 7,196 (8.3% of the ranking).
- Dropping `collision_transport` moves a cell's score by **n/a** points on average (95th percentile n/a).
- Dropping `311_sanitation` moves it by **1.19** (95th percentile 2.79).
- Imputation vs renormalization: mean absolute difference **10.84** points.
- Citywide mean composite under each normalization: percentile 50.55, minmax 42.28, zscore 50.12 -- the level differences are why scores must state their method version.

## What this licenses

Per-cell intervals and rank ranges are in `data/ready/analysis/sensitivity_cells.parquet` (untracked, regenerable with the seed). A published score can now carry its interval, and a rank claim ('safer than X% of the city') its range -- the acceptance criterion for item 1.4. The toggle effects above are the quantified cost of the two decisions item 1.3 posed; they turn 'should we drop the duplicated metric?' into 'dropping it moves scores by N points on average', which is a decision a product owner can actually take.

## Stated limits

- ZIP-grain metrics (ems, fire, parks) are held out, matching how production renormalizes when a ZIP lookup fails; their weight share is redistributed identically in every draw.
- `restaurant_context`'s inspection-quality adjustment exists only in the percentile branch; min-max and z rebuild from counts alone.
- Absence from a sparse risk table (`aep`: 586 cells citywide) is treated as missing, as production does. Whether absence should score as good news for count-of-bad-thing metrics is an open product question this analysis surfaces but does not settle.
- The `building` category's weight is 0.0 nominally and stays 0 under multiplicative noise -- the degenerate case the weight-sensitivity literature warns about. Deciding building's status (PROJECT_PLAN P0-02) is prerequisite to including it here meaningfully.
