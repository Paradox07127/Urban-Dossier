import { useEffect, useRef, type MutableRefObject } from 'react';
import maplibregl, { GeoJSONSource, Map as MapLibreMap, MapGeoJSONFeature } from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';
import { Location } from '../types';

type RenderTag = 'general' | 'safety' | 'transit' | 'amenities';
type Coordinate = [number, number];
type RadiusMeters = 200 | 500 | 1000;

interface LocalRenderTarget {
  center: [number, number];
  radiusM: RadiusMeters;
  priorityOrder?: string[];
}

interface HotspotData {
  center_lat: number;
  center_lon: number;
  radius_m: number;
  incident_count: number;
  dominant_type: string;
}

interface MapProps {
  center: [number, number];
  zoom: number;
  renderTag?: RenderTag;
  localRenderTarget?: LocalRenderTarget | null;
  refreshKey?: number;
  markers?: Location[];
  hotspots?: HotspotData[];
  onMarkerClick: (location: Location) => void;
  onMapClick: (lat: number, lng: number) => void;
}

type RenderPoint = {
  latitude: number;
  longitude: number;
  score: number;
  tag?: RenderTag;
  kind?: string;
  summary?: string;
};

type RenderPalette = {
  low: string;
  high: string;
  accent: string;
  label: string;
};

type RenderConfig = {
  mode: 'global' | 'local';
  tag: RenderTag;
  points: RenderPoint[];
  center?: Coordinate;
  radiusKm?: number;
};

const DEFAULT_BUILDING_STYLE = {
  fill: '#d4cfc8',
  outline: '#bab6ae',
};

const TAG_STYLES: Record<RenderTag, RenderPalette> = {
  general: { low: '#c51f1a', high: '#f3b78f', accent: '#7f120e', label: 'General' },
  safety: { low: '#b31274', high: '#f2a7cf', accent: '#73034c', label: 'Safety' },
  transit: { low: '#0f74b8', high: '#96d0f3', accent: '#084e82', label: 'Transit' },
  amenities: { low: '#19843c', high: '#9cd79d', accent: '#0d5b26', label: 'Amenities' },
};

const EMPTY_FEATURE_COLLECTION: GeoJSON.FeatureCollection = {
  type: 'FeatureCollection',
  features: [],
};

