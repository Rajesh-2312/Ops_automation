-- =============================================================================
-- byteXL Ops Intelligence Platform — 1700 — the Comms Service outbound queue
-- =============================================================================
-- CLAUDE.md §8, "Shared services (not agents)":
--
--   "Comms Service — single outbound queue. Channel, recipient, template, and
--    diff-from-template shown at approval."
--
-- One table. Every outbound message — a chase to TA, a college status note, a
-- trainer reminder — is a row in it before it is anything else, and it walks the
-- R4 ladder DRAFT -> PENDING_APPROVAL -> APPROVED -> RELEASED before it is
-- eligible to leave. There is no second path out. That is what "single" means.
--
-- RELEASING IS NOT TRANSMITTING, IN THIS PHASE
-- --------------------------------------------
-- No provider is wired: no SMTP, no Twilio, no SendGrid, no webhook. `released_at`
-- means "an authenticated human said this may go", and `sent_at` — a column this
-- migration deliberately does NOT create — would mean "a provider accepted it".
-- Conflating the two is how a system starts reporting deliveries it never made.
-- When a provider lands, it adds its own columns and its own failure states in a
-- later migration, and it may only read rows in state RELEASED.
--
-- =============================================================================
-- THE ONE PLACE THIS FILE DEPARTS FROM ITS BRIEF, STATED LOUDLY
-- =============================================================================
-- R4's machinery is `public.artifact_versions` (1300), and a comms message ought
-- to be a row in it rather than to carry lifecycle columns of its own. It cannot
-- be, and the obstruction is structural rather than a preference:
--
--   * `artifact_versions.artifact_type` is `public.artifact_type`, a CLOSED
--     three-label enum whose values are table names, and 1300's own header says
--     the closure is the point.
--   * That type mirrors `app.domain.enums.ArtifactType` label for label, and
--     `app/db/models.py` binds the column to that Python enum. Adding
--     'comms_messages' in SQL alone would make every SELECT that returns such a
--     row raise `LookupError` in SQLAlchemy — the mirror is load-bearing, not
--     decorative.
--   * The Python half of that change is `app/domain/enums.py`, which this
--     workstream is not permitted to edit.
--
-- So the lifecycle columns below are `public.artifact_state` — the SAME type,
-- not a parallel vocabulary — under the SAME constraints and the SAME freeze
-- trigger shape as 1300, and the transition GRAMMAR is not restated here at all:
-- it stays in `app.domain.enums.ALLOWED_TRANSITIONS`, reached through
-- `app.services.approval.state_machine.check_transition()`, exactly as 1300
-- refuses to restate it. `app/services/comms/lifecycle.py` is an adapter over
-- that state machine, not a second one.
--
-- WHEN §14 Q3 IS ANSWERED, this is the migration to revisit: add
-- 'comms_messages' to `public.artifact_type` and its mirror in the domain enum,
-- move these eight columns into `artifact_versions`, and leave `comms_messages`
-- holding content only. That is a data migration with an owner, not a quiet
-- refactor, and it should happen in the same change that records who may approve
-- a college-facing message.
--
-- =============================================================================
-- WHY THE DIFF IS STORED AND NOT COMPUTED AT READ TIME
-- =============================================================================
-- §8 requires the diff-from-template to be "shown at approval". The approver is
-- being asked to sign off on what the drafter CHANGED, and that judgement is
-- only meaningful against the template as it read when the draft was made. A
-- diff recomputed at read time against a template edited since would silently
-- restate what the approver was shown — the same failure R4's content hash
-- exists to prevent, arriving through the review surface instead of the payload.
--
-- Hence `template_body` holds the rendered BASELINE (the template with its
-- structured values substituted, and nothing else), `body` holds what is
-- actually queued, and `diff` holds the hunks between them as computed by
-- `app/services/comms/diff.py`. All three are frozen by the content hash at
-- approval, so "the approver saw this diff" is an equality test.
--
-- `template_values` is stored beside them for CLAUDE.md R1: "No agent may assert
-- a fact it did not read from a system of record [...] If a value appears in a
-- generated message, it was passed in as structured input." That column IS the
-- structured input, on the record, next to the sentence it produced.
-- =============================================================================

-- =============================================================================
-- Enums
-- =============================================================================
-- Declared here rather than in 0100 for the reason 1300 gives at length: 0100 is
-- shipped and "never edit a shipped migration" is the stronger rule. Both are
-- `create type` in full, not `alter type ... add value`, and both are used by a
-- table created below in this same file, so the transaction hazard 0100's header
-- warns about does not arise.
--
-- Neither has a counterpart in `app/domain/enums.py` yet — that file is closed to
-- this workstream — so `app/db/models.py` maps them the way it already maps
-- `document_category` and `document_status`: labels spelled out inline, column
-- typed `str`, named constants beside them so no caller writes a bare literal
-- (§11). `app/services/comms/types.py` holds the `StrEnum` pair and its
-- docstring says where they belong.

