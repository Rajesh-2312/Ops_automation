"""The ERM sync queue's HTTP surface — CLAUDE.md §10, "manual by design".

    GET    /erm/tasks                    the queue, filtered
    POST   /erm/tasks                    file a card for a trainer or a program
    GET    /erm/tasks/{id}               one card, its LIVE field pack, its drift
    POST   /erm/tasks/{id}/assign        give it to a named person
    POST   /erm/tasks/{id}/confirm       they pasted it; freeze the pack, stamp
                                         erm_synced_at / erm_synced_by
    POST   /erm/tasks/{id}/cancel        withdraw an open card, with a reason

This router is I/O, authorisation and persistence. The rules are
`app/services/erm/`, which is pure: the ordered pack, the lifecycle edges, the
drift explanation. Nothing about §10 is decided here; this file takes the
`ErmOutcome` the service returns and lands both halves — the row and the audit
event — in ONE transaction (§11, `write_within()`).

THE FOUR SENTENCES WORTH READING BEFORE THE CODE
================================================
**Nothing here transmits anything, and there is no capability to add.** There is
no HTTP client in this module, no scraper, no browser driver, no ERM credential
and no config key naming one. `POST .../confirm` records that a named human says
they retyped a named set of values into a portal this system cannot reach. §10
forbids a scraper outright and R3 forbids any release capability anywhere near an
agent; both hold here structurally, because the code that would do the sending
does not exist.

**The pack is generated LIVE on every read of an open card, and frozen only at
confirm.** A card that sat in the queue for a week must hand over today's values,
not last Tuesday's — that is the whole failure §10 describes. `field_pack` is
NULL until somebody confirms, at which point the exact list they were shown is
stamped onto the row as evidence.

**Drift is the database's verdict, not this router's.** `erm_status` flips to
`stale` in `supabase/migrations/1900_erm_sync.sql`, inside whatever transaction
did the editing, on every connection — including the browser's direct PostgREST
writes, which §3 says are most of them. This file only ever *explains* drift, by
diffing the confirmed snapshot against the live pack. A `POST` that set staleness
would be a second detector that misses the common case.

**R5: no pack carries a commercial value, and the wall is not needed BECAUSE of
that.** Unlike `app/api/comms.py`, no endpoint here calls `require_commercials()`
— and that is a positive design decision, not an omission. Neither field order
names a rate, a bank rail or a P&L line, so there is nothing here for the wall to
protect, and `tests/unit/test_erm_fieldpack.py` asserts that by scanning the
declared sources. If a future field ever needs a rate, the wall goes in FIRST and
the pack second.

AUTHORISATION — THE APP-SIDE MIRROR OF 1900's THREE POLICIES
============================================================
`app/db/session.py` connects with BYPASSRLS, so none of 1900's policies runs for
us and every handler reproduces them (`app/core/security.py` explains why at
length). The three, in the same shape the SQL writes them:

    trainer card, write : app_role() in (senior_manager, manager)
                          and can_reach_trainer()
    trainer card, read  : is_internal() and can_reach_trainer()
    program card, any   : is_internal() and can_reach_program()

`can_reach_trainer()` is migration 2200's definition — "reachable, OR not yet
deployed anywhere". The second disjunct is the trainer split 0400 argued and 1900
restated: sourcing and onboarding precede deployment, so an educator engaged
nowhere must stay visible to the pipeline that is onboarding them. What 2200
added, and what `_authorise_trainer()` now reproduces, is that the carve-out ENDS
at the first deployment — a Manager with zero assignments used to authorise
against every trainer in the estate (SEC-05), and the pack carries PAN, email and
phone.

The persona half is the PERSONA test, never `can_see_commercials()`, even though
the two name the same two personas today — one means "may see money" and the
other means "owns the trainer pipeline", and reusing the money predicate for a
non-money purpose is how a wall gets moved by accident later.

ONE RULE, TWO EVALUATIONS — AND WHY THAT IS NOT TWO RULES
=========================================================
`GET /erm/tasks` needs that authorisation inside a `WHERE`, ahead of `LIMIT`;
every other handler needs it as a yes/no about one named subject. Those are two
*evaluations*. The failure mode to design against is that they quietly become two
*rules*, which is the drift this file's author refused to accept when the listing
was first written — and the reason it shipped filtering in Python after `LIMIT`
instead (SEC-08, below).

So the rule is decomposed into three pieces, each defined exactly ONCE and used
by both paths:

    `_trainer_colleges()`     the deployments -> batches -> programs -> colleges
                              walk. Executed by `_trainer_college_ids()`;
                              correlated into an `EXISTS` by `_visible_to()`.
    `_trainer_deployments()`  "is this trainer engaged anywhere at all". Executed
                              by `_trainer_is_deployed()`; `NOT EXISTS` in
                              `_visible_to()`.
    `_trainer_reach_rule()`   the boolean that combines them, and the only place
                              the sourcing carve-out is spelled. Called with
                              `bool`s by the guard and with SQL expressions by the
                              listing — the same function, because `|` means OR
                              on both.

`tests/unit/test_erm_list_tasks_limit.py` asserts that sharing directly: it spies
on all three and fails if either path stops going through them. Re-inlining the
walk or the boolean in either place turns a test red rather than turning the
counts silently wrong.

**The Python pass is still the wall.** `_visible_to()` is a pre-filter whose only
job is to make `LIMIT` count rows the caller may SEE. `_authorise()` still runs
over every row that comes back, so the worst a divergence in the SQL can do is
return too few rows — the very defect SEC-08 fixes — and never a row the guard
would have refused. That asymmetry is what makes the pre-filter safe to add.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any, Final, TypeVar
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import ColumnElement, Select, SQLColumnExpression, and_, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import AuditWriter, get_audit_writer
from app.core.security import CurrentPrincipal, Principal, require_internal
from app.db.models import Batch, College, Deployment, Program, Trainer
from app.db.session import get_session
from app.domain.enums import COMMERCIALS_PERSONAS, ErmStatus
from app.services.approval import Actor
from app.services.erm import (
    FIELD_ORDER_VERIFIED,
    FIELD_ORDER_VERSION,
    DriftedField,
    ErmError,
    ErmHumanActorRequiredError,
    ErmIllegalTransitionError,
    ErmOutcome,
    ErmSubjectKind,
    ErmSyncState,
    ErmSyncTask,
    FieldPack,
    ProgramFacts,
    SyncTask,
    TrainerFacts,
    assign_task,
    build_program_pack,
    build_trainer_pack,
    cancel_task,
    confirm_task,
    drifted_fields,
    queue_task,
)

router = APIRouter(prefix="/erm", tags=["erm"])

Session = Annotated[AsyncSession, Depends(get_session)]
Audit = Annotated[AuditWriter, Depends(get_audit_writer)]
TaskIdPath = Annotated[UUID, Path(description="`erm_sync_tasks.id`")]

#: Cap on a listing page. This queue is read by a person deciding what to retype
#: next; a request for ten thousand rows is a report, not a queue.
MAX_PAGE: Final[int] = 200

#: Personas that OWN the trainer pipeline — 0400's `trainers_sourcing_all`
#: policy, mirrored. Spelled from `COMMERCIALS_PERSONAS` would be wrong even
#: though the members coincide: see the module docstring. It is spelled from the
#: same frozenset only to avoid a second literal list of personas drifting from
#: `app/domain/enums.py`, and the alias name records that the MEANING differs.
TRAINER_PIPELINE_PERSONAS: Final[frozenset[object]] = COMMERCIALS_PERSONAS

#: Either a concrete subject id — the per-card guards, which know which record
#: they are asking about — or the queue row's own column, which is what turns the
#: shared statement builders below into correlated subqueries for `_visible_to()`.
#: One alias so those builders serve both callers without a second definition.
_SubjectRef = UUID | SQLColumnExpression[Any]

#: A truth value in whichever algebra the caller is working in: a Python `bool`
#: for the per-card guards, a SQL boolean expression for the listing's `WHERE`.
#: `|` is OR in both, which is the whole reason `_trainer_reach_rule()` can be one
#: function rather than two that have to be kept in step by hand.
_Truth = TypeVar("_Truth", bool, ColumnElement[bool])


# --- wire models ---------------------------------------------------------------


class QueueRequest(BaseModel):
    """File a job card for one record.

    Only the subject. There is nothing else to supply: the pack is generated from
    the record, the order is declared in `app/services/erm/fieldpack.py`, and the
    assignee is a separate act because §10 separates them ("assigns it to a named
    person" is its own step, and a card can legitimately sit unassigned in a
    queue).
    """

    model_config = ConfigDict(extra="forbid")

    subject_kind: ErmSubjectKind
    subject_id: UUID = Field(description="`trainers.id` or `programs.id`, per `subject_kind`.")


class AssignRequest(BaseModel):
    """§10: "assigns it to a named person"."""

    model_config = ConfigDict(extra="forbid")

    assignee_id: UUID = Field(description="`profiles.id` of the person who will paste it.")


class ConfirmRequest(BaseModel):
    """ "They paste, they confirm" — the human's claim, with what came back.

    Every field mirrors a column of the legacy ERM update logs, which are the
    only real evidence of how this step was recorded before:
    `erm_external_id` is their "ERM Trainer ID" / "ERM Program ID", `verified` is
    their "Verified?", `remarks` is their "Remarks".

    The pack is NOT in this request. It is regenerated server-side at confirm
    time from the same source record and frozen onto the row — a client-supplied
    pack would let the row attest to values the database never held, which is R1
    read backwards.
    """

    model_config = ConfigDict(extra="forbid")

    erm_external_id: str | None = Field(
        default=None,
        description="The record id read back off ERM's own screen, if there is one.",
    )
    verified: bool = Field(
        default=False,
        description="Whether the pasted values were read back and checked in ERM.",
    )
    remarks: str | None = None


class CancelRequest(BaseModel):
    """Withdraw an open card. The reason is required, not decorative.

    A cancelled ERM push is a decision that a record deliberately does not match
    ERM — which is the divergence §10 is about. The reason is what stops it
    reading as an oversight to whoever finds it in three months.
    """

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1)


class PackEntryOut(BaseModel):
    """One line of the pack, as the screen shows it and the human retypes it."""

    model_config = ConfigDict(frozen=True)

    label: str
    #: Dotted local origin. On the wire because a person about to retype a value
    #: into a third-party portal is entitled to know which column it came from.
    source: str
    value: str
    #: True when the source is NULL or blank. Carried separately so a screen can
    #: say "leave this field alone" without the pack containing a word like "N/A"
    #: that must never be pasted.
    is_blank: bool


class FieldPackOut(BaseModel):
    """The ordered field-value list §10 asks for, plus its own health warning."""

    model_config = ConfigDict(frozen=True)

    subject_kind: ErmSubjectKind
    entries: tuple[PackEntryOut, ...]
    #: Tab-separated `label<TAB>value` lines — pastes into a textarea and into a
    #: spreadsheet column alike, which matters because the legacy record of every
    #: one of these pastes is a spreadsheet.
    paste_text: str
    field_order_version: int
    #: **False, today and until somebody opens ERM and writes the order down.**
    #: On the wire so the screen can say so; see `app/services/erm/fieldpack.py`.
    field_order_verified: bool

    @classmethod
    def of(cls, pack: FieldPack) -> FieldPackOut:
        return cls(
            subject_kind=pack.subject_kind,
            entries=tuple(
                PackEntryOut(
                    label=entry.label,
                    source=entry.source,
                    value=entry.value,
                    is_blank=entry.is_blank,
                )
                for entry in pack.entries
            ),
            paste_text=pack.as_paste_text(),
            field_order_version=pack.field_order_version,
            field_order_verified=pack.field_order_verified,
        )


class DriftedFieldOut(BaseModel):
    """One field that has moved since the confirmed sync.

    `was` is `None` when the field did not exist in the stored snapshot at all —
    the pack gained a field between one sync and the next. Distinct from "it was
    blank", and collapsing the two would report a never-sent field as unchanged.
    """

    model_config = ConfigDict(frozen=True)

    label: str
    source: str
    was: str | None
    now: str

    @classmethod
    def of(cls, drifted: DriftedField) -> DriftedFieldOut:
        return cls(label=drifted.label, source=drifted.source, was=drifted.was, now=drifted.now)


class ErmTaskOut(BaseModel):
    """One job card on the wire."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    subject_kind: ErmSubjectKind
    subject_id: UUID
    #: Trainer name or program name. On the list so the queue is legible without
    #: a second request per row; a queue of bare UUIDs is not a queue.
    subject_label: str
    state: ErmSyncState

    field_order_version: int
    assigned_to: UUID | None = None
    assigned_at: dt.datetime | None = None

    erm_external_id: str | None = None
    verified: bool = False
    remarks: str | None = None

    #: §10: "Record carries erm_synced_at, erm_synced_by". On the card these are
    #: `confirmed_*`; the source record carries the columns of those names, and
    #: the confirm handler stamps both in the same transaction.
    confirmed_by: UUID | None = None
    confirmed_at: dt.datetime | None = None

    #: Set by the 1900 triggers, never by this service.
    stale_at: dt.datetime | None = None
    stale_reason: str | None = None
    supersedes_id: UUID | None = None

    cancelled_at: dt.datetime | None = None
    cancelled_reason: str | None = None

    created_at: dt.datetime
    updated_at: dt.datetime

    @classmethod
    def of(cls, row: ErmSyncTask, subject_label: str) -> ErmTaskOut:
        subject_id = row.trainer_id if row.trainer_id is not None else row.program_id
        assert subject_id is not None  # noqa: S101 - 1900's subject CHECK guarantees it
        return cls(
            id=row.id,
            subject_kind=row.subject_kind,
            subject_id=subject_id,
            subject_label=subject_label,
            state=row.state,
            field_order_version=row.field_order_version,
            assigned_to=row.assigned_to,
            assigned_at=row.assigned_at,
            erm_external_id=row.erm_external_id,
            verified=row.verified,
            remarks=row.remarks,
            confirmed_by=row.confirmed_by,
            confirmed_at=row.confirmed_at,
            stale_at=row.stale_at,
            stale_reason=row.stale_reason,
            supersedes_id=row.supersedes_id,
            cancelled_at=row.cancelled_at,
            cancelled_reason=row.cancelled_reason,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class ErmTaskDetail(BaseModel):
    """One card with everything the person doing the paste needs.

    The pack is the LIVE one for an open card and the FROZEN one for a card that
    has been confirmed — see the module docstring. `drift` is empty unless the
    card was confirmed and the record has moved since.
    """

    model_config = ConfigDict(frozen=True)

    task: ErmTaskOut
    pack: FieldPackOut
    #: Which fields moved since the confirmed sync. May legitimately be empty on
    #: a card the database marked stale (a value edited and edited back), and
    #: that is not licence to clear the flag.
    drift: tuple[DriftedFieldOut, ...] = ()
    #: True when the pack shown is the one frozen at confirm rather than one
    #: generated from the record as it reads now.
    pack_is_frozen: bool = False


class ErmQueue(BaseModel):
    """A page of the queue, oldest first — the order somebody working it wants."""

    model_config = ConfigDict(frozen=True)

    tasks: tuple[ErmTaskOut, ...]
    #: Repeated at the collection level so a client cannot render a queue without
    #: having been told the order is provisional.
    field_order_version: int = FIELD_ORDER_VERSION
    field_order_verified: bool = FIELD_ORDER_VERIFIED


# --- lookups -------------------------------------------------------------------


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def _trainer_colleges(trainer_id: _SubjectRef) -> Select[tuple[UUID]]:
    """The colleges a trainer teaches at — the reach half of `can_reach_trainer()`.

    The same walk 0400's SQL helper does —
    `deployments -> batches -> programs -> colleges` — because the reach must be
    the same reach. Note what it inherits: a sourced-but-undeployed trainer
    reaches nobody, which is why migration 2200 pairs this with the "engaged
    nowhere" carve-out rather than using it alone (`_can_reach_trainer()`).

    A statement rather than a result, because it has two callers and they must
    not be two walks: `_trainer_college_ids()` executes it against one id, and
    `_visible_to()` passes `ErmSyncTask.trainer_id` so SQLAlchemy correlates it
    into an `EXISTS` against the queue row. Same joins, same `WHERE`, one place.
    """
    return (
        select(College.id)
        .join(Program, Program.college_id == College.id)
        .join(Batch, Batch.program_id == Program.id)
        .join(Deployment, Deployment.batch_id == Batch.id)
        .where(Deployment.trainer_id == trainer_id)
    )


def _trainer_deployments(trainer_id: _SubjectRef) -> Select[tuple[UUID]]:
    """Every deployment row this trainer has — the carve-out's half of the rule.

    Asked of `deployments` directly rather than inferred from
    `_trainer_colleges()` returning nothing, and the difference is load-bearing:
    a deployment whose batch or program row has gone missing drops out of that
    walk, and reading "no colleges" as "not deployed" would turn a broken join
    into an open door. 2200's SQL asks the same question the same way —
    `not exists (select 1 from public.deployments d where d.trainer_id = ...)`.
    """
    return select(Deployment.id).where(Deployment.trainer_id == trainer_id)


def _trainer_reach_rule(reachable: _Truth, undeployed: _Truth, *, carve_out: bool) -> _Truth:
    """`can_reach_trainer()`'s boolean, and the ONLY place it is spelled.

    2200, restated: reachable, OR — for a caller who gets the sourcing carve-out —
    engaged nowhere at all. `carve_out` is a plain Python `bool` because it is a
    property of the CALLER, fixed for a whole request, so it can branch at build
    time in both algebras:

    * the pipeline personas get both disjuncts. Onboarding precedes deployment,
      so a freshly sourced educator must stay visible to the people onboarding
      them;
    * an LDE Executive gets only the first. An educator engaged nowhere is on no
      campus, so "is this trainer on my campus synced" is not a question that has
      an answer for them, and refusing is fail-closed (`_authorise_trainer()`).

    `|` is OR for a `bool` and OR for a SQL expression, which is what lets the
    guard and the listing share this function instead of keeping two boolean
    shapes in step by hand. `~` is not used: it is bitwise NOT on a `bool` and
    would silently yield `-2`, so the negation is done by the caller and handed
    in as `undeployed`.
    """
    if carve_out:
        return reachable | undeployed
    return reachable


async def _trainer_college_ids(session: AsyncSession, trainer_id: UUID) -> set[UUID]:
    """`_trainer_colleges()`, run for one trainer."""
    return set((await session.execute(_trainer_colleges(trainer_id))).scalars().all())


async def _trainer_is_deployed(session: AsyncSession, trainer_id: UUID) -> bool:
    """True when this trainer has ANY deployment row at all."""
    statement = _trainer_deployments(trainer_id).limit(1)
    return bool((await session.execute(statement)).scalars().all())


async def _trainer_college_names(session: AsyncSession, trainer_id: UUID) -> tuple[str, ...]:
    """The pack's derived "College Assigned" value, ordered by name.

    Ordered in SQL rather than in Python so the value is stable across reads: an
    unordered set rendered into a comma-separated string would make a pack look
    drifted every time Postgres felt like returning the rows differently.
    """
    statement = (
        select(College.name)
        .join(Program, Program.college_id == College.id)
        .join(Batch, Batch.program_id == Program.id)
        .join(Deployment, Deployment.batch_id == Batch.id)
        .where(Deployment.trainer_id == trainer_id)
        .distinct()
        .order_by(College.name)
    )
    return tuple((await session.execute(statement)).scalars().all())


async def _trainer(session: AsyncSession, trainer_id: UUID) -> Trainer:
    row = await session.get(Trainer, trainer_id)
    if row is None:
        raise _not_found(f"No trainer with id {trainer_id}")
    return row


async def _program(session: AsyncSession, program_id: UUID) -> Program:
    row = await session.get(Program, program_id)
    if row is None:
        raise _not_found(f"No program with id {program_id}")
    return row


# --- authorisation: the app-side mirror of 1900's three policies -----------------


def _owns_trainer_pipeline(principal: Principal) -> bool:
    """0400's `app_role() in ('senior_manager','manager')`, in code.

    Not `principal.can_see_commercials` even though it would return the same
    answer today. The module docstring has the argument; the short version is
    that the two predicates mean different things and will one day disagree.
    """
    return principal.persona in TRAINER_PIPELINE_PERSONAS


async def _can_reach_trainer(
    session: AsyncSession, principal: Principal, trainer_id: UUID, *, carve_out: bool = True
) -> bool:
    """App-side `public.can_reach_trainer(uuid)` as migrations 2200/2500 define it.

    "Reachable, or not yet deployed anywhere" — both disjuncts, in 2200's order
    of intent:

    * the caller covers a college where this trainer is engaged; or
    * the trainer is engaged NOWHERE, which is the sourcing carve-out. Onboarding
      precedes deployment, so a predicate that demanded plain reachability would
      make a freshly sourced educator invisible to the very people whose job is
      to onboard them — and you cannot file an ERM card for somebody you cannot
      see.

    The moment a trainer is deployed they belong to whoever covers that college,
    which is the half that was missing and is the whole of SEC-05.

    `carve_out` defaults to True because that is what the SQL helper this mirrors
    always does. `_authorise_trainer()` passes False for the LDE Executive's read,
    which is deliberately stricter — see that function.

    Both facts are fetched here and combined by `_trainer_reach_rule()` rather
    than by an `or` written in place, so the listing's `WHERE` and this guard
    cannot come to disagree about what the rule IS. The two `and`s below are the
    short-circuit the `or` used to provide: the deployments query is skipped when
    the walk already said yes, and skipped entirely when there is no carve-out to
    apply.
    """
    reachable = bool(await _trainer_college_ids(session, trainer_id) & principal.college_ids)
    undeployed = carve_out and not reachable and not await _trainer_is_deployed(session, trainer_id)
    return _trainer_reach_rule(reachable, undeployed, carve_out=carve_out)


async def _authorise_trainer(
    session: AsyncSession, principal: Principal, trainer_id: UUID, *, write: bool
) -> None:
    """Trainer cards: pipeline personas write, reaching staff read. Reach, always.

    Persona is checked before any query, so a college login probing task ids gets
    the same 403 for every id and learns nothing about which exist.

    SEC-05 — WHY THE PIPELINE PERSONA IS NOT A FREE PASS
    ----------------------------------------------------
    This guard used to `return` the moment the caller held a pipeline persona,
    mirroring 1900's `erm_sync_tasks_sourcing_all` as it was originally written.
    That policy checked the persona and skipped the reach, so a Manager with ZERO
    assignments authorised against ANY `trainer_id` — and `_live_pack()` hands
    back `full_name`, `pan`, `email` and `phone`. CLAUDE.md §4 is explicit that
    the two are different things: "Persona lives on `profiles.role`. Reach does
    not."

    Migration 2200 added the missing conjunct in SQL. It is reproduced here
    because `app/db/session.py` connects with BYPASSRLS: no policy on this table
    runs for a FastAPI request, so this function is the wall and nothing behind it
    is.

    The LDE Executive's read keeps the STRICTER predicate — a college the trainer
    is actually deployed to, with no undeployed carve-out. 2200 widened
    `can_reach_trainer()` for everyone, which incidentally widened 1900's
    `erm_sync_tasks_lde_select_trainer`; an undeployed educator is on no campus,
    so "is this trainer on my campus synced" is not a question that has an answer
    for them. Refusing is fail-closed and is what this endpoint already did.
    """
    require_internal(principal)

    if not _owns_trainer_pipeline(principal):
        if write:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "Pushing a trainer to ERM is trainer-pipeline work: Senior Manager "
                    "or Manager. An LDE Executive may read the ERM state of trainers on "
                    "their campus."
                ),
            )
        if not await _can_reach_trainer(session, principal, trainer_id, carve_out=False):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have access to this trainer",
            )
        return

    if not await _can_reach_trainer(session, principal, trainer_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this trainer",
        )