const MAP_STYLE: maplibregl.StyleSpecification = {
  version: 8,
  sources: {
    openmaptiles: {
      type: 'vector',
      tiles: [`${window.location.origin}/tiles/{z}/{x}/{y}.pbf`],
      minzoom: 0,
      maxzoom: 14,
    },
    renderedBuildings: {
      type: 'geojson',
      data: EMPTY_FEATURE_COLLECTION,
    },
    renderedBuildings3d: {
      type: 'geojson',
      data: EMPTY_FEATURE_COLLECTION,
    },
    renderRadius: {
      type: 'geojson',
      data: EMPTY_FEATURE_COLLECTION,
    },
    hotspotOverlay: {
      type: 'geojson',
      data: EMPTY_FEATURE_COLLECTION,
    },
    hexOverlay: {
      type: 'geojson',
      data: EMPTY_FEATURE_COLLECTION,
    },
  },
  glyphs: `${window.location.origin}/fonts/{fontstack}/{range}.pbf`,
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
      id: 'hex-overlay-fill',
      type: 'fill',
      source: 'hexOverlay',
      paint: {
        'fill-color': [
          'interpolate', ['linear'], ['get', 'display_score'],
          0, '#d73027',
          25, '#fc8d59',
          50, '#fee08b',
          75, '#91cf60',
          100, '#1a9850',
        ],
        // Crossfade: hex overlay fades out as buildings fade in
        'fill-opacity': ['interpolate', ['linear'], ['zoom'], 12, 0.55, 15, 0.08],
      },
    },
    {
      id: 'hex-overlay-line',
      type: 'line',
      source: 'hexOverlay',
      paint: {
        'line-color': '#ffffff',
        'line-width': 1.5,
        'line-opacity': ['interpolate', ['linear'], ['zoom'], 12, 0.7, 15, 0.0],
      },
    },
    {
      id: 'building',
      type: 'fill',
      source: 'renderedBuildings',
      minzoom: 12,
      paint: {
        'fill-color': ['coalesce', ['get', 'render_color'], '#d4cfc8'],
        // Crossfade: buildings fade in as hex overlay fades out
        'fill-opacity': ['interpolate', ['linear'], ['zoom'], 12, 0.15, 15, 0.88],
        'fill-outline-color': ['coalesce', ['get', 'outline_color'], '#bab6ae'],
      },
    },
    {
      id: 'building-3d',
      type: 'fill-extrusion',
      source: 'renderedBuildings3d',
      minzoom: 15,
      paint: {
        'fill-extrusion-color': ['coalesce', ['get', 'render_color'], '#d4cfc8'],
        'fill-extrusion-height': ['coalesce', ['get', 'render_height'], 10],
        'fill-extrusion-base': ['coalesce', ['get', 'render_min_height'], 0],
        'fill-extrusion-opacity': ['interpolate', ['linear'], ['zoom'], 15, 0, 15.5, 0.9],
      },
    },
    {
      id: 'render-radius-fill',
      type: 'fill',
      source: 'renderRadius',
      paint: {
        'fill-color': '#3b82f6',
        'fill-opacity': 0.10,
      },
    },
    {
      id: 'render-radius-line-glow',
      type: 'line',
      source: 'renderRadius',
      paint: {
        'line-color': '#ffffff',
        'line-width': 7,
        'line-opacity': 0.6,
      },
    },
    {
      id: 'render-radius-line',
      type: 'line',
      source: 'renderRadius',
      paint: {
        'line-color': '#1d4ed8',
        'line-width': 3.5,
        'line-opacity': 0.9,
      },
    },
    {
      id: 'hotspot-fill',
      type: 'fill',
      source: 'hotspotOverlay',
      paint: {
        'fill-color': '#ef4444',
        'fill-opacity': 0.18,
      },
    },
    {
      id: 'hotspot-line',
      type: 'line',
      source: 'hotspotOverlay',
      paint: {
        'line-color': '#ef4444',
        'line-width': 2.5,
        'line-opacity': 0.7,
        'line-dasharray': [3, 2],
      },
    },
    {
      id: 'road-motorway',
      type: 'line',
      source: 'openmaptiles',
      'source-layer': 'transportation',
      filter: ['==', 'class', 'motorway'],
      layout: { 'line-cap': 'round', 'line-join': 'round' },
      paint: {
        'line-color': '#8c8c8c',
        'line-width': { stops: [[5, 0.5], [14, 6], [18, 12]] },
      },
    },
    {
      id: 'road-primary',
      type: 'line',
      source: 'openmaptiles',
      'source-layer': 'transportation',
      filter: ['in', 'class', 'trunk', 'primary'],
      layout: { 'line-cap': 'round', 'line-join': 'round' },
      paint: {
        'line-color': '#a8a8a8',
        'line-width': { stops: [[5, 0.3], [14, 4], [18, 10]] },
      },
    },
    {
      id: 'road-secondary',
      type: 'line',
      source: 'openmaptiles',
      'source-layer': 'transportation',
      filter: ['==', 'class', 'secondary'],
      layout: { 'line-cap': 'round', 'line-join': 'round' },
      paint: {
        'line-color': '#c0c0c0',
        'line-width': { stops: [[8, 0.3], [14, 3], [18, 8]] },
      },
    },
    {
      id: 'road-minor',
      type: 'line',
      source: 'openmaptiles',
      'source-layer': 'transportation',
      filter: ['in', 'class', 'minor', 'service', 'street'],
      minzoom: 12,
      layout: { 'line-cap': 'round', 'line-join': 'round' },
      paint: {
        'line-color': '#ffffff',
        'line-width': { stops: [[12, 0.5], [14, 2], [18, 6]] },
      },
    },
    {
      id: 'road-path',
      type: 'line',
      source: 'openmaptiles',
      'source-layer': 'transportation',
      filter: ['in', 'class', 'path', 'track'],
      minzoom: 14,
      layout: { 'line-cap': 'round' },
      paint: {
        'line-color': '#ccc',
        'line-width': 1,
        'line-dasharray': [2, 2],
      },
    },
    {
      id: 'road-label',
      type: 'symbol',
      source: 'openmaptiles',
      'source-layer': 'transportation_name',
      minzoom: 14,
      layout: {
        'symbol-placement': 'line',
        'text-field': '{name}',
        'text-size': { stops: [[14, 10], [18, 14]] },
        'text-font': ['Open Sans Regular'],
        'text-max-angle': 30,
      },
      paint: {
        'text-color': '#555',
        'text-halo-color': '#fff',
        'text-halo-width': 1.5,
      },
    },
    {
      id: 'water-label',
      type: 'symbol',
      source: 'openmaptiles',
      'source-layer': 'water_name',
      layout: {
        'text-field': '{name}',
        'text-size': { stops: [[3, 10], [14, 16]] },
        'text-font': ['Open Sans Regular'],
      },
      paint: {
        'text-color': '#5888b0',
        'text-halo-color': 'rgba(255,255,255,0.7)',
        'text-halo-width': 1,
      },
    },
    {
      id: 'park-label',
      type: 'symbol',
      source: 'openmaptiles',
      'source-layer': 'park',
      minzoom: 11,
      layout: {
        'text-field': '{name}',
        'text-size': 11,
        'text-font': ['Open Sans Regular'],
      },
      paint: {
        'text-color': '#4a8040',
        'text-halo-color': 'rgba(255,255,255,0.8)',
        'text-halo-width': 1,
      },
    },
    {
      id: 'poi-label',
      type: 'symbol',
      source: 'openmaptiles',
      'source-layer': 'poi',
      minzoom: 15,
      layout: {
        'text-field': '{name}',
        'text-size': 11,
        'text-font': ['Open Sans Regular'],
        'text-offset': [0, 0.6],
        'text-anchor': 'top',
      },
      paint: {
        'text-color': '#666',
        'text-halo-color': '#fff',
        'text-halo-width': 1,
      },
    },
    {
      id: 'place-city',
      type: 'symbol',
      source: 'openmaptiles',
      'source-layer': 'place',
      filter: ['==', 'class', 'city'],
      layout: {
        'text-field': '{name}',
        'text-size': { stops: [[5, 14], [14, 24]] },
        'text-font': ['Open Sans Bold'],
      },
      paint: {
        'text-color': '#333',
        'text-halo-color': '#fff',
        'text-halo-width': 2,
      },
    },
    {
      id: 'place-suburb',
      type: 'symbol',
      source: 'openmaptiles',
      'source-layer': 'place',
      filter: ['in', 'class', 'suburb', 'neighbourhood', 'quarter'],
      minzoom: 12,
      layout: {
        'text-field': '{name}',
        'text-size': 13,
        'text-font': ['Open Sans Regular'],
        'text-transform': 'uppercase',
        'text-letter-spacing': 0.1,
      },
      paint: {
        'text-color': '#777',
        'text-halo-color': '#fff',
        'text-halo-width': 1.5,
      },
    },
  ],
};

