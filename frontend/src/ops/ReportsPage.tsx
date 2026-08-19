import { useMemo, useState } from 'react'
import { PAGE, bounded } from '../lib/bounds'
import { useMutation, useQuery } from '@tanstack/react-query'
import { supabase, errorMessage } from '../lib/supabase'
import { qk } from '../lib/queryKeys'
import { STAGE_LABEL, type College, type Program, type ProgramStage } from '../lib/types'
import {
  FIGURES_NOTE,
  NARRATIVE_NOTE,
  NOTHING_SENDS_NOTE,
  REPORT_APPROVAL_UNDECIDED_NOTE,
  draftGovernanceReport,
  fetchCollegeSummary,
  fetchProgramFeedback,
  isDraftRefused,
  isForbidden,
  isNarrationUnavailable,
  isNotFound,
  reportKeys,
  type ArtifactState,
  type FeedbackSynthesis,
  type GovernanceDraftResult,
  type ReportApproval,
  type ReportNarrative,
  type TrainerCost,
} from '../lib/reports'
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
  Input,
  MonoValue,
  PageIntro,
  SectionTitle,
  Select,
  Spinner,
  Stat,
  TableSkeleton,
  Tabs,
  Td,
  Th,
  Toolbar,
} from '../components/ui'

/* --------------------------------------------------------------------------
   Reports — Phase 6 (CLAUDE.md §8), and the screen that makes it operable.

   `app/services/reporting/` and `app/api/reports.py` have existed with no
   caller. Three endpoints: a governance report for one program-period, a
   feedback synthesis, and a summary of every program at one college. All three
   produce a DRAFT and nothing else, and this screen is built so that the ceiling
   is legible rather than merely respected.

   FOUR THINGS HERE ARE STRUCTURAL, NOT COSMETIC.

   1. FIGURES AND PROSE ARE DRAWN AS TWO DIFFERENT KINDS OF THING (R1). The
      counts, the attendance percentage, the feedback scores and the trainer
      net-pay lines are laid out as data — stat tiles and tables — and every
      panel of them carries the sentence that they were SELECTed. The generated
      narrative sits in its own card, visually distinct, labelled as generated,
      with the model and token cost of the call that produced it printed beside
      it. That separation exists on the wire (`GovernanceReportOut.narrative` is
      a sibling of the figures, not a rendering of them) and this file preserves
      it. Nothing on this screen reads a number out of `narrative.body`.

   2. THERE IS NO SEND. Not a share button, not a "publish to college", not a
      mailto. §8 caps Reporting at Draft and R4 gives every artifact the same
      one-way path with a human at each step. The publish gate,
      `governance_reports.shared_with_college_at`, is not writable from any
      reporting endpoint, so a button here would have nothing to call.

   3. §14 Q3 IS SHOWN AS AN OPEN QUESTION, NOT AS A FAILURE. A governance report
      has no approval authority because nobody has decided who signs a
      college-facing artifact off. The API answers that in the draft's own
      `approval` block — `can_be_approved: false` plus the reason — and the
      Approvals screen would answer 501. Both are rendered here in the same
      sky-toned "the system has no answer yet" panel that `ApprovalsPage` uses,
      never as a red error.

   4. THE COMMERCIALS CHECKBOX IS A COSMETIC GATE, AND IS LABELLED AS ONE. It is
      drawn only for a persona that can hold commercial rows, exactly as OpsRoot
      hides the Payouts nav item — and exactly as there, that is not what stops
      an LDE Executive reading trainer cost. `require_commercials()` refuses the
      section in `reports.py` before a remuneration row is read, and the RLS
      policies would return zero rows underneath it. There is no client-side
      filtering of any figure in this file.

   NO MONEY IS COMPUTED HERE (R2/R7). Net pay arrives as a string and is rendered
   by `fmtAmount`, which groups digits on the string and never parses it. The
   trainer-cost section has no total, because the API sends `total: null` on
   purpose.
-------------------------------------------------------------------------- */

// --- Dates -------------------------------------------------------------------
// Built by hand, never through toISOString(), which converts to UTC and would
// slide an IST date back a day — the same reason Attendance and Payouts do it
// this way. A reporting period is a Postgres DATE and must be the calendar day
// the human picked.

const pad = (n: number) => String(n).padStart(2, '0')
const ymd = (year: number, month0: number, day: number) => `${year}-${pad(month0 + 1)}-${pad(day)}`

function monthBounds(monthsAgo: number): { start: string; end: string } {
  const now = new Date()
  const first = new Date(now.getFullYear(), now.getMonth() - monthsAgo, 1)
  const last = new Date(first.getFullYear(), first.getMonth() + 1, 0).getDate()
  return {
    start: ymd(first.getFullYear(), first.getMonth(), 1),
    end: ymd(first.getFullYear(), first.getMonth(), last),
  }
}

type ReportTab = 'governance' | 'feedback' | 'college'

type ProgramOption = Program & { colleges: { name: string } | null }

