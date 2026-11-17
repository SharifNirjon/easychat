/**
 * Radio (single) or checkbox (multi) list (designs 2, 7). Native inputs inside a fieldset, so
 * keyboard and screen readers work as usual. Submitting sends the chosen labels as a message.
 */
import { el } from '../../dom';
import type { ActionHandler, Block, Choice } from '../../types';
import { image, uid } from './shared';

type ChoiceListBlock = Extract<Block, { type: 'choice_list' }>;

function option(choice: Choice, group: string, type: 'radio' | 'checkbox'): HTMLLabelElement {
  const input = el('input', { type, name: group, value: choice.id, class: 'ec-choice-input' });
  const thumb = image(choice.image, 'ec-choice-img');
  return el('label', { class: 'ec-choice' }, [
    input,
    ...(thumb ? [thumb] : []),
    el('span', { class: 'ec-choice-text' }, [
      el('span', { class: 'ec-choice-label' }, [choice.label]),
      ...(choice.detail ? [el('span', { class: 'ec-choice-detail' }, [choice.detail])] : []),
    ]),
  ]);
}

export function renderChoiceList(block: ChoiceListBlock, onAction: ActionHandler): HTMLElement {
  const group = uid('choice');
  const type = block.mode === 'multi' ? 'checkbox' : 'radio';
  const options = block.options.map((choice) => option(choice, group, type));
  const submit = el('button', { type: 'submit', class: 'ec-btn ec-btn--primary ec-btn--block', disabled: true }, [
    block.submit_label ?? 'Next',
  ]);

  const chosen = () =>
    options.filter((label) => (label.querySelector('input') as HTMLInputElement).checked).map(
      (label) => label.querySelector('.ec-choice-label')?.textContent ?? '',
    );

  // The step header sits outside the fieldset: browsers always draw a <legend> first.
  const form = el('form', { class: 'ec-panel-card ec-choices' }, [
    ...(block.step ? [stepHeader(block.step.current, block.step.total)] : []),
    el('fieldset', {}, [
      el('legend', { class: 'ec-question' }, [block.question]),
      el('div', { class: 'ec-choice-list' }, options),
    ]),
    submit,
  ]);

  form.addEventListener('change', () => {
    options.forEach((label) => label.classList.toggle('is-selected', (label.querySelector('input') as HTMLInputElement).checked));
    submit.disabled = chosen().length === 0;
  });
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    const labels = chosen();
    if (labels.length) onAction({ kind: 'send', text: labels.join(', ') });
  });
  return form;
}

function stepHeader(current: number, total: number): HTMLElement {
  const fill = el('span');
  // CSSOM, not a style="" attribute: works under a strict style-src CSP.
  fill.style.width = `${Math.round((Math.min(current, total) / total) * 100)}%`;
  return el('div', { class: 'ec-step' }, [
    el('p', { class: 'ec-step-label' }, [`Step ${current} of ${total}`]),
    el('div', { class: 'ec-progress', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': String(total), 'aria-valuenow': String(current) }, [fill]),
  ]);
}
