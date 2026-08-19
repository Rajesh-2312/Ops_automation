"""Risk scoring. CLAUDE.md §8 — "attendance, usage, syllabus anomalies, **risk
scoring**".

The score is a sum of severity weights over the anomalies that fired. Nothing
cleverer, on purpose:

* **It is explainable line by line.** A Manager asking "why is this program HIGH"
  gets the arithmetic, not a model's opinion. §8 puts this agent at autonomy
  level 1 — Observe — and an unexplainable score is a decision wearing an
  observation's clothes.
* **It is stable.** `Decimal` weights summed in detector order give the same
  score every run. A monitor whose numbers drift between runs teaches people to
  ignore the number.
* **It cannot be tuned into a decision.** There is no threshold in here that
  triggers an action. Acting on a score is the Escalation Engine's job, through
  SLA rules that are written down as data.

Two anomalies of the same code cannot both fire — detectors return one anomaly
each — so no de-duplication is needed and none is hidden here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from app.domain.enums import AutonomyLevel, ProgramType
from app.domain.risk import (
    ANOMALY_SEVERITY_WEIGHT,
    AnomalySeverity,
    RiskBand,
    band_for_score,
)
from app.services.monitoring.detectors import Anomaly, Detector, detect_anomalies
from app.services.monitoring.signals import MonitorContext

__all__ = ["MONITOR_AUTONOMY_CEILING", "RiskAssessment", "assess_risk", "score_anomalies"]

#: CLAUDE.md §8: the Delivery Monitor's ceiling is "Alert (internal only)" —
#: level 1, Observe. Stated as a constant so a test can assert it and so raising
#: it becomes a visible edit rather than a quiet drift.
MONITOR_AUTONOMY_CEILING: AutonomyLevel = AutonomyLevel.OBSERVE


def score_anomalies(anomalies: Sequence[Anomaly]) -> Decimal:
    """Sum the severity weights. Empty -> 0."""
    total = Decimal(0)
    for anomaly in anomalies:
        total += ANOMALY_SEVERITY_WEIGHT[anomaly.severity]
    return total


@dataclass(frozen=True, slots=True)
class RiskAssessment:
    """One program, one observation instant, everything that fired.

    Carries `program_type` because every reader of this object needs §5 in hand
    to interpret an attendance anomaly, and because the escalation rules select
    on it.
    """

    program_id: UUID
    college_id: UUID
    program_type: ProgramType
    as_of: datetime
    anomalies: tuple[Anomaly, ...]
    score: Decimal
    band: RiskBand

    @property
    def critical(self) -> tuple[Anomaly, ...]:
        return tuple(a for a in self.anomalies if a.severity is AnomalySeverity.CRITICAL)

    @property
    def is_clean(self) -> bool:
        return not self.anomalies


def assess_risk(ctx: MonitorContext, detectors: Sequence[Detector] | None = None) -> RiskAssessment:
    """Run the detectors and band the result. Pure; no I/O, no clock."""
    anomalies = detect_anomalies(ctx, detectors)
    score = score_anomalies(anomalies)
    return RiskAssessment(
        program_id=ctx.program_id,
        college_id=ctx.college_id,
        program_type=ctx.program_type,
        as_of=ctx.as_of,
        anomalies=anomalies,
        score=score,
        band=band_for_score(score),
    )
