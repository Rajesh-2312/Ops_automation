import { ApiError, apiGet, apiPost, describeErrorBody, unreachableMessage } from './api'
import { supabase } from './supabase'

/* =============================================================================
   The single outbound queue, as the browser sees it. CLAUDE.md §8.
   =============================================================================

   Mirrors `app/api/comms.py`. Nine endpoints, all under `/comms/messages`:

       POST   /comms/messages                 draft into the queue        (201)
       GET    /comms/messages?program_id=…    the queue, program-scoped
       GET    /comms/messages/{id}            one message, with its diff
       PATCH  /comms/messages/{id}            amend a DRAFT's body
       POST   /comms/messages/{id}/submit     DRAFT -> PENDING_APPROVAL
       POST   /comms/messages/{id}/approve    PENDING_APPROVAL -> APPROVED   (501)
       POST   /comms/messages/{id}/reject     PENDING_APPROVAL -> DRAFT      (501)
       POST   /comms/messages/{id}/release    APPROVED -> RELEASED           (501)
       POST   /comms/messages/{id}/supersede  a new DRAFT replacing a frozen one

   NOTHING IN THIS FILE TRANSMITS ANYTHING, AND NOTHING MAY BE ADDED THAT DOES.
   There is no `sendMessage()`. `releaseMessage()` is not a send: the router it
   calls has no SMTP client, no Twilio, no SendGrid, no `sent_at` column, and its
   own summary line reads "Marks state; transmits nothing." RELEASED means an
   authenticated human cleared the message, which makes it *eligible* for a
   transport that does not exist yet. A helper here that posted a message
   somewhere would be the second path out of the queue that §8's word "single"
   exists to forbid.

   APPROVE, REJECT AND RELEASE ANSWER 501 TODAY, AND THAT IS THE DESIGN.
   `app/services/comms/authority.py` holds an EMPTY `COMMS_APPROVAL_AUTHORITY`
   because CLAUDE.md §14 Q3 — "Approval authority for college-facing comms:
   Manager or Senior Manager?" — has not been answered by the owner. §14's
   instruction is one sentence: "Carry these; do not invent answers." So the three
   calls below are written in full, they are wired to real buttons, and they come
   back 501 Not Implemented carrying the server's own message, which names the
   question. 501 and not 403 is load-bearing: the caller is not forbidden, the
   organisation has no answer yet, and a 403 would send a Senior Manager hunting
   for a permission that does not exist. `isAuthorityUndefined()` below is what
   keeps that distinction alive on screen instead of losing it in a red box.

   R3. Every call here goes out with the signed-in human's Supabase JWT and
   `app/api/comms.py` resolves a `CurrentPrincipal` from it. No agent reaches any
   of this; an agent drafts by producing the fields a human then posts.

   R1. `template_values` is the structured input, and the server renders it into
   the template to produce the baseline the diff is taken against. Nothing in this
   file renders a template, computes a diff, or formats an amount — the diff is
   read from the row (see `parseDiff`), never recomputed in the browser, because a
   second implementation would eventually disagree with the one that was frozen at
   approval and the approver's record would stop being recoverable.
   ============================================================================= */

// --- Vocabulary ---------------------------------------------------------------

/** `app.domain.enums.ArtifactState` — the R4 ladder, in order. */
export type ArtifactState = 'DRAFT' | 'PENDING_APPROVAL' | 'APPROVED' | 'RELEASED'

/** `app.services.comms.types.CommsChannel`. Three labels, none of which sends. */
export type CommsChannel = 'email' | 'whatsapp' | 'platform_ticket'

/** `app.services.comms.types.CommsRecipientKind` — a CLASS, never an address. */
export type CommsRecipientKind = 'internal_staff' | 'trainer' | 'college'

/** `app.domain.enums.ArtifactType`. The values ARE the table names. */
export type ArtifactType = 'remuneration_sheets' | 'governance_reports' | 'program_documents'

export const COMMS_STATES: ArtifactState[] = [
  'DRAFT',
  'PENDING_APPROVAL',
  'APPROVED',
  'RELEASED',
]

export const COMMS_CHANNELS: CommsChannel[] = ['email', 'whatsapp', 'platform_ticket']

export const COMMS_RECIPIENT_KINDS: CommsRecipientKind[] = [
  'internal_staff',
  'trainer',
  'college',
]