-- How the message physically leaves, once a provider exists. Three labels, not
-- five: these are the channels §8 and R3 actually name ("send_email",
-- "send_whatsapp", "post_message" / "platform tickets"). An unused label is an
-- invitation to build the integration behind it.
create type public.comms_channel as enum (
  'email',
  'whatsapp',
  'platform_ticket'
);

comment on type public.comms_channel is
  'Outbound channel for a queued message. Mirrors app.services.comms.types.CommsChannel.';

-- WHO is on the other end, as a CLASS rather than as an address. This is the
-- column the autonomy ladder reads: §8 puts internal chase messages within reach
-- of level 4 "after a demonstrated track record", while "nothing touching money,
-- contracts, or a college contact goes past level 3". Those are different
-- ceilings for different recipients, and a system that only stored an email
-- address could not tell them apart.
create type public.comms_recipient_kind as enum (
  'internal_staff',
  'trainer',
  'college'
);

comment on type public.comms_recipient_kind is
  'Class of recipient, which is what sets the CLAUDE.md §8 autonomy ceiling — internal chase and a college contact are not the same risk. Mirrors app.services.comms.types.CommsRecipientKind.';

-- =============================================================================
-- comms_messages
-- =============================================================================

create table public.comms_messages (
  id                uuid primary key default gen_random_uuid(),

  -- SCOPE. Every message in this system is about a program, and that is what
  -- makes one reach predicate — `can_reach_program()` — sufficient for the
  -- policies below, exactly as it was for `can_reach_artifact()` in 1300.
  -- CASCADE: a deleted program's unsent drafts are noise, and anything that was
  -- released is in `audit_events`, which cascades from nothing.
  program_id        uuid not null
                      references public.programs (id) on delete cascade,

  -- --- the four things §8 requires be visible at approval --------------------

  channel           public.comms_channel not null,

  recipient_kind    public.comms_recipient_kind not null,

  -- The address, handle or ticket queue. Free text on purpose: an email address,
  -- an E.164 number and a platform queue name have nothing in common but being
  -- the thing a human must eyeball before approving. Validated per channel in
  -- `app/services/comms/validators.py`, where a rejection can explain itself.
  recipient_ref     text not null,

  -- Display name for the approval screen. Nullable — a platform ticket has no
  -- person on it — and deliberately NOT a foreign key to profiles or trainers:
  -- this is what the message was addressed to at the time, and a later rename
  -- must not restate an approved artifact.
  recipient_name    text,

  -- Which template this was drafted from. A stable text key, not an FK: there is
  -- no template LIBRARY table in this phase, because the review surface needs the
  -- template as it READ at draft time (see the header) and a snapshot column
  -- gives that without a versioned library. When a library lands it can add a
  -- nullable FK beside this key; the key stays, because it is what survives.
  template_key      text not null,

  -- The BASELINE: the template with `template_values` substituted and nothing
  -- else. This is the left-hand side of the diff.
  template_body     text not null,

  -- R1, in a column. The structured facts that were substituted in — every
  -- number, date and count in the baseline came from here, and here came from
  -- SQL. If a figure appears in `body` and not in this object, the drafter
  -- invented it, which is the thing R1 forbids and the thing the approver is
  -- looking for.
  template_values   jsonb not null default '{}'::jsonb,

  -- Subject line, where the channel has one. Nullable for WhatsApp.
  subject           text,

  -- WHAT ACTUALLY GOES OUT. The right-hand side of the diff.
  body              text not null,

  -- The review surface itself, as computed by app/services/comms/diff.py:
  -- {"version": 1, "identical": bool, "lines_added": int, "lines_removed": int,
  --  "hunks": [{"op": ..., "template": [...], "message": [...]}, ...]}
  --
  -- Stored, not derived at read time. See the header: a diff recomputed later
  -- against a changed template is not the diff the approver was shown.
  diff              jsonb not null,

  -- --- the commercials wall (CLAUDE.md R5, §4) --------------------------------
  -- A comms row ABOUT a payout is commercial: "your July invoice of ₹14,035 is
  -- approved" is the payout, restated in a sentence, and an LDE Executive must
  -- get zero rows for it in the DATABASE and not merely in the UI.
  --
  -- A stored boolean rather than a function over `related_artifact_*`, because
  -- unlike `artifact_is_commercial()` in 1300 there is nothing here to inspect:
  -- the sensitivity of a message is a property of its PROSE, and no predicate can
  -- read prose. So the drafter declares it, the CHECK below forces the
  -- declaration to be true whenever the message points at a remuneration
  -- artifact, and `app/services/comms/` defaults it to true for that case rather
  -- than trusting a caller to remember.
  is_commercial     boolean not null default false,

  -- Optional back-reference to the R4 artifact this message is about, so
  -- "which approved sheet did we tell them about" is answerable. No FK — the
  -- target table varies by row, the same polymorphism 1300 has.
  related_artifact_type public.artifact_type,
  related_artifact_id   uuid,

  -- --- the R4 lifecycle -------------------------------------------------------
  -- Same type, same constraints and same freeze semantics as artifact_versions.
  -- See the header for why these columns are here and not there.

  state             public.artifact_state not null default 'DRAFT',

  -- R4: "Approval freezes and hashes the version." Computed by
  -- `app.services.approval.hashing.content_hash` — the SAME function that
  -- freezes a remuneration sheet, not a second implementation — over the
  -- channel, recipient, template, baseline, body and diff.
  content_hash      text,

  -- A draft may have no human author: §8 puts drafting at autonomy level 2 and an
  -- agent is exactly what produces most rows here. Approval and release may not
  -- (R3), which is why those two are constrained below and this one is not.
  created_by        uuid,
  created_at        timestamptz not null default now(),
  submitted_by      uuid,
  submitted_at      timestamptz,
  approved_by       uuid,
  approved_at       timestamptz,
  released_by       uuid,
  released_at       timestamptz,

  -- R4: editing an approved artifact creates a NEW version in DRAFT. A comms
  -- message is superseded rather than edited, and `supersedes_id` chains the two
  -- so the approval history of a re-drafted message is walkable.
  version           integer not null default 1,
  supersedes_id     uuid references public.comms_messages (id) on delete set null,
  superseded_at     timestamptz,

  -- Rejection reason, warning override, release note. The one field that stays
  -- mutable after approval, for 1300's reason: commentary ABOUT the message is
  -- not the message, and forbidding it pushes that text somewhere with no trail.
  notes             text,

  updated_at        timestamptz not null default now(),

  constraint comms_messages_version_ck check (version >= 1),

  constraint comms_messages_recipient_ck check (length(btrim(recipient_ref)) > 0),
  constraint comms_messages_template_key_ck check (length(btrim(template_key)) > 0),
  constraint comms_messages_body_ck check (length(btrim(body)) > 0),

  -- The diff is the review surface. A row without one cannot be approved because
  -- there is nothing to approve AGAINST, so it may not exist in the first place.
  constraint comms_messages_diff_object_ck check (jsonb_typeof(diff) = 'object'),
  constraint comms_messages_values_object_ck check (jsonb_typeof(template_values) = 'object'),

  -- The back-reference is a pair or it is nothing.
  constraint comms_messages_related_pair_ck check (
    (related_artifact_type is null) = (related_artifact_id is null)
  ),

  -- R5, forced rather than trusted: a message about a remuneration sheet is
  -- commercial, whatever the caller declared. The reverse is NOT implied — a
  -- message with no back-reference may still be commercial, because prose about
  -- a rate is commercial with or without a foreign key.
  constraint comms_messages_commercial_implied_ck check (
    related_artifact_type is distinct from 'remuneration_sheets' or is_commercial
  ),

  -- R4, first half — approval freezes AND hashes. Three separate biconditionals
  -- for 1300's reason: the tempting one-liner passes for a DRAFT that carries
  -- `approved_by` alone, and a draft displaying an approver who approved nothing
  -- is precisely the row being forbidden.
  constraint comms_messages_approved_ck check (
    (state in ('APPROVED', 'RELEASED')) = (approved_by  is not null)
    and (state in ('APPROVED', 'RELEASED')) = (approved_at  is not null)
    and (state in ('APPROVED', 'RELEASED')) = (content_hash is not null)
  ),

  -- R4, second half — release is its own act with its own actor and timestamp.
  constraint comms_messages_released_ck check (
    (state = 'RELEASED') = (released_by is not null)
    and (state = 'RELEASED') = (released_at is not null)
  ),

  constraint comms_messages_order_ck check (
    (submitted_at is null or submitted_at >= created_at)
    and (approved_at is null or submitted_at is null or approved_at >= submitted_at)
    and (released_at is null or approved_at is null or released_at >= approved_at)
    and (superseded_at is null or superseded_at >= created_at)
  ),

  constraint comms_messages_supersedes_not_self_ck check (supersedes_id is distinct from id)
);

