"""The job card's lifecycle — CLAUDE.md §10, "assigns it to a named person, they
paste, they confirm".

Pure. Every function takes a `SyncTask` and returns a new one plus the
`AuditEvent` that describes the change (§11: "every state transition writes an
AuditEvent: actor, action, before, after, at"). Nothing here opens a connection,
reads a row or reaches ERM — `app/api/erm.py` does the persistence, in one
transaction with the audit row.

    queued ──assign──> assigned ──confirm──> confirmed ──(the database)──> stale
      │                    │                     ▲
      └──confirm───────────┴─────────────────────┘
      │                    │
      └──cancel────────────┴──> cancelled

=============================================================================
WHY THIS IS NOT THE R4 LADDER, WRITTEN DOWN ONCE SO NOBODY "FIXES" IT
=============================================================================
`app/services/approval/state_machine.py` exists and is good, and this file
deliberately does not use it. R4 governs artifacts that LEAVE: a remuneration
sheet, an invoice, a message to a college. Its ladder ends in RELEASED because
something is released.

An ERM sync releases nothing. A named member of staff retypes values byteXL
already holds into a portal byteXL already uses, and §8 puts precisely that work
— "WO / ZOHO / ERM / platform-access checklist, internal chase" — at "Auto
(internal only)". Borrowing `artifact_state` would put an approval gate in front
of internal record-keeping, and the predictable result of a gate nobody believes
in is that the state stops meaning anything on the artifacts where it does
matter.

What IS borrowed is `Actor`, from the same package, because "who did this" has
one definition in this system and should not acquire a second.

=============================================================================
CONFIRM IS A HUMAN CLAIM, AND IS RECORDED AS ONE
=============================================================================
`confirm_task()` requires `actor.actor_id`. Not as ceremony: the entire evidential
content of this table is that a *named person* says they pasted a *named pack* on
a *named day*. An agent or a cron job cannot make that claim — it did not open
ERM, it cannot have — so a confirmation with no human behind it is a false record
of a manual act, which is a worse artifact than an empty queue.

R3 is upstream of that anyway: `app/agents/tools/catalog.py` is a closed set of
read tools plus `save_draft`, and nothing in it reaches this module. The check
here is the second lock, and it stays even though the first one holds.

=============================================================================
NOTHING IN THIS FILE MARKS A TASK STALE
=============================================================================
There is no `mark_stale()` and there must not be. `drift.py`'s docstring has the
long version: the frontend writes to Supabase directly for ordinary CRUD (§3), so
a Python-side detector would miss the common edit. Staleness is set by the
triggers in `1900_erm_sync.sql`, inside the transaction that does the editing, on
every connection. A function here would be a second detector that disagrees with
the first, and the one that runs less often would be the one people read.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, replace
from uuid import UUID

from app.core.audit import AuditEvent, JsonValue
from app.services.approval import Actor
from app.services.erm.fieldpack import FieldPack
from app.services.erm.types import (
    ERM_ENTITY_TABLE,
    ErmSubjectKind,
    ErmSyncAction,
    ErmSyncState,
)

__all__ = [
    "ErmError",
    "ErmHumanActorRequiredError",
    "ErmIllegalTransitionError",
    "ErmOutcome",
    "SyncTask",
    "assign_task",
    "cancel_task",
    "confirm_task",
    "queue_task",
]


class ErmError(Exception):
    """Base for every refusal this package raises.

    One base so `app/api/erm.py` can translate the family in one place, the way
    `app/api/comms.py` translates `ApprovalError`.
    """


class ErmIllegalTransitionError(ErmError):
    """The requested move is not an edge of the graph above.

    Carries both states rather than a sentence, so a caller can log the edge and
    a test can assert on it without string matching.
    """

    def __init__(self, action: ErmSyncAction, current: ErmSyncState) -> None:
        super().__init__(
            f"'{action.value}' is not available on an ERM sync task in state "
            f"'{current.value}'. A card is assigned or confirmed only while it is "
            f"open; a confirmed, stale or cancelled card is history. A stale card "
            f"already has its replacement — the database files one in the same "
            f"transaction as the drift (CLAUDE.md §10)."
        )
        self.action = action
        self.current = current


class ErmHumanActorRequiredError(ErmError):
    """A confirmation with nobody behind it.

    See the module docstring: the table's entire evidential value is that a named
    person says they did a manual thing. Anonymous confirmation would be a
    fabricated record of a human act.
    """

    def __init__(self, action: ErmSyncAction) -> None:
        super().__init__(
            f"'{action.value}' requires an authenticated human actor: actor_id is "
            "None. CLAUDE.md §10 models the ERM integration as a person pasting a "
            "field pack — a confirmation nobody made is a false record of a manual "
            "act, not a missing detail."
        )
        self.action = action


@dataclass(frozen=True, slots=True)
class SyncTask:
    """One job card, as the lifecycle sees it.

    A projection of an `erm_sync_tasks` row, not the row: the columns the state
    machine reasons about and nothing else. The pack itself, the ERM id read
    back, the remarks and the drift bookkeeping all live on the row and are
    handled by `app/api/erm.py`, because none of them changes which transitions
    are legal.

    Frozen, and every transition returns a new instance. A state machine that
    mutates its input cannot report a `before` snapshot, and §11 requires one on
    every audit event.
    """

    task_id: UUID
    subject_kind: ErmSubjectKind
    subject_id: UUID
    state: ErmSyncState
    field_order_version: int
    assigned_to: UUID | None = None
    assigned_at: dt.datetime | None = None
    confirmed_by: UUID | None = None
    confirmed_at: dt.datetime | None = None
    cancelled_at: dt.datetime | None = None

    def __post_init__(self) -> None:
        """The invariants `1900_erm_sync.sql` states as CHECK constraints.

        Restated here so a `SyncTask` built from a hand-made dict in a test obeys
        the same rules as one loaded from Postgres. If these two ever disagree
        the SQL wins and this is the bug — the database is the source of truth
        (R1), and this is a mirror.
        """
        if (self.assigned_to is None) != (self.assigned_at is None):
            raise ValueError("assigned_to and assigned_at are set together or not at all")
        if self.state is ErmSyncState.ASSIGNED and self.assigned_to is None:
            raise ValueError("an ASSIGNED card must name the person it is assigned to (§10)")
        synced = self.state in (ErmSyncState.CONFIRMED, ErmSyncState.STALE)
        if synced != (self.confirmed_at is not None) or synced != (self.confirmed_by is not None):
            raise ValueError(
                "CONFIRMED and STALE both mean somebody pasted this once: both "
                "carry confirmed_by and confirmed_at, and no other state does"
            )
        if (self.state is ErmSyncState.CANCELLED) != (self.cancelled_at is not None):
            raise ValueError("cancelled_at is set exactly when the state is CANCELLED")

    @property
    def is_open(self) -> bool:
        """Still workable: queued or assigned."""
        return self.state in (ErmSyncState.QUEUED, ErmSyncState.ASSIGNED)


@dataclass(frozen=True, slots=True)
class ErmOutcome:
    """The result of one transition: the next card, and the row that records it.

    Both halves, always, so a caller cannot land the state change and forget the
    evidence. `app/api/erm.py` commits them in one transaction via
    `AuditWriter.write_within()`.
    """

    task: SyncTask
    event: AuditEvent


def _event(
    action: ErmSyncAction,
    actor: Actor,
    task: SyncTask,
    before: dict[str, JsonValue] | None,
    after: dict[str, JsonValue],
    at: dt.datetime,
) -> AuditEvent:
    """§11's five fields, assembled once so every transition records the same shape."""
    return AuditEvent(
        actor_id=actor.actor_id,
        actor_persona=actor.persona,
        action=action.value,
        entity_table=ERM_ENTITY_TABLE,
        entity_id=task.task_id,
        before=before,
        after=after,
        at=at,
    )


