/**
 * The inspector: everything known about one point, in four answers.
 *
 * This panel used to be a single 2,580px scroll -- fourteen blocks at one
 * visual weight, in the order they happened to be built. A reader looking for
 * the score and a reader auditing the sources scrolled the same 2.8 screens,
 * and neither could tell where one question ended and the next began.
 *
 * It is now split along the questions a reader actually asks, in the order
 * they ask them:
 *
 *   Score    what is it            -- fits one screen, no scrolling to the answer
 *   Context  compared with what    -- the city distribution and the pinned point
 *   Signals  what is driving it    -- trends, hotspots, local counts
 *   Sources  where did it come from -- evidence, coverage, reports
 *
 * The split is not only ergonomic. Two context-only indicators (modelled NO2,
 * heat vulnerability) used to sit inside the score grid carrying the caption
 * "0% of overall" -- a caption doing work that layout was actively undoing.
 * They live under Context now, where their weight of zero matches their
 * position.
 */

import type React from 'react';
import { useMemo, useState } from 'react';
import {
  Activity,
  AlertCircle,
  ChevronDown,
  Download,
  FileText,
  Gauge,
  Layers3,
  Loader2,
  ScrollText,
  ShieldCheck,
  Sun,
  TramFront,
  UtensilsCrossed,
  Wind,
} from 'lucide-react';

import VegaChart from './VegaChart';
import { Button } from '@/components/ui/button';
import type { CompareResponse } from '../features/compare/useComparison';
import type {
  DetailPreviewResponse,
  EvidenceEntry,
  PriorityAction,
  RadiusMeters,
  Scores,
} from '../types';

export type InspectorTabId = 'score' | 'context' | 'signals' | 'sources';

const TABS: { id: InspectorTabId; label: string; icon: typeof Gauge; hint: string }[] = [
  { id: 'score', label: 'Score', icon: Gauge, hint: 'What this place scores' },
  { id: 'context', label: 'Context', icon: Layers3, hint: 'How it compares' },
  { id: 'signals', label: 'Signals', icon: Activity, hint: 'What is driving it' },
  { id: 'sources', label: 'Sources', icon: ScrollText, hint: 'Where the numbers come from' },
];

/* The weighted dimensions -- and only the weighted ones.
 *
 * `building` and `environment` are scored, published, and carry
 * `weight_in_overall = 0.0` permanently: building is an independent risk flag
 * (P0-02, resolved 2026-08-12) and environment is context-only. Putting them
 * in this grid would have the layout assert a contribution the model
 * deliberately refuses them, so they get their own blocks that say what they
 * are. What was actually missing before was not a fourth card but a sentence
 * explaining why the composition chart draws five bars and this grid holds
 * three. */
const DIMENSIONS: {
  id: keyof Scores;
  label: string;
  icon: typeof ShieldCheck;
  blurb: string;
}[] = [
  { id: 'safety', label: 'Safety', icon: ShieldCheck, blurb: 'Collisions, EMS and fire response, rodent and sanitation complaints' },
  { id: 'transit', label: 'Transit', icon: TramFront, blurb: 'Subway and bus access, bike routes, open streets' },
  { id: 'amenities', label: 'Amenities', icon: UtensilsCrossed, blurb: 'Parks, trees, restaurants, public facilities' },
];

const RADIUS_OPTIONS: RadiusMeters[] = [200, 500, 1000];

export interface InspectorProps {
  preview: DetailPreviewResponse | null;
  scores: Scores | null | undefined;
  loading: boolean;
  reportLoading: boolean;
  exportLoading: boolean;
  error: string | null;
  selectedRadiusM: RadiusMeters;
  onRadiusChange: (radius: RadiusMeters) => void;
  displayTitle?: string;

  pinnedPreview: DetailPreviewResponse | null;
  pinnedTitle: string;
  comparisonActive: boolean;
  comparisonLoading: boolean;
  comparisonError: string | null;
  serverComparison: CompareResponse | null;
  onClearComparison: () => void;

  reportCache: Record<string, unknown>;
  activeReportMode: string | null;
  onGenerateReport: (mode: 'individual' | 'organization') => void;
  onExportHtml: () => void;
  onOpenMethodology: () => void;

  formatScore: (score: number | null | undefined) => string;
  scoreGradientStyle: (score: number | null | undefined) => React.CSSProperties;
  scoreTextStyle: (score: number | null | undefined) => React.CSSProperties;
  environmentTier: (score: number | null | undefined) => string;
  heatVulnerabilityTier: (score: number | null | undefined) => string;
  summarizeEvidence: (entry: EvidenceEntry) => string;
}

