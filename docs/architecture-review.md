# Architecture review — byteXL Ops Intelligence Platform

Date: 2026-08-19 · Scope: whole repo, read-only · Reviewer: senior architecture pass

**How to read this.** Findings are ranked by impact × likelihood. Each one is
labelled **DEFECT** (the code does not do what it says, or does something that
will hurt) or **TRADE-OFF** (a defensible decision I would make differently, and
which one is right depends on facts I do not have). I have tried hard not to
dress up taste as a bug. Section 12 lists the things I checked that are correct,
because a review that only lists problems misrepresents the codebase — this is
an unusually well-reasoned repo and most of what I probed held.

Every number below is measured, not estimated. Method is in §13.

---

## 0. Summary table

| # | Finding | Kind | Impact | Likelihood | Effort |
|---|---|---|---|---|---|
| 1 | RLS reach helpers cost **523 µs/row**; flat equivalent is **1.8 µs** (290×). Every policy on `tasks`/`batches`/`students`/`attendance` pays it per row. | DEFECT | Critical | Certain | 1–2 d |
| 2 | Frontend issues **111 PostgREST reads and 0 `.limit()`/`.range()`**. Unbounded reads multiply #1. | DEFECT | Critical | Certain | 1–2 d |
| 3 | `/reports/*` returns **503 in a Phase-1 deployment even with `include_narrative=false`**. Proven by execution. No test covers it. | DEFECT | High | Certain | 30 min |
| 4 | `2000_last_admin_guard.sql` has **never been applied** to the live database. One admin exists. | DEFECT | High | Medium | 15 min |
| 5 | `tests/rag_eval/conftest.py` injects the real `.env` — including the **production `DATABASE_URL` and service-role key** — into the whole pytest process. | DEFECT | High | Certain | 1 h |
| 6 | CI: `pip install -e . --no-deps` **defeats `--require-hashes`**; frontend is never built, typechecked or tested; RLS suite is run by no automation. | DEFECT | High | Certain | 2–3 h |
| 7 | `LLMClient` → `AsyncOpenAI` → `httpx.AsyncClient` constructed **per request**, never closed. | DEFECT | Medium | High | 1 h |
| 8 | N+1 awaits: `erm.py` (2 queries × up to 200 rows), `reports.py` (~7 queries × programs). | DEFECT | Medium | High | 3–4 h |
| 9 | No connection-pool sizing, no `statement_timeout`, no LLM timeout. | TRADE-OFF | Medium | Medium | 1 h |
| 10 | `POST /payouts/commit` idempotency is check-then-act; a concurrent double-click yields **500**, not the documented 200/409. | DEFECT | Medium | Medium | 1 h |
| 11 | Package-level import cycles `app.core ↔ app.db` and `app.agents ↔ app.services`. No module-level cycles; `domain/` is clean. | TRADE-OFF | Low | — | 2 h |
| 12 | §11's `information_schema` drift test does not exist. Five columns on `programs` are unmapped, found in seconds once looked for. | DEFECT | Medium | Certain | 2 h |
| 13 | Migration ledger records `statements = NULL`. "Never edit a shipped migration" is unenforced. | DEFECT | Medium | Low | 1–2 h |
| 14 | 802 KB single-chunk bundle. Measured route split saves **62 kB gzip (27%)** on first load. | TRADE-OFF | Medium | Certain | 2 h |
| 15 | `commsKeys.queue()` omits `limit` — the same class as the fixed `erm.ts` bug, currently latent. | DEFECT | Low | Low | 5 min |
| 16 | Duplicate index on `trainer_attendance`, 16 FKs with no covering index. | DEFECT | Low | Medium | 1 h |
| 17 | `list_payout_queue` over-fetches commercial rows outside the caller's reach and has no pagination. | TRADE-OFF | Low | Medium | 2 h |
| 18 | The two-wall (BYPASSRLS API + RLS frontend) architecture itself. | TRADE-OFF | — | — | see §2 |
| 19 | Four policies carry the wall with **no reach conjunct**; chained with self-assignable persona at signup this is a cross-tenant read **and write** of 1,025 payment rails. **Missed on my first pass, then under-rated.** Severity and fix owned by `docs/security-findings.md` SEC-01/02/03; §2.4 adds a fourth policy they did not list. | DEFECT | **Critical** | — | see that doc |

---

## 1. The database performance finding — the largest single risk

### 1.1 What was measured

Against the live Supabase project, impersonating a real Senior Manager exactly the
way PostgREST does (`set local role authenticated` + `request.jwt.claims`), on
PostgreSQL 17.0.6:

```
predicate                                    per call    vs flat
------------------------------------------   ---------   -------
flat inline EXISTS (identical semantics)         1.8 µs      1×
college_id in (select public.my_college_ids())   1.6 µs      1×
public.can_reach_college(<column>)              47.1 µs     26×
public.can_reach_program(<column>)             523.6 µs    291×
public.can_reach_batch(<column>)               455.5 µs    253×
public.can_reach_deployment(<column>)          465.6 µs    259×
public.can_read_corpus(<column>)               156.4 µs     87×
```

`can_reach_program`, `can_reach_batch` and `can_reach_deployment` all call
`can_reach_college()` from inside their own body
(`supabase/migrations/0300_org_core.sql:295,313`,
`supabase/migrations/0400_trainers_deployments.sql:141`). Every one of these is
`STABLE SECURITY DEFINER SET search_path = ''`, which makes it **non-inlinable**;
a nested call therefore pays a second full function invocation. The 47 µs → 523 µs
jump is that second level.

The per-row evaluation is not inference. Here is the real plan, unmodified:

```
select count(*) from public.tasks;

Aggregate (actual time=17.447..17.447 rows=1 loops=1)
  ->  Seq Scan on tasks (actual time=17.443..17.443 rows=0 loops=1)
        Filter: (is_internal() AND can_reach_program(program_id))
        Rows Removed by Filter: 37
Execution Time: 17.501 ms
```

**37 rows cost 17.5 ms.** No index can help — the predicate is an opaque function
of a column.

### 1.2 The documented reasoning is empirically backwards

`supabase/migrations/0300_org_core.sql:266–270` argues for the function form over
the set form:

> Membership test, written against the base tables rather than as
> `... in (select my_college_ids())`. Same answer, but this form is two indexed
> lookups with an early exit, whereas the set version expands the caller's whole
> cluster before comparing — which for a Senior Manager over a 40-college cluster
> happens once per row of every policy evaluation.

The premise "happens once per row" is false. `my_college_ids()` takes **no
arguments**, so the planner hoists it. Measured plan for the set form:

```
Hash Semi Join (actual time=... rows=10000 loops=1)
  Hash Cond: (<column> = (my_college_ids()))
  ->  HashAggregate (actual time=2.354..2.356 rows=1 loops=1)
        Group Key: my_college_ids()
        ->  ProjectSet (actual time=2.329..2.350 rows=1 loops=1)   ← loops=1
```

`loops=1`. The cluster is expanded **once per statement**, hashed, and every row
is then a hash probe. That is why the set form measures 1.6 µs/row against
47 µs/row for the function it was rejected in favour of. The decision was
reasonable to make without a profiler and is wrong with one.

### 1.3 What it costs at scale

`trainer_attendance` is one row per deployment per day by design (CLAUDE.md §6),
so it is the table that grows fastest. Its policy is
`is_internal() AND can_reach_deployment(deployment_id)` at 465.6 µs/row:

| rows in `trainer_attendance` | `select … from trainer_attendance` via PostgREST |
|---|---|
| 10,000 | **4.7 s** |
| 50,000 | **23 s** |
| 100,000 | **47 s** |

