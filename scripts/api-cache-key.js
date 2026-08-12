const crypto = require('crypto');

function normalizedNumber(value, fallback, digits) {
  const number = Number(value ?? fallback);
  if (!Number.isFinite(number)) return Number(fallback).toFixed(digits);
  return number.toFixed(digits);
}

function previewCacheKey(body = {}) {
  const payload = {
    latitude: normalizedNumber(body.latitude, 0, 6),
    longitude: normalizedNumber(body.longitude, 0, 6),
    radius_m: Number(body.radius_m ?? 500),
    priority_order: Array.isArray(body.priority_order)
      ? body.priority_order.map((value) => String(value).toLowerCase())
      : [],
    time_window_days: Number(body.time_window_days ?? 365),
    data_mode: body.data_mode == null ? null : String(body.data_mode),
  };
  return crypto
    .createHash('sha256')
    .update(JSON.stringify(payload))
    .digest('hex')
    .slice(0, 32);
}

module.exports = { previewCacheKey };
