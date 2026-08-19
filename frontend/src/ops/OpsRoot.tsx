import { Suspense, lazy } from 'react'
import { Route, Routes } from 'react-router-dom'
import { AppShell, Icons, NotFound, PageSkeleton, type NavItem } from '../components/AppShell'
import { useAuth } from '../auth/AuthProvider'

/* ==========================================================================
   ROUTE-LEVEL CODE SPLITTING
   ==========================================================================

   Every screen below is a `lazy()` import, so Rollup emits one chunk per route
   and the browser downloads a screen the first time somebody navigates to it.

   WHY IT IS WORTH DOING HERE SPECIFICALLY. This console has seventeen screens
   and no persona uses all of them. `CommsPage` is 1,674 lines and `ReportsPage`
   is 1,112; an LDE Executive cannot use `/payouts` or `/work-orders` at all,
   because `can_see_commercials()` returns false for them and the endpoints 403
   before reading a row (§4). Shipping those bytes to that persona is paying the
   download cost of a page they are not permitted to see.

   Measured on this repo, not estimated: one 818 kB / 230 kB-gzip chunk before,
   and after the split a first load of 164.57 kB gzip for a Manager landing on
   Home — 28.6% less. The full chunk table and the method are in
   `vite.config.ts`, next to the vendor split that produced the other half.

   THIS IS NOT A SECURITY BOUNDARY AND MUST NEVER BE MISTAKEN FOR ONE. A chunk
   that is not downloaded is not a permission: `/payouts` stays REGISTERED for
   every internal persona below, the chunk is fetched the moment the URL is
   typed, and the page then draws its own honest explanation. The wall is in
   Postgres (R5). Splitting changes WHEN bytes arrive, never WHO may read rows.

   `lazy()` wants a module with a default export and every screen here is a
   named export, so each import maps the name onto `default`. Renaming an
   export therefore breaks at runtime rather than at compile time — which is
   the one real cost of this pattern and the reason the list is kept flat,
   alphabetical-by-route and adjacent to the <Route> table that uses it.
   ========================================================================== */

const HomePage = lazy(() => import('./HomePage').then((m) => ({ default: m.HomePage })))
const WorkQueue = lazy(() => import('./WorkQueue').then((m) => ({ default: m.WorkQueue })))
const ProgramConsole = lazy(() =>
  import('./ProgramConsole').then((m) => ({ default: m.ProgramConsole })),
)
const ProgramDetail = lazy(() =>
  import('./ProgramDetail').then((m) => ({ default: m.ProgramDetail })),
)
const CollegesPage = lazy(() =>
  import('./CollegesPage').then((m) => ({ default: m.CollegesPage })),
)
const TrainersPage = lazy(() =>
  import('./TrainersPage').then((m) => ({ default: m.TrainersPage })),
)
const ApprovalsPage = lazy(() =>
  import('./ApprovalsPage').then((m) => ({ default: m.ApprovalsPage })),
)
const DeploymentsPage = lazy(() =>
  import('./DeploymentsPage').then((m) => ({ default: m.DeploymentsPage })),
)
const AttendancePage = lazy(() =>
  import('./AttendancePage').then((m) => ({ default: m.AttendancePage })),
)
const WorkOrdersPage = lazy(() =>
  import('./WorkOrdersPage').then((m) => ({ default: m.WorkOrdersPage })),
)
const PayoutsPage = lazy(() => import('./PayoutsPage').then((m) => ({ default: m.PayoutsPage })))
const UsersPage = lazy(() => import('./UsersPage').then((m) => ({ default: m.UsersPage })))
const CopilotPage = lazy(() => import('./CopilotPage').then((m) => ({ default: m.CopilotPage })))
const AlertsPage = lazy(() => import('./AlertsPage').then((m) => ({ default: m.AlertsPage })))
const ReportsPage = lazy(() => import('./ReportsPage').then((m) => ({ default: m.ReportsPage })))
const CommsPage = lazy(() => import('./CommsPage').then((m) => ({ default: m.CommsPage })))
const ErmSyncPage = lazy(() => import('./ErmSyncPage').then((m) => ({ default: m.ErmSyncPage })))

