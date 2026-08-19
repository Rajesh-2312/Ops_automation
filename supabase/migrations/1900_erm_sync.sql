-- =============================================================================
-- byteXL Ops Intelligence Platform — 1900 — the ERM sync task and its drift
--                                           detector
-- =============================================================================
-- CLAUDE.md §10, verbatim and in full, because every decision below is one of
-- its sentences:
--
--   "ERM is external with no API. Do not build a scraper.
--    Model as a sync task with a generated field pack: the system produces the
--    exact field-value list in ERM's own field order, assigns it to a named
--    person, they paste, they confirm. Record carries erm_synced_at,
--    erm_synced_by.
--    If the local record changes after sync, flip to erm_stale and requeue.
--    Without drift detection the two systems diverge within a month and neither
--    is trusted."
--
-- Nothing in this file talks to ERM. There is no HTTP, no scraping, no queue of
-- outbound anything. A row in `erm_sync_tasks` is a job card for a human: here
-- are the fields, in order, go and paste them, come back and say you did. That
-- is the whole integration, and it is deliberate — §10 ends "never block a
-- feature on API access that does not exist".
--
-- =============================================================================
-- DRIFT DETECTION IS THE LOAD-BEARING HALF
-- =============================================================================
-- The field pack is the easy part. The half that decides whether anyone trusts
-- either system a month from now is the trigger set below, so read that first if
-- you are only reading one thing.
--
-- The rule: **an update that changes a field the pack CARRIES, on a record that
-- has been synced, flips the record to `stale` and requeues it.** Three
-- consequences, all of which the triggers are shaped around:
--
--   1. THE WATCH LIST IS THE PACK, NOT THE TABLE. A trainer's `erm_url` changing
--      is not drift — ERM already knows it. `full_name` changing IS drift. So
--      the trigger names the columns the pack reads and no others. That list is
--      duplicated in `app/services/erm/fieldpack.py`, and the duplication is
--      policed: `tests/unit/test_erm_drift.py` parses THIS FILE and fails if the
--      two lists disagree. Add a field to the pack and the test tells you to add
--      the column here.
--
--   2. STAMPING A SYNC MUST NOT LOOK LIKE DRIFT. The confirm path writes
--      `erm_status`, `erm_synced_at`, `erm_synced_by`, `erm_external_id`. None of
--      those is on the watch list, so the stamp cannot mark itself stale on the
--      way in. Getting this backwards produces a record that is stale one
--      millisecond after every sync, which reads as "the detector works" right
--      up until everybody turns it off.
--
--   3. DRIFT ARRIVES SIDEWAYS AS OFTEN AS HEAD-ON. "College Assigned" is in the
--      trainer pack and is not a column on `trainers` at all — it is derived by
--      walking `deployments -> batches -> programs -> colleges`. A trainer moved
--      to another campus drifts their ERM record without `trainers` being
--      touched. Same for `colleges.name` under the program pack. Both get their
--      own trigger; a detector that only watched the obvious table would be
--      silently wrong in the two cases that actually happen in July.
--
-- The requeue is one INSERT and it is idempotent: a partial unique index allows
-- exactly one OPEN task per subject, so a record edited five times in an
-- afternoon produces one job card, not five.
--
-- =============================================================================
-- WHAT THIS FILE DOES NOT DO, AND WHY
-- =============================================================================
-- **No R4 ladder.** Pasting a trainer's phone number into a third-party portal
-- is not an artifact leaving the building — it is internal record-keeping
-- between two systems byteXL already owns the data in. §8 puts the Onboarding
-- agent's ERM work at "Auto (internal only)". So a sync task has its own small
-- state set (queued -> assigned -> confirmed, plus stale and cancelled) rather
-- than borrowing `artifact_state`, and there is no approval gate on it. If ERM
-- ever became college-facing this decision is the one to revisit.
--
-- **No commercials.** Not one field in either pack is a rate, a bank rail, a PAN-
-- adjacent payment detail or a P&L line, and R5 is why: this table is readable by
-- an LDE Executive on the same terms `trainers` is, so a pack carrying a day rate
-- would route a commercial value around `can_see_commercials()`. PAN is in the
-- trainer pack because it is identity (§6), not money.
-- `tests/unit/test_erm_fieldpack.py` asserts the absence rather than trusting it.
--
-- **No DELETE grant.** A confirmed sync task is the evidence that a named person
-- pasted a named set of values on a named day. That is the record §10 asks for.
--
-- =============================================================================
-- THE FIELD ORDER IS NOT VERIFIED, AND THIS FILE DOES NOT PRETEND IT IS
-- =============================================================================
-- §10 asks for "the exact field-value list in ERM's own field order". Nobody on
-- this side of the integration has seen ERM's form. `D:\bytexl_Operations` holds
-- two ERM artefacts and both are LOGS OF THE UPDATE, not the update:
--
--   02_Program_Planning/ERM_Program_Updates/ERM_Program_Update_Log.xlsx
--     S.No | College | Program | Batch | Updated in ERM On | Updated By |
--     ERM Program ID | Verified? | Status | Remarks
--   04_Trainer_Onboarding/ERM_Trainer_Updates/ERM_Trainer_Update_Log.xlsx
--     S.No | Trainer Name | College Assigned | Updated in ERM On | Updated By |
--     ERM Trainer ID | Verified? | Status | Remarks
--
-- Those are the columns of a spreadsheet that RECORDS the paste. They tell us
-- what is worth capturing on this table — who pasted, when, the ERM id they read
-- back, whether anyone verified it — and they are why `confirmed_by`,
-- `confirmed_at`, `erm_external_id`, `verified` and `remarks` exist below. They
-- say nothing about the order of the fields on ERM's own screen.
--
-- So the order is a single declared constant in
-- `app/services/erm/fieldpack.py`, flagged UNVERIFIED in the code, on the wire
-- and on screen, and stamped onto every task as `field_order_version`. When
-- somebody opens ERM and writes the real order down, they bump that constant and
-- every pack generated under the guess stays identifiable. Inventing an order and
-- calling it "ERM's" would make the guess unfalsifiable, which is worse than
-- having no order at all.
--
-- =============================================================================
-- FOLLOW-UPS OWNED BY OTHER FILES
-- =============================================================================
-- * `supabase/tests/00_isolate.sql` clears every application table before the RLS
--   matrix runs. `public.erm_sync_tasks` belongs in it, ahead of
--   `delete from public.trainers` and `delete from public.programs` (both FKs
--   cascade, so the order is belt and braces).
-- * `app/db/models.py` is the typed mirror of this schema (§11). It gains
--   `ErmSyncTask` and the five new `programs.erm_*` columns when that file's
--   owner picks them up; until then `app/services/erm/models.py` carries the
--   mapping beside its only consumer, and says so in its docstring. There is no
--   `information_schema` drift test in the repo yet — `tests/integration/` is
--   empty — so nothing fails today. It should, and when it lands this is the
--   migration it will point at first.
-- =============================================================================

