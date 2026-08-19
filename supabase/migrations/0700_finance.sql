-- =============================================================================
-- byteXL Ops Intelligence Platform — 0700 — work_orders, remuneration_sheets,
--                                           governance_reports, pnl
-- =============================================================================
-- THE COMMERCIALS WALL. CLAUDE.md §4 and R5:
--
--   "An LDE Executive gets ZERO ROWS from pnl, remuneration, invoices and
--    work-order rates, in the database rather than in the UI. Per R5, each of
--    those boundaries has a test asserting exactly that."
--
-- Every SELECT policy on a money table in this file has the same two-conjunct
-- shape, and both halves are load-bearing:
--
--     using (public.can_see_commercials() and public.can_reach_<scope>(id))
--            ^ the wall: senior_manager | manager only
--                                          ^ the scope: their colleges only
--
-- Drop the first conjunct and every campus executive sees every trainer's rate.
-- Drop the second and a manager sees another cluster's P&L. Neither failure
-- raises an error; both just return rows.
--
-- WHAT IS DELIBERATELY NOT HERE
-- -----------------------------
-- There is no admin override on any policy in this file. `is_admin()` grants the
-- right to hand out reach; it is not itself reach. A Senior Manager with no
-- cluster assignment sees no P&L, and that is correct — §4 scopes them to "all
-- programs in cluster", not to all programs.
--
-- WHY NO MONEY IS COMPUTED HERE
-- -----------------------------
-- R2: no monetary arithmetic in SQL. There is not a single generated column, sum
-- or trigger-computed amount below. Every figure is written by
-- app/services/remuneration/engine.py, which is pure Python, uses Decimal, and
-- is unit-tested against the two §6 regression fixtures. A CHECK constraint
-- asserting `gross = earned + ta_da + ...` looks like a safety net and is
-- actually a second implementation of the spec, in a language with different
-- rounding — it would start rejecting correct rows the first time the engine's
-- Decimal precision and Postgres numeric disagreed at the last paisa.
-- =============================================================================

-- --- work_orders -------------------------------------------------------------
-- The signed contract behind a deployment, and the home of the engagement RATE.
--
-- The rate lives here rather than on `deployments` (0400) for one reason: an LDE
-- Executive must be able to read deployments — attendance, tracksheets and the
-- campus dashboard all walk through them — and must not be able to read rates.
-- A commercial column on an operational table forces a choice between those two,
-- and either answer is wrong. Splitting the table costs one join and buys a
-- clean wall.
--
-- Three §7 validation gates read this table: "signed work order on file",
-- "payout period inside wo_valid_from..wo_valid_to", and "engagement rate
-- matches the rate in the signed WO".

create table public.work_orders (
  id             uuid primary key default gen_random_uuid(),
  trainer_id     uuid not null references public.trainers (id) on delete restrict,
  program_id     uuid not null references public.programs (id) on delete restrict,
  rate           numeric(14,2) not null,
  rate_basis     public.rate_basis not null,
  valid_from     date not null,
  valid_to       date not null,
  status         public.doc_status not null default 'not_started',
  signed_at      timestamptz,
  -- The document itself lives at documents/trainers/<trainer_id>/ in Storage
  -- (0900). We hold the pointer and the terms.
  document_url   text,
  notes          text,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now(),

  constraint work_orders_validity_ck check (valid_to >= valid_from),
  constraint work_orders_rate_ck     check (rate > 0)
);

comment on table public.work_orders is
  'Signed engagement terms for a trainer on a program. COMMERCIAL — never visible to an LDE Executive (CLAUDE.md §4).';
comment on column public.work_orders.rate_basis is
  'per_day (CRT) or per_month (bCAP). Decides which branch of the §6 payout formula applies — multiply-before-divide on the per_month path.';
comment on column public.work_orders.valid_to is
  'The §7 gate: a payout period must fall entirely inside valid_from..valid_to.';

create index work_orders_trainer_idx on public.work_orders (trainer_id);
create index work_orders_program_idx on public.work_orders (program_id);
create unique index work_orders_unique_engagement
  on public.work_orders (trainer_id, program_id, valid_from);

create trigger work_orders_set_updated_at
  before update on public.work_orders
  for each row execute function public.set_updated_at();

-- --- remuneration_sheets -----------------------------------------------------
-- One computed payout for one trainer, one program, one period. The columns
-- mirror §6 line for line, in the order the formula runs, because a payout that
-- cannot be re-derived from its own stored row cannot be defended in a dispute —
-- and "the engine said so" is not a defence when the engine has been redeployed
-- twice since.
--
-- All money columns are NULLABLE. A sheet exists from the moment a period opens;
-- the figures arrive when the engine runs. NOT NULL here would force the API to
-- invent zeroes before the computation, and a zero is indistinguishable from a
-- genuine nil payout.

