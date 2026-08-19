"""Load markdown documents into a RAG corpus, and take them out again.

    python tools/ingest_docs.py --ingest docs/corpus/sop --corpus sop
    python tools/ingest_docs.py --status
    python tools/ingest_docs.py --delete docs/corpus/sop --corpus sop

The six corpora shipped empty. `app/rag/` has a chunker, an ingestor, an
embedder and a retriever, all reachable, and `rag_documents` was 0 — so the Ops
Copilot refused every question with `no_sources`. This is the missing edge: a
file on disk becomes a citable, embedded, persona-filtered row.

WHICH CREDENTIAL THIS USES, AND WHY THAT IS RIGHT
=================================================
The same one the FastAPI service uses: `DATABASE_URL` through
`app/db/session.py`, which carries **BYPASSRLS** (that module's docstring says
so in its first line). It has to. Migration 1600 does
`revoke all on public.rag_documents from public, anon, authenticated` and then
grants back **select only**; `rag_embeddings` gets no grant to anybody at all.
There is no INSERT privilege on any `rag_*` table for any browser-reachable
role, deliberately — "a corpus that can be written from the browser is a corpus
into which anyone can plant an authoritative-looking SOP, and §9's answers cite
these rows, so a forged row is a forged citation."

So ingestion is service-role by construction, and this script is the operator
holding that credential. R5 is not weakened by that: R5 is about what a *reader*
can see, and every read path — `rag_search()`, `RagRetriever`, the policies in
1600 — is untouched here. What this script must not do is decide who may read
what, and it does not: the commercial gate below **reads `rag_corpus_access`**
rather than hardcoding the ACL, so the wall it enforces is the wall the database
holds (R1 — the database owns truth).

SECTIONS ARE THE WHOLE POINT (§9)
=================================
    "Every answer cites source document and section. No citation → no answer."

`app/rag/chunking.py` already derives sections from markdown ATX headings and
numbered clause headings, and `rag_chunks.section` is NOT NULL. What it cannot
do is refuse a document — it is a pure function, so text with no headings at all
comes back as one section called `Document`, which satisfies the NOT NULL and
names no findable place inside the file. A citation reading
"Payout SOP — Document" is a citation in shape only.

This script therefore pre-flights the chunking and **refuses a document whose
chunks all land in `ROOT_SECTION`** — a markdown file with no headings is not
ingestible, and the fix is to add headings, not to relax the rule. A document
that *does* have headings but carries a preamble before the first one is
ingested with a warning: `chunking.py` keeps that preamble on purpose (it is
frequently the definitions clause) and dropping it would make defined terms
uncitable, so it is reported, not discarded.

The document half of the citation is derived too: `title` comes from the file's
first `# ` heading, falling back to the filename stem.

THE COMMERCIALS GATE (§4)
=========================
`looks_commercial()` in `app/rag/ingest.py` is a regex over currency and rate
tokens, and that module is candid about it: "the cheapest one is also the least
reliable", a third layer behind the document flag and the corpus ACL.

That matters here because the corpora are not equally walled.
`rag_corpus_access` grants `sop`, `college_dossier`, `curriculum` and `reports`
to `lde_executive` — a persona §4 gives **no commercials at all**. Put a
money-quoting document in one of those and the only thing standing between an
LDE Executive and a rate card is a regex.

So: when a document has any commercial chunk **and** the target corpus is
readable by a persona outside `COMMERCIALS_PERSONAS`, this script refuses until
a human states which wall they meant:

  --commercial document   flag the whole document; every chunk inherits it and
                          an LDE Executive sees none of it. The safe answer for
                          a rate card, an MoU extract, a payout schedule.
  --commercial chunks     flag only the chunks the regex catches. This is the
                          `reports` case the ACL describes in as many words —
                          "a governance report is mostly delivery narrative and
                          walling the whole corpus would cost the LDE Executive
                          the part that is theirs".

`contracts` needs neither: the ACL walls that corpus wholesale, and documents
there are flagged commercial automatically as the belt to those braces.

DELETION IS EXACT
=================
`--delete` addresses rows by `(corpus, source_ref)` — the same pairs `--ingest`
of the same path would produce. Never a `LIKE '%test%'`, never a date sweep,
which is the cleanup that eventually takes a real row with it and teaches people
to stop running cleanup. `source_ref` is the repo-relative POSIX path, so the
corpus and the path together are the namespace: a throwaway corpus under
`docs/corpus/scratch/` comes out with one command and nothing else is addressed.
`--source-ref` removes a document whose file is already gone, still by exact id.

`ingested_by` is left NULL. A CLI run has no authenticated principal, and
writing an operator's uuid into a provenance column on nothing but their say-so
would be a fabricated audit trail rather than a real one.

ONE UPSTREAM DEFECT, AND ONE TRAP NEXT TO IT
============================================
Both live in code this script does not own.

**F-A — `RagIngestor` cannot write to a real database through a plain session.**
Reproduced, not assumed: a `RagIngestor` on a bare `AsyncSession` raises

    psycopg.errors.ForeignKeyViolation: insert or update on table "rag_chunks"
    violates foreign key constraint "rag_chunks_document_id_fkey"

This is why the six corpora were empty. Detail:
`ingest()` adds a `RagDocument`, then its `RagChunk`s, then their
`RagEmbedding`s, and never flushes. `app/db/models.py` declares no
`relationship()` between them, and SQLAlchemy's unit of work orders inserts from
relationship dependencies — a bare `ForeignKey` column is invisible to that
sort. The flush emits `rag_chunks` first and Postgres rejects it with
`ForeignKeyViolation on rag_chunks_document_id_fkey`. `_OrderedFlush` below
releases the three types in dependency order. `tests/rag_eval/conftest.py` hit
the same wall and built the same shim; two independent workarounds for one
defect is the signal that the fix belongs upstream — a `flush()` after each type
inside `RagIngestor.ingest()`, or declared relationships.

**F-B — a latent trap around the embedding model name. NOT a live defect.**
The gateway does not echo the name you send it: ask OpenRouter for
`openai/text-embedding-3-small` and the response body says
`text-embedding-3-small`. `OpenRouterEmbedder.embed()` then does
`self._model = response.model`, which reads like it adopts that echo — and if it
did, idempotency would be dead. `RagIngestor` reads `embedder.model` *after*
embedding (so rows would store the echo) but `_embed_missing()` queries
`RagEmbedding.model == embedder.model` *before* embedding (so it would look up
the requested name). Second run of an unchanged file: the join misses, every
chunk looks un-embedded, and the re-insert collides with the existing primary
key `(chunk_id, model)`.

It does not happen, for one reason: `app/core/llm.py` builds
`EmbeddingResponse(model=resolved)` — the name the CALLER asked for — and throws
the gateway's echo away. That discard is load-bearing and nowhere marked as
such. "Fix" `EmbeddingResponse.model` to report what the gateway actually served
and the whole corpus re-embeds on the next cron run and then dies on a duplicate
key. Verified both halves against the live API before writing this down.

`resolve_embedder()` embeds one probe string at startup and adopts whatever name
comes back, so this script is correct under either behaviour and says so out
loud when the two disagree. The probe is also requirement 4's verification:
nothing is written until a real vector of the right width has come back.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import selectors
import sys
import uuid
from collections.abc import Awaitable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TypeVar

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:  # `python tools/ingest_docs.py` from anywhere
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import bindparam  # noqa: E402
from sqlalchemy import text as sql  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.core.llm import EMBEDDING_DIM, EMBEDDING_MODEL_ENV, LLMClient  # noqa: E402
from app.db.models import RagChunk, RagDocument, RagEmbedding  # noqa: E402
from app.db.session import dispose_engine, get_sessionmaker  # noqa: E402
from app.domain.enums import COMMERCIALS_PERSONAS, Corpus, Persona  # noqa: E402
from app.rag.chunking import ROOT_SECTION, Chunk, chunk_document, normalise  # noqa: E402
from app.rag.embeddings import OpenRouterEmbedder  # noqa: E402
from app.rag.ingest import DocumentSpec, RagIngestor, looks_commercial  # noqa: E402

_T = TypeVar("_T")

#: Extensions accepted as "a markdown document". One document per file.
MARKDOWN_SUFFIXES: Final[frozenset[str]] = frozenset({".md", ".markdown"})

#: Corpora whose ACL admits nobody without commercials rights, so a document
#: landing there is already walled corpus-wide. `contracts` additionally gets the
#: document-level flag set, per the `rag_documents.is_commercial` comment in 1600
#: ("belt to the Contracts corpus ACL's braces").
ALWAYS_COMMERCIAL: Final[frozenset[Corpus]] = frozenset({Corpus.CONTRACTS})

#: The first ATX H1 in a file, which becomes the document half of a citation.
_H1_RE: Final[re.Pattern[str]] = re.compile(r"^#\s+(?P<title>\S.*?)\s*#*$", re.M)

#: One short string, embedded before anything is written, to prove the model
#: answers and to learn the name it answers under (F-B above).
_PROBE_TEXT: Final[str] = "byteXL ops corpus ingestion preflight"


def say(message: str) -> None:
    print(f"  {message}")


def lenient_stdout() -> None:
    """Never let a document title kill an ingest run.

    Section titles and document titles come out of the corpus files, and a real
    SOP heading contains an em dash, a rupee sign or a Devanagari college name
    sooner or later. On Windows `sys.stdout` defaults to the console codepage
    (cp1252 here) and `print()` raises `UnicodeEncodeError` on anything outside
    it — which would abort a batch ingest partway on a *display* problem, after
    it had already spent embeddings. Degrade the character, not the run.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")