async def _authorise_program(
    session: AsyncSession, principal: Principal, program_id: UUID, *, write: bool
) -> None:
    """Program cards: `is_internal() and can_reach_program()`, both verbs.

    `write` is accepted and unused, deliberately: 1900's program policy is a
    single `for all`, so read and write take the identical predicate, and a
    parameter that silently did nothing would be worse than one whose signature
    matches its trainer counterpart and whose body says why.
    """
    require_internal(principal)
    program = await _program(session, program_id)
    if not principal.can_reach_college(program.college_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this college",
        )


async def _authorise(
    session: AsyncSession,
    principal: Principal,
    kind: ErmSubjectKind,
    subject_id: UUID,
    *,
    write: bool,
) -> None:
    """Dispatch to the policy for this subject. One call site per handler."""
    if kind is ErmSubjectKind.TRAINER:
        await _authorise_trainer(session, principal, subject_id, write=write)
    else:
        await _authorise_program(session, principal, subject_id, write=write)


def _visible_to(principal: Principal) -> ColumnElement[bool]:
    """`_authorise(..., write=False)` as a `WHERE`, for `list_tasks()` only.

    The same rule the guards above apply one subject at a time, expressed over the
    queue row so Postgres can apply it BEFORE `LIMIT`. It is built from the same
    three pieces — `_trainer_colleges()`, `_trainer_deployments()`,
    `_trainer_reach_rule()` — for the reason the module docstring gives: two
    evaluations of one rule, never two rules.

    Read it against 1900's three policies, which is the shape it takes:

        trainer card : app_role() in (pipeline) ? reachable or undeployed
                                                : reachable
        program card : the program exists and its college is in reach

    The caller's persona is checked by `require_internal()` before this is built,
    so `carve_out` is the only persona-dependent term left and the two internal
    personas that reach here are exactly "pipeline" and "LDE Executive".

    THE NULL GUARDS ARE MIGRATION 2500, NOT DECORATION
    --------------------------------------------------
    `not exists (... where d.trainer_id = null)` is TRUE — the carve-out branch
    answers "engaged nowhere, therefore visible" for a row that names no trainer
    at all. 1900's `erm_sync_tasks_subject_ck` means a `subject_kind = 'trainer'`
    row always has one, so the `subject_kind` conjunct already holds the line;
    2500's whole argument is that relying on a column constraint to keep a reach
    predicate honest is how the next caller gets hurt. Both `IS NOT NULL`s are
    therefore written out. The program branch is naturally fail-closed — an
    `EXISTS` on `programs.id = null` matches nothing — and says so anyway.

    `_authorise_program()` answers 404 for a program row that has gone missing and
    403 for one out of reach; both are invisible in a listing, so the single
    `EXISTS` here covers the pair. That is the one conjunct of the rule this
    function restates rather than shares, because the per-card guard needs the
    two apart and a listing cannot use the difference.
    """
    colleges = sorted(principal.college_ids)
    reachable = _trainer_colleges(ErmSyncTask.trainer_id).where(College.id.in_(colleges)).exists()
    undeployed = ~_trainer_deployments(ErmSyncTask.trainer_id).exists()

    trainer_card = and_(
        ErmSyncTask.subject_kind == ErmSubjectKind.TRAINER,
        ErmSyncTask.trainer_id.is_not(None),
        _trainer_reach_rule(reachable, undeployed, carve_out=_owns_trainer_pipeline(principal)),
    )
    program_card = and_(
        ErmSyncTask.subject_kind == ErmSubjectKind.PROGRAM,
        ErmSyncTask.program_id.is_not(None),
        select(Program.id)
        .where(Program.id == ErmSyncTask.program_id, Program.college_id.in_(colleges))
        .exists(),
    )
    return or_(trainer_card, program_card)


