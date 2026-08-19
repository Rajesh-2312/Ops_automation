"""The deterministic evaluator. CLAUDE.md §8: "deterministic SLA rules. Not LLM
judgement."

Everything this module does is: look a metric up in a mapping the caller
measured, compare it to a `Decimal` threshold with an enum-selected operator,
and, when it fires, record which rule fired and with what numbers. There is no
branch in here that asks a question the rule table did not already answer.

DETERMINISM, CONCRETELY
-----------------------
1. **No clock.** `SlaFacts.as_of` is passed in. Nothing calls `datetime.now()`,
   so a run is reproducible from its inputs, and §12's "same input twice gives an
   identical result" is assertable rather than aspirational.
2. **No I/O.** Metrics arrive pre-measured; persistence sits behind a Protocol
   (`app.services.escalation.targets`) that this module never calls.
3. **No iteration-order dependence.** Rules are evaluated in table order and the
   result preserves it.
4. **A missing metric never fires.** An absent key means "not measured", not
   "zero" — defaulting to zero would make `LT` rules fire on every subject whose
   metric was never collected, which is how an escalation queue fills with noise
   and stops being read.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
It does not notify, queue, mark, or resolve anything. R3: this is a shared
service, not an agent, and its output is a value the caller persists and a human
works. Escalation targets are internal (§4) and `targets.py` is where that is
enforced.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Final
from uuid import UUID

from app.domain.enums import ProgramType
from app.domain.risk import (
    AnomalySeverity,
    Comparison,
    EscalationTier,
    SlaCode,
    SlaMetric,
    next_tier,
)
from app.services.escalation.rules import DEFAULT_RULES, SlaRule

__all__ = ["Escalation", "EscalationDecision", "SlaFacts", "evaluate_sla"]


@dataclass(frozen=True, slots=True)
class SlaFacts:
    """Everything the rule table can refer to, already measured.

    The metric mapping is the whole interface: a rule may only be written against
    a `SlaMetric`, and a `SlaMetric` may only be filled in here by the caller
    reading a system of record. R1 end to end — the engine cannot assert a fact
    it was not given, because it has no way to obtain one.

    `current_tier` is who owns the subject right now. It is what `tier=None`
    rules climb from, and it comes from the existing escalation record rather
    than from a persona: §4 is explicit that persona is not reach.
    """

    subject_table: str
    subject_id: UUID
    program_id: UUID
    college_id: UUID
    program_type: ProgramType

    #: Observation instant, UTC (§11). Presentation converts to IST; nothing here
    #: does, because a timezone conversion inside a comparison is a bug waiting.
    as_of: datetime

    metrics: Mapping[SlaMetric, Decimal] = field(default_factory=dict)
    current_tier: EscalationTier = EscalationTier.LDE_EXECUTIVE


@dataclass(frozen=True, slots=True)
class Escalation:
    """One rule that fired, with the numbers that made it fire.

    `measured` and `threshold` are both on the record so the escalation can be
    justified from the row alone — no re-running the engine, no re-querying, and
    no model needed to say why this landed on someone's desk.
    """

    code: SlaCode
    metric: SlaMetric
    comparison: Comparison
    measured: Decimal
    threshold: Decimal
    tier: EscalationTier
    severity: AnomalySeverity
    subject_table: str
    subject_id: UUID
    program_id: UUID
    college_id: UUID
    at: datetime
    reason: str

    def audit_payload(self) -> dict[str, str]:
        """The `after` snapshot for the §11 audit row. Strings only, so the value
        serialises to JSON unchanged and reads the same in a dispute as it did on
        the day."""
        return {
            "sla_code": self.code.value,
            "metric": self.metric.value,
            "comparison": self.comparison.value,
            "measured": str(self.measured),
            "threshold": str(self.threshold),
            "tier": self.tier.value,
            "severity": self.severity.value,
            "subject_table": self.subject_table,
            "subject_id": str(self.subject_id),
            "at": self.at.isoformat(),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class EscalationDecision:
    """Everything one evaluation produced, in rule-table order."""

    facts: SlaFacts
    escalations: tuple[Escalation, ...]

    @property
    def fired(self) -> bool:
        return bool(self.escalations)

    @property
    def highest_tier(self) -> EscalationTier | None:
        """The most senior rung anything reached. `None` when nothing fired."""
        if not self.escalations:
            return None
        return max(self.escalations, key=lambda e: _TIER_RANK[e.tier]).tier

    def codes(self) -> tuple[SlaCode, ...]:
        return tuple(e.code for e in self.escalations)


_TIER_RANK: Final[dict[EscalationTier, int]] = {
    EscalationTier.LDE_EXECUTIVE: 0,
    EscalationTier.MANAGER: 1,
    EscalationTier.SENIOR_MANAGER: 2,
}


def _compare(measured: Decimal, comparison: Comparison, threshold: Decimal) -> bool:
    """The only comparison in the package.

    A mapping of enum to operator, exhaustive over `Comparison`. Written out
    rather than pulled from `operator` by name so that adding a comparison is a
    visible edit here, and so no string ever selects an operator.
    """
    if comparison is Comparison.GT:
        return measured > threshold
    if comparison is Comparison.GTE:
        return measured >= threshold
    if comparison is Comparison.LT:
        return measured < threshold
    return measured <= threshold


def _resolve_tier(rule: SlaRule, facts: SlaFacts) -> EscalationTier:
    """The rung this firing lands on. `tier=None` climbs one from current (§4)."""
    if rule.tier is not None:
        return rule.tier
    return next_tier(facts.current_tier)


def evaluate_sla(facts: SlaFacts, rules: Sequence[SlaRule] | None = None) -> EscalationDecision:
    """Evaluate the rule table against measured facts. Pure and total.

    Every rule is evaluated; there is no short-circuit, for the same reason the
    §7 payout gates have none — whoever picks this up wants the full picture
    once, not one item per round trip.

    Rules whose `program_type` does not match are skipped rather than evaluated
    and discarded, because CLAUDE.md §5's two attendance rules share one metric
    and firing both would report a CRT consequence on a bCAP program.
    """
    selected = DEFAULT_RULES if rules is None else rules
    out: list[Escalation] = []
    for rule in selected:
        if rule.program_type is not None and rule.program_type is not facts.program_type:
            continue
        measured = facts.metrics.get(rule.metric)
        if measured is None:
            # Not measured is not zero. See the module docstring.
            continue
        if not _compare(measured, rule.comparison, rule.threshold):
            continue
        tier = _resolve_tier(rule, facts)
        out.append(
            Escalation(
                code=rule.code,
                metric=rule.metric,
                comparison=rule.comparison,
                measured=measured,
                threshold=rule.threshold,
                tier=tier,
                severity=rule.severity,
                subject_table=facts.subject_table,
                subject_id=facts.subject_id,
                program_id=facts.program_id,
                college_id=facts.college_id,
                at=facts.as_of,
                reason=f"{rule.explain()} Measured {measured}.",
            )
        )
    return EscalationDecision(facts=facts, escalations=tuple(out))