def _snapshot(task: SyncTask) -> dict[str, JsonValue]:
    """The card as an audit snapshot. Ids as strings; JSON has no UUID."""
    return {
        "state": task.state.value,
        "subject_kind": task.subject_kind.value,
        "subject_id": str(task.subject_id),
        "field_order_version": task.field_order_version,
        "assigned_to": str(task.assigned_to) if task.assigned_to else None,
        "confirmed_by": str(task.confirmed_by) if task.confirmed_by else None,
    }


def queue_task(task: SyncTask, actor: Actor, at: dt.datetime) -> ErmOutcome:
    """File a new card. QUEUED, unassigned, no pack yet.

    No human requirement and no persona check, deliberately. Filing work for a
    person to do is the least consequential act in this package, and §8 puts the
    Onboarding agent's ERM checklist work at "Auto (internal only)" — a scheduled
    job noticing an unpushed trainer and queueing the card is exactly the
    intended behaviour. The human requirement lands on `confirm_task()`, which is
    where a claim about the world gets made.

    The pack is NOT generated here. An open card renders its pack live from the
    source record on every read, so a card that sat in the queue for a week hands
    over today's values rather than last Tuesday's.
    """
    if task.state is not ErmSyncState.QUEUED:
        raise ErmIllegalTransitionError(ErmSyncAction.QUEUED, task.state)
    return ErmOutcome(
        task=task,
        event=_event(ErmSyncAction.QUEUED, actor, task, None, _snapshot(task), at),
    )


