import { describe, expect, it } from 'vitest'
import { ApiError } from './api'
import {
  SUPPORTED_DIFF_VERSION,
  buildDiffView,
  isAuthorityUndefined,
  isConflict,
  isForbidden,
  isFrozenContentMismatch,
  isNotFound,
  isUnprocessable,
  linesChanged,
  parseDiff,
  templateSlots,
  COMMS_STATES,
  COMMS_STATE_LABEL,
  COMMS_STATE_BLURB,
  COMMS_CHANNELS,
  CHANNEL_LABEL,
  COMMS_RECIPIENT_KINDS,
  RECIPIENT_KIND_LABEL,
  RECIPIENT_KIND_CEILING,
} from './comms'
import type { DiffBlock, DiffOp, Hunk, TemplateDiff } from './comms'

/* =============================================================================
   The approval surface (§8), tested as logic.

   `buildDiffView` is what an approver actually reads before a college-facing
   message moves. A block it draws wrongly is a line somebody approved without
   seeing, so the elision rules get the same treatment a payout fixture does:
   exact expected block lists, not "it returned an array".
   ============================================================================= */

function hunkOf(op: DiffOp, at: number, template: string[], message: string[]): Hunk {
  return { op, at, template, message }
}

function diffOf(hunks: Hunk[], counts: Partial<TemplateDiff> = {}): TemplateDiff {
  return {
    version: 1,
    identical: hunks.length === 0,
    lines_added: 0,
    lines_removed: 0,
    template_lines: 0,
    message_lines: 0,
    hunks,
    ...counts,
  }
}

/** `L0\nL1\n…` — n lines whose text is their own baseline index. */
function baselineOf(n: number): string {
  return Array.from({ length: n }, (_, i) => `L${i}`).join('\n')
}

/** Every baseline line the view actually puts on screen, hunks aside. */
function renderedContext(blocks: DiffBlock[]): string[] {
  return blocks.flatMap((block) => (block.kind === 'context' ? block.lines : []))
}

describe('parseDiff', () => {
  const wire = {
    version: 1,
    identical: false,
    lines_added: 3,
    lines_removed: 2,
    template_lines: 9,
    message_lines: 10,
    hunks: [
      { op: 'changed', at: 4, template: ['old'], message: ['new'] },
      { op: 'added', at: 1, template: [], message: ['extra'] },
    ],
  }

  it('reads a well-formed diff and sorts hunks into template order', () => {
    const diff = parseDiff(wire)
    expect(diff).not.toBeNull()
    expect(diff?.hunks.map((h) => h.at)).toEqual([1, 4])
    expect(diff?.hunks[0]).toEqual({ op: 'added', at: 1, template: [], message: ['extra'] })
    expect(diff?.lines_added).toBe(3)
    expect(diff?.lines_removed).toBe(2)
    expect(diff?.identical).toBe(false)
  })

  it('does not mutate the caller’s hunk array while sorting', () => {
    const input = structuredClone(wire)
    parseDiff(input)
    expect(input.hunks.map((h) => h.at)).toEqual([4, 1])
  })

  it.each([
    ['null', null],
    ['undefined', undefined],
    ['an array', [{ version: 1 }]],
    ['a string', '{"version":1}'],
    ['a number', 1],
  ])('returns null for %s', (_label, value) => {
    expect(parseDiff(value)).toBeNull()
  })

  it.each([
    ['version missing', { identical: true, hunks: [] }],
    ['version not a number', { version: '1', identical: true, hunks: [] }],
    ['identical missing', { version: 1, hunks: [] }],
    ['identical not a boolean', { version: 1, identical: 'no', hunks: [] }],
    ['hunks missing', { version: 1, identical: true }],
    ['hunks not an array', { version: 1, identical: true, hunks: {} }],
  ])('returns null when %s', (_label, value) => {
    expect(parseDiff(value)).toBeNull()
  })

  it.each([
    ['op is not a DiffOp', { op: 'rewritten', at: 0, template: [], message: [] }],
    ['op is missing', { at: 0, template: [], message: [] }],
    ['at is not a number', { op: 'added', at: '0', template: [], message: [] }],
    ['the hunk is null', null],
    ['the hunk is an array', ['added', 0]],
  ])('rejects the WHOLE diff when %s', (_label, entry) => {
    expect(parseDiff({ version: 1, identical: false, hunks: [entry] })).toBeNull()
  })

  it('accepts each of the three DiffOps and nothing else', () => {
    for (const op of ['added', 'removed', 'changed']) {
      const parsed = parseDiff({
        version: 1,
        identical: false,
        hunks: [{ op, at: 0, template: [], message: [] }],
      })
      expect(parsed?.hunks[0].op).toBe(op)
    }
  })

  it('drops non-string entries from a hunk’s line arrays rather than rejecting', () => {
    const parsed = parseDiff({
      version: 1,
      identical: false,
      hunks: [{ op: 'changed', at: 0, template: ['keep', 7, null], message: 'not-an-array' }],
    })
    expect(parsed?.hunks[0].template).toEqual(['keep'])
    expect(parsed?.hunks[0].message).toEqual([])
  })

  it('defaults absent or mistyped counts to 0 instead of NaN', () => {
    const parsed = parseDiff({
      version: 1,
      identical: true,
      hunks: [],
      lines_added: 'three',
    })
    expect(parsed?.lines_added).toBe(0)
    expect(parsed?.lines_removed).toBe(0)
    expect(parsed?.template_lines).toBe(0)
    expect(parsed?.message_lines).toBe(0)
  })

  it('returns a newer version rather than refusing it, so the caller can warn', () => {
    const parsed = parseDiff({ ...wire, version: SUPPORTED_DIFF_VERSION + 1 })
    expect(parsed?.version).toBe(SUPPORTED_DIFF_VERSION + 1)
  })
})

