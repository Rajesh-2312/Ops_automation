-- =============================================================================
-- 2300 — reach predicates in the SET form (architecture review §1)
-- =============================================================================
-- A pure PERFORMANCE change. Not one row becomes visible to anyone who could
-- not already see it; §"PROOF" below records how that was established rather
-- than asserted.
--
-- WHAT WAS WRONG
-- --------------
-- `0300_org_core.sql:266-270` argued for the scalar function form over the set
-- form:
--
--   > Membership test, written against the base tables rather than as
--   > `... in (select my_college_ids())`. Same answer, but this form is two
--   > indexed lookups with an early exit, whereas the set version expands the
--   > caller's whole cluster before comparing — which for a Senior Manager over
--   > a 40-college cluster happens once per row of every policy evaluation.
--
-- "once per row" is false IN A POLICY POSITION. `my_college_ids()` takes no
-- arguments, so the sub-select carries no correlated parameter and the planner
-- hoists it to an InitPlan / HashAggregate evaluated once per statement
-- (loops=1). Every row after that is a hash probe.
--
--   Seq Scan on tasks
--     Filter: (is_internal() AND can_reach_program(program_id))   <- per row
--
--   Seq Scan on tasks
--     Filter: (is_internal() AND (hashed SubPlan 2))              <- once
--       SubPlan 2
--         ->  ProjectSet (loops=1)
--               ->  Result (loops=1)
--
-- MEASURED, live database, PostgreSQL 17.6, impersonating a real Manager the
-- way PostgREST does (`request.jwt.claims` + `set local role authenticated`),
-- `explain (analyze, timing on)` over generate_series with the argument made
-- row-dependent so nothing can be hoisted, loop baseline subtracted:
--
--   predicate                                     per call    vs set form
--   -------------------------------------------   ---------   -----------
--   <col> in (select public.my_college_ids())        0.2 us         1x
--   public.can_reach_college(<col>)                 37.8 us       189x
--   public.can_reach_program(<col>)                448.7 us      2244x
--   public.can_reach_batch(<col>)                  472.6 us      2363x
--   public.can_reach_deployment(<col>)             458.4 us      2292x
--   public.can_reach_trainer(<col>)                251.6 us      1258x
--
-- WHY `create or replace function` ALONE COULD NOT FIX IT
-- ------------------------------------------------------
-- The obvious minimal change — rewrite the helper BODIES in the set form and
-- leave all thirty-one policies untouched — was written and measured before
-- this migration was written. It does not work, and it is worth recording why,
-- because the reasoning in 0300 turns out to be right about the body and wrong
-- only about the policy:
--
--   can_reach_college(<col>)                            37.6 us   (shipped)
--   same, body rewritten to `p_id in (select my_college_ids())`  464.6 us
--   can_reach_program(<col>)                           566.1 us   (shipped)
--   same, body rewritten to the set form                550.9 us
--
-- Inside a function body the sub-select is planned once per CALL, so the set
-- form buys nothing there and costs a nested non-inlinable invocation — twelve
-- times WORSE for `can_reach_college`. Hoisting is a property of the policy
-- expression, not of the SQL text. So the policies have to change; there is no
-- version of this fix that only touches functions.
--
-- What `create or replace` CAN do is remove the second level of nesting, which
-- is where the 38 us -> 450 us jump comes from: `can_reach_program/batch/
-- deployment` each call `can_reach_college()` from inside their own body, and a
-- `STABLE SECURITY DEFINER SET search_path` function is non-inlinable, so the
-- inner call is a second full invocation. Part B below flattens those three
-- against the assignment tables directly. That is a ~12x win for every caller
-- that is NOT a policy — ad-hoc SQL, psql, the FastAPI mirrors' spot checks —
-- and it needs no policy change at all.
--
-- SHAPE OF THIS MIGRATION
-- -----------------------
--   Part A  new zero-argument set helpers, the hoistable form
--   Part B  `create or replace` the scalar can_reach_* bodies, un-nested
--   Part C  the thirty-one policies, reach conjunct swapped for the set form
--   Part D  the three college-facing views, same swap
--
-- Style follows `2200_trainer_reach_conjuncts.sql`, which already wrote
-- `can_reach_trainer()` this way and left the note explaining it.
--
-- PROOF (see tests/perf/)
-- ----------------------
--   1. Structural. Every policy's normalised `pg_policies.qual`/`with_check` is
--      captured before and after. The two are compared after applying the exact
--      textual substitution `can_reach_X(e)` -> `e in (select my_X_ids())`.
--      Any other difference fails.
--   2. Behavioural. For all seven personas in `tests/security/conftest.py`
--      USERS, `count(*)` and a checksum of the visible primary keys is taken
--      per table before and after, under RLS. Identical, or the change is
--      rejected. A performance fix that grants a row is worse than a slow one.
--   3. Both were run inside a transaction that was ROLLED BACK, against the
--      live database, before this file was committed.
--
-- WHAT DELIBERATELY DOES NOT CHANGE
-- ---------------------------------
-- * `can_reach_college()` keeps its shipped body. It is already flat — one
--   function level, 37.8 us — and the set form measured 12x worse inside a
--   body (above). 0300's argument stands for this function.
-- * `can_reach_trainer()` (2200) keeps its body. It is a fresh security fix
--   (SEC-02/03) and, after Part C, no policy calls it per row any more, so its
--   per-call cost no longer sits in a hot path. Re-deriving a security
--   predicate for a performance win nobody collects is a bad trade.
-- * `rag_chunk_is_commercial(id)` and `artifact_is_commercial(type, id)` stay
--   per-row. They are the WALL half, not the reach half, and both are
--   `SECURITY DEFINER` precisely so an invisible row still reads as commercial
--   (fail closed). The obvious set rewrite of `rag_chunk_is_commercial` is an
--   invoker-side subquery over `rag_documents`, which RLS would then filter —
--   turning "cannot see it, so treat it as commercial" into "cannot see it, so
--   it is not commercial". That is a widening, and it is why they are left.
-- * `trainers_sourcing_all` (0400) and `documents_commercials_trainer_rw`
--   (0900) carry no reach conjunct at all — deliberately, per 2200's header.
--   Nothing here adds one.
-- * `erm_sync_tasks_sourcing_all` and `trainer_bank_accounts_commercials_all`
--   are recreated `to public` rather than `to authenticated`, because that is
--   what 2200 left live. Narrowing the grantee list would be a permission
--   change smuggled into a performance migration.
-- =============================================================================


