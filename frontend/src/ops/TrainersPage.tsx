import { useEffect, useMemo, useState, type FormEvent } from 'react'
import { PAGE, bounded, emptyBound, usePageLimit } from '../lib/bounds'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { supabase, errorMessage, unwrap } from '../lib/supabase'
import { qk } from '../lib/queryKeys'
import {
  DOC_STATUSES,
  DOC_STATUS_LABEL,
  ERM_STATUS_LABEL,
  TRAINER_TYPE_LABEL,
  type DocStatus,
  type ErmStatus,
  type Trainer,
  type TrainerBankAccount,
  type TrainerType,
} from '../lib/types'
import { useAuth } from '../auth/AuthProvider'
import { Page, PageHeader } from '../components/AppShell'
import {
  Badge,
  BoundNote,
  Button,
  Card,
  DocStatusPill,
  EmptyState,
  ErrorNote,
  Field,
  FilterChip,
  fmtDate,
  HelpTip,
  InfoNote,
  Input,
  Modal,
  MonoValue,
  PageIntro,
  SearchInput,
  Select,
  TableSkeleton,
  Td,
  Th,
  Toolbar,
} from '../components/ui'

/** PAN is AAAAA9999A. The DB enforces length and upper-case; this mirrors the
 *  full shape so a typo is caught before the round trip. The database remains
 *  the authority — this only saves a failed request. */
const PAN_PATTERN = /^[A-Z]{5}[0-9]{4}[A-Z]$/

/** RBI shape: four letters, the reserved zero, six alphanumerics. The database
 *  asserts only length-11 and upper-case (migration 1400, same split as PAN);
 *  `gate_ifsc` asserts this full shape at payout, where a rejection can carry an
 *  explanation. Mirrored here so a typo is caught before the round trip. */
const IFSC_PATTERN = /^[A-Z]{4}0[A-Z0-9]{6}$/
const ACCOUNT_PATTERN = /^[0-9]+$/

const ERM_TONE: Record<ErmStatus, 'neutral' | 'accent' | 'warn'> = {
  not_pushed: 'neutral',
  pending: 'warn',
  synced: 'accent',
  stale: 'warn',
  failed: 'warn',
}

/**
 * The trainer roster.
 *
 * IDENTITY IS PAN (CLAUDE.md §6). It is `not null unique` on the table, it seeds
 * the invoice number as PAN[0:4], and it is the only stable key present in every
 * legacy sheet. Never match a trainer by name string: "VEMA PRUDHVI SAI" and
 * "Vema Prudhvi Sai" become two trainers, two invoice sequences and a duplicate
 * payout nobody notices until reconciliation. That is why PAN is displayed in
 * the identity column rather than tucked into a detail panel, and why the search
 * box below matches on it.
 *
 * WHO MAY WRITE HERE (migration 0400):
 *
 *   trainers_sourcing_all       — senior_manager | manager, whole roster
 *   trainers_lde_select_deployed — LDE Executive, SELECT only, and only trainers
 *                                  deployed to a college they reach
 *
 * The roster is deliberately NOT college-scoped for the sourcing personas:
 * sourcing happens before any deployment exists, so a policy requiring reach
 * would make it impossible to record the trainer you are about to deploy.
 *
 * NOTE the role test below is `senior_manager | manager` spelled out, NOT
 * `canSeeCommercials`. The two name the same personas today and mean different
 * things — one is "may see money", the other is "owns the trainer pipeline".
 * Reusing the money predicate for a non-money purpose is how a wall gets moved
 * by accident when the personas later diverge. The migration makes the same
 * point in the same words.
 */
