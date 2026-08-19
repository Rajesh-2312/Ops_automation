-- =============================================================================
-- byteXL Ops Intelligence Platform — 0300 — clusters, colleges, scope
--                                            assignments, programs, batches,
--                                            students
-- =============================================================================
-- Two things live here, and they are inseparable:
--
--   1. The college-side spine:  clusters -> colleges -> programs -> batches
--                                                            -> students
--   2. The SCOPE GRAPH that decides who can see any of it.
--
-- CLAUDE.md §4: "Persona lives on profiles.role. Reach does not — it comes from
-- two assignment tables." A Manager covers several colleges; a Senior Manager
-- covers a cluster and therefore every college in it. Neither relationship fits
-- in a column on profiles, so both are join tables, and every policy from here
-- to 1000 resolves reach through the helpers defined at the bottom of this file.
--
-- WHY THE ASSIGNMENT TABLES ARE HERE AND NOT IN 0200
-- --------------------------------------------------
-- They reference colleges and clusters, which are created in this file, and a
-- `language sql` function body is parsed and validated at CREATE time — so
-- my_college_ids() physically cannot be written until the tables it reads exist.
-- The same constraint is why the trainer-path helpers wait for 0400. Migration
-- order is a real dependency graph, not a filing convention.
--
-- RLS for the trainer persona on these four tables lives at the end of 0400,
-- because trainer visibility is derived through `deployments`. The tables are
-- deny-by-default from the moment they are created, so there is no window in
-- which they are readable without a policy.
-- =============================================================================

-- --- clusters ----------------------------------------------------------------
-- A Senior Manager's unit of scope. Deliberately thin: a cluster is a bag of
-- colleges with a name, not a hierarchy. Nesting clusters would make
-- my_college_ids() recursive, and a recursive scope resolver is the kind of
-- thing that is correct for a year and then hands someone the wrong college.

create table public.clusters (
  id          uuid primary key default gen_random_uuid(),
  name        text not null unique,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

comment on table public.clusters is
  'A group of colleges. The unit of Senior Manager scope (CLAUDE.md §4). Flat by design — no nesting.';

create trigger clusters_set_updated_at
  before update on public.clusters
  for each row execute function public.set_updated_at();

-- --- colleges ----------------------------------------------------------------

create table public.colleges (
  id             uuid primary key default gen_random_uuid(),
  cluster_id     uuid references public.clusters (id) on delete set null,
  name           text not null,
  city           text,
  contact_name   text,
  contact_email  text,
  contact_phone  text,
  mou_status     public.doc_status not null default 'not_started',
  po_status      public.doc_status not null default 'not_started',
  notes          text,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now()
);

comment on table public.colleges is
  'A college byteXL has an engagement with (CLAUDE.md §4).';
comment on column public.colleges.cluster_id is
  'Expands a Senior Manager''s cluster assignment to this college. ON DELETE SET NULL: deleting a cluster must narrow reach to nothing, never orphan a college into everyone''s scope.';
comment on column public.colleges.mou_status is
  'Contract state only. The signed document itself lives in the `documents` storage bucket (0900).';

create unique index colleges_name_key on public.colleges (lower(name));
create index colleges_cluster_id_idx on public.colleges (cluster_id) where cluster_id is not null;

create trigger colleges_set_updated_at
  before update on public.colleges
  for each row execute function public.set_updated_at();

-- profiles.college_id could not be constrained until now (see 0200's note on
-- the circular dependency between profiles and colleges).
alter table public.profiles
  add constraint profiles_college_id_fkey
  foreign key (college_id) references public.colleges (id) on delete set null;

-- --- user_college_assignments ------------------------------------------------
-- Manager and LDE Executive to colleges. Many-to-many both ways: a manager
-- covers several colleges, a college is covered by a manager and one or more
-- campus executives.

create table public.user_college_assignments (
  id           uuid primary key default gen_random_uuid(),
  user_id      uuid not null references public.profiles (id) on delete cascade,
  college_id   uuid not null references public.colleges (id) on delete cascade,
  assigned_by  uuid references public.profiles (id) on delete set null,
  assigned_at  timestamptz not null default now(),

  constraint user_college_assignments_unique unique (user_id, college_id)
);

comment on table public.user_college_assignments is
  'Direct college reach for internal staff (CLAUDE.md §4). ON DELETE CASCADE on both sides: reach must not outlive either party.';
comment on column public.user_college_assignments.assigned_by is
  'Who granted the reach. SET NULL on delete so the grant survives the granter leaving — an audit row that disappears with its author is not an audit row.';

create index user_college_assignments_user_idx    on public.user_college_assignments (user_id);
create index user_college_assignments_college_idx on public.user_college_assignments (college_id);

-- --- user_cluster_assignments ------------------------------------------------
-- Senior Manager to clusters. Kept as a separate table rather than a nullable
-- pair of columns on one table: a single table with (college_id, cluster_id)
-- and a "exactly one of" CHECK reads fine and then makes every scope query a
-- branch. Two tables, two unambiguous UNIQUE constraints, one UNION.

create table public.user_cluster_assignments (
  id           uuid primary key default gen_random_uuid(),
  user_id      uuid not null references public.profiles (id) on delete cascade,
  cluster_id   uuid not null references public.clusters (id) on delete cascade,
  assigned_by  uuid references public.profiles (id) on delete set null,
  assigned_at  timestamptz not null default now(),

  constraint user_cluster_assignments_unique unique (user_id, cluster_id)
);

comment on table public.user_cluster_assignments is
  'Cluster reach for Senior Managers (CLAUDE.md §4). Expanded to colleges through colleges.cluster_id.';

create index user_cluster_assignments_user_idx    on public.user_cluster_assignments (user_id);
create index user_cluster_assignments_cluster_idx on public.user_cluster_assignments (cluster_id);

-- --- programs ----------------------------------------------------------------

create table public.programs (
  id            uuid primary key default gen_random_uuid(),
  college_id    uuid not null references public.colleges (id) on delete restrict,
  type          public.program_type  not null,
  name          text not null,
  start_date    date,
  end_date      date,
  timetable_url text,
  stage         public.program_stage not null default 'acquisition_setup',
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),

  constraint programs_date_order_ck check (
    start_date is null or end_date is null or end_date >= start_date
  )
);

