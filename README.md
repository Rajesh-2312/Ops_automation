# byteXL Ops Intelligence Platform

Operations platform for byteXL's college training programs. Tracks the full program
lifecycle from MoU to trainer payout, across every college associated with byteXL.

**[CLAUDE.md](CLAUDE.md) is the binding specification.** Read §1 (hard rules R1–R7) before
writing any code. This README only covers how to run things.

---

## Prerequisites

| Tool | Version | Notes |
|---|---|---|
| Python | **3.11** | what CI runs — see below |
| Node | 20+ | frontend only |
| Postgres access | — | a Supabase project |

**Use Python 3.11, and work inside `.venv`.** `pyproject.toml` says `>=3.11` and
newer interpreters mostly work, but `ruff` and `black` are configured for `py311`
and CI runs 3.11 exclusively — so anything else is a configuration nobody
validates. Dependencies come from the **lock**, `requirements-dev.txt`, not from
the pyproject ranges, which pin floors only. Installing from the ranges resolves
whatever is newest that day and lets CI drift; that is exactly how a route test
came to pass on FastAPI 0.136 and fail on 0.141 with the app serving identically.

## Setup

```bash
# 1. Python environment — 3.11, from the lock
py -3.11 -m venv .venv          # or: uv venv --python 3.11
.venv\Scripts\activate          # Windows
pip install --require-hashes -r requirements-dev.txt
pip install -e . --no-deps

# 2. Environment
cp .env.example .env            # then fill it in — see below

# 3. Apply the schema
python supabase/tests/run_tests.py --apply-only

# 4. Run
python run_api.py               # API  -- NOT `uvicorn app.main:app`, see below
cd frontend && npm install && npm run dev
```

**Start the API with `python run_api.py`.** On Windows, `uvicorn app.main:app`
appears to work and then fails every database call. Python builds a
`ProactorEventLoop` by default, psycopg's async driver refuses to run on it, and
the result is the worst shape of failure: startup logs are clean, `/health`
returns 200, and every endpoint that touches the database returns 500. A health
check would call the service up. `run_api.py` builds a selector loop via
`asyncio.run(..., loop_factory=...)` — the loop must be chosen where it is
created, and uvicorn creates its own after importing `app.main`, so nothing
inside that module can fix it. On Linux and macOS the script is a plain
`asyncio.run`, so the same command works everywhere.

### Filling in `.env`

Values come from the Supabase dashboard under **Project Settings → API** and
**→ Database**. Two things bite people:

- **`DATABASE_URL` must use port 5432**, the session pooler — not 6543, the transaction
  pooler. The transaction pooler does not preserve `SET ROLE` across statements, which
  makes every RLS persona test pass while proving nothing. `app/core/config.py` rejects
  port 6543 outright rather than let that happen silently.
- **`SUPABASE_SERVICE_ROLE_KEY` carries `BYPASSRLS`.** It is backend-only and never
  reaches the frontend. Every code path using it must re-check role and ownership itself —
  RLS policies *and* the column-guard triggers both step aside for it.

`OPENROUTER_MODEL_VOLUME` and `OPENROUTER_MODEL_FRONTIER` set the two routing tiers
(CLAUDE.md §2). They live in env so routing changes without a code deploy.

### Auth posture: email + password only

There is **no email confirmation, no OTP, no magic link**. Sign up, and you are signed
in. Verification is a deferred feature.

That is enforced in `supabase/migrations/1100_no_email_confirmation.sql`, not by a
dashboard setting — a `BEFORE INSERT` trigger on `auth.users` confirms every row as it is
created, so an account is usable the instant it exists whatever GoTrue is configured to
do. Keeping it in a migration means the posture is visible in a fresh clone and cannot
drift silently.

Optional tidy-up, and the only part that needs the dashboard: **Authentication → Sign In /
Providers → Email → Confirm email → off**. With it still on, signup works fine (the form
falls back to an immediate password sign-in) but GoTrue mails a confirmation link nobody
needs to click.

To bring verification back later, drop the trigger in a new migration. Do not edit 1100.

