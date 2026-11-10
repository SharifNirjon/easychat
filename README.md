# easychat

Multi-tenant AI chatbot SaaS. MVP: "Aria" widget + admin console. See `CLAUDE.md` for the roadmap.

| Folder | What |
|---|---|
| `api/` | FastAPI: `/health`, `/config`, `/chat` (SSE) |
| `api/tenants/*.json` | One file per tenant: branding, prompts, allowed origins |
| `widget/` | Embeddable chat widget (vanilla TS → one `widget.js`) |

## Prerequisites
Docker Desktop, Python 3.12, Node 20+, curl.

## 1. Start local services (Postgres+pgvector, Redis, Mailpit)
```sh
docker compose up -d
docker compose ps          # db, redis, mailpit should be running/healthy
```
Mailpit inbox: http://localhost:8025

## 2. Configure the API
Copy `api/.env.example` to `api/.env` and set `OPENROUTER_API_KEY`. `api/.env` is git-ignored — never commit it.

Tenants live in `api/tenants/<site_id>.json` (validated at startup). A tenant's `allowed_origins`
decides which websites may load its widget; anything else gets **403**.

## 3. Run the API

**Windows (PowerShell)**
```powershell
cd api
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

**Mac / Linux**
```sh
cd api
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

## 4. Run the widget

```sh
cd widget
cp .env.example .env        # PowerShell: Copy-Item .env.example .env
npm install
npm run dev
```
`npm run dev` rebuilds `widget.js` on every change and serves:

| URL | What |
|---|---|
| http://localhost:5173/widget.js | the widget (where the script tag points) |
| http://localhost:3000 | `demo/index.html` — a deliberately hostile host page (red serif CSS) that proves style isolation |

Ports can be changed: `WIDGET_PORT=5174 DEMO_PORT=3001 npm run dev`
(PowerShell: `$env:DEMO_PORT=3001; npm run dev`).

Embed on any allowed site with one tag:
```html
<script src="http://localhost:5173/widget.js" data-site="culpine" async></script>
```

**On the real Culpine site:** `culpine-website/frontend/vite.config.js` injects that tag in dev only
(never in production builds). Run the widget with the demo moved off port 3000, then the site on 3000:
```sh
# terminal A (widget)
cd widget && DEMO_PORT=3001 npm run dev
# terminal B (Culpine site)
cd ../culpine-website/frontend && npx vite --port 3000
```

Production build (prints the gzipped size, fails above 30 KB):
```sh
npm run build               # -> widget/dist/widget.js
```

## 5. Try the API with curl
`/config` and `/chat` require an `Origin` from the tenant's allowed list.
```sh
curl http://localhost:8000/health
curl -H "Origin: http://localhost:3000" "http://localhost:8000/config?site=culpine"
curl -N -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" -H "Origin: http://localhost:3000" \
  -d '{"site_id":"culpine","messages":[{"role":"user","content":"Hi, what can you do?"}]}'
```
PowerShell: use `curl.exe` and put the body in a file:
```powershell
'{"site_id":"culpine","messages":[{"role":"user","content":"Hi, what can you do?"}]}' | Out-File -Encoding ascii body.json
curl.exe -N -X POST http://localhost:8000/chat -H "Content-Type: application/json" -H "Origin: http://localhost:3000" --data-binary "@body.json"
```
You should see `event: token` lines followed by `event: done`. Failures arrive as `event: error`, never a hang.

## Config notes
- `PRIVACY_MODE=dev` sends no provider preferences (free models allowed). Use `strict` in production:
  it sends `{"data_collection":"deny","zdr":true,"sort":"latency"}`, which free models usually can't satisfy.
- `REASONING=off` disables model reasoning for the fastest first token; `low` enables light reasoning (never streamed to the client).
- `VITE_API_BASE` (widget/.env) is baked into `widget.js` at build time.
