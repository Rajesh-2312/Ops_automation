import { supabase } from './supabase'

/* =============================================================================
   FastAPI client — the work RLS cannot express
   =============================================================================

   Everything else in this app talks to PostgREST directly. Two operations
   cannot: seeding a program's checklist from `task_templates` and seeding its
   document register from `document_templates`. Both need to resolve each
   template's `offset_days` / `offset_anchor` against the program's dates, both
   must be idempotent by `template_id`, and both must be re-runnable to top a
   program up after a template is added. That is application logic, and
   CLAUDE.md §3 puts it in FastAPI.

   This replaces the old `lib/pending-backend.ts`, which did the resolution in
   the browser. It is deleted, not ported.

   ⚠️  ASSUMED CONTRACT. At the time of writing `app/api/` contains only
   `__init__.py` — the routers do not exist yet. These two paths and this
   response shape are what the brief specifies, and they are the shape the old
   client-side stand-in already had:

       POST {VITE_API_BASE_URL}/programs/{id}/tasks:generate      -> GenerateResult
       POST {VITE_API_BASE_URL}/programs/{id}/documents:generate  -> GenerateResult

   The colon is part of the path segment (Google AIP-136 custom-method style),
   so `{id}` must not be URL-encoded past the colon. If the backend lands on a
   different shape, this file is the only place that changes.

   AUTH. The user's Supabase JWT goes out as a Bearer token. The backend is
   expected to verify it and act as that user, so the same RLS that governs the
   direct PostgREST calls governs these. The frontend never holds a service-role
   key (CLAUDE.md R5), and an endpoint that used one would have to re-check role
   and reach itself.
   ============================================================================= */

const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/+$/, '')

export interface GenerateResult {
  created: number
  skipped: number
}

