"""Tests for `app.services.approval.state_machine` — CLAUDE.md R4 and §12.

R4 has four clauses and each gets its own section below:

* the ladder DRAFT -> PENDING_APPROVAL -> APPROVED -> RELEASED, every legal edge
  with a passing case and every illegal one refused by type;
* approval freezes and hashes the version;
* editing an approved artifact creates a NEW version in DRAFT;
* approval and release are separate actions with separate audit rows.

Plus the judgement call the module is built around: an artifact type with no
entry in `APPROVAL_AUTHORITY` must RAISE. §14 Q3 is open, and a test that made an
invented authority pass would be the mechanism by which an open governance
question quietly became a decision.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import FrozenInstanceError
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from app.domain.enums import (
    ALLOWED_TRANSITIONS,
    APPROVAL_AUTHORITY,
    ArtifactState,
    ArtifactType,
    Persona,
)
from app.services.approval import state_machine as sm
from app.services.approval.hashing import CanonicalisationError
from app.services.approval.state_machine import (
    Actor,
    ApprovalAction,
    ApprovalAuthorityUndefinedError,
    ApprovalOutcome,
    Artifact,
    FrozenContentMismatchError,
    HumanActorRequiredError,
    IllegalTransitionError,
    MissingRejectionReasonError,
    TerminalArtifactError,
    UnauthorisedApproverError,
    VersioningError,
    amend_draft,
    approve,
    approver_personas,
    can_transition,
    check_transition,
    new_version,
    reject,
    release,
    submit_for_approval,
    verify_frozen,
)

SENIOR_ID = UUID("aaaaaaaa-0000-0000-0000-000000000001")
MANAGER_ID = UUID("aaaaaaaa-0000-0000-0000-000000000002")
ARTIFACT_ID = UUID("bbbbbbbb-0000-0000-0000-000000000001")

SENIOR = Actor(actor_id=SENIOR_ID, persona=Persona.SENIOR_MANAGER)
MANAGER = Actor(actor_id=MANAGER_ID, persona=Persona.MANAGER)
LDE = Actor(actor_id=uuid4(), persona=Persona.LDE_EXECUTIVE)
#: A scheduled payout run or an agent: no human behind it (§8, autonomy 2).
SYSTEM = Actor(actor_id=None, persona=Persona.SENIOR_MANAGER)

#: The one artifact type §4 settles: "payout approval" is the Senior Manager's.
SHEET = ArtifactType.REMUNERATION_SHEET

#: Artifact types §14 Q3 leaves open. Derived from the enum, not hardcoded, so
#: this suite keeps passing on the day Q3 is answered and entries are added.
UNDEFINED_TYPES = [t for t in ArtifactType if t not in APPROVAL_AUTHORITY]


def payload(net: str = "14035") -> dict[str, object]:
    return {
        "pan": "BCDPS1234K",
        "invoice_number": "BCDP/26-27/JUL1",
        "net": Decimal(net),
        "period_start": dt.date(2026, 7, 26),
    }


def draft(artifact_type: ArtifactType = SHEET, **overrides: object) -> Artifact:
    """A fresh DRAFT. Each test moves exactly one thing."""
    base: dict[str, object] = {
        "artifact_type": artifact_type,
        "artifact_id": ARTIFACT_ID,
        "version": 1,
        "state": ArtifactState.DRAFT,
        "payload": payload(),
    }
    base.update(overrides)
    return Artifact(**base)  # type: ignore[arg-type]


def pending(artifact_type: ArtifactType = SHEET) -> Artifact:
    return submit_for_approval(draft(artifact_type), SYSTEM).artifact


def approved(artifact_type: ArtifactType = SHEET) -> Artifact:
    return approve(pending(artifact_type), SENIOR).artifact


def released(artifact_type: ArtifactType = SHEET) -> Artifact:
    return release(approved(artifact_type), SENIOR).artifact


STATE_BUILDERS = {
    ArtifactState.DRAFT: draft,
    ArtifactState.PENDING_APPROVAL: pending,
    ArtifactState.APPROVED: approved,
    ArtifactState.RELEASED: released,
}


# --- 1. the ladder: every edge, legal and illegal ----------------------------


@pytest.mark.parametrize("current", list(ArtifactState))
@pytest.mark.parametrize("target", list(ArtifactState))
def test_every_state_pair_matches_allowed_transitions(
    current: ArtifactState, target: ArtifactState
) -> None:
    """`ALLOWED_TRANSITIONS` is the whole grammar — including the self-edges.

    Sixteen pairs, no exceptions carved out. A DRAFT -> DRAFT "no-op" would be a
    state write with no meaning and an audit row that says nothing happened.
    """
    legal = target in ALLOWED_TRANSITIONS[current]
    assert can_transition(current, target) is legal

    if legal:
        assert check_transition(current, target) is None
        return

    expected = (
        TerminalArtifactError if current is ArtifactState.RELEASED else IllegalTransitionError
    )
    with pytest.raises(expected) as exc:
        check_transition(current, target)
    assert isinstance(exc.value, IllegalTransitionError)
    assert exc.value.current is current
    assert exc.value.target is target


def test_illegal_transition_raises_rather_than_returning_false() -> None:
    """A refusal that is merely falsy gets dropped by one missing `if`.

    The failure mode of that omission is an unapproved artifact leaving the
    system, which is the single thing R4 exists to prevent.
    """
    with pytest.raises(IllegalTransitionError):
        release(pending(), SENIOR)


def test_released_is_terminal() -> None:
    """A released artifact is superseded by a new version, never re-opened."""
    final = released()
    assert ALLOWED_TRANSITIONS[ArtifactState.RELEASED] == frozenset()
    for operation in (submit_for_approval, approve, release):
        with pytest.raises(TerminalArtifactError):
            operation(final, SENIOR)


def test_submit_passes_from_draft_and_is_refused_from_anywhere_else() -> None:
    outcome = submit_for_approval(draft(), SYSTEM)
    assert outcome.artifact.state is ArtifactState.PENDING_APPROVAL
    assert outcome.artifact.content_hash is None  # not frozen until approval

    for state in (ArtifactState.PENDING_APPROVAL, ArtifactState.APPROVED, ArtifactState.RELEASED):
        with pytest.raises(IllegalTransitionError):
            submit_for_approval(STATE_BUILDERS[state](), SENIOR)


def test_submit_needs_no_human_because_an_agent_may_propose() -> None:
    """§8 autonomy level 2: an agent drafts and proposes; a human approves."""
    assert submit_for_approval(draft(), SYSTEM).artifact.state is ArtifactState.PENDING_APPROVAL


def test_rejection_returns_pending_approval_to_draft() -> None:
    outcome = reject(pending(), SENIOR, reason="Attendance for 29 Jul is unmarked.")
    assert outcome.artifact.state is ArtifactState.DRAFT
    assert outcome.artifact.content_hash is None
    assert outcome.event.action == ApprovalAction.REJECTED.value
    assert outcome.event.after is not None
    assert outcome.event.after["reason"] == "Attendance for 29 Jul is unmarked."


@pytest.mark.parametrize("blank", ["", "   ", "\n\t"])
def test_rejection_without_a_stated_reason_is_refused(blank: str) -> None:
    """A blank string is how a required field becomes a formality (cf. §7)."""
    with pytest.raises(MissingRejectionReasonError):
        reject(pending(), SENIOR, reason=blank)


def test_rejection_is_refused_from_a_state_that_is_not_pending() -> None:
    with pytest.raises(IllegalTransitionError):
        reject(approved(), SENIOR, reason="too late")


# --- 2. approval authority: the missing entries must RAISE -------------------


def test_remuneration_sheet_is_approved_by_a_senior_manager() -> None:
    """§4 puts "payout approval" in the Senior Manager's column, explicitly."""
    assert approver_personas(SHEET) == frozenset({Persona.SENIOR_MANAGER})
    assert approve(pending(), SENIOR).artifact.state is ArtifactState.APPROVED


