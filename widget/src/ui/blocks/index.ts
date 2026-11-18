/**
 * Block renderer registry: one function per block type (api/blocks.py is the catalogue).
 * An unknown or malformed block renders nothing rather than breaking the conversation.
 */
import type { ActionHandler, Block } from '../../types';
import { renderButtons, renderChips } from './actions';
import { renderCardBlock, renderCarousel } from './cards';
import { renderChoiceList } from './choices';
import { renderForm } from './form';
import { renderNotice, renderSources } from './info';
import { renderTimeline } from './timeline';

/** Pick the renderer for the block's type; unknown types render nothing. */
function renderOne(block: Block, onAction: ActionHandler): HTMLElement | null {
  switch (block.type) {
    case 'chips':
      return renderChips(block, onAction);
    case 'buttons':
      return renderButtons(block, onAction);
    case 'card':
      return renderCardBlock(block, onAction);
    case 'carousel':
      return renderCarousel(block, onAction);
    case 'choice_list':
      return renderChoiceList(block, onAction);
    case 'timeline':
      return renderTimeline(block, onAction);
    case 'form':
      return renderForm(block);
    case 'sources':
      return renderSources(block, onAction);
    case 'notice':
      return renderNotice(block);
    default:
      return null;
  }
}

/** Render one block, or null if it is unknown or malformed (never throws). */
export function renderBlock(block: Block, onAction: ActionHandler): HTMLElement | null {
  try {
    return renderOne(block, onAction);
  } catch {
    return null; // e.g. a tampered sessionStorage copy missing a field
  }
}
