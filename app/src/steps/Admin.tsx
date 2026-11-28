/**
 * Admin v0 (roadmap step 10): Inbox (A-01), Leads (A-04), Knowledge (A-05), Team.
 * Everything visitors or the model wrote is rendered as plain text (Preact escapes it): never
 * as HTML, and links in it are not made clickable.
 */
import { useEffect, useState } from 'preact/hooks';

import {
  api, type AskResult, type ConversationDetail, type ConversationRow, type KbEntry, type KbState,
  type OnboardingState, type Page, type RequestItem, type TeamState,
} from '../api';
import { Field, useAction } from './shared';

const KIND_LABELS: Record<string, string> = { lead: 'Lead', handoff: 'Wants a person', return: 'Return' };
const when = (iso: string | null) => (iso ? new Date(iso).toLocaleString() : '');
const text = (value: unknown) => (value === null || value === undefined ? '' : String(value));

/** Previous / next buttons for a paged list. */
function Pager({ page, total, size, onPage }: { page: number; total: number; size: number; onPage: (p: number) => void }) {
  const pages = Math.max(1, Math.ceil(total / size));
  return (
    <div class="row small muted">
      <button type="button" class="link" disabled={page <= 1} onClick={() => onPage(page - 1)}>← Newer</button>
      <span>Page {page} of {pages} · {total} total</span>
      <button type="button" class="link" disabled={page >= pages} onClick={() => onPage(page + 1)}>Older →</button>
    </div>
  );
}

/** One request's details with its consent (inbox side panel and leads table). */
function RequestCard({ request }: { request: RequestItem }) {
  const d = request.data;
  const rows: [string, string][] = request.kind === 'return'
    ? [['Order', `#${text(d.order_number)}`], ['Customer', text(d.customer_email)],
      ['Items', ((d.items as { title: string; quantity: number }[]) ?? []).map((i) => `${i.title} ×${i.quantity}`).join(', ')],
      ['Reason', text(d.reason)], ['Refund', text(d.refund_preference)], ['Notes', text(d.notes)]]
    : [['Name', text(d.name)], ['Email', text(d.email)], ['Summary', text(d.summary)]];
  const email = text(request.kind === 'return' ? d.customer_email : d.email);
  return (
    <div class="panel stack small">
      <p><strong>{KIND_LABELS[request.kind]}</strong> · {request.status}</p>
      <dl class="facts">{rows.filter(([, v]) => v).map(([k, v]) => <><dt>{k}</dt><dd>{v}</dd></>)}</dl>
      <p class="muted">Consent given {when(request.consented_at)}: “{request.consent}” · kept until {when(request.expires_at)}</p>
      <p class="muted">{request.emailed_at ? `Emailed to the team ${when(request.emailed_at)}` : 'Not emailed yet'}
        {request.webhook_error ? ` · webhook failed: ${request.webhook_error}` : ''}</p>
      {email && <a class="secondary button" href={`mailto:${encodeURIComponent(email)}`}>Reply by email</a>}
    </div>
  );
}

// --- Inbox (A-01) -------------------------------------------------------------------------------

const VIEWS = [['all', 'All'], ['follow_up', 'Needs follow-up'], ['requests', 'With a request'], ['open', 'Open'],
  ['resolved', 'Resolved']] as const;