At 40 trainers × 250 working days, 100k rows is roughly year ten — but 10k is
roughly year one, and 4.7 s for one screen is already a broken product. `tasks`
is 37 rows per program (`supabase/seed.sql`), so 500 programs is 18,500 rows =
**9.6 s** for the work queue.

This bites **only on the frontend/PostgREST path** — which, per
`app/main.py:5–9`, is the primary CRUD path. The FastAPI path is `BYPASSRLS` and
never evaluates these predicates at all. So the fast path is the one nobody uses
for lists and the slow path is the one every screen uses.

### 1.4 Recommendation

Rewrite the reach predicates set-side. The semantics are identical by
construction — `can_reach_college(x)` is defined as membership in exactly the
union `my_college_ids()` returns.

```sql
-- new migration, e.g. 2100_reach_predicates_set_form.sql

create or replace function public.my_program_ids()
returns setof uuid language sql stable security definer set search_path = '' as $$
  select pr.id from public.programs pr
  where pr.college_id in (select public.my_college_ids());
$$;

create or replace function public.my_batch_ids() ...      -- via my_program_ids()
create or replace function public.my_deployment_ids() ... -- via my_batch_ids()
```

then replace the policy bodies:

```sql
-- before:  using (public.is_internal() and public.can_reach_program(program_id))
-- after:   using (public.is_internal() and program_id in (select public.my_program_ids()))
```

Each of the new helpers is **zero-argument**, so each is hoisted to a single
hashed subplan per statement. The `can_reach_*(uuid)` functions stay — the
FastAPI code path and ad-hoc SQL use them, and they are correct, just expensive
in a per-row position.

Do this in one migration covering all fifteen `can_reach_*` policies, and extend
`supabase/tests/02_rls_matrix_test.sql` with a before/after run so the persona
matrix proves the rewrite changed nothing but the plan. **Effort: 1–2 days**,
most of it re-running the matrix.

Expected result, extrapolating the measured 1.6 µs/row: the 100k-row query goes
from ~47 s to well under 200 ms.

### 1.5 What is *not* a problem here (checked)

The usual Supabase advice is to wrap policy function calls as
`(select public.is_internal())`. **That is unnecessary in this schema.** Measured:

```
is_internal() bare        →  One-Time Filter: is_internal()      9.0 ms / 20k rows
(select is_internal())    →  One-Time Filter: (InitPlan 1).col1  8.6 ms / 20k rows
```

The zero-argument helpers are already hoisted. Do not spend time on this.

Likewise `(select auth.uid())` is correctly used inside every helper body
(`0200_identity.sql` passim) — that part is right.

---

## 2. The two-wall architecture — an honest assessment

This is the most consequential design question in the codebase, and I want to be
clear up front: **the current design is defensible and is documented better than
most production systems ever manage.** `app/db/session.py:3–13`,
`app/core/security.py:1–49` and `supabase/migrations/README.md` all state the
hazard plainly, name the exact mechanism, and give the Python mirror of each SQL
helper. That is the good version of this decision.

### 2.1 What it actually costs

**Cost 1 — the wall is duplicated, and only one copy is tested.**
`supabase/tests/02_rls_matrix_test.sql` is 1,489 lines and asserts unscoped
zero-row reads per persona. That covers the SQL wall thoroughly and satisfies R5
for the frontend path. There is **no equivalent for the Python wall.** The
enforcement on the FastAPI path is `require_commercials()` +
`require_college_reach()` called by hand in each handler, and nothing structurally
asserts that a new handler calls them.

I wrote a 20-line AST check for this (§13) that flags any router handler
referencing `Pnl`/`RemunerationSheet`/`WorkOrder`/`TrainerBankAccount` without
both guards. It produced exactly one hit — `list_payout_queue` — and that hit was
a **false positive**: the guard is inside `_require_payout_persona()`
(`app/api/payouts.py:751–765`) and scope comes from `principal.college_ids`
directly, which is the correct pattern. So on the **API path** the wall holds
everywhere. The point is that it holds because humans have been careful.

The SQL side is a different story — see §2.4, which I got wrong on the first pass
and am correcting rather than quietly editing.

**Cost 2 — the column-guard triggers are inert on this connection.**
`supabase/migrations/README.md` calls this "the sharpest edge in this schema" and
it is right. `profiles_guard_privileged_columns` short-circuits on
`auth.uid() is null` (`0200_identity.sql`), which is exactly what a service-role
connection looks like. So `UPDATE profiles SET role = 'senior_manager'` from any
FastAPI code path succeeds silently. Nothing in the API does that today, but the
guard that would stop it is not a guard on this path.

**Cost 3 — over-fetching becomes invisible.** `list_payout_queue`
(`app/api/payouts.py:1230–1232`) loads `WorkOrder` and `RemunerationSheet` filtered
by `trainer_id` **only** — no program filter — so it pulls commercial rows for
programs outside the caller's reach into process memory. They are filtered
correctly before serialisation, so nothing leaks today. On an RLS connection this
over-fetch would have been impossible; on this one it is a code review away from
being a leak.

### 2.2 What it buys

Real things, which is why I am not calling it a defect:

- Finding #1 above is precisely the cost of RLS at scale. The BYPASSRLS path is
  ~290× cheaper per row, and the payout, report and monitoring endpoints do
  genuinely heavy multi-table work.
- Roll-ups across a manager's whole reach are a single query rather than a
  policy evaluation per candidate row.
- `rag_search()` can take the persona as parameters (see §12) which lets the
  service path pass a scope explicitly rather than depend on session state.

### 2.3 What it would take to make the API path enforce RLS

This is entirely feasible and the machinery already exists in this repo.
`supabase/tests/02_rls_matrix_test.sql:170–183` already impersonates a persona by
setting `request.jwt.claims` and `set role authenticated` — I used that same
incantation for every measurement in §1, so it is proven to work on this
project's connection.

The change is a wrapper on `get_session()`:

```python
async def get_rls_session(
    principal: CurrentPrincipal,
) -> AsyncIterator[AsyncSession]:
    factory = get_sessionmaker()
    async with factory() as session:
        await session.execute(
            text("select set_config('request.jwt.claims', :c, true)"),
            {"c": json.dumps({"sub": str(principal.user_id), "role": "authenticated"})},
        )
        await session.execute(text("set local role authenticated"))
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
```

Constraints, all real:

1. **`set local` requires a transaction.** The session must be inside an explicit
   `begin()` for the whole request, and `set local role` must be reset before the
   connection returns to the pool. SQLAlchemy's `AsyncSession` does begin lazily,
   so this needs an explicit `await session.begin()` and a pool `reset_on_return`
   audit. Get this wrong and a pooled connection keeps the role — that is a
   privilege leak in the wrong direction, and it is the reason to do this
   carefully rather than quickly.
2. **Port 5432 only.** `app/core/config.py:63–83` already refuses the transaction
   pooler for exactly this reason, so the guard rail is in place.
3. **Circularity.** `get_principal` itself reads `profiles` and the assignment
   tables. That resolution must stay on the BYPASSRLS session, then the scoped
   session is opened. Two sessions per request, or one session that switches role
   after resolution.
4. **Performance.** Adopting RLS on the API path inherits finding #1 in full. So
   **do §1 first.** Doing them in the other order would take the API path from
   fast-and-manually-guarded to slow-and-automatically-guarded, and the second
   trade is only good once the slowness is gone.

**My recommendation: do not do this now.** Do finding #1 first (it is required
either way, because the frontend path already pays it), then reconsider. In the
meantime, close the cheap half of the gap:

- **Add a structural test** that every router handler touching a commercial model
  calls both guards. The AST check in §13 is ~40 lines and already works; promote
  it into `tests/unit/` next to `test_agents_toolsets.py`, which does the same
  kind of structural enforcement for R3 and does it very well. **Effort: 2 h.**
  This is the single highest-value thing available for the two-wall design.
