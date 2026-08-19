import { ApiError, apiGet, apiPost } from './api'

/* =============================================================================
   The ERM sync queue, as the browser sees it. CLAUDE.md §10.
   =============================================================================

   Mirrors `app/api/erm.py`. Six endpoints, all under `/erm/tasks`:

       POST   /erm/tasks                 file a card for a trainer or a program (201)
       GET    /erm/tasks                 the queue, filtered, oldest first
       GET    /erm/tasks/{id}            one card, its field pack, its drift
       POST   /erm/tasks/{id}/assign     give it to a named person
       POST   /erm/tasks/{id}/confirm    they pasted it — freeze the pack, stamp
       POST   /erm/tasks/{id}/cancel     withdraw an open card, with a reason

   NOTHING IN THIS FILE TRANSMITS ANYTHING, AND NOTHING MAY BE ADDED THAT DOES.
   ERM is external and has no API (§10), so there is no `pushToErm()`, no client
   for it, and no URL of it anywhere in this codebase. `confirmErmTask()` is not
   a send: it records that a named, signed-in human says they retyped a named set
   of values into a portal this system cannot reach. That is the strongest true
   statement available, and dressing it up as a synchronisation would be the lie
   the whole design exists to avoid. R3 holds here structurally — the code that
   would do the sending does not exist.

   THE FIELD ORDER IS A GUESS AND THIS FILE SAYS SO OUT LOUD.
   `app/services/erm/fieldpack.py` opens with the argument in full: nobody on this
   side of the integration has ever seen ERM's form, the only two ERM artefacts in
   the legacy folder are logs *of* the paste rather than the form being pasted
   into, so the order below is declared once, versioned, and flagged
   `field_order_verified: false` on every response. `FIELD_ORDER_UNVERIFIED_NOTE`
   is what that flag says on screen. It is carried to the person doing the paste
   because THEY are the only one who can confirm or correct it — they have ERM
   open in the next window and we do not.

   R1. Every value in a pack was read from a column of a system of record and
   passed into a pure builder as structured input. Nothing here renders, derives,
   computes or reformats a pack value: `paste_text` is the server's own
   serialisation, and `drift` is the server's own comparison of the frozen
   snapshot against the live pack. A browser that recomputed either would
   eventually disagree with the row that is standing as evidence of what somebody
   was shown.

   R5. No pack field is a rate, a bank rail or a P&L line — deliberately, because
   `erm_sync_tasks` is readable by an LDE Executive on the same terms `trainers`
   is, and a pack carrying a day rate would walk a commercial value straight
   around `can_see_commercials()`. PAN is in the trainer pack because §6 makes it
   identity, not because it is financial.
   ============================================================================= */

// --- Vocabulary ---------------------------------------------------------------

/** `app.services.erm.types.ErmSubjectKind` — the two ERM touchpoints evidenced. */
export type ErmSubjectKind = 'trainer' | 'program'

/** `app.services.erm.types.ErmSyncState`. Deliberately NOT `ArtifactState`. */
export type ErmSyncState = 'queued' | 'assigned' | 'confirmed' | 'stale' | 'cancelled'

export const ERM_SUBJECT_KINDS: ErmSubjectKind[] = ['trainer', 'program']

export const ERM_STATES: ErmSyncState[] = [
  'queued',
  'assigned',
  'confirmed',
  'stale',
  'cancelled',
]

/** `OPEN_STATES` in types.py — the states a card can still be worked from. */
export const ERM_OPEN_STATES: ErmSyncState[] = ['queued', 'assigned']

export const SUBJECT_KIND_LABEL: Record<ErmSubjectKind, string> = {
  trainer: 'Trainer',
  program: 'Program',
}

export const ERM_STATE_LABEL: Record<ErmSyncState, string> = {
  queued: 'Queued',
  assigned: 'Assigned',
  confirmed: 'Confirmed',
  stale: 'Stale — diverged',
  cancelled: 'Cancelled',
}

/**
 * What each state means for a job card, in one sentence.
 *
 * `stale` is the one worth reading twice, and its wording is taken from
 * `types.py`: it does not mean the push failed. It means the push HAPPENED,
 * correctly, and the local record has moved since — so what ERM holds is a
 * photograph of a thing that changed.
 */
