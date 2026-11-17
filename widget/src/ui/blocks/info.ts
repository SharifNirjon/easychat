/** Sources (pages an answer came from, design 11) and notices. */
import { el } from '../../dom';
import { icon } from '../../icons';
import type { ActionHandler, Block } from '../../types';
import { actionControl, groupLabel, safeUrl } from './shared';

type SourcesBlock = Extract<Block, { type: 'sources' }>;
type NoticeBlock = Extract<Block, { type: 'notice' }>;

/** "example.com/help" — host and path, without scheme, query or trailing slash. */
function shortUrl(raw: string): string {
  const href = safeUrl(raw);
  if (!href) return '';
  const url = new URL(href);
  return `${url.host}${url.pathname}`.replace(/\/$/, '');
}

export function renderSources(block: SourcesBlock, onAction: ActionHandler): HTMLElement {
  return el('section', { class: 'ec-sources' }, [
    groupLabel('Sources'),
    ...block.items.map((source) =>
      actionControl({ kind: 'link', url: source.url }, 'ec-source', [
        icon('file', 18),
        el('span', {}, [
          el('span', { class: 'ec-source-title' }, [source.title]),
          el('span', { class: 'ec-source-url' }, [shortUrl(source.url)]),
        ]),
      ], onAction),
    ),
  ]);
}

export function renderNotice(block: NoticeBlock): HTMLElement {
  return el('p', { class: `ec-notice ec-tone--${block.tone ?? 'info'}`, role: 'note' }, [icon('info', 18), el('span', {}, [block.text])]);
}
