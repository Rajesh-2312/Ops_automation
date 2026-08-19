import { ApiError, apiPost } from './api'
import { bounded, type Bounded } from './bounds'
import { supabase } from './supabase'
import type { DocumentCategory, ProgramType, RateBasis } from './types'

/* =============================================================================
   The R4 lifecycle, as the browser sees it.
   =============================================================================

   Mirrors `app/api/approvals.py`. Five endpoints, keyed on an artifact TYPE
   (which is the Postgres table name) and the row id in that table:

       POST /approvals/{type}/{id}/submit     DRAFT            -> PENDING_APPROVAL
       POST /approvals/{type}/{id}/approve    PENDING_APPROVAL -> APPROVED   (freezes + hashes)
       POST /approvals/{type}/{id}/reject     PENDING_APPROVAL -> DRAFT      (reason REQUIRED)
       POST /approvals/{type}/{id}/release    APPROVED         -> RELEASED
       GET  /approvals/{type}/{id}/versions   the whole history, oldest first

   APPROVE AND RELEASE ARE TWO CALLS AND THIS FILE OFFERS NO THIRD.
   There is no `approveAndRelease()` here, and there must never be one. R4:
   "Approval and release are separate actions with separate audit rows."
   Approval freezes the version and hashes its content; release is the separate
   act of letting it leave. A convenience wrapper in this file would erase that
   distinction for every caller at once — the API deliberately has no combined
   route, and neither does its client.

   THERE IS NO LIST ENDPOINT. `approvals.py` exposes nothing like
   `GET /approvals/pending`, so the queue is read straight from
   `artifact_versions` over PostgREST with the user's own JWT. Migration 1300
   defines two policies on that table and they do the scoping: a remuneration
   artifact requires `can_see_commercials()`, everything else requires
   `is_internal()`, and both require `can_reach_artifact()`. An LDE Executive
   therefore gets ZERO ROWS for a payout from the database itself (R5) — this
   file adds no filter of its own and must not, because a client-side filter
   would be a second, weaker wall.

   MONEY IS READ AS TEXT. Every amount below is typed `string` and is selected
   with a `::text` cast, because PostgREST serialises `numeric` as a JSON float
   and a payout that has been through a double is not the payout that was
   computed (R7). Nothing here parses, adds or rounds one.
   ============================================================================= */

// --- Vocabulary ---------------------------------------------------------------

/** `app.domain.enums.ArtifactType`. The values ARE the table names. */
export type ArtifactType = 'remuneration_sheets' | 'governance_reports' | 'program_documents'

/** `app.domain.enums.ArtifactState` — the R4 lifecycle, in order. */
export type ArtifactState = 'DRAFT' | 'PENDING_APPROVAL' | 'APPROVED' | 'RELEASED'

export const ARTIFACT_TYPES: ArtifactType[] = [
  'remuneration_sheets',
  'governance_reports',
  'program_documents',
]

export const ARTIFACT_TYPE_LABEL: Record<ArtifactType, string> = {
  remuneration_sheets: 'Remuneration sheet',
  governance_reports: 'Governance report',
  program_documents: 'Program document',
}

export const ARTIFACT_STATE_LABEL: Record<ArtifactState, string> = {
  DRAFT: 'Draft',
  PENDING_APPROVAL: 'Pending approval',
  APPROVED: 'Approved — not released',
  RELEASED: 'Released',
}

/**
 * What each state means in one sentence, in the words R4 uses.
 *
 * APPROVED is the one worth being pedantic about: it is *releasable*, which is
 * not the same as released. Nothing has left the system.
 */
export const ARTIFACT_STATE_BLURB: Record<ArtifactState, string> = {
  DRAFT: 'Being worked on. Not in front of an approver yet — submitting it is a deliberate act.',
  PENDING_APPROVAL:
    'In front of an approver. It can be approved, or sent back to draft with a stated reason.',
  APPROVED:
    'Frozen and hashed. Nothing has been sent: releasing it is a SECOND, separate action with its own audit row (R4).',
  RELEASED: 'Released. Terminal — a change from here is a new version starting again at draft.',
}

/**
 * Who may approve each artifact type, mirroring `APPROVAL_AUTHORITY` in
 * `app/domain/enums.py`.
 *
 * `null` does NOT mean "nobody" and it does not mean "you are not allowed". It
 * means the organisation has not decided — CLAUDE.md §14 Q3, still open. The API
 * answers 501 Not Implemented for those two types, and the screen renders that
 * as the open question it is rather than as a failure. Do not "fix" this by
 * filling in a guess; fix it by answering Q3.
 */