# --- environment --------------------------------------------------------------


def load_env() -> dict[str, str]:
    """Read `.env` into `os.environ` (without overriding a real variable).

    `app/core/config.py` reads `.env` through pydantic-settings, which does NOT
    populate `os.environ` — and `LLMClient.embed()` resolves the embedding model
    from `os.environ` specifically. So a `.env` entry alone would not reach it.
    `setdefault` rather than assignment so an explicitly exported variable still
    wins, which is how CI overrides the DSN.
    """
    values = dict(os.environ)
    env_file = REPO_ROOT / ".env"
    if env_file.exists():
        for key, value in re.findall(
            r"^([A-Z][A-Z0-9_]*)=(.*)$", env_file.read_text(encoding="utf-8"), re.M
        ):
            values.setdefault(key, value.strip())
            os.environ.setdefault(key, value.strip())
    if not values.get("DATABASE_URL"):
        sys.exit("DATABASE_URL is missing from the environment and from .env")
    return values


def _loop_factory() -> asyncio.AbstractEventLoop:
    """A selector loop, which psycopg's async driver supports on every platform.

    Same reason as `run_api.py`: on Windows the default `ProactorEventLoop` makes
    every query raise `psycopg.InterfaceError`.
    """
    return asyncio.SelectorEventLoop(selectors.SelectSelector())


