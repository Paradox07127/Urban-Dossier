import { useEffect, useRef, useState } from 'react';
import { Box, Layers, Pause, Play, Search, Shield, TramFront, UtensilsCrossed, X } from 'lucide-react';
import {
  classBreaks,
  classColor,
  classColors,
  type ScoreDomain,
} from '../lib/scoreClasses';
import type { BivariatePresentation, TimelinePresentation } from '../types';

export type RenderTag = 'general' | 'safety' | 'transit' | 'amenities';
export type ColourDomain = ScoreDomain;

interface Props {
  tag: RenderTag;
  onTagChange: (tag: RenderTag) => void;
  sandbox: boolean;
  sandboxAvailable: boolean;
  onSandboxChange: (on: boolean) => void;
  domains: Record<string, ColourDomain>;
  bivariate: boolean;
  bivariatePresentation?: BivariatePresentation | null;
  onBivariateChange: (enabled: boolean) => void;
  timeline: boolean;
  timelinePresentation?: TimelinePresentation | null;
  timelinePeriod?: string | null;
  timelinePlaying: boolean;
  onTimelineChange: (enabled: boolean) => void;
  onTimelinePeriodChange: (period: string) => void;
  onTimelinePlayingChange: (playing: boolean) => void;
  onSearch: (query: string) => void;
  onResetView: () => void;
  searchError?: string | null;
}

/* The four lenses.
 *
 * "Overall" is named rather than left as the unlabelled default: it is a
 * weighted blend of the other three and a reader should be able to see that it
 * is a choice among four, not the absence of one. The order runs from the
 * composite down to its parts. */
const LENSES: { tag: RenderTag; label: string; icon: typeof Shield }[] = [
  { tag: 'general', label: 'Overall', icon: Layers },
  { tag: 'safety', label: 'Safety', icon: Shield },
  { tag: 'transit', label: 'Transit', icon: TramFront },
  { tag: 'amenities', label: 'Amenities', icon: UtensilsCrossed },
];

const TAG_FIELD: Record<RenderTag, string> = {
  general: 'overall',
  safety: 'safety',
  transit: 'transit',
  amenities: 'amenities',
};

// Class colours and breaks come from the same module the map paints with, so
// a histogram bar is exactly the colour those buildings are on screen.

/**
 * The distribution of the active lens across the city.
 *
 * This replaces the gradient bar a map normally carries. A gradient bar says
 * what the colours mean; it does not say how much of the city is any of them,
 * and for these scores that omission matters more than the scale itself.
 * Overall sits between 34 and 66 for 96% of buildings, so a reader shown an
 * even 0-100 ramp assumes a spread the city does not have and reads a
 * mid-range block as unremarkable when it is in fact typical.
 *
 * Drawing the real histogram in the ramp's own colours makes the bunching the
 * first thing you see, and explains at a glance why the ends are labelled with
 * percentiles rather than 0 and 100.
 */
function DistributionStrip({
  domain,
}: {
  domain: ColourDomain | undefined;
}) {
  const hist = domain?.histogram;
  const colors = classColors(domain);
  if (!domain || !hist || hist.length === 0) {
    return (
      <div className="px-3 pb-3 pt-2">
        <div className="flex h-2 w-full gap-[2px]">
          {colors.map((colour) => (
            <div key={colour} className="flex-1 rounded-[1px]" style={{ background: colour }} />
          ))}
        </div>
        <div className="mt-1.5 flex justify-between font-mono text-[10px] tabular-nums text-muted-foreground">
          <span>0</span>
          <span>100</span>
        </div>
        <div className="mt-0.5 text-center font-mono text-[9px] text-muted-foreground/60">
          fixed bands · presentation contract unavailable
        </div>
      </div>
    );
  }

  const peak = Math.max(...hist, 1);
  const total = hist.reduce((a, b) => a + b, 0);
  const breaks = classBreaks(domain);

  return (
    <div className="px-3 pb-3 pt-2">
      <div className="ud-label mb-2 flex items-baseline justify-between">
        <span>Distribution</span>
        <span className="font-mono text-[9px] normal-case tracking-normal text-muted-foreground/70">
          {(total / 1000).toFixed(0)}k buildings
        </span>
      </div>

      <div
        className="flex h-11 items-end gap-[1.5px]"
        role="img"
        aria-label={
          `Score distribution across ${total.toLocaleString()} buildings. ` +
          `The middle 96% falls between ${domain.low} and ${domain.high}.`
        }
      >
        {hist.map((count, i) => {
          const bucketMid = i * 5 + 2.5;
          return (
            <div
              key={i}
              className="flex-1 rounded-[1px] transition-[height] duration-300"
              style={{
                height: `${Math.max(count > 0 ? 8 : 2, (count / peak) * 100)}%`,
                background: count > 0 ? classColor(bucketMid, breaks, colors) : 'var(--ud-rule)',
              }}
            />
          );
        })}
      </div>

      {/* The ends are percentiles, not 0 and 100: the ramp is stretched over
          the range the data occupies, and saying otherwise would claim a span
          the colours do not cover. */}
      {/* Histogram-derived class edges; tied/bucketed values need not contain
          exactly one fifth of the buildings. */}
      <div className="mt-1.5 flex items-baseline justify-between font-mono text-[10px] tabular-nums text-muted-foreground">
        <span>{domain.low}</span>
        <span className="text-muted-foreground/70">{breaks.join(' · ')}</span>
        <span>{domain.high}</span>
      </div>
      <div className="mt-0.5 text-center font-mono text-[9px] text-muted-foreground/60">
        server quantiles · {domain.population?.replaceAll('_', ' ') ?? 'H3 population'}
      </div>
    </div>
  );
}

