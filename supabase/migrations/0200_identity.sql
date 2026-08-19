-- =============================================================================
-- byteXL Ops Intelligence Platform — 0200 — profiles, persona helpers, signup
-- =============================================================================
-- The keystone migration. Every RLS policy in every later migration resolves the
-- caller's PERSONA through the helpers defined here. Their REACH is resolved by
-- a second set of helpers in 0300, which cannot exist until the assignment
-- tables do.
--
-- Three techniques carry the whole security model. They are not stylistic and
-- removing any one of them silently breaks it:
--
-- 1. SECURITY DEFINER + STABLE + `SET search_path = ''` on every helper.
--    A policy ON profiles that SELECTs FROM profiles recurses infinitely —
--    Postgres aborts with "infinite recursion detected in policy for relation".
--    SECURITY DEFINER functions run as the owner and bypass RLS, which breaks
--    the cycle. STABLE lets Postgres evaluate them once per statement instead of
--    once per row. An empty search_path forces every reference to be
--    schema-qualified (which is why everything below is spelled out in full) and
--    closes the classic definer attack: a caller who creates `public.profiles`
--    in a schema earlier on the path and gets the definer to read it instead.
--
-- 2. FORCE ROW LEVEL SECURITY on every table, so even the table owner is subject
--    to policy. Roles carrying BYPASSRLS — `postgres` and Supabase's
--    `service_role` — still bypass. That is the intended FastAPI path, and
--    exactly why the backend must re-check persona and ownership in code rather
--    than assuming RLS protected a service-role query.
--
-- 3. Column-level narrowing via BEFORE UPDATE triggers. RLS `WITH CHECK` sees
--    only the NEW row; it cannot compare against OLD. So "you may edit your own
--    profile but not your own role" is impossible to express as a policy and is
--    written as a trigger instead. Same pattern on tasks (0500) and deployments
--    (0400).
-- =============================================================================

-- --- Table -------------------------------------------------------------------

-- Extends auth.users with app-level identity. One row per authenticated user.
--
-- trainer_id / college_id are plain uuid here; their FK constraints are added in
-- 0300 and 0400 once those tables exist. That ordering is deliberate and must be
-- preserved: profiles has to exist before colleges, because the assignment
-- tables and every audit-ish `-> profiles(id)` column depend on it, while
-- profiles.college_id depends on colleges. One of the two FKs has to be added
-- out of line, and this is the one.
create table public.profiles (
  id          uuid primary key references auth.users (id) on delete cascade,
  role        public.app_role not null,
  is_admin    boolean         not null default false,
  full_name   text,
  trainer_id  uuid,
  college_id  uuid,
  created_at  timestamptz     not null default now(),
  updated_at  timestamptz     not null default now(),

  -- A profile links to at most one external identity, matching its persona.
  --
  -- NOTE the internal branch: senior_manager / manager / lde_executive carry
  -- NEITHER link. CLAUDE.md §4 is explicit that internal staff never take scope
  -- from profiles — an LDE Executive pinned to a college via profiles.college_id
  -- would be invisible to my_college_ids() and would quietly get zero rows,
  -- which reads as "RLS is broken" rather than "the assignment is missing".
  -- Forbidding the column outright makes that mistake impossible instead of
  -- merely wrong.
  constraint profiles_role_link_ck check (
    (role = 'trainer' and college_id is null)
    or (role = 'college' and trainer_id is null)
    or (role in ('senior_manager', 'manager', 'lde_executive')
        and trainer_id is null and college_id is null)
  ),

  -- Only a Senior Manager may be an admin. Admin is the right to grant reach and
  -- curate templates; giving it to a persona that is itself scope-limited would
  -- let that persona widen its own scope, which is the one escalation this
  -- schema is built to prevent.
  constraint profiles_admin_ck check (
    is_admin = false or role = 'senior_manager'
  )
);

comment on table public.profiles is
  'App identity extending auth.users. Persona lives here; reach does not (CLAUDE.md §4).';
comment on column public.profiles.role is
  'Persona only. Scope comes from user_college_assignments / user_cluster_assignments (0300).';
comment on column public.profiles.is_admin is
  'May manage users, assignments, and templates. Constrained to senior_manager.';
comment on column public.profiles.college_id is
  'ONLY for the college persona (a college''s own login). Never set for internal staff.';

create index profiles_role_idx       on public.profiles (role);
create index profiles_trainer_id_idx on public.profiles (trainer_id) where trainer_id is not null;
create index profiles_college_id_idx on public.profiles (college_id) where college_id is not null;

create trigger profiles_set_updated_at
  before update on public.profiles
  for each row execute function public.set_updated_at();

