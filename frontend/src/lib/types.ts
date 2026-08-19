/* =============================================================================
   ⚠️  HAND-WRITTEN MIRROR OF supabase/migrations/*.sql — REGENERATE WHEN ABLE
   =============================================================================

   There is no provisioned Supabase project yet and no `supabase` CLI on the
   build machine, so this file was written by reading the ten migrations by
   hand. That means it can drift, silently, in exactly the way a generated file
   cannot: a column renamed in migration 1100 will still typecheck here.

   THE MOMENT A PROJECT EXISTS, REPLACE THIS FILE:

       npx supabase gen types typescript \
         --project-id <project-ref> \
         --schema public \
         > frontend/src/lib/generated-db.ts

   …then re-export the row aliases below from `Database['public']['Tables']`
   and delete the hand-written interfaces. Keep the LABEL/ORDER constants and
   the urgency helpers — those are presentation, not schema, and the generator
   has nothing to say about them.

   Until then: enum members below must match the Postgres enums in
   0100_enums_and_utils.sql exactly. The database will reject anything else,
   which is the intended behaviour.
   ============================================================================= */

// --- Enums (0100_enums_and_utils.sql) ---------------------------------------

/**
 * CLAUDE.md §4. Five personas: senior_manager -> manager -> lde_executive on
 * the internal side, plus trainer and college facing outward.
 *
 * PERSONA IS NOT SCOPE. `senior_manager` does not mean "sees everything" — it
 * means "sees the clusters assigned to them". Reach comes from
 * user_college_assignments / user_cluster_assignments, never from this value.
 */
export type AppRole =
  | 'senior_manager'
  | 'manager'
  | 'lde_executive'
  | 'trainer'
  | 'college'

export type ProgramType = 'bCAP' | 'CRT'

export type ProgramStage =
  | 'acquisition_setup'
  | 'trainer_sourcing'
  | 'trainer_onboarding'
  | 'deployment'
  | 'active_monitoring'
  | 'closeout_finance'

export type RateBasis = 'per_day' | 'per_month'
export type TaskStatus = 'pending' | 'in_progress' | 'done' | 'blocked'
export type TaskCadence = 'one_time' | 'daily' | 'weekly' | 'monthly' | 'quarterly'
export type ScheduleAnchor = 'program_start' | 'program_end'
export type TrainerType = 'freelancer' | 'full_timer'
export type DocStatus = 'not_started' | 'in_progress' | 'sent' | 'signed' | 'expired'
export type ErmStatus = 'not_pushed' | 'pending' | 'synced' | 'stale' | 'failed'

/** TRAINER-day mark, one row per deployment per day. Feeds the payout engine. */
export type AttendanceMark = 'P' | 'A' | 'H' | 'HOLIDAY' | 'UNMARKED'

/** STUDENT session attendance — a different grain and a different consumer. */
export type AttendanceStatus = 'present' | 'absent' | 'late' | 'excused'

export type CredentialsStatus = 'not_created' | 'created' | 'shared' | 'activated'
export type FeedbackSource = 'platform' | 'gform'

export type DocumentCategory =
  | 'templates_and_masters'
  | 'college_onboarding'
  | 'program_planning'
  | 'trainer_recruitment'
  | 'trainer_onboarding'
  | 'student_credentials'
  | 'trainer_deployment'
  | 'monitoring_and_quality'
  | 'feedback_and_assessments'
  | 'governance_reports'
  | 'remuneration'
  | 'invoice_generation'
  | 'archive'

export type DocumentStatus =
  | 'not_started'
  | 'in_progress'
  | 'filed'
  | 'approved'
  | 'not_applicable'

// --- Persona presentation ----------------------------------------------------

/**
 * Every persona the DATABASE can hold, for display. Includes `trainer`, which no
 * human may choose any more — see SELECTABLE_ROLES.
 */