create table public.remuneration_sheets (
  id              uuid primary key default gen_random_uuid(),
  trainer_id      uuid not null references public.trainers (id) on delete restrict,
  program_id      uuid not null references public.programs (id) on delete restrict,
  work_order_id   uuid references public.work_orders (id) on delete set null,
  period_start    date not null,
  period_end      date not null,

  -- Inputs, snapshotted. The rate is copied from the work order at computation
  -- time rather than joined at read time: a work order gets re-signed at a new
  -- rate, and last month's sheet must keep saying what it paid.
  rate            numeric(14,2),
  rate_basis      public.rate_basis,
  -- numeric, not integer: a half day is 0.5 (§5).
  payable_days    numeric(6,2),
  days_in_month   integer,

  -- §6, in order.
  earned          numeric(14,2),
  ta_da           numeric(14,2),
  accommodation   numeric(14,2),
  travel_reimb    numeric(14,2),
  gross           numeric(14,2),
  -- TDS base is EARNED, never gross — reimbursements are not income. This is
  -- why the §6 fixture shows 1,548 and not 1,558.
  tds_rate        numeric(5,4),
  tds             numeric(14,2),
  deductions      numeric(14,2),
  -- The only rounded figure in the row (R6: round once, at net).
  net_amount      numeric(14,2),
  amount_in_words text,

  currency        text not null default 'INR',
  payout_status   public.doc_status not null default 'not_started',
  paid_on         date,

  -- Invoice identity (§6: {PAN[0:4]}/{FY}/{MON}{seq}, e.g. BCDP/26-27/JUL1).
  -- invoice_pan is a SNAPSHOT of trainers.pan, not a join. An invoice number is
  -- frozen at issue: if a PAN is later corrected the issued number must not
  -- change, and the uniqueness claim must still hold against the value that was
  -- actually printed.
  invoice_pan     text,
  invoice_fy      text,
  invoice_month   text,
  invoice_seq     integer,
  invoice_no      text,
  invoice_issued_at timestamptz,

  -- Payout links in the finance system.
  source_system   text,
  external_id     text,
  external_url    text,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),

  constraint remuneration_sheets_period_ck check (period_end >= period_start),
  constraint remuneration_sheets_net_ck    check (net_amount is null or net_amount >= 0),
  constraint remuneration_sheets_days_ck   check (
    payable_days is null or days_in_month is null or payable_days <= days_in_month
  )
);

comment on table public.remuneration_sheets is
  'A computed payout for one trainer, one program, one period. COMMERCIAL, plus the owning trainer. Written only by app/services/remuneration/engine.py (R2).';
comment on column public.remuneration_sheets.payable_days is
  'CRT counts UP from P marks; bCAP counts DOWN from the period length. Same column, opposite derivation — see app.domain.attendance.';
comment on column public.remuneration_sheets.tds_rate is
  'Default 0.10, applied to `earned`. Stored per sheet so a rate change does not retroactively restate a paid month.';
comment on column public.remuneration_sheets.invoice_no is
  'PAN[0:4]/FY/MONseq. FY runs April-March and derives from the PAYOUT month, not the period start.';

create index remuneration_sheets_trainer_id_idx on public.remuneration_sheets (trainer_id);
create index remuneration_sheets_program_id_idx on public.remuneration_sheets (program_id);
create unique index remuneration_sheets_unique_period
  on public.remuneration_sheets (trainer_id, program_id, period_start, period_end);

-- §6: "Unique constraint on (pan, fiscal_year, month, seq)". Partial, because
-- the components are NULL until an invoice is actually issued and NULLs would
-- otherwise make the constraint vacuous for every draft.
create unique index remuneration_sheets_invoice_identity_key
  on public.remuneration_sheets (invoice_pan, invoice_fy, invoice_month, invoice_seq)
  where invoice_no is not null;

-- The §7 gate "invoice number not already issued", enforced rather than checked.
create unique index remuneration_sheets_invoice_no_key
  on public.remuneration_sheets (invoice_no) where invoice_no is not null;

create trigger remuneration_sheets_set_updated_at
  before update on public.remuneration_sheets
  for each row execute function public.set_updated_at();

-- --- governance_reports ------------------------------------------------------
-- Not a commercial table: a governance report is delivery reporting, and §4 puts
-- reports in the Manager's column and published artifacts in the college's. It
-- lives in this file because the publish gate is the same shape as the money
-- wall, not because it is behind it.

create table public.governance_reports (
  id                     uuid primary key default gen_random_uuid(),
  program_id             uuid not null references public.programs (id) on delete cascade,
  title                  text,
  url                    text not null,
  reporting_period_start date,
  reporting_period_end   date,
  -- The college sees a report only once it has been deliberately shared. NULL
  -- means drafted-but-not-shared and must stay invisible to the college — R4:
  -- nothing leaves the system unapproved.
  shared_with_college_at timestamptz,
  shared_by              uuid references public.profiles (id) on delete set null,
  created_at             timestamptz not null default now(),
  updated_at             timestamptz not null default now()
);

