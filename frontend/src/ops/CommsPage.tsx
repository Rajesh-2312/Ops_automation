import { useMemo, useState, type ReactNode } from 'react'
import { PAGE, bounded, boundedFromServer } from '../lib/bounds'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { errorMessage, supabase } from '../lib/supabase'
import { qk } from '../lib/queryKeys'
import { fetchActorNames } from '../lib/approvals'
import type { Program } from '../lib/types'
import {
  CHANNEL_LABEL,
  COMMS_AUTHORITY_UNDECIDED_NOTE,
  COMMS_CHANNELS,
  COMMS_RECIPIENT_KINDS,
  COMMS_STATES,
  COMMS_STATE_BLURB,
  COMMS_STATE_LABEL,
  RECIPIENT_KIND_CEILING,
  RECIPIENT_KIND_LABEL,
  RELEASE_IS_NOT_SEND_NOTE,
  SUPPORTED_DIFF_VERSION,
  amendMessage,
  approveMessage,
  buildDiffView,
  commsKeys,
  draftMessage,
  fetchCommsMessage,
  fetchCommsQueue,
  isAuthorityUndefined,
  isConflict,
  isForbidden,
  isFrozenContentMismatch,
  isNotFound,
  isUnprocessable,
  linesChanged,
  parseDiff,
  rejectMessage,
  releaseMessage,
  submitMessage,
  supersedeMessage,
  templateSlots,
  type ArtifactState,
  type ArtifactType,
  type CommsChannel,
  type CommsMessage,
  type CommsRecipientKind,
  type DiffBlock,
  type Hunk,
  type JsonValue,
  type TemplateDiff,
} from '../lib/comms'
import { useAuth } from '../auth/AuthProvider'
import { Page, PageHeader } from '../components/AppShell'
import {
  Badge,
  BoundNote,
  Button,
  Card,
  EmptyState,
  ErrorNote,
  Field,
  FilterChip,
  fmtDate,
  HelpTip,
  InfoNote,
  Input,
  KeyValue,
  Legend,
  Loading,
  Modal,
  MonoValue,
  PageIntro,
  SearchInput,
  SectionTitle,
  Select,
  Spinner,
  TableSkeleton,
  Td,
  Textarea,
  Th,
  Toolbar,
} from '../components/ui'

/* --------------------------------------------------------------------------
   Comms queue — CLAUDE.md §8's single outbound queue, made operable.

     "Comms Service — single outbound queue. Channel, recipient, template, and
      diff-from-template shown at approval."

   `app/api/comms.py`, `app/services/comms/` and `1700_comms_queue.sql` have
   existed with no caller. This screen is that caller. Four things about it are
   structural rather than cosmetic:

   1. THE DIFF IS THE SCREEN. Everything else on the panel is context for it. An
      approver asked to re-read a whole message reads the first two lines and
      approves the rest; an approver shown "three lines differ from the template,
      here they are" is reviewing the thing that actually carries risk — what the
      drafter, usually a model, wrote of its own. The diff is READ from
      `comms_messages.diff`, never recomputed here: it was computed by
      `diff.py` over the baseline and the body, it is frozen by the content hash
      at approval, and a browser-side recomputation would eventually disagree with
      what the approver was actually shown.

   2. APPROVE, REJECT AND RELEASE ARE DRAWN, AND THEY RETURN 501. §14 Q3 —
      "Approval authority for college-facing comms: Manager or Senior Manager?" —
      is an open question owned by Rajesh Maroju, so `COMMS_APPROVAL_AUTHORITY` is
      empty and the service refuses. This screen says so BEFORE the click, in a
      panel that names the question, and says it again AFTER the click with the
      server's own wording. The buttons are not hidden: hiding them would describe
      a system where those actions do not exist, and they do — they are
      implemented, tested, and waiting on one decision. They are not disabled
      either: the block is a decision, not a permission, and the day the question
      is answered the server starts accepting these calls without anyone having to
      remember to re-enable a button here.

   3. NOTHING ON THIS SCREEN TRANSMITS. There is no send button because there is
      no send. `POST .../release` marks state and writes an audit row; comms.py has
      no provider behind it and no `sent_at` column to stamp. Both the approval
      panel and the release panel say so in as many words, because "released"
      reads as "sent" to everybody who has not read the migration.

   4. THE SERVER DECIDES. `useAuth()` is used for LAYOUT ONLY, per its own
      docstring. The wall is `require_internal()` / `require_commercials()` in
      comms.py mirroring 1700's two policies (R5); what this file draws changes
      nothing about what is reachable.
-------------------------------------------------------------------------- */

type ProgramOption = Program & { colleges: { name: string } | null }

const STATE_TONE: Record<ArtifactState, string> = {
  DRAFT: 'bg-surface-2 text-ink-3 border-line',
  PENDING_APPROVAL: 'bg-warn-wash text-warn-ink border-warn/25',
  APPROVED: 'bg-info-wash text-info-ink border-info/25',
  RELEASED: 'bg-good-wash text-good-ink border-good/25',
}

const PILL =
  'inline-flex items-center rounded-md border px-1.5 py-0.5 text-[11px] font-medium whitespace-nowrap'

function StatePill({ state }: { state: ArtifactState }) {
  return <span className={`${PILL} ${STATE_TONE[state]}`}>{COMMS_STATE_LABEL[state]}</span>
}

/** The order the queue is worked in: what is waiting on a human, then the rest. */
const STATE_ORDER: ArtifactState[] = ['PENDING_APPROVAL', 'DRAFT', 'APPROVED', 'RELEASED']