export const ERM_STATE_BLURB: Record<ErmSyncState, string> = {
  queued:
    'Filed and waiting. Nobody owns it yet. The field pack is generated live, so it will show today’s values whenever it is opened.',
  assigned:
    'Owned by a named person (§10). They open it, paste the pack into ERM in another window, and come back to confirm.',
  confirmed:
    'A named human says they pasted these exact values on this date. The pack is frozen onto the card as the evidence of what they were shown.',
  stale:
    'This push happened and was correct. The local record has changed since, so ERM now holds a photograph of a thing that moved. A fresh card supersedes this one — the card keeps its evidence.',
  cancelled:
    'Withdrawn with a stated reason. That reason is what stops it reading as an oversight to whoever finds it in three months.',
}

/**
 * §10's warning, in the words that belong on a screen.
 *
 * Kept verbatim beside every stale card rather than paraphrased per panel, so
 * the same sentence appears wherever divergence is being reported.
 */
export const DRIFT_NOTE =
  'The local record changed after this sync, so byteXL and ERM now disagree. ' +
  'CLAUDE.md §10: “Without drift detection the two systems diverge within a month ' +
  'and neither is trusted.” The database made this call inside the transaction that ' +
  'did the editing — every connection, including the browser’s direct writes — and ' +
  'it already filed a replacement card. Work the new card; this one stays as the ' +
  'record of what was pasted before.'

/**
 * Why a stale card can legitimately show no drifted fields.
 *
 * `app/services/erm/drift.py` states this outright and the screen must not
 * quietly hide the case: a value edited and edited back, or a column watched by
 * the trigger but rendered identically, lands exactly here. The database is
 * conservative on purpose — it would rather requeue a push nobody needed than
 * skip one somebody did.
 */
export const DRIFT_WITHOUT_DIFFERENCES_NOTE =
  'Marked stale, but no field differences were found. That is a real state, not a ' +
  'glitch: a value edited and edited back still marks the record stale, and the ' +
  'detector is conservative on purpose — it would rather requeue a push nobody ' +
  'needed than skip one somebody did. Re-pasting is harmless; skipping is not.'

/**
 * The disclaimer that must reach the person doing the paste (§10, §14).
 *
 * It is on the wire as `field_order_verified` and it is on the screen because of
 * this constant. They are the only person who can settle it: they have ERM open
 * and this side of the integration never has.
 */
export const FIELD_ORDER_UNVERIFIED_NOTE =
  'The order of the fields below has never been checked against ERM’s actual form. ' +
  'Nobody on this side of the integration has seen it — the only ERM artefacts in the ' +
  'legacy folder are logs OF the paste, not the screen being pasted into — so the order ' +
  'is a documented guess, versioned so that every pack generated under it stays ' +
  'identifiable. Match each value to its LABEL in ERM, not to its position. If the real ' +
  'order differs, say so: reordering the two tuples in app/services/erm/fieldpack.py, ' +
  'bumping FIELD_ORDER_VERSION and flipping FIELD_ORDER_VERIFIED is a small change, and ' +
  'you are the only person who can make it possible.'

/** Said wherever a control could be mistaken for a push to ERM. */
export const NOTHING_TRANSMITS_NOTE =
  'Nothing on this screen sends anything to ERM. ERM is external with no API, and §10 ' +
  'forbids a scraper outright — there is no HTTP client, no browser driver and no ERM ' +
  'credential anywhere behind these buttons. Confirming records that YOU pasted these ' +
  'values in another window, stamps erm_synced_at / erm_synced_by on the record, and ' +
  'freezes the pack you were shown as the evidence.'

/** Why the pack travels as `label ⇥ value` and never as a bare column of values. */
export const PASTE_TEXT_NOTE =
  'Tab-separated label and value on each line. The label travels with the value on ' +
  'purpose: until the order is verified, a bare column of values would be dangerous to ' +
  'paste — an off-by-one against ERM’s real form puts a phone number in an email field ' +
  'with nothing on screen to catch it. It also drops into a spreadsheet column-aligned, ' +
  'which matters because the legacy record of every one of these pastes is a spreadsheet.'

// --- Wire shapes --------------------------------------------------------------

/** `PackEntryOut` in erm.py. One line of the pack, as it is retyped. */
export interface ErmPackEntry {
  label: string
  /** Dotted local origin, e.g. `trainers.phone`. On the wire because a person
   *  about to retype a value into a third-party portal is entitled to know which
   *  column it came from. */
  source: string
  value: string
  /** True when the source is NULL or blank. Carried separately so the screen can
   *  say “leave this field alone” without the pack containing a word like “N/A”
   *  that must never be pasted — `value` stays empty, so a copy is harmless. */
  is_blank: boolean
}