-- =============================================================================
-- Part A — zero-argument set helpers
-- =============================================================================
-- Each is `setof uuid`, zero-argument, and therefore hoistable to one hashed
-- subplan per statement wherever it appears as `<column> in (select ...)`.
--
-- Each resolves colleges through `my_college_ids()` — ONE nested call, not a
-- chain. `my_batch_ids()` deliberately does not call `my_program_ids()`, and
-- `my_deployment_ids()` does not call `my_batch_ids()`: a nested non-inlinable
-- call costs ~460 us, and since these run once per statement that would put a
-- fixed 1.4 ms floor under every query. One level, joined out to the base
-- tables, keeps the fixed cost at a single call while leaving `my_college_ids()`
-- the single source of truth for what "reach" means.

create or replace function public.my_program_ids()
returns setof uuid
language sql
stable
security definer
set search_path = ''
as $$
  select pr.id
  from public.programs pr
  where pr.college_id in (select public.my_college_ids());
$$;

comment on function public.my_program_ids() is
  'Every program the caller reaches, via programs.college_id. The set form of '
  'can_reach_program(uuid): zero-argument, so the planner hoists it to one '
  'hashed subplan per statement instead of a function call per row (2300).';

create or replace function public.my_batch_ids()
returns setof uuid
language sql
stable
security definer
set search_path = ''
as $$
  select b.id
  from public.batches b
  join public.programs pr on pr.id = b.program_id
  where pr.college_id in (select public.my_college_ids());
$$;

comment on function public.my_batch_ids() is
  'Every batch the caller reaches, via batches.program_id -> programs.college_id. '
  'The set form of can_reach_batch(uuid). See 2300.';

create or replace function public.my_deployment_ids()
returns setof uuid
language sql
stable
security definer
set search_path = ''
as $$
  select d.id
  from public.deployments d
  join public.batches b  on b.id  = d.batch_id
  join public.programs pr on pr.id = b.program_id
  where pr.college_id in (select public.my_college_ids());
$$;

comment on function public.my_deployment_ids() is
  'Every deployment the caller reaches, via deployments.batch_id -> batches.'
  'program_id -> programs.college_id. The set form of can_reach_deployment(uuid). '
  'Gate for trainer_attendance, attendance_records and observations. See 2300.';

