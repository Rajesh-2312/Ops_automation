import { useEffect, useMemo, useState } from 'react'
import { PAGE, bounded, emptyBound, usePageLimit } from '../lib/bounds'
import { useMutation, useQuery } from '@tanstack/react-query'
import { supabase, errorMessage } from '../lib/supabase'
import { qk } from '../lib/queryKeys'
import {
  RATE_BASIS_LABEL,
  type Deployment,
  type ProgramType,
  type RateBasis,
} from '../lib/types'
import {
  BANK_RAIL_CODES,
  SHEET_LABEL,
  VALIDATION_REMEDY,
  downloadSheet,
  isForbidden,
  validatePayout,
  type DownloadedSheet,
  type PayoutPreview,
  type PayoutValidationRequest,
  type SheetKind,
  type ValidationCode,
  type ValidationIssue,
  type ValidationReport,
} from '../lib/payouts'
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
  KeyValue,
  Loading,
  MonoValue,
  PageIntro,
  SectionTitle,
  Select,
  Spinner,
  Td,
  Textarea,
  Th,
} from '../components/ui'

/* --------------------------------------------------------------------------
   Dates. Built by hand, never through toISOString(), which converts to UTC and
   would slide an IST date back a day — the same reason AttendancePage does it
   this way. A payout period is a Postgres DATE and must be the calendar day the
   human picked.
-------------------------------------------------------------------------- */

const pad = (n: number) => String(n).padStart(2, '0')
const ymd = (year: number, month0: number, day: number) => `${year}-${pad(month0 + 1)}-${pad(day)}`
const daysInMonth = (year: number, month0: number) => new Date(year, month0 + 1, 0).getDate()

function currentMonthKey(): string {
  const now = new Date()
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}`
}

function parseMonthKey(key: string): { year: number; month0: number } {
  const [y, m] = key.split('-')
  return { year: Number(y), month0: Number(m) - 1 }
}

function shiftMonth(key: string, delta: number): string {
  const { year, month0 } = parseMonthKey(key)
  const d = new Date(year, month0 + delta, 1)
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}`
}

function monthLabel(key: string): string {
  const { year, month0 } = parseMonthKey(key)
  return new Date(year, month0, 1).toLocaleDateString(undefined, {
    month: 'long',
    year: 'numeric',
  })
}

/** The last twelve months plus the next, newest first — as on Attendance. */
function monthOptions(): string[] {
  const base = currentMonthKey()
  return Array.from({ length: 14 }, (_, i) => shiftMonth(base, 1 - i))
}

