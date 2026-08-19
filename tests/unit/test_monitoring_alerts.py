"""Risk scoring and internal alerts. CLAUDE.md §8, §12.

The claims under test:

* the risk score is a plain, reproducible sum of severity weights;
* banding is the published table in `app.domain.risk.RISK_BAND_FLOOR`;
* an alert **cannot** be addressed to a TRAINER or a COLLEGE — §8 caps the
  Delivery Monitor at "Alert (internal only)", and that ceiling is enforced by
  `build_alert` raising, not by a comment.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from app.domain.attendance import expand_period
from app.domain.enums import AttendanceMark, AutonomyLevel, Persona, ProgramType
from app.domain.risk import AnomalyCode, AnomalySeverity, RiskBand, band_for_score
from app.services.monitoring.alerts import build_alert
from app.services.monitoring.detectors import Anomaly
from app.services.monitoring.scoring import (
    MONITOR_AUTONOMY_CEILING,
    assess_risk,
    score_anomalies,
)
from app.services.monitoring.signals import AttendanceSignal, MonitorContext, SyllabusSignal

PROGRAM_ID = UUID("11111111-1111-1111-1111-111111111111")
COLLEGE_ID = UUID("22222222-2222-2222-2222-222222222222")
AS_OF = datetime(2026, 7, 15, 6, 0, tzinfo=UTC)


def _anomaly(severity: AnomalySeverity) -> Anomaly:
    return Anomaly(code=AnomalyCode.SYLLABUS_STALLED, severity=severity, message="x")


def _context(program_type: ProgramType = ProgramType.CRT) -> MonitorContext:
    marks = {date(2026, 7, d): AttendanceMark.PRESENT for d in range(1, 15) if d not in {9, 10}}
    return MonitorContext(
        program_id=PROGRAM_ID,
        college_id=COLLEGE_ID,
        program_type=program_type,
        as_of=AS_OF,
        attendance=AttendanceSignal(
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 31),
            days=tuple(expand_period(date(2026, 7, 1), date(2026, 7, 31), marks)),
            elapsed_through=date(2026, 7, 14),
            last_marked_at=AS_OF,
        ),
        syllabus=SyllabusSignal(completion_percent=Decimal(5), expected_percent=Decimal(50)),
    )


# --- scoring -----------------------------------------------------------------


def test_score_is_the_sum_of_severity_weights():
    anomalies = [
        _anomaly(AnomalySeverity.CRITICAL),
        _anomaly(AnomalySeverity.WARNING),
        _anomaly(AnomalySeverity.INFO),
    ]
    assert score_anomalies(anomalies) == Decimal(12)


def test_clean_program_scores_zero_and_bands_low():
    assert score_anomalies([]) == Decimal(0)
    assert band_for_score(Decimal(0)) is RiskBand.LOW


@pytest.mark.parametrize(
    ("score", "band"),
    [
        (Decimal(0), RiskBand.LOW),
        (Decimal(2), RiskBand.LOW),
        (Decimal(3), RiskBand.MEDIUM),
        (Decimal(8), RiskBand.HIGH),
        (Decimal(16), RiskBand.CRITICAL),
        (Decimal(99), RiskBand.CRITICAL),
    ],
)
def test_banding_matches_the_published_table(score, band):
    assert band_for_score(score) is band


def test_one_critical_anomaly_reaches_high_but_never_critical():
    """CRITICAL band means failing on more than one axis — see RISK_BAND_FLOOR."""
    assert band_for_score(score_anomalies([_anomaly(AnomalySeverity.CRITICAL)])) is RiskBand.HIGH


def test_assessment_is_deterministic():
    """§12: the same input evaluated twice gives an identical result."""
    ctx = _context()
    assert assess_risk(ctx) == assess_risk(ctx)


def test_assessment_carries_program_type_and_critical_anomalies():
    assessment = assess_risk(_context(ProgramType.CRT))
    assert assessment.program_type is ProgramType.CRT
    assert not assessment.is_clean
    assert AnomalyCode.ATTENDANCE_UNMARKED_DAYS in {a.code for a in assessment.critical}


def test_monitor_stays_at_autonomy_level_observe():
    """§8: the Delivery Monitor's ceiling is Alert (internal only) — level 1."""
    assert MONITOR_AUTONOMY_CEILING is AutonomyLevel.OBSERVE


# --- alerts ------------------------------------------------------------------


def test_alert_renders_critical_lines_first():
    assessment = assess_risk(_context(ProgramType.CRT))
    alert = build_alert(assessment, (Persona.MANAGER, Persona.LDE_EXECUTIVE))

    assert alert.is_internal_only
    assert alert.lines[0].startswith("[critical]")
    assert alert.band.value in alert.headline.lower()


@pytest.mark.parametrize("persona", [Persona.TRAINER, Persona.COLLEGE])
def test_alert_refuses_an_external_audience(persona):
    """§8: Alert, INTERNAL ONLY. External contact is the Comms Service's, behind
    human approval (R4)."""
    assessment = assess_risk(_context())
    with pytest.raises(ValueError, match="external persona"):
        build_alert(assessment, (Persona.MANAGER, persona))


def test_alert_refuses_an_empty_audience():
    assessment = assess_risk(_context())
    with pytest.raises(ValueError, match="at least one recipient"):
        build_alert(assessment, ())
