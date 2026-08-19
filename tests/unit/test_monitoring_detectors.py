"""Delivery Monitor detector tests. CLAUDE.md §12.

One firing case and one non-firing case per detector, plus the two things that
are easy to break and expensive to notice:

* **the CLAUDE.md §5 asymmetry** — one unmarked day is CRITICAL on CRT and only
  INFO on bCAP, because CRT counts payable days UP from `P` marks (an unmarked
  day underpays, silently) and bCAP counts DOWN from period length (an unmarked
  day is paid, silently);
* **determinism** — the same context evaluated twice gives an identical result.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from app.domain.attendance import expand_period
from app.domain.enums import AttendanceMark, ProgramType
from app.domain.risk import AnomalyCode, AnomalySeverity
from app.services.monitoring.detectors import (
    detect_absence_rate,
    detect_absence_streak,
    detect_anomalies,
    detect_low_active_share,
    detect_marking_stale,
    detect_no_activity,
    detect_syllabus_behind,
    detect_syllabus_implausible,
    detect_syllabus_stalled,
    detect_unmarked_days,
)
from app.services.monitoring.signals import (
    AttendanceSignal,
    MonitorContext,
    SyllabusSignal,
    UsageSignal,
)

PROGRAM_ID = UUID("11111111-1111-1111-1111-111111111111")
COLLEGE_ID = UUID("22222222-2222-2222-2222-222222222222")

PERIOD_START = date(2026, 7, 1)
PERIOD_END = date(2026, 7, 31)
AS_OF = datetime(2026, 7, 15, 6, 0, tzinfo=UTC)


def _attendance(
    marks: dict[date, AttendanceMark],
    *,
    elapsed_through: date = date(2026, 7, 14),
    last_marked_at: datetime | None = AS_OF,
) -> AttendanceSignal:
    return AttendanceSignal(
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        days=tuple(expand_period(PERIOD_START, PERIOD_END, marks)),
        elapsed_through=elapsed_through,
        last_marked_at=last_marked_at,
    )


def _all_present(through: date) -> dict[date, AttendanceMark]:
    out: dict[date, AttendanceMark] = {}
    day = PERIOD_START
    while day <= through:
        out[day] = AttendanceMark.PRESENT
        day = date.fromordinal(day.toordinal() + 1)
    return out


def _ctx(
    program_type: ProgramType = ProgramType.BCAP,
    *,
    attendance: AttendanceSignal | None = None,
    usage: UsageSignal | None = None,
    syllabus: SyllabusSignal | None = None,
) -> MonitorContext:
    return MonitorContext(
        program_id=PROGRAM_ID,
        college_id=COLLEGE_ID,
        program_type=program_type,
        as_of=AS_OF,
        attendance=attendance,
        usage=usage,
        syllabus=syllabus,
    )


# --- unmarked days: the §5 branch --------------------------------------------


def test_unmarked_day_is_critical_for_crt():
    """CRT counts UP from P marks: one missing mark is an underpayment in flight."""
    marks = _all_present(date(2026, 7, 14))
    del marks[date(2026, 7, 9)]
    anomaly = detect_unmarked_days(_ctx(ProgramType.CRT, attendance=_attendance(marks)))

    assert anomaly is not None
    assert anomaly.code is AnomalyCode.ATTENDANCE_UNMARKED_DAYS
    assert anomaly.severity is AnomalySeverity.CRITICAL
    assert anomaly.detail["unmarked_days"] == "1"


def test_unmarked_day_is_only_info_for_bcap():
    """bCAP counts DOWN from period length: the same day is paid, not lost."""
    marks = _all_present(date(2026, 7, 14))
    del marks[date(2026, 7, 9)]
    anomaly = detect_unmarked_days(_ctx(ProgramType.BCAP, attendance=_attendance(marks)))

    assert anomaly is not None
    assert anomaly.severity is AnomalySeverity.INFO


def test_bcap_warns_once_unmarked_days_are_material():
    marks = _all_present(date(2026, 7, 14))
    for day in (date(2026, 7, 9), date(2026, 7, 10), date(2026, 7, 11)):
        del marks[day]
    anomaly = detect_unmarked_days(_ctx(ProgramType.BCAP, attendance=_attendance(marks)))

    assert anomaly is not None
    assert anomaly.severity is AnomalySeverity.WARNING


def test_unmarked_days_silent_when_every_elapsed_day_marked():
    ctx = _ctx(ProgramType.CRT, attendance=_attendance(_all_present(date(2026, 7, 14))))
    assert detect_unmarked_days(ctx) is None


def test_future_days_are_not_unmarked():
    """The 20th is not missing on the 14th — see `AttendanceSignal.elapsed_through`."""
    ctx = _ctx(ProgramType.CRT, attendance=_attendance(_all_present(date(2026, 7, 14))))
    # The signal covers 1..31 July; only 1..14 have marks, and nothing fires.
    assert detect_unmarked_days(ctx) is None


# --- marking staleness -------------------------------------------------------


def test_marking_stale_fires_past_threshold():
    signal = _attendance(
        _all_present(date(2026, 7, 14)),
        last_marked_at=datetime(2026, 7, 12, 5, 0, tzinfo=UTC),
    )
    anomaly = detect_marking_stale(_ctx(attendance=signal))

    assert anomaly is not None
    assert anomaly.code is AnomalyCode.ATTENDANCE_MARKING_STALE
    assert anomaly.detail["hours_since_mark"] == "73"


def test_marking_stale_silent_when_recent():
    signal = _attendance(
        _all_present(date(2026, 7, 14)),
        last_marked_at=datetime(2026, 7, 14, 6, 0, tzinfo=UTC),
    )
    assert detect_marking_stale(_ctx(attendance=signal)) is None


def test_marking_never_done_fires():
    signal = _attendance(_all_present(date(2026, 7, 14)), last_marked_at=None)
    anomaly = detect_marking_stale(_ctx(attendance=signal))
    assert anomaly is not None
    assert anomaly.detail["last_marked_at"] == "None"


# --- absence streak and rate -------------------------------------------------


def test_absence_streak_fires_at_three():
    marks = _all_present(date(2026, 7, 14))
    for day in (date(2026, 7, 6), date(2026, 7, 7), date(2026, 7, 8)):
        marks[day] = AttendanceMark.ABSENT
    anomaly = detect_absence_streak(_ctx(attendance=_attendance(marks)))

    assert anomaly is not None
    assert anomaly.severity is AnomalySeverity.WARNING
    assert anomaly.detail["streak_days"] == "3"
    assert anomaly.detail["streak_start"] == "2026-07-06"


def test_absence_streak_escalates_to_critical_at_five():
    marks = _all_present(date(2026, 7, 14))
    for offset in range(6, 11):
        marks[date(2026, 7, offset)] = AttendanceMark.ABSENT
    anomaly = detect_absence_streak(_ctx(attendance=_attendance(marks)))

    assert anomaly is not None
    assert anomaly.severity is AnomalySeverity.CRITICAL


def test_absence_streak_silent_for_scattered_absences():
    marks = _all_present(date(2026, 7, 14))
    marks[date(2026, 7, 3)] = AttendanceMark.ABSENT
    marks[date(2026, 7, 9)] = AttendanceMark.ABSENT
    assert detect_absence_streak(_ctx(attendance=_attendance(marks))) is None


def test_absence_streak_ignores_unmarked_days():
    """An unmarked day is a clerical gap, not a trainer who stopped attending."""
    marks = _all_present(date(2026, 7, 14))
    for day in (date(2026, 7, 6), date(2026, 7, 7), date(2026, 7, 8)):
        del marks[day]
    assert detect_absence_streak(_ctx(attendance=_attendance(marks))) is None


def test_absence_rate_fires_above_tolerance():
    marks = _all_present(date(2026, 7, 14))
    for day in (date(2026, 7, 2), date(2026, 7, 5), date(2026, 7, 9), date(2026, 7, 12)):
        marks[day] = AttendanceMark.ABSENT
    anomaly = detect_absence_rate(_ctx(attendance=_attendance(marks)))

    assert anomaly is not None
    assert anomaly.code is AnomalyCode.ATTENDANCE_ABSENCE_RATE


def test_absence_rate_silent_below_minimum_sample():
    """One absence out of two marked days is 50% and means nothing."""
    marks = {
        date(2026, 7, 1): AttendanceMark.PRESENT,
        date(2026, 7, 2): AttendanceMark.ABSENT,
    }
    signal = _attendance(marks, elapsed_through=date(2026, 7, 2))
    assert detect_absence_rate(_ctx(attendance=signal)) is None


def test_absence_rate_counts_half_days_as_half():
    marks = _all_present(date(2026, 7, 14))
    for day in (date(2026, 7, 2), date(2026, 7, 5), date(2026, 7, 9)):
        marks[day] = AttendanceMark.HALF_DAY
    anomaly = detect_absence_rate(_ctx(attendance=_attendance(marks)))
    # 1.5 lost over 14 marked days = 0.107, under tolerance.
    assert anomaly is None


# --- usage -------------------------------------------------------------------


def test_no_activity_fires_after_sessions_held():
    usage = UsageSignal(enrolled_learners=60, active_learners=0, sessions_held=4)
    anomaly = detect_no_activity(_ctx(usage=usage))

    assert anomaly is not None
    assert anomaly.severity is AnomalySeverity.CRITICAL


def test_no_activity_silent_before_the_batch_starts():
    usage = UsageSignal(enrolled_learners=60, active_learners=0, sessions_held=0)
    assert detect_no_activity(_ctx(usage=usage)) is None


def test_low_active_share_fires_and_no_activity_owns_the_zero_case():
    low = UsageSignal(enrolled_learners=100, active_learners=45, sessions_held=3)
    anomaly = detect_low_active_share(_ctx(usage=low))
    assert anomaly is not None
    assert anomaly.severity is AnomalySeverity.WARNING
    assert anomaly.detail["active_share"] == "0.45"

    zero = UsageSignal(enrolled_learners=100, active_learners=0, sessions_held=3)
    assert detect_low_active_share(_ctx(usage=zero)) is None


def test_low_active_share_critical_below_floor():
    usage = UsageSignal(enrolled_learners=100, active_learners=20, sessions_held=3)
    anomaly = detect_low_active_share(_ctx(usage=usage))
    assert anomaly is not None
    assert anomaly.severity is AnomalySeverity.CRITICAL


def test_low_active_share_silent_at_healthy_engagement():
    usage = UsageSignal(enrolled_learners=100, active_learners=80, sessions_held=3)
    assert detect_low_active_share(_ctx(usage=usage)) is None


# --- syllabus ----------------------------------------------------------------


def test_syllabus_behind_fires_past_tolerance():
    syllabus = SyllabusSignal(completion_percent=Decimal(30), expected_percent=Decimal(50))
    anomaly = detect_syllabus_behind(_ctx(syllabus=syllabus))
    assert anomaly is not None
    assert anomaly.detail["shortfall_points"] == "20"


def test_syllabus_behind_silent_within_tolerance():
    syllabus = SyllabusSignal(completion_percent=Decimal(40), expected_percent=Decimal(50))
    assert detect_syllabus_behind(_ctx(syllabus=syllabus)) is None


def test_syllabus_behind_silent_on_an_implausible_reading():
    """A confident wrong figure is worse than no figure (R1)."""
    syllabus = SyllabusSignal(completion_percent=Decimal(-10), expected_percent=Decimal(50))
    assert detect_syllabus_behind(_ctx(syllabus=syllabus)) is None
    assert detect_syllabus_implausible(_ctx(syllabus=syllabus)) is not None


def test_syllabus_implausible_silent_on_a_sane_reading():
    syllabus = SyllabusSignal(
        completion_percent=Decimal(55),
        expected_percent=Decimal(50),
        previous_completion_percent=Decimal(40),
    )
    assert detect_syllabus_implausible(_ctx(syllabus=syllabus)) is None


def test_syllabus_stalled_fires_when_completion_has_not_moved():
    syllabus = SyllabusSignal(
        completion_percent=Decimal(40),
        expected_percent=Decimal(45),
        previous_completion_percent=Decimal(40),
        days_since_previous_reading=10,
    )
    anomaly = detect_syllabus_stalled(_ctx(syllabus=syllabus))
    assert anomaly is not None
    assert anomaly.code is AnomalyCode.SYLLABUS_STALLED


def test_syllabus_stalled_silent_when_progress_was_made():
    syllabus = SyllabusSignal(
        completion_percent=Decimal(52),
        expected_percent=Decimal(50),
        previous_completion_percent=Decimal(40),
        days_since_previous_reading=10,
    )
    assert detect_syllabus_stalled(_ctx(syllabus=syllabus)) is None


def test_finished_syllabus_is_not_stalled():
    syllabus = SyllabusSignal(
        completion_percent=Decimal(100),
        expected_percent=Decimal(100),
        previous_completion_percent=Decimal(100),
        days_since_previous_reading=30,
    )
    assert detect_syllabus_stalled(_ctx(syllabus=syllabus)) is None


# --- the whole set -----------------------------------------------------------


def test_absent_signals_produce_no_anomalies():
    """A program with nothing observed yet must not read as a program in trouble."""
    assert detect_anomalies(_ctx()) == ()


def test_detection_is_deterministic():
    """§12: the same input evaluated twice gives an identical result."""
    marks = _all_present(date(2026, 7, 14))
    del marks[date(2026, 7, 9)]
    for day in (date(2026, 7, 6), date(2026, 7, 7), date(2026, 7, 8)):
        marks[day] = AttendanceMark.ABSENT
    ctx = _ctx(
        ProgramType.CRT,
        attendance=_attendance(marks),
        usage=UsageSignal(enrolled_learners=100, active_learners=20, sessions_held=3),
        syllabus=SyllabusSignal(completion_percent=Decimal(20), expected_percent=Decimal(50)),
    )

    first = detect_anomalies(ctx)
    second = detect_anomalies(ctx)

    assert first == second
    assert [a.code for a in first] == [a.code for a in second]


def test_detectors_run_in_declared_order_without_short_circuit():
    marks = _all_present(date(2026, 7, 14))
    del marks[date(2026, 7, 9)]
    ctx = _ctx(
        ProgramType.CRT,
        attendance=_attendance(marks),
        syllabus=SyllabusSignal(completion_percent=Decimal(10), expected_percent=Decimal(50)),
    )
    codes = [a.code for a in detect_anomalies(ctx)]

    assert codes == [
        AnomalyCode.ATTENDANCE_UNMARKED_DAYS,
        AnomalyCode.SYLLABUS_BEHIND_SCHEDULE,
    ]