describe('linesChanged', () => {
  it('counts a rewritten line as two — one read, one re-read', () => {
    expect(linesChanged(diffOf([], { lines_added: 3, lines_removed: 2 }))).toBe(5)
  })

  it('is 0 for an identical message', () => {
    expect(linesChanged(diffOf([]))).toBe(0)
  })
})

describe('buildDiffView', () => {
  it('interleaves a hunk with the baseline lines either side of it', () => {
    const blocks = buildDiffView(baselineOf(5), diffOf([hunkOf('changed', 2, ['L2'], ['new'])]))
    expect(blocks).toEqual([
      { kind: 'context', at: 0, lines: ['L0', 'L1'] },
      { kind: 'hunk', hunk: { op: 'changed', at: 2, template: ['L2'], message: ['new'] } },
      { kind: 'context', at: 3, lines: ['L3', 'L4'] },
    ])
  })

  it('elides the middle of a long run between two hunks, keeping context both sides', () => {
    const diff = diffOf([hunkOf('changed', 0, ['L0'], ['x']), hunkOf('changed', 15, ['L15'], ['y'])])
    const blocks = buildDiffView(baselineOf(20), diff)
    expect(blocks[1]).toEqual({ kind: 'context', at: 1, lines: ['L1', 'L2'] })
    expect(blocks[2]).toEqual({ kind: 'elided', hidden: 10 })
    // `at` on the tail block must index the BASELINE, not the run.
    expect(blocks[3]).toEqual({ kind: 'context', at: 13, lines: ['L13', 'L14'] })
  })

  /* -------------------------------------------------------------------------
     REGRESSION. `pushGap` ends with `lines.slice(-tail)`, and `slice(-0)` is
     `slice(0)` — the WHOLE array. Guardless, a trailing gap (tail = 0) pushed
     back every line the elision had just hidden, at a bogus index, so the
     approver saw the full boilerplate tail claiming to start at baseline.length.
     Both assertions below fail if the `tail > 0` guard is removed.
     ---------------------------------------------------------------------- */
  it('does not re-render the tail it just elided after the last hunk', () => {
    const blocks = buildDiffView(baselineOf(12), diffOf([hunkOf('changed', 1, ['L1'], ['x'])]))
    expect(blocks).toEqual([
      { kind: 'context', at: 0, lines: ['L0'] },
      { kind: 'hunk', hunk: { op: 'changed', at: 1, template: ['L1'], message: ['x'] } },
      { kind: 'context', at: 2, lines: ['L2', 'L3'] },
      { kind: 'elided', hidden: 8 },
    ])
    expect(blocks.at(-1)?.kind).toBe('elided')
    expect(renderedContext(blocks)).not.toContain('L11')
  })

  it('elides every gap when context is 0, including the ones with a hunk after them', () => {
    // The same `slice(-0)` trap on the LEADING side: keepTail is true here and
    // `tail` is still 0, so an unguarded tail block would dump the whole run.
    const diff = diffOf([hunkOf('changed', 4, ['L4'], ['x']), hunkOf('changed', 8, ['L8'], ['y'])])
    const blocks = buildDiffView(baselineOf(12), diff, 0)
    expect(blocks).toEqual([
      { kind: 'elided', hidden: 4 },
      { kind: 'hunk', hunk: { op: 'changed', at: 4, template: ['L4'], message: ['x'] } },
      { kind: 'elided', hidden: 3 },
      { kind: 'hunk', hunk: { op: 'changed', at: 8, template: ['L8'], message: ['y'] } },
      { kind: 'elided', hidden: 3 },
    ])
    expect(renderedContext(blocks)).toEqual([])
  })

  it('keeps a run of exactly 2 * context whole rather than eliding nothing usefully', () => {
    const diff = diffOf([hunkOf('changed', 0, ['L0'], ['x']), hunkOf('changed', 5, ['L5'], ['y'])])
    const blocks = buildDiffView(baselineOf(10), diff)
    expect(blocks[1]).toEqual({ kind: 'context', at: 1, lines: ['L1', 'L2', 'L3', 'L4'] })
    expect(blocks[2]?.kind).toBe('hunk')
  })

  it('elides a run one line longer than 2 * context', () => {
    const diff = diffOf([hunkOf('changed', 0, ['L0'], ['x']), hunkOf('changed', 6, ['L6'], ['y'])])
    const blocks = buildDiffView(baselineOf(10), diff)
    expect(blocks[1]).toEqual({ kind: 'context', at: 1, lines: ['L1', 'L2'] })
    expect(blocks[2]).toEqual({ kind: 'elided', hidden: 1 })
    expect(blocks[3]).toEqual({ kind: 'context', at: 4, lines: ['L4', 'L5'] })
  })

  it('clamps a hunk index past the end of the baseline', () => {
    const blocks = buildDiffView(baselineOf(3), diffOf([hunkOf('added', 99, [], ['appended'])]))
    expect(blocks).toEqual([
      { kind: 'context', at: 0, lines: ['L0', 'L1', 'L2'] },
      { kind: 'hunk', hunk: { op: 'added', at: 99, template: [], message: ['appended'] } },
    ])
  })

  it('never walks the cursor backwards when hunks overlap', () => {
    // Hunk A consumes L0–L4, so hunk B's recorded `at` of 2 is already behind the
    // cursor. Rewinding would re-render L2–L4 as context between the two hunks.
    const diff = diffOf([
      hunkOf('removed', 0, ['L0', 'L1', 'L2', 'L3', 'L4'], []),
      hunkOf('added', 2, [], ['inserted']),
    ])
    const blocks = buildDiffView(baselineOf(10), diff)
    expect(blocks[0]?.kind).toBe('hunk')
    expect(blocks[1]?.kind).toBe('hunk')
    expect(renderedContext(blocks)).toEqual(['L5', 'L6'])
  })

  it('rstrips baseline lines so context sits flush with the hunk text', () => {
    // diff.py rstrips before comparing; a trailing space left here would render a
    // context line differently from the hunk line beside it.
    expect(buildDiffView('A  \nB\t', diffOf([]))).toEqual([
      { kind: 'context', at: 0, lines: ['A', 'B'] },
    ])
  })

  it('renders the whole template as context when the message is identical', () => {
    const blocks = buildDiffView(baselineOf(2), diffOf([]))
    expect(blocks).toEqual([{ kind: 'context', at: 0, lines: ['L0', 'L1'] }])
  })

  it('reports an empty template body as a single empty line, not as nothing', () => {
    // ''.split('\n') is [''] — a one-line baseline. Silently dropping it would
    // make an empty template and a missing template look the same on screen.
    expect(buildDiffView('', diffOf([]))).toEqual([{ kind: 'context', at: 0, lines: [''] }])
  })
})

