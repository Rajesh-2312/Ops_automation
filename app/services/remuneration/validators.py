"""Payout validation gates. CLAUDE.md §7 is the specification.

Blocking — the cycle cannot reach `PENDING_APPROVAL`:

* signed work order on file, and the payout period inside `wo_valid_from..wo_valid_to`
* ZOHO account exists
* PAN (10 chars), bank account, IFSC (11 chars) present and well-formed
* `payable_days <= days_in_month`
* attendance complete for the period (**CRT only**)
* invoice number not already issued
* engagement rate matches the rate in the signed work order
* net pay > 0

Warning — permitted, but requires a stated reason recorded against the run:

* net pay deviates >20% from the trailing 3-month average
* reimbursement claimed with zero payable days
* attendance incomplete (**bCAP**)

**Every gate is a pure function of a `PayoutContext`.** CLAUDE.md §3 and R1: the
caller fetches from the systems of record and passes structured data in; nothing
here opens a connection, and no gate can be "satisfied" by something a model
said. That separation is what makes each gate testable in isolation, which §12
requires — one passing case and one blocked case, each.

The attendance gate is the reason severity is decided per-gate rather than
per-code. CLAUDE.md §5: an unmarked day silently PAYS a bCAP trainer (the
retainer stands) and silently UNDERPAYS a CRT trainer (no `P`, no money). The
CRT failure mode is an underpayment nobody notices, so it blocks; the bCAP
failure mode is an overpayment a human can consciously accept, so it warns.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from types import MappingProxyType
from typing import Final

from app.domain.enums import ProgramType, ValidationCode, ValidationSeverity
from app.domain.money import ZERO
from app.domain.payout import PayoutResult

__all__ = [
    "GATES",
    "DEVIATION_THRESHOLD",
    "PayoutContext",
    "ValidationIssue",
    "ValidationReport",
    "validate_payout",
]

#: CLAUDE.md §7 — "Net pay deviates >20% from trailing 3-month average".
#: Strictly greater than: a payout exactly 20% off does not warn.
DEVIATION_THRESHOLD: Final[Decimal] = Decimal("0.20")

#: Statutory shapes. PAN is 5 letters, 4 digits, 1 letter. IFSC is 4 letters,
#: then '0' (reserved by RBI), then 6 alphanumerics — 11 characters.
#: §7 says "present and well-formed", so length alone is not enough: a 10-char
#: PAN of the wrong shape still seeds a colliding invoice number, and an 11-char
#: IFSC without the reserved zero fails at the bank, after release.
_PAN_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Z]{5}\d{4}[A-Z]$")
_IFSC_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")

PAN_LENGTH: Final[int] = 10
IFSC_LENGTH: Final[int] = 11


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """One failed gate.

    `detail` carries the operative numbers so the Payout agent (autonomy level 2,
    draft only — §8) can *explain* the failure without recomputing anything. R1:
    a message may only contain figures that were passed to it as structured
    input, and this is that input.
    """

    code: ValidationCode
    severity: ValidationSeverity
    message: str
    detail: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))

    @property
    def is_blocking(self) -> bool:
        return self.severity is ValidationSeverity.BLOCKING


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """The outcome of every gate, in declaration order."""

    issues: tuple[ValidationIssue, ...]

    @property
    def blocking(self) -> tuple[ValidationIssue, ...]:
        return tuple(i for i in self.issues if i.severity is ValidationSeverity.BLOCKING)

    @property
    def warnings(self) -> tuple[ValidationIssue, ...]:
        return tuple(i for i in self.issues if i.severity is ValidationSeverity.WARNING)

    @property
    def is_blocked(self) -> bool:
        """True if the cycle cannot reach `PENDING_APPROVAL`."""
        return bool(self.blocking)

    @property
    def requires_reason(self) -> tuple[ValidationCode, ...]:
        """Warning codes needing a stated reason before submission (§7)."""
        return tuple(i.code for i in self.warnings)

    def can_submit(self, stated_reasons: Mapping[ValidationCode, str] | None = None) -> bool:
        """Whether this payout may move DRAFT -> PENDING_APPROVAL.

        Requires zero blocking issues AND a non-empty stated reason for every
        warning. An empty or whitespace-only reason does not count — §7 says
        "requires a stated reason", and a blank string is how a required field
        becomes a formality.
        """
        if self.is_blocked:
            return False
        reasons = stated_reasons or {}
        return all(reasons.get(code, "").strip() for code in self.requires_reason)


@dataclass(frozen=True, slots=True)
class PayoutContext:
    """Everything the §7 gates need, already fetched.

    Deliberately plain data. The caller resolves the work order, the ZOHO
    account, the bank details, the issued-invoice set and the trailing net pays;
    the validators only compare. This is what keeps the gates unit-testable
    without a database (§3) and what stops a gate from quietly becoming a query.
    """

    program_type: ProgramType
    result: PayoutResult

    period_start: date
    period_end: date
    days_in_month: int

    #: Attendance completeness for the period, from
    #: `app.domain.attendance.PayableDays.is_complete`. Passed in rather than
    #: recomputed so the gate validates the same count the engine was given.
    attendance_complete: bool
    unmarked_days: int = 0

    # --- Work order ---------------------------------------------------------
    work_order_signed: bool = False
    wo_valid_from: date | None = None
    wo_valid_to: date | None = None
    #: The rate written in the signed work order, for the §7 rate-match gate.
    wo_rate: Decimal | None = None
    #: The rate actually used by the engine (`PayoutInput.rate`).
    engagement_rate: Decimal | None = None

    # --- Identity and payment rails ----------------------------------------
    zoho_account_id: str | None = None
    pan: str | None = None
    bank_account_number: str | None = None
    ifsc: str | None = None

    # --- Invoice ------------------------------------------------------------
    invoice_number: str | None = None
    #: Invoice numbers already issued. The database's unique constraint is the
    #: real enforcement (§6); this gate catches it before a human approves.
    issued_invoice_numbers: frozenset[str] = frozenset()

    # --- History ------------------------------------------------------------
    #: Net pay for up to the last three months, most recent first. Empty for a
    #: trainer's first payout — with no history there is nothing to deviate from,
    #: so the deviation gate stays silent rather than warning on every new joiner.
    trailing_net_pays: tuple[Decimal, ...] = ()


# --- Individual gates --------------------------------------------------------
# Each returns an issue or None. One function per §7 bullet, so §12's
# "one passing case and one blocked case" maps 1:1 onto a test.


def _issue(
    code: ValidationCode,
    severity: ValidationSeverity,
    message: str,
    **detail: object,
) -> ValidationIssue:
    return ValidationIssue(
        code=code,
        severity=severity,
        message=message,
        detail=MappingProxyType({k: str(v) for k, v in detail.items()}),
    )


def gate_work_order_signed(ctx: PayoutContext) -> ValidationIssue | None:
    """A signed work order must be on file. No WO, no payout."""
    if ctx.work_order_signed and ctx.wo_valid_from is not None and ctx.wo_valid_to is not None:
        return None
    return _issue(
        ValidationCode.WORK_ORDER_MISSING,
        ValidationSeverity.BLOCKING,
        "No signed work order on file for this engagement.",
        signed=ctx.work_order_signed,
        valid_from=ctx.wo_valid_from,
        valid_to=ctx.wo_valid_to,
    )


def gate_period_within_work_order(ctx: PayoutContext) -> ValidationIssue | None:
    """The payout period must sit inside `wo_valid_from..wo_valid_to`.

    Both bounds inclusive. Paying for a day outside the signed validity window is
    a contractual exposure, not a rounding question — a WO that expired mid-month
    means the tail of the month needs a fresh WO, not a wider tolerance.

    Silent when no WO is on file: `gate_work_order_signed` already blocks, and
    two blocks for one root cause reads as two separate problems to whoever has
    to fix it.
    """
    if ctx.wo_valid_from is None or ctx.wo_valid_to is None:
        return None
    if ctx.wo_valid_from <= ctx.period_start and ctx.period_end <= ctx.wo_valid_to:
        return None
    return _issue(
        ValidationCode.WORK_ORDER_PERIOD_MISMATCH,
        ValidationSeverity.BLOCKING,
        f"Payout period {ctx.period_start}..{ctx.period_end} falls outside the signed "
        f"work order validity {ctx.wo_valid_from}..{ctx.wo_valid_to}.",
        period_start=ctx.period_start,
        period_end=ctx.period_end,
        wo_valid_from=ctx.wo_valid_from,
        wo_valid_to=ctx.wo_valid_to,
    )


def gate_zoho_account(ctx: PayoutContext) -> ValidationIssue | None:
    """A ZOHO account must exist — without it Finance cannot book the payment."""
    if ctx.zoho_account_id and ctx.zoho_account_id.strip():
        return None
    return _issue(
        ValidationCode.ZOHO_ACCOUNT_MISSING,
        ValidationSeverity.BLOCKING,
        "No ZOHO account on file for this trainer.",
    )


def gate_pan(ctx: PayoutContext) -> ValidationIssue | None:
    """PAN present, 10 characters, statutory shape.

    §6: "Trainer identity is PAN." It is also the seed of the invoice number, so
    a wrong PAN is not one bad field — it is a wrong identity and a colliding
    invoice number at the same time.
    """
    pan = (ctx.pan or "").strip().upper()
    if len(pan) == PAN_LENGTH and _PAN_RE.match(pan):
        return None
    return _issue(
        ValidationCode.PAN_INVALID,
        ValidationSeverity.BLOCKING,
        f"PAN missing or malformed (expected {PAN_LENGTH} characters, AAAAA9999A).",
        pan_length=len(pan),
    )


def gate_bank_account(ctx: PayoutContext) -> ValidationIssue | None:
    """A bank account number must be present.

    No length or checksum rule: Indian account numbers run roughly 9-18 digits
    and vary by bank, so any length assertion invented here would block valid
    trainers. Presence and digits-only is the honest limit of what we can check.
    """
    account = (ctx.bank_account_number or "").strip()
    if account and account.isdigit():
        return None
    return _issue(
        ValidationCode.BANK_ACCOUNT_MISSING,
        ValidationSeverity.BLOCKING,
        "Bank account number missing or non-numeric.",
    )


def gate_ifsc(ctx: PayoutContext) -> ValidationIssue | None:
    """IFSC present, 11 characters, RBI shape (4 letters, `0`, 6 alphanumerics)."""
    ifsc = (ctx.ifsc or "").strip().upper()
    if len(ifsc) == IFSC_LENGTH and _IFSC_RE.match(ifsc):
        return None
    return _issue(
        ValidationCode.IFSC_INVALID,
        ValidationSeverity.BLOCKING,
        f"IFSC missing or malformed (expected {IFSC_LENGTH} characters, AAAA0XXXXXX).",
        ifsc_length=len(ifsc),
    )


def gate_payable_days_within_month(ctx: PayoutContext) -> ValidationIssue | None:
    """`payable_days <= days_in_month`.

    On the bCAP path `earned = rate * payable_days / days_in_month`, so a
    payable-day count above the month length pays more than the full monthly
    retainer. It is the arithmetic signature of a duplicated attendance row.
    """
    if ctx.result.payable_days <= Decimal(ctx.days_in_month):
        return None
    return _issue(
        ValidationCode.PAYABLE_DAYS_EXCEED_MONTH,
        ValidationSeverity.BLOCKING,
        f"Payable days {ctx.result.payable_days} exceed days in month " f"{ctx.days_in_month}.",
        payable_days=ctx.result.payable_days,
        days_in_month=ctx.days_in_month,
    )


def gate_attendance_complete(ctx: PayoutContext) -> ValidationIssue | None:
    """Attendance completeness. BLOCKING for CRT, WARNING for bCAP (§5, §7).

    This is the single most important asymmetry in the module. CRT counts payable
    days UP from `P` marks, so an unmarked day pays nothing and the trainer is
    underpaid by a clerical omission that produces no visible error — it must
    block. bCAP counts DOWN from the period length, so an unmarked day is paid
    and the risk is an overpayment a Manager can knowingly accept with a stated
    reason — it warns.

    Never "simplify" this into one severity. Blocking bCAP would stall every
    retainer payout over a missing weekend mark; warning on CRT would ship silent
    underpayments.
    """
    if ctx.attendance_complete:
        return None
    severity = (
        ValidationSeverity.BLOCKING
        if ctx.program_type is ProgramType.CRT
        else ValidationSeverity.WARNING
    )
    consequence = (
        "CRT counts payable days up from P marks, so each unmarked day underpays the trainer"
        if ctx.program_type is ProgramType.CRT
        else "bCAP counts down from period length, so each unmarked day is paid regardless"
    )
    return _issue(
        ValidationCode.ATTENDANCE_INCOMPLETE,
        severity,
        f"Attendance incomplete: {ctx.unmarked_days} unmarked day(s) in "
        f"{ctx.period_start}..{ctx.period_end}. {consequence}.",
        program_type=ctx.program_type.value,
        unmarked_days=ctx.unmarked_days,
    )


def gate_invoice_number_unissued(ctx: PayoutContext) -> ValidationIssue | None:
    """The invoice number must not already be issued.

    The database's `(pan, fiscal_year, month, seq)` unique constraint is the real
    guarantee (§6); this gate exists so the clash surfaces before a human spends
    time approving a cycle the insert will reject.
    """
    if ctx.invoice_number is None:
        return None
    if ctx.invoice_number not in ctx.issued_invoice_numbers:
        return None
    return _issue(
        ValidationCode.INVOICE_NUMBER_ALREADY_ISSUED,
        ValidationSeverity.BLOCKING,
        f"Invoice number {ctx.invoice_number} has already been issued.",
        invoice_number=ctx.invoice_number,
    )


def gate_rate_matches_work_order(ctx: PayoutContext) -> ValidationIssue | None:
    """The engagement rate must equal the rate in the signed work order.

    Compared with `!=` on `Decimal`, so 3500 and 3500.00 match by value while
    3500 and 3499.99 do not. The work order is the contract; if the engagement
    record disagrees with it, one of the two is wrong and neither the engine nor
    a human should guess which.

    Silent when the WO carries no rate — `gate_work_order_signed` covers that.
    """
    if ctx.wo_rate is None or ctx.engagement_rate is None:
        return None
    if ctx.wo_rate == ctx.engagement_rate:
        return None
    return _issue(
        ValidationCode.RATE_MISMATCH_WITH_WORK_ORDER,
        ValidationSeverity.BLOCKING,
        f"Engagement rate {ctx.engagement_rate} does not match the signed work order "
        f"rate {ctx.wo_rate}.",
        engagement_rate=ctx.engagement_rate,
        wo_rate=ctx.wo_rate,
    )


def gate_net_pay_positive(ctx: PayoutContext) -> ValidationIssue | None:
    """Net pay must be > 0.

    Zero is blocked as well as negative. A zero net is either a data error or a
    payout that should not have been raised; either way it must not travel
    through the approval chain and land as a ₹0 bank instruction.
    """
    if ctx.result.net > ZERO:
        return None
    return _issue(
        ValidationCode.NET_PAY_NOT_POSITIVE,
        ValidationSeverity.BLOCKING,
        f"Net pay is {ctx.result.net}; must be greater than zero.",
        net=ctx.result.net,
    )


def gate_net_deviation(ctx: PayoutContext) -> ValidationIssue | None:
    """WARNING: net pay deviates >20% from the trailing 3-month average.

    Silent with no history (a first payout has nothing to compare against) and
    with a zero average (dividing by it is undefined, and a jump from zero is
    better caught by the payable-days and rate gates).

    `Decimal` throughout, including the threshold — a `0.20` float literal here
    would make the boundary case non-deterministic, which is exactly the kind of
    thing that makes a warning gate untrusted.
    """
    history = ctx.trailing_net_pays[:3]
    if not history:
        return None
    average = sum(history, ZERO) / Decimal(len(history))
    if average == ZERO:
        return None

    deviation = abs(ctx.result.net - average) / average
    if deviation <= DEVIATION_THRESHOLD:
        return None
    return _issue(
        ValidationCode.NET_DEVIATION_FROM_TRAILING_AVERAGE,
        ValidationSeverity.WARNING,
        f"Net pay {ctx.result.net} deviates from the trailing "
        f"{len(history)}-month average {average} by more than "
        f"{DEVIATION_THRESHOLD * 100}%.",
        net=ctx.result.net,
        trailing_average=average,
        deviation=deviation,
    )


def gate_reimbursement_without_payable_days(ctx: PayoutContext) -> ValidationIssue | None:
    """WARNING: reimbursement claimed with zero payable days.

    Legitimate — travel booked for a deployment that was then cancelled still
    costs the trainer money. Suspicious enough to need a stated reason, because
    it is also what a TA&DA claim against the wrong month looks like.
    """
    if ctx.result.payable_days > ZERO or ctx.result.reimbursements <= ZERO:
        return None
    return _issue(
        ValidationCode.REIMBURSEMENT_WITH_ZERO_PAYABLE_DAYS,
        ValidationSeverity.WARNING,
        f"Reimbursement of {ctx.result.reimbursements} claimed with zero payable days.",
        reimbursements=ctx.result.reimbursements,
        payable_days=ctx.result.payable_days,
    )


#: Gates in the order §7 lists them. Order is the reporting order; every gate
#: runs regardless of earlier failures, because a Manager fixing a payout wants
#: the whole list once, not one blocker per round trip.
GATES: Final[tuple[Callable[[PayoutContext], ValidationIssue | None], ...]] = (
    gate_work_order_signed,
    gate_period_within_work_order,
    gate_zoho_account,
    gate_pan,
    gate_bank_account,
    gate_ifsc,
    gate_payable_days_within_month,
    gate_attendance_complete,
    gate_invoice_number_unissued,
    gate_rate_matches_work_order,
    gate_net_pay_positive,
    gate_net_deviation,
    gate_reimbursement_without_payable_days,
)


Gate = Callable[[PayoutContext], ValidationIssue | None]


def validate_payout(ctx: PayoutContext, gates: Sequence[Gate] | None = None) -> ValidationReport:
    """Run every §7 gate and return the full report.

    No short-circuit. Returning on the first blocker would hide the other four
    problems in the same row and turn one fix into five review cycles.
    """
    selected = GATES if gates is None else gates
    issues = tuple(issue for gate in selected if (issue := gate(ctx)) is not None)
    return ValidationReport(issues=issues)
