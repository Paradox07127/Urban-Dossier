export type OverviewTag = 'general' | 'safety' | 'transit' | 'amenities';
export type CategoryId = 'safety' | 'transit' | 'amenities' | 'building';
export type RadiusMeters = 200 | 500 | 1000;

export interface Scores {
  overall: number | null;
  amenities: number | null;
  transit: number | null;
  safety: number | null;
  building?: number | null;
  environment?: number | null;
}

export interface ChartSpec {
  schema_version: '1.0';
  chart_id: string;
  title: string;
  code_ref: string;
  methodology_version: string;
  spec: Record<string, unknown>;
}

export interface EvidenceEntry {
  evidence_id: string;
  source: string;
  date: string;
  summary: string;
}

export interface PriorityAction {
  rank: number;
  signal: string;
  category: CategoryId;
  action: string;
  signal_description: string;
  priority_score: number;
  evidence_ids: string[];
}

export interface WhyNowItem {
  signal: string;
  evidence_ids: string[];
}

export interface BuildingFlag {
  latitude: number;
  longitude: number;
  summary: string;
  severity?: string;
  score_hint?: number;
  bbl?: string;
  bin?: string;
}

export interface DetailItems {
  map_points: Array<Record<string, unknown>>;
  nearby_facilities: Array<Record<string, unknown>>;
  building_flags: BuildingFlag[];
  recent_incidents: Array<Record<string, unknown>>;
  hotspots?: Array<Record<string, unknown>>;
}

export interface DetailTarget {
  latitude: number;
  longitude: number;
  radius_m: RadiusMeters;
  matched_address?: string | null;
  borough?: string | null;
  zip?: string | null;
  canonical_location_id?: string | null;
}

export interface DetailPreviewResponse {
  schema_version: string;
  mode: 'detail_preview';
  target: DetailTarget;
  priority_profile: {
    order: CategoryId[];
    weights: Record<string, number>;
  };
  priority_actions: PriorityAction[];
  why_now: WhyNowItem[];
  current_state: Record<string, Record<string, unknown>>;
  detail_items: DetailItems;
  overview_context?: {
    overall?: Record<string, unknown> | null;
    categories?: Record<string, Record<string, unknown>>;
  } | null;
  trends?: Record<string, Record<string, unknown>>;
  patterns?: Array<Record<string, unknown>>;
  enriched_context?: Record<string, unknown>;
  baselines?: Record<string, Record<string, unknown>>;
  evidence_table: EvidenceEntry[];
  data_gaps: string[];
  scores: Scores;
  chart_specs?: Record<string, ChartSpec>;
  // How much of each category's intended evidence base produced a value.
  score_coverage?: Record<string, ScoreCoverage>;
  // 95% intervals from the offline sensitivity analysis, at cell grain.
  // Absent when the artifact has not been generated -- absence is disclosed,
  // never faked.
  score_uncertainty?: ScoreUncertainty | null;
  preview_ready: boolean;
}

export interface ScoreCoverage {
  available?: number;
  total?: number;
  ratio?: number;
  source?: 'prepared' | 'fallback' | 'none';
  missing?: string[];
  effective_ratio?: number;
}

export interface ScoreUncertainty {
  grain: string;
  methodology_version: string;
  artifact_version: string;
  artifact_generated: string;
  draws: number;
  score_median: number | null;
  nominal_score: number | null;
  nominal_percentile: number | null;
  distribution?: {
    grain: string;
    score_field: string;
    population_n: number;
    bin_width: number;
    bins: Array<{ bin_start: number; bin_end: number; count: number }>;
    marker_score: number | null;
    marker_percentile: number | null;
    method: string;
  } | null;
  // Production normalization held fixed; weights, inclusion and the
  // missing-data rule vary. The interval a reader should lead with.
  score_range: [number | null, number | null];
  public_tier: {
    schema_version: '1.0';
    scale: 'fixed_20_point_score_bands';
    basis: 'production_normalization_95pct_interval';
    label: string;
    spans_multiple_tiers: boolean;
    lower: { id: string; label: string; score_min: number; score_max: number };
    upper: { id: string; label: string; score_min: number; score_max: number };
    score_range: [number, number];
  } | null;
  // Additionally varies the normalization method. Wider, and honest about it.
  score_range_all_methods: [number | null, number | null];
  rank_range_share: [number, number] | null;
  note: string;
}

