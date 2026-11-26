/**
 * Sign-in: ask for an email, then (from the emailed link) a "Continue" button. The link doesn't
 * sign in by itself because email scanners open links automatically and would use up the token.
 */
import { useState } from 'preact/hooks';

import { api, ApiError } from '../api';

/** Email form, then "check your inbox". */
export function SignIn() {
  const [email, setEmail] = useState('');
  const [sentTo, setSentTo] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (event: Event) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.requestLink(email.trim());
      setSentTo(email.trim());
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Something went wrong.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div class="center">
      <div class="card narrow">
        <div class="brand"><span class="logo">e</span> easychat</div>
        {sentTo ? (
          <>
            <h1>Check your inbox</h1>
            <p>We sent a sign-in link to <strong>{sentTo}</strong>. It works once and expires in 15 minutes.</p>
            <button type="button" class="link" onClick={() => setSentTo(null)}>Use a different email</button>
          </>
        ) : (
          <form onSubmit={submit}>
            <h1>Your website assistant in 5 minutes</h1>
            <p class="muted">Enter your email. We'll send you a link: no password needed.</p>
            <label class="field">
              <span>Email</span>
              <input type="email" required autocomplete="email" value={email} maxLength={254}
                onInput={(e) => setEmail((e.target as HTMLInputElement).value)} />
            </label>
            {error && <p class="error" role="alert">{error}</p>}
            <button type="submit" class="primary" disabled={busy}>{busy ? 'Sending…' : 'Email me a sign-in link'}</button>
          </form>
        )}
      </div>
    </div>
  );
}

/** Landing page of the emailed link: one click to sign in. */
export function Verify({ token, onSignedIn }: { token: string; onSignedIn: () => void }) {
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const signIn = async () => {
    setBusy(true);
    try {
      await api.verify(token);
      onSignedIn();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Something went wrong.');
      setBusy(false);
    }
  };

  return (
    <div class="center">
      <div class="card narrow">
        <div class="brand"><span class="logo">e</span> easychat</div>
        <h1>Welcome!</h1>
        {error ? (
          <>
            <p class="error" role="alert">{error}</p>
            <a class="button primary" href="/app/">Get a new link</a>
          </>
        ) : (
          <button type="button" class="primary" disabled={busy} onClick={() => void signIn()}>
            {busy ? 'Signing in…' : 'Continue to easychat'}
          </button>
        )}
      </div>
    </div>
  );
}
