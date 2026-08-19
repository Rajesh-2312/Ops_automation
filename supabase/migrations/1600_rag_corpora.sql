-- =============================================================================
-- byteXL Ops Intelligence Platform — 1600 — the RAG corpora (Phase 3, Copilot)
-- =============================================================================
-- CLAUDE.md §9, in tables:
--
--   "Six corpora, separately indexed and permissioned: SOP, Contracts, College
--    dossier, Educator, Curriculum, Reports."
--   "Every answer cites source document and section. No citation → no answer."
--   "Persona filter applies BEFORE retrieval, not after generation."
--   "Structured facts (dates, amounts, counts) are never retrieved from RAG."
--   "Contracts corpus is versioned. A superseded clause must not surface without
--    a version flag."
--
-- WHY THE VECTORS LIVE IN THIS DATABASE AND NOT IN A VECTOR STORE
-- ---------------------------------------------------------------
-- §2 states it as a design constraint rather than a preference: pgvector, "same
-- Postgres — not a separate Chroma instance because permission filtering must
-- live in one place". A separate store cannot see `user_college_assignments`,
-- cannot call `can_see_commercials()`, and therefore cannot filter before it
-- retrieves. It can only hand back neighbours and hope the caller filters after,
-- which is the exact failure §9 names. Everything below follows from that: the
-- corpus ACL is a table, the wall is the same `can_see_commercials()` predicate
-- 0700 uses, and the nearest-neighbour scan itself is a function whose WHERE
-- clause carries the persona filter, so there is no way to ask for neighbours
-- without also stating who is asking.
--
-- THE FOUR TABLES
-- ---------------
--   rag_corpus_access  which persona may read which corpus. Data, not code.
--   rag_documents      one row per document VERSION. Contracts supersede.
--   rag_chunks         the retrievable unit. Carries its own `section`, because
--                      §9 requires a citation to name document AND section, and
--                      a citation the store cannot produce is a citation the
--                      generator will invent.
--   rag_embeddings     one row per (chunk, model). Split off the chunk so a
--                      re-embed under a new model is an insert rather than a
--                      destructive rewrite of the corpus, and so the vector —
--                      which is a lossy but real reconstruction surface for the
--                      chunk text — can be denied to `authenticated` outright
--                      while the text itself stays readable under policy.
--
-- WHAT THIS FILE DELIBERATELY DOES NOT DO
-- ---------------------------------------
-- It does not store facts. Nothing here is a system of record for an amount, a
-- date or a count (R1, §9). A chunk that quotes a rate is text about a rate; the
-- rate itself lives in `work_orders` and `remuneration_sheets`, and the Copilot
-- is forbidden from answering a numeric question from this schema at all —
-- enforced in `app/rag/guards.py`, tested, and restated in the comments below so
-- nobody "improves" retrieval by teaching it to answer them.
-- =============================================================================

-- --- pgvector ----------------------------------------------------------------
-- Supabase pre-installs `vector` into the `extensions` schema on most projects
-- and into `public` on some older ones. `create extension if not exists` is a
-- no-op in either case but leaves the type in a schema we cannot name in
-- advance, so the DDL below runs with both schemas on the search_path and lets
-- Postgres resolve `vector` wherever it actually is. The resolution happens once
-- at DDL time and is stored as a type OID, so nothing later depends on the
-- search_path being right — except the `<=>` operator in `rag_search()`, which
-- carries its own `set search_path` for the same reason.
do $$
begin
  if not exists (select 1 from pg_extension where extname = 'vector') then
    create extension vector with schema extensions;
  end if;
end
$$;

set local search_path = public, extensions;

-- =============================================================================
-- Enum
-- =============================================================================
-- 0100's house rule is "every enum in one file", and 1300 already argued the
-- exception: never edit a shipped migration is the stronger rule, this is a
-- `create type` in full rather than an `alter type ... add value`, and the type
-- is used by a table created further down this same file.
--
-- Values mirror `app.domain.enums.Corpus` exactly. Six, closed, and closed on
-- purpose: a seventh corpus is a migration plus an ACL decision plus a line in
-- `rag_corpus_access`, which is three places somebody has to think about who may
-- read it. An open `text` column would make "add a corpus" a one-line insert
-- that inherits nobody's permissions and therefore, by the deny-by-default
-- policies below, would silently be readable by no one — or, if someone "fixed"
-- that with a permissive default, by everyone.
create type public.rag_corpus as enum (
  'sop',
  'contracts',
  'college_dossier',
  'educator',
  'curriculum',
  'reports'
);

