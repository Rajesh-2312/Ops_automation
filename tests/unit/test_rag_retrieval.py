"""The persona filter — CLAUDE.md §9's load-bearing rule, and R5's discipline.

    "Persona filter applies BEFORE retrieval, not after generation."

R5 asks for "a test that asserts a forbidden read returns zero rows". The
equivalent here is a forbidden corpus returning zero *chunks*, and — because the
backend runs on a BYPASSRLS connection where no policy can save us — a test that
the filter is *structurally* impossible to skip:

  * a scope cannot claim reach its persona does not have;
  * a non-internal persona never reaches the database at all;
  * every persona parameter is bound into the one search statement;
  * a commercial chunk arriving for a persona outside the wall RAISES, because
    silently dropping it would conceal the failure of the wall that matters;
  * there is exactly one place in `app/` that reads `rag_chunks`.

The last one is a source-tree assertion rather than a behavioural one, which is
unusual and deliberate: a second, unscoped query path would pass every
behavioural test in this file while defeating all of them.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app.core.security import Principal
from app.domain.enums import Corpus, Persona
from app.rag.embeddings import DeterministicEmbedder
from app.rag.retrieval import RagRetriever, RetrievalWallBreach, RetrievedChunk
from app.rag.scope import ALL_CORPORA, RetrievalScope

COLLEGE_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
COLLEGE_B = uuid.UUID("22222222-2222-2222-2222-222222222222")
USER = uuid.UUID("33333333-3333-3333-3333-333333333333")


def principal(persona: Persona, colleges=(COLLEGE_A,)) -> Principal:
    return Principal(user_id=USER, persona=persona, college_ids=frozenset(colleges))


# --- the fake session --------------------------------------------------------


class FakeMappings:
    def __init__(self, rows):
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return FakeMappings(self._rows)


class FakeSession:
    """Records the parameters every query was issued with. Evaluates no SQL."""

    def __init__(self, rows=()):
        self.rows = list(rows)
        self.calls: list[dict] = []

    async def execute(self, _statement, params=None):
        self.calls.append(dict(params or {}))
        return FakeResult(self.rows)


def row(**overrides) -> dict:
    base = {
        "chunk_id": uuid.uuid4(),
        "document_id": uuid.uuid4(),
        "corpus": Corpus.SOP.value,
        "title": "Trainer Onboarding SOP",
        "section": "Work orders",
        "content": "A signed work order must be on file before deployment.",
        "version": 1,
        "is_superseded": False,
        "is_commercial": False,
        "similarity": 0.81,
    }
    base.update(overrides)
    return base


def retriever(session) -> RagRetriever:
    return RagRetriever(session, DeterministicEmbedder(dim=64))


# --- a scope cannot lie about itself -----------------------------------------


def test_scope_mirrors_the_principal():
    scope = RetrievalScope.for_principal(principal(Persona.MANAGER))
    assert scope.is_internal
    assert scope.can_see_commercials
    assert scope.college_ids == frozenset({COLLEGE_A})


def test_lde_executive_scope_is_outside_the_commercials_wall():
    """§4: an LDE Executive has NO commercials."""
    scope = RetrievalScope.for_principal(principal(Persona.LDE_EXECUTIVE))
    assert scope.is_internal
    assert not scope.can_see_commercials


def test_a_scope_cannot_grant_itself_commercials():
    """The open door this check closes: `can_see_commercials=True` to pass a test."""
    with pytest.raises(ValueError, match="NO commercials"):
        RetrievalScope(
            persona=Persona.LDE_EXECUTIVE,
            college_ids=frozenset({COLLEGE_A}),
            is_internal=True,
            can_see_commercials=True,
        )


def test_a_scope_cannot_grant_itself_internal_status():
    with pytest.raises(ValueError, match="is_internal"):
        RetrievalScope(persona=Persona.TRAINER, is_internal=True)


# --- zero chunks for the personas that hold no corpus ------------------------


@pytest.mark.parametrize("persona", [Persona.TRAINER, Persona.COLLEGE])
async def test_external_personas_retrieve_zero_chunks_and_never_query(persona):
    """R5's "zero rows", for retrieval. And zero queries: the denial is free."""
    session = FakeSession([row()])  # the store is NOT empty; the persona is refused
    scope = RetrievalScope.for_principal(principal(persona, colleges=()))

    hits = await retriever(session).retrieve(scope, "what is the onboarding process?")

    assert hits == ()
    assert session.calls == []


# --- the persona travels with the query --------------------------------------


