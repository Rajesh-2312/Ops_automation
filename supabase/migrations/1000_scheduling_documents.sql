-- =============================================================================
-- byteXL Ops Intelligence Platform — 1000 — checklist scheduling and the
--                                           document register
-- =============================================================================
-- Two changes aimed at the same thing: making the console describe how the
-- operation actually RUNS, rather than just what exists.
--
-- 1. SCHEDULING. task_templates gain an offset and an anchor, so generating a
--    checklist also generates a realistic timeline. Without this every task is
--    created with a NULL due_date, which means the urgency bands in 0500 can
--    never fire — nobody is going to set 37 due dates by hand per program, so in
--    practice they would stay empty forever and the "needs attention" panel
--    would read empty on precisely the programs that need attention.
--
-- 2. DOCUMENT REGISTER. The byteXL Drive is organised as 00_Templates_and_
--    Masters plus a folder per stage, and documents are filed as work proceeds.
--    That IS the operating model, so it gets modelled: master templates in one
--    table, per-program instances in another.
--
-- WHY THE FOLDERS ARE CATEGORIES AND NOT STAGES
-- ---------------------------------------------
-- There are twelve working folders and six pipeline stages. Adding stages to
-- match would relitigate CLAUDE.md §5-§6. Instead each folder becomes a document
-- CATEGORY carrying the stage it belongs to, so the filing structure is
-- preserved exactly while the pipeline stays as specified. 99_Archive is not a
-- stage either — it is a program state — and 00_Templates_and_Masters is the
-- master library that seeds the rest.
-- =============================================================================

-- =============================================================================
-- 1. Scheduling
-- =============================================================================
-- Kept out of 0500 and grouped here with the register because both answer the
-- same question — when is this program's paperwork due, and has it been filed —
-- and because the seed populates both in one pass.

alter table public.task_templates
  add column offset_days   integer,
  add column offset_anchor public.schedule_anchor not null default 'program_start';

comment on column public.task_templates.offset_days is
  'Days from the anchor date. Negative = before (MOU at -45 is due 45 days pre-start). NULL = no automatic due date.';
comment on column public.task_templates.offset_anchor is
  'Which program date offset_days counts from. Closeout work anchors to program_end; nothing else makes sense for it.';

-- Generation is FastAPI's job, not a trigger's: it needs the program's dates,
-- which may be NULL during acquisition_setup, and a task created with a silently
-- NULL due_date is better than one created with a due date derived from a date
-- nobody has confirmed yet.

-- =============================================================================
-- 2. Document register
-- =============================================================================

-- --- document_templates (the 00_Templates_and_Masters library) ---------------

create table public.document_templates (
  id              uuid primary key default gen_random_uuid(),
  category        public.document_category not null,
  stage           public.program_stage,
  name            text not null,
  description     text,
  -- Link to the master in Drive. We never hold a copy.
  master_url      text,
  -- Whether every program is expected to produce one of these.
  is_required     boolean not null default true,
  applies_to_type public.program_type,
  order_index     integer not null default 0,
  is_active       boolean not null default true,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);

comment on table public.document_templates is
  'The master document library. One row per template in 00_Templates_and_Masters, tagged with the folder it is filed under and the stage that produces it.';
comment on column public.document_templates.stage is
  'NULL for categories that are not stage work (the master library itself, and the archive).';

create unique index document_templates_category_name_key
  on public.document_templates (category, lower(name));
create index document_templates_stage_idx on public.document_templates (stage, order_index);

create trigger document_templates_set_updated_at
  before update on public.document_templates
  for each row execute function public.set_updated_at();

-- --- program_documents (per-program instances) -------------------------------