export const ROLES: AppRole[] = [
  'senior_manager',
  'manager',
  'lde_executive',
  'trainer',
  'college',
]

/**
 * The personas a human may actually pick — at signup, and in Users & roles.
 *
 * `trainer` is deliberately absent (owner's decision, 2026-08-18: educators are
 * records, not users). Migration 1800 dropped all eighteen trainer policies, so
 * a trainer-persona account now reads zero rows from every table and can write
 * nothing. Offering the option would create an account that can do nothing at
 * all, which reads as a broken app rather than a closed door.
 *
 * `trainer` is NOT removed from the `AppRole` type or from ROLES above, and that
 * is not an oversight. `handle_new_user` still does
 * `coalesce(requested_role, 'trainer')`, so it remains the deny-by-default
 * sentinel for a signup whose role metadata is absent or malformed — and now
 * that it grants nothing, it is a better sentinel than it was. The database can
 * still return it, so the type must still model it.
 */
export const SELECTABLE_ROLES: AppRole[] = [
  'senior_manager',
  'manager',
  'lde_executive',
  'college',
]

export const ROLE_LABEL: Record<AppRole, string> = {
  senior_manager: 'Senior Manager',
  manager: 'Manager',
  lde_executive: 'LDE Executive',
  trainer: 'Trainer',
  college: 'College',
}

/** Short form for badges, where the sidebar is 60px of usable width. */
export const ROLE_SHORT: Record<AppRole, string> = {
  senior_manager: 'Sr Manager',
  manager: 'Manager',
  lde_executive: 'LDE',
  trainer: 'Trainer',
  college: 'College',
}

export const ROLE_SCOPE_BLURB: Record<AppRole, string> = {
  senior_manager:
    'All programs in their assigned clusters. P&L, escalations, payout approval.',
  manager: 'Their assigned colleges. Full program state, trainer costs, reports.',
  lde_executive:
    'Their assigned colleges only. Attendance, batches, daily tasks. No commercials.',
  trainer: 'Own deployment, tracksheet, invoice status. Nothing else.',
  college: 'Published artifacts only, read-only.',
}

/** The three personas that are byteXL staff. Mirrors `public.is_internal()`. */
export const INTERNAL_ROLES: AppRole[] = ['senior_manager', 'manager', 'lde_executive']

export function isInternalRole(role: AppRole | undefined | null): boolean {
  return !!role && INTERNAL_ROLES.includes(role)
}

/**
 * Mirrors `public.can_see_commercials()` — true for Senior Manager and Manager
 * only (CLAUDE.md §4).
 *
 * COSMETIC ONLY. This function decides which nav items and panels are drawn.
 * It decides nothing about what data is reachable: an LDE Executive who edited
 * the bundle to render the P&L panel would get an empty table, because `pnl`,
 * `remuneration_sheets` and `work_orders` are gated by the same predicate
 * inside Postgres. Never use this in place of a policy, and never assume a
 * value is safe to display because a check like this one passed.
 */
export function roleSeesCommercials(role: AppRole | undefined | null): boolean {
  return role === 'senior_manager' || role === 'manager'
}

// --- Stage presentation ------------------------------------------------------

/** Ordered as in the enum — drives the kanban column order. */
export const STAGES: ProgramStage[] = [
  'acquisition_setup',
  'trainer_sourcing',
  'trainer_onboarding',
  'deployment',
  'active_monitoring',
  'closeout_finance',
]

export const STAGE_LABEL: Record<ProgramStage, string> = {
  acquisition_setup: 'Acquisition & Setup',
  trainer_sourcing: 'Trainer Sourcing',
  trainer_onboarding: 'Trainer Onboarding',
  deployment: 'Deployment',
  active_monitoring: 'Active Monitoring',
  closeout_finance: 'Closeout & Finance',
}

