-- =============================================================================
-- byteXL Ops Intelligence Platform — the CLAUDE.md §4 persona matrix, asserted
-- =============================================================================
-- This file is the R5 proof: "Row-level security is tested, not assumed. Every
-- persona boundary has a test that asserts a forbidden read returns zero rows."
-- Every "must not see" in §4 gets an explicit `= 0` assertion rather than being
-- left to inference.
--
-- MECHANISM
-- ---------
-- Each persona is impersonated exactly the way PostgREST does it: set the
-- `request.jwt.claims` GUC to {"sub": <uuid>, "role": "authenticated"}, then
-- `SET ROLE authenticated`. Nothing here mocks auth.uid() — the real helper
-- reads the real GUC, so the policies under test are the policies that ship.
--
-- The `SET ROLE` is why DATABASE_URL must be the SESSION pooler (port 5432).
-- The transaction pooler on 6543 does not preserve it across statements, which
-- would leave every query running as the BYPASSRLS owner and make all 300-odd
-- assertions below pass while proving precisely nothing. run_tests.py refuses
-- port 6543 for that reason.
--
-- SAFE TO RUN AGAINST A REAL PROJECT: the runner executes 00_isolate.sql,
-- 01_fixtures.sql and this file inside a SINGLE transaction that is rolled back
-- unconditionally — on pass, on failure, and on Ctrl-C.
--
-- Contains no psql meta-commands, so it runs through psycopg as well as psql.
--
-- Run via: python supabase/tests/run_tests.py
--
-- =============================================================================
-- FINDINGS — deliberately NOT asserted, because asserting them would freeze
--            behaviour this suite considers questionable. Raised with the schema
--            owner instead. Listed here so the next reader does not think they
--            were missed.
-- =============================================================================
--
-- F1. `documents_commercials_trainer_rw` (0900) gates the whole `trainers/`
--     folder on can_see_commercials() ALONE, with no reach conjunct. Every
--     Manager therefore reads every trainer's signed work order — including
--     trainers deployed only at colleges they do not cover. The `work_orders`
--     TABLE is correctly scoped by can_reach_program(); the DOCUMENT stating the
--     same rate is not. The migration argues this deliberately (a
--     sourced-but-undeployed trainer is reachable by nobody, so a reach-gated
--     policy could never file the work order in the first place), and the
--     argument holds for INSERT. It does not obviously hold for SELECT.
--     Observable below as: manager_b, who covers only college C, sees both
--     trainer folders.
--
-- F2. `colleges_internal_update` (0300) has no column guard, so `cluster_id` is
--     writable by any internal persona that reaches the college — including an
--     LDE Executive. That cannot widen the writer's own reach (an LDE holds a
--     direct college assignment, not a cluster one), so it is not a privilege
--     escalation. It can silently move a campus out of a Senior Manager's
--     cluster, i.e. remove oversight. profiles, tasks and deployments all narrow
--     columns with a BEFORE UPDATE trigger; colleges does not.
--
-- F3. `tasks_trainer_select_own` / `tasks_trainer_update_own` (0500) are keyed on
--     `owner_id = auth.uid()` with no persona conjunct. A college login named as
--     a task owner would read and update that task, contradicting §4's
--     "published artifacts only, read-only". No fixture creates that row and
--     seed.sql never assigns a template to the college persona, so it is latent
--     rather than live — but it is one bad INSERT away.
-- =============================================================================

create schema if not exists test;
grant usage on schema test to authenticated;

-- --- Assertion helpers -------------------------------------------------------
-- SECURITY INVOKER (the default) is essential and must stay that way: the
-- dynamic SQL has to execute as `authenticated` so that RLS applies. A SECURITY
-- DEFINER helper here would run as the owner, bypass the very thing under test,
-- and make every assertion pass vacuously.
--
-- Failures `raise exception`, which aborts the run and is caught by the Python
-- runner. Passes `raise notice`, which the runner streams through a notice
-- handler — that is why the suite prints progressively rather than at the end.

create or replace function test.section(p_title text)
returns void
language plpgsql
as $$
begin
  raise notice '%', '';
  raise notice '%', p_title;
end;
$$;

create or replace function test.assert_count(p_label text, p_sql text, p_expected bigint)
returns void
language plpgsql
as $$
declare
  actual bigint;
begin
  execute p_sql into actual;
  if actual is distinct from p_expected then
    raise exception 'FAIL  % — expected %, got %', p_label, p_expected, actual;
  end if;
  raise notice '  ok  % = %', p_label, actual;
end;
$$;

create or replace function test.assert_value(p_label text, p_sql text, p_expected text)
returns void
language plpgsql
as $$
declare
  actual text;
begin
  execute p_sql into actual;
  if actual is distinct from p_expected then
    raise exception 'FAIL  % — expected %, got %', p_label, p_expected, coalesce(actual, '<null>');
  end if;
  raise notice '  ok  % = %', p_label, actual;
end;
$$;

-- A write is correctly blocked either by RLS filtering it to zero rows or by an
-- outright rejection (a WITH CHECK violation, a column-guard trigger, or a CHECK
-- constraint). Both count as a pass; silently succeeding does not.
create or replace function test.assert_no_write(p_label text, p_sql text)
returns void
language plpgsql
as $$
declare
  n       bigint := -1;
  blocked boolean := false;
  reason  text;
begin
  begin
    execute p_sql;
    get diagnostics n = row_count;
    if n = 0 then
      blocked := true;
      reason  := 'filtered to 0 rows';
    end if;
  exception when others then
    blocked := true;
    reason  := 'rejected: ' || sqlerrm;
  end;

  if not blocked then
    raise exception 'FAIL  % — write SUCCEEDED (% rows affected)', p_label, n;
  end if;
  raise notice '  ok  % (%)', p_label, reason;
end;
$$;

create or replace function test.assert_writes(p_label text, p_sql text, p_expected bigint)
returns void
language plpgsql
as $$
declare
  n bigint;
begin
  execute p_sql;
  get diagnostics n = row_count;
  if n is distinct from p_expected then
    raise exception 'FAIL  % — expected % rows affected, got %', p_label, p_expected, n;
  end if;
  raise notice '  ok  % (% rows)', p_label, n;
end;
$$;

create or replace function test.login(p_user uuid)
returns void
language plpgsql
as $$
begin
  perform set_config(
    'request.jwt.claims',
    json_build_object('sub', p_user, 'role', 'authenticated')::text,
    false
  );
end;
$$;

create or replace function test.logout()
returns void
language plpgsql
as $$
begin
  perform set_config('request.jwt.claims', '', false);
end;
$$;

grant execute on all functions in schema test to authenticated;


-- =============================================================================
-- LATE FIXTURES — 1300 and 1400
-- =============================================================================
-- 01_fixtures.sql predates all three of these tables and is owned by another
-- workstream, so the rows the assertions below need are created here instead.
-- Same transaction, same rollback, and they are loaded before the first
-- `set role`, i.e. as the BYPASSRLS owner exactly as 01_fixtures.sql is.
--
-- Kept deliberately small: one row per side of each boundary being asserted, and
-- no row that any existing unscoped count would have to be re-derived for. The
-- three tables are new, so every count over them is new too.

-- --- 1400: payment rails, one per trainer ------------------------------------
-- Trainer A is deployed at college A (which LDE-A1 fully reaches), which is what
-- makes the wall assertion below meaningful: the roster row IS visible to that
-- persona and the rails are not.
insert into public.trainer_bank_accounts
  (trainer_id, bank_account_number, ifsc, bank_name, branch, account_name) values
  ('d0000000-0000-0000-0000-00000000000a', '50100123456789', 'HDFC0001234',
   'HDFC Bank', 'Guntur', 'ANITA SHARMA'),
  ('d0000000-0000-0000-0000-00000000000b', '38200987654321', 'SBIN0004567',
   'State Bank of India', 'Vijayawada', 'BALA KRISHNAN');

-- --- 1300: one artifact version per side of the commercial split -------------
-- A remuneration version (walled) and a governance version (not walled), both
-- hanging off program PA — so LDE-A1 reaches the PROGRAM for both and only
-- can_see_commercials() separates them.
insert into public.artifact_versions
  (id, artifact_type, artifact_id, version, state, created_by) values
  ('a5000000-0000-0000-0000-0000000000a1', 'remuneration_sheets',
   '4e000000-0000-0000-0000-0000000000a1', 1, 'DRAFT',
   'f0000000-0000-0000-0000-000000000002'),
  ('a5000000-0000-0000-0000-0000000000a2', 'governance_reports',
   '61000000-0000-0000-0000-0000000000a1', 1, 'DRAFT',
   'f0000000-0000-0000-0000-000000000002');

-- --- 1300: two audit rows by two different actors ----------------------------
-- One written by LDE-A1 themselves and one by Manager A, which is the whole
-- boundary on this table: you read your own rows, an admin reads all of them,
-- and nobody reads somebody else's.
insert into public.audit_events
  (id, actor_id, actor_persona, action, entity_table, entity_id, before, after) values
  ('a6000000-0000-0000-0000-0000000000a1', 'f0000000-0000-0000-0000-000000000004',
   'lde_executive', 'task.completed', 'tasks',
   '70000000-0000-0000-0000-0000000000a1', null, '{"status": "done"}'),
  ('a6000000-0000-0000-0000-0000000000a2', 'f0000000-0000-0000-0000-000000000002',
   'manager', 'payout.validated', 'remuneration_sheets',
   '4e000000-0000-0000-0000-0000000000a1', null, '{"can_submit": false}');


-- =============================================================================
select test.section('=== SENIOR MANAGER — cluster 1 reach, and its boundary ===');
-- =============================================================================
-- §4: "All programs in cluster. P&L, escalations, payout approval."
-- Assigned to cluster 1 only, and NOT an admin — which is what makes the
-- privilege-guard assertions later in this file meaningful.
select test.login('f0000000-0000-0000-0000-000000000001');
set role authenticated;

select test.assert_value('persona is senior_manager', 'select public.app_role()::text', 'senior_manager');
select test.assert_value('  ...and is NOT an admin',   'select public.is_admin()::text', 'false');
select test.assert_value('  ...but may see commercials','select public.can_see_commercials()::text', 'true');

-- Cluster expansion: one cluster assignment reaches two colleges.
select test.assert_count('SM: my_college_ids expands the cluster',
  'select count(*) from public.my_college_ids()', 2);
select test.assert_count('SM sees cluster 1 colleges',   'select count(*) from public.colleges', 2);
select test.assert_count('SM sees NO college C (cluster 2)',
  'select count(*) from public.colleges where id = ''c0000000-0000-0000-0000-00000000000c''', 0);

