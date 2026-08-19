# Migrations

Hand-authored SQL, applied in **filename order**. That order is the contract — the
numeric prefix is the only thing sequencing these files, so a new migration always gets
a higher number and an existing one is never renamed.

Not Alembic. See CLAUDE.md §11 for why: the security posture *is* RLS policies,
`SECURITY DEFINER` helpers and triggers on `auth.users`, none of which Alembic diffs or
autogenerates, and two migration systems against one database is a defect generator.

**Never edit a shipped migration.** Add a new one.

## Order

| File | Contents |
|---|---|
| `0100_enums_and_utils.sql` | All enums, `set_updated_at()`, `try_uuid()` |
| `0200_identity.sql` | `profiles`, the RLS helper functions, signup trigger, privilege guard |
| `0300_org_core.sql` | `clusters`, `colleges`, the two assignment tables, `programs`, `batches`, `students` |
| `0400_trainers_deployments.sql` | `trainers` (PAN is the identity key), `deployments`, `work_orders` |
| `0500_tasks.sql` | `tasks`, owner guard trigger |
| `0600_monitoring.sql` | `observations`, `feedback`, `assessments`, `attendance_records`, `trainer_attendance` |
| `0700_finance.sql` | `pnl`, `remuneration_sheets`, `governance_reports` — the commercials wall |
| `0800_college_views.sql` | The three curated college-facing views |
| `0900_storage.sql` | `documents` bucket and path-derived object policies |
| `1000_scheduling_documents.sql` | `task_templates`, `document_templates`, `program_documents` |
| `1100_no_email_confirmation.sql` | Password-only auth: auto-confirms `auth.users` on INSERT, backfills stranded accounts |
| `1200_backfill_profiles_and_bootstrap_admin.sql` | Profiles for accounts predating the signup trigger; promotes the first account to admin while none exists |
| `1300_audit_and_artifact_versions.sql` | `audit_events` (append-only) and `artifact_versions` — R4's freeze-and-hash on approval |
| `1400_trainer_bank_rails.sql` | `trainer_bank_accounts` with the PAN/IFSC/account-number check constraints §7 blocks on |
| `1500_batch_passout_year.sql` | `batches.passout_year`, so a batch is segregated by graduating year |
| `1600_rag_corpora.sql` | pgvector corpora and the persona filter §9 applies BEFORE retrieval |
| `1700_comms_queue.sql` | `comms_messages` — the single outbound queue, drafted and held, never sent |
| `1800_remove_trainer_persona_access.sql` | Drops all 18 trainer policies (4 of them writes). Educators are records, not users; the `trainer` enum label survives as the deny-by-default sentinel |
| `1900_erm_sync.sql` | `erm_sync_tasks` (CLAUDE.md §10 field pack + named assignee), `programs.erm_*` columns, and the drift triggers that flip a synced record to `stale` and requeue it |
| `2000_last_admin_guard.sql` | `profiles_guard_last_admin()` + two AFTER-row **constraint** triggers: no UPDATE or DELETE may leave a non-empty `profiles` with zero admins. Deliberately has **no** `auth.uid() is null` short-circuit — see technique 3 below — because it asserts system state, not caller authority, and the 2026-08-15 self-demotion came in on exactly that connection |
| `2400_truncate_grant_and_storage_reach.sql` | Two DB-layer findings. **SEC-04:** revokes `truncate, trigger, references, maintain` from `anon`/`authenticated` on every table in `public` and amends `alter default privileges` so new tables do not re-acquire them — RLS never applies to TRUNCATE, so Supabase's default `arwdDxtm` was an unconditional wipe right on the payout inputs. **F1:** `documents_commercials_trainer_rw` gains 2200's `can_reach_trainer()` conjunct, keyed off the object path. The explicit `try_uuid(...) is not null` in front of it is load-bearing: `can_reach_trainer(NULL)` is **TRUE**, so without it a malformed path bypasses the conjunct it just added |

`supabase/seed.sql` runs last: 37 task templates + 37 document templates. It is idempotent
(`on conflict … do update`), so re-running it evolves the operating model in place rather
than duplicating rows. **The RLS test suite asserts those exact counts** — editing the seed
means updating `supabase/tests/`.

## Applying

```bash
python supabase/tests/run_tests.py --apply-only     # applies, records in supabase_migrations
python supabase/tests/run_tests.py                  # applies, then runs the persona matrix
```

`DATABASE_URL` must be the **session** pooler (port 5432). The transaction pooler (6543)
does not preserve `SET ROLE`, which makes every persona test pass while proving nothing.
`app/core/config.py` rejects port 6543 rather than let that happen quietly.

## Three techniques that are load-bearing

Preserved deliberately from the three-persona schema this was ported from. Each avoids a
specific failure, documented at the top of `0200_identity.sql`:

1. **`SECURITY DEFINER` + `STABLE` + `SET search_path = ''`** on every helper. Definer
   breaks the infinite recursion of a policy on `profiles` that selects from `profiles`.
   `STABLE` gets it evaluated once per statement rather than once per row. The empty
   `search_path` stops a caller shadowing an object in an earlier schema — which is why
   every reference inside those functions is spelled out fully qualified.
2. **`FORCE ROW LEVEL SECURITY`**, not just `ENABLE`, so the table owner is subject to
   policy too. `BYPASSRLS` roles (`postgres`, `service_role`) still bypass — that is the
   intended FastAPI path, and exactly why the backend must re-check persona and ownership
   in code.
3. **Column-level narrowing via `BEFORE UPDATE` triggers**, not policies, because RLS
   `WITH CHECK` cannot see the `OLD` row. Every guard short-circuits on
   `auth.uid() is null` so migrations and seeds pass through — **which means the guards do
   nothing on a service-role connection.** Treat that as the sharpest edge in this schema.

## The commercials wall

One predicate, reused: `can_see_commercials()` is true for `senior_manager` and `manager`
only. Every money table's policy is `can_see_commercials() and can_reach_<scope>(…)`, so an
LDE Executive fails the first conjunct and gets zero rows from `pnl`, `remuneration_sheets`
and work-order rates — in the database, not in the UI. CLAUDE.md §4 and R5.

Reach itself comes from `user_college_assignments` and `user_cluster_assignments` via
`my_college_ids()`, never from `profiles.college_id` — that column serves the College
persona alone.
