-- =============================================================================
-- byteXL Ops Intelligence Platform — RLS harness — isolation
-- =============================================================================
-- ⚠️  THIS FILE DELETES EVERY APPLICATION ROW. Read the next paragraph before
-- you panic, and before you ever run it outside run_tests.py.
--
-- It executes as the FIRST statement of the same transaction that loads the
-- fixtures and runs the assertions — the transaction run_tests.py rolls back
-- unconditionally, on pass, on failure and on Ctrl-C. Nothing here is ever
-- committed. Other sessions never observe it either: under MVCC they keep
-- reading the pre-transaction snapshot throughout.
--
-- WHY IT HAS TO EXIST
-- -------------------
-- The strength of the CLAUDE.md §4 / R5 suite comes from UNSCOPED counts.
-- "LDE Executive A sees 2 batches" is only meaningful when the query is
-- `select count(*) from public.batches` with no filter — that is what proves
-- nothing else is reachable. Scoping every assertion to fixture ids would turn
-- each one into "the rows I already know about are visible", which would pass
-- even if the policies leaked every other row in the database.
--
-- So the choice is between weak assertions against live data, or strong
-- assertions against a known-empty state. This buys the second, and pays for it
-- with a delete that is thrown away moments later.
--
-- WHAT IT DOES NOT TOUCH
-- ----------------------
--   auth.users            — real accounts are left alone entirely. Only their
--                           `profiles` rows are cleared (and restored on
--                           rollback), which is all the assertions count.
--   task_templates        — committed seed data (37 rows); the suite asserts it
--   document_templates      is intact, so clearing it would defeat the point.
--   storage.objects       — Supabase blocks direct deletes with a trigger
--                           (storage.protect_delete), so the bucket is left
--                           as-is. Every POSITIVE storage count in the suite is
--                           therefore scoped to the fixture path prefixes; every
--                           ZERO-row storage assertion stays unscoped, which is
--                           where the security value is.
--
-- 27 tables exist in `public`. 25 are cleared below; the two template libraries
-- are the exceptions named above. If a migration adds a table and it is not
-- added here, its rows survive into the run and some unscoped count goes wrong
-- in a way that looks like an RLS failure. Keep this list complete.
--
-- Four were added after this file was first written: `audit_events` and
-- `artifact_versions` (1300), `trainer_bank_accounts` (1400) and
-- `comms_messages` (1700).
-- =============================================================================

-- FK-safe order: leaves first, roots last.
--
-- Three FKs make the order load-bearing rather than cosmetic:
--   programs.college_id            on delete RESTRICT  -> programs before colleges
--   deployments.trainer_id         on delete RESTRICT  -> deployments before trainers
--   work_orders/remuneration.trainer_id  on delete RESTRICT -> both before trainers

delete from public.trainer_attendance;
delete from public.attendance_records;
delete from public.observations;
delete from public.feedback;
delete from public.assessments;

-- 1700 — the outbound queue. Before programs: the FK is ON DELETE CASCADE, so
-- this is belt and braces rather than load-bearing.
delete from public.comms_messages;

delete from public.program_documents;
delete from public.remuneration_sheets;
delete from public.work_orders;
delete from public.governance_reports;
delete from public.pnl;

delete from public.tasks;
delete from public.students;
delete from public.deployments;
-- 1400. Before `trainers` — the FK is ON DELETE CASCADE, so this would happen
-- anyway, but the harness never relies on a cascade to reach a table it names.
delete from public.trainer_bank_accounts;
delete from public.batches;
delete from public.programs;
delete from public.trainers;

delete from public.user_college_assignments;
delete from public.user_cluster_assignments;
delete from public.colleges;
delete from public.clusters;

-- --- 1300: the two append-only tables ----------------------------------------
-- artifact_versions first: nothing references it, but an audit row is the
-- explanation of a version's transition and outliving it reads badly in a
-- partial failure.
--
-- Both carry triggers that reject DELETE outright, and 1300 is explicit that the
-- rejection has no `auth.uid() is null` escape hatch precisely so it also binds
-- the service-role connection the harness runs on. So the only way through is to
-- disable the trigger and say why:
--
--   * it happens inside the transaction run_tests.py rolls back unconditionally,
--     so the trigger is restored on pass, on failure and on Ctrl-C;
--   * 1300's own footer names this dance as the sanctioned form — "that is the
--     only sanctioned way to remove an audit row, and it is visible".
--
-- `alter table ... disable trigger` takes ACCESS EXCLUSIVE and is transactional
-- in Postgres, which is what makes the restore automatic rather than a promise.

alter table public.artifact_versions disable trigger artifact_versions_freeze;
delete from public.artifact_versions;
alter table public.artifact_versions enable  trigger artifact_versions_freeze;

alter table public.audit_events disable trigger audit_events_no_delete;
delete from public.audit_events;
alter table public.audit_events enable  trigger audit_events_no_delete;

-- Profiles only — auth.users itself is untouched, so no real login is affected
-- even momentarily inside the transaction.
delete from public.profiles;