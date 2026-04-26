import maplibregl from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';

type LngLat = [number, number];
type FeatureCollection = GeoJSON.FeatureCollection<GeoJSON.Geometry>;

const SEARCH_DIAMETER_KM = 5;
const SEARCH_RADIUS_KM = SEARCH_DIAMETER_KM / 2;
const DEFAULT_CENTER: LngLat = [-73.98513, 40.758896];
const EMPTY_POLYGON = createCirclePolygon(DEFAULT_CENTER, 0.01);

const NYC_LANDMARKS: Record<string, LngLat> = {
  'times square': [-73.9855, 40.758],
  'central park': [-73.9654, 40.7829],
  'statue of liberty': [-74.0445, 40.6892],
  'brooklyn bridge': [-73.9969, 40.7061],
  'empire state': [-73.9857, 40.7484],
  'wall street': [-74.0113, 40.7074],
  'grand central': [-73.9772, 40.7527],
  'penn station': [-73.9935, 40.7506],
  'columbia university': [-73.9626, 40.8075],
  'rockefeller center': [-73.9787, 40.7587],
  'madison square garden': [-73.9934, 40.7505],
  'washington square': [-73.9973, 40.7308],
  'high line': [-74.0048, 40.748],
};

const app = document.getElementById('app');

if (!app) {
  throw new Error('Missing #app container');
}

document.body.style.margin = '0';
document.body.style.fontFamily = '"Segoe UI", sans-serif';

app.innerHTML = `
  <div style="position:relative;height:100vh;width:100vw;overflow:hidden;background:#f0ede9;">
    <div id="map" style="height:100%;width:100%;"></div>
    <div style="position:absolute;top:16px;left:16px;z-index:2;width:min(420px,calc(100vw - 32px));padding:16px 18px;border-radius:16px;background:rgba(255,255,255,0.94);box-shadow:0 12px 30px rgba(0,0,0,0.16);line-height:1.5;">
      <div style="font-size:18px;font-weight:700;">Offline Address Highlight Map</div>
      <div style="margin-top:8px;font-size:14px;color:#444;">
        Enter a NYC landmark name or coordinates like <strong>40.7580,-73.9855</strong>.
      </div>
      <div style="display:flex;gap:10px;margin-top:12px;">
        <input id="search-input" type="text" value="Times Square" placeholder="Landmark or lat,lng" style="flex:1;min-width:0;height:42px;padding:0 14px;border:1px solid #d5d1cb;border-radius:12px;font-size:14px;outline:none;" />
        <button id="search-button" style="height:42px;padding:0 16px;border:none;border-radius:12px;background:#222;color:#fff;font-size:14px;font-weight:600;cursor:pointer;">
          Search
        </button>
      </div>
      <div id="status" style="margin-top:10px;font-size:13px;color:#666;">
        Buildings intersecting the 5km diameter area will turn red.
      </div>
    </div>
  </div>
`;

const searchInput = document.getElementById('search-input') as HTMLInputElement | null;
const searchButton = document.getElementById('search-button') as HTMLButtonElement | null;
const statusEl = document.getElementById('status');

if (!searchInput || !searchButton || !statusEl) {
  throw new Error('Missing UI elements');
}

let activeCenter: LngLat = DEFAULT_CENTER;
let mapLoaded = false;

