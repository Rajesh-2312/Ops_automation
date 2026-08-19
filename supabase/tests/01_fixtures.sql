-- =============================================================================
-- byteXL Ops Intelligence Platform — RLS harness — fixtures
-- =============================================================================
-- Shaped so that every CLAUDE.md §4 rule has both a POSITIVE and a NEGATIVE
-- case. The negatives are the point (R5: "a forbidden read returns zero rows"),
-- so the data is arranged to make a leak show up as a non-zero count rather than
-- as an ambiguous one.
--
-- THE SCOPE GRAPH
-- ---------------
--   cluster 1 (North) ── college A ── program PA (bCAP) ── batch A1 ── trainer A
--                     │                                 └─ batch A2   (NOBODY)
--                     └─ college B ── program PB (CRT)  ── batch B1 ── trainer B
--   cluster 2 (South) ── college C ── program PC (bCAP) ── batch C1   (no deploy)
--
-- Reach comes from the two assignment tables, NEVER from profiles.college_id
-- (CLAUDE.md §4). profiles.college_id is set only on the two college logins.
--
--   senior_manager  -> cluster 1            => colleges A, B.  College C proves
--                                              the cluster boundary is real.
--   manager_a       -> colleges A AND B     => a Manager covers many colleges.
--   manager_b       -> college C            => the other side of every
--                                              cross-manager negative.
--   lde_a1          -> college A            => campus scope, no commercials.
--   lde_a2          -> college B            => two LDEs, mutually isolated.
--   admin           -> NOTHING              => is_admin() is the right to GRANT
--                                              reach, not reach itself (0700
--                                              header). Asserted: sees colleges,
--                                              sees zero P&L.
--
-- THE DELIBERATE ASYMMETRIES
-- --------------------------
--   * Trainer A is deployed to batch A1 but NOT batch A2, which is in the SAME
--     program. `trainer_on_program()` is true for PA but `trainer_on_batch()` is
--     false for A2, so "sees own deployment" is distinguishable from "sees the
--     whole program". A count of 2 batches here is a leak.
--   * Trainers A and B share no college, so cross-trainer leakage is a non-zero
--     count rather than an ambiguity.
--   * Program PA carries one SHARED and one UNSHARED governance report; PB
--     carries only an unshared one. That gives college B a genuine zero on the
--     publish gate rather than an accidental one.
--   * Program PA carries program_documents in BOTH commercial categories
--     (remuneration, invoice_generation) and two operational ones, so the LDE
--     Executive's split on that table is measurable in one query.
--   * Both colleges A and B carry attendance, so an isolation failure in the
--     RLS-BYPASSING views of 0800 surfaces as extra rows rather than silence.
--
-- Runs as postgres (BYPASSRLS), so no policy applies during setup, and the
-- column-guard triggers all short-circuit on `auth.uid() is null`.
--
-- Fixed UUIDs so assertions can reference them literally:
--   clusters c1..   colleges c0..   programs a1..   batches b1..   students 50..
--   trainers d0..   deployments e0..   work orders 40..   users f0..
--   tasks 7a..      remuneration 4e..   governance 61..   documents d1..
-- =============================================================================


-- --- Clusters ----------------------------------------------------------------

insert into public.clusters (id, name) values
  ('c1000000-0000-0000-0000-000000000001', 'North Cluster'),
  ('c1000000-0000-0000-0000-000000000002', 'South Cluster');


-- --- Colleges ----------------------------------------------------------------
-- A and B in cluster 1, C in cluster 2. That split is what makes the senior
-- manager's reach provable AND bounded.

insert into public.colleges (id, cluster_id, name, city, contact_name, mou_status, po_status) values
  ('c0000000-0000-0000-0000-00000000000a', 'c1000000-0000-0000-0000-000000000001',
   'Alpha Institute of Technology', 'Hyderabad', 'R. Rao',   'signed', 'signed'),
  ('c0000000-0000-0000-0000-00000000000b', 'c1000000-0000-0000-0000-000000000001',
   'Beta College of Engineering',   'Pune',      'S. Iyer',  'signed', 'sent'),
  ('c0000000-0000-0000-0000-00000000000c', 'c1000000-0000-0000-0000-000000000002',
   'Gamma College of Science',      'Chennai',   'K. Menon', 'sent',   'not_started');