def _subject_id(row: ErmSyncTask) -> UUID:
    """The card's subject, whichever column holds it.

    1900's `erm_sync_tasks_subject_ck` guarantees exactly one is set and that it
    agrees with `subject_kind`, so a row where both are NULL is a schema
    violation and a 500 rather than something to route around.
    """
    subject = row.trainer_id if row.subject_kind is ErmSubjectKind.TRAINER else row.program_id
    if subject is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                f"erm_sync_tasks row {row.id} has no subject for kind "
                f"'{row.subject_kind.value}', which erm_sync_tasks_subject_ck "
                "should have made impossible."
            ),
        )
    return subject


async def _row(
    session: AsyncSession, task_id: UUID, principal: Principal, *, write: bool
) -> ErmSyncTask:
    """One card the caller is entitled to, or 403/404.

    Fetched, then authorised, then returned — `app/api/comms.py`'s order and its
    reasoning: a 404 for a card outside the caller's reach would leak the id
    space, and a 403 is what the SQL policies produce (zero rows) once translated
    into an app that has already read the row on a BYPASSRLS connection.
    """
    row = await session.get(ErmSyncTask, task_id)
    if row is None:
        raise _not_found(f"No ERM sync task with id {task_id}")
    await _authorise(session, principal, row.subject_kind, _subject_id(row), write=write)
    return row


