# Security review — byteXL Ops Intelligence Platform

**Date:** 2026-08-19 · **Scope:** authorised internal review, owner-requested
**Target:** `app/`, `supabase/migrations/`, `frontend/`, `tools/`, live Supabase project `rydozyntxyxdyfjukkim`
**Method:** static audit of all 36 FastAPI routes and all 58 RLS policies, plus live probing of
the database as each persona (`set local role authenticated` + `request.jwt.claims`, which is
exactly the pair PostgREST establishes for a browser session).

**Every database write in this review ran inside a transaction that was rolled back.** Nothing was
committed; no row that existed before the review was altered or deleted. Verified afterwards:
zero probe accounts left in `auth.users`, and the one bank row used as a write target still
carries its original account number.

Regression tests: `tests/security/`. Each open finding has a test asserting the *secure*
behaviour, marked `xfail(strict=True)` — so it documents the hole today and turns into a hard
failure the moment someone fixes the hole and forgets to remove the marker.

```
$ python -m pytest -q tests/security
44 passed, 33 xfailed in 86.02s
```

44 passed = controls that held (§7 below). 33 xfailed = every finding reproduced.

---

## Status as of 2026-08-19 — READ THIS FIRST

**Six of seven findings are CLOSED and the fixes are APPLIED to the live database.**
The sections below are the original write-ups, kept verbatim as the record of what
was found and how it was reproduced. Do not read them as current state.

| ID | Severity | Finding | Status |
|----|----------|---------|--------|
| SEC-01 | **Critical** | Anyone on the internet chooses their own persona at signup | **CLOSED** — migration 2100. Exploit re-run: signup with `{"role":"manager"}` now lands on the `trainer` sentinel; 1,026 trainers → 0, 1,025 writable rails → 0 |
| SEC-02 | **Critical** | Commercial policies carry the wall but not the scope | **PARTLY CLOSED** — migration 2200 added a reach conjunct to `trainer_bank_accounts` and `erm_sync_tasks`; 2400 closed the storage `trainers/` folder. **`trainers_sourcing_all` is still open** (PAN, email, phone on the roster) — see "Still open" below |
| SEC-03 | **High** | The same gap is a cross-tenant **write** | **CLOSED** for rails, ERM pack and storage — 2200/2400 carry the conjunct in `with check` as well as `using`. Open for the roster, with SEC-02 |
| SEC-04 | **High** | `TRUNCATE` granted to `anon` and `authenticated` | **CLOSED** — migration 2400 revoked `truncate, trigger, references, maintain` and amended default privileges. 0 relations carry it |
| SEC-05 | **Medium** | `app/api/erm.py` reproduces SEC-02 in the FastAPI layer | **CLOSED** — `_authorise_trainer()` now mirrors `can_reach_trainer()` instead of returning on persona alone |
| SEC-06 | Low | `/payouts/preview` trainer carve-out | **CLOSED** — `trainer_may_read` removed; the endpoint is behind `require_commercials()` alone |
| SEC-07 | Low | `comms.list_messages` applies `LIMIT` before the filter | **CLOSED** — the predicate moved into the `WHERE`; the Python pass is kept as a backstop |

**Verification:** `pytest -q tests/security` → 93 passed, 6 xfailed. The `xfail(strict=True)`
markers did their job — applying 2400 turned 25 of them into hard failures whose message was
the instruction to delete them. The 6 that remain are the findings genuinely still open.

### Still open

- **SEC-02 / SEC-03 on `trainers_sourcing_all`.** The roster policy is keyed on persona alone
  and is `for all`, so PAN is cross-tenant writable. It has been unfixable rather than
  unfixed: sourcing PRECEDES deployment, so there is nothing to scope it *by*. See the
  carve-out assessment in §6 — one `trainers.owning_college_id` column closes this, the
  undeployed carve-out and its cascade tail together, and it needs an owner decision.