export default function InstrumentRail({
  tag,
  onTagChange,
  sandbox,
  sandboxAvailable,
  onSandboxChange,
  domains,
  bivariate,
  bivariatePresentation,
  onBivariateChange,
  timeline,
  timelinePresentation,
  timelinePeriod,
  timelinePlaying,
  onTimelineChange,
  onTimelinePeriodChange,
  onTimelinePlayingChange,
  onSearch,
  onResetView,
  searchError,
}: Props) {
  const [searchOpen, setSearchOpen] = useState(false);
  const [query, setQuery] = useState('');
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (searchOpen) inputRef.current?.focus();
  }, [searchOpen]);

  const domain = domains[TAG_FIELD[tag]];

  return (
    <div className="pointer-events-none absolute left-4 top-4 bottom-4 z-30 flex w-[248px] flex-col gap-3">
      {/* --- Find ------------------------------------------------------- */}
      <div className="pointer-events-auto rounded-xl border border-border bg-background/95 shadow-lg backdrop-blur-md">
        {searchOpen ? (
          <div className="p-2">
            <div className="flex items-center gap-1.5">
              <Search className="ml-1.5 h-4 w-4 shrink-0 text-muted-foreground" />
              <input
                ref={inputRef}
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') onSearch(query);
                  if (e.key === 'Escape') setSearchOpen(false);
                }}
                placeholder="Address or lat, lng"
                aria-label="Find a place in New York"
                className="h-8 min-w-0 flex-1 bg-transparent text-sm outline-none placeholder:text-muted-foreground"
              />
              <button
                type="button"
                onClick={() => setSearchOpen(false)}
                aria-label="Close search"
                className="rounded-md p-1 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            </div>
            {searchError && (
              <p className="px-1.5 pb-1 pt-1.5 text-xs text-destructive">{searchError}</p>
            )}
          </div>
        ) : (
          <div className="flex items-center gap-1 p-2">
            <button
              type="button"
              onClick={() => setSearchOpen(true)}
              className="flex h-8 flex-1 items-center gap-2 rounded-md px-2 text-left text-sm text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
            >
              <Search className="h-4 w-4" />
              Find a place
            </button>
            <button
              type="button"
              onClick={onResetView}
              title="Show all of New York"
              aria-label="Show all of New York"
              className="rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
            >
              <Layers className="h-4 w-4" />
            </button>
          </div>
        )}
      </div>

      {/* --- View ------------------------------------------------------- */}
      <div className="pointer-events-auto overflow-hidden rounded-xl border border-border bg-background/95 shadow-lg backdrop-blur-md">
        {sandboxAvailable && (
          <div className="border-b border-border p-2">
            {/* Two ways of looking at the same city, so they are one control
                with two states rather than a switch that has to be decoded. */}
            <div className="grid grid-cols-2 gap-1" role="group" aria-label="View">
              {([false, true] as const).map((isSandbox) => (
                <button
                  key={String(isSandbox)}
                  type="button"
                  onClick={() => onSandboxChange(isSandbox)}
                  aria-pressed={sandbox === isSandbox}
                  className={`flex items-center justify-center gap-1.5 rounded-md px-2 py-1.5 text-[13px] font-medium transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring ${
                    sandbox === isSandbox
                      ? 'bg-foreground text-background'
                      : 'text-muted-foreground hover:bg-muted hover:text-foreground'
                  }`}
                >
                  {isSandbox ? <Box className="h-3.5 w-3.5" /> : <Layers className="h-3.5 w-3.5" />}
                  {isSandbox ? 'Model' : 'Map'}
                </button>
              ))}
            </div>
          </div>
        )}

        {/* --- Lens ----------------------------------------------------- */}
        <div className="p-2" role="group" aria-label="Colour the city by">
          <div className="ud-label mb-1.5 px-1">Colour by</div>
          <div className="flex flex-col gap-0.5">
            {LENSES.map(({ tag: t, label, icon: Icon }) => (
              <button
                key={t}
                type="button"
                onClick={() => onTagChange(t)}
                aria-pressed={!bivariate && !timeline && tag === t}
                className={`flex items-center gap-2.5 rounded-md px-2 py-1.5 text-left text-[13px] transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring ${
                  !bivariate && !timeline && tag === t
                    ? 'bg-foreground text-background'
                    : 'text-foreground hover:bg-muted'
                }`}
              >
                <Icon className="h-3.5 w-3.5 shrink-0" />
                {label}
              </button>
            ))}
            {bivariatePresentation && (
              <button
                type="button"
                onClick={() => onBivariateChange(!bivariate)}
                aria-pressed={bivariate}
                aria-label="Safety by Transit bivariate map"
                className={`mt-1 flex items-center gap-2.5 rounded-md border px-2 py-1.5 text-left text-[13px] transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring ${
                  bivariate
                    ? 'border-foreground bg-foreground text-background'
                    : 'border-border text-foreground hover:bg-muted'
                }`}
              >
                <span className="grid h-3.5 w-3.5 grid-cols-2 gap-px overflow-hidden rounded-[2px]">
                  {bivariatePresentation.matrix.flat()
                    .filter((_, index) => [0, 2, 6, 8].includes(index))
                    .map((color, index) => (
                      <span key={`${color}-${index}`} style={{ backgroundColor: color }} />
                    ))}
                </span>
                Safety × Transit
              </button>
            )}
            <button
              type="button"
              onClick={() => onTimelineChange(!timeline)}
              aria-pressed={timeline}
              aria-label="Collision timeline map"
              className={`mt-1 flex items-center gap-2.5 rounded-md border px-2 py-1.5 text-left text-[13px] transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring ${
                timeline
                  ? 'border-foreground bg-foreground text-background'
                  : 'border-border text-foreground hover:bg-muted'
              }`}
            >
              <span className="font-mono text-[10px]">Q→</span>
              Collisions over time
            </button>
          </div>
        </div>

        {/* The legend sits directly under the lens that produces it, so the
            relationship needs no explaining. */}
        <div className="border-t border-border">
          {timeline && timelinePresentation ? (
            <div className="p-3" aria-label="Timeline map controls">
              {(() => {
                const periods = timelinePresentation.periods;
                const activeIndex = Math.max(
                  0,
                  periods.findIndex((item) => item.period === timelinePeriod),
                );
                const active = periods[activeIndex];
                return (
                  <>
                    <div className="flex items-center justify-between gap-2">
                      <div>
                        <div className="ud-label">{timelinePresentation.label}</div>
                        <div className="font-mono text-[11px] tabular-nums">
                          {active?.period ?? timelinePresentation.default_period}
                          {active && !active.period_complete ? ' · partial' : ''}
                        </div>
                      </div>
                      <button
                        type="button"
                        onClick={() => onTimelinePlayingChange(!timelinePlaying)}
                        aria-label={timelinePlaying ? 'Pause timeline' : 'Play timeline'}
                        className="rounded border border-border p-1.5 hover:bg-muted"
                      >
                        {timelinePlaying ? <Pause size={14} /> : <Play size={14} />}
                      </button>
                    </div>
                    <input
                      className="mt-3 w-full accent-foreground"
                      type="range"
                      min={0}
                      max={Math.max(0, periods.length - 1)}
                      value={activeIndex}
                      aria-label="Timeline quarter"
                      onChange={(event) => {
                        onTimelinePlayingChange(false);
                        onTimelinePeriodChange(periods[Number(event.target.value)].period);
                      }}
                    />
                    <div className="mt-1 flex justify-between font-mono text-[9px] text-muted-foreground">
                      <span>{periods[0]?.period}</span>
                      <span>{periods.at(-1)?.period}</span>
                    </div>
                    <div className="mt-3 flex h-2 gap-[2px]">
                      {(active?.colors ?? []).map((color) => (
                        <span key={color} className="flex-1" style={{ backgroundColor: color }} />
                      ))}
                    </div>
                    <div className="mt-1 font-mono text-[9px] text-muted-foreground/70">
                      server quantiles · real period key · {timelinePresentation.cell_count} H3 cells
                    </div>
                  </>
                );
              })()}
            </div>
          ) : bivariate && bivariatePresentation ? (
            <div className="p-3" aria-label="Bivariate map legend">
              <div className="mb-2 flex items-baseline justify-between">
                <span className="ud-label">Transit →</span>
                <span className="font-mono text-[9px] text-muted-foreground">
                  {bivariatePresentation.palette}
                </span>
              </div>
              <div className="flex items-end gap-2">
                <span className="-rotate-90 whitespace-nowrap font-mono text-[9px] text-muted-foreground">
                  Safety →
                </span>
                <div className="grid flex-1 grid-cols-3 gap-1">
                  {bivariatePresentation.matrix.flat().map((color, index) => (
                    <span
                      key={`${color}-${index}`}
                      className="aspect-square rounded-[2px] border border-black/5"
                      style={{ backgroundColor: color }}
                    />
                  ))}
                </div>
              </div>
              <div className="mt-2 font-mono text-[9px] text-muted-foreground/70">
                server 3×3 quantiles · CVD ΔE ≥ {Math.min(
                  ...Object.values(bivariatePresentation.accessibility.minimum_adjacent_delta_e),
                ).toFixed(1)}
              </div>
            </div>
          ) : (
            <DistributionStrip domain={domain} />
          )}
        </div>
      </div>
    </div>
  );
}
