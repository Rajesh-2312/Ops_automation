# CLAUDE.md — byteXL Ops Intelligence Platform

Operations platform for byteXL's college training programs. Tracks the full
program lifecycle from MoU to trainer payout, with agents that draft and
retrieve — never agents that decide or send.

Owner: Rajesh Maroju. Consumers: OPS Senior Managers, Managers, LDE Executives,
trainers, colleges.

---

## 1. Hard rules — read before writing any code

These are not preferences. Violating any one is a defect, not a style issue.

**R1. The database owns truth. The LLM owns language.**
No agent may assert a fact it did not read from a system of record. Attendance
counts, payment amounts, dates, and syllabus percentages come from SQL queries.
If a value appears in a generated message, it was passed in as structured input,
not produced by the model.

**R2. No money is computed by an LLM.**
All monetary arithmetic lives in `services/remuneration/engine.py`, is pure
Python, uses `Decimal`, and is unit-tested. An agent may explain a number. It
may never produce one.

**R3. Agents have no release capability.**
Agent tool sets contain read and `save_draft` only. There is no `send_email`,
`send_whatsapp`, `post_message`, or `mark_released` tool bound to any agent
graph. Release endpoints require an authenticated human session. This is
enforced by tool binding, not by prompt instruction — never add a send-capable
tool to an agent's toolset "temporarily".

**R4. Nothing leaves the system unapproved.**
Every artifact moves DRAFT → PENDING_APPROVAL → APPROVED → RELEASED. Approval
freezes and hashes the version. Editing an approved artifact creates a new
version in DRAFT requiring fresh approval. Approval and release are separate
actions with separate audit rows.

**R5. Row-level security is tested, not assumed.**
Every persona boundary has a test that asserts a forbidden read returns zero
rows. An LDE Executive must never see commercials. A trainer must never see
another trainer's payout.

**R6. Rounding happens once.**
Full `Decimal` precision through every intermediate. Round only at final
display or at net pay. Never feed a rounded value back into a calculation.

**R7. Never use float for money.** `Decimal` only. No exceptions.

---

## 2. Stack

| Layer | Choice | Notes |
|---|---|---|
| API | FastAPI (Python 3.11+) | async, Pydantic v2 models |
| DB | Postgres via Supabase | RLS enforced at row level |
| Vectors | pgvector, same Postgres | not a separate Chroma instance — permission filtering must live in one place |
| Agents | LangGraph | supervisor + specialists, Postgres checkpointer |
| LLM | OpenRouter, sole gateway. Volume tier (drafting, chase, summaries) · frontier tier (document extraction, governance reports) | route by task, not by default — `app/core/llm.py`, `TASK_TIER` |
| Local inference | Ollama | dev only |
| Frontend | React + Vite + TypeScript | |
| Queue | Celery + Redis | scheduled monitors, retries |
| Files | Supabase Storage | signed URLs, never public buckets |
| Sheets | openpyxl | outputs must match existing column order |

---

## 3. Repo layout

```
app/
  api/              FastAPI routers, one per workstream
  core/             config, security, RLS helpers, audit
  db/               SQLAlchemy models, Alembic migrations
  domain/           pure dataclasses/enums, zero I/O
  services/
    remuneration/   engine.py, validators.py, generators.py
    approval/       state machine, versioning, hashing
    erm/            manual sync task + field-pack generation
    comms/          outbound queue, templates, approval binding
  agents/
    supervisor.py   program orchestrator graph
    intake.py       sourcing.py  onboarding.py  logistics.py
    monitor.py      assessment.py  reporting.py  payout.py
    copilot.py      RAG Q&A
    tools/          tool definitions — READ AND DRAFT ONLY
  rag/              ingestion, chunking, retrieval with persona filter
tests/
  unit/  integration/  rls/  fixtures/
```

`domain/` must import nothing from `db/`, `api/`, or `agents/`. Keep the
business rules testable without a database.

---

## 4. Roles and visibility

```
Senior Manager → Manager → LDE Executive (on campus)
```

| Persona | Scope |
|---|---|
| Senior Manager | All programs in cluster. P&L, escalations, payout approval. |
| Manager | Own colleges. Full program state, trainer costs, reports. |
| LDE Executive | Own college only. Attendance, batches, daily tasks. **No commercials.** |
| College | Published artifacts only, read-only. |

