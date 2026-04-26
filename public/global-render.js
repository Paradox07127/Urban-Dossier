(function () {
  const DEFAULT_CENTER = [-73.98513, 40.758896];
  const TAG_STYLES = {
    general: { low: '#d73027', high: '#fee8c8', accent: '#8c1d18', legend: 'General' },
    safety: { low: '#c51b8a', high: '#fde0dd', accent: '#7a0177', legend: 'Safety' },
    transit: { low: '#2b8cbe', high: '#deebf7', accent: '#045a8d', legend: 'Transit' },
    amenities: { low: '#31a354', high: '#e5f5e0', accent: '#006d2c', legend: 'Amenities' },
  };

  const tagSelect = document.getElementById('tag-select');
  const loadButton = document.getElementById('load-button');
  const applyButton = document.getElementById('apply-button');
  const payloadInput = document.getElementById('payload-input');
  const statusEl = document.getElementById('status');
  const legendTitle = document.getElementById('legend-title');
  const legendSub = document.getElementById('legend-sub');
  const legendBar = document.getElementById('legend-bar');

  let mapLoaded = false;
  let activeConfig = null;

  const style = {
    version: 8,
    glyphs: window.location.origin + '/fonts/{fontstack}/{range}.pbf',
    sources: {
      openmaptiles: {
        type: 'vector',
        tiles: [window.location.origin + '/tiles/{z}/{x}/{y}.pbf'],
        minzoom: 0,
        maxzoom: 14,
      },
      renderedBuildings: {
        type: 'geojson',
        data: emptyFeatureCollection(),
      },
      renderedBuildings3d: {
        type: 'geojson',
        data: emptyFeatureCollection(),
      },
    },
    layers: [
      { id: 'background', type: 'background', paint: { 'background-color': '#f0ede9' } },
      { id: 'water', type: 'fill', source: 'openmaptiles', 'source-layer': 'water', paint: { 'fill-color': '#a3cfec', 'fill-opacity': 0.8 } },
      { id: 'waterway', type: 'line', source: 'openmaptiles', 'source-layer': 'waterway', paint: { 'line-color': '#a3cfec', 'line-width': 1.5 } },
      { id: 'landcover', type: 'fill', source: 'openmaptiles', 'source-layer': 'landcover', paint: { 'fill-color': '#d4edaa', 'fill-opacity': 0.4 } },
      {
        id: 'landuse',
        type: 'fill',
        source: 'openmaptiles',
        'source-layer': 'landuse',
        paint: {
          'fill-color': ['match', ['get', 'class'], 'residential', '#ede8e3', 'commercial', '#f5e6d0', 'industrial', '#e8e0d8', 'park', '#c8e6a0', 'cemetery', '#d6e4c0', 'hospital', '#f8d8d8', 'school', '#f0e4d0', '#e8e4de'],
          'fill-opacity': 0.4,
        },
      },
      { id: 'park-fill', type: 'fill', source: 'openmaptiles', 'source-layer': 'park', paint: { 'fill-color': '#c8e6a0', 'fill-opacity': 0.5 } },
      {
        id: 'building',
        type: 'fill',
        source: 'renderedBuildings',
        minzoom: 12,
        paint: {
          'fill-color': ['coalesce', ['get', 'render_color'], '#d4cfc8'],
          'fill-opacity': 0.88,
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
          'fill-extrusion-opacity': 0.9,
        },
      },
      { id: 'road-motorway', type: 'line', source: 'openmaptiles', 'source-layer': 'transportation', filter: ['==', 'class', 'motorway'], layout: { 'line-cap': 'round', 'line-join': 'round' }, paint: { 'line-color': '#ffa35c', 'line-width': { stops: [[5, 0.5], [14, 6], [18, 12]] } } },
      { id: 'road-primary', type: 'line', source: 'openmaptiles', 'source-layer': 'transportation', filter: ['in', 'class', 'trunk', 'primary'], layout: { 'line-cap': 'round', 'line-join': 'round' }, paint: { 'line-color': '#ffd080', 'line-width': { stops: [[5, 0.3], [14, 4], [18, 10]] } } },
      { id: 'road-secondary', type: 'line', source: 'openmaptiles', 'source-layer': 'transportation', filter: ['==', 'class', 'secondary'], layout: { 'line-cap': 'round', 'line-join': 'round' }, paint: { 'line-color': '#f0e8c0', 'line-width': { stops: [[8, 0.3], [14, 3], [18, 8]] } } },
      { id: 'road-minor', type: 'line', source: 'openmaptiles', 'source-layer': 'transportation', filter: ['in', 'class', 'minor', 'service', 'street'], minzoom: 12, layout: { 'line-cap': 'round', 'line-join': 'round' }, paint: { 'line-color': '#ffffff', 'line-width': { stops: [[12, 0.5], [14, 2], [18, 6]] } } },
    ],
  };

  const map = new maplibregl.Map({
    container: 'map',
    style,
    center: DEFAULT_CENTER,
    zoom: 12,
    maxZoom: 20,
  });

  map.addControl(new maplibregl.NavigationControl(), 'top-left');

  map.on('load', async function () {
    mapLoaded = true;
    const response = await fetch('/api/render/global?tag=general');
    const config = await response.json();
    applyConfig(config);
  });

  map.on('moveend', function () {
    if (mapLoaded && activeConfig) {
      renderVisibleBuildings(activeConfig);
    }
  });

  loadButton.addEventListener('click', async function () {
    try {
      const selectedTag = (tagSelect.value || 'general').toLowerCase();
      const response = await fetch('/api/render/global?tag=' + encodeURIComponent(selectedTag));
      const config = await response.json();
      applyConfig(config);
      statusEl.textContent = 'Loaded built-in dataset for tag: ' + config.tag + '.';
    } catch (error) {
      statusEl.textContent = 'Dataset load error: ' + error.message;
    }
  });

  applyButton.addEventListener('click', async function () {
    try {
      const selectedTag = (tagSelect.value || 'general').toLowerCase();
      const payload = {
        tag: selectedTag,
        points: JSON.parse(payloadInput.value),
      };

      const response = await fetch('/api/render/global', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });

      const result = await response.json();
      if (!response.ok) {
        throw new Error(result.error || 'API request failed');
      }

      applyConfig(result);
      statusEl.textContent = 'API payload applied. Visible NYC buildings are recolored.';
    } catch (error) {
      statusEl.textContent = 'Payload error: ' + error.message;
    }
  });

  function applyConfig(config) {
    activeConfig = config;
    tagSelect.value = config.tag === 'general' ? '' : config.tag;
    updateLegend(config);
    renderVisibleBuildings(config);
  }

  function updateLegend(config) {
    const style = config.style || TAG_STYLES[config.tag] || TAG_STYLES.general;
    legendTitle.textContent = (style.legend || 'General') + ' score';
    legendSub.textContent = 'More saturated color means lower score.';
    legendBar.style.background = 'linear-gradient(to right, ' + style.low + ', ' + style.high + ')';
  }

  function renderVisibleBuildings(config) {
    const fillSource = map.getSource('renderedBuildings');
    const extrusionSource = map.getSource('renderedBuildings3d');
    if (!fillSource || !extrusionSource) return;

    const features = map.querySourceFeatures('openmaptiles', { sourceLayer: 'building' });
    const seen = new Set();
    const styledFeatures = [];

    for (const feature of features) {
      if (!feature.geometry) continue;
      const coords = feature.geometry.type === 'Polygon'
          ? feature.geometry.coordinates[0]
          : feature.geometry.coordinates[0][0];
      const signature = coords[0][0] + ':' + coords[0][1] + ':' + coords.length;
      if (seen.has(signature)) continue;
      seen.add(signature);

      const center = getGeometryCenter(feature.geometry);
      const nearest = getNearestPoint(center, config.points);
      const colors = getScoreColors(config.tag, nearest ? nearest.score : 100);
      const properties = Object.assign({}, feature.properties || {}, {
        render_color: colors.fill,
        outline_color: colors.outline,
      });

      styledFeatures.push({
        type: 'Feature',
        properties: properties,
        geometry: feature.geometry,
      });
    }

    const collection = { type: 'FeatureCollection', features: styledFeatures };
    fillSource.setData(collection);
    extrusionSource.setData(collection);
    statusEl.textContent = 'Rendered ' + styledFeatures.length + ' visible buildings using the ' + config.tag + ' palette.';
  }

  function getNearestPoint(center, points) {
    let best = null;
    let bestDistance = Infinity;

    for (const point of points || []) {
      const distance = distanceKm([point.longitude, point.latitude], center);
      if (distance < bestDistance) {
        bestDistance = distance;
        best = point;
      }
    }

    return best;
  }

  function getGeometryCenter(geometry) {
    const bounds = { minLng: Infinity, minLat: Infinity, maxLng: -Infinity, maxLat: -Infinity };
    visitCoordinates(geometry, function (lng, lat) {
      if (lng < bounds.minLng) bounds.minLng = lng;
      if (lat < bounds.minLat) bounds.minLat = lat;
      if (lng > bounds.maxLng) bounds.maxLng = lng;
      if (lat > bounds.maxLat) bounds.maxLat = lat;
    });
    return [(bounds.minLng + bounds.maxLng) / 2, (bounds.minLat + bounds.maxLat) / 2];
  }

  function visitCoordinates(geometry, visitor) {
    if (geometry.type === 'Polygon') {
      geometry.coordinates.forEach(function (ring) {
        ring.forEach(function (coord) { visitor(coord[0], coord[1]); });
      });
    } else if (geometry.type === 'MultiPolygon') {
      geometry.coordinates.forEach(function (polygon) {
        polygon.forEach(function (ring) {
          ring.forEach(function (coord) { visitor(coord[0], coord[1]); });
        });
      });
    }
  }

  function getScoreColors(tag, score) {
    const palette = TAG_STYLES[tag] || TAG_STYLES.general;
    const t = Math.max(0, Math.min(1, score / 100));
    return {
      fill: lerpColor(palette.low, palette.high, t),
      outline: palette.accent,
    };
  }

  function lerpColor(hexA, hexB, t) {
    const a = hexToRgb(hexA);
    const b = hexToRgb(hexB);
    const r = Math.round(a.r + (b.r - a.r) * t);
    const g = Math.round(a.g + (b.g - a.g) * t);
    const bch = Math.round(a.b + (b.b - a.b) * t);
    return 'rgb(' + r + ', ' + g + ', ' + bch + ')';
  }

  function hexToRgb(hex) {
    const normalized = hex.replace('#', '');
    return {
      r: parseInt(normalized.slice(0, 2), 16),
      g: parseInt(normalized.slice(2, 4), 16),
      b: parseInt(normalized.slice(4, 6), 16),
    };
  }

  function emptyFeatureCollection() {
    return { type: 'FeatureCollection', features: [] };
  }

  function distanceKm(a, b) {
    var cosLat = 0.7580107;
    var degToKm = 111.320;
    var dlat = (b[1] - a[1]) * degToKm;
    var dlng = (b[0] - a[0]) * degToKm * cosLat;
    return Math.sqrt(dlat * dlat + dlng * dlng);
  }

  function toRadians(value) {
    return value * Math.PI / 180;
  }
})();