comment on table public.comms_messages is
  'CLAUDE.md §8 single outbound queue: channel, recipient, template and diff-from-template, on the R4 ladder. RELEASED means an authenticated human cleared it to go — it does NOT mean anything was transmitted; no provider is wired in this phase.';
comment on column public.comms_messages.template_body is
  'The template with template_values substituted and nothing else — the LEFT side of the approval diff. Snapshotted so a later template edit cannot restate what an approver was shown.';
comment on column public.comms_messages.template_values is
  'CLAUDE.md R1: the structured input every fact in the message came from. A figure in `body` that is not in here was invented by the drafter.';
comment on column public.comms_messages.diff is
  'The §8 review surface, computed by app/services/comms/diff.py and frozen with the rest at approval. An approver reads what CHANGED, not the whole message again.';
comment on column public.comms_messages.is_commercial is
  'CLAUDE.md R5. A message ABOUT a payout is the payout restated in prose. Declared by the drafter because no SQL predicate can read prose; forced true for remuneration back-references by comms_messages_commercial_implied_ck.';
comment on column public.comms_messages.released_by is
  'Approval and release are SEPARATE acts with separate actors, timestamps and audit rows (R4). Release marks state; it transmits nothing.';
comment on column public.comms_messages.supersedes_id is
  'R4: an approved message is not edited, it is superseded by a new DRAFT row that points back here.';

