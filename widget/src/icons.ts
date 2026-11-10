/** Inline SVG icons (24×24, stroke-based), built with DOM APIs — no innerHTML. */

const SVG_NS = 'http://www.w3.org/2000/svg';

const PATHS = {
  chat: 'M5 5h14a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H10l-4 3.5V17H5a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2Z',
  close: 'M6 6l12 12M18 6 6 18',
  chevronDown: 'm6 9 6 6 6-6',
  chevronRight: 'm9 6 6 6-6 6',
  send: 'M5 12h14M13 6l6 6-6 6',
  spark: 'M12 3v4M12 17v4M3 12h4M17 12h4M12 9a3 3 0 1 1 0 6 3 3 0 0 1 0-6Z',
  info: 'M12 8h.01M11 12h1v5h1M12 3a9 9 0 1 1 0 18 9 9 0 0 1 0-18Z',
  retry: 'M4 4v6h6M20 12a8 8 0 0 1-14.9 4M4 10a8 8 0 0 1 14.9-2',
} as const;

export type IconName = keyof typeof PATHS;

export function icon(name: IconName, size = 18): SVGSVGElement {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('width', String(size));
  svg.setAttribute('height', String(size));
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '2');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');

  const path = document.createElementNS(SVG_NS, 'path');
  path.setAttribute('d', PATHS[name]);
  svg.append(path);
  return svg;
}
