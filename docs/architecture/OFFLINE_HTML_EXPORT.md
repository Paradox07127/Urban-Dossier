# Self-contained HTML report export

EXPANSION_PLAN 2.6 is implemented by `POST /api/export/html`. The browser sends
the already-published detail snapshot, not authored HTML. Node validates that
snapshot and owns the complete document template.

## Export contract

The request carries the target, scores, evidence coverage, evidence rows, data
gaps, optional generated report text, and the backend-owned `chart_specs` from
the current detail response. The export route:

- accepts one to eight ChartSpec 1.0 objects;
- requires every chart to carry methodology version `3.9.0`;
- rejects a chart containing an external `url` data reference;
- limits each serialized chart to 1.5 MB and the entire JSON request to the
  existing 2 MB Express limit;
- escapes all user-visible text and JSON script boundaries; and
- generates the ISO timestamp on the server.

The response is an attachment named from a restricted ASCII slug. The report
includes score and coverage cards, optional narrative, all accepted charts,
data gaps, evidence rows, code references, the methodology version, and the
generation timestamp.

## Offline runtime and security boundary

`vega.min.js`, `vega-lite.min.js`, and `vega-embed.min.js` are read from the
installed local frontend dependencies and inserted into the HTML. Chart data
remains inline in each ChartSpec. There are no CDN, stylesheet, font, image, or
data requests.

The document CSP starts with `default-src 'none'`. Inline styles and scripts
are necessary because the deliverable is one file. Vega's expression compiler
also requires `unsafe-eval`; it is confined to this network-disabled document,
and external Vega data URLs are rejected before generation. Rendering waits
until `DOMContentLoaded` and the following animation frame so responsive
`width: "container"` charts measure a real layout width.

## Verification

`scripts/test-html-report-export.js` covers version and external-data rejection,
HTML/JSON escaping, missing-score preservation, runtime inlining, CSP, method
version, and timestamp output.

`interactive-map-explorer/scripts/smoke-chart-render.mjs` performs the user
path against real detail data: it clicks the download control, observes the
attachment response and Blob-backed download link, writes those exact Blob
bytes to a temporary file, blocks all HTTP(S) requests, and opens the file in
Chromium. The acceptance gate requires three visible Vega SVGs, no render
errors, methodology `3.9.0`, an ISO generation timestamp, and zero external
requests. The temporary file is deleted in `finally`.
