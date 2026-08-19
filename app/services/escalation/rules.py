"""The SLA rule table. CLAUDE.md §8:

    "**Escalation Engine — deterministic SLA rules. Not LLM judgement.**"

That one line is this package's entire design constraint, and it is taken
literally: a rule is a **row of data**, not a piece of code.

    SlaRule(code, metric, comparison, threshold, tier, severity, program_type)

Every field is an enum, a `Decimal` or `None`. There is no predicate, no lambda,
no callback and no free-text expression anywhere in a rule, and `__post_init__`
rejects a callable in any field. The consequences are the point:

* **A reviewer can read why something escalated without running anything.** The
  rule table below IS the explanation. `explain()` renders one row into a
  sentence, and that sentence is derived from the row, not written about it.
* **The evaluator has nothing to interpret.** `engine.evaluate_sla()` is a loop
  over comparisons of a number to a number. There is no place in it where a
  model could be consulted, because there is no question left open.
* **Adding an LLM here is structurally obvious.** A model call cannot be
  expressed as a `SlaRule`; it would have to arrive as a new code path in the
  evaluator, next to a docstring saying it must not, and past a test that scans
  this package's source for exactly that (`tests/unit/test_escalation_purity`).

WHY NOT A PREDICATE FUNCTION PER RULE
-------------------------------------
`app/services/remuneration/validators.py` is one function per gate, and this
package deliberately does NOT copy that shape. A payout gate has to look at a
work order, a date range and a regex — heterogeneous inputs that only code can
express. An SLA is always the same shape: this measured quantity, against this
threshold, for this long. Written as functions, the rule table would stop being
readable as a table, would stop being diffable as policy, and would give an LLM
call somewhere to hide. Written as data, none of that is possible.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from decimal import Decimal
from typing import Final

from app.domain.enums import ProgramType
from app.domain.risk import (
    AnomalySeverity,
    Comparison,
    EscalationTier,
    SlaCode,
    SlaMetric,
)

__all__ = ["DEFAULT_RULES", "SlaRule", "validate_rules"]

#: How a `Comparison` reads in an explanation. Rendering table, not logic — the
#: comparison itself is performed in `engine._compare`.
_COMPARISON_WORDS: Final[dict[Comparison, str]] = {
    Comparison.GT: "is above",
    Comparison.GTE: "has reached",
    Comparison.LT: "is below",
    Comparison.LTE: "is at or below",
}


@dataclass(frozen=True, slots=True)
class SlaRule:
    """One deterministic SLA rule. Data only — see the module docstring.

    `tier=None` means "one rung above whoever currently owns this", resolved
    against the subject's tier at evaluation time. That is how the §4 ladder
    (Senior Manager -> Manager -> LDE Executive) is climbed without writing a
    separate rule per starting rung — still data, still deterministic, and still
    readable in the table.
    """

    code: SlaCode
    metric: SlaMetric
    comparison: Comparison
    threshold: Decimal
    #: Absolute tier this fires at, or `None` to climb one rung from current.
    tier: EscalationTier | None
    severity: AnomalySeverity
    #: Restrict the rule to one program type. `None` applies to both. CLAUDE.md
    #: §5 makes this essential for anything touching attendance.
    program_type: ProgramType | None = None
    #: Why the rule exists, in one line, for the person reading the escalation.
    rationale: str = ""

    def __post_init__(self) -> None:
        """Reject anything that is not data.

        A callable in a rule field is the exact shape a "just call the model to
        decide this one" change would take. Refusing it at construction makes
        that change fail at import, not in review — §8 says deterministic rules,
        and a rule that can run arbitrary code is not one.
        """
        for spec in fields(self):
            value = getattr(self, spec.name)
            if callable(value):
                raise TypeError(
                    f"SlaRule.{spec.name} holds a callable — SLA rules are data, evaluated "
                    "deterministically (CLAUDE.md §8: 'deterministic SLA rules. Not LLM "
                    "judgement.'). Express the condition as metric/comparison/threshold."
                )
        if not isinstance(self.threshold, Decimal):
            raise TypeError(
                f"SlaRule.{self.code.value} threshold must be Decimal, got "
                f"{type(self.threshold).__name__} — a float threshold makes the boundary "
                "case non-deterministic, and a rule that fires 'sometimes' at the boundary "
                "is a rule nobody trusts."
            )

    def explain(self) -> str:
        """One sentence, derived from the row itself.

        Deliberately generated from the fields rather than stored as prose: a
        hand-written message can drift from the threshold it describes, and an
        escalation whose explanation disagrees with its rule is worse than one
        with no explanation.
        """
        scope = f" [{self.program_type.value}]" if self.program_type else ""
        tail = f" {self.rationale}" if self.rationale else ""
        return (
            f"{self.code.value}{scope}: {self.metric.value} "
            f"{_COMPARISON_WORDS[self.comparison]} {self.threshold}.{tail}"
        )


def validate_rules(rules: tuple[SlaRule, ...]) -> tuple[SlaRule, ...]:
    """Reject a rule table that cannot be evaluated deterministically.

    Duplicate codes are the failure this catches in practice: two rows with the
    same `SlaCode` produce two escalations that acknowledgement cannot tell
    apart, so acknowledging one silently clears the other.
    """
    seen: set[SlaCode] = set()
    for rule in rules:
        if rule.code in seen:
            raise ValueError(
                f"duplicate SLA rule code {rule.code.value} — the code is what an "
                "escalation record and its acknowledgement key off, so two rows sharing "
                "one code make acknowledgement ambiguous"
            )
        seen.add(rule.code)
    return rules


#: The shipped rule table. Read top to bottom, this is the escalation policy.
#:
#: Thresholds are hours except where the metric says otherwise. They are
#: deliberately generous: an escalation engine tuned tight produces a queue
#: nobody works, and the §8 ladder only means something if arriving at the top
#: is rare.
DEFAULT_RULES: Final[tuple[SlaRule, ...]] = validate_rules(
    (
        # --- Tasks and documents -------------------------------------------
        SlaRule(
            code=SlaCode.TASK_OVERDUE,
            metric=SlaMetric.TASK_HOURS_OVERDUE,
            comparison=Comparison.GT,
            threshold=Decimal(24),
            tier=EscalationTier.LDE_EXECUTIVE,
            severity=AnomalySeverity.WARNING,
            rationale="A day past due is the campus owner's to close before anyone above.",
        ),
        SlaRule(
            code=SlaCode.TASK_SEVERELY_OVERDUE,
            metric=SlaMetric.TASK_HOURS_OVERDUE,
            comparison=Comparison.GT,
            threshold=Decimal(72),
            tier=EscalationTier.MANAGER,
            severity=AnomalySeverity.CRITICAL,
            rationale="Three days means the campus owner is blocked, not slow.",
        ),
        SlaRule(
            code=SlaCode.DOCUMENT_UNSIGNED,
            metric=SlaMetric.DOCUMENT_HOURS_UNSIGNED,
            comparison=Comparison.GT,
            threshold=Decimal(120),
            tier=EscalationTier.MANAGER,
            severity=AnomalySeverity.WARNING,
            rationale=(
                "An unsigned work order blocks the §7 payout gate at month end, when "
                "there is no longer time to chase it."
            ),
        ),
        # --- Attendance: the CLAUDE.md §5 branch ----------------------------
        # Two rules, one metric, opposite thresholds. This asymmetry is the same
        # one the payout validators encode as BLOCKING-for-CRT /
        # WARNING-for-bCAP, and it is why `program_type` is a rule field.
        SlaRule(
            code=SlaCode.ATTENDANCE_UNMARKED_CRT,
            metric=SlaMetric.ATTENDANCE_UNMARKED_DAYS,
            comparison=Comparison.GTE,
            threshold=Decimal(1),
            tier=EscalationTier.LDE_EXECUTIVE,
            severity=AnomalySeverity.CRITICAL,
            program_type=ProgramType.CRT,
            rationale=(
                "CRT counts payable days UP from P marks: one unmarked day is already an "
                "underpayment in flight, and it blocks the payout cycle (§7)."
            ),
        ),
        SlaRule(
            code=SlaCode.ATTENDANCE_UNMARKED_BCAP,
            metric=SlaMetric.ATTENDANCE_UNMARKED_DAYS,
            comparison=Comparison.GTE,
            threshold=Decimal(3),
            tier=EscalationTier.LDE_EXECUTIVE,
            severity=AnomalySeverity.WARNING,
            program_type=ProgramType.BCAP,
            rationale=(
                "bCAP counts DOWN from period length: an unmarked day is paid, so the "
                "exposure accumulates rather than blocking (§7 keeps it a warning)."
            ),
        ),
        SlaRule(
            code=SlaCode.ATTENDANCE_MARKING_STALE,
            metric=SlaMetric.ATTENDANCE_HOURS_SINCE_MARK,
            comparison=Comparison.GT,
            threshold=Decimal(72),
            tier=EscalationTier.MANAGER,
            severity=AnomalySeverity.WARNING,
            rationale="Marking has stopped, not slipped — the campus owner needs cover.",
        ),
        # --- Delivery risk, from the Monitor --------------------------------
        SlaRule(
            code=SlaCode.DELIVERY_RISK_HIGH,
            metric=SlaMetric.DELIVERY_RISK_SCORE,
            comparison=Comparison.GTE,
            threshold=Decimal(8),
            tier=EscalationTier.MANAGER,
            severity=AnomalySeverity.WARNING,
            rationale="RiskBand.HIGH floor. See app.domain.risk.RISK_BAND_FLOOR.",
        ),
        SlaRule(
            code=SlaCode.DELIVERY_RISK_CRITICAL,
            metric=SlaMetric.DELIVERY_RISK_SCORE,
            comparison=Comparison.GTE,
            threshold=Decimal(16),
            tier=EscalationTier.SENIOR_MANAGER,
            severity=AnomalySeverity.CRITICAL,
            rationale="RiskBand.CRITICAL floor: failing on more than one axis at once.",
        ),
        # --- Money -----------------------------------------------------------
        SlaRule(
            code=SlaCode.PAYOUT_BLOCKED,
            metric=SlaMetric.PAYOUT_HOURS_BLOCKED,
            comparison=Comparison.GT,
            threshold=Decimal(48),
            tier=EscalationTier.MANAGER,
            severity=AnomalySeverity.CRITICAL,
            rationale=(
                "A blocked payout run is a trainer who will not be paid on time. The "
                "engine escalates it; it never resolves it (R2, R3)."
            ),
        ),
        # --- The ladder ------------------------------------------------------
        SlaRule(
            code=SlaCode.ESCALATION_UNACKNOWLEDGED,
            metric=SlaMetric.ESCALATION_HOURS_UNACKNOWLEDGED,
            comparison=Comparison.GT,
            threshold=Decimal(24),
            tier=None,  # one rung above the current owner — §4's chain
            severity=AnomalySeverity.WARNING,
            rationale=(
                "An unacknowledged escalation climbs. Saturates at Senior Manager: "
                "above that there is no internal recipient this platform models."
            ),
        ),
    )
)
