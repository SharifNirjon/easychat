/**
 * Step 1: the owner's website. One URL starts everything (branding, site key, first crawl);
 * a sitemap file can be uploaded instead when the site has none we can find.
 */
import { useState } from 'preact/hooks';

import { api, ApiError, type OnboardingState } from '../api';

const MAX_SITEMAP_BYTES = 1_000_000;          // the API's upload cap (onboarding.SITEMAP_UPLOAD_MAX_BYTES)

interface Props {
  state: OnboardingState;
  onUpdate: (state: OnboardingState) => void;
  onNext: () => void;
}

export function WebsiteStep({ state, onUpdate, onNext }: Props) {
  const [url, setUrl] = useState(state.site_url ?? '');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  /** Run an API call that returns the new state; show its error, if any. */
  const run = async (label: string, action: () => Promise<OnboardingState>, then?: () => void) => {
    setBusy(label);
    setError(null);
    try {
      onUpdate(await action());
      then?.();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Something went wrong.');
    } finally {
      setBusy(null);
    }
  };

  const submit = (event: Event) => {
    event.preventDefault();
    void run('Reading your website…', () => api.setSite(url.trim()), onNext);
  };

  const upload = async (event: Event) => {
    const file = (event.target as HTMLInputElement).files?.[0];
    if (!file) return;
    if (file.size > MAX_SITEMAP_BYTES) {
      setError('That file is too large (1 MB at most).');
      return;
    }
    await run('Reading your sitemap…', async () => api.uploadSitemap(await file.text()));
  };

  return (
    <section>
      <p class="eyebrow">Step 1</p>
      <h1>What's your website?</h1>
      <p class="muted">We'll read it to learn your business, and pick up your name and brand colour.</p>
      <form onSubmit={submit} class="row">
        <label class="field grow">
          <span>Website address</span>
          <input type="text" inputMode="url" required placeholder="https://myshop.com" value={url} maxLength={2000}
            onInput={(e) => setUrl((e.target as HTMLInputElement).value)} />
        </label>
        <button type="submit" class="primary" disabled={busy !== null}>{state.site_url ? 'Update' : 'Continue'}</button>
      </form>
      {busy && <p class="muted" aria-live="polite">{busy}</p>}
      {error && <p class="error" role="alert">{error}</p>}

      {state.site_url && (
        <details class="panel">
          <summary>Pages missing? Upload your sitemap.xml</summary>
          <p class="muted small">
            We look for a sitemap automatically. If your site has none we can find, upload the file
            and we'll read exactly those pages (up to {state.limits.max_pages}, on {state.site_url} only).
          </p>
          <input type="file" accept=".xml,application/xml,text/xml" disabled={busy !== null}
            onChange={(e) => void upload(e)} />
        </details>
      )}
    </section>
  );
}
