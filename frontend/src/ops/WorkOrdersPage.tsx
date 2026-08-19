import { useEffect, useMemo, useState, type FormEvent } from 'react'
import { PAGE, bounded, emptyBound, usePageLimit, type Bounded } from '../lib/bounds'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { supabase, errorMessage, unwrap } from '../lib/supabase'
import { qk } from '../lib/queryKeys'
import {
  DOC_STATUSES,
  DOC_STATUS_LABEL,
  RATE_BASIS_LABEL,
  type DocStatus,
  type Program,
  type RateBasis,
  type Trainer,
  type WorkOrder,
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
  fmtAmount,
  fmtDate,
  HelpTip,
  InfoNote,
  Input,
  Modal,
  MonoValue,
  PageIntro,
  Select,
  TableSkeleton,
  Td,
  Textarea,
  Th,
} from '../components/ui'

type WorkOrderRow = WorkOrder & {
  trainers: { full_name: string; pan: string } | null
  programs: { name: string; type: string; colleges: { name: string } | null } | null
}

type ProgramOption = Program & { colleges: { name: string } | null }

/**
 * Work orders — the signed engagement terms, and the home of the RATE.
 *
 * THIS IS THE COMMERCIALS SURFACE. CLAUDE.md §4 puts work-order rates behind
 * can_see_commercials(): Senior Manager and Manager only. OpsRoot omits the nav
 * link for an LDE Executive, and that omission is COSMETIC — it keeps them from
 * walking into a page that would be empty for them, and it is not what stops
 * them reading the rates.
 *
 * What stops them is `work_orders_commercials_all` in migration 0700:
 *
 *     using (public.can_see_commercials() and public.can_reach_program(program_id))
 *
 * An LDE Executive who types this URL gets an empty table, because the SELECT
 * returns ZERO ROWS. The query below is issued for every internal persona
 * precisely so that stays true and visible: nothing here filters by role, and if
 * the policy were ever weakened the rows would appear — which is the failure
 * mode a UI-side filter would hide.
 *
 * WHY THE RATE IS A STRING, EVERYWHERE, ALL THE WAY TO THE SCREEN.
 * numeric(14,2) arrives from PostgREST as a string and is kept as one. It is
 * never parsed to a JS number, and nothing on this page adds, multiplies or
 * rounds it. R2/R7: every rupee is computed in Decimal inside
 * services/remuneration/engine.py. A helper here that returned a number would be
 * the first step towards money arithmetic happening in a browser, in binary
 * floating point.
 */