# --- pack generation -------------------------------------------------------------


async def _live_pack(session: AsyncSession, kind: ErmSubjectKind, subject_id: UUID) -> FieldPack:
    """The pack as the record reads RIGHT NOW.

    Every value comes from a column of a system of record and is passed into a
    pure builder as structured input — R1, in the narrow sense that matters here:
    nothing in this system may assert an ERM field value it did not read from
    SQL, and there is no other way for one to arrive.
    """
    if kind is ErmSubjectKind.TRAINER:
        trainer = await _trainer(session, subject_id)
        return build_trainer_pack(
            TrainerFacts(
                full_name=trainer.full_name,
                pan=trainer.pan,
                email=trainer.email,
                phone=trainer.phone,
                trainer_type=str(trainer.type),
                work_order_status=str(trainer.work_order_status),
                zoho_id=trainer.zoho_id,
                colleges=await _trainer_college_names(session, subject_id),
            )
        )
    program = await _program(session, subject_id)
    college = await session.get(College, program.college_id)
    return build_program_pack(
        ProgramFacts(
            college_name=college.name if college is not None else "",
            name=program.name,
            program_type=str(program.type),
            start_date=program.start_date,
            end_date=program.end_date,
        )
    )


async def _subject_label(session: AsyncSession, kind: ErmSubjectKind, subject_id: UUID) -> str:
    """A human-readable name for the queue row.

    Falls back to the id rather than raising: a card whose subject was deleted
    should still be listable, because the alternative is a queue that 500s
    because of one dead row.
    """
    if kind is ErmSubjectKind.TRAINER:
        trainer = await session.get(Trainer, subject_id)
        return trainer.full_name if trainer is not None else str(subject_id)
    program = await session.get(Program, subject_id)
    return program.name if program is not None else str(subject_id)


