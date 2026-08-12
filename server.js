const express = require('express');
const path = require('path');
const fs = require('fs');
const crypto = require('crypto');
const zlib = require('zlib');
const { previewCacheKey } = require('./scripts/api-cache-key');
const EXPECTED_METHODOLOGY_VERSION = '3.9.0';
const EXPECTED_BUILDING_SCORING_CONTRACT = 'point-radius-haversine-v1';

const app = express();
// 3456 stays the deployment default. The override exists so a second instance
// can be brought up alongside the running one to verify a change end-to-end
// without taking the LAN demo down.
const PORT = Number(process.env.URBAN_DOSSIER_PORT || 3456);
const HOST = (process.env.URBAN_DOSSIER_BIND_HOST || '0.0.0.0').trim();
const BACKEND_BASE_URL = (process.env.URBAN_DOSSIER_BACKEND_URL || 'http://127.0.0.1:8090').replace(/\/$/, '');
const DEMO_TOKEN = (process.env.URBAN_DOSSIER_DEMO_TOKEN || '').trim();
const BACKEND_TIMEOUT_MS = Number(process.env.URBAN_DOSSIER_BACKEND_TIMEOUT_MS || 180000);
const DEBUG_PROXY_ERRORS = process.env.URBAN_DOSSIER_DEBUG_ERRORS === '1';
const ALLOWED_ORIGINS = new Set([
  'http://localhost:3000',
  'http://127.0.0.1:3000',
  'http://localhost:3456',
  'http://127.0.0.1:3456',
  'http://localhost:5173',
  'http://127.0.0.1:5173',
]);
app.use(express.json({ limit: '2mb' }));
app.disable('x-powered-by');

app.use((req, res, next) => {
  res.setHeader('X-Content-Type-Options', 'nosniff');
  res.setHeader('X-Frame-Options', 'DENY');
  res.setHeader('Referrer-Policy', 'same-origin');
  res.setHeader('Cache-Control', 'no-store');
  next();
});

app.use('/api', (req, res, next) => {
  const origin = req.get('origin');
  if (origin && !isAllowedOrigin(origin)) {
    return res.status(403).json({ ok: false, error: 'Origin not allowed' });
  }
  next();
});

const MBTILES_PATH = path.join(__dirname, 'osm-2020-02-10-v3.11_new-york_new-york.mbtiles');

function createDatabase(dbPath) {
  try {
    const { DatabaseSync } = require('node:sqlite');
    return new DatabaseSync(dbPath, { readOnly: true });
  } catch (sqliteError) {
    const BetterSqlite3 = require('better-sqlite3');
    return new BetterSqlite3(dbPath, { readonly: true });
  }
}

const db = createDatabase(MBTILES_PATH);

// Per-building scores, baked into their own tileset by
// backend/scripts/build_building_tiles.py. Optional on purpose: the pipeline
// needs a 3 GB OSM extract and tippecanoe, and a checkout without them should
// still serve a working map rather than a broken one. When this is absent the
// endpoint below reports so, and the client keeps its previous behaviour.
const BUILDING_TILES_PATH =
  process.env.URBAN_DOSSIER_BUILDING_TILES ||
  path.join(__dirname, 'building-scores.mbtiles');
const BUILDING_SCORES_MANIFEST =
  process.env.URBAN_DOSSIER_BUILDING_MANIFEST ||
  '/mnt/data/urban-dossier-state/maps/buildings/building_scores.manifest.json';
const BUILDING_TILES_MANIFEST =
  process.env.URBAN_DOSSIER_BUILDING_TILES_MANIFEST ||
  '/mnt/data/urban-dossier-state/maps/buildings/building_tiles.manifest.json';

function loadBuildingPublication() {
  try {
    const scores = JSON.parse(fs.readFileSync(BUILDING_SCORES_MANIFEST, 'utf8'));
    const tiles = JSON.parse(fs.readFileSync(BUILDING_TILES_MANIFEST, 'utf8'));
    const valid =
      scores.methodology_version === EXPECTED_METHODOLOGY_VERSION &&
      tiles.methodology_version === EXPECTED_METHODOLOGY_VERSION &&
      scores.scoring_contract === EXPECTED_BUILDING_SCORING_CONTRACT &&
      tiles.scoring_contract === EXPECTED_BUILDING_SCORING_CONTRACT &&
      typeof scores.artifact_sha256 === 'string' &&
      tiles.source_scores_sha256 === scores.artifact_sha256 &&
      fs.realpathSync(BUILDING_TILES_PATH) === fs.realpathSync(tiles.tileset);
    return { valid, scores, reason: valid ? null : 'manifest mismatch' };
  } catch (error) {
    return { valid: false, scores: null, reason: error.message };
  }
}

const buildingPublication = loadBuildingPublication();
let buildingDb = null;
try {
  if (fs.existsSync(BUILDING_TILES_PATH) && buildingPublication.valid) {
    buildingDb = createDatabase(BUILDING_TILES_PATH);
  } else if (fs.existsSync(BUILDING_TILES_PATH)) {
    console.warn(`  Building score tiles rejected: ${buildingPublication.reason}`);
  }
} catch (error) {
  console.warn(`  Building score tiles unavailable: ${error.message}`);
  buildingDb = null;
}

// A Terrain-RGB tileset used to sit here, lifting the boroughs onto a plateau
// so the 3D view read as a model on a table. The client no longer asks for it:
// terrain triangulates between height samples, so the 260 m step at the
// shoreline came out as a one-sample ramp with the basemap stretched over it
// rather than as a cut edge. The ground is sea level now and the route is gone
// with its only consumer. backend/scripts/build_plateau_dem.py still builds
// the tileset if the plinth is ever wanted back.

