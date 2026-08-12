const fs = require('fs');
const path = require('path');

const MAX_CHARTS = 8;
const MAX_CHART_BYTES = 1_500_000;
const MAX_REPORT_CHARS = 50_000;
let cachedRuntime = null;

class ExportValidationError extends Error {}

function text(value, maxLength = 500) {
  return typeof value === 'string' ? value.trim().slice(0, maxLength) : '';
}

function html(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function scriptText(value) {
  return value.replace(/<\/script/gi, '<\\/script');
}

function jsonForScript(value) {
  return JSON.stringify(value)
    .replaceAll('<', '\\u003c')
    .replaceAll('>', '\\u003e')
    .replaceAll('&', '\\u0026')
    .replaceAll('\u2028', '\\u2028')
    .replaceAll('\u2029', '\\u2029');
}

function containsExternalDataReference(value, seen = new Set()) {
  if (!value || typeof value !== 'object') return false;
  if (seen.has(value)) return false;
  seen.add(value);
  if (Array.isArray(value)) {
    return value.some((item) => containsExternalDataReference(item, seen));
  }
  return Object.entries(value).some(([key, item]) => {
    if (key.toLowerCase() === 'url' && typeof item === 'string' && item.trim()) return true;
    return containsExternalDataReference(item, seen);
  });
}

function validateCharts(chartSpecs, methodologyVersion) {
  if (!chartSpecs || typeof chartSpecs !== 'object' || Array.isArray(chartSpecs)) {
    throw new ExportValidationError('chart_specs must be an object');
  }
  const charts = Object.entries(chartSpecs);
  if (charts.length === 0 || charts.length > MAX_CHARTS) {
    throw new ExportValidationError(`chart_specs must contain 1-${MAX_CHARTS} charts`);
  }
  return charts.map(([key, chart]) => {
    if (!chart || typeof chart !== 'object' || Array.isArray(chart)) {
      throw new ExportValidationError(`chart_specs.${key} must be an object`);
    }
    if (chart.schema_version !== '1.0') {
      throw new ExportValidationError(`chart_specs.${key} has an unsupported schema_version`);
    }
    if (chart.methodology_version !== methodologyVersion) {
      throw new ExportValidationError(`chart_specs.${key} has a stale methodology_version`);
    }
    if (!chart.spec || typeof chart.spec !== 'object' || Array.isArray(chart.spec)) {
      throw new ExportValidationError(`chart_specs.${key}.spec must be an object`);
    }
    if (containsExternalDataReference(chart.spec)) {
      throw new ExportValidationError(`chart_specs.${key} contains an external data URL`);
    }
    const serialised = JSON.stringify(chart.spec);
    if (Buffer.byteLength(serialised, 'utf8') > MAX_CHART_BYTES) {
      throw new ExportValidationError(`chart_specs.${key} exceeds the export size limit`);
    }
    return {
      chart_id: text(chart.chart_id || key, 100),
      title: text(chart.title || key, 200),
      code_ref: text(chart.code_ref, 300),
      methodology_version: chart.methodology_version,
      spec: chart.spec,
    };
  });
}

function finiteNumber(value) {
  if (value == null || value === '') return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function normalisePayload(payload, methodologyVersion) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    throw new ExportValidationError('request body must be an object');
  }
  const charts = validateCharts(payload.chart_specs, methodologyVersion);
  const target = payload.target && typeof payload.target === 'object' ? payload.target : {};
  const scores = payload.scores && typeof payload.scores === 'object' ? payload.scores : {};
  const coverage = payload.score_coverage && typeof payload.score_coverage === 'object'
    ? payload.score_coverage
    : {};
  const uncertainty = payload.score_uncertainty && typeof payload.score_uncertainty === 'object'
    ? payload.score_uncertainty
    : {};
  const tier = uncertainty.public_tier && typeof uncertainty.public_tier === 'object'
    ? uncertainty.public_tier
    : {};
  const tierRange = Array.isArray(tier.score_range)
    ? tier.score_range.map(finiteNumber)
    : [];
  const evidence = Array.isArray(payload.evidence_table)
    ? payload.evidence_table.slice(0, 100).map((row) => ({
        source: text(row?.source, 200),
        date: text(row?.date, 60),
        summary: text(row?.summary, 1_000),
      }))
    : [];
  return {
    title: text(payload.title, 200) || text(target.matched_address, 200) || 'Urban Dossier report',
    report_mode: text(payload.report_mode, 40) || 'snapshot',
    target: {
      latitude: finiteNumber(target.latitude),
      longitude: finiteNumber(target.longitude),
      radius_m: finiteNumber(target.radius_m),
      matched_address: text(target.matched_address, 300),
      borough: text(target.borough, 100),
      zip: text(target.zip, 20),
    },
    scores: Object.fromEntries(
      ['overall', 'amenities', 'transit', 'safety', 'building'].map((key) => [key, finiteNumber(scores[key])]),
    ),
    score_coverage: Object.fromEntries(
      ['overall', 'amenities', 'transit', 'safety', 'building'].map((key) => {
        const item = coverage[key] && typeof coverage[key] === 'object' ? coverage[key] : {};
        return [key, {
          available: finiteNumber(item.available),
          total: finiteNumber(item.total),
          ratio: finiteNumber(item.ratio),
        }];
      }),
    ),
    public_tier: text(tier.label, 100) && tierRange.length === 2 && tierRange.every((value) => value != null)
      ? {
          label: text(tier.label, 100),
          score_range: tierRange,
          artifact_version: text(uncertainty.artifact_version, 100),
        }
      : null,
    report_markdown: text(payload.report_markdown, MAX_REPORT_CHARS),
    data_gaps: Array.isArray(payload.data_gaps)
      ? payload.data_gaps.slice(0, 100).map((item) => text(item, 500)).filter(Boolean)
      : [],
    evidence_table: evidence,
    charts,
  };
}