-- =============================================================================
-- 1. Enums
-- =============================================================================
-- Declared here and not in 0100 for 1300's and 1700's reason: 0100 is shipped and
-- "never edit a shipped migration" is the stronger rule. `create type` in full
-- rather than `alter type ... add value`, and both are consumed by a table
-- created below in this same file, so 0100's transaction hazard does not arise.
--
-- Neither has a counterpart in `app/domain/enums.py` yet — that file is closed to
-- this workstream, exactly as it was to the comms workstream — so
-- `app/services/erm/types.py` holds the `StrEnum` pair beside its only consumer
-- and its docstring says where it belongs. `public.erm_status` (0100) is NOT
-- redeclared: the record-level vocabulary already exists, already carries
-- 'stale', and is already mirrored by `app.domain.enums.ErmStatus`.

-- WHICH RECORD is being pushed. Two labels, and they are the two ERM touchpoints
-- the legacy folder actually evidences: program details (workflow step 8) and
-- trainer details (step 13). A third would be a guess about a system nobody here
-- can open.
create type public.erm_subject_kind as enum (
  'trainer',
  'program'
);

comment on type public.erm_subject_kind is
  'Which local record an ERM sync task pushes. Mirrors app.services.erm.types.ErmSubjectKind. The two labels are the two ERM touchpoints evidenced in D:\bytexl_Operations — step 8 (program) and step 13 (trainer).';

