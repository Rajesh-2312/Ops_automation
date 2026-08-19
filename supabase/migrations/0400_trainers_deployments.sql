-- =============================================================================
-- byteXL Ops Intelligence Platform — 0400 — trainers, deployments, and the
--                                           trainer visibility path
-- =============================================================================
-- `deployments` is the workhorse join and also the entire basis of trainer
-- authorisation: a trainer reaches data only by walking
--
--     deployments (trainer_id = me) -> batches -> programs -> colleges
--
-- and internal staff reach a trainer by walking the same edges in reverse.
-- Nothing about either is stored denormalised, so revoking a deployment revokes
-- visibility instantly with no cleanup step and no stale grant to find later.
--
-- NOTHING IN THIS FILE IS COMMERCIAL
-- ----------------------------------
-- There is no rate column on trainers or deployments, and that is a deliberate
-- placement decision rather than an omission. CLAUDE.md §4 gives the LDE
-- Executive attendance, batches and daily tasks — all of which require reading
-- deployments — while denying them commercials. If the engagement rate lived on
-- `deployments`, the only way to keep it from an LDE would be to deny them the
-- whole table, and the campus dashboard would go dark. Rates therefore live on
-- `work_orders` in 0700, behind can_see_commercials(). `work_order_status` stays
-- here because a status is not a number: an LDE chasing a signature needs to
-- know it is unsigned without knowing what it is worth.
-- =============================================================================

-- --- trainers ----------------------------------------------------------------

create table public.trainers (
  id                 uuid primary key default gen_random_uuid(),
  -- CLAUDE.md §6: "Trainer identity is PAN." It is the only stable key present
  -- in every legacy sheet and it seeds the invoice number
  -- ({PAN[0:4]}/{FY}/{MON}{seq}). NOT NULL and UNIQUE because the alternative —
  -- matching trainers by name string — is exactly the failure the spec forbids:
  -- "VEMA PRUDHVI SAI" and "Vema Prudhvi Sai" become two trainers, two invoice
  -- sequences, and a duplicate payout that nobody notices until reconciliation.
  pan                text not null,
  full_name          text not null,
  email              text,
  phone              text,
  type               public.trainer_type not null,
  work_order_status  public.doc_status not null default 'not_started',
  -- External system links. We record identifiers and sync state only; ZOHO and
  -- ERM remain systems of record (CLAUDE.md §10 — no scraper, no mirror).
  zoho_id            text,
  zoho_url           text,
  erm_status         public.erm_status not null default 'not_pushed',
  erm_external_id    text,
  erm_url            text,
  erm_synced_at      timestamptz,
  erm_synced_by      uuid references public.profiles (id) on delete set null,
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now(),

  -- §7 validation gate: "PAN (10 chars)". Length is checked here because it is
  -- cheap, absolute, and catches a truncated paste at write time. The full
  -- well-formedness check (AAAAA9999A, with the fourth character encoding
  -- holder type) is a §7 gate in Python, where a rejection can carry an
  -- explanation a human can act on.
  constraint trainers_pan_length_ck check (char_length(pan) = 10),

  -- Case normalisation is part of the identity claim, not cosmetics. UNIQUE
  -- alone would happily accept 'abcde1234f' alongside 'ABCDE1234F' and hand the
  -- same human two trainer rows and two invoice sequences — the precise defect
  -- the PAN key exists to prevent.
  constraint trainers_pan_upper_ck check (pan = upper(pan)),
  constraint trainers_pan_key unique (pan)
);

comment on table public.trainers is
  'A freelancer or full-timer who can be deployed to batches. PAN is the identity key (CLAUDE.md §6).';
comment on column public.trainers.pan is
  'Trainer identity. Seeds the invoice number as PAN[0:4]. Never match trainers by name string.';
comment on column public.trainers.zoho_id is
  'Identifier in ZOHO. Never copy ZOHO field data into this table.';
comment on column public.trainers.erm_status is
  'CLAUDE.md §10. Flips to ''stale'' when the local record changes after a sync, and requeues.';
comment on column public.trainers.erm_synced_by is
  'The named person who pasted the field pack into ERM. §10 models the integration as a human sync task, so the human is part of the record.';

create unique index trainers_email_key on public.trainers (lower(email)) where email is not null;
create index trainers_type_idx on public.trainers (type);
create index trainers_erm_status_idx on public.trainers (erm_status) where erm_status <> 'synced';

create trigger trainers_set_updated_at
  before update on public.trainers
  for each row execute function public.set_updated_at();