def run_async(coro: Awaitable[_T]) -> _T:
    factory = _loop_factory if sys.platform == "win32" else None
    with asyncio.Runner(loop_factory=factory) as runner:
        return runner.run(coro)


# --- reading the files --------------------------------------------------------


def markdown_files(path: Path) -> list[Path]:
    """Every markdown file at `path`, sorted. A file is itself; a directory recurses.

    Sorted because ingestion order decides nothing about the result but decides
    everything about whether two runs read the same in a log.
    """
    if path.is_dir():
        return sorted(p for p in path.rglob("*") if p.suffix.lower() in MARKDOWN_SUFFIXES)
    if not path.exists():
        sys.exit(f"no such path: {path}")
    if path.suffix.lower() not in MARKDOWN_SUFFIXES:
        sys.exit(f"not a markdown file: {path} (accepted: {', '.join(sorted(MARKDOWN_SUFFIXES))})")
    return [path]


def source_ref_for(path: Path) -> str:
    """The stable identity of a document ACROSS versions.

    Repo-relative POSIX path when the file is inside the repo, absolute POSIX
    otherwise. POSIX either way so a Windows ingest and a Linux ingest of the
    same file are the same document rather than two.
    """
    resolved = path.resolve()
    try:
        return resolved.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return resolved.as_posix()


def title_for(text: str, path: Path) -> str:
    """The document half of a §9 citation: the first H1, else the filename stem."""
    match = _H1_RE.search(normalise(text))
    if match is not None:
        return match.group("title").strip()
    return path.stem


# --- planning: everything that can refuse, before anything is written ---------


@dataclass(frozen=True, slots=True)
class Refusal:
    """A document that will not be ingested, and why."""

    path: Path
    reason: str
    remedy: str


