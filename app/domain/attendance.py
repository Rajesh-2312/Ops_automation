"""Payable-day counting. CLAUDE.md §5 — the central branch.

CLAUDE.md §3: zero I/O. This module is a pure function over a list of
`(date, AttendanceMark)` and knows nothing about where those marks were stored.

    | | CRT | bCAP |
    |---|---|---|
    | Rate basis      | per day              | per month, prorated by calendar days |
    | Payable days    | counted UP from `P`  | counted DOWN from period length      |
    | Unmarked day    | not payable          | payable                              |
    | Weekend         | not payable (no `P`) | payable                              |
    | College holiday | not payable          | payable — the retainer absorbs it    |
    | Half day (`H`)  | 0.5                  | deducts 0.5                          |
    | Absent (`A`)    | 0                    | deducts 1                            |

**The two paths are written out separately and are not unified.** A single
table of per-mark weights would be shorter and would be a trap: the marks do not
mean the same thing on the two paths, they mean opposite things. `UNMARKED`
contributes 0 on both paths, but on CRT that 0 is "no evidence of work, pay
nothing" and on bCAP it is "nothing deducted, the retainer stands". Collapsing
them into one expression hides exactly the asymmetry that CLAUDE.md §5 warns is
"deliberate but dangerous": an unmarked day silently PAYS a bCAP trainer and
silently UNDERPAYS a CRT trainer.

That is also why attendance completeness is a hard block for CRT and only a
warning for bCAP (CLAUDE.md §7) — see `app.services.remuneration.validators`.

`HOLIDAY` and `UNMARKED` behave identically on both paths today. They are still
listed on separate branches below, because they diverge for different reasons
and a future rule change to one must not silently move the other.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from app.domain.enums import AttendanceMark, ProgramType

__all__ = [
    "AttendanceDay",
    "PayableDays",
    "expand_period",
    "payable_days",
]

_ONE = Decimal(1)
_HALF = Decimal("0.5")
_ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class AttendanceDay:
    """One trainer-day. CLAUDE.md §6: "one row per day, never a wide D1-D31
    layout. Payout disputes are always about specific days.\" """

    on_date: date
    mark: AttendanceMark


@dataclass(frozen=True, slots=True)
class PayableDays:
    """The counted result, carrying its own audit trail.

    `days` is a `Decimal` and not an `int` because a half day is 0.5 — and not a
    `float` because R7 has no exceptions, including for day counts that later
    multiply a rate.

    `unmarked` is surfaced rather than folded into a bool so the CRT block
    message can name how many days are missing. "Attendance incomplete" without
    a count sends someone hunting through 31 rows.
    """

    program_type: ProgramType
    days: Decimal
    period_days: int
    marked: int
    unmarked: int

    @property
    def is_complete(self) -> bool:
        """Every day in the period carries a mark other than `UNMARKED`."""
        return self.unmarked == 0


def expand_period(
    period_start: date,
    period_end: date,
    marks: Mapping[date, AttendanceMark] | None = None,
) -> list[AttendanceDay]:
    """Build the full day-by-day period, filling gaps with `UNMARKED`.

    Callers fetch sparse marks from the database (only days someone touched) and
    must not hand a sparse list to `payable_days()`: on the bCAP path the count
    starts from `len(days)`, so a sparse list would shorten the period itself and
    quietly underpay a retainer. Materialising every calendar day here is what
    makes "unmarked" a visible state rather than an absent row.

    Both bounds are inclusive — a payout period of 26-31 July is six days.
    """
    if period_end < period_start:
        raise ValueError(f"period_end {period_end} precedes period_start {period_start}")

    lookup = marks or {}
    out: list[AttendanceDay] = []
    cursor = period_start
    while cursor <= period_end:
        out.append(AttendanceDay(cursor, lookup.get(cursor, AttendanceMark.UNMARKED)))
        cursor += timedelta(days=1)

    stray = sorted(d for d in lookup if d < period_start or d > period_end)
    if stray:
        raise ValueError(
            f"marks outside {period_start}..{period_end}: {stray[:5]} "
            "— a mark on a day outside the payout period is a data error, not a no-op"
        )
    return out


