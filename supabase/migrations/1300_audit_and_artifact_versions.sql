-- =============================================================================
-- byteXL Ops Intelligence Platform — 1300 — audit_events, artifact_versions
-- =============================================================================
-- The two tables CLAUDE.md assumes exist and until now did not.
--
--   §11: "Every state transition writes an `AuditEvent`: actor, action, before,
--         after, at."
--   R4:  "Every artifact moves DRAFT -> PENDING_APPROVAL -> APPROVED ->
--         RELEASED. Approval freezes and hashes the version. Editing an approved
--         artifact creates a new version in DRAFT requiring fresh approval.
--         Approval and release are separate actions with separate audit rows."
--
-- `app/core/audit.py` shipped its writer against a table that was a comment. The
-- Pydantic `AuditEvent` there is the column list below, field for field —
-- actor_id, actor_persona, action, entity_table, entity_id, before, after, at.
-- If the two ever diverge the writer fails at INSERT time in production, so
-- `tests/unit/test_audit.py` diffs this file against that model on every run.
--
-- THE ONE RULE THAT SHAPES EVERYTHING HERE
-- ----------------------------------------
-- An audit trail that can be edited is not an audit trail. Neither is one that
-- can be deleted, or one whose author can be forged. Every design decision below
-- that looks paranoid is that sentence applied:
--
--   * no UPDATE policy and no DELETE policy on audit_events, for anyone,
--     including admins;
--   * no UPDATE, DELETE or INSERT *grant* either — a missing grant is checked
--     before RLS is, so the denial is two layers deep;
--   * a trigger that rejects UPDATE, DELETE and TRUNCATE outright, because
--     BYPASSRLS roles (`postgres`, `service_role`) walk past policies and grants
--     and that is exactly the connection our own FastAPI service uses;
--   * no foreign key on `actor_id` (see the column note — every available ON
--     DELETE action is worse than none).
--
-- WHY THE APPEND-ONLY TRIGGER DOES NOT SHORT-CIRCUIT ON `auth.uid() is null`
-- -------------------------------------------------------------------------
-- Technique 3 in 0200's header says every column guard begins with
--     if (select auth.uid()) is null then return new; end if;
-- so migrations and seeds pass through. That escape hatch exists because those
-- guards protect *descriptive* columns from their own owner, and a seed writing
-- a full row is a legitimate writer.
--
-- The guards in this file have no legitimate writer at all. Nothing — not a
-- migration, not a backfill, not the API — has a defensible reason to rewrite a
-- recorded audit event or to un-freeze an approved artifact version. Adding the
-- short-circuit here would mean the guard is inert on precisely the connection
-- every write in this system actually arrives on. So it is omitted, deliberately
-- and against the house style, and this paragraph is why.
--
-- The cost is real and worth naming: a genuine data-correction to an audit row
-- is now impossible without dropping the trigger in a new migration, under
-- review, on the record. That is the intended level of friction.
-- =============================================================================

-- =============================================================================
-- Enums
-- =============================================================================
-- 0100's header states the house rule — every enum in one file — and its reason:
-- Postgres cannot add a value to an enum and then use it in the same
-- transaction, so scattering `create type` makes the next `alter type ... add
-- value` a two-migration dance.
--
-- These two are declared here anyway, because the competing rule is stronger:
-- **never edit a shipped migration** (§11, and supabase/migrations/README.md).
-- 0100 is applied. The reason behind the house rule also does not bite here —
-- both types are `create type` in full, not `alter type ... add value`, and both
-- are used by a table created further down this same file. A future value added
-- to either still costs two migrations, which is the normal price.
--
-- Both mirror `app/domain/enums.py` exactly, including case. ArtifactState is
-- UPPERCASE there and therefore UPPERCASE here — Postgres enum labels are
-- case-sensitive, and a lowercase label would make every insert from the app
-- fail with "invalid input value for enum".

-- CLAUDE.md R4, in order. APPROVED and RELEASED are separate states because
-- approval and release are separate actions with separate audit rows — a single
-- "done" state would make it impossible to answer "who approved this, and did
-- anyone actually send it", which is the only question that matters afterwards.
--
-- Mirrors app.domain.enums.ArtifactState.
create type public.artifact_state as enum (
  'DRAFT',
  'PENDING_APPROVAL',
  'APPROVED',
  'RELEASED'
);

comment on type public.artifact_state is
  'CLAUDE.md R4 lifecycle. Mirrors app.domain.enums.ArtifactState — UPPERCASE labels, deliberately.';

