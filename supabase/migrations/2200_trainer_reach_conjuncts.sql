-- =============================================================================
-- 2200 — reach conjuncts for the trainer-scoped commercial tables (SEC-02/03)
-- =============================================================================
-- Migration 2100 closed the PUBLIC route to these tables by stopping a signup
-- from choosing its own persona. It did not close the tables themselves: a
-- legitimately provisioned Manager still reads and writes every trainer's bank
-- rails in the country, regardless of which colleges they cover.
--
-- That is an insider-scope failure rather than a stranger-off-the-internet one,
-- and CLAUDE.md §4 is explicit that persona and reach are different things:
-- "Persona lives on `profiles.role`. Reach does not." A wall that checks only
-- `can_see_commercials()` has checked the persona and skipped the reach.
--
-- WHAT CHANGES
-- ------------
--   trainer_bank_accounts_commercials_all  (1400)  gains a reach conjunct
--   erm_sync_tasks_sourcing_all            (1900)  gains a reach conjunct
--
-- WHAT DELIBERATELY DOES NOT CHANGE
-- ---------------------------------
-- `trainers_sourcing_all` (0400:320-334) stays reach-free. Its comment argues
-- the case and the argument is right: sourcing PRECEDES deployment, so a
-- trainer who is not yet engaged anywhere reaches no college by construction. A
-- reach conjunct there would make the sourcing roster invisible to the people
-- whose job is to build it. 2100 is that policy's fix, not this migration.
--
-- The storage `trainers/` folder policy (0900) has the same gap and is NOT
-- touched here: object policies key off a path prefix rather than a column, so
-- the predicate is a different shape and deserves its own change with its own
-- test. Left open deliberately rather than bundled in half-done.
--
-- THE PREDICATE: "REACHABLE, OR NOT YET DEPLOYED ANYWHERE"
-- -------------------------------------------------------
-- Not plain reachability. A trainer with no deployment row has no college and
-- would fail any reach test, which would break the same sourcing pipeline the
-- carve-out above protects — you cannot collect somebody's bank details for
-- onboarding if you cannot see them until after they are deployed.
--
-- So `can_reach_trainer()` is true when the trainer is engaged somewhere the
-- caller reaches, OR is engaged nowhere at all. The moment a trainer is
-- deployed, they belong to whoever covers that college.
--
-- WRITTEN IN THE SET FORM, ON PURPOSE
-- -----------------------------------
-- This helper resolves colleges with `in (select public.my_college_ids())`
-- rather than by calling `can_reach_college()` per row. `my_college_ids()` is
-- zero-arg, so the planner hoists it into a HashAggregate evaluated once
-- (loops=1); the nested `SECURITY DEFINER` form measures ~450-520 us per row
-- against ~1.8 us for the flat equivalent. Adding a conjunct in the slow form
-- would have made every read of these tables materially worse in order to make
-- it correct. The wider rewrite of the other fifteen policies is a separate
-- change; this one at least does not add to the debt.
-- =============================================================================

create or replace function public.can_reach_trainer(p_trainer_id uuid)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select
    -- Engaged nowhere yet: the sourcing pipeline carve-out. See the header.
    not exists (
      select 1 from public.deployments d where d.trainer_id = p_trainer_id
    )
    or exists (
      select 1
      from public.deployments d
      join public.batches b  on b.id = d.batch_id
      join public.programs p on p.id = b.program_id
      where d.trainer_id = p_trainer_id
        and p.college_id in (select public.my_college_ids())
    );
$$;

comment on function public.can_reach_trainer(uuid) is
  'True when the caller covers a college where this trainer is deployed, OR the '
  'trainer is deployed nowhere at all (the sourcing carve-out — see migration '
  '2200). Written in the set form so my_college_ids() is hoisted once rather '
  'than evaluated per row; do not rewrite it as nested can_reach_college().';

revoke all on function public.can_reach_trainer(uuid) from public;
grant execute on function public.can_reach_trainer(uuid) to authenticated;

-- --- SEC-02: trainer_bank_accounts -------------------------------------------
-- Account number and IFSC. `for all`, so the old gap was a WRITE: a Manager
-- could redirect another region's payments.

drop policy if exists trainer_bank_accounts_commercials_all on public.trainer_bank_accounts;

create policy trainer_bank_accounts_commercials_all
  on public.trainer_bank_accounts
  for all
  using (public.can_see_commercials() and public.can_reach_trainer(trainer_id))
  with check (public.can_see_commercials() and public.can_reach_trainer(trainer_id));

-- --- SEC-03: erm_sync_tasks --------------------------------------------------
-- The field pack carries full_name, PAN, email and phone (app/api/erm.py
-- `_live_pack`). The program-subject branch is already program-scoped; only the
-- trainer-subject branch was persona-only.

drop policy if exists erm_sync_tasks_sourcing_all on public.erm_sync_tasks;

create policy erm_sync_tasks_sourcing_all
  on public.erm_sync_tasks
  for all
  using (
    subject_kind = 'trainer'::public.erm_subject_kind
    and public.app_role() in ('senior_manager', 'manager')
    and public.can_reach_trainer(trainer_id)
  )
  with check (
    subject_kind = 'trainer'::public.erm_subject_kind
    and public.app_role() in ('senior_manager', 'manager')
    and public.can_reach_trainer(trainer_id)
  );