function Transcript({ id, onChanged }: { id: string; onChanged: () => void }) {
  const [chat, setChat] = useState<ConversationDetail | null>(null);
  const { busy, error, run } = useAction();
  useEffect(() => { void run(async () => setChat(await api.conversation(id))); }, [id]);
  if (!chat) return <p class="muted">{error ?? 'Loading…'}</p>;
  const toggle = () => run(async () => {
    await api.setConversationStatus(id, chat.status === 'open' ? 'resolved' : 'open');
    setChat(await api.conversation(id));
    onChanged();
  });
  const remove = () => {
    if (!confirm('Delete this chat, its messages and its requests? This cannot be undone.')) return;
    void run(async () => { await api.deleteConversation(id); onChanged(); });
  };
  return (
    <div class="inbox-detail stack">
      <div class="row">
        <button type="button" class="secondary" disabled={busy} onClick={() => void toggle()}>
          {chat.status === 'open' ? 'Mark resolved' : 'Reopen'}</button>
        <button type="button" class="link small" disabled={busy} onClick={remove}>Delete data</button>
      </div>
      <ol class="transcript">
        {chat.messages.map((m, i) => (
          <li key={i} class={`msg msg--${m.role}`}>
            <span class="muted small">{m.role === 'user' ? 'Visitor' : 'Assistant'} · {when(m.at)}
              {m.flags.length ? ` · flagged: ${m.flags.join(', ')}` : ''}</span>
            <p>{m.content}</p>
          </li>
        ))}
      </ol>
      {chat.requests.map((r) => <RequestCard key={r.id} request={r} />)}
      {error && <p class="error" role="alert">{error}</p>}
    </div>
  );
}

export function InboxStep() {
  const [view, setView] = useState('all');
  const [q, setQ] = useState('');
  const [page, setPage] = useState(1);
  const [list, setList] = useState<Page<ConversationRow> | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const { error, run } = useAction();
  const load = () => run(async () => setList(await api.conversations({ view, q, page: String(page) })));
  useEffect(() => { void load(); }, [view, page]);

  return (
    <section>
      <h1>Inbox</h1>
      <form class="row" onSubmit={(e) => { e.preventDefault(); setPage(1); void load(); }}>
        <select value={view} onChange={(e) => { setView((e.target as HTMLSelectElement).value); setPage(1); }}>
          {VIEWS.map(([id, label]) => <option key={id} value={id}>{label}</option>)}
        </select>
        <input type="search" placeholder="Search messages" value={q} onInput={(e) => setQ((e.target as HTMLInputElement).value)} />
        <button type="submit" class="secondary">Search</button>
      </form>
      {error && <p class="error" role="alert">{error}</p>}
      <div class="inbox">
        <ul class="inbox-list">
          {list?.items.map((c) => (
            <li key={c.id}>
              <button type="button" class={`inbox-row${open === c.id ? ' is-current' : ''}`} onClick={() => setOpen(c.id)}>
                <span class="small muted">{when(c.last_message_at)} · {c.turns} messages{c.status === 'resolved' ? ' · resolved' : ''}
                  {c.flagged ? ' · flagged' : ''}</span>
                <span>{c.preview || '(no message)'}</span>
                {c.requests.map((r, i) => <span key={i} class="tag">{KIND_LABELS[r.kind]}{r.name ? `: ${r.name}` : ''}{r.order ? ` #${r.order}` : ''}</span>)}
              </button>
            </li>
          ))}
          {list && list.items.length === 0 && <li class="muted">No conversations here.</li>}
        </ul>
        {open ? <Transcript id={open} onChanged={() => { void load(); }} /> : <p class="muted">Pick a conversation.</p>}
      </div>
      {list && <Pager page={page} total={list.total} size={list.page_size} onPage={setPage} />}
    </section>
  );
}

// --- Leads (A-04) -------------------------------------------------------------------------------

