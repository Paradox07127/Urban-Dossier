import { useState } from 'react';
import { motion } from 'motion/react';
import { ChevronRight } from 'lucide-react';
import type { AgentTrace } from '../types';

/**
 * The provenance thread.
 *
 * The premise of this product is that scores come from data and the model only
 * explains them. That claim is worth nothing if a reader cannot check it, so
 * every answer arrives with the chain of tool calls that produced it: what ran,
 * in what order, what came back, and how long it took.
 *
 * Ordinals are used here because this genuinely is a sequence -- a ReAct loop
 * investigates in order, and step three often exists because step two failed.
 * Failed calls are shown rather than hidden for the same reason: watching the
 * agent hit an unavailable tool and re-route is evidence about the answer.
 */

interface Props {
  trace: AgentTrace[];
  compact?: boolean;
}

/** One line describing what a tool actually returned. */
function summarize(entry: AgentTrace): { text: string; score?: number } {
  const r = entry.result ?? {};
  if (r.error) return { text: String(r.error) };

  switch (entry.tool_name) {
    case 'score_neighborhood': {
      const target = r.target ?? {};
      const scores = r.scores ?? {};
      const place = target.matched_address ?? target.zip ?? 'location resolved';
      const parts = [String(place).toLowerCase()];
      if (target.radius_m) parts.push(`${target.radius_m} m radius`);
      return {
        text: parts.join(' · '),
        score: typeof scores.overall === 'number' ? scores.overall : undefined,
      };
    }
    case 'compare_neighborhoods': {
      const deltas = r.deltas ?? {};
      const overall = deltas.overall;
      const shown = Object.entries(deltas)
        .filter(([k]) => k !== 'overall')
        .slice(0, 3)
        .map(([k, v]) => `${k} ${(v as number) > 0 ? '+' : ''}${v}`)
        .join(' · ');
      return {
        text: shown || 'two locations compared',
        score: typeof overall === 'number' ? 50 + overall : undefined,
      };
    }
    case 'query_dataset': {
      const total = r.total ?? 0;
      const rows = Array.isArray(r.rows) ? r.rows.length : 0;
      return { text: `${total.toLocaleString()} rows matched · ${rows} returned` };
    }
    case 'find_similar_neighborhoods': {
      const n = Array.isArray(r.neighbors) ? r.neighbors.length : 0;
      return { text: `${n} comparable ${n === 1 ? 'area' : 'areas'}` };
    }
    case 'walking_isochrone': {
      const p = r.properties ?? {};
      const km2 = typeof p.area_m2 === 'number' ? (p.area_m2 / 1e6).toFixed(2) : null;
      const bits = [];
      if (km2) bits.push(`${km2} km² reachable`);
      if (p.minutes) bits.push(`${p.minutes} min walk`);
      if (p.reachable_nodes) bits.push(`${p.reachable_nodes.toLocaleString()} street nodes`);
      return { text: bits.join(' · ') || 'isochrone computed' };
    }
    case 'simulate_intervention': {
      const d = r.deltas ?? {};
      const iv = r.intervention ?? {};
      const overall = d.overall;
      const label = `${iv.count ?? '?'} × ${String(iv.type ?? 'asset').replace(/_/g, ' ')}`;
      const move =
        typeof overall === 'number'
          ? `overall ${overall > 0 ? '+' : ''}${overall}`
          : 'projected';
      return { text: `${label} · ${move}` };
    }
    case 'search_address': {
      const n = Array.isArray(r.results) ? r.results.length : 0;
      return { text: `${n} address ${n === 1 ? 'match' : 'matches'}` };
    }
    default:
      return { text: 'result captured' };
  }
}

function scoreColor(score?: number): string | undefined {
  if (typeof score !== 'number') return undefined;
  if (score < 40) return 'var(--ud-low)';
  if (score < 70) return 'var(--ud-mid)';
  return 'var(--ud-high)';
}