select test.assert_count('SM sees cluster 1 programs',   'select count(*) from public.programs', 2);
select test.assert_count('SM sees NO program PC',
  'select count(*) from public.programs where id = ''a1000000-0000-0000-0000-00000000000c''', 0);
select test.assert_count('SM sees cluster 1 batches',    'select count(*) from public.batches', 3);
select test.assert_count('SM sees cluster 1 students',   'select count(*) from public.students', 7);
select test.assert_count('SM sees the trainer roster',   'select count(*) from public.trainers', 2);
select test.assert_count('SM sees cluster 1 deployments','select count(*) from public.deployments', 2);
select test.assert_count('SM sees cluster 1 tasks',      'select count(*) from public.tasks', 6);
select test.assert_count('SM sees cluster 1 urgency',    'select count(*) from public.task_urgency', 6);
select test.assert_count('SM: 2 tasks done',
  'select count(*) from public.task_urgency where urgency = ''done''', 2);
select test.assert_count('SM: every urgency row is banded',
  'select count(*) from public.task_urgency where urgency is null', 0);
select test.assert_count('SM sees observations',        'select count(*) from public.observations', 1);
select test.assert_count('SM sees feedback',            'select count(*) from public.feedback', 1);
select test.assert_count('SM sees assessments',         'select count(*) from public.assessments', 2);
select test.assert_count('SM sees student attendance',  'select count(*) from public.attendance_records', 8);
select test.assert_count('SM sees trainer attendance',  'select count(*) from public.trainer_attendance', 5);

-- The org chart is readable by all internal staff (0300 documents this).
select test.assert_count('SM sees all profiles',        'select count(*) from public.profiles', 11);
select test.assert_count('SM sees all clusters',        'select count(*) from public.clusters', 2);
select test.assert_count('SM sees all college assignments',
  'select count(*) from public.user_college_assignments', 5);
select test.assert_count('SM sees all cluster assignments',
  'select count(*) from public.user_cluster_assignments', 1);

-- --- COMMERCIALS: allowed, but still bounded by reach ------------------------
select test.assert_count('SM sees cluster 1 work orders','select count(*) from public.work_orders', 2);
select test.assert_count('SM sees cluster 1 remuneration',
  'select count(*) from public.remuneration_sheets', 2);
select test.assert_count('SM sees cluster 1 P&L',       'select count(*) from public.pnl', 2);
-- THE CLUSTER BOUNDARY ON MONEY. Both conjuncts of the money policy are live:
-- the wall passes, the scope does not.
select test.assert_count('SM sees NO cluster 2 P&L',
  'select count(*) from public.pnl where program_id = ''a1000000-0000-0000-0000-00000000000c''', 0);

select test.assert_count('SM sees cluster 1 governance', 'select count(*) from public.governance_reports', 3);
select test.assert_count('SM sees the unshared drafts',
  'select count(*) from public.governance_reports where shared_with_college_at is null', 2);
select test.assert_count('SM sees cluster 1 program documents',
  'select count(*) from public.program_documents', 5);

-- 1400 — payment rails. NOT bounded by reach, and the count says so: both
-- trainers, including trainer B, who is deployed at college B. The migration
-- argues this deliberately (rails are collected at onboarding, before any
-- deployment exists, so a reach conjunct could never file the first one) and it
-- mirrors trainers_sourcing_all rather than work_orders. Asserted at 2 rather
-- than 1 so that adding a reach conjunct later has to break this line to land.
select test.assert_count('SM sees both trainers'' bank rails',
  'select count(*) from public.trainer_bank_accounts', 2);
select test.assert_value('  ...and reads the account number',
  'select bank_account_number from public.trainer_bank_accounts
     where trainer_id = ''d0000000-0000-0000-0000-00000000000a''', '50100123456789');

-- 1300 — artifact versions, both sides of the commercial split, within reach.
select test.assert_count('SM sees both artifact versions',
  'select count(*) from public.artifact_versions', 2);
-- ...and reach is NOT a key to the audit trail. This Senior Manager is not an
-- admin and wrote none of the fixture rows, so 1300's two policies — admin, or
-- your own rows — both return nothing. Payout approval authority is not the same
-- thing as reading who else did what.
select test.assert_count('SM sees NO audit events (not an admin, wrote none)',
  'select count(*) from public.audit_events', 0);

-- 1700 — the outbound queue. Cluster 1 only, and both sides of the wall, since
-- this persona passes can_see_commercials().
select test.assert_count('SM sees cluster 1 comms messages',
  'select count(*) from public.comms_messages', 2);
select test.assert_count('SM sees NO college C comms message',
  'select count(*) from public.comms_messages
     where program_id = ''a1000000-0000-0000-0000-00000000000c''', 0);

-- --- SEED INTEGRITY ----------------------------------------------------------
-- Asserted from an internal persona because task_templates / document_templates
-- are internal-read. These counts are DELIBERATELY BRITTLE: they exist so that
-- editing supabase/seed.sql cannot happen silently. If you changed the seed on
-- purpose, change the number here in the same commit.
select test.assert_count('seed: 37 task templates',     'select count(*) from public.task_templates', 37);
select test.assert_count('seed: 6 stages covered',
  'select count(distinct stage) from public.task_templates', 6);
select test.assert_count('seed: templates daily',
  'select count(*) from public.task_templates where cadence = ''daily''', 1);
select test.assert_count('seed: templates weekly',
  'select count(*) from public.task_templates where cadence = ''weekly''', 2);
select test.assert_count('seed: templates monthly',
  'select count(*) from public.task_templates where cadence = ''monthly''', 9);
select test.assert_count('seed: templates quarterly',
  'select count(*) from public.task_templates where cadence = ''quarterly''', 3);
select test.assert_count('seed: templates one-time',
  'select count(*) from public.task_templates where cadence = ''one_time''', 22);

-- Every template must carry a schedule offset. A NULL here silently produces a
-- task with no due_date, which never surfaces in any urgency band — the failure
-- 1000_scheduling_documents.sql exists to prevent.
select test.assert_count('seed: every template is scheduled',
  'select count(*) from public.task_templates where offset_days is null', 0);
select test.assert_count('seed: closeout anchors to program_end',
  'select count(*) from public.task_templates where offset_anchor = ''program_end''', 6);
select test.assert_count('seed: nothing anchors closeout to program_start',
  'select count(*) from public.task_templates
     where stage = ''closeout_finance'' and offset_anchor <> ''program_end''', 0);

-- The responsibility matrix. §4 gives the LDE Executive no commercials, so no
-- closeout_finance template may be owned by one; and the college persona owns no
-- checklist item at all (it holds no UPDATE policy anywhere, so such a task
-- could never be completed).
select test.assert_count('seed: no closeout template owned by an LDE',
  'select count(*) from public.task_templates
     where stage = ''closeout_finance'' and default_owner_role = ''lde_executive''', 0);
select test.assert_count('seed: no template owned by the college persona',
  'select count(*) from public.task_templates where default_owner_role = ''college''', 0);

select test.assert_count('seed: 37 document templates',
  'select count(*) from public.document_templates', 37);
select test.assert_count('seed: 11 document categories in use',
  'select count(distinct category) from public.document_templates', 11);
select test.assert_count('seed: every document template maps to a stage',
  'select count(*) from public.document_templates where stage is null', 0);