-- Mirrors `can_reach_trainer()` (2200) EXACTLY, carve-out included: a trainer
-- deployed nowhere at all is reachable by everyone, because sourcing precedes
-- deployment and you cannot collect somebody's bank details for onboarding if
-- you cannot see them until after they are deployed.
create or replace function public.my_trainer_ids()
returns setof uuid
language sql
stable
security definer
set search_path = ''
as $$
  -- Engaged nowhere yet: the sourcing pipeline carve-out (2200).
  select t.id
  from public.trainers t
  where not exists (
    select 1 from public.deployments d where d.trainer_id = t.id
  )
  union
  -- Engaged somewhere the caller covers.
  select d.trainer_id
  from public.deployments d
  join public.batches b   on b.id  = d.batch_id
  join public.programs pr on pr.id = b.program_id
  where pr.college_id in (select public.my_college_ids());
$$;

comment on function public.my_trainer_ids() is
  'The set form of can_reach_trainer(uuid), carve-out and all: trainers deployed '
  'at a college the caller covers, UNION trainers deployed nowhere at all. Keep '
  'the two definitions in step — 2200 owns the reasoning, 2300 owns the shape.';

-- The set form of `can_read_corpus(rag_corpus)`. Zero-argument, so the ACL
-- lookup happens once per statement rather than once per document row.
create or replace function public.my_rag_corpora()
returns setof public.rag_corpus
language sql
stable
security definer
set search_path = ''
as $$
  select a.corpus
  from public.rag_corpus_access a
  where a.role = public.app_role();
$$;

comment on function public.my_rag_corpora() is
  'The set form of can_read_corpus(rag_corpus): every corpus this persona holds. '
  'Deny by default — a persona with no row in rag_corpus_access reads nothing.';

-- The set form of `can_reach_rag_document(uuid)`. NULL college/program means
-- organisation-wide, exactly as in 1600; no admin override, exactly as in 1600.
create or replace function public.my_rag_document_ids()
returns setof uuid
language sql
stable
security definer
set search_path = ''
as $$
  select d.id
  from public.rag_documents d
  where (d.college_id is null or d.college_id in (select public.my_college_ids()))
    and (d.program_id is null or d.program_id in (select public.my_program_ids()));
$$;

comment on function public.my_rag_document_ids() is
  'The set form of can_reach_rag_document(uuid). NULL college/program is '
  'organisation-wide. Reach only — says nothing about the corpus ACL '
  '(my_rag_corpora) or the commercials wall (rag_chunk_is_commercial).';

-- The set form of `can_reach_artifact(artifact_type, uuid)`. Returns the PAIR,
-- so the policy can write `(artifact_type, artifact_id) in (select ...)` — a
-- row-comparison IN, which the planner hashes and hoists just like a scalar
-- one. A per-type function taking the type as an argument would work too, but
-- only if the argument were a constant; `artifact_type` is a column, so the
-- subplan would correlate and hoisting would be lost.
--
-- SECURITY DEFINER for the same reason 1300 gives: as invoker, this would be
-- filtered by each target table's own policy and would return false for an LDE
-- Executive because they cannot SEE the row — the right answer by the wrong
-- mechanism, and one that inverts silently the moment a policy elsewhere
-- widens.
create or replace function public.my_artifact_keys()
returns table (artifact_type public.artifact_type, artifact_id uuid)
language sql
stable
security definer
set search_path = ''
as $$
  select 'remuneration_sheets'::public.artifact_type, rs.id
  from public.remuneration_sheets rs
  where rs.program_id in (select public.my_program_ids())
  union all
  select 'governance_reports'::public.artifact_type, gr.id
  from public.governance_reports gr
  where gr.program_id in (select public.my_program_ids())
  union all
  select 'program_documents'::public.artifact_type, pd.id
  from public.program_documents pd
  where pd.program_id in (select public.my_program_ids());
$$;

comment on function public.my_artifact_keys() is
  'The set form of can_reach_artifact(artifact_type, uuid): every (type, id) '
  'pair whose program the caller reaches. Scope only — says NOTHING about the '
  'commercials wall, which stays artifact_is_commercial(), kept separate on '
  'purpose (1300).';

revoke execute on function public.my_program_ids()       from public, anon;
revoke execute on function public.my_batch_ids()         from public, anon;
revoke execute on function public.my_deployment_ids()    from public, anon;
revoke execute on function public.my_trainer_ids()       from public, anon;
revoke execute on function public.my_rag_corpora()       from public, anon;
revoke execute on function public.my_rag_document_ids()  from public, anon;
revoke execute on function public.my_artifact_keys()     from public, anon;

