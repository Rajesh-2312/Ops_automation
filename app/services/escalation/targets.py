"""Who an escalation lands on. CLAUDE.md §4 — and INTERNAL only.

    "Senior Manager -> Manager -> LDE Executive (on campus)"
    "Persona lives on `profiles.role`. Reach does not."

Reach comes from `user_college_assignments` and `user_cluster_assignments`, and
the database resolves it through the `SECURITY DEFINER` helpers §4 names. This
module therefore does **not** query anything: it declares the narrowest possible
Protocol for "which internal users on this rung can reach this college", and
another workstream binds it to those tables. Tests use a fake.

Two things are enforced here rather than documented:

* **The persona for a tier comes from `TIER_PERSONA`, never from a caller.** An
  escalation cannot be addressed to a TRAINER or a COLLEGE because those are not
  tiers — §8 makes both this service and the Delivery Monitor internal-only, and
  external contact belongs to the Comms Service behind human approval (R4).
* **An unstaffed rung climbs, it does not drop.** If no internal user can reach
  the college at the rule's tier, resolution moves one rung up the §4 chain and
  tries again, up to Senior Manager. A signal that lands on nobody is a signal
  that was never raised, and the whole point of an SLA is that it survives the
  person who was supposed to act being on leave.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from uuid import UUID

from app.domain.enums import INTERNAL_PERSONAS, Persona
from app.domain.risk import TIER_ORDER, TIER_PERSONA, EscalationTier
from app.services.escalation.engine import Escalation

__all__ = ["EscalationRecipients", "ReachDirectory", "resolve_recipients"]


@runtime_checkable
class ReachDirectory(Protocol):
    """The one thing this package needs from the database.

    Narrow on purpose: a Protocol wide enough to be convenient is a Protocol that
    ends up holding business rules. One method, one question — "which internal
    users at this tier can reach this college" — answered by the RLS helpers,
    never reconstructed here from personas and college ids.
    """

    async def users_at_tier(self, tier: EscalationTier, college_id: UUID) -> tuple[UUID, ...]:
        """Internal user ids on `tier` whose assignments reach `college_id`.

        Ordering must be stable across calls (order by id in SQL). An escalation
        that names its recipients in a different order on every run is not
        deterministic, and §8's promise is that it is.
        """
        ...


@dataclass(frozen=True, slots=True)
class EscalationRecipients:
    """Where one escalation actually landed.

    `requested_tier` and `resolved_tier` are both kept: when they differ, an
    unstaffed rung was climbed, and that is exactly the fact a Senior Manager
    wondering "why is this on my desk" needs to see.
    """

    escalation: Escalation
    requested_tier: EscalationTier
    resolved_tier: EscalationTier
    persona: Persona
    user_ids: tuple[UUID, ...]

    @property
    def is_internal(self) -> bool:
        return self.persona in INTERNAL_PERSONAS

    @property
    def is_unrouted(self) -> bool:
        """True when no internal user could be found on any rung.

        Not an exception: an escalation nobody can receive is itself the finding,
        and raising here would lose the other escalations in the same run.
        Callers surface it — a college with no staffed assignment is an ops gap,
        not a crash.
        """
        return not self.user_ids


async def resolve_recipients(
    escalation: Escalation, directory: ReachDirectory
) -> EscalationRecipients:
    """Resolve one escalation to internal recipients, climbing unstaffed rungs.

    Deterministic given a deterministic directory: the climb order is
    `TIER_ORDER` and stops at the first staffed rung.
    """
    start = TIER_ORDER.index(escalation.tier)
    for tier in TIER_ORDER[start:]:
        user_ids = await directory.users_at_tier(tier, escalation.college_id)
        if user_ids:
            return EscalationRecipients(
                escalation=escalation,
                requested_tier=escalation.tier,
                resolved_tier=tier,
                persona=TIER_PERSONA[tier],
                user_ids=user_ids,
            )
    return EscalationRecipients(
        escalation=escalation,
        requested_tier=escalation.tier,
        resolved_tier=EscalationTier.SENIOR_MANAGER,
        persona=TIER_PERSONA[EscalationTier.SENIOR_MANAGER],
        user_ids=(),
    )
