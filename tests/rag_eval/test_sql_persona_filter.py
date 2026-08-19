"""§9's persona filter, proven in the DEPLOYED SQL and in live retrieval.

    "Persona filter applies **before** retrieval, not after generation."

`tests/unit/test_rag_retrieval.py` asserts that the application binds every
persona parameter, using a fake session. That is necessary and not sufficient:
it proves the call site, not the callee. These tests read
`pg_get_functiondef('public.rag_search')` out of the live database and check the
shape of the statement that actually runs, then run it as three personas against
a real index and count rows.

WHAT "BEFORE RETRIEVAL" HAS TO MEAN
-----------------------------------
Not "the answer omits the forbidden chunk". Not even "the application drops the
row". It has to mean the forbidden row is never a candidate for the ranking, so
that the permitted chunk it would have outranked is still returned. That is a
property of ONE SQL statement — filter, ORDER BY and LIMIT together — and it is
what `test_the_filter_precedes_the_ranking_in_one_statement` checks textually and
`test_an_lde_executive_never_retrieves_a_contract_chunk` checks behaviourally.

The physical execution is a separate question with a different answer; see
`tools/bench_rag.py recall` and finding F12.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import text

from app.domain.enums import Corpus, Persona
from app.rag.retrieval import RagRetriever
from app.rag.scope import RetrievalScope
from tests.rag_eval.conftest import db_session, principal, run_async

pytestmark = pytest.mark.usefixtures("ingested")


def _functiondef() -> str:
    async def go() -> str:
        async with db_session() as session:
            return (
                await session.execute(
                    text("select pg_get_functiondef('public.rag_search'::regproc)")
                )
            ).scalar_one()

    return run_async(go())


def _body_of(functiondef: str) -> str:
    """The SQL between the `$function$` markers, lower-cased, comments stripped.

    Comments are stripped because the migration's own comment says "Every
    conjunct below runs before the ORDER BY", and a test that reads the comment
    instead of the code proves only that somebody wrote a comment.
    """
    inner = functiondef.split("$function$")[1]
    without_comments = re.sub(r"--[^\n]*", " ", inner)
    return without_comments.lower()


def _retrieve(persona: Persona, query: str, **kwargs: object) -> tuple:
    from app.rag.embeddings import DeterministicEmbedder

    async def go() -> tuple:
        async with db_session() as session:
            retriever = RagRetriever(session, DeterministicEmbedder())
            return await retriever.retrieve(
                RetrievalScope.for_principal(principal(persona)),
                query,
                **kwargs,  # type: ignore[arg-type]
            )

    return run_async(go())


# --- the statement, as deployed -----------------------------------------------


def test_the_filter_precedes_the_ranking_in_one_statement() -> None:
    """Read from the live database, not from the migration file on disk.

    A migration that was edited after being applied, or a function replaced by
    hand in a console, both look correct in `supabase/migrations/` and wrong in
    production. This asserts what Postgres will actually run.
    """
    body = _body_of(_functiondef())
    where = re.search(r"\bwhere\b", body)
    assert where is not None
    order = body.index("order by")
    limit = body.rindex("limit")
    assert where.start() < order < limit, "filter must precede ORDER BY and LIMIT"

    clause = body[where.start() : order]
    for conjunct in (
        "p_is_internal",
        "rag_corpus_access",
        "p_can_see_commercials",
        "p_college_ids",
        "p_include_superseded",
    ):
        assert conjunct in clause, f"{conjunct} is not in the WHERE clause of rag_search()"

    # One statement, not two: no CTE or subselect ranks first and filters after.
    assert body.count("order by") == 1
    assert "limit greatest(p_limit, 0)" in body


def test_no_persona_parameter_has_a_default() -> None:
    """A defaulted persona parameter is a call site that can silently omit it."""
    signature = _functiondef().split(")\n", 1)[0].lower()
    for parameter in (
        "p_is_internal",
        "p_can_see_commercials",
        "p_role",
        "p_college_ids",
    ):
        match = re.search(rf"{parameter}\s+[\w\[\]. ]+?(default|,|\))", signature)
        assert match is not None, f"{parameter} missing from the signature"
        assert match.group(1) != "default", f"{parameter} has a default; §9 forbids it"


def test_the_benchmark_copy_of_the_body_has_not_drifted() -> None:
    """`tools/bench_rag.py` reproduces the function body in order to EXPLAIN it.

    `rag_search()` carries `SET search_path`, which makes it non-inlinable, so
    `explain select * from rag_search(...)` shows a bare Function Scan and
    nothing about the index. The copy is the only way to see the plan — and a
    copy that has drifted from the original measures a query nobody runs.
    """
    from tools.bench_rag import _BODY

    deployed = re.sub(r"\s+", " ", _body_of(_functiondef()))
    copied = re.sub(r"\s+", " ", _BODY.lower())
    for fragment in (
        "from public.rag_chunks c",
        "join public.rag_documents d on d.id = c.document_id",
        "and (d.college_id is null or d.college_id = any",
        "order by e.embedding <=>",
    ):
        assert fragment in deployed, f"the deployed function no longer contains {fragment!r}"
        assert fragment in copied, f"tools/bench_rag.py no longer contains {fragment!r}"


# --- the wall, as behaviour ---------------------------------------------------

CONTRACT_QUERY = "How much notice must an educator give before withdrawing?"
MARGIN_QUERY = "What is the programme margin for the quarter?"


def test_a_manager_retrieves_the_contract_clause() -> None:
    """The control. Without it, the LDE assertion below could pass on an empty index."""
    hits = _retrieve(Persona.MANAGER, CONTRACT_QUERY, limit=8)
    contracts = [h for h in hits if h.corpus is Corpus.CONTRACTS]
    assert contracts, "a Manager must be able to read the contracts corpus (§4)"
    assert all(h.is_commercial for h in contracts)


def test_an_lde_executive_never_retrieves_a_contract_chunk() -> None:
    """R5, at the retrieval boundary: zero rows, not a filtered answer."""
    hits = _retrieve(Persona.LDE_EXECUTIVE, CONTRACT_QUERY, limit=8)
    assert [h for h in hits if h.corpus is Corpus.CONTRACTS] == []
    assert [h for h in hits if h.is_commercial] == []


def test_an_lde_executive_gets_zero_rows_when_asking_only_for_contracts() -> None:
    """Naming a corpus you do not hold returns nothing, and does not error.

    An error would confirm the corpus exists and who holds it.
    """
    hits = _retrieve(Persona.LDE_EXECUTIVE, CONTRACT_QUERY, corpora=[Corpus.CONTRACTS], limit=8)
    assert hits == ()


def test_the_commercial_paragraph_inside_a_readable_report_is_walled_per_row() -> None:
    """§4's per-row wall: the delivery narrative is theirs, the margin is not."""
    lde = _retrieve(Persona.LDE_EXECUTIVE, MARGIN_QUERY, corpora=[Corpus.REPORTS], limit=8)
    manager = _retrieve(Persona.MANAGER, MARGIN_QUERY, corpora=[Corpus.REPORTS], limit=8)
    lde_sections = {h.section for h in lde}
    manager_sections = {h.section for h in manager}
    assert "Commercial position" in manager_sections
    assert "Commercial position" not in lde_sections
    assert "Delivery narrative" in lde_sections


