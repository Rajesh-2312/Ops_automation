"""`app/services/reporting/assembly.py` — the facts half of Phase 6.

Pure functions over frozen dataclasses, so every test here runs without a
database, a session or a model. What is asserted is the set of decisions the
module makes that a reader would otherwise have to take on trust:

* a percentage over zero sessions is `None`, never `0.0`;
* an average is taken over the collections that HAVE a score, and says so;
* a report is non-commercial unless somebody asked for the other kind;
* the commercial section carries no total, because R2 keeps money arithmetic in
  the engine.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytest

from app.domain.enums import ProgramStage, ProgramType
from app.services.reporting.assembly import (
    AssessmentFacts,
    BatchFacts,
    FeedbackEntry,
    ProgramFacts,
    ProgramSummaryLine,
    ReportPeriod,
    StudentAttendanceFacts,
    TaskFacts,
    TrainerCostLine,
    TrainerCostSection,
    TrainerDeliveryFacts,
    assemble_governance_report,
    summarise_college,
    synthesise_feedback,
)

JULY = ReportPeriod(start=dt.date(2026, 7, 1), end=dt.date(2026, 7, 31))
PROGRAM_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
COLLEGE_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")


def a_program() -> ProgramFacts:
    return ProgramFacts(
        program_id=PROGRAM_ID,
        program_name="bCAP 2026",
        program_type=ProgramType.BCAP,
        stage=ProgramStage.ACTIVE_MONITORING,
        college_id=COLLEGE_ID,
        college_name="Malineni Lakshmaiah",
    )


def a_trainer(*, marked: int, days: int = 31) -> TrainerDeliveryFacts:
    return TrainerDeliveryFacts(
        deployment_id=uuid.uuid4(),
        trainer_name="VEMA PRUDHVI SAI",
        batch_name="CSE-A",
        days_in_period=days,
        marked_days=marked,
        present_days=marked,
        absent_days=0,
    )


def a_report(**kwargs: object) -> object:
    defaults: dict[str, object] = {
        "program": a_program(),
        "period": JULY,
        "batches": (BatchFacts(batch_id=uuid.uuid4(), name="CSE-A", student_count=60),),
        "trainers": (a_trainer(marked=31),),
        "student_attendance": StudentAttendanceFacts(sessions=100, present=92, absent=8),
        "assessments": AssessmentFacts(conducted=2, with_report=1),
        "observations": 3,
        "tasks": TaskFacts(open=4, overdue=1, blocked=0, done=33),
        "feedback": (),
    }
    return assemble_governance_report(**{**defaults, **kwargs})  # type: ignore[arg-type]


# --- period -------------------------------------------------------------------


def test_a_period_cannot_end_before_it_starts() -> None:
    with pytest.raises(ValueError, match="ends"):
        ReportPeriod(start=dt.date(2026, 7, 31), end=dt.date(2026, 7, 1))


def test_period_days_are_inclusive() -> None:
    assert JULY.days == 31


# --- percentages --------------------------------------------------------------


def test_attendance_percent_is_a_quantised_decimal() -> None:
    facts = StudentAttendanceFacts(sessions=100, present=92, absent=8)
    assert facts.attendance_percent == Decimal("92.0")


def test_no_sessions_reports_none_not_zero() -> None:
    """ "No sessions were held" and "nobody attended" are different statements."""
    assert StudentAttendanceFacts().attendance_percent is None


def test_attendance_percent_never_carries_a_float() -> None:
    facts = StudentAttendanceFacts(sessions=3, present=1)
    percent = facts.attendance_percent
    assert isinstance(percent, Decimal)
    assert percent == Decimal("33.3")


# --- tracksheet completeness (§5) --------------------------------------------


def test_an_unmarked_day_makes_a_tracksheet_incomplete() -> None:
    trainer = a_trainer(marked=30)
    assert trainer.unmarked_days == 1
    assert trainer.is_complete is False


def test_incomplete_tracksheets_are_singled_out_on_the_delivery_section() -> None:
    report = a_report(trainers=(a_trainer(marked=31), a_trainer(marked=20)))
    assert len(report.delivery.incomplete_tracksheets) == 1  # type: ignore[attr-defined]


# --- feedback synthesis -------------------------------------------------------


def test_average_is_taken_over_scored_collections_and_says_so() -> None:
    entries = [
        FeedbackEntry(source="gform", summary_score=Decimal("4.00"), response_count=30),
        FeedbackEntry(source="platform", summary_score=Decimal("4.50"), response_count=20),
        FeedbackEntry(source="gform", summary_score=None, response_count=10),
    ]
    synthesis = synthesise_feedback(entries)

    assert synthesis.collections == 3
    assert synthesis.scored_collections == 2
    assert synthesis.average_score == Decimal("4.25")
    assert synthesis.lowest_score == Decimal("4.00")
    assert synthesis.highest_score == Decimal("4.50")
    assert synthesis.total_responses == 60


def test_a_missing_score_is_never_averaged_as_zero() -> None:
    entries = [
        FeedbackEntry(source="gform", summary_score=Decimal("4.00")),
        FeedbackEntry(source="gform", summary_score=None),
    ]
    assert synthesise_feedback(entries).average_score == Decimal("4.00")


def test_no_scores_at_all_synthesises_to_none() -> None:
    synthesis = synthesise_feedback([FeedbackEntry(source="gform")])
    assert synthesis.average_score is None
    assert synthesis.total_responses is None


def test_synthesis_payload_stringifies_every_score() -> None:
    """R7's rule applied outside money: no Decimal leaves as a JSON float."""
    payload = synthesise_feedback(
        [FeedbackEntry(source="gform", summary_score=Decimal("4.25"))]
    ).as_payload()
    assert payload["average_score"] == "4.25"


