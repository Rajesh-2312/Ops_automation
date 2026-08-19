/**
 * One place for every TanStack Query key.
 *
 * Keys are centralised rather than written inline because invalidation is a
 * cross-screen concern: completing a task on the work queue has to invalidate
 * the board's roll-up, and assigning a college in Users has to invalidate every
 * scope-dependent list. Inline string arrays make those relationships
 * unfindable; this file makes them greppable.
 *
 * Convention: broadest first, so `qk.tasks.all` invalidates every task query.
 *
 * -----------------------------------------------------------------------------
 * EVERY BOUNDED LIST TAKES ITS BOUND AS A REQUIRED ARGUMENT.
 * -----------------------------------------------------------------------------
 *
 * A query that sends `limit` to the server and leaves it out of its cache key
 * is not a stale-cache bug, it is a WRONG-DATA bug: two queries differing only
 * in their bound collide on one entry, and whichever ran first serves the other
 * its rows — so a request for 200 can be answered with 1,000, or a request for
 * 1,000 with 200, and nothing on screen says which happened. `lib/erm.ts`
 * shipped exactly that and the comment on `ermKeys.queue` records it.
 *
 * The defence is the type system rather than a convention: `limit` here is a
 * required parameter with no default, so a call site that forgets the bound
 * does not compile. Adding a default would restore the bug the moment somebody
 * bounded a query and reused an existing key.
 *
 * `all` stays a bare prefix, so `invalidateQueries({ queryKey: qk.tasks.all })`
 * still reaches every page size of every task query — which is what makes the
 * cross-screen invalidations above keep working unchanged.
 */
export const qk = {
  profiles: {
    all: ['profiles'] as const,
    list: (limit: number) => ['profiles', 'list', limit] as const,
    one: (id: string) => ['profiles', 'one', id] as const,
  },
  /**
   * Clusters take NO bound, and that is a decision rather than an omission.
   * A cluster is org geography — a handful of rows, created by an admin, one
   * per region — and it is the classic case where a limit is pure ceremony.
   * If clusters ever become per-college this needs revisiting.
   */
  clusters: {
    all: ['clusters'] as const,
    list: () => ['clusters', 'list'] as const,
  },
  colleges: {
    all: ['colleges'] as const,
    list: (limit: number) => ['colleges', 'list', limit] as const,
  },
  collegeAssignments: {
    all: ['college_assignments'] as const,
    list: (limit: number) => ['college_assignments', 'list', limit] as const,
  },
  clusterAssignments: {
    all: ['cluster_assignments'] as const,
    list: (limit: number) => ['cluster_assignments', 'list', limit] as const,
  },
  programs: {
    all: ['programs'] as const,
    list: (limit: number) => ['programs', 'list', limit] as const,
    one: (id: string) => ['programs', 'one', id] as const,
  },
  batches: {
    all: ['batches'] as const,
    byProgram: (programId: string, limit: number) =>
      ['batches', 'byProgram', programId, limit] as const,
    /** Every batch the caller reaches, with its program and college — the
     *  deployment picker needs all of them, not one program's. */
    list: (limit: number) => ['batches', 'list', limit] as const,
  },
  tasks: {
    all: ['tasks'] as const,
    queue: (limit: number) => ['tasks', 'queue', limit] as const,
    rollup: (limit: number) => ['tasks', 'rollup', limit] as const,
    byProgram: (programId: string, limit: number) =>
      ['tasks', 'byProgram', programId, limit] as const,
    mine: (limit: number) => ['tasks', 'mine', limit] as const,
  },
  documents: {
    all: ['program_documents'] as const,
    byProgram: (programId: string, limit: number) =>
      ['program_documents', 'byProgram', programId, limit] as const,
  },
  trainers: {
    all: ['trainers'] as const,
    list: (limit: number) => ['trainers', 'list', limit] as const,
  },
  deployments: {
    all: ['deployments'] as const,
    list: (limit: number) => ['deployments', 'list', limit] as const,
    byTrainer: (trainerId: string, limit: number) =>
      ['deployments', 'byTrainer', trainerId, limit] as const,
  },
  /**
   * Trainer payment rails (1400). Behind can_see_commercials(), so the list
   * query is only issued for a persona that can hold rows — but it is keyed
   * separately from `trainers` on purpose: a rails edit must not invalidate the
   * roster, and the roster is readable by a persona the rails are not.
   */
  bankRails: {
    all: ['trainer_bank_accounts'] as const,
    list: (limit: number) => ['trainer_bank_accounts', 'list', limit] as const,
  },
  workOrders: {
    all: ['work_orders'] as const,
    list: (limit: number) => ['work_orders', 'list', limit] as const,
    byTrainer: (trainerId: string, limit: number) =>
      ['work_orders', 'byTrainer', trainerId, limit] as const,
  },
  /**
   * One deployment, one month. NOT bounded by a row limit and it does not need
   * to be: the query is `mark_date` between the first and last of a named
   * month for a single deployment, so the schema's `unique (deployment_id,
   * mark_date)` caps it at 31 rows. A page size here would be theatre.
   */
  trainerAttendance: {
    all: ['trainer_attendance'] as const,
    byDeploymentMonth: (deploymentId: string, month: string) =>
      ['trainer_attendance', deploymentId, month] as const,
  },
  /**
   * FastAPI, not PostgREST. Keyed on deployment + period + the claim amounts,
   * because a payout for a different TA&DA is a different answer and must not
   * be served from the cache of the previous one. The §7 verdict additionally
   * depends on the stated reasons, so `validate` carries them too.
   */
  payouts: {
    all: ['payouts'] as const,
    preview: (fingerprint: string) => ['payouts', 'preview', fingerprint] as const,
    validate: (fingerprint: string) => ['payouts', 'validate', fingerprint] as const,
  },
  /**
   * Home-screen roll-ups. Only the queries no working screen already makes get
   * a key here — the home reuses `tasks.queue()`, `programs.list()` and
   * `deployments.list()` verbatim so opening it warms the caches the rest of
   * the console reads, rather than fetching the same rows under a second name.
   *
   * That reuse is why the home passes the DEFAULT page size to those three: a
   * different bound would be a different cache entry and the warming would stop
   * working. Raising the bound on the work queue therefore opens a second
   * entry, which is correct — the two are genuinely different result sets.
   */
  home: {
    all: ['home'] as const,
    batches: (limit: number) => ['home', 'batches', limit] as const,
    studentCount: (limit: number) => ['home', 'student_count', limit] as const,
    attendanceMonth: (month: string, limit: number) =>
      ['home', 'attendance_month', month, limit] as const,
    pendingApprovals: (limit: number) => ['home', 'pending_approvals', limit] as const,
    pnl: (limit: number) => ['home', 'pnl', limit] as const,
  },
  college: {
    progress: (limit: number) => ['college', 'progress', limit] as const,
    attendance: (limit: number) => ['college', 'attendance', limit] as const,
    reports: (limit: number) => ['college', 'reports', limit] as const,
  },
} as const
