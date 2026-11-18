# easychat

Multi-tenant AI chatbot SaaS. MVP: "Aria" widget + admin console. See `CLAUDE.md` for the roadmap.

| Folder | What |
|---|---|
| `api/` | FastAPI: `/health`, `/ready`, `/config`, `/chat` (SSE). Postgres (RLS) + Redis limits |
| `api/crawl.py`, `api/ask.py` | Knowledge base: crawl a tenant's site; inspect what retrieval finds |
| `api/migrations/` | Plain SQL migrations, applied in order by `migrate.py` |
| `api/seeds/*.json` | Tenant + site key definitions, loaded with `seed_tenant.py` |
| `api/tests/` | pytest suite (runs against a throwaway database) |
| `widget/` | Embeddable chat widget (vanilla TS → one `widget.js` + self-hosted fonts) |

## Prerequisites
Docker Desktop, Node 20+, curl. Python 3.12 is optional — everything Python also runs in Docker.

## 1. Start local services (Postgres+pgvector, Redis, Mailpit)
```sh
docker compose up -d
docker compose ps          # db, redis, mailpit should be running/healthy
```
Mailpit inbox: http://localhost:8025

## 2. Configure the API
Copy `api/.env.example` to `api/.env` and set `OPENROUTER_API_KEY`. `api/.env` is git-ignored — never commit it.

Two database logins, on purpose:
- `MIGRATION_DATABASE_URL` — the schema owner. Used only by `migrate.py` and `seed_tenant.py`.
- `DATABASE_URL` — what the API uses: a **non-owner** role, so Postgres row-level security
  always applies to it. Locally, `migrate.py` creates it for you.

## 3. Create the schema and the tenants
```sh
docker compose --profile api build api
docker compose --profile api run --rm api python migrate.py
docker compose --profile api run --rm api python seed_tenant.py seeds/culpine.json
docker compose --profile api run --rm api python seed_tenant.py seeds/mirela.json   # fictional demo shop
```
(With a local Python 3.12 venv: `cd api && python migrate.py && python seed_tenant.py seeds/culpine.json && python seed_tenant.py seeds/mirela.json`.)

Two tenants, same widget, different config: **Culpine** (green, lead-gen) and **Mirela**, a
fictional skincare shop (terracotta, Mira designs). A tenant's theme, home screen (promo, action
cards, chips) and allowed image hosts all live in its seed file.

A tenant's site key (`pk_…`) goes in the script tag; its `allowed_origins` decide which websites
may use it. Anything else gets **403**. Re-running the seed updates the tenant in place.

## 4. Run the API
**In Docker (production image, no local Python needed)**
```sh
docker compose --profile api up --build api      # http://localhost:8000
```

**Or with a local venv (Mac/Linux; on Windows use `py -3.12` and `.venv\Scripts\Activate.ps1`)**
```sh
cd api
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn main:app --reload --port 8000
```
**Windows:** run `python serve_local.py` instead of `uvicorn main:app`. psycopg's async driver
can't use Windows' default event loop, so plain uvicorn fails at startup with `PoolTimeout`.
Also keep the database/Redis URLs on `127.0.0.1`, not `localhost`: Windows tries IPv6 `::1`
first, where Docker isn't listening, and the connection hangs.

## 5. Run the widget
```sh
cd widget
cp .env.example .env        # PowerShell: Copy-Item .env.example .env
npm install
npm run dev
```
`npm run dev` rebuilds `widget.js` on every change and serves:

| URL | What |
|---|---|
| http://localhost:5173/widget.js | the widget (where the script tag points); fonts at `/fonts/` |
| http://localhost:3000 | `demo/index.html` — Culpine on a deliberately hostile host page (red serif CSS) that proves style isolation |
| http://localhost:3000/shop.html | `demo/shop.html` — the fictional Mirela shop (its seed allows only port 3001, so run the demo there: see below) |

Ports can be changed: `WIDGET_PORT=5174 DEMO_PORT=3001 npm run dev`
(PowerShell: `$env:DEMO_PORT=3001; npm run dev`).

**See every UI block (local only):** open the chat and type `/demo` — it lists the samples
(`/demo carousel`, `quiz`, `returns`, `timeline`, `form`, `sources`, `buttons`, `card`, `invalid`).
They run through the real pipeline (server validation → database → SSE `block` events → widget)
without calling the model; `/demo invalid` shows the server dropping bad blocks. The command is
ignored outside `APP_ENV=local/test`.

