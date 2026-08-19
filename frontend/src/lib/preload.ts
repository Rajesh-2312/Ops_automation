import type { AppRole } from './types'

/* =============================================================================
   Collapsing the persona-root request waterfall.
   =============================================================================

   THE PROBLEM THIS SOLVES, measured on this repo before the fix:

     wave 1  index.html         entry + v-react + v-query + v-router + v-supabase + css
     wave 2  entry executes  ->  import('./ops/OpsRoot')      6.1 kB
     wave 3  OpsRoot renders ->  import('./HomePage')        30.4 kB

   Three sequential round trips to draw the first screen. The route split that
   produced them was right about BYTES and silent about LATENCY: Vite emits
   `<link rel="modulepreload">` only for the entry chunk's STATIC dependencies,
   so every `React.lazy` boundary on the landing path is a fetch the browser
   cannot discover until the chunk in front of it has arrived and run. On a
   college wifi connection with a 200 ms round trip that is 400 ms of nothing,
   spent on two files totalling 9.6 kB gzip.

   The fix has two halves and this module is the shared half.

   HALF ONE — the landing route is no longer lazy (see ops/OpsRoot.tsx). It is a
   static import of its root, so Rollup folds it into the root's chunk and wave 3
   stops existing. The other sixteen screens stay lazy; nothing about the bytes
   argument changed for them.

   HALF TWO — the root chunk is preloaded FROM THE HTML, so wave 2 stops being a
   wave and joins wave 1. That needs the hashed filename, which only exists after
   the bundle is written, so `collectPreloadFiles` below is called by a small
   Vite plugin (see vite.config.ts) that injects the resulting `<link>` list.

   WHY IT IS CONDITIONAL, AND WHAT THE CONDITION IS.

   Preloading the ops root unconditionally would charge every college login and
   every signed-out visitor ~10 kB gzip for a console they will never open —
   which is the exact waste the root split was introduced to remove. So the
   injected script preloads the root matching `bytexl-persona-root` in
   localStorage, written here when a profile resolves and cleared on sign-out.

   THAT KEY IS A LOAD HINT AND NOTHING ELSE. Read this before touching it:

     - It decides which BYTES are fetched early. It never decides which
       component renders: `Gate` in App.tsx switches on `profile.role`, read
       from the database on every session.
     - It never decides which ROWS arrive. That is RLS, in Postgres (R5).
     - A forged, stale or corrupt value can therefore do exactly one thing:
       download a chunk the browser then does not use. An absent value falls
       through to the pre-fix behaviour, which is correct rather than degraded —
       a first-time visitor genuinely does not know which root they need.

   Do not widen it. The moment something reads this key to decide what to SHOW,
   it stops being a hint and becomes a client-side permission check.
   ============================================================================= */

/**
 * The two front doors. `null` is a real answer — the `trainer` sentinel reaches
 * neither root (App.tsx renders `NoAccess`), so there is nothing to warm.
 */
export type PersonaRoot = 'ops' | 'college'

/** localStorage key. Shared with the build plugin by IMPORT, never by copy. */
export const PERSONA_ROOT_KEY = 'bytexl-persona-root'

/**
 * Which root a persona lands on. Mirrors the switch in `App.tsx`'s `Gate`, and
 * that duplication is deliberate: this one answers "what should we fetch", runs
 * before any profile is loaded, and must be safe to be wrong. `Gate` answers
 * "what should we render" and is the one that has to be right.
 */
export function personaRootFor(role: AppRole | null | undefined): PersonaRoot | null {
  switch (role) {
    case 'senior_manager':
    case 'manager':
    case 'lde_executive':
      return 'ops'
    case 'college':
      return 'college'
    default:
      // 'trainer' and anything unrecognised. Deny-by-default, matching §4.
      return null
  }
}

/**
 * The storage this module talks to.
 *
 * Injectable because localStorage throws outright in a blocked-cookie context
 * and does not exist at all under the node test environment. Every call site
 * below treats a throw as "no preference", which is the same fall-through an
 * absent value takes.
 */
export interface HintStorage {
  getItem(key: string): string | null
  setItem(key: string, value: string): void
  removeItem(key: string): void
}

function defaultStorage(): HintStorage | null {
  try {
    return (globalThis as { localStorage?: HintStorage }).localStorage ?? null
  } catch {
    return null
  }
}

/** Read the remembered root. Any value that is not one of the two is discarded. */
export function readPersonaRoot(storage: HintStorage | null = defaultStorage()): PersonaRoot | null {
  if (!storage) return null
  try {
    const v = storage.getItem(PERSONA_ROOT_KEY)
    return v === 'ops' || v === 'college' ? v : null
  } catch {
    return null
  }
}

/**
 * Remember which root this browser landed on, for the next cold start.
 *
 * A persona with no root (the sentinel) CLEARS the hint rather than leaving the
 * previous one in place — otherwise an account that lost its persona would keep
 * pulling a console it can no longer open, every load, forever.
 */
export function rememberPersonaRoot(
  role: AppRole | null | undefined,
  storage: HintStorage | null = defaultStorage(),
): void {
  if (!storage) return
  const root = personaRootFor(role)
  try {
    if (root) storage.setItem(PERSONA_ROOT_KEY, root)
    else storage.removeItem(PERSONA_ROOT_KEY)
  } catch {
    /* blocked storage — the app works, it just starts cold next time */
  }
}

