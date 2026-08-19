"""The outbound queue's HTTP surface — CLAUDE.md §8's Comms Service.

    POST   /comms/messages                    draft a message into the queue
    GET    /comms/messages                    the queue, filtered
    GET    /comms/messages/{id}               one message, with its diff
    PATCH  /comms/messages/{id}               amend a DRAFT's body
    POST   /comms/messages/{id}/submit        DRAFT -> PENDING_APPROVAL
    POST   /comms/messages/{id}/approve       PENDING_APPROVAL -> APPROVED   (501)
    POST   /comms/messages/{id}/reject        PENDING_APPROVAL -> DRAFT      (501)
    POST   /comms/messages/{id}/release       APPROVED -> RELEASED           (501)
    POST   /comms/messages/{id}/supersede     a new DRAFT replacing a frozen one

This router is I/O, authorisation and persistence. The lifecycle rules are
`app/services/comms/lifecycle.py`, which is itself an adapter over
`app/services/approval/` — the grammar in `ALLOWED_TRANSITIONS` is reached
through `check_transition()` and the freeze through the same `content_hash()`
that freezes a remuneration sheet. Nothing about R4 is decided in this file; it
takes the `CommsOutcome` the service returns and lands both halves of it, the row
and the audit event, in ONE transaction (§11, `write_within()`).

THE THREE SENTENCES WORTH READING BEFORE THE CODE
=================================================
**Release does not transmit, and this router could not make it.** There is no
provider here: no SMTP, no Twilio, no SendGrid, no outbound HTTP client of any
kind, and no `sent_at` column to stamp. `POST .../release` marks state and writes
an audit row. RELEASED means an authenticated human cleared the message to go,
which makes it *eligible* for a transport that does not exist yet. When one is
built it reads rows in that state; it does not get a hook here.

**Approve, reject and release all return 501 today, on purpose.** CLAUDE.md §14
Q3 — "Approval authority for college-facing comms: Manager or Senior Manager?" —
is open, so `COMMS_APPROVAL_AUTHORITY` in `app/services/comms/authority.py` is
empty and the service raises. 501 and not 403, mirroring `app/api/approvals.py`
exactly: the caller is not forbidden, the organisation has no answer yet, and a
403 would send a Senior Manager hunting for a permission that does not exist. The
exception's own message — which names the question — is passed through verbatim.
Do not "fix" these 501s by filling in the authority table; fix them by answering
Q3, and see `1700_comms_queue.sql` for the migration that should accompany the
answer.

**R3: no agent path reaches any of this.** Every handler takes
`CurrentPrincipal`, resolved from a verified Supabase JWT and a live `profiles`
row. `app/agents/tools/catalog.py` is a closed set of read tools plus
`save_draft`; `tools/rule_linter.py` L3 fails the build on a release-capable name
or a send-shaped import under `app/agents/`; `tests/unit/test_agents_toolsets.py`
asserts the same independently. An agent drafts by producing the fields a human
then POSTs, and that is the only way in.

R5 — THE COMMERCIALS WALL, IN CODE AS WELL AS IN SQL
====================================================
`app/db/session.py` connects with BYPASSRLS, so the two policies 1700 defines on
`comms_messages` never run for us. Every handler therefore reproduces them, in
the same shape and with the same two predicates kept separate the way the SQL
keeps them:

    commercial row : can_see_commercials() AND can_reach_program()
    other row      : is_internal()         AND can_reach_program()

A message about a payout is the payout restated in prose — "your July invoice of
₹14,035 is approved" — so an LDE Executive gets 403 before any row is read, and
cannot even confirm the message exists. Unlike `artifact_versions`, there is no
`artifact_is_commercial()` to consult: no predicate can read prose, so the flag is
declared at draft time and forced true for a remuneration back-reference by a
CHECK constraint. `_creation_is_commercial()` below applies the same forcing in
code so the constraint is a backstop rather than the only guard.

R1 — WHERE THE FACTS COME FROM
==============================
`template_values` is the structured input, and `render()` substitutes it into the
template to produce the baseline that the diff is taken against. A float in that
object is REFUSED (R7) rather than rounded — a rupee amount reaching a trainer as
`15584.000000000001` is wrong in the one way nobody forgives — so amounts arrive
as strings or integers. Nothing in this router computes money, and per R2 nothing
ever may: an amount is passed in, or it does not appear.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Final
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, ConfigDict, Field, JsonValue
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
from app.db.models import CommsMessage, Program
from app.db.session import get_session
from app.domain.enums import ArtifactState, ArtifactType
from app.services.approval import (
    Actor,
    CanonicalisationError,
    FrozenContentMismatchError,
    HumanActorRequiredError,
    IllegalTransitionError,
    MissingRejectionReasonError,
)
from app.services.comms import (
    CommsApprovalAuthorityUndefinedError,
    CommsChannel,
    CommsOutcome,
    CommsRecipientKind,
    CommsUnauthorisedApproverError,
    QueuedMessage,
    TemplateRenderError,
    amend_message,
    approve_message,
    diff_from_template,
    queue_message,
    reject_message,
    release_message,
    render,
    submit_message,
    supersede_message,
)

router = APIRouter(prefix="/comms", tags=["comms"])

Session = Annotated[AsyncSession, Depends(get_session)]
Audit = Annotated[AuditWriter, Depends(get_audit_writer)]
MessageIdPath = Annotated[UUID, Path(description="`comms_messages.id`")]

#: Status for a message nobody has been given authority to approve (§14 Q3).
#: Not 403 — see the module docstring. Identical to `app/api/approvals.py`'s
#: constant, deliberately: the two surfaces report the same open question and a
#: client should not have to learn two codes for it.
AUTHORITY_UNDEFINED_STATUS: Final[int] = status.HTTP_501_NOT_IMPLEMENTED

#: Cap on a listing page. The queue is read by a human deciding what to review,
#: and a request for ten thousand rows is a report, not a queue.
MAX_PAGE: Final[int] = 200


# --- wire models --------------------------------------------------------------


class DraftRequest(BaseModel):
    """A message being put into the queue.

    `template` is the RAW template text and `body` is what would actually go out.
    The server renders the first against `template_values` to produce the
    baseline, then diffs the baseline against the second — see the module
    docstring on R1. Sending a pre-rendered baseline is not possible on purpose:
    the diff must be computed here, over inputs that are all on the record, or it
    is not evidence of anything.

    There is no template LIBRARY in this phase, so the template text travels with
    the draft and is snapshotted on the row. That is not a shortcut — §8 requires
    the diff be *shown at approval*, and a diff recomputed later against a
    since-edited library entry would silently restate what the approver saw.
    """

    model_config = ConfigDict(extra="forbid")

    program_id: UUID = Field(description="The program this message is about. Sets reach (§4).")
    channel: CommsChannel
    recipient_kind: CommsRecipientKind = Field(
        description="Recipient CLASS, which sets the §8 autonomy ceiling — not the address."
    )
    recipient_ref: str = Field(min_length=1, description="Address, handle or ticket queue.")
    recipient_name: str | None = None

    template_key: str = Field(min_length=1, description="Stable identifier of the template used.")
    template: str = Field(min_length=1, description="Raw template text, with `{{slot}}` markers.")
    template_values: dict[str, JsonValue] = Field(
        default_factory=dict,
        description=(
            "R1: the structured facts substituted into the template. A float is refused "
            "(R7) — send an amount as a string or an integer."
        ),
    )

    subject: str | None = None
    body: str = Field(min_length=1, description="What would actually be sent.")

    is_commercial: bool = Field(
        default=False,
        description=(
            "R5: true when the message is about money. Forced true for a "
            "remuneration_sheets back-reference regardless of what is sent."
        ),
    )
    related_artifact_type: ArtifactType | None = None
    related_artifact_id: UUID | None = None


class AmendRequest(BaseModel):
    """A new body for a DRAFT. Same version, still DRAFT.

    Only the body. Changing the recipient, the channel or the template changes
    what message this IS, and the honest representation of that is a new draft —
    `POST .../supersede` for a frozen one, or a fresh `POST /comms/messages`.
    """

    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1)


class RejectionRequest(BaseModel):
    """A rejection, with the reason that makes it actionable.

    `min_length` here and a whitespace check in the service, the same belt-and-
    braces `app/api/approvals.py` uses: the model guards the wire and turns a
    service exception into a 422 naming the field, the service guards every other
    caller.
    """

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, description="Why the message is going back to its drafter.")


class ReleaseRequest(BaseModel):
    """Optional commentary attached to a release. Never content.

    `notes` is the one column the 1700 freeze trigger leaves mutable after
    approval, for 1300's reason: a release note is commentary ABOUT the message,
    not part of it. It is not hashed and cannot alter what was approved.
    """

    model_config = ConfigDict(extra="forbid")

    notes: str | None = None


class SupersedeRequest(BaseModel):
    """Replace a frozen message with a fresh DRAFT at `version + 1`.

    R4: editing an approved artifact creates a new version in DRAFT requiring
    fresh approval. The predecessor row is left exactly as approved.
    """

    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1)
    template: str | None = Field(
        default=None,
        description="New template text. Omit to keep the predecessor's baseline.",
    )
    template_values: dict[str, JsonValue] | None = Field(
        default=None, description="Required when `template` is supplied."
    )


class CommsMessageOut(BaseModel):
    """One queue row on the wire, diff included.

    The BODY is included and the diff is included, because unlike
    `ArtifactVersionOut` this endpoint IS the review surface — an approver who
    cannot read the message cannot approve it. The wall is what keeps that safe:
    a commercial row never reaches a persona that fails
    `can_see_commercials()`, and the check happens before the row is read.
    """

    model_config = ConfigDict(frozen=True)

    id: UUID
    program_id: UUID
    channel: str
    recipient_kind: str
    recipient_ref: str
    recipient_name: str | None = None

    template_key: str
    template_body: str
    template_values: dict[str, JsonValue]
    subject: str | None = None
    body: str
    #: The §8 review surface: what the drafter changed, not the whole message.
    diff: dict[str, JsonValue]

    is_commercial: bool
    related_artifact_type: ArtifactType | None = None
    related_artifact_id: UUID | None = None

    state: ArtifactState
    content_hash: str | None = Field(
        default=None, description="Frozen at approval (R4). NULL before it."
    )
    version: int = Field(ge=1)
    supersedes_id: UUID | None = None
    superseded_at: dt.datetime | None = None

    created_by: UUID | None = None
    created_at: dt.datetime
    submitted_by: UUID | None = None
    submitted_at: dt.datetime | None = None
    approved_by: UUID | None = None
    approved_at: dt.datetime | None = None
    #: Set when a human cleared the message to go. NOT a send timestamp — no
    #: provider is wired in this phase and there is no `sent_at` column.
    released_by: UUID | None = None
    released_at: dt.datetime | None = None
    notes: str | None = None

    @classmethod
    def of(cls, row: CommsMessage) -> CommsMessageOut:
        return cls(
            id=row.id,
            program_id=row.program_id,
            channel=row.channel,
            recipient_kind=row.recipient_kind,
            recipient_ref=row.recipient_ref,
            recipient_name=row.recipient_name,
            template_key=row.template_key,
            template_body=row.template_body,
            template_values=dict(row.template_values),
            subject=row.subject,
            body=row.body,
            diff=dict(row.diff),
            is_commercial=row.is_commercial,
            related_artifact_type=row.related_artifact_type,
            related_artifact_id=row.related_artifact_id,
            state=row.state,
            content_hash=row.content_hash,
            version=row.version,
            supersedes_id=row.supersedes_id,
            superseded_at=row.superseded_at,
            created_by=row.created_by,
            created_at=row.created_at,
            submitted_by=row.submitted_by,
            submitted_at=row.submitted_at,
            approved_by=row.approved_by,
            approved_at=row.approved_at,
            released_by=row.released_by,
            released_at=row.released_at,
            notes=row.notes,
        )


class CommsQueue(BaseModel):
    """A page of the queue, newest first — the order somebody triaging wants."""

    model_config = ConfigDict(frozen=True)

    messages: tuple[CommsMessageOut, ...]


# --- authorisation: the app-side mirror of 1700's two policies -----------------


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


async def _college_id(session: AsyncSession, program_id: UUID) -> UUID:
    """The college reach is resolved against — `programs.college_id`, as in SQL."""
    program = await session.get(Program, program_id)
    if program is None:
        raise _not_found(f"No program with id {program_id}")
    return program.college_id


async def _authorise(
    session: AsyncSession, principal: Principal, program_id: UUID, *, is_commercial: bool
) -> None:
    """Persona, then wall, then reach. Refuse otherwise.

    The ordering is `app/api/approvals.py`'s and it is deliberate.
    `require_internal()` runs before any query, so a trainer or a college login
    probing message ids gets the same 403 for every id. The wall closes next, on
    the row's own `is_commercial` flag, so an LDE Executive cannot even learn that
    a payout message exists — which on a BYPASSRLS connection is the whole
    protection (R5).

    Both conjuncts are applied, as every policy in 1700, 1300 and 0700 writes
    them: the wall alone would let a Manager act on another cluster's message, the
    reach alone would let an LDE Executive act on a payout chase.
    """
    require_internal(principal)
    if is_commercial:
        require_commercials(principal)
    require_college_reach(principal, await _college_id(session, program_id))


async def _row(session: AsyncSession, message_id: UUID, principal: Principal) -> CommsMessage:
    """One queue row the caller is entitled to, or 403/404.

    Note the order: the row is fetched, then authorised, then returned. A 404 for
    a message outside the caller's reach would be more polite and would leak the
    id space; a 403 is what the SQL policies produce (zero rows) once translated
    into an app that has already read the row on a BYPASSRLS connection.
    """
    row = await session.get(CommsMessage, message_id)
    if row is None:
        raise _not_found(f"No comms message with id {message_id}")
    await _authorise(session, principal, row.program_id, is_commercial=row.is_commercial)
    return row


def _creation_is_commercial(request: DraftRequest) -> bool:
    """R5, forced rather than trusted.

    A message about a remuneration sheet is commercial whatever the caller
    declared — `comms_messages_commercial_implied_ck` says so in SQL, and this
    says so before the INSERT so the constraint is a backstop rather than the only
    guard. The reverse is deliberately NOT implied: prose about a rate is
    commercial with or without a back-reference, so a caller may flag a message
    commercial with no artifact attached and that flag stands.
    """
    if request.related_artifact_type is ArtifactType.REMUNERATION_SHEET:
        return True
    return request.is_commercial


# --- the service <-> row bridge ------------------------------------------------


def _queued(row: CommsMessage) -> QueuedMessage:
    """The pure `QueuedMessage` the lifecycle operates on, from one stored row.

    `QueuedMessage.__post_init__` enforces the same biconditional the database
    does — a frozen state carries a hash, an unfrozen one does not. A row that
    satisfies `comms_messages_approved_ck` always satisfies it, so a `ValueError`
    here means the two invariants have diverged: a 500, not a 4xx, because the
    caller did nothing wrong and there is nothing they can change.
    """
    try:
        return QueuedMessage(
            message_id=row.id,
            program_id=row.program_id,
            version=row.version,
            state=row.state,
            channel=CommsChannel(row.channel),
            recipient_kind=CommsRecipientKind(row.recipient_kind),
            recipient_ref=row.recipient_ref,
            recipient_name=row.recipient_name,
            template_key=row.template_key,
            template_body=row.template_body,
            subject=row.subject,
            body=row.body,
            is_commercial=row.is_commercial,
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
                f"comms_messages row {row.id} is internally inconsistent and cannot be "
                f"loaded into the R4 lifecycle: {exc}"
            ),
        ) from exc


def _apply(row: CommsMessage, message: QueuedMessage, actor: Actor, at: dt.datetime) -> None:
    """Copy the lifecycle's result onto the stored row.

    Only the columns the transition moved, and `approved_by` / `approved_at` are
    taken from the `QueuedMessage` rather than from `actor` so the release path
    cannot restamp an approval. The 1700 freeze trigger would reject that anyway,
    and the two agreeing is the point.

    The body is copied too, because `amend_message()` is the one operation that
    changes it, and the diff is recomputed from the two sides in the same breath —
    a stored diff that no longer describes the stored body would be a review
    surface that lies.
    """
    row.state = message.state
    row.body = message.body
    row.diff = diff_from_template(message.template_body, message.body).as_json()
    row.content_hash = message.content_hash
    row.approved_by = message.approved_by
    row.approved_at = message.approved_at
    row.released_by = message.released_by
    row.released_at = message.released_at
    if message.state is ArtifactState.PENDING_APPROVAL:
        row.submitted_by = actor.actor_id
        row.submitted_at = at
    row.updated_at = at


async def _persist(
    session: AsyncSession,
    audit: AuditWriter,
    row: CommsMessage,
    outcome: CommsOutcome,
    actor: Actor,
    at: dt.datetime,
    *,
    notes: str | None = None,
) -> CommsMessageOut:
    """Land the transition and its audit row in ONE transaction.

    `write_within()` and not `write()`, per `app/core/audit.py`: "if losing the
    audit row would leave a money or approval decision unattributable, use
    `write_within()`". An approval and a release are exactly that case — it
    enlists the INSERT in this transaction and raises rather than swallowing, so
    the state change and the evidence commit together or neither does.
    """
    _apply(row, outcome.message, actor, at)
    if notes is not None:
        row.notes = notes
    await audit.write_within(session, outcome.event)
    await session.commit()
    return CommsMessageOut.of(row)


def _actor(principal: Principal) -> Actor:
    """The authenticated human behind the request (R3).

    `actor_id` is never `None` on this path — `CurrentPrincipal` is resolved from
    a verified JWT and a live `profiles` row — which is why no route here can
    satisfy an agent or a scheduled job. `HumanActorRequiredError` is still mapped
    below, because a guard that is unreachable today is a guard that keeps working
    when somebody adds a service principal tomorrow.
    """
    return Actor(actor_id=principal.user_id, persona=principal.persona)


# --- error translation ---------------------------------------------------------


def _refuse(exc: Exception) -> HTTPException:
    """Map a governance refusal onto the status code that tells the truth.

    501 for an undefined authority is the one worth pausing on, and it is the
    status every approve, reject and release currently returns: the caller is not
    forbidden and the request is not malformed — the organisation has not decided
    who signs off a college-facing message (§14 Q3). The exception's own message
    names the question and is passed through verbatim rather than summarised, so
    the person reading the response learns what has to happen next.
    """
    if isinstance(exc, CommsApprovalAuthorityUndefinedError):
        return HTTPException(status_code=AUTHORITY_UNDEFINED_STATUS, detail=str(exc))
    if isinstance(exc, CommsUnauthorisedApproverError | HumanActorRequiredError):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    if isinstance(exc, MissingRejectionReasonError | TemplateRenderError):
        return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc))
    if isinstance(exc, FrozenContentMismatchError | IllegalTransitionError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, CanonicalisationError):
        return HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                f"This message's content cannot be canonicalised, so it cannot be frozen "
                f"and therefore cannot be approved (R4): {exc}"
            ),
        )
    if isinstance(exc, ValueError):
        # `queue_message`, `amend_message` and `supersede_message` refuse a wrong
        # starting state with a ValueError. That is the caller asking for
        # something the ladder does not allow, which is a conflict, not a bug.
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    raise exc  # pragma: no cover - a refusal this module does not know


# --- endpoints -----------------------------------------------------------------


@router.post(
    "/messages",
    response_model=CommsMessageOut,
    status_code=status.HTTP_201_CREATED,
    summary="Draft a message into the single outbound queue",
)
async def draft_message(
    request: DraftRequest,
    principal: CurrentPrincipal,
    session: Session,
    audit: Audit,
) -> CommsMessageOut:
    """Put a message in the queue as a DRAFT, with its diff-from-template.

    The two-step §8 requires happens here and nowhere else: the template is
    rendered against `template_values` to produce the baseline, then the baseline
    is diffed against the body. Everything the diff reports is drafter-authored
    prose, because every substituted fact is identical on both sides — which is
    what makes the diff worth an approver's attention rather than noise.

    No approval authority is checked and none should be. §8 puts "propose, human
    edits and sends" at autonomy level 2, so drafting is exactly what may happen
    without an approver in the room. What IS checked is the wall and the reach: a
    commercial message may only be drafted by a persona that could see it (R5).

    Nothing is sent. The row is DRAFT and there is no path from here to a
    recipient that does not pass through approve and release.
    """
    is_commercial = _creation_is_commercial(request)
    await _authorise(session, principal, request.program_id, is_commercial=is_commercial)

    now = dt.datetime.now(dt.UTC)
    try:
        template_body = render(request.template, request.template_values)
    except TemplateRenderError as exc:
        raise _refuse(exc) from exc
    diff = diff_from_template(template_body, request.body)

    row = CommsMessage(
        id=uuid.uuid4(),
        program_id=request.program_id,
        channel=request.channel,
        recipient_kind=request.recipient_kind,
        recipient_ref=request.recipient_ref,
        recipient_name=request.recipient_name,
        template_key=request.template_key,
        template_body=template_body,
        template_values=dict(request.template_values),
        subject=request.subject,
        body=request.body,
        diff=diff.as_json(),
        is_commercial=is_commercial,
        related_artifact_type=request.related_artifact_type,
        related_artifact_id=request.related_artifact_id,
        state=ArtifactState.DRAFT,
        version=1,
        created_by=principal.user_id,
        created_at=now,
        updated_at=now,
    )
    session.add(row)

    actor = _actor(principal)
    try:
        outcome = queue_message(_queued(row), actor, now)
    except Exception as exc:
        raise _refuse(exc) from exc
    return await _persist(session, audit, row, outcome, actor, now)


@router.get(
    "/messages",
    response_model=CommsQueue,
    status_code=status.HTTP_200_OK,
    summary="The outbound queue, newest first",
)
async def list_messages(
    principal: CurrentPrincipal,
    session: Session,
    program_id: Annotated[UUID, Query(description="Required: the queue is program-scoped.")],
    state: Annotated[ArtifactState | None, Query(description="Filter by lifecycle state.")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 50,
) -> CommsQueue:
    """What is in the queue for one program, and what state each message is in.

    `program_id` is required rather than optional. A cross-program queue would
    have to apply reach row by row, and the honest way to do that on a BYPASSRLS
    connection is a query this router does not have — so the scope is stated by
    the caller and checked once, before anything is read.

    Commercial messages are filtered out for a persona that cannot see them,
    rather than the whole request being refused: a Manager and an LDE Executive
    both legitimately read this queue, and they should see different rows in it,
    which is exactly what the two policies in 1700 do. That is why the wall is
    applied per row here and up front everywhere else.

    SEC-07 — THE WALL GOES IN THE STATEMENT, NOT AFTER IT
    -----------------------------------------------------
    The filter used to run in Python over rows the database had ALREADY limited,
    which quietly made `limit` mean "rows considered" rather than "rows returned":
    an LDE Executive asking for 50 messages on a program whose 50 newest are all
    payout chases received an empty queue while fifty operational messages sat
    behind them. Under-disclosure rather than a leak — the wall itself never
    moved — but a queue that hides work is a queue people stop trusting, and this
    one is read to decide what to approve next.

    So the predicate goes into the `WHERE`, ahead of `ORDER BY ... LIMIT`, which
    is also how the SQL policies would have applied it if RLS ran on this
    connection at all. The Python pass is kept BELOW as a backstop rather than
    replaced: it is the assertion that no commercial row can reach the response
    even if the statement is later edited, and it costs one boolean per row.

    A pure read, so no audit row is written.
    """
    require_internal(principal)
    require_college_reach(principal, await _college_id(session, program_id))

    statement = select(CommsMessage).where(CommsMessage.program_id == program_id)
    if state is not None:
        statement = statement.where(CommsMessage.state == state)
    if not principal.can_see_commercials:
        statement = statement.where(CommsMessage.is_commercial.is_(False))
    rows = (
        (await session.execute(statement.order_by(CommsMessage.created_at.desc()).limit(limit)))
        .scalars()
        .all()
    )
    visible = [row for row in rows if principal.can_see_commercials or not row.is_commercial]
    return CommsQueue(messages=tuple(CommsMessageOut.of(row) for row in visible))


@router.get(
    "/messages/{message_id}",
    response_model=CommsMessageOut,
    status_code=status.HTTP_200_OK,
    summary="One message, with the diff an approver reads",
)
async def read_message(
    message_id: MessageIdPath,
    principal: CurrentPrincipal,
    session: Session,
) -> CommsMessageOut:
    """The approval screen's payload: channel, recipient, template and diff (§8).

    Behind the same wall and the same reach as every write below. A pure read, so
    no audit row.
    """
    return CommsMessageOut.of(await _row(session, message_id, principal))


@router.patch(
    "/messages/{message_id}",
    response_model=CommsMessageOut,
    status_code=status.HTTP_200_OK,
    summary="Amend a DRAFT's body. Recomputes the diff",
)
async def amend(
    message_id: MessageIdPath,
    request: AmendRequest,
    principal: CurrentPrincipal,
    session: Session,
    audit: Audit,
) -> CommsMessageOut:
    """Rewrite a DRAFT in place. Same version, still DRAFT, diff recomputed.

    Refused for anything else with 409. A message in PENDING_APPROVAL is in front
    of a human, and editing it underneath them means they approve one text while
    another is stored; reject it back to DRAFT first, which leaves an audit row
    saying so. A frozen message is superseded, never amended (R4).
    """
    row = await _row(session, message_id, principal)
    now = dt.datetime.now(dt.UTC)
    actor = _actor(principal)
    try:
        outcome = amend_message(_queued(row), actor, request.body, now)
    except Exception as exc:
        raise _refuse(exc) from exc
    return await _persist(session, audit, row, outcome, actor, now)


@router.post(
    "/messages/{message_id}/submit",
    response_model=CommsMessageOut,
    status_code=status.HTTP_200_OK,
    summary="DRAFT -> PENDING_APPROVAL",
)
async def submit(
    message_id: MessageIdPath,
    principal: CurrentPrincipal,
    session: Session,
    audit: Audit,
) -> CommsMessageOut:
    """Put the message in front of an approver.

    No authority check, deliberately — §8 puts preparing work for a human at
    autonomy level 2. This is the last step that currently succeeds: with §14 Q3
    open, the queue fills to PENDING_APPROVAL and stops there. That is R4 working,
    not a bug to route around.
    """
    row = await _row(session, message_id, principal)
    now = dt.datetime.now(dt.UTC)
    actor = _actor(principal)
    try:
        outcome = submit_message(_queued(row), actor, now)
    except Exception as exc:
        raise _refuse(exc) from exc
    return await _persist(session, audit, row, outcome, actor, now)


@router.post(
    "/messages/{message_id}/approve",
    response_model=CommsMessageOut,
    status_code=status.HTTP_200_OK,
    summary="PENDING_APPROVAL -> APPROVED. Returns 501 while §14 Q3 is open",
)
async def approve(
    message_id: MessageIdPath,
    principal: CurrentPrincipal,
    session: Session,
    audit: Audit,
) -> CommsMessageOut:
    """Approve the current message, and stop.

    **Returns 501 for every message today**, naming §14 Q3 — "Approval authority
    for college-facing comms: Manager or Senior Manager?" — because nobody has
    decided who may do this. See the module docstring; do not answer the question
    by editing the authority table to make this endpoint work.

    When it does work: approval FREEZES. The channel, recipient, template,
    baseline, body and commercial flag are hashed and the digest stored where the
    1700 trigger makes it immutable. From there the message is *releasable*, which
    is not released — nothing is sent, and the only way out is a separate
    `POST .../release` writing its own audit row (R4).
    """
    row = await _row(session, message_id, principal)
    now = dt.datetime.now(dt.UTC)
    actor = _actor(principal)
    try:
        outcome = approve_message(_queued(row), actor, now)
    except Exception as exc:
        raise _refuse(exc) from exc
    return await _persist(session, audit, row, outcome, actor, now)


@router.post(
    "/messages/{message_id}/reject",
    response_model=CommsMessageOut,
    status_code=status.HTTP_200_OK,
    summary="PENDING_APPROVAL -> DRAFT, with a required reason. 501 while Q3 is open",
)
async def reject(
    message_id: MessageIdPath,
    request: RejectionRequest,
    principal: CurrentPrincipal,
    session: Session,
    audit: Audit,
) -> CommsMessageOut:
    """Send the message back to DRAFT, with a stated reason.

    Same authority as approval, and therefore the same 501 today: the power to
    withhold approval is the power to approve, and an actor who could not have
    approved must not be able to block. The reason is recorded on the audit row by
    the service and on `comms_messages.notes` here, so the drafter can read it
    without querying the audit trail.

    The version does not change. A rejected draft is the same message being
    reworked; only a frozen one is superseded (R4).
    """
    row = await _row(session, message_id, principal)
    now = dt.datetime.now(dt.UTC)
    actor = _actor(principal)
    try:
        outcome = reject_message(_queued(row), actor, request.reason, now)
    except Exception as exc:
        raise _refuse(exc) from exc
    return await _persist(session, audit, row, outcome, actor, now, notes=request.reason.strip())


@router.post(
    "/messages/{message_id}/release",
    response_model=CommsMessageOut,
    status_code=status.HTTP_200_OK,
    summary="APPROVED -> RELEASED. Marks state; transmits nothing. 501 while Q3 is open",
)
async def release(
    message_id: MessageIdPath,
    request: ReleaseRequest,
    principal: CurrentPrincipal,
    session: Session,
    audit: Audit,
) -> CommsMessageOut:
    """Mark an approved message released. Terminal, human-only, and silent.

    **RELEASING IS NOT SENDING.** No email leaves, no WhatsApp message is posted,
    no ticket is opened. No provider is wired in this phase and none may be added
    to this router. Release records that an authenticated human cleared the
    message, which makes it *eligible* to be transmitted by a transport built
    later — that transport will read rows in state RELEASED and record delivery in
    its own columns. There is deliberately no `sent_at` here to blur the two.

    THE HASH IS RE-VERIFIED. `release_message()` recomputes the digest over the
    stored content and refuses with 409 if it no longer matches what was frozen at
    approval. Without that recheck "APPROVED" would be a column value rather than
    a claim about content, and a recipient swapped after approval would ride out
    on somebody else's signature.

    Returns 501 today, for the same reason approve does.

    R3: no agent reaches this. `CurrentPrincipal` requires a verified human
    session, `app/agents/tools/catalog.py` binds no release-capable tool, and both
    the L3 linter rule and `tests/unit/test_agents_toolsets.py` fail the build if
    one appears.
    """
    row = await _row(session, message_id, principal)
    now = dt.datetime.now(dt.UTC)
    actor = _actor(principal)
    try:
        outcome = release_message(_queued(row), actor, now)
    except Exception as exc:
        raise _refuse(exc) from exc
    return await _persist(session, audit, row, outcome, actor, now, notes=request.notes)


@router.post(
    "/messages/{message_id}/supersede",
    response_model=CommsMessageOut,
    status_code=status.HTTP_201_CREATED,
    summary="A new DRAFT replacing a frozen message (R4)",
)
async def supersede(
    message_id: MessageIdPath,
    request: SupersedeRequest,
    principal: CurrentPrincipal,
    session: Session,
    audit: Audit,
) -> CommsMessageOut:
    """Replace an APPROVED or RELEASED message with a fresh DRAFT.

    R4: "Editing an approved artifact creates a new version in DRAFT requiring
    fresh approval." The predecessor is left exactly as it was approved, stamped
    `superseded_at`, and the successor is a new row pointing back at it through
    `supersedes_id` — so the approval history of a re-drafted message is walkable
    rather than overwritten.

    A new template may be supplied, in which case `template_values` must come with
    it: a baseline rendered from stale values would make the successor's diff
    describe facts nobody re-fetched. Omit both to keep the predecessor's baseline
    and change only the prose.

    Both rows are written in one transaction. A successor that exists without its
    predecessor being marked superseded would leave two live messages to the same
    recipient, which is how a college receives the same thing twice.
    """
    row = await _row(session, message_id, principal)
    now = dt.datetime.now(dt.UTC)
    actor = _actor(principal)

    template_body: str | None = None
    if request.template is not None:
        if request.template_values is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    "`template_values` is required when `template` is supplied: a baseline "
                    "rendered from the predecessor's values would make the successor's diff "
                    "describe facts nobody re-fetched (CLAUDE.md R1)."
                ),
            )
        try:
            template_body = render(request.template, request.template_values)
        except TemplateRenderError as exc:
            raise _refuse(exc) from exc

    successor_id = uuid.uuid4()
    try:
        outcome = supersede_message(
            _queued(row),
            actor,
            successor_id,
            body=request.body,
            template_body=template_body,
            at=now,
        )
    except Exception as exc:
        raise _refuse(exc) from exc

    successor = outcome.message
    values = (
        request.template_values
        if request.template_values is not None
        else dict(row.template_values)
    )
    successor_row = CommsMessage(
        id=successor_id,
        program_id=successor.program_id,
        channel=successor.channel,
        recipient_kind=successor.recipient_kind,
        recipient_ref=successor.recipient_ref,
        recipient_name=successor.recipient_name,
        template_key=successor.template_key,
        template_body=successor.template_body,
        template_values=dict(values),
        subject=successor.subject,
        body=successor.body,
        diff=diff_from_template(successor.template_body, successor.body).as_json(),
        is_commercial=successor.is_commercial,
        related_artifact_type=row.related_artifact_type,
        related_artifact_id=row.related_artifact_id,
        state=ArtifactState.DRAFT,
        version=successor.version,
        supersedes_id=row.id,
        created_by=principal.user_id,
        created_at=now,
        updated_at=now,
    )
    session.add(successor_row)
    row.superseded_at = now
    row.updated_at = now

    await audit.write_within(session, outcome.event)
    await session.commit()
    return CommsMessageOut.of(successor_row)
