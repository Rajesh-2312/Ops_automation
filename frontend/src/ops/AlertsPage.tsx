import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { errorMessage } from '../lib/supabase'
import {
  BANDS,
  BAND_LABEL,
  SEVERITY_LABEL,
  TIER_LABEL,
  alertKeys,
  codeLabel,
  fetchAlertFeed,
  type AnomalySeverity,
  type DeploymentRisk,
  type Escalation,
  type ProgramRisk,
  type RiskBand,
} from '../lib/alerts'
import { Page, PageHeader } from '../components/AppShell'
import {
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorNote,
  FilterChip,
  HelpTip,
  Loading,
  MonoValue,
  PageIntro,
  SearchInput,
  Toolbar,
  fmtDate,
} from '../components/ui'

/**
 * The Delivery Monitor's screen. CLAUDE.md §8 — ceiling **Alert, internal only**.
 *
 * The Work Queue answers "what is on me today". This answers the question
 * nobody is assigned: "what is going wrong that nobody has raised yet". Those
 * are different lists — an unmarked CRT day is not a task anyone was given, and
 * it will not appear on a checklist until it has already underpaid a trainer
 * (§5). So the arrangement here is by RISK, worst first, and the server does
 * that sort: the band and the score are the engine's, and re-deriving them in
 * the browser would let this screen and the SLA disagree.
 *
 * READ-ONLY, AND NOT INCIDENTALLY
 * -------------------------------
 * There is no acknowledge button, no dismiss, no snooze and no "notify the
 * college". §8 puts this agent at autonomy level 1 (Observe) and R3 keeps
 * release capability off every agent surface; the way to clear an alert is to
 * do the thing it is about. An action here would also have to answer "who did
 * it, and where is the audit row", and a dashboard is the wrong place for both.
 *
 * NOTHING ON THIS SCREEN IS COMMERCIAL
 * ------------------------------------
 * Day counts, hours, absence rates and a risk score — no amount, no rate, no
 * invoice number, no PAN. That is why an LDE Executive can open it at all (§4
 * gives them attendance and batches, and no commercials), and it is checked on
 * the server rather than assumed here.
 *
 * AND NOTHING ON IT IS `flame`
 * ----------------------------
 * Worth stating, because this is an "agent" screen and the reflex is to mark it
 * as one. `flame` is reserved for prose a language model wrote (DESIGN.md §2).
 * Every sentence rendered below — every anomaly `message`, every escalation
 * `reason` — is formatted by the detector or the SLA rule row that raised it,
 * in pure Python, from the figures it compared. It is generated the way an
 * error message is generated, not the way a draft is. Painting it flame would
 * teach a reader that flame means "a computer produced this", which is every
 * pixel in the app, and the token would stop meaning anything.
 */