-- =============================================================================
-- Persona helpers
-- =============================================================================
-- These read `profiles` and nothing else, which is why they can live here while
-- the reach helpers must wait for 0300. Each one returns false/NULL for a caller
-- with no profile row, so deny-by-default falls out for free: a freshly signed
-- up user matches no policy anywhere in the schema.

create or replace function public.app_role()
returns public.app_role
language sql
stable
security definer
set search_path = ''
as $$
  select p.role
  from public.profiles p
  where p.id = (select auth.uid());
$$;

comment on function public.app_role() is
  'The calling user''s persona, or NULL if unauthenticated / no profile row.';

-- byteXL staff, as opposed to an external trainer or college login. Almost every
-- policy in the schema is `is_internal() and can_reach_<thing>()`.
--
-- The is_internal() conjunct looks redundant — a trainer has no assignment rows,
-- so can_reach_*() already returns false for them. It is kept because that is a
-- statement about the DATA, not about the SCHEMA: one bad row in
-- user_college_assignments naming a trainer's profile would otherwise hand that
-- trainer a manager's view. Persona and reach are checked independently so a
-- data error cannot become a privilege escalation.
create or replace function public.is_internal()
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1
    from public.profiles p
    where p.id = (select auth.uid())
      and p.role in ('senior_manager', 'manager', 'lde_executive')
  );
$$;

comment on function public.is_internal() is
  'True for senior_manager / manager / lde_executive. Persona only — says nothing about reach.';

create or replace function public.is_senior_manager()
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1
    from public.profiles p
    where p.id = (select auth.uid())
      and p.role = 'senior_manager'
  );
$$;

comment on function public.is_senior_manager() is
  'True for the senior_manager persona (cluster scope, P&L, payout approval).';

create or replace function public.is_admin()
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1
    from public.profiles p
    where p.id = (select auth.uid())
      and p.role = 'senior_manager'
      and p.is_admin
  );
$$;

comment on function public.is_admin() is
  'True for senior managers flagged as admin: they manage users, assignments and templates.';

-- =============================================================================
-- THE COMMERCIALS WALL
-- =============================================================================
-- CLAUDE.md R5 and §4: an LDE Executive must get ZERO ROWS from pnl,
-- remuneration, invoices and work-order rates — in the database, not in the UI.
--
-- WHY THIS IS A SEPARATE FUNCTION AND NOT `role in (...)` INLINE IN EACH POLICY:
-- the wall is a single rule applied in five or six places. Written inline it is
-- five or six places to get wrong, and the failure is silent — a money policy
-- that says `is_internal()` instead of `can_see_commercials()` still returns
-- rows, still passes a smoke test, and leaks every trainer's rate to every
-- campus executive. As one named predicate it is greppable, it is testable on
-- its own, and adding a persona to the wall is one edit rather than a survey.
--
-- Every money policy in this schema has the shape:
--     using (public.can_see_commercials() and public.can_reach_<scope>(...))
-- Both conjuncts are required. The first is the wall; the second is the scope.
create or replace function public.can_see_commercials()
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1
    from public.profiles p
    where p.id = (select auth.uid())
      and p.role in ('senior_manager', 'manager')
  );
$$;

comment on function public.can_see_commercials() is
  'CLAUDE.md §4 commercials wall: senior_manager and manager ONLY. LDE Executive gets zero rows from every money table. Mirrors app.domain.enums.COMMERCIALS_PERSONAS.';

-- --- External-identity helpers -----------------------------------------------
-- Unchanged from the three-persona design. Both return NULL for the wrong
-- persona, which makes every dependent policy match no rows.

create or replace function public.my_trainer_id()
returns uuid
language sql
stable
security definer
set search_path = ''
as $$
  select p.trainer_id
  from public.profiles p
  where p.id = (select auth.uid())
    and p.role = 'trainer';
$$;

comment on function public.my_trainer_id() is
  'The caller''s trainer id, or NULL. NULL means trainer policies match no rows — deny by default.';

-- Scalar, and singular, on purpose: this is the COLLEGE PERSONA's own college.
-- Internal staff reach many colleges and use my_college_ids() (0300) instead.
-- The two are never interchangeable — using this one for a manager would pin
-- them to profiles.college_id, which the CHECK constraint above forbids them
-- from having.
create or replace function public.my_college_id()
returns uuid
language sql
stable
security definer
set search_path = ''
as $$
  select p.college_id
  from public.profiles p
  where p.id = (select auth.uid())
    and p.role = 'college';
$$;

comment on function public.my_college_id() is
  'The college persona''s own college id, or NULL. Not for internal staff — they use my_college_ids().';

-- These run as the definer, so they must not be callable by anonymous users.
revoke execute on function public.app_role()            from public, anon;
revoke execute on function public.is_internal()         from public, anon;
revoke execute on function public.is_senior_manager()   from public, anon;
revoke execute on function public.is_admin()            from public, anon;
revoke execute on function public.can_see_commercials() from public, anon;
revoke execute on function public.my_trainer_id()       from public, anon;
revoke execute on function public.my_college_id()       from public, anon;