-- What kind of thing is moving through the lifecycle. The values ARE the table
-- names, which is not a shortcut: `app/domain/enums.py` notes that the value
-- doubles as `audit_events.entity_table`, and §11 puts the table name on the
-- audit row "so a row can be found again without guessing which id is meant".
--
-- A closed vocabulary is the point. An artifact type nobody has decided the
-- approval authority for cannot be approved — which is the correct failure, and
-- it is why `APPROVAL_AUTHORITY` in the domain layer is deliberately incomplete
-- while §14 Q3 is open.
--
-- Mirrors app.domain.enums.ArtifactType.
create type public.artifact_type as enum (
  'remuneration_sheets',
  'governance_reports',
  'program_documents'
);

comment on type public.artifact_type is
  'The R4-governed artifact tables. Values are Postgres table names on purpose. Mirrors app.domain.enums.ArtifactType.';

-- =============================================================================
-- audit_events
-- =============================================================================
-- One row per state transition: who, what, to what, from what value, to what
-- value, when. §11 in six columns.
--
-- WHY `action` IS text AND NOT AN ENUM
-- ------------------------------------
-- Every other status-shaped column in this schema is an enum, and §11 says
-- "enums in domain/, never string literals for status values". `action` is the
-- exception on purpose, and `app/core/audit.py` already documents the same
-- decision from the Python side: the vocabulary is open by design so that a
-- workstream can record its own action without editing another team's file or
-- shipping a migration to name it. An enum here would make "add an audit line"
-- a schema change, and the predictable outcome of that is that somebody stops
-- writing the audit line. `AuditAction` in app/core/audit.py names what exists;
-- it is the source of the vocabulary, not a gate on it.
--
-- WHY `before` / `after` ARE jsonb AND NOT STRUCTURED
-- --------------------------------------------------
-- They are a serialised record, not a contract between layers — the one place in
-- this codebase where free-form key-value data is legitimate. They are also the
-- reason the read policies below are as narrow as they are: a snapshot of a
-- `remuneration_sheets` row carries `net_amount`, so this table is a commercials
-- surface whose payload shape no RLS policy can inspect. See the RLS section.

create table public.audit_events (
  id            uuid primary key default gen_random_uuid(),

  -- WHO. NULL only for a scheduled job with no human behind it (the supervisor
  -- graph running hourly, a Celery monitor) — never for an API request.
  --
  -- NO FOREIGN KEY TO profiles, and this is the most deliberate line in the
  -- file. Every available ON DELETE action is worse than none:
  --   CASCADE  — deleting a departed employee's account erases their audit
  --              history. That is the failure this table exists to prevent.
  --   SET NULL — keeps the row, loses the single most important field on it.
  --              "Someone approved a 4.2 lakh payout" is not an audit trail.
  --   RESTRICT — makes offboarding impossible while any audit row survives, so
  --              somebody eventually deletes audit rows to unblock HR. The table
  --              forbids DELETE, so what they would actually do is drop the
  --              trigger.
  -- A bare uuid outlives the account, `actor_persona` below keeps the row
  -- self-describing without a join, and the referential-integrity loss is
  -- nominal: nothing reads this table by joining to a live profile.
  --
  -- It is also what keeps supabase/tests/00_isolate.sql working — that harness
  -- deletes every profile inside its rolled-back transaction, and a RESTRICT
  -- here would abort the whole RLS suite.
  actor_id      uuid,

  -- The actor's persona AS IT WAS. Snapshotted, not joined: a promotion from
  -- LDE Executive to Manager must not retroactively restate who was allowed to
  -- do what last quarter. Same reasoning as remuneration_sheets.invoice_pan.
  actor_persona public.app_role,

  -- WHAT happened. See the note above on why this is text.
  action        text not null,

  -- WHAT IT HAPPENED TO. entity_table is the Postgres table name; entity_id is
  -- the row. No FK on entity_id either — it is polymorphic across every table in
  -- the schema, so there is nothing to point at, and a cascade from any of them
  -- would delete history as a side effect of ordinary cleanup.
  entity_table  text not null,
  entity_id     uuid,

  -- The row as it stood, and as it now stands. Either may be NULL: a creation
  -- has no before, a deletion has no after.
  before        jsonb,
  after         jsonb,

  -- WHEN. Defaulted, but the writer always supplies it, because the moment that
  -- matters is when the transition happened rather than when the row landed —
  -- and `write_within()` in app/core/audit.py can sit in a transaction that
  -- commits later.
  at            timestamptz not null default now(),

  constraint audit_events_action_ck       check (length(btrim(action)) > 0),
  constraint audit_events_entity_table_ck check (length(btrim(entity_table)) > 0)
);

comment on table public.audit_events is
  'CLAUDE.md §11 audit trail: actor, action, before, after, at. APPEND-ONLY — no UPDATE or DELETE policy, no UPDATE/DELETE grant, and a trigger that rejects both outright because BYPASSRLS roles step around the first two.';
