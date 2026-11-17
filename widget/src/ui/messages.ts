/**
 * The conversation list: bubbles, blocks under assistant replies, typing indicator (W-03),
 * streamed reply (W-02), error + Retry.
 */
import { el } from '../dom';
import { icon } from '../icons';
import type { ActionHandler, Block, ChatMessage, Role } from '../types';
import { renderBlock } from './blocks';

/** A reply bubble that grows as tokens stream in. */
export interface ReplyBubble {
  append(text: string): void;
  remove(): void;
}

/** The bot's initial in a small circle, as in the Mira designs. */
export function avatar(botName: string, size: 'sm' | 'lg' = 'sm'): HTMLElement {
  return el('div', { class: `ec-avatar ec-avatar--${size}`, 'aria-hidden': 'true' }, [botName.charAt(0).toUpperCase()]);
}

export class MessageList {
  readonly element: HTMLDivElement;
  private transient: HTMLElement | null = null; // typing indicator or error row

  constructor(
    private readonly botName: string,
    private readonly onAction: ActionHandler,
  ) {
    // Screen-reader announcements happen in one place (Panel.announce), so this list
    // isn't a live region — otherwise every streamed token would be read out.
    this.element = el('div', { class: 'ec-messages', role: 'list', 'aria-label': `Conversation with ${botName}` });
  }

  render(messages: ChatMessage[]): void {
    this.element.replaceChildren();
    for (const message of messages) {
      this.element.append(this.row(message.role, message.content).row);
      if (message.blocks?.length) this.addBlocks(message.blocks);
    }
    // Only the latest reply's blocks stay interactive.
    const last = messages[messages.length - 1];
    const keepLast = last?.role === 'assistant' && Boolean(last.blocks?.length);
    this.retireBlocks(keepLast ? this.element.lastElementChild : null);
  }

  addUser(text: string): void {
    this.retireBlocks(null); // the visitor moved on: earlier chips/choices can't be used twice
    this.element.append(this.row('user', text).row);
  }

  addBlocks(blocks: Block[]): void {
    const rendered = blocks
      .map((block) => renderBlock(block, this.onAction))
      .filter((node): node is HTMLElement => node !== null);
    if (rendered.length) this.element.append(el('div', { class: 'ec-blocks', role: 'listitem' }, rendered));
  }

  showTyping(): void {
    const dots = el('span', { class: 'ec-dots', 'aria-hidden': 'true' }, [el('i'), el('i'), el('i')]);
    const label = el('span', { class: 'ec-sr' }, [`${this.botName} is typing`]);
    this.setTransient(this.assistantRow(el('div', { class: 'ec-bubble ec-bubble--typing' }, [dots, label])));
  }

  showError(message: string, onRetry: () => void): void {
    const retry = el('button', { type: 'button', class: 'ec-retry' }, [icon('retry', 14), 'Retry']);
    retry.addEventListener('click', onRetry, { once: true });
    const bubble = el('div', { class: 'ec-bubble ec-bubble--error', role: 'alert' }, [el('p', {}, [message]), retry]);
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

  /** Disable send-buttons and inputs in every block group except `keep` (links stay usable). */
  private retireBlocks(keep: Element | null): void {
    this.element.querySelectorAll('.ec-blocks').forEach((group) => {
      if (group === keep) return;
      group.classList.add('is-retired');
      group.querySelectorAll<HTMLButtonElement | HTMLInputElement | HTMLTextAreaElement>('button, input, textarea')
        .forEach((control) => { control.disabled = true; });
    });
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
    return el('div', { class: 'ec-row ec-row--assistant', role: 'listitem' }, [avatar(this.botName), bubble]);
  }
}
