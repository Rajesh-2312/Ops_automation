"""The artifact lifecycle. CLAUDE.md R4 is its specification.

    "Every artifact moves DRAFT -> PENDING_APPROVAL -> APPROVED -> RELEASED.
     Approval freezes and hashes the version. Editing an approved artifact
     creates a new version in DRAFT requiring fresh approval. Approval and
     release are separate actions with separate audit rows."

Four properties this module exists to guarantee, each of which has a test:

1. **Only the legal edges happen.** `ALLOWED_TRANSITIONS` in `app.domain.enums`
   is the whole grammar. An illegal move raises `IllegalTransitionError` — it
   never returns `False`, because a boolean gets ignored at exactly one call site
   eventually and that call site is the one that releases an unapproved payout.
2. **Approval freezes.** `approve()` writes a `content_hash` over the payload,
   and `release()` recomputes it and refuses to proceed if the payload has moved
   since. Without the recheck, "approved" would mean "was approved once, in some
   state nobody can now reconstruct".
3. **Editing an approved artifact is not a transition.** It is `new_version()`,
   which returns a fresh DRAFT at `version + 1` with the freeze cleared. Note
   that `ALLOWED_TRANSITIONS` deliberately has no APPROVED -> DRAFT edge; adding
   one would make an approval reversible in place and lose the record of what was
   approved.
4. **Approval and release are separate calls.** There is no `approve_and_
   release()` and there must never be one. Two actions, two audit rows, and the
   opportunity for the second to be refused.

PURITY, AND WHY NOTHING IS WRITTEN HERE
=======================================
Like `app.services.remuneration.engine` and `.validators`, this layer takes data
and returns data. Each operation returns an `ApprovalOutcome` carrying the NEW
artifact and the `AuditEvent` that describes the change; persisting both, in one
database transaction, is the caller's job. That separation is what makes every
rule below testable without a database (§3), and it means the audit row is
constructed by the same code that decided the transition was legal rather than by
whoever remembered to log afterwards (§11: "Every state transition writes an
`AuditEvent`").

WHO MAY DO WHAT
===============
Approval authority comes from `APPROVAL_AUTHORITY`, which today defines only
`REMUNERATION_SHEET -> {SENIOR_MANAGER}` (§4: "payout approval" sits with the
Senior Manager). The other artifact types are **absent on purpose** — §14 Q3,
"Approval authority for college-facing comms: Manager or Senior Manager?", is an
open question, and §14 says "Carry these; do not invent answers". So
`approver_personas()` raises `ApprovalAuthorityUndefinedError` for a type with no
entry. The failure is loud, at the approval attempt, naming the question. Filling
in a plausible default would convert an open governance question into a silent
answer that nobody ever revisits.

**Release authority is read as approval authority** — a deliberately conservative
choice, also open. R3 requires release to be "an authenticated human session" and
R4 keeps release distinct from approval, but no persona is named for it anywhere
in CLAUDE.md. Reusing the approval set can never permit somebody who could not
have approved in the first place; picking anything wider would be an invention.
If byteXL wants a distinct releaser (Finance & Accounts is an actor, not a
persona), that is a second authority table and an answer to Q3, not a widening of
this one. `approved_by` and `released_by` are recorded separately so a four-eyes
rule can be added later without a data migration.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Final
from uuid import UUID

from pydantic import JsonValue

from app.core.audit import AuditEvent
from app.domain.enums import (
    ALLOWED_TRANSITIONS,
    APPROVAL_AUTHORITY,
    ArtifactState,
    ArtifactType,
    Persona,
)
from app.services.approval.hashing import content_hash

__all__ = [
    "FROZEN_STATES",
    "Actor",
    "ApprovalAction",
    "ApprovalAuthorityUndefinedError",
    "ApprovalError",
    "ApprovalOutcome",
    "Artifact",
    "FrozenContentMismatchError",
    "HumanActorRequiredError",
    "IllegalTransitionError",
    "MissingRejectionReasonError",
    "TerminalArtifactError",
    "UnauthorisedApproverError",
    "VersioningError",
    "amend_draft",
    "approve",
    "approver_personas",
    "can_transition",
    "check_transition",
    "new_version",
    "reject",
    "release",
    "submit_for_approval",
    "verify_frozen",
]


class ApprovalAction(StrEnum):
    """The audit vocabulary of the R4 lifecycle.

    Lives here rather than in `app/domain/enums.py` for the reason
    `app.core.audit.AuditAction` gives: `AuditEvent.action` is typed `str`
    precisely so a workstream can name its own actions without editing a shared
    file, and the `audit_events` table that would fix this column's SQL type is a
    Phase 2 migration owned by another agent. This is an audit *label*, not a
    stored status column — the status enum is `ArtifactState`, and that one does
    live in `domain/` as §11 requires.

    Six actions, not four: version creation and draft amendment are not
    transitions, but they change what would be approved, so they are audited.
    """

    SUBMITTED = "artifact.submitted"
    APPROVED = "artifact.approved"
    REJECTED = "artifact.rejected"
    RELEASED = "artifact.released"
    VERSION_CREATED = "artifact.version_created"
    DRAFT_AMENDED = "artifact.draft_amended"


#: States in which the content is frozen and a `content_hash` must be present.
#: R4: approval freezes; release does not thaw.
FROZEN_STATES: Final[frozenset[ArtifactState]] = frozenset(
    {ArtifactState.APPROVED, ArtifactState.RELEASED}
)


# --- errors ------------------------------------------------------------------


class ApprovalError(Exception):
    """Base for every refusal in this module.

    A plain `Exception` subclass, not `ValueError`: these are governance
    refusals, and a broad `except ValueError` written around form parsing must
    not be able to swallow "this artifact was released without approval".
    """


class IllegalTransitionError(ApprovalError):
    """A move that is not in `ALLOWED_TRANSITIONS`."""

    def __init__(self, current: ArtifactState, target: ArtifactState) -> None:
        allowed = sorted(s.value for s in ALLOWED_TRANSITIONS.get(current, frozenset()))
        super().__init__(
            f"{current.value} -> {target.value} is not a legal artifact transition "
            f"(from {current.value}, only: {', '.join(allowed) or 'nothing — terminal'}). "
            "CLAUDE.md R4: DRAFT -> PENDING_APPROVAL -> APPROVED -> RELEASED."
        )
        self.current = current
        self.target = target


class TerminalArtifactError(IllegalTransitionError):
    """A move out of RELEASED, which is terminal.

    Subclasses `IllegalTransitionError` so a caller can catch either, while the
    distinct type says "this is a dead end", not "you took the wrong edge". A
    released artifact is superseded by a new version, never re-opened.
    """


class ApprovalAuthorityUndefinedError(ApprovalError):
    """No persona has been given approval authority for this artifact type.

    This is the correct failure for an unanswered governance question (§14 Q3),
    not a bug to be patched by adding a plausible entry to `APPROVAL_AUTHORITY`.
    """

    def __init__(self, artifact_type: ArtifactType) -> None:
        super().__init__(
            f"No approval authority is defined for artifact type "
            f"'{artifact_type.value}'. CLAUDE.md §14 Q3 — 'Approval authority for "
            "college-facing comms: Manager or Senior Manager?' — is an open question, "
            "and §14 says carry it, do not invent an answer. Nothing of this type can "
            "be approved or released until APPROVAL_AUTHORITY in app/domain/enums.py "
            "records a decision made by a human, not by a test that needed to pass."
        )
        self.artifact_type = artifact_type


class UnauthorisedApproverError(ApprovalError):
    """The actor's persona is not in the artifact type's authority set."""

    def __init__(
        self, artifact_type: ArtifactType, persona: Persona, permitted: frozenset[Persona]
    ) -> None:
        names = ", ".join(sorted(p.value for p in permitted))
        super().__init__(
            f"Persona '{persona.value}' may not approve or release "
            f"'{artifact_type.value}' — permitted: {names}."
        )
        self.artifact_type = artifact_type
        self.persona = persona
        self.permitted = permitted


class HumanActorRequiredError(ApprovalError):
    """An approval, rejection or release was attempted with no human behind it.

    CLAUDE.md R3: "Release endpoints require an authenticated human session."
    §8's autonomy ladder puts anything touching money at level 3 at most, so a
    scheduled job or an agent may prepare and submit, and may never approve.
    """

    def __init__(self, action: ApprovalAction) -> None:
        super().__init__(
            f"'{action.value}' requires an authenticated human actor: actor_id is None. "
            "CLAUDE.md R3 — release endpoints require an authenticated human session, "
            "and nothing touching money goes past autonomy level 3 (§8)."
        )
        self.action = action


class FrozenContentMismatchError(ApprovalError):
    """The payload no longer matches the hash taken at approval.

    Either the artifact was edited after approval — which R4 forbids; edits make
    a new version — or the stored hash belongs to a different row. Both are
    release-blocking, and neither is recoverable by recomputing the hash.
    """

    def __init__(self, artifact_id: UUID, version: int, stored: str, recomputed: str) -> None:
        super().__init__(
            f"Artifact {artifact_id} v{version} was frozen as {stored} but its payload "
            f"now hashes to {recomputed}. It was edited after approval. CLAUDE.md R4: "
            "editing an approved artifact creates a NEW version in DRAFT requiring fresh "
            "approval — it does not amend the approved one."
        )
        self.artifact_id = artifact_id
        self.version = version
        self.stored = stored
        self.recomputed = recomputed


class MissingRejectionReasonError(ApprovalError):
    """A rejection with no stated reason.

    The same standard `ValidationReport.can_submit()` holds a §7 warning override
    to: a blank string is how a required field becomes a formality. The person
    who has to fix the artifact needs to know what was wrong with it.
    """


class VersioningError(ApprovalError):
    """A version operation attempted from a state where it makes no sense."""


# --- data --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Actor:
    """Who is acting. `actor_id` is `None` only for a scheduled job or an agent.

    Mirrors `AuditEvent.actor_id` / `actor_persona`. Persona is required and
    `actor_id` has no default: a caller with no human behind the action must say
    so explicitly, because that is the case `HumanActorRequiredError` turns on.
    """

    actor_id: UUID | None
    persona: Persona


@dataclass(frozen=True, slots=True)
class Artifact:
    """One version of one artifact moving through the R4 lifecycle.

    Immutable. Every operation returns a new instance, so the pre-transition
    value is still in the caller's hand for the audit row and nothing can be
    half-transitioned by an exception thrown midway.

    `payload` is the content that gets frozen — the remuneration sheet rows, the
    report body, the document fields — canonicalised by
    `app.services.approval.hashing`. It is copied into a `MappingProxyType` at
    construction so the top level cannot be mutated behind the freeze; deep
    immutability is guaranteed by the hash and the `release()` recheck, not by
    the type. That is the honest guarantee: a nested list can be mutated, and
    `verify_frozen()` will catch it.

    The `content_hash` invariant is enforced here rather than trusted: a frozen
    state without a hash is an artifact claiming an approval it cannot evidence,
    and an unfrozen state carrying one implies a freeze that never happened.
    """

    artifact_type: ArtifactType
    artifact_id: UUID
    #: Version of this artifact, from 1. The database key is
    #: `(artifact_id, version)` — a new version is a new row, never an update.
    version: int
    state: ArtifactState
    #: Accepts any mapping; stored as a `MappingProxyType` copy (see above).
    payload: Mapping[str, object]

    #: sha256 over the canonical payload, written by `approve()`. `None` until
    #: the freeze, and cleared by `new_version()`.
    content_hash: str | None = None

    approved_by: UUID | None = None
    approved_at: dt.datetime | None = None
    released_by: UUID | None = None
    released_at: dt.datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))
        if self.version < 1:
            raise ValueError(f"Artifact.version starts at 1, got {self.version}")
        frozen = self.state in FROZEN_STATES
        if frozen and not self.content_hash:
            raise ValueError(
                f"{self.state.value} artifact has no content_hash — CLAUDE.md R4: "
                "approval freezes and hashes the version. An approval that cannot be "
                "evidenced is not an approval."
            )
        if not frozen and self.content_hash:
            raise ValueError(
                f"{self.state.value} artifact carries a content_hash. Only APPROVED and "
                "RELEASED versions are frozen; a hash here would imply a freeze that "
                "never happened."
            )

    @property
    def is_frozen(self) -> bool:
        """True once approval has hashed this version (R4)."""
        return self.state in FROZEN_STATES

    def payload_hash(self) -> str:
        """Hash of the CURRENT payload, whatever state the artifact is in.

        For a frozen artifact this must equal `content_hash`; `verify_frozen()`
        is the check that says so.
        """
        return content_hash(self.payload)


@dataclass(frozen=True, slots=True)
class ApprovalOutcome:
    """The result of one lifecycle operation: the new artifact and its audit row.

    Both, together, deliberately. §11 requires an `AuditEvent` for every state
    transition, and returning the pair makes it impossible to persist the new
    state while forgetting the evidence — the caller writes both in one
    transaction or neither.
    """

    artifact: Artifact
    event: AuditEvent


# --- authority and transition checks -----------------------------------------


def approver_personas(artifact_type: ArtifactType) -> frozenset[Persona]:
    """Personas that may approve this artifact type.

    Raises `ApprovalAuthorityUndefinedError` when `APPROVAL_AUTHORITY` has no
    entry — the deliberate behaviour described in `app.domain.enums` and in this
    module's docstring. Do not "fix" a raise here by adding an entry there; fix
    it by answering §14 Q3.

    An entry that is present but empty raises too. "Nobody may approve this" is
    not a decision somebody made; it is an incomplete one, and it should fail the
    same way.
    """
    permitted = APPROVAL_AUTHORITY.get(artifact_type)
    if not permitted:
        raise ApprovalAuthorityUndefinedError(artifact_type)
    return permitted


def can_transition(current: ArtifactState, target: ArtifactState) -> bool:
    """Whether `current -> target` is one of the R4 edges.

    A predicate for rendering a button, nothing more. The gate that decides is
    `check_transition()`; a caller that asks this and then acts without it has
    put the decision in the UI, where it can be bypassed with a curl.
    """
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


def check_transition(current: ArtifactState, target: ArtifactState) -> None:
    """Raise unless `current -> target` is legal. Returns `None` on success.

    Never returns a boolean. A rejected transition that is merely falsy gets
    dropped by one `if` somebody forgot to write, and the failure mode of that
    omission is an unapproved artifact leaving the system (R4).
    """
    if current is ArtifactState.RELEASED:
        raise TerminalArtifactError(current, target)
    if not can_transition(current, target):
        raise IllegalTransitionError(current, target)


def verify_frozen(artifact: Artifact) -> None:
    """Raise unless the frozen payload still hashes to the stored `content_hash`.

    Called by `release()`, and safe to call from a monitor or a nightly integrity
    job: it is pure and reads nothing. This is the check that gives R4's freeze
    teeth — without it, "APPROVED" would be a column value rather than a claim
    about content.
    """
    if not artifact.is_frozen or artifact.content_hash is None:
        return
    recomputed = artifact.payload_hash()
    if recomputed != artifact.content_hash:
        raise FrozenContentMismatchError(
            artifact.artifact_id, artifact.version, artifact.content_hash, recomputed
        )


# --- the four transitions ----------------------------------------------------


def submit_for_approval(
    artifact: Artifact, actor: Actor, at: dt.datetime | None = None
) -> ApprovalOutcome:
    """DRAFT -> PENDING_APPROVAL.

    No persona check and no human requirement: preparing work for a human to
    approve is exactly what an agent is allowed to do (§8, autonomy level 2 —
    "propose, human edits and sends"), and a scheduled payout run submits a whole
    cycle with nobody at the keyboard. Reach is enforced by RLS in the database
    (§4), not re-implemented here.

    Whether the artifact is *fit* to be submitted is a different question, owned
    by the domain that produced it — for a payout,
    `ValidationReport.can_submit()` in `app.services.remuneration.validators`
    (§7: blocking gates mean "the cycle cannot reach PENDING_APPROVAL"). This
    module governs the lifecycle, not the content.
    """
    check_transition(artifact.state, ArtifactState.PENDING_APPROVAL)
    return _outcome(
        artifact,
        replace(artifact, state=ArtifactState.PENDING_APPROVAL),
        ApprovalAction.SUBMITTED,
        actor,
        at,
    )


def approve(artifact: Artifact, actor: Actor, at: dt.datetime | None = None) -> ApprovalOutcome:
    """PENDING_APPROVAL -> APPROVED. Freezes and hashes the version (R4).

    Three checks, in this order, so the most fundamental refusal is the one the
    caller sees: is the edge legal, is there a human, may this persona approve
    this type. The hash is taken last, over the payload as it stands at this
    instant — that digest is what `release()` will insist on later.

    Approval does NOT release. There is no `send`, no queue push, no side effect
    of any kind here; the artifact is merely releasable now, by a separate call
    that writes a separate audit row (R4).
    """
    check_transition(artifact.state, ArtifactState.APPROVED)
    _require_human(actor, ApprovalAction.APPROVED)
    _require_authority(artifact.artifact_type, actor)

    approved = replace(
        artifact,
        state=ArtifactState.APPROVED,
        content_hash=artifact.payload_hash(),
        approved_by=actor.actor_id,
        approved_at=_stamp(at),
    )
    return _outcome(artifact, approved, ApprovalAction.APPROVED, actor, at)


def reject(
    artifact: Artifact, actor: Actor, reason: str, at: dt.datetime | None = None
) -> ApprovalOutcome:
    """PENDING_APPROVAL -> DRAFT. The rejection edge R4 needs to be usable.

    Requires the same authority as approval: the power to withhold approval is
    the power to approve, and letting an actor who could not have approved send
    an artifact back would be a denial-of-approval with no accountability.

    A non-blank `reason` is mandatory and is recorded on the audit row. The draft
    goes back to whoever has to fix it, and "rejected" with no reason turns one
    review cycle into three.
    """
    check_transition(artifact.state, ArtifactState.DRAFT)
    if not reason.strip():
        raise MissingRejectionReasonError(
            "A rejection needs a stated reason: the artifact goes back to its author, "
            "and a blank reason makes a required field a formality."
        )
    _require_human(actor, ApprovalAction.REJECTED)
    _require_authority(artifact.artifact_type, actor)

    return _outcome(
        artifact,
        replace(artifact, state=ArtifactState.DRAFT),
        ApprovalAction.REJECTED,
        actor,
        at,
        detail={"reason": reason.strip()},
    )


def release(artifact: Artifact, actor: Actor, at: dt.datetime | None = None) -> ApprovalOutcome:
    """APPROVED -> RELEASED. The only door out of the system, and it is terminal.

    `verify_frozen()` runs first: releasing an artifact whose payload has drifted
    from its approved hash is precisely the event R4 exists to prevent, and it is
    the one moment where the drift is still cheap to catch.

    R3 makes this a human-only action, and no agent toolset may bind a function
    that calls it — there is no `mark_released` tool, and the test in §12 asserts
    there never is. Releasing does not send anything either: the Comms service
    owns the outbound queue (§8), and it may only act on a RELEASED artifact.
    """
    check_transition(artifact.state, ArtifactState.RELEASED)
    _require_human(actor, ApprovalAction.RELEASED)
    _require_authority(artifact.artifact_type, actor)
    verify_frozen(artifact)

    released = replace(
        artifact,
        state=ArtifactState.RELEASED,
        released_by=actor.actor_id,
        released_at=_stamp(at),
    )
    return _outcome(artifact, released, ApprovalAction.RELEASED, actor, at)


# --- versioning: an edit is not a transition ---------------------------------


def new_version(
    artifact: Artifact, actor: Actor, payload: Mapping[str, object], at: dt.datetime | None = None
) -> ApprovalOutcome:
    """Supersede a frozen artifact with a fresh DRAFT at `version + 1`.

    R4: "Editing an approved artifact creates a new version in DRAFT requiring
    fresh approval." This is NOT a transition and `check_transition()` is not
    called — `ALLOWED_TRANSITIONS` has no APPROVED -> DRAFT edge, deliberately.
    Treating an edit as a transition would move the approved row backwards and
    destroy the record of what was approved; here the approved version stays
    exactly as it was and a new row is born beside it.

    The new version keeps `artifact_id` (it is the same logical artifact) and
    carries none of the freeze: no `content_hash`, no approver, no releaser. It
    starts at DRAFT and walks the whole ladder again.

    Only legal from a frozen state. Editing a DRAFT is `amend_draft()`, and
    editing something in PENDING_APPROVAL is refused — see that function.
    """
    if not artifact.is_frozen:
        raise VersioningError(
            f"new_version() supersedes a frozen artifact; this one is "
            f"{artifact.state.value}. Use amend_draft() for a DRAFT, or reject() an "
            "artifact awaiting approval before changing it."
        )
    superseded = Artifact(
        artifact_type=artifact.artifact_type,
        artifact_id=artifact.artifact_id,
        version=artifact.version + 1,
        state=ArtifactState.DRAFT,
        payload=payload,
    )
    return _outcome(artifact, superseded, ApprovalAction.VERSION_CREATED, actor, at)


def amend_draft(
    artifact: Artifact, actor: Actor, payload: Mapping[str, object], at: dt.datetime | None = None
) -> ApprovalOutcome:
    """Replace the payload of a DRAFT in place. Same version, still DRAFT.

    A draft has never been approved, so changing it supersedes nothing and
    minting a version for every keystroke would bury the versions that matter —
    the ones somebody approved — in noise. It is still audited: what was in the
    draft at submission time is what the approver will be asked about.

    Refused for PENDING_APPROVAL. An artifact awaiting approval is in front of a
    human; editing it underneath them means they approve one thing and something
    else is stored. Reject it back to DRAFT first — that is what the rejection
    edge is for, and it leaves an audit row saying so.
    """
    if artifact.state is not ArtifactState.DRAFT:
        raise VersioningError(
            f"amend_draft() edits a DRAFT; this artifact is {artifact.state.value}. "
            "An artifact awaiting approval must be rejected back to DRAFT first, and a "
            "frozen one is superseded with new_version() (CLAUDE.md R4)."
        )
    amended = Artifact(
        artifact_type=artifact.artifact_type,
        artifact_id=artifact.artifact_id,
        version=artifact.version,
        state=ArtifactState.DRAFT,
        payload=payload,
    )
    return _outcome(artifact, amended, ApprovalAction.DRAFT_AMENDED, actor, at)


# --- internals ---------------------------------------------------------------


def _require_human(actor: Actor, action: ApprovalAction) -> None:
    if actor.actor_id is None:
        raise HumanActorRequiredError(action)


def _require_authority(artifact_type: ArtifactType, actor: Actor) -> None:
    permitted = approver_personas(artifact_type)
    if actor.persona not in permitted:
        raise UnauthorisedApproverError(artifact_type, actor.persona, permitted)


def _stamp(at: dt.datetime | None) -> dt.datetime:
    """The instant of the action, UTC (§11). Explicit `at` wins, for replay."""
    return at if at is not None else dt.datetime.now(dt.UTC)


def _snapshot(artifact: Artifact) -> dict[str, JsonValue]:
    """The audit row's before/after view of an artifact.

    The payload itself is NOT copied in — it can be large, and a remuneration
    payload is commercials that would then exist in a second table with its own
    access rules (§4). `payload_hash` fingerprints it instead, which is enough to
    prove what changed and enough to prove what did not: the `payload_hash` on
    the submission row equals the `content_hash` on the approval row exactly when
    the thing approved is the thing submitted.
    """
    return {
        "state": artifact.state.value,
        "version": artifact.version,
        "payload_hash": artifact.payload_hash(),
        "content_hash": artifact.content_hash,
    }


def _outcome(
    before: Artifact,
    after: Artifact,
    action: ApprovalAction,
    actor: Actor,
    at: dt.datetime | None,
    detail: dict[str, JsonValue] | None = None,
) -> ApprovalOutcome:
    """Pair the new artifact with its audit event.

    `entity_table` is `ArtifactType`'s value, which `app.domain.enums` documents
    as being the Postgres table name for exactly this reason — §11 wants the row
    findable again without guessing which "id" was meant.
    """
    after_snapshot = _snapshot(after)
    if detail:
        after_snapshot |= detail
    event = AuditEvent(
        actor_id=actor.actor_id,
        actor_persona=actor.persona,
        action=action.value,
        entity_table=after.artifact_type.value,
        entity_id=after.artifact_id,
        before=_snapshot(before),
        after=after_snapshot,
        at=_stamp(at),
    )
    return ApprovalOutcome(artifact=after, event=event)