function loadVegaRuntime() {
  if (cachedRuntime) return cachedRuntime;
  const modules = path.join(__dirname, '..', 'interactive-map-explorer', 'node_modules');
  cachedRuntime = [
    'vega/build/vega.min.js',
    'vega-lite/build/vega-lite.min.js',
    'vega-embed/build/vega-embed.min.js',
  ].map((relativePath) => scriptText(fs.readFileSync(path.join(modules, relativePath), 'utf8')));
  return cachedRuntime;
}

function scoreCards(report) {
  return Object.entries(report.scores)
    .filter(([, value]) => value != null)
    .map(([key, value]) => {
      const coverage = report.score_coverage[key];
      const coverageLabel = coverage?.available != null && coverage?.total != null
        ? `${coverage.available}/${coverage.total} sources`
        : 'coverage unavailable';
      if (key === 'overall' && report.public_tier) {
        const [low, high] = report.public_tier.score_range;
        return `<div class="score"><span>overall tier</span><strong>${html(report.public_tier.label)}</strong><small>95% range ${html(low)}–${html(high)} · point estimate ${html(Math.round(value))}</small></div>`;
      }
      return `<div class="score"><span>${html(key)}</span><strong>${html(Math.round(value))}</strong><small>${html(coverageLabel)}</small></div>`;
    })
    .join('');
}

function evidenceRows(report) {
  if (!report.evidence_table.length) return '<p class="muted">No evidence rows were published.</p>';
  return `<table><thead><tr><th>Source</th><th>Date / period</th><th>Evidence</th></tr></thead><tbody>${report.evidence_table
    .map((row) => `<tr><td>${html(row.source)}</td><td>${html(row.date)}</td><td>${html(row.summary)}</td></tr>`)
    .join('')}</tbody></table>`;
}

