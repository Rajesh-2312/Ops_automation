"""Tests for `app.domain.attendance` — the CLAUDE.md §5 CRT/bCAP asymmetry.

One test per row of the §5 table, exercised on BOTH program types, because the
whole risk in this module is that the two paths quietly converge.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.domain.attendance import AttendanceDay, expand_period, payable_days
from app.domain.enums import AttendanceMark, ProgramType

# 2026-07-25 is a Saturday; 2026-07-27 is a Monday. Used for the weekend row.
SATURDAY = date(2026, 7, 25)
MONDAY = date(2026, 7, 27)


def _one_day(mark: AttendanceMark, on: date = MONDAY) -> list[AttendanceDay]:
    """A one-day period. Isolates a single mark's effect on each path."""
    return [AttendanceDay(on, mark)]


# --- The §5 table, row by row, both program types ---------------------------
#
#   | | CRT | bCAP |
#   | Unmarked day    | not payable          | payable |
#   | Weekend         | not payable (no `P`) | payable |
#   | College holiday | not payable          | payable |
#   | Half day (`H`)  | 0.5                  | deducts 0.5 |
#   | Absent (`A`)    | 0                    | deducts 1 |


@pytest.mark.parametrize(
    ("row", "mark", "on", "crt_expected", "bcap_expected"),
    [
        ("present", AttendanceMark.PRESENT, MONDAY, "1", "1"),
        # An unmarked day silently PAYS a bCAP trainer and silently UNDERPAYS a
        # CRT trainer. This row is the reason §7 blocks CRT on incompleteness.
        ("unmarked", AttendanceMark.UNMARKED, MONDAY, "0", "1"),
        # A weekend is simply an unmarked day — nobody marks P on a Saturday.
        # There is deliberately no weekday rule in the code: some batches run
        # on Saturdays and a hard-coded weekend rule would underpay them.
        ("weekend", AttendanceMark.UNMARKED, SATURDAY, "0", "1"),
        # The bCAP retainer absorbs the college's calendar; CRT does not.
        ("college holiday", AttendanceMark.HOLIDAY, MONDAY, "0", "1"),
        # Half day: CRT adds 0.5, bCAP deducts 0.5 from 1 -> also 0.5.
        ("half day", AttendanceMark.HALF_DAY, MONDAY, "0.5", "0.5"),
        # Absent: CRT adds 0, bCAP deducts 1 from 1 -> 0.
        ("absent", AttendanceMark.ABSENT, MONDAY, "0", "0"),
    ],
)
def test_section_5_table(
    row: str, mark: AttendanceMark, on: date, crt_expected: str, bcap_expected: str
) -> None:
    days = _one_day(mark, on)
    crt = payable_days(ProgramType.CRT, days)
    bcap = payable_days(ProgramType.BCAP, days)
    assert crt.days == Decimal(crt_expected), f"CRT row {row!r}"
    assert bcap.days == Decimal(bcap_expected), f"bCAP row {row!r}"


def test_crt_and_bcap_disagree_on_an_unmarked_period() -> None:
    """The asymmetry made loud: the same six unmarked days pay 0 on CRT and 6 on
    bCAP. If this ever asserts equal, the two paths have been unified and §5 has
    been lost."""
    days = expand_period(date(2026, 7, 26), date(2026, 7, 31))
    assert payable_days(ProgramType.CRT, days).days == Decimal(0)
    assert payable_days(ProgramType.BCAP, days).days == Decimal(6)


# --- CRT counts UP -----------------------------------------------------------


def test_crt_counts_up_from_p_marks() -> None:
    """Real form data (Kota Lakshmi Manikanta): an 8-calendar-day period,
    11-18 Jul 2026, with attendance 7 and earned 2700 x 7 = 18,900.

    A count-DOWN model would have produced 8 payable days and paid 21,600.
    """
    marks = {
        date(2026, 7, d): AttendanceMark.PRESENT for d in range(11, 18)
    }  # 11..17 = 7 days present
    marks[date(2026, 7, 18)] = AttendanceMark.ABSENT
    days = expand_period(date(2026, 7, 11), date(2026, 7, 18), marks)

    result = payable_days(ProgramType.CRT, days)
    assert result.period_days == 8
    assert result.days == Decimal(7)
    assert result.is_complete


