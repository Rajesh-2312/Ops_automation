-- =============================================================================
-- byteXL Ops Intelligence Platform — seed
-- =============================================================================
-- Two libraries, both derived from how byteXL operations actually run:
--
--   1. task_templates      — the stage checklists (37 rows)
--   2. document_templates  — the master document library (37 rows), mirroring
--                            the 00_Templates_and_Masters folder and the
--                            per-stage folders it feeds
--
-- Both are idempotent and self-correcting: re-running updates wording, owner,
-- order, cadence and schedule on existing rows rather than skipping or
-- duplicating them. That makes this file the single place to evolve the
-- operating model — edit here, re-run, and every future program picks it up.
--
-- Run AFTER every migration in supabase/migrations/, in filename order. It
-- writes to task_templates (0500, columns added in 1000) and document_templates
-- (1000). Both tables have FORCE ROW LEVEL SECURITY, so this file must run as a
-- BYPASSRLS role — the migration runner, `supabase db reset`, or a service-role
-- session. It is not runnable as `authenticated`, by design.
-- =============================================================================


-- =============================================================================
-- 1. TASK TEMPLATES
-- =============================================================================
-- `default_owner_role` implements the responsibility matrix: owners are a ROLE
-- plus a specific owner_id at assignment time, never a hardcoded name.
--
-- THE ROLE MAPPING, AND THE RULE BEHIND IT
-- ----------------------------------------
-- These templates were written against three personas, where every internal
-- item said 'ops'. Five personas mean the checklist now has to say WHICH kind of
-- internal. The rule applied throughout, from CLAUDE.md §4:
--
--   senior_manager  P&L, accruals, invoicing to the college, payout release.
--                   Anything where the org is deciding what money moves.
--   manager         Contracting, sourcing, trainer onboarding, governance
--                   reporting, remuneration preparation. Owns the program.
--   lde_executive   Campus execution — batch data, credentials, tracksheets,
--                   observation, the introduction call. Never an amount.
--   trainer         Only what the trainer themselves submits or records.
--   college         Never. The college persona is read-only (§4) and owns no
--                   checklist item; a task assigned to it could never be marked
--                   done, because no college policy grants UPDATE anywhere.
--
-- The split between manager and senior_manager on the finance stage is the one
-- worth arguing with: "prepare" and "validate" are Manager work because §4 gives
-- the Manager trainer costs, while "release", "accrue" and "invoice" are Senior
-- Manager work because §4 gives them payout approval and P&L. If OPS disagrees,
-- change it here and re-run — nothing else has to move.
--
-- `cadence` separates periodic work from one-off work. Most of the pipeline
-- happens once per program; monitoring and finance items repeat for its whole
-- life. Cadence is a label only — nothing generates repeat instances.
--
-- `offset_days` / `offset_anchor` are what turn a checklist into a SCHEDULE.
-- Negative offsets fall before the anchor date, positive after. Generation
-- stamps a real due_date on every task, which is the only reason the urgency
-- bands mean anything — nobody sets 37 due dates a program by hand.
--
-- The numbers below encode a working lead-time model: contracting starts ~45
-- days out, sourcing ~28, onboarding ~15, deployment in the final week, and
-- closeout keys off the END date rather than the start.

insert into public.task_templates
  (stage, title, description, default_owner_role, order_index, cadence,
   offset_days, offset_anchor)
values

