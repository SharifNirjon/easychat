/** Small pieces shared by the settings steps (Orders, Requests). */
import { useState } from 'preact/hooks';

import { ApiError } from '../api';

/** Run an API call with a busy flag and a visible error. */
export function useAction() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = async (action: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await action();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Something went wrong.');
    } finally {
      setBusy(false);
    }
  };
  return { busy, error, run };
}

/** A labelled text input (required unless `required={false}`). */
export function Field(props: { label: string; value: string; onInput: (v: string) => void; placeholder?: string;
  type?: string; autoComplete?: string; required?: boolean }) {
  return (
    <label class="field">
      <span>{props.label}</span>
      <input type={props.type ?? 'text'} required={props.required ?? true} value={props.value}
        placeholder={props.placeholder} autoComplete={props.autoComplete ?? 'off'} spellcheck={false}
        onInput={(e) => props.onInput((e.target as HTMLInputElement).value)} />
    </label>
  );
}
