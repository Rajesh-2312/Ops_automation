import { ApiError, apiGet, apiPost } from './api'
import type { ProgramType } from './types'

/* =============================================================================
   The reporting API, as the browser sees it. Phase 6 (CLAUDE.md §8).
   =============================================================================

   Mirrors `app/api/reports.py`. Three endpoints, and nothing else exists:

       POST /reports/programs/{id}/governance   -> GovernanceReportDraft
       GET  /reports/programs/{id}/feedback     -> ProgramFeedback
       GET  /reports/colleges/{id}/summary      -> CollegeSummaryDraft

   THERE IS NO FOURTH FUNCTION IN THIS FILE, AND THERE MUST NOT BE.
   §8 caps the Reporting agent at DRAFT. `reports.py` says the same thing from
   the other side: "Nothing below sends, shares, publishes or transitions
   anything [...] `governance_reports.shared_with_college_at` — the publish gate
   — is not written by any handler in this file and must not become writable from
   one." A `shareWithCollege()` written here would be a client for an endpoint
   that does not exist, and adding the endpoint to make it work would be the
   defect. Approval and release have exactly one client in this app —
   `lib/approvals.ts`, driven from the Approvals screen by a human — and a
   governance report cannot use even that yet, for the reason
   `REPORT_APPROVAL_UNDECIDED_NOTE` states.

   EVERY FIGURE ARRIVES AS DATA; THE PROSE ARRIVES SEPARATELY (R1).
   The wire shape keeps the two apart and so does this file: the counts, the
   attendance percentage, the feedback scores and the trainer net-pay lines are
   fields on the report object, while the generated paragraphs are the whole of
   `narrative`, a sibling the UI labels as generated. Nothing here merges them,
   and nothing here reads a number out of `narrative.body`. `assert_grounded`
   inside the narrator has already refused any draft whose prose stated a figure
   the payload did not carry — a 422, not a repair.

   SCORES AND AMOUNTS ARE STRINGS AND STAY STRINGS.
   `reports.py` serialises every `Decimal` with `str()` (R7). Typing them as
   `string` is what makes the browser incapable of summing them: R2 puts all
   monetary arithmetic in `services/remuneration/engine.py`, and `TrainerCost`
   below carries the `total: null` the API sends deliberately, so that a screen
   showing per-trainer pay cannot quietly grow a programme total nobody
   reconciles.
   ============================================================================= */

/** `app.domain.enums.ArtifactState`. A report from these endpoints is only ever the first. */
export type ArtifactState = 'DRAFT' | 'PENDING_APPROVAL' | 'APPROVED' | 'RELEASED'

/** `X-Artifact-State` — the header `reports.py` shares with `payouts.py`. */
export const ARTIFACT_STATE_HEADER = 'X-Artifact-State'

// --- Wire shapes -------------------------------------------------------------

/**
 * `ApprovalOut` — whether this draft can be approved, and by whom.
 *
 * `can_be_approved` is false today for both artifact types here, and
 * `blocked_reason` is the server's own sentence about why. It is displayed
 * verbatim rather than replaced: the reason names §14 Q3, and a UI that
 * paraphrased it could drift from the API's answer.
 */
export interface ReportApproval {
  artifact_type: string
  can_be_approved: boolean
  approvers: string[]
  blocked_reason: string | null
}

/**
 * `NarrativeOut` — the generated prose, plus the §11 telemetry of the call.
 *
 * The telemetry is not decoration. It is how a reader tells at a glance that the
 * block came out of a model: which task routed it (§2 routes by task, never by a
 * hardcoded model id), which model answered, what it cost, how long it took.
 * `body` is the ONLY generated text in any response in this file.
 */
export interface ReportNarrative {
  body: string
  llm_task: string
  model: string
  prompt_tokens: number
  completion_tokens: number
  latency_ms: number
}

/** One persisted `remuneration_sheets.net_amount`, read back. COMMERCIAL (§4). */
export interface TrainerCostLine {
  trainer: string
  pan: string
  period_start: string
  period_end: string
  net: string | null
  invoice_no: string | null
}

/**
 * The commercial section. `total` is always `null`, and that is the contract.
 *
 * `reports.py`: "a sum computed in a reporting endpoint would be a second
 * implementation of money that nobody reconciles". Typed as `null` rather than
 * omitted so that anyone reaching for a total finds the reason instead of an
 * absence they might fill in.
 */
export interface TrainerCost {
  payout_count: number
  lines: TrainerCostLine[]
  trainers_without_payout: string[]
  total: null
}

/**
 * `FeedbackSynthesisOut`. Computed in `assembly.py` over the collections that
 * carried a score — hence two denominators, both on the wire, because an average
 * silently taken over part of the data is how a wrong number reaches a college
 * meeting.
 */
