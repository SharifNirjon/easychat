/**
 * Per-tab persistence in sessionStorage: survives page navigation in the same tab,
 * disappears when the tab closes. Storage can be blocked, so every access is guarded.
 *
 * The messages here are a display copy only. The server keeps the real history, keyed by
 * conversationId, and never trusts what the browser remembers.
 */
import { MAX_DISPLAY_MESSAGES } from './config';
import type { ChatMessage } from './types';

export interface SavedState {
  conversationId: string | null;
  /** The visitor's last message until the server confirms it stored it (then null). */
  unsent: string | null;
  messages: ChatMessage[];
  open: boolean;
  teaserSeen: boolean;
}

const EMPTY: SavedState = { conversationId: null, unsent: null, messages: [], open: false, teaserSeen: false };

const storageKey = (siteKey: string) => `easychat:${siteKey}`;

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Drop anything that isn't a well-formed user/assistant message (storage is untrusted). */
function cleanMessages(value: unknown): ChatMessage[] {
  if (!Array.isArray(value)) return [];
  return value
    .filter(
      (m): m is ChatMessage =>
        m && (m.role === 'user' || m.role === 'assistant') && typeof m.content === 'string' && m.content !== '',
    )
    .slice(-MAX_DISPLAY_MESSAGES);
}

export function loadState(siteKey: string): SavedState {
  try {
    const raw = sessionStorage.getItem(storageKey(siteKey));
    if (!raw) return { ...EMPTY };
    const parsed = JSON.parse(raw);
    return {
      conversationId: typeof parsed.conversationId === 'string' && UUID.test(parsed.conversationId)
        ? parsed.conversationId
        : null,
      unsent: typeof parsed.unsent === 'string' && parsed.unsent !== '' ? parsed.unsent : null,
      messages: cleanMessages(parsed.messages),
      open: parsed.open === true,
      teaserSeen: parsed.teaserSeen === true,
    };
  } catch {
    return { ...EMPTY };
  }
}

export function saveState(siteKey: string, state: SavedState): void {
  try {
    const trimmed = { ...state, messages: state.messages.slice(-MAX_DISPLAY_MESSAGES) };
    sessionStorage.setItem(storageKey(siteKey), JSON.stringify(trimmed));
  } catch {
    // Storage full or blocked: the widget still works, it just won't remember.
  }
}