/**
 * What a route shows while its chunk is in flight.
 *
 * This used to be the app's shared `Loading` spinner in an empty `Page`, on the
 * argument that one loading language is better than two. The argument was right
 * and the conclusion was wrong: a centred spinner on a blank column is the same
 * picture as a screen that loaded and returned nothing, and on a console where
 * an empty table is a REAL and frequent answer - RLS reach, an unassigned
 * Manager, a month with no payouts - those two must not look alike.
 *
 * `PageSkeleton` is shaped like the screen that is arriving: a header bar and
 * rows. It says "this is coming and here is its shape" rather than "something
 * is happening somewhere", and nothing jumps when the real screen replaces it.
 * Each page keeps its own labelled `Loading` for its first fetch, which is a
 * different wait about different data.
 *
 * The label stays generic. Naming the destination would mean a per-route
 * fallback, read for the ~100 ms before the screen's own label replaces it.
 */
function RouteFallback() {
  return <PageSkeleton label="Loading screen" />
}

/**
 * The internal console — shared by Senior Manager, Manager and LDE Executive.
 *
 * The three personas differ in REACH, not in screens: a Senior Manager covers
 * clusters, a Manager covers colleges, an LDE Executive covers the campus they
 * are assigned to, and all three resolve through `my_college_ids()` in
 * Postgres. So the same components serve all three and simply return fewer rows
 * for a narrower persona. There is no client-side filtering anywhere in this
 * tree, on purpose: a filter written in JS is a second copy of a rule that
 * already exists in SQL, and the copy is the one that drifts.
 *
 * The single exception is the COMMERCIALS nav item below, and it is cosmetic.
 * See the comment on the nav.
 *
 * Phase 1 has no AI in it (CLAUDE.md §13). There is deliberately no copilot, no
 * "summarise", no draft button anywhere in this console.
 */
/**
 * Deployment icon — a person joined to a place. Defined here rather than in
 * `Icons` because every entry there is already spoken for by another nav item,
 * and two links sharing a glyph is worse than one local SVG.
 */
const deploymentIcon = (
  <svg width={17} height={17} viewBox="0 0 24 24" fill="none" aria-hidden>
    <circle
      cx="8"
      cy="7.5"
      r="3"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
    />
    <path
      d="M3 20v-1.5A3.5 3.5 0 016.5 15h3"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
    />
    <path
      d="M13 12h8v8h-8z"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
    />
    <path
      d="M15.5 16h3"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
    />
  </svg>
)

