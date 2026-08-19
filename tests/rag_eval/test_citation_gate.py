"""§9's citation rule, attacked. Offline — the gate is code, so no model is needed.

    "Every answer cites source document and section. No citation → no answer."

`check_citations()` is the enforcement, and it is genuinely unbypassable by
prompt: no arrangement of words in a question removes a Python `if`. That is the
right architecture and these tests confirm it holds.

They also measure what the gate actually checks, which is narrower than the
sentence in §9 and is the finding worth carrying:

    §9 says      every answer cites source document and section
    the gate says the answer contains at least one `[n]` with 1 <= n <= len(sources)

Those are not the same statement. One in-range marker anywhere licenses an
arbitrary amount of unattributed prose around it, and no check compares a
sentence against the chunk it points at. Both directions of that gap are
demonstrated below with real strings.
"""

from __future__ import annotations

import pytest

from app.rag.guards import RefusalReason, check_citations, extract_citations

SOURCES = 3


def test_an_answer_with_no_marker_is_refused() -> None:
    """The rule, working. No prompt can get past this because it is not a prompt."""
    refusal = check_citations("Payable days are counted down from the period length.", SOURCES)
    assert refusal is not None
    assert refusal.reason is RefusalReason.UNCITED


@pytest.mark.parametrize(
    "answer",
    [
        "Payable days are counted down [1].",
        "First point [1]. Second point [3].",
        "Ranges work [1][2].",
    ],
)
def test_a_properly_marked_answer_passes(answer: str) -> None:
    assert check_citations(answer, SOURCES) is None


def test_a_marker_beyond_the_supplied_sources_is_refused() -> None:
    refusal = check_citations("The clause says otherwise [9].", SOURCES)
    assert refusal is not None
    assert refusal.reason is RefusalReason.INVALID_CITATION


def test_marker_zero_is_refused() -> None:
    refusal = check_citations("As stated [0].", SOURCES)
    assert refusal is not None
    assert refusal.reason is RefusalReason.INVALID_CITATION


# --- the gap between §9's sentence and the gate's check -----------------------


def test_one_marker_licenses_an_entire_unattributed_answer() -> None:
    """FINDING F4. Four claims, one marker, and the gate is satisfied.

    §9 requires every answer to cite its source and section. What is enforced is
    the presence of one in-range marker. A model that opens with three
    unsourced sentences and closes with `[1]` is indistinguishable, to this
    gate, from one that attributes every claim.
    """
    answer = (
        "Payable days for bCAP are counted down from the period length. "
        "Weekends and college holidays are payable. "
        "A trainer may claim accommodation without prior approval. "
        "The notice period is one calendar month. "
        "See the SOP [1]."
    )
    assert check_citations(answer, SOURCES) is None


def test_the_gate_cannot_see_misattribution() -> None:
    """FINDING F4. A marker pointing at an unrelated chunk passes.

    Nothing compares the sentence to the chunk it cites. The citation is
    structurally valid and semantically false, which is the shape of every
    convincing wrong answer.
    """
    answer = "The Institution may cancel a session with no notice at all [2]."
    assert check_citations(answer, SOURCES) is None


def test_a_refusal_shaped_answer_carrying_a_marker_is_reported_as_answered() -> None:
    """FINDING F4. The system prompt asks for this text; the gate blesses it.

    Rule 4 of `SYSTEM_PROMPT` tells the model to say the sources do not cover
    the question. If it appends a marker while doing so, `answered=True` and the
    caller renders a non-answer as an answer with a citation.
    """
    assert check_citations("The sources do not cover this [1].", SOURCES) is None


@pytest.mark.parametrize(
    "answer",
    [
        "Payable days are counted down (Source 1).",
        "Payable days are counted down [Source 1].",
        "Payable days are counted down 【1】.",
        "Payable days are counted down [1, 2].",
        "Payable days are counted down.\n\nSources: 1, 2",
    ],
)
def test_a_citation_the_model_formats_differently_is_discarded(answer: str) -> None:
    """FINDING F5. Correctly-sourced answers thrown away over bracket shape.

    Every string here names its source. None matches `\\[(\\d{1,2})\\]`, so all
    five are refused as UNCITED. The refusal is safe, but it is an availability
    cost paid on every generation the model formats even slightly differently —
    and `[1, 2]` in particular is a form the model reaches for unprompted.
    """
    refusal = check_citations(answer, SOURCES)
    assert refusal is not None
    assert refusal.reason is RefusalReason.UNCITED


def test_a_three_digit_marker_is_misclassified_as_uncited() -> None:
    """A fabricated `[100]` is reported as a formatting miss, not a fabrication.

    Both refuse, so nothing leaks. It matters only for the counter: §9's own
    argument is that a fabricated citation is worth paging someone about and a
    missing one is not, and this case is silently filed under the wrong one.
    """
    refusal = check_citations("As set out in the schedule [100].", SOURCES)
    assert refusal is not None
    assert refusal.reason is RefusalReason.UNCITED


def test_markers_are_deduplicated_in_order() -> None:
    assert extract_citations("a [2] b [1] c [2]") == (2, 1)
