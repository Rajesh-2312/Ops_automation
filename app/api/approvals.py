"""Approval endpoints — the HTTP surface of CLAUDE.md R4.

    POST /approvals/{artifact_type}/{artifact_id}/submit
    POST /approvals/{artifact_type}/{artifact_id}/approve
    POST /approvals/{artifact_type}/{artifact_id}/reject
    POST /approvals/{artifact_type}/{artifact_id}/release
    GET  /approvals/{artifact_type}/{artifact_id}/versions

The lifecycle itself is not implemented here and must not be. `app/services/
approval/` owns the grammar (`ALLOWED_TRANSITIONS`), the authority table, the
freeze and the `AuditEvent` each transition produces; it is pure and writes
nothing. This module is the I/O, the authorisation and the persistence — it
takes the `ApprovalOutcome` the state machine returns and lands both halves of
it, the row and the audit event, in ONE transaction.

R4 — APPROVAL AND RELEASE ARE TWO ENDPOINTS, NOT ONE
====================================================
"Approval and release are separate actions with separate audit rows." There is
no combined route, no `?and_release=true`, and no handler that calls both
`approve()` and `release()`. `POST .../approve` freezes the version and stops;
releasing is a second authenticated request by a human who could have refused.
Each writes its own `audit_events` row through `write_within()`, so the two acts
are separately attributable months later, which is the only question anybody
asks during a dispute.

Release does NOT transmit anything. It marks state. There is no email, no
WhatsApp, no queue push in this file, and per R3 no agent path reaches these
routes: every handler takes `CurrentPrincipal`, which is an authenticated human
session resolved from a Supabase JWT and a `profiles` row.

WHERE THE PAYLOAD COMES FROM, AND WHY THE HASH CAN FAIL AT RELEASE
==================================================================
`artifact_versions` deliberately does not hold the artifact — migration 1300 says
so: `remuneration_sheets`, `governance_reports` and `program_documents` remain
the systems of record for content. So the payload that gets frozen is READ BACK
from the system of record on every call, by `_payload()` below, and never taken
from the request body. Two consequences, both intended:

* Nothing a caller sends can change what is hashed. A release request carries no
  content at all.
* If the source row is edited after approval, the payload recomputed at release
  no longer matches the `content_hash` frozen at approval, and `verify_frozen()`
  refuses with 409. That is R4's freeze doing its job — the edit should have
  created a new version in DRAFT.

R5 — THE COMMERCIALS WALL, IN CODE
==================================
We are on a `BYPASSRLS` service-role connection, so the two policies migration
1300 defines on `artifact_versions` never run for us. Every handler therefore
reproduces them, using the same two predicates and keeping them separate the way
the SQL does:

    can_reach_artifact()      -> `_reach()`   — the program the artifact hangs off
    artifact_is_commercial()  -> `_is_commercial()`

    commercial row : can_see_commercials() AND can_reach_artifact()
    other row      : is_internal()         AND can_reach_artifact()

`_is_commercial()` mirrors 1300 exactly, including the fail-closed default:
`remuneration_sheets` always, `governance_reports` never, `program_documents` by
category. A remuneration artifact is refused to an LDE Executive before any row
is read, so the endpoint cannot even confirm the artifact exists.

§14 Q3 IS OPEN, AND THIS FILE SAYS SO OUT LOUD
==============================================
`APPROVAL_AUTHORITY` names an approver for `remuneration_sheets` only. For the
other two types `approver_personas()` raises `ApprovalAuthorityUndefinedError`,
and that is surfaced here as **501 Not Implemented** carrying the exception's own
message, which names the question. 501 rather than 403: the caller is not
forbidden, the system has no answer yet, and a 403 would send a Senior Manager
looking for a permission that does not exist. Do not "fix" one of these 501s by
adding an entry to `app/domain/enums.py`; fix it by answering Q3.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Final, TypeAlias
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import AuditWriter, get_audit_writer
from app.core.security import (
    CurrentPrincipal,
    Principal,
    require_college_reach,
    require_commercials,
    require_internal,
)
from app.db.models import (
    COMMERCIAL_DOCUMENT_CATEGORIES,
    ArtifactVersion,
    GovernanceReport,
    Program,
    ProgramDocument,
    RemunerationSheet,
)
from app.db.session import get_session
from app.domain.enums import ArtifactState, ArtifactType
from app.services.approval import (
    Actor,
    ApprovalAuthorityUndefinedError,
    ApprovalOutcome,
    Artifact,
    CanonicalisationError,
    FrozenContentMismatchError,
    HumanActorRequiredError,
    IllegalTransitionError,
    MissingRejectionReasonError,
    UnauthorisedApproverError,
    approve,
    reject,
    release,
    submit_for_approval,
)

router = APIRouter(prefix="/approvals", tags=["approvals"])

Session = Annotated[AsyncSession, Depends(get_session)]
Audit = Annotated[AuditWriter, Depends(get_audit_writer)]
ArtifactTypePath = Annotated[ArtifactType, Path(description="Artifact table name")]
ArtifactIdPath = Annotated[UUID, Path(description="Row id in that table")]

#: The three systems of record R4 governs. `artifact_versions` holds the
#: lifecycle; these tables hold the content that gets hashed (migration 1300).
SourceRow: TypeAlias = RemunerationSheet | GovernanceReport | ProgramDocument

#: Status for an artifact type whose approval authority nobody has decided yet
#: (§14 Q3). Not 403 — see the module docstring.
AUTHORITY_UNDEFINED_STATUS: Final[int] = status.HTTP_501_NOT_IMPLEMENTED


# --- wire models --------------------------------------------------------------


class RejectionRequest(BaseModel):
    """A rejection, with the reason that makes it actionable.

    `min_length` plus a whitespace check: the state machine already refuses a
    blank reason, and refusing it here too turns a 500-shaped service exception
    into a 422 naming the field. Both are kept — the model guards the wire, the
    state machine guards every other caller.
    """

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(
        min_length=1,
        description=(
            "Why the artifact is being sent back. Recorded on the audit row and "
            "on the version. A blank reason turns one review cycle into three."
        ),
    )


class ReleaseRequest(BaseModel):
    """Optional commentary attached to a release. Never content.

    `notes` is the one column migration 1300 leaves mutable after approval, and
    for the reason given there: a release note is commentary ABOUT the version,
    not part of it. It is not hashed and cannot alter what was approved.
    """

    model_config = ConfigDict(extra="forbid")

    notes: str | None = Field(default=None, description="Release note. Not part of the freeze.")


class ArtifactVersionOut(BaseModel):
    """One `artifact_versions` row on the wire.

    The payload is NOT included, at any state. It is the artifact's content and
    it lives in the system of record with its own access rules; echoing it
    through the lifecycle API would create a second commercial surface with a
    different policy. `content_hash` fingerprints it instead, which is what a
    dispute actually needs.
    """

    model_config = ConfigDict(frozen=True)

    id: UUID
    artifact_type: ArtifactType
    artifact_id: UUID
    version: int = Field(ge=1)
    state: ArtifactState
    content_hash: str | None = Field(
        default=None, description="Frozen at approval (R4). NULL before it."
    )
    is_current: bool = Field(description="`superseded_at is null` — exactly one per artifact.")

    created_by: UUID | None = None
    created_at: dt.datetime
    submitted_by: UUID | None = None
    submitted_at: dt.datetime | None = None
    approved_by: UUID | None = None
    approved_at: dt.datetime | None = None
    released_by: UUID | None = None
    released_at: dt.datetime | None = None
    superseded_at: dt.datetime | None = None
    notes: str | None = None

    @classmethod
    def of(cls, row: ArtifactVersion) -> ArtifactVersionOut:
        return cls(
            id=row.id,
            artifact_type=row.artifact_type,
            artifact_id=row.artifact_id,
            version=row.version,
            state=row.state,
            content_hash=row.content_hash,
            is_current=row.superseded_at is None,
            created_by=row.created_by,
            created_at=row.created_at,
            submitted_by=row.submitted_by,
            submitted_at=row.submitted_at,
            approved_by=row.approved_by,
            approved_at=row.approved_at,
            released_by=row.released_by,
            released_at=row.released_at,
            superseded_at=row.superseded_at,
            notes=row.notes,
        )


class VersionHistory(BaseModel):
    """Every version of one artifact, oldest first.

    Oldest first because this is read as a narrative — drafted, submitted,
    rejected, resubmitted, approved, released — and a reversed list makes the
    reader assemble the story backwards.
    """

    model_config = ConfigDict(frozen=True)

    artifact_type: ArtifactType
    artifact_id: UUID
    versions: tuple[ArtifactVersionOut, ...]


# --- authorisation: the app-side mirror of 1300's two predicates ---------------


def _forbidden(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


async def _source_row(
    session: AsyncSession, artifact_type: ArtifactType, artifact_id: UUID
) -> SourceRow:
    """The system-of-record row this lifecycle governs, or 404.

    `ArtifactType`'s values are the table names precisely so this stays a lookup
    rather than a branch that can drift (`app/domain/enums.py`).
    """
    row: SourceRow | None
    if artifact_type is ArtifactType.REMUNERATION_SHEET:
        row = await session.get(RemunerationSheet, artifact_id)
    elif artifact_type is ArtifactType.GOVERNANCE_REPORT:
        row = await session.get(GovernanceReport, artifact_id)
    else:
        row = await session.get(ProgramDocument, artifact_id)
    if row is None:
        raise _not_found(f"No {artifact_type.value} row with id {artifact_id}")
    return row


def _is_commercial(
    artifact_type: ArtifactType,
    row: SourceRow,
) -> bool:
    """Mirror of `public.artifact_is_commercial()` (migration 1300).

    Remuneration always — it is the payout. A governance report never; 0700 is
    explicit that it is delivery reporting rather than a commercial table. A
    program document by CATEGORY, the same split as 1000 and as
    `app/api/programs.py`, because a filed remuneration row is not an amount but
    it is a live pointer to one.
    """
    if isinstance(row, ProgramDocument):
        return row.category in COMMERCIAL_DOCUMENT_CATEGORIES
    return artifact_type is ArtifactType.REMUNERATION_SHEET


def _program_id(row: SourceRow) -> UUID:
    """All three artifact types are program-scoped, which is what makes 1300's
    single `can_reach_artifact()` — and this single function — possible."""
    return row.program_id


async def _authorised_artifact(
    session: AsyncSession, principal: Principal, artifact_type: ArtifactType, artifact_id: UUID
) -> SourceRow:
    """Persona, then existence, then wall, then reach. Refuse otherwise.

    The ordering is `programs.py`'s and it is deliberate. `require_internal()`
    runs before any query, so a trainer or a college login probing artifact ids
    gets the same 403 for every id. A `remuneration_sheets` artifact is known to
    be commercial from its TYPE alone, so the wall also closes before the first
    query — an LDE Executive cannot even learn that the sheet exists, which on a
    BYPASSRLS connection is the whole protection.

    Both conjuncts are then applied, as every policy in 1300 and 0700 writes
    them: the wall alone would let a Manager act on another cluster's payout, the
    reach alone would let an LDE Executive act on a rate.
    """
    require_internal(principal)
    if artifact_type is ArtifactType.REMUNERATION_SHEET:
        require_commercials(principal)

    row = await _source_row(session, artifact_type, artifact_id)
    if _is_commercial(artifact_type, row):
        require_commercials(principal)
    require_college_reach(principal, await _college_id(session, _program_id(row)))
    return row


async def _college_id(session: AsyncSession, program_id: UUID) -> UUID:
    """The college reach is resolved against — `programs.college_id`, as in SQL."""
    program = await session.get(Program, program_id)
    if program is None:
        raise _not_found("Artifact is not attached to a program")
    return program.college_id


# --- the payload that gets frozen ---------------------------------------------


def _payload(
    artifact_type: ArtifactType,
    row: SourceRow,
) -> dict[str, object]:
    """The content this artifact's freeze covers, read from the system of record.

    Never from a request body. What is hashed at approval must be what the
    approver was shown, and a caller who could supply it could approve one thing
    and release another.

    Only CONTENT is included — the figures, the identifiers, the dates. Lifecycle
    columns (`payout_status`, `filed_at`, `shared_with_college_at`) are excluded
    deliberately: they change as the artifact moves through the world, and
    hashing them would make every downstream status update look like tampering
    at release time. `Decimal` goes in as `Decimal`; `app.services.approval.
    hashing` renders it losslessly and refuses a float outright (R7).
    """
    if isinstance(row, RemunerationSheet):
        return {
            "trainer_id": row.trainer_id,
            "program_id": row.program_id,
            "work_order_id": row.work_order_id,
            "period_start": row.period_start,
            "period_end": row.period_end,
            "rate": row.rate,
            "rate_basis": row.rate_basis,
            "payable_days": row.payable_days,
            "days_in_month": row.days_in_month,
            "earned": row.earned,
            "ta_da": row.ta_da,
            "accommodation": row.accommodation,
            "travel_reimb": row.travel_reimb,
            "gross": row.gross,
            "tds_rate": row.tds_rate,
            "tds": row.tds,
            "deductions": row.deductions,
            "net_amount": row.net_amount,
            "amount_in_words": row.amount_in_words,
            "currency": row.currency,
            "invoice_no": row.invoice_no,
            "invoice_pan": row.invoice_pan,
        }
    if isinstance(row, GovernanceReport):
        return {
            "program_id": row.program_id,
            "title": row.title,
            "url": row.url,
            "reporting_period_start": row.reporting_period_start,
            "reporting_period_end": row.reporting_period_end,
        }
    return {
        "program_id": row.program_id,
        "document_template_id": row.document_template_id,
        "category": row.category,
        "name": row.name,
        "url": row.url,
        "due_date": row.due_date,
    }


def _artifact(row: ArtifactVersion, payload: dict[str, object]) -> Artifact:
    """The pure `Artifact` the state machine operates on, from one stored row.

    `Artifact.__post_init__` enforces the same biconditional the database does —
    a frozen state carries a hash, an unfrozen one does not. A row that satisfies
    `artifact_versions_approved_ck` always satisfies it, so a `ValueError` here
    means the two invariants have diverged, which is a 500 and not a 4xx: the
    caller did nothing wrong and there is nothing they can change.
    """
    try:
        return Artifact(
            artifact_type=row.artifact_type,
            artifact_id=row.artifact_id,
            version=row.version,
            state=row.state,
            payload=payload,
            content_hash=row.content_hash,
            approved_by=row.approved_by,
            approved_at=row.approved_at,
            released_by=row.released_by,
            released_at=row.released_at,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                f"artifact_versions row {row.id} is internally inconsistent and cannot be "
                f"loaded into the R4 state machine: {exc}"
            ),
        ) from exc


# --- persistence --------------------------------------------------------------


async def _current_version(
    session: AsyncSession, artifact_type: ArtifactType, artifact_id: UUID
) -> ArtifactVersion | None:
    """The live version — `superseded_at is null`, of which there is exactly one.

    That uniqueness is a partial unique index in 1300, not an assumption here.
    """
    rows = (
        (
            await session.execute(
                select(ArtifactVersion).where(
                    ArtifactVersion.artifact_type == artifact_type,
                    ArtifactVersion.artifact_id == artifact_id,
                    ArtifactVersion.superseded_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    return rows[0] if rows else None


async def _require_current_version(
    session: AsyncSession, artifact_type: ArtifactType, artifact_id: UUID
) -> ArtifactVersion:
    row = await _current_version(session, artifact_type, artifact_id)
    if row is None:
        raise _not_found(
            f"{artifact_type.value} {artifact_id} has no current version row. "
            "Submit it for approval first — that is what opens a version (R4)."
        )
    return row


def _apply(row: ArtifactVersion, artifact: Artifact, actor: Actor, at: dt.datetime) -> None:
    """Copy the state machine's result onto the stored row.

    Only the columns the transition actually moved. Notably `approved_by` /
    `approved_at` are taken from the `Artifact` rather than from `actor`, so the
    release path cannot accidentally restamp an approval — migration 1300's
    freeze trigger would reject that anyway, and the two agreeing is the point.
    """
    row.state = artifact.state
    row.content_hash = artifact.content_hash
    row.approved_by = artifact.approved_by
    row.approved_at = artifact.approved_at
    row.released_by = artifact.released_by
    row.released_at = artifact.released_at
    if artifact.state is ArtifactState.PENDING_APPROVAL:
        row.submitted_by = actor.actor_id
        row.submitted_at = at
    row.updated_at = at


async def _persist(
    session: AsyncSession,
    audit: AuditWriter,
    row: ArtifactVersion,
    outcome: ApprovalOutcome,
    actor: Actor,
    at: dt.datetime,
    *,
    notes: str | None = None,
) -> ArtifactVersionOut:
    """Land the transition and its audit row in ONE transaction.

    `write_within()` and not `write()`, per `app/core/audit.py`: "if losing the
    audit row would leave a money or approval decision unattributable, use
    `write_within()`". An approval and a release are exactly that case — it
    enlists the INSERT in this transaction and raises rather than swallowing, so
    the state change and the evidence for it commit together or neither does.
    """
    _apply(row, outcome.artifact, actor, at)
    if notes is not None:
        row.notes = notes
    await audit.write_within(session, outcome.event)
    await session.commit()
    return ArtifactVersionOut.of(row)


def _actor(principal: Principal) -> Actor:
    """The authenticated human behind the request (R3).

    `actor_id` is never `None` on this path — `CurrentPrincipal` is resolved from
    a verified JWT and a live `profiles` row — which is why no route here can
    satisfy an agent or a scheduled job. `HumanActorRequiredError` is still
    mapped below, because a guard that is unreachable today is a guard that keeps
    working when somebody adds a service principal tomorrow.
    """
    return Actor(actor_id=principal.user_id, persona=principal.persona)


# --- error translation --------------------------------------------------------


def _refuse(exc: Exception) -> HTTPException:
    """Map a governance refusal onto the status code that tells the truth.

    501 for an undefined authority is the one worth pausing on: the caller is
    not forbidden and the request is not malformed — the organisation has not
    decided who signs this off (§14 Q3). The exception's own message names the
    question and is passed through verbatim rather than summarised, so the person
    reading the response learns what has to happen next.
    """
    if isinstance(exc, ApprovalAuthorityUndefinedError):
        return HTTPException(status_code=AUTHORITY_UNDEFINED_STATUS, detail=str(exc))
    if isinstance(exc, UnauthorisedApproverError | HumanActorRequiredError):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    if isinstance(exc, MissingRejectionReasonError):
        return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc))
    if isinstance(exc, FrozenContentMismatchError | IllegalTransitionError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, CanonicalisationError):
        return HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                f"This artifact's content cannot be canonicalised, so it cannot be frozen "
                f"and therefore cannot be approved (R4): {exc}"
            ),
        )
    raise exc  # pragma: no cover - an approval error this module does not know


# --- endpoints ----------------------------------------------------------------


@router.post(
    "/{artifact_type}/{artifact_id}/submit",
    response_model=ArtifactVersionOut,
    status_code=status.HTTP_200_OK,
    summary="DRAFT -> PENDING_APPROVAL",
)
async def submit_artifact(
    artifact_type: ArtifactTypePath,
    artifact_id: ArtifactIdPath,
    principal: CurrentPrincipal,
    session: Session,
    audit: Audit,
) -> ArtifactVersionOut:
    """Put an artifact in front of an approver.

    Opens version 1 if the artifact has no version row yet. Nothing else in the
    system creates one, and a lifecycle that cannot be entered is not a
    lifecycle; the row is opened as a DRAFT and transitioned in the same
    transaction, so an artifact never sits in a DRAFT nobody asked for.

    No approval authority is checked and none should be. §8 puts "propose, human
    edits and sends" at autonomy level 2 — preparing work for a human is exactly
    what may happen without one. Whether the artifact is FIT to be submitted is
    the producing domain's question: for a payout that is
    `ValidationReport.can_submit()` and `POST /payouts/validate` reports it.
    """
    row = await _authorised_artifact(session, principal, artifact_type, artifact_id)
    now = dt.datetime.now(dt.UTC)

    version = await _current_version(session, artifact_type, artifact_id)
    if version is None:
        version = ArtifactVersion(
            id=uuid.uuid4(),
            artifact_type=artifact_type,
            artifact_id=artifact_id,
            version=1,
            state=ArtifactState.DRAFT,
            created_by=principal.user_id,
            created_at=now,
            updated_at=now,
        )
        session.add(version)

    actor = _actor(principal)
    try:
        outcome = submit_for_approval(_artifact(version, _payload(artifact_type, row)), actor, now)
    except Exception as exc:
        raise _refuse(exc) from exc
    return await _persist(session, audit, version, outcome, actor, now)


@router.post(
    "/{artifact_type}/{artifact_id}/approve",
    response_model=ArtifactVersionOut,
    status_code=status.HTTP_200_OK,
    summary="PENDING_APPROVAL -> APPROVED. Freezes and hashes the version (R4)",
)
async def approve_artifact(
    artifact_type: ArtifactTypePath,
    artifact_id: ArtifactIdPath,
    principal: CurrentPrincipal,
    session: Session,
    audit: Audit,
) -> ArtifactVersionOut:
    """Approve the current version, and stop.

    Approval FREEZES: the payload is read from the system of record, hashed, and
    the digest stored on the row where the database's freeze trigger makes it
    immutable. From here the artifact is *releasable*, which is not the same as
    released — nothing is sent, no queue is touched, and the only way out is a
    separate `POST .../release` writing its own audit row (R4).

    Authority comes from `APPROVAL_AUTHORITY`: a remuneration sheet is Senior
    Manager only (§4, "payout approval"), so a Manager who may READ the payout is
    refused here with 403. For an artifact type nobody has assigned authority to,
    this returns 501 naming §14 Q3 — see the module docstring.
    """
    row = await _authorised_artifact(session, principal, artifact_type, artifact_id)
    version = await _require_current_version(session, artifact_type, artifact_id)
    now = dt.datetime.now(dt.UTC)

    actor = _actor(principal)
    try:
        outcome = approve(_artifact(version, _payload(artifact_type, row)), actor, now)
    except Exception as exc:
        raise _refuse(exc) from exc
    return await _persist(session, audit, version, outcome, actor, now)


@router.post(
    "/{artifact_type}/{artifact_id}/reject",
    response_model=ArtifactVersionOut,
    status_code=status.HTTP_200_OK,
    summary="PENDING_APPROVAL -> DRAFT, with a required reason",
)
async def reject_artifact(
    artifact_type: ArtifactTypePath,
    artifact_id: ArtifactIdPath,
    request: RejectionRequest,
    principal: CurrentPrincipal,
    session: Session,
    audit: Audit,
) -> ArtifactVersionOut:
    """Send the current version back to DRAFT, with a stated reason.

    Same authority as approval, deliberately: the power to withhold approval is
    the power to approve, and an actor who could not have approved must not be
    able to block. The reason is recorded on the audit row by the state machine
    and on `artifact_versions.notes` here, so the person who has to fix the
    artifact can read it without querying the audit trail.

    The version number does not change. A rejected draft is the same version
    being reworked; only a frozen artifact is superseded (R4).
    """
    row = await _authorised_artifact(session, principal, artifact_type, artifact_id)
    version = await _require_current_version(session, artifact_type, artifact_id)
    now = dt.datetime.now(dt.UTC)

    actor = _actor(principal)
    try:
        outcome = reject(
            _artifact(version, _payload(artifact_type, row)), actor, request.reason, now
        )
    except Exception as exc:
        raise _refuse(exc) from exc
    return await _persist(
        session, audit, version, outcome, actor, now, notes=request.reason.strip()
    )


@router.post(
    "/{artifact_type}/{artifact_id}/release",
    response_model=ArtifactVersionOut,
    status_code=status.HTTP_200_OK,
    summary="APPROVED -> RELEASED. Separate act, separate audit row (R4)",
)
async def release_artifact(
    artifact_type: ArtifactTypePath,
    artifact_id: ArtifactIdPath,
    request: ReleaseRequest,
    principal: CurrentPrincipal,
    session: Session,
    audit: Audit,
) -> ArtifactVersionOut:
    """Mark an approved version released. Terminal, human-only, and silent.

    THE HASH IS RE-VERIFIED HERE. `release()` calls `verify_frozen()` over the
    payload read back from the system of record a moment ago, and refuses with
    409 if it no longer matches the digest taken at approval. Without that
    recheck "APPROVED" would be a column value rather than a claim about
    content, and an edit made after approval would ride out of the system on
    somebody else's signature.

    Releasing TRANSMITS NOTHING. No email, no WhatsApp, no message: this marks
    state. §8 gives the outbound queue to the Comms service, which may only act
    on an already-RELEASED artifact, and R3 keeps a release-capable tool out of
    every agent toolset — asserted by a test, not by this docstring.

    Refused before approval: DRAFT and PENDING_APPROVAL have no edge to RELEASED
    in `ALLOWED_TRANSITIONS`, so the attempt is a 409 naming the legal moves.
    """
    row = await _authorised_artifact(session, principal, artifact_type, artifact_id)
    version = await _require_current_version(session, artifact_type, artifact_id)
    now = dt.datetime.now(dt.UTC)

    actor = _actor(principal)
    try:
        outcome = release(_artifact(version, _payload(artifact_type, row)), actor, now)
    except Exception as exc:
        raise _refuse(exc) from exc
    return await _persist(session, audit, version, outcome, actor, now, notes=request.notes)


@router.get(
    "/{artifact_type}/{artifact_id}/versions",
    response_model=VersionHistory,
    status_code=status.HTTP_200_OK,
    summary="Every version of one artifact, oldest first",
)
async def artifact_versions(
    artifact_type: ArtifactTypePath,
    artifact_id: ArtifactIdPath,
    principal: CurrentPrincipal,
    session: Session,
) -> VersionHistory:
    """Read the lifecycle history: who submitted, who approved, who released.

    Behind the same wall and the same reach as every write above — an
    `artifact_versions` row for a remuneration sheet is commercial, so an LDE
    Executive gets 403 rather than an approval trail with a Senior Manager's name
    and a payout period on it.

    A pure read, so no audit row is written. Returning an empty list rather than
    404 for an artifact that has never entered the lifecycle is deliberate: the
    artifact exists and the honest answer to "what has happened to it" is
    "nothing yet".
    """
    await _authorised_artifact(session, principal, artifact_type, artifact_id)
    rows = (
        (
            await session.execute(
                select(ArtifactVersion)
                .where(
                    ArtifactVersion.artifact_type == artifact_type,
                    ArtifactVersion.artifact_id == artifact_id,
                )
                .order_by(ArtifactVersion.version)
            )
        )
        .scalars()
        .all()
    )
    return VersionHistory(
        artifact_type=artifact_type,
        artifact_id=artifact_id,
        versions=tuple(ArtifactVersionOut.of(row) for row in sorted(rows, key=_version_of)),
    )


def _version_of(row: ArtifactVersion) -> int:
    """Sort key for the history. The ORDER BY above is the real one; this keeps
    the response ordered even where the statement is served from a cache or a
    test double that does not evaluate it."""
    return row.version
