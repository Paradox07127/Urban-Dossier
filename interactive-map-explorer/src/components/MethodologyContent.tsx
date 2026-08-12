import { useEffect, useState } from 'react';

export interface MetricRow {
  id: string;
  category: string;
  label: string;
  description: string;
  unit: string;
  direction: string;
  spatial_grain: string;
  temporal_grain: string;
  weight_in_category: number;
  normalization: string;
  absence_means_zero: boolean;
  overall_contribution: number;
  source_dataset: string;
  methodology_version: string;
  data_vintage: string | null;
  overlaps_with: string[];
  notes: string | null;
}

export interface MethodologyPublication {
  schema_version: '1.0';
  methodology_version: string;
  code_methodology_version: string;
  version_verified: true;
  categories: Array<{
    id: string;
    label: string;
    weight_in_overall: number;
    metrics: string[];
    notes: string | null;
  }>;
  metrics: MetricRow[];
  dataset_coverage: {
    provider: string;
    provider_ready: boolean;
    overview_ready: boolean;
    available_count: number;
    required_count: number;
    datasets: Array<{ id: string; available: boolean }>;
    available_overview_categories: string[];
    missing_overview_categories: string[];
  };
}

const GRAIN_LABEL: Record<string, string> = {
  h3_r9: '≈175 m hex',
  zip: 'ZIP code',
};
const NORMALIZATION_LABEL: Record<string, string> = {
  empirical_percentile: 'empirical percentile',
  composite_percentile: 'weighted percentile blend',
};
const DIRECTION_LABEL: Record<string, string> = {
  higher_is_better: 'higher is better',
  lower_is_better: 'lower is better',
};