export function ReportsPage() {
  const { isInternal, canSeeCommercials } = useAuth()

  const [tab, setTab] = useState<ReportTab>('governance')
  const thisMonth = useMemo(() => monthBounds(0), [])
  const [periodStart, setPeriodStart] = useState(thisMonth.start)
  const [periodEnd, setPeriodEnd] = useState(thisMonth.end)
  const [programId, setProgramId] = useState('')
  const [collegeId, setCollegeId] = useState('')
  const [includeTrainerCost, setIncludeTrainerCost] = useState(false)
  const [includeNarrative, setIncludeNarrative] = useState(false)

  const periodValid = periodStart !== '' && periodEnd !== '' && periodEnd >= periodStart

  const programs = useQuery({
    queryKey: qk.programs.list(PAGE.programs),
    enabled: isInternal,
    queryFn: () =>
      bounded<ProgramOption>(PAGE.programs, (rows) =>
        supabase.from('programs').select('*, colleges(name)').order('name').limit(rows),
      ),
  })

  const colleges = useQuery({
    queryKey: qk.colleges.list(PAGE.colleges),
    enabled: isInternal,
    queryFn: () =>
      bounded<College>(PAGE.colleges, (rows) =>
        supabase.from('colleges').select('*').order('name').limit(rows),
      ),
  })

  /**
   * The governance draft. A mutation rather than a query because it is a POST
   * that writes an audit row and may spend a frontier-tier model call — it must
   * happen when somebody asks for it, never on render or on a cache miss.
   */
  const governance = useMutation<GovernanceDraftResult, unknown, void>({
    mutationFn: () =>
      draftGovernanceReport(programId, {
        period_start: periodStart,
        period_end: periodEnd,
        include_trainer_cost: includeTrainerCost,
        include_narrative: includeNarrative,
      }),
  })

  const feedback = useQuery({
    queryKey: reportKeys.feedback(programId, periodStart, periodEnd, includeNarrative),
    enabled: isInternal && tab === 'feedback' && programId !== '' && periodValid,
    retry: false,
    queryFn: () =>
      fetchProgramFeedback(programId, { periodStart, periodEnd, includeNarrative }),
  })

  const summary = useQuery({
    queryKey: reportKeys.collegeSummary(collegeId, periodStart, periodEnd, includeNarrative),
    enabled: isInternal && tab === 'college' && collegeId !== '' && periodValid,
    retry: false,
    queryFn: () => fetchCollegeSummary(collegeId, { periodStart, periodEnd, includeNarrative }),
  })

  // --- The wall ------------------------------------------------------------
  // All three endpoints call `require_internal()` before they read a row, so a
  // trainer or a college login would be refused whatever they picked. Drawing
  // the pickers for them would be offering an action that cannot work.
  if (!isInternal) {
    return (
      <>
        <PageHeader
          title="Reports"
          purpose="Where byteXL staff assemble the write-ups that get discussed in a governance meeting. Your account is not internal staff, so there is nothing for you to assemble here."
          subtitle="Internal staff only"
        />
        <Page>
          <div className="max-w-3xl">
            <InfoNote>
              Governance reports, feedback synthesis and college summaries are internal working
              drafts. Every endpoint behind this screen checks for internal staff before it reads
              a row, so there is deliberately no picker here rather than one that would refuse
              every selection you made. A college sees a report once it has been formally
              released to it — and nobody has yet decided who is allowed to sign that release
              off, so no report has been released to anybody.
            </InfoNote>
          </div>
        </Page>
      </>
    )
  }

  return (
    <>
      <PageHeader
        title="Reports"
        purpose="Pull one program's delivery record for a period into a write-up you can take to a meeting — how many batches ran, how much was marked, what the feedback said. Everything you make here is a draft nobody outside byteXL can see."
        subtitle="Figures are read from the database. Any prose is written by a model and is marked in orange wherever it appears."
      />

      <Page>
        <div className="space-y-4">
          <PageIntro
            steps={[
              'Pick a period',
              'Pick a program or college',
              'Assemble the figures',
              'Optionally have a model write the covering prose',
              'Edit it yourself, elsewhere',
            ]}
          >
            <p>
              A report here is made of two different kinds of thing and they are drawn
              differently on purpose. The <strong className="font-medium text-ink">figures</strong>{' '}
              — batch counts, attendance, feedback scores, net pay — were read straight out of
              the database and no model touched them. The{' '}
              <strong className="font-medium text-ink">narrative</strong> is wording a model
              produced from those figures, and everything generated carries an orange rail and a
              &ldquo;Generated&rdquo; label so you never have to guess which you are reading.
            </p>
            <p className="mt-2">
              Everything this screen makes is a{' '}
              <HelpTip term="draft — not a release">
                Two different acts, deliberately kept apart. Drafting produces a working
                document that lives inside byteXL. Releasing is what puts a document in front of
                a college, and it is a separate, human, audited step taken on the Approvals
                screen — never here. There is no button on this page that shows anything to
                anybody outside the company, because no such button exists anywhere in this
                console for a report.
              </HelpTip>
              . Assembling one is not a page load: it reads the period's records and logs who
              asked for it.
            </p>
          </PageIntro>

          <CeilingNote />

          <PeriodControls
            periodStart={periodStart}
            periodEnd={periodEnd}
            onStart={setPeriodStart}
            onEnd={setPeriodEnd}
            valid={periodValid}
            includeNarrative={includeNarrative}
            onNarrative={setIncludeNarrative}
          />

          <Tabs<ReportTab>
            tabs={[
              { id: 'governance', label: 'Governance report' },
              { id: 'feedback', label: 'Feedback synthesis' },
              { id: 'college', label: 'College summary' },
            ]}
            active={tab}
            onChange={setTab}
          />

          {tab === 'governance' && (
            <div className="space-y-4">
              <Card className="p-4">
                <SectionTitle
                  title="Assemble a governance report"
                  subtitle="One program, one period. What comes back is a draft this screen can neither approve nor release."
                />
                <div className="grid gap-3 sm:grid-cols-2">
                  <Field
                    label="Program"
                    hint="Only the programs your college and cluster assignments reach are listed — and the API re-checks that reach before it reads a row."
                  >
                    <Select value={programId} onChange={(e) => setProgramId(e.target.value)}>
                      <option value="">Select a program…</option>
                      {(programs.data?.rows ?? []).map((p) => (
                        <option key={p.id} value={p.id}>
                          {p.colleges?.name ?? 'Unknown college'} · {p.name} ({p.type})
                        </option>
                      ))}
                    </Select>
                    <BoundNote bound={programs.data} noun="programs" />
                  </Field>

                  {/*
                    COSMETIC GATE, exactly as OpsRoot describes its nav gating.
                    Hiding this checkbox keeps an LDE Executive from asking for a
                    section that would 403; it is NOT what protects the figures.
                    `require_commercials()` runs in reports.py before any
                    remuneration row is read, and the RLS policies underneath it
                    would return zero rows even if this branch were deleted.
                  */}
                  {canSeeCommercials && (
                    <Field
                      label="Trainer cost section"
                      hint="Money: Senior Manager and Manager only. Off by default — a report carries no commercial data unless somebody deliberately asked for it."
                    >
                      <Toggle
                        checked={includeTrainerCost}
                        onChange={setIncludeTrainerCost}
                        label="Include per-trainer net pay"
                      />
                    </Field>
                  )}
                </div>

                <div className="mt-4 flex items-center gap-3">
                  <Button
                    variant="primary"
                    disabled={!programId || !periodValid || governance.isPending}
                    onClick={() => governance.mutate()}
                  >
                    {governance.isPending && <Spinner />}
                    Assemble draft
                  </Button>
                  {!programId && <span className="text-xs text-ink-3">Pick a program first.</span>}
                  {programId && !periodValid && (
                    <span className="text-xs text-ink-3">
                      The period ends before it starts. The API refuses that too.
                    </span>
                  )}
                </div>

                {governance.error ? (
                  <div className="mt-3">
                    <ReportError error={governance.error} />
                  </div>
                ) : null}
              </Card>

              {governance.data ? (
                <GovernanceDraftView result={governance.data} />
              ) : (
                <Card>
                  <EmptyState
                    title="No draft assembled yet"
                    body={
                      'Nothing is fetched on arrival. Assembling a governance report reads the ' +
                      'period’s delivery facts, logs who asked for it, ' +
                      'and — if you asked for prose — spends a model call. That is ' +
                      'a deliberate act, not a page load.'
                    }
                    hint="Pick a program above and press Assemble draft. Nothing is saved and nothing leaves byteXL when you do."
                  />
                </Card>
              )}
            </div>
          )}

          {tab === 'feedback' && (
            <div className="space-y-4">
              <Card className="p-4">
                <SectionTitle
                  title="Feedback synthesis"
                  subtitle="Counted and averaged in plain code — no model involved at any point. This is delivery data rather than money, so an LDE Executive reads it too."
                />
                <Field
                  label="Program"
                  hint="A collection with no recorded date is included, which is why two denominators are shown below."
                >
                  <Select value={programId} onChange={(e) => setProgramId(e.target.value)}>
                    <option value="">Select a program…</option>
                    {(programs.data?.rows ?? []).map((p) => (
                      <option key={p.id} value={p.id}>
                        {p.colleges?.name ?? 'Unknown college'} · {p.name} ({p.type})
                      </option>
                    ))}
                  </Select>
                </Field>
              </Card>

              {!programId ? (
                <Card>
                  <EmptyState
                    title="Pick a program"
                    body="The synthesis gathers every feedback collection recorded against one program inside the period above."
                    hint="Only programs your college and cluster assignments reach are in the list — a program you cannot see is not missing, it is out of your reach."
                  />
                </Card>
              ) : feedback.isPending ? (
                // Skeleton rather than a spinner: what lands here is a table, and
                // a centred spinner collapses the layout and then springs it back.
                <Card>
                  <TableSkeleton rows={5} cols={4} />
                </Card>
              ) : feedback.error ? (
                <ReportError error={feedback.error} />
              ) : feedback.data ? (
                <div className="space-y-4">
                  <Card className="p-4">
                    <SectionTitle
                      title={feedback.data.program_name}
                      subtitle={`${fmtDate(feedback.data.period_start)} → ${fmtDate(
                        feedback.data.period_end,
                      )}`}
                      action={<Badge>Not an artifact</Badge>}
                    />
                    <InfoNote>
                      This one is a read, not a draft: there is no artifact id, no version and
                      nothing to approve. The governance report is where a synthesis becomes part
                      of something that would need signing off.
                    </InfoNote>
                    <div className="mt-3">
                      <SynthesisPanel synthesis={feedback.data.synthesis} />
                    </div>
                  </Card>

                  <Card>
                    <div className="px-4 pt-4">
                      <SectionTitle
                        title="Collections"
                        subtitle="Each row as it was recorded. Scores are strings from the database and are not averaged here."
                        action={<Badge>{feedback.data.entries.length}</Badge>}
                      />
                    </div>
                    {feedback.data.entries.length === 0 ? (
                      <EmptyState
                        title="No feedback collected in this period"
                        body="An empty synthesis is a finding, not a fault — it is what a governance report should say when nobody collected anything."
                        hint="Widen the reporting period above if you expected rows here. A collection with no recorded date still counts, so a missing date is not the explanation."
                      />
                    ) : (
                      <div className="overflow-x-auto scroll-slim">
                        <table className="w-full text-sm">
                          <thead>
                            <tr className="border-b border-line">
                              <Th>Source</Th>
                              <Th>Collected</Th>
                              <Th className="text-right">Score</Th>
                              <Th className="text-right">Responses</Th>
                            </tr>
                          </thead>
                          <tbody className="divide-y divide-line-soft">
                            {feedback.data.entries.map((entry, i) => (
                              <tr key={`${entry.source}-${entry.collected_on ?? 'undated'}-${i}`}>
                                <Td className="text-ink">{entry.source}</Td>
                                <Td className="text-xs text-ink-2">
                                  {entry.collected_on ? (
                                    fmtDate(entry.collected_on)
                                  ) : (
                                    <span className="text-ink-3">Date not recorded — still counted</span>
                                  )}
                                </Td>
                                <Td className="text-right tabular-nums text-ink-2">
                                  {entry.summary_score ?? '—'}
                                </Td>
                                <Td className="text-right tabular-nums text-ink-2">
                                  {entry.response_count ?? '—'}
                                </Td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    )}
                  </Card>

                  <NarrativePanel narrative={feedback.data.narrative} />
                </div>
              ) : null}
            </div>
          )}

          {tab === 'college' && (
            <div className="space-y-4">
              <Card className="p-4">
                <SectionTitle
                  title="College summary"
                  subtitle="Every program at one college. It carries no money at all — not behind a switch, not present-but-blank."
                />
                <Field
                  label="College"
                  hint="Reach comes from your college and cluster assignments, resolved in Postgres — not from your role alone."
                >
                  <Select value={collegeId} onChange={(e) => setCollegeId(e.target.value)}>
                    <option value="">Select a college…</option>
                    {(colleges.data?.rows ?? []).map((c) => (
                      <option key={c.id} value={c.id}>
                        {c.name}
                        {c.city ? ` · ${c.city}` : ''}
                      </option>
                    ))}
                  </Select>
                  <BoundNote bound={colleges.data} noun="colleges" />
                </Field>
              </Card>

              {!collegeId ? (
                <Card>
                  <EmptyState
                    title="Pick a college"
                    body="The summary covers every program running at one college in the period above."
                    hint="It carries no money at all — not hidden, not empty-but-present. A college summary is the one report shaped to be readable by anyone internal."
                  />
                </Card>
              ) : summary.isPending ? (
                <Card>
                  <TableSkeleton rows={5} cols={5} />
                </Card>
              ) : summary.error ? (
                <ReportError error={summary.error} />
              ) : summary.data ? (
                <div className="space-y-4">
                  <Card className="p-4">
                    <SectionTitle
                      title={summary.data.title}
                      subtitle={`${summary.data.college_name} · ${fmtDate(
                        summary.data.period_start,
                      )} → ${fmtDate(summary.data.period_end)}`}
                      action={<StatePill state={summary.data.artifact_state} />}
                    />
                    <ArtifactMeta artifactId={summary.data.artifact_id} />
                  </Card>

                  <Card>
                    <div className="px-4 pt-4">
                      <SectionTitle
                        title="Programs"
                        subtitle={FIGURES_NOTE}
                        action={<Badge>{summary.data.programs.length}</Badge>}
                      />
                    </div>
                    {summary.data.programs.length === 0 ? (
                      <EmptyState
                        title="No programs at this college"
                        body="Either none have been created, or none are within your reach. Both look the same here on purpose — the database answers that question, not this screen."
                        hint="Reach comes from your college and cluster assignments, so a colleague may see programs here that you do not. Ask a Manager if you think you should be assigned."
                      />
                    ) : (
                      <div className="overflow-x-auto scroll-slim">
                        <table className="w-full text-sm">
                          <thead>
                            <tr className="border-b border-line">
                              <Th>Program</Th>
                              <Th>Stage</Th>
                              <Th className="text-right">Batches</Th>
                              <Th className="text-right">Trainers</Th>
                              <Th className="text-right">Incomplete tracksheets</Th>
                            </tr>
                          </thead>
                          <tbody className="divide-y divide-line-soft">
                            {summary.data.programs.map((line) => (
                              <tr key={line.program_id}>
                                <Td>
                                  <span className="font-medium text-ink">{line.program_name}</span>
                                  <span className="block text-[11px] text-ink-3 mt-0.5">
                                    {line.program_type}
                                  </span>
                                </Td>
                                <Td className="text-xs text-ink-2">
                                  {STAGE_LABEL[line.stage as ProgramStage] ?? line.stage}
                                </Td>
                                <Td className="text-right tabular-nums text-ink-2">
                                  {line.batch_count}
                                </Td>
                                <Td className="text-right tabular-nums text-ink-2">
                                  {line.trainer_count}
                                </Td>
                                <Td
                                  className={`text-right tabular-nums ${
                                    line.incomplete_tracksheets > 0
                                      ? 'text-warn-ink font-medium'
                                      : 'text-ink-2'
                                  }`}
                                >
                                  {line.incomplete_tracksheets}
                                </Td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    )}
                  </Card>

                  <NarrativePanel narrative={summary.data.narrative} />
                  <ApprovalPanel approval={summary.data.approval} />
                </div>
              ) : null}
            </div>
          )}
        </div>
      </Page>
    </>
  )
}

/** What this screen is allowed to do, said once at the top (§8, R3, R4). */
function CeilingNote() {
  return (
    <Card className="p-4">
      <SectionTitle
        title="Draft is the ceiling"
        subtitle="The Reporting agent is allowed to propose and nothing else. A human edits it, and a human releases it — and that second half happens on another screen entirely."
      />
      <div className="grid gap-2 sm:grid-cols-2">
        <div className="rounded-lg border border-line bg-surface-2 px-3 py-2.5">
          <p className="text-xs font-medium text-ink">Nothing here reaches a college</p>
          <p className="text-[11px] text-ink-2 mt-1.5 leading-relaxed">{NOTHING_SENDS_NOTE}</p>
        </div>
        <div className="rounded-lg border border-line bg-surface-2 px-3 py-2.5">
          <p className="text-xs font-medium text-ink">Where the numbers come from</p>
          <p className="text-[11px] text-ink-2 mt-1.5 leading-relaxed">{FIGURES_NOTE}</p>
        </div>
      </div>
    </Card>
  )
}

function PeriodControls({
  periodStart,
  periodEnd,
  onStart,
  onEnd,
  valid,
  includeNarrative,
  onNarrative,
}: {
  periodStart: string
  periodEnd: string
  onStart: (value: string) => void
  onEnd: (value: string) => void
  valid: boolean
  includeNarrative: boolean
  onNarrative: (value: boolean) => void
}) {
  const apply = (monthsAgo: number) => {
    const bounds = monthBounds(monthsAgo)
    onStart(bounds.start)
    onEnd(bounds.end)
  }

  return (
    <Card className="p-4">
      <SectionTitle
        title="Reporting period"
        subtitle="Shared by all three reports. Task overdue-ness is measured against the end of the period, never against today — a report regenerated for an old period must report what was true then."
      />
      <div className="grid gap-3 sm:grid-cols-3">
        <Field label="Period start">
          <Input type="date" value={periodStart} onChange={(e) => onStart(e.target.value)} />
        </Field>
        <Field label="Period end">
          <Input type="date" value={periodEnd} onChange={(e) => onEnd(e.target.value)} />
        </Field>
        <Field
          label="Narrative"
          hint="Off by default. The figures are the report; somebody who needs only the numbers should not spend a model call to get them."
        >
          <Toggle
            checked={includeNarrative}
            onChange={onNarrative}
            label="Generate prose alongside the figures"
          />
        </Field>
      </div>
      <Toolbar className="mt-3">
        <Button size="sm" onClick={() => apply(0)}>
          This month
        </Button>
        <Button size="sm" onClick={() => apply(1)}>
          Last month
        </Button>
        {includeNarrative && (
          <Badge tone="flame">A model will write the covering prose</Badge>
        )}
        {!valid && (
          <span className="text-xs text-warn-ink">
            The period ends before it starts.
          </span>
        )}
      </Toolbar>
    </Card>
  )
}

/**
 * One governance draft, in full.
 *
 * Order is deliberate: what it is, then the figures, then the commercial section
 * if it was asked for, then the generated prose, then who could approve it —
 * which today is nobody. The prose comes after the figures because it is a gloss
 * on them, and putting it first would invite it to be read as the report.
 */
function GovernanceDraftView({ result }: { result: GovernanceDraftResult }) {
  const { report, headerState } = result

  return (
    <div className="space-y-4">
      <Card className="p-4">
        <SectionTitle
          title={report.title}
          subtitle={`${report.college_name} · ${report.program_name} (${report.program_type}) · ${fmtDate(
            report.period_start,
          )} → ${fmtDate(report.period_end)}`}
          action={<StatePill state={report.artifact_state} />}
        />
        <ArtifactMeta
          artifactId={report.artifact_id}
          version={report.artifact_version}
          payloadHash={report.payload_hash}
          headerState={headerState}
        />
        {report.is_commercial && (
          <div className="mt-3">
            <InfoNote>
              This draft carries the trainer-cost section, so it is a commercial artifact (§4). It
              is not a document to hand to a college as it stands.
            </InfoNote>
          </div>
        )}
      </Card>

      <Card className="p-4">
        <SectionTitle title="Delivery" subtitle={FIGURES_NOTE} />
        <div className="grid gap-3 grid-cols-2 lg:grid-cols-4">
          <Stat label="Batches" value={report.batch_count} />
          <Stat label="Trainers deployed" value={report.trainer_count} />
          <Stat
            label="Incomplete tracksheets"
            value={report.incomplete_tracksheet_count}
            tone={report.incomplete_tracksheet_count > 0 ? 'warn' : 'normal'}
          />
          <Stat
            label="Student attendance"
            value={
              report.student_attendance_percent === null
                ? '—'
                : `${report.student_attendance_percent}%`
            }
          />
          <Stat label="Assessments conducted" value={report.assessments_conducted} />
          <Stat label="Observations" value={report.observations} />
          <Stat label="Tasks open" value={report.tasks_open} />
          <Stat
            label="Tasks overdue"
            value={report.tasks_overdue}
            tone={report.tasks_overdue > 0 ? 'warn' : 'normal'}
          />
        </div>
        {report.incomplete_tracksheet_count > 0 && (
          <p className="text-xs text-ink-2 mt-3 leading-relaxed">
            A tracksheet whose marks do not cover the period is an operational gap, not a cosmetic
            one: §5 makes an unmarked day pay a bCAP trainer silently and underpay a CRT trainer
            silently. It is counted here so a governance meeting sees it before a payout does.
          </p>
        )}
      </Card>

      <Card className="p-4">
        <SectionTitle
          title="Feedback"
          subtitle="Averaged over the collections that carried a score, in Decimal, in assembly.py."
        />
        <SynthesisPanel synthesis={report.feedback} />
      </Card>

      <TrainerCostPanel cost={report.trainer_cost} />

      <NarrativePanel narrative={report.narrative} />

      <Card className="p-4">
        <SectionTitle
          title="Earlier reports on file"
          subtitle="Read-only context. A governance report is periodic, and the useful question when drafting one is what the last one said."
        />
        {report.prior_reports.length === 0 ? (
          <p className="text-sm text-ink-3">
            None on file for this program. This would be the first.
          </p>
        ) : (
          <ul className="space-y-1.5">
            {report.prior_reports.map((title, i) => (
              <li key={`${title}-${i}`} className="text-sm text-ink-2 break-all">
                {title}
              </li>
            ))}
          </ul>
        )}
      </Card>

      <ApprovalPanel approval={report.approval} />
    </div>
  )
}

/** The synthesis, with both denominators visible. */
function SynthesisPanel({ synthesis }: { synthesis: FeedbackSynthesis }) {
  return (
    <div className="space-y-3">
      <div className="grid gap-3 grid-cols-2 lg:grid-cols-4">
        <Stat label="Collections" value={synthesis.collections} />
        <Stat label="Of which scored" value={synthesis.scored_collections} />
        <Stat label="Responses" value={synthesis.total_responses ?? '—'} />
        <Stat label="Average score" value={synthesis.average_score ?? '—'} />
      </div>
      <div className="rounded-lg border border-line bg-surface-2 px-3 py-2.5 space-y-1.5">
        <Meta label="Lowest score" value={synthesis.lowest_score ?? '—'} />
        <Meta label="Highest score" value={synthesis.highest_score ?? '—'} />
      </div>
      {synthesis.scored_collections !== synthesis.collections && (
        <p className="text-xs text-ink-2 leading-relaxed">
          The average is over the {synthesis.scored_collections} scored collections, not over all{' '}
          {synthesis.collections}. Both numbers are shown because an average silently taken over
          part of the data is how a wrong number reaches a college meeting.
        </p>
      )}
    </div>
  )
}

/**
 * The commercial section. Rendered only when the API sent one.
 *
 * Absent for two different reasons that must not be confused: it was not asked
 * for, or it was refused. A refusal arrives as a 403 on the whole request and is
 * handled by `ReportError`, so this component only ever draws "not requested".
 */
function TrainerCostPanel({ cost }: { cost: TrainerCost | null }) {
  if (cost === null) {
    return (
      <Card className="p-4">
        <SectionTitle
          title="Trainer cost"
          subtitle="Commercial section (§4). Not included in this draft."
        />
        <p className="text-sm text-ink-3">
          A report is non-commercial unless somebody asked for the other kind. Tick the trainer
          cost box and assemble again to include per-trainer net pay — and note that doing so makes
          the draft a commercial artifact.
        </p>
      </Card>
    )
  }

  return (
    <Card>
      <div className="px-4 pt-4">
        <SectionTitle
          title="Trainer cost"
          subtitle="Every figure is a net_amount the engine wrote and this report read back. Nothing here was recomputed."
          action={<Badge tone="warn">Commercial</Badge>}
        />
      </div>

      {cost.lines.length === 0 ? (
        <EmptyState
          title="No remuneration sheets overlap this period"
          body="Deployed trainers with no sheet are listed below. A programme reported as delivered with unpaid trainers is the governance signal that matters."
          hint="This is a real finding about the period, not a permission problem — a refusal would have stopped the whole report rather than emptying one table."
        />
      ) : (
        <div className="overflow-x-auto scroll-slim">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-line">
                <Th>Trainer</Th>
                <Th>Period</Th>
                <Th>Invoice</Th>
                <Th className="text-right">Net pay</Th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line-soft">
              {cost.lines.map((line, i) => (
                <tr key={`${line.pan}-${line.period_start}-${i}`}>
                  <Td>
                    <span className="font-medium text-ink">{line.trainer}</span>
                    {/* PAN is the trainer's identity, not a decoration: it seeds
                        the invoice number and it is what a payout is matched on,
                        never the name string above it. Monospaced and
                        select-all so copying one is a single gesture. */}
                    <span className="block text-[11px] text-ink-3 mt-0.5">
                      {line.pan ? (
                        <MonoValue title="PAN — the trainer's identity of record">
                          {line.pan}
                        </MonoValue>
                      ) : (
                        'no PAN'
                      )}
                    </span>
                  </Td>
                  <Td className="text-xs text-ink-2">
                    {fmtDate(line.period_start)} → {fmtDate(line.period_end)}
                  </Td>
                  <Td className="text-xs text-ink-2">
                    {line.invoice_no ? (
                      <MonoValue>{line.invoice_no}</MonoValue>
                    ) : (
                      'Not issued'
                    )}
                  </Td>
                  <Td className="text-right tabular-nums whitespace-nowrap font-medium text-ink">
                    {fmtAmount(line.net)}
                  </Td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="px-4 pb-4 pt-3 space-y-3">
        <p className="text-xs text-ink-2 leading-relaxed">
          <strong className="text-ink">There is no total, and there will not be one.</strong> R2
          puts every rupee of arithmetic in <code>services/remuneration/engine.py</code>, where it
          is unit-tested against the §6 fixtures. The API sends <code>total: null</code> on purpose
          and this screen renders each figure as the string it arrived as — a sum computed in a
          report would be a second implementation of money that nobody reconciles.
        </p>
        {cost.trainers_without_payout.length > 0 && (
          <div className="rounded-lg border border-warn/25 bg-warn-wash px-3 py-2.5">
            <p className="text-xs font-medium text-warn-ink">
              Deployed in this period with no remuneration sheet ({cost.trainers_without_payout.length})
            </p>
            <p className="text-xs text-ink-2 mt-1.5 leading-relaxed">
              {cost.trainers_without_payout.join(' · ')}
            </p>
          </div>
        )}
      </div>
    </Card>
  )
}

/**
 * The generated prose, and only the prose.
 *
 * Drawn as visibly a different kind of object from everything above it: its own
 * card, a labelled header, the telemetry of the call printed underneath. R1 puts
 * language on the model's side of the line and figures on the database's, and
 * this panel is where that line is visible to a reader who has not read
 * CLAUDE.md. Nothing else on this screen renders `narrative.body`, and nothing
 * anywhere reads a number out of it.
 */
function NarrativePanel({ narrative }: { narrative: ReportNarrative | null }) {
  if (narrative === null) {
    return (
      <Card className="p-4">
        <SectionTitle
          title="Narrative"
          subtitle="Not generated for this draft."
        />
        <p className="text-sm text-ink-3">
          The figures above are the report. Turn the narrative on in the period panel to have a
          model write the covering prose — it is off by default so that nobody spends a model
          call just to read a count. Nothing is missing from what you are looking at.
        </p>
      </Card>
    )
  }

  return (
    // `flame`, and this is the panel the token was reserved for. It used to
    // wear `accent`, which is this app's colour for stored data and therefore
    // said the opposite of what this card is — the one paragraph on the screen
    // no database wrote. Everything else on this page is figures; this is the
    // only card a reader must discount, so it is the only one that is warm.
    <Card className="p-4 border-flame/30">
      <SectionTitle
        title="Narrative — generated"
        subtitle="Wording a model wrote from the figures above. It is a first draft to edit, never a source to quote."
        action={<Badge tone="flame">Generated</Badge>}
      />

      <div className="rounded-lg border border-flame/30 bg-flame-soft px-3.5 py-3">
        <p className="text-sm text-ink leading-relaxed whitespace-pre-wrap">{narrative.body}</p>
      </div>

      <p className="text-xs text-ink-2 mt-3 leading-relaxed">{NARRATIVE_NOTE}</p>

      {/* The telemetry of the call that produced the paragraph above. It sits
          inside the flame card because it describes the generation, not the
          program — nothing here is a delivery figure. */}
      <div className="mt-3 pt-3 border-t border-line grid gap-1.5 sm:grid-cols-2">
        <Meta label="Task" value={narrative.llm_task} />
        <Meta label="Model" value={narrative.model} mono />
        <Meta
          label="Tokens"
          value={`${narrative.prompt_tokens} in · ${narrative.completion_tokens} out`}
        />
        <Meta label="Latency" value={`${narrative.latency_ms} ms`} />
      </div>
    </Card>
  )
}

/**
 * Who may approve this. Today: nobody, and the reason is an open question.
 *
 * NO BUTTON. Not a disabled one either — a greyed-out "Approve" would suggest
 * that the right permission would light it up, and no permission will. The
 * lifecycle is operated from the Approvals screen, and for this artifact type it
 * answers 501 until §14 Q3 has an owner's answer.
 */
function ApprovalPanel({ approval }: { approval: ReportApproval }) {
  if (approval.can_be_approved) {
    return (
      <Card className="p-4">
        <SectionTitle
          title="Approval"
          subtitle="Taken on the Approvals screen, by a human, as a separate act from release (R4)."
        />
        <InfoNote>
          This artifact type can be approved by:{' '}
          <strong>{approval.approvers.join(', ') || 'nobody named'}</strong>. The act itself is not
          offered here — approving freezes and hashes a version, and it belongs on the screen that
          shows the version history beside it.
        </InfoNote>
      </Card>
    )
  }

  return (
    <Card className="p-4">
      <SectionTitle
        title="Approval"
        subtitle="Reported with the draft rather than discovered at the attempt."
      />
      <div className="rounded-lg border border-info/30 bg-info-wash px-3 py-2.5">
        <p className="text-sm font-medium text-info-ink">
          Nobody can approve a {approval.artifact_type.replace(/_/g, ' ')} yet — and that is an
          open question, not a refusal.
        </p>
        <p className="text-xs text-ink-2 mt-1.5 leading-relaxed">
          {REPORT_APPROVAL_UNDECIDED_NOTE}
        </p>
        {approval.blocked_reason && (
          <p className="text-[11px] text-ink-3 mt-2 font-mono break-words">
            {approval.blocked_reason}
          </p>
        )}
      </div>
      <p className="text-xs text-ink-2 mt-3 leading-relaxed">{NOTHING_SENDS_NOTE}</p>
    </Card>
  )
}

/**
 * A refusal, said as what it actually was.
 *
 * Three of these must never render as a generic red box:
 *
 *   503 — the narrator is not configured. Phase 1 has no AI in it by design.
 *   422 — the prose stated a figure the data did not carry, so the draft was
 *         thrown away rather than repaired. The platform working, not failing.
 *   403 — the commercials wall. The report is still available without the
 *         section, and saying so is the difference between a dead end and a
 *         retry.
 */
function ReportError({ error }: { error: unknown }) {
  if (isNarrationUnavailable(error)) {
    return (
      <div className="rounded-lg border border-info/30 bg-info-wash px-3 py-2.5" role="status">
        <p className="text-sm font-medium text-info-ink">
          Narration is not switched on in this environment.
        </p>
        <p className="text-xs text-ink-2 mt-1.5 leading-relaxed">
          The request was valid; the OpenRouter gateway simply is not configured here, which is the
          default while Phase 1 stands (§13: “Phase 1 has no AI in it”). Turn the narrative off and
          ask again — the figures do not depend on a model, and they are the artifact.
        </p>
        <p className="text-[11px] text-ink-3 mt-1.5 font-mono break-words">
          {errorMessage(error)}
        </p>
      </div>
    )
  }

  if (isDraftRefused(error)) {
    return (
      <ErrorNote>
        <strong>The draft was refused, not corrected.</strong> {errorMessage(error)} If that message
        names a figure, it means the generated prose stated a number the structured data did not
        contain, and R1 makes that fatal to the whole draft — a sentence quietly patched to match
        would hide a systematic problem behind one good-looking report. There is deliberately no
        retry: ask again without the narrative to get the facts, and treat a repeat as something to
        investigate rather than to re-roll.
      </ErrorNote>
    )
  }

  if (isForbidden(error)) {
    return (
      <ErrorNote>
        <strong>The server refused this account.</strong> {errorMessage(error)} If you asked for the
        trainer-cost section, that is the commercials wall (§4: Senior Manager and Manager only),
        and it closed before any remuneration row was read — the refusal is on the section, not on
        the report, so untick it and the same period assembles fine. Otherwise the program or
        college is outside your reach, which comes from your college and cluster assignments rather
        than from your role alone.
      </ErrorNote>
    )
  }

  if (isNotFound(error)) {
    return (
      <ErrorNote>
        <strong>Not found.</strong> {errorMessage(error)} Either it does not exist, or it is not
        attached to a college. The list you picked from is scoped by RLS, so a stale tab is the
        usual cause — reload.
      </ErrorNote>
    )
  }

  return <ErrorNote>{errorMessage(error)}</ErrorNote>
}

// --- Small pieces ------------------------------------------------------------

const PILL =
  'inline-flex items-center rounded-md border px-1.5 py-0.5 text-[11px] font-medium whitespace-nowrap'

const STATE_TONE: Record<ArtifactState, string> = {
  DRAFT: 'bg-surface-2 text-ink-3 border-line',
  PENDING_APPROVAL: 'bg-warn-wash text-warn-ink border-warn/25',
  APPROVED: 'bg-info-wash text-info-ink border-info/25',
  RELEASED: 'bg-good-wash text-good-ink border-good/25',
}

/** The same four tones as the Approvals queue, so one state reads identically on both screens. */
function StatePill({ state }: { state: ArtifactState }) {
  return <span className={`${PILL} ${STATE_TONE[state]}`}>{state.replace(/_/g, ' ')}</span>
}

/**
 * The artifact's identity. The hash is shown because it is what lets a reader
 * prove the draft on screen is the one the API produced — it is not a freeze,
 * and it says so: approval is what freezes (R4), and nothing here has been
 * approved.
 */
function ArtifactMeta({
  artifactId,
  version,
  payloadHash,
  headerState,
}: {
  artifactId: string
  version?: number
  payloadHash?: string
  headerState?: ArtifactState
}) {
  return (
    <div className="rounded-lg border border-line bg-surface-2 px-3 py-2.5 space-y-1.5">
      <Meta label="Artifact id" value={artifactId} mono />
      {version !== undefined && <Meta label="Version" value={`v${version}`} />}
      {/* The hash and the caveat used to be one string, which meant the caveat
          was monospaced and select-all along with the hash. Split, so copying
          the hash copies the hash. */}
      {payloadHash !== undefined && (
        <>
          <Meta label="Payload hash" value={payloadHash} mono />
          <p className="text-[11px] text-ink-3 leading-relaxed">
            A fingerprint of what you are looking at, so you can prove later that this is the
            draft the system produced. It is not a freeze — approving is what freezes a
            version, and nothing here has been approved.
          </p>
        </>
      )}
      {headerState !== undefined && (
        <Meta label={`Response header`} value={`X-Artifact-State: ${headerState}`} mono />
      )}
      <Meta label="Persisted" value="No — assembled on request, stored nowhere" />
    </div>
  )
}

/**
 * `mono` now routes through `MonoValue` rather than hand-applying `font-mono`,
 * which buys the one behaviour that matters on these rows: `select-all`. Every
 * real use of an artifact id or a payload hash is copying it whole into a
 * ticket, and a half-selected hash is worse than none.
 */
function Meta({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <span className="text-xs text-ink-3 shrink-0">{label}</span>
      <span className="text-xs text-ink text-right break-all">
        {mono ? <MonoValue className="text-xs">{value}</MonoValue> : value}
      </span>
    </div>
  )
}

/** A checkbox. `ui.tsx` has no checkbox because no screen needed one until this. */
function Toggle({
  checked,
  onChange,
  label,
}: {
  checked: boolean
  onChange: (value: boolean) => void
  label: string
}) {
  return (
    <span className="inline-flex items-center gap-2 h-9">
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        className="h-4 w-4 rounded border-line accent-accent"
      />
      <span className="text-sm text-ink">{label}</span>
    </span>
  )
}