export const CHANNEL_LABEL: Record<CommsChannel, string> = {
  email: 'Email',
  whatsapp: 'WhatsApp',
  platform_ticket: 'Platform ticket',
}

export const RECIPIENT_KIND_LABEL: Record<CommsRecipientKind, string> = {
  internal_staff: 'Internal staff',
  trainer: 'Trainer',
  college: 'College',
}

/**
 * The §8 autonomy ceiling each recipient class carries.
 *
 * "Nothing touching money, contracts, or a college contact goes past level 3.
 *  Internal chase messages and platform tickets may reach level 4 only after a
 *  demonstrated track record."
 *
 * A trainer is a contracted counterparty, so a message to one is
 * contract-adjacent and takes the level-3 ceiling even though it is not a college
 * contact — which is exactly why `CommsRecipientKind` is its own field and not
 * inferred from the address.
 */
export const RECIPIENT_KIND_CEILING: Record<CommsRecipientKind, string> = {
  internal_staff:
    'Internal only. §8 allows level 4 for internal chase and platform tickets — after a demonstrated track record, and not today.',
  trainer:
    'A contracted counterparty. Contract-adjacent, so §8 caps this at level 3: act only with a human approval.',
  college:
    'A college contact. §8 caps this at level 3 and this is the case §14 Q3 is literally about.',
}

export const COMMS_STATE_LABEL: Record<ArtifactState, string> = {
  DRAFT: 'Draft',
  PENDING_APPROVAL: 'Pending approval',
  APPROVED: 'Approved — not released',
  RELEASED: 'Released — still not sent',
}

/**
 * What each state means for a queued message, in one sentence.
 *
 * The last two are worded against the misreading that matters: no provider is
 * wired anywhere in this phase, so neither APPROVED nor RELEASED means anybody
 * received anything.
 */
export const COMMS_STATE_BLURB: Record<ArtifactState, string> = {
  DRAFT:
    'Being written. Amendable in place, and the diff is recomputed by the server on every amend.',
  PENDING_APPROVAL:
    'In front of an approver — and today it stops here, because nobody has been given authority to approve it (§14 Q3).',
  APPROVED:
    'Frozen and hashed. Not sent, and not even releasable without a second, separate human act (R4).',
  RELEASED:
    'A human cleared it to go. Still not sent: no provider exists in this phase, and there is deliberately no sent_at column.',
}

/**
 * §14 Q3, in the words that belong on a screen.
 *
 * Kept verbatim next to every blocked control rather than summarised per button,
 * so the same sentence appears wherever the block is met.
 */
export const COMMS_AUTHORITY_UNDECIDED_NOTE =
  'Nobody has been given authority to approve an outbound message. CLAUDE.md §14 Q3 — ' +
  '“Approval authority for college-facing comms: Manager or Senior Manager?” — is an open ' +
  'question owned by Rajesh Maroju, so COMMS_APPROVAL_AUTHORITY in ' +
  'app/services/comms/authority.py is deliberately empty and approve, reject and release all ' +
  'answer 501 Not Implemented. This is not a permission problem, not an outage and not an ' +
  'unfinished feature: drafting, amending and submitting work, and the queue is meant to fill ' +
  'and stop (R4). No permission grant will clear it. It clears when the question is answered ' +
  'and that answer is recorded — with the migration 1700_comms_queue.sql describes.'

/** Said wherever a control could be mistaken for a send. */
export const RELEASE_IS_NOT_SEND_NOTE =
  'Releasing is not sending. app/api/comms.py has no provider behind it — no SMTP, no Twilio, ' +
  'no SendGrid, no outbound HTTP of any kind — and no sent_at column to stamp. Release marks ' +
  'state and writes an audit row, which makes the message eligible for a transport that will ' +
  'be built later and will read rows in RELEASED. Nothing on this screen transmits anything.'

// --- Wire shapes --------------------------------------------------------------

export type JsonValue =
  | string
  | number
  | boolean
  | null
  | JsonValue[]
  | { [key: string]: JsonValue }

/** One `comms_messages` row, as `CommsMessageOut` in comms.py. */
export interface CommsMessage {
  id: string
  program_id: string
  channel: CommsChannel
  recipient_kind: CommsRecipientKind
  recipient_ref: string
  recipient_name: string | null

  template_key: string
  /** The BASELINE: the template with `template_values` already substituted. */
  template_body: string
  template_values: Record<string, JsonValue>
  subject: string | null
  body: string
  /** The §8 review surface, stored on the row. Read with `parseDiff`. */
  diff: Record<string, JsonValue>

