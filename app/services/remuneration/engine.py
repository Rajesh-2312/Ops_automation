"""The payout engine. CLAUDE.md §6 is its specification.

CLAUDE.md R2: "All monetary arithmetic lives in `services/remuneration/engine.py`,
is pure Python, uses `Decimal`, and is unit-tested. An agent may explain a number.
It may never produce one." Nothing in this module imports `app.core.llm`, and
nothing ever should.

CLAUDE.md §3 / R1: the engine takes a `PayoutInput` and returns a `PayoutResult`.
It opens no connection, reads no config, and has no notion of a trainer record.
The caller fetches; the engine computes.

The canonical chain::

    per_month:  rate_per_day = rate / days_in_month        (DISPLAY ONLY)
                earned       = rate * payable_days / days_in_month
    per_day:    rate_per_day = rate
                earned       = rate * payable_days

    gross   = earned + ta_da + accommodation + travel_reimb
    tds     = earned * tds_rate          # base is EARNED, never gross
    net     = gross - deductions - tds
    ROUND(net, 0)                        # the only rounding

Three rules are load-bearing and each has a regression test:

1. **Multiply before dividing** on the per-month path. `Decimal(65000)/31*31` is
   `64999.99999999999999999999999`, not 65000. The legacy spreadsheet carries
   exactly this artifact (`65000.00000000001` appears in the shipped invoice
   sheet). `rate * payable_days / days_in_month` recombines exactly when
   `payable_days == days_in_month`; divide-first does not.
2. **`rate_per_day` is display-only on the per-month path** and never re-enters
   the maths (R6). The legacy process rounds it and multiplies by it, and loses
   money in both directions: VEMA's sheet shows per-day 2581, `2581 x 6 = 15,486`
   against a correct 15,483.87; Vijay Chalagala's cell literally reads
   `2258*26 = 58708` against a correct 58,709.68 — ₹1.68 underpaid.
3. **TDS is levied on `earned`, never on `gross`.** Reimbursements are the
   trainer's own money coming back and are not income. CLAUDE.md §6: "This is why
   the sample sheet shows 1,548 and not 1,558."
"""

from __future__ import annotations

from decimal import Decimal

from app.domain.enums import RateBasis
from app.domain.money import quantize_paise, round_rupees
from app.domain.payout import PayoutInput, PayoutResult

__all__ = ["compute_payout"]


def compute_payout(payout: PayoutInput) -> PayoutResult:
    """Compute a single trainer-month payout. Pure; no I/O; `Decimal` throughout.

    Validation is NOT performed here. `app.services.remuneration.validators` owns
    the §7 gates, and it needs this result (net > 0, deviation from trailing
    average) to run. Keeping them apart means the engine cannot be tempted to
    "fix" bad input by clamping it — a payout that should be blocked must compute
    a blockable number, not a plausible one.
    """
    earned, rate_per_day = _earned_and_display_rate(payout)

    reimbursements = payout.reimbursements
    gross = earned + reimbursements

    # TDS base is EARNED. Using `gross` here would tax the trainer's own travel
    # money: for the VEMA fixture it produces 1,558.39 instead of 1,548.39 and
    # short-pays by ₹10.
    tds = earned * payout.tds_rate

    net_unrounded = gross - payout.deductions - tds

    return PayoutResult(
        # Quantised for the sheet's `Commercials per day` column. This value has
        # already NOT been used above — `earned` was computed from `rate`.
        rate_per_day=quantize_paise(rate_per_day),
        earned=earned,
        reimbursements=reimbursements,
        gross=gross,
        tds=tds,
        deductions=payout.deductions,
        net_unrounded=net_unrounded,
        # R6: the one and only rounding in the whole chain.
        net=round_rupees(net_unrounded),
        payable_days=payout.payable_days,
        days_in_month=payout.days_in_month,
        rate_basis=payout.rate_basis,
        tds_rate=payout.tds_rate,
    )


def _earned_and_display_rate(payout: PayoutInput) -> tuple[Decimal, Decimal]:
    """Return `(earned, rate_per_day)` for the engagement's rate basis.

    The two are computed independently on the per-month path. That independence
    is the point: `rate_per_day` is the sheet's display column and `earned` is
    the money, and the only way to guarantee the display value never contaminates
    the money is for the money never to reference it.
    """
    days = payout.payable_days
    rate = payout.rate

    if payout.rate_basis is RateBasis.PER_MONTH:
        divisor = Decimal(payout.days_in_month)

        # DISPLAY ONLY (R6, §6). Repeating decimal for most months —
        # 80000/31 = 2580.645161… — and rounding it to 2581 for the sheet is
        # exactly what the legacy process then wrongly multiplies by.
        rate_per_day = rate / divisor

        # MULTIPLY BEFORE DIVIDING. Written as one expression evaluated
        # left-to-right so nobody can "simplify" it into a two-step that reuses
        # `rate_per_day`. `Decimal(65000) * 31 / 31` is exactly 65000;
        # `Decimal(65000) / 31 * 31` is 64999.99999999999999999999999.
        earned = rate * days / divisor
        return earned, rate_per_day

    if payout.rate_basis is RateBasis.PER_DAY:
        # CRT. No divisor at all — the day rate IS the rate, so there is no
        # repeating decimal and no display/money split to get wrong.
        # Confirmed against real form data: 2700/day x 7 payable days over an
        # 8-calendar-day period = 18,900 (not 8 days' worth).
        return rate * days, rate

    raise ValueError(f"unhandled rate basis {payout.rate_basis!r}")  # pragma: no cover
