/**
 * Small forms (designs 6, 8, 10): text/email/textarea/one-time code, checkbox, star rating, tag chips.
 * A form the server can handle (`submit`, e.g. order tracking) sends its text values to /chat;
 * the others are shown with a disabled button and a note until roadmap step 9 wires them up.
 * Typed values live only in the DOM and the one request: never in sessionStorage.
 */
import { el } from '../../dom';
import { icon } from '../../icons';
import type { ActionHandler, Block, FormField } from '../../types';
import { groupLabel, uid } from './shared';

type FormBlock = Extract<Block, { type: 'form' }>;
type TextFormField = Extract<FormField, { kind: 'text' | 'email' | 'textarea' | 'code' }>;

const RATING_MAX = 5;
const RATING_WORDS = ['Poor', 'Fair', 'Okay', 'Really good', 'Loved it'];
const NOT_YET = "Sending this form isn't available yet.";
const FILL_IN = 'Please fill in every field.';
const CODE_DIGITS = 6;

/** A labelled text, email, multi-line or one-time-code field, optionally pre-filled. */
function textField(field: TextFormField): HTMLElement {
  const id = uid('field');
  const attrs = {
    id, name: field.name, class: 'ec-field', placeholder: field.placeholder ?? '', required: field.required ?? false,
    value: field.value ?? '',
  };
  let input: HTMLElement;
  if (field.kind === 'textarea') {
    input = el('textarea', { ...attrs, rows: '3' });
    (input as HTMLTextAreaElement).value = field.value ?? '';
  } else if (field.kind === 'code') {
    // Numeric keypad on phones, and the OS can offer the code from the email/SMS.
    input = el('input', {
      ...attrs, type: 'text', inputmode: 'numeric', autocomplete: 'one-time-code', pattern: `[0-9]{${CODE_DIGITS}}`,
      maxlength: String(CODE_DIGITS), class: 'ec-field ec-field--code',
    });
  } else {
    input = el('input', { ...attrs, type: field.kind, autocomplete: field.kind === 'email' ? 'email' : 'off' });
  }
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

/** The text values of a form's text-like fields, trimmed; null if a required one is empty or invalid. */
function textValues(form: HTMLFormElement, fields: FormField[]): Record<string, string> | null {
  const values: Record<string, string> = {};
  for (const field of fields) {
    if (!['text', 'email', 'textarea', 'code'].includes(field.kind)) continue;
    const input = form.elements.namedItem(field.name) as HTMLInputElement | HTMLTextAreaElement | null;
    const value = input?.value.trim() ?? '';
    if (!input || (('required' in field && field.required) && !value) || (value && !input.checkValidity())) return null;
    values[field.name] = value;
  }
  return values;
}

/**
 * A small form. With `submit` it sends its values (once) through `onAction`; without, the button
 * stays disabled with a note until roadmap step 9.
 */
export function renderForm(block: FormBlock, onAction: ActionHandler): HTMLElement {
  const noteId = uid('note');
  const submittable = Boolean(block.submit);
  const note = el('p', { class: 'ec-form-note', id: noteId, 'aria-live': 'polite' }, submittable ? [] : [NOT_YET]);
  const button = el('button', {
    type: 'submit', class: 'ec-btn ec-btn--primary ec-btn--block', disabled: !submittable, 'aria-describedby': noteId,
  }, [block.submit_label]);
  const form = el('form', { class: 'ec-panel-card ec-form', novalidate: true }, [
    ...(block.title ? [el('h3', { class: 'ec-form-title' }, [block.title])] : []),
    ...block.fields.map(renderField),
    button,
    note,
  ]);
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    if (!block.submit || button.disabled) return;
    const values = textValues(form, block.fields);
    if (!values) {
      note.textContent = FILL_IN;
      return;
    }
    // One submission per form: the next reply brings a fresh form if one is needed.
    form.querySelectorAll<HTMLInputElement | HTMLButtonElement | HTMLTextAreaElement>('input, button, textarea')
      .forEach((control) => { control.disabled = true; });
    onAction({ kind: 'form', form: block.submit, values, label: block.submit_label });
  });
  // Tag chips show their checked state (no :has() needed).
  form.addEventListener('change', (event) => {
    const input = event.target as HTMLInputElement;
    if (input.closest('.ec-chip--toggle')) input.parentElement?.classList.toggle('is-selected', input.checked);
  });
  return form;
}
