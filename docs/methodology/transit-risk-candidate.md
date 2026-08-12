# transit_risk: a candidate metric, measured and not registered

2026-08-11. Status: **built, gated, declined**. The score table exists in the
exploration layer (`transit/transit_risk_scores_h3.parquet`); it does not
enter the composite.

## What was built

The successor candidate for the removed `collision_transport`, constructed to
NYC DOT's own Vision Zero Priority Corridor method (pedestrian KSI per
street-mile, 2023 BPSAP update) and FHWA-SA-18-032's exposure guidance:

    rate(cell) = [3 x (ped+cyc killed) + 1 x (ped+cyc injured)] / street_miles

over the trailing five years of MVC data (2021-06-11 to 2026-06-11; upstream
updates are paused, window anchored to newest crash on file), with street
miles from the CSCL centerline (`inkn-q76z`; streets, bridges and alleys only
-- no limited-access highways, where the numerator cannot occur). 65,394
casualty crashes over 7,185 street-bearing cells. Full construction and every
parameter: `backend/scripts/preprocess_transit_risk.py` and the manifest
beside the table.

## Why it was declined

The registration gate was the same measurement that removed its predecessor:

| view | rho vs `collision` (safety) |
| --- | --- |
| zero-filled raw values, union frame (N=7,292) | **+0.832** |
| published scores, inner join (N=6,616) | **+0.803** |

Both views sit above the 0.75 double-counting threshold the collected COINr
guidance uses for within-group correlation. The construction is genuinely
different -- casualty subset, severity weights, exposure denominator -- but at
H3 r9 in this city it ranks places almost the way raw collision counts do.
Registering it at transit 0.30 would hand the collisions dataset a combined
21.5% of `overall` (0.125 + 0.09) across two categories at rho 0.83: the
double count of v3.7.8 reduced by degree, reintroduced by name.

For calibration, its correlations with genuinely distinct metrics: subway
+0.35, bus +0.49. The problem is specific to the collision pair.

Transit therefore stays a four-metric access category, as shipped in v3.8.0.

## What would change the answer

- **A real exposure denominator.** Street miles are supply, not usage.
  Pedestrian volumes (`cqsj-cfgu` bi-annual counts are sparse point samples,
  not a surface) or transit ridership would decorrelate the rate from ambient
  density, which is what the 0.83 mostly is.
- **KSI-only numerator.** Killed + severely injured is the standard signal,
  but public MVC data has no severity grades below killed, and killed alone
  (~1,200 events over five years across 7,000+ cells) is too sparse to rank
  at r9. Viable at NTA grain if transit risk is wanted as an NTA-view metric.
- **DOT's own designations.** Share of a cell's street miles on a 2023
  Priority Corridor (`kdda-2wcy`) outsources the methodology to DOT entirely;
  vintage-2023 snapshot, untested correlation. The zero-modelling fallback.

## The general lesson, recorded

This is the registration pipeline working as designed: candidate metrics get
measured against the registry before they get weights, and a plausible,
officially-blessed construction still failed the gate. The measurement lives
here rather than in a chat log so the next attempt starts from the numbers.
