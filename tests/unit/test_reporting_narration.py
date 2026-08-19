"""`app/services/reporting/narration.py` — the prose half, and its two refusals.

No live model. `FakeLLM` from `tests.unit.agent_fakes` returns queued strings,
which is the only way to assert what happens when a model says something WRONG —
and §12 asks for exactly that assertion ("absence of fabricated figures").

Three properties are pinned here:

* **R1.** A figure in the prose that is not in the structured facts raises, and
  the narration is not returned in any form. Reformatting a figure that WAS given
  (`92.0` as `92`) is fine — the check compares numeric value, not string.
* **§9.** When retrieved passages are supplied, an answer with no citation, or
  one citing a source that was never retrieved, is discarded.
* **§2.** A governance report routes to the frontier tier through
  `LLMTask.GOVERNANCE_REPORT`, and nothing in the module names a model id.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytest

from app.agents.grounding import UngroundedFigureError
from app.domain.enums import LLMTask, ModelTier, ProgramStage, ProgramType
from app.rag.guards import RefusalReason
from app.services.reporting.assembly import (
    AssessmentFacts,
    BatchFacts,
    FeedbackEntry,
    ProgramFacts,
    ReportPeriod,
    StudentAttendanceFacts,
    TaskFacts,
    TrainerDeliveryFacts,
    assemble_governance_report,
    summarise_college,
    synthesise_feedback,
)
from app.services.reporting.narration import (
    Citation,
    NarrationRefused,
    ReportNarrator,
)
from tests.unit.agent_fakes import FakeLLM

JULY = ReportPeriod(start=dt.date(2026, 7, 1), end=dt.date(2026, 7, 31))


def a_report() -> object:
    return assemble_governance_report(
        program=ProgramFacts(
            program_id=uuid.uuid4(),
            program_name="bCAP",
            program_type=ProgramType.BCAP,
            stage=ProgramStage.ACTIVE_MONITORING,
            college_id=uuid.uuid4(),
            college_name="Malineni Lakshmaiah",
            starts_on=None,
            ends_on=None,
        ),
        period=JULY,
        batches=(BatchFacts(batch_id=uuid.uuid4(), name="CSE-A", student_count=60),),
        trainers=(
            TrainerDeliveryFacts(
                deployment_id=uuid.uuid4(),
                trainer_name="VEMA PRUDHVI SAI",
                batch_name="CSE-A",
                days_in_period=31,
                marked_days=31,
                present_days=31,
                absent_days=0,
            ),
        ),
        student_attendance=StudentAttendanceFacts(sessions=100, present=92, absent=8),
        assessments=AssessmentFacts(conducted=2, with_report=2),
        observations=3,
        tasks=TaskFacts(open=4, overdue=1, blocked=0, done=33),
        feedback=(),
    )


def narrator(*responses: str) -> tuple[ReportNarrator, FakeLLM]:
    llm = FakeLLM(responses=list(responses))
    return ReportNarrator(llm=llm), llm


# --- R1: figures ---------------------------------------------------------------


@pytest.mark.anyio
async def test_a_narrative_quoting_only_given_figures_is_accepted() -> None:
    report = a_report()
    text = (
        "60 students across 1 batch. Student attendance stood at 92.0 percent over "
        "100 sessions. 2 assessments were conducted, both with reports. 4 tasks "
        "remain open, 1 of them overdue."
    )
    reporter, llm = narrator(text)

    narration = await reporter.narrate_governance(report)  # type: ignore[arg-type]

    assert narration.body == text
    assert llm.tasks_called == [LLMTask.GOVERNANCE_REPORT]


@pytest.mark.anyio
async def test_a_reformatted_figure_is_still_grounded() -> None:
    """`92.0` written as `92` is presentation. The check compares numeric value."""
    reporter, _ = narrator("Attendance was 92 percent.")
    narration = await reporter.narrate_governance(a_report())  # type: ignore[arg-type]
    assert "92" in narration.body


@pytest.mark.anyio
async def test_an_invented_figure_refuses_the_whole_draft() -> None:
    reporter, _ = narrator("Attendance was approximately 95 percent, up from last month.")

    with pytest.raises(UngroundedFigureError) as excinfo:
        await reporter.narrate_governance(a_report())  # type: ignore[arg-type]

    assert "reporting.narrate_governance" in str(excinfo.value)
    assert any(v.value == Decimal("95") for v in excinfo.value.violations)


@pytest.mark.anyio
async def test_an_averaged_figure_is_an_invented_figure() -> None:
    """R2's shape outside money: the model may explain a number, never derive one."""
    reporter, _ = narrator("Across the period trainers averaged 15.5 marked days each.")

    with pytest.raises(UngroundedFigureError):
        await reporter.narrate_governance(a_report())  # type: ignore[arg-type]


