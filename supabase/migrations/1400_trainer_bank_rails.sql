-- =============================================================================
-- byteXL Ops Intelligence Platform — 1400 — trainer bank rails
-- =============================================================================
-- The columns §7 has been demanding since the validators shipped: a bank
-- account number, an IFSC, the bank and branch that own them, and the
-- beneficiary name printed on the invoice sheet.
--
-- Until this migration, `gate_bank_account` and `gate_ifsc` fired BLOCKING on
-- every payout in the system because there was nowhere for the answer to live.
-- `can_submit` could never be true, so no cycle could reach PENDING_APPROVAL and
-- Phase 2's parallel run could not start.
--
-- WHY THIS IS A TABLE AND NOT FIVE COLUMNS ON `trainers`
-- -----------------------------------------------------
-- This is the whole design decision, and it is forced by an existing policy.
--
-- 0400 grants an LDE Executive SELECT on the trainer roster:
--
--     create policy trainers_lde_select_deployed on public.trainers
--       for select to authenticated
--       using (public.is_internal() and public.can_reach_trainer(id));
--
-- That policy is correct and stays. §4 gives the campus executive attendance,
-- batches and daily tasks, all of which need the trainer's name and phone
-- number. But RLS is ROW-level: a policy that returns the row returns EVERY
-- column of it. Putting an account number on `trainers` would hand it to that
-- persona on the same SELECT, and §4's "No commercials" line would be enforced
-- nowhere. Column-level GRANTs cannot rescue it either — every persona logs in
-- as the one `authenticated` role, so a grant that hides a column from an LDE
-- Executive hides it from the Manager who has to type it in.
--
-- 0400's own header already worked this out for a different number:
--
--     "If the engagement rate lived on `deployments`, the only way to keep it
--      from an LDE would be to deny them the whole table, and the campus
--      dashboard would go dark. Rates therefore live on `work_orders` in 0700,
--      behind can_see_commercials()."
--
-- A bank account is that argument again, verbatim, with a different number. So
-- the rails go in their own table behind the same predicate, and `trainers`
-- stays exactly as commercially inert as its header claims.
--
-- THE PREDICATE IS can_see_commercials(), DELIBERATELY
-- ---------------------------------------------------
-- 0400 is careful to gate the roster on `app_role() in ('senior_manager',
-- 'manager')` rather than on can_see_commercials(), because "owns the trainer
-- pipeline" and "may see money" are different ideas that happen to name the same
-- two personas today.
--
-- This table is on the other side of that distinction. A payment instruction is
-- money — it is the last field on the remuneration sheet and the thing an
-- approved payout is executed against — so it takes the money predicate, and it
-- moves automatically if the wall ever moves.
--
-- NO REACH CONJUNCT, AND WHY THAT IS NOT AN OVERSIGHT
-- --------------------------------------------------
-- Every money table in 0700 is `can_see_commercials() and can_reach_<scope>()`.
-- This one is the wall alone, matching `trainers_sourcing_all` rather than
-- `work_orders`, because rails are collected during onboarding — before any
-- deployment exists, which is to say before `can_reach_trainer()` can be true of
-- anybody. A reach-gated policy could not file the first account number.
--
-- The cost is stated rather than hidden, and it is the same shape as finding F1
-- in supabase/tests/02_rls_matrix_test.sql: a Manager reads the rails of a
-- trainer deployed only at colleges they do not cover. That is a roster-wide
-- read of a payment rail by a persona already trusted with the whole roster and
-- with every signed work order in storage. If F1 is ever fixed with a
-- "reachable or undeployed" predicate, this policy takes the same fix.
--
-- WHY A TRAINER READS THEIR OWN RAILS AND MAY NOT WRITE THEM
-- ---------------------------------------------------------
-- Read: §4 gives a trainer their "own deployment, tracksheet, invoice status",
-- and being able to check the account their money is about to land in is the
-- cheapest possible way to catch a transposed digit before Finance releases.
-- Denying it would make the trainer phone their Manager to have it read aloud,
-- which is worse for the data and worse for the secret.
--
-- Write: no. A payee who can silently repoint their own payment rail between
-- approval and release defeats R4 — the approved artifact would name an account
-- that no longer exists on the row. Changing a rail stays an internal act by a
-- persona that passes the wall, so it happens under a session that writes an
-- audit row naming who did it (§11). This is a deliberate friction, not a gap:
-- the trainer supplies the number, a human enters it, and both are on record.
-- =============================================================================

