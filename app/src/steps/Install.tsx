/**
 * Step 5: the script tag to paste, and "Installed ✓" once the widget is seen on the owner's site
 * (the app polls while waiting; the API records the first load from an allowed origin).
 */
import { useState } from 'preact/hooks';

import type { OnboardingState } from '../api';

const LOCAL_DEMO_SITE = 'http://localhost:3001';   // the widget's dev demo server (widget/demo)

export function InstallStep({ state }: { state: OnboardingState }) {
  const [copied, setCopied] = useState(false);
  if (!state.snippet || !state.site_key) return <p class="muted">Add your website first.</p>;

  const copy = async () => {
    await navigator.clipboard.writeText(state.snippet!);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <section>
      <p class="eyebrow">Step 5</p>
      <h1>{state.installed_at ? 'Installed ✓' : 'Put it on your website'}</h1>
      {state.installed_at ? (
        <div class="success">
          <p><strong>{state.assistant.bot_name} is live on your website.</strong></p>
          <p class="muted small">First seen {new Date(state.installed_at).toLocaleString()}.</p>
        </div>
      ) : (
        <p class="muted">
          Paste this line into your website's HTML, just before <code>&lt;/body&gt;</code>, on every page
          (most site builders have a "custom code" or "footer scripts" setting).
        </p>
      )}
      <div class="snippet">
        <pre><code>{state.snippet}</code></pre>
        <button type="button" class="secondary" onClick={() => void copy()}>{copied ? 'Copied!' : 'Copy'}</button>
      </div>
      <p class="muted small">It works only on {state.install_origins.join(' and ')}.</p>
      {!state.installed_at && (
        <p class="waiting" aria-live="polite"><span class="spinner" aria-hidden="true" /> Waiting to see it on your site…</p>
      )}
      {!state.installed_at && state.site_url === LOCAL_DEMO_SITE && (
        <p class="muted small">
          Local demo: open <a href={`${LOCAL_DEMO_SITE}/try.html?key=${state.site_key}`} target="_blank"
            rel="noopener noreferrer">{LOCAL_DEMO_SITE}/try.html</a>, a page on that site with your tag on it.
        </p>
      )}
    </section>
  );
}