comment on column public.audit_events.actor_id is
  'Who acted. NULL only for a scheduled job. Deliberately NOT a foreign key — every ON DELETE action loses either the row or the actor.';
comment on column public.audit_events.actor_persona is
  'The actor''s persona at the time of the action, snapshotted. A later promotion must not restate history.';
comment on column public.audit_events.action is
  'Open vocabulary, text not enum, so recording a new audit line is never a schema change. app.core.audit.AuditAction names what exists.';
comment on column public.audit_events.entity_table is
  'The Postgres table name, so a row can be found again without guessing which "id" is meant (§11).';
comment on column public.audit_events.before is
  'Row snapshot before the transition. Free-form jsonb — a serialised record, not a layer contract. May contain commercials, which is why the read policies below are narrow.';

-- The three questions this table is actually asked, in the order they are asked:
--   "what happened to this row?"      -> entity
--   "what did this person do?"        -> actor
--   "what happened in the last hour?" -> recency
-- `at desc` in each, because every one of them wants the newest first.
create index audit_events_entity_idx on public.audit_events (entity_table, entity_id, at desc);
create index audit_events_actor_idx  on public.audit_events (actor_id, at desc)
  where actor_id is not null;
create index audit_events_at_idx     on public.audit_events (at desc);
create index audit_events_action_idx on public.audit_events (action, at desc);

-- --- The append-only trigger --------------------------------------------------
-- Policies and grants stop `authenticated`. This stops everyone else.
--
-- `app/db/session.py` connects with a BYPASSRLS credential and says so in its
-- own header: "A `select` issued through a session from here sees every row in
-- the database." The same is true of UPDATE and DELETE. So the RLS denial below
-- protects this table from every caller EXCEPT the one that writes to it, which
-- is not a useful place to stop. A trigger is not bypassed by BYPASSRLS.
--
-- TRUNCATE needs its own statement-level trigger: a row-level BEFORE DELETE
-- trigger never fires for TRUNCATE, which is precisely why TRUNCATE is the tool
-- someone reaches for when DELETE is blocked.

create or replace function public.audit_events_append_only()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  raise exception
    'public.audit_events is append-only: % is not permitted (CLAUDE.md §11). '
    'An audit trail that can be edited is not an audit trail. If a row is '
    'genuinely wrong, record a correcting event; do not rewrite history.',
    tg_op
    using errcode = '42501';
  return null;   -- unreachable; plpgsql wants a trigger function to have an exit
end;
$$;

comment on function public.audit_events_append_only() is
  'Rejects UPDATE / DELETE / TRUNCATE on audit_events unconditionally. No auth.uid() short-circuit: BYPASSRLS roles are exactly who this stops.';

create trigger audit_events_no_update
  before update on public.audit_events
  for each row execute function public.audit_events_append_only();

create trigger audit_events_no_delete
  before delete on public.audit_events
  for each row execute function public.audit_events_append_only();

create trigger audit_events_no_truncate
  before truncate on public.audit_events
  for each statement execute function public.audit_events_append_only();

-- =============================================================================
-- artifact_versions
-- =============================================================================
-- The R4 store. Generic over artifact type rather than reimplemented per table,
-- because R4 says "every artifact" and three near-identical approval columns
-- bolted onto three tables is three chances to forget one.
--
-- WHAT THIS TABLE DOES NOT DO
-- ---------------------------
-- It does not hold the artifact. `remuneration_sheets`, `governance_reports` and
-- `program_documents` remain the systems of record for content; this holds the
-- lifecycle and the hash that pins content to an approval.
--
-- It also does not encode the transition table. `ALLOWED_TRANSITIONS` lives in
-- `app/domain/enums.py` and the state machine in `app/services/approval/`;
-- restating either in SQL would be a second implementation of the same spec, in
-- a different language, drifting on its own schedule. 0700 refuses to CHECK the
-- payout formula for exactly this reason. What IS enforced here is the pair of
-- structural claims that a state machine in application code cannot make on its
-- own, because they must survive a bug in that code:
--
--   1. Approval FREEZES. An approved version's identity, hash and approver are
--      immutable, and it cannot be walked backwards into DRAFT. Editing produces
--      a new version — that is R4's sentence, enforced rather than intended.
--   2. Exactly ONE current version per artifact, as a unique index, so two
--      concurrent "create a new draft" calls cannot both succeed.