def assign_task(
    task: SyncTask,
    actor: Actor,
    assignee_id: UUID,
    at: dt.datetime,
) -> ErmOutcome:
    """§10: "assigns it to a named person".

    Legal from QUEUED and from ASSIGNED — reassignment is a normal event (someone
    is on leave, the work moved teams) and forcing a cancel-and-refile for it
    would fork the history of one push across two rows.

    Illegal from CONFIRMED, STALE and CANCELLED. A confirmed card is a record of
    something that already happened, and a stale one is that same record plus a
    note that it no longer holds; the successor card is what gets assigned. The
    successor already exists — `1900_erm_sync.sql` files it in the same
    transaction as the drift.
    """
    if not task.is_open:
        raise ErmIllegalTransitionError(ErmSyncAction.ASSIGNED, task.state)
    before = _snapshot(task)
    moved = replace(
        task,
        state=ErmSyncState.ASSIGNED,
        assigned_to=assignee_id,
        assigned_at=at,
    )
    return ErmOutcome(
        task=moved,
        event=_event(ErmSyncAction.ASSIGNED, actor, moved, before, _snapshot(moved), at),
    )


def confirm_task(
    task: SyncTask,
    actor: Actor,
    pack: FieldPack,
    at: dt.datetime,
) -> ErmOutcome:
    """§10: "they paste, they confirm".

    The claim being recorded is "I, this person, put these exact values into ERM
    today". So:

    * `actor.actor_id` is required — see the module docstring.
    * The `pack` handed in is the one the caller displayed, and it is frozen onto
      the row by `app/api/erm.py`. Not regenerated at write time: a pack
      regenerated after the human read the screen could differ from what they
      actually pasted, and the row would then attest to values nobody typed.
    * The pack's `field_order_version` is carried onto the card, so a
      confirmation made under an unverified order stays identifiable when the
      real order lands.

    Legal from QUEUED as well as ASSIGNED. Requiring assignment first would mean
    a Manager who did the push themselves has to assign it to themselves before
    they may say so, and a workflow that makes honesty inconvenient gets worked
    around rather than followed.
    """
    if actor.actor_id is None:
        raise ErmHumanActorRequiredError(ErmSyncAction.CONFIRMED)
    if not task.is_open:
        raise ErmIllegalTransitionError(ErmSyncAction.CONFIRMED, task.state)
    before = _snapshot(task)
    moved = replace(
        task,
        state=ErmSyncState.CONFIRMED,
        field_order_version=pack.field_order_version,
        confirmed_by=actor.actor_id,
        confirmed_at=at,
    )
    after = _snapshot(moved)
    # The pack is on the audit row as well as on the table. §11's "before, after"
    # is the whole point of an audit trail, and the thing in dispute six months
    # from now is what ERM was told, not which enum label the card held.
    after["field_pack"] = [dict(entry) for entry in pack.as_json()]
    after["field_order_verified"] = pack.field_order_verified
    return ErmOutcome(
        task=moved,
        event=_event(ErmSyncAction.CONFIRMED, actor, moved, before, after, at),
    )


def cancel_task(task: SyncTask, actor: Actor, reason: str, at: dt.datetime) -> ErmOutcome:
    """Withdraw an open card — the subject is gone, duplicated, or was a mistake.

    A reason is required and is not optional politeness. A cancelled ERM push is
    a decision that a record deliberately does NOT match ERM, which is exactly
    the divergence §10 is about; the reason is what stops it reading as an
    oversight to whoever finds it next.

    Only open cards. A confirmed push cannot be un-done from here, because it
    happened: somebody typed those values into a system this one cannot reach.
    Undoing it means going back to ERM, which is a new card.
    """
    if not reason.strip():
        raise ValueError(
            "Cancelling an ERM sync task requires a reason: a record that "
            "deliberately does not match ERM is exactly the divergence §10 is "
            "about, and an unexplained cancellation reads as an oversight."
        )
    if not task.is_open:
        raise ErmIllegalTransitionError(ErmSyncAction.CANCELLED, task.state)
    before = _snapshot(task)
    moved = replace(task, state=ErmSyncState.CANCELLED, cancelled_at=at)
    after = _snapshot(moved)
    after["reason"] = reason
    return ErmOutcome(
        task=moved,
        event=_event(ErmSyncAction.CANCELLED, actor, moved, before, after, at),
    )