export function LeadsStep() {
  const [kind, setKind] = useState<'leads' | 'returns'>('leads');
  const [page, setPage] = useState(1);
  const [list, setList] = useState<Page<RequestItem> | null>(null);
  const { error, run } = useAction();
  const load = () => run(async () => setList(await api.requests({ kind, page: String(page) })));
  useEffect(() => { void load(); }, [kind, page]);
  const status = (id: string, value: string) => run(async () => { await api.setRequestStatus(id, value); await load(); });

  return (
    <section>
      <h1>Leads</h1>
      <div class="row">
        <label><input type="radio" checked={kind === 'leads'} onChange={() => { setKind('leads'); setPage(1); }} /> Contact requests</label>
        <label><input type="radio" checked={kind === 'returns'} onChange={() => { setKind('returns'); setPage(1); }} /> Returns</label>
        <a class="secondary button" href={api.exportUrl(kind)} download>Export CSV</a>
      </div>
      <p class="muted small">Requests are deleted after your retention period (Requests &amp; privacy). Exports are logged.</p>
      {error && <p class="error" role="alert">{error}</p>}
      <div class="stack">
        {list?.items.map((r) => (
          <div key={r.id} class="lead">
            <RequestCard request={r} />
            <select value={r.status} onChange={(e) => void status(r.id, (e.target as HTMLSelectElement).value)}>
              <option value="new">New</option><option value="contacted">Contacted</option><option value="closed">Closed</option>
            </select>
          </div>
        ))}
        {list && list.items.length === 0 && <p class="muted">Nothing yet.</p>}
      </div>
      {list && <Pager page={page} total={list.total} size={list.page_size} onPage={setPage} />}
    </section>
  );
}

// --- Knowledge (A-05) ---------------------------------------------------------------------------

function EntryForm({ entry, onDone }: { entry?: KbEntry; onDone: () => void }) {
  const [section, setSection] = useState(entry?.section ?? '');
  const [title, setTitle] = useState(entry?.title ?? '');
  const [body, setBody] = useState(entry?.body ?? '');
  const { busy, error, run } = useAction();
  const save = (e: Event) => {
    e.preventDefault();
    void run(async () => { await api.saveEntry({ section, title, body }, entry?.id); onDone(); });
  };
  return (
    <form class="panel stack" onSubmit={save}>
      <Field label="Section" value={section} onInput={setSection} placeholder="e.g. Services" />
      <Field label="Title" value={title} onInput={setTitle} />
      <label class="field"><span>Text the assistant may answer from</span>
        <textarea required rows={5} maxLength={8000} value={body} onInput={(e) => setBody((e.target as HTMLTextAreaElement).value)} />
      </label>
      <div class="row">
        <button type="submit" class="primary" disabled={busy}>Save draft</button>
        <button type="button" class="link" onClick={onDone}>Cancel</button>
      </div>
      {error && <p class="error" role="alert">{error}</p>}
    </form>
  );
}

function TestConsole() {
  const [question, setQuestion] = useState('');
  const [result, setResult] = useState<AskResult | null>(null);
  const { busy, error, run } = useAction();
  const ask = (e: Event) => { e.preventDefault(); void run(async () => setResult(await api.ask(question))); };
  return (
    <div class="panel stack">
      <h2>Test console</h2>
      <p class="muted small">Asks the published knowledge, as the assistant would. Publish drafts first.</p>
      <form class="row" onSubmit={ask}>
        <input class="grow" required minLength={2} maxLength={500} value={question} placeholder="Ask a question…"
          onInput={(e) => setQuestion((e.target as HTMLInputElement).value)} />
        <button type="submit" class="secondary" disabled={busy}>Ask</button>
      </form>
      {result && (
        <div class="stack small">
          {result.answer !== null && <p><strong>{result.knows ? 'Answer' : "Says it doesn't know"}:</strong> {result.answer}</p>}
          {result.error && <p class="muted">{result.error}</p>}
          <p class="muted">{result.hits.length ? 'Matches:' : 'No matching knowledge.'}</p>
          <ul>{result.hits.map((h, i) => <li key={i}>{h.title} ({h.kind === 'entry' ? 'your entry' : h.url}) · {h.score}</li>)}</ul>
        </div>
      )}
      {error && <p class="error" role="alert">{error}</p>}
    </div>
  );
}