-- --- Programs ----------------------------------------------------------------

insert into public.programs (id, college_id, type, name, start_date, end_date, stage) values
  ('a1000000-0000-0000-0000-00000000000a', 'c0000000-0000-0000-0000-00000000000a',
   'bCAP', 'Alpha bCAP 2026', '2026-07-01', '2026-12-15', 'active_monitoring'),
  ('a1000000-0000-0000-0000-00000000000b', 'c0000000-0000-0000-0000-00000000000b',
   'CRT',  'Beta CRT 2026',   '2026-08-01', '2026-11-30', 'deployment'),
  ('a1000000-0000-0000-0000-00000000000c', 'c0000000-0000-0000-0000-00000000000c',
   'bCAP', 'Gamma bCAP 2026', '2026-09-01', '2027-01-31', 'acquisition_setup');


-- --- Batches -----------------------------------------------------------------
-- A1 and A2 are BOTH in program PA. Trainer A is deployed only to A1 — see the
-- header. This is the single most important row pair in the fixture.

insert into public.batches (id, program_id, name, branch, expected_student_count) values
  ('b1000000-0000-0000-0000-0000000000a1', 'a1000000-0000-0000-0000-00000000000a', 'Alpha CSE-A', 'CSE', 60),
  ('b1000000-0000-0000-0000-0000000000a2', 'a1000000-0000-0000-0000-00000000000a', 'Alpha ECE-A', 'ECE', 55),
  ('b1000000-0000-0000-0000-0000000000b1', 'a1000000-0000-0000-0000-00000000000b', 'Beta IT-A',   'IT',  50),
  ('b1000000-0000-0000-0000-0000000000c1', 'a1000000-0000-0000-0000-00000000000c', 'Gamma MECH-A','MECH',40);


-- --- Students ----------------------------------------------------------------
-- A1: 3, A2: 2, B1: 2, C1: 1  =  8 total.

insert into public.students (id, batch_id, full_name, roll_number, credentials_status) values
  ('50000000-0000-0000-0000-0000000000a1', 'b1000000-0000-0000-0000-0000000000a1', 'Student A1-1', 'A1-001', 'activated'),
  ('50000000-0000-0000-0000-0000000000a2', 'b1000000-0000-0000-0000-0000000000a1', 'Student A1-2', 'A1-002', 'activated'),
  ('50000000-0000-0000-0000-0000000000a3', 'b1000000-0000-0000-0000-0000000000a1', 'Student A1-3', 'A1-003', 'shared'),
  ('50000000-0000-0000-0000-0000000000a4', 'b1000000-0000-0000-0000-0000000000a2', 'Student A2-1', 'A2-001', 'created'),
  ('50000000-0000-0000-0000-0000000000a5', 'b1000000-0000-0000-0000-0000000000a2', 'Student A2-2', 'A2-002', 'created'),
  ('50000000-0000-0000-0000-0000000000b1', 'b1000000-0000-0000-0000-0000000000b1', 'Student B1-1', 'B1-001', 'not_created'),
  ('50000000-0000-0000-0000-0000000000b2', 'b1000000-0000-0000-0000-0000000000b1', 'Student B1-2', 'B1-002', 'not_created'),
  ('50000000-0000-0000-0000-0000000000c1', 'b1000000-0000-0000-0000-0000000000c1', 'Student C1-1', 'C1-001', 'not_created');


-- --- Trainers ----------------------------------------------------------------
-- PAN is the identity key (CLAUDE.md §6) and is CHECK-constrained to 10 upper
-- case characters, so these are shaped like real PANs rather than filler.

