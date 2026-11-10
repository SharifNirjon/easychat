/**
 * Per-tab persistence in sessionStorage: survives page navigation in the same tab,
 * disappears when the tab closes. Storage can be blocked, so every access is guarded.
 */
import { MAX_HISTORY_TURNS } from './config';
import type { ChatMessage } from './types';

export interface SavedState {
  messages: ChatMessage[];
  open: boolean;
  teaserSeen: boolean;
}

const EMPTY: SavedState = { messages: [], open: false, teaserSeen: false };

const storageKey = (siteId: string) => `easychat:${siteId}`;

/** Drop anything that isn't a well-formed user/assistant message (storage is untrusted). */
function cleanMessages(value: unknown): ChatMessage[] {
  if (!Array.isArray(value)) return [];
  return value
    .filter(
      (m): m is ChatMessage =>
        m && (m.role === 'user' || m.role === 'assistant') && typeof m.content === 'string' && m.content !== '',
    )
    .slice(-MAX_HISTORY_TURNS);
}

export function loadState(siteId: string): SavedState {
  try {
    const raw = sessionStorage.getItem(storageKey(siteId));
    if (!raw) return { ...EMPTY };
    const parsed = JSON.parse(raw);
    return {
      messages: cleanMessages(parsed.messages),
      open: parsed.open === true,
      teaserSeen: parsed.teaserSeen === true,
    };
  } catch {
    return { ...EMPTY };
  }
}

export function saveState(siteId: string, state: SavedState): void {
  try {
    const trimmed = { ...state, messages: state.messages.slice(-MAX_HISTORY_TURNS) };
    sessionStorage.setItem(storageKey(siteId), JSON.stringify(trimmed));
  } catch {
    // Storage full or blocked: the widget still works, it just won't remember.
  }
}
