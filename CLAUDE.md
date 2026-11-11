# easychat — project brief for Claude Code

## What we're building
easychat is a multi-tenant AI chatbot SaaS. The MVP is "Aria", a chat widget embedded on
culpine.com (our own site, tenant #1) with one script tag, plus an admin console for the Culpine team.
We dogfood on Culpine first; no Culpine-specific logic may be hardcoded — everything is tenant config.

## Source of truth (read before planning)
- `docs/easychat_architecture_v4.pdf` — architecture, model routing, data model, request flows
- `docs/designs/` — screens: W-01..W-14 (widget), A-01..A-06 (admin), D-01/02 (desktop), M-01..04 (mobile), design system
- `docs/designs/widget designs/` — the current widget visual design ("Mira", e-commerce). Designs 6 (order tracking) and 7 (returns form) are the reference flows.
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
  sends no provider prefs. Staging/production must run `PRIVACY_MODE=strict` (the default; `dev` is refused unless `APP_ENV` is local/test).

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
| 3 | Foundation + review fixes: Postgres schema + RLS, tenants/site keys in DB, server-side conversations, Redis rate limits + budgets, hardening, tests, Dockerfile, CI | review issues closed; cross-tenant RLS tests pass; spoofed-Origin flood gets 429 | done |
| 4 | Widget restyle + UI blocks: Mira shell (`docs/designs/widget designs/`) themed per tenant; server-validated JSON blocks (cards, chips, checkbox/radio lists, timeline, buttons, forms) | Culpine (green) and a demo shop (terracotta) both match designs; invalid blocks rejected server-side | todo |
| 5 | Knowledge: sitemap.xml/URL crawl (SSRF-safe, plain HTTP) → chunk → bge-m3 → pgvector; products from schema.org `Product`; grounded answers + source links | answers cite pages; unknowns get "I don't know" + fallback | todo |
| 6 | Guardrails + eval set (50+ Qs incl. off-topic, injection, "are you human?", order-data leakage) | eval ≥ 90%; 100% on data-leak cases | todo |
| 7 | Self-serve onboarding: magic link → URL or sitemap upload → auto branding → crawl progress → preview → copy script tag → "installed ✓"; free-plan caps | new tenant from email to working widget in < 5 min, no staff | todo |
| 8 | Commerce: Shopify connect + generic order-lookup API; order tracking with email OTP (widget design 6) | verified visitor sees timeline; wrong code/email reveals nothing | todo |
| 9 | Chat forms (fixed set): return request (design 7), lead capture with consent (W-07, W-08), talk to a person, email transcript, delete my data | requests reach shop/team by email + webhook; nothing saved without consent | todo |
| 10 | Admin v0: inbox, leads, KB, settings/install (A-01, A-04, A-05, A-06) | team reads chats, exports leads, edits + publishes KB | todo |
| 11 | Files + voice (W-05, W-06, mic) | scanned, parsed, answered; bad types rejected | todo |
| 12 | Multilingual/RTL, chat menu, limit UI (W-11, W-12, W-14) | matches designs | todo |
| 13 | Dashboard + Langfuse (A-02, A-03) | traces + cost per chat visible | todo |
| 14 | Deploy to GCP (Terraform, Cloud Run) | staging live on culpine staging domain | todo |

Product decisions (2026-11-10): open sign-up with hard per-tenant caps; admin login by email magic link;
fixed chat-form set (no form builder); crawler is plain HTTP (no headless browser); order data via
Shopify + generic API; order access needs an emailed one-time code; returns are requests to the shop
team (no store writes). Ask before adding Preact for the admin/onboarding UI, and before cart/checkout.

Data-model notes: `schema_migrations` is the only table without `tenant_id` (infrastructure, not
tenant data). The API connects as a non-owner role so RLS always applies; only `resolve_site_key()`
(SECURITY DEFINER) reads across tenants, and only for the exact key it is given.
