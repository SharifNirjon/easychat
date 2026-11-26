/**
 * The widget controller: owns the conversation state and wires the UI pieces together.
 * UI classes only render and report events; all decisions live here.
 */
import { type FailureKind, streamChat } from './api';
import { MAX_DISPLAY_MESSAGES, TEASER_DELAY_MS } from './config';
import { el } from './dom';
import { loadState, saveState, type SavedState } from './store';
import type { Action, Block, ChatMessage, FormName, FormSubmit, FormValues, WidgetConfig } from './types';
import { safeUrl } from './ui/blocks/shared';
import { createTeaser, Launcher } from './ui/launcher';
import { Panel } from './ui/panel';

const RESUME_ERROR = "Your last message didn't get a reply.";
/** Theme keys the tenant may set (api/models.py Theme); each becomes a --ec-<key> CSS variable. */
const THEME_TOKENS = ['primary', 'accent', 'surface', 'text'];

/** One widget instance: owns the conversation state and wires launcher, teaser and panel together. */
export class Widget {
  private readonly root: HTMLElement;
  private readonly launcher: Launcher;
  private readonly panel: Panel;
  private teaser: HTMLElement | null = null;
  private state: SavedState;
  private inFlight: AbortController | null = null;
  /** A submitted form not yet stored by the server. Memory only: emails and codes are never persisted. */
  private pendingForm: { name: FormName; values: FormValues } | null = null;