  is_commercial: boolean
  related_artifact_type: ArtifactType | null
  related_artifact_id: string | null

  state: ArtifactState
  /** Frozen at approval (R4). NULL before it. */
  content_hash: string | null
  version: number
  supersedes_id: string | null
  superseded_at: string | null

  created_by: string | null
  created_at: string
  submitted_by: string | null
  submitted_at: string | null
  approved_by: string | null
  approved_at: string | null
  /** When a human cleared it. NOT a send timestamp — there is no send. */
  released_by: string | null
  released_at: string | null
  notes: string | null
}

export interface CommsQueue {
  messages: CommsMessage[]
}

/** `DraftRequest` in comms.py. `extra="forbid"`, so no field may be invented. */
export interface DraftRequest {
  program_id: string
  channel: CommsChannel
  recipient_kind: CommsRecipientKind
  recipient_ref: string
  recipient_name?: string | null
  template_key: string
  /** RAW template text, with `{{slot}}` markers. The server renders it. */
  template: string
  template_values: Record<string, JsonValue>
  subject?: string | null
  body: string
  is_commercial?: boolean
  related_artifact_type?: ArtifactType | null
  related_artifact_id?: string | null
}

// --- The diff, read rather than recomputed ------------------------------------

/** `app.services.comms.diff.DiffOp`. */
export type DiffOp = 'added' | 'removed' | 'changed'

/** One divergence between the rendered template and the queued message. */
export interface Hunk {
  op: DiffOp
  /** Line index in the BASELINE, so hunks sort into template order. */
  at: number
  template: string[]
  message: string[]
}

/** `TemplateDiff.as_json()` — the stored `comms_messages.diff` payload. */
export interface TemplateDiff {
  version: number
  identical: boolean
  lines_added: number
  lines_removed: number
  template_lines: number
  message_lines: number
  hunks: Hunk[]
}

/** The diff shape this client knows how to draw. `DIFF_VERSION` in diff.py. */
export const SUPPORTED_DIFF_VERSION = 1

const DIFF_OPS: DiffOp[] = ['added', 'removed', 'changed']

function asStringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((line): line is string => typeof line === 'string') : []
}

/**
 * Read the stored diff object into something drawable, or `null`.
 *
 * The column is typed `dict[str, JsonValue]` on the wire, which is honest — it
 * outlives the code that wrote it, and `DIFF_VERSION` is stamped into every row
 * precisely so a consumer can tell one algorithm's output from another's rather
 * than silently misreading it. So this validates instead of casting: a row this
 * client does not understand returns `null` and the screen says so, which is the
 * only safe failure for a review surface. Rendering an unrecognised blob as
 * though it were the diff would be worse than rendering nothing.
 *
 * `version` is returned rather than checked here, so the caller can draw a
 * recognisable-but-newer diff with a warning rather than refusing it outright.
 */
export function parseDiff(value: unknown): TemplateDiff | null {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return null
  const raw = value as Record<string, unknown>
  if (typeof raw.version !== 'number' || typeof raw.identical !== 'boolean') return null
  if (!Array.isArray(raw.hunks)) return null

  const hunks: Hunk[] = []
  for (const entry of raw.hunks) {
    if (entry === null || typeof entry !== 'object' || Array.isArray(entry)) return null
    const h = entry as Record<string, unknown>
    const op = h.op
    if (typeof op !== 'string' || !DIFF_OPS.includes(op as DiffOp)) return null
    if (typeof h.at !== 'number') return null
    hunks.push({
      op: op as DiffOp,
      at: h.at,
      template: asStringArray(h.template),
      message: asStringArray(h.message),
    })
  }

  const count = (key: string): number => (typeof raw[key] === 'number' ? (raw[key] as number) : 0)

  return {
    version: raw.version,
    identical: raw.identical,
    lines_added: count('lines_added'),
    lines_removed: count('lines_removed'),
    template_lines: count('template_lines'),
    message_lines: count('message_lines'),
    hunks: [...hunks].sort((a, b) => a.at - b.at),
  }
}

/** Total lines an approver has to read. A rewritten line is two, not one. */
export function linesChanged(diff: TemplateDiff): number {
  return diff.lines_added + diff.lines_removed
}

/**
 * One block of the review view: unchanged baseline text, elided baseline text,
 * or a hunk.
 */