export const APPROVAL_AUTHORITY: Record<ArtifactType, string | null> = {
  remuneration_sheets: 'Senior Manager',
  governance_reports: null,
  program_documents: null,
}

/** §14 Q3, in the words that belong on a screen. */
export const AUTHORITY_UNDECIDED_NOTE =
  'Nobody has decided who signs this off. CLAUDE.md §14 Q3 — “Approval authority for ' +
  'college-facing comms: Manager or Senior Manager?” — is an open question, so ' +
  'APPROVAL_AUTHORITY names an approver for remuneration sheets only. The API answers ' +
  '501 Not Implemented rather than 403 for the other two types, and the difference is the ' +
  'whole point: you are not forbidden, the system has no answer yet. This is not a bug and ' +
  'no permission grant will clear it — it clears when the question is answered.'

// --- Wire shapes --------------------------------------------------------------

/** One `artifact_versions` row, as `ArtifactVersionOut` in approvals.py. */
export interface ArtifactVersion {
  id: string
  artifact_type: ArtifactType
  artifact_id: string
  version: number
  state: ArtifactState
  /** Frozen at approval (R4). NULL before it. */
  content_hash: string | null
  is_current: boolean
  created_by: string | null
  created_at: string
  submitted_by: string | null
  submitted_at: string | null
  approved_by: string | null
  approved_at: string | null
  released_by: string | null
  released_at: string | null
  superseded_at: string | null
  notes: string | null
}

export interface VersionHistory {
  artifact_type: ArtifactType
  artifact_id: string
  versions: ArtifactVersion[]
}

// --- The five calls -----------------------------------------------------------

const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/+$/, '')

function path(type: ArtifactType, id: string, action: string): string {
  return `/approvals/${type}/${id}/${action}`
}

/** DRAFT -> PENDING_APPROVAL. Opens version 1 if the artifact has none. */
export async function submitArtifact(
  type: ArtifactType,
  id: string,
): Promise<ArtifactVersion> {
  const response = await apiPost(path(type, id, 'submit'))
  return (await response.json()) as ArtifactVersion
}

/**
 * PENDING_APPROVAL -> APPROVED. Freezes the version and hashes its content.
 *
 * AND STOPS. This does not send, queue, or release anything. See `releaseArtifact`
 * — and note that it is a different function, called from a different button,
 * after this one has already returned.
 */
export async function approveArtifact(
  type: ArtifactType,
  id: string,
): Promise<ArtifactVersion> {
  const response = await apiPost(path(type, id, 'approve'))
  return (await response.json()) as ArtifactVersion
}

/**
 * PENDING_APPROVAL -> DRAFT, with a reason.
 *
 * The reason is required by the API (`min_length=1`, 422 without it) and by the
 * state machine behind it. It is trimmed here so a string of spaces cannot pass
 * for one — the caller is expected to have refused to enable the button as well,
 * because a required field that anything satisfies is a formality.
 */
export async function rejectArtifact(
  type: ArtifactType,
  id: string,
  reason: string,
): Promise<ArtifactVersion> {
  const trimmed = reason.trim()
  if (trimmed === '') {
    throw new ApiError('A rejection needs a reason. The API refuses a blank one.', 422)
  }
  const response = await apiPost(path(type, id, 'reject'), { reason: trimmed })
  return (await response.json()) as ArtifactVersion
}

/**
 * APPROVED -> RELEASED. The second human act, with its own audit row.
 *
 * The server re-reads the artifact's content and re-checks it against the hash
 * frozen at approval. If the source row changed in between, this 409s — see
 * `isFrozenContentMismatch`. `notes` is commentary about the version and is not
 * part of the freeze; it cannot alter what was approved.
 */
export async function releaseArtifact(
  type: ArtifactType,
  id: string,
  notes?: string,
): Promise<ArtifactVersion> {
  const trimmed = (notes ?? '').trim()
  const response = await apiPost(path(type, id, 'release'), {
    notes: trimmed === '' ? null : trimmed,
  })
  return (await response.json()) as ArtifactVersion
}

/**
 * The whole history of one artifact, oldest first.
 *
 * A GET, and `apiPost` only posts, so the fetch is written out here with the
 * same bearer token and the same error unwrapping. It is the narrative the
 * approver reads: drafted, submitted, rejected, resubmitted, approved, released.
 */
