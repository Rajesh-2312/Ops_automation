"""Tests for `app.services.remuneration.invoice_no` — CLAUDE.md §6.

Format `{PAN[0:4]}/{FY}/{MON}{seq}`, FY April-March, derived from the payout
month. The April boundary gets its own parametrised sweep because that is the
one date arithmetic in the module and getting it wrong mis-stamps a whole year.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.services.remuneration.invoice_no import (
    InvoiceNumber,
    build_invoice_number,
    fiscal_year,
    month_token,
)

VALID_PAN = "BCDPS1234K"


# --- Fiscal year across the April boundary ----------------------------------


@pytest.mark.parametrize(
    ("payout_month", "expected"),
    [
        # The §6 worked example.
        (date(2026, 7, 1), "26-27"),
        # The brief's second example: February 2026 is still FY 25-26.
        (date(2026, 2, 1), "25-26"),
        # The boundary itself, day by day.
        (date(2026, 3, 31), "25-26"),
        (date(2026, 4, 1), "26-27"),
        (date(2027, 3, 31), "26-27"),
        (date(2027, 4, 1), "27-28"),
        # January and March sit in the PREVIOUS calendar year's FY.
        (date(2026, 1, 15), "25-26"),
        (date(2026, 12, 31), "26-27"),
        # Century rollover of the two-digit form.
        (date(2099, 4, 1), "99-00"),
        (date(2100, 3, 1), "99-00"),
    ],
)
def test_fiscal_year_derivation(payout_month: date, expected: str) -> None:
    assert fiscal_year(payout_month) == expected


def test_fiscal_year_is_derived_from_the_payout_month_not_today() -> None:
    """A July payout processed in April must still carry the July FY. Deriving
    from `date.today()` would stamp the new year onto old work."""
    assert fiscal_year(date(2026, 7, 31)) == fiscal_year(date(2026, 7, 1))
    assert fiscal_year(date(2026, 7, 1)) != fiscal_year(date(2026, 3, 1))


@pytest.mark.parametrize(
    ("month", "token"),
    [(1, "JAN"), (4, "APR"), (7, "JUL"), (9, "SEP"), (12, "DEC")],
)
def test_month_token_is_locale_independent(month: int, token: str) -> None:
    """Not `strftime("%b")` — a non-English server locale would emit a token no
    one can look up in the invoice log."""
    assert month_token(date(2026, month, 1)) == token


# --- Building ----------------------------------------------------------------


def test_build_invoice_number_matches_the_section_6_example() -> None:
    invoice = build_invoice_number(VALID_PAN, date(2026, 7, 1), seq=1)
    assert str(invoice) == "BCDP/26-27/JUL1"
    assert invoice.pan_prefix == "BCDP"
    assert invoice.key == ("BCDP", "26-27", "JUL", 1)


def test_sequence_distinguishes_reissues_within_a_month() -> None:
    first = build_invoice_number(VALID_PAN, date(2026, 7, 1), seq=1)
    second = build_invoice_number(VALID_PAN, date(2026, 7, 1), seq=2)
    assert str(second) == "BCDP/26-27/JUL2"
    assert first.key != second.key


def test_pan_is_normalised_before_use() -> None:
    assert str(build_invoice_number("  bcdps1234k ", date(2026, 7, 1))) == "BCDP/26-27/JUL1"


@pytest.mark.parametrize(
    "bad_pan",
    ["BCDPS1234", "BCDPS1234KK", "BCD PS1234K", "1234567890", "", "BCDP51234K"],
)
def test_malformed_pan_is_refused(bad_pan: str) -> None:
    """A malformed PAN produces a plausible-looking invoice number that then
    collides with a different trainer's — a failure that surfaces months later at
    reconciliation."""
    with pytest.raises(ValueError, match="invalid PAN"):
        build_invoice_number(bad_pan, date(2026, 7, 1))


def test_seq_must_be_positive() -> None:
    with pytest.raises(ValueError, match="seq"):
        build_invoice_number(VALID_PAN, date(2026, 7, 1), seq=0)


# --- Parsing legacy numbers --------------------------------------------------


def test_parse_round_trips() -> None:
    parsed = InvoiceNumber.parse("BCDP/26-27/JUL1")
    assert str(parsed) == "BCDP/26-27/JUL1"
    assert parsed.seq == 1


def test_legacy_month_word_is_rejected_as_malformed() -> None:
    """Observed in the shipped sample: `ANSP/25-26/JULY` — `JULY` where `JUL1`
    belongs, because the Google Form placeholder taught the wrong shape."""
    with pytest.raises(ValueError, match="malformed"):
        InvoiceNumber.parse("ANSP/25-26/JULY")


def test_a_legacy_number_can_parse_and_still_be_wrong() -> None:
    """`BRMP/25-26/JUL1` is well-formed and names the wrong fiscal year for a
    July-2026 payout — the form's placeholder text reads `(XXXX/25-26/SEP1)` and
    people copy the `25-26` verbatim.

    Regenerated numbers will therefore NOT match ~1 in 5 historical ones. During
    the Phase 2 parallel run that is the system being right.
    """
    legacy = InvoiceNumber.parse("BRMP/25-26/JUL1")
    regenerated = build_invoice_number("BRMPS9876Q", date(2026, 7, 1))
    assert legacy.fiscal_year == "25-26"
    assert regenerated.fiscal_year == "26-27"
    assert legacy.key != regenerated.key


@pytest.mark.parametrize(
    "bad", ["BCD/26-27/JUL1", "BCDP/2026-27/JUL1", "BCDP/26-27/JUL", "BCDP-26-27-JUL1", ""]
)
def test_parse_rejects_malformed_shapes(bad: str) -> None:
    with pytest.raises(ValueError, match="malformed"):
        InvoiceNumber.parse(bad)