export type DiffBlock =
  | { kind: 'context'; at: number; lines: string[] }
  | { kind: 'elided'; hidden: number }
  | { kind: 'hunk'; hunk: Hunk }

/**
 * Interleave the stored hunks with the baseline they came from.
 *
 * `Hunk.at` is an index into the RENDERED TEMPLATE — diff.py says so, and says
 * why: "hunks sort into template order and a UI can interleave them with the
 * unchanged text without a second pass". This is that pass. Nothing here diffs
 * anything; it walks a cursor down `template_body` and drops the server's hunks
 * in at their recorded positions, so what is drawn is the stored diff and not a
 * browser's opinion of one.
 *
 * Unchanged runs longer than `2 * context` are elided in the middle, the way a
 * unified diff does, because an approver is being asked to review what CHANGED
 * and a screen of identical boilerplate is what turns a review into a scroll.
 */
export function buildDiffView(
  templateBody: string,
  diff: TemplateDiff,
  context = 2,
): DiffBlock[] {
  // `_lines` in diff.py rstrips each line before comparing, so the same is done
  // here — otherwise a trailing space makes a context line render differently
  // from the hunk line it sits beside.
  const baseline = templateBody.split('\n').map((line) => line.replace(/\s+$/, ''))
  const blocks: DiffBlock[] = []

  /**
   * `keepHead` keeps the first `context` lines of the run — the text immediately
   * after the previous hunk. `keepTail` keeps the last `context` lines — the text
   * immediately before the next one. Whatever is left in the middle is elided,
   * with its count, so nothing is hidden silently.
   */
  const pushGap = (from: number, to: number, keepHead: boolean, keepTail: boolean): void => {
    const lines = baseline.slice(from, to)
    if (lines.length === 0) return
    const head = keepHead ? context : 0
    const tail = keepTail ? context : 0
    if (lines.length <= head + tail) {
      blocks.push({ kind: 'context', at: from, lines })
      return
    }
    if (head > 0) blocks.push({ kind: 'context', at: from, lines: lines.slice(0, head) })
    blocks.push({ kind: 'elided', hidden: lines.length - head - tail })
    if (tail > 0) blocks.push({ kind: 'context', at: to - tail, lines: lines.slice(-tail) })
  }

  let cursor = 0
  for (const hunk of diff.hunks) {
    const at = Math.max(cursor, Math.min(hunk.at, baseline.length))
    pushGap(cursor, at, true, true)
    blocks.push({ kind: 'hunk', hunk })
    cursor = at + hunk.template.length
  }
  // Nothing follows the last hunk, so only the lines just after it are kept.
  pushGap(cursor, baseline.length, true, false)

  return blocks
}

/**
 * The `{{slot}}` names a raw template asks for.
 *
 * `PLACEHOLDER_RE` in diff.py, kept in step deliberately: this is used only to
 * tell a drafter which values are still missing BEFORE they post, so the server's
 * `TemplateRenderError` is a backstop rather than the first they hear of it. It
 * does not render, and nothing on this screen substitutes a value — the baseline
 * is the server's output, always.
 */
export function templateSlots(template: string): string[] {
  const re = /\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}/g
  const found = new Set<string>()
  let match = re.exec(template)
  while (match !== null) {
    found.add(match[1])
    match = re.exec(template)
  }
  return [...found].sort()
}

// --- PATCH ---------------------------------------------------------------------

const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/+$/, '')

/**
 * PATCH as the signed-in user.
 *
 * `lib/api.ts` exports `apiGet` and `apiPost` and nothing else, and amending a
 * draft is the one verb on this surface that is neither. Written here rather than
 * added there because a shared client is a shared blast radius: this workstream
 * owns the comms surface and one method used by one endpoint belongs beside it.
 * The auth, the network-failure message and the `ApiError` unwrapping are
 * deliberately identical to `apiPost`'s, so a 403 from an amend reads exactly
 * like a 403 from a submit.
 */
async function apiPatch(path: string, body: unknown): Promise<Response> {
  const { data } = await supabase.auth.getSession()
  const token = data.session?.access_token
  if (!token) throw new ApiError('Not signed in.', 401)

  let response: Response
  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      method: 'PATCH',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify(body),
    })
  } catch {
    throw new ApiError(unreachableMessage(), 0)
  }

  if (!response.ok) {
    const text = await response.text().catch(() => '')
    // Was a second, drifting copy of `describeDetail` plus the same raw-body
    // fallback. One implementation, so a 422 reads identically wherever it
    // came from and an HTML body is diagnosed rather than pasted.
    throw new ApiError(
      describeErrorBody(text, response.status, response.statusText),
      response.status,
    )
  }

  return response
}