describe('templateSlots', () => {
  it('finds each {{slot}} once, sorted, regardless of order or repetition', () => {
    expect(templateSlots('Dear {{trainer_name}}, your {{month}} sheet for {{month}} is ready.')).toEqual([
      'month',
      'trainer_name',
    ])
  })

  it('tolerates whitespace inside the braces', () => {
    expect(templateSlots('{{  college_name  }}')).toEqual(['college_name'])
  })

  it('ignores things that are not slots', () => {
    expect(templateSlots('{single} {{1leading_digit}} {{has-dash}} {{}} plain text')).toEqual([])
  })

  it('accepts a leading underscore, digits after the first character', () => {
    expect(templateSlots('{{_internal}} {{batch2}}')).toEqual(['_internal', 'batch2'])
  })

  it('returns an empty list for a template with no slots', () => {
    expect(templateSlots('Nothing to fill in here.')).toEqual([])
  })

  it('gives the same answer when called twice on the same template', () => {
    // A module-scoped /g regex would carry lastIndex between calls; this one is
    // built per call, and that is worth pinning.
    const template = '{{a}} {{b}}'
    expect(templateSlots(template)).toEqual(templateSlots(template))
  })
})

describe('refusal predicates', () => {
  it('tells 501 (nobody has authority, §14 Q3) apart from every other refusal', () => {
    const notImplemented = new ApiError('Approval authority is undecided.', 501)
    expect(isAuthorityUndefined(notImplemented)).toBe(true)
    expect(isForbidden(notImplemented)).toBe(false)
    expect(isConflict(notImplemented)).toBe(false)
    expect(isNotFound(notImplemented)).toBe(false)
    expect(isUnprocessable(notImplemented)).toBe(false)
  })

  it.each<[number, (error: unknown) => boolean]>([
    [403, isForbidden],
    [409, isConflict],
    [422, isUnprocessable],
    [404, isNotFound],
  ])('matches %i and nothing else', (status, predicate) => {
    expect(predicate(new ApiError('x', status))).toBe(true)
    expect(predicate(new ApiError('x', status + 1))).toBe(false)
  })

  it('returns false for anything that is not an ApiError', () => {
    for (const value of [new Error('403'), { status: 403 }, '403', null, undefined]) {
      expect(isForbidden(value)).toBe(false)
      expect(isAuthorityUndefined(value)).toBe(false)
    }
  })

  it('recognises the freeze catching changed content, case-insensitively', () => {
    expect(isFrozenContentMismatch(new ApiError('Frozen payload hash mismatch', 409))).toBe(true)
    expect(isFrozenContentMismatch(new ApiError('content HASH differs', 409))).toBe(true)
  })

  it('does not read every 409 as a hash mismatch', () => {
    expect(isFrozenContentMismatch(new ApiError('Illegal transition APPROVED -> DRAFT', 409))).toBe(
      false,
    )
  })

  it('requires the 409 — a hash mentioned in a 403 is not a freeze failure', () => {
    expect(isFrozenContentMismatch(new ApiError('hash', 403))).toBe(false)
    expect(isFrozenContentMismatch(new Error('frozen'))).toBe(false)
  })
})