insert into public.trainers (id, pan, full_name, email, type, work_order_status, zoho_id, erm_status) values
  ('d0000000-0000-0000-0000-00000000000a', 'AAAPA1234A', 'Anita Sharma',  'anita@example.test', 'freelancer', 'signed', 'ZOHO-A-1', 'synced'),
  ('d0000000-0000-0000-0000-00000000000b', 'BBBPB5678B', 'Bala Krishnan', 'bala@example.test',  'full_timer', 'signed', 'ZOHO-B-1', 'synced');


-- --- Deployments -------------------------------------------------------------

insert into public.deployments (id, trainer_id, batch_id, start_date, end_date, tracksheet_url) values
  ('e0000000-0000-0000-0000-00000000000a', 'd0000000-0000-0000-0000-00000000000a',
   'b1000000-0000-0000-0000-0000000000a1', '2026-07-01', '2026-12-15', 'https://tracksheets.test/a1'),
  ('e0000000-0000-0000-0000-00000000000b', 'd0000000-0000-0000-0000-00000000000b',
   'b1000000-0000-0000-0000-0000000000b1', '2026-08-01', '2026-11-30', 'https://tracksheets.test/b1');


-- --- Users -------------------------------------------------------------------
-- Inserting into auth.users fires handle_new_user(), which creates the profile
-- from raw_user_meta_data.role (falling back to the least-privilege default,
-- 'trainer' with a NULL trainer_id).
--
--   ...0001 senior_manager   ...0002 manager_a   ...0003 manager_b
--   ...0004 lde_a1           ...0005 lde_a2      ...0006 admin
--   ...000a trainer_a        ...000b trainer_b
--   ...000c college_a        ...000d college_b   ...000e unassigned
--
-- ELEVEN, not the nine personas of §4. The two extras earn their place:
--
--   admin       — `is_admin` is the only role that may change a persona or grant
--                 reach. Without it nothing exercises the admin half of the
--                 profiles guard, the assignment tables, or template curation,
--                 and the guard test degenerates into "everyone is refused".
--                 It carries NO assignment on purpose, so the suite can assert
--                 the 0700 rule: admin is the right to grant reach, not reach.
--   college_b   — the 0800 views are `security_invoker = false` and bypass RLS;
--                 their internal WHERE clause is the entire boundary. Proving it
--                 needs a SECOND college login reading the same views.

insert into auth.users (id, email, raw_user_meta_data) values
  ('f0000000-0000-0000-0000-000000000001', 'sm.north@bytexl.test',   '{"role":"senior_manager","full_name":"Senior Manager North"}'),
  ('f0000000-0000-0000-0000-000000000002', 'mgr.a@bytexl.test',      '{"role":"manager","full_name":"Manager A"}'),
  ('f0000000-0000-0000-0000-000000000003', 'mgr.b@bytexl.test',      '{"role":"manager","full_name":"Manager B"}'),
  ('f0000000-0000-0000-0000-000000000004', 'lde.a1@bytexl.test',     '{"role":"lde_executive","full_name":"LDE Alpha Campus"}'),
  ('f0000000-0000-0000-0000-000000000005', 'lde.a2@bytexl.test',     '{"role":"lde_executive","full_name":"LDE Beta Campus"}'),
  ('f0000000-0000-0000-0000-000000000006', 'admin@bytexl.test',      '{"role":"senior_manager","full_name":"Ops Admin"}'),
  ('f0000000-0000-0000-0000-00000000000a', 'anita@example.test',     '{"role":"trainer","full_name":"Anita Sharma"}'),
  ('f0000000-0000-0000-0000-00000000000b', 'bala@example.test',      '{"role":"trainer","full_name":"Bala Krishnan"}'),
  ('f0000000-0000-0000-0000-00000000000c', 'contact@alpha.test',     '{"role":"college","full_name":"Alpha Contact"}'),
  ('f0000000-0000-0000-0000-00000000000d', 'contact@beta.test',      '{"role":"college","full_name":"Beta Contact"}'),
  -- A brand-new signup with no metadata at all: must land on the least-privilege
  -- default and be able to see nothing anywhere.
  ('f0000000-0000-0000-0000-00000000000e', 'newbie@bytexl.test',     '{}');