export function WorkOrdersPage() {
  const { canSeeCommercials } = useAuth()
  const queryClient = useQueryClient()
  const [editing, setEditing] = useState<WorkOrder | null>(null)
  const [creating, setCreating] = useState(false)

  // BOUNDED. One row per trainer per engagement and nothing removes an expired
  // one — a signed WO covering a past period is the evidence a past payout was
  // legitimate, so the table only grows. Newest `valid_from` first, so the
  // bound cuts expired history rather than the orders currently gating a
  // payout at §7.
  const page = usePageLimit(PAGE.workOrders)

  const workOrdersQuery = useQuery({
    queryKey: qk.workOrders.list(page.limit),
    queryFn: () =>
      bounded<WorkOrderRow>(page.limit, (limitRows) =>
        supabase
          .from('work_orders')
          // `rate:rate::text` is load-bearing, not a style choice. PostgREST
          // serialises `numeric` as a JSON number, so a plain `*` would hand
          // JavaScript a float — 80000.0 — and CLAUDE.md R7 exists precisely
          // because money must never become a float. Casting on the server keeps
          // the value a string from Postgres to pixel, so nothing here can round
          // it. The alias keeps the field named `rate`.
          .select('*, rate:rate::text, trainers(full_name, pan), programs(name, type, colleges(name))')
          .order('valid_from', { ascending: false })
          .limit(limitRows),
      ),
  })

  const trainersQuery = useQuery({
    queryKey: qk.trainers.list(PAGE.trainers),
    enabled: canSeeCommercials,
    queryFn: () =>
      bounded<Trainer>(PAGE.trainers, (rows) =>
        supabase.from('trainers').select('*').order('full_name').limit(rows),
      ),
  })

  const programsQuery = useQuery({
    queryKey: qk.programs.list(PAGE.programs),
    enabled: canSeeCommercials,
    queryFn: () =>
      bounded<ProgramOption>(PAGE.programs, (rows) =>
        supabase.from('programs').select('*, colleges(name)').order('name').limit(rows),
      ),
  })

  const bound = workOrdersQuery.data ?? emptyBound<WorkOrderRow>(page.limit)
  const rows = useMemo(() => bound.rows, [bound.rows])
  const unsigned = rows.filter((w) => w.status !== 'signed').length

  function close() {
    setCreating(false)
    setEditing(null)
  }

  return (
    <>
      <PageHeader
        title="Work orders"
        purpose="The signed agreement covering one trainer on one program — what they are paid, whether that is per day or per month, and the dates it covers. No signed work order on file, no payout."
        subtitle={
          canSeeCommercials
            ? `${rows.length}${bound.truncated ? '+' : ''} on file${
                unsigned > 0 ? ` · ${unsigned} not yet signed` : ''
              }`
            : 'Commercial — restricted to Senior Managers and Managers'
        }
        actions={
          canSeeCommercials ? (
            <Button variant="primary" size="sm" onClick={() => setCreating(true)}>
              New work order
            </Button>
          ) : undefined
        }
      />

      <Page>
        {workOrdersQuery.error && (
          <div className="mb-4">
            <ErrorNote>{errorMessage(workOrdersQuery.error)}</ErrorNote>
          </div>
        )}

        {canSeeCommercials && (
          <div className="mb-4">
            <PageIntro
              steps={[
                'Record the rate and the dates it covers',
                'Send it out for signature',
                'File the signed document here',
                'Payouts falling inside those dates can now be checked against it',
              ]}
            >
              A work order is the signed agreement for one trainer on one program. Three
              separate checks read this table before that trainer can be paid: the order must
              be <strong>signed</strong>, the payout month must fall <strong>entirely
              inside</strong> the valid-from to valid-to window, and the rate on the payout must
              <strong> match the rate signed here</strong>. Any one of those failing stops the
              payout — it is not a warning someone can wave through.
            </PageIntro>
          </div>
        )}

        {!canSeeCommercials && (
          <div className="mb-4 max-w-3xl">
            <InfoNote>
              Work orders carry the engagement rate, so they are restricted to Senior Managers
              and Managers (CLAUDE.md §4). This page is empty for your role because the
              database returned no rows — not because the screen hid them. The nav link is
              omitted for the same reason it is worth saying so here: a 404 would imply the
              page does not exist, when the honest answer is that the data is not yours.
            </InfoNote>
          </div>
        )}

        {workOrdersQuery.isPending ? (
          <Card className="overflow-hidden">
            <TableSkeleton rows={6} cols={6} />
          </Card>
        ) : rows.length === 0 ? (
          <Card>
            <EmptyState
              title={canSeeCommercials ? 'No work orders yet' : 'Nothing to show'}
              body={
                canSeeCommercials
                  ? 'Each row here is one trainer engaged on one program, at an agreed rate, for an agreed stretch of dates.'
                  : 'Your role does not include commercials.'
              }
              hint={
                canSeeCommercials
                  ? 'Nothing has been raised yet. Until a signed order exists for a trainer, every payout for them will be blocked at validation.'
                  : 'The database returned zero rows for your role — the screen is not hiding anything it was given.'
              }
              action={
                canSeeCommercials ? (
                  <Button variant="primary" size="sm" onClick={() => setCreating(true)}>
                    Add the first work order
                  </Button>
                ) : undefined
              }
            />
          </Card>
        ) : (
          <div className="space-y-4">
            {/* "N not yet signed" in the header counts the rows that arrived.
                An unsigned WO is a §7 blocking gate, so an undercount here
                reads as "nothing is blocking a payout" when something is. */}
            <BoundNote
              bound={bound}
              noun="work orders"
              derived="The unsigned count covers only those."
              onMore={page.more}
              step={page.step}
            />

            <Card className="overflow-hidden">
              <div className="overflow-x-auto scroll-slim">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-line">
                      <Th>
                        <HelpTip term="Trainer">
                          Identified by their PAN, the ten-character income-tax number shown
                          under the name. Names repeat and get spelled differently; the PAN is
                          the one key that does not, and it is what the invoice number is built
                          from.
                        </HelpTip>
                      </Th>
                      <Th>Program</Th>
                      <Th className="text-right">
                        <HelpTip term="Rate">
                          The amount agreed in the signed document. A payout is blocked unless
                          the rate on the engagement matches this figure — a rate someone edited
                          after signature is exactly what that check is for.
                        </HelpTip>
                      </Th>
                      <Th>
                        <HelpTip term="Basis">
                          Whether the rate is per day or per month. CRT programs are normally
                          per day; bCAP programs are per month, spread across the calendar days
                          of that month. The basis also decides what an unmarked attendance day
                          means — see the Attendance screen.
                        </HelpTip>
                      </Th>
                      <Th>
                        <HelpTip term="Valid">
                          The dates this order covers. A payout month must fall entirely inside
                          them; one that starts a day early or ends a day late cannot be paid
                          against this order.
                        </HelpTip>
                      </Th>
                      <Th>Status</Th>
                      <Th>Document</Th>
                      <Th />
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-line-soft">
                    {rows.map((w) => (
                      <tr key={w.id} className="hover:bg-surface-2/60 transition">
                        <Td className="font-medium text-ink">
                          {w.trainers?.full_name ?? 'Unknown trainer'}
                          <span className="block font-normal text-ink-3">
                            <MonoValue>{w.trainers?.pan}</MonoValue>
                          </span>
                        </Td>
                        <Td className="text-ink-2">
                          {w.programs?.name ?? '—'}
                          <span className="block text-xs text-ink-3">
                            {w.programs?.colleges?.name ?? '—'}
                          </span>
                        </Td>
                        {/* String in, string out. Never parsed. */}
                        <Td className="text-right tabular-nums text-ink font-medium">
                          {fmtAmount(w.rate)}
                        </Td>
                        <Td>
                          <Badge>{RATE_BASIS_LABEL[w.rate_basis]}</Badge>
                        </Td>
                        <Td className="text-ink-2 whitespace-nowrap text-xs">
                          {fmtDate(w.valid_from)} → {fmtDate(w.valid_to)}
                        </Td>
                        <Td>
                          <DocStatusPill status={w.status} />
                          {w.signed_at && (
                            <span className="block text-[11px] text-ink-3 mt-0.5">
                              {fmtDate(w.signed_at)}
                            </span>
                          )}
                        </Td>
                        <Td>
                          {w.document_url ? (
                            <a
                              href={w.document_url}
                              target="_blank"
                              rel="noreferrer"
                              className="text-xs font-medium text-accent hover:underline"
                            >
                              Open ↗
                            </a>
                          ) : (
                            <span className="text-xs text-ink-3">Not filed</span>
                          )}
                        </Td>
                        <Td className="text-right">
                          <Button size="sm" variant="ghost" onClick={() => setEditing(w)}>
                            Edit
                          </Button>
                        </Td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>

            <div className="max-w-3xl">
              <InfoNote>
                The rate basis is the branch point for everything downstream.{' '}
                <HelpTip term="CRT">
                  Classroom / campus recruitment training. Paid <strong>per day</strong>: the
                  payable days are counted up from the days marked present, so a day nobody
                  marked pays nothing.
                </HelpTip>{' '}
                is normally <strong>per day</strong>;{' '}
                <HelpTip term="bCAP">
                  A monthly retainer engagement. Paid <strong>per month</strong>, spread across
                  the calendar days of the month: payable days are counted down from the length
                  of the period, so weekends, college holidays and days nobody marked are all
                  still paid.
                </HelpTip>{' '}
                is normally <strong>per month</strong>. The unusual pairing is allowed — the
                signed document wins — but it changes what attendance means as well as how the
                payout is derived, so check the document before you save one.
              </InfoNote>
            </div>
          </div>
        )}
      </Page>

      {canSeeCommercials && (
        <WorkOrderModal
          open={creating || editing !== null}
          workOrder={editing}
          trainers={trainersQuery.data ?? emptyBound<Trainer>(PAGE.trainers)}
          programs={programsQuery.data ?? emptyBound<ProgramOption>(PAGE.programs)}
          onClose={close}
          onSaved={() => {
            close()
            void queryClient.invalidateQueries({ queryKey: qk.workOrders.all })
          }}
        />
      )}
    </>
  )
}

interface WorkOrderForm {
  trainer_id: string
  program_id: string
  /** STRING. See the component comment — this value is never parsed. */
  rate: string
  rate_basis: RateBasis
  valid_from: string
  valid_to: string
  status: DocStatus
  signed_on: string
  document_url: string
  notes: string
}

const EMPTY_FORM: WorkOrderForm = {
  trainer_id: '',
  program_id: '',
  rate: '',
  rate_basis: 'per_month',
  valid_from: '',
  valid_to: '',
  status: 'not_started',
  signed_on: '',
  document_url: '',
  notes: '',
}

function WorkOrderModal({
  open,
  workOrder,
  trainers,
  programs,
  onClose,
  onSaved,
}: {
  open: boolean
  workOrder: WorkOrder | null
  /** Bounded, so the form can say when a picker is not the whole list. */
  trainers: Bounded<Trainer>
  programs: Bounded<ProgramOption>
  onClose: () => void
  onSaved: () => void
}) {
  const [form, setForm] = useState<WorkOrderForm>(EMPTY_FORM)

  useEffect(() => {
    if (!open) return
    setForm(
      workOrder
        ? {
            trainer_id: workOrder.trainer_id,
            program_id: workOrder.program_id,
            rate: workOrder.rate,
            rate_basis: workOrder.rate_basis,
            valid_from: workOrder.valid_from,
            valid_to: workOrder.valid_to,
            status: workOrder.status,
            signed_on: workOrder.signed_at ? workOrder.signed_at.slice(0, 10) : '',
            document_url: workOrder.document_url ?? '',
            notes: workOrder.notes ?? '',
          }
        : EMPTY_FORM,
    )
  }, [open, workOrder])

  const save = useMutation({
    mutationFn: () => {
      const payload = {
        trainer_id: form.trainer_id,
        program_id: form.program_id,
        // Sent as the string the user typed. PostgREST hands it to Postgres as
        // numeric(14,2); it is not turned into a JS number on the way.
        rate: form.rate.trim(),
        rate_basis: form.rate_basis,
        valid_from: form.valid_from,
        valid_to: form.valid_to,
        status: form.status,
        signed_at: form.signed_on ? new Date(`${form.signed_on}T00:00:00`).toISOString() : null,
        document_url: form.document_url.trim() || null,
        notes: form.notes.trim() || null,
      }
      return workOrder
        ? unwrap(supabase.from('work_orders').update(payload).eq('id', workOrder.id))
        : unwrap(supabase.from('work_orders').insert(payload))
    },
    onSuccess: onSaved,
  })

  const set = <K extends keyof WorkOrderForm>(k: K, v: WorkOrderForm[K]) =>
    setForm((f) => ({ ...f, [k]: v }))

  const selectedProgram = programs.rows.find((p) => p.id === form.program_id)

  function onSubmit(e: FormEvent) {
    e.preventDefault()
    save.mutate()
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={workOrder ? 'Edit work order' : 'New work order'}
      width="max-w-xl"
    >
      <form onSubmit={onSubmit} className="space-y-4">
        <div className="grid grid-cols-2 gap-3">
          <Field label="Trainer">
            <Select
              required
              value={form.trainer_id}
              onChange={(e) => set('trainer_id', e.target.value)}
            >
              <option value="">Select a trainer…</option>
              {trainers.rows.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.full_name} · {t.pan}
                </option>
              ))}
            </Select>
            {/* A missing option here is the start of a duplicate PAN identity —
                see the same note on the deployment form. */}
            <BoundNote
              bound={trainers}
              noun="trainers"
              derived="A trainer beyond that is not selectable — do not add a second record for them."
            />
          </Field>
          <Field label="Program">
            <Select
              required
              value={form.program_id}
              onChange={(e) => set('program_id', e.target.value)}
            >
              <option value="">Select a program…</option>
              {programs.rows.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.colleges?.name ?? '—'} · {p.name} ({p.type})
                </option>
              ))}
            </Select>
            <BoundNote bound={programs} noun="programs" />
          </Field>
        </div>

        <div className="grid grid-cols-2 gap-3">
          <Field label="Rate (INR)" hint="Two decimal places. Stored exactly as typed.">
            <Input
              required
              inputMode="decimal"
              pattern="^\d+(\.\d{1,2})?$"
              value={form.rate}
              onChange={(e) => set('rate', e.target.value)}
              placeholder="80000.00"
              className="tabular-nums"
            />
          </Field>
          <Field
            label="Rate basis"
            hint={
              selectedProgram
                ? selectedProgram.type === 'CRT'
                  ? 'CRT is normally per day.'
                  : 'bCAP is normally per month, prorated by calendar days.'
                : 'per_day for CRT, per_month for bCAP.'
            }
          >
            <Select
              value={form.rate_basis}
              onChange={(e) => set('rate_basis', e.target.value as RateBasis)}
            >
              <option value="per_month">{RATE_BASIS_LABEL.per_month}</option>
              <option value="per_day">{RATE_BASIS_LABEL.per_day}</option>
            </Select>
          </Field>
        </div>

        {selectedProgram &&
          ((selectedProgram.type === 'CRT' && form.rate_basis === 'per_month') ||
            (selectedProgram.type === 'bCAP' && form.rate_basis === 'per_day')) && (
            <p className="text-xs text-warn-ink -mt-2">
              This is a {selectedProgram.type} program on a {RATE_BASIS_LABEL[form.rate_basis]}{' '}
              basis, which is the unusual pairing. It is allowed — the basis on the signed work
              order wins — but check the document before saving.
            </p>
          )}

        <div className="grid grid-cols-2 gap-3">
          <Field label="Valid from">
            <Input
              required
              type="date"
              value={form.valid_from}
              onChange={(e) => set('valid_from', e.target.value)}
            />
          </Field>
          <Field label="Valid to" hint="A payout period must fall entirely inside this window.">
            <Input
              required
              type="date"
              value={form.valid_to}
              onChange={(e) => set('valid_to', e.target.value)}
            />
          </Field>
        </div>

        <div className="grid grid-cols-2 gap-3">
          <Field label="Status">
            <Select
              value={form.status}
              onChange={(e) => set('status', e.target.value as DocStatus)}
            >
              {DOC_STATUSES.map((s) => (
                <option key={s} value={s}>
                  {DOC_STATUS_LABEL[s]}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Signed on">
            <Input
              type="date"
              value={form.signed_on}
              onChange={(e) => set('signed_on', e.target.value)}
            />
          </Field>
        </div>

        <Field label="Signed document link" hint="The file lives in the documents bucket.">
          <Input
            type="url"
            value={form.document_url}
            onChange={(e) => set('document_url', e.target.value)}
            placeholder="https://…"
          />
        </Field>

        <Field label="Notes">
          <Textarea rows={2} value={form.notes} onChange={(e) => set('notes', e.target.value)} />
        </Field>

        {save.error && <ErrorNote>{errorMessage(save.error)}</ErrorNote>}

        <div className="flex justify-end gap-2">
          <Button type="button" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" disabled={save.isPending}>
            {workOrder ? 'Save changes' : 'Create work order'}
          </Button>
        </div>
      </form>
    </Modal>
  )
}
