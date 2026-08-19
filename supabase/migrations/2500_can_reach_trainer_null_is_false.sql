-- =============================================================================
-- 2500 — can_reach_trainer(NULL) must be FALSE
-- =============================================================================
-- A defect in migration 2200, which I wrote. Found while reviewing the storage
-- policy in 2400.
--
--     select public.can_reach_trainer(null);   -- true      <-- 2200
--     select public.can_reach_college(null);   -- false
--     select public.can_reach_program(null);   -- false
--
-- WHY IT HAPPENS
-- --------------
-- 2200's carve-out branch is:
--
--     not exists (select 1 from public.deployments d where d.trainer_id = p_trainer_id)
--
-- `d.trainer_id = NULL` is NULL for every row, so the subquery is empty, so
-- `not exists` is TRUE — the function reports "this trainer is deployed
-- nowhere, therefore visible to the sourcing pipeline" for an argument that is
-- not a trainer at all. Its siblings are bare `exists (... where id = p_id)`
-- and are FALSE for NULL, which is why only this one is wrong.
--
-- WHY IT IS NOT CURRENTLY EXPLOITABLE
-- -----------------------------------
-- Both call sites 2200 created happen to guarantee a non-NULL argument:
--
--   * `trainer_bank_accounts.trainer_id` is NOT NULL.
--   * `erm_sync_tasks` carries `erm_sync_tasks_subject_ck`, which asserts
--     `(subject_kind = 'trainer') = (trainer_id is not null)`, and the policy
--     predicate already requires `subject_kind = 'trainer'`.
--
-- That is the column constraints holding the line, not the function. A
-- reach helper that answers TRUE for "no trainer" is a loaded gun for the next
-- caller who has no such guarantee — and there already is one: the storage
-- policy in 2400 derives the uuid from an object PATH, where a malformed
-- segment yields NULL. That policy carries its own explicit
-- `try_uuid(...) is not null` guard precisely because of this, and the guard
-- stays after this migration. Two independent checks on a path that turns a
-- filename into a permission is the right number, not one too many.
--
-- WHAT CHANGES
-- ------------
-- The carve-out branch gains `p_trainer_id is not null`. Semantics are
-- otherwise untouched: a real trainer deployed nowhere is still visible to
-- sourcing (the onboarding path 2200 exists to preserve), and a real trainer
-- deployed somewhere is still scoped to callers who reach that college.
--
-- No policy is rewritten. `create or replace function` keeps every existing
-- `using` and `with check` clause working unchanged.
-- =============================================================================

create or replace function public.can_reach_trainer(p_trainer_id uuid)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select
    p_trainer_id is not null
    and (
      -- Deployed nowhere yet: the sourcing carve-out (2200). The NULL guard
      -- above is what stops "no trainer at all" taking this branch.
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
      )
    );
$$;

comment on function public.can_reach_trainer(uuid) is
  'True when the caller covers a college where this trainer is deployed, OR the '
  'trainer is deployed nowhere at all (the sourcing carve-out — migration 2200). '
  'FALSE for NULL: without that guard the carve-out branch answers TRUE for a '
  'missing argument, because `d.trainer_id = NULL` matches no row and `not '
  'exists` is therefore true (migration 2500). Set form so my_college_ids() is '
  'hoisted once; do not rewrite it as nested can_reach_college().';
