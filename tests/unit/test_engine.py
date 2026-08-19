"""Tests for `app.services.remuneration.engine` — CLAUDE.md §6 and §12.

"Payout engine: unit-tested against both fixtures, to the rupee. Non-negotiable."

Every fixture below was reconciled by hand against the shipped legacy workbooks.
If code and fixture disagree, the code is wrong — CLAUDE.md §6: "Do not adjust
the fixtures to match code."
"""

from __future__ import annotations

import ast
import importlib
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.domain.attendance import expand_period, payable_days
from app.domain.enums import AttendanceMark, ProgramType, RateBasis
from app.domain.money import DEFAULT_TDS_RATE, quantize_paise
from app.domain.payout import PayoutInput, PayoutResult
from app.services.remuneration.engine import compute_payout

# =============================================================================
# §6 regression fixtures — must reconcile to the rupee
# =============================================================================


def test_fixture_vema_prudhvi_sai() -> None:
    """bCAP, ₹80,000/mo, 26-31 Jul 2026 (6 days of a 31-day month), TA&DA ₹100.

    Expected: earned 15,483.87… · gross 15,583.87… · TDS 1,548.39… · net 14,035.

    TDS is levied on EARNED, not gross. On gross it would be 1,558.39 and the
    trainer would be short ₹10 — CLAUDE.md §6: "This is why the sample sheet
    shows 1,548 and not 1,558."
    """
    days = expand_period(date(2026, 7, 26), date(2026, 7, 31))
    counted = payable_days(ProgramType.BCAP, days)
    assert counted.days == Decimal(6)

    result = compute_payout(
        PayoutInput(
            program_type=ProgramType.BCAP,
            rate_basis=RateBasis.PER_MONTH,
            rate=Decimal(80000),
            payable_days=counted.days,
            days_in_month=31,
            period_start=date(2026, 7, 26),
            period_end=date(2026, 7, 31),
            ta_da=Decimal(100),
        )
    )

    # Full precision through every intermediate (R6).
    assert result.earned == Decimal(480000) / Decimal(31)
    assert quantize_paise(result.earned) == Decimal("15483.87")
    assert quantize_paise(result.gross) == Decimal("15583.87")
    assert quantize_paise(result.tds) == Decimal("1548.39")
    assert quantize_paise(result.net_unrounded) == Decimal("14035.48")

    # The one rounding, and the number Finance reconciles against.
    assert result.net == Decimal(14035)

    # TDS on gross would be 1,558.39. Guard against a future "simplification".
    assert quantize_paise(result.gross * DEFAULT_TDS_RATE) == Decimal("1558.39")
    assert result.tds != result.gross * DEFAULT_TDS_RATE


def test_fixture_bushily_kondala_rao() -> None:
    """bCAP, ₹65,000/mo, full July 2026 (31 of 31 days).

    Expected: earned 65,000 · TDS 6,500 · net 58,500 — EXACTLY.

    The shipped invoice sheet stores 65000.00000000001 / 6500.000000000001 /
    58500.00000000001 for this very row. That is the float artifact R7 exists to
    prevent, live in the current process. Our output must be exact.
    """
    days = expand_period(date(2026, 7, 1), date(2026, 7, 31))
    counted = payable_days(ProgramType.BCAP, days)
    assert counted.days == Decimal(31)

    result = compute_payout(
        PayoutInput(
            program_type=ProgramType.BCAP,
            rate_basis=RateBasis.PER_MONTH,
            rate=Decimal(65000),
            payable_days=counted.days,
            days_in_month=31,
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 31),
        )
    )

    assert result.earned == Decimal(65000)
    assert result.gross == Decimal(65000)
    assert result.tds == Decimal(6500)
    assert result.net == Decimal(58500)

    # Exact, not merely close: the legacy sheet's value is 1e-11 off and that is
    # the whole point of this fixture.
    assert str(result.earned) == "65000"
    assert result.net_unrounded == Decimal("58500.00")


# =============================================================================
# CRT fixtures — from the Google Form responses (partially answers §14 Q1)
# =============================================================================


