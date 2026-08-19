"""The Copilot's answer path: what it returns, and everything it refuses.

The LLM is mocked throughout — no live OpenRouter call is made from a test, and
the stub is what lets a *bad* model response be asserted against, which is the
half that matters. The gates in `app/rag/guards.py` exist precisely because a
real model will occasionally produce these answers, so the tests hand the
pipeline exactly those answers on purpose.
"""

from __future__ import annotations

import uuid

import pytest

from app.domain.enums import Corpus, LLMTask, Persona
from app.rag.copilot import COPILOT_TOOLS, OpsCopilot
from app.rag.guards import RefusalReason
from app.rag.retrieval import RetrievedChunk
from app.rag.scope import RetrievalScope

COLLEGE = uuid.UUID("11111111-1111-1111-1111-111111111111")


def scope(persona: Persona = Persona.MANAGER) -> RetrievalScope:
    return RetrievalScope(
        persona=persona,
        college_ids=frozenset({COLLEGE}),
        is_internal=persona in {Persona.SENIOR_MANAGER, Persona.MANAGER, Persona.LDE_EXECUTIVE},
        can_see_commercials=persona in {Persona.SENIOR_MANAGER, Persona.MANAGER},
    )


def chunk(
    content: str = "A signed work order must be on file before deployment.",
) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        corpus=Corpus.SOP,
        title="Trainer Onboarding SOP",
        section="Work orders",
        content=content,
        version=1,
        is_superseded=False,
        is_commercial=False,
        similarity=0.88,
    )


class StubRetriever:
    def __init__(self, hits=()):
        self.hits = tuple(hits)
        self.calls: list[dict] = []

    async def retrieve(self, scope_, query, *, corpora=None, limit=8, include_superseded=False):
        self.calls.append(
            {
                "scope": scope_,
                "query": query,
                "corpora": corpora,
                "limit": limit,
                "include_superseded": include_superseded,
            }
        )
        return self.hits


class StubResponse:
    def __init__(self, text):
        self.text = text
        self.total_tokens = 123


class StubLLM:
    """Records what it was asked. Never makes a network call."""

    def __init__(self, text="A signed work order is required before deployment [1]."):
        self.text = text
        self.calls: list[dict] = []

    async def complete(self, task, *, system, user, **kwargs):
        self.calls.append({"task": task, "system": system, "user": user})
        return StubResponse(self.text)


def build(hits=(), text=None) -> tuple[OpsCopilot, StubRetriever, StubLLM]:
    retriever = StubRetriever(hits)
    llm = StubLLM(text) if text is not None else StubLLM()
    return OpsCopilot(retriever, llm), retriever, llm  # type: ignore[arg-type]


# --- the happy path -----------------------------------------------------------


async def test_a_cited_answer_is_returned_with_its_citations():
    copilot, _, _ = build([chunk()])

    result = await copilot.answer(scope(), "what must be on file before deployment?")

    assert result.answered
    assert result.text is not None and "[1]" in result.text
    assert [(c.marker, c.document, c.section) for c in result.citations] == [
        (1, "Trainer Onboarding SOP", "Work orders")
    ]


async def test_generation_routes_to_the_volume_tier():
    """§2: route by task. Summarising supplied context is not document extraction."""
    copilot, _, llm = build([chunk()])
    await copilot.answer(scope(), "what must be on file?")
    assert llm.calls[0]["task"] is LLMTask.SUMMARY


async def test_the_sources_reach_the_prompt_numbered_and_labelled():
    copilot, _, llm = build([chunk()])
    await copilot.answer(scope(), "what must be on file?")
    prompt = llm.calls[0]["user"]
    assert "[1] Trainer Onboarding SOP — Work orders (v1)" in prompt


async def test_the_copilot_has_exactly_one_read_only_tool():
    """R3: no send, no post, no release — and no save_draft either (§8, read-only)."""
    assert COPILOT_TOOLS == ("rag.retrieve",)


