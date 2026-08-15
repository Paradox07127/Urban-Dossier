// Contract test for the disk-cache key server.js derives for /api/analyze-point
// and the report route (server.js:919, :935). The key decides when two requests
// share a cached report, so the properties below are correctness, not style:
// equal-but-differently-typed coordinates must collide, and anything that
// changes the answer -- window, radius, data mode, priority order -- must not.
//
// Wired into the root package.json `test` script. It sat unreferenced for a
// while, and an unrun test guarding a cache key is indistinguishable from no
// test at all.
const assert = require('node:assert/strict');
const { previewCacheKey } = require('./api-cache-key');

const base = {
  latitude: 40.758,
  longitude: -73.9855,
  radius_m: 200,
  priority_order: ['safety', 'transit', 'amenities'],
  time_window_days: 365,
};

assert.equal(previewCacheKey(base), previewCacheKey({ ...base, latitude: '40.758000' }));
assert.notEqual(previewCacheKey(base), previewCacheKey({ ...base, time_window_days: 30 }));
assert.notEqual(previewCacheKey(base), previewCacheKey({ ...base, data_mode: 'fixture' }));
assert.notEqual(previewCacheKey(base), previewCacheKey({ ...base, radius_m: 500 }));
assert.notEqual(
  previewCacheKey(base),
  previewCacheKey({ ...base, latitude: base.latitude + 0.00001 }),
);
assert.notEqual(
  previewCacheKey(base),
  previewCacheKey({ ...base, priority_order: [...base.priority_order].reverse() }),
);

console.log('API cache key contract OK');
