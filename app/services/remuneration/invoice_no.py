"""Invoice number generation. CLAUDE.md §6.

Format: ``{PAN[0:4]}/{FY}/{MON}{seq}`` — e.g. ``BCDP/26-27/JUL1``.

The fiscal year runs April to March and derives from the **payout month**, not
from today's date and not from the invoice date:

    July 2026     -> FY 26-27   (April 2026 .. March 2027)
    February 2026 -> FY 25-26   (April 2025 .. March 2026)

Uniqueness is on ``(pan, fiscal_year, month, seq)``. This module generates the
string; the **database** enforces the constraint. Doing it the other way round —
scanning for the next free sequence in Python — races under concurrent payout
runs and issues the same number twice.

**Generate, never accept.** Invoice numbers are trainer-supplied in the legacy
process and roughly one in five is malformed, because the Google Form's own
placeholder label reads ``Invoice Number  (XXXX/25-26/SEP1)`` and people copy the
``25-26`` verbatim regardless of the payout month. Observed in the shipped
samples: ``ANSP/25-26/JULY`` (wrong FY *and* ``JULY`` where ``JUL1`` belongs) and
``BRMP/25-26/JUL1`` against a July-2026 payout.

Consequence: **historical invoice numbers will not always match regenerated
ones.** During the Phase 2 parallel run (CLAUDE.md §13) a mismatch against a
legacy number is the system being right and the spreadsheet being wrong. Do not
"fix" this by echoing back whatever the form captured; that would import the
defect and break the uniqueness constraint at the same time.

CLAUDE.md §3: zero I/O. `parse()` exists so a migration can *inspect* legacy
numbers, not so the system can trust them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Final

__all__ = [
    "INVOICE_NUMBER_RE",
    "InvoiceNumber",
    "build_invoice_number",
    "fiscal_year",
    "month_token",
]

#: The fiscal year starts in April. Months 4..12 belong to FY (year, year+1);
#: months 1..3 belong to FY (year-1, year).
FY_START_MONTH: Final[int] = 4

#: Three-letter uppercase month tokens, indexed 1..12. Explicit rather than
#: `strftime("%b").upper()` because `%b` is locale-dependent — a server with a
#: non-English locale would emit a token no one can look up in the invoice log.
_MONTH_TOKENS: Final[tuple[str, ...]] = (
    "",
    "JAN",
    "FEB",
    "MAR",
    "APR",
    "MAY",
    "JUN",
    "JUL",
    "AUG",
    "SEP",
    "OCT",
    "NOV",
    "DEC",
)

#: A well-formed number. `JULY` fails this deliberately — see the module note.
INVOICE_NUMBER_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<pan_prefix>[A-Z]{4})/(?P<fiscal_year>\d{2}-\d{2})/"
    r"(?P<month>JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(?P<seq>\d+)$"
)

_PAN_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Z]{5}\d{4}[A-Z]$")


@dataclass(frozen=True, slots=True)
class InvoiceNumber:
    """A parsed invoice number and its uniqueness key.

    `key` mirrors the database's unique constraint exactly, so the caller can
    dedupe in-flight rows against the same tuple the DB will reject on.
    """

    pan_prefix: str
    fiscal_year: str
    month: str
    seq: int

    def __str__(self) -> str:
        return f"{self.pan_prefix}/{self.fiscal_year}/{self.month}{self.seq}"

    @property
    def key(self) -> tuple[str, str, str, int]:
        """`(pan_prefix, fiscal_year, month, seq)` — the unique constraint."""
        return (self.pan_prefix, self.fiscal_year, self.month, self.seq)

    @classmethod
    def parse(cls, value: str) -> InvoiceNumber:
        """Parse a well-formed number, raising on anything else.

        For inspecting legacy data only. A parse success does NOT mean the number
        is correct — `BRMP/25-26/JUL1` parses cleanly and still names the wrong
        fiscal year for a July-2026 payout. Use `build_invoice_number()` and
        compare if you need to know whether a legacy number was right.
        """
        match = INVOICE_NUMBER_RE.match(value.strip())
        if match is None:
            raise ValueError(
                f"malformed invoice number {value!r} — expected PAN[0:4]/YY-YY/MONn, "
                "e.g. BCDP/26-27/JUL1"
            )
        return cls(
            pan_prefix=match["pan_prefix"],
            fiscal_year=match["fiscal_year"],
            month=match["month"],
            seq=int(match["seq"]),
        )


def fiscal_year(payout_month: date) -> str:
    """Indian fiscal year of a payout month, as ``YY-YY``.

    April 2026 .. March 2027 is ``26-27``. The April boundary is the whole point:
    31 March 2027 is ``26-27`` and 1 April 2027 is ``27-28``, so a payout run
    that straddles a year end must derive per-row from the payout month rather
    than once per batch.

    Derives from the payout month, never from `date.today()`. A July payout
    processed late in April would otherwise be stamped with the new FY.
    """
    year = payout_month.year if payout_month.month >= FY_START_MONTH else payout_month.year - 1
    return f"{year % 100:02d}-{(year + 1) % 100:02d}"


def month_token(payout_month: date) -> str:
    """Three-letter uppercase month token, e.g. ``JUL``. Locale-independent."""
    return _MONTH_TOKENS[payout_month.month]


def build_invoice_number(pan: str, payout_month: date, seq: int = 1) -> InvoiceNumber:
    """Generate the invoice number for a trainer-month.

    `pan` is the full 10-character PAN; only the first four characters appear in
    the number. CLAUDE.md §6: "Trainer identity is PAN. It is the only stable key
    present in every legacy sheet and it seeds the invoice number. Never match
    trainers by name string."

    `seq` distinguishes multiple invoices for the same trainer in the same month
    (a corrected re-issue, or two engagements). It starts at 1. The caller is
    responsible for choosing it — typically `max(existing) + 1` inside the same
    transaction that inserts the row, so the unique index arbitrates the race.

    The PAN is validated to the statutory shape here because a malformed PAN
    produces a plausible-looking invoice number that then collides with a
    different trainer's — a failure that surfaces months later at reconciliation
    rather than now.
    """
    normalised = pan.strip().upper()
    if not _PAN_RE.match(normalised):
        raise ValueError(
            f"invalid PAN {pan!r} — expected 10 characters, AAAAA9999A. "
            "The invoice number is seeded from PAN[0:4]; a malformed PAN collides."
        )
    if seq < 1:
        raise ValueError(f"seq must be >= 1, got {seq}")

    return InvoiceNumber(
        pan_prefix=normalised[:4],
        fiscal_year=fiscal_year(payout_month),
        month=month_token(payout_month),
        seq=seq,
    )