def test_fixture_crt_yellepeddi() -> None:
    """CRT, work-order rate field reads `3500/day`, 6 payable days,
    13-18 Jul 2026. Earned 21,000.

    No `days_in_month` divisor on the CRT path.

    TA&DA is 0 here because the source row records "Na". CLAUDE.md §14 Q1 stays
    open on whether CRT TA&DA is per travel day — the engine accepts `ta_da` as
    an input and derives nothing.
    """
    marks = {date(2026, 7, d): AttendanceMark.PRESENT for d in range(13, 19)}
    days = expand_period(date(2026, 7, 13), date(2026, 7, 18), marks)
    counted = payable_days(ProgramType.CRT, days)
    assert counted.days == Decimal(6)

    result = compute_payout(
        PayoutInput(
            program_type=ProgramType.CRT,
            rate_basis=RateBasis.PER_DAY,
            rate=Decimal(3500),
            payable_days=counted.days,
            days_in_month=31,
            period_start=date(2026, 7, 13),
            period_end=date(2026, 7, 18),
        )
    )

    assert result.earned == Decimal(21000)
    assert result.rate_per_day == Decimal("3500.00")  # per-day: the rate IS the rate
    assert result.tds == Decimal("2100.0")
    assert result.net == Decimal(18900)


def test_fixture_crt_kota_lakshmi_manikanta() -> None:
    """CRT, ₹2,700/day, 7 payable days inside an 8-calendar-day period
    (11-18 Jul 2026). Earned 18,900.

    The load-bearing row: 8 calendar days, 7 paid. Direct evidence that CRT
    counts UP from `P` marks rather than DOWN from period length. A count-down
    model would produce 8 x 2700 = 21,600 and overpay by ₹2,700.
    """
    marks = {date(2026, 7, d): AttendanceMark.PRESENT for d in range(11, 18)}
    marks[date(2026, 7, 18)] = AttendanceMark.ABSENT
    days = expand_period(date(2026, 7, 11), date(2026, 7, 18), marks)
    counted = payable_days(ProgramType.CRT, days)

    assert counted.period_days == 8
    assert counted.days == Decimal(7)

    result = compute_payout(
        PayoutInput(
            program_type=ProgramType.CRT,
            rate_basis=RateBasis.PER_DAY,
            rate=Decimal(2700),
            payable_days=counted.days,
            days_in_month=31,
            period_start=date(2026, 7, 11),
            period_end=date(2026, 7, 18),
        )
    )

    assert result.earned == Decimal(18900)
    assert result.earned != Decimal(2700) * Decimal(8)  # the count-down mistake


def test_fixture_bcap_vijay_chalagala() -> None:
    """bCAP, ₹70,000/mo, 26 payable days of 31 (`No.of LOPs = 5`).

    The legacy cell literally contains `2258*26 = 58708`: the sheet rounded the
    per-day rate to 2258 and multiplied by it. Correct is 70,000 x 26 / 31 =
    58,709.68 — the trainer was underpaid ₹1.68 by a display column.
    """
    marks = {date(2026, 7, d): AttendanceMark.ABSENT for d in range(27, 32)}
    days = expand_period(date(2026, 7, 1), date(2026, 7, 31), marks)
    counted = payable_days(ProgramType.BCAP, days)
    assert counted.days == Decimal(26)

    result = compute_payout(
        PayoutInput(
            program_type=ProgramType.BCAP,
            rate_basis=RateBasis.PER_MONTH,
            rate=Decimal(70000),
            payable_days=counted.days,
            days_in_month=31,
        )
    )

    assert quantize_paise(result.earned) == Decimal("58709.68")
    assert result.earned != Decimal(58708)  # the legacy value


# =============================================================================
# The multiply-before-divide guard
# =============================================================================


def test_decimal_divide_first_does_not_recombine() -> None:
    """The failure the rule exists to prevent, stated in one line.

    `Decimal(65000) / 31 * 31` is 64999.99999999999999999999999. This is not a
    hypothetical: the shipped invoice sheet carries 65000.00000000001 for the
    same computation done in floating point.
    """
    assert Decimal(65000) / Decimal(31) * Decimal(31) != Decimal(65000)
    assert Decimal(65000) * Decimal(31) / Decimal(31) == Decimal(65000)


@pytest.mark.parametrize("rate", [Decimal(65000), Decimal(80000), Decimal(70000)])
@pytest.mark.parametrize("days_in_month", [28, 29, 30, 31])
def test_full_month_earns_exactly_the_monthly_rate(rate: Decimal, days_in_month: int) -> None:
    """A trainer present for every day of the month earns the monthly rate to the
    paisa — for every month length. Divide-first fails this for 28, 29 and 31.
    """
    result = compute_payout(
        PayoutInput(
            program_type=ProgramType.BCAP,
            rate_basis=RateBasis.PER_MONTH,
            rate=rate,
            payable_days=Decimal(days_in_month),
            days_in_month=days_in_month,
        )
    )
    assert result.earned == rate
    assert result.net == rate - rate * DEFAULT_TDS_RATE


