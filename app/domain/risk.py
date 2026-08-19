"""Delivery-risk and escalation vocabulary. CLAUDE.md §8.

Two shared services live off this module:

* `app.services.monitoring` — the Delivery Monitor. Ceiling **Alert, internal
  only**; autonomy level 1 (Observe) on the §8 ladder. It reads attendance,
  usage and syllabus signals, names anomalies, and scores risk. It decides
  nothing and contacts nobody.
* `app.services.escalation` — the Escalation Engine. §8: "**deterministic SLA
  rules. Not LLM judgement.**"

§11 puts enums in `domain/`, and `app/domain/enums.py` is shared with other
workstreams, so this vocabulary lives in its own module. §3 holds: zero I/O,
nothing imported from `db/`, `api/`, `agents/` or `services/`.

WHY SEVERITY IS NOT ON THE CODE
-------------------------------
`AnomalyCode` names *what was observed*; severity is decided by the detector,
because CLAUDE.md §5 makes the same observation mean opposite things by program
type. An unmarked day silently UNDERPAYS a CRT trainer (payable days are counted
UP from `P` marks, so a missing mark is a missing payment nobody sees) and
silently PAYS a bCAP trainer (counted DOWN from period length, so a missing mark
is money out of the door). `app/services/remuneration/validators.py` already
encodes that asymmetry as BLOCKING-for-CRT / WARNING-for-bCAP, and the monitor
must mirror it rather than treat unmarked days uniformly. Pinning a severity to
the code would force two codes for one observation and lose the ability to
report "attendance incomplete" across both program types.

Values are stable identifiers: an alert row, a UI filter and an acknowledgement
record all key off them. Changing one is a migration, not a refactor.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Final

from app.domain.enums import Persona

__all__ = [
    "ANOMALY_SEVERITY_WEIGHT",
    "AnomalyCode",
    "AnomalySeverity",
    "Comparison",
    "EscalationTier",
    "RISK_BAND_FLOOR",
    "RiskBand",
    "SlaCode",
    "SlaMetric",
    "TIER_PERSONA",
    "TIER_ORDER",
    "band_for_score",
    "next_tier",
]


class AnomalySeverity(StrEnum):
    """How loudly one observation should read on an internal alert.

    Three levels, not five. The monitor's only output is an internal alert, and a
    scale finer than "note it / look at it / act today" invites argument about
    the boundary instead of about the program.
    """

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AnomalyCode(StrEnum):
    """What the Delivery Monitor observed. §8: "attendance, usage, syllabus"."""

    # --- Attendance ---------------------------------------------------------
    #: Days in the period carry no mark at all. Severity is program-type
    #: dependent — see the module docstring and §5.
    ATTENDANCE_UNMARKED_DAYS = "attendance_unmarked_days"
    #: Nobody has marked attendance for N consecutive elapsed days.
    ATTENDANCE_MARKING_STALE = "attendance_marking_stale"
    #: A run of consecutive `A` marks — a trainer who has stopped showing up.
    ATTENDANCE_ABSENCE_STREAK = "attendance_absence_streak"
    #: Absence rate across the marked part of the period exceeds tolerance.
    ATTENDANCE_ABSENCE_RATE = "attendance_absence_rate"

    # --- Platform usage -----------------------------------------------------
    #: Active learners as a share of enrolled has fallen below the floor.
    USAGE_LOW_ACTIVE_SHARE = "usage_low_active_share"
    #: No learner activity at all since a session that has already happened.
    USAGE_NO_ACTIVITY = "usage_no_activity"

    # --- Syllabus -----------------------------------------------------------
    #: Syllabus completion trails elapsed schedule by more than tolerance.
    SYLLABUS_BEHIND_SCHEDULE = "syllabus_behind_schedule"
    #: Reported completion has not moved since the previous observation.
    SYLLABUS_STALLED = "syllabus_stalled"
    #: Reported completion went backwards, or above 100%. A data fault, not a
    #: delivery fault, and worth separating because the fix is different.
    SYLLABUS_IMPLAUSIBLE = "syllabus_implausible"


#: Contribution of one anomaly to the program risk score, by severity.
#:
#: `Decimal`, not `float`: two runs of the monitor over the same signals must
#: produce byte-identical scores, and a float sum reorders into a different last
#: digit. R7 is about money, but the reason generalises — a risk score that
#: wobbles is a risk score people stop believing.
ANOMALY_SEVERITY_WEIGHT: Final[dict[AnomalySeverity, Decimal]] = {
    AnomalySeverity.INFO: Decimal(1),
    AnomalySeverity.WARNING: Decimal(3),
    AnomalySeverity.CRITICAL: Decimal(8),
}


class RiskBand(StrEnum):
    """The banded reading of a risk score, for a dashboard column."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


#: Inclusive lower bound of each band, highest first. A score is in the first
#: band whose floor it reaches. Ordered highest-first so `band_for_score` is a
#: single pass with no interval arithmetic to get subtly wrong.
#:
#: One CRITICAL anomaly (weight 8) alone reaches HIGH, and never CRITICAL on its
#: own: a single critical signal is a thing to look at today, whereas CRITICAL
#: band means the program is failing on more than one axis at once.
RISK_BAND_FLOOR: Final[tuple[tuple[RiskBand, Decimal], ...]] = (
    (RiskBand.CRITICAL, Decimal(16)),
    (RiskBand.HIGH, Decimal(8)),
    (RiskBand.MEDIUM, Decimal(3)),
    (RiskBand.LOW, Decimal(0)),
)