-- Admin flag. profiles_admin_ck constrains is_admin to senior_manager, so this
-- update would be rejected on any other persona.
update public.profiles set is_admin = true
  where id = 'f0000000-0000-0000-0000-000000000006';

-- External identity links. profiles_role_link_ck forbids trainer_id on a college
-- persona, college_id on a trainer, and BOTH on any internal persona — so the
-- three internal users above deliberately get neither.
update public.profiles set trainer_id = 'd0000000-0000-0000-0000-00000000000a'
  where id = 'f0000000-0000-0000-0000-00000000000a';
update public.profiles set trainer_id = 'd0000000-0000-0000-0000-00000000000b'
  where id = 'f0000000-0000-0000-0000-00000000000b';
update public.profiles set college_id = 'c0000000-0000-0000-0000-00000000000a'
  where id = 'f0000000-0000-0000-0000-00000000000c';
update public.profiles set college_id = 'c0000000-0000-0000-0000-00000000000b'
  where id = 'f0000000-0000-0000-0000-00000000000d';


-- --- Scope assignments -------------------------------------------------------
-- THIS is where reach comes from (CLAUDE.md §4). manager_a holds two rows, which
-- is the whole point of the join table.

insert into public.user_college_assignments (user_id, college_id, assigned_by) values
  ('f0000000-0000-0000-0000-000000000002', 'c0000000-0000-0000-0000-00000000000a', 'f0000000-0000-0000-0000-000000000006'),
  ('f0000000-0000-0000-0000-000000000002', 'c0000000-0000-0000-0000-00000000000b', 'f0000000-0000-0000-0000-000000000006'),
  ('f0000000-0000-0000-0000-000000000003', 'c0000000-0000-0000-0000-00000000000c', 'f0000000-0000-0000-0000-000000000006'),
  ('f0000000-0000-0000-0000-000000000004', 'c0000000-0000-0000-0000-00000000000a', 'f0000000-0000-0000-0000-000000000006'),
  ('f0000000-0000-0000-0000-000000000005', 'c0000000-0000-0000-0000-00000000000b', 'f0000000-0000-0000-0000-000000000006');

insert into public.user_cluster_assignments (user_id, cluster_id, assigned_by) values
  ('f0000000-0000-0000-0000-000000000001', 'c1000000-0000-0000-0000-000000000001', 'f0000000-0000-0000-0000-000000000006');


-- --- Tasks -------------------------------------------------------------------
-- PA: 4 tasks, 2 done, exactly one owned by trainer A's user.
-- PB: 2 tasks, 0 done  (so the two colleges' checklist_complete_pct differ and a
--                       view mix-up cannot hide behind an identical number).
-- PC: 2 tasks, 0 done.