comment on type public.rag_corpus is
  'The six separately-indexed, separately-permissioned RAG corpora (CLAUDE.md §9). Mirrors app.domain.enums.Corpus.';

-- =============================================================================
-- rag_corpus_access — the persona filter, as data
-- =============================================================================
-- §9 says the six corpora are "separately permissioned". That could have been
-- six branches inside a policy; it is a table instead, for one reason: the
-- question "who can read Contracts?" must be answerable by SELECT, by a Senior
-- Manager, during an access review, without reading SQL source. A CASE inside a
-- policy body is invisible to everyone who is not holding the migration.
--
-- It is NOT a substitute for the commercials wall. Access here says "this
-- persona may read this corpus at all"; `can_see_commercials()` still applies
-- per row to anything flagged commercial, and both conjuncts appear in every
-- policy below in the house shape 0700 established:
--     using (<the wall> and <the scope>)
--
-- Seeded, not left empty. An empty ACL table means the Copilot returns nothing
-- for everyone, which reads as "retrieval is broken" rather than as "nobody has
-- been granted anything" — and the person debugging it will reach for a
-- permissive default. The seed states the §4 reading directly.
create table public.rag_corpus_access (
  corpus     public.rag_corpus not null,
  role       public.app_role   not null,
  -- Why this persona has this corpus. Free text, required, and it is the column
  -- an access review actually reads.
  rationale  text not null,
  granted_at timestamptz not null default now(),

  primary key (corpus, role),
  constraint rag_corpus_access_rationale_ck check (length(btrim(rationale)) > 0)
);

comment on table public.rag_corpus_access is
  'Which persona may read which RAG corpus (CLAUDE.md §9, §4). Data rather than a CASE in a policy body, so an access review is a SELECT. Does NOT replace can_see_commercials(), which still applies per row.';

insert into public.rag_corpus_access (corpus, role, rationale) values
  -- SOP: the reason the Copilot exists. §13 phase 3's gate is "trusted by
  -- Managers for policy lookups", and an LDE Executive on campus is the persona
  -- most often asking "what is the procedure here".
  ('sop', 'senior_manager', 'Policy lookup across the cluster.'),
  ('sop', 'manager',        'Policy lookup for own colleges.'),
  ('sop', 'lde_executive',  'On-campus procedure lookup. SOPs carry no commercials.'),

  -- CONTRACTS: commercials personas only, corpus-wide. Not per row: an MoU is
  -- one document whose clauses interleave scope, obligations and rates, and a
  -- chunker cannot be trusted to have caught every clause that quotes a number.
  -- The wall is drawn at the corpus so that a mis-flagged chunk is a redundancy
  -- failure rather than a leak. §4: an LDE Executive has NO commercials.
  ('contracts', 'senior_manager', 'Cluster P&L and escalation authority (§4).'),
  ('contracts', 'manager',        'Owns college commercials and trainer costs (§4).'),

  -- COLLEGE DOSSIER: history, contacts, past programs. Internal, and scoped
  -- further per document by can_reach_college() below — a Manager reads the
  -- dossiers of their own colleges, not the cluster's.
  ('college_dossier', 'senior_manager', 'Cluster oversight.'),
  ('college_dossier', 'manager',        'Own colleges.'),
  ('college_dossier', 'lde_executive',  'Own college only; commercial rows still walled.'),

  -- EDUCATOR: trainer-facing policy, capability notes, code of conduct. Internal
  -- staff only. A trainer does NOT get this corpus: §4 gives them "own
  -- deployment, tracksheet, invoice status. Nothing else", and an educator
  -- dossier is written about trainers, not for them.
  ('educator', 'senior_manager', 'Sourcing and quality oversight.'),
  ('educator', 'manager',        'Owns trainer deployment and quality.'),

  -- CURRICULUM: syllabus, module maps, delivery guides. The one corpus with a
  -- plausible college-facing case — deliberately not granted. §4 gives the
  -- College persona "published artifacts only, read-only", which is the curated
  -- view in 0800, not a similarity search over an internal index that also holds
  -- draft curricula. Granting it later is one INSERT; taking it back is a
  -- renegotiation with a customer.
  ('curriculum', 'senior_manager', 'Cluster oversight.'),
  ('curriculum', 'manager',        'Owns delivery.'),
  ('curriculum', 'lde_executive',  'Runs delivery on campus.'),

  -- REPORTS: governance reports, feedback synthesis. Internal. Commercial rows
  -- inside it (a report quoting programme margin) are flagged per row and walled
  -- by can_see_commercials(), because unlike Contracts a governance report is
  -- mostly delivery narrative and walling the whole corpus would cost the LDE
  -- Executive the part that is theirs.
  ('reports', 'senior_manager', 'Governance and escalation.'),
  ('reports', 'manager',        'Own colleges.'),
  ('reports', 'lde_executive',  'Delivery narrative for own college; commercial rows walled per row.');