export const STAGE_BLURB: Record<ProgramStage, string> = {
  acquisition_setup: 'MoU/PO, batches, dates, P&L prepared',
  trainer_sourcing: 'TA request, TOC to L&S, ERM push',
  trainer_onboarding: 'Work order, ZOHO, ERM update, TOC shared',
  deployment: 'Credentials, travel, tracksheet, intro call',
  active_monitoring: 'Observations, feedback, attendance, reports',
  closeout_finance: 'Remuneration, payouts, accruals, invoices',
}

// --- Task presentation -------------------------------------------------------

export const TASK_STATUS_LABEL: Record<TaskStatus, string> = {
  pending: 'Pending',
  in_progress: 'In progress',
  done: 'Done',
  blocked: 'Blocked',
}

/** Most-frequent first, so the periodic work reads top-down. */
export const CADENCES: TaskCadence[] = [
  'daily',
  'weekly',
  'monthly',
  'quarterly',
  'one_time',
]

export const CADENCE_LABEL: Record<TaskCadence, string> = {
  one_time: 'One-time',
  daily: 'Daily',
  weekly: 'Weekly',
  monthly: 'Monthly',
  quarterly: 'Quarterly',
}

export const DOC_STATUS_LABEL: Record<DocStatus, string> = {
  not_started: 'Not started',
  in_progress: 'In progress',
  sent: 'Sent',
  signed: 'Signed',
  expired: 'Expired',
}

export const DOC_STATUSES: DocStatus[] = [
  'not_started',
  'in_progress',
  'sent',
  'signed',
  'expired',
]

export const ERM_STATUS_LABEL: Record<ErmStatus, string> = {
  not_pushed: 'Not pushed',
  pending: 'Pending',
  synced: 'Synced',
  stale: 'Stale — re-sync',
  failed: 'Failed',
}

export const TRAINER_TYPE_LABEL: Record<TrainerType, string> = {
  freelancer: 'Freelancer',
  full_timer: 'Full-timer',
}

export const RATE_BASIS_LABEL: Record<RateBasis, string> = {
  per_day: 'Per day',
  per_month: 'Per month',
}

// --- Urgency -----------------------------------------------------------------
// Derived, never stored. The Postgres definition lives in the `task_urgency`
// view; `deriveUrgency` below mirrors it so a list already in memory does not
// need a second round trip. If you change one, change the other — the view is
// the authority.

export type TaskUrgency =
  | 'overdue'
  | 'due_today'
  | 'due_soon'
  | 'scheduled'
  | 'unscheduled'
  | 'blocked'
  | 'done'

export const URGENCY_LABEL: Record<TaskUrgency, string> = {
  overdue: 'Overdue',
  due_today: 'Due today',
  due_soon: 'Due soon',
  scheduled: 'Scheduled',
  unscheduled: 'No due date',
  blocked: 'Blocked',
  done: 'Done',
}

/** The three bands that mean "act on this now". */
export const URGENT_BANDS: TaskUrgency[] = ['overdue', 'due_today', 'due_soon']

export function deriveUrgency(task: Pick<Task, 'status' | 'due_date'>): TaskUrgency {
  if (task.status === 'done') return 'done'
  if (task.status === 'blocked') return 'blocked'
  if (!task.due_date) return 'unscheduled'

  const days = daysBetweenTodayAnd(task.due_date)
  if (days < 0) return 'overdue'
  if (days === 0) return 'due_today'
  if (days <= 3) return 'due_soon'
  return 'scheduled'
}

function daysBetweenTodayAnd(isoDate: string): number {
  const today = new Date()
  today.setHours(0, 0, 0, 0)
  const due = new Date(`${isoDate}T00:00:00`)
  return Math.round((due.getTime() - today.getTime()) / 86_400_000)
}

export function daysUntilDue(task: Pick<Task, 'status' | 'due_date'>): number | null {
  if (task.status === 'done' || task.status === 'blocked' || !task.due_date) return null
  return daysBetweenTodayAnd(task.due_date)
}

