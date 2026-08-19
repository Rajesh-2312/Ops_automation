import { useCallback, useState } from 'react'
import { unwrap } from './supabase'

/* =============================================================================
   Row bounds for every list this console reads.
   =============================================================================

   WHY THIS FILE EXISTS.

   Until now the frontend issued PostgREST reads with no `.limit()` and no
   `.range()` anywhere — every list screen asked for every row the caller could
   reach. That is not merely wasteful here, because the reach predicates on
   `tasks`, `batches`, `deployments` and `trainer_attendance` are per-row
   `SECURITY DEFINER` functions measured at 450–520 µs a row
   (docs/architecture-review.md §1). An unbounded read multiplies that cost by
   the size of the business, so the screens get slower forever and no index can
   help. `trainer_attendance` is one row per deployment per day by design
   (CLAUDE.md §6), which makes it the table that gets there first.

   A BOUND THAT LIES IS WORSE THAN NO BOUND.

   This is the whole reason the module returns `Bounded<T>` rather than `T[]`.
   A silently truncated list is a screen that shows a Manager 200 of 640 tasks
   and lets them conclude the queue is clear; on this product that conclusion
   ends in an unmarked attendance day and an underpaid CRT trainer. So every
   bounded read reports whether it was cut, and every screen that consumes one
   has to say so — see `BoundNote` in components/ui.tsx, and the rule that a
   figure DERIVED from a truncated list is suppressed rather than shown wrong.

   HOW TRUNCATION IS DETECTED: ONE EXTRA ROW.

   `bounded()` asks for `limit + 1` rows and hands back at most `limit`. If the
   extra row came back there is more behind the bound; if it did not, the list
   is complete. The obvious alternative — `rows.length === limit` — is wrong in
   exactly the case people notice, a list whose total is exactly the page size,
   where it claims a truncation that did not happen. One extra row costs one
   extra RLS predicate evaluation and removes the ambiguity entirely.

   BOUNDS ARE ABOUT VOLUME, NEVER ABOUT VISIBILITY.

   Nothing in this file filters rows. RLS decides what a persona may read and it
   decides it in Postgres (CLAUDE.md R5, and the comment at the head of
   ops/OpsRoot.tsx). A limit changes HOW MANY of the rows you are entitled to
   arrive at once; it must never change WHICH rows you are entitled to.

   THE BOUND BELONGS IN THE CACHE KEY.

   Every limit here is passed to a TanStack Query key factory as well as to the
   server. `lib/erm.ts` shipped without that once: `fetchErmQueue` sent `limit`
   and `ermKeys.queue` omitted it, so two queries differing only in their bound
   collided on one cache entry and the narrower query was served the wider
   query's rows. That is wrong data, not stale data, and it is silent. The key
   factories in lib/queryKeys.ts now take the limit as a REQUIRED argument, so
   the compiler refuses a call site that forgets it.
   ============================================================================= */

// --- The bounds ---------------------------------------------------------------

/**
 * Page sizes, per surface, with the reasoning attached.
 *
 * These are deliberately not one number. A screen a human reads and a lookup
 * that fills a `Map` fail differently when they are cut, so they are sized
 * differently and disclosed differently.
 */