- **The undeployed carve-out.** `can_reach_trainer()` returns TRUE for a trainer deployed
  nowhere, so any Manager nationally reads and writes a bench trainer's rails. Accepted cost
  of the onboarding path, pinned by
  `test_the_undeployed_carve_out_is_the_price_of_the_onboarding_path`.
- **F2.** An LDE Executive can null their own college's `cluster_id`, removing it from the
  overseeing Senior Manager's view. `colleges_internal_update` has no column guard.

### Found while fixing, and worth recording

`can_reach_trainer(NULL)` returned **TRUE** — a defect in migration 2200. Its carve-out branch
is `not exists (... where trainer_id = p_trainer_id)`, and `= NULL` matches no row, so
`not exists` is true: the helper answered "deployed nowhere, therefore visible" for an argument
that was not a trainer at all. Its siblings return FALSE for NULL.

It was never exploitable, but only because `trainer_bank_accounts.trainer_id` is `NOT NULL` and
`erm_sync_tasks_subject_ck` forces the same — the column constraints were holding the line, not
the function. It surfaced because 2400's storage policy derives the uuid from an object PATH,
where a malformed segment yields NULL, so the naive spelling would have shipped the conjunct and
a filename-shaped bypass on the same line. Closed twice: 2400 guards the policy with an explicit
`try_uuid(...) is not null`, and **migration 2500** makes the helper FALSE for NULL at source.

---

## Original findings (as written, 2026-08-19)

**The headline was SEC-01 × SEC-02.** Each was signed off on its own, with a written rationale.
Neither rationale survives the other. Details in §3.

---

## 1. SEC-01 — Persona is a form field (Critical, confirmed)

**Where:** `supabase/migrations/0200_identity.sql:340-365` · `frontend/src/auth/LoginPage.tsx:48`

`handle_new_user()` reads the persona out of client-supplied signup metadata:

```sql
requested_role := (new.raw_user_meta_data ->> 'role')::public.app_role;
insert into public.profiles (id, role, full_name)
values (new.id, coalesce(requested_role, 'trainer'), ...);
```

`raw_user_meta_data` is the `data` object of `POST /auth/v1/signup`, which needs nothing but the
anon key — a key that ships to every browser by design (`frontend/src/lib/supabase.ts`). The
frontend's own signup form offers it as a dropdown.

The migration header, 4 lines above the code, already identifies the danger and defends against
half of it:

> `is_admin` is never taken from user-supplied metadata — `raw_user_meta_data` is
> attacker-controlled at signup, and honouring `{"is_admin": true}` there would be the whole
> ballgame.

`is_admin` is indeed refused — I confirmed that. But `role` is the input to
`can_see_commercials()`, `is_internal()` and `is_senior_manager()`, which is the same ballgame
through a different door.

Two settings make it immediate rather than theoretical:

- `GET /auth/v1/settings` on the live project returns `"disable_signup": false`.
- `auto_confirm_email_on_signup` (`1100_no_email_confirmation.sql:71`) stamps `email_confirmed_at`
  on INSERT, so no mailbox is needed.

### Reproduction

```
POST https://<project>.supabase.co/auth/v1/signup
apikey: <public anon key>
{"email":"...","password":"...","data":{"role":"senior_manager"}}
```

Proven at the database layer by inserting the row GoTrue inserts, inside a rolled-back transaction
(`tests/security/test_signup_persona_escalation.py`):

```
metadata role='senior_manager' -> profiles.role='senior_manager' is_admin=False email_confirmed=True
    app_role/is_internal/commercials/is_admin/is_sm = ('senior_manager', True, True, False, True)
metadata role='manager'        -> profiles.role='manager'        is_admin=False email_confirmed=True
    app_role/is_internal/commercials/is_admin/is_sm = ('manager', True, True, False, False)
metadata role=None             -> profiles.role='trainer'   (sentinel, correct)
metadata role='not_a_role'     -> profiles.role='trainer'   (sentinel, correct)
```

