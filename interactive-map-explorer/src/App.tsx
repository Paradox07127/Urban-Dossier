/**
 * Urban Dossier NYC - Main Application
 */

import React, { useEffect, useMemo, useRef, useState } from 'react';
import { motion, AnimatePresence, Reorder } from 'motion/react';
import {
  AlertCircle,
  Bus,
  ChevronRight,
  Globe,
  Loader2,
  Maximize2,
  Minimize2,
  Pin,
  Search,
  ShieldCheck,
  Utensils,
  X,
} from 'lucide-react';

import MapComponent from './components/Map';
import AgentToggle from './components/AgentToggle';
import AgentPanel from './components/AgentPanel';
import type {
  DetailPreviewResponse,
  DetailResponse,
  EvidenceEntry,
  PriorityAction,
  RadiusMeters,
  Scores,
} from './types';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';

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

function priorityOrderKey(priorities: string[]): string[] {
  return priorities.map((p) => p.toLowerCase());
}

// Match the map heatmap gradient: red → orange → yellow → light-green → green
const GRADIENT_STOPS = [
  { at: 0,   r: 215, g: 48,  b: 39  }, // #d73027
  { at: 25,  r: 252, g: 141, b: 89  }, // #fc8d59
  { at: 50,  r: 254, g: 224, b: 139 }, // #fee08b
  { at: 75,  r: 145, g: 207, b: 96  }, // #91cf60
  { at: 100, r: 26,  g: 152, b: 80  }, // #1a9850
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
  return {
    backgroundColor: `rgba(${r}, ${g}, ${b}, 0.12)`,
    borderColor: `rgba(${r}, ${g}, ${b}, 0.35)`,
  };
}

