'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');

const {
  buildMethodologyPublication,
  MethodologyPublicationError,
} = require('./methodology-publication');

function fixture() {
  return {
    registry: {
      methodology_version: '3.9.0',
      categories: [{ id: 'safety', label: 'Safety', metrics: ['collision'] }],
      metrics: [
        { id: 'collision', methodology_version: '3.9.0' },
        { id: 'rodent', methodology_version: '3.9.0' },
      ],
      duplicated_sources: [],
      overlapping_metrics: [],
    },
    coverage: {
      provider: 'DirectQueryDataProvider',
      provider_ready: true,
      overview_ready: true,
      required_datasets: ['collisions', 'rodent_inspections'],
      available_datasets: ['collisions'],
      missing_datasets: ['rodent_inspections'],
      available_overview_categories: ['overall', 'safety'],
      missing_overview_categories: [],
    },
  };
}

test('publishes only an exact code, registry, metric and coverage snapshot', () => {
  const { registry, coverage } = fixture();
  const publication = buildMethodologyPublication(registry, coverage, '3.9.0');
  assert.equal(publication.version_verified, true);
  assert.equal(publication.code_methodology_version, '3.9.0');
  assert.equal(publication.methodology_version, '3.9.0');
  assert.equal(publication.dataset_coverage.available_count, 1);
  assert.equal(publication.dataset_coverage.required_count, 2);
  assert.deepEqual(publication.dataset_coverage.datasets, [
    { id: 'collisions', available: true },
    { id: 'rodent_inspections', available: false },
  ]);
});

test('rejects a stale top-level registry version', () => {
  const { registry, coverage } = fixture();
  registry.methodology_version = '3.8.0';
  assert.throws(
    () => buildMethodologyPublication(registry, coverage, '3.9.0'),
    (error) => error instanceof MethodologyPublicationError
      && error.code === 'METHODOLOGY_VERSION_MISMATCH',
  );
});

test('rejects one stale metric even when the registry headline is current', () => {
  const { registry, coverage } = fixture();
  registry.metrics[1].methodology_version = '3.8.0';
  assert.throws(
    () => buildMethodologyPublication(registry, coverage, '3.9.0'),
    (error) => error instanceof MethodologyPublicationError
      && error.code === 'METHODOLOGY_VERSION_MISMATCH',
  );
});

test('rejects ambiguous ready/missing dataset coverage', () => {
  const { registry, coverage } = fixture();
  coverage.missing_datasets.push('collisions');
  assert.throws(
    () => buildMethodologyPublication(registry, coverage, '3.9.0'),
    (error) => error instanceof MethodologyPublicationError
      && error.code === 'INVALID_DATASET_COVERAGE',
  );
});

test('rejects empty or out-of-contract dataset coverage', () => {
  const { registry, coverage } = fixture();
  coverage.available_datasets.push('undeclared');
  assert.throws(
    () => buildMethodologyPublication(registry, coverage, '3.9.0'),
    (error) => error instanceof MethodologyPublicationError
      && error.code === 'INVALID_DATASET_COVERAGE',
  );
  coverage.required_datasets = [];
  coverage.available_datasets = [];
  coverage.missing_datasets = [];
  assert.throws(
    () => buildMethodologyPublication(registry, coverage, '3.9.0'),
    (error) => error instanceof MethodologyPublicationError
      && error.code === 'INVALID_DATASET_COVERAGE',
  );
});