insert into public.tasks (id, program_id, stage, title, owner_id, owner_role, status, due_date, cadence) values
  ('7a000000-0000-0000-0000-0000000000a1', 'a1000000-0000-0000-0000-00000000000a', 'deployment',
   'Create tracksheet and assign educator', 'f0000000-0000-0000-0000-000000000004', 'lde_executive', 'done',        '2026-06-27', 'one_time'),
  ('7a000000-0000-0000-0000-0000000000a2', 'a1000000-0000-0000-0000-00000000000a', 'deployment',
   'Generate student platform credentials', 'f0000000-0000-0000-0000-000000000004', 'lde_executive', 'done',        '2026-06-25', 'one_time'),
  ('7a000000-0000-0000-0000-0000000000a3', 'a1000000-0000-0000-0000-00000000000a', 'active_monitoring',
   'Mark daily attendance',                 'f0000000-0000-0000-0000-00000000000a', 'trainer',       'in_progress', '2026-12-15', 'daily'),
  ('7a000000-0000-0000-0000-0000000000a4', 'a1000000-0000-0000-0000-00000000000a', 'active_monitoring',
   'Prepare governance report',             null,                                   'manager',       'pending',     '2026-10-01', 'monthly'),

  ('7a000000-0000-0000-0000-0000000000b1', 'a1000000-0000-0000-0000-00000000000b', 'deployment',
   'Confirm platform access and student onboarding', 'f0000000-0000-0000-0000-000000000005', 'lde_executive', 'pending',     '2026-08-01', 'one_time'),
  ('7a000000-0000-0000-0000-0000000000b2', 'a1000000-0000-0000-0000-00000000000b', 'deployment',
   'Run introduction call with college',             'f0000000-0000-0000-0000-000000000005', 'lde_executive', 'in_progress', '2026-07-31', 'one_time'),

  ('7a000000-0000-0000-0000-0000000000c1', 'a1000000-0000-0000-0000-00000000000c', 'acquisition_setup',
   'Collect signed MOU from college', 'f0000000-0000-0000-0000-000000000003', 'manager', 'in_progress', '2026-07-18', 'one_time'),
  ('7a000000-0000-0000-0000-0000000000c2', 'a1000000-0000-0000-0000-00000000000c', 'acquisition_setup',
   'Obtain purchase order (PO)',      'f0000000-0000-0000-0000-000000000003', 'manager', 'pending',     '2026-07-28', 'one_time');


-- --- Monitoring --------------------------------------------------------------

insert into public.observations (id, deployment_id, observer_id, observed_on, notes) values
  ('06000000-0000-0000-0000-0000000000a1', 'e0000000-0000-0000-0000-00000000000a',
   'f0000000-0000-0000-0000-000000000004', '2026-09-10', 'Good pace; revisit recursion module.');

insert into public.feedback (id, batch_id, source, collected_on, external_url, summary_score, response_count) values
  ('0f000000-0000-0000-0000-0000000000a1', 'b1000000-0000-0000-0000-0000000000a1',
   'gform', '2026-09-15', 'https://forms.test/alpha-a1', 4.30, 52);

insert into public.assessments (id, batch_id, title, conducted_on, clickup_url, report_url) values
  ('0a000000-0000-0000-0000-0000000000a1', 'b1000000-0000-0000-0000-0000000000a1',
   'Mid-term assessment', '2026-09-20', 'https://clickup.test/a1', 'https://reports.test/a1'),
  ('0a000000-0000-0000-0000-0000000000b1', 'b1000000-0000-0000-0000-0000000000b1',
   'Baseline assessment', '2026-08-20', 'https://clickup.test/b1', 'https://reports.test/b1');


-- --- STUDENT attendance (attendance_records) ---------------------------------
-- Deployment A: 2 sessions x 3 students = 6 marks, 5 of them present-or-late
--               => the college view must compute 83.3%.
-- Deployment B: 2 marks, both present => 100.0%.
-- Two different percentages, so a cross-college leak through the 0800 views
-- changes a number rather than merely adding a row.

insert into public.attendance_records (deployment_id, student_id, session_date, status) values
  ('e0000000-0000-0000-0000-00000000000a', '50000000-0000-0000-0000-0000000000a1', '2026-09-01', 'present'),
  ('e0000000-0000-0000-0000-00000000000a', '50000000-0000-0000-0000-0000000000a2', '2026-09-01', 'present'),
  ('e0000000-0000-0000-0000-00000000000a', '50000000-0000-0000-0000-0000000000a3', '2026-09-01', 'absent'),
  ('e0000000-0000-0000-0000-00000000000a', '50000000-0000-0000-0000-0000000000a1', '2026-09-02', 'present'),
  ('e0000000-0000-0000-0000-00000000000a', '50000000-0000-0000-0000-0000000000a2', '2026-09-02', 'late'),
  ('e0000000-0000-0000-0000-00000000000a', '50000000-0000-0000-0000-0000000000a3', '2026-09-02', 'present'),
  ('e0000000-0000-0000-0000-00000000000b', '50000000-0000-0000-0000-0000000000b1', '2026-08-05', 'present'),
  ('e0000000-0000-0000-0000-00000000000b', '50000000-0000-0000-0000-0000000000b2', '2026-08-05', 'present');


