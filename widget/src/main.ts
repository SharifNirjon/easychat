/**
 * Entry point for widget.js. Embedded on a tenant's site as:
 *   <script src=".../widget.js" data-site="culpine" async></script>
 *
 * Contract with the host page: never throw, never touch its styles or layout.
 * The only thing added outside our Shadow DOM is a Google Fonts <link> (font
 * definitions only — it changes nothing on the page unless the page uses those fonts).
 */
import { fetchConfig } from './api';
import { FONT_STYLESHEET } from './config';
import styles from './styles.css?inline';
import { Widget } from './widget';

const HOST_TAG = 'easychat-widget';

// Must be read synchronously at load: currentScript is null after any await.
const ownScript = document.currentScript as HTMLScriptElement | null;

function findSiteId(): string | null {
  const script = ownScript ?? document.querySelector<HTMLScriptElement>('script[data-site]');
  return script?.dataset.site ?? null;
}

function loadFonts(): void {
  if (document.querySelector(`link[href="${FONT_STYLESHEET}"]`)) return;
  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = FONT_STYLESHEET;
  document.head.append(link);
}

/** A zero-size custom element whose Shadow DOM holds everything we render. */
function createShadowHost(): ShadowRoot {
  const host = document.createElement(HOST_TAG);
  document.body.append(host);
  const shadow = host.attachShadow({ mode: 'open' });
  const style = document.createElement('style');
  style.textContent = styles;
  shadow.append(style);
  return shadow;
}

async function boot(siteId: string): Promise<void> {
  const config = await fetchConfig(siteId);
  loadFonts();
  new Widget(config, createShadowHost());
}

function start(): void {
  const flag = window as Window & { __easychatLoaded?: boolean };
  if (flag.__easychatLoaded) return; // script tag included twice
  flag.__easychatLoaded = true;

  const siteId = findSiteId();
  if (!siteId) {
    console.warn('easychat: missing data-site attribute on the script tag');
    return;
  }
  const run = () => boot(siteId).catch((err) => console.warn('easychat: widget disabled —', err));
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', run, { once: true });
  else void run();
}

try {
  start();
} catch (err) {
  console.warn('easychat: widget disabled —', err);
}
