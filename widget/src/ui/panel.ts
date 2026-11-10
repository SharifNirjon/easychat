/** The open chat panel (D-02 / M-02): header, welcome (W-01), messages, composer, footer. */
import { MAX_MESSAGE_CHARS } from '../config';
import { el, withAccent } from '../dom';
import { icon } from '../icons';
import type { WidgetConfig } from '../types';
import { MessageList } from './messages';

/** Composer grows with its content up to this height, then scrolls. */
const MAX_INPUT_HEIGHT_PX = 120;

export interface PanelHandlers {
  onSend(text: string): void;
  onMinimize(): void;
}

export class Panel {
  readonly element: HTMLElement;
  readonly messages: MessageList;
  private readonly body: HTMLElement;
  private readonly welcome: HTMLElement;
  private readonly input: HTMLTextAreaElement;
  private readonly sendButton: HTMLButtonElement;
  private readonly liveRegion: HTMLElement;
  private busy = false;

  constructor(config: WidgetConfig, private readonly handlers: PanelHandlers) {
    this.messages = new MessageList(config.bot_name);
    this.welcome = this.buildWelcome(config);
    this.body = el('div', { class: 'ec-body' }, [this.welcome, this.messages.element]);

    this.input = el('textarea', {
      class: 'ec-input',
      rows: '1',
      maxlength: String(MAX_MESSAGE_CHARS),
      placeholder: `Message ${config.bot_name}…`,
      'aria-label': `Message ${config.bot_name}`,
    });
    this.sendButton = el('button', { type: 'submit', class: 'ec-send', 'aria-label': 'Send message', disabled: true }, [
      icon('send', 16),
    ]);
    this.liveRegion = el('div', { class: 'ec-sr', 'aria-live': 'polite' });

    this.element = el(
      'section',
      { class: 'ec-panel', id: 'ec-panel', role: 'dialog', 'aria-label': `Chat with ${config.bot_name}`, hidden: true },
      [this.buildHeader(config), this.body, this.buildComposer(), this.buildFooter(config), this.liveRegion],
    );
  }

  focusInput(): void {
    this.input.focus();
  }

  /** While a reply streams the visitor can keep typing, but can't send yet. */
  setBusy(busy: boolean): void {
    this.busy = busy;
    this.updateSendButton();
  }

  setWelcomeVisible(visible: boolean): void {
    this.welcome.hidden = !visible;
  }

  scrollToBottom(): void {
    this.body.scrollTop = this.body.scrollHeight;
  }

  /** Tell screen readers about something new (one polite announcement, not per token). */
  announce(text: string): void {
    this.liveRegion.textContent = text;
  }

  private buildHeader(config: WidgetConfig): HTMLElement {
    const minimize = el('button', { type: 'button', class: 'ec-icon-btn', 'aria-label': 'Minimize chat' }, [
      icon('chevronDown', 20),
    ]);
    minimize.addEventListener('click', () => this.handlers.onMinimize());

    return el('header', { class: 'ec-header' }, [
      el('div', { class: 'ec-avatar ec-avatar--online', 'aria-hidden': 'true' }, [icon('spark', 18)]),
      el('div', { class: 'ec-header-text' }, [
        el('div', { class: 'ec-header-name' }, [config.bot_name, el('span', { class: 'ec-badge' }, ['AI'])]),
        el('div', { class: 'ec-header-sub' }, ['AI assistant · Replies instantly']),
      ]),
      minimize,
    ]);
  }

  private buildWelcome(config: WidgetConfig): HTMLElement {
    const { greeting } = config;
    const prompts = config.suggested_prompts.map((prompt) => {
      const button = el('button', { type: 'button', class: 'ec-prompt' }, [
        el('span', {}, [prompt]),
        icon('chevronRight', 16),
      ]);
      button.addEventListener('click', () => this.trySend(prompt));
      return el('li', {}, [button]);
    });

    return el('div', { class: 'ec-welcome' }, [
      el('h2', { class: 'ec-welcome-title' }, [greeting.title, el('br'), ...withAccent(greeting.headline)]),
      el('p', { class: 'ec-welcome-sub' }, [greeting.subtitle]),
      el('p', { class: 'ec-note' }, [icon('info', 16), el('span', {}, [config.disclosure])]),
      ...(prompts.length
        ? [el('p', { class: 'ec-label', id: 'ec-try' }, ['Try asking']), el('ul', { class: 'ec-prompts', 'aria-labelledby': 'ec-try' }, prompts)]
        : []),
    ]);
  }

  private buildComposer(): HTMLElement {
    const form = el('form', { class: 'ec-composer' }, [this.input, this.sendButton]);

    form.addEventListener('submit', (event) => {
      event.preventDefault();
      this.trySend(this.input.value);
    });
    // Enter sends; Shift+Enter inserts a newline.
    this.input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        this.trySend(this.input.value);
      }
    });
    this.input.addEventListener('input', () => {
      this.autoGrow();
      this.updateSendButton();
    });
    return form;
  }

  private buildFooter(config: WidgetConfig): HTMLElement {
    return el('footer', { class: 'ec-footer' }, [
      'AI can make mistakes · ',
      el('a', { href: config.privacy_url, target: '_blank', rel: 'noopener noreferrer' }, ['Privacy']),
      ' · Powered by ',
      el('strong', {}, ['easychat']),
    ]);
  }

  private trySend(raw: string): void {
    const text = raw.trim();
    if (!text || this.busy) return;
    this.input.value = '';
    this.autoGrow();
    this.updateSendButton();
    this.handlers.onSend(text);
  }

  private autoGrow(): void {
    this.input.style.height = 'auto';
    this.input.style.height = `${Math.min(this.input.scrollHeight, MAX_INPUT_HEIGHT_PX)}px`;
  }

  private updateSendButton(): void {
    this.sendButton.disabled = this.busy || this.input.value.trim() === '';
  }
}
