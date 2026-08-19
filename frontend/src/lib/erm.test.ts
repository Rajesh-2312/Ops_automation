import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from './api'
import {
  COPY_OUTCOME_LABEL,
  ERM_OPEN_STATES,
  ERM_STATES,
  ERM_STATE_BLURB,
  ERM_STATE_LABEL,
  ERM_SUBJECT_KINDS,
  SUBJECT_KIND_LABEL,
  copyText,
  ermKeys,
  isConflict,
  isForbidden,
  isNotFound,
  isUnprocessable,
} from './erm'

/* =============================================================================
   The ERM screen's one real interaction: getting a field pack onto a clipboard.

   §10 models ERM as a human pasting values into another window, so a Copy button
   that silently no-ops is a broken feature, not a cosmetic one. `copyText`
   degrades through three rungs, and the rung it lands on decides what the screen
   tells the person. That LADDER is what is tested here.

   NO jsdom. The three globals `copyText` touches — `navigator`, `window`,
   `document` — are stubbed with hand-written objects, so what is under test is
   the decision tree and the cleanup, not a DOM implementation's clipboard.
   ============================================================================= */

interface FakeTextarea {
  tag: string
  value: string
  style: Record<string, string>
  attributes: Record<string, string>
  selectedRange: [number, number] | null
  selected: boolean
  removed: boolean
  setAttribute(name: string, value: string): void
  select(): void
  setSelectionRange(start: number, end: number): void
  remove(): void
}

interface FakeDom {
  doc: unknown
  created: FakeTextarea[]
  appended: FakeTextarea[]
  commands: string[]
  previousRange: { label: string }
  restored: unknown[]
  clears(): number
}

/** A `document` with exactly the surface `copyText` reaches for. */
function fakeDom(execCommand: (command: string) => boolean, rangeCount = 1): FakeDom {
  const created: FakeTextarea[] = []
  const appended: FakeTextarea[] = []
  const commands: string[] = []
  const restored: unknown[] = []
  const previousRange = { label: 'what the user had highlighted' }
  let clears = 0

  const selection = {
    rangeCount,
    getRangeAt: (): unknown => previousRange,
    removeAllRanges: (): void => {
      clears += 1
    },
    addRange: (range: unknown): void => {
      restored.push(range)
    },
  }

  const doc = {
    createElement: (tag: string): FakeTextarea => {
      const element: FakeTextarea = {
        tag,
        value: '',
        style: {},
        attributes: {},
        selectedRange: null,
        selected: false,
        removed: false,
        setAttribute(name: string, value: string): void {
          element.attributes[name] = value
        },
        select(): void {
          element.selected = true
        },
        setSelectionRange(start: number, end: number): void {
          element.selectedRange = [start, end]
        },
        remove(): void {
          element.removed = true
        },
      }
      created.push(element)
      return element
    },
    body: {
      appendChild: (element: FakeTextarea): void => {
        appended.push(element)
      },
    },
    getSelection: (): unknown => selection,
    execCommand: (command: string): boolean => {
      commands.push(command)
      return execCommand(command)
    },
  }

  return { doc, created, appended, commands, previousRange, restored, clears: () => clears }
}

/** A field pack is multi-line and long — the shape actually being copied. */
const PACK = ['Trainer name: VEMA PRUDHVI SAI', 'PAN: ABCDE1234F', 'IFSC: SBIN0001234'].join('\n')

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('copyText — rung 1, the async Clipboard API', () => {
  it('uses the clipboard on a secure origin and reports it', async () => {
    const writeText = vi.fn(async () => {})
    vi.stubGlobal('navigator', { clipboard: { writeText } })
    vi.stubGlobal('window', { isSecureContext: true })
    const dom = fakeDom(() => true)
    vi.stubGlobal('document', dom.doc)

    expect(await copyText(PACK)).toBe('clipboard')
    expect(writeText).toHaveBeenCalledExactlyOnceWith(PACK)
    // Rung 2 must not run as well — no scratch element, no execCommand.
    expect(dom.created).toHaveLength(0)
    expect(dom.commands).toEqual([])
  })
})