/** `FieldPackOut` in erm.py — §10's ordered field-value list, plus its warning. */
export interface ErmFieldPack {
  subject_kind: ErmSubjectKind
  entries: ErmPackEntry[]
  /** The server's own `label<TAB>value` serialisation. Never rebuilt here. */
  paste_text: string
  field_order_version: number
  /** **False today.** See `FIELD_ORDER_UNVERIFIED_NOTE`. */
  field_order_verified: boolean
}

/** `DriftedFieldOut` in erm.py. `was === null` means the pack GAINED this field
 *  between one sync and the next — a different thing from “it was blank”, and
 *  collapsing the two would report a never-sent field as unchanged. */
export interface ErmDriftedField {
  label: string
  source: string
  was: string | null
  now: string
}

/** `ErmTaskOut` in erm.py. One job card. */
export interface ErmTask {
  id: string
  subject_kind: ErmSubjectKind
  subject_id: string
  /** Trainer name or program name — a queue of bare UUIDs is not a queue. */
  subject_label: string
  state: ErmSyncState

  field_order_version: number
  assigned_to: string | null
  assigned_at: string | null

  erm_external_id: string | null
  verified: boolean
  remarks: string | null

  /** §10's `erm_synced_by` / `erm_synced_at`, as the card names them. */
  confirmed_by: string | null
  confirmed_at: string | null

  /** Written by the 1900 triggers, never by the API. */
  stale_at: string | null
  stale_reason: string | null
  supersedes_id: string | null

  cancelled_at: string | null
  cancelled_reason: string | null

  created_at: string
  updated_at: string
}

/** `ErmTaskDetail` in erm.py — everything the person doing the paste needs. */
export interface ErmTaskDetail {
  task: ErmTask
  pack: ErmFieldPack
  /** Which fields moved since the confirmed sync. May legitimately be empty on a
   *  stale card — see `DRIFT_WITHOUT_DIFFERENCES_NOTE`. */
  drift: ErmDriftedField[]
  /** True when the pack shown is the one frozen at confirm rather than one
   *  generated from the record as it reads now. */
  pack_is_frozen: boolean
}

/** `ErmQueue` in erm.py. The disclaimer is repeated at collection level so a
 *  client cannot render a queue without having been told the order is
 *  provisional — this file honours that rather than dropping the fields. */
export interface ErmQueue {
  tasks: ErmTask[]
  field_order_version: number
  field_order_verified: boolean
}

/** `QueueRequest`. `extra="forbid"`, so no field may be invented. */
export interface ErmQueueRequest {
  subject_kind: ErmSubjectKind
  subject_id: string
}

/** `ConfirmRequest`. The pack is NOT in it: the server regenerates and freezes
 *  it, because a client-supplied pack would let the row attest to values the
 *  database never held (R1, read backwards). */
export interface ErmConfirmRequest {
  erm_external_id?: string | null
  verified: boolean
  remarks?: string | null
}

export interface ErmQueueFilter {
  state?: ErmSyncState | null
  subject_kind?: ErmSubjectKind | null
  assigned_to_me?: boolean
  limit?: number
}

// --- The six calls ------------------------------------------------------------

async function postJson<T>(path: string, body?: unknown): Promise<T> {
  const response = await apiPost(path, body)
  return (await response.json()) as T
}

/**
 * The queue, oldest first, across every record the caller reaches.
 *
 * Deliberately NOT program-scoped: the person who works this queue works it
 * across their whole reach, which is what a queue is for. The scope comes from
 * the caller's assignments and the wall is applied per row, exactly as 1900's
 * three policies do it in SQL. This client adds no filter of its own beyond the
 * ones the API declares, and must not — a client-side wall is a second and
 * weaker one.
 */
export function fetchErmQueue(filter: ErmQueueFilter = {}): Promise<ErmQueue> {
  const params = new URLSearchParams({ limit: String(filter.limit ?? 200) })
  if (filter.state) params.set('state', filter.state)
  if (filter.subject_kind) params.set('subject_kind', filter.subject_kind)
  if (filter.assigned_to_me) params.set('assigned_to_me', 'true')
  return apiGet<ErmQueue>(`/erm/tasks?${params.toString()}`)
}

