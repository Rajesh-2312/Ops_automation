# Deployment

Two halves, deployed separately, joined by two settings that must agree.

| Half | What runs it | Deployed by |
|---|---|---|
| Console (React) | Static files | `.github/workflows/deploy-pages.yml` → GitHub Pages |
| API (FastAPI) | A process | `Dockerfile` + `render.yaml` → Render (or any host that runs containers) |

Everything the console does through Supabase — programs, batches, checklists,
trainers, alerts — works with the API absent, because those calls go to
PostgREST directly and RLS enforces the walls. The seven screens that need the
API do not: **Payouts, Approvals, Comms queue, ERM sync, Reports, Ops Copilot**,
and the two program-generation buttons.

That is why the console can look almost entirely healthy while the Copilot
returns an error. It is not a Copilot fault; it is the half of the system that
needs a process to be running.

---

## The two settings

```
API service:   CORS_ALLOWED_ORIGINS = https://rajesh-2312.github.io
Pages build:   VITE_API_BASE_URL    = https://bytexl-ops-api.onrender.com
```

Each points at the other. Getting either wrong produces the *same* symptom —
"Could not reach the API" in the console, and **nothing at all in the API's
log**, because a request the browser refuses never arrives.

`CORS_ALLOWED_ORIGINS` takes an **origin**: scheme and host, no path, no
trailing slash. `https://rajesh-2312.github.io/Ops_automation/` is a URL and
will match nothing.

`VITE_API_BASE_URL` is a **build-time substitution**. Vite bakes it into the
JavaScript, so changing it requires re-running the Pages workflow. A reload will
not pick it up, and neither will a browser hard-refresh.

---

## Deploying the API

### 1. Create the service

Render reads `render.yaml` from the repository root. New → Blueprint → pick this
repo. Everything marked `sync: false` is prompted for and stored outside the
repo; nothing else needs touching.

Any container host works — the blueprint carries no Render-specific code, only
`Dockerfile` and a list of environment variables.

### 2. Set the secrets

| Variable | Where it comes from | Required |
|---|---|---|
| `DATABASE_URL` | Supabase → Project Settings → Database → **Session** pooler | yes |
| `SUPABASE_URL` | Project Settings → API | yes |
| `SUPABASE_ANON_KEY` | Project Settings → API | yes |
| `SUPABASE_SERVICE_ROLE_KEY` | Project Settings → API | yes |
| `SUPABASE_JWT_SECRET` | Project Settings → API → JWT Settings | yes¹ |
| `OPENROUTER_API_KEY` | openrouter.ai | Copilot only |
| `OPENROUTER_MODEL_VOLUME` / `_FRONTIER` | model slugs | Copilot only |
| `OPENROUTER_EMBEDDING_MODEL` | a **1536-dimension** model | Copilot only |

¹ Optional to boot, required in practice: without it `app.core.security` fails
closed and every authenticated endpoint returns 500. That is deliberate — a
verifier that cannot verify must never fall back to trusting the token.

**`DATABASE_URL` must be the session pooler on port 5432.** Port 6543 is the
transaction pooler, which does not preserve `SET ROLE` across statements. The
application refuses that port at startup rather than let the RLS persona tests
pass while proving nothing.

**The embedding model must be 1536-dimensional.** `rag_embeddings.embedding` is
`vector(1536)` (migration 1600), and a corpus embedded under one model and
queried under another returns confident, wrong neighbours rather than an error.

### 3. Point the console at it

Repo → Settings → Secrets and variables → Actions → `VITE_API_BASE_URL` = the
service URL, no trailing slash. Then re-run **Deploy Pages**.

---

## Checking it worked

```bash
# 1. The service is up. No auth, no database — this answers if the process runs.
curl https://bytexl-ops-api.onrender.com/health
# {"status":"ok","app_env":"prod","version":"0.1.0"}

# 2. The browser will be allowed to call it. The header is the whole answer;
#    its absence is the refusal, and there is no error status to look for.
curl -i -X OPTIONS https://bytexl-ops-api.onrender.com/copilot/ask \
  -H "Origin: https://rajesh-2312.github.io" \
  -H "Access-Control-Request-Method: POST" \
  -H "Access-Control-Request-Headers: authorization,content-type" \
  | grep -i access-control-allow-origin
# access-control-allow-origin: https://rajesh-2312.github.io
```

If the first works and the second returns nothing, `CORS_ALLOWED_ORIGINS` is
wrong — almost always a trailing slash or a path.

---

## Things that will bite

**Render's free tier sleeps.** After 15 minutes idle the container stops, and
the next request waits ~50 seconds for a cold start. The console's own timeouts
may fire first, which reads as an outage. A paid instance removes it.

**`/docs` is off in production.** `APP_ENV=prod` disables `/docs` and
`/openapi.json`, because the schema is a map of every commercial endpoint. A 404
there is correct.

**The Copilot needs a corpus.** Deploying the API does not ingest documents.
From a machine with `DATABASE_URL` set:

```bash
python tools/ingest_docs.py --ingest docs/corpus/sop --corpus sop --commercial chunks
```

`tools/` is deliberately **not** in the image (see `.dockerignore`): it holds
code that writes to the database and, in `agentmail.py`, code that can send
mail. CLAUDE.md R3 keeps send-capable code away from anything an agent can
reach, and keeping it out of the image keeps it away from the running service
too.

**Migrations are not applied by the deploy.** They are hand-authored SQL in
`supabase/migrations/`, applied in filename order, and that is the single source
of truth for the schema (CLAUDE.md §11). Nothing here runs them.

---

## Running the image locally

```bash
docker build -t bytexl-ops-api .
docker run --rm -p 8000:8000 --env-file .env -e CORS_ALLOWED_ORIGINS=http://localhost:5173 \
  bytexl-ops-api
```

`CORS_ALLOWED_ORIGINS` is needed in dev too. Vite serves the console on `:5173`
and the API on `:8000`; those are different origins, so every browser call
between them is cross-origin. Without it the console reports "Could not reach
the API" against a service that is running perfectly.
