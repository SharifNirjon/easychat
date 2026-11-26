/**
 * Step 6 (optional, roadmap step 8): connect the shop's orders, so customers can track an order in
 * the chat after confirming a code sent to their checkout email. Shopify or the shop's own API.
 * Credentials go to the API once and never come back: the state shows only the shop / URL.
 */
import { useState } from 'preact/hooks';

import { api, ApiError, type OnboardingState, type OrdersConnection } from '../api';

const STATUS_WORDS: Record<string, string> = {
  placed: 'order placed', packed: 'packed', shipped: 'shipped', out_for_delivery: 'out for delivery',
  delivered: 'delivered', cancelled: 'cancelled',
};

interface Props {
  state: OnboardingState;
  onChange: () => void;          // reload the state after connecting or disconnecting
}

/** Run an API call with a busy flag and a visible error. */
function useAction() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = async (action: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await action();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Something went wrong.');
    } finally {
      setBusy(false);
    }
  };
  return { busy, error, run };
}

/** A labelled text input. */
function Field(props: { label: string; value: string; onInput: (v: string) => void; placeholder?: string;
  type?: string; autoComplete?: string }) {
  return (
    <label class="field">
      <span>{props.label}</span>
      <input type={props.type ?? 'text'} required value={props.value} placeholder={props.placeholder}
        autoComplete={props.autoComplete ?? 'off'} spellcheck={false}
        onInput={(e) => props.onInput((e.target as HTMLInputElement).value)} />
    </label>
  );
}

function ShopifyForm({ onConnected }: { onConnected: () => void }) {
  const [shop, setShop] = useState('');
  const [clientId, setClientId] = useState('');
  const [clientSecret, setClientSecret] = useState('');
  const [token, setToken] = useState('');
  const [legacy, setLegacy] = useState(false);
  const { busy, error, run } = useAction();

  const submit = (event: Event) => {
    event.preventDefault();
    void run(async () => {
      await api.connectShopify(legacy ? { shop, access_token: token }
        : { shop, client_id: clientId, client_secret: clientSecret });
      onConnected();
    });
  };

  return (
    <form class="stack" onSubmit={submit}>
      <ol class="tips small">
        <li>In Shopify's <strong>Dev Dashboard</strong>, create an app for your store.</li>
        <li>Give it the <code>read_orders</code> scope and access to customer email, then install it on the store.</li>
        <li>Copy the app's <strong>client ID</strong> and <strong>secret</strong> here. We only ever read orders.</li>
      </ol>
      <Field label="Store address" value={shop} onInput={setShop} placeholder="my-store.myshopify.com" />
      {legacy ? (
        <Field label="Admin API access token" value={token} onInput={setToken} type="password" placeholder="shpat_…" />
      ) : (
        <>
          <Field label="Client ID" value={clientId} onInput={setClientId} />
          <Field label="Client secret" value={clientSecret} onInput={setClientSecret} type="password" />
        </>
      )}
      <button type="button" class="link small" onClick={() => setLegacy(!legacy)}>
        {legacy ? 'Use a client ID and secret instead' : 'I have an older app with an access token'}
      </button>
      <div><button type="submit" class="primary" disabled={busy}>{busy ? 'Checking with Shopify…' : 'Connect Shopify'}</button></div>
      {error && <p class="error" role="alert">{error}</p>}
    </form>
  );
}

function ApiForm({ onConnected }: { onConnected: (secret: string) => void }) {
  const [url, setUrl] = useState('');
  const { busy, error, run } = useAction();

  const submit = (event: Event) => {
    event.preventDefault();
    void run(async () => onConnected((await api.connectOrderApi(url.trim())).secret));
  };

  return (
    <form class="stack" onSubmit={submit}>
      <p class="muted small">
        Your developer adds one endpoint to your shop: we call <code>GET your-url?order_number=…</code> with
        a secret we create for you, and it answers with the order's status, events and carrier as JSON
        (the format is in the easychat README, "Order tracking").
      </p>
      <Field label="Order API address" value={url} onInput={setUrl} placeholder="https://myshop.com/api/easychat/orders" />
      <div><button type="submit" class="primary" disabled={busy}>Connect</button></div>
      {error && <p class="error" role="alert">{error}</p>}
    </form>
  );
}

function Connected({ orders, secret, onChange }: { orders: OrdersConnection; secret: string | null; onChange: () => void }) {
  const [number, setNumber] = useState('');
  const [result, setResult] = useState<string | null>(null);
  const { busy, error, run } = useAction();

  const test = (event: Event) => {
    event.preventDefault();
    void run(async () => {
      const answer = await api.testOrder(number);
      setResult(answer.found
        ? `Found it: ${STATUS_WORDS[answer.status ?? ''] ?? answer.status}${answer.item_count ? `, ${answer.item_count} items` : ''}.`
        : 'No order with that number.');
      if (answer.found) onChange();
    });
  };

  return (
    <div class="stack">
      <div class="success">
        <p><strong>Connected: {orders.kind === 'shopify' ? 'Shopify' : 'your order API'}</strong> · {orders.endpoint}</p>
        <p class="muted small">{orders.checked_at ? `Last test lookup worked ${new Date(orders.checked_at).toLocaleString()}.`
          : 'Try a lookup below to check it works.'}</p>
      </div>
      {secret && (
        <div class="panel">
          <p><strong>Your API secret (shown once):</strong></p>
          <div class="snippet"><pre><code>{secret}</code></pre></div>
          <p class="muted small">Your endpoint must accept only requests with the header
            <code> Authorization: Bearer {'<'}this secret{'>'}</code>. Store it like a password.</p>
        </div>
      )}
      <form class="row" onSubmit={test}>
        <label class="field grow">
          <span>Test with an order number</span>
          <input type="text" required value={number} placeholder="e.g. 1001"
            onInput={(e) => setNumber((e.target as HTMLInputElement).value)} />
        </label>
        <button type="submit" class="secondary" disabled={busy}>Look it up</button>
      </form>
      {result && <p aria-live="polite">{result}</p>}
      {error && <p class="error" role="alert">{error}</p>}
      <div>
        <button type="button" class="link small" disabled={busy}
          onClick={() => void run(async () => { await api.disconnectOrders(); onChange(); })}>
          Disconnect
        </button>
      </div>
    </div>
  );
}

export function OrdersStep({ state, onChange }: Props) {
  const [kind, setKind] = useState<'shopify' | 'api'>('shopify');
  const [secret, setSecret] = useState<string | null>(null);

  return (
    <section>
      <p class="eyebrow">Step 6 · optional</p>
      <h1>Order tracking</h1>
      <p class="muted">
        Let customers ask "where's my order?" in the chat. To see an order they type its number and
        their checkout email, and we email them a one-time code first, so nobody sees someone else's order.
      </p>
      {!state.orders_available ? (
        <p class="error">Order tracking isn't set up on this server yet (SECRETS_KEY is missing).</p>
      ) : state.orders ? (
        <Connected orders={state.orders} secret={secret} onChange={onChange} />
      ) : (
        <div class="panel stack">
          <div class="row" role="radiogroup" aria-label="Where your orders are">
            <label><input type="radio" name="kind" checked={kind === 'shopify'} onChange={() => setKind('shopify')} /> Shopify</label>
            <label><input type="radio" name="kind" checked={kind === 'api'} onChange={() => setKind('api')} /> My own system</label>
          </div>
          {kind === 'shopify' ? <ShopifyForm onConnected={onChange} />
            : <ApiForm onConnected={(s) => { setSecret(s); onChange(); }} />}
        </div>
      )}
    </section>
  );
}