const TAG_STYLES = {
  general: {
    low: '#d73027',
    high: '#fee8c8',
    accent: '#8c1d18',
    legend: 'General',
  },
  safety: {
    low: '#c51b8a',
    high: '#fde0dd',
    accent: '#7a0177',
    legend: 'Safety',
  },
  transit: {
    low: '#2b8cbe',
    high: '#deebf7',
    accent: '#045a8d',
    legend: 'Transit',
  },
  amenities: {
    low: '#31a354',
    high: '#e5f5e0',
    accent: '#006d2c',
    legend: 'Amenities',
  },
};
const ALLOWED_RADIUS_METERS = new Set([200, 500, 1000]);

// A seeded random-point generator, four precomputed demo datasets and two
// payload sanitisers stood here. Nothing reached any of them: the datasets
// were assigned to a const no code read, and the sanitisers had lost their
// last caller when the render endpoints started passing backend output
// through unchanged. Synthetic scores in the file that serves real ones is
// also a hazard worth not keeping around.

function mapTagToOverviewRequest(tag) {
  if (tag === 'general') {
    return { view_mode: 'overall', category_id: null, render_mode: 'h3_cells' };
  }
  return { view_mode: 'category', category_id: tag, render_mode: 'h3_cells' };
}

function scoreForTag(cell = {}, tag = 'general') {
  if (tag === 'general') {
    const raw = cell.overall_score ?? cell.score ?? cell.category_scores?.overall;
    return raw == null ? Number.NaN : Number(raw);
  }
  const raw = cell[`${tag}_score`] ?? cell.category_scores?.[tag] ?? cell.score;
  return raw == null ? Number.NaN : Number(raw);
}

function coverageForTag(row = {}, tag = 'general') {
  const prefix = tag === 'general' ? 'overall' : tag;
  const n = Number(row[`${prefix}_coverage_n`]);
  const total = Number(row[`${prefix}_coverage_total`]);
  const fraction = Number(row[`${prefix}_coverage_fraction`]);
  const ratio = Number(row[`${prefix}_coverage_ratio`]);
  return {
    coverage_n: Number.isFinite(n) ? n : null,
    coverage_total: Number.isFinite(total) ? total : null,
    coverage_fraction: Number.isFinite(fraction)
      ? Math.max(0, Math.min(1, fraction))
      : null,
    coverage_ratio: Number.isFinite(ratio) ? Math.max(0, Math.min(1, ratio)) : null,
  };
}

function cellsToRenderPoints(cells = [], tag = 'general') {
  return cells
    .map((cell) => ({
      latitude: Number(cell.latitude ?? cell.lat ?? cell.center_lat ?? cell.centroid_lat),
      longitude: Number(cell.longitude ?? cell.lng ?? cell.center_lng ?? cell.centroid_lng),
      score: Math.max(0, Math.min(100, scoreForTag(cell, tag))),
    }))
    .filter((point) =>
      Number.isFinite(point.latitude) &&
      Number.isFinite(point.longitude) &&
      Number.isFinite(point.score),
    );
}

function categoryScoreFromPreview(data, tag) {
  if (tag === 'general') {
    const raw = data?.scores?.overall;
    return raw == null ? 50 : Number(raw);
  }
  const raw = data?.scores?.[tag] ?? data?.scores?.overall;
  return raw == null ? 50 : Number(raw);
}

function averageNearestNeighborDistance(point, peers, limit = 3) {
  const distances = [];
  for (const peer of peers) {
    if (peer === point) continue;
    const distance = distanceMeters(
      [point.latitude, point.longitude],
      [peer.latitude, peer.longitude],
    );
    if (Number.isFinite(distance)) {
      distances.push(distance);
    }
  }
  if (!distances.length) return Number.POSITIVE_INFINITY;
  distances.sort((a, b) => a - b);
  const nearest = distances.slice(0, limit);
  return nearest.reduce((sum, value) => sum + value, 0) / nearest.length;
}

function buildRankLookup(values, invert = false, anchor = 50) {
  const sorted = values.filter((value) => Number.isFinite(value)).sort((a, b) => a - b);
  if (!sorted.length) {
    return () => anchor;
  }

  const firstIndex = new Map();
  const lastIndex = new Map();
  sorted.forEach((value, index) => {
    if (!firstIndex.has(value)) firstIndex.set(value, index);
    lastIndex.set(value, index);
  });

  const denominator = Math.max(sorted.length - 1, 1);
  // Map percentile ranks to a ±25-point window centered on the anchor score,
  // so detail-point colors stay consistent with the overview zone color.
  const SPREAD = 25;
  const low = Math.max(0, anchor - SPREAD);
  const high = Math.min(100, anchor + SPREAD);

  return (value) => {
    if (!Number.isFinite(value)) return anchor;
    const first = firstIndex.get(value);
    const last = lastIndex.get(value);
    if (first === undefined || last === undefined) return anchor;
    const percentile = ((first + last) / 2) / denominator;
    const normalized = invert ? 1 - percentile : percentile;
    return Math.round(low + normalized * (high - low));
  };
}

function pointBelongsToTag(point, tag) {
  if (tag === 'general') return true;
  const kind = typeof point?.kind === 'string' ? point.kind.toLowerCase() : '';
  if (tag === 'safety') return kind === 'safety' || kind === 'rodent' || kind === '311';
  if (tag === 'transit') return kind === 'transit' || kind === 'collision';
  if (tag === 'amenities') return kind === 'toilet' || kind === 'linknyc' || kind === 'restaurant' || kind === 'tree';
  return false;
}