@pytest.mark.parametrize("actor", [MANAGER, LDE])
def test_persona_outside_the_authority_set_cannot_approve(actor: Actor) -> None:
    with pytest.raises(UnauthorisedApproverError) as exc:
        approve(pending(), actor)
    assert exc.value.persona is actor.persona
    assert exc.value.permitted == frozenset({Persona.SENIOR_MANAGER})


@pytest.mark.parametrize("artifact_type", UNDEFINED_TYPES)
def test_artifact_type_without_an_authority_raises(artifact_type: ArtifactType) -> None:
    """The single most important judgement call in this module.

    `APPROVAL_AUTHORITY` omits these types because §14 Q3 — "Approval authority
    for college-facing comms: Manager or Senior Manager?" — is unanswered, and
    §14 says carry the open questions rather than invent answers. So the attempt
    fails loudly, naming the question, instead of defaulting to whichever persona
    would have made this test green.
    """
    with pytest.raises(ApprovalAuthorityUndefinedError) as exc:
        approver_personas(artifact_type)
    assert "§14 Q3" in str(exc.value)
    assert exc.value.artifact_type is artifact_type


@pytest.mark.parametrize("artifact_type", UNDEFINED_TYPES)
def test_undefined_authority_blocks_both_approval_and_release(
    artifact_type: ArtifactType,
) -> None:
    """Nothing of an undecided type can leave the system by any route."""
    with pytest.raises(ApprovalAuthorityUndefinedError):
        approve(pending(artifact_type), SENIOR)
    with pytest.raises(ApprovalAuthorityUndefinedError):
        reject(pending(artifact_type), SENIOR, reason="no authority defined")