grant execute on function public.my_program_ids()       to authenticated, service_role;
grant execute on function public.my_batch_ids()         to authenticated, service_role;
grant execute on function public.my_deployment_ids()    to authenticated, service_role;
grant execute on function public.my_trainer_ids()       to authenticated, service_role;
grant execute on function public.my_rag_corpora()       to authenticated, service_role;
grant execute on function public.my_rag_document_ids()  to authenticated, service_role;
grant execute on function public.my_artifact_keys()     to authenticated, service_role;


-- =============================================================================
-- Part B — un-nest the scalar can_reach_* helpers
-- =============================================================================
-- Same signature, same answer, no nested `can_reach_college()` call. The
-- rewrite is exact because the joining column is a primary key: there is at
-- most one `programs` row for a given `p_program_id`, so
-- `exists(pr where pr.id = $1 and can_reach_college(pr.college_id))` and the
-- flattened join below agree row for row, NULL argument included (both false).
--
-- The `union all` mirrors `can_reach_college()`'s own body: direct assignment,
-- or cluster assignment expanded through `colleges.cluster_id`. `exists` makes
-- the duplicate a non-issue, which is why this is `union all` and
-- `my_college_ids()` is `union`.
--
-- These functions are kept, not dropped: `app/core/security.py` documents them
-- as the SQL side of the Python mirror, ad-hoc SQL uses them, and dropping a
-- granted function is a wider blast radius than replacing its body.

create or replace function public.can_reach_program(p_program_id uuid)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1
    from public.programs pr
    join public.user_college_assignments uca on uca.college_id = pr.college_id
    where pr.id = p_program_id
      and uca.user_id = (select auth.uid())
    union all
    select 1
    from public.programs pr
    join public.colleges c on c.id = pr.college_id
    join public.user_cluster_assignments ucl on ucl.cluster_id = c.cluster_id
    where pr.id = p_program_id
      and ucl.user_id = (select auth.uid())
  );
$$;

comment on function public.can_reach_program(uuid) is
  'Reach to a program, via programs.college_id. Same answer as the 0300 body; '
  'flattened against the assignment tables in 2300 so it no longer pays a '
  'nested non-inlinable can_reach_college() call (~450 us -> ~40 us). In a '
  'POLICY, prefer `program_id in (select public.my_program_ids())`.';

create or replace function public.can_reach_batch(p_batch_id uuid)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1
    from public.batches b
    join public.programs pr on pr.id = b.program_id
    join public.user_college_assignments uca on uca.college_id = pr.college_id
    where b.id = p_batch_id
      and uca.user_id = (select auth.uid())
    union all
    select 1
    from public.batches b
    join public.programs pr on pr.id = b.program_id
    join public.colleges c on c.id = pr.college_id
    join public.user_cluster_assignments ucl on ucl.cluster_id = c.cluster_id
    where b.id = p_batch_id
      and ucl.user_id = (select auth.uid())
  );
$$;

comment on function public.can_reach_batch(uuid) is
  'Reach to a batch, via batches.program_id -> programs.college_id. Flattened in '
  '2300; same answer as the 0300 body. In a POLICY, prefer '
  '`batch_id in (select public.my_batch_ids())`.';

create or replace function public.can_reach_deployment(p_deployment_id uuid)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1
    from public.deployments d
    join public.batches b   on b.id  = d.batch_id
    join public.programs pr on pr.id = b.program_id
    join public.user_college_assignments uca on uca.college_id = pr.college_id
    where d.id = p_deployment_id
      and uca.user_id = (select auth.uid())
    union all
    select 1
    from public.deployments d
    join public.batches b   on b.id  = d.batch_id
    join public.programs pr on pr.id = b.program_id
    join public.colleges c  on c.id  = pr.college_id
    join public.user_cluster_assignments ucl on ucl.cluster_id = c.cluster_id
    where d.id = p_deployment_id
      and ucl.user_id = (select auth.uid())
  );
$$;

comment on function public.can_reach_deployment(uuid) is
  'Internal reach to a deployment, via deployments.batch_id -> batches.program_id '
  '-> programs.college_id. Flattened in 2300; same answer as the 0400 body. In a '
  'POLICY, prefer `deployment_id in (select public.my_deployment_ids())`.';


-- =============================================================================
-- Part C — the policies
-- =============================================================================
-- Every policy below is recreated with its predicate byte-identical EXCEPT that
-- `public.can_reach_X(<expr>)` becomes `<expr> in (select public.my_X_ids())`.
-- Nothing else moves: same name, same command, same grantee list, same other
-- conjuncts in the same order.
--
-- `drop policy` + `create policy` rather than `alter policy` because `alter
-- policy ... using (...)` cannot change `with check` and `using` in one
-- statement portably, and a half-altered `for all` policy is a security state
-- nobody should be able to observe. The whole migration runs in one
-- transaction, so no window exists where a table is unprotected.

