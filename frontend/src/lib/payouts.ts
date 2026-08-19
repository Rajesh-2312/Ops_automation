import { ApiError, apiPost } from './api'
import type { ProgramType, RateBasis } from './types'

/* =============================================================================
   The payout API, as the browser sees it.
   =============================================================================

   Mirrors `app/api/payouts.py`. Four endpoints, all POST, all keyed on a
   deployment plus one calendar month:

       POST /payouts/preview                  -> PayoutPreview
       POST /payouts/validate                 -> PayoutValidation
       POST /payouts/remuneration-sheet.xlsx  -> .xlsx + verdict headers
       POST /payouts/invoice-sheet.xlsx       -> .xlsx + verdict headers

   EVERY AMOUNT IS A STRING AND STAYS ONE.
   `payouts.py` serialises each `Decimal` with `str()` precisely so no rupee
   reaches a client as a JSON float (CLAUDE.md R7). Typing them as `string` here
   is what makes that stick: TypeScript refuses `preview.breakdown.gross -
   preview.breakdown.tds` outright, so the browser cannot start doing the
   arithmetic that R2 puts in `services/remuneration/engine.py`. Nothing in this
   file or in PayoutsPage parses, adds, or rounds one; `fmtAmount` groups digits
   on the string and hands it back. The amount in words is likewise generated in
   Python and displayed verbatim — an Indian-numbering renderer written here
   would be a second implementation that could disagree with the invoice.

   MONEY GOES OUT AS A STRING TOO. `Money` on the request side runs
   `app.domain.money.money()` as a BeforeValidator and 422s on a JSON float, so
   `{"ta_da": 100.5}` is refused and `{"ta_da": "100.50"}` is accepted. The form
   fields below are the strings the user typed, sent unparsed.
   ============================================================================= */

// --- §7 vocabulary -----------------------------------------------------------

/** `app.domain.enums.ValidationSeverity`. */
export type ValidationSeverity = 'blocking' | 'warning'

/** `app.domain.enums.ValidationCode` — the thirteen §7 gates. */
export type ValidationCode =
  | 'work_order_missing'
  | 'work_order_period_mismatch'
  | 'zoho_account_missing'
  | 'pan_invalid'
  | 'bank_account_missing'
  | 'ifsc_invalid'
  | 'payable_days_exceed_month'
  | 'attendance_incomplete'
  | 'invoice_number_already_issued'
  | 'rate_mismatch_with_work_order'
  | 'net_pay_not_positive'
  | 'net_deviation_from_trailing_average'
  | 'reimbursement_with_zero_payable_days'

/**
 * Where the fix lives, per gate. The backend's `message` says what is wrong;
 * this says which screen or which person clears it, which is the part a Manager
 * staring at a blocked cycle actually needs.
 *
 * Presentation only. The gate list, its severities and `can_submit` all come
 * from `validators.py`; nothing here re-decides any of them, and a code this map
 * has not heard of still renders with its server-supplied message.
 */
const BANK_RAILS_NOTE =
  'The trainers table has no bank account or IFSC column yet, so this gate blocks every ' +
  'payout today. It is a schema gap, not a data-entry mistake — see app/api/payouts.py, ' +
  '“A KNOWN GAP, CARRIED RATHER THAN INVENTED”.'

export const VALIDATION_REMEDY: Partial<Record<ValidationCode, string>> = {
  work_order_missing: 'File the signed work order on the Work orders screen.',
  work_order_period_mismatch:
    'The payout month falls outside valid_from…valid_to on the signed work order. Extend or re-sign it.',
  zoho_account_missing: 'Add the trainer’s ZOHO account id on the Trainers screen.',
  pan_invalid:
    'The PAN is not ten characters. It is the trainer’s identity and it seeds the invoice number, so nothing can be issued until it is right.',
  bank_account_missing: BANK_RAILS_NOTE,
  ifsc_invalid: BANK_RAILS_NOTE,
  payable_days_exceed_month:
    'More payable days than the month has. Check the attendance marks for duplicates.',
  attendance_incomplete: 'Mark the missing days on the Attendance screen.',
  invoice_number_already_issued:
    'A sheet with this invoice number is already on file for this trainer and month.',
  rate_mismatch_with_work_order:
    'The rate used disagrees with the signed work order. The work order wins — clear the override, or re-sign the work order.',
  net_pay_not_positive: 'Net pay is zero or negative. Check the deductions and the payable days.',
  net_deviation_from_trailing_average:
    'More than 20% away from this trainer’s trailing three-month net. State why below.',
  reimbursement_with_zero_payable_days:
    'A reimbursement is claimed for a month with no payable days. State why below.',
}

/** The two gates that no amount of data entry can currently clear. */
export const BANK_RAIL_CODES: ValidationCode[] = ['bank_account_missing', 'ifsc_invalid']

// --- Responses ---------------------------------------------------------------

/** `ArtifactState`. Everything this API emits is DRAFT (R4). */
export type ArtifactState = 'DRAFT' | 'PENDING_APPROVAL' | 'APPROVED' | 'RELEASED'

export type RateSource = 'work_order' | 'request_override'

/** The §6 chain. Every field is a string amount except `days_in_month`. */
export interface PayoutBreakdown {
  rate: string
  rate_basis: RateBasis
  rate_source: RateSource
  payable_days: string
  days_in_month: number
  /**
   * DISPLAY ONLY on the per-month path. It is rate / days_in_month and it did
   * not enter the calculation of `earned` — multiplying it by `payable_days`
   * will NOT reproduce `earned`, and the screen says so next to it.
   */
  rate_per_day: string
  earned: string
  reimbursements: string
  gross: string
  tds_rate: string
  /** Levied on `earned`, never on `gross`. */
  tds: string
  deductions: string
  net_unrounded: string
  /** The only rounded figure in the chain (R6). */
  net: string
  net_in_words: string
}