# --- the service <-> row bridge ---------------------------------------------------


def _task(row: ErmSyncTask) -> SyncTask:
    """The pure `SyncTask` the lifecycle operates on, from one stored row.

    `SyncTask.__post_init__` enforces the same invariants 1900 states as CHECK
    constraints, so a row that satisfies the database always satisfies it. A
    `ValueError` here therefore means the two have diverged: a 500, because the
    caller did nothing wrong and there is nothing they can change.
    """
    try:
        return SyncTask(
            task_id=row.id,
            subject_kind=row.subject_kind,
            subject_id=_subject_id(row),
            state=row.state,
            field_order_version=row.field_order_version,
            assigned_to=row.assigned_to,
            assigned_at=row.assigned_at,
            confirmed_by=row.confirmed_by,
            confirmed_at=row.confirmed_at,
            cancelled_at=row.cancelled_at,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                f"erm_sync_tasks row {row.id} is internally inconsistent and cannot "
                f"be loaded into the §10 lifecycle: {exc}"
            ),
        ) from exc


def _apply(row: ErmSyncTask, task: SyncTask, at: dt.datetime) -> None:
    """Copy the lifecycle's result onto the stored row. Only what it moved.

    `stale_at`, `stale_reason` and `supersedes_id` are absent on purpose: those
    columns belong to the 1900 triggers, and a router that also wrote them would
    be a second detector racing the first.
    """
    row.state = task.state
    row.field_order_version = task.field_order_version
    row.assigned_to = task.assigned_to
    row.assigned_at = task.assigned_at
    row.confirmed_by = task.confirmed_by
    row.confirmed_at = task.confirmed_at
    row.cancelled_at = task.cancelled_at
    row.updated_at = at


