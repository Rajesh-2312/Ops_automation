import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { PAGE, bounded, boundedFromServer, type Bounded } from '../lib/bounds'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { errorMessage, supabase } from '../lib/supabase'
import { fetchActorNames } from '../lib/approvals'
import {
  COPY_OUTCOME_LABEL,
  DRIFT_NOTE,
  DRIFT_WITHOUT_DIFFERENCES_NOTE,
  ERM_OPEN_STATES,
  ERM_STATES,
  ERM_STATE_BLURB,
  ERM_STATE_LABEL,
  ERM_SUBJECT_KINDS,
  FIELD_ORDER_UNVERIFIED_NOTE,
  NOTHING_TRANSMITS_NOTE,
  PASTE_TEXT_NOTE,
  SUBJECT_KIND_LABEL,
  assignErmTask,
  cancelErmTask,
  confirmErmTask,
  copyText,
  ermKeys,
  fetchErmQueue,
  fetchErmTask,
  isConflict,
  isForbidden,
  isNotFound,
  queueErmTask,
  type CopyOutcome,
  type ErmDriftedField,
  type ErmFieldPack,
  type ErmSubjectKind,
  type ErmSyncState,
  type ErmTask,
  type ErmTaskDetail,
} from '../lib/erm'
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
  Loading,
  Modal,
  MonoValue,
  PageIntro,
  SearchInput,
  SectionTitle,
  Select,
  TableSkeleton,
  Td,
  Textarea,
  Th,
  Toolbar,
} from '../components/ui'

/* --------------------------------------------------------------------------
   ERM sync — CLAUDE.md §10, "manual by design", made operable.

     "ERM is external with no API. Do not build a scraper.
      Model as a sync task with a generated field pack: the system produces the
      exact field-value list in ERM's own field order, assigns it to a named
      person, they paste, they confirm."

   `app/api/erm.py`, `app/services/erm/` and `1900_erm_sync.sql` have existed
   with no caller. This screen is that caller, and four things about it are
   structural rather than cosmetic:

   1. COPYING IS THE FEATURE. The user of this screen is a named human with ERM
      open in another window, retyping a handful of values, many times a week.
      So every field carries its own copy button, the whole pack copies in one
      click as `label ⇥ value` lines, and the pack is ALSO always on screen as
      selectable text. That last part is not redundancy: `navigator.clipboard` is
      undefined on a non-secure origin, which includes the plain-HTTP dev host and
      the LAN address somebody opens this on from a second machine to do exactly
      this job. `copyText()` falls back to `execCommand` and then reports failure
      honestly — a Copy button that silently no-ops would break the only
      interaction this screen has.

   2. THE FIELD ORDER IS UNVERIFIED AND THIS SCREEN SAYS SO, TO THE ONE PERSON
      WHO CAN FIX IT. `fieldpack.py` is blunt: nobody on this side has seen ERM's
      form, so the order is a documented guess carried as
      `field_order_verified: false` on every response. It is repeated in the queue
      header and again beside the pack itself, because the person pasting has ERM
      on screen and we never will. Presenting the guess as a specification would
      make it unfalsifiable, which is strictly worse than an order that announces
      itself as provisional (§14: carry the open questions).

   3. DRIFT IS DRAWN LOUD. `stale` does not mean the push failed — it means the
      push happened, correctly, and the record has moved since, so ERM holds a
      photograph of a thing that changed. Stale cards get a red rail in the queue,
      a filter chip of their own, and a panel naming every field that moved with
      its before and after. A stale card with no field differences says so in
      words rather than rendering as an empty success. §10: "Without drift
      detection the two systems diverge within a month and neither is trusted."

   4. NOTHING HERE TRANSMITS. There is no push button because there is no push.
      Confirm records that a named human retyped these values into a portal this
      system cannot reach, freezes the pack they were shown, and stamps
      `erm_synced_at` / `erm_synced_by` (R3, §10). The panel says so above the
      button, because "confirm" reads as "send" to everybody who has not read the
      migration.

   The server decides everything else. `useAuth()` is used for layout and for the
   "assigned to me" chip only; the wall is 1900's three policies mirrored in
   `erm.py`, and what this file draws changes nothing about what is reachable.
-------------------------------------------------------------------------- */

const STATE_TONE: Record<ErmSyncState, string> = {
  queued: 'bg-surface-2 text-ink-2 border-line',
  assigned: 'bg-info-wash text-info-ink border-info/25',
  confirmed: 'bg-good-wash text-good-ink border-good/25',
  // The loud one, and it has earned it: this is the state §10 exists for.
  stale: 'bg-bad-wash text-bad-ink border-bad/40',
  cancelled: 'bg-surface-2 text-ink-3 border-line',
}

const PILL =
  'inline-flex items-center rounded-control border px-1.5 py-0.5 text-[11px] font-medium whitespace-nowrap'

function StatePill({ state }: { state: ErmSyncState }) {
  return <span className={`${PILL} ${STATE_TONE[state]}`}>{ERM_STATE_LABEL[state]}</span>
}

/** Timestamps are UTC in the database and IST on screen (CLAUDE.md §11). */
function fmtStamp(value: string | null | undefined): string {
  if (!value) return '—'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return '—'
  return `${d.toLocaleString(undefined, {
    day: '2-digit',
    month: 'short',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    timeZone: 'Asia/Kolkata',
  })} IST`
}

