import { useMemo, useState } from 'react'
import { PAGE, bounded, emptyBound, usePageLimit } from '../lib/bounds'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { supabase, errorMessage, unwrap } from '../lib/supabase'
import { qk } from '../lib/queryKeys'
import {
  SELECTABLE_ROLES,
  ROLE_LABEL,
  ROLE_SCOPE_BLURB,
  isInternalRole,
  type AppRole,
  type Cluster,
  type College,
  type Profile,
  type Trainer,
  type UserClusterAssignment,
  type UserCollegeAssignment,
} from '../lib/types'
import { useAuth } from '../auth/AuthProvider'
import { Page, PageHeader } from '../components/AppShell'
import {
  Badge,
  BoundNote,
  Button,
  Card,
  EmptyState,
  ErrorNote,
  HelpTip,
  InfoNote,
  Legend,
  MonoValue,
  PageIntro,
  RemovableChip,
  SearchInput,
  Select,
  TableSkeleton,
  Td,
  Th,
  Toolbar,
} from '../components/ui'

/**
 * Users, personas and REACH.
 *
 * THE MODEL CHANGED, AND THIS SCREEN IS WHERE THE CHANGE IS VISIBLE.
 * The three-persona app set a single `profiles.college_id` and called it scope.
 * That column still exists but now serves the COLLEGE PERSONA ONLY — a college's
 * own login. Internal staff take no scope from it at all; the
 * `profiles_role_link_ck` constraint forbids them from even holding a value
 * there, because an LDE Executive pinned to a college that way would be
 * invisible to `my_college_ids()` and would silently get zero rows, which reads
 * as "RLS is broken" rather than "the assignment is missing".
 *
 * Reach now comes from two join tables (CLAUDE.md §4):
 *
 *   user_college_assignments — Manager and LDE Executive to colleges. A Manager
 *                              may hold many; an LDE Executive typically one.
 *   user_cluster_assignments — Senior Manager to clusters. `colleges.cluster_id`
 *                              expands a cluster to every college in it.
 *
 * `my_college_ids()` is the UNION of the two, so this screen is the thing that
 * makes every other screen non-empty. A new signup lands as a trainer with a
 * NULL trainer_id and matches no policy anywhere; it is an assignment made here
 * that opens the door.
 *
 * WHAT THE DATABASE WILL REFUSE, WHATEVER THIS UI SENDS
 * ----------------------------------------------------
 * The `profiles_guard_privileged_columns` BEFORE UPDATE trigger permits ONLY an
 * admin to change `role`, `is_admin`, `trainer_id` or `college_id`. It is a
 * trigger and not an RLS policy because a WITH CHECK cannot see the OLD row, so
 * "you may edit your own profile but not your own role" is inexpressible as a
 * policy. For a non-admin it raises 42501 — so every one of those controls
 * below will 403 for a non-admin, by design. The same is true of the assignment
 * tables: `user_college_assignments_admin_all` and its cluster twin are
 * admin-write, internal-read. The controls are disabled to match, and the
 * disabling is a courtesy — the trigger and the policies are the actual rule.
 *
 * `is_admin` is additionally constrained to Senior Managers
 * (`profiles_admin_ck`): admin is the right to GRANT reach, and giving it to a
 * scope-limited persona would let that persona widen its own scope.
 *
 * Nothing here is a permission check. It is a form over the same tables the
 * policies read.
 */