-- --- Storage -----------------------------------------------------------------
-- Positive counts are scoped to the fixture path prefixes: 00_isolate.sql cannot
-- clear storage.objects, so a real upload in the bucket would otherwise skew
-- them. Every ZERO-row storage assertion in this file is unscoped.
select test.assert_count('SM sees cluster 1 + trainer documents',
  'select count(*) from storage.objects where bucket_id = ''documents''
     and (name like ''colleges/c0000000-%'' or name like ''trainers/d0000000-%''
          or name like ''programs/a1000000-%'')', 5);
select test.assert_count('SM sees NO college C documents',
  'select count(*) from storage.objects
     where name like ''colleges/c0000000-0000-0000-0000-00000000000c/%''', 0);

reset role;


-- =============================================================================
select test.section('=== MANAGER A — two colleges, one manager (§4 reach model) ===');
-- =============================================================================
-- The assignment table exists so a Manager can cover many colleges. manager_a
-- holds two rows in user_college_assignments and no cluster assignment.
select test.login('f0000000-0000-0000-0000-000000000002');
set role authenticated;

select test.assert_value('persona is manager',           'select public.app_role()::text', 'manager');
select test.assert_value('  ...may see commercials',     'select public.can_see_commercials()::text', 'true');
select test.assert_count('MGR-A reaches two colleges',
  'select count(*) from public.my_college_ids()', 2);
select test.assert_count('MGR-A sees colleges A and B',  'select count(*) from public.colleges', 2);
select test.assert_count('MGR-A sees programs PA + PB',  'select count(*) from public.programs', 2);
select test.assert_count('MGR-A sees batches across both','select count(*) from public.batches', 3);
select test.assert_count('MGR-A sees students across both','select count(*) from public.students', 7);
select test.assert_count('MGR-A sees both deployments',  'select count(*) from public.deployments', 2);
select test.assert_count('MGR-A sees tasks across both', 'select count(*) from public.tasks', 6);
select test.assert_count('MGR-A sees trainer attendance','select count(*) from public.trainer_attendance', 5);
select test.assert_count('MGR-A sees work orders',       'select count(*) from public.work_orders', 2);
select test.assert_count('MGR-A sees remuneration',      'select count(*) from public.remuneration_sheets', 2);
select test.assert_count('MGR-A sees P&L for A and B',   'select count(*) from public.pnl', 2);
select test.assert_count('MGR-A sees program documents (incl. commercial)',
  'select count(*) from public.program_documents', 5);

-- ---- EVERY COLLEGE C ROW IS ZERO -------------------------------------------
select test.assert_count('MGR-A sees NO college C',
  'select count(*) from public.colleges where id = ''c0000000-0000-0000-0000-00000000000c''', 0);
select test.assert_count('MGR-A sees NO program PC',
  'select count(*) from public.programs where college_id = ''c0000000-0000-0000-0000-00000000000c''', 0);
select test.assert_count('MGR-A sees NO college C batches',
  'select count(*) from public.batches where program_id = ''a1000000-0000-0000-0000-00000000000c''', 0);
select test.assert_count('MGR-A sees NO college C students',
  'select count(*) from public.students where batch_id = ''b1000000-0000-0000-0000-0000000000c1''', 0);
select test.assert_count('MGR-A sees NO college C tasks',
  'select count(*) from public.tasks where program_id = ''a1000000-0000-0000-0000-00000000000c''', 0);
-- CLUSTER 2 COMMERCIALS. The wall passes for a Manager; the scope must not.
select test.assert_count('MGR-A sees NO cluster 2 P&L',
  'select count(*) from public.pnl where program_id = ''a1000000-0000-0000-0000-00000000000c''', 0);
select test.assert_count('MGR-A sees NO college C governance',
  'select count(*) from public.governance_reports where program_id = ''a1000000-0000-0000-0000-00000000000c''', 0);
select test.assert_count('MGR-A sees NO college C documents (register)',
  'select count(*) from public.program_documents where program_id = ''a1000000-0000-0000-0000-00000000000c''', 0);
select test.assert_count('MGR-A sees NO college C documents (storage)',
  'select count(*) from storage.objects
     where name like ''colleges/c0000000-0000-0000-0000-00000000000c/%''', 0);

reset role;


-- =============================================================================
select test.section('=== MANAGER B — college C only; the mirror negatives ===');
-- =============================================================================
select test.login('f0000000-0000-0000-0000-000000000003');
set role authenticated;

select test.assert_count('MGR-B sees college C only',    'select count(*) from public.colleges', 1);
select test.assert_value('  ...and it is Gamma',
  'select name from public.colleges', 'Gamma College of Science');
select test.assert_count('MGR-B sees program PC only',   'select count(*) from public.programs', 1);
select test.assert_count('MGR-B sees batch C1 only',     'select count(*) from public.batches', 1);
select test.assert_count('MGR-B sees 1 student',         'select count(*) from public.students', 1);
select test.assert_count('MGR-B sees PC tasks',          'select count(*) from public.tasks', 2);
select test.assert_count('MGR-B sees PC P&L',            'select count(*) from public.pnl', 1);
select test.assert_count('MGR-B sees PC governance',     'select count(*) from public.governance_reports', 1);
select test.assert_count('MGR-B sees PC documents',      'select count(*) from public.program_documents', 1);

-- ---- COLLEGES A AND B ARE ZERO IN EVERY DIRECTION ---------------------------
select test.assert_count('MGR-B sees NO college A or B',
  'select count(*) from public.colleges
     where id in (''c0000000-0000-0000-0000-00000000000a'', ''c0000000-0000-0000-0000-00000000000b'')', 0);
select test.assert_count('MGR-B sees NO program PA or PB',
  'select count(*) from public.programs
     where id in (''a1000000-0000-0000-0000-00000000000a'', ''a1000000-0000-0000-0000-00000000000b'')', 0);
select test.assert_count('MGR-B sees NO batches A1/A2/B1',
  'select count(*) from public.batches
     where id <> ''b1000000-0000-0000-0000-0000000000c1''', 0);
select test.assert_count('MGR-B sees NO deployments',    'select count(*) from public.deployments', 0);
select test.assert_count('MGR-B sees NO trainer attendance',
  'select count(*) from public.trainer_attendance', 0);
select test.assert_count('MGR-B sees NO student attendance',
  'select count(*) from public.attendance_records', 0);
select test.assert_count('MGR-B sees NO observations',   'select count(*) from public.observations', 0);
select test.assert_count('MGR-B sees NO feedback',       'select count(*) from public.feedback', 0);
select test.assert_count('MGR-B sees NO assessments',    'select count(*) from public.assessments', 0);
-- The commercials themselves: the wall passes for a Manager, so ONLY reach is
-- keeping college A and B's money out of view here.
select test.assert_count('MGR-B sees NO work orders',    'select count(*) from public.work_orders', 0);
select test.assert_count('MGR-B sees NO remuneration',   'select count(*) from public.remuneration_sheets', 0);
select test.assert_count('MGR-B sees NO cluster 1 P&L',
  'select count(*) from public.pnl
     where program_id <> ''a1000000-0000-0000-0000-00000000000c''', 0);
select test.assert_count('MGR-B sees NO cluster 1 governance',
  'select count(*) from public.governance_reports
     where program_id <> ''a1000000-0000-0000-0000-00000000000c''', 0);
select test.assert_count('MGR-B sees NO cluster 1 documents (storage)',
  'select count(*) from storage.objects
     where name like ''colleges/c0000000-0000-0000-0000-00000000000a/%''
        or name like ''colleges/c0000000-0000-0000-0000-00000000000b/%''
        or name like ''programs/a1000000-%''', 0);

-- The trainer ROSTER is deliberately not college-scoped: sourcing happens before
-- any deployment exists (0400). Asserted so the widening is a choice on record.
select test.assert_count('MGR-B sees the whole trainer roster (by design, 0400)',
  'select count(*) from public.trainers', 2);
-- FINDING F1, observable: the trainers/ storage folder is gated on the
-- commercials wall ALONE, so this Manager reads signed work orders for trainers
-- at colleges they do not cover.
select test.assert_count('MGR-B sees both trainer folders (FINDING F1)',
  'select count(*) from storage.objects where name like ''trainers/d0000000-%''', 2);

reset role;


-- =============================================================================
select test.section('=== LDE EXECUTIVE A1 — THE COMMERCIALS WALL ===');
-- =============================================================================
-- The single most important section in this file. CLAUDE.md §4 and R5:
--
--   "An LDE Executive gets ZERO ROWS from pnl, remuneration, invoices and
--    work-order rates, in the database rather than in the UI."
--
-- Every money assertion below is run against a college this LDE FULLY REACHES.
-- That is the whole point: the scope conjunct of the money policy is satisfied,
-- and can_see_commercials() is the only thing returning zero.
select test.login('f0000000-0000-0000-0000-000000000004');
set role authenticated;

select test.assert_value('persona is lde_executive',      'select public.app_role()::text', 'lde_executive');
select test.assert_value('  ...is internal',              'select public.is_internal()::text', 'true');
select test.assert_value('  ...MAY NOT see commercials',  'select public.can_see_commercials()::text', 'false');
select test.assert_value('  ...reaches college A',
  'select public.can_reach_college(''c0000000-0000-0000-0000-00000000000a'')::text', 'true');
select test.assert_value('  ...reaches program PA',
  'select public.can_reach_program(''a1000000-0000-0000-0000-00000000000a'')::text', 'true');

-- --- The campus job: §4 gives them attendance, batches, daily tasks ----------
select test.assert_count('LDE-A1 sees own college',      'select count(*) from public.colleges', 1);
select test.assert_count('LDE-A1 sees own program',      'select count(*) from public.programs', 1);
select test.assert_count('LDE-A1 sees both PA batches',  'select count(*) from public.batches', 2);
select test.assert_count('LDE-A1 sees PA students',      'select count(*) from public.students', 5);
select test.assert_count('LDE-A1 sees the deployment',   'select count(*) from public.deployments', 1);
select test.assert_count('LDE-A1 sees PA tasks',         'select count(*) from public.tasks', 4);
select test.assert_count('LDE-A1 sees PA urgency',       'select count(*) from public.task_urgency', 4);
select test.assert_count('LDE-A1 sees student attendance','select count(*) from public.attendance_records', 6);
select test.assert_count('LDE-A1 sees trainer attendance','select count(*) from public.trainer_attendance', 3);
select test.assert_count('LDE-A1 sees observations',     'select count(*) from public.observations', 1);
select test.assert_count('LDE-A1 sees feedback',         'select count(*) from public.feedback', 1);
select test.assert_count('LDE-A1 sees PA assessments',   'select count(*) from public.assessments', 1);
select test.assert_count('LDE-A1 sees governance (not commercial)',
  'select count(*) from public.governance_reports', 2);
select test.assert_count('LDE-A1 reads the template libraries',
  'select count(*) from public.task_templates', 37);
select test.assert_count('LDE-A1 reads the document library',
  'select count(*) from public.document_templates', 37);
-- Trainer roster is narrowed for this persona: only trainers deployed to a
-- college they reach, not every freelancer byteXL has engaged.
select test.assert_count('LDE-A1 sees only the deployed trainer',
  'select count(*) from public.trainers', 1);
select test.assert_value('  ...and it is trainer A',
  'select full_name from public.trainers', 'Anita Sharma');

-- ============ ZERO ROWS FROM EVERY MONEY SURFACE ============================
-- 1700: the wall on comms_messages is a property of the ROW, not the table. This
-- LDE FULLY REACHES PA, so the scope conjunct of both policies is satisfied and
-- `is_commercial` is the only thing returning zero — which is exactly the case
-- worth asserting (CLAUDE.md R5, §8).
select test.assert_count('WALL: LDE-A1 sees NO commercial comms message',
  'select count(*) from public.comms_messages where is_commercial', 0);
select test.assert_count('WALL: LDE-A1 sees NO payout message for their OWN program',
  'select count(*) from public.comms_messages
     where program_id = ''a1000000-0000-0000-0000-00000000000a'' and is_commercial', 0);
select test.assert_count('LDE-A1 DOES see the operational comms message',
  'select count(*) from public.comms_messages', 1);

select test.assert_count('WALL: LDE-A1 sees NO pnl',
  'select count(*) from public.pnl', 0);
select test.assert_count('WALL: LDE-A1 sees NO pnl for their OWN program',
  'select count(*) from public.pnl where program_id = ''a1000000-0000-0000-0000-00000000000a''', 0);
select test.assert_count('WALL: LDE-A1 sees NO remuneration_sheets',
  'select count(*) from public.remuneration_sheets', 0);
select test.assert_count('WALL: LDE-A1 sees NO sheet for their OWN trainer',
  'select count(*) from public.remuneration_sheets
     where trainer_id = ''d0000000-0000-0000-0000-00000000000a''', 0);
select test.assert_count('WALL: LDE-A1 sees NO work orders',
  'select count(*) from public.work_orders', 0);
-- The rate column specifically. work_orders is walled as a whole table, so this
-- is the same zero from a different direction — asserted because §4 names
-- "work-order rates" explicitly and a future column-level relaxation would have
-- to break this line to land.
select test.assert_count('WALL: LDE-A1 reads NO work-order rate',
  'select count(*) from public.work_orders where rate is not null', 0);
select test.assert_count('WALL: LDE-A1 sees NO invoice numbers',
  'select count(*) from public.remuneration_sheets where invoice_no is not null', 0);
-- program_documents is the one table where the wall is a CATEGORY filter rather
-- than a table boundary: full reach to PA, 4 rows on PA, exactly 2 visible.
select test.assert_count('WALL: LDE-A1 sees only operational documents',
  'select count(*) from public.program_documents', 2);
select test.assert_count('WALL: LDE-A1 sees NO remuneration documents',
  'select count(*) from public.program_documents where category = ''remuneration''', 0);
select test.assert_count('WALL: LDE-A1 sees NO invoice documents',
  'select count(*) from public.program_documents where category = ''invoice_generation''', 0);
-- 1400 — THE BANK RAILS. The sharpest case in this section, and the reason the
-- rails are their own table: this persona legitimately reads trainer A's roster
-- row three assertions above (name, phone, work-order status — the campus job),
-- and reads ZERO of their payment rails. RLS is row-level, so five columns on
-- `trainers` would have arrived on that same SELECT with nothing able to stop
-- them. Scoped to the trainer they DO see, so the zero cannot be an artefact of
-- reach.
select test.assert_count('WALL: LDE-A1 sees NO bank rails',
  'select count(*) from public.trainer_bank_accounts', 0);
select test.assert_count('WALL: LDE-A1 sees NO rails for the trainer they DO see',
  'select count(*) from public.trainer_bank_accounts
     where trainer_id = ''d0000000-0000-0000-0000-00000000000a''', 0);
select test.assert_count('WALL: LDE-A1 reads NO account number',
  'select count(*) from public.trainer_bank_accounts where bank_account_number is not null', 0);

-- 1300 — the wall is a property of the ROW here: same table, same program, one
-- commercial artifact and one not.
select test.assert_count('WALL: LDE-A1 sees NO remuneration artifact versions',
  'select count(*) from public.artifact_versions
     where artifact_type = ''remuneration_sheets''', 0);
select test.assert_count('LDE-A1 DOES see governance artifact versions',
  'select count(*) from public.artifact_versions
     where artifact_type = ''governance_reports''', 1);
-- The audit trail is a governance surface, not a scoped one: this persona reads
-- the row they wrote and no other.
select test.assert_count('LDE-A1 sees their own audit events',
  'select count(*) from public.audit_events', 1);
select test.assert_count('WALL: LDE-A1 sees NO audit events but their own',
  'select count(*) from public.audit_events
     where actor_id is distinct from ''f0000000-0000-0000-0000-000000000004''', 0);

-- Storage: the trainers/ folder holds signed work orders, which state the rate.
select test.assert_count('WALL: LDE-A1 sees NO trainer documents',
  'select count(*) from storage.objects where name like ''trainers/%''', 0);
select test.assert_count('LDE-A1 DOES see their college + program documents',
  'select count(*) from storage.objects where bucket_id = ''documents''
     and (name like ''colleges/c0000000-%'' or name like ''programs/a1000000-%'')', 2);

-- ============ CROSS-CAMPUS: COLLEGE B IS ZERO ===============================
select test.assert_count('LDE-A1 sees NO college B',
  'select count(*) from public.colleges where id = ''c0000000-0000-0000-0000-00000000000b''', 0);
select test.assert_count('LDE-A1 sees NO college B program',
  'select count(*) from public.programs where college_id = ''c0000000-0000-0000-0000-00000000000b''', 0);
select test.assert_count('LDE-A1 sees NO college B batches',
  'select count(*) from public.batches where program_id = ''a1000000-0000-0000-0000-00000000000b''', 0);
select test.assert_count('LDE-A1 sees NO college B tasks',
  'select count(*) from public.tasks where program_id = ''a1000000-0000-0000-0000-00000000000b''', 0);
select test.assert_count('LDE-A1 sees NO college B attendance',
  'select count(*) from public.attendance_records
     where deployment_id = ''e0000000-0000-0000-0000-00000000000b''', 0);
select test.assert_count('LDE-A1 sees NO college B trainer attendance',
  'select count(*) from public.trainer_attendance
     where deployment_id = ''e0000000-0000-0000-0000-00000000000b''', 0);
select test.assert_count('LDE-A1 sees NO college B deployment',
  'select count(*) from public.deployments
     where id = ''e0000000-0000-0000-0000-00000000000b''', 0);
select test.assert_count('LDE-A1 sees NO college B students',
  'select count(*) from public.students where batch_id = ''b1000000-0000-0000-0000-0000000000b1''', 0);
select test.assert_count('LDE-A1 sees NO college B documents',
  'select count(*) from storage.objects
     where name like ''colleges/c0000000-0000-0000-0000-00000000000b/%''', 0);

-- --- The curated views, read by internal staff -------------------------------
-- 0800 exists partly so a Manager can see EXACTLY what the college sees. Scoped
-- by reach through the second branch of the views' WHERE clause.
select test.assert_count('LDE-A1 view: own program only',
  'select count(*) from public.college_program_progress', 1);
select test.assert_count('LDE-A1 view: own batches only',
  'select count(*) from public.college_attendance_summary', 2);
select test.assert_count('LDE-A1 view: only SHARED governance',
  'select count(*) from public.college_governance_reports', 1);

reset role;


-- =============================================================================
select test.section('=== LDE EXECUTIVE A2 — the other campus, mutually isolated ===');
-- =============================================================================
select test.login('f0000000-0000-0000-0000-000000000005');
set role authenticated;

select test.assert_count('LDE-A2 sees college B only',   'select count(*) from public.colleges', 1);
select test.assert_value('  ...and it is Beta',
  'select name from public.colleges', 'Beta College of Engineering');
select test.assert_count('LDE-A2 sees batch B1 only',    'select count(*) from public.batches', 1);
select test.assert_count('LDE-A2 sees PB tasks',         'select count(*) from public.tasks', 2);
select test.assert_count('LDE-A2 sees own trainer attendance',
  'select count(*) from public.trainer_attendance', 2);
select test.assert_count('LDE-A2 sees only trainer B',   'select count(*) from public.trainers', 1);

-- Mirror of the A1 wall, so the boundary is not an artefact of one fixture.
select test.assert_count('WALL: LDE-A2 sees NO pnl',     'select count(*) from public.pnl', 0);
select test.assert_count('WALL: LDE-A2 sees NO remuneration',
  'select count(*) from public.remuneration_sheets', 0);
select test.assert_count('WALL: LDE-A2 sees NO work orders',
  'select count(*) from public.work_orders', 0);
select test.assert_count('WALL: LDE-A2 sees NO bank rails',
  'select count(*) from public.trainer_bank_accounts', 0);
select test.assert_count('WALL: LDE-A2 sees NO trainer documents',
  'select count(*) from storage.objects where name like ''trainers/%''', 0);

-- Campus A is invisible to campus B's executive.
select test.assert_count('LDE-A2 sees NO college A',
  'select count(*) from public.colleges where id = ''c0000000-0000-0000-0000-00000000000a''', 0);
select test.assert_count('LDE-A2 sees NO PA batches',
  'select count(*) from public.batches where program_id = ''a1000000-0000-0000-0000-00000000000a''', 0);
select test.assert_count('LDE-A2 sees NO PA tasks',
  'select count(*) from public.tasks where program_id = ''a1000000-0000-0000-0000-00000000000a''', 0);
select test.assert_count('LDE-A2 sees NO PA observations',
  'select count(*) from public.observations', 0);
select test.assert_count('LDE-A2 sees NO PA feedback',
  'select count(*) from public.feedback', 0);

reset role;


-- =============================================================================
select test.section('=== TRAINER A — own deployment, and nothing else (§4) ===');
select test.login('f0000000-0000-0000-0000-00000000000a');
set role authenticated;

-- =============================================================================
-- Trainers are RECORDS, not users (owner decision 2026-08-18, migration 1800).
-- Every trainer policy in the schema was dropped, which makes this persona the
-- strongest negative case in the matrix: it authenticates successfully and then
-- reads NOTHING and writes NOTHING, anywhere.
--
-- These assertions used to be positive - "TRN-A sees own deployment = 1" and so
-- on. They were INVERTED rather than deleted, deliberately. A deleted assertion
-- proves nothing; an inverted one fails loudly the day somebody reinstates a
-- trainer policy without meaning to.
--
-- `trainer` also remains the deny-by-default sentinel for a malformed signup
-- (`handle_new_user` does coalesce(requested_role, 'trainer')), so what is
-- proved below is exactly what that fallback is now worth: nothing at all.
-- =============================================================================
select test.assert_value('persona is trainer',           'select public.app_role()::text', 'trainer');
select test.assert_value('  ...is NOT internal',         'select public.is_internal()::text', 'false');
select test.assert_value('  ...MAY NOT see commercials', 'select public.can_see_commercials()::text', 'false');

-- profiles_self_select (0200) is NOT a trainer policy - every authenticated user
-- reads their own row - so this one legitimately stays at 1.
select test.assert_count('TRN-A still sees own profile row', 'select count(*) from public.profiles', 1);

select test.assert_count('WALL: TRN-A sees NO trainers',            'select count(*) from public.trainers', 0);
select test.assert_count('WALL: TRN-A sees NO deployments',         'select count(*) from public.deployments', 0);
select test.assert_count('WALL: TRN-A sees NO colleges',            'select count(*) from public.colleges', 0);
select test.assert_count('WALL: TRN-A sees NO programs',            'select count(*) from public.programs', 0);
select test.assert_count('WALL: TRN-A sees NO batches',             'select count(*) from public.batches', 0);
select test.assert_count('WALL: TRN-A sees NO students',            'select count(*) from public.students', 0);
select test.assert_count('WALL: TRN-A sees NO tasks',               'select count(*) from public.tasks', 0);
select test.assert_count('WALL: TRN-A sees NO task_urgency',        'select count(*) from public.task_urgency', 0);
select test.assert_count('WALL: TRN-A sees NO attendance_records',  'select count(*) from public.attendance_records', 0);
select test.assert_count('WALL: TRN-A sees NO trainer_attendance',  'select count(*) from public.trainer_attendance', 0);
select test.assert_count('WALL: TRN-A sees NO assessments',         'select count(*) from public.assessments', 0);
select test.assert_count('WALL: TRN-A sees NO work_orders',         'select count(*) from public.work_orders', 0);
select test.assert_count('WALL: TRN-A sees NO remuneration_sheets', 'select count(*) from public.remuneration_sheets', 0);
select test.assert_count('WALL: TRN-A sees NO bank rails',          'select count(*) from public.trainer_bank_accounts', 0);
select test.assert_count('WALL: TRN-A sees NO pnl',                 'select count(*) from public.pnl', 0);
select test.assert_count('WALL: TRN-A sees NO comms messages',      'select count(*) from public.comms_messages', 0);
select test.assert_count('WALL: TRN-A sees NO artifact versions',   'select count(*) from public.artifact_versions', 0);
select test.assert_count('WALL: TRN-A sees NO audit events',        'select count(*) from public.audit_events', 0);
select test.assert_count('WALL: TRN-A sees NO storage objects',
  'select count(*) from storage.objects where bucket_id = ''documents''', 0);

-- The college-facing views were never for trainers; still zero.
select test.assert_count('TRN-A sees no progress view',   'select count(*) from public.college_program_progress', 0);
select test.assert_count('TRN-A sees no attendance view', 'select count(*) from public.college_attendance_summary', 0);
select test.assert_count('TRN-A sees no governance view', 'select count(*) from public.college_governance_reports', 0);

-- --- WRITES: the four policies 1800 dropped ---------------------------------
-- Two of these mattered most. The payee could mark the days that decided their
-- own pay (trainer_attendance), and could insert student session attendance.
-- Both are now refused outright rather than merely scoped.
select test.assert_no_write('TRN-A cannot mark own attendance',
  'insert into public.trainer_attendance (deployment_id, mark_date, mark)
     values (''e0000000-0000-0000-0000-00000000000a'', ''2026-07-09'', ''P'')');
select test.assert_no_write('TRN-A cannot insert student attendance',
  'insert into public.attendance_records (deployment_id, student_id, session_date, status)
     values (''e0000000-0000-0000-0000-00000000000a'',
             ''c1000000-0000-0000-0000-0000000000a1'', ''2026-07-09'', ''present'')');
select test.assert_no_write('TRN-A cannot update a deployment',
  'update public.deployments set travel_notes = ''x''');
select test.assert_no_write('TRN-A cannot update a task',
  'update public.tasks set status = ''done''');
reset role;


-- =============================================================================
select test.section('=== TRAINER B — cross-trainer isolation (R5, second clause) ===');
-- =============================================================================
-- R5: "A trainer must never see another trainer's payout." Every assertion here
-- names trainer A's fixture ids explicitly, so a leak is a non-zero count rather
-- than an ambiguity.
select test.login('f0000000-0000-0000-0000-00000000000b');
set role authenticated;

-- Since 1800, trainer B is denied for the same reason trainer A is: there is no
-- trainer policy left to leak through. The cross-trainer clause of R5 is now
-- proved by TOTAL denial rather than by narrow scoping, which is strictly
-- stronger - there is no scope left to get wrong.
select test.assert_count('TRN-B sees NO deployments',      'select count(*) from public.deployments', 0);
select test.assert_count('TRN-B sees NO colleges',         'select count(*) from public.colleges', 0);
select test.assert_count('TRN-B sees NO trainers',         'select count(*) from public.trainers', 0);
select test.assert_count('TRN-B sees NO bank rails',       'select count(*) from public.trainer_bank_accounts', 0);
select test.assert_count('TRN-B sees NO remuneration',     'select count(*) from public.remuneration_sheets', 0);
select test.assert_count('TRN-B sees NO trainer A storage objects',
  'select count(*) from storage.objects where name like ''trainers/d0000000-0000-0000-0000-00000000000a/%''', 0);
select test.assert_count('TRN-B sees NO pnl',              'select count(*) from public.pnl', 0);
select test.assert_count('TRN-B sees NO tasks',            'select count(*) from public.tasks', 0);
select test.assert_count('TRN-B sees NO profiles but their own',
  'select count(*) from public.profiles', 1);

reset role;


-- =============================================================================
select test.section('=== COLLEGE A — published artifacts only, read-only (§4) ===');
-- =============================================================================
select test.login('f0000000-0000-0000-0000-00000000000c');
set role authenticated;

select test.assert_value('persona is college',           'select public.app_role()::text', 'college');
select test.assert_value('  ...is NOT internal',         'select public.is_internal()::text', 'false');
select test.assert_count('COL-A sees own college row',   'select count(*) from public.colleges', 1);
-- 1700 has NO college policy either. §4 grants a college "published artifacts
-- only", and nothing in the outbound queue is published until it is released and
-- transmitted — R4 governs everything before that, and none of it is theirs.
select test.assert_count('COL-A sees NO comms messages',
  'select count(*) from public.comms_messages', 0);
select test.assert_count('COL-A sees own program',       'select count(*) from public.programs', 1);
select test.assert_count('COL-A sees own batches',       'select count(*) from public.batches', 2);

-- ---- Everything §4 denies the college --------------------------------------
select test.assert_count('COL-A sees NO tasks',          'select count(*) from public.tasks', 0);
select test.assert_count('COL-A sees NO task urgency',   'select count(*) from public.task_urgency', 0);
select test.assert_count('COL-A sees NO trainers',       'select count(*) from public.trainers', 0);
select test.assert_count('COL-A sees NO deployments',    'select count(*) from public.deployments', 0);
select test.assert_count('COL-A sees NO students',       'select count(*) from public.students', 0);
select test.assert_count('COL-A sees NO observations',   'select count(*) from public.observations', 0);
select test.assert_count('COL-A sees NO feedback',       'select count(*) from public.feedback', 0);
select test.assert_count('COL-A sees NO assessments',    'select count(*) from public.assessments', 0);
select test.assert_count('COL-A sees NO raw student attendance',
  'select count(*) from public.attendance_records', 0);
select test.assert_count('COL-A sees NO trainer attendance',
  'select count(*) from public.trainer_attendance', 0);
select test.assert_count('COL-A sees NO bank rails',
  'select count(*) from public.trainer_bank_accounts', 0);
select test.assert_count('COL-A sees NO artifact versions',
  'select count(*) from public.artifact_versions', 0);
select test.assert_count('COL-A sees NO audit events',   'select count(*) from public.audit_events', 0);
select test.assert_count('COL-A sees NO task templates', 'select count(*) from public.task_templates', 0);
select test.assert_count('COL-A sees NO document templates',
  'select count(*) from public.document_templates', 0);
select test.assert_count('COL-A sees NO program documents',
  'select count(*) from public.program_documents', 0);
select test.assert_count('COL-A sees NO clusters',       'select count(*) from public.clusters', 0);
select test.assert_count('COL-A sees NO assignments',
  'select count(*) from public.user_college_assignments', 0);
select test.assert_count('COL-A sees NO other profiles', 'select count(*) from public.profiles', 1);
select test.assert_count('COL-A sees NO storage documents',
  'select count(*) from storage.objects where bucket_id = ''documents''', 0);

-- ---- The financial wall ------------------------------------------------------
select test.assert_count('COL-A sees NO pnl',            'select count(*) from public.pnl', 0);
select test.assert_count('COL-A sees NO remuneration',   'select count(*) from public.remuneration_sheets', 0);
select test.assert_count('COL-A sees NO work orders',    'select count(*) from public.work_orders', 0);

-- ---- The publish gate (R4: nothing leaves the system unapproved) -------------
select test.assert_count('COL-A sees only SHARED governance',
  'select count(*) from public.governance_reports', 1);
select test.assert_value('  ...and it is the September report',
  'select title from public.governance_reports', 'Alpha governance — September');
select test.assert_count('COL-A cannot see the October draft',
  'select count(*) from public.governance_reports where shared_with_college_at is null', 0);

-- ---- The curated views -------------------------------------------------------
select test.assert_count('VIEW progress: 1 own program',
  'select count(*) from public.college_program_progress', 1);
select test.assert_value('VIEW progress: batch_count',
  'select batch_count::text from public.college_program_progress', '2');
select test.assert_value('VIEW progress: student_count',
  'select student_count::text from public.college_program_progress', '5');
select test.assert_value('VIEW progress: tasks_total',
  'select tasks_total::text from public.college_program_progress', '4');
select test.assert_value('VIEW progress: tasks_done',
  'select tasks_done::text from public.college_program_progress', '2');
select test.assert_value('VIEW progress: completion pct',
  'select checklist_complete_pct::text from public.college_program_progress', '50.0');

select test.assert_count('VIEW attendance: 2 own batches',
  'select count(*) from public.college_attendance_summary', 2);
select test.assert_value('VIEW attendance: batch A1 pct (5 of 6)',
  'select attendance_pct::text from public.college_attendance_summary
     where batch_name = ''Alpha CSE-A''', '83.3');
select test.assert_value('VIEW attendance: batch A2 has no marks',
  'select coalesce(attendance_pct::text, ''<null>'') from public.college_attendance_summary
     where batch_name = ''Alpha ECE-A''', '<null>');

select test.assert_count('VIEW governance: only the shared report',
  'select count(*) from public.college_governance_reports', 1);

reset role;


-- =============================================================================
select test.section('=== COLLEGE B — cross-college isolation THROUGH THE VIEWS ===');
-- =============================================================================
-- The most valuable section in the file. The three views in 0800 are declared
-- `security_invoker = false`, so they execute as the view OWNER and BYPASS RLS on
-- tasks, attendance_records and students. Their internal WHERE clause is the
-- ENTIRE security boundary — there is no second line of defence behind it.
--
-- A bug in that filter leaks every college's progress and attendance to every
-- other college, and nothing else in the schema would notice. These assertions
-- test the filter directly, from a second college login, against a database
-- where college A's rows definitely exist.
select test.login('f0000000-0000-0000-0000-00000000000d');
set role authenticated;

select test.assert_count('COL-B sees own college only',  'select count(*) from public.colleges', 1);
select test.assert_value('  ...and it is Beta',
  'select name from public.colleges', 'Beta College of Engineering');

-- Base tables first, so a view failure can be told apart from a table failure.
select test.assert_count('COL-B sees NO Alpha programs',
  'select count(*) from public.programs where college_id = ''c0000000-0000-0000-0000-00000000000a''', 0);
select test.assert_count('COL-B sees NO Alpha batches',
  'select count(*) from public.batches where program_id = ''a1000000-0000-0000-0000-00000000000a''', 0);
select test.assert_count('COL-B sees NO Gamma programs',
  'select count(*) from public.programs where college_id = ''c0000000-0000-0000-0000-00000000000c''', 0);

-- --- THROUGH THE RLS-BYPASSING VIEWS -----------------------------------------
select test.assert_count('VIEW progress: NO Alpha rows',
  'select count(*) from public.college_program_progress
     where college_id = ''c0000000-0000-0000-0000-00000000000a''', 0);
select test.assert_count('VIEW progress: NO Gamma rows',
  'select count(*) from public.college_program_progress
     where college_id = ''c0000000-0000-0000-0000-00000000000c''', 0);
select test.assert_count('VIEW progress: own program only',
  'select count(*) from public.college_program_progress', 1);
select test.assert_value('VIEW progress: own tasks_total',
  'select tasks_total::text from public.college_program_progress', '2');
select test.assert_value('VIEW progress: own completion pct',
  'select checklist_complete_pct::text from public.college_program_progress', '0.0');

select test.assert_count('VIEW attendance: own batch only',
  'select count(*) from public.college_attendance_summary', 1);
select test.assert_value('VIEW attendance: own batch pct',
  'select attendance_pct::text from public.college_attendance_summary', '100.0');
select test.assert_count('VIEW attendance: NO Alpha batches',
  'select count(*) from public.college_attendance_summary
     where program_id = ''a1000000-0000-0000-0000-00000000000a''', 0);

-- Beta's only governance report is an unshared draft, so the honest answer is
-- zero — both from the base table and through the view.
select test.assert_count('COL-B sees NO governance (draft only)',
  'select count(*) from public.governance_reports', 0);
select test.assert_count('VIEW governance: nothing shared with Beta',
  'select count(*) from public.college_governance_reports', 0);
select test.assert_count('VIEW governance: NOT Alpha''s shared report',
  'select count(*) from public.college_governance_reports
     where program_id = ''a1000000-0000-0000-0000-00000000000a''', 0);

select test.assert_count('COL-B sees NO pnl',            'select count(*) from public.pnl', 0);
select test.assert_count('COL-B sees NO remuneration',   'select count(*) from public.remuneration_sheets', 0);
select test.assert_count('COL-B sees NO storage documents',
  'select count(*) from storage.objects where bucket_id = ''documents''', 0);

reset role;


-- =============================================================================
select test.section('=== UNASSIGNED SIGNUP — least privilege by default ===');
-- =============================================================================
-- handle_new_user() defaults an unrecognised signup to 'trainer' with a NULL
-- trainer_id, so every predicate in the schema returns false or NULL. A new user
-- must see nothing, anywhere, until an admin assigns them.
--
-- Since 1800 this is a STRONGER guarantee than it was. `trainer` used to carry
-- real (if narrow) policies, so a forged or malformed signup landed on a persona
-- with some access. It now carries none at all, which is exactly why the label
-- is kept as the fallback rather than removed.
select test.login('f0000000-0000-0000-0000-00000000000e');
set role authenticated;

select test.assert_value('defaults to the trainer persona',
  'select public.app_role()::text', 'trainer');
-- Read from `profiles` rather than through `my_trainer_id()`: 1800 revoked
-- EXECUTE on that helper from `authenticated`, so calling it here would assert
-- the wrong thing (a permission error, not a NULL identity). The column is the
-- fact; the helper was only ever a convenience over it.
select test.assert_value('  ...with no trainer identity',
  'select coalesce(trainer_id::text, ''<null>'') from public.profiles
     where id = auth.uid()', '<null>');
select test.assert_value('  ...and no college identity',
  'select coalesce(public.my_college_id()::text, ''<null>'')', '<null>');
select test.assert_value('  ...not internal',            'select public.is_internal()::text', 'false');
select test.assert_value('  ...no commercials',          'select public.can_see_commercials()::text', 'false');
select test.assert_count('  ...and reaches no college',
  'select count(*) from public.my_college_ids()', 0);

select test.assert_count('NEW sees own profile',         'select count(*) from public.profiles', 1);
select test.assert_count('NEW sees no clusters',         'select count(*) from public.clusters', 0);
select test.assert_count('NEW sees no colleges',         'select count(*) from public.colleges', 0);
select test.assert_count('NEW sees no programs',         'select count(*) from public.programs', 0);
select test.assert_count('NEW sees no batches',          'select count(*) from public.batches', 0);
select test.assert_count('NEW sees no students',         'select count(*) from public.students', 0);
select test.assert_count('NEW sees no trainers',         'select count(*) from public.trainers', 0);
select test.assert_count('NEW sees no deployments',      'select count(*) from public.deployments', 0);
select test.assert_count('NEW sees no tasks',            'select count(*) from public.tasks', 0);
select test.assert_count('NEW sees no task urgency',     'select count(*) from public.task_urgency', 0);
select test.assert_count('NEW sees no task templates',   'select count(*) from public.task_templates', 0);
select test.assert_count('NEW sees no document templates',
  'select count(*) from public.document_templates', 0);
select test.assert_count('NEW sees no program documents',
  'select count(*) from public.program_documents', 0);
select test.assert_count('NEW sees no observations',     'select count(*) from public.observations', 0);
select test.assert_count('NEW sees no feedback',         'select count(*) from public.feedback', 0);
select test.assert_count('NEW sees no assessments',      'select count(*) from public.assessments', 0);
select test.assert_count('NEW sees no student attendance',
  'select count(*) from public.attendance_records', 0);
select test.assert_count('NEW sees no trainer attendance',
  'select count(*) from public.trainer_attendance', 0);
select test.assert_count('NEW sees no work orders',      'select count(*) from public.work_orders', 0);
select test.assert_count('NEW sees no remuneration',     'select count(*) from public.remuneration_sheets', 0);
select test.assert_count('NEW sees no pnl',              'select count(*) from public.pnl', 0);
select test.assert_count('NEW sees no governance',       'select count(*) from public.governance_reports', 0);
select test.assert_count('NEW sees no progress view',
  'select count(*) from public.college_program_progress', 0);
select test.assert_count('NEW sees no attendance view',
  'select count(*) from public.college_attendance_summary', 0);
select test.assert_count('NEW sees no governance view',
  'select count(*) from public.college_governance_reports', 0);
select test.assert_count('NEW sees no storage documents',
  'select count(*) from storage.objects where bucket_id = ''documents''', 0);

reset role;


-- =============================================================================
select test.section('=== ADMIN — the right to GRANT reach is not reach ===');
-- =============================================================================
-- 0700, verbatim: "There is no admin override on any policy in this file.
-- is_admin() grants the right to hand out reach; it is not itself reach. A
-- Senior Manager with no cluster assignment sees no P&L, and that is correct."
--
-- This fixture user is an admin with ZERO assignments, which is the only way to
-- prove that sentence rather than assume it.
select test.login('f0000000-0000-0000-0000-000000000006');
set role authenticated;

select test.assert_value('is an admin',                  'select public.is_admin()::text', 'true');
select test.assert_count('ADM reaches no college',
  'select count(*) from public.my_college_ids()', 0);

-- Admin policies exist on identity, the org chart, the template libraries and
-- storage — so these are NOT zero.
select test.assert_count('ADM manages every profile',    'select count(*) from public.profiles', 11);
select test.assert_count('ADM manages every college',    'select count(*) from public.colleges', 3);
select test.assert_count('ADM manages every cluster',    'select count(*) from public.clusters', 2);
select test.assert_count('ADM reads the task library',   'select count(*) from public.task_templates', 37);
select test.assert_count('ADM reads the document library',
  'select count(*) from public.document_templates', 37);
select test.assert_count('ADM can fix any misfiled document',
  'select count(*) from storage.objects where bucket_id = ''documents''
     and (name like ''colleges/c0000000-%'' or name like ''trainers/d0000000-%''
          or name like ''programs/a1000000-%'')', 6);

-- ...and delivery / money data is NOT among them, because reach is empty.
select test.assert_count('ADM sees NO programs',         'select count(*) from public.programs', 0);
select test.assert_count('ADM sees NO batches',          'select count(*) from public.batches', 0);
select test.assert_count('ADM sees NO students',         'select count(*) from public.students', 0);
select test.assert_count('ADM sees NO deployments',      'select count(*) from public.deployments', 0);
select test.assert_count('ADM sees NO tasks',            'select count(*) from public.tasks', 0);
select test.assert_count('ADM sees NO pnl',              'select count(*) from public.pnl', 0);
select test.assert_count('ADM sees NO remuneration',     'select count(*) from public.remuneration_sheets', 0);
select test.assert_count('ADM sees NO work orders',      'select count(*) from public.work_orders', 0);
select test.assert_count('ADM sees NO governance',       'select count(*) from public.governance_reports', 0);
select test.assert_count('ADM sees NO program documents',
  'select count(*) from public.program_documents', 0);
-- 1300 — the audit trail IS an admin surface. §4 puts escalations and payout
-- approval in this column, and 1300's policy note is explicit that anything
-- richer than "admin, or your own rows" belongs in a curated view. Artifact
-- versions are NOT: artifact reach is reach, and this admin has none.
select test.assert_count('ADM reads the whole audit trail',
  'select count(*) from public.audit_events', 2);
select test.assert_count('ADM reaches NO artifact versions',
  'select count(*) from public.artifact_versions', 0);
-- 1400 — and this one is NOT the admin flag: this fixture user's PERSONA is
-- senior_manager, so can_see_commercials() is true and the rails policy carries
-- no reach conjunct. Recorded as 2 rather than hidden, because the interesting
-- fact is that reach is not what returns them.
select test.assert_count('ADM sees bank rails (as a senior_manager, not as an admin)',
  'select count(*) from public.trainer_bank_accounts', 2);

reset role;


-- =============================================================================
select test.section('=== WRITE PATHS — what each persona may and may not change ===');
-- =============================================================================
-- Ordered after every read assertion, because the ALLOWED writes below change
-- the counts those assertions depend on. Within this section, writes that widen
-- someone's reach are pushed to the very end for the same reason.


-- -----------------------------------------------------------------------------
select test.section('--- privilege escalation: the profiles guard (0200) ---');
-- -----------------------------------------------------------------------------
-- RLS WITH CHECK cannot see the OLD row, so "edit your own profile but not your
-- own role" is a BEFORE UPDATE trigger, not a policy. The guard is STRICTER than
-- the three-persona schema it replaces: only an admin may change role, is_admin,
-- trainer_id or college_id — because with reach derived from assignments, the
-- ability to change role IS the ability to change scope.

select test.login('f0000000-0000-0000-0000-000000000004');  -- lde_a1
set role authenticated;

select test.assert_no_write('LDE cannot promote self to manager',
  'update public.profiles set role = ''manager''
     where id = ''f0000000-0000-0000-0000-000000000004''');
select test.assert_no_write('LDE cannot promote self to senior_manager',
  'update public.profiles set role = ''senior_manager''
     where id = ''f0000000-0000-0000-0000-000000000004''');
select test.assert_no_write('LDE cannot self-grant is_admin',
  'update public.profiles set is_admin = true
     where id = ''f0000000-0000-0000-0000-000000000004''');
select test.assert_no_write('LDE cannot pin self to a college via profiles',
  'update public.profiles set college_id = ''c0000000-0000-0000-0000-00000000000c''
     where id = ''f0000000-0000-0000-0000-000000000004''');
select test.assert_no_write('LDE cannot edit another profile',
  'update public.profiles set full_name = ''hacked''
     where id = ''f0000000-0000-0000-0000-000000000002''');
select test.assert_writes('LDE CAN edit own display name',
  'update public.profiles set full_name = ''LDE Alpha (edited)''
     where id = ''f0000000-0000-0000-0000-000000000004''', 1);

-- Granting reach is an admin act, not a manager act.
select test.assert_no_write('LDE cannot grant themselves another college',
  'insert into public.user_college_assignments (user_id, college_id)
     values (''f0000000-0000-0000-0000-000000000004'', ''c0000000-0000-0000-0000-00000000000b'')');
select test.assert_no_write('LDE cannot grant themselves a cluster',
  'insert into public.user_cluster_assignments (user_id, cluster_id)
     values (''f0000000-0000-0000-0000-000000000004'', ''c1000000-0000-0000-0000-000000000001'')');
select test.assert_no_write('LDE cannot curate the task template library',
  'insert into public.task_templates (stage, title, order_index)
     values (''deployment'', ''lde injected'', 999)');

reset role;

-- A NON-ADMIN senior manager is refused exactly like everyone else. This is the
-- assertion that pins the "stricter than the old schema" behaviour.
select test.login('f0000000-0000-0000-0000-000000000001');  -- senior_manager, not admin
set role authenticated;

select test.assert_no_write('non-admin SM cannot self-grant is_admin',
  'update public.profiles set is_admin = true
     where id = ''f0000000-0000-0000-0000-000000000001''');
select test.assert_no_write('non-admin SM cannot change another persona''s role',
  'update public.profiles set role = ''senior_manager''
     where id = ''f0000000-0000-0000-0000-000000000004''');
select test.assert_no_write('non-admin SM cannot grant reach',
  'insert into public.user_cluster_assignments (user_id, cluster_id)
     values (''f0000000-0000-0000-0000-000000000001'', ''c1000000-0000-0000-0000-000000000002'')');
select test.assert_no_write('non-admin SM cannot curate templates',
  'insert into public.task_templates (stage, title, order_index)
     values (''deployment'', ''sm injected'', 998)');

reset role;

select test.login('f0000000-0000-0000-0000-00000000000a');  -- trainer_a
set role authenticated;
select test.assert_no_write('trainer cannot reassign own trainer_id',
  'update public.profiles set trainer_id = ''d0000000-0000-0000-0000-00000000000b''
     where id = ''f0000000-0000-0000-0000-00000000000a''');
select test.assert_no_write('trainer cannot self-grant is_admin',
  'update public.profiles set is_admin = true
     where id = ''f0000000-0000-0000-0000-00000000000a''');
reset role;


-- -----------------------------------------------------------------------------
select test.section('--- the commercials wall is a WRITE wall too ---');
-- -----------------------------------------------------------------------------
select test.login('f0000000-0000-0000-0000-000000000004');  -- lde_a1
set role authenticated;

select test.assert_no_write('LDE cannot insert pnl',
  'insert into public.pnl (program_id, revenue)
     values (''a1000000-0000-0000-0000-00000000000a'', 1)');
select test.assert_no_write('LDE cannot update pnl',
  'update public.pnl set revenue = 1
     where program_id = ''a1000000-0000-0000-0000-00000000000a''');
select test.assert_no_write('LDE cannot insert a work order',
  'insert into public.work_orders (trainer_id, program_id, rate, rate_basis, valid_from, valid_to)
     values (''d0000000-0000-0000-0000-00000000000a'', ''a1000000-0000-0000-0000-00000000000a'',
             1, ''per_day'', ''2026-07-01'', ''2026-07-31'')');
select test.assert_no_write('LDE cannot insert a remuneration sheet',
  'insert into public.remuneration_sheets (trainer_id, program_id, period_start, period_end, net_amount)
     values (''d0000000-0000-0000-0000-00000000000a'', ''a1000000-0000-0000-0000-00000000000a'',
             ''2026-10-01'', ''2026-10-31'', 1)');
-- 1400 — the rails are a write wall as well as a read wall. An LDE Executive
-- who could INSERT a payment rail could point a payout at any account they like
-- without ever reading one.
select test.assert_no_write('LDE cannot insert bank rails',
  'insert into public.trainer_bank_accounts (trainer_id, bank_account_number, ifsc)
     values (''d0000000-0000-0000-0000-00000000000a'', ''99999999999'', ''HDFC0009999'')');
select test.assert_no_write('LDE cannot repoint an existing bank rail',
  'update public.trainer_bank_accounts set bank_account_number = ''99999999999''
     where trainer_id = ''d0000000-0000-0000-0000-00000000000a''');
select test.assert_no_write('LDE cannot file a remuneration document',
  'insert into public.program_documents (program_id, category, name)
     values (''a1000000-0000-0000-0000-00000000000a'', ''remuneration'', ''injected'')');
select test.assert_no_write('LDE cannot file an invoice document',
  'insert into public.program_documents (program_id, category, name)
     values (''a1000000-0000-0000-0000-00000000000a'', ''invoice_generation'', ''injected'')');
-- ...but the operational half of the register is theirs.
select test.assert_writes('LDE CAN file an operational document',
  'insert into public.program_documents (program_id, category, name, status)
     values (''a1000000-0000-0000-0000-00000000000a'', ''monitoring_and_quality'',
             ''Alpha observation pack'', ''filed'')', 1);
select test.assert_value('  ...and filed_at was stamped',
  'select (filed_at is not null)::text from public.program_documents
     where name = ''Alpha observation pack''', 'true');

-- Cross-campus writes are refused on the same predicate as cross-campus reads.
select test.assert_no_write('LDE cannot create a batch on another campus',
  'insert into public.batches (program_id, name)
     values (''a1000000-0000-0000-0000-00000000000b'', ''injected'')');
select test.assert_no_write('LDE cannot mark trainer attendance on another deployment',
  'insert into public.trainer_attendance (deployment_id, mark_date, mark)
     values (''e0000000-0000-0000-0000-00000000000b'', ''2026-08-06'', ''P'')');
-- Corrections to trainer attendance ARE the LDE's job (0600: the payee must not
-- be able to rewrite their own days, so the campus holder does it).
select test.assert_writes('LDE CAN correct trainer attendance on their campus',
  'update public.trainer_attendance set mark = ''P'', note = ''Corrected after tracksheet review''
     where deployment_id = ''e0000000-0000-0000-0000-00000000000a''
       and mark_date = ''2026-07-28''', 1);

reset role;


-- -----------------------------------------------------------------------------
select test.section('--- cross-college writes between Managers ---');
-- -----------------------------------------------------------------------------
select test.login('f0000000-0000-0000-0000-000000000002');  -- manager_a
set role authenticated;

select test.assert_no_write('MGR-A cannot create a program under college C',
  'insert into public.programs (college_id, type, name)
     values (''c0000000-0000-0000-0000-00000000000c'', ''bCAP'', ''injected'')');
select test.assert_no_write('MGR-A cannot edit college C',
  'update public.colleges set mou_status = ''expired''
     where id = ''c0000000-0000-0000-0000-00000000000c''');
select test.assert_no_write('MGR-A cannot move a program into college C',
  'update public.programs set college_id = ''c0000000-0000-0000-0000-00000000000c''
     where id = ''a1000000-0000-0000-0000-00000000000a''');
select test.assert_no_write('MGR-A cannot touch college C''s P&L',
  'update public.pnl set revenue = 1
     where program_id = ''a1000000-0000-0000-0000-00000000000c''');
select test.assert_no_write('MGR-A cannot grant reach to anyone',
  'insert into public.user_college_assignments (user_id, college_id)
     values (''f0000000-0000-0000-0000-000000000004'', ''c0000000-0000-0000-0000-00000000000b'')');
select test.assert_writes('MGR-A CAN update a college they cover',
  'update public.colleges set contact_phone = ''+91 90000 00001''
     where id = ''c0000000-0000-0000-0000-00000000000a''', 1);

-- 1400 — maintaining payment rails is the positive control for the wall: some
-- persona has to be able to type an account number in or Phase 2 never starts.
-- Roster-wide, matching trainers_sourcing_all: MGR-A does not cover trainer B's
-- college and still maintains their rails, which the migration argues for
-- explicitly (rails are collected before any deployment exists).
select test.assert_writes('MGR-A CAN correct a bank rail',
  'update public.trainer_bank_accounts set bank_name = ''HDFC Bank Ltd''
     where trainer_id = ''d0000000-0000-0000-0000-00000000000a''', 1);
select test.assert_writes('MGR-A CAN maintain rails roster-wide (no reach conjunct)',
  'update public.trainer_bank_accounts set branch = ''Vijayawada Main''
     where trainer_id = ''d0000000-0000-0000-0000-00000000000b''', 1);
-- The CHECK constraints are the last line of §7 defence at write time: a
-- 10-character IFSC and a non-numeric account are rejected by the database
-- rather than surfacing at the bank after release.
select test.assert_no_write('a short IFSC is rejected by the database',
  'update public.trainer_bank_accounts set ifsc = ''HDFC000123''
     where trainer_id = ''d0000000-0000-0000-0000-00000000000a''');
select test.assert_no_write('a lowercase IFSC is rejected by the database',
  'update public.trainer_bank_accounts set ifsc = ''hdfc0001234''
     where trainer_id = ''d0000000-0000-0000-0000-00000000000a''');
select test.assert_no_write('a non-numeric account number is rejected',
  'update public.trainer_bank_accounts set bank_account_number = ''50100-1234''
     where trainer_id = ''d0000000-0000-0000-0000-00000000000a''');
select test.assert_no_write('rails cannot be DELETED, only corrected',
  'delete from public.trainer_bank_accounts
     where trainer_id = ''d0000000-0000-0000-0000-00000000000a''');

reset role;


-- -----------------------------------------------------------------------------
select test.section('--- tasks: the owner guard (0500) ---');
-- -----------------------------------------------------------------------------
-- The guard only narrows NON-internal callers, so it must be exercised through
-- the trainer who owns a task. Internal staff legitimately reschedule and
-- reassign work on programs they reach.
select test.login('f0000000-0000-0000-0000-00000000000a');  -- trainer_a
set role authenticated;

select test.assert_no_write('trainer cannot touch a task they do not own',
  'update public.tasks set status = ''done''
     where id = ''7a000000-0000-0000-0000-0000000000a4''');
select test.assert_no_write('trainer cannot move own task due date',
  'update public.tasks set due_date = ''2027-01-01''
     where id = ''7a000000-0000-0000-0000-0000000000a3''');
select test.assert_no_write('trainer cannot reassign own task',
  'update public.tasks set owner_id = ''f0000000-0000-0000-0000-000000000004''
     where id = ''7a000000-0000-0000-0000-0000000000a3''');
-- Cadence is program design, not task progress: relabelling a daily job as
-- one-time would remove it from the daily filter without touching its status.
select test.assert_no_write('trainer cannot change own task cadence',
  'update public.tasks set cadence = ''one_time''
     where id = ''7a000000-0000-0000-0000-0000000000a3''');
select test.assert_no_write('trainer cannot move own task to another program',
  'update public.tasks set program_id = ''a1000000-0000-0000-0000-00000000000b''
     where id = ''7a000000-0000-0000-0000-0000000000a3''');
select test.assert_no_write('trainer cannot create tasks',
  'insert into public.tasks (program_id, stage, title)
     values (''a1000000-0000-0000-0000-00000000000a'', ''deployment'', ''injected'')');
-- Blockers ARE progress information — that is what turns `blocked` into an
-- escalation list rather than a dead end.
select test.assert_no_write('trainer CANNOT flag own task blocked with a reason (1800: policy dropped)',
  'update public.tasks set status = ''blocked'', blocked_reason = ''Awaiting ERM push'',
                           waiting_on = ''L&S''
     where id = ''7a000000-0000-0000-0000-0000000000a3''');
select test.assert_no_write('trainer CANNOT mark own task done (1800: policy dropped)',
  'update public.tasks set status = ''done''
     where id = ''7a000000-0000-0000-0000-0000000000a3''');


-- -----------------------------------------------------------------------------
select test.section('--- deployments: the trainer column guard (0400) ---');
-- -----------------------------------------------------------------------------
-- Dates are an input to payable days, so extending a deployment is a raise.
select test.assert_no_write('trainer cannot reassign own deployment to another batch',
  'update public.deployments set batch_id = ''b1000000-0000-0000-0000-0000000000a2''
     where id = ''e0000000-0000-0000-0000-00000000000a''');
select test.assert_no_write('trainer cannot extend own deployment dates',
  'update public.deployments set end_date = ''2027-06-30''
     where id = ''e0000000-0000-0000-0000-00000000000a''');
select test.assert_no_write('trainer cannot hand their deployment to someone else',
  'update public.deployments set trainer_id = ''d0000000-0000-0000-0000-00000000000b''
     where id = ''e0000000-0000-0000-0000-00000000000a''');
select test.assert_no_write('trainer CANNOT submit travel on own deployment (1800: policy dropped)',
  'update public.deployments set travel_notes = ''Train 12723, arriving 06:40'',
                                 travel_submitted_at = now()
     where id = ''e0000000-0000-0000-0000-00000000000a''');
select test.assert_no_write('trainer CANNOT update own tracksheet link (1800: policy dropped)',
  'update public.deployments set tracksheet_url = ''https://tracksheets.test/a1-v2''
     where id = ''e0000000-0000-0000-0000-00000000000a''');


-- -----------------------------------------------------------------------------
select test.section('--- money and attendance are read-only for the payee ---');
-- -----------------------------------------------------------------------------
select test.assert_no_write('trainer cannot alter own net pay',
  'update public.remuneration_sheets set net_amount = 999999
     where trainer_id = ''d0000000-0000-0000-0000-00000000000a''');
select test.assert_no_write('trainer cannot alter own work order rate',
  'update public.work_orders set rate = 999999
     where trainer_id = ''d0000000-0000-0000-0000-00000000000a''');
select test.assert_no_write('trainer cannot insert a remuneration sheet',
  'insert into public.remuneration_sheets (trainer_id, program_id, period_start, period_end, net_amount)
     values (''d0000000-0000-0000-0000-00000000000a'', ''a1000000-0000-0000-0000-00000000000a'',
             ''2026-10-01'', ''2026-10-31'', 1)');
select test.assert_no_write('trainer cannot insert pnl',
  'insert into public.pnl (program_id, revenue)
     values (''a1000000-0000-0000-0000-00000000000a'', 1)');

-- 1400 — the payee READS their own rails (asserted in the TRAINER A section) and
-- may not WRITE them. R4: a payee who can silently repoint their own account
-- between approval and release makes the approved artifact name an account that
-- no longer exists on the row. Changing a rail is an internal act, on record.
select test.assert_no_write('trainer cannot repoint their OWN bank account',
  'update public.trainer_bank_accounts set bank_account_number = ''99999999999''
     where trainer_id = ''d0000000-0000-0000-0000-00000000000a''');
select test.assert_no_write('trainer cannot change their OWN IFSC',
  'update public.trainer_bank_accounts set ifsc = ''HDFC0009999''
     where trainer_id = ''d0000000-0000-0000-0000-00000000000a''');
select test.assert_no_write('trainer cannot file rails for another trainer',
  'insert into public.trainer_bank_accounts (trainer_id, bank_account_number, ifsc)
     values (''d0000000-0000-0000-0000-00000000000b'', ''12345678901'', ''HDFC0009999'')');
select test.assert_no_write('trainer cannot DELETE their own rails',
  'delete from public.trainer_bank_accounts
     where trainer_id = ''d0000000-0000-0000-0000-00000000000a''');

-- trainer_attendance: SELECT and INSERT only, deliberately. If the payee could
-- rewrite a day after the fact, the audit trail that resolves a payout dispute
-- would be exactly as trustworthy as the person disputing it (0600).
select test.assert_no_write('trainer CANNOT mark a day on own deployment (1800: policy dropped)',
  'insert into public.trainer_attendance (deployment_id, mark_date, mark)
     values (''e0000000-0000-0000-0000-00000000000a'', ''2026-07-29'', ''P'')');
select test.assert_no_write('trainer cannot mark a day on another deployment',
  'insert into public.trainer_attendance (deployment_id, mark_date, mark)
     values (''e0000000-0000-0000-0000-00000000000b'', ''2026-08-06'', ''P'')');
select test.assert_no_write('trainer cannot REWRITE their own attendance mark',
  'update public.trainer_attendance set mark = ''P''
     where deployment_id = ''e0000000-0000-0000-0000-00000000000a''
       and mark_date = ''2026-07-29''');
select test.assert_no_write('trainer cannot DELETE their own attendance mark',
  'delete from public.trainer_attendance
     where deployment_id = ''e0000000-0000-0000-0000-00000000000a''
       and mark_date = ''2026-07-29''');
-- Student attendance is a different table with different rules: the trainer
-- records it as part of delivery.
select test.assert_no_write('trainer CANNOT record student attendance on own deployment (1800: policy dropped)',
  'insert into public.attendance_records (deployment_id, student_id, session_date, status)
     values (''e0000000-0000-0000-0000-00000000000a'', ''50000000-0000-0000-0000-0000000000a1'',
             ''2026-09-03'', ''present'')');
select test.assert_no_write('trainer cannot record student attendance elsewhere',
  'insert into public.attendance_records (deployment_id, session_date, status)
     values (''e0000000-0000-0000-0000-00000000000b'', ''2026-09-03'', ''present'')');

reset role;


-- -----------------------------------------------------------------------------
select test.section('--- college is read-only EVERYWHERE (§4) ---');
-- -----------------------------------------------------------------------------
-- There is no INSERT/UPDATE/DELETE policy for the college persona anywhere in
-- this schema. These assertions are what makes that a fact rather than a claim.
select test.login('f0000000-0000-0000-0000-00000000000c');
set role authenticated;

select test.assert_no_write('college cannot advance its own program stage',
  'update public.programs set stage = ''closeout_finance''
     where college_id = ''c0000000-0000-0000-0000-00000000000a''');
select test.assert_no_write('college cannot edit its own college row',
  'update public.colleges set mou_status = ''expired''
     where id = ''c0000000-0000-0000-0000-00000000000a''');
select test.assert_no_write('college cannot publish a governance report',
  'update public.governance_reports set shared_with_college_at = now()
     where id = ''61000000-0000-0000-0000-0000000000a2''');
select test.assert_no_write('college cannot create a batch',
  'insert into public.batches (program_id, name)
     values (''a1000000-0000-0000-0000-00000000000a'', ''injected'')');
select test.assert_no_write('college cannot enrol a student',
  'insert into public.students (batch_id, full_name)
     values (''b1000000-0000-0000-0000-0000000000a1'', ''injected'')');
select test.assert_no_write('college cannot record attendance',
  'insert into public.attendance_records (deployment_id, session_date, status)
     values (''e0000000-0000-0000-0000-00000000000a'', ''2026-09-04'', ''present'')');
select test.assert_no_write('college cannot create a task',
  'insert into public.tasks (program_id, stage, title)
     values (''a1000000-0000-0000-0000-00000000000a'', ''deployment'', ''injected'')');

reset role;


-- -----------------------------------------------------------------------------
select test.section('--- admin: the positive control (run last: it widens reach) ---');
-- -----------------------------------------------------------------------------
-- Everything above asserts that some persona is REFUSED. Without these four
-- lines the guard trigger would pass this suite even if it rejected literally
-- every update, and template curation would look protected while being broken.
select test.login('f0000000-0000-0000-0000-000000000006');
set role authenticated;

select test.assert_writes('ADM CAN curate the task template library',
  'insert into public.task_templates (stage, title, order_index, offset_days)
     values (''deployment'', ''admin added'', 999, 0)', 1);
select test.assert_writes('ADM CAN curate the document template library',
  'insert into public.document_templates (category, stage, name)
     values (''templates_and_masters'', null, ''admin added'')', 1);
select test.assert_writes('ADM CAN change a persona',
  'update public.profiles set role = ''lde_executive''
     where id = ''f0000000-0000-0000-0000-00000000000e''', 1);
select test.assert_writes('ADM CAN grant reach',
  'insert into public.user_college_assignments (user_id, college_id)
     values (''f0000000-0000-0000-0000-00000000000e'', ''c0000000-0000-0000-0000-00000000000c'')', 1);

reset role;

-- The formerly-unassigned signup is now an LDE Executive on college C. Reach
-- follows the assignment immediately — nothing is cached, nothing is stamped on
-- the profile — which is the reason §4 puts reach in a join table at all.
select test.login('f0000000-0000-0000-0000-00000000000e');
set role authenticated;
select test.assert_count('newly-assigned user now reaches college C',
  'select count(*) from public.colleges', 1);
reset role;


select test.logout();
select test.section('=== ALL RLS MATRIX ASSERTIONS PASSED ===');
