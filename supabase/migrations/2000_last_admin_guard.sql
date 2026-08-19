-- =============================================================================
-- byteXL Ops Intelligence Platform — 2000 — the last admin cannot be removed
-- =============================================================================
-- On 2026-08-15 this database held exactly one admin, that admin's `is_admin`
-- was set to false, and the platform lost the only account that could grant
-- reach. Nothing in the schema objected. Recovery took a hand-written migration
-- — 1200 Part 2, which promotes the oldest account WHILE NO ADMIN EXISTS — and
-- a person who happened to know that statement was there.
--
-- The hole is still open as this file is written: 8 profiles, 1 admin. Every
-- assignment row in `user_college_assignments` / `user_cluster_assignments`,
-- i.e. every internal user's REACH (CLAUDE.md §4), is writable by `is_admin()`
-- alone. One UPDATE on one boolean freezes the whole platform, and the person
-- most likely to run it is the admin themselves, tidying up their own row.
--
-- =============================================================================
-- WHAT CAN ACTUALLY STRIP THE LAST ADMIN
-- =============================================================================
-- Three paths are reachable. A fourth is already closed, and knowing which is
-- which is the difference between guarding the schema and guarding a column.
--
--   1. `update profiles set is_admin = false` on the last admin row. 0200's
--      column guard permits it — the caller IS an admin, and managing identity
--      is the definition of the flag. This is the observed incident.
--   2. `delete from profiles` on the last admin row. `profiles_admin_all` (0200)
--      is `for all`, so an admin may delete any profile including their own.
--   3. `delete from auth.users` for that account. `profiles.id references
--      auth.users on delete cascade`, so the profile goes with it — and a
--      cascading delete fires this table's row triggers, which is what makes
--      path 3 catchable here rather than needing a trigger on auth.users.
--   4. NOT reachable: a role change alone. `profiles_admin_ck` (0200) is
--      `is_admin = false or role = 'senior_manager'`, so
--      `set role = 'manager'` on an admin is rejected as a check violation
--      (23514) and the admin survives. Only the COMBINED
--      `set role = 'manager', is_admin = false` gets through, and that is path 1
--      wearing a hat.
--
-- This guard therefore COUNTS ADMINS rather than watching columns. It catches
-- the combination without enumerating it, and it keeps its meaning if
-- profiles_admin_ck is ever relaxed.
--
-- =============================================================================
-- WHY `AFTER`, WHEN EVERY OTHER GUARD IN THIS SCHEMA IS `BEFORE`
-- =============================================================================
-- 0200 technique 3 uses BEFORE UPDATE triggers for column narrowing, and this
-- file deliberately breaks that pattern. A query inside a BEFORE ROW trigger
-- cannot see the other rows the SAME statement is changing, so:
--
--     update public.profiles set is_admin = false where is_admin;
--
-- would fire once per admin, each firing would see the OTHER admins still
-- flagged, every row would be allowed, and the table would end with zero admins
-- and no error. An AFTER ROW trigger fires once the whole statement has landed,
-- so the count it reads is the count that would be committed. That is the
-- difference between a guard and the appearance of one, and there is a test for
-- exactly this statement.
--
-- =============================================================================
-- WHY A DEFERRABLE CONSTRAINT TRIGGER
-- =============================================================================
-- An admin handover is naturally written demote-then-promote. INITIALLY
-- IMMEDIATE means that order fails at the demote, which is right for a human at
-- a console — the error lands on the statement that caused it. DEFERRABLE means
-- a script that knows what it is doing can say
--
--     begin;
--       set constraints all deferred;
--       update public.profiles set is_admin = false where id = <outgoing>;
--       update public.profiles set role = 'senior_manager', is_admin = true
--         where id = <incoming>;
--     commit;
--
-- and the check runs at COMMIT against the final state. The invariant is not
-- weakened by deferring it: a transaction that ends with no admin still aborts.
-- Only the moment of checking moves.
--
-- =============================================================================
-- WHY THERE IS NO `auth.uid() is null` SHORT-CIRCUIT — read before "fixing" it
-- =============================================================================
-- Every other guard in this schema (0200 profiles, 0400 deployments, 0500
-- tasks) opens with `if auth.uid() is null then return new`, and the migrations
-- README calls that "the sharpest edge in this schema". This one does not, on
-- purpose:
--
--   * Those guards answer "is THIS CALLER allowed to change this column" — a
--     question about authority, which is meaningless when there is no caller.
--     This one answers "does the system still have an administrator" — a
--     question about the resulting STATE, whose answer does not depend on who
--     asked. There is nothing to short-circuit.
--   * Every path that actually produces the incident has a NULL auth.uid(). The
--     Supabase SQL editor runs as `postgres`; the FastAPI backend connects as
--     `service_role`; a psql session uses the connection string in .env. All
--     three bypass RLS and all three would sail past a guard that stepped aside
--     for them. A guard that is silent on 2026-08-15, and silent for the same
--     accident tomorrow, is decoration.
--   * Nothing legitimate is blocked by binding here — see the next two sections,
--     which are the whole argument that this cannot cause a lockout.
--
-- =============================================================================
-- WHY 1200's BOOTSTRAP STILL WORKS — the lockout question
-- =============================================================================
-- A guard that makes recovery impossible is worse than the hole it closes. This
-- one cannot, structurally: it fires only when a row that WAS an admin stops
-- being one. 1200 Part 2 only ever sets `is_admin = true`, and only
-- `where not exists (select 1 from public.profiles where is_admin)` — it ADDS an
-- admin to a database that has none. No old row loses adminness, the WHEN clause
-- below is false, and the trigger does not fire at all. The same holds for any
-- future repair of that shape, and for `handle_new_user` (INSERT is not guarded
-- and could not lower the count anyway).
--
-- So a zero-admin database can always be re-admined. What it can no longer do is
-- quietly BECOME one. There is a test that runs 1200's shipped statement against
-- a zero-admin state with this trigger installed.
--
-- =============================================================================
-- THE EMPTY-TABLE CARVE-OUT — stated plainly, because it is a real gap
-- =============================================================================
-- The invariant enforced is "`profiles` is EMPTY, or contains at least one
-- admin" — not "contains at least one admin". A statement that removes the last
-- profile row entirely is allowed through.
--
--   * It has to be. `supabase/tests/00_isolate.sql` opens the R5 persona harness
--     with `delete from public.profiles;` inside the transaction run_tests.py
--     rolls back unconditionally. Without the carve-out, installing this
--     migration would break that suite — and a security suite that must be
--     disabled to install a security guard is not a trade this repo should make.
--   * What it costs: an admin can still lock everyone out with an unqualified
--     `delete from public.profiles`. That is not the accident being guarded. It
--     is loud, it is total, and it belongs in the same drawer as `drop table`.
--     The accident is one flag on one row, and that is now impossible.
--   * `truncate public.profiles` is allowed for the same reason and by the same
--     reasoning: TRUNCATE fires no row triggers, and its end state is the empty
--     table the carve-out already permits. Consistent rather than overlooked.
--   * "Zero profiles" is precisely the state 1200 Part 1 exists to repair: the
--     accounts still sit in auth.users, the backfill recreates the rows, and
--     Part 2 re-admins the oldest.
--
-- =============================================================================
-- IF YOU GENUINELY MUST GET PAST IT
-- =============================================================================
-- The triggers are disableable by the table OWNER, transactionally, the way
-- 00_isolate.sql already handles 1300's append-only triggers:
--
--     alter table public.profiles disable trigger profiles_guard_last_admin_upd;
--     alter table public.profiles disable trigger profiles_guard_last_admin_del;
--     ...
--     alter table public.profiles enable  trigger profiles_guard_last_admin_upd;
--     alter table public.profiles enable  trigger profiles_guard_last_admin_del;
--
-- `authenticated` cannot do that — ALTER TABLE requires ownership — so the hatch
-- exists for `postgres` alone and is visible in the SQL that used it. Note it
-- takes ACCESS EXCLUSIVE on `profiles`, which every RLS helper in the schema
-- reads: hold it briefly or the whole platform waits.
-- =============================================================================

