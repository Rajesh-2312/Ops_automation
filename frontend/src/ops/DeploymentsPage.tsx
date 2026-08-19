import { useEffect, useMemo, useState, type FormEvent } from 'react'
import { PAGE, bounded, emptyBound, usePageLimit, type Bounded } from '../lib/bounds'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { supabase, errorMessage, unwrap } from '../lib/supabase'
import { qk } from '../lib/queryKeys'
import type { Batch, Deployment, Trainer } from '../lib/types'
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
  Modal,
  MonoValue,
  PageIntro,
  SearchInput,
  Select,
  TableSkeleton,
  Td,
  Textarea,
  Th,
  Toolbar,
} from '../components/ui'

type DeploymentRow = Deployment & {
  trainers: { id: string; full_name: string; pan: string } | null
  batches:
    | {
        id: string
        name: string
        passout_year: number | null
        programs: { id: string; name: string; type: string; colleges: { name: string } | null } | null
      }
    | null
}

type BatchOption = Batch & {
  programs: { id: string; name: string; type: string; colleges: { name: string } | null } | null
}

/** "St. Mary's · CRT Jul–Sep · CSE-A (2027)" — everything needed to tell two
 *  same-named batches apart, in the order a human narrows down. */
function batchLabel(b: BatchOption): string {
  const college = b.programs?.colleges?.name ?? 'Unknown college'
  const program = b.programs ? `${b.programs.name} (${b.programs.type})` : 'Unknown program'
  const year = b.passout_year ? ` · passing out ${b.passout_year}` : ' · no passout year'
  return `${college} · ${program} · ${b.name}${year}`
}

/**
 * Deployments — which trainer teaches which BATCH, from when.
 *
 * WHY THIS SCREEN EXISTS AT ALL. A deployment is the row every downstream thing
 * is keyed on: `trainer_attendance` is one row per DEPLOYMENT per day, and a
 * payout is computed for a deployment over a period. Until this page there was
 * no UI that inserted one, so a real program could be tracked all the way to
 * "trainer onboarded" and then stop — nobody could mark a day of attendance,
 * and no payout could be produced, without hand-written SQL.
 *
 * WHY ITS OWN PAGE RATHER THAN A PANEL ON A PROGRAM. A deployment does not
 * belong to a program; it belongs to a BATCH, and one trainer is routinely
 * spread across batches in several programs and colleges. The question the Ops
 * person actually arrives with is "who is on what, and is anyone unassigned" —
 * which is a roster question, not a program question. Hanging it off
 * ProgramDetail would answer only the narrow version and would have to be built
 * twice for a Manager who wants the wide one. The page pairs with Attendance,
 * whose own selector reads exactly this list.
 *
 * READ vs WRITE, PER PERSONA — decided here, enforced in Postgres.
 *
 *   Senior Manager / Manager / LDE Executive — READ AND WRITE, all three.
 *   Trainer — reads own deployments; may update only tracksheet/travel columns.
 *   College — nothing.
 *
 * The interesting one is the LDE Executive, and the decision is to let them
 * write. §4 gives them attendance and batches as their daily job, and they are
 * the persona physically on campus who knows the day a trainer actually started
 * — a deployment they cannot create is attendance they cannot mark, on the one
 * screen they use most. `deployments_internal_all` in migration 0400 already
 * says exactly this:
 *
 *     using (public.is_internal() and public.can_reach_batch(batch_id))
 *
 * `for all`, every internal persona, narrowed by REACH rather than by role.
 * There is nothing commercial on this table — 0400's header is explicit that
 * the engagement rate was kept off `deployments` precisely so an LDE Executive
 * could be given the whole row. So a UI that made this read-only for them would
 * be inventing a rule the database does not have, and R5's answer to "who may
 * write?" is the policy, not the screen. What the database does NOT allow is a
 * trainer editing their own dates (an extension is a raise) — that is the
 * `deployments_guard_trainer_columns` trigger, and no trainer-facing form here
 * exists to test it.
 *
 * No client-side filtering by role anywhere below. Every persona issues the
 * same SELECT and the policy returns fewer rows for a narrower reach.
 */
