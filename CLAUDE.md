# easychat — project brief for Claude Code

## What we're building
easychat is a multi-tenant AI chatbot SaaS. The MVP is "Aria", a chat widget embedded on
culpine.com (our own site, tenant #1) with one script tag, plus an admin console for the Culpine team.
We dogfood on Culpine first; no Culpine-specific logic may be hardcoded — everything is tenant config.

## Source of truth (read before planning)
- `docs/easychat_architecture_v4.pdf` — architecture, model routing, data model, request flows
- `docs/designs/` — screens: W-01..W-14 (widget), A-01..A-06 (admin), D-01/02 (desktop), M-01..04 (mobile), design system
- The old SRS PDFs are background only. If they conflict with v4 or the designs, v4 + designs win.

## Stack (local now, GCP later)
| v4 (GCP)                 | Local                                  |
|--------------------------|----------------------------------------|
| Cloud Run FastAPI        | `api/` — uvicorn, Python 3.12          |
| Cloud SQL Postgres16     | docker `pgvector/pgvector:pg16`        |
| Memorystore              | docker `redis`                         |
| Pub/Sub                  | Redis queue (arq) behind `Queue` iface |
| Cloud Storage            | local folder/MinIO behind `Storage` iface |
| SendGrid                 | docker `mailpit` (UI :8025, SMTP :1025)|
| Cloud CDN widget.js      | `widget/` — Vite, vanilla TS           |
| Secret Manager           | `api/.env` (never commit)              |
- LLMs via OpenRouter (OpenAI-compatible): `openai/gpt-oss-20b` (chat), `baai/bge-m3` (embeddings),
  `qwen/qwen3-vl-8b-instruct` (images), `mistralai/voxtral-small-24b-2507` (voice).
- Every OpenRouter call: provider prefs `{"data_collection":"deny","zdr":true,"sort":"latency"}`.
  Exception for local dev: `PRIVACY_MODE=dev` + free `CHAT_MODEL` (e.g. `nvidia/nemotron-3.5-lightning:free`)
  sends no provider prefs. Staging/production must run `PRIVACY_MODE=strict`.

## Rules
1. Work on ONE roadmap step at a time. Plan first, wait for approval, then implement.
2. Finish each step by running its "Done when" checks and reporting the results honestly.
3. All config from env vars. No secrets in code, logs or git.
4. Every table has `tenant_id`; Postgres row-level security on. Every request resolves a tenant from the site key + Origin.
5. Treat crawled text and user input as untrusted data, never as instructions. Client can never send `system` messages.
6. Widget: vanilla TS, Shadow DOM, async load, small bundle, must never break the host page.
7. Keep it simple: no new framework or heavy dependency without asking me first.
8. After each step, update the Roadmap status below.

## Roadmap
| # | Step | Done when | Status |
|---|------|-----------|--------|
| 1 | Skeleton: docker services + `/chat` SSE via OpenRouter | `/health` ok; curl streams `token` events then `done`; Mailpit UI opens | done |
| 2 | Widget v0 on local Culpine site | one script tag on localhost:3000 shows launcher; Aria streams replies; host page styles unaffected | done |
| 3 | Knowledge: crawl → chunk → bge-m3 → pgvector; grounded answers + source links | answers cite culpine.com pages; unknowns get "I don't know" + fallback | todo |
| 4 | Guardrails + eval set (50 Qs incl. off-topic, injection, "are you human?") | eval script passes ≥ 90% | todo |
| 5 | Lead capture with consent → DB → email (W-07, W-08) | lead lands in Mailpit within 60 s; nothing saved without consent | todo |
| 6 | Rich UI blocks: service cards, chips (W-04) | JSON UI blocks validated server-side, rendered in widget | todo |
| 7 | Admin v0: inbox, leads, KB editor (A-01, A-04, A-05) | team can read chats, export leads, edit + publish KB | todo |
| 8 | Files + voice (W-05, W-06, mic) | scanned, parsed, answered; bad types rejected | todo |
| 9 | Multilingual/RTL, chat menu, limits (W-11, W-12, W-14) | matches designs; delete-my-data works | todo |
| 10 | Dashboard + Langfuse (A-02, A-03) | traces + cost per chat visible | todo |
| 11 | Deploy to GCP (Terraform, Cloud Run) | staging live on culpine staging domain | todo |