function isRenderTag(value: string): value is RenderTag {
  return value === 'general' || value === 'safety' || value === 'transit' || value === 'amenities';
}

async function fetchGlobalRenderConfig(tag: RenderTag): Promise<RenderConfig> {
  try {
    const response = await fetch(`/api/render/global?tag=${encodeURIComponent(tag)}`);
    if (!response.ok) throw new Error(`Global render API returned ${response.status}`);

    const data = await response.json();
    return {
      mode: 'global',
      tag: typeof data.tag === 'string' && isRenderTag(data.tag) ? data.tag : tag,
      points: Array.isArray(data.points) ? data.points : [],
    };
  } catch {
    return { mode: 'global', tag, points: [] };
  }
}

async function fetchHexOverlayGeoJSON(tag: RenderTag): Promise<GeoJSON.FeatureCollection> {
  // Try NTA zone overlay first (better visual), fall back to H3 hex overlay
  try {
    const ntaResponse = await fetch(`/api/overview/nta-geojson?tag=${encodeURIComponent(tag)}`);
    if (ntaResponse.ok) {
      const ntaData = await ntaResponse.json();
      if (ntaData?.type === 'FeatureCollection' && ntaData.features?.length > 0) {
        return ntaData;
      }
    }
  } catch { /* fall through to H3 */ }
  try {
    const response = await fetch(`/api/overview/geojson?tag=${encodeURIComponent(tag)}`);
    if (!response.ok) throw new Error(`Overview GeoJSON API returned ${response.status}`);
    const data = await response.json();
    return data?.type === 'FeatureCollection' ? data : EMPTY_FEATURE_COLLECTION;
  } catch {
    return EMPTY_FEATURE_COLLECTION;
  }
}

async function fetchLocalRenderConfig(tag: RenderTag, target: LocalRenderTarget): Promise<RenderConfig> {
  try {
    const response = await fetch('/api/render/local', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        latitude: target.center[0],
        longitude: target.center[1],
        radius_m: target.radiusM,
        tag,
        priority_order: target.priorityOrder,
      }),
    });

    if (!response.ok) throw new Error(`Local render API returned ${response.status}`);

    const data = await response.json();
    return {
      mode: 'local',
      tag: typeof data.tag === 'string' && isRenderTag(data.tag) ? data.tag : tag,
      points: Array.isArray(data.points) ? data.points : [],
      center: [Number(data.center?.longitude ?? target.center[1]), Number(data.center?.latitude ?? target.center[0])],
      radiusKm: Number(data.radius_m ?? target.radiusM) / 1000,
    };
  } catch {
    return {
      mode: 'local',
      tag,
      points: [],
      center: [target.center[1], target.center[0]],
      radiusKm: target.radiusM / 1000,
    };
  }
}

// --- Overlay-based building scoring for global mode ---

function pointInPolygon(point: Coordinate, polygon: number[][]): boolean {
  // Ray-casting algorithm
  const [x, y] = point;
  let inside = false;
  for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
    const [xi, yi] = polygon[i];
    const [xj, yj] = polygon[j];
    if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) {
      inside = !inside;
    }
  }
  return inside;
}

function pointInFeature(point: Coordinate, geometry: GeoJSON.Geometry): boolean {
  if (geometry.type === 'Polygon') {
    if (!pointInPolygon(point, geometry.coordinates[0] as number[][])) return false;
    // Exclude holes (inner rings)
    for (let i = 1; i < geometry.coordinates.length; i++) {
      if (pointInPolygon(point, geometry.coordinates[i] as number[][])) return false;
    }
    return true;
  }
  if (geometry.type === 'MultiPolygon') {
    return geometry.coordinates.some((poly) => {
      if (!pointInPolygon(point, poly[0] as number[][])) return false;
      for (let i = 1; i < poly.length; i++) {
        if (pointInPolygon(point, poly[i] as number[][])) return false;
      }
      return true;
    });
  }
  return false;
}

// --- Spatial index for fast overlay lookups ---

type ZoneIndexEntry = {
  minLng: number; minLat: number; maxLng: number; maxLat: number;
  score: number;
  feature: GeoJSON.Feature;
};

let _zoneIndex: ZoneIndexEntry[] = [];
let _zoneIndexKey = '';  // cache key to avoid rebuilding on same data

function buildZoneIndex(overlayGeoJSON: GeoJSON.FeatureCollection): ZoneIndexEntry[] {
  // Cache: skip rebuild if features haven't changed (include tag to avoid stale scores)
  const tag = overlayGeoJSON.features[0]?.properties?.tag ?? '';
  const key = `${tag}:${overlayGeoJSON.features.length}`;
  if (key === _zoneIndexKey && _zoneIndex.length > 0) return _zoneIndex;

  const entries: ZoneIndexEntry[] = [];
  for (const feature of overlayGeoJSON.features) {
    if (!feature.geometry || !feature.properties) continue;
    const score = feature.properties.display_score;
    if (score == null) continue;

    // Compute bounding box from geometry
    let minLng = Infinity, minLat = Infinity, maxLng = -Infinity, maxLat = -Infinity;
    const geom = feature.geometry;
    const rings: number[][][] =
      geom.type === 'Polygon' ? geom.coordinates as number[][][] :
      geom.type === 'MultiPolygon' ? (geom.coordinates as number[][][][]).flat() :
      [];
    for (const ring of rings) {
      for (const [lng, lat] of ring) {
        if (lng < minLng) minLng = lng;
        if (lat < minLat) minLat = lat;
        if (lng > maxLng) maxLng = lng;
        if (lat > maxLat) maxLat = lat;
      }
    }
    if (minLng === Infinity) continue;
    entries.push({ minLng, minLat, maxLng, maxLat, score: Number(score), feature });
  }

  _zoneIndex = entries;
  _zoneIndexKey = key;
  return entries;
}