export function CommsPage() {
  const { isInternal, canSeeCommercials } = useAuth()
  const [programId, setProgramId] = useState('')
  const [filter, setFilter] = useState<ArtifactState | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [composing, setComposing] = useState(false)
  const [query, setQuery] = useState('')

  const programs = useQuery({
    queryKey: qk.programs.list(PAGE.programs),
    enabled: isInternal,
    queryFn: () =>
      bounded<ProgramOption>(PAGE.programs, (rows) =>
        supabase.from('programs').select('*, colleges(name)').order('name').limit(rows),
      ),
  })

  // No `state` param: the filter chips need counts for every state, so the
  // queue is fetched whole for one program and grouped here.
  //
  // The limit is now PASSED rather than defaulted, and it is in the key. It was
  // previously neither: `fetchCommsQueue` defaulted to 200 and sent it, and
  // `commsKeys.queue` did not carry it — the same shape as the erm.ts bug,
  // latent only because this was the single call site. Both are required
  // arguments now, so the request and the cache entry cannot disagree.
  const queue = useQuery({
    queryKey: commsKeys.queue(programId, null, PAGE.comms),
    enabled: isInternal && programId !== '',
    queryFn: () => fetchCommsQueue(programId, null, PAGE.comms),
  })

  const actors = useQuery({
    queryKey: commsKeys.actors(PAGE.profiles),
    enabled: isInternal,
    queryFn: () => fetchActorNames(PAGE.profiles),
  })

  const actorName = useMemo(() => {
    const byId = new Map((actors.data?.rows ?? []).map((p) => [p.id, p.full_name]))
    // The uuid, not "Unknown", when a name cannot be resolved: the id is what is
    // on the audit row and it is what answers a dispute.
    return (id: string | null): string => (id ? (byId.get(id) ?? id) : '—')
  }, [actors.data])

  const messages = useMemo(() => queue.data?.messages ?? [], [queue.data])

  // The API takes a limit and cannot be asked for limit+1, so "at the cap" is
  // the honest phrasing here rather than "there are more" — see
  // `boundedFromServer`.
  const messagesBound = boundedFromServer(messages, PAGE.comms)

  const counts = useMemo(() => {
    const tally = { DRAFT: 0, PENDING_APPROVAL: 0, APPROVED: 0, RELEASED: 0 }
    for (const m of messages) tally[m.state] += 1
    return tally
  }, [messages])

  // The search is a PRESENTATION filter over rows already fetched — no extra
  // request, no extra cache key, nothing narrowed server-side. It reads the four
  // fields somebody actually arrives holding: an address off a thread, a name, a
  // subject line off a forward, or a template key out of an SOP.
  const rows = useMemo(() => {
    const needle = query.trim().toLowerCase()
    const visible = messages.filter((m) => {
      if (filter && m.state !== filter) return false
      if (needle === '') return true
      return [m.recipient_name, m.recipient_ref, m.subject, m.template_key].some((field) =>
        (field ?? '').toLowerCase().includes(needle),
      )
    })
    return [...visible].sort((a, b) => STATE_ORDER.indexOf(a.state) - STATE_ORDER.indexOf(b.state))
  }, [messages, filter, query])

  // --- The wall ------------------------------------------------------------
  // Every route behind this screen calls `require_internal()` before it reads a
  // row, so a college login would get the same 403 for every message id. Drawing
  // a queue for them would be presenting an action that cannot work.
  if (!isInternal) {
    return (
      <>
        <PageHeader
          title="Comms queue"
          subtitle="Internal staff only"
          purpose="Where byteXL staff write messages and check them before a person approves them. A college reads a message where it arrives — in its own inbox — never from inside this console, so there is nothing here for your account."
        />
        <Page>
          <div className="max-w-3xl">
            <InfoNote>
              The outbound queue is internal governance. Every endpoint behind this screen calls{' '}
              <MonoValue>require_internal()</MonoValue> before it reads a row, so there is
              deliberately no queue here rather than one that would refuse every action. A college
              sees a message when it arrives through a channel it already uses — not from inside
              this console.
            </InfoNote>
          </div>
        </Page>
      </>
    )
  }

  const selected = selectedId

  return (
    <>
      <PageHeader
        title="Comms queue"
        subtitle="One queue for everything outbound. Drafted, diffed against its template, approved by a human — and today, stopped at pending approval."
        purpose="Every message this platform would put in front of a college, a trainer or a colleague is written here first, compared line by line with the template it came from, and approved by a person. Nothing on this screen transmits: there is no send button because there is no send."
        actions={
          <Button
            variant="primary"
            disabled={programId === ''}
            onClick={() => setComposing(true)}
            title={programId === '' ? 'Choose a program first — the queue is program-scoped.' : undefined}
          >
            Draft a message
          </Button>
        }
      />

      <Page>
        <div className="space-y-4">
          {/* WHAT THIS SCREEN IS, SAID BEFORE ANYTHING IS CLICKED.
              Two misreadings cost more than everything else on this page put
              together: that "approve" means "send", and that something behind
              this queue is capable of sending at all. Neither is true, and
              neither is inferable from a list of rows — so both are stated
              here, in the order a first-time reader meets them. */}
          <PageIntro
            steps={[
              'Draft — written, amended, re-diffed',
              'Pending approval — waiting on a person',
              'Approved — frozen and hashed',
              'Released — a second, separate human act',
            ]}
          >
            <p>
              This is the single outbound queue. Every message to a college, a trainer or an
              internal team is a row here before it is anything else: a drafter writes it against
              a{' '}
              <HelpTip term="template">
                Fixed wording with <MonoValue>{'{{slot}}'}</MonoValue> markers left in it for the
                facts. No conditionals, no loops, no arithmetic — a template that can compute is a
                template that can compute money.
              </HelpTip>
              , the server fills that template's{' '}
              <HelpTip term="slots">
                Named holes in the template, filled from structured input the caller passed in —
                a date, a count, an amount read out of a system of record. The database owns
                truth, the language model owns language (R1); a figure a model produced is not
                allowed to reach a recipient.
              </HelpTip>{' '}
              and works out the{' '}
              <HelpTip term="diff">
                The lines where the finished message and the filled-in template disagree. It is
                what an approver is asked to read: the template was already agreed, so the diff
                is the only part carrying new risk.
              </HelpTip>
              , and an approver reads the part the drafter wrote of its own instead of re-reading
              the whole message.
            </p>
            <p className="mt-2">
              <strong className="text-ink">
                Approving is not releasing, and releasing is not sending.
              </strong>{' '}
              Approving freezes the message and hashes it. Releasing is a second act, taken later,
              by a person who could still refuse, and it writes its own separate audit row (R4).
              Neither one puts anything on a wire: no email or WhatsApp provider is wired behind
              this screen, there is no <MonoValue>sent_at</MonoValue> column to stamp, and there
              is no send control anywhere in this console.
            </p>
            <p className="mt-2">
              No agent can send either, and that is not a matter of trust. An agent's tools are
              read-only plus <MonoValue>save_draft</MonoValue> — there is no send tool bound to
              any agent graph to call (R3). Agents draft; people release.
            </p>
          </PageIntro>

          <OpenQuestionBanner />

          <Card className="p-4">
            <SectionTitle
              title="Program"
              subtitle="Required. The queue is program-scoped because reach is: comms.py resolves your college from programs.college_id and checks it once, before anything is read."
            />
            {programs.error ? (
              <ErrorNote>{errorMessage(programs.error)}</ErrorNote>
            ) : (
              <div className="max-w-xl">
                <Field
                  label="Which program's outbound queue"
                  hint="Only programs your college and cluster assignments reach are listed — that list comes from the database, not from this screen (R5)."
                >
                  <Select
                    value={programId}
                    disabled={programs.isPending}
                    onChange={(e) => {
                      setProgramId(e.target.value)
                      setSelectedId(null)
                    }}
                  >
                    <option value="">
                      {programs.isPending ? 'Loading programs…' : 'Select a program…'}
                    </option>
                    {(programs.data?.rows ?? []).map((p) => (
                      <option key={p.id} value={p.id}>
                        {p.colleges?.name ? `${p.colleges.name} · ` : ''}
                        {p.name} ({p.type})
                      </option>
                    ))}
                  </Select>
                  <div className="mt-2">
                    <BoundNote
                      bound={programs.data}
                      noun="programs"
                      derived="A program beyond that has no reachable queue on this screen."
                    />
                  </div>
                </Field>
              </div>
            )}
          </Card>

          <LifecycleLegend />

          {programId === '' ? (
            <Card>
              <EmptyState
                title="Choose a program"
                body="Every outbound message belongs to one program, and the queue is read one program at a time. Pick one above and its messages appear here — drafts, anything waiting on an approver, and anything already frozen."
                hint="There is deliberately no all-programs view. Applying reach row by row on a connection that bypasses row-level security is exactly the query that leaks, so the scope is named by the caller and checked once, before anything is read."
              />
            </Card>
          ) : queue.error ? (
            <ErrorNote>{errorMessage(queue.error)}</ErrorNote>
          ) : queue.isPending ? (
            /* Shaped like the queue rather than a centred spinner: the table
               below collapses the two-column layout and springs it back when
               the rows land, and the eye is already where the recipients will
               be. */
            <Card>
              <div className="px-4 pt-4">
                <SectionTitle
                  title="Queue"
                  subtitle="Reading every outbound message for this program."
                />
              </div>
              <TableSkeleton rows={6} cols={3} />
            </Card>
          ) : messages.length === 0 ? (
            <Card>
              <EmptyState
                title="Nothing is queued for this program"
                body="Every outbound message is a row here before it is anything else — that is what §8's word “single” means. Draft one to put it in front of a reviewer."
                hint="Empty because nothing has been drafted yet, not because a filter is hiding it: no message exists for this program in any state. Drafting needs no approval authority, so this is a step you can take right now."
                action={<Button variant="primary" onClick={() => setComposing(true)}>Draft a message</Button>}
              />
            </Card>
          ) : (
            <div className="space-y-3">
              {/* The state chips count what arrived, so a queue at the cap
                  reports fewer PENDING_APPROVAL messages than exist. */}
              <BoundNote
                bound={messagesBound}
                noun="queued messages"
                derived="The state chip counts cover only those."
                atServerCap
              />

              <Toolbar>
                <SearchInput
                  value={query}
                  onChange={setQuery}
                  placeholder="Recipient, subject or template key"
                  count={rows.length}
                  total={messages.length}
                  className="w-full sm:w-80"
                />
                <FilterChip
                  label="All"
                  count={messages.length}
                  active={filter === null}
                  onClick={() => setFilter(null)}
                />
                {COMMS_STATES.map((state) => (
                  <FilterChip
                    key={state}
                    label={COMMS_STATE_LABEL[state]}
                    count={counts[state]}
                    active={filter === state}
                    tone={state === 'PENDING_APPROVAL' ? 'alert' : 'neutral'}
                    onClick={() => setFilter(filter === state ? null : state)}
                  />
                ))}
              </Toolbar>

              {/* The chips deliberately do NOT follow the search. A "Pending
                  approval 6" that shrank to 1 because somebody typed a name
                  would hide work rather than find it — and the queue's shape is
                  the thing those counts are for. Said out loud rather than left
                  as an inconsistency the reader has to resolve alone. */}
              {query.trim() !== '' && (
                <p className="text-[11px] text-ink-3 leading-relaxed" role="status">
                  The state chips count the whole queue, not the search. The figure in the search
                  box — {rows.length} of {messages.length} — is the one describing the list below.
                </p>
              )}

              <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.45fr)] items-start">
                <QueueTable
                  rows={rows}
                  selectedId={selected}
                  onSelect={(m) => setSelectedId(m.id)}
                  emptyHint={
                    query.trim() !== ''
                      ? `Nothing matches “${query.trim()}”${filter ? ' inside the state you have picked' : ''}. Press Esc in the search box to clear it.`
                      : 'A state chip above is narrowing this list. Click the highlighted chip again, or “All”, to see the whole queue.'
                  }
                />

                {selected ? (
                  <MessagePanel
                    key={selected}
                    messageId={selected}
                    programId={programId}
                    actorName={actorName}
                  />
                ) : (
                  <Card>
                    <EmptyState
                      title="Open a message"
                      body="Nothing is reviewed from a list row. Pick one on the left and this panel shows what §8 requires at approval — channel, recipient, template, and the diff from that template — which is the whole reason this queue exists rather than a mail merge."
                      hint="Empty because no message is selected. Nothing is approved, released or changed until you open one and act on it deliberately."
                    />
                  </Card>
                )}
              </div>
            </div>
          )}
        </div>
      </Page>

      <ComposeModal
        open={composing}
        programId={programId}
        canSeeCommercials={canSeeCommercials}
        onClose={() => setComposing(false)}
        onDrafted={(message) => {
          setComposing(false)
          setSelectedId(message.id)
        }}
      />
    </>
  )
}

