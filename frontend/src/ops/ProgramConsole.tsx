import { useMemo, useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { supabase, errorMessage, unwrap } from '../lib/supabase'
import { PAGE, bounded, emptyBound, usePageLimit, type Bounded } from '../lib/bounds'
import { qk } from '../lib/queryKeys'
import { generateDocuments, generateTasks } from '../lib/api'
import {
  STAGES,
  STAGE_BLURB,
  STAGE_LABEL,
  type College,
  type Program,
  type ProgramStage,
  type ProgramType,
} from '../lib/types'
import { Page, PageHeader } from '../components/AppShell'
import {
  Badge,
  BoundNote,
  Button,
  Card,
  EmptyState,
  ErrorNote,
  Field,
  HelpTip,
  Input,
  Meter,
  Modal,
  PageIntro,
  SearchInput,
  Select,
  Skeleton,
  Toolbar,
  fmtDate,
} from '../components/ui'

type ProgramRow = Program & { colleges: { name: string } | null }

interface ProgramCard extends Program {
  college_name: string
  tasks_total: number
  tasks_done: number
}

/**
 * The Program Console: one card per college-program, columns are the six
 * pipeline stages. Stage is a program ATTRIBUTE, not a state machine — tasks
 * inside a stage run in parallel — so moving a card is just an UPDATE, with no
 * ordering rules to enforce.
 */
export function ProgramConsole() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [creating, setCreating] = useState(false)
  const [dragId, setDragId] = useState<string | null>(null)
  const [overStage, setOverStage] = useState<ProgramStage | null>(null)

  const programPage = usePageLimit(PAGE.programs)

  const programsQuery = useQuery({
    queryKey: qk.programs.list(programPage.limit),
    queryFn: () =>
      bounded<ProgramRow>(programPage.limit, (rows) =>
        supabase
          .from('programs')
          .select('*, colleges(name)')
          .order('created_at', { ascending: false })
          .limit(rows),
      ),
  })

  const collegesQuery = useQuery({
    queryKey: qk.colleges.list(PAGE.colleges),
    queryFn: () =>
      bounded<College>(PAGE.colleges, (rows) =>
        supabase.from('colleges').select('*').order('name').limit(rows),
      ),
  })

  // Roll tasks up per program client-side. Fine at Phase 1 volumes; this is
  // exactly the kind of thing that becomes a FastAPI roll-up later.
  //
  // THE BOUND MATTERS MORE HERE THAN ANYWHERE ELSE ON THIS SCREEN. Truncating a
  // list makes it short; truncating the input to a roll-up makes every progress
  // meter WRONG — a program whose tasks fell past the bound shows fewer total
  // tasks and therefore a flattering completion percentage. So the meters are
  // suppressed rather than drawn when this read is cut, and the note says why.
  //
  // `.order('program_id')` is load-bearing rather than cosmetic: a limit over an
  // unordered read takes an arbitrary slice, so without it the same query could
  // return a different set of tasks on each refetch and the meters would move
  // for no reason anybody could explain.
  const rollupQuery = useQuery({
    queryKey: qk.tasks.rollup(PAGE.tasks),
    queryFn: async () => {
      const result = await bounded<{ program_id: string; status: string }>(PAGE.tasks, (rows) =>
        supabase.from('tasks').select('program_id, status').order('program_id').limit(rows),
      )
      const totals = new Map<string, { total: number; done: number }>()
      for (const t of result.rows) {
        const acc = totals.get(t.program_id) ?? { total: 0, done: 0 }
        acc.total += 1
        if (t.status === 'done') acc.done += 1
        totals.set(t.program_id, acc)
      }
      return { ...result, totals }
    },
  })

  /** Progress is only drawable when the roll-up saw every task. */
  const rollupComplete = rollupQuery.data !== undefined && !rollupQuery.data.truncated

  const programsBound = programsQuery.data ?? emptyBound<ProgramRow>(programPage.limit)

  const cards: ProgramCard[] = useMemo(
    () =>
      programsBound.rows.map((p) => ({
        ...p,
        college_name: p.colleges?.name ?? 'Unknown college',
        tasks_total: rollupQuery.data?.totals.get(p.id)?.total ?? 0,
        tasks_done: rollupQuery.data?.totals.get(p.id)?.done ?? 0,
      })),
    [programsBound.rows, rollupQuery.data],
  )

  const moveTo = useMutation({
    mutationFn: ({ programId, stage }: { programId: string; stage: ProgramStage }) =>
      unwrap(supabase.from('programs').update({ stage }).eq('id', programId)),

    // The board should feel instant, and RLS will reject an unauthorised move,
    // at which point we roll straight back.
    // The key carries the page size, so the optimistic write must use the SAME
    // limit the query was issued with. Writing to `programs.list(200)` while the
    // screen reads `programs.list(400)` patches an entry nobody is looking at
    // and leaves the visible card where it was until the refetch lands.
    onMutate: async ({ programId, stage }) => {
      const key = qk.programs.list(programPage.limit)
      await queryClient.cancelQueries({ queryKey: key })
      const previous = queryClient.getQueryData<Bounded<ProgramRow>>(key)
      queryClient.setQueryData<Bounded<ProgramRow>>(key, (old) =>
        old === undefined
          ? old
          : { ...old, rows: old.rows.map((p) => (p.id === programId ? { ...p, stage } : p)) },
      )
      return { previous }
    },
    onError: (_err, _vars, context) => {
      if (context?.previous) {
        queryClient.setQueryData(qk.programs.list(programPage.limit), context.previous)
      }
    },
    onSettled: () => queryClient.invalidateQueries({ queryKey: qk.programs.all }),
  })

  // Display-only narrowing. Finding one engagement on a six-column board is
  // otherwise a scroll-hunt, and the search box prints "3 of 40" so the reader
  // can always see that the per-stage counts below it are counting a slice.
  const [search, setSearch] = useState('')
  const q = search.trim().toLowerCase()
  const visible = useMemo(
    () =>
      q === ''
        ? cards
        : cards.filter(
            (c) =>
              c.name.toLowerCase().includes(q) || c.college_name.toLowerCase().includes(q),
          ),
    [cards, q],
  )

  const byStage = useMemo(() => {
    const map = new Map<ProgramStage, ProgramCard[]>()
    for (const s of STAGES) map.set(s, [])
    for (const c of visible) map.get(c.stage)?.push(c)
    return map
  }, [visible])

  const colleges = collegesQuery.data?.rows ?? []
  const loading = programsQuery.isPending || collegesQuery.isPending
  const failure = programsQuery.error ?? collegesQuery.error ?? moveTo.error

  return (
    <>
      <PageHeader
        title="Program Console"
        purpose="Every training program you can reach, laid out left to right by how far along it is. Drag a card into the next column when that program moves on."
        subtitle={
          `${cards.length}${programsBound.truncated ? '+' : ''} ` +
          `program${cards.length === 1 ? '' : 's'} across ` +
          `${colleges.length}${collegesQuery.data?.truncated ? '+' : ''} ` +
          `college${colleges.length === 1 ? '' : 's'} you reach`
        }
        actions={
          <Button variant="primary" size="sm" onClick={() => setCreating(true)}>
            New program
          </Button>
        }
      />

      <Page>
        {failure && (
          <div className="mb-4">
            <ErrorNote>{errorMessage(failure)}</ErrorNote>
          </div>
        )}

        <PageIntro
          className="mb-4"
          steps={[
            'Sign the MoU and PO, set the batches and dates',
            'Ask TA to source trainers',
            'Work order, ZOHO and ERM before anyone travels',
            'Deploy — credentials, travel, intro call',
            'Run it: attendance, observations, feedback',
            'Close it out: remuneration, invoices, payout',
          ]}
        >
          A program is one college buying one block of training. Each card sits in the column
          for the stage it has reached, and the six columns are that lifecycle in order. The
          type on a card is the thing to read first:{' '}
          <HelpTip term="CRT">
            Campus Recruitment Training. The trainer is paid <strong>per day</strong>, and
            payable days are counted <strong>up</strong> from the days actually marked
            present — so an unmarked day pays nothing and attendance must be complete before
            a payout can proceed.
          </HelpTip>{' '}
          and{' '}
          <HelpTip term="bCAP">
            A monthly retainer, prorated across the calendar days of the month. Payable days
            are counted <strong>down</strong> from the full period, so weekends, college
            holidays and unmarked days all still pay — the retainer absorbs them.
          </HelpTip>{' '}
          are paid on completely different bases, and that changes what attendance means
          later on.
        </PageIntro>

        {loading ? (
          <div
            role="status"
            aria-label="Loading pipeline"
            className="flex gap-3 overflow-hidden pb-4"
          >
            {STAGES.map((s) => (
              <div key={s} className="w-72 shrink-0 rounded-xl border border-line bg-surface-2/50 p-3 space-y-2">
                <Skeleton className="h-3 w-32" />
                <Skeleton className="h-16 w-full rounded-lg" />
                <Skeleton className="h-16 w-full rounded-lg" />
              </div>
            ))}
          </div>
        ) : (
          <>
            <div className="mb-4 space-y-2">
              <BoundNote
                bound={programsBound}
                noun="programs"
                derived="The per-stage counts cover only those."
                onMore={programPage.more}
                step={programPage.step}
              />
              <BoundNote
                bound={rollupQuery.data}
                noun="checklist rows"
                derived={
                  'Progress meters are hidden rather than drawn from a partial count — a card ' +
                  'would otherwise show fewer tasks than it has and read as further along.'
                }
              />
            </div>

            <Toolbar className="mb-4">
              <SearchInput
                value={search}
                onChange={setSearch}
                placeholder="Search program or college…"
                count={visible.length}
                total={cards.length}
              />
              {/* Said out loud rather than left to be noticed: with a search
                  active, the number on each column heading is a count of what
                  survived it, not of the stage. */}
              {q !== '' && (
                <span className="text-xs text-ink-3">
                  Column counts show matches only.
                </span>
              )}
            </Toolbar>

            {cards.length === 0 && (
              <Card className="mb-4">
                <EmptyState
                  title="No programs yet"
                  body="A program is one college buying one block of training. The columns below are the stages it will move through."
                  hint="Nothing is being hidden from you — no program has been created on a college you reach. Creating one also generates its full task checklist and document register."
                  action={
                    <Button variant="primary" size="sm" onClick={() => setCreating(true)}>
                      Create the first program
                    </Button>
                  }
                />
              </Card>
            )}

            {cards.length > 0 && visible.length === 0 && (
              <Card className="mb-4">
                <EmptyState
                  title="No program matches that search"
                  body={`None of your ${cards.length} programs mention “${search.trim()}”.`}
                  hint="This searches the program name and its college. A program on a college you are not assigned to never reaches this board at all."
                  action={
                    <Button size="sm" onClick={() => setSearch('')}>
                      Clear the search
                    </Button>
                  }
                />
              </Card>
            )}

            <div className="flex gap-3 overflow-x-auto scroll-slim pb-4 -mx-1 px-1">
            {STAGES.map((stage) => {
              const items = byStage.get(stage) ?? []
              const isOver = overStage === stage
              return (
                <section
                  key={stage}
                  onDragOver={(e) => {
                    e.preventDefault()
                    setOverStage(stage)
                  }}
                  onDragLeave={() => setOverStage((s) => (s === stage ? null : s))}
                  onDrop={(e) => {
                    e.preventDefault()
                    setOverStage(null)
                    if (dragId) moveTo.mutate({ programId: dragId, stage })
                    setDragId(null)
                  }}
                  className={`w-72 shrink-0 rounded-xl border transition ${
                    isOver ? 'border-accent bg-accent-soft/40' : 'border-line bg-surface-2/50'
                  }`}
                >
                  <div className="px-3 pt-3 pb-2">
                    <div className="flex items-center justify-between gap-2">
                      <h2 className="text-xs font-semibold uppercase tracking-wide text-ink-2">
                        {STAGE_LABEL[stage]}
                      </h2>
                      <Badge>{items.length}</Badge>
                    </div>
                    <p className="text-[11px] text-ink-3 mt-1 leading-snug">
                      {STAGE_BLURB[stage]}
                    </p>
                  </div>

                  <div className="p-2 pt-0 space-y-2 min-h-24">
                    {items.map((card) => (
                      <article
                        key={card.id}
                        draggable
                        onDragStart={() => setDragId(card.id)}
                        onDragEnd={() => setDragId(null)}
                        onClick={() => navigate(`/programs/${card.id}`)}
                        className={`cursor-pointer rounded-lg border border-line bg-surface p-3
                          shadow-[0_1px_2px_rgb(0_0_0/0.04)] transition hover:border-accent/50
                          hover:shadow-md active:cursor-grabbing ${
                            dragId === card.id ? 'opacity-40' : ''
                          }`}
                      >
                        <div className="flex items-start justify-between gap-2">
                          <h3 className="text-sm font-medium text-ink leading-snug">
                            {card.name}
                          </h3>
                          {/* One line rather than a HelpTip: a "?" on every
                              card would out-shout the program name, and the
                              full definition is already in the intro above. */}
                          <span
                            className="shrink-0"
                            title={
                              card.type === 'CRT'
                                ? 'CRT — the trainer is paid per day, and payable days are counted up from the days marked present.'
                                : 'bCAP — a monthly retainer prorated by calendar days; payable days are counted down from the full period.'
                            }
                          >
                            <Badge tone="accent">{card.type}</Badge>
                          </span>
                        </div>
                        <p className="text-xs text-ink-2 mt-1 truncate">{card.college_name}</p>

                        {/* Progress is drawn ONLY when the roll-up read every
                            task. A meter over a truncated count is not a rough
                            figure, it is a wrong one in the flattering
                            direction — fewer tasks total, same number done —
                            and a card that says a program is further along than
                            it is, is the one thing this board must not do. */}
                        {rollupComplete ? (
                          <div className="mt-3 flex items-center gap-2">
                            <Meter
                              pct={
                                card.tasks_total === 0
                                  ? 0
                                  : (100 * card.tasks_done) / card.tasks_total
                              }
                            />
                            <span className="text-[11px] tabular-nums text-ink-3 shrink-0">
                              {card.tasks_done}/{card.tasks_total}
                            </span>
                          </div>
                        ) : (
                          <p
                            className="text-[11px] text-ink-3 mt-3"
                            title="The checklist read was cut at its row bound, so a completion figure for this program would be understated."
                          >
                            Progress unavailable
                          </p>
                        )}

                        <p className="text-[11px] text-ink-3 mt-2">
                          {fmtDate(card.start_date)} → {fmtDate(card.end_date)}
                        </p>
                      </article>
                    ))}

                    {items.length === 0 && (
                      <p className="text-center text-[11px] text-ink-3 py-6 px-2 leading-relaxed">
                        Nothing at this stage.
                        <br />
                        Drag a card here to move a program into it.
                      </p>
                    )}
                  </div>
                </section>
              )
            })}
            </div>
          </>
        )}
      </Page>

      <NewProgramModal
        open={creating}
        colleges={colleges}
        onClose={() => setCreating(false)}
      />
    </>
  )
}