-- The queue, read three ways:
--   "what is waiting on me"          -> pending
--   "what has this program sent"     -> program
--   "what is cleared to go out"      -> releasable
create index comms_messages_pending_idx
  on public.comms_messages (submitted_at)
  where state = 'PENDING_APPROVAL';
create index comms_messages_program_idx
  on public.comms_messages (program_id, created_at desc);
create index comms_messages_state_idx on public.comms_messages (state);
create index comms_messages_related_idx
  on public.comms_messages (related_artifact_type, related_artifact_id)
  where related_artifact_id is not null;

-- Exactly one live row per supersession chain link: a message may be superseded
-- once, by one successor. Without this, two concurrent re-drafts both claim the
-- same predecessor and the history forks.
create unique index comms_messages_one_successor
  on public.comms_messages (supersedes_id)
  where supersedes_id is not null;

create trigger comms_messages_set_updated_at
  before update on public.comms_messages
  for each row execute function public.set_updated_at();

-- --- The freeze ---------------------------------------------------------------
-- 1300's `artifact_versions_freeze()`, applied to this table's columns. Not
-- shared with it, because that function reads `old.artifact_type` and friends by
-- name and a generic version would have to be written in dynamic SQL — a trigger
-- that can be got wrong quietly is worse than two triggers that cannot.
--
-- No `auth.uid() is null` short-circuit, for 1300's reason verbatim: the FastAPI
-- service connects with BYPASSRLS and a NULL auth.uid(), so a guard that steps
-- aside for NULL is a guard that never runs on the only connection that writes.
--
-- What stays mutable after approval:
--   state -> RELEASED, released_by, released_at   — release is the next act
--   superseded_at, supersedes_id on the SUCCESSOR — that is how R4 re-drafts
--   notes                                         — commentary, not content
--   updated_at                                    — stamped above

create or replace function public.comms_messages_freeze()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if tg_op = 'DELETE' then
    -- A never-approved draft may be discarded: an agent that drafts badly should
    -- not leave litter that looks like governance history. Anything that reached
    -- approval is the record of a decision.
    if old.approved_at is not null or old.state in ('APPROVED', 'RELEASED') then
      raise exception
        'comms_messages %: an approved or released message cannot be deleted '
        '(CLAUDE.md R4). Supersede it with a new DRAFT instead.', old.id
        using errcode = '42501';
    end if;
    return old;
  end if;

  if old.state not in ('APPROVED', 'RELEASED') then
    return new;   -- still in flight; app/services/comms/lifecycle.py owns it
  end if;

  -- The message, the recipient and the evidence. Everything the approver read.
  if new.program_id      is distinct from old.program_id
     or new.channel        is distinct from old.channel
     or new.recipient_kind is distinct from old.recipient_kind
     or new.recipient_ref  is distinct from old.recipient_ref
     or new.template_key   is distinct from old.template_key
     or new.template_body  is distinct from old.template_body
     or new.subject        is distinct from old.subject
     or new.body           is distinct from old.body
     or new.diff           is distinct from old.diff
     or new.is_commercial  is distinct from old.is_commercial
     or new.version        is distinct from old.version
     or new.created_at     is distinct from old.created_at
     or new.content_hash   is distinct from old.content_hash
     or new.approved_by    is distinct from old.approved_by
     or new.approved_at    is distinct from old.approved_at then
    raise exception
      'comms_messages %: approval freezes the message — recipient, channel, '
      'template, body, diff, hash and approver are immutable once APPROVED '
      '(CLAUDE.md R4). Editing an approved message creates a NEW draft that '
      'supersedes it.', old.id
      using errcode = '42501';
  end if;

  -- No walking backwards. R4 gives one way out of APPROVED and it is forwards.
  if old.state = 'RELEASED' then
    if new.state is distinct from old.state
       or new.released_by is distinct from old.released_by
       or new.released_at is distinct from old.released_at then
      raise exception
        'comms_messages %: RELEASED is terminal (CLAUDE.md R4).', old.id
        using errcode = '42501';
    end if;
  elsif new.state not in ('APPROVED', 'RELEASED') then
    raise exception
      'comms_messages %: an APPROVED message may only move to RELEASED '
      '(CLAUDE.md R4). To revise it, supersede it with a new DRAFT.', old.id
      using errcode = '42501';
  end if;

  return new;
