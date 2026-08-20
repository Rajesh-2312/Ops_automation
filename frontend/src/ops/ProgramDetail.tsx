import { useMemo, useState, type FormEvent } from 'react'
import { PAGE, bounded, emptyBound, type Bounded } from '../lib/bounds'
import { Link, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { supabase, errorMessage, unwrap } from '../lib/supabase'
import { qk } from '../lib/queryKeys'
import { generateTasks } from '../lib/api'
import {
  CADENCES,
  CADENCE_LABEL,
  ROLE_LABEL,
  STAGES,
  STAGE_LABEL,
  URGENT_BANDS,
  deriveUrgency,
  dueLabel,
  isInternalRole,
  type Batch,
  type College,
  type Profile,
  type Program,
  type ProgramStage,
  type Task,
  type TaskCadence,
  type TaskStatus,
} from '../lib/types'
import { Page, PageHeader } from '../components/AppShell'
import {
  Badge,
  BoundNote,
  Button,
  CadencePill,
  Card,
  EmptyState,
  ErrorNote,
  Field,
  FilterChip,
  fmtDate,
  HelpTip,
  Input,
  Meter,
  Modal,
  PageIntro,
  SearchInput,
  Select,
  Skeleton,
  TableSkeleton,
  Tabs,
  TaskStatusPill,
  Toolbar,
  UrgencyPill,
} from '../components/ui'
import { DocumentRegister } from './DocumentRegister'

type ProgramWithCollege = Program & { colleges: College | null }

/**
 * Program detail: the stage checklist plus the program's batches.
 *
 * The checklist is grouped by stage rather than shown flat, because a stage is
 * a bucket of parallel work, not a step in a sequence.
 */
export function ProgramDetail() {
  const { id } = useParams<{ id: string }>()
  const programId = id ?? ''
  const queryClient = useQueryClient()
  const [addingBatch, setAddingBatch] = useState(false)
  const [filter, setFilter] = useState<TaskCadence | 'all'>('all')
  const [tab, setTab] = useState<'checklist' | 'documents'>('checklist')
  const [generateNote, setGenerateNote] = useState<string | null>(null)
  // Display-only narrowing of the checklist. Thirty-seven items across six
  // stage cards is a lot of scrolling to answer "did anyone raise the work
  // order?", and the chips only narrow by rhythm.
  const [search, setSearch] = useState('')

  /**
   * Which checklist rows are expanded.
   *
   * A SET rather than a single id, so several rows can be open at once. The
   * screen this replaced rendered four controls — due date, cadence, owner,
   * status — on every one of thirty-seven rows at all times, which pushed each
   * title into three wrapped lines and made the checklist several screens tall
   * before anyone had done anything. Collapsed, the same list is a page. But
   * setting due dates across a stage is a real task, and a single-open
   * accordion would make that a sequence of open-edit-close, so opening one row
   * does not close another.
   */
  const [openTasks, setOpenTasks] = useState<ReadonlySet<string>>(() => new Set())

  function toggleTask(id: string) {
    setOpenTasks((prev) => {
      const next = new Set(prev)
      if (!next.delete(id)) next.add(id)
      return next
    })
  }

  /** Which batch is being edited. `null` means the modal is adding a new one. */
  const [editingBatch, setEditingBatch] = useState<Batch | null>(null)

  const programQuery = useQuery({
    queryKey: qk.programs.one(programId),
    enabled: !!programId,
    queryFn: () =>
      unwrap<ProgramWithCollege>(
        supabase.from('programs').select('*, colleges(*)').eq('id', programId).single(),
      ),
  })

  // BOUNDED, generously. One program's checklist is 37 rows out of seed.sql,
  // so 200 is five times a full generated register — but the bound is real
  // rather than decorative because `tasks_internal_all` resolves reach through
  // `can_reach_program()` per row, and a program with a daily cadence item
  // running for a year is not a hypothetical.
  const tasksQuery = useQuery({
    queryKey: qk.tasks.byProgram(programId, PAGE.programTasks),
    enabled: !!programId,
    queryFn: () =>
      bounded<Task>(PAGE.programTasks, (rows) =>
        supabase
          .from('tasks')
          .select('*')
          .eq('program_id', programId)
          .order('stage')
          .order('title')
          .limit(rows),
      ),
  })

  const batchesQuery = useQuery({
    queryKey: qk.batches.byProgram(programId, PAGE.programBatches),
    enabled: !!programId,
    queryFn: () =>
      bounded<Batch>(PAGE.programBatches, (rows) =>
        supabase
          .from('batches')
          .select('*')
          .eq('program_id', programId)
          .order('name')
          .limit(rows),
      ),
  })

  // Candidates for task ownership. Trimmed to the internal personas because a
  // checklist item is byteXL's work — a trainer or college login has no
  // business owning "Send TOC to L&S". Trimming the PICKER is presentation;
  // the tasks policies decide who can actually action a row.
  const ownersQuery = useQuery({
    queryKey: qk.profiles.list(PAGE.profiles),
    queryFn: () =>
      bounded<Profile>(PAGE.profiles, (rows) =>
        supabase.from('profiles').select('*').order('full_name').limit(rows),
      ),
    // NOT a permission filter. `isInternalRole` trims the owner PICKER to the
    // personas whose job a checklist item is; the tasks policies decide who can
    // action a row, in Postgres (R5). The bound above is about volume and the
    // filter here is about presentation — neither is a wall.
    select: (bound) => ({ ...bound, rows: bound.rows.filter((p) => isInternalRole(p.role)) }),
  })

  const program = programQuery.data
  const tasksBound = tasksQuery.data ?? emptyBound<Task>(PAGE.programTasks)
  const tasks = useMemo(() => tasksBound.rows, [tasksBound.rows])
  const batches = batchesQuery.data?.rows ?? []

  const patchTask = useMutation({
    mutationFn: ({ taskId, patch }: { taskId: string; patch: Partial<Task> }) =>
      unwrap<Task>(
        supabase.from('tasks').update(patch).eq('id', taskId).select().maybeSingle(),
      ),
    onMutate: async ({ taskId, patch }) => {
      const key = qk.tasks.byProgram(programId, PAGE.programTasks)
      await queryClient.cancelQueries({ queryKey: key })
      const previous = queryClient.getQueryData<Bounded<Task>>(key)
      queryClient.setQueryData<Bounded<Task>>(key, (old) =>
        old === undefined
          ? old
          : { ...old, rows: old.rows.map((t) => (t.id === taskId ? { ...t, ...patch } : t)) },
      )
      return { previous }
    },
    onError: (_err, _vars, context) => {
      if (context?.previous) {
        queryClient.setQueryData(
          qk.tasks.byProgram(programId, PAGE.programTasks),
          context.previous,
        )
      }
    },
    // Take the server's row back on success: completed_at is stamped by a
    // trigger, not by this client.
    onSuccess: (row) => {
      if (!row) return
      queryClient.setQueryData<Bounded<Task>>(
        qk.tasks.byProgram(programId, PAGE.programTasks),
        (old) =>
          old === undefined
            ? old
            : { ...old, rows: old.rows.map((t) => (t.id === row.id ? row : t)) },
      )
    },
    // Invalidate the whole namespace rather than one page size: the queue and
    // the board may each be holding a different bound, and both counted this
    // task.
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: qk.tasks.all })
    },
  })

  const changeStage = useMutation({
    mutationFn: (stage: ProgramStage) =>
      unwrap(supabase.from('programs').update({ stage }).eq('id', programId)),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: qk.programs.all }),
  })

  const regenerate = useMutation({
    mutationFn: () => generateTasks(programId),
    onSuccess: (result) => {
      setGenerateNote(
        result.created === 0
          ? 'Already up to date — every applicable template is on this program.'
          : `Added ${result.created} task${result.created === 1 ? '' : 's'}.`,
      )
      void queryClient.invalidateQueries({ queryKey: qk.tasks.all })
    },
    onError: () => setGenerateNote(null),
  })

  /** Everything that needs acting on now, regardless of cadence or stage. */
  const urgent = useMemo(
    () => tasks.filter((t) => URGENT_BANDS.includes(deriveUrgency(t))),
    [tasks],
  )

  /** Owner id -> display name, so a collapsed row can name who has it without
   *  scanning the whole profile list on every render. */
  const ownerNames = useMemo(() => {
    const map = new Map<string, string>()
    for (const p of ownersQuery.data?.rows ?? []) {
      map.set(p.id, p.full_name ?? p.id.slice(0, 8))
    }
    return map
  }, [ownersQuery.data?.rows])

  const cadenceCounts = useMemo(() => {
    const counts = Object.fromEntries(CADENCES.map((c) => [c, 0])) as Record<
      TaskCadence,
      number
    >
    for (const t of tasks) counts[t.cadence] += 1
    return counts
  }, [tasks])

  const visible = useMemo(() => {
    const q = search.trim().toLowerCase()
    return tasks.filter(
      (t) =>
        (filter === 'all' || t.cadence === filter) &&
        (q === '' ||
          t.title.toLowerCase().includes(q) ||
          (t.description ?? '').toLowerCase().includes(q)),
    )
  }, [tasks, filter, search])

  const byStage = useMemo(() => {
    const map = new Map<ProgramStage, Task[]>()
    for (const s of STAGES) map.set(s, [])
    for (const t of visible) map.get(t.stage)?.push(t)
    return map
  }, [visible])

  const done = tasks.filter((t) => t.status === 'done').length
  const failure =
    programQuery.error ??
    tasksQuery.error ??
    batchesQuery.error ??
    patchTask.error ??
    changeStage.error ??
    regenerate.error

  if (programQuery.isPending) {
    return (
      <Page>
        <div role="status" aria-label="Loading program" className="space-y-4">
          <Skeleton className="h-6 w-72" />
          <Skeleton className="h-3.5 w-96" />
          <Card className="overflow-hidden">
            <TableSkeleton rows={8} cols={3} />
          </Card>
        </div>
      </Page>
    )
  }
  if (!program) {
    return (
      <Page>
        <ErrorNote>
          {failure
            ? errorMessage(failure)
            : 'Program not found, or not visible to your role. Reach comes from your college and cluster assignments.'}
        </ErrorNote>
      </Page>
    )
  }

  return (
    <>
      <PageHeader
        title={program.name}
        purpose="One college's block of training, end to end: everything that has to be done for it, everything that has to be filed for it, and the student batches it runs for."
        subtitle={`${program.colleges?.name ?? '—'} · ${program.type} · ${fmtDate(program.start_date)} → ${fmtDate(program.end_date)}`}
        actions={
          <>
            <Link to="/board">
              <Button size="sm" variant="ghost">
                ← Board
              </Button>
            </Link>
            <Select
              className="w-52"
              value={program.stage}
              onChange={(e) => changeStage.mutate(e.target.value as ProgramStage)}
              aria-label="Program stage"
            >
              {STAGES.map((s) => (
                <option key={s} value={s}>
                  {STAGE_LABEL[s]}
                </option>
              ))}
            </Select>
          </>
        }
      />

      <Page>
        {failure && (
          <div className="mb-4">
            <ErrorNote>{errorMessage(failure)}</ErrorNote>
          </div>
        )}

        <PageIntro className="mb-4">
          This program is a{' '}
          {program.type === 'CRT' ? (
            <HelpTip term="CRT">
              Campus Recruitment Training. Trainers on it are paid <strong>per day</strong>,
              and payable days are counted <strong>up</strong> from the days marked present —
              so an unmarked day pays nothing, and the attendance for a month has to be
              complete before a payout can even be submitted.
            </HelpTip>
          ) : (
            <HelpTip term="bCAP">
              A monthly retainer, prorated across the calendar days of the month. Payable
              days are counted <strong>down</strong> from the full period, so weekends,
              college holidays and unmarked days all still pay — the retainer absorbs them.
            </HelpTip>
          )}{' '}
          program. The two tabs below are the two halves of running one: the{' '}
          <strong>Checklist</strong> is what has to be <em>done</em>, the{' '}
          <strong>Document register</strong> is what has to be <em>filed</em>. The dropdown in
          the header moves the whole program to a different stage; the panel on the right
          holds its student batches and the college it belongs to.
        </PageIntro>

        {/* Checklist and documents are the two halves of running a program:
            what has to be DONE, and what has to be FILED. Tabs rather than one
            long scroll, because they are used at different moments. */}
        <Tabs
          tabs={[
            {
              id: 'checklist' as const,
              label: 'Checklist',
              hint: tasks.length > 0 ? `${done}/${tasks.length}` : undefined,
            },
            { id: 'documents' as const, label: 'Document register' },
          ]}
          active={tab}
          onChange={setTab}
        />

        {tab === 'documents' ? (
          <div className="max-w-3xl">
            <DocumentRegister program={program} />
          </div>
        ) : (
          <div className="grid lg:grid-cols-[1fr_20rem] gap-5 items-start">
            {/* --- Checklist ------------------------------------------------- */}
            <div className="space-y-4 min-w-0">
              <Card className="p-4">
                <div className="flex items-center justify-between gap-3 mb-2">
                  <span className="text-sm font-medium text-ink">Checklist progress</span>
                  {/* Always the whole checklist, never the filtered slice — the
                      chips and the search box above must not be able to move
                      the answer to "how far along is this program". */}
                  <span className="text-xs tabular-nums text-ink-2">
                    {done} of {tasks.length} done
                  </span>
                </div>
                <Meter pct={tasks.length ? (100 * done) / tasks.length : 0} />
                <p className="text-xs text-ink-3 mt-2 leading-relaxed">
                  Every task on this program, whatever stage it sits in. Filters below never
                  change this figure.
                </p>
              </Card>

              {/* Needs attention: pinned above the checklist and never filtered,
                  because the whole point is that it cuts across stage and
                  cadence. Hidden entirely when empty rather than showing a
                  green all-clear that would take up room on every screen. */}
              {urgent.length > 0 && (
                <Card className="border-bad/30">
                  <div className="flex items-center justify-between gap-3 px-4 py-3 border-b border-line">
                    <h3 className="text-sm font-semibold text-bad-ink">
                      Needs attention
                    </h3>
                    <span className="text-xs tabular-nums text-ink-3">
                      {urgent.length} task{urgent.length === 1 ? '' : 's'}
                    </span>
                  </div>
                  <ul className="divide-y divide-line-soft">
                    {urgent.map((task) => (
                      <li key={task.id} className="flex items-center gap-3 px-4 py-2.5">
                        <input
                          type="checkbox"
                          className="h-4 w-4 shrink-0 accent-[var(--color-accent)] cursor-pointer"
                          checked={false}
                          onChange={() =>
                            patchTask.mutate({ taskId: task.id, patch: { status: 'done' } })
                          }
                          aria-label={`Mark ${task.title} done`}
                        />
                        <div className="min-w-0 flex-1">
                          <p className="text-sm text-ink leading-snug">{task.title}</p>
                          <p className="text-[11px] text-ink-3 mt-0.5">
                            {STAGE_LABEL[task.stage]}
                          </p>
                        </div>
                        <div className="flex items-center gap-1.5 shrink-0">
                          <CadencePill cadence={task.cadence} />
                          <UrgencyPill urgency={deriveUrgency(task)} />
                        </div>
                      </li>
                    ))}
                  </ul>
                </Card>
              )}

              {tasksQuery.isPending ? (
                /* Shaped like the checklist it replaces rather than a centred
                   spinner, so the page does not jump when the rows land. */
                <Card className="p-4">
                  <div role="status" aria-label="Loading checklist" className="space-y-3">
                    {Array.from({ length: 5 }, (_, i) => (
                      <div key={i} className="flex items-center gap-3">
                        <Skeleton className="h-4 w-4 rounded" />
                        <Skeleton className="h-3.5 flex-1 rounded" />
                        <Skeleton className="h-3.5 w-16 rounded" />
                      </div>
                    ))}
                  </div>
                </Card>
              ) : tasks.length === 0 ? (
                <Card>
                  <EmptyState
                    title="No checklist yet"
                    body="Generate this program's tasks from the stage templates."
                    action={
                      <Button
                        variant="primary"
                        size="sm"
                        disabled={regenerate.isPending}
                        onClick={() => regenerate.mutate()}
                      >
                        Generate checklist
                      </Button>
                    }
                  />
                </Card>
              ) : (
                <>
                  <BoundNote
                    bound={tasksBound}
                    noun="checklist rows"
                    derived="The cadence chip counts and the completion figure cover only those."
                  />

                  <Toolbar>
                    <div className="flex flex-wrap items-center gap-1.5">
                      <FilterChip
                        label="All"
                        count={tasks.length}
                        active={filter === 'all'}
                        onClick={() => setFilter('all')}
                      />
                      <span className="w-px h-5 bg-line mx-1" aria-hidden />
                      {CADENCES.map((c) => (
                        <FilterChip
                          key={c}
                          label={CADENCE_LABEL[c]}
                          count={cadenceCounts[c]}
                          active={filter === c}
                          onClick={() => setFilter(filter === c ? 'all' : c)}
                        />
                      ))}
                    </div>
                    {/* The chips narrow by rhythm; this narrows by wording. The
                        count is passed so the box always says how much of the
                        checklist it is hiding — a filter that quietly shortens a
                        37-item list is how someone concludes a task was never
                        raised. */}
                    <SearchInput
                      value={search}
                      onChange={setSearch}
                      placeholder="Find a checklist item…"
                      count={visible.length}
                      total={tasks.length}
                      className="md:ml-auto md:w-72"
                    />
                  </Toolbar>
                  {visible.length === 0 && (
                    <Card>
                      {/* Two different empty states, because they need two
                          different actions. Before the search box existed this
                          branch could only be reached with a cadence chip
                          active, so it indexed CADENCE_LABEL unconditionally —
                          a search that matches nothing while the chip says
                          "All" now reaches it too, and that lookup would render
                          the literal words "No undefined tasks". */}
                      {search.trim() ? (
                        <EmptyState
                          title="Nothing matches that search"
                          body={`No checklist item mentions “${search.trim()}”.`}
                          hint="Search covers each task's title and description — not the blocked-reason note on it."
                          action={
                            <Button size="sm" variant="secondary" onClick={() => setSearch('')}>
                              Clear search
                            </Button>
                          }
                        />
                      ) : (
                        <EmptyState
                          title={`No ${CADENCE_LABEL[filter as TaskCadence].toLowerCase()} tasks`}
                          body="This program has no checklist items on that rhythm."
                          hint="The rhythm comes from the stage template the task was generated from."
                        />
                      )}
                    </Card>
                  )}
                </>
              )}

              {tasks.length > 0 &&
                STAGES.map((stage) => {
                  const items = byStage.get(stage) ?? []
                  if (items.length === 0) return null
                  const stageDone = items.filter((t) => t.status === 'done').length
                  return (
                    <Card key={stage}>
                      <div className="flex items-center justify-between gap-3 px-4 py-3 border-b border-line">
                        <div className="flex items-center gap-2 min-w-0">
                          <h3 className="text-sm font-semibold text-ink truncate">
                            {STAGE_LABEL[stage]}
                          </h3>
                          {program.stage === stage && <Badge tone="accent">Current</Badge>}
                        </div>
                        <span className="text-xs tabular-nums text-ink-3 shrink-0">
                          {stageDone}/{items.length}
                        </span>
                      </div>

                      <ul className="divide-y divide-line-soft">
                        {items.map((task) => {
                          const open = openTasks.has(task.id)
                          const overdue =
                            task.status !== 'done' && URGENT_BANDS.includes(deriveUrgency(task))
                          return (
                          <li key={task.id} className="hover:bg-surface-2/60 transition">
                            {/* THE COLLAPSED ROW. One line, and it stays one
                                line: the title truncates rather than wrapping,
                                because thirty-seven rows that each grow to fit
                                their longest word is what made this list
                                unreadable. The full title is in the expanded
                                panel and in the `title` attribute.

                                The checkbox is a SIBLING of the disclosure
                                button, not a child. Nesting a control inside a
                                button is invalid, and it would also mean every
                                attempt to tick something off toggled the panel
                                open — the single most common action on this
                                screen made annoying to serve the second most. */}
                            <div className="flex items-center gap-3 px-4 py-2">
                              <input
                                type="checkbox"
                                className="h-4 w-4 shrink-0 accent-[var(--color-accent)] cursor-pointer"
                                checked={task.status === 'done'}
                                onChange={(e) =>
                                  patchTask.mutate({
                                    taskId: task.id,
                                    patch: { status: e.target.checked ? 'done' : 'pending' },
                                  })
                                }
                                aria-label={`Mark ${task.title} done`}
                              />

                              <button
                                type="button"
                                onClick={() => toggleTask(task.id)}
                                aria-expanded={open}
                                aria-controls={`task-panel-${task.id}`}
                                title={task.title}
                                className="min-w-0 flex-1 flex items-center gap-3 text-left py-0.5"
                              >
                                <span
                                  className={`text-sm truncate ${
                                    task.status === 'done'
                                      ? 'text-ink-3 line-through'
                                      : 'text-ink'
                                  }`}
                                >
                                  {task.title}
                                </span>

                                {/* The three facts worth seeing without opening
                                    anything: when it is due, who has it, what
                                    state it is in. Everything else is a click
                                    away. */}
                                <span className="ml-auto flex items-center gap-2 shrink-0">
                                  {task.status !== 'done' && task.due_date && (
                                    <span
                                      className={`hidden sm:inline text-[11px] tabular-nums ${
                                        overdue ? 'font-medium text-bad-ink' : 'text-ink-3'
                                      }`}
                                    >
                                      {fmtDate(task.due_date)}
                                    </span>
                                  )}
                                  {task.owner_id && (
                                    <span className="hidden md:inline text-[11px] text-ink-3 truncate max-w-28">
                                      {ownerNames.get(task.owner_id) ?? 'Assigned'}
                                    </span>
                                  )}
                                  <TaskStatusPill status={task.status} />
                                  <svg
                                    width="14"
                                    height="14"
                                    viewBox="0 0 24 24"
                                    fill="none"
                                    aria-hidden
                                    className={`text-ink-3 transition-transform ${
                                      open ? 'rotate-90' : ''
                                    }`}
                                  >
                                    <path
                                      d="M9 5l7 7-7 7"
                                      stroke="currentColor"
                                      strokeWidth="2"
                                      strokeLinecap="round"
                                      strokeLinejoin="round"
                                    />
                                  </svg>
                                </span>
                              </button>
                            </div>

                            {/* THE EXPANDED PANEL. Indented to the title's left
                                edge so it reads as belonging to the row above
                                rather than as a new row. */}
                            {open && (
                            <div
                              id={`task-panel-${task.id}`}
                              className="px-4 pb-3 pl-11 animate-in"
                            >
                              {task.description && (
                                <p className="text-xs text-ink-3 mb-2 leading-snug max-w-prose">
                                  {task.description}
                                </p>
                              )}
                              <div className="flex flex-wrap items-center gap-1.5 mt-1.5">
                                {task.owner_role && <Badge>{ROLE_LABEL[task.owner_role]}</Badge>}
                                <CadencePill cadence={task.cadence} />
                                {task.status !== 'done' && task.due_date && (
                                  <span
                                    className={`text-[11px] ${
                                      URGENT_BANDS.includes(deriveUrgency(task))
                                        ? 'font-medium text-bad-ink'
                                        : 'text-ink-3'
                                    }`}
                                  >
                                    {dueLabel(task)} · {fmtDate(task.due_date)}
                                  </span>
                                )}
                                {task.external_url && (
                                  <a
                                    href={task.external_url}
                                    target="_blank"
                                    rel="noreferrer"
                                    className="text-[11px] text-accent hover:underline"
                                  >
                                    open in {task.source_system ?? 'source'} ↗
                                  </a>
                                )}
                              </div>

                              {/* A blocked task with no counterparty is just a
                                  stalled task. Capturing who it waits on is what
                                  turns the status into an escalation list. */}
                              {task.status === 'blocked' && (
                                <div className="flex flex-wrap gap-2 mt-2">
                                  <Input
                                    className="!h-7 !w-36 !text-xs"
                                    placeholder="Waiting on… (HR, L&S, ERM)"
                                    defaultValue={task.waiting_on ?? ''}
                                    onBlur={(e) => {
                                      const v = e.target.value.trim() || null
                                      if (v !== task.waiting_on)
                                        patchTask.mutate({
                                          taskId: task.id,
                                          patch: { waiting_on: v },
                                        })
                                    }}
                                    aria-label="Waiting on"
                                  />
                                  <Input
                                    className="!h-7 !w-64 !text-xs"
                                    placeholder="Why is it blocked?"
                                    defaultValue={task.blocked_reason ?? ''}
                                    onBlur={(e) => {
                                      const v = e.target.value.trim() || null
                                      if (v !== task.blocked_reason)
                                        patchTask.mutate({
                                          taskId: task.id,
                                          patch: { blocked_reason: v },
                                        })
                                    }}
                                    aria-label="Blocked reason"
                                  />
                                </div>
                              )}

                              {/* The four editing controls. They wrap now
                                  instead of competing with the title for one
                                  row's width, which is what squeezed titles
                                  into three lines. */}
                              <div className="flex flex-wrap items-center gap-1.5 mt-2.5">
                                {/* A due date is what makes urgency mean anything —
                                    without one every task sits in 'unscheduled'
                                    and Needs-attention stays empty forever. */}
                              <Input
                                type="date"
                                className="!h-7 !w-32 !text-xs !px-2"
                                value={task.due_date ?? ''}
                                onChange={(e) =>
                                  patchTask.mutate({
                                    taskId: task.id,
                                    patch: { due_date: e.target.value || null },
                                  })
                                }
                                aria-label="Due date"
                              />
                              <Select
                                className="!h-7 !w-24 !text-xs"
                                value={task.cadence}
                                onChange={(e) =>
                                  patchTask.mutate({
                                    taskId: task.id,
                                    patch: { cadence: e.target.value as TaskCadence },
                                  })
                                }
                                aria-label="Cadence"
                              >
                                {CADENCES.map((c) => (
                                  <option key={c} value={c}>
                                    {CADENCE_LABEL[c]}
                                  </option>
                                ))}
                              </Select>
                              <Select
                                className="!h-7 !w-28 !text-xs"
                                value={task.owner_id ?? ''}
                                onChange={(e) =>
                                  patchTask.mutate({
                                    taskId: task.id,
                                    patch: { owner_id: e.target.value || null },
                                  })
                                }
                                aria-label="Assign owner"
                              >
                                <option value="">Unassigned</option>
                                {(ownersQuery.data?.rows ?? []).map((p) => (
                                  <option key={p.id} value={p.id}>
                                    {p.full_name ?? p.id.slice(0, 8)}
                                  </option>
                                ))}
                              </Select>
                              <Select
                                className="!h-7 !w-24 !text-xs"
                                value={task.status}
                                onChange={(e) =>
                                  patchTask.mutate({
                                    taskId: task.id,
                                    patch: { status: e.target.value as TaskStatus },
                                  })
                                }
                                aria-label="Status"
                              >
                                <option value="pending">Pending</option>
                                <option value="in_progress">In progress</option>
                                <option value="done">Done</option>
                                <option value="blocked">Blocked</option>
                              </Select>
                              </div>
                            </div>
                            )}
                          </li>
                          )
                        })}
                      </ul>
                    </Card>
                  )
                })}

              {tasks.length > 0 && (
                <div className="flex items-center justify-end gap-3">
                  {generateNote && <span className="text-xs text-ink-3">{generateNote}</span>}
                  <Button
                    size="sm"
                    disabled={regenerate.isPending}
                    onClick={() => regenerate.mutate()}
                  >
                    Top up from templates
                  </Button>
                </div>
              )}
            </div>

            {/* --- Batches ---------------------------------------------------- */}
            <div className="space-y-4">
              <Card>
                <div className="flex items-center justify-between px-4 py-3 border-b border-line">
                  <h3 className="text-sm font-semibold text-ink">Batches</h3>
                  <Button size="sm" onClick={() => setAddingBatch(true)}>
                    Add
                  </Button>
                </div>
                {batches.length === 0 ? (
                  <EmptyState title="No batches" body="Add the branch and section split." />
                ) : (
                  <ul className="divide-y divide-line-soft">
                    {groupByPassoutYear(batches).map(([year, cohort]) => (
                      <li key={year ?? 'unset'}>
                        {/* The segregation. `CSE-A` is a different set of
                            students every year, so the year is the heading and
                            the batch name sits under it — not the reverse. */}
                        <div className="flex items-center justify-between px-4 py-1.5 bg-surface border-y border-line-soft">
                          <span className="text-xs font-semibold text-ink-2">
                            {year == null ? 'Passout year not set' : `${year} passout`}
                          </span>
                          <span className="text-xs text-ink-3">
                            {cohort.length} {cohort.length === 1 ? 'batch' : 'batches'}
                          </span>
                        </div>
                        <ul className="divide-y divide-line-soft">
                          {cohort.map((b) => (
                            <li key={b.id} className="px-4 py-3 flex items-start gap-3">
                              <div className="min-w-0 flex-1">
                                <p className="text-sm font-medium text-ink">{b.name}</p>
                                <p className="text-xs text-ink-3 mt-0.5">
                                  {[b.branch, b.section].filter(Boolean).join(' · ') ||
                                    'No branch set'}
                                  {b.expected_student_count != null &&
                                    ` · ${b.expected_student_count} students`}
                                </p>
                                {b.passout_year == null && (
                                  <p className="text-xs text-warn-ink mt-1">
                                    Created before passout year was tracked — Edit to set it,
                                    so this cohort can be told apart from next year&apos;s.
                                  </p>
                                )}
                              </div>
                              {/* The branch, section and headcount on a batch
                                  are the facts most likely to be wrong on day
                                  one and most likely to be noticed by whoever
                                  is standing in the room. Until now the only
                                  way to correct one was to ask someone with
                                  database access — the RLS policy has always
                                  allowed this edit, nothing ever offered it. */}
                              <Button
                                size="sm"
                                variant="ghost"
                                onClick={() => setEditingBatch(b)}
                                aria-label={`Edit ${b.name}`}
                              >
                                Edit
                              </Button>
                            </li>
                          ))}
                        </ul>
                      </li>
                    ))}
                  </ul>
                )}
              </Card>

              <Card className="p-4">
                <h3 className="text-sm font-semibold text-ink mb-2.5">College</h3>
                <p className="text-sm text-ink">{program.colleges?.name ?? '—'}</p>
                <p className="text-xs text-ink-3 mt-0.5">{program.colleges?.city ?? ''}</p>
                <div className="flex flex-wrap gap-1.5 mt-3">
                  <Badge>MoU: {program.colleges?.mou_status ?? '—'}</Badge>
                  <Badge>PO: {program.colleges?.po_status ?? '—'}</Badge>
                </div>
              </Card>
            </div>
          </div>
        )}
      </Page>

      {/* `key` resets the form between batches — see BatchModal's header. */}
      <BatchModal
        key={editingBatch?.id ?? 'new'}
        open={addingBatch || editingBatch !== null}
        programId={program.id}
        batch={editingBatch}
        onClose={() => {
          setAddingBatch(false)
          setEditingBatch(null)
        }}
      />
    </>
  )
}