const style: maplibregl.StyleSpecification = {
  version: 8,
  glyphs: `${window.location.origin}/fonts/{fontstack}/{range}.pbf`,
  sources: {
    openmaptiles: {
      type: 'vector',
      tiles: [`${window.location.origin}/tiles/{z}/{x}/{y}.pbf`],
      minzoom: 0,
      maxzoom: 14,
    },
    highlightArea: {
      type: 'geojson',
      data: EMPTY_POLYGON,
    },
    highlightBuildings: {
      type: 'geojson',
      data: emptyFeatureCollection(),
    },
    normalBuildings3d: {
      type: 'geojson',
      data: emptyFeatureCollection(),
    },
    searchPoint: {
      type: 'geojson',
      data: emptyFeatureCollection(),
    },
  },
  layers: [
    { id: 'background', type: 'background', paint: { 'background-color': '#f0ede9' } },
    {
      id: 'water',
      type: 'fill',
      source: 'openmaptiles',
      'source-layer': 'water',
      paint: { 'fill-color': '#a3cfec', 'fill-opacity': 0.8 },
    },
    {
      id: 'waterway',
      type: 'line',
      source: 'openmaptiles',
      'source-layer': 'waterway',
      paint: { 'line-color': '#a3cfec', 'line-width': 1.5 },
    },
    {
      id: 'landcover',
      type: 'fill',
      source: 'openmaptiles',
      'source-layer': 'landcover',
      paint: { 'fill-color': '#d4edaa', 'fill-opacity': 0.4 },
    },
    {
      id: 'landuse',
      type: 'fill',
      source: 'openmaptiles',
      'source-layer': 'landuse',
      paint: {
        'fill-color': [
          'match', ['get', 'class'],
          'residential', '#ede8e3',
          'commercial', '#f5e6d0',
          'industrial', '#e8e0d8',
          'park', '#c8e6a0',
          'cemetery', '#d6e4c0',
          'hospital', '#f8d8d8',
          'school', '#f0e4d0',
          '#e8e4de',
        ],
        'fill-opacity': 0.4,
      },
    },
    {
      id: 'park-fill',
      type: 'fill',
      source: 'openmaptiles',
      'source-layer': 'park',
      paint: { 'fill-color': '#c8e6a0', 'fill-opacity': 0.5 },
    },
    {
      id: 'building',
      type: 'fill',
      source: 'openmaptiles',
      'source-layer': 'building',
      minzoom: 13,
      paint: {
        'fill-color': '#d4cfc8',
        'fill-opacity': 0.7,
        'fill-outline-color': '#bab6ae',
      },
    },
    {
      id: 'building-highlight',
      type: 'fill',
      source: 'highlightBuildings',
      minzoom: 13,
      paint: {
        'fill-color': '#e44743',
        'fill-opacity': 0.92,
        'fill-outline-color': '#a32824',
      },
    },
    {
      id: 'building-3d',
      type: 'fill-extrusion',
      source: 'normalBuildings3d',
      minzoom: 15,
      paint: {
        'fill-extrusion-color': '#d4cfc8',
        'fill-extrusion-height': ['coalesce', ['get', 'render_height'], 10],
        'fill-extrusion-base': ['coalesce', ['get', 'render_min_height'], 0],
        'fill-extrusion-opacity': 0.7,
      },
    },
    {
      id: 'building-highlight-3d',
      type: 'fill-extrusion',
      source: 'highlightBuildings',
      minzoom: 15,
      paint: {
        'fill-extrusion-color': '#e44743',
        'fill-extrusion-height': ['coalesce', ['get', 'render_height'], 10],
        'fill-extrusion-base': ['coalesce', ['get', 'render_min_height'], 0],
        'fill-extrusion-opacity': 0.92,
      },
    },
    {
      id: 'road-motorway',
      type: 'line',
      source: 'openmaptiles',
      'source-layer': 'transportation',
      filter: ['==', 'class', 'motorway'],
      layout: { 'line-cap': 'round', 'line-join': 'round' },
      paint: { 'line-color': '#ffa35c', 'line-width': { stops: [[5, 0.5], [14, 6], [18, 12]] } },
    },
    {
      id: 'road-primary',
      type: 'line',
      source: 'openmaptiles',
      'source-layer': 'transportation',
      filter: ['in', 'class', 'trunk', 'primary'],
      layout: { 'line-cap': 'round', 'line-join': 'round' },
      paint: { 'line-color': '#ffd080', 'line-width': { stops: [[5, 0.3], [14, 4], [18, 10]] } },
    },
    {
      id: 'road-secondary',
      type: 'line',
      source: 'openmaptiles',
      'source-layer': 'transportation',
      filter: ['==', 'class', 'secondary'],
      layout: { 'line-cap': 'round', 'line-join': 'round' },
      paint: { 'line-color': '#f0e8c0', 'line-width': { stops: [[8, 0.3], [14, 3], [18, 8]] } },
    },
    {
      id: 'road-minor',
      type: 'line',
      source: 'openmaptiles',
      'source-layer': 'transportation',
      filter: ['in', 'class', 'minor', 'service', 'street'],
      minzoom: 12,
      layout: { 'line-cap': 'round', 'line-join': 'round' },
      paint: { 'line-color': '#ffffff', 'line-width': { stops: [[12, 0.5], [14, 2], [18, 6]] } },
    },
    {
      id: 'road-path',
      type: 'line',
      source: 'openmaptiles',
      'source-layer': 'transportation',
      filter: ['in', 'class', 'path', 'track'],
      minzoom: 14,
      layout: { 'line-cap': 'round' },
      paint: { 'line-color': '#ccc', 'line-width': 1, 'line-dasharray': [2, 2] },
    },
    {
      id: 'search-point',
      type: 'circle',
      source: 'searchPoint',
      paint: {
        'circle-radius': 6,
        'circle-color': '#cf3a36',
        'circle-stroke-width': 2,
        'circle-stroke-color': '#fff',
      },
    },
    {
      id: 'highlight-area-outline',
      type: 'line',
      source: 'highlightArea',
      paint: { 'line-color': '#cf3a36', 'line-width': 1.5, 'line-opacity': 0.25 },
    },
  ],
};

