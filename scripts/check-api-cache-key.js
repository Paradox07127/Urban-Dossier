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
