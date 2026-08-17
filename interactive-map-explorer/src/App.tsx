/**
 * Urban Dossier NYC - Main Application
 */

import React, { useEffect, useMemo, useRef, useState } from 'react';
import { motion, AnimatePresence, Reorder } from 'motion/react';
import {
  AlertCircle,
  Bus,
  ChevronRight,
  Download,
  Globe,
  Loader2,
  Maximize2,
  Minimize2,
  Pin,
  Search,
  ShieldCheck,
  Sun,
  Utensils,
  Wind,
  X,
} from 'lucide-react';

import MapComponent from './components/Map';
import InstrumentRail, { type ColourDomain } from './components/InstrumentRail';
import AgentToggle from './components/AgentToggle';
import AgentPanel from './components/AgentPanel';
import type {
  DetailPreviewResponse,
  DetailResponse,
  EvidenceEntry,
  AgentStatus,
  BivariatePresentation,
  TimelinePresentation,
  PriorityAction,
  RadiusMeters,
  Scores,
} from './types';

const EMPTY_HOTSPOTS: any[] = [];
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import MethodologyPanel from './components/MethodologyPanel';
import VegaChart from './components/VegaChart';
import { useComparison } from './features/compare/useComparison';

// --- Constants ---

const NYC_LANDMARKS: Record<string, [number, number]> = {
  'times square': [40.758, -73.9855],
  'central park': [40.7829, -73.9654],
  'statue of liberty': [40.6892, -74.0445],
  'brooklyn bridge': [40.7061, -73.9969],
  'empire state': [40.7484, -73.9857],
};

const PRIORITY_ICONS: Record<string, React.ComponentType<{ className?: string }>> = {
  Amenities: Utensils,
  Transit: Bus,
  Safety: ShieldCheck,
};

const RADIUS_OPTIONS: RadiusMeters[] = [200, 500, 1000];

// --- Utils ---

function formatScore(score: number | null | undefined): string {
  return typeof score === 'number' && !Number.isNaN(score) ? String(Math.round(score)) : '--';
}

function environmentTier(score: number | null | undefined): string {
  if (typeof score !== 'number' || Number.isNaN(score)) return 'Not available';
  if (score >= 67) return 'Lower modeled NO';
  if (score <= 33) return 'Higher modeled NO';
  return 'Middle modeled NO';
}

function heatVulnerabilityTier(score: number | null | undefined): string {
  if (typeof score !== 'number' || Number.isNaN(score)) return 'Not available';
  const hvi = Math.max(1, Math.min(5, Math.round(5 - score / 25)));
  const labels = ['Lowest', 'Lower', 'Middle', 'Higher', 'Highest'];
  return `${labels[hvi - 1]} heat vulnerability · HVI ${hvi}/5`;
}

function priorityOrderKey(priorities: string[]): string[] {
  return priorities.map((p) => p.toLowerCase());
}

/*
 * The score ramp. This is the only chroma the interface spends, so it has to
 * do real work.
 *
 * The previous ramp was RdYlGn in five stops, which put a pale yellow at the
 * midpoint: mid-range scores washed out to khaki against the panel, and the
 * light middle meant the scale carried almost no information in greyscale or
 * to a red-green colourblind reader. This ramp keeps the bad-to-good reading
 * the domain expects but separates the ends by lightness as well as hue, so a
 * low score stays the darkest, heaviest mark on the page whatever you can see.
 *
 * Kept in sync with --ud-low / --ud-mid / --ud-high in index.css.
 */
const GRADIENT_STOPS = [
  { at: 0,   r: 140, g: 29,  b: 24  }, // #8C1D18
  { at: 50,  r: 150, g: 146, b: 138 }, // #96928A
  { at: 100, r: 46,  g: 139, b: 98  }, // #2E8B62
];

function lerpGradient(score: number): [number, number, number] {
  const s = Math.max(0, Math.min(100, score));
  for (let i = 0; i < GRADIENT_STOPS.length - 1; i++) {
    const a = GRADIENT_STOPS[i], b = GRADIENT_STOPS[i + 1];
    if (s >= a.at && s <= b.at) {
      const t = (s - a.at) / (b.at - a.at);
      return [
        Math.round(a.r + (b.r - a.r) * t),
        Math.round(a.g + (b.g - a.g) * t),
        Math.round(a.b + (b.b - a.b) * t),
      ];
    }
  }
  const last = GRADIENT_STOPS[GRADIENT_STOPS.length - 1];
  return [last.r, last.g, last.b];
}

function scoreGradientStyle(score: number | null | undefined): React.CSSProperties {
  if (score == null) return {};
  const [r, g, b] = lerpGradient(score);
  /* A wash, not a fill: the card stays paper and the colour reads as a
     measurement sitting on it. */
  return {
    backgroundColor: `rgba(${r}, ${g}, ${b}, 0.07)`,
    borderColor: `rgba(${r}, ${g}, ${b}, 0.28)`,
  };
}

function scoreTextStyle(score: number | null | undefined): React.CSSProperties {
  if (score == null) return {};
  const [r, g, b] = lerpGradient(score);
  /* The ramp is already chosen to be legible as text on paper, so it is used
     as-is. The previous multiply-by-0.7 darkening is what turned mid scores
     into mud. */
  return { color: `rgb(${r}, ${g}, ${b})` };
}

function scoreColor(score: number | null | undefined): string {
  if (score == null) return 'text-muted-foreground';
  return ''; // handled by inline style now
}

function summarizeEvidence(e: EvidenceEntry): string {
  return e.summary || `${e.source} (${e.date})`;
}

async function extractBackendError(resp: Response, fallback: string): Promise<string> {
  try {
    const body = await resp.json();
    return body?.detail || body?.error || body?.message || fallback;
  } catch {
    return fallback;
  }
}

interface SelectedTarget {
  title: string;
  position: [number, number];
  category: string;
}

interface LocationDisplay {
  title: string;
  description: string;
  position: [number, number];
  category: string;
  scores: Scores;
}

function buildLocationDisplay(
  target: SelectedTarget,
  preview: DetailPreviewResponse | null,
): LocationDisplay | null {
  if (!target) return null;
  if (!preview) {
    return {
      title: target.title,
      description: target.category,
      position: target.position,
      category: target.category,
      scores: { overall: null, amenities: null, transit: null, safety: null },
    };
  }
  return {
    title: preview.target.matched_address || target.title,
    description:
      `${preview.target.borough || ''} ${preview.target.zip || ''}`.trim() || target.category,
    position: target.position,
    category: 'Analysis',
    scores: { ...preview.scores, building: preview.scores.building ?? null },
  };
}