export function AlertsPage() {
  const [band, setBand] = useState<RiskBand | 'all'>('all')
  const [search, setSearch] = useState('')

  const {
    data: feed,
    isPending,
    error,
    refetch,
    isFetching,
  } = useQuery({
    queryKey: alertKeys.feed(),
    queryFn: () => fetchAlertFeed(),
  })

  // Both filters are client-side and neither refetches: the feed is one server
  // computation over everything the reader reaches, so narrowing it is a view
  // concern. The count beside the search box is what keeps that honest.
  const programs = useMemo(() => {
    const q = search.trim().toLowerCase()
    return (feed?.programs ?? []).filter(
      (p) =>
        (band === 'all' || p.band === band) &&
        (q === '' ||
          p.college_name.toLowerCase().includes(q) ||
          p.program_name.toLowerCase().includes(q)),
    )
  }, [feed?.programs, band, search])

  const total = feed?.program_count ?? 0
  const atRisk = (feed?.programs ?? []).filter((p) => p.band !== 'low').length
  const filtered = band !== 'all' || search.trim() !== ''

  return (
    <>
      <PageHeader
        title="Delivery alerts"
        purpose="Programs where delivery looks like it is going wrong — attendance not marked, tasks left open, documents unsigned — worst first. It is a noticeboard for the byteXL team only: nothing here reaches a trainer or a college, and you clear an item by doing the job it points at."
        subtitle={
          isPending
            ? 'Loading…'
            : feed
              ? `${atRisk} of ${total} program${total === 1 ? '' : 's'} at risk · ` +
                `${feed.escalation_count} escalation${feed.escalation_count === 1 ? '' : 's'} · ` +
                `${fmtDate(feed.period_start)} – ${fmtDate(feed.period_end)}`
              : undefined
        }
        actions={
          <Button size="sm" variant="ghost" disabled={isFetching} onClick={() => void refetch()}>
            Refresh
          </Button>
        }
      />

      <Page>
        {error && (
          <div className="mb-4">
            <ErrorNote>{errorMessage(error)}</ErrorNote>
          </div>
        )}

        {isPending ? (
          <Loading label="Assessing delivery risk" />
        ) : (
          <div className="max-w-4xl space-y-4">
            <PageIntro
              steps={[
                'Rules read the stored data',
                'Anomalies score the program',
                'Bands sort it, worst first',
                'A person does the underlying job',
              ]}
            >
              <p>
                Every line below was produced by a fixed rule comparing a stored figure
                against a threshold — no language model is involved in deciding what appears
                here or how loudly. An{' '}
                <HelpTip term="escalation">
                  Naming who should be looking at a problem, and at which level: LDE
                  Executive, then Manager, then Senior Manager. The rung is chosen by the
                  rule that fired, and it climbs only when the rung below has nobody assigned
                  to this college.
                </HelpTip>{' '}
                is likewise a table lookup, not a judgement call, so two people reading the
                same feed always get the same answer.
              </p>
              <p className="mt-2">
                This screen sits at{' '}
                <HelpTip term="autonomy level 1 — observe">
                  The lowest rung of the four-step ladder the platform's agents are held to:
                  observe (read and report internally) · draft (propose, a human sends) ·
                  act-with-approval · act. The Delivery Monitor never leaves the first rung,
                  so it can raise a flag and nothing else.
                </HelpTip>
                , which is why there is nothing to click on an alert. No acknowledge, no
                dismiss, no snooze, and no way to notify the college from here. An alert goes
                away when the attendance is marked or the document is signed.
              </p>
            </PageIntro>

            <Toolbar>
              <SearchInput
                value={search}
                onChange={setSearch}
                placeholder="Search college or program"
                count={programs.length}
                total={total}
              />
              <FilterChip
                label="Everything"
                count={total}
                active={band === 'all'}
                onClick={() => setBand('all')}
              />
              <span className="w-px h-5 bg-line mx-1" aria-hidden />
              {BANDS.map((b) => (
                <FilterChip
                  key={b}
                  label={BAND_LABEL[b]}
                  count={feed?.band_counts[b] ?? 0}
                  active={band === b}
                  onClick={() => setBand(band === b ? 'all' : b)}
                  tone={b === 'critical' || b === 'high' ? 'alert' : undefined}
                />
              ))}
            </Toolbar>

            {programs.length === 0 ? (
              <Card>
                {/* Three different absences with three different answers, which is
                    exactly the case `hint` exists for: nothing is being monitored,
                    nothing is wrong, or a filter above is hiding what is. */}
                <EmptyState
                  title={
                    total === 0
                      ? 'Nothing to monitor yet'
                      : filtered
                        ? 'No program matches these filters'
                        : 'Nothing is drifting'
                  }
                  body={
                    total === 0
                      ? 'No program you reach has a deployment in the current period. The monitor reads attendance, tasks and documents — it has nothing to read until a trainer is deployed.'
                      : filtered
                        ? 'Programs you reach are being monitored, but none of them are in the band you picked or match what you typed.'
                        : 'Every program you reach is being monitored and no rule has fired against any of them in this period.'
                  }
                  hint={
                    total === 0
                      ? 'Deploy a trainer to a batch and this fills in on the next refresh — the feed is computed on request, not stored.'
                      : filtered
                        ? `Showing 0 of ${total}. Clear the search and the band filter to see everything again.`
                        : 'This is the good outcome, not a loading state. The counts in the header above are live.'
                  }
                  action={
                    total === 0 ? (
                      <Link to="/board">
                        <Button size="sm">Open Program Console</Button>
                      </Link>
                    ) : filtered ? (
                      <Button
                        size="sm"
                        onClick={() => {
                          setBand('all')
                          setSearch('')
                        }}
                      >
                        Show everything
                      </Button>
                    ) : undefined
                  }
                />
              </Card>
            ) : (
              programs.map((program) => <ProgramCard key={program.program_id} program={program} />)
            )}
          </div>
        )}
      </Page>
    </>
  )
}

