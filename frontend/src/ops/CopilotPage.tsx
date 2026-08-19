import { useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery } from '@tanstack/react-query'
import { useAuth } from '../auth/AuthProvider'
import { Page, PageHeader } from '../components/AppShell'
import {
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorNote,
  HelpTip,
  InfoNote,
  Loading,
  MonoValue,
  PageIntro,
  Spinner,
  Textarea,
  Toolbar,
} from '../components/ui'
import {
  CORPUS_LABEL,
  DEFAULT_LIMIT,
  REFUSAL_IS_GUARDRAIL,
  REFUSAL_TITLE,
  ask,
  copilotErrorMessage,
  copilotKeys,
  myCorpora,
  splitAnswer,
  type AskResponse,
  type Citation,
  type Corpus,
  type CorpusAccess,
} from '../lib/copilot'

/* --------------------------------------------------------------------------
   The Ops Copilot screen.

   THIS IS NOT A CHAT BOX, AND THE LAYOUT IS THE ARGUMENT.

   CLAUDE.md §9: "Structured facts (dates, amounts, counts) are never retrieved
   from RAG. Query the database." A blank prompt saying "Ask me anything" is a
   promise the backend will refuse to keep — it teaches a Manager to type "how
   many payable days does Prudhvi have", which `app/rag/guards.py` refuses
   before retrieval, and a person who is refused twice stops opening the screen.
   So the boundary is drawn on the page ITSELF, above the box: what it answers,
   what it will never answer, and where those other answers actually live. The
   seed questions are all policy-shaped for the same reason — the fastest way to
   teach the shape of a tool is to hand someone a question that works.

   CITATIONS ARE NOT A FOOTNOTE. §9: "Every answer cites source document and
   section. No citation → no answer." The sources therefore get their own panel
   at the same visual weight as the prose, not a grey line beneath it. On a wide
   screen they sit BESIDE the answer so a reader checking a claim never has to
   scroll away from it. A superseded contract version is flagged in the panel
   and in the answer's own marker, because §9 forbids a superseded clause
   surfacing unflagged.

   REFUSALS ARE NOT ERRORS. The API returns 200 with `answered: false`, and this
   screen renders that as an explanation with somewhere else to go. The only red
   on this page is a transport failure or a guardrail trip (`uncited`,
   `invalid_citation`, `fabricated_figure`) — those three mean a model produced
   something that failed a gate, which is worth looking louder than "ask the
   tracksheet instead".

   NOTHING HERE FILTERS. The persona wall is `app/rag/scope.py`, applied inside
   the same SQL statement as the ranking. The corpus list below is drawn from
   `GET /copilot/corpora` and is descriptive — it tells you what you hold; it
   does not decide it (R5).

   WHERE `flame` GOES ON THIS SCREEN. On the answer prose, and nowhere else.
   DESIGN.md §2 reserves the token for wording a language model produced, and an
   answer body is the only thing here that qualifies: the sentences are the
   model's, assembled from retrieved passages. The sources panel is not flame —
   a citation is a row from the index, and a reader has to be able to trust it
   differently from the prose it supports. The `facts` block is not flame either
   and is deliberately drawn in `accent`, the app's ordinary colour for stored
   data, because those values came out of SQL. That is §9's "hybrid answers must
   visibly separate the two", done in colour rather than in a caption someone
   can skip: warm means a model wrote it, cool means the database said it.
-------------------------------------------------------------------------- */

/** One question and its outcome, kept for the session only. */
interface Exchange {
  id: number
  question: string
  response: AskResponse
}

/**
 * Seed questions. Every one is policy-shaped and every one is drawn from a rule
 * that genuinely lives in a document rather than in a row — §5's rate-basis
 * branch, §7's gates, R4's approval ladder. None asks for a value.
 */
const EXAMPLES: readonly string[] = [
  'How are payable days counted for a bCAP trainer versus a CRT trainer?',
  'What does the SOP say about a college holiday during a bCAP engagement?',
  'Which validation gates block a payout cycle from reaching approval?',
  'What is the approval process for a college-facing report?',
  'How is a work order period meant to relate to the payout period?',
]