def test_only_the_remuneration_sheet_has_a_decided_authority_today() -> None:
    """Guards against an authority being invented to make something pass.

    If this fails because §14 Q3 was answered and an entry was added, delete the
    assertion — after checking the answer came from Rajesh and not from a
    debugging session.
    """
    assert set(APPROVAL_AUTHORITY) == {ArtifactType.REMUNERATION_SHEET}


@pytest.mark.parametrize("operation", ["approve", "release"])
def test_a_system_actor_cannot_approve_or_release(operation: str) -> None:
    """R3: release requires an authenticated human session; §8 caps money at 3."""
    artifact = pending() if operation == "approve" else approved()
    call = approve if operation == "approve" else release
    with pytest.raises(HumanActorRequiredError):
        call(artifact, SYSTEM)


def test_a_system_actor_cannot_reject_either() -> None:
    with pytest.raises(HumanActorRequiredError):
        reject(pending(), SYSTEM, reason="looks wrong")


# --- 3. approval freezes and hashes the version ------------------------------


def test_approval_freezes_and_hashes() -> None:
    artifact = approve(pending(), SENIOR).artifact
    assert artifact.is_frozen
    assert artifact.content_hash == artifact.payload_hash()
    assert artifact.approved_by == SENIOR_ID
    assert artifact.approved_at is not None
    assert artifact.approved_at.tzinfo is not None  # §11: UTC in the DB


def test_the_freeze_is_of_the_content_not_of_the_row() -> None:
    """Two artifacts with different content freeze to different hashes."""
    one = approve(pending(), SENIOR).artifact
    other = approve(
        submit_for_approval(draft(payload=payload("14036")), SYSTEM).artifact, SENIOR
    ).artifact
    assert one.content_hash != other.content_hash


def test_release_refuses_an_artifact_edited_after_approval() -> None:
    """The freeze has teeth: R4's guarantee is checked, not asserted.

    A nested container is still reachable by whoever passed it in — the artifact
    copies only the top level. That is the honest guarantee, and this is what
    catches the mutation: the hash, not the type.
    """
    rows: list[dict[str, object]] = [{"net": Decimal("14035")}]
    artifact = approve(
        submit_for_approval(draft(payload={"rows": rows}), SYSTEM).artifact, SENIOR
    ).artifact
    verify_frozen(artifact)  # clean before the tamper

    rows[0]["net"] = Decimal("99999")

    with pytest.raises(FrozenContentMismatchError) as exc:
        release(artifact, SENIOR)
    assert exc.value.stored != exc.value.recomputed
    with pytest.raises(FrozenContentMismatchError):
        verify_frozen(artifact)


def test_verify_frozen_is_silent_for_an_unfrozen_artifact() -> None:
    assert verify_frozen(draft()) is None
    assert verify_frozen(pending()) is None


def test_an_unfreezable_payload_fails_at_submission_not_at_approval() -> None:
    """A float in the payload (R7) must surface before a human is involved.

    Every operation snapshots the payload hash for its audit row, so a payload
    that cannot be canonicalised is refused at the first step rather than at the
    freeze — the approver never sees an artifact that could not have been frozen.
    """
    with pytest.raises(CanonicalisationError):
        submit_for_approval(draft(payload={"net": 14035.0}), SYSTEM)


def test_a_frozen_state_without_a_hash_cannot_be_constructed() -> None:
    """An approval that cannot be evidenced is not an approval."""
    for state in (ArtifactState.APPROVED, ArtifactState.RELEASED):
        with pytest.raises(ValueError, match="content_hash"):
            draft(state=state)


def test_an_unfrozen_state_carrying_a_hash_cannot_be_constructed() -> None:
    """A hash before approval implies a freeze that never happened."""
    for state in (ArtifactState.DRAFT, ArtifactState.PENDING_APPROVAL):
        with pytest.raises(ValueError, match="content_hash"):
            draft(state=state, content_hash="deadbeef")