function monthBounds(key: string): { start: string; end: string } {
  const { year, month0 } = parseMonthKey(key)
  return { start: ymd(year, month0, 1), end: ymd(year, month0, daysInMonth(year, month0)) }
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

/** The claim side of a payout — everything §7 calls an assertion, not a fact. */
interface ClaimForm {
  ta_da: string
  accommodation: string
  travel_reimbursement: string
  deductions: string
  tds_rate: string
  overrideRate: boolean
  rate: string
  rate_basis: RateBasis
}

const EMPTY_CLAIM: ClaimForm = {
  ta_da: '',
  accommodation: '',
  travel_reimbursement: '',
  deductions: '',
  tds_rate: '',
  overrideRate: false,
  rate: '',
  rate_basis: 'per_month',
}

/**
 * Payouts — compute one trainer-month, and find out whether it may be paid.
 *
 * THIS IS A COMMERCIALS SURFACE, AND THE STRICTEST ONE IN THE APP. A payout is
 * a rate, an earned figure and a bank instruction in one object, so §4 puts it
 * behind can_see_commercials(): Senior Manager and Manager. OpsRoot omits the
 * nav link for an LDE Executive and this component draws no form for them, and
 * BOTH of those are cosmetic. What actually stops them is
 * `_require_payout_persona()` in app/api/payouts.py, which 403s all four
 * endpoints before a single row is read. The rule for this screen is therefore:
 * never present a control whose only outcome is a 403 — and if one arrives
 * anyway, say what it means rather than printing a status code.
 *
 * NO MONEY IS COMPUTED HERE (R2/R7). Every amount arrives from the engine as a
 * STRING and is rendered as one; `fmtAmount` groups digits and returns text.
 * Nothing on this page parses an amount into a JS number, and the amount in
 * words is the one Python generated — a second renderer written in TypeScript
 * could disagree with the invoice, and disagreeing with the invoice is the whole
 * failure mode.
 *
 * BLOCKING AND WARNING ARE NOT THE SAME THING (§7), and they are not drawn the
 * same way. A blocking issue means the cycle cannot leave DRAFT at all and no
 * amount of typing clears it; the fix is on another screen, or in the schema. A
 * warning is permitted with a stated reason, so each one gets a text box and the
 * verdict is recomputed by the SERVER once the reason is filled in — `can_submit`
 * is never derived here.
 *
 * NOTHING ON THIS SCREEN APPROVES OR RELEASES (R4). Both sheets download as
 * DRAFT, the response headers say so, and the banner beside a blocked download
 * says so louder.
 */
export function PayoutsPage() {
  const { canSeeCommercials } = useAuth()

  const [deploymentId, setDeploymentId] = useState('')
  const [month, setMonth] = useState(currentMonthKey)
  const [periodStart, setPeriodStart] = useState(() => monthBounds(currentMonthKey()).start)
  const [periodEnd, setPeriodEnd] = useState(() => monthBounds(currentMonthKey()).end)
  const [claim, setClaim] = useState<ClaimForm>(EMPTY_CLAIM)
  const [reasons, setReasons] = useState<Partial<Record<ValidationCode, string>>>({})

  /**
   * The request the server last answered. Held separately from the form because
   * POST /payouts/validate writes an audit row on every call — refetching on
   * each keystroke would fill the audit log with a Manager's typing. So the run
   * is explicit, and the screen says plainly when the form has moved on from the
   * verdict on display.
   */
  const [committed, setCommitted] = useState<PayoutValidationRequest | null>(null)
  const [downloaded, setDownloaded] = useState<DownloadedSheet | null>(null)

  // BOUNDED at the shared default page size, so this screen reads the same
  // cache entry Deployments and Attendance fill (see the note on
  // `qk.deployments.list`). `enabled` still skips the request entirely outside
  // the commercials wall — that saves a round trip; the wall itself is the
  // policy in Postgres.
  const page = usePageLimit(PAGE.deployments)

  const deploymentsQuery = useQuery({
    queryKey: qk.deployments.list(page.limit),
    enabled: canSeeCommercials,
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

  const deploymentsBound = deploymentsQuery.data ?? emptyBound<DeploymentRow>(page.limit)
  const deployments = useMemo(() => deploymentsBound.rows, [deploymentsBound.rows])
  const selected = deployments.find((d) => d.id === deploymentId) ?? null
  const programType = selected?.batches?.programs?.type ?? null

  // Changing the month resets the period to the whole of it. The two date inputs
  // stay editable because a mid-month deployment is a real case — the July
  // 26–31 regression fixture is exactly one.
  useEffect(() => {
    const bounds = monthBounds(month)
    setPeriodStart(bounds.start)
    setPeriodEnd(bounds.end)
  }, [month])

  const draft = useMemo<PayoutValidationRequest | null>(() => {
    if (!deploymentId || !periodStart || !periodEnd) return null
    return buildRequest(deploymentId, periodStart, periodEnd, claim, reasons)
  }, [deploymentId, periodStart, periodEnd, claim, reasons])

  const validation = useQuery({
    queryKey: qk.payouts.validate(JSON.stringify(committed)),
    enabled: committed !== null,
    // The endpoint is a POST that writes an audit row. Retrying it, or
    // refetching it when the window regains focus, would multiply those rows for
    // no new information — the answer only changes when the request does.
    retry: false,
    refetchOnWindowFocus: false,
    staleTime: Infinity,
    queryFn: () => validatePayout(committed as PayoutValidationRequest),
  })

  const sheet = useMutation({
    mutationFn: (kind: SheetKind) =>
      downloadSheet(kind, stripReasons(committed as PayoutValidationRequest)),
    onSuccess: setDownloaded,
  })

  /** The form has moved on from the verdict on screen. */
  const stale =
    committed !== null && draft !== null && JSON.stringify(draft) !== JSON.stringify(committed)

  const report = validation.data?.report ?? null
  const preview = validation.data?.preview ?? null

  function run() {
    if (!draft) return
    setDownloaded(null)
    setCommitted(draft)
  }

  // --- The wall ------------------------------------------------------------
  // Drawn before anything else, and with no form at all: every control on this
  // page posts to an endpoint that would 403 for this persona, and offering one
  // would be presenting an action that cannot work.
  if (!canSeeCommercials) {
    return (
      <>
        <PageHeader
          title="Payouts"
          subtitle="Commercial — restricted to Senior Managers and Managers"
          purpose="Where trainer pay for one month is worked out and checked. Your role does not cover commercial data, so this screen has nothing on it for you — that is the design, not a fault."
        />
        <Page>
          <div className="max-w-3xl">
            <InfoNote>
              A payout is a rate, an earned figure and a payment instruction in one object, so
              CLAUDE.md §4 puts it behind <code>can_see_commercials()</code> — Senior Manager and
              Manager only. All four payout endpoints refuse this account before they read a row,
              so there is deliberately no form here rather than a form that would fail. The nav
              link is omitted for the same reason it is worth saying so on the page: a 404 would
              imply the screen does not exist, when the honest answer is that the data is not
              yours.
            </InfoNote>
          </div>
        </Page>
      </>
    )
  }

  return (
    <>
      <PageHeader
        title="Payouts"
        subtitle="One trainer, one calendar month. Computed by the engine, gated by §7, released by nobody here."
        purpose="Trainer pay for one month, worked out from the attendance on record and checked against the signed work order before anyone approves it. Nothing on this screen approves, releases or pays."
      />

      <Page>
        <div className="max-w-5xl space-y-4">
          {/* THE CHAIN, IN ORDER, BEFORE ANY FIGURE IS SHOWN.
              A reader who does not know that TDS is charged on earned and not
              on gross will read the breakdown below as an arithmetic error and
              "correct" it in a spreadsheet. The order is the whole explanation,
              so it is stated once, at the top, in the engine's own sequence
              (§6) — not inferred from the table further down. */}
          <PageIntro
            steps={[
              'Payable days, from attendance',
              'Earned, from the rate',
              'Gross, add the reimbursements',
              'TDS, off the earned figure only',
              'Net, the one rounded number',
            ]}
          >
            Pay is built in one direction and never worked backwards. The attendance marked on
            the{' '}
            <HelpTip term="deployment">
              One named trainer teaching one named batch, between a start and an end date. Pay
              always belongs to a deployment, never to a trainer on their own.
            </HelpTip>{' '}
            decides the{' '}
            <HelpTip term="payable days">
              The number of days the pay is worked out from. On a per-day CRT engagement they
              are counted <strong>up</strong> from the P marks, so an unmarked day pays nothing.
              On a monthly bCAP retainer they are counted <strong>down</strong> from the length
              of the period, so an unmarked day is paid.
            </HelpTip>
            ; the rate on the signed{' '}
            <HelpTip term="work order">
              The signed document carrying the agreed rate and the dates it covers. The payout
              period must sit inside those dates, and the rate used must match the one signed —
              both are hard gates in §7.
            </HelpTip>{' '}
            turns those into what was <strong>earned</strong>; TA&amp;DA, accommodation and
            travel reimbursement are added on top to make the <strong>gross</strong>;{' '}
            <HelpTip term="TDS">
              Tax Deducted at Source. It is charged on <strong>earned</strong> and never on
              gross, so reimbursements are excluded from its base. Default rate 0.10. This is
              why a claim of ₹15,484 earned plus ₹100 TA&amp;DA deducts 1,548 and not 1,558.
            </HelpTip>{' '}
            comes off the earned figure alone; what is left is the <strong>net</strong>, and net
            is the only figure that is rounded — everything above it keeps full precision (R6).
            Every amount here is computed in Python and arrives as text; this screen labels
            money and never adds it up.
          </PageIntro>

          {deploymentsQuery.error && (
            <ErrorNote>{errorMessage(deploymentsQuery.error)}</ErrorNote>
          )}

          {deploymentsQuery.isPending ? (
            <Loading label="Loading deployments" />
          ) : deployments.length === 0 ? (
            <Card>
              <EmptyState
                title="No deployments to pay"
                body="A payout is computed against a deployment — a trainer teaching a batch — and the attendance marked on it. Create one from the program first."
              />
            </Card>
          ) : (
            <>
              {/* --- What is being paid ------------------------------------ */}
              <Card className="p-4 space-y-4">
                {/* Outside the two <Field> labels rather than inside the
                    deployment one: `Field` renders a <label>, and a button
                    inside a label activates the labelled control when it is
                    clicked — so "Load more" would also open the select.

                    The picker decides WHOSE payout is computed. A deployment
                    beyond the bound cannot be paid from this screen, and a
                    dropdown that is quietly short reads as "no deployment on
                    record" — a wrong answer to a money question. */}
                <BoundNote
                  bound={deploymentsBound}
                  noun="deployments"
                  derived="A deployment beyond that cannot be selected below."
                  onMore={page.more}
                  step={page.step}
                />

                <div className="grid gap-3 md:grid-cols-[2fr_1fr]">
                  <Field label="Deployment">
                    <Select
                      value={deploymentId}
                      onChange={(e) => setDeploymentId(e.target.value)}
                      aria-label="Deployment"
                    >
                      <option value="">Select a trainer and batch…</option>
                      {deployments.map((d) => (
                        <option key={d.id} value={d.id}>
                          {d.trainers?.full_name ?? 'Unknown trainer'} · {d.batches?.name ?? '—'}{' '}
                          · {d.batches?.programs?.name ?? '—'} (
                          {d.batches?.programs?.type ?? 'unknown type'})
                        </option>
                      ))}
                    </Select>
                  </Field>
                  <Field label="Payout month" hint="One calendar month. The API refuses a period that straddles two.">
                    <Select value={month} onChange={(e) => setMonth(e.target.value)}>
                      {monthOptions().map((m) => (
                        <option key={m} value={m}>
                          {monthLabel(m)}
                        </option>
                      ))}
                    </Select>
                  </Field>
                </div>

                {selected && (
                  <div className="flex flex-wrap items-center gap-2 text-xs text-ink-2">
                    <Badge tone="accent">{programType}</Badge>
                    {/* Identity is the PAN, not the name (§6): it is the only key
                        present in every legacy sheet and it seeds the invoice
                        number. Mono for the same reason — an `l` read as a `1`
                        here is a payment to the wrong person. */}
                    <HelpTip term="PAN">
                      The trainer&rsquo;s Permanent Account Number, and the identity this system
                      pays against. Trainers are matched by PAN and never by name string, and
                      its first four characters seed the invoice number.
                    </HelpTip>
                    <MonoValue className="text-ink-3">{selected.trainers?.pan}</MonoValue>
                    <span>{selected.batches?.programs?.colleges?.name ?? '—'}</span>
                    <span className="text-ink-3">
                      Deployed {fmtDate(selected.start_date)} → {fmtDate(selected.end_date)}
                    </span>
                  </div>
                )}

                <div className="grid gap-3 sm:grid-cols-2">
                  <Field label="Period start">
                    <Input
                      type="date"
                      value={periodStart}
                      onChange={(e) => setPeriodStart(e.target.value)}
                    />
                  </Field>
                  <Field
                    label="Period end"
                    hint="Defaults to the whole month; narrow it for a mid-month start or end."
                  >
                    <Input
                      type="date"
                      value={periodEnd}
                      onChange={(e) => setPeriodEnd(e.target.value)}
                    />
                  </Field>
                </div>

                {programType && (
                  <InfoNote>
                    {programType === 'CRT' ? (
                      <>
                        <HelpTip term="CRT">
                          A per-day engagement. The trainer is paid for the days they were
                          there, so the days marked present are added up.
                        </HelpTip>{' '}
                        <strong>pays per day</strong> and counts payable days UP from{' '}
                        <code>P</code> marks, so an unmarked day pays nothing. Attendance
                        completeness is a <strong>hard block</strong> for this program type —
                        the cycle cannot be sent for approval while an elapsed day inside the
                        period is still unmarked.
                      </>
                    ) : (
                      <>
                        <HelpTip term="bCAP">
                          A monthly retainer engagement. The trainer is paid for the month and
                          absences are taken off it.
                        </HelpTip>{' '}
                        <strong>pays a monthly retainer</strong>, prorated by calendar days, and
                        counts DOWN from the period length — an unmarked day is <em>paid</em>.
                        Weekends and college holidays are paid too; the retainer absorbs them.
                        Attendance completeness is a <strong>warning</strong> here, not a block,
                        and it needs a stated reason.
                      </>
                    )}
                  </InfoNote>
                )}
              </Card>

              {/* --- The claim -------------------------------------------- */}
              <Card className="p-4">
                <SectionTitle
                  title="Claimed amounts"
                  subtitle="Reimbursements and deductions are claims, not facts — no system of record holds them yet, so they are typed here and travel to the engine as strings."
                />

                <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                  <AmountField
                    label="TA & DA"
                    value={claim.ta_da}
                    onChange={(v) => setClaim((c) => ({ ...c, ta_da: v }))}
                  />
                  <AmountField
                    label="Accommodation"
                    value={claim.accommodation}
                    onChange={(v) => setClaim((c) => ({ ...c, accommodation: v }))}
                  />
                  <AmountField
                    label="Travel reimbursement"
                    value={claim.travel_reimbursement}
                    onChange={(v) => setClaim((c) => ({ ...c, travel_reimbursement: v }))}
                  />
                  <AmountField
                    label="Deductions"
                    value={claim.deductions}
                    onChange={(v) => setClaim((c) => ({ ...c, deductions: v }))}
                  />
                </div>

                <div className="grid gap-3 sm:grid-cols-2 mt-3">
                  <Field
                    label="TDS rate"
                    hint="Blank uses the 0.10 default (§6). Override only for a lower-deduction certificate."
                  >
                    <Input
                      inputMode="decimal"
                      pattern="^\d*(\.\d+)?$"
                      placeholder="0.10"
                      value={claim.tds_rate}
                      onChange={(e) => setClaim((c) => ({ ...c, tds_rate: e.target.value }))}
                      className="tabular-nums"
                    />
                  </Field>
                </div>

                <div className="mt-4 pt-3 border-t border-line">
                  <label className="inline-flex items-center gap-2 text-xs font-medium text-ink-2">
                    <input
                      type="checkbox"
                      checked={claim.overrideRate}
                      onChange={(e) =>
                        setClaim((c) => ({ ...c, overrideRate: e.target.checked }))
                      }
                    />
                    Assert a rate instead of reading the signed work order
                  </label>
                  <p className="text-xs text-ink-3 mt-1 leading-relaxed">
                    An asserted rate does <strong>not</strong> override the work order. It is
                    handed to the <code>rate_mismatch_with_work_order</code> gate and blocks if
                    the two disagree — the signed document wins, every time.
                  </p>

                  {claim.overrideRate && (
                    <div className="grid gap-3 sm:grid-cols-2 mt-3">
                      <AmountField
                        label="Asserted rate (INR)"
                        value={claim.rate}
                        onChange={(v) => setClaim((c) => ({ ...c, rate: v }))}
                      />
                      <Field label="Rate basis" hint="Required with a rate — never inferred from the program type.">
                        <Select
                          value={claim.rate_basis}
                          onChange={(e) =>
                            setClaim((c) => ({ ...c, rate_basis: e.target.value as RateBasis }))
                          }
                        >
                          <option value="per_month">{RATE_BASIS_LABEL.per_month}</option>
                          <option value="per_day">{RATE_BASIS_LABEL.per_day}</option>
                        </Select>
                      </Field>
                    </div>
                  )}
                </div>

                <div className="flex flex-wrap items-center justify-end gap-3 mt-4 pt-3 border-t border-line">
                  {stale && (
                    <span className="text-xs text-warn-ink">
                      The form has changed since this verdict was computed.
                    </span>
                  )}
                  <Button
                    variant="primary"
                    disabled={!draft || validation.isFetching}
                    onClick={run}
                  >
                    {validation.isFetching && <Spinner />}
                    {committed ? 'Re-run the §7 gates' : 'Compute and check §7'}
                  </Button>
                </div>
              </Card>

              {/* --- The answer -------------------------------------------- */}
              {validation.error && <PayoutError error={validation.error} />}

              {validation.isFetching && !preview && <Loading label="Running the engine" />}

              {preview && report && (
                <>
                  <VerdictBanner report={report} stale={stale} />
                  <BreakdownCard
                    preview={preview}
                    /* stale marks the figures as belonging to the previous run */
                    stale={stale}
                  />
                  <GateList
                    report={report}
                    reasons={reasons}
                    onReason={(code, text) =>
                      setReasons((r) => ({ ...r, [code]: text }))
                    }
                    onRecheck={run}
                    stale={stale}
                  />
                  <SheetsCard
                    busy={sheet.isPending}
                    error={sheet.error}
                    downloaded={downloaded}
                    stale={stale}
                    onDownload={(kind) => sheet.mutate(kind)}
                  />
                </>
              )}
            </>
          )}
        </div>
      </Page>
    </>
  )
}

// --- Request assembly --------------------------------------------------------

/**
 * Turn the form into the request body. Amounts go out as the STRINGS the user
 * typed: `Money` on the API side runs `app.domain.money.money()` as a
 * BeforeValidator and 422s a JSON float, which is exactly the guard we want —
 * `JSON.stringify(Number("100.50"))` would send `100.5` and be refused.
 *
 * A blank field is omitted rather than sent as "0", so the server's own default
 * applies and the request says nothing it was not told.
 */
function buildRequest(
  deploymentId: string,
  periodStart: string,
  periodEnd: string,
  claim: ClaimForm,
  reasons: Partial<Record<ValidationCode, string>>,
): PayoutValidationRequest {
  const amount = (raw: string) => {
    const text = raw.trim()
    return text === '' ? undefined : text
  }

  const stated = Object.fromEntries(
    Object.entries(reasons).filter(([, text]) => (text ?? '').trim() !== ''),
  ) as Partial<Record<ValidationCode, string>>

  return {
    deployment_id: deploymentId,
    period_start: periodStart,
    period_end: periodEnd,
    ta_da: amount(claim.ta_da),
    accommodation: amount(claim.accommodation),
    travel_reimbursement: amount(claim.travel_reimbursement),
    deductions: amount(claim.deductions),
    tds_rate: amount(claim.tds_rate),
    // rate and rate_basis must be supplied together or not at all — the API
    // rejects one without the other rather than guessing the basis.
    ...(claim.overrideRate && claim.rate.trim() !== ''
      ? { rate: claim.rate.trim(), rate_basis: claim.rate_basis }
      : {}),
    ...(Object.keys(stated).length > 0 ? { stated_reasons: stated } : {}),
  }
}

/** The sheet endpoints take a `SheetRequest`, which has no `stated_reasons`. */
function stripReasons(request: PayoutValidationRequest) {
  const { stated_reasons: _ignored, ...rest } = request
  void _ignored
  return rest
}

// --- Pieces ------------------------------------------------------------------

function AmountField({
  label,
  value,
  onChange,
  hint,
}: {
  label: string
  value: string
  onChange: (value: string) => void
  hint?: string
}) {
  return (
    <Field label={label} hint={hint}>
      <Input
        inputMode="decimal"
        // The typed string is sent verbatim. It is never parsed here (R7).
        pattern="^\d+(\.\d{1,2})?$"
        placeholder="0.00"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="tabular-nums"
      />
    </Field>
  )
}

function PayoutError({ error }: { error: unknown }) {
  if (isForbidden(error)) {
    return (
      <ErrorNote>
        <strong>The payout API refused this account.</strong> Payouts are commercial data and are
        restricted to Senior Managers and Managers with reach over the college (CLAUDE.md §4). If
        you believe you should have reach here, it comes from your college or cluster
        assignments, not from your role — ask an admin on the Users &amp; roles screen.
      </ErrorNote>
    )
  }
  return <ErrorNote>{errorMessage(error)}</ErrorNote>
}

/**
 * The headline. Three states, drawn differently on purpose:
 *
 *   blocked            red. The cycle cannot leave DRAFT. Nothing typed on this
 *                      screen changes that.
 *   warnings unstated  amber. Permitted — with a reason, which is missing.
 *   can_submit         green. And still not submitted: this endpoint reports,
 *                      it does not transition (R4).
 *
 * `can_submit` is read straight off the response. It is never inferred from the
 * lists, because inferring it would be a second implementation of §7 living in a
 * browser.
 */
function VerdictBanner({ report, stale }: { report: ValidationReport; stale: boolean }) {
  const tone = report.is_blocked
    ? 'border-bad/35 bg-bad-wash text-bad-ink'
    : report.can_submit
      ? 'border-good/35 bg-good-wash text-good-ink'
      : 'border-warn/35 bg-warn-wash text-warn-ink'

  const headline = report.is_blocked
    ? `Blocked — ${count(report.blocking.length, 'issue')} must be cleared before this payout can be submitted for approval.`
    : report.can_submit
      ? 'Clear. Every §7 gate passes and every warning has a stated reason.'
      : `Not submittable — ${count(report.reasons_missing.length, 'warning')} still needs a stated reason.`

  return (
    <div className={`rounded-xl border px-4 py-3 ${tone}`} role="status">
      <p className="text-sm font-semibold">{headline}</p>
      <p className="text-xs mt-1 opacity-90 leading-relaxed">
        {report.is_blocked
          ? 'A blocking gate is not a formality and cannot be waived with a note. Each one below says where the fix lives.'
          : report.can_submit
            ? 'Submitting for approval is a separate, human action and it does not live on this screen — this endpoint reports whether the cycle could move, and moves nothing (CLAUDE.md R4).'
            : 'A warning is permitted, but §7 requires a stated reason. Type one into every box below and re-run — the verdict is recomputed by the server, not here.'}
        {stale && ' This verdict describes the previous run; the form has changed since.'}
      </p>
    </div>
  )
}

function count(n: number, noun: string): string {
  return `${n} ${noun}${n === 1 ? '' : 's'}`
}

/** The §6 chain, in the order the engine computes it. Read-only, all strings. */
function BreakdownCard({
  preview,
  stale,
}: {
  preview: PayoutPreview
  stale: boolean
}) {
  const b = preview.breakdown
  const a = preview.attendance
  const perMonth = b.rate_basis === 'per_month'

  return (
    <Card className={`p-4 ${stale ? 'opacity-60' : ''}`}>
      <SectionTitle
        title={`${preview.trainer_name} · ${monthTitle(preview.payout_month)}`}
        subtitle={`${preview.college_name} · ${preview.program_name} · ${fmtDate(
          preview.period_start,
        )} → ${fmtDate(preview.period_end)}`}
        action={<Badge>{preview.artifact_state}</Badge>}
      />

      <div className="grid gap-4 lg:grid-cols-2">
        <div className="overflow-x-auto scroll-slim">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-line">
                <Th>Line</Th>
                <Th className="text-right">Amount</Th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line-soft">
              <Line
                label="Rate"
                value={fmtAmount(b.rate)}
                note={`${RATE_BASIS_LABEL[b.rate_basis]} · from the ${
                  b.rate_source === 'work_order' ? 'signed work order' : 'rate you asserted'
                }`}
              />
              <Line
                label="Rate per day"
                value={fmtAmount(b.rate_per_day)}
                note={
                  perMonth
                    ? 'DISPLAY ONLY. It is rate ÷ days in month and it did not enter the calculation below — rate_per_day × payable days will NOT equal Earned, and that is correct, not a bug (§6).'
                    : 'The day rate itself, on a per-day engagement.'
                }
              />
              <Line
                label="Payable days"
                value={`${b.payable_days} of ${b.days_in_month}`}
                note={
                  a.program_type === 'CRT'
                    ? 'Counted UP from P marks.'
                    : 'Counted DOWN from the period length.'
                }
              />
              <Line label="Earned" value={fmtAmount(b.earned)} strong />
              <Line label="Reimbursements" value={fmtAmount(b.reimbursements)} />
              <Line label="Gross" value={fmtAmount(b.gross)} strong />
              <Line
                label={`TDS @ ${b.tds_rate}`}
                value={`− ${fmtAmount(b.tds)}`}
                note="Levied on Earned, never on Gross — which is why it excludes the reimbursements above (§6)."
              />
              <Line label="Deductions" value={`− ${fmtAmount(b.deductions)}`} />
              <Line
                label="Net (unrounded)"
                value={fmtAmount(b.net_unrounded)}
                note="Kept beside the rounded figure as the evidence that rounding happened once, at the end (R6)."
              />
              <Line label="Net pay" value={fmtAmount(b.net)} strong />
            </tbody>
          </table>
        </div>

        <div className="space-y-3">
          <div className="rounded-lg border border-line bg-surface-2 px-3 py-2.5">
            <p className="text-xs text-ink-3">Net pay, in words</p>
            <p className="text-sm text-ink mt-1 leading-relaxed">{b.net_in_words}</p>
            <p className="text-[11px] text-ink-3 mt-2 leading-relaxed">
              Generated in Python in Indian numbering and displayed verbatim. Nothing here
              re-renders it — words and figure cannot be allowed to disagree.
            </p>
          </div>

          <div className="rounded-lg border border-line bg-surface-2 px-3 py-2.5">
            <KeyValue
              className="py-1"
              label={
                <HelpTip term="Trainer PAN">
                  The Permanent Account Number, and the identity a payout is made against.
                  Trainers are matched by PAN and never by name string — two people can share a
                  name, and one of them would be paid twice.
                </HelpTip>
              }
            >
              <MonoValue>{preview.trainer_pan}</MonoValue>
            </KeyValue>
            <KeyValue
              className="py-1"
              label={
                <HelpTip term="Invoice number">
                  Built as <code>PAN[0:4]/FY/MONseq</code> — the first four characters of the
                  PAN, the financial year, then the month and a sequence, e.g.{' '}
                  <span className="font-mono">BCDP/26-27/JUL1</span>. The financial year runs
                  April to March and is taken from the payout month, so a July 2026 payout sits
                  in 26-27.
                </HelpTip>
              }
            >
              {preview.invoice_number === null ? (
                <span className="text-ink-3">None — the PAN cannot seed one</span>
              ) : (
                <MonoValue>{preview.invoice_number}</MonoValue>
              )}
            </KeyValue>
            <p className="text-[11px] text-ink-3 leading-relaxed mt-1.5">
              Provisional. Nothing is reserved by computing it — the{' '}
              <code>(pan, fiscal_year, month, seq)</code> unique constraint in Postgres is what
              actually issues a number.
            </p>
          </div>

          <div className="rounded-lg border border-line bg-surface-2 px-3 py-2.5">
            <KeyValue className="py-1" label="Program type" value={a.program_type} />
            <KeyValue className="py-1" label="Days in period" value={String(a.period_days)} />
            <KeyValue className="py-1" label="Marked" value={String(a.marked)} />
            <KeyValue
              className="py-1"
              label={
                <HelpTip term="Unmarked">
                  A day inside the period carrying no attendance row at all. It is not a
                  neutral gap: on CRT it pays nothing and quietly underpays the trainer, on
                  bCAP it is paid in full and quietly pays for a day nobody confirmed.
                </HelpTip>
              }
              value={a.unmarked === 0 ? '0' : `${a.unmarked} — mark them on Attendance`}
            />
            <KeyValue
              className="py-1"
              label={
                <HelpTip term="Attendance complete">
                  Whether every elapsed day in the period carries a mark. Incomplete attendance
                  is a hard block on CRT and a warning needing a stated reason on bCAP.
                </HelpTip>
              }
              value={a.is_complete ? 'Yes' : 'No'}
            />
          </div>
        </div>
      </div>
    </Card>
  )
}

function monthTitle(isoDate: string): string {
  const d = new Date(`${isoDate}T00:00:00`)
  if (Number.isNaN(d.getTime())) return isoDate
  return d.toLocaleDateString(undefined, { month: 'long', year: 'numeric' })
}

function Line({
  label,
  value,
  note,
  strong = false,
}: {
  label: string
  value: string
  note?: string
  strong?: boolean
}) {
  return (
    <tr>
      <Td>
        <span className={strong ? 'font-medium text-ink' : 'text-ink-2'}>{label}</span>
        {note && <span className="block text-[11px] text-ink-3 mt-0.5 leading-relaxed">{note}</span>}
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

/* `Meta` lived here — a local label/value row that predated the kit. Every call
   site now uses `KeyValue` from ui.tsx instead, which draws the same shape but
   accepts a ReactNode label, and that is what lets the labels on this screen
   carry a HelpTip explaining what a PAN or an unmarked day actually means. */

/**
 * The §7 gates. Two lists, and they are never merged.
 *
 * Blocking issues get no input control at all — offering a reason box beside one
 * would imply it can be talked past. Warnings get a required text box each, and
 * a blank one does not count: `can_submit` comes back false until the server
 * sees a non-blank string for every code in `reasons_required`.
 */
function GateList({
  report,
  reasons,
  onReason,
  onRecheck,
  stale,
}: {
  report: ValidationReport
  reasons: Partial<Record<ValidationCode, string>>
  onReason: (code: ValidationCode, text: string) => void
  onRecheck: () => void
  stale: boolean
}) {
  const bankRailsBlocking = report.blocking.filter((i) => BANK_RAIL_CODES.includes(i.code))
  const otherBlocking = report.blocking.filter((i) => !BANK_RAIL_CODES.includes(i.code))

  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Card className="p-4">
        <SectionTitle
          title="Blocking"
          subtitle="The cycle cannot leave DRAFT while any of these stands. No reason clears one."
          action={<Badge tone={report.blocking.length > 0 ? 'warn' : 'neutral'}>
            {report.blocking.length}
          </Badge>}
        />
        {report.blocking.length === 0 ? (
          <p className="text-sm text-ink-3">Nothing blocking. Every hard gate in §7 passes.</p>
        ) : (
          <div className="space-y-2">
            {otherBlocking.map((issue) => (
              <IssueRow key={issue.code} issue={issue} />
            ))}

            {bankRailsBlocking.length > 0 && (
              <div className="rounded-lg border border-bad/30 bg-bad-wash px-3 py-2.5">
                <p className="text-sm font-medium text-bad-ink">
                  Bank rails missing —{' '}
                  {bankRailsBlocking.map((i) => i.code).join(' and ')}
                </p>
                <p className="text-xs text-ink-2 mt-1.5 leading-relaxed">
                  These two gates block <strong>every</strong> payout in the system today, for
                  every trainer, because the <code>trainers</code> table carries no bank account,
                  IFSC, branch or beneficiary-name column. It is a schema gap being closed, not
                  something you can fix by typing — and the sheets deliberately write those cells
                  empty so an un-releasable payout looks obviously incomplete rather than
                  plausible. Until the columns land, <code>can_submit</code> is false for
                  everyone. Everything else on this screen is still real: the figures, the other
                  eleven gates and both sheets all work.
                </p>
              </div>
            )}
          </div>
        )}
      </Card>

      <Card className="p-4">
        <SectionTitle
          title="Warnings"
          subtitle="Permitted — with a stated reason. A blank box does not count."
          action={<Badge tone={report.reasons_missing.length > 0 ? 'warn' : 'neutral'}>
            {report.warnings.length}
          </Badge>}
        />
        {report.warnings.length === 0 ? (
          <p className="text-sm text-ink-3">No warnings on this payout.</p>
        ) : (
          <div className="space-y-3">
            {report.warnings.map((issue) => {
              const required = report.reasons_required.includes(issue.code)
              const missing = report.reasons_missing.includes(issue.code)
              return (
                <div key={issue.code} className="space-y-1.5">
                  <IssueRow issue={issue} />
                  {required && (
                    <Field
                      label={missing ? 'Stated reason — required' : 'Stated reason'}
                      hint={
                        missing
                          ? 'The server has not seen a reason for this warning. Type one and re-run.'
                          : 'Recorded with the run.'
                      }
                    >
                      <Textarea
                        rows={2}
                        value={reasons[issue.code] ?? ''}
                        onChange={(e) => onReason(issue.code, e.target.value)}
                        placeholder="Why this payout is correct despite the warning…"
                      />
                    </Field>
                  )}
                </div>
              )
            })}

            <div className="flex justify-end pt-1">
              <Button size="sm" variant={stale ? 'primary' : 'secondary'} onClick={onRecheck}>
                Re-check with these reasons
              </Button>
            </div>
          </div>
        )}
      </Card>
    </div>
  )
}

function IssueRow({ issue }: { issue: ValidationIssue }) {
  const blocking = issue.severity === 'blocking'
  const tone = blocking
    ? 'border-bad/30 bg-bad-wash'
    : 'border-warn/30 bg-warn-wash'
  const codeTone = blocking
    ? 'text-bad-ink'
    : 'text-warn-ink'
  const remedy = VALIDATION_REMEDY[issue.code]
  const details = Object.entries(issue.detail)

  return (
    <div className={`rounded-lg border px-3 py-2.5 ${tone}`}>
      <p className={`text-[11px] font-mono uppercase tracking-wide ${codeTone}`}>{issue.code}</p>
      {/* The message is the server's, verbatim — it carries the operative
          figures that made the gate fail, and rewording it here would put a
          second account of the same rule in a second place. */}
      <p className="text-sm text-ink mt-1 leading-relaxed">{issue.message}</p>
      {details.length > 0 && (
        <dl className="mt-1.5 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5">
          {details.map(([key, value]) => (
            <div key={key} className="contents">
              <dt className="text-[11px] text-ink-3">{key}</dt>
              <dd className="text-[11px] text-ink-2 font-mono break-all">{value}</dd>
            </div>
          ))}
        </dl>
      )}
      {remedy && <p className="text-xs text-ink-2 mt-1.5 leading-relaxed">{remedy}</p>}
    </div>
  )
}

/**
 * The two legacy sheets.
 *
 * They generate even when §7 blocks — the API is explicit that refusing would
 * strand the feature while the bank-rail gap stands. So the state of what landed
 * in the Downloads folder is read off the response headers and stated here, in
 * the plainest words available: a blocked draft is not an approved sheet, and
 * Finance must not be sent one (R4).
 */
function SheetsCard({
  busy,
  error,
  downloaded,
  stale,
  onDownload,
}: {
  busy: boolean
  error: unknown
  downloaded: DownloadedSheet | null
  stale: boolean
  onDownload: (kind: SheetKind) => void
}) {
  return (
    <Card className="p-4">
      <SectionTitle
        title="Finance sheets"
        subtitle="The legacy column orders, preserved exactly — 21 columns for remuneration, 34 for the invoice. Both download as DRAFT."
      />

      {stale && (
        <div className="mb-3">
          <InfoNote>
            The form has changed since the last run. A sheet downloaded now would be generated
            from the last <em>committed</em> request, not from what is on screen — re-run first.
          </InfoNote>
        </div>
      )}

      <div className="flex flex-wrap gap-2">
        <Button disabled={busy} onClick={() => onDownload('remuneration')}>
          {busy && <Spinner />}
          Download remuneration sheet
        </Button>
        <Button disabled={busy} onClick={() => onDownload('invoice')}>
          {busy && <Spinner />}
          Download invoice sheet
        </Button>
      </div>

      {error ? (
        <div className="mt-3">
          <PayoutError error={error} />
        </div>
      ) : null}

      {downloaded && (
        <div className="mt-3">
          {downloaded.blocked ? (
            <ErrorNote>
              <strong>
                {SHEET_LABEL[downloaded.kind]} downloaded as a BLOCKED {downloaded.artifactState}.
              </strong>{' '}
              <code>{downloaded.filename}</code> — the <code>_DRAFT</code> suffix is part of the
              filename the server chose. It failed{' '}
              {downloaded.blockingCodes.length > 0
                ? downloaded.blockingCodes.join(', ')
                : 'at least one §7 gate'}
              , so it cannot be approved and must not be sent to Finance or to the trainer. Cells
              with no system of record behind them — the bank rails — are written empty on
              purpose, so this file looks incomplete rather than plausible.
            </ErrorNote>
          ) : (
            <InfoNote>
              <strong>
                {SHEET_LABEL[downloaded.kind]} downloaded as {downloaded.artifactState}.
              </strong>{' '}
              <code>{downloaded.filename}</code> — §7 raised no blocking issue. It is still a
              draft: approval and release are separate human actions with separate audit rows,
              and neither one happens on this screen.
            </InfoNote>
          )}
        </div>
      )}
    </Card>
  )
}
