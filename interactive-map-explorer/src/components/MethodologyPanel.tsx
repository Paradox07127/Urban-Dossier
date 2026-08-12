import { ExternalLink, X } from 'lucide-react';
import {
  MethodologyContent,
  useMethodologyPublication,
} from './MethodologyContent';

export default function MethodologyPanel({ onClose }: { onClose: () => void }) {
  const { publication, error } = useMethodologyPublication();
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label="Scoring methodology"
    >
      <div
        className="flex max-h-[90vh] w-full max-w-5xl flex-col overflow-hidden rounded-xl border border-border bg-background shadow-2xl"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-border px-5 py-3">
          <div>
            <h2 className="text-sm font-semibold text-foreground">How the scores work</h2>
            {publication && (
              <div className="font-mono text-[11px] text-muted-foreground">
                methodology v{publication.methodology_version} · request-verified
              </div>
            )}
          </div>
          <div className="flex items-center gap-2">
            <a
              href="/methodology"
              className="inline-flex items-center gap-1 rounded px-2 py-1 text-xs text-muted-foreground hover:bg-muted hover:text-foreground"
            >
              shareable page <ExternalLink className="h-3 w-3" />
            </a>
            <button
              type="button"
              onClick={onClose}
              aria-label="Close"
              className="rounded p-1 text-muted-foreground hover:text-foreground"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        </div>
        <div className="overflow-y-auto px-5 py-5">
          {error && (
            <p role="alert" className="rounded-md border border-amber-400/50 bg-amber-50 p-3 text-sm text-amber-900 dark:bg-amber-950/20 dark:text-amber-200">
              Methodology publication withheld: {error}
            </p>
          )}
          {!publication && !error && <p className="text-sm text-muted-foreground">Verifying methodology…</p>}
          {publication && <MethodologyContent publication={publication} />}
        </div>
      </div>
    </div>
  );
}