def test_version_starts_at_one() -> None:
    with pytest.raises(ValueError, match="version"):
        draft(version=0)


def test_payload_is_copied_and_the_top_level_is_read_only() -> None:
    source = payload()
    artifact = draft(payload=source)
    source["net"] = Decimal("1")
    assert artifact.payload["net"] == Decimal("14035")
    with pytest.raises(TypeError):
        artifact.payload["net"] = Decimal("1")  # type: ignore[index]


def test_artifacts_are_immutable() -> None:
    with pytest.raises(FrozenInstanceError):
        draft().state = ArtifactState.APPROVED  # type: ignore[misc]


# --- 4. approval and release are separate actions ----------------------------


def test_release_needs_a_separate_call_and_writes_a_separate_audit_row() -> None:
    approval = approve(pending(), SENIOR)
    assert approval.artifact.state is ArtifactState.APPROVED
    assert approval.artifact.released_by is None
    assert approval.artifact.released_at is None

    releasing = release(approval.artifact, SENIOR)
    assert releasing.artifact.state is ArtifactState.RELEASED
    assert releasing.artifact.released_by == SENIOR_ID
    assert releasing.artifact.released_at is not None

    assert approval.event.action == ApprovalAction.APPROVED.value
    assert releasing.event.action == ApprovalAction.RELEASED.value
    assert approval.event is not releasing.event


def test_the_public_api_offers_no_combined_approve_and_release() -> None:
    """R4 keeps them separate so the second one can be refused.

    A convenience wrapper would collapse two audit rows into one and remove the
    only moment at which a frozen payload is re-checked before it leaves.
    """
    combined = [
        name
        for name in sm.__all__
        if "releas" in name.lower() and ("approv" in name.lower() or "and" in name.lower())
    ]
    assert combined == []


def test_release_preserves_the_approval_evidence() -> None:
    """Release does not thaw: the approver, the instant and the hash all stand."""
    approval = approve(pending(), SENIOR)
    final = release(approval.artifact, SENIOR).artifact
    assert final.content_hash == approval.artifact.content_hash
    assert final.approved_by == approval.artifact.approved_by
    assert final.approved_at == approval.artifact.approved_at


def test_release_authority_is_the_approval_authority() -> None:
    """Conservative reading — no persona may release who could not have approved.

    CLAUDE.md names no releaser persona anywhere (R3 says only "an authenticated
    human session"), so this is the narrowest defensible rule rather than an
    invented one. It is carried in the module docstring as an open question.
    """
    with pytest.raises(UnauthorisedApproverError):
        release(approved(), MANAGER)


# --- 5. editing an approved artifact creates a NEW version -------------------


@pytest.mark.parametrize("state", [ArtifactState.APPROVED, ArtifactState.RELEASED])
def test_editing_a_frozen_artifact_creates_a_new_draft_version(state: ArtifactState) -> None:
    """R4, verbatim: a new version in DRAFT requiring fresh approval.

    Note the original is untouched. That is the whole point — the record of what
    was approved survives the correction.
    """
    original = STATE_BUILDERS[state]()
    outcome = new_version(original, SENIOR, payload("14036"))
    superseded = outcome.artifact

    assert superseded.state is ArtifactState.DRAFT
    assert superseded.version == original.version + 1
    assert superseded.artifact_id == original.artifact_id  # same logical artifact
    assert superseded.content_hash is None
    assert superseded.approved_by is None
    assert superseded.released_by is None
    assert superseded.payload["net"] == Decimal("14036")

    assert original.state is state
    assert original.content_hash is not None
    assert outcome.event.action == ApprovalAction.VERSION_CREATED.value


def test_version_creation_is_not_a_transition() -> None:
    """`ALLOWED_TRANSITIONS` has no APPROVED -> DRAFT edge, deliberately.

    If it did, an approval could be reversed in place and the evidence of what
    was approved would be overwritten by the correction.
    """
    assert ArtifactState.DRAFT not in ALLOWED_TRANSITIONS[ArtifactState.APPROVED]
    assert not can_transition(ArtifactState.APPROVED, ArtifactState.DRAFT)
    # ...and yet the version operation succeeds, without going through the edge.
    assert new_version(approved(), SENIOR, payload()).artifact.state is ArtifactState.DRAFT


