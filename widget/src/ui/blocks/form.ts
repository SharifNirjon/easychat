/**
 * Small forms (designs 6, 7, 8, 10, W-07): text/email/textarea/one-time code, checkbox (incl. the
 * required consent box), star rating, tag chips, and single/multi choice (items with thumbnails,
 * reason chips). A form the server can handle (`submit`) sends its values to /chat; others are
 * shown with a disabled button. Typed values live only in the DOM and the one request: never in
 * sessionStorage. The server re-checks everything (consent included): these checks are a courtesy.
 */
import { el } from '../../dom';
import { icon } from '../../icons';
import type { ActionHandler, Block, Choice, FormField, FormValues } from '../../types';
import { groupLabel, image, safeUrl, uid } from './shared';

type FormBlock = Extract<Block, { type: 'form' }>;
type TextFormField = Extract<FormField, { kind: 'text' | 'email' | 'textarea' | 'code' }>;
type ChoiceFormField = Extract<FormField, { kind: 'choice' }>;

const RATING_MAX = 5;
const RATING_WORDS = ['Poor', 'Fair', 'Okay', 'Really good', 'Loved it'];
const NOT_YET = "Sending this form isn't available yet.";
const FILL_IN = 'Please fill in every field.';
const CODE_DIGITS = 6;
const CHECKED = 'yes'; // what a ticked checkbox sends (api/team_requests.py CONSENT_VALUE)
const TEXT_KINDS = ['text', 'email', 'textarea', 'code'];

/** A labelled text, email, multi-line or one-time-code field, optionally pre-filled. */
function textField(field: TextFormField): HTMLElement {
  const id = uid('field');
  const attrs = {
    id, name: field.name, class: 'ec-field', placeholder: field.placeholder ?? '', required: field.required ?? false,
    value: field.value ?? '',
  };
  let input: HTMLElement;
  if (field.kind === 'textarea') {
    input = el('textarea', { ...attrs, rows: '3', maxlength: '1000' });
    (input as HTMLTextAreaElement).value = field.value ?? '';
  } else if (field.kind === 'code') {
    // Numeric keypad on phones, and the OS can offer the code from the email/SMS.
    input = el('input', {
      ...attrs, type: 'text', inputmode: 'numeric', autocomplete: 'one-time-code', pattern: `[0-9]{${CODE_DIGITS}}`,
      maxlength: String(CODE_DIGITS), class: 'ec-field ec-field--code',
    });
  } else {
    const autocomplete = field.kind === 'email' ? 'email' : field.name === 'name' ? 'name' : 'off';
    input = el('input', { ...attrs, type: field.kind, autocomplete, maxlength: '254' });
  }
  return el('div', { class: 'ec-form-row' }, [el('label', { class: 'ec-label', for: id }, [field.label]), input]);
}

/** A single labelled checkbox (a consent box when required). */
function checkboxField(field: Extract<FormField, { kind: 'checkbox' }>): HTMLElement {
  return el('label', { class: `ec-check${field.required ? ' ec-check--consent' : ''}` }, [
    el('input', { type: 'checkbox', name: field.name, value: CHECKED, required: field.required ?? false }),
    el('span', {}, [field.label]),
  ]);
}

