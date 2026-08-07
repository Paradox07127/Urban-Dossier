import { useEffect, useRef, useState, type MutableRefObject } from 'react';
import maplibregl, { GeoJSONSource, Map as MapLibreMap } from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';
import { Box } from 'lucide-react';
import { Location } from '../types';

type RenderTag = 'general' | 'safety' | 'transit' | 'amenities';
type Coordinate = [number, number];
type RadiusMeters = 200 | 500 | 1000;

// Design tokens duplicated from index.css (--ud-low/--ud-mid/--ud-high/--ud-ink).
// MapLibre paint expressions run on the GPU and cannot read CSS custom
// properties, so the values have to be literals here. Change them together.
const UD_INK = '#0E1218';

/* The score ramp.
 *
 * The midpoint used to be a mid-grey (#96928A), chosen when chroma appeared
 * only in scattered patches. Once the overlay tiled the whole city that choice
 * failed badly: 93% of buildings score between 35 and 65, so a grey midpoint
 * meant a grey city under a heavy wash, and the map read as though something
 * dark had been laid over it.
 *
 * The midpoint is now a warm near-paper tone. It keeps the same rule -- chroma
 * is reserved for measured values -- but applies it more strictly: an
 * unremarkable score should recede into the page and let the genuinely high
 * and low places carry the only colour on the map. Both ends stay light enough
 * to tint the basemap rather than bury it.
 */
const UD_LOW = '#B3382C';
const UD_MID = '#E8DFD0';
const UD_HIGH = '#2F8C63';
// Buildings the pipeline could not score. A cool grey, deliberately off the
// ramp's warm axis: "no reading here" must not be mistakeable for "scored
// mid". Roughly a quarter of footprints sit in r9 cells with no dataset
// coverage, so this is a common state rather than an edge case.
const UD_NO_DATA = '#C6CACE';

/** 2nd/50th/98th percentile per field, as measured by the scoring pass. */
type ColourDomain = { low: number; mid: number; high: number };
const FULL_RANGE_DOMAIN: ColourDomain = { low: 0, mid: 50, high: 100 };

/* ---------------------------------------------------------------------------
   Sandbox view
   ---------------------------------------------------------------------------
   The city as a physical model: a slab of land with the buildings standing on
   it, turnable by dragging, and nothing else on screen.

   The slab is built by extruding the land polygon upward rather than cutting
   the ground downward, because fill-extrusion-base cannot go below zero. The
   buildings then start at the slab's top face. Visually identical to a carved
   block, and it stays inside what the renderer actually supports.
--------------------------------------------------------------------------- */
// A plate, not a plinth. Thick enough to read as a solid object cut from
// something -- about five pixels of edge when the whole city is on screen --
// and no thicker, because every metre of it is also the distance by which a
// ground-level overlay would sit below the surface the buildings stand on.
const SLAB_TOP_M = 150;
const SLAB_SIDE = '#C9C3B8';
const SLAB_TOP = '#E4DFD5';
const VOID_COLOUR = '#DCE3E8';
// How far a dropped pin stands above the model surface.
const PIN_HEIGHT_M = 260;
const PIN_RADIUS_KM = 0.018;

/* Vertical exaggeration.
 *
 * NYC is 40 km across. At the zoom where the whole city fits, one pixel is
 * about 27 m, so a 100 m tower is under four pixels and the skyline is
 * invisible -- a true-scale model of a city this wide reads as a flat sheet.
 * Heights are stretched at city scale and relax to true scale by z15, where a
 * building occupies enough screen for its real proportions to be readable.
 *
 * This distorts, so the view labels the current factor rather than leaving the
 * reader to assume the towers are to scale.
 */
const EXAGGERATION_STOPS: [number, number][] = [[10, 6], [13, 3], [15, 1]];

function exaggerationAtZoom(zoom: number): number {
  const stops = EXAGGERATION_STOPS;
  if (zoom <= stops[0][0]) return stops[0][1];
  if (zoom >= stops[stops.length - 1][0]) return 1;
  for (let i = 0; i < stops.length - 1; i++) {
    const [z0, v0] = stops[i];
    const [z1, v1] = stops[i + 1];
    if (zoom >= z0 && zoom <= z1) {
      return v0 + ((v1 - v0) * (zoom - z0)) / (z1 - z0);
    }
  }
  return 1;
}

/** Building prism, standing on the slab.
 *
 * The zoom interpolation has to be the outermost expression and take ['zoom']
 * directly -- MapLibre allows zoom only as the input of a top-level step or
 * interpolate, and rejects the entire style otherwise, leaving a blank map
 * rather than a degraded layer. So the exaggeration cannot multiply a feature
 * value from inside; instead each zoom stop carries its own fully-formed
 * height expression.
 */
