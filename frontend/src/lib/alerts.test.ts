import { describe, expect, it } from 'vitest'
import { BANDS, BAND_LABEL, SEVERITY_LABEL, TIER_LABEL, alertKeys, codeLabel, scoreOf } from './alerts'
import type { DeploymentRisk } from './alerts'

/* =============================================================================
   The alert feed's two pure helpers, plus the vocabulary the feed is sorted by.
   ============================================================================= */

describe('codeLabel', () => {
  it('turns a snake_case code into a sentence-cased label', () => {
    expect(codeLabel('attendance_unmarked_days')).toBe('Attendance unmarked days')
    expect(codeLabel('syllabus_behind_schedule')).toBe('Syllabus behind schedule')
  })

  it('derives rather than tabulates, so an unseen code still reads', () => {
    // The point of deriving: a code a migration adds tomorrow renders as an
    // ugly-but-readable label instead of as a blank cell.
    expect(codeLabel('a_code_nobody_has_written_yet')).toBe('A code nobody has written yet')
  })

  it('leaves a single word alone but for its first letter', () => {
    expect(codeLabel('usage')).toBe('Usage')
    expect(codeLabel('a')).toBe('A')
  })

  it('does not lowercase anything the code already capitalised', () => {
    expect(codeLabel('crt_attendance_gap')).toBe('Crt attendance gap')
    expect(codeLabel('PAN_missing')).toBe('PAN missing')
  })

  it('returns an empty string for an empty code rather than throwing', () => {
    expect(codeLabel('')).toBe('')
  })
})

describe('scoreOf', () => {
  it('reads a Decimal string as a number for ordering', () => {
    expect(scoreOf('72.5')).toBe(72.5)
    expect(scoreOf('0')).toBe(0)
    expect(scoreOf('-3.25')).toBe(-3.25)
    expect(scoreOf('  7  ')).toBe(7)
  })

  it('sorts a feed of risk scores highest first', () => {
    const rows = ['9.5', '81', '7', '100.25'].map((score) => ({ score }))
    const ordered = [...rows].sort((a, b) => scoreOf(b.score) - scoreOf(a.score))
    // String ordering would put '9.5' above '81'. That is the bug this exists for.
    expect(ordered.map((r) => r.score)).toEqual(['100.25', '81', '9.5', '7'])
  })

  it.each(['', 'abc', 'NaN', 'Infinity', '-Infinity', '12,5', '₹80,000'])(
    'sinks unreadable value %j to 0 instead of poisoning the sort with NaN',
    (value) => {
      expect(scoreOf(value)).toBe(0)
    },
  )

  it('is for ordering only, and is not safe for money (R7)', () => {
    // Two Decimal strings whose float sum is not their decimal sum. This is why
    // `scoreOf` is documented "never maths", and why nothing that reaches a
    // rupee goes through it.
    expect(scoreOf('0.1') + scoreOf('0.2')).not.toBe(0.3)
    expect(scoreOf('0.1') + scoreOf('0.2')).toBeCloseTo(0.3, 10)
  })

  it('does not silently round a value it cannot hold exactly', () => {
    // A 20-digit Decimal survives as a string on the wire and loses digits the
    // moment it becomes a float — visible here so nobody mistakes scoreOf for a
    // parser.
    expect(String(scoreOf('12345678901234567890.5'))).not.toBe('12345678901234567890.5')
  })
})

describe('band, severity and tier vocabulary', () => {
  it('orders BANDS worst-first, which is the order the feed is worked in', () => {
    expect(BANDS).toEqual(['critical', 'high', 'medium', 'low'])
  })

  it('labels every band it lists', () => {
    expect([...BANDS].sort()).toEqual(Object.keys(BAND_LABEL).sort())
  })

  it('keeps severity to the three levels risk.py defines, not five', () => {
    expect(Object.keys(SEVERITY_LABEL).sort()).toEqual(['critical', 'info', 'warning'])
    expect(SEVERITY_LABEL.info).toBe('Note')
  })

  it('names only the three internal rungs of the §4 chain', () => {
    // §8: the Delivery Monitor alerts INTERNALLY. A college or trainer tier here
    // would be a route out of the system that §4's chain does not have.
    expect(Object.keys(TIER_LABEL).sort()).toEqual(['lde_executive', 'manager', 'senior_manager'])
    expect(Object.values(TIER_LABEL).join(' ')).not.toMatch(/college|trainer/i)
  })

  it('sorts a mixed feed into BANDS order', () => {
    const feed: Pick<DeploymentRisk, 'band'>[] = [
      { band: 'low' },
      { band: 'critical' },
      { band: 'medium' },
      { band: 'high' },
    ]
    const ordered = [...feed].sort((a, b) => BANDS.indexOf(a.band) - BANDS.indexOf(b.band))
    expect(ordered.map((row) => row.band)).toEqual(['critical', 'high', 'medium', 'low'])
  })
})

describe('alertKeys', () => {
  it('keys the feed by its params so two scopes do not share a cache entry', () => {
    expect(alertKeys.feed({ programId: 'p1' })).not.toEqual(alertKeys.feed({ programId: 'p2' }))
    expect(alertKeys.feed({ periodStart: '2026-07-01' })).not.toEqual(
      alertKeys.feed({ periodStart: '2026-08-01' }),
    )
  })

  it('defaults to one shared entry for the unfiltered feed', () => {
    expect(alertKeys.feed()).toEqual(alertKeys.feed({}))
    expect(alertKeys.feed()).toEqual(['monitoring', 'alerts', {}])
  })

  it('stays under the monitoring namespace', () => {
    expect(alertKeys.all).toEqual(['monitoring'])
    expect(alertKeys.rules()[0]).toBe('monitoring')
  })
})