function getScoreFromOverlay(
  buildingCenter: Coordinate,
  overlayGeoJSON: GeoJSON.FeatureCollection,
): number | null {
  const index = buildZoneIndex(overlayGeoJSON);
  const [lng, lat] = buildingCenter;

  // Phase 1: BBox filter (4 comparisons per zone — very fast)
  for (const entry of index) {
    if (lng < entry.minLng || lng > entry.maxLng || lat < entry.minLat || lat > entry.maxLat) {
      continue;
    }
    // Phase 2: Precise PIP only on bbox-matched candidates (typically 1-2)
    if (pointInFeature(buildingCenter, entry.feature.geometry!)) {
      return entry.score;
    }
  }
  return null;
}

// Deterministic micro-variation so buildings within one zone aren't all identical
function positionJitter(center: Coordinate): number {
  // Hash the coordinate into a repeatable value in [-4, +4]
  const raw = Math.sin(center[0] * 12345.6789 + center[1] * 98765.4321) * 43758.5453;
  return (raw - Math.floor(raw) - 0.5) * 8;
}

function renderVisibleBuildings(
  map: MapLibreMap,
  config: RenderConfig,
  target: LocalRenderTarget | null | undefined,
  overlayGeoJSON?: GeoJSON.FeatureCollection,
) {
  const fillSource = map.getSource('renderedBuildings') as GeoJSONSource | undefined;
  const extrusionSource = map.getSource('renderedBuildings3d') as GeoJSONSource | undefined;
  if (!fillSource || !extrusionSource) return;

  const features = map.querySourceFeatures('openmaptiles', { sourceLayer: 'building' });
  const seen = new Set<string>();
  const styledFeatures: GeoJSON.Feature[] = [];
  const hasOverlay = overlayGeoJSON != null && overlayGeoJSON.features.length > 0;

  // Pre-build spatial index once per overlay dataset (cached internally)
  if (hasOverlay) buildZoneIndex(overlayGeoJSON);

  for (const feature of features) {
    if (!feature.geometry || !isPolygonGeometry(feature)) continue;

    // Fast dedup + center: compute from outer ring only
    const outerRing = feature.geometry.type === 'Polygon'
        ? feature.geometry.coordinates[0]
        : feature.geometry.coordinates[0][0];
    const signature = `${outerRing[0][0]}:${outerRing[0][1]}:${outerRing.length}`;
    if (seen.has(signature)) continue;
    seen.add(signature);

    // Fast center from outer ring min/max (avoids visiting all rings)
    let cMinLng = Infinity, cMinLat = Infinity, cMaxLng = -Infinity, cMaxLat = -Infinity;
    for (const [lng, lat] of outerRing) {
      if (lng < cMinLng) cMinLng = lng;
      if (lat < cMinLat) cMinLat = lat;
      if (lng > cMaxLng) cMaxLng = lng;
      if (lat > cMaxLat) cMaxLat = lat;
    }
    const buildingCenter: Coordinate = [(cMinLng + cMaxLng) / 2, (cMinLat + cMaxLat) / 2];

    let colors = DEFAULT_BUILDING_STYLE;

    if (config.mode === 'local' && target) {
      // Local mode: IDW interpolation from nearby data points
      const localScore = getLocalBuildingScore(buildingCenter, config, target);
      colors = localScore === null
        ? DEFAULT_BUILDING_STYLE
        : getScoreColors(config.tag, localScore);
    } else if (hasOverlay) {
      // Global mode: derive color from hex overlay zone + position jitter
      const zoneScore = getScoreFromOverlay(buildingCenter, overlayGeoJSON);
      if (zoneScore !== null) {
        const jittered = Math.max(0, Math.min(100, zoneScore + positionJitter(buildingCenter)));
        colors = getScoreColors(config.tag, jittered);
      }
    } else {
      // Fallback: nearest global render point (sparse data)
      const nearestPoint = getNearestPoint(buildingCenter, config.points);
      colors = nearestPoint
        ? getScoreColors(config.tag, nearestPoint.score)
        : DEFAULT_BUILDING_STYLE;
    }

    styledFeatures.push({
      type: 'Feature',
      properties: {
        ...(feature.properties || {}),
        render_color: colors.fill,
        outline_color: colors.outline,
      },
      geometry: feature.geometry,
    });
  }

  const collection: GeoJSON.FeatureCollection = {
    type: 'FeatureCollection',
    features: styledFeatures,
  };

  fillSource.setData(collection);
  extrusionSource.setData(collection);
}

