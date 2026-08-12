# Public methodology publication

`/methodology` is a shareable SPA entry point that mounts without the map
application. It renders a live statistical-audit view: metric sources and
vintages, original spatial/temporal grain, normalization, configured weights,
and the active provider's required/available/missing prepared datasets.

The page does not fetch the raw registry directly. `GET /api/methodology`
loads `/api/metrics` and `/api/coverage` for each request, then fails closed
unless all of the following are true:

1. the registry methodology version equals Node's
   `EXPECTED_METHODOLOGY_VERSION`;
2. every individual metric carries that same version;
3. dataset coverage is a complete, unambiguous partition of the required
   prepared collections.

The frontend repeats the equality assertion before rendering. A mismatch is
shown as "Methodology publication withheld"; stale definitions are never
presented as current. `backend/tests/test_overview_artifacts.py` separately
pins the Node expected version to Python's `METHODOLOGY_VERSION`, so the gate
itself cannot silently drift.

The existing score-card modal consumes the same verified publication and links
to the stable `/methodology` URL. Chromium smoke coverage asserts that the
standalone route issues one publication request, renders both audit tables,
and does not mount a MapLibre canvas.
