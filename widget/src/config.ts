/** Build-time settings and widget-wide constants. */

/** API origin, baked in at build time from VITE_API_BASE (see widget/.env.example). */
export const API_BASE: string = (import.meta.env.VITE_API_BASE || 'http://localhost:8000').replace(/\/$/, '');

/** Same limit the API applies: only the most recent messages are kept and sent. */
export const MAX_HISTORY_TURNS = 10;

/** Same limit the API applies to a single message. */
export const MAX_MESSAGE_CHARS = 4000;

/** The proactive teaser appears this long after page load, once per tab session. */
export const TEASER_DELAY_MS = 5000;

export const FONT_STYLESHEET =
  'https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=Instrument+Serif:ital@1&display=swap';