export async function fetchVersionHistory(
  type: ArtifactType,
  id: string,
): Promise<VersionHistory> {
  const { data } = await supabase.auth.getSession()
  const token = data.session?.access_token
  if (!token) throw new ApiError('Not signed in.', 401)

  let response: Response
  try {
    response = await fetch(`${API_BASE_URL}${path(type, id, 'versions')}`, {
      headers: { Authorization: `Bearer ${token}` },
    })
  } catch {
    throw new ApiError(
      `Could not reach the API at ${API_BASE_URL || 'this origin'}. ` +
        'Check VITE_API_BASE_URL and that the FastAPI service is running.',
      0,
    )
  }

  if (!response.ok) {
    const body = await response.text().catch(() => '')
    let detail = body
    try {
      const parsed = JSON.parse(body) as { detail?: unknown }
      if (typeof parsed.detail === 'string') detail = parsed.detail
    } catch {
      /* not JSON; use it verbatim */
    }
    throw new ApiError(detail || `${response.status} ${response.statusText}`, response.status)
  }

  return (await response.json()) as VersionHistory
}

// --- Reading what a refusal actually was --------------------------------------

function status(error: unknown): number | null {
  return error instanceof ApiError ? error.status : null
}

/**
 * 501 — the approval authority for this artifact type is undecided (§14 Q3).
 *
 * NOT a permission problem and not an outage. `approvals.py` picked 501 over 403
 * deliberately, "a 403 would send a Senior Manager looking for a permission that
 * does not exist", and the screen has to preserve that distinction or the whole
 * signal is lost in a generic red box.
 */
export function isAuthorityUndefined(error: unknown): boolean {
  return status(error) === 501
}

/** 403 — the wall or the authority table refused this persona. */
export function isForbidden(error: unknown): boolean {
  return status(error) === 403
}

/**
 * 409 — either an illegal transition, or the frozen-hash check failing at
 * release because the artifact's content changed after it was approved.
 *
 * The second is an integrity signal, not a glitch: somebody edited a row that
 * had already been signed off, and the release refused to carry somebody else's
 * signature over new content. The two are told apart on the server's message,
 * which is why it is shown verbatim rather than summarised.
 */
export function isConflict(error: unknown): boolean {
  return status(error) === 409
}

/** True when a 409 is specifically the freeze catching a changed artifact. */
export function isFrozenContentMismatch(error: unknown): boolean {
  if (!isConflict(error)) return false
  const message = error instanceof Error ? error.message.toLowerCase() : ''
  return (
    message.includes('hash') || message.includes('frozen') || message.includes('content')
  )
}

// --- The queue, read from Postgres --------------------------------------------
// There is no GET /approvals/pending. See the header.

/** `artifact_versions` as PostgREST returns it — the same columns, unwrapped. */
export type ArtifactVersionRow = Omit<ArtifactVersion, 'is_current'>

// One literal string, not a concatenation: supabase-js infers the row shape from
// the select as a TEMPLATE LITERAL type, and a `string`-typed constant collapses
// that inference to GenericStringError. Same reason the two below are one line.
const VERSION_COLUMNS =
  'id, artifact_type, artifact_id, version, state, content_hash, created_by, created_at, submitted_by, submitted_at, approved_by, approved_at, released_by, released_at, superseded_at, notes' as const

/**
 * Every CURRENT version the signed-in user can reach — the approval queue.
 *
 * `superseded_at is null` is 1300's definition of current, and there is exactly
 * one such row per artifact (a partial unique index enforces it). No state filter
 * is applied: an approver needs to see what is waiting on them AND what they have
 * already approved but not released, and the second of those is the pile R4
 * creates on purpose.
 *
 * BOUNDED. There is one current version per artifact and no state filter, so
 * this list grows with every artifact the org has ever drafted and never
 * shrinks — RELEASED rows stay current forever. The `submitted_at` ordering
 * puts what is waiting on an approver at the front of the bound, which is the
 * half that must not be cut; the tail behind it is released history.
 */
export function fetchArtifactQueue(limit: number): Promise<Bounded<ArtifactVersionRow>> {
  return bounded<ArtifactVersionRow>(limit, (rows) =>
    supabase
      .from('artifact_versions')
      .select(VERSION_COLUMNS)
      .is('superseded_at', null)
      .order('submitted_at', { ascending: true, nullsFirst: false })
      .order('created_at', { ascending: false })
      .limit(rows),
  )
}

// --- The artifacts themselves -------------------------------------------------
// `artifact_versions` deliberately holds no content (1300), and the versions
// endpoint deliberately does not echo the payload. So the thing being approved is
// read from its system of record — which is also where its own RLS lives.

/**
 * A remuneration sheet, as §6 computes it. EVERY AMOUNT IS A STRING.
 *
 * Selected with `::text` casts so full Decimal precision survives PostgREST,
 * which would otherwise hand JavaScript `80000.0` for a `numeric(14,2)` (R7).
 */
