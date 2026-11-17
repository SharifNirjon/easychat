/** Product/service cards: a single card or a swipeable carousel (designs 1, 8, 13). */
import { el } from '../../dom';
import { icon } from '../../icons';
import type { ActionHandler, Block, Card } from '../../types';
import { actionControl, image, ratingLine } from './shared';

type CardBlock = Extract<Block, { type: 'card' }>;
type CarouselBlock = Extract<Block, { type: 'carousel' }>;

function renderCard(card: Card, onAction: ActionHandler): HTMLElement {
  const media = image(card.image, 'ec-card-img');
  const badge = card.badge ? el('span', { class: 'ec-card-badge' }, [card.badge]) : null;
  // With an image the badge floats over it; without one it sits inline above the title.
  const mediaBox = media ? el('div', { class: 'ec-card-media' }, [media, ...(badge ? [badge] : [])]) : null;
  const inlineBadge = !media && badge ? [badge] : [];

  const footer: (Node | string)[] = [];
  if (card.price) footer.push(el('span', { class: 'ec-card-price' }, [card.price]));
  if (card.action) {
    const label = card.action.kind === 'send' ? card.action.text : `Open ${card.title}`;
    footer.push(actionControl(card.action, 'ec-card-action', [icon(card.action.kind === 'send' ? 'plus' : 'external', 16)], onAction, label));
  }

  return el('article', { class: 'ec-card' }, [
    ...(mediaBox ? [mediaBox] : []),
    el('div', { class: 'ec-card-body' }, [
      ...inlineBadge,
      el('h3', { class: 'ec-card-title' }, [card.title]),
      ...(card.subtitle ? [el('p', { class: 'ec-card-sub' }, [card.subtitle])] : []),
      ...[ratingLine(card.rating, card.reviews)].filter((n): n is HTMLElement => n !== null),
      ...(footer.length ? [el('div', { class: 'ec-card-footer' }, footer)] : []),
    ]),
  ]);
}

export function renderCardBlock(block: CardBlock, onAction: ActionHandler): HTMLElement {
  return el('div', { class: 'ec-card-single' }, [renderCard(block.card, onAction)]);
}

/** Cards in a scroll-snap row, with dots and an "n of N" hint that follow the scroll position. */
export function renderCarousel(block: CarouselBlock, onAction: ActionHandler): HTMLElement {
  const track = el(
    'ul',
    { class: 'ec-carousel', 'aria-label': `${block.items.length} items` },
    block.items.map((card) => el('li', {}, [renderCard(card, onAction)])),
  );
  const dots = block.items.map(() => el('i'));
  const counter = el('span', { class: 'ec-carousel-count' });

  const update = () => {
    const slide = track.firstElementChild as HTMLElement | null;
    const step = slide ? slide.offsetWidth + 10 : 1; // 10px = gap in styles.css
    const index = Math.min(block.items.length - 1, Math.round(track.scrollLeft / step));
    dots.forEach((dot, i) => dot.classList.toggle('is-active', i === index));
    counter.textContent = `${index + 1} of ${block.items.length} · swipe`;
  };
  track.addEventListener('scroll', update, { passive: true });
  update();

  return el('div', { class: 'ec-carousel-wrap' }, [
    track,
    el('div', { class: 'ec-carousel-meta', 'aria-hidden': 'true' }, [el('span', { class: 'ec-dots-nav' }, dots), counter]),
  ]);
}
