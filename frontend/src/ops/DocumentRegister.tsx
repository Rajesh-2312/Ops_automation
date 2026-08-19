import { useMemo, useState } from 'react'
import { PAGE, bounded, emptyBound, type Bounded } from '../lib/bounds'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { supabase, errorMessage, unwrap } from '../lib/supabase'
import { qk } from '../lib/queryKeys'
import { generateDocuments } from '../lib/api'
import {
  CATEGORY_LABEL,
  COMMERCIAL_CATEGORIES,
  DOCUMENT_STATUSES,
  DOCUMENT_STATUS_LABEL,
  type DocumentCategory,
  type DocumentStatus,
  type Program,
  type ProgramDocument,
} from '../lib/types'
import { useAuth } from '../auth/AuthProvider'
import {
  Badge,
  BoundNote,
  Button,
  Card,
  EmptyState,
  ErrorNote,
  HelpTip,
  InfoNote,
  Input,
  PageIntro,
  SearchInput,
  Select,
  TableSkeleton,
  Toolbar,
  Meter,
} from '../components/ui'

const STATUS_TONE: Record<DocumentStatus, string> = {
  not_started: 'text-ink-3',
  in_progress: 'text-warn-ink',
  filed: 'text-info-ink',
  approved: 'text-good-ink',
  not_applicable: 'text-ink-3',
}

/**
 * The document register for one program, grouped by Drive folder.
 *
 * This is the app's representation of how byteXL already works: a master lives
 * in 00_Templates_and_Masters, a copy is filed into the numbered stage folder,
 * and the folder tells you which stage it belongs to. The categories carry the
 * Drive numbering so the two read identically side by side.
 *
 * The document itself is never held here — only the link and the state. Drive
 * stays the system of record (CLAUDE.md §10: link, never duplicate).
 *
 * TWO CATEGORIES ARE WALLED OFF IN THE DATABASE. `program_documents` has two
 * policies (migration 1000): the internal one excludes 'remuneration' and
 * 'invoice_generation', and a second, commercials-only policy adds them back
 * for Senior Manager and Manager. So an LDE Executive reading this register
 * gets the operational rows and nothing else — not because this component
 * filtered anything (it does not), but because those rows never arrive. The
 * note rendered below tells them that is what happened, rather than leaving
 * them to wonder where the remuneration folder went.
 */
