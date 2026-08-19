import { describe, expect, expectTypeOf, it } from 'vitest'
import { ApiError } from './api'
import {
  ARTIFACT_STATE_HEADER,
  FIGURES_NOTE,
  NARRATIVE_NOTE,
  NOTHING_SENDS_NOTE,
  REPORT_APPROVAL_UNDECIDED_NOTE,
  isDraftRefused,
  isForbidden,
  isNarrationUnavailable,
  isNotFound,
  reportKeys,
} from './reports'
import type { FeedbackSynthesis, GovernanceReportDraft, TrainerCost, TrainerCostLine } from './reports'

/* =============================================================================
   R2 and R7, enforced by the compiler.
   =============================================================================

   Money crosses this boundary as a `Decimal` STRING and `TrainerCost.total` is
   typed as the literal `null`. That is not a wire-format detail — it is the rule
   made unrepresentable: there is no expression in this file that adds two payouts
   together, because TypeScript will not compile one.

   The `@ts-expect-error` lines below are the assertions. They are checked by
   `tsc --noEmit` / `npm run build` (tsconfig includes `src`), NOT by the vitest
   run — vitest strips types without checking them. Each one fails the build in
   BOTH directions:

     · retype `net` as a number and the arithmetic compiles, so the directive
       becomes an unused '@ts-expect-error' — error TS2578;
     · leave it a string and the directive is doing its job.

   `NonNullable<…>` is used deliberately so what is being proved is the STRING,
   not merely the `| null`. A field retyped `number | null` would still be
   non-arithmetic through the union, and the test would pass for the wrong reason.
   ============================================================================= */

/** The §6 regression fixture, as the reporting endpoint reads it back. */
const LINE: TrainerCostLine = {
  trainer: 'VEMA PRUDHVI SAI',
  pan: 'ABCDE1234F',
  period_start: '2026-07-26',
  period_end: '2026-07-31',
  net: '14035.00',
  invoice_no: 'BCDP/26-27/JUL1',
}

const COST: TrainerCost = {
  payout_count: 1,
  lines: [LINE],
  trainers_without_payout: [],
  total: null,
}

/** Proves the string-ness of `net` with the `| null` already taken out. */
function multiplyNet(net: NonNullable<TrainerCostLine['net']>): void {
  // @ts-expect-error — R7: a payout is a Decimal string. Multiplying it is float money.
  void (net * 2)
  // @ts-expect-error — R7: and so is scaling it, however it is spelled.
  void Math.round(net)
}

/** The same wall around a score the reporting layer only ever displays. */
function averageScore(score: NonNullable<FeedbackSynthesis['average_score']>): void {
  // @ts-expect-error — R7: `average_score` is a Decimal string; dividing it is a float.
  void (score / 2)
}

/** And around the one attendance figure that is a percentage, not a count. */
function attendance(percent: NonNullable<GovernanceReportDraft['student_attendance_percent']>): void {
  // @ts-expect-error — R7: a percentage of record is a Decimal string too.
  void (percent * 100)
}

describe('money is a string, and the compiler is what says so (R2/R7)', () => {
  it('types every monetary and Decimal field as a string', () => {
    expectTypeOf<TrainerCostLine['net']>().toEqualTypeOf<string | null>()
    expectTypeOf<FeedbackSynthesis['average_score']>().toEqualTypeOf<string | null>()
    expectTypeOf<FeedbackSynthesis['lowest_score']>().toEqualTypeOf<string | null>()
    expectTypeOf<FeedbackSynthesis['highest_score']>().toEqualTypeOf<string | null>()
    expectTypeOf<GovernanceReportDraft['student_attendance_percent']>().toEqualTypeOf<string | null>()
    expect(typeof LINE.net).toBe('string')
  })

  it('refuses arithmetic on those fields at compile time', () => {
    multiplyNet('14035.00')
    averageScore('4.20')
    attendance('87.50')
    // Nothing above runs an assertion that vitest can fail — the failure mode is
    // a build error. This line keeps the test honest about that.
    expect(NOTHING_SENDS_NOTE).toContain('goes nowhere')
  })

  it('still types counts as numbers, so the wall is about money and not about rigour', () => {
    expectTypeOf<TrainerCost['payout_count']>().toEqualTypeOf<number>()
    expectTypeOf<FeedbackSynthesis['collections']>().toEqualTypeOf<number>()
    expectTypeOf<FeedbackSynthesis['total_responses']>().toEqualTypeOf<number | null>()
    expectTypeOf<GovernanceReportDraft['tasks_overdue']>().toEqualTypeOf<number>()
  })
})

describe('TrainerCost.total is the literal null (R2)', () => {
  it('is typed null rather than omitted, so the reason is findable', () => {
    expectTypeOf<TrainerCost['total']>().toEqualTypeOf<null>()
    expect(COST.total).toBeNull()
  })

  it('cannot be summed into, and cannot be assigned a computed total', () => {
    // @ts-expect-error — R2: there is no programme total, so there is nothing to add to.
    void (COST.total + 0)
    // @ts-expect-error — R2: a sum computed in a reporting endpoint is a second money.
    void ({ ...COST, total: 29584 } satisfies TrainerCost)
    expect(COST.lines).toHaveLength(1)
  })

  it('leaves per-trainer lines intact — the wall is on the SUM, not on the detail', () => {
    expect(COST.lines[0].net).toBe('14035.00')
    expect(COST.payout_count).toBe(1)
  })
})