/** "3d overdue" / "due today" / "in 5d" — for a compact inline hint. */
export function dueLabel(task: Pick<Task, 'status' | 'due_date'>): string | null {
  const days = daysUntilDue(task)
  if (days === null) return null
  if (days < 0) return `${Math.abs(days)}d overdue`
  if (days === 0) return 'due today'
  if (days === 1) return 'due tomorrow'
  return `in ${days}d`
}

// --- Document register presentation -----------------------------------------
// Mirrors the byteXL Drive: 00_Templates_and_Masters holds the masters, and each
// numbered folder is a category that belongs to a pipeline stage. Folders are
// categories rather than stages on purpose — twelve folders against six locked
// stages would mean relitigating the pipeline.

export const CATEGORY_LABEL: Record<DocumentCategory, string> = {
  templates_and_masters: '00 · Templates & Masters',
  college_onboarding: '01 · College Onboarding',
  program_planning: '02 · Program Planning',
  trainer_recruitment: '03 · Trainer Recruitment',
  trainer_onboarding: '04 · Trainer Onboarding',
  student_credentials: '05 · Student Credentials',
  trainer_deployment: '06 · Trainer Deployment',
  monitoring_and_quality: '07 · Monitoring & Quality',
  feedback_and_assessments: '08 · Feedback & Assessments',
  governance_reports: '09 · Governance Reports',
  remuneration: '10 · Remuneration',
  invoice_generation: '11 · Invoice Generation',
  archive: '99 · Archive',
}

/**
 * The two register categories the LDE Executive is denied at the database
 * (migration 1000: `category not in ('remuneration', 'invoice_generation')`).
 * A row there is a live pointer to a rupee figure, and Drive's permissions are
 * outside this database's control. Listed here for labelling only — the policy
 * is the wall.
 */
export const COMMERCIAL_CATEGORIES: DocumentCategory[] = [
  'remuneration',
  'invoice_generation',
]

export const DOCUMENT_STATUS_LABEL: Record<DocumentStatus, string> = {
  not_started: 'Not started',
  in_progress: 'In progress',
  filed: 'Filed',
  approved: 'Approved',
  not_applicable: 'N/A',
}

export const DOCUMENT_STATUSES: DocumentStatus[] = [
  'not_started',
  'in_progress',
  'filed',
  'approved',
  'not_applicable',
]

// --- Attendance presentation -------------------------------------------------

/** Order the marking grid offers them: the common case first. */
export const ATTENDANCE_MARKS: AttendanceMark[] = ['P', 'A', 'H', 'HOLIDAY', 'UNMARKED']

export const ATTENDANCE_MARK_LABEL: Record<AttendanceMark, string> = {
  P: 'Present',
  A: 'Absent',
  H: 'Half day',
  HOLIDAY: 'College holiday',
  UNMARKED: 'Unmarked',
}

export const ATTENDANCE_MARK_SHORT: Record<AttendanceMark, string> = {
  P: 'P',
  A: 'A',
  H: 'H',
  HOLIDAY: 'HOL',
  UNMARKED: '—',
}

/**
 * What a mark MEANS for pay, per program type (CLAUDE.md §5). Shown as a hint
 * on the marking grid so whoever is marking understands the consequence.
 *
 * This is a caption, not a calculation. No money is computed in this codebase
 * (CLAUDE.md R2) — `services/remuneration/engine.py` owns every rupee.
 */
export const ATTENDANCE_PAY_HINT: Record<ProgramType, Record<AttendanceMark, string>> = {
  CRT: {
    P: 'counts 1 payable day',
    A: 'counts 0',
    H: 'counts 0.5',
    HOLIDAY: 'not payable',
    UNMARKED: 'not payable — CRT counts UP from P',
  },
  bCAP: {
    P: 'no deduction',
    A: 'deducts 1 day',
    H: 'deducts 0.5',
    HOLIDAY: 'payable — the retainer absorbs it',
    UNMARKED: 'payable — bCAP counts DOWN from period length',
  },
}

