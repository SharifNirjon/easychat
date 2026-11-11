/** Build-time settings and widget-wide constants. */

/** API origin, baked in at build time from VITE_API_BASE. vite.config.ts fails the build without it. */
export const API_BASE: string = String(import.meta.env.VITE_API_BASE).replace(/\/$/, '');

/** Same limit the API applies to a single message. */
export const MAX_MESSAGE_CHARS = 4000;

/** How many messages the widget keeps for display. The server keeps the real history. */
export const MAX_DISPLAY_MESSAGES = 40;

/** The proactive teaser appears this long after page load, once per tab session. */
export const TEASER_DELAY_MS = 5000;

/**
 * Self-hosted fonts (served next to widget.js, never from Google: no visitor IPs leave for a
 * third party). Registered under private family names so they can't clash with the host page.
 */
export const FONTS: ReadonlyArray<{ family: string; file: string; descriptors: FontFaceDescriptors }> = [
  {
    family: 'easychat-sans',
    file: 'plus-jakarta-sans-latin-wght.woff2',
    descriptors: {
      weight: '200 800',
      unicodeRange:
        'U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,U+0329,U+2000-206F,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD',
    },
  },
  {
    family: 'easychat-sans',
    file: 'plus-jakarta-sans-latin-ext-wght.woff2',
    descriptors: {
      weight: '200 800',
      unicodeRange:
        'U+0100-02BA,U+02BD-02C5,U+02C7-02CC,U+02CE-02D7,U+02DD-02FF,U+0304,U+0308,U+0329,U+1D00-1DBF,U+1E00-1E9F,U+1EF2-1EFF,U+2020,U+20A0-20AB,U+20AD-20C0,U+2113,U+2C60-2C7F,U+A720-A7FF',
    },
  },
  {
    family: 'easychat-serif',
    file: 'instrument-serif-latin-italic.woff2',
    descriptors: { style: 'italic', weight: '400' },
  },
];