function getLocalBuildingScore(
  buildingCenter: Coordinate,
  config: RenderConfig,
  target: LocalRenderTarget,
): number | null {
  const analysisCenter: Coordinate = [target.center[1], target.center[0]];
  const maxDistanceKm = target.radiusM / 1000;
  const distanceFromSelection = distanceKm(buildingCenter, analysisCenter);

  if (distanceFromSelection > maxDistanceKm) {
    return null;
  }

  // Collect all data points within the analysis radius
  const nearbyPoints = config.points
    .map((point) => {
      const pointDistanceKm = distanceKm(buildingCenter, [point.longitude, point.latitude]);
      return { point, pointDistanceKm };
    })
    .filter(({ pointDistanceKm }) => pointDistanceKm <= maxDistanceKm);

  if (!nearbyPoints.length) {
    return null;
  }

  // Pure IDW (inverse-distance-weighted) interpolation
  // Closer data points have quadratically more influence
  let weightedScore = 0;
  let totalWeight = 0;
  for (const { point, pointDistanceKm } of nearbyPoints) {
    const weight = Math.pow(Math.max(0, 1 - pointDistanceKm / maxDistanceKm), 2);
    weightedScore += point.score * weight;
    totalWeight += weight;
  }

  if (totalWeight <= 0) {
    return null;
  }

  return Math.max(0, Math.min(100, Math.round(weightedScore / totalWeight)));
}

function renderHexOverlay(
  map: MapLibreMap,
  geojson: GeoJSON.FeatureCollection,
  overlayRef?: MutableRefObject<GeoJSON.FeatureCollection>,
) {
  const source = map.getSource('hexOverlay') as GeoJSONSource | undefined;
  if (!source) return;
  source.setData(geojson);
  if (overlayRef) overlayRef.current = geojson;
}

function setGlobalVisualizationMode(map: MapLibreMap, isGlobal: boolean) {
  // Buildings are always visible — in global mode they get colored from
  // global render points when zoomed in (>= 15), in local mode from
  // the selected point's data. Only hide the 3D layer when at overview zoom.
  if (map.getLayer('building')) {
    map.setLayoutProperty('building', 'visibility', 'visible');
  }
  if (map.getLayer('building-3d')) {
    map.setLayoutProperty('building-3d', 'visibility', 'visible');
  }
}

function updateHotspotOverlay(map: MapLibreMap, hotspots: HotspotData[]) {
  const source = map.getSource('hotspotOverlay') as GeoJSONSource | undefined;
  if (!source) return;
  if (!hotspots?.length) {
    source.setData(EMPTY_FEATURE_COLLECTION);
    return;
  }
  const features = hotspots.map((hs) => ({
    type: 'Feature' as const,
    properties: { incident_count: hs.incident_count, dominant_type: hs.dominant_type },
    geometry: createCirclePolygon([hs.center_lon, hs.center_lat], Math.max(hs.radius_m, 40) / 1000).geometry,
  }));
  source.setData({ type: 'FeatureCollection', features });
}

function updateRadiusOverlay(
  map: MapLibreMap,
  target: LocalRenderTarget | null | undefined,
  tag: RenderTag,
) {
  const radiusSource = map.getSource('renderRadius') as GeoJSONSource | undefined;
  if (!radiusSource) return;

  if (!target) {
    radiusSource.setData(EMPTY_FEATURE_COLLECTION);
    if (map.getLayer('render-radius-line')) {
      map.setPaintProperty('render-radius-line', 'line-color', '#2d2d2d');
    }
    return;
  }

  const circle = createCirclePolygon([target.center[1], target.center[0]], target.radiusM / 1000);
  radiusSource.setData({
    type: 'FeatureCollection',
    features: [
      {
        type: 'Feature',
        geometry: circle,
        properties: {},
      },
    ],
  });

  if (map.getLayer('render-radius-line')) {
    map.setPaintProperty('render-radius-line', 'line-color', '#1d4ed8');
  }
  if (map.getLayer('render-radius-fill')) {
    map.setPaintProperty('render-radius-fill', 'fill-color', '#3b82f6');
  }
}

function buildPostMarkerElement(config: RenderConfig, point: RenderPoint) {
  const palette = TAG_STYLES[config.tag];
  const fillColor = point.score >= 100 ? '#8f8a84' : getScoreColors(config.tag, point.score).fill;

  const wrapper = document.createElement('div');
  wrapper.style.cssText = `
    width: 22px;
    height: 22px;
    transform: translate(-50%, -50%);
  `;

  const pin = document.createElement('div');
  pin.style.cssText = `
    width: 22px;
    height: 22px;
    border-radius: 50% 50% 50% 0;
    transform: rotate(-45deg);
    background: ${fillColor};
    border: 2px solid #ffffff;
    box-shadow: 0 8px 16px rgba(0,0,0,0.22);
    position: relative;
  `;

  const core = document.createElement('div');
  core.style.cssText = `
    position: absolute;
    left: 5px;
    top: 5px;
    width: 8px;
    height: 8px;
    border-radius: 999px;
    background: ${palette.accent};
    border: 1px solid rgba(255,255,255,0.95);
  `;

  pin.appendChild(core);
  wrapper.appendChild(pin);
  const kindLabel = point.kind || palette.label;
  wrapper.title = point.summary || `${kindLabel} — score ${point.score}`;

  return wrapper;
}

function updatePostMarkers(
  map: MapLibreMap,
  markerStore: MutableRefObject<maplibregl.Marker[]>,
  config: RenderConfig,
) {
  markerStore.current.forEach((marker) => marker.remove());
  markerStore.current = [];

  if (config.mode !== 'local') {
    return;
  }

  config.points.forEach((point) => {
    const element = buildPostMarkerElement(config, point);
    const marker = new maplibregl.Marker({
      element,
      anchor: 'center',
    })
      .setLngLat([point.longitude, point.latitude])
      .addTo(map);

    markerStore.current.push(marker);
  });
}

function isPolygonGeometry(
  feature: MapGeoJSONFeature,
): feature is MapGeoJSONFeature & { geometry: GeoJSON.Polygon | GeoJSON.MultiPolygon } {
  return feature.geometry.type === 'Polygon' || feature.geometry.type === 'MultiPolygon';
}

