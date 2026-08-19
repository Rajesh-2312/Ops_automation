import { useMemo } from 'react'
import { PAGE, bounded, emptyBound } from '../lib/bounds'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { supabase, errorMessage } from '../lib/supabase'
import { qk } from '../lib/queryKeys'
import {
  ROLE_LABEL,
  STAGES,
  STAGE_LABEL,
  deriveUrgency,
  dueLabel,
  type AttendanceMark,
  type Batch,
  type Deployment,
  type DocStatus,
  type Program,
  type ProgramStage,
  type ProgramType,
  type RateBasis,
  type Task,
} from '../lib/types'
import { useAuth } from '../auth/AuthProvider'
import { Page, PageHeader } from '../components/AppShell'
import {
  Badge,
  BoundNote,
  Button,
  DocStatusPill,
  ErrorNote,
  fmtAmount,
  fmtDate,
  HelpTip,
  InfoNote,
  Loading,
  PageIntro,
  UrgencyPill,
} from '../components/ui'
import {
  BentoGrid,
  Metric,
  SegmentBar,
  SegmentLegend,
  Tile,
  TileEmpty,
  TileRow,
} from '../components/bento'

/* ==========================================================================
   The internal home screen.

   Until now the console opened onto the work queue — a list. A list is the
   right SECOND screen and the wrong first one: it answers "what is on me" and
   nothing else, so a Senior Manager landed on a page whose entire content was
   somebody else's checklist, and an LDE Executive landed on the same page a
   Senior Manager did.

   THE THREE HOMES ARE DIFFERENT SCREENS, NOT ONE SCREEN WITH FEWER ROWS.
   That is a departure from the rest of this console, where all three personas
   share components and RLS decides what comes back (see OpsRoot's comment), and
   it is deliberate here and nowhere else. The other screens answer a question
   that happens to be scoped — "show me the attendance I can reach". A home
   screen answers a question that is different PER PERSONA:

     Senior Manager  what is blocked on my signature, and what is the money doing
     Manager         what is on fire in my colleges, and what will block a payout
     LDE Executive   is today's attendance marked, and what is due on campus today

   Composing one grid and hiding tiles would produce a Senior Manager's screen
   with holes in it, which is what a shared dashboard always degrades into.

   WHAT IS AND IS NOT A CONTROL. `canSeeCommercials` below decides whether a
   money tile is drawn AND whether its query is issued at all. Neither is the
   wall: `pnl` and `work_orders` are gated by `can_see_commercials()` inside
   Postgres and return zero rows regardless (CLAUDE.md R5). Skipping the query
   is about not firing a request whose only possible outcome is an empty result
   — and, for the LDE Executive, about the screen containing no trace of a
   commercial concept at all. §4 does not say "shows an empty payout tile".

   NO ARITHMETIC ON MONEY HAPPENS HERE. Every rupee figure is a string from the
   server, cast with `::text` in the select so PostgREST cannot hand JavaScript
   a float, and rendered through `fmtAmount`, which groups digits and does not
   compute (R2/R7). There is no total row on this screen for that reason: a sum
   of trainer costs is arithmetic on money and it belongs in Python.
   ========================================================================== */

// --- Dates -------------------------------------------------------------------
// Hand-built rather than via toISOString(), which converts to UTC and would
// slide an IST morning back a day. Same reasoning as AttendancePage.