create or replace function public.profiles_guard_last_admin()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  -- SECURITY DEFINER is load-bearing here, not house style. `profiles` is FORCE
  -- ROW LEVEL SECURITY, so this count run as the INVOKER would be filtered by
  -- policy: a caller who can see only their own row would read "no admin
  -- remains" and be refused a change that is perfectly safe, while the real
  -- answer sat one policy away. The count has to be the true one, so it runs as
  -- the owner — the same reasoning as the helpers at the top of 0200.

  -- The carve-out, argued in the header: an empty table is a teardown, not a
  -- demotion. `00_isolate.sql` depends on this branch.
  if not exists (select 1 from public.profiles) then
    return null;
  end if;

  -- Mirrors public.is_admin() exactly — the flag AND the persona.
  -- profiles_admin_ck already makes the two inseparable; restating it here means
  -- this guard still guards the right thing if that constraint is ever relaxed.
  if exists (
    select 1
    from public.profiles p
    where p.is_admin
      and p.role = 'senior_manager'
  ) then
    return null;
  end if;

  -- The fix belongs in the MESSAGE, not in a HINT: clients drop hints, logs keep
  -- messages, and whoever reads this is mid-incident. Plain ASCII on purpose —
  -- this string ends up in Windows consoles and log files that are not UTF-8.
  raise exception
    'Refusing to leave the platform with no admin: profile % was the last one. '
    'Promote a replacement FIRST, then repeat this change. '
    'update public.profiles set role = ''senior_manager'', is_admin = true '
    'where id = ''<incoming admin profile id>'';', old.id
    using errcode = '42501';
end;
$$;

comment on function public.profiles_guard_last_admin() is
  'Refuses any UPDATE or DELETE that would leave public.profiles non-empty with zero admins. Binds the service-role connection too: unlike 0200''s column guard it has no auth.uid() short-circuit, because it is a statement about system state rather than about caller authority.';

-- Fires only when THIS row stopped being an admin. A promotion, a name edit, or
-- any update to a non-admin row never reaches the function — which is also why
-- 1200's bootstrap is untouched by it.
create constraint trigger profiles_guard_last_admin_upd
  after update on public.profiles
  deferrable initially immediate
  for each row
  when (
    old.is_admin and old.role = 'senior_manager'
    and not (new.is_admin and new.role = 'senior_manager')
  )
  execute function public.profiles_guard_last_admin();

-- Covers both a direct `delete from public.profiles` and the ON DELETE CASCADE
-- from `auth.users` — deleting the last admin's ACCOUNT is the same lockout as
-- deleting their profile, and it now fails with the same message.
create constraint trigger profiles_guard_last_admin_del
  after delete on public.profiles
  deferrable initially immediate
  for each row
  when (old.is_admin and old.role = 'senior_manager')
  execute function public.profiles_guard_last_admin();