export class ApiError extends Error {
  readonly status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

/**
 * POST to the FastAPI service as the signed-in user, and hand back the raw
 * `Response`.
 *
 * Exported because not every endpoint answers with JSON: the two payout sheet
 * endpoints return an `.xlsx` body plus `X-Artifact-State` /
 * `X-Validation-Blocked` headers, and those headers are the only thing standing
 * between a blocked draft and a sheet someone files as though it were approved
 * (CLAUDE.md R4). A helper that returned `await response.json()` would throw
 * them away.
 *
 * A non-2xx response is raised as `ApiError` here so every caller gets the same
 * FastAPI `detail` unwrapping and the same 403 shape.
 */
export async function apiPost(path: string, body?: unknown): Promise<Response> {
  const { data } = await supabase.auth.getSession()
  const token = data.session?.access_token
  if (!token) throw new ApiError('Not signed in.', 401)

  let response: Response
  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token}`,
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    })
  } catch {
    // "Failed to fetch" is what the browser says. It is never what happened.
    throw new ApiError(unreachableMessage(), 0)
  }

  if (!response.ok) {
    const body = await response.text().catch(() => '')
    throw new ApiError(
      describeErrorBody(body, response.status, response.statusText),
      response.status,
    )
  }

  return response
}

/**
 * GET from the FastAPI service as the signed-in user.
 *
 * The mirror of `apiPost`, sharing its auth, its network-failure message and
 * its `ApiError` unwrapping so a 403 from a read reads the same as a 403 from
 * a write. Reads return JSON in every case we have, so unlike `apiPost` this
 * parses for the caller — the `.xlsx` responses that forced `apiPost` to hand
 * back a raw `Response` are all POSTs.
 */
export async function apiGet<T>(path: string): Promise<T> {
  const { data } = await supabase.auth.getSession()
  const token = data.session?.access_token
  if (!token) throw new ApiError('Not signed in.', 401)

  let response: Response
  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      headers: { Authorization: `Bearer ${token}` },
    })
  } catch {
    throw new ApiError(unreachableMessage(), 0)
  }

  if (!response.ok) {
    const body = await response.text().catch(() => '')
    throw new ApiError(
      describeErrorBody(body, response.status, response.statusText),
      response.status,
    )
  }

  return (await response.json()) as T
}

/**
 * What to say when `fetch` itself rejects, which is a different failure from
 * any status code.
 *
 * A rejected `fetch` means the request never left, or left and was refused
 * before a response existed. The browser deliberately tells JavaScript nothing
 * about which — a page must not be able to probe what is on another origin — so
 * the message has to name the small set of things it can actually be. The old
 * one named the wrong one first: "check that the FastAPI service is running"
 * sends someone to restart a service that is up and idle.
 *
 * THE TWO CASES ARE NOT THE SAME PROBLEM
 * ======================================
 * With no base URL, the request went to whatever serves the console. On a
 * static host that is a file server with no API on it, and no amount of
 * restarting anything changes that — the fix is a rebuild, because Vite bakes
 * `VITE_API_BASE_URL` into the bundle at build time.
 *
 * With a base URL, the likeliest cause is CORS rather than an outage: the
 * console and the API are on different origins by construction, so the API must
 * name this origin in `CORS_ALLOWED_ORIGINS` or the browser refuses the request
 * before sending it. That refusal leaves no trace in the API's log, which is
 * what makes it look like a network problem for far longer than it should.
 */
export function unreachableMessage(): string {
  if (!API_BASE_URL) {
    return (
      'This console was built without an API address, so the request went to the static ' +
      'host serving these pages, which has no API on it. Set VITE_API_BASE_URL to the ' +
      'FastAPI service and redeploy — it is baked in at build time, so a reload will not ' +
      'pick it up.'
    )
  }
  const here = typeof window === 'undefined' ? 'this origin' : window.location.origin
  return (
    `Could not reach the API at ${API_BASE_URL}. Either it is not running, or it is ` +
    `running and does not allow requests from ${here} — check CORS_ALLOWED_ORIGINS on ` +
    'the service. A refused request never arrives, so the API log is empty either way.'
  )
}

/** Longest raw body pasted into an error message before it is cut short. */
const MAX_ERROR_BODY = 400

/**
 * Turn a non-2xx response body into one line a reader can act on.
 *
 * WHY THIS IS NOT THE BODY VERBATIM
 * =================================
 * All four call sites used to fall back to the raw body when it would not
 * parse as JSON. FastAPI always answers JSON, so "not JSON" does not mean the
 * API said something unusual — it means the API never saw the request. On a
 * static host an unmatched path is answered by the host's own 404 page, and
 * the fallback pasted that page's entire markup into the error box: two
 * hundred lines of inline CSS wrapped around the one fact that mattered, which
 * was that there is no API at this origin. That is what the Ops Copilot showed
 * on GitHub Pages, and it looked like a Copilot fault rather than a missing
 * backend.
 *
 * A JSON body is unwrapped exactly as before. An HTML body is reported as what
 * it is. Anything else is truncated rather than pasted whole.
 */
export function describeErrorBody(body: string, status: number, statusText: string): string {
  try {
    const parsed = JSON.parse(body) as { detail?: unknown; message?: string }
    const detail = describeDetail(parsed.detail) ?? parsed.message
    if (detail) return detail
  } catch {
    /* not JSON — the shapes below handle that case deliberately */
  }

  if (/^\s*<(?:!doctype|html|\?xml)/i.test(body)) {
    return (
      `No API is deployed at ${API_BASE_URL || 'this origin'} — it answered ${status} ` +
      'with an HTML page instead of JSON, which is a static host replying rather than ' +
      'FastAPI. Point VITE_API_BASE_URL at a running backend and rebuild.'
    )
  }

  const trimmed = body.trim()
  if (!trimmed) return `${status} ${statusText}`
  return trimmed.length > MAX_ERROR_BODY ? `${trimmed.slice(0, MAX_ERROR_BODY)}…` : trimmed
}

/**
 * FastAPI's `detail` is a string for a raised `HTTPException` and a list of
 * `{loc, msg}` objects for a 422 from Pydantic. Rendering the second as
 * `[object Object]` is how a "period spans two months" turns into a mystery.
 */
function describeDetail(detail: unknown): string | undefined {
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    const parts = detail
      .map((item) => {
        if (typeof item === 'string') return item
        const e = item as { loc?: unknown[]; msg?: string }
        const field = Array.isArray(e.loc) ? e.loc.filter((p) => p !== 'body').join('.') : ''
        return field ? `${field}: ${e.msg ?? ''}` : (e.msg ?? '')
      })
      .filter(Boolean)
    if (parts.length > 0) return parts.join(' · ')
  }
  return undefined
}

async function post<T>(path: string): Promise<T> {
  const response = await apiPost(path)
  return (await response.json()) as T
}

/**
 * Seed (or top up) a program's checklist from the active task templates.
 * Idempotent by `template_id` — re-running after a template is added fills the
 * gap rather than duplicating the program's tasks.
 */
export function generateTasks(programId: string): Promise<GenerateResult> {
  return post<GenerateResult>(`/programs/${programId}/tasks:generate`)
}

/**
 * Seed (or top up) a program's document register from the required document
 * templates. The Drive equivalent of copying each master out of
 * 00_Templates_and_Masters into the program's stage folders.
 */
export function generateDocuments(programId: string): Promise<GenerateResult> {
  return post<GenerateResult>(`/programs/${programId}/documents:generate`)
}