grant execute on function public.app_role()            to authenticated;
grant execute on function public.is_internal()         to authenticated;
grant execute on function public.is_senior_manager()   to authenticated;
grant execute on function public.is_admin()            to authenticated;
grant execute on function public.can_see_commercials() to authenticated;
grant execute on function public.my_trainer_id()       to authenticated;
grant execute on function public.my_college_id()       to authenticated;

-- =============================================================================
-- Privilege-escalation guard
-- =============================================================================
-- A self-update policy is required (users edit their own full_name), but on its
-- own it would let an LDE Executive run
--     update profiles set role = 'manager'
-- and walk straight through the commercials wall. RLS WITH CHECK cannot see the
-- OLD row, so the guard is a trigger — technique 3 from the header.

create or replace function public.profiles_guard_privileged_columns()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  -- No JWT means a service-role, migration, or seed context. RLS has already
  -- decided this caller may write (only BYPASSRLS roles reach here), and FastAPI
  -- is required to re-check persona and ownership in code.
  if (select auth.uid()) is null then
    return new;
  end if;

  -- Admins manage identity; that is the definition of the flag.
  if public.is_admin() then
    return new;
  end if;

  -- Everyone else — including a non-admin senior manager — may edit descriptive
  -- fields only. Persona, admin bit, and both identity links are off limits.
  -- Note this is stricter than the three-persona schema it replaces, where any
  -- ops user could reassign anyone: with reach now derived from assignments,
  -- the ability to change role IS the ability to change scope.
  if new.role       is distinct from old.role
     or new.is_admin   is distinct from old.is_admin
     or new.trainer_id is distinct from old.trainer_id
     or new.college_id is distinct from old.college_id then
    raise exception 'Only an admin may change role, is_admin, trainer_id, or college_id'
      using errcode = '42501';
  end if;

  return new;
end;
$$;

create trigger profiles_guard_privileged_columns
  before update on public.profiles
  for each row execute function public.profiles_guard_privileged_columns();

-- =============================================================================
-- Signup trigger
-- =============================================================================
-- Every auth.users row gets a profile immediately, so there is never a window in
-- which an authenticated caller has no identity — a NULL persona would make
-- app_role() return NULL and the failure would surface as a confusing empty
-- dashboard rather than a clean denial.
--
-- Default persona is 'trainer' with a NULL trainer_id, which resolves to zero
-- rows under every policy in this schema. That is deliberate least privilege: a
-- new signup sees nothing until an admin assigns them. is_admin is never taken
-- from user-supplied metadata — raw_user_meta_data is attacker-controlled at
-- signup, and honouring `{"is_admin": true}` there would be the whole ballgame.

create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  requested_role public.app_role;
begin
  begin
    requested_role := (new.raw_user_meta_data ->> 'role')::public.app_role;
  exception when others then
    requested_role := null;
  end;

  insert into public.profiles (id, role, full_name)
  values (
    new.id,
    coalesce(requested_role, 'trainer'),
    nullif(new.raw_user_meta_data ->> 'full_name', '')
  )
  on conflict (id) do nothing;

  return new;
end;
$$;

create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();

-- =============================================================================
-- RLS
-- =============================================================================

alter table public.profiles enable row level security;
alter table public.profiles force  row level security;

grant select, insert, update, delete on public.profiles to authenticated;

-- Admins manage every profile (CLAUDE.md §4 — assigning reach is an admin act).
create policy profiles_admin_all on public.profiles
  for all to authenticated
  using (public.is_admin())
  with check (public.is_admin());

-- All internal staff READ every profile. This is wider than their data scope and
-- that is intentional: a task owner picker, an escalation list and a "who covers
-- this college" answer all need names across the org chart. profiles carries no
-- commercials and no college data, so this does not touch the §4 wall. Writing
-- is still admin-only, and the guard trigger above stops a self-update from
-- becoming a self-promotion.
create policy profiles_internal_select on public.profiles
  for select to authenticated
  using (public.is_internal());

-- Everyone else sees exactly their own row.
create policy profiles_self_select on public.profiles
  for select to authenticated
  using (id = (select auth.uid()));

-- Self-service edits to descriptive fields only; the guard trigger rejects any
-- attempt to touch role / is_admin / trainer_id / college_id.
create policy profiles_self_update on public.profiles
  for update to authenticated
  using (id = (select auth.uid()))
  with check (id = (select auth.uid()));

-- No INSERT or DELETE policy for non-admins: profiles are created by the signup
-- trigger (which runs as definer) and removed by cascade from auth.users.
