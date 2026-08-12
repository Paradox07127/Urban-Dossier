const test = require('node:test');
const assert = require('node:assert/strict');

const {
  buildOfflineHtmlReport,
  ExportValidationError,
} = require('./html-report-export');

function payload(overrides = {}) {
  return {
    title: 'A <script>alert(1)</script> place',
    target: { latitude: 40.75, longitude: -73.98, radius_m: 500, borough: 'Manhattan' },
    scores: { overall: 72, amenities: 64, environment: 81 },
    metric_scores: { nyccas_no: 81, heat_vulnerability: 25 },
    score_coverage: {
      amenities: { available: 3, total: 4, ratio: 0.75 },
    },
    score_uncertainty: {
      artifact_version: 'abc123',
      public_tier: { label: 'Middle–High', score_range: [48, 63] },
    },
    evidence_table: [{ source: 'NYC Open Data', date: '2026-Q2', summary: 'Observed value' }],
    data_gaps: ['One source unavailable'],
    report_markdown: '## Finding\nThe inline report is escaped.',
    chart_specs: {
      score: {
        schema_version: '1.0',
        chart_id: 'score',
        title: 'Score composition',
        code_ref: 'urban_dossier_backend.chart_specs:score_composition_chart',
        methodology_version: '3.9.0',
        spec: {
          $schema: 'https://vega.github.io/schema/vega-lite/v6.json',
          data: { values: [{ category: 'Overall', score: 72 }] },
          mark: 'bar',
          encoding: {
            x: { field: 'score', type: 'quantitative' },
            y: { field: 'category', type: 'nominal' },
          },
        },
      },
    },
    ...overrides,
  };
}

test('builds a self-contained, stamped, escaped Vega report', () => {
  const output = buildOfflineHtmlReport(payload(), {
    methodologyVersion: '3.9.0',
    generatedAt: '2026-08-12T12:34:56.000Z',
  });
  assert.match(output, /methodology v3\.9\.0/);
  assert.match(output, /generated 2026-08-12T12:34:56\.000Z/);
  assert.match(output, /self-contained offline export/);
  assert.match(output, /script-src 'unsafe-inline' 'unsafe-eval'/);
  assert.match(output, /vegaEmbed\(node, chart\.spec/);
  assert.doesNotMatch(output, /<script\s+src=/i);
  assert.doesNotMatch(output, /<link\s+[^>]*href=/i);
  assert.doesNotMatch(output, /<script>alert\(1\)<\/script>/);
  assert.match(output, /A &lt;script&gt;alert\(1\)&lt;\/script&gt; place/);
  assert.match(output, /<strong>Middle–High<\/strong>/);
  assert.match(output, /95% range 48–63 · point estimate 72/);
  assert.match(output, /<span>modeled NO context<\/span><strong>81<\/strong>/);
  assert.match(output, /<span>heat vulnerability<\/span><strong>25<\/strong>/);
  assert.doesNotMatch(output, /<span>environment<\/span>/, 'context must not masquerade as a composite');
  assert.doesNotMatch(output, /<span>building<\/span>/, 'missing scores must not become zero');
});

test('rejects stale methodology and external data URLs', () => {
  const stale = payload();
  stale.chart_specs.score.methodology_version = '3.8.0';
  assert.throws(
    () => buildOfflineHtmlReport(stale, { methodologyVersion: '3.9.0' }),
    ExportValidationError,
  );

  const external = payload();
  external.chart_specs.score.spec.data = { url: 'https://example.com/data.json' };
  assert.throws(
    () => buildOfflineHtmlReport(external, { methodologyVersion: '3.9.0' }),
    /external data URL/,
  );
});


test('the frontend export body carries every key the generator reads', () => {
  const fs = require('node:fs');
  const path = require('node:path');
  // The review found NYCCAS/HVI present online and absent offline: the
  // generator read metric_scores, the frontend never sent it, and this unit
  // file could not notice because it fed the generator directly. This check
  // is deliberately textual -- there is no browser harness in this repo --
  // and pins the request-body contract at its two ends.
  const app = fs.readFileSync(
    path.join(__dirname, '..', 'interactive-map-explorer', 'src', 'App.tsx'),
    'utf8',
  );
  const exportBody = app.slice(app.indexOf("fetch('/api/export/html'"));
  for (const key of ['metric_scores', 'chart_specs', 'score_uncertainty',
                     'score_coverage', 'evidence_table', 'scores', 'target']) {
    assert.match(
      exportBody.slice(0, 1600),
      new RegExp(`${key}:`),
      `App.tsx export body is missing ${key}`,
    );
  }
});
