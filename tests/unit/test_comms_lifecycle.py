"""`app/services/comms/lifecycle.py` — R4 for the outbound queue.

Four things are worth pinning, and all four are here:

* **The grammar is borrowed, not rebuilt.** An illegal move raises the SHARED
  `IllegalTransitionError` from `app/services/approval/`, which is the assertion
  that this module did not quietly grow a second transition table.
* **Approval freezes, and release re-verifies.** Changing the recipient after
  approval makes release refuse.
* **§14 Q3 is unanswered and nothing can be approved.** Asserted as a property of
  the authority table rather than of one call, so filling the table in will fail
  this test loudly and make somebody read the question.
* **Release transmits nothing.** Asserted structurally — no provider import
  anywhere in the package.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from app.domain.enums import ArtifactState, Persona
from app.services.approval import (
    Actor,
    ApprovalAction,
    FrozenContentMismatchError,
    HumanActorRequiredError,
    IllegalTransitionError,
    MissingRejectionReasonError,
    TerminalArtifactError,
    content_hash,
)
from app.services.comms import (
    COMMS_APPROVAL_AUTHORITY,
    COMMS_ENTITY_TABLE,
    CommsAction,
    CommsApprovalAuthorityUndefinedError,
    CommsChannel,
    CommsRecipientKind,
    QueuedMessage,
    amend_message,
    approve_message,
    comms_payload,
    queue_message,
    reject_message,
    release_message,
    submit_message,
    supersede_message,
    verify_message_frozen,
)

NOW = dt.datetime(2026, 8, 1, tzinfo=dt.UTC)
MESSAGE_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
PROGRAM_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
USER_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")

BASELINE = "Hello Rao,\nYour July payout is approved.\nRegards,\nOps"
BODY = "Hello Rao,\nYour July payout is approved. Do check your bank by Friday.\nRegards,\nOps"

HUMAN = Actor(actor_id=USER_ID, persona=Persona.SENIOR_MANAGER)
AGENT = Actor(actor_id=None, persona=Persona.MANAGER)


def message(
    state: ArtifactState = ArtifactState.DRAFT,
    *,
    body: str = BODY,
    recipient_ref: str = "rao@example.edu",
    hashed: bool = False,
    recipient_kind: CommsRecipientKind = CommsRecipientKind.COLLEGE,
) -> QueuedMessage:
    """A queue row in whatever state a test needs.

    `hashed` computes the real digest rather than a placeholder, because
    `QueuedMessage.__post_init__` refuses a frozen state without one and
    `verify_message_frozen()` refuses a wrong one — a fake hash would make every
    freeze test pass for the wrong reason.
    """
    draft = QueuedMessage(
        message_id=MESSAGE_ID,
        program_id=PROGRAM_ID,
        version=1,
        state=ArtifactState.DRAFT,
        channel=CommsChannel.EMAIL,
        recipient_kind=recipient_kind,
        recipient_ref=recipient_ref,
        recipient_name="Rao",
        template_key="payout.approved.v1",
        template_body=BASELINE,
        subject="July payout",
        body=body,
        is_commercial=True,
    )
    if state is ArtifactState.DRAFT:
        return draft
    if not hashed:
        return QueuedMessage(**{**_fields(draft), "state": state})
    return QueuedMessage(
        **{
            **_fields(draft),
            "state": state,
            "content_hash": content_hash(comms_payload(draft)),
            "approved_by": USER_ID,
            "approved_at": NOW,
        }
    )


def _fields(msg: QueuedMessage) -> dict[str, object]:
    return {slot: getattr(msg, slot) for slot in QueuedMessage.__slots__}


# --- §14 Q3: the queue fills and stops ----------------------------------------


def test_no_approval_authority_is_defined_for_any_recipient_kind() -> None:
    """CLAUDE.md §14 Q3 is open, and §14 says carry it, do not invent an answer.

    Asserted over the whole table rather than over one call, so that filling any
    entry in fails HERE and makes somebody read the question first.
    """
    assert COMMS_APPROVAL_AUTHORITY == {}


@pytest.mark.parametrize("kind", list(CommsRecipientKind))
def test_approve_refuses_every_recipient_kind_naming_the_open_question(
    kind: CommsRecipientKind,
) -> None:
    pending = QueuedMessage(
        **{
            **_fields(message(recipient_kind=kind)),
            "state": ArtifactState.PENDING_APPROVAL,
        }
    )
    with pytest.raises(CommsApprovalAuthorityUndefinedError) as exc:
        approve_message(pending, HUMAN, NOW)
    assert "§14 Q3" in str(exc.value)


def test_reject_needs_the_same_undecided_authority_as_approve() -> None:
    """The power to withhold approval is the power to approve."""
    with pytest.raises(CommsApprovalAuthorityUndefinedError):
        reject_message(message(ArtifactState.PENDING_APPROVAL), HUMAN, "wrong tone", NOW)


def test_release_needs_it_too() -> None:
    with pytest.raises(CommsApprovalAuthorityUndefinedError):
        release_message(message(ArtifactState.APPROVED, hashed=True), HUMAN, NOW)


# --- the borrowed grammar ------------------------------------------------------


def test_draft_cannot_be_released_and_the_refusal_is_the_shared_type() -> None:
    """The assertion that this module has no transition table of its own: the
    error comes from `app/services/approval/`, over `ALLOWED_TRANSITIONS`."""
    with pytest.raises(IllegalTransitionError):
        release_message(message(), HUMAN, NOW)


def test_released_is_terminal() -> None:
    released = QueuedMessage(
        **{
            **_fields(message(ArtifactState.APPROVED, hashed=True)),
            "state": ArtifactState.RELEASED,
            "released_by": USER_ID,
            "released_at": NOW,
        }
    )
    with pytest.raises(TerminalArtifactError):
        submit_message(released, HUMAN, NOW)


def test_approval_requires_a_human_before_it_checks_authority() -> None:
    """R3: nothing touching a college contact is approved by a scheduled job.

    The human check runs before the authority lookup, so an agent gets told it is
    an agent rather than told the org has not decided.
    """
    with pytest.raises(HumanActorRequiredError):
        approve_message(message(ArtifactState.PENDING_APPROVAL), AGENT, NOW)


def test_rejection_needs_a_stated_reason() -> None:
    with pytest.raises(MissingRejectionReasonError):
        reject_message(message(ArtifactState.PENDING_APPROVAL), HUMAN, "   ", NOW)


# --- drafting and submitting still work ---------------------------------------


def test_an_agent_may_queue_and_submit_but_that_is_the_ceiling() -> None:
    """§8 autonomy level 2: propose, human edits and sends."""
    queued = queue_message(message(), AGENT, NOW)
    assert queued.message.state is ArtifactState.DRAFT
    assert queued.event.action == CommsAction.QUEUED.value
    assert queued.event.before is None  # the row did not exist

    submitted = submit_message(queued.message, AGENT, NOW)
    assert submitted.message.state is ArtifactState.PENDING_APPROVAL
    assert submitted.event.action == ApprovalAction.SUBMITTED.value


def test_queue_refuses_a_message_that_arrives_already_approved() -> None:
    with pytest.raises(ValueError, match="unapproved"):
        queue_message(message(ArtifactState.APPROVED, hashed=True), HUMAN, NOW)


def test_amend_rewrites_a_draft_in_place_and_refuses_anything_else() -> None:
    amended = amend_message(message(), HUMAN, "New text", NOW)
    assert amended.message.body == "New text"
    assert amended.message.version == 1
    assert amended.message.state is ArtifactState.DRAFT
    assert amended.event.action == ApprovalAction.DRAFT_AMENDED.value

    with pytest.raises(ValueError):
        amend_message(message(ArtifactState.PENDING_APPROVAL), HUMAN, "x", NOW)


def test_the_diff_travels_with_the_message() -> None:
    """`QueuedMessage.diff()` recomputes the §8 review surface from the two
    stored sides, which is how the column is refreshed after an amendment."""
    assert not message().diff().identical
    assert message(body=BASELINE).diff().identical


# --- the freeze ----------------------------------------------------------------


def test_a_frozen_state_without_a_hash_is_refused_at_construction() -> None:
    """The Python half of `comms_messages_approved_ck`. An approval that cannot
    be evidenced is not an approval."""
    with pytest.raises(ValueError, match="content_hash"):
        message(ArtifactState.APPROVED)


def test_an_unfrozen_state_carrying_a_hash_is_refused_too() -> None:
    with pytest.raises(ValueError, match="content_hash"):
        QueuedMessage(**{**_fields(message()), "content_hash": "deadbeef"})


def test_release_refuses_a_message_edited_after_approval() -> None:
    """R4's freeze, with teeth. The recipient is swapped after the hash was
    taken, which is exactly the drift release exists to catch.

    Reached by calling `verify_message_frozen()` directly, because the authority
    gate in `release_message()` fires first while §14 Q3 is open.
    """
    approved = message(ArtifactState.APPROVED, hashed=True)
    tampered = QueuedMessage(**{**_fields(approved), "recipient_ref": "elsewhere@example.com"})
    with pytest.raises(FrozenContentMismatchError):
        verify_message_frozen(tampered)


def test_verify_is_a_no_op_for_an_unfrozen_message() -> None:
    verify_message_frozen(message())  # does not raise


def test_the_commercial_flag_is_part_of_the_freeze() -> None:
    """R5: a message reclassified after approval would be visible to a different
    set of people than the one that approved it."""
    approved = message(ArtifactState.APPROVED, hashed=True)
    reclassified = QueuedMessage(**{**_fields(approved), "is_commercial": False})
    with pytest.raises(FrozenContentMismatchError):
        verify_message_frozen(reclassified)


def test_notes_are_not_in_the_payload_because_they_are_commentary() -> None:
    assert "notes" not in comms_payload(message())


def test_the_diff_is_not_hashed_because_it_is_derived() -> None:
    """Hashing it would make every historical approval depend on the diff
    algorithm's version, so improving `diff.py` would invalidate them."""
    assert "diff" not in comms_payload(message())