### The first admin

`is_admin` is never taken from signup metadata — 0200 refuses to honour it, because
`raw_user_meta_data` is attacker-controlled. Only an admin may write the assignment
tables, so a fresh database has no admin and no in-app way to make one: every account is
permanently scopeless.

`1200_backfill_profiles_and_bootstrap_admin.sql` breaks that cycle by promoting the
oldest account to `senior_manager` + `is_admin`, **only while no admin exists**. Apply the
schema, sign up, re-run `--apply-only`, and the first account owns the console. It also
backfills profile rows for any account created before 0200 was applied — those get no
profile otherwise, since the signup trigger is `AFTER INSERT` and does not fire
retroactively.

A senior manager still sees nothing until clusters exist and are assigned. Reach is never
implied by persona.

---

## Architecture in one paragraph

Supabase Postgres RLS is the permission wall — the frontend talks to it directly with the
user's JWT for ordinary CRUD, and the database decides what each of the five personas can
see. FastAPI exists only for what RLS cannot express: checklist generation, the payout
engine, the approval state machine, roll-ups, and integrations. React is five front doors
onto the same data. Agents draft and retrieve; they never decide or send.

## Layout

```
app/
  api/          FastAPI routers, one per workstream
  core/         config, security, LLM gateway, audit
  db/           SQLAlchemy models — a mapping layer, never a schema generator
  domain/       pure dataclasses and enums, zero I/O
  services/     remuneration · approval · erm · comms
  agents/       supervisor + specialists; tools/ is READ AND DRAFT ONLY
  rag/          ingestion, chunking, retrieval with persona filter
supabase/
  migrations/   hand-authored SQL, applied in filename order. The source of truth.
  tests/        RLS persona matrix
tools/          rule_linter.py — enforces R1–R7 at CI time
tests/          unit · integration · rls · fixtures
docs/
frontend/
```

`domain/` imports nothing from `db/`, `api/`, or `agents/`. That is checked, not trusted.

---

## Testing

Run these from the activated `.venv` (Python 3.11). A different interpreter or a
non-locked dependency set is not the environment CI gates on, and the difference
is not always visible — it can hide a failure as easily as invent one.

```bash
pytest -m "not integration and not rls"   # what CI runs
python tools/rule_linter.py               # R1-R7 static checks
python supabase/tests/run_tests.py        # RLS persona matrix, needs DATABASE_URL
ruff check . && black --check . && mypy app/
```

### Changing a dependency

Edit the range in `pyproject.toml`, then regenerate the lock and reinstall:

```bash
uv pip compile pyproject.toml --extra dev --universal \
  --python-version 3.11 --generate-hashes -o requirements-dev.txt
pip install --require-hashes -r requirements-dev.txt
```

`--universal` resolves markers for every platform, so one lock serves Windows
development and the Linux CI runner. Commit the regenerated file with the change
that caused it — a lock that lags its `pyproject.toml` is worse than none,
because it looks authoritative.

The RLS suite applies migrations, then runs its fixtures and assertions inside a single
transaction that is **unconditionally rolled back** — on pass, on fail, and on Ctrl-C. It
first deletes every application row inside that transaction, so `select count(*)` is an
unscoped proof rather than "the rows I happened to know about". It is safe to run against
a database with data in it, but point it at a non-production project anyway.

Three things are non-negotiable and fail the build:

- The payout engine reconciles both CLAUDE.md §6 fixtures **to the rupee**. If they break,
  the build is broken — do not adjust the fixtures to match the code.
- Every persona boundary has a test asserting a forbidden read returns **zero rows**.
- No agent toolset exposes a release-capable tool. Checked by both a test and the linter.

## Migrations

Hand-authored SQL in `supabase/migrations/`, applied in filename order. Not Alembic — the
security posture *is* RLS policies, `SECURITY DEFINER` helpers and triggers on
`auth.users`, none of which Alembic diffs, and two migration systems against one database
is a defect generator. See CLAUDE.md §11.

**Never edit a shipped migration.** Add a new one.