function Section({
  title,
  children,
  note,
}: {
  title: string;
  children: React.ReactNode;
  note?: string;
}) {
  return (
    <section className="space-y-2.5">
      <div className="flex items-baseline justify-between gap-3">
        <h3 className="ud-label">{title}</h3>
        {note && (
          <span className="font-mono text-[10px] text-muted-foreground/70">{note}</span>
        )}
      </div>
      {children}
    </section>
  );
}

/**
 * A disclosure for the second layer of detail.
 *
 * The rule this panel follows: a reader should be able to answer the tab's
 * question without opening anything, and be able to audit that answer by
 * opening exactly one thing. Anything that is neither the answer nor its audit
 * trail does not belong on the tab at all.
 */
function Disclosure({
  summary,
  children,
  count,
}: {
  summary: string;
  children: React.ReactNode;
  count?: number;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="rounded-md border border-border/60 bg-card/50">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center justify-between gap-2 px-3 py-2 text-left text-xs text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
      >
        <span>
          {summary}
          {count != null && (
            <span className="ml-1.5 font-mono tabular-nums text-muted-foreground/70">
              {count}
            </span>
          )}
        </span>
        <ChevronDown
          className={`h-3.5 w-3.5 shrink-0 transition-transform ${open ? 'rotate-180' : ''}`}
        />
      </button>
      {open && <div className="border-t border-border/60 px-3 py-2.5">{children}</div>}
    </div>
  );
}

/**
 * The headline, and the one thing this panel most has to get right.
 *
 * The previous card printed `95% range {a}–{b} · point estimate {c}` on one
 * line, which reads as an interval and its centre. They are not that. The
 * interval comes from the offline sensitivity artifact at h3 r9 grain under
 * the production weighting; the point estimate is computed live for the chosen
 * radius under whatever priority order the reader picked. Different estimand,
 * different grain, different weights -- and on the default priority order the
 * headline lands outside its own stated interval for the majority of points in
 * the city. Six of ten sampled addresses, at the shipped defaults.
 *
 * So they are shown as two readings of two questions, each labelled with what
 * it actually measured, and the disclosure says why they differ.
 */
function OverallCard({
  preview,
  scores,
  formatScore,
  scoreGradientStyle,
  scoreTextStyle,
}: Pick<InspectorProps, 'preview' | 'scores' | 'formatScore' | 'scoreGradientStyle' | 'scoreTextStyle'>) {
  const uncertainty = preview?.score_uncertainty;
  const tier = uncertainty?.public_tier;
  const weights = preview?.priority_profile?.weights;
  const order = preview?.priority_profile?.order ?? [];

  return (
    <div className="space-y-2">
      <div
        className="rounded-md border px-5 py-4"
        style={scoreGradientStyle(scores?.overall)}
      >
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <div className="text-sm font-semibold text-foreground">Citywide tier</div>
            <div className="mt-0.5 text-xs leading-snug text-muted-foreground">
              {tier
                ? 'Production weighting, measured on the h3 cell containing this address'
                : 'Sensitivity artifact unavailable for this cell'}
            </div>
          </div>
          <div className="text-right">
            <span className="ud-display text-3xl leading-none" style={scoreTextStyle(scores?.overall)}>
              {tier?.label ?? 'Not tiered'}
            </span>
            {tier && (
              <div className="mt-1 font-mono text-[10px] tabular-nums text-muted-foreground">
                95% interval {tier.score_range[0]}–{tier.score_range[1]}
              </div>
            )}
          </div>
        </div>
      </div>

      <div className="rounded-md border border-border/60 bg-card/60 px-5 py-3">
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <div className="text-sm font-semibold text-foreground">Your priority order</div>
            <div className="mt-0.5 font-mono text-[10px] leading-snug text-muted-foreground">
              {order.length && weights
                ? order
                    .map((key) => `${key} ${(weights[key] ?? 0).toFixed(2)}`)
                    .join(' · ')
                : 'default weighting'}
            </div>
          </div>
          <span
            className="ud-display text-2xl leading-none tabular-nums"
            style={scoreTextStyle(scores?.overall)}
          >
            {formatScore(scores?.overall)}
          </span>
        </div>
      </div>

      {tier && (
        <Disclosure summary="Why these two numbers can disagree">
          <p className="text-xs leading-relaxed text-muted-foreground">
            They answer different questions. The tier comes from the offline
            sensitivity artifact: production weighting, computed once per{' '}
            <span className="font-mono">{uncertainty?.grain ?? 'cell'}</span> cell over{' '}
            <span className="font-mono tabular-nums">{uncertainty?.draws ?? '?'}</span> draws.
            The number beside your priority order is computed live for the radius
            you selected, re-weighted by the order you chose. Reordering your
            priorities moves the second number and never the first, so the second
            can sit outside the first interval — that is the two readings
            disagreeing honestly, not an error.
          </p>
          {uncertainty?.note && (
            <p className="mt-2 border-t border-border/60 pt-2 font-mono text-[10px] leading-relaxed text-muted-foreground/80">
              {uncertainty.note}
            </p>
          )}
        </Disclosure>
      )}
    </div>
  );
}

