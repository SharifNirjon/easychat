/**
 * The onboarding API (api/auth.py, api/onboarding.py). Same origin as this app, so the
 * session cookie is sent automatically and stays HttpOnly (JavaScript never sees it).
 */

export interface Assistant {
  company_name: string;
  bot_name: string;
  headline: string;
  subtitle: string;
  teaser: string;
  primary: string;
  accent: string;
}

export interface Crawl {
  status: 'queued' | 'running' | 'done' | 'failed';
  pages_found: number;
  pages_indexed: number;
  pages_unchanged: number;
  pages_skipped: number;
  pages_failed: number;
  error: string | null;
}

/** Everything the app renders from (GET /api/onboarding). */
export interface OnboardingState {
  email: string;
  role: 'owner' | 'member';
  status: 'onboarding' | 'live' | 'suspended';
  plan: string;
  site_url: string | null;
  assistant: Assistant;
  crawl: Crawl | null;
  site_key: string | null;
  snippet: string | null;
  widget_url: string;
  install_origins: string[];
  installed_at: string | null;
  limits: { max_pages: number; daily_crawls: number; crawls_left_today: number };
  /** The connected order system (step 8): never its credentials. */
  orders: OrdersConnection | null;
  /** False when this server has no SECRETS_KEY (order tracking can't be connected). */
  orders_available: boolean;
  /** Where chat-form requests go (step 9): never the webhook's secret. */
  requests: RequestSettings;
}

export interface Webhook {
  url: string;
  created_at: string;
  checked_at: string | null;
}

export interface RequestSettings {
  notify_email: string | null;     // null: the account's email
  sends_to: string | null;         // where team emails actually go
  booking_url: string | null;
  retention_days: number;
  chat_retention_days: number;
  leads: boolean;
  returns: boolean;
  return_window_days: number;
  webhook: Webhook | null;
}

export type RequestSettingsIn = Omit<RequestSettings, 'sends_to' | 'webhook'>;

export interface OrdersConnection {
  kind: 'shopify' | 'api';
  endpoint: string;             // shop domain or API URL
  connected_at: string;
  checked_at: string | null;
}

/** Shopify app credentials: client ID + secret, or an older app's access token. */
export interface ShopifyCredentials {
  shop: string;
  client_id?: string;
  client_secret?: string;
  access_token?: string;
}

export interface OrderTestResult {
  found: boolean;
  status?: string;
  item_count?: number | null;
  orders: OrdersConnection | null;
}

// --- Admin (step 10) ---------------------------------------------------------------------------

export interface Page<T> { total: number; page: number; page_size: number; items: T[] }

export interface ConversationRow {
  id: string; created_at: string; last_message_at: string; turns: number; status: 'open' | 'resolved';
  preview: string; flagged: boolean;
  requests: { kind: string; status: string; name: string | null; order: string | null }[];
}

export interface RequestItem {
  id: string; kind: 'lead' | 'handoff' | 'return'; status: 'new' | 'contacted' | 'closed';
  data: Record<string, unknown>; consent: string; consented_at: string; expires_at: string;
  emailed_at: string | null; webhook_sent_at: string | null; webhook_error: string | null;
  conversation_id: string | null;
}

export interface ConversationDetail {
  id: string; status: 'open' | 'resolved'; created_at: string; last_message_at: string;
  messages: { role: 'user' | 'assistant'; content: string; at: string; flags: string[] }[];
  requests: RequestItem[];
}

export interface KbEntry {
  id: string; section: string; title: string; body: string; live: boolean; changed: boolean; deleted: boolean;
}
export interface KbPage { id: number; url: string; title: string; kind: string; hidden: boolean; chunks: number }
export interface KbState {
  pages: KbPage[]; entries: KbEntry[]; changes: number;
  publish: { state: 'queued' | 'running' | 'done' | 'failed'; detail: string } | null;
}
export interface AskResult {
  hits: { title: string; kind: string; url: string | null; score: number; excerpt: string }[];
  answer: string | null; knows: boolean; error: string | null;
}
export interface TeamState {
  you: { email: string; role: string }; max_members: number;
  members: { id: string; email: string; role: 'owner' | 'member'; joined: string; last_sign_in: string | null }[];
}

/** An API error with the server's visitor-safe message. */
export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

