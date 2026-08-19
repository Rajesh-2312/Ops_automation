"""Program endpoints — checklist and document-register generation.

    POST /programs/{program_id}/tasks:generate
    POST /programs/{program_id}/documents:generate

These are the two endpoints `pending-backend.ts` was written to be replaced by;
the offset arithmetic and the idempotency rules live in `app/api/scheduling.py`,
which is pure and unit-tested. This module is the I/O and the authorisation.

AUTHORISATION — WHY EVERY HANDLER RE-CHECKS
===========================================
We are on a service-role connection. RLS returns every row and the column-guard
triggers do nothing (`app/core/security.py`). So each handler reproduces, in
code, the policy that would have governed the same statement from the browser:

    tasks              is_internal() AND can_reach_program(program_id)
    program_documents  is_internal() AND can_reach_program(program_id)
                       AND category NOT IN ('remuneration','invoice_generation')
                   OR  can_see_commercials() AND can_reach_program(program_id)

`can_reach_program()` resolves through `programs.college_id`, so both handlers
load the program first and check reach on its college. Fetching the program is
therefore not just a 404 check — it is the authorisation input, which is why it
happens before anything else and why its result is never widened.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.scheduling import (
    DocumentTemplateSpec,
    ProgramAnchors,
    TaskTemplateSpec,
    plan_documents,
    plan_tasks,
)
from app.core.audit import AuditAction, AuditEvent, AuditWriter, get_audit_writer
from app.core.security import (
    CurrentPrincipal,
    Principal,
    require_college_reach,
    require_internal,
)
from app.db.models import (
    DocumentTemplate,
    Program,
    ProgramDocument,
    Task,
    TaskTemplate,
)
from app.db.session import get_session

router = APIRouter(prefix="/programs", tags=["programs"])

ProgramId = Annotated[UUID, Path(description="Program id")]
Session = Annotated[AsyncSession, Depends(get_session)]
Audit = Annotated[AuditWriter, Depends(get_audit_writer)]


class GenerateResult(BaseModel):
    """`{ created, skipped }` — the shape `pending-backend.ts` promised its callers.

    Kept byte-compatible so swapping the browser function for a `fetch()` needs no
    call-site change. `withheld_commercial` is additive and absent from the task
    response, which has no commercial dimension.
    """

    created: int = Field(ge=0, description="Rows inserted by this call")
    skipped: int = Field(ge=0, description="Applicable templates already present")


class DocumentGenerateResult(GenerateResult):
    """As above, plus what the §4 commercials wall withheld from this caller."""

    withheld_commercial: int = Field(
        default=0,
        ge=0,
        description=(
            "Required templates in the remuneration / invoice_generation categories "
            "that this persona may not instantiate"
        ),
    )


async def _authorised_program(
    session: AsyncSession, principal: Principal, program_id: UUID
) -> Program:
    """Load a program the caller is allowed to act on, or raise.

    The order matters. Persona is checked first (`is_internal()`), then existence,
    then reach — so a trainer probing program ids gets the same 403 for every id
    and learns nothing about which ones exist. A 404 leaks membership; that is
    only acceptable once the caller has already proven staff persona and reach.
    """
    require_internal(principal)
    program = (
        await session.execute(select(Program).where(Program.id == program_id))
    ).scalar_one_or_none()
    if program is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Program not found")
    require_college_reach(principal, program.college_id)
    return program


@router.post(
    "/{program_id}/tasks:generate",
    response_model=GenerateResult,
    status_code=status.HTTP_200_OK,
    summary="Seed a program's checklist from task_templates, with due dates",
)
async def generate_tasks(
    program_id: ProgramId,
    principal: CurrentPrincipal,
    session: Session,
    audit: Audit,
) -> GenerateResult:
    """Top a program's checklist up from the active `task_templates`.

    Idempotent by `template_id`: templates already represented on the program are
    skipped, so re-running after a template is added adds only the new ones. Safe
    to retry, and safe to call again once the program's dates are confirmed —
    though note that a re-run only fills in *missing* tasks; it does not restate
    the due date of a task created while `start_date` was still NULL. Moving an
    existing task's date is an edit, with an owner and an audit row, not a
    side effect of a generate call.

    Open to all three internal personas, matching `tasks_internal_all`. An LDE
    Executive generating their own campus checklist is exactly the intended use.
    """
    program = await _authorised_program(session, principal, program_id)

    template_rows = (
        (
            await session.execute(
                select(TaskTemplate)
                .where(TaskTemplate.is_active.is_(True))
                .order_by(TaskTemplate.stage, TaskTemplate.order_index)
            )
        )
        .scalars()
        .all()
    )
    # The `is_not(None)` in the WHERE clause is what makes this safe at runtime;
    # the comprehension's guard is what makes it provable. Both are kept: dropping
    # the SQL filter would drag every NULL row across the wire, and dropping the
    # comprehension guard would let a NULL into a set of template ids, where it
    # would silently mark an untemplated task as "already planned".
    existing = frozenset(
        template_id
        for template_id in (
            await session.execute(
                select(Task.template_id).where(
                    Task.program_id == program_id, Task.template_id.is_not(None)
                )
            )
        )
        .scalars()
        .all()
        if template_id is not None
    )

    plan = plan_tasks(
        ProgramAnchors(type=program.type, start_date=program.start_date, end_date=program.end_date),
        [
            TaskTemplateSpec(
                id=row.id,
                stage=row.stage,
                title=row.title,
                description=row.description,
                default_owner_role=row.default_owner_role,
                cadence=row.cadence,
                applies_to_type=row.applies_to_type,
                offset_days=row.offset_days,
                offset_anchor=row.offset_anchor,
            )
            for row in template_rows
        ],
        existing,
    )

    now = dt.datetime.now(dt.UTC)
    session.add_all(
        [
            Task(
                id=uuid.uuid4(),
                program_id=program_id,
                template_id=planned.template_id,
                stage=planned.stage,
                title=planned.title,
                description=planned.description,
                owner_role=planned.owner_role,
                status=planned.status,
                cadence=planned.cadence,
                due_date=planned.due_date,
                created_at=now,
                updated_at=now,
            )
            for planned in plan.to_create
        ]
    )
    await session.commit()

    await audit.write(
        AuditEvent(
            actor_id=principal.user_id,
            actor_persona=principal.persona,
            action=AuditAction.TASKS_GENERATED,
            entity_table="tasks",
            entity_id=program_id,
            # `before` is the checklist as it stood; `after` as it now stands.
            # Counts, not row dumps: the rows themselves are queryable by
            # program_id, and an audit trail that duplicates them goes stale.
            before={"task_count": len(existing)},
            after={
                "task_count": len(existing) + len(plan.to_create),
                "created": len(plan.to_create),
                "skipped": plan.skipped,
            },
        )
    )
    return GenerateResult(created=len(plan.to_create), skipped=plan.skipped)


@router.post(
    "/{program_id}/documents:generate",
    response_model=DocumentGenerateResult,
    status_code=status.HTTP_200_OK,
    summary="Seed a program's document register from document_templates",
)
async def generate_documents(
    program_id: ProgramId,
    principal: CurrentPrincipal,
    session: Session,
    audit: Audit,
) -> DocumentGenerateResult:
    """Instantiate the required master documents against a program.

    The Drive equivalent of copying each master out of `00_Templates_and_Masters`
    into the program's stage folders. Only `is_required` templates are
    instantiated, so the register stays a real to-do list.

    Idempotent by `document_template_id`, and backed by a real unique index —
    `program_documents_unique_template` — so a concurrent duplicate call fails at
    the database rather than doubling the register.

    The `remuneration` and `invoice_generation` categories are withheld from a
    caller outside the commercials wall and reported as `withheld_commercial`.
    Withheld, not silently dropped: an LDE Executive should be able to see that
    part of the register exists and is not theirs, which is what the count says.
    """
    program = await _authorised_program(session, principal, program_id)

    template_rows = (
        (
            await session.execute(
                select(DocumentTemplate)
                .where(
                    DocumentTemplate.is_active.is_(True),
                    DocumentTemplate.is_required.is_(True),
                )
                .order_by(DocumentTemplate.category, DocumentTemplate.order_index)
            )
        )
        .scalars()
        .all()
    )
    # Guarded in both places, for the reason given on the task query above.
    existing = frozenset(
        template_id
        for template_id in (
            await session.execute(
                select(ProgramDocument.document_template_id).where(
                    ProgramDocument.program_id == program_id,
                    ProgramDocument.document_template_id.is_not(None),
                )
            )
        )
        .scalars()
        .all()
        if template_id is not None
    )

    plan = plan_documents(
        ProgramAnchors(type=program.type, start_date=program.start_date, end_date=program.end_date),
        [
            DocumentTemplateSpec(
                id=row.id,
                category=row.category,
                name=row.name,
                applies_to_type=row.applies_to_type,
            )
            for row in template_rows
        ],
        existing,
        can_see_commercials=principal.can_see_commercials,
    )

    now = dt.datetime.now(dt.UTC)
    session.add_all(
        [
            ProgramDocument(
                id=uuid.uuid4(),
                program_id=program_id,
                document_template_id=planned.document_template_id,
                category=planned.category,
                name=planned.name,
                status=planned.status,
                created_at=now,
                updated_at=now,
            )
            for planned in plan.to_create
        ]
    )
    await session.commit()

    await audit.write(
        AuditEvent(
            actor_id=principal.user_id,
            actor_persona=principal.persona,
            action=AuditAction.DOCUMENTS_GENERATED,
            entity_table="program_documents",
            entity_id=program_id,
            before={"document_count": len(existing)},
            after={
                "document_count": len(existing) + len(plan.to_create),
                "created": len(plan.to_create),
                "skipped": plan.skipped,
                "withheld_commercial": plan.withheld_commercial,
            },
        )
    )
    return DocumentGenerateResult(
        created=len(plan.to_create),
        skipped=plan.skipped,
        withheld_commercial=plan.withheld_commercial,
    )