comment on table public.programs is
  'A training program run for a college. `stage` is an attribute, not a state machine — tasks within a stage run in parallel (CLAUDE.md §5).';
comment on column public.programs.type is
  'CLAUDE.md §5 — the central branch. Drives rate basis and therefore attendance semantics all the way through to payout.';
comment on column public.programs.timetable_url is
  'Link to the timetable in whatever system owns it. Link, never duplicate.';

create index programs_college_id_idx on public.programs (college_id);
create index programs_stage_idx      on public.programs (stage);

create trigger programs_set_updated_at
  before update on public.programs
  for each row execute function public.set_updated_at();

-- --- batches -----------------------------------------------------------------

create table public.batches (
  id                     uuid primary key default gen_random_uuid(),
  program_id             uuid not null references public.programs (id) on delete cascade,
  name                   text not null,
  branch                 text,
  section                text,
  expected_student_count integer,
  created_at             timestamptz not null default now(),
  updated_at             timestamptz not null default now(),

  constraint batches_expected_student_count_ck check (
    expected_student_count is null or expected_student_count >= 0
  )
);

comment on table public.batches is
  'A cohort within a program. The unit a trainer is deployed against.';

create index batches_program_id_idx on public.batches (program_id);

create trigger batches_set_updated_at
  before update on public.batches
  for each row execute function public.set_updated_at();

-- --- students ----------------------------------------------------------------

create table public.students (
  id                  uuid primary key default gen_random_uuid(),
  batch_id            uuid not null references public.batches (id) on delete cascade,
  full_name           text,
  roll_number         text,
  credentials_status  public.credentials_status not null default 'not_created',
  -- Orchestrator link fields: the LMS remains system of record for the student.
  -- We track only credential-issuance state.
  source_system       text,
  external_id         text,
  external_url        text,
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now()
);

comment on table public.students is
  'Credential-tracking stub for a student. The LMS owns the student record — do not duplicate it here.';

create index students_batch_id_idx  on public.students (batch_id);
create unique index students_batch_roll_key
  on public.students (batch_id, roll_number) where roll_number is not null;

create trigger students_set_updated_at
  before update on public.students
  for each row execute function public.set_updated_at();

-- =============================================================================
-- REACH HELPERS — how an internal persona gets from a row to "yes"
-- =============================================================================
-- WHY THESE ARE FUNCTIONS AND NOT INLINE `EXISTS` CLAUSES:
-- A policy on `batches` that subqueries `programs` triggers the policy on
-- `programs`, which (once 0400 adds the trainer path) subqueries `batches`
-- again — Postgres aborts the query with "infinite recursion detected in policy
-- for relation". That is a hard failure at read time, not a slow query. Routing
-- every cross-table reachability check through a SECURITY DEFINER function
-- breaks the cycle, and is faster besides: one indexed lookup instead of a
-- nested policy evaluation per row.
--
-- Every one of them returns FALSE for a caller with no assignments, so trainers,
-- colleges and unassigned new staff are denied by default.

