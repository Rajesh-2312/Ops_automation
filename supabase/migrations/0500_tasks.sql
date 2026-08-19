-- =============================================================================
-- byteXL Ops Intelligence Platform — 0500 — task_templates, tasks, urgency
-- =============================================================================
-- The checklist engine. `task_templates` is the seed library FastAPI reads to
-- auto-generate a program's `tasks` when the program is created; 0900-era
-- scheduling (1000) then stamps a real due_date on each one.
--
-- Visibility here is one of the sharper edges of §4:
--   - internal staff see the checklist for programs they REACH, which for an
--     LDE Executive means their campus and nothing else;
--   - a trainer sees ONLY tasks they personally own — not the tasks of programs
--     they are deployed on, which would leak the wider operational plan;
--   - the college sees nothing at all. Internal task ownership is exactly the
--     kind of detail that turns "we are on it" into "your program is behind and
--     here is who to blame".
--
-- CADENCE IS A LABEL, NOT A SCHEDULER
-- -----------------------------------
-- A daily task is one row. It does not spawn ninety dated children across the
-- program window. Generating instances is scheduling logic and CLAUDE.md §3 puts
-- that in FastAPI, next to checklist generation — not in the database and not in
-- React.
--
-- URGENCY IS NOT STORED, DELIBERATELY
-- -----------------------------------
-- "Urgent" is derived at read time from due_date and status. A stored priority
-- flag would be correct on the day someone set it and quietly wrong every day
-- after. The `task_urgency` view below is the single definition, so the UI and
-- any future roll-up cannot disagree about what overdue means.
-- =============================================================================

-- --- task_templates ----------------------------------------------------------

create table public.task_templates (
  id                  uuid primary key default gen_random_uuid(),
  stage               public.program_stage not null,
  title               text not null,
  description         text,
  -- The responsibility matrix: owners are a ROLE plus a specific owner_id at
  -- assignment time, never a hardcoded name. Defaulting to 'manager' rather than
  -- the widest persona means a template added without thought lands on someone
  -- who can actually action it, instead of on a Senior Manager's escalation
  -- queue.
  default_owner_role  public.app_role not null default 'manager',
  order_index         integer not null,
  cadence             public.task_cadence not null default 'one_time',
  -- NULL means "applies to every program type"; set to narrow a template to
  -- bCAP or CRT only. The two types will not share every checklist item forever
  -- — attendance completeness alone is a hard block on one and a warning on the
  -- other (§5).
  applies_to_type     public.program_type,
  is_active           boolean not null default true,
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now()
);

comment on table public.task_templates is
  'Per-stage checklist template. Seeds `tasks` when a program is created. Curated in supabase/seed.sql.';
comment on column public.task_templates.default_owner_role is
  'Which persona owns this class of work. Never a person — assignment happens per program.';
comment on column public.task_templates.cadence is
  'How often the work recurs. Metadata only; nothing generates instances from it.';

create index task_templates_stage_order_idx on public.task_templates (stage, order_index);
create index task_templates_cadence_idx     on public.task_templates (cadence);

-- The conflict target the seed relies on. Case-insensitive so that re-running
-- the seed after a wording tweak updates the row rather than creating a near
-- duplicate.
create unique index task_templates_stage_title_key
  on public.task_templates (stage, lower(title));

create trigger task_templates_set_updated_at
  before update on public.task_templates
  for each row execute function public.set_updated_at();

-- --- tasks -------------------------------------------------------------------

create table public.tasks (
  id             uuid primary key default gen_random_uuid(),
  program_id     uuid not null references public.programs (id) on delete cascade,
  template_id    uuid references public.task_templates (id) on delete set null,
  stage          public.program_stage not null,
  title          text not null,
  description    text,
  owner_id       uuid references public.profiles (id) on delete set null,
  owner_role     public.app_role,
  status         public.task_status not null default 'pending',
  cadence        public.task_cadence not null default 'one_time',
  due_date       date,
  completed_at   timestamptz,
  -- A `blocked` status with no reason and no counterparty is an unactionable
  -- status. Ops delays are nearly always handoffs — HR, L&S, ERM, the college —
  -- so a blocked task records who it is waiting on. That is what turns a status
  -- into an escalation list.
  blocked_reason text,
  waiting_on     text,
  -- Orchestrator link fields. A task really tracked in ClickUp / ZOHO / a Google
  -- Form points at it; we never copy the other system's data.
  source_system  text,
  external_id    text,
  external_url   text,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now()
);

comment on table public.tasks is
  'A checklist item on a program. Tasks within a stage run in parallel.';
comment on column public.tasks.waiting_on is
  'Who the task is blocked behind — HR, L&S, ERM, the college, a trainer. Free text: the counterparties are outside our system and we do not duplicate their records.';
comment on column public.tasks.external_url is
  'Deep link to the item in its owning system. Link, never duplicate.';