export function DocumentRegister({ program }: { program: Program }) {
  const { canSeeCommercials } = useAuth()
  const queryClient = useQueryClient()
  const [note, setNote] = useState<string | null>(null)

  // BOUNDED, generously. `seed.sql` ships 37 document templates, so a fully
  // generated register is 37 rows and 200 is five times that — a program past
  // the bound has had rows added by hand. The bound is here because
  // `program_documents` carries TWO policies (1000) and both resolve reach
  // per row, so an unbounded read is linear in a cost nobody sees.
  const documentsQuery = useQuery({
    queryKey: qk.documents.byProgram(program.id, PAGE.programDocuments),
    queryFn: () =>
      bounded<ProgramDocument>(PAGE.programDocuments, (rows) =>
        supabase
          .from('program_documents')
          .select('*')
          .eq('program_id', program.id)
          .order('category')
          .order('name')
          .limit(rows),
      ),
  })

  const bound = documentsQuery.data ?? emptyBound<ProgramDocument>(PAGE.programDocuments)
  const docs = useMemo(() => bound.rows, [bound.rows])

  // Display-only. A full register is 37 rows across thirteen folder cards, which
  // is more scrolling than "where is the signed work order?" deserves. The
  // settled meter below deliberately keeps counting the WHOLE register, not the
  // visible slice — see the note on it.
  const [search, setSearch] = useState('')
  const query = search.trim().toLowerCase()
  const visible = useMemo(
    () =>
      query === ''
        ? docs
        : docs.filter(
            (d) =>
              d.name.toLowerCase().includes(query) ||
              CATEGORY_LABEL[d.category].toLowerCase().includes(query),
          ),
    [docs, query],
  )

  const patch = useMutation({
    mutationFn: ({ id, body }: { id: string; body: Partial<ProgramDocument> }) =>
      unwrap<ProgramDocument>(
        supabase.from('program_documents').update(body).eq('id', id).select().maybeSingle(),
      ),
    onMutate: async ({ id, body }) => {
      const key = qk.documents.byProgram(program.id, PAGE.programDocuments)
      await queryClient.cancelQueries({ queryKey: key })
      const previous = queryClient.getQueryData<Bounded<ProgramDocument>>(key)
      queryClient.setQueryData<Bounded<ProgramDocument>>(key, (old) =>
        old === undefined
          ? old
          : { ...old, rows: old.rows.map((d) => (d.id === id ? { ...d, ...body } : d)) },
      )
      return { previous }
    },
    onError: (_err, _vars, context) => {
      if (context?.previous) {
        queryClient.setQueryData(
          qk.documents.byProgram(program.id, PAGE.programDocuments),
          context.previous,
        )
      }
    },
    // Take the server row back: filed_at is stamped by the program_documents_stamp
    // trigger, not by this client.
    onSuccess: (row) => {
      if (!row) return
      queryClient.setQueryData<Bounded<ProgramDocument>>(
        qk.documents.byProgram(program.id, PAGE.programDocuments),
        (old) =>
          old === undefined
            ? old
            : { ...old, rows: old.rows.map((d) => (d.id === row.id ? row : d)) },
      )
    },
  })

  const generate = useMutation({
    mutationFn: () => generateDocuments(program.id),
    onSuccess: (result) => {
      setNote(
        result.created === 0
          ? 'Already complete — every required master is on this program.'
          : `Filed ${result.created} new document${result.created === 1 ? '' : 's'}.`,
      )
      void queryClient.invalidateQueries({
        queryKey: qk.documents.byProgram(program.id, PAGE.programDocuments),
      })
    },
    onError: () => setNote(null),
  })

  const byCategory = useMemo(() => {
    const map = new Map<DocumentCategory, ProgramDocument[]>()
    for (const d of visible) {
      if (!map.has(d.category)) map.set(d.category, [])
      map.get(d.category)!.push(d)
    }
    // Sort by the numbered label, which is the Drive folder order.
    return [...map.entries()].sort((a, b) =>
      CATEGORY_LABEL[a[0]].localeCompare(CATEGORY_LABEL[b[0]]),
    )
  }, [visible])

  const settled = docs.filter(
    (d) => d.status === 'approved' || d.status === 'filed' || d.status === 'not_applicable',
  ).length

  const failure = documentsQuery.error ?? patch.error ?? generate.error

  // A skeleton rather than a spinner: this panel sits inside a tab that is
  // already on screen, so collapsing it to a centred spinner and springing it
  // back is a bigger jump than the wait it is covering.
  if (documentsQuery.isPending) {
    return (
      <Card>
        <TableSkeleton rows={6} cols={3} />
      </Card>
    )
  }

  return (
    <div className="space-y-4">
      <PageIntro
        steps={[
          'Build the register from the master library',
          'File a copy of each master in the program’s Drive folder',
          'Paste that Drive link on the row here',
          'Move the row to Filed, then Approved',
        ]}
      >
        Every document this program is required to have, in one checklist, grouped by the
        same numbered Drive folders your team already uses. The file itself always stays in
        Drive — this register holds only the link to it and how far along it is, so nothing
        here can go out of date against the real document.
      </PageIntro>

      {failure && <ErrorNote>{errorMessage(failure)}</ErrorNote>}

      {!canSeeCommercials && (
        <InfoNote>
          The <strong>Remuneration</strong> and <strong>Invoice generation</strong> folders
          are not listed for your role. A register row is a live link to a rupee figure, so
          the database excludes both categories for an LDE Executive — this component does
          no filtering of its own and would show them if they arrived.
        </InfoNote>
      )}

      <BoundNote
        bound={bound}
        noun="register rows"
        derived="The “documents settled” meter counts only those."
      />

      {docs.length === 0 ? (
        <Card>
          <EmptyState
            title="No document register yet"
            body="Build this program's document list from the master library — one entry per required template, filed under the same folders as the Drive."
            hint="It is empty because nobody has built it yet, not because this program needs no paperwork. One click copies the whole master checklist onto this program; you can still delete or mark rows N/A afterwards."
            action={
              <Button
                variant="primary"
                size="sm"
                disabled={generate.isPending}
                onClick={() => generate.mutate()}
              >
                Build register from masters
              </Button>
            }
          />
        </Card>
      ) : (
        <>
          <Toolbar>
            <SearchInput
              value={search}
              onChange={setSearch}
              placeholder="Find a document or folder…"
              count={visible.length}
              total={docs.length}
            />
          </Toolbar>

          <Card className="p-4">
            <div className="flex items-center justify-between gap-3 mb-2">
              <span className="text-sm font-medium text-ink">
                <HelpTip term="Documents settled">
                  A row is settled once it is <strong>Filed</strong>, <strong>Approved</strong>{' '}
                  or marked <strong>N/A</strong> — in other words, once nobody has to chase it
                  any more. Not started and In progress are the ones still owed.
                </HelpTip>
              </span>
              {/* Always the whole register, never the searched slice: this meter is
                  the answer to "is this program's paperwork done", which a search
                  box in the corner must not be able to change. */}
              <span className="text-xs tabular-nums text-ink-2">
                {settled} of {docs.length}
              </span>
            </div>
            <Meter pct={docs.length ? (100 * settled) / docs.length : 0} />
          </Card>

          {byCategory.length === 0 && (
            <Card>
              <EmptyState
                title="No document matches that search"
                body={`Nothing in this register mentions “${search.trim()}”.`}
                hint="The register holds all 37 masters for a program, so a miss usually means a different wording — try “work order”, “MoU”, or a folder number such as “04”."
                action={
                  <Button size="sm" onClick={() => setSearch('')}>
                    Clear the search
                  </Button>
                }
              />
            </Card>
          )}

          {byCategory.map(([category, items]) => (
            <Card key={category}>
              <div className="flex items-center justify-between gap-3 px-4 py-3 border-b border-line">
                <div className="flex items-center gap-2 min-w-0">
                  <h3 className="text-sm font-semibold text-ink font-mono tracking-tight truncate">
                    {CATEGORY_LABEL[category]}
                  </h3>
                  {COMMERCIAL_CATEGORIES.includes(category) && (
                    <Badge tone="warn">
                      <HelpTip term="Commercial">
                        This folder points at money — rates, payouts, invoices. Only a Senior
                        Manager and a Manager can see it; an LDE Executive is refused these
                        rows by the database, not by this screen.
                      </HelpTip>
                    </Badge>
                  )}
                </div>
                <span className="text-xs tabular-nums text-ink-3 shrink-0">
                  {items.filter((d) => d.status === 'approved' || d.status === 'filed').length}/
                  {items.length}
                </span>
              </div>

              <ul className="divide-y divide-line-soft">
                {items.map((doc) => (
                  <li key={doc.id} className="px-4 py-3">
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0 flex-1">
                        <p className="text-sm text-ink leading-snug">{doc.name}</p>
                        <p className={`text-[11px] mt-0.5 font-medium ${STATUS_TONE[doc.status]}`}>
                          {DOCUMENT_STATUS_LABEL[doc.status]}
                          {doc.filed_at && ' · filed'}
                        </p>
                      </div>

                      <Select
                        className="!h-7 !w-28 !text-xs shrink-0"
                        value={doc.status}
                        onChange={(e) =>
                          patch.mutate({
                            id: doc.id,
                            body: { status: e.target.value as DocumentStatus },
                          })
                        }
                        aria-label={`Status for ${doc.name}`}
                      >
                        {DOCUMENT_STATUSES.map((s) => (
                          <option key={s} value={s}>
                            {DOCUMENT_STATUS_LABEL[s]}
                          </option>
                        ))}
                      </Select>
                    </div>

                    {/* Link out, never store. The document lives in Drive. */}
                    <div className="flex items-center gap-2 mt-2">
                      <Input
                        className="!h-7 !text-xs"
                        placeholder="Paste the Drive link once it's filed…"
                        defaultValue={doc.url ?? ''}
                        onBlur={(e) => {
                          const next = e.target.value.trim() || null
                          if (next !== doc.url) patch.mutate({ id: doc.id, body: { url: next } })
                        }}
                        aria-label={`Link for ${doc.name}`}
                      />
                      {doc.url && (
                        <a
                          href={doc.url}
                          target="_blank"
                          rel="noreferrer"
                          className="text-xs font-medium text-accent hover:underline shrink-0"
                        >
                          Open ↗
                        </a>
                      )}
                    </div>
                  </li>
                ))}
              </ul>
            </Card>
          ))}

          <div className="flex flex-wrap items-center justify-end gap-3">
            <span className="text-xs text-ink-3 mr-auto">
              {note ?? 'Adds any master that is missing here. It never touches a row you have already filed.'}
            </span>
            <Button size="sm" disabled={generate.isPending} onClick={() => generate.mutate()}>
              Top up from masters
            </Button>
          </div>
        </>
      )}
    </div>
  )
}