- Scope `list_payout_queue`'s `WorkOrder`/`RemunerationSheet` fetches by
  `program_id in (reachable programs)` as well as `trainer_id`. **Effort: 30 min.**

### 2.4 CORRECTION — four policies carry the wall without the scope

**I missed this, and then I got its severity wrong. Both are recorded here.**

I dumped all 54 policies (§13) and analysed the *handlers* for the both-conjuncts
rule but never analysed the *policies* for it. A concurrently-running security
review landed `tests/security/test_reach_conjunct_missing.py` and
`docs/security-findings.md` naming three; querying `pg_policies` across both
`public` and `storage` finds **four** — `erm_sync_tasks_sourcing_all` (1900) has
the same shape and is not on their list:

```
[ALL] public.trainer_bank_accounts.trainer_bank_accounts_commercials_all   (1400)
      using (can_see_commercials())                        -- account number + IFSC
[ALL] public.trainers.trainers_sourcing_all                                (0400)
      using (app_role() = any (array['senior_manager','manager']))  -- PAN, email, phone
[ALL] public.erm_sync_tasks.erm_sync_tasks_sourcing_all                    (1900)
      using (subject_kind = 'trainer' and app_role() = any (…))
[ALL] storage.objects.documents_commercials_trainer_rw                     (0900)
      using (… foldername[1] = 'trainers' and can_see_commercials())  -- signed WO PDFs
```

**Severity: see `docs/security-findings.md` (SEC-02 / SEC-03), not this section.**
My first pass called this "less alarming than it looks" and a trade-off needing an
owner decision. That was wrong, and wrong in the unsafe direction. The reasoning
was that the actor would be a legitimately-provisioned Manager, for whom org-wide
trainer-pipeline access is defensible (and is argued well at
`0400_trainers_deployments.sql:320–334`).

That premise does not hold, because **persona is self-assignable at signup**.
`0200_identity.sql`'s `handle_new_user` does:

```sql
requested_role := (new.raw_user_meta_data ->> 'role')::public.app_role;
...
insert into public.profiles (id, role, full_name)
values (new.id, coalesce(requested_role, 'trainer'), ...);
```

`raw_user_meta_data` is attacker-controlled at signup — the function's own comment
says so, one line away, while explaining why `is_admin` is *not* taken from it.
`role` is. So anyone who can reach the signup endpoint becomes a `manager`,
`can_see_commercials()` returns true, and the four policies above need no
assignment rows at all. The live database currently holds **1,026 trainers and
1,025 `trainer_bank_accounts` rows**, and all four policies are `for all`, so the
same gap is a cross-tenant **write** to payment rails.

I had `handle_new_user` in front of me and noted only that `is_admin` is safe. I
did not connect it to the policy shape. The security agent did, and their
chained finding is the correct reading.

**What remains mine here:**

1. **A fourth policy.** `erm_sync_tasks_sourcing_all` (1900) has the identical
   shape and is not in SEC-02's list of three. Whatever remediation lands must
   cover it, or the ERM card surface keeps the trainer subject readable and
   writable by any pipeline persona with no reach.