### Impact

An unauthenticated stranger becomes `senior_manager` or `manager` and is inside the §4 commercials
wall before any human has seen the account. On its own that would be contained — see the stated
mitigation in §3 — but it is not contained, because of SEC-02.

### Note

The `trainer` sentinel works exactly as §4 describes: a malformed or absent role lands on a persona
holding no policy anywhere. That defence is sound and is pinned by a passing test. The defect is
that a *well-formed* role is honoured.

---

## 2. SEC-02 / SEC-03 — the wall without the scope (Critical / High, confirmed)

**Where:**
- `supabase/migrations/1400_trainer_bank_rails.sql:179-182` — `trainer_bank_accounts_commercials_all`
- `supabase/migrations/0400_trainers_deployments.sql:336-339` — `trainers_sourcing_all`
- `supabase/migrations/0900_storage.sql:124-137` — `documents_commercials_trainer_rw`

`app/core/security.py` states the invariant these three break, in its own words:

> Every money policy in `0700_finance.sql` has the shape
> `using (can_see_commercials() and can_reach_<scope>(...))` — the wall, and the scope.
> **Both conjuncts are load-bearing.** `require_commercials()` alone lets a Manager read another
> cluster's P&L.

All three ship with the wall alone:

```sql
create policy trainer_bank_accounts_commercials_all on public.trainer_bank_accounts
  for all to authenticated
  using (public.can_see_commercials())          -- no can_reach_*()
  with check (public.can_see_commercials());

create policy trainers_sourcing_all on public.trainers
  for all to authenticated
  using (public.app_role() in ('senior_manager', 'manager'));   -- no can_reach_*()
```

### Reproduction — read (SEC-02)

`mgr_no_reach` and `sm_no_reach` are seeded accounts with **zero** rows in
`user_college_assignments` and **zero** in `user_cluster_assignments`. There is no tenant whose
data they could legitimately hold.

```
mgr_no_reach: can_see_commercials=True, my_college_ids() count=0
  trainers visible            1026
  trainer_bank_accounts       1025
    LoadTest Educator 000270 | LTAKK0270S | lt000270@loadtest.bytexl.in | 0500******90 | CNRB0196716
    LoadTest Educator 000271 | LTAKL0271Z | lt000271@loadtest.bytexl.in | 0500******97 | PUNB0422286
    LoadTest Educator 000272 | LTAKM0272G | lt000272@loadtest.bytexl.in | 0500******04 | SBIN0405715
```

Cross-*cluster* too. `sm_cluster_south` covers cluster "Demo Cluster – South" only:

```
  can_reach_college(Malineni) = False
  select full_name,pan,email,phone from trainers where id = <Malineni trainer>
    -> ('Vema', 'FKYPM5666Z', 'marojurajesh4321@gmail.com', '9059433134')
```

### Reproduction — write (SEC-03)

The policies are `for all`, so the same predicate governs UPDATE and INSERT:

```
mgr_no_reach (0 colleges reached):
  update trainer_bank_accounts set bank_account_number='99999999999', ifsc='ATTK0000001'
    -> rowcount = 1025                      # every payment rail in the estate, one statement
  update trainers set pan='ZZZZZ9999Z' where id=<Malineni trainer>
    -> UPDATE ... RETURNING -> [(e2d183e9…, 'Vema', 'ZZZZZ9999Z')]
  insert into trainer_bank_accounts(...) values (<Malineni trainer>, '11112222333', 'ATTK0000001', …)
    -> INSERT ... RETURNING -> [(e2d183e9…, '11112222333')]
```

(All rolled back. `DELETE` is *not* granted on `trainer_bank_accounts` — that grant is correctly
narrow, and it is the one thing limiting the blast radius here.)

### Why the documented rationale does not cover this

