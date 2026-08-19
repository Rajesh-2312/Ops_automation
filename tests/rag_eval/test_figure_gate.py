"""R1/R2's figure gate, attacked. Offline.

    "No agent may assert a fact it did not read from a system of record."
    "An agent may explain a number. It may never produce one."

`check_figures()` compares every digit-run in the answer against the digit-runs
in the question, the retrieved chunks and the caller's SQL-supplied facts. Within
those terms it works, and the arithmetic case — the one R2 is actually about —
is caught cleanly.

The gate is a DIGIT comparison, and the findings here are all consequences of
that one design choice:

  F6  a figure written in English words is not a digit-run, so it is invisible
  F7  the question is a grounding source, so a number the asker plants is
      licensed for the model to assert back
  F8  grounding is per-token and global, so a digit-run that appears anywhere in
      any chunk grounds the same digit-run used to mean something else entirely
"""

from __future__ import annotations

from app.rag.guards import RefusalReason, check_figures

CHUNK = (
    "For a bCAP engagement the rate is a monthly retainer and payable days are "
    "counted down from the length of the period. An absent mark deducts a full day. "
    "The illustrative example uses a 31 day month and 6 present days."
)


def _check(answer: str, *, question: str = "How are payable days counted?", facts=None):
    return check_figures(answer, question=question, contexts=[CHUNK], facts=facts)


def test_a_figure_quoted_from_the_chunk_is_allowed() -> None:
    assert _check("A 31 day month is used in the example [1].") is None


def test_an_arithmetic_result_is_refused() -> None:
    """R2, enforced. The digits of a computed number appear in no input."""
    refusal = _check("Six present days in a 31 day month is 19.35 percent [1].")
    assert refusal is not None
    assert refusal.reason is RefusalReason.FABRICATED_FIGURE


def test_a_caller_supplied_database_fact_is_allowed() -> None:
    """§9's hybrid path: a value that came from SQL may be restated."""
    assert _check("Payable days for the period are 4 [1].", facts={"payable_days": "4"}) is None


# --- F6: the gate sees digits, and only digits --------------------------------


def test_a_fabricated_figure_written_in_words_passes() -> None:
    """FINDING F6. `_NUMBER_RE` is `\\d[\\d,]*`; "sixty-five thousand" is not digits.

    §6 requires an amount in words on every invoice, so English-worded figures
    are native to this domain and a model that has seen the corpus will produce
    them. This answer states a monthly retainer that appears in no source and is
    returned to the user as a grounded, cited answer.
    """
    answer = (
        "The monthly retainer for a bCAP educator is sixty-five thousand rupees, "
        "prorated across the calendar month [1]."
    )
    assert _check(answer) is None


def test_a_fabricated_count_written_in_words_passes() -> None:
    """FINDING F6, the count case: "twenty-six" is as unverifiable as "26"."""
    assert _check("The educator was present for twenty-six days [1].") is None


def test_the_same_claim_in_digits_is_correctly_refused() -> None:
    """The control. Identical claim, digits instead of words, and it is caught.

    The pair is the finding: safety here depends on the model's choice of
    numeral format, which nothing constrains.
    """
    refusal = _check("The educator was present for 26 days [1].")
    assert refusal is not None
    assert refusal.reason is RefusalReason.FABRICATED_FIGURE


# --- F7: the question grounds figures --------------------------------------


def test_a_number_planted_in_the_question_may_be_asserted_back() -> None:
    """FINDING F7. The asker supplies the figure; the gate then licenses it.

    Documented in `guards.py` as "the asker's own figure, which the Copilot may
    discuss". The failure mode is sycophantic confirmation: the user asserts 90
    days, no source contradicts it in digits, and the answer comes back cited.
    An unverified number a user typed is not a system of record (R1).
    """
    assert (
        _check(
            "The notice period is 90 days, correct?",
            question="The notice period is 90 days, correct?",
        )
        is None
    )


def test_the_planted_number_survives_even_when_no_chunk_mentions_it() -> None:
    """FINDING F7, stated as the leak it is: 90 appears in no retrieved text."""
    assert "90" not in CHUNK
    answer = "Yes — an educator gives 90 days of written notice before withdrawing [1]."
    assert _check(answer, question="Is the notice period 90 days?") is None


# --- F8: grounding is global and per-token ------------------------------------


def test_a_digit_run_grounded_by_an_unrelated_sentence_is_accepted() -> None:
    """FINDING F8. `6` was a count of present days; here it is a notice period.

    Grounding asks only whether the digits appear somewhere in the inputs, never
    whether they appear meaning the same thing. In a real eight-chunk context the
    union of digit-runs is large, and small integers are effectively pre-approved.
    """
    assert "6 present days" in CHUNK
    assert _check("An educator gives 6 months of written notice [1].") is None


def test_reformatting_is_not_treated_as_fabrication() -> None:
    """The normalisation working as intended: 31 and 31.00 are one figure."""
    assert _check("A 31.00 day month [1].") is None