export function useMethodologyPublication() {
  const [publication, setPublication] = useState<MethodologyPublication | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetch('/api/methodology')
      .then(async (response) => {
        const body = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(body.detail || `HTTP ${response.status}`);
        }
        if (
          body.version_verified !== true
          || body.methodology_version !== body.code_methodology_version
        ) {
          throw new Error('Methodology version verification failed at render time');
        }
        return body as MethodologyPublication;
      })
      .then((data) => {
        if (!cancelled) setPublication(data);
      })
      .catch((reason) => {
        if (!cancelled) setError(String(reason));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return { publication, error };
}

function Status({ available }: { available: boolean }) {
  return (
    <span className="inline-flex items-center gap-1.5 font-mono text-[11px]">
      <span
        aria-hidden="true"
        className={`h-1.5 w-1.5 rounded-full ${available ? 'bg-foreground' : 'bg-amber-600'}`}
      />
      {available ? 'ready' : 'missing'}
    </span>
  );
}

export function MethodologyContent({ publication }: { publication: MethodologyPublication }) {
  const coverage = publication.dataset_coverage;
  return (
    <div className="space-y-8 text-sm">
      <section className="grid gap-3 sm:grid-cols-3" aria-label="Publication status">
        <div className="rounded-md border border-border bg-card p-3">
          <div className="ud-label">Code equality</div>
          <div className="mt-2 font-mono text-sm">v{publication.methodology_version}</div>
          <div className="mt-1 text-[11px] text-muted-foreground">verified on this request</div>
        </div>
        <div className="rounded-md border border-border bg-card p-3">
          <div className="ud-label">Analysis datasets</div>
          <div className="mt-2 font-mono text-sm">
            {coverage.available_count}/{coverage.required_count} ready
          </div>
          <div className="mt-1 text-[11px] text-muted-foreground">{coverage.provider}</div>
        </div>
        <div className="rounded-md border border-border bg-card p-3">
          <div className="ud-label">Overview publication</div>
          <div className="mt-2 font-mono text-sm">{coverage.overview_ready ? 'ready' : 'unavailable'}</div>
          <div className="mt-1 text-[11px] text-muted-foreground">
            {coverage.available_overview_categories.length} map categories
          </div>
        </div>
      </section>

      <section>
        <h2 className="ud-display text-xl">How the composite is read</h2>
        <p className="mt-2 max-w-3xl text-xs leading-relaxed text-muted-foreground">
          Raw sub-metric scores are empirical percentiles within New York City.
          Category and overall scores are weighted averages of those percentiles;
          they are not percentile ranks themselves. The public overall headline is
          a fixed 20-point tier spanning the containing cell&apos;s production-method
          95% sensitivity interval. The radius-aggregated point estimate is secondary.
        </p>
      </section>

      <section>
        <div className="mb-3 flex items-end justify-between gap-3">
          <div>
            <h2 className="ud-display text-xl">Metric audit table</h2>
            <p className="mt-1 text-xs text-muted-foreground">
              Source, vintage, geography, normalization and exact configured weight.
            </p>
          </div>
          <span className="font-mono text-[11px] text-muted-foreground">
            {publication.metrics.length} metrics
          </span>
        </div>
        <div className="overflow-x-auto rounded-md border border-border bg-card">
          <table className="w-full min-w-[920px] border-collapse text-left text-[11px]">
            <thead className="border-b border-border bg-muted/60 font-mono uppercase tracking-wide text-muted-foreground">
              <tr>
                <th className="px-3 py-2 font-medium">Metric</th>
                <th className="px-3 py-2 font-medium">Dataset / vintage</th>
                <th className="px-3 py-2 font-medium">Grain / cadence</th>
                <th className="px-3 py-2 font-medium">Direction / normalization</th>
                <th className="px-3 py-2 text-right font-medium">Category weight</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {publication.metrics.map((metric) => (
                <tr key={metric.id} className="align-top">
                  <td className="px-3 py-2.5">
                    <div className="font-medium text-foreground">{metric.label}</div>
                    <div className="mt-0.5 font-mono text-[10px] text-muted-foreground">
                      {metric.category} / {metric.id}
                    </div>
                  </td>
                  <td className="max-w-[260px] px-3 py-2.5">
                    <div>{metric.source_dataset}</div>
                    <div className={metric.data_vintage ? 'mt-0.5 text-muted-foreground' : 'mt-0.5 text-amber-700 dark:text-amber-500'}>
                      {metric.data_vintage ?? 'vintage not verified'}
                    </div>
                  </td>
                  <td className="px-3 py-2.5">
                    <div>{GRAIN_LABEL[metric.spatial_grain] ?? metric.spatial_grain}</div>
                    <div className="mt-0.5 text-muted-foreground">{metric.temporal_grain}</div>
                  </td>
                  <td className="px-3 py-2.5">
                    <div>{DIRECTION_LABEL[metric.direction] ?? metric.direction}</div>
                    <div className="mt-0.5 text-muted-foreground">
                      {NORMALIZATION_LABEL[metric.normalization] ?? metric.normalization}
                    </div>
                  </td>
                  <td className="px-3 py-2.5 text-right font-mono tabular-nums">
                    {Math.round(metric.weight_in_category * 100)}%
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section>
        <div className="mb-3 flex items-end justify-between gap-3">
          <div>
            <h2 className="ud-display text-xl">Runtime dataset coverage</h2>
            <p className="mt-1 text-xs text-muted-foreground">
              Required prepared collections checked by the active deterministic provider.
            </p>
          </div>
          <span className="font-mono text-[11px] text-muted-foreground">
            {coverage.available_count}/{coverage.required_count}
          </span>
        </div>
        <div className="grid overflow-hidden rounded-md border border-border bg-card sm:grid-cols-2 lg:grid-cols-3">
          {coverage.datasets.map((dataset) => (
            <div key={dataset.id} className="flex items-center justify-between gap-3 border-b border-border px-3 py-2.5 sm:border-r">
              <span className="font-mono text-[11px]">{dataset.id}</span>
              <Status available={dataset.available} />
            </div>
          ))}
        </div>
      </section>

      <section>
        <h2 className="ud-display text-xl">Category weights</h2>
        <div className="mt-3 grid gap-3 sm:grid-cols-2">
          {publication.categories.map((category) => (
            <div key={category.id} className="rounded-md border border-border bg-card p-3">
              <div className="flex items-baseline justify-between gap-3">
                <h3 className="text-sm font-semibold">{category.label}</h3>
                <span className="font-mono text-[11px] text-muted-foreground">
                  {Math.round(category.weight_in_overall * 100)}% overall
                </span>
              </div>
              <p className="mt-2 text-[11px] leading-relaxed text-muted-foreground">
                {category.notes || `${category.metrics.length} registered metrics.`}
              </p>
            </div>
          ))}
        </div>
      </section>

      <p className="border-t border-border pt-4 text-[11px] leading-relaxed text-muted-foreground">
        Map, chart and legend colours use server-published breaks. Tied values may
        reduce the effective class count rather than inventing distinctions. Full
        correlation and sensitivity audit artifacts live under{' '}
        <span className="font-mono">docs/methodology/</span> in the repository.
      </p>
    </div>
  );
}
