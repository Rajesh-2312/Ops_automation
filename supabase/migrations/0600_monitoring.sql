-- =============================================================================
-- byteXL Ops Intelligence Platform — 0600 — observations, feedback,
--                                           assessments, attendance
-- =============================================================================
-- The active-monitoring tables. All of them hang off a deployment or a batch, so
-- internal reach reuses can_reach_deployment() / can_reach_batch() from 0400 and
-- 0300, and trainer visibility reuses the trainer_on_*() predicates.
--
-- TWO ATTENDANCE TABLES, ON PURPOSE
-- ---------------------------------
-- `attendance_records` is STUDENT attendance: who turned up to a session. It
-- feeds the college-facing summary view in 0800 and nothing else.
--
-- `trainer_attendance` is TRAINER attendance: one row per trainer-day, and the
-- direct input to payable days and therefore to money (CLAUDE.md §5, §6).
--
-- They are separate tables rather than one table with a nullable column, and the
-- reasons compound:
--
--   GRAIN. One is student-session, the other is trainer-day. A UNIQUE constraint
--   that is correct for one is wrong for the other, and "one row per day" — the
--   §6 rule that makes payout disputes resolvable — cannot be enforced at all on
--   a table that also holds thirty student rows for the same day.
--
--   VOCABULARY. attendance_status is present/absent/late/excused; attendance_mark
--   is P/A/H/HOLIDAY/UNMARKED with asymmetric payable-day meanings between CRT
--   and bCAP. 'late' has no payout meaning; 'H' has no student meaning. Merging
--   them would produce an enum where half the values are invalid for half the
--   rows, enforced by nothing.
--
--   SECURITY CLASS. Student attendance is an aggregate a college may see.
--   Trainer attendance is a payout input: it decides what a person is paid, and
--   the person being paid must not be able to rewrite it after the fact.
--
--   BLAST RADIUS. The college views in 0800 read attendance_records through a
--   view that bypasses RLS. Widening that table's grain to carry trainer-day
--   rows would silently change what those views compute — attendance % would
--   start including trainer marks — with no error anywhere.
--
-- So attendance_records is left exactly as it was, and trainer attendance is new.
-- =============================================================================

-- --- observations ------------------------------------------------------------

create table public.observations (
  id             uuid primary key default gen_random_uuid(),
  deployment_id  uuid not null references public.deployments (id) on delete cascade,
  observer_id    uuid references public.profiles (id) on delete set null,
  observed_on    date not null default current_date,
  notes          text,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now()
);

comment on table public.observations is
  'Classroom observation of a deployment, recorded by internal staff on campus.';

create index observations_deployment_id_idx on public.observations (deployment_id);
create index observations_observed_on_idx   on public.observations (observed_on);

create trigger observations_set_updated_at
  before update on public.observations
  for each row execute function public.set_updated_at();

-- --- feedback ----------------------------------------------------------------

create table public.feedback (
  id             uuid primary key default gen_random_uuid(),
  batch_id       uuid not null references public.batches (id) on delete cascade,
  source         public.feedback_source not null,
  collected_on   date,
  -- The responses live in the platform or the Google Form. We store the link
  -- and, at most, a headline score — never a copy of the responses.
  external_url   text,
  external_id    text,
  summary_score  numeric(4,2),
  response_count integer,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now(),

  constraint feedback_response_count_ck check (response_count is null or response_count >= 0),
  constraint feedback_summary_score_ck  check (summary_score is null or summary_score >= 0)
);

comment on table public.feedback is
  'Pointer to a feedback collection for a batch. Responses stay in the platform / Google Form.';

create index feedback_batch_id_idx on public.feedback (batch_id);

create trigger feedback_set_updated_at
  before update on public.feedback
  for each row execute function public.set_updated_at();

-- --- assessments -------------------------------------------------------------

create table public.assessments (
  id           uuid primary key default gen_random_uuid(),
  batch_id     uuid not null references public.batches (id) on delete cascade,
  title        text,
  conducted_on date,
  clickup_url  text,
  report_url   text,
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);