`1400` argues the missing conjunct deliberately, under the heading *"NO REACH CONJUNCT, AND WHY
THAT IS NOT AN OVERSIGHT"*. The argument is genuinely good and I am not disputing it:

> rails are collected during onboarding — before any deployment exists, which is to say before
> `can_reach_trainer()` can be true of anybody. A reach-gated policy could not file the first
> account number.

That argument is about **INSERT at onboarding**. The migration's own cost statement is about
**SELECT** — *"a Manager reads the rails of a trainer deployed only at colleges they do not
cover"* — and it cross-references finding F1 in `supabase/tests/02_rls_matrix_test.sql`, which
reaches the same conclusion for the storage folder: *"the argument holds for INSERT. It does not
obviously hold for SELECT."*

Nothing in either document argues for **cross-tenant UPDATE of an existing payment rail**, which
is the highest-value write in the schema: repointing a beneficiary account between approval and
release redirects a payout that a Senior Manager has already signed. A policy shaped
`can_see_commercials() and (can_reach_trainer(trainer_id) or not exists(select 1 from deployments
where trainer_id = ...))` — "reachable, or not yet deployed anywhere" — would preserve the
onboarding path 1400 needs while closing the rest. 1400 anticipates exactly this fix:
*"If F1 is ever fixed with a 'reachable or undeployed' predicate, this policy takes the same fix."*

### Prior art, and the limit of what I proved

F1 (`supabase/tests/02_rls_matrix_test.sql:37`) already records the storage half of this —
`documents_commercials_trainer_rw` gating the whole `trainers/` folder on `can_see_commercials()`
alone. The policy is **still live and unchanged**, confirmed by reading `pg_policies`:

```
storage.objects :: documents_commercials_trainer_rw [ALL]
  USING: bucket_id = 'documents'
         AND (storage.foldername(name))[1] = 'trainers'
         AND can_see_commercials()                      -- no reach conjunct
```

**I could not demonstrate it against data:** the `documents` bucket is empty in this environment
(`select count(*) from storage.objects where bucket_id='documents'` → `0`). So the storage half is
confirmed by policy shape only, not by an observed leak. It will start leaking the first time a
signed work order is uploaded.

The `trainers` and `trainer_bank_accounts` tables are *not* covered by F1 — those are new here, and
both were demonstrated against live rows. The write half is not covered anywhere.

---

## 3. The chain — SEC-01 × SEC-02 (this is the finding that matters)

`frontend/src/auth/LoginPage.tsx:17-22` states the mitigation that makes open persona selection
acceptable:

> a new internal account has NO assignments, and internal reach comes from
> `user_college_assignments` / `user_cluster_assignments`, not from the persona. So a fresh Manager
> signup sees an empty console until an admin assigns them colleges. **That is least privilege
> working, not a bug to route around.**

That mitigation is true for every reach-scoped table — and false for exactly the three surfaces in
SEC-02, because those do not consult reach at all.

Measured end to end, in one rolled-back transaction: create the `auth.users` row a public signup
creates, with `{"role":"manager"}`, then act as it.

```
=== fresh signup as manager, zero assignments ===
  profiles.role, is_admin                    = ('manager', False)
  email auto-confirmed                       = True
  is_internal, can_see_commercials, reach    = (True, True, 0)
  trainers visible                           = 1026
  bank accounts visible                      = 1025
    LoadTest Educator 000270 | LTAKK0270S | lt000270@loadtest.bytexl.in | 0500******90 | CNRB0196716
  UPDATE all rails rowcount                  = 1025
```

**One unauthenticated HTTP request away: 1,026 trainer tax identities (PAN) with email and phone,
1,025 bank account numbers with IFSC, and the ability to rewrite all 1,025 payment rails in a
single statement.** No admin action, no email confirmation, no assignment. The `trainers/` storage
folder is granted by the same chain but could not be demonstrated — that bucket is currently empty.

