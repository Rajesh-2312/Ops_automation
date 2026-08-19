"""Internal alerts. CLAUDE.md §8 — ceiling "Alert (**internal only**)".

An alert is a rendering of a `RiskAssessment` for people inside byteXL. It is not
a message to a college, not a nudge to a trainer, and not a queued outbound
comm — the Comms Service (§8, "single outbound queue") is the only thing that
ever addresses an external party, and it does so behind human approval (R4).

That constraint is enforced here rather than asserted in a docstring:
`build_alert` refuses to produce an alert for a persona outside
`INTERNAL_PERSONAS`. TRAINER and COLLEGE are external parties in §4 terms, so an
alert addressed to either raises. A future caller who wants to "just tell the
college" has to change this function, in a diff someone reviews.

R1 holds for the rendered text: every figure in a line came out of an anomaly's
`detail`, which the caller passed in as structured input. Nothing in this module
computes a fact.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.domain.enums import INTERNAL_PERSONAS, Persona
from app.domain.risk import AnomalySeverity, RiskBand
from app.services.monitoring.scoring import RiskAssessment

__all__ = ["InternalAlert", "build_alert"]

#: Order anomalies are rendered in: loudest first, then detector order within a
#: severity. A Manager reads the top of an alert and stops; the critical line
#: must not be third.
_SEVERITY_RANK: dict[AnomalySeverity, int] = {
    AnomalySeverity.CRITICAL: 0,
    AnomalySeverity.WARNING: 1,
    AnomalySeverity.INFO: 2,
}


@dataclass(frozen=True, slots=True)
class InternalAlert:
    """One internal alert. Data, not a delivery — nothing here sends it."""

    program_id: UUID
    college_id: UUID
    as_of: datetime
    band: RiskBand
    audience: tuple[Persona, ...]
    headline: str
    lines: tuple[str, ...]

    @property
    def is_internal_only(self) -> bool:
        """True when every recipient is byteXL staff. Invariant, not a check —
        `build_alert` cannot construct an alert for which this is false."""
        return all(p in INTERNAL_PERSONAS for p in self.audience)


def build_alert(assessment: RiskAssessment, audience: tuple[Persona, ...]) -> InternalAlert:
    """Render an assessment for an internal audience.

    Raises `ValueError` if any recipient is external (§4: TRAINER and COLLEGE),
    or if the audience is empty — an alert with no reader is a silently dropped
    signal, and this service exists to not drop signals.
    """
    if not audience:
        raise ValueError("an internal alert needs at least one recipient persona")
    external = tuple(p for p in audience if p not in INTERNAL_PERSONAS)
    if external:
        raise ValueError(
            f"alert audience contains external persona(s) {[p.value for p in external]} — "
            "the Delivery Monitor's ceiling is Alert, INTERNAL ONLY (CLAUDE.md §8). "
            "External contact goes through the Comms Service behind human approval (R4)."
        )

    ordered = sorted(
        assessment.anomalies,
        key=lambda a: _SEVERITY_RANK[a.severity],
    )
    lines = tuple(f"[{a.severity.value}] {a.code.value}: {a.message}" for a in ordered)
    headline = (
        f"Delivery risk {assessment.band.value.upper()} "
        f"(score {assessment.score}, {len(assessment.anomalies)} anomaly/anomalies)"
    )
    return InternalAlert(
        program_id=assessment.program_id,
        college_id=assessment.college_id,
        as_of=assessment.as_of,
        band=assessment.band,
        audience=audience,
        headline=headline,
        lines=lines,
    )