create table public.trainer_bank_accounts (
  -- The trainer IS the key. One set of rails per trainer, so a payout can never
  -- be ambiguous about which account it is instructing, and the 1:1 makes
  -- "rails on file?" a single existence test rather than a max(created_at) race.
  -- A trainer who changes bank updates this row; the previous value is in the
  -- audit trail, which is where a superseded payment instruction belongs.
  --
  -- CASCADE rather than RESTRICT: `deployments` and `work_orders` already hold
  -- trainers down with RESTRICT, so a trainer with any history cannot be deleted
  -- anyway. If one with no history is removed, orphaned rails are pure liability.
  trainer_id           uuid primary key
                         references public.trainers (id) on delete cascade,

  -- §7: "bank account [...] present and well-formed". Digits only and non-empty
  -- is the honest limit of what SQL can assert — `validators.py` says why:
  -- "Indian account numbers run roughly 9-18 digits and vary by bank, so any
  -- length assertion invented here would block valid trainers." A length rule
  -- guessed in a CHECK constraint is one that takes a migration to unguess.
  bank_account_number  text not null,

  -- §7: "IFSC (11 chars)". Length and case are checked here, following the PAN
  -- precedent in 0400 exactly: the cheap absolute test that catches a truncated
  -- paste at write time lives in the database, and the full statutory shape
  -- (AAAA0XXXXXX — four letters, the reserved zero, six alphanumerics) stays in
  -- `gate_ifsc`, "where a rejection can carry an explanation a human can act on".
  --
  -- Uppercase is normalised for the same reason PAN is: the RBI directory is
  -- uppercase, and a lowercase rail that is byte-unequal to the same rail
  -- entered elsewhere turns a reconciliation into a puzzle.
  ifsc                 text not null,

  -- Descriptive, and nullable because they are recoverable from the IFSC. They
  -- are stored anyway because both legacy sheets print them (`Name of the Bank`,
  -- `Branch`) and §11 makes the sheet an output contract.
  bank_name            text,
  branch               text,

  -- `account_name` on the invoice sheet: the beneficiary name as the bank holds
  -- it, which is not always the trainer's display name — a joint account, an
  -- initial expanded, a married name not yet changed at the branch. A payment
  -- rejected on a name mismatch is a fortnight of delay, so it is its own column
  -- and never defaulted from `trainers.full_name`.
  account_name         text,

  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now(),

  constraint trainer_bank_accounts_account_digits_ck
    check (bank_account_number ~ '^[0-9]+$'),

  constraint trainer_bank_accounts_ifsc_length_ck
    check (char_length(ifsc) = 11),
  constraint trainer_bank_accounts_ifsc_upper_ck
    check (ifsc = upper(ifsc))
);

comment on table public.trainer_bank_accounts is
  'Payment rails for a trainer, 1:1. Separate from `trainers` because RLS is row-level and 0400 gives an LDE Executive the trainer row (CLAUDE.md §4, R5). Behind can_see_commercials().';
comment on column public.trainer_bank_accounts.bank_account_number is
  'Digits only. No length rule — Indian account numbers vary by bank; §7 gate_bank_account is the full check.';
comment on column public.trainer_bank_accounts.ifsc is
  'Uppercase, 11 characters. The RBI shape AAAA0XXXXXX is asserted by §7 gate_ifsc, not here — same split as trainers.pan in 0400.';
comment on column public.trainer_bank_accounts.account_name is
  'Beneficiary name as the BANK holds it. Never defaulted from trainers.full_name — a name mismatch bounces the payment.';

create trigger trainer_bank_accounts_set_updated_at
  before update on public.trainer_bank_accounts
  for each row execute function public.set_updated_at();

-- =============================================================================
-- RLS
-- =============================================================================

alter table public.trainer_bank_accounts enable row level security;
alter table public.trainer_bank_accounts force  row level security;

-- SELECT, INSERT and UPDATE, but no DELETE — the same reasoning as
-- `artifact_versions` in 1300. Rails that vanish take the evidence of what an
-- approved payout was executed against with them, and correcting a wrong number
-- is an UPDATE. Withholding the grant is what stops the `for all` policy below
-- authorising a delete: grants are the only place the four verbs can be told
-- apart here.
--
-- `authenticated` is named explicitly in the revoke so a future
-- `grant ... to public` cannot quietly reopen it.
revoke all on public.trainer_bank_accounts from public, anon, authenticated;
grant select, insert, update on public.trainer_bank_accounts to authenticated;

-- The wall. `for all` covers SELECT, INSERT and UPDATE (DELETE is ungranted
-- above), so a Manager or Senior Manager both reads and maintains rails.
-- An LDE Executive fails the predicate and gets ZERO ROWS — including for the
-- one trainer whose roster row they legitimately read, which is the case the
-- persona matrix asserts, because that is where only the wall is doing the work.
create policy trainer_bank_accounts_commercials_all on public.trainer_bank_accounts
  for all to authenticated
  using (public.can_see_commercials())
  with check (public.can_see_commercials());

-- The payee reads their own rails. SELECT only — see the header on why there is
-- deliberately no matching UPDATE policy.
create policy trainer_bank_accounts_trainer_select_own on public.trainer_bank_accounts
  for select to authenticated
  using (trainer_id = public.my_trainer_id());

-- No college policy. §4 gives a college published artifacts only, and a
-- trainer's account number is not one.