create table public.program_documents (
  id                   uuid primary key default gen_random_uuid(),
  program_id           uuid not null references public.programs (id) on delete cascade,
  document_template_id uuid references public.document_templates (id) on delete set null,
  category             public.document_category not null,
  name                 text not null,
  status               public.document_status not null default 'not_started',
  -- The filled-in document, wherever it lives. Drive stays the system of record;
  -- we hold the link and the state.
  url                  text,
  owner_id             uuid references public.profiles (id) on delete set null,
  due_date             date,
  filed_at             timestamptz,
  notes                text,
  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now()
);

comment on table public.program_documents is
  'What each program owes, per document. Mirrors filing a copy of a master into that program''s stage folder.';
comment on column public.program_documents.category is
  'Copied from the template rather than joined, because it is also a SECURITY input — the RLS policies below wall off the remuneration and invoice categories, and a policy that had to join to find out would recurse through document_templates.';

create index program_documents_program_idx on public.program_documents (program_id, category);
create index program_documents_status_idx  on public.program_documents (status);
create unique index program_documents_unique_template
  on public.program_documents (program_id, document_template_id)
  where document_template_id is not null;

create trigger program_documents_set_updated_at
  before update on public.program_documents
  for each row execute function public.set_updated_at();

-- Keep filed_at consistent with status, the same way tasks.completed_at is
-- handled — so neither can be set to something the status contradicts.
create or replace function public.program_documents_stamp()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if new.status in ('filed', 'approved') and
     (tg_op = 'INSERT' or old.status not in ('filed', 'approved')) then
    new.filed_at := coalesce(new.filed_at, now());
  elsif new.status not in ('filed', 'approved') then
    new.filed_at := null;
  end if;
  return new;
end;
$$;

create trigger program_documents_stamp
  before insert or update on public.program_documents
  for each row execute function public.program_documents_stamp();

-- =============================================================================
-- RLS
-- =============================================================================

alter table public.document_templates enable row level security;
alter table public.document_templates force  row level security;
alter table public.program_documents  enable row level security;
alter table public.program_documents  force  row level security;

grant select, insert, update, delete on public.document_templates to authenticated;
grant select, insert, update, delete on public.program_documents  to authenticated;

-- --- document_templates -------------------------------------------------------
-- All internal staff read the master library; only an admin curates it, matching
-- how task_templates works.
--
-- No commercials split here, unlike program_documents below: a master template
-- is a BLANK. "Invoice template" names a form, not an amount. The instance that
-- gets filed against a program is the one that carries numbers.

create policy document_templates_internal_select on public.document_templates
  for select to authenticated
  using (public.is_internal());

create policy document_templates_admin_write on public.document_templates
  for all to authenticated
  using (public.is_admin())
  with check (public.is_admin());

-- --- program_documents --------------------------------------------------------
-- Split in two, because a filed document register is a list of links to real
-- documents and two of its categories are money.
--
-- A row like "Remuneration sheet — July, filed, <drive url>" is not an amount,
-- but it is a live pointer to one, and Drive's own permissions are outside this
-- database's control. §4 says the LDE Executive gets zero rows from remuneration
-- and invoices; handing them the URL instead of the number would satisfy the
-- letter of that and defeat it entirely.
--
-- Everything else in the register — MOUs, tracksheets, observation forms,
-- assessment packs — is operational and reaches all three internal personas on
-- the usual program-reach terms.

create policy program_documents_internal_all on public.program_documents
  for all to authenticated
  using (
    public.is_internal()
    and public.can_reach_program(program_id)
    and category not in ('remuneration', 'invoice_generation')
  )
  with check (
    public.is_internal()
    and public.can_reach_program(program_id)
    and category not in ('remuneration', 'invoice_generation')
  );

create policy program_documents_commercials_all on public.program_documents
  for all to authenticated
  using (public.can_see_commercials() and public.can_reach_program(program_id))
  with check (public.can_see_commercials() and public.can_reach_program(program_id));

-- No trainer policy and no college policy on either table. Deny by default.
-- Selective sharing, if it is ever wanted, should be an explicit publish gate
-- like governance_reports.shared_with_college_at — not a blanket read.