export function CopilotPage() {
  const { canSeeCommercials } = useAuth()
  const [question, setQuestion] = useState('')
  const [selected, setSelected] = useState<Set<Corpus>>(new Set())
  const [includeSuperseded, setIncludeSuperseded] = useState(false)
  const [exchanges, setExchanges] = useState<Exchange[]>([])
  const nextId = useRef(1)

  const {
    data: access = [],
    isPending: accessPending,
    error: accessError,
  } = useQuery({
    queryKey: copilotKeys.corpora(),
    queryFn: myCorpora,
    staleTime: 5 * 60_000,
  })

  const held = useMemo(() => access.map((a) => a.corpus), [access])

  const askMutation = useMutation({
    mutationFn: async (text: string): Promise<Exchange> => {
      const response = await ask({
        question: text,
        // Empty selection means "everything I hold" — the ACL is the ceiling
        // either way, so sending no `corpora` is not asking for more.
        corpora: selected.size > 0 ? [...selected] : undefined,
        limit: DEFAULT_LIMIT,
        include_superseded: includeSuperseded,
      })
      return { id: nextId.current++, question: text, response }
    },
    onSuccess: (exchange) => {
      setExchanges((prev) => [exchange, ...prev])
      setQuestion('')
    },
  })

  const trimmed = question.trim()
  const canAsk = trimmed.length >= 3 && !askMutation.isPending

  function submit() {
    if (!canAsk) return
    askMutation.mutate(trimmed)
  }

  function toggleCorpus(corpus: Corpus) {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(corpus)) next.delete(corpus)
      else next.add(corpus)
      return next
    })
  }

  const contractsHeld = held.includes('contracts')

  return (
    <>
      <PageHeader
        title="Ops Copilot"
        purpose="Ask how something is supposed to work and get an answer quoted out of our own SOPs and signed contracts, with the document and section it came from. It only reads and answers — it cannot change a record, draft a message, or tell anyone anything."
        subtitle="Policy and process questions — answered with a source, or refused. Read-only."
        actions={
          exchanges.length > 0 ? (
            <Button size="sm" variant="ghost" onClick={() => setExchanges([])}>
              Clear session
            </Button>
          ) : undefined
        }
      />

      <Page>
        <div className="max-w-4xl space-y-4">
          <PageIntro
            steps={[
              'You ask in plain English',
              'It searches only what your persona holds',
              'It answers from the passages it found',
              'Every claim carries its source',
            ]}
          >
            <p>
              This is a{' '}
              <HelpTip term="RAG">
                Retrieval-Augmented Generation. The model is not answering from memory: the
                question is first used to search our own documents, and only the passages
                that come back are given to it to write from. That is why it can quote a
                section number, and why it goes quiet on subjects we have not indexed.
              </HelpTip>{' '}
              search over six separately permissioned{' '}
              <HelpTip term="corpora">
                A corpus is one indexed body of documents. There are six — SOPs, contracts,
                college dossiers, educator records, curriculum and reports — and they are
                permissioned one by one, so holding one says nothing about holding another.
                Your persona decides which you can search at all.
              </HelpTip>
              .
            </p>
            <p className="mt-2">
              <strong className="font-medium text-ink">
                No citation means no answer.
              </strong>{' '}
              If the model writes something it cannot attribute to a document and a section,
              the answer is thrown away rather than shown to you — so a blank result here is
              the safeguard working. The wording of an answer is the model's and is marked
              in orange throughout this console; anything drawn in blue was read from the
              database instead.
            </p>
          </PageIntro>

          <ScopeNote canSeeCommercials={canSeeCommercials} />

          <Card className="p-4">
            <label className="block">
              <span className="block text-xs font-medium text-ink-2 mb-1.5">
                Ask a policy question
              </span>
              <Textarea
                rows={3}
                value={question}
                maxLength={2000}
                placeholder="e.g. How is a college holiday treated for a bCAP trainer?"
                onChange={(e) => setQuestion(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
                    e.preventDefault()
                    submit()
                  }
                }}
              />
            </label>

            <div className="mt-3 space-y-3">
              <CorpusPicker
                access={access}
                pending={accessPending}
                error={accessError}
                selected={selected}
                onToggle={toggleCorpus}
                onClear={() => setSelected(new Set())}
              />

              {contractsHeld && (
                <label className="flex items-start gap-2 text-xs text-ink-2 cursor-pointer">
                  <input
                    type="checkbox"
                    className="mt-0.5 h-3.5 w-3.5 shrink-0 accent-[var(--color-accent)]"
                    checked={includeSuperseded}
                    onChange={(e) => setIncludeSuperseded(e.target.checked)}
                  />
                  <span>
                    Search superseded contract versions.
                    <span className="text-ink-3">
                      {' '}
                      They are always flagged as superseded in the sources panel; this only
                      controls whether they are searched at all.
                    </span>
                  </span>
                </label>
              )}

              <div className="flex items-center justify-between gap-3">
                <p className="text-[11px] text-ink-3">
                  {trimmed.length > 0 && trimmed.length < 3
                    ? 'A question needs at least three characters.'
                    : 'Ctrl/⌘ + Enter to ask. Nothing you ask here is written to any record.'}
                </p>
                <Button variant="primary" disabled={!canAsk} onClick={submit}>
                  {askMutation.isPending && <Spinner />}
                  {askMutation.isPending ? 'Searching…' : 'Ask'}
                </Button>
              </div>
            </div>
          </Card>

          {askMutation.error && (
            <ErrorNote>{copilotErrorMessage(askMutation.error)}</ErrorNote>
          )}

          {exchanges.length === 0 && !askMutation.isPending && (
            <Card>
              <EmptyState
                title="Nothing asked yet"
                body="Start with one of these. Each is a question the documents can actually answer — a rule, not a value."
                hint="Questions and answers live in this tab only. Nothing is saved, nothing is sent, and reloading the page clears the lot."
              />
              <ul className="border-t border-line divide-y divide-line-soft">
                {EXAMPLES.map((example) => (
                  <li key={example}>
                    <button
                      type="button"
                      onClick={() => setQuestion(example)}
                      className="w-full text-left px-4 py-2.5 text-sm text-accent
                        hover:bg-surface-2/70 transition"
                    >
                      {example}
                    </button>
                  </li>
                ))}
              </ul>
            </Card>
          )}

          {askMutation.isPending && <Loading label="Searching the corpora you hold" />}

          {exchanges.map((exchange) => (
            <ExchangeCard key={exchange.id} exchange={exchange} />
          ))}
        </div>
      </Page>
    </>
  )
}