function isOpen(state: ErmSyncState): boolean {
  return ERM_OPEN_STATES.includes(state)
}

// --- Copying ------------------------------------------------------------------

/**
 * A copy button that reports what actually happened.
 *
 * The transient tick is local so a row can confirm itself without a re-render of
 * the panel; the FAILURE is lifted to the caller via `onOutcome`, because
 * "clipboard unavailable on this origin" is a sentence about the whole screen and
 * belongs in one live region rather than repeated eight times down a table.
 */
function CopyButton({
  text,
  label = 'Copy',
  title,
  disabled = false,
  onOutcome,
}: {
  text: string
  label?: string
  title?: string
  disabled?: boolean
  onOutcome?: (outcome: CopyOutcome) => void
}) {
  const [outcome, setOutcome] = useState<CopyOutcome | null>(null)

  useEffect(() => {
    if (outcome === null) return
    const timer = window.setTimeout(() => setOutcome(null), 2000)
    return () => window.clearTimeout(timer)
  }, [outcome])

  return (
    <Button
      size="sm"
      variant="secondary"
      disabled={disabled}
      title={title ?? 'Copy to clipboard'}
      onClick={() => {
        void copyText(text).then((result) => {
          setOutcome(result)
          onOutcome?.(result)
        })
      }}
    >
      {outcome === null
        ? label
        : outcome === 'unavailable'
          ? 'Copy failed'
          : `${label} ✓`}
    </Button>
  )
}

// --- The pack -----------------------------------------------------------------

/**
 * §10's "exact field-value list in ERM's own field order".
 *
 * Rendered in the order the server sent, never re-sorted: the order IS the
 * deliverable, which is why the API sends an array and this walks it. A blank
 * field renders as "leave blank" rather than as a dash — the pack's own value
 * stays empty so that copying it is harmless, and a human who pastes "—" into
 * ERM has written that character into a system of record.
 */
