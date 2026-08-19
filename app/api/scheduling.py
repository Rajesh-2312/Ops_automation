"""Checklist and document-register generation — the pure half.

PORTED FROM THE BROWSER
=======================
This is `D:\\projects\\Operational_byteXL\\frontend\\src\\lib\\pending-backend.ts`,
moved to where CLAUDE.md §3 always said it belonged. That file describes itself
as a temporary client-side stand-in and names its replacements exactly::

    POST /programs/{id}/tasks:generate      -> { created, skipped }
    POST /programs/{id}/documents:generate  -> { created, skipped }

Everything here is pure: no session, no I/O, no clock. `app/api/programs.py`
reads the rows, calls `plan_tasks()` / `plan_documents()`, and writes what comes
back. That split is what makes the offset arithmetic — the part that is easy to
get quietly wrong — unit-testable without a database.

TWO DELIBERATE DIFFERENCES FROM THE TYPESCRIPT
----------------------------------------------
1. **The date arithmetic is fixed, not transcribed.** The original built a local
   `Date` at midnight and then read `toISOString().slice(0, 10)`, which is a UTC
   render of a local instant. For any user east of Greenwich — IST is UTC+05:30 —
   local midnight is the *previous* day in UTC, so every generated due date came
   out one day early. Python `date` arithmetic has no timezone in it at all, so
   the bug cannot be reproduced here. CLAUDE.md §11 keeps timestamps UTC in the
   DB and IST at the presentation layer; a `due_date` is a calendar date and
   belongs to neither conversion.

2. **The commercials wall is honoured on document generation.** The browser
   version ran as the signed-in user, so RLS decided what it could insert. Here
   we are on a service-role connection where RLS decides nothing (see
   `app/core/security.py`), and the `program_documents` policies in
   `1000_scheduling_documents.sql` deny an LDE Executive the `remuneration` and
   `invoice_generation` categories. `plan_documents()` takes
   `can_see_commercials` and skips those templates otherwise, so the endpoint
   creates exactly the rows the caller could have created for themselves.

IDEMPOTENCY
-----------
Both planners take the set of `template_id`s already present on the program and
skip them, so re-running after a template is added tops the program up rather
than duplicating it. Preserved from the original, and it is the property that
makes these endpoints safe to retry.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.db.models import COMMERCIAL_DOCUMENT_CATEGORIES, DOCUMENT_STATUS_NOT_STARTED
from app.domain.enums import (
    Persona,
    ProgramStage,
    ProgramType,
    ScheduleAnchor,
    TaskCadence,
    TaskStatus,
)


class ProgramAnchors(BaseModel):
    """The three program facts generation depends on.

    A narrow input rather than the whole `Program` row, so the planners cannot
    accidentally start reading `stage` or `college_id` and become untestable.
    """

    model_config = ConfigDict(frozen=True)

    type: ProgramType
    start_date: dt.date | None = None
    end_date: dt.date | None = None


class TaskTemplateSpec(BaseModel):
    """A `task_templates` row, as generation sees it."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    stage: ProgramStage
    title: str
    description: str | None = None
    default_owner_role: Persona
    cadence: TaskCadence
    #: NULL applies to every program type (`applies_to_type` in SQL).
    applies_to_type: ProgramType | None = None
    offset_days: int | None = None
    offset_anchor: ScheduleAnchor = ScheduleAnchor.PROGRAM_START