describe('vocabulary lists cover their label maps', () => {
  // Record<T, string> makes the compiler check the MAPS. Nothing checks that the
  // ARRAYS the pickers iterate are complete, so a state added to the type and
  // the map but forgotten here would simply never be selectable.
  it('COMMS_STATES lists every labelled state, in R4 ladder order', () => {
    expect(COMMS_STATES).toEqual(['DRAFT', 'PENDING_APPROVAL', 'APPROVED', 'RELEASED'])
    expect(COMMS_STATES).toEqual(Object.keys(COMMS_STATE_LABEL))
    expect(COMMS_STATES).toEqual(Object.keys(COMMS_STATE_BLURB))
  })

  it('COMMS_CHANNELS lists every labelled channel', () => {
    expect([...COMMS_CHANNELS].sort()).toEqual(Object.keys(CHANNEL_LABEL).sort())
  })

  it('COMMS_RECIPIENT_KINDS lists every labelled kind and every §8 ceiling', () => {
    expect([...COMMS_RECIPIENT_KINDS].sort()).toEqual(Object.keys(RECIPIENT_KIND_LABEL).sort())
    expect([...COMMS_RECIPIENT_KINDS].sort()).toEqual(Object.keys(RECIPIENT_KIND_CEILING).sort())
  })

  it('never lets a state label or blurb claim something was sent', () => {
    // §8/R3: no provider exists in this phase. Every mention of sending in this
    // vocabulary must be a negation, because the system cannot make the claim.
    for (const text of [
      ...Object.values(COMMS_STATE_LABEL),
      ...Object.values(COMMS_STATE_BLURB),
    ]) {
      for (const sentence of text.split(/(?<=[.—])/)) {
        if (/\bsent\b/i.test(sentence)) expect(sentence).toMatch(/\bnot\b/i)
      }
    }
    expect(COMMS_STATE_LABEL.RELEASED).toContain('not sent')
    expect(COMMS_STATE_BLURB.RELEASED).toContain('Still not sent')
  })
})
