/**
 * The open chat panel (Mira shell, `docs/designs/widget designs/`): header, home screen,
 * messages, composer, footer. Controls for later roadmap steps (cart, "⋯" menu, mic,
 * attachments) are intentionally not rendered until they work.
 */
import { MAX_MESSAGE_CHARS } from '../config';
import { el, withAccent } from '../dom';
import { icon, isIconName } from '../icons';
import type { Action, ActionHandler, HomeAction, WidgetConfig } from '../types';
import { avatar, MessageList } from './messages';
import { actionControl } from './blocks/shared';

/** Composer grows with its content up to this height, then scrolls. */
const MAX_INPUT_HEIGHT_PX = 120;
const FALLBACK_ICON = 'sparkle';

export interface PanelHandlers {
  onSend(text: string): void;
  onAction: ActionHandler;
  onMinimize(): void;
}

/** A home tile sends its message or opens its URL (the API guarantees exactly one). */
function homeAction(item: HomeAction): Action {
  return item.url ? { kind: 'link', url: item.url } : { kind: 'send', text: item.message ?? item.title };
}

/** A home tile's icon, or the default sparkle if the config names an icon we don't ship. */
function homeIcon(name: string) {
  return icon(isIconName(name) ? name : FALLBACK_ICON, 18);
}

/** The open chat panel: header, home screen, messages, composer and footer. */
export class Panel {
  readonly element: HTMLElement;
  readonly messages: MessageList;
  private readonly body: HTMLElement;
  private readonly home: HTMLElement;
  private readonly input: HTMLTextAreaElement;
  private readonly sendButton: HTMLButtonElement;
  private readonly liveRegion: HTMLElement;
  private busy = false;

  constructor(config: WidgetConfig, private readonly handlers: PanelHandlers) {
    this.messages = new MessageList(config.bot_name, handlers.onAction);
    this.home = this.buildHome(config);
    this.body = el('div', { class: 'ec-body' }, [this.home, this.messages.element]);

    this.input = el('textarea', {
      class: 'ec-input',
      rows: '1',
      maxlength: String(MAX_MESSAGE_CHARS),
      placeholder: `Message ${config.bot_name}…`,
      'aria-label': `Message ${config.bot_name}`,
    });
    this.sendButton = el('button', { type: 'submit', class: 'ec-send', 'aria-label': 'Send message', disabled: true }, [
      icon('send', 18),
    ]);
    this.liveRegion = el('div', { class: 'ec-sr', 'aria-live': 'polite' });

    this.element = el(
      'section',
      { class: 'ec-panel', id: 'ec-panel', role: 'dialog', 'aria-label': `Chat with ${config.bot_name}`, hidden: true },
      [this.buildHeader(config), this.body, this.buildComposer(), this.buildFooter(config), this.liveRegion],
    );
  }

  /** Move keyboard focus to the message box. */
  focusInput(): void {
    this.input.focus();
  }

  /** While a reply streams the visitor can keep typing, but can't send yet. */
  setBusy(busy: boolean): void {
    this.busy = busy;
    this.updateSendButton();
  }

  /** Show the home screen (new chat) or hide it (once there are messages). */
  setHomeVisible(visible: boolean): void {
    this.home.hidden = !visible;
  }

  /** Scroll to the newest message. */
  scrollToBottom(): void {
    this.body.scrollTop = this.body.scrollHeight;
  }

  /** Scroll to the top (the home screen's greeting). */
  scrollToTop(): void {
    this.body.scrollTop = 0;
  }

  /** Tell screen readers about something new (one polite announcement, not per token). */
  announce(text: string): void {
    this.liveRegion.textContent = text;
  }

  /** Send text unless a reply is still streaming. Used by the composer and by "send" actions. */
  trySend(raw: string): void {
    const text = raw.trim();
    if (!text || this.busy) return;
    this.handlers.onSend(text);
  }

  /** Header: avatar, bot name with an AI badge, 'replies instantly' status, minimize button. */
  private buildHeader(config: WidgetConfig): HTMLElement {
    const minimize = el('button', { type: 'button', class: 'ec-icon-btn', 'aria-label': 'Minimize chat' }, [icon('minus', 20)]);
    minimize.addEventListener('click', () => this.handlers.onMinimize());

    return el('header', { class: 'ec-header' }, [
      avatar(config.bot_name, 'lg'),
      el('div', { class: 'ec-header-text' }, [
        el('div', { class: 'ec-header-name' }, [config.bot_name, el('span', { class: 'ec-badge' }, ['AI'])]),
        el('div', { class: 'ec-header-sub' }, [
          el('span', { class: 'ec-online', 'aria-hidden': 'true' }),
          `${config.company_name} · replies instantly`,
        ]),
      ]),
      minimize,
    ]);
  }

