'use strict';

class MethodologyPublicationError extends Error {
  constructor(message, code = 'INVALID_METHODOLOGY_PUBLICATION') {
    super(message);
    this.name = 'MethodologyPublicationError';
    this.code = code;
  }
}

function object(value, label) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new MethodologyPublicationError(`${label} must be an object`);
  }
  return value;
}

function strings(value) {
  return Array.isArray(value)
    ? value.filter((item) => typeof item === 'string')
    : [];
}

function buildMethodologyPublication(registryValue, coverageValue, expectedVersion) {
  const registry = object(registryValue, 'registry');
  const coverage = object(coverageValue, 'coverage');
  if (typeof expectedVersion !== 'string' || !expectedVersion) {
    throw new MethodologyPublicationError('expected methodology version is missing');
  }
  if (registry.methodology_version !== expectedVersion) {
    throw new MethodologyPublicationError(
      `backend methodology ${registry.methodology_version ?? 'missing'} does not match code ${expectedVersion}`,
      'METHODOLOGY_VERSION_MISMATCH',
    );
  }
  if (!Array.isArray(registry.categories) || !Array.isArray(registry.metrics)) {
    throw new MethodologyPublicationError('registry categories and metrics must be arrays');
  }
  const staleMetric = registry.metrics.find(
    (metric) => !metric || metric.methodology_version !== expectedVersion,
  );
  if (staleMetric) {
    throw new MethodologyPublicationError(
      `metric ${staleMetric.id ?? 'unknown'} does not match code ${expectedVersion}`,
      'METHODOLOGY_VERSION_MISMATCH',
    );
  }

  const required = strings(coverage.required_datasets);
  const availableSet = new Set(strings(coverage.available_datasets));
  const missingSet = new Set(strings(coverage.missing_datasets));
  const requiredSet = new Set(required);
  const unexpectedDataset = [...availableSet, ...missingSet].find(
    (id) => !requiredSet.has(id),
  );
  if (!required.length || requiredSet.size !== required.length || unexpectedDataset) {
    throw new MethodologyPublicationError(
      `dataset coverage has an invalid required set${unexpectedDataset ? ` at ${unexpectedDataset}` : ''}`,
      'INVALID_DATASET_COVERAGE',
    );
  }
  const ambiguousDataset = required.find(
    (id) => availableSet.has(id) === missingSet.has(id),
  );
  if (ambiguousDataset) {
    throw new MethodologyPublicationError(
      `dataset coverage is not a complete partition at ${ambiguousDataset}`,
      'INVALID_DATASET_COVERAGE',
    );
  }
  const datasets = required.map((id) => ({
    id,
    available: availableSet.has(id) && !missingSet.has(id),
  }));

  return {
    schema_version: '1.0',
    methodology_version: expectedVersion,
    code_methodology_version: expectedVersion,
    version_verified: true,
    categories: registry.categories,
    metrics: registry.metrics,
    duplicated_sources: Array.isArray(registry.duplicated_sources)
      ? registry.duplicated_sources
      : [],
    overlapping_metrics: Array.isArray(registry.overlapping_metrics)
      ? registry.overlapping_metrics
      : [],
    dataset_coverage: {
      provider: typeof coverage.provider === 'string' ? coverage.provider : 'unknown',
      provider_ready: coverage.provider_ready === true,
      overview_ready: coverage.overview_ready === true,
      available_count: datasets.filter((dataset) => dataset.available).length,
      required_count: datasets.length,
      datasets,
      available_overview_categories: strings(coverage.available_overview_categories),
      missing_overview_categories: strings(coverage.missing_overview_categories),
    },
  };
}

module.exports = {
  buildMethodologyPublication,
  MethodologyPublicationError,
};
