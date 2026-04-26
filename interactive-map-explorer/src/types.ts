export type OverviewTag = 'general' | 'safety' | 'transit' | 'amenities';
export type CategoryId = 'safety' | 'transit' | 'amenities' | 'building';
export type RadiusMeters = 200 | 500 | 1000;

export interface Scores {
  overall: number | null;
  amenities: number | null;
  transit: number | null;
  safety: number | null;
  building?: number | null;
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
  preview_ready: boolean;
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
}

export interface AgentChatMessage {
  role: "user" | "assistant";
  content: string;
  timestamp: number;
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