describe('copyText — rung 2, execCommand over a detached textarea', () => {
  it('skips the clipboard entirely on an insecure origin (http://192.168.x.x)', async () => {
    const writeText = vi.fn(async () => {})
    vi.stubGlobal('navigator', { clipboard: { writeText } })
    vi.stubGlobal('window', { isSecureContext: false })
    const dom = fakeDom(() => true)
    vi.stubGlobal('document', dom.doc)

    expect(await copyText(PACK)).toBe('legacy')
    expect(writeText).not.toHaveBeenCalled()
    expect(dom.commands).toEqual(['copy'])
  })

  it('falls through when the clipboard write is refused', async () => {
    const writeText = vi.fn(async () => {
      throw new Error('Document is not focused')
    })
    vi.stubGlobal('navigator', { clipboard: { writeText } })
    vi.stubGlobal('window', { isSecureContext: true })
    const dom = fakeDom(() => true)
    vi.stubGlobal('document', dom.doc)

    expect(await copyText(PACK)).toBe('legacy')
    expect(writeText).toHaveBeenCalledOnce()
    expect(dom.commands).toEqual(['copy'])
  })

  it('falls through when the browser exposes no clipboard object at all', async () => {
    vi.stubGlobal('navigator', {})
    vi.stubGlobal('window', { isSecureContext: true })
    const dom = fakeDom(() => true)
    vi.stubGlobal('document', dom.doc)

    expect(await copyText(PACK)).toBe('legacy')
  })

  it('puts the whole text in the scratch element and selects all of it', async () => {
    vi.stubGlobal('navigator', {})
    vi.stubGlobal('window', { isSecureContext: false })
    const dom = fakeDom(() => true)
    vi.stubGlobal('document', dom.doc)

    await copyText(PACK)
    const scratch = dom.created[0]
    expect(scratch.tag).toBe('textarea')
    expect(scratch.value).toBe(PACK)
    expect(scratch.attributes).toHaveProperty('readonly')
    expect(scratch.selected).toBe(true)
    // A short range would copy a truncated pack — the failure §10 cannot afford.
    expect(scratch.selectedRange).toEqual([0, PACK.length])
    expect(dom.appended).toEqual([scratch])
  })

  it('keeps the scratch element off-screen without making it unselectable', async () => {
    vi.stubGlobal('navigator', {})
    vi.stubGlobal('window', { isSecureContext: false })
    const dom = fakeDom(() => true)
    vi.stubGlobal('document', dom.doc)

    await copyText(PACK)
    expect(dom.created[0].style).toMatchObject({ position: 'fixed', left: '-9999px' })
    // `display: none` would make it unselectable and the copy would silently fail.
    expect(dom.created[0].style.display).toBeUndefined()
  })
})

describe('copyText — rung 3, and the cleanup that must happen either way', () => {
  it('reports unavailable when execCommand declines', async () => {
    vi.stubGlobal('navigator', {})
    vi.stubGlobal('window', { isSecureContext: false })
    const dom = fakeDom(() => false)
    vi.stubGlobal('document', dom.doc)

    expect(await copyText(PACK)).toBe('unavailable')
  })

  it('reports unavailable when execCommand throws instead of returning false', async () => {
    vi.stubGlobal('navigator', {})
    vi.stubGlobal('window', { isSecureContext: false })
    const dom = fakeDom(() => {
      throw new Error('not allowed')
    })
    vi.stubGlobal('document', dom.doc)

    expect(await copyText(PACK)).toBe('unavailable')
  })

  it.each([
    ['succeeds', () => true],
    ['declines', () => false],
    [
      'throws',
      () => {
        throw new Error('not allowed')
      },
    ],
  ])('removes the scratch element when execCommand %s', async (_label, execCommand) => {
    vi.stubGlobal('navigator', {})
    vi.stubGlobal('window', { isSecureContext: false })
    const dom = fakeDom(execCommand as () => boolean)
    vi.stubGlobal('document', dom.doc)

    await copyText(PACK)
    expect(dom.created).toHaveLength(1)
    expect(dom.created[0].removed).toBe(true)
  })

  it('restores the selection the copy ate', async () => {
    vi.stubGlobal('navigator', {})
    vi.stubGlobal('window', { isSecureContext: false })
    const dom = fakeDom(() => true)
    vi.stubGlobal('document', dom.doc)

    await copyText(PACK)
    expect(dom.clears()).toBe(1)
    expect(dom.restored).toEqual([dom.previousRange])
  })

  it('does not invent a selection when the user had none', async () => {
    vi.stubGlobal('navigator', {})
    vi.stubGlobal('window', { isSecureContext: false })
    const dom = fakeDom(() => true, 0)
    vi.stubGlobal('document', dom.doc)

    await copyText(PACK)
    expect(dom.restored).toEqual([])
    expect(dom.clears()).toBe(0)
  })

  it('reports unavailable rather than throwing where there is no DOM at all', async () => {
    vi.stubGlobal('navigator', {})
    vi.stubGlobal('window', { isSecureContext: true })
    expect(await copyText(PACK)).toBe('unavailable')
  })

  it('does not pretend an unavailable copy succeeded', async () => {
    expect(COPY_OUTCOME_LABEL.clipboard).toBe('Copied')
    expect(COPY_OUTCOME_LABEL.legacy).toBe('Copied')
    expect(COPY_OUTCOME_LABEL.unavailable).not.toBe('Copied')
    expect(COPY_OUTCOME_LABEL.unavailable).toContain('Ctrl-C')
  })
})

