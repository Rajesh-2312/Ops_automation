"""Fixtures for the RAG evaluation harness.

Two deliberate choices worth knowing before adding anything here.

**Every database test is a SYNCHRONOUS function that calls `run_async()`.** Two
reasons, and the second is not optional. A session-scoped async fixture plus a
function-scoped event loop is the standard way to get a connection pool bound to
a loop that has already closed, and the failure surfaces as an unrelated `Event
loop is closed` two tests later. More concretely: on Windows, Python builds a
`ProactorEventLoop` and psycopg's async driver refuses to run on it — the same
problem `run_api.py` exists to solve for the server. `run_async()` builds a
selector loop the way that file does, so the harness works on the platform it is
being run on without pytest-asyncio needing to know anything.

**The engine is built here rather than taken from `app.db.session`.** That module
caches its engine in an `lru_cache`, which is right for a long-lived process and
wrong for a test session that opens and closes several loops.

Nothing in this package writes outside `corpus.NAMESPACE`. The one DELETE is in
`_purge()` and is keyed on that prefix.
"""

from __future__ import annotations

import asyncio
import os
import selectors
import sys
from collections.abc import AsyncIterator, Awaitable, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TypeVar
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.security import Principal
from app.db.models import RagChunk, RagDocument, RagEmbedding
from app.domain.enums import Persona
from app.rag.embeddings import DeterministicEmbedder
from app.rag.ingest import RagIngestor
from app.rag.scope import RetrievalScope
from tests.rag_eval import corpus

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Stable ids so a failed run leaves recognisable rows behind rather than random
#: uuids nobody can attribute.
EVAL_USER = UUID("e7a10000-0000-4000-8000-00000000e7a1")


def _dotenv_value(name: str) -> str:
    """Read ONE key out of the repo `.env`, without touching `os.environ`.

    This used to be a `_load_dotenv()` that ran at import and `setdefault`-ed
    the WHOLE `.env` into the process — production `DATABASE_URL`, the
    service-role key and `OPENROUTER_API_KEY` — for every test in every
    directory, because conftest import is global.

    That was two separate problems. The security one: a test process that only
    meant to check a pure function held the production service-role credential.
    The correctness one: it broke unrelated tests that assert the platform boots
    WITHOUT configuration — `tests/unit/test_llm.py` exists precisely to prove
    "Phase 1 has no AI in it" (CLAUDE.md §13), and it failed because this file
    had quietly supplied the key it asserts is absent. Five failures, none of
    them in this directory, none of them a real regression.

    Reading a single named key on demand keeps the blast radius at the one
    fixture that asked. Nothing is written back to the environment.
    """
    path = REPO_ROOT / ".env"
    if not path.exists():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == name:
            return value.strip().strip('"').strip("'")
    return ""


def _database_url() -> str:
    """`DATABASE_URL` from the environment, falling back to `.env` on demand."""
    return (os.environ.get("DATABASE_URL") or _dotenv_value("DATABASE_URL")).strip()


_T = TypeVar("_T")


def _loop_factory() -> asyncio.AbstractEventLoop:
    """A selector-based loop, which psycopg's async driver supports everywhere.

    Mirrors `run_api.py`. On Windows the default `ProactorEventLoop` makes every
    query raise `psycopg.InterfaceError`, so without this the whole DB half of
    the harness fails on the developer's machine and passes in CI, which is the
    worst possible split.
    """
    return asyncio.SelectorEventLoop(selectors.SelectSelector())


def run_async(coro: Awaitable[_T]) -> _T:
    """Run one coroutine on a private selector loop, then close it.

    `asyncio.Runner` rather than `asyncio.run(..., loop_factory=)` because the
    latter is 3.12+, and `pyproject.toml` targets 3.11.
    """
    factory = _loop_factory if sys.platform == "win32" else None
    with asyncio.Runner(loop_factory=factory) as runner:
        return runner.run(coro)


def _async_url() -> str | None:
    url = _database_url()
    if not url:
        return None
    _, separator, rest = url.partition("://")
    return f"postgresql+psycopg://{rest}" if separator else None


def _make_engine():  # noqa: ANN202 - AsyncEngine, but importing it here adds nothing
    url = _async_url()
    if url is None:
        raise RuntimeError("DATABASE_URL is not set")
    return create_async_engine(url, pool_pre_ping=True, future=True)


@asynccontextmanager
async def db_session() -> AsyncIterator[AsyncSession]:
    """One session on a private engine, disposed on exit.

    Use inside an `async def` test. The engine is per-call on purpose; see the
    module docstring.
    """
    engine = _make_engine()
    try:
        factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


# --- ingestion ----------------------------------------------------------------


async def _purge() -> None:
    """Remove every row this package created. Keyed on the namespace, only.

    Chunks and embeddings go by cascade from `rag_documents`, which is what the
    schema's `on delete cascade` is for. Written as one statement so a partial
    teardown is not possible.
    """
    async with db_session() as session:
        await session.execute(
            text("delete from public.rag_documents where source_ref like :prefix"),
            {"prefix": f"{corpus.NAMESPACE}%"},
        )
        await session.commit()


