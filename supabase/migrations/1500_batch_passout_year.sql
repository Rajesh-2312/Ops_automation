-- =============================================================================
-- byteXL Ops Intelligence Platform — 1500 — batch passout year
-- =============================================================================
-- A batch is "the unit a trainer is deployed against" (0300), and until now it
-- carried only `branch` and `section`. That is enough to name a cohort and not
-- enough to tell two of them apart: a college runs CSE-A every year, so `CSE-A`
-- at a given college means one set of students in 2027 and a different set in
-- 2028. Without the graduating year the roster, the attendance and the batch a
-- payout was earned against all collapse into each other the moment a college is
-- in its second year with us — which is the normal case, not the edge case.
--
-- WHY AN INTEGER AND NOT A DATE
-- -----------------------------
-- A passout year is how colleges, students and the MoU all refer to a cohort —
-- "the 2027 batch". It is a label, not an instant. A date would invite a month
-- and a day nobody knows, which would then be rendered back out as a bare year
-- everywhere it is displayed.
--
-- The range check is deliberately loose. Its job is to catch a transposed digit
-- or a two-digit entry (`27` for `2027`), not to encode policy about how far
-- ahead a program may be planned — a tight window would reject a legitimate
-- four-year engagement signed early, and unpicking that costs a migration.
--
-- WHY THIS COLUMN IS NULLABLE, AND WHY THAT IS TEMPORARY
-- -----------------------------------------------------
-- It should be NOT NULL. The entire point of the column is to SEGREGATE batches,
-- and a nullable grouping key produces an "Unknown" bucket — which is where
-- every batch somebody was in a hurry about ends up. Optional segregation stops
-- happening in week three.
--
-- It is nullable anyway because `batches` already had rows when this was
-- written, and NOT NULL needs a value for each of them. There is no honest one
-- available. The rows belong to a program named "bCAP 2026-27" — an ACADEMIC
-- year, which does not determine the cohort's PASSOUT year; a Sep-Dec 2026
-- engagement could be running for finalists graduating in 2027 or for
-- pre-finalists graduating in 2028. Deriving it from `programs.end_date` would
-- produce a plausible number that is wrong roughly half the time, and a wrong
-- passout year is undetectable later precisely because it looks reasonable.
-- CLAUDE.md §14: carry the open question, do not invent the answer.
--
-- So the constraint is deferred rather than guessed:
--
--   1. This migration adds the column nullable. The UI requires it on every NEW
--      batch from the same change, so the null set cannot grow.
--   2. A human sets the year on the existing rows — they are the only party who
--      knows which cohort was contracted.
--   3. A follow-up migration runs `set not null` once `select count(*) from
--      public.batches where passout_year is null` returns 0.
--
-- Step 3 is the migration that makes the guarantee real. Until it ships, treat
-- "nullable" here as a backlog item with a number on it, not as a design.
-- =============================================================================

alter table public.batches
  add column passout_year integer;

-- Applies to non-null values only, so it constrains every row the UI can now
-- create while leaving the pre-existing rows addressable.
alter table public.batches
  add constraint batches_passout_year_ck
    check (passout_year is null or passout_year between 2000 and 2100);

comment on column public.batches.passout_year is
  'Graduating year of the cohort, e.g. 2027. Nullable ONLY until the rows that predate this column are filled in and a follow-up migration sets NOT NULL — see 1500. Not derivable from the program''s academic year.';

-- Composite and in this order: every screen that reads batches is already inside
-- one program and wants that program's cohorts grouped by year. `program_id`
-- first serves both the equality filter and the ordering; a lone index on
-- `passout_year` would serve neither.
create index batches_program_passout_year_idx
  on public.batches (program_id, passout_year);
