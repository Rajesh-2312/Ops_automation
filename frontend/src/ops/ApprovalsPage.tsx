import { useMemo, useState, type ReactNode } from 'react'
import { PAGE, usePageLimit } from '../lib/bounds'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { errorMessage } from '../lib/supabase'
import { CATEGORY_LABEL, RATE_BASIS_LABEL } from '../lib/types'
import {
  APPROVAL_AUTHORITY,
  ARTIFACT_STATE_BLURB,
  ARTIFACT_STATE_LABEL,
  ARTIFACT_TYPE_LABEL,
  AUTHORITY_UNDECIDED_NOTE,
  approvalKeys,
  approveArtifact,
  fetchActorNames,
  fetchArtifactQueue,
  fetchDocumentArtifact,
  fetchGovernanceArtifact,
  fetchRemunerationArtifact,
  fetchVersionHistory,
  isAuthorityUndefined,
  isConflict,
  isForbidden,
  isFrozenContentMismatch,
  rejectArtifact,
  releaseArtifact,
  submitArtifact,
  type ArtifactState,
  type ArtifactType,
  type ArtifactVersion,
  type ArtifactVersionRow,
  type DocumentArtifact,
  type GovernanceArtifact,
  type RemunerationArtifact,
} from '../lib/approvals'
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
  fmtAmount,
  fmtDate,
  HelpTip,
  InfoNote,
  Loading,
  MonoValue,
  PageIntro,
  SearchInput,
  SectionTitle,
  Spinner,
  TableSkeleton,
  Td,
  Textarea,
  Th,
  Toolbar,
} from '../components/ui'

/* --------------------------------------------------------------------------
   Approvals — the screen R4 exists to be operated from.

   "Nothing leaves the system unapproved. Every artifact moves DRAFT →
   PENDING_APPROVAL → APPROVED → RELEASED. [...] Approval and release are
   separate actions with separate audit rows."

   Until this screen existed, `app/api/approvals.py` had no caller and the
   lifecycle was unreachable by a human, which means nothing in the system could
   ever be released. Everything below is that sentence made clickable, and three
   things about it are structural rather than cosmetic:

   1. APPROVE AND RELEASE ARE NEVER THE SAME CLICK, and cannot be rendered as
      one. They live in different panels, they are enabled by different states,
      and the second one does not appear until the first has already returned and
      the queue has refetched. There is no "approve and release" affordance, no
      shortcut, and no place in this file where one call chains into the other.
      They mean different things — approval freezes and hashes the version,
      release is the act of letting it leave — and they produce two separate
      audit rows that a dispute is answered with months later.

   2. NOBODY APPROVES BLIND. The artifact's own content is drawn beside the
      decision: for a remuneration sheet that is the §6 chain, the invoice number
      and the PAN; for the other two it is the document and the period. The
      version number, who submitted it and when, and the full version history are
      all on screen, so a rejected-and-resubmitted artifact reads as exactly that.

   3. THE SERVER DECIDES. `useAuth()` is used for LAYOUT ONLY, per its own
      docstring — a Manager sees the queue and is told, before clicking, that a
      remuneration sheet is Senior Manager's to approve. That is a courtesy, not
      a control: `approvals.py` checks `APPROVAL_AUTHORITY` and 403s regardless
      of what this file drew.

   NO MONEY IS COMPUTED HERE (R2/R7). Every amount arrives as a STRING from a
   `::text` cast and is rendered by `fmtAmount`, which groups digits on the
   string and never parses it.
-------------------------------------------------------------------------- */

type Selection = { type: ArtifactType; id: string }

const STATE_TONE: Record<ArtifactState, string> = {
  DRAFT: 'bg-surface-2 text-ink-3 border-line',
  PENDING_APPROVAL: 'bg-warn-wash text-warn-ink border-warn/25',
  APPROVED: 'bg-info-wash text-info-ink border-info/25',
  RELEASED: 'bg-good-wash text-good-ink border-good/25',
}

const PILL =
  'inline-flex items-center rounded-control border px-1.5 py-0.5 text-[11px] font-medium whitespace-nowrap'

function StatePill({ state }: { state: ArtifactState }) {
  return <span className={`${PILL} ${STATE_TONE[state]}`}>{ARTIFACT_STATE_LABEL[state]}</span>
}

/** The order the queue is worked in: what needs me, then what I have held. */
const STATE_ORDER: ArtifactState[] = ['PENDING_APPROVAL', 'APPROVED', 'DRAFT', 'RELEASED']

