import { describe, expect, it } from 'vitest'
import { ApiError } from './api'
import {
  CORPORA,
  CORPUS_LABEL,
  DEFAULT_LIMIT,
  MAX_LIMIT,
  REFUSAL_IS_GUARDRAIL,
  REFUSAL_TITLE,
  copilotErrorMessage,
  copilotKeys,
  splitAnswer,
} from './copilot'
import type { AnswerSegment, RefusalReason } from './copilot'

/* =============================================================================
   Citation marker parsing and refusal classification.

   §9: "Every answer cites source document and section. No citation → no answer."
   A marker the server validated but this splitter did not recognise renders as
   literal `[3]` and loses its link to the source — the one thing that makes the
   answer trustworthy. So the splitter is tested against the boundaries of
   `_CITATION_RE` in guards.py, not just the happy sentence.
   ============================================================================= */

/** Reassemble the input from its segments. Anything lost shows up here. */
function reassemble(segments: AnswerSegment[]): string {
  return segments.map((s) => (s.kind === 'text' ? s.text : `[${s.marker}]`)).join('')
}

describe('splitAnswer', () => {
  it('splits prose around a marker without losing the surrounding text', () => {
    expect(splitAnswer('A signed WO is required [1] before payout.')).toEqual([
      { kind: 'text', text: 'A signed WO is required ' },
      { kind: 'citation', marker: 1 },
      { kind: 'text', text: ' before payout.' },
    ])
  })

  it('emits no empty text segment when a marker leads', () => {
    expect(splitAnswer('[1] says so.')).toEqual([
      { kind: 'citation', marker: 1 },
      { kind: 'text', text: ' says so.' },
    ])
  })

  it('emits no empty text segment when a marker trails', () => {
    expect(splitAnswer('Per the SOP [2]')).toEqual([
      { kind: 'text', text: 'Per the SOP ' },
      { kind: 'citation', marker: 2 },
    ])
  })

  it('keeps adjacent markers as separate citations with nothing between them', () => {
    expect(splitAnswer('Both apply [1][2].')).toEqual([
      { kind: 'text', text: 'Both apply ' },
      { kind: 'citation', marker: 1 },
      { kind: 'citation', marker: 2 },
      { kind: 'text', text: '.' },
    ])
  })

  it('returns one text segment when there is nothing to cite', () => {
    expect(splitAnswer('No markers here at all.')).toEqual([
      { kind: 'text', text: 'No markers here at all.' },
    ])
  })

  it('returns nothing for an empty answer', () => {
    expect(splitAnswer('')).toEqual([])
  })

  it('reads two-digit markers, and refuses three — the guards.py bound', () => {
    // `_CITATION_RE` is \d{1,2}. A `[123]` is not a citation, and must stay text
    // rather than becoming a link to citation 12 with a stray "3" beside it.
    expect(splitAnswer('[12]')).toEqual([{ kind: 'citation', marker: 12 }])
    expect(splitAnswer('[123]')).toEqual([{ kind: 'text', text: '[123]' }])
  })

  it.each(['[]', '[a]', '[ 1 ]', '[1', '1]', '{1}', '[-1]', '[1.5]'])(
    'leaves %s as prose rather than treating it as a marker',
    (input) => {
      expect(splitAnswer(input)).toEqual([{ kind: 'text', text: input }])
    },
  )

  it.each([
    'Plain prose.',
    'Leading [1] and trailing [2]',
    '[1][2][3]',
    'Newlines\nare\tpreserved [4] verbatim.',
    '  double  spaces  [5]  survive  ',
  ])('round-trips %j losslessly', (answer) => {
    expect(reassemble(splitAnswer(answer))).toBe(answer)
  })

  it('normalises a zero-padded marker to its number', () => {
    // Lossy on purpose — the marker is an index into `citations`, not a label.
    expect(splitAnswer('[01]')).toEqual([{ kind: 'citation', marker: 1 }])
  })

  it('does not resolve markers against a citation list', () => {
    // §9 note in the source: a marker with no matching citation cannot reach the
    // browser (check_citations discards that whole answer), so the splitter
    // renders the marker and lets a lookup miss be visible rather than eating it.
    expect(splitAnswer('[99]')).toEqual([{ kind: 'citation', marker: 99 }])
  })
})