/**
 * One card: the pack to paste, and what has drifted since it was pasted.
 *
 * For an OPEN card the pack is generated live from the record, so what is on
 * screen is what the database holds at this instant — a card that sat in the
 * queue for a week hands over today's values, not last Tuesday's, which is the
 * whole failure §10 describes. For a CONFIRMED or STALE card the frozen pack
 * comes back instead: that row is evidence of what was pasted, and regenerating
 * it would quietly rewrite the evidence.
 */
export function fetchErmTask(id: string): Promise<ErmTaskDetail> {
  return apiGet<ErmTaskDetail>(`/erm/tasks/${id}`)
}

/**
 * File a card for one record. Unassigned, no pack yet, nothing sent.
 *
 * 409 when the record already has an open card, and that is the design rather
 * than a collision to retry around: a record edited five times in an afternoon
 * must produce one job, not five, or the queue becomes noise and the noise is
 * what gets ignored. `isConflict()` is how the screen tells that apart from a
 * failure.
 */
export function queueErmTask(request: ErmQueueRequest): Promise<ErmTask> {
  return postJson<ErmTask>('/erm/tasks', request)
}

/**
 * §10: "assigns it to a named person".
 *
 * Reassignment is allowed and is not an exception path — people go on leave and
 * work moves teams. Forcing a cancel-and-refile would fork the history of one
 * push across two rows.
 */
export function assignErmTask(id: string, assigneeId: string): Promise<ErmTask> {
  return postJson<ErmTask>(`/erm/tasks/${id}/assign`, { assignee_id: assigneeId })
}

/**
 * §10: "they paste, they confirm". **This transmits nothing.**
 *
 * Read `NOTHING_TRANSMITS_NOTE` before wiring anything to it. It records a claim
 * by the signed-in human, freezes the pack they were shown, and stamps
 * `erm_synced_at` / `erm_synced_by` on the source record. Blank strings are
 * normalised to `null` here so a stray space cannot pass for an ERM id or for a
 * remark — the row is evidence, and evidence made of whitespace is worse than an
 * empty column.
 */
export function confirmErmTask(
  id: string,
  request: { ermExternalId?: string; verified: boolean; remarks?: string },
): Promise<ErmTask> {
  const external = (request.ermExternalId ?? '').trim()
  const remarks = (request.remarks ?? '').trim()
  const body: ErmConfirmRequest = {
    erm_external_id: external === '' ? null : external,
    verified: request.verified,
    remarks: remarks === '' ? null : remarks,
  }
  return postJson<ErmTask>(`/erm/tasks/${id}/confirm`, body)
}

/**
 * Withdraw an open card. The reason is required, not decorative.
 *
 * A cancelled ERM push is a decision that a record deliberately does not match
 * ERM — which is the divergence §10 is about. Trimmed here so a string of spaces
 * cannot pass for a reason; the API 422s on it independently.
 */
export function cancelErmTask(id: string, reason: string): Promise<ErmTask> {
  const trimmed = reason.trim()
  if (trimmed === '') {
    throw new ApiError(
      'A cancellation needs a reason. It is what stops this reading as an oversight ' +
        'to whoever finds it in three months.',
      422,
    )
  }
  return postJson<ErmTask>(`/erm/tasks/${id}/cancel`, { reason: trimmed })
}

// --- Reading what a refusal actually was ---------------------------------------

function statusOf(error: unknown): number | null {
  return error instanceof ApiError ? error.status : null
}

/** 403 — not internal, outside the caller's reach, or not a trainer-pipeline
 *  persona trying to write a trainer card. An LDE Executive may READ the ERM
 *  state of trainers on their campus and may not push one. */
export function isForbidden(error: unknown): boolean {
  return statusOf(error) === 403
}

/** 409 — one open card per record already exists, or an illegal transition. */
export function isConflict(error: unknown): boolean {
  return statusOf(error) === 409
}

/** 404 — no such card, trainer or program. */
export function isNotFound(error: unknown): boolean {
  return statusOf(error) === 404
}

/** 422 — a blank cancellation reason, or a body the API refuses. */
export function isUnprocessable(error: unknown): boolean {
  return statusOf(error) === 422
}

// --- Copying, which is the actual job ------------------------------------------