export function OpsRoot() {
  const { canSeeCommercials } = useAuth()

  // Home first, then the queue, then the board.
  //
  // The queue used to be the landing screen, and its own comment argued the
  // right thing for the wrong scope: the board is not what you want at 9am, but
  // neither is a flat list of tasks, and the three personas do not open this
  // console asking the same question at all (see HomePage's header comment). So
  // the queue keeps its job — "what is on me, in one list" — at its own URL, and
  // the home answers the persona's first question and hands off to it.
  //
  // GROUPED IN THE ORDER THE WORK FLOWS, not alphabetically and not by how
  // often a screen is opened. Fifteen flat links made a newcomer read all
  // fifteen to find the two that are theirs this morning, and gave no clue that
  // Attendance feeds Payouts or that Approvals is the gate between them. The
  // six headings are the pipeline, said out loud:
  //
  //   Overview   what is on me right now
  //   Programs   the records - who we sell to, what we sell, who delivers it
  //   Delivery   the daily campus work, and what went wrong with it
  //   Money      what it costs and who signs it off
  //   Assist     things that help with the job rather than being the job
  //   Admin      who may do any of the above
  //
  // The grouping is presentation only. WHICH links exist is still decided by
  // `canSeeCommercials` below and, underneath that, by RLS - a heading grants
  // and withholds nothing.
  const nav: NavItem[] = [
    { to: '/', label: 'Home', icon: Icons.home, end: true, group: 'Overview' },
    { to: '/queue', label: 'Work queue', icon: Icons.check, group: 'Overview' },

    { to: '/board', label: 'Program Console', icon: Icons.board, group: 'Programs' },
    { to: '/colleges', label: 'Colleges', icon: Icons.building, group: 'Programs' },
    // Trainers sits with the records rather than with Delivery: this screen is
    // the educator's identity - PAN, bank, work-order history - and PAN is the
    // only key that survives across every legacy sheet (CLAUDE.md section 6).
    // WHERE a trainer is currently working is Deployments, one group down.
    { to: '/trainers', label: 'Trainers', icon: Icons.badge, group: 'Programs' },

    { to: '/attendance', label: 'Attendance', icon: Icons.calendar, group: 'Delivery' },
    // NOT gated. A deployment carries no money — 0400 kept the engagement rate
    // off this table precisely so the campus persona could be given the whole
    // row — and `deployments_internal_all` is `for all` to every internal
    // persona, narrowed by reach. An LDE Executive needs it: attendance is
    // marked per deployment, and it is their daily job.
    { to: '/deployments', label: 'Deployments', icon: deploymentIcon, group: 'Delivery' },
    // COSMETIC GATE. Work orders carry the engagement rate, so §4 puts them
    // behind can_see_commercials() — Senior Manager and Manager only. Removing
    // the link keeps an LDE Executive from walking into a page that would be
    // empty for them; it is NOT what stops them reading the rates. The
    // work_orders policy does that, in the database, and it would return zero
    // rows even if this line were deleted.
    // The same cosmetic gate covers Payouts, and more strictly: a payout is a
    // rate, an earned figure and a payment instruction at once, and all four
    // /payouts endpoints 403 an LDE Executive before they read a row.
    ...(canSeeCommercials
      ? [
          { to: '/work-orders', label: 'Work orders', icon: Icons.rupee, group: 'Money' },
          { to: '/payouts', label: 'Payouts', icon: Icons.doc, group: 'Money' },
        ]
      : []),
    // NOT gated, unlike the two above. The only artifact type that can be
    // approved today is a remuneration sheet, which is commercial — but the
    // approval LIFECYCLE is not itself commercial, and `artifact_versions`
    // carries two policies (1300): the commercial rows are walled, the
    // governance and operational-document rows are not. An LDE Executive
    // therefore gets a real, correctly-scoped queue rather than an empty page,
    // and hiding the link would misdescribe R4 as a money feature.
    { to: '/approvals', label: 'Approvals', icon: Icons.check, group: 'Money' },
    // NOT gated. The Delivery Monitor produces day counts, absence rates,
    // syllabus percentages and learner counts — audited field by field, no
    // alert carries a monetary figure, which is why /monitoring calls no
    // require_commercials(). Gating this would take the page away from the
    // persona who needs it most: the LDE Executive is the one on campus who
    // can act on an attendance gap the morning it appears.
    { to: '/alerts', label: 'Alerts', icon: Icons.bell, group: 'Delivery' },
    // Phase 3 and the only generated surface here. Read-only, cited, and it
    // refuses structured facts by design (§9) — the screen says so before the
    // input rather than after a refusal.
    { to: '/copilot', label: 'Ops Copilot', icon: Icons.chat, group: 'Assist' },
    // NOT gated. A comms draft is a message to a college, not a number.
    { to: '/comms', label: 'Comms queue', icon: Icons.mail, group: 'Assist' },
    // NOT gated. Pasting a field pack into ERM is campus-side onboarding work,
    // and the pack's contents are already whatever RLS lets the caller read.
    { to: '/erm', label: 'ERM sync', icon: Icons.sync, group: 'Assist' },
    // NOT gated, for the same reason Approvals is not. A governance report may
    // carry P&L, but the commercial SECTION is what the wall covers — the page
    // requests it only for a commercials persona and the API refuses it for
    // anyone else. A college summary is legitimately an LDE Executive's work.
    { to: '/reports', label: 'Reports', icon: Icons.chart, group: 'Assist' },

    { to: '/users', label: 'Users & roles', icon: Icons.users, group: 'Admin' },
  ]

  // ON `badge`. `NavItem` now carries an optional pending count and the shell
  // renders it, but nothing here sets one, and that is a decision rather than
  // an omission. Work queue, Approvals and Comms are the three links that would
  // want a count, and OpsRoot holds no query - every figure in this console is
  // fetched by the page that shows it. A count here would mean a query running
  // on every screen in order to decorate a link, and a number on the nav that
  // silently disagrees with the number on the page as soon as either goes
  // stale. Wire it when a shared, already-fetched count exists to read.

  return (
    <AppShell nav={nav}>
      {/* One Suspense boundary around the whole route table rather than one
          per route: they are siblings, only one is ever mounted at a time, and
          seventeen boundaries would render identically while making a
          navigation that touches two chunks flash twice. */}
      <Suspense fallback={<RouteFallback />}>
        <Routes>
        <Route path="/" element={<HomePage />} />
        <Route path="/queue" element={<WorkQueue />} />
        <Route path="/board" element={<ProgramConsole />} />
        <Route path="/programs/:id" element={<ProgramDetail />} />
        <Route path="/attendance" element={<AttendancePage />} />
        <Route path="/colleges" element={<CollegesPage />} />
        <Route path="/trainers" element={<TrainersPage />} />
        <Route path="/deployments" element={<DeploymentsPage />} />
        {/* Registered for every internal persona. The page draws its own wall:
            it explains who may approve rather than 404ing or silently hiding,
            because "why can't I approve this?" is a question the screen should
            answer (CLAUDE.md R4, and §14 Q3 which is still open). */}
        <Route path="/approvals" element={<ApprovalsPage />} />
        {/* The route is registered for every internal persona even though the
            link is hidden. An LDE Executive who types the URL gets an empty
            table and an honest note, which is a better failure than a 404 that
            implies the page does not exist. */}
        <Route path="/work-orders" element={<WorkOrdersPage />} />
        {/* Registered for every internal persona for the same reason, but the
            page itself draws no form for a persona the API would refuse —
            offering a button whose only outcome is a 403 is worse than saying
            plainly that the data is not theirs. */}
        <Route path="/payouts" element={<PayoutsPage />} />
        {/* Phase 5. Runs the Delivery Monitor and Escalation Engine on
            request; both are deterministic, and the escalation rules are SLA
            arithmetic rather than LLM judgement (§8). Alerts are internal —
            nothing here notifies an external party. */}
        <Route path="/alerts" element={<AlertsPage />} />
        {/* Phase 6. Draft is the ceiling: no send, share or publish control
            exists on this screen, and the publish gate is not reachable from
            any reporting endpoint. */}
        <Route path="/reports" element={<ReportsPage />} />
        {/* Phase 4. Approve, reject and release all answer 501 today because
            §14 Q3 — who may approve college-facing comms — is unanswered by
            the owner. The screen explains that rather than hiding it, and the
            buttons stay live so answering Q3 needs no frontend edit. */}
        <Route path="/comms" element={<CommsPage />} />
        {/* §10. ERM has no API and this is not a scraper: the system generates
            a field pack, a named human pastes it, that human confirms. Drift
            detection is the load-bearing half — editing a watched column on a
            synced record flips it stale and requeues a fresh card. */}
        <Route path="/erm" element={<ErmSyncPage />} />
        {/* Phase 3. RAG Q&A, read-only, every answer cited or refused. */}
        <Route path="/copilot" element={<CopilotPage />} />
        <Route path="/users" element={<UsersPage />} />
        {/* Was `<Navigate to="/" />`. A silent redirect and a working link
            are indistinguishable to somebody who followed a stale bookmark or
            mistyped a program id: they arrive on Home having been told nothing
            and conclude the record was deleted. `NotFound` names the path it
            could not match and hands back a way home. */}
        <Route path="*" element={<NotFound />} />
        </Routes>
      </Suspense>
    </AppShell>
  )
}