# --- refusal 1: structured facts, before any retrieval or generation ----------


async def test_a_numeric_question_is_refused_without_retrieving_or_generating():
    """R1: the model never even sees a context window with a number sitting in it."""
    copilot, retriever, llm = build([chunk("The trainer worked 26 days in July.")])

    result = await copilot.answer(scope(), "how many days did the trainer work in July?")

    assert not result.answered
    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.STRUCTURED_FACT
    assert retriever.calls == []
    assert llm.calls == []


# --- refusal 2: no sources ----------------------------------------------------


async def test_no_sources_is_a_refusal_not_an_unsourced_answer():
    copilot, _, llm = build([])

    result = await copilot.answer(scope(), "what does the travel policy say?")

    assert not result.answered
    assert result.refusal is not None and result.refusal.reason is RefusalReason.NO_SOURCES
    assert llm.calls == []


async def test_an_external_persona_is_refused_as_no_corpus_access():
    """§4: a trainer holds no corpus. Named distinctly from an empty index."""
    copilot, _, _ = build([])
    external = RetrievalScope(persona=Persona.TRAINER)

    result = await copilot.answer(external, "what does the SOP say about leave?")

    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.NO_CORPUS_ACCESS


# --- refusal 3: uncited and invented output -----------------------------------


async def test_an_uncited_model_answer_is_discarded():
    """§9: "No citation → no answer." The prose is not returned in any form."""
    copilot, _, _ = build([chunk()], text="A signed work order is required.")

    result = await copilot.answer(scope(), "what must be on file?")

    assert not result.answered
    assert result.text is None
    assert result.refusal is not None and result.refusal.reason is RefusalReason.UNCITED


async def test_a_citation_to_a_source_that_was_not_supplied_is_discarded():
    copilot, _, _ = build([chunk()], text="The notice period is thirty days [4].")

    result = await copilot.answer(scope(), "what is the notice period?")

    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.INVALID_CITATION


async def test_a_computed_figure_is_discarded():
    """R2: an agent may explain a number, never produce one."""
    copilot, _, _ = build(
        [chunk("The retainer is ₹65,000 per month.")],
        text="That is about ₹2,096.77 per day [1].",
    )

    result = await copilot.answer(scope(), "how is the retainer expressed?")

    assert not result.answered
    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.FABRICATED_FIGURE


async def test_a_figure_quoted_from_the_source_survives():
    copilot, _, _ = build(
        [chunk("The retainer is ₹65,000 per month.")],
        text="The retainer is expressed per month, at ₹65,000 [1].",
    )

    result = await copilot.answer(scope(), "how is the retainer expressed?")

    assert result.answered


# --- §9's hybrid rule: policy and fact stay visibly separate ------------------


async def test_caller_supplied_facts_are_labelled_in_the_prompt_and_kept_separate():
    copilot, _, llm = build(
        [chunk()],
        text="Six payable days were recorded, which the SOP counts up from P marks [1].",
    )

    result = await copilot.answer(
        scope(),
        "explain the payable-day count on this sheet",
        facts={"payable_days": "6"},
    )

    assert result.answered
    # The values of record are a separate block, never merged into the prose.
    assert result.facts == {"payable_days": "6"}
    assert "VALUES FROM THE DATABASE" in llm.calls[0]["user"]


# --- retrieval options are passed through, not reinvented --------------------


@pytest.mark.parametrize("include_superseded", [True, False])
async def test_retrieval_options_reach_the_retriever(include_superseded):
    copilot, retriever, _ = build([chunk()])

    await copilot.answer(
        scope(),
        "what do the contracts say about notice?",
        corpora=[Corpus.CONTRACTS],
        limit=3,
        include_superseded=include_superseded,
    )

    call = retriever.calls[0]
    assert call["corpora"] == [Corpus.CONTRACTS]
    assert call["limit"] == 3
    assert call["include_superseded"] is include_superseded
    assert call["scope"].persona is Persona.MANAGER
