import React, { useEffect, useMemo, useRef, useState } from 'react';
import { motion, AnimatePresence } from 'motion/react';
import { ArrowUp } from 'lucide-react';
import { Input } from '@/components/ui/input';
import ProvenanceThread from './ProvenanceThread';
import type { AgentChatMessage, AgentTrace, DetailPreviewResponse } from '../types';

interface Props {
  sessionId: string | null;
  analysisPayload: DetailPreviewResponse | null;
  onCreateSession: () => Promise<string>;
  /** Selected point, so the agent knows what "here" refers to. */
  target?: { latitude: number; longitude: number; label?: string } | null;
  /** Hands a computed isochrone up to the map. */
  onIsochrone?: (feature: any | null) => void;
  toolAvailability?: Record<string, { available: boolean; reason: string }>;
}

/**
 * Questions that exercise what this agent can actually do. A suggestion that
 * the tools cannot answer teaches the wrong thing about the product, so each
 * of these maps to a real capability: scoring, routing, and scenario
 * projection.
 */
const SUGGESTED = [
  { label: 'What drives the safety score here?', tool: 'score_neighborhood' },
  { label: 'How far can I walk in 10 minutes?', tool: 'walking_isochrone' },
  { label: 'What would 3 more public toilets change?', tool: 'simulate_intervention' },
];

const TOOL_LABELS: Record<string, string> = {
  find_similar_neighborhoods: 'similar-neighborhood search',
  walking_isochrone: 'walking routes',
  simulate_intervention: 'intervention projections',
};

/**
 * Strip machine residue from the answer before it is read.
 *
 * The answer prompt asks for a citation list, and the model sometimes emits it
 * as a raw JSON array or repeats the FINAL_ANSWER marker it was given. Both are
 * artefacts of the instruction rather than anything a reader asked for, and the
 * evidence they encode is already rendered properly by the provenance thread.
 * Only trailing blocks are removed, so prose that merely contains a brace is
 * left alone.
 */
function cleanAnswer(text: string): string {
  let out = (text ?? '').trim();
  out = out.replace(/^\s*\**\s*FINAL[_ ]ANSWER\s*:?\s*\**\s*/i, '');
  // Trailing JSON array or object, optionally fenced.
  out = out.replace(/\n*```(?:json)?\s*[\[{][\s\S]*?[\]}]\s*```\s*$/i, '');
  out = out.replace(/\n*\[\s*\{[\s\S]*\}\s*\]\s*$/, '');
  out = out.replace(/\n*\{\s*"(?:evidence|citations)"[\s\S]*\}\s*$/i, '');
  return out.trim();
}

function renderSimpleMarkdown(text: string): React.ReactNode[] {
  const lines = text.split('\n');
  const elements: React.ReactNode[] = [];
  let listItems: string[] = [];

  const flushList = () => {
    if (listItems.length > 0) {
      elements.push(
        <ul key={`ul-${elements.length}`} className="list-disc pl-4 space-y-1">
          {listItems.map((item, i) => (
            <li key={i}>{boldify(item)}</li>
          ))}
        </ul>,
      );
      listItems = [];
    }
  };

  for (const line of lines) {
    const trimmed = line.trim();
    if (trimmed.startsWith('- ') || trimmed.startsWith('* ')) {
      listItems.push(trimmed.replace(/^[-*]\s+/, ''));
    } else {
      flushList();
      if (trimmed) {
        elements.push(<p key={`p-${elements.length}`}>{boldify(trimmed)}</p>);
      }
    }
  }
  flushList();
  return elements;
}

function boldify(text: string): React.ReactNode {
  const parts = text.split(/(\*\*[^*]+\*\*)/g);
  return parts.map((part, i) =>
    part.startsWith('**') && part.endsWith('**') ? (
      <strong key={i} className="font-semibold">
        {part.slice(2, -2)}
      </strong>
    ) : (
      part
    ),
  );
}

/**
 * Waiting state. A tool-using turn can run for a minute across several model
 * calls, and a spinner that says nothing for that long reads as a hang. The
 * elapsed count is the honest thing available without streaming: it confirms
 * work is happening and sets expectations.
 */