async def _persist(
    session: AsyncSession,
    audit: AuditWriter,
    row: ErmSyncTask,
    outcome: ErmOutcome,
    at: dt.datetime,
) -> ErmTaskOut:
    """Land the transition and its audit row in ONE transaction.

    `write_within()` and not `write()`. `app/core/audit.py`'s rule of thumb is
    "if losing the audit row would leave a money or approval decision
    unattributable"; a confirmation is neither, and it is still exactly that kind
    of record — the row's entire value is the attribution. A confirmed sync whose
    audit event silently vanished is a claim with no claimant.
    """
    _apply(row, outcome.task, at)
    await audit.write_within(session, outcome.event)
    await session.commit()
    label = await _subject_label(session, row.subject_kind, _subject_id(row))
    return ErmTaskOut.of(row, label)


def _actor(principal: Principal) -> Actor:
    """The authenticated human behind the request.

    `actor_id` is never `None` on this path — `CurrentPrincipal` is resolved from
    a verified JWT and a live `profiles` row — which is why no route here can be
    satisfied by an agent or a scheduled job. `ErmHumanActorRequiredError` is
    still mapped below, because a guard that is unreachable today is a guard that
    keeps working when somebody adds a service principal tomorrow.
    """
    return Actor(actor_id=principal.user_id, persona=principal.persona)


def _refuse(exc: ErmError) -> HTTPException:
    """Map a §10 refusal onto the status code that tells the truth."""
    if isinstance(exc, ErmHumanActorRequiredError):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    if isinstance(exc, ErmIllegalTransitionError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    raise exc  # pragma: no cover - a refusal this module does not know


# --- endpoints ---------------------------------------------------------------------


@router.post(
    "/tasks",
    response_model=ErmTaskOut,
    status_code=status.HTTP_201_CREATED,
    summary="File an ERM sync task for a trainer or a program",
)
async def create_task(
    request: QueueRequest,
    principal: CurrentPrincipal,
    session: Session,
    audit: Audit,
) -> ErmTaskOut:
    """Queue the card. Unassigned, no pack yet, nothing sent.

    The subject is read before anything else, so filing a card for a trainer who
    does not exist is a 404 rather than a row nobody can ever open.

    Refuses with 409 if the record already has an open card. That mirrors the
    partial unique index in 1900, and the index exists for §10's sake: a record
    edited five times in an afternoon must produce one job, not five, or the
    queue becomes noise and the noise is what gets ignored.
    """
    await _authorise(session, principal, request.subject_kind, request.subject_id, write=True)
    # Existence, before anything else. Also the first half of what makes the
    # 404-vs-403 ordering above meaningful.
    if request.subject_kind is ErmSubjectKind.TRAINER:
        await _trainer(session, request.subject_id)
    else:
        await _program(session, request.subject_id)

    subject_column = (
        ErmSyncTask.trainer_id
        if request.subject_kind is ErmSubjectKind.TRAINER
        else ErmSyncTask.program_id
    )
    open_rows = (
        (
            await session.execute(
                select(ErmSyncTask).where(
                    subject_column == request.subject_id,
                    ErmSyncTask.state.in_([ErmSyncState.QUEUED, ErmSyncState.ASSIGNED]),
                )
            )
        )
        .scalars()
        .all()
    )
    if open_rows:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"This {request.subject_kind.value} already has an open ERM sync "
                f"task ({open_rows[0].id}). One open card per record, so that a "
                "record edited repeatedly produces one job rather than a queue "
                "nobody reads (CLAUDE.md §10)."
            ),
        )

    now = dt.datetime.now(dt.UTC)
    row = ErmSyncTask(
        id=uuid.uuid4(),
        subject_kind=request.subject_kind,
        trainer_id=(request.subject_id if request.subject_kind is ErmSubjectKind.TRAINER else None),
        program_id=(request.subject_id if request.subject_kind is ErmSubjectKind.PROGRAM else None),
        state=ErmSyncState.QUEUED,
        field_order_version=FIELD_ORDER_VERSION,
        verified=False,
        created_by=principal.user_id,
        created_at=now,
        updated_at=now,
    )
    session.add(row)

    try:
        outcome = queue_task(_task(row), _actor(principal), now)
    except ErmError as exc:
        raise _refuse(exc) from exc
    return await _persist(session, audit, row, outcome, now)