const map = new maplibregl.Map({
  container: 'map',
  style,
  center: DEFAULT_CENTER,
  zoom: 13,
  maxZoom: 20,
});

map.addControl(new maplibregl.NavigationControl(), 'top-left');

map.on('load', () => {
  mapLoaded = true;
  updateSearchArea(DEFAULT_CENTER, 'Times Square');
});

map.on('moveend', () => {
  if (mapLoaded) refreshBuildingSources(activeCenter, SEARCH_RADIUS_KM);
});

map.on('click', (event) => {
  const center: LngLat = [event.lngLat.lng, event.lngLat.lat];
  searchInput.value = `${event.lngLat.lat.toFixed(6)}, ${event.lngLat.lng.toFixed(6)}`;
  updateSearchArea(center, 'Clicked point');
});

searchButton.addEventListener('click', runSearch);
searchInput.addEventListener('keydown', (event) => {
  if (event.key === 'Enter') runSearch();
});

function runSearch() {
  const query = searchInput.value.trim();
  const result = resolveQuery(query);
  if (!result) {
    statusEl.textContent = 'No offline match found. Try a NYC landmark or lat,lng.';
    return;
  }
  updateSearchArea(result.center, result.label);
}

function updateSearchArea(center: LngLat, label: string) {
  if (!mapLoaded) return;
  activeCenter = center;

  const radiusPolygon = createCirclePolygon(center, SEARCH_RADIUS_KM);
  const areaSource = map.getSource('highlightArea') as maplibregl.GeoJSONSource | undefined;
  const pointSource = map.getSource('searchPoint') as maplibregl.GeoJSONSource | undefined;

  if (areaSource) areaSource.setData(radiusPolygon);
  if (pointSource) pointSource.setData(pointFeature(center, label));

  refreshBuildingSources(center, SEARCH_RADIUS_KM);

  map.fitBounds(getBoundsForCircle(center, SEARCH_RADIUS_KM), {
    padding: 60,
    duration: 900,
    maxZoom: 14,
  });

  statusEl.textContent = `Centered on ${label}. Buildings intersecting the ${SEARCH_DIAMETER_KM}km diameter area are red.`;
}

