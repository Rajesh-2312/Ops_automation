"""The three refusals: structured facts, missing citations, invented figures.

These are the tests that make CLAUDE.md R1, R2 and §9 enforceable claims rather
than aspirations. Each one corresponds to a sentence in the spec:

  R1  "No agent may assert a fact it did not read from a system of record."
  R2  "An agent may explain a number. It may never produce one."
  §9  "No citation → no answer."
  §9  "Structured facts (dates, amounts, counts) are never retrieved from RAG."
"""

from __future__ import annotations

import pytest

from app.rag.guards import (
    RefusalReason,
    check_citations,
    check_figures,
    extract_citations,
    is_structured_fact_question,
    structured_fact_refusal,
)

# --- structured-fact refusal (R1, §9) ----------------------------------------

STRUCTURED = [
    "How many days did VEMA PRUDHVI SAI work in July?",
    "How much was the net pay for the July cycle?",
    "What is the total remuneration for Malineni this month?",
    "When did the batch start?",
    "What date was the work order signed?",
    "What is the number of students in batch B2?",
    "how long was the trainer deployed",
    "Show me the payable days for this deployment.",
    "What was the TDS amount?",
    "What is the attendance percentage for the CRT batch?",
    "What's the invoice number for July?",
]

POLICY = [
    "How are payable days counted for a bCAP trainer?",
    "What is the process for onboarding a trainer?",
    "Does an unmarked day pay a bCAP trainer?",
    "Who approves a remuneration sheet?",
    "What does the MoU say about notice?",
    "Explain the difference between CRT and bCAP attendance semantics.",
    "Which documents must be on file before deployment?",
]


@pytest.mark.parametrize("question", STRUCTURED)
def test_numeric_questions_are_classified_as_structured_facts(question):
    assert is_structured_fact_question(question)


@pytest.mark.parametrize("question", POLICY)
def test_policy_questions_are_not_refused(question):
    """The Copilot must still be useful. A gate that refuses everything is a bug."""
    assert not is_structured_fact_question(question)


def test_the_refusal_names_where_the_answer_actually_lives():
    """A bare "cannot answer" trains people to stop asking."""
    refusal = structured_fact_refusal("how many days did the trainer work?")
    assert refusal.reason is RefusalReason.STRUCTURED_FACT
    assert "tracksheet" in refusal.message
    assert "policy" in refusal.message.lower()


# --- citation enforcement (§9) ------------------------------------------------


def test_markers_are_extracted_in_order_without_duplicates():
    assert extract_citations("A [2] and B [1], again [2].") == (2, 1)


def test_an_uncited_answer_is_refused():
    """§9: "No citation → no answer." Refused, not returned with a warning."""
    refusal = check_citations("A signed work order is required before deployment.", 3)
    assert refusal is not None
    assert refusal.reason is RefusalReason.UNCITED


def test_a_cited_answer_passes():
    assert check_citations("A signed work order is required [1].", 3) is None


def test_a_citation_to_a_source_that_was_not_retrieved_is_refused():
    """An invented citation is a fabrication, not a formatting problem."""
    refusal = check_citations("The notice period is thirty days [7].", 3)
    assert refusal is not None
    assert refusal.reason is RefusalReason.INVALID_CITATION


def test_citation_zero_is_refused():
    refusal = check_citations("Something [0].", 3)
    assert refusal is not None
    assert refusal.reason is RefusalReason.INVALID_CITATION


# --- figure grounding (R1, R2) ------------------------------------------------

CONTEXT = ["The retainer is ₹65,000 per month and the notice period is 30 days."]


def test_a_figure_quoted_from_a_source_is_allowed():
    """R2: "An agent may explain a number." """
    assert (
        check_figures("The retainer is ₹65,000 per month [1].", question="q", contexts=CONTEXT)
        is None
    )


def test_reformatting_a_source_figure_is_not_fabrication():
    """`65000` and `65,000` are the same figure; only NEW digits are the offence."""
    assert check_figures("The retainer is 65000 [1].", question="q", contexts=CONTEXT) is None


def test_an_arithmetic_result_is_refused():
    """R2: "It may never produce one." 65,000 / 31 appears in no input."""
    refusal = check_figures(
        "That works out to ₹2,096.77 per day [1].", question="q", contexts=CONTEXT
    )
    assert refusal is not None
    assert refusal.reason is RefusalReason.FABRICATED_FIGURE
    assert "2096.77" in refusal.message


def test_citation_markers_are_not_mistaken_for_figures():
    """Otherwise every properly cited answer would fail the figure check."""
    assert check_figures("Work orders are required [2].", question="q", contexts=CONTEXT) is None


def test_a_figure_from_the_question_is_allowed():
    """The asker's own number, which the Copilot may legitimately discuss."""
    assert (
        check_figures(
            "Yes, ₹80,000 is a per-month rate [1].",
            question="is ₹80,000 a monthly rate?",
            contexts=CONTEXT,
        )
        is None
    )


def test_a_caller_supplied_database_fact_is_allowed():
    """§9's hybrid case: values that entered as structured input from SQL."""
    assert (
        check_figures(
            "The 15,484 earned figure reflects six payable days [1].",
            question="explain my payout",
            contexts=CONTEXT,
            facts={"earned": "15,484", "payable_days": "6"},
        )
        is None
    )


def test_a_figure_absent_from_every_input_is_refused_even_with_facts():
    refusal = check_figures(
        "Net pay is therefore ₹14,035 [1].",
        question="explain my payout",
        contexts=CONTEXT,
        facts={"earned": "15,484"},
    )
    assert refusal is not None
    assert refusal.reason is RefusalReason.FABRICATED_FIGURE