-- WHERE the job card is. Not `artifact_state`: see the header on why the ERM
-- sync is not an R4 artifact.
--
--   queued    -> nobody owns it yet
--   assigned  -> a NAMED person owns it (§10: "assigns it to a named person")
--   confirmed -> that person says they pasted it; the pack they used is frozen
--                onto the row and the source record is stamped
--   stale     -> it WAS confirmed and the local record has since changed. The
--                task keeps its evidence and a fresh `queued` task supersedes it
--   cancelled -> the push is not wanted (subject withdrawn, duplicate, mistake)
create type public.erm_sync_state as enum (
  'queued',
  'assigned',
  'confirmed',
  'stale',
  'cancelled'
);

comment on type public.erm_sync_state is
  'Lifecycle of one ERM sync task. Mirrors app.services.erm.types.ErmSyncState. NOT public.artifact_state — an ERM push is internal record-keeping, not an artifact leaving the building (CLAUDE.md §8, "Auto (internal only)").';

-- =============================================================================
-- 2. The program record gains the ERM columns the trainer record already has
-- =============================================================================
-- `trainers` has carried `erm_status`, `erm_external_id`, `erm_url`,
-- `erm_synced_at` and `erm_synced_by` since 0400. `programs` has carried none of
-- them, and step 8 of the legacy workflow is "ERM program updates" — so the
-- program half of §10 has had nowhere to record itself.
--
-- Same five columns, same names, same types, same FK behaviour. Symmetry is the
-- point: the drift triggers below are two copies of one shape, and a reader who
-- has understood the trainer path has understood the program path.
--
-- `erm_status` takes 0100's `public.erm_status`, which already carries 'stale'.
-- §10's "flip to erm_stale" is that label, on this column, set by the trigger in
-- section 4 — not a new boolean, because a boolean could not distinguish
-- 'not_pushed' from 'synced'.

alter table public.programs
  add column erm_status      public.erm_status not null default 'not_pushed',
  add column erm_external_id text,
  add column erm_url         text,
  add column erm_synced_at   timestamptz,
  add column erm_synced_by   uuid references public.profiles (id) on delete set null;

comment on column public.programs.erm_status is
  'CLAUDE.md §10. Flips to ''stale'' when a field the ERM program pack carries changes after a sync, and requeues. Set by erm_programs_detect_drift(); the app writes it only on the confirm path.';
comment on column public.programs.erm_external_id is
  'The ERM Program ID, read back off ERM''s own screen by the person who pasted. Deliberately NOT part of the field pack — it is what ERM tells us, not what we tell ERM.';
comment on column public.programs.erm_synced_by is
  'The named person who pasted the field pack into ERM. §10 models the integration as a human sync task, so the human is part of the record.';

create index programs_erm_status_idx
  on public.programs (erm_status) where erm_status <> 'synced';

-- =============================================================================
-- 3. erm_sync_tasks — the job card
-- =============================================================================