@dataclass(frozen=True, slots=True)
class Plan:
    """One document, chunked and classified, ready to write."""

    path: Path
    source_ref: str
    title: str
    text: str
    chunks: tuple[Chunk, ...]
    is_commercial: bool
    warnings: tuple[str, ...]

    @property
    def sections(self) -> tuple[str, ...]:
        """Distinct section titles, in document order."""
        seen: list[str] = []
        for chunk in self.chunks:
            if chunk.section not in seen:
                seen.append(chunk.section)
        return tuple(seen)

    @property
    def unsectioned(self) -> tuple[Chunk, ...]:
        """Chunks that can only cite as `Document` — preamble before the first heading."""
        return tuple(c for c in self.chunks if c.section == ROOT_SECTION)

    @property
    def commercial_chunks(self) -> tuple[Chunk, ...]:
        return tuple(c for c in self.chunks if looks_commercial(c.content))


def open_roles_for(granted: Iterable[Persona]) -> frozenset[Persona]:
    """Of the personas granted this corpus, those §4 gives no commercials.

    Non-empty means the corpus is readable by someone who must never see a rate,
    which is what makes the gate in `plan_document()` bite.
    """
    return frozenset(granted) - COMMERCIALS_PERSONAS


def plan_document(
    path: Path,
    text: str,
    corpus: Corpus,
    granted: Iterable[Persona],
    commercial_mode: str | None,
) -> Plan | Refusal:
    """Chunk, classify and decide. Pure — no I/O, so the gates are unit-testable."""
    chunks = chunk_document(normalise(text))
    if not chunks:
        return Refusal(
            path,
            "empty document — chunking produced nothing",
            "a document with no text cannot be retrieved or cited; remove it or write it",
        )

    plan = Plan(
        path=path,
        source_ref=source_ref_for(path),
        title=title_for(text, path),
        text=text,
        chunks=chunks,
        is_commercial=False,
        warnings=(),
    )

    # --- §9: a chunk that cannot name a section cannot be cited ---------------
    if len(plan.unsectioned) == len(chunks):
        return Refusal(
            path,
            f"no headings — all {len(chunks)} chunks would cite as "
            f"'{plan.title} — {ROOT_SECTION}'",
            "CLAUDE.md §9 requires a citation to name a section. Add '## ' headings; "
            "chunking.py derives sections from them and rag_chunks.section is NOT NULL",
        )

    warnings: list[str] = []
    if plan.unsectioned:
        warnings.append(
            f"{len(plan.unsectioned)} chunk(s) sit before the first heading and will cite as "
            f"'{ROOT_SECTION}'. Retained on purpose (chunking.py keeps a preamble because it "
            "is frequently the definitions clause), but they name no findable place"
        )

    # --- §4: the commercials wall --------------------------------------------
    commercial = plan.commercial_chunks
    open_roles = open_roles_for(granted)
    is_commercial = corpus in ALWAYS_COMMERCIAL or commercial_mode == "document"

    if commercial and open_roles and commercial_mode is None:
        roles = ", ".join(sorted(r.value for r in open_roles))
        return Refusal(
            path,
            f"{len(commercial)} of {len(chunks)} chunks quote money, and corpus "
            f"'{corpus.value}' is readable by {roles} — personas CLAUDE.md §4 gives "
            "NO commercials",
            "state the wall you mean: --commercial document (wall the whole document) "
            "or --commercial chunks (wall only the flagged chunks, the 'reports' case). "
            f"A commercials-only corpus (contracts, educator) needs neither. Sections "
            f"that trip the check: {_join_sections(commercial)}",
        )

    if commercial and not is_commercial:
        warnings.append(
            f"{len(commercial)} of {len(chunks)} chunks flagged commercial and walled "
            f"individually: {_join_sections(commercial)}"
        )
    if is_commercial:
        warnings.append(
            "whole document flagged commercial — invisible to every persona without "
            "can_see_commercials()"
            + (" (corpus ACL already walls it)" if corpus in ALWAYS_COMMERCIAL else "")
        )

    return Plan(
        path=plan.path,
        source_ref=plan.source_ref,
        title=plan.title,
        text=plan.text,
        chunks=chunks,
        is_commercial=is_commercial,
        warnings=tuple(warnings),
    )