/**
 * Group batches by passout year, newest cohort first.
 *
 * Rows with no year sort LAST rather than first. They are the pre-1500 backlog
 * (see that migration): putting them at the top would make the one thing needing
 * correction look like the headline cohort, and putting them in with a real year
 * would be a guess. They sit at the bottom, labelled, until someone sets them.
 */
function groupByPassoutYear(batches: Batch[]): [number | null, Batch[]][] {
  const byYear = new Map<number | null, Batch[]>()
  for (const b of batches) {
    const key = b.passout_year ?? null
    const bucket = byYear.get(key)
    if (bucket) bucket.push(b)
    else byYear.set(key, [b])
  }
  return [...byYear.entries()].sort(([a], [b]) => {
    if (a === null) return 1
    if (b === null) return -1
    return b - a
  })
}

/**
 * Add a batch, or correct one that already exists.
 *
 * ONE COMPONENT FOR BOTH, because the fields, the validation and the copy are
 * identical — a separate edit modal would be the same form twice, and the
 * second copy is the one that drifts.
 *
 * THE CALLER PASSES A `key`. Every field below seeds its `useState` from
 * `batch`, and a seeded initialiser only runs on mount; without a key that
 * changes with the batch, opening a second batch would show the first one's
 * values, and saving would write them onto the wrong cohort. Keying the element
 * remounts it, which is React's own answer to "reset this form" and cheaper to
 * read than a useEffect that copies props into state.
 *
 * WHO CAN SAVE IS NOT DECIDED HERE. `batches_internal_all` (migration 2300) is
 * `for all` to any internal persona whose reach covers the program, so an LDE
 * Executive may correct a batch at their own college — which is the point: they
 * are the ones on campus who know the section moved or the headcount changed.
 * This form does not check that, and must not; Postgres does, per R5. What the
 * form owes the user is the REASON when the database says no, which is what the
 * error note at the bottom is for.
 */