create table public.artifact_versions (
  id             uuid primary key default gen_random_uuid(),

  -- WHICH ARTIFACT. The pair is the foreign key; there is no real FK because the
  -- target table varies by row. artifact_type names it (and the enum's values are
  -- the table names precisely so this stays mechanical rather than a lookup).
  artifact_type  public.artifact_type not null,
  artifact_id    uuid not null,

  -- 1, 2, 3... per artifact. Assigned by the approval service, not by a
  -- sequence: a sequence is global and would make version numbers jump, which
  -- reads as "versions are missing" to the person in the dispute.
  version        integer not null,

  state          public.artifact_state not null default 'DRAFT',

  -- R4: "Approval freezes and hashes the version." The hash is computed over the
  -- artifact's content by app/services/approval/ and written at the moment of
  -- approval, so "is what I am about to release the thing that was approved" is
  -- an equality test rather than a judgement call. NULL until then — enforced by
  -- artifact_versions_approved_ck below, in both directions.
  content_hash   text,

  -- Who drafted it. Nullable: a version generated by a scheduled run has no
  -- human author. Same no-FK reasoning as audit_events.actor_id — these columns
  -- are historical record, not live references.
  created_by     uuid,
  created_at     timestamptz not null default now(),

  -- Submission is not approval. Kept separate so "sat in the queue for nine
  -- days" is answerable, which is the actual complaint in a slow payout month.
  submitted_by   uuid,
  submitted_at   timestamptz,

  -- APPROVAL and RELEASE, as two distinct acts with two distinct actors and two
  -- distinct timestamps. R4 says they are separate actions with separate audit
  -- rows; collapsing them into one pair of columns here would make the audit
  -- rows describe something the schema cannot represent.
  approved_by    uuid,
  approved_at    timestamptz,
  released_by    uuid,
  released_at    timestamptz,

  -- NULL means "this is the current version". Set when a newer version is
  -- created, which is what R4's "editing an approved artifact creates a new
  -- version in DRAFT" looks like from this side.
  --
  -- A boolean `is_current` would need a trigger to keep exactly one true;
  -- a nullable timestamp gets the same guarantee from a partial unique index
  -- below and answers "when was it superseded" for free.
  superseded_at  timestamptz,

  -- Free text for the human part: a rejection reason, a §7 warning override
  -- ("net deviates 34% — trainer covered two extra batches"), a release note.
  notes          text,

  updated_at     timestamptz not null default now(),

  constraint artifact_versions_version_ck check (version >= 1),

  -- R4, first half: approval freezes AND hashes. Written as three biconditionals
  -- (`=` between two booleans) rather than one, and the difference is not
  -- cosmetic. The tempting one-liner
  --     (state in ('APPROVED','RELEASED')) = (hash is not null and by is not null and at is not null)
  -- passes for a DRAFT carrying `approved_by` alone, because the right-hand side
  -- is then false and both sides agree. A draft displaying an approver who never
  -- approved anything is exactly the row this constraint exists to forbid, so
  -- each column gets its own biconditional and each therefore fails in both
  -- directions: present when APPROVED, absent before it.
  constraint artifact_versions_approved_ck check (
    (state in ('APPROVED', 'RELEASED')) = (approved_by   is not null)
    and (state in ('APPROVED', 'RELEASED')) = (approved_at   is not null)
    and (state in ('APPROVED', 'RELEASED')) = (content_hash  is not null)
  ),

  -- R4, second half: release is its own act with its own actor. Same shape, same
  -- reason — RELEASED demands a releaser, and nothing short of it may have one.
  constraint artifact_versions_released_ck check (
    (state = 'RELEASED') = (released_by is not null)
    and (state = 'RELEASED') = (released_at is not null)
  ),

  -- Time runs forwards through the lifecycle. Cheap, and it catches a backdated
  -- approval written by a fixture or a bad clock before it reaches a report.
  constraint artifact_versions_order_ck check (
    (submitted_at is null or submitted_at >= created_at)
    and (approved_at is null or submitted_at is null or approved_at >= submitted_at)
    and (released_at is null or approved_at is null or released_at >= approved_at)
    and (superseded_at is null or superseded_at >= created_at)
  )
);

comment on table public.artifact_versions is
  'CLAUDE.md R4 lifecycle store: DRAFT -> PENDING_APPROVAL -> APPROVED -> RELEASED, generic over artifact type. Approval freezes the version and its hash; editing an approved artifact creates a NEW version in DRAFT.';
comment on column public.artifact_versions.artifact_type is
  'Names the table artifact_id points into. The enum''s values ARE the table names — see app.domain.enums.ArtifactType.';
comment on column public.artifact_versions.content_hash is
  'Frozen at approval (R4). NULL before it, immutable after it. Lets "is this the thing that was approved" be an equality test.';
comment on column public.artifact_versions.superseded_at is
  'NULL means current. Exactly one current version per artifact, enforced by artifact_versions_one_current below.';