def _join_sections(chunks: Sequence[Chunk], limit: int = 4) -> str:
    seen: list[str] = []
    for chunk in chunks:
        if chunk.section not in seen:
            seen.append(chunk.section)
    head = "; ".join(s[:48] for s in seen[:limit])
    return head + (f"; +{len(seen) - limit} more" if len(seen) > limit else "")


# --- the ordered-flush shim (F-A) --------------------------------------------


class _OrderedFlush:
    """Session proxy that releases rag_documents → rag_chunks → rag_embeddings.

    A WORKAROUND, not a convenience. See F-A in the module docstring: with no
    `relationship()` declared, SQLAlchemy's unit of work has no dependency edge
    between these three mappers and emits the child INSERT first.

    The leading `flush()` in `sequence()` is load-bearing for the supersede path:
    stamping `superseded_at` on the old contract row is an UPDATE that must land
    before the new version's INSERT, or the partial unique index
    `rag_documents_one_current` rejects the pair.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._staged: list[object] = []

    def add(self, obj: object) -> None:
        self._staged.append(obj)

    async def execute(self, *args: object, **kwargs: object):  # noqa: ANN201, ANN401
        return await self._session.execute(*args, **kwargs)

    async def sequence(self) -> None:
        await self._session.flush()
        for kind in (RagDocument, RagChunk, RagEmbedding):
            batch = [o for o in self._staged if isinstance(o, kind)]
            if batch:
                self._session.add_all(batch)
                await self._session.flush()
        self._staged.clear()


# --- the embedder (F-B, and requirement 4's proof) ---------------------------


@dataclass(frozen=True, slots=True)
class EmbedderCheck:
    """What the startup probe learned. Printed, so a run states its own model."""

    requested: str
    canonical: str
    dim: int

    @property
    def drifted(self) -> bool:
        return self.requested != self.canonical


async def resolve_embedder(
    requested: str | None, *, client: object | None = None
) -> tuple[OpenRouterEmbedder, EmbedderCheck]:
    """Build the production embedder and prove it works before anything is written.

    Names the model explicitly — `app/core/llm.py` refuses to default one, and it
    is right to: "a defaulted embedding model produces a corpus whose vectors
    were computed under one model and queried under another, which returns
    confident, wrong neighbours."
    """
    model = (requested or os.environ.get(EMBEDDING_MODEL_ENV, "")).strip()
    if not model:
        sys.exit(
            f"No embedding model. {EMBEDDING_MODEL_ENV} is unset in the environment and in "
            ".env, and app/core/llm.py deliberately has no default.\n"
            "  Pass --embedding-model openai/text-embedding-3-small, or add\n"
            f"    {EMBEDDING_MODEL_ENV}=openai/text-embedding-3-small\n"
            "  to .env. It must be a 1536-dimension model: rag_embeddings.embedding is\n"
            f"  vector({EMBEDDING_DIM}) and the width is a migration, not a config change."
        )

    llm = client if client is not None else LLMClient()
    embedder = OpenRouterEmbedder(llm, model=model)  # type: ignore[arg-type]
    vectors = await embedder.embed([_PROBE_TEXT])
    if len(vectors) != 1 or len(vectors[0]) != EMBEDDING_DIM:
        sys.exit(
            f"Embedding probe returned {len(vectors)} vector(s) of width "
            f"{len(vectors[0]) if vectors else 0}; expected 1 of {EMBEDDING_DIM}."
        )
    # After one round trip `embedder.model` is whatever name the stack decided to
    # keep — today the requested one, because `app/core/llm.py` discards the
    # gateway's echo (F-B). Reading it back here rather than assuming makes this
    # script correct either way, and makes a change in that behaviour visible in
    # the run output instead of at 3am on a duplicate key.
    return embedder, EmbedderCheck(requested=model, canonical=embedder.model, dim=len(vectors[0]))


# --- corpus ACL, read from the database (R1) ---------------------------------


async def granted_roles(session: AsyncSession, corpus: Corpus) -> frozenset[Persona]:
    """Which personas `rag_corpus_access` grants this corpus.

    Read, not hardcoded. The ACL is a table precisely so "who can read Contracts?"
    is answerable by SELECT, and a CLI that carried its own copy would be a second
    source of truth that drifts from the one the policies use.
    """
    rows = await session.execute(
        sql("select role::text from public.rag_corpus_access where corpus = :corpus"),
        {"corpus": corpus.value},
    )
    return frozenset(Persona(value) for (value,) in rows.all())


# --- ingest -------------------------------------------------------------------


async def ingest(
    path: Path,
    corpus: Corpus,
    *,
    commercial_mode: str | None,
    college_id: uuid.UUID | None,
    program_id: uuid.UUID | None,
    embedding_model: str | None,
    dry_run: bool,
) -> int:
    files = markdown_files(path)
    if not files:
        sys.exit(f"no markdown files under {path}")

    print(f"\nIngest -> corpus '{corpus.value}'\n" + "-" * 72)

    factory = get_sessionmaker()
    async with factory() as session:
        granted = await granted_roles(session, corpus)
        if not granted:
            sys.exit(
                f"corpus '{corpus.value}' is granted to nobody in rag_corpus_access; "
                "ingesting into it would index text no persona can retrieve"
            )
        open_roles = open_roles_for(granted)
        say(f"ACL: {', '.join(sorted(r.value for r in granted))}")
        say(
            "commercials-walled corpus (no persona here lacks commercials)"
            if not open_roles
            else "readable without commercials by: "
            + ", ".join(sorted(r.value for r in open_roles))
        )

        # --- plan every file first. All-or-nothing: `RagIngestor.ingest()` does
        # not commit for the same reason — "a half-ingested contract whose
        # clauses 1-6 are indexed and 7-12 are not is worse than no contract".
        plans: list[Plan] = []
        refusals: list[Refusal] = []
        for file in files:
            outcome = plan_document(
                file, file.read_text(encoding="utf-8"), corpus, granted, commercial_mode
            )
            if isinstance(outcome, Refusal):
                refusals.append(outcome)
            else:
                plans.append(outcome)

        print()
        for plan in plans:
            say(
                f"{plan.source_ref}  ->  {len(plan.chunks)} chunks in "
                f"{len(plan.sections)} sections"
            )
            say(f"    title: {plan.title}")
            for warning in plan.warnings:
                say(f"    WARN   {warning}")
        for refusal in refusals:
            say(f"REFUSED  {source_ref_for(refusal.path)}")
            say(f"    {refusal.reason}")
            say(f"    fix: {refusal.remedy}")

        if refusals:
            print("\n" + "-" * 72)
            print(f"{len(refusals)} document(s) refused. Nothing was written.")
            return 2

        if dry_run:
            print("\n" + "-" * 72)
            print("--dry-run: nothing written, no embeddings bought.")
            return 0

        embedder, check = await resolve_embedder(embedding_model)
        print()
        say(f"embedding model: {check.canonical} ({check.dim} dims) — probe returned a vector")
        if check.drifted:
            say(
                f"    requested '{check.requested}', stack reports '{check.canonical}'; "
                "storing and querying under the reported name so re-ingest stays idempotent"
            )

        staged = _OrderedFlush(session)
        ingestor = RagIngestor(staged, embedder)  # type: ignore[arg-type]
        written = unchanged = chunks = embeddings = 0
        print()
        for plan in plans:
            result = await ingestor.ingest(
                DocumentSpec(
                    corpus=corpus,
                    source_ref=plan.source_ref,
                    title=plan.title,
                    text=plan.text,
                    college_id=college_id,
                    program_id=program_id,
                    is_commercial=plan.is_commercial,
                    ingested_by=None,
                )
            )
            await staged.sequence()
            chunks += result.chunks_written
            embeddings += result.embeddings_written
            if result.unchanged:
                unchanged += 1
                say(f"unchanged  {plan.source_ref}  (v{result.version})")
            else:
                written += 1
                superseded = (
                    f"  supersedes {result.superseded_document_id}"
                    if result.superseded_document_id
                    else ""
                )
                say(
                    f"ingested   {plan.source_ref}  v{result.version}  "
                    f"{result.chunks_written} chunks  "
                    f"{result.embeddings_written} vectors{superseded}"
                )
        await session.commit()

    print("\n" + "-" * 72)
    print(f"{written} written, {unchanged} unchanged · {chunks} chunks · {embeddings} embeddings")
    return 0


# --- delete -------------------------------------------------------------------


async def delete(path: Path | None, corpus: Corpus, source_refs: Sequence[str]) -> int:
    """Remove documents by exact `(corpus, source_ref)`. Chunks and vectors cascade."""
    refs = list(source_refs)
    if path is not None:
        refs += [source_ref_for(p) for p in markdown_files(path)]
    if not refs:
        sys.exit("--delete needs a PATH or at least one --source-ref")

    print(f"\nDelete from corpus '{corpus.value}'\n" + "-" * 72)
    # `expanding=True` renders one bind per value — an explicit IN list rather
    # than an array parameter, so nothing depends on the driver's array
    # adaptation and the statement reads in a query log exactly as it ran.
    scope = bindparam("refs", expanding=True)
    factory = get_sessionmaker()
    async with factory() as session:
        counted = await session.execute(
            sql("""
                select d.source_ref, d.version, d.superseded_at is null as is_current,
                       count(c.id) as chunks
                  from public.rag_documents d
                  left join public.rag_chunks c on c.document_id = d.id
                 where d.corpus = :corpus and d.source_ref in :refs
                 group by d.id, d.source_ref, d.version, d.superseded_at
                 order by d.source_ref, d.version
                """).bindparams(scope),
            {"corpus": corpus.value, "refs": refs},
        )
        rows = counted.all()
        for source_ref, version, is_current, chunk_count in rows:
            flag = "current" if is_current else "superseded"
            say(f"{source_ref}  v{version} ({flag})  {chunk_count} chunks")
        if not rows:
            say("nothing matched — these source_refs are not in this corpus")

        result = await session.execute(
            sql(
                "delete from public.rag_documents " "where corpus = :corpus and source_ref in :refs"
            ).bindparams(bindparam("refs", expanding=True)),
            {"corpus": corpus.value, "refs": refs},
        )
        removed = result.rowcount
        await session.commit()

    print("\n" + "-" * 72)
    print(f"{removed} document row(s) removed by exact (corpus, source_ref).")
    print("Chunks and embeddings went with them by ON DELETE CASCADE.")
    return 0


# --- status -------------------------------------------------------------------

#: `{filter}` is substituted with a constant fragment, never with user input —
#: the corpus arrives as a bind parameter in both branches.
_STATUS_SQL: Final[str] = """
select d.corpus::text                                              as corpus,
       count(distinct d.id)                                        as docs,
       count(distinct d.id) filter (where d.superseded_at is null) as current,
       count(distinct d.id) filter (where d.is_commercial)         as commercial_docs,
       count(c.id)                                                 as chunks,
       count(c.id) filter (where c.is_commercial)                  as commercial_chunks,
       count(c.id) filter (where c.section = :root)                as unsectioned
  from public.rag_documents d
  left join public.rag_chunks c on c.document_id = d.id
 {filter}
 group by d.corpus
 order by d.corpus