create table public.erm_sync_tasks (
  id                  uuid primary key default gen_random_uuid(),

  -- --- WHAT is being pushed --------------------------------------------------
  -- Polymorphic over exactly two subjects, with a real FK for each rather than
  -- an untyped (kind, id) pair. Two nullable FKs and a CHECK is more columns and
  -- fewer ways to be wrong: a deleted trainer takes their job cards with them,
  -- which an untyped id could not express.
  subject_kind        public.erm_subject_kind not null,
  trainer_id          uuid references public.trainers (id) on delete cascade,
  program_id          uuid references public.programs (id) on delete cascade,

  state               public.erm_sync_state not null default 'queued',

  -- Which declared field order the pack was (or will be) generated under. See
  -- the header: the order is a documented GUESS, and this column is what keeps
  -- the guess falsifiable — when the real order is learned the constant is
  -- bumped and every historical pack stays attributable to the order it used.
  field_order_version integer not null default 1,

  -- --- WHO owns the paste (§10: "assigns it to a named person") --------------
  assigned_to         uuid references public.profiles (id) on delete set null,
  assigned_by         uuid references public.profiles (id) on delete set null,
  assigned_at         timestamptz,

  -- --- WHAT was actually handed over, frozen at confirm ----------------------
  -- Before confirm this is NULL and the pack is generated live from the source
  -- record on every read, so an unconfirmed task can never show a pack that has
  -- gone out of date behind the reader's back.
  --
  -- At confirm it is stamped with the pack the human says they pasted:
  --   [{"label": "Trainer Name", "source": "trainers.full_name", "value": "..."}, ...]
  -- a JSON ARRAY, because order is the entire point of a field pack and an object
  -- does not have one.
  field_pack          jsonb,

  -- The source column values behind that pack, keyed by dotted source path. Not
  -- redundant with `field_pack`: the pack is what a human read (formatted, dates
  -- rendered, nulls shown as blanks) and this is what the database held. It is
  -- what lets a screen say WHICH field drifted, rather than only that something
  -- did.
  source_snapshot     jsonb,

  -- --- WHAT came back (the legacy log's own columns) -------------------------
  erm_external_id     text,
  verified            boolean not null default false,
  remarks             text,

  confirmed_by        uuid references public.profiles (id) on delete set null,
  confirmed_at        timestamptz,

  -- --- DRIFT ------------------------------------------------------------------
  stale_at            timestamptz,
  stale_reason        text,

  -- The requeued task points back at the confirmed task it replaces, so "how
  -- many times has this trainer's ERM record been redone, and why each time" is
  -- a walk rather than an archaeology exercise.
  supersedes_id       uuid references public.erm_sync_tasks (id) on delete set null,

  cancelled_by        uuid references public.profiles (id) on delete set null,
  cancelled_at        timestamptz,
  cancelled_reason    text,

  created_by          uuid references public.profiles (id) on delete set null,
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now(),

  -- Exactly one subject, and it agrees with the label. Both halves: the first
  -- alone permits a trainer task carrying a program_id, which every reach
  -- predicate below would then resolve against the wrong table.
  constraint erm_sync_tasks_subject_ck check (
    (trainer_id is null) <> (program_id is null)
    and (subject_kind = 'trainer') = (trainer_id is not null)
    and (subject_kind = 'program') = (program_id is not null)
  ),

  constraint erm_sync_tasks_field_order_ck check (field_order_version >= 1),

  -- ASSIGNED means a named person owns it. §10 does not say "queue it and hope".
  constraint erm_sync_tasks_assigned_ck check (
    (assigned_to is null) = (assigned_at is null)
    and (state <> 'assigned' or assigned_to is not null)
  ),

  -- CONFIRMED and STALE both mean "this was pasted once": the evidence — who,
  -- when, and the exact pack — survives the record going stale, because a
  -- dispute about what ERM was told is a dispute about the pack that was
  -- handed over, not about the values today.
  constraint erm_sync_tasks_confirmed_ck check (
    (state in ('confirmed', 'stale')) = (confirmed_at is not null)
    and (state in ('confirmed', 'stale')) = (confirmed_by is not null)
    and (state in ('confirmed', 'stale')) = (field_pack is not null)
  ),

  -- A task cannot be stale without having been confirmed first — that is what
  -- the word means here. Enforced by the confirmed check above plus this one.
  constraint erm_sync_tasks_stale_ck check ((state = 'stale') = (stale_at is not null)),

  constraint erm_sync_tasks_cancelled_ck check (
    (state = 'cancelled') = (cancelled_at is not null)
  ),

  constraint erm_sync_tasks_pack_array_ck check (
    field_pack is null or jsonb_typeof(field_pack) = 'array'
  ),
  constraint erm_sync_tasks_snapshot_object_ck check (
    source_snapshot is null or jsonb_typeof(source_snapshot) = 'object'
  ),

  constraint erm_sync_tasks_order_ck check (
    (assigned_at  is null or assigned_at  >= created_at)
    and (confirmed_at is null or confirmed_at >= created_at)
    and (stale_at     is null or confirmed_at is null or stale_at >= confirmed_at)
    and (cancelled_at is null or cancelled_at >= created_at)
  ),

  constraint erm_sync_tasks_supersedes_not_self_ck check (supersedes_id is distinct from id)
);

comment on table public.erm_sync_tasks is
  'CLAUDE.md §10: ERM has no API, so the integration is a job card for a human — generated field pack, named assignee, they paste, they confirm. Nothing here transmits anything. The load-bearing part is the drift trigger set that flips a synced record to stale and requeues it.';
comment on column public.erm_sync_tasks.field_pack is
  'The ordered field-value list as actually handed over, frozen at confirm. A JSON ARRAY because order is the point. NULL before confirm — an open task generates its pack live from the source record so it can never show a stale one.';
comment on column public.erm_sync_tasks.source_snapshot is
  'The source column values behind the confirmed pack, keyed by dotted source path. What lets a screen name WHICH field drifted rather than only that something did.';
comment on column public.erm_sync_tasks.field_order_version is
  'Which declared field order the pack used. ERM''s real field order is UNVERIFIED (see this migration''s header and app/services/erm/fieldpack.py); this column is what keeps the guess falsifiable.';
comment on column public.erm_sync_tasks.erm_external_id is
  'The ERM record id read back off ERM''s own screen. Mirrors the "ERM Trainer ID" / "ERM Program ID" column of the legacy update logs.';
comment on column public.erm_sync_tasks.supersedes_id is
  'The confirmed task this one replaces after drift. §10: "flip to erm_stale and requeue" — the requeued card points back at the sync it invalidates.';

-- The queue, read the three ways a human reads it: what is open, what is mine,
-- and what has this record's ERM history been.
create index erm_sync_tasks_open_idx
  on public.erm_sync_tasks (created_at)
  where state in ('queued', 'assigned');
create index erm_sync_tasks_assigned_to_idx
  on public.erm_sync_tasks (assigned_to)
  where state in ('queued', 'assigned');
create index erm_sync_tasks_trainer_idx on public.erm_sync_tasks (trainer_id, created_at desc);
create index erm_sync_tasks_program_idx on public.erm_sync_tasks (program_id, created_at desc);
create index erm_sync_tasks_state_idx   on public.erm_sync_tasks (state);

-- ONE open job card per record. This is what makes the requeue in section 4 safe
-- to fire on every drifting update: a trainer edited five times in an afternoon
-- produces one card, not five, and the person holding it sees the latest values
-- because the pack is generated live.
create unique index erm_sync_tasks_one_open_trainer
  on public.erm_sync_tasks (trainer_id)
  where trainer_id is not null and state in ('queued', 'assigned');
create unique index erm_sync_tasks_one_open_program
  on public.erm_sync_tasks (program_id)
  where program_id is not null and state in ('queued', 'assigned');

-- One successor per supersession link, for 1700's reason: without it two
-- concurrent requeues both claim the same predecessor and the history forks.
create unique index erm_sync_tasks_one_successor
  on public.erm_sync_tasks (supersedes_id)
  where supersedes_id is not null;

create trigger erm_sync_tasks_set_updated_at
  before update on public.erm_sync_tasks
  for each row execute function public.set_updated_at();

-- =============================================================================
-- 4. DRIFT DETECTION
-- =============================================================================
-- The half of §10 that decides whether anyone trusts either system next month.
--
-- Shape, for both subjects:
--
--   BEFORE UPDATE on the source table   -> did a WATCHED field change on a
--                                          SYNCED record? then new.erm_status
--                                          := 'stale'. No second UPDATE, so no
--                                          recursion.
--   AFTER UPDATE on the source table    -> did erm_status just BECOME 'stale'?
--                                          then mark the confirmed task stale
--                                          and requeue exactly one fresh card.
--   a public marker function            -> for drift that arrives from another
--                                          table entirely (deployments,
--                                          colleges). It sets erm_status and
--                                          lets the AFTER trigger above do the
--                                          requeue, so there is exactly ONE
--                                          requeue site per subject.
--
-- No `auth.uid() is null` short-circuit anywhere in this section, for 1300's and
-- 1700's reason stated once more because it is the trap: the FastAPI service
-- connects with BYPASSRLS and a NULL `auth.uid()`, so a guard that steps aside
-- for NULL is a guard that never runs on the only connection that writes. These
-- are not permission guards in any case — they are bookkeeping, and they must run
-- for migrations, seeds, PostgREST and the service alike.

-- --- The watch lists ----------------------------------------------------------
-- WATCHED COLUMNS — trainers:  full_name, pan, email, phone, type,
--                              work_order_status, zoho_id
-- WATCHED COLUMNS — programs:  name, type, start_date, end_date, college_id
--
-- These two comment lines are not decoration. `tests/unit/test_erm_drift.py`
-- parses the `is distinct from` comparisons out of the two functions below and
-- asserts they equal the source columns declared in
-- `app/services/erm/fieldpack.py`. If you add a field to a pack, this file fails
-- the test until the column is added here too. That is the only mechanism keeping
-- one list in two languages honest, so do not "simplify" the comparisons into
-- dynamic SQL — the test would stop being able to read them and the guarantee
-- would quietly evaporate.
--
-- NOT watched, and each for a reason:
--   erm_status, erm_synced_at, erm_synced_by, erm_external_id, erm_url
--       the sync stamp itself. Watching these makes every record stale one
--       millisecond after every successful sync.
--   updated_at
--       set by set_updated_at() on every write, so watching it would make every
--       write drift.
--   zoho_url, created_at, id
--       not carried by any pack.

create or replace function public.erm_trainers_detect_drift()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  -- Never synced: there is nothing to diverge from. A trainer edited nine times
  -- before their first push is not stale, they are 'not_pushed'.
  if old.erm_synced_at is null then
    return new;
  end if;

  if new.full_name          is distinct from old.full_name
     or new.pan               is distinct from old.pan
     or new.email             is distinct from old.email
     or new.phone             is distinct from old.phone
     or new.type              is distinct from old.type
     or new.work_order_status is distinct from old.work_order_status
     or new.zoho_id           is distinct from old.zoho_id then
    new.erm_status := 'stale';
  end if;

  return new;
end;
$$;

comment on function public.erm_trainers_detect_drift() is
  'CLAUDE.md §10 drift detection for the trainer pack. Flips erm_status to stale when a WATCHED column changes on a synced trainer. The watch list is the pack''s source columns and is diffed against app/services/erm/fieldpack.py by tests/unit/test_erm_drift.py.';

create trigger erm_trainers_detect_drift
  before update on public.trainers
  for each row execute function public.erm_trainers_detect_drift();

create or replace function public.erm_programs_detect_drift()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if old.erm_synced_at is null then
    return new;
  end if;

  if new.name          is distinct from old.name
     or new.type        is distinct from old.type
     or new.start_date  is distinct from old.start_date
     or new.end_date    is distinct from old.end_date
     or new.college_id  is distinct from old.college_id then
    new.erm_status := 'stale';
  end if;

  return new;
end;
$$;

comment on function public.erm_programs_detect_drift() is
  'CLAUDE.md §10 drift detection for the program pack. Watch list diffed against app/services/erm/fieldpack.py by tests/unit/test_erm_drift.py.';

create trigger erm_programs_detect_drift
  before update on public.programs
  for each row execute function public.erm_programs_detect_drift();

-- --- The requeue --------------------------------------------------------------
-- §10: "flip to erm_stale and requeue". Fires on the TRANSITION into stale, not
-- on the state, so a record that stays stale across ten further edits keeps one
-- card. The `where not exists` is belt to the partial unique index's braces: the
-- index makes a duplicate impossible, this makes it not raise.

create or replace function public.erm_trainers_requeue()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_superseded uuid;
begin
  if new.erm_status <> 'stale' or old.erm_status = 'stale' then
    return null;   -- AFTER trigger: the return value is ignored either way
  end if;

  -- The sync that no longer holds keeps every scrap of its evidence — who
  -- pasted, when, and the exact pack — and gains a reason it stopped holding.
  update public.erm_sync_tasks
     set state        = 'stale',
         stale_at     = now(),
         stale_reason = 'The trainer record changed after this sync (CLAUDE.md §10).'
   where trainer_id = new.id
     and state = 'confirmed'
  returning id into v_superseded;

  insert into public.erm_sync_tasks (subject_kind, trainer_id, state, supersedes_id)
  select 'trainer', new.id, 'queued', v_superseded
   where not exists (
     select 1 from public.erm_sync_tasks
      where trainer_id = new.id and state in ('queued', 'assigned')
   );

  return null;
end;
$$;

comment on function public.erm_trainers_requeue() is
  'CLAUDE.md §10 "and requeue". Fires on the TRANSITION into stale so repeated edits produce one job card, not one per edit. The confirmed task keeps its evidence and is superseded rather than rewritten.';

create trigger erm_trainers_requeue
  after update on public.trainers
  for each row execute function public.erm_trainers_requeue();

create or replace function public.erm_programs_requeue()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_superseded uuid;
begin
  if new.erm_status <> 'stale' or old.erm_status = 'stale' then
    return null;
  end if;

  update public.erm_sync_tasks
     set state        = 'stale',
         stale_at     = now(),
         stale_reason = 'The program record changed after this sync (CLAUDE.md §10).'
   where program_id = new.id
     and state = 'confirmed'
  returning id into v_superseded;

  insert into public.erm_sync_tasks (subject_kind, program_id, state, supersedes_id)
  select 'program', new.id, 'queued', v_superseded
   where not exists (
     select 1 from public.erm_sync_tasks
      where program_id = new.id and state in ('queued', 'assigned')
   );

  return null;
end;
$$;

comment on function public.erm_programs_requeue() is
  'CLAUDE.md §10 "and requeue", program half. Identical shape to erm_trainers_requeue() by design.';

create trigger erm_programs_requeue
  after update on public.programs
  for each row execute function public.erm_programs_requeue();

-- --- Drift that arrives sideways ----------------------------------------------
-- Consequence 3 from the header. These two markers set `erm_status` and stop;
-- the AFTER triggers above see the transition and do the requeue, so there is
-- exactly one requeue site per subject and no chance of the two paths diverging.
--
-- SECURITY DEFINER because they are invoked from triggers on `deployments` and
-- `colleges`, whose writers legitimately have no rights on `trainers` or
-- `programs` — an LDE Executive maintaining a deployment must not need write
-- access to the trainer roster in order for drift to be recorded. Not STABLE:
-- they write.

create or replace function public.erm_mark_trainer_stale(p_trainer_id uuid)
returns void
language sql
security definer
set search_path = ''
as $$
  update public.trainers
     set erm_status = 'stale'
   where id = p_trainer_id
     and erm_synced_at is not null
     and erm_status <> 'stale';
$$;

comment on function public.erm_mark_trainer_stale(uuid) is
  'Mark a synced trainer''s ERM record stale from OUTSIDE the trainers table. Sets the flag only; erm_trainers_requeue() sees the transition and files the job card, so there is one requeue site.';

create or replace function public.erm_mark_program_stale(p_program_id uuid)
returns void
language sql
security definer
set search_path = ''
as $$
  update public.programs
     set erm_status = 'stale'
   where id = p_program_id
     and erm_synced_at is not null
     and erm_status <> 'stale';
$$;

comment on function public.erm_mark_program_stale(uuid) is
  'Mark a synced program''s ERM record stale from OUTSIDE the programs table.';

-- Neither is anything a signed-in user should be able to call directly: they are
-- the internals of a trigger, and a caller who could invoke them could manufacture
-- ERM churn. The triggers below run as their owner and are unaffected.
revoke execute on function public.erm_mark_trainer_stale(uuid) from public, anon, authenticated;
revoke execute on function public.erm_mark_program_stale(uuid) from public, anon, authenticated;

-- DEPLOYMENTS -> the trainer pack's "College Assigned" field.
-- A trainer moved from one campus to another drifts their ERM record without
-- `trainers` being touched at all. This is the case §10's warning is really
-- about: the two systems diverge in a month precisely through the edits nobody
-- thinks of as edits to the record.
create or replace function public.erm_deployments_detect_drift()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  if tg_op = 'DELETE' then
    perform public.erm_mark_trainer_stale(old.trainer_id);
    return old;
  end if;

  if tg_op = 'INSERT' then
    perform public.erm_mark_trainer_stale(new.trainer_id);
    return new;
  end if;

  -- UPDATE. Only a change of trainer or of batch can move the college set; a
  -- tracksheet URL or a travel note cannot, and marking those as drift would
  -- requeue an ERM push every time a trainer uploaded a file.
  if new.trainer_id is distinct from old.trainer_id
     or new.batch_id is distinct from old.batch_id then
    perform public.erm_mark_trainer_stale(new.trainer_id);
    if new.trainer_id is distinct from old.trainer_id then
      perform public.erm_mark_trainer_stale(old.trainer_id);
    end if;
  end if;

  return new;
end;
$$;

comment on function public.erm_deployments_detect_drift() is
  'CLAUDE.md §10 drift for the trainer pack''s derived "College Assigned" field, which is not a column on trainers at all. A campus move drifts the ERM record without trainers being written to.';

create trigger erm_deployments_detect_drift
  after insert or update or delete on public.deployments
  for each row execute function public.erm_deployments_detect_drift();

-- COLLEGES -> both packs carry the college NAME, not its id.
-- Renames are rare and they are exactly the kind of rare that goes unnoticed for
-- a year. Statement-scoped work in a row trigger is acceptable here because a
-- college has tens of programs, not thousands, and a rename is a once-a-year
-- event.
create or replace function public.erm_colleges_detect_drift()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_id uuid;
begin
  if new.name is not distinct from old.name then
    return null;
  end if;

  for v_id in select id from public.programs where college_id = new.id loop
    perform public.erm_mark_program_stale(v_id);
  end loop;

  -- The trainer pack's "College Assigned" is this name too, so every trainer
  -- deployed to this college drifts as well.
  for v_id in
    select distinct d.trainer_id
      from public.deployments d
      join public.batches b   on b.id  = d.batch_id
      join public.programs pr on pr.id = b.program_id
     where pr.college_id = new.id
  loop
    perform public.erm_mark_trainer_stale(v_id);
  end loop;

  return null;
end;
$$;

comment on function public.erm_colleges_detect_drift() is
  'CLAUDE.md §10 drift for the college NAME, which both packs carry verbatim. A rename silently invalidates every ERM record filed under the old one.';

create trigger erm_colleges_detect_drift
  after update on public.colleges
  for each row execute function public.erm_colleges_detect_drift();

-- =============================================================================
-- 5. RLS
-- =============================================================================

alter table public.erm_sync_tasks enable row level security;
alter table public.erm_sync_tasks force  row level security;

-- SELECT / INSERT / UPDATE, no DELETE — 1300's and 1700's reasoning. A confirmed
-- sync task is the evidence that a named person pasted a named set of values on a
-- named day, which is the record §10 asks for; unwanted work is CANCELLED, which
-- leaves a row saying so.
--
-- `authenticated` is named explicitly in the revoke: Supabase's DEFAULT
-- PRIVILEGES on schema `public` grant all four verbs to that role at creation
-- time, so revoking from `public, anon` alone leaves the table wide open. 1300
-- records finding exactly that the hard way.
revoke all on public.erm_sync_tasks from public, anon, authenticated;
grant select, insert, update on public.erm_sync_tasks to authenticated;

-- --- trainer-subject cards -----------------------------------------------------
-- Deliberately the SAME split 0400 applies to `trainers` itself, and for 0400's
-- reason restated: the trainer roster is not college-scoped data, because
-- sourcing and onboarding happen before any deployment exists — a policy that
-- required reach would make it impossible to file the ERM push for the trainer
-- you are about to deploy.
--
-- The predicate is `app_role() in (...)` and NOT `can_see_commercials()`, even
-- though the two name the same two personas today. They mean different things —
-- one is "may see money", the other is "owns the trainer pipeline" — and reusing
-- the money predicate for a non-money purpose is how a wall gets moved by
-- accident when the personas later diverge. 0400 makes this exact point.
create policy erm_sync_tasks_sourcing_all on public.erm_sync_tasks
  for all to authenticated
  using (subject_kind = 'trainer' and public.app_role() in ('senior_manager', 'manager'))
  with check (subject_kind = 'trainer' and public.app_role() in ('senior_manager', 'manager'));

-- An LDE Executive READS the ERM state of trainers on their campus — "has this
-- trainer been pushed to ERM yet, and is it stale" is a real campus question —
-- and does not act on it. Step 13 is onboarding work and onboarding is a
-- Manager's. Select-only, and only for a trainer they reach.
create policy erm_sync_tasks_lde_select_trainer on public.erm_sync_tasks
  for select to authenticated
  using (
    subject_kind = 'trainer'
    and public.is_internal()
    and public.can_reach_trainer(trainer_id)
  );

-- --- program-subject cards -----------------------------------------------------
-- A program IS college-scoped, so the ordinary reach predicate is exactly right
-- and all three internal personas get the same treatment they get on `programs`.
create policy erm_sync_tasks_program_all on public.erm_sync_tasks
  for all to authenticated
  using (subject_kind = 'program' and public.is_internal() and public.can_reach_program(program_id))
  with check (
    subject_kind = 'program' and public.is_internal() and public.can_reach_program(program_id)
  );

-- NO trainer policy and NO college policy.
--
--   TRAINER — 1800 removed the trainer persona from this database entirely:
--     trainers are records, not users. There is nothing to write a policy for,
--     and adding one back here would be a defect rather than a convenience.
--
--   COLLEGE — §4 grants a college "published artifacts only, read-only". An
--     internal job card to retype records into a third-party portal is not a
--     published artifact by any reading.
--
-- Deny by default covers both: with no policy naming them they get zero rows, and
-- R5 requires that be asserted rather than assumed.
-- =============================================================================