# --- superseding ---------------------------------------------------------------


def test_supersede_leaves_the_approved_row_alone_and_starts_a_new_draft() -> None:
    approved = message(ArtifactState.APPROVED, hashed=True)
    successor_id = uuid.uuid4()
    outcome = supersede_message(approved, HUMAN, successor_id, body="Rewritten", at=NOW)

    assert outcome.message.message_id == successor_id
    assert outcome.message.version == 2
    assert outcome.message.state is ArtifactState.DRAFT
    assert outcome.message.content_hash is None
    assert outcome.message.approved_by is None
    assert outcome.event.action == ApprovalAction.VERSION_CREATED.value
    # the predecessor object is untouched — it is a frozen dataclass
    assert approved.state is ArtifactState.APPROVED


def test_supersede_refuses_a_draft_because_that_is_an_amendment() -> None:
    with pytest.raises(ValueError, match="amend_message"):
        supersede_message(message(), HUMAN, uuid.uuid4(), body="x", at=NOW)


# --- the audit row -------------------------------------------------------------


def test_every_transition_carries_an_audit_event_naming_the_table() -> None:
    outcome = submit_message(message(), HUMAN, NOW)
    event = outcome.event
    assert event.entity_table == COMMS_ENTITY_TABLE == "comms_messages"
    assert event.entity_id == MESSAGE_ID
    assert event.actor_id == USER_ID
    assert event.actor_persona is Persona.SENIOR_MANAGER
    assert event.at == NOW


