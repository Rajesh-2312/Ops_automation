"""R1 at the seam. CLAUDE.md §12: "compare every number in generated text against
the structured input".

The interesting cases are the near misses, because those are the ones a model
actually produces: the rounded figure, the plausible total, the year nobody
supplied. A grounding check that only catches `4815162342` is not protecting
anything.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.agents.grounding import (
    UngroundedFigureError,
    assert_grounded,
    collect_grounded_values,
    figures_in,
)

# --- what counts as a figure -------------------------------------------------


def test_thousands_separators_are_one_value() -> None:
    """`15,484` and `15484` are the same claim; the model may format either way."""
    assert [value for _, value in figures_in("earned 15,484")] == [Decimal("15484")]


def test_ordered_list_markers_are_not_claims() -> None:
    """Layout, not fact. Without this the check fails on every bulleted draft."""
    text = "1. Confirm the MoU\n2) Chase the work order\n3. Book travel"
    assert figures_in(text) == []


def test_a_decimal_inside_a_line_is_still_a_claim() -> None:
    """The list-marker exclusion must not swallow real figures."""
    assert [value for _, value in figures_in("1. Deploy for 1.5 days")] == [Decimal("1.5")]


def test_dates_are_claims() -> None:
    values = {value for _, value in figures_in("the period is 26-31 Jul 2026")}
    assert values == {Decimal(26), Decimal(31), Decimal(2026)}


# --- what the structured input licenses --------------------------------------


def test_values_are_collected_from_nested_structures() -> None:
    payload = {"trainer": {"payable_days": 6, "rates": [3500, Decimal("100.50")]}}
    assert collect_grounded_values(payload) == {
        Decimal(6),
        Decimal(3500),
        Decimal("100.50"),
    }


def test_digits_inside_strings_are_licensed() -> None:
    """A date or an invoice number from the record licenses its own digits."""
    assert collect_grounded_values({"invoice_no": "BCDP/26-27/JUL1"}) == {
        Decimal(26),
        Decimal(27),
        Decimal(1),
    }


def test_booleans_do_not_license_the_figure_one() -> None:
    """`bool` subclasses `int`. Without the guard, `signed=True` grounds "1"."""
    assert collect_grounded_values({"signed": True, "expired": False}) == set()


def test_floats_are_collected_without_their_binary_expansion() -> None:
    """`Decimal(0.1)` is 0.1000000000000000055…, which grounds nothing useful."""
    assert collect_grounded_values({"ratio": 0.1}) == {Decimal("0.1")}


# --- the assertion itself ----------------------------------------------------


def test_a_grounded_figure_passes() -> None:
    assert_grounded("Earned 15,484 for 6 payable days.", {"earned": 15484, "days": 6}, "t")


def test_formatting_differences_pass() -> None:
    """`15484.00` and `15484` are one value — R6's "round once at display"."""
    assert_grounded("Total 15484.00", {"earned": Decimal("15484")}, "t")


def test_an_invented_figure_is_refused() -> None:
    """The failure mode that matters: a plausible number nobody supplied."""
    with pytest.raises(UngroundedFigureError) as exc:
        assert_grounded("Approximately 15,500 was earned.", {"earned": 15484}, "payout.explain")
    assert "15,500" in str(exc.value)
    assert "R1" in str(exc.value)
    assert exc.value.context == "payout.explain"


def test_a_computed_total_is_refused() -> None:
    """R2 in miniature: the model may explain 6 and 3500, never their product."""
    with pytest.raises(UngroundedFigureError):
        assert_grounded(
            "6 days at 3500 comes to 21000.",
            {"payable_days": 6, "rate": 3500},
            "payout.explain",
        )


def test_every_violation_is_reported_not_just_the_first() -> None:
    with pytest.raises(UngroundedFigureError) as exc:
        assert_grounded("We saw 11 and 22 and 33.", {"n": 11}, "t")
    assert {v.value for v in exc.value.violations} == {Decimal(22), Decimal(33)}


def test_a_percentage_sign_does_not_launder_a_figure() -> None:
    with pytest.raises(UngroundedFigureError):
        assert_grounded("Syllabus is 92% complete.", {"completion": 90}, "t")


def test_prose_with_no_figures_always_passes() -> None:
    """The model is free with language; R1 constrains only the facts."""
    assert_grounded("The work order remains unsigned; chase the TA team.", {}, "t")