create index tasks_program_stage_idx on public.tasks (program_id, stage);
create index tasks_owner_id_idx      on public.tasks (owner_id) where owner_id is not null;
create index tasks_status_idx        on public.tasks (status);
create index tasks_due_date_idx      on public.tasks (due_date) where due_date is not null;
create index tasks_cadence_idx       on public.tasks (cadence) where cadence <> 'one_time';
create index tasks_blocked_idx       on public.tasks (program_id) where status = 'blocked';

create trigger tasks_set_updated_at
  before update on public.tasks
  for each row execute function public.set_updated_at();

-- --- Column guard + completion stamp -----------------------------------------
-- A task owner may progress their own work; they may not reassign it, move its
-- due date, relabel its cadence, or move it to another program. Cadence is on
-- the forbidden list because it is program design, not task progress — a trainer
-- who relabelled a daily job as one-time would make it vanish from the daily
-- filter without ever touching its status.

create or replace function public.tasks_guard_and_stamp()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  -- Service-role / migration / seed context, or internal staff who already
  -- passed the row-level policy for this program.
  if (select auth.uid()) is not null and not public.is_internal() then
    if new.program_id is distinct from old.program_id
       or new.template_id is distinct from old.template_id
       or new.stage      is distinct from old.stage
       or new.title      is distinct from old.title
       or new.owner_id   is distinct from old.owner_id
       or new.owner_role is distinct from old.owner_role
       or new.cadence    is distinct from old.cadence
       or new.due_date   is distinct from old.due_date then
      raise exception 'A task owner may only update status, description, blocker and external link fields'
        using errcode = '42501';
    end if;
  end if;

  -- Keep completed_at consistent with status regardless of who is editing. A
  -- completion timestamp that survives a re-open is worse than none: every
  -- cycle-time report downstream would treat the task as finished.
  if new.status = 'done' and old.status is distinct from 'done' then
    new.completed_at := now();
  elsif new.status is distinct from 'done' then
    new.completed_at := null;
  end if;

  return new;
end;
$$;

create trigger tasks_guard_and_stamp
  before update on public.tasks
  for each row execute function public.tasks_guard_and_stamp();

-- =============================================================================
-- Derived urgency
-- =============================================================================
-- security_invoker = TRUE here, unlike the college views in 0800. That is the
-- important difference: this view must run as the CALLER so the policies on
-- `tasks` still apply. A Manager sees urgency across their colleges, an LDE
-- across their campus, a trainer for their own tasks, a college nothing at all —
-- all without a single policy of its own.

create view public.task_urgency
with (security_invoker = true) as
select
  t.*,
  case
    when t.status = 'done'                                     then 'done'
    when t.status = 'blocked'                                  then 'blocked'
    when t.due_date is null                                    then 'unscheduled'
    when t.due_date <  current_date                            then 'overdue'
    when t.due_date =  current_date                            then 'due_today'
    when t.due_date <= current_date + 3                        then 'due_soon'
    else                                                            'scheduled'
  end as urgency,
  case
    when t.status in ('done', 'blocked') or t.due_date is null then null
    else t.due_date - current_date
  end as days_until_due
from public.tasks t;

comment on view public.task_urgency is
  'tasks plus a derived urgency band. security_invoker = true, so RLS on tasks still applies — unlike the college views in 0800, which intentionally bypass it.';

revoke all on public.task_urgency from public, anon;
grant select on public.task_urgency to authenticated;

-- =============================================================================
-- RLS
-- =============================================================================

alter table public.task_templates enable row level security;
alter table public.task_templates force  row level security;
alter table public.tasks          enable row level security;
alter table public.tasks          force  row level security;

grant select, insert, update, delete on public.task_templates to authenticated;
grant select, insert, update, delete on public.tasks          to authenticated;

-- --- task_templates ----------------------------------------------------------
-- All internal staff read the library; only an admin curates it. A template edit
-- changes every program generated afterwards, so it is an org-wide act even
-- though it looks like a text change.

create policy task_templates_internal_select on public.task_templates
  for select to authenticated
  using (public.is_internal());

create policy task_templates_admin_write on public.task_templates
  for all to authenticated
  using (public.is_admin())
  with check (public.is_admin());

-- --- tasks -------------------------------------------------------------------
-- Scoped by program reach, not by persona rank: a Manager and an LDE Executive
-- run the same policy and differ only in which colleges their assignments cover.

create policy tasks_internal_all on public.tasks
  for all to authenticated
  using (public.is_internal() and public.can_reach_program(program_id))
  with check (public.is_internal() and public.can_reach_program(program_id));

-- §4 denies trainers the pipeline and the assignment list but they do own
-- checklist items (travel submission, tracksheet confirmation, daily
-- attendance). Owner-scoped, therefore — NOT scoped to the programs they are
-- deployed on, which would hand them the whole operational plan.
create policy tasks_trainer_select_own on public.tasks
  for select to authenticated
  using (owner_id = (select auth.uid()));

create policy tasks_trainer_update_own on public.tasks
  for update to authenticated
  using (owner_id = (select auth.uid()))
  with check (owner_id = (select auth.uid()));

-- No INSERT/DELETE for trainers: tasks are generated from templates by FastAPI.
-- No college policy at all.