def test_the_new_version_must_be_approved_again_from_scratch() -> None:
    superseded = new_version(approved(), SENIOR, payload("14036")).artifact
    with pytest.raises(IllegalTransitionError):
        release(superseded, SENIOR)
    with pytest.raises(IllegalTransitionError):
        approve(superseded, SENIOR)

    walked = release(
        approve(submit_for_approval(superseded, SENIOR).artifact, SENIOR).artifact, SENIOR
    ).artifact
    assert walked.state is ArtifactState.RELEASED
    assert walked.version == 2


@pytest.mark.parametrize("state", [ArtifactState.DRAFT, ArtifactState.PENDING_APPROVAL])
def test_new_version_is_refused_for_an_unfrozen_artifact(state: ArtifactState) -> None:
    with pytest.raises(VersioningError):
        new_version(STATE_BUILDERS[state](), SENIOR, payload("1"))


def test_amending_a_draft_keeps_the_version() -> None:
    """A draft has never been approved, so changing it supersedes nothing."""
    outcome = amend_draft(draft(), SENIOR, payload("14036"))
    assert outcome.artifact.state is ArtifactState.DRAFT
    assert outcome.artifact.version == 1
    assert outcome.artifact.payload["net"] == Decimal("14036")
    assert outcome.event.action == ApprovalAction.DRAFT_AMENDED.value


@pytest.mark.parametrize(
    "state", [ArtifactState.PENDING_APPROVAL, ArtifactState.APPROVED, ArtifactState.RELEASED]
)
def test_amend_draft_is_refused_anywhere_but_draft(state: ArtifactState) -> None:
    """Editing under an approver means they approve one thing and another stores."""
    with pytest.raises(VersioningError):
        amend_draft(STATE_BUILDERS[state](), SENIOR, payload("1"))


# --- 6. the audit trail ------------------------------------------------------


def test_every_operation_returns_an_audit_event() -> None:
    """§11: "Every state transition writes an `AuditEvent`: actor, action,
    before, after, at." This layer produces it; the caller persists it."""
    at = dt.datetime(2026, 8, 1, 6, 30, tzinfo=dt.UTC)
    outcome = approve(pending(), SENIOR, at=at)
    event = outcome.event

    assert event.actor_id == SENIOR_ID
    assert event.actor_persona is Persona.SENIOR_MANAGER
    assert event.action == ApprovalAction.APPROVED.value
    # §11 / app.domain.enums: the type's value IS the Postgres table name.
    assert event.entity_table == SHEET.value
    assert event.entity_id == ARTIFACT_ID
    assert event.at == at
    assert event.before is not None and event.after is not None
    assert event.before["state"] == ArtifactState.PENDING_APPROVAL.value
    assert event.after["state"] == ArtifactState.APPROVED.value


def test_the_audit_chain_proves_what_was_approved_was_what_was_submitted() -> None:
    """The submission row's `payload_hash` equals the approval row's freeze.

    That equality is the evidence a dispute needs: nobody swapped the content
    between the moment it was put in front of the approver and the moment they
    approved it.
    """
    submission = submit_for_approval(draft(), SYSTEM)
    approval = approve(submission.artifact, SENIOR)

    assert submission.event.after is not None and approval.event.after is not None
    assert submission.event.after["payload_hash"] == approval.event.after["content_hash"]


def test_audit_snapshots_carry_no_commercials() -> None:
    """Only a fingerprint of the payload, never the payload.

    A remuneration payload is commercials (§4). Copying it into an audit row
    would create a second store of the same figures with its own access rules,
    and the LDE Executive wall would then have two places to hold.
    """
    outcome = approve(pending(), SENIOR)
    for snapshot in (outcome.event.before, outcome.event.after):
        assert snapshot is not None
        assert set(snapshot) == {"state", "version", "payload_hash", "content_hash"}
        assert "14035" not in str(snapshot)


def test_the_full_ladder_produces_three_events_in_order() -> None:
    outcomes: list[ApprovalOutcome] = []
    artifact = draft()
    outcomes.append(submit_for_approval(artifact, SYSTEM))
    outcomes.append(approve(outcomes[-1].artifact, SENIOR))
    outcomes.append(release(outcomes[-1].artifact, SENIOR))

    assert [o.artifact.state for o in outcomes] == [
        ArtifactState.PENDING_APPROVAL,
        ArtifactState.APPROVED,
        ArtifactState.RELEASED,
    ]
    assert [o.event.action for o in outcomes] == [
        ApprovalAction.SUBMITTED.value,
        ApprovalAction.APPROVED.value,
        ApprovalAction.RELEASED.value,
    ]
