/**
 * The one source of truth for score classing, shared by the map layers and
 * the legend so a building's colour and its legend swatch cannot drift apart.
 *
 * Five classed steps, brown pole (worst) to teal pole (best) with a neutral
 * near-paper midpoint. Diverging because a 0-100 score is polarity data.
 * Brown-teal is the third pair this ramp has worn, each retired for a named
 * reason: green-red is the classic colour-vision failure pair, and blue-red
 * reads as an election map in an American city -- a livability score must not
 * look like a precinct result. Brown reads as barren and teal as thriving,
 * with no party wearing either. Classed rather than continuous because 93% of
 * scores bunch between 35 and 65, where a continuous ramp hands adjacent
 * places imperceptible tints. The backend publishes the quantile edges and
 * numerical accessibility report; this module only applies that contract.
 *
 * These exact BrBG-5 hexes were re-run through the dataviz palette checker
 * rather than inheriting the previous pair's numbers: worst adjacent-pair
 * CVD dE 14.7 (target >= 8), worst normal-vision dE 17.4 (floor 15),
 * lightness monotonic per arm. The near-white midpoint sits low-contrast on
 * the paper basemap; the relief rule is carried by the always-visible legend
 * and the click-through panel.
 */

/** Exact d3-scale-chromatic / ColorBrewer schemeBrBG[5] fallback values. */
export const CLASS_COLORS = [
  '#a6611a', // worst fifth
  '#dfc27d',
  '#f5f5f5', // typical fifth -- deliberately recedes
  '#80cdc1',
  '#018571', // best fifth
] as const;

export type ClassBreaks = number[];

export interface ScoreDomain {
  low: number;
  mid: number;
  high: number;
  /** Twenty five-point buckets across 0-100, from the scoring pass. */
  histogram?: number[];
  /** Strict quantile edges computed over the served H3 population. */
  breaks?: number[];
  /** One colour per class, also owned by the backend contract. */
  colors?: string[];
  population?: string;
  populationN?: number;
}

/** Read server-published edges; fixed bands are only a no-contract fallback. */
export function classBreaks(domain: ScoreDomain): ClassBreaks {
  if (
    domain.breaks && domain.breaks.length <= 4 &&
    domain.breaks.every((value, index, values) =>
      Number.isFinite(value) && (index === 0 || value > values[index - 1]))
  ) return domain.breaks;
  return [20, 40, 60, 80];
}

export function classColors(domain?: ScoreDomain): readonly string[] {
  return domain?.colors?.length === (domain?.breaks?.length ?? -1) + 1
    ? domain.colors
    : CLASS_COLORS;
}

/** Which class a score falls in, 0 = worst. */
export function classIndex(score: number, breaks: ClassBreaks): number {
  for (let i = 0; i < breaks.length; i += 1) {
    if (score < breaks[i]) return i;
  }
  return breaks.length;
}

/** The class colour for a score, given the active breaks. */
export function classColor(
  score: number,
  breaks: ClassBreaks,
  colors: readonly string[] = CLASS_COLORS,
): string {
  return colors[classIndex(score, breaks)];
}