export interface FeedbackSynthesis {
  collections: number
  scored_collections: number
  total_responses: number | null
  average_score: string | null
  lowest_score: string | null
  highest_score: string | null
}

export interface FeedbackEntry {
  source: string
  collected_on: string | null
  summary_score: string | null
  response_count: number | null
}

/** `GovernanceReportOut`. A DRAFT artifact that the API did not persist. */
export interface GovernanceReportDraft {
  artifact_id: string
  artifact_state: ArtifactState
  artifact_version: number
  payload_hash: string
  is_commercial: boolean

  title: string
  program_id: string
  college_name: string
  program_name: string
  program_type: ProgramType
  period_start: string
  period_end: string

  batch_count: number
  trainer_count: number
  incomplete_tracksheet_count: number
  student_attendance_percent: string | null
  assessments_conducted: number
  observations: number
  tasks_open: number
  tasks_overdue: number

  feedback: FeedbackSynthesis
  trainer_cost: TrainerCost | null
  narrative: ReportNarrative | null
  approval: ReportApproval
  prior_reports: string[]
}

export interface CollegeSummaryLine {
  program_id: string
  program_name: string
  program_type: ProgramType
  stage: string
  batch_count: number
  trainer_count: number
  incomplete_tracksheets: number
}

/** `CollegeSummaryOut`. Carries no commercials by construction, not behind a flag (§4). */
export interface CollegeSummaryDraft {
  artifact_id: string
  artifact_state: ArtifactState
  title: string
  college_id: string
  college_name: string
  period_start: string
  period_end: string
  programs: CollegeSummaryLine[]
  narrative: ReportNarrative | null
  approval: ReportApproval
}

/** `ProgramFeedbackOut`. Not an artifact — a read, with nothing to approve. */
export interface ProgramFeedback {
  program_id: string
  program_name: string
  period_start: string
  period_end: string
  synthesis: FeedbackSynthesis
  entries: FeedbackEntry[]
  narrative: ReportNarrative | null
}

/**
 * `GovernanceReportRequest`. `extra="forbid"` on the server, so this object goes
 * out as-is and nothing may be bolted onto it — a mistyped `include_trainer_cost`
 * is a 422 rather than a silently non-commercial report.
 */
export interface GovernanceRequest {
  period_start: string
  period_end: string
  include_trainer_cost: boolean
  include_narrative: boolean
  /**
   * The date task overdue-ness is measured against. Null means "the end of the
   * period", never today: a report regenerated for an old period must report
   * what was true then.
   */
  as_of?: string | null
}

/**
 * What came back from the POST, plus what the response header said it was.
 *
 * The header is carried rather than assumed, for the reason `downloadSheet`
 * carries it: a client must not be able to render a draft as though it were
 * approved (R4), and the body field and the header agreeing is worth being able
 * to show. An absent header reads as DRAFT, which is the safe reading.
 */
export interface GovernanceDraftResult {
  report: GovernanceReportDraft
  headerState: ArtifactState
}

// --- Calls -------------------------------------------------------------------

/**
 * Assemble a DRAFT governance report for one program and period.
 *
 * A POST because it generates — optionally spending a frontier-tier call — not
 * because it publishes: `reports.py` persists no report here and cannot approve
 * what it produced. It does write one audit row recording who asked for a draft
 * and whether it was the commercial kind.
 */
export async function draftGovernanceReport(
  programId: string,
  request: GovernanceRequest,
): Promise<GovernanceDraftResult> {
  const response = await apiPost(
    `/reports/programs/${encodeURIComponent(programId)}/governance`,
    request,
  )
  const report = (await response.json()) as GovernanceReportDraft
  const header = response.headers.get(ARTIFACT_STATE_HEADER)
  return { report, headerState: isArtifactState(header) ? header : 'DRAFT' }
}

function isArtifactState(value: string | null): value is ArtifactState {
  return (
    value === 'DRAFT' ||
    value === 'PENDING_APPROVAL' ||
    value === 'APPROVED' ||
    value === 'RELEASED'
  )
}

/** Feedback synthesis for one program and period. Internal, and not commercial (§4). */
export function fetchProgramFeedback(
  programId: string,
  params: PeriodParams,
): Promise<ProgramFeedback> {
  return apiGet<ProgramFeedback>(
    `/reports/programs/${encodeURIComponent(programId)}/feedback${periodQuery(params)}`,
  )
}

/** Every program at one college, as a DRAFT artifact. */
export function fetchCollegeSummary(
  collegeId: string,
  params: PeriodParams,
): Promise<CollegeSummaryDraft> {
  return apiGet<CollegeSummaryDraft>(
    `/reports/colleges/${encodeURIComponent(collegeId)}/summary${periodQuery(params)}`,
  )
}

export interface PeriodParams {
  periodStart: string
  periodEnd: string
  includeNarrative: boolean
}

