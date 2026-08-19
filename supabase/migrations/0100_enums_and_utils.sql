-- =============================================================================
-- byteXL Ops Intelligence Platform — 0100 — enums and shared utilities
-- =============================================================================
-- Foundation migration. Every enum used anywhere in the schema is declared here,
-- plus the shared updated_at trigger function. No tables, therefore no RLS.
--
-- WHY EVERY ENUM LIVES IN ONE FILE
-- --------------------------------
-- Postgres cannot add a value to an enum and then use that value in the same
-- transaction. Scattering `create type` across migrations therefore makes the
-- next `alter type ... add value` a two-migration dance for whoever comes after.
-- Keeping the vocabulary in one file also makes the CLAUDE.md §11 rule
-- ("enums in domain/, never string literals") auditable: this file and
-- `app/domain/enums.py` are diffable side by side.
--
-- THE PYTHON MIRROR IS BINDING
-- ----------------------------
-- Each value below is the exact string in `app/domain/enums.py`. That module is
-- owned by the application layer; if the two ever disagree, Pydantic will
-- happily serialise a value Postgres rejects and the failure lands at INSERT
-- time in production rather than at import time in CI. Changing a value here is
-- a migration AND a code change, never one without the other.
--
-- Four enums below have no counterpart in `app/domain/enums.py` yet
-- (`attendance_status`, `trainer_type`, `feedback_source`, `credentials_status`,
-- `document_category`, `document_status`). They belong to tables the API does
-- not yet expose. Flagged in supabase/migrations/README.md so the domain layer
-- can pick them up rather than re-derive them from string literals.
-- =============================================================================

create extension if not exists "pgcrypto";  -- gen_random_uuid()

-- --- Identity ----------------------------------------------------------------

-- CLAUDE.md §4. Five personas, hierarchical on the internal side:
--   senior_manager -> manager -> lde_executive (on campus)
-- trainer and college are external-facing and sit outside that chain.
--
-- This enum is declared with all five values at creation, NOT built up with
-- `alter type ... add value` from an earlier three-value version. This is a new
-- database, so there is no history to preserve, and a fresh `create type` is the
-- only form that lets 0200's CHECK constraints reference every value in the same
-- transaction.
--
-- IMPORTANT: persona is NOT scope. `senior_manager` does not mean "sees
-- everything" — it means "sees the clusters assigned to them". Reach comes from
-- user_college_assignments / user_cluster_assignments (0300), never from this
-- column. Mirrors app.domain.enums.Persona.
create type public.app_role as enum (
  'senior_manager',
  'manager',
  'lde_executive',
  'trainer',
  'college'
);

comment on type public.app_role is
  'The five personas of CLAUDE.md §4. Persona only — reach lives in the assignment tables.';

-- --- Programs ----------------------------------------------------------------

-- CLAUDE.md §5 — the central branch. The two types differ in rate basis, and
-- that difference propagates into attendance semantics (see attendance_mark and
-- app.domain.attendance).
create type public.program_type as enum ('bCAP', 'CRT');

-- The program lifecycle. Programs advance through these; tasks within a stage
-- run in parallel, so this is an attribute, not a state machine.
create type public.program_stage as enum (
  'acquisition_setup',
  'trainer_sourcing',
  'trainer_onboarding',
  'deployment',
  'active_monitoring',
  'closeout_finance'
);

-- --- Money -------------------------------------------------------------------

-- CLAUDE.md §6. How an engagement rate is expressed. The basis lives on the
-- signed work order, not on the program type, because the work order is the
-- contract — but in practice CRT implies per_day and bCAP implies per_month, and
-- that correspondence is a §7 validation gate rather than a database constraint.
create type public.rate_basis as enum ('per_day', 'per_month');

-- --- Tasks -------------------------------------------------------------------

create type public.task_status as enum ('pending', 'in_progress', 'done', 'blocked');

-- How often a checklist item recurs. A LABEL, not a scheduler — nothing in this
-- database generates repeat instances from it. Instance generation is FastAPI's
-- job (CLAUDE.md §3).
create type public.task_cadence as enum (
  'one_time',
  'daily',
  'weekly',
  'monthly',
  'quarterly'
);

