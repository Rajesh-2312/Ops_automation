-- =============================================================================
-- 2400 — the TRUNCATE over-grant (SEC-04) and the storage trainers/ folder (F1)
-- =============================================================================
-- Two database-layer gaps left open after 2100/2200. They are unrelated in
-- mechanism and are bundled here only because each is a few lines and both are
-- the last of the DB-layer findings; the two halves are independently
-- reviewable and independently tested.
--
--   Part 1  SEC-04  revoke TRUNCATE / TRIGGER / REFERENCES / MAINTAIN from
--                   `anon` and `authenticated`, and stop new tables inheriting
--                   them.
--   Part 2  F1      `documents_commercials_trainer_rw` gains the reach conjunct
--                   2200 gave `trainer_bank_accounts`, expressed against a PATH
--                   rather than a column.
--
-- =============================================================================
-- PART 1 — SEC-04: TRUNCATE is a privilege RLS cannot restrain
-- =============================================================================
-- PostgreSQL applies row-level security to SELECT / INSERT / UPDATE / DELETE.
-- It does NOT apply it to TRUNCATE, which is gated by table privilege alone.
-- So every policy in this directory is silent on TRUNCATE, and a TRUNCATE grant
-- is an unconditional wipe right over the table it names.
--
-- Every migration here writes the minimal grant:
--
--     grant select, insert, update, delete on public.trainer_attendance
--       to authenticated;
--
-- and every one of them is additive to a wider implicit grant. Supabase ships
-- the project with
--
--     alter default privileges in schema public
--       grant all on tables to anon, authenticated, service_role;
--
-- recorded in `pg_default_acl` as `authenticated=arwdDxtm/postgres`, where
-- D = TRUNCATE, x = REFERENCES, t = TRIGGER and m = MAINTAIN (PG17). Measured
-- on this project immediately before this migration: 27 tables in `public`
-- carried TRUNCATE for both `anon` and `authenticated`.
--
-- Demonstrated impact (security review, in a rolled-back transaction): an
-- `lde_executive` — the least-privileged internal persona, and the one §4 gives
-- no commercials at all — truncated `trainer_attendance`, the payout input of
-- §5/§6: 152 rows to 0. The `trainer` sentinel could do it too, and that
-- persona holds no policy anywhere.
--
-- REACHABILITY, STATED HONESTLY
-- -----------------------------
-- No remote path to this was demonstrated and none is claimed. PostgREST
-- exposes CRUD and `rpc/` and cannot express TRUNCATE; `pg_graphql` likewise.
-- No SECURITY INVOKER function in this schema builds dynamic SQL, and the one
-- schema that does (`test`, from `supabase/tests/02_rls_matrix_test.sql`) is
-- not deployed here. So this is a latent over-grant, not a live exploit — it
-- becomes live the moment anything can run one arbitrary statement as
-- `authenticated`. It is closed now because it is four lines, not because it is
-- burning.
--
-- WHAT IS AND IS NOT REVOKED
-- --------------------------
-- Revoked, from `anon` and `authenticated` only:
--   TRUNCATE    — wipes a table; RLS is silent on it
--   TRIGGER     — lets the grantee attach a trigger to a table they do not own
--   REFERENCES  — lets the grantee pin rows via a foreign key
--   MAINTAIN    — PG17; VACUUM / ANALYZE / REINDEX / CLUSTER / REFRESH, each of
--                 which takes a heavy lock. Not in the review's headline three,
--                 but it arrives through the same default ACL, no code path
--                 wants it, and leaving one letter of `arwdDxtm` behind would
--                 only make the next audit ask why.
--
-- NOT revoked: SELECT / INSERT / UPDATE / DELETE. Those are the four verbs RLS
-- does cover, and the policies are the control for them. Removing them here
-- would break every persona.
--
-- NOT touched: `service_role` and `postgres`. `app/db/session.py` connects with
-- a BYPASSRLS credential — that is the intended FastAPI path, where the Python
-- guard is the wall — and narrowing it would break the backend without closing
-- anything, because a BYPASSRLS role is already past every policy.
--
-- THE DEFAULT PRIVILEGE, WHICH IS THE PART THAT LASTS
-- ---------------------------------------------------
-- Revoking on existing tables fixes today. `alter default privileges` fixes
-- tomorrow: without it the next `create table` in `public` silently re-acquires
-- all four, and this migration reads as done while being undone.
--
-- `alter default privileges` is per-GRANTOR: it keys on the role that CREATES
-- the object, not on the schema alone. All 36 relations in `public` are owned
-- by `postgres`, and `postgres` is the role `DATABASE_URL` connects as, so
-- amending `postgres`'s entry is the one that governs every table this project
-- will create. No `for role` clause is written, so this applies to whichever
-- role runs the migration — which is, by construction, the role that will
-- create the next table.
--
-- `pg_default_acl` also holds a `supabase_admin` entry for `public` carrying
-- the same wide grant. `postgres` is not a member of `supabase_admin` and
-- cannot amend it, so a table created in `public` BY `supabase_admin` would
-- still arrive wide. Nothing in this project creates tables that way, and
-- `tests/security/test_truncate_grant.py` sweeps EVERY table in `public`
-- rather than a fixed list, so if one ever appears it fails there rather than
-- going unnoticed.
-- =============================================================================