# --- §9: citations -------------------------------------------------------------


SOURCE = Citation(
    document_title="Trainer Attendance SOP",
    section="4.2 Marking",
    text="Every deployed day must be marked before the payout cycle opens.",
)


@pytest.mark.anyio
async def test_a_cited_claim_against_a_retrieved_source_is_accepted() -> None:
    reporter, _ = narrator("Every deployed day must be marked before payout opens [1].")
    narration = await reporter.narrate_governance(a_report(), passages=[SOURCE])  # type: ignore[arg-type]
    assert narration.citations == (SOURCE,)


@pytest.mark.anyio
async def test_an_uncited_answer_is_discarded_when_sources_were_supplied() -> None:
    reporter, _ = narrator("Every deployed day must be marked before payout opens.")

    with pytest.raises(NarrationRefused) as excinfo:
        await reporter.narrate_governance(a_report(), passages=[SOURCE])  # type: ignore[arg-type]

    assert excinfo.value.refusal.reason is RefusalReason.UNCITED


@pytest.mark.anyio
async def test_a_citation_to_a_source_that_was_not_retrieved_is_a_fabrication() -> None:
    reporter, _ = narrator("Marking is mandatory [3].")

    with pytest.raises(NarrationRefused) as excinfo:
        await reporter.narrate_governance(a_report(), passages=[SOURCE])  # type: ignore[arg-type]

    assert excinfo.value.refusal.reason is RefusalReason.INVALID_CITATION


@pytest.mark.anyio
async def test_no_citation_is_demanded_when_no_source_was_supplied() -> None:
    """A narration written purely from SQL facts cites nothing and needs no marker."""
    reporter, _ = narrator("1 batch, 60 students.")
    assert await reporter.narrate_governance(a_report()) is not None  # type: ignore[arg-type]


# --- §2: routing and §11: telemetry -------------------------------------------


@pytest.mark.anyio
async def test_a_governance_report_routes_to_the_frontier_tier() -> None:
    reporter, llm = narrator("1 batch.")
    await reporter.narrate_governance(a_report())  # type: ignore[arg-type]
    assert llm.responses == []
    assert llm.calls[0]["task"] is LLMTask.GOVERNANCE_REPORT


@pytest.mark.anyio
async def test_a_feedback_summary_routes_to_the_volume_tier() -> None:
    synthesis = synthesise_feedback(
        [FeedbackEntry(source="gform", summary_score=Decimal("4.25"), response_count=30)]
    )
    reporter, llm = narrator("The average score was 4.25 across 30 responses.")

    narration = await reporter.narrate_feedback(synthesis, program_name="bCAP")

    assert narration.task is LLMTask.SUMMARY
    assert narration.model  # the fake still reports which model answered
    assert llm.calls[0]["task"] is LLMTask.SUMMARY


@pytest.mark.anyio
async def test_the_narration_records_what_the_call_cost() -> None:
    """§11: prompt, tokens, latency, every invocation."""
    reporter, _ = narrator("1 batch.")
    narration = await reporter.narrate_governance(a_report())  # type: ignore[arg-type]

    assert narration.prompt_chars > 0
    assert narration.total_tokens == narration.prompt_tokens + narration.completion_tokens
    assert narration.latency_ms >= 0


@pytest.mark.anyio
async def test_the_program_name_is_grounded_input_not_a_prompt_string() -> None:
    """A programme called "bCAP 2026" licenses the model to write 2026."""
    synthesis = synthesise_feedback([FeedbackEntry(source="gform")])
    reporter, _ = narrator("No scored feedback was collected for bCAP 2026.")

    assert await reporter.narrate_feedback(synthesis, program_name="bCAP 2026")


@pytest.mark.anyio
async def test_a_college_summary_narration_is_grounded_against_the_summary() -> None:
    summary = summarise_college(
        college_id=uuid.uuid4(),
        college_name="Malineni Lakshmaiah",
        period=JULY,
        programs=[],
    )
    reporter, _ = narrator("No programmes are running at Malineni Lakshmaiah in this period.")

    narration = await reporter.narrate_college_summary(summary)
    assert narration.task is LLMTask.SUMMARY


@pytest.mark.anyio
async def test_the_tier_a_task_maps_to_is_not_chosen_by_this_module() -> None:
    """§2: routing is by task through TASK_TIER; no model id is named here."""
    from app.core.llm import TASK_TIER

    assert TASK_TIER[LLMTask.GOVERNANCE_REPORT] is ModelTier.FRONTIER
    assert TASK_TIER[LLMTask.SUMMARY] is ModelTier.VOLUME