// =============================================================================
// Row types — one per table, in migration order
// =============================================================================

// --- 0200_identity -----------------------------------------------------------

export interface Profile {
  id: string
  role: AppRole
  /**
   * Renamed from `is_ops_admin`. Only a Senior Manager may hold it
   * (profiles_admin_ck) — admin is the right to grant reach, so giving it to a
   * scope-limited persona would let that persona widen its own scope.
   */
  is_admin: boolean
  full_name: string | null
  /** College persona only. Internal staff never take scope from this column. */
  trainer_id: string | null
  college_id: string | null
  created_at: string
  updated_at: string
}

// --- 0300_org_core -----------------------------------------------------------

export interface Cluster {
  id: string
  name: string
  created_at: string
  updated_at: string
}

export interface College {
  id: string
  cluster_id: string | null
  name: string
  city: string | null
  contact_name: string | null
  contact_email: string | null
  contact_phone: string | null
  mou_status: DocStatus
  po_status: DocStatus
  notes: string | null
  created_at: string
  updated_at: string
}

/** Binds a Manager or LDE Executive to one college. Admin-write only. */
export interface UserCollegeAssignment {
  id: string
  user_id: string
  college_id: string
  assigned_by: string | null
  assigned_at: string
}

/**
 * Binds a Senior Manager to a cluster. `colleges.cluster_id` expands that to
 * every college in the cluster inside `my_college_ids()`.
 */
export interface UserClusterAssignment {
  id: string
  user_id: string
  cluster_id: string
  assigned_by: string | null
  assigned_at: string
}

export interface Program {
  id: string
  college_id: string
  type: ProgramType
  name: string
  start_date: string | null
  end_date: string | null
  timetable_url: string | null
  stage: ProgramStage
  created_at: string
  updated_at: string
}

export interface Batch {
  id: string
  program_id: string
  name: string
  branch: string | null
  section: string | null
  /**
   * Graduating year of the cohort, e.g. 2027. Batches are segregated by it —
   * `CSE-A` is a different set of students each year, so the name alone does
   * not identify a cohort.
   *
   * `null` only for rows created before migration 1500. New batches always
   * carry one; the add form requires it. Once the pre-existing rows are filled
   * in, a follow-up migration makes the column NOT NULL and this becomes
   * `number` — treat the null branch as a backlog item, not a permanent case.
   */
  passout_year: number | null
  expected_student_count: number | null
  created_at: string
  updated_at: string
}

export interface Student {
  id: string
  batch_id: string
  full_name: string | null
  roll_number: string | null
  credentials_status: CredentialsStatus
  source_system: string | null
  external_id: string | null
  external_url: string | null
  created_at: string
  updated_at: string
}

// --- 0400_trainers_deployments ----------------------------------------------

export interface Trainer {
  id: string
  /** Trainer identity IS the PAN. Never match a trainer by name string. */
  pan: string
  full_name: string
  email: string | null
  phone: string | null
  type: TrainerType
  work_order_status: DocStatus
  zoho_id: string | null
  zoho_url: string | null
  erm_status: ErmStatus
  erm_external_id: string | null
  erm_url: string | null
  erm_synced_at: string | null
  erm_synced_by: string | null
  created_at: string
  updated_at: string
}

export interface Deployment {
  id: string
  trainer_id: string
  batch_id: string
  start_date: string | null
  end_date: string | null
  tracksheet_url: string | null
  travel_notes: string | null
  travel_submitted_at: string | null
  created_at: string
  updated_at: string
}

// --- 1400_trainer_bank_rails -------------------------------------------------

