/**
 * Step 3: crawl progress (the crawl_jobs counters, refreshed while it runs), and "read again".
 */
import { useState } from 'preact/hooks';

import { api, ApiError, crawlActive, type Crawl, type OnboardingState } from '../api';

/** Pages handled so far, out of the pages found. */
function progress(crawl: Crawl): { done: number; total: number } {
  const done = crawl.pages_indexed + crawl.pages_unchanged + crawl.pages_skipped + crawl.pages_failed;
  return { done, total: Math.max(crawl.pages_found, done) };
}

/** What the crawl is doing, in words. */
function describe(crawl: Crawl): string {
  const { done, total } = progress(crawl);
  if (crawl.status === 'queued') return 'Waiting to start…';
  if (crawl.status === 'running') return total ? `Reading page ${done} of ${total}…` : 'Finding your pages…';
  if (crawl.status === 'failed') return "We couldn't read your website.";
  return `Learned ${crawl.pages_indexed + crawl.pages_unchanged} pages.`;
}

/** Progress bar + summary; `compact` for the sidebar. */
export function CrawlMeter({ crawl, compact = false }: { crawl: Crawl; compact?: boolean }) {
  const { done, total } = progress(crawl);
  const percent = crawl.status === 'done' ? 100 : total ? Math.round((done / total) * 100) : 5;
  return (
    <div class={`meter${compact ? ' compact' : ''}`}>
      <div class="meter-label small">{describe(crawl)}</div>
      <div class="meter-track" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent}>
        <div class={`meter-fill ${crawl.status}`} style={{ width: `${percent}%` }} />
      </div>
    </div>
  );
}

interface Props {
  state: OnboardingState;
  onUpdate: (state: OnboardingState) => void;
  onNext: () => void;
}

export function LearningStep({ state, onUpdate, onNext }: Props) {
  const [error, setError] = useState<string | null>(null);
  const crawl = state.crawl;
  const active = crawlActive(crawl);

  const again = async () => {
    setError(null);
    try {
      onUpdate(await api.crawlAgain());
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Something went wrong.');
    }
  };

  return (
    <section>
      <p class="eyebrow">Step 3</p>
      <h1>Learning your website</h1>
      <p class="muted">We read up to {state.limits.max_pages} pages of {state.site_url}. This usually takes under a minute.</p>
      {crawl ? <CrawlMeter crawl={crawl} /> : <p class="muted">Not started yet.</p>}
      {crawl?.status === 'done' && (
        <p class="muted small">
          {crawl.pages_skipped ? `${crawl.pages_skipped} pages skipped (not public, empty or need JavaScript). ` : ''}
          {crawl.pages_failed ? `${crawl.pages_failed} pages couldn't be opened.` : ''}
        </p>
      )}
      {crawl?.status === 'failed' && <p class="error">{crawl.error}</p>}
      {crawl?.status === 'queued' && (
        <p class="muted small">If this doesn't start within a minute, the crawler isn't running (python worker.py).</p>
      )}
      {error && <p class="error" role="alert">{error}</p>}
      <div class="row">
        <button type="button" class="primary" disabled={!crawl || crawl.status !== 'done'} onClick={onNext}>
          Try your assistant
        </button>
        <button type="button" class="secondary" disabled={active || state.limits.crawls_left_today === 0}
          onClick={() => void again()}>
          Read my site again ({state.limits.crawls_left_today} left today)
        </button>
      </div>
    </section>
  );
}