export default function ProvenanceThread({ trace, compact = false }: Props) {
  const [openIndex, setOpenIndex] = useState<number | null>(null);

  if (!trace?.length) return null;

  const totalMs = trace.reduce((sum, t) => sum + (t.latency_ms ?? 0), 0);

  return (
    <section className="mt-3" aria-label="How this answer was produced">
      <header className="flex items-baseline justify-between gap-3 mb-2">
        <h3 className="ud-label">How this was answered</h3>
        <span className="font-mono text-[10px] text-muted-foreground">
          {trace.length} {trace.length === 1 ? 'call' : 'calls'} · {totalMs.toLocaleString()} ms
        </span>
      </header>

      <ol className="relative pl-5">
        {/* The thread itself. */}
        <span
          aria-hidden="true"
          className="ud-thread-line absolute left-[3px] top-0 bottom-0 w-px"
        />

        {trace.map((entry, i) => {
          const { text, score } = summarize(entry);
          const failed = Boolean(entry.result?.error);
          const isOpen = openIndex === i;
          const colour = scoreColor(score);

          return (
            <li key={`${entry.tool_name}-${i}`} className="relative pb-3 last:pb-0">
              {/* Node marker. A failed call is drawn hollow. */}
              <span
                aria-hidden="true"
                className="absolute -left-5 top-[5px] w-[7px] h-[7px] rounded-full border"
                style={{
                  background: failed ? 'transparent' : 'var(--ud-ink)',
                  borderColor: failed ? 'var(--ud-thread)' : 'var(--ud-ink)',
                }}
              />

              <button
                type="button"
                onClick={() => setOpenIndex(isOpen ? null : i)}
                aria-expanded={isOpen}
                className="ud-thread-node group w-full text-left rounded-sm px-1 -mx-1 py-0.5 transition-colors hover:bg-muted focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
              >
                <span className="flex items-baseline gap-2">
                  <span className="font-mono text-[10px] text-muted-foreground tabular-nums">
                    {String(i + 1).padStart(2, '0')}
                  </span>
                  <span className="font-mono text-[11px] font-medium text-foreground truncate">
                    {entry.tool_name}
                  </span>
                  <ChevronRight
                    className={`w-3 h-3 text-muted-foreground/60 shrink-0 transition-transform motion-reduce:transition-none ${
                      isOpen ? 'rotate-90' : ''
                    }`}
                  />
                  <span className="ml-auto font-mono text-[10px] text-muted-foreground tabular-nums shrink-0">
                    {(entry.latency_ms ?? 0).toLocaleString()} ms
                  </span>
                </span>

                <span className="mt-0.5 flex items-baseline gap-2 pl-[1.6rem]">
                  <span
                    className={`text-xs leading-snug ${
                      failed ? 'text-muted-foreground italic' : 'text-muted-foreground'
                    }`}
                  >
                    {text}
                  </span>
                  {typeof score === 'number' && !failed && (
                    <span
                      className="font-mono text-xs font-semibold tabular-nums shrink-0"
                      style={{ color: colour }}
                    >
                      {Math.round(score)}
                    </span>
                  )}
                </span>
              </button>

              {isOpen && (
                <motion.div
                  initial={{ opacity: 0, height: 0 }}
                  animate={{ opacity: 1, height: 'auto' }}
                  className="overflow-hidden"
                >
                  <pre className="mt-1.5 ml-[1.6rem] max-h-40 overflow-auto rounded-sm bg-muted px-2.5 py-2 font-mono text-[10px] leading-relaxed text-muted-foreground">
                    {JSON.stringify(entry.args ?? {}, null, 2)}
                  </pre>
                </motion.div>
              )}
            </li>
          );
        })}
      </ol>

      {!compact && (
        <p className="mt-2.5 text-[11px] leading-relaxed text-muted-foreground">
          Every figure above came from these calls. The model reads the results;
          it does not compute scores.
        </p>
      )}
    </section>
  );
}