end;
$$;

comment on function public.comms_messages_freeze() is
  'R4 for the outbound queue: blocks mutation of an approved message and its evidence, blocks APPROVED/RELEASED -> DRAFT, blocks deletion of anything that reached approval. No auth.uid() short-circuit — see 1300 header.';

create trigger comms_messages_freeze
  before update or delete on public.comms_messages
  for each row execute function public.comms_messages_freeze();

-- =============================================================================
-- RLS
-- =============================================================================

alter table public.comms_messages enable row level security;
alter table public.comms_messages force  row level security;

-- SELECT / INSERT / UPDATE, no DELETE — `artifact_versions`' reasoning in 1300.
-- The freeze trigger already refuses to delete anything approved; withholding
-- the grant stops an unapproved draft being reaped through PostgREST too, and
-- grants are the only place the four verbs can be told apart, because the `for
-- all` policies below cover DELETE as well.
--
-- `authenticated` is named explicitly in the revoke. Supabase's DEFAULT
-- PRIVILEGES on schema `public` grant all four verbs to that role at creation
-- time, so revoking from `public, anon` alone leaves the table wide open — 1300
-- records finding exactly that the hard way.
revoke all on public.comms_messages from public, anon, authenticated;
grant select, insert, update on public.comms_messages to authenticated;

-- TWO policies, mutually exclusive by construction (one requires
-- `is_commercial`, the other requires `not` of it), so the usual hazard of OR'd
-- permissive policies does not apply: no row satisfies both, and neither can
-- widen the other.
--
-- R5, concretely: an LDE Executive fully assigned to the college fails
-- `can_see_commercials()` in the first policy and `not is_commercial` in the
-- second, so a payout chase message returns ZERO ROWS for them — which is the
-- case worth asserting, because the reach conjunct passes and only the wall is
-- doing the work.
create policy comms_messages_commercials_all on public.comms_messages
  for all to authenticated
  using (
    public.can_see_commercials()
    and is_commercial
    and public.can_reach_program(program_id)
  )
  with check (
    public.can_see_commercials()
    and is_commercial
    and public.can_reach_program(program_id)
  );

create policy comms_messages_internal_all on public.comms_messages
  for all to authenticated
  using (
    public.is_internal()
    and not is_commercial
    and public.can_reach_program(program_id)
  )
  with check (
    public.is_internal()
    and not is_commercial
    and public.can_reach_program(program_id)
  );

-- NO trainer policy and NO college policy, and both are deliberate.
--
--   TRAINER — §4 gives them "own deployment, tracksheet, invoice status".
--     A queue row addressed to them is not a message they have received; it is
--     an internal draft that may still be rejected. Showing a trainer the
--     unapproved text of what someone might send them invites a negotiation with
--     the drafter that the approval gate exists to prevent. They see the message
--     when it is sent, through the channel it is sent on.
--
--   COLLEGE — §4 grants a college "published artifacts only, read-only", and
--     nothing in this table is published until it is released and transmitted.
--     R4 governs everything before that, and none of it is theirs.
--
-- Deny by default covers both: with no policy naming them, both personas get
-- zero rows, and R5 asserts it rather than assuming it.
-- =============================================================================
-- FOLLOW-UP OWNED BY ANOTHER FILE
-- =============================================================================
-- `supabase/tests/00_isolate.sql` clears every application table so that the RLS
-- counts are unscoped. `public.comms_messages` is added to it in this change,
-- before `delete from public.programs` (the FK is ON DELETE CASCADE, so the
-- order is belt and braces rather than load-bearing).
-- =============================================================================
