"""Anomaly detectors. CLAUDE.md §8 — Delivery Monitor, ceiling Alert.

Shaped after `app/services/remuneration/validators.py`: one function per rule,
each a pure function of the context returning an `Anomaly` or `None`, all of them
collected in a declaration-ordered tuple and run without short-circuit. §12 then
maps 1:1 onto tests — a firing case and a non-firing case each.

WHY NO SHORT-CIRCUIT
--------------------
Same reason as the payout gates: a Manager reading a Monday alert wants the whole
picture of a program at once. Stopping at the first anomaly turns one review into
five, and the second anomaly is often the one that explains the first.

THE CRT / bCAP ASYMMETRY (§5) IS THE LOAD-BEARING PART
------------------------------------------------------
`detect_unmarked_days` is the monitor's counterpart to the §7 attendance gate,
and it inherits the same asymmetry. CRT counts payable days UP from `P` marks, so
one unmarked elapsed day is already an underpayment in flight and nothing
downstream will surface it — CRITICAL from the first day. bCAP counts DOWN from
period length, so an unmarked day is paid and the exposure accumulates rather
than appearing at once — INFO, then WARNING once it is material. That mirrors
BLOCKING-for-CRT / WARNING-for-bCAP in the validators.

Do not "simplify" the two branches into one threshold. They are not the same
rule with different numbers; they are opposite failure modes that happen to share
an input.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from types import MappingProxyType
from typing import Final

from app.domain.attendance import AttendanceDay
from app.domain.enums import AttendanceMark, ProgramType
from app.domain.risk import AnomalyCode, AnomalySeverity
from app.services.monitoring.signals import MonitorContext

__all__ = [
    "ABSENCE_RATE_WARNING",
    "ABSENCE_STREAK_CRITICAL",
    "ABSENCE_STREAK_WARNING",
    "ACTIVE_SHARE_CRITICAL",
    "ACTIVE_SHARE_WARNING",
    "Anomaly",
    "BCAP_UNMARKED_WARNING_DAYS",
    "DETECTORS",
    "Detector",
    "MARKING_STALE_HOURS",
    "SYLLABUS_SHORTFALL_CRITICAL",
    "SYLLABUS_SHORTFALL_WARNING",
    "SYLLABUS_STALL_DAYS",
    "detect_anomalies",
]

_ZERO: Final[Decimal] = Decimal(0)
_HUNDRED: Final[Decimal] = Decimal(100)

#: Unmarked bCAP days below this are noted (INFO) and at or above it warn. Three
#: days is roughly a long weekend of missed marking — below that the retainer
#: exposure is small enough that an alert would cost more attention than it saves.
BCAP_UNMARKED_WARNING_DAYS: Final[int] = 3

#: Hours without any attendance mark before the marking itself looks abandoned.
#: 48 rather than 24: a Friday-evening batch marked on Monday morning is normal
#: operations, and a monitor that cries on every weekend gets muted.
MARKING_STALE_HOURS: Final[Decimal] = Decimal(48)

#: Consecutive `A` marks. Three is "something happened"; five is "the deployment
#: has effectively stopped and nobody raised it".
ABSENCE_STREAK_WARNING: Final[int] = 3
ABSENCE_STREAK_CRITICAL: Final[int] = 5

#: Share of marked days lost to absence, and the minimum sample it needs. Below
#: five marked days a single absence is 20% and means nothing.
ABSENCE_RATE_WARNING: Final[Decimal] = Decimal("0.25")
ABSENCE_RATE_MIN_MARKED_DAYS: Final[int] = 5

#: Active learners as a share of enrolled.
ACTIVE_SHARE_WARNING: Final[Decimal] = Decimal("0.60")
ACTIVE_SHARE_CRITICAL: Final[Decimal] = Decimal("0.30")

#: Percentage points of syllabus completion behind the elapsed schedule.
SYLLABUS_SHORTFALL_WARNING: Final[Decimal] = Decimal(15)
SYLLABUS_SHORTFALL_CRITICAL: Final[Decimal] = Decimal(30)

#: Days a completion reading may stay flat before it reads as stalled.
SYLLABUS_STALL_DAYS: Final[int] = 7


@dataclass(frozen=True, slots=True)
class Anomaly:
    """One observation. Never a conclusion, never an instruction.

    `detail` carries the operative numbers so a Payout or Reporting agent
    (autonomy level 2, draft only — §8) can *explain* the anomaly without
    recomputing anything. R1: a generated message may only contain figures passed
    to it as structured input, and this mapping is that input.
    """

    code: AnomalyCode
    severity: AnomalySeverity
    message: str
    detail: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))

    @property
    def is_critical(self) -> bool:
        return self.severity is AnomalySeverity.CRITICAL


def _anomaly(
    code: AnomalyCode,
    severity: AnomalySeverity,
    message: str,
    **detail: object,
) -> Anomaly:
    return Anomaly(
        code=code,
        severity=severity,
        message=message,
        detail=MappingProxyType({k: str(v) for k, v in detail.items()}),
    )


# --- Attendance --------------------------------------------------------------


def detect_unmarked_days(ctx: MonitorContext) -> Anomaly | None:
    """Unmarked elapsed days. Severity by program type — CLAUDE.md §5, §7.

    CRT: any unmarked elapsed day is CRITICAL. Payable days are counted UP from
    `P` marks, so the day is worth nothing to the trainer and no error is raised
    anywhere — it is an underpayment that only surfaces if a human notices.

    bCAP: payable days are counted DOWN from the period length, so an unmarked
    day is paid. The exposure is an overpayment a Manager can consciously accept
    with a stated reason (§7 keeps it a WARNING there too), so it starts as INFO
    and becomes a WARNING once it is material.

    Silent when nothing has elapsed yet: a period that has not started cannot be
    behind on marking.
    """
    signal = ctx.attendance
    if signal is None:
        return None
    elapsed = signal.elapsed_days()
    if not elapsed:
        return None

    unmarked = tuple(d for d in elapsed if d.mark is AttendanceMark.UNMARKED)
    if not unmarked:
        return None

    if ctx.program_type is ProgramType.CRT:
        severity = AnomalySeverity.CRITICAL
        consequence = (
            "CRT counts payable days up from P marks, so each unmarked day underpays "
            "the trainer and nothing downstream reports it"
        )
    else:
        severity = (
            AnomalySeverity.WARNING
            if len(unmarked) >= BCAP_UNMARKED_WARNING_DAYS
            else AnomalySeverity.INFO
        )
        consequence = "bCAP counts down from period length, so each unmarked day is paid regardless"

    return _anomaly(
        AnomalyCode.ATTENDANCE_UNMARKED_DAYS,
        severity,
        f"{len(unmarked)} unmarked day(s) up to {signal.elapsed_through} in "
        f"{signal.period_start}..{signal.period_end}. {consequence}.",
        program_type=ctx.program_type.value,
        unmarked_days=len(unmarked),
        elapsed_days=len(elapsed),
        first_unmarked=unmarked[0].on_date,
        last_unmarked=unmarked[-1].on_date,
    )


def detect_marking_stale(ctx: MonitorContext) -> Anomaly | None:
    """Nobody has marked attendance for `MARKING_STALE_HOURS`.

    Distinct from `detect_unmarked_days` on purpose. That one says "these days
    are missing"; this one says "the person who marks has stopped". The second
    predicts the first, and catching it early is the difference between one LDE
    Executive filling a gap and a payout cycle blocked at month end.

    Both timestamps are UTC (§11). `as_of` comes from the context rather than a
    clock so the result is reproducible.
    """
    signal = ctx.attendance
    if signal is None or not signal.elapsed_days():
        return None
    if signal.last_marked_at is None:
        return _anomaly(
            AnomalyCode.ATTENDANCE_MARKING_STALE,
            AnomalySeverity.WARNING,
            f"Attendance has never been marked for {signal.period_start}.." f"{signal.period_end}.",
            last_marked_at=None,
        )

    hours = Decimal((ctx.as_of - signal.last_marked_at).total_seconds()) / Decimal(3600)
    if hours <= MARKING_STALE_HOURS:
        return None
    return _anomaly(
        AnomalyCode.ATTENDANCE_MARKING_STALE,
        AnomalySeverity.WARNING,
        f"No attendance marked for {hours.quantize(Decimal('1'))} hours "
        f"(threshold {MARKING_STALE_HOURS}).",
        hours_since_mark=hours.quantize(Decimal("1")),
        threshold_hours=MARKING_STALE_HOURS,
        last_marked_at=signal.last_marked_at.isoformat(),
    )


def detect_absence_streak(ctx: MonitorContext) -> Anomaly | None:
    """A run of consecutive `A` marks.

    Deliberately counts only `A`, never `UNMARKED`. Conflating the two would let
    a clerical gap read as a trainer who stopped attending, and the two need
    opposite responses — one is chased with the LDE Executive, the other with the
    trainer and the sourcing chain.
    """
    signal = ctx.attendance
    if signal is None:
        return None
    streak, start = _longest_absence_streak(signal.elapsed_days())
    if streak < ABSENCE_STREAK_WARNING:
        return None
    severity = (
        AnomalySeverity.CRITICAL if streak >= ABSENCE_STREAK_CRITICAL else AnomalySeverity.WARNING
    )
    return _anomaly(
        AnomalyCode.ATTENDANCE_ABSENCE_STREAK,
        severity,
        f"{streak} consecutive absent day(s) from {start}.",
        streak_days=streak,
        streak_start=start,
    )


def detect_absence_rate(ctx: MonitorContext) -> Anomaly | None:
    """Absence across the marked part of the period exceeds tolerance.

    Half days count as half an absence, matching §5's `H` semantics, so a trainer
    on repeated half days is not invisible to the rate while being invisible to
    the streak detector.

    Needs `ABSENCE_RATE_MIN_MARKED_DAYS` of evidence. Without a minimum sample
    the first absent day of a month fires at 100%.
    """
    signal = ctx.attendance
    if signal is None:
        return None
    marked = tuple(d for d in signal.elapsed_days() if d.mark is not AttendanceMark.UNMARKED)
    if len(marked) < ABSENCE_RATE_MIN_MARKED_DAYS:
        return None

    lost = _ZERO
    for day in marked:
        if day.mark is AttendanceMark.ABSENT:
            lost += Decimal(1)
        elif day.mark is AttendanceMark.HALF_DAY:
            lost += Decimal("0.5")
    rate = lost / Decimal(len(marked))
    if rate <= ABSENCE_RATE_WARNING:
        return None
    return _anomaly(
        AnomalyCode.ATTENDANCE_ABSENCE_RATE,
        AnomalySeverity.WARNING,
        f"Absence rate {rate} across {len(marked)} marked day(s) exceeds "
        f"{ABSENCE_RATE_WARNING}.",
        absence_rate=rate,
        threshold=ABSENCE_RATE_WARNING,
        marked_days=len(marked),
        absent_equivalent_days=lost,
    )


def _longest_absence_streak(days: Sequence[AttendanceDay]) -> tuple[int, date | None]:
    """Longest run of `A`, and the date it started. `(0, None)` when there is none.

    Assumes `days` is in calendar order, which `expand_period()` guarantees.
    """
    best = 0
    best_start: date | None = None
    current = 0
    current_start: date | None = None
    for day in days:
        if day.mark is AttendanceMark.ABSENT:
            current += 1
            if current == 1:
                current_start = day.on_date
            if current > best:
                best, best_start = current, current_start
        else:
            current = 0
            current_start = None
    return best, best_start


# --- Platform usage ----------------------------------------------------------


def detect_no_activity(ctx: MonitorContext) -> Anomaly | None:
    """Sessions have been delivered and not one learner is active.

    Almost always an integration fault rather than a delivery fault — which is
    why it is worth its own code. Reporting it as "low usage" would send a
    Manager to the college about a problem that lives in the platform.
    """
    usage = ctx.usage
    if usage is None or usage.sessions_held <= 0 or usage.enrolled_learners <= 0:
        return None
    if usage.active_learners > 0:
        return None
    return _anomaly(
        AnomalyCode.USAGE_NO_ACTIVITY,
        AnomalySeverity.CRITICAL,
        f"No active learners against {usage.enrolled_learners} enrolled after "
        f"{usage.sessions_held} session(s).",
        enrolled=usage.enrolled_learners,
        sessions_held=usage.sessions_held,
    )


def detect_low_active_share(ctx: MonitorContext) -> Anomaly | None:
    """Active learners as a share of enrolled has fallen below the floor.

    Silent when `active == 0`: `detect_no_activity` already owns that case, and
    two anomalies for one root cause read as two problems to whoever picks it up.
    """
    usage = ctx.usage
    if usage is None or usage.sessions_held <= 0 or usage.enrolled_learners <= 0:
        return None
    if usage.active_learners <= 0:
        return None

    share = Decimal(usage.active_learners) / Decimal(usage.enrolled_learners)
    if share >= ACTIVE_SHARE_WARNING:
        return None
    severity = (
        AnomalySeverity.CRITICAL if share < ACTIVE_SHARE_CRITICAL else AnomalySeverity.WARNING
    )
    return _anomaly(
        AnomalyCode.USAGE_LOW_ACTIVE_SHARE,
        severity,
        f"{usage.active_learners} of {usage.enrolled_learners} learners active "
        f"(share {share}, floor {ACTIVE_SHARE_WARNING}).",
        active=usage.active_learners,
        enrolled=usage.enrolled_learners,
        active_share=share,
        threshold=ACTIVE_SHARE_WARNING,
    )


# --- Syllabus ----------------------------------------------------------------


def detect_syllabus_implausible(ctx: MonitorContext) -> Anomaly | None:
    """Completion outside 0..100, or moving backwards.

    A data fault, not a delivery fault, and separated because the fix is
    different: nobody should be chased about a program whose reported number is
    wrong. It runs before the shortfall detector for the same reason a bad
    reading must not be presented as "behind schedule".
    """
    syllabus = ctx.syllabus
    if syllabus is None:
        return None
    reasons: list[str] = []
    if syllabus.completion_percent < _ZERO or syllabus.completion_percent > _HUNDRED:
        reasons.append(f"completion {syllabus.completion_percent}% outside 0..100")
    previous = syllabus.previous_completion_percent
    if previous is not None and syllabus.completion_percent < previous:
        reasons.append(f"completion fell from {previous}% to {syllabus.completion_percent}%")
    if not reasons:
        return None
    return _anomaly(
        AnomalyCode.SYLLABUS_IMPLAUSIBLE,
        AnomalySeverity.WARNING,
        "Syllabus reading is not plausible: " + "; ".join(reasons) + ".",
        completion_percent=syllabus.completion_percent,
        previous_completion_percent=previous,
    )


def detect_syllabus_behind(ctx: MonitorContext) -> Anomaly | None:
    """Completion trails the elapsed schedule by more than tolerance.

    Silent when the reading is implausible — `detect_syllabus_implausible` owns
    that, and computing a shortfall from a broken number produces a confident
    wrong figure, which is worse than no figure (R1).
    """
    syllabus = ctx.syllabus
    if syllabus is None:
        return None
    if syllabus.completion_percent < _ZERO or syllabus.completion_percent > _HUNDRED:
        return None

    shortfall = syllabus.expected_percent - syllabus.completion_percent
    if shortfall <= SYLLABUS_SHORTFALL_WARNING:
        return None
    severity = (
        AnomalySeverity.CRITICAL
        if shortfall > SYLLABUS_SHORTFALL_CRITICAL
        else AnomalySeverity.WARNING
    )
    return _anomaly(
        AnomalyCode.SYLLABUS_BEHIND_SCHEDULE,
        severity,
        f"Syllabus at {syllabus.completion_percent}% against {syllabus.expected_percent}% "
        f"expected — {shortfall} points behind.",
        completion_percent=syllabus.completion_percent,
        expected_percent=syllabus.expected_percent,
        shortfall_points=shortfall,
        threshold_points=SYLLABUS_SHORTFALL_WARNING,
    )


def detect_syllabus_stalled(ctx: MonitorContext) -> Anomaly | None:
    """Completion has not moved in `SYLLABUS_STALL_DAYS`.

    Independent of the shortfall detector: a batch that is ahead of schedule and
    has not moved for a fortnight is still a batch that stopped, and the
    shortfall rule would stay silent about it right up until it was behind.
    """
    syllabus = ctx.syllabus
    if syllabus is None or syllabus.previous_completion_percent is None:
        return None
    if syllabus.days_since_previous_reading < SYLLABUS_STALL_DAYS:
        return None
    if syllabus.completion_percent != syllabus.previous_completion_percent:
        return None
    if syllabus.completion_percent >= _HUNDRED:
        # A finished syllabus does not move. That is completion, not a stall.
        return None
    return _anomaly(
        AnomalyCode.SYLLABUS_STALLED,
        AnomalySeverity.WARNING,
        f"Syllabus unchanged at {syllabus.completion_percent}% for "
        f"{syllabus.days_since_previous_reading} day(s).",
        completion_percent=syllabus.completion_percent,
        days_since_previous_reading=syllabus.days_since_previous_reading,
        threshold_days=SYLLABUS_STALL_DAYS,
    )


Detector = Callable[[MonitorContext], Anomaly | None]

#: Detectors in reporting order: attendance, then usage, then syllabus — §8's own
#: ordering. Every detector runs regardless of what fired before it; see the
#: module docstring on short-circuiting. The order is fixed rather than
#: alphabetical so an alert reads the same way every week.
DETECTORS: Final[tuple[Detector, ...]] = (
    detect_unmarked_days,
    detect_marking_stale,
    detect_absence_streak,
    detect_absence_rate,
    detect_no_activity,
    detect_low_active_share,
    detect_syllabus_implausible,
    detect_syllabus_behind,
    detect_syllabus_stalled,
)


def detect_anomalies(
    ctx: MonitorContext, detectors: Sequence[Detector] | None = None
) -> tuple[Anomaly, ...]:
    """Run every detector and return what fired, in declaration order."""
    selected = DETECTORS if detectors is None else detectors
    return tuple(a for detector in selected if (a := detector(ctx)) is not None)