export function KnowledgeStep() {
  const [kb, setKb] = useState<KbState | null>(null);
  const [editing, setEditing] = useState<KbEntry | 'new' | null>(null);
  const { busy, error, run } = useAction();
  const load = () => run(async () => setKb(await api.kb()));
  useEffect(() => { void load(); }, []);
  useEffect(() => {      // follow a publish until the worker finishes it
    if (!kb?.publish || !['queued', 'running'].includes(kb.publish.state)) return;
    const timer = setTimeout(() => void load(), 2000);
    return () => clearTimeout(timer);
  }, [kb]);
  if (!kb) return <p class="muted">{error ?? 'Loading…'}</p>;

  return (
    <section>
      <h1>Knowledge</h1>
      <div class="row">
        <button type="button" class="primary" disabled={busy || kb.changes === 0} onClick={() => void run(async () => { await api.publish(); await load(); })}>
          Publish {kb.changes} change{kb.changes === 1 ? '' : 's'}</button>
        <button type="button" class="secondary" onClick={() => setEditing('new')}>Add entry</button>
        {kb.publish && <span class="small muted">Last publish: {kb.publish.state} {kb.publish.detail}</span>}
      </div>
      {error && <p class="error" role="alert">{error}</p>}
      {editing && <EntryForm entry={editing === 'new' ? undefined : editing} onDone={() => { setEditing(null); void load(); }} />}
      <h2>Your entries</h2>
      <ul class="stack">
        {kb.entries.map((e) => (
          <li key={e.id} class="panel small">
            <p><strong>{e.title}</strong> <span class="muted">· {e.section} · {e.deleted ? 'deleted at next publish'
              : e.changed ? (e.live ? 'edited, not published' : 'draft') : 'live'}</span></p>
            <p class="clamp">{e.body}</p>
            <button type="button" class="link" onClick={() => setEditing(e)}>Edit</button>{' '}
            {!e.deleted && <button type="button" class="link" onClick={() => void run(async () => { await api.deleteEntry(e.id); await load(); })}>Delete</button>}
          </li>
        ))}
        {kb.entries.length === 0 && <li class="muted">No entries yet: add facts the website doesn't say.</li>}
      </ul>
      <TestConsole />
      <h2>Pages from your website</h2>
      <ul class="stack small">
        {kb.pages.map((p) => (
          <li key={p.id} class="row">
            <span class="grow">{p.title || p.url} <span class="muted">· {p.hidden ? 'hidden' : `${p.chunks} pieces`}</span></span>
            <button type="button" class="link" onClick={() => void run(async () => { await api.hidePage(p.id, !p.hidden); await load(); })}>
              {p.hidden ? 'Show again (next re-sync)' : 'Hide'}</button>
          </li>
        ))}
      </ul>
    </section>
  );
}

// --- Team ---------------------------------------------------------------------------------------

export function TeamStep({ state }: { state: OnboardingState }) {
  const [team, setTeam] = useState<TeamState | null>(null);
  const [email, setEmail] = useState('');
  const { busy, error, run } = useAction();
  const load = () => run(async () => setTeam(await api.team()));
  useEffect(() => { void load(); }, []);
  const owner = state.role === 'owner';
  return (
    <section>
      <h1>Team</h1>
      <p class="muted">Members can read chats and leads, edit knowledge and settings. Only the owner manages the team.</p>
      <ul class="stack">
        {team?.members.map((m) => (
          <li key={m.id} class="row">
            <span class="grow">{m.email} <span class="muted small">· {m.role}{m.last_sign_in ? '' : ' · not signed in yet'}</span></span>
            {owner && m.role === 'member' && (
              <button type="button" class="link small" disabled={busy}
                onClick={() => void run(async () => { await api.removeMember(m.id); await load(); })}>Remove</button>
            )}
          </li>
        ))}
      </ul>
      {owner && team && (
        <form class="row" onSubmit={(e) => { e.preventDefault(); void run(async () => { await api.invite(email); setEmail(''); await load(); }); }}>
          <Field label={`Invite by email (${team.members.length}/${team.max_members})`} type="email" value={email} onInput={setEmail} />
          <button type="submit" class="secondary" disabled={busy}>Send invite</button>
        </form>
      )}
      {error && <p class="error" role="alert">{error}</p>}
    </section>
  );
}