-- profiles.trainer_id could not be constrained until now.
alter table public.profiles
  add constraint profiles_trainer_id_fkey
  foreign key (trainer_id) references public.trainers (id) on delete set null;

-- --- deployments -------------------------------------------------------------

create table public.deployments (
  id                   uuid primary key default gen_random_uuid(),
  trainer_id           uuid not null references public.trainers (id) on delete restrict,
  batch_id             uuid not null references public.batches (id)  on delete cascade,
  start_date           date,
  end_date             date,
  tracksheet_url       text,
  -- Travel is submitted by the trainer. There is no separate travel entity, so
  -- it is carried here alongside the deployment it belongs to.
  travel_notes         text,
  travel_submitted_at  timestamptz,
  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now(),

  constraint deployments_date_order_ck check (
    start_date is null or end_date is null or end_date >= start_date
  )
);

comment on table public.deployments is
  'Which trainer teaches which batch, from when. The join that defines trainer visibility and the grain of trainer_attendance (0600).';
comment on column public.deployments.tracksheet_url is
  'Link to the tracksheet in its owning system. Link, never duplicate.';

create index deployments_trainer_id_idx on public.deployments (trainer_id);
create index deployments_batch_id_idx   on public.deployments (batch_id);
create unique index deployments_trainer_batch_key
  -- coalesce so that two open-ended deployments of the same trainer to the same
  -- batch still collide. A literal date, not 'epoch', to keep the index
  -- expression unambiguously immutable.
  on public.deployments (trainer_id, batch_id, coalesce(start_date, date '1970-01-01'));

create trigger deployments_set_updated_at
  before update on public.deployments
  for each row execute function public.set_updated_at();

-- =============================================================================
-- Deployment-path predicates
-- =============================================================================
-- SECURITY DEFINER for the same reason as the reach helpers in 0300: inline
-- cross-table EXISTS clauses between the `programs` and `batches` policies would
-- recurse and abort the query outright.

-- --- Internal reach through a deployment -------------------------------------

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
    where d.id = p_deployment_id
      and public.can_reach_college(pr.college_id)
  );
$$;

comment on function public.can_reach_deployment(uuid) is
  'Internal reach to a deployment, via deployments.batch_id -> batches.program_id -> programs.college_id. Gate for trainer_attendance and observations.';

-- Reach to the TRAINER rather than to the deployment: true when the caller
-- reaches any college the trainer currently teaches at. Used by the LDE
-- Executive's trainer view and by the storage policies in 0900.
create or replace function public.can_reach_trainer(p_trainer_id uuid)
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
    where d.trainer_id = p_trainer_id
      and public.can_reach_college(pr.college_id)
  );
$$;

comment on function public.can_reach_trainer(uuid) is
  'True when the caller reaches a college this trainer is deployed to. NOTE: false for a sourced-but-undeployed trainer, which is why the sourcing personas get a separate roster-wide policy below.';

-- --- Trainer's own path -------------------------------------------------------
-- All of these return false when my_trainer_id() is NULL, so every non-trainer
-- persona is denied by default.

create or replace function public.trainer_on_deployment(p_deployment_id uuid)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1
    from public.deployments d
    where d.id = p_deployment_id
      and d.trainer_id = public.my_trainer_id()
  );
$$;

create or replace function public.trainer_on_batch(p_batch_id uuid)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1
    from public.deployments d
    where d.batch_id = p_batch_id
      and d.trainer_id = public.my_trainer_id()
  );
$$;

create or replace function public.trainer_on_program(p_program_id uuid)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1
    from public.deployments d
    join public.batches b on b.id = d.batch_id
    where b.program_id = p_program_id
      and d.trainer_id = public.my_trainer_id()
  );
$$;

create or replace function public.trainer_on_college(p_college_id uuid)
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
    where pr.college_id = p_college_id
      and d.trainer_id = public.my_trainer_id()
  );
$$;

comment on function public.trainer_on_batch(uuid) is
  'True when the caller has a deployment against this batch. The root of all trainer visibility (CLAUDE.md §4: "own deployment, tracksheet, invoice status. Nothing else.").';

revoke execute on function public.can_reach_deployment(uuid)  from public, anon;
revoke execute on function public.can_reach_trainer(uuid)     from public, anon;
revoke execute on function public.trainer_on_deployment(uuid) from public, anon;
revoke execute on function public.trainer_on_batch(uuid)      from public, anon;
revoke execute on function public.trainer_on_program(uuid)    from public, anon;
revoke execute on function public.trainer_on_college(uuid)    from public, anon;

