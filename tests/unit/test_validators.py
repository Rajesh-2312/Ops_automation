"""Tests for `app.services.remuneration.validators` — CLAUDE.md §7 and §12.

"Every validation gate has a passing case and a blocked case." Each gate below
gets exactly that pair, driven off one known-good context so a test failure names
the field that changed rather than the whole row.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from app.domain.enums import ProgramType, RateBasis, ValidationCode, ValidationSeverity
from app.domain.payout import PayoutInput
from app.services.remuneration.engine import compute_payout
from app.services.remuneration.validators import (
    GATES,
    PayoutContext,
    gate_attendance_complete,
    gate_bank_account,
    gate_ifsc,
    gate_invoice_number_unissued,
    gate_net_deviation,
    gate_net_pay_positive,
    gate_pan,
    gate_payable_days_within_month,
    gate_period_within_work_order,
    gate_rate_matches_work_order,
    gate_reimbursement_without_payable_days,
    gate_work_order_signed,
    gate_zoho_account,
    validate_payout,
)

VALID_PAN = "BCDPS1234K"
VALID_IFSC = "HDFC0001234"


def _result(**overrides: object):
    base: dict[str, object] = {
        "program_type": ProgramType.BCAP,
        "rate_basis": RateBasis.PER_MONTH,
        "rate": Decimal(80000),
        "payable_days": Decimal(6),
        "days_in_month": 31,
        "ta_da": Decimal(100),
    }
    base.update(overrides)
    return compute_payout(PayoutInput(**base))  # type: ignore[arg-type]


def clean_context(**overrides: object) -> PayoutContext:
    """A context that passes every §7 gate. Each test mutates exactly one field."""
    ctx = PayoutContext(
        program_type=ProgramType.BCAP,
        result=_result(),
        period_start=date(2026, 7, 26),
        period_end=date(2026, 7, 31),
        days_in_month=31,
        attendance_complete=True,
        unmarked_days=0,
        work_order_signed=True,
        wo_valid_from=date(2026, 6, 1),
        wo_valid_to=date(2026, 12, 31),
        wo_rate=Decimal(80000),
        engagement_rate=Decimal(80000),
        zoho_account_id="ZOHO-4417",
        pan=VALID_PAN,
        bank_account_number="50100234567890",
        ifsc=VALID_IFSC,
        invoice_number="BCDP/26-27/JUL1",
        issued_invoice_numbers=frozenset({"AFBP/26-27/JUL1"}),
        trailing_net_pays=(Decimal(14000), Decimal(13800), Decimal(14200)),
    )
    return replace(ctx, **overrides) if overrides else ctx


def test_the_clean_context_produces_no_issues_at_all() -> None:
    """The baseline every other test in this file depends on."""
    report = validate_payout(clean_context())
    assert report.issues == ()
    assert not report.is_blocked
    assert report.can_submit()


# =============================================================================
# Blocking gates — one passing case and one blocked case each (§12)
# =============================================================================


def test_work_order_signed_passes() -> None:
    assert gate_work_order_signed(clean_context()) is None


def test_work_order_missing_blocks() -> None:
    issue = gate_work_order_signed(clean_context(work_order_signed=False))
    assert issue is not None
    assert issue.code is ValidationCode.WORK_ORDER_MISSING
    assert issue.severity is ValidationSeverity.BLOCKING


def test_period_inside_work_order_passes() -> None:
    assert gate_period_within_work_order(clean_context()) is None


def test_period_outside_work_order_blocks() -> None:
    """The WO expired mid-month. The tail of the period needs a fresh WO, not a
    wider tolerance."""
    issue = gate_period_within_work_order(clean_context(wo_valid_to=date(2026, 7, 28)))
    assert issue is not None
    assert issue.code is ValidationCode.WORK_ORDER_PERIOD_MISMATCH
    assert issue.severity is ValidationSeverity.BLOCKING


def test_period_bounds_are_inclusive() -> None:
    ctx = clean_context(wo_valid_from=date(2026, 7, 26), wo_valid_to=date(2026, 7, 31))
    assert gate_period_within_work_order(ctx) is None


def test_period_gate_stays_silent_when_no_work_order_exists() -> None:
    """One root cause, one blocker. Two blocks for a missing WO reads as two
    separate problems to whoever has to fix it."""
    ctx = clean_context(work_order_signed=False, wo_valid_from=None, wo_valid_to=None)
    assert gate_period_within_work_order(ctx) is None
    report = validate_payout(ctx)
    assert [i.code for i in report.blocking] == [ValidationCode.WORK_ORDER_MISSING]


def test_zoho_account_present_passes() -> None:
    assert gate_zoho_account(clean_context()) is None


@pytest.mark.parametrize("value", [None, "", "   "])
def test_zoho_account_missing_blocks(value: str | None) -> None:
    issue = gate_zoho_account(clean_context(zoho_account_id=value))
    assert issue is not None
    assert issue.code is ValidationCode.ZOHO_ACCOUNT_MISSING


def test_valid_pan_passes() -> None:
    assert gate_pan(clean_context()) is None


@pytest.mark.parametrize(
    "bad_pan",
    [
        None,
        "",
        "BCDPS1234",  # 9 chars
        "BCDPS1234KK",  # 11 chars
        "BCDP51234K",  # digit in the letter block — right length, wrong shape
    ],
)
def test_bad_pan_blocks(bad_pan: str | None) -> None:
    issue = gate_pan(clean_context(pan=bad_pan))
    assert issue is not None
    assert issue.code is ValidationCode.PAN_INVALID
    assert issue.severity is ValidationSeverity.BLOCKING


def test_bank_account_present_passes() -> None:
    assert gate_bank_account(clean_context()) is None


@pytest.mark.parametrize("value", [None, "", "  ", "ACC-12345"])
def test_bank_account_missing_or_non_numeric_blocks(value: str | None) -> None:
    issue = gate_bank_account(clean_context(bank_account_number=value))
    assert issue is not None
    assert issue.code is ValidationCode.BANK_ACCOUNT_MISSING


def test_valid_ifsc_passes() -> None:
    assert gate_ifsc(clean_context()) is None


@pytest.mark.parametrize(
    "bad_ifsc",
    [
        None,
        "",
        "HDFC000123",  # 10 chars
        "HDFC00012345",  # 12 chars
        "HDFC1001234",  # 11 chars but no RBI-reserved '0' in position 5
    ],
)
def test_bad_ifsc_blocks(bad_ifsc: str | None) -> None:
    issue = gate_ifsc(clean_context(ifsc=bad_ifsc))
    assert issue is not None
    assert issue.code is ValidationCode.IFSC_INVALID


def test_payable_days_within_month_passes() -> None:
    assert gate_payable_days_within_month(clean_context()) is None
    full = clean_context(result=_result(payable_days=Decimal(31)))
    assert gate_payable_days_within_month(full) is None  # boundary: 31 <= 31


def test_payable_days_exceeding_month_blocks() -> None:
    """The arithmetic signature of a duplicated attendance row: on the bCAP path
    it pays more than the full monthly retainer."""
    ctx = clean_context(result=_result(payable_days=Decimal(32)))
    issue = gate_payable_days_within_month(ctx)
    assert issue is not None
    assert issue.code is ValidationCode.PAYABLE_DAYS_EXCEED_MONTH


def test_attendance_complete_passes_on_both_program_types() -> None:
    for program in (ProgramType.CRT, ProgramType.BCAP):
        assert gate_attendance_complete(clean_context(program_type=program)) is None


def test_incomplete_attendance_blocks_crt() -> None:
    """CRT counts UP from `P` marks: an unmarked day pays nothing and the
    trainer is underpaid by a clerical omission that produces no visible error."""
    ctx = clean_context(program_type=ProgramType.CRT, attendance_complete=False, unmarked_days=3)
    issue = gate_attendance_complete(ctx)
    assert issue is not None
    assert issue.code is ValidationCode.ATTENDANCE_INCOMPLETE
    assert issue.severity is ValidationSeverity.BLOCKING
    assert validate_payout(ctx).is_blocked


def test_incomplete_attendance_only_warns_for_bcap() -> None:
    """bCAP counts DOWN: the unmarked day is paid regardless, so the risk is an
    overpayment a Manager can knowingly accept with a stated reason."""
    ctx = clean_context(attendance_complete=False, unmarked_days=3)
    issue = gate_attendance_complete(ctx)
    assert issue is not None
    assert issue.code is ValidationCode.ATTENDANCE_INCOMPLETE
    assert issue.severity is ValidationSeverity.WARNING

    report = validate_payout(ctx)
    assert not report.is_blocked
    assert not report.can_submit()
    assert report.can_submit(
        {ValidationCode.ATTENDANCE_INCOMPLETE: "LDE on leave; college confirmed delivery"}
    )


def test_the_same_incompleteness_blocks_crt_and_warns_bcap() -> None:
    """The asymmetry asserted head-on. If these two ever agree, §5 has been lost."""
    crt = validate_payout(
        clean_context(program_type=ProgramType.CRT, attendance_complete=False, unmarked_days=2)
    )
    bcap = validate_payout(clean_context(attendance_complete=False, unmarked_days=2))
    assert crt.is_blocked
    assert not bcap.is_blocked


def test_unissued_invoice_number_passes() -> None:
    assert gate_invoice_number_unissued(clean_context()) is None


def test_already_issued_invoice_number_blocks() -> None:
    ctx = clean_context(issued_invoice_numbers=frozenset({"BCDP/26-27/JUL1"}))
    issue = gate_invoice_number_unissued(ctx)
    assert issue is not None
    assert issue.code is ValidationCode.INVOICE_NUMBER_ALREADY_ISSUED


def test_rate_matching_work_order_passes() -> None:
    assert gate_rate_matches_work_order(clean_context()) is None
    # Decimal compares by value: 80000 and 80000.00 are the same rate.
    assert gate_rate_matches_work_order(clean_context(wo_rate=Decimal("80000.00"))) is None


def test_rate_mismatch_with_work_order_blocks() -> None:
    """The work order is the contract. If the engagement record disagrees, one of
    the two is wrong and neither the engine nor a human should guess which."""
    issue = gate_rate_matches_work_order(clean_context(wo_rate=Decimal(75000)))
    assert issue is not None
    assert issue.code is ValidationCode.RATE_MISMATCH_WITH_WORK_ORDER


def test_positive_net_pay_passes() -> None:
    assert gate_net_pay_positive(clean_context()) is None


@pytest.mark.parametrize("deductions", [Decimal(20000), Decimal("14035.49")])
def test_non_positive_net_pay_blocks(deductions: Decimal) -> None:
    """Zero is blocked as well as negative — a ₹0 bank instruction must never
    travel through the approval chain."""
    ctx = clean_context(result=_result(deductions=deductions))
    issue = gate_net_pay_positive(ctx)
    assert issue is not None
    assert issue.code is ValidationCode.NET_PAY_NOT_POSITIVE


# =============================================================================
# Warning gates
# =============================================================================


def test_net_within_twenty_percent_of_trailing_average_passes() -> None:
    assert gate_net_deviation(clean_context()) is None


def test_net_deviation_beyond_twenty_percent_warns() -> None:
    ctx = clean_context(trailing_net_pays=(Decimal(50000), Decimal(52000), Decimal(48000)))
    issue = gate_net_deviation(ctx)
    assert issue is not None
    assert issue.code is ValidationCode.NET_DEVIATION_FROM_TRAILING_AVERAGE
    assert issue.severity is ValidationSeverity.WARNING


def test_deviation_of_exactly_twenty_percent_does_not_warn() -> None:
    """§7 says "deviates >20%". The boundary is deterministic because the
    threshold is a Decimal — a 0.20 float literal would make it a coin flip."""
    # net 14035; an average of 14035 / 0.8 = 17543.75 is exactly 20% above.
    ctx = clean_context(trailing_net_pays=(Decimal("17543.75"),))
    assert gate_net_deviation(ctx) is None


def test_first_payout_has_no_history_and_does_not_warn() -> None:
    """With no trailing months there is nothing to deviate from. Warning here
    would fire on every new joiner and train people to ignore the gate."""
    assert gate_net_deviation(clean_context(trailing_net_pays=())) is None


def test_zero_trailing_average_does_not_warn() -> None:
    assert gate_net_deviation(clean_context(trailing_net_pays=(Decimal(0),))) is None


def test_only_the_last_three_months_are_averaged() -> None:
    """A fourth month must not dilute the comparison §7 specifies."""
    ctx = clean_context(
        trailing_net_pays=(Decimal(14000), Decimal(14000), Decimal(14000), Decimal(999999))
    )
    assert gate_net_deviation(ctx) is None


def test_reimbursement_with_payable_days_passes() -> None:
    assert gate_reimbursement_without_payable_days(clean_context()) is None


def test_reimbursement_with_zero_payable_days_warns() -> None:
    """Legitimate — travel booked for a cancelled deployment still cost money.
    Also what a TA&DA claim against the wrong month looks like."""
    ctx = clean_context(
        result=_result(
            payable_days=Decimal(0), ta_da=Decimal(0), travel_reimbursement=Decimal(3200)
        )
    )
    issue = gate_reimbursement_without_payable_days(ctx)
    assert issue is not None
    assert issue.code is ValidationCode.REIMBURSEMENT_WITH_ZERO_PAYABLE_DAYS
    assert issue.severity is ValidationSeverity.WARNING


def test_zero_payable_days_and_zero_reimbursement_does_not_warn() -> None:
    ctx = clean_context(result=_result(payable_days=Decimal(0), ta_da=Decimal(0)))
    assert gate_reimbursement_without_payable_days(ctx) is None


# =============================================================================
# Report semantics
# =============================================================================


def test_every_section_7_gate_is_registered() -> None:
    """A gate that exists but is not in GATES never runs. §12 requires all of
    them, so the count is asserted rather than assumed."""
    assert len(GATES) == 13
    names = {g.__name__ for g in GATES}
    assert names == {
        "gate_work_order_signed",
        "gate_period_within_work_order",
        "gate_zoho_account",
        "gate_pan",
        "gate_bank_account",
        "gate_ifsc",
        "gate_payable_days_within_month",
        "gate_attendance_complete",
        "gate_invoice_number_unissued",
        "gate_rate_matches_work_order",
        "gate_net_pay_positive",
        "gate_net_deviation",
        "gate_reimbursement_without_payable_days",
    }


def test_validation_does_not_short_circuit_on_the_first_blocker() -> None:
    """Returning early would hide four problems in the same row and turn one fix
    into five review cycles."""
    ctx = clean_context(
        work_order_signed=False,
        zoho_account_id=None,
        pan="NOPE",
        bank_account_number=None,
        ifsc="BAD",
    )
    report = validate_payout(ctx)
    codes = {i.code for i in report.blocking}
    assert codes == {
        ValidationCode.WORK_ORDER_MISSING,
        ValidationCode.ZOHO_ACCOUNT_MISSING,
        ValidationCode.PAN_INVALID,
        ValidationCode.BANK_ACCOUNT_MISSING,
        ValidationCode.IFSC_INVALID,
    }


def test_a_blocked_report_cannot_submit_even_with_reasons() -> None:
    """A stated reason overrides a WARNING, never a BLOCK."""
    ctx = clean_context(pan=None)
    report = validate_payout(ctx)
    assert report.is_blocked
    assert not report.can_submit({ValidationCode.PAN_INVALID: "will fix later"})


def test_a_blank_reason_does_not_satisfy_a_warning() -> None:
    """§7: "requires a stated reason". A blank string is how a required field
    becomes a formality."""
    ctx = clean_context(attendance_complete=False, unmarked_days=1)
    report = validate_payout(ctx)
    assert not report.can_submit({ValidationCode.ATTENDANCE_INCOMPLETE: "   "})
    assert report.can_submit({ValidationCode.ATTENDANCE_INCOMPLETE: "college holiday week"})


def test_issue_detail_carries_the_numbers_for_the_payout_agent() -> None:
    """R1: an agent may only state figures passed to it as structured input.
    `detail` is that input, so the Payout agent never has to recompute."""
    issue = gate_payable_days_within_month(clean_context(result=_result(payable_days=Decimal(32))))
    assert issue is not None
    assert issue.detail["payable_days"] == "32"
    assert issue.detail["days_in_month"] == "31"


def test_report_partitions_issues_by_severity() -> None:
    ctx = clean_context(pan=None, attendance_complete=False, unmarked_days=1)
    report = validate_payout(ctx)
    assert [i.code for i in report.blocking] == [ValidationCode.PAN_INVALID]
    assert [i.code for i in report.warnings] == [ValidationCode.ATTENDANCE_INCOMPLETE]
    assert report.requires_reason == (ValidationCode.ATTENDANCE_INCOMPLETE,)


def test_validators_hold_no_database_handle() -> None:
    """§3 / R1: the caller fetches, the validators compare. `PayoutContext` is
    plain data — if a field here ever becomes a callable or a session, a gate has
    quietly become a query."""
    ctx = clean_context()
    for name in PayoutContext.__slots__:
        value = getattr(ctx, name)
        assert not callable(value), f"PayoutContext.{name} is callable — a gate became a query"