-- NO trainer row and NO college row anywhere above, and that is the whole ACL
-- for those two personas. Deny by default: `can_read_corpus()` returns false for
-- a persona with no row, so the Copilot returns zero chunks rather than a
-- filtered subset. R5 requires a test asserting exactly that; see
-- tests/unit/test_rag_retrieval.py.

-- =============================================================================
-- rag_documents — one row per document VERSION
-- =============================================================================
-- The versioning is §9's, and it is the reason this table is not "one row per
-- document with a version column that gets overwritten":
--
--     "Contracts corpus is versioned. A superseded clause must not surface
--      without a version flag."
--
-- Superseding therefore has to preserve the old text, not replace it. A dispute
-- about a July payout is argued against the work order as it read in July, and
-- an index that silently upgraded itself to the November amendment would answer
-- that dispute wrongly and confidently. So: `version` counts up per
-- `source_ref`, `superseded_at` marks the older rows, both stay indexed, and
-- `rag_search()` excludes superseded chunks unless the caller explicitly asks —
-- and flags every row it returns either way, so the flag cannot be lost between
-- the query and the citation.
create table public.rag_documents (
  id           uuid primary key default gen_random_uuid(),

  corpus       public.rag_corpus not null,

  -- The stable identity of the document ACROSS versions: a Drive path, a
  -- storage object key, an SOP number. Two rows with the same source_ref are two
  -- versions of one document, which is what makes superseding mechanical.
  source_ref   text not null,

  -- What a citation names. §9: "Every answer cites source document and section."
  -- `title` is the document half of that; `rag_chunks.section` is the other.
  -- Both are `not null` because a chunk that cannot be cited must not be
  -- retrievable, and the cheapest place to guarantee that is the column.
  title        text not null,

  version      integer not null default 1,
  superseded_at timestamptz,

  -- Scope. NULL means "applies everywhere" — an SOP, a standard MoU template.
  -- Non-NULL narrows the document to a college or a program, and the reach
  -- helpers below then apply the same assignment-graph logic every other table
  -- in this schema uses. NOT redundant with the corpus ACL: the ACL says which
  -- corpus a persona may read at all, this says which rows of it are theirs.
  college_id   uuid references public.colleges(id) on delete cascade,
  program_id   uuid references public.programs(id) on delete cascade,

  -- §4's wall, at document granularity. TRUE means "reading this requires
  -- can_see_commercials()". Defaults FALSE, and the default is safe only because
  -- the Contracts corpus is walled wholesale in the ACL above — a contract that
  -- someone forgot to flag is still unreachable by an LDE Executive.
  is_commercial boolean not null default false,

  -- Provenance and idempotency. `content_hash` is the sha256 of the normalised
  -- source text: re-ingesting an unchanged document is a no-op, which is what
  -- makes `app/rag/ingest.py` re-runnable in a cron without churning the index
  -- (and without re-billing every embedding).
  content_hash text not null,

  ingested_at  timestamptz not null default now(),
  -- No FK, same reasoning as audit_events.actor_id: this is a historical record
  -- of who ingested, and it must outlive the account.
  ingested_by  uuid,
  updated_at   timestamptz not null default now(),

  constraint rag_documents_version_ck  check (version >= 1),
  constraint rag_documents_title_ck    check (length(btrim(title)) > 0),
  constraint rag_documents_source_ck   check (length(btrim(source_ref)) > 0),
  constraint rag_documents_hash_ck     check (length(btrim(content_hash)) > 0)
);

comment on table public.rag_documents is
  'One row per document VERSION (CLAUDE.md §9). Superseded versions are retained and flagged, never overwritten — a July dispute is argued against the July text.';
