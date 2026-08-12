import { ArrowLeft, ShieldCheck } from 'lucide-react';
import { useEffect } from 'react';
import {
  MethodologyContent,
  useMethodologyPublication,
} from './components/MethodologyContent';

export default function MethodologyPage() {
  const { publication, error } = useMethodologyPublication();
  useEffect(() => {
    document.title = 'Scoring methodology · Urban Dossier';
  }, []);

  return (
    <main className="min-h-full bg-background text-foreground">
      <header className="border-b border-border bg-card">
        <div className="mx-auto flex max-w-6xl items-start justify-between gap-6 px-5 py-6 sm:px-8">
          <div>
            <a href="/" className="mb-5 inline-flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground">
              <ArrowLeft className="h-3.5 w-3.5" /> Back to map
            </a>
            <div className="ud-label">Urban Dossier / statistical audit</div>
            <h1 className="ud-display mt-2 text-3xl sm:text-4xl">Scoring methodology</h1>
            <p className="mt-2 max-w-2xl text-sm leading-relaxed text-muted-foreground">
              A live projection of the scoring registry and prepared-data coverage,
              published only after its version is checked against the running code.
            </p>
          </div>
          {publication && (
            <div data-testid="methodology-version-verified" className="mt-7 hidden shrink-0 items-center gap-2 rounded-md border border-border bg-background px-3 py-2 text-xs text-foreground sm:flex">
              <ShieldCheck className="h-4 w-4" />
              code = registry = v{publication.methodology_version}
            </div>
          )}
        </div>
      </header>
      <div className="mx-auto max-w-6xl px-5 py-8 sm:px-8">
        {error && (
          <div role="alert" data-testid="methodology-withheld" className="rounded-md border border-amber-400/50 bg-amber-50 p-5 text-sm text-amber-950 dark:bg-amber-950/20 dark:text-amber-100">
            <h2 className="font-semibold">Methodology publication withheld</h2>
            <p className="mt-2 text-xs leading-relaxed">{error}</p>
          </div>
        )}
        {!publication && !error && <p className="text-sm text-muted-foreground">Verifying methodology and dataset coverage…</p>}
        {publication && <MethodologyContent publication={publication} />}
      </div>
    </main>
  );
}