export function TrainersPage() {
  const { profile, canSeeCommercials } = useAuth()
  const canManageRoster = profile?.role === 'senior_manager' || profile?.role === 'manager'

  const queryClient = useQueryClient()
  const [editing, setEditing] = useState<Trainer | null>(null)
  const [creating, setCreating] = useState(false)
  const [railsFor, setRailsFor] = useState<Trainer | null>(null)
  const [search, setSearch] = useState('')
  const [woFilter, setWoFilter] = useState<DocStatus | 'all'>('all')
  const [missingRailsOnly, setMissingRailsOnly] = useState(false)

  // BOUNDED. The live database already holds 1,026 trainers, and the roster is
  // deliberately NOT college-scoped for the sourcing personas (see the header),
  // so a Manager's unbounded read is the whole org — which is the single
  // largest list this console can ask for today.
  const page = usePageLimit(PAGE.trainers)

  const trainersQuery = useQuery({
    queryKey: qk.trainers.list(page.limit),
    queryFn: () =>
      bounded<Trainer>(page.limit, (rows) =>
        supabase.from('trainers').select('*').order('full_name').limit(rows),
      ),
  })

  // Rails are a SEPARATE query, not a join on the roster select, and that is
  // deliberate. `trainers` is readable by an LDE Executive; `trainer_bank_accounts`
  // is not. Embedding it would make the roster query itself commercial, and the
  // day PostgREST changes how an embed behaves under a failing policy, the
  // roster would break for the persona that is entitled to it. Two queries, two
  // walls, and the rails one simply returns nothing for a persona behind the
  // commercials wall — `enabled` below only saves a round trip.
  //
  // BOUNDED SEPARATELY, and that is what makes the coverage logic below
  // necessary. `trainers` is ordered by name and `trainer_bank_accounts` by
  // trainer_id, so when EITHER read is cut the two bounds cover different
  // subsets and the join has holes — a trainer on screen whose rails simply
  // did not arrive is indistinguishable, in the map, from a trainer with no
  // rails on file. The second of those is a §7 blocking gate: "Missing" here
  // means every future payout for that person stops before PENDING_APPROVAL.
  // So the "Missing" verdict is only rendered when BOTH reads were complete.
  const railsQuery = useQuery({
    queryKey: qk.bankRails.list(PAGE.bankRails),
    enabled: canSeeCommercials,
    queryFn: () =>
      bounded<TrainerBankAccount>(PAGE.bankRails, (rows) =>
        supabase.from('trainer_bank_accounts').select('*').order('trainer_id').limit(rows),
      ),
  })

  const railsByTrainer = useMemo(() => {
    const map = new Map<string, TrainerBankAccount>()
    for (const r of railsQuery.data?.rows ?? []) map.set(r.trainer_id, r)
    return map
  }, [railsQuery.data])

  const trainersBound = trainersQuery.data ?? emptyBound<Trainer>(page.limit)
  const trainers = useMemo(() => trainersBound.rows, [trainersBound.rows])

  // §7 blocks a payout on a missing bank account or IFSC, so "no rails on file"
  // is not a cosmetic gap — it is every future payout for that trainer stuck
  // before PENDING_APPROVAL. Counted, and offered as a filter, so it is visible
  // at a glance rather than discovered one payout at a time.
  // Only once the rails query has actually answered — counting before it lands
  // would flash "every trainer is missing rails", which is the most alarming
  // possible thing to say wrongly.
  //
  // AND only when neither read was truncated. A rails row outside the bound
  // would otherwise be counted as an absent rail, which reads as "this payout
  // is blocked" about a trainer whose details are on file.
  const railsComplete =
    trainersQuery.data?.truncated === false && railsQuery.data?.truncated === false
  const railsLoaded = canSeeCommercials && railsQuery.isSuccess && railsComplete
  const missingRails = railsLoaded
    ? trainers.filter((t) => !railsByTrainer.has(t.id)).length
    : 0

  /**
   * The "No bank rails" FILTER is gated on the same fact as the verdict, and it
   * has to be gated separately from the chip that sets it.
   *
   * The chip only renders while `railsLoaded`, so it cannot be switched on from
   * an incomplete read. It can, however, still be ON from a complete one when
   * the read stops being complete underneath it — a colleague files the 201st
   * trainer, the next refetch truncates, the chip disappears and `missingRailsOnly`
   * stays `true`. The list would then be silently narrowed by a map with holes
   * in it: trainers whose rails simply did not load, presented as the roster's
   * §7 blockers. That is the same wrong-because-partial failure the verdict
   * suppresses, arriving through the filter instead of through the badge.
   *
   * Derived rather than reset, so the switch comes back on by itself when the
   * read is complete again and the reader does not have to notice it went away.
   */
  const railsFilterActive = missingRailsOnly && railsLoaded

  const visible = useMemo(() => {
    const needle = search.trim().toLowerCase()
    return trainers.filter(
      (t) =>
        (woFilter === 'all' || t.work_order_status === woFilter) &&
        (!railsFilterActive || !railsByTrainer.has(t.id)) &&
        (needle === '' ||
          t.full_name.toLowerCase().includes(needle) ||
          t.pan.toLowerCase().includes(needle) ||
          (t.email ?? '').toLowerCase().includes(needle)),
    )
  }, [trainers, search, woFilter, railsFilterActive, railsByTrainer])

  const woCounts = useMemo(() => {
    const counts = Object.fromEntries(DOC_STATUSES.map((s) => [s, 0])) as Record<
      DocStatus,
      number
    >
    for (const t of trainers) counts[t.work_order_status] += 1
    return counts
  }, [trainers])

  // §10: a record that changed after its ERM sync is 'stale' and must be
  // requeued. Surfaced as a count because the whole point of drift detection is
  // that somebody sees the drift.
  const needsErm = trainers.filter(
    (t) => t.erm_status === 'stale' || t.erm_status === 'failed' || t.erm_status === 'pending',
  ).length

  function close() {
    setCreating(false)
    setEditing(null)
  }

  const subtitleParts = [`${trainers.length}${trainersBound.truncated ? '+' : ''} on the roster`]
  if (needsErm > 0) subtitleParts.push(`${needsErm} awaiting an ERM sync`)
  if (missingRails > 0) subtitleParts.push(`${missingRails} with no bank rails`)

  return (
    <>
      <PageHeader
        title="Trainers"
        purpose="Everyone byteXL engages to teach, and whether their paperwork has got far enough to pay them. Trainers are records here, not users — nobody on this list signs in."
        subtitle={subtitleParts.join(' · ')}
        actions={
          <Button
            variant="primary"
            size="sm"
            disabled={!canManageRoster}
            title={
              canManageRoster
                ? undefined
                : 'The roster is maintained by Senior Managers and Managers. You see the trainers deployed to your campus.'
            }
            onClick={() => setCreating(true)}
          >
            New trainer
          </Button>
        }
      />

      <Page>
        {trainersQuery.error && (
          <div className="mb-4">
            <ErrorNote>{errorMessage(trainersQuery.error)}</ErrorNote>
          </div>
        )}

        <div className="mb-4">
          <PageIntro
            steps={[
              'Add the person, with their PAN',
              'Get the work order signed',
              'Create the ZOHO account and push the record to ERM',
              'File the bank account and IFSC',
              'Payouts for them can now pass their checks',
            ]}
          >
            One row per person, and the identity is the{' '}
            <HelpTip term="PAN">
              Permanent Account Number — the ten-character income-tax ID, shaped AAAAA9999A.
              It is unique on this table and the first four characters seed every invoice
              number, which is why trainers are matched on PAN and{' '}
              <strong>never on a name string</strong>: two spellings of one person become two
              records, two invoice sequences and a duplicate payment.
            </HelpTip>
            , not the name. Four things have to be on file before a payout for anyone here can
            even be submitted for approval — a signed work order covering the period, a{' '}
            <HelpTip term="ZOHO">
              The accounting system Finance actually pays from. The trainer must exist there
              before a payout for them can be submitted.
            </HelpTip>{' '}
            account, a well-formed PAN, and a bank account with its IFSC. Any one of them
            missing stops the payment before a human ever sees it.
          </PageIntro>
        </div>

        {!canManageRoster && (
          <div className="mb-4 max-w-3xl">
            <InfoNote>
              Read-only for your role, and narrowed: you see trainers deployed to a college
              you reach, not the whole roster. Rates are not on this table at all — they
              live on work orders, behind the commercials wall.
            </InfoNote>
          </div>
        )}

        {trainersQuery.isPending ? (
          <Card className="overflow-hidden">
            <TableSkeleton rows={8} cols={6} />
          </Card>
        ) : trainers.length === 0 ? (
          <Card>
            <EmptyState
              title="No trainers visible to you"
              body={
                canManageRoster
                  ? 'Add the trainers you are sourcing. PAN is required — it is the identity key and it seeds the invoice number.'
                  : 'No trainer is deployed to a college you reach yet.'
              }
              hint={
                canManageRoster
                  ? 'Nobody has been added to the roster yet. Sourcing happens before any deployment exists, so this list is the first thing to fill.'
                  : 'You see trainers deployed to a college you are assigned to. Until one is deployed there, this list stays empty — the roster itself is not yours to browse.'
              }
              action={
                canManageRoster ? (
                  <Button variant="primary" size="sm" onClick={() => setCreating(true)}>
                    Add the first trainer
                  </Button>
                ) : undefined
              }
            />
          </Card>
        ) : (
          <div className="space-y-4">
            {/* The search box filters what was LOADED, so a bounded roster
                turns "search by PAN" into a search of the first page — and
                "trainer not found" is the step before somebody creates a
                duplicate PAN identity (§6). */}
            <BoundNote
              bound={trainersBound}
              noun="trainers"
              derived="The search and the filter counts cover only those."
              onMore={page.more}
              step={page.step}
            />
            <BoundNote
              bound={railsQuery.data}
              noun="bank-rail records"
              derived="Bank-rail coverage is therefore not stated below: an absent rail here would be indistinguishable from one that simply was not loaded."
            />

            {/* `count`/`total` is the whole reason for the swap. This list runs
                to four figures, the search filters only what was LOADED, and
                "trainer not found" is the step before somebody creates a second
                PAN-keyed identity for one human (§6). A narrowed list has to
                say it is narrowed. */}
            <Toolbar>
              <SearchInput
                className="w-full sm:w-72"
                placeholder="Search name, PAN or email…"
                value={search}
                onChange={setSearch}
                count={visible.length}
                total={trainers.length}
              />
              <span className="w-px h-5 bg-line mx-1" aria-hidden />
              <FilterChip
                label="Any work order"
                count={trainers.length}
                active={woFilter === 'all'}
                onClick={() => setWoFilter('all')}
              />
              {DOC_STATUSES.map((s) => (
                <FilterChip
                  key={s}
                  label={DOC_STATUS_LABEL[s]}
                  count={woCounts[s]}
                  active={woFilter === s}
                  onClick={() => setWoFilter(woFilter === s ? 'all' : s)}
                />
              ))}
              {railsLoaded && (
                <>
                  <span className="w-px h-5 bg-line mx-1" aria-hidden />
                  <FilterChip
                    label="No bank rails"
                    count={missingRails}
                    tone="alert"
                    active={railsFilterActive}
                    onClick={() => setMissingRailsOnly((v) => !v)}
                  />
                </>
              )}
            </Toolbar>

            <Card className="overflow-hidden">
              <div className="overflow-x-auto scroll-slim">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-line">
                      <Th>Trainer</Th>
                      <Th>
                        <HelpTip term="PAN">
                          Permanent Account Number, shaped AAAAA9999A. This is the trainer's
                          identity in the system — unique on the table, and its first four
                          characters seed every invoice number. Never match a trainer by name:
                          two spellings of one person are two records and two payments.
                        </HelpTip>
                      </Th>
                      <Th>Type</Th>
                      <Th>
                        <HelpTip term="Work order">
                          The signed engagement document, kept on the Work orders screen.
                          Without a signed one covering the payout period, that period cannot
                          be paid.
                        </HelpTip>
                      </Th>
                      <Th>
                        <HelpTip term="ZOHO">
                          The accounting system Finance pays from. A payout is blocked until
                          the trainer has an account there.
                        </HelpTip>
                      </Th>
                      <Th>
                        <HelpTip term="ERM">
                          An external HR system with no API, so the sync is a human task: the
                          system generates the field pack, a named person pastes it into ERM
                          and confirms. A record edited after its sync shows as Stale and has
                          to be pushed again.
                        </HelpTip>
                      </Th>
                      {/* The whole column is absent for an LDE Executive — not
                          a disabled cell, not an empty one. A blank column that
                          only some roles can fill still tells the campus that a
                          payment instruction exists and is missing. §4 gives
                          them no commercials at all. */}
                      {canSeeCommercials && (
                        <Th>
                          <HelpTip term="Bank rails">
                            The account the money is actually sent to — account number and
                            IFSC, the eleven-character code identifying the branch. Missing or
                            malformed, and every payout for this trainer stops before it can be
                            submitted for approval.
                          </HelpTip>
                        </Th>
                      )}
                      <Th />
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-line-soft">
                    {visible.map((t) => (
                      <tr key={t.id} className="hover:bg-surface-2/60 transition">
                        <Td className="font-medium text-ink">
                          {t.full_name}
                          <span className="block text-xs font-normal text-ink-3">
                            {t.email || t.phone || 'No contact on file'}
                          </span>
                        </Td>
                        <Td className="text-ink-2">
                          <MonoValue>{t.pan}</MonoValue>
                        </Td>
                        <Td className="text-ink-2">{TRAINER_TYPE_LABEL[t.type]}</Td>
                        <Td>
                          <DocStatusPill status={t.work_order_status} />
                        </Td>
                        <Td className="text-ink-2 text-xs">
                          {t.zoho_url ? (
                            <a
                              href={t.zoho_url}
                              target="_blank"
                              rel="noreferrer"
                              className="text-accent hover:underline"
                            >
                              {t.zoho_id || 'Open'} ↗
                            </a>
                          ) : (
                            t.zoho_id || <span className="text-ink-3">Not created</span>
                          )}
                        </Td>
                        <Td>
                          <Badge tone={ERM_TONE[t.erm_status]}>
                            {ERM_STATUS_LABEL[t.erm_status]}
                          </Badge>
                          {t.erm_synced_at && (
                            <span className="block text-[11px] text-ink-3 mt-0.5">
                              synced {fmtDate(t.erm_synced_at)}
                            </span>
                          )}
                        </Td>
                        {canSeeCommercials && (
                          <Td>
                            {!railsLoaded ? (
                              <span className="text-xs text-ink-3">—</span>
                            ) : railsByTrainer.has(t.id) ? (
                              <>
                                <Badge tone="accent">On file</Badge>
                                <span className="block mt-0.5 text-ink-3">
                                  <MonoValue
                                    className="text-[11px]"
                                    title="Last four digits of the account, then the IFSC"
                                  >
                                    ····
                                    {railsByTrainer
                                      .get(t.id)!
                                      .bank_account_number.slice(-4)}{' '}
                                    · {railsByTrainer.get(t.id)!.ifsc}
                                  </MonoValue>
                                </span>
                              </>
                            ) : (
                              // Only reachable when both reads were complete
                              // (`railsLoaded`), so "Missing" is a fact about
                              // the database rather than about the page size.
                              <>
                                <Badge tone="warn">Missing</Badge>
                                <span className="block text-[11px] text-ink-3 mt-0.5">
                                  blocks every payout
                                </span>
                              </>
                            )}
                          </Td>
                        )}
                        <Td className="text-right whitespace-nowrap">
                          {canSeeCommercials && (
                            <Button size="sm" variant="ghost" onClick={() => setRailsFor(t)}>
                              Bank rails
                            </Button>
                          )}
                          <Button
                            size="sm"
                            variant="ghost"
                            disabled={!canManageRoster}
                            onClick={() => setEditing(t)}
                          >
                            Edit
                          </Button>
                        </Td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              {visible.length === 0 && (
                <EmptyState
                  title="No trainers match"
                  body="Clear the search or the work-order filter."
                  hint={
                    railsFilterActive
                      ? 'The “No bank rails” filter is on, so only trainers with no account on file would show — and every one loaded has rails filed.'
                      : 'Rows are loaded, but nothing here matches your search text and work-order filter together. The search reads name, PAN and email.'
                  }
                />
              )}
            </Card>

            <div className="max-w-3xl">
              <InfoNote>
                ERM has no API (CLAUDE.md §10), so the sync is modelled as a human task: the
                system generates the field pack, a named person pastes it, and{' '}
                <code>erm_synced_at</code> / <code>erm_synced_by</code> record who and when.
                A record edited after its sync flips to <strong>Stale</strong> and needs
                re-pushing. Field-pack generation is not built yet — this screen tracks the
                state, it does not produce the pack.
              </InfoNote>
            </div>
          </div>
        )}
      </Page>

      {canSeeCommercials && (
        <BankRailsModal
          trainer={railsFor}
          existing={railsFor ? (railsByTrainer.get(railsFor.id) ?? null) : null}
          onClose={() => setRailsFor(null)}
          onSaved={() => {
            setRailsFor(null)
            void queryClient.invalidateQueries({ queryKey: qk.bankRails.all })
          }}
        />
      )}

      <TrainerModal
        open={creating || editing !== null}
        trainer={editing}
        onClose={close}
        onSaved={() => {
          close()
          void queryClient.invalidateQueries({ queryKey: qk.trainers.all })
        }}
      />
    </>
  )
}

interface TrainerForm {
  pan: string
  full_name: string
  email: string
  phone: string
  type: TrainerType
  work_order_status: DocStatus
  zoho_id: string
  zoho_url: string
  erm_status: ErmStatus
  erm_external_id: string
  erm_url: string
}

const EMPTY_FORM: TrainerForm = {
  pan: '',
  full_name: '',
  email: '',
  phone: '',
  type: 'freelancer',
  work_order_status: 'not_started',
  zoho_id: '',
  zoho_url: '',
  erm_status: 'not_pushed',
  erm_external_id: '',
  erm_url: '',
}

const ERM_STATUSES: ErmStatus[] = ['not_pushed', 'pending', 'synced', 'stale', 'failed']

function TrainerModal({
  open,
  trainer,
  onClose,
  onSaved,
}: {
  open: boolean
  trainer: Trainer | null
  onClose: () => void
  onSaved: () => void
}) {
  const [form, setForm] = useState<TrainerForm>(EMPTY_FORM)

  useEffect(() => {
    if (!open) return
    setForm(
      trainer
        ? {
            pan: trainer.pan,
            full_name: trainer.full_name,
            email: trainer.email ?? '',
            phone: trainer.phone ?? '',
            type: trainer.type,
            work_order_status: trainer.work_order_status,
            zoho_id: trainer.zoho_id ?? '',
            zoho_url: trainer.zoho_url ?? '',
            erm_status: trainer.erm_status,
            erm_external_id: trainer.erm_external_id ?? '',
            erm_url: trainer.erm_url ?? '',
          }
        : EMPTY_FORM,
    )
  }, [open, trainer])

  const save = useMutation({
    mutationFn: () => {
      const payload = {
        // Upper-cased before it is sent: trainers_pan_upper_ck rejects anything
        // else, and case normalisation is part of the identity claim rather than
        // cosmetics — 'abcde1234f' alongside 'ABCDE1234F' is one human with two
        // trainer rows and two invoice sequences.
        pan: form.pan.trim().toUpperCase(),
        full_name: form.full_name.trim(),
        email: form.email.trim() || null,
        phone: form.phone.trim() || null,
        type: form.type,
        work_order_status: form.work_order_status,
        zoho_id: form.zoho_id.trim() || null,
        zoho_url: form.zoho_url.trim() || null,
        erm_status: form.erm_status,
        erm_external_id: form.erm_external_id.trim() || null,
        erm_url: form.erm_url.trim() || null,
      }
      return trainer
        ? unwrap(supabase.from('trainers').update(payload).eq('id', trainer.id))
        : unwrap(supabase.from('trainers').insert(payload))
    },
    onSuccess: onSaved,
  })

  const set = <K extends keyof TrainerForm>(k: K, v: TrainerForm[K]) =>
    setForm((f) => ({ ...f, [k]: v }))

  const pan = form.pan.trim().toUpperCase()
  const panLooksWrong = pan.length > 0 && !PAN_PATTERN.test(pan)

  function onSubmit(e: FormEvent) {
    e.preventDefault()
    save.mutate()
  }

  return (
    <Modal open={open} onClose={onClose} title={trainer ? 'Edit trainer' : 'New trainer'}>
      <form onSubmit={onSubmit} className="space-y-4">
        <div className="grid grid-cols-2 gap-3">
          <Field label="Full name">
            <Input
              required
              value={form.full_name}
              onChange={(e) => set('full_name', e.target.value)}
              placeholder="Vema Prudhvi Sai"
            />
          </Field>
          <Field
            label="PAN"
            hint={
              trainer
                ? 'Changing a PAN changes an identity — invoice numbers already issued keep the old one.'
                : 'AAAAA9999A. The identity key, and the first four characters of every invoice number.'
            }
          >
            <Input
              required
              value={form.pan}
              onChange={(e) => set('pan', e.target.value.toUpperCase())}
              placeholder="BCDPS1234A"
              maxLength={10}
              className={`font-mono ${panLooksWrong ? '!border-warn' : ''}`}
            />
          </Field>
        </div>

        {panLooksWrong && (
          <p className="text-xs text-warn-ink -mt-2">
            That does not look like a PAN (five letters, four digits, one letter). The
            database enforces ten characters; the full shape is checked at payout.
          </p>
        )}

        <div className="grid grid-cols-2 gap-3">
          <Field label="Email">
            <Input
              type="email"
              value={form.email}
              onChange={(e) => set('email', e.target.value)}
            />
          </Field>
          <Field label="Phone">
            <Input value={form.phone} onChange={(e) => set('phone', e.target.value)} />
          </Field>
        </div>

        <div className="grid grid-cols-2 gap-3">
          <Field label="Type">
            <Select
              value={form.type}
              onChange={(e) => set('type', e.target.value as TrainerType)}
            >
              <option value="freelancer">{TRAINER_TYPE_LABEL.freelancer}</option>
              <option value="full_timer">{TRAINER_TYPE_LABEL.full_timer}</option>
            </Select>
          </Field>
          <Field
            label="Work order status"
            hint="A status, not an amount — the rate lives on the work order itself."
          >
            <Select
              value={form.work_order_status}
              onChange={(e) => set('work_order_status', e.target.value as DocStatus)}
            >
              {DOC_STATUSES.map((s) => (
                <option key={s} value={s}>
                  {DOC_STATUS_LABEL[s]}
                </option>
              ))}
            </Select>
          </Field>
        </div>

        <div className="grid grid-cols-2 gap-3">
          <Field label="ZOHO id">
            <Input value={form.zoho_id} onChange={(e) => set('zoho_id', e.target.value)} />
          </Field>
          <Field label="ZOHO link">
            <Input
              type="url"
              value={form.zoho_url}
              onChange={(e) => set('zoho_url', e.target.value)}
              placeholder="https://…"
            />
          </Field>
        </div>

        <div className="grid grid-cols-2 gap-3">
          <Field label="ERM status">
            <Select
              value={form.erm_status}
              onChange={(e) => set('erm_status', e.target.value as ErmStatus)}
            >
              {ERM_STATUSES.map((s) => (
                <option key={s} value={s}>
                  {ERM_STATUS_LABEL[s]}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="ERM id">
            <Input
              value={form.erm_external_id}
              onChange={(e) => set('erm_external_id', e.target.value)}
            />
          </Field>
        </div>

        <Field label="ERM link">
          <Input
            type="url"
            value={form.erm_url}
            onChange={(e) => set('erm_url', e.target.value)}
            placeholder="https://…"
          />
        </Field>

        {save.error && <ErrorNote>{errorMessage(save.error)}</ErrorNote>}

        <div className="flex justify-end gap-2">
          <Button type="button" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" disabled={save.isPending}>
            {trainer ? 'Save changes' : 'Create trainer'}
          </Button>
        </div>
      </form>
    </Modal>
  )
}

interface RailsForm {
  bank_account_number: string
  ifsc: string
  bank_name: string
  branch: string
  account_name: string
}

const EMPTY_RAILS: RailsForm = {
  bank_account_number: '',
  ifsc: '',
  bank_name: '',
  branch: '',
  account_name: '',
}

/**
 * Bank rails for one trainer — `public.trainer_bank_accounts`, 1:1 on
 * `trainer_id` (migration 1400).
 *
 * WHY THERE IS NO DELETE BUTTON. The migration grants SELECT, INSERT and UPDATE
 * to `authenticated` and deliberately withholds DELETE, for the same reason as
 * `artifact_versions`: rails that vanish take with them the evidence of what an
 * approved payout was executed against. A correction is an UPDATE. A delete
 * button here would be a button that always fails at the database.
 *
 * WHY THE WRITE IS AN UPSERT. `trainer_id` is the primary key, so "file rails"
 * and "correct rails" are one statement rather than two code paths that can
 * disagree about which one this trainer is in — and the 1:1 makes "rails on
 * file?" a single existence test rather than a max(created_at) race.
 *
 * WHY NO TRAINER-FACING VERSION OF THIS FORM EXISTS. A trainer may read their
 * own rails (catching a transposed digit before Finance releases is worth more
 * than the secrecy) but has no UPDATE policy. A payee who can silently repoint
 * their own account between approval and release defeats R4 — the approved
 * artifact would name an account that no longer exists on the row. The trainer
 * supplies the number; a human on this side types it; both are on record.
 *
 * The account number is handled as a STRING throughout. It is an identifier
 * with significant leading zeros, not a quantity.
 */
function BankRailsModal({
  trainer,
  existing,
  onClose,
  onSaved,
}: {
  trainer: Trainer | null
  existing: TrainerBankAccount | null
  onClose: () => void
  onSaved: () => void
}) {
  const [form, setForm] = useState<RailsForm>(EMPTY_RAILS)

  useEffect(() => {
    if (!trainer) return
    setForm(
      existing
        ? {
            bank_account_number: existing.bank_account_number,
            ifsc: existing.ifsc,
            bank_name: existing.bank_name ?? '',
            branch: existing.branch ?? '',
            // NOT defaulted from trainer.full_name when absent — see the field
            // hint. An account name we invented is worse than a blank one.
            account_name: existing.account_name ?? '',
          }
        : EMPTY_RAILS,
    )
  }, [trainer, existing])

  const save = useMutation({
    mutationFn: () => {
      if (!trainer) throw new Error('No trainer selected')
      return unwrap(
        supabase.from('trainer_bank_accounts').upsert(
          {
            trainer_id: trainer.id,
            bank_account_number: form.bank_account_number.trim(),
            // Upper-cased on the way out as well as on the way in: the RBI
            // directory is uppercase, and a lowercase rail that is byte-unequal
            // to the same rail entered elsewhere turns a reconciliation into a
            // puzzle. The DB rejects anything else outright.
            ifsc: form.ifsc.trim().toUpperCase(),
            bank_name: form.bank_name.trim() || null,
            branch: form.branch.trim() || null,
            account_name: form.account_name.trim() || null,
          },
          { onConflict: 'trainer_id' },
        ),
      )
    },
    onSuccess: onSaved,
  })

  const set = <K extends keyof RailsForm>(k: K, v: RailsForm[K]) =>
    setForm((f) => ({ ...f, [k]: v }))

  const account = form.bank_account_number.trim()
  const ifsc = form.ifsc.trim().toUpperCase()
  const accountLooksWrong = account.length > 0 && !ACCOUNT_PATTERN.test(account)
  const ifscLooksWrong = ifsc.length > 0 && !IFSC_PATTERN.test(ifsc)

  function onSubmit(e: FormEvent) {
    e.preventDefault()
    save.mutate()
  }

  return (
    <Modal
      open={trainer !== null}
      onClose={onClose}
      title={existing ? 'Correct bank rails' : 'File bank rails'}
      width="max-w-xl"
    >
      <form onSubmit={onSubmit} className="space-y-4">
        <div className="rounded-lg border border-line bg-surface-2 px-3 py-2">
          <p className="text-sm font-medium text-ink">{trainer?.full_name}</p>
          <p className="text-xs font-mono text-ink-3 mt-0.5">{trainer?.pan}</p>
        </div>

        <Field
          label="Bank account number"
          hint="Digits only. No length rule — Indian account numbers run roughly 9 to 18 digits and vary by bank. Copy it exactly, leading zeros included."
        >
          <Input
            required
            inputMode="numeric"
            value={form.bank_account_number}
            onChange={(e) => set('bank_account_number', e.target.value)}
            placeholder="50100123456789"
            className={`font-mono tabular-nums ${accountLooksWrong ? '!border-warn' : ''}`}
          />
        </Field>

        {accountLooksWrong && (
          <p className="text-xs text-warn-ink -mt-2">
            Digits only — no spaces, dashes or letters. The database rejects anything else, so
            this will not save.
          </p>
        )}

        <Field
          label="IFSC"
          hint="Exactly 11 characters, e.g. HDFC0001234 — four letters, a zero, then six more. Upper-cased for you."
        >
          <Input
            required
            value={form.ifsc}
            onChange={(e) => set('ifsc', e.target.value.toUpperCase())}
            placeholder="HDFC0001234"
            maxLength={11}
            className={`font-mono ${ifscLooksWrong ? '!border-warn' : ''}`}
          />
        </Field>

        {ifscLooksWrong && (
          <p className="text-xs text-warn-ink -mt-2">
            That is not the RBI shape (four letters, the reserved <code>0</code>, then six
            letters or digits). The database enforces eleven characters; the full shape is
            checked again at payout.
          </p>
        )}

        <Field
          label="Account name (beneficiary)"
          hint="The name AS THE BANK HOLDS IT — not necessarily the trainer's name here. A joint account, an expanded initial or a married name not yet changed at the branch are all normal. A mismatch bounces the payment and costs a fortnight, so copy it from a cheque or passbook rather than typing what you expect."
        >
          <Input
            value={form.account_name}
            onChange={(e) => set('account_name', e.target.value)}
            placeholder="As printed on the cheque"
          />
        </Field>

        <div className="grid grid-cols-2 gap-3">
          <Field label="Bank name" hint="Printed on the remuneration sheet.">
            <Input
              value={form.bank_name}
              onChange={(e) => set('bank_name', e.target.value)}
              placeholder="HDFC Bank"
            />
          </Field>
          <Field label="Branch">
            <Input
              value={form.branch}
              onChange={(e) => set('branch', e.target.value)}
              placeholder="Madhapur"
            />
          </Field>
        </div>

        <InfoNote>
          A payout is blocked before it can be submitted for approval unless a well-formed
          account number and IFSC are on file (CLAUDE.md §7). Rails are never deleted —
          correcting a wrong number is an edit here, and the previous value stays in the audit
          trail, which is where a superseded payment instruction belongs.
        </InfoNote>

        {save.error && <ErrorNote>{errorMessage(save.error)}</ErrorNote>}

        <div className="flex justify-end gap-2">
          <Button type="button" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" disabled={save.isPending}>
            {existing ? 'Save correction' : 'File rails'}
          </Button>
        </div>
      </form>
    </Modal>
  )
}
