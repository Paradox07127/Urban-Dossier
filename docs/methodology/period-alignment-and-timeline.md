# Period alignment and map timeline

Quarter labels are data keys, not display labels inferred from array position.
The public quarterly point contract is `{period, value, coverage,
period_complete}`, where `period` is canonical `YYYY-Qn`, `coverage` is the
fraction of requested H3 cells represented by the Gold aggregate, and
`period_complete` says whether the calendar quarter has ended. Missing periods
remain missing; the service does not pad or relabel an array relative to today.

## Statistical policies

- Anomaly detection compares the latest complete quarter with prior complete,
  observed quarters using a population-standard-deviation z-score. It requires
  at least four observations and publishes the sample size, minimum, method,
  latest period, and listwise-exclusion policy.
- Persistence counts adjacent complete calendar quarters above the configured
  threshold. A missing quarter breaks the run.
- Pattern correlation uses Spearman rank correlation after an inner join on
  exact period keys. It requires four paired quarters, applies the existing
  Bonferroni family-wise correction, and publishes sample size, period range,
  alignment method, and pairwise-complete missing-data policy.
- The current partial quarter remains visible in charts and timeline metadata,
  but cannot trigger anomaly, persistence, or correlation findings.

## Dataset publication gate

Preprocessing accepts observed dates from 2000-01-01 through the run date.
Placeholder years, malformed keys, and future event dates become null before
grouping. `validate_ready_parquet.py` independently rejects any quarterly Gold
row outside `YYYYQ1`–`YYYYQ4`, before 2000, or after the current quarter.

The 2026-08-12 audit found 1,365 invalid restaurant-quarter rows and 46,102
invalid housing-violation rows. `rebuild_quarterly_artifacts.py` rebuilt those
two derived tables atomically from their filtered indexed artifacts; the full
43-file ready-layer audit then passed with zero invalid files and no partial
publication files. The rebuild keeps explicit backups before replacement.

## Map timeline

`GET /api/timeline` aggregates the selected H3 r9 quarterly event artifact to
the land-clipped Safety H3 r8 population. Absent event rows are observed zeros
for these count datasets. Every period publishes its own quantile breaks,
effective class count, standard d3/ColorBrewer colours, total, and exact value
and colour property names. The response cache key includes artifact size and
mtime, so replacing a Gold file invalidates the cached timeline.

The collision view currently publishes the latest 20 real quarters across
1,154 cells. MapLibre's fill expression is a `match` on
`global-state.timeline_period`; each branch reads the server-classified colour
property for that exact key. Play and slider ticks call only
`setGlobalStateProperty('timeline_period', period)`. They do not refetch data,
walk features, or use an array index as analytical identity.
