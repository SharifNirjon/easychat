/** The conversation list: bubbles, typing indicator (W-03), streamed reply (W-02), error + Retry. */
import { el } from '../dom';
import { icon } from '../icons';
import type { ChatMessage, Role } from '../types';

/** A reply bubble that grows as tokens stream in. */
export interface ReplyBubble {
  append(text: string): void;
  remove(): void;
}

function avatar(): HTMLElement {
  return el('div', { class: 'ec-avatar ec-avatar--sm', 'aria-hidden': 'true' }, [icon('spark', 14)]);
}

export class MessageList {
  readonly element: HTMLDivElement;
  private transient: HTMLElement | null = null; // typing indicator or error row

  constructor(private readonly botName: string) {
    // Screen-reader announcements happen in one place (Panel.announce), so this list
    // isn't a live region — otherwise every streamed token would be read out.
    this.element = el('div', { class: 'ec-messages', role: 'list', 'aria-label': `Conversation with ${botName}` });
  }

  render(messages: ChatMessage[]): void {
    this.element.replaceChildren(...messages.map((m) => this.row(m.role, m.content).row));
  }

  addUser(text: string): void {
    this.element.append(this.row('user', text).row);
  }

  showTyping(): void {
    const dots = el('span', { class: 'ec-dots', 'aria-hidden': 'true' }, [el('i'), el('i'), el('i')]);
    const label = el('span', { class: 'ec-sr' }, [`${this.botName} is typing`]);
    this.setTransient(this.assistantRow(el('div', { class: 'ec-bubble ec-bubble--typing' }, [dots, label])));
  }

  showError(message: string, onRetry: () => void): void {
    const retry = el('button', { type: 'button', class: 'ec-retry' }, [icon('retry', 14), 'Retry']);
    retry.addEventListener('click', onRetry, { once: true });
    const bubble = el('div', { class: 'ec-bubble ec-bubble--error', role: 'alert' }, [
      el('p', {}, [message]),
      retry,
    ]);
    this.setTransient(this.assistantRow(bubble));
  }

  clearTransient(): void {
    this.transient?.remove();
    this.transient = null;
  }

  startReply(): ReplyBubble {
    this.clearTransient();
    const { row, bubble } = this.row('assistant', '');
    this.element.append(row);
    return {
      append: (text) => bubble.append(text), // text node, never parsed as HTML
      remove: () => row.remove(),
    };
  }

  private setTransient(row: HTMLElement): void {
    this.clearTransient();
    this.transient = row;
    this.element.append(row);
  }

  private row(role: Role, text: string): { row: HTMLElement; bubble: HTMLElement } {
    const bubble = el('div', { class: `ec-bubble ec-bubble--${role}` }, [text]);
    const row = role === 'user' ? el('div', { class: 'ec-row ec-row--user', role: 'listitem' }, [bubble]) : this.assistantRow(bubble);
    return { row, bubble };
  }

  private assistantRow(bubble: HTMLElement): HTMLElement {
    return el('div', { class: 'ec-row ec-row--assistant', role: 'listitem' }, [avatar(), bubble]);
  }
}