-- --- Stage 1 — acquisition_setup --------------------------------------------
-- Contracting and planning. Everything here must land before sourcing starts.
  ('acquisition_setup', 'Collect signed MOU from college',
   'Obtain the signed MOU and upload to documents/colleges/<college_id>/.', 'manager', 10, 'one_time', -45, 'program_start'),
  ('acquisition_setup', 'Obtain purchase order (PO)',
   'Confirm PO issued by the college and record po_status.', 'manager', 20, 'one_time', -35, 'program_start'),
  ('acquisition_setup', 'Capture batch and branch details',
   'Create batches with branch, section, and expected student count.', 'lde_executive', 30, 'one_time', -32, 'program_start'),
  ('acquisition_setup', 'Confirm program type (bCAP / CRT)',
   'Set programs.type — it drives rate basis, attendance semantics, and which checklist items apply (§5).', 'manager', 40, 'one_time', -32, 'program_start'),
  ('acquisition_setup', 'Confirm program start and end dates',
   'Lock the delivery window and attach the timetable link.', 'manager', 50, 'one_time', -30, 'program_start'),
  ('acquisition_setup', 'Prepare P&L for the program',
   'Draft revenue and cost lines. Senior Manager / Manager only — an LDE Executive gets zero rows from pnl (§4).', 'senior_manager', 60, 'one_time', -28, 'program_start'),

-- --- Stage 2 — trainer_sourcing ---------------------------------------------
-- Roughly four weeks out: this is the long pole, and slipping it slips delivery.
  ('trainer_sourcing', 'Raise TA (trainer allocation) request',
   'Submit the trainer allocation request for the confirmed batches.', 'manager', 10, 'one_time', -28, 'program_start'),
  ('trainer_sourcing', 'Raise TOC request to L&S',
   'Request the table of contents / curriculum scope from Learning & Science.', 'manager', 20, 'one_time', -26, 'program_start'),
  ('trainer_sourcing', 'Push requirement to ERM',
   'Generate the ERM field pack, assign it to a named person to paste, and record erm_status (§10 — ERM has no API).', 'manager', 30, 'one_time', -23, 'program_start'),
  ('trainer_sourcing', 'Receive and record trainer details',
   'Capture returned trainer profiles against the trainers table. PAN is the identity key — never match by name (§6).', 'manager', 40, 'one_time', -19, 'program_start'),

-- --- Stage 3 — trainer_onboarding -------------------------------------------
-- Contracting the trainer. The signed work order gates everything downstream:
-- three §7 validation gates read it before a payout can be approved.
  ('trainer_onboarding', 'Raise work order with HR',
   'Initiate the work order (and extension, where applicable) with HR. Record rate and validity on work_orders.', 'manager', 10, 'one_time', -16, 'program_start'),
  ('trainer_onboarding', 'Collect signed work order',
   'File the signed work order under documents/trainers/<trainer_id>/ and set status to signed.', 'manager', 20, 'one_time', -11, 'program_start'),
  ('trainer_onboarding', 'Create ZOHO account for trainer',
   'Record zoho_id and zoho_url. Never copy ZOHO field data — ZOHO stays the system of record.', 'manager', 30, 'one_time', -9, 'program_start'),
  ('trainer_onboarding', 'Update trainer record in ERM',
   'Sync onboarding state back to ERM, then stamp erm_synced_at / erm_synced_by (§10).', 'manager', 40, 'one_time', -8, 'program_start'),
  ('trainer_onboarding', 'Share TOC and delivery instructions with trainer',
   'Send curriculum scope and delivery expectations to the assigned trainer.', 'manager', 50, 'one_time', -6, 'program_start'),

-- --- Stage 4 — deployment ----------------------------------------------------
-- The final week. Everything converges on day zero, and it happens on campus.
  ('deployment', 'Generate student platform credentials',
   'Create credentials and track students.credentials_status through to activated.', 'lde_executive', 10, 'one_time', -6, 'program_start'),
  ('deployment', 'Confirm trainer travel and accommodation',
   'Trainer submits travel details against their deployment.', 'trainer', 20, 'one_time', -4, 'program_start'),
  ('deployment', 'Create tracksheet and assign educator',
   'Create the deployment row linking trainer to batch and attach tracksheet_url.', 'lde_executive', 30, 'one_time', -4, 'program_start'),
  ('deployment', 'Share tracksheet link with trainer',
   'Trainer confirms access to the tracksheet for their batch.', 'trainer', 40, 'one_time', -2, 'program_start'),
  ('deployment', 'Run introduction call with college',
   'Introduce the assigned trainer to the college point of contact.', 'lde_executive', 50, 'one_time', -1, 'program_start'),
  ('deployment', 'Confirm platform access and student onboarding',
   'Verify students can log in and complete platform onboarding.', 'lde_executive', 60, 'one_time', 0, 'program_start'),

