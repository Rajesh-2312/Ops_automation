import { Route, Routes } from 'react-router-dom'
import { PAGE, bounded, emptyBound } from '../lib/bounds'
import { useQuery } from '@tanstack/react-query'
import { supabase, errorMessage } from '../lib/supabase'
import { qk } from '../lib/queryKeys'
import {
  STAGE_LABEL,
  type CollegeAttendanceSummary,
  type CollegeGovernanceReport,
  type CollegeProgramProgress,
} from '../lib/types'
import { AppShell, Icons, NotFound, Page, PageHeader, type NavItem } from '../components/AppShell'
import {
  Badge,
  BoundNote,
  Card,
  EmptyState,
  ErrorNote,
  fmtDate,
  InfoNote,
  Loading,
  Meter,
  Td,
  Th,
} from '../components/ui'
import { BentoGrid, Metric, Tile, TileEmpty, TileRow } from '../components/bento'

/**
 * Two links, on the same section taxonomy as the ops console.
 *
 * Two headings over two links is more chrome than a two-item list strictly
 * needs. It is still the right shape: a college principal and a byteXL manager
 * who are on a call together are reading sidebars that name their sections the
 * same way, and this list only grows - published artifacts are the one thing
 * §4 lets out of the building, and there will be more of them than reports.
 *
 * No `badge` here on purpose. A count belongs where somebody has to act on it,
 * and this persona has no actions: the whole root is read-only, with no write
 * path anywhere in the file.
 */
const NAV: NavItem[] = [
  { to: '/', label: 'Overview', icon: Icons.home, end: true, group: 'Overview' },
  { to: '/progress', label: 'Progress detail', icon: Icons.chart, group: 'Programs' },
]

/**
 * The college's front door — read-only, and deliberately thin.
 *
 * THREE CURATED VIEWS, NEVER THE BASE TABLES:
 *   college_program_progress    — stage and checklist completion, as COUNTS
 *   college_attendance_summary  — STUDENT attendance per batch, aggregated
 *   college_governance_reports  — only reports actually shared
 *
 * That is not a stylistic choice. §4 grants the college aggregates and published
 * artifacts while denying it task ownership, student rosters and every cost, and
 * row-level RLS cannot express "you may see counts but not rows". So the views
 * do it: they are `security_invoker = false`, execute as their owner, and their
 * WHERE clause — `college_id = my_college_id()` — IS the security boundary,
 * with no second line of defence behind it.
 *
 * Two things are load-bearing and easy to undo by accident:
 *
 *   The attendance summary is sourced from `attendance_records` (students),
 *   NEVER from `trainer_attendance`. Trainer attendance is a payroll record
 *   about a byteXL contractor; an attendance % that moved when a trainer took
 *   half a day would leak exactly that, at any level of aggregation.
 *
 *   Governance reports appear only where `shared_with_college_at is not null`.
 *   The view enforces it and so does the base-table policy, which is what you
 *   want for the one artifact that actually leaves the building (R4 — nothing
 *   leaves the system unapproved). An unshared draft is invisible here to every
 *   persona, including internal staff reading this same view.
 *
 * There is NO WRITE PATH anywhere in this component, and no query that could
 * reach a trainer, a task or a rupee.
 */
export function CollegeRoot() {
  return (
    <AppShell nav={NAV}>
      <Routes>
        <Route path="/" element={<CollegeHome />} />
        <Route path="/progress" element={<CollegeProgress />} />
        {/* A real not-found rather than a silent bounce to Overview. This
            persona reaches the console through links byteXL sends them, so a
            stale one is the likeliest way they arrive at a bad URL - and
            landing on Overview with no explanation reads as "the report was
            withdrawn" rather than "that link has moved". */}
        <Route path="*" element={<NotFound />} />
      </Routes>
    </AppShell>
  )
}

/**
 * The college's overview.
 *
 * §4 gives this persona "published artifacts only, read-only", so the hero is
 * the reports byteXL has actually shared — that is the artifact, and it is the
 * only thing on the screen a principal would open. Everything else is context
 * for it and is sized accordingly.
 *
 * Same three curated views as the detail page, same keys, so opening the
 * overview warms it. No base table is touched here either, and there is still
 * no write path anywhere in this file.
 */