-- Which program date a task template's offset_days is measured from. Most of the
-- pipeline hangs off the start; closeout only makes sense relative to the end.
create type public.schedule_anchor as enum ('program_start', 'program_end');

-- --- Trainers ----------------------------------------------------------------

create type public.trainer_type as enum ('freelancer', 'full_timer');

-- Shared lifecycle for documents whose contents we track but do not store:
-- colleges.mou_status, colleges.po_status, trainers.work_order_status,
-- remuneration_sheets.payout_status.
create type public.doc_status as enum (
  'not_started',
  'in_progress',
  'sent',
  'signed',
  'expired'
);

-- CLAUDE.md §10 — ERM is external with no API and is synced by a human paste.
-- 'stale' is the value that matters: when the local record changes after a sync
-- the row flips to stale and requeues. Without that drift state the two systems
-- diverge within a month and neither is trusted.
create type public.erm_status as enum (
  'not_pushed',
  'pending',
  'synced',
  'stale',
  'failed'
);

-- --- Attendance --------------------------------------------------------------

-- Trainer-day mark. CLAUDE.md §6: one row per day, never a wide D1-D31 layout,
-- because payout disputes are always about specific days.
--
-- The payable-day consequences are asymmetric between program types and are
-- deliberately NOT encoded here — CRT counts payable days UP from 'P', bCAP
-- counts DOWN from the period length. Keeping the semantics out of the enum
-- stops anyone assuming a mark means the same thing on both paths.
--
-- 'UNMARKED' exists so a day can be explicitly asserted as unmarked. The payout
-- engine treats a missing row and an UNMARKED row identically, so storing it is
-- never required for correctness — it is for humans reconciling a sheet.
create type public.attendance_mark as enum ('P', 'A', 'H', 'HOLIDAY', 'UNMARKED');

-- STUDENT attendance, a different thing entirely from attendance_mark above.
-- Consumed only by the college-facing summary view in 0800. Kept separate
-- because the grain (student-session vs trainer-day), the consumer (college vs
-- payout engine) and the security class (aggregate vs money input) all differ.
create type public.attendance_status as enum ('present', 'absent', 'late', 'excused');

-- --- Students ----------------------------------------------------------------

create type public.credentials_status as enum (
  'not_created',
  'created',
  'shared',
  'activated'
);

-- --- Feedback ----------------------------------------------------------------

create type public.feedback_source as enum ('platform', 'gform');

-- --- Documents ---------------------------------------------------------------

-- One value per working folder in the byteXL Drive, in the same order. These are
-- CATEGORIES, not stages: there are twelve working folders and six pipeline
-- stages, and bending the pipeline to match the filing cabinet would relitigate
-- CLAUDE.md §5-§6. Each category carries the stage that produces it instead, so
-- the filing structure is preserved exactly while the pipeline stays as spec'd.
create type public.document_category as enum (
  'templates_and_masters',    -- 00
  'college_onboarding',       -- 01
  'program_planning',         -- 02
  'trainer_recruitment',      -- 03
  'trainer_onboarding',       -- 04
  'student_credentials',      -- 05
  'trainer_deployment',       -- 06
  'monitoring_and_quality',   -- 07
  'feedback_and_assessments', -- 08
  'governance_reports',       -- 09
  'remuneration',             -- 10
  'invoice_generation',       -- 11
  'archive'                   -- 99
);

-- Deliberately shorter than task_status: a document is either not started, being
-- worked, filed, approved, or does not apply to this program.
create type public.document_status as enum (
  'not_started',
  'in_progress',
  'filed',
  'approved',
  'not_applicable'
);

-- =============================================================================
-- Shared trigger function: keep updated_at honest
-- =============================================================================
-- Attached to every table in this schema. SECURITY INVOKER (the default) is
-- correct here — it only touches the NEW row already being written, so there is
-- nothing to escalate. `set search_path = ''` all the same: a trigger function
-- with a mutable search_path is a hijack surface even when it does no lookups
-- today, and this one will be edited by someone who does not read this comment.

create or replace function public.set_updated_at()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

comment on function public.set_updated_at() is
  'Trigger fn: stamps updated_at on every UPDATE. Attached to all public tables.';