def test_a_scope_cannot_be_forged_to_widen_the_wall() -> None:
    """`RetrievalScope.__post_init__` re-derives both flags from the persona."""
    with pytest.raises(ValueError, match="can_see_commercials"):
        RetrievalScope(persona=Persona.LDE_EXECUTIVE, is_internal=True, can_see_commercials=True)


# --- the over-application of the same wall ------------------------------------


def test_the_commercial_regex_walls_off_sop_chunks_from_the_lde_executive() -> None:
    """FINDING F13. `looks_commercial()` fires on the WORD, not on a figure.

    Migration 1600 grants the SOP corpus to an LDE Executive with the rationale
    "On-campus procedure lookup. SOPs carry no commercials." Then
    `app/rag/ingest.py` flags any chunk containing "payout", "invoice",
    "remuneration", "TDS" or a currency token as commercial — and a procedure
    document about the payout cycle says "payout" in most of its paragraphs.

    The result is that the ACL grants the corpus and the chunk classifier takes
    most of it back, silently, at ingest time.
    """

    async def go() -> tuple[int, int, list[str]]:
        async with db_session() as session:
            rows = (
                await session.execute(
                    text(
                        "select c.section, c.is_commercial from public.rag_chunks c "
                        "join public.rag_documents d on d.id = c.document_id "
                        "where d.corpus = 'sop' and d.source_ref like 'rag-eval/sop/%' "
                        "order by d.source_ref, c.ordinal"
                    )
                )
            ).all()
        walled = [section for section, commercial in rows if commercial]
        return len(rows), len(walled), walled

    total, walled, sections = run_async(go())
    assert total > 0
    assert walled > 0, "expected the commercial regex to have fired inside the SOP corpus"
    print(f"\nSOP chunks walled from the LDE Executive: {walled}/{total} -> {sections}")
    # The two that make the point: neither quotes a figure, both are procedure.
    assert "Work order validity" in sections
    assert "Half days" in sections
