/**
 * Step 7 (optional, roadmap step 9): what visitors can send to the team from the chat, where it
 * goes (email, signed webhook), and how long it is kept. Every request needs the visitor's
 * consent; the webhook's signing secret is shown once and never comes back.
 */
import { useState } from 'preact/hooks';

import { api, type OnboardingState, type RequestSettings, type Webhook } from '../api';
import { Field, useAction } from './shared';

interface Props {
  state: OnboardingState;
  onChange: () => void;          // reload the state after saving
}

/** A labelled number input. */
function NumberField(props: { label: string; value: number; min: number; max: number; onInput: (v: number) => void }) {
  return (
    <label class="field">
      <span>{props.label}</span>
      <input type="number" required min={props.min} max={props.max} value={props.value}
        onInput={(e) => props.onInput(Number((e.target as HTMLInputElement).value))} />
    </label>
  );
}

function SettingsForm({ settings, orders, onSaved }: { settings: RequestSettings; orders: boolean; onSaved: () => void }) {
  const [email, setEmail] = useState(settings.notify_email ?? '');
  const [booking, setBooking] = useState(settings.booking_url ?? '');
  const [retention, setRetention] = useState(settings.retention_days);
  const [leads, setLeads] = useState(settings.leads);
  const [returns, setReturns] = useState(settings.returns);
  const [windowDays, setWindowDays] = useState(settings.return_window_days);
  const [saved, setSaved] = useState(false);
  const { busy, error, run } = useAction();

  const submit = (event: Event) => {
    event.preventDefault();
    setSaved(false);
    void run(async () => {
      await api.saveRequestSettings({
        notify_email: email.trim() || null, booking_url: booking.trim() || null, retention_days: retention,
        leads, returns, return_window_days: windowDays,
      });
      setSaved(true);
      onSaved();
    });
  };

  return (
    <form class="panel stack" onSubmit={submit}>
      <h2>What visitors can send</h2>
      <label><input type="checkbox" checked={leads} onChange={() => setLeads(!leads)} /> Contact requests and
        "talk to a person" (name, email and a short summary)</label>
      <label><input type="checkbox" checked={returns} onChange={() => setReturns(!returns)} /> Return requests
        {!orders && <span class="muted small"> (needs order tracking connected)</span>}</label>
      {returns && <NumberField label="Returns accepted for (days after delivery)" value={windowDays} min={1} max={365}
        onInput={setWindowDays} />}
      <h2>Where it goes</h2>
      <Field label="Team email (empty: your account's email)" type="email" value={email} onInput={setEmail}
        placeholder={settings.sends_to ?? 'team@myshop.com'} required={false} />
      <Field label="Booking page for calls (optional)" value={booking} onInput={setBooking}
        placeholder="https://cal.com/myshop" required={false} />
      <NumberField label="Delete requests after (days)" value={retention} min={7} max={365} onInput={setRetention} />
      <p class="muted small">Visitors always tick a consent box first, which names your company and this retention
        period. Nothing is changed in your store: the team decides on every request by email.</p>
      <div><button type="submit" class="primary" disabled={busy}>Save</button></div>
      {saved && <p aria-live="polite">Saved. Emails go to {settings.sends_to ?? 'your account email'}.</p>}
      {error && <p class="error" role="alert">{error}</p>}
    </form>
  );
}

function WebhookPanel({ webhook, available, onChange }: { webhook: Webhook | null; available: boolean; onChange: () => void }) {
  const [url, setUrl] = useState('');
  const [secret, setSecret] = useState<string | null>(null);
  const [result, setResult] = useState<string | null>(null);
  const { busy, error, run } = useAction();

  if (!available) return <p class="muted small">Webhooks aren't set up on this server yet (SECRETS_KEY is missing).</p>;

  const connect = (event: Event) => {
    event.preventDefault();
    void run(async () => {
      setSecret((await api.connectWebhook(url.trim())).secret);
      onChange();
    });
  };
  const test = () => void run(async () => {
    const answer = await api.testWebhook();
    setResult(answer.ok ? 'Delivered: your endpoint answered 2xx.' : `Not delivered: ${answer.detail ?? 'unknown error'}.`);
    onChange();
  });

  return (
    <div class="panel stack">
      <h2>Webhook (optional)</h2>
      <p class="muted small">Each request is also POSTed as JSON to your endpoint, signed in the
        <code> X-Easychat-Signature</code> header (the README, "Chat forms", shows how to check it).</p>
      {webhook ? (
        <>
          <p><strong>Sending to</strong> {webhook.url}</p>
          <p class="muted small">{webhook.checked_at ? `Last test delivery worked ${new Date(webhook.checked_at).toLocaleString()}.`
            : 'Send a test to check it works.'}</p>
          {secret && (
            <div>
              <p><strong>Signing secret (shown once):</strong></p>
              <div class="snippet"><pre><code>{secret}</code></pre></div>
            </div>
          )}
          <div class="row">
            <button type="button" class="secondary" disabled={busy} onClick={test}>Send a test</button>
            <button type="button" class="link small" disabled={busy}
              onClick={() => void run(async () => { await api.disconnectWebhook(); setSecret(null); onChange(); })}>
              Remove
            </button>
          </div>
          {result && <p aria-live="polite">{result}</p>}
        </>
      ) : (
        <form class="row" onSubmit={connect}>
          <label class="field grow">
            <span>Endpoint address</span>
            <input type="url" required value={url} placeholder="https://myshop.com/hooks/easychat"
              onInput={(e) => setUrl((e.target as HTMLInputElement).value)} />
          </label>
          <button type="submit" class="secondary" disabled={busy}>Add</button>
        </form>
      )}
      {error && <p class="error" role="alert">{error}</p>}
    </div>
  );
}

export function RequestsStep({ state, onChange }: Props) {
  return (
    <section>
      <p class="eyebrow">Step 7 · optional</p>
      <h1>Requests &amp; privacy</h1>
      <p class="muted">
        Visitors can ask your team to follow up, ask for a person, request a return for a delivered order,
        email themselves the chat, or delete their chat. Requests reach you by email, and by webhook if you add one.
      </p>
      <div class="stack">
        <SettingsForm settings={state.requests} orders={state.orders !== null} onSaved={onChange} />
        <WebhookPanel webhook={state.requests.webhook} available={state.orders_available} onChange={onChange} />
      </div>
    </section>
  );
}
