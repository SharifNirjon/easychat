/**
 * Step 4: the real widget, with the tenant's real site key and knowledge, running on this page
 * (the app's origin is one of the key's allowed origins: the preview). It's injected once and
 * stays for the rest of the session, like on a real site.
 */
import { useEffect } from 'preact/hooks';

import type { OnboardingState } from '../api';

const SCRIPT_ID = 'easychat-preview-widget';

/** Load widget.js with this tenant's key (no-op if it is already on the page). */
function loadWidget(src: string, siteKey: string): void {
  if (document.getElementById(SCRIPT_ID)) return;
  const script = document.createElement('script');
  script.id = SCRIPT_ID;
  script.src = src;
  script.async = true;
  script.dataset.key = siteKey;
  document.body.append(script);
}

export function TryStep({ state, onNext }: { state: OnboardingState; onNext: () => void }) {
  useEffect(() => {
    if (state.site_key) loadWidget(state.widget_url, state.site_key);
  }, [state.site_key, state.widget_url]);

  return (
    <section>
      <p class="eyebrow">Step 4</p>
      <h1>Try {state.assistant.bot_name}</h1>
      <p class="muted">
        Your assistant is running on this page now: open the chat button at the bottom right and ask
        something about {state.assistant.company_name}. It answers only from your website and says so when it doesn't know.
      </p>
      <ul class="tips">
        <li>Ask about a product, price or policy on your site.</li>
        <li>Ask something your site doesn't cover: it should say it doesn't know.</li>
        <li>Ask "are you a human?": it always says it's an AI.</li>
      </ul>
      <p class="muted small">
        Changed something in step 2? Reload this page to see it in the chat.
      </p>
      <button type="button" class="primary" onClick={onNext}>Put it on my website</button>
    </section>
  );
}
