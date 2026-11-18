/**
 * Per-tab persistence in sessionStorage: survives page navigation in the same tab,
 * disappears when the tab closes. Storage can be blocked, so every access is guarded.
 *
 * The messages here are a display copy only. The server keeps the real history, keyed by
 * conversationId, and never trusts what the browser remembers.
 */
import { MAX_DISPLAY_MESSAGES } from './config';
import type { Block, ChatMessage } from './types';

export interface SavedState {
  conversationId: string | null;
  /** The visitor's last message until the server confirms it stored it (then null). */
  unsent: string | null;
  messages: ChatMessage[];
  open: boolean;
  teaserSeen: boolean;
}

const EMPTY: SavedState = { conversationId: null, unsent: null, messages: [], open: false, teaserSeen: false };

/** One sessionStorage entry per site key, so two widgets on one page don't collide. */
const storageKey = (siteKey: string) => `easychat:${siteKey}`;

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Same cap the API applies per reply (api/blocks.py MAX_BLOCKS_PER_REPLY). */
const MAX_BLOCKS_PER_MESSAGE = 6;

/** Keep only block-shaped objects. Renderers are defensive too, so this is a cheap first filter. */
function cleanBlocks(value: unknown): Block[] | undefined {
  if (!Array.isArray(value)) return undefined;
  const blocks = value.filter((b) => b && typeof b === 'object' && typeof b.type === 'string');
  return blocks.length ? (blocks.slice(0, MAX_BLOCKS_PER_MESSAGE) as Block[]) : undefined;
}

/** Drop anything that isn't a well-formed user/assistant message (storage is untrusted). */
function cleanMessages(value: unknown): ChatMessage[] {
  if (!Array.isArray(value)) return [];
  return value
    .filter(
      (m): m is ChatMessage =>
        m && (m.role === 'user' || m.role === 'assistant') && typeof m.content === 'string' && m.content !== '',
    )
    .map((m) => {
      const blocks = m.role === 'assistant' ? cleanBlocks(m.blocks) : undefined;
      return blocks ? { role: m.role, content: m.content, blocks } : { role: m.role, content: m.content };
    })
    .slice(-MAX_DISPLAY_MESSAGES);
}

/** This tab's saved state, cleaned of anything malformed; empty state if none or unreadable. */
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

/** Save this tab's state (capped to MAX_DISPLAY_MESSAGES); a full or blocked storage is ignored. */
export function saveState(siteKey: string, state: SavedState): void {
  try {
    const trimmed = { ...state, messages: state.messages.slice(-MAX_DISPLAY_MESSAGES) };
    sessionStorage.setItem(storageKey(siteKey), JSON.stringify(trimmed));
  } catch {
    // Storage full or blocked: the widget still works, it just won't remember.
  }
}