def payable_days(program_type: ProgramType, days: Sequence[AttendanceDay]) -> PayableDays:
    """Count payable days for a period, per CLAUDE.md §5.

    `days` must cover every calendar day of the payout period exactly once — use
    `expand_period()` to build it. Duplicates are rejected: a duplicated `A` would
    deduct twice on the bCAP path.
    """
    _reject_duplicates(days)
    period_days = len(days)

    if program_type is ProgramType.CRT:
        counted = _crt_count_up(days)
    elif program_type is ProgramType.BCAP:
        counted = _bcap_count_down(days)
    else:  # pragma: no cover — exhaustive over ProgramType
        raise ValueError(f"unhandled program type {program_type!r}")

    unmarked = sum(1 for d in days if d.mark is AttendanceMark.UNMARKED)
    return PayableDays(
        program_type=program_type,
        days=counted,
        period_days=period_days,
        marked=period_days - unmarked,
        unmarked=unmarked,
    )


# --- CRT: count UP -----------------------------------------------------------


def _crt_count_up(days: Iterable[AttendanceDay]) -> Decimal:
    """CRT is per-day work: a day is paid only if someone recorded that it was
    worked. Start at zero and add evidence.

    Weekends need no special case — nobody marks `P` on a Saturday, so a weekend
    contributes nothing simply by not being present work. Encoding a weekday rule
    here would be wrong for the batches that genuinely run on Saturdays.
    """
    total = _ZERO
    for day in days:
        if day.mark is AttendanceMark.PRESENT:
            total += _ONE
        elif day.mark is AttendanceMark.HALF_DAY:
            total += _HALF
        elif day.mark is AttendanceMark.ABSENT:
            total += _ZERO
        elif day.mark is AttendanceMark.HOLIDAY:
            # A college holiday is not a worked day. The CRT trainer is not paid
            # for it. (The mirror-image bCAP rule is the opposite — see below.)
            total += _ZERO
        elif day.mark is AttendanceMark.UNMARKED:
            # No evidence of work => no pay. This UNDERPAYS if the LDE Executive
            # simply forgot to mark, which is why §7 makes attendance
            # completeness a BLOCKING gate for CRT.
            total += _ZERO
    return total


# --- bCAP: count DOWN --------------------------------------------------------


def _bcap_count_down(days: Sequence[AttendanceDay]) -> Decimal:
    """bCAP is a monthly retainer prorated by calendar days: every day of the
    period is payable until something takes it away. Start at the full period
    length and subtract absence.

    Weekends and college holidays are payable — the retainer absorbs them — so
    they subtract nothing. The floor at zero exists because a period of absences
    plus a data-entry duplicate should produce "nothing owed", never a negative
    day count that would invert the sign of the payout.
    """
    total = Decimal(len(days))
    for day in days:
        if day.mark is AttendanceMark.ABSENT:
            total -= _ONE
        elif day.mark is AttendanceMark.HALF_DAY:
            total -= _HALF
        elif day.mark is AttendanceMark.PRESENT:
            total -= _ZERO
        elif day.mark is AttendanceMark.HOLIDAY:
            # Payable. The monthly retainer covers college holidays; deducting
            # here would charge the trainer for the college's calendar.
            total -= _ZERO
        elif day.mark is AttendanceMark.UNMARKED:
            # Payable. This silently PAYS if the trainer was in fact absent,
            # which is why §7 keeps attendance completeness as a WARNING for
            # bCAP — a stated reason, not a block.
            total -= _ZERO
    return max(total, _ZERO)


def _reject_duplicates(days: Sequence[AttendanceDay]) -> None:
    seen: set[date] = set()
    for day in days:
        if day.on_date in seen:
            raise ValueError(
                f"duplicate attendance row for {day.on_date} — one row per day; "
                "a duplicated mark double-counts on both the CRT and bCAP paths"
            )
        seen.add(day.on_date)
