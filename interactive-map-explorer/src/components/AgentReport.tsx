import React, { useState } from 'react';
import { motion, AnimatePresence } from 'motion/react';
import { ExternalLink, Download, Loader2, Send } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';

interface Props {
  sessionId: string | null;
  onCreateSession: () => Promise<string>;
  mode: 'report' | 'poster';
}

const FOCUS_OPTIONS = ['All', 'Safety', 'Transit', 'Amenities', 'Building'] as const;

const POSTER_TEMPLATES = [
  { id: 'card' as const, label: 'Card', color: 'bg-teal-500/20 border-teal-500/30' },
  { id: 'offline' as const, label: 'Offline', color: 'bg-blue-500/20 border-blue-500/30' },
  { id: 'horizontal' as const, label: 'Horizontal', color: 'bg-emerald-500/20 border-emerald-500/30' },
  { id: 'analytical' as const, label: 'Analytical', color: 'bg-violet-500/20 border-violet-500/30' },
] as const;

export default function AgentReport({ sessionId, onCreateSession, mode }: Props) {
  const [loading, setLoading] = useState(false);
  const [statusText, setStatusText] = useState('');
  const [resultHtml, setResultHtml] = useState<string | null>(null);
  const [focus, setFocus] = useState<string>('All');
  const [selectedTemplate, setSelectedTemplate] = useState<'card' | 'offline' | 'horizontal' | 'analytical'>('card');
  const [refineInput, setRefineInput] = useState('');
  const [refining, setRefining] = useState(false);

  const handleGenerate = async () => {
    setLoading(true);
    const label = mode === 'report' ? 'Analyzing neighborhood' : 'Generating poster';
    setStatusText(`${label}...`);
    setResultHtml(null);
    const t0 = Date.now();
    const ticker = setInterval(() => {
      const elapsed = Math.round((Date.now() - t0) / 1000);
      setStatusText(`${label}... (${elapsed}s)`);
    }, 3000);
    try {
      let sid = sessionId;
      if (!sid) {
        sid = await onCreateSession();
      }

      const endpoint = mode === 'report' ? '/api/agent/report' : '/api/agent/poster';
      const body =
        mode === 'report'
          ? { session_id: sid, ...(focus !== 'All' ? { focus: focus.toLowerCase() } : {}) }
          : { session_id: sid, template: selectedTemplate };

      const resp = await fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!resp.ok) {
        const errBody = await resp.json().catch(() => ({}));
        throw new Error(errBody?.detail || errBody?.error || `Request failed (${resp.status})`);
      }
      const data = await resp.json();
      setResultHtml(data.html);
    } catch (err) {
      setStatusText(`Error: ${err instanceof Error ? err.message : 'Generation failed.'}`);
    } finally {
      clearInterval(ticker);
      setLoading(false);
    }
  };

  const handleRefine = async () => {
    if (!refineInput.trim() || !sessionId) return;
    setRefining(true);
    try {
      const resp = await fetch('/api/agent/refine', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: sessionId, feedback: refineInput.trim() }),
      });
      if (!resp.ok) {
        const errBody = await resp.json().catch(() => ({}));
        throw new Error(errBody?.detail || errBody?.error || `Refine failed (${resp.status})`);
      }
      const data = await resp.json();
      setResultHtml(data.html);
      setRefineInput('');
    } catch (err) {
      setStatusText(`Refine error: ${err instanceof Error ? err.message : 'Failed.'}`);
    } finally {
      setRefining(false);
    }
  };

  const openInNewTab = () => {
    if (!resultHtml) return;
    const blob = new Blob([resultHtml], { type: 'text/html' });
    const url = URL.createObjectURL(blob);
    window.open(url, '_blank');
    setTimeout(() => URL.revokeObjectURL(url), 5000);
  };

  const downloadHtml = () => {
    if (!resultHtml) return;
    const blob = new Blob([resultHtml], { type: 'text/html' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `urban-dossier-${mode}-${Date.now()}.html`;
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="flex flex-col h-full min-h-0 p-4 space-y-4">
      {/* Controls */}
      {!resultHtml && (
        <div className="space-y-3">
          {mode === 'report' && (
            <div className="space-y-1.5">
              <label className="text-xs font-medium text-muted-foreground">Focus area</label>
              <div className="flex flex-wrap gap-1.5">
                {FOCUS_OPTIONS.map((opt) => (
                  <button
                    key={opt}
                    type="button"
                    onClick={() => setFocus(opt)}
                    className={`px-2.5 py-1 rounded-lg text-xs font-medium border transition-colors ${
                      focus === opt
                        ? 'bg-foreground text-background border-foreground'
                        : 'bg-muted/50 border-transparent text-foreground hover:bg-muted'
                    }`}
                  >
                    {opt}
                  </button>
                ))}
              </div>
            </div>
          )}

          {mode === 'poster' && (
            <div className="space-y-1.5">
              <label className="text-xs font-medium text-muted-foreground">Template</label>
              <div className="grid grid-cols-4 gap-2">
                {POSTER_TEMPLATES.map((tpl) => (
                  <button
                    key={tpl.id}
                    type="button"
                    onClick={() => setSelectedTemplate(tpl.id)}
                    className={`rounded-xl border-2 p-3 text-center transition-all ${
                      selectedTemplate === tpl.id
                        ? 'border-foreground ring-1 ring-foreground/20'
                        : 'border-border hover:border-foreground/30'
                    }`}
                  >
                    <div className={`h-12 rounded-lg border ${tpl.color} mb-2`} />
                    <span className="text-xs font-medium">{tpl.label}</span>
                  </button>
                ))}
              </div>
            </div>
          )}

          <Button
            type="button"
            onClick={handleGenerate}
            disabled={loading}
            className="w-full rounded-lg h-9 text-sm font-medium"
          >
            {loading ? (
              <span className="flex items-center gap-2">
                <Loader2 className="w-4 h-4 animate-spin" />
                {statusText}
              </span>
            ) : mode === 'report' ? (
              'Generate Deep Report'
            ) : (
              'Generate Poster'
            )}
          </Button>
        </div>
      )}

      {/* Loading state (when no result yet) */}
      <AnimatePresence>
        {loading && !resultHtml && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="flex flex-col items-center justify-center py-12 gap-3"
          >
            <Loader2 className="w-8 h-8 text-primary animate-spin" />
            <span className="text-sm text-muted-foreground">{statusText}</span>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Error display (when loading finished with error) */}
      {!loading && !resultHtml && statusText.startsWith('Error') && (
        <div className="rounded-xl border border-destructive/30 bg-destructive/5 px-4 py-3 text-sm text-destructive">
          {statusText}
        </div>
      )}

      {/* Result iframe */}
      {resultHtml && (
        <div className="flex-1 min-h-0 flex flex-col gap-3">
          <div className="flex-1 min-h-0 rounded-lg border border-border overflow-hidden">
            <iframe
              srcDoc={resultHtml}
              sandbox=""
              title={mode === 'report' ? 'Deep Report' : 'Poster'}
              className="w-full h-full min-h-[500px] bg-white"
            />
          </div>

          {/* Action buttons */}
          <div className="flex items-center gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={openInNewTab}
              className="rounded-lg text-xs h-7"
            >
              <ExternalLink className="w-3.5 h-3.5 mr-1" />
              Open in New Tab
            </Button>
            {mode === 'poster' && (
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={downloadHtml}
                className="rounded-lg text-xs h-7"
              >
                <Download className="w-3.5 h-3.5 mr-1" />
                Download
              </Button>
            )}
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => {
                setResultHtml(null);
                setStatusText('');
              }}
              className="rounded-lg text-xs h-7 ml-auto"
            >
              Regenerate
            </Button>
          </div>

          {/* Refine input (report mode only) */}
          {mode === 'report' && (
            <div className="flex items-center gap-2">
              <Input
                placeholder="Refine: e.g. Focus more on transit data"
                className="flex-1 h-8 text-xs rounded-lg"
                value={refineInput}
                onChange={(e) => setRefineInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') handleRefine();
                }}
                disabled={refining}
              />
              <Button
                type="button"
                size="icon-sm"
                onClick={handleRefine}
                disabled={!refineInput.trim() || refining}
                className="rounded-lg flex-shrink-0"
              >
                {refining ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Send className="w-3.5 h-3.5" />}
              </Button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