// --- the boundary, stated up front ------------------------------------------

/**
 * The two-column "answers / does not answer" note.
 *
 * Deliberately above the input rather than beside the results: it has to be
 * read BEFORE the first question, because the cost of the wrong first question
 * is a refusal that reads as a broken tool.
 */
function ScopeNote({ canSeeCommercials }: { canSeeCommercials: boolean }) {
  return (
    <Card className="p-4">
      <div className="grid gap-4 sm:grid-cols-2">
        <div>
          <p className="text-xs font-medium text-ink flex items-center gap-1.5">
            <span aria-hidden>📄</span> Answers, with a citation
          </p>
          <ul className="mt-1.5 space-y-1 text-xs text-ink-2 leading-relaxed">
            <li>How a rule works — payable days, holidays, rate basis</li>
            <li>What an SOP or a signed contract says, and which section</li>
            <li>Process and approval questions</li>
            <li>College history and program context from the dossier</li>
          </ul>
        </div>
        <div>
          <p className="text-xs font-medium text-ink flex items-center gap-1.5">
            <span aria-hidden>🚫</span> Refuses, every time
          </p>
          <ul className="mt-1.5 space-y-1 text-xs text-ink-2 leading-relaxed">
            <li>Amounts, counts and dates — anyone's payable days, net pay, TDS</li>
            <li>Attendance figures and syllabus percentages</li>
            <li>Anything whose answer is a row rather than a rule</li>
          </ul>
          <p className="mt-2 text-[11px] text-ink-3 leading-relaxed">
            Those come from{' '}
            <Link to="/attendance" className="text-accent hover:underline">
              Attendance
            </Link>
            {canSeeCommercials && (
              <>
                {' and '}
                <Link to="/payouts" className="text-accent hover:underline">
                  Payouts
                </Link>
              </>
            )}
            , which read the systems of record.
          </p>
        </div>
      </div>
      <div className="mt-3">
        <InfoNote>
          A number read out of a document is not the number of record and may be months
          stale, so the Copilot refuses numeric questions before it even searches. It
          cannot send a message, save a draft, or change a single record — reading
          documents and citing them is the entire list of what it does.
        </InfoNote>
      </div>
    </Card>
  )
}

