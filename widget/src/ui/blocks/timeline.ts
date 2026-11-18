/** Progress timeline, e.g. order tracking (design 6). */
import { el } from '../../dom';
import { icon } from '../../icons';
import type { ActionHandler, Block, TimelineStep } from '../../types';
import { actionControl } from './shared';

type TimelineBlock = Extract<Block, { type: 'timeline' }>;

const STATE_LABEL: Record<TimelineStep['state'], string> = {
  done: 'completed',
  current: 'in progress',
  upcoming: 'not yet',
};

/** One timeline step: marker (a tick when done), label, and its state for screen readers. */
function step(item: TimelineStep): HTMLElement {
  return el('li', { class: `ec-tl-step is-${item.state}` }, [
    el('span', { class: 'ec-tl-marker', 'aria-hidden': 'true' }, item.state === 'done' ? [icon('check', 12)] : []),
    el('div', {}, [
      el('p', { class: 'ec-tl-label' }, [item.label, el('span', { class: 'ec-sr' }, [`, ${STATE_LABEL[item.state]}`])]),
      ...(item.detail ? [el('p', { class: 'ec-tl-detail' }, [item.detail])] : []),
    ]),
  ]);
}

/** A progress timeline (order tracking): eyebrow, status pill, title, steps, footer link. */
export function renderTimeline(block: TimelineBlock, onAction: ActionHandler): HTMLElement {
  const head: HTMLElement[] = [];
  if (block.eyebrow || block.status) {
    head.push(el('div', { class: 'ec-tl-head' }, [
      el('span', { class: 'ec-eyebrow' }, [block.eyebrow ?? '']),
      ...(block.status ? [el('span', { class: `ec-pill ec-tone--${block.status.tone ?? 'info'}` }, [block.status.label])] : []),
    ]));
  }

  const footer: (Node | string)[] = [];
  if (block.footer) footer.push(el('span', { class: 'ec-tl-footer-text' }, [icon('truck', 16), block.footer]));
  if (block.link) footer.push(actionControl(block.link, 'ec-link', ['Track', icon('external', 14)], onAction));

  return el('section', { class: 'ec-panel-card ec-timeline' }, [
    ...head,
    el('h3', { class: 'ec-tl-title' }, [block.title]),
    el('ol', { class: 'ec-tl-steps' }, block.steps.map(step)),
    ...(footer.length ? [el('div', { class: 'ec-tl-footer' }, footer)] : []),
  ]);
}