function getGeometryCenter(geometry: GeoJSON.Polygon | GeoJSON.MultiPolygon): Coordinate {
  const bounds = {
    minLng: Infinity,
    minLat: Infinity,
    maxLng: -Infinity,
    maxLat: -Infinity,
  };

  visitCoordinates(geometry, (lng, lat) => {
    if (lng < bounds.minLng) bounds.minLng = lng;
    if (lat < bounds.minLat) bounds.minLat = lat;
    if (lng > bounds.maxLng) bounds.maxLng = lng;
    if (lat > bounds.maxLat) bounds.maxLat = lat;
  });

  return [(bounds.minLng + bounds.maxLng) / 2, (bounds.minLat + bounds.maxLat) / 2];
}

function visitCoordinates(
  geometry: GeoJSON.Polygon | GeoJSON.MultiPolygon,
  visitor: (lng: number, lat: number) => void,
) {
  if (geometry.type === 'Polygon') {
    geometry.coordinates.forEach((ring) => {
      ring.forEach(([lng, lat]) => visitor(lng, lat));
    });
    return;
  }

  geometry.coordinates.forEach((polygon) => {
    polygon.forEach((ring) => {
      ring.forEach(([lng, lat]) => visitor(lng, lat));
    });
  });
}

function getNearestPoint(center: Coordinate, points: RenderPoint[], maxDistKm = 1.5) {
  let best: (RenderPoint & { distanceKm: number }) | null = null;
  let bestDistance = Number.POSITIVE_INFINITY;

  for (const point of points) {
    const distance = distanceKm(center, [point.longitude, point.latitude]);
    if (distance < bestDistance) {
      bestDistance = distance;
      best = { ...point, distanceKm: distance };
    }
  }

  // Don't color buildings too far from any data point
  if (best && best.distanceKm > maxDistKm) return null;
  return best;
}

// Same 5-stop red→green gradient used by hex overlay and legend
const SCORE_GRADIENT = [
  { at: 0,   r: 215, g: 48,  b: 39  }, // #d73027
  { at: 25,  r: 252, g: 141, b: 89  }, // #fc8d59
  { at: 50,  r: 254, g: 224, b: 139 }, // #fee08b
  { at: 75,  r: 145, g: 207, b: 96  }, // #91cf60
  { at: 100, r: 26,  g: 152, b: 80  }, // #1a9850
];

// Pre-computed color LUT for scores 0-100 (avoids per-building gradient math)
const SCORE_COLOR_LUT: string[] = (() => {
  const lut: string[] = new Array(101);
  for (let s = 0; s <= 100; s++) {
    for (let i = 0; i < SCORE_GRADIENT.length - 1; i++) {
      const a = SCORE_GRADIENT[i], b = SCORE_GRADIENT[i + 1];
      if (s >= a.at && s <= b.at) {
        const t = (s - a.at) / (b.at - a.at);
        lut[s] = `rgb(${Math.round(a.r + (b.r - a.r) * t)}, ${Math.round(a.g + (b.g - a.g) * t)}, ${Math.round(a.b + (b.b - a.b) * t)})`;
        break;
      }
    }
    if (!lut[s]) {
      const last = SCORE_GRADIENT[SCORE_GRADIENT.length - 1];
      lut[s] = `rgb(${last.r}, ${last.g}, ${last.b})`;
    }
  }
  return lut;
})();

function scoreToRgb(score: number): string {
  return SCORE_COLOR_LUT[Math.max(0, Math.min(100, Math.round(score)))];
}

function getScoreColors(tag: RenderTag, score: number) {
  const palette = TAG_STYLES[tag];
  return {
    fill: scoreToRgb(score),
    outline: palette.accent,
  };
}



function distanceKm(a: Coordinate, b: Coordinate) {
  const cosLat = 0.7580107;
  const degToKm = 111.320;
  const dlat = (b[1] - a[1]) * degToKm;
  const dlng = (b[0] - a[0]) * degToKm * cosLat;
  return Math.sqrt(dlat * dlat + dlng * dlng);
}

function toRadians(value: number) {
  return (value * Math.PI) / 180;
}

function createCirclePolygon(center: Coordinate, radiusKm: number, segments = 64): GeoJSON.Polygon {
  const coordinates: Coordinate[] = [];

  for (let index = 0; index <= segments; index += 1) {
    const angle = (index / segments) * Math.PI * 2;
    coordinates.push(destinationPoint(center, radiusKm, angle));
  }

  return {
    type: 'Polygon',
    coordinates: [coordinates],
  };
}

function destinationPoint(center: Coordinate, distanceKmValue: number, bearingRad: number): Coordinate {
  const earthRadiusKm = 6371.0088;
  const lat1 = toRadians(center[1]);
  const lng1 = toRadians(center[0]);
  const angularDistance = distanceKmValue / earthRadiusKm;

  const lat2 = Math.asin(
    Math.sin(lat1) * Math.cos(angularDistance) +
      Math.cos(lat1) * Math.sin(angularDistance) * Math.cos(bearingRad),
  );

  const lng2 =
    lng1 +
    Math.atan2(
      Math.sin(bearingRad) * Math.sin(angularDistance) * Math.cos(lat1),
      Math.cos(angularDistance) - Math.sin(lat1) * Math.sin(lat2),
    );

  return [((lng2 * 180) / Math.PI), ((lat2 * 180) / Math.PI)];
}