function NewProgramModal({
  open,
  colleges,
  onClose,
}: {
  open: boolean
  colleges: College[]
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const [name, setName] = useState('')
  const [collegeId, setCollegeId] = useState('')
  const [type, setType] = useState<ProgramType>('bCAP')
  const [startDate, setStartDate] = useState('')
  const [endDate, setEndDate] = useState('')
  const [status, setStatus] = useState<string | null>(null)

  const create = useMutation({
    mutationFn: async () => {
      const program = await unwrap<Program>(
        supabase
          .from('programs')
          .insert({
            name,
            college_id: collegeId,
            type,
            start_date: startDate || null,
            end_date: endDate || null,
          })
          .select()
          .single(),
      )

      // Checklist and register generation belong to FastAPI (see lib/api.ts).
      // They are a SEPARATE failure from creating the program: if the API is
      // down the program still exists and can be topped up later from its
      // detail page, so the error says exactly that rather than implying the
      // create rolled back.
      setStatus('Generating checklist and document register…')
      try {
        const [tasks, documents] = await Promise.all([
          generateTasks(program.id),
          generateDocuments(program.id),
        ])
        return {
          program,
          note: `Created ${tasks.created} tasks and ${documents.created} documents.`,
        }
      } catch (err) {
        return {
          program,
          note:
            `Program created, but generation failed: ${errorMessage(err)} ` +
            'Open the program and use "Top up from templates" once the API is reachable.',
        }
      }
    },
    onSuccess: ({ note }) => {
      setStatus(note)
      setName('')
      setCollegeId('')
      setStartDate('')
      setEndDate('')
      void queryClient.invalidateQueries({ queryKey: qk.programs.all })
      void queryClient.invalidateQueries({ queryKey: qk.tasks.all })
      void queryClient.invalidateQueries({ queryKey: qk.documents.all })
    },
    onError: () => setStatus(null),
  })

  function onSubmit(e: FormEvent) {
    e.preventDefault()
    create.mutate()
  }

  return (
    <Modal open={open} onClose={onClose} title="New program">
      {colleges.length === 0 ? (
        <p className="text-sm text-ink-2 leading-relaxed">
          No colleges are visible to you, and a program must belong to one. Either
          none exist yet, or none are assigned to you — an admin grants reach on
          Users &amp; roles.
        </p>
      ) : (
        <form onSubmit={onSubmit} className="space-y-4">
          <Field label="Program name">
            <Input
              required
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Alpha bCAP 2026"
            />
          </Field>

          <Field label="College">
            <Select required value={collegeId} onChange={(e) => setCollegeId(e.target.value)}>
              <option value="">Select a college…</option>
              {colleges.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </Select>
          </Field>

          <Field
            label="Type"
            hint="CRT pays per day and counts payable days UP from present marks; bCAP pays per month and counts DOWN. It changes what attendance means."
          >
            <Select value={type} onChange={(e) => setType(e.target.value as ProgramType)}>
              <option value="bCAP">bCAP</option>
              <option value="CRT">CRT</option>
            </Select>
          </Field>

          <div className="grid grid-cols-2 gap-3">
            <Field label="Start date">
              <Input
                type="date"
                value={startDate}
                onChange={(e) => setStartDate(e.target.value)}
              />
            </Field>
            <Field label="End date">
              <Input type="date" value={endDate} onChange={(e) => setEndDate(e.target.value)} />
            </Field>
          </div>

          <p className="text-xs text-ink-3 leading-relaxed">
            Creating the program generates its full stage checklist and its document
            register. Due dates are computed from each template's lead time relative
            to the dates above — so set them if you know them.
          </p>

          {create.error && <ErrorNote>{errorMessage(create.error)}</ErrorNote>}
          {status && <p className="text-xs text-ink-2 leading-relaxed">{status}</p>}

          <div className="flex justify-end gap-2 pt-1">
            <Button type="button" onClick={onClose}>
              Close
            </Button>
            <Button type="submit" variant="primary" disabled={create.isPending}>
              Create program
            </Button>
          </div>
        </form>
      )}
    </Modal>
  )
}