-- --- Stage 5 — active_monitoring --------------------------------------------
-- The recurring heart of a live program. Offsets mark the FIRST occurrence;
-- cadence describes the rhythm from there.
  ('active_monitoring', 'Mark daily attendance',
   'Trainer records one trainer_attendance row per day against their deployment. Completeness is a hard block for CRT and a warning for bCAP (§5).', 'trainer', 10, 'daily', 1, 'program_start'),
  ('active_monitoring', 'Track platform usage',
   'Monitor LMS usage. LMS remains system of record — link, never duplicate.', 'lde_executive', 20, 'weekly', 7, 'program_start'),
  ('active_monitoring', 'Track syllabus completion',
   'Update syllabus progress against the shared TOC.', 'trainer', 30, 'weekly', 7, 'program_start'),
  ('active_monitoring', 'Schedule classroom observation',
   'Plan the observation window for each active deployment.', 'lde_executive', 40, 'monthly', 14, 'program_start'),
  ('active_monitoring', 'Record classroom observation',
   'Capture observation notes against the deployment.', 'lde_executive', 50, 'monthly', 21, 'program_start'),
  ('active_monitoring', 'Prepare governance report',
   'Draft the periodic governance report for the program.', 'manager', 60, 'monthly', 30, 'program_start'),
  ('active_monitoring', 'Share governance report with college',
   'Set shared_with_college_at — this is the gate that makes it visible to the college.', 'manager', 70, 'monthly', 33, 'program_start'),
  ('active_monitoring', 'Initiate student feedback collection',
   'Launch platform or Google Form feedback and record the link.', 'lde_executive', 80, 'quarterly', 45, 'program_start'),
  ('active_monitoring', 'Review feedback results',
   'Review response counts and headline scores; escalate outliers.', 'manager', 90, 'quarterly', 52, 'program_start'),
  ('active_monitoring', 'Conduct assessment and publish report',
   'Run the assessment, then record clickup_url and report_url.', 'manager', 100, 'quarterly', 60, 'program_start'),

-- --- Stage 6 — closeout_finance ---------------------------------------------
-- Anchored to the END date: none of this is meaningful relative to the start.
-- Every item here is commercial, so no template is owned by an LDE Executive.
  ('closeout_finance', 'Prepare trainer remuneration sheets',
   'Run the payout engine per trainer per period. No amount is ever computed by hand or by an LLM (R2).', 'manager', 10, 'monthly', 3, 'program_end'),
  ('closeout_finance', 'Validate remuneration against attendance and tracksheet',
   'Work the §7 gates: signed WO on file, period inside validity, attendance complete (CRT), payable_days <= days_in_month, net > 0.', 'manager', 20, 'monthly', 5, 'program_end'),
  ('closeout_finance', 'Record accruals for the period',
   'Update pnl.accrued_amount for unbilled delivery.', 'senior_manager', 30, 'monthly', 7, 'program_end'),
  ('closeout_finance', 'Process trainer payouts',
   'Approve and release payment, then record payout_status and paid_on. Release is a human action with its own audit row (R3, R4).', 'senior_manager', 40, 'monthly', 10, 'program_end'),
  ('closeout_finance', 'Raise monthly invoice to college',
   'Issue the invoice and record invoiced_amount.', 'senior_manager', 50, 'monthly', 12, 'program_end'),
  ('closeout_finance', 'Close out program P&L',
   'Finalise revenue and cost lines; confirm the program can be closed.', 'senior_manager', 60, 'one_time', 21, 'program_end')