// --- The nine calls ------------------------------------------------------------

async function postJson<T>(path: string, body?: unknown): Promise<T> {
  const response = await apiPost(path, body)
  return (await response.json()) as T
}

/**
 * The queue for one program, newest first.
 *
 * `program_id` is REQUIRED by the API and is not an oversight: a cross-program
 * queue would have to apply reach row by row on a BYPASSRLS connection, so the
 * scope is stated by the caller and checked once before anything is read.
 * Commercial rows are filtered out server-side for a persona outside the wall
 * (R5) — this client adds no filter of its own, and must not, because a
 * client-side wall is a second and weaker one.
 */
export function fetchCommsQueue(
  programId: string,
  state: ArtifactState | null,
  limit: number,
): Promise<CommsQueue> {
  const params = new URLSearchParams({ program_id: programId, limit: String(limit) })
  if (state) params.set('state', state)
  return apiGet<CommsQueue>(`/comms/messages?${params.toString()}`)
}

/** One message with the diff an approver reads. */
export function fetchCommsMessage(id: string): Promise<CommsMessage> {
  return apiGet<CommsMessage>(`/comms/messages/${id}`)
}

/**
 * Draft a message into the queue. It lands in DRAFT and goes nowhere.
 *
 * No approval authority is checked and none should be — §8 puts "propose, human
 * edits and sends" at autonomy level 2, so drafting is exactly what happens
 * without an approver in the room. What IS checked is the wall and the reach.
 */
export function draftMessage(request: DraftRequest): Promise<CommsMessage> {
  return postJson<CommsMessage>('/comms/messages', request)
}

/**
 * Rewrite a DRAFT's body. Same version, still DRAFT, diff recomputed server-side.
 *
 * Refused with 409 for anything else, and rightly: a message in PENDING_APPROVAL
 * is in front of a human, and editing it underneath them means they approve one
 * text while another is stored.
 */
export async function amendMessage(id: string, body: string): Promise<CommsMessage> {
  const response = await apiPatch(`/comms/messages/${id}`, { body })
  return (await response.json()) as CommsMessage
}

/**
 * DRAFT -> PENDING_APPROVAL.
 *
 * The last call on this surface that currently succeeds. With §14 Q3 open the
 * queue fills to PENDING_APPROVAL and stops, which is R4 working rather than a
 * bug to route around.
 */
export function submitMessage(id: string): Promise<CommsMessage> {
  return postJson<CommsMessage>(`/comms/messages/${id}/submit`)
}

/**
 * PENDING_APPROVAL -> APPROVED. **501 today, for every message.**
 *
 * Written in full and wired to a real button on purpose. The endpoint exists, the
 * lifecycle behind it is implemented and tested, and the only missing thing is a
 * decision by the owner (§14 Q3). Hiding this call would describe the system as
 * unfinished; deleting the button would describe it as unbuilt. Both are false.
 * See `isAuthorityUndefined` for how the refusal is told apart from a 403.
 */
export function approveMessage(id: string): Promise<CommsMessage> {
  return postJson<CommsMessage>(`/comms/messages/${id}/approve`)
}

/**
 * PENDING_APPROVAL -> DRAFT, with a required reason. **501 today.**
 *
 * Same authority as approval and therefore the same refusal: the power to
 * withhold approval is the power to approve, and an actor who could not have
 * approved must not be able to block. The reason is trimmed here so a string of
 * spaces cannot pass for one — the API 422s on it independently.
 */
export function rejectMessage(id: string, reason: string): Promise<CommsMessage> {
  const trimmed = reason.trim()
  if (trimmed === '') {
    throw new ApiError('A rejection needs a reason. The API refuses a blank one.', 422)
  }
  return postJson<CommsMessage>(`/comms/messages/${id}/reject`, { reason: trimmed })
}

/**
 * APPROVED -> RELEASED. **501 today.** And it is not a send.
 *
 * Read `RELEASE_IS_NOT_SEND_NOTE` before wiring anything to this. The server
 * re-verifies the frozen hash before it would move the state, so a message whose
 * content changed after approval is refused with 409 rather than carrying
 * somebody else's signature over new content.
 */
