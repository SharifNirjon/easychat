/**
 * Helpers shared by the block renderers. Defence in depth: the server already validated every
 * block, but the widget also re-checks URLs and only ever inserts text as text nodes.
 */
import { el } from '../../dom';
import { icon } from '../../icons';
import type { Action, ActionHandler, ImageRef } from '../../types';

const LOCAL_HOSTS = ['localhost', '127.0.0.1'];
const MAX_STARS = 5;

/** The URL if it's https (or http on localhost, for local dev); otherwise null. */
export function safeUrl(raw: string): string | null {
  try {
    const url = new URL(raw);
    const localHttp = url.protocol === 'http:' && LOCAL_HOSTS.includes(url.hostname);
    return url.protocol === 'https:' || localHttp ? url.href : null;
  } catch {
    return null;
  }
}

/**
 * A tappable control for an action: a <button> that sends a message, or an <a> that opens a
 * page in a new tab. Send buttons are disabled once the conversation moves on (see MessageList).
 */
export function actionControl(
  action: Action,
  className: string,
  children: (Node | string)[],
  onAction: ActionHandler,
  ariaLabel?: string,
): HTMLElement {
  if (action.kind === 'link') {
    const href = safeUrl(action.url);
    if (!href) return el('span', { class: `${className} ec-is-disabled` }, children);
    return el('a', { class: className, href, target: '_blank', rel: 'noopener noreferrer', 'aria-label': ariaLabel }, children);
  }
  const button = el('button', { type: 'button', class: className, 'aria-label': ariaLabel }, children);
  button.addEventListener('click', () => onAction(action));
  return button;
}

/** A lazy-loaded image that doesn't tell its host which page it is on; null for unsafe URLs. */
export function image(ref: ImageRef | undefined, className: string): HTMLImageElement | null {
  const src = ref && safeUrl(ref.url);
  if (!src) return null;
  return el('img', {
    class: className,
    src,
    alt: ref?.alt ?? '',
    loading: 'lazy',
    decoding: 'async',
    referrerpolicy: 'no-referrer', // don't tell image hosts which page the visitor is on
  });
}

/** "★ 4.8 · 1.2k reviews" */
export function ratingLine(rating: number | undefined, reviews: string | undefined): HTMLElement | null {
  if (rating === undefined && !reviews) return null;
  const parts: (Node | string)[] = [];
  if (rating !== undefined) {
    const value = Math.max(0, Math.min(MAX_STARS, rating));
    parts.push(el('span', { class: 'ec-star' }, [icon('star', 13, { filled: true })]), value.toFixed(1));
  }
  if (reviews) parts.push(rating !== undefined ? ` · ${reviews}` : reviews);
  return el('p', { class: 'ec-rating', 'aria-label': rating !== undefined ? `Rated ${rating} out of 5` : undefined }, parts);
}

/** Small uppercase label above a group ("SOURCES", "WHAT STOOD OUT?"). */
export function groupLabel(text: string, id?: string): HTMLElement {
  return el('p', { class: 'ec-label', id }, [text]);
}

let idCounter = 0;
/** Unique ids for label/aria wiring inside the shadow root. */
export function uid(prefix: string): string {
  idCounter += 1;
  return `ec-${prefix}-${idCounter}`;
}