comment on column public.rag_documents.source_ref is
  'Stable identity ACROSS versions. Two rows sharing a source_ref are two versions of one document.';
comment on column public.rag_documents.superseded_at is
  'NULL means current. §9: "a superseded clause must not surface without a version flag" — rag_search() excludes these by default and flags them always.';
comment on column public.rag_documents.is_commercial is
  'TRUE when reading the document requires can_see_commercials() (§4). Belt to the Contracts corpus ACL''s braces.';
comment on column public.rag_documents.content_hash is
  'sha256 of the normalised source text. Re-ingesting an unchanged version is a no-op — this column is what makes ingestion idempotent.';

-- One version number per document, once.
create unique index rag_documents_version_uq
  on public.rag_documents (corpus, source_ref, version);

-- Exactly one CURRENT version per document, as a partial unique index — the
-- same construction artifact_versions uses in 1300, for the same reason: the
-- history is unbounded while the live row is unique, and two concurrent
-- ingestions of a new version cannot both land.
create unique index rag_documents_one_current
  on public.rag_documents (corpus, source_ref)
  where superseded_at is null;

create index rag_documents_corpus_idx  on public.rag_documents (corpus)
  where superseded_at is null;
create index rag_documents_college_idx on public.rag_documents (college_id)
  where college_id is not null;
create index rag_documents_program_idx on public.rag_documents (program_id)
  where program_id is not null;

create trigger rag_documents_set_updated_at
  before update on public.rag_documents
  for each row execute function public.set_updated_at();

-- =============================================================================
-- rag_chunks — the retrievable unit
-- =============================================================================
-- `section` is `not null` and that single word is §9's citation rule made
-- structural. "No citation → no answer" is enforced three times over, on
-- purpose, because the failure is silent and expensive:
--
--   1. HERE — a chunk without a section cannot be stored, so retrieval cannot
--      return one, so the generator is never in a position to be asked for a
--      citation it does not have.
--   2. In `rag_search()` — the section travels with every returned row.
--   3. In `app/rag/copilot.py` — an answer whose citation markers do not resolve
--      to retrieved chunks is REFUSED rather than returned uncited.
--
-- The reason for three is that a model asked to cite will happily invent
-- "SOP-14, §3.2" when the retrieved context has no section, and the invention is
-- indistinguishable from a real citation to the person reading it.
create table public.rag_chunks (
  id           uuid primary key default gen_random_uuid(),

  document_id  uuid not null references public.rag_documents(id) on delete cascade,

  -- Position within the document. Deterministic — `app/rag/chunking.py` is a
  -- pure function of the source text — which is what lets a re-ingest of the
  -- same version produce byte-identical chunks and therefore be a no-op.
  ordinal      integer not null,

  -- The other half of a citation. See the block comment above.
  section      text not null,

  content      text not null,

  -- sha256 of the chunk text. Cheap change detection at chunk granularity, so a
  -- one-paragraph edit re-embeds one chunk rather than the document.
  content_hash text not null,

  -- §4's wall at chunk granularity. Set by ingestion when the chunk quotes money
  -- (`app/rag/ingest.py`), OR inherited from the document. A commercial chunk is
  -- invisible to an LDE Executive even inside a corpus they may read — which is
  -- the case that matters for `reports`, where the delivery narrative is theirs
  -- and the margin paragraph is not.
  --
  -- To be explicit about what this column is NOT: it is not a claim that the
  -- number in the chunk is correct or current. R1 stands — a figure in a
  -- retrieved chunk is text, the system of record is the table it came from, and
  -- the Copilot refuses numeric questions outright rather than reading one off.
  is_commercial boolean not null default false,

  created_at   timestamptz not null default now(),

  constraint rag_chunks_ordinal_ck check (ordinal >= 0),
  constraint rag_chunks_section_ck check (length(btrim(section)) > 0),
  constraint rag_chunks_content_ck check (length(btrim(content)) > 0)
);

comment on table public.rag_chunks is
  'The retrievable unit. `section` is NOT NULL because CLAUDE.md §9 requires every answer to cite document AND section — a chunk that cannot be cited must not be retrievable.';
comment on column public.rag_chunks.is_commercial is
  'Chunk-level §4 wall. Text about money is still walled; it is NOT a system of record for the number (R1).';

create unique index rag_chunks_ordinal_uq on public.rag_chunks (document_id, ordinal);
create index rag_chunks_document_idx on public.rag_chunks (document_id);

