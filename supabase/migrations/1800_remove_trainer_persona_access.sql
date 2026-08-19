-- =============================================================================
-- byteXL Ops Intelligence Platform — 1800 — trainers are records, not users
-- =============================================================================
-- Owner's decision, 2026-08-18: educators do not sign in to this platform. Only
-- LDE Executive, Manager and Senior Manager work in it.
--
-- WHAT THIS DOES NOT CHANGE. Trainers remain first-class RECORDS. `trainers`,
-- `trainer_bank_accounts`, `work_orders`, `deployments`, `trainer_attendance`
-- and `remuneration_sheets` are untouched, and internal staff manage all of them
-- exactly as before. PAN is still the identity key (§6). Nothing about payout
-- computation moves. This migration removes ONE thing: the trainer persona's
-- ability to authenticate and act.
--
-- The `college` persona is deliberately untouched and keeps its read-only view.
--
-- =============================================================================
-- WHY THE `trainer` ENUM LABEL SURVIVES — do not "tidy" this away
-- =============================================================================
-- `handle_new_user` (0200) creates every profile with:
--
--     coalesce(requested_role, 'trainer')
--
-- `raw_user_meta_data` is attacker-controlled at signup, so a role that is
-- absent, misspelled or hostile falls back to `trainer`. That label is the
-- DENY-BY-DEFAULT SENTINEL, and it is load-bearing: removing it would make the
-- fallback fail and a malformed signup would either error at the trigger or
-- have to default to a persona that grants something.
--
-- Before this migration the sentinel was imperfect — `trainer` carried real
-- policies, so a forged signup landed on a persona with genuine (if narrow)
-- read access. After it, `trainer` matches NO policy on ANY table, which makes
-- it a strictly better sentinel than it was. That is the argument for keeping
-- the label, and it is why `profiles.trainer_id` and `profiles_role_link_ck`
-- stay too.
--
-- Dropping a label from a Postgres enum already in use is also close to
-- irreversible. There is no upside and a permanent downside.
--
-- =============================================================================
-- WHAT IS REMOVED
-- =============================================================================
-- Seventeen policies. FOUR of them were writes, and two of those are the reason
-- this migration is worth doing carefully rather than quickly:
--
--   * `trainer_attendance_trainer_insert_own` let the PAYEE mark the days that
--     decide their own pay. 0600 argued this was safe because there was no
--     UPDATE or DELETE, so a mistaken mark failed loudly on the unique
--     constraint. That reasoning was sound while trainers were users. They are
--     not, and marking attendance is now wholly the LDE Executive's job (§4).
--   * `attendance_records_trainer_insert_own` did the same for student session
--     attendance.
--
-- NOT removed, despite the name: `documents_commercials_trainer_rw` (0900) is an
-- INTERNAL policy over the `trainers/` storage folder, gated on
-- `can_see_commercials()`. It has nothing to do with the trainer persona.
-- Likewise `trainers_sourcing_all`, `trainers_lde_select_deployed` and
-- `trainer_attendance_internal_all` are internal-staff policies and stay.
--
-- Every drop below is `if exists` so this file is safe to re-run.
-- =============================================================================

-- --- 0400: the trainer's own record, deployment and org context --------------

drop policy if exists trainers_trainer_select_self      on public.trainers;
drop policy if exists deployments_trainer_select_own    on public.deployments;
drop policy if exists deployments_trainer_update_own    on public.deployments;  -- WRITE
drop policy if exists colleges_trainer_select_deployed  on public.colleges;
drop policy if exists programs_trainer_select_deployed  on public.programs;
drop policy if exists batches_trainer_select_deployed   on public.batches;
drop policy if exists students_trainer_select_deployed  on public.students;

-- --- 0500: own tasks ---------------------------------------------------------

drop policy if exists tasks_trainer_select_own on public.tasks;
drop policy if exists tasks_trainer_update_own on public.tasks;                 -- WRITE

-- --- 0600: assessments and BOTH attendance grains ----------------------------

drop policy if exists assessments_trainer_select_deployed     on public.assessments;
drop policy if exists attendance_records_trainer_select_own   on public.attendance_records;
drop policy if exists attendance_records_trainer_insert_own   on public.attendance_records;  -- WRITE
drop policy if exists trainer_attendance_trainer_select_own   on public.trainer_attendance;
drop policy if exists trainer_attendance_trainer_insert_own   on public.trainer_attendance;  -- WRITE

-- --- 0700 / 1400: the money the trainer could see about themselves -----------

drop policy if exists work_orders_trainer_select_own          on public.work_orders;
drop policy if exists remuneration_sheets_trainer_select_own  on public.remuneration_sheets;
drop policy if exists trainer_bank_accounts_trainer_select_own on public.trainer_bank_accounts;

-- =============================================================================
-- The helper functions
-- =============================================================================
-- `my_trainer_id()` (0200) and the four `trainer_on_*()` predicates (0400) are
-- now referenced by no policy in the schema.
--
-- They are NOT dropped. They are `SECURITY DEFINER`, and dropping a definer
-- function that a later migration or a rollback expects to exist is a worse
-- failure than leaving an unreferenced one. Instead EXECUTE is revoked from
-- `authenticated`, which removes the attack surface — an unreferenced definer
-- function nobody can call is inert — while leaving the definitions in place
-- and the change trivially reversible with a single `grant`.
--
-- They keep working for `postgres` / `service_role`, which is what the RLS test
-- harness and any future internal use would need.

revoke execute on function public.my_trainer_id()             from authenticated;
revoke execute on function public.trainer_on_deployment(uuid) from authenticated;
revoke execute on function public.trainer_on_batch(uuid)      from authenticated;
revoke execute on function public.trainer_on_program(uuid)    from authenticated;
revoke execute on function public.trainer_on_college(uuid)    from authenticated;

comment on function public.my_trainer_id() is
  'DORMANT since 1800: trainers are records, not users. No policy references this and EXECUTE is revoked from authenticated. Kept because profiles.trainer_id still exists and dropping a SECURITY DEFINER function is harder to undo than revoking it.';

-- --- 0900: the trainer's own storage folder ----------------------------------
-- §4 gave a trainer read access to their own work order and ERM/ZOHO status
-- inside `trainers/<trainer_id>/`. With no trainer login there is no reader.
--
-- `documents_commercials_trainer_rw` on the same folder is NOT dropped: despite
-- the name it is an INTERNAL policy gated on `can_see_commercials()`, and it is
-- what lets a Manager file a work order in the first place.

drop policy if exists documents_trainer_select_own on storage.objects;