-- --- colleges ----------------------------------------------------------------

drop policy if exists colleges_internal_select on public.colleges;
create policy colleges_internal_select on public.colleges
  for select to authenticated
  using (public.is_internal() and id in (select public.my_college_ids()));

drop policy if exists colleges_internal_update on public.colleges;
create policy colleges_internal_update on public.colleges
  for update to authenticated
  using (public.is_internal() and id in (select public.my_college_ids()))
  with check (public.is_internal() and id in (select public.my_college_ids()));

-- --- programs ----------------------------------------------------------------

drop policy if exists programs_internal_all on public.programs;
create policy programs_internal_all on public.programs
  for all to authenticated
  using (public.is_internal() and college_id in (select public.my_college_ids()))
  with check (public.is_internal() and college_id in (select public.my_college_ids()));

-- --- batches / students ------------------------------------------------------

drop policy if exists batches_internal_all on public.batches;
create policy batches_internal_all on public.batches
  for all to authenticated
  using (public.is_internal() and program_id in (select public.my_program_ids()))
  with check (public.is_internal() and program_id in (select public.my_program_ids()));

drop policy if exists students_internal_all on public.students;
create policy students_internal_all on public.students
  for all to authenticated
  using (public.is_internal() and batch_id in (select public.my_batch_ids()))
  with check (public.is_internal() and batch_id in (select public.my_batch_ids()));

-- --- deployments -------------------------------------------------------------

drop policy if exists deployments_internal_all on public.deployments;
create policy deployments_internal_all on public.deployments
  for all to authenticated
  using (public.is_internal() and batch_id in (select public.my_batch_ids()))
  with check (public.is_internal() and batch_id in (select public.my_batch_ids()));

-- --- monitoring: the highest-growth tables in the schema ---------------------
-- `trainer_attendance` is one row per deployment per day (CLAUDE.md §6), so it
-- is the table this whole migration exists for.

drop policy if exists observations_internal_all on public.observations;
create policy observations_internal_all on public.observations
  for all to authenticated
  using (public.is_internal() and deployment_id in (select public.my_deployment_ids()))
  with check (public.is_internal() and deployment_id in (select public.my_deployment_ids()));

drop policy if exists feedback_internal_all on public.feedback;
create policy feedback_internal_all on public.feedback
  for all to authenticated
  using (public.is_internal() and batch_id in (select public.my_batch_ids()))
  with check (public.is_internal() and batch_id in (select public.my_batch_ids()));

drop policy if exists assessments_internal_all on public.assessments;
create policy assessments_internal_all on public.assessments
  for all to authenticated
  using (public.is_internal() and batch_id in (select public.my_batch_ids()))
  with check (public.is_internal() and batch_id in (select public.my_batch_ids()));

drop policy if exists attendance_records_internal_all on public.attendance_records;
create policy attendance_records_internal_all on public.attendance_records
  for all to authenticated
  using (public.is_internal() and deployment_id in (select public.my_deployment_ids()))
  with check (public.is_internal() and deployment_id in (select public.my_deployment_ids()));

drop policy if exists trainer_attendance_internal_all on public.trainer_attendance;
create policy trainer_attendance_internal_all on public.trainer_attendance
  for all to authenticated
  using (public.is_internal() and deployment_id in (select public.my_deployment_ids()))
  with check (public.is_internal() and deployment_id in (select public.my_deployment_ids()));

-- --- tasks -------------------------------------------------------------------

drop policy if exists tasks_internal_all on public.tasks;
create policy tasks_internal_all on public.tasks
  for all to authenticated
  using (public.is_internal() and program_id in (select public.my_program_ids()))
  with check (public.is_internal() and program_id in (select public.my_program_ids()));

-- --- the commercials wall ----------------------------------------------------
-- The wall conjunct `can_see_commercials()` is untouched and stays first: it is
-- zero-argument and already hoisted (review §1.5), and an LDE Executive still
-- fails it before reach is ever considered.

drop policy if exists pnl_commercials_all on public.pnl;
create policy pnl_commercials_all on public.pnl
  for all to authenticated
  using (public.can_see_commercials() and program_id in (select public.my_program_ids()))
  with check (public.can_see_commercials() and program_id in (select public.my_program_ids()));

drop policy if exists remuneration_sheets_commercials_all on public.remuneration_sheets;
create policy remuneration_sheets_commercials_all on public.remuneration_sheets
  for all to authenticated
  using (public.can_see_commercials() and program_id in (select public.my_program_ids()))
  with check (public.can_see_commercials() and program_id in (select public.my_program_ids()));