-- =============================================================================
-- rag_embeddings — one row per (chunk, model)
-- =============================================================================
-- Split from rag_chunks rather than a column on it, for three reasons that all
-- turn up in the first month:
--
--   * Re-embedding under a new model becomes an INSERT beside the old vectors
--     instead of a destructive UPDATE across the whole corpus, so retrieval keeps
--     working while the backfill runs and a bad model is a DELETE to roll back.
--   * The dimension is per model. Pinning one `vector(n)` on the chunk makes the
--     next model a table rewrite.
--   * `authenticated` can be denied this table wholesale while still reading
--     chunk text under policy. A 1536-dimensional embedding is not the text, but
--     it is enough of it to be worth not handing to the browser — and nothing in
--     the UI needs it, because the search runs server-side in rag_search().
--
-- 1536 is text-embedding-3-small's dimension and the ceiling here is pgvector's
-- 2000-dimension limit for indexed columns. `dim` is stored explicitly so a
-- mismatched vector is caught by the CHECK rather than by a silently wrong
-- distance.
create table public.rag_embeddings (
  chunk_id   uuid not null references public.rag_chunks(id) on delete cascade,

  -- The model that produced the vector. Part of the key, because two models'
  -- vectors are not comparable and mixing them in one nearest-neighbour scan
  -- produces plausible, wrong neighbours — the worst failure mode retrieval has.
  model      text not null,
  dim        integer not null,
  embedding  vector(1536) not null,

  created_at timestamptz not null default now(),

  primary key (chunk_id, model),
  constraint rag_embeddings_model_ck check (length(btrim(model)) > 0),
  constraint rag_embeddings_dim_ck   check (dim = 1536)
);

comment on table public.rag_embeddings is
  'One vector per (chunk, model). Split from rag_chunks so a re-embed is additive, and so `authenticated` can be denied vectors while still reading chunk text.';
comment on column public.rag_embeddings.model is
  'Part of the primary key: two models'' vectors are not comparable, and mixing them yields plausible wrong neighbours.';

-- HNSW over cosine distance. `vector_cosine_ops` because the embedder normalises
-- (`app/rag/embeddings.py`), so cosine and inner product agree and cosine is the
-- one whose numbers a human can read: 1.0 identical, 0.0 unrelated.
--
-- HNSW rather than IVFFlat: IVFFlat's recall depends on a `lists` parameter
-- tuned to a row count this corpus does not have yet, and an under-tuned IVFFlat
-- silently misses the right chunk. A missed chunk here does not look like a bug;
-- it looks like the Copilot not knowing something, which is exactly the failure
-- §13 says destroys trust once and permanently.
create index rag_embeddings_hnsw
  on public.rag_embeddings
  using hnsw (embedding vector_cosine_ops);

-- =============================================================================
-- Reach helpers
-- =============================================================================
-- SECURITY DEFINER + STABLE + `set search_path = ''`, per 0200's technique 1.
-- Definer matters here for the same reason it did in 1300: these read
-- `rag_corpus_access` and `rag_documents`, and as invoker they would be filtered
-- by those tables' own policies — giving an answer that happens to be right
-- today by a mechanism that inverts the moment a policy widens.
--
-- Kept SEPARATE rather than fused into one convenient `can_see_chunk()`, so
-- every policy below keeps the house shape and the wall stays visible in the
-- policy text:
--     using (<the wall> and <the scope>)

-- Does the caller's persona hold this corpus at all? Pure ACL lookup; says
-- nothing about reach and nothing about commercials.
create or replace function public.can_read_corpus(p_corpus public.rag_corpus)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1
    from public.rag_corpus_access a
    where a.corpus = p_corpus
      and a.role   = public.app_role()
  );
$$;

comment on function public.can_read_corpus(public.rag_corpus) is
  'ACL half of the §9 persona filter: does this persona hold this corpus. Deny by default — a persona with no row in rag_corpus_access reads zero chunks.';

-- Pure reach for one document row. A document with no college and no program is
-- organisation-wide (an SOP); anything narrower resolves through the same
-- assignment graph every other table uses.
--
-- Deliberately NOT an admin override. 0700's header: is_admin() is the right to
-- hand out reach, it is not itself reach.
create or replace function public.can_reach_rag_document(p_document_id uuid)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1
    from public.rag_documents d
    where d.id = p_document_id
      and (d.college_id is null or public.can_reach_college(d.college_id))
      and (d.program_id is null or public.can_reach_program(d.program_id))
  );