comment on column public.artifact_versions.approved_by is
  'Approval and release are SEPARATE actions with separate actors, timestamps and audit rows (R4). Do not collapse these two pairs.';

-- R4: "Exactly one current version per artifact." A partial unique index, so the
-- superseded history is unbounded while the live row is unique. This is also the
-- concurrency guard: two simultaneous "open a new draft" calls cannot both land.
create unique index artifact_versions_one_current
  on public.artifact_versions (artifact_type, artifact_id)
  where superseded_at is null;

-- Version numbers are per artifact and dense. Nothing may issue v2 twice.
create unique index artifact_versions_unique_version
  on public.artifact_versions (artifact_type, artifact_id, version);

-- The approval queue: "what is waiting on me". Partial, because PENDING_APPROVAL
-- is a handful of rows against a table that only grows.
create index artifact_versions_pending_idx
  on public.artifact_versions (artifact_type, submitted_at)
  where state = 'PENDING_APPROVAL';

create index artifact_versions_state_idx on public.artifact_versions (state);

create trigger artifact_versions_set_updated_at
  before update on public.artifact_versions
  for each row execute function public.set_updated_at();

-- --- The freeze -------------------------------------------------------------
-- R4's "approval freezes and hashes the version", as a BEFORE UPDATE trigger
-- rather than a policy, for the reason 0200 gives in technique 3: RLS WITH CHECK
-- sees only the NEW row and cannot compare against OLD, so "this column was
-- allowed to change until it was approved" is not expressible as a policy.
--
-- No `auth.uid() is null` short-circuit, for the reason in this file's header:
-- the FastAPI service connects with BYPASSRLS and a NULL auth.uid(), so a guard
-- that steps aside for NULL is a guard that never runs.
--
-- What stays mutable after approval, and why:
--   superseded_at — that is how a new version replaces this one (R4).
--   released_by / released_at / state -> RELEASED — release is the next act.
--   notes         — a release note or a dispute annotation is commentary about
--                   the version, not the version. It does not change what was
--                   approved, and forbidding it would push that text somewhere
--                   with no audit trail at all.
--   updated_at    — stamped by the trigger above.

create or replace function public.artifact_versions_freeze()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if tg_op = 'DELETE' then
    -- A never-approved draft may be discarded. Anything that reached approval is
    -- the record of a decision, and deleting it deletes the decision.
    if old.approved_at is not null or old.state in ('APPROVED', 'RELEASED') then
      raise exception
        'artifact_versions %: an approved or released version cannot be deleted '
        '(CLAUDE.md R4). Supersede it with a new version instead.', old.id
        using errcode = '42501';
    end if;
    return old;
  end if;

  if old.state not in ('APPROVED', 'RELEASED') then
    return new;   -- still in flight; the state machine in app/services/approval owns it
  end if;

  -- Identity and the frozen evidence.
  if new.artifact_type is distinct from old.artifact_type
     or new.artifact_id  is distinct from old.artifact_id
     or new.version      is distinct from old.version
     or new.created_at   is distinct from old.created_at
     or new.content_hash is distinct from old.content_hash
     or new.approved_by  is distinct from old.approved_by
     or new.approved_at  is distinct from old.approved_at then
    raise exception
      'artifact_versions %: approval freezes the version — artifact_type, '
      'artifact_id, version, created_at, content_hash, approved_by and '
      'approved_at are immutable once APPROVED (CLAUDE.md R4). Editing an '
      'approved artifact creates a NEW version in DRAFT.', old.id
      using errcode = '42501';
  end if;

  -- No walking backwards. R4 gives exactly one way out of APPROVED, and it is
  -- forwards to RELEASED; a rejection at this point is a new version, not a
  -- reopened one. RELEASED is terminal, and its releaser is frozen too.
  if old.state = 'RELEASED' then
    if new.state is distinct from old.state
       or new.released_by is distinct from old.released_by
       or new.released_at is distinct from old.released_at then
      raise exception
        'artifact_versions %: RELEASED is terminal (CLAUDE.md R4).', old.id
        using errcode = '42501';
    end if;
  elsif new.state not in ('APPROVED', 'RELEASED') then
    raise exception
      'artifact_versions %: an APPROVED version may only move to RELEASED '
      '(CLAUDE.md R4). To revise it, supersede it with a new DRAFT version.',
      old.id
      using errcode = '42501';
  end if;

  return new;
end;
$$;

comment on function public.artifact_versions_freeze() is
  'R4: approval freezes the version. Blocks mutation of the approved evidence, blocks APPROVED/RELEASED -> DRAFT, and blocks deletion of anything that reached approval. No auth.uid() short-circuit — see 1300 header.';

create trigger artifact_versions_freeze
  before update or delete on public.artifact_versions
  for each row execute function public.artifact_versions_freeze();