describe('ERM vocabulary', () => {
  it('lists every labelled and blurbed state', () => {
    expect(ERM_STATES).toEqual(Object.keys(ERM_STATE_LABEL))
    expect(ERM_STATES).toEqual(Object.keys(ERM_STATE_BLURB))
    expect(ERM_SUBJECT_KINDS).toEqual(Object.keys(SUBJECT_KIND_LABEL))
  })

  it('treats only queued and assigned as workable, never a terminal state', () => {
    expect(ERM_OPEN_STATES).toEqual(['queued', 'assigned'])
    for (const state of ERM_OPEN_STATES) expect(ERM_STATES).toContain(state)
    for (const terminal of ['confirmed', 'stale', 'cancelled']) {
      expect(ERM_OPEN_STATES).not.toContain(terminal)
    }
  })

  it('says stale means drift, not a failed push (§10)', () => {
    // The misreading this wording exists to prevent: `stale` does NOT mean the
    // sync failed. It means it succeeded and the local record moved after.
    expect(ERM_STATE_LABEL.stale).toContain('diverged')
    expect(ERM_STATE_BLURB.stale).toContain('happened and was correct')
    expect(ERM_STATE_BLURB.stale.toLowerCase()).not.toContain('failed')
  })
})

describe('ermKeys', () => {
  it('gives two different filters two different cache entries', () => {
    const queued = ermKeys.queue({ state: 'queued' })
    const confirmed = ermKeys.queue({ state: 'confirmed' })
    expect(queued).not.toEqual(confirmed)
    expect(ermKeys.queue({ assigned_to_me: true })).not.toEqual(
      ermKeys.queue({ assigned_to_me: false }),
    )
    expect(ermKeys.queue({ subject_kind: 'trainer' })).not.toEqual(
      ermKeys.queue({ subject_kind: 'program' }),
    )
  })

  it('gives the same filter the same entry, whether written as null or omitted', () => {
    expect(ermKeys.queue({})).toEqual(['erm', 'queue', 'all', 'all', 'everyone', 200])
    expect(ermKeys.queue({ state: null, subject_kind: null })).toEqual(ermKeys.queue({}))
  })

  // fetchErmQueue SENDS `limit`, so it has to be in the key. It was not, and two
  // queries differing only in limit shared one cache entry — the narrower one was
  // served the wider one's rows. That is a wrong-data bug rather than a staleness
  // one, so it gets its own test instead of riding along in the shape test above.
  it('separates two queues that differ only in limit', () => {
    expect(ermKeys.queue({ limit: 50 })).not.toEqual(ermKeys.queue({ limit: 200 }))
  })

  it('treats an omitted limit as the default fetchErmQueue would send', () => {
    expect(ermKeys.queue({})).toEqual(ermKeys.queue({ limit: 200 }))
  })

  it('stays under its own namespace so another screen cannot invalidate it', () => {
    expect(ermKeys.all).toEqual(['erm'])
    expect(ermKeys.task('abc')[0]).toBe('erm')
    expect(ermKeys.actors(500)[0]).toBe('erm')
    expect(ermKeys.subjects('trainer')).toEqual(['erm', 'subjects', 'trainer'])
  })
})

describe('ERM refusal predicates', () => {
  it.each<[number, (error: unknown) => boolean]>([
    [403, isForbidden],
    [409, isConflict],
    [404, isNotFound],
    [422, isUnprocessable],
  ])('matches %i and no neighbouring status', (status, predicate) => {
    expect(predicate(new ApiError('x', status))).toBe(true)
    expect(predicate(new ApiError('x', status - 1))).toBe(false)
    expect(predicate(new ApiError('x', status + 1))).toBe(false)
  })

  it('ignores anything that is not an ApiError', () => {
    expect(isForbidden(new Error('403 Forbidden'))).toBe(false)
    expect(isConflict({ status: 409 })).toBe(false)
    expect(isNotFound(undefined)).toBe(false)
  })
})
