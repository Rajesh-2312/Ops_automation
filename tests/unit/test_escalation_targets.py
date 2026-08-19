"""Escalation routing and audit rows. CLAUDE.md §4, §8, §11.

Reach is not persona (§4): which humans receive an escalation comes from the
assignment tables, resolved behind the `ReachDirectory` Protocol. These tests use
a fake directory — no schema is invented here, and the binding workstream can
attach the real `SECURITY DEFINER` helpers without touching this package.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from app.domain.enums import Persona, ProgramType
from app.domain.risk import (
    AnomalySeverity,
    Comparison,
    EscalationTier,
    SlaCode,
    SlaMetric,
)
from app.services.escalation.audit import (
    ESCALATION_RAISED,
    escalation_event,
    recipients_event,
)
from app.services.escalation.engine import Escalation
from app.services.escalation.targets import ReachDirectory, resolve_recipients

SUBJECT_ID = UUID("33333333-3333-3333-3333-333333333333")
PROGRAM_ID = UUID("11111111-1111-1111-1111-111111111111")
COLLEGE_ID = UUID("22222222-2222-2222-2222-222222222222")
LDE_USER = UUID("44444444-4444-4444-4444-444444444444")
MANAGER_USER = UUID("55555555-5555-5555-5555-555555555555")
SENIOR_USER = UUID("66666666-6666-6666-6666-666666666666")
AS_OF = datetime(2026, 7, 15, 6, 0, tzinfo=UTC)


class FakeDirectory:
    """Stands in for the assignment tables. Deterministic by construction."""

    def __init__(self, staffing: dict[EscalationTier, tuple[UUID, ...]]) -> None:
        self._staffing = staffing
        self.calls: list[EscalationTier] = []

    async def users_at_tier(self, tier: EscalationTier, college_id: UUID) -> tuple[UUID, ...]:
        self.calls.append(tier)
        return self._staffing.get(tier, ())


def _escalation(tier: EscalationTier = EscalationTier.LDE_EXECUTIVE) -> Escalation:
    return Escalation(
        code=SlaCode.ATTENDANCE_UNMARKED_CRT,
        metric=SlaMetric.ATTENDANCE_UNMARKED_DAYS,
        comparison=Comparison.GTE,
        measured=Decimal(2),
        threshold=Decimal(1),
        tier=tier,
        severity=AnomalySeverity.CRITICAL,
        subject_table="attendance_days",
        subject_id=SUBJECT_ID,
        program_id=PROGRAM_ID,
        college_id=COLLEGE_ID,
        at=AS_OF,
        reason="attendance_unmarked_crt [CRT]: attendance_unmarked_days has reached 1.",
    )


def test_fake_satisfies_the_protocol():
    """The Protocol is narrow enough that a test double is four lines."""
    assert isinstance(FakeDirectory({}), ReachDirectory)


async def test_resolves_to_the_staffed_rung_the_rule_asked_for():
    directory = FakeDirectory({EscalationTier.LDE_EXECUTIVE: (LDE_USER,)})
    recipients = await resolve_recipients(_escalation(), directory)

    assert recipients.resolved_tier is EscalationTier.LDE_EXECUTIVE
    assert recipients.persona is Persona.LDE_EXECUTIVE
    assert recipients.user_ids == (LDE_USER,)
    assert recipients.is_internal
    assert not recipients.is_unrouted


async def test_unstaffed_rung_climbs_rather_than_dropping():
    """A signal that lands on nobody is a signal that was never raised."""
    directory = FakeDirectory({EscalationTier.MANAGER: (MANAGER_USER,)})
    recipients = await resolve_recipients(_escalation(), directory)

    assert recipients.requested_tier is EscalationTier.LDE_EXECUTIVE
    assert recipients.resolved_tier is EscalationTier.MANAGER
    assert recipients.persona is Persona.MANAGER
    assert directory.calls == [EscalationTier.LDE_EXECUTIVE, EscalationTier.MANAGER]


async def test_a_college_with_no_internal_reach_is_reported_not_raised():
    recipients = await resolve_recipients(_escalation(), FakeDirectory({}))

    assert recipients.is_unrouted
    assert recipients.user_ids == ()
    assert recipients.is_internal


async def test_routing_never_reaches_an_external_persona():
    """§8: internal only. Tiers map to internal personas and nothing else."""
    directory = FakeDirectory({EscalationTier.SENIOR_MANAGER: (SENIOR_USER,)})
    recipients = await resolve_recipients(_escalation(EscalationTier.MANAGER), directory)

    assert recipients.persona not in {Persona.TRAINER, Persona.COLLEGE}
    assert recipients.is_internal


async def test_routing_is_deterministic():
    directory = FakeDirectory({EscalationTier.MANAGER: (MANAGER_USER, SENIOR_USER)})
    first = await resolve_recipients(_escalation(), directory)
    second = await resolve_recipients(_escalation(), directory)
    assert first == second


# --- audit (§11) -------------------------------------------------------------


def test_raised_event_carries_the_rule_and_the_numbers():
    event = escalation_event(_escalation())

    assert event.action == ESCALATION_RAISED
    assert event.actor_id is None  # scheduled evaluation, no human behind it
    assert event.before is None
    assert event.at == AS_OF
    assert event.after is not None
    assert event.after["sla_code"] == SlaCode.ATTENDANCE_UNMARKED_CRT.value
    assert event.after["measured"] == "2"
    assert event.after["threshold"] == "1"


async def test_routed_event_records_a_climb():
    directory = FakeDirectory({EscalationTier.MANAGER: (MANAGER_USER,)})
    recipients = await resolve_recipients(_escalation(), directory)
    event = recipients_event(recipients)

    assert event.before == {"tier": EscalationTier.LDE_EXECUTIVE.value}
    assert event.after is not None
    assert event.after["tier"] == EscalationTier.MANAGER.value
    assert event.after["climbed"] == "True"
    assert event.after["recipient_count"] == "1"


def test_program_type_is_not_guessed_by_the_audit_row():
    """R1: an audit row asserts only what it was given."""
    event = escalation_event(_escalation())
    assert ProgramType.CRT.value not in (event.after or {}).get("subject_table", "")