export interface ComparisonDeltaMap {
  schema_version: '1.0';
  code_ref: string;
  methodology_version: string;
  direction: 'point_b_minus_point_a';
  radius_m: number;
  bbox: [number, number, number, number];
  geojson: GeoJSON.FeatureCollection;
  presentation: {
    palette: string;
    domain: [number, number];
    clamp: boolean;
    stops: Array<{ value: number; color: string }>;
    zero_color: string;
    no_data_color: string;
    point_a_color: string;
    category_fields: Record<string, string>;
  };
}

export interface BivariatePresentation {
  palette: string;
  x: {
    category: string;
    field: string;
    breaks: number[];
  };
  y: {
    category: string;
    field: string;
    breaks: number[];
  };
  matrix: string[][];
  index: string;
  accessibility: {
    passes: boolean;
    threshold: number;
    minimum_adjacent_delta_e: Record<string, number>;
  };
}

export interface TimelinePeriod {
  period: string;
  period_complete: boolean;
  value_property: string;
  color_property: string;
  breaks: number[];
  colors: string[];
  classification: string;
  requested_classes: number;
  effective_classes: number;
  population_n: number;
  total_value: number;
}

export interface TimelinePresentation {
  schema_version: string;
  code_ref: string;
  methodology_version: string;
  signal: string;
  label: string;
  available: boolean;
  artifact_version: string;
  periods: TimelinePeriod[];
  default_period: string;
  population: string;
  cell_count: number;
  no_data_color: string;
  animation: {
    state_property: string;
    lookup: string;
    tick_mutation: string;
  };
}

export interface DetailResponse extends Omit<DetailPreviewResponse, 'mode' | 'preview_ready'> {
  mode: 'detail';
  report_summary: string;
  report_markdown: string;
}

export interface OverviewCell {
  h3?: string;
  latitude?: number;
  longitude?: number;
  overall_score?: number | null;
  safety_score?: number | null;
  transit_score?: number | null;
  amenities_score?: number | null;
  building_stress_score?: number | null;
  category_scores?: Record<string, number | null>;
}

export interface OverviewResponse {
  schema_version: string;
  mode: 'overview';
  view_mode: 'overall' | 'category';
  category_id: string | null;
  overview_ready: boolean;
  cells: OverviewCell[];
  coverage: {
    overview_ready: boolean;
    available_categories: string[];
    missing_categories: string[];
  };
  resolved_data_mode?: string;
  ui_message?: string;
}

// --- Agent Mode types ---

export interface AgentStatus {
  enabled: boolean;
  backend: string;
  nemoclaw_available: boolean;
  scripts_available: boolean;
  model: string;
  tools: Record<
    string,
    {
      available: boolean;
      reason: string;
      release_gate: string;
      interventions?: string[];
    }
  >;
  available_tools: string[];
  unavailable_tools: string[];
}

/** One dispatched tool call, as returned in the /api/agent/ask trace. */
export interface AgentTrace {
  iteration: number;
  tool_name: string;
  args: Record<string, unknown>;
  result: Record<string, any>;
  latency_ms: number;
}

export interface AgentEvidence {
  source: string;
  detail: string;
}

export interface AgentChatMessage {
  role: "user" | "assistant";
  content: string;
  timestamp: number;
  /** Present on assistant turns answered by the tool-using agent loop. */
  trace?: AgentTrace[];
  evidence?: AgentEvidence[];
  iterations?: number;
  failed?: boolean;
}

export interface AgentReportResult {
  html: string;
  markdown: string;
}

export interface AgentPosterResult {
  html: string;
}

// Legacy mock type kept temporarily so the current demo can render before full API wiring is complete.
export interface Location {
  id: string;
  title: string;
  description: string;
  position: [number, number];
  category: string;
  scores: Scores;
  aiSummary: string;
  evidence: Array<{
    id: string;
    source: string;
    date: string;
    summary: string;
  }>;
}
