"""§9's contract versioning, against a real supersede in the real tables.

    "Contracts corpus is versioned. A superseded clause must not surface without
     a version flag."

The eval corpus ingests one Master Services Agreement twice under a single
`source_ref`. Version 1 says thirty days' notice; version 2 says sixty. Both stay
indexed. The rule then has two halves, and both are tested here:

  * by default the superseded clause does not surface at all
  * when it is asked for explicitly it surfaces WITH its flag, all the way to
    the citation string the reader sees

What is NOT enforced, and is finding F15: nothing checks that the generated prose
mentions the flag. `SYSTEM_PROMPT` rule 5 asks the model to say so; unlike the
citation and figure rules, no gate verifies it afterwards. §9's other three rules
are code; this one is a request.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.domain.enums import Corpus, Persona
from app.rag.copilot import _render_sources
from app.rag.embeddings import DeterministicEmbedder
from app.rag.retrieval import RagRetriever
from app.rag.scope import RetrievalScope
from tests.rag_eval.conftest import db_session, principal, run_async

pytestmark = pytest.mark.usefixtures("ingested")

NOTICE_QUERY = "How much notice must an educator give before withdrawing?"


def _retrieve(query: str, **kwargs: object) -> tuple:
    async def go() -> tuple:
        async with db_session() as session:
            return await RagRetriever(session, DeterministicEmbedder()).retrieve(
                RetrievalScope.for_principal(principal(Persona.MANAGER)),
                query,
                **kwargs,  # type: ignore[arg-type]
            )

    return run_async(go())


def test_the_second_ingest_superseded_the_first_rather_than_replacing_it() -> None:
    async def go() -> list[tuple]:
        async with db_session() as session:
            return list(
                (
                    await session.execute(
                        text(
                            "select version, superseded_at is not null "
                            "from public.rag_documents "
                            "where source_ref = 'rag-eval/contracts/master-services' "
                            "order by version"
                        )
                    )
                ).all()
            )

    rows = run_async(go())
    assert rows == [
        (1, True),
        (2, False),
    ], "Contracts must retain the old version and flag it (§9), not overwrite it"


def test_the_superseded_clause_does_not_surface_by_default() -> None:
    hits = _retrieve(NOTICE_QUERY, corpora=[Corpus.CONTRACTS], limit=8)
    assert hits, "the current contract should be retrievable"
    assert all(not h.is_superseded for h in hits)
    assert all(h.version == 2 for h in hits)
    assert not any("thirty days" in h.content for h in hits)
    assert any("sixty days" in h.content for h in hits)


def test_asking_for_history_returns_the_old_clause_carrying_its_flag() -> None:
    hits = _retrieve(NOTICE_QUERY, corpora=[Corpus.CONTRACTS], limit=8, include_superseded=True)
    old = [h for h in hits if h.is_superseded]
    assert old, "include_superseded=True should reach version 1"
    assert any("thirty days" in h.content for h in old)
    assert all(h.version == 1 for h in old)


def test_the_flag_survives_into_the_citation_string() -> None:
    """The flag must reach the reader, not stop at a boolean column."""
    hits = _retrieve(NOTICE_QUERY, corpora=[Corpus.CONTRACTS], limit=8, include_superseded=True)
    old = next(h for h in hits if h.is_superseded)
    assert "SUPERSEDED" in old.citation
    assert f"v{old.version}" in old.citation


def test_the_flag_reaches_the_model_in_the_source_block() -> None:
    """`_render_sources` marks it, so the model is told. That is half the job."""
    hits = _retrieve(NOTICE_QUERY, corpora=[Corpus.CONTRACTS], limit=8, include_superseded=True)
    rendered = _render_sources(hits)
    assert "[SUPERSEDED]" in rendered


def test_nothing_verifies_the_answer_repeats_the_flag() -> None:
    """FINDING F15. §9's version rule is the one §9 rule with no gate behind it.

    `check_citations` and `check_figures` both discard a non-compliant answer.
    There is no `check_superseded`, so an answer that quotes the thirty-day
    clause as current — while the citation list quietly carries
    `is_superseded=True` — is returned as an answer.
    """
    from app.rag import guards

    assert not any(
        name for name in dir(guards) if "supersed" in name.lower() or "version" in name.lower()
    ), "a gate now exists; update docs/rag-findings.md and delete this test"