export function ApprovalsPage() {
  const { isInternal, canSeeCommercials, profile } = useAuth()
  const [selected, setSelected] = useState<Selection | null>(null)

  // BOUNDED. Every artifact that has ever had a version row stays current
  // forever — RELEASED is terminal, not archived — so this queue only grows.
  // `fetchArtifactQueue` orders by `submitted_at` first, which puts the rows
  // waiting on an approver at the front of the bound and leaves released
  // history as the part that is cut.
  const page = usePageLimit(PAGE.approvals)

  const queue = useQuery({
    queryKey: approvalKeys.queue(page.limit),
    enabled: isInternal,
    queryFn: () => fetchArtifactQueue(page.limit),
  })

  const actors = useQuery({
    queryKey: approvalKeys.actors(PAGE.profiles),
    enabled: isInternal,
    queryFn: () => fetchActorNames(PAGE.profiles),
  })

  const actorName = useMemo(() => {
    const byId = new Map((actors.data?.rows ?? []).map((p) => [p.id, p.full_name]))
    // The uuid, not "Unknown", when a name cannot be resolved. The id is what is
    // on the audit row and it is what answers a dispute.
    return (id: string | null): string => (id ? (byId.get(id) ?? id) : '—')
  }, [actors.data])

  const rows = useMemo(() => {
    const list = queue.data?.rows ?? []
    return [...list].sort(
      (a, b) => STATE_ORDER.indexOf(a.state) - STATE_ORDER.indexOf(b.state),
    )
  }, [queue.data])

  // --- The wall ------------------------------------------------------------
  // Every route on this screen requires `require_internal()` before it reads a
  // row, so a trainer or a college login would get the same 403 for every
  // artifact id. Drawing a queue for them would be presenting an action that
  // cannot work.
  if (!isInternal) {
    return (
      <>
        <PageHeader
          title="Approvals"
          subtitle="Internal staff only"
          purpose="Where byteXL staff sign off the things this system produces before any of them leave the building. Your account is not internal staff, so there is no queue here — you see a report or a document once byteXL has released it to you."
        />
        <Page>
          <div className="max-w-3xl">
            <InfoNote>
              The approval lifecycle is internal governance. Every endpoint behind this screen
              calls <code>require_internal()</code> before it reads a row, so there is
              deliberately no queue here rather than one that would refuse every action. A
              trainer sees their own payout status on their own screen; a college sees an
              artifact once it has been released to them.
            </InfoNote>
          </div>
        </Page>
      </>
    )
  }

  return (
    <>
      <PageHeader
        title="Approvals"
        subtitle="DRAFT → PENDING_APPROVAL → APPROVED → RELEASED. Approval freezes the version; release is a separate act."
        purpose="Nothing byteXL produces — a trainer's pay sheet, a governance report, a signed document — leaves this system until a person has read it, approved it, and then separately released it. Those are two different decisions, and this is the screen both are made on."
      />

      <Page>
        <div className="space-y-4">
          <PageIntro
            steps={[
              'Somebody submits a draft',
              'An approver reads the whole thing',
              'Approving freezes that exact version',
              'Releasing it is a second, separate act',
              'Any later edit starts a new version at draft',
            ]}
          >
            <p>
              Everything byteXL produces for somebody outside this system travels the same four
              states, and nothing skips one. An{' '}
              <HelpTip term="artifact">
                One thing that can be signed off — a trainer's remuneration sheet, a governance
                report, or a program document. The chain of states is identical for all three;
                only who is allowed to approve differs.
              </HelpTip>{' '}
              that has been <strong className="font-medium text-ink">approved</strong> has gone
              precisely nowhere. Approving is byteXL saying “this is right”; releasing is byteXL
              saying “this may now leave”. Two decisions, two panels, and each writes its own{' '}
              <HelpTip term="audit event">
                A permanent row saying who acted, what they did, what the record looked like
                before and after, and when. Every state change on this screen writes one, and a
                payout dispute six months from now is answered out of them.
              </HelpTip>
              .
            </p>
            <p className="mt-2">
              Approval also <strong className="font-medium text-ink">freezes</strong> the version
              and hashes its contents, so nobody can edit an artifact after the fact and leave
              somebody else's sign-off attached to different figures. Editing one that has been
              approved does not amend it — it opens a <em>new</em> version, back at draft, needing
              fresh approval.
            </p>
            <p className="mt-2">
              <strong className="font-medium text-ink">
                No agent in this system can release anything.
              </strong>{' '}
              The drafting agents elsewhere in the console read records and propose wording; not
              one of them is handed a tool that sends, posts or marks something released. Every
              act on this screen needs a signed-in human, and the record says which one.
            </p>
          </PageIntro>

          <LifecycleLegend />

          {queue.error && <ErrorNote>{errorMessage(queue.error)}</ErrorNote>}

          {/* The queue is sorted client-side into PENDING_APPROVAL first, so a
              truncated read can drop a pending artifact off the END of the
              server ordering and out of the group a reader scans first. R4
              makes approval the point at which the organisation is stopped;
              "nothing is waiting on me" has to be true when it is shown. */}
          <BoundNote
            bound={queue.data}
            noun="artifact versions"
            derived="An artifact beyond the bound is not in the list below, whatever its state."
            onMore={page.more}
            step={page.step}
          />

          {queue.isPending ? (
            /* A skeleton rather than a centred spinner: the queue is a table,
               and a spinner collapses the layout and then springs it back the
               moment rows land. */
            <Card>
              <div className="px-4 pt-4">
                <SectionTitle
                  title="Queue"
                  subtitle="Reading every artifact version you can reach…"
                />
              </div>
              <TableSkeleton rows={6} cols={3} />
            </Card>
          ) : rows.length === 0 ? (
            <Card>
              <EmptyState
                title="Nothing is in the lifecycle yet"
                body={
                  'An artifact appears here once it has a version row, and a version row is opened ' +
                  'by submitting it for approval. A payout has to be COMMITTED to remuneration_sheets ' +
                  'before it can be submitted — computing one on the Payouts screen produces a draft ' +
                  'sheet and persists nothing. Until that endpoint lands, this queue is legitimately ' +
                  'empty; it is not broken.'
                }
                hint="Nothing is filtered out — this is everything you can reach. An artifact reaches this list only after somebody submits it, so an empty queue means nobody is waiting on you."
              />
            </Card>
          ) : (
            <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.2fr)] items-start">
              <QueueTable
                rows={rows}
                selected={selected}
                actorName={actorName}
                onSelect={(row) => setSelected({ type: row.artifact_type, id: row.artifact_id })}
              />

              {selected ? (
                <ArtifactPanel
                  key={`${selected.type}:${selected.id}`}
                  selection={selected}
                  row={
                    rows.find(
                      (r) => r.artifact_type === selected.type && r.artifact_id === selected.id,
                    ) ?? null
                  }
                  actorName={actorName}
                  canSeeCommercials={canSeeCommercials}
                  isSeniorManager={profile?.role === 'senior_manager'}
                />
              ) : (
                <Card>
                  <EmptyState
                    title="Select an artifact"
                    body="Nothing is approved from a list row. Open one and the panel shows what is actually being signed off — the figures, the version, who submitted it and the full history."
                    hint="Pick any row on the left. The approve and release actions only appear once something is open, because neither is a decision anyone should make from a summary line."
                  />
                </Card>
              )}
            </div>
          )}
        </div>
      </Page>
    </>
  )
}