-- =============================================================================
-- Reach helpers for artifact_versions
-- =============================================================================
-- SECURITY DEFINER + STABLE + `set search_path = ''`, per 0200's technique 1.
-- Definer is load-bearing for a second reason here: these functions read
-- remuneration_sheets, which is behind the commercials wall. Running as the
-- invoker, `can_reach_artifact()` would be filtered by that table's own policy
-- and would return false for an LDE Executive because they cannot SEE the row —
-- which happens to be the right answer for that persona and the wrong mechanism,
-- and would silently invert the moment a policy elsewhere widened.
--
-- The two predicates are kept SEPARATE — one answers "can they reach it", the
-- other "is it commercial" — so every policy below keeps the house shape:
--     using (public.can_see_commercials() and public.can_reach_<scope>(...))
-- Fusing them into one convenient `can_see_artifact()` would hide the wall
-- inside a function body, and 0200's note on why can_see_commercials() is a
-- named predicate applies word for word: the failure is silent, the policy still
-- returns rows, and a smoke test still passes.

-- Pure reach: does the caller's assignment graph cover the program this artifact
-- hangs off? All three artifact types are program-scoped, which is what makes
-- one function possible.
--
-- Returns FALSE for an artifact_id that points at nothing — a deleted target, a
-- typo, a row from a dropped fixture. Deny by default; a version whose artifact
-- has vanished is visible to nobody through RLS.
create or replace function public.can_reach_artifact(
  p_artifact_type public.artifact_type,
  p_artifact_id   uuid
)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select case p_artifact_type
    when 'remuneration_sheets' then exists (
      select 1 from public.remuneration_sheets rs
      where rs.id = p_artifact_id and public.can_reach_program(rs.program_id)
    )
    when 'governance_reports' then exists (
      select 1 from public.governance_reports gr
      where gr.id = p_artifact_id and public.can_reach_program(gr.program_id)
    )
    when 'program_documents' then exists (
      select 1 from public.program_documents pd
      where pd.id = p_artifact_id and public.can_reach_program(pd.program_id)
    )
  end;
$$;

comment on function public.can_reach_artifact(public.artifact_type, uuid) is
  'Scope half of the artifact_versions policies: reach to the program the artifact hangs off. Says NOTHING about the commercials wall — that is can_see_commercials(), kept separate on purpose.';

-- Wall half: is this artifact one of the money surfaces §4 walls off from an LDE
-- Executive?
--
--   remuneration_sheets — always. It is the payout.
--   governance_reports  — never. 0700 is explicit that a governance report is
--                         delivery reporting and not a commercial table; it
--                         lives in that file because its publish gate has the
--                         same shape as the wall, not because it is behind it.
--   program_documents   — by CATEGORY, mirroring 1000 exactly: `remuneration`
--                         and `invoice_generation` are walled, everything else
--                         is operational. A filed remuneration row is not an
--                         amount, but it is a live pointer to one.
--
-- An unresolvable artifact_id returns TRUE (fail closed): if we cannot tell what
-- something is, it is treated as commercial, so the narrower policy applies. It
-- will still be invisible, because can_reach_artifact() returns false for the
-- same row — but the two failure directions should not have to agree for the
-- result to be safe.
create or replace function public.artifact_is_commercial(
  p_artifact_type public.artifact_type,
  p_artifact_id   uuid
)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select case p_artifact_type
    when 'remuneration_sheets' then true
    when 'governance_reports'  then false
    when 'program_documents'   then coalesce(
      (
        select pd.category in ('remuneration', 'invoice_generation')
        from public.program_documents pd
        where pd.id = p_artifact_id
      ),
      true
    )
  end;
$$;

comment on function public.artifact_is_commercial(public.artifact_type, uuid) is
  'True when an artifact_versions row describes a money artifact (CLAUDE.md §4). Mirrors the category split in 1000_scheduling_documents.sql. Fails closed on an unresolvable id.';

revoke execute on function public.can_reach_artifact(public.artifact_type, uuid)
  from public, anon;
revoke execute on function public.artifact_is_commercial(public.artifact_type, uuid)
  from public, anon;

grant execute on function public.can_reach_artifact(public.artifact_type, uuid)
  to authenticated;
grant execute on function public.artifact_is_commercial(public.artifact_type, uuid)
  to authenticated;

-- =============================================================================
-- RLS
-- =============================================================================

alter table public.audit_events      enable row level security;
alter table public.audit_events      force  row level security;
alter table public.artifact_versions enable row level security;
alter table public.artifact_versions force  row level security;

