"""Money primitives. `Decimal` only — CLAUDE.md R7 admits no exceptions.

CLAUDE.md §3: `domain/` imports nothing from `db/`, `api/` or `agents/`. Zero I/O.

Two failure modes this module exists to prevent:

1. **A float reaching the arithmetic.** `float` cannot represent 0.1, so a single
   `float()` anywhere in the chain silently perturbs every downstream figure and
   the error is invisible until Finance reconciles by hand. `money()` therefore
   *rejects* `float` at the boundary rather than converting it — a conversion
   would launder the defect instead of surfacing it.
2. **Rounding more than once.** CLAUDE.md R6: full precision through every
   intermediate, one rounding at net pay. `round_rupees()` is deliberately the
   only rounding helper here, and nothing in this module rounds implicitly.

`amount_in_words()` is Python, not an Excel macro. The legacy sheet renders
`#NAME?` because its `SpellNumber` macro is missing from the workbook; a payout
document that prints `#NAME?` where the payable amount belongs is not a document.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Final

__all__ = [
    "DEFAULT_TDS_RATE",
    "ONE",
    "PAISE",
    "ZERO",
    "amount_in_words",
    "format_indian",
    "money",
    "quantize_paise",
    "round_rupees",
]

ZERO: Final[Decimal] = Decimal(0)
ONE: Final[Decimal] = Decimal(1)

#: Two-place storage precision (CLAUDE.md §11: "Decimal, two-place storage,
#: whole-rupee display"). Used for persistence and display only — NEVER as an
#: intermediate step inside a calculation, which would be a second rounding.
PAISE: Final[Decimal] = Decimal("0.01")

#: Whole-rupee exponent for the single permitted rounding at net pay.
RUPEE: Final[Decimal] = Decimal(1)

#: CLAUDE.md §6 — "Default rate 0.10". Kept here rather than in the engine so a
#: statutory change is one edit, and so tests can reference the same constant the
#: engine uses instead of re-typing a literal that could drift.
DEFAULT_TDS_RATE: Final[Decimal] = Decimal("0.10")


def money(value: int | str | Decimal) -> Decimal:
    """Coerce a monetary literal to `Decimal`, refusing `float` outright.

    `float` is rejected rather than converted. `Decimal(0.1)` is
    `0.1000000000000000055511151231257827021181583404541015625`; accepting a
    float here would let that error into a payout and make the resulting rupee
    discrepancy untraceable to its source. A caller holding a float has a bug
    upstream, and this raises where that bug is cheap to find.

    `bool` is rejected too: it is an `int` subclass, and `money(True)` is far
    more likely to be a mistake than an intent to mean one rupee.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise TypeError("money() refuses bool — R7: use Decimal for monetary values")
    if isinstance(value, float):
        raise TypeError(
            "money() refuses float — CLAUDE.md R7: never use float for money. "
            "Pass a str or Decimal (e.g. money('0.10'), not money(0.10))."
        )
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        return Decimal(value)
    raise TypeError(f"money() cannot coerce {type(value).__name__}")


def round_rupees(amount: Decimal) -> Decimal:
    """Round to whole rupees, half away from zero.

    This is the ONE rounding permitted by CLAUDE.md R6, applied to net pay at the
    end of the chain. Banker's rounding (Python's `Decimal` default,
    `ROUND_HALF_EVEN`) is wrong here: Indian payroll and the legacy spreadsheet
    both round half up, and a half-even net would disagree with the sheet Finance
    reconciles against roughly half the time a .50 lands.

    The returned value must never re-enter a calculation.
    """
    return amount.quantize(RUPEE, rounding=ROUND_HALF_UP)


def quantize_paise(amount: Decimal) -> Decimal:
    """Two-place value for storage or display.

    Display-side only. Feeding this back into arithmetic is the R6 violation the
    engine is written to avoid — see `PayoutResult.rate_per_day`, which is
    quantised for the sheet and never multiplied by anything.
    """
    return amount.quantize(PAISE, rounding=ROUND_HALF_UP)


# --- Indian numbering --------------------------------------------------------

_UNITS: Final[tuple[str, ...]] = (
    "",
    "One",
    "Two",
    "Three",
    "Four",
    "Five",
    "Six",
    "Seven",
    "Eight",
    "Nine",
    "Ten",
    "Eleven",
    "Twelve",
    "Thirteen",
    "Fourteen",
    "Fifteen",
    "Sixteen",
    "Seventeen",
    "Eighteen",
    "Nineteen",
)

_TENS: Final[tuple[str, ...]] = (
    "",
    "",
    "Twenty",
    "Thirty",
    "Forty",
    "Fifty",
    "Sixty",
    "Seventy",
    "Eighty",
    "Ninety",
)