/** Call the API; throws ApiError with the server's `detail` on any non-2xx. */
async function call<T>(method: string, path: string, body?: unknown, contentType = 'application/json'): Promise<T> {
  const init: RequestInit = { method, credentials: 'same-origin', headers: {} };
  if (body !== undefined) {
    init.headers = { 'Content-Type': contentType };
    init.body = typeof body === 'string' ? body : JSON.stringify(body);
  }
  let response: Response;
  try {
    response = await fetch(path, init);
  } catch {
    throw new ApiError("Couldn't reach easychat. Check your connection and try again.", 0);
  }
  if (response.status === 204) return undefined as T;
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = typeof data.detail === 'string' ? data.detail : 'Something went wrong. Please try again.';
    throw new ApiError(detail, response.status);
  }
  return data as T;
}

export const api = {
  requestLink: (email: string) => call<{ ok: boolean }>('POST', '/api/auth/start', { email }),
  verify: (token: string) => call<{ email: string }>('POST', '/api/auth/verify', { token }),
  logout: () => call<void>('POST', '/api/auth/logout'),
  state: () => call<OnboardingState>('GET', '/api/onboarding'),
  setSite: (url: string) => call<OnboardingState>('POST', '/api/onboarding/site', { url }),
  uploadSitemap: (xml: string) => call<OnboardingState>('POST', '/api/onboarding/sitemap', xml, 'application/xml'),
  saveAssistant: (assistant: Omit<Assistant, 'accent'>) =>
    call<OnboardingState>('PUT', '/api/onboarding/assistant', assistant),
  crawlAgain: () => call<OnboardingState>('POST', '/api/onboarding/crawl'),
  connectShopify: (credentials: ShopifyCredentials) =>
    call<{ orders: OrdersConnection }>('PUT', '/api/onboarding/orders/shopify', credentials),
  /** The answer's `secret` is shown this once only. */
  connectOrderApi: (url: string) =>
    call<{ orders: OrdersConnection; secret: string }>('PUT', '/api/onboarding/orders/api', { url }),
  testOrder: (order_number: string) => call<OrderTestResult>('POST', '/api/onboarding/orders/test', { order_number }),
  disconnectOrders: () => call<void>('DELETE', '/api/onboarding/orders'),
  saveRequestSettings: (settings: RequestSettingsIn) =>
    call<{ requests: RequestSettings }>('PUT', '/api/onboarding/requests/settings', settings),
  /** The answer's `secret` is shown this once only. */
  connectWebhook: (url: string) =>
    call<{ webhook: Webhook; secret: string }>('PUT', '/api/onboarding/requests/webhook', { url }),
  testWebhook: () => call<{ ok: boolean; detail?: string }>('POST', '/api/onboarding/requests/webhook/test'),
  disconnectWebhook: () => call<void>('DELETE', '/api/onboarding/requests/webhook'),
  // Admin (step 10)
  conversations: (params: Record<string, string>) =>
    call<Page<ConversationRow>>('GET', `/api/admin/conversations?${new URLSearchParams(params)}`),
  conversation: (id: string) => call<ConversationDetail>('GET', `/api/admin/conversations/${id}`),
  setConversationStatus: (id: string, status: string) =>
    call<unknown>('PUT', `/api/admin/conversations/${id}/status`, { status }),
  deleteConversation: (id: string) => call<void>('DELETE', `/api/admin/conversations/${id}`),
  requests: (params: Record<string, string>) =>
    call<Page<RequestItem>>('GET', `/api/admin/requests?${new URLSearchParams(params)}`),
  setRequestStatus: (id: string, status: string) => call<unknown>('PUT', `/api/admin/requests/${id}/status`, { status }),
  /** A plain link (the browser downloads it with the session cookie). */
  exportUrl: (kind: string) => `/api/admin/requests/export.csv?kind=${encodeURIComponent(kind)}`,
  kb: () => call<KbState>('GET', '/api/admin/kb'),
  saveEntry: (entry: { section: string; title: string; body: string }, id?: string) =>
    id ? call<KbEntry>('PUT', `/api/admin/kb/entries/${id}`, entry) : call<KbEntry>('POST', '/api/admin/kb/entries', entry),
  deleteEntry: (id: string) => call<void>('DELETE', `/api/admin/kb/entries/${id}`),
  hidePage: (id: number, hidden: boolean) => call<unknown>('PUT', `/api/admin/kb/pages/${id}`, { hidden }),
  publish: () => call<unknown>('POST', '/api/admin/kb/publish'),
  ask: (question: string) => call<AskResult>('POST', '/api/admin/kb/ask', { question }),
  team: () => call<TeamState>('GET', '/api/admin/team'),
  invite: (email: string) => call<unknown>('POST', '/api/admin/team', { email }),
  removeMember: (id: string) => call<void>('DELETE', `/api/admin/team/${id}`),
};

/** True while a crawl is waiting for the worker or running. */
export const crawlActive = (crawl: Crawl | null): boolean =>
  crawl !== null && (crawl.status === 'queued' || crawl.status === 'running');