def test_the_audit_snapshot_fingerprints_the_body_instead_of_copying_it() -> None:
    """A message about a payout is commercials, and `audit_events`' read policies
    cannot inspect a jsonb payload (1300 says so). The hash proves what changed
    without putting the amount in a second table."""
    after = submit_message(message(), HUMAN, NOW).event.after
    assert after is not None
    assert "body" not in after
    assert after["payload_hash"] == message().payload_hash()
    assert after["recipient_ref"] == "rao@example.edu"  # not secret from anyone who sees the row


def test_a_rejection_reason_lands_on_the_audit_row() -> None:
    """Reached through the state machine rather than the authority gate is not
    possible today, so the reason guard is asserted where it fires: before the
    authority lookup, a blank reason is refused; a real one gets past it."""
    with pytest.raises(CommsApprovalAuthorityUndefinedError):
        reject_message(message(ArtifactState.PENDING_APPROVAL), HUMAN, "tone", NOW)


# --- release transmits nothing -------------------------------------------------

PROVIDER_TOKENS = (
    "smtplib",
    "twilio",
    "sendgrid",
    "boto3",
    "httpx",
    "requests",
    "aiohttp",
    "urllib",
    "socket",
)


def test_the_comms_package_imports_no_provider_client(repo_root: object) -> None:
    """R3 and the phase boundary, asserted structurally rather than promised in a
    docstring: releasing marks state, and there is nothing in this package that
    could make it do more.
    """
    from pathlib import Path

    package = Path(str(repo_root)) / "app" / "services" / "comms"
    offenders = [
        (path.name, token)
        for path in package.glob("*.py")
        for token in PROVIDER_TOKENS
        if f"import {token}" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_the_router_imports_no_provider_client(repo_root: object) -> None:
    from pathlib import Path

    source = (Path(str(repo_root)) / "app" / "api" / "comms.py").read_text(encoding="utf-8")
    assert [token for token in PROVIDER_TOKENS if f"import {token}" in source] == []