function ProgramCard({ program }: { program: ProgramRisk }) {
  const loud = program.band === 'critical' || program.band === 'high'
  return (
    <Card className={loud ? 'border-bad/30' : undefined}>
      <div className="flex items-center justify-between gap-3 px-4 py-3 border-b border-line">
        <div className="min-w-0">
          <Link
            to={`/programs/${program.program_id}`}
            className="text-sm font-semibold text-accent hover:underline"
          >
            {program.college_name} · {program.program_name}
          </Link>
          <div className="flex flex-wrap items-center gap-1.5 mt-1">
            <Badge tone={loud ? 'warn' : 'neutral'}>{BAND_LABEL[program.band]} risk</Badge>
            <Badge>{program.program_type}</Badge>
            <span className="text-[11px] text-ink-3">
              {program.deployments.length} deployment
              {program.deployments.length === 1 ? '' : 's'}
            </span>
          </div>
        </div>
        {/* The score is the engine's, shown verbatim. It is a sum of severity
            weights and is meant to be reconcilable line by line. */}
        <span className="text-xs tabular-nums text-ink-3 shrink-0">score {program.score}</span>
      </div>

      {program.escalations.length > 0 && (
        <div className="px-4 py-3 border-b border-line-soft space-y-2">
          <h3 className="text-xs font-semibold text-ink-2">
            <HelpTip term={`SLA escalations (${program.escalations.length})`}>
              An SLA is a service level we hold ourselves to — "attendance is marked within
              two days", say. When a stored figure crosses one of those thresholds the
              Escalation Engine names who should be looking at it. The thresholds are a
              shipped rule table, not a model's opinion, so the same numbers always escalate
              the same way.
            </HelpTip>
          </h3>
          <ul className="space-y-2">
            {program.escalations.map((e) => (
              <EscalationRow key={e.code} escalation={e} />
            ))}
          </ul>
        </div>
      )}

      <ul className="divide-y divide-line-soft">
        {program.deployments.map((d) => (
          <DeploymentRow key={d.deployment_id} deployment={d} />
        ))}
      </ul>
    </Card>
  )
}

function EscalationRow({ escalation }: { escalation: Escalation }) {
  return (
    <li className="text-xs">
      <div className="flex flex-wrap items-center gap-1.5">
        <SeverityBadge severity={escalation.severity} />
        <span className="text-ink font-medium">{codeLabel(escalation.code)}</span>
        <span className="text-ink-3">
          → {TIER_LABEL[escalation.resolved_tier]}
          {escalation.recipient_count > 0 && ` (${escalation.recipient_count})`}
        </span>
        {escalation.climbed && (
          <Badge tone="warn">climbed from {TIER_LABEL[escalation.requested_tier]}</Badge>
        )}
        {escalation.unrouted && <Badge tone="warn">nobody assigned</Badge>}
      </div>
      {/* Generated from the rule row itself, so it cannot disagree with the
          threshold it describes. Shown verbatim for that reason — and NOT in
          `flame`, because "generated" here means string-formatted by the rule,
          not written by a model. */}
      <p className="text-ink-3 mt-0.5 leading-snug">{escalation.reason}</p>
      {/* The stable identifier for the rule that fired. Monospaced because it is
          an id people paste into a thread when they ask why this escalated. */}
      <MonoValue className="text-[10px] text-ink-3" title="SLA rule code">
        {escalation.code}
      </MonoValue>
    </li>
  )
}

function DeploymentRow({ deployment }: { deployment: DeploymentRisk }) {
  const clean = deployment.anomalies.length === 0
  return (
    <li className="px-4 py-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-sm text-ink leading-snug">
            {deployment.trainer_name} · {deployment.batch_name}
          </p>
          <p className="text-[11px] text-ink-3 mt-0.5">
            {fmtDate(deployment.period_start)} – {fmtDate(deployment.period_end)} · marked through{' '}
            {fmtDate(deployment.elapsed_through)}
          </p>
        </div>
        <span className="text-xs tabular-nums text-ink-3 shrink-0">{deployment.score}</span>
      </div>

      {clean ? (
        <p className="text-xs text-ink-3 mt-1.5">No anomalies in this window.</p>
      ) : (
        <ul className="mt-2 space-y-1.5">
          {deployment.anomalies.map((a) => (
            <li key={a.code} className="text-xs">
              <div className="flex flex-wrap items-center gap-1.5">
                <SeverityBadge severity={a.severity} />
                <span className="text-ink font-medium">{codeLabel(a.code)}</span>
              </div>
              <p className="text-ink-3 mt-0.5 leading-snug">{a.message}</p>
            </li>
          ))}
        </ul>
      )}
    </li>
  )
}

function SeverityBadge({ severity }: { severity: AnomalySeverity }) {
  return (
    <Badge tone={severity === 'info' ? 'neutral' : 'warn'}>{SEVERITY_LABEL[severity]}</Badge>
  )
}