export default function Map({
  center,
  zoom,
  renderTag = 'general',
  localRenderTarget = null,
  refreshKey = 0,
  markers = [],
  hotspots = [],
  onMarkerClick,
  onMapClick,
}: MapProps) {
  const mapContainer = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MapLibreMap | null>(null);
  const markersRef = useRef<maplibregl.Marker[]>([]);
  const postMarkersRef = useRef<maplibregl.Marker[]>([]);
  const activeConfigRef = useRef<RenderConfig>({ mode: 'global', tag: renderTag, points: [] });
  const localRenderTargetRef = useRef<LocalRenderTarget | null>(localRenderTarget);
  const hexOverlayRef = useRef<GeoJSON.FeatureCollection>(EMPTY_FEATURE_COLLECTION);

  const legendStyle = TAG_STYLES[renderTag];
  const activeRadiusM = localRenderTarget?.radiusM ?? 200;

  useEffect(() => {
    localRenderTargetRef.current = localRenderTarget;
  }, [localRenderTarget]);

  useEffect(() => {
    if (!mapContainer.current || mapRef.current) return;

    const map = new maplibregl.Map({
      container: mapContainer.current,
      style: MAP_STYLE,
      center: [center[1], center[0]],
      zoom,
      maxZoom: 20,
    });

    map.addControl(new maplibregl.NavigationControl(), 'top-left');
    map.on('click', (event) => {
      onMapClick(event.lngLat.lat, event.lngLat.lng);
    });

    map.on('load', async () => {
      const config = localRenderTarget
        ? await fetchLocalRenderConfig(renderTag, localRenderTarget)
        : await fetchGlobalRenderConfig(renderTag);

      activeConfigRef.current = config;
      updateRadiusOverlay(map, localRenderTarget, renderTag);
      updateHotspotOverlay(map, hotspots);
      updatePostMarkers(map, postMarkersRef, config);
      if (config.mode === 'global') {
        const hexGeoJSON = await fetchHexOverlayGeoJSON(renderTag);
        renderHexOverlay(map, hexGeoJSON, hexOverlayRef);
        setGlobalVisualizationMode(map, true);
        renderVisibleBuildings(map, config, null, hexOverlayRef.current);
      } else {
        renderHexOverlay(map, EMPTY_FEATURE_COLLECTION, hexOverlayRef);
        setGlobalVisualizationMode(map, false);
        renderVisibleBuildings(map, config, localRenderTargetRef.current);
      }
    });

    // NTA zone hover popup
    const zonePopup = new maplibregl.Popup({
      closeButton: false,
      closeOnClick: false,
      offset: 12,
    });

    map.on('mouseenter', 'hex-overlay-fill', () => {
      map.getCanvas().style.cursor = 'pointer';
    });

    map.on('mouseleave', 'hex-overlay-fill', () => {
      map.getCanvas().style.cursor = '';
      zonePopup.remove();
    });

    map.on('mousemove', 'hex-overlay-fill', (e) => {
      const feat = e.features?.[0];
      if (!feat?.properties) return;
      const props = feat.properties;
      const name = props.nta_name;
      // Only show popup for NTA zones (which have nta_name), not H3 hexes
      if (!name) {
        zonePopup.remove();
        return;
      }
      const borough = props.borough || '';
      const risk = props.risk_level || '';
      const currentTag = activeConfigRef.current.tag;

      const riskColor = risk === 'low' ? '#16a34a' : risk === 'high' ? '#dc2626' : '#ca8a04';
      const riskLabel = risk === 'low' ? 'Low Risk' : risk === 'high' ? 'High Risk' : 'Moderate';

      // Build score rows based on active category
      let scoreRows: string;
      if (currentTag === 'safety') {
        const val = props.safety_score ?? '—';
        scoreRows =
          `<div style="color:#555">Safety Score</div><div style="color:#1a1a1a;font-weight:700;font-size:16px">${val}</div>`;
      } else if (currentTag === 'transit') {
        const val = props.transit_score ?? '—';
        scoreRows =
          `<div style="color:#555">Transit Score</div><div style="color:#1a1a1a;font-weight:700;font-size:16px">${val}</div>`;
      } else if (currentTag === 'amenities') {
        const val = props.amenities_score ?? '—';
        scoreRows =
          `<div style="color:#555">Amenities Score</div><div style="color:#1a1a1a;font-weight:700;font-size:16px">${val}</div>`;
      } else {
        // General / default: show all scores
        const overall = props.overall_score ?? '—';
        const safety = props.safety_score ?? '—';
        const transit = props.transit_score ?? '—';
        const amenities = props.amenities_score ?? '—';
        scoreRows =
          `<div style="color:#555">Overall</div><div style="color:#1a1a1a;font-weight:700">${overall}</div>` +
          `<div style="color:#555">Safety</div><div style="color:#1a1a1a">${safety}</div>` +
          `<div style="color:#555">Transit</div><div style="color:#1a1a1a">${transit}</div>` +
          `<div style="color:#555">Amenities</div><div style="color:#1a1a1a">${amenities}</div>`;
      }

      zonePopup
        .setLngLat(e.lngLat)
        .setHTML(
          `<div style="font:700 14px/1.3 system-ui;color:#1a1a1a;margin-bottom:2px">${name}</div>` +
          `<div style="font:400 11px/1.4 system-ui;color:#666;margin-bottom:6px">${borough} <span style="color:${riskColor};font-weight:600">${riskLabel}</span></div>` +
          `<div style="display:grid;grid-template-columns:1fr 1fr;gap:3px 12px;font:500 11px/1.5 system-ui">` +
            scoreRows +
          `</div>`
        )
        .addTo(map);
    });

    // Re-render buildings after map movement (flyTo, pan, zoom)
    map.on('moveend', () => {
      if (!map.isStyleLoaded()) return;
      const config = activeConfigRef.current;
      if (config.mode === 'local') {
        renderVisibleBuildings(map, config, localRenderTargetRef.current);
      } else {
        renderVisibleBuildings(map, config, null, hexOverlayRef.current);
      }
    });

    // Re-render buildings when new vector tiles load (covers flyTo race condition)
    let sourceDataTimer: ReturnType<typeof setTimeout> | null = null;
    map.on('sourcedata', (e) => {
      if (e.sourceId !== 'openmaptiles' || !e.isSourceLoaded) return;
      const config = activeConfigRef.current;
      // Debounce — tiles load in batches (350ms avoids redundant renders)
      if (sourceDataTimer) clearTimeout(sourceDataTimer);
      sourceDataTimer = setTimeout(() => {
        if (map.isStyleLoaded()) {
          if (config.mode === 'local') {
            renderVisibleBuildings(map, config, localRenderTargetRef.current);
          } else {
            renderVisibleBuildings(map, config, null, hexOverlayRef.current);
          }
        }
      }, 350);
    });

    mapRef.current = map;

    return () => {
      map.remove();
      mapRef.current = null;
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!mapRef.current) return;
    mapRef.current.flyTo({
      center: [center[1], center[0]],
      zoom,
      duration: 800,
    });
  }, [center, zoom]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;

    let cancelled = false;

    const refreshRender = async () => {
      // Wait for style to be loaded (handles flyTo race condition)
      if (!map.isStyleLoaded()) {
        await new Promise<void>((resolve) => {
          const onIdle = () => { map.off('idle', onIdle); resolve(); };
          map.on('idle', onIdle);
        });
      }
      if (cancelled || !mapRef.current) return;

      const config = localRenderTarget
        ? await fetchLocalRenderConfig(renderTag, localRenderTarget)
        : await fetchGlobalRenderConfig(renderTag);

      if (cancelled || !mapRef.current) return;

      activeConfigRef.current = config;
      updateRadiusOverlay(map, localRenderTarget, renderTag);
      updateHotspotOverlay(map, hotspots);
      updatePostMarkers(map, postMarkersRef, config);
      if (config.mode === 'global') {
        const hexGeoJSON = await fetchHexOverlayGeoJSON(renderTag);
        if (!cancelled && mapRef.current) {
          renderHexOverlay(map, hexGeoJSON, hexOverlayRef);
          setGlobalVisualizationMode(map, true);
          renderVisibleBuildings(map, config, null, hexOverlayRef.current);
        }
      } else {
        renderHexOverlay(map, EMPTY_FEATURE_COLLECTION, hexOverlayRef);
        setGlobalVisualizationMode(map, false);
        renderVisibleBuildings(map, config, localRenderTarget ?? undefined);
      }
    };

    refreshRender();

    return () => {
      cancelled = true;
    };
  }, [
    renderTag,
    localRenderTarget ? `${localRenderTarget.center[0]},${localRenderTarget.center[1]},${localRenderTarget.radiusM}` : 'global',
    refreshKey,
    hotspots,
  ]);

  useEffect(() => {
    markersRef.current.forEach((marker) => marker.remove());
    markersRef.current = [];

    if (!mapRef.current) return;

    markers.forEach((location) => {
      const element = document.createElement('div');
      element.style.cssText = `
        width: 38px;
        height: 38px;
        background: #1a1a1a;
        border: 4px solid #fff;
        border-radius: 50% 50% 50% 0;
        transform: rotate(-45deg);
        box-shadow: 0 10px 28px rgba(0,0,0,0.3);
        cursor: pointer;
      `;

      const marker = new maplibregl.Marker({ element })
        .setLngLat([location.position[1], location.position[0]])
        .addTo(mapRef.current);

      element.addEventListener('click', (event) => {
        event.stopPropagation();
        onMarkerClick(location);
      });

      markersRef.current.push(marker);
    });
  }, [markers, onMarkerClick]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      mapRef.current?.resize();
    }, 500);

    return () => window.clearTimeout(timer);
  });

  useEffect(() => {
    return () => {
      postMarkersRef.current.forEach((marker) => marker.remove());
      postMarkersRef.current = [];
    };
  }, []);

  return (
    <div className="w-full h-full relative z-0">
      <div
        ref={mapContainer}
        className="w-full h-full [&_.maplibregl-ctrl-group_button]:h-12 [&_.maplibregl-ctrl-group_button]:w-12 [&_.maplibregl-ctrl-group]:rounded-xl [&_.maplibregl-ctrl-group]:shadow-lg [&_.maplibregl-ctrl-group]:overflow-hidden"
      />
      <div className="pointer-events-none absolute top-4 right-4 z-20">
        <div className="bg-background/95 backdrop-blur-md border border-border rounded-xl shadow-lg px-4 py-2.5">
          <div className="text-[10px] font-semibold text-muted-foreground uppercase tracking-wider text-center mb-1.5">{legendStyle.label} Score</div>
          <div className="flex items-center gap-2">
            <span className="text-xs font-medium text-muted-foreground leading-none w-4 text-right">0</span>
            <div
              className="h-3 w-36 rounded-full"
              style={{ background: 'linear-gradient(90deg, #d73027, #fc8d59, #fee08b, #91cf60, #1a9850)' }}
            />
            <span className="text-xs font-medium text-muted-foreground leading-none w-6">100</span>
          </div>
        </div>
      </div>
    </div>
  );
}
