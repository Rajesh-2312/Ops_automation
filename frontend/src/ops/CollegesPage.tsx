import { useEffect, useState, type FormEvent } from 'react'
import { PAGE, bounded, emptyBound, usePageLimit } from '../lib/bounds'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { supabase, errorMessage, unwrap } from '../lib/supabase'
import { qk } from '../lib/queryKeys'
import {
  DOC_STATUSES,
  DOC_STATUS_LABEL,
  type Cluster,
  type College,
  type DocStatus,
} from '../lib/types'
import { Page, PageHeader } from '../components/AppShell'
import { useAuth } from '../auth/AuthProvider'
import {
  Badge,
  BoundNote,
  Button,
  Card,
  DocStatusPill,
  EmptyState,
  ErrorNote,
  Field,
  HelpTip,
  InfoNote,
  Input,
  Modal,
  SearchInput,
  Select,
  TableSkeleton,
  Td,
  Textarea,
  Th,
  Toolbar,
} from '../components/ui'

/**
 * Colleges — the root of the whole scope graph.
 *
 * WHAT EACH PERSONA CAN DO HERE, AND WHY THE BUTTONS DIFFER (migration 0300):
 *
 *   colleges_internal_select / _update — every internal persona reads and edits
 *   the colleges they REACH. So this list is already scoped: a Manager sees
 *   their assigned colleges, a Senior Manager sees every college in their
 *   clusters, an LDE Executive sees their campus. There is no client-side
 *   filter here and there must not be one.
 *
 *   colleges_admin_all — creating a college is ADMIN ONLY, and that is a
 *   structural fact rather than a policy preference: an insert policy predicated
 *   on can_reach_college(id) is unsatisfiable, because nobody can reach a
 *   college that does not exist yet. So the WITH CHECK could never pass. The
 *   "New college" button is therefore gated on isAdmin — cosmetically. A
 *   non-admin who forced the insert gets 42501 from Postgres.
 *
 * `cluster_id` is new in this schema and is not decoration: it is what expands a
 * Senior Manager's single cluster assignment into reach over every college in
 * it. Leaving it NULL means no Senior Manager reaches this college by cluster.
 */