comment on table public.governance_reports is
  'Governance report for a program. Visible to the college only after shared_with_college_at is set.';
comment on column public.governance_reports.shared_with_college_at is
  'The publish gate. Until this is set the report is internal.';

create index governance_reports_program_id_idx on public.governance_reports (program_id);
create index governance_reports_shared_idx
  on public.governance_reports (program_id) where shared_with_college_at is not null;

create trigger governance_reports_set_updated_at
  before update on public.governance_reports
  for each row execute function public.set_updated_at();

-- --- pnl ---------------------------------------------------------------------

create table public.pnl (
  id                uuid primary key default gen_random_uuid(),
  program_id        uuid not null references public.programs (id) on delete cascade,
  revenue           numeric(14,2),
  trainer_cost      numeric(14,2),
  travel_cost       numeric(14,2),
  platform_cost     numeric(14,2),
  other_cost        numeric(14,2),
  accrued_amount    numeric(14,2),
  invoiced_amount   numeric(14,2),
  currency          text not null default 'INR',
  notes             text,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now()
);

comment on table public.pnl is
  'Program profit & loss. Senior Manager and Manager only, and only for programs they reach. Never a trainer, never a college, never an LDE Executive.';

create unique index pnl_program_id_key on public.pnl (program_id);

create trigger pnl_set_updated_at
  before update on public.pnl
  for each row execute function public.set_updated_at();

-- =============================================================================
-- RLS — the commercials wall
-- =============================================================================

alter table public.work_orders         enable row level security;
alter table public.work_orders         force  row level security;
alter table public.remuneration_sheets enable row level security;
alter table public.remuneration_sheets force  row level security;
alter table public.governance_reports  enable row level security;
alter table public.governance_reports  force  row level security;
alter table public.pnl                 enable row level security;
alter table public.pnl                 force  row level security;

grant select, insert, update, delete on public.work_orders         to authenticated;
grant select, insert, update, delete on public.remuneration_sheets to authenticated;
grant select, insert, update, delete on public.governance_reports  to authenticated;
grant select, insert, update, delete on public.pnl                 to authenticated;

-- --- work_orders -------------------------------------------------------------

create policy work_orders_commercials_all on public.work_orders
  for all to authenticated
  using (public.can_see_commercials() and public.can_reach_program(program_id))
  with check (public.can_see_commercials() and public.can_reach_program(program_id));

-- A trainer sees their OWN work order, rate included. The wall is about other
-- people's money: a contractor is entitled to the terms they signed, and
-- withholding them would make the §7 gate "engagement rate matches the WO"
-- unarguable in the one direction that matters. Read only — no trainer writes a
-- number that determines their own payout.
create policy work_orders_trainer_select_own on public.work_orders
  for select to authenticated
  using (trainer_id = public.my_trainer_id());

-- --- remuneration_sheets -----------------------------------------------------

create policy remuneration_sheets_commercials_all on public.remuneration_sheets
  for all to authenticated
  using (public.can_see_commercials() and public.can_reach_program(program_id))
  with check (public.can_see_commercials() and public.can_reach_program(program_id));

-- §4: a trainer sees their own payout and no one else's. R5 pins this with a
-- test: "a trainer must never see another trainer's payout."
create policy remuneration_sheets_trainer_select_own on public.remuneration_sheets
  for select to authenticated
  using (trainer_id = public.my_trainer_id());

-- --- governance_reports ------------------------------------------------------
-- All internal personas, scoped by program reach. An LDE Executive contributes
-- to the report for their own campus; nothing in it is a rate.

create policy governance_reports_internal_all on public.governance_reports
  for all to authenticated
  using (public.is_internal() and public.can_reach_program(program_id))
  with check (public.is_internal() and public.can_reach_program(program_id));

-- College: own programs, and only once explicitly shared. Both conditions.
create policy governance_reports_college_select_shared on public.governance_reports
  for select to authenticated
  using (
    shared_with_college_at is not null
    and public.college_owns_program(program_id)
  );

-- No trainer policy: §4 gives trainers nothing on governance reports.

-- --- pnl ---------------------------------------------------------------------
-- ONE policy, deliberately. Permissive policies are OR'd together, so any second
-- policy on this table can only widen access, never narrow it. If a future
-- requirement seems to need one, it needs a different table.

create policy pnl_commercials_all on public.pnl
  for all to authenticated
  using (public.can_see_commercials() and public.can_reach_program(program_id))
  with check (public.can_see_commercials() and public.can_reach_program(program_id));
