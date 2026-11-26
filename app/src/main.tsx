/**
 * easychat onboarding app (roadmap step 7): email -> website -> assistant -> learning ->
 * try it -> install. Renders everything from GET /api/onboarding and polls it while something
 * is happening (a crawl, or waiting for the widget to appear on the owner's site).
 */
import { render } from 'preact';
import { useCallback, useEffect, useState } from 'preact/hooks';

import { api, ApiError, crawlActive, type OnboardingState } from './api';
import { AssistantStep } from './steps/Assistant';
import { InstallStep } from './steps/Install';
import { CrawlMeter, LearningStep } from './steps/Learning';
import { SignIn, Verify } from './steps/SignIn';
import { TryStep } from './steps/TryIt';
import { WebsiteStep } from './steps/Website';
import './styles.css';

const POLL_MS = 2500;
const SIGNIN_HASH = '#signin=';

type StepId = 'website' | 'assistant' | 'learning' | 'try' | 'install';
const STEPS: { id: StepId; label: string }[] = [
  { id: 'website', label: 'Your website' },
  { id: 'assistant', label: 'Your assistant' },
  { id: 'learning', label: 'Learning your site' },
  { id: 'try', label: 'Try it' },
  { id: 'install', label: 'Install' },
];

/** Where to land after loading: the first thing still to do. */
function firstStep(state: OnboardingState): StepId {
  if (!state.site_url) return 'website';
  if (state.installed_at) return 'install';
  return 'assistant';
}

type View =
  | { kind: 'loading' }
  | { kind: 'signed-out' }
  | { kind: 'verify'; token: string }
  | { kind: 'ready'; state: OnboardingState };

function App() {
  const [view, setView] = useState<View>({ kind: 'loading' });
  const [step, setStep] = useState<StepId>('website');

  /** Load (or reload) the state; a 401 means signed out. */
  const load = useCallback(async (pickStep = false) => {
    try {
      const state = await api.state();
      setView({ kind: 'ready', state });
      if (pickStep) setStep(firstStep(state));
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) setView({ kind: 'signed-out' });
      else setView((current) => (current.kind === 'loading' ? { kind: 'signed-out' } : current));
    }
  }, []);

  useEffect(() => {
    // A sign-in link: take the token out of the address bar at once (it's single-use anyway).
    if (location.hash.startsWith(SIGNIN_HASH)) {
      const token = location.hash.slice(SIGNIN_HASH.length);
      history.replaceState(null, '', location.pathname);
      setView({ kind: 'verify', token });
      return;
    }
    void load(true);
  }, [load]);

  const state = view.kind === 'ready' ? view.state : null;
  const waitingForInstall = state !== null && step === 'install' && !state.installed_at && state.site_key !== null;
  const polling = state !== null && (crawlActive(state.crawl) || waitingForInstall);
  useEffect(() => {
    if (!polling) return;
    const timer = setInterval(() => void load(), POLL_MS);
    return () => clearInterval(timer);
  }, [polling, load]);

  if (view.kind === 'loading') return <div class="center muted">Loading…</div>;
  if (view.kind === 'signed-out') return <SignIn />;
  if (view.kind === 'verify') return <Verify token={view.token} onSignedIn={() => void load(true)} />;

  const current = view.state;
  const update = (next: OnboardingState) => setView({ kind: 'ready', state: next });
  const logout = async () => {
    await api.logout().catch(() => undefined);
    setView({ kind: 'signed-out' });
  };

  return (
    <div class="shell">
      <aside class="sidebar">
        <div class="brand"><span class="logo">e</span> easychat</div>
        <nav aria-label="Setup steps">
          <ol class="steps">
            {STEPS.map((s, index) => {
              const locked = s.id !== 'website' && !current.site_url;
              return (
                <li key={s.id}>
                  <button type="button" class={`step${step === s.id ? ' is-current' : ''}`} disabled={locked}
                    aria-current={step === s.id ? 'step' : undefined} onClick={() => setStep(s.id)}>
                    <span class="step-num">{s.id === 'install' && current.installed_at ? '✓' : index + 1}</span>
                    {s.label}
                  </button>
                </li>
              );
            })}
          </ol>
        </nav>
        {current.crawl && <CrawlMeter crawl={current.crawl} compact />}
        <div class="account">
          <span class="muted small">{current.email}</span>
          <button type="button" class="link" onClick={() => void logout()}>Sign out</button>
        </div>
      </aside>
      <main class="content">
        {step === 'website' && <WebsiteStep state={current} onUpdate={update} onNext={() => setStep('assistant')} />}
        {step === 'assistant' && <AssistantStep state={current} onUpdate={update} onNext={() => setStep('learning')} />}
        {step === 'learning' && <LearningStep state={current} onUpdate={update} onNext={() => setStep('try')} />}
        {step === 'try' && <TryStep state={current} onNext={() => setStep('install')} />}
        {step === 'install' && <InstallStep state={current} />}
      </main>
    </div>
  );
}

render(<App />, document.getElementById('root')!);
