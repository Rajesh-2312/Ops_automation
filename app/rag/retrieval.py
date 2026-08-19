"""The shared retrieval service. The ONE place a similarity query is issued.

CLAUDE.md §8 lists Retrieval as a shared service rather than an agent, and §9
governs what it may do. Both matter here:

  * There is exactly one method that returns chunks, it requires a
    `RetrievalScope`, and it is the only SQL in the application that reads
    `rag_chunks`. `tests/unit/test_rag_retrieval.py` asserts that last clause
    against the source tree, so a second, unscoped query path fails CI rather
    than shipping.
  * The wall is applied twice on purpose: once in `public.rag_search()`'s WHERE
    clause (which is also what protects the browser path, where RLS is live) and
    once here, over the returned rows. The second pass is NOT a filter. It is an
    assertion: a row that reaches it and fails is a breach of the first wall, and
    it raises rather than quietly dropping the row, because silently dropping it
    would hide the failure that matters.

WHY THE SECOND PASS IS AN ASSERTION AND NOT A FILTER
----------------------------------------------------
If the SQL wall ever weakens — a policy edit, a parameter dropped at a call site,
a new corpus with no ACL row — a filter here would paper over it and the system
would keep answering correctly while its actual defence was gone. An exception
names the failure on the first request instead. This is the same reasoning
`app/core/security.py` applies to a misconfigured JWT verifier: fail loudly, do
not fall back to trusting.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import format_vector
from app.domain.enums import Corpus
from app.rag.embeddings import Embedder
from app.rag.scope import ALL_CORPORA, RetrievalScope

log = structlog.get_logger(__name__)

#: Default number of chunks handed to a generator. Eight fits comfortably inside
#: a volume-tier context alongside the question and the citation instructions,
#: and past roughly that number the marginal chunk mostly dilutes: the model
#: starts citing the weakest source it was given because it is present.
DEFAULT_LIMIT: int = 8

#: The one statement in this application that reads `rag_chunks`. Every persona
#: parameter is bound; none has a default here or in the SQL function.
_SEARCH_SQL = text("""
    select chunk_id, document_id, corpus::text as corpus, title, section, content,
           version, is_superseded, is_commercial, similarity
    from public.rag_search(
      :query_embedding,
      :model,
      cast(:corpora as public.rag_corpus[]),
      :is_internal,
      :can_see_commercials,
      cast(:role as public.app_role),
      cast(:college_ids as uuid[]),
      :limit,
      :include_superseded
    )
    """)


class RetrievalWallBreach(RuntimeError):
    """A chunk came back that the caller's scope does not permit.

    Never expected. If this raises, `public.rag_search()`'s filter did not run or
    did not hold, and the correct response is a 500 and an alert — not a retry
    and not a narrower re-query.
    """


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    """One retrieved chunk and everything a citation needs.

    `title` and `section` are the two halves §9 requires a citation to name, and
    they are non-optional here for the same reason they are NOT NULL in the
    schema: an uncitable chunk must never reach a generator, because a generator
    handed uncitable context will cite something plausible instead.

    `is_superseded` travels with every row rather than being filtered out
    silently — §9: "A superseded clause must not surface without a version flag."
    """

    chunk_id: UUID
    document_id: UUID
    corpus: Corpus
    title: str
    section: str
    content: str
    version: int
    is_superseded: bool
    is_commercial: bool
    similarity: float

    @property
    def citation(self) -> str:
        """Human-readable provenance: what the answer must attribute the text to."""
        flag = f" (SUPERSEDED, v{self.version})" if self.is_superseded else f" (v{self.version})"
        return f"{self.title} — {self.section}{flag}"


class RagRetriever:
    """Persona-scoped similarity search. The only path to a chunk.

    Takes a session rather than opening one: retrieval frequently runs inside a
    request that has already begun a transaction, and a second connection would
    read a different snapshot mid-answer.
    """

    def __init__(self, session: AsyncSession, embedder: Embedder) -> None:
        self._session = session
        self._embedder = embedder

    async def retrieve(
        self,
        scope: RetrievalScope,
        query: str,
        *,
        corpora: Sequence[Corpus] | None = None,
        limit: int = DEFAULT_LIMIT,
        include_superseded: bool = False,
    ) -> tuple[RetrievedChunk, ...]:
        """Nearest chunks the caller is permitted to see. Scope first, always.

        `scope` is positional and required — see `app/rag/scope.py` for why that
        signature is the enforcement mechanism rather than a convention.

        A non-internal persona short-circuits to zero chunks WITHOUT issuing a
        query or spending an embedding call. §4 gives a trainer "own deployment,
        tracksheet, invoice status. Nothing else" and a college "published
        artifacts only"; neither holds any corpus, so the query could only ever
        return nothing. Returning early makes the denial cheap and, more
        usefully, makes it observable in the log as a distinct event rather than
        as an empty result that looks like a corpus gap.
        """
        if not scope.is_internal:
            log.info(
                "rag.retrieve.denied",
                reason="persona_not_internal",
                persona=scope.persona.value,
            )
            return ()

        requested = tuple(corpora) if corpora else ALL_CORPORA
        if not requested or limit <= 0:
            return ()

        started = time.perf_counter()
        vector = (await self._embedder.embed([query]))[0]

        rows = (
            await self._session.execute(
                _SEARCH_SQL,
                {
                    "query_embedding": format_vector(vector),
                    "model": self._embedder.model,
                    "corpora": [c.value for c in requested],
                    "is_internal": scope.is_internal,
                    "can_see_commercials": scope.can_see_commercials,
                    "role": scope.persona.value,
                    "college_ids": [str(cid) for cid in sorted(scope.college_ids, key=str)],
                    "limit": limit,
                    "include_superseded": include_superseded,
                },
            )
        ).mappings()

        hits = tuple(
            RetrievedChunk(
                chunk_id=row["chunk_id"],
                document_id=row["document_id"],
                corpus=Corpus(row["corpus"]),
                title=row["title"],
                section=row["section"],
                content=row["content"],
                version=row["version"],
                is_superseded=row["is_superseded"],
                is_commercial=row["is_commercial"],
                similarity=float(row["similarity"]),
            )
            for row in rows
        )

        self._assert_wall_held(scope, hits)

        log.info(
            "rag.retrieve",
            persona=scope.persona.value,
            corpora=[c.value for c in requested],
            can_see_commercials=scope.can_see_commercials,
            include_superseded=include_superseded,
            limit=limit,
            hits=len(hits),
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
        return hits

    @staticmethod
    def _assert_wall_held(scope: RetrievalScope, hits: Sequence[RetrievedChunk]) -> None:
        """Re-check the §4 wall over what came back. Raises; does not filter.

        See the module docstring: dropping the row here would conceal the failure
        of the wall that actually matters. Only the commercials flag is
        re-checkable from the returned columns — corpus ACL and college reach are
        resolved inside the SQL function against tables this row does not carry —
        so this asserts the one boundary it can, which is also the one §4 names
        explicitly and R5 requires a test for.
        """
        for hit in hits:
            if hit.is_commercial and not scope.permits_commercial():
                raise RetrievalWallBreach(
                    f"public.rag_search() returned commercial chunk {hit.chunk_id} to "
                    f"persona {scope.persona.value!r}, which CLAUDE.md §4 walls off. "
                    "The SQL persona filter did not hold — do not retry, investigate."
                )
