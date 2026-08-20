import { describe, expect, it } from 'vitest'
import { describeErrorBody } from './api'

/* =============================================================================
   What a failed request is allowed to say to the person who made it.
   =============================================================================

   THE BUG THIS FILE EXISTS FOR
   ============================
   The Ops Copilot, deployed to GitHub Pages with no backend behind it, rendered
   an error box containing GitHub's entire 404 page: doctype, a Content-Security
   -Policy meta tag, two hundred columns of inline CSS, two base64 logos. The
   one sentence in there that mattered — "There isn't a GitHub Pages site here"
   — was four hundred characters in, and even that sentence describes GitHub's
   routing rather than the actual problem, which is that VITE_API_BASE_URL was
   never set so the request went to the static host instead of to FastAPI.

   It read as a Copilot fault. It was a deployment fact.

   The cause was one line repeated at four call sites: a bare `catch` whose
   comment read "body was not JSON; use it verbatim". Verbatim is right for a
   body FastAPI wrote and wrong for a body it did not, and "did not parse as
   JSON" is precisely the signal that FastAPI never saw the request at all.

   These tests are all on the pure describer, so none of them mocks `fetch` or a
   Supabase session — the thing that broke was the message, not the transport.
   ============================================================================= */

/** The real body, trimmed. The parts that matter are the doctype and the bulk. */
const GITHUB_404 = `<!DOCTYPE html>
<html>
  <head>
    <meta http-equiv="Content-type" content="text/html; charset=utf-8">
    <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:; connect-src 'self'">
    <title>Site not found &middot; GitHub Pages</title>
    <style type="text/css" media="screen">
      body { background-color: #f1f1f1; margin: 0; font-family: "Helvetica Neue", Helvetica, Arial, sans-serif; }
      .container { margin: 50px auto 40px auto; width: 600px; text-align: center; }
    </style>
  </head>
  <body>
    <div class="container"><h1>404</h1>
      <p><strong>There isn't a GitHub Pages site here.</strong></p>
    </div>
  </body>
</html>`

describe('an HTML body means the request never reached FastAPI', () => {
  it('does not put the markup in front of the user', () => {
    const message = describeErrorBody(GITHUB_404, 404, 'Not Found')

    expect(message).not.toContain('<!DOCTYPE')
    expect(message).not.toContain('<style')
    expect(message).not.toContain('base64')
    // The regression was length as much as content: the box was unreadable.
    expect(message.length).toBeLessThan(GITHUB_404.length)
  })

  it('names the actual problem, which is a missing backend', () => {
    const message = describeErrorBody(GITHUB_404, 404, 'Not Found')

    expect(message).toContain('No API is deployed')
    // The variable to set. Without it the reader has a diagnosis and no fix.
    expect(message).toContain('VITE_API_BASE_URL')
  })

  it('keeps the status, because 404 and 502 mean different things here', () => {
    // 404: nothing is routed at that path. 502: something is, and it is down.
    expect(describeErrorBody(GITHUB_404, 404, 'Not Found')).toContain('404')
    expect(describeErrorBody('<html><body>Bad gateway</body></html>', 502, 'Bad Gateway')).toContain(
      '502',
    )
  })

  it('recognises HTML that does not start at character zero', () => {
    // Some hosts emit a leading newline or a BOM-adjacent blank line. A body
    // that fails the doctype test falls through to being pasted whole, which
    // is the bug returning by the back door.
    const message = describeErrorBody(`\n\n  ${GITHUB_404}`, 404, 'Not Found')
    expect(message).toContain('No API is deployed')
  })
})

describe('a JSON body is still unwrapped exactly as it was', () => {
  it('reads a raised HTTPException detail', () => {
    const body = JSON.stringify({ detail: 'Attendance is incomplete for 3 days in this period.' })
    expect(describeErrorBody(body, 422, 'Unprocessable Entity')).toBe(
      'Attendance is incomplete for 3 days in this period.',
    )
  })

  it("flattens Pydantic's 422 list into fields a person can find", () => {
    // Rendering this as [object Object] is how "period spans two months"
    // becomes a mystery ticket.
    const body = JSON.stringify({
      detail: [
        { loc: ['body', 'period_start'], msg: 'must be the first of the month' },
        { loc: ['body', 'rate'], msg: 'must be greater than 0' },
      ],
    })
    expect(describeErrorBody(body, 422, 'Unprocessable Entity')).toBe(
      'period_start: must be the first of the month · rate: must be greater than 0',
    )
  })

  it('falls back to `message` when there is no `detail`', () => {
    const body = JSON.stringify({ message: 'Rate limit exceeded.' })
    expect(describeErrorBody(body, 429, 'Too Many Requests')).toBe('Rate limit exceeded.')
  })

  it('shows JSON that carries neither key rather than swallowing it', () => {
    // An unfamiliar shape is still evidence. Only HTML is diagnosed away.
    const body = '{"error":"upstream_timeout"}'
    expect(describeErrorBody(body, 504, 'Gateway Timeout')).toBe(body)
  })
})

describe('everything else', () => {
  it('falls back to the status line when the body is empty', () => {
    expect(describeErrorBody('', 503, 'Service Unavailable')).toBe('503 Service Unavailable')
    expect(describeErrorBody('   \n ', 503, 'Service Unavailable')).toBe('503 Service Unavailable')
  })

  it('truncates a long plain-text body instead of pasting it whole', () => {
    // A proxy stack trace is the usual source. Same failure mode as the 404
    // page, minus the doctype that would have caught it.
    const trace = 'Traceback (most recent call last):\n'.repeat(60)
    const message = describeErrorBody(trace, 500, 'Internal Server Error')

    expect(message.length).toBeLessThan(trace.length)
    expect(message.endsWith('…')).toBe(true)
    // Truncated, not discarded — the first frames are the useful ones.
    expect(message).toContain('Traceback (most recent call last)')
  })

  it('leaves a short plain-text body alone', () => {
    expect(describeErrorBody('upstream connect error', 502, 'Bad Gateway')).toBe(
      'upstream connect error',
    )
  })
})
