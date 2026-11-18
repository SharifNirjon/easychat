/**
 * Small forms (designs 8, 10): text/email/textarea, checkbox, star rating, tag chips.
 * Step 4 renders them; submitting is wired up in roadmap step 9, so the submit button is
 * disabled with a short note rather than pretending to work.
 */
import { el } from '../../dom';
import { icon } from '../../icons';
import type { Block, FormField } from '../../types';
import { groupLabel, uid } from './shared';

type FormBlock = Extract<Block, { type: 'form' }>;

const RATING_MAX = 5;
const RATING_WORDS = ['Poor', 'Fair', 'Okay', 'Really good', 'Loved it'];
const NOT_YET = "Sending this form isn't available yet.";

/** A labelled text, email or multi-line field. */
function textField(field: Extract<FormField, { kind: 'text' | 'email' | 'textarea' }>): HTMLElement {
  const id = uid('field');
  const attrs = { id, name: field.name, class: 'ec-field', placeholder: field.placeholder ?? '', required: field.required ?? false };
  const input = field.kind === 'textarea' ? el('textarea', { ...attrs, rows: '3' }) : el('input', { ...attrs, type: field.kind });
  return el('div', { class: 'ec-form-row' }, [el('label', { class: 'ec-label', for: id }, [field.label]), input]);
}

/** A single labelled checkbox. */
function checkboxField(field: Extract<FormField, { kind: 'checkbox' }>): HTMLElement {
  return el('label', { class: 'ec-check' }, [el('input', { type: 'checkbox', name: field.name }), field.label]);
}

/** Five radio inputs styled as stars; the word next to them follows the choice. */
function ratingField(field: Extract<FormField, { kind: 'rating' }>): HTMLElement {
  const group = uid('rating');
  const word = el('span', { class: 'ec-rating-word', 'aria-hidden': 'true' });
  const stars = Array.from({ length: RATING_MAX }, (_, i) =>
    el('label', { class: 'ec-star-input' }, [
      el('input', { type: 'radio', name: group, value: String(i + 1), 'aria-label': `${i + 1} of ${RATING_MAX} stars` }),
      icon('star', 26, { filled: true }),
    ]),
  );
  const row = el('div', { class: 'ec-stars' }, stars);
  row.addEventListener('change', (event) => {
    const value = Number((event.target as HTMLInputElement).value);
    stars.forEach((star, i) => star.classList.toggle('is-on', i < value));
    word.textContent = RATING_WORDS[value - 1] ?? '';
  });
  return el('fieldset', { class: 'ec-form-row' }, [
    el('legend', { class: 'ec-sr' }, [field.label]),
    el('div', { class: 'ec-stars-row' }, [row, word]),
  ]);
}

/** Multi-select tag chips (checkboxes styled as chips) under a group label. */
function tagsField(field: Extract<FormField, { kind: 'tags' }>): HTMLElement {
  const labelId = uid('tags');
  return el('div', { class: 'ec-form-row', role: 'group', 'aria-labelledby': labelId }, [
    groupLabel(field.label, labelId),
    el('div', { class: 'ec-chips' }, field.options.map((option) =>
      el('label', { class: 'ec-chip ec-chip--toggle' }, [
        el('input', { type: 'checkbox', name: field.name, value: option }),
        icon('check', 14),
        option,
      ]),
    )),
  ]);
}

/** Render one form field by its kind. */
function renderField(field: FormField): HTMLElement {
  switch (field.kind) {
    case 'checkbox':
      return checkboxField(field);
    case 'rating':
      return ratingField(field);
    case 'tags':
      return tagsField(field);
    default:
      return textField(field);
  }
}

/** A small form. Submitting arrives in roadmap step 9, so the button is disabled with a note. */
export function renderForm(block: FormBlock): HTMLElement {
  const noteId = uid('note');
  const form = el('form', { class: 'ec-panel-card ec-form', novalidate: true }, [
    ...(block.title ? [el('h3', { class: 'ec-form-title' }, [block.title])] : []),
    ...block.fields.map(renderField),
    el('button', { type: 'submit', class: 'ec-btn ec-btn--primary ec-btn--block', disabled: true, 'aria-describedby': noteId }, [
      block.submit_label,
    ]),
    el('p', { class: 'ec-form-note', id: noteId }, [NOT_YET]),
  ]);
  form.addEventListener('submit', (event) => event.preventDefault());
  // Tag chips show their checked state (no :has() needed).
  form.addEventListener('change', (event) => {
    const input = event.target as HTMLInputElement;
    if (input.closest('.ec-chip--toggle')) input.parentElement?.classList.toggle('is-selected', input.checked);
  });
  return form;
}