function extrusionHeight(): maplibregl.ExpressionSpecification {
  const atStop = (factor: number): maplibregl.ExpressionSpecification => [
    '+', SLAB_TOP_M, ['*', ['coalesce', ['get', 'height'], 8], factor],
  ];
  return [
    'interpolate', ['linear'], ['zoom'],
    ...EXAGGERATION_STOPS.flatMap(([zoom, factor]) => [zoom, atStop(factor)]),
  ] as maplibregl.ExpressionSpecification;
}

/** Which tile property backs each map tag. */
const TAG_TO_SCORE_FIELD: Record<RenderTag, string> = {
  general: 'overall',
  safety: 'safety',
  transit: 'transit',
  amenities: 'amenities',
};

/**
 * Paint expression colouring a building by one of its baked score fields.
 *
 * Evaluated per feature on the GPU, so the whole choropleth costs one uniform
 * upload rather than a pass over every building in JavaScript.
 */
function buildingScoreColor(
  field: string,
  domain: ColourDomain = FULL_RANGE_DOMAIN,
): maplibregl.ExpressionSpecification {
  return [
    'case',
    ['==', ['get', field], null],
    UD_NO_DATA,
    [
      'interpolate',
      ['linear'],
      ['to-number', ['get', field]],
      domain.low, UD_LOW,
      domain.mid, UD_MID,
      domain.high, UD_HIGH,
    ],
  ];
}

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
  /** GeoJSON Feature returned by POST /api/isochrone, or null to clear. */
  isochrone?: GeoJSON.Feature | null;
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