  /** Home screen (Mira `widget.png`): greeting, optional disclosure, promo, 2×2 cards, chips. */
  private buildHome(config: WidgetConfig): HTMLElement {
    const { greeting, home } = config;
    const onAction = this.handlers.onAction;
    const parts: HTMLElement[] = [
      el('h2', { class: 'ec-home-title' }, [greeting.title, el('br'), ...withAccent(greeting.headline)]),
      el('p', { class: 'ec-home-sub' }, [greeting.subtitle]),
    ];

    if (config.disclosure) parts.push(el('p', { class: 'ec-note' }, [icon('info', 16), el('span', {}, [config.disclosure])]));

    if (home.promo) {
      parts.push(actionControl(homeAction(home.promo), 'ec-promo', [
        el('span', { class: 'ec-tile-icon ec-tile-icon--white' }, [homeIcon(home.promo.icon)]),
        el('span', { class: 'ec-promo-text' }, [
          el('span', { class: 'ec-promo-title' }, [home.promo.title]),
          ...(home.promo.subtitle ? [el('span', { class: 'ec-promo-sub' }, [home.promo.subtitle])] : []),
        ]),
        icon('chevronRight', 18),
      ], onAction));
    }

    if (home.cards.length) {
      parts.push(el('div', { class: 'ec-home-cards' }, home.cards.map((card) =>
        actionControl(homeAction(card), 'ec-home-card', [
          el('span', { class: 'ec-tile-icon' }, [homeIcon(card.icon)]),
          el('span', { class: 'ec-home-card-title' }, [card.title]),
          ...(card.subtitle ? [el('span', { class: 'ec-home-card-sub' }, [card.subtitle])] : []),
        ], onAction),
      )));
    }

    if (home.chips.length) {
      parts.push(
        el('p', { class: 'ec-label' }, [home.chips_label]),
        el('div', { class: 'ec-chips' }, home.chips.map((chip) =>
          actionControl({ kind: 'send', text: chip }, 'ec-chip', [chip], onAction),
        )),
      );
    }
    return el('div', { class: 'ec-home' }, parts);
  }

  /** Message box and send button. Enter sends; Shift+Enter adds a new line. */
  private buildComposer(): HTMLElement {
    const form = el('form', { class: 'ec-composer' }, [el('div', { class: 'ec-input-wrap' }, [this.input]), this.sendButton]);

    form.addEventListener('submit', (event) => {
      event.preventDefault();
      this.submitInput();
    });
    // Enter sends; Shift+Enter inserts a newline.
    this.input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        this.submitInput();
      }
    });
    this.input.addEventListener('input', () => {
      this.autoGrow();
      this.updateSendButton();
    });
    return form;
  }

  /** Footer: AI disclaimer, the tenant's privacy link, and the easychat credit. */
  private buildFooter(config: WidgetConfig): HTMLElement {
    return el('footer', { class: 'ec-footer' }, [
      'AI assistant · can make mistakes · ',
      el('a', { href: config.privacy_url, target: '_blank', rel: 'noopener noreferrer' }, ['Privacy']),
      ' · Powered by ',
      el('strong', {}, ['easychat']),
    ]);
  }

  /** Send what's in the message box and clear it (ignored while a reply streams). */
  private submitInput(): void {
    if (this.busy || !this.input.value.trim()) return;
    const text = this.input.value;
    this.input.value = '';
    this.autoGrow();
    this.updateSendButton();
    this.trySend(text);
  }

  /** Grow the message box with its content, up to MAX_INPUT_HEIGHT_PX. */
  private autoGrow(): void {
    this.input.style.height = 'auto';
    this.input.style.height = `${Math.min(this.input.scrollHeight, MAX_INPUT_HEIGHT_PX)}px`;
  }

  /** Enable Send only when there is text and no reply is streaming. */
  private updateSendButton(): void {
    this.sendButton.disabled = this.busy || this.input.value.trim() === '';
  }
}
