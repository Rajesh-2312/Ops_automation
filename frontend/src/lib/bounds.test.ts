import { describe, expect, it } from 'vitest'
import { PAGE, bounded, boundedFromServer, emptyBound } from './bounds'
import { qk } from './queryKeys'
import { commsKeys } from './comms'
import { ermKeys } from './erm'
import { approvalKeys } from './approvals'

/* =============================================================================
   The bound is part of the answer, so it has to be part of the key.
   =============================================================================

   THE BUG THESE TESTS EXIST FOR, in full, because it is subtle and it shipped:

   `fetchErmQueue` sent `limit` to the server. `ermKeys.queue` did not include
   it. TanStack Query caches by key, so a request for 50 rows and a request for
   1,000 rows resolved to ONE cache entry — and whichever ran first answered the
   other. The narrower query was served the wider query's rows. Note what that
   is and is not: it is not staleness, which resolves on refetch, and it is not
   an error, which surfaces. It is a screen quietly holding the wrong number of
   rows, and on this product a wrong row count is an attendance day or a payout.

   `commsKeys.queue` carried the same shape, latent because its only call site
   never passed a limit. Both are fixed and both are locked below.

   Every bounded key in the app is asserted here on two properties:

     1. two different bounds produce two different keys, and
     2. the limit is actually IN the key rather than merely making it unequal
        by accident.

   `limit` is a REQUIRED parameter on every one of these factories, which is the
   other half of the defence: a call site that bounds a query and forgets to key
   it does not compile. These tests cover the half the compiler cannot — that
   the argument reaches the key rather than being accepted and dropped.
   ============================================================================= */

/** Every bounded key factory, as (name, factory) so one loop can cover them. */
const BOUNDED_KEYS: [string, (limit: number) => readonly unknown[]][] = [
  ['qk.profiles.list', (n) => qk.profiles.list(n)],
  ['qk.colleges.list', (n) => qk.colleges.list(n)],
  ['qk.collegeAssignments.list', (n) => qk.collegeAssignments.list(n)],
  ['qk.clusterAssignments.list', (n) => qk.clusterAssignments.list(n)],
  ['qk.programs.list', (n) => qk.programs.list(n)],
  ['qk.batches.list', (n) => qk.batches.list(n)],
  ['qk.batches.byProgram', (n) => qk.batches.byProgram('prog-1', n)],
  ['qk.tasks.queue', (n) => qk.tasks.queue(n)],
  ['qk.tasks.rollup', (n) => qk.tasks.rollup(n)],
  ['qk.tasks.byProgram', (n) => qk.tasks.byProgram('prog-1', n)],
  ['qk.tasks.mine', (n) => qk.tasks.mine(n)],
  ['qk.deployments.byTrainer', (n) => qk.deployments.byTrainer('t-1', n)],
  ['qk.workOrders.byTrainer', (n) => qk.workOrders.byTrainer('t-1', n)],
  ['qk.documents.byProgram', (n) => qk.documents.byProgram('prog-1', n)],
  ['qk.trainers.list', (n) => qk.trainers.list(n)],
  ['qk.deployments.list', (n) => qk.deployments.list(n)],
  ['qk.bankRails.list', (n) => qk.bankRails.list(n)],
  ['qk.workOrders.list', (n) => qk.workOrders.list(n)],
  ['qk.home.batches', (n) => qk.home.batches(n)],
  ['qk.home.studentCount', (n) => qk.home.studentCount(n)],
  ['qk.home.attendanceMonth', (n) => qk.home.attendanceMonth('2026-08', n)],
  ['qk.home.pendingApprovals', (n) => qk.home.pendingApprovals(n)],
  ['qk.home.pnl', (n) => qk.home.pnl(n)],
  ['qk.college.progress', (n) => qk.college.progress(n)],
  ['qk.college.attendance', (n) => qk.college.attendance(n)],
  ['qk.college.reports', (n) => qk.college.reports(n)],
  ['commsKeys.queue', (n) => commsKeys.queue('prog-1', null, n)],
  ['commsKeys.actors', (n) => commsKeys.actors(n)],
  ['ermKeys.queue', (n) => ermKeys.queue({ limit: n })],
  ['ermKeys.actors', (n) => ermKeys.actors(n)],
  ['approvalKeys.queue', (n) => approvalKeys.queue(n)],
  ['approvalKeys.actors', (n) => approvalKeys.actors(n)],
]