function CollegeHome() {
  // BOUNDED, at one shared page size across both screens in this file so the
  // overview warms the caches the detail page reads (the two use the same keys
  // on purpose).
  //
  // These are three curated VIEWS for one institution rather than base tables,
  // so they are the smallest lists in the app — but `college_governance_reports`
  // gains a row every reporting period per program and never loses one, which
  // is unbounded growth on a screen this persona will open for years.
  const programsQuery = useQuery({
    queryKey: qk.college.progress(PAGE.collegeViews),
    queryFn: () =>
      bounded<CollegeProgramProgress>(PAGE.collegeViews, (rows) =>
        supabase
          .from('college_program_progress')
          .select('*')
          .order('program_name')
          .limit(rows),
      ),
  })

  const attendanceQuery = useQuery({
    queryKey: qk.college.attendance(PAGE.collegeViews),
    queryFn: () =>
      bounded<CollegeAttendanceSummary>(PAGE.collegeViews, (rows) =>
        supabase
          .from('college_attendance_summary')
          .select('*')
          .order('batch_name')
          .limit(rows),
      ),
  })

  const reportsQuery = useQuery({
    queryKey: qk.college.reports(PAGE.collegeViews),
    queryFn: () =>
      bounded<CollegeGovernanceReport>(PAGE.collegeViews, (rows) =>
        supabase
          .from('college_governance_reports')
          .select('*')
          .order('shared_with_college_at', { ascending: false })
          .limit(rows),
      ),
  })

  const programsBound =
    programsQuery.data ?? emptyBound<CollegeProgramProgress>(PAGE.collegeViews)
  const programs = programsBound.rows
  const attendance = attendanceQuery.data?.rows ?? []
  const reports = reportsQuery.data?.rows ?? []

  // Roll-ups over a bounded list, so they are stated as "at least" when it was
  // cut rather than presented as the institution's totals.
  const students = programs.reduce((n, p) => n + p.student_count, 0)
  const batches = programs.reduce((n, p) => n + p.batch_count, 0)


  const failure = programsQuery.error ?? attendanceQuery.error ?? reportsQuery.error

  if (programsQuery.isPending)
    return (
      <>
        {/* The SAME title and purpose the loaded state uses. This header
            previously read "Overview" and then re-titled itself to "Your
            programs with byteXL" once the query landed — which reads, to a
            college that visits this page twice a term, as having arrived
            somewhere different from where they arrived last time. A heading
            that moves while you are reading it is worse than a slow one. */}
        <PageHeader
          title="Your programs with byteXL"
          purpose="Everything byteXL has formally shared with your institution, in one place: the reports we have published to you, how far each program has got, and aggregate attendance. Nothing here names an individual student."
        />
        <Page>
          <Loading label="Loading your programs" />
        </Page>
      </>
    )

  return (
    <>
      <PageHeader
        title="Your programs with byteXL"
        subtitle="Published reports and delivery status for your institution."
        purpose="Everything byteXL has formally shared with your institution, in one place: the reports we have published to you, how far each program has got, and aggregate attendance. Nothing here names an individual student."
      />

      <Page>
        <div className="space-y-4">
          {failure && <ErrorNote>{errorMessage(failure)}</ErrorNote>}

          {/* The student and batch totals below are sums over the programs that
              arrived, so a cut read understates an institution's own figures to
              that institution. */}
          <BoundNote
            bound={programsBound}
            noun="programs"
            derived="The student and batch totals are sums over those programs, so they are floors rather than totals."
          />
          <BoundNote bound={reportsQuery.data} noun="shared reports" />

          <BentoGrid>
            <Tile
              title="Reports shared with you"
              hint="Governance reports byteXL has published"
              span={2}
              rows={2}
              count={reports.length || undefined}
            >
              {reports.length === 0 ? (
                <TileEmpty whatFills="Nothing published yet. A governance report appears here only once byteXL explicitly shares it — a report still in draft is invisible to everyone, including byteXL's own staff on this view." />
              ) : (
                reports.map((r) => (
                  <TileRow
                    key={r.id}
                    primary={
                      <a
                        href={r.url}
                        target="_blank"
                        rel="noreferrer"
                        className="text-accent hover:underline"
                      >
                        {r.title || 'Governance report'} ↗
                      </a>
                    }
                    secondary={`${r.program_name} · shared ${fmtDate(r.shared_with_college_at)}`}
                  />
                ))
              )}
            </Tile>

            <Tile
              title="Programs running"
              hint="Delivery stage and checklist completion"
              span={2}
              count={programs.length || undefined}
              to="/progress"
              toLabel="Detail"
            >
              {programs.length === 0 ? (
                <TileEmpty whatFills="Once byteXL sets up a program for your institution it appears here, with its stage and how far through its checklist it is." />
              ) : (
                programs.slice(0, 5).map((p) => (
                  <TileRow
                    key={p.program_id}
                    primary={p.program_name}
                    secondary={STAGE_LABEL[p.stage]}
                    trailing={
                      <span className="flex items-center gap-2 w-28">
                        <Meter pct={p.checklist_complete_pct} />
                        <span className="text-[11px] tabular-nums text-ink-3 shrink-0">
                          {p.checklist_complete_pct == null
                            ? '—'
                            : `${p.checklist_complete_pct}%`}
                        </span>
                      </span>
                    }
                  />
                ))
              )}
            </Tile>

            <Tile title="Cohorts" hint="Batches and enrolled students">
              {batches === 0 ? (
                <TileEmpty whatFills="Batches appear once your programs are set up and rosters are loaded." />
              ) : (
                <Metric
                  value={students}
                  size="lg"
                  label={students === 1 ? 'student' : 'students'}
                  caption={`across ${batches} batch${batches === 1 ? '' : 'es'}`}
                />
              )}
            </Tile>

            <Tile title="Attendance" hint="Student sessions, aggregate" to="/progress">
              {attendance.length === 0 ? (
                <TileEmpty whatFills="Session attendance appears here once training begins. It is always an aggregate — no individual student is named on this view." />
              ) : (
                <Metric
                  value={`${attendance.length}`}
                  size="lg"
                  label={attendance.length === 1 ? 'batch recorded' : 'batches recorded'}
                  caption="Per-batch percentages are on the detail page."
                />
              )}
            </Tile>
          </BentoGrid>

          <InfoNote>
            Read-only by design, and deliberately narrow: this view carries published artifacts
            and aggregate delivery status. It shows nothing about individual students, nothing
            about the trainers byteXL deploys, and no costs.
          </InfoNote>
        </div>
      </Page>
    </>
  )
}