const pad = (n: number) => String(n).padStart(2, '0')
const ymd = (d: Date) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`

function monthWindow(): { key: string; first: string; last: string; today: string } {
  const now = new Date()
  const first = new Date(now.getFullYear(), now.getMonth(), 1)
  const last = new Date(now.getFullYear(), now.getMonth() + 1, 0)
  return {
    key: `${now.getFullYear()}-${pad(now.getMonth() + 1)}`,
    first: ymd(first),
    last: ymd(last),
    today: ymd(now),
  }
}

/** Whole days between an ISO date and today. Days, not money. */
function daysSince(iso: string | null): number | null {
  if (!iso) return null
  const then = new Date(iso)
  if (Number.isNaN(then.getTime())) return null
  return Math.floor((Date.now() - then.getTime()) / 86_400_000)
}

function greeting(): string {
  const h = new Date().getHours()
  if (h < 12) return 'Good morning'
  if (h < 17) return 'Good afternoon'
  return 'Good evening'
}

// --- Row shapes --------------------------------------------------------------

type ProgramRow = Program & { colleges: { id: string; name: string } | null }

type TaskRow = Task & {
  programs: { name: string; colleges: { name: string } | null } | null
}

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

type WorkOrderRow = {
  id: string
  rate: string
  rate_basis: RateBasis
  valid_from: string
  valid_to: string
  status: DocStatus
  trainers: { full_name: string; pan: string } | null
  programs: { name: string; colleges: { name: string } | null } | null
}

type PnlRow = {
  id: string
  program_id: string
  revenue: string | null
  trainer_cost: string | null
  accrued_amount: string | null
  invoiced_amount: string | null
  programs: { name: string; colleges: { name: string } | null } | null
}

/**
 * `artifact_versions` (migration 1300). Not in lib/types.ts yet — that file is
 * a hand-written mirror of the schema and adding a row type to it is a schema
 * decision, not a dashboard one. Declared locally with only the columns this
 * screen reads.
 */
type ArtifactVersionRow = {
  id: string
  artifact_type: string
  artifact_id: string
  version: number
  state: string
  submitted_at: string | null
  created_at: string
  notes: string | null
}

/**
 * The wire type of the `pnl` select above.
 *
 * This is the ONE place in this file that asserts a row shape rather than
 * having it inferred, and it is not new work: the query previously ran through
 * `unwrap<PnlRow[]>(…)`, which asserted exactly the same thing less visibly.
 *
 * The reason it has to be stated is the one ErmSyncPage documents at its
 * subject query: with no generated database types in this project, supabase-js
 * infers a to-ONE embed as an ARRAY, so it types `programs(name, colleges(…))`
 * as `{ name; colleges: { name }[] }[]` where PostgREST actually returns a
 * single object. Selects elsewhere in this file open with `*`, which stops the
 * inference before it reaches the embed and is why they need no assertion.
 *
 * The assertion covers the embed shape only — every scalar above is a `::text`
 * cast for R7, and `PnlRow` types all four of them as `string`.
 */
type PnlQuery = PromiseLike<{ data: PnlRow[] | null; error: unknown }>

const ARTIFACT_LABEL: Record<string, string> = {
  remuneration_sheets: 'Remuneration sheet',
  governance_reports: 'Governance report',
  program_documents: 'Program document',
}

type AttendanceRow = {
  deployment_id: string
  mark_date: string
  mark: AttendanceMark
}

// --- Shared queries ----------------------------------------------------------
// Every one of these is UNFILTERED by college. The policies resolve reach in
// Postgres, so the identical statement gives a Senior Manager their clusters
// and an LDE Executive their campus. Keys and selects are copied verbatim from
// the screens that already run them, so the home warms those caches instead of
// duplicating the rows under a second name.
//
// BOUNDS ON A DASHBOARD ARE A DIFFERENT PROBLEM FROM BOUNDS ON A LIST.
//
// Every tile below is a COUNT, a MAXIMUM or a GROUPING over one of these
// reads, and none of them displays the rows themselves. So a truncated read
// here does not produce a short list a reader can see the end of — it produces
// a confidently wrong number: "4 colleges need intervention" when it is
// eleven, "0 days unmarked" when the deployments that carry the gap fell past
// the bound. That is the exact failure this screen must not have, because the
// three homes exist to tell a persona what to act on first.
//
// Two consequences, both applied throughout:
//   1. Each of these hooks returns the bound alongside the rows, and every
//      tile whose figure depends on a truncated read says so.
//   2. Each uses the DEFAULT page size rather than a screen-local one, so the
//      cache entries are the same ones Work queue, Program Console,
//      Deployments and Attendance fill — which is what the comment above
//      means by warming rather than duplicating.

function useProgramsQuery() {
  return useQuery({
    queryKey: qk.programs.list(PAGE.programs),
    queryFn: () =>
      bounded<ProgramRow>(PAGE.programs, (rows) =>
        supabase
          .from('programs')
          .select('*, colleges(id, name)')
          .order('created_at', { ascending: false })
          .limit(rows),
      ),
  })
}

function useOpenTasksQuery() {
  return useQuery({
    queryKey: qk.tasks.queue(PAGE.tasks),
    queryFn: () =>
      bounded<TaskRow>(PAGE.tasks, (rows) =>
        supabase
          .from('tasks')
          .select('*, programs(name, colleges(name))')
          .neq('status', 'done')
          .order('due_date', { nullsFirst: false })
          .limit(rows),
      ),
  })
}

function useDeploymentsQuery() {
  return useQuery({
    queryKey: qk.deployments.list(PAGE.deployments),
    queryFn: () =>
      bounded<DeploymentRow>(PAGE.deployments, (rows) =>
        supabase
          .from('deployments')
          .select(
            '*, trainers(id, full_name, pan), batches(id, name, programs(id, name, type, colleges(name)))',
          )
          .order('start_date', { ascending: false })
          .limit(rows),
      ),
  })
}

/**
 * This month's marks, across every deployment the caller reaches.
 *
 * The month window is the real bound and does most of the work — but unlike
 * AttendancePage's single-deployment read, this one is unfiltered by
 * deployment, so its size is (deployments reachable × days elapsed) and it
 * grows with the business exactly as §1.3 of the architecture review projects.
 * At 40 trainers that is ~1,200 rows a month; the 5,000 ceiling is the
 * backstop, and `.order('mark_date')` keeps the slice deterministic if it ever
 * bites.
 *
 * When it DOES bite, the unmarked-day tiles overstate the gap — a marked day
 * whose row fell past the bound looks unmarked — so those tiles say so rather
 * than sending somebody to re-mark a day that is already marked.
 */
function useMonthAttendanceQuery(window: ReturnType<typeof monthWindow>) {
  return useQuery({
    queryKey: qk.home.attendanceMonth(window.key, PAGE.attendanceMonth),
    queryFn: () =>
      bounded<AttendanceRow>(PAGE.attendanceMonth, (rows) =>
        supabase
          .from('trainer_attendance')
          .select('deployment_id, mark_date, mark')
          .gte('mark_date', window.first)
          .lte('mark_date', window.last)
          .order('mark_date')
          .limit(rows),
      ),
  })
}

// --- Derivations -------------------------------------------------------------

interface TaskBands {
  overdue: TaskRow[]
  today: TaskRow[]
  soon: TaskRow[]
  blocked: TaskRow[]
  /** Overdue + due today + blocked. What "act now" means on a home screen. */
  actNow: TaskRow[]
}

function bandTasks(tasks: TaskRow[]): TaskBands {
  const bands: TaskBands = { overdue: [], today: [], soon: [], blocked: [], actNow: [] }
  for (const t of tasks) {
    const u = deriveUrgency(t)
    if (u === 'overdue') bands.overdue.push(t)
    else if (u === 'due_today') bands.today.push(t)
    else if (u === 'due_soon') bands.soon.push(t)
    else if (u === 'blocked') bands.blocked.push(t)
  }
  bands.actNow = [...bands.overdue, ...bands.today, ...bands.blocked]
  return bands
}

const collegeOf = (t: TaskRow) => t.programs?.colleges?.name ?? 'Unassigned college'

/**
 * Stage segments in a single-hue ramp rather than six arbitrary colours.
 *
 * Six hues would be a new palette, and this app spends its colour budget on
 * status (see ui.tsx). A ramp also says the true thing: the stages are ordered,
 * so later means further along, and a reader gets that from the bar without the
 * legend.
 */
function stageSegments(programs: ProgramRow[]) {
  const counts = new Map<ProgramStage, number>(STAGES.map((s) => [s, 0]))
  for (const p of programs) counts.set(p.stage, (counts.get(p.stage) ?? 0) + 1)
  return STAGES.map((s, i) => ({
    key: s,
    label: STAGE_LABEL[s],
    value: counts.get(s) ?? 0,
    color: `color-mix(in oklch, var(--color-accent) ${25 + i * 15}%, var(--color-surface-2))`,
  }))
}

/** Deployments live on the calendar day `date` (open-ended dates count). */
function activeOn(deployments: DeploymentRow[], date: string): DeploymentRow[] {
  return deployments.filter(
    (d) => (!d.start_date || d.start_date <= date) && (!d.end_date || d.end_date >= date),
  )
}

/**
 * Days this month that should carry a mark and do not, per deployment.
 *
 * Counts calendar days only. It deliberately stops short of saying what the gap
 * is WORTH, because that depends on program type in opposite directions — an
 * unmarked day silently underpays a CRT trainer and silently pays a bCAP one
 * (CLAUDE.md §5) — and the answer is the payout engine's, not a dashboard's.
 * The tile shows the count and names the consequence in words.
 */
function unmarkedByDeployment(
  deployments: DeploymentRow[],
  rows: AttendanceRow[],
  window: ReturnType<typeof monthWindow>,
) {
  const marked = new Set(
    rows.filter((r) => r.mark !== 'UNMARKED').map((r) => `${r.deployment_id}|${r.mark_date}`),
  )
  const endOfWindow = window.today < window.last ? window.today : window.last

  return deployments
    .map((d) => {
      const from = d.start_date && d.start_date > window.first ? d.start_date : window.first
      const to = d.end_date && d.end_date < endOfWindow ? d.end_date : endOfWindow
      if (from > to) return { deployment: d, days: 0, expected: 0 }

      let days = 0
      let expected = 0
      const cursor = new Date(`${from}T00:00:00`)
      const stop = new Date(`${to}T00:00:00`)
      while (cursor <= stop) {
        const key = ymd(cursor)
        expected += 1
        if (!marked.has(`${d.id}|${key}`)) days += 1
        cursor.setDate(cursor.getDate() + 1)
      }
      return { deployment: d, days, expected }
    })
    .filter((r) => r.expected > 0)
}

// ==========================================================================
// Entry point
// ==========================================================================

export function HomePage() {
  const { profile, canSeeCommercials } = useAuth()
  const role = profile?.role

  const firstName = profile?.full_name?.split(' ')[0] ?? 'there'
  const subtitle =
    role === 'senior_manager'
      ? 'Your clusters, and what is waiting on you.'
      : role === 'manager'
        ? 'Your colleges, and what will block them this week.'
        : 'Your campus today.'

  return (
    <>
      {/* The title is a greeting, so it says nothing about the screen. `purpose`
          carries that load instead: a first-time reader has to learn what this
          page IS from somewhere, and on every other screen the title does it. */}
      <PageHeader
        title={`${greeting()}, ${firstName}`}
        subtitle={subtitle}
        purpose="Everything assigned to you across the colleges you cover, gathered onto one screen with the most urgent first — what is late, what is blocked, and what is waiting on you today."
        actions={role ? <Badge tone="accent">{ROLE_LABEL[role]}</Badge> : undefined}
      />
      <Page>
        {/* The vocabulary, once, on the front door.
            This screen is three different dashboards and none of them can carry
            their own glossary: `Tile`'s title and hint are typed `string`, so a
            HelpTip cannot live inside a tile. Explaining the four words every
            home shares — and where a persona's reach actually comes from —
            belongs here, above the split, where it is read once rather than
            three times. */}
        <PageIntro className="mb-5">
          Your screen is built from what you have been{' '}
          <HelpTip term="assigned">
            Reach is not the same as job title. A Manager covers the colleges
            they are assigned to and a Senior Manager covers a cluster of them,
            so two people with identical roles can open this page and correctly
            see different work. If something you expect is missing, it is an
            assignment question, not a bug.
          </HelpTip>
          , not from your job title alone. Three words carry most of this page:
          a{' '}
          <HelpTip term="deployment">
            One named trainer teaching one named batch, between a start date and
            an end date. It is the unit almost everything else hangs off —
            attendance is marked against it, and pay is always calculated for
            one, never for a trainer on their own.
          </HelpTip>
          , the{' '}
          <HelpTip term="work order">
            The signed agreement with a trainer, carrying the agreed rate and
            the dates it is valid between. No signed work order on file means no
            payout can leave draft — and if the rate on a payout disagrees with
            the rate in the signed order, that is a blocking failure, not a
            rounding difference.
          </HelpTip>{' '}
          behind it, and the{' '}
          <HelpTip term="payout">
            One trainer, one calendar month. Every figure in it is computed in
            Python against attendance and the signed rate; nothing here is
            approved or sent — the console only ever shows you a draft and who
            still has to look at it.
          </HelpTip>{' '}
          at the end of it.
        </PageIntro>

        {role === 'senior_manager' ? (
          <SeniorManagerHome />
        ) : role === 'manager' ? (
          <ManagerHome />
        ) : (
          // LDE Executive, and the fail-safe: an internal login with an
          // unexpected role gets the narrowest home, never the widest.
          <LdeHome commercialsHidden={!canSeeCommercials} />
        )}
      </Page>
    </>
  )
}

// ==========================================================================
// Senior Manager
// ==========================================================================

function SeniorManagerHome() {
  const programsQuery = useProgramsQuery()
  const tasksQuery = useOpenTasksQuery()

  const approvalsQuery = useQuery({
    queryKey: qk.home.pendingApprovals(PAGE.approvals),
    queryFn: () =>
      bounded<ArtifactVersionRow>(PAGE.approvals, (rows) =>
        supabase
          .from('artifact_versions')
          .select('id, artifact_type, artifact_id, version, state, submitted_at, created_at, notes')
          .eq('state', 'PENDING_APPROVAL')
          .order('submitted_at', { nullsFirst: false })
          .limit(rows),
      ),
  })

  const pnlQuery = useQuery({
    queryKey: qk.home.pnl(PAGE.pnl),
    queryFn: () =>
      bounded<PnlRow>(PAGE.pnl, (limitRows) =>
        supabase
          .from('pnl')
          // `::text` per column: PostgREST serialises numeric as a JSON number
          // and a rupee must never become a float in a browser (R7).
          //
          // ONE LITERAL STRING, not a concatenation, for the reason
          // lib/approvals.ts records on VERSION_COLUMNS: supabase-js infers the
          // row shape from the select as a template-literal type, and a `+`
          // join collapses that inference to GenericStringError.
          .select(
            'id, program_id, revenue:revenue::text, trainer_cost:trainer_cost::text, accrued_amount:accrued_amount::text, invoiced_amount:invoiced_amount::text, programs(name, colleges(name))',
          )
          // `pnl` has one row per program, so this bound is the programs bound
          // in disguise; ordering by program keeps the slice stable.
          .order('program_id')
          .limit(limitRows) as PnlQuery,
      ),
  })

  const workOrdersQuery = useQuery({
    queryKey: qk.workOrders.list(PAGE.workOrders),
    queryFn: () =>
      bounded<WorkOrderRow>(PAGE.workOrders, (rows) =>
        supabase
          .from('work_orders')
          .select(
            '*, rate:rate::text, trainers(full_name, pan), programs(name, type, colleges(name))',
          )
          .order('valid_from', { ascending: false })
          .limit(rows),
      ),
  })

  const programsBound = programsQuery.data ?? emptyBound<ProgramRow>(PAGE.programs)
  const tasksBound = tasksQuery.data ?? emptyBound<TaskRow>(PAGE.tasks)
  const programs = useMemo(() => programsBound.rows, [programsBound.rows])
  const bands = useMemo(() => bandTasks(tasksBound.rows), [tasksBound.rows])
  const approvals = approvalsQuery.data?.rows ?? []
  const pnl = pnlQuery.data?.rows ?? []
  const workOrders = workOrdersQuery.data?.rows ?? []
  const unsigned = workOrders.filter((w) => w.status !== 'signed')

  const byCollege = useMemo(() => {
    const map = new Map<string, { overdue: number; blocked: number }>()
    for (const t of bands.overdue) {
      const acc = map.get(collegeOf(t)) ?? { overdue: 0, blocked: 0 }
      acc.overdue += 1
      map.set(collegeOf(t), acc)
    }
    for (const t of bands.blocked) {
      const acc = map.get(collegeOf(t)) ?? { overdue: 0, blocked: 0 }
      acc.blocked += 1
      map.set(collegeOf(t), acc)
    }
    return [...map.entries()].sort(
      (a, b) => b[1].overdue + b[1].blocked - (a[1].overdue + a[1].blocked),
    )
  }, [bands.overdue, bands.blocked])

  const segments = useMemo(() => stageSegments(programs), [programs])

  const loading = programsQuery.isPending || tasksQuery.isPending
  const failure = programsQuery.error ?? tasksQuery.error

  if (loading) return <Loading label="Loading your clusters" />

  return (
    <div className="space-y-4">
      {failure && <ErrorNote>{errorMessage(failure)}</ErrorNote>}

      {/* Every tile below is a count or a grouping rather than a list, so a
          truncated read shows a confident wrong number instead of a short
          list. Disclosed here, once, above the grid it applies to. */}
      <BoundNote
        bound={tasksBound}
        noun="open tasks"
        derived="The escalation roll-up by college counts only those."
      />
      <BoundNote
        bound={programsBound}
        noun="programs"
        derived="The pipeline bar counts only those."
      />
      <BoundNote
        bound={approvalsQuery.data}
        noun="artifacts pending approval"
        derived="More are waiting on you than the number shown."
      />
      <BoundNote bound={pnlQuery.data} noun="P&L rows" />
      <BoundNote
        bound={workOrdersQuery.data}
        noun="work orders"
        derived="The unsigned count is a floor, and an unsigned WO blocks a payout at §7."
      />

      <BentoGrid>
        {/* HERO — the only tile on this screen where the organisation is
            literally stopped until this person acts. Everything else is
            information; this is a queue with their name on it (R4: approval is
            an authenticated human act, and this is that human). */}
        <Tile
          title="Waiting on your approval"
          hint="Submitted artifacts frozen at PENDING_APPROVAL"
          span={2}
          rows={2}
          tone={approvals.length > 0 ? 'alert' : 'neutral'}
          count={approvals.length || undefined}
        >
          {approvalsQuery.error ? (
            <p className="text-xs text-ink-3 leading-relaxed">
              {errorMessage(approvalsQuery.error)}
            </p>
          ) : approvals.length === 0 ? (
            <TileEmpty
              whatFills={
                'Nothing is submitted for approval. A remuneration sheet lands here when a ' +
                'Manager submits it, and it stays until you approve or reject it — approval ' +
                'freezes and hashes the version, and releasing it is a second, separate act.'
              }
            />
          ) : (
            <>
              <Metric
                value={approvals.length}
                size="lg"
                tone="bad"
                label={approvals.length === 1 ? 'artifact waiting' : 'artifacts waiting'}
              />
              <div className="mt-3">
                {approvals.slice(0, 8).map((a) => {
                  const waited = daysSince(a.submitted_at ?? a.created_at)
                  return (
                    <TileRow
                      key={a.id}
                      primary={ARTIFACT_LABEL[a.artifact_type] ?? a.artifact_type}
                      secondary={`v${a.version}${a.notes ? ` — ${a.notes}` : ''}`}
                      trailing={
                        <span
                          className={`text-[11px] tabular-nums ${
                            waited !== null && waited >= 3
                              ? 'font-medium text-[var(--color-bad)]'
                              : 'text-ink-3'
                          }`}
                        >
                          {waited === null
                            ? '—'
                            : waited === 0
                              ? 'today'
                              : `${waited}d waiting`}
                        </span>
                      }
                    />
                  )
                })}
              </div>
              <p className="text-[11px] text-ink-3 mt-3 leading-relaxed">
                There is no approve button here yet — the approval endpoints exist, the screen
                that calls them is Phase 2. Nothing on this page can release anything.
              </p>
            </>
          )}
        </Tile>

        {/* Escalations are the second question and get a wide, short tile: a
            Senior Manager wants the shape of the problem by college, not the
            individual task — that is the Manager's job and the queue's screen. */}
        <Tile
          title="Colleges needing intervention"
          hint="Overdue and blocked work, by college"
          span={2}
          tone={byCollege.length > 0 ? 'alert' : 'neutral'}
          to="/queue"
          toLabel="Work queue"
        >
          {byCollege.length === 0 ? (
            <TileEmpty
              whatFills={
                'No college in your clusters has overdue or blocked work. A task appears here ' +
                'the day it passes its due date, or the moment someone marks it blocked.'
              }
            />
          ) : (
            byCollege.slice(0, 6).map(([name, counts]) => (
              <TileRow
                key={name}
                primary={name}
                trailing={
                  <>
                    {counts.overdue > 0 && (
                      <span className="text-[11px] tabular-nums font-medium text-[var(--color-bad)]">
                        {counts.overdue} overdue
                      </span>
                    )}
                    {counts.blocked > 0 && (
                      <span className="text-[11px] tabular-nums text-[var(--color-warn)]">
                        {counts.blocked} blocked
                      </span>
                    )}
                  </>
                }
              />
            ))
          )}
        </Tile>

        <Tile
          title="Pipeline"
          hint="Programs across the six stages"
          span={2}
          count={programs.length || undefined}
          to="/board"
          toLabel="Console"
        >
          {programs.length === 0 ? (
            <TileEmpty
              whatFills={
                'No programs in your clusters yet. Either none have been created, or no cluster ' +
                'is assigned to you — an admin grants that on Users & roles.'
              }
              action={
                <Link to="/users">
                  <Button size="sm">Users &amp; roles</Button>
                </Link>
              }
            />
          ) : (
            <>
              <SegmentBar segments={segments} className="mt-1" />
              <SegmentLegend segments={segments} />
            </>
          )}
        </Tile>

        {/* COMMERCIALS. Drawn for this persona because §4 gives the Senior
            Manager P&L; the `pnl` policy is what actually permits the read. */}
        <Tile
          title="P&L by program"
          hint="Revenue, trainer cost, accrued, invoiced — as filed"
          span={3}
          count={pnl.length || undefined}
        >
          {pnlQuery.error ? (
            <p className="text-xs text-ink-3 leading-relaxed">{errorMessage(pnlQuery.error)}</p>
          ) : pnl.length === 0 ? (
            <TileEmpty
              whatFills={
                'No P&L rows filed. One is prepared per program during Acquisition & Setup; ' +
                'until Phase 2 there is no screen that writes one, so rows arrive from the ' +
                'database directly.'
              }
            />
          ) : (
            pnl.slice(0, 6).map((p) => (
              <TileRow
                key={p.id}
                primary={p.programs?.name ?? 'Program'}
                secondary={p.programs?.colleges?.name ?? undefined}
                trailing={
                  <span className="flex items-center gap-3 text-[11px] tabular-nums">
                    <span className="text-ink-2" title="Revenue">
                      {fmtAmount(p.revenue)}
                    </span>
                    <span className="text-ink-3" title="Trainer cost">
                      −{fmtAmount(p.trainer_cost)}
                    </span>
                    <span className="text-ink-3" title="Invoiced">
                      inv {fmtAmount(p.invoiced_amount)}
                    </span>
                  </span>
                }
              />
            ))
          )}
        </Tile>

        {/* One number, because a Senior Manager acts on the count and a Manager
            acts on the list. An unsigned work order is the §7 gate that stops a
            payout cycle dead, which is why it is on this screen and not buried
            in Work orders. */}
        <Tile
          title="Unsigned work orders"
          hint="Blocks the payout cycle at §7"
          to="/work-orders"
          tone={unsigned.length > 0 ? 'alert' : 'neutral'}
        >
          {workOrdersQuery.error ? (
            <p className="text-xs text-ink-3">{errorMessage(workOrdersQuery.error)}</p>
          ) : unsigned.length === 0 ? (
            <TileEmpty
              whatFills={
                workOrders.length === 0
                  ? 'No work orders on file at all yet.'
                  : workOrdersQuery.data?.truncated
                    ? 'Every work order that was loaded is signed — but the read was cut at its row bound, so this is not a statement about all of them.'
                    : 'Every work order on file is signed. Nothing here is holding a payout.'
              }
            />
          ) : (
            <Metric
              value={unsigned.length}
              tone="bad"
              size="lg"
              label={`of ${workOrders.length}${
                workOrdersQuery.data?.truncated ? '+' : ''
              } on file`}
              caption="No payout validates without a signed WO covering the period."
            />
          )}
        </Tile>
      </BentoGrid>
    </div>
  )
}

// ==========================================================================
// Manager
// ==========================================================================

function ManagerHome() {
  const programsQuery = useProgramsQuery()
  const tasksQuery = useOpenTasksQuery()
  const deploymentsQuery = useDeploymentsQuery()
  const window = useMemo(monthWindow, [])
  const attendanceQuery = useMonthAttendanceQuery(window)

  const workOrdersQuery = useQuery({
    queryKey: qk.workOrders.list(PAGE.workOrders),
    queryFn: () =>
      bounded<WorkOrderRow>(PAGE.workOrders, (rows) =>
        supabase
          .from('work_orders')
          .select(
            '*, rate:rate::text, trainers(full_name, pan), programs(name, type, colleges(name))',
          )
          .order('valid_from', { ascending: false })
          .limit(rows),
      ),
  })

  const programsBound = programsQuery.data ?? emptyBound<ProgramRow>(PAGE.programs)
  const tasksBound = tasksQuery.data ?? emptyBound<TaskRow>(PAGE.tasks)
  const deploymentsBound = deploymentsQuery.data ?? emptyBound<DeploymentRow>(PAGE.deployments)
  const attendanceBound =
    attendanceQuery.data ?? emptyBound<AttendanceRow>(PAGE.attendanceMonth)

  const programs = useMemo(() => programsBound.rows, [programsBound.rows])
  const bands = useMemo(() => bandTasks(tasksBound.rows), [tasksBound.rows])
  const deployments = useMemo(() => deploymentsBound.rows, [deploymentsBound.rows])
  const workOrders = workOrdersQuery.data?.rows ?? []

  const segments = useMemo(() => stageSegments(programs), [programs])

  // Unmarked days are derived from TWO bounded reads at once, and they fail in
  // opposite directions: a deployment past its bound hides a real gap, an
  // attendance row past its bound invents one. Either way the number is not
  // the month's gap, so it is disclosed rather than presented.
  const gapsComplete = !deploymentsBound.truncated && !attendanceBound.truncated

  const gaps = useMemo(
    () => unmarkedByDeployment(deployments, attendanceBound.rows, window),
    [deployments, attendanceBound.rows, window],
  )
  const gapDays = gaps.reduce((n, g) => n + g.days, 0)
  const crtGapDays = gaps
    .filter((g) => g.deployment.batches?.programs?.type === 'CRT')
    .reduce((n, g) => n + g.days, 0)

  const perCollege = useMemo(() => {
    const map = new Map<string, { programs: number; overdue: number; blocked: number }>()
    for (const p of programs) {
      const name = p.colleges?.name ?? 'Unassigned college'
      const acc = map.get(name) ?? { programs: 0, overdue: 0, blocked: 0 }
      acc.programs += 1
      map.set(name, acc)
    }
    for (const t of bands.overdue) {
      const acc = map.get(collegeOf(t)) ?? { programs: 0, overdue: 0, blocked: 0 }
      acc.overdue += 1
      map.set(collegeOf(t), acc)
    }
    for (const t of bands.blocked) {
      const acc = map.get(collegeOf(t)) ?? { programs: 0, overdue: 0, blocked: 0 }
      acc.blocked += 1
      map.set(collegeOf(t), acc)
    }
    return [...map.entries()].sort((a, b) => b[1].overdue - a[1].overdue)
  }, [programs, bands.overdue, bands.blocked])

  const unsigned = workOrders.filter((w) => w.status !== 'signed')

  const loading = programsQuery.isPending || tasksQuery.isPending
  const failure = programsQuery.error ?? tasksQuery.error

  if (loading) return <Loading label="Loading your colleges" />

  return (
    <div className="space-y-4">
      {failure && <ErrorNote>{errorMessage(failure)}</ErrorNote>}

      <BoundNote
        bound={tasksBound}
        noun="open tasks"
        derived="The per-college roll-up counts only those."
      />
      <BoundNote bound={programsBound} noun="programs" />
      <BoundNote
        bound={deploymentsBound}
        noun="deployments"
        derived="The unmarked-day figure is not stated while this read is cut — a deployment past the bound would hide a real gap."
      />
      <BoundNote
        bound={attendanceQuery.data}
        noun="attendance rows this month"
        derived="A marked day past the bound would be counted as unmarked, so the gap figure is withheld."
      />
      <BoundNote
        bound={workOrdersQuery.data}
        noun="work orders"
        derived="The unsigned count is a floor."
      />

      <BentoGrid>
        {/* HERO — a Manager's day is made of individual tasks, so unlike the
            Senior Manager's roll-up this one names them. Overdue first, then
            due today, then blocked: the order they have to be dealt with. */}
        <Tile
          title="Needs you today"
          hint="Overdue, due today, and blocked — across every college you reach"
          span={2}
          rows={2}
          tone={bands.actNow.length > 0 ? 'alert' : 'neutral'}
          count={bands.actNow.length || undefined}
          to="/queue"
          toLabel="Full queue"
        >
          {bands.actNow.length === 0 ? (
            <TileEmpty
              whatFills={
                tasksBound.rows.length === 0
                  ? 'No open tasks in your colleges. A program gets its full checklist the ' +
                    'moment it is created, so an empty queue usually means a program still ' +
                    'needs generating.'
                  : 'Everything open is scheduled further out. Items move here on the day they ' +
                    'come due, or the moment someone marks one blocked.'
              }
              action={
                <Link to="/board">
                  <Button size="sm">Program Console</Button>
                </Link>
              }
            />
          ) : (
            <>
              <div className="flex items-baseline gap-4">
                <Metric
                  value={bands.overdue.length}
                  size="lg"
                  tone={bands.overdue.length > 0 ? 'bad' : 'normal'}
                  label="overdue"
                />
                <Metric value={bands.today.length} label="due today" />
                <Metric
                  value={bands.blocked.length}
                  tone={bands.blocked.length > 0 ? 'warn' : 'normal'}
                  label="blocked"
                />
              </div>
              <div className="mt-3">
                {bands.actNow.slice(0, 10).map((t) => (
                  <TileRow
                    key={t.id}
                    to={`/programs/${t.program_id}`}
                    primary={t.title}
                    secondary={`${collegeOf(t)} · ${t.programs?.name ?? '—'}${
                      t.due_date ? ` · ${dueLabel(t) ?? fmtDate(t.due_date)}` : ''
                    }`}
                    trailing={<UrgencyPill urgency={deriveUrgency(t)} />}
                  />
                ))}
              </div>
            </>
          )}
        </Tile>

        <Tile
          title="Your colleges"
          hint="Programs running, and what is late in each"
          span={2}
          count={perCollege.length || undefined}
          to="/colleges"
        >
          {perCollege.length === 0 ? (
            <TileEmpty
              whatFills={
                'No colleges are assigned to you yet. An admin assigns them on Users & roles; ' +
                'until then every screen in this console is legitimately empty.'
              }
              action={
                <Link to="/users">
                  <Button size="sm">Users &amp; roles</Button>
                </Link>
              }
            />
          ) : (
            perCollege.slice(0, 6).map(([name, c]) => (
              <TileRow
                key={name}
                primary={name}
                secondary={`${c.programs} program${c.programs === 1 ? '' : 's'}`}
                trailing={
                  c.overdue + c.blocked === 0 ? (
                    <span className="text-[11px] text-[var(--color-good)]">on track</span>
                  ) : (
                    <span className="text-[11px] tabular-nums font-medium text-[var(--color-bad)]">
                      {c.overdue + c.blocked} late
                    </span>
                  )
                }
              />
            ))
          )}
        </Tile>

        <Tile
          title="Pipeline"
          hint="Programs across the six stages"
          span={2}
          count={programs.length || undefined}
          to="/board"
          toLabel="Console"
        >
          {programs.length === 0 ? (
            <TileEmpty whatFills="No programs in your colleges yet. Create one on the Program Console and its checklist generates with it." />
          ) : (
            <>
              <SegmentBar segments={segments} className="mt-1" />
              <SegmentLegend segments={segments} />
            </>
          )}
        </Tile>

        <Tile
          title="Attendance gaps"
          hint={`Unmarked days so far in ${window.key}`}
          to="/attendance"
          // The figure below is suppressed when the reads were cut, so the TONE
          // has to be too — it is derived from exactly the same partial count.
          // Left as `crtGapDays > 0` it would state the withheld number in
          // colour: alert when the partial count happened to be non-zero,
          // reassuringly neutral when it happened to be zero, and neither of
          // those is known.
          tone={gapsComplete && crtGapDays > 0 ? 'alert' : 'neutral'}
        >
          {/* `gaps` is empty when no deployment overlapped this month at all —
              a different thing from "every day is marked", and saying the
              second when the first is true is how a dashboard earns distrust. */}
          {!gapsComplete ? (
            // Neither branch below can be told honestly from a truncated read:
            // a deployment past its bound HIDES a gap and an attendance row
            // past its bound INVENTS one, so the figure is withheld rather
            // than qualified. "0 days unmarked" is a sentence a Manager acts
            // on by not acting.
            <TileEmpty whatFills="Not stated: the deployment or attendance read was cut at its row bound this month, and an unmarked-day count from a partial read is wrong in both directions. Open Attendance for the per-deployment truth." />
          ) : gaps.length === 0 ? (
            <TileEmpty
              whatFills={
                deployments.length === 0
                  ? 'No deployments yet, so there is nothing to mark. Deploy a trainer to a batch and its days appear on Attendance.'
                  : `No deployment ran in ${window.key}, so no day this month is expected to carry a mark.`
              }
            />
          ) : (
            <Metric
              value={gapDays}
              size="lg"
              tone={crtGapDays > 0 ? 'bad' : gapDays > 0 ? 'warn' : 'good'}
              label={gapDays === 1 ? 'day unmarked' : 'days unmarked'}
              caption={
                crtGapDays > 0
                  ? `${crtGapDays} of them on CRT, where an unmarked day pays nothing and blocks the cycle.`
                  : gapDays > 0
                    ? 'All on bCAP, where an unmarked day pays in full — check it is meant to.'
                    : 'Every elapsed day this month carries a mark.'
              }
            />
          )}
        </Tile>

        <Tile
          title="Work orders"
          hint="Signed is the §7 gate"
          to="/work-orders"
          tone={unsigned.length > 0 ? 'alert' : 'neutral'}
        >
          {workOrders.length === 0 ? (
            <TileEmpty whatFills="None on file. A work order carries the rate a payout is validated against, so the cycle cannot run without one." />
          ) : (
            <Metric
              value={unsigned.length}
              size="lg"
              tone={unsigned.length > 0 ? 'bad' : 'good'}
              label={`unsigned of ${workOrders.length}`}
              caption={
                unsigned.length === 0
                  ? 'All signed.'
                  : unsigned.map((w) => w.trainers?.full_name ?? 'Unnamed').join(', ')
              }
            />
          )}
        </Tile>

        {/* Trainer cost, per §4. Rates only, exactly as filed — no total, no
            projection, no per-day derivation. Those are Decimal arithmetic and
            they live in engine.py (R2). */}
        <Tile
          title="Engagement rates on file"
          hint="The signed terms a payout is validated against"
          span={2}
          count={workOrders.length || undefined}
          to="/work-orders"
        >
          {workOrdersQuery.error ? (
            <p className="text-xs text-ink-3">{errorMessage(workOrdersQuery.error)}</p>
          ) : workOrders.length === 0 ? (
            <TileEmpty whatFills="No work orders yet. Raise one per trainer per program on the Work orders screen; the rate there is the one the engine checks against." />
          ) : (
            workOrders.slice(0, 6).map((w) => (
              <TileRow
                key={w.id}
                primary={w.trainers?.full_name ?? 'Unnamed trainer'}
                secondary={`${w.programs?.name ?? '—'} · ${fmtDate(w.valid_from)} → ${fmtDate(
                  w.valid_to,
                )}`}
                trailing={
                  <>
                    <span className="text-xs tabular-nums font-medium text-ink">
                      {fmtAmount(w.rate)}
                    </span>
                    <Badge>{w.rate_basis === 'per_day' ? '/day' : '/mo'}</Badge>
                    <DocStatusPill status={w.status} />
                  </>
                }
              />
            ))
          )}
        </Tile>
      </BentoGrid>
    </div>
  )
}

// ==========================================================================
// LDE Executive
// ==========================================================================

/**
 * The campus home. Compare it against the Senior Manager's: not one tile is
 * shared, and the hero is a marking checklist rather than an approval queue.
 *
 * THERE IS NO COMMERCIAL TILE ON THIS SCREEN, EMPTY OR OTHERWISE. §4 gives the
 * LDE Executive attendance, batches and daily tasks and denies them
 * commercials, and a greyed-out payout tile would still teach them that a
 * payout figure exists on this screen and is being withheld. No query in this
 * component touches `pnl`, `work_orders`, `remuneration_sheets` or the payout
 * API. The single mention of the boundary is a one-line note at the bottom,
 * which is honesty about the product's shape, not a placeholder for data.
 */
function LdeHome({ commercialsHidden }: { commercialsHidden: boolean }) {
  const programsQuery = useProgramsQuery()
  const tasksQuery = useOpenTasksQuery()
  const deploymentsQuery = useDeploymentsQuery()
  const window = useMemo(monthWindow, [])
  const attendanceQuery = useMonthAttendanceQuery(window)

  const batchesQuery = useQuery({
    queryKey: qk.home.batches(PAGE.batches),
    queryFn: () =>
      bounded<Batch & { programs: { name: string; type: ProgramType } | null }>(
        PAGE.batches,
        (rows) =>
          supabase.from('batches').select('*, programs(name, type)').order('name').limit(rows),
      ),
  })

  // One row per learner per batch — the highest-volume table on this screen by
  // some distance, and read here only to COUNT rows per batch. `.order('id')`
  // is required for the same reason as everywhere else: an unordered limit
  // takes an arbitrary slice, and here that would move the per-batch counts
  // between refetches for no visible reason.
  //
  // A per-batch count from a truncated roster is an undercount, and "roster
  // empty" is a thing an LDE Executive would act on — so the tile falls back to
  // the expected count and says the roster was not fully read.
  const studentsQuery = useQuery({
    queryKey: qk.home.studentCount(PAGE.students),
    queryFn: () =>
      bounded<{ id: string; batch_id: string }>(PAGE.students, (rows) =>
        supabase.from('students').select('id, batch_id').order('id').limit(rows),
      ),
  })

  const programsBound = programsQuery.data ?? emptyBound<ProgramRow>(PAGE.programs)
  const tasksBound = tasksQuery.data ?? emptyBound<TaskRow>(PAGE.tasks)
  const deploymentsBound = deploymentsQuery.data ?? emptyBound<DeploymentRow>(PAGE.deployments)
  const attendanceBound =
    attendanceQuery.data ?? emptyBound<AttendanceRow>(PAGE.attendanceMonth)

  const programs = programsBound.rows
  const bands = useMemo(() => bandTasks(tasksBound.rows), [tasksBound.rows])
  const deployments = useMemo(() => deploymentsBound.rows, [deploymentsBound.rows])
  const batches = batchesQuery.data?.rows ?? []
  const students = studentsQuery.data?.rows ?? []
  const rosterComplete = studentsQuery.data?.truncated === false

  const attendance = useMemo(() => attendanceBound.rows, [attendanceBound.rows])

  // Same two reads, same two opposite failures, same rule as ManagerHome: a
  // deployment past its bound HIDES a gap and an attendance row past its bound
  // INVENTS one, so the month's gap figure is withheld rather than qualified.
  //
  // This screen used to qualify it instead — the note below said the figure was
  // "an overstatement" and then drew it anyway. That is the wrong half of the
  // rule: an LDE Executive reads this tile as a list of days to go and mark,
  // and a number that is wrong in an unknown direction sends them either to
  // re-mark days that are already marked or, worse, home.
  const gapsComplete = !deploymentsBound.truncated && !attendanceBound.truncated

  /** Today's marking state, per deployment running today. The hero. */
  const todayRows = useMemo(() => {
    const marks = new Map<string, AttendanceMark>()
    for (const r of attendance) {
      if (r.mark_date === window.today) marks.set(r.deployment_id, r.mark)
    }
    return activeOn(deployments, window.today).map((d) => ({
      deployment: d,
      mark: marks.get(d.id) ?? null,
    }))
  }, [deployments, attendance, window.today])

  const unmarkedToday = todayRows.filter((r) => r.mark === null || r.mark === 'UNMARKED')

  const gaps = useMemo(
    () => unmarkedByDeployment(deployments, attendance, window),
    [deployments, attendance, window],
  )
  const gapDays = gaps.reduce((n, g) => n + g.days, 0)
  const crtGapDays = gaps
    .filter((g) => g.deployment.batches?.programs?.type === 'CRT')
    .reduce((n, g) => n + g.days, 0)

  const studentsPerBatch = useMemo(() => {
    const map = new Map<string, number>()
    for (const s of students) map.set(s.batch_id, (map.get(s.batch_id) ?? 0) + 1)
    return map
  }, [students])

  const loading = deploymentsQuery.isPending || tasksQuery.isPending
  const failure = deploymentsQuery.error ?? tasksQuery.error

  if (loading) return <Loading label="Loading your campus" />

  return (
    <div className="space-y-4">
      {failure && <ErrorNote>{errorMessage(failure)}</ErrorNote>}

      {/* The hero on this screen is a marking checklist: "3 trainers still
          unmarked today" is a to-do list, and a bounded read makes it a
          shorter one than the truth. */}
      <BoundNote
        bound={deploymentsBound}
        noun="deployments"
        derived="A deployment past the bound is missing from today's marking list."
      />
      <BoundNote
        bound={attendanceQuery.data}
        noun="attendance rows this month"
        derived="The unmarked-day figure is withheld rather than shown: a marked day past the bound would be counted here as unmarked."
      />
      <BoundNote bound={tasksBound} noun="open tasks" />
      <BoundNote bound={batchesQuery.data} noun="batches" />
      <BoundNote
        bound={studentsQuery.data}
        noun="student rows"
        derived="Per-batch enrolment is shown as “not fully loaded” rather than as a count."
      />

      <BentoGrid>
        {/* HERO — the one thing on campus that is time-sensitive to the day.
            A mark missed today is not a mark that can be reconstructed next
            month, and on CRT it is a day the trainer is not paid for. */}
        <Tile
          title="Today's attendance"
          hint={fmtDate(window.today)}
          span={2}
          rows={2}
          tone={unmarkedToday.length > 0 ? 'alert' : 'neutral'}
          count={todayRows.length ? `${todayRows.length - unmarkedToday.length}/${todayRows.length}` : undefined}
          to="/attendance"
          toLabel="Mark"
        >
          {todayRows.length === 0 ? (
            <TileEmpty
              whatFills={
                deployments.length === 0
                  ? 'No trainer is deployed to a batch at your college yet. Once a deployment ' +
                    'exists, each of its days appears here to be marked.'
                  : 'No deployment is running today — every one of them starts later or has ' +
                    'already ended. Nothing to mark.'
              }
              action={
                <Link to="/attendance">
                  <Button size="sm">Open Attendance</Button>
                </Link>
              }
            />
          ) : (
            <>
              <Metric
                value={unmarkedToday.length}
                size="lg"
                tone={unmarkedToday.length > 0 ? 'bad' : 'good'}
                label={
                  unmarkedToday.length === 0
                    ? 'every trainer marked today'
                    : unmarkedToday.length === 1
                      ? 'trainer still unmarked today'
                      : 'trainers still unmarked today'
                }
              />
              <div className="mt-3">
                {todayRows.map(({ deployment: d, mark }) => (
                  <TileRow
                    key={d.id}
                    primary={d.trainers?.full_name ?? 'Unnamed trainer'}
                    secondary={`${d.batches?.programs?.name ?? '—'} · ${
                      d.batches?.name ?? 'Batch'
                    }`}
                    trailing={
                      mark && mark !== 'UNMARKED' ? (
                        <span className="text-[11px] font-medium text-[var(--color-good)]">
                          marked {mark}
                        </span>
                      ) : (
                        <span className="text-[11px] font-medium text-[var(--color-bad)]">
                          not marked
                        </span>
                      )
                    }
                  />
                ))}
              </div>
            </>
          )}
        </Tile>

        <Tile
          title="On you today"
          hint="Overdue, due today and blocked tasks at your college"
          span={2}
          tone={bands.actNow.length > 0 ? 'alert' : 'neutral'}
          count={bands.actNow.length || undefined}
          to="/queue"
          toLabel="Full queue"
        >
          {bands.actNow.length === 0 ? (
            <TileEmpty
              whatFills={
                tasksBound.rows.length === 0
                  ? 'No open tasks at your college. Checklist items are generated per program, ' +
                    'so an empty list here means either everything is done or no program has ' +
                    'been created yet.'
                  : 'Nothing is due today or overdue. Items appear here on the day they come due.'
              }
            />
          ) : (
            bands.actNow.slice(0, 5).map((t) => (
              <TileRow
                key={t.id}
                to={`/programs/${t.program_id}`}
                primary={t.title}
                secondary={t.programs?.name ?? undefined}
                trailing={<UrgencyPill urgency={deriveUrgency(t)} />}
              />
            ))
          )}
        </Tile>

        <Tile
          title="Unmarked this month"
          hint={`Elapsed days without a mark, ${window.key}`}
          to="/attendance"
          // Withheld figures do not get to tint the tile either. A tone is a
          // claim — 'alert' says act, 'neutral' says do not — and reading it
          // off `gapDays` while refusing to print `gapDays` would say the
          // quiet part in colour.
          tone={gapsComplete && gapDays > 0 ? 'alert' : 'neutral'}
        >
          {!gapsComplete ? (
            <TileEmpty whatFills="Not stated: the deployment or attendance read was cut at its row bound this month, and an unmarked-day count from a partial read is wrong in both directions. Open Attendance for the per-deployment truth." />
          ) : gaps.length === 0 ? (
            <TileEmpty
              whatFills={
                deployments.length === 0
                  ? 'No trainer is deployed at your college yet, so no day is expected to carry a mark.'
                  : `No deployment ran in ${window.key}. Marks are only expected on days a deployment is live.`
              }
            />
          ) : (
            <Metric
              value={gapDays}
              size="lg"
              tone={crtGapDays > 0 ? 'bad' : gapDays > 0 ? 'warn' : 'good'}
              label={gapDays === 1 ? 'day' : 'days'}
              caption={
                crtGapDays > 0
                  ? `${crtGapDays} on a CRT program, where an unmarked day is an unpaid day for the trainer.`
                  : gapDays > 0
                    ? 'All on bCAP. Those days pay regardless — mark them so the record is true.'
                    : 'The month is complete to date.'
              }
            />
          )}
        </Tile>

        <Tile title="Programs here" hint="At the college you cover" to="/board" toLabel="Console">
          {programs.length === 0 ? (
            <TileEmpty whatFills="No program has been set up for your college yet. Your Manager creates it, and its checklist appears in your queue the same moment." />
          ) : (
            <Metric
              value={programs.length}
              size="lg"
              label={programs.length === 1 ? 'program running' : 'programs running'}
              caption={programs
                .slice(0, 3)
                .map((p) => STAGE_LABEL[p.stage])
                .join(' · ')}
            />
          )}
        </Tile>

        <Tile
          title="Batches"
          hint="Cohorts on campus and their rosters"
          span={2}
          count={batches.length || undefined}
        >
          {batchesQuery.error ? (
            <p className="text-xs text-ink-3">{errorMessage(batchesQuery.error)}</p>
          ) : batches.length === 0 ? (
            <TileEmpty whatFills="No batches yet. They are added per program — a batch is a cohort plus its passout year, and students hang off it." />
          ) : (
            batches.slice(0, 6).map((b) => {
              const enrolled = studentsPerBatch.get(b.id) ?? 0
              return (
                <TileRow
                  key={b.id}
                  primary={`${b.name}${b.passout_year ? ` · ${b.passout_year}` : ''}`}
                  secondary={b.programs?.name ?? undefined}
                  trailing={
                    <span className="text-[11px] tabular-nums text-ink-2">
                      {enrolled > 0
                        ? `${enrolled}${rosterComplete ? '' : '+'} student${
                            enrolled === 1 && rosterComplete ? '' : 's'
                          }`
                        : b.expected_student_count
                          ? `${b.expected_student_count} expected`
                          : rosterComplete
                            ? 'roster empty'
                            : // Not "roster empty": the student read was cut,
                              // so an absent count here is a fact about the
                              // page size and not about the cohort.
                              'roster not loaded'}
                    </span>
                  }
                />
              )
            })
          )}
        </Tile>
      </BentoGrid>

      {commercialsHidden && (
        <InfoNote>
          Rates, payouts and P&amp;L are not on this screen and are not hidden behind a click —
          your role covers delivery, and the database returns no commercial rows to it at all.
          Attendance you mark here is what the payout engine later counts, which is why the
          unmarked-day tiles are the loud ones.
        </InfoNote>
      )}
    </div>
  )
}