describe('every bounded query key carries its bound', () => {
  it.each(BOUNDED_KEYS)('%s separates two different limits', (_name, key) => {
    expect(key(50)).not.toEqual(key(200))
  })

  it.each(BOUNDED_KEYS)('%s puts the limit in the key, not merely near it', (_name, key) => {
    expect(key(37)).toContain(37)
    expect(key(1234)).toContain(1234)
  })

  it.each(BOUNDED_KEYS)('%s is stable for the same limit', (_name, key) => {
    expect(key(200)).toEqual(key(200))
  })
})

describe('commsKeys.queue — the latent copy of the erm.ts bug', () => {
  // The specific regression. `fetchCommsQueue(programId, state, limit)` sends
  // all three; all three must be in the key or two different asks collide.
  it('separates two queues that differ only in limit', () => {
    expect(commsKeys.queue('p', null, 50)).not.toEqual(commsKeys.queue('p', null, 200))
  })

  it('still separates on program and on state', () => {
    expect(commsKeys.queue('p1', null, 200)).not.toEqual(commsKeys.queue('p2', null, 200))
    expect(commsKeys.queue('p', 'DRAFT', 200)).not.toEqual(
      commsKeys.queue('p', 'APPROVED', 200),
    )
  })

  it('renders a null state as "all" rather than dropping the slot', () => {
    // Dropping it would let ['comms','queue','p',200] and
    // ['comms','queue','p','DRAFT'] differ only by position.
    expect(commsKeys.queue('p', null, 200)).toEqual(['comms', 'queue', 'p', 'all', 200])
  })

  it('keeps a prefix that matches every bound of one program, for invalidation', () => {
    // The screen invalidates with `queuesFor`, and TanStack matches by prefix.
    // If this stopped being a prefix, a queue cached at another page size would
    // survive an approval and show a stale state.
    const prefix = commsKeys.queuesFor('p1')
    for (const limit of [50, 200, 1000]) {
      const full = commsKeys.queue('p1', null, limit)
      expect(full.slice(0, prefix.length)).toEqual([...prefix])
    }
    expect(commsKeys.queue('p2', null, 200).slice(0, prefix.length)).not.toEqual([...prefix])
  })
})

describe('bounded keys stay under the namespace their invalidation uses', () => {
  // `invalidateQueries({ queryKey: qk.tasks.all })` has to keep reaching every
  // page size of every task query, or a mutation on one screen stops
  // refreshing another. Adding the limit at the END is what preserves that.
  it.each([
    ['tasks', qk.tasks.all, [qk.tasks.queue(200), qk.tasks.rollup(200), qk.tasks.byProgram('p', 200)]],
    ['trainers', qk.trainers.all, [qk.trainers.list(200)]],
    ['deployments', qk.deployments.all, [qk.deployments.list(200)]],
    ['programs', qk.programs.all, [qk.programs.list(200)]],
    ['colleges', qk.colleges.all, [qk.colleges.list(200)]],
    ['work orders', qk.workOrders.all, [qk.workOrders.list(200)]],
  ])('%s', (_name, prefix, keys) => {
    for (const key of keys) {
      expect(key.slice(0, prefix.length)).toEqual([...prefix])
    }
  })
})

/* --------------------------------------------------------------------------
   The truncation signal itself.
-------------------------------------------------------------------------- */

