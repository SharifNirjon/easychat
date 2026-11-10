/**
 * The widget controller: owns the conversation state and wires the UI pieces together.
 * UI classes only render and report events; all decisions live here.
 */
import { streamChat } from './api';
import { MAX_HISTORY_TURNS, TEASER_DELAY_MS } from './config';
import { el } from './dom';
import { loadState, saveState, type SavedState } from './store';
import type { ChatMessage, WidgetConfig } from './types';
import { createTeaser, Launcher } from './ui/launcher';
import { Panel } from './ui/panel';

const RESUME_ERROR = "Your last message didn't get a reply.";

export class Widget {
  private readonly root: HTMLElement;
  private readonly launcher: Launcher;
  private readonly panel: Panel;
  private teaser: HTMLElement | null = null;
  private state: SavedState;
  private inFlight: AbortController | null = null;

  constructor(
    private readonly config: WidgetConfig,
    private readonly shadow: ShadowRoot,
  ) {
    this.state = loadState(config.site_id);

    this.launcher = new Launcher(config.bot_name, () => (this.state.open ? this.close() : this.open()));
    this.panel = new Panel(config, {
      onSend: (text) => this.send(text),
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

  open({ focus = true } = {}): void {
    this.hideTeaser();
    this.setOpen(true);
    this.panel.scrollToBottom();
    if (focus) this.panel.focusInput();
  }

  close(): void {
    // Only move focus back to the launcher if it was inside the widget (don't yank it from the page).
    const focusWasInside = this.shadow.activeElement !== null;
    this.setOpen(false);
    if (focusWasInside) this.launcher.element.focus();
  }

  private setOpen(open: boolean): void {
    this.state.open = open;
    this.panel.element.hidden = !open;
    this.root.toggleAttribute('data-open', open);
    this.launcher.setOpen(open);
    this.persist();
  }

  private send(text: string): void {
    this.remember({ role: 'user', content: text });
    this.panel.setWelcomeVisible(false);
    this.panel.messages.clearTransient();
    this.panel.messages.addUser(text);
    this.panel.scrollToBottom();
    void this.requestReply();
  }

  /** Stream a reply to the current history. Also used by Retry. */
  private async requestReply(): Promise<void> {
    this.inFlight?.abort();
    const controller = new AbortController();
    this.inFlight = controller;

    const { messages: list } = this.panel;
    list.showTyping();
    this.panel.setBusy(true);
    this.panel.scrollToBottom();

    let reply = '';
    let bubble: ReturnType<typeof list.startReply> | null = null;

    await streamChat(
      this.config.site_id,
      this.state.messages,
      {
        onToken: (text) => {
          bubble ??= list.startReply(); // first token replaces the typing indicator
          bubble.append(text);
          reply += text;
          this.panel.scrollToBottom();
        },
        onDone: () => {
          this.remember({ role: 'assistant', content: reply });
          this.panel.announce(`${this.config.bot_name}: ${reply}`);
        },
        onError: (message) => {
          bubble?.remove(); // drop a half-finished reply rather than leave it dangling
          this.showRetry(message);
        },
      },
      controller.signal,
    );

    if (this.inFlight === controller) this.inFlight = null;
    this.panel.setBusy(false);
  }

  private showRetry(message: string): void {
    this.panel.messages.showError(message, () => void this.requestReply());
    this.panel.announce(message);
    this.panel.scrollToBottom();
  }

  private restoreConversation(): void {
    const { messages } = this.state;
    this.panel.messages.render(messages);
    this.panel.setWelcomeVisible(messages.length === 0);
    // The page was left mid-reply: offer to finish it.
    if (messages.length > 0 && messages[messages.length - 1].role === 'user') this.showRetry(RESUME_ERROR);
  }

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

  private hideTeaser(): void {
    this.teaser?.remove();
    this.teaser = null;
    this.launcher.setBadge(false);
  }

  /** Tenant brand colors override the design-system defaults. */
  private applyTheme(): void {
    const { theme } = this.config;
    const isHex = (value: string) => /^#[0-9a-f]{6}$/i.test(value);
    if (isHex(theme.primary)) this.root.style.setProperty('--ec-pine', theme.primary);
    if (isHex(theme.primary_dark)) this.root.style.setProperty('--ec-pine9', theme.primary_dark);
    if (isHex(theme.accent)) this.root.style.setProperty('--ec-lime', theme.accent);
  }

  /** Add a message, keeping only the most recent turns (the API drops older ones anyway). */
  private remember(message: ChatMessage): void {
    this.state.messages = [...this.state.messages, message].slice(-MAX_HISTORY_TURNS);
    this.persist();
  }

  private persist(): void {
    saveState(this.config.site_id, this.state);
  }
}
