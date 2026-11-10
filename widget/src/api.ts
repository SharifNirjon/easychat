/** Talks to the easychat API: tenant config + streamed chat replies (SSE over fetch). */
import { API_BASE } from './config';
import type { ChatMessage, WidgetConfig } from './types';

const NETWORK_ERROR = "I couldn't reach the server. Check your connection and try again.";
const INTERRUPTED_ERROR = 'The reply was interrupted. Please try again.';

export async function fetchConfig(siteId: string): Promise<WidgetConfig> {
  const res = await fetch(`${API_BASE}/config?site=${encodeURIComponent(siteId)}`);
  if (!res.ok) throw new Error(`easychat: config request failed (${res.status})`);
  return (await res.json()) as WidgetConfig;
}

export interface StreamHandlers {
  onToken(text: string): void;
  onDone(): void;
  onError(message: string): void;
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

/**
 * POST the conversation and stream the reply. Exactly one of onDone / onError is
 * called at the end, unless the request was aborted via `signal` (then neither).
 */
export async function streamChat(
  siteId: string,
  messages: ChatMessage[],
  handlers: StreamHandlers,
  signal: AbortSignal,
): Promise<void> {
  try {
    const res = await fetch(`${API_BASE}/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ site_id: siteId, messages }),
      signal,
    });
    if (!res.ok || !res.body) {
      handlers.onError(NETWORK_ERROR);
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

        const data = parsed.data as { t?: unknown; message?: unknown };
        if (parsed.event === 'token' && typeof data.t === 'string') {
          handlers.onToken(data.t);
        } else if (parsed.event === 'done') {
          handlers.onDone();
          return;
        } else if (parsed.event === 'error') {
          handlers.onError(typeof data.message === 'string' ? data.message : INTERRUPTED_ERROR);
          return;
        }
      }
    }
    // Stream closed without done/error: treat as interrupted rather than hanging.
    handlers.onError(INTERRUPTED_ERROR);
  } catch {
    if (!signal.aborted) handlers.onError(NETWORK_ERROR);
  }
}