Fixing either finding breaks the chain. Fixing SEC-01 is the smaller change and restores the
mitigation the frontend already claims: drop `role` from the metadata path so every signup lands
on the `trainer` sentinel and an admin assigns the persona — which is what §4 says an admin is
*for*. SEC-02/03 should still be fixed on its own merits, because a genuine Manager acquiring
another cluster's payment rails is a finding with or without SEC-01.

---

## 4. SEC-04 — TRUNCATE bypasses RLS and is granted to everyone (High, confirmed at DB layer)

**Where:** Supabase project default privileges (`pg_default_acl`), not any migration.

PostgreSQL applies RLS to SELECT/INSERT/UPDATE/DELETE. It does **not** apply to TRUNCATE, which is
gated by table privilege alone. A TRUNCATE grant is therefore an unconditional table-wipe right
that no policy in `supabase/migrations/` can restrain.

The migrations are careful — every one writes the minimal form:

```sql
grant select, insert, update, delete on public.trainer_attendance to authenticated;
```

But the project-level `ALTER DEFAULT PRIVILEGES` already granted `arwdDxtm` (the `D` is TRUNCATE)
on every new table in `public` to `anon`, `authenticated` **and** `service_role`. Nothing revokes
it, so the narrow explicit grant is additive to a wide implicit one:

```
pg_default_acl: ('postgres', 'public', 'r',
                 ['anon=arwdDxtm/postgres', 'authenticated=arwdDxtm/postgres', ...])

information_schema.role_table_grants, grantee='authenticated':
  trainer_attendance   DELETE,INSERT,REFERENCES,SELECT,TRIGGER,TRUNCATE,UPDATE
  remuneration_sheets  DELETE,INSERT,REFERENCES,SELECT,TRIGGER,TRUNCATE,UPDATE
  work_orders          DELETE,INSERT,REFERENCES,SELECT,TRIGGER,TRUNCATE,UPDATE
```

### Reproduction

Proven with a `postgres`-role row count taken *inside the same rolled-back transaction*, so the
count is about the table and not about what the impersonated persona can see:

```
postgres sees before:                                     152
postgres sees after TRUNCATE issued by lde_executive:       0
after ROLLBACK (restored):                                152
```

The lowest internal persona, and also the deny-by-default `trainer` sentinel, both succeeded on
`trainer_attendance` (the payout input, §5/§6), `remuneration_sheets`, `work_orders`,
`user_college_assignments` (the reach map every policy resolves against) and `tasks`.

### Reachability — stated honestly

**I did not demonstrate a remote path to this.** PostgREST exposes CRUD and `rpc/` and cannot
express TRUNCATE; `pg_graphql` likewise. No SECURITY INVOKER function in this schema executes
dynamic SQL — I specifically checked that the `test` schema from
`supabase/tests/02_rls_matrix_test.sql`, which *does* build dynamic SQL and grants itself to
`authenticated`, is **not deployed** to the live project (`pg_namespace` has no `test` schema).
That is a good outcome and worth keeping that way.

So today this is a latent over-grant, not a live exploit. It becomes live the moment anything can
run one arbitrary statement as `authenticated` — a deployed test harness, an injectable RPC, a
future SQL-executing function. It is cheap to close now:

```sql
revoke truncate, trigger, references on all tables in schema public from anon, authenticated;
alter default privileges in schema public revoke truncate, trigger, references
  on tables from anon, authenticated;
```

`audit_events` is the one table that got this right and is used as the model in the regression
test: SELECT-only grant *plus* a `BEFORE TRUNCATE` trigger.

---

## 5. SEC-05 — the FastAPI mirror of SEC-02 (Medium, confirmed)

**Where:** `app/api/erm.py:435-457`

`app/db/session.py` connects with a BYPASSRLS credential, so for every FastAPI route the Python
guard *is* the wall. `_authorise_trainer()` faithfully mirrors 1900's policy — including its
missing reach conjunct:

