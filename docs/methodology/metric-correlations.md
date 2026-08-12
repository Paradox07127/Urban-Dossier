# Sub-metric correlation report

Generated 2026-08-11 by `backend/scripts/analyze_metric_correlations.py` (EXPANSION_PLAN item 1.3).

Frame: 7,205 H3 r9 cells (union of H3 r9 cells across all H3 score tables); absent cells are genuine zeros. Statistic: Spearman on zero-filled raw counts. With this many cells every p-value rounds to zero, so magnitudes are the finding, not significance.

## Declared relationships, measured

The metric registry declares two suspect relationships. Both hold:

- `311_sanitation` vs `rodent` -- declared overlap: **rho = +0.897**

## All pairs at |rho| >= 0.7

| pair | rho (counts, zero-filled) | rho (scores, inner join) | inner N | level |
| --- | --- | --- | --- | --- |
| `311_sanitation` / `housing_violations` | +0.933 | +0.893 | 5,460 | collinear |
| `rodent` / `311_sanitation` | +0.897 | +0.834 | 5,670 | high |
| `rodent` / `housing_violations` | +0.866 | +0.777 | 5,281 | high |
| `collision` / `311_sanitation` | +0.740 | +0.661 | 6,137 | high |
| `collision` / `rodent` | +0.721 | +0.627 | 5,722 | high |
| `collision` / `housing_violations` | +0.701 | +0.609 | 5,480 | high |

## ZIP-grain metrics

- `ems_response` vs `fire_response`: rho = +0.269 (N = 171 ZIPs)
- `ems_response` vs `parks_access`: rho = -0.123 (N = 171 ZIPs)
- `fire_response` vs `parks_access`: rho = +0.082 (N = 171 ZIPs)

## Full matrix (Spearman, zero-filled counts)

| | `collision` | `rodent` | `311_sanitation` | `subway` | `bus` | `bike_routes` | `open_streets` | `trees` | `public_toilets` | `linknyc` | `restaurant_context` | `facilities` | `housing_violations` | `aep` |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `collision` | +1.00 | +0.72 | +0.74 | +0.34 | +0.48 | +0.38 | +0.20 | +0.43 | +0.15 | +0.41 | +0.66 | +0.62 | +0.70 | +0.30 |
| `rodent` | +0.72 | +1.00 | +0.90 | +0.29 | +0.39 | +0.31 | +0.22 | +0.57 | +0.14 | +0.39 | +0.58 | +0.59 | +0.87 | +0.39 |
| `311_sanitation` | +0.74 | +0.90 | +1.00 | +0.28 | +0.40 | +0.31 | +0.21 | +0.60 | +0.14 | +0.36 | +0.57 | +0.60 | +0.93 | +0.41 |
| `subway` | +0.34 | +0.29 | +0.28 | +1.00 | +0.24 | +0.21 | +0.12 | +0.11 | +0.09 | +0.35 | +0.39 | +0.31 | +0.27 | +0.15 |
| `bus` | +0.48 | +0.39 | +0.40 | +0.24 | +1.00 | +0.25 | +0.12 | +0.22 | +0.13 | +0.31 | +0.47 | +0.40 | +0.37 | +0.16 |
| `bike_routes` | +0.38 | +0.31 | +0.31 | +0.21 | +0.25 | +1.00 | +0.17 | +0.11 | +0.17 | +0.27 | +0.31 | +0.34 | +0.27 | +0.18 |
| `open_streets` | +0.20 | +0.22 | +0.21 | +0.12 | +0.12 | +0.17 | +1.00 | +0.15 | +0.06 | +0.22 | +0.22 | +0.21 | +0.20 | +0.13 |
| `trees` | +0.43 | +0.57 | +0.60 | +0.11 | +0.22 | +0.11 | +0.15 | +1.00 | +0.05 | +0.21 | +0.32 | +0.29 | +0.60 | +0.23 |
| `public_toilets` | +0.15 | +0.14 | +0.14 | +0.09 | +0.13 | +0.17 | +0.06 | +0.05 | +1.00 | +0.10 | +0.15 | +0.23 | +0.14 | +0.07 |
| `linknyc` | +0.41 | +0.39 | +0.36 | +0.35 | +0.31 | +0.27 | +0.22 | +0.21 | +0.10 | +1.00 | +0.44 | +0.39 | +0.33 | +0.18 |
| `restaurant_context` | +0.66 | +0.58 | +0.57 | +0.39 | +0.47 | +0.31 | +0.22 | +0.32 | +0.15 | +0.44 | +1.00 | +0.56 | +0.54 | +0.25 |
| `facilities` | +0.62 | +0.59 | +0.60 | +0.31 | +0.40 | +0.34 | +0.21 | +0.29 | +0.23 | +0.39 | +0.56 | +1.00 | +0.57 | +0.28 |
| `housing_violations` | +0.70 | +0.87 | +0.93 | +0.27 | +0.37 | +0.27 | +0.20 | +0.60 | +0.14 | +0.33 | +0.54 | +0.57 | +1.00 | +0.43 |
| `aep` | +0.30 | +0.39 | +0.41 | +0.15 | +0.16 | +0.18 | +0.13 | +0.23 | +0.07 | +0.18 | +0.25 | +0.28 | +0.43 | +1.00 |

## Reading the numbers

Everything correlates with everything at rho 0.2-0.6, because every metric is an unnormalized count within a radius and therefore measures activity density before it measures its own phenomenon. That baseline makes the pairs above it stand out more, not less.

The `rodent` / `311_sanitation` / `housing_violations` triangle is the substantive finding: resident complaints, confirmed inspections and open housing violations are three measurements of one underlying condition of the building stock, sitting in two categories under three weights. The registry declared the first pair; the cross-category legs were not declared anywhere and only the measurement found them.

## Decision required (not taken here)

1. `collision_transport` (rho = 1.000 by construction, 19% of `overall` combined with `collision`): drop it and reweight transit, or replace it with an actual transit-risk measure. Keeping it as-is is a decision to double-count collisions, and should be written down as such if taken.
2. The rodent/sanitation/violations triangle: candidate treatments are down-weighting within safety, or merging the two safety metrics into one 'sanitation conditions' signal with two evidence sources. Any change moves published scores and belongs with item 1.4's sensitivity analysis, which can quantify how much.