describe('refusal vocabulary', () => {
  const REASONS: RefusalReason[] = [
    'structured_fact',
    'no_sources',
    'uncited',
    'invalid_citation',
    'fabricated_figure',
    'no_corpus_access',
  ]

  it('titles every reason guards.py can return, with no two alike', () => {
    expect(Object.keys(REFUSAL_TITLE).sort()).toEqual([...REASONS].sort())
    expect(new Set(Object.values(REFUSAL_TITLE)).size).toBe(REASONS.length)
  })

  it('classifies the three model-failure refusals as guardrail hits', () => {
    // uncited / invalid_citation / fabricated_figure mean a model produced
    // something that failed a gate — a run of them is a defect, not a usage
    // pattern, and the screen shows them louder.
    expect(REFUSAL_IS_GUARDRAIL.uncited).toBe(true)
    expect(REFUSAL_IS_GUARDRAIL.invalid_citation).toBe(true)
    expect(REFUSAL_IS_GUARDRAIL.fabricated_figure).toBe(true)
  })

  it('classifies the three "ask differently" refusals as the boundary working', () => {
    expect(REFUSAL_IS_GUARDRAIL.structured_fact).toBe(false)
    expect(REFUSAL_IS_GUARDRAIL.no_sources).toBe(false)
    expect(REFUSAL_IS_GUARDRAIL.no_corpus_access).toBe(false)
  })

  it('classifies every reason it titles', () => {
    expect(Object.keys(REFUSAL_IS_GUARDRAIL).sort()).toEqual(Object.keys(REFUSAL_TITLE).sort())
  })

  it('sends a structured-fact question to the database, per §9', () => {
    expect(REFUSAL_TITLE.structured_fact.toLowerCase()).toContain('database')
  })
})

describe('corpora', () => {
  it('offers all six separately-permissioned corpora (§9)', () => {
    // Record<Corpus, string> makes the compiler check CORPUS_LABEL. Nothing
    // checks CORPORA, and a corpus missing from it is simply unselectable.
    expect([...CORPORA].sort()).toEqual(Object.keys(CORPUS_LABEL).sort())
    expect(CORPORA).toHaveLength(6)
  })

  it("keeps the default result count inside the API's hard cap", () => {
    expect(DEFAULT_LIMIT).toBeLessThanOrEqual(MAX_LIMIT)
    expect(DEFAULT_LIMIT).toBeGreaterThan(0)
  })
})

describe('copilotErrorMessage', () => {
  it('passes a 503 through verbatim — it names an unconfigured deployment', () => {
    const message = 'The Copilot is not configured: OPENROUTER_API_KEY is unset.'
    expect(copilotErrorMessage(new ApiError(message, 503))).toBe(message)
  })

  it('replaces a 403 with the persona explanation rather than the server prose', () => {
    const said = copilotErrorMessage(new ApiError('Forbidden', 403))
    expect(said).not.toBe('Forbidden')
    expect(said).toContain('internal staff only')
  })

  it('passes any other ApiError through with its own message', () => {
    expect(copilotErrorMessage(new ApiError('Boom', 500))).toBe('Boom')
    expect(copilotErrorMessage(new ApiError('question: field required', 422))).toBe(
      'question: field required',
    )
  })

  it('reads a plain Error and a non-Error alike', () => {
    expect(copilotErrorMessage(new Error('network down'))).toBe('network down')
    expect(copilotErrorMessage('just a string')).toBe('just a string')
    expect(copilotErrorMessage(null)).toBe('null')
    expect(copilotErrorMessage(undefined)).toBe('undefined')
  })
})

describe('copilotKeys', () => {
  it('namespaces every key under "copilot" so a report invalidation cannot reach it', () => {
    expect(copilotKeys.all[0]).toBe('copilot')
    expect(copilotKeys.corpora()[0]).toBe('copilot')
  })
})
