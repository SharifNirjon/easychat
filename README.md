# easychat

Multi-tenant AI chatbot SaaS. MVP: "Aria" widget + admin console. See `CLAUDE.md` for the roadmap.

| Folder | What |
|---|---|
| `api/` | FastAPI: `/health`, `/ready`, `/config`, `/chat` (SSE). Postgres (RLS) + Redis limits |
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

## 3. Create the schema and the Culpine tenant
```sh
docker compose --profile api build api
docker compose --profile api run --rm api python migrate.py
docker compose --profile api run --rm api python seed_tenant.py seeds/culpine.json
```
(With a local Python 3.12 venv: `cd api && python migrate.py && python seed_tenant.py seeds/culpine.json`.)

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
| http://localhost:3000 | `demo/index.html` — a deliberately hostile host page (red serif CSS) that proves style isolation |

Ports can be changed: `WIDGET_PORT=5174 DEMO_PORT=3001 npm run dev`
(PowerShell: `$env:DEMO_PORT=3001; npm run dev`).

Embed on any allowed site with one tag:
```html
<script src="http://localhost:5173/widget.js" data-key="pk_local_culpine" async></script>
```

**On the real Culpine site:** `culpine-website/frontend/vite.config.js` injects that tag in dev only
(never in production builds). **Update it from `data-site="culpine"` to `data-key="pk_local_culpine"`.**
Run the widget with the demo moved off port 3000, then the site on 3000:
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
You should see `event: meta` (with a `conversation_id`), `event: token` lines, then `event: done`.
Send the next turn with `"conversation_id": "<that id>"`. The server keeps the history: clients
send only their new message. Failures arrive as `event: error`, never a hang. Limit hits are
HTTP 429 with `Retry-After`.

## 7. Tests
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
- Limits: `IP_MESSAGES_PER_MINUTE` (default 20), `MAX_REQUEST_BYTES` (16 KB). Per-tenant caps
  (daily tokens, monthly conversations, turns per conversation) live in the tenant's `limits`.
- `TRUSTED_PROXY_HOPS`: number of proxies in front of the API that append to `X-Forwarded-For`
  (0 locally). Set it correctly per deployment or clients can spoof their IP past the rate limit.
- `VITE_API_BASE` (widget/.env) is baked into `widget.js` at build time.