/**
 * What happened when we tried to put text on the clipboard.
 *
 * Three outcomes and not a boolean, because the screen must react differently to
 * each: `unavailable` is the one where the human has to select the text by hand,
 * and telling them that is the difference between a working screen and one that
 * silently does nothing when they click Copy.
 */
export type CopyOutcome = 'clipboard' | 'legacy' | 'unavailable'

/**
 * Copy text, degrading rather than failing.
 *
 * **`navigator.clipboard` is undefined on a non-secure origin**, which includes
 * plain-HTTP dev hosts and any LAN address somebody opens the console on to do
 * exactly this job from a second machine. Copy-paste IS this screen — §10's
 * whole model is a human moving values between two windows by hand — so a
 * primary interaction that silently no-ops on half the origins it will be opened
 * from is not an acceptable failure mode.
 *
 * Two fallbacks, in order:
 *   1. the async Clipboard API, on a secure origin, inside a user gesture;
 *   2. `document.execCommand('copy')` over a detached textarea — deprecated,
 *      still implemented by every browser this app supports, and the only thing
 *      that works on `http://192.168.x.x:5173`;
 *   3. failing both, `unavailable`, and the caller shows the text selected for a
 *      manual Ctrl-C rather than pretending the copy happened.
 *
 * The selection is restored afterwards so a copy does not eat whatever the human
 * had highlighted, and the scratch element is removed in a `finally` so a throw
 * mid-copy cannot leave it in the DOM.
 */
export async function copyText(text: string): Promise<CopyOutcome> {
  if (typeof navigator !== 'undefined' && navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text)
      return 'clipboard'
    } catch {
      /* Permission denied, or not in a user gesture. Fall through. */
    }
  }

  if (typeof document === 'undefined') return 'unavailable'

  const scratch = document.createElement('textarea')
  const previous = document.getSelection()?.rangeCount ? document.getSelection()?.getRangeAt(0) : null
  try {
    scratch.value = text
    scratch.setAttribute('readonly', '')
    // Off-screen but focusable. `display:none` would make it unselectable, and
    // a zero-size element makes iOS scroll to it.
    scratch.style.position = 'fixed'
    scratch.style.top = '0'
    scratch.style.left = '-9999px'
    scratch.style.opacity = '0'
    document.body.appendChild(scratch)
    scratch.select()
    scratch.setSelectionRange(0, text.length)
    // eslint-disable-next-line deprecation/deprecation -- the only path on http://
    const ok = document.execCommand('copy')
    if (ok) return 'legacy'
  } catch {
    /* Some browsers throw rather than returning false. Same outcome. */
  } finally {
    scratch.remove()
    if (previous) {
      const selection = document.getSelection()
      selection?.removeAllRanges()
      selection?.addRange(previous)
    }
  }

  return 'unavailable'
}

/** What to tell the human after a copy. `unavailable` is not a success. */
export const COPY_OUTCOME_LABEL: Record<CopyOutcome, string> = {
  clipboard: 'Copied',
  legacy: 'Copied',
  unavailable: 'Could not copy — select the text and press Ctrl-C',
}

// --- Query keys ----------------------------------------------------------------
// Local to this file rather than in lib/queryKeys.ts, following lib/comms.ts and
// lib/approvals.ts: this workstream owns the ERM surface end to end and the keys
// move with the client that uses them.

export const ermKeys = {
  all: ['erm'] as const,
  queue: (filter: ErmQueueFilter) =>
    [
      'erm',
      'queue',
      filter.state ?? 'all',
      filter.subject_kind ?? 'all',
      filter.assigned_to_me ? 'mine' : 'everyone',
      // `limit` belongs in the key because fetchErmQueue SENDS it. Leaving it
      // out let two queries that differ only in limit share one cache entry, so
      // the narrower one was served the wider one's rows — a silent wrong-data
      // bug, not a stale-cache one. Mirrors the default in fetchErmQueue so the
      // key is stable whether or not the caller passed a limit.
      filter.limit ?? 200,
    ] as const,
  task: (id: string) => ['erm', 'task', id] as const,
  /** Names for the uuids on a card. Keyed here rather than reusing the approvals
   *  key so an invalidation on this screen cannot reach into that one, and
   *  carrying its own bound for the same reason `queue` carries one. */
  actors: (limit: number) => ['erm', 'actors', limit] as const,
  subjects: (kind: ErmSubjectKind) => ['erm', 'subjects', kind] as const,
}