comment on table public.assessments is
  'Assessment run for a batch. ClickUp owns the work item; we hold the links.';

create index assessments_batch_id_idx on public.assessments (batch_id);

create trigger assessments_set_updated_at
  before update on public.assessments
  for each row execute function public.set_updated_at();

-- --- attendance_records (STUDENT grain) --------------------------------------
-- `student_id` is nullable so the same table holds either grain: per-student
-- rows where a roster exists, or a single session-level row where it does not.
-- Attendance % in the college view is computed over whatever rows are present.

create table public.attendance_records (
  id             uuid primary key default gen_random_uuid(),
  deployment_id  uuid not null references public.deployments (id) on delete cascade,
  student_id     uuid references public.students (id) on delete cascade,
  session_date   date not null,
  status         public.attendance_status not null,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now()
);

comment on table public.attendance_records is
  'STUDENT attendance. Grain is per-student when student_id is set, per-session when NULL. Not a payout input — see trainer_attendance.';

create index attendance_records_deployment_date_idx
  on public.attendance_records (deployment_id, session_date);
create unique index attendance_records_unique_mark
  on public.attendance_records (
    deployment_id,
    session_date,
    coalesce(student_id, '00000000-0000-0000-0000-000000000000'::uuid)
  );

create trigger attendance_records_set_updated_at
  before update on public.attendance_records
  for each row execute function public.set_updated_at();

-- --- trainer_attendance (TRAINER-DAY grain) ----------------------------------
-- CLAUDE.md §6: "Attendance storage is one row per day, never a wide D1-D31
-- layout. Payout disputes are always about specific days." The UNIQUE constraint
-- below is that rule, made unbreakable — a wide layout cannot express "who
-- changed the 14th, and when", and a second row for the same day would let two
-- marks disagree with no tiebreaker while the payout engine silently picks one.

create table public.trainer_attendance (
  id             uuid primary key default gen_random_uuid(),
  deployment_id  uuid not null references public.deployments (id) on delete cascade,
  mark_date      date not null,
  mark           public.attendance_mark not null,
  marked_by      uuid references public.profiles (id) on delete set null,
  -- marked_at is this table's created_at: the operational timestamp of the mark.
  marked_at      timestamptz not null default now(),
  note           text,
  updated_at     timestamptz not null default now(),

  constraint trainer_attendance_unique_day unique (deployment_id, mark_date)
);

comment on table public.trainer_attendance is
  'One trainer-day mark against a deployment. The input to payable days (CLAUDE.md §5-§6). One row per day, enforced.';
comment on column public.trainer_attendance.mark is
  'P / A / H / HOLIDAY / UNMARKED. The payable-day consequence is asymmetric: CRT counts UP from P, bCAP counts DOWN from the period. A missing row and an UNMARKED row are equivalent to the engine — which is exactly why attendance completeness is a hard block for CRT and only a warning for bCAP (§5).';
comment on column public.trainer_attendance.note is
  'Free text for the day: why it was a holiday, why half. Read by humans resolving a payout dispute, never parsed.';

create index trainer_attendance_deployment_idx on public.trainer_attendance (deployment_id, mark_date);
create index trainer_attendance_date_idx       on public.trainer_attendance (mark_date);

create trigger trainer_attendance_set_updated_at
  before update on public.trainer_attendance
  for each row execute function public.set_updated_at();

-- NOT constrained here: that mark_date falls inside the deployment window. That
-- is a cross-row check, so it would need a trigger, and a trigger that silently
-- rejects a mark is a bad place to learn about a date-entry error. It is a §7
-- validation gate in Python instead, where the failure can say which day and
-- which window.

-- =============================================================================
-- RLS
-- =============================================================================