// --- corpus picker -----------------------------------------------------------

function CorpusPicker({
  access,
  pending,
  error,
  selected,
  onToggle,
  onClear,
}: {
  access: CorpusAccess[]
  pending: boolean
  error: unknown
  selected: Set<Corpus>
  onToggle: (corpus: Corpus) => void
  onClear: () => void
}) {
  if (pending) {
    return <p className="text-[11px] text-ink-3">Loading the corpora you hold…</p>
  }
  if (error) {
    return <ErrorNote>{copilotErrorMessage(error)}</ErrorNote>
  }
  if (access.length === 0) {
    return (
      <InfoNote>
        Your role holds none of the six document collections, so every question will be
        refused. This is the access model working rather than a fault — each collection is
        granted separately, and yours currently has none. Ask your Manager if you believe
        you should hold one.
      </InfoNote>
    )
  }

  return (
    <div>
      <Toolbar className="gap-1.5">
        <span className="text-[11px] text-ink-3 mr-0.5">Search:</span>
        <button
          type="button"
          onClick={onClear}
          className={`rounded-lg border px-2.5 h-7 text-xs font-medium transition ${
            selected.size === 0
              ? 'bg-accent-soft border-accent/40 text-accent-ink'
              : 'bg-surface border-line text-ink-2 hover:border-accent/40 hover:text-ink'
          }`}
        >
          Everything I hold
        </button>
        {access.map((entry) => {
          const active = selected.has(entry.corpus)
          return (
            <button
              key={entry.corpus}
              type="button"
              title={entry.rationale}
              onClick={() => onToggle(entry.corpus)}
              className={`rounded-lg border px-2.5 h-7 text-xs font-medium transition ${
                active
                  ? 'bg-accent-soft border-accent/40 text-accent-ink'
                  : 'bg-surface border-line text-ink-2 hover:border-accent/40 hover:text-ink'
              }`}
            >
              {CORPUS_LABEL[entry.corpus]}
            </button>
          )
        })}
      </Toolbar>
      <p className="text-[11px] text-ink-3 mt-1.5">
        Narrowing only. You hold {access.length} of 6 corpora; the rest are not searchable
        for your persona whatever is selected here. Hover a corpus for why you hold it.
      </p>
    </div>
  )
}

// --- one question and its outcome -------------------------------------------

function ExchangeCard({ exchange }: { exchange: Exchange }) {
  const { question, response } = exchange
  return (
    <Card className="overflow-hidden">
      <div className="border-b border-line px-4 py-3">
        <p className="text-sm text-ink leading-snug">{question}</p>
      </div>
      {response.answered && response.answer ? (
        <AnswerBody answer={response.answer} citations={response.citations} facts={response.facts} />
      ) : (
        <RefusalBody response={response} />
      )}
    </Card>
  )
}