function previewToRenderPoints(tag, data = {}) {
  const categoryScore = Math.max(0, Math.min(100, categoryScoreFromPreview(data, tag)));
  const mapPoints = Array.isArray(data?.detail_items?.map_points) ? data.detail_items.map_points : [];
  const buildingFlags = Array.isArray(data?.detail_items?.building_flags) ? data.detail_items.building_flags : [];
  const combined = [
    ...mapPoints,
    ...(tag === 'general' ? buildingFlags : []),
  ];

  const filtered = combined
    .filter((point) => pointBelongsToTag(point, tag))
    .map((point) => ({
      latitude: Number(point.latitude),
      longitude: Number(point.longitude),
      baseScore: Number(point.score_hint ?? Number.NaN),
      tag,
      point,
    }))
    .filter((point) =>
      Number.isFinite(point.latitude) &&
      Number.isFinite(point.longitude),
    );

  if (!filtered.length) {
    return [];
  }

  const densityValues = filtered.map((point) => {
    const neighborDistance = averageNearestNeighborDistance(point, filtered);
    if (!Number.isFinite(neighborDistance)) return 0;
    return 1 / Math.max(neighborDistance, 1);
  });

  const densityToScore = buildRankLookup(densityValues, tag !== 'amenities', categoryScore);
  const baseValues = filtered
    .map((point) => point.baseScore)
    .filter((value) => Number.isFinite(value));
  const baseToScore = buildRankLookup(baseValues, false, categoryScore);

  return filtered.map((point, index) => {
    const densityScore = densityToScore(densityValues[index]);
    const explicitScore = Number.isFinite(point.baseScore)
      ? Math.max(0, Math.min(100, baseToScore(point.baseScore)))
      : Number.NaN;

    const blendedScore = Number.isFinite(explicitScore)
      ? Math.round(explicitScore * 0.65 + densityScore * 0.35)
      : Math.round(categoryScore * 0.45 + densityScore * 0.55);

    return {
      latitude: point.latitude,
      longitude: point.longitude,
      score: Math.max(0, Math.min(100, blendedScore)),
      tag,
      kind: point.point?.kind || tag,
      summary: point.point?.summary || '',
    };
  });
}

async function backendRequest(routePath, { method = 'GET', body } = {}) {
  const headers = {
    'Content-Type': 'application/json',
  };
  if (DEMO_TOKEN) {
    headers['X-Urban-Dossier-Token'] = DEMO_TOKEN;
  }
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), BACKEND_TIMEOUT_MS);
  let response;
  try {
    response = await fetch(`${BACKEND_BASE_URL}${routePath}`, {
      method,
      headers,
      body: body ? JSON.stringify(body) : undefined,
      signal: controller.signal,
    });
  } catch (error) {
    if (error.name === 'AbortError') {
      const timeoutError = new Error(`Backend request timed out after ${BACKEND_TIMEOUT_MS}ms`);
      timeoutError.status = 504;
      throw timeoutError;
    }
    throw error;
  } finally {
    clearTimeout(timeout);
  }

  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }

  if (!response.ok) {
    const error = new Error(`Backend request failed: ${response.status}`);
    error.status = response.status;
    error.payload = payload;
    throw error;
  }

  return payload;
}

// Gzip for the bulky JSON bodies. The overview GeoJSON is ~580 KB of
// coordinates that squeeze below 90 KB; over the LAN that is invisible, over
// anything slower it is the difference between the choropleth snapping in and
// crawling in. Applied per-route to the known-heavy payloads rather than as
// blanket middleware, so tiles (already compressed) and small JSON stay
// untouched.
function sendJsonMaybeGzip(req, res, payload) {
  const body = Buffer.from(JSON.stringify(payload));
  const accepts = String(req.headers['accept-encoding'] || '');
  res.set('Content-Type', 'application/json; charset=utf-8');
  if (body.length > 16384 && /\bgzip\b/.test(accepts)) {
    return zlib.gzip(body, { level: 6 }, (err, zipped) => {
      if (err) return res.send(body);
      res.set('Content-Encoding', 'gzip');
      res.send(zipped);
    });
  }
  res.send(body);
}

function sendProxyError(res, status, message, extra = {}) {
  res.status(status).json({
    ok: false,
    error: message,
    ...(DEBUG_PROXY_ERRORS ? extra : {}),
  });
}

function isSafePathSegment(value) {
  return typeof value === 'string' && /^[A-Za-z0-9 _.-]+$/.test(value);
}

function isPrivateOrLoopbackHost(hostname) {
  if (!hostname) return false;
  const value = hostname.toLowerCase();
  if (value === 'localhost' || value === '127.0.0.1' || value === '::1') return true;
  const match = value.match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/);
  if (!match) return false;
  const [a, b] = [Number(match[1]), Number(match[2])];
  if (a === 10) return true;
  if (a === 127) return true;
  if (a === 192 && b === 168) return true;
  if (a === 172 && b >= 16 && b <= 31) return true;
  if (a === 100 && b >= 64 && b <= 127) return true; // Tailscale / CGNAT range
  return false;
}

function isAllowedOrigin(origin) {
  if (!origin) return true;
  if (ALLOWED_ORIGINS.has(origin)) return true;
  try {
    const parsed = new URL(origin);
    const port = Number(parsed.port || (parsed.protocol === 'https:' ? 443 : 80));
    // Allow any host on the serving port (same-server requests via proxies/tunnels)
    if (port === PORT) return true;
    return isPrivateOrLoopbackHost(parsed.hostname) && [3000, 3456, 5173].includes(port);
  } catch {
    return false;
  }
}

function distanceMeters(a, b) {
  const NYC_COS_LAT = 0.7580107;
  const DEG_TO_M = 111320.0;
  const dlat = (b[0] - a[0]) * DEG_TO_M;
  const dlng = (b[1] - a[1]) * DEG_TO_M * NYC_COS_LAT;
  return Math.sqrt(dlat * dlat + dlng * dlng);
}

// destinationPoint and generateLocalDemoPoints stood here: a synthetic
// scatter of points around a location, used when the backend had nothing
// to say. Nothing called it any more, and inventing scores in the proxy is
// the exact failure the architecture is built to prevent -- the backend is
// the source of analysis truth and an empty answer from it is information.