export interface RemunerationArtifact {
  id: string
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
  invoice_no: string | null
  invoice_pan: string | null
  payout_status: string
  trainers: { full_name: string | null; pan: string | null } | null
  programs: {
    name: string | null
    type: ProgramType | null
    colleges: { name: string | null } | null
  } | null
}

const REMUNERATION_COLUMNS =
  'id, period_start, period_end, rate:rate::text, rate_basis, ' +
  'payable_days:payable_days::text, days_in_month, earned:earned::text, ta_da:ta_da::text, ' +
  'accommodation:accommodation::text, travel_reimb:travel_reimb::text, gross:gross::text, ' +
  'tds_rate:tds_rate::text, tds:tds::text, deductions:deductions::text, ' +
  'net_amount:net_amount::text, amount_in_words, currency, invoice_no, invoice_pan, ' +
  'payout_status, trainers(full_name, pan), programs(name, type, colleges(name))'

/**
 * `unwrap` coalesces a null `data` to `[]`, which is right for a list query and
 * wrong for `maybeSingle()` — an artifact the wall hid would arrive as a truthy
 * empty object and render as a blank card instead of as "not visible to you".
 * So the single-row reads throw on error and preserve the null.
 */
async function one<T>(
  builder: PromiseLike<{ data: T | null; error: unknown }>,
): Promise<T | null> {
  const { data, error } = await builder
  if (error) throw error
  return data
}

export function fetchRemunerationArtifact(id: string): Promise<RemunerationArtifact | null> {
  return one<RemunerationArtifact>(
    supabase.from('remuneration_sheets').select(REMUNERATION_COLUMNS).eq('id', id).maybeSingle(),
  )
}

export interface GovernanceArtifact {
  id: string
  title: string | null
  url: string
  reporting_period_start: string | null
  reporting_period_end: string | null
  shared_with_college_at: string | null
  programs: { name: string | null; colleges: { name: string | null } | null } | null
}

export function fetchGovernanceArtifact(id: string): Promise<GovernanceArtifact | null> {
  return one<GovernanceArtifact>(
    supabase
      .from('governance_reports')
      .select(
        'id, title, url, reporting_period_start, reporting_period_end, ' +
          'shared_with_college_at, programs(name, colleges(name))',
      )
      .eq('id', id)
      .maybeSingle(),
  )
}

export interface DocumentArtifact {
  id: string
  name: string
  category: DocumentCategory
  status: string
  url: string | null
  due_date: string | null
  filed_at: string | null
  programs: { name: string | null; colleges: { name: string | null } | null } | null
}

export function fetchDocumentArtifact(id: string): Promise<DocumentArtifact | null> {
  return one<DocumentArtifact>(
    supabase
      .from('program_documents')
      .select(
        'id, name, category, status, url, due_date, filed_at, programs(name, colleges(name))',
      )
      .eq('id', id)
      .maybeSingle(),
  )
}

/**
 * Names for the uuids on a version row.
 *
 * `artifact_versions.submitted_by` and friends carry no foreign key — 1300 is
 * explicit that they are historical record rather than live references, so
 * PostgREST cannot embed the profile and the lookup is a second query. Any name
 * that cannot be resolved renders as the raw uuid rather than as "Unknown": the
 * id is the thing on the audit row, and it is what a dispute is answered with.
 *
 * BOUNDED, and the fallback above is what makes the bound safe to apply: an
 * actor beyond the limit renders as their uuid, which is legible, traceable and
 * exactly what an unbounded miss already produced. `.order('full_name')` is not
 * cosmetic — a limit over an unordered read takes an arbitrary slice, so
 * repeating the same query could return a different set of names.
 */
export function fetchActorNames(
  limit: number,
): Promise<Bounded<{ id: string; full_name: string | null }>> {
  return bounded<{ id: string; full_name: string | null }>(limit, (rows) =>
    supabase.from('profiles').select('id, full_name').order('full_name').limit(rows),
  )
}

// --- Query keys ---------------------------------------------------------------
// Defined here rather than in lib/queryKeys.ts: this workstream owns the
// approvals surface end to end, and the keys move with the client that uses them.

export const approvalKeys = {
  all: ['approvals'] as const,
  /** `limit` is required and keyed for the reason on `commsKeys.queue`. */
  queue: (limit: number) => ['approvals', 'queue', limit] as const,
  actors: (limit: number) => ['approvals', 'actors', limit] as const,
  history: (type: ArtifactType, id: string) => ['approvals', 'history', type, id] as const,
  artifact: (type: ArtifactType, id: string) => ['approvals', 'artifact', type, id] as const,
}