on conflict (stage, lower(title)) do update set
  description        = excluded.description,
  default_owner_role = excluded.default_owner_role,
  order_index        = excluded.order_index,
  cadence            = excluded.cadence,
  offset_days        = excluded.offset_days,
  offset_anchor      = excluded.offset_anchor;


-- =============================================================================
-- 2. DOCUMENT TEMPLATES
-- =============================================================================
-- The master library, mirroring the byteXL Drive. Each row is a template that
-- lives in 00_Templates_and_Masters and gets copied into the stage folder named
-- by its category when a program needs one.
--
-- ⚠️ NAMES AND URLS ARE PLACEHOLDERS. They match the intent of each folder, not
-- the real filenames. Replace `name` with the actual template names and set
-- `master_url` to the Drive link for each — then re-run this file. Nothing else
-- has to change: the categories, stages and ordering are already correct.
--
-- master_url stays a LINK. Drive remains the system of record for the documents
-- themselves; we hold the pointer and the state.
--
-- Note the two commercial categories at the bottom (remuneration,
-- invoice_generation). The TEMPLATES are visible to all internal staff — a blank
-- form names no amount — but the per-program INSTANCES filed under those
-- categories are walled to Senior Manager and Manager by the RLS policies in
-- 1000_scheduling_documents.sql.

insert into public.document_templates
  (category, stage, name, description, is_required, order_index)
values

-- --- 01_College_Onboarding ---------------------------------------------------
  ('college_onboarding', 'acquisition_setup', 'MOU template',
   'Master memorandum of understanding issued to the college.', true, 10),
  ('college_onboarding', 'acquisition_setup', 'Purchase order tracker',
   'PO reference, value and validity against the engagement.', true, 20),
  ('college_onboarding', 'acquisition_setup', 'College contact sheet',
   'Named points of contact: principal, TPO, department heads.', true, 30),
  ('college_onboarding', 'acquisition_setup', 'College onboarding checklist',
   'Sign-off that the engagement is contractually ready to plan.', true, 40),

-- --- 02_Program_Planning -----------------------------------------------------
  ('program_planning', 'acquisition_setup', 'Program plan',
   'Scope, duration, delivery model and success measures.', true, 10),
  ('program_planning', 'acquisition_setup', 'Batch and branch master',
   'Branch, section and expected headcount per batch.', true, 20),
  ('program_planning', 'acquisition_setup', 'Timetable template',
   'Session grid agreed with the college.', true, 30),
  ('program_planning', 'acquisition_setup', 'P&L template',
   'Revenue and cost model for the program. Commercial — Senior Manager / Manager only (§4).', true, 40),

-- --- 03_Trainer_Recruitment --------------------------------------------------
  ('trainer_recruitment', 'trainer_sourcing', 'TA request form',
   'Trainer allocation request raised against confirmed batches.', true, 10),
  ('trainer_recruitment', 'trainer_sourcing', 'TOC request to L&S',
   'Curriculum scope request to Learning & Science.', true, 20),
  ('trainer_recruitment', 'trainer_sourcing', 'ERM push tracker',
   'State of the resourcing requirement in ERM. Link only — ERM owns the data and has no API (§10).', true, 30),
  ('trainer_recruitment', 'trainer_sourcing', 'Trainer profile sheet',
   'Profiles returned against the request, for shortlisting.', true, 40),

-- --- 04_Trainer_Onboarding ---------------------------------------------------
  ('trainer_onboarding', 'trainer_onboarding', 'Work order template',
   'Master work order issued through HR. The signed copy states the rate and is therefore commercial.', true, 10),
  ('trainer_onboarding', 'trainer_onboarding', 'Work order extension template',
   'Used when delivery runs past the original window. A payout period must sit inside the WO validity (§7).', false, 20),
  ('trainer_onboarding', 'trainer_onboarding', 'ZOHO account request',
   'Account creation request. Record the ZOHO id, never copy its fields.', true, 30),
  ('trainer_onboarding', 'trainer_onboarding', 'ERM update log',
   'Onboarding state synced back to ERM, with who pasted it and when.', true, 40),
  ('trainer_onboarding', 'trainer_onboarding', 'TOC and delivery instructions pack',
   'What the trainer receives before day one.', true, 50),