export const PAGE = {
  /**
   * Lists a human scrolls. 200 rows is past the point anyone reads without
   * filtering, and `usePageLimit` raises it on request, so the bound costs a
   * click in the rare case and a table-scan in none.
   */
  tasks: 200,
  deployments: 200,
  trainers: 200,
  programs: 200,
  colleges: 200,
  workOrders: 200,
  batches: 200,
  approvals: 200,
  /**
   * One program's own rows. `seed.sql` ships 37 task and 37 document templates
   * per program, so 200 is roughly five times a full generated register and a
   * program that exceeds it has had rows added by hand.
   */
  programTasks: 200,
  programDocuments: 200,
  programBatches: 200,

  /**
   * Lookups that become a `Map` behind a screen rather than a list in front of
   * one. Larger, because a hole in a lookup is invisible in a way a short list
   * is not — and where a hole would change a number, the number is suppressed
   * instead of drawn (see TrainersPage's rails coverage).
   */
  profiles: 500,
  assignments: 1000,
  bankRails: 500,
  pnl: 200,

  /**
   * The two highest-growth tables in the schema.
   *
   * `students` is one row per learner per batch; `trainer_attendance` is one
   * row per deployment per day and is the table §1.3 of the architecture review
   * projects at 100k rows by year ten. The month window on the home screen is
   * already the strongest filter available (`mark_date` between two dates), and
   * this bound is the backstop behind it.
   */
  students: 2000,
  attendanceMonth: 5000,

  /** The college persona's three curated views. Read-only, one institution. */
  collegeViews: 200,

  /** FastAPI-backed queues. The server caps these too; this is the ask. */
  comms: 200,
  erm: 200,
} as const

// --- The result shape ---------------------------------------------------------

/**
 * A list plus the truth about whether it is all of them.
 *
 * `limit` travels with the rows so a screen can say the number out loud without
 * importing the constant, and so a component that renders the note does not
 * have to be told twice which bound produced it.
 */
export interface Bounded<T> {
  rows: T[]
  limit: number
  /** True when more rows exist behind the bound. Never a guess — see the header. */
  truncated: boolean
}

/** An empty result, for the render pass before a query resolves. */
export function emptyBound<T>(limit: number): Bounded<T> {
  return { rows: [], limit, truncated: false }
}

/**
 * Run a bounded PostgREST read.
 *
 * The caller builds the query and applies `.limit(n)` itself, with `n` supplied
 * by this function. That shape was chosen over "hand me a builder and I will
 * bound it" for two reasons: it keeps `.limit()` visible in the query at the
 * call site, where a reader looking for the bound will look for it, and it
 * avoids constraining the caller to a structural type that the supabase-js
 * builder happens to satisfy today.
 *
 * @param limit how many rows the caller wants; `limit + 1` are requested.
 * @param build receives the row count to ask for and returns the query.
 */
export async function bounded<T>(
  limit: number,
  build: (rows: number) => PromiseLike<{ data: T[] | null; error: unknown }>,
): Promise<Bounded<T>> {
  const rows = await unwrap<T[]>(build(limit + 1))
  const truncated = rows.length > limit
  return { rows: truncated ? rows.slice(0, limit) : rows, limit, truncated }
}

/**
 * The same, for a list that arrives from FastAPI already bounded server-side.
 *
 * The API endpoints take a `limit` and cannot be asked for `limit + 1` without
 * changing their contract, so truncation here is the `length >= limit`
 * heuristic the header calls ambiguous. It is used ONLY on this path, and the
 * note it produces is worded to match — "at the 200-row cap" rather than "there
 * are more" — because at the cap is exactly what is known.
 */
export function boundedFromServer<T>(rows: T[], limit: number): Bounded<T> {
  return { rows, limit, truncated: rows.length >= limit }
}

// --- Paging -------------------------------------------------------------------

/**
 * A raisable bound for one screen.
 *
 * Deliberately "raise the ceiling and refetch" rather than cursor paging. This
 * console's tables are filtered and counted client-side — the work queue bands
 * by urgency, Trainers counts work-order states across the whole roster — and
 * page-at-a-time windows would make every one of those counts a count of the
 * current page, which is the silent-partial-data failure this module exists to
 * prevent. Raising the ceiling keeps one contiguous list whose extent the
 * screen can state honestly.
 *
 * The returned `limit` is what goes in the query key AND in the request, which
 * is what makes the two agree.
 */
export function usePageLimit(step: number): {
  limit: number
  more: () => void
  step: number
} {
  const [limit, setLimit] = useState(step)
  const more = useCallback(() => setLimit((n) => n + step), [step])
  return { limit, more, step }
}