def test_crt_half_days_accumulate_to_a_half() -> None:
    marks = {
        date(2026, 7, 1): AttendanceMark.PRESENT,
        date(2026, 7, 2): AttendanceMark.HALF_DAY,
        date(2026, 7, 3): AttendanceMark.HALF_DAY,
    }
    days = expand_period(date(2026, 7, 1), date(2026, 7, 3), marks)
    assert payable_days(ProgramType.CRT, days).days == Decimal(2)


# --- bCAP counts DOWN --------------------------------------------------------


def test_bcap_counts_down_from_period_length() -> None:
    """Real sheet data (Vijay Chalagala): 31-day July, `No.of LOPs = 5`,
    attendance 26 — i.e. 31 - 5, counted down."""
    marks = {date(2026, 7, d): AttendanceMark.ABSENT for d in range(27, 32)}
    days = expand_period(date(2026, 7, 1), date(2026, 7, 31), marks)

    result = payable_days(ProgramType.BCAP, days)
    assert result.period_days == 31
    assert result.days == Decimal(26)


def test_bcap_never_goes_negative() -> None:
    """A floor at zero. A negative day count would invert the sign of the
    payout, turning a data error into a recovery from the trainer."""
    days = expand_period(date(2026, 7, 1), date(2026, 7, 3))
    all_absent = [AttendanceDay(d.on_date, AttendanceMark.ABSENT) for d in days]
    assert payable_days(ProgramType.BCAP, all_absent).days == Decimal(0)


# --- Completeness (feeds the §7 gate) ---------------------------------------


def test_completeness_counts_unmarked_days() -> None:
    marks = {date(2026, 7, 1): AttendanceMark.PRESENT}
    days = expand_period(date(2026, 7, 1), date(2026, 7, 4), marks)
    result = payable_days(ProgramType.BCAP, days)
    assert result.unmarked == 3
    assert result.marked == 1
    assert not result.is_complete


def test_holiday_counts_as_marked() -> None:
    """HOLIDAY is a recorded decision, not a gap. Treating it as unmarked would
    block every CRT payout over a college festival week."""
    marks = {
        date(2026, 7, 1): AttendanceMark.PRESENT,
        date(2026, 7, 2): AttendanceMark.HOLIDAY,
    }
    days = expand_period(date(2026, 7, 1), date(2026, 7, 2), marks)
    assert payable_days(ProgramType.CRT, days).is_complete


# --- Period expansion --------------------------------------------------------


def test_expand_period_is_inclusive_of_both_bounds() -> None:
    days = expand_period(date(2026, 7, 26), date(2026, 7, 31))
    assert len(days) == 6
    assert days[0].on_date == date(2026, 7, 26)
    assert days[-1].on_date == date(2026, 7, 31)
    assert all(d.mark is AttendanceMark.UNMARKED for d in days)


def test_expand_period_rejects_reversed_bounds() -> None:
    with pytest.raises(ValueError, match="precedes"):
        expand_period(date(2026, 7, 31), date(2026, 7, 1))


def test_expand_period_rejects_marks_outside_the_period() -> None:
    """A mark on a day outside the payout period is a data error, not a no-op —
    most likely someone marked the wrong month."""
    with pytest.raises(ValueError, match="outside"):
        expand_period(
            date(2026, 7, 1),
            date(2026, 7, 3),
            {date(2026, 8, 1): AttendanceMark.PRESENT},
        )


def test_duplicate_day_is_rejected() -> None:
    """A duplicated `A` would deduct twice on the bCAP path."""
    days = [
        AttendanceDay(date(2026, 7, 1), AttendanceMark.ABSENT),
        AttendanceDay(date(2026, 7, 1), AttendanceMark.ABSENT),
    ]
    with pytest.raises(ValueError, match="duplicate"):
        payable_days(ProgramType.BCAP, days)


def test_payable_days_is_decimal_never_float() -> None:
    """R7 reaches day counts too — `payable_days` multiplies a rate."""
    days = expand_period(date(2026, 7, 1), date(2026, 7, 2))
    for program in (ProgramType.CRT, ProgramType.BCAP):
        assert isinstance(payable_days(program, days).days, Decimal)
