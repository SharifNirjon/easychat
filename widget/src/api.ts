/** Talks to the easychat API: tenant config + streamed chat replies (SSE over fetch). */
import { API_BASE } from './config';
import type { WidgetConfig } from './types';

const NETWORK_ERROR = "I couldn't reach the server. Check your connection and try again.";
const INTERRUPTED_ERROR = 'The reply was interrupted. Please try again.';
const UNAVAILABLE_ERROR = 'The assistant is unavailable right now. Please try again.';

export async function fetchConfig(siteKey: string): Promise<WidgetConfig> {
  const res = await fetch(`${API_BASE}/config?key=${encodeURIComponent(siteKey)}`);
  if (!res.ok) throw new Error(`easychat: config request failed (${res.status})`);
  return (await res.json()) as WidgetConfig;
}

/** Why a turn failed, so the controller can decide what the visitor can do next. */
export type FailureKind =
  | 'retryable'        // network/model hiccup: Retry re-asks the same question
  | 'limited'          // rate or budget limit: Retry later
  | 'conversation-gone'; // the server doesn't know this conversation: start a new one

export interface StreamHandlers {
  onConversation(id: string): void;
  onToken(text: string): void;
  onDone(): void;
  onError(message: string, kind: FailureKind): void;
}

export interface ChatTurn {
  siteKey: string;
  conversationId: string | null;
  /** null = retry: ask the server to answer the history it already has. */
  message: string | null;
}

interface SseEvent {
  event: string;
  data: unknown;
}

/** Parse one raw SSE block ("event: x\ndata: {...}"). Returns null for comments/garbage. */
function parseSseBlock(block: string): SseEvent | null {
  let event = 'message';
  const dataLines: string[] = [];
  for (const line of block.split('\n')) {
    if (line.startsWith('event:')) event = line.slice(6).trim();
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart());
  }
  if (dataLines.length === 0) return null;
  try {
    return { event, data: JSON.parse(dataLines.join('\n')) };
  } catch {
    return null;
  }
}

/** Map a non-2xx response to something safe to show. Only 429 texts come from the server. */
async function failureFor(res: Response): Promise<{ message: string; kind: FailureKind }> {
  if (res.status === 429) {
    let detail = '';
    try {
      const body = (await res.json()) as { detail?: unknown };
      if (typeof body.detail === 'string') detail = body.detail.slice(0, 200);
    } catch {
      // fall through to the generic text
    }
    return { message: detail || 'Too many messages — please wait a moment.', kind: 'limited' };
  }
  if (res.status === 404 || res.status === 409) {
    return { message: 'This chat has expired. Please start a new one.', kind: 'conversation-gone' };
  }
  return { message: UNAVAILABLE_ERROR, kind: 'retryable' };
}

/**
 * POST one turn and stream the reply. Exactly one of onDone / onError is called at the end,
 * unless the request was aborted via `signal` (then neither).
 */
export async function streamChat(turn: ChatTurn, handlers: StreamHandlers, signal: AbortSignal): Promise<void> {
  const body: Record<string, string> = { site_key: turn.siteKey };
  if (turn.conversationId) body.conversation_id = turn.conversationId;
  if (turn.message !== null) body.message = turn.message;

  try {
    const res = await fetch(`${API_BASE}/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal,
    });
    if (!res.ok || !res.body) {
      const failure = res.ok ? { message: NETWORK_ERROR, kind: 'retryable' as const } : await failureFor(res);
      handlers.onError(failure.message, failure.kind);
      return;
    }

    const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
    let buffer = '';
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += value.replace(/\r\n/g, '\n');

      // A blank line ends each SSE event; keep any partial event in the buffer.
      let boundary: number;
      while ((boundary = buffer.indexOf('\n\n')) !== -1) {
        const parsed = parseSseBlock(buffer.slice(0, boundary));
        buffer = buffer.slice(boundary + 2);
        if (!parsed) continue;

        const data = parsed.data as { t?: unknown; message?: unknown; conversation_id?: unknown };
        if (parsed.event === 'meta' && typeof data.conversation_id === 'string') {
          handlers.onConversation(data.conversation_id);
        } else if (parsed.event === 'token' && typeof data.t === 'string') {
          handlers.onToken(data.t);
        } else if (parsed.event === 'done') {
          handlers.onDone();
          return;
        } else if (parsed.event === 'error') {
          handlers.onError(typeof data.message === 'string' ? data.message : INTERRUPTED_ERROR, 'retryable');
          return;
        }
      }
    }
    // Stream closed without done/error: treat as interrupted rather than hanging.
    handlers.onError(INTERRUPTED_ERROR, 'retryable');
  } catch {
    if (!signal.aborted) handlers.onError(NETWORK_ERROR, 'retryable');
  }
}
