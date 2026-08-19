import { ApiError, apiGet, apiPost } from './api'

/* =============================================================================
   The Ops Copilot API, as the browser sees it.
   =============================================================================

   Mirrors `app/api/copilot.py` field for field:

       POST /copilot/ask      AskRequest  -> AskResponse
       GET  /copilot/corpora              -> CorpusAccessOut[]

   THREE THINGS THIS FILE DELIBERATELY DOES NOT DO.

   1. It does not filter by persona. CLAUDE.md §9 puts the persona filter BEFORE
      retrieval, and `app/rag/scope.py` is where it lives — the ACL in
      `rag_corpus_access` is intersected inside `public.rag_search()`, in the
      same statement as the `ORDER BY ... LIMIT`. A second copy of that rule in
      JavaScript would be the copy that drifts (R5), and it could only ever
      narrow a result set the server already narrowed correctly.

   2. It does not classify questions. `app/rag/guards.py` decides what is a
      structured-fact question, and it refuses BEFORE retrieval. The UI's job is
      to explain the refusal it is handed, not to predict it — a client-side
      guess that disagreed with the server would either block a question the
      Copilot would have answered or promise an answer it will refuse.

   3. It does not send `facts`. There is no `facts` field on `AskRequest` on
      purpose: a client-supplied "fact" is an unverified number that the figure
      check in `guards.py` would then bless as grounded, inverting R1 exactly.
      `facts` is response-only here, and is rendered in its own block (§9:
      "hybrid answers must visibly separate the two").

   A REFUSAL IS A 200. `answered: false` is a correct outcome of a well-formed
   request, not an error, so nothing here maps it onto a thrown `ApiError`. The
   page renders it as an explanation with somewhere else to go.
   ============================================================================= */

// --- vocabulary (mirrors app/domain/enums.py and app/rag/guards.py) ----------

/** `app.domain.enums.Corpus` — the six separately-permissioned corpora (§9). */
export type Corpus =
  | 'sop'
  | 'contracts'
  | 'college_dossier'
  | 'educator'
  | 'curriculum'
  | 'reports'

export const CORPORA: readonly Corpus[] = [
  'sop',
  'contracts',
  'college_dossier',
  'educator',
  'curriculum',
  'reports',
] as const

export const CORPUS_LABEL: Record<Corpus, string> = {
  sop: 'SOP',
  contracts: 'Contracts',
  college_dossier: 'College dossier',
  educator: 'Educator',
  curriculum: 'Curriculum',
  reports: 'Reports',
}

/** `app.rag.guards.RefusalReason`. A closed vocabulary, because these are counted. */
export type RefusalReason =
  | 'structured_fact'
  | 'no_sources'
  | 'uncited'
  | 'invalid_citation'
  | 'fabricated_figure'
  | 'no_corpus_access'

/**
 * A one-line heading per refusal, for the person who asked.
 *
 * The server's `message` is the authority and is always rendered verbatim
 * underneath; this is only the headline, because "Unable to answer" repeated
 * six times teaches nothing about which of six very different things happened.
 */
export const REFUSAL_TITLE: Record<RefusalReason, string> = {
  structured_fact: 'That is a database question, not a policy question',
  no_sources: 'Nothing in your corpora was close enough to cite',
  uncited: 'The answer could not be attributed to a source',
  invalid_citation: 'The answer cited a source that was not retrieved',
  fabricated_figure: 'The answer contained a figure from no source',
  no_corpus_access: 'You hold no searchable corpus',
}

/**
 * Whether a refusal means "ask differently" or "this was a system fault".
 *
 * `structured_fact` and `no_sources` are the Copilot working: the boundary held
 * and the user should go elsewhere or rephrase. The other three mean a model
 * produced something that failed a gate — worth showing in a louder tone,
 * because a run of them is a defect rather than a usage pattern.
 */
