"""The approval core — CLAUDE.md R4, "Nothing leaves the system unapproved".

    DRAFT -> PENDING_APPROVAL -> APPROVED -> RELEASED

`state_machine` owns the lifecycle: the four transitions, the two version
operations that are deliberately not transitions, approval authority, and the
`AuditEvent` each one produces. `hashing` owns the freeze: one canonical,
deterministic sha256 over the artifact payload.

Both are pure. Nothing in this package opens a connection, sends anything, or
knows what a remuneration sheet is — it takes an artifact and returns the next
artifact plus the audit row that describes the change. Persistence and release
endpoints live in `app/api/`, behind an authenticated human session (R3).

**There is no `versioning.py`.** §3's layout sketch lists "state machine,
versioning, hashing" for this package, and versioning turned out to be four
functions' worth of rules that share the exception hierarchy and the `Artifact`
record with the state machine — splitting them would have meant a module that
imports its own errors from the module that imports it. Hashing genuinely stands
alone (it has no dependency on the lifecycle at all), so it is its own file.

Typical use, all inside one database transaction at the call site::

    outcome = approve(artifact, Actor(actor_id=user.id, persona=user.role))
    await repo.save(outcome.artifact)
    await audit.write(outcome.event)

    # separately, later, by a separate human action and a separate audit row:
    outcome = release(outcome.artifact, Actor(actor_id=user.id, persona=user.role))
"""

from __future__ import annotations

from app.services.approval.hashing import (
    CANONICALISATION_VERSION,
    HASH_ALGORITHM,
    CanonicalisationError,
    canonical_json,
    canonical_preimage,
    content_hash,
)
from app.services.approval.state_machine import (
    FROZEN_STATES,
    Actor,
    ApprovalAction,
    ApprovalAuthorityUndefinedError,
    ApprovalError,
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

__all__ = [
    "CANONICALISATION_VERSION",
    "FROZEN_STATES",
    "HASH_ALGORITHM",
    "Actor",
    "ApprovalAction",
    "ApprovalAuthorityUndefinedError",
    "ApprovalError",
    "ApprovalOutcome",
    "Artifact",
    "CanonicalisationError",
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
    "canonical_json",
    "canonical_preimage",
    "check_transition",
    "content_hash",
    "new_version",
    "reject",
    "release",
    "submit_for_approval",
    "verify_frozen",
]