/**
 * The open question, said once at the top of the screen rather than discovered
 * one 501 at a time.
 *
 * Deliberately not styled as an error. It is not a failure: it is a system that
 * knows what it does not know, and is refusing to guess an approval authority
 * rather than quietly picking the permissive option.
 */
function OpenQuestionBanner() {
  return (
    <Card className="p-4 border-info/30 bg-info/[0.06]">
      <div className="flex items-start gap-3">
        <span
          className="mt-0.5 inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-lg
            bg-info-wash text-info-ink text-sm font-semibold"
          aria-hidden
        >
          ?
        </span>
        <div className="min-w-0">
          <p className="text-sm font-semibold text-info-ink">
            Approval is blocked on an owner decision — CLAUDE.md §14 Q3
          </p>
          <p className="text-xs text-ink-2 mt-1.5 leading-relaxed">
            {COMMS_AUTHORITY_UNDECIDED_NOTE}
          </p>
          <div className="mt-3 grid gap-2 sm:grid-cols-2">
            <div className="rounded-lg border border-line bg-surface px-3 py-2">
              <p className="text-[11px] font-medium text-ink">Works today</p>
              <p className="text-[11px] text-ink-2 mt-0.5 leading-relaxed">
                Draft → amend → submit for approval. A message can be written, reviewed against
                its template, and put in front of a human.
              </p>
            </div>
            <div className="rounded-lg border border-line bg-surface px-3 py-2">
              <p className="text-[11px] font-medium text-ink">Answers 501 Not Implemented</p>
              <p className="text-[11px] text-ink-2 mt-0.5 leading-relaxed">
                Approve, reject, release. The buttons below are drawn and live — the endpoints
                exist and are tested. Pressing one changes nothing and returns the question.
              </p>
            </div>
          </div>
        </div>
      </div>
    </Card>
  )
}

/** The four states, with the two that are unreachable today marked as such. */
function LifecycleLegend() {
  return (
    <Card className="p-4">
      <SectionTitle
        title="The lifecycle"
        subtitle="R4, on a message: DRAFT → PENDING_APPROVAL → APPROVED → RELEASED. Approval freezes and hashes it; release is a separate act — and neither one sends anything."
      />
      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
        {COMMS_STATES.map((state) => (
          <div key={state} className="rounded-lg border border-line bg-surface-2 px-3 py-2.5">
            <div className="flex items-center gap-1.5">
              <StatePill state={state} />
              {(state === 'APPROVED' || state === 'RELEASED') && (
                <span className="text-[10px] uppercase tracking-wide text-ink-3">unreachable</span>
              )}
            </div>
            <p className="text-[11px] text-ink-2 mt-1.5 leading-relaxed">
              {COMMS_STATE_BLURB[state]}
            </p>
          </div>
        ))}
      </div>
      <p className="text-[11px] text-ink-3 mt-3 leading-relaxed">
        The last two are empty on purpose and will stay empty until §14 Q3 is answered. A queue
        that fills and stops is R4 working — “nothing leaves the system unapproved” — not a screen
        that is half built.
      </p>
    </Card>
  )
}