export const REFUSAL_IS_GUARDRAIL: Record<RefusalReason, boolean> = {
  structured_fact: false,
  no_sources: false,
  uncited: true,
  invalid_citation: true,
  fabricated_figure: true,
  no_corpus_access: false,
}

// --- wire models -------------------------------------------------------------

/** `app.api.copilot.CitationOut` — §9's two required halves, plus the version flag. */
export interface Citation {
  marker: number
  document: string
  section: string
  corpus: Corpus
  version: number
  is_superseded: boolean
}

export interface Refusal {
  reason: RefusalReason
  message: string
}

/** `app.api.copilot.AskResponse`. An answer or a refusal — never a blend. */
export interface AskResponse {
  answered: boolean
  answer: string | null
  citations: Citation[]
  /** Values of record, supplied by the service from SQL. Never merged into prose. */
  facts: Record<string, string>
  refusal: Refusal | null
}

export interface AskRequest {
  question: string
  /** Narrows the search. Never widens beyond your access — the ACL still applies. */
  corpora?: Corpus[]
  limit?: number
  include_superseded?: boolean
}

export interface CorpusAccess {
  corpus: Corpus
  rationale: string
}

/** `MAX_LIMIT` in `app/api/copilot.py`. Beyond this the request 422s. */
export const MAX_LIMIT = 20
/** `DEFAULT_LIMIT` in `app/rag/retrieval.py`, as the API's default. */
export const DEFAULT_LIMIT = 8

// --- calls -------------------------------------------------------------------

export async function ask(body: AskRequest): Promise<AskResponse> {
  const response = await apiPost('/copilot/ask', body)
  return (await response.json()) as AskResponse
}

export function myCorpora(): Promise<CorpusAccess[]> {
  return apiGet<CorpusAccess[]>('/copilot/corpora')
}

// --- query keys (local, per the brief — queryKeys.ts is not ours to edit) -----

export const copilotKeys = {
  all: ['copilot'] as const,
  corpora: () => ['copilot', 'corpora'] as const,
}

// --- rendering helpers -------------------------------------------------------

export type AnswerSegment =
  | { kind: 'text'; text: string }
  | { kind: 'citation'; marker: number }

/**
 * Split an answer into prose and `[n]` citation markers.
 *
 * The same pattern as `_CITATION_RE` in `app/rag/guards.py`, and it must stay
 * the same: a marker the server validated but this splitter did not recognise
 * would render as literal `[3]` and lose its link to the source, which is the
 * one thing on this screen that makes the answer trustworthy.
 *
 * Markers are NOT resolved to citations here. A marker with no matching
 * citation cannot reach the browser — `check_citations()` discards that whole
 * answer — so the page renders the marker and lets the lookup miss be visible
 * rather than silently swallowing it.
 */
export function splitAnswer(answer: string): AnswerSegment[] {
  const segments: AnswerSegment[] = []
  const pattern = /\[(\d{1,2})\]/g
  let cursor = 0
  let match: RegExpExecArray | null
  while ((match = pattern.exec(answer)) !== null) {
    if (match.index > cursor) {
      segments.push({ kind: 'text', text: answer.slice(cursor, match.index) })
    }
    segments.push({ kind: 'citation', marker: Number(match[1]) })
    cursor = match.index + match[0].length
  }
  if (cursor < answer.length) segments.push({ kind: 'text', text: answer.slice(cursor) })
  return segments
}

/**
 * A transport failure, said in a way that names the likely cause.
 *
 * 503 is the one worth special-casing: `get_copilot()` returns it when
 * OpenRouter is unconfigured, which is a deployment state rather than a bug
 * (Phase 1 is AI-free by design and boots without those variables), and
 * "Service Unavailable" would send someone hunting for an outage.
 */
export function copilotErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 503) return error.message
    if (error.status === 403) {
      return 'The Copilot is available to internal staff only. Your persona holds no access to it.'
    }
    return error.message
  }
  return error instanceof Error ? error.message : String(error)
}