drop policy if exists work_orders_commercials_all on public.work_orders;
create policy work_orders_commercials_all on public.work_orders
  for all to authenticated
  using (public.can_see_commercials() and program_id in (select public.my_program_ids()))
  with check (public.can_see_commercials() and program_id in (select public.my_program_ids()));

drop policy if exists governance_reports_internal_all on public.governance_reports;
create policy governance_reports_internal_all on public.governance_reports
  for all to authenticated
  using (public.is_internal() and program_id in (select public.my_program_ids()))
  with check (public.is_internal() and program_id in (select public.my_program_ids()));

-- `to public`, not `to authenticated` — that is what 2200 left live, and
-- narrowing the grantee list would be a permission change hiding in a
-- performance migration.
drop policy if exists trainer_bank_accounts_commercials_all on public.trainer_bank_accounts;
create policy trainer_bank_accounts_commercials_all on public.trainer_bank_accounts
  for all
  using (public.can_see_commercials() and trainer_id in (select public.my_trainer_ids()))
  with check (public.can_see_commercials() and trainer_id in (select public.my_trainer_ids()));

-- --- trainers ----------------------------------------------------------------

drop policy if exists trainers_lde_select_deployed on public.trainers;
create policy trainers_lde_select_deployed on public.trainers
  for select to authenticated
  using (public.is_internal() and id in (select public.my_trainer_ids()));

-- --- program_documents -------------------------------------------------------

drop policy if exists program_documents_internal_all on public.program_documents;
create policy program_documents_internal_all on public.program_documents
  for all to authenticated
  using (
    public.is_internal()
    and program_id in (select public.my_program_ids())
    and category <> all (array['remuneration', 'invoice_generation']::public.document_category[])
  )
  with check (
    public.is_internal()
    and program_id in (select public.my_program_ids())
    and category <> all (array['remuneration', 'invoice_generation']::public.document_category[])
  );

drop policy if exists program_documents_commercials_all on public.program_documents;
create policy program_documents_commercials_all on public.program_documents
  for all to authenticated
  using (public.can_see_commercials() and program_id in (select public.my_program_ids()))
  with check (public.can_see_commercials() and program_id in (select public.my_program_ids()));

-- --- comms_messages ----------------------------------------------------------

drop policy if exists comms_messages_internal_all on public.comms_messages;
create policy comms_messages_internal_all on public.comms_messages
  for all to authenticated
  using (
    public.is_internal()
    and not is_commercial
    and program_id in (select public.my_program_ids())
  )
  with check (
    public.is_internal()
    and not is_commercial
    and program_id in (select public.my_program_ids())
  );

drop policy if exists comms_messages_commercials_all on public.comms_messages;
create policy comms_messages_commercials_all on public.comms_messages
  for all to authenticated
  using (
    public.can_see_commercials()
    and is_commercial
    and program_id in (select public.my_program_ids())
  )
  with check (
    public.can_see_commercials()
    and is_commercial
    and program_id in (select public.my_program_ids())
  );

-- --- artifact_versions -------------------------------------------------------
-- Row-comparison IN against the zero-argument pair set. `artifact_is_commercial`
-- stays per-row on purpose — see the header.

drop policy if exists artifact_versions_internal_all on public.artifact_versions;
create policy artifact_versions_internal_all on public.artifact_versions
  for all to authenticated
  using (
    public.is_internal()
    and not public.artifact_is_commercial(artifact_type, artifact_id)
    and (artifact_type, artifact_id) in (select k.artifact_type, k.artifact_id
                                         from public.my_artifact_keys() k)
  )
  with check (
    public.is_internal()
    and not public.artifact_is_commercial(artifact_type, artifact_id)
    and (artifact_type, artifact_id) in (select k.artifact_type, k.artifact_id
                                         from public.my_artifact_keys() k)
  );

drop policy if exists artifact_versions_commercials_all on public.artifact_versions;
create policy artifact_versions_commercials_all on public.artifact_versions
  for all to authenticated
  using (
    public.can_see_commercials()
    and public.artifact_is_commercial(artifact_type, artifact_id)
    and (artifact_type, artifact_id) in (select k.artifact_type, k.artifact_id
                                         from public.my_artifact_keys() k)
  )
  with check (
    public.can_see_commercials()
    and public.artifact_is_commercial(artifact_type, artifact_id)
    and (artifact_type, artifact_id) in (select k.artifact_type, k.artifact_id
                                         from public.my_artifact_keys() k)
  );