**Trainers are records, not users** (owner's decision, 2026-08-18). An educator
never signs in. Work orders, deployments, attendance and payouts are managed
*for* them by internal staff. `1800_remove_trainer_persona_access.sql` dropped
all eighteen trainer policies, including four writes -- two of which let the
payee mark the attendance that decided their own pay.

The `trainer` label survives in the `app_role` enum, and deleting it would be a
defect rather than a tidy-up. It is the **deny-by-default sentinel**: since
migration 2100, `handle_new_user` assigns it to **every** new signup and reads
no persona from the request at all. Because it carries no policy anywhere, it
grants precisely nothing -- which makes it a better sentinel than when it was a
real persona.

**A signup may not choose its own persona.** 0200 used to do
`coalesce(requested_role, 'trainer')`, reading `raw_user_meta_data ->> 'role'`
-- the client-supplied `data` object of a public `POST /auth/v1/signup`. With
1100 auto-confirming email, one unauthenticated request produced a Manager, and
three policies that carried the commercials wall but no reach conjunct handed
that account 1,026 trainer identities and 1,025 **writable** payment rails.
Reproduced, then closed by 2100; 2200 and 2400 added the missing conjuncts. The
persona now comes only from an admin, through the guarded UPDATE path in 0200,
which is what this section always said. Do not reintroduce a persona field at
signup -- the UI picker is gone for this reason, not by oversight. `profiles.trainer_id` and
`profiles_role_link_ck` remain for the same reason, and `my_trainer_id()` is
kept but has EXECUTE revoked from `authenticated`.

If an educator portal is ever wanted, it is a new build against the existing
`trainers` / `deployments` tables, not a revert.

Internal teams modelled as actors: TA/Sourcing, HR, Finance & Accounts,
Tech/Assessment, Platform, Learning & Science, Central OPS, Placements.

**How scope is bound.** Persona lives on `profiles.role`. Reach does not — it
comes from two assignment tables, so a Manager can cover many colleges and a
Senior Manager a cluster:

- `user_college_assignments` — Manager and LDE Executive to colleges
- `user_cluster_assignments` — Senior Manager to clusters; `colleges.cluster_id`
  expands a cluster to its colleges

`profiles.college_id` is used **only** by the College persona (a college's own
login). Internal staff never take scope from it.

Every policy resolves reach through `SECURITY DEFINER` helpers that read those
tables: `is_internal()`, `is_senior_manager()`, `my_college_ids()`,
`can_reach_college/program/batch/deployment()`. The commercials wall is one
reusable predicate, `can_see_commercials()` — true for Senior Manager and
Manager only. An LDE Executive gets **zero rows** from `pnl`, remuneration,
invoices and work-order rates, in the database rather than in the UI. Per R5,
each of those boundaries has a test asserting exactly that.

---

## 5. Program types — the central branch

Two program types. They differ in rate basis, and that difference propagates
into attendance semantics.

| | CRT | bCAP |
|---|---|---|
| Rate basis | **per day** | **per month**, prorated by calendar days |
| Payable days | counted **UP** from `P` marks | counted **DOWN** from period length |
| Unmarked day | not payable | payable |
| Weekend | not payable (no `P` mark) | payable |
| College holiday | **not payable** | **payable** — retainer absorbs it |
| Half day (`H`) | 0.5 | deducts 0.5 |
| Absent (`A`) | 0 | deducts 1 |

The asymmetry is deliberate but dangerous: an unmarked day silently pays a bCAP
trainer and silently underpays a CRT trainer. Attendance completeness validation
is therefore a **hard block** for CRT and a **warning** for bCAP.

---

## 6. Payout computation — canonical

Implemented in `services/remuneration/engine.py`. Do not reimplement elsewhere.

```
payable_days   per the table above
days_in_month  calendar days

per_month:  rate_per_day = rate / days_in_month        (display only)
            earned       = rate * payable_days / days_in_month
per_day:    rate_per_day = rate
            earned       = rate * payable_days

gross   = earned + ta_da + accommodation + travel_reimb
tds     = earned * tds_rate          # base is EARNED, never gross
net     = gross - deductions - tds
ROUND(net, 0)                        # the only rounding
```

**Multiply before dividing** on the per-month path. Dividing first leaves a
repeating decimal that does not recombine — `65000/31*31` lands on
`64999.99999…`. The legacy spreadsheet carries exactly this artifact.

**TDS excludes reimbursements.** Default rate 0.10. This is why the sample
sheet shows 1,548 and not 1,558.

**Invoice number:** `{PAN[0:4]}/{FY}/{MON}{seq}` — e.g. `BCDP/26-27/JUL1`.
FY runs April–March and derives from the **payout month**. Unique constraint on
`(pan, fiscal_year, month, seq)`.

**Amount in words:** generated in Python, Indian numbering (lakh/crore). The
legacy sheet renders `#NAME?` from a missing Excel macro — do not reproduce.

**Trainer identity is PAN.** It is the only stable key present in every legacy
sheet and it seeds the invoice number. Never match trainers by name string.

**Attendance storage is one row per day**, never a wide D1–D31 layout. Payout
disputes are always about specific days.

### Regression fixtures — must reconcile to the rupee

| Trainer | Terms | Expected |
|---|---|---|
| VEMA PRUDHVI SAI | bCAP, ₹80,000/mo, 26–31 Jul 2026, TA&DA ₹100 | Earned 15,484 · Gross 15,584 · TDS 1,548 · **Net 14,035** |
| Bushily Kondala Rao | bCAP, ₹65,000/mo, full Jul 2026 | Earned 65,000 · TDS 6,500 · **Net 58,500** |

If either breaks, the build is broken. Do not adjust the fixtures to match code.

---

## 7. Validation gates

Blocking — cycle cannot reach PENDING_APPROVAL:

- Signed work order on file, payout period inside `wo_valid_from..wo_valid_to`
- ZOHO account exists
- PAN (10 chars), bank account, IFSC (11 chars) present and well-formed
- `payable_days <= days_in_month`
- Attendance complete for the period (CRT)
- Invoice number not already issued
- Engagement rate matches the rate in the signed WO
- Net pay > 0

Warning — requires a stated reason:

- Net pay deviates >20% from trailing 3-month average
- Reimbursement claimed with zero payable days
- Attendance incomplete (bCAP)

---

## 8. Agent architecture

One supervisor, nine specialists, on LangGraph with a Postgres checkpointer so a
program graph can pause for days awaiting a human.

**Supervisor (Program Orchestrator)** — not a chatbot. Runs hourly and on
events. Per program: what phase, what tasks open, what overdue, what blocked,
who to nudge. Routes to specialists. Never contacts an external party.

| Agent | Owns | Ceiling |
|---|---|---|
| Intake | Parse MoU/PO/mail → structured Program draft, flag unusual clauses | Draft |
| Sourcing Liaison | Requirement spec, TA follow-up, re-spec diffs, profile ranking | Draft |
| Onboarding | WO / ZOHO / ERM / platform-access checklist, internal chase | Auto (internal only) |
| Logistics | Travel need detection, booking request, onward + **return** | Draft |
| Delivery Monitor | Attendance, usage, syllabus anomalies, risk scoring | Alert (internal only) |
| Assessment | Assessment request assembly, Tech-team chase, report package | Draft |
| Reporting | Governance report, feedback synthesis, college summaries | Draft |
| Payout | Explain validation failures, draft variance reasons, run summaries | Draft |
| Ops Copilot | RAG Q&A over SOPs, contracts, college history | Read-only |

### Autonomy ladder

1. Observe — read, report, alert internally
2. Draft — propose, human edits and sends
3. Act-with-approval — execute on one click
4. Act — autonomous, logged

**Nothing touching money, contracts, or a college contact goes past level 3.**
Internal chase messages and platform tickets may reach level 4 only after a
demonstrated track record.

### Shared services (not agents)

- **Comms Service** — single outbound queue. Channel, recipient, template, and
  diff-from-template shown at approval.
- **Escalation Engine** — deterministic SLA rules. Not LLM judgement.
- **Retrieval Service** — shared RAG layer.

---

## 9. RAG rules

Six corpora, separately indexed and permissioned: SOP, Contracts, College
dossier, Educator, Curriculum, Reports.

- Every answer cites source document and section. No citation → no answer.
- Persona filter applies **before** retrieval, not after generation.
- Structured facts (dates, amounts, counts) are **never** retrieved from RAG.
  Query the database. RAG supplies policy and context only. Hybrid answers must
  visibly separate the two.
- Contracts corpus is versioned. A superseded clause must not surface without a
  version flag.

---

## 10. ERM integration — manual by design

ERM is external with no API. Do not build a scraper.

Model as a **sync task with a generated field pack**: the system produces the
exact field-value list in ERM's own field order, assigns it to a named person,
they paste, they confirm. Record carries `erm_synced_at`, `erm_synced_by`.

If the local record changes after sync, flip to `erm_stale` and requeue. Without
drift detection the two systems diverge within a month and neither is trusted.

Same pattern for any integration lacking an API. Never block a feature on API
access that does not exist.

---

## 11. Conventions

- Python: `ruff` + `black`, line length 100. Type hints on all public functions.
- Pydantic v2 for all API boundaries. No raw dicts across a layer boundary.
- Migrations are hand-authored SQL in `supabase/migrations/`, applied in filename
  order. That is the single source of truth for schema. **Not Alembic** — the
  security posture *is* RLS policies, `SECURITY DEFINER` helpers and triggers on
  `auth.users`, none of which Alembic diffs or autogenerates, and two migration
  systems against one database is a defect generator. SQLAlchemy models in `db/`
  are a typed mapping layer that mirrors the schema and never generates it; a
  test diffs them against `information_schema` to catch drift.
  Never edit a shipped migration.
- Enums in `domain/`, never string literals for status values.
- All timestamps UTC in the DB, IST at the presentation layer.
- Currency INR throughout. `Decimal`, two-place storage, whole-rupee display.
- Every state transition writes an `AuditEvent`: actor, action, before, after, at.
- Log agent I/O — prompt, tools called, tokens, latency — for every invocation.
- Sheet outputs preserve legacy column order. People trust the format they read.

## 12. Testing

- Payout engine: unit-tested against both fixtures, to the rupee. Non-negotiable.
- Every validation gate has a passing case and a blocked case.
- RLS: one test per persona boundary asserting zero rows on forbidden reads.
- Approval gate: assert no agent toolset exposes a release-capable tool. This is
  a test, so it fails loudly if someone adds one.
- Agent outputs: assert structure and absence of fabricated figures — compare
  every number in generated text against the structured input.

---

## 13. Build order

Do not skip ahead. Each phase is usable on its own.

| Phase | Scope | Gate to proceed |
|---|---|---|
| 1 | Program tracker. Entities, state machine, task dependency graph, RBAC, persona dashboards, audit log. **Zero AI.** | In daily use by one Manager |
| 2 | Remuneration engine + sheet/invoice generators + approval gate | One month parallel run, zero unexplained variance |
| 3 | Ops Copilot. RAG, read-only, cited | Trusted by Managers for policy lookups |
| 4 | Draft agents: Intake, Sourcing Liaison, Comms queue | Draft acceptance rate acceptable |
| 5 | Delivery Monitor + Escalation Engine. Internal alerts only | |
| 6 | Reporting: governance, feedback synthesis, assessment packages | |
| 7 | Selective promotion to autonomy level 3–4, one agent at a time, with rollback | |

Phase 1 has no AI in it. That is intentional. An agent that cannot reliably
answer "is this trainer's work order signed?" is worse than no agent, because
trust is lost once and not regained.

---

## 14. Open questions

Carry these; do not invent answers.

1. **CRT payout sample** — all validated fixtures are bCAP. Need a real per-day
   sheet: is TA&DA per travel day? Is the day rate in the WO or per program?
2. **`Expense for the month`** — legacy invoice field, `2026-07-26` against a
   01–31 July period. Semantics unknown.
3. Approval authority for college-facing comms: Manager or Senior Manager?
4. Does Finance accept a system-generated remuneration sheet, or must it stay
   manual? Determines whether Phase 2 is automatable end to end.
5. Internal byteXL tool or a product Rajesh owns? Affects hosting and data
   ownership.
6. EdTech platform access — direct DB, API, or neither?
7. Peak concurrency: programs, colleges, trainers.

---

## 15. Reference

**Does not exist — do not go looking.** Earlier drafts of this file cited
`remuneration_engine.py` ("24 passing tests"), `remuneration-module-spec.md` and
`bytexl-ops-agent-platform-design.md`. A full-disk search found none of them.
The canonical engine is `app/services/remuneration/engine.py`, written fresh;
§6 above is its specification.

**Exists, and authoritative:**

- `D:\bytexl_Operations\` — legacy folder structure and sheet formats. Naming
  convention `[College]_[Program]_[Batch]_[YYYY-MM]`. Treat the sheet column
  orders as an output contract.
- `C:\Users\Ramya\Downloads\sample_renumeration_sheet.xlsx` — the remuneration
  sheet's 21 columns in their exact order, plus the VEMA PRUDHVI SAI row behind
  the §6 fixture.
- `C:\Users\Ramya\Downloads\sample_invoice_generation_sheet.xlsx` — invoice
  output shape: `Total Pay`, `Amount in Words`, `account_name`, recipient mail
  fields, and the Bushily Kondala Rao row behind the second §6 fixture.
- `C:\Users\Ramya\Downloads\MALINENI Trainer Invoice Details- Month of JULY
  (Responses).xlsx` — the Google-Form input side: LOP fields, leave dates, and
  the one per-day trainer on record (₹3,500 × 6 days). Partial lead on §14 Q1.

**Prior art, read-only:** `D:\projects\Operational_byteXL\` — the earlier
3-persona build. Its `supabase/migrations/`, `seed.sql` (37 task + 37 document
templates) and `supabase/tests/` RLS harness are the basis of ours. It has no
backend and no git history. Its `.env` holds credentials marked compromised;
this project uses a separate Supabase project and shares nothing with it.