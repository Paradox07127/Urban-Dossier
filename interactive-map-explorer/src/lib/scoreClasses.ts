/**
 * The one source of truth for score classing, shared by the map layers and
 * the legend so a building's colour and its legend swatch cannot drift apart.
 *
 * Five classed steps, red pole (worst) to blue pole (best) with a neutral
 * near-paper midpoint. Diverging because a 0-100 score is polarity data;
 * blue-red rather than the old green-red because green-red is the classic
 * colour-vision failure pair. Classed rather than continuous because 93% of
 * scores bunch between 35 and 65, where a continuous ramp hands adjacent
 * places imperceptible tints; quantile classes guarantee every step is a
 * visible step.
 *
 * Validated with the dataviz palette checker rather than eyeballed:
 * worst adjacent-pair CVD dE 14.7 (target >= 8), worst normal-vision dE 16.5
 * (floor 15), lightness monotonic per arm. The midpoint's low contrast
 * against the paper basemap invokes the relief rule, satisfied by the
 * always-visible legend and the click-through detail panel.
 */

export const CLASS_COLORS = [
  '#b23230', // worst fifth
  '#dc8f80',
  '#d8d4cc', // typical fifth -- deliberately recedes
  '#7fa8d9',
  '#2a78d6', // best fifth
] as const;

export type ClassBreaks = [number, number, number, number];

export interface ScoreDomain {
  low: number;
  mid: number;
  high: number;
  /** Twenty five-point buckets across 0-100, from the scoring pass. */
  histogram?: number[];
}

/**
 * Equal-population breaks from the server's measured histogram.
 *
 * The histogram is server data (twenty 5-point buckets over every scored
 * building), so the breaks stay server-defined in spirit even though the
 * cumulative walk happens client-side. Falls back to an even split of the
 * served percentile span when no histogram arrived.
 */
export function classBreaks(domain: ScoreDomain): ClassBreaks {
  const hist = domain.histogram;
  if (hist && hist.length === 20) {
    const total = hist.reduce((a, b) => a + b, 0);
    if (total > 0) {
      const breaks: number[] = [];
      let cum = 0;
      let target = 0.2;
      for (let i = 0; i < 20 && breaks.length < 4; i += 1) {
        cum += hist[i];
        while (breaks.length < 4 && cum / total >= target) {
          breaks.push((i + 1) * 5);
          target += 0.2;
        }
      }
      while (breaks.length < 4) breaks.push(100);
      // Bunched data can land two quantiles in one bucket; nudge duplicates
      // apart so a MapLibre step expression stays strictly ascending.
      for (let i = 1; i < 4; i += 1) {
        if (breaks[i] <= breaks[i - 1]) breaks[i] = breaks[i - 1] + 1;
      }
      return breaks as ClassBreaks;
    }
  }
  const span = domain.high - domain.low;
  return [
    domain.low + span * 0.2,
    domain.low + span * 0.4,
    domain.low + span * 0.6,
    domain.low + span * 0.8,
  ];
}

/** Which of the five classes a score falls in, 0 = worst. */
export function classIndex(score: number, breaks: ClassBreaks): number {
  for (let i = 0; i < breaks.length; i += 1) {
    if (score < breaks[i]) return i;
  }
  return breaks.length;
}

/** The class colour for a score, given the active breaks. */
export function classColor(score: number, breaks: ClassBreaks): string {
  return CLASS_COLORS[classIndex(score, breaks)];
}