/**
 * The four states, said once at the top.
 *
 * APPROVED gets the longest line on purpose: it is the state people misread. An
 * approved artifact has been frozen and hashed and has gone precisely nowhere.
 */
function LifecycleLegend() {
  return (
    <Card className="p-4">
      <SectionTitle
        title="The lifecycle"
        subtitle="Four states, and two of them are not one state. Approval and release are separate acts with separate audit rows (R4)."
      />
      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
        {STATE_ORDER.slice()
          .sort(
            (a, b) =>
              (['DRAFT', 'PENDING_APPROVAL', 'APPROVED', 'RELEASED'] as ArtifactState[]).indexOf(
                a,
              ) -
              (['DRAFT', 'PENDING_APPROVAL', 'APPROVED', 'RELEASED'] as ArtifactState[]).indexOf(
                b,
              ),
          )
          .map((state, i) => (
            <div key={state} className="rounded-card border border-line bg-surface-2 px-3 py-2.5">
              <div className="flex items-center gap-2">
                <span
                  aria-hidden
                  className="inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-full
                    bg-accent-soft text-[10px] font-semibold tabular-nums text-accent-ink"
                >
                  {i + 1}
                </span>
                <StatePill state={state} />
              </div>
              {/* The raw enum name as well as the friendly one. It is what the
                  API, the audit rows and every error message on this screen
                  say, and a reader who only ever sees “Pending approval” has
                  nothing to match PENDING_APPROVAL against when one appears. */}
              <MonoValue className="mt-1.5 block text-[10px] text-ink-3">{state}</MonoValue>
              <p className="text-[11px] text-ink-2 mt-1.5 leading-relaxed">
                {ARTIFACT_STATE_BLURB[state]}
              </p>
            </div>
          ))}
      </div>
    </Card>
  )
}

