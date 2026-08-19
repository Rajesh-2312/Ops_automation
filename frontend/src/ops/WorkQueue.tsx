import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { supabase, errorMessage, unwrap } from '../lib/supabase'
import { PAGE, bounded, emptyBound, usePageLimit, type Bounded } from '../lib/bounds'
import { qk } from '../lib/queryKeys'
import {
  CADENCES,
  CADENCE_LABEL,
  STAGE_LABEL,
  URGENCY_LABEL,
  deriveUrgency,
  dueLabel,
  type Task,
  type TaskCadence,
  type TaskStatus,
  type TaskUrgency,
} from '../lib/types'
import { useAuth } from '../auth/AuthProvider'
import { Page, PageHeader } from '../components/AppShell'
import {
  Badge,
  BoundNote,
  Button,
  CadencePill,
  Card,
  EmptyState,
  ErrorNote,
  FilterChip,
  HelpTip,
  Legend,
  SearchInput,
  Select,
  TableSkeleton,
  Toolbar,
  fmtDate,
} from '../components/ui'

interface QueueTask extends Task {
  program_name: string
  college_name: string
}

type TaskRow = Task & {
  programs: { name: string; colleges: { name: string } | null } | null
}

/**
 * The internal home screen.
 *
 * The Program Console is a MANAGEMENT view — it answers "where is every
 * engagement in the pipeline". That is the wrong first question at 9am. An ops
 * person needs "what is on me today, across every college I reach", which cuts
 * across program, stage and cadence, and no board arranged by stage can show
 * it.
 *
 * So this screen is arranged by URGENCY instead: overdue first, then today,
 * then the next three days, then blocked work that needs chasing. Everything
 * further out is deliberately not shown — a queue that lists work due in six
 * weeks is a list, not a queue.
 *
 * The query is unfiltered by college on purpose. `tasks_internal_all` already
 * restricts it to programs the caller can reach, so a Senior Manager sees their
 * clusters and an LDE Executive sees their campus from the identical statement.
 */