```python
async def _authorise_trainer(session, principal, trainer_id, *, write) -> None:
    require_internal(principal)
    if _owns_trainer_pipeline(principal):
        return                       # <-- returns before any reach check
```

`_owns_trainer_pipeline()` is true for Manager and Senior Manager, so a caller with zero
assignments passes for **any** `trainer_id`. `POST /erm/tasks` then files a card for an arbitrary
trainer and `GET /erm/tasks/{id}` returns `_live_pack()`, built from
`TrainerFacts(full_name, pan, email, phone, …)` (`app/api/erm.py:536-560`).

Confirmed against the guard function directly with a synthetic zero-reach `Principal`
(`tests/security/test_erm_trainer_reach.py`) — no HTTP or database needed, because the
short-circuit happens before the session is ever touched.

Same fix as SEC-02: the pipeline carve-out should be "reachable, or not yet deployed anywhere",
not "persona alone".

---

## 6. Lower-severity

**SEC-06 (Low, suspected).** `app/api/payouts.py:751-765,1407` keeps a trainer carve-out —
`_require_payout_persona(..., trainer_may_read=True)` on `POST /payouts/preview`. §4 (owner's
decision, 2026-08-18) says trainers are records, not users, and migration 1800 dropped all
eighteen trainer policies. The carve-out is **not exploitable today**: it additionally requires
`profiles.trainer_id`, which only an admin can set (`profiles_guard_privileged_columns`), and no
seeded account has one. But it is a live code path for a persona the schema deliberately disarmed,
and SEC-01 means the `trainer` label is now trivially self-assigned. Worth deleting rather than
carrying.

**SEC-07 (Low, confirmed, fails closed).** `app/api/comms.py:670,674` applies `LIMIT` in SQL and
then filters commercial rows in Python, so an LDE Executive asking for 50 messages may receive
fewer than 50 visible ones with more available. Under-disclosure, not over-disclosure — a
correctness bug, listed only so it is not mistaken for a leak later.

**Still-live known findings.** Both recorded in `supabase/tests/02_rls_matrix_test.sql:37-56`:

- **F1** — storage `trainers/` folder, no reach conjunct. Policy unchanged and still live;
  demonstrable only once the bucket has objects (see §2).
- **F2** — `colleges_internal_update` has no column guard, so `cluster_id` is writable by any
  internal persona that reaches the college. **Reproduced.** An LDE Executive at Demo Institute
  detached their own college from its cluster:

  ```
  as lde_demo_inst:
    update colleges set cluster_id = null where id = <Demo Institute> returning id, name, cluster_id
      -> rowcount 1  [(eb8ca94c…, 'Demo Institute of Technology', None)]
  ```

  This cannot widen the writer's own reach, so it is not privilege escalation — but it silently
  removes the campus from the Senior Manager who oversees that cluster, which is oversight removal
  by the persona with the least authority. `profiles`, `tasks` and `deployments` all narrow columns
  with a `BEFORE UPDATE` trigger; `colleges` does not.
- **F3** is **stale**: it describes `tasks_trainer_select_own` / `tasks_trainer_update_own`, which
  migration 1800 dropped. I confirmed no policy of either name exists. Recommend deleting F3 so the
  list stays trustworthy.

---

## 7. What held

These were attacked and did not move. All are pinned by passing (non-xfail) tests in
`tests/security/`, so a later refactor that undoes one goes red.

**R5 — the commercials wall, for the LDE Executive.** Zero rows from `pnl`,
`remuneration_sheets`, `work_orders` and `trainer_bank_accounts`, for both seeded LDE accounts,
read and write, from an impersonated `authenticated` session. This is enforced in the database,
as §4 requires — not by a Python filter above it.

**§4 — the trainer sentinel.** A signup with no role metadata gets `role='trainer'` and reads
**zero rows from all 26 tables and views**, its own `profiles` row excepted. All four writes that
1800 removed are refused, including the one that mattered most: the payee marking the attendance
that decides their own pay.