-- --- 05_Student_Credentials --------------------------------------------------
  ('student_credentials', 'deployment', 'Credential generation sheet',
   'Student list prepared for platform account creation.', true, 10),
  ('student_credentials', 'deployment', 'Credential handover tracker',
   'Issued, shared and activated state per batch.', true, 20),

-- --- 06_Trainer_Deployment ---------------------------------------------------
  ('trainer_deployment', 'deployment', 'Tracksheet template',
   'Session-by-session delivery record for a batch.', true, 10),
  ('trainer_deployment', 'deployment', 'Educator assignment sheet',
   'Which trainer takes which batch, and from when.', true, 20),
  ('trainer_deployment', 'deployment', 'Travel and accommodation form',
   'Submitted by the trainer against their deployment. Feeds TA&DA and travel reimbursement, which are excluded from the TDS base (§6).', true, 30),
  ('trainer_deployment', 'deployment', 'College introduction call agenda',
   'Structure for the trainer-to-college handover call.', false, 40),

-- --- 07_Monitoring_and_Quality -----------------------------------------------
  ('monitoring_and_quality', 'active_monitoring', 'Classroom observation form',
   'Structured observation of a live session.', true, 10),
  ('monitoring_and_quality', 'active_monitoring', 'Platform usage tracker',
   'LMS engagement per batch. LMS is the system of record.', true, 20),
  ('monitoring_and_quality', 'active_monitoring', 'Syllabus completion tracker',
   'Progress against the shared TOC.', true, 30),

-- --- 08_Feedback_and_Assessments ---------------------------------------------
  ('feedback_and_assessments', 'active_monitoring', 'Student feedback form',
   'Platform or Google Form instrument. Responses stay in the source system.', true, 10),
  ('feedback_and_assessments', 'active_monitoring', 'Feedback summary',
   'Response counts and headline scores, with escalations.', true, 20),
  ('feedback_and_assessments', 'active_monitoring', 'Assessment plan',
   'What is assessed, when, and how it is marked.', true, 30),
  ('feedback_and_assessments', 'active_monitoring', 'Assessment report',
   'Outcome pack. ClickUp owns the work item; link to it.', true, 40),

-- --- 09_Governance_Reports ---------------------------------------------------
  ('governance_reports', 'active_monitoring', 'Governance report template',
   'Periodic report shared with the college once published (R4 — approval before release).', true, 10),
  ('governance_reports', 'active_monitoring', 'Governance review deck',
   'Slides for the governance meeting.', false, 20),

-- --- 10_Remuneration ---------------------------------------------------------
  ('remuneration', 'closeout_finance', 'Remuneration sheet template',
   'Per-trainer amounts per period, in the legacy 21-column order. Commercial, plus the owning trainer (§4).', true, 10),
  ('remuneration', 'closeout_finance', 'Payout tracker',
   'Release state and payment reference. Commercial.', true, 20),
  ('remuneration', 'closeout_finance', 'Accrual sheet',
   'Unbilled delivery recognised for the period. Commercial.', true, 30),

-- --- 11_Invoice_generation ---------------------------------------------------
  ('invoice_generation', 'closeout_finance', 'Invoice template',
   'Monthly invoice raised to the college. Commercial.', true, 10),
  ('invoice_generation', 'closeout_finance', 'Invoice tracker',
   'Raised, sent and settled state per invoice, keyed on PAN/FY/MON/seq (§6). Commercial.', true, 20)

on conflict (category, lower(name)) do update set
  stage       = excluded.stage,
  description = excluded.description,
  is_required = excluded.is_required,
  order_index = excluded.order_index;