function QueueTable({
  rows,
  selected,
  actorName,
  onSelect,
}: {
  rows: ArtifactVersionRow[]
  selected: Selection | null
  actorName: (id: string | null) => string
  onSelect: (row: ArtifactVersionRow) => void
}) {
  // Display-only. It narrows what is DRAWN from the rows already fetched and
  // touches no query — but a filter that narrows in silence is how somebody
  // concludes an artifact was never submitted, so `SearchInput` prints the
  // count it survived (DESIGN.md §4.4).
  const [query, setQuery] = useState('')
  const needle = query.trim().toLowerCase()
  const shown = useMemo(
    () =>
      needle === ''
        ? rows
        : rows.filter((row) =>
            [
              ARTIFACT_TYPE_LABEL[row.artifact_type],
              ARTIFACT_STATE_LABEL[row.state],
              row.artifact_id,
              `v${row.version}`,
              actorName(row.submitted_by),
            ]
              .join(' ')
              .toLowerCase()
              .includes(needle),
          ),
    [rows, needle, actorName],
  )

  return (
    <Card>
      <div className="px-4 pt-4">
        <SectionTitle
          title="Queue"
          subtitle="Every current version you can reach. Pending first, then approved-but-not-released — the pile R4 creates on purpose."
          action={<Badge>{rows.length}</Badge>}
        />
        <Toolbar className="mb-3">
          <SearchInput
            value={query}
            onChange={setQuery}
            placeholder="Artifact, state, id or who submitted it"
            count={shown.length}
            total={rows.length}
          />
        </Toolbar>
      </div>
      <div className="overflow-x-auto scroll-slim">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-line">
              <Th>Artifact</Th>
              <Th>State</Th>
              <Th>Submitted</Th>
            </tr>
          </thead>
          <tbody className="divide-y divide-line-soft">
            {shown.map((row) => {
              const active =
                selected?.type === row.artifact_type && selected?.id === row.artifact_id
              return (
                <tr
                  key={row.id}
                  onClick={() => onSelect(row)}
                  className={`cursor-pointer transition ${
                    active ? 'bg-accent-soft' : 'hover:bg-surface-2'
                  }`}
                >
                  <Td>
                    <span className="font-medium text-ink">
                      {ARTIFACT_TYPE_LABEL[row.artifact_type]}
                    </span>
                    <span className="block text-[11px] text-ink-3 mt-0.5">
                      <MonoValue className="text-[11px]">{row.artifact_id}</MonoValue>
                    </span>
                    <span className="block text-[11px] text-ink-3 mt-0.5">v{row.version}</span>
                  </Td>
                  <Td>
                    <StatePill state={row.state} />
                  </Td>
                  <Td className="text-xs text-ink-2">
                    {row.submitted_at ? (
                      <>
                        {fmtDate(row.submitted_at)}
                        <span className="block text-[11px] text-ink-3">
                          by {actorName(row.submitted_by)}
                        </span>
                      </>
                    ) : (
                      <span className="text-ink-3">Not submitted</span>
                    )}
                  </Td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      {shown.length === 0 && (
        <EmptyState
          title="Nothing in the queue matches that"
          body="The box searches the artifact type, its state, its id, its version number and the name of whoever submitted it."
          hint={`All ${rows.length} version${rows.length === 1 ? '' : 's'} are still here. Clear the search to see them again.`}
        />
      )}
    </Card>
  )
}

/**
 * One artifact, in full: what it says, where it is, and the two — never one —
 * actions available from here.
 */
function ArtifactPanel({
  selection,
  row,
  actorName,
  canSeeCommercials,
  isSeniorManager,
}: {
  selection: Selection
  row: ArtifactVersionRow | null
  actorName: (id: string | null) => string
  canSeeCommercials: boolean
  isSeniorManager: boolean
}) {
  const queryClient = useQueryClient()
  const { type, id } = selection

  const history = useQuery({
    queryKey: approvalKeys.history(type, id),
    queryFn: () => fetchVersionHistory(type, id),
    retry: false,
  })

  // One query for three systems of record: the panel is generic over artifact
  // type, and the branch below narrows it back before anything is rendered.
  const artifact = useQuery<
    RemunerationArtifact | GovernanceArtifact | DocumentArtifact | null
  >({
    queryKey: approvalKeys.artifact(type, id),
    queryFn: () =>
      type === 'remuneration_sheets'
        ? fetchRemunerationArtifact(id)
        : type === 'governance_reports'
          ? fetchGovernanceArtifact(id)
          : fetchDocumentArtifact(id),
  })

  const [reason, setReason] = useState('')
  const [releaseNote, setReleaseNote] = useState('')

  /**
   * Every transition invalidates the queue, the history and the artifact, and
   * NOTHING chains into another call. A mutation's success handler here refetches
   * and stops — the next act is a separate decision by a human who could refuse
   * it, which is the whole of R4's second sentence.
   */
  const settle = () => {
    // The whole `approvals` namespace, not one page size: the queue may be
    // cached at several bounds if somebody has loaded more, and every one of
    // them counted the artifact that just moved.
    void queryClient.invalidateQueries({ queryKey: approvalKeys.all })
    void queryClient.invalidateQueries({ queryKey: approvalKeys.history(type, id) })
    void queryClient.invalidateQueries({ queryKey: approvalKeys.artifact(type, id) })
  }

  const submit = useMutation<ArtifactVersion, unknown, void>({
    mutationFn: () => submitArtifact(type, id),
    onSuccess: settle,
  })
  const approve = useMutation<ArtifactVersion, unknown, void>({
    mutationFn: () => approveArtifact(type, id),
    onSuccess: settle,
  })
  const reject = useMutation<ArtifactVersion, unknown, void>({
    mutationFn: () => rejectArtifact(type, id, reason),
    onSuccess: () => {
      setReason('')
      settle()
    },
  })
  const release = useMutation<ArtifactVersion, unknown, void>({
    mutationFn: () => releaseArtifact(type, id, releaseNote),
    onSuccess: () => {
      setReleaseNote('')
      settle()
    },
  })

  const state = row?.state ?? null
  const authority = APPROVAL_AUTHORITY[type]
  const busy = submit.isPending || approve.isPending || reject.isPending || release.isPending

  return (
    <div className="space-y-4">
      <Card className="p-4">
        <SectionTitle
          title={ARTIFACT_TYPE_LABEL[type]}
          subtitle={row ? `Version ${row.version}` : 'No current version row'}
          action={state ? <StatePill state={state} /> : undefined}
        />

        {/* What is being approved. Drawn before any button, deliberately. */}
        {artifact.isPending ? (
          <Loading label="Loading the artifact" />
        ) : artifact.error ? (
          <ErrorNote>{errorMessage(artifact.error)}</ErrorNote>
        ) : artifact.data === null ? (
          <InfoNote>
            The lifecycle row is visible to you but the artifact itself is not. That is the
            commercials wall doing its job in the database rather than in this UI (CLAUDE.md §4,
            R5) — approving something you cannot read is exactly what this screen exists to
            prevent, so no action is offered.
          </InfoNote>
        ) : type === 'remuneration_sheets' ? (
          <RemunerationContent
            artifact={artifact.data as RemunerationArtifact}
            canSeeCommercials={canSeeCommercials}
          />
        ) : type === 'governance_reports' ? (
          <GovernanceContent artifact={artifact.data as GovernanceArtifact} />
        ) : (
          <DocumentContent artifact={artifact.data as DocumentArtifact} />
        )}

        {row && (
          <div className="mt-4 pt-3 border-t border-line grid gap-1.5 sm:grid-cols-2">
            <Meta label="Submitted by" value={actorName(row.submitted_by)} />
            <Meta label="Submitted at" value={fmtDate(row.submitted_at)} />
            <Meta label="Approved by" value={actorName(row.approved_by)} />
            <Meta label="Approved at" value={fmtDate(row.approved_at)} />
            <Meta
              label={
                <HelpTip term="Content hash">
                  A fingerprint taken of the artifact's contents at the moment it was approved.
                  Before release the server re-reads the artifact and re-takes it: if the two do
                  not match, the source row was edited after somebody signed it off, and the
                  release is refused rather than carrying their name over different figures.
                </HelpTip>
              }
              value={row.content_hash ?? 'Not frozen — no approval yet'}
              mono={row.content_hash !== null}
            />
            <Meta label="Notes" value={row.notes ?? '—'} />
          </div>
        )}
      </Card>

      {/* --- The acts. Two panels, never one button. --------------------- */}
      <ApprovalActions
        type={type}
        state={state}
        authority={authority}
        isSeniorManager={isSeniorManager}
        busy={busy}
        reason={reason}
        onReason={setReason}
        onSubmit={() => submit.mutate()}
        onApprove={() => approve.mutate()}
        onReject={() => reject.mutate()}
        submitError={submit.error}
        approveError={approve.error}
        rejectError={reject.error}
      />

      <ReleaseAction
        state={state}
        busy={busy}
        note={releaseNote}
        onNote={setReleaseNote}
        onRelease={() => release.mutate()}
        error={release.error}
      />

      <HistoryCard
        pending={history.isPending}
        error={history.error}
        versions={history.data?.versions ?? []}
        actorName={actorName}
      />
    </div>
  )
}

/**
 * Submit, approve and reject — everything up TO the freeze, and nothing past it.
 *
 * Release is deliberately not in this component. It is a different act, in a
 * different panel, and a reader of this file should have to scroll to find it.
 */
function ApprovalActions({
  type,
  state,
  authority,
  isSeniorManager,
  busy,
  reason,
  onReason,
  onSubmit,
  onApprove,
  onReject,
  submitError,
  approveError,
  rejectError,
}: {
  type: ArtifactType
  state: ArtifactState | null
  authority: string | null
  isSeniorManager: boolean
  busy: boolean
  reason: string
  onReason: (value: string) => void
  onSubmit: () => void
  onApprove: () => void
  onReject: () => void
  submitError: unknown
  approveError: unknown
  rejectError: unknown
}) {
  // A blank-looking reason is how a required field becomes a formality. The API
  // 422s on it; the button is not pressable before it does.
  const reasonReady = reason.trim().length > 0

  return (
    <Card className="p-4">
      <SectionTitle
        title="Approval"
        subtitle="Approving FREEZES the version and hashes its content. It sends nothing."
      />

      {/* §14 Q3, said before anything is clicked rather than after a 501. */}
      {authority === null ? (
        <div className="rounded-card border border-info/30 bg-info-wash px-3 py-2.5">
          <p className="text-sm font-medium text-info-ink">
            No approval authority has been decided for a {ARTIFACT_TYPE_LABEL[type].toLowerCase()}.
          </p>
          <p className="text-xs text-ink-2 mt-1.5 leading-relaxed">{AUTHORITY_UNDECIDED_NOTE}</p>
        </div>
      ) : (
        <InfoNote>
          A {ARTIFACT_TYPE_LABEL[type].toLowerCase()} is approved by a{' '}
          <strong>{authority}</strong> — CLAUDE.md §4 puts payout approval in that column.
          {!isSeniorManager && (
            <>
              {' '}
              Your account is not one, so the approve and reject buttons below are drawn but the
              server will refuse them: authority is checked in{' '}
              <code>APPROVAL_AUTHORITY</code>, not here. You can read the whole artifact and its
              history — that is what a Manager needs in order to ask for it to be signed off.
            </>
          )}
        </InfoNote>
      )}

      <div className="mt-3 space-y-3">
        {state === null && (
          <p className="text-sm text-ink-3">
            This artifact has no version row. Submitting it opens version 1 in draft and moves it
            straight to pending approval.
          </p>
        )}

        {(state === null || state === 'DRAFT') && (
          <div>
            <Button variant="primary" disabled={busy} onClick={onSubmit}>
              {busy && <Spinner />}
              Submit for approval
            </Button>
            <p className="text-xs text-ink-3 mt-1.5 leading-relaxed">
              Putting work in front of a human needs no approval authority of its own — whether
              the artifact is FIT to be submitted is the producing domain's question (for a payout,
              the §7 gates on the Payouts screen).
            </p>
            {submitError ? <div className="mt-2"><ActionError error={submitError} /></div> : null}
          </div>
        )}

        {state === 'PENDING_APPROVAL' && (
          <>
            {/* ACT ONE OF TWO, and it is framed as such.
                The frame is `info` because `info` is what the APPROVED pill is
                on this same screen: an act is coloured by the state it
                produces, so a reader can see where the button lands before
                pressing it. The consequence list sits ABOVE the button on
                purpose — DESIGN.md §4.7, an irreversible act says what it will
                do before it does it, and approval cannot be walked back. */}
            <div className="rounded-card border border-info/30 bg-info-wash p-3.5">
              <p className="text-[11px] font-semibold uppercase tracking-wide text-info-ink">
                Act 1 of 2 · Approve
              </p>
              <ul className="mt-2 space-y-1 text-xs text-ink-2 leading-relaxed">
                <li>
                  ·{' '}
                  <HelpTip term="Freezes">
                    Takes a fingerprint of the artifact's contents and stores it on the version.
                    From then on the system can tell whether what it is looking at is still the
                    thing that was signed off, and it refuses to release it if it is not.
                  </HelpTip>{' '}
                  this exact version and records that you are the one who signed it off.
                </li>
                <li>· Writes an audit row: you, the act, the state before and after, the time.</li>
                <li>
                  ·{' '}
                  <strong className="font-medium text-ink">
                    Sends nothing, to nobody, anywhere.
                  </strong>{' '}
                  Letting it leave is Act 2, in the next panel, by a human who could still refuse.
                </li>
              </ul>
              <Button className="mt-3" variant="primary" disabled={busy} onClick={onApprove}>
                {busy && <Spinner />}
                Approve — freeze this version
              </Button>
              {approveError ? (
                <div className="mt-2">
                  <ActionError error={approveError} />
                </div>
              ) : null}
            </div>

            <div className="pt-3 border-t border-line">
              <Field
                label="Rejection reason — required"
                hint="Recorded on the audit row and on the version, so the person reworking it can read it without querying the audit trail. The API refuses a blank one."
              >
                <Textarea
                  rows={3}
                  value={reason}
                  onChange={(e) => onReason(e.target.value)}
                  placeholder="What has to change before this can be approved…"
                />
              </Field>
              <div className="mt-2 flex items-center gap-3">
                <Button variant="danger" disabled={busy || !reasonReady} onClick={onReject}>
                  {busy && <Spinner />}
                  Send back to draft
                </Button>
                {!reasonReady && (
                  <span className="text-xs text-ink-3">
                    Type a reason. Whitespace does not count.
                  </span>
                )}
              </div>
              <p className="text-xs text-ink-3 mt-1.5 leading-relaxed">
                The version number does not change. A rejected draft is the same version being
                reworked — only a frozen artifact is superseded.
              </p>
              {rejectError ? (
                <div className="mt-2">
                  <ActionError error={rejectError} />
                </div>
              ) : null}
            </div>
          </>
        )}

        {(state === 'APPROVED' || state === 'RELEASED') && (
          <p className="text-sm text-ink-3">
            Already approved. An approved version cannot be walked back into draft — R4 gives
            exactly one way out of it, and it is forwards. Editing the artifact does not amend
            what was signed off: it opens a <em>new</em> version, starting again at draft, and
            that version needs approving from scratch. The frozen one stays as the record of what
            somebody actually put their name to.
          </p>
        )}
      </div>
    </Card>
  )
}

/**
 * Release. Its own card, its own button, its own audit row.
 *
 * It only draws an action once the state is APPROVED, so it is impossible to
 * reach without the approve click having already happened and returned. It also
 * says what release is NOT: it transmits nothing. §8 gives the outbound queue to
 * the Comms service, which may only act on an artifact that is already RELEASED.
 */
function ReleaseAction({
  state,
  busy,
  note,
  onNote,
  onRelease,
  error,
}: {
  state: ArtifactState | null
  busy: boolean
  note: string
  onNote: (value: string) => void
  onRelease: () => void
  error: unknown
}) {
  return (
    <Card className="p-4">
      <SectionTitle
        title="Release"
        subtitle="A separate act from approval, with a separate audit row. It marks state — it sends nothing."
      />

      {state === 'RELEASED' ? (
        <p className="text-sm text-ink-3">
          Released. This is terminal: a change from here is a new version, starting again at
          draft and requiring fresh approval.
        </p>
      ) : state !== 'APPROVED' ? (
        <p className="text-sm text-ink-3">
          Nothing to release. An artifact becomes releasable only once it has been approved and
          frozen — there is no path from draft or pending approval to released, and attempting
          one is refused by the state machine rather than hidden by this screen.
        </p>
      ) : (
        <>
          <Field
            label="Release note (optional)"
            hint="Commentary ABOUT the version, never part of it. It is not hashed and cannot alter what was approved."
          >
            <Textarea
              rows={2}
              value={note}
              onChange={(e) => onNote(e.target.value)}
              placeholder="Anything the record should carry about this release…"
            />
          </Field>
          {/* ACT TWO, coloured `good` to match the RELEASED pill, exactly as Act
              One is coloured to match APPROVED. Same frame, different colour,
              different card — the one thing this screen must never let a
              newcomer do is read "approve" as "send". */}
          <div className="mt-3 rounded-card border border-good/30 bg-good-wash p-3.5">
            <p className="text-[11px] font-semibold uppercase tracking-wide text-good-ink">
              Act 2 of 2 · Release
            </p>
            <ul className="mt-2 space-y-1 text-xs text-ink-2 leading-relaxed">
              <li>
                · Re-reads the artifact and re-checks it against the fingerprint taken at
                approval. If the source row was edited in between, this refuses — the freeze
                working, not a fault.
              </li>
              <li>· Writes a second audit row, separate from the approval's.</li>
              <li>
                · Marks the artifact as cleared to leave.{' '}
                <strong className="font-medium text-ink">
                  It still does not send it anywhere.
                </strong>{' '}
                Anything that goes out is queued and approved separately by the comms service,
                which may only touch an artifact that has already been released.
              </li>
            </ul>
            <Button className="mt-3" variant="primary" disabled={busy} onClick={onRelease}>
              {busy && <Spinner />}
              Release
            </Button>
          </div>
          {error ? (
            <div className="mt-2">
              <ActionError error={error} />
            </div>
          ) : null}
        </>
      )}
    </Card>
  )
}

/**
 * A refusal, said as what it actually was.
 *
 * The three that must never render as a generic red box:
 *
 *   501 — §14 Q3 is open. Not a permission problem, not an outage.
 *   409 + hash — the artifact changed after approval. An integrity signal.
 *   403 — the wall or the authority table. Names which, and where reach comes from.
 */
function ActionError({ error }: { error: unknown }) {
  if (isAuthorityUndefined(error)) {
    return (
      <div className="rounded-card border border-info/30 bg-info-wash px-3 py-2.5" role="status">
        <p className="text-sm font-medium text-info-ink">
          Not implemented — nobody has decided who approves this.
        </p>
        <p className="text-xs text-ink-2 mt-1.5 leading-relaxed">{AUTHORITY_UNDECIDED_NOTE}</p>
        <p className="text-[11px] text-ink-3 mt-1.5 font-mono break-words">
          {errorMessage(error)}
        </p>
      </div>
    )
  }

  if (isFrozenContentMismatch(error)) {
    return (
      <ErrorNote>
        <strong>The artifact changed after it was approved.</strong> The content re-read from the
        system of record no longer matches the hash frozen at approval, so this release was
        refused rather than carrying somebody else's signature over different content. That edit
        should have created a NEW version in draft requiring fresh approval (R4). Do not retry —
        find out what changed, then supersede this version.
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
        <strong>The server refused this account.</strong> {errorMessage(error)} Either the
        artifact is behind the commercials wall (§4: Senior Manager and Manager only), or approval
        authority for it belongs to another persona — a remuneration sheet is a Senior Manager's
        to approve, and the power to withhold approval is the power to approve, so rejection is
        refused on the same grounds. Reach comes from your college and cluster assignments, not
        from your role alone.
      </ErrorNote>
    )
  }

  return <ErrorNote>{errorMessage(error)}</ErrorNote>
}

/**
 * The history, oldest first, as the API returns it — read as a narrative.
 *
 * This is where "it was rejected and came back" is legible: a version that was
 * submitted, sent back to draft with a reason, and submitted again shows all of
 * it on one row, and a superseded version shows when it stopped being current.
 */
function HistoryCard({
  pending,
  error,
  versions,
  actorName,
}: {
  pending: boolean
  error: unknown
  versions: ArtifactVersion[]
  actorName: (id: string | null) => string
}) {
  return (
    <Card>
      <div className="px-4 pt-4">
        <SectionTitle
          title="Version history"
          subtitle="Oldest first. Drafted, submitted, rejected, resubmitted, approved, released — the two right-hand columns are two separate acts."
        />
      </div>

      {pending ? (
        <TableSkeleton rows={3} cols={5} />
      ) : error ? (
        <div className="px-4 pb-4">
          <ActionError error={error} />
        </div>
      ) : versions.length === 0 ? (
        <EmptyState
          title="Nothing has happened to this artifact yet"
          body="It exists, and it has never entered the lifecycle. Submitting it for approval is what opens version 1."
          hint="History is written by the four acts — submit, approve, reject, release — so a blank history means none of them has been taken, not that a record is missing."
        />
      ) : (
        <div className="overflow-x-auto scroll-slim">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-line">
                <Th>Version</Th>
                <Th>State</Th>
                <Th>Submitted</Th>
                <Th>Approved</Th>
                <Th>Released</Th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line-soft">
              {versions.map((v) => (
                <tr key={v.id} className={v.is_current ? '' : 'opacity-60'}>
                  <Td>
                    <span className="font-medium text-ink tabular-nums">v{v.version}</span>
                    {v.is_current ? (
                      <span className="block text-[11px] text-ink-3 mt-0.5">current</span>
                    ) : (
                      <span className="block text-[11px] text-ink-3 mt-0.5">
                        superseded {fmtDate(v.superseded_at)}
                      </span>
                    )}
                    {v.notes && (
                      <span className="block text-[11px] text-ink-2 mt-1 leading-relaxed">
                        “{v.notes}”
                      </span>
                    )}
                  </Td>
                  <Td>
                    <StatePill state={v.state} />
                    {v.content_hash && (
                      <span className="block mt-1 truncate max-w-[10rem]">
                        <MonoValue className="text-[11px] text-ink-3" title={v.content_hash}>
                          {v.content_hash}
                        </MonoValue>
                      </span>
                    )}
                  </Td>
                  <Td className="text-xs text-ink-2">
                    <Event at={v.submitted_at} by={v.submitted_by} actorName={actorName} />
                  </Td>
                  <Td className="text-xs text-ink-2">
                    <Event at={v.approved_at} by={v.approved_by} actorName={actorName} />
                  </Td>
                  <Td className="text-xs text-ink-2">
                    <Event at={v.released_at} by={v.released_by} actorName={actorName} />
                  </Td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  )
}

function Event({
  at,
  by,
  actorName,
}: {
  at: string | null
  by: string | null
  actorName: (id: string | null) => string
}) {
  if (!at) return <span className="text-ink-3">—</span>
  return (
    <>
      {fmtDate(at)}
      <span className="block text-[11px] text-ink-3">{actorName(by)}</span>
    </>
  )
}

// --- The artifacts, rendered -------------------------------------------------

/**
 * The §6 chain, exactly as it is stored. Every figure is a string.
 *
 * `fmtAmount` groups digits on the string and hands it back; nothing here parses,
 * adds or rounds a rupee (R2/R7). `amount_in_words` is the one Python generated
 * in Indian numbering and is shown verbatim — a renderer written here could
 * disagree with the invoice, and disagreeing with the invoice is the failure.
 */
function RemunerationContent({
  artifact,
  canSeeCommercials,
}: {
  artifact: RemunerationArtifact
  canSeeCommercials: boolean
}) {
  if (!canSeeCommercials) {
    // Unreachable in practice — RLS would have returned no row — but stated
    // rather than assumed, because "the query came back empty" and "this persona
    // may not see it" must not be told apart by accident.
    return (
      <InfoNote>
        A remuneration sheet is{' '}
        <HelpTip term="commercial data">
          Money: rates, payouts, invoices, programme P&amp;L. Senior Managers and Managers see it;
          an LDE Executive never does. That boundary is enforced in the database, so an account
          outside it gets no rows at all rather than a blanked-out screen.
        </HelpTip>{' '}
        (§4). This account is outside the wall, so its figures are not drawn.
      </InfoNote>
    )
  }

  return (
    <div className="space-y-3">
      <div className="text-sm text-ink flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <span className="font-medium">{artifact.trainers?.full_name ?? 'Unknown trainer'}</span>
        <span className="text-ink-3" aria-hidden>
          ·
        </span>
        <span className="inline-flex items-baseline gap-1.5 text-xs text-ink-2">
          <HelpTip term="PAN">
            Permanent Account Number. This is how byteXL identifies a trainer — never the name,
            because names repeat and get spelled two ways — and the invoice number is seeded from
            its first four characters.
          </HelpTip>
          <MonoValue className="text-ink-2">{artifact.trainers?.pan ?? 'no PAN'}</MonoValue>
        </span>
      </div>
      <p className="text-xs text-ink-2">
        {artifact.programs?.colleges?.name ?? '—'} · {artifact.programs?.name ?? '—'}
        {artifact.programs?.type ? ` (${artifact.programs.type})` : ''} ·{' '}
        {fmtDate(artifact.period_start)} → {fmtDate(artifact.period_end)}
      </p>

      <div className="overflow-x-auto scroll-slim">
        <table className="w-full text-sm">
          <tbody className="divide-y divide-line-soft">
            <Line
              label="Rate"
              value={fmtAmount(artifact.rate)}
              note={artifact.rate_basis ? RATE_BASIS_LABEL[artifact.rate_basis] : undefined}
            />
            <Line
              label={
                <HelpTip term="Payable days">
                  The number of days this pay is worked out from. On a per-day CRT engagement
                  they are counted <strong>up</strong> from the days marked present; on a monthly
                  bCAP retainer they are counted <strong>down</strong> from the length of the
                  period. Same words, opposite arithmetic.
                </HelpTip>
              }
              value={`${artifact.payable_days ?? '—'} of ${artifact.days_in_month ?? '—'}`}
              note={
                artifact.programs?.type === 'CRT' ? (
                  <>
                    <HelpTip term="CRT">
                      A per-day engagement: the trainer is paid for the days they were there, so
                      the days marked present are added up.
                    </HelpTip>{' '}
                    counts UP from P marks — an unmarked day pays nothing.
                  </>
                ) : (
                  <>
                    <HelpTip term="bCAP">
                      A monthly retainer engagement: the trainer is paid for the month and
                      absences are taken off it.
                    </HelpTip>{' '}
                    counts DOWN from the period length — an unmarked day is paid.
                  </>
                )
              }
            />
            <Line label="Earned" value={fmtAmount(artifact.earned)} strong />
            <Line label="TA & DA" value={fmtAmount(artifact.ta_da)} />
            <Line label="Accommodation" value={fmtAmount(artifact.accommodation)} />
            <Line label="Travel reimbursement" value={fmtAmount(artifact.travel_reimb)} />
            <Line label="Gross" value={fmtAmount(artifact.gross)} strong />
            <Line
              label={
                <>
                  <HelpTip term="TDS">
                    Tax Deducted at Source. byteXL withholds it from the trainer's pay and remits
                    it to the tax authority on their behalf, so the trainer receives the figure
                    net of it.
                  </HelpTip>
                  {artifact.tds_rate ? ` @ ${artifact.tds_rate}` : ''}
                </>
              }
              value={`− ${fmtAmount(artifact.tds)}`}
              note="Levied on Earned, never on Gross — which is why it excludes the reimbursements above (§6)."
            />
            <Line label="Deductions" value={`− ${fmtAmount(artifact.deductions)}`} />
            <Line label="Net pay" value={fmtAmount(artifact.net_amount)} strong />
          </tbody>
        </table>
      </div>

      {artifact.amount_in_words && (
        <div className="rounded-card border border-line bg-surface-2 px-3 py-2.5">
          <p className="text-xs text-ink-3">
            <HelpTip term="Net pay, in words">
              The same figure spelled out in Indian numbering — lakh and crore — because that is
              what the invoice carries and what a bank reads back. It is written by the payout
              engine, not here and not by a model, so that this line and the invoice cannot
              disagree.
            </HelpTip>
          </p>
          <p className="text-sm text-ink mt-1 leading-relaxed">{artifact.amount_in_words}</p>
        </div>
      )}

      <div className="rounded-card border border-line bg-surface-2 px-3 py-2.5 space-y-1.5">
        <Meta
          label={
            <HelpTip term="Invoice number">
              Built from the trainer's PAN, the financial year and the payout month — BCDP/26-27/
              JUL1. It is issued once against that combination and cannot be issued twice, which
              is one of the checks a payout has to clear before it can be submitted at all.
            </HelpTip>
          }
          value={artifact.invoice_no ?? 'Not issued'}
          mono={artifact.invoice_no !== null}
        />
        <Meta label="Invoice PAN" value={artifact.invoice_pan ?? '—'} mono={!!artifact.invoice_pan} />
        <Meta label="Currency" value={artifact.currency} />
        <Meta label="Payout status" value={artifact.payout_status} />
      </div>
    </div>
  )
}

function GovernanceContent({ artifact }: { artifact: GovernanceArtifact }) {
  return (
    <div className="space-y-3">
      <p className="text-sm font-medium text-ink">{artifact.title ?? 'Untitled report'}</p>
      <p className="text-xs text-ink-2">
        {artifact.programs?.colleges?.name ?? '—'} · {artifact.programs?.name ?? '—'} ·{' '}
        {fmtDate(artifact.reporting_period_start)} → {fmtDate(artifact.reporting_period_end)}
      </p>
      <div className="rounded-card border border-line bg-surface-2 px-3 py-2.5 space-y-1.5">
        <Meta label="Document" value={artifact.url} />
        <Meta
          label="Shared with college"
          value={
            artifact.shared_with_college_at
              ? fmtDate(artifact.shared_with_college_at)
              : 'Not shared — internal'
          }
        />
      </div>
      <InfoNote>
        The report itself lives at that link and is not mirrored here. What is approved is the
        record — the program, the period and the document it points at — and that is what gets
        hashed and frozen. Open it before approving. A governance report may have been{' '}
        <HelpTip term="drafted by an agent">
          The Reporting agent can assemble the wording of a governance report, and anything it
          writes is marked in orange wherever this console shows it. Its ceiling is draft: it
          cannot approve, it cannot release, and it holds no tool that sends. Whether the words
          are right is your judgement, not its.
        </HelpTip>
        , so read it rather than skimming the record around it.
      </InfoNote>
    </div>
  )
}

function DocumentContent({ artifact }: { artifact: DocumentArtifact }) {
  return (
    <div className="space-y-3">
      <p className="text-sm font-medium text-ink">{artifact.name}</p>
      <p className="text-xs text-ink-2">
        {artifact.programs?.colleges?.name ?? '—'} · {artifact.programs?.name ?? '—'}
      </p>
      <div className="rounded-card border border-line bg-surface-2 px-3 py-2.5 space-y-1.5">
        <Meta label="Category" value={CATEGORY_LABEL[artifact.category] ?? artifact.category} />
        <Meta label="Status" value={artifact.status} />
        <Meta label="Due" value={fmtDate(artifact.due_date)} />
        <Meta label="Filed" value={fmtDate(artifact.filed_at)} />
        <Meta label="Document" value={artifact.url ?? 'No link on file'} />
      </div>
    </div>
  )
}

function Line({
  label,
  value,
  note,
  strong = false,
}: {
  /** ReactNode rather than string so a line can carry its own `HelpTip` — the
   *  §6 chain is where the vocabulary is thickest and a definition three cards
   *  away is a definition nobody reads (DESIGN.md §4.2). */
  label: ReactNode
  value: string
  note?: ReactNode
  strong?: boolean
}) {
  return (
    <tr>
      <Td>
        <span className={strong ? 'font-medium text-ink' : 'text-ink-2'}>{label}</span>
        {note && (
          <span className="block text-[11px] text-ink-3 mt-0.5 leading-relaxed">{note}</span>
        )}
      </Td>
      <Td
        className={`text-right tabular-nums whitespace-nowrap ${
          strong ? 'font-semibold text-ink' : 'text-ink-2'
        }`}
      >
        {value}
      </Td>
    </tr>
  )
}

function Meta({
  label,
  value,
  mono = false,
}: {
  label: ReactNode
  value: string
  mono?: boolean
}) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <span className="text-xs text-ink-3 shrink-0">{label}</span>
      <span className="text-xs text-ink text-right break-all">
        {/* Identifiers go through `MonoValue`, which is also select-all: every
            real use of an invoice number or a PAN is copying it into ZOHO, a
            bank form or a dispute thread, and a half-selected one is worse than
            none at all. */}
        {mono ? <MonoValue title={value}>{value}</MonoValue> : value}
      </span>
    </div>
  )
}