-- --- audit_events: grants ----------------------------------------------------
-- SELECT and nothing else. Not "select, insert, update, delete" like every other
-- table in this schema, and the difference is the whole point:
--
--   no UPDATE / DELETE — append-only, per this file's header.
--   no INSERT          — audit rows are written by `app/core/audit.py` over the
--                        service-role connection, which bypasses RLS anyway. A
--                        PostgREST client that could INSERT here could forge
--                        history, which is exactly as damaging as erasing it,
--                        and a forged row is harder to notice. There is no use
--                        case that needs it: an audit event is a side effect of
--                        a state transition, and the transition already goes
--                        through the API.
--
-- THE `revoke all` MUST NAME `authenticated`, and it is the line most likely to
-- be "tidied" by someone matching the style of 0500/0800, which revoke only from
-- `public, anon`. Supabase ships DEFAULT PRIVILEGES on schema `public` that
-- grant every table privilege to `authenticated` at creation time, so a table
-- arrives pre-granted SELECT, INSERT, UPDATE **and DELETE**. Revoking from
-- `public, anon` alone leaves all four in place, and the narrowing below becomes
-- decorative — verified the hard way: before this line named `authenticated`, an
-- LDE Executive could DELETE artifact_versions rows through PostgREST.
--
-- `service_role` is untouched by this, which is correct: it is the FastAPI path.

revoke all on public.audit_events from public, anon, authenticated;
grant select on public.audit_events to authenticated;

-- --- audit_events: read policies ---------------------------------------------
-- NARROW, and narrower than it first looks reasonable to be. The reasoning:
--
-- `before` and `after` are free-form jsonb. A row describing an approval on a
-- remuneration sheet contains `net_amount`; one describing a P&L edit contains
-- revenue. There is NO predicate a policy can write that inspects a jsonb
-- payload of unknown shape and decides whether it crossed the §4 commercials
-- wall. Routing on `entity_table` was considered and rejected: it is free text,
-- so the resolver would need a branch per table, and the failure mode of a
-- missing branch is a leak that nothing detects — a new table ships, its audit
-- rows fall through to the permissive default, and every campus executive reads
-- them.
--
-- So the honest answer is that this table is not safely readable through
-- PostgREST by a scoped persona, and the policies say that rather than
-- pretending otherwise. Two ways in:
--
--   1. ADMIN. A Senior Manager flagged is_admin — the governance role — reads
--      everything. §4 puts escalations and payout approval in that column, and
--      an admin already reaches every identity and template surface.
--   2. YOURSELF. Anyone reads the rows they themselves wrote: "what did I
--      change last Tuesday" is a question people genuinely ask and the answer
--      leaks nothing new. That holds because an actor can only have triggered an
--      action over data they were permitted to touch — the wall is enforced on
--      every write path, so an LDE Executive's own audit rows cannot contain
--      commercials. If that invariant is ever broken, this policy breaks with
--      it; it is stated here so the next person notices.
--
-- Anything richer — "show this program's history to its Manager" — belongs in a
-- curated view that knows the entity and can therefore apply the wall, the same
-- pattern as the three college-facing views in 0800. Deliberately not built
-- speculatively.
--
-- NOTE these are permissive policies and therefore OR'd. That is intended and
-- safe here: both branches are strictly narrower than the union, and neither can
-- widen the other, because "rows I wrote" and "all rows" do not interact.

create policy audit_events_admin_select on public.audit_events
  for select to authenticated
  using (public.is_admin());

create policy audit_events_self_select on public.audit_events
  for select to authenticated
  using (actor_id = (select auth.uid()));

-- NO insert, update or delete policy on this table, for anyone. Not an
-- oversight — see the grants note above and the header. If you are here to add
-- one, the answer is a new migration with an argument in it, not a patch.

-- --- artifact_versions: grants and policies ----------------------------------
-- SELECT / INSERT / UPDATE, but no DELETE. A lifecycle row is the evidence that
-- something was approved; the trigger above already refuses to delete anything
-- that reached approval, and withholding the grant means an unapproved draft
-- cannot be quietly reaped through PostgREST either. Superseding is an UPDATE.
--
-- Note that withholding DELETE has to be done with the GRANT, not the policy:
-- the `for all` policies below apply to DELETE as well, so a policy that
-- authorises a Manager to read and update these rows authorises them to delete
-- them too. Grants are the only place the four verbs can be told apart here.
-- See the note above `revoke all on public.audit_events` for why `authenticated`
-- is named explicitly.

revoke all on public.artifact_versions from public, anon, authenticated;
grant select, insert, update on public.artifact_versions to authenticated;