def test_rate_per_day_is_display_only_and_never_feeds_earned() -> None:
    """R6: "never feed a rounded value back into a calculation."

    The legacy sheet for VEMA shows per-day 2581 and 2581 x 6 = 15,486, while
    the same row's Earned column says 15,484. The sheet contradicts itself. Our
    `earned` must match neither the rounded-rate product nor the quantised-rate
    product — it must be `rate * days / days_in_month`.
    """
    result = compute_payout(
        PayoutInput(
            program_type=ProgramType.BCAP,
            rate_basis=RateBasis.PER_MONTH,
            rate=Decimal(80000),
            payable_days=Decimal(6),
            days_in_month=31,
        )
    )

    rounded_rate_product = result.rate_per_day.quantize(Decimal(1)) * Decimal(6)
    quantised_rate_product = result.rate_per_day * Decimal(6)

    assert rounded_rate_product == Decimal(15486)  # the legacy artifact
    assert result.earned != rounded_rate_product
    assert result.earned != quantised_rate_product
    assert result.earned == Decimal(80000) * Decimal(6) / Decimal(31)


def test_engine_source_computes_earned_without_referencing_rate_per_day() -> None:
    """Structural guard: no assignment to `earned` may mention `rate_per_day`.

    A numeric test catches today's divergence; this catches the refactor that
    reintroduces `earned = rate_per_day * days` on a month where the two happen
    to agree (a 1-day period, say) and ships silently.
    """
    tree = ast.parse(_source_of("app.services.remuneration.engine"))

    earned_expressions: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "earned":
                    earned_expressions.append(node.value)

    assert earned_expressions, "no assignment to `earned` found — did the engine move?"
    for expr in earned_expressions:
        names = {n.id for n in ast.walk(expr) if isinstance(n, ast.Name)}
        assert "rate_per_day" not in names, (
            "`earned` references `rate_per_day` — R6 violation: the display rate "
            "must never re-enter the maths"
        )


# =============================================================================
# R7 — no float anywhere in the computation path
# =============================================================================

COMPUTATION_MODULES = (
    "app.domain.money",
    "app.domain.attendance",
    "app.domain.payout",
    "app.services.remuneration.engine",
    "app.services.remuneration.validators",
    "app.services.remuneration.invoice_no",
)


def _source_of(module_path: str) -> str:
    """Read a module's source via its import location, not a relative path — so
    these guards keep working whatever directory pytest was invoked from."""
    module = importlib.import_module(module_path)
    assert module.__file__ is not None
    return Path(module.__file__).read_text(encoding="utf-8")


@pytest.mark.parametrize("module_path", COMPUTATION_MODULES)
def test_no_float_literals_or_float_calls_in_source(module_path: str) -> None:
    """R7: "Never use float for money. Decimal only. No exceptions."

    Parses the AST rather than grepping, so `0.5` inside a docstring or a
    `Decimal("0.10")` string does not trip it and a real `1e-3` cannot hide.
    """
    tree = ast.parse(_source_of(module_path))

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, float):
            raise AssertionError(f"{module_path}:{node.lineno} float literal {node.value!r} — R7")
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "float"
        ):
            raise AssertionError(f"{module_path}:{node.lineno} call to float() — R7")


