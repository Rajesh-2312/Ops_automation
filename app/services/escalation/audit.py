"""Audit rows for escalations. CLAUDE.md §11: "Every state transition writes an
`AuditEvent`: actor, action, before, after, at."

Raising an escalation IS a state transition — a subject acquires an owner one
rung up the §4 chain, and the question "why did this land on me, and when" has to
be answerable months later without re-running anything.

WHICH WRITE PATH — `write()`, NOT `write_within()`
--------------------------------------------------
`app/core/audit.py` states the rule: "if losing the audit row would leave a money
or approval decision unattributable, use `write_within()`. Everything else may
use `write()`."

An escalation is neither. It moves no money (R2), approves nothing and releases
nothing (R3, R4) — it puts a task in front of a human, who then acts through the
endpoints that do use `write_within()`. Making the audit insert atomic with the
escalation would mean a transient audit failure suppresses the escalation itself,
which trades a missing log line for a delivery problem nobody hears about. So:
raise the escalation, write the audit row best-effort, and alert on
`audit_persist_failed`.

This changes the day a payout release is ever driven from an escalation. It is
not today, and if it becomes true this docstring is the thing to revisit, not
work around.

This module is the only place in the package that imports outside `app.domain`,
and it holds no rule logic — `rules.py` and `engine.py` stay pure so the
determinism argument in §8 is about code with no dependencies to reason around.
"""

from __future__ import annotations

from uuid import UUID

from app.core.audit import AuditEvent
from app.domain.enums import INTERNAL_PERSONAS, Persona
from app.services.escalation.engine import Escalation
from app.services.escalation.targets import EscalationRecipients

__all__ = ["ESCALATION_RAISED", "escalation_event", "recipients_event"]

#: Audit action vocabulary for this service. `audit_events.action` is `text` by
#: design (migration 1300), so a workstream names its own actions without editing
#: the shared enum — see the `AuditAction` docstring in `app/core/audit.py`.
ESCALATION_RAISED: str = "escalation.raised"
ESCALATION_ROUTED: str = "escalation.routed"

#: The table an escalation is recorded against. Named as a constant because it is
#: the grep handle tying these events to whatever table the binding workstream
#: creates; §11 wants the table name on the row "so a row can be found again
#: without guessing which id is meant".
ESCALATION_TABLE: str = "escalations"


def escalation_event(escalation: Escalation) -> AuditEvent:
    """The `escalation.raised` row.

    `actor_id` is None and `actor_persona` is None: the Escalation Engine is a
    scheduled deterministic evaluation with no human behind it, and `audit.py`
    documents NULL actor as exactly that case. Attributing it to whoever happened
    to trigger the run would be a fiction on an audit row.

    `before` is None because the escalation did not exist a moment ago. `after`
    is the rule, the measured value and the threshold — enough to justify the
    escalation from the row alone, which is §8's "a reviewer must be able to read
    why something escalated without running a model".
    """
    return AuditEvent(
        actor_id=None,
        actor_persona=None,
        action=ESCALATION_RAISED,
        entity_table=ESCALATION_TABLE,
        entity_id=escalation.subject_id,
        before=None,
        after=dict(escalation.audit_payload()),
        at=escalation.at,
    )


def recipients_event(recipients: EscalationRecipients, entity_id: UUID | None = None) -> AuditEvent:
    """The `escalation.routed` row — who it landed on, and whether it climbed.

    Separate from `escalation.raised` for the same reason R4 keeps approval and
    release as separate audit rows: raising and routing are different decisions
    with different failure modes, and an unstaffed rung that silently climbed to
    a Senior Manager is a fact about the org chart, not about the program.
    """
    _assert_internal(recipients.persona)
    return AuditEvent(
        actor_id=None,
        actor_persona=None,
        action=ESCALATION_ROUTED,
        entity_table=ESCALATION_TABLE,
        entity_id=entity_id or recipients.escalation.subject_id,
        before={"tier": recipients.requested_tier.value},
        after={
            "tier": recipients.resolved_tier.value,
            "persona": recipients.persona.value,
            "recipient_count": str(len(recipients.user_ids)),
            "climbed": str(recipients.requested_tier is not recipients.resolved_tier),
            "unrouted": str(recipients.is_unrouted),
        },
        at=recipients.escalation.at,
    )


def _assert_internal(persona: Persona) -> None:
    """Belt to `targets.py`'s braces.

    Recipient personas can only come from `TIER_PERSONA`, so there is no current
    path by which an external persona reaches an audit row. This check makes that
    invariant fail loudly at the last point before it is written down, rather
    than resting on the reader having followed the call chain back to §4.
    """
    if persona not in INTERNAL_PERSONAS:  # pragma: no cover - unreachable via TIER_PERSONA
        raise ValueError(
            f"escalation routed to external persona {persona.value} — the Escalation "
            "Engine is internal only (CLAUDE.md §8)"
        )