function parseReportBlocks(text: string) {
  const lines = text.trim().split(/\n/);
  const blocks: { type: string; content?: string; items?: string[] }[] = [];
  let currentList: string[] = [];

  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    if (trimmed.startsWith('## ')) {
      if (currentList.length) {
        blocks.push({ type: 'list', items: [...currentList] });
        currentList = [];
      }
      blocks.push({ type: 'heading', content: trimmed.slice(3) });
    } else if (trimmed.startsWith('### ')) {
      if (currentList.length) {
        blocks.push({ type: 'list', items: [...currentList] });
        currentList = [];
      }
      blocks.push({ type: 'subheading', content: trimmed.slice(4) });
    } else if (trimmed.startsWith('- ')) {
      currentList.push(trimmed.slice(2));
    } else {
      if (currentList.length) {
        blocks.push({ type: 'list', items: [...currentList] });
        currentList = [];
      }
      blocks.push({ type: 'paragraph', content: trimmed });
    }
  }
  if (currentList.length) blocks.push({ type: 'list', items: currentList });
  return blocks;
}

// --- Main App ---

export default function App() {
  // NYC-wide overview center (shows all 5 boroughs)
  const NYC_OVERVIEW: [number, number] = [40.7128, -73.96];
  const NYC_OVERVIEW_ZOOM = 10.5;

  const [center, setCenter] = useState<[number, number]>(NYC_OVERVIEW);
  const [zoom, setZoom] = useState(NYC_OVERVIEW_ZOOM);
  const [selectedTarget, setSelectedTarget] = useState<SelectedTarget | null>(null);
  const [searchQuery, setSearchQuery] = useState('');
  const [priorities, setPriorities] = useState(['Amenities', 'Transit', 'Safety']);
  const [activePriority, setActivePriority] = useState<string | null>(null);
  const [selectedRadiusM, setSelectedRadiusM] = useState<RadiusMeters>(200);
  const [preview, setPreview] = useState<DetailPreviewResponse | null>(null);
  const [finalReport, setFinalReport] = useState<DetailResponse | null>(null);
  const [methodologyOpen, setMethodologyOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [reportLoading, setReportLoading] = useState(false);
  const [exportLoading, setExportLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [panelExpanded, setPanelExpanded] = useState(false);
  const [refreshKey, setRefreshKey] = useState(0);

  // Report cache: keep both types per location
  const [reportCache, setReportCache] = useState<Record<string, DetailResponse>>({});
  const [activeReportMode, setActiveReportMode] = useState<string | null>(null);
  const [reportModalOpen, setReportModalOpen] = useState(false);

  // Agent mode
  const [agentMode, setAgentMode] = useState(false);
  const [agentStatus, setAgentStatus] = useState<AgentStatus | null>(null);
  const [agentSessionId, setAgentSessionId] = useState<string | null>(null);
  /* Isochrone the agent computed for the current point, drawn on the map. */
  const [isochrone, setIsochrone] = useState<any | null>(null);

  // View state, owned here so the instrument rail can drive it and the map can
  // report back what it measured.
  const [sandbox, setSandbox] = useState(false);
  const [sandboxAvailable, setSandboxAvailable] = useState(false);
  const [colourDomains, setColourDomains] = useState<Record<string, ColourDomain>>({});
  const [bivariate, setBivariate] = useState(false);
  const [bivariatePresentation, setBivariatePresentation] =
    useState<BivariatePresentation | null>(null);
  const [timeline, setTimeline] = useState(false);
  const [timelinePresentation, setTimelinePresentation] =
    useState<TimelinePresentation | null>(null);
  const [timelinePeriod, setTimelinePeriod] = useState<string | null>(null);
  const [timelinePlaying, setTimelinePlaying] = useState(false);

  useEffect(() => {
    if (!timeline || !timelinePlaying || !timelinePresentation?.periods.length) return;
    const periods = timelinePresentation.periods.map((item) => item.period);
    const timer = window.setInterval(() => {
      setTimelinePeriod((current) => {
        const index = Math.max(0, periods.indexOf(current ?? ''));
        return periods[(index + 1) % periods.length];
      });
    }, 900);
    return () => window.clearInterval(timer);
  }, [timeline, timelinePlaying, timelinePresentation]);

  const reportRef = useRef<HTMLDivElement>(null);

  const renderTag = (activePriority ? activePriority.toLowerCase() : 'general') as 'general' | 'safety' | 'transit' | 'amenities';
  const display = useMemo(
    () => (selectedTarget ? buildLocationDisplay(selectedTarget, preview) : null),
    [preview, selectedTarget],
  );
  const priorityOrder = useMemo(() => priorityOrderKey(priorities), [priorities]);
  const {
    pinnedPreview,
    pinnedTitle,
    serverComparison,
    comparisonActive,
    pin: pinComparison,
    clear: clearComparison,
  } = useComparison(preview, selectedRadiusM, priorityOrder);

  // Zoom ref to avoid stale closure in onMapClick
  const zoomRef = useRef(zoom);
  useEffect(() => {
    zoomRef.current = zoom;
  }, [zoom]);

  // Check agent availability on mount
  useEffect(() => {
    fetch('/api/agent/status')
      .then((r) => r.json())
      .then((data: AgentStatus) => setAgentStatus(data))
      .catch(() => setAgentStatus(null));
  }, []);

  // Create agent session on demand
  const createAgentSession = async (): Promise<string> => {
    if (agentSessionId) return agentSessionId;
    const target = selectedTarget;
    if (!target) throw new Error('No location selected');
    const res = await fetch('/api/agent/session', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        latitude: target.position[0],
        longitude: target.position[1],
        radius_m: selectedRadiusM,
        priority_order: priorities.map((p) => p.toLowerCase()),
        time_window_days: 365,
      }),
    });
    if (!res.ok) {
      const errBody = await res.json().catch(() => ({}));
      throw new Error(errBody?.detail || errBody?.error || `Session creation failed (${res.status})`);
    }
    const data = await res.json();
    if (!data.session_id) throw new Error('No session_id returned');
    setAgentSessionId(data.session_id);
    return data.session_id;
  };

  // --- Fetch preview on target / radius change ---
  useEffect(() => {
    if (!selectedTarget) return;
    setAgentSessionId(null);
    // Location changed — clear report cache
    setReportCache({});
    setActiveReportMode(null);
    setReportModalOpen(false);
    let cancelled = false;
    const ac = new AbortController();

    (async () => {
      setLoading(true);
      setError(null);
      setFinalReport(null);
      setPreview(null);
      try {
        const resp = await fetch('/api/detail/preview', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          signal: ac.signal,
          body: JSON.stringify({
            latitude: selectedTarget.position[0],
            longitude: selectedTarget.position[1],
            radius_m: selectedRadiusM,
            priority_order: priorityOrder,
            time_window_days: 365,
          }),
        });
        if (!resp.ok) {
          const msg = await extractBackendError(resp, `Preview failed with status ${resp.status}`);
          throw new Error(msg);
        }
        const data = await resp.json();
        if (!cancelled) setPreview(data);
      } catch (err) {
        if (!cancelled) {
          setPreview(null);
          setError(err instanceof Error ? err.message : 'Preview request failed.');
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => {
      cancelled = true;
      ac.abort();
    };
  }, [priorityOrder, selectedRadiusM, selectedTarget]);

  // --- Handlers ---

  const handleMarkerClick = (loc: any) => {
    setCenter(loc.position);
    setZoom(18);
    setSelectedTarget({ title: loc.title, position: loc.position, category: loc.category });
  };

  // Takes the query as an argument rather than reading state, so a caller that
  // has just typed it does not have to wait a render for setState to land.
  const handleSearch = async (queryOverride?: string) => {
    const raw = (queryOverride ?? searchQuery).trim();
    if (!raw) return;
    setError(null);
    const q = raw;
    const coordMatch = q.match(/^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$/);
    let label = q;
    let pos: [number, number] | null = null;

    if (coordMatch) {
      pos = [Number(coordMatch[1]), Number(coordMatch[2])];
    } else {
      const lower = q.toLowerCase();
      for (const [name, coord] of Object.entries(NYC_LANDMARKS)) {
        if (name.includes(lower) || lower.includes(name)) {
          label = name
            .split(' ')
            .map((w) => w[0].toUpperCase() + w.slice(1))
            .join(' ');
          pos = coord;
          break;
        }
      }
      if (!pos) {
        try {
          const resp = await fetch('/api/search', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ query: q, limit: 1 }),
          });
          if (resp.ok) {
            const results = (await resp.json()).results || [];
            if (results.length > 0) {
              const r = results[0];
              pos = [r.latitude, r.longitude];
              label = [r.address, r.borough].filter(Boolean).join(', ') || q;
            }
          }
        } catch {}
      }
    }
    if (!pos) {
      setError(`No results found for "${q}". Try a NYC address, landmark, or coordinates.`);
      return;
    }
    setError(null);
    setCenter(pos);
    setZoom(15);
    setSelectedTarget({ title: label, position: pos, category: 'Search Result' });
    setSearchQuery('');
  };

  const handleSearchKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter') handleSearch();
  };

  const handleMapClick = (lat: number, lng: number) => {
    const currentZoom = zoomRef.current;
    setCenter([lat, lng]);
    // Only zoom in if not already close enough — avoids full map reload
    if (currentZoom < 15) {
      setZoom(15);
    }
    setSelectedTarget({
      title: `Selected point ${lat.toFixed(4)}, ${lng.toFixed(4)}`,
      position: [lat, lng],
      category: 'Point Analysis',
    });
  };

  const handleGenerateReport = async (mode: string = 'individual') => {
    if (!selectedTarget) return;
    // If already cached, just show it
    if (reportCache[mode]) {
      setFinalReport(reportCache[mode]);
      setActiveReportMode(mode);
      setReportModalOpen(true);
      return;
    }
    const targetSnapshot = selectedTarget;
    setReportLoading(true);
    setActiveReportMode(mode);
    setReportModalOpen(true);
    setError(null);
    try {
      const resp = await fetch('/api/analyze-point', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          latitude: targetSnapshot.position[0],
          longitude: targetSnapshot.position[1],
          radius_m: selectedRadiusM,
          priority_order: priorityOrder,
          time_window_days: 365,
          include_report: true,
          report_mode: mode,
        }),
      });
      if (!resp.ok) {
        const msg = await extractBackendError(resp, `Report failed with status ${resp.status}`);
        throw new Error(msg);
      }
      const data = await resp.json();
      // Guard against stale response: only update if the target hasn't changed
      setSelectedTarget((current) => {
        if (current === targetSnapshot) {
          setFinalReport(data);
          setPreview(data);
          setReportCache((prev) => ({ ...prev, [mode]: data }));
        }
        return current;
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Report request failed.');
    } finally {
      setReportLoading(false);
    }
  };

  const handleExportHtml = async () => {
    if (!preview || !display || !preview.chart_specs) return;
    setExportLoading(true);
    setError(null);
    try {
      const response = await fetch('/api/export/html', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          title: display.title,
          report_mode: activeReportMode || 'snapshot',
          target: preview.target,
          scores: preview.scores,
          score_coverage: preview.score_coverage,
          score_uncertainty: preview.score_uncertainty,
          // The generator reads NYCCAS/HVI from metric_scores only; omitting
          // it made the two context metrics vanish from the downloaded file
          // while showing fine online. Chromium smoke covers this now.
          metric_scores: preview.metric_scores,
          chart_specs: preview.chart_specs,
          evidence_table: preview.evidence_table,
          data_gaps: preview.data_gaps,
          report_markdown: finalReport?.report_markdown || '',
        }),
      });
      if (!response.ok) {
        throw new Error(await extractBackendError(response, `Export failed with status ${response.status}`));
      }
      const blob = await response.blob();
      const disposition = response.headers.get('Content-Disposition') || '';
      const filename = disposition.match(/filename="([^"]+)"/)?.[1] || 'urban-dossier-report.html';
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = filename;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1_000);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'HTML export failed.');
    } finally {
      setExportLoading(false);
    }
  };

  const handleClose = () => {
    setSelectedTarget(null);
    setPreview(null);
    setFinalReport(null);
    setError(null);
    setPanelExpanded(false);
    setReportModalOpen(false);
    setReportCache({});
    setActiveReportMode(null);
    setIsochrone(null);
  };

  /* An isochrone belongs to the point it was computed from. Moving the
     selection has to drop it, or the map shows a walkable area for somewhere
     the reader is no longer looking at. */
  useEffect(() => {
    setIsochrone(null);
  }, [selectedTarget?.position[0], selectedTarget?.position[1]]);

  const handleGlobalView = () => {
    handleClose();
    clearComparison();
    setActivePriority(null);
    setBivariate(false);
    setTimeline(false);
    setTimelinePlaying(false);
    setCenter(NYC_OVERVIEW);
    setZoom(NYC_OVERVIEW_ZOOM);
    setRefreshKey((k) => k + 1);
  };

  const scores = preview?.scores ?? display?.scores;
  const metricScores = preview?.metric_scores;
  const buildingFlags = preview?.detail_items?.building_flags ?? [];
  const evidenceTable = preview?.evidence_table ?? [];
  const priorityActions = preview?.priority_actions ?? [];

  // Extract hidden data for Neighborhood Insights
  const enrichedContext = preview?.enriched_context as Record<string, any> | undefined;
  const currentState = preview?.current_state as Record<string, any> | undefined;
  const baselines = preview?.baselines as Record<string, any> | undefined;
  const whyNow = preview?.why_now as Array<{ signal: string; trend_type: string }> | undefined;
  const hotspots = preview?.detail_items?.hotspots as Array<any> | undefined;
  const nearestParks = enrichedContext?.nearest_parks as string[] | undefined;
  const treeHealth = enrichedContext?.tree_health as Record<string, number> | undefined;
  const restaurantHL = enrichedContext?.restaurant_highlights as Record<string, any> | undefined;
  const violationAge = enrichedContext?.violation_age as Record<string, number> | undefined;
  const collisionBuckets = enrichedContext?.collision_time_buckets as Record<string, number> | undefined;

  const reportMarkdown = finalReport?.report_markdown || '';
  const reportBlocks = useMemo(
    () => (reportMarkdown ? parseReportBlocks(reportMarkdown) : []),
    [reportMarkdown],
  );

  // Panel background tint based on overall score
  const panelBgStyle = useMemo<React.CSSProperties>(() => {
    const s = scores?.overall;
    if (s == null) return {};
    const [r, g, b] = lerpGradient(s);
    return { backgroundColor: `rgba(${r}, ${g}, ${b}, 0.06)` };
  }, [scores?.overall]);

  return (
    <div className="relative h-screen w-full overflow-hidden bg-background text-foreground font-sans">
      {/* Map */}
      <div
        className={`absolute inset-0 transition-all duration-300 ease-in-out ${
          display ? (panelExpanded ? 'md:pr-[55vw]' : 'md:pr-[680px]') : ''
        }`}
      >
        <MapComponent
          center={center}
          zoom={zoom}
          renderTag={renderTag}
          localRenderTarget={
            selectedTarget
              ? { center: selectedTarget.position, radiusM: selectedRadiusM, priorityOrder }
              : null
          }
          refreshKey={refreshKey}
          markers={display ? [{ id: 'sel', title: display.title, description: display.description, position: display.position, category: display.category, scores: display.scores, aiSummary: '', evidence: [] }] : []}
          hotspots={(hotspots as any[]) ?? EMPTY_HOTSPOTS}
          isochrone={isochrone}
          comparisonDeltaMap={serverComparison?.delta_map ?? null}
          bivariate={bivariate}
          timeline={timeline}
          timelinePeriod={timelinePeriod}
          sandbox={sandbox}
          onViewChange={(view) => {
            setCenter((current) =>
              Math.abs(current[0] - view.center[0]) < 1e-7 &&
              Math.abs(current[1] - view.center[1]) < 1e-7
                ? current
                : view.center,
            );
            setZoom((current) => Math.abs(current - view.zoom) < 1e-6 ? current : view.zoom);
          }}
          onSandboxAvailable={setSandboxAvailable}
          onColourDomains={setColourDomains}
          onBivariatePresentation={setBivariatePresentation}
          onTimelinePresentation={(presentation) => {
            setTimelinePresentation(presentation);
            if (presentation && !presentation.periods.some((item) => item.period === timelinePeriod)) {
              setTimelinePeriod(presentation.default_period);
            }
          }}
          onMarkerClick={handleMarkerClick}
          onMapClick={handleMapClick}
        />
      </div>

      {/* The instrument rail: everything that changes how the city is being
          looked at, in one column. Search, projection, lens, and the legend
          that decodes the lens -- previously spread across three corners with
          no relationship visible between them. */}
      <InstrumentRail
        tag={renderTag}
        onTagChange={(t) => {
          setBivariate(false);
          setTimeline(false);
          setTimelinePlaying(false);
          setActivePriority(t === 'general' ? null : t.charAt(0).toUpperCase() + t.slice(1));
          if (t !== 'general' && !selectedTarget) {
            setCenter(NYC_OVERVIEW);
            setZoom(NYC_OVERVIEW_ZOOM);
          }
        }}
        sandbox={sandbox}
        sandboxAvailable={sandboxAvailable}
        onSandboxChange={setSandbox}
        domains={colourDomains}
        bivariate={bivariate}
        bivariatePresentation={bivariatePresentation}
        onBivariateChange={(enabled) => {
          if (enabled) {
            handleGlobalView();
            setSandbox(false);
            setTimeline(false);
            setTimelinePlaying(false);
          }
          setBivariate(enabled);
        }}
        timeline={timeline}
        timelinePresentation={timelinePresentation}
        timelinePeriod={timelinePeriod}
        timelinePlaying={timelinePlaying}
        onTimelineChange={(enabled) => {
          if (enabled) {
            handleGlobalView();
            setSandbox(false);
            setBivariate(false);
          }
          setTimeline(enabled);
          setTimelinePlaying(false);
        }}
        onTimelinePeriodChange={setTimelinePeriod}
        onTimelinePlayingChange={setTimelinePlaying}
        onSearch={(q) => { setSearchQuery(q); handleSearch(q); }}
        onResetView={handleGlobalView}
        searchError={error}
      />

      {/* Results that appeared on the map get a readout next to it. Not a
          control -- it reports something the agent computed and offers the one
          action that applies, which is to take it away again. */}
      <AnimatePresence>
        {isochrone?.properties && (
          <motion.div
            initial={{ opacity: 0, y: 6 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 6 }}
            className="absolute bottom-4 left-4 z-30 flex items-center gap-3 rounded-xl border border-border bg-background/95 px-3 py-2 shadow-lg backdrop-blur-md"
          >
            <span className="ud-label">Walkable area</span>
            <span className="font-mono text-xs tabular-nums text-foreground">
              {(isochrone.properties.area_m2 / 1e6).toFixed(2)} km²
            </span>
            <span className="font-mono text-[10px] tabular-nums text-muted-foreground">
              {isochrone.properties.minutes} min · {Number(isochrone.properties.reachable_nodes ?? 0).toLocaleString()} nodes
            </span>
            <button
              type="button"
              onClick={() => setIsochrone(null)}
              className="rounded-sm p-1 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
              aria-label="Remove walkable area from map"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Detail Panel */}
      <AnimatePresence>
        {display && (
          <motion.div
            initial={{ x: '100%' }}
            animate={{ x: 0 }}
            exit={{ x: '100%' }}
            transition={{ type: 'spring', damping: 25, stiffness: 200 }}
            /* transition-[width], not transition-all.
               transition-all covers transform, which motion is already
               animating frame by frame -- the CSS transition then interpolated
               towards each frame's value and the element lagged behind its own
               spring. backdrop-blur promotes this to its own compositing
               layer, so the blurred backing and the text composited at
               different points in that lag and the panel appeared to arrive
               twice. The CSS transition is only wanted for the expand toggle,
               which is a width change. */
            className={`absolute right-0 top-0 h-full w-full min-h-0 backdrop-blur-xl border-l border-border shadow-xl z-20 flex flex-col transition-[width] duration-300 ${
              panelExpanded ? 'md:w-[55vw]' : 'md:w-[680px]'
            }`}
            style={{ backgroundColor: `rgba(255,255,255,0.95)`, ...panelBgStyle }}
          >
            {/* Panel header */}
            <div className="px-5 py-4 flex items-start justify-between border-b border-border/60 gap-3">
              <div className="min-w-0 flex-1">
                <h2 className="ud-display text-lg leading-snug break-words">{display.title}</h2>
                <span className="text-xs text-muted-foreground">
                  {display.description || display.category}
                </span>
              </div>
              <div className="flex items-center gap-1.5 flex-shrink-0">
                <AgentToggle
                  enabled={agentMode}
                  onToggle={setAgentMode}
                  available={agentStatus?.enabled ?? false}
                />
                <button
                  onClick={() => setPanelExpanded(!panelExpanded)}
                  className="p-2 hover:bg-muted rounded-md hidden md:flex"
                >
                  {panelExpanded ? (
                    <Minimize2 className="w-4 h-4" />
                  ) : (
                    <Maximize2 className="w-4 h-4" />
                  )}
                </button>
                <button
                  onClick={() => {
                    if (preview) {
                      pinComparison(preview, display?.title || 'Pinned');
                    }
                  }}
                  className={`p-2 hover:bg-muted rounded-md ${pinnedPreview ? 'text-primary' : ''}`}
                  title="Pin for compare"
                  aria-label="Pin current location for comparison"
                >
                  <Pin className="w-4 h-4" />
                </button>
                <button onClick={handleClose} className="p-2 hover:bg-muted rounded-md">
                  <X className="w-4 h-4" />
                </button>
              </div>
            </div>

            {/* Panel body */}
            <div className="min-h-0 flex-1 overflow-y-auto relative">
              {agentMode ? (
                <AgentPanel
                  sessionId={agentSessionId}
                  onCreateSession={createAgentSession}
                  analysisPayload={preview}
                  target={
                    selectedTarget
                      ? {
                          latitude: selectedTarget.position[0],
                          longitude: selectedTarget.position[1],
                          label: display?.title,
                        }
                      : null
                  }
                  onIsochrone={setIsochrone}
                  toolAvailability={agentStatus?.tools}
                />
              ) : (
              <>
              {/* Loading overlay */}
              <AnimatePresence>
                {(loading || reportLoading) && (
                  <motion.div
                    initial={{ opacity: 0 }}
                    animate={{ opacity: 1 }}
                    exit={{ opacity: 0 }}
                    className="absolute inset-0 z-10 bg-background/70 backdrop-blur-sm flex flex-col items-center justify-center gap-3"
                  >
                    <Loader2 className="w-8 h-8 text-primary animate-spin" />
                    <span className="text-sm font-medium text-muted-foreground">
                      {reportLoading ? 'Generating report...' : 'Analyzing area...'}
                    </span>
                  </motion.div>
                )}
              </AnimatePresence>

              <div className="p-5 space-y-5">
                {/* Compare bar */}
                {pinnedPreview && preview && comparisonActive && (
                  <div className="rounded-xl border border-primary/30 bg-primary/5 p-4 space-y-3">
                    <div className="flex items-center justify-between">
                      <span className="text-xs font-semibold uppercase tracking-wider text-primary">Compare</span>
                      <button onClick={clearComparison} className="text-xs text-muted-foreground hover:text-foreground">Clear pin</button>
                    </div>
                    <div className="grid grid-cols-3 gap-2 text-center text-xs">
                      <div className="font-medium text-muted-foreground truncate" title={pinnedTitle}>{pinnedTitle}</div>
                      <div></div>
                      <div className="font-medium text-muted-foreground truncate" title={display?.title}>{display?.title}</div>
                    </div>
                    {(['overall', 'safety', 'transit', 'amenities'] as const).map((cat) => {
                      const pScore = serverComparison?.point_a.scores?.[cat as keyof Scores];
                      const cScore = serverComparison?.point_b.scores?.[cat as keyof Scores];
                      const pVal = typeof pScore === 'number' ? pScore : null;
                      const cVal = typeof cScore === 'number' ? cScore : null;
                      // Backend-computed; local subtraction is not a fallback,
                      // because a silently different delta path is worse than
                      // a briefly absent chip. Chip colours are the ramp's own
                      // poles -- green/red is banned here for the same reason
                      // it is banned on the map.
                      const rawDelta = serverComparison?.deltas?.[cat];
                      const diff = typeof rawDelta === 'number' ? Math.round(rawDelta) : null;
                      const deltaStops = serverComparison?.delta_map?.presentation.stops ?? [];
                      const negativeColor = deltaStops[0]?.color ?? '#e66101';
                      const positiveColor = deltaStops[deltaStops.length - 1]?.color ?? '#5e3c99';
                      return (
                        <div key={cat} className="grid grid-cols-3 gap-2 text-center text-sm tabular-nums">
                          <span className="font-bold" style={scoreTextStyle(pVal)}>{pVal ?? '--'}</span>
                          <span className="text-xs text-muted-foreground self-center capitalize">{cat}</span>
                          <span className="font-bold" style={scoreTextStyle(cVal)}>
                            {cVal ?? '--'}
                            {diff != null && diff !== 0 && (
                              <span
                                className="text-[10px] ml-1"
                                style={{ color: diff > 0 ? positiveColor : negativeColor }}
                              >
                                {diff > 0 ? '+' : ''}{diff}
                              </span>
                            )}
                          </span>
                        </div>
                      );
                    })}
                    {serverComparison?.delta_map && (
                      <div
                        className="space-y-1.5 border-t border-primary/15 pt-2"
                        aria-label="Comparison delta map legend"
                      >
                        <div className="flex items-center justify-between font-mono text-[10px] text-muted-foreground">
                          <span>Map · B − A · {renderTag === 'general' ? 'overall' : renderTag}</span>
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
                          <span>B lower</span><span>same</span><span>B higher</span>
                        </div>
                      </div>
                    )}
                    {serverComparison?.chart_specs?.compare_scores && (
                      <VegaChart chart={serverComparison.chart_specs.compare_scores} />
                    )}
                  </div>
                )}

                {/* The radius is a parameter of this reading, not of the map, so
                    it sits with the number it produces. Changing it visibly
                    changes the score directly beneath it. */}
                <div className="mb-3 flex items-center justify-between">
                  <span className="ud-label">Measured within</span>
                  <div
                    className="flex gap-0.5 rounded-md border border-border bg-muted/40 p-0.5"
                    role="group"
                    aria-label="Analysis radius"
                  >
                    {RADIUS_OPTIONS.map((r) => (
                      <button
                        key={r}
                        type="button"
                        onClick={() => setSelectedRadiusM(r)}
                        aria-pressed={selectedRadiusM === r}
                        className={`rounded-[4px] px-2.5 py-1 font-mono text-[11px] tabular-nums transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring ${
                          selectedRadiusM === r
                            ? 'bg-foreground text-background'
                            : 'text-muted-foreground hover:text-foreground'
                        }`}
                      >
                        {r} m
                      </button>
                    ))}
                  </div>
                </div>

                {/* Score cards — 2x2 grid */}
                <div className="grid grid-cols-2 gap-3">
                  {/* Overall score - spans full width */}
                  <div
                    className="col-span-2 rounded-md border px-5 py-4 flex items-center justify-between"
                    style={scoreGradientStyle(scores?.overall)}
                  >
                    <div>
                      <div className="text-sm font-semibold text-foreground">Overall score</div>
                      <div className="text-xs text-muted-foreground">
                        public tier for this address and its surroundings
                      </div>
                    </div>
                    <div className="text-right">
                      <span
                        className="ud-display text-3xl leading-none"
                        style={scoreTextStyle(scores?.overall)}
                      >
                        {preview?.score_uncertainty?.public_tier?.label ?? 'Not tiered'}
                      </span>
                      <div className="mt-1 font-mono text-[10px] text-muted-foreground">
                        {preview?.score_uncertainty?.public_tier
                          ? `95% range ${preview.score_uncertainty.score_range[0]}–${preview.score_uncertainty.score_range[1]} · point estimate ${formatScore(scores?.overall)}`
                          : `point estimate ${formatScore(scores?.overall)} · uncertainty unavailable`}
                      </div>
                    </div>
                  </div>
                  {/* Category scores — one per cell */}
                  {priorities.map((label) => {
                    const scoreMap: Record<string, number | null | undefined> = {
                      Amenities: scores?.amenities,
                      Transit: scores?.transit,
                      Safety: scores?.safety,
                    };
                    const Icon = PRIORITY_ICONS[label];
                    const val = scoreMap[label];
                    return (
                      <div
                        key={label}
                        className="rounded-md border px-4 py-3 flex items-center gap-3"
                        style={scoreGradientStyle(val)}
                      >
                        <Icon className="w-5 h-5 text-muted-foreground flex-shrink-0" />
                        <div className="min-w-0">
                          <div
                            className="ud-display text-[1.75rem] tabular-nums leading-none"
                            style={scoreTextStyle(val)}
                          >
                            {formatScore(val)}
                          </div>
                          <div className="text-xs text-muted-foreground mt-0.5">
                            {label}
                            {/* Evidence coverage: a one-source score must not
                                dress like a five-source one. Shown only when
                                thin, so full coverage stays quiet. */}
                            {(() => {
                              const cov = preview?.score_coverage?.[label.toLowerCase()];
                              if (!cov || cov.available == null || cov.total == null) return null;
                              if (cov.available >= cov.total) return null;
                              return (
                                <span
                                  className="ml-1.5 font-mono text-[10px] tabular-nums text-amber-700 dark:text-amber-500"
                                  title={`Missing: ${(cov.missing ?? []).join(', ')}`}
                                >
                                  {cov.available}/{cov.total} sources
                                </span>
                              );
                            })()}
                          </div>
                        </div>
                      </div>
                    );
                  })}
                  {typeof (metricScores?.nyccas_no ?? scores?.environment) === 'number' && (
                    <div
                      className="col-span-2 rounded-md border px-4 py-3 flex items-center gap-3"
                      style={scoreGradientStyle(metricScores?.nyccas_no ?? scores?.environment)}
                    >
                      <Wind className="h-5 w-5 shrink-0 text-muted-foreground" />
                      <div className="min-w-0 flex-1">
                        <div className="text-sm font-semibold text-foreground">
                          {environmentTier(metricScores?.nyccas_no ?? scores?.environment)}
                        </div>
                        <div className="mt-0.5 text-[11px] leading-snug text-muted-foreground">
                          NYCCAS 2023–24 annual-average model · context only, 0% of overall
                        </div>
                      </div>
                      <div className="text-right font-mono text-[10px] text-muted-foreground">
                        relative score {formatScore(metricScores?.nyccas_no ?? scores?.environment)}/100
                      </div>
                    </div>
                  )}
                  {typeof metricScores?.heat_vulnerability === 'number' && (
                    <div
                      className="col-span-2 rounded-md border px-4 py-3 flex items-center gap-3"
                      style={scoreGradientStyle(metricScores.heat_vulnerability)}
                    >
                      <Sun className="h-5 w-5 shrink-0 text-muted-foreground" />
                      <div className="min-w-0 flex-1">
                        <div className="text-sm font-semibold text-foreground">
                          {heatVulnerabilityTier(metricScores.heat_vulnerability)}
                        </div>
                        <div className="mt-0.5 text-[11px] leading-snug text-muted-foreground">
                          NYC DOHMH mortality-risk quintile · ZCTA 2020 · context only, 0% of overall
                        </div>
                      </div>
                      <div className="text-right font-mono text-[10px] text-muted-foreground">
                        relative score {formatScore(metricScores.heat_vulnerability)}/100
                      </div>
                    </div>
                  )}
                </div>

                {preview?.chart_specs?.score_composition && (
                  <VegaChart chart={preview.chart_specs.score_composition} />
                )}
                {preview?.chart_specs?.score_distribution && (
                  <VegaChart chart={preview.chart_specs.score_distribution} />
                )}

                {/* Building Risk Flag -- P0-02's resolution. Status colours
                    per the dataviz rules: reserved semantics, shipped with an
                    icon and a label, never colour alone. `none` renders as a
                    quiet line and `unknown` says "no data" -- a flag that
                    only ever appears when something is wrong trains people to
                    forget it exists. */}
                {preview?.building_risk_flag && (() => {
                  const flag = preview.building_risk_flag;
                  const STYLE: Record<string, { color: string; icon: string; label: string }> = {
                    serious: { color: '#d03b3b', icon: '⬢', label: 'Serious building risk' },
                    elevated: { color: '#ec835a', icon: '⬢', label: 'Elevated building risk' },
                    watch: { color: '#b8860b', icon: '⬡', label: 'Building watch' },
                    none: { color: 'var(--muted-foreground, #777)', icon: '○', label: 'No building risk signals' },
                    unknown: { color: 'var(--muted-foreground, #777)', icon: '◌', label: 'Building risk: no data' },
                  };
                  const style = STYLE[flag.level] ?? STYLE.unknown;
                  return (
                    <div
                      className="rounded-md border px-4 py-2.5 text-sm"
                      style={{ borderColor: flag.level === 'none' || flag.level === 'unknown' ? undefined : style.color }}
                      title={flag.reasons.join('; ')}
                    >
                      <span className="font-semibold" style={{ color: style.color }}>
                        {style.icon} {style.label}
                      </span>
                      {flag.level !== 'none' && flag.level !== 'unknown' && (
                        <div className="mt-1 text-xs text-muted-foreground">
                          {flag.reasons.join(' · ')}
                        </div>
                      )}
                    </div>
                  );
                })()}

                {/* Method footer: version + the door to item 1.6's page. */}
                <div className="flex items-center justify-between font-mono text-[10px] text-muted-foreground/70">
                  <span>
                    {preview?.score_uncertainty
                      ? `methodology v${preview.score_uncertainty.methodology_version} · percentile within NYC`
                      : 'percentile within NYC'}
                  </span>
                  <button
                    type="button"
                    onClick={() => setMethodologyOpen(true)}
                    className="underline decoration-dotted underline-offset-2 hover:text-foreground"
                  >
                    how scores work
                  </button>
                </div>

                {/* Neighborhood Insights — surfacing hidden backend data */}
                {!loading && preview && enrichedContext && (
                  <div className="space-y-3">
                    <h3 className="ud-label">
                      Neighborhood Insights
                    </h3>

                    {/* Why Now — trend alerts */}
                    {whyNow && whyNow.length > 0 && (
                      <div className="rounded-xl border border-amber-300/50 bg-amber-50/50 dark:bg-amber-950/20 px-4 py-3">
                        <div className="text-xs font-semibold text-amber-700 dark:text-amber-400 mb-1">Trend Alert</div>
                        {whyNow.map((w, i) => (
                          <div key={i} className="text-xs text-amber-800 dark:text-amber-300">
                            {w.signal} — {w.trend_type}
                          </div>
                        ))}
                      </div>
                    )}

                    {/* Key stats grid */}
                    <div className="grid grid-cols-2 gap-2">
                      {/* EMS response time */}
                      {currentState?.safety?.ems_avg_response_seconds != null && (
                        <div className="rounded-lg border bg-card px-3 py-2">
                          <div className="text-lg font-bold tabular-nums">
                            {Math.round(currentState.safety.ems_avg_response_seconds / 60)}m {Math.round(currentState.safety.ems_avg_response_seconds % 60)}s
                          </div>
                          <div className="text-[10px] text-muted-foreground">EMS Response Avg</div>
                        </div>
                      )}
                      {/* Fire response time */}
                      {currentState?.safety?.fire_avg_response_seconds != null && (
                        <div className="rounded-lg border bg-card px-3 py-2">
                          <div className="text-lg font-bold tabular-nums">
                            {Math.round(currentState.safety.fire_avg_response_seconds / 60)}m {Math.round(currentState.safety.fire_avg_response_seconds % 60)}s
                          </div>
                          <div className="text-[10px] text-muted-foreground">Fire Response Avg</div>
                        </div>
                      )}
                      {/* Trees */}
                      {currentState?.amenities?.tree_count_500m != null && (
                        <div className="rounded-lg border bg-card px-3 py-2">
                          <div className="text-lg font-bold tabular-nums">{currentState.amenities.tree_count_500m}</div>
                          <div className="text-[10px] text-muted-foreground">
                            Street Trees
                            {treeHealth ? ` (${Object.entries(treeHealth).map(([k, v]) => `${v} ${k.toLowerCase()}`).join(', ')})` : ''}
                          </div>
                        </div>
                      )}
                      {/* Parks */}
                      {currentState?.amenities?.park_acres_zip_proxy != null && currentState.amenities.park_acres_zip_proxy > 0 && (
                        <div className="rounded-lg border bg-card px-3 py-2">
                          <div className="text-lg font-bold tabular-nums">{currentState.amenities.park_acres_zip_proxy} ac</div>
                          <div className="text-[10px] text-muted-foreground">
                            Park Area{nearestParks?.length ? `: ${nearestParks.slice(0, 2).join(', ')}` : ''}
                          </div>
                        </div>
                      )}
                      {/* Restaurants */}
                      {currentState?.amenities?.restaurant_count_500m != null && currentState.amenities.restaurant_count_500m > 0 && (
                        <div className="rounded-lg border bg-card px-3 py-2">
                          <div className="text-lg font-bold tabular-nums">{currentState.amenities.restaurant_count_500m}</div>
                          <div className="text-[10px] text-muted-foreground">
                            Restaurants
                            {currentState.amenities.restaurant_critical_rate_500m > 0
                              ? ` (${Math.round(currentState.amenities.restaurant_critical_rate_500m * 100)}% critical)`
                              : ''}
                          </div>
                        </div>
                      )}
                      {/* Building violations age */}
                      {violationAge?.avg_age_days != null && violationAge.avg_age_days > 0 && (
                        <div className="rounded-lg border bg-card px-3 py-2">
                          <div className="text-lg font-bold tabular-nums">
                            {Math.round(violationAge.avg_age_days / 365)}yr
                          </div>
                          <div className="text-[10px] text-muted-foreground">
                            Avg Violation Age
                            {violationAge.older_than_2yr ? ` (${violationAge.older_than_2yr} &gt;2yr)` : ''}
                          </div>
                        </div>
                      )}
                    </div>

                    {/* Hotspot alert */}
                    {hotspots && hotspots.length > 0 && (
                      <div className="rounded-xl border border-red-300/50 bg-red-50/50 dark:bg-red-950/20 px-4 py-3">
                        <div className="text-xs font-semibold text-red-700 dark:text-red-400">
                          {hotspots.length} Incident Hotspot{hotspots.length > 1 ? 's' : ''} Detected
                        </div>
                        <div className="text-[10px] text-red-600 dark:text-red-300 mt-0.5">
                          Spatial clustering of incidents found within analysis radius
                        </div>
                      </div>
                    )}

                    {preview?.chart_specs?.recent_trends && (
                      <VegaChart chart={preview.chart_specs.recent_trends} />
                    )}
                  </div>
                )}

                {/* Priority actions */}
                {!loading && priorityActions.length > 0 && (
                  <div className="space-y-2">
                    <h3 className="ud-label">
                      Priority Actions
                    </h3>
                    <div className="space-y-2">
                      {priorityActions.slice(0, 3).map((action) => (
                        <div
                          key={`${action.signal}-${action.rank}`}
                          className="rounded-xl border border-border/50 bg-card px-4 py-3 hover:shadow-sm transition-shadow"
                        >
                          <div className="flex items-baseline gap-2">
                            <span className="text-xs font-bold text-primary">#{action.rank}</span>
                            <span className="text-sm font-medium">{action.action}</span>
                          </div>
                          <p className="text-xs text-muted-foreground mt-1 leading-relaxed">
                            {action.signal_description}
                          </p>
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {/* Report buttons — always visible */}
                <div className="space-y-3">
                  <h3 className="ud-label">
                    Generate Report
                  </h3>
                  <div className="flex items-center gap-2">
                    <Button
                      type="button"
                      onClick={() => handleGenerateReport('individual')}
                      disabled={reportLoading || loading || !preview}
                      variant={activeReportMode === 'individual' ? 'default' : 'outline'}
                      size="sm"
                      className="flex-1 rounded-lg h-9 text-xs font-medium"
                    >
                      {reportCache['individual'] ? 'View Individual' : 'Individual Report'}
                    </Button>
                    <Button
                      type="button"
                      onClick={() => handleGenerateReport('organization')}
                      disabled={reportLoading || loading || !preview}
                      variant={activeReportMode === 'organization' ? 'default' : 'outline'}
                      size="sm"
                      className="flex-1 rounded-lg h-9 text-xs font-medium"
                    >
                      {reportCache['organization'] ? 'View Organization' : 'Organization Report'}
                    </Button>
                  </div>
                  <Button
                    type="button"
                    onClick={handleExportHtml}
                    disabled={exportLoading || loading || !preview?.chart_specs}
                    variant="outline"
                    size="sm"
                    className="w-full rounded-lg h-9 text-xs font-medium"
                    aria-label="Download offline HTML report"
                  >
                    {exportLoading ? (
                      <Loader2 className="w-3.5 h-3.5 mr-1.5 animate-spin" />
                    ) : (
                      <Download className="w-3.5 h-3.5 mr-1.5" />
                    )}
                    Download Offline HTML
                  </Button>
                </div>

                {/* Error */}
                {error && (
                  <div className="rounded-xl border border-destructive/30 bg-destructive/5 px-4 py-3 text-sm text-destructive flex items-start gap-2">
                    <AlertCircle className="w-4 h-4 mt-0.5 flex-shrink-0" />
                    <span>{error}</span>
                  </div>
                )}

                {/* Building signals */}
                {buildingFlags.length > 0 && (
                  <div className="space-y-2">
                    <h3 className="ud-label">
                      Building Signals
                    </h3>
                    <div className="space-y-2">
                      {buildingFlags.slice(0, 3).map((flag, i) => (
                        <div
                          key={`${flag.bbl ?? 'building'}-${i}`}
                          className="rounded-xl border border-border/50 bg-card px-4 py-3 text-sm"
                        >
                          <span className="font-medium">{flag.summary}</span>
                          {flag.severity && (
                            <span className="ml-2 text-xs text-muted-foreground uppercase">
                              ({flag.severity})
                            </span>
                          )}
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {/* Evidence table */}
                {evidenceTable.length > 0 && (
                  <div className="space-y-2">
                    <h3 className="ud-label">
                      Evidence ({evidenceTable.length})
                    </h3>
                    <div className="space-y-2">
                      {evidenceTable.slice(0, 6).map((e) => (
                        <div
                          key={e.evidence_id}
                          className="rounded-xl border border-border/50 bg-card px-4 py-2.5"
                        >
                          <div className="flex justify-between gap-2 text-xs">
                            <span className="font-medium text-primary">{e.source}</span>
                            <span className="text-muted-foreground font-mono text-[10px]">
                              {e.date}
                            </span>
                          </div>
                          <p className="text-xs text-muted-foreground mt-1 leading-snug">
                            {summarizeEvidence(e)}
                          </p>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
              </>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Report Modal — centered overlay */}
      <AnimatePresence>
        {reportModalOpen && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm"
            onClick={() => setReportModalOpen(false)}
          >
            <motion.div
              initial={{ opacity: 0, scale: 0.95, y: 20 }}
              animate={{ opacity: 1, scale: 1, y: 0 }}
              exit={{ opacity: 0, scale: 0.95, y: 20 }}
              transition={{ type: 'spring', damping: 25, stiffness: 300 }}
              className="bg-background border border-border rounded-2xl shadow-2xl w-[90vw] max-w-[800px] max-h-[80vh] flex flex-col overflow-hidden"
              onClick={(e) => e.stopPropagation()}
            >
              {/* Modal header */}
              <div className="px-6 py-4 border-b border-border flex items-center justify-between flex-shrink-0">
                <div>
                  <h2 className="text-base font-semibold">
                    {activeReportMode === 'organization' ? 'Organization Report' : 'Individual Report'}
                  </h2>
                  <span className="text-xs text-muted-foreground">
                    {display?.title}
                  </span>
                </div>
                <div className="flex items-center gap-2">
                  {/* Toggle between report types */}
                  <div className="flex items-center gap-1 bg-muted/50 rounded-lg p-0.5">
                    <button
                      onClick={() => handleGenerateReport('individual')}
                      className={`px-3 py-1 rounded-md text-xs font-medium transition-all ${
                        activeReportMode === 'individual'
                          ? 'bg-background shadow-sm text-foreground'
                          : 'text-muted-foreground hover:text-foreground'
                      }`}
                    >
                      Individual
                    </button>
                    <button
                      onClick={() => handleGenerateReport('organization')}
                      className={`px-3 py-1 rounded-md text-xs font-medium transition-all ${
                        activeReportMode === 'organization'
                          ? 'bg-background shadow-sm text-foreground'
                          : 'text-muted-foreground hover:text-foreground'
                      }`}
                    >
                      Organization
                    </button>
                  </div>
                  <button
                    onClick={() => setReportModalOpen(false)}
                    className="p-2 hover:bg-muted rounded-md"
                  >
                    <X className="w-4 h-4" />
                  </button>
                </div>
              </div>

              {/* Modal body */}
              <div className="flex-1 overflow-y-auto px-8 py-6">
                {reportLoading ? (
                  <div className="flex flex-col items-center justify-center py-16 gap-3">
                    <Loader2 className="w-8 h-8 text-primary animate-spin" />
                    <span className="text-sm text-muted-foreground">Generating report...</span>
                  </div>
                ) : reportBlocks.length > 0 ? (
                  <div className="space-y-4 text-sm text-foreground/85 leading-7 max-w-[65ch] mx-auto">
                    {reportBlocks.map((block, i) => {
                      if (block.type === 'heading')
                        return <h3 key={i} className="text-lg font-semibold text-foreground pt-2">{block.content}</h3>;
                      if (block.type === 'subheading')
                        return <h4 key={i} className="text-base font-semibold text-foreground pt-1">{block.content}</h4>;
                      if (block.type === 'list')
                        return (
                          <ul key={i} className="list-disc space-y-2 pl-5">
                            {block.items!.map((item, j) => <li key={j}>{item}</li>)}
                          </ul>
                        );
                      return <p key={i} className="leading-7">{block.content}</p>;
                    })}
                  </div>
                ) : (
                  <div className="text-center py-16 text-sm text-muted-foreground">
                    No report content available.
                  </div>
                )}
              </div>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>
      {methodologyOpen && <MethodologyPanel onClose={() => setMethodologyOpen(false)} />}
    </div>
  );
}