function AnswerBody({
  answer,
  citations,
  facts,
}: {
  answer: string
  citations: Citation[]
  facts: Record<string, string>
}) {
  const byMarker = useMemo(
    () => new Map(citations.map((c) => [c.marker, c])),
    [citations],
  )
  const factEntries = Object.entries(facts)

  return (
    <div className="grid gap-0 lg:grid-cols-[minmax(0,1fr)_20rem]">
      <div className="p-4 min-w-0">
        {/* THE FLAME RAIL. Everything inside this border is wording a language
            model produced, and the rail runs the full height of it so a reader
            skimming down the card can see exactly where the model's voice
            starts and stops. A wash behind the text was the other option and
            was rejected: this is the paragraph people read most carefully on
            the screen, and tinting a reading surface costs contrast for a
            signal a 2px rail already carries. */}
        <div className="border-l-2 border-flame/40 pl-3">
          <p className="flex items-center gap-2 mb-1.5">
            <Badge tone="flame">Generated wording</Badge>
            <span className="text-[11px] text-ink-3">
              Written by a model from the sources listed
            </span>
          </p>
          <p className="whitespace-pre-wrap text-sm text-ink leading-relaxed">
            {splitAnswer(answer).map((segment, i) =>
              segment.kind === 'text' ? (
                <span key={i}>{segment.text}</span>
              ) : (
                <CitationMarker
                  key={i}
                  marker={segment.marker}
                  citation={byMarker.get(segment.marker)}
                />
              ),
            )}
          </p>
        </div>

        {factEntries.length > 0 && (
          <div className="mt-4 rounded-lg border border-accent/30 bg-accent-soft/40 p-3">
            <p className="text-[11px] font-medium uppercase tracking-wide text-accent-ink">
              Values from the system of record
            </p>
            {/* §9: hybrid answers must VISIBLY separate policy prose from stored
                values. These came from a SQL query the service ran; they are
                never retrieved from a document and never merged into the prose
                above by this screen.

                Sitting OUTSIDE the flame rail is the whole point of where this
                block is in the markup. Move it inside and the screen starts
                claiming a model produced these figures, which is the one thing
                R1 exists to prevent. */}
            <dl className="mt-2 space-y-1">
              {factEntries.map(([key, value]) => (
                <div key={key} className="flex items-baseline justify-between gap-3">
                  <dt className="text-xs text-ink-2">{key}</dt>
                  <dd className="text-xs font-medium tabular-nums text-ink">{value}</dd>
                </div>
              ))}
            </dl>
            <p className="mt-2 text-[11px] text-ink-3 leading-relaxed">
              Queried from the database, not read out of a document and not written by the
              model. These are the figures of record; the paragraph above is only an
              explanation of them.
            </p>
          </div>
        )}
      </div>

      <SourcesPanel citations={citations} />
    </div>
  )
}

/**
 * An inline `[n]`, rendered as something you can hover to see what it points at.
 *
 * A marker with no matching citation cannot reach here — `check_citations()`
 * discards any answer whose markers do not resolve — so the unresolved branch
 * is drawn as a visible fault rather than quietly as plain text.
 */
function CitationMarker({ marker, citation }: { marker: number; citation?: Citation }) {
  if (!citation) {
    return (
      <sup className="mx-0.5 rounded bg-bad-wash px-1 text-[10px] font-medium text-bad-ink">
        [{marker}] unresolved
      </sup>
    )
  }
  return (
    <sup
      title={`${citation.document} — ${citation.section}${
        citation.is_superseded ? ' (superseded version)' : ''
      }`}
      className={`mx-0.5 rounded px-1 text-[10px] font-medium cursor-help ${
        citation.is_superseded
          ? 'bg-warn-wash text-warn-ink'
          : 'bg-accent-soft text-accent-ink'
      }`}
    >
      [{marker}]
    </sup>
  )
}

/**
 * The trust mechanism, at full weight.
 *
 * Beside the prose on a wide screen and directly beneath it on a narrow one, so
 * checking a claim is never a scroll away from the claim. Document and section
 * are both shown because §9 requires both — a document name alone sends someone
 * to a forty-page SOP.
 */