export function CollegesPage() {
  const { isAdmin } = useAuth()
  const queryClient = useQueryClient()
  const [editing, setEditing] = useState<College | null>(null)
  const [creating, setCreating] = useState(false)

  // BOUNDED. The college roster is the entity every other list hangs off and it
  // grows for as long as the business does — a Senior Manager's cluster is ~40
  // colleges today and this table is org-wide. `colleges_internal_select`
  // resolves reach through `can_reach_college()` at 47 µs a row, so the cost is
  // linear in colleges REACHABLE, not in colleges shown.
  const page = usePageLimit(PAGE.colleges)

  const collegesQuery = useQuery({
    queryKey: qk.colleges.list(page.limit),
    queryFn: () =>
      bounded<College>(page.limit, (rows) =>
        supabase.from('colleges').select('*').order('name').limit(rows),
      ),
  })

  // Clusters are readable by every internal persona (clusters_internal_select),
  // so the cluster column resolves for everyone even though only an admin can
  // change the mapping.
  //
  // DELIBERATELY UNBOUNDED, and the only read on this screen that is. A cluster
  // is org geography: a handful of rows, one per region, admin-created, read
  // here purely to resolve `cluster_id` into a name. A page size on a
  // fixed-size lookup is ceremony — and a truncated one would render a real
  // cluster as "Unknown cluster", which is a worse outcome than the read it
  // saves.
  const clustersQuery = useQuery({
    queryKey: qk.clusters.list(),
    queryFn: () => unwrap<Cluster[]>(supabase.from('clusters').select('*').order('name')),
  })

  const clusters = clustersQuery.data ?? []
  const clusterName = (id: string | null) =>
    id ? (clusters.find((c) => c.id === id)?.name ?? 'Unknown cluster') : null

  const bound = collegesQuery.data ?? emptyBound<College>(page.limit)
  const rows = bound.rows
  const failure = collegesQuery.error

  // Display-only narrowing over the rows already returned. It searches the
  // cluster name too, because "show me everything in the South cluster" is how
  // a Senior Manager thinks about this roster.
  const [search, setSearch] = useState('')
  const q = search.trim().toLowerCase()
  const visible =
    q === ''
      ? rows
      : rows.filter(
          (c) =>
            c.name.toLowerCase().includes(q) ||
            (c.city ?? '').toLowerCase().includes(q) ||
            (clusterName(c.cluster_id) ?? '').toLowerCase().includes(q),
        )

  function close() {
    setCreating(false)
    setEditing(null)
  }

  return (
    <>
      <PageHeader
        title="Colleges"
        purpose="Every college you are assigned to, and how far its paperwork has got. A college has to exist here before any program, batch or trainer can be attached to it."
        subtitle={
          collegesQuery.isPending
            ? 'Loading…'
            : `${rows.length}${bound.truncated ? '+' : ''} college${
                rows.length === 1 ? '' : 's'
              } you can reach · the signed files themselves stay in the documents bucket`
        }
        actions={
          <Button
            variant="primary"
            size="sm"
            disabled={!isAdmin}
            title={
              isAdmin
                ? undefined
                : 'Creating a college is an admin action — reach cannot be granted to a college that does not exist yet.'
            }
            onClick={() => setCreating(true)}
          >
            New college
          </Button>
        }
      />

      <Page>
        {failure && (
          <div className="mb-4">
            <ErrorNote>{errorMessage(failure)}</ErrorNote>
          </div>
        )}

        {collegesQuery.isPending ? (
          <Card className="overflow-hidden">
            <TableSkeleton rows={6} cols={6} />
          </Card>
        ) : rows.length === 0 ? (
          <Card>
            <EmptyState
              title="No colleges visible to you"
              body={
                isAdmin
                  ? 'Every program belongs to a college, so start here.'
                  : 'Either none exist yet, or none are assigned to you. Reach comes from your college and cluster assignments — an admin grants it on Users & roles.'
              }
              hint={
                isAdmin
                  ? 'Nothing is hidden from you here — the roster is genuinely empty. Adding a college is the first step of the whole lifecycle.'
                  : 'This screen never hides a college it can see. If a college you work with is missing, you have not been assigned to it (or to its cluster) yet — ask an admin to add the assignment on Users & roles.'
              }
              action={
                isAdmin ? (
                  <Button variant="primary" size="sm" onClick={() => setCreating(true)}>
                    Add the first college
                  </Button>
                ) : undefined
              }
            />
          </Card>
        ) : (
          <div className="space-y-3">
            <BoundNote bound={bound} noun="colleges" onMore={page.more} step={page.step} />

            <Toolbar>
              <SearchInput
                value={search}
                onChange={setSearch}
                placeholder="Search college, city or cluster…"
                count={visible.length}
                total={rows.length}
              />
            </Toolbar>

            <Card className="overflow-hidden">
            <div className="overflow-x-auto scroll-slim">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-line">
                    <Th>College</Th>
                    <Th>
                      <HelpTip term="Cluster">
                        A group of colleges treated as one region. It is what turns a Senior
                        Manager's single cluster assignment into reach over every college
                        inside it — an unclustered college is reachable only by the people
                        assigned to it by name.
                      </HelpTip>
                    </Th>
                    <Th>Contact</Th>
                    <Th>
                      <HelpTip term="MoU">
                        Memorandum of Understanding — the agreement with the college that
                        says the training will happen at all. It comes before the purchase
                        order and before any program can be planned.
                      </HelpTip>
                    </Th>
                    <Th>
                      <HelpTip term="PO">
                        Purchase Order — the college's commitment to pay, raised after the
                        MoU. Until it is signed, work here is at risk: the program can run,
                        but nothing is contracted to be paid for.
                      </HelpTip>
                    </Th>
                    <Th />
                  </tr>
                </thead>
                <tbody className="divide-y divide-line-soft">
                  {visible.map((c) => (
                    <tr key={c.id} className="hover:bg-surface-2/60 transition">
                      <Td className="font-medium text-ink">
                        {c.name}
                        <span className="block text-xs font-normal text-ink-3">
                          {c.city || 'No city set'}
                        </span>
                      </Td>
                      <Td>
                        {c.cluster_id ? (
                          <Badge>{clusterName(c.cluster_id)}</Badge>
                        ) : (
                          <span
                            className="text-xs text-ink-3"
                            title="No Senior Manager reaches this college by cluster."
                          >
                            Unclustered
                          </span>
                        )}
                      </Td>
                      <Td className="text-ink-2">
                        {c.contact_name || '—'}
                        {c.contact_email && (
                          <span className="block text-xs text-ink-3">{c.contact_email}</span>
                        )}
                      </Td>
                      <Td>
                        <DocStatusPill status={c.mou_status} />
                      </Td>
                      <Td>
                        <DocStatusPill status={c.po_status} />
                      </Td>
                      <Td className="text-right">
                        <Button size="sm" variant="ghost" onClick={() => setEditing(c)}>
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
                title="No college matches that search"
                body={`Nothing in your roster mentions “${search.trim()}”.`}
                hint="This searches the college name, its city and its cluster. A college you cannot reach will never appear here however you spell it — that is an assignment question, not a search one."
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

        {rows.length > 0 && !isAdmin && (
          <div className="mt-4 max-w-3xl">
            <InfoNote>
              You can edit the colleges you reach but not create or delete one, and you
              cannot move a college between clusters — both change who else can see it,
              which is an admin action.
            </InfoNote>
          </div>
        )}
      </Page>

      <CollegeModal
        open={creating || editing !== null}
        college={editing}
        clusters={clusters}
        canSetCluster={isAdmin}
        onClose={close}
        onSaved={() => {
          close()
          void queryClient.invalidateQueries({ queryKey: qk.colleges.all })
        }}
      />
    </>
  )
}

interface CollegeForm {
  name: string
  city: string
  cluster_id: string
  contact_name: string
  contact_email: string
  contact_phone: string
  mou_status: DocStatus
  po_status: DocStatus
  notes: string
}

const EMPTY_FORM: CollegeForm = {
  name: '',
  city: '',
  cluster_id: '',
  contact_name: '',
  contact_email: '',
  contact_phone: '',
  mou_status: 'not_started',
  po_status: 'not_started',
  notes: '',
}

function CollegeModal({
  open,
  college,
  clusters,
  canSetCluster,
  onClose,
  onSaved,
}: {
  open: boolean
  college: College | null
  clusters: Cluster[]
  canSetCluster: boolean
  onClose: () => void
  onSaved: () => void
}) {
  const [form, setForm] = useState<CollegeForm>(EMPTY_FORM)

  useEffect(() => {
    if (!open) return
    setForm(
      college
        ? {
            name: college.name,
            city: college.city ?? '',
            cluster_id: college.cluster_id ?? '',
            contact_name: college.contact_name ?? '',
            contact_email: college.contact_email ?? '',
            contact_phone: college.contact_phone ?? '',
            mou_status: college.mou_status,
            po_status: college.po_status,
            notes: college.notes ?? '',
          }
        : EMPTY_FORM,
    )
  }, [open, college])

  const save = useMutation({
    mutationFn: () => {
      const payload = {
        name: form.name,
        city: form.city || null,
        contact_name: form.contact_name || null,
        contact_email: form.contact_email || null,
        contact_phone: form.contact_phone || null,
        mou_status: form.mou_status,
        po_status: form.po_status,
        notes: form.notes || null,
        // Only sent when the caller may set it. An admin-only column included in
        // a non-admin UPDATE would fail the whole statement even when the value
        // is unchanged.
        ...(canSetCluster ? { cluster_id: form.cluster_id || null } : {}),
      }
      return college
        ? unwrap(supabase.from('colleges').update(payload).eq('id', college.id))
        : unwrap(supabase.from('colleges').insert(payload))
    },
    onSuccess: onSaved,
  })

  const set = <K extends keyof CollegeForm>(k: K, v: CollegeForm[K]) =>
    setForm((f) => ({ ...f, [k]: v }))

  function onSubmit(e: FormEvent) {
    e.preventDefault()
    save.mutate()
  }

  return (
    <Modal open={open} onClose={onClose} title={college ? 'Edit college' : 'New college'}>
      <form onSubmit={onSubmit} className="space-y-4">
        <Field label="Name">
          <Input
            required
            value={form.name}
            onChange={(e) => set('name', e.target.value)}
            placeholder="Malineni Lakshmaiah Womens Engineering College"
          />
        </Field>

        <div className="grid grid-cols-2 gap-3">
          <Field label="City">
            <Input value={form.city} onChange={(e) => set('city', e.target.value)} />
          </Field>
          <Field
            label="Cluster"
            hint={
              canSetCluster
                ? 'Expands a Senior Manager’s cluster assignment to this college.'
                : 'Admin only — changing it changes who reaches this college.'
            }
          >
            <Select
              value={form.cluster_id}
              disabled={!canSetCluster}
              onChange={(e) => set('cluster_id', e.target.value)}
            >
              <option value="">Unclustered</option>
              {clusters.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </Select>
          </Field>
        </div>

        <div className="grid grid-cols-2 gap-3">
          <Field label="Contact name">
            <Input
              value={form.contact_name}
              onChange={(e) => set('contact_name', e.target.value)}
            />
          </Field>
          <Field label="Contact phone">
            <Input
              value={form.contact_phone}
              onChange={(e) => set('contact_phone', e.target.value)}
            />
          </Field>
        </div>

        <Field label="Contact email">
          <Input
            type="email"
            value={form.contact_email}
            onChange={(e) => set('contact_email', e.target.value)}
          />
        </Field>

        <div className="grid grid-cols-2 gap-3">
          <Field
            label="MoU status"
            hint="Memorandum of Understanding — the agreement that the training will happen."
          >
            <Select
              value={form.mou_status}
              onChange={(e) => set('mou_status', e.target.value as DocStatus)}
            >
              {DOC_STATUSES.map((s) => (
                <option key={s} value={s}>
                  {DOC_STATUS_LABEL[s]}
                </option>
              ))}
            </Select>
          </Field>
          <Field
            label="PO status"
            hint="Purchase Order — the college's commitment to pay, raised after the MoU."
          >
            <Select
              value={form.po_status}
              onChange={(e) => set('po_status', e.target.value as DocStatus)}
            >
              {DOC_STATUSES.map((s) => (
                <option key={s} value={s}>
                  {DOC_STATUS_LABEL[s]}
                </option>
              ))}
            </Select>
          </Field>
        </div>

        <Field label="Notes">
          <Textarea rows={3} value={form.notes} onChange={(e) => set('notes', e.target.value)} />
        </Field>

        {save.error && <ErrorNote>{errorMessage(save.error)}</ErrorNote>}

        <div className="flex justify-end gap-2">
          <Button type="button" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" disabled={save.isPending}>
            {college ? 'Save changes' : 'Create college'}
          </Button>
        </div>
      </form>
    </Modal>
  )
}
