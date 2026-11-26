/**
 * Step 2: the assistant's name, texts and colours, pre-filled from the website, with a live
 * preview in the widget's own layout (the real widget runs in step 4, "Try it").
 */
import { useState } from 'preact/hooks';

import { api, ApiError, type Assistant, type OnboardingState } from '../api';

interface Props {
  state: OnboardingState;
  onUpdate: (state: OnboardingState) => void;
  onNext: () => void;
}

type Draft = Omit<Assistant, 'accent'>;

const FIELDS: { key: keyof Draft; label: string; max: number; hint?: string }[] = [
  { key: 'company_name', label: 'Business name', max: 120 },
  { key: 'bot_name', label: 'Assistant name', max: 40 },
  { key: 'headline', label: 'Greeting', max: 120, hint: 'Words in *asterisks* are shown in italics.' },
  { key: 'subtitle', label: 'Greeting subtitle', max: 300 },
  { key: 'teaser', label: 'Bubble message', max: 200, hint: 'Shown next to the chat button.' },
];

/** The headline with *starred* words in italics (as the widget renders it). */
function Headline({ text }: { text: string }) {
  return <>{text.split(/(\*[^*]+\*)/).map((part) => (part.startsWith('*') ? <em>{part.slice(1, -1)}</em> : part))}</>;
}

/** A static picture of the widget with the draft applied. */
function Preview({ draft, accent }: { draft: Draft; accent: string }) {
  return (
    <div class="preview" aria-label="Preview">
      <div class="preview-header" style={{ background: draft.primary }}>
        <span class="preview-avatar" style={{ background: accent, color: draft.primary }}>
          {draft.bot_name.charAt(0).toUpperCase() || 'A'}
        </span>
        <span>
          <strong>{draft.bot_name || 'Assistant'}</strong>
          <span class="preview-sub">AI assistant · {draft.company_name}</span>
        </span>
      </div>
      <div class="preview-body">
        <p class="preview-headline"><Headline text={draft.headline} /></p>
        <p class="muted small">{draft.subtitle}</p>
      </div>
      <div class="preview-teaser">{draft.teaser}</div>
      <span class="preview-launcher" style={{ background: draft.primary }} aria-hidden="true">💬</span>
    </div>
  );
}

export function AssistantStep({ state, onUpdate, onNext }: Props) {
  const { accent, ...initial } = state.assistant;
  const [draft, setDraft] = useState<Draft>(initial);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const save = async (event: Event) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onUpdate(await api.saveAssistant(draft));
      onNext();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Something went wrong.');
    } finally {
      setBusy(false);
    }
  };

  const set = (key: keyof Draft, value: string) => setDraft({ ...draft, [key]: value });

  return (
    <section>
      <p class="eyebrow">Step 2</p>
      <h1>Meet your assistant</h1>
      <p class="muted">We filled this in from your website. Change anything you like; your site is being read meanwhile.</p>
      <div class="split">
        <form onSubmit={save} class="stack">
          {FIELDS.map((f) => (
            <label class="field" key={f.key}>
              <span>{f.label}</span>
              <input type="text" required maxLength={f.max} value={draft[f.key]}
                onInput={(e) => set(f.key, (e.target as HTMLInputElement).value)} />
              {f.hint && <small class="muted">{f.hint}</small>}
            </label>
          ))}
          <label class="field">
            <span>Brand colour</span>
            <span class="row">
              <input type="color" value={draft.primary} onInput={(e) => set('primary', (e.target as HTMLInputElement).value)} />
              <code>{draft.primary.toUpperCase()}</code>
            </span>
          </label>
          {error && <p class="error" role="alert">{error}</p>}
          <button type="submit" class="primary" disabled={busy}>{busy ? 'Saving…' : 'Save and continue'}</button>
        </form>
        <Preview draft={draft} accent={accent} />
      </div>
    </section>
  );
}