// CORS
app.use((req, res, next) => {
  const origin = req.headers.origin;
  if (!origin || isAllowedOrigin(origin)) {
    res.set('Access-Control-Allow-Origin', origin || 'http://localhost:3456');
  }
  res.set('Vary', 'Origin');
  res.set('Access-Control-Allow-Headers', 'Content-Type, X-Urban-Dossier-Token');
  res.set('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
  if (req.method === 'OPTIONS') {
    return res.status(204).end();
  }
  next();
});

// Metadata endpoint
app.get('/metadata', (req, res) => {
  const rows = db.prepare('SELECT name, value FROM metadata').all();
  const metadata = {};
  for (const row of rows) {
    metadata[row.name] = row.value;
  }
  res.json(metadata);
});

app.post('/api/search', async (req, res) => {
  try {
    const payload = await backendRequest('/api/search', { method: 'POST', body: req.body });
    res.json(payload);
  } catch (error) {
    sendProxyError(res, 502, 'Search proxy failed', { details: error.payload ?? null });
  }
});

app.get('/api/health', async (req, res) => {
  try {
    const payload = await backendRequest('/api/health');
    res.json(payload);
  } catch (error) {
    sendProxyError(res, 502, 'Python backend health check failed', { details: error.payload ?? null });
  }
});

app.get('/api/categories', async (req, res) => {
  try {
    const payload = await backendRequest('/api/categories');
    res.json(payload);
  } catch (error) {
    sendProxyError(res, 502, 'Python backend categories lookup failed', { details: error.payload ?? null });
  }
});

app.get('/api/coverage', async (req, res) => {
  try {
    const payload = await backendRequest('/api/coverage');
    res.json(payload);
  } catch (error) {
    sendProxyError(res, 502, 'Python backend coverage lookup failed', { details: error.payload ?? null });
  }
});

// Metric definitions. Pass-through only -- the registry lives in the backend
// and this file must not restate a unit or a weight, or the two drift.
app.get('/api/metrics', async (req, res) => {
  try {
    res.json(await backendRequest('/api/metrics'));
  } catch (error) {
    sendProxyError(res, 502, 'Python backend metric registry lookup failed', { details: error.payload ?? null });
  }
});

app.get('/api/metrics/:metricId', async (req, res) => {
  const { metricId } = req.params;
  if (!isSafePathSegment(metricId)) {
    return res.status(400).json({ detail: 'Invalid metric id' });
  }
  try {
    res.json(await backendRequest(`/api/metrics/${encodeURIComponent(metricId)}`));
  } catch (error) {
    // "No such metric" is an answer, not a proxy failure. Collapsing it into
    // 502 like the other routes do would leave a caller unable to tell a
    // misspelled id from a backend that fell over -- and this endpoint exists
    // precisely so a UI can look up an id it is not sure about.
    if (error.status >= 400 && error.status < 500) {
      return res.status(error.status).json(error.payload ?? { detail: 'Metric lookup failed' });
    }
    sendProxyError(res, 502, 'Python backend metric lookup failed', { details: error.payload ?? null });
  }
});

app.post('/api/overview', async (req, res) => {
  try {
    const payload = await backendRequest('/api/overview', {
      method: 'POST',
      body: req.body,
    });
    res.json(payload);
  } catch (error) {
    sendProxyError(res, 502, 'Overview proxy failed', { details: error.payload ?? null });
  }
});

// buildDisplayScoreLookup stood here -- a percentile-ranking helper with no
// callers, duplicating what buildRankLookup already does for the callers it
// has.

app.get('/api/overview/geojson', async (req, res) => {
  const requestedTag = typeof req.query.tag === 'string' ? req.query.tag.toLowerCase() : 'general';
  const tag = TAG_STYLES[requestedTag] ? requestedTag : 'general';

  try {
    const payload = await backendRequest('/api/overview', {
      method: 'POST',
      body: mapTagToOverviewRequest(tag),
    });

    const cells = Array.isArray(payload?.cells) ? payload.cells : [];
    let cellsWithoutGeometry = 0;

    const features = cells
      .map((cell) => {
        const lat = Number(cell.latitude ?? cell.lat ?? cell.center_lat ?? cell.centroid_lat);
        const lng = Number(cell.longitude ?? cell.lng ?? cell.center_lng ?? cell.centroid_lng);
        const h3Id = cell.h3 ?? cell.cell_id;
        if (!Number.isFinite(lat) || !Number.isFinite(lng) || !h3Id) return null;

        const raw = scoreForTag(cell, tag);
        if (!Number.isFinite(raw)) return null;
        const coverage = coverageForTag(cell, tag);
        // The geometry is the backend's to state. It is the true cell boundary
        // clipped to the coastline, so a cell straddling the shore stops at
        // the water instead of colouring the river, and cells that are
        // entirely water never arrive at all. The type comes with it because
        // clipping a cell containing an island yields a MultiPolygon; this is
        // a pass-through, not a reconstruction.
        //
        // A cell without a boundary is dropped rather than approximated. There
        // used to be a hexApprox fallback here for older backends, drawing a
        // hexagon at a hardcoded 0.0025 degrees -- about half the true radius,
        // a quarter of the area. It turned a grid that tiles the city without
        // gaps into isolated dots over a mostly uncoloured map, and it did so
        // silently, because a wrong polygon renders just as happily as a right
        // one. Keeping it as a safety net meant keeping a path that quietly
        // reinstates a fixed bug. Missing geometry is now visible as a missing
        // cell and a warning, which is the failure mode you can act on.
        if (!Array.isArray(cell.boundary) || cell.boundary.length === 0) {
          cellsWithoutGeometry += 1;
          return null;
        }
        const geometry = {
          type: cell.boundary_type === 'MultiPolygon' ? 'MultiPolygon' : 'Polygon',
          coordinates: cell.boundary,
        };
        return {
          type: 'Feature',
          properties: {
            h3: h3Id,
            score: raw,
            display_score: Math.max(0, Math.min(100, Math.round(raw))),
            land_fraction: typeof cell.land_fraction === 'number' ? cell.land_fraction : null,
            ...coverage,
            tag,
          },
          geometry,
        };
      })
      .filter(Boolean);

    if (cellsWithoutGeometry > 0) {
      console.warn(
        `  ${cellsWithoutGeometry} of ${cells.length} overview cells arrived without a ` +
        'boundary and were dropped. The backend should be clipping and returning ' +
        'cell geometry -- check /api/overview.',
      );
    }

    sendJsonMaybeGzip(req, res, {
      type: 'FeatureCollection',
      features,
      metadata: {
        tag,
        cell_count: features.length,
        // Reported so a thin map is distinguishable from a small city.
        cells_without_geometry: cellsWithoutGeometry,
        scoring_mode: 'absolute',
        overview_ready: Boolean(payload?.coverage?.overview_ready ?? payload?.overview_ready),
      },
    });
  } catch (error) {
    sendProxyError(res, 502, 'Overview GeoJSON failed', {
      details: error?.payload ?? null,
    });
  }
});

// ── NTA zone-based overview (reads pre-built JSON + GeoJSON, no backend needed) ──
const NTA_GEOJSON_PATH = path.join(__dirname, 'data', 'boundaries', 'nta_2020.geojson');
const NTA_SCORES_DIR = path.join(__dirname, 'data', 'cache', 'overview');
let _ntaBoundaryCache = null;
const _ntaScoresCache = new Map();
let _ntaManifestCache = null;

function loadNtaBoundaries() {
  if (_ntaBoundaryCache) return _ntaBoundaryCache;
  try {
    const raw = fs.readFileSync(NTA_GEOJSON_PATH, 'utf8');
    const geojson = JSON.parse(raw);
    const byCode = new Map();
    for (const feat of geojson.features || []) {
      const code = feat.properties?.NTA2020 || feat.properties?.nta2020;
      if (code) byCode.set(code, feat);
    }
    _ntaBoundaryCache = byCode;
    return byCode;
  } catch { return new Map(); }
}

function loadNtaScores(tag) {
  const scoreTag = tag === 'general' ? 'overall' : tag;
  if (_ntaScoresCache.has(scoreTag)) return _ntaScoresCache.get(scoreTag);
  const jsonPath = path.join(NTA_SCORES_DIR, `overview_${scoreTag}_nta.json`);
  try {
    const raw = fs.readFileSync(jsonPath, 'utf8');
    if (!_ntaManifestCache) {
      _ntaManifestCache = JSON.parse(
        fs.readFileSync(path.join(NTA_SCORES_DIR, 'overview.manifest.json'), 'utf8'),
      );
    }
    const ntaStamp = _ntaManifestCache.nta;
    const expectedHash = ntaStamp?.json_sha256?.[scoreTag];
    const actualHash = crypto.createHash('sha256').update(raw).digest('hex');
    const valid =
      _ntaManifestCache.methodology_version === EXPECTED_METHODOLOGY_VERSION &&
      ntaStamp?.methodology_version === EXPECTED_METHODOLOGY_VERSION &&
      Number(ntaStamp?.zones?.[scoreTag]) > 0 &&
      expectedHash === actualHash;
    if (!valid) {
      console.warn(`  NTA overview ${scoreTag} rejected: stale or unverified artifact`);
      return [];
    }
    const zones = JSON.parse(raw);
    if (!Array.isArray(zones) || zones.length !== Number(ntaStamp.zones[scoreTag])) {
      console.warn(`  NTA overview ${scoreTag} rejected: zone count mismatch`);
      return [];
    }
    _ntaScoresCache.set(scoreTag, zones);
    return zones;
  } catch { return []; }
}

app.get('/api/overview/nta-geojson', (req, res) => {
  const requestedTag = typeof req.query.tag === 'string' ? req.query.tag.toLowerCase() : 'general';
  const tag = TAG_STYLES[requestedTag] ? requestedTag : 'general';
  try {
    const zones = loadNtaScores(tag);
    const boundaries = loadNtaBoundaries();
    if (!zones.length || !boundaries.size) {
      return res.json({ type: 'FeatureCollection', features: [], metadata: { tag, zone_count: 0, overview_ready: false } });
    }
    const features = zones.map((zone) => {
      const boundary = boundaries.get(zone.nta_code);
      if (!boundary) return null;
      const raw = scoreForTag(zone, tag);
      if (!Number.isFinite(raw)) return null;
      const coverage = coverageForTag(zone, tag);
      return {
        type: 'Feature',
        properties: {
          nta_code: zone.nta_code,
          nta_name: zone.nta_name || boundary.properties?.NTAName || boundary.properties?.ntaname || '',
          borough: zone.borough || boundary.properties?.BoroName || boundary.properties?.boroname || '',
          nta_type: zone.nta_type || '0',
          score: raw,
          display_score: Math.max(0, Math.min(100, Math.round(raw))),
          cell_count: zone.cell_count || 0,
          risk_level: zone.risk_level || 'unknown',
          overall_score: zone.overall_score ?? null,
          safety_score: zone.safety_score ?? null,
          transit_score: zone.transit_score ?? null,
          amenities_score: zone.amenities_score ?? null,
          ...coverage,
          tag,
        },
        geometry: boundary.geometry,
      };
    }).filter(Boolean);
    res.json({
      type: 'FeatureCollection',
      features,
      metadata: {
        tag,
        zone_count: features.length,
        scoring_mode: 'absolute',
        overview_ready: true,
        methodology_version: EXPECTED_METHODOLOGY_VERSION,
      },
    });
  } catch (error) {
    sendProxyError(res, 502, 'NTA overview GeoJSON failed', { details: error?.message ?? null });
  }
});

app.get('/api/render/global', async (req, res) => {
  const requestedTag = typeof req.query.tag === 'string' ? req.query.tag.toLowerCase() : 'general';
  const tag = TAG_STYLES[requestedTag] ? requestedTag : 'general';

  try {
    const payload = await backendRequest('/api/overview', {
      method: 'POST',
      body: mapTagToOverviewRequest(tag),
    });

    res.json({
      ok: true,
      schema_version: payload.schema_version ?? 'v3.7.6',
      tag,
      points: cellsToRenderPoints(payload.cells, tag),
      source: 'backend_overview',
      overview_ready: Boolean(payload.coverage?.overview_ready ?? payload.overview_ready),
      coverage: payload.coverage ?? null,
      ui_message: payload.ui_message ?? payload.coverage?.ui_message ?? null,
    });
  } catch (error) {
    sendProxyError(res, 502, 'Overview proxy failed', {
      tag,
      points: [],
      details: error.payload ?? null,
    });
  }
});

// ── Disk-backed response cache for slow backend queries ──
//
// Versioned by backend methodology and bounded by a TTL. The key contains
// every request field that changes the result (including time_window_days and
// data_mode) and keeps coordinates to sub-metre precision. The TTL is also the
// invalidation guard for a refreshed data snapshot that intentionally keeps
// the same methodology version.
const CACHE_DIR = process.env.URBAN_DOSSIER_API_CACHE_DIR
  ? path.resolve(process.env.URBAN_DOSSIER_API_CACHE_DIR)
  : path.join(__dirname, 'data', 'cache', 'api');
const configuredCacheTtl = Number(process.env.URBAN_DOSSIER_API_CACHE_TTL_MS || 900000);
const CACHE_TTL_MS = Number.isFinite(configuredCacheTtl) && configuredCacheTtl >= 0
  ? configuredCacheTtl
  : 900000;
try { fs.mkdirSync(CACHE_DIR, { recursive: true }); } catch {}

let cacheVersion = null;
async function ensureCacheVersion() {
  if (cacheVersion) return cacheVersion;
  try {
    const registry = await backendRequest('/api/metrics');
    if (registry?.methodology_version) {
      cacheVersion = String(registry.methodology_version).replace(/[^0-9A-Za-z.-]/g, '');
      for (const file of fs.readdirSync(CACHE_DIR)) {
        if (!file.startsWith(`v${cacheVersion}_`)) {
          try { fs.unlinkSync(path.join(CACHE_DIR, file)); } catch {}
        }
      }
    }
  } catch {
    // Backend not up yet; stay uncached rather than serve stale.
  }
  return cacheVersion;
}


async function cacheGet(prefix, key) {
  const version = await ensureCacheVersion();
  if (!version) return null;
  try {
    const fp = path.join(CACHE_DIR, `v${version}_${prefix}_${key}.json`);
    if (!fs.existsSync(fp)) return null;
    const ageMs = Date.now() - fs.statSync(fp).mtimeMs;
    if (CACHE_TTL_MS === 0 || ageMs > CACHE_TTL_MS) {
      try { fs.unlinkSync(fp); } catch {}
      return null;
    }
    return JSON.parse(fs.readFileSync(fp, 'utf8'));
  } catch { return null; }
}

async function cacheSet(prefix, key, data) {
  const version = await ensureCacheVersion();
  if (!version) return;
  try {
    const fp = path.join(CACHE_DIR, `v${version}_${prefix}_${key}.json`);
    const tmp = `${fp}.${process.pid}.tmp`;
    fs.writeFileSync(tmp, JSON.stringify(data));
    fs.renameSync(tmp, fp);
  } catch {}
}

app.post('/api/detail/preview', async (req, res) => {
  const key = previewCacheKey(req.body);
  const cached = await cacheGet('preview', key);
  if (cached) return res.json(cached);
  try {
    const payload = await backendRequest('/api/detail/preview', {
      method: 'POST',
      body: req.body,
    });
    await cacheSet('preview', key, payload);
    res.json(payload);
  } catch (error) {
    sendProxyError(res, 502, 'Detail preview proxy failed', { details: error.payload ?? null });
  }
});

app.post('/api/analyze-point', async (req, res) => {
  const key = previewCacheKey(req.body) + '_' + (req.body?.report_mode ?? 'individual');
  const cached = await cacheGet('report', key);
  if (cached) return res.json(cached);
  try {
    const payload = await backendRequest('/api/analyze-point', {
      method: 'POST',
      body: req.body,
    });
    await cacheSet('report', key, payload);
    res.json(payload);
  } catch (error) {
    sendProxyError(res, 502, 'Detail report proxy failed', { details: error.payload ?? null });
  }
});

// Agent tool endpoints. Node stays a pure proxy: scoring and dataset access
// belong to FastAPI.
app.post('/api/compare-points', async (req, res) => {
  try {
    const payload = await backendRequest('/api/compare-points', { method: 'POST', body: req.body });
    res.json(payload);
  } catch (error) {
    sendProxyError(res, 502, 'Compare points failed', { details: error.payload ?? null });
  }
});

app.post('/api/dataset/query', async (req, res) => {
  try {
    const payload = await backendRequest('/api/dataset/query', { method: 'POST', body: req.body });
    res.json(payload);
  } catch (error) {
    sendProxyError(res, 502, 'Dataset query failed', { details: error.payload ?? null });
  }
});

app.post('/api/isochrone', async (req, res) => {
  try {
    const payload = await backendRequest('/api/isochrone', { method: 'POST', body: req.body });
    res.json(payload);
  } catch (error) {
    sendProxyError(res, 502, 'Isochrone failed', { details: error.payload ?? null });
  }
});

app.post('/api/simulate', async (req, res) => {
  try {
    const payload = await backendRequest('/api/simulate', { method: 'POST', body: req.body });
    res.json(payload);
  } catch (error) {
    sendProxyError(res, 502, 'Simulation failed', { details: error.payload ?? null });
  }
});

app.post('/api/watchlist/run', async (req, res) => {
  try {
    const payload = await backendRequest('/api/watchlist/run', {
      method: 'POST',
      body: req.body,
    });
    res.json(payload);
  } catch (error) {
    sendProxyError(res, 502, 'Watchlist proxy failed', { details: error.payload ?? null });
  }
});

// ── Agent Mode endpoints (pass-through to Python backend) ──────────────

app.get('/api/agent/status', async (req, res) => {
  try {
    const payload = await backendRequest('/api/agent/status');
    res.json(payload);
  } catch (error) {
    // Agent not available is not an error — return disabled status
    res.json({ enabled: false, reason: 'backend_unavailable' });
  }
});

app.post('/api/agent/session', async (req, res) => {
  try {
    const payload = await backendRequest('/api/agent/session', { method: 'POST', body: req.body });
    res.json(payload);
  } catch (error) {
    sendProxyError(res, 502, 'Agent session creation failed', { details: error.payload ?? null });
  }
});

// /api/agent/chat was proxied here beside /ask. Two entry points to the same
// agent is what PROJECT_PLAN P0-01 set out to end, and the frontend stopped
// calling this one when AgentChat moved to /ask, so it was a second contract
// nobody exercised and nothing tested. Gone.

// v2 structured agent loop. Node stays a pure proxy here: the ReAct loop,
// tool dispatch and evidence assembly all belong to FastAPI. Note this route
// can run for several LLM round-trips, so it relies on BACKEND_TIMEOUT_MS
// (default 180s) rather than a shorter per-route timeout.
app.post('/api/agent/ask', async (req, res) => {
  try {
    const payload = await backendRequest('/api/agent/ask', { method: 'POST', body: req.body });
    res.json(payload);
  } catch (error) {
    sendProxyError(res, 502, 'Agent ask failed', { details: error.payload ?? null });
  }
});

app.post('/api/agent/report', async (req, res) => {
  try {
    const payload = await backendRequest('/api/agent/report', { method: 'POST', body: req.body });
    res.json(payload);
  } catch (error) {
    sendProxyError(res, 502, 'Agent report generation failed', { details: error.payload ?? null });
  }
});

app.post('/api/agent/poster', async (req, res) => {
  try {
    const payload = await backendRequest('/api/agent/poster', { method: 'POST', body: req.body });
    res.json(payload);
  } catch (error) {
    sendProxyError(res, 502, 'Agent poster generation failed', { details: error.payload ?? null });
  }
});

app.post('/api/agent/refine', async (req, res) => {
  try {
    const payload = await backendRequest('/api/agent/refine', { method: 'POST', body: req.body });
    res.json(payload);
  } catch (error) {
    sendProxyError(res, 502, 'Agent report refinement failed', { details: error.payload ?? null });
  }
});

app.post('/api/render/local', async (req, res) => {
  const latitude = Number(req.body?.latitude);
  const longitude = Number(req.body?.longitude);
  const requestedRadius = Number(req.body?.radius_m);
  const requestedTag = typeof req.body?.tag === 'string' ? req.body.tag.toLowerCase() : 'general';
  const tag = TAG_STYLES[requestedTag] ? requestedTag : 'general';
  const radius_m = ALLOWED_RADIUS_METERS.has(requestedRadius) ? requestedRadius : 200;
  const priority_order = Array.isArray(req.body?.priority_order)
    ? req.body.priority_order.filter((value) => typeof value === 'string').slice(0, 3)
    : ['Amenities', 'Transit', 'Safety'];

  if (!Number.isFinite(latitude) || !Number.isFinite(longitude)) {
    return res.status(400).json({
      error: 'latitude and longitude are required',
      expected_body: {
        latitude: 40.758,
        longitude: -73.9855,
        radius_m: 200,
        tag: 'general|safety|transit|amenities',
      },
    });
  }

  try {
    const payload = await backendRequest('/api/detail/preview', {
      method: 'POST',
      body: {
        latitude,
        longitude,
        radius_m,
        priority_order,
        time_window_days: 365,
      },
    });

    res.json({
      ok: true,
      schema_version: payload.schema_version ?? 'v3.7.6',
      center: {
        latitude,
        longitude,
      },
      radius_m,
      available_radii_m: [200, 500, 1000],
      tag,
      points: previewToRenderPoints(tag, payload),
      style: TAG_STYLES[tag],
      source: 'backend_detail_preview',
      legend: {
        title: `${TAG_STYLES[tag].legend} score`,
        description: 'Lower score uses a more saturated color.',
        stops: [
          { label: '0 (lowest)', color: TAG_STYLES[tag].low },
          { label: '100 (highest)', color: TAG_STYLES[tag].high },
        ],
      },
    });
  } catch (error) {
    sendProxyError(res, 502, 'Local render proxy failed', {
      center: { latitude, longitude },
      radius_m,
      tag,
      points: [],
      details: error.payload ?? null,
    });
  }
});

// Tile endpoint: /tiles/{z}/{x}/{y}.pbf
app.get('/tiles/:z/:x/:y.pbf', (req, res) => {
  const { z, x, y } = req.params;
  const zInt = parseInt(z);
  const xInt = parseInt(x);
  const yFlipped = (1 << zInt) - 1 - parseInt(y);

  const row = db.prepare(`
    SELECT tile_data FROM tiles
    WHERE zoom_level = ? AND tile_column = ? AND tile_row = ?
  `).get(zInt, xInt, yFlipped);

  if (!row) {
    return res.status(404).send('Tile not found');
  }

  res.set({
    'Content-Type': 'application/x-protobuf',
    'Content-Encoding': 'gzip',
    'Cache-Control': 'public, max-age=86400',
  });
  res.send(row.tile_data);
});

// Per-building score tiles: /building-tiles/{z}/{x}/{y}.pbf
//
// Served from a second mbtiles so the OpenMapTiles basemap stays a pristine
// artefact that can be regenerated without carrying our scores along.
app.get('/building-tiles/:z/:x/:y.pbf', (req, res) => {
  if (!buildingDb) {
    return res.status(404).send('Building score tiles not built');
  }
  const zInt = parseInt(req.params.z, 10);
  const xInt = parseInt(req.params.x, 10);
  const yInt = parseInt(req.params.y, 10);
  if (!Number.isInteger(zInt) || !Number.isInteger(xInt) || !Number.isInteger(yInt)) {
    return res.status(400).send('Invalid tile coordinate');
  }
  const yFlipped = (1 << zInt) - 1 - yInt;

  const row = buildingDb.prepare(`
    SELECT tile_data FROM tiles
    WHERE zoom_level = ? AND tile_column = ? AND tile_row = ?
  `).get(zInt, xInt, yFlipped);

  if (!row) {
    return res.status(404).send('Tile not found');
  }

  res.set({
    'Content-Type': 'application/x-protobuf',
    'Content-Encoding': 'gzip',
    'Cache-Control': 'public, max-age=86400',
  });
  res.send(row.tile_data);
});

// Lets the client decide between the baked tileset and its JS fallback without
// probing for a 404 on a tile that may legitimately be empty.
//
// Also carries the colour domain the scoring pass measured. The scores do not
// span 0-100 -- overall sits between 34 and 68 for 96% of buildings -- so a
// ramp stretched over the full range paints the whole city its midpoint
// colour. Serving the measured percentiles keeps that decision with the data
// instead of hardcoding numbers in the client that quietly go stale.
function readColourDomains() {
  return buildingPublication.scores?.colour_domains ?? null;
}

app.get('/api/land-outline', async (req, res) => {
  try {
    sendJsonMaybeGzip(req, res, await backendRequest('/api/land-outline', { method: 'GET' }));
  } catch (error) {
    sendProxyError(res, 502, 'Land outline failed', { details: error.payload ?? null });
  }
});

app.get('/api/building-tiles/status', (req, res) => {
  res.json({
    available: buildingDb != null,
    colour_domains: buildingDb != null ? readColourDomains() : null,
    methodology_version: buildingDb != null ? EXPECTED_METHODOLOGY_VERSION : null,
    scoring_contract: buildingDb != null ? EXPECTED_BUILDING_SCORING_CONTRACT : null,
    unavailable_reason: buildingDb == null ? buildingPublication.reason : null,
  });
});

// Font glyph endpoint for MapLibre
// Glyphs for every label on the map.
//
// This served nothing at all. res.sendFile was given an absolute path, which
// Express 5 answers with "Not Found" when the path contains spaces -- and every
// fontstack here is "Open Sans Regular" or "Open Sans Bold". The handler then
// fell through to its own last resort and returned an empty buffer with a 200,
// so the failure was completely silent: the client saw a valid, glyphless font.
//
// The visible result was subtle enough to be mistaken for a data problem. No
// Latin label rendered anywhere, while the handful of POIs whose OSM name is in
// Chinese still appeared, because MapLibre draws CJK locally from system fonts
// and never asks this endpoint for them. A map that shows eight Chinatown
// restaurants and no street names looks like dirty data; it was a broken file
// server.
//
// Uses the root form, which is what Express 5 documents and which also confines
// the lookup to the font directory rather than relying on the segment check
// alone.
const FONT_ROOT = path.join(__dirname, 'public', 'fonts');
const FALLBACK_FONTSTACK = 'Open Sans Regular';

app.get('/fonts/:fontstack/:range.pbf', (req, res) => {
  const { fontstack, range } = req.params;
  if (!isSafePathSegment(fontstack) || !isSafePathSegment(range)) {
    return res.status(400).send('Invalid font path');
  }
  const opts = { root: FONT_ROOT };
  res.sendFile(path.join(fontstack, `${range}.pbf`), opts, (err) => {
    if (!err) return;
    res.sendFile(path.join(FALLBACK_FONTSTACK, `${range}.pbf`), opts, (err2) => {
      if (!err2) return;
      // A genuinely absent range -- most of Unicode is not in these files, and
      // MapLibre expects an empty response for the ranges a font does not
      // cover. Logged so an absent *font* cannot hide here again.
      if (range === '0-255') {
        console.warn(`  Glyphs missing for the basic Latin range of "${fontstack}"`);
      }
      res.status(200).set('Content-Type', 'application/x-protobuf').send(Buffer.alloc(0));
    });
  });
});

// Static assets for standalone offline test pages
app.use('/public', express.static(path.join(__dirname, 'public')));
app.use('/vendor/maplibre-gl', express.static(path.join(__dirname, 'node_modules', 'maplibre-gl', 'dist')));

app.get('/building-id-test', (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'building-id-test.html'));
});

