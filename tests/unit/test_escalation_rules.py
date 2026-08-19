"""Escalation Engine tests. CLAUDE.md §8 and §12.

Every shipped rule gets a firing case and a non-firing case. Beyond that, three
properties of the engine itself are asserted, because they are what "deterministic
SLA rules. Not LLM judgement." means in practice:

* the same facts evaluated twice give an identical decision;
* a metric that was never measured never fires a rule;
* the CLAUDE.md §5 attendance rules select on program type, so a CRT consequence
  is never reported against a bCAP program (or the reverse).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from app.domain.enums import ProgramType
from app.domain.risk import (
    AnomalySeverity,
    Comparison,
    EscalationTier,
    SlaCode,
    SlaMetric,
)
from app.services.escalation.engine import SlaFacts, evaluate_sla
from app.services.escalation.rules import DEFAULT_RULES, SlaRule, validate_rules

SUBJECT_ID = UUID("33333333-3333-3333-3333-333333333333")
PROGRAM_ID = UUID("11111111-1111-1111-1111-111111111111")
COLLEGE_ID = UUID("22222222-2222-2222-2222-222222222222")
AS_OF = datetime(2026, 7, 15, 6, 0, tzinfo=UTC)


def _facts(
    metrics: dict[SlaMetric, Decimal],
    *,
    program_type: ProgramType = ProgramType.BCAP,
    current_tier: EscalationTier = EscalationTier.LDE_EXECUTIVE,
) -> SlaFacts:
    return SlaFacts(
        subject_table="program_tasks",
        subject_id=SUBJECT_ID,
        program_id=PROGRAM_ID,
        college_id=COLLEGE_ID,
        program_type=program_type,
        as_of=AS_OF,
        metrics=metrics,
        current_tier=current_tier,
    )


# --- one firing and one non-firing case per shipped rule ---------------------


@pytest.mark.parametrize(
    ("code", "metric", "firing", "quiet", "program_type"),
    [
        (
            SlaCode.TASK_OVERDUE,
            SlaMetric.TASK_HOURS_OVERDUE,
            Decimal(25),
            Decimal(24),
            ProgramType.BCAP,
        ),
        (
            SlaCode.TASK_SEVERELY_OVERDUE,
            SlaMetric.TASK_HOURS_OVERDUE,
            Decimal(73),
            Decimal(72),
            ProgramType.BCAP,
        ),
        (
            SlaCode.DOCUMENT_UNSIGNED,
            SlaMetric.DOCUMENT_HOURS_UNSIGNED,
            Decimal(121),
            Decimal(120),
            ProgramType.BCAP,
        ),
        (
            SlaCode.ATTENDANCE_MARKING_STALE,
            SlaMetric.ATTENDANCE_HOURS_SINCE_MARK,
            Decimal(73),
            Decimal(72),
            ProgramType.BCAP,
        ),
        (
            SlaCode.ATTENDANCE_UNMARKED_CRT,
            SlaMetric.ATTENDANCE_UNMARKED_DAYS,
            Decimal(1),
            Decimal(0),
            ProgramType.CRT,
        ),
        (
            SlaCode.ATTENDANCE_UNMARKED_BCAP,
            SlaMetric.ATTENDANCE_UNMARKED_DAYS,
            Decimal(3),
            Decimal(2),
            ProgramType.BCAP,
        ),
        (
            SlaCode.DELIVERY_RISK_HIGH,
            SlaMetric.DELIVERY_RISK_SCORE,
            Decimal(8),
            Decimal(7),
            ProgramType.BCAP,
        ),
        (
            SlaCode.DELIVERY_RISK_CRITICAL,
            SlaMetric.DELIVERY_RISK_SCORE,
            Decimal(16),
            Decimal(15),
            ProgramType.BCAP,
        ),
        (
            SlaCode.PAYOUT_BLOCKED,
            SlaMetric.PAYOUT_HOURS_BLOCKED,
            Decimal(49),
            Decimal(48),
            ProgramType.BCAP,
        ),
        (
            SlaCode.ESCALATION_UNACKNOWLEDGED,
            SlaMetric.ESCALATION_HOURS_UNACKNOWLEDGED,
            Decimal(25),
            Decimal(24),
            ProgramType.BCAP,
        ),
    ],
)
def test_each_rule_fires_and_stays_quiet_at_its_boundary(code, metric, firing, quiet, program_type):
    fired = evaluate_sla(_facts({metric: firing}, program_type=program_type)).codes()
    assert code in fired

    silent = evaluate_sla(_facts({metric: quiet}, program_type=program_type)).codes()
    assert code not in silent


# --- the CLAUDE.md §5 branch -------------------------------------------------


def test_crt_unmarked_day_escalates_immediately_and_critically():
    """CRT counts payable days UP from P marks: one missing mark underpays."""
    decision = evaluate_sla(
        _facts({SlaMetric.ATTENDANCE_UNMARKED_DAYS: Decimal(1)}, program_type=ProgramType.CRT)
    )
    (escalation,) = decision.escalations

    assert escalation.code is SlaCode.ATTENDANCE_UNMARKED_CRT
    assert escalation.severity is AnomalySeverity.CRITICAL
    assert "counts payable days UP" in escalation.reason


def test_bcap_tolerates_the_same_day_and_only_warns_when_material():
    """bCAP counts DOWN from period length: the day is paid, so it accumulates."""
    quiet = evaluate_sla(
        _facts({SlaMetric.ATTENDANCE_UNMARKED_DAYS: Decimal(1)}, program_type=ProgramType.BCAP)
    )
    assert quiet.escalations == ()

    loud = evaluate_sla(
        _facts({SlaMetric.ATTENDANCE_UNMARKED_DAYS: Decimal(3)}, program_type=ProgramType.BCAP)
    )
    (escalation,) = loud.escalations
    assert escalation.code is SlaCode.ATTENDANCE_UNMARKED_BCAP
    assert escalation.severity is AnomalySeverity.WARNING


def test_program_type_filter_never_crosses_the_branch():
    crt = evaluate_sla(
        _facts({SlaMetric.ATTENDANCE_UNMARKED_DAYS: Decimal(9)}, program_type=ProgramType.CRT)
    ).codes()
    bcap = evaluate_sla(
        _facts({SlaMetric.ATTENDANCE_UNMARKED_DAYS: Decimal(9)}, program_type=ProgramType.BCAP)
    ).codes()

    assert SlaCode.ATTENDANCE_UNMARKED_BCAP not in crt
    assert SlaCode.ATTENDANCE_UNMARKED_CRT not in bcap


# --- the ladder (§4) ---------------------------------------------------------


def test_unacknowledged_escalation_climbs_one_rung():
    decision = evaluate_sla(
        _facts(
            {SlaMetric.ESCALATION_HOURS_UNACKNOWLEDGED: Decimal(30)},
            current_tier=EscalationTier.LDE_EXECUTIVE,
        )
    )
    (escalation,) = decision.escalations
    assert escalation.tier is EscalationTier.MANAGER


def test_climb_saturates_at_senior_manager():
    """Losing the signal is worse than re-firing at the top."""
    decision = evaluate_sla(
        _facts(
            {SlaMetric.ESCALATION_HOURS_UNACKNOWLEDGED: Decimal(30)},
            current_tier=EscalationTier.SENIOR_MANAGER,
        )
    )
    (escalation,) = decision.escalations
    assert escalation.tier is EscalationTier.SENIOR_MANAGER


def test_highest_tier_reports_the_most_senior_rung_reached():
    decision = evaluate_sla(
        _facts(
            {
                SlaMetric.TASK_HOURS_OVERDUE: Decimal(100),
                SlaMetric.DELIVERY_RISK_SCORE: Decimal(20),
            }
        )
    )
    assert decision.highest_tier is EscalationTier.SENIOR_MANAGER


def test_nothing_fired_has_no_highest_tier():
    assert evaluate_sla(_facts({})).highest_tier is None


# --- engine properties -------------------------------------------------------


def test_evaluation_is_deterministic():
    """§12: the same input evaluated twice gives an identical result."""
    facts = _facts(
        {
            SlaMetric.TASK_HOURS_OVERDUE: Decimal(100),
            SlaMetric.DELIVERY_RISK_SCORE: Decimal(20),
            SlaMetric.PAYOUT_HOURS_BLOCKED: Decimal(60),
        }
    )
    assert evaluate_sla(facts) == evaluate_sla(facts)


def test_an_unmeasured_metric_never_fires():
    """Absent is not zero — a `LT` rule must not fire on data nobody collected."""
    assert evaluate_sla(_facts({})).escalations == ()


def test_no_short_circuit_every_matching_rule_reports():
    decision = evaluate_sla(_facts({SlaMetric.TASK_HOURS_OVERDUE: Decimal(100)}))
    assert decision.codes() == (SlaCode.TASK_OVERDUE, SlaCode.TASK_SEVERELY_OVERDUE)


def test_escalation_carries_the_numbers_that_made_it_fire():
    """§8: a reviewer must be able to read why something escalated."""
    (escalation,) = evaluate_sla(_facts({SlaMetric.PAYOUT_HOURS_BLOCKED: Decimal(60)})).escalations

    assert escalation.measured == Decimal(60)
    assert escalation.threshold == Decimal(48)
    assert escalation.comparison is Comparison.GT
    assert "Measured 60" in escalation.reason
    assert escalation.audit_payload()["threshold"] == "48"


def test_evaluation_uses_the_supplied_instant_not_a_clock():
    (escalation,) = evaluate_sla(_facts({SlaMetric.PAYOUT_HOURS_BLOCKED: Decimal(60)})).escalations
    assert escalation.at == AS_OF


# --- the rule table is data --------------------------------------------------


def test_rules_are_data_not_callables():
    """A callable in a rule row is where a model call would hide (§8)."""
    with pytest.raises(TypeError, match="callable"):
        SlaRule(
            code=SlaCode.TASK_OVERDUE,
            metric=SlaMetric.TASK_HOURS_OVERDUE,
            comparison=Comparison.GT,
            threshold=Decimal(1),
            tier=EscalationTier.MANAGER,
            severity=lambda facts: AnomalySeverity.WARNING,  # type: ignore[arg-type]
        )


def test_float_thresholds_are_rejected():
    with pytest.raises(TypeError, match="must be Decimal"):
        SlaRule(
            code=SlaCode.TASK_OVERDUE,
            metric=SlaMetric.TASK_HOURS_OVERDUE,
            comparison=Comparison.GT,
            threshold=24,  # type: ignore[arg-type]
            tier=EscalationTier.MANAGER,
            severity=AnomalySeverity.WARNING,
        )


def test_duplicate_rule_codes_are_rejected():
    rule = DEFAULT_RULES[0]
    with pytest.raises(ValueError, match="duplicate SLA rule code"):
        validate_rules((rule, rule))


def test_every_shipped_rule_explains_itself_without_running_anything():
    for rule in DEFAULT_RULES:
        text = rule.explain()
        assert rule.code.value in text
        assert str(rule.threshold) in text
        assert rule.rationale in text