function Working({ startedAt }: { startedAt: number }) {
  const [elapsed, setElapsed] = useState(0);
  useEffect(() => {
    const id = window.setInterval(
      () => setElapsed(Math.floor((Date.now() - startedAt) / 1000)),
      1000,
    );
    return () => window.clearInterval(id);
  }, [startedAt]);

  return (
    <div className="flex items-center gap-2.5 py-1">
      <span className="relative flex h-1.5 w-1.5" aria-hidden="true">
        <motion.span
          className="absolute inline-flex h-full w-full rounded-full bg-foreground"
          animate={{ opacity: [1, 0.25, 1] }}
          transition={{ duration: 1.4, repeat: Infinity }}
        />
      </span>
      <span className="ud-label">Consulting the data</span>
      <span className="font-mono text-[10px] text-muted-foreground tabular-nums">
        {elapsed}s
      </span>
    </div>
  );
}

export default function AgentChat({
  sessionId,
  analysisPayload,
  onCreateSession,
  target,
  onIsochrone,
  toolAvailability,
}: Props) {
  const [messages, setMessages] = useState<AgentChatMessage[]>([]);
  const [input, setInput] = useState('');
  const [sending, setSending] = useState(false);
  const [startedAt, setStartedAt] = useState(0);
  const scrollRef = useRef<HTMLDivElement>(null);
  const hasStarted = messages.length > 0;
  const suggestions = SUGGESTED.filter(
    (item) => toolAvailability?.[item.tool]?.available !== false,
  );
  const unavailableLabels = Object.entries(toolAvailability ?? {})
    .filter(([, state]) => !state.available)
    .map(([name]) => TOOL_LABELS[name] ?? name);

  const resolvedTarget = useMemo(() => {
    if (target) return target;
    const t = analysisPayload?.target;
    if (t?.latitude != null && t?.longitude != null) {
      return {
        latitude: t.latitude,
        longitude: t.longitude,
        label: t.matched_address ?? undefined,
      };
    }
    return null;
  }, [target, analysisPayload]);

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages, sending]);

  const sendMessage = async (text: string) => {
    const trimmed = text.trim();
    if (!trimmed || sending) return;

    setMessages((prev) => [
      ...prev,
      { role: 'user', content: trimmed, timestamp: Date.now() },
    ]);
    setInput('');
    setSending(true);
    setStartedAt(Date.now());

    try {
      let sid = sessionId;
      if (!sid) sid = await onCreateSession();

      // The agent fetches its own evidence, but it still needs to know which
      // point the map has selected. This goes through the API's history field
      // rather than being pasted into the user's message, so what the reader
      // sees stays what the reader typed.
      const history = resolvedTarget
        ? [
            {
              role: 'user',
              content:
                `Context: the location under discussion is ` +
                `${resolvedTarget.latitude.toFixed(5)}, ${resolvedTarget.longitude.toFixed(5)}` +
                `${resolvedTarget.label ? ` (${resolvedTarget.label})` : ''}. ` +
                `Use it for any tool that needs coordinates unless I name another place.`,
            },
          ]
        : [];

      const resp = await fetch('/api/agent/ask', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message: trimmed,
          session_id: sid,
          history,
          max_iterations: 6,
        }),
      });

      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}));
        throw new Error(
          body?.error || body?.detail || `The agent could not complete that request (${resp.status}).`,
        );
      }

      const data = await resp.json();
      const trace: AgentTrace[] = data.trace ?? [];

      setMessages((prev) => [
        ...prev,
        {
          role: 'assistant',
          content: cleanAnswer(data.answer ?? ''),
          timestamp: Date.now(),
          trace,
          evidence: data.evidence ?? [],
          iterations: data.iterations,
        },
      ]);

      // Anything spatial the agent computed belongs on the map, not buried in
      // a JSON blob.
      const iso = [...trace]
        .reverse()
        .find((t) => t.tool_name === 'walking_isochrone' && !t.result?.error);
      if (iso && onIsochrone) onIsochrone(iso.result);
    } catch (err) {
      setMessages((prev) => [
        ...prev,
        {
          role: 'assistant',
          content:
            err instanceof Error
              ? err.message
              : 'The agent could not complete that request.',
          timestamp: Date.now(),
          failed: true,
        },
      ]);
    } finally {
      setSending(false);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage(input);
    }
  };

  return (
    <div className="flex flex-col h-full min-h-0">
      <div ref={scrollRef} className="flex-1 min-h-0 overflow-y-auto px-5 py-4">
        <AnimatePresence mode="popLayout">
          {!hasStarted && !sending && (
            <motion.div
              key="empty"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              className="py-6"
            >
              <h3 className="ud-display text-[1.35rem] leading-tight text-foreground">
                Ask about this place.
              </h3>
              <p className="mt-2 text-sm leading-relaxed text-muted-foreground max-w-[42ch]">
                The agent queries the city datasets directly and shows every call
                it made. Scores come from the data, not from the model.
              </p>

              {unavailableLabels.length > 0 && (
                <p className="mt-3 text-xs leading-relaxed text-muted-foreground max-w-[52ch]">
                  Not enabled in this deployment: {unavailableLabels.join(', ')}.
                </p>
              )}

              {/* Kept next to the invitation rather than pinned above the input:
                  these are examples of what to ask, so they belong with the
                  sentence that asks. */}
              <ul className="mt-5 space-y-1.5">
                {suggestions.map((q) => (
                  <li key={q.label}>
                    <button
                      type="button"
                      onClick={() => sendMessage(q.label)}
                      disabled={sending}
                      className="flex w-full items-center gap-3 rounded-md border border-border bg-card px-3 py-2.5 text-left transition-colors hover:bg-muted disabled:opacity-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
                    >
                      <span className="text-[13px] text-foreground">{q.label}</span>
                      <span className="ml-auto font-mono text-[10px] uppercase tracking-wider text-muted-foreground">
                        {q.tool}
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            </motion.div>
          )}

          {messages.map((msg, i) =>
            msg.role === 'user' ? (
              <motion.div
                key={`${msg.timestamp}-${i}`}
                initial={{ opacity: 0, y: 6 }}
                animate={{ opacity: 1, y: 0 }}
                className="mb-4 flex justify-end"
              >
                <p className="max-w-[85%] rounded-md bg-foreground px-3 py-2 text-sm leading-relaxed text-background">
                  {msg.content}
                </p>
              </motion.div>
            ) : (
              <motion.div
                key={`${msg.timestamp}-${i}`}
                initial={{ opacity: 0, y: 6 }}
                animate={{ opacity: 1, y: 0 }}
                className="mb-6"
              >
                <div
                  className={`space-y-2 text-sm leading-relaxed ${
                    msg.failed ? 'text-muted-foreground' : 'text-foreground'
                  }`}
                >
                  {msg.failed ? <p>{msg.content}</p> : renderSimpleMarkdown(msg.content)}
                </div>
                {msg.trace && msg.trace.length > 0 && (
                  <ProvenanceThread trace={msg.trace} />
                )}
              </motion.div>
            ),
          )}

          {sending && (
            <motion.div key="working" initial={{ opacity: 0 }} animate={{ opacity: 1 }}>
              <Working startedAt={startedAt} />
            </motion.div>
          )}
        </AnimatePresence>
      </div>

      <div className="border-t border-border px-4 py-3">
        <div className="flex items-center gap-2">
          <Input
            placeholder="Ask about this neighborhood"
            className="h-9 flex-1 rounded-md border-border bg-card text-sm"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            disabled={sending}
            maxLength={2000}
            aria-label="Ask about this neighborhood"
          />
          <button
            type="button"
            onClick={() => sendMessage(input)}
            disabled={!input.trim() || sending}
            aria-label="Send question"
            className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-foreground text-background transition-opacity disabled:opacity-30 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
          >
            <ArrowUp className="h-4 w-4" />
          </button>
        </div>
      </div>
    </div>
  );
}