function QueueTable({
  rows,
  selectedId,
  onSelect,
  emptyHint,
}: {
  rows: CommsMessage[]
  selectedId: string | null
  onSelect: (message: CommsMessage) => void
  /** Why the list is empty RIGHT NOW — which control is doing the narrowing. */
  emptyHint: string
}) {
  return (
    <Card>
      <div className="px-4 pt-4">
        <SectionTitle
          title="Queue"
          subtitle="Waiting on a human first, then the rest. Every outbound message for this program, whatever state it is in."
          action={<Badge>{rows.length}</Badge>}
        />
      </div>

      {/* The list is non-empty by the time this component is drawn, so an empty
          `rows` can only mean a control above narrowed it to nothing. Naming
          which control is the whole job here: a table that just stops reads as
          "there is no such message", which is how somebody re-drafts one that
          already exists. */}
      {rows.length === 0 ? (
        <EmptyState
          title="Nothing here matches"
          body="This program does have queued messages — drafts, messages waiting on an approver, and anything already frozen. None of them survive the filters set above."
          hint={emptyHint}
        />
      ) : (
      <div className="overflow-x-auto scroll-slim">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-line">
              <Th>Recipient</Th>
              <Th>
                <HelpTip term="State">
                  Where the message sits on R4's one-way ladder: <strong>Draft</strong>, being
                  written · <strong>Pending approval</strong>, in front of a person ·{' '}
                  <strong>Approved</strong>, frozen and hashed · <strong>Released</strong>, a
                  person cleared it — and still not sent. Nothing walks back up it; the only way
                  to change a frozen message is a new version starting again at Draft.
                </HelpTip>
              </Th>
              <Th>
                <HelpTip term="Diff">
                  How far the message has drifted from its template. A message identical to its
                  template carries no drafted prose at all and can be checked in one glance;{' '}
                  <strong>+n</strong> counts the lines the drafter wrote of its own, which are the
                  lines an approver actually has to read.
                </HelpTip>
              </Th>
            </tr>
          </thead>
          <tbody className="divide-y divide-line-soft">
            {rows.map((m) => {
              const diff = parseDiff(m.diff)
              const active = selectedId === m.id
              return (
                <tr
                  key={m.id}
                  onClick={() => onSelect(m)}
                  className={`cursor-pointer transition ${
                    active ? 'bg-accent-soft' : 'hover:bg-surface-2'
                  }`}
                >
                  <Td>
                    <span className="font-medium text-ink">
                      {m.recipient_name || m.recipient_ref}
                    </span>
                    <span className="block text-[11px] text-ink-3 mt-0.5">
                      {RECIPIENT_KIND_LABEL[m.recipient_kind]} · {CHANNEL_LABEL[m.channel]}
                    </span>
                    <span className="block text-[11px] text-ink-3 mt-0.5 truncate max-w-[16rem]">
                      {m.subject || m.template_key}
                    </span>
                  </Td>
                  <Td>
                    <StatePill state={m.state} />
                    <span className="block text-[11px] text-ink-3 mt-1">v{m.version}</span>
                    {m.is_commercial && (
                      <span className="block mt-1">
                        <Badge tone="warn">Commercial</Badge>
                      </span>
                    )}
                  </Td>
                  <Td className="text-xs">
                    <DiffSummary diff={diff} />
                    <span className="block text-[11px] text-ink-3 mt-1">
                      {fmtDate(m.created_at)}
                    </span>
                  </Td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      )}
    </Card>
  )
}

/**
 * "Identical" or "+n / −n" — the one-glance version of the review surface.
 *
 * `+n` is flame and `−n` is not, for the same reason the hunks are: the added
 * lines are drafter-authored prose and the removed ones are agreed template
 * wording. "Identical to template" stays green because it is the one genuinely
 * settled outcome on this screen — nothing was written, so there is nothing to
 * read.
 */
function DiffSummary({ diff }: { diff: TemplateDiff | null }) {
  if (!diff) {
    return <span className="text-ink-3">Diff unreadable — review the full text</span>
  }
  if (diff.identical) {
    return (
      <span className="text-good-ink font-medium">
        Identical to template
      </span>
    )
  }
  return (
    <span className="tabular-nums">
      <span className="text-flame-ink font-medium">+{diff.lines_added}</span>
      <span className="text-ink-3"> / </span>
      <span className="text-ink-2">−{diff.lines_removed}</span>
      <span className="block text-[11px] text-ink-3">
        {diff.lines_added} drafted {diff.lines_added === 1 ? 'line' : 'lines'} across{' '}
        {diff.hunks.length} {diff.hunks.length === 1 ? 'hunk' : 'hunks'}
      </span>
    </span>
  )
}

/**
 * One message, in full: §8's four facts, the diff, and the acts.
 *
 * The message is refetched by id rather than read from the queue row, because the
 * diff is the thing being reviewed and it must be the current one — an amend
 * recomputes it server-side, and a stale copy of it is a review surface that lies.
 */
function MessagePanel({
  messageId,
  programId,
  actorName,
}: {
  messageId: string
  programId: string
  actorName: (id: string | null) => string
}) {
  const queryClient = useQueryClient()

  const message = useQuery({
    queryKey: commsKeys.message(messageId),
    queryFn: () => fetchCommsMessage(messageId),
    retry: false,
  })

  const settle = () => {
    void queryClient.invalidateQueries({ queryKey: commsKeys.queuesFor(programId) })
    void queryClient.invalidateQueries({ queryKey: commsKeys.message(messageId) })
  }

  if (message.isPending) return <Card><Loading label="Loading the message" /></Card>
  if (message.error) {
    return (
      <Card className="p-4">
        <ActionError error={message.error} />
      </Card>
    )
  }

  const m = message.data
  const diff = parseDiff(m.diff)

  return (
    <div className="space-y-4">
      <Card className="p-4">
        <SectionTitle
          title={m.subject || m.template_key}
          subtitle={`Version ${m.version}${m.supersedes_id ? ' · supersedes an earlier message' : ''}`}
          action={<StatePill state={m.state} />}
        />

        {/* §8's first three facts, before anything is clicked. */}
        <div className="grid gap-2 sm:grid-cols-2">
          <Fact
            label={
              <HelpTip term="Channel">
                How this message would travel if anything could carry it — email, WhatsApp, or a
                ticket raised on the EdTech platform. It is recorded so an approver can judge the
                tone and the audience. No provider is wired to any of the three in this phase.
              </HelpTip>
            }
            value={CHANNEL_LABEL[m.channel]}
            note="No provider is wired to any of them in this phase."
          />
          <Fact
            label={
              <HelpTip term="Recipient">
                Who would receive it, and what class of counterparty they are. The class is its
                own field rather than guessed from the address, because it sets the autonomy
                ceiling: a trainer is a contracted counterparty and a college is an external
                contact, and §8 caps both at "act only with a human approval".
              </HelpTip>
            }
            value={`${m.recipient_name ? `${m.recipient_name} · ` : ''}${m.recipient_ref}`}
            note={`${RECIPIENT_KIND_LABEL[m.recipient_kind]} — ${RECIPIENT_KIND_CEILING[m.recipient_kind]}`}
            mono
          />
          <Fact
            label={
              <HelpTip term="Template">
                The agreed wording this message was written against, with named slots left for
                the facts. Its text is copied onto this row when the draft is made, so the diff
                cannot be restated months later against a library entry somebody has since edited.
              </HelpTip>
            }
            value={m.template_key}
            note="Snapshotted on the row, so the diff cannot be restated later against an edited library entry."
            mono
          />
          <Fact
            label={
              <HelpTip term="Commercial">
                A message that states money — a rate, a payout, an invoice figure. The
                commercials wall (§4) puts those in front of Senior Managers and Managers only;
                an LDE Executive gets zero rows for them, in the database rather than in this UI.
              </HelpTip>
            }
            value={m.is_commercial ? 'Yes — behind the wall' : 'No'}
            note={
              m.is_commercial
                ? 'A message about money is the money restated in prose, so an LDE Executive gets zero rows for it (R5).'
                : 'Readable by any internal persona that reaches this program.'
            }
          />
        </div>
      </Card>

      <DiffCard message={m} diff={diff} />

      <TemplateValuesCard values={m.template_values} />

      <ApprovalActions message={m} onSettled={settle} />

      <ReleaseAction message={m} onSettled={settle} />

      <SupersedeAction message={m} onSettled={settle} />

      <RecordCard message={m} actorName={actorName} />
    </div>
  )
}

/**
 * The diff-from-template. §8 names it as the thing shown at approval, and it is
 * the single most important element on this screen.
 *
 * Read from the row, drawn as two sides. `diff.py` keeps both the template lines
 * and the message lines on every hunk rather than a unified `-`/`+` stream
 * precisely so a review UI can do this — "recovering the two sides from a unified
 * string is not [trivial]" — and throwing that away to render a terminal-style
 * patch would waste the one design decision made for this component.
 */
function DiffCard({ message, diff }: { message: CommsMessage; diff: TemplateDiff | null }) {
  const [showFull, setShowFull] = useState(false)

  return (
    <Card className="p-4">
      <SectionTitle
        title="Diff from template"
        subtitle="The only part of this message that was not already agreed. Quiet lines are the template with every fact substituted in; flame lines are the drafter's own words. Approving is approving those."
        action={
          <Button size="sm" variant="ghost" onClick={() => setShowFull((v) => !v)}>
            {showFull ? 'Hide full text' : 'Show full text'}
          </Button>
        }
      />

      {diff === null ? (
        <InfoNote>
          This row's <code>diff</code> object is not in a shape this screen recognises, so nothing
          is drawn rather than a blob presented as a review. The stored diff carries a{' '}
          <code>version</code> stamp for exactly this reason — a diff outlives the code that made
          it. Read the message body below and treat it as unreviewed.
        </InfoNote>
      ) : (
        <>
          {diff.version !== SUPPORTED_DIFF_VERSION && (
            <div className="mb-3">
              <InfoNote>
                This diff was produced by algorithm version {diff.version}; this screen draws
                version {SUPPORTED_DIFF_VERSION}. The hunks are rendered because the shape is
                recognisable, but check them against the full text before approving.
              </InfoNote>
            </div>
          )}

          {diff.identical ? (
            <div className="rounded-lg border border-good/30 bg-good/[0.08] px-3 py-2.5">
              <p className="text-sm font-medium text-good-ink">
                Identical to the rendered template.
              </p>
              <p className="text-xs text-ink-2 mt-1.5 leading-relaxed">
                The drafter wrote nothing of its own: every word here is either template text or a
                value substituted from a system of record (R1). The only judgement left is whether
                this is the right recipient and the right facts — which is a decision you can make
                in one glance, correctly.
              </p>
            </div>
          ) : (
            <>
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 rounded-lg border border-line bg-surface-2 px-3 py-2">
                <span className="text-sm text-ink">
                  <strong className="tabular-nums">{linesChanged(diff)}</strong> lines to read
                </span>
                <span className="text-xs text-ink-3 tabular-nums">
                  <span className="text-flame-ink font-medium">
                    +{diff.lines_added} drafted
                  </span>{' '}
                  ·{' '}
                  <span className="text-ink-2">
                    −{diff.lines_removed} template lines dropped
                  </span>{' '}
                  ·{' '}
                  {diff.hunks.length}{' '}
                  <HelpTip term={diff.hunks.length === 1 ? 'hunk' : 'hunks'}>
                    One run of lines where the message and the filled-in template disagree. Two
                    changes a page apart are two hunks; two changes on neighbouring lines are one.
                    It counts the <em>places</em> to look, not the lines.
                  </HelpTip>{' '}
                  · template {diff.template_lines} lines, message {diff.message_lines} lines
                </span>
              </div>

              {/* THE KEY, AND IT IS NOT DECORATION.
                  Colour here answers the only question the approver has —
                  "which of these words did a model write?" — so it has to be
                  spelled out before the first hunk rather than inferred from a
                  diff convention this one deliberately does not follow. */}
              <Legend
                className="mt-3"
                items={[
                  {
                    swatch: 'bg-flame-soft border-flame/30',
                    label: '+ drafted',
                    hint: 'the drafter’s own words — what you are approving',
                  },
                  {
                    swatch: 'bg-surface-2 border-line',
                    label: '− template',
                    hint: 'agreed wording the message drops or replaces',
                  },
                  {
                    swatch: 'bg-surface border-line',
                    label: 'unmarked',
                    hint: 'template and message agree, character for character',
                  },
                ]}
              />

              <div className="mt-3 rounded-lg border border-line overflow-hidden">
                {buildDiffView(message.template_body, diff).map((block, index) => (
                  <DiffBlockView key={index} block={block} />
                ))}
              </div>

              <p className="text-[11px] text-ink-3 mt-2 leading-relaxed">
                A rewritten line counts twice — once removed, once added — because reading it is
                two pieces of work, not one. Unchanged runs are elided; use “Show full text” to
                read both sides end to end.
              </p>
            </>
          )}
        </>
      )}

      {showFull && (
        <div className="mt-4 grid gap-3 lg:grid-cols-2">
          <FullText
            title="Rendered template — the baseline"
            note="The agreed wording, with every slot already filled in by the server from the structured input. This is what the diff is taken against — never the raw template, which would report every substituted fact as drafter prose."
            text={message.template_body}
          />
          <FullText
            drafted
            title="Message body — what would go out"
            note="Stored word for word as the drafter left it. Nothing on this screen sends it, and there is no provider behind this queue that could."
            text={message.body}
          />
        </div>
      )}
    </Card>
  )
}

function DiffBlockView({ block }: { block: DiffBlock }) {
  if (block.kind === 'elided') {
    return (
      <div className="bg-surface-2 px-3 py-1 text-[11px] text-ink-3 border-b border-line-soft">
        ⋯ {block.hidden} unchanged {block.hidden === 1 ? 'line' : 'lines'}
      </div>
    )
  }

  if (block.kind === 'context') {
    return (
      <div className="border-b border-line-soft">
        {block.lines.map((line, i) => (
          <div key={i} className="flex gap-2 px-3 py-0.5">
            {/* Same gutter width as a hunk's, so the mono text of an unchanged
                line sits on the same left edge as a changed one. A diff whose
                columns shift between blocks is read as two documents. */}
            <span className="w-[4.5rem] shrink-0 text-right text-[11px] text-ink-3 tabular-nums select-none">
              {block.at + i + 1}
            </span>
            <span className="font-mono text-xs text-ink-3 whitespace-pre-wrap break-words min-w-0">
              {line === '' ? ' ' : line}
            </span>
          </div>
        ))}
      </div>
    )
  }

  return <HunkView hunk={block.hunk} />
}

const HUNK_LABEL: Record<Hunk['op'], string> = {
  added: 'Written by the drafter',
  removed: 'Template wording dropped',
  changed: 'Template wording rewritten',
}

/** What each hunk is asking the approver to do, in one clause. */
const HUNK_ASK: Record<Hunk['op'], string> = {
  added: 'nothing in the template says this — read it as new',
  removed: 'the template said this and the message does not',
  changed: 'template first, the drafter’s replacement under it',
}

/**
 * THIS IS NOT THE USUAL RED/GREEN PATCH, AND THAT IS DELIBERATE.
 *
 * A terminal diff paints additions green, which on this screen would be a lie
 * told in the app's own vocabulary: green means "completed, signed, reconciled"
 * everywhere else in the console (DESIGN.md §4.6), and these lines are the
 * opposite of settled — they are the unreviewed prose the whole approval exists
 * to catch. So they carry `flame`, the token reserved for drafted and generated
 * surfaces (DESIGN.md §2, CLAUDE.md R1), and a reader can tell drafted language
 * from a queried fact at a glance without having to work out which side is which.
 *
 * The template side goes QUIET rather than red. It is the wording that was
 * already agreed; it carries no risk, so it earns no colour. Spending a second
 * hue on it would halve the contrast of the one thing an approver is here to
 * read. Per DESIGN.md §5 colour is not the only channel either way: the gutter
 * names each side in words and the hunk label says what happened.
 */
function HunkView({ hunk }: { hunk: Hunk }) {
  return (
    <div className="border-b border-line-soft">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 px-3 py-1.5 bg-surface-2">
        <Badge tone={hunk.op === 'removed' ? 'neutral' : 'flame'}>{HUNK_LABEL[hunk.op]}</Badge>
        <span className="text-[11px] text-ink-3">{HUNK_ASK[hunk.op]}</span>
        <span className="text-[11px] text-ink-3 tabular-nums">
          · at template line {hunk.at + 1}
        </span>
      </div>

      {hunk.template.map((line, i) => (
        <div key={`t${i}`} className="flex gap-2 px-3 py-0.5 bg-surface-2">
          <span
            className="w-[4.5rem] shrink-0 text-right text-[10px] uppercase tracking-wide leading-5
              text-ink-2 select-none"
          >
            − template
          </span>
          <span className="font-mono text-xs text-ink-2 whitespace-pre-wrap break-words min-w-0">
            {line === '' ? ' ' : line}
          </span>
        </div>
      ))}

      {hunk.message.map((line, i) => (
        <div key={`m${i}`} className="flex gap-2 px-3 py-0.5 bg-flame-soft">
          <span
            className="w-[4.5rem] shrink-0 text-right text-[10px] uppercase tracking-wide leading-5
              text-flame-ink select-none"
          >
            + drafted
          </span>
          <span className="font-mono text-xs text-flame-ink whitespace-pre-wrap break-words min-w-0">
            {line === '' ? ' ' : line}
          </span>
        </div>
      ))}
    </div>
  )
}

/**
 * One side of the message, end to end.
 *
 * `drafted` frames the panel in `flame` and badges it, and it is set on exactly
 * one of the two: the body. It is a FRAME rather than a tint on every character,
 * because the body is not uniformly generated — most of it is template text with
 * facts substituted from a system of record, and colouring those orange would
 * say the opposite of R1. The frame says "this is the drafted artefact"; the
 * diff above says which words inside it the drafter actually wrote.
 */
function FullText({
  title,
  note,
  text,
  drafted = false,
}: {
  title: string
  note: string
  text: string
  drafted?: boolean
}) {
  return (
    <div
      className={`rounded-lg border overflow-hidden ${
        drafted ? 'border-flame/30 bg-surface-2' : 'border-line bg-surface-2'
      }`}
    >
      <div className={`px-3 py-2 border-b ${drafted ? 'border-flame/25 bg-flame-soft' : 'border-line'}`}>
        <div className="flex flex-wrap items-center gap-2">
          <p className={`text-xs font-medium ${drafted ? 'text-flame-ink' : 'text-ink'}`}>{title}</p>
          {drafted && <Badge tone="flame">Drafted prose</Badge>}
        </div>
        <p className="text-[11px] text-ink-3 mt-0.5 leading-relaxed">{note}</p>
      </div>
      <pre className="px-3 py-2 font-mono text-xs text-ink-2 whitespace-pre-wrap break-words max-h-80 overflow-y-auto scroll-slim">
        {text}
      </pre>
    </div>
  )
}

/**
 * `template_values` — R1's provenance record, shown rather than trusted.
 *
 * "If a value appears in a generated message, it was passed in as structured
 * input, not produced by the model." This card is where an approver checks that
 * claim: every fact in the baseline came from one of these entries, and an entry
 * that was fetched and not printed is kept deliberately, because it is how an
 * auditor sees what the drafter had in front of it.
 */
function TemplateValuesCard({ values }: { values: Record<string, JsonValue> }) {
  const entries = Object.entries(values)
  return (
    <Card className="p-4">
      <SectionTitle
        title="Structured input"
        subtitle="Every fact substituted into the template, as it was passed in. R1: the database owns truth, the LLM owns language."
        action={<Badge>{entries.length}</Badge>}
      />
      {entries.length === 0 ? (
        <InfoNote>
          No values were supplied. The template had no slots to fill — everything in the baseline
          is fixed template text.
        </InfoNote>
      ) : (
        <div className="rounded-lg border border-line bg-surface-2 divide-y divide-line-soft">
          {/* Slot name and value both go through MonoValue: these are the exact
              strings somebody pastes into a query or a ticket when a figure is
              disputed, and `select-all` plus a face where l and 1 differ is the
              difference between checking a value and retyping one. */}
          {entries.map(([key, value]) => (
            <KeyValue
              key={key}
              className="px-3 py-1.5"
              label={<MonoValue className="text-ink-3">{key}</MonoValue>}
              value={
                <MonoValue className="break-all">
                  {typeof value === 'string' ? value : JSON.stringify(value)}
                </MonoValue>
              }
            />
          ))}
        </div>
      )}
      <p className="text-[11px] text-ink-3 mt-2 leading-relaxed">
        Amounts arrive as strings or integers. A float is refused by the server rather than
        rounded (R7) — a rupee figure reaching a trainer as 15584.000000000001 is wrong in the one
        way nobody forgives.
      </p>
    </Card>
  )
}

/**
 * Amend, submit, approve, reject.
 *
 * Release is deliberately not here. It is a different act with its own audit row,
 * it lives in the next card, and a reader of this file should have to scroll to
 * find it — the same separation `ApprovalsPage` keeps and for the same reason.
 */
function ApprovalActions({
  message,
  onSettled,
}: {
  message: CommsMessage
  onSettled: () => void
}) {
  const [body, setBody] = useState(message.body)
  const [reason, setReason] = useState('')

  const amend = useMutation({
    mutationFn: () => amendMessage(message.id, body),
    onSuccess: onSettled,
  })
  const submit = useMutation({
    mutationFn: () => submitMessage(message.id),
    onSuccess: onSettled,
  })
  const approve = useMutation({
    mutationFn: () => approveMessage(message.id),
    onSuccess: onSettled,
  })
  const reject = useMutation({
    mutationFn: () => rejectMessage(message.id, reason),
    onSuccess: () => {
      setReason('')
      onSettled()
    },
  })

  const busy = amend.isPending || submit.isPending || approve.isPending || reject.isPending
  const reasonReady = reason.trim().length > 0
  const bodyChanged = body !== message.body && body.trim().length > 0

  return (
    <Card className="p-4">
      <SectionTitle
        title="Approval"
        subtitle="Approving FREEZES the message — channel, recipient, template, baseline, body and the commercial flag are hashed. It sends nothing."
      />

      <div className="space-y-3">
        {message.state === 'DRAFT' && (
          <>
            <div>
              <Field
                label="Message body"
                hint="Amending recomputes the diff server-side in the same breath, so the stored diff never stops describing the stored body. Only the body: changing the recipient, the channel or the template changes what message this IS, and that is a new draft."
              >
                <Textarea
                  rows={8}
                  value={body}
                  onChange={(e) => setBody(e.target.value)}
                  className="font-mono text-xs"
                />
              </Field>
              <div className="mt-2 flex items-center gap-3">
                <Button variant="secondary" disabled={busy || !bodyChanged} onClick={() => amend.mutate()}>
                  {amend.isPending && <Spinner />}
                  Save amendment
                </Button>
                {!bodyChanged && (
                  <span className="text-xs text-ink-3">Unchanged from what is stored.</span>
                )}
              </div>
              {amend.error ? (
                <div className="mt-2">
                  <ActionError error={amend.error} />
                </div>
              ) : null}
            </div>

            <div className="pt-3 border-t border-line">
              <Button variant="primary" disabled={busy} onClick={() => submit.mutate()}>
                {submit.isPending && <Spinner />}
                Submit for approval
              </Button>
              <p className="text-xs text-ink-3 mt-1.5 leading-relaxed">
                <strong>This one works.</strong> Putting work in front of a human needs no approval
                authority of its own — §8 puts “propose, human edits and sends” at{' '}
                <HelpTip term="autonomy level 2">
                  The rung an agent is allowed to reach. 1 observe · <strong>2 draft</strong>, a
                  human edits and sends · 3 act on one human click · 4 act alone. Nothing touching
                  money, contracts or a college contact goes past 3, which is why no agent
                  anywhere has a send tool bound to it (R3).
                </HelpTip>
                . It is also the last step on this surface that currently succeeds.
              </p>
              {submit.error ? (
                <div className="mt-2">
                  <ActionError error={submit.error} />
                </div>
              ) : null}
            </div>
          </>
        )}

        {message.state === 'PENDING_APPROVAL' && (
          <>
            <BlockedByOpenQuestion
              what="Approving and rejecting"
              why="The power to withhold approval is the power to approve, so rejection is refused on exactly the same grounds — an actor who could not have approved must not be able to block."
            />

            <div>
              <Button variant="primary" disabled={busy} onClick={() => approve.mutate()}>
                {approve.isPending && <Spinner />}
                Approve — freeze this message
              </Button>
              {/* THE SENTENCE THIS PAGE EXISTS TO GET RIGHT. Two different
                  misreadings of one button, so both are refused by name and in
                  that order: approve ≠ release, and release ≠ send. */}
              <p className="text-xs text-ink-2 mt-1.5 leading-relaxed">
                <strong className="text-ink">
                  Approving is not releasing, and neither one is sending.
                </strong>{' '}
                Approving{' '}
                <HelpTip term="freezes">
                  Locks this version and takes a digest over it: channel, recipient, template,
                  baseline, body and the commercial flag. From then on the wording is evidence.
                  Changing an approved message is not an edit — it is a new version that starts
                  again at Draft and needs fresh approval (R4).
                </HelpTip>{' '}
                the message and records who approved it. It hands the message to nobody. Letting
                it go is a second act on the next card, a separate click by a person who can
                still refuse, and it writes its own separate audit row.
              </p>
              {approve.error ? (
                <div className="mt-2">
                  <ActionError error={approve.error} />
                </div>
              ) : null}
            </div>

            <div className="pt-3 border-t border-line">
              <Field
                label="Rejection reason — required"
                hint="Recorded on the audit row and on comms_messages.notes, so the drafter can read it without querying the audit trail. The API refuses a blank one."
              >
                <Textarea
                  rows={3}
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                  placeholder="What has to change before this can be approved…"
                />
              </Field>
              <div className="mt-2 flex items-center gap-3">
                <Button variant="danger" disabled={busy || !reasonReady} onClick={() => reject.mutate()}>
                  {reject.isPending && <Spinner />}
                  Send back to draft
                </Button>
                {!reasonReady && (
                  <span className="text-xs text-ink-3">Type a reason. Whitespace does not count.</span>
                )}
              </div>
              <p className="text-xs text-ink-3 mt-1.5 leading-relaxed">
                The version does not change. A rejected draft is the same message being reworked —
                only a frozen one is superseded (R4).
              </p>
              {reject.error ? (
                <div className="mt-2">
                  <ActionError error={reject.error} />
                </div>
              ) : null}
            </div>
          </>
        )}

        {(message.state === 'APPROVED' || message.state === 'RELEASED') && (
          <p className="text-sm text-ink-3">
            Already approved and frozen. An approved message cannot be walked back into draft — R4
            gives exactly one way out of it, and it is forwards. To revise it, supersede it with a
            new version below.
          </p>
        )}
      </div>
    </Card>
  )
}

/**
 * Release. Its own card, its own button, its own audit row — and its own
 * paragraph saying it is not a send, because "released" reads as "sent" to
 * everybody who has not read the migration.
 */
function ReleaseAction({ message, onSettled }: { message: CommsMessage; onSettled: () => void }) {
  const [notes, setNotes] = useState('')

  const release = useMutation({
    mutationFn: () => releaseMessage(message.id, notes),
    onSuccess: () => {
      setNotes('')
      onSettled()
    },
  })

  return (
    <Card className="p-4">
      <SectionTitle
        title="Release"
        subtitle="A separate act from approval, with a separate audit row. It marks state and transmits nothing."
      />

      <p className="text-xs text-ink-2 mb-3 leading-relaxed">
        <HelpTip term="Releasing">
          The second and final human act on a message: a person says this approved wording may
          leave. It is not the same act as approving and it is not done by the same click — R4
          keeps them apart so the record shows two people, or one person twice, deciding two
          different things. It is also not a transmission.
        </HelpTip>{' '}
        marks the row and writes an audit event. Nothing is handed to a recipient, because there
        is nothing behind this queue to hand it to.
      </p>

      <InfoNote>{RELEASE_IS_NOT_SEND_NOTE}</InfoNote>

      <div className="mt-3">
        {message.state === 'RELEASED' ? (
          <p className="text-sm text-ink-3">
            Released — meaning a human cleared it, not that anybody received it. Terminal: a change
            from here is a new version starting again at draft.
          </p>
        ) : message.state !== 'APPROVED' ? (
          <p className="text-sm text-ink-3">
            Nothing to release. A message becomes releasable only once it has been approved and
            frozen, and no message can be approved while §14 Q3 is open — so this control is
            legitimately unreachable today rather than missing. There is no path from draft or
            pending approval to released, and attempting one is refused by the state machine.
          </p>
        ) : (
          <>
            <BlockedByOpenQuestion
              what="Releasing"
              why="Release authority is read as approval authority — conservatively, because no persona is named for it anywhere in CLAUDE.md and reusing the approval set can never permit somebody who could not have approved."
            />
            <div className="mt-3">
              <Field
                label="Release note (optional)"
                hint="Commentary ABOUT the message, never part of it. It is the one column the freeze trigger leaves mutable, it is not hashed, and it cannot alter what was approved."
              >
                <Textarea
                  rows={2}
                  value={notes}
                  onChange={(e) => setNotes(e.target.value)}
                  placeholder="Anything the record should carry about this release…"
                />
              </Field>
              <div className="mt-2">
                <Button variant="primary" disabled={release.isPending} onClick={() => release.mutate()}>
                  {release.isPending && <Spinner />}
                  Release
                </Button>
              </div>
              <p className="text-xs text-ink-3 mt-1.5 leading-relaxed">
                The server recomputes the digest over the stored content and refuses with 409 if it
                no longer matches what was frozen at approval — so a recipient swapped after
                approval cannot ride out on somebody else's signature.
              </p>
              {release.error ? (
                <div className="mt-2">
                  <ActionError error={release.error} />
                </div>
              ) : null}
            </div>
          </>
        )}
      </div>
    </Card>
  )
}

/**
 * Supersede — R4's only way to change something already frozen.
 *
 * Unreachable today for the same reason release is: nothing can reach a frozen
 * state. Drawn anyway, and drawn as "not applicable in this state" rather than
 * omitted, because the rule it enforces is the one people try to route around.
 */
function SupersedeAction({ message, onSettled }: { message: CommsMessage; onSettled: () => void }) {
  const [body, setBody] = useState(message.body)
  const frozen = message.state === 'APPROVED' || message.state === 'RELEASED'

  const supersede = useMutation({
    mutationFn: () => supersedeMessage(message.id, body),
    onSuccess: onSettled,
  })

  if (!frozen) {
    return (
      <Card className="p-4">
        <SectionTitle
          title="Supersede"
          subtitle="R4: editing an approved artifact creates a new version in DRAFT requiring fresh approval."
        />
        <p className="text-sm text-ink-2 leading-relaxed">
          Not applicable.{' '}
          <HelpTip term="Superseding">
            The only way to change a message that has already been approved. It leaves the
            approved version exactly as it was signed off and opens a fresh Draft at the next
            version number, which walks the whole ladder again. An approved artifact is never
            edited in place — that would change what a person put their name to.
          </HelpTip>{' '}
          only applies to a frozen message. This one is not frozen, so it is edited in place:
          amend a draft, and reject a message awaiting approval before changing it. Superseding an
          unfrozen message is refused by the service, not hidden by this screen.
        </p>
      </Card>
    )
  }

  return (
    <Card className="p-4">
      <SectionTitle
        title="Supersede"
        subtitle="A new DRAFT at version + 1. The predecessor is left exactly as it was approved."
      />
      <Field
        label="New body"
        hint="The successor keeps this message's channel, recipient and baseline, carries none of the freeze, and walks the whole ladder again."
      >
        <Textarea
          rows={6}
          value={body}
          onChange={(e) => setBody(e.target.value)}
          className="font-mono text-xs"
        />
      </Field>
      <div className="mt-2">
        <Button
          variant="secondary"
          disabled={supersede.isPending || body.trim() === ''}
          onClick={() => supersede.mutate()}
        >
          {supersede.isPending && <Spinner />}
          Create superseding draft
        </Button>
      </div>
      {supersede.error ? (
        <div className="mt-2">
          <ActionError error={supersede.error} />
        </div>
      ) : null}
    </Card>
  )
}

/**
 * The block, stated before the click.
 *
 * Not an error box — a decision that has not been made is not a failure. The
 * button beside it stays live on purpose: the day §14 Q3 is answered and
 * `COMMS_APPROVAL_AUTHORITY` records it, the server starts accepting these calls
 * and nobody has to remember to come back here and re-enable anything.
 */
function BlockedByOpenQuestion({ what, why }: { what: string; why: string }) {
  return (
    <div className="rounded-lg border border-info/30 bg-info/[0.08] px-3 py-2.5" role="status">
      <p className="text-sm font-medium text-info-ink">
        {what} will be refused with 501 Not Implemented.
      </p>
      <p className="text-xs text-ink-2 mt-1.5 leading-relaxed">{why}</p>
      <p className="text-xs text-ink-2 mt-1.5 leading-relaxed">{COMMS_AUTHORITY_UNDECIDED_NOTE}</p>
      <p className="text-[11px] text-ink-3 mt-1.5 leading-relaxed">
        The button below is live rather than greyed out: this is an undecided question, not a
        permission you are missing, and the day it is answered the server will start accepting the
        call without this screen changing. Pressing it now changes nothing and writes no audit row.
      </p>
    </div>
  )
}

/**
 * A refusal, said as what it actually was.
 *
 * The ones that must never render as a generic red box:
 *
 *   501 — §14 Q3 is open. Not a permission problem, not an outage, not a bug.
 *   409 + hash — the content changed after approval. An integrity signal.
 *   403 — the commercials wall or the authority set.
 *   422 — an unfilled slot, a float in template_values (R7), or a blank reason.
 */
function ActionError({ error }: { error: unknown }) {
  if (isAuthorityUndefined(error)) {
    return (
      <div className="rounded-lg border border-info/30 bg-info/[0.08] px-3 py-2.5" role="status">
        <p className="text-sm font-medium text-info-ink">
          Refused, as expected — nobody has been given authority to do this.
        </p>
        <p className="text-xs text-ink-2 mt-1.5 leading-relaxed">
          Nothing changed and no audit row was written. The message is still exactly where it was.
          This is the queue filling and stopping, which is what R4 asks for while the question is
          open — it is not a fault to retry, and no permission grant will clear it.
        </p>
        <p className="text-[11px] text-ink-3 mt-1.5 font-mono break-words leading-relaxed">
          {errorMessage(error)}
        </p>
      </div>
    )
  }

  if (isFrozenContentMismatch(error)) {
    return (
      <ErrorNote>
        <strong>This message changed after it was approved.</strong> The digest recomputed over the
        stored content no longer matches the one frozen at approval, so the release was refused
        rather than carrying somebody else's signature over different text. That edit should have
        created a new version in draft requiring fresh approval (R4). Do not retry — find out what
        changed, then supersede.
      </ErrorNote>
    )
  }

  if (isConflict(error)) {
    return (
      <ErrorNote>
        <strong>That move is not legal from this state.</strong> {errorMessage(error)} The
        lifecycle runs one way — DRAFT → PENDING_APPROVAL → APPROVED → RELEASED — and the state on
        screen may simply be out of date. Reload the queue.
      </ErrorNote>
    )
  }

  if (isForbidden(error)) {
    return (
      <ErrorNote>
        <strong>The server refused this account.</strong> {errorMessage(error)} Either this message
        is behind the commercials wall (§4: Senior Manager and Manager only — a message about a
        payout is the payout restated in prose), or this program is outside your reach. Reach comes
        from your college and cluster assignments, not from your role alone.
      </ErrorNote>
    )
  }

  if (isUnprocessable(error)) {
    return (
      <ErrorNote>
        <strong>The server would not accept that content.</strong> {errorMessage(error)} The usual
        causes are a template slot with no value — the braces survive into what the recipient
        reads — a float in the structured input (R7 accepts a string or an integer), or a blank
        rejection reason.
      </ErrorNote>
    )
  }

  if (isNotFound(error)) {
    return (
      <ErrorNote>
        <strong>No such message or program.</strong> {errorMessage(error)}
      </ErrorNote>
    )
  }

  return <ErrorNote>{errorMessage(error)}</ErrorNote>
}

/** The audit face of the row: who did what, when, and what was frozen. */
function RecordCard({
  message,
  actorName,
}: {
  message: CommsMessage
  actorName: (id: string | null) => string
}) {
  return (
    <Card className="p-4">
      <SectionTitle
        title="Record"
        subtitle="Every transition on this row landed with its audit event in one transaction (§11)."
      />
      <div className="grid gap-1.5 sm:grid-cols-2">
        <Meta label="Drafted by" value={actorName(message.created_by)} />
        <Meta label="Drafted at" value={fmtDate(message.created_at)} />
        <Meta label="Submitted by" value={actorName(message.submitted_by)} />
        <Meta label="Submitted at" value={fmtDate(message.submitted_at)} />
        <Meta label="Approved by" value={actorName(message.approved_by)} />
        <Meta label="Approved at" value={fmtDate(message.approved_at)} />
        <Meta
          label={
            <HelpTip term="Released by">
              The second person, and the second act. Approving froze the message; releasing is a
              separate click with its own audit row, taken by somebody who could still have
              refused (R4). It is also not a send: releasing marks the row and writes the row's
              history, and no provider exists behind this queue to carry it anywhere.
            </HelpTip>
          }
          value={actorName(message.released_by)}
        />
        <Meta label="Released at" value={fmtDate(message.released_at)} />
        <Meta
          label={
            <HelpTip term="Content hash">
              A digest taken over the channel, recipient, template, baseline, body and commercial
              flag at the moment of approval — the fingerprint of exactly what was approved. On
              release the server recomputes it and refuses with a conflict if it no longer
              matches, so text edited after approval cannot ride out on somebody else's signature.
            </HelpTip>
          }
          value={message.content_hash ?? 'Not frozen — no approval yet'}
          mono={message.content_hash !== null}
        />
        <Meta label="Version" value={`v${message.version}`} />
        {message.supersedes_id && <Meta label="Supersedes" value={message.supersedes_id} mono />}
        {message.superseded_at && (
          <Meta label="Superseded at" value={fmtDate(message.superseded_at)} />
        )}
        {message.related_artifact_id && (
          <Meta
            label="Related artifact"
            value={`${message.related_artifact_type ?? '—'} · ${message.related_artifact_id}`}
            mono
          />
        )}
      </div>
      {message.notes && (
        <div className="mt-3 rounded-lg border border-line bg-surface-2 px-3 py-2">
          <p className="text-xs text-ink-3">Notes</p>
          <p className="text-sm text-ink mt-1 leading-relaxed">{message.notes}</p>
        </div>
      )}
    </Card>
  )
}

/**
 * One of §8's four facts, in a box of its own.
 *
 * `label` is a ReactNode rather than a string so it can carry a `HelpTip`.
 * "Commercial" and "Template" are exactly the words a first-week LDE Executive
 * reads straight past, and the moment of confusion and the moment of reading
 * are the same moment (DESIGN.md §4.2).
 *
 * Identifiers go through `MonoValue`, not a hand-rolled `font-mono`: a
 * recipient address and a template key are things people copy into a ticket or
 * a mail client, and `MonoValue` is where `select-all` and the digit-disambiguating
 * face live.
 */
function Fact({
  label,
  value,
  note,
  mono = false,
}: {
  label: ReactNode
  value: string
  note?: string
  mono?: boolean
}) {
  return (
    <div className="rounded-lg border border-line bg-surface-2 px-3 py-2">
      <p className="text-[11px] text-ink-3">{label}</p>
      <p className="text-sm text-ink mt-0.5 break-words">
        {mono ? <MonoValue>{value}</MonoValue> : value}
      </p>
      {note && <p className="text-[11px] text-ink-3 mt-1 leading-relaxed">{note}</p>}
    </div>
  )
}

/** One audit fact. `KeyValue` owns the label weight and the em-dash-for-empty. */
function Meta({ label, value, mono = false }: { label: ReactNode; value: string; mono?: boolean }) {
  return <KeyValue label={label} value={value} mono={mono} className="py-0.5" />
}

// --- Drafting -----------------------------------------------------------------

interface ValueRow {
  key: string
  value: string
}

/**
 * Put a message in the queue.
 *
 * The template and the body are BOTH typed here, and the body is not prefilled
 * from the template by this screen. That is deliberate: rendering the baseline is
 * `render()`'s job on the server, it is what the stored `template_body` is, and a
 * browser-side substitution that formatted a Decimal differently would produce a
 * phantom hunk in the diff — a review surface reporting a change nobody made.
 * Paste the template text into the body and edit from there, and a message that
 * is pure substitution will correctly diff to “identical”.
 *
 * Every value is sent as a STRING. R7: the server refuses a float outright rather
 * than rounding it, and a number typed into a form is a float in JSON.
 */
function ComposeModal({
  open,
  programId,
  canSeeCommercials,
  onClose,
  onDrafted,
}: {
  open: boolean
  programId: string
  canSeeCommercials: boolean
  onClose: () => void
  onDrafted: (message: CommsMessage) => void
}) {
  const queryClient = useQueryClient()

  const [channel, setChannel] = useState<CommsChannel>('email')
  const [recipientKind, setRecipientKind] = useState<CommsRecipientKind>('internal_staff')
  const [recipientRef, setRecipientRef] = useState('')
  const [recipientName, setRecipientName] = useState('')
  const [templateKey, setTemplateKey] = useState('')
  const [template, setTemplate] = useState('')
  const [values, setValues] = useState<ValueRow[]>([])
  const [subject, setSubject] = useState('')
  const [body, setBody] = useState('')
  const [isCommercial, setIsCommercial] = useState(false)
  const [artifactType, setArtifactType] = useState<ArtifactType | ''>('')
  const [artifactId, setArtifactId] = useState('')

  const slots = useMemo(() => templateSlots(template), [template])
  const supplied = useMemo(
    () => new Set(values.map((v) => v.key.trim()).filter((k) => k !== '')),
    [values],
  )
  const missing = slots.filter((slot) => !supplied.has(slot))

  const draft = useMutation({
    mutationFn: () => {
      const templateValues: Record<string, JsonValue> = {}
      for (const row of values) {
        const key = row.key.trim()
        if (key !== '') templateValues[key] = row.value
      }
      return draftMessage({
        program_id: programId,
        channel,
        recipient_kind: recipientKind,
        recipient_ref: recipientRef.trim(),
        recipient_name: recipientName.trim() === '' ? null : recipientName.trim(),
        template_key: templateKey.trim(),
        template,
        template_values: templateValues,
        subject: subject.trim() === '' ? null : subject.trim(),
        body,
        is_commercial: isCommercial,
        related_artifact_type: artifactType === '' ? null : artifactType,
        related_artifact_id: artifactId.trim() === '' ? null : artifactId.trim(),
      })
    },
    onSuccess: (message) => {
      void queryClient.invalidateQueries({ queryKey: commsKeys.queuesFor(programId) })
      onDrafted(message)
    },
  })

  const ready =
    recipientRef.trim() !== '' &&
    templateKey.trim() !== '' &&
    template.trim() !== '' &&
    body.trim() !== ''

  return (
    <Modal open={open} onClose={onClose} title="Draft a message into the queue" width="max-w-3xl">
      <div className="space-y-3">
        <InfoNote>
          Drafting needs no approval authority — §8 puts “propose, human edits and sends” at
          autonomy level 2, so this is exactly what may happen without an approver in the room. The
          message lands in DRAFT and goes nowhere. The server renders your template against the
          values below to produce the baseline, then diffs that baseline against the body; nothing
          in this browser renders a template or computes a diff.
        </InfoNote>

        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Channel" hint="What an approver reads to judge the message. Nothing routes on it yet.">
            <Select value={channel} onChange={(e) => setChannel(e.target.value as CommsChannel)}>
              {COMMS_CHANNELS.map((c) => (
                <option key={c} value={c}>
                  {CHANNEL_LABEL[c]}
                </option>
              ))}
            </Select>
          </Field>

          <Field label="Recipient class" hint={RECIPIENT_KIND_CEILING[recipientKind]}>
            <Select
              value={recipientKind}
              onChange={(e) => setRecipientKind(e.target.value as CommsRecipientKind)}
            >
              {COMMS_RECIPIENT_KINDS.map((k) => (
                <option key={k} value={k}>
                  {RECIPIENT_KIND_LABEL[k]}
                </option>
              ))}
            </Select>
          </Field>

          <Field label="Recipient" hint="Address, handle or ticket queue.">
            <Input
              value={recipientRef}
              onChange={(e) => setRecipientRef(e.target.value)}
              placeholder="ops@college.edu"
            />
          </Field>

          <Field label="Recipient name (optional)">
            <Input value={recipientName} onChange={(e) => setRecipientName(e.target.value)} />
          </Field>

          <Field label="Template key" hint="Stable identifier of the template used. There is no template library in this phase, so the text travels with the draft and is snapshotted on the row.">
            <Input
              value={templateKey}
              onChange={(e) => setTemplateKey(e.target.value)}
              placeholder="attendance_chase_v1"
            />
          </Field>

          <Field label="Subject (optional)">
            <Input value={subject} onChange={(e) => setSubject(e.target.value)} />
          </Field>
        </div>

        <Field
          label="Template text"
          hint="Use {{slot}} markers. No conditionals, no loops, no expressions — a template that can compute is a template that can compute money, and R2 puts every rupee in the engine."
        >
          <Textarea
            rows={6}
            value={template}
            onChange={(e) => setTemplate(e.target.value)}
            className="font-mono text-xs"
            placeholder={'Dear {{college_name}},\n\nAttendance for {{month}} is incomplete.'}
          />
        </Field>

        <div>
          <div className="flex items-center justify-between gap-3 mb-1.5">
            <span className="block text-xs font-medium text-ink-2">
              Structured input — the facts substituted into the template (R1)
            </span>
            <Button
              size="sm"
              variant="secondary"
              onClick={() => setValues((rows) => [...rows, { key: '', value: '' }])}
            >
              Add value
            </Button>
          </div>

          {values.length === 0 ? (
            <p className="text-xs text-ink-3 leading-relaxed">
              No values yet. Every <MonoValue>{'{{slot}}'}</MonoValue> in the template above must
              have one — the server refuses to render an unfilled slot rather than leaving the
              braces in what the recipient reads. Each value is a fact you looked up, never a
              figure you worked out (R1).
            </p>
          ) : (
            <div className="space-y-2">
              {values.map((row, index) => (
                <div key={index} className="flex items-center gap-2">
                  <Input
                    value={row.key}
                    placeholder="slot name"
                    className="font-mono text-xs max-w-[12rem]"
                    onChange={(e) =>
                      setValues((rows) =>
                        rows.map((r, i) => (i === index ? { ...r, key: e.target.value } : r)),
                      )
                    }
                  />
                  <Input
                    value={row.value}
                    placeholder="value from a system of record"
                    onChange={(e) =>
                      setValues((rows) =>
                        rows.map((r, i) => (i === index ? { ...r, value: e.target.value } : r)),
                      )
                    }
                  />
                  <Button
                    size="sm"
                    variant="ghost"
                    aria-label="Remove value"
                    onClick={() => setValues((rows) => rows.filter((_, i) => i !== index))}
                  >
                    ✕
                  </Button>
                </div>
              ))}
            </div>
          )}

          <p className="text-xs text-ink-3 mt-1.5 leading-relaxed">
            Sent as strings. An amount must be a string or an integer — a float is refused by the
            server (R7), never rounded. No figure here may be one you worked out: it comes from a
            query, or it does not appear (R1/R2).
          </p>

          {missing.length > 0 && (
            <div className="mt-2">
              <InfoNote>
                Slots with no value yet:{' '}
                {missing.map((slot, i) => (
                  <span key={slot}>
                    {i > 0 && ', '}
                    <MonoValue>{slot}</MonoValue>
                  </span>
                ))}
                . The server will refuse this draft rather than render a blank — checked here only
                so you hear it before you post.
              </InfoNote>
            </div>
          )}
        </div>

        <Field
          label="Message body — what would actually go out"
          hint="Everything here that is not in the rendered template is drafter-authored prose, and it is what the approver will be shown as the diff."
        >
          <Textarea
            rows={8}
            value={body}
            onChange={(e) => setBody(e.target.value)}
            className="font-mono text-xs"
          />
        </Field>

        {canSeeCommercials ? (
          <label className="flex items-start gap-2">
            <input
              type="checkbox"
              checked={isCommercial}
              onChange={(e) => setIsCommercial(e.target.checked)}
              className="mt-0.5"
            />
            <span className="text-xs text-ink-2 leading-relaxed">
              <strong className="text-ink">This message is about money.</strong> R5: it is then
              readable only by a persona inside the commercials wall. A remuneration back-reference
              forces this true regardless of what is ticked here — in code before the INSERT and by
              a CHECK constraint behind it — but prose about a rate is commercial with or without
              one, so tick it when in doubt.
            </span>
          </label>
        ) : (
          <InfoNote>
            Your persona is outside the commercials wall (§4), so a message about money cannot be
            drafted from this account — the server refuses before the row is written, not after.
          </InfoNote>
        )}

        <details className="rounded-lg border border-line bg-surface-2 px-3 py-2">
          <summary className="text-xs font-medium text-ink-2 cursor-pointer">
            Link to an artifact (optional)
          </summary>
          <div className="grid gap-3 sm:grid-cols-2 mt-2">
            <Field label="Artifact type">
              <Select
                value={artifactType}
                onChange={(e) => setArtifactType(e.target.value as ArtifactType | '')}
              >
                <option value="">None</option>
                <option value="remuneration_sheets">Remuneration sheet</option>
                <option value="governance_reports">Governance report</option>
                <option value="program_documents">Program document</option>
              </Select>
            </Field>
            <Field label="Artifact id" hint="A remuneration back-reference forces the commercial flag true.">
              <Input
                value={artifactId}
                onChange={(e) => setArtifactId(e.target.value)}
                className="font-mono text-xs"
              />
            </Field>
          </div>
        </details>

        {draft.error ? <ActionError error={draft.error} /> : null}

        <div className="flex items-center justify-end gap-2 pt-1">
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="primary"
            disabled={!ready || draft.isPending}
            onClick={() => draft.mutate()}
          >
            {draft.isPending && <Spinner />}
            Put in the queue as a draft
          </Button>
        </div>
      </div>
    </Modal>
  )
}