/**
 * Payment rails for a trainer, 1:1 — `trainer_id` IS the primary key.
 *
 * A SEPARATE TABLE, NOT COLUMNS ON `trainers`, and the reason is the whole
 * design (migration 1400's header): RLS is ROW-level, so 0400's
 * `trainers_lde_select_deployed` policy returns EVERY column of the trainer row
 * to an LDE Executive. An account number living on `trainers` would be handed to
 * the persona §4 says has no commercials, and column-level GRANTs cannot rescue
 * it because every persona logs in as the one `authenticated` role.
 *
 * So the rails sit behind `can_see_commercials()` — Senior Manager and Manager
 * read AND write; an LDE Executive gets zero rows; the trainer may SELECT their
 * own row and there is deliberately no matching UPDATE policy, because a payee
 * who can repoint their own account between approval and release defeats R4.
 *
 * There is also NO DELETE GRANT. Corrections are updates; a deleted rail takes
 * with it the evidence of what an approved payout was executed against.
 *
 * The account number is a STRING and stays one. It is an identifier with
 * significant leading zeros, not a quantity — parsing it to a JS number would
 * lose them and would round anything over 2^53.
 */
export interface TrainerBankAccount {
  trainer_id: string
  /** Digits only (CHECK `^[0-9]+$`). No length rule — Indian account numbers
   *  run roughly 9–18 digits and vary by bank. */
  bank_account_number: string
  /** Exactly 11 characters, UPPERCASE. Both are CHECK constraints. */
  ifsc: string
  bank_name: string | null
  branch: string | null
  /** The beneficiary name as the BANK holds it. NEVER defaulted from
   *  `trainers.full_name` — a mismatch bounces the payment. */
  account_name: string | null
  created_at: string
  updated_at: string
}

// --- 0500_tasks --------------------------------------------------------------

export interface TaskTemplate {
  id: string
  stage: ProgramStage
  title: string
  description: string | null
  default_owner_role: AppRole
  order_index: number
  cadence: TaskCadence
  applies_to_type: ProgramType | null
  is_active: boolean
  /** Added in 1000. NULL means "no scheduled due date". */
  offset_days: number | null
  offset_anchor: ScheduleAnchor
  created_at: string
  updated_at: string
}

export interface Task {
  id: string
  program_id: string
  template_id: string | null
  stage: ProgramStage
  title: string
  description: string | null
  owner_id: string | null
  owner_role: AppRole | null
  status: TaskStatus
  cadence: TaskCadence
  due_date: string | null
  completed_at: string | null
  blocked_reason: string | null
  waiting_on: string | null
  source_system: string | null
  external_id: string | null
  external_url: string | null
  created_at: string
  updated_at: string
}

/** The `task_urgency` view: every task column plus the two derived ones. */
export interface TaskWithUrgency extends Task {
  urgency: TaskUrgency
  days_until_due: number | null
}

// --- 0600_monitoring ---------------------------------------------------------

export interface Observation {
  id: string
  deployment_id: string
  observer_id: string | null
  observed_on: string
  notes: string | null
  created_at: string
  updated_at: string
}

export interface Feedback {
  id: string
  batch_id: string
  source: FeedbackSource
  collected_on: string | null
  external_url: string | null
  external_id: string | null
  summary_score: number | null
  response_count: number | null
  created_at: string
  updated_at: string
}

export interface Assessment {
  id: string
  batch_id: string
  title: string | null
  conducted_on: string | null
  clickup_url: string | null
  report_url: string | null
  created_at: string
  updated_at: string
}

/** STUDENT attendance. Not a payout input. */
export interface AttendanceRecord {
  id: string
  deployment_id: string
  student_id: string | null
  session_date: string
  status: AttendanceStatus
  created_at: string
  updated_at: string
}

/**
 * TRAINER attendance — one row per deployment per day, never a wide D1–D31
 * layout, because payout disputes are always about specific days.
 *
 * This table is a payout INPUT. What is marked here is what the remuneration
 * engine counts.
 */
export interface TrainerAttendance {
  id: string
  deployment_id: string
  mark_date: string
  mark: AttendanceMark
  marked_by: string | null
  marked_at: string
  note: string | null
  updated_at: string
}