function SourcesPanel({ citations }: { citations: Citation[] }) {
  return (
    <aside className="border-t border-line bg-surface-2/50 p-4 lg:border-t-0 lg:border-l">
      <p className="text-[11px] font-medium uppercase tracking-wide text-ink-3">
        <HelpTip term={`Sources · ${citations.length}`}>
          The documents the answer was written from. Each numbered marker in the prose
          points at one of these, and every claim has to carry one — an answer whose
          markers do not resolve to a real document and section is discarded before it
          reaches you.
        </HelpTip>
      </p>
      <ol className="mt-2.5 space-y-2.5">
        {citations.map((citation) => (
          <li key={citation.marker} className="flex gap-2">
            <span className="shrink-0 mt-0.5 rounded bg-accent-soft px-1 text-[10px] font-medium text-accent-ink h-4 leading-4">
              {citation.marker}
            </span>
            <div className="min-w-0">
              <p className="text-xs font-medium text-ink leading-snug break-words">
                {citation.document}
              </p>
              <p className="text-[11px] text-ink-2 leading-snug break-words">
                {citation.section}
              </p>
              <div className="flex flex-wrap items-center gap-1 mt-1">
                <Badge>{CORPUS_LABEL[citation.corpus]}</Badge>
                <Badge>v{citation.version}</Badge>
                {citation.is_superseded && <Badge tone="warn">Superseded</Badge>}
              </div>
            </div>
          </li>
        ))}
      </ol>
      <p className="mt-3 text-[11px] text-ink-3 leading-relaxed">
        Every answer names a document and a section. One that cannot be attributed is
        thrown away rather than shown to you — no citation, no answer.
      </p>
    </aside>
  )
}

/**
 * A refusal, rendered as an outcome rather than a failure.
 *
 * The server's `message` is shown verbatim: it is written for the person who
 * asked and names where the answer actually lives, which is the whole point of
 * refusing rather than hedging.
 */
function RefusalBody({ response }: { response: AskResponse }) {
  const refusal = response.refusal
  if (!refusal) {
    // Not reachable through `app/api/copilot.py` — an unanswered response always
    // carries a refusal. Rendered honestly rather than as a blank card, because
    // a blank card is the shape a silent failure takes.
    return (
      <div className="p-4">
        <ErrorNote>
          The Copilot returned no answer and no reason. Nothing is wrong with your question
          — this is a fault worth reporting.
        </ErrorNote>
      </div>
    )
  }

  const loud = REFUSAL_IS_GUARDRAIL[refusal.reason]
  return (
    <div className="p-4">
      <div
        className={`rounded-lg border p-3 ${
          loud
            ? 'border-bad/30 bg-bad-wash'
            : 'border-warn/30 bg-warn-wash'
        }`}
      >
        <p
          className={`text-sm font-medium ${
            loud ? 'text-bad-ink' : 'text-warn-ink'
          }`}
        >
          {REFUSAL_TITLE[refusal.reason]}
        </p>
        <p className="mt-1.5 text-xs text-ink-2 leading-relaxed">{refusal.message}</p>
        {/* The reason code is an identifier people quote when they ask why they
            were refused, so it gets the monospaced face the rest of the app's
            identifiers get. */}
        {refusal.reason === 'structured_fact' && (
          <p className="mt-2 text-[11px] text-ink-3">
            Reason code: <MonoValue className="text-ink-2">{refusal.reason}</MonoValue> ·
            this is the boundary holding, not a fault.
          </p>
        )}
        {loud && (
          <p className="mt-2 text-[11px] text-ink-3">
            Reason code: <MonoValue className="text-ink-2">{refusal.reason}</MonoValue> ·
            a model did write something, and it was discarded because it failed a check.
            Nothing was saved or sent. The attempt is logged.
          </p>
        )}
      </div>
    </div>
  )
}
