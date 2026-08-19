"""Escalation Engine. CLAUDE.md §8, "Shared services (not agents)":

    "**Escalation Engine — deterministic SLA rules. Not LLM judgement.**"

A shared service, not an agent: it has no graph, no tools and no autonomy level,
because it makes no judgement to be autonomous about. Rules live in `rules.py` as
data; `engine.py` compares numbers to thresholds; `targets.py` routes the result
to internal staff through a Protocol the database binds later; `audit.py` turns a
firing into the §11 audit row.

**No LLM anywhere in this package, by construction.** `rules.py` and `engine.py`
import nothing outside `app.domain`; the only outside import in the package is
`app.core.audit`, in `audit.py`. `tests/unit/test_escalation_purity.py` asserts
that statically over the package source, the way `tools/rule_linter.py` L2 does
for the remuneration engine — so an LLM call added here fails a test rather than
passing review.
"""

from app.services.escalation.audit import escalation_event, recipients_event
from app.services.escalation.engine import (
    Escalation,
    EscalationDecision,
    SlaFacts,
    evaluate_sla,
)
from app.services.escalation.rules import DEFAULT_RULES, SlaRule
from app.services.escalation.targets import (
    EscalationRecipients,
    ReachDirectory,
    resolve_recipients,
)

__all__ = [
    "DEFAULT_RULES",
    "Escalation",
    "EscalationDecision",
    "EscalationRecipients",
    "ReachDirectory",
    "SlaFacts",
    "SlaRule",
    "escalation_event",
    "evaluate_sla",
    "recipients_event",
    "resolve_recipients",
]