alter table public.observations       enable row level security;
alter table public.observations       force  row level security;
alter table public.feedback           enable row level security;
alter table public.feedback           force  row level security;
alter table public.assessments        enable row level security;
alter table public.assessments        force  row level security;
alter table public.attendance_records enable row level security;
alter table public.attendance_records force  row level security;
alter table public.trainer_attendance enable row level security;
alter table public.trainer_attendance force  row level security;

grant select, insert, update, delete on public.observations       to authenticated;
grant select, insert, update, delete on public.feedback           to authenticated;
grant select, insert, update, delete on public.assessments        to authenticated;
grant select, insert, update, delete on public.attendance_records to authenticated;
grant select, insert, update, delete on public.trainer_attendance to authenticated;

-- --- INTERNAL ----------------------------------------------------------------
-- None of these tables carries a rate or an amount, so all three internal
-- personas reach them on the same terms — an LDE Executive owns exactly this
-- work (§4: "attendance, batches, daily tasks"). The commercials wall is not
-- involved here and must not be copy-pasted in: adding can_see_commercials() to
-- attendance would lock the campus out of its own job.

create policy observations_internal_all on public.observations
  for all to authenticated
  using (public.is_internal() and public.can_reach_deployment(deployment_id))
  with check (public.is_internal() and public.can_reach_deployment(deployment_id));

create policy feedback_internal_all on public.feedback
  for all to authenticated
  using (public.is_internal() and public.can_reach_batch(batch_id))
  with check (public.is_internal() and public.can_reach_batch(batch_id));

create policy assessments_internal_all on public.assessments
  for all to authenticated
  using (public.is_internal() and public.can_reach_batch(batch_id))
  with check (public.is_internal() and public.can_reach_batch(batch_id));

create policy attendance_records_internal_all on public.attendance_records
  for all to authenticated
  using (public.is_internal() and public.can_reach_deployment(deployment_id))
  with check (public.is_internal() and public.can_reach_deployment(deployment_id));

create policy trainer_attendance_internal_all on public.trainer_attendance
  for all to authenticated
  using (public.is_internal() and public.can_reach_deployment(deployment_id))
  with check (public.is_internal() and public.can_reach_deployment(deployment_id));

-- --- TRAINER -----------------------------------------------------------------
-- observations and feedback: NO trainer policy. Evaluative records about the
-- trainer; §4 grants them "own deployment, tracksheet, invoice status. Nothing
-- else." Deny by default until OPS rules otherwise — reversing it is one policy
-- per table using the existing predicates, and no schema change.

create policy assessments_trainer_select_deployed on public.assessments
  for select to authenticated
  using (public.trainer_on_batch(batch_id));

create policy attendance_records_trainer_select_own on public.attendance_records
  for select to authenticated
  using (public.trainer_on_deployment(deployment_id));

create policy attendance_records_trainer_insert_own on public.attendance_records
  for insert to authenticated
  with check (public.trainer_on_deployment(deployment_id));

-- Trainers mark and read their OWN days. SELECT and INSERT only — no UPDATE and
-- no DELETE, deliberately.
--
-- This table decides what the caller is paid. If the payee could rewrite a day
-- after the fact, the audit trail that resolves a payout dispute would be
-- exactly as trustworthy as the person disputing it. Corrections go through the
-- LDE Executive or Manager who holds the campus, which is also who the trainer
-- would be talking to anyway. The UNIQUE constraint means a mistaken mark fails
-- loudly on re-insert rather than being silently overwritten.
create policy trainer_attendance_trainer_select_own on public.trainer_attendance
  for select to authenticated
  using (public.trainer_on_deployment(deployment_id));

create policy trainer_attendance_trainer_insert_own on public.trainer_attendance
  for insert to authenticated
  with check (public.trainer_on_deployment(deployment_id));

-- --- COLLEGE -----------------------------------------------------------------
-- No policy on any of these tables. §4 grants the college an attendance SUMMARY,
-- which row-level RLS cannot express — it is served by the curated view in 0800.
-- trainer_attendance is never exposed to a college in any form: it is a payroll
-- record about a byteXL contractor, not a delivery metric.