function periodQuery(params: PeriodParams): string {
  const query = new URLSearchParams({
    period_start: params.periodStart,
    period_end: params.periodEnd,
    include_narrative: String(params.includeNarrative),
  })
  return `?${query.toString()}`
}

// --- Query keys, local to this screen ----------------------------------------

/**
 * Kept here rather than in `lib/queryKeys.ts`, on purpose.
 *
 * Nothing outside this screen invalidates a report: these are FastAPI reads of
 * an assembled artifact, not PostgREST rows another screen can edit. The keys
 * carry the whole period and the narrative flag, because a report for a
 * different period — or one with prose in it — is a different answer and must
 * not be served out of the previous one's cache.
 */
export const reportKeys = {
  all: ['reports'] as const,
  feedback: (programId: string, start: string, end: string, narrative: boolean) =>
    ['reports', 'feedback', programId, start, end, narrative] as const,
  collegeSummary: (collegeId: string, start: string, end: string, narrative: boolean) =>
    ['reports', 'college-summary', collegeId, start, end, narrative] as const,
}

// --- Refusals, told apart -----------------------------------------------------

function statusOf(error: unknown): number | null {
  return error instanceof ApiError ? error.status : null
}

/**
 * 503 — narration is not configured in this environment.
 *
 * `get_narrator()` raises this when the OpenRouter variables are unset, which is
 * the DEFAULT in Phase 1 (§13: "Phase 1 has no AI in it"). The request was valid
 * and the facts are available without the narrative, so this must never render
 * as an outage or as a failed report.
 */
export function isNarrationUnavailable(error: unknown): boolean {
  return statusOf(error) === 503
}

/**
 * 422 — the platform threw the generated prose away.
 *
 * Either `assert_grounded` caught a figure that was not in the structured data
 * (R1), or `check_citations` caught a §9 violation. Both mean the system worked:
 * the draft was refused rather than corrected, because a corrected sentence
 * hides a systematic problem. `reports.py` deliberately does not retry, so
 * neither does this screen.
 *
 * A 422 from Pydantic on the period is the same status; both are the request
 * failing rather than the server breaking, and the message says which.
 */
export function isDraftRefused(error: unknown): boolean {
  return statusOf(error) === 422
}

/** 403 — the commercials wall (§4), or a persona outside `require_internal()`. */
export function isForbidden(error: unknown): boolean {
  return statusOf(error) === 403
}

/** 404 — no such program or college, or one outside this account's reach. */
export function isNotFound(error: unknown): boolean {
  return statusOf(error) === 404
}

// --- The sentences this screen has to be able to say --------------------------

/**
 * §14 Q3, as it reaches a report rather than as it reaches the approval queue.
 *
 * `approval_readiness()` asks the approval layer the question WITHOUT raising,
 * so a governance report arrives already carrying "nobody can approve this"
 * instead of discovering it at a 501. The wording follows
 * `app/api/approvals.py`: the caller is not forbidden, the system has no answer
 * yet, and no permission grant clears it.
 */
export const REPORT_APPROVAL_UNDECIDED_NOTE =
  'This draft cannot be approved by anybody, and that is not a permission problem. ' +
  'CLAUDE.md §14 Q3 — “Approval authority for college-facing comms: Manager or Senior ' +
  'Manager?” — is an open question the owner has not answered, so APPROVAL_AUTHORITY names ' +
  'an approver for remuneration sheets only. The reporting API reports that with the draft ' +
  'rather than letting you find it at a 501, which is what the Approvals screen would ' +
  'answer for the same reason. It clears when the question is answered, not when somebody ' +
  'is granted a role.'

/** Why the figure panels and the prose panel are drawn as two different things. */
export const FIGURES_NOTE =
  'Every number on this screen was SELECTed from a system of record and handed to the ' +
  'report assembler as structured input (R1). None of it was written by a model, and none ' +
  'of it is read back out of the narrative.'

export const NARRATIVE_NOTE =
  'Generated text. The model was given the assembled figures and asked for language, never ' +
  'for arithmetic — and before this was returned, every digit run in it was checked against ' +
  'the structured payload by assert_grounded. A figure the payload did not contain refuses ' +
  'the whole draft with a 422; it is never quietly corrected. Read it as a first pass to ' +
  'edit, not as a source.'

/** What a DRAFT report is and is not, said next to it (R3, R4, §8). */
export const NOTHING_SENDS_NOTE =
  'A report drafted here goes nowhere. There is no button on this screen that shares one ' +
  'with a college: §8 caps the Reporting agent at Draft, and R4 moves every artifact ' +
  'DRAFT → PENDING_APPROVAL → APPROVED → RELEASED with a human at each step. The publish ' +
  'gate — governance_reports.shared_with_college_at — is not writable from any reporting ' +
  'endpoint at all.'