/** Five radio inputs styled as stars; the word next to them follows the choice. */
function ratingField(field: Extract<FormField, { kind: 'rating' }>): HTMLElement {
  const word = el('span', { class: 'ec-rating-word', 'aria-hidden': 'true' });
  const stars = Array.from({ length: RATING_MAX }, (_, i) =>
    el('label', { class: 'ec-star-input' }, [
      el('input', { type: 'radio', name: field.name, value: String(i + 1), 'aria-label': `${i + 1} of ${RATING_MAX} stars` }),
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

/** A chip-styled checkbox or radio. */
function chip(name: string, value: string, label: string, type: 'checkbox' | 'radio'): HTMLElement {
  return el('label', { class: 'ec-chip ec-chip--toggle' }, [
    el('input', { type, name, value }),
    icon('check', 14),
    label,
  ]);
}

/** Multi-select tag chips (checkboxes styled as chips) under a group label. */
function tagsField(field: Extract<FormField, { kind: 'tags' }>): HTMLElement {
  const labelId = uid('tags');
  return el('div', { class: 'ec-form-row', role: 'group', 'aria-labelledby': labelId }, [
    groupLabel(field.label, labelId),
    el('div', { class: 'ec-chips', 'data-group': '' }, field.options.map((option) => chip(field.name, option, option, 'checkbox'))),
  ]);
}

/** One list option (design 7): a native radio/checkbox, optional thumbnail, label and detail. */
function listOption(choice: Choice, name: string, type: 'checkbox' | 'radio'): HTMLElement {
  const thumb = image(choice.image, 'ec-choice-img');
  return el('label', { class: 'ec-choice' }, [
    el('input', { type, name, value: choice.id, class: 'ec-choice-input' }),
    ...(thumb ? [thumb] : []),
    el('span', { class: 'ec-choice-text' }, [
      el('span', { class: 'ec-choice-label' }, [choice.label]),
      ...(choice.detail ? [el('span', { class: 'ec-choice-detail' }, [choice.detail])] : []),
    ]),
  ]);
}

/** Pick one or several options: a list (items, refund options) or chips (reasons). */
function choiceField(field: ChoiceFormField): HTMLElement {
  const labelId = uid('choice');
  const type = field.mode === 'multi' ? 'checkbox' : 'radio';
  const options = field.style === 'chips'
    ? el('div', { class: 'ec-chips', 'data-group': '' }, field.options.map((o) => chip(field.name, o.id, o.label, type)))
    : el('div', { class: 'ec-choice-list', 'data-group': '' }, field.options.map((o) => listOption(o, field.name, type)));
  return el('div', { class: 'ec-form-row', role: type === 'radio' ? 'radiogroup' : 'group', 'aria-labelledby': labelId }, [
    groupLabel(field.label, labelId),
    options,
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
    case 'choice':
      return choiceField(field);
    default:
      return textField(field);
  }
}

/** The checked inputs named `name` in the form. */
function checked(form: HTMLFormElement, name: string): HTMLInputElement[] {
  return Array.from(form.querySelectorAll<HTMLInputElement>('input')).filter((i) => i.name === name && i.checked);
}

/** Every field's value; null if a required one is empty or a typed value is invalid. */
function formValues(form: HTMLFormElement, fields: FormField[]): FormValues | null {
  const values: FormValues = {};
  for (const field of fields) {
    if (TEXT_KINDS.includes(field.kind)) {
      const input = form.elements.namedItem(field.name) as HTMLInputElement | HTMLTextAreaElement | null;
      const value = input?.value.trim() ?? '';
      if (!input || ('required' in field && field.required && !value) || (value && !input.checkValidity())) return null;
      values[field.name] = value;
      continue;
    }
    const ticked = checked(form, field.name).map((input) => input.value);
    const required = 'required' in field && field.required;
    if (required && ticked.length === 0) return null;
    if (ticked.length === 0) continue;
    const multi = field.kind === 'tags' || (field.kind === 'choice' && field.mode === 'multi');
    values[field.name] = multi ? ticked : ticked[0];
  }
  return values;
}

/** "Privacy policy" under a consent box, if the URL is safe. */
function privacyLink(url: string | undefined): HTMLElement[] {
  const href = url ? safeUrl(url) : null;
  return href ? [el('a', { class: 'ec-form-link', href, target: '_blank', rel: 'noopener noreferrer' }, ['Privacy policy'])] : [];
}

/**
 * A small form. With `submit` it sends its values (once) through `onAction`; without, the button
 * stays disabled with a note.
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
    ...privacyLink(block.privacy_url),
    button,
    note,
  ]);
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    if (!block.submit || button.disabled) return;
    const values = formValues(form, block.fields);
    if (!values) {
      note.textContent = FILL_IN;
      return;
    }
    // One submission per form: the next reply brings a fresh form if one is needed.
    form.querySelectorAll<HTMLInputElement | HTMLButtonElement | HTMLTextAreaElement>('input, button, textarea')
      .forEach((control) => { control.disabled = true; });
    onAction({ kind: 'form', form: block.submit, values, label: block.submit_label });
  });
  // Chips and list options show their checked state (radios: the whole group updates; no :has()).
  form.addEventListener('change', (event) => {
    const group = (event.target as HTMLElement).closest('[data-group]');
    group?.querySelectorAll('label').forEach((label) => {
      label.classList.toggle('is-selected', Boolean(label.querySelector<HTMLInputElement>('input')?.checked));
    });
  });
  return form;
}