/** Drop the hint. Called on sign-out, beside `queryClient.clear()`. */
export function forgetPersonaRoot(storage: HintStorage | null = defaultStorage()): void {
  if (!storage) return
  try {
    storage.removeItem(PERSONA_ROOT_KEY)
  } catch {
    /* nothing to do — a stale hint costs one wasted download, never a wrong render */
  }
}

// --- Navigation prefetch ------------------------------------------------------

/**
 * Warm a lazy route's chunk, at most once, and never let a failure surface.
 *
 * Used on nav hover and focus. The pointer sits on a link for 100–300 ms before
 * the click lands, which is most of a round trip on the connections this
 * console is read over, and a chunk already in the module registry makes
 * `React.lazy` resolve without suspending at all.
 *
 * A rejection here is deliberately swallowed. This is speculative work for a
 * navigation that may never happen; if the chunk is genuinely unreachable the
 * real `lazy()` import will fail at click time, where there is a Suspense
 * boundary and an ErrorBoundary to say so properly. Turning a hover into an
 * unhandled rejection would put a console error on the screen of somebody who
 * did nothing but move their mouse.
 */
export function makeRoutePrefetcher(
  routes: Record<string, () => Promise<unknown>>,
): (path: string) => void {
  const started = new Set<string>()
  return (path: string) => {
    if (started.has(path)) return
    const load = routes[path]
    if (!load) return
    started.add(path)
    void Promise.resolve()
      .then(load)
      .catch(() => {
        // Allow a later hover to retry: a one-off network blip should not
        // permanently disable prefetch for that link.
        started.delete(path)
      })
  }
}

// --- Build side ---------------------------------------------------------------
//
// Everything below runs in NODE, at build time, called from vite.config.ts. It
// ships to no browser: nothing in the app imports it, so Rollup tree-shakes it
// out of the bundle. It lives here rather than in the config because it is the
// other end of `PERSONA_ROOT_KEY` and of the waterfall argument at the top of
// this file, and because a pure function over a plain object is testable while
// a closure inside a Vite plugin is not.
//
// KEEP THIS MODULE FREE OF BROWSER-ONLY TOP-LEVEL CODE. vite.config.ts imports
// it, so a `document.` at module scope would break the build rather than the
// page. (The localStorage access above is inside function bodies, and the
// `AppRole` import is type-only and erased.)

/** The subset of a Rollup output chunk this needs. Structural, so tests can fake it. */
export interface PreloadChunk {
  fileName: string
  /** The source module this chunk is the facade for, if any. */
  facadeModuleId?: string | null
  /**
   * True for a chunk the HTML loads directly.
   *
   * Detected by flag rather than by matching `src/main.tsx`, because for an
   * HTML-entry build Vite records the entry against `index.html` and the
   * chunk's `facadeModuleId` is not reliably the main module. That mismatch
   * silently made the first version of this function subtract nothing, so the
   * injected list duplicated all four vendor chunks. The flag cannot drift.
   */
  isEntry?: boolean
  /** fileNames of chunks this one imports STATICALLY. Dynamic imports excluded. */
  imports?: string[]
  /** fileNames of stylesheets this chunk pulls in. */
  css?: string[]
}

const norm = (id: string) => id.replace(/\\/g, '/')

function findChunk(chunks: PreloadChunk[], moduleSuffix: string): PreloadChunk | undefined {
  const want = norm(moduleSuffix)
  return chunks.find((c) => {
    const id = c.facadeModuleId ? norm(c.facadeModuleId) : ''
    return id === want || id.endsWith(`/${want}`)
  })
}

function staticClosure(chunks: PreloadChunk[], ...roots: (PreloadChunk | undefined)[]): Set<string> {
  const seen = new Set<string>()
  const byFile = new Map(chunks.map((c) => [c.fileName, c]))
  const stack = roots.filter((c): c is PreloadChunk => c !== undefined)
  while (stack.length) {
    const c = stack.pop()!
    if (seen.has(c.fileName)) continue
    seen.add(c.fileName)
    for (const css of c.css ?? []) seen.add(css)
    for (const f of c.imports ?? []) {
      const next = byFile.get(f)
      if (next) stack.push(next)
      else seen.add(f) // an import with no chunk of its own is still a file to fetch
    }
  }
  return seen
}

/**
 * The files a persona root needs that the HTML does not already ask for.
 *
 * The static closure of the ENTRY chunks is exactly what Vite has already
 * written into index.html as `<script>` + `<link rel=modulepreload>` +
 * `<link rel=stylesheet>`, so subtracting it is what keeps this from
 * duplicating the four vendor chunks and the stylesheet in the head. What is
 * left is the root chunk and whatever only it reaches — for the ops root, the
 * root plus the landing screen it now statically imports, plus their shared
 * helpers.
 *
 * Returns URLs (base-prefixed), root chunk first so it gets the earliest slot,
 * then the rest in a stable order so two builds of the same source produce the
 * same HTML.
 */
export function collectPreloadFiles(
  chunks: PreloadChunk[],
  options: { rootModule: string; base?: string },
): string[] {
  const root = findChunk(chunks, options.rootModule)
  if (!root) return []
  const already = staticClosure(chunks, ...chunks.filter((c) => c.isEntry))
  const needed = [...staticClosure(chunks, root)].filter((f) => !already.has(f))

  const base = options.base ?? '/'
  const prefix = base.endsWith('/') ? base : `${base}/`
  const ordered = [
    ...needed.filter((f) => f === root.fileName),
    ...needed.filter((f) => f !== root.fileName).sort(),
  ]
  return ordered.map((f) => `${prefix}${f}`)
}
