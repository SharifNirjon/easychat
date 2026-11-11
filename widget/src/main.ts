/**
 * Entry point for widget.js. Embedded on a tenant's site as:
 *   <script src=".../widget.js" data-key="pk_..." async></script>
 *
 * Contract with the host page: never throw, never touch its styles or layout, add nothing to
 * its <head>. The only page-level side effect is registering two font faces under private
 * family names (easychat-sans / easychat-serif), loaded from the widget's own origin.
 */
import { fetchConfig } from './api';
import { FONTS } from './config';
import styles from './styles.css?inline';
import { Widget } from './widget';

const HOST_TAG = 'easychat-widget';

// Must be read synchronously at load: currentScript is null after any await. There is no
// fallback search for "some script with data-key": it could pick up another vendor's tag.
const ownScript = document.currentScript as HTMLScriptElement | null;

/** Register the self-hosted fonts. Failures (e.g. a strict font-src CSP) fall back to system fonts. */
function loadFonts(scriptSrc: string): void {
  if (typeof FontFace === 'undefined' || !document.fonts) return;
  for (const font of FONTS) {
    try {
      const url = new URL(`fonts/${font.file}`, scriptSrc).href;
      const face = new FontFace(font.family, `url("${url}") format("woff2")`, {
        display: 'swap',
        ...font.descriptors,
      });
      document.fonts.add(face);
      face.load().catch(() => undefined);
    } catch {
      // ignore: typography degrades, the widget still works
    }
  }
}

/**
 * Constructable stylesheets aren't subject to CSP 'unsafe-inline' rules, so the widget keeps
 * its look on hosts with a strict style-src. Older browsers fall back to a <style> element.
 */
function applyStyles(shadow: ShadowRoot): void {
  try {
    const sheet = new CSSStyleSheet();
    sheet.replaceSync(styles);
    shadow.adoptedStyleSheets = [sheet];
  } catch {
    const style = document.createElement('style');
    style.textContent = styles;
    shadow.append(style);
  }
}

/** A zero-size custom element whose Shadow DOM holds everything we render. */
function createShadowHost(): ShadowRoot {
  const host = document.createElement(HOST_TAG);
  document.body.append(host);
  const shadow = host.attachShadow({ mode: 'open' });
  applyStyles(shadow);
  return shadow;
}

async function boot(siteKey: string, scriptSrc: string): Promise<void> {
  const config = await fetchConfig(siteKey);
  loadFonts(scriptSrc);
  new Widget(siteKey, config, createShadowHost());
}

function start(): void {
  const flag = window as Window & { __easychatLoaded?: boolean };
  if (flag.__easychatLoaded) return; // script tag included twice
  flag.__easychatLoaded = true;

  const siteKey = ownScript?.dataset.key;
  if (!ownScript || !siteKey) {
    console.warn('easychat: add data-key="pk_..." to the widget <script> tag (and load it as a classic script)');
    return;
  }
  const scriptSrc = ownScript.src;
  const run = () => boot(siteKey, scriptSrc).catch((err) => console.warn('easychat: widget disabled —', err));
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', run, { once: true });
  else void run();
}

try {
  start();
} catch (err) {
  console.warn('easychat: widget disabled —', err);
}
