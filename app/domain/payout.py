"""Payout input and result dataclasses. CLAUDE.md §6.

CLAUDE.md §3: zero I/O, no imports from `db/`, `api/` or `agents/`. These are the
structured input the engine takes and the structured result it returns; the
caller fetches, the engine computes, nobody in this package opens a connection.

CLAUDE.md R2: no LLM is anywhere near this. An agent may *explain* a number in
`PayoutResult`; it may never produce one. There is deliberately no field here
that an agent could write to.

Plain frozen dataclasses rather than Pydantic: there is no serialisation boundary
to validate at, and Pydantic v2 will happily coerce a `float` into a `Decimal`
field, which is precisely the R7 violation this layer must refuse. `money()`
raises on float instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import ClassVar

from app.domain.enums import ProgramType, RateBasis
from app.domain.money import DEFAULT_TDS_RATE, ZERO

__all__ = ["PayoutInput", "PayoutResult"]


@dataclass(frozen=True, slots=True)
class PayoutInput:
    """Everything the engine needs, and nothing it does not.

    Notably absent: trainer name. CLAUDE.md §6 — "Trainer identity is PAN. Never
    match trainers by name string." Identity belongs to the validation context
    and the invoice, not to the arithmetic.

    Every monetary field is a `Decimal` (R7). `__post_init__` enforces it rather
    than trusting the annotation, because a dataclass annotation is documentation
    and a `float` slipped in from a JSON parse would otherwise sail straight into
    the multiplication.
    """

    program_type: ProgramType
    rate_basis: RateBasis

    #: The engagement rate from the signed work order — per day (CRT) or per
    #: month (bCAP). §7 requires this to match the WO rate; that check is a
    #: validator's job, not the engine's.
    rate: Decimal

    #: From `app.domain.attendance.payable_days`. Decimal because half days exist.
    payable_days: Decimal

    #: Calendar days of the payout month — 31 for July, not the length of the
    #: payout period. A 6-day period inside a 31-day month prorates over 31.
    days_in_month: int

    period_start: date | None = None
    period_end: date | None = None

    ta_da: Decimal = ZERO
    accommodation: Decimal = ZERO
    travel_reimbursement: Decimal = ZERO
    deductions: Decimal = ZERO

    #: CLAUDE.md §6 default 0.10. Overridable because a trainer with a lower-
    #: deduction certificate exists; never overridable to a float.
    tds_rate: Decimal = DEFAULT_TDS_RATE

    def __post_init__(self) -> None:
        for name in (
            "rate",
            "payable_days",
            "ta_da",
            "accommodation",
            "travel_reimbursement",
            "deductions",
            "tds_rate",
        ):
            value = getattr(self, name)
            # `bool` is an `int` subclass and `float` is not a `Decimal`, so the
            # isinstance check must be positive ("is it a Decimal?") rather than
            # negative ("is it a float?") — otherwise `int` and `bool` pass.
            if not isinstance(value, Decimal):
                raise TypeError(
                    f"PayoutInput.{name} must be Decimal, got {type(value).__name__} "
                    "— CLAUDE.md R7: never use float for money"
                )
        if self.days_in_month <= 0:
            raise ValueError("days_in_month must be positive")
        if self.payable_days < ZERO:
            raise ValueError("payable_days cannot be negative")

    @property
    def reimbursements(self) -> Decimal:
        """TA&DA + accommodation + travel. These add to gross but NOT to the TDS
        base — CLAUDE.md §6, "TDS excludes reimbursements. This is why the sample
        sheet shows 1,548 and not 1,558."
        """
        return self.ta_da + self.accommodation + self.travel_reimbursement


@dataclass(frozen=True, slots=True)
class PayoutResult:
    """The computed payout, every intermediate preserved.

    Intermediates are returned rather than discarded so a dispute can be walked
    line by line and so the remuneration sheet can print the same numbers the
    engine used. `net_unrounded` sits next to `net` for exactly that reason: it
    is the evidence that the single R6 rounding happened where it was supposed to.
    """

    rate_per_day: Decimal
    """DISPLAY ONLY on the per-month path.

    CLAUDE.md R6 and §6: on `PER_MONTH` this is `rate / days_in_month`, a
    repeating decimal, and it MUST NOT re-enter the maths. `earned` is computed
    as `rate * payable_days / days_in_month` — multiply first — because
    `(65000/31) * 31` lands on 64999.99999…, the artifact the legacy spreadsheet
    carries. This field exists for the sheet column and nothing else.
    """

    earned: Decimal
    reimbursements: Decimal
    gross: Decimal
    tds: Decimal
    deductions: Decimal
    net_unrounded: Decimal
    net: Decimal
    """The only rounded value in this object (R6). Whole rupees, half up."""

    payable_days: Decimal
    days_in_month: int
    rate_basis: RateBasis
    tds_rate: Decimal

    #: Field names carrying money or day counts, for the R7 guard test and for
    #: any caller that wants to assert the whole result is Decimal before it
    #: crosses a layer boundary. `ClassVar`, so it is not an init argument and
    #: not a slot — it describes the class, it is not payout data.
    NUMERIC_FIELDS: ClassVar[tuple[str, ...]] = (
        "rate_per_day",
        "earned",
        "reimbursements",
        "gross",
        "tds",
        "deductions",
        "net_unrounded",
        "net",
        "payable_days",
        "tds_rate",
    )
