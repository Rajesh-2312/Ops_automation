"""What the Delivery Monitor is given. CLAUDE.md §3 and §8.

Plain frozen data, fetched by the caller. The monitor compares; it does not
query. That is what keeps every detector unit-testable without a database (§12)
and what stops a detector quietly becoming a query with a threshold in it.

THE `elapsed_through` FIELD IS THE ONE THAT MATTERS
--------------------------------------------------
A payout period runs to the end of the month. On the 14th, the 20th is not
"unmarked" — it has not happened. Without an explicit elapsed boundary the
monitor would raise an attendance anomaly every day of every month for days
nobody could possibly have marked yet, and an alert that fires every day is an
alert nobody reads. Every attendance detector therefore looks only at
`elapsed_days()`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from app.domain.attendance import AttendanceDay
from app.domain.enums import ProgramType

__all__ = [
    "AttendanceSignal",
    "MonitorContext",
    "SyllabusSignal",
    "UsageSignal",
]


@dataclass(frozen=True, slots=True)
class AttendanceSignal:
    """Day-by-day attendance for the current period, plus marking freshness.

    `days` must cover every calendar day of the period exactly once — build it
    with `app.domain.attendance.expand_period()`, which materialises `UNMARKED`
    for days no row exists for. A sparse list would make "unmarked" invisible,
    which is the single failure mode this whole detector family exists to catch
    (§5).
    """

    period_start: date
    period_end: date
    days: tuple[AttendanceDay, ...]

    #: The last day that has already happened. Days after it are not yet late.
    elapsed_through: date

    #: When attendance was last marked for this batch, UTC. `None` means never.
    #: §11: timestamps are UTC in the DB and IST only at presentation, so every
    #: comparison in this package is UTC-to-UTC.
    last_marked_at: datetime | None = None

    def elapsed_days(self) -> tuple[AttendanceDay, ...]:
        """The days a mark could reasonably exist for. See the module docstring."""
        return tuple(d for d in self.days if d.on_date <= self.elapsed_through)


@dataclass(frozen=True, slots=True)
class UsageSignal:
    """EdTech-platform engagement for the batch.

    §14 Q6 is open — "EdTech platform access: direct DB, API, or neither?" — so
    this is deliberately the smallest shape that supports a usage anomaly at all.
    Counts, not rates: a rate computed upstream cannot be checked against its
    denominator, and a "40% active" reading over 5 enrolled learners is noise.
    """

    enrolled_learners: int
    active_learners: int
    #: Sessions already delivered. Zero means the batch has not started, and no
    #: usage anomaly should fire on a batch that has not started.
    sessions_held: int
    observed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class SyllabusSignal:
    """Reported syllabus completion against elapsed schedule.

    Percentages as `Decimal` in 0..100. Not `float`: two monitor runs over the
    same reading must produce the same shortfall to the same digit, or the
    "behind schedule" line moves under people while they are looking at it.
    """

    completion_percent: Decimal
    #: Fraction of the plan that should be done by now, 0..100, computed by the
    #: caller from the batch calendar. Passed in rather than derived here because
    #: the calendar (holidays, rescheduled sessions) is not in this package.
    expected_percent: Decimal

    #: The previous reading and its age, for the stalled-progress detector.
    previous_completion_percent: Decimal | None = None
    days_since_previous_reading: int = 0


@dataclass(frozen=True, slots=True)
class MonitorContext:
    """One program's observation window, fully assembled.

    `program_type` is on the context and not on the attendance signal because it
    governs more than attendance: it is CLAUDE.md §5's central branch and every
    detector that touches an unmarked day has to consult it.
    """

    program_id: UUID
    college_id: UUID
    program_type: ProgramType

    #: Observation instant, UTC. Passed in, never read from a clock inside this
    #: package: a detector that calls `datetime.now()` cannot be asserted
    #: deterministically, and §12 requires exactly that assertion.
    as_of: datetime

    attendance: AttendanceSignal | None = None
    usage: UsageSignal | None = None
    syllabus: SyllabusSignal | None = None

    #: Free-form identifiers echoed onto every anomaly (batch, trainer PAN, …).
    #: §6: trainer identity is PAN, never a name string.
    labels: tuple[tuple[str, str], ...] = field(default_factory=tuple)