-- --- erm_sync_tasks ----------------------------------------------------------
-- `erm_sync_tasks_subject_ck` guarantees
--   subject_kind = 'trainer' <=> trainer_id is not null
--   subject_kind = 'program' <=> program_id is not null
-- so the reach conjunct never sees a NULL subject id. That matters: NULL is the
-- one input on which `can_reach_trainer(NULL)` (true, via `not exists`) and
-- `NULL in (select ...)` (NULL, denied) disagree, and the constraint is what
-- makes the row unreachable rather than the policy.

drop policy if exists erm_sync_tasks_program_all on public.erm_sync_tasks;
create policy erm_sync_tasks_program_all on public.erm_sync_tasks
  for all to authenticated
  using (
    subject_kind = 'program'::public.erm_subject_kind
    and public.is_internal()
    and program_id in (select public.my_program_ids())
  )
  with check (
    subject_kind = 'program'::public.erm_subject_kind
    and public.is_internal()
    and program_id in (select public.my_program_ids())
  );

drop policy if exists erm_sync_tasks_lde_select_trainer on public.erm_sync_tasks;
create policy erm_sync_tasks_lde_select_trainer on public.erm_sync_tasks
  for select to authenticated
  using (
    subject_kind = 'trainer'::public.erm_subject_kind
    and public.is_internal()
    and trainer_id in (select public.my_trainer_ids())
  );

-- `to public`, as 2200 left it. See trainer_bank_accounts above.
drop policy if exists erm_sync_tasks_sourcing_all on public.erm_sync_tasks;
create policy erm_sync_tasks_sourcing_all on public.erm_sync_tasks
  for all
  using (
    subject_kind = 'trainer'::public.erm_subject_kind
    and public.app_role() in ('senior_manager', 'manager')
    and trainer_id in (select public.my_trainer_ids())
  )
  with check (
    subject_kind = 'trainer'::public.erm_subject_kind
    and public.app_role() in ('senior_manager', 'manager')
    and trainer_id in (select public.my_trainer_ids())
  );

-- --- RAG ---------------------------------------------------------------------
-- Review §9.3: the most expensive policy in the schema, three function levels
-- deep, and one screen away from running on the browser path.
--
-- Note the corpus clause on rag_chunks is rewritten as an UNCORRELATED subquery
-- over `rag_documents`, NOT moved into a SECURITY DEFINER helper. It stays
-- invoker-side, so `rag_documents_read` still filters it exactly as the
-- original `exists (...)` did — the rewrite removes the correlation, not the
-- RLS. `document_id` is compared against `d.id`, the primary key, so at most
-- one document matched before and at most one matches now.

drop policy if exists rag_documents_read on public.rag_documents;
create policy rag_documents_read on public.rag_documents
  for select to authenticated
  using (
    public.is_internal()
    and corpus in (select public.my_rag_corpora())
    and (public.can_see_commercials() or not is_commercial)
    and (college_id is null or college_id in (select public.my_college_ids()))
    and (program_id is null or program_id in (select public.my_program_ids()))
  );

drop policy if exists rag_chunks_read on public.rag_chunks;
create policy rag_chunks_read on public.rag_chunks
  for select to authenticated
  using (
    public.is_internal()
    and (public.can_see_commercials() or not public.rag_chunk_is_commercial(id))
    and document_id in (select public.my_rag_document_ids())
    and document_id in (select d.id from public.rag_documents d
                        where d.corpus in (select public.my_rag_corpora()))
  );

-- --- storage.objects ---------------------------------------------------------
-- `try_uuid()` returns NULL for a malformed path. `can_reach_college(NULL)` is
-- FALSE; `NULL in (select ...)` is NULL against a non-empty set and FALSE
-- against an empty one. RLS denies on both FALSE and NULL, so a misfiled object
-- stays invisible either way — the same answer by a slightly different route,
-- which is worth stating rather than leaving for someone to rediscover.

drop policy if exists documents_internal_college_rw on storage.objects;
create policy documents_internal_college_rw on storage.objects
  for all to authenticated
  using (
    bucket_id = 'documents'
    and (storage.foldername(name))[1] = 'colleges'
    and public.is_internal()
    and public.try_uuid((storage.foldername(name))[2]) in (select public.my_college_ids())
  )
  with check (
    bucket_id = 'documents'
    and (storage.foldername(name))[1] = 'colleges'
    and public.is_internal()
    and public.try_uuid((storage.foldername(name))[2]) in (select public.my_college_ids())
  );

