"""Tests for `app.domain.money` — R7 enforcement, rounding, Indian numbering."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.domain.money import (
    ZERO,
    amount_in_words,
    format_indian,
    money,
    quantize_paise,
    round_rupees,
)

# --- R7: float is refused, not converted ------------------------------------


def test_money_refuses_float() -> None:
    """A float must raise at the boundary, not be silently converted.

    `Decimal(0.1)` is 0.1000000000000000055511151231257827…; converting would
    launder the defect into a payout that no one can trace back to its source.
    """
    with pytest.raises(TypeError, match="never use float"):
        money(0.1)  # type: ignore[arg-type]


def test_money_refuses_bool() -> None:
    with pytest.raises(TypeError, match="bool"):
        money(True)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (80000, Decimal(80000)),
        ("0.10", Decimal("0.10")),
        (Decimal("15483.87"), Decimal("15483.87")),
    ],
)
def test_money_accepts_int_str_decimal(value: object, expected: Decimal) -> None:
    assert money(value) == expected  # type: ignore[arg-type]


# --- Rounding ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("14035.48387096774193548387097", 14035),
        ("0.5", 1),  # half UP, not banker's half-even (which would give 0)
        ("1.5", 2),
        ("2.5", 3),  # half-even would give 2 and disagree with the legacy sheet
        ("-0.5", -1),
        ("58500.00", 58500),
    ],
)
def test_round_rupees_is_half_up(raw: str, expected: int) -> None:
    assert round_rupees(Decimal(raw)) == Decimal(expected)


def test_quantize_paise() -> None:
    assert quantize_paise(Decimal("15483.870967741935")) == Decimal("15483.87")
    assert quantize_paise(Decimal("58709.677419354838")) == Decimal("58709.68")


# --- Amount in words ---------------------------------------------------------
# Format (documented in the module docstring):
#   "<words> Rupees Only"  /  "<words> Rupees and <words> Paise Only"


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        # The §6 fixture net, and the value the legacy sheet renders as `#NAME?`.
        (14035, "Fourteen Thousand and Thirty Five Rupees Only"),
        # "and" attaches to the sub-hundred group only; 500 is a hundreds group.
        (58500, "Fifty Eight Thousand Five Hundred Rupees Only"),
        (0, "Zero Rupees Only"),
        # Lakh boundary.
        (99999, "Ninety Nine Thousand Nine Hundred and Ninety Nine Rupees Only"),
        (100000, "One Lakh Rupees Only"),
        (100001, "One Lakh and One Rupees Only"),
        # Crore boundary.
        (9999999, "Ninety Nine Lakh Ninety Nine Thousand Nine Hundred and Ninety Nine Rupees Only"),
        (10000000, "One Crore Rupees Only"),
        (10000001, "One Crore and One Rupees Only"),
        # Indian 2-2-3 grouping made explicit: 12,34,567.
        (
            1234567,
            "Twelve Lakh Thirty Four Thousand Five Hundred and Sixty Seven Rupees Only",
        ),
        # Above 99 crore the crore count is itself worded Indian-style.
        (1000000000, "One Hundred Crore Rupees Only"),
        # Small values with and without the "and".
        (5, "Five Rupees Only"),
        (105, "One Hundred and Five Rupees Only"),
        (500, "Five Hundred Rupees Only"),
        (20, "Twenty Rupees Only"),
        (19, "Nineteen Rupees Only"),
    ],
)
def test_amount_in_words(amount: int, expected: str) -> None:
    assert amount_in_words(Decimal(amount)) == expected


def test_amount_in_words_never_renders_name_error() -> None:
    """CLAUDE.md §6: the legacy sheet prints `#NAME?` from a missing Excel macro.

    A payout document that prints `#NAME?` where the payable amount belongs is
    not a document. Whatever this function returns, it is words.
    """
    for value in (0, 1, 14035, 58500, 12345678):
        rendered = amount_in_words(Decimal(value))
        assert "#" not in rendered
        assert rendered.endswith("Rupees Only") or rendered.endswith("Paise Only")


def test_amount_in_words_with_paise() -> None:
    assert amount_in_words(Decimal("15483.87")) == (
        "Fifteen Thousand Four Hundred and Eighty Three Rupees " "and Eighty Seven Paise Only"
    )


def test_amount_in_words_negative_keeps_its_sign() -> None:
    """A negative net is a §7 block, but dropping the sign would turn a recovery
    into a payment."""
    assert amount_in_words(Decimal(-500)).startswith("Minus ")


# --- Indian digit grouping ---------------------------------------------------


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        (0, "0"),
        (999, "999"),
        (1000, "1,000"),
        (100000, "1,00,000"),
        (1234567, "12,34,567"),
        (10000000, "1,00,00,000"),
        (Decimal("15483.87"), "15,483.87"),
        (Decimal("-14035"), "-14,035"),
    ],
)
def test_format_indian(amount: object, expected: str) -> None:
    """Not `format(n, ',')` — that groups in threes and prints 1,234,567, which
    no Indian invoice reader parses at a glance."""
    assert format_indian(Decimal(amount)) == expected  # type: ignore[arg-type]


def test_zero_constant_is_decimal() -> None:
    assert isinstance(ZERO, Decimal)