function DimensionGrid({
  preview,
  scores,
  formatScore,
  scoreGradientStyle,
  scoreTextStyle,
}: Pick<InspectorProps, 'preview' | 'scores' | 'formatScore' | 'scoreGradientStyle' | 'scoreTextStyle'>) {
  return (
    <div className="grid grid-cols-3 gap-2.5">
      {DIMENSIONS.map(({ id, label, icon: Icon, blurb }) => {
        const value = scores?.[id] as number | null | undefined;
        const coverage = preview?.score_coverage?.[id as string];
        const thin =
          coverage &&
          coverage.available != null &&
          coverage.total != null &&
          coverage.available < coverage.total;
        return (
          <div
            key={id}
            className="rounded-md border px-4 py-3"
            style={scoreGradientStyle(value)}
            title={blurb}
          >
            <div className="flex items-center gap-2.5">
              <Icon className="h-4 w-4 shrink-0 text-muted-foreground" />
              <div className="min-w-0 flex-1">
                <div
                  className="ud-display text-[1.6rem] leading-none tabular-nums"
                  style={scoreTextStyle(value)}
                >
                  {formatScore(value)}
                </div>
                <div className="mt-0.5 truncate text-xs text-muted-foreground">{label}</div>
              </div>
            </div>
            {thin && (
              <div
                className="mt-1.5 font-mono text-[10px] tabular-nums text-amber-700 dark:text-amber-500"
                title={`Missing: ${(coverage?.missing ?? []).join(', ')}`}
              >
                {coverage?.available}/{coverage?.total} sources
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

/**
 * Building: the flag, and the score that is deliberately not a dimension.
 *
 * `weight_in_overall` for building is 0.0 permanently -- it is an independent
 * risk flag rather than a wellbeing dimension. Its 0-100 score is still worth
 * showing for relative reading, so it sits here with the flag it belongs to
 * and says its own weight out loud, instead of standing in the weighted grid
 * where position alone would claim otherwise.
 */
function RiskFlag({
  preview,
  scores,
  formatScore,
}: {
  preview: DetailPreviewResponse | null;
  scores: Scores | null | undefined;
  formatScore: InspectorProps['formatScore'];
}) {
  const flag = preview?.building_risk_flag;
  const buildingScore = scores?.building;
  if (!flag) return null;
  const STYLE: Record<string, { color: string; icon: string; label: string }> = {
    serious: { color: '#d03b3b', icon: '⬢', label: 'Serious building risk' },
    elevated: { color: '#ec835a', icon: '⬢', label: 'Elevated building risk' },
    watch: { color: '#b8860b', icon: '⬡', label: 'Building watch' },
    none: { color: 'var(--muted-foreground, #777)', icon: '○', label: 'No building risk signals' },
    unknown: { color: 'var(--muted-foreground, #777)', icon: '◌', label: 'Building risk: no data' },
  };
  const style = STYLE[flag.level] ?? STYLE.unknown;
  const quiet = flag.level === 'none' || flag.level === 'unknown';
  return (
    <div
      className="rounded-md border px-4 py-3 text-sm"
      style={{ borderColor: quiet ? undefined : style.color }}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <span className="font-semibold" style={{ color: style.color }}>
            {style.icon} {style.label}
          </span>
          {!quiet && (
            <div className="mt-1 text-xs leading-snug text-muted-foreground">
              {flag.reasons.join(' · ')}
            </div>
          )}
        </div>
        {typeof buildingScore === 'number' && (
          <div className="shrink-0 text-right">
            <div className="ud-display text-xl leading-none tabular-nums text-muted-foreground">
              {formatScore(buildingScore)}
            </div>
            <div className="mt-0.5 font-mono text-[9px] uppercase tracking-wide text-muted-foreground/70">
              building · 0% of score
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

/** A context indicator: published, explicitly outside the composite. */
function ContextIndicator({
  icon: Icon,
  tier,
  detail,
  score,
  formatScore,
  scoreGradientStyle,
}: {
  icon: typeof Wind;
  tier: string;
  detail: string;
  score: number | null | undefined;
  formatScore: InspectorProps['formatScore'];
  scoreGradientStyle: InspectorProps['scoreGradientStyle'];
}) {
  return (
    <div
      className="flex items-center gap-3 rounded-md border px-4 py-3"
      style={scoreGradientStyle(score)}
    >
      <Icon className="h-5 w-5 shrink-0 text-muted-foreground" />
      <div className="min-w-0 flex-1">
        <div className="text-sm font-semibold text-foreground">{tier}</div>
        <div className="mt-0.5 text-[11px] leading-snug text-muted-foreground">{detail}</div>
      </div>
      <div className="text-right font-mono text-[10px] text-muted-foreground">
        {formatScore(score)}/100
      </div>
    </div>
  );
}

function StatCard({ value, label }: { value: string; label: React.ReactNode }) {
  return (
    <div className="rounded-md border bg-card px-3 py-2">
      <div className="ud-display text-lg leading-tight tabular-nums">{value}</div>
      <div className="mt-0.5 text-[10px] leading-snug text-muted-foreground">{label}</div>
    </div>
  );
}

export default function Inspector(props: InspectorProps) {
  const {
    preview,
    scores,
    loading,
    reportLoading,
    exportLoading,
    error,
    selectedRadiusM,
    onRadiusChange,
    displayTitle,
    pinnedPreview,
    pinnedTitle,
    comparisonActive,
    comparisonLoading,
    comparisonError,
    serverComparison,
    onClearComparison,
    reportCache,
    activeReportMode,
    onGenerateReport,
    onExportHtml,
    onOpenMethodology,
    formatScore,
    scoreGradientStyle,
    scoreTextStyle,
    environmentTier,
    heatVulnerabilityTier,
    summarizeEvidence,
  } = props;

  const [tab, setTab] = useState<InspectorTabId>('score');
  const [showAllEvidence, setShowAllEvidence] = useState(false);

  const metricScores = preview?.metric_scores;
  const currentState = preview?.current_state as Record<string, any> | undefined;
  const enriched = preview?.enriched_context as Record<string, any> | undefined;
  const whyNow = preview?.why_now ?? [];
  const hotspots = (preview?.detail_items?.hotspots ?? []) as Array<unknown>;
  const buildingFlags = preview?.detail_items?.building_flags ?? [];
  const evidence: EvidenceEntry[] = preview?.evidence_table ?? [];
  const priorityActions: PriorityAction[] = preview?.priority_actions ?? [];
  const dataGaps = preview?.data_gaps ?? [];

  const nearestParks = enriched?.nearest_parks as string[] | undefined;
  const treeHealth = enriched?.tree_health as Record<string, number> | undefined;
  const violationAge = enriched?.violation_age as Record<string, number> | undefined;

  /* The citywide standing of this cell, from `overview_context`.
     The backend has always sent this block and nothing has ever rendered it,
     so the panel could show a score of 46 without ever saying whether 46 is
     ordinary or unusual for New York. */
  const cityStanding = useMemo(() => {
    const overall = preview?.overview_context?.overall as Record<string, any> | undefined;
    const dist = overall?.distribution as
      | { bins?: Array<{ bin_start: number; bin_end: number; count: number }>; population_n?: number }
      | undefined;
    const score = typeof overall?.score === 'number' ? overall.score : null;
    if (score == null || !dist?.bins?.length) return null;
    const total = dist.bins.reduce((sum, b) => sum + (b.count ?? 0), 0);
    if (!total) return null;
    const below = dist.bins
      .filter((b) => b.bin_end <= score)
      .reduce((sum, b) => sum + (b.count ?? 0), 0);
    return {
      score,
      level: typeof overall?.level === 'string' ? overall.level : null,
      percentile: Math.round((below / total) * 100),
      population: dist.population_n ?? total,
    };
  }, [preview?.overview_context]);

  const visibleEvidence = showAllEvidence ? evidence : evidence.slice(0, 5);

  const tabPanelId = (id: InspectorTabId) => `inspector-panel-${id}`;

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {/* Tabs. Arrow keys move between them, as a tablist should. */}
      <div
        role="tablist"
        aria-label="Inspector sections"
        className="flex shrink-0 gap-1 border-b border-border/60 px-4 pb-2 pt-1"
        onKeyDown={(event) => {
          const index = TABS.findIndex((t) => t.id === tab);
          if (event.key === 'ArrowRight') {
            event.preventDefault();
            setTab(TABS[(index + 1) % TABS.length].id);
          } else if (event.key === 'ArrowLeft') {
            event.preventDefault();
            setTab(TABS[(index - 1 + TABS.length) % TABS.length].id);
          }
        }}
      >
        {TABS.map(({ id, label, icon: Icon, hint }) => {
          const active = tab === id;
          return (
            <button
              key={id}
              role="tab"
              type="button"
              id={`inspector-tab-${id}`}
              aria-selected={active}
              aria-controls={tabPanelId(id)}
              tabIndex={active ? 0 : -1}
              title={hint}
              onClick={() => setTab(id)}
              className={`flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-medium transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring ${
                active
                  ? 'bg-foreground text-background'
                  : 'text-muted-foreground hover:bg-muted hover:text-foreground'
              }`}
            >
              <Icon className="h-3.5 w-3.5" />
              {label}
            </button>
          );
        })}
      </div>

      <div
        role="tabpanel"
        id={tabPanelId(tab)}
        aria-labelledby={`inspector-tab-${tab}`}
        className="min-h-0 flex-1 overflow-y-auto p-5"
      >
        {error && (
          <div className="mb-4 flex items-start gap-2 rounded-md border border-destructive/30 bg-destructive/5 px-4 py-3 text-sm text-destructive">
            <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
            <span>{error}</span>
          </div>
        )}

        {/* ---------------------------------------------------------- SCORE */}
        {tab === 'score' && (
          <div className="space-y-5">
            <div className="flex items-center justify-between">
              <span className="ud-label">Measured within</span>
              <div
                className="flex gap-0.5 rounded-md border border-border bg-muted/40 p-0.5"
                role="group"
                aria-label="Analysis radius"
              >
                {RADIUS_OPTIONS.map((radius) => (
                  <button
                    key={radius}
                    type="button"
                    onClick={() => onRadiusChange(radius)}
                    aria-pressed={selectedRadiusM === radius}
                    className={`rounded-[4px] px-2.5 py-1 font-mono text-[11px] tabular-nums transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring ${
                      selectedRadiusM === radius
                        ? 'bg-foreground text-background'
                        : 'text-muted-foreground hover:text-foreground'
                    }`}
                  >
                    {radius} m
                  </button>
                ))}
              </div>
            </div>

            <OverallCard
              preview={preview}
              scores={scores}
              formatScore={formatScore}
              scoreGradientStyle={scoreGradientStyle}
              scoreTextStyle={scoreTextStyle}
            />

            <Section title="Weighted dimensions" note="these three make the score">
              <DimensionGrid
                preview={preview}
                scores={scores}
                formatScore={formatScore}
                scoreGradientStyle={scoreGradientStyle}
                scoreTextStyle={scoreTextStyle}
              />
              <p className="text-[11px] leading-snug text-muted-foreground">
                Building and environment are scored too, and carry no weight by
                decision — a flag and a context reading, not wellbeing
                dimensions. That is why the composition chart under Context
                draws five bars and this grid holds three.
              </p>
            </Section>

            <RiskFlag preview={preview} scores={scores} formatScore={formatScore} />

            <div className="flex items-center justify-between border-t border-border/50 pt-3 font-mono text-[10px] text-muted-foreground/70">
              <span>
                {preview?.score_uncertainty
                  ? `methodology v${preview.score_uncertainty.methodology_version}`
                  : 'methodology version unavailable'}
              </span>
              <button
                type="button"
                onClick={onOpenMethodology}
                className="underline decoration-dotted underline-offset-2 hover:text-foreground"
              >
                how scores work
              </button>
            </div>
          </div>
        )}

        {/* -------------------------------------------------------- CONTEXT */}
        {tab === 'context' && (
          <div className="space-y-5">
            {cityStanding && (
              <Section title="Standing in the city">
                <div className="rounded-md border bg-card px-4 py-3">
                  <div className="flex items-baseline justify-between gap-3">
                    <span className="ud-display text-2xl leading-none tabular-nums">
                      {cityStanding.percentile}
                      <span className="ml-1 text-sm text-muted-foreground">th percentile</span>
                    </span>
                    <span className="font-mono text-[10px] text-muted-foreground">
                      of {cityStanding.population.toLocaleString()} cells
                    </span>
                  </div>
                  <p className="mt-1.5 text-xs leading-snug text-muted-foreground">
                    This cell scores{' '}
                    <span className="font-mono tabular-nums">{cityStanding.score}</span>
                    {cityStanding.level ? ` (${cityStanding.level})` : ''}. Most of New York
                    falls in a narrow band, so a mid-range score is typical rather than
                    unremarkable — the distribution below shows how narrow.
                  </p>
                </div>
              </Section>
            )}

            {pinnedPreview && preview && comparisonActive && (
              <Section title="Compared with pinned">
                <div className="space-y-3 rounded-md border border-primary/30 bg-primary/5 p-4">
                  <div className="flex items-center justify-between">
                    <span className="truncate text-xs text-muted-foreground" title={pinnedTitle}>
                      {pinnedTitle} → {displayTitle}
                    </span>
                    <button
                      type="button"
                      onClick={onClearComparison}
                      className="text-xs text-muted-foreground hover:text-foreground"
                    >
                      Clear pin
                    </button>
                  </div>
                  {comparisonLoading && (
                    <p role="status" aria-live="polite" className="text-xs text-muted-foreground">
                      Updating comparison…
                    </p>
                  )}
                  {comparisonError && (
                    <p role="alert" className="text-xs text-destructive">
                      Live comparison unavailable; showing the two loaded snapshots.
                    </p>
                  )}
                  {(['overall', 'safety', 'transit', 'amenities'] as const).map((category) => {
                    const a =
                      serverComparison?.point_a.scores?.[category] ??
                      pinnedPreview.scores?.[category];
                    const b =
                      serverComparison?.point_b.scores?.[category] ?? preview.scores?.[category];
                    const rawDelta = serverComparison?.deltas?.[category];
                    const delta = typeof rawDelta === 'number' ? Math.round(rawDelta) : null;
                    const stops = serverComparison?.delta_map?.presentation.stops ?? [];
                    const negative = stops[0]?.color ?? '#e66101';
                    const positive = stops[stops.length - 1]?.color ?? '#5e3c99';
                    return (
                      <div
                        key={category}
                        className="grid grid-cols-3 gap-2 text-center text-sm tabular-nums"
                      >
                        <span className="font-bold" style={scoreTextStyle(a ?? null)}>
                          {a ?? '--'}
                        </span>
                        <span className="self-center text-xs capitalize text-muted-foreground">
                          {category}
                        </span>
                        <span className="font-bold" style={scoreTextStyle(b ?? null)}>
                          {b ?? '--'}
                          {delta != null && delta !== 0 && (
                            <span
                              className="ml-1 text-[10px]"
                              style={{ color: delta > 0 ? positive : negative }}
                            >
                              {delta > 0 ? '+' : ''}
                              {delta}
                            </span>
                          )}
                        </span>
                      </div>
                    );
                  })}
                  {/* The legend for the layer the comparison paints on the
                      map. Without it the delta colours on screen have nothing
                      that decodes them. */}
                  {serverComparison?.delta_map && (
                    <div
                      className="space-y-1.5 border-t border-primary/15 pt-2"
                      aria-label="Comparison delta map legend"
                    >
                      <div className="flex items-center justify-between font-mono text-[10px] text-muted-foreground">
                        <span>Map · B − A</span>
                        <span>{serverComparison.delta_map.presentation.palette}</span>
                      </div>
                      <div className="flex h-2 overflow-hidden rounded-full border border-black/5">
                        {serverComparison.delta_map.presentation.stops.map((stop) => (
                          <span
                            key={stop.value}
                            className="flex-1"
                            style={{ backgroundColor: stop.color }}
                            title={`${stop.value > 0 ? '+' : ''}${stop.value}`}
                          />
                        ))}
                      </div>
                      <div className="flex justify-between font-mono text-[9px] text-muted-foreground/80">
                        <span>B lower</span>
                        <span>same</span>
                        <span>B higher</span>
                      </div>
                    </div>
                  )}

                  {serverComparison?.chart_specs?.compare_scores && (
                    <VegaChart chart={serverComparison.chart_specs.compare_scores} />
                  )}
                </div>
              </Section>
            )}

            {preview?.chart_specs?.score_composition && (
              <VegaChart chart={preview.chart_specs.score_composition} />
            )}

            {preview?.chart_specs?.score_distribution && (
              <VegaChart chart={preview.chart_specs.score_distribution} />
            )}

            {/* Context-only indicators. Published, and carrying no weight --
                which is why they are here and not in the score grid. */}
            {(typeof (metricScores?.nyccas_no ?? scores?.environment) === 'number' ||
              typeof metricScores?.heat_vulnerability === 'number') && (
              <Section title="Context only" note="0% of the score">
                <div className="space-y-2">
                  {typeof (metricScores?.nyccas_no ?? scores?.environment) === 'number' && (
                    <ContextIndicator
                      icon={Wind}
                      tier={environmentTier(metricScores?.nyccas_no ?? scores?.environment)}
                      detail="NYCCAS 2023–24 annual-average model"
                      score={metricScores?.nyccas_no ?? scores?.environment}
                      formatScore={formatScore}
                      scoreGradientStyle={scoreGradientStyle}
                    />
                  )}
                  {typeof metricScores?.heat_vulnerability === 'number' && (
                    <ContextIndicator
                      icon={Sun}
                      tier={heatVulnerabilityTier(metricScores.heat_vulnerability)}
                      detail="NYC DOHMH mortality-risk quintile · ZCTA 2020"
                      score={metricScores.heat_vulnerability}
                      formatScore={formatScore}
                      scoreGradientStyle={scoreGradientStyle}
                    />
                  )}
                </div>
              </Section>
            )}
          </div>
        )}

        {/* -------------------------------------------------------- SIGNALS */}
        {tab === 'signals' && (
          <div className="space-y-5">
            {whyNow.length > 0 && (
              <div className="rounded-md border border-amber-300/50 bg-amber-50/50 px-4 py-3 dark:bg-amber-950/20">
                <div className="mb-1 text-xs font-semibold text-amber-700 dark:text-amber-400">
                  Trend alert
                </div>
                {/* `signal` already reads "collision is worsening (last 30
                    days 14 vs previous 30 days 9 (+55.6%))", so appending
                    trend_type restated the direction the sentence had just
                    given. It is kept only for `pattern`, where it says
                    something the sentence does not. */}
                {whyNow.map((item, index) => (
                  <div key={index} className="text-xs leading-snug text-amber-800 dark:text-amber-300">
                    {item.signal}
                    {item.trend_type === 'pattern' && (
                      <span className="ml-1.5 font-mono text-[10px] uppercase opacity-70">
                        pattern
                      </span>
                    )}
                  </div>
                ))}
              </div>
            )}

            {hotspots.length > 0 && (
              <div className="rounded-md border border-red-300/50 bg-red-50/50 px-4 py-3 dark:bg-red-950/20">
                <div className="text-xs font-semibold text-red-700 dark:text-red-400">
                  {hotspots.length} incident hotspot{hotspots.length > 1 ? 's' : ''} detected
                </div>
                <div className="mt-0.5 text-[10px] text-red-600 dark:text-red-300">
                  Spatial clustering of incidents within the analysis radius
                </div>
              </div>
            )}

            <Section title="Local counts" note={`within ${selectedRadiusM} m`}>
              <div className="grid grid-cols-2 gap-2">
                {currentState?.safety?.ems_avg_response_seconds != null && (
                  <StatCard
                    value={`${Math.round(currentState.safety.ems_avg_response_seconds / 60)}m ${Math.round(
                      currentState.safety.ems_avg_response_seconds % 60,
                    )}s`}
                    label="EMS response, average"
                  />
                )}
                {currentState?.safety?.fire_avg_response_seconds != null && (
                  <StatCard
                    value={`${Math.round(currentState.safety.fire_avg_response_seconds / 60)}m ${Math.round(
                      currentState.safety.fire_avg_response_seconds % 60,
                    )}s`}
                    label="Fire response, average"
                  />
                )}
                {currentState?.amenities?.tree_count_500m != null && (
                  <StatCard
                    value={String(currentState.amenities.tree_count_500m)}
                    label={
                      <>
                        Street trees
                        {treeHealth
                          ? ` (${Object.entries(treeHealth)
                              .map(([key, value]) => `${value} ${key.toLowerCase()}`)
                              .join(', ')})`
                          : ''}
                      </>
                    }
                  />
                )}
                {currentState?.amenities?.park_acres_zip_proxy != null &&
                  currentState.amenities.park_acres_zip_proxy > 0 && (
                    <StatCard
                      value={`${currentState.amenities.park_acres_zip_proxy} ac`}
                      label={`Park area${
                        nearestParks?.length ? `: ${nearestParks.slice(0, 2).join(', ')}` : ''
                      }`}
                    />
                  )}
                {currentState?.amenities?.restaurant_count_500m != null &&
                  currentState.amenities.restaurant_count_500m > 0 && (
                    <StatCard
                      value={String(currentState.amenities.restaurant_count_500m)}
                      label={`Restaurants${
                        currentState.amenities.restaurant_critical_rate_500m > 0
                          ? ` (${Math.round(
                              currentState.amenities.restaurant_critical_rate_500m * 100,
                            )}% critical)`
                          : ''
                      }`}
                    />
                  )}
                {violationAge?.avg_age_days != null && violationAge.avg_age_days > 0 && (
                  <StatCard
                    value={`${Math.round(violationAge.avg_age_days / 365)}yr`}
                    /* Written as a JSX expression, not a template string: the
                       previous spelling put the literal characters &gt; inside
                       a JS template literal, where JSX does no entity decoding,
                       and shipped "96 &gt;2yr" to the page. */
                    label={
                      <>
                        Average violation age
                        {violationAge.older_than_2yr
                          ? ` (${violationAge.older_than_2yr} over 2yr)`
                          : ''}
                      </>
                    }
                  />
                )}
              </div>
            </Section>

            {preview?.chart_specs?.recent_trends && (
              <VegaChart chart={preview.chart_specs.recent_trends} />
            )}

            {priorityActions.length > 0 && (
              <Section title="Priority actions">
                <div className="space-y-2">
                  {priorityActions.slice(0, 3).map((action) => (
                    <div
                      key={`${action.signal}-${action.rank}`}
                      className="rounded-md border border-border/50 bg-card px-4 py-3"
                    >
                      <div className="flex items-baseline gap-2">
                        <span className="font-mono text-xs font-bold text-primary">
                          #{action.rank}
                        </span>
                        <span className="text-sm font-medium">{action.action}</span>
                      </div>
                      <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
                        {action.signal_description}
                      </p>
                    </div>
                  ))}
                </div>
              </Section>
            )}

            {buildingFlags.length > 0 && (
              <Section title="Building signals" note={`${buildingFlags.length} within radius`}>
                <Disclosure summary="Individual records" count={buildingFlags.length}>
                  <div className="space-y-1.5">
                    {buildingFlags.map((flag, index) => (
                      <div
                        key={`${flag.bbl ?? 'building'}-${index}`}
                        className="flex items-baseline justify-between gap-2 text-xs"
                      >
                        <span>{flag.summary}</span>
                        {flag.severity && (
                          <span className="shrink-0 font-mono text-[10px] uppercase text-muted-foreground">
                            {flag.severity}
                          </span>
                        )}
                      </div>
                    ))}
                  </div>
                </Disclosure>
              </Section>
            )}
          </div>
        )}

        {/* -------------------------------------------------------- SOURCES */}
        {tab === 'sources' && (
          <div className="space-y-5">
            <Section
              title="Evidence"
              note={
                evidence.length > visibleEvidence.length
                  ? `showing ${visibleEvidence.length} of ${evidence.length}`
                  : `${evidence.length} record${evidence.length === 1 ? '' : 's'}`
              }
            >
              <div className="space-y-2">
                {visibleEvidence.map((entry) => (
                  <div
                    key={entry.evidence_id}
                    className="rounded-md border border-border/50 bg-card px-4 py-2.5"
                  >
                    <div className="flex justify-between gap-2 text-xs">
                      <span className="font-medium text-primary">{entry.source}</span>
                      <span className="shrink-0 font-mono text-[10px] text-muted-foreground">
                        {entry.date}
                      </span>
                    </div>
                    <p className="mt-1 text-xs leading-snug text-muted-foreground">
                      {summarizeEvidence(entry)}
                    </p>
                  </div>
                ))}
                {evidence.length === 0 && (
                  <p className="text-xs text-muted-foreground">
                    No evidence records for this radius.
                  </p>
                )}
              </div>
              {evidence.length > 5 && (
                <button
                  type="button"
                  onClick={() => setShowAllEvidence((v) => !v)}
                  className="w-full rounded-md border border-border/60 py-1.5 text-xs text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
                >
                  {showAllEvidence ? 'Show fewer' : `Show all ${evidence.length}`}
                </button>
              )}
            </Section>

            {dataGaps.length > 0 && (
              <Section title="Declared gaps" note="absent, not zero">
                <ul className="space-y-1">
                  {dataGaps.map((gap) => (
                    <li key={gap} className="text-xs leading-snug text-muted-foreground">
                      · {gap}
                    </li>
                  ))}
                </ul>
              </Section>
            )}

            <Section title="Generate report">
              <div className="flex items-center gap-2">
                <Button
                  type="button"
                  onClick={() => onGenerateReport('individual')}
                  disabled={reportLoading || loading || !preview}
                  variant={activeReportMode === 'individual' ? 'default' : 'outline'}
                  size="sm"
                  className="h-9 flex-1 rounded-md text-xs font-medium"
                >
                  <FileText className="mr-1.5 h-3.5 w-3.5" />
                  {reportCache['individual'] ? 'View individual' : 'Individual'}
                </Button>
                <Button
                  type="button"
                  onClick={() => onGenerateReport('organization')}
                  disabled={reportLoading || loading || !preview}
                  variant={activeReportMode === 'organization' ? 'default' : 'outline'}
                  size="sm"
                  className="h-9 flex-1 rounded-md text-xs font-medium"
                >
                  <FileText className="mr-1.5 h-3.5 w-3.5" />
                  {reportCache['organization'] ? 'View organization' : 'Organization'}
                </Button>
              </div>
              <Button
                type="button"
                onClick={onExportHtml}
                disabled={exportLoading || loading || !preview?.chart_specs}
                variant="outline"
                size="sm"
                className="h-9 w-full rounded-md text-xs font-medium"
                aria-label="Download offline HTML report"
              >
                {exportLoading ? (
                  <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
                ) : (
                  <Download className="mr-1.5 h-3.5 w-3.5" />
                )}
                Download offline HTML
              </Button>
            </Section>

            <div className="border-t border-border/50 pt-3 font-mono text-[10px] leading-relaxed text-muted-foreground/70">
              {preview?.score_uncertainty
                ? `artifact ${preview.score_uncertainty.artifact_version.slice(0, 12)} · generated ${preview.score_uncertainty.artifact_generated}`
                : 'no sensitivity artifact for this cell'}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
