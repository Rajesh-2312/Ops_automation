import { useMemo, useState } from 'react'
import { PAGE, bounded, emptyBound, usePageLimit } from '../lib/bounds'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { supabase, errorMessage, unwrap } from '../lib/supabase'
import { qk } from '../lib/queryKeys'
import {
  ATTENDANCE_MARKS,
  ATTENDANCE_MARK_LABEL,
  ATTENDANCE_MARK_SHORT,
  ATTENDANCE_PAY_HINT,
  type AttendanceMark,
  type Deployment,
  type ProgramType,
  type TrainerAttendance,
} from '../lib/types'
import { useAuth } from '../auth/AuthProvider'
import { Page, PageHeader } from '../components/AppShell'
import {
  attendanceCellClass,
  Badge,
  BoundNote,
  Button,
  Card,
  EmptyState,
  ErrorNote,
  fmtDate,
  HelpTip,
  InfoNote,
  Legend,
  Loading,
  MonoValue,
  PageIntro,
  Select,
  Stat,
} from '../components/ui'

/**
 * The key's swatches, written out rather than reused from
 * `attendanceCellClass`. That helper carries the day cell's own `h-9` sizing,
 * and Tailwind resolves a conflicting `h-3`/`h-9` pair by stylesheet order
 * rather than by string order — so borrowing it would give the key
 * full-height chips at random. Same tokens, same meanings, chip-sized.
 *
 * UNMARKED is drawn as a HOLE — no wash, dashed border — for the same reason it
 * is in the grid itself: a filled chip reads as a recorded decision, and the
 * whole hazard here is that nobody recorded one.
 */
const LEGEND_SWATCH: Record<AttendanceMark, string> = {
  P: 'bg-good-wash border-good/30',
  A: 'bg-bad-wash border-bad/30',
  H: 'bg-warn-wash border-warn/30',
  HOLIDAY: 'bg-info-wash border-info/30',
  UNMARKED: 'bg-transparent border-line border-dashed',
}

/* --------------------------------------------------------------------------
   Dates

   Built by hand rather than through Date.toISOString(), which converts to UTC
   and would slide every mark in IST back a day for anything before 05:30.
   `mark_date` is a Postgres DATE and must be the local calendar day the human
   is looking at.
-------------------------------------------------------------------------- */

const pad = (n: number) => String(n).padStart(2, '0')
const ymd = (year: number, month0: number, day: number) =>
  `${year}-${pad(month0 + 1)}-${pad(day)}`

function currentMonthKey(): string {
  const now = new Date()
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}`
}

function parseMonthKey(key: string): { year: number; month0: number } {
  const [y, m] = key.split('-')
  return { year: Number(y), month0: Number(m) - 1 }
}

/** Day 0 of the next month is the last day of this one. */
const daysInMonth = (year: number, month0: number) => new Date(year, month0 + 1, 0).getDate()

function todayKey(): string {
  const now = new Date()
  return ymd(now.getFullYear(), now.getMonth(), now.getDate())
}

function monthLabel(key: string): string {
  const { year, month0 } = parseMonthKey(key)
  return new Date(year, month0, 1).toLocaleDateString(undefined, {
    month: 'long',
    year: 'numeric',
  })
}

function shiftMonth(key: string, delta: number): string {
  const { year, month0 } = parseMonthKey(key)
  const d = new Date(year, month0 + delta, 1)
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}`
}

/** The last twelve months plus the next, newest first. Enough to correct a
 *  closed period without offering an unbounded date picker. */
function monthOptions(): string[] {
  const base = currentMonthKey()
  return Array.from({ length: 14 }, (_, i) => shiftMonth(base, 1 - i))
}

// --- Data shapes -------------------------------------------------------------

type DeploymentRow = Deployment & {
  trainers: { id: string; full_name: string; pan: string } | null
  batches:
    | {
        id: string
        name: string
        programs: {
          id: string
          name: string
          type: ProgramType
          colleges: { name: string } | null
        } | null
      }
    | null
}

interface DayCell {
  day: number
  date: string
  mark: AttendanceMark
  note: string | null
  /** Inside the deployment's start/end window — outside it, nothing is markable. */
  inWindow: boolean
  isFuture: boolean
  isWeekend: boolean
}