  constructor(
    private readonly siteKey: string,
    private readonly config: WidgetConfig,
    private readonly shadow: ShadowRoot,
  ) {
    this.state = loadState(siteKey);

    this.launcher = new Launcher(config.bot_name, () => (this.state.open ? this.close() : this.open()));
    this.panel = new Panel(config, {
      onSend: (text) => this.send(text),
      onAction: (action) => this.handleAction(action),
      onMinimize: () => this.close(),
    });

    this.root = el('div', { class: 'ec-root' }, [this.panel.element, this.launcher.element]);
    this.applyTheme();
    this.root.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && this.state.open) this.close();
    });
    shadow.append(this.root);

    this.restoreConversation();
    if (this.state.open) this.open({ focus: false }); // don't steal focus on page load
    else this.scheduleTeaser();
  }

  /** Open the panel (hides the teaser); `focus: false` when restoring an open panel on page load. */
  open({ focus = true } = {}): void {
    this.hideTeaser();
    this.setOpen(true);
    // A conversation opens at the newest message; the home screen opens at its greeting.
    if (this.state.messages.length) this.panel.scrollToBottom();
    else this.panel.scrollToTop();
    if (focus) this.panel.focusInput();
  }

  /** Close the panel, returning focus to the launcher only if it was inside the widget. */
  close(): void {
    // Only move focus back to the launcher if it was inside the widget (don't yank it from the page).
    const focusWasInside = this.shadow.activeElement !== null;
    this.setOpen(false);
    if (focusWasInside) this.launcher.element.focus();
  }

  /** Apply the open/closed state everywhere it shows (panel, launcher, attribute) and save it. */
  private setOpen(open: boolean): void {
    this.state.open = open;
    this.panel.element.hidden = !open;
    this.root.toggleAttribute('data-open', open);
    this.launcher.setOpen(open);
    this.persist();
  }

  /** A tap on a chip, button, card or home tile, or a submitted form. Links are normally plain <a> elements. */
  private handleAction(action: Action | FormSubmit): void {
    if (action.kind === 'send') {
      this.panel.trySend(action.text);
      return;
    }
    if (action.kind === 'form') {
      this.submitForm(action);
      return;
    }
    const href = safeUrl(action.url);
    if (href) window.open(href, '_blank', 'noopener,noreferrer');
  }

  /** Show the visitor's message, remember it as not yet stored, and request a reply. */
  private send(text: string): void {
    this.state.unsent = text;
    this.remember({ role: 'user', content: text });
    this.panel.setHomeVisible(false);
    this.panel.messages.clearTransient();
    this.panel.messages.addUser(text);
    this.panel.scrollToBottom();
    void this.requestReply();
  }

  /** Send a chat form's values; the visitor's bubble shows only the button's label. */
  private submitForm(submit: FormSubmit): void {
    if (!this.state.conversationId) return; // forms only appear inside a conversation
    this.state.unsent = null;
    this.pendingForm = { name: submit.form, values: submit.values };
    this.remember({ role: 'user', content: submit.label });
    this.panel.messages.clearTransient();
    this.panel.messages.addUser(submit.label);
    this.panel.scrollToBottom();
    void this.requestReply();
  }

  /**
   * Stream a reply. Also used by Retry: if the server never confirmed storing the visitor's
   * message, it is sent again; otherwise the server is asked to answer the history it has.
   */
  private async requestReply(): Promise<void> {
    this.inFlight?.abort();
    const controller = new AbortController();
    this.inFlight = controller;

    const { messages: list } = this.panel;
    list.showTyping();
    this.panel.setBusy(true);
    this.panel.scrollToBottom();

    let reply = '';
    let forgotten = false; // the server deleted this conversation at the visitor's request
    const blocks: Block[] = [];
    let bubble: ReturnType<typeof list.startReply> | null = null;

    await streamChat(
      {
        siteKey: this.siteKey, conversationId: this.state.conversationId, message: this.state.unsent,
        form: this.pendingForm,
      },
      {
        onConversation: (id) => {
          // The server has stored the visitor's message (or the form's summary) under this conversation.
          this.state.conversationId = id;
          this.state.unsent = null;
          this.pendingForm = null;
          this.persist();
        },
        onToken: (text) => {
          bubble ??= list.startReply(); // first token replaces the typing indicator
          bubble.append(text);
          reply += text;
          this.panel.scrollToBottom();
        },
        onReplace: (text) => {
          bubble ??= list.startReply();
          bubble.replace(text);
          reply = text; // what is remembered for this tab is the safe text, never the blocked one
          this.panel.scrollToBottom();
        },
        onBlock: (block) => {
          blocks.push(block); // shown together once the reply is complete
        },
        onForget: () => {
          forgotten = true;
        },
        onDone: () => {
          if (blocks.length) list.addBlocks(blocks);
          if (forgotten) {
            this.forgetConversation(reply);
          } else {
            this.remember(blocks.length ? { role: 'assistant', content: reply, blocks } : { role: 'assistant', content: reply });
          }
          this.panel.announce(`${this.config.bot_name}: ${reply}`);
          this.panel.scrollToBottom();
        },
        onError: (message, kind) => {
          bubble?.remove(); // drop a half-finished reply rather than leave it dangling
          this.handleFailure(kind);
          this.showRetry(message);
        },
      },
      controller.signal,
    );

    if (this.inFlight === controller) this.inFlight = null;
    this.panel.setBusy(false);
  }

  /** If the server lost the conversation, make Retry start a new one with the last question. */
  private handleFailure(kind: FailureKind): void {
    if (kind !== 'conversation-gone') return;
    // The server no longer has this chat: Retry starts a new one with the last question.
    const lastUser = [...this.state.messages].reverse().find((m) => m.role === 'user');
    this.pendingForm = null; // a form belongs to its conversation: it can't move to a new one
    this.state.conversationId = null;
    this.state.unsent = lastUser?.content ?? null;
    this.persist();
  }

  /**
   * "Delete my data" went through: the server no longer has this conversation. Keep only the
   * confirmation on screen; the saved copy and the conversation id go, so the next message
   * starts a new chat.
   */
  private forgetConversation(confirmation: string): void {
    this.state.conversationId = null;
    this.state.unsent = null;
    this.state.messages = [{ role: 'assistant', content: confirmation }];
    this.persist();
    this.panel.messages.render(this.state.messages);
  }

  /** Show an error row with a Retry button, and announce it to screen readers. */
  private showRetry(message: string): void {
    this.panel.messages.showError(message, () => void this.requestReply());
    this.panel.announce(message);
    this.panel.scrollToBottom();
  }

  /** Re-render this tab's saved conversation; offer Retry if it ended on an unanswered message. */
  private restoreConversation(): void {
    const { messages } = this.state;
    this.panel.messages.render(messages);
    this.panel.setHomeVisible(messages.length === 0);
    // The page was left mid-reply: offer to finish it.
    if (messages.length > 0 && messages[messages.length - 1].role === 'user') this.showRetry(RESUME_ERROR);
  }

  /** Show the proactive teaser once per tab session, after TEASER_DELAY_MS, if the chat is still closed. */
  private scheduleTeaser(): void {
    if (this.state.teaserSeen || !this.config.teaser) return;
    window.setTimeout(() => {
      if (this.state.open || this.state.teaserSeen) return;
      this.teaser = createTeaser(this.config, {
        onOpen: () => this.open(),
        onDismiss: () => this.hideTeaser(),
      });
      this.root.prepend(this.teaser);
      this.launcher.setBadge(true);
      this.state.teaserSeen = true; // shown once per tab session
      this.persist();
    }, TEASER_DELAY_MS);
  }

  /** Remove the teaser and the launcher's attention badge. */
  private hideTeaser(): void {
    this.teaser?.remove();
    this.teaser = null;
    this.launcher.setBadge(false);
  }

  /** The tenant's seed colours; styles.css derives tints, borders and muted text from them. */
  private applyTheme(): void {
    /** Only plain #rrggbb values reach CSS: anything else could inject styles. */
    const isHex = (value: unknown) => typeof value === 'string' && /^#[0-9a-f]{6}$/i.test(value);
    for (const [name, value] of Object.entries(this.config.theme ?? {})) {
      if (THEME_TOKENS.includes(name) && isHex(value)) this.root.style.setProperty(`--ec-${name}`, value);
    }
  }

  /** Add a message to the display copy (the server keeps the authoritative history). */
  private remember(message: ChatMessage): void {
    this.state.messages = [...this.state.messages, message].slice(-MAX_DISPLAY_MESSAGES);
    this.persist();
  }

  /** Save this tab's state to sessionStorage. */
  private persist(): void {
    saveState(this.siteKey, this.state);
  }
}