/** A stand-in for the PostgREST builder: records the row count it was asked for. */
function fakeQuery(available: number) {
  const asked: number[] = []
  const build = (rows: number) => {
    asked.push(rows)
    const data = Array.from({ length: Math.min(rows, available) }, (_, i) => ({ i }))
    return Promise.resolve({ data, error: null })
  }
  return { asked, build }
}

describe('bounded() reports truncation without guessing', () => {
  it('asks the server for one row more than the caller wants', async () => {
    const q = fakeQuery(500)
    await bounded(200, q.build)
    expect(q.asked).toEqual([201])
  })

  it('never hands back more than the limit', async () => {
    const q = fakeQuery(500)
    const result = await bounded(200, q.build)
    expect(result.rows).toHaveLength(200)
    expect(result.limit).toBe(200)
  })

  it('flags truncation when a row exists behind the bound', async () => {
    const result = await bounded(10, fakeQuery(11).build)
    expect(result.truncated).toBe(true)
    expect(result.rows).toHaveLength(10)
  })

  // The case the `length === limit` heuristic gets wrong, and the reason for
  // the extra row: a list whose total is EXACTLY the page size is complete, and
  // claiming otherwise trains people to ignore the warning.
  it('does NOT flag a list whose total is exactly the limit', async () => {
    const result = await bounded(10, fakeQuery(10).build)
    expect(result.truncated).toBe(false)
    expect(result.rows).toHaveLength(10)
  })

  it('does not flag a short list', async () => {
    const result = await bounded(200, fakeQuery(3).build)
    expect(result.truncated).toBe(false)
    expect(result.rows).toHaveLength(3)
  })

  it('treats an empty result as complete, not truncated', async () => {
    const result = await bounded(200, fakeQuery(0).build)
    expect(result).toEqual({ rows: [], limit: 200, truncated: false })
  })

  it('throws rather than caching an RLS refusal as an empty page', async () => {
    // Same contract as `unwrap`: a refusal must not render as "no rows".
    const failing = () => Promise.resolve({ data: null, error: { code: '42501' } })
    await expect(bounded(200, failing)).rejects.toBeTruthy()
  })
})

describe('boundedFromServer() — the FastAPI path', () => {
  // These endpoints take a limit and cannot be asked for limit+1, so this is
  // the ambiguous heuristic on purpose. `BoundNote` words it as "at the cap"
  // rather than "there are more", which is exactly what is known.
  it('reports a full page as at the cap', () => {
    expect(boundedFromServer([1, 2, 3], 3).truncated).toBe(true)
  })

  it('reports a short page as complete', () => {
    expect(boundedFromServer([1, 2], 3).truncated).toBe(false)
  })

  it('carries the rows through untouched', () => {
    const rows = [{ id: 'a' }, { id: 'b' }]
    expect(boundedFromServer(rows, 200).rows).toBe(rows)
  })
})

describe('emptyBound() is never a truncation claim', () => {
  it('is complete and empty', () => {
    expect(emptyBound(200)).toEqual({ rows: [], limit: 200, truncated: false })
  })
})

describe('PAGE sizes', () => {
  it('are all positive integers', () => {
    for (const [name, value] of Object.entries(PAGE)) {
      expect(Number.isInteger(value), name).toBe(true)
      expect(value, name).toBeGreaterThan(0)
    }
  })

  // The home screen reuses `tasks.queue`, `programs.list` and
  // `deployments.list` at the DEFAULT page size so it warms the caches the
  // working screens read rather than fetching the same rows under a second
  // bound. That only holds while both sides take the same constant, which is
  // exactly the kind of coupling that rots silently.
  it('keeps the highest-growth tables at or above the shared list page size', () => {
    expect(PAGE.students).toBeGreaterThanOrEqual(PAGE.tasks)
    expect(PAGE.attendanceMonth).toBeGreaterThanOrEqual(PAGE.students)
  })
})