-- --- TRAINER attendance (trainer_attendance) ---------------------------------
-- A payout input, one row per trainer-day (CLAUDE.md §6). Deployment A: 3 marks,
-- Deployment B: 2. Kept on distinct dates from each other so a cross-deployment
-- leak is visible in the count.

insert into public.trainer_attendance (deployment_id, mark_date, mark, marked_by, note) values
  ('e0000000-0000-0000-0000-00000000000a', '2026-07-26', 'P',       'f0000000-0000-0000-0000-000000000004', null),
  ('e0000000-0000-0000-0000-00000000000a', '2026-07-27', 'P',       'f0000000-0000-0000-0000-000000000004', null),
  ('e0000000-0000-0000-0000-00000000000a', '2026-07-28', 'H',       'f0000000-0000-0000-0000-000000000004', 'Left after morning session.'),
  ('e0000000-0000-0000-0000-00000000000b', '2026-08-03', 'P',       'f0000000-0000-0000-0000-000000000005', null),
  ('e0000000-0000-0000-0000-00000000000b', '2026-08-04', 'A',       'f0000000-0000-0000-0000-000000000005', 'Absent, informed.');


-- --- Work orders (COMMERCIAL — the rate lives here, not on deployments) ------

insert into public.work_orders (id, trainer_id, program_id, rate, rate_basis, valid_from, valid_to, status, signed_at, document_url) values
  ('40000000-0000-0000-0000-00000000000a', 'd0000000-0000-0000-0000-00000000000a', 'a1000000-0000-0000-0000-00000000000a',
   80000.00, 'per_month', '2026-07-01', '2026-12-31', 'signed', '2026-06-20 10:00+05:30',
   'documents/trainers/d0000000-0000-0000-0000-00000000000a/signed-work-order.pdf'),
  ('40000000-0000-0000-0000-00000000000b', 'd0000000-0000-0000-0000-00000000000b', 'a1000000-0000-0000-0000-00000000000b',
   3500.00,  'per_day',   '2026-08-01', '2026-11-30', 'signed', '2026-07-22 10:00+05:30',
   'documents/trainers/d0000000-0000-0000-0000-00000000000b/signed-work-order.pdf');


-- --- Remuneration sheets (COMMERCIAL, plus the owning trainer) ----------------
-- Sheet A mirrors the CLAUDE.md §6 regression fixture (bCAP, 80,000/month,
-- 26-31 Jul 2026, TA&DA 100 => net 14,035). It is here as REALISTIC DATA, not as
-- an arithmetic test — R2 puts every rupee of that computation in
-- app/services/remuneration/engine.py, and this suite asserts only who may read
-- the row. The engine's own unit tests own the numbers.
--
-- Sheet B is the per-day (CRT) branch, consistent with work order B: 3,500 x 6.

insert into public.remuneration_sheets
  (id, trainer_id, program_id, work_order_id, period_start, period_end,
   rate, rate_basis, payable_days, days_in_month,
   earned, ta_da, accommodation, travel_reimb, gross,
   tds_rate, tds, deductions, net_amount, amount_in_words,
   payout_status, invoice_pan, invoice_fy, invoice_month, invoice_seq, invoice_no)