grant execute on function public.can_reach_deployment(uuid)  to authenticated;
grant execute on function public.can_reach_trainer(uuid)     to authenticated;
grant execute on function public.trainer_on_deployment(uuid) to authenticated;
grant execute on function public.trainer_on_batch(uuid)      to authenticated;
grant execute on function public.trainer_on_program(uuid)    to authenticated;
grant execute on function public.trainer_on_college(uuid)    to authenticated;

-- =============================================================================
-- Column guard on deployments
-- =============================================================================
-- §4 lets a trainer submit travel updates and maintain their tracksheet link,
-- but not reassign themselves to another batch or extend their own dates —
-- dates being an input to payable days, an extension is a raise. RLS policies
-- are row-scoped and cannot restrict columns, and a column-level GRANT would
-- restrict internal staff too, so the narrowing is a BEFORE UPDATE trigger.

create or replace function public.deployments_guard_trainer_columns()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  -- Service-role / migration / seed context, or internal staff who already
  -- passed the row-level policy for this deployment.
  if (select auth.uid()) is null or public.is_internal() then
    return new;
  end if;

  if new.id           is distinct from old.id
     or new.trainer_id is distinct from old.trainer_id
     or new.batch_id   is distinct from old.batch_id
     or new.start_date is distinct from old.start_date
     or new.end_date   is distinct from old.end_date then
    raise exception 'A trainer may only update tracksheet_url and travel fields on their own deployment'
      using errcode = '42501';
  end if;

  return new;
end;
$$;

create trigger deployments_guard_trainer_columns
  before update on public.deployments
  for each row execute function public.deployments_guard_trainer_columns();

-- =============================================================================
-- RLS — trainers, deployments
-- =============================================================================

alter table public.trainers    enable row level security;
alter table public.trainers    force  row level security;
alter table public.deployments enable row level security;
alter table public.deployments force  row level security;

grant select, insert, update, delete on public.trainers    to authenticated;
grant select, insert, update, delete on public.deployments to authenticated;

-- --- trainers ----------------------------------------------------------------
-- The trainer roster is NOT college-scoped data: sourcing happens before any
-- deployment exists, so a policy that required reach would make it impossible to
-- record the trainer you are about to deploy. It is split in two instead:
--
--   Senior Manager / Manager — the sourcing and contracting personas — manage
--   the whole roster.
--   LDE Executive — campus scope — reads only trainers deployed to a college
--   they reach. They do not need, and should not have, a list of every
--   freelancer byteXL has ever engaged.
--
-- The `app_role() in (...)` test here is intentionally NOT can_see_commercials()
-- even though the two currently name the same two personas. They mean different
-- things — one is "may see money", the other is "owns the trainer pipeline" —
-- and reusing the money predicate for a non-money purpose is how a wall gets
-- moved by accident when the personas later diverge.

create policy trainers_sourcing_all on public.trainers
  for all to authenticated
  using (public.app_role() in ('senior_manager', 'manager'))
  with check (public.app_role() in ('senior_manager', 'manager'));

create policy trainers_lde_select_deployed on public.trainers
  for select to authenticated
  using (public.is_internal() and public.can_reach_trainer(id));

-- A trainer sees their own trainer record and nothing about any other trainer.
create policy trainers_trainer_select_self on public.trainers
  for select to authenticated
  using (id = public.my_trainer_id());

-- --- deployments -------------------------------------------------------------

create policy deployments_internal_all on public.deployments
  for all to authenticated
  using (public.is_internal() and public.can_reach_batch(batch_id))
  with check (public.is_internal() and public.can_reach_batch(batch_id));

create policy deployments_trainer_select_own on public.deployments
  for select to authenticated
  using (trainer_id = public.my_trainer_id());

-- Narrowed to tracksheet/travel columns by the guard trigger above.
create policy deployments_trainer_update_own on public.deployments
  for update to authenticated
  using (trainer_id = public.my_trainer_id())
  with check (trainer_id = public.my_trainer_id());

-- No college policy on either table: §4 gives the college published artifacts
-- only, and nothing about trainers or internal assignment.

-- =============================================================================
-- Trainer policies for the 0300 tables
-- =============================================================================
-- Deferred to here because every one of them walks through `deployments`.

create policy colleges_trainer_select_deployed on public.colleges
  for select to authenticated
  using (public.trainer_on_college(id));

create policy programs_trainer_select_deployed on public.programs
  for select to authenticated
  using (public.trainer_on_program(id));

create policy batches_trainer_select_deployed on public.batches
  for select to authenticated
  using (public.trainer_on_batch(id));

-- Trainers need student credential state for the batches they actually teach.
create policy students_trainer_select_deployed on public.students
  for select to authenticated
  using (public.trainer_on_batch(batch_id));