export interface AttendanceSummary {
  program_type: ProgramType
  payable_days: string
  period_days: number
  marked: number
  unmarked: number
  is_complete: boolean
}

export interface PayoutPreview {
  deployment_id: string
  trainer_pan: string
  trainer_name: string
  college_name: string
  program_name: string
  period_start: string
  period_end: string
  payout_month: string
  /** Provisional, and null when the PAN cannot seed one. Nothing is reserved. */
  invoice_number: string | null
  artifact_state: ArtifactState
  attendance: AttendanceSummary
  breakdown: PayoutBreakdown
}

export interface ValidationIssue {
  code: ValidationCode
  severity: ValidationSeverity
  message: string
  detail: Record<string, string>
}

export interface ValidationReport {
  is_blocked: boolean
  /** No blocking issue AND a non-blank reason for every warning. */
  can_submit: boolean
  blocking: ValidationIssue[]
  warnings: ValidationIssue[]
  reasons_required: ValidationCode[]
  reasons_missing: ValidationCode[]
}

export interface PayoutValidation {
  preview: PayoutPreview
  report: ValidationReport
}

// --- Requests ----------------------------------------------------------------

/** The claim side of a payout: what the caller supplies, all as typed strings. */
export interface PayoutRequest {
  deployment_id: string
  period_start: string
  period_end: string
  rate?: string
  rate_basis?: RateBasis
  ta_da?: string
  accommodation?: string
  travel_reimbursement?: string
  deductions?: string
  tds_rate?: string
}

export type PayoutValidationRequest = PayoutRequest & {
  stated_reasons?: Partial<Record<ValidationCode, string>>
}

export type SheetRequest = PayoutRequest & { invoice_date?: string | null }

// --- Calls -------------------------------------------------------------------

/**
 * The figures alone, with no §7 verdict.
 *
 * The ops console does not call this: `/payouts/validate` returns the same
 * preview alongside the report, and `payouts.py` says why one response rather
 * than two — "two round trips can disagree if attendance is marked in between".
 * This is here for the TRAINER surface, which is the one persona allowed to
 * preview their own payout and refused the §7 report and both sheets. That lives
 * in `src/trainer/`, which this workstream does not own.
 */
export async function previewPayout(request: PayoutRequest): Promise<PayoutPreview> {
  const response = await apiPost('/payouts/preview', request)
  return (await response.json()) as PayoutPreview
}

export async function validatePayout(
  request: PayoutValidationRequest,
): Promise<PayoutValidation> {
  const response = await apiPost('/payouts/validate', request)
  return (await response.json()) as PayoutValidation
}

// --- Sheets ------------------------------------------------------------------

export type SheetKind = 'remuneration' | 'invoice'

const SHEET_PATH: Record<SheetKind, string> = {
  remuneration: '/payouts/remuneration-sheet.xlsx',
  invoice: '/payouts/invoice-sheet.xlsx',
}

export const SHEET_LABEL: Record<SheetKind, string> = {
  remuneration: 'Remuneration sheet',
  invoice: 'Invoice sheet',
}

/**
 * What a downloaded sheet turned out to be. Read off the response headers rather
 * than inferred from the last validate call, because the sheet in the user's
 * Downloads folder is the one the headers describe.
 */
export interface DownloadedSheet {
  kind: SheetKind
  filename: string
  artifactState: ArtifactState
  blocked: boolean
  blockingCodes: ValidationCode[]
  at: string
}

/**
 * Download one of the two legacy sheets and hand back what it is.
 *
 * The sheet generates EVEN WHEN §7 BLOCKS — `payouts.py` is explicit that
 * refusing would strand the feature while the bank-rail gap stands, and that a
 * blocked payout should produce a sheet that "looks obviously incomplete rather
 * than plausible". So the verdict travels in the headers and the filename
 * carries `_DRAFT`, and this function surfaces both so the screen can say, next
 * to the download, that what just landed cannot be released (R4).
 */
export async function downloadSheet(
  kind: SheetKind,
  request: SheetRequest,
): Promise<DownloadedSheet> {
  const response = await apiPost(SHEET_PATH[kind], request)
  const blob = await response.blob()

  const filename =
    filenameFromDisposition(response.headers.get('Content-Disposition')) ??
    `${kind}_DRAFT.xlsx`

  saveBlob(blob, filename)

  const codes = (response.headers.get('X-Validation-Blocking-Codes') ?? '')
    .split(',')
    .map((c) => c.trim())
    .filter(Boolean) as ValidationCode[]

  return {
    kind,
    filename,
    artifactState: (response.headers.get('X-Artifact-State') as ArtifactState) ?? 'DRAFT',
    // Absent header is treated as blocked. If the state cannot be read, the safe
    // reading is the one that stops somebody filing it (R4).
    blocked: (response.headers.get('X-Validation-Blocked') ?? 'true') !== 'false',
    blockingCodes: codes,
    at: new Date().toISOString(),
  }
}

function filenameFromDisposition(header: string | null): string | null {
  if (!header) return null
  const match = /filename="?([^";]+)"?/i.exec(header)
  return match ? match[1] : null
}

function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  URL.revokeObjectURL(url)
}

/** True when a failure is the commercials wall rather than a bad request. */
export function isForbidden(error: unknown): boolean {
  return error instanceof ApiError && error.status === 403
}