def band_for_score(score: Decimal) -> RiskBand:
    """Band a risk score. Total over `Decimal`, including negatives (-> LOW)."""
    for band, floor in RISK_BAND_FLOOR:
        if score >= floor:
            return band
    return RiskBand.LOW


class EscalationTier(StrEnum):
    """Who an escalation lands on. CLAUDE.md §4, and INTERNAL only.

    "Senior Manager -> Manager -> LDE Executive (on campus)". Escalation climbs
    that chain and never leaves it: there is no COLLEGE or TRAINER tier, because
    an escalation is an internal management signal and the §8 ceiling for both
    services in this workstream is "internal only".
    """

    LDE_EXECUTIVE = "lde_executive"
    MANAGER = "manager"
    SENIOR_MANAGER = "senior_manager"


#: The tier a tier climbs to. Senior Manager is terminal — above it there is no
#: internal recipient this platform models, and inventing one would be inventing
#: an org chart.
_TIER_ABOVE: Final[dict[EscalationTier, EscalationTier]] = {
    EscalationTier.LDE_EXECUTIVE: EscalationTier.MANAGER,
    EscalationTier.MANAGER: EscalationTier.SENIOR_MANAGER,
}

#: Ascending seniority. Used to order escalations and to compare two tiers
#: without relying on the enum's declaration order by accident.
TIER_ORDER: Final[tuple[EscalationTier, ...]] = (
    EscalationTier.LDE_EXECUTIVE,
    EscalationTier.MANAGER,
    EscalationTier.SENIOR_MANAGER,
)

#: Tier to persona. A tier says *which rung*; the persona is how the recipient is
#: recognised. **Reach is not here** — §4: "Persona lives on `profiles.role`.
#: Reach does not." Which humans on that rung can see this college comes from
#: `user_college_assignments` / `user_cluster_assignments`, resolved by the
#: database, never inferred from persona alone.
TIER_PERSONA: Final[dict[EscalationTier, Persona]] = {
    EscalationTier.LDE_EXECUTIVE: Persona.LDE_EXECUTIVE,
    EscalationTier.MANAGER: Persona.MANAGER,
    EscalationTier.SENIOR_MANAGER: Persona.SENIOR_MANAGER,
}


def next_tier(tier: EscalationTier) -> EscalationTier:
    """One rung up, saturating at Senior Manager.

    Saturates rather than raising: an unacknowledged escalation that has already
    reached the top must keep re-firing at the top, not fall over. Losing the
    signal is the worse failure.
    """
    return _TIER_ABOVE.get(tier, EscalationTier.SENIOR_MANAGER)


class SlaMetric(StrEnum):
    """The measurable quantities an SLA rule may be written against.

    A closed vocabulary on purpose. Rules are data (see
    `app.services.escalation.rules`), and data can only be evaluated
    deterministically if the set of things it can refer to is finite and
    pre-computed by the caller. A rule cannot invent a metric, cannot call
    anything, and cannot read a clock.
    """

    #: Hours a task has been open past its due date.
    TASK_HOURS_OVERDUE = "task_hours_overdue"
    #: Hours a required document has sat unsigned past its due date.
    DOCUMENT_HOURS_UNSIGNED = "document_hours_unsigned"
    #: Hours since anyone marked attendance for this batch.
    ATTENDANCE_HOURS_SINCE_MARK = "attendance_hours_since_mark"
    #: Unmarked days in the current payout period. Read with the program-type
    #: filter on the rule — §5 makes this mean opposite things on CRT and bCAP.
    ATTENDANCE_UNMARKED_DAYS = "attendance_unmarked_days"
    #: The Delivery Monitor's risk score for the program.
    DELIVERY_RISK_SCORE = "delivery_risk_score"
    #: Hours a payout run has been blocked by a §7 validation gate.
    PAYOUT_HOURS_BLOCKED = "payout_hours_blocked"
    #: Hours since an escalation was raised without being acknowledged.
    ESCALATION_HOURS_UNACKNOWLEDGED = "escalation_hours_unacknowledged"


class Comparison(StrEnum):
    """How an SLA rule compares its metric to its threshold.

    An enum, not a lambda. The whole point of §8's "deterministic SLA rules" is
    that a reviewer can read the rule table and know what fires; a callable in a
    rule row is unreadable in a diff, unserialisable into a config, and one step
    from being a model call.
    """

    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"


class SlaCode(StrEnum):
    """Stable identifier of an SLA rule.

    Written onto the escalation record and onto the acknowledgement, so it
    outlives any rewording of the rule's message.
    """

    TASK_OVERDUE = "task_overdue"
    TASK_SEVERELY_OVERDUE = "task_severely_overdue"
    DOCUMENT_UNSIGNED = "document_unsigned"
    ATTENDANCE_MARKING_STALE = "attendance_marking_stale"
    ATTENDANCE_UNMARKED_CRT = "attendance_unmarked_crt"
    ATTENDANCE_UNMARKED_BCAP = "attendance_unmarked_bcap"
    DELIVERY_RISK_HIGH = "delivery_risk_high"
    DELIVERY_RISK_CRITICAL = "delivery_risk_critical"
    PAYOUT_BLOCKED = "payout_blocked"
    ESCALATION_UNACKNOWLEDGED = "escalation_unacknowledged"
