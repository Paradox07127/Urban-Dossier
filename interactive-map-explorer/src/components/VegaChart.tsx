import { useEffect, useRef, useState } from 'react';

import type { ChartSpec } from '../types';


interface Props {
  chart: ChartSpec;
  className?: string;
}

export default function VegaChart({ chart, className = '' }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    let cancelled = false;
    let finalize: (() => void) | undefined;
    let observer: ResizeObserver | undefined;
    setError(null);
    container.replaceChildren();

    // Dynamic import creates a local Vite chunk. Rendering remains available
    // with the network disconnected; no CDN script or remote data URL is used.
    import('vega-embed')
      .then(({ default: embed }) =>
        embed(container, chart.spec as any, {
          actions: false,
          renderer: 'svg',
          mode: 'vega-lite',
        }),
      )
      .then((result) => {
        if (cancelled) {
          result.view.finalize();
          return;
        }
        finalize = () => result.view.finalize();
        observer = new ResizeObserver(() => {
          result.view.resize().runAsync().catch(() => undefined);
        });
        observer.observe(container);
      })
      .catch((reason) => {
        if (!cancelled) {
          setError(reason instanceof Error ? reason.message : 'Chart rendering failed');
        }
      });

    return () => {
      cancelled = true;
      observer?.disconnect();
      finalize?.();
    };
  }, [chart]);

  return (
    <figure className={`rounded-md border border-border/70 bg-card px-3 py-3 ${className}`}>
      <figcaption className="mb-2 flex items-baseline justify-between gap-3">
        <span className="ud-label">{chart.title}</span>
        <span
          className="font-mono text-[9px] text-muted-foreground/70"
          title={chart.code_ref}
        >
          method {chart.methodology_version}
        </span>
      </figcaption>
      {error ? (
        <div role="alert" className="py-6 text-xs text-muted-foreground">
          Chart unavailable: {error}
        </div>
      ) : (
        <div ref={containerRef} className="min-h-[110px] w-full overflow-hidden" />
      )}
    </figure>
  );
}