/**
 * Trainer attendance marking.
 *
 * THIS SCREEN IS A PAYOUT INPUT. Every mark here is read by
 * services/remuneration/engine.py to derive payable days, so the grid is built
 * around making an ABSENCE OF DATA visible rather than around making marking
 * fast. CLAUDE.md §5:
 *
 *   CRT  — pays per day, counts payable days UP from P marks. An unmarked day
 *          silently UNDERPAYS the trainer. Completeness is a HARD BLOCK.
 *   bCAP — pays per month, counts DOWN from the period length. An unmarked day
 *          silently PAYS. Completeness is a WARNING.
 *
 * Same missing row, opposite consequence, and neither one raises an error
 * anywhere — which is exactly why the unmarked count is a headline figure and
 * not a footnote. The panel below states which of the two rules applies to the
 * selected deployment, taken from its program type.
 *
 * NO ARITHMETIC HAPPENS HERE. The counts are counts of days; nothing on this
 * page multiplies a rate or predicts a payout (R2 — an agent, or a screen, may
 * explain a number but must never produce one).
 *
 * One row per deployment per day, unique on (deployment_id, mark_date), never a
 * wide D1–D31 layout (§6) — payout disputes are always about specific days, and
 * a wide row cannot say who changed the 14th.
 */
export function AttendancePage() {
  const { profile } = useAuth()
  const queryClient = useQueryClient()
  const [deploymentId, setDeploymentId] = useState('')
  const [month, setMonth] = useState(currentMonthKey)
  const [brush, setBrush] = useState<AttendanceMark>('P')

  // Unfiltered on purpose: deployments_internal_all already narrows this to
  // batches the caller reaches, so a Manager gets their colleges and an LDE
  // Executive gets their campus from the identical statement.
  // BOUNDED, and keyed at the DEFAULT page size on purpose: Deployments,
  // Payouts and this screen all read the same statement under
  // `qk.deployments.list()`, so sharing one bound means opening any of them
  // warms the other two rather than fetching the same rows twice under two
  // page sizes.
  const page = usePageLimit(PAGE.deployments)

  const deploymentsQuery = useQuery({
    queryKey: qk.deployments.list(page.limit),
    queryFn: () =>
      bounded<DeploymentRow>(page.limit, (rows) =>
        supabase
          .from('deployments')
          .select(
            '*, trainers(id, full_name, pan), batches(id, name, programs(id, name, type, colleges(name)))',
          )
          .order('start_date', { ascending: false })
          .limit(rows),
      ),
  })

  const deploymentsBound =
    deploymentsQuery.data ?? emptyBound<DeploymentRow>(page.limit)
  const deployments = useMemo(() => deploymentsBound.rows, [deploymentsBound.rows])

  // Nothing is auto-selected. Attendance is written per trainer-day, and
  // pre-selecting somebody's row means the first click lands on whoever
  // happened to sort first.
  const selected = deployments.find((d) => d.id === deploymentId) ?? null
  const programType = selected?.batches?.programs?.type ?? null

  const { year, month0 } = parseMonthKey(month)
  const monthStart = ymd(year, month0, 1)
  const monthEnd = ymd(year, month0, daysInMonth(year, month0))

  // DELIBERATELY UNBOUNDED, and it is the read most people would bound first.
  //
  // `trainer_attendance` is the fastest-growing table in the schema, but THIS
  // query is not the reason: it asks for one deployment between the first and
  // last of one named month, and `trainer_attendance_unique_day` makes
  // (deployment_id, mark_date) unique — so it can return at most 31 rows, ever.
  // The date window IS the bound, and it is a stronger one than a page size
  // because it is the exact set the grid draws.
  //
  // Adding `.limit(31)` here would be worse than useless: the grid renders a
  // cell per calendar day and reads its mark from this map, so a limit that
  // ever bit would silently draw a marked day as UNMARKED — which on CRT is an
  // unpaid day and on bCAP is a paid one (§5). A bound whose failure mode is a
  // wrong attendance mark does not belong on the query that decides pay.
  const marksQuery = useQuery({
    queryKey: qk.trainerAttendance.byDeploymentMonth(deploymentId, month),
    enabled: !!deploymentId,
    queryFn: () =>
      unwrap<TrainerAttendance[]>(
        supabase
          .from('trainer_attendance')
          .select('*')
          .eq('deployment_id', deploymentId)
          .gte('mark_date', monthStart)
          .lte('mark_date', monthEnd)
          .order('mark_date'),
      ),
  })

  const cells = useMemo<DayCell[]>(() => {
    if (!selected) return []
    const rows = new Map((marksQuery.data ?? []).map((r) => [r.mark_date, r]))
    const today = todayKey()
    const total = daysInMonth(year, month0)

    return Array.from({ length: total }, (_, i) => {
      const day = i + 1
      const date = ymd(year, month0, day)
      const weekday = new Date(year, month0, day).getDay()
      const row = rows.get(date)
      return {
        day,
        date,
        mark: row?.mark ?? 'UNMARKED',
        note: row?.note ?? null,
        inWindow:
          (!selected.start_date || date >= selected.start_date) &&
          (!selected.end_date || date <= selected.end_date),
        isFuture: date > today,
        isWeekend: weekday === 0 || weekday === 6,
      }
    })
  }, [selected, marksQuery.data, year, month0])

  /**
   * Days that SHOULD carry a mark: inside the deployment window and already
   * elapsed. A future day is not missing data, and counting it as such would
   * make every open month look broken.
   */
  const expected = cells.filter((c) => c.inWindow && !c.isFuture)
  const unmarked = expected.filter((c) => c.mark === 'UNMARKED')
  const unmarkedWeekdays = unmarked.filter((c) => !c.isWeekend)
  const marked = expected.length - unmarked.length

  const setMark = useMutation({
    mutationFn: ({ date, mark }: { date: string; mark: AttendanceMark }) =>
      // UNMARKED deletes the row rather than storing the value. A missing row
      // and an UNMARKED row are equivalent to the payout engine (see the column
      // comment in migration 0600), and deleting keeps "what was actually
      // recorded" unambiguous when someone reads the table directly.
      mark === 'UNMARKED'
        ? unwrap(
            supabase
              .from('trainer_attendance')
              .delete()
              .eq('deployment_id', deploymentId)
              .eq('mark_date', date),
          )
        : unwrap(
            supabase.from('trainer_attendance').upsert(
              {
                deployment_id: deploymentId,
                mark_date: date,
                mark,
                marked_by: profile?.id ?? null,
              },
              { onConflict: 'deployment_id,mark_date' },
            ),
          ),
    onSettled: () =>
      queryClient.invalidateQueries({
        queryKey: qk.trainerAttendance.byDeploymentMonth(deploymentId, month),
      }),
  })

  /** Fills only days that carry NO row at all. It never overwrites a mark
   *  somebody made — an "apply to all" that could silently replace an A is a
   *  payout defect waiting to happen. */
  const fillWeekdays = useMutation({
    mutationFn: () => {
      const targets = unmarkedWeekdays.map((c) => ({
        deployment_id: deploymentId,
        mark_date: c.date,
        mark: 'P' as const,
        marked_by: profile?.id ?? null,
      }))
      if (targets.length === 0) return Promise.resolve([])
      return unwrap(
        supabase
          .from('trainer_attendance')
          .upsert(targets, { onConflict: 'deployment_id,mark_date' }),
      )
    },
    onSettled: () =>
      queryClient.invalidateQueries({
        queryKey: qk.trainerAttendance.byDeploymentMonth(deploymentId, month),
      }),
  })

  const failure = deploymentsQuery.error ?? marksQuery.error ?? setMark.error ?? fillWeekdays.error

  // Monday-first, matching how a training week is actually read here.
  const firstWeekday = (new Date(year, month0, 1).getDay() + 6) % 7

  return (
    <>
      <PageHeader
        title="Attendance"
        purpose="Mark every day a trainer worked. These marks are the only record of how many days to pay for, and a day nobody marks is not a blank — it is a decision that costs someone money."
        subtitle={
          selected
            ? `${selected.trainers?.full_name ?? 'Unknown trainer'} · ${
                selected.batches?.name ?? '—'
              } · ${monthLabel(month)}`
            : `${monthLabel(month)} · one trainer and one batch at a time`
        }
        actions={
          <>
            <Button size="sm" variant="ghost" onClick={() => setMonth(shiftMonth(month, -1))}>
              ←
            </Button>
            <Select
              className="w-44"
              value={month}
              onChange={(e) => setMonth(e.target.value)}
              aria-label="Month"
            >
              {monthOptions().map((m) => (
                <option key={m} value={m}>
                  {monthLabel(m)}
                </option>
              ))}
            </Select>
            <Button size="sm" variant="ghost" onClick={() => setMonth(shiftMonth(month, 1))}>
              →
            </Button>
          </>
        }
      />

      <Page>
        {failure && (
          <div className="mb-4">
            <ErrorNote>{errorMessage(failure)}</ErrorNote>
          </div>
        )}

        <div className="mb-4 max-w-4xl">
          <PageIntro
            steps={[
              'Pick the trainer and batch',
              'Pick the month',
              'Click each day to set its mark',
              'Close every gap before the month is paid',
            ]}
          >
            Attendance is recorded one day at a time against a single{' '}
            <HelpTip term="deployment">
              One named trainer teaching one named batch, between a start and an end date.
              Attendance, and the pay that follows from it, always belong to a deployment —
              never to a trainer on their own.
            </HelpTip>
            . Nothing is filled in for you, and what each mark is worth depends on the program
            type — so read the key above the calendar before you start marking.
          </PageIntro>
        </div>

        {deploymentsQuery.isPending ? (
          <Loading label="Loading deployments" />
        ) : deployments.length === 0 ? (
          <Card>
            <EmptyState
              title="No deployments to mark"
              body="Attendance is recorded against a deployment — a trainer teaching a batch. Create one from the program before marking days."
              hint="Nothing you can reach has a trainer assigned to a batch yet. A deployment created on the Deployments screen appears in the picker here immediately."
            />
          </Card>
        ) : (
          <div className="max-w-4xl space-y-4">
            <Card className="p-4">
              <label className="block">
                <span className="block text-xs font-medium text-ink-2 mb-1.5">Deployment</span>
                <Select
                  value={deploymentId}
                  onChange={(e) => setDeploymentId(e.target.value)}
                  aria-label="Deployment"
                >
                  <option value="">Select a trainer and batch…</option>
                  {deployments.map((d) => (
                    <option key={d.id} value={d.id}>
                      {d.trainers?.full_name ?? 'Unknown trainer'} · {d.batches?.name ?? '—'} ·{' '}
                      {d.batches?.programs?.name ?? '—'} (
                      {d.batches?.programs?.type ?? 'unknown type'})
                    </option>
                  ))}
                </Select>
              </label>

              {/* The picker, not a table — so the failure is "the trainer I
                  need is not in the dropdown", which reads as "no deployment
                  exists" unless the screen says otherwise. Marking against the
                  wrong deployment is not recoverable by the person who did it. */}
              <div className="mt-2">
                <BoundNote
                  bound={deploymentsBound}
                  noun="deployments"
                  derived="A deployment beyond that cannot be selected, so its days cannot be marked."
                  onMore={page.more}
                  step={page.step}
                />
              </div>

              {selected && (
                <div className="flex flex-wrap items-center gap-2 mt-3 text-xs text-ink-2">
                  <Badge tone="accent">{programType}</Badge>
                  {/* PAN, not the name. Trainer identity IS the PAN (§6) — two
                      spellings of one person are two rows, one PAN is one
                      payee. */}
                  <MonoValue className="text-ink-3" title="PAN — the trainer's identity">
                    {selected.trainers?.pan}
                  </MonoValue>
                  <span>{selected.batches?.programs?.colleges?.name ?? '—'}</span>
                  <span className="text-ink-3">
                    Deployed {fmtDate(selected.start_date)} → {fmtDate(selected.end_date)}
                  </span>
                </div>
              )}
            </Card>

            {!selected ? (
              <Card>
                <EmptyState
                  title="Pick a deployment"
                  body="Marks belong to one trainer on one batch. Nothing is selected by default so the first click cannot land on the wrong person."
                  hint="The calendar appears as soon as you choose one from the list above. Marking against the wrong deployment is not something the person who did it can undo."
                />
              </Card>
            ) : marksQuery.isPending ? (
              <Loading label="Loading marks" />
            ) : (
              <>
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
                  <Stat
                    label="Days to account for"
                    hint="Inside the deployment dates and already past"
                    value={expected.length}
                  />
                  <Stat
                    label="Marked"
                    hint="Carrying P, A, H or a holiday"
                    value={marked}
                  />
                  <Stat
                    label="Unmarked"
                    hint="No mark at all — the hazard, see the key"
                    value={unmarked.length}
                    tone={unmarked.length > 0 ? 'warn' : 'normal'}
                  />
                  <Stat
                    label="Unmarked weekdays"
                    hint="Mon–Fri gaps, the usual teaching days"
                    value={unmarkedWeekdays.length}
                    tone={unmarkedWeekdays.length > 0 ? 'warn' : 'normal'}
                  />
                </div>

                {/* The §5 asymmetry, stated for the program actually selected.
                    Deliberately worded as a consequence, not a rule number —
                    whoever is marking needs to know what a gap DOES. */}
                {unmarked.length > 0 &&
                  (programType === 'CRT' ? (
                    <ErrorNote>
                      <strong>
                        {unmarked.length} day{unmarked.length === 1 ? '' : 's'} unmarked on a CRT
                        deployment.
                      </strong>{' '}
                      CRT pays per day and counts payable days UP from P marks, so an unmarked
                      day pays nothing — this trainer would be underpaid for every one of them.
                      Attendance completeness is a hard block: the payout cycle cannot reach
                      approval until this reaches zero.
                    </ErrorNote>
                  ) : (
                    <InfoNote>
                      <strong>
                        {unmarked.length} day{unmarked.length === 1 ? '' : 's'} unmarked on a bCAP
                        deployment.
                      </strong>{' '}
                      bCAP pays a monthly retainer and counts DOWN from the period length, so an
                      unmarked day is <em>paid</em>. Nothing will block the payout — it is a
                      warning, and an absence left unmarked here is money out of the door. Mark
                      the days you know about.
                    </InfoNote>
                  ))}

                {/* --- Brush ------------------------------------------------- */}
                <Card className="p-4">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-xs font-medium text-ink-2">Click a day to mark it</span>
                    <div className="flex flex-wrap gap-1.5">
                      {ATTENDANCE_MARKS.map((m) => (
                        <button
                          key={m}
                          type="button"
                          onClick={() => setBrush(m)}
                          className={`inline-flex items-center gap-1.5 rounded-lg border px-2.5 h-8
                            text-xs font-medium transition ${
                              brush === m
                                ? 'border-accent bg-accent-soft text-accent-ink'
                                : 'border-line bg-surface text-ink-2 hover:text-ink'
                            }`}
                          title={
                            programType
                              ? `${ATTENDANCE_MARK_LABEL[m]} — ${ATTENDANCE_PAY_HINT[programType][m]}`
                              : ATTENDANCE_MARK_LABEL[m]
                          }
                        >
                          <span className="font-mono">{ATTENDANCE_MARK_SHORT[m]}</span>
                          {ATTENDANCE_MARK_LABEL[m]}
                        </button>
                      ))}
                    </div>
                  </div>

                  {programType && (
                    <p className="text-xs text-ink-3 mt-2.5 leading-relaxed">
                      On this {programType} deployment, <strong>{ATTENDANCE_MARK_LABEL[brush]}</strong>{' '}
                      {ATTENDANCE_PAY_HINT[programType][brush]}. Choosing “Unmarked” removes the
                      row — a missing row and an UNMARKED row mean the same thing to the payout
                      engine.
                    </p>
                  )}
                </Card>

                {/* --- Grid -------------------------------------------------- */}
                <Card className="p-4">
                  {/* THE KEY, AND IT IS NOT DECORATION.
                      Five marks are the payout engine's entire input, and two
                      of them mean OPPOSITE things on the two program types
                      (§5). The per-mark hints are read straight out of
                      ATTENDANCE_PAY_HINT rather than retyped here, so the key
                      cannot drift from what the engine is actually told. */}
                  <div className="mb-3 pb-3 border-b border-line-soft space-y-2.5">
                    <Legend
                      items={ATTENDANCE_MARKS.map((m) => ({
                        swatch: LEGEND_SWATCH[m],
                        label: `${ATTENDANCE_MARK_SHORT[m]} ${ATTENDANCE_MARK_LABEL[m]}`,
                        hint: programType ? ATTENDANCE_PAY_HINT[programType][m] : undefined,
                      }))}
                    />

                    <p className="text-xs text-ink-2 leading-relaxed max-w-3xl">
                      {programType === 'CRT' ? (
                        <>
                          <HelpTip term="CRT">
                            A per-day engagement. The trainer is paid for the days they were
                            there, so the days marked present are added up.
                          </HelpTip>{' '}
                          pays <strong>per day</strong> and counts{' '}
                          <HelpTip term="payable days">
                            The number of days the pay is worked out from. On CRT they are
                            counted <strong>up</strong> from the P marks; on bCAP they are
                            counted <strong>down</strong> from the length of the period.
                          </HelpTip>{' '}
                          up from the P marks — so a day nobody marked pays{' '}
                          <strong>nothing</strong>. Weekends and college holidays carry no P
                          either, so they are not paid. Every gap left in this calendar quietly{' '}
                          <strong>underpays</strong> the trainer, and that is why completeness
                          is a <strong>hard block</strong> here: the month cannot be sent for
                          approval while a day that has already passed, inside the deployment
                          dates, is still unmarked.
                        </>
                      ) : programType === 'bCAP' ? (
                        <>
                          <HelpTip term="bCAP">
                            A monthly retainer engagement. The trainer is paid for the month and
                            absences are taken off it.
                          </HelpTip>{' '}
                          pays <strong>per month</strong>, spread across the calendar days of
                          the month, and counts{' '}
                          <HelpTip term="payable days">
                            The number of days the pay is worked out from. On bCAP they are
                            counted <strong>down</strong> from the length of the period; on CRT
                            they are counted <strong>up</strong> from the P marks.
                          </HelpTip>{' '}
                          down from the length of the period — so a day nobody marked is{' '}
                          <strong>paid</strong>. Weekends and college holidays are paid too; the
                          retainer absorbs them. Which makes a real absence left unmarked money
                          out of the door. Completeness is only a <strong>warning</strong> here:
                          the month can still be sent for approval with gaps in it.
                        </>
                      ) : (
                        <>
                          This deployment has no program type on record, so what a mark is worth
                          cannot be stated. On a per-day CRT engagement payable days are counted
                          up from the P marks and an unmarked day pays nothing; on a monthly
                          bCAP retainer they are counted down from the length of the period and
                          an unmarked day is paid. Find the program type before relying on
                          anything marked here.
                        </>
                      )}
                    </p>

                    <p className="text-xs text-ink-3 leading-relaxed max-w-3xl">
                      A half day (H) is worth 0.5 on either type — half a payable day on CRT,
                      half a day off the retainer on bCAP. An absence (A) is a full day: zero on
                      CRT, one day deducted on bCAP.
                    </p>
                  </div>

                  <div className="grid grid-cols-7 gap-1.5 mb-1.5">
                    {['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].map((d) => (
                      <div
                        key={d}
                        className="text-center text-[11px] font-medium uppercase tracking-wide text-ink-3"
                      >
                        {d}
                      </div>
                    ))}
                  </div>

                  <div className="grid grid-cols-7 gap-1.5">
                    {Array.from({ length: firstWeekday }, (_, i) => (
                      <div key={`pad-${i}`} aria-hidden />
                    ))}

                    {cells.map((cell) => (
                      <button
                        key={cell.date}
                        type="button"
                        disabled={!cell.inWindow || setMark.isPending}
                        onClick={() => setMark.mutate({ date: cell.date, mark: brush })}
                        className={attendanceCellClass(cell.mark, false)}
                        title={
                          cell.inWindow
                            ? `${cell.date} · ${ATTENDANCE_MARK_LABEL[cell.mark]}${
                                cell.note ? ` — ${cell.note}` : ''
                              }`
                            : `${cell.date} — outside the deployment window`
                        }
                        aria-label={`${cell.date}: ${ATTENDANCE_MARK_LABEL[cell.mark]}`}
                      >
                        <span className="block leading-none">{cell.day}</span>
                        <span className="block text-[10px] leading-none mt-0.5 opacity-80">
                          {cell.mark === 'UNMARKED' && cell.isFuture
                            ? ''
                            : ATTENDANCE_MARK_SHORT[cell.mark]}
                        </span>
                      </button>
                    ))}
                  </div>

                  <div className="flex flex-wrap items-center justify-between gap-3 mt-4 pt-3 border-t border-line">
                    <p className="text-xs text-ink-3">
                      Days outside {fmtDate(selected.start_date)} → {fmtDate(selected.end_date)}{' '}
                      are not markable.
                    </p>
                    <Button
                      size="sm"
                      disabled={unmarkedWeekdays.length === 0 || fillWeekdays.isPending}
                      title="Only fills days with no mark at all. Never overwrites an existing one."
                      onClick={() => fillWeekdays.mutate()}
                    >
                      Fill {unmarkedWeekdays.length} unmarked weekday
                      {unmarkedWeekdays.length === 1 ? '' : 's'} with P
                    </Button>
                  </div>
                </Card>

                {/* CORRECTED COPY. This note used to say a trainer could add
                    their own marks. They cannot, and could not since migration
                    1800 dropped all eighteen trainer policies — four of them
                    writes, two of which let the payee mark the attendance that
                    decided their own pay. Trainers are records, not users
                    (§4); they never sign in. */}
                <InfoNote>
                  Every mark and every correction is made here, by campus staff or the Manager.
                  Trainers do not sign in to this system at all — the person being paid must not
                  be able to write the record that decides the payment.
                </InfoNote>
              </>
            )}
          </div>
        )}
      </Page>
    </>
  )
}