function scoreTextStyle(score: number | null | undefined): React.CSSProperties {
  if (score == null) return {};
  const [r, g, b] = lerpGradient(score);
  // Darken for text readability
  return { color: `rgb(${Math.round(r * 0.7)}, ${Math.round(g * 0.7)}, ${Math.round(b * 0.7)})` };
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
  const [isSearchOpen, setIsSearchOpen] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const [priorities, setPriorities] = useState(['Amenities', 'Transit', 'Safety']);
  const [activePriority, setActivePriority] = useState<string | null>(null);
  const [selectedRadiusM, setSelectedRadiusM] = useState<RadiusMeters>(200);
  const [preview, setPreview] = useState<DetailPreviewResponse | null>(null);
  const [finalReport, setFinalReport] = useState<DetailResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [reportLoading, setReportLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Compare mode
  const [pinnedPreview, setPinnedPreview] = useState<DetailPreviewResponse | null>(null);
  const [pinnedTitle, setPinnedTitle] = useState<string>('');
  const [panelExpanded, setPanelExpanded] = useState(false);
  const [refreshKey, setRefreshKey] = useState(0);

  // Report cache: keep both types per location
  const [reportCache, setReportCache] = useState<Record<string, DetailResponse>>({});
  const [activeReportMode, setActiveReportMode] = useState<string | null>(null);
  const [reportModalOpen, setReportModalOpen] = useState(false);

  // Agent mode
  const [agentMode, setAgentMode] = useState(false);
  const [agentAvailable, setAgentAvailable] = useState(false);
  const [agentSessionId, setAgentSessionId] = useState<string | null>(null);

  const reportRef = useRef<HTMLDivElement>(null);

  const renderTag = (activePriority ? activePriority.toLowerCase() : 'general') as 'general' | 'safety' | 'transit' | 'amenities';
  const display = useMemo(
    () => (selectedTarget ? buildLocationDisplay(selectedTarget, preview) : null),
    [preview, selectedTarget],
  );
  const priorityOrder = useMemo(() => priorityOrderKey(priorities), [priorities]);

  // Zoom ref to avoid stale closure in onMapClick
  const zoomRef = useRef(zoom);
  useEffect(() => {
    zoomRef.current = zoom;
  }, [zoom]);

  // Check agent availability on mount
  useEffect(() => {
    fetch('/api/agent/status')
      .then((r) => r.json())
      .then((data) => setAgentAvailable(data.enabled))
      .catch(() => setAgentAvailable(false));
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

  const handleSearch = async () => {
    if (!searchQuery.trim()) return;
    setError(null);
    const q = searchQuery.trim();
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
    setIsSearchOpen(false);
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

  const handleClose = () => {
    setSelectedTarget(null);
    setPreview(null);
    setFinalReport(null);
    setError(null);
    setPanelExpanded(false);
    setReportModalOpen(false);
    setReportCache({});
    setActiveReportMode(null);
  };

  const handleGlobalView = () => {
    handleClose();
    setPinnedPreview(null);
    setPinnedTitle('');
    setActivePriority(null);
    setCenter(NYC_OVERVIEW);
    setZoom(NYC_OVERVIEW_ZOOM);
    setRefreshKey((k) => k + 1);
  };

  const scores = preview?.scores ?? display?.scores;
  const buildingFlags = preview?.detail_items?.building_flags ?? [];
  const evidenceTable = preview?.evidence_table ?? [];
  const priorityActions = preview?.priority_actions ?? [];

  // Extract hidden data for Neighborhood Insights
  const enrichedContext = preview?.enriched_context as Record<string, any> | undefined;
  const currentState = preview?.current_state as Record<string, any> | undefined;
  const trends = preview?.trends as Record<string, any> | undefined;
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
          hotspots={(hotspots as any[]) ?? []}
          onMarkerClick={handleMarkerClick}
          onMapClick={handleMapClick}
        />
      </div>

      {/* Bottom-left controls */}
      <div className="absolute bottom-6 left-6 z-30 flex flex-col items-start gap-4 max-w-[calc(100vw-3rem)]">
        {/* Search bar */}
        <AnimatePresence>
          {isSearchOpen && (
            <motion.div
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: 8 }}
              className="bg-background/95 backdrop-blur-md border border-border px-4 py-3 rounded-2xl shadow-lg w-[min(680px,calc(100vw-3rem))]"
            >
              <div className="flex items-center gap-3">
                <div className="relative flex-1">
                  <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-5 h-5 text-muted-foreground" />
                  <input
                    placeholder="Search address, landmark, or lat,lng..."
                    className="w-full pl-10 pr-3 h-12 rounded-xl text-base bg-muted/50 border-0 outline-none focus:ring-2 focus:ring-primary/30"
                    value={searchQuery}
                    onChange={(e) => setSearchQuery(e.target.value)}
                    onKeyDown={handleSearchKeyDown}
                    autoFocus
                  />
                </div>
                <Button type="button" onClick={handleSearch} size="sm" className="h-12 rounded-xl px-6 text-sm font-semibold">
                  Go
                </Button>
              </div>
              {error && (
                <p className="text-sm text-destructive mt-2 px-1">{error}</p>
              )}
            </motion.div>
          )}
        </AnimatePresence>

        <div className="flex items-center gap-3">
          {/* Search + Globe buttons */}
          <div className="flex gap-2">
            <motion.button
              whileTap={{ scale: 0.92 }}
              onClick={() => setIsSearchOpen(!isSearchOpen)}
              className={`w-12 h-12 rounded-full flex items-center justify-center shadow-lg transition-colors ${
                isSearchOpen
                  ? 'bg-destructive text-destructive-foreground'
                  : 'bg-primary text-primary-foreground'
              }`}
              title="Search"
            >
              {isSearchOpen ? <X className="w-5 h-5" /> : <Search className="w-5 h-5" />}
            </motion.button>
            <motion.button
              whileTap={{ scale: 0.92 }}
              onClick={handleGlobalView}
              className="w-12 h-12 rounded-full flex items-center justify-center shadow-lg bg-background/95 border border-border text-foreground hover:bg-muted"
              title="Global view"
            >
              <Globe className="w-5 h-5" />
            </motion.button>
          </div>

          {/* Priority reorder */}
          <div className="bg-background/95 backdrop-blur-md border border-border px-3 py-2 rounded-xl shadow-lg flex items-center gap-1.5">
            <Reorder.Group
              axis="x"
              values={priorities}
              onReorder={setPriorities}
              className="flex items-center gap-1.5"
            >
              {priorities.map((item) => {
                const Icon = PRIORITY_ICONS[item];
                const isActive = activePriority === item;
                return (
                  <Reorder.Item key={item} value={item}>
                    <button
                      onClick={() => {
                        const next = isActive ? null : item;
                        setActivePriority(next);
                        if (next && !selectedTarget) {
                          setCenter(NYC_OVERVIEW);
                          setZoom(NYC_OVERVIEW_ZOOM);
                        }
                      }}
                      className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg border text-sm transition-all cursor-pointer ${
                        isActive
                          ? 'bg-foreground text-background border-foreground'
                          : 'bg-muted/50 border-transparent text-foreground hover:bg-muted'
                      }`}
                    >
                      <Icon className={`w-4 h-4 ${isActive ? 'text-background' : 'text-primary'}`} />
                      <span className="font-medium">{item}</span>
                    </button>
                  </Reorder.Item>
                );
              })}
            </Reorder.Group>
          </div>

          {/* Radius selector */}
          <div className="bg-background/95 backdrop-blur-md border border-border px-3 py-2 rounded-xl shadow-lg flex items-center gap-1.5">
            {RADIUS_OPTIONS.map((r) => {
              const active = selectedRadiusM === r;
              return (
                <button
                  key={r}
                  type="button"
                  onClick={() => setSelectedRadiusM(r)}
                  className={`rounded-lg border px-3 py-1.5 text-sm font-medium transition-all ${
                    active
                      ? 'bg-foreground text-background border-foreground'
                      : 'bg-muted/50 border-transparent text-foreground hover:bg-muted'
                  }`}
                >
                  {r}m
                </button>
              );
            })}
          </div>
        </div>
      </div>

      {/* Detail Panel */}
      <AnimatePresence>
        {display && (
          <motion.div
            initial={{ x: '100%' }}
            animate={{ x: 0 }}
            exit={{ x: '100%' }}
            transition={{ type: 'spring', damping: 25, stiffness: 200 }}
            className={`absolute right-0 top-0 h-full w-full min-h-0 backdrop-blur-xl border-l border-border shadow-xl z-20 flex flex-col transition-all duration-300 ${
              panelExpanded ? 'md:w-[55vw]' : 'md:w-[680px]'
            }`}
            style={{ backgroundColor: `rgba(255,255,255,0.95)`, ...panelBgStyle }}
          >
            {/* Panel header */}
            <div className="px-5 py-4 flex items-start justify-between border-b border-border/60 gap-3">
              <div className="min-w-0 flex-1">
                <h2 className="text-base font-semibold leading-snug break-words">{display.title}</h2>
                <span className="text-xs text-muted-foreground">
                  {display.description || display.category}
                </span>
              </div>
              <div className="flex items-center gap-1.5 flex-shrink-0">
                <AgentToggle
                  enabled={agentMode}
                  onToggle={setAgentMode}
                  available={agentAvailable}
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
                      setPinnedPreview(preview);
                      setPinnedTitle(display?.title || 'Pinned');
                    }
                  }}
                  className={`p-2 hover:bg-muted rounded-md ${pinnedPreview ? 'text-primary' : ''}`}
                  title="Pin for compare"
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
                {pinnedPreview && preview && pinnedPreview !== preview && (
                  <div className="rounded-xl border border-primary/30 bg-primary/5 p-4 space-y-3">
                    <div className="flex items-center justify-between">
                      <span className="text-xs font-semibold uppercase tracking-wider text-primary">Compare</span>
                      <button onClick={() => setPinnedPreview(null)} className="text-xs text-muted-foreground hover:text-foreground">Clear pin</button>
                    </div>
                    <div className="grid grid-cols-3 gap-2 text-center text-xs">
                      <div className="font-medium text-muted-foreground truncate" title={pinnedTitle}>{pinnedTitle}</div>
                      <div></div>
                      <div className="font-medium text-muted-foreground truncate" title={display?.title}>{display?.title}</div>
                    </div>
                    {(['overall', 'safety', 'transit', 'amenities'] as const).map((cat) => {
                      const pScore = pinnedPreview.scores?.[cat as keyof Scores];
                      const cScore = scores?.[cat as keyof Scores];
                      const pVal = typeof pScore === 'number' ? pScore : null;
                      const cVal = typeof cScore === 'number' ? cScore : null;
                      const diff = pVal != null && cVal != null ? cVal - pVal : null;
                      return (
                        <div key={cat} className="grid grid-cols-3 gap-2 text-center text-sm tabular-nums">
                          <span className="font-bold" style={scoreTextStyle(pVal)}>{pVal ?? '--'}</span>
                          <span className="text-xs text-muted-foreground self-center capitalize">{cat}</span>
                          <span className="font-bold" style={scoreTextStyle(cVal)}>
                            {cVal ?? '--'}
                            {diff != null && diff !== 0 && (
                              <span className={`text-[10px] ml-1 ${diff > 0 ? 'text-green-600' : 'text-red-600'}`}>
                                {diff > 0 ? '+' : ''}{diff}
                              </span>
                            )}
                          </span>
                        </div>
                      );
                    })}
                  </div>
                )}

                {/* Score cards — 2x2 grid */}
                <div className="grid grid-cols-2 gap-3">
                  {/* Overall score - spans full width */}
                  <div
                    className="col-span-2 rounded-xl border px-5 py-4 flex items-center justify-between"
                    style={scoreGradientStyle(scores?.overall)}
                  >
                    <div>
                      <div className="text-sm font-semibold text-foreground">Overall Score</div>
                      <div className="text-xs text-muted-foreground">
                        {selectedRadiusM}m radius
                      </div>
                    </div>
                    <span
                      className="text-4xl font-bold tabular-nums"
                      style={scoreTextStyle(scores?.overall)}
                    >
                      {formatScore(scores?.overall)}
                    </span>
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
                        className="rounded-xl border px-4 py-3 flex items-center gap-3"
                        style={scoreGradientStyle(val)}
                      >
                        <Icon className="w-5 h-5 text-muted-foreground flex-shrink-0" />
                        <div className="min-w-0">
                          <div
                            className="text-2xl font-bold tabular-nums leading-none"
                            style={scoreTextStyle(val)}
                          >
                            {formatScore(val)}
                          </div>
                          <div className="text-xs text-muted-foreground mt-0.5">
                            {label}
                          </div>
                        </div>
                      </div>
                    );
                  })}
                </div>

                {/* Neighborhood Insights — surfacing hidden backend data */}
                {!loading && preview && enrichedContext && (
                  <div className="space-y-3">
                    <h3 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
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

                    {/* Trend sparklines — collision, rodent, violations */}
                    {trends && Object.keys(trends).length > 0 && (
                      <div className="space-y-2">
                        {(['collision', 'rodent', 'housing_violations'] as const).map((key) => {
                          const t = trends[key] as any;
                          if (!t?.raw_windows) return null;
                          const last30 = t.raw_windows.last_30d ?? 0;
                          const prev30 = t.raw_windows.prev_30d ?? 0;
                          const delta = last30 - prev30;
                          const arrow = delta > 0 ? '\u2191' : delta < 0 ? '\u2193' : '\u2192';
                          const color = delta > 0 ? 'text-red-600' : delta < 0 ? 'text-green-600' : 'text-muted-foreground';
                          const label = key === 'housing_violations' ? 'Violations' : key.charAt(0).toUpperCase() + key.slice(1) + 's';
                          // Sparkline from quarterly_series
                          const qs = (t.quarterly_series as Array<{ quarter: string; count: number }>) ?? [];
                          const recent = qs.slice(-20);
                          let sparkSvg: React.ReactNode = null;
                          if (recent.length >= 2) {
                            const vals = recent.map((q) => q.count);
                            const maxV = Math.max(...vals, 1);
                            const minV = Math.min(...vals, 0);
                            const range = maxV - minV || 1;
                            const w = 120, h = 28;
                            const pts = vals.map((v, i) => `${(i / (vals.length - 1)) * w},${h - ((v - minV) / range) * (h - 4) - 2}`).join(' ');
                            const strokeColor = delta > 0 ? '#dc2626' : delta < 0 ? '#16a34a' : '#6b7280';
                            sparkSvg = (
                              <svg width={w} height={h} className="flex-shrink-0">
                                <polyline points={pts} fill="none" stroke={strokeColor} strokeWidth="1.5" strokeLinejoin="round" />
                              </svg>
                            );
                          }
                          return (
                            <div key={key} className="flex items-center gap-2 px-1">
                              <span className="text-xs text-muted-foreground w-20 flex-shrink-0">{label}</span>
                              {sparkSvg}
                              <span className={`text-xs font-mono font-semibold ml-auto ${color}`}>
                                {last30} {arrow}
                              </span>
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </div>
                )}

                {/* Priority actions */}
                {!loading && priorityActions.length > 0 && (
                  <div className="space-y-2">
                    <h3 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
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
                  <h3 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
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
                    <h3 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
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
                    <h3 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
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
    </div>
  );
}