drop policy if exists documents_internal_program_rw on storage.objects;
create policy documents_internal_program_rw on storage.objects
  for all to authenticated
  using (
    bucket_id = 'documents'
    and (storage.foldername(name))[1] = 'programs'
    and public.is_internal()
    and public.try_uuid((storage.foldername(name))[2]) in (select public.my_program_ids())
  )
  with check (
    bucket_id = 'documents'
    and (storage.foldername(name))[1] = 'programs'
    and public.is_internal()
    and public.try_uuid((storage.foldername(name))[2]) in (select public.my_program_ids())
  );


-- =============================================================================
-- Part D — the college-facing views
-- =============================================================================
-- `security_invoker = false`, so the WHERE clause IS the access control (0800).
-- The staff branch of each one called `can_reach_college(pr.college_id)` once
-- per row of a multi-table join; the set form hoists it exactly as it does in a
-- policy. The College-persona branch (`pr.college_id = public.my_college_id()`)
-- is untouched — `my_college_id()` is a zero-argument SCALAR and is already
-- hoisted, so there is nothing to win there.
--
-- Every column, alias, join, lateral, filter and group-by below is copied
-- verbatim from `0800_college_views.sql`. `create or replace view` refuses a
-- changed column list or type, which makes that claim machine-checked rather
-- than a promise.

create or replace view public.college_program_progress
with (security_invoker = false) as
select
  pr.id                                   as program_id,
  pr.college_id,
  pr.name                                 as program_name,
  pr.type                                 as program_type,
  pr.stage,
  pr.start_date,
  pr.end_date,
  coalesce(b.batch_count, 0)              as batch_count,
  coalesce(b.student_count, 0)            as student_count,
  coalesce(t.tasks_total, 0)              as tasks_total,
  coalesce(t.tasks_done, 0)               as tasks_done,
  case
    when coalesce(t.tasks_total, 0) = 0 then null
    else round(100.0 * t.tasks_done / t.tasks_total, 1)
  end                                     as checklist_complete_pct
from public.programs pr
left join lateral (
  select
    count(distinct ba.id) as batch_count,
    count(st.id)          as student_count
  from public.batches ba
  left join public.students st on st.batch_id = ba.id
  where ba.program_id = pr.id
) b on true
left join lateral (
  select
    count(*)                                   as tasks_total,
    count(*) filter (where tk.status = 'done') as tasks_done
  from public.tasks tk
  where tk.program_id = pr.id
) t on true
-- THE SECURITY BOUNDARY. Do not remove either branch.
where pr.college_id = public.my_college_id()
   or (public.is_internal() and pr.college_id in (select public.my_college_ids()));

create or replace view public.college_attendance_summary
with (security_invoker = false) as
select
  ba.id                           as batch_id,
  ba.program_id,
  ba.name                         as batch_name,
  ba.branch,
  count(distinct ar.session_date) as sessions_recorded,
  min(ar.session_date)            as first_session,
  max(ar.session_date)            as last_session,
  count(ar.id)                    as marks_recorded,
  count(ar.id) filter (
    where ar.status in ('present', 'late')
  )                               as marks_attended,
  case
    when count(ar.id) = 0 then null
    else round(
      100.0 * count(ar.id) filter (where ar.status in ('present', 'late'))
            / count(ar.id), 1)
  end                             as attendance_pct
from public.batches ba
join public.programs pr on pr.id = ba.program_id
left join public.deployments d         on d.batch_id = ba.id
left join public.attendance_records ar on ar.deployment_id = d.id
-- THE SECURITY BOUNDARY. Do not remove either branch.
where pr.college_id = public.my_college_id()
   or (public.is_internal() and pr.college_id in (select public.my_college_ids()))
group by ba.id, ba.program_id, ba.name, ba.branch;

create or replace view public.college_governance_reports
with (security_invoker = false) as
select
  gr.id,
  gr.program_id,
  pr.name as program_name,
  gr.title,
  gr.url,
  gr.reporting_period_start,
  gr.reporting_period_end,
  gr.shared_with_college_at
from public.governance_reports gr
join public.programs pr on pr.id = gr.program_id
-- THE SECURITY BOUNDARY. The publish gate applies to everyone: an internal
-- reader querying THIS view is asking "what has the college got?", and the
-- honest answer excludes unshared drafts. Internal staff read drafts from the
-- base table, where their own policy governs.
where gr.shared_with_college_at is not null
  and (
    pr.college_id = public.my_college_id()
    or (public.is_internal() and pr.college_id in (select public.my_college_ids()))
  );