"""

_MODELS_SQL: Final[str] = """
select e.model, count(*) as vectors, count(distinct c.document_id) as docs
  from public.rag_embeddings e
  join public.rag_chunks c on c.id = e.chunk_id
  join public.rag_documents d on d.id = c.document_id
 {filter}
 group by e.model
 order by e.model
"""

_MISSING_VECTORS_SQL: Final[str] = """
select count(*) from public.rag_chunks c
 where not exists (select 1 from public.rag_embeddings e where e.chunk_id = c.id)
"""

_CORPUS_FILTER: Final[str] = "where d.corpus = :corpus"


async def status(corpus: Corpus | None) -> int:
    print("\nRAG corpora — what is indexed\n" + "-" * 72)
    where = _CORPUS_FILTER if corpus else ""
    params: dict[str, object] = {"root": ROOT_SECTION}
    if corpus:
        params["corpus"] = corpus.value

    factory = get_sessionmaker()
    async with factory() as session:
        rows = (await session.execute(sql(_STATUS_SQL.format(filter=where)), params)).all()
        header = (
            f"  {'corpus':<18}{'docs':>6}{'current':>9}{'chunks':>8}"
            f"{'comm.doc':>10}{'comm.chunk':>12}{'no-section':>12}"
        )
        rule = "  " + "-" * (len(header) - 2)
        print(header)
        print(rule)
        docs = current = comm_docs = chunks = comm_chunks = unsectioned = 0
        for row in rows:
            print(
                f"  {row.corpus:<18}{row.docs:>6}{row.current:>9}{row.chunks:>8}"
                f"{row.commercial_docs:>10}{row.commercial_chunks:>12}{row.unsectioned:>12}"
            )
            docs += row.docs
            current += row.current
            comm_docs += row.commercial_docs
            chunks += row.chunks
            comm_chunks += row.commercial_chunks
            unsectioned += row.unsectioned
        if not rows:
            print("  (nothing indexed — the Copilot refuses every question with no_sources)")
        else:
            print(rule)
            print(
                f"  {'total':<18}{docs:>6}{current:>9}{chunks:>8}"
                f"{comm_docs:>10}{comm_chunks:>12}{unsectioned:>12}"
            )

        model_params = {"corpus": corpus.value} if corpus else {}
        models = (await session.execute(sql(_MODELS_SQL.format(filter=where)), model_params)).all()
        print("\n  embedding models present")
        if not models:
            print("    (none)")
        for model, vectors, model_docs in models:
            print(f"    {model:<40}{vectors:>8} vectors across {model_docs} document(s)")
        if len(models) > 1:
            print("    NOTE: more than one model. rag_search() only compares within a model.")

        empty = (await session.execute(sql(_MISSING_VECTORS_SQL))).scalar_one()
        if empty:
            print(f"\n  {empty} chunk(s) have no embedding at all — re-run --ingest to backfill.")
        if unsectioned:
            print(
                f"  {unsectioned} chunk(s) cite as '{ROOT_SECTION}' — they name a document "
                "but no findable place in it (§9)."
            )
    return 0


# --- cli ----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ingest_docs.py",
        description=(__doc__ or "").split("\n\n", 1)[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ingest", metavar="PATH", help="markdown file or directory to ingest")
    group.add_argument(
        "--delete",
        metavar="PATH",
        nargs="?",
        const="",
        help="remove the documents PATH would produce (or use --source-ref)",
    )
    group.add_argument("--status", action="store_true", help="what is indexed right now")

    parser.add_argument("--corpus", choices=[c.value for c in Corpus], help="target corpus")
    parser.add_argument(
        "--commercial",
        choices=["document", "chunks"],
        default=None,
        help="how to wall money-quoting text (§4): whole document, or per chunk",
    )
    parser.add_argument(
        "--source-ref",
        action="append",
        default=[],
        metavar="REF",
        help="exact source_ref to delete; repeatable, for files already gone from disk",
    )
    parser.add_argument("--college-id", default=None, help="scope the document to one college")
    parser.add_argument("--program-id", default=None, help="scope the document to one program")
    parser.add_argument(
        "--embedding-model",
        default=None,
        metavar="NAME",
        help=f"override ${EMBEDDING_MODEL_ENV}; must be a {EMBEDDING_DIM}-dim model",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="plan and report only — no writes, no embedding spend",
    )
    return parser


def _uuid_or_exit(value: str | None, label: str) -> uuid.UUID | None:
    if value is None:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        sys.exit(f"--{label} is not a uuid: {value!r}")


async def _run(args: argparse.Namespace, corpus: Corpus | None) -> int:
    """One command, then dispose the engine — on the SAME loop that opened it.

    `app/db/session.py` caches the engine process-wide, and its pooled
    connections belong to the loop they were created on. Disposing from a second
    `asyncio.Runner` would hand psycopg a dead loop.
    """
    try:
        if args.ingest:
            return await ingest(
                Path(args.ingest),
                corpus or Corpus.SOP,
                commercial_mode=args.commercial,
                college_id=_uuid_or_exit(args.college_id, "college-id"),
                program_id=_uuid_or_exit(args.program_id, "program-id"),
                embedding_model=args.embedding_model,
                dry_run=args.dry_run,
            )
        if args.delete is not None:
            return await delete(
                Path(args.delete) if args.delete else None,
                corpus or Corpus.SOP,
                args.source_ref,
            )
        return await status(corpus)
    finally:
        await dispose_engine()


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    lenient_stdout()
    load_env()

    corpus: Corpus | None = Corpus(args.corpus) if args.corpus else None
    if (args.ingest or args.delete is not None) and corpus is None:
        parser.error("--corpus is required with --ingest and --delete")

    return run_async(_run(args, corpus))


if __name__ == "__main__":
    raise SystemExit(main())