values
  ('4e000000-0000-0000-0000-0000000000a1', 'd0000000-0000-0000-0000-00000000000a',
   'a1000000-0000-0000-0000-00000000000a', '40000000-0000-0000-0000-00000000000a',
   '2026-07-26', '2026-07-31',
   80000.00, 'per_month', 6.00, 31,
   15483.87, 100.00, 0.00, 0.00, 15583.87,
   0.1000, 1548.39, 0.00, 14035.00, 'Fourteen thousand and thirty five rupees only',
   'sent', 'AAAPA1234A', '26-27', 'JUL', 1, 'AAAP/26-27/JUL1'),

  ('4e000000-0000-0000-0000-0000000000b1', 'd0000000-0000-0000-0000-00000000000b',
   'a1000000-0000-0000-0000-00000000000b', '40000000-0000-0000-0000-00000000000b',
   '2026-08-01', '2026-08-31',
   3500.00, 'per_day', 6.00, 31,
   21000.00, 0.00, 0.00, 0.00, 21000.00,
   0.1000, 2100.00, 0.00, 18900.00, 'Eighteen thousand nine hundred rupees only',
   'not_started', 'BBBPB5678B', '26-27', 'AUG', 1, 'BBBP/26-27/AUG1');


-- --- P&L (COMMERCIAL) --------------------------------------------------------
-- One row per program (unique index on program_id). All three programs carry
-- one, so "manager_a sees cluster 2's P&L" has something concrete to fail on.

insert into public.pnl (program_id, revenue, trainer_cost, travel_cost, accrued_amount, invoiced_amount) values
  ('a1000000-0000-0000-0000-00000000000a', 1800000.00, 510000.00, 45000.00, 300000.00, 900000.00),
  ('a1000000-0000-0000-0000-00000000000b', 1200000.00, 368000.00, 22000.00, 200000.00, 600000.00),
  ('a1000000-0000-0000-0000-00000000000c',  950000.00, 240000.00, 18000.00, 150000.00,      0.00);


-- --- Governance reports ------------------------------------------------------
-- The publish gate: shared_with_college_at. PA has one shared and one draft; PB
-- has ONLY a draft, so college B's correct answer through the view is zero.

insert into public.governance_reports (id, program_id, title, url, reporting_period_start, reporting_period_end, shared_with_college_at, shared_by) values
  ('61000000-0000-0000-0000-0000000000a1', 'a1000000-0000-0000-0000-00000000000a',
   'Alpha governance — September', 'https://reports.test/gov-a-sep', '2026-09-01', '2026-09-30',
   '2026-10-01 09:00+05:30', 'f0000000-0000-0000-0000-000000000002'),
  ('61000000-0000-0000-0000-0000000000a2', 'a1000000-0000-0000-0000-00000000000a',
   'Alpha governance — October (draft)', 'https://reports.test/gov-a-oct', '2026-10-01', '2026-10-31',
   null, null),
  ('61000000-0000-0000-0000-0000000000b1', 'a1000000-0000-0000-0000-00000000000b',
   'Beta governance — September (draft)', 'https://reports.test/gov-b-sep', '2026-09-01', '2026-09-30',
   null, null),
  ('61000000-0000-0000-0000-0000000000c1', 'a1000000-0000-0000-0000-00000000000c',
   'Gamma governance — September', 'https://reports.test/gov-c-sep', '2026-09-01', '2026-09-30',
   '2026-10-02 09:00+05:30', 'f0000000-0000-0000-0000-000000000003');


-- --- Document register -------------------------------------------------------
-- PA deliberately carries rows in BOTH commercial categories AND two operational
-- ones. `program_documents` is the one table where the commercials wall is a
-- CATEGORY filter rather than a table boundary, so this is the only way to test
-- it: an LDE Executive with full reach to PA must see 2 of these 4 rows.

insert into public.program_documents (id, program_id, category, name, status, url) values
  ('d1000000-0000-0000-0000-0000000000a1', 'a1000000-0000-0000-0000-00000000000a',
   'college_onboarding',     'Alpha MOU',                'approved',    'https://drive.test/alpha-mou'),
  ('d1000000-0000-0000-0000-0000000000a2', 'a1000000-0000-0000-0000-00000000000a',
   'program_planning',       'Alpha program plan',       'in_progress', null),
  ('d1000000-0000-0000-0000-0000000000a3', 'a1000000-0000-0000-0000-00000000000a',
   'remuneration',           'Alpha remuneration — July','filed',       'https://drive.test/alpha-rem-jul'),
  ('d1000000-0000-0000-0000-0000000000a4', 'a1000000-0000-0000-0000-00000000000a',
   'invoice_generation',     'Alpha invoice — July',     'not_started', null),
  ('d1000000-0000-0000-0000-0000000000b1', 'a1000000-0000-0000-0000-00000000000b',
   'college_onboarding',     'Beta MOU',                 'filed',       'https://drive.test/beta-mou'),
  ('d1000000-0000-0000-0000-0000000000c1', 'a1000000-0000-0000-0000-00000000000c',
   'monitoring_and_quality', 'Gamma observation pack',   'not_started', null);