export function releaseMessage(id: string, notes?: string): Promise<CommsMessage> {
  const trimmed = (notes ?? '').trim()
  return postJson<CommsMessage>(`/comms/messages/${id}/release`, {
    notes: trimmed === '' ? null : trimmed,
  })
}

/**
 * Replace a frozen message with a fresh DRAFT at `version + 1` (R4).
 *
 * Legal only from APPROVED or RELEASED — a DRAFT is amended, and a message
 * awaiting approval is rejected first. `template_values` is required by the API
 * whenever `template` is supplied: a baseline rendered from stale values would
 * make the successor's diff describe facts nobody re-fetched.
 *
 * Unreachable in practice today, because nothing can reach a frozen state while
 * §14 Q3 is open. It is written anyway so answering the question does not also
 * require writing this.
 */
export function supersedeMessage(
  id: string,
  body: string,
  template?: { template: string; template_values: Record<string, JsonValue> },
): Promise<CommsMessage> {
  return postJson<CommsMessage>(`/comms/messages/${id}/supersede`, {
    body,
    template: template?.template ?? null,
    template_values: template?.template_values ?? null,
  })
}

// --- Reading what a refusal actually was ---------------------------------------

function statusOf(error: unknown): number | null {
  return error instanceof ApiError ? error.status : null
}

/**
 * 501 — nobody has authority to approve, reject or release (§14 Q3).
 *
 * The single most important predicate in this file. `comms.py` chose 501 over 403
 * deliberately, and if the screen renders it as a generic failure the whole
 * signal is lost: the reader concludes they lack a permission, goes looking for
 * it, and finds a system that appears broken instead of one that is waiting on a
 * decision somebody in the room can make.
 */
export function isAuthorityUndefined(error: unknown): boolean {
  return statusOf(error) === 501
}

/** 403 — the commercials wall (R5) or a persona outside the authority set. */
export function isForbidden(error: unknown): boolean {
  return statusOf(error) === 403
}

/** 409 — an illegal transition, or the frozen-hash check failing at release. */
export function isConflict(error: unknown): boolean {
  return statusOf(error) === 409
}

/** True when a 409 is specifically the freeze catching changed content. */
export function isFrozenContentMismatch(error: unknown): boolean {
  if (!isConflict(error)) return false
  const message = error instanceof Error ? error.message.toLowerCase() : ''
  return message.includes('hash') || message.includes('frozen')
}

/** 422 — an unrenderable template, a float in `template_values` (R7), or a blank reason. */
export function isUnprocessable(error: unknown): boolean {
  return statusOf(error) === 422
}

/** 404 — no such message, or no such program. */
export function isNotFound(error: unknown): boolean {
  return statusOf(error) === 404
}

// --- Query keys ----------------------------------------------------------------
// Local to this file rather than in lib/queryKeys.ts, following lib/approvals.ts:
// this workstream owns the comms surface end to end and the keys move with the
// client that uses them.

export const commsKeys = {
  all: ['comms'] as const,
  /**
   * `limit` is REQUIRED and is in the key because `fetchCommsQueue` SENDS it.
   *
   * This key previously stopped at `state`, which is the same defect `erm.ts`
   * shipped and fixed: two queries differing only in their bound share one
   * cache entry, so the narrower one is served the wider one's rows. It was
   * latent here only because the single call site never passed a limit — a
   * bound waiting for its second caller. Both arguments are now required, so a
   * call site that sends a limit it did not key does not compile.
   */
  queue: (programId: string, state: ArtifactState | null, limit: number) =>
    ['comms', 'queue', programId, state ?? 'all', limit] as const,
  /**
   * The PREFIX of every queue key for one program — every state, every bound.
   *
   * Invalidation needs this because the key now ends in a page size: naming one
   * bound would leave a queue cached at another one stale, and reaching for
   * `all` instead would needlessly drop the actor-name cache too. TanStack
   * matches query keys by prefix, so this invalidates exactly the queues for
   * this program and nothing else.
   */
  queuesFor: (programId: string) => ['comms', 'queue', programId] as const,
  message: (id: string) => ['comms', 'message', id] as const,
  /** Names for the uuids on a row. Keyed here rather than reusing the approvals
   *  key so an invalidation on this screen cannot reach into that one, and
   *  carrying its own bound for the reason on `queue` above. */
  actors: (limit: number) => ['comms', 'actors', limit] as const,
}