function refreshBuildingSources(center: LngLat, radiusKm: number) {
  const highlightSource = map.getSource('highlightBuildings') as maplibregl.GeoJSONSource | undefined;
  const normal3dSource = map.getSource('normalBuildings3d') as maplibregl.GeoJSONSource | undefined;
  if (!highlightSource || !normal3dSource) return;

  const { highlighted, normal3d } = collectVisibleBuildings(center, radiusKm);
  highlightSource.setData(highlighted);
  normal3dSource.setData(normal3d);
}

function collectVisibleBuildings(center: LngLat, radiusKm: number) {
  const features = map.querySourceFeatures('openmaptiles', { sourceLayer: 'building' });
  const seen = new Set<string>();
  const highlighted: GeoJSON.Feature[] = [];
  const normal3d: GeoJSON.Feature[] = [];

  for (const feature of features) {
    if (!feature.geometry) continue;
    const signature = JSON.stringify(feature.geometry);
    if (seen.has(signature)) continue;
    seen.add(signature);

    const outputFeature: GeoJSON.Feature = {
      type: 'Feature',
      properties: feature.properties ?? {},
      geometry: feature.geometry,
    };

    if (geometryIntersectsCircle(feature.geometry, center, radiusKm)) {
      highlighted.push(outputFeature);
    } else {
      normal3d.push(outputFeature);
    }
  }

  return {
    highlighted: { type: 'FeatureCollection', features: highlighted } as FeatureCollection,
    normal3d: { type: 'FeatureCollection', features: normal3d } as FeatureCollection,
  };
}

function resolveQuery(query: string): { center: LngLat; label: string } | null {
  if (!query) return null;

  const coordinateMatch = query.match(/^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$/);
  if (coordinateMatch) {
    const lat = Number(coordinateMatch[1]);
    const lng = Number(coordinateMatch[2]);
    if (Number.isFinite(lat) && Number.isFinite(lng)) {
      return { center: [lng, lat], label: `${lat.toFixed(4)}, ${lng.toFixed(4)}` };
    }
  }

  const normalized = query.toLowerCase();
  for (const [name, center] of Object.entries(NYC_LANDMARKS)) {
    if (name.includes(normalized) || normalized.includes(name)) {
      return { center, label: toTitleCase(name) };
    }
  }

  return null;
}

function createCirclePolygon(center: LngLat, radiusKm: number): GeoJSON.Feature<GeoJSON.Polygon> {
  const steps = 72;
  const earthRadiusKm = 6371.0088;
  const [lng, lat] = center;
  const latRad = toRadians(lat);
  const lngRad = toRadians(lng);
  const angularDistance = radiusKm / earthRadiusKm;
  const coordinates: number[][] = [];

  for (let i = 0; i <= steps; i += 1) {
    const bearing = (i / steps) * Math.PI * 2;
    const lat2 = Math.asin(
      Math.sin(latRad) * Math.cos(angularDistance) +
      Math.cos(latRad) * Math.sin(angularDistance) * Math.cos(bearing),
    );
    const lng2 = lngRad + Math.atan2(
      Math.sin(bearing) * Math.sin(angularDistance) * Math.cos(latRad),
      Math.cos(angularDistance) - Math.sin(latRad) * Math.sin(lat2),
    );
    coordinates.push([normalizeLongitude(toDegrees(lng2)), toDegrees(lat2)]);
  }

  return {
    type: 'Feature',
    properties: { radiusKm },
    geometry: { type: 'Polygon', coordinates: [coordinates] },
  };
}

function geometryIntersectsCircle(geometry: GeoJSON.Geometry, center: LngLat, radiusKm: number): boolean {
  if (geometry.type === 'Polygon') return polygonIntersectsCircle(geometry.coordinates, center, radiusKm);
  if (geometry.type === 'MultiPolygon') {
    return geometry.coordinates.some((polygon) => polygonIntersectsCircle(polygon, center, radiusKm));
  }
  return false;
}

function polygonIntersectsCircle(polygon: number[][][], center: LngLat, radiusKm: number): boolean {
  for (const ring of polygon) {
    for (const point of ring) {
      if (distanceKm([point[0], point[1]], center) <= radiusKm) return true;
    }
    for (let index = 0; index < ring.length - 1; index += 1) {
      if (segmentIntersectsCircle(ring[index], ring[index + 1], center, radiusKm)) return true;
    }
  }
  return pointInPolygon(center, polygon);
}