export function WorkQueue() {
  const { profile } = useAuth()
  const queryClient = useQueryClient()
  const [mine, setMine] = useState(false)
  const [cadence, setCadence] = useState<TaskCadence | 'all'>('all')
  // Free-text narrowing over what is already on screen. It searches the task,
  // the college and the programme name together, because on a cross-programme
  // queue "Malineni" and "attendance" are the same kind of question.
  const [search, setSearch] = useState('')

  // BOUNDED. `tasks` is 37 rows per program straight out of seed.sql, so 500
  // programs is 18,500 rows — and its policy is `is_internal() and
  // can_reach_program(program_id)`, a per-row SECURITY DEFINER call measured at
  // 523 µs. Unbounded, that queue is a nine-second request (architecture review
  // §1.3). `.order('due_date')` already puts the rows that matter at the front
  // of the bound, which is what makes cutting the tail defensible.
  const page = usePageLimit(PAGE.tasks)

  const {
    data: bound = emptyBound<QueueTask>(page.limit),
    isPending,
    error,
    refetch,
    isFetching,
  } = useQuery({
    queryKey: qk.tasks.queue(page.limit),
    queryFn: async (): Promise<Bounded<QueueTask>> => {
      const result = await bounded<TaskRow>(page.limit, (rows) =>
        supabase
          .from('tasks')
          .select('*, programs(name, colleges(name))')
          .neq('status', 'done')
          .order('due_date', { nullsFirst: false })
          .limit(rows),
      )
      return {
        ...result,
        rows: result.rows.map((t) => ({
          ...t,
          program_name: t.programs?.name ?? '—',
          college_name: t.programs?.colleges?.name ?? '—',
        })),
      }
    },
  })

  const tasks = bound.rows

  const setStatus = useMutation({
    mutationFn: async ({ id, status }: { id: string; status: TaskStatus }) =>
      unwrap(supabase.from('tasks').update({ status }).eq('id', id).select().maybeSingle()),

    // Optimistic: ticking a checkbox should feel instant. On failure the
    // snapshot goes back, which is also what happens when RLS refuses.
    // The key carries the page size, so the optimistic write has to use the
    // SAME limit the query was issued with — writing to `qk.tasks.queue(200)`
    // while the screen is reading `qk.tasks.queue(400)` would patch a cache
    // entry nobody is looking at and leave the visible one stale.
    onMutate: async ({ id, status }) => {
      const key = qk.tasks.queue(page.limit)
      await queryClient.cancelQueries({ queryKey: key })
      const previous = queryClient.getQueryData<Bounded<QueueTask>>(key)
      queryClient.setQueryData<Bounded<QueueTask>>(key, (old) =>
        old === undefined
          ? old
          : {
              ...old,
              rows: old.rows
                .map((t) => (t.id === id ? { ...t, status } : t))
                // The queue only holds open work, so a completed task leaves it.
                .filter((t) => t.status !== 'done'),
            },
      )
      return { previous }
    },
    onError: (_err, _vars, context) => {
      if (context?.previous) {
        queryClient.setQueryData(qk.tasks.queue(page.limit), context.previous)
      }
    },
    // The board's per-program roll-up counts the same rows this screen just
    // changed. Invalidating the whole `tasks` namespace keeps them agreeing
    // without either screen knowing about the other.
    onSettled: () => queryClient.invalidateQueries({ queryKey: qk.tasks.all }),
  })

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase()
    return tasks.filter(
      (t) =>
        (!mine || t.owner_id === profile?.id) &&
        (cadence === 'all' || t.cadence === cadence) &&
        (q === '' ||
          t.title.toLowerCase().includes(q) ||
          t.college_name.toLowerCase().includes(q) ||
          t.program_name.toLowerCase().includes(q)),
    )
  }, [tasks, mine, cadence, search, profile?.id])

  /** Only the bands that mean "act now". Anything scheduled further out is
   *  intentionally excluded — see the component comment. */
  const buckets = useMemo(() => {
    const order: TaskUrgency[] = ['overdue', 'due_today', 'due_soon', 'blocked']
    const map = new Map<TaskUrgency, QueueTask[]>(order.map((u) => [u, []]))
    for (const t of filtered) map.get(deriveUrgency(t))?.push(t)
    return order.map((u) => ({ urgency: u, items: map.get(u) ?? [] }))
  }, [filtered])

  const cadenceCounts = useMemo(() => {
    const counts = Object.fromEntries(CADENCES.map((c) => [c, 0])) as Record<
      TaskCadence,
      number
    >
    for (const t of tasks) if (!mine || t.owner_id === profile?.id) counts[t.cadence] += 1
    return counts
  }, [tasks, mine, profile?.id])

  const actionable = buckets.reduce((n, b) => n + b.items.length, 0)
  const mineCount = tasks.filter((t) => t.owner_id === profile?.id).length
  /** Is the reader looking at a narrowed view? Decides which empty state to show. */
  const narrowed = mine || cadence !== 'all' || search.trim() !== ''
  const failure = error ?? setStatus.error

  return (
    <>
      <PageHeader
        title="Work queue"
        purpose="Everything still open across every college you can reach, most overdue first. Work due further out is left off on purpose — this is the list for today, not the plan for the month."
        subtitle={
          isPending
            ? 'Loading…'
            : `${actionable} item${actionable === 1 ? '' : 's'} need attention · ${tasks.length}${
                bound.truncated ? '+' : ''
              } open overall`
        }
        actions={
          <Button size="sm" variant="ghost" disabled={isFetching} onClick={() => void refetch()}>
            Refresh
          </Button>
        }
      />

      <Page>
        {failure && (
          <div className="mb-4">
            <ErrorNote>{errorMessage(failure)}</ErrorNote>
          </div>
        )}

        {isPending ? (
          <Card className="max-w-4xl">
            <TableSkeleton rows={6} cols={3} />
          </Card>
        ) : (
          <div className="max-w-4xl space-y-4">
            {/* Every count on this screen — the chips, the band headings, the
                subtitle — is computed from `tasks`, so a truncated read makes
                all of them counts of the page rather than of the queue. Say so
                where they are read, not in a tooltip. */}
            <BoundNote
              bound={bound}
              noun="open tasks"
              derived="Every count on this screen covers only those rows."
              onMore={page.more}
              step={page.step}
            />

            <Toolbar>
              <SearchInput
                value={search}
                onChange={setSearch}
                placeholder="Search task, college or program…"
                count={filtered.length}
                total={tasks.length}
              />
            </Toolbar>

            <Toolbar className="gap-1.5">
              <FilterChip
                label="Everyone"
                count={tasks.length}
                active={!mine}
                onClick={() => setMine(false)}
              />
              <FilterChip
                label="Assigned to me"
                count={mineCount}
                active={mine}
                onClick={() => setMine(true)}
              />
              <span className="w-px h-5 bg-line mx-1" aria-hidden />
              {/* "Rhythm" is the plain-English word for the cadence stored on a
                  task. Explaining it here rather than on every chip keeps the
                  row scannable — the chips beside it are the vocabulary. */}
              <span className="text-xs text-ink-2 mr-0.5">
                <HelpTip term="Rhythm">
                  How often a task comes back round. A one-off happens once for the whole
                  program; a daily, weekly, monthly or quarterly task reappears on that beat
                  for as long as the program runs — attendance is daily, a governance report
                  is monthly.
                </HelpTip>
              </span>
              <FilterChip
                label="Any rhythm"
                count={filtered.length}
                active={cadence === 'all'}
                onClick={() => setCadence('all')}
              />
              {CADENCES.map((c) => (
                <FilterChip
                  key={c}
                  label={CADENCE_LABEL[c]}
                  count={cadenceCounts[c]}
                  active={cadence === c}
                  onClick={() => setCadence(cadence === c ? 'all' : c)}
                />
              ))}
            </Toolbar>

            {/* The bands below are colour-coded and the colour is load-bearing:
                red is late, amber is a human decision waiting. Say so once,
                above them, rather than hoping it is inferred. */}
            <Legend
              className="px-0.5"
              items={[
                {
                  swatch: 'bg-bad-wash border-bad/60',
                  label: 'Overdue',
                  hint: 'the due date has passed',
                },
                {
                  swatch: 'bg-warn-wash border-warn/60',
                  label: 'Due today',
                  hint: 'finish before you log off',
                },
                {
                  swatch: 'bg-warn-wash border-warn/30',
                  label: 'Due soon',
                  hint: 'within the next three days',
                },
                {
                  swatch: 'bg-bad-wash border-bad/30',
                  label: 'Blocked',
                  hint: 'waiting on someone else — chase it',
                },
              ]}
            />

            {actionable === 0 ? (
              <Card>
                {/* Three different silences, and they need opposite reactions:
                    a filter is hiding the work, there is genuinely no work, or
                    the work exists but is not due yet. Saying "Nothing needs
                    attention" to all three teaches a reader to distrust it. */}
                {narrowed ? (
                  <EmptyState
                    title="Nothing matches these filters"
                    body="There is open work in your queue, but none of it survives the search box and chips above."
                    hint="Clear the filters to see the whole queue. A search that silently hides a college is the usual reason someone concludes a task was never created."
                    action={
                      <Button
                        size="sm"
                        onClick={() => {
                          setSearch('')
                          setMine(false)
                          setCadence('all')
                        }}
                      >
                        Clear all filters
                      </Button>
                    }
                  />
                ) : (
                  <EmptyState
                    title="Nothing needs attention"
                    body={
                      tasks.length === 0
                        ? 'No open tasks at all. Either nothing is assigned to the colleges you reach, or a program still needs its checklist generated.'
                        : 'Everything open is scheduled further out. The Program Console has the full picture.'
                    }
                    hint={
                      tasks.length === 0
                        ? 'A new program starts with no tasks until someone generates its checklist from the master list — that is done on the program itself, under Tasks.'
                        : 'This queue only shows work that is overdue, due in the next three days, or blocked. Everything else is waiting for its date.'
                    }
                    action={
                      <Link to="/board">
                        <Button size="sm">Open Program Console</Button>
                      </Link>
                    }
                  />
                )}
              </Card>
            ) : (
              buckets.map(
                ({ urgency, items }) =>
                  items.length > 0 && (
                    <Card
                      key={urgency}
                      className={urgency === 'overdue' ? 'border-bad/30' : undefined}
                    >
                      <div className="flex items-center justify-between gap-3 px-4 py-3 border-b border-line">
                        <h2
                          className={`text-sm font-semibold ${
                            urgency === 'overdue'
                              ? 'text-bad-ink'
                              : 'text-ink'
                          }`}
                        >
                          {URGENCY_LABEL[urgency]}
                        </h2>
                        <span className="text-xs tabular-nums text-ink-3">{items.length}</span>
                      </div>

                      <ul className="divide-y divide-line-soft">
                        {items.map((task) => (
                          <li
                            key={task.id}
                            className="flex items-start gap-3 px-4 py-3 hover:bg-surface-2/60 transition"
                          >
                            <input
                              type="checkbox"
                              className="mt-1 h-4 w-4 shrink-0 accent-[var(--color-accent)] cursor-pointer"
                              checked={false}
                              disabled={setStatus.isPending}
                              onChange={() => setStatus.mutate({ id: task.id, status: 'done' })}
                              aria-label={`Mark ${task.title} done`}
                            />

                            <div className="min-w-0 flex-1">
                              <p className="text-sm text-ink leading-snug">{task.title}</p>

                              {/* Which engagement this belongs to is the single
                                  most important thing on a cross-program queue
                                  — without it the list is unusable. */}
                              <Link
                                to={`/programs/${task.program_id}`}
                                className="text-xs text-accent hover:underline mt-0.5 inline-block"
                              >
                                {task.college_name} · {task.program_name}
                              </Link>

                              <div className="flex flex-wrap items-center gap-1.5 mt-1.5">
                                <Badge>{STAGE_LABEL[task.stage]}</Badge>
                                <CadencePill cadence={task.cadence} />
                                {task.due_date && (
                                  <span
                                    className={`text-[11px] ${
                                      urgency === 'overdue'
                                        ? 'font-medium text-bad-ink'
                                        : 'text-ink-3'
                                    }`}
                                  >
                                    {dueLabel(task)} · {fmtDate(task.due_date)}
                                  </span>
                                )}
                              </div>

                              {task.status === 'blocked' && (
                                <p className="text-xs text-warn-ink mt-1.5">
                                  Blocked
                                  {task.waiting_on && ` on ${task.waiting_on}`}
                                  {task.blocked_reason && ` — ${task.blocked_reason}`}
                                </p>
                              )}
                            </div>

                            <Select
                              className="!h-7 !w-24 !text-xs shrink-0"
                              value={task.status}
                              disabled={setStatus.isPending}
                              onChange={(e) =>
                                setStatus.mutate({
                                  id: task.id,
                                  status: e.target.value as TaskStatus,
                                })
                              }
                              aria-label="Status"
                            >
                              <option value="pending">Pending</option>
                              <option value="in_progress">In progress</option>
                              <option value="done">Done</option>
                              <option value="blocked">Blocked</option>
                            </Select>
                          </li>
                        ))}
                      </ul>
                    </Card>
                  ),
              )
            )}
          </div>
        )}
      </Page>
    </>
  )
}