async def test_every_persona_parameter_is_bound_into_the_search():
    """Filter before retrieve: the scope is in the same statement as ORDER BY/LIMIT."""
    session = FakeSession([row()])
    scope = RetrievalScope.for_principal(principal(Persona.LDE_EXECUTIVE))

    await retriever(session).retrieve(scope, "how are work orders filed?")

    (params,) = session.calls
    assert params["is_internal"] is True
    assert params["can_see_commercials"] is False
    assert params["role"] == Persona.LDE_EXECUTIVE.value
    assert params["college_ids"] == [str(COLLEGE_A)]
    assert params["include_superseded"] is False
    assert set(params["corpora"]) == {c.value for c in ALL_CORPORA}


async def test_requested_corpora_narrow_the_search():
    session = FakeSession([row()])
    scope = RetrievalScope.for_principal(principal(Persona.MANAGER))

    await retriever(session).retrieve(scope, "notice period?", corpora=[Corpus.CONTRACTS])

    assert session.calls[0]["corpora"] == [Corpus.CONTRACTS.value]


async def test_a_manager_with_no_assignments_reaches_nothing():
    """Deny by default, exactly as `my_college_ids()` does in SQL."""
    session = FakeSession([row()])
    scope = RetrievalScope.for_principal(principal(Persona.MANAGER, colleges=()))

    await retriever(session).retrieve(scope, "anything")

    assert session.calls[0]["college_ids"] == []


async def test_zero_limit_short_circuits():
    session = FakeSession([row()])
    scope = RetrievalScope.for_principal(principal(Persona.MANAGER))
    assert await retriever(session).retrieve(scope, "q", limit=0) == ()
    assert session.calls == []


# --- the second wall asserts; it does not quietly filter ---------------------


async def test_a_commercial_chunk_reaching_an_lde_executive_raises():
    """If the SQL wall ever fails, this must be loud rather than tidied away."""
    session = FakeSession([row(is_commercial=True)])
    scope = RetrievalScope.for_principal(principal(Persona.LDE_EXECUTIVE))

    with pytest.raises(RetrievalWallBreach, match="§4|walls off"):
        await retriever(session).retrieve(scope, "what is the trainer rate?")


async def test_a_commercial_chunk_is_fine_for_a_manager():
    session = FakeSession([row(is_commercial=True)])
    scope = RetrievalScope.for_principal(principal(Persona.MANAGER))

    hits = await retriever(session).retrieve(scope, "what do the commercial terms say?")

    assert len(hits) == 1
    assert hits[0].is_commercial


# --- §9's version flag -------------------------------------------------------


async def test_superseded_chunks_are_excluded_by_default_and_flagged_when_asked():
    session = FakeSession([row(is_superseded=True, version=2)])
    scope = RetrievalScope.for_principal(principal(Persona.MANAGER))

    await retriever(session).retrieve(scope, "q")
    assert session.calls[0]["include_superseded"] is False

    hits = await retriever(session).retrieve(scope, "q", include_superseded=True)
    assert session.calls[1]["include_superseded"] is True
    assert hits[0].is_superseded
    assert "SUPERSEDED" in hits[0].citation


def test_a_citation_names_document_and_section():
    """§9: "Every answer cites source document and section.\" """
    chunk = RetrievedChunk(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        corpus=Corpus.SOP,
        title="Trainer Onboarding SOP",
        section="Work orders",
        content="...",
        version=1,
        is_superseded=False,
        is_commercial=False,
        similarity=0.9,
    )
    assert "Trainer Onboarding SOP" in chunk.citation
    assert "Work orders" in chunk.citation


# --- there is exactly one query path -----------------------------------------


def test_only_the_retriever_reads_rag_chunks(repo_root: Path):
    """A second, unscoped query path would pass every test above while defeating it.

    So the shape of the codebase is asserted directly: the table may be named in
    SQL (`public.rag_chunks`) nowhere in `app/` at all. The scoped search reaches
    it through `public.rag_search()`, which cannot run without a persona, and
    ingestion reaches it through the ORM model, which returns nothing to a
    caller. A hand-written `select ... from public.rag_chunks` anywhere is the
    unscoped path this test exists to catch.
    """
    offenders = []
    for path in (repo_root / "app").rglob("*.py"):
        # The mapping layer names every table it maps, by definition. It issues
        # no queries of its own; `db/models.py` is declarations only.
        if path.name == "models.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "public.rag_chunks" in text or "from rag_chunks" in text:
            offenders.append(path.relative_to(repo_root).as_posix())
    assert offenders == [], (
        "these modules name rag_chunks outside the scoped retrieval path: " f"{offenders}"
    )


def test_the_search_statement_binds_no_persona_default(repo_root: Path):
    """Every persona parameter is passed explicitly at the one call site."""
    source = (repo_root / "app" / "rag" / "retrieval.py").read_text(encoding="utf-8")
    for parameter in (
        ":is_internal",
        ":can_see_commercials",
        "cast(:role as public.app_role)",
        "cast(:college_ids as uuid[])",
        "cast(:corpora as public.rag_corpus[])",
    ):
        assert parameter in source