function segmentIntersectsCircle(start: number[], end: number[], center: LngLat, radiusKm: number): boolean {
  const meanLat = (start[1] + end[1] + center[1]) / 3;
  const startXY = projectToKm(start, meanLat);
  const endXY = projectToKm(end, meanLat);
  const centerXY = projectToKm(center, meanLat);
  const segmentX = endXY[0] - startXY[0];
  const segmentY = endXY[1] - startXY[1];
  const lengthSquared = segmentX * segmentX + segmentY * segmentY;

  if (lengthSquared === 0) return distanceKm([start[0], start[1]], center) <= radiusKm;

  const projection = ((centerXY[0] - startXY[0]) * segmentX + (centerXY[1] - startXY[1]) * segmentY) / lengthSquared;
  const clamped = Math.max(0, Math.min(1, projection));
  const closestX = startXY[0] + clamped * segmentX;
  const closestY = startXY[1] + clamped * segmentY;
  const dx = centerXY[0] - closestX;
  const dy = centerXY[1] - closestY;
  return dx * dx + dy * dy <= radiusKm * radiusKm;
}

function pointInPolygon(point: LngLat, polygon: number[][][]) {
  let inside = false;
  const [x, y] = point;
  for (const ring of polygon) {
    let ringInside = false;
    for (let i = 0, j = ring.length - 1; i < ring.length; j = i, i += 1) {
      const xi = ring[i][0];
      const yi = ring[i][1];
      const xj = ring[j][0];
      const yj = ring[j][1];
      const intersects = yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / ((yj - yi) || 1e-12) + xi;
      if (intersects) ringInside = !ringInside;
    }
    inside = ringInside ? !inside : inside;
  }
  return inside;
}

function getBoundsForCircle(center: LngLat, radiusKm: number): maplibregl.LngLatBoundsLike {
  const [lng, lat] = center;
  const latOffset = radiusKm / 111.32;
  const lngOffset = radiusKm / (111.32 * Math.cos(toRadians(lat)));
  return [[lng - lngOffset, lat - latOffset], [lng + lngOffset, lat + latOffset]];
}

function pointFeature(center: LngLat, label: string): FeatureCollection {
  return {
    type: 'FeatureCollection',
    features: [{
      type: 'Feature',
      properties: { label },
      geometry: { type: 'Point', coordinates: center },
    }],
  };
}

function emptyFeatureCollection(): FeatureCollection {
  return { type: 'FeatureCollection', features: [] };
}

function distanceKm(a: LngLat, b: LngLat) {
  const earthRadiusKm = 6371.0088;
  const dLat = toRadians(b[1] - a[1]);
  const dLng = toRadians(b[0] - a[0]);
  const lat1 = toRadians(a[1]);
  const lat2 = toRadians(b[1]);
  const sinLat = Math.sin(dLat / 2);
  const sinLng = Math.sin(dLng / 2);
  const h = sinLat * sinLat + Math.cos(lat1) * Math.cos(lat2) * sinLng * sinLng;
  return 2 * earthRadiusKm * Math.atan2(Math.sqrt(h), Math.sqrt(1 - h));
}

function projectToKm(point: number[] | LngLat, meanLat: number): [number, number] {
  const lngScale = 111.32 * Math.cos(toRadians(meanLat));
  const latScale = 111.32;
  return [point[0] * lngScale, point[1] * latScale];
}

function toRadians(value: number) {
  return (value * Math.PI) / 180;
}

function toDegrees(value: number) {
  return (value * 180) / Math.PI;
}

function normalizeLongitude(value: number) {
  return ((value + 540) % 360) - 180;
}

function toTitleCase(value: string) {
  return value.split(' ').map((word) => word.charAt(0).toUpperCase() + word.slice(1)).join(' ');
}