-- Returns a SET, not a scalar, because reach is genuinely many-to-many: a
-- manager covering four colleges has no single "my college". Set-returning also
-- means callers can write `college_id in (select public.my_college_ids())`
-- inside application queries and views without materialising an array or
-- string-joining uuids into a query — the two failure modes a scalar version
-- invites.
--
-- The UNION (not UNION ALL) de-duplicates the case where someone holds both a
-- direct assignment to a college and a cluster assignment covering it, which is
-- normal during a handover.
create or replace function public.my_college_ids()
returns setof uuid
language sql
stable
security definer
set search_path = ''
as $$
  select uca.college_id
  from public.user_college_assignments uca
  where uca.user_id = (select auth.uid())
  union
  select c.id
  from public.user_cluster_assignments ucl
  join public.colleges c on c.cluster_id = ucl.cluster_id
  where ucl.user_id = (select auth.uid());
$$;

comment on function public.my_college_ids() is
  'Every college the caller reaches: direct assignments UNION the colleges of their assigned clusters. Empty set for trainers, colleges and unassigned staff.';

-- Membership test, written against the base tables rather than as
-- `... in (select my_college_ids())`. Same answer, but this form is two indexed
-- lookups with an early exit, whereas the set version expands the caller's whole
-- cluster before comparing — which for a Senior Manager over a 40-college
-- cluster happens once per row of every policy evaluation.
create or replace function public.can_reach_college(p_college_id uuid)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1
    from public.user_college_assignments uca
    where uca.user_id = (select auth.uid())
      and uca.college_id = p_college_id
    union all
    select 1
    from public.user_cluster_assignments ucl
    join public.colleges c on c.cluster_id = ucl.cluster_id
    where ucl.user_id = (select auth.uid())
      and c.id = p_college_id
  );
$$;

comment on function public.can_reach_college(uuid) is
  'True when the caller has direct or cluster-derived reach to this college. The root of all internal-staff visibility.';

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
    where pr.id = p_program_id
      and public.can_reach_college(pr.college_id)
  );
$$;

comment on function public.can_reach_program(uuid) is
  'Reach to a program, via programs.college_id. The scope half of every commercials policy.';

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
    where b.id = p_batch_id
      and public.can_reach_college(pr.college_id)
  );
$$;

comment on function public.can_reach_batch(uuid) is
  'Reach to a batch, via batches.program_id -> programs.college_id.';

-- --- College-persona path predicates ------------------------------------------
-- The same idea for the college's own login. Return false when my_college_id()
-- is NULL, which is every non-college persona.

create or replace function public.college_owns_program(p_program_id uuid)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1
    from public.programs pr
    where pr.id = p_program_id
      and pr.college_id = public.my_college_id()
  );
$$;

create or replace function public.college_owns_batch(p_batch_id uuid)
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
    where b.id = p_batch_id
      and pr.college_id = public.my_college_id()
  );
$$;

comment on function public.college_owns_program(uuid) is
  'Reachability check for the college persona. False when my_college_id() is NULL — deny by default.';

revoke execute on function public.my_college_ids()             from public, anon;
revoke execute on function public.can_reach_college(uuid)      from public, anon;
revoke execute on function public.can_reach_program(uuid)      from public, anon;
revoke execute on function public.can_reach_batch(uuid)        from public, anon;
revoke execute on function public.college_owns_program(uuid)   from public, anon;
revoke execute on function public.college_owns_batch(uuid)     from public, anon;

grant execute on function public.my_college_ids()             to authenticated;
grant execute on function public.can_reach_college(uuid)      to authenticated;
grant execute on function public.can_reach_program(uuid)      to authenticated;
grant execute on function public.can_reach_batch(uuid)        to authenticated;
grant execute on function public.college_owns_program(uuid)   to authenticated;
grant execute on function public.college_owns_batch(uuid)     to authenticated;

-- =============================================================================
-- RLS
-- =============================================================================

alter table public.clusters                 enable row level security;
alter table public.clusters                 force  row level security;
alter table public.colleges                 enable row level security;
alter table public.colleges                 force  row level security;
alter table public.user_college_assignments enable row level security;
alter table public.user_college_assignments force  row level security;
alter table public.user_cluster_assignments enable row level security;
alter table public.user_cluster_assignments force  row level security;
alter table public.programs                 enable row level security;
alter table public.programs                 force  row level security;
alter table public.batches                  enable row level security;
alter table public.batches                  force  row level security;
alter table public.students                 enable row level security;
alter table public.students                 force  row level security;

grant select, insert, update, delete on public.clusters                 to authenticated;
grant select, insert, update, delete on public.colleges                 to authenticated;
grant select, insert, update, delete on public.user_college_assignments to authenticated;
grant select, insert, update, delete on public.user_cluster_assignments to authenticated;
grant select, insert, update, delete on public.programs                 to authenticated;
grant select, insert, update, delete on public.batches                  to authenticated;
grant select, insert, update, delete on public.students                 to authenticated;