export function DeploymentsPage() {
  const { profile } = useAuth()
  const queryClient = useQueryClient()
  const [editing, setEditing] = useState<DeploymentRow | null>(null)
  const [creating, setCreating] = useState(false)
  const [search, setSearch] = useState('')
  const [openOnly, setOpenOnly] = useState(false)

  // Internal personas only. A trainer or college signing in has their own
  // surface; this console is the internal one (OpsRoot), so the query is issued
  // unconditionally and the policy decides.
  // BOUNDED. One row per trainer per batch, and nothing ever deletes one — a
  // finished engagement stays on record because attendance and payouts are
  // keyed on it. `deployments_internal_all` resolves reach with
  // `can_reach_deployment()`, measured at 466 µs a row, which makes this the
  // second-most expensive unbounded read in the console after attendance.
  // Newest first, so the bound cuts history rather than current work.
  const page = usePageLimit(PAGE.deployments)

  const deploymentsQuery = useQuery({
    queryKey: qk.deployments.list(page.limit),
    queryFn: () =>
      bounded<DeploymentRow>(page.limit, (rows) =>
        supabase
          .from('deployments')
          .select(
            '*, trainers(id, full_name, pan), batches(id, name, passout_year, programs(id, name, type, colleges(name)))',
          )
          .order('start_date', { ascending: false })
          .limit(rows),
      ),
  })

  // Picker sources. A truncated PICKER is its own hazard, and a quieter one than
  // a truncated table: the trainer you want is simply not in the list, and the
  // documented recovery for "trainer missing" is to add them again — which on
  // this schema means a second PAN-keyed identity for one human and a duplicate
  // payout nobody notices until reconciliation (§6). So the form says when its
  // list is cut, rather than letting a missing option read as a missing record.
  const trainersQuery = useQuery({
    queryKey: qk.trainers.list(PAGE.trainers),
    queryFn: () =>
      bounded<Trainer>(PAGE.trainers, (rows) =>
        supabase.from('trainers').select('*').order('full_name').limit(rows),
      ),
  })

  // Every batch the caller reaches. Batches belong to programs belong to
  // colleges, and all three are needed to identify one — `CSE-A` alone is a
  // different cohort at every college and in every year.
  const batchesQuery = useQuery({
    queryKey: qk.batches.list(PAGE.batches),
    queryFn: () =>
      bounded<BatchOption>(PAGE.batches, (rows) =>
        supabase
          .from('batches')
          .select('*, programs(id, name, type, colleges(name))')
          .order('name')
          .limit(rows),
      ),
  })

  const bound = deploymentsQuery.data ?? emptyBound<DeploymentRow>(page.limit)
  const rows = useMemo(() => bound.rows, [bound.rows])

  const today = new Date().toISOString().slice(0, 10)
  const isOpen = (d: DeploymentRow) => !d.end_date || d.end_date >= today
  const openCount = rows.filter(isOpen).length

  const visible = useMemo(() => {
    const needle = search.trim().toLowerCase()
    return rows.filter((d) => {
      if (openOnly && !isOpen(d)) return false
      if (needle === '') return true
      const haystack = [
        d.trainers?.full_name,
        d.trainers?.pan,
        d.batches?.name,
        d.batches?.passout_year?.toString(),
        d.batches?.programs?.name,
        d.batches?.programs?.colleges?.name,
      ]
        .filter(Boolean)
        .join(' ')
        .toLowerCase()
      return haystack.includes(needle)
    })
    // `isOpen` closes over today's date, which is constant for a session; the
    // memo is keyed on the three things that actually change.
  }, [rows, search, openOnly])

  function close() {
    setCreating(false)
    setEditing(null)
  }

  const failure = deploymentsQuery.error ?? trainersQuery.error ?? batchesQuery.error

  return (
    <>
      <PageHeader
        title="Deployments"
        purpose="Who is teaching which batch, and from when. Every attendance mark and every payout hangs off one of these rows — a trainer with no deployment cannot be marked present and cannot be paid."
        subtitle={
          rows.length === 0
            ? 'Which trainer teaches which batch'
            : `${rows.length}${bound.truncated ? '+' : ''} on record · ${openCount} currently running`
        }
        actions={
          <Button variant="primary" size="sm" onClick={() => setCreating(true)}>
            New deployment
          </Button>
        }
      />

      <Page>
        {failure && (
          <div className="mb-4">
            <ErrorNote>{errorMessage(failure)}</ErrorNote>
          </div>
        )}

        <div className="mb-4">
          <PageIntro
            steps={[
              'Pick the trainer',
              'Pick the college, program and batch they teach',
              'Set the start date; leave the end date blank while it runs',
              'Attendance can now be marked, and a payout can be produced',
            ]}
          >
            A deployment attaches one trainer to one batch for a stretch of dates. It is the
            row everything downstream hangs off: attendance is stored one row per deployment
            per day, and pay is worked out for a deployment over a period. The dates bound
            what a payout may cover, so an end date typed a week early is a week of pay lost.
          </PageIntro>
        </div>

        {deploymentsQuery.isPending ? (
          <Card className="overflow-hidden">
            <TableSkeleton rows={6} cols={6} />
          </Card>
        ) : rows.length === 0 ? (
          <Card>
            <EmptyState
              title="No deployments yet"
              body={
                (batchesQuery.data?.rows ?? []).length === 0
                  ? 'A deployment attaches a trainer to a batch. There are no batches you can reach yet — add a program and its batches on the college first, then come back.'
                  : 'A deployment attaches a trainer to a batch, and it is what attendance and payouts are keyed on: no deployment means no day can be marked and no payout can be produced. Add one for each trainer who is teaching.'
              }
              hint={
                (batchesQuery.data?.rows ?? []).length === 0
                  ? 'The blocker is upstream: with no batch in reach there is nothing to attach a trainer to, so this list cannot fill until one exists.'
                  : 'Nobody has been attached to a batch yet. The first deployment you add appears here and in the Attendance picker straight away.'
              }
              action={
                <Button variant="primary" size="sm" onClick={() => setCreating(true)}>
                  Add the first deployment
                </Button>
              }
            />
          </Card>
        ) : (
          <div className="space-y-4">
            {/* The chip counts and the "currently running" figure are computed
                from the rows that arrived, so they are counts of the page when
                the page is not the whole list. */}
            <BoundNote
              bound={bound}
              noun="deployments"
              derived="The filter counts cover only those."
              onMore={page.more}
              step={page.step}
            />

            {/* `count`/`total` is the point of the swap, not the magnifier. A
                filter that narrows in silence is how somebody concludes a
                trainer has no deployment and creates a second one. */}
            <Toolbar>
              <SearchInput
                className="w-full sm:w-80"
                placeholder="Search trainer, PAN, batch, program or college…"
                value={search}
                onChange={setSearch}
                count={visible.length}
                total={rows.length}
              />
              <span className="w-px h-5 bg-line mx-1" aria-hidden />
              <FilterChip
                label="All"
                count={rows.length}
                active={!openOnly}
                onClick={() => setOpenOnly(false)}
              />
              <FilterChip
                label="Currently running"
                count={openCount}
                active={openOnly}
                onClick={() => setOpenOnly(true)}
              />
            </Toolbar>

            <Card className="overflow-hidden">
              <div className="overflow-x-auto scroll-slim">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-line">
                      <Th>
                        <HelpTip term="Trainer">
                          Shown with their PAN underneath. Trainer identity is the{' '}
                          <strong>PAN</strong>, never the name — two people can share a name,
                          and one person can be spelled three ways. Matching on a name is how
                          one human ends up with two records and two payouts.
                        </HelpTip>
                      </Th>
                      <Th>Batch</Th>
                      <Th>Program</Th>
                      <Th>Dates</Th>
                      <Th>Tracksheet</Th>
                      <Th>Travel</Th>
                      <Th />
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-line-soft">
                    {visible.map((d) => (
                      <tr key={d.id} className="hover:bg-surface-2/60 transition">
                        <Td className="font-medium text-ink">
                          {d.trainers?.full_name ?? 'Unknown trainer'}
                          <span className="block font-normal text-ink-3">
                            <MonoValue title="PAN — the trainer's identity">
                              {d.trainers?.pan}
                            </MonoValue>
                          </span>
                        </Td>
                        <Td className="text-ink-2">
                          {d.batches?.name ?? '—'}
                          {/* The passout year is shown on its own line, never
                              folded into the name: `CSE-A` is a different set of
                              students every year, and picking last year's cohort
                              is a mistake nothing downstream will catch. */}
                          <span className="block text-xs text-ink-3">
                            {d.batches?.passout_year
                              ? `passing out ${d.batches.passout_year}`
                              : 'no passout year on file'}
                          </span>
                        </Td>
                        <Td className="text-ink-2">
                          {d.batches?.programs?.name ?? '—'}
                          <span className="block text-xs text-ink-3">
                            {d.batches?.programs?.colleges?.name ?? '—'}
                          </span>
                        </Td>
                        <Td className="whitespace-nowrap text-xs text-ink-2">
                          {fmtDate(d.start_date)} → {d.end_date ? fmtDate(d.end_date) : 'open'}
                          {isOpen(d) && (
                            <span className="block mt-0.5">
                              <Badge tone="accent">Running</Badge>
                            </span>
                          )}
                        </Td>
                        <Td>
                          {d.tracksheet_url ? (
                            <a
                              href={d.tracksheet_url}
                              target="_blank"
                              rel="noreferrer"
                              className="text-xs font-medium text-accent hover:underline"
                            >
                              Open ↗
                            </a>
                          ) : (
                            <span className="text-xs text-ink-3">Not linked</span>
                          )}
                        </Td>
                        <Td className="text-xs text-ink-2 max-w-[16rem]">
                          {d.travel_notes ? (
                            <>
                              <span className="line-clamp-2">{d.travel_notes}</span>
                              {d.travel_submitted_at && (
                                <span className="block text-[11px] text-ink-3 mt-0.5">
                                  submitted {fmtDate(d.travel_submitted_at)}
                                </span>
                              )}
                            </>
                          ) : (
                            <span className="text-ink-3">—</span>
                          )}
                        </Td>
                        <Td className="text-right">
                          <Button size="sm" variant="ghost" onClick={() => setEditing(d)}>
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
                  title="No deployments match"
                  body="Clear the search, or switch back to All."
                  hint={
                    openOnly
                      ? 'The “Currently running” filter is on, so anything that has already ended is hidden — not missing.'
                      : 'Rows are here, but your search text matches none of them. The search reads trainer name, PAN, batch, program and college.'
                  }
                />
              )}
            </Card>

            <div className="max-w-3xl">
              {/* CORRECTED COPY. This note used to end "…which is why a trainer
                  can read but not edit them on their own row". Trainers are
                  records, not users (§4) — migration 1800 dropped all eighteen
                  trainer policies, so there is no row a trainer reads or edits
                  anywhere. The dates still matter for the reason given; only
                  the who-may-edit clause was wrong. */}
              <InfoNote>
                Attendance is marked per deployment per day, and a payout is computed for a
                deployment over a period — so a trainer with no deployment cannot be marked
                present and cannot be paid. Dates matter: they bound the period a payout may
                cover. Only internal staff can set them; trainers do not sign in to this
                system at all.
                {profile?.role === 'lde_executive' && (
                  <>
                    {' '}
                    You see the batches at the colleges you are assigned to, and you may add a
                    deployment for them — that is the same reach that lets you mark their
                    attendance.
                  </>
                )}
              </InfoNote>
            </div>
          </div>
        )}
      </Page>

      <DeploymentModal
        open={creating || editing !== null}
        deployment={editing}
        trainers={trainersQuery.data ?? emptyBound<Trainer>(PAGE.trainers)}
        batches={batchesQuery.data ?? emptyBound<BatchOption>(PAGE.batches)}
        onClose={close}
        onSaved={() => {
          close()
          // Attendance reads the same list, and the home roll-up counts it.
          void queryClient.invalidateQueries({ queryKey: qk.deployments.all })
        }}
      />
    </>
  )
}

interface DeploymentForm {
  trainer_id: string
  batch_id: string
  start_date: string
  end_date: string
  tracksheet_url: string
  travel_notes: string
  travel_submitted_on: string
}

const EMPTY_FORM: DeploymentForm = {
  trainer_id: '',
  batch_id: '',
  start_date: '',
  end_date: '',
  tracksheet_url: '',
  travel_notes: '',
  travel_submitted_on: '',
}

function DeploymentModal({
  open,
  deployment,
  trainers,
  batches,
  onClose,
  onSaved,
}: {
  open: boolean
  deployment: DeploymentRow | null
  /** Bounded, so the form can say when a picker is not the whole roster. */
  trainers: Bounded<Trainer>
  batches: Bounded<BatchOption>
  onClose: () => void
  onSaved: () => void
}) {
  const [form, setForm] = useState<DeploymentForm>(EMPTY_FORM)
  // The batch list is long and reads as one flat sentence per option, so it
  // gets its own narrowing box rather than a second picker for college and a
  // third for program — two dependent selects are two more things to get into
  // the wrong order.
  const [batchFilter, setBatchFilter] = useState('')

  useEffect(() => {
    if (!open) return
    setBatchFilter('')
    setForm(
      deployment
        ? {
            trainer_id: deployment.trainer_id,
            batch_id: deployment.batch_id,
            start_date: deployment.start_date ?? '',
            end_date: deployment.end_date ?? '',
            tracksheet_url: deployment.tracksheet_url ?? '',
            travel_notes: deployment.travel_notes ?? '',
            travel_submitted_on: deployment.travel_submitted_at
              ? deployment.travel_submitted_at.slice(0, 10)
              : '',
          }
        : EMPTY_FORM,
    )
  }, [open, deployment])

  const save = useMutation({
    mutationFn: () => {
      const payload = {
        trainer_id: form.trainer_id,
        batch_id: form.batch_id,
        // Nullable on the table. An empty end date means "still running", which
        // is the normal state of a live deployment — not a missing value.
        start_date: form.start_date || null,
        end_date: form.end_date || null,
        tracksheet_url: form.tracksheet_url.trim() || null,
        travel_notes: form.travel_notes.trim() || null,
        travel_submitted_at: form.travel_submitted_on
          ? new Date(`${form.travel_submitted_on}T00:00:00`).toISOString()
          : null,
      }
      return deployment
        ? unwrap(supabase.from('deployments').update(payload).eq('id', deployment.id))
        : unwrap(supabase.from('deployments').insert(payload))
    },
    onSuccess: onSaved,
  })

  const set = <K extends keyof DeploymentForm>(k: K, v: DeploymentForm[K]) =>
    setForm((f) => ({ ...f, [k]: v }))

  const visibleBatches = useMemo(() => {
    const needle = batchFilter.trim().toLowerCase()
    const list = needle
      ? batches.rows.filter((b) => batchLabel(b).toLowerCase().includes(needle))
      : batches.rows
    // The currently selected batch always stays in the list, or narrowing the
    // filter after choosing would silently blank the field.
    if (form.batch_id && !list.some((b) => b.id === form.batch_id)) {
      const chosen = batches.rows.find((b) => b.id === form.batch_id)
      if (chosen) return [chosen, ...list]
    }
    return list
  }, [batches, batchFilter, form.batch_id])

  const selectedBatch = batches.rows.find((b) => b.id === form.batch_id)

  // Mirrors `deployments_date_order_ck`. The database is the authority; this
  // only turns a 400 into a sentence.
  const datesOutOfOrder =
    form.start_date !== '' && form.end_date !== '' && form.end_date < form.start_date

  function onSubmit(e: FormEvent) {
    e.preventDefault()
    if (datesOutOfOrder) return
    save.mutate()
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={deployment ? 'Edit deployment' : 'New deployment'}
      width="max-w-xl"
    >
      <form onSubmit={onSubmit} className="space-y-4">
        <Field
          label="Trainer"
          hint="Listed with PAN, the identity key — two trainers can share a name."
        >
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
          {/* A short PICKER fails differently from a short table. The trainer
              you want is simply absent, and the natural recovery is to add
              them — which on this schema means a second PAN-keyed identity for
              one person and a duplicate payout that surfaces at
              reconciliation (§6). Say the list is cut. */}
          <div className="mt-2">
            <BoundNote
              bound={trainers}
              noun="trainers"
              derived="A trainer beyond that is not selectable here — do not add a second record for them."
            />
          </div>
        </Field>

        <Field
          label="Batch"
          hint="College, then program, then batch and the year that cohort passes out. Check the passout year — the same batch name exists at the same college every year, and choosing the wrong cohort is a mistake nothing downstream will catch."
        >
          <div className="space-y-2">
            <Input
              value={batchFilter}
              onChange={(e) => setBatchFilter(e.target.value)}
              placeholder="Narrow by college, program or batch…"
              aria-label="Filter batches"
              className="!h-8 !text-xs"
            />
            <Select
              required
              value={form.batch_id}
              onChange={(e) => set('batch_id', e.target.value)}
            >
              <option value="">Select a batch…</option>
              {visibleBatches.map((b) => (
                <option key={b.id} value={b.id}>
                  {batchLabel(b)}
                </option>
              ))}
            </Select>
            <BoundNote
              bound={batches}
              noun="batches"
              derived="The narrowing box above filters what was loaded, not the whole table."
            />
          </div>
        </Field>

        {batches.rows.length === 0 && (
          <p className="text-xs text-warn-ink -mt-2">
            No batches are visible to you. A deployment needs one — add the program and its
            batches on the college first.
          </p>
        )}

        {selectedBatch && !selectedBatch.passout_year && (
          <p className="text-xs text-warn-ink -mt-2">
            This batch has no passout year on file, so it cannot be told apart from the same
            batch name in another year. Worth filling in on the batch itself.
          </p>
        )}

        {selectedBatch?.programs && (
          <InfoNote>
            {selectedBatch.programs.type === 'CRT' ? (
              <>
                <HelpTip term="CRT">
                  A per-day engagement — the trainer is paid for the days they were there.
                </HelpTip>
                : paid per day, counted <strong>up</strong> from the P marks. An unmarked day
                is <strong>not</strong> payable, so attendance for this deployment must be
                complete before a payout can be submitted.
              </>
            ) : (
              <>
                <HelpTip term="bCAP">
                  A monthly retainer engagement — the trainer is paid for the month and
                  absences are taken off it.
                </HelpTip>
                : paid per month, spread across the calendar days and counted{' '}
                <strong>down</strong> from the period. Weekends and college holidays are
                payable, and an unmarked day pays — incomplete attendance is a warning here,
                not a block.
              </>
            )}
          </InfoNote>
        )}

        <div className="grid grid-cols-2 gap-3">
          <Field label="Start date" hint="The first day this trainer is on this batch.">
            <Input
              type="date"
              value={form.start_date}
              onChange={(e) => set('start_date', e.target.value)}
            />
          </Field>
          <Field
            label="End date"
            hint="Leave blank while the deployment is still running."
          >
            <Input
              type="date"
              value={form.end_date}
              min={form.start_date || undefined}
              onChange={(e) => set('end_date', e.target.value)}
              className={datesOutOfOrder ? '!border-warn' : ''}
            />
          </Field>
        </div>

        {datesOutOfOrder && (
          <p className="text-xs text-warn-ink -mt-2">
            The end date is before the start date. The database rejects this outright, so fix
            it before saving.
          </p>
        )}

        <Field
          label="Tracksheet link"
          hint="A link to the tracksheet in its own system — never a copy of it here."
        >
          <Input
            type="url"
            value={form.tracksheet_url}
            onChange={(e) => set('tracksheet_url', e.target.value)}
            placeholder="https://…"
          />
        </Field>

        <div className="grid grid-cols-2 gap-3">
          <Field
            label="Travel notes"
            hint="Onward AND return — a return leg nobody booked is the usual failure."
          >
            <Textarea
              rows={2}
              value={form.travel_notes}
              onChange={(e) => set('travel_notes', e.target.value)}
            />
          </Field>
          <Field label="Travel submitted on">
            <Input
              type="date"
              value={form.travel_submitted_on}
              onChange={(e) => set('travel_submitted_on', e.target.value)}
            />
          </Field>
        </div>

        {save.error && <ErrorNote>{errorMessage(save.error)}</ErrorNote>}

        <div className="flex justify-end gap-2">
          <Button type="button" onClick={onClose}>
            Cancel
          </Button>
          <Button
            type="submit"
            variant="primary"
            disabled={save.isPending || datesOutOfOrder || batches.rows.length === 0}
          >
            {deployment ? 'Save changes' : 'Create deployment'}
          </Button>
        </div>
      </form>
    </Modal>
  )
}