@pytest.mark.parametrize("module_path", COMPUTATION_MODULES)
def test_no_llm_import_in_the_money_path(module_path: str) -> None:
    """R2: "No money is computed by an LLM." Enforced by import, not by prompt."""
    tree = ast.parse(_source_of(module_path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("app.core.llm"):
            raise AssertionError(f"{module_path} imports app.core.llm — R2")
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("app.core.llm"), f"{module_path} — R2"


def test_every_numeric_result_field_is_decimal() -> None:
    """No float may survive to the result object, whatever the caller passed."""
    result = compute_payout(
        PayoutInput(
            program_type=ProgramType.BCAP,
            rate_basis=RateBasis.PER_MONTH,
            rate=Decimal(80000),
            payable_days=Decimal("5.5"),
            days_in_month=31,
            ta_da=Decimal(100),
            accommodation=Decimal(250),
            travel_reimbursement=Decimal("1200.50"),
            deductions=Decimal(75),
        )
    )
    for name in PayoutResult.NUMERIC_FIELDS:
        value = getattr(result, name)
        assert isinstance(value, Decimal), f"{name} is {type(value).__name__}, not Decimal"
        assert not isinstance(value, float)


@pytest.mark.parametrize(
    "field_name",
    [
        "rate",
        "payable_days",
        "ta_da",
        "accommodation",
        "travel_reimbursement",
        "deductions",
        "tds_rate",
    ],
)
def test_payout_input_rejects_float_fields(field_name: str) -> None:
    """The boundary refuses floats rather than coercing them — a coercion would
    launder the defect into a payout nobody can trace."""
    kwargs: dict[str, object] = {
        "program_type": ProgramType.BCAP,
        "rate_basis": RateBasis.PER_MONTH,
        "rate": Decimal(80000),
        "payable_days": Decimal(6),
        "days_in_month": 31,
    }
    kwargs[field_name] = 0.1
    with pytest.raises(TypeError, match="never use float"):
        PayoutInput(**kwargs)  # type: ignore[arg-type]


# =============================================================================
# Chain arithmetic
# =============================================================================


def test_reimbursements_add_to_gross_but_not_to_the_tds_base() -> None:
    bare = PayoutInput(
        program_type=ProgramType.BCAP,
        rate_basis=RateBasis.PER_MONTH,
        rate=Decimal(31000),
        payable_days=Decimal(31),
        days_in_month=31,
    )
    with_reimb = PayoutInput(
        program_type=ProgramType.BCAP,
        rate_basis=RateBasis.PER_MONTH,
        rate=Decimal(31000),
        payable_days=Decimal(31),
        days_in_month=31,
        ta_da=Decimal(500),
        accommodation=Decimal(2000),
        travel_reimbursement=Decimal(1500),
    )

    a, b = compute_payout(bare), compute_payout(with_reimb)
    assert b.gross - a.gross == Decimal(4000)
    assert b.tds == a.tds  # unchanged: TDS base is earned
    assert b.net - a.net == Decimal(4000)


def test_deductions_reduce_net_but_not_tds() -> None:
    result = compute_payout(
        PayoutInput(
            program_type=ProgramType.BCAP,
            rate_basis=RateBasis.PER_MONTH,
            rate=Decimal(31000),
            payable_days=Decimal(31),
            days_in_month=31,
            deductions=Decimal(1000),
        )
    )
    assert result.tds == Decimal(3100)
    assert result.net == Decimal(31000) - Decimal(1000) - Decimal(3100)


def test_zero_payable_days_earns_nothing_but_reimbursements_still_flow() -> None:
    """A cancelled deployment: no earnings, travel already spent. The §7 warning
    gate catches this shape; the engine still has to compute it honestly."""
    result = compute_payout(
        PayoutInput(
            program_type=ProgramType.BCAP,
            rate_basis=RateBasis.PER_MONTH,
            rate=Decimal(80000),
            payable_days=Decimal(0),
            days_in_month=31,
            travel_reimbursement=Decimal(3200),
        )
    )
    assert result.earned == Decimal(0)
    assert result.tds == Decimal(0)
    assert result.net == Decimal(3200)


def test_custom_tds_rate_is_honoured() -> None:
    """A lower-deduction certificate exists; the rate is overridable — but only
    with a Decimal."""
    result = compute_payout(
        PayoutInput(
            program_type=ProgramType.CRT,
            rate_basis=RateBasis.PER_DAY,
            rate=Decimal(3500),
            payable_days=Decimal(6),
            days_in_month=31,
            tds_rate=Decimal("0.02"),
        )
    )
    assert result.tds == Decimal(420)
    assert result.net == Decimal(20580)


def test_result_carries_every_intermediate_for_dispute_walkthrough() -> None:
    result = compute_payout(
        PayoutInput(
            program_type=ProgramType.BCAP,
            rate_basis=RateBasis.PER_MONTH,
            rate=Decimal(80000),
            payable_days=Decimal(6),
            days_in_month=31,
            ta_da=Decimal(100),
        )
    )
    assert result.gross == result.earned + result.reimbursements
    assert result.net_unrounded == result.gross - result.deductions - result.tds
    assert result.rate_basis is RateBasis.PER_MONTH
    assert result.days_in_month == 31
    assert result.payable_days == Decimal(6)


def test_invalid_days_in_month_is_rejected() -> None:
    with pytest.raises(ValueError, match="days_in_month"):
        PayoutInput(
            program_type=ProgramType.BCAP,
            rate_basis=RateBasis.PER_MONTH,
            rate=Decimal(80000),
            payable_days=Decimal(6),
            days_in_month=0,
        )


def test_negative_payable_days_is_rejected() -> None:
    with pytest.raises(ValueError, match="payable_days"):
        PayoutInput(
            program_type=ProgramType.CRT,
            rate_basis=RateBasis.PER_DAY,
            rate=Decimal(3500),
            payable_days=Decimal(-1),
            days_in_month=31,
        )