-- --- clusters and the assignment tables --------------------------------------
-- The org chart. All internal staff READ it — "who covers Malineni?" is a
-- question an LDE Executive is allowed to answer, and the tables hold no
-- commercials and no student data. Only an admin WRITES it, because writing here
-- is granting reach, which is the one operation that changes what anyone else
-- can see.
--
-- Note the read policies are NOT scoped to the caller's own assignments. Scoping
-- them would hide exactly the rows a manager needs when handing a college over,
-- and would tell an attacker nothing they cannot infer from profiles anyway.

create policy clusters_admin_all on public.clusters
  for all to authenticated
  using (public.is_admin())
  with check (public.is_admin());

create policy clusters_internal_select on public.clusters
  for select to authenticated
  using (public.is_internal());

create policy user_college_assignments_admin_all on public.user_college_assignments
  for all to authenticated
  using (public.is_admin())
  with check (public.is_admin());

create policy user_college_assignments_internal_select on public.user_college_assignments
  for select to authenticated
  using (public.is_internal());

-- A trainer or college login may see their OWN assignment rows (there should be
-- none — the CHECK on profiles keeps internal links off external personas — but
-- an empty self-view is a better failure than a permissions error in the UI).
create policy user_college_assignments_self_select on public.user_college_assignments
  for select to authenticated
  using (user_id = (select auth.uid()));

create policy user_cluster_assignments_admin_all on public.user_cluster_assignments
  for all to authenticated
  using (public.is_admin())
  with check (public.is_admin());

create policy user_cluster_assignments_internal_select on public.user_cluster_assignments
  for select to authenticated
  using (public.is_internal());

create policy user_cluster_assignments_self_select on public.user_cluster_assignments
  for select to authenticated
  using (user_id = (select auth.uid()));

-- --- colleges ----------------------------------------------------------------
-- Internal staff read and update the colleges they reach. They do NOT insert or
-- delete, and that is not an oversight: an insert policy predicated on
-- can_reach_college(id) is unsatisfiable by construction — nobody can reach a
-- college that does not exist yet, so the WITH CHECK could never pass. Creating
-- a college is therefore an admin act, which is also the right answer
-- organisationally: a new college arrives with a cluster and an assignment or it
-- arrives invisible.

create policy colleges_admin_all on public.colleges
  for all to authenticated
  using (public.is_admin())
  with check (public.is_admin());

create policy colleges_internal_select on public.colleges
  for select to authenticated
  using (public.is_internal() and public.can_reach_college(id));

create policy colleges_internal_update on public.colleges
  for update to authenticated
  using (public.is_internal() and public.can_reach_college(id))
  with check (public.is_internal() and public.can_reach_college(id));

-- The college's own login sees its own row.
create policy colleges_college_select_self on public.colleges
  for select to authenticated
  using (id = public.my_college_id());

-- --- programs, batches, students ---------------------------------------------
-- These DO get a full `for all` internal policy, because unlike colleges the
-- WITH CHECK is satisfiable: a manager who reaches a college may create a
-- program under it, and the same predicate that authorises the read authorises
-- the insert. It also means a program cannot be moved to a college the caller
-- cannot reach — the WITH CHECK is evaluated against the NEW row.

create policy programs_internal_all on public.programs
  for all to authenticated
  using (public.is_internal() and public.can_reach_college(college_id))
  with check (public.is_internal() and public.can_reach_college(college_id));

create policy batches_internal_all on public.batches
  for all to authenticated
  using (public.is_internal() and public.can_reach_program(program_id))
  with check (public.is_internal() and public.can_reach_program(program_id));

create policy students_internal_all on public.students
  for all to authenticated
  using (public.is_internal() and public.can_reach_batch(batch_id))
  with check (public.is_internal() and public.can_reach_batch(batch_id));

-- --- COLLEGE: read-only, own engagement only ---------------------------------
-- §4 grants the college published artifacts and curated progress, nothing else.
-- There is no INSERT/UPDATE/DELETE policy for the college persona anywhere in
-- this schema.

create policy programs_college_select_own on public.programs
  for select to authenticated
  using (college_id = public.my_college_id());

create policy batches_college_select_own on public.batches
  for select to authenticated
  using (public.college_owns_program(program_id));

-- students: no college policy. §4 does not grant the college a roster, so it is
-- denied by default; aggregate counts are exposed through the curated view in
-- 0800. Reversing this is a single
--   using (public.college_owns_batch(batch_id))
-- policy if OPS decides otherwise.