function BatchModal({
  open,
  programId,
  batch,
  onClose,
}: {
  open: boolean
  programId: string
  /** The batch being corrected, or null to create a new one. */
  batch: Batch | null
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const editing = batch !== null
  const [name, setName] = useState(batch?.name ?? '')
  const [branch, setBranch] = useState(batch?.branch ?? '')
  const [section, setSection] = useState(batch?.section ?? '')
  const [passoutYear, setPassoutYear] = useState(
    batch?.passout_year != null ? String(batch.passout_year) : '',
  )
  const [count, setCount] = useState(
    batch?.expected_student_count != null ? String(batch.expected_student_count) : '',
  )

  const save = useMutation({
    mutationFn: () => {
      const values = {
        name,
        branch: branch || null,
        section: section || null,
        // Required by the form, so never null on a row this screen writes. The
        // column stays nullable in the database only for rows that predate
        // migration 1500 — correcting one of those is exactly what the warning
        // on the batch row is asking for.
        passout_year: Number(passoutYear),
        expected_student_count: count ? Number(count) : null,
      }
      // `program_id` is deliberately absent from the update. Moving a batch
      // between programs would move its deployments, attendance and payouts
      // with it, and that is not a correction — it is a migration.
      return batch
        ? unwrap(supabase.from('batches').update(values).eq('id', batch.id))
        : unwrap(supabase.from('batches').insert({ program_id: programId, ...values }))
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: qk.batches.all })
      onClose()
    },
  })

  function onSubmit(e: FormEvent) {
    e.preventDefault()
    save.mutate()
  }

  return (
    <Modal open={open} onClose={onClose} title={editing ? `Edit ${batch.name}` : 'Add batch'}>
      <form onSubmit={onSubmit} className="space-y-4">
        <Field label="Batch name">
          <Input
            required
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="CSE-A"
          />
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Branch">
            <Input value={branch} onChange={(e) => setBranch(e.target.value)} placeholder="CSE" />
          </Field>
          <Field label="Section">
            <Input value={section} onChange={(e) => setSection(e.target.value)} placeholder="A" />
          </Field>
        </div>
        <Field
          label="Passout year"
          hint="The year this cohort graduates. Batches are grouped by it — CSE-A is a different set of students each year, so the name alone does not identify a cohort."
        >
          <Input
            type="number"
            required
            min={2000}
            max={2100}
            value={passoutYear}
            onChange={(e) => setPassoutYear(e.target.value)}
            placeholder="2027"
          />
        </Field>
        <Field label="Expected students">
          <Input
            type="number"
            min={0}
            value={count}
            onChange={(e) => setCount(e.target.value)}
            placeholder="60"
          />
        </Field>
        {save.error && <ErrorNote>{errorMessage(save.error)}</ErrorNote>}
        <div className="flex justify-end gap-2">
          <Button type="button" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" disabled={save.isPending}>
            {editing ? 'Save changes' : 'Add batch'}
          </Button>
        </div>
      </form>
    </Modal>
  )
}