// --- 0700_finance (commercials — Senior Manager and Manager only) ------------

export interface WorkOrder {
  id: string
  trainer_id: string
  program_id: string
  /**
   * numeric(14,2) arrives from PostgREST as a STRING, and it is kept a string
   * all the way to the screen. Parsing it to a JS number would be the first
   * step towards arithmetic in the browser, and CLAUDE.md R2/R7 put every rupee
   * in `services/remuneration/engine.py` using Decimal.
   */
  rate: string
  rate_basis: RateBasis
  valid_from: string
  valid_to: string
  status: DocStatus
  signed_at: string | null
  document_url: string | null
  notes: string | null
  created_at: string
  updated_at: string
}

/** Phase 2. Typed here so the shape is agreed; no screen reads it yet. */
export interface RemunerationSheet {
  id: string
  trainer_id: string
  program_id: string
  work_order_id: string | null
  period_start: string
  period_end: string
  rate: string | null
  rate_basis: RateBasis | null
  payable_days: string | null
  days_in_month: number | null
  earned: string | null
  ta_da: string | null
  accommodation: string | null
  travel_reimb: string | null
  gross: string | null
  tds_rate: string | null
  tds: string | null
  deductions: string | null
  net_amount: string | null
  amount_in_words: string | null
  currency: string
  payout_status: DocStatus
  paid_on: string | null
  invoice_pan: string | null
  invoice_fy: string | null
  invoice_month: string | null
  invoice_seq: number | null
  invoice_no: string | null
  invoice_issued_at: string | null
  source_system: string | null
  external_id: string | null
  external_url: string | null
  created_at: string
  updated_at: string
}

export interface GovernanceReport {
  id: string
  program_id: string
  title: string | null
  url: string
  reporting_period_start: string | null
  reporting_period_end: string | null
  shared_with_college_at: string | null
  shared_by: string | null
  created_at: string
  updated_at: string
}

/** Phase 2. Commercials — zero rows for an LDE Executive, in the database. */
export interface Pnl {
  id: string
  program_id: string
  revenue: string | null
  trainer_cost: string | null
  travel_cost: string | null
  platform_cost: string | null
  other_cost: string | null
  accrued_amount: string | null
  invoiced_amount: string | null
  currency: string
  notes: string | null
  created_at: string
  updated_at: string
}

// --- 1000_scheduling_documents ----------------------------------------------

export interface DocumentTemplate {
  id: string
  category: DocumentCategory
  stage: ProgramStage | null
  name: string
  description: string | null
  master_url: string | null
  is_required: boolean
  applies_to_type: ProgramType | null
  order_index: number
  is_active: boolean
  created_at: string
  updated_at: string
}

export interface ProgramDocument {
  id: string
  program_id: string
  document_template_id: string | null
  category: DocumentCategory
  name: string
  status: DocumentStatus
  url: string | null
  owner_id: string | null
  due_date: string | null
  filed_at: string | null
  notes: string | null
  created_at: string
  updated_at: string
}

// --- 0800_college_views (read-only, curated) --------------------------------

export interface CollegeProgramProgress {
  program_id: string
  college_id: string
  program_name: string
  program_type: ProgramType
  stage: ProgramStage
  start_date: string | null
  end_date: string | null
  batch_count: number
  student_count: number
  tasks_total: number
  tasks_done: number
  checklist_complete_pct: number | null
}

export interface CollegeAttendanceSummary {
  batch_id: string
  program_id: string
  batch_name: string
  branch: string | null
  sessions_recorded: number
  first_session: string | null
  last_session: string | null
  marks_recorded: number
  marks_attended: number
  attendance_pct: number | null
}

export interface CollegeGovernanceReport {
  id: string
  program_id: string
  program_name: string
  title: string | null
  url: string
  reporting_period_start: string | null
  reporting_period_end: string | null
  shared_with_college_at: string
}