"""The ERM job card's lifecycle — CLAUDE.md §10, "assigns it to a named person,
they paste, they confirm".

Four things are pinned:

* **The edges, in both directions.** What is legal from an open card, and what is
  refused on a card that is already history.
* **Confirm requires a human.** The table's whole evidential content is that a
  NAMED person says they pasted a NAMED pack on a NAMED day. A confirmation with
  no actor would be a fabricated record of a manual act.
* **Every transition writes §11's five fields.** actor, action, before, after,
  at — and `before` is a real snapshot, which is only possible because the state
  machine is pure and returns a new card rather than mutating one.
* **Nothing in this package can send.** Asserted structurally rather than by
  reading the code: no transport import, no state that means "sent", no
  transition into STALE (that is the database's).
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from app.domain.enums import Persona
from app.services.approval import Actor
from app.services.erm import (
    ErmHumanActorRequiredError,
    ErmIllegalTransitionError,
    ErmSubjectKind,
    ErmSyncAction,
    ErmSyncState,
    SyncTask,
    TrainerFacts,
    assign_task,
    build_trainer_pack,
    cancel_task,
    confirm_task,
    queue_task,
)
from app.services.erm import lifecycle as lifecycle_module

TASK_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
TRAINER_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
USER_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
ASSIGNEE_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")

NOW = dt.datetime(2026, 8, 18, 9, 0, tzinfo=dt.UTC)
LATER = NOW + dt.timedelta(hours=2)

MANAGER = Actor(actor_id=USER_ID, persona=Persona.MANAGER)
ROBOT = Actor(actor_id=None, persona=Persona.MANAGER)

PACK = build_trainer_pack(
    TrainerFacts(
        full_name="VEMA PRUDHVI SAI",
        pan="BCDPV1234K",
        email="vema@example.com",
        phone="9876543210",
        trainer_type="freelancer",
        work_order_status="signed",
        zoho_id="ZH-1",
        colleges=("ABC Engineering College",),
    )
)


def queued() -> SyncTask:
    return SyncTask(
        task_id=TASK_ID,
        subject_kind=ErmSubjectKind.TRAINER,
        subject_id=TRAINER_ID,
        state=ErmSyncState.QUEUED,
        field_order_version=1,
    )


def assigned() -> SyncTask:
    return assign_task(queued(), MANAGER, ASSIGNEE_ID, NOW).task


def confirmed() -> SyncTask:
    return confirm_task(assigned(), MANAGER, PACK, LATER).task


# --- the happy path ----------------------------------------------------------------


def test_queue_records_the_card_without_requiring_a_human() -> None:
    """Filing work for a person to do is the least consequential act here.

    §8 puts the Onboarding agent's ERM checklist work at "Auto (internal only)",
    so a scheduled job noticing an unpushed trainer and queueing the card is the
    intended behaviour. The human requirement lands on confirm, where a claim
    about the world gets made.
    """
    outcome = queue_task(queued(), ROBOT, NOW)

    assert outcome.task.state is ErmSyncState.QUEUED
    assert outcome.event.action == ErmSyncAction.QUEUED.value
    assert outcome.event.actor_id is None


def test_assign_names_the_person() -> None:
    outcome = assign_task(queued(), MANAGER, ASSIGNEE_ID, NOW)

    assert outcome.task.state is ErmSyncState.ASSIGNED
    assert outcome.task.assigned_to == ASSIGNEE_ID
    assert outcome.task.assigned_at == NOW
    assert outcome.event.before is not None
    assert outcome.event.before["state"] == ErmSyncState.QUEUED.value
    assert outcome.event.after is not None
    assert outcome.event.after["assigned_to"] == str(ASSIGNEE_ID)


def test_reassignment_is_normal_and_keeps_one_row() -> None:
    """People go on leave. Forcing a cancel-and-refile would fork the history of
    one push across two rows and lose the link between them."""
    other = uuid.UUID("55555555-5555-5555-5555-555555555555")
    outcome = assign_task(assigned(), MANAGER, other, LATER)

    assert outcome.task.task_id == TASK_ID
    assert outcome.task.assigned_to == other


def test_confirm_freezes_the_pack_onto_the_audit_row() -> None:
    """§11's "before, after". The thing in dispute six months from now is what ERM
    was told, not which enum label the card held."""
    outcome = confirm_task(assigned(), MANAGER, PACK, LATER)

    assert outcome.task.state is ErmSyncState.CONFIRMED
    assert outcome.task.confirmed_by == USER_ID
    assert outcome.task.confirmed_at == LATER
    assert outcome.event.after is not None
    recorded = outcome.event.after["field_pack"]
    assert isinstance(recorded, list)
    assert [entry["label"] for entry in recorded] == [entry.label for entry in PACK.entries]
    assert outcome.event.after["field_order_verified"] is False


def test_confirm_is_legal_straight_from_queued() -> None:
    """A Manager who did the push themselves should not have to assign it to
    themselves first. A workflow that makes honesty inconvenient gets worked
    around rather than followed."""
    assert confirm_task(queued(), MANAGER, PACK, NOW).task.state is ErmSyncState.CONFIRMED


def test_cancel_requires_a_reason() -> None:
    """A cancelled push is a decision that a record deliberately does not match
    ERM — the divergence §10 is about. Unexplained, it reads as an oversight."""
    with pytest.raises(ValueError, match="requires a reason"):
        cancel_task(queued(), MANAGER, "   ", NOW)

    outcome = cancel_task(queued(), MANAGER, "Trainer withdrew before onboarding", NOW)
    assert outcome.task.state is ErmSyncState.CANCELLED
    assert outcome.event.after is not None
    assert outcome.event.after["reason"] == "Trainer withdrew before onboarding"


# --- the refusals ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "transition",
    [
        lambda task: assign_task(task, MANAGER, ASSIGNEE_ID, LATER),
        lambda task: confirm_task(task, MANAGER, PACK, LATER),
        lambda task: cancel_task(task, MANAGER, "changed my mind", LATER),
    ],
)
def test_a_confirmed_card_is_history(transition) -> None:  # noqa: ANN001
    """It happened: somebody typed those values into a system this one cannot
    reach. Undoing it means going back to ERM, which is a new card."""
    with pytest.raises(ErmIllegalTransitionError):
        transition(confirmed())


def test_a_cancelled_card_cannot_be_revived() -> None:
    cancelled = cancel_task(queued(), MANAGER, "duplicate", NOW).task
    with pytest.raises(ErmIllegalTransitionError):
        assign_task(cancelled, MANAGER, ASSIGNEE_ID, LATER)


def test_confirmation_requires_an_authenticated_human() -> None:
    """See the module docstring. This is the second lock — R3 already keeps agents
    out — and it stays because a guard that is unreachable today keeps working
    when somebody adds a service principal tomorrow."""
    with pytest.raises(ErmHumanActorRequiredError):
        confirm_task(queued(), ROBOT, PACK, NOW)


def test_an_assigned_card_must_name_its_assignee() -> None:
    """1900 says so as a CHECK; `SyncTask` says so as an invariant, so a card
    built in a test obeys the same rule as one loaded from Postgres."""
    with pytest.raises(ValueError, match="must name the person"):
        SyncTask(
            task_id=TASK_ID,
            subject_kind=ErmSubjectKind.TRAINER,
            subject_id=TRAINER_ID,
            state=ErmSyncState.ASSIGNED,
            field_order_version=1,
        )


def test_confirmed_and_stale_both_carry_the_confirming_human() -> None:
    """STALE does not mean "failed": it means this push happened correctly and the
    record has since moved. The evidence survives."""
    with pytest.raises(ValueError, match="pasted this once"):
        SyncTask(
            task_id=TASK_ID,
            subject_kind=ErmSubjectKind.TRAINER,
            subject_id=TRAINER_ID,
            state=ErmSyncState.STALE,
            field_order_version=1,
        )


# --- R3 / §10, structurally --------------------------------------------------------------


def test_the_lifecycle_offers_no_transition_into_stale() -> None:
    """Staleness has one author and it is `1900_erm_sync.sql`.

    A Python transition would be a second detector that only sees writes going
    through this application, and §3 says most do not.
    """
    assert not [name for name in lifecycle_module.__all__ if "stale" in name.lower()]


def test_nothing_in_the_package_imports_a_transport(repo_root) -> None:  # noqa: ANN001
    """§10 forbids a scraper; R3 forbids a send capability anywhere near an agent.

    Both hold here structurally: the code that would do the sending does not
    exist, and this test fails the build if somebody adds it.
    """
    banned = (
        "import httpx",
        "import requests",
        "smtplib",
        "selenium",
        "playwright",
        "twilio",
        "sendgrid",
        "aiohttp",
        "urllib.request",
    )
    for path in (repo_root / "app" / "services" / "erm").glob("*.py"):
        source = path.read_text(encoding="utf-8")
        for token in banned:
            assert token not in source, f"{path.name} imports {token} — ERM has no API (§10)"


def test_no_action_in_the_vocabulary_claims_a_transmission() -> None:
    """ "Confirmed" is the strongest true statement available about a portal this
    system cannot reach. "Sent", "pushed" or "released" would be claims nobody
    can support."""
    for action in ErmSyncAction:
        assert not any(
            word in action.value for word in ("sent", "send", "push", "release", "deliver")
        )