**The anon role.** Zero rows from every table. Every policy is `to authenticated`, which is the
single assumption that makes publishing the anon key safe — and it holds.

**JWT verification** (`app/core/security.py`). Refused: `alg: none` (and `alg: None`), an
algorithm outside the allow-list, a signature from a different secret, expired, wrong audience,
an ES256 header with no `kid`, and an unknown `kid`. The `alg` is checked against the allow-list
*before* anything else happens, which is the correct ordering and defeats algorithm confusion.
All failures return one indistinguishable 401. A token claiming `role: service_role` is accepted
as a claim but gains nothing — persona is re-read from `profiles` on every request and never taken
from the token.

**Endpoint coverage.** All 36 routes enumerated from the live router. All 35 non-health routes
resolve a `Principal` *and* call a reach/wall guard; `/health` performs no I/O and discloses only
`app_env` and a version string. Contrary to the brief's expectation, I found no endpoint that
forgets to filter. The one app-layer gap (SEC-05) is a guard that is present but too permissive,
mirroring a policy that is also too permissive — not an omission.

**The college views (0800).** Declared `security_invoker = false`, so their `WHERE` clause is
their entire access control. Both branches are intact; two LDE Executives at different colleges
see disjoint program sets, and unshared governance reports are invisible to every persona
including admins.

**R3 — agents cannot release.** No toolset holds an effect beyond `READ`/`SAVE_DRAFT`, no tool
name matches a send-suggestive pattern, and nothing under `app/` references `tools/agentmail.py`.
The mailer's allow-list refuses case variants, whitespace padding, comma smuggling inside a single
recipient string, and display-name wrapping — before any socket is opened.

**R4 — the lifecycle.** `APPROVED` is the only state with an edge to `RELEASED`; `RELEASED` is
terminal. The `artifact_versions_freeze` and `comms_messages_freeze` triggers make
`content_hash`, `approved_by` and `approved_at` immutable and block deletion of anything that
reached approval — enforced by trigger, so it survives the BYPASSRLS connection every FastAPI
request uses. `release()` re-verifies the hash against the system of record. `audit_events` is
append-only by trigger against UPDATE, DELETE **and** TRUNCATE.

**Injection.** No unparameterised SQL found. The single `text(f"...")`
(`app/api/erm.py:969`) interpolates a table name chosen from two hard-coded literals and binds
every value. `try_uuid()` makes malformed storage paths deny rather than error. `rag_search()` is
called with bound parameters only, and `RetrievalScope.__post_init__` re-derives `is_internal` and
`can_see_commercials` from the persona and raises on disagreement, so a scope cannot claim reach
its persona lacks.

**`rag_embeddings`.** RLS enabled with zero policies and no SELECT grant — fail-closed. Vector
rows are reachable only through `rag_search()`, which applies the persona filter in the same
statement as the `ORDER BY … LIMIT`.

---

## 8. Recommended order

1. **SEC-01** — stop honouring `role` from `raw_user_meta_data`. Smallest diff, breaks the
   critical chain, and restores the least-privilege story `LoginPage.tsx` already tells.
2. **SEC-03** — narrow the three `for all` policies to "reachable, or not yet deployed anywhere".
   1400 already names this fix. Closes the cross-tenant write to payment rails and PAN.
3. **SEC-02 / F1** — same predicate applied to SELECT and to the `trainers/` storage folder.
4. **SEC-04** — revoke `truncate, trigger, references` from `anon` and `authenticated`, and fix
   the default privileges so new tables do not reacquire them.
5. **SEC-05** — apply the SEC-03 predicate to `app/api/erm.py:_authorise_trainer`.
6. **SEC-06 / SEC-07 / F3** — delete the trainer carve-out, filter before `LIMIT`, retire the
   stale finding.