class DocumentTemplateSpec(BaseModel):
    """A `document_templates` row, as generation sees it."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    category: str
    name: str
    applies_to_type: ProgramType | None = None


class PlannedTask(BaseModel):
    """One `tasks` row about to be inserted."""

    model_config = ConfigDict(frozen=True)

    template_id: UUID
    stage: ProgramStage
    title: str
    description: str | None
    owner_role: Persona
    cadence: TaskCadence
    due_date: dt.date | None
    status: TaskStatus = TaskStatus.PENDING


class PlannedDocument(BaseModel):
    """One `program_documents` row about to be inserted."""

    model_config = ConfigDict(frozen=True)

    document_template_id: UUID
    category: str
    name: str
    status: str = DOCUMENT_STATUS_NOT_STARTED


class TaskPlan(BaseModel):
    """What `plan_tasks()` decided. `skipped` is `applicable - created`."""

    model_config = ConfigDict(frozen=True)

    to_create: tuple[PlannedTask, ...] = ()
    applicable: int = 0

    @property
    def skipped(self) -> int:
        return self.applicable - len(self.to_create)


class DocumentPlan(BaseModel):
    """What `plan_documents()` decided.

    `withheld_commercial` counts templates this caller may not instantiate
    because of the §4 commercials wall — reported separately from `skipped` so
    "nothing happened" and "you were not allowed to do part of this" never look
    the same to a caller.
    """

    model_config = ConfigDict(frozen=True)

    to_create: tuple[PlannedDocument, ...] = ()
    applicable: int = 0
    withheld_commercial: int = 0

    @property
    def skipped(self) -> int:
        return self.applicable - len(self.to_create)


def applies_to_program(applies_to_type: ProgramType | None, program_type: ProgramType) -> bool:
    """A NULL `applies_to_type` applies to every program type.

    The two types will not share every checklist item forever — attendance
    completeness alone is a hard block on CRT and a warning on bCAP (§5, §7).
    """
    return applies_to_type is None or applies_to_type == program_type


def resolve_due_date(
    *,
    offset_days: int | None,
    offset_anchor: ScheduleAnchor,
    start_date: dt.date | None,
    end_date: dt.date | None,
) -> dt.date | None:
    """Turn a template's relative offset into a real due date.

    This is the piece that turns a checklist into a schedule. Without it every
    task is created with `due_date = NULL`, lands in the `unscheduled` urgency
    band of `public.task_urgency`, and never appears in "needs attention" — so
    the whole cadence/urgency layer sits inert. Nobody sets 37 due dates a
    program by hand.

    Returns `None` in two legitimate cases, and they mean different things:

    * `offset_days is None` — the template has no automatic due date at all.
    * the anchor date is not set yet — dates get confirmed during
      `acquisition_setup`, and a top-up run once they are known fills the gaps
      in. A task with a NULL due date is better than one dated off a date nobody
      has agreed to.

    Negative offsets fall before the anchor (an MOU at -45 is due 45 days
    pre-start), positive after. `timedelta` handles month and year boundaries and
    leap days; do not reimplement it with arithmetic on `.day`.
    """
    if offset_days is None:
        return None
    anchor = end_date if offset_anchor == ScheduleAnchor.PROGRAM_END else start_date
    if anchor is None:
        return None
    return anchor + dt.timedelta(days=offset_days)


def plan_tasks(
    program: ProgramAnchors,
    templates: Sequence[TaskTemplateSpec],
    existing_template_ids: frozenset[UUID],
) -> TaskPlan:
    """Decide which `tasks` rows a program is missing, with due dates resolved.

    `templates` is expected pre-filtered to `is_active` and pre-sorted by
    (stage, order_index) — both are the database's job, and both are cheaper
    there. Type applicability is decided here because it is a rule, not a filter.
    """
    applicable = [t for t in templates if applies_to_program(t.applies_to_type, program.type)]
    planned = [
        PlannedTask(
            template_id=template.id,
            stage=template.stage,
            title=template.title,
            description=template.description,
            owner_role=template.default_owner_role,
            cadence=template.cadence,
            due_date=resolve_due_date(
                offset_days=template.offset_days,
                offset_anchor=template.offset_anchor,
                start_date=program.start_date,
                end_date=program.end_date,
            ),
        )
        for template in applicable
        if template.id not in existing_template_ids
    ]
    return TaskPlan(to_create=tuple(planned), applicable=len(applicable))


def plan_documents(
    program: ProgramAnchors,
    templates: Sequence[DocumentTemplateSpec],
    existing_template_ids: frozenset[UUID],
    *,
    can_see_commercials: bool,
) -> DocumentPlan:
    """Decide which `program_documents` rows a program is missing.

    `templates` is expected pre-filtered to `is_active AND is_required` — only
    required templates are instantiated, so the register stays a real to-do list
    rather than a wall of N/A rows. Optional ones (work-order extensions, review
    decks) are added by hand when they are actually needed.

    Templates in `COMMERCIAL_DOCUMENT_CATEGORIES` are withheld from a caller
    without the commercials wall, mirroring the `program_documents` policies —
    see the module docstring.
    """
    applicable = [t for t in templates if applies_to_program(t.applies_to_type, program.type)]
    withheld = 0
    planned: list[PlannedDocument] = []
    for template in applicable:
        if template.category in COMMERCIAL_DOCUMENT_CATEGORIES and not can_see_commercials:
            withheld += 1
            continue
        if template.id in existing_template_ids:
            continue
        planned.append(
            PlannedDocument(
                document_template_id=template.id,
                category=template.category,
                name=template.name,
            )
        )
    return DocumentPlan(
        to_create=tuple(planned),
        applicable=len(applicable),
        withheld_commercial=withheld,
    )