def _under_hundred(n: int) -> str:
    """0..99 in words. Returns "" for 0 so callers can test truthiness."""
    if n < 20:
        return _UNITS[n]
    tens, units = divmod(n, 10)
    return _TENS[tens] if units == 0 else f"{_TENS[tens]} {_UNITS[units]}"


def _under_thousand(n: int) -> list[str]:
    """0..999 as [hundreds?, remainder?] components, each already worded."""
    parts: list[str] = []
    hundreds, rest = divmod(n, 100)
    if hundreds:
        parts.append(f"{_UNITS[hundreds]} Hundred")
    if rest:
        parts.append(_under_hundred(rest))
    return parts


def _integer_in_words(n: int) -> str:
    """Indian numbering: crore / lakh / thousand / hundred, then the sub-hundred
    remainder joined with "and".

    Indian grouping is 2-2-3 above the hundreds (12,34,567) rather than the
    Western 3-3, so "lakh" and "crore" are real place names here and not a
    formatting preference. Above 99 crore the count of crores is itself worded
    with Indian grouping — 1,00,00,00,000 reads "One Hundred Crore" — which falls
    out of recursing on the crore count.
    """
    if n == 0:
        return "Zero"

    crore, rest = divmod(n, 10_000_000)
    lakh, rest = divmod(rest, 100_000)
    thousand, rest = divmod(rest, 1_000)

    parts: list[str] = []
    if crore:
        parts.append(f"{_integer_in_words(crore)} Crore")
    if lakh:
        parts.append(f"{_under_hundred(lakh)} Lakh")
    if thousand:
        parts.append(f"{_under_hundred(thousand)} Thousand")

    hundreds, sub_hundred = divmod(rest, 100)
    if hundreds:
        parts.append(f"{_UNITS[hundreds]} Hundred")

    if sub_hundred:
        # "and" precedes the final sub-hundred group only when something came
        # before it: "Fourteen Thousand and Thirty Five", but plain "Thirty Five".
        tail = _under_hundred(sub_hundred)
        parts.append(f"and {tail}" if parts else tail)

    return " ".join(parts)


def amount_in_words(amount: Decimal) -> str:
    """Render a rupee amount in Indian numbering words.

    Format, fixed and documented so the invoice generator (Wave 3) and any test
    can rely on it:

        "<Rupee words> Rupees Only"
        "<Rupee words> Rupees and <Paise words> Paise Only"   (when paise != 0)

    Examples::

        14035     -> "Fourteen Thousand and Thirty Five Rupees Only"
        100000    -> "One Lakh Rupees Only"
        10000000  -> "One Crore Rupees Only"
        0         -> "Zero Rupees Only"
        1234567   -> "Twelve Lakh Thirty Four Thousand Five Hundred and Sixty
                      Seven Rupees Only"

    The amount is quantised to paise for wording. That is a *display* rounding on
    a value that has already been through `round_rupees()` in the normal payout
    path, not a second rounding of a live intermediate.

    Negative amounts are prefixed "Minus". A negative net is a §7 blocking
    failure, so this should never reach an invoice — but silently dropping the
    sign would turn a recovery into a payment.
    """
    sign = "Minus " if amount < ZERO else ""
    whole, frac = divmod(abs(quantize_paise(amount)), ONE)
    rupees = int(whole)
    paise = int((frac * 100).to_integral_value())

    words = f"{sign}{_integer_in_words(rupees)} Rupees"
    if paise:
        words = f"{words} and {_integer_in_words(paise)} Paise"
    return f"{words} Only"


def format_indian(amount: Decimal) -> str:
    """Group digits Indian-style: 12,34,567.89 — last three, then twos.

    Python's `format(n, ',')` groups in threes and would print 1,234,567, which
    no Indian invoice reader parses at a glance.
    """
    sign = "-" if amount < ZERO else ""
    text = str(quantize_paise(abs(amount)))
    whole, _, frac = text.partition(".")
    if frac == "00":
        # Whole rupees print without a paise part — §11: "two-place storage,
        # whole-rupee display". A net pay of 14035 reads "14,035", not "14,035.00".
        frac = ""

    if len(whole) <= 3:
        grouped = whole
    else:
        head, tail = whole[:-3], whole[-3:]
        chunks: list[str] = []
        while len(head) > 2:
            head, chunk = head[:-2], head[-2:]
            chunks.insert(0, chunk)
        if head:
            chunks.insert(0, head)
        grouped = f"{','.join(chunks)},{tail}"

    return f"{sign}{grouped}.{frac}" if frac else f"{sign}{grouped}"