function PackTable({
  pack,
  onOutcome,
}: {
  pack: ErmFieldPack
  onOutcome: (outcome: CopyOutcome) => void
}) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead className="border-b border-line">
          <tr>
            <Th className="w-8">#</Th>
            <Th>Field in ERM</Th>
            <Th>Value to paste</Th>
            <Th className="w-24" />
          </tr>
        </thead>
        <tbody className="divide-y divide-line">
          {pack.entries.map((entry, index) => (
            <tr key={entry.source} className="hover:bg-surface-2/60">
              <Td className="text-xs tabular-nums text-ink-3">{index + 1}</Td>
              <Td>
                <p className="font-medium text-ink">{entry.label}</p>
                <p className="mt-0.5">
                  <MonoValue className="text-[11px] text-ink-3">{entry.source}</MonoValue>
                </p>
              </Td>
              <Td>
                {entry.is_blank ? (
                  <span className="text-xs text-ink-3 italic">
                    blank in byteXL — leave this field alone in ERM
                  </span>
                ) : (
                  /* `MonoValue` is select-all, which is the whole job here: the
                     reader is about to retype this into somebody else's portal,
                     and a half-selected value is worse than none. */
                  <MonoValue className="text-ink break-all">{entry.value}</MonoValue>
                )}
              </Td>
              <Td className="text-right">
                <CopyButton
                  text={entry.value}
                  disabled={entry.is_blank}
                  title={
                    entry.is_blank
                      ? 'Nothing to copy — this field is blank in byteXL, so it must be left alone in ERM.'
                      : `Copy the value of ${entry.label}`
                  }
                  onOutcome={onOutcome}
                />
              </Td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// --- Drift --------------------------------------------------------------------

/**
 * What moved since the confirmed sync, field by field.
 *
 * `was === null` is drawn differently from a blank: it means the pack GAINED
 * this field between one sync and the next, so ERM has never been told it at all.
 * Collapsing the two would report a never-sent field as unchanged.
 */
function DriftPanel({ drift }: { drift: ErmDriftedField[] }) {
  return (
    <div className="rounded-panel border border-bad/40 bg-bad-wash p-4">
      <div className="flex items-center gap-2 mb-2">
        <span aria-hidden className="text-bad-ink">
          ⚠
        </span>
        <h3 className="text-sm font-semibold text-bad-ink">
          <HelpTip term="byteXL and ERM have diverged">
            The card is <code>erm_stale</code>. Not a failure: the paste happened and it was
            right at the time. The local record has changed since, so ERM is holding a
            photograph of something that moved. Detecting that is the point of the whole round
            trip — without it the two systems drift apart within a month and neither one can be
            trusted.
          </HelpTip>
        </h3>
      </div>
      <p className="text-xs leading-relaxed text-bad-ink/90">{DRIFT_NOTE}</p>

      {drift.length === 0 ? (
        <p className="mt-3 rounded-card border border-bad/30 bg-surface px-3 py-2 text-xs leading-relaxed text-ink-2">
          {DRIFT_WITHOUT_DIFFERENCES_NOTE}
        </p>
      ) : (
        <div className="mt-3 overflow-x-auto rounded-card border border-bad/30 bg-surface">
          <table className="w-full text-sm">
            <thead className="border-b border-line">
              <tr>
                <Th>Field</Th>
                <Th>What ERM was told</Th>
                <Th>What byteXL says now</Th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {drift.map((field) => (
                <tr key={field.source}>
                  <Td>
                    <p className="font-medium text-ink">{field.label}</p>
                    <p className="mt-0.5">
                      <MonoValue className="text-[11px] text-ink-3">{field.source}</MonoValue>
                    </p>
                  </Td>
                  <Td>
                    {field.was === null ? (
                      <span className="text-xs italic text-ink-3">
                        never sent — the pack gained this field after that sync
                      </span>
                    ) : field.was === '' ? (
                      <span className="text-xs italic text-ink-3">blank</span>
                    ) : (
                      <MonoValue className="text-ink-2 line-through break-all">
                        {field.was}
                      </MonoValue>
                    )}
                  </Td>
                  <Td>
                    {field.now === '' ? (
                      <span className="text-xs italic text-ink-3">blank</span>
                    ) : (
                      <MonoValue className="font-medium text-ink break-all">{field.now}</MonoValue>
                    )}
                  </Td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

// --- The screen ---------------------------------------------------------------

type StateFilter = ErmSyncState | 'all'

export function ErmSyncPage() {
  const { profile } = useAuth()
  const queryClient = useQueryClient()

  const [stateFilter, setStateFilter] = useState<StateFilter>('all')
  const [kindFilter, setKindFilter] = useState<ErmSubjectKind | 'all'>('all')
  const [mine, setMine] = useState(false)
  const [search, setSearch] = useState('')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [fileOpen, setFileOpen] = useState(false)

  // The queue is fetched UNFILTERED by state so the chips can carry counts
  // without a request per chip; `assigned_to_me` and `subject_kind` go to the
  // server because they change which rows exist rather than how they are grouped.
  //
  // `limit` is PASSED explicitly rather than left to `fetchErmQueue`'s default.
  // The default is mirrored in `ermKeys.queue`, so omitting it worked — but it
  // worked by two constants agreeing in two files, which is the arrangement
  // that produced the original cache-key bug this file's key comment records.
  // Stating it once, here, makes the request and the key share one source.
  const filter = useMemo(
    () => ({
      subject_kind: kindFilter === 'all' ? null : kindFilter,
      assigned_to_me: mine,
      limit: PAGE.erm,
    }),
    [kindFilter, mine],
  )

  const queueQuery = useQuery({
    queryKey: ermKeys.queue(filter),
    queryFn: () => fetchErmQueue(filter),
  })

  const detailQuery = useQuery({
    queryKey: ermKeys.task(selectedId ?? ''),
    queryFn: () => fetchErmTask(selectedId as string),
    enabled: selectedId !== null,
  })

  const actorsQuery = useQuery({
    queryKey: ermKeys.actors(PAGE.profiles),
    queryFn: () => fetchActorNames(PAGE.profiles),
  })

  const actorName = useMemo(() => {
    const map = new Map<string, string>()
    for (const actor of actorsQuery.data?.rows ?? []) map.set(actor.id, actor.full_name ?? actor.id)
    return (id: string | null): string => (id === null ? '—' : (map.get(id) ?? id))
  }, [actorsQuery.data])

  const tasks = queueQuery.data?.tasks ?? []

  // The API takes a limit and cannot be asked for limit+1, so this reports "at
  // the cap" rather than "there are more" — see `boundedFromServer`.
  const tasksBound = boundedFromServer(tasks, PAGE.erm)

  const counts = useMemo(() => {
    const base = Object.fromEntries(ERM_STATES.map((s) => [s, 0])) as Record<ErmSyncState, number>
    for (const task of tasks) base[task.state] += 1
    return base
  }, [tasks])

  const visible = useMemo(
    () => (stateFilter === 'all' ? tasks : tasks.filter((t) => t.state === stateFilter)),
    [tasks, stateFilter],
  )

  // Display-only, over rows already fetched. It changes nothing about what the
  // server returned, but a queue narrowed in silence is how somebody concludes
  // a trainer has no card outstanding, so the count is printed beside the box
  // (DESIGN.md §4.4) and `total` is the chip-filtered set it narrowed FROM.
  const needle = search.trim().toLowerCase()
  const shown = useMemo(
    () =>
      needle === ''
        ? visible
        : visible.filter((task) =>
            [
              task.subject_label,
              SUBJECT_KIND_LABEL[task.subject_kind],
              ERM_STATE_LABEL[task.state],
              actorName(task.assigned_to),
            ]
              .join(' ')
              .toLowerCase()
              .includes(needle),
          ),
    [visible, needle, actorName],
  )

  const staleCount = counts.stale
  const openCount = counts.queued + counts.assigned

  const invalidate = (): void => {
    void queryClient.invalidateQueries({ queryKey: ermKeys.all })
  }

  const orderVerified = queueQuery.data?.field_order_verified ?? false
  const orderVersion = queueQuery.data?.field_order_version

  return (
    <>
      <PageHeader
        title="ERM sync"
        purpose="ERM is a separate system byteXL has to keep in step with, and it has no way of being written to except by a person typing into it. So this screen turns that into a job with a name on it: here are the exact values, in ERM's own field order, go and paste them, then come back and record that you did."
        subtitle={
          queueQuery.isPending
            ? 'Loading…'
            : `${openCount} card${openCount === 1 ? '' : 's'} to paste · ` +
              `${staleCount} diverged · ${tasks.length} in view`
        }
        actions={
          <>
            <Button
              size="sm"
              variant="ghost"
              disabled={queueQuery.isFetching}
              onClick={() => void queueQuery.refetch()}
            >
              Refresh
            </Button>
            <Button size="sm" variant="primary" onClick={() => setFileOpen(true)}>
              File a card
            </Button>
          </>
        }
      />

      <Page>
        {queueQuery.error && (
          <div className="mb-4">
            <ErrorNote>{errorMessage(queueQuery.error)}</ErrorNote>
          </div>
        )}

        <div className="space-y-4">
          <PageIntro
            steps={[
              'File a card for one record',
              'Give it to a named person',
              'They copy the pack into ERM',
              'They come back and record that they pasted it',
              'If the record changes later, the card goes stale and a new one is filed',
            ]}
          >
            <p>
              <HelpTip term="ERM">
                The external system byteXL's records also have to exist in. It belongs to
                somebody else, this side of the integration has no programmatic way into it, and
                there is deliberately no scraper — a robot driving a portal we do not control
                fails silently and writes rubbish into a system of record.
              </HelpTip>{' '}
              cannot be written to by a machine, so it is written to by a person. That round trip
              is the design, not a gap waiting to be closed: every integration without an API is
              modelled this way, as a named job rather than as a promise. Each card carries a{' '}
              <HelpTip term="field pack">
                The exact list of field-and-value pairs for one record, laid out in ERM's own
                field order and generated fresh from the database the moment you open the card.
                Copy it whole or field by field — nothing has to be remembered, retyped from a
                second screen, or worked out.
              </HelpTip>
              , and confirming it stamps the record with who carried it across and when.
            </p>
            <p className="mt-2">
              <strong className="font-medium text-ink">
                Drift is what the round trip is really for.
              </strong>{' '}
              When a record changes here after it was carried across, its card flips to{' '}
              <HelpTip term="stale">
                <code>erm_stale</code>. Not a failure — the paste happened and it was correct.
                It means ERM is now holding a photograph of something that has since moved, so
                the card is requeued and a fresh one is filed against today's values. Without
                that check the two systems quietly diverge within a month and neither one can be
                trusted, which is worse than never having copied anything across at all.
              </HelpTip>{' '}
              and a replacement card is filed automatically. Working the stale pile is how the
              two systems stay worth reading.
            </p>
          </PageIntro>

          {/* The disclaimer travels with the queue, not just with one card: the
              API repeats it at collection level precisely so that no client can
              render a list of packs without having been told the order is a
              guess. */}
          {!orderVerified && (
            <InfoNote>
              <strong className="text-ink">Field order v{orderVersion ?? '?'} — unverified.</strong>{' '}
              {FIELD_ORDER_UNVERIFIED_NOTE}
            </InfoNote>
          )}

          <InfoNote>{NOTHING_TRANSMITS_NOTE}</InfoNote>

          <Toolbar>
            <SearchInput
              value={search}
              onChange={setSearch}
              placeholder="Trainer, program, state or who it is with"
              count={shown.length}
              total={visible.length}
            />
          </Toolbar>

          <Toolbar>
            <FilterChip
              label="All states"
              count={tasks.length}
              active={stateFilter === 'all'}
              onClick={() => setStateFilter('all')}
            />
            {ERM_STATES.map((state) => (
              <FilterChip
                key={state}
                label={ERM_STATE_LABEL[state]}
                count={counts[state]}
                active={stateFilter === state}
                tone={state === 'stale' ? 'alert' : 'neutral'}
                onClick={() => setStateFilter(state)}
              />
            ))}
            <span className="w-px h-5 bg-line mx-1" aria-hidden />
            <FilterChip
              label="Trainers & programs"
              count={tasks.length}
              active={kindFilter === 'all'}
              onClick={() => setKindFilter('all')}
            />
            {ERM_SUBJECT_KINDS.map((kind) => (
              <FilterChip
                key={kind}
                label={`${SUBJECT_KIND_LABEL[kind]}s`}
                count={tasks.filter((t) => t.subject_kind === kind).length}
                active={kindFilter === kind}
                onClick={() => setKindFilter(kind)}
              />
            ))}
            <span className="w-px h-5 bg-line mx-1" aria-hidden />
            <FilterChip
              label="Assigned to me"
              count={tasks.filter((t) => t.assigned_to === profile?.id).length}
              active={mine}
              onClick={() => setMine(!mine)}
            />
          </Toolbar>

          {/* Every chip on this screen counts what arrived, and the header
              says how many cards are "to paste" and how many have diverged.
              A queue at the cap under-reports both, and a stale ERM record
              that nobody sees is exactly the drift §10 exists to catch. */}
          <BoundNote
            bound={tasksBound}
            noun="ERM cards"
            derived="The state and “assigned to me” counts cover only those."
            atServerCap
          />

          {queueQuery.isPending ? (
            /* Skeleton, not a spinner: this is a table, and a spinner collapses
               the layout and springs it back the moment the cards land. */
            <Card className="overflow-hidden">
              <TableSkeleton rows={6} cols={4} />
            </Card>
          ) : tasks.length === 0 ? (
            <Card>
              <EmptyState
                title="Nothing waiting for ERM"
                body="A card is filed when somebody pushes a record, or automatically by the drift triggers when a synced record changes. An empty queue means the two systems agree — as far as anything here can tell."
                hint="Nothing is being hidden by a filter — this is every card you can reach. If a record needs to exist in ERM, file a card for it and it becomes somebody's job."
                action={
                  <Button variant="primary" onClick={() => setFileOpen(true)}>
                    File a card
                  </Button>
                }
              />
            </Card>
          ) : (
            <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.45fr)] items-start">
              <Card className="overflow-hidden">
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead className="border-b border-line bg-surface-2/40">
                      <tr>
                        <Th>Record</Th>
                        <Th>State</Th>
                        <Th>Assigned</Th>
                        <Th>Filed</Th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-line">
                      {shown.map((task) => (
                        <tr
                          key={task.id}
                          onClick={() => setSelectedId(task.id)}
                          className={`cursor-pointer transition ${
                            task.state === 'stale'
                              ? 'border-l-4 border-l-bad bg-bad-wash hover:bg-bad-wash'
                              : 'hover:bg-surface-2/60'
                          } ${selectedId === task.id ? 'bg-accent-soft/60' : ''}`}
                        >
                          <Td>
                            <p className="font-medium text-ink truncate">{task.subject_label}</p>
                            <div className="mt-1 flex items-center gap-1.5">
                              <Badge>{SUBJECT_KIND_LABEL[task.subject_kind]}</Badge>
                              {task.verified && <Badge tone="accent">Verified in ERM</Badge>}
                            </div>
                          </Td>
                          <Td>
                            <StatePill state={task.state} />
                          </Td>
                          <Td className="text-xs text-ink-2">{actorName(task.assigned_to)}</Td>
                          <Td className="text-xs text-ink-3 whitespace-nowrap">
                            {fmtDate(task.created_at)}
                          </Td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>

                {/* Outside the table rather than as a lone <tr>: an empty state
                    that teaches needs more than one cell, and the reason the
                    list is empty differs — a chip is hiding rows, or the search
                    is (DESIGN.md §4.3). */}
                {shown.length === 0 && (
                  <EmptyState
                    title={
                      needle === ''
                        ? 'No cards in this state right now'
                        : 'No card matches that search'
                    }
                    body={
                      needle === ''
                        ? 'Cards move between states as they are worked: queued when filed, assigned when somebody takes them, confirmed once they have been pasted, stale if the record changes afterwards.'
                        : 'The box searches the record name, its type, its state and who the card is assigned to.'
                    }
                    hint={
                      needle === ''
                        ? `There ${tasks.length === 1 ? 'is' : 'are'} ${tasks.length} card${tasks.length === 1 ? '' : 's'} in view under other filters — pick “All states” to see them.`
                        : `${visible.length} card${visible.length === 1 ? '' : 's'} passed the filters above. Clear the search to see them.`
                    }
                  />
                )}
              </Card>

              <div className="lg:sticky lg:top-20">
                {selectedId === null ? (
                  <Card>
                    <EmptyState
                      title="Pick a card"
                      body="The pack is generated when you open it, so a card that has sat in the queue for a week still hands over today’s values — which is the whole failure §10 describes."
                      hint="Choose any row on the left and its field pack builds here, ready to copy. Opening a card sends nothing and commits you to nothing."
                    />
                  </Card>
                ) : detailQuery.isPending ? (
                  <Card>
                    <Loading label="Building the field pack" />
                  </Card>
                ) : detailQuery.error ? (
                  <ErrorNote>
                    {isNotFound(detailQuery.error)
                      ? 'That card no longer exists.'
                      : isForbidden(detailQuery.error)
                        ? 'You do not have access to that record. Reach comes from your college and cluster assignments, and the database refused — not this screen.'
                        : errorMessage(detailQuery.error)}
                  </ErrorNote>
                ) : detailQuery.data ? (
                  <TaskPanel
                    detail={detailQuery.data}
                    actorName={actorName}
                    actors={actorsQuery.data?.rows ?? []}
                    onChanged={invalidate}
                  />
                ) : null}
              </div>
            </div>
          )}
        </div>
      </Page>

      <FileCardModal
        open={fileOpen}
        onClose={() => setFileOpen(false)}
        onFiled={(task) => {
          invalidate()
          setSelectedId(task.id)
          setFileOpen(false)
        }}
      />
    </>
  )
}

// --- One card -----------------------------------------------------------------

function TaskPanel({
  detail,
  actorName,
  actors,
  onChanged,
}: {
  detail: ErmTaskDetail
  actorName: (id: string | null) => string
  actors: { id: string; full_name: string | null }[]
  onChanged: () => void
}) {
  const { task, pack, drift, pack_is_frozen } = detail
  const [copyNote, setCopyNote] = useState<string | null>(null)
  const [assignee, setAssignee] = useState('')
  const [externalId, setExternalId] = useState('')
  const [verified, setVerified] = useState(false)
  const [remarks, setRemarks] = useState('')
  const [reason, setReason] = useState('')
  const [confirming, setConfirming] = useState(false)

  // Every control resets when a different card is opened; without this the ERM
  // id typed for one trainer would still be sitting in the box for the next.
  useEffect(() => {
    setAssignee(task.assigned_to ?? '')
    setExternalId('')
    setVerified(false)
    setRemarks('')
    setReason('')
    setConfirming(false)
    setCopyNote(null)
  }, [task.id, task.assigned_to])

  const noteCopy = (outcome: CopyOutcome): void => {
    setCopyNote(outcome === 'unavailable' ? COPY_OUTCOME_LABEL.unavailable : null)
  }

  const assign = useMutation({
    mutationFn: (id: string) => assignErmTask(task.id, id),
    onSuccess: onChanged,
  })

  const confirm = useMutation({
    mutationFn: () =>
      confirmErmTask(task.id, { ermExternalId: externalId, verified, remarks }),
    onSuccess: () => {
      setConfirming(false)
      onChanged()
    },
  })

  const cancel = useMutation({
    mutationFn: () => cancelErmTask(task.id, reason),
    onSuccess: onChanged,
  })

  const failure = assign.error ?? confirm.error ?? cancel.error
  const open = isOpen(task.state)

  return (
    <div className="space-y-4">
      <Card className="p-5">
        <SectionTitle
          title={task.subject_label}
          subtitle={ERM_STATE_BLURB[task.state]}
          action={<StatePill state={task.state} />}
        />

        <dl className="grid gap-x-4 gap-y-2 sm:grid-cols-2 text-sm">
          <Detail label="Record">{SUBJECT_KIND_LABEL[task.subject_kind]}</Detail>
          <Detail label="Filed">{fmtStamp(task.created_at)}</Detail>
          <Detail label="Assigned to">
            {task.assigned_to === null ? (
              <span className="text-ink-3">nobody yet</span>
            ) : (
              `${actorName(task.assigned_to)} · ${fmtStamp(task.assigned_at)}`
            )}
          </Detail>
          <Detail
            label={
              <HelpTip term="Field order">
                The sequence the fields below are listed in, versioned so that every pack ever
                generated stays identifiable. It is a documented guess until somebody with ERM
                open confirms it, which is why it is stamped on the card rather than assumed.
              </HelpTip>
            }
          >
            v{pack.field_order_version} ·{' '}
            {pack.field_order_verified ? (
              'verified against ERM'
            ) : (
              <span className="text-warn-ink">unverified guess</span>
            )}
          </Detail>
          {task.confirmed_at !== null && (
            <>
              <Detail label="Pasted by">
                {actorName(task.confirmed_by)} · {fmtStamp(task.confirmed_at)}
              </Detail>
              <Detail label="ERM id read back">
                {task.erm_external_id ? (
                  <MonoValue>{task.erm_external_id}</MonoValue>
                ) : (
                  <span className="text-ink-3">none recorded</span>
                )}
              </Detail>
              <Detail
                label={
                  <HelpTip term="Read back and checked">
                    Whether the person who pasted then re-read the values off ERM's own screen
                    and confirmed they landed correctly. Pasting and checking are recorded
                    separately because only one of them is a claim that ERM is now right.
                  </HelpTip>
                }
              >
                {task.verified ? 'Yes' : 'No — pasted but not verified in ERM'}
              </Detail>
              {task.remarks && <Detail label="Remarks">{task.remarks}</Detail>}
            </>
          )}
          {task.cancelled_at !== null && (
            <Detail label="Cancelled">
              {fmtStamp(task.cancelled_at)} — {task.cancelled_reason ?? 'no reason recorded'}
            </Detail>
          )}
          {task.stale_at !== null && (
            <Detail label="Marked stale">
              {fmtStamp(task.stale_at)}
              {task.stale_reason ? ` — ${task.stale_reason}` : ''}
            </Detail>
          )}
        </dl>
      </Card>

      {(task.state === 'stale' || drift.length > 0) && <DriftPanel drift={drift} />}

      <Card className="p-5">
        <SectionTitle
          title="Field pack"
          subtitle={
            pack_is_frozen
              ? 'Frozen at confirm — this is the evidence of what was on screen when somebody pasted, not a fresh read of the record.'
              : 'Generated live from the database just now. Copy each field, or the whole block, into ERM.'
          }
          action={
            <CopyButton
              text={pack.paste_text}
              label="Copy all"
              title="Copy every field as label ⇥ value lines"
              onOutcome={noteCopy}
            />
          }
        />

        {!pack.field_order_verified && (
          <div className="mb-3">
            <InfoNote>
              <strong className="text-ink">Match by label, not by position.</strong>{' '}
              {FIELD_ORDER_UNVERIFIED_NOTE}
            </InfoNote>
          </div>
        )}

        <PackTable pack={pack} onOutcome={noteCopy} />

        <div className="mt-4 space-y-2">
          <p className="text-xs text-ink-3">{PASTE_TEXT_NOTE}</p>
          {/* Always on screen and always selectable. This is the fallback that
              makes the screen usable when the clipboard API is unavailable —
              the human selects it and presses Ctrl-C, which no origin can
              take away. */}
          <Textarea
            readOnly
            rows={Math.min(12, pack.entries.length + 1)}
            value={pack.paste_text}
            aria-label="The whole field pack as tab-separated label and value lines"
            className="font-mono text-xs"
            onFocus={(event) => event.currentTarget.select()}
          />
          <p className="text-xs text-ink-3" role="status" aria-live="polite">
            {copyNote ?? ' '}
          </p>
        </div>
      </Card>

      {failure && (
        <ErrorNote>
          {isConflict(failure)
            ? `${errorMessage(failure)} — the card has moved since this screen loaded. Refresh and look at it again.`
            : errorMessage(failure)}
        </ErrorNote>
      )}

      {open ? (
        <Card className="p-5">
          <SectionTitle
            title="Hand it to somebody, then record the paste"
            subtitle="§10: the system generates the pack, assigns it to a named person, they paste, they confirm."
          />

          <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_auto] items-end">
            <Field
              label="Assign to"
              hint="Reassignment is fine — people go on leave and work moves teams."
            >
              <Select value={assignee} onChange={(e) => setAssignee(e.target.value)}>
                <option value="">Select a person…</option>
                {actors.map((actor) => (
                  <option key={actor.id} value={actor.id}>
                    {actor.full_name ?? actor.id}
                  </option>
                ))}
              </Select>
            </Field>
            <Button
              variant="secondary"
              disabled={assignee === '' || assignee === task.assigned_to || assign.isPending}
              onClick={() => assign.mutate(assignee)}
            >
              {assign.isPending ? 'Assigning…' : 'Assign'}
            </Button>
          </div>

          <div className="mt-5 border-t border-line pt-4">
            <InfoNote>{NOTHING_TRANSMITS_NOTE}</InfoNote>

            <div className="mt-3 grid gap-3 sm:grid-cols-2">
              <Field
                label="ERM record id (optional)"
                hint="The id read back off ERM's own screen, if it shows one."
              >
                <Input
                  value={externalId}
                  onChange={(e) => setExternalId(e.target.value)}
                  placeholder="e.g. ERM-TR-10482"
                />
              </Field>
              <Field
                label="Remarks (optional)"
                hint="Anything the next person needs — a field ERM would not accept, a label that did not match."
              >
                <Input value={remarks} onChange={(e) => setRemarks(e.target.value)} />
              </Field>
            </div>

            <label className="mt-3 flex items-start gap-2 text-sm text-ink">
              <input
                type="checkbox"
                className="mt-0.5"
                checked={verified}
                onChange={(e) => setVerified(e.target.checked)}
              />
              <span>
                I read the values back off ERM after pasting and they match.
                <span className="block text-xs text-ink-3">
                  Leave this unticked if you pasted without re-checking. Both are recorded; only
                  one of them is a claim that ERM is correct.
                </span>
              </span>
            </label>

            {confirming ? (
              <div className="mt-3 rounded-card border border-line bg-surface-2 p-3">
                <p className="text-sm text-ink">
                  Record that <strong>you</strong> pasted these {pack.entries.length} values into
                  ERM?
                </p>
                <p className="text-xs text-ink-3 mt-1">
                  This freezes the pack above onto the card as evidence and stamps{' '}
                  <HelpTip term="erm_synced_at / erm_synced_by">
                    Two columns on the record itself: when it was last carried across to ERM, and
                    which named person carried it. They are what a later change is compared
                    against — an edit after that timestamp is what flips the record to stale.
                  </HelpTip>{' '}
                  on the record. It sends nothing.
                </p>
                <div className="mt-3 flex gap-2">
                  <Button
                    variant="primary"
                    disabled={confirm.isPending}
                    onClick={() => confirm.mutate()}
                  >
                    {confirm.isPending ? 'Recording…' : 'Yes — I pasted it'}
                  </Button>
                  <Button variant="ghost" onClick={() => setConfirming(false)}>
                    Not yet
                  </Button>
                </div>
              </div>
            ) : (
              <Button className="mt-3" variant="primary" onClick={() => setConfirming(true)}>
                I pasted this into ERM
              </Button>
            )}
          </div>

          <div className="mt-5 border-t border-line pt-4">
            <Field
              label="Or withdraw this card"
              hint="A cancelled push is a decision that this record deliberately does not match ERM. The reason is what stops it reading as an oversight later."
            >
              <Input
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                placeholder="Why is this card not going to be pasted?"
              />
            </Field>
            <Button
              className="mt-2"
              variant="danger"
              disabled={reason.trim() === '' || cancel.isPending}
              onClick={() => cancel.mutate()}
            >
              {cancel.isPending ? 'Cancelling…' : 'Cancel this card'}
            </Button>
          </div>
        </Card>
      ) : (
        <InfoNote>
          {task.state === 'stale'
            ? 'This card is closed and stays as the record of what was pasted. The drift triggers have already filed a replacement — work that one.'
            : task.state === 'confirmed'
              ? 'Confirmed. A confirmed push cannot be undone from here, because it happened: somebody typed those values into a system this one cannot reach. Undoing it means going back to ERM, which is a new card.'
              : 'Cancelled. File a fresh card if this record needs to reach ERM after all.'}
        </InfoNote>
      )}
    </div>
  )
}

function Detail({ label, children }: { label: ReactNode; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-ink-3">{label}</dt>
      <dd className="text-sm text-ink break-words">{children}</dd>
    </div>
  )
}

// --- Filing a card ------------------------------------------------------------

interface SubjectOption {
  id: string
  label: string
  hint: string
}

/**
 * File a card for one record.
 *
 * The subject lists come from PostgREST directly, so RLS decides what is
 * offered — a Manager sees their colleges' programs and an LDE Executive sees
 * theirs, from the identical statement. The 409 is drawn as an explanation
 * rather than an error: one open card per record is the design, not a
 * collision, because a record edited five times in an afternoon must produce
 * one job and not five.
 */
function FileCardModal({
  open,
  onClose,
  onFiled,
}: {
  open: boolean
  onClose: () => void
  onFiled: (task: ErmTask) => void
}) {
  const [kind, setKind] = useState<ErmSubjectKind>('trainer')
  const [subjectId, setSubjectId] = useState('')

  const subjectsQuery = useQuery({
    queryKey: ermKeys.subjects(kind),
    enabled: open,
    queryFn: async (): Promise<Bounded<SubjectOption>> => {
      if (kind === 'trainer') {
        const result = await bounded<{ id: string; full_name: string; pan: string }>(
          PAGE.trainers,
          (rows) =>
            supabase
              .from('trainers')
              .select('id, full_name, pan')
              .order('full_name')
              .limit(rows),
        )
        // PAN, because §6 makes it the identity and two trainers can share a name.
        return {
          ...result,
          rows: result.rows.map((row) => ({
            id: row.id,
            label: row.full_name,
            hint: row.pan,
          })),
        }
      }
      // PostgREST returns a to-one embed as an object, and supabase-js — with no
      // generated database types in this project — infers it as an array. Both
      // shapes are accepted rather than cast away, because a cast here would go
      // wrong silently the day the types are generated.
      type Embedded = { name: string } | { name: string }[] | null
      const result = await bounded<{ id: string; name: string; colleges: Embedded }>(
        PAGE.programs,
        (rows) =>
          supabase
            .from('programs')
            .select('id, name, colleges(name)')
            .order('name')
            .limit(rows),
      )
      const collegeName = (embedded: Embedded): string =>
        embedded === null ? '' : Array.isArray(embedded) ? (embedded[0]?.name ?? '') : embedded.name
      return {
        ...result,
        rows: result.rows.map((row) => ({
          id: row.id,
          label: row.name,
          hint: collegeName(row.colleges),
        })),
      }
    },
  })

  const file = useMutation({
    mutationFn: () => queueErmTask({ subject_kind: kind, subject_id: subjectId }),
    onSuccess: onFiled,
  })

  useEffect(() => {
    setSubjectId('')
    file.reset()
    // `file` is a stable mutation object from react-query; resetting on a kind
    // change keeps a 409 about a trainer from hanging over a program.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kind, open])

  return (
    <Modal open={open} onClose={onClose} title="File an ERM sync card" width="max-w-xl">
      <div className="space-y-4">
        <InfoNote>
          A{' '}
          <HelpTip term="card">
            One unit of work for one record: here are its fields, in ERM's order, go and paste
            them, come back and say you did. There is only ever one open card per record — a
            trainer edited five times in an afternoon produces one job, not five.
          </HelpTip>{' '}
          is a job for a human. Filing one sends nothing and changes nothing in ERM.
        </InfoNote>

        <Field label="Record type">
          <Select value={kind} onChange={(e) => setKind(e.target.value as ErmSubjectKind)}>
            {ERM_SUBJECT_KINDS.map((option) => (
              <option key={option} value={option}>
                {SUBJECT_KIND_LABEL[option]}
              </option>
            ))}
          </Select>
        </Field>

        <Field
          label={SUBJECT_KIND_LABEL[kind]}
          hint={
            subjectsQuery.isPending
              ? 'Loading…'
              : 'Only records you can reach are listed — the database decides that, not this form.'
          }
        >
          <Select
            value={subjectId}
            disabled={subjectsQuery.isPending}
            onChange={(e) => setSubjectId(e.target.value)}
          >
            <option value="">Select…</option>
            {(subjectsQuery.data?.rows ?? []).map((option) => (
              <option key={option.id} value={option.id}>
                {option.label}
                {option.hint ? ` — ${option.hint}` : ''}
              </option>
            ))}
          </Select>
          <div className="mt-2">
            <BoundNote
              bound={subjectsQuery.data}
              noun={kind === 'trainer' ? 'trainers' : 'programs'}
              derived="A record beyond that cannot have a card filed from this form."
            />
          </div>
        </Field>

        {subjectsQuery.error && <ErrorNote>{errorMessage(subjectsQuery.error)}</ErrorNote>}

        {file.error &&
          (isConflict(file.error) ? (
            <InfoNote>
              <strong className="text-ink">There is already an open card for this record.</strong>{' '}
              {errorMessage(file.error)}
            </InfoNote>
          ) : (
            <ErrorNote>{errorMessage(file.error)}</ErrorNote>
          ))}

        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>
            Close
          </Button>
          <Button
            variant="primary"
            disabled={subjectId === '' || file.isPending}
            onClick={() => file.mutate()}
          >
            {file.isPending ? 'Filing…' : 'File the card'}
          </Button>
        </div>
      </div>
    </Modal>
  )
}