@router.get(
    "/tasks",
    response_model=ErmQueue,
    status_code=status.HTTP_200_OK,
    summary="The ERM sync queue, oldest first",
)
async def list_tasks(
    principal: CurrentPrincipal,
    session: Session,
    state: Annotated[ErmSyncState | None, Query(description="Filter by card state.")] = None,
    subject_kind: Annotated[
        ErmSubjectKind | None, Query(description="Trainer cards or program cards.")
    ] = None,
    assigned_to_me: Annotated[
        bool, Query(description="Only cards assigned to the caller.")
    ] = False,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 50,
) -> ErmQueue:
    """What is waiting to be retyped, across every record the caller reaches.

    Deliberately NOT scoped to one program the way `GET /comms/messages` is. The
    person who works this queue works it across their whole reach — that is what
    a queue is for — so the scope comes from the caller's assignments and the
    wall is applied per row, exactly as 1900's three policies do it in SQL.

    Oldest first, because a card that has been queued for three weeks is the one
    that matters. A pure read, so no audit row.

    SEC-08 — THE REACH GOES IN THE STATEMENT, NOT AFTER IT
    ------------------------------------------------------
    The filter used to run in Python over rows the database had ALREADY limited,
    which quietly made `limit` mean "rows considered" rather than "rows returned":
    a caller asking for fifty cards received fewer than fifty while more were
    waiting behind the ones dropped. Under-disclosure rather than a leak — no
    unreachable card was ever returned — but a queue that hides work is a queue
    people stop trusting, and this one exists to tell somebody what to retype
    next. It was already true for an LDE Executive; SEC-05 gave a Manager and a
    Senior Manager reach they can fall outside of too, so it stopped being one
    persona's problem.

    So `_visible_to()` goes into the `WHERE`, ahead of `ORDER BY ... LIMIT`, which
    is where 1900's three policies would have applied it if RLS ran on this
    connection at all. It is built from the same walk, the same existence check
    and the same boolean the per-card guards use, so it is one rule evaluated
    twice rather than a second copy — see the module docstring, and
    `tests/unit/test_erm_list_tasks_limit.py`, which fails if either path stops
    going through them.

    The Python pass is kept BELOW as a backstop rather than replaced. On a
    BYPASSRLS connection it is the assertion that no unreachable card reaches the
    response whatever the statement does, and it costs one memoised verdict per
    distinct subject on the page. It also keeps the failure asymmetric: a
    `_visible_to()` that is ever too WIDE returns rows the guard then drops (too
    few cards, the defect above), never rows the guard would have refused.
    """
    require_internal(principal)

    statement = select(ErmSyncTask).where(_visible_to(principal))
    if state is not None:
        statement = statement.where(ErmSyncTask.state == state)
    if subject_kind is not None:
        statement = statement.where(ErmSyncTask.subject_kind == subject_kind)
    if assigned_to_me:
        statement = statement.where(ErmSyncTask.assigned_to == principal.user_id)
    rows = (
        (await session.execute(statement.order_by(ErmSyncTask.created_at).limit(limit)))
        .scalars()
        .all()
    )

    # One verdict per SUBJECT, not per row. A record edited repeatedly carries one
    # open card and a tail of confirmed and stale ones, so a page is routinely
    # several cards about the same trainer — and since SEC-05 each trainer verdict
    # costs up to two queries where a pipeline persona used to cost none. Same
    # answer, asked once: `_authorise()` is a pure function of persona, reach and
    # subject within a request, all three of which are fixed for its duration.
    verdicts: dict[tuple[ErmSubjectKind, UUID], bool] = {}
    visible: list[ErmTaskOut] = []
    for row in rows:
        subject = _subject_id(row)
        key = (row.subject_kind, subject)
        if key not in verdicts:
            try:
                await _authorise(session, principal, row.subject_kind, subject, write=False)
            except HTTPException:
                # Per-row filtering rather than a refusal for the whole request: a
                # Manager and an LDE Executive both legitimately read this queue and
                # should see different rows in it, which is what the SQL policies do.
                verdicts[key] = False
            else:
                verdicts[key] = True
        if not verdicts[key]:
            continue
        visible.append(ErmTaskOut.of(row, await _subject_label(session, row.subject_kind, subject)))
    return ErmQueue(tasks=tuple(visible))


@router.get(
    "/tasks/{task_id}",
    response_model=ErmTaskDetail,
    status_code=status.HTTP_200_OK,
    summary="One card, its field pack, and what has drifted since it was synced",
)
async def read_task(
    task_id: TaskIdPath,
    principal: CurrentPrincipal,
    session: Session,
) -> ErmTaskDetail:
    """The paste screen's payload — §10's "generated field pack", in order.

    For an OPEN card the pack is generated live from the record, so what is on
    screen is what the database holds at this instant. For a CONFIRMED or STALE
    card the frozen pack is returned instead: that row is evidence of what was
    pasted, and regenerating it would quietly rewrite the evidence.

    On a stale card, `drift` names the fields that moved. It can legitimately be
    empty — a value edited and edited back still marks the record stale, and the
    database is conservative on purpose. See `app/services/erm/drift.py`.

    A pure read, so no audit row.
    """
    row = await _row(session, task_id, principal, write=False)
    subject = _subject_id(row)
    live = await _live_pack(session, row.subject_kind, subject)

    frozen = row.field_pack is not None
    pack = FieldPackOut.of(live)
    if frozen and row.field_pack is not None:
        pack = FieldPackOut(
            subject_kind=row.subject_kind,
            entries=tuple(
                PackEntryOut(
                    label=str(entry.get("label", "")),
                    source=str(entry.get("source", "")),
                    value=str(entry.get("value", "")),
                    is_blank=bool(entry.get("is_blank", False)),
                )
                for entry in row.field_pack
            ),
            paste_text="\n".join(
                f"{entry.get('label', '')}\t{entry.get('value', '')}" for entry in row.field_pack
            ),
            field_order_version=row.field_order_version,
            field_order_verified=FIELD_ORDER_VERIFIED,
        )

    drift = drifted_fields(row.source_snapshot, live)
    return ErmTaskDetail(
        task=ErmTaskOut.of(row, await _subject_label(session, row.subject_kind, subject)),
        pack=pack,
        drift=tuple(DriftedFieldOut.of(item) for item in drift),
        pack_is_frozen=frozen,
    )


