-- =============================================================================
-- byteXL Ops Intelligence Platform — 1200 — profile backfill + admin bootstrap
-- =============================================================================
-- Two one-time repairs for the gap between "an account exists" and "an account
-- can do anything". Both are idempotent and both no-op on a database that was
-- built in the right order.
--
-- PART 1 — accounts that predate the signup trigger.
-- `handle_new_user` (0200) gives every auth.users row a profile, and every RLS
-- helper resolves persona through that row. An account created BEFORE 0200 was
-- applied has no profile and never will: the trigger is AFTER INSERT and does
-- not fire retroactively. Such a user authenticates successfully, then
-- app_role() returns NULL, every policy matches zero rows, and the console shows
-- "No profile found" — which reads as a broken permission model rather than a
-- missing row. That is exactly what happened here: the first account was created
-- at 06:08 and the schema first applied at ~06:20 the same morning.
--
-- PART 2 — the admin bootstrap, which is a genuine chicken-and-egg.
-- Reach comes from `user_college_assignments` / `user_cluster_assignments`
-- (CLAUDE.md §4), those tables are writable only by `is_admin()`, and is_admin
-- is never taken from signup metadata — raw_user_meta_data is attacker
-- controlled, and 0200 is emphatic about not honouring it. Correct, and it means
-- a fresh database has NO admin and no in-app path to create one. Every account
-- is permanently scopeless. The first admin has to come from a migration, which
-- is the one context where granting it is not an escalation.
-- =============================================================================

-- --- Part 1: backfill -------------------------------------------------------
-- Mirrors handle_new_user's logic exactly, including its fallbacks: an
-- unrecognised or absent role becomes 'trainer' with a NULL trainer_id, which
-- resolves to zero rows under every policy in the schema. Least privilege for a
-- row we are creating without the user asking.

insert into public.profiles (id, role, full_name)
select
  u.id,
  coalesce(
    -- Safe cast. A plain ::app_role on junk metadata aborts the whole migration;
    -- checking membership first turns that into the 'trainer' fallback instead.
    case
      when u.raw_user_meta_data ->> 'role'
           = any (enum_range(null::public.app_role)::text[])
      then (u.raw_user_meta_data ->> 'role')::public.app_role
    end,
    'trainer'
  ),
  nullif(u.raw_user_meta_data ->> 'full_name', '')
from auth.users u
left join public.profiles p on p.id = u.id
where p.id is null
on conflict (id) do nothing;

-- --- Part 2: first admin ----------------------------------------------------
-- The oldest account becomes senior_manager + admin, but ONLY while no admin
-- exists. The guard is what makes this safe to keep in the migration history:
-- once anybody holds the flag this statement matches nothing, so it cannot
-- silently re-promote a demoted account or hand admin to a stranger on a
-- database that has moved on.
--
-- senior_manager is not a preference here — profiles_admin_ck (0200) constrains
-- is_admin to that persona, on the reasoning that a scope-limited persona
-- holding admin could widen its own scope.
--
-- No trainer_id / college_id to clear: profiles_role_link_ck already forbids
-- both for internal personas, so any row reaching here has them NULL.
--
-- The privilege-escalation guard trigger permits this. It short-circuits on
-- `auth.uid() is null`, which is true in a migration — see the note at the top
-- of supabase/migrations/README.md about guards not defending against this
-- connection.

update public.profiles p
set role     = 'senior_manager',
    is_admin = true
where not exists (select 1 from public.profiles where is_admin)
  and p.id = (
    select u.id
    from auth.users u
    join public.profiles p2 on p2.id = u.id
    order by u.created_at asc
    limit 1
  );