app.get('/global-render', (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'global-render.html'));
});

// Serve React production build if available
const distPath = path.join(__dirname, 'interactive-map-explorer', 'dist');
if (fs.existsSync(distPath)) {
  app.use(express.static(distPath));
  // Unmatched data routes 404 cleanly instead of falling through to the SPA.
  // The fallback below is app.use, which matches EVERY method -- so a
  // misspelled or removed API path used to answer 200 with index.html, and a
  // client would try to JSON.parse a page of HTML and report an error with
  // nothing to do with the cause. This bit three separate times (a POST to a
  // GET-only route, the removed plateau tiles, the removed chat endpoint)
  // before earning its fix.
  app.use((req, res, next) => {
    if (/^\/(api|tiles|fonts|building-tiles|plateau-dem)\//.test(req.path)) {
      return res.status(404).json({ detail: `No such route: ${req.method} ${req.path}` });
    }
    next();
  });
  // SPA fallback -- now only for page navigations.
  app.use((req, res) => {
    res.sendFile(path.join(distPath, 'index.html'));
  });
} else {
  // Fallback to simple demo page
  app.use(express.static(path.join(__dirname, 'public')));
}

app.listen(PORT, HOST, () => {
  console.log(`\n  Urban Dossier NYC Map - Offline Mode`);
  console.log(`  ──────────────────────────────────`);
  console.log(`  Tile server: http://${HOST}:${PORT}/tiles/{z}/{x}/{y}.pbf`);
  if (fs.existsSync(distPath)) {
    console.log(`  Frontend:    http://${HOST}:${PORT} (production build)`);
  } else {
    console.log(`  Frontend:    http://${HOST}:${PORT} (built-in offline fallback page)`);
    console.log(`               Optional React build: cd interactive-map-explorer && npm run build`);
  }
  console.log(`  完全离线运行，无需联网\n`);
});
