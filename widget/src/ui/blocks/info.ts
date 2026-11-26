/** Sources (pages an answer came from, design 11), notices and summary cards (W-08). */
import { el } from '../../dom';
import { icon } from '../../icons';
import type { ActionHandler, Block } from '../../types';
import { actionControl, groupLabel, safeUrl } from './shared';

type SourcesBlock = Extract<Block, { type: 'sources' }>;
type NoticeBlock = Extract<Block, { type: 'notice' }>;
type SummaryBlock = Extract<Block, { type: 'summary' }>;

/** "example.com/help" — host and path, without scheme, query or trailing slash. */
function shortUrl(raw: string): string {
  const href = safeUrl(raw);
  if (!href) return '';
  const url = new URL(href);
  return `${url.host}${url.pathname}`.replace(/\/$/, '');
}

/** 'Sources': each page an answer came from, as a link with its title and short URL. */
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

/** A confirmation card (W-08 "Sent to the team"): a check, a title, label/value rows, a footer. */
export function renderSummary(block: SummaryBlock): HTMLElement {
  return el('section', { class: 'ec-panel-card ec-summary' }, [
    el('h3', { class: 'ec-summary-title' }, [icon('check', 16), el('span', {}, [block.title])]),
    el('dl', { class: 'ec-summary-rows' }, block.rows.flatMap((row) => [
      el('dt', {}, [row.label]),
      el('dd', {}, [row.value]),
    ])),
    ...(block.footer ? [el('p', { class: 'ec-summary-footer' }, [block.footer])] : []),
  ]);
}

/** A short coloured note with an info icon (tone: info, success, warning, danger). */
export function renderNotice(block: NoticeBlock): HTMLElement {
  return el('p', { class: `ec-notice ec-tone--${block.tone ?? 'info'}`, role: 'note' }, [icon('info', 18), el('span', {}, [block.text])]);
}