function CollegeProgress() {
  // Same keys and same bound as CollegeHome, so opening one warms the other.
  const programsQuery = useQuery({
    queryKey: qk.college.progress(PAGE.collegeViews),
    queryFn: () =>
      bounded<CollegeProgramProgress>(PAGE.collegeViews, (rows) =>
        supabase
          .from('college_program_progress')
          .select('*')
          .order('program_name')
          .limit(rows),
      ),
  })

  const attendanceQuery = useQuery({
    queryKey: qk.college.attendance(PAGE.collegeViews),
    queryFn: () =>
      bounded<CollegeAttendanceSummary>(PAGE.collegeViews, (rows) =>
        supabase
          .from('college_attendance_summary')
          .select('*')
          .order('batch_name')
          .limit(rows),
      ),
  })

  const reportsQuery = useQuery({
    queryKey: qk.college.reports(PAGE.collegeViews),
    queryFn: () =>
      bounded<CollegeGovernanceReport>(PAGE.collegeViews, (rows) =>
        supabase
          .from('college_governance_reports')
          .select('*')
          .order('shared_with_college_at', { ascending: false })
          .limit(rows),
      ),
  })

  const programsBound =
    programsQuery.data ?? emptyBound<CollegeProgramProgress>(PAGE.collegeViews)
  const programs = programsBound.rows
  const attendance = attendanceQuery.data?.rows ?? []
  const reports = reportsQuery.data?.rows ?? []

  const failure = programsQuery.error ?? attendanceQuery.error ?? reportsQuery.error

  return (
    <>
      <PageHeader
        title="Program progress"
        subtitle="Delivery status for your institution."
        purpose="Program by program: which stage it has reached, how much of its setup checklist is done, and what proportion of scheduled sessions students attended."
      />

      <Page>
        {failure && (
          <div className="mb-4">
            <ErrorNote>{errorMessage(failure)}</ErrorNote>
          </div>
        )}

        <div className="mb-4 space-y-2">
          <BoundNote bound={programsBound} noun="programs" />
          <BoundNote bound={attendanceQuery.data} noun="batch attendance rows" />
          <BoundNote bound={reportsQuery.data} noun="shared reports" />
        </div>

        {programsQuery.isPending ? (
          <Loading label="Loading your programs" />
        ) : programs.length === 0 ? (
          <Card>
            <EmptyState
              title="No programs yet"
              body="Once byteXL sets up a program for your institution, its progress appears here."
            />
          </Card>
        ) : (
          <div className="max-w-4xl space-y-6">
            {/* --- Programs ------------------------------------------------ */}
            <section>
              <h2 className="text-sm font-semibold text-ink mb-3">Programs</h2>
              <div className="grid sm:grid-cols-2 gap-3">
                {programs.map((p) => (
                  <Card key={p.program_id} className="p-4">
                    <div className="flex items-start justify-between gap-2">
                      <h3 className="text-sm font-medium text-ink leading-snug">
                        {p.program_name}
                      </h3>
                      <Badge tone="accent">{p.program_type}</Badge>
                    </div>
                    <p className="text-xs text-ink-2 mt-1">{STAGE_LABEL[p.stage]}</p>

                    <div className="mt-3.5 flex items-center gap-2">
                      <Meter pct={p.checklist_complete_pct} />
                      <span className="text-[11px] tabular-nums text-ink-3 shrink-0">
                        {p.checklist_complete_pct == null
                          ? '—'
                          : `${p.checklist_complete_pct}%`}
                      </span>
                    </div>

                    <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 mt-4 text-xs">
                      <Row label="Batches" value={String(p.batch_count)} />
                      <Row label="Students" value={String(p.student_count)} />
                      <Row label="Starts" value={fmtDate(p.start_date)} />
                      <Row label="Ends" value={fmtDate(p.end_date)} />
                    </dl>
                  </Card>
                ))}
              </div>
            </section>

            {/* --- Attendance ---------------------------------------------- */}
            <section>
              <h2 className="text-sm font-semibold text-ink mb-3">Attendance summary</h2>
              <Card className="overflow-hidden">
                {attendance.length === 0 ? (
                  <EmptyState
                    title="No attendance recorded yet"
                    body="Session attendance appears here once training begins."
                  />
                ) : (
                  <div className="overflow-x-auto scroll-slim">
                    <table className="w-full text-sm">
                      <thead>
                        <tr className="border-b border-line">
                          <Th>Batch</Th>
                          <Th>Branch</Th>
                          <Th>Sessions</Th>
                          <Th>Period</Th>
                          <Th>Attendance</Th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-line-soft">
                        {attendance.map((a) => (
                          <tr key={a.batch_id}>
                            <Td className="font-medium text-ink">{a.batch_name}</Td>
                            <Td className="text-ink-2">{a.branch || '—'}</Td>
                            <Td className="tabular-nums text-ink-2">{a.sessions_recorded}</Td>
                            <Td className="text-ink-2 whitespace-nowrap">
                              {a.first_session
                                ? `${fmtDate(a.first_session)} → ${fmtDate(a.last_session)}`
                                : '—'}
                            </Td>
                            <Td className="w-40">
                              {a.attendance_pct == null ? (
                                <span className="text-ink-3">—</span>
                              ) : (
                                <div className="flex items-center gap-2">
                                  <Meter pct={a.attendance_pct} />
                                  <span className="text-xs tabular-nums text-ink-2 shrink-0">
                                    {a.attendance_pct}%
                                  </span>
                                </div>
                              )}
                            </Td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </Card>
              <p className="text-xs text-ink-3 mt-2">
                Student session attendance, per batch. Aggregate only — no individual student
                appears here, and nothing on this page reflects trainer attendance.
              </p>
            </section>

            {/* --- Governance reports -------------------------------------- */}
            <section>
              <h2 className="text-sm font-semibold text-ink mb-3">Governance reports</h2>
              <Card>
                {reports.length === 0 ? (
                  <EmptyState
                    title="No reports shared yet"
                    body="Reports appear here only once byteXL publishes them to you."
                  />
                ) : (
                  <ul className="divide-y divide-line-soft">
                    {reports.map((r) => (
                      <li
                        key={r.id}
                        className="flex items-center justify-between gap-4 px-4 py-3"
                      >
                        <div className="min-w-0">
                          <p className="text-sm font-medium text-ink truncate">
                            {r.title || 'Governance report'}
                          </p>
                          <p className="text-xs text-ink-3 mt-0.5">
                            {r.program_name} · shared {fmtDate(r.shared_with_college_at)}
                          </p>
                        </div>
                        <a
                          href={r.url}
                          target="_blank"
                          rel="noreferrer"
                          className="text-sm font-medium text-accent hover:underline shrink-0"
                        >
                          Open ↗
                        </a>
                      </li>
                    ))}
                  </ul>
                )}
              </Card>
            </section>

            <InfoNote>
              This view is read-only by design. Only reports byteXL has explicitly shared with
              you are listed — a drafted report is not visible here to anyone.
            </InfoNote>
          </div>
        )}
      </Page>
    </>
  )
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <>
      <dt className="text-ink-3">{label}</dt>
      <dd className="text-ink text-right tabular-nums">{value}</dd>
    </>
  )
}
