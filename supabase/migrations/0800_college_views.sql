-- =============================================================================
-- byteXL Ops Intelligence Platform — 0800 — curated read-only college views
-- =============================================================================
-- CLAUDE.md §4 gives the college "published artifacts only, read-only" — in
-- practice program progress and an attendance summary — while denying it
-- internal task ownership and every cost. Row-level RLS cannot express "you may
-- see counts but not rows", so the college reads these views instead of the
-- underlying tables.
--
-- Internal staff can read them too, scoped by reach. That is not a convenience:
-- before a governance call a Manager needs to see EXACTLY what the college sees,
-- and the only way to guarantee that is for both to read the same view. A second
-- "internal version" of the same aggregate would drift within a quarter.
--
-- ⚠️  SECURITY MODEL — READ BEFORE EDITING ANY VIEW IN THIS FILE
--
-- These views are declared `security_invoker = false`, so they execute as the
-- view OWNER and therefore BYPASS RLS on tasks, attendance_records and students.
-- That is deliberate: no persona but internal staff has a policy on those
-- tables, and the view could not aggregate them otherwise.
--
-- The consequence is that the WHERE clause inside each view is the ENTIRE
-- security boundary. There is no second line of defence behind it. Any new view
-- here, or any new column on an existing one, must keep that filter intact and
-- must not surface:
--   - task owner_id / title / assignment detail
--   - anything from pnl, work_orders or remuneration_sheets
--   - any other college's rows
--
-- The filter has the same two branches in every view:
--
--     where <college_id> = public.my_college_id()                    -- the college itself
--        or (public.is_internal() and public.can_reach_college(...)) -- staff with reach
--
-- A trainer fails both: my_college_id() is NULL for them and is_internal() is
-- false, so they get zero rows — which is correct, they read their own
-- deployment through the base tables. An LDE Executive passes the second branch;
-- there is nothing commercial in any view here, so no third conjunct is needed.
-- If a cost column is ever added to one of these views, that stops being true
-- and can_see_commercials() must go with it — which is the strongest argument
-- for never adding one.
--
-- The RLS test suite asserts cross-college isolation against these views
-- specifically, not just against the base tables.
-- =============================================================================

-- --- college_program_progress ------------------------------------------------
-- Program-level roll-up: where the program is in the pipeline and how much of
-- its checklist is complete, as COUNTS ONLY. No task titles, no owners.

create view public.college_program_progress
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
   or (public.is_internal() and public.can_reach_college(pr.college_id));

comment on view public.college_program_progress is
  'Program roll-up as the college sees it. Counts only — no task titles, owners, or cost. The WHERE clause IS the access control (security_invoker = false).';

-- --- college_attendance_summary ----------------------------------------------
-- Batch-level STUDENT attendance. Deliberately not per-student and not
-- per-trainer: §4 gives the college a summary and nothing about internal
-- assignment.
--
-- Note the source is attendance_records, never trainer_attendance. Trainer
-- attendance is a payroll input about a byteXL contractor and has no business in
-- a college-facing surface at any level of aggregation — an attendance % that
-- moved when a trainer took half a day would leak exactly that.

create view public.college_attendance_summary
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
   or (public.is_internal() and public.can_reach_college(pr.college_id))
group by ba.id, ba.program_id, ba.name, ba.branch;

comment on view public.college_attendance_summary is
  'Student attendance roll-up per batch. No student or trainer identity, and never sourced from trainer_attendance. The WHERE clause IS the access control.';

-- --- college_governance_reports ----------------------------------------------
-- Projection over governance_reports. Unlike the two views above this one has
-- real RLS behind it as well, since the college does hold a policy on the base
-- table — the publish gate is enforced in both places, which is what you want
-- for the one artifact that actually leaves the building.

create view public.college_governance_reports
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
    or (public.is_internal() and public.can_reach_college(pr.college_id))
  );

comment on view public.college_governance_reports is
  'Governance reports actually shared with the owning college. Unshared drafts are invisible here to every persona, by design.';

-- --- Grants ------------------------------------------------------------------
-- Authenticated only. An anonymous caller has no profile, so my_college_id()
-- would be NULL, is_internal() false and every view empty — but revoke
-- explicitly rather than rely on a chain of NULLs behaving.

revoke all on public.college_program_progress   from public, anon;
revoke all on public.college_attendance_summary from public, anon;
revoke all on public.college_governance_reports from public, anon;

grant select on public.college_program_progress   to authenticated;
grant select on public.college_attendance_summary to authenticated;
grant select on public.college_governance_reports to authenticated;
