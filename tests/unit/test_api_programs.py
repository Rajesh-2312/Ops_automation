"""Checklist and document generation — the logic ported out of the browser.

Covers `app/api/scheduling.py`, which is the whole of `pending-backend.ts` minus
the I/O. Three things are worth pinning and all three are here:

* the offset arithmetic, including the timezone bug the TypeScript had;
* idempotency by template id, which is what makes the endpoints retry-safe;
* the commercials wall on the document register, which the browser version got
  from RLS and we have to apply ourselves.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from app.api.scheduling import (
    DocumentTemplateSpec,
    ProgramAnchors,
    TaskTemplateSpec,
    applies_to_program,
    plan_documents,
    plan_tasks,
    resolve_due_date,
)
from app.domain.enums import Persona, ProgramStage, ProgramType, ScheduleAnchor, TaskCadence

START = dt.date(2026, 7, 1)
END = dt.date(2026, 9, 30)

BCAP = ProgramAnchors(type=ProgramType.BCAP, start_date=START, end_date=END)
UNDATED = ProgramAnchors(type=ProgramType.BCAP)


def task_template(**overrides) -> TaskTemplateSpec:
    spec = {
        "id": uuid.uuid4(),
        "stage": ProgramStage.ACQUISITION_SETUP,
        "title": "Sign the MoU",
        "description": None,
        "default_owner_role": Persona.MANAGER,
        "cadence": TaskCadence.ONE_TIME,
        "applies_to_type": None,
        "offset_days": None,
        "offset_anchor": ScheduleAnchor.PROGRAM_START,
    }
    spec.update(overrides)
    return TaskTemplateSpec(**spec)


def doc_template(**overrides) -> DocumentTemplateSpec:
    spec = {
        "id": uuid.uuid4(),
        "category": "college_onboarding",
        "name": "MoU",
        "applies_to_type": None,
    }
    spec.update(overrides)
    return DocumentTemplateSpec(**spec)


# --- offset resolution -------------------------------------------------------


def test_offset_counts_forward_from_program_start():
    assert resolve_due_date(
        offset_days=14,
        offset_anchor=ScheduleAnchor.PROGRAM_START,
        start_date=START,
        end_date=END,
    ) == dt.date(2026, 7, 15)


def test_negative_offset_falls_before_the_anchor():
    """ "MOU at -45 is due 45 days pre-start" — 1000_scheduling_documents.sql."""
    assert resolve_due_date(
        offset_days=-45,
        offset_anchor=ScheduleAnchor.PROGRAM_START,
        start_date=START,
        end_date=END,
    ) == dt.date(2026, 5, 17)


def test_program_end_anchor_uses_the_end_date():
    """Closeout work anchors to program_end; nothing else makes sense for it."""
    assert resolve_due_date(
        offset_days=7,
        offset_anchor=ScheduleAnchor.PROGRAM_END,
        start_date=START,
        end_date=END,
    ) == dt.date(2026, 10, 7)


def test_offset_crosses_a_leap_day_correctly():
    """`timedelta`, not arithmetic on `.day`. 2028 is a leap year."""
    assert resolve_due_date(
        offset_days=1,
        offset_anchor=ScheduleAnchor.PROGRAM_START,
        start_date=dt.date(2028, 2, 28),
        end_date=None,
    ) == dt.date(2028, 2, 29)


def test_zero_offset_is_the_anchor_itself_not_no_due_date():
    """0 and None are different answers; a falsy check would conflate them."""
    assert (
        resolve_due_date(
            offset_days=0,
            offset_anchor=ScheduleAnchor.PROGRAM_START,
            start_date=START,
            end_date=None,
        )
        == START
    )


def test_no_offset_means_no_automatic_due_date():
    assert (
        resolve_due_date(
            offset_days=None,
            offset_anchor=ScheduleAnchor.PROGRAM_START,
            start_date=START,
            end_date=END,
        )
        is None
    )


@pytest.mark.parametrize(
    ("anchor", "start", "end"),
    [
        (ScheduleAnchor.PROGRAM_START, None, END),
        (ScheduleAnchor.PROGRAM_END, START, None),
    ],
)
def test_missing_anchor_date_yields_no_due_date(anchor, start, end):
    """Legitimate during acquisition_setup — a top-up run fills the gap later."""
    assert (
        resolve_due_date(offset_days=10, offset_anchor=anchor, start_date=start, end_date=end)
        is None
    )


def test_due_date_does_not_shift_by_timezone():
    """The bug in `pending-backend.ts`, pinned so the port cannot reacquire it.

    The TypeScript built a local `Date` at midnight and then read
    `toISOString().slice(0, 10)`. For IST (UTC+05:30) local midnight is 18:30 the
    previous day in UTC, so every generated due date came out one day early.
    `date + timedelta` has no timezone in it, so the anchor date with offset 0 is
    the anchor date, in every locale.
    """
    for day in (dt.date(2026, 1, 1), dt.date(2026, 7, 1), dt.date(2026, 12, 31)):
        assert (
            resolve_due_date(
                offset_days=0,
                offset_anchor=ScheduleAnchor.PROGRAM_START,
                start_date=day,
                end_date=None,
            )
            == day
        )


# --- type applicability ------------------------------------------------------


def test_null_applies_to_type_applies_to_every_program_type():
    assert applies_to_program(None, ProgramType.CRT)
    assert applies_to_program(None, ProgramType.BCAP)


def test_narrowed_template_applies_to_one_type_only():
    assert applies_to_program(ProgramType.CRT, ProgramType.CRT)
    assert not applies_to_program(ProgramType.CRT, ProgramType.BCAP)


# --- task planning -----------------------------------------------------------


def test_plan_creates_every_applicable_template_on_a_fresh_program():
    templates = [task_template(), task_template(), task_template()]
    plan = plan_tasks(BCAP, templates, frozenset())
    assert len(plan.to_create) == 3
    assert plan.applicable == 3
    assert plan.skipped == 0


def test_plan_is_idempotent_by_template_id():
    """Re-running tops the program up rather than duplicating it."""
    templates = [task_template(), task_template()]
    already = frozenset({templates[0].id})
    plan = plan_tasks(BCAP, templates, already)
    assert [p.template_id for p in plan.to_create] == [templates[1].id]
    assert plan.skipped == 1


def test_second_run_with_nothing_new_creates_nothing():
    templates = [task_template(), task_template()]
    plan = plan_tasks(BCAP, templates, frozenset(t.id for t in templates))
    assert plan.to_create == ()
    assert plan.skipped == 2


def test_templates_for_the_other_program_type_are_not_counted_at_all():
    """Skipped means "already there", not "not yours" — the counts must not blur."""
    templates = [
        task_template(applies_to_type=ProgramType.CRT),
        task_template(applies_to_type=ProgramType.BCAP),
    ]
    plan = plan_tasks(BCAP, templates, frozenset())
    assert plan.applicable == 1
    assert plan.skipped == 0
    assert len(plan.to_create) == 1


def test_planned_task_carries_the_resolved_due_date_and_template_fields():
    template = task_template(
        title="Kick-off call",
        description="Introduce the trainer to the campus",
        default_owner_role=Persona.LDE_EXECUTIVE,
        cadence=TaskCadence.WEEKLY,
        stage=ProgramStage.DEPLOYMENT,
        offset_days=3,
    )
    planned = plan_tasks(BCAP, [template], frozenset()).to_create[0]
    assert planned.due_date == dt.date(2026, 7, 4)
    assert planned.title == "Kick-off call"
    assert planned.description == "Introduce the trainer to the campus"
    assert planned.owner_role == Persona.LDE_EXECUTIVE
    assert planned.cadence == TaskCadence.WEEKLY
    assert planned.stage == ProgramStage.DEPLOYMENT


def test_a_program_with_no_dates_still_gets_its_checklist():
    """Undated tasks are legitimate; refusing to generate them is not."""
    plan = plan_tasks(UNDATED, [task_template(offset_days=10)], frozenset())
    assert len(plan.to_create) == 1
    assert plan.to_create[0].due_date is None


def test_template_order_is_preserved():
    """The database sorts by (stage, order_index); planning must not reshuffle."""
    templates = [task_template(title=f"t{i}") for i in range(5)]
    plan = plan_tasks(BCAP, templates, frozenset())
    assert [p.title for p in plan.to_create] == ["t0", "t1", "t2", "t3", "t4"]


# --- document planning and the commercials wall ------------------------------


def test_documents_plan_mirrors_task_planning():
    templates = [doc_template(), doc_template()]
    plan = plan_documents(BCAP, templates, frozenset(), can_see_commercials=True)
    assert len(plan.to_create) == 2
    assert plan.skipped == 0
    assert plan.withheld_commercial == 0


def test_documents_are_idempotent_by_template_id():
    templates = [doc_template(), doc_template()]
    plan = plan_documents(BCAP, templates, frozenset({templates[0].id}), can_see_commercials=True)
    assert [p.document_template_id for p in plan.to_create] == [templates[1].id]
    assert plan.skipped == 1


@pytest.mark.parametrize("category", ["remuneration", "invoice_generation"])
def test_commercial_categories_are_withheld_without_the_wall(category):
    """CLAUDE.md §4 / R5, applied where RLS cannot: on a service-role connection.

    `1000_scheduling_documents.sql` denies an LDE Executive these two categories
    because a filed remuneration row is a live pointer to an amount.
    """
    templates = [doc_template(category=category), doc_template(category="college_onboarding")]
    plan = plan_documents(BCAP, templates, frozenset(), can_see_commercials=False)
    assert plan.withheld_commercial == 1
    assert [p.category for p in plan.to_create] == ["college_onboarding"]


@pytest.mark.parametrize("category", ["remuneration", "invoice_generation"])
def test_commercial_categories_are_created_with_the_wall(category):
    templates = [doc_template(category=category)]
    plan = plan_documents(BCAP, templates, frozenset(), can_see_commercials=True)
    assert plan.withheld_commercial == 0
    assert len(plan.to_create) == 1


def test_withheld_is_reported_separately_from_skipped():
    """ "Nothing to do" and "part of this was not yours" must not look the same."""
    templates = [doc_template(category="remuneration")]
    plan = plan_documents(BCAP, templates, frozenset(), can_see_commercials=False)
    assert plan.to_create == ()
    assert plan.withheld_commercial == 1
    assert plan.skipped == 1
