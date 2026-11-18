/** Chips (quick replies) and button rows. */
import { el } from '../../dom';
import type { ActionHandler, Block } from '../../types';
import { actionControl } from './shared';

type ChipsBlock = Extract<Block, { type: 'chips' }>;
type ButtonsBlock = Extract<Block, { type: 'buttons' }>;

/** A row of quick-reply chips; each one sends a message or opens a link. */
export function renderChips(block: ChipsBlock, onAction: ActionHandler): HTMLElement {
  return el(
    'div',
    { class: 'ec-chips' },
    block.items.map((chip) => actionControl(chip.action, 'ec-chip', [chip.label], onAction)),
  );
}

/** One to three buttons; `primary` is filled, `secondary` outlined. */
export function renderButtons(block: ButtonsBlock, onAction: ActionHandler): HTMLElement {
  return el(
    'div',
    { class: 'ec-buttons' },
    block.items.map((button) =>
      actionControl(button.action, `ec-btn ec-btn--${button.style === 'primary' ? 'primary' : 'secondary'}`, [button.label], onAction),
    ),
  );
}