-- TWO policies, mirroring the program_documents split in 1000, because the wall
-- here is a property of the ROW rather than of the TABLE: an artifact_versions
-- row for a remuneration sheet is commercial and one for a governance report is
-- not, and they sit side by side.
--
-- The two are mutually exclusive by construction — one requires
-- artifact_is_commercial(), the other requires `not` of it — so the usual danger
-- of OR'd permissive policies does not apply: neither can widen the other,
-- because no row satisfies both.
--
-- CLAUDE.md §4 and R5, concretely: for a `remuneration_sheets` artifact an LDE
-- Executive fails `can_see_commercials()` in the first policy and
-- `not artifact_is_commercial(...)` in the second. ZERO ROWS, in the database,
-- even for a program they fully reach — which is the case worth testing, since
-- the scope conjunct passes and only the wall is doing the work.

create policy artifact_versions_commercials_all on public.artifact_versions
  for all to authenticated
  using (
    public.can_see_commercials()
    and public.artifact_is_commercial(artifact_type, artifact_id)
    and public.can_reach_artifact(artifact_type, artifact_id)
  )
  with check (
    public.can_see_commercials()
    and public.artifact_is_commercial(artifact_type, artifact_id)
    and public.can_reach_artifact(artifact_type, artifact_id)
  );

create policy artifact_versions_internal_all on public.artifact_versions
  for all to authenticated
  using (
    public.is_internal()
    and not public.artifact_is_commercial(artifact_type, artifact_id)
    and public.can_reach_artifact(artifact_type, artifact_id)
  )
  with check (
    public.is_internal()
    and not public.artifact_is_commercial(artifact_type, artifact_id)
    and public.can_reach_artifact(artifact_type, artifact_id)
  );

-- NO trainer policy and NO college policy. Deny by default, and both are
-- deliberate:
--
--   TRAINER — §4 gives them "own deployment, tracksheet, invoice status". The
--     status of their own payout is `remuneration_sheets.payout_status`, which
--     they already read through remuneration_sheets_trainer_select_own (0700).
--     The APPROVAL state is internal governance — who approved it, who held it,
--     what the pending queue looks like — and handing that over invites a
--     conversation with the approver that the approval gate exists to prevent.
--     Adding it later is one policy; taking it back is a renegotiation.
--
--   COLLEGE — §4 grants the college published artifacts only, read-only. A
--     college learns that a governance report exists when it is shared, via
--     governance_reports.shared_with_college_at (0700) and the curated view in
--     0800. R4 is about what happens BEFORE that, and none of it is theirs.

-- =============================================================================
-- FOLLOW-UPS OWNED BY OTHER FILES — recorded here so they are not lost
-- =============================================================================
-- Two files this migration cannot touch need a line each. Both are inert today
-- and both become wrong the moment these tables carry real rows.
--
-- 1. supabase/tests/00_isolate.sql — its header says "23 tables exist in public.
--    21 are cleared below [...] If a migration adds a table and it is not added
--    here, its rows survive into the run and some unscoped count goes wrong in a
--    way that looks like an RLS failure." It is now 25. Add, before the
--    `delete from public.profiles` line and in this order:
--
--        delete from public.artifact_versions;
--        delete from public.audit_events;
--
--    NOTE the second one will FAIL against the append-only trigger, which is
--    correct and is the point. The harness needs `alter table public.audit_events
--    disable trigger audit_events_no_delete;` around it — inside the transaction
--    that is rolled back unconditionally, so the trigger is restored either way.
--    That is the only sanctioned way to remove an audit row, and it is visible.
--
-- 2. supabase/tests/02_rls_matrix_test.sql — R5 requires a persona test per
--    boundary. The assertions this migration is asking for, ready to paste into
--    the LDE EXECUTIVE A1 section after the existing WALL block (they need one
--    artifact_versions row per artifact type in 01_fixtures.sql):
--
--        select test.assert_count('WALL: LDE-A1 sees NO remuneration artifact versions',
--          'select count(*) from public.artifact_versions
--             where artifact_type = ''remuneration_sheets''', 0);
--        select test.assert_count('WALL: LDE-A1 sees NO audit events but their own',
--          'select count(*) from public.audit_events
--             where actor_id is distinct from ''f0000000-0000-0000-0000-000000000004''', 0);
--        select test.assert_count('LDE-A1 DOES see governance artifact versions',
--          'select count(*) from public.artifact_versions
--             where artifact_type = ''governance_reports''', 1);
--
--    and, in the ADMIN section, that an admin with zero reach still reads the
--    audit trail (it is a governance surface, not a scoped one) while still
--    seeing no artifact_versions, because artifact reach is reach:
--
--        select test.assert_count('ADM reads the audit trail',
--          'select count(*) from public.audit_events', <fixture count>);
--        select test.assert_count('ADM reaches no artifact versions',
--          'select count(*) from public.artifact_versions', 0);
-- =============================================================================