# --- the commercials wall in the data (§4, R5) -------------------------------


def test_a_report_is_not_commercial_unless_cost_was_asked_for() -> None:
    assert a_report().is_commercial is False  # type: ignore[attr-defined]


def test_a_report_with_trainer_cost_declares_itself_commercial() -> None:
    section = TrainerCostSection(
        lines=(
            TrainerCostLine(
                trainer_name="VEMA PRUDHVI SAI",
                trainer_pan="VEMAP1234K",
                period_start=JULY.start,
                period_end=JULY.end,
                net=Decimal("14035"),
            ),
        )
    )
    report = a_report(trainer_cost=section)

    assert report.is_commercial is True  # type: ignore[attr-defined]
    assert "trainer_cost" in report.as_payload()  # type: ignore[attr-defined]


def test_the_cost_section_carries_no_total_and_says_why() -> None:
    """R2: monetary arithmetic lives in the engine, unit-tested. Not in a report."""
    payload = TrainerCostSection(lines=()).as_payload()
    assert payload["total"] is None
    assert "R2" in str(payload["total_note"])


def test_a_cost_line_renders_net_as_a_string() -> None:
    line = TrainerCostLine(
        trainer_name="Bushily Kondala Rao",
        trainer_pan="BCDPK1234K",
        period_start=JULY.start,
        period_end=JULY.end,
        net=Decimal("58500"),
    )
    assert line.as_payload()["net"] == "58500"


# --- payload ------------------------------------------------------------------


def test_the_payload_carries_every_reportable_figure() -> None:
    payload = a_report().as_payload()  # type: ignore[attr-defined]

    delivery = payload["delivery"]
    assert delivery["batch_count"] == 1
    assert delivery["trainer_count"] == 1
    assert delivery["tasks_overdue"] == 1
    assert delivery["student_attendance"]["attendance_percent"] == "92.0"
    assert payload["is_commercial"] is False


def test_the_title_names_college_program_and_period() -> None:
    title = a_report().title  # type: ignore[attr-defined]
    assert "Malineni Lakshmaiah" in title
    assert "2026-07-01" in title


# --- college summary ----------------------------------------------------------


def a_line(name: str, stage: ProgramStage) -> ProgramSummaryLine:
    return ProgramSummaryLine(
        program_id=uuid.uuid4(),
        program_name=name,
        program_type=ProgramType.CRT,
        stage=stage,
        batch_count=1,
        trainer_count=1,
        incomplete_tracksheets=0,
    )


def test_a_college_summary_orders_by_stage_then_name() -> None:
    summary = summarise_college(
        college_id=COLLEGE_ID,
        college_name="Malineni Lakshmaiah",
        period=JULY,
        programs=[
            a_line("Zeta", ProgramStage.ACQUISITION_SETUP),
            a_line("Alpha", ProgramStage.ACQUISITION_SETUP),
            a_line("Beta", ProgramStage.ACTIVE_MONITORING),
        ],
    )
    assert [p.program_name for p in summary.programs] == ["Alpha", "Zeta", "Beta"]


def test_a_college_summary_payload_carries_no_money_key_at_all() -> None:
    """§4: a summary is the LDE Executive's daily view. Nothing commercial in it."""
    payload = summarise_college(
        college_id=COLLEGE_ID,
        college_name="Malineni Lakshmaiah",
        period=JULY,
        programs=[a_line("Alpha", ProgramStage.DEPLOYMENT)],
    ).as_payload()

    rendered = str(payload)
    for forbidden in ("net", "rate", "invoice", "cost", "payout"):
        assert forbidden not in rendered