// The legend has to describe the ramp the map actually draws. These used to be
// a different hue per tag -- magenta for safety, blue for transit -- which said
// that a low safety score and a low transit score were different *kinds* of
// thing rather than the same 0-100 reading of different data. They are the same
// scale, so they get the same ramp, and the tag is named in the label instead.
const TAG_STYLES: Record<RenderTag, RenderPalette> = {
  general: { low: UD_LOW, high: UD_HIGH, accent: UD_INK, label: 'General' },
  safety: { low: UD_LOW, high: UD_HIGH, accent: UD_INK, label: 'Safety' },
  transit: { low: UD_LOW, high: UD_HIGH, accent: UD_INK, label: 'Transit' },
  amenities: { low: UD_LOW, high: UD_HIGH, accent: UD_INK, label: 'Amenities' },
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
      attribution:
        '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap contributors</a> ' +
        '&copy; <a href="https://openmaptiles.org/">OpenMapTiles</a>',
    },
    // Per-building scores baked in at build time by
    // backend/scripts/build_building_tiles.py. Every feature carries the four
    // category scores plus the overall, so switching the active tag is a paint
    // expression change rather than a refetch and a rebuild of a GeoJSON
    // source. Served from a second tileset that may legitimately be absent;
    // the layers below simply draw nothing when it is.
    buildingScores: {
      type: 'vector',
      tiles: [`${window.location.origin}/building-tiles/{z}/{x}/{y}.pbf`],
      minzoom: 10,
      maxzoom: 16,
    },
    // The slab the sandbox stands on. Filled from /api/land-outline, which is
    // the same coastline the overview cells are clipped against, so the base
    // and the choropleth end at one line rather than two that nearly agree.
    landOutline: {
      type: 'geojson',
      data: EMPTY_FEATURE_COLLECTION,
    },
    // Pins that live inside the 3D scene.
    //
    // A maplibregl.Marker is a DOM element positioned by map.project(), which
    // takes a lng/lat and no elevation -- there is no supported way to project
    // a point that sits on top of an extrusion. So in the sandbox a marker
    // lands on the slab's underside while the buildings stand on its top, and
    // no amount of pixel offsetting fixes it, because the error depends on
    // pitch, bearing and zoom together. Drawing the pin as extruded geometry
    // based at the slab surface makes it part of the model instead, and it is
    // then correct from every angle by construction.
    sandboxPins: {
      type: 'geojson',
      data: EMPTY_FEATURE_COLLECTION,
    },
    // A sheet covering the world with New York City punched out of it, so the
    // flat map stops where the data does. Everything outside the five boroughs
    // -- New Jersey, Nassau, Westchester -- has no NYC Open Data behind it, and
    // drawing its streets in the same ink as the scored city invites reading
    // the blank as "nothing happening here" rather than "not measured".
    cityMask: {
      type: 'geojson',
      data: EMPTY_FEATURE_COLLECTION,
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
    isochroneOverlay: {
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
          0, UD_LOW,
          50, UD_MID,
          100, UD_HIGH,
        ],
        // Adjacent H3 cells share an edge exactly. MapLibre's default fill
        // antialiasing draws a feathered outline on each of them, and the two
        // half-transparent edges do not sum back to an opaque one -- the seam
        // shows as a hairline of basemap between neighbours. The grid is
        // contiguous (measured: 846 of 1171 cells have all six neighbours
        // present, 11 genuine holes), so any visible gap is this artefact.
        'fill-antialias': false,
        // Lowered from 0.55. That value was set when the cells were drawn at
        // half size and covered about a quarter of the map; at full coverage
        // the same alpha is a wash over the entire city that hides the street
        // network the scores are supposed to be read against. This tints the
        // basemap instead of replacing it, and clears out entirely once
        // buildings carry the colour.
        'fill-opacity': ['interpolate', ['linear'], ['zoom'], 11, 0.42, 14.5, 0],
      },
    },
    {
      id: 'hex-overlay-line',
      type: 'line',
      source: 'hexOverlay',
      paint: {
        // Off by default for H3.
        //
        // A stroke on a contiguous grid draws the grid, and the grid is an
        // artefact of how the analysis is bucketed rather than anything in the
        // city -- outlining it invites reading the hexagon as a place. White
        // made it worse (it read as a gap); ink still made every seam a line.
        // NTA zones share this layer and are real boundaries, so the layer
        // stays and its opacity is raised only for features that name one.
        'line-color': UD_INK,
        'line-width': 0.6,
        // ``zoom`` is only legal as the direct input of a top-level step or
        // interpolate, so the per-feature test has to sit in the output
        // values rather than wrap the interpolation. Nesting it the other way
        // round is not a warning -- MapLibre rejects the whole style and the
        // map renders nothing at all.
        'line-opacity': [
          'interpolate',
          ['linear'],
          ['zoom'],
          11, ['case', ['has', 'nta_name'], 0.22, 0],
          14.5, 0,
        ],
      },
    },
    // Per-building choropleth, coloured straight from the tileset.
    //
    // This replaces the JS path below for the global view. The old approach
    // pulled every building out of the basemap on each camera move, computed a
    // centroid, ran a point-in-polygon against the overlay and rebuilt a
    // GeoJSON source -- all on the main thread, every moveend. Here the score
    // is already a feature property, so MapLibre resolves the colour on the
    // GPU and JavaScript does nothing at all.
    //
    // 'overall' is a placeholder: setBuildingScoreTag() rewrites the property
    // this reads when the active tag changes, which is why every category
    // travels in the tile rather than only the selected one.
    {
      id: 'building-scores-fill',
      type: 'fill',
      source: 'buildingScores',
      'source-layer': 'building_scores',
      minzoom: 13,
      paint: {
        'fill-color': buildingScoreColor('overall'),
        // Not fully opaque even when zoomed in: the basemap's own building
        // shading carries roof/height detail that a flat fill would erase, and
        // letting a little of it through keeps the blocks legible as buildings
        // rather than as coloured rectangles.
        'fill-opacity': ['interpolate', ['linear'], ['zoom'], 13, 0.3, 15.5, 0.82],
        // Buildings abut across tile boundaries; the feathered outline that
        // antialiasing adds shows up as a seam there for the same reason it did
        // between hexagons.
        'fill-antialias': false,
      },
    },
    /* ----------------------------------------------------------------- 3D --
       All hidden until sandbox mode is switched on. They sit here rather than
       being added at runtime so the style is validated once at load: MapLibre
       rejects an entire style for one malformed expression, and finding that
       out on a mode toggle is worse than finding it out on boot.
    -------------------------------------------------------------------- */
    {
      id: 'land-slab',
      type: 'fill-extrusion',
      source: 'landOutline',
      layout: { visibility: 'none' },
      paint: {
        'fill-extrusion-color': [
          // Only the top face carries the city; the sides are the cut edge of
          // the block and are shaded darker so the slab reads as solid.
          'interpolate', ['linear'], ['zoom'], 0, SLAB_SIDE, 22, SLAB_SIDE,
        ],
        'fill-extrusion-height': SLAB_TOP_M,
        'fill-extrusion-base': 0,
        'fill-extrusion-opacity': 1,
        'fill-extrusion-vertical-gradient': true,
      },
    },
    {
      // A flat cap on the slab, so the ground the buildings stand on is a
      // lighter surface than the block's cut sides.
      id: 'land-slab-top',
      type: 'fill-extrusion',
      source: 'landOutline',
      layout: { visibility: 'none' },
      paint: {
        'fill-extrusion-color': SLAB_TOP,
        'fill-extrusion-height': SLAB_TOP_M + 1,
        'fill-extrusion-base': SLAB_TOP_M,
        'fill-extrusion-opacity': 1,
      },
    },
    {
      // City scale: the massing layer, only buildings over 25 m.
      id: 'sandbox-massing',
      type: 'fill-extrusion',
      source: 'buildingScores',
      'source-layer': 'building_massing',
      layout: { visibility: 'none' },
      maxzoom: 13,
      paint: {
        'fill-extrusion-color': buildingScoreColor('overall'),
        'fill-extrusion-height': extrusionHeight(),
        'fill-extrusion-base': SLAB_TOP_M,
        'fill-extrusion-opacity': 1,
        'fill-extrusion-vertical-gradient': true,
      },
    },
    {
      // Street scale: every building.
      id: 'sandbox-buildings',
      type: 'fill-extrusion',
      source: 'buildingScores',
      'source-layer': 'building_scores',
      layout: { visibility: 'none' },
      minzoom: 13,
      paint: {
        'fill-extrusion-color': buildingScoreColor('overall'),
        'fill-extrusion-height': extrusionHeight(),
        'fill-extrusion-base': SLAB_TOP_M,
        'fill-extrusion-opacity': 1,
        'fill-extrusion-vertical-gradient': true,
      },
    },
    {
      // The analysis radius, drawn as a shallow disc lying on the model
      // surface rather than the flat circle used on the map, which would cut
      // through the slab at ground level.
      id: 'sandbox-radius',
      type: 'fill-extrusion',
      source: 'renderRadius',
      layout: { visibility: 'none' },
      paint: {
        'fill-extrusion-color': UD_INK,
        'fill-extrusion-base': SLAB_TOP_M,
        'fill-extrusion-height': SLAB_TOP_M + 4,
        'fill-extrusion-opacity': 0.13,
      },
    },
    {
      id: 'sandbox-pin',
      type: 'fill-extrusion',
      source: 'sandboxPins',
      layout: { visibility: 'none' },
      paint: {
        'fill-extrusion-color': UD_INK,
        'fill-extrusion-base': SLAB_TOP_M,
        'fill-extrusion-height': SLAB_TOP_M + PIN_HEIGHT_M,
        'fill-extrusion-opacity': 0.95,
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
    /* Walking isochrone. Drawn in ink rather than colour: it is a boundary of
       reach, not a measured value, and colour is reserved for measurements. */
    {
      id: 'isochrone-fill',
      type: 'fill',
      source: 'isochroneOverlay',
      paint: {
        'fill-color': '#0E1218',
        'fill-opacity': 0.07,
      },
    },
    {
      id: 'isochrone-line',
      type: 'line',
      source: 'isochroneOverlay',
      paint: {
        'line-color': '#0E1218',
        'line-width': 1.75,
        'line-opacity': 0.85,
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
        'line-width': ['interpolate', ['linear'], ['zoom'], 5, 0.5, 14, 6, 18, 12],
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
        'line-width': ['interpolate', ['linear'], ['zoom'], 5, 0.3, 14, 4, 18, 10],
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
        'line-width': ['interpolate', ['linear'], ['zoom'], 8, 0.3, 14, 3, 18, 8],
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
        'line-width': ['interpolate', ['linear'], ['zoom'], 12, 0.5, 14, 2, 18, 6],
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
        'text-size': ['interpolate', ['linear'], ['zoom'], 14, 10, 18, 14],
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
        'text-size': ['interpolate', ['linear'], ['zoom'], 3, 10, 14, 16],
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
        'text-size': ['interpolate', ['linear'], ['zoom'], 5, 14, 14, 24],
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
    /* Everything beyond the five boroughs, veiled.
       Last in the list so it covers the roads and labels below it. Not opaque:
       the surrounding street network is useful for orientation -- you should
       still be able to tell that the blank across the Hudson is Jersey City --
       but it must not read as part of the analysis. */
    {
      id: 'city-mask',
      type: 'fill',
      source: 'cityMask',
      paint: {
        'fill-color': '#F1EEE9',
        'fill-opacity': 0.82,
        'fill-antialias': false,
      },
    },
    {
      id: 'city-mask-edge',
      type: 'line',
      source: 'cityMask',
      paint: {
        'line-color': UD_INK,
        'line-width': 0.8,
        'line-opacity': 0.18,
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

/**
 * Is the baked per-building tileset being served?
 *
 * Answered false on any error: the JS renderer is the safe fallback, so an
 * unreachable status endpoint must not leave the map with no buildings at all.
 */
async function fetchBuildingTileStatus(): Promise<{
  available: boolean;
  domains: Record<string, ColourDomain>;
}> {
  try {
    const resp = await fetch('/api/building-tiles/status');
    if (!resp.ok) return { available: false, domains: {} };
    const data = await resp.json();
    const raw = data?.colour_domains;
    const domains: Record<string, ColourDomain> = {};
    if (raw && typeof raw === 'object') {
      for (const [field, d] of Object.entries(raw as Record<string, unknown>)) {
        const v = d as Partial<ColourDomain> | null;
        // Reject anything non-monotonic: a bad domain would make the
        // interpolation throw and take the whole style down, and falling back
        // to the full range is merely dull rather than broken.
        if (
          v && typeof v.low === 'number' && typeof v.mid === 'number' &&
          typeof v.high === 'number' && v.low < v.mid && v.mid < v.high
        ) {
          domains[field] = { low: v.low, mid: v.mid, high: v.high };
        }
      }
    }
    return { available: data?.available === true, domains };
  } catch {
    return { available: false, domains: {} };
  }
}

/**
 * Turn the land outline into a sheet with the city punched out of it.
 *
 * A GeoJSON polygon's first ring is its outline and the rest are holes, so the
 * mask is one big rectangle whose holes are the boroughs. Winding order is not
 * enforced by MapLibre's renderer, which fills by even-odd, so the rings are
 * passed through as they come.
 */
function buildCityMask(outline: GeoJSON.Feature): GeoJSON.Feature | null {
  const geom = outline.geometry;
  const holes: GeoJSON.Position[][] = [];
  if (geom.type === 'Polygon') {
    holes.push(geom.coordinates[0]);
  } else if (geom.type === 'MultiPolygon') {
    for (const poly of geom.coordinates) holes.push(poly[0]);
  } else {
    return null;
  }
  // Comfortably past any viewport the app can reach, but short of the poles,
  // where the Mercator projection sends the corners to infinity.
  const world: GeoJSON.Position[] = [
    [-180, -85], [180, -85], [180, 85], [-180, 85], [-180, -85],
  ];
  return {
    type: 'Feature',
    properties: {},
    geometry: { type: 'Polygon', coordinates: [world, ...holes] },
  };
}

/** The city's landmass, for the sandbox slab. Null when unavailable. */
async function fetchLandOutline(): Promise<GeoJSON.Feature | null> {
  try {
    const resp = await fetch('/api/land-outline');
    if (!resp.ok) return null;
    const data = await resp.json();
    return data?.type === 'Feature' && data.geometry ? data : null;
  } catch {
    return null;
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
    const geometry = feature.geometry;
    if (!geometry || (geometry.type !== 'Polygon' && geometry.type !== 'MultiPolygon')) continue;

    // Fast dedup + center: compute from outer ring only
    const outerRing = geometry.type === 'Polygon'
        ? geometry.coordinates[0]
        : geometry.coordinates[0][0];
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
      geometry,
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

/**
 * Point the baked-tile choropleth at a different category.
 *
 * Every category rides along in the tile, so switching tags is a paint
 * property write -- no refetch, no re-tessellation, no JS pass over the
 * buildings. That is the payoff for baking all five scores instead of only the
 * active one.
 */
function setBuildingScoreTag(
  map: MapLibreMap,
  tag: RenderTag,
  domains: Record<string, ColourDomain> = {},
) {
  if (!map.getLayer('building-scores-fill')) return;
  const field = TAG_TO_SCORE_FIELD[tag] ?? 'overall';
  const domain = domains[field] ?? FULL_RANGE_DOMAIN;
  map.setPaintProperty(
    'building-scores-fill',
    'fill-color',
    buildingScoreColor(field, domain),
  );
}

/**
 * Hand the global choropleth to whichever renderer is actually available.
 *
 * With the baked tileset present the JS path is not merely redundant, it is
 * harmful: it would draw its own differently-derived colours on top. So the
 * two are mutually exclusive rather than layered.
 */
function setBuildingRenderer(map: MapLibreMap, baked: boolean, isGlobal: boolean) {
  const useBaked = baked && isGlobal;
  if (map.getLayer('building-scores-fill')) {
    map.setLayoutProperty(
      'building-scores-fill',
      'visibility',
      useBaked ? 'visible' : 'none',
    );
  }
  if (map.getLayer('building')) {
    map.setLayoutProperty('building', 'visibility', useBaked ? 'none' : 'visible');
  }
}

/* Layers that make up the flat map. In sandbox mode the city is a physical
 * object on an empty ground, so every one of these goes away -- leaving them
 * would put a road network and a coloured choropleth on the table next to the
 * model. Water is included: the slab's edge already is the shoreline, and a
 * blue sheet at ground level would sit 700 m below the land it borders. */
const FLAT_MAP_LAYERS = [
  'water', 'waterway', 'landcover', 'landuse', 'park-fill',
  'hex-overlay-fill', 'hex-overlay-line',
  'building-scores-fill', 'building', 'building-3d',
  'isochrone-fill', 'isochrone-line',
  // Ground-level overlays. Left on, these lie on the slab's underside and cut
  // through the model at the waterline.
  'render-radius-fill', 'render-radius-line', 'render-radius-line-glow',
  'hotspot-fill', 'hotspot-line',
  // Nothing left to veil once the basemap is gone; the slab's edge is the
  // boundary.
  'city-mask', 'city-mask-edge',
];

const SANDBOX_LAYERS = [
  'land-slab', 'land-slab-top', 'sandbox-massing', 'sandbox-buildings',
  'sandbox-radius', 'sandbox-pin',
];

/**
 * Switch between the flat map and the sandbox.
 *
 * Camera limits are the renderer's own: pitch is clamped to 0-85 degrees by
 * MapLibre, so the model physically cannot be turned upside down, and bearing
 * is free through 360. Nothing here has to enforce that.
 */
/**
 * Hide or restore the HTML markers.
 *
 * They are absolutely-positioned DOM nodes sitting above the canvas, so in the
 * sandbox they neither respect the model's surface nor occlude behind its
 * towers -- they float over the scene at the wrong height. The in-scene pin
 * replaces the one that matters; the rest are map furniture that has no place
 * on a physical model anyway.
 */
function setDomMarkersVisible(
  refs: MutableRefObject<maplibregl.Marker[]>[], visible: boolean,
) {
  for (const ref of refs) {
    for (const marker of ref.current) {
      marker.getElement().style.display = visible ? '' : 'none';
    }
  }
}

/** Put a pin into the model at the selected point. */
function updateSandboxPin(map: MapLibreMap, target: LocalRenderTarget | null | undefined) {
  const source = map.getSource('sandboxPins') as GeoJSONSource | undefined;
  if (!source) return;
  if (!target) {
    source.setData(EMPTY_FEATURE_COLLECTION);
    return;
  }
  source.setData({
    type: 'FeatureCollection',
    features: [{
      type: 'Feature',
      properties: {},
      geometry: createCirclePolygon(
        [target.center[1], target.center[0]], PIN_RADIUS_KM, 18,
      ),
    }],
  });
}

function setSandboxMode(map: MapLibreMap, on: boolean, tag: RenderTag,
                        domains: Record<string, ColourDomain>) {
  for (const id of FLAT_MAP_LAYERS) {
    if (map.getLayer(id)) {
      map.setLayoutProperty(id, 'visibility', on ? 'none' : 'visible');
    }
  }
  for (const id of SANDBOX_LAYERS) {
    if (map.getLayer(id)) {
      map.setLayoutProperty(id, 'visibility', on ? 'visible' : 'none');
    }
  }
  if (map.getLayer('background')) {
    map.setPaintProperty(
      'background', 'background-color', on ? VOID_COLOUR : '#f0ede9',
    );
  }
  // Road and label layers come from the basemap and are matched by prefix
  // rather than listed, since the style grows them over time.
  for (const layer of map.getStyle().layers ?? []) {
    if (/^(road|bridge|tunnel|place|poi|boundary|label)/.test(layer.id)) {
      map.setLayoutProperty(layer.id, 'visibility', on ? 'none' : 'visible');
    }
  }

  if (on) {
    setSandboxTag(map, tag, domains);
    map.easeTo({ pitch: 62, bearing: -18, duration: 900 });
    map.dragRotate.enable();
    map.touchZoomRotate.enableRotation();
  } else {
    map.easeTo({ pitch: 0, bearing: 0, duration: 700 });
  }
}

function setSandboxTag(map: MapLibreMap, tag: RenderTag,
                       domains: Record<string, ColourDomain>) {
  const field = TAG_TO_SCORE_FIELD[tag] ?? 'overall';
  const domain = domains[field] ?? FULL_RANGE_DOMAIN;
  for (const id of ['sandbox-massing', 'sandbox-buildings']) {
    if (map.getLayer(id)) {
      map.setPaintProperty(id, 'fill-extrusion-color', buildingScoreColor(field, domain));
    }
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
    geometry: createCirclePolygon([hs.center_lon, hs.center_lat], Math.max(hs.radius_m, 40) / 1000),
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

// The score ramp, and the only chroma the interface spends. It must be the
// same three stops the legend in App.tsx draws and the same ones declared as
// --ud-low/--ud-mid/--ud-high in index.css: a map whose colours disagree with
// its own legend is not readable at all. Kept as literals rather than read
// from CSS because MapLibre style expressions are evaluated on the GPU and
// never see the cascade.
const SCORE_GRADIENT = [
  { at: 0,   r: 140, g: 29,  b: 24  }, // #8C1D18  --ud-low
  { at: 50,  r: 150, g: 146, b: 138 }, // #96928A  --ud-mid
  { at: 100, r: 46,  g: 139, b: 98  }, // #2E8B62  --ud-high
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
  isochrone = null,
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
  // Whether the baked per-building tileset is being served. Asked once, via a
  // status endpoint rather than by probing for a 404 on a tile that might be
  // legitimately empty over water.
  const bakedTilesRef = useRef(false);
  const colourDomainsRef = useRef<Record<string, ColourDomain>>({});
  // Mirrored into state purely so the legend can label its own ends; the map
  // itself reads the ref.
  const [colourDomains, setColourDomains] = useState<Record<string, ColourDomain>>({});
  const [sandbox, setSandbox] = useState(false);
  const [sandboxReady, setSandboxReady] = useState(false);
  const [exaggeration, setExaggeration] = useState(6);
  const sandboxRef = useRef(false);

  const legendStyle = TAG_STYLES[renderTag];
  const legendDomain =
    colourDomains[TAG_TO_SCORE_FIELD[renderTag]] ?? FULL_RANGE_DOMAIN;
  const activeRadiusM = localRenderTarget?.radiusM ?? 200;

  useEffect(() => {
    localRenderTargetRef.current = localRenderTarget;
  }, [localRenderTarget]);

  // Sandbox toggle. Kept in an effect rather than in the click handler so the
  // map and React agree even if the mode is set from elsewhere later.
  useEffect(() => {
    sandboxRef.current = sandbox;
    const map = mapRef.current;
    if (!map || !map.isStyleLoaded()) return;
    setSandboxMode(map, sandbox, renderTag, colourDomainsRef.current);
    setDomMarkersVisible([markersRef, postMarkersRef], !sandbox);
    updateSandboxPin(map, sandbox ? localRenderTargetRef.current : null);
    if (sandbox) setExaggeration(exaggerationAtZoom(map.getZoom()));
  }, [sandbox, renderTag]);

  // Keep the in-scene pin on the current selection, and keep newly created DOM
  // markers hidden while the sandbox is open -- they are rebuilt on every
  // selection change and would otherwise reappear over the model.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !map.isStyleLoaded()) return;
    setDomMarkersVisible([markersRef, postMarkersRef], !sandboxRef.current);
    if (sandboxRef.current) updateSandboxPin(map, localRenderTarget);
  }, [localRenderTarget, refreshKey, markers, hotspots]);

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
      const status = await fetchBuildingTileStatus();
      bakedTilesRef.current = status.available;
      colourDomainsRef.current = status.domains;
      setColourDomains(status.domains);

      // The sandbox needs both the baked prisms and the coastline to stand
      // them on; without either it is not offered rather than shown broken.
      // The outline serves both the sandbox slab and the flat map's mask, so
      // it is fetched whether or not the building tiles exist.
      const outline = await fetchLandOutline();
      if (outline) {
        (map.getSource('landOutline') as GeoJSONSource | undefined)?.setData(outline);
        const mask = buildCityMask(outline);
        if (mask) {
          (map.getSource('cityMask') as GeoJSONSource | undefined)?.setData(mask);
        }
        // The sandbox also needs the baked prisms; without them it would be an
        // empty plate, so it is not offered rather than shown broken.
        if (status.available) setSandboxReady(true);
      }

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
        setBuildingRenderer(map, bakedTilesRef.current, true);
        if (bakedTilesRef.current) {
          setBuildingScoreTag(map, config.tag, colourDomainsRef.current);
        } else {
          renderVisibleBuildings(map, config, null, hexOverlayRef.current);
        }
      } else {
        renderHexOverlay(map, EMPTY_FEATURE_COLLECTION, hexOverlayRef);
        setGlobalVisualizationMode(map, false);
        setBuildingRenderer(map, bakedTilesRef.current, false);
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

    // Both handlers below exist only to feed the JavaScript renderer. With the
    // baked tileset serving the global view there is nothing to recompute when
    // the camera moves -- MapLibre re-colours from the tile it already has --
    // so they return immediately. That is the actual performance win here: not
    // a faster per-frame pass, but no per-frame pass.
    const needsJsRender = (config: RenderConfig) =>
      config.mode === 'local' || !bakedTilesRef.current;

    // Re-render buildings after map movement (flyTo, pan, zoom)
    map.on('moveend', () => {
      if (!map.isStyleLoaded()) return;
      if (sandboxRef.current) {
        // Report the exaggeration actually in force, since it changes with
        // zoom and the reader would otherwise assume the towers are to scale.
        setExaggeration(exaggerationAtZoom(map.getZoom()));
        return;
      }
      const config = activeConfigRef.current;
      if (!needsJsRender(config)) return;
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
      if (!needsJsRender(config)) return;
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
          setBuildingRenderer(map, bakedTilesRef.current, true);
          if (bakedTilesRef.current) {
            // Tag change only: the geometry and every category already sit in
            // the tiles the map is holding, so this is a paint write.
            setBuildingScoreTag(map, config.tag, colourDomainsRef.current);
          } else {
            renderVisibleBuildings(map, config, null, hexOverlayRef.current);
          }
        }
      } else {
        renderHexOverlay(map, EMPTY_FEATURE_COLLECTION, hexOverlayRef);
        setGlobalVisualizationMode(map, false);
        setBuildingRenderer(map, bakedTilesRef.current, false);
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

  /* Draw whatever isochrone the agent computed, and frame it once so the
     result is visible without the reader hunting for it. */
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;

    const apply = () => {
      const source = map.getSource('isochroneOverlay') as GeoJSONSource | undefined;
      if (!source) return;
      if (!isochrone?.geometry) {
        source.setData(EMPTY_FEATURE_COLLECTION);
        return;
      }
      source.setData({ type: 'FeatureCollection', features: [isochrone] });

      const coords: number[][] = [];
      const walk = (node: any) => {
        if (!Array.isArray(node)) return;
        if (typeof node[0] === 'number' && typeof node[1] === 'number') {
          coords.push(node as number[]);
          return;
        }
        node.forEach(walk);
      };
      walk((isochrone.geometry as any).coordinates);
      if (coords.length < 2) return;

      const lons = coords.map((c) => c[0]);
      const lats = coords.map((c) => c[1]);
      map.fitBounds(
        [
          [Math.min(...lons), Math.min(...lats)],
          [Math.max(...lons), Math.max(...lats)],
        ],
        { padding: 72, duration: 700, maxZoom: 16 },
      );
    };

    if (map.isStyleLoaded()) apply();
    else map.once('load', apply);
  }, [isochrone]);

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
      {sandboxReady && (
        <div className="absolute top-4 left-1/2 -translate-x-1/2 z-20 flex items-center gap-2">
          <button
            type="button"
            onClick={() => setSandbox((v) => !v)}
            aria-pressed={sandbox}
            className={`flex items-center gap-2 rounded-xl border px-3.5 py-2 text-[13px] font-medium shadow-lg backdrop-blur-md transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring ${
              sandbox
                ? 'bg-foreground border-foreground text-background'
                : 'bg-background/95 border-border text-foreground hover:bg-muted'
            }`}
          >
            <Box className="h-4 w-4" />
            {sandbox ? 'Sandbox' : 'Flat map'}
          </button>
          {sandbox && (
            <div className="rounded-xl border border-border bg-background/95 px-3 py-2 shadow-lg backdrop-blur-md">
              {/* The heights on screen are not to scale at city zoom, and a
                  reader has no way to know that from the picture. */}
              <span className="ud-label">Height</span>
              <span className="ml-2 font-mono text-[11px] tabular-nums text-foreground">
                {exaggeration <= 1.05 ? 'true scale' : `${exaggeration.toFixed(1)}× exaggerated`}
              </span>
            </div>
          )}
        </div>
      )}

      <div className="pointer-events-none absolute top-4 right-4 z-20">
        <div className="bg-background/95 backdrop-blur-md border border-border rounded-xl shadow-lg px-4 py-2.5">
          <div className="ud-label text-center mb-1.5">{legendStyle.label} Score</div>
          <div className="flex items-center gap-2">
            {/* The ends are the ramp's real endpoints, not 0 and 100.
                The scores do not span the full range -- overall sits between
                34 and 68 for 96% of buildings -- so the ramp is stretched over
                the 2nd-98th percentile to make the variation visible. Printing
                0 and 100 here would claim a span the colours do not cover and
                would make the middle of the city look like a mid score when it
                is really the middle of a much narrower band. */}
            <span className="font-mono text-[11px] text-muted-foreground leading-none w-6 text-right tabular-nums">
              {legendDomain.low}
            </span>
            <div
              className="h-2.5 w-32 rounded-sm"
              style={{
                // Three stops, matching the paint expression. Interpolating
                // straight from the low red to the high green would pass
                // through a muddy brown the map never draws, so the legend
                // would be describing a ramp that does not exist.
                background:
                  `linear-gradient(90deg, ${legendStyle.low} 0%, ` +
                  `${UD_MID} 50%, ${legendStyle.high} 100%)`,
              }}
            />
            <span className="font-mono text-[11px] text-muted-foreground leading-none w-6 tabular-nums">
              {legendDomain.high}
            </span>
          </div>
          {legendDomain !== FULL_RANGE_DOMAIN && (
            <div className="mt-1 text-center font-mono text-[9px] text-muted-foreground/70">
              2nd–98th pct
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