@router.post(
    "/tasks/{task_id}/assign",
    response_model=ErmTaskOut,
    status_code=status.HTTP_200_OK,
    summary="Assign the card to a named person (§10)",
)
async def assign(
    task_id: TaskIdPath,
    request: AssignRequest,
    principal: CurrentPrincipal,
    session: Session,
    audit: Audit,
) -> ErmTaskOut:
    """§10: "assigns it to a named person".

    Reassignment is allowed and is not an exception path — people go on leave and
    work moves teams. Forcing a cancel-and-refile for it would fork the history
    of one push across two rows and lose the connection between them.
    """
    row = await _row(session, task_id, principal, write=True)
    now = dt.datetime.now(dt.UTC)
    try:
        outcome = assign_task(_task(row), _actor(principal), request.assignee_id, now)
    except ErmError as exc:
        raise _refuse(exc) from exc
    row.assigned_by = principal.user_id
    return await _persist(session, audit, row, outcome, now)


@router.post(
    "/tasks/{task_id}/confirm",
    response_model=ErmTaskOut,
    status_code=status.HTTP_200_OK,
    summary="Record that a named person pasted the pack into ERM",
)
async def confirm(
    task_id: TaskIdPath,
    request: ConfirmRequest,
    principal: CurrentPrincipal,
    session: Session,
    audit: Audit,
) -> ErmTaskOut:
    """§10: "they paste, they confirm". Freezes the pack, stamps the record.

    **This transmits nothing.** It records a claim: this person says they typed
    these exact values into ERM. That is the strongest true statement available
    about a system with no API, and dressing it up as a synchronisation would be
    a lie the whole design exists to avoid.

    Three things happen in one transaction:

    1. The pack is regenerated from the record and frozen onto the card, with the
       source values beside it. Regenerated rather than taken from the request
       body: a client-supplied pack could attest to values the database never
       held (R1 backwards). It is generated from the same rows the GET rendered
       microseconds earlier, so the only way the two differ is a concurrent edit
       — which the drift triggers will immediately mark stale, correctly.
    2. `erm_synced_at` / `erm_synced_by` are stamped on the SOURCE record, which
       is where §10 says they go, along with `erm_status = 'synced'` and any ERM
       id read back.
    3. The card moves to CONFIRMED and the audit event lands with it.

    Step 2 is raw parameterised SQL rather than the ORM, and the reason is
    recorded rather than hidden: `programs`' five `erm_*` columns are added by
    migration 1900 and `app/db/models.py` — which is owned by another workstream
    — has no mapped attribute for them yet. One parameterised statement per
    subject keeps the trainer path and the program path identical instead of one
    being ORM and the other not. When those columns are mapped this becomes two
    attribute assignments and the SQL goes away.
    """
    row = await _row(session, task_id, principal, write=True)
    subject = _subject_id(row)
    now = dt.datetime.now(dt.UTC)
    pack = await _live_pack(session, row.subject_kind, subject)

    try:
        outcome = confirm_task(_task(row), _actor(principal), pack, now)
    except ErmError as exc:
        raise _refuse(exc) from exc

    row.field_pack = pack.as_json()
    row.source_snapshot = pack.source_snapshot()
    row.erm_external_id = request.erm_external_id
    row.verified = request.verified
    row.remarks = request.remarks

    # The watch lists in 1900 do NOT include any of these columns, so this
    # statement cannot mark the record it just synced as stale. That is the
    # single most important property of the drift design and it is asserted by
    # tests/unit/test_erm_drift.py rather than left to this comment.
    table = "trainers" if row.subject_kind is ErmSubjectKind.TRAINER else "programs"
    await session.execute(
        text(
            f"update public.{table} set "  # noqa: S608 - `table` is one of two literals above
            "  erm_status = :synced, "
            "  erm_synced_at = :at, "
            "  erm_synced_by = :by, "
            "  erm_external_id = coalesce(:ext, erm_external_id) "
            "where id = :id"
        ),
        {
            "synced": ErmStatus.SYNCED.value,
            "at": now,
            "by": principal.user_id,
            "ext": request.erm_external_id,
            "id": subject,
        },
    )
    return await _persist(session, audit, row, outcome, now)


@router.post(
    "/tasks/{task_id}/cancel",
    response_model=ErmTaskOut,
    status_code=status.HTTP_200_OK,
    summary="Withdraw an open card, with a reason",
)
async def cancel(
    task_id: TaskIdPath,
    request: CancelRequest,
    principal: CurrentPrincipal,
    session: Session,
    audit: Audit,
) -> ErmTaskOut:
    """The subject is gone, duplicated, or the card was filed in error.

    Only open cards. A confirmed push cannot be undone from here because it
    happened — somebody typed those values into a system this one cannot reach —
    and undoing it means going back to ERM, which is a new card.
    """
    row = await _row(session, task_id, principal, write=True)
    now = dt.datetime.now(dt.UTC)
    try:
        outcome = cancel_task(_task(row), _actor(principal), request.reason, now)
    except ErmError as exc:
        raise _refuse(exc) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    row.cancelled_by = principal.user_id
    row.cancelled_reason = request.reason
    return await _persist(session, audit, row, outcome, now)
