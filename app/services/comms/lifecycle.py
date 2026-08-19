"""The R4 ladder for a queued message — an ADAPTER, not a second state machine.

    DRAFT -> PENDING_APPROVAL -> APPROVED -> RELEASED

CLAUDE.md R4 governs "every artifact", and the machinery already exists in
`app/services/approval/`. Nothing in this module re-decides what that module has
already decided. Concretely, everything below is borrowed:

* the **grammar** — `check_transition()`, over
  `app.domain.enums.ALLOWED_TRANSITIONS`. There is no second transition table in
  this file, in `1700_comms_queue.sql`, or anywhere else. An illegal move raises
  `IllegalTransitionError` from the shared module, with the shared message.
* the **freeze** — `app.services.approval.hashing.content_hash()`, the same
  canonical sha256 that freezes a remuneration sheet.
* the **refusals** — `HumanActorRequiredError`, `MissingRejectionReasonError`,
  `FrozenContentMismatchError`, all raised as the shared types so `app/api/`
  translates them to the same status codes.
* the **actor** and the **audit vocabulary** — `Actor` and `ApprovalAction`.
* the **shape of the result** — a message plus the `AuditEvent` describing the
  change, returned together so the caller cannot persist one without the other
  (§11).

WHAT IS NOT BORROWED, AND WHY
=============================
`Artifact` and the four `state_machine` transition functions are keyed by
`ArtifactType`, a closed three-label enum of table names, mirrored by the
Postgres type `public.artifact_type` and by `app/db/models.py`. A comms message
is not one of those three, adding a fourth label means editing
`app/domain/enums.py` and migrating `artifact_versions`, and
`1700_comms_queue.sql` explains at length why that change belongs with the ANSWER
to §14 Q3 rather than ahead of it.

So this module supplies the two things that are genuinely comms-specific — the
authority lookup (`app/services/comms/authority.py`, deliberately empty) and the
payload that gets hashed — and delegates everything else. If a future change
opens `ArtifactType`, the correct move is to delete this file and call
`state_machine` directly, not to grow it.

RELEASE DOES NOT TRANSMIT
=========================
`release_message()` marks state. It opens no socket, calls no provider, imports
no client, and there is no `sent_at` column for it to stamp. RELEASED means "an
authenticated human cleared this to go" and nothing more; a provider, when one
exists, will read rows in that state and record delivery in its own columns.
Conflating the two is how a system starts reporting sends it never made.

PURITY
======
Like `app/services/approval/` and `app/services/remuneration/`, this layer takes
data and returns data. Persisting the message and its audit row, in one
transaction, is `app/api/comms.py`'s job. That is what makes every rule here
testable without a database (§3), and it means the audit row is built by the same
code that decided the transition was legal.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Final
from uuid import UUID

from pydantic import JsonValue

from app.core.audit import AuditEvent
from app.domain.enums import ArtifactState
from app.services.approval import (
    FROZEN_STATES,
    Actor,
    ApprovalAction,
    FrozenContentMismatchError,
    HumanActorRequiredError,
    MissingRejectionReasonError,
    check_transition,
    content_hash,
)
from app.services.comms.authority import (
    CommsUnauthorisedApproverError,
    comms_approver_personas,
)
from app.services.comms.diff import TemplateDiff, diff_from_template
from app.services.comms.types import CommsChannel, CommsRecipientKind

__all__ = [
    "COMMS_ENTITY_TABLE",
    "CommsAction",
    "CommsOutcome",
    "QueuedMessage",
    "amend_message",
    "approve_message",
    "comms_payload",
    "queue_message",
    "reject_message",
    "release_message",
    "submit_message",
    "supersede_message",
    "verify_message_frozen",
]

#: `AuditEvent.entity_table`, which §11 requires to be the Postgres table name
#: "so a row can be found again without guessing which id is meant".
COMMS_ENTITY_TABLE: Final[str] = "comms_messages"


class CommsAction(StrEnum):
    """The one audit label the artifact lifecycle has no word for.

    Everything else reuses `ApprovalAction` — submitted, approved, rejected,
    released, version_created, draft_amended — because a queued message walks
    exactly the same ladder and inventing parallel labels would make "show me
    every approval last month" a two-vocabulary query.

    Queueing is the exception: an artifact enters `artifact_versions` when it is
    first submitted, whereas a comms message is CREATED as a draft by whatever
    drafted it, usually an agent at autonomy level 2. That moment is worth its own
    line, because "who put this in the queue" and "who submitted it" are different
    questions and often different actors.
    """

    QUEUED = "comms.queued"


@dataclass(frozen=True, slots=True)
class QueuedMessage:
    """One outbound message on the R4 ladder. Immutable; every operation returns
    a new instance, so the pre-transition value is still in the caller's hand for
    the audit row.

    The content fields are exactly what §8 requires be visible at approval —
    channel, recipient, template, and the diff — plus the body they produce. All
    of them are hashed by `comms_payload()`, so approving a message freezes the
    review surface along with the text: "the approver saw this diff" is an
    equality test rather than a claim.

    `is_commercial` is content too, and hashed, because it is what decides who may
    see the row at all (R5). A message reclassified after approval would be
    visible to a different set of people than the one that approved it.

    The `content_hash` invariant is enforced in `__post_init__` rather than
    trusted, mirroring `app.services.approval.state_machine.Artifact` and the
    `comms_messages_approved_ck` constraint: a frozen state without a hash is a
    message claiming an approval it cannot evidence.
    """

    message_id: UUID
    program_id: UUID
    version: int
    state: ArtifactState

    channel: CommsChannel
    recipient_kind: CommsRecipientKind
    recipient_ref: str
    recipient_name: str | None

    template_key: str
    #: The RENDERED baseline (`app.services.comms.diff.render`), not the raw
    #: template. The left-hand side of the diff.
    template_body: str
    subject: str | None
    body: str

    #: CLAUDE.md R5. Declared by the drafter — no predicate can read prose — and
    #: forced true for a remuneration back-reference by the SQL constraint.
    is_commercial: bool

    content_hash: str | None = None
    approved_by: UUID | None = None
    approved_at: dt.datetime | None = None
    released_by: UUID | None = None
    released_at: dt.datetime | None = None

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError(f"QueuedMessage.version starts at 1, got {self.version}")
        frozen = self.state in FROZEN_STATES
        if frozen and not self.content_hash:
            raise ValueError(
                f"{self.state.value} comms message has no content_hash — CLAUDE.md R4: "
                "approval freezes and hashes. An approval that cannot be evidenced is not "
                "an approval."
            )
        if not frozen and self.content_hash:
            raise ValueError(
                f"{self.state.value} comms message carries a content_hash. Only APPROVED "
                "and RELEASED messages are frozen; a hash here implies a freeze that never "
                "happened."
            )

    @property
    def is_frozen(self) -> bool:
        """True once approval has hashed this message (R4)."""
        return self.state in FROZEN_STATES

    def diff(self) -> TemplateDiff:
        """The §8 review surface, recomputed from the two stored sides.

        The stored `comms_messages.diff` column is the canonical copy — it is
        what the approver was shown and it is hashed at approval. This recomputes
        the same thing from `template_body` and `body`, which is how a caller
        refreshes the column after an amendment, and how a test asserts the two
        agree.
        """
        return diff_from_template(self.template_body, self.body)

    def payload_hash(self) -> str:
        """Hash of the CURRENT content, whatever state the message is in.

        For a frozen message this must equal `content_hash`;
        `verify_message_frozen()` is the check that says so.
        """
        return content_hash(comms_payload(self))


@dataclass(frozen=True, slots=True)
class CommsOutcome:
    """The result of one lifecycle operation: the new message and its audit row.

    Both together, deliberately — `app.services.approval.ApprovalOutcome`'s
    reasoning verbatim. §11 requires an `AuditEvent` for every state transition,
    and returning the pair makes it impossible to persist the new state while
    forgetting the evidence.
    """

    message: QueuedMessage
    event: AuditEvent


def comms_payload(message: QueuedMessage) -> dict[str, object]:
    """What the R4 freeze covers for a queued message.

    Everything the approver was asked to judge: where it is going, how, from which
    template, what the template said, what the message says, and whether it is
    commercial. Nothing else — `notes` is commentary about the message (1300's
    reasoning, and the SQL freeze trigger leaves that column mutable for the same
    reason), and the lifecycle columns describe the freeze rather than being
    frozen by it.

    The diff is NOT hashed directly. It is a pure function of `template_body` and
    `body`, both of which are, so hashing it too would add nothing and would make
    the digest depend on the diff algorithm's version — meaning an improvement to
    `diff.py` would invalidate every historical approval.
    """
    return {
        "program_id": message.program_id,
        "channel": message.channel,
        "recipient_kind": message.recipient_kind,
        "recipient_ref": message.recipient_ref,
        "recipient_name": message.recipient_name,
        "template_key": message.template_key,
        "template_body": message.template_body,
        "subject": message.subject,
        "body": message.body,
        "is_commercial": message.is_commercial,
    }


def verify_message_frozen(message: QueuedMessage) -> None:
    """Raise unless a frozen message still hashes to its stored `content_hash`.

    Called by `release_message()`, and safe to call from a nightly integrity job:
    pure, reads nothing. This is what gives the freeze teeth — without it,
    "APPROVED" would be a column value rather than a claim about content, and a
    recipient swapped after approval would ride out on somebody else's signature.
    """
    if not message.is_frozen or message.content_hash is None:
        return
    recomputed = message.payload_hash()
    if recomputed != message.content_hash:
        raise FrozenContentMismatchError(
            message.message_id, message.version, message.content_hash, recomputed
        )


# --- the ladder ---------------------------------------------------------------


def queue_message(
    message: QueuedMessage, actor: Actor, at: dt.datetime | None = None
) -> CommsOutcome:
    """Record that a message entered the queue as a DRAFT.

    Not a transition — it is the birth of the row — so `check_transition()` is
    not called and no authority is required. §8 puts drafting at autonomy level 2
    ("propose, human edits and sends"), which is exactly what may happen with
    nobody at the keyboard, so `actor.actor_id` may be `None` here and only here
    plus `submit`/`amend`.

    Refuses anything but DRAFT: a message that arrives already approved has
    skipped the ladder, which is the single failure R4 exists to prevent.
    """
    if message.state is not ArtifactState.DRAFT:
        raise ValueError(
            f"a message enters the queue as {ArtifactState.DRAFT.value}, not "
            f"{message.state.value}. CLAUDE.md R4: nothing leaves the system unapproved, "
            "and nothing enters it already approved."
        )
    return _outcome(None, message, CommsAction.QUEUED.value, actor, at)


def amend_message(
    message: QueuedMessage, actor: Actor, body: str, at: dt.datetime | None = None
) -> CommsOutcome:
    """Replace the body of a DRAFT in place. Same version, still DRAFT.

    `app.services.approval.state_machine.amend_draft`'s reasoning, applied here: a
    draft has never been approved, so changing it supersedes nothing, and minting
    a version per keystroke would bury the versions somebody actually approved.

    Refused for PENDING_APPROVAL. A message sitting in front of an approver must
    not change underneath them — they would approve one text and another would be
    stored. Reject it back to DRAFT first; that is what the rejection edge is for
    and it leaves an audit row saying so.

    Only `body` is amendable. Changing the recipient, the channel or the template
    changes what message this IS, and the honest representation of that is a new
    draft — see `supersede_message()`.
    """
    if message.state is not ArtifactState.DRAFT:
        raise ValueError(
            f"amend_message() edits a {ArtifactState.DRAFT.value}; this message is "
            f"{message.state.value}. A message awaiting approval must be rejected back to "
            "DRAFT first, and an approved one is superseded (CLAUDE.md R4)."
        )
    return _outcome(
        message, replace(message, body=body), ApprovalAction.DRAFT_AMENDED.value, actor, at
    )


def submit_message(
    message: QueuedMessage, actor: Actor, at: dt.datetime | None = None
) -> CommsOutcome:
    """DRAFT -> PENDING_APPROVAL. Put the message in front of an approver.

    No persona check and no human requirement, exactly as
    `state_machine.submit_for_approval()`: preparing work for a human to approve
    is what an agent is allowed to do (§8, level 2). Reach is enforced by RLS in
    the database and mirrored in `app/api/comms.py` (R5), not re-implemented here.
    """
    check_transition(message.state, ArtifactState.PENDING_APPROVAL)
    return _outcome(
        message,
        replace(message, state=ArtifactState.PENDING_APPROVAL),
        ApprovalAction.SUBMITTED.value,
        actor,
        at,
    )


def approve_message(
    message: QueuedMessage, actor: Actor, at: dt.datetime | None = None
) -> CommsOutcome:
    """PENDING_APPROVAL -> APPROVED. Freezes and hashes the message (R4).

    **Raises `CommsApprovalAuthorityUndefinedError` for every message today**, and
    that is the designed behaviour, not a gap — §14 Q3 is open and
    `app/services/comms/authority.py` says why at length. The queue fills and
    stops until a human records who may sign a college-facing message.

    Three checks in this order — legal edge, human actor, authority — so the most
    fundamental refusal is the one the caller sees. The hash is taken last, over
    the content as it stands at this instant, and that digest is what
    `release_message()` will insist on.

    Approval does NOT release. No queue push, no provider call, no side effect of
    any kind: the message is merely releasable now, by a separate call writing a
    separate audit row (R4).
    """
    check_transition(message.state, ArtifactState.APPROVED)
    _require_human(actor, ApprovalAction.APPROVED)
    _require_authority(message, actor)

    approved = replace(
        message,
        state=ArtifactState.APPROVED,
        content_hash=message.payload_hash(),
        approved_by=actor.actor_id,
        approved_at=_stamp(at),
    )
    return _outcome(message, approved, ApprovalAction.APPROVED.value, actor, at)


def reject_message(
    message: QueuedMessage, actor: Actor, reason: str, at: dt.datetime | None = None
) -> CommsOutcome:
    """PENDING_APPROVAL -> DRAFT, with a required reason.

    Same authority as approval: the power to withhold approval is the power to
    approve, and letting an actor who could not have approved send a message back
    would be a denial with no accountability. So this raises today too.

    A non-blank reason is mandatory and lands on the audit row. The draft goes
    back to whoever has to fix it, and "rejected" with no reason turns one review
    cycle into three.
    """
    check_transition(message.state, ArtifactState.DRAFT)
    if not reason.strip():
        raise MissingRejectionReasonError(
            "A rejection needs a stated reason: the message goes back to its drafter, and "
            "a blank reason makes a required field a formality."
        )
    _require_human(actor, ApprovalAction.REJECTED)
    _require_authority(message, actor)

    return _outcome(
        message,
        replace(message, state=ArtifactState.DRAFT),
        ApprovalAction.REJECTED.value,
        actor,
        at,
        detail={"reason": reason.strip()},
    )


def release_message(
    message: QueuedMessage, actor: Actor, at: dt.datetime | None = None
) -> CommsOutcome:
    """APPROVED -> RELEASED. Terminal, human-only, and **silent**.

    RELEASING IS NOT TRANSMITTING. This function marks state and returns data. It
    opens no connection, imports no provider client, and there is deliberately no
    `sent_at` column for it to stamp — no email, WhatsApp or ticket integration
    exists in this phase and none may be added here. RELEASED means "an
    authenticated human cleared this to go", which makes the message **eligible**
    to be sent by whatever transport is built later, reading rows in this state.

    `verify_message_frozen()` runs first. Releasing a message whose recipient or
    body has drifted from the approved hash is precisely the event R4 exists to
    prevent, and this is the last moment the drift is cheap to catch.

    R3 makes this human-only, and no agent toolset may bind a function that reaches
    it: `app/agents/tools/catalog.py` is a closed set of read tools plus
    `save_draft`, `tools/rule_linter.py` L3 fails the build on a release-capable
    tool name or a send-shaped import under `app/agents/`, and
    `tests/unit/test_agents_toolsets.py` asserts the same. Three independent
    checks, none of which is a prompt instruction.
    """
    check_transition(message.state, ArtifactState.RELEASED)
    _require_human(actor, ApprovalAction.RELEASED)
    _require_authority(message, actor)
    verify_message_frozen(message)

    released = replace(
        message,
        state=ArtifactState.RELEASED,
        released_by=actor.actor_id,
        released_at=_stamp(at),
    )
    return _outcome(message, released, ApprovalAction.RELEASED.value, actor, at)


def supersede_message(
    message: QueuedMessage,
    actor: Actor,
    successor_id: UUID,
    *,
    body: str,
    template_body: str | None = None,
    at: dt.datetime | None = None,
) -> CommsOutcome:
    """Supersede a frozen message with a fresh DRAFT at `version + 1`.

    R4: "Editing an approved artifact creates a new version in DRAFT requiring
    fresh approval." This is NOT a transition and `check_transition()` is not
    called — `ALLOWED_TRANSITIONS` deliberately has no APPROVED -> DRAFT edge.
    Treating an edit as a transition would move the approved row backwards and
    destroy the record of what was approved.

    A NEW `message_id` is required rather than reusing the old one, because unlike
    `artifact_versions` the content and the lifecycle share one table here: the
    predecessor row stays exactly as approved, and the successor is a sibling that
    points back at it through `comms_messages.supersedes_id`. The successor
    carries none of the freeze — no hash, no approver, no releaser — and walks the
    whole ladder again.

    Only legal from a frozen state. Editing a DRAFT is `amend_message()`, and
    editing something in PENDING_APPROVAL is refused — reject it first.
    """
    if not message.is_frozen:
        raise ValueError(
            f"supersede_message() replaces a frozen message; this one is "
            f"{message.state.value}. Use amend_message() for a DRAFT, or reject a message "
            "awaiting approval before changing it (CLAUDE.md R4)."
        )
    successor = QueuedMessage(
        message_id=successor_id,
        program_id=message.program_id,
        version=message.version + 1,
        state=ArtifactState.DRAFT,
        channel=message.channel,
        recipient_kind=message.recipient_kind,
        recipient_ref=message.recipient_ref,
        recipient_name=message.recipient_name,
        template_key=message.template_key,
        template_body=template_body if template_body is not None else message.template_body,
        subject=message.subject,
        body=body,
        is_commercial=message.is_commercial,
    )
    return _outcome(message, successor, ApprovalAction.VERSION_CREATED.value, actor, at)


# --- internals ----------------------------------------------------------------


def _require_human(actor: Actor, action: ApprovalAction) -> None:
    if actor.actor_id is None:
        raise HumanActorRequiredError(action)


def _require_authority(message: QueuedMessage, actor: Actor) -> None:
    """The §14 Q3 gate. Raises `CommsApprovalAuthorityUndefinedError` today.

    The second branch is unreachable while the authority table is empty. It is
    written anyway, for the reason `CommsUnauthorisedApproverError`'s docstring
    gives: filling the dict in should be a one-line change, not a design exercise
    under time pressure.
    """
    permitted = comms_approver_personas(message.recipient_kind)
    if actor.persona not in permitted:
        raise CommsUnauthorisedApproverError(message.recipient_kind, actor.persona, permitted)


def _stamp(at: dt.datetime | None) -> dt.datetime:
    """The instant of the action, UTC (§11). Explicit `at` wins, for replay."""
    return at if at is not None else dt.datetime.now(dt.UTC)


def _snapshot(message: QueuedMessage) -> dict[str, JsonValue]:
    """The audit row's before/after view of a message.

    The BODY is not copied in, for `state_machine._snapshot()`'s reason and one
    more of its own: it can be long, and a message about a payout is commercials
    that would then exist in `audit_events`, whose read policies cannot inspect a
    jsonb payload (1300 says so explicitly). `payload_hash` fingerprints it
    instead, which is enough to prove what changed and enough to prove what did
    not — the hash on the submission row equals the `content_hash` on the approval
    row exactly when the thing approved is the thing submitted.

    Recipient and channel ARE included: "who was this going to" is the first
    question anybody asks about an outbound message, and it is not secret from
    anyone who could see the row at all.
    """
    return {
        "state": message.state.value,
        "version": message.version,
        "channel": message.channel.value,
        "recipient_kind": message.recipient_kind.value,
        "recipient_ref": message.recipient_ref,
        "template_key": message.template_key,
        "is_commercial": message.is_commercial,
        "payload_hash": message.payload_hash(),
        "content_hash": message.content_hash,
    }


def _outcome(
    before: QueuedMessage | None,
    after: QueuedMessage,
    action: str,
    actor: Actor,
    at: dt.datetime | None,
    detail: dict[str, JsonValue] | None = None,
) -> CommsOutcome:
    """Pair the new message with its audit event.

    `before` is `None` only for `queue_message()`, where §11's "before" genuinely
    does not exist — the row did not. Everything else has both halves.
    """
    after_snapshot = _snapshot(after)
    if detail:
        after_snapshot |= detail
    event = AuditEvent(
        actor_id=actor.actor_id,
        actor_persona=actor.persona,
        action=action,
        entity_table=COMMS_ENTITY_TABLE,
        entity_id=after.message_id,
        before=_snapshot(before) if before is not None else None,
        after=after_snapshot,
        at=_stamp(at),
    )
    return CommsOutcome(message=after, event=event)