-- --- Storage objects ---------------------------------------------------------
-- Paths follow the 0900 convention exactly — the policies parse them, so a typo
-- here reads as a policy failure. One object per branch of that file:
--   colleges/<id>/  three, one per college, to test reach
--   trainers/<id>/  two, behind the commercials wall
--   programs/<id>/  one, internal delivery paperwork
--
-- 00_isolate.sql cannot clear this table (Supabase's storage.protect_delete
-- trigger blocks direct deletes), so every POSITIVE count in the suite is scoped
-- to these prefixes. Zero-row assertions stay unscoped.

insert into storage.objects (bucket_id, name) values
  ('documents', 'colleges/c0000000-0000-0000-0000-00000000000a/mou.pdf'),
  ('documents', 'colleges/c0000000-0000-0000-0000-00000000000b/mou.pdf'),
  ('documents', 'colleges/c0000000-0000-0000-0000-00000000000c/mou.pdf'),
  ('documents', 'trainers/d0000000-0000-0000-0000-00000000000a/signed-work-order.pdf'),
  ('documents', 'trainers/d0000000-0000-0000-0000-00000000000b/signed-work-order.pdf'),
  ('documents', 'programs/a1000000-0000-0000-0000-00000000000a/governance-sep.pdf');


-- --- Outbound queue (1700) ---------------------------------------------------
-- CLAUDE.md §8's single queue, and the R5 case it exists to be tested for: the
-- wall on `comms_messages` is a property of the ROW (`is_commercial`), not of the
-- table, so PA carries one of each. An LDE Executive who FULLY REACHES PA must
-- see the operational message and zero of the commercial one — the scope
-- conjunct is satisfied and only the wall returns nothing.
--
-- PC gets a third row so reach is tested independently of the wall.

insert into public.comms_messages
  (id, program_id, channel, recipient_kind, recipient_ref, recipient_name,
   template_key, template_body, template_values, subject, body, diff,
   is_commercial, state)
values
  ('e1000000-0000-0000-0000-0000000000a1', 'a1000000-0000-0000-0000-00000000000a',
   'email', 'college', 'principal@alpha.test', 'Principal Alpha',
   'attendance.complete.v1',
   'Attendance for July 2026 is complete.', '{"month": "July 2026"}'::jsonb,
   'July attendance', 'Attendance for July 2026 is complete.',
   '{"version": 1, "identical": true, "hunks": []}'::jsonb,
   false, 'DRAFT'),
  ('e1000000-0000-0000-0000-0000000000a2', 'a1000000-0000-0000-0000-00000000000a',
   'email', 'trainer', 'anita@trainer.test', 'Anita Sharma',
   'payout.approved.v1',
   'Your July payout of INR 14035 is approved.', '{"net": "14035"}'::jsonb,
   'July payout', 'Your July payout of INR 14035 is approved.',
   '{"version": 1, "identical": true, "hunks": []}'::jsonb,
   true, 'PENDING_APPROVAL'),
  ('e1000000-0000-0000-0000-0000000000c1', 'a1000000-0000-0000-0000-00000000000c',
   'whatsapp', 'internal_staff', '+910000000000', 'TA desk',
   'sourcing.chase.v1',
   'Two profiles are still outstanding.', '{"count": 2}'::jsonb,
   null, 'Two profiles are still outstanding.',
   '{"version": 1, "identical": true, "hunks": []}'::jsonb,
   false, 'DRAFT');
