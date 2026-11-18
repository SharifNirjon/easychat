/** Launcher button and the one-time proactive teaser (D-01). */
import { el } from '../dom';
import { icon } from '../icons';
import type { WidgetConfig } from '../types';
import { avatar } from './messages';

/** The round button that opens and closes the chat. */
export class Launcher {
  readonly element: HTMLButtonElement;

  constructor(private readonly botName: string, onToggle: () => void) {
    this.element = el('button', {
      type: 'button',
      class: 'ec-launcher',
      'aria-controls': 'ec-panel',
    });
    this.element.addEventListener('click', onToggle);
    this.setOpen(false);
  }

  /** Closed: chat icon. Open: becomes a close (X) button (D-02). */
  setOpen(open: boolean): void {
    this.element.setAttribute('aria-expanded', String(open));
    this.element.setAttribute('aria-label', open ? 'Close chat' : `Open chat with ${this.botName}`);
    this.element.replaceChildren(icon(open ? 'close' : 'chat', 22));
  }

  /** Show or hide the small attention dot (used while the teaser is visible). */
  setBadge(visible: boolean): void {
    this.element.classList.toggle('ec-launcher--badge', visible);
  }
}

export interface TeaserHandlers {
  onOpen(): void;
  onDismiss(): void;
}

/** The one-time teaser bubble: tapping it opens the chat; its X dismisses it. */
export function createTeaser(config: WidgetConfig, handlers: TeaserHandlers): HTMLElement {
  const body = el('button', { type: 'button', class: 'ec-teaser-body' }, [
    avatar(config.bot_name),
    el('span', {}, [
      el('strong', {}, [`${config.bot_name} · ${config.company_name} AI assistant`]),
      el('span', {}, [config.teaser]),
    ]),
  ]);
  body.addEventListener('click', handlers.onOpen);

  const dismiss = el('button', { type: 'button', class: 'ec-teaser-close', 'aria-label': 'Dismiss message' }, [
    icon('close', 14),
  ]);
  dismiss.addEventListener('click', handlers.onDismiss);

  return el('div', { class: 'ec-teaser', role: 'status' }, [body, dismiss]);
}
