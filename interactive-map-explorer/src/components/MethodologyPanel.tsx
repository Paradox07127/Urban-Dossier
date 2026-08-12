/**
 * The methodology page -- EXPANSION_PLAN item 1.6, JRC statistical-audit
 * style: every metric with its source, unit, grain, weight and vintage,
 * straight from GET /api/metrics.
 *
 * Rendered from the registry rather than written as prose, which is the
 * acceptance criterion: the page cannot claim a version the code does not,
 * because the version on screen IS the code's. Nothing here is restated or
 * summarised client-side; the panel is a projection of the API payload.
 */
import { useEffect, useState } from 'react';
import { X } from 'lucide-react';

interface MetricRow {
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
  data_vintage: string | null;
  overlaps_with: string[];
  notes: string | null;
}

interface Registry {
  methodology_version: string;
  categories: Array<{
    id: string;
    label: string;
    weight_in_overall: number;
    metrics: string[];
    notes: string | null;
  }>;
  metrics: MetricRow[];
  duplicated_sources: Array<{ source_relpath: string; metrics: string[] }>;
  overlapping_metrics: Array<{ metrics: string[] }>;
}

const GRAIN_LABEL: Record<string, string> = {
  h3_r9: '≈175 m hex',
  zip: 'ZIP code',
};
const NORMALIZATION_LABEL: Record<string, string> = {
  empirical_percentile: 'empirical percentile of the observed metric',
  composite_percentile: 'weighted blend of percentile components',
};

const DIRECTION_LABEL: Record<string, string> = {
  higher_is_better: 'higher is better',
  lower_is_better: 'lower is better',
};

export default function MethodologyPanel({ onClose }: { onClose: () => void }) {
  const [registry, setRegistry] = useState<Registry | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetch('/api/metrics')
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((data) => { if (!cancelled) setRegistry(data); })
      .catch((e) => { if (!cancelled) setError(String(e)); });
    return () => { cancelled = true; };
  }, []);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label="Scoring methodology"
    >
      <div
        className="flex max-h-[85vh] w-full max-w-2xl flex-col overflow-hidden rounded-xl border border-border bg-background shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-border px-5 py-3">
          <div>
            <h2 className="text-sm font-semibold text-foreground">How the scores work</h2>
            {registry && (
              <div className="font-mono text-[11px] text-muted-foreground">
                methodology v{registry.methodology_version} · {registry.metrics.length} metrics
              </div>
            )}
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="rounded p-1 text-muted-foreground hover:text-foreground"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="overflow-y-auto px-5 py-4 text-sm">
          {error && (
            <p className="text-muted-foreground">
              The metric registry is unreachable ({error}). Scores on the map still
              work; this page describes them and needs the backend.
            </p>
          )}
          {!registry && !error && <p className="text-muted-foreground">Loading…</p>}

          {registry && (
            <div className="space-y-6">
              <p className="text-xs leading-relaxed text-muted-foreground">
                Raw sub-metric scores are empirical percentiles within New York
                City. Category and overall scores are weighted averages of those
                percentiles; they are not percentile ranks themselves, so 50 is
                the scale midpoint rather than a claim that half the city ranks
                below it. Point cards label the cell-level sensitivity interval
                separately from the radius-aggregated headline. Weights below are
                the exact ones the composite uses.
              </p>

              {registry.categories.map((category) => (
                <section key={category.id}>
                  <h3 className="mb-1 flex items-baseline justify-between text-xs font-semibold uppercase tracking-wide text-foreground">
                    {category.label}
                    <span className="font-mono text-[11px] font-normal text-muted-foreground">
                      {Math.round(category.weight_in_overall * 100)}% of overall
                    </span>
                  </h3>
                  {category.notes && (
                    <p className="mb-2 text-[11px] leading-snug text-muted-foreground">
                      {category.notes}
                    </p>
                  )}
                  <div className="divide-y divide-border rounded-md border border-border">
                    {registry.metrics
                      .filter((metric) => metric.category === category.id)
                      .map((metric) => (
                        <details key={metric.id} className="group px-3 py-2">
                          <summary className="flex cursor-pointer list-none items-baseline justify-between gap-2">
                            <span className="text-[13px] text-foreground">{metric.label}</span>
                            <span className="shrink-0 font-mono text-[11px] tabular-nums text-muted-foreground">
                              {Math.round(metric.weight_in_category * 100)}%
                              {' · '}
                              {GRAIN_LABEL[metric.spatial_grain] ?? metric.spatial_grain}
                            </span>
                          </summary>
                          <div className="mt-1.5 space-y-1 text-[11px] leading-snug text-muted-foreground">
                            <p>{metric.description}</p>
                            <p className="font-mono">unit: {metric.unit}</p>
                            <p>
                              normalization:{' '}
                              {NORMALIZATION_LABEL[metric.normalization] ?? metric.normalization}
                            </p>
                            <p>
                              direction: {DIRECTION_LABEL[metric.direction] ?? metric.direction}
                            </p>
                            <p>temporal grain: {metric.temporal_grain}</p>
                            <p>
                              absent cell:{' '}
                              {metric.absence_means_zero
                                ? 'observed zero'
                                : 'missing (no denominator)'}
                            </p>
                            <p>source: {metric.source_dataset}</p>
                            {metric.data_vintage && (
                              <p className="text-amber-700 dark:text-amber-500">
                                data vintage: {metric.data_vintage}
                              </p>
                            )}
                            {metric.overlaps_with.length > 0 && (
                              <p>
                                declared overlap with{' '}
                                {metric.overlaps_with.map((m) => `“${m}”`).join(', ')} — the
                                pair shares one weight slot.
                              </p>
                            )}
                          </div>
                        </details>
                      ))}
                  </div>
                </section>
              ))}

              <p className="text-[11px] leading-relaxed text-muted-foreground">
                Map, chart, and legend colours use the same server-published
                quantile breaks calculated over land-clipped H3 r8 overview
                cells. The building histogram remains frequency context only;
                it does not redefine the colour classes. Tied values may reduce
                the effective class count instead of inventing distinctions.
                Full method notes, the correlation audit and the sensitivity
                analysis live in the repository under{' '}
                <span className="font-mono">docs/methodology/</span>.
              </p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