$$;

comment on function public.can_reach_rag_document(uuid) is
  'Scope half of the rag policies. NULL college/program means organisation-wide. No admin override — is_admin() is the right to grant reach, not reach itself (0700).';

-- Wall half, for a chunk: is this row behind the §4 commercials wall? TRUE if
-- either the chunk or its document says so. Fails CLOSED on an unresolvable
-- chunk id — an unknown row is treated as commercial, so the narrower policy
-- applies. Same construction, and the same reasoning, as
-- `artifact_is_commercial()` in 1300.
create or replace function public.rag_chunk_is_commercial(p_chunk_id uuid)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select coalesce(
    (
      select c.is_commercial or d.is_commercial
      from public.rag_chunks c
      join public.rag_documents d on d.id = c.document_id
      where c.id = p_chunk_id
    ),
    true
  );
$$;

comment on function public.rag_chunk_is_commercial(uuid) is
  'True when a chunk sits behind the §4 wall — chunk flag OR document flag. Fails closed on an unresolvable id.';

revoke execute on function public.can_read_corpus(public.rag_corpus)   from public, anon;
revoke execute on function public.can_reach_rag_document(uuid)         from public, anon;
revoke execute on function public.rag_chunk_is_commercial(uuid)        from public, anon;

grant execute on function public.can_read_corpus(public.rag_corpus)    to authenticated;
grant execute on function public.can_reach_rag_document(uuid)          to authenticated;
grant execute on function public.rag_chunk_is_commercial(uuid)         to authenticated;

-- =============================================================================
-- rag_search() — the ONLY way to get neighbours out of this schema
-- =============================================================================
-- This function is §9's "persona filter applies BEFORE retrieval, not after
-- generation", made structural rather than procedural.
--
-- The property being bought: there is no callable path that returns chunks
-- ordered by similarity WITHOUT stating who is asking. Every persona input is a
-- required parameter with no default, so "retrieve now, filter later" is not a
-- shortcut somebody can take under deadline — it does not typecheck. The ORDER
-- BY and the LIMIT sit *inside* the same statement as the filter, which is the
-- part that actually matters: a filter applied to an already-truncated top-k has
-- silently dropped the permitted chunks that the forbidden ones outranked, so
-- filtering late is not merely unsafe, it is also wrong.
--
-- WHY THE PERSONA ARRIVES AS PARAMETERS AND NOT FROM auth.uid()
-- -------------------------------------------------------------
-- Because the caller is `app/rag/retrieval.py` on the FastAPI service-role
-- connection, where `auth.uid()` is NULL and every RLS policy steps aside
-- (`app/core/security.py`). A function that read the session's identity would be
-- reading NOBODY on the one connection that actually runs this query, and would
-- then have to choose between failing closed (Copilot never works) and failing
-- open (Copilot answers everything to everyone). So the app passes the resolved
-- principal in, and `RetrievalScope` — the only type that can produce these
-- arguments — is constructible only from a `Principal`.
--
-- SECURITY INVOKER, deliberately. Called from the browser as `authenticated`,
-- the RLS policies below ALSO apply and the two walls agree. Called from
-- FastAPI, RLS is bypassed and these parameters are the wall. Making it DEFINER
-- would remove the second wall for no gain.
--
-- WHAT IT REFUSES TO DO: it does not return an amount, a date or a count. It
-- returns text and its provenance (R1, §9). If a caller wants a number it must
-- query the system of record; `app/rag/guards.py` refuses the question before it
-- ever reaches here.
create or replace function public.rag_search(
  -- The query vector, as pgvector's text form (`[0.1,0.2,...]`). Text rather
  -- than `vector` so no client-side driver needs to know the extension type, and
  -- so the cast happens inside a function that has the right search_path.
  p_query_embedding     text,
  p_model               text,
  -- Which corpora to search. Intersected with the caller's ACL, never trusted:
  -- a caller asking for `contracts` they do not hold gets zero rows from that
  -- corpus, not an error, because an error would confirm the corpus exists.
  p_corpora             public.rag_corpus[],
  -- The resolved principal. No defaults: see the block comment.
  p_is_internal         boolean,
  p_can_see_commercials boolean,
  p_role                public.app_role,
  p_college_ids         uuid[],
  p_limit               integer,
  -- §9's version flag. FALSE is the default posture; a caller doing contract
  -- archaeology passes TRUE and gets `is_superseded` on every row either way.
  p_include_superseded  boolean default false
)
returns table (
  chunk_id      uuid,
  document_id   uuid,
  corpus        public.rag_corpus,
  title         text,
  section       text,
  content       text,
  version       integer,
  is_superseded boolean,
  is_commercial boolean,
  similarity    double precision
)
language sql
stable
security invoker
set search_path = public, extensions
as $$
  select
    c.id,
    d.id,
    d.corpus,
    d.title,
    c.section,
    c.content,
    d.version,
    d.superseded_at is not null,
    c.is_commercial or d.is_commercial,
    (1 - (e.embedding <=> p_query_embedding::vector(1536)))::double precision
  from public.rag_chunks c
  join public.rag_documents  d on d.id = c.document_id
  join public.rag_embeddings e on e.chunk_id = c.id and e.model = p_model
  where
    -- THE PERSONA FILTER. Every conjunct below runs before the ORDER BY.
    p_is_internal
    -- The corpus ACL, resolved against the passed role rather than the session's
    -- — the session has no role on the service connection.
    and exists (
      select 1 from public.rag_corpus_access a
      where a.corpus = d.corpus and a.role = p_role
    )
    and d.corpus = any (p_corpora)
    -- §4's wall. A commercial row is invisible unless the caller is inside it.
    and (p_can_see_commercials or not (c.is_commercial or d.is_commercial))
    -- Reach. NULL scope is organisation-wide; anything else must be in the
    -- caller's college set. `p_college_ids` is the app-side value of
    -- my_college_ids(), computed in app/core/security.py from the same two
    -- assignment tables the SQL helper reads.
    and (d.college_id is null or d.college_id = any (p_college_ids))
    and (
      d.program_id is null
      or exists (
        select 1 from public.programs p
        where p.id = d.program_id and p.college_id = any (p_college_ids)
      )
    )
    -- §9's version flag.
    and (p_include_superseded or d.superseded_at is null)
  order by e.embedding <=> p_query_embedding::vector(1536)
  limit greatest(p_limit, 0);