2. **One of the four is deliberate and should stay.**
   `0400_trainers_deployments.sql:320–334` argues that the trainer roster is
   genuinely not college-scoped ("sourcing happens before any deployment exists,
   so a policy that required reach would make it impossible to record the trainer
   you are about to deploy") and deliberately declines to reuse
   `can_see_commercials()` for it. That reasoning survives SEC-01 being fixed.
   The remediation for `trainers_sourcing_all` is therefore **fix the signup
   vector**, not add a reach conjunct that would break sourcing. The other three
   carry money — rails, WO rates, ERM payloads — and should be narrowed.
3. **The method gap.** The both-conjuncts check should run against `pg_policies`,
   not just handler source. Twelve lines, and it would have caught this in the
   same pass that (correctly) found nothing on the API side:

```sql
select schemaname, tablename, policyname from pg_policies
where (coalesce(qual,'') || coalesce(with_check,'')) like '%can_see_commercials%'
  and (coalesce(qual,'') || coalesce(with_check,'')) not like '%can_reach_%';
```

   Add it to the nightly DB workflow (§8.2) as an allow-listed assertion, so a
   *new* wall-without-scope policy fails loudly while any intentional ones stay
   listed with a justification beside them.

**This supersedes row 19 of the summary table's "TRADE-OFF" classification.** It is
a defect, it is the highest-severity item in this document, and remediation is
tracked in `docs/security-findings.md` rather than here to avoid two owners for
one fix.

---

## 3. Layering and import graph

### 3.1 `domain/` purity — clean

Verified by AST over all 87 modules under `app/`, not by eye. Complete list of
`app.domain` imports:

```
app.domain.attendance -> app.domain.enums
app.domain.payout     -> app.domain.enums, app.domain.money
app.domain.risk       -> app.domain.enums
VIOLATIONS: none
```

No imports of `db`, `api`, `agents`, `services` or `rag` from `domain/`, at
runtime, deferred, or under `TYPE_CHECKING`. **§3 holds.**

### 3.2 Module-level cycles — none

Depth-first colouring over the full 87-module graph: **zero cycles.**

### 3.3 Package-level cycles — two, both benign today

```
CYCLE: app.core -> app.db -> app.core
   app.core.audit:49,50    -> app.db.models, app.db.session
   app.core.security:69,76 -> app.db.models, app.db.session
   app.db.session:31       -> app.core.config

CYCLE: app.agents -> app.services -> app.agents
   app.agents.monitor:98–101            -> app.services.escalation.engine,
                                           app.services.monitoring.{alerts,scoring,signals}
   app.services.reporting.narration:68  -> app.agents.grounding
```

Neither can deadlock at import time, because the module-level graph is acyclic:
`core.audit → db.models → domain.enums` and `db.session → core.config` never
close a loop, and `agents.monitor → services.monitoring` and
`services.reporting.narration → agents.grounding` touch disjoint sub-packages.

The second one is the near-cycle another agent reported, and the code is honest
about it — `app/services/reporting/narration.py:53` says the `Completer` protocol
exists "to keep `app/services/` from depending on `app.agents.runtime`", and then
line 68 imports `app.agents.grounding` anyway.

**Classification: TRADE-OFF, not defect.** `assert_grounded` is a pure
string/number check with no agent dependencies; it is in `app/agents/` because
that is where it was first needed. The clean fix is to move it — it is R1
enforcement, not agent machinery, and it belongs in `app/domain/grounding.py`
where both `agents/` and `services/` can import it without a package cycle.
**Effort: 2 h** including moving `tests/unit/test_agents_grounding.py`.

The `core ↔ db` cycle is structural to the design (`core.security` needs models;
`db.session` needs config) and I would leave it. Splitting `core.config` into its
own leaf package would resolve it cosmetically without changing anything real.

---

## 4. Money handling (R2 / R6 / R7) — clean

I went looking for a violation and did not find one. Recording the checks so the
next reviewer does not repeat them.

**R7 — no float.** Every monetary column in the live database is
`numeric(14,2)`; `tds_rate` is `numeric(5,4)`; `payable_days` is `numeric(6,2)`.
Zero `double precision` or `real` columns anywhere in `public`. The only `float`
in the Python money path is a *rejection*: `app/domain/money.py:71–75` raises on
`float` rather than coercing, and `app/api/payouts.py:227` runs the same guard as
a Pydantic `BeforeValidator` so `{"ta_da": 100.50}` is a 422. Outbound amounts
are `str()`-serialised so a `Decimal` cannot become a JSON float. `float` appears
elsewhere only for embedding vectors (`app/db/models.py:939–1000`), which
`app/db/models.py:948` explicitly flags as the deliberate asymmetry.

**Multiply-before-divide.** `app/services/remuneration/engine.py:115`:

```python
earned = rate * days / divisor
```

Python evaluates left-to-right, so this is `(rate * days) / divisor`. Line 109
computes `rate_per_day = rate / divisor` **separately**, and `PayoutResult`
receives `quantize_paise(rate_per_day)` (line 76) which is never multiplied by
anything. The two are computed independently, which is exactly what §6 requires
and what the legacy sheet gets wrong.

**One rounding.** `round_rupees()` is called once, at `engine.py:84`, on
`net_unrounded`. `quantize_paise()` is used only on display values.
`app/domain/money.py:94` uses `ROUND_HALF_UP`, correctly rejecting Python's
default banker's rounding with a stated reason.

**TDS base.** `engine.py:69`: `tds = earned * payout.tds_rate` — `earned`, not
`gross`. This is what produces 1,548 rather than 1,558 on the VEMA fixture.

**Both §6 regression fixtures pass** (`tests/unit/test_engine.py`, part of the
1,451 passing tests).

**One thing worth knowing, not a defect.** Money columns are `numeric(14,2)`, so
`earned` is quantised to paise on write while `net_amount` is the already-rounded
value. Any future code that recomputes net from stored `gross - tds` can differ
by up to ₹0.01 before rounding. It does not today — `_replay`
(`app/api/payouts.py:1634`) recomputes from source, not from stored columns.
Worth a comment on the model so it stays that way.

---

## 5. Async correctness

### 5.1 DEFECT — `LLMClient` per request, never closed

`app/api/copilot.py:84` and `app/api/reports.py:173` each construct a fresh
`LLMClient()` inside a FastAPI dependency. `LLMClient.__init__`
(`app/core/llm.py:143`) constructs `AsyncOpenAI(...)`, which constructs an
`httpx.AsyncClient` with its own connection pool. Neither is cached, and neither
is ever `aclose()`d.

Consequences: a fresh TLS handshake to `openrouter.ai` on every copilot question
and every report (~100–300 ms of pure latency), and a connection pool whose
sockets are reclaimed only when the garbage collector runs the client's
finaliser. Under any real concurrency that is a file-descriptor leak.

`get_copilot`'s docstring correctly explains why the *retriever* must be
per-request (it holds the request's session). That reasoning does not extend to
the LLM client.

**Fix:** build one `LLMClient` in `lifespan` (`app/main.py:97`), store it on
`app.state`, hand it out via `Depends`, and `await client.close()` in the
shutdown half — beside the existing `dispose_engine()`. **Effort: 1 h.**

### 5.2 DEFECT — N+1 awaits

**`app/api/erm.py:806–815`** — for each queue row, `_authorise()` issues a query
(`_trainer_college_ids` or `_program`) and `_subject_label()` issues another
(`session.get(Trainer|Program, …)`). `limit` defaults to 50 and `MAX_PAGE` is 200
(`app/api/erm.py:116,778`), so a full page is **up to 400 sequential round-trips**.

**`app/api/reports.py:928–931`** — `college_summary` calls `_delivery_inputs()`
once per program, and `_delivery_inputs` issues ~7 sequential queries
(`app/api/reports.py:534–602`). A college with 10 programs is ~70 sequential
round-trips.

Measured round-trip to the configured Supabase host from this machine: **median
102 ms** (min 69.5, max 248.9). At that latency the ERM queue takes ~40 s and the
college summary ~7 s. Colocated in the same region you would expect 2–5 ms, so
0.8–2 s and 0.15–0.35 s — still bad for the ERM queue, tolerable for the summary.

**Fix:** batch. In `erm.py`, collect the distinct trainer and program ids from
the page, issue two `IN` queries, then authorise and label from the two dicts —
2 queries total instead of 2N. In `reports.py`, hoist `_delivery_inputs` to take
a list of programs and issue one `IN` query per entity type. **Effort: 3–4 h.**

### 5.3 Checked and fine

- Every route handler in `app/api/` is `async def`. No sync handlers.
- No `time.sleep`, `requests`, or blocking file I/O inside a handler.
- `openpyxl` (blocking, CPU-bound) is called from `generate_remuneration_sheet`
  and `generate_invoice_sheet`. It writes to an in-memory `io.BytesIO`
  (`app/api/payouts.py:1810,1851`) for a single trainer-month — tens of rows. Not
  worth `run_in_threadpool` today. Revisit if a bulk "whole month" export lands.
- `get_session()` rolls back on exception (`app/db/session.py:83–85`), so a
  failed 37-row task generation leaves nothing behind, as documented.
- `run_api.py`'s selector-loop workaround for psycopg on Windows is correct and
  well-argued; the refusal to combine it with `--reload` is the right call.

### 5.4 TRADE-OFF — no resource limits anywhere

`create_async_engine` (`app/db/session.py:53–59`) sets only `pool_pre_ping=True`.
That leaves SQLAlchemy's defaults: `pool_size=5`, `max_overflow=10`, so **15
concurrent DB operations maximum**, and `pool_timeout=30` — request 16 waits 30 s
then raises. Nothing sets `pool_recycle`, and nothing sets a `statement_timeout`,
so a query caught by finding #1 can hold a pooled connection for 47 s.

Similarly, `AsyncOpenAI` is constructed with no `timeout` (SDK default: **600 s**)
and no `max_retries` (SDK default: **2**). `app/core/llm.py:101–105` states "no
retry policy" — the SDK retries twice regardless. That is a docstring that
disagrees with the behaviour.

**Fix, all in two files:**

```python
create_async_engine(
    _async_url(),
    pool_pre_ping=True,
    pool_size=10, max_overflow=20, pool_recycle=1800,
    connect_args={"options": "-c statement_timeout=30000"},
    future=True,
)
AsyncOpenAI(..., timeout=60.0, max_retries=2)   # and fix the docstring
```

Sizing needs §14 Q7 (peak concurrency) answered; until then the numbers above are
a reasonable placeholder that is strictly better than the implicit defaults.
**Effort: 1 h.**

---

## 6. Correctness defects found by execution

### 6.1 DEFECT (high) — `/reports/*` is dead in a Phase-1 deployment

CLAUDE.md §13 states Phase 1 has no AI, and `.env.example` documents the
OpenRouter variables as optional for exactly that reason. `app/core/config.py:56`
makes them optional. `get_narrator()` (`app/api/reports.py:160–182`) turns a
missing configuration into a 503 whose message reads:

> "Request the report without `include_narrative` for the facts alone."

That advice does not work. `narrator: Narrator` is a plain dependency parameter on
`draft_governance` (line 759), `program_feedback` (853) and `college_summary`
(909). FastAPI resolves **all** dependencies before the handler body runs, so
`get_narrator()` raises before anything reads `include_narrative`.

Executed against a `Settings` with no OpenRouter values:

```
GET /reports/colleges/{id}/summary?...&include_narrative=false  ->  503
{'detail': 'Report narration is not configured in this environment: LLMClient
 requires OPENROUTER_API_KEY, OPENROUTER_MODEL_VOLUME, OPENROUTER_MODEL_FRONTIER…'}
```

**Every reports endpoint is 503 in the deployment shape CLAUDE.md calls the
default.** The frontend has `isNarrationUnavailable()`
(`frontend/src/lib/reports.ts`) to handle 503 gracefully, so a user sees "not
configured" where the facts should be.

Why no test caught it: `tests/unit/test_api_reports.py:221` overrides
`reports.get_narrator` in **every** test. The real dependency is never exercised.

**Fix:** inject a factory, not an instance.

```python
def get_narrator_factory() -> Callable[[], ReportNarrator]:
    return ReportNarrator.build          # raises only when called

# in the handler:
narration = await _narrate(make_narrator().narrate_college_summary(summary)) \
            if include_narrative else None
```

Then add one test that hits each endpoint with `include_narrative=false` and no
OpenRouter config, asserting 200. **Effort: 30 min.**

### 6.2 DEFECT (high) — `2000_last_admin_guard.sql` is not applied

```
supabase_migrations.schema_migrations:  19 rows
supabase/migrations/*.sql on disk:      20 files
2000_last_admin_guard.sql               ** NOT APPLIED **
```

Confirmed in the database itself, not just the ledger:

```sql
select proname from pg_proc where proname like '%last_admin%';   -- []
select tgname from pg_trigger where tgrelid='public.profiles'::regclass and not tgisinternal;
-- [profiles_guard_privileged_columns, profiles_set_updated_at]   -- no constraint triggers
```

`supabase/migrations/README.md` describes this migration as the response to a
real incident — "the 2026-08-15 self-demotion came in on exactly that connection"
— and notes it deliberately has no `auth.uid() is null` escape hatch so that it
also binds the service-role connection. The live database has **one** admin
(`a07dd66e…`, `senior_manager`, `is_admin=true`). One `UPDATE` on the BYPASSRLS
connection removes the only account that can grant reach, and nothing stops it.

The guard has a test — `tests/rls/test_last_admin_guard.py` — but it carries the
`rls` marker and CI runs `pytest -m "not integration and not rls"`, so no
automation would have noticed.

**Fix:** `python supabase/tests/run_tests.py --apply-only`, then re-run the
persona matrix. **Effort: 15 min** plus verification.

### 6.3 DEFECT (high) — the test suite loads the real `.env` into the process

**Two** conftests now do this, and the pattern is spreading:

- `tests/rag_eval/conftest.py:54–72` — `_load_dotenv()` called at line 72,
  `os.environ.setdefault()` per key.
- `tests/api_contract/conftest.py:45–53` — `_load_env()` called at line 53,
  `load_dotenv(REPO_ROOT/.env)`.

Both run at **module import time**. Because `testpaths = ["tests"]`, pytest
imports both during collection for **every** run, including CI's
`-m "not integration and not rls"`.

Observed effect on this machine (second run, after the concurrent suites landed):

```
7 failed, 1516 passed, 99 skipped, 53 deselected, 21 xfailed
FAILED tests/rag_eval/test_logging.py::test_the_prompt_is_emitted_at_debug_level
FAILED tests/rag_eval/test_logging.py::test_what_is_logged_is_the_question_and_not_the_assembled_prompt
FAILED tests/unit/test_llm.py::test_missing_setting_is_named[env0-OPENROUTER_API_KEY]
FAILED tests/unit/test_llm.py::test_missing_setting_is_named[env1-OPENROUTER_MODEL_VOLUME]
FAILED tests/unit/test_llm.py::test_missing_setting_is_named[env2-OPENROUTER_MODEL_FRONTIER]
FAILED tests/unit/test_llm.py::test_guard_lists_all_missing_at_once
FAILED tests/unit/test_llm.py::test_phase_one_settings_build_without_any_llm_config
```

An earlier run, before those suites existed, was `5 failed, 1451 passed`. The two
new failures are in the same family.

`pytest tests/unit/test_llm.py` alone: **12 passed.** The tests are correct — they
use `Settings(_env_file=None, …)` and `tests/unit/test_llm.py:35–40` explains
precisely why. They are defeated by an environment variable that a sibling
conftest injected into the process.

The broken tests are the small half of the problem. The large half is that after
that import, **`os.environ["DATABASE_URL"]` is the production Supabase URL and
`os.environ["SUPABASE_SERVICE_ROLE_KEY"]` is the live service-role key**, for the
whole suite. Any test that constructs an engine from the environment connects to
production. Today none does; that is a property of current test code, not of the
harness.

It also fails in the worst direction: CI has no `.env`, so `_load_dotenv()`
returns early and CI is green while every developer with credentials is red.

Note: `tests/rag_eval/`, `tests/security/` and `tests/api_contract/` were all
created during this session by concurrent agents — this is fresh work, not
long-standing debt. That is also why it is worth fixing **now**: the pattern has
already been copied once, and a third suite will copy it again.

**Fix:** move the load into the fixture that needs it, gated on the `RAG_EVAL_LIVE`
opt-in that file already defines at line 282:

```python
@pytest.fixture(scope="session")
def rag_eval_env() -> None:
    if not _live(): pytest.skip("RAG_EVAL_LIVE is not set")
    _load_dotenv()
```

and delete the module-level call. Apply the same shape to
`tests/api_contract/conftest.py`, whose `DSN`/`JWT_SECRET` module constants and
`pytestmark = skipif(...)` currently depend on the import-time load — those need
to become fixture-time lookups. **Effort: 1 h.**

Minor, same area: `tests/api_contract` and `tests/security` use a `contract`
marker that is not registered in `pyproject.toml`'s `markers` list, so pytest
emits `PytestUnknownMarkWarning` and `--strict-markers` would fail the run. Add
it beside `integration` and `rls`. Also note `contract` is **not** in CI's
`-m "not integration and not rls"` exclusion, so those tests do run in CI — they
skip cleanly on the absent `DATABASE_URL`, which is the right behaviour, but it
is worth being deliberate about.

### 6.4 DEFECT (medium) — `POST /payouts/commit` idempotency is check-then-act

`app/api/payouts.py:1546–1600` reads `_committed_sheet(resolved)`, and if it is
`None` computes `_next_invoice_seq()` and inserts. `_next_invoice_seq`'s own
docstring (line 917–925) is candid: "scanning for a free sequence in Python races
under concurrent runs; the database's unique index is the real arbiter, and this
call reserves nothing."

That is correct about safety and wrong about the outcome. Two concurrent commits
for the same trainer-period both see `None`, both insert, and the second violates
`remuneration_sheets_unique_period`. Nothing in the module catches
`IntegrityError` (grep: zero occurrences), so SQLAlchemy raises, `get_session()`
rolls back, and FastAPI returns **500**. The documented contract for exactly this
case — "a retried request, a double-clicked button" — is a 200 replay.

The same applies to the invoice sequence across different periods: one trainer,
two programs, same month → both compute `seq = 1` → `(pan, fy, month, seq)`
unique index → 500.

Data integrity is fine. The error surface is wrong for the most likely trigger.

**Fix:** wrap the insert, and on `IntegrityError` roll back, re-read, and route
into the existing `_replay()` path:

```python
try:
    await session.commit()
except IntegrityError:
    await session.rollback()
    existing = _committed_sheet(await _refetch(session, resolved))
    if existing is not None:
        return await _replay(session, resolved, report, existing, response)
    raise HTTPException(409, "…invoice sequence taken concurrently; retry")
```

**Effort: 1 h** including a concurrency test.

---

## 7. Frontend

### 7.1 DEFECT (critical) — no query is bounded

Across `frontend/src/`: **111 PostgREST calls, 0 uses of `.limit()` or
`.range()`.** Every list screen fetches everything the caller can reach.
Supabase does not cap this server-side by default.

Examples: `TrainersPage.tsx:99` (`trainers`, all), `CollegesPage.tsx:62`
(`colleges`, all), `AttendancePage.tsx:180` (`trainer_attendance`),
`ProgramDetail.tsx:102` (`profiles`, all).

On its own this is a growth problem. Combined with finding #1 it is the
product's scalability ceiling: `trainer_attendance` unbounded × 465.6 µs/row of
RLS predicate is the 47-second query in §1.3.

**Fix:** add `.range(from, to)` with a page size to every list query and a
"load more" or paginated table in the UI. Start with the four highest-growth
tables: `trainer_attendance`, `tasks`, `attendance_records`, `students`.
**Effort: 1–2 days** across the pages. Do it together with §1 — either alone
leaves the ceiling in place.

### 7.2 TRADE-OFF — code splitting, quantified

I reproduced the build in an isolated copy and measured the alternative rather
than estimating it.

**Baseline (current):**
```
index.css     44.39 kB │ gzip:   8.33 kB
index.js     801.88 kB │ gzip: 225.88 kB     ← one chunk, 168 modules
(!) Some chunks are larger than 500 kB after minification.
```

**With `React.lazy` on the 17 ops routes + the two persona roots** (no other
change):
```
index.js     499.98 kB │ gzip: 147.04 kB     ← shared/vendor
HomePage      19.77 kB │ gzip:   6.36 kB
OpsRoot        5.62 kB │ gzip:   1.61 kB
AppShell       5.74 kB │ gzip:   1.85 kB
useQuery       8.84 kB │ gzip:   3.21 kB
…
CommsPage     44.73 kB │ gzip:  13.64 kB     ← no longer on the critical path
ReportsPage   27.56 kB │ gzip:   8.31 kB
ErmSyncPage   23.80 kB │ gzip:   8.34 kB
PayoutsPage   23.64 kB │ gzip:   8.39 kB
```

First paint for a Manager landing on Home:
`147.04 + 1.61 + 1.85 + 3.21 + 0.91 + 0.84 + 0.52 + 1.40 + 6.36` ≈ **163.7 kB
gzip**, against **225.88 kB** today.

> **Measured win: 62 kB gzip, 27.5% off first load.** Each subsequent route
> costs 2–14 kB gzip, fetched on navigation.

The larger structural win is that `CommsPage` (44.73 kB, `1,674` lines) and
`PayoutsPage` (8.39 kB gzip) stop being downloaded by people who never open them
— an LDE Executive cannot even use `/payouts`.

**Where the remaining 147 kB gzip goes** (measured with `manualChunks`):

```
v-supabase   216.82 kB │ gzip: 57.10 kB
v-react      192.35 kB │ gzip: 60.29 kB
v-router      37.12 kB │ gzip: 13.39 kB
v-query       35.89 kB │ gzip: 10.60 kB
app entry     27.55 kB │ gzip:  9.55 kB
```

Vendor is 482 kB / ~141 kB gzip — **62% of the current bundle** and mostly
irreducible. One note: `grep -rn "realtime\|\.channel(" frontend/src` returns
**nothing**, so the realtime client inside `@supabase/supabase-js` is dead weight
in the largest dependency. Worth an experiment with a narrower import if the
first-load budget ever matters more than it does now.

**Recommendation:** do the route split (**2 h**, mechanical, measured above).
Splitting `CommsPage.tsx` (1,674 lines) and `ReportsPage.tsx` (1,112 lines) into
components is a **maintainability** argument, not a bundle-size one — the split
above already gets the size win without touching them. I would do it, but as
refactoring, not as performance work.

### 7.3 DEFECT (low, latent) — one more unkeyed parameter

`fetchCommsQueue(programId, state?, limit = 200)` sends `limit`
(`frontend/src/lib/comms.ts:493–497`). `commsKeys.queue(programId, state)`
(`comms.ts:659–660`) does not key it. That is precisely the class of bug already
found and fixed in `erm.ts` — and `erm.ts:512–517` now carries a comment
explaining it, so the lesson is recorded but was not applied next door.

Currently latent: the only call site (`CommsPage.tsx:155`) never passes `limit`.
One line to close:

```ts
queue: (programId: string, state: ArtifactState | null, limit = 200) =>
  ['comms', 'queue', programId, state ?? 'all', limit] as const,
```

**Effort: 5 min.** I checked the rest and found no others: `alerts.ts:143` keys
the whole params object (TanStack hashes it deterministically, and `undefined`
values are dropped on both the key and the request side, so they agree);
`reports.ts:313–317` carries period **and** `narrative`; `copilot.ts` has no
parameterised query; `payouts` keys on a fingerprint of the claim amounts, which
`queryKeys.ts:86–96` argues correctly.

### 7.4 Checked and fine

- `queryClient.clear()` fires on sign-out (`AuthProvider.tsx:88–91`) with the
  right reason stated. Residual edge: a `SIGNED_IN` event with a *different*
  `user.id` (no intervening sign-out) does not clear. Not reachable through this
  UI. Hardening: compare `next.user.id` to the previous one and clear on change.
- `frontend/.env.local` carries `VITE_SUPABASE_URL`, `VITE_SUPABASE_ANON_KEY`,
  `VITE_API_BASE_URL` and nothing else. No service-role key reaches the client.
- `App.tsx:28–38` and `OpsRoot.tsx:103–117` are explicit that the persona nav
  gating is cosmetic and the wall is in Postgres. That is the correct framing and
  it is written down where someone will read it.

---

## 8. Operational readiness

### 8.1 DEFECT — CI does not deliver what its comment promises

`.github/workflows/ci.yml:42–45`:

```yaml
- name: Install
  run: |
    pip install --require-hashes -r requirements-dev.txt
    pip install -e . --no-deps
```

The comment above it (lines 37–38) claims `--require-hashes` means "a tampered or
substituted artifact fails the install rather than running." The very next line
breaks that. `pip install -e .` triggers **PEP 517 build isolation**: pip creates
a fresh environment and downloads the build backend named in
`pyproject.toml`'s `[build-system].requires` — `setuptools>=68` — from PyPI,
**unpinned and unhashed**, on every run. `grep -i "^setuptools\|^wheel" requirements-dev.txt`
returns nothing, so it is not in the lock.

The build backend is the one dependency that executes arbitrary code during
installation. It is currently the only unverified one.

**Fix:** add `setuptools` and `wheel` to the dev extra (so `uv pip compile` locks
and hashes them) and pass `--no-build-isolation`:

```yaml
pip install --require-hashes -r requirements-dev.txt
pip install -e . --no-deps --no-build-isolation
```

**Effort: 15 min.**

### 8.2 DEFECT — three test suites that no automation runs

1. **The frontend.** `frontend/package.json` defines `test` (vitest), `typecheck`
   and `build`. CI runs none of them. There are six test files —
   `alerts.test.ts`, `comms.test.ts`, `copilot.test.ts`, `erm.test.ts`,
   `reports.test.ts`, `supabase.test.ts` (~1,470 lines) — that never execute in
   CI. Note that `erm.test.ts` is where the fixed cache-key bug lives; the
   regression test for it is not run by anything.
2. **The RLS persona matrix.** `supabase/tests/02_rls_matrix_test.sql` is 1,489
   lines and is the *entire* evidence for R5 — "Row-level security is tested, not
   assumed". `ci.yml:63–66` says these "run against a real Supabase project in a
   separate workflow, not here." `.github/workflows/` contains one file. There is
   no separate workflow. Finding 6.2 (an unapplied migration whose guard has a
   test) is what that gap looks like in practice.
3. **`tests/integration/`** contains only `__init__.py`.

**Fix:**
- Add a `frontend` job: `npm ci && npm run typecheck && npm run test && npm run build`.
  **Effort: 30 min.** Consider failing the build on a chunk over ~250 kB gzip
  once §7.2 lands.
- Add a scheduled (nightly) or manually-dispatched workflow running
  `python supabase/tests/run_tests.py` against a dedicated Supabase project, with
  `DATABASE_URL` as a secret. It rolls back unconditionally
  (`run_tests.py:18–22`), so it is safe. **Effort: 1–2 h**, mostly provisioning.

### 8.3 DEFECT — the migration ledger records nothing verifiable

```sql
select version, name, statements from supabase_migrations.schema_migrations limit 3;
 0100 | enums_and_utils | statements = None
 0200 | identity        | statements = None
 0300 | org_core        | statements = None
```

`supabase/tests/run_tests.py:267–271` inserts `(version, name)` only. The Supabase
CLI populates `statements`. Consequences:

- `supabase/migrations/README.md`'s rule "**Never edit a shipped migration**" is
  policy with no enforcement. Editing `0700_finance.sql` today changes the file,
  changes nothing in the database, and leaves no evidence anywhere.
- The claim that "a later `supabase db push` agrees with this script" holds only
  for *which* versions are applied, not for *what* they contained.

**Fix:** record a `sha256` of each file at apply time (an extra column, or reuse
`statements` with the split statement list the CLI uses), and have
`run_tests.py --apply-only` verify the hash of every already-applied file before
applying anything new — failing loudly on a mismatch. **Effort: 1–2 h.** This is
the cheapest real integrity control available for a hand-authored migration
system.

### 8.4 DEFECT — §11's schema drift test does not exist

CLAUDE.md §11: "SQLAlchemy models in `db/` are a typed mapping layer that mirrors
the schema and never generates it; **a test diffs them against
`information_schema` to catch drift.**"

No such test exists. `grep -rln information_schema tests/` returns
`tests/unit/test_audit.py`, whose line 10 says the full version "needs a live
[database]" — an acknowledged gap.

I wrote the test as a 20-line script (§13) and ran it. It found drift immediately:

```
Base.metadata: 32 tables | db public tables: 32
  programs: in DB but UNMAPPED: ['erm_external_id', 'erm_status',
                                 'erm_synced_at', 'erm_synced_by', 'erm_url']
mismatches: 1
```

To the team's considerable credit, this is already self-reported:
`app/services/erm/models.py:1–30` names the debt precisely, explains that
`app/db/models.py` was closed to that workstream, and predicts that a future
drift test "will find the `programs.erm_*` columns unmapped, and that finding is
correct: it is this workstream's debt, recorded here rather than discovered."

That is exemplary. It is also an argument for writing the test: the debt is only
visible because one careful author wrote a docstring, and that does not scale.

**Fix:** promote the script into `tests/integration/test_schema_drift.py` with the
`integration` marker, run it in the nightly DB workflow from §8.2. Then map the
five columns and delete the docstring. **Effort: 2 h.**

### 8.5 Secrets and logging — fine

- `.gitignore` covers `.env`, `.env.*` (matching `frontend/.env.local` by
  basename), `reference/`, `frontend/dist/`. `git ls-files` returns nothing —
  the repo has no tracked files yet, so nothing is committed.
- `.env.example` carries names only, with genuinely useful commentary.
- `app/core/config.py:63–83` rejects the transaction pooler with a stated reason.
  Good — that same property is what makes the §2.3 migration feasible.
- `app/core/security.py` fails **closed** on a missing JWT secret (500, not 401),
  checks `alg` against an allow-list *before* verification (the `alg: none`
  defence), returns one indistinguishable 401 for every failure mode, and
  re-reads persona from the database on every request rather than trusting the
  token. This module is the strongest code in the repo.
- `LOG_LEVEL` guidance in `.env.example` correctly warns that DEBUG writes
  prompts containing PAN and bank details to the log store, and
  `app/core/llm.py:199–201` implements exactly that split.

### 8.6 Audit and agent-I/O logging — both satisfied

§11 requires an `AuditEvent` per state transition. Verified by reading every
mutating handler:

- R4 transitions use `write_within()` — atomic with the state change, raises on
  failure: `approvals.py:508`, `comms.py:508`, `comms.py:966`, `erm.py:652`,
  `payouts.py:1586` (commit). This matches `app/core/audit.py`'s own rule
  ("if losing the audit row would leave a money or approval decision
  unattributable, use `write_within()`") exactly.
- Best-effort `write()` is used for non-transitions: validation
  (`payouts.py:1440`), sheet generation (`payouts.py:1813,1854`), task/document
  generation (`programs.py:212,325`).
- One borderline case: `reports.py:836` uses best-effort `write()` for
  `draft.event`, which is the creation of an artifact in DRAFT — arguably a
  transition. It persists nothing to `artifact_versions`, so nothing becomes
  unattributable. I would leave it and revisit if governance reports start
  persisting.

§11's agent I/O requirement — "prompt, tools called, tokens, latency, for every
invocation" — is met at `app/core/llm.py:203–210` and `app/agents/runtime.py:270`.
Note it is **log-only**; there is no database record of an agent invocation. For
Phase 4+ that is worth revisiting, because a log store is not a system of record.

---

## 9. Remaining database findings

### 9.1 DEFECT (low) — duplicate index on the highest-growth table

`supabase/migrations/0600_monitoring.sql:165` creates
`constraint trainer_attendance_unique_day unique (deployment_id, mark_date)`,
which builds a unique btree on those columns. Line 175 then creates
`trainer_attendance_deployment_idx on public.trainer_attendance (deployment_id, mark_date)`
— the same columns, same order.

Confirmed in `pg_index`: two indexes, identical `indkey`. Every insert into the
fastest-growing table in the schema maintains both. Drop
`trainer_attendance_deployment_idx` in a new migration. **Effort: 15 min.**

### 9.2 DEFECT (low) — 16 foreign keys with no covering index

Full list (leading column of the FK not the leading column of any index):

```
attendance_records.student_id            -> students          ← matters
remuneration_sheets.work_order_id        -> work_orders       ← matters
program_documents.document_template_id   -> document_templates
tasks.template_id                        -> task_templates
observations.observer_id                 -> profiles
trainer_attendance.marked_by             -> profiles
governance_reports.shared_by             -> profiles
program_documents.owner_id               -> profiles
programs.erm_synced_by                   -> profiles
trainers.erm_synced_by                   -> profiles
user_college_assignments.assigned_by     -> profiles
user_cluster_assignments.assigned_by     -> profiles
erm_sync_tasks.{assigned_by,cancelled_by,confirmed_by,created_by} -> profiles
```

The twelve `*_by -> profiles` ones are low consequence: they cost a sequential
scan of the child table when a profile row is deleted, and profile deletion is
rare. The first two matter — `attendance_records` is high-volume and
`student_id`-keyed deletes/joins will degrade.

Add indexes for `attendance_records.student_id` and
`remuneration_sheets.work_order_id`; leave the rest and record the decision.
**Effort: 30 min.**

### 9.3 Structural risk — the RAG chunk policy will not scale

`rag_chunks_read` is the most expensive policy in the schema:

```sql
using (is_internal()
   and (can_see_commercials() or not rag_chunk_is_commercial(id))
   and can_reach_rag_document(document_id)                     -- 3 levels deep
   and exists (select 1 from rag_documents d
               where d.id = rag_chunks.document_id
                 and can_read_corpus(d.corpus)))               -- 156 µs/call
```

`can_reach_rag_document` nests to `can_reach_program` → `can_reach_college`. On a
KNN query (`order by embedding <=> q limit k`) the HNSW index cannot push these
down, so Postgres post-filters index candidates — which either returns fewer than
`k` rows or degenerates to a scan.

**This is not live today.** The frontend reaches RAG only through
`POST /copilot/ask` (FastAPI, BYPASSRLS), and `public.rag_search()` carries its
own explicit WHERE clause that costs ~nothing. But `authenticated` holds EXECUTE
on `rag_search`, so the moment any screen calls it directly, this policy engages
on the browser path.

Fold this into the §1 rewrite (`document_id in (select public.my_rag_document_ids())`)
rather than treating it separately.

---

## 10. Things I looked for and did not find

Recording negatives so the next reviewer can skip them.

- **A float in the money path.** None. See §4.
- **An LLM computing money.** None. `app/services/remuneration/engine.py` imports
  nothing from `app.core.llm`; `tools/rule_linter.py:259` enforces it in CI.
- **A send-capable agent tool.** None, and R3 is enforced five independent ways
  (`tests/unit/test_agents_toolsets.py:1–24`): closed set of tool *effects*, a
  toolset type with no field able to hold a callable, port protocols with no
  sending method, a dispatcher that refuses out-of-toolset calls at runtime, a
  registry total over `AgentName`, and — last and explicitly weakest — a name
  check. This is the best-engineered rule in the repo.
- **A privilege-widening RPC.** `public.rag_search()` takes `p_is_internal`,
  `p_can_see_commercials`, `p_role` and `p_college_ids` as caller-supplied
  parameters and `authenticated` holds EXECUTE. That would be a full bypass if
  the function were `SECURITY DEFINER`. It is **not** (`prosecdef = false`), so
  RLS on `rag_chunks`/`rag_documents` still runs underneath and the parameters can
  only narrow. Correct by construction — but this is one `create or replace …
  security definer` away from being a complete authorisation bypass, so it is
  worth a comment in `1600_rag_corpora.sql` saying so.
- **A commercials *endpoint* missing the wall.** AST-checked all router handlers
  touching `Pnl`/`RemunerationSheet`/`WorkOrder`/`TrainerBankAccount`; the single
  hit was a false positive (§2.1). **But I did not run the equivalent check on the
  *policies*, and should have — see §2.4.**
- **`Base.metadata.create_all()` anywhere.** None, in app or tests. The prohibition
  in `app/db/models.py` is honoured.
- **Cache poisoning across personas.** `queryClient.clear()` on sign-out.

---

## 11. Suggested order of work

Sequencing matters here, because several items are prerequisites for others.

**This week**
1. Apply `2000_last_admin_guard.sql` (§6.2) — 15 min, one production risk closed.
2. Fix the reports narrator dependency (§6.1) — 30 min, one whole feature area.
3. Fix `tests/rag_eval/conftest.py` (§6.3) — 1 h, unbreaks local test runs and
   removes production credentials from the test process.
4. CI: `--no-build-isolation` + a frontend job (§8.1, §8.2) — 1 h.
5. `commsKeys.queue` limit (§7.3) — 5 min.
6. **Before anything else on this list**: `docs/security-findings.md` SEC-01
   (persona is a signup form field) and SEC-02/03. My §2.4 adds a fourth policy —
   `erm_sync_tasks_sourcing_all` (1900) — that their remediation list omits.

**Next two weeks**
7. **The RLS set-form rewrite (§1)** — 1–2 days. Everything else in the
   performance story is downstream of this.
8. Frontend pagination (§7.1) — 1–2 days, together with 7.
9. Route-level code splitting (§7.2) — 2 h, measured 27% first-load win.
10. LLM client lifecycle + pool/timeout config (§5.1, §5.4) — 2 h.
11. Structural test that commercial handlers call both guards (§2.3), **plus the
    `pg_policies` both-conjuncts query in §2.4** — 2 h. This
    is the highest-value single test available for the two-wall design.

**Backlog**
12. N+1 batching in `erm.py` and `reports.py` (§5.2).
13. `commit_payout` IntegrityError → replay (§6.4).
14. Schema drift test + migration ledger hashing (§8.4, §8.3).
15. Nightly RLS workflow (§8.2).
16. Move `assert_grounded` to `app/domain/` to close the package cycle (§3.3).
17. Index cleanup (§9.1, §9.2).

---

## 12. Closing assessment

The parts of this system that carry **money** are, as far as I could determine by
execution rather than reading, correct. The parts that carry **permission** are
not: a concurrent security review found that persona is self-assignable at signup
and that four policies grant the trainer pipeline without a reach conjunct — a
chain I had the evidence for and did not assemble (§2.4). Read
`docs/security-findings.md` first; this document assumes those are fixed. The payout engine
reconciles to the rupee on both §6 fixtures, uses `Decimal` end to end, multiplies
before dividing, and rounds exactly once. The commercials wall is applied correctly on the API
path; its *scope* conjunct is missing from four trainer-pipeline policies, which
is the finding in §2.4. R3 is enforced structurally by five independent mechanisms rather than by
a prompt. The JWT verifier fails closed and defends against algorithm confusion.
1,516 tests pass and the seven that fail are an environment leak, not a logic
error.

The remaining problems — setting SEC-01/02/03 aside — are largely in the
**operational envelope** rather than the domain logic: a migration that was written and never applied, a dependency that
turns a whole feature off in the deployment shape the docs call default, a
conftest that leaks production credentials into the test process, a CI pipeline
that verifies less than its comments claim, and — most importantly — a permission
model whose per-row cost was reasoned about carefully and measured never.

That last one is the finding I would act on first after the four one-hour fixes.
The reasoning at `0300_org_core.sql:266` is exactly the kind of thoughtful
argument this codebase is full of, and it happens to be wrong by a factor of 290.
It is worth noting that the argument was *written down*, in the migration, next to
the code — which is the only reason it was checkable at all.

---

## 13. Method

All measurements were taken on 2026-08-19 against the live Supabase project named
by `DATABASE_URL`, on a **read-only psycopg connection** (`conn.read_only = True`),
inside transactions that were rolled back. No schema, data, or configuration was
modified. Nothing outside `docs/architecture-review.md` was written in the repo.

- **Import graph**: AST walk over all 87 modules under `app/`, recording runtime,
  function-local and `TYPE_CHECKING` imports separately; cycles by DFS colouring
  at module level and at package depths 2 and 3.
- **RLS cost**: `explain (analyze, timing on, costs off)` over
  `generate_series(1, N)` with the helper's argument made row-dependent via a
  `case` expression so the planner cannot hoist it, impersonating a real
  Senior Manager with `set_config('request.jwt.claims', …, true)` +
  `set local role authenticated` — the same mechanism
  `supabase/tests/02_rls_matrix_test.sql:170–183` uses. N = 5,000–20,000
  depending on per-call cost. Baseline subtracted.
- **Round-trip latency**: 20 × `select 1`, median reported. Note this is measured
  from a developer machine in India against the project host and will be much
  lower from a colocated deployment; extrapolations state both.
- **Frontend bundle**: the repo's `frontend/` copied to a scratch directory with
  `node_modules` junctioned, then built three ways with the project's own Vite 7
  — baseline (reproduced 801.88 kB / 225.88 kB gzip exactly), route-split via
  `React.lazy`, and vendor-split via `manualChunks`. The repo's own
  `frontend/dist/` was not touched.
- **Reports 503**: executed via `fastapi.testclient.TestClient` against
  `create_app()` with a `Settings(_env_file=None, …)` carrying no OpenRouter
  values, overriding only `get_session` and `get_principal`.
- **Schema drift**: `Base.metadata.tables` (with `app.services.erm.models`
  imported, so `ErmSyncTask` registers) diffed against `information_schema.columns`.
- **Commercials wall**: AST check over `app/api/*.py` for router-decorated
  functions referencing a commercial model, asserting both `require_commercials`
  and a reach check appear in the function body.
- **Test suite**: `pytest -m "not integration and not rls" -q -p no:cacheprovider`,
  and again scoped to `tests/unit/test_llm.py` to isolate the contamination.

Four other agents were writing to this repository during the review, and the test
tree grew while I was in it: `tests/rag_eval/`, `tests/security/` and
`tests/api_contract/` all appeared between my first and second suite runs (1,451
→ 1,516 passing). Findings touching those directories concern work created during
this session and may already be in flight.

One of those suites — `tests/security/test_reach_conjunct_missing.py` — found
something I had the data for and did not analyse. §2.4 records that miss and the
method change that would have prevented it. I have left the original claim
visible with a pointer rather than silently rewriting it, because a review that
edits away its own errors is not one you can calibrate against next time.