revoke truncate, trigger, references, maintain
  on all tables in schema public
  from anon, authenticated;

alter default privileges in schema public
  revoke truncate, trigger, references, maintain
  on tables
  from anon, authenticated;

-- =============================================================================
-- PART 2 — F1: the storage `trainers/` folder had the wall but not the scope
-- =============================================================================
-- `documents_commercials_trainer_rw` (0900:124) is the third policy in the
-- SEC-02/03 family. 2200 fixed the other two and said why this one was left:
--
--   "object policies key off a path prefix rather than a column, so the
--    predicate is a different shape and deserves its own change with its own
--    test. Left open deliberately rather than bundled in half-done."
--
-- This is that change. The folder holds signed work orders, and a signed work
-- order states the rate — which is why 0900 put it behind the commercials wall
-- to begin with. What it never had was the scope: any Manager or Senior Manager
-- in the country could read, overwrite or delete the work order of a trainer
-- deployed only at colleges they do not cover.
--
-- The security review could not demonstrate this against data — the `documents`
-- bucket is empty in this environment — so it is a policy-shape finding that
-- starts leaking on the first upload. The regression tests for it therefore
-- seed objects inside the rolled-back transaction, so the proof is about the
-- policy and does not wait on the bucket ever being used.
--
-- THE PREDICATE IS 2200'S, DELIBERATELY
-- -------------------------------------
-- `public.can_reach_trainer()` — "reachable, or not yet deployed anywhere".
-- 0900's own comment argued against a reach conjunct on exactly the ground 2200
-- then answered:
--
--   "a trainer who has been sourced and contracted but not yet deployed is
--    reachable by NOBODY, and the work order could never be filed in the first
--    place."
--
-- True of 0400's `can_reach_trainer()`. Not true of 2200's, which returns TRUE
-- for a trainer with no deployment row precisely so the contracting window
-- keeps working. Reusing that helper rather than writing a second one keeps one
-- predicate and one story across the payment rails, the ERM field pack and the
-- work-order PDFs — and means a later narrowing of the carve-out narrows all
-- three at once.
--
-- THE NULL GUARD IS LOAD-BEARING — DO NOT DELETE IT
-- -------------------------------------------------
-- `try_uuid()` (0900) returns NULL for a path segment that is not a UUID, so a
-- misfiled object denies instead of raising 22P02 and failing the whole listing
-- query for everyone. The other two 0900 policies rely on that NULL flowing
-- into `can_reach_college()` / `can_reach_program()`, both of which are a bare
-- `exists (... where id = p_id)` and are therefore FALSE for NULL.
--
-- `can_reach_trainer()` is NOT of that shape. Its first branch is
--
--     not exists (select 1 from deployments d where d.trainer_id = p_trainer_id)
--
-- and for p_trainer_id = NULL that subquery matches nothing, so `not exists` is
-- TRUE. Verified live: `select public.can_reach_trainer(null)` -> `t`.
--
-- So the obvious spelling of this fix —
--
--     and public.can_reach_trainer(public.try_uuid((storage.foldername(name))[2]))
--
-- — would make `trainers/not-a-uuid/wo.pdf` readable AND writable by every
-- commercials persona: it would ship the reach conjunct and a bypass for it on
-- the same line, and the bypass is a filename away. The explicit `is not null`
-- in front is what makes a malformed path deny. It is also what makes
-- `trainers/wo.pdf` deny, where `storage.foldername()` returns a one-element
-- array and segment 2 is NULL.
--
-- RESIDUAL, NAMED
-- ---------------
-- A well-formed UUID belonging to no trainer row still passes, because "no such
-- trainer" is indistinguishable from 2200's "deployed nowhere" carve-out. That
-- is the residual the carve-out already carries rather than a new one, and it
-- exposes only objects nobody legitimately owns. `documents_admin_all` remains
-- the way a misfiled object gets moved.
-- =============================================================================

drop policy if exists documents_commercials_trainer_rw on storage.objects;

create policy documents_commercials_trainer_rw on storage.objects
  for all to authenticated
  using (
    bucket_id = 'documents'
    and (storage.foldername(name))[1] = 'trainers'
    and public.can_see_commercials()
    -- Malformed path -> NULL -> DENY. See "THE NULL GUARD IS LOAD-BEARING".
    and public.try_uuid((storage.foldername(name))[2]) is not null
    and public.can_reach_trainer(public.try_uuid((storage.foldername(name))[2]))
  )
  with check (
    bucket_id = 'documents'
    and (storage.foldername(name))[1] = 'trainers'
    and public.can_see_commercials()
    and public.try_uuid((storage.foldername(name))[2]) is not null
    and public.can_reach_trainer(public.try_uuid((storage.foldername(name))[2]))
  );