$$;

comment on function public.rag_search is
  'The only similarity search over the corpora. CLAUDE.md §9: the persona filter is in the same statement as the ORDER BY and LIMIT, so retrieval cannot precede filtering. Returns text and provenance only — never a fact (R1).';

revoke execute on function public.rag_search(
  text, text, public.rag_corpus[], boolean, boolean, public.app_role, uuid[], integer, boolean
) from public, anon;
grant execute on function public.rag_search(
  text, text, public.rag_corpus[], boolean, boolean, public.app_role, uuid[], integer, boolean
) to authenticated;

-- =============================================================================
-- RLS
-- =============================================================================
-- FORCE, so even the table owner is subject to policy (0200, technique 2). It
-- does not stop the service-role connection — nothing in SQL does — which is why
-- `app/rag/retrieval.py` re-applies the same predicates in code. Two walls, one
-- vocabulary.

alter table public.rag_corpus_access enable row level security;
alter table public.rag_corpus_access force  row level security;
alter table public.rag_documents     enable row level security;
alter table public.rag_documents     force  row level security;
alter table public.rag_chunks        enable row level security;
alter table public.rag_chunks        force  row level security;
alter table public.rag_embeddings    enable row level security;
alter table public.rag_embeddings    force  row level security;

-- Supabase's DEFAULT PRIVILEGES grant `authenticated` all four verbs on any new
-- table in `public`, so every revoke below names it explicitly. 1300 learned
-- this the hard way — before that file named `authenticated`, an LDE Executive
-- could DELETE artifact_versions through PostgREST. Do not "tidy" these to
-- `from public, anon`.

-- --- rag_corpus_access -------------------------------------------------------
-- Readable by internal staff, writable by nobody through PostgREST. It is the
-- ACL: a persona that could INSERT into it could grant itself Contracts. Changes
-- are migrations, under review, on the record.
revoke all on public.rag_corpus_access from public, anon, authenticated;
grant select on public.rag_corpus_access to authenticated;

create policy rag_corpus_access_internal_select on public.rag_corpus_access
  for select to authenticated
  using (public.is_internal());

