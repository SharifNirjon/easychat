/**
 * Inline SVG icons (24×24, stroke-based), built with DOM APIs — no innerHTML.
 * The names tenants may use in config/blocks must match `Icon` in api/blocks.py.
 */

const SVG_NS = 'http://www.w3.org/2000/svg';

const PATHS = {
  // Tenant-usable icons (keep in sync with api/blocks.py `Icon`)
  sparkle: 'M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8Z',
  truck: 'M3 6h11v10H3ZM14 9h4l3 3v4h-7M7 20a2 2 0 1 0 0-4 2 2 0 0 0 0 4ZM17 20a2 2 0 1 0 0-4 2 2 0 0 0 0 4Z',
  return: 'M4 12a8 8 0 1 0 2.3-5.7L4 8.6M4 4v4.6h4.6',
  gift: 'M4 11h16v9H4ZM3 7h18v4H3ZM12 7v13M12 7C10.5 4 7 4 7 6s3 1 5 1Zm0 0c1.5-3 5-3 5-1s-3 1-5 1Z',
  tag: 'M20.5 12.5 12 21l-9-9V3h9ZM7.5 7.5h.01',
  box: 'M21 8l-9-5-9 5v8l9 5 9-5ZM3 8l9 5 9-5M12 13v8',
  grid: 'M4 4h7v7H4ZM13 4h7v7h-7ZM4 13h7v7H4ZM13 13h7v7h-7Z',
  calculator: 'M6 3h12v18H6ZM9 7h6M9 12h.01M12 12h.01M15 12h.01M9 16h.01M12 16h.01M15 16h.01',
  calendar: 'M4 6h16v15H4ZM4 10h16M8 3v5M16 3v5',
  play: 'M12 3a9 9 0 1 1 0 18 9 9 0 0 1 0-18ZM10 8.5v7l6-3.5Z',
  chat: 'M5 5h14a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H10l-4 3.5V17H5a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2Z',
  star: 'M12 3l2.8 5.7 6.2.9-4.5 4.4 1 6.2L12 17.3l-5.5 2.9 1-6.2L3 9.6l6.2-.9Z',
  info: 'M12 8h.01M11 12h1v5h1M12 3a9 9 0 1 1 0 18 9 9 0 0 1 0-18Z',
  file: 'M7 3h7l5 5v13H7ZM14 3v5h5',
  check: 'M5 12.5l4.5 4.5L19 7',
  bell: 'M6 16v-5a6 6 0 1 1 12 0v5l2 2H4ZM10 21h4',
  // Widget-internal icons
  close: 'M6 6l12 12M18 6 6 18',
  minus: 'M5 12h14',
  plus: 'M12 5v14M5 12h14',
  chevronRight: 'm9 6 6 6-6 6',
  send: 'M21 3 10 14M21 3l-7 18-4-7-7-4Z',
  external: 'M14 4h6v6M20 4l-9 9M18 14v6H4V6h6',
  retry: 'M4 4v6h6M20 12a8 8 0 0 1-14.9 4M4 10a8 8 0 0 1 14.9-2',
} as const;

export type IconName = keyof typeof PATHS;

export function isIconName(name: string): name is IconName {
  return Object.prototype.hasOwnProperty.call(PATHS, name);
}

export function icon(name: IconName, size = 18, { filled = false } = {}): SVGSVGElement {
  const svg = document.createElementNS(SVG_NS, 'svg');
  const attrs: Record<string, string> = {
    viewBox: '0 0 24 24',
    width: String(size),
    height: String(size),
    fill: filled ? 'currentColor' : 'none',
    stroke: 'currentColor',
    'stroke-width': '2',
    'stroke-linecap': 'round',
    'stroke-linejoin': 'round',
    'aria-hidden': 'true',
    focusable: 'false',
  };
  for (const [key, value] of Object.entries(attrs)) svg.setAttribute(key, value);

  const path = document.createElementNS(SVG_NS, 'path');
  path.setAttribute('d', PATHS[name]);
  svg.append(path);
  return svg;
}