function buildOfflineHtmlReport(payload, options = {}) {
  const methodologyVersion = options.methodologyVersion || '3.9.0';
  const generatedAt = options.generatedAt || new Date().toISOString();
  const report = normalisePayload(payload, methodologyVersion);
  const [vega, vegaLite, vegaEmbed] = loadVegaRuntime();
  const location = [report.target.borough, report.target.zip].filter(Boolean).join(' ');
  const coordinates = report.target.latitude != null && report.target.longitude != null
    ? `${report.target.latitude.toFixed(5)}, ${report.target.longitude.toFixed(5)}`
    : 'not available';
  const charts = report.charts.map((chart, index) => `
    <figure>
      <div id="chart-${index}" class="chart" aria-label="${html(chart.title)}"></div>
      <figcaption>${html(chart.title)} · ${html(chart.code_ref)}</figcaption>
    </figure>`).join('');
  const gaps = report.data_gaps.length
    ? `<ul>${report.data_gaps.map((gap) => `<li>${html(gap)}</li>`).join('')}</ul>`
    : '<p class="muted">No data gaps were reported.</p>';

  return `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval'; style-src 'unsafe-inline'; img-src data: blob:; font-src data:">
  <title>${html(report.title)} — Urban Dossier</title>
  <style>
    :root{color-scheme:light;--ink:#17201d;--muted:#66706c;--line:#dfe5e2;--paper:#fff;--wash:#f3f6f4;--accent:#246b4f}*{box-sizing:border-box}body{margin:0;background:var(--wash);color:var(--ink);font:15px/1.55 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}main{max-width:1040px;margin:0 auto;padding:48px 28px 80px}header{border-bottom:3px solid var(--ink);padding-bottom:24px;margin-bottom:28px}h1{font-size:34px;line-height:1.12;margin:0 0 10px}h2{font-size:19px;margin:34px 0 12px}.meta,.muted,figcaption{color:var(--muted)}.meta{display:flex;gap:18px;flex-wrap:wrap;font:12px/1.5 ui-monospace,SFMono-Regular,monospace}.scores{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px}.score,figure,.section{background:var(--paper);border:1px solid var(--line);border-radius:10px}.score{padding:14px;display:grid;gap:2px;text-transform:capitalize}.score strong{font-size:31px;color:var(--accent)}.score small{color:var(--muted)}figure{margin:0 0 18px;padding:18px;overflow:auto}.chart{min-height:120px;width:100%}figcaption{font:11px/1.45 ui-monospace,SFMono-Regular,monospace;border-top:1px solid var(--line);padding-top:10px;margin-top:8px}.section{padding:20px;white-space:pre-wrap}table{border-collapse:collapse;width:100%;background:var(--paper);font-size:13px}th,td{text-align:left;vertical-align:top;border:1px solid var(--line);padding:9px}th{background:var(--wash)}.render-error{color:#9b2c2c;font-size:13px}@media print{body{background:#fff}main{max-width:none;padding:20px}figure,.score,.section{break-inside:avoid}}
  </style>
</head>
<body>
<main>
  <header>
    <p class="muted">Urban Dossier · ${html(report.report_mode)} report</p>
    <h1>${html(report.title)}</h1>
    <p>${html(location || 'New York City')} · ${html(coordinates)}${report.target.radius_m != null ? ` · ${html(report.target.radius_m)} m radius` : ''}</p>
    <div class="meta"><span data-testid="methodology-version">methodology v${html(methodologyVersion)}</span><span data-testid="generated-at">generated ${html(generatedAt)}</span><span>self-contained offline export</span></div>
  </header>
  <h2>Scores and evidence coverage</h2>
  <div class="scores">${scoreCards(report)}</div>
  ${report.report_markdown ? `<h2>Report</h2><div class="section">${html(report.report_markdown)}</div>` : ''}
  <h2>Charts</h2>
  ${charts}
  <h2>Data gaps</h2>
  <div class="section">${gaps}</div>
  <h2>Evidence</h2>
  ${evidenceRows(report)}
</main>
<script>${vega}</script>
<script>${vegaLite}</script>
<script>${vegaEmbed}</script>
<script id="urban-dossier-report" type="application/json">${jsonForScript(report)}</script>
<script>
  (() => {
    const report = JSON.parse(document.getElementById('urban-dossier-report').textContent);
    const render = () => requestAnimationFrame(() => {
      report.charts.forEach((chart, index) => {
        const node = document.getElementById('chart-' + index);
        vegaEmbed(node, chart.spec, { actions: false, renderer: 'svg' }).catch((error) => {
          node.className = 'render-error';
          node.textContent = 'Chart could not be rendered: ' + error.message;
        });
      });
    });
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', render, { once: true });
    } else {
      render();
    }
  })();
</script>
</body>
</html>`;
}

module.exports = { buildOfflineHtmlReport, ExportValidationError };