-- --- rag_documents -----------------------------------------------------------
-- SELECT only. Ingestion is a service-role job (`app/rag/ingest.py`): documents
-- arrive from Supabase Storage and are chunked and embedded server-side, so
-- there is no browser write path to authorise, and a corpus that can be written
-- from the browser is a corpus into which anyone can plant an authoritative
-- looking SOP. §9's answers cite these rows; a forged row is a forged citation.
revoke all on public.rag_documents from public, anon, authenticated;
grant select on public.rag_documents to authenticated;

create policy rag_documents_read on public.rag_documents
  for select to authenticated
  using (
    public.is_internal()
    and public.can_read_corpus(corpus)
    and (public.can_see_commercials() or not is_commercial)
    and (college_id is null or public.can_reach_college(college_id))
    and (program_id is null or public.can_reach_program(program_id))
  );

-- --- rag_chunks --------------------------------------------------------------
-- Same shape, resolved through the parent document. The chunk-level commercial
-- flag is OR'd in via rag_chunk_is_commercial(), which is what makes a margin
-- paragraph inside an otherwise-readable governance report invisible to an LDE
-- Executive.
revoke all on public.rag_chunks from public, anon, authenticated;
grant select on public.rag_chunks to authenticated;

create policy rag_chunks_read on public.rag_chunks
  for select to authenticated
  using (
    public.is_internal()
    and (public.can_see_commercials() or not public.rag_chunk_is_commercial(id))
    and public.can_reach_rag_document(document_id)
    and exists (
      select 1 from public.rag_documents d
      where d.id = document_id and public.can_read_corpus(d.corpus)
    )
  );

-- --- rag_embeddings ----------------------------------------------------------
-- NO grant and NO policy, for anyone. Not an oversight.
--
-- An embedding is not the text, but it is a lossy encoding of it, and inversion
-- attacks on sentence embeddings recover a usable paraphrase. Handing the
-- browser the vectors for the Contracts corpus would hand it a blurred copy of
-- the contracts, past a wall that the chunk policy above is carefully enforcing
-- three lines up. Nothing in the UI needs them: the similarity search runs
-- server-side in rag_search(), and the browser receives text plus citations.
--
-- `service_role` is unaffected, which is the ingestion and retrieval path.
revoke all on public.rag_embeddings from public, anon, authenticated;

-- NO trainer policy and NO college policy on any table in this file. §4 gives a
-- trainer "own deployment, tracksheet, invoice status. Nothing else" and a
-- college "published artifacts only, read-only". A similarity search over the
-- internal corpora is neither. Both personas also hold no rag_corpus_access row,
-- so the denial is two layers deep — R5 wants a test asserting zero rows, and
-- tests/unit/test_rag_retrieval.py asserts it on the app side where the
-- BYPASSRLS connection makes it load-bearing.

-- =============================================================================
-- FOLLOW-UPS OWNED BY OTHER FILES — recorded here so they are not lost
-- =============================================================================
-- 1. supabase/tests/00_isolate.sql — four new tables. Add, before the
--    `delete from public.profiles` line:
--
--        delete from public.rag_embeddings;
--        delete from public.rag_chunks;
--        delete from public.rag_documents;
--
--    NOT rag_corpus_access: it is seeded by this migration and is reference
--    data, like task_templates. Deleting it would make every corpus unreadable
--    for the duration of the run and every §9 assertion would pass vacuously.
--
-- 2. supabase/tests/02_rls_matrix_test.sql — R5's per-boundary assertions. With
--    one commercial and one non-commercial document fixture per corpus:
--
--        select test.assert_count('WALL: LDE-A1 sees NO contracts chunks',
--          'select count(*) from public.rag_chunks c
--             join public.rag_documents d on d.id = c.document_id
--            where d.corpus = ''contracts''', 0);
--        select test.assert_count('WALL: LDE-A1 sees NO commercial chunks',
--          'select count(*) from public.rag_chunks where is_commercial', 0);
--        select test.assert_count('LDE-A1 DOES see SOP chunks',
--          'select count(*) from public.rag_chunks c
--             join public.rag_documents d on d.id = c.document_id
--            where d.corpus = ''sop''', <fixture count>);
--        select test.assert_count('TRAINER sees NO chunks at all',
--          'select count(*) from public.rag_chunks', 0);
--        select test.assert_count('Nobody reads raw embeddings',
--          'select count(*) from public.rag_embeddings', 0);  -- errors: no grant
-- =============================================================================