class StagedSession:
    """A session proxy that flushes document → chunk → embedding, in that order.

    THIS IS A WORKAROUND FOR FINDING F1, NOT A FIXTURE CONVENIENCE.

    `RagIngestor.ingest()` adds a `RagDocument`, then its `RagChunk`s, then their
    `RagEmbedding`s, and never flushes. SQLAlchemy's unit of work orders inserts
    from `relationship()` dependencies, and `app/db/models.py` declares none — a
    bare `ForeignKey` column is invisible to that sort. So the flush emits the
    `rag_chunks` INSERT first and Postgres rejects it:

        psycopg.errors.ForeignKeyViolation: insert or update on table
        "rag_chunks" violates foreign key constraint "rag_chunks_document_id_fkey"

    Ingestion therefore cannot write a single document to the real database, and
    the six corpora are empty. This proxy buffers each add by type and lets
    `sequence()` release them in dependency order, so the rest of the harness can
    measure a corpus that would otherwise not exist.

    Everything downstream of ingestion — chunking, the vectors, `rag_search()`,
    the persona filter, the citation gates — is the shipped code, untouched.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._staged: list[object] = []

    def add(self, obj: object) -> None:
        self._staged.append(obj)

    async def execute(self, *args: object, **kwargs: object):  # noqa: ANN201, ANN401
        return await self._session.execute(*args, **kwargs)

    async def sequence(self) -> None:
        """Flush what is buffered, parents first. Call after each `ingest()`.

        The leading flush matters for the supersede path: stamping
        `superseded_at` on the old row is an UPDATE that must land before the new
        version's INSERT, or `rag_documents_one_current` — the partial unique
        index over current rows — rejects the pair.
        """
        await self._session.flush()
        for kind in (RagDocument, RagChunk, RagEmbedding):
            batch = [o for o in self._staged if isinstance(o, kind)]
            if batch:
                self._session.add_all(batch)
                await self._session.flush()
        self._staged.clear()

    async def commit(self) -> None:
        await self.sequence()
        await self._session.commit()


async def _ingest_all() -> dict[str, object]:
    """Ingest the eval corpus, including the contract supersede, and report."""
    embedder = DeterministicEmbedder()
    summary: dict[str, object] = {}
    async with db_session() as session:
        staged = StagedSession(session)
        ingestor = RagIngestor(staged, embedder)  # type: ignore[arg-type]
        chunks = 0
        for spec in corpus.documents():
            result = await ingestor.ingest(spec)
            await staged.sequence()
            chunks += result.chunks_written
        # Contract v1 then v2 under one source_ref: the second call supersedes
        # the first, which is the behaviour §9's version rule is about.
        v1 = await ingestor.ingest(corpus.contract_v1())
        await staged.sequence()
        v2 = await ingestor.ingest(corpus.contract_v2())
        await staged.sequence()
        chunks += v1.chunks_written + v2.chunks_written
        await session.commit()
        summary["chunks"] = chunks
        summary["contract_v1_id"] = v1.document_id
        summary["contract_v2_id"] = v2.document_id
        summary["superseded"] = v2.superseded_document_id
    return summary


@pytest.fixture(scope="session")
def database_url() -> str:
    url = _database_url()
    if not url:
        pytest.skip("DATABASE_URL is not set; DB-backed RAG evaluation skipped")
    return url


@pytest.fixture(scope="session")
def ingested(database_url: str) -> Iterator[dict[str, object]]:
    """The eval corpus, live in the real tables, removed afterwards.

    Purges first as well as last: a previous run killed mid-way must not leave
    the second contract version behind and make the versioning test pass for the
    wrong reason.
    """
    run_async(_purge())
    try:
        yield run_async(_ingest_all())
    finally:
        run_async(_purge())


# --- personas -----------------------------------------------------------------


def principal(persona: Persona, colleges: frozenset[UUID] = frozenset()) -> Principal:
    return Principal(user_id=EVAL_USER, persona=persona, college_ids=colleges)


@pytest.fixture
def manager_scope() -> RetrievalScope:
    return RetrievalScope.for_principal(principal(Persona.MANAGER))


@pytest.fixture
def lde_scope() -> RetrievalScope:
    return RetrievalScope.for_principal(principal(Persona.LDE_EXECUTIVE))


@pytest.fixture
def senior_scope() -> RetrievalScope:
    return RetrievalScope.for_principal(principal(Persona.SENIOR_MANAGER))


@pytest.fixture
def embedder() -> DeterministicEmbedder:
    """Offline embedder. Deterministic, free, and the same one ingestion used."""
    return DeterministicEmbedder()


def live_enabled() -> bool:
    """True when real OpenRouter calls are permitted. Off by default: money."""
    return os.environ.get("RAG_EVAL_LIVE", "").strip() == "1"


requires_live = pytest.mark.skipif(
    not live_enabled(),
    reason="live model evaluation costs money; set RAG_EVAL_LIVE=1 to run",
)