export function UsersPage() {
  const { profile: me, isAdmin, refreshProfile } = useAuth()
  const queryClient = useQueryClient()

  // BOUNDED. `profiles` is one row per byteXL login and grows with headcount —
  // slower than the operational tables, but this screen ALSO fetches both
  // assignment tables whole to build the reach column, and those grow as
  // colleges × staff.
  const page = usePageLimit(PAGE.profiles)

  const profilesQuery = useQuery({
    queryKey: qk.profiles.list(page.limit),
    queryFn: () =>
      bounded<Profile>(page.limit, (rows) =>
        supabase.from('profiles').select('*').order('role').order('full_name').limit(rows),
      ),
  })

  const collegesQuery = useQuery({
    queryKey: qk.colleges.list(PAGE.colleges),
    queryFn: () =>
      bounded<College>(PAGE.colleges, (rows) =>
        supabase.from('colleges').select('*').order('name').limit(rows),
      ),
  })

  // Unbounded, as on Colleges: cluster is org geography and a truncated lookup
  // would render a real cluster as "Unknown cluster".
  const clustersQuery = useQuery({
    queryKey: qk.clusters.list(),
    queryFn: () => unwrap<Cluster[]>(supabase.from('clusters').select('*').order('name')),
  })

  // Trainers are needed only to link a trainer persona to its record. An LDE
  // Executive sees a narrowed roster here; that is the trainers policy doing its
  // job, and it does not affect anything else on this page.
  const trainersQuery = useQuery({
    queryKey: qk.trainers.list(PAGE.trainers),
    queryFn: () =>
      bounded<Trainer>(PAGE.trainers, (rows) =>
        supabase.from('trainers').select('*').order('full_name').limit(rows),
      ),
  })

  // The reach column is built from these two whole tables, so they are the
  // reads on this screen most likely to bite: `user_college_assignments` is one
  // row per (staff member × college) and grows as the product of both.
  //
  // `.order('user_id')` is required, not cosmetic. Grouping by user happens
  // client-side, so an unordered limit would take an arbitrary slice and one
  // user's assignments could land inside the bound while another's fell
  // outside — making a correctly-provisioned Manager render as "No reach", the
  // exact string an admin acts on.
  const collegeAssignmentsQuery = useQuery({
    queryKey: qk.collegeAssignments.list(PAGE.assignments),
    queryFn: () =>
      bounded<UserCollegeAssignment>(PAGE.assignments, (rows) =>
        supabase.from('user_college_assignments').select('*').order('user_id').limit(rows),
      ),
  })

  const clusterAssignmentsQuery = useQuery({
    queryKey: qk.clusterAssignments.list(PAGE.assignments),
    queryFn: () =>
      bounded<UserClusterAssignment>(PAGE.assignments, (rows) =>
        supabase.from('user_cluster_assignments').select('*').order('user_id').limit(rows),
      ),
  })

  /** True only when both assignment reads saw every row. */
  const reachComplete =
    collegeAssignmentsQuery.data?.truncated === false &&
    clusterAssignmentsQuery.data?.truncated === false

  const profilesBound = profilesQuery.data ?? emptyBound<Profile>(page.limit)
  const profiles = profilesBound.rows

  // Display-only. "Who covers Malineni?" is the question this page exists to
  // answer and it is asked by name far more often than by scrolling.
  const [search, setSearch] = useState('')
  const q = search.trim().toLowerCase()
  const visibleProfiles = useMemo(
    () =>
      q === ''
        ? profiles
        : profiles.filter(
            (p) =>
              (p.full_name ?? '').toLowerCase().includes(q) ||
              ROLE_LABEL[p.role].toLowerCase().includes(q),
          ),
    [profiles, q],
  )
  const colleges = useMemo(() => collegesQuery.data?.rows ?? [], [collegesQuery.data])
  const clusters = useMemo(() => clustersQuery.data ?? [], [clustersQuery.data])
  const trainers = trainersQuery.data?.rows ?? []

  const collegeName = (id: string) => colleges.find((c) => c.id === id)?.name ?? 'Unknown college'
  const clusterName = (id: string) => clusters.find((c) => c.id === id)?.name ?? 'Unknown cluster'

  const collegeAssignmentsByUser = useMemo(() => {
    const map = new Map<string, UserCollegeAssignment[]>()
    for (const a of collegeAssignmentsQuery.data?.rows ?? []) {
      if (!map.has(a.user_id)) map.set(a.user_id, [])
      map.get(a.user_id)!.push(a)
    }
    return map
  }, [collegeAssignmentsQuery.data])

  const clusterAssignmentsByUser = useMemo(() => {
    const map = new Map<string, UserClusterAssignment[]>()
    for (const a of clusterAssignmentsQuery.data?.rows ?? []) {
      if (!map.has(a.user_id)) map.set(a.user_id, [])
      map.get(a.user_id)!.push(a)
    }
    return map
  }, [clusterAssignmentsQuery.data])

  /**
   * Reach changes are cross-cutting: after one, a different set of colleges,
   * programs and tasks is legible to somebody. Invalidating the assignment
   * queries alone would leave this page correct and every other page stale, so
   * the scope-dependent namespaces go with it. Only the caller's OWN change
   * alters what this browser can read, but invalidating either way is cheap and
   * beats reasoning about whose row was touched.
   */
  function invalidateScope() {
    void queryClient.invalidateQueries({ queryKey: qk.profiles.all })
    void queryClient.invalidateQueries({ queryKey: qk.collegeAssignments.all })
    void queryClient.invalidateQueries({ queryKey: qk.clusterAssignments.all })
    void queryClient.invalidateQueries({ queryKey: qk.colleges.all })
    void queryClient.invalidateQueries({ queryKey: qk.programs.all })
    void queryClient.invalidateQueries({ queryKey: qk.tasks.all })
  }

  const patchProfile = useMutation({
    mutationFn: async ({ id, patch }: { id: string; patch: Partial<Profile> }) => {
      const body: Partial<Profile> = { ...patch }

      // profiles_role_link_ck: each persona may hold at most the ONE identity
      // link that matches it, and the internal personas may hold neither.
      // Changing role without clearing the stale link makes the row fail the
      // constraint, so the clearing happens here rather than surfacing as a
      // check-violation the user cannot act on.
      if (body.role) {
        if (isInternalRole(body.role)) {
          body.trainer_id = null
          body.college_id = null
        } else if (body.role === 'trainer') {
          body.college_id = null
        } else if (body.role === 'college') {
          body.trainer_id = null
        }
        // profiles_admin_ck: only a senior_manager may carry the admin bit.
        if (body.role !== 'senior_manager') body.is_admin = false
      }

      const row = await unwrap<Profile>(
        supabase.from('profiles').update(body).eq('id', id).select().maybeSingle(),
      )
      if (id === me?.id) await refreshProfile()
      return row
    },
    onSuccess: invalidateScope,
  })

  const addCollege = useMutation({
    mutationFn: ({ userId, collegeId }: { userId: string; collegeId: string }) =>
      unwrap(
        supabase.from('user_college_assignments').insert({
          user_id: userId,
          college_id: collegeId,
          assigned_by: me?.id ?? null,
        }),
      ),
    onSuccess: invalidateScope,
  })

  const addCluster = useMutation({
    mutationFn: ({ userId, clusterId }: { userId: string; clusterId: string }) =>
      unwrap(
        supabase.from('user_cluster_assignments').insert({
          user_id: userId,
          cluster_id: clusterId,
          assigned_by: me?.id ?? null,
        }),
      ),
    onSuccess: invalidateScope,
  })

  const removeCollege = useMutation({
    mutationFn: (id: string) =>
      unwrap(supabase.from('user_college_assignments').delete().eq('id', id)),
    onSuccess: invalidateScope,
  })

  const removeCluster = useMutation({
    mutationFn: (id: string) =>
      unwrap(supabase.from('user_cluster_assignments').delete().eq('id', id)),
    onSuccess: invalidateScope,
  })

  const loading =
    profilesQuery.isPending ||
    collegesQuery.isPending ||
    clustersQuery.isPending ||
    collegeAssignmentsQuery.isPending ||
    clusterAssignmentsQuery.isPending

  const failure =
    profilesQuery.error ??
    collegeAssignmentsQuery.error ??
    clusterAssignmentsQuery.error ??
    patchProfile.error ??
    addCollege.error ??
    addCluster.error ??
    removeCollege.error ??
    removeCluster.error

  return (
    <>
      <PageHeader
        title="Users & roles"
        purpose="Who can sign in, what job they hold, and which colleges each of them can actually see. Those last two are separate settings, and both have to be right before anyone sees anything."
        subtitle={
          loading
            ? 'Loading…'
            : `${profiles.length}${profilesBound.truncated ? '+' : ''} account${
                profiles.length === 1 ? '' : 's'
              }`
        }
      />

      <Page>
        {/* THE ONE IDEA THIS PAGE HAS TO LAND. Persona and reach are two
            independent settings, and almost every "RLS is broken" report is
            really a persona that was set without an assignment. Saying it as
            prose at the top costs a paragraph and saves that conversation. */}
        <PageIntro className="mb-4">
          <p>
            Two different settings decide what a person sees here, and getting one right
            without the other is why a screen comes up empty.
          </p>
          <p className="mt-2">
            <strong>Persona</strong> is what someone <em>is</em> — Senior Manager, Manager,{' '}
            <HelpTip term="LDE Executive">
              Learning &amp; Development Executive: the person on campus. They run attendance,
              batches and the day-to-day at their own college. They never see rates, payouts
              or invoices — the database refuses them those rows outright.
            </HelpTip>
            , College. It decides what <em>kind</em> of thing they may do.
          </p>
          <p className="mt-2">
            <strong>Assignments</strong> are what they <em>reach</em> — which colleges, or
            which{' '}
            <HelpTip term="cluster">
              A group of colleges treated as one region. Assigning a Senior Manager to a
              cluster gives them every college inside it, so a college with no cluster set is
              reached by no Senior Manager at all.
            </HelpTip>
            . A Manager with the right persona and no assignments reaches nothing, and every
            screen in the console is empty for them.
          </p>
        </PageIntro>

        <Card className="mb-4 p-4">
          <p className="text-sm font-medium text-ink mb-1">What each persona can reach</p>
          <p className="text-xs text-ink-3 mb-3">
            Green means that persona can see commercials — rates, payouts, P&amp;L and
            invoices. Red means the database returns them no such row, whatever the screen
            shows.
          </p>
          <Legend
            items={[
              {
                swatch: 'bg-good-wash border-good/30',
                label: 'Senior Manager',
                hint: 'every program in their assigned clusters · P&L, escalations, payout approval',
              },
              {
                swatch: 'bg-good-wash border-good/30',
                label: 'Manager',
                hint: 'their assigned colleges · full program state, trainer costs, reports',
              },
              {
                swatch: 'bg-bad-wash border-bad/30',
                label: 'LDE Executive',
                hint: 'their assigned colleges only · attendance, batches, daily tasks — no commercials',
              },
              {
                swatch: 'bg-bad-wash border-bad/30',
                label: 'College',
                hint: 'published artefacts for their own college, read-only',
              },
            ]}
          />
        </Card>

        {failure && (
          <div className="mb-4">
            <ErrorNote>{errorMessage(failure)}</ErrorNote>
          </div>
        )}

        {!isAdmin && (
          <div className="mb-4 max-w-4xl">
            <InfoNote>
              Read-only for you. Changing a persona, the admin flag, or an identity link is
              blocked by a database trigger for anyone who is not an admin, and the assignment
              tables are admin-write — so these controls are disabled rather than left to fail
              with a permissions error. Every internal persona can READ this page, because
              “who covers this college?” is a question anyone on the team may ask.
            </InfoNote>
          </div>
        )}

        {loading ? (
          <Card className="overflow-hidden">
            <TableSkeleton rows={6} cols={5} />
          </Card>
        ) : profiles.length === 0 ? (
          <Card>
            <EmptyState
              title="No users visible"
              body="Every byteXL login gets a row here the first time it signs in."
              hint="If you expected accounts and see none, the profiles read was refused rather than empty — sign out and back in, and if it persists it is a database permissions problem, not a missing user."
            />
          </Card>
        ) : (
          <div className="space-y-3">
            <BoundNote
              bound={profilesBound}
              noun="users"
              onMore={page.more}
              step={page.step}
            />
            {/* The Reach column is the one thing on this page an admin acts on,
                and it is built by grouping two whole assignment tables in the
                browser. If either read was cut, an account whose rows fell
                outside the bound renders as "No reach" — which reads as a
                provisioning bug and invites an admin to grant a duplicate
                assignment. Say it here rather than let the column lie. */}
            <BoundNote
              bound={collegeAssignmentsQuery.data}
              noun="college assignments"
              derived="The Reach column below is incomplete: an account whose rows fell outside the bound shows as having none."
            />
            <BoundNote
              bound={clusterAssignmentsQuery.data}
              noun="cluster assignments"
              derived="The Reach column below is incomplete for the same reason."
            />

            <Toolbar>
              <SearchInput
                value={search}
                onChange={setSearch}
                placeholder="Search a name or a persona…"
                count={visibleProfiles.length}
                total={profiles.length}
              />
            </Toolbar>

            <Card className="overflow-hidden">
            <div className="overflow-x-auto scroll-slim">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-line">
                    <Th>User</Th>
                    <Th>
                      <HelpTip term="Persona">
                        What this person <em>is</em>. It decides the kind of thing they may
                        do — approve a payout, mark attendance, read a published report. It
                        does <em>not</em> decide which colleges they see; that is Reach.
                      </HelpTip>
                    </Th>
                    <Th>
                      <HelpTip term="Reach">
                        Which colleges this person can see, granted one at a time or a whole
                        cluster at once. Persona without reach means every screen in the
                        console is empty for them — this column is the fix for that.
                      </HelpTip>
                    </Th>
                    <Th>
                      <HelpTip term="Identity link">
                        Only for the College persona, which is a college's own login and
                        takes its scope from the college it is tied to. byteXL staff never
                        use this — their scope comes from Reach.
                      </HelpTip>
                    </Th>
                    <Th>
                      <HelpTip term="Sees">
                        The plain-English consequence of the persona and reach on this row:
                        what this account will actually be able to open.
                      </HelpTip>
                    </Th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-line-soft">
                  {visibleProfiles.map((p) => {
                    const isMe = p.id === me?.id
                    const internal = isInternalRole(p.role)
                    const myColleges = collegeAssignmentsByUser.get(p.id) ?? []
                    const myClusters = clusterAssignmentsByUser.get(p.id) ?? []
                    const hasReach = myColleges.length > 0 || myClusters.length > 0
                    const unlinked =
                      (p.role === 'trainer' && !p.trainer_id) ||
                      (p.role === 'college' && !p.college_id)

                    const unassignedColleges = colleges.filter(
                      (c) => !myColleges.some((a) => a.college_id === c.id),
                    )
                    const unassignedClusters = clusters.filter(
                      (c) => !myClusters.some((a) => a.cluster_id === c.id),
                    )

                    return (
                      <tr key={p.id} className="hover:bg-surface-2/60 transition align-top">
                        {/* --- Identity ------------------------------------- */}
                        <Td>
                          <div className="flex flex-wrap items-center gap-1.5">
                            <span className="font-medium text-ink">
                              {p.full_name || 'Unnamed'}
                            </span>
                            {isMe && <Badge tone="accent">You</Badge>}
                            {p.is_admin && <Badge>Admin</Badge>}
                          </div>
                          {/* The account id, truncated. An identifier, so it gets
                              the monospaced face — this is the value someone
                              pastes into a support thread. */}
                          <span className="block text-ink-3 mt-0.5">
                            <MonoValue title={`Account id ${p.id}`}>
                              {`${p.id.slice(0, 8)}…`}
                            </MonoValue>
                          </span>

                          {/* The admin bit is constrained to senior_manager by
                              profiles_admin_ck, so the control is only offered
                              where the database would accept it. */}
                          {p.role === 'senior_manager' && (
                            <label className="flex items-center gap-1.5 mt-2 text-xs text-ink-2">
                              <input
                                type="checkbox"
                                className="h-3.5 w-3.5 accent-[var(--color-accent)]"
                                checked={p.is_admin}
                                disabled={!isAdmin || patchProfile.isPending}
                                onChange={(e) =>
                                  patchProfile.mutate({
                                    id: p.id,
                                    patch: { is_admin: e.target.checked },
                                  })
                                }
                              />
                              Admin
                            </label>
                          )}
                        </Td>

                        {/* --- Persona -------------------------------------- */}
                        <Td>
                          <Select
                            className="!h-8 !w-36 !text-xs"
                            value={p.role}
                            disabled={!isAdmin || isMe || patchProfile.isPending}
                            title={
                              isMe
                                ? 'Change your own persona from another admin account — demoting yourself here would take the admin bit with it.'
                                : undefined
                            }
                            onChange={(e) =>
                              patchProfile.mutate({
                                id: p.id,
                                patch: { role: e.target.value as AppRole },
                              })
                            }
                            aria-label={`Persona for ${p.full_name ?? p.id}`}
                          >
                            {SELECTABLE_ROLES.map((r) => (
                              <option key={r} value={r}>
                                {ROLE_LABEL[r]}
                              </option>
                            ))}
                          </Select>
                        </Td>

                        {/* --- Reach ---------------------------------------- */}
                        <Td className="min-w-72">
                          {internal ? (
                            <div className="space-y-2">
                              {hasReach ? (
                                <div className="flex flex-wrap gap-1.5">
                                  {myClusters.map((a) => (
                                    <RemovableChip
                                      key={a.id}
                                      label={`◈ ${clusterName(a.cluster_id)}`}
                                      title="Cluster — expands to every college in it"
                                      disabled={!isAdmin || removeCluster.isPending}
                                      onRemove={() => removeCluster.mutate(a.id)}
                                    />
                                  ))}
                                  {myColleges.map((a) => (
                                    <RemovableChip
                                      key={a.id}
                                      label={collegeName(a.college_id)}
                                      title="Direct college assignment"
                                      disabled={!isAdmin || removeCollege.isPending}
                                      onRemove={() => removeCollege.mutate(a.id)}
                                    />
                                  ))}
                                </div>
                              ) : reachComplete ? (
                                <p className="text-xs text-warn-ink">
                                  No assignments — this user reaches nothing and every screen is
                                  empty for them.
                                </p>
                              ) : (
                                // The assignment read was cut, so "no rows for
                                // this user" and "this user's rows fell past the
                                // bound" are indistinguishable here. Saying the
                                // first would invite an admin to grant a
                                // duplicate assignment to somebody who has one.
                                <p className="text-xs text-ink-3">
                                  Reach not loaded — the assignment read was cut at its row bound,
                                  so this is not a statement that the user has none.
                                </p>
                              )}

                              {/* Senior Managers are scoped by CLUSTER; the other
                                  two by college. Both pickers stay available to a
                                  Senior Manager because holding a direct college
                                  alongside a cluster is normal during a handover,
                                  and my_college_ids() unions the two. */}
                              {p.role === 'senior_manager' && (
                                <Select
                                  className="!h-8 !w-52 !text-xs"
                                  value=""
                                  disabled={
                                    !isAdmin ||
                                    unassignedClusters.length === 0 ||
                                    addCluster.isPending
                                  }
                                  onChange={(e) =>
                                    e.target.value &&
                                    addCluster.mutate({ userId: p.id, clusterId: e.target.value })
                                  }
                                  aria-label="Assign a cluster"
                                >
                                  <option value="">
                                    {unassignedClusters.length === 0
                                      ? 'All clusters assigned'
                                      : 'Assign a cluster…'}
                                  </option>
                                  {unassignedClusters.map((c) => (
                                    <option key={c.id} value={c.id}>
                                      {c.name}
                                    </option>
                                  ))}
                                </Select>
                              )}

                              <Select
                                className="!h-8 !w-52 !text-xs"
                                value=""
                                disabled={
                                  !isAdmin ||
                                  unassignedColleges.length === 0 ||
                                  addCollege.isPending
                                }
                                onChange={(e) =>
                                  e.target.value &&
                                  addCollege.mutate({ userId: p.id, collegeId: e.target.value })
                                }
                                aria-label="Assign a college"
                              >
                                <option value="">
                                  {unassignedColleges.length === 0
                                    ? 'All colleges assigned'
                                    : p.role === 'senior_manager'
                                      ? 'Add a direct college…'
                                      : 'Assign a college…'}
                                </option>
                                {unassignedColleges.map((c) => (
                                  <option key={c.id} value={c.id}>
                                    {c.name}
                                  </option>
                                ))}
                              </Select>
                            </div>
                          ) : hasReach ? (
                            // Left over from a demotion. is_internal() is false so
                            // the rows grant nothing, but stale reach that nobody
                            // can see is worse than stale reach that is listed.
                            <div className="space-y-1.5">
                              <p className="text-xs text-warn-ink">
                                Assignments left over from an internal persona. They grant
                                nothing now — every policy checks persona and reach
                                independently — but they should be removed.
                              </p>
                              <div className="flex flex-wrap gap-1.5">
                                {myClusters.map((a) => (
                                  <RemovableChip
                                    key={a.id}
                                    label={`◈ ${clusterName(a.cluster_id)}`}
                                    disabled={!isAdmin}
                                    onRemove={() => removeCluster.mutate(a.id)}
                                  />
                                ))}
                                {myColleges.map((a) => (
                                  <RemovableChip
                                    key={a.id}
                                    label={collegeName(a.college_id)}
                                    disabled={!isAdmin}
                                    onRemove={() => removeCollege.mutate(a.id)}
                                  />
                                ))}
                              </div>
                            </div>
                          ) : (
                            <span className="text-xs text-ink-3">
                              Not applicable — scope comes from the identity link.
                            </span>
                          )}
                        </Td>

                        {/* --- Identity link -------------------------------- */}
                        <Td>
                          {p.role === 'college' ? (
                            <Select
                              className="!h-8 !w-48 !text-xs"
                              value={p.college_id ?? ''}
                              disabled={!isAdmin || patchProfile.isPending}
                              onChange={(e) =>
                                patchProfile.mutate({
                                  id: p.id,
                                  patch: { college_id: e.target.value || null },
                                })
                              }
                              aria-label="Linked college"
                            >
                              <option value="">Not linked</option>
                              {colleges.map((c) => (
                                <option key={c.id} value={c.id}>
                                  {c.name}
                                </option>
                              ))}
                            </Select>
                          ) : p.role === 'trainer' ? (
                            <Select
                              className="!h-8 !w-48 !text-xs"
                              value={p.trainer_id ?? ''}
                              disabled={!isAdmin || patchProfile.isPending}
                              onChange={(e) =>
                                patchProfile.mutate({
                                  id: p.id,
                                  patch: { trainer_id: e.target.value || null },
                                })
                              }
                              aria-label="Linked trainer"
                            >
                              <option value="">Not linked</option>
                              {trainers.map((t) => (
                                <option key={t.id} value={t.id}>
                                  {t.full_name} · {t.pan}
                                </option>
                              ))}
                            </Select>
                          ) : (
                            <span
                              className="text-xs text-ink-3"
                              title="profiles_role_link_ck forbids an identity link on an internal persona."
                            >
                              None — internal staff take no scope from a link.
                            </span>
                          )}
                        </Td>

                        {/* --- Consequence ---------------------------------- */}
                        <Td className="text-xs text-ink-2 max-w-64">
                          {unlinked || (internal && !hasReach) ? (
                            <span className="text-warn-ink">
                              Nothing yet — this account signs in successfully and then finds
                              every screen empty.
                            </span>
                          ) : (
                            ROLE_SCOPE_BLURB[p.role]
                          )}
                        </Td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
            {visibleProfiles.length === 0 && (
              <EmptyState
                title="No user matches that search"
                body={`None of the ${profiles.length} accounts match “${search.trim()}”.`}
                hint="This searches the person's name and their persona — try “Manager” or “LDE”. Someone who has never signed in has no row here at all, however you spell them."
                action={
                  <Button size="sm" onClick={() => setSearch('')}>
                    Clear the search
                  </Button>
                }
              />
            )}
            </Card>
          </div>
        )}

        <div className="mt-4 max-w-4xl space-y-3">
          <InfoNote>
            A Manager may hold many colleges; an LDE Executive typically holds one. A Senior
            Manager is assigned a <strong>cluster</strong>, and{' '}
            <code>colleges.cluster_id</code> expands that to every college in it — so a college
            with no cluster is reached by no Senior Manager, however senior they are. There is
            no “sees everything” persona in this schema: a Senior Manager with no cluster
            assignment sees no P&amp;L, and that is correct rather than a bug.
          </InfoNote>
          {clusters.length === 0 && (
            <InfoNote>
              No clusters exist yet, so Senior Managers can only be given direct college
              assignments. Clusters have no screen of their own in Phase 1 — create them in the
              database, then assign colleges to them from the Colleges page.
            </InfoNote>
          )}
        </div>
      </Page>
    </>
  )
}
