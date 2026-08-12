# Sub-metric correlation report

Generated 2026-08-12 by `backend/scripts/analyze_metric_correlations.py` (EXPANSION_PLAN item 1.3).

Frame: 7,196 H3 r9 cells (union of H3 r9 cells across all H3 score tables). Count metrics treat absence as zero; rate metrics keep absence missing and use pairwise-complete cells. Statistic: Spearman on raw values. With this many cells every p-value rounds to zero, so magnitudes are the finding, not significance.

## Declared relationships, measured

The metric registry's declared relationships are measured below:

- `311_sanitation` vs `housing_violations` -- declared overlap: **rho = +0.933**
- `311_sanitation` vs `rodent` -- declared overlap: **rho = +0.258**

## All pairs at |rho| >= 0.7

| pair | rho (raw, metric-aware absence) | rho (scores, inner join) | inner N | level |
| --- | --- | --- | --- | --- |
| `311_sanitation` / `housing_violations` | +0.933 | +0.893 | 5,460 | collinear |
| `collision` / `311_sanitation` | +0.740 | +0.661 | 6,137 | high |
| `collision` / `housing_violations` | +0.700 | +0.609 | 5,480 | high |

## ZIP-grain metrics

- `ems_response` vs `fire_response`: rho = +0.269 (N = 171 ZIPs)
- `ems_response` vs `parks_access`: rho = -0.123 (N = 171 ZIPs)
- `fire_response` vs `parks_access`: rho = +0.082 (N = 171 ZIPs)

## Full matrix (Spearman, raw values with metric-aware absence)

| | `collision` | `rodent` | `311_sanitation` | `subway` | `bus` | `bike_routes` | `open_streets` | `trees` | `public_toilets` | `linknyc` | `restaurant_context` | `facilities` | `housing_violations` | `aep` |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `collision` | +1.00 | +0.27 | +0.74 | +0.34 | +0.48 | +0.38 | +0.20 | +0.43 | +0.15 | +0.41 | +0.66 | +0.61 | +0.70 | +0.30 |
| `rodent` | +0.27 | +1.00 | +0.26 | +0.13 | +0.15 | +0.23 | +0.05 | -0.03 | +0.07 | +0.13 | +0.22 | +0.24 | +0.22 | +0.14 |
| `311_sanitation` | +0.74 | +0.26 | +1.00 | +0.28 | +0.40 | +0.30 | +0.21 | +0.60 | +0.14 | +0.36 | +0.57 | +0.60 | +0.93 | +0.41 |
| `subway` | +0.34 | +0.13 | +0.28 | +1.00 | +0.24 | +0.21 | +0.12 | +0.11 | +0.09 | +0.35 | +0.39 | +0.31 | +0.27 | +0.15 |
| `bus` | +0.48 | +0.15 | +0.40 | +0.24 | +1.00 | +0.25 | +0.12 | +0.22 | +0.13 | +0.31 | +0.47 | +0.40 | +0.37 | +0.16 |
| `bike_routes` | +0.38 | +0.23 | +0.30 | +0.21 | +0.25 | +1.00 | +0.17 | +0.11 | +0.17 | +0.27 | +0.31 | +0.34 | +0.27 | +0.18 |
| `open_streets` | +0.20 | +0.05 | +0.21 | +0.12 | +0.12 | +0.17 | +1.00 | +0.15 | +0.06 | +0.22 | +0.22 | +0.21 | +0.20 | +0.13 |
| `trees` | +0.43 | -0.03 | +0.60 | +0.11 | +0.22 | +0.11 | +0.15 | +1.00 | +0.05 | +0.21 | +0.32 | +0.29 | +0.59 | +0.23 |
| `public_toilets` | +0.15 | +0.07 | +0.14 | +0.09 | +0.13 | +0.17 | +0.06 | +0.05 | +1.00 | +0.10 | +0.15 | +0.23 | +0.13 | +0.07 |
| `linknyc` | +0.41 | +0.13 | +0.36 | +0.35 | +0.31 | +0.27 | +0.22 | +0.21 | +0.10 | +1.00 | +0.44 | +0.39 | +0.33 | +0.18 |
| `restaurant_context` | +0.66 | +0.22 | +0.57 | +0.39 | +0.47 | +0.31 | +0.22 | +0.32 | +0.15 | +0.44 | +1.00 | +0.56 | +0.54 | +0.25 |
| `facilities` | +0.61 | +0.24 | +0.60 | +0.31 | +0.40 | +0.34 | +0.21 | +0.29 | +0.23 | +0.39 | +0.56 | +1.00 | +0.57 | +0.28 |
| `housing_violations` | +0.70 | +0.22 | +0.93 | +0.27 | +0.37 | +0.27 | +0.20 | +0.59 | +0.13 | +0.33 | +0.54 | +0.57 | +1.00 | +0.43 |
| `aep` | +0.30 | +0.14 | +0.41 | +0.15 | +0.16 | +0.18 | +0.13 | +0.23 | +0.07 | +0.18 | +0.25 | +0.28 | +0.43 | +1.00 |

## Reading the numbers

Most count metrics share a positive activity-density baseline: busy, densely observed cells contain more of many phenomena. Rate metrics do not get synthetic zeros outside their observed denominators, so their rows answer a pairwise conditional question instead.

The inspection-anchored `rodent` rate is no longer highly correlated with `311_sanitation` or `housing_violations`; this is the intended v3.9 result. The remaining collinear pair is `311_sanitation` / `housing_violations`, both count surfaces that still share the activity-density baseline and underlying building conditions.

## Decisions

1. Resolved in v3.8: `collision_transport` was removed and transit was reweighted; the measured replacement remains an unregistered candidate.
2. Resolved in v3.9: rodent changed from positive-inspection counts to an inspection failure rate; uninspected cells are missing, not zero.
3. Resolved in v3.9: retain the current weights for the `311_sanitation` / `housing_violations` pair. The relationship is now declared in the registry, but it crosses safety and building, and building has zero overall weight, so housing contributes exactly zero to the current public composite. This is not permission to add building later: before any non-zero overall or priority weight, replace the shared activity-density count with an exposure-adjusted rate or cap the pair as one construct, then rerun correlation and sensitivity.