Embed on any allowed site with one tag:
```html
<script src="http://localhost:5173/widget.js" data-key="pk_local_culpine" async></script>
```

**On the real Culpine site:** `culpine-website/frontend/vite.config.js` injects that tag in dev only
(never in production builds). Run the widget with the demo moved to port 3001 (where both demo
pages work), then the site on 3000:
```sh
# terminal A (widget)
cd widget && DEMO_PORT=3001 npm run dev
# terminal B (Culpine site)
cd ../culpine-website/frontend && npx vite --port 3000
```

Production build (fails without `VITE_API_BASE`; prints the gzipped size, fails above 30 KB):
```sh
npm run build               # -> widget/dist/widget.js + widget/dist/fonts/
```
Host `dist/` as-is. The fonts are loaded cross-origin, so the CDN must send
`Access-Control-Allow-Origin: *` for `/fonts/*`.

## 6. Try the API with curl
`/config` and `/chat` require an `Origin` from the site key's allowed list.
```sh
curl http://localhost:8000/health
curl -H "Origin: http://localhost:3000" "http://localhost:8000/config?key=pk_local_culpine"
curl -N -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" -H "Origin: http://localhost:3000" \
  -d '{"site_key":"pk_local_culpine","message":"Hi, what can you do?"}'
```
You should see `event: meta` (with a `conversation_id`), `event: token` lines, zero or more
`event: block` lines (validated UI blocks, see `api/blocks.py`), then `event: done`.
Send the next turn with `"conversation_id": "<that id>"`. The server keeps the history: clients
send only their new message. Failures arrive as `event: error`, never a hang. Limit hits are
HTTP 429 with `Retry-After`.

## 7. Knowledge base (grounded answers)
A tenant's `knowledge` config (seed file) says what to crawl: `sitemaps` and/or `start_urls`, and
`max_pages`. With no sitemap the crawler follows same-site links from the start URLs. It is plain
HTTP (no JavaScript), so single-page apps need prerendered HTML, as culpine.com has.
```sh
cd api
python crawl.py culpine          # crawl -> chunk -> bge-m3 embeddings -> pgvector (re-run any time)
python ask.py culpine "Where is your office?"   # what retrieval finds, with scores; no chat model call
```
(Docker: `docker compose --profile api run --rm api python crawl.py culpine`.)

- Re-crawling only re-embeds pages whose content changed; pages gone from the site are removed.
- The crawler only fetches the hosts of the configured URLs, over https (http only for localhost
  in local/test), and refuses private/internal addresses, including after redirects.
- Mirela's pages live in `widget/demo/mirela/` (served on :3001 by `DEMO_PORT=3001 npm run dev`).
- Once a tenant has a knowledge base, `/chat` answers from it: replies carry a `sources` block
  (and product cards for schema.org `Product` pages). When the knowledge doesn't answer, the
  reply says so and shows the tenant's `fallback` (notice + contact/handoff chips).

## 8. Tests
```sh
docker compose run --rm --build api-test      # pytest against easychat_test + Redis db 15
cd widget && npm run build                     # typecheck + build + size budget
```
CI (`.github/workflows/ci.yml`) runs the same on every push and pull request.

## Config notes
- `APP_ENV` is required: `local | test | staging | production`. `/docs` is only served in local/test.
- `PRIVACY_MODE` defaults to `strict`: `{"data_collection":"deny","zdr":true,"sort":"latency"}`.
  `dev` (no provider prefs, so free models work) is **refused** unless `APP_ENV` is local/test.
- `REASONING=off` disables model reasoning for the fastest first token; `low` enables light reasoning (never streamed to the client).
  **`openai/gpt-oss-20b` requires `REASONING=low`**: it rejects requests with reasoning disabled.
- Knowledge: `EMBED_MODEL` (default `baai/bge-m3`, must return 1024 dimensions), `RAG_MIN_SCORE`
  (default 0.45: cosine similarity a chunk needs to be used), `RAG_TOP_K` (default 6 chunks per answer).
  Tune the floor with `ask.py`.
- Limits: `IP_MESSAGES_PER_MINUTE` (default 20), `MAX_REQUEST_BYTES` (16 KB). Per-tenant caps
  (daily tokens, monthly conversations, turns per conversation) live in the tenant's `limits`.
- `TRUSTED_PROXY_HOPS`: number of proxies in front of the API that append to `X-Forwarded-For`
  (0 locally). Set it correctly per deployment or clients can spoof their IP past the rate limit.
- `VITE_API_BASE` (widget/.env) is baked into `widget.js` at build time.