describe('the Decimal survives the wire because it is never a float', () => {
  it('keeps the two-place storage §11 requires', () => {
    const line = JSON.parse(
      '{"trainer":"VEMA PRUDHVI SAI","pan":"ABCDE1234F","period_start":"2026-07-26",' +
        '"period_end":"2026-07-31","net":"14035.10","invoice_no":null}',
    ) as TrainerCostLine
    expect(line.net).toBe('14035.10')
    // What the same value looks like the moment anything treats it as a number.
    expect(String(Number(line.net))).toBe('14035.1')
  })

  it('an explicit Number() still compiles, and that is the point', () => {
    // The wall is against SILENT arithmetic. A deliberate conversion is one
    // greppable token that shows up in review; an implicit `a * b` does not.
    expect(Number(LINE.net)).toBe(14035)
  })
})

describe('reportKeys', () => {
  it('does not serve one period out of another period’s cache', () => {
    expect(reportKeys.feedback('p1', '2026-07-01', '2026-07-31', false)).not.toEqual(
      reportKeys.feedback('p1', '2026-08-01', '2026-08-31', false),
    )
  })

  it('treats a narrated report as a different answer from a bare one', () => {
    expect(reportKeys.feedback('p1', '2026-07-01', '2026-07-31', true)).not.toEqual(
      reportKeys.feedback('p1', '2026-07-01', '2026-07-31', false),
    )
  })

  it('does not collide a feedback key with a college summary key', () => {
    expect(reportKeys.feedback('x', '2026-07-01', '2026-07-31', false)).not.toEqual(
      reportKeys.collegeSummary('x', '2026-07-01', '2026-07-31', false),
    )
  })

  it('is stable for identical arguments', () => {
    expect(reportKeys.collegeSummary('c1', '2026-07-01', '2026-07-31', true)).toEqual(
      reportKeys.collegeSummary('c1', '2026-07-01', '2026-07-31', true),
    )
  })
})

describe('refusals are told apart, because they mean opposite things', () => {
  it('reads 503 as "narration is not configured here", not as an outage', () => {
    const unconfigured = new ApiError('Narration is unavailable.', 503)
    expect(isNarrationUnavailable(unconfigured)).toBe(true)
    expect(isDraftRefused(unconfigured)).toBe(false)
    expect(isForbidden(unconfigured)).toBe(false)
  })

  it('reads 422 as the platform refusing its own draft (R1/§9)', () => {
    const refused = new ApiError('Generated narrative contained an ungrounded figure.', 422)
    expect(isDraftRefused(refused)).toBe(true)
    expect(isNarrationUnavailable(refused)).toBe(false)
  })

  it('reads 403 as the commercials wall and 404 as out of reach', () => {
    expect(isForbidden(new ApiError('x', 403))).toBe(true)
    expect(isNotFound(new ApiError('x', 404))).toBe(true)
    expect(isForbidden(new ApiError('x', 404))).toBe(false)
    expect(isNotFound(new ApiError('x', 403))).toBe(false)
  })

  it('ignores anything that is not an ApiError', () => {
    for (const predicate of [isNarrationUnavailable, isDraftRefused, isForbidden, isNotFound]) {
      expect(predicate(new Error('503'))).toBe(false)
      expect(predicate({ status: 503 })).toBe(false)
      expect(predicate(null)).toBe(false)
    }
  })
})

describe('the sentences this screen has to be able to say', () => {
  it('names the header that carries the artifact state (R4)', () => {
    expect(ARTIFACT_STATE_HEADER).toBe('X-Artifact-State')
  })

  it('says the approval block is an open question, not a missing permission', () => {
    expect(REPORT_APPROVAL_UNDECIDED_NOTE).toContain('not a permission problem')
    expect(REPORT_APPROVAL_UNDECIDED_NOTE).toContain('§14 Q3')
    expect(REPORT_APPROVAL_UNDECIDED_NOTE).toContain('not when somebody')
  })

  it('separates the figures from the prose, per R1 and §9', () => {
    expect(FIGURES_NOTE).toContain('R1')
    expect(FIGURES_NOTE).toContain('system of record')
    expect(NARRATIVE_NOTE).toContain('assert_grounded')
    expect(NARRATIVE_NOTE).toContain('422')
    expect(NARRATIVE_NOTE).toContain('never quietly corrected')
  })

  it('states that a drafted report has no way out of the system (R3/R4)', () => {
    expect(NOTHING_SENDS_NOTE).toContain('goes nowhere')
    expect(NOTHING_SENDS_NOTE).toContain('shared_with_college_at')
    expect(NOTHING_SENDS_NOTE).toContain('DRAFT → PENDING_APPROVAL → APPROVED → RELEASED')
  })
})
