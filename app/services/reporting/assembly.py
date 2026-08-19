"""Governance report assembly. CLAUDE.md §8: "Reporting". Ceiling: Draft.

    §8  "Reporting — governance report, feedback synthesis, college summaries."
    R1  "The database owns truth. The LLM owns language. No agent may assert a
         fact it did not read from a system of record."

WHAT THIS MODULE IS, AND WHAT IT DELIBERATELY IS NOT
====================================================
This is the *facts* half of Phase 6. It takes rows the caller has already read
from the systems of record and folds them into the sections a governance report
is made of. It performs no I/O, holds no session, imports nothing from `app/db/`
or `app/api/`, and — most importantly — never calls a model. The prose half lives
in `narration.py`, and it may only quote figures that appear on the payload this
module produces (`app.agents.grounding.assert_grounded` is what checks that).

The split is R1 in the file layout. A reporting service that fetched, computed
and narrated in one function would make "did the model invent this number?" an
unanswerable question; here, the numbers exist as a frozen dataclass before a
prompt is built, and the grounding check compares the two.

WHICH SIDE OF THE COMMERCIALS WALL EACH SECTION SITS ON (§4, R5)
================================================================
This matters more here than anywhere else in Phase 6, because a governance report
is the one artifact that plausibly wants both kinds of data in one document.

    DeliverySection      NOT commercial. Attendance completeness, student
                         attendance, assessments, observations, task state. An
                         LDE Executive may see all of it.
    FeedbackSynthesis    NOT commercial. Scores and response counts.
    CollegeSummary       NOT commercial. Programs, batches, stages, delivery.
    TrainerCostSection   **COMMERCIAL.** Net pay per trainer, read back from
                         `remuneration_sheets`. Senior Manager and Manager only
                         (`can_see_commercials()`), and the caller must have
                         already refused everybody else — this module cannot
                         check a persona because it has none.

`GovernanceReport.trainer_cost` is therefore `None` by default and the API layer
must not populate it for a persona that fails `require_commercials()`. A report
carrying that section is a commercial document and its `is_commercial` flag says
so, so a caller cannot lose track of which one it is holding.

WHY THERE IS NO TOTAL COST
==========================
`TrainerCostSection` lists each trainer's net pay and does not add them up. R2:
"All monetary arithmetic lives in `services/remuneration/engine.py`, is pure
Python, uses `Decimal`, and is unit-tested." A sum written here would be a second,
untested implementation of money living in a reporting module, and the first time
it disagreed with Finance nobody would know which one was wrong. If a governance
report needs a programme cost total, it belongs in the engine with a fixture
behind it. This is recorded as a known gap rather than papered over.

R7 AND THE NON-MONEY DECIMALS
=============================
Scores and percentages are `Decimal` too, and quantised exactly once at the end
(R6's discipline applied outside money): a feedback average that changes in the
last digit between two runs of the same report is a support ticket, and a float
average would do exactly that.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Final
from uuid import UUID

from pydantic import JsonValue

from app.domain.enums import ProgramStage, ProgramType

__all__ = [
    "AssessmentFacts",
    "BatchFacts",
    "CollegeSummary",
    "DeliverySection",
    "FeedbackEntry",
    "FeedbackSynthesis",
    "GovernanceReport",
    "ProgramFacts",
    "ProgramSummaryLine",
    "ReportPeriod",
    "StudentAttendanceFacts",
    "TaskFacts",
    "TrainerCostLine",
    "TrainerCostSection",
    "TrainerDeliveryFacts",
    "assemble_governance_report",
    "summarise_college",
    "synthesise_feedback",
]

#: Percentages are reported to one decimal place, scores to two. Both are
#: quantised once, at the end of the only calculation they appear in.
_PERCENT_PLACES: Final[Decimal] = Decimal("0.1")
_SCORE_PLACES: Final[Decimal] = Decimal("0.01")
_HUNDRED: Final[Decimal] = Decimal("100")


def _ratio_percent(part: int, whole: int) -> Decimal | None:
    """`part/whole` as a percentage, or `None` when the denominator is zero.

    `None`, never `0.0`. "No sessions were held" and "nobody attended" are
    different statements about a program and a report that renders them
    identically will eventually be used to justify the wrong decision. Multiply
    before dividing, for the reason §6 gives about the per-month payout path: it
    keeps the repeating decimal out of the intermediate.
    """
    if whole <= 0:
        return None
    return (Decimal(part) * _HUNDRED / Decimal(whole)).quantize(
        _PERCENT_PLACES, rounding=ROUND_HALF_UP
    )


# --- inputs: what the caller read from the systems of record ------------------


@dataclass(frozen=True, slots=True)
class ReportPeriod:
    """The window a report covers. Business dates, IST-facing (§11)."""

    start: dt.date
    end: dt.date

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError(f"report period ends {self.end} before it starts {self.start}")

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def as_payload(self) -> dict[str, JsonValue]:
        return {"start": self.start.isoformat(), "end": self.end.isoformat(), "days": self.days}


@dataclass(frozen=True, slots=True)
class ProgramFacts:
    """Identity and stage of the program being reported on. Not commercial."""

    program_id: UUID
    program_name: str
    program_type: ProgramType
    stage: ProgramStage
    college_id: UUID
    college_name: str
    starts_on: dt.date | None = None
    ends_on: dt.date | None = None

    def as_payload(self) -> dict[str, JsonValue]:
        return {
            "program_id": str(self.program_id),
            "program_name": self.program_name,
            "program_type": self.program_type.value,
            "stage": self.stage.value,
            "college_name": self.college_name,
            "starts_on": self.starts_on.isoformat() if self.starts_on else None,
            "ends_on": self.ends_on.isoformat() if self.ends_on else None,
        }


@dataclass(frozen=True, slots=True)
class BatchFacts:
    """One cohort. `expected_student_count` is the plan, `student_count` the roll."""

    batch_id: UUID
    name: str
    student_count: int = 0
    expected_student_count: int | None = None

    def as_payload(self) -> dict[str, JsonValue]:
        return {
            "batch": self.name,
            "student_count": self.student_count,
            "expected_student_count": self.expected_student_count,
        }


@dataclass(frozen=True, slots=True)
class TrainerDeliveryFacts:
    """One deployment's attendance completeness over the period.

    Counts of marks, never payable days. Payable days are a payout input whose
    semantics differ by program type (§5), and putting one in a delivery report
    would invite it to be quoted at a trainer as though it were settled. This
    section reports whether the tracksheet is complete; what it is worth is
    `app/services/remuneration/`'s to say.
    """

    deployment_id: UUID
    trainer_name: str
    batch_name: str
    days_in_period: int
    marked_days: int
    present_days: int
    absent_days: int

    @property
    def unmarked_days(self) -> int:
        return self.days_in_period - self.marked_days

    @property
    def is_complete(self) -> bool:
        """§5: an unmarked day silently pays a bCAP trainer and underpays a CRT one."""
        return self.unmarked_days == 0

    def as_payload(self) -> dict[str, JsonValue]:
        return {
            "trainer": self.trainer_name,
            "batch": self.batch_name,
            "days_in_period": self.days_in_period,
            "marked_days": self.marked_days,
            "unmarked_days": self.unmarked_days,
            "present_days": self.present_days,
            "absent_days": self.absent_days,
            "tracksheet_complete": self.is_complete,
        }


@dataclass(frozen=True, slots=True)
class StudentAttendanceFacts:
    """Student attendance, from `attendance_records` — NOT the trainer tracksheet.

    The two tables are deliberately separate (see `0600_monitoring.sql`), and
    conflating them in a report is how a student absence ends up deducting a
    trainer's pay in somebody's mental model.
    """

    sessions: int = 0
    present: int = 0
    absent: int = 0

    @property
    def attendance_percent(self) -> Decimal | None:
        return _ratio_percent(self.present, self.sessions)

    def as_payload(self) -> dict[str, JsonValue]:
        percent = self.attendance_percent
        return {
            "sessions": self.sessions,
            "present": self.present,
            "absent": self.absent,
            "attendance_percent": str(percent) if percent is not None else None,
        }


@dataclass(frozen=True, slots=True)
class AssessmentFacts:
    """Assessments held in the period, and how many produced a report package."""

    conducted: int = 0
    with_report: int = 0

    def as_payload(self) -> dict[str, JsonValue]:
        return {
            "assessments_conducted": self.conducted,
            "assessments_with_report": self.with_report,
        }


@dataclass(frozen=True, slots=True)
class TaskFacts:
    """Programme obligations, as the tracker holds them.

    `overdue` is computed by the caller against a date it passes in, never against
    `date.today()` in here — the same rule `app.agents.ports.TaskSnapshot` states,
    so a report regenerated for an old period reports what was true then.
    """

    open: int = 0
    overdue: int = 0
    blocked: int = 0
    done: int = 0

    def as_payload(self) -> dict[str, JsonValue]:
        return {
            "tasks_open": self.open,
            "tasks_overdue": self.overdue,
            "tasks_blocked": self.blocked,
            "tasks_done": self.done,
        }


@dataclass(frozen=True, slots=True)
class FeedbackEntry:
    """One feedback collection. Responses stay in the external form (§10 spirit).

    `summary_score` is what the collection recorded; this service never invents one
    for a collection that has none, and never averages a missing score as zero.
    """

    source: str
    collected_on: dt.date | None = None
    summary_score: Decimal | None = None
    response_count: int | None = None
    external_url: str | None = None

    def as_payload(self) -> dict[str, JsonValue]:
        return {
            "source": self.source,
            "collected_on": self.collected_on.isoformat() if self.collected_on else None,
            "summary_score": str(self.summary_score) if self.summary_score is not None else None,
            "response_count": self.response_count,
        }


@dataclass(frozen=True, slots=True)
class TrainerCostLine:
    """One trainer's net pay for the period. **COMMERCIAL** (§4, R5).

    `net` is read back from a `remuneration_sheets` row the engine wrote (R2). It
    is carried as a `Decimal` and rendered with `str()` on the payload — never a
    float, and never recomputed here.
    """

    trainer_name: str
    trainer_pan: str
    period_start: dt.date
    period_end: dt.date
    net: Decimal | None
    invoice_no: str | None = None

    def as_payload(self) -> dict[str, JsonValue]:
        return {
            "trainer": self.trainer_name,
            "pan": self.trainer_pan,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "net": str(self.net) if self.net is not None else None,
            "invoice_no": self.invoice_no,
        }


# --- assembled sections -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeliverySection:
    """How delivery actually went. The part of a governance report a college sees."""

    batches: tuple[BatchFacts, ...]
    trainers: tuple[TrainerDeliveryFacts, ...]
    student_attendance: StudentAttendanceFacts
    assessments: AssessmentFacts
    observations: int
    tasks: TaskFacts

    @property
    def incomplete_tracksheets(self) -> tuple[TrainerDeliveryFacts, ...]:
        """Deployments whose marks do not cover the period. §5's silent failure."""
        return tuple(t for t in self.trainers if not t.is_complete)

    def as_payload(self) -> dict[str, JsonValue]:
        return {
            "batch_count": len(self.batches),
            "batches": [b.as_payload() for b in self.batches],
            "trainer_count": len(self.trainers),
            "trainers": [t.as_payload() for t in self.trainers],
            "incomplete_tracksheet_count": len(self.incomplete_tracksheets),
            "student_attendance": self.student_attendance.as_payload(),
            "observations": self.observations,
            **self.assessments.as_payload(),
            **self.tasks.as_payload(),
        }


@dataclass(frozen=True, slots=True)
class FeedbackSynthesis:
    """Feedback across collections. Deterministic; the model only narrates it.

    The average is over collections that HAVE a score, and `scored_collections`
    reports how many those were. An average silently taken over three of eight
    collections, presented as "the feedback score", is the kind of figure that
    survives into a college meeting unchallenged.

    Unweighted by response count, deliberately: `response_count` is nullable, and
    a weighting that silently treats a null as zero would drop a collection out of
    the average without saying so. Both figures are on the payload so a reader can
    see what the average is made of.
    """

    entries: tuple[FeedbackEntry, ...]
    collections: int
    scored_collections: int
    total_responses: int | None
    average_score: Decimal | None
    lowest_score: Decimal | None
    highest_score: Decimal | None

    def as_payload(self) -> dict[str, JsonValue]:
        return {
            "collections": self.collections,
            "scored_collections": self.scored_collections,
            "total_responses": self.total_responses,
            "average_score": str(self.average_score) if self.average_score is not None else None,
            "lowest_score": str(self.lowest_score) if self.lowest_score is not None else None,
            "highest_score": str(self.highest_score) if self.highest_score is not None else None,
            "entries": [e.as_payload() for e in self.entries],
        }


@dataclass(frozen=True, slots=True)
class TrainerCostSection:
    """Trainer cost for the period. **COMMERCIAL — Senior Manager / Manager only.**

    No total (see the module docstring on R2). `lines` are engine-written figures
    quoted verbatim; `missing_payouts` names deployments with no sheet on file,
    which is the actually useful governance signal — a programme reported as
    delivered with three trainers unpaid is a finding.
    """

    lines: tuple[TrainerCostLine, ...]
    missing_payouts: tuple[str, ...] = ()

    def as_payload(self) -> dict[str, JsonValue]:
        return {
            "payout_count": len(self.lines),
            "lines": [line.as_payload() for line in self.lines],
            "trainers_without_payout": list(self.missing_payouts),
            "total": None,
            "total_note": (
                "No total is computed here. CLAUDE.md R2 puts all monetary "
                "arithmetic in services/remuneration/engine.py, unit-tested."
            ),
        }


@dataclass(frozen=True, slots=True)
class GovernanceReport:
    """The assembled facts of one program over one period. Every figure is SQL-read.

    This object is the structured input for `narration.py` AND the payload that
    `app.services.approval.state_machine.Artifact` freezes at approval (R4). One
    object for both is the point: the prose is grounded against exactly the facts
    that get hashed, so "what was approved" and "what the narrative could say" can
    never drift apart.
    """

    program: ProgramFacts
    period: ReportPeriod
    delivery: DeliverySection
    feedback: FeedbackSynthesis
    #: `None` unless the caller cleared `can_see_commercials()` (§4, R5).
    trainer_cost: TrainerCostSection | None = None

    @property
    def is_commercial(self) -> bool:
        """True when this report carries trainer cost, and must be walled as such."""
        return self.trainer_cost is not None

    @property
    def title(self) -> str:
        return (
            f"Governance report — {self.program.college_name}, {self.program.program_name}, "
            f"{self.period.start.isoformat()} to {self.period.end.isoformat()}"
        )

    def as_payload(self) -> dict[str, JsonValue]:
        """The report as JSON. The frozen content (R4) and the grounding set (R1)."""
        payload: dict[str, JsonValue] = {
            "program": self.program.as_payload(),
            "period": self.period.as_payload(),
            "delivery": self.delivery.as_payload(),
            "feedback": self.feedback.as_payload(),
            "is_commercial": self.is_commercial,
        }
        if self.trainer_cost is not None:
            payload["trainer_cost"] = self.trainer_cost.as_payload()
        return payload


@dataclass(frozen=True, slots=True)
class ProgramSummaryLine:
    """One program's state, as a college summary lists it. Not commercial."""

    program_id: UUID
    program_name: str
    program_type: ProgramType
    stage: ProgramStage
    batch_count: int
    trainer_count: int
    incomplete_tracksheets: int
    starts_on: dt.date | None = None
    ends_on: dt.date | None = None

    def as_payload(self) -> dict[str, JsonValue]:
        return {
            "program_id": str(self.program_id),
            "program_name": self.program_name,
            "program_type": self.program_type.value,
            "stage": self.stage.value,
            "batch_count": self.batch_count,
            "trainer_count": self.trainer_count,
            "incomplete_tracksheets": self.incomplete_tracksheets,
            "starts_on": self.starts_on.isoformat() if self.starts_on else None,
            "ends_on": self.ends_on.isoformat() if self.ends_on else None,
        }


@dataclass(frozen=True, slots=True)
class CollegeSummary:
    """Every program at one college, with its delivery state. Not commercial.

    Deliberately carries no money at all, not even behind a flag. A college
    summary is the view an LDE Executive works from daily (§4), and a section that
    is present-but-empty for them is one refactor away from being present-and-full.
    """

    college_id: UUID
    college_name: str
    period: ReportPeriod
    programs: tuple[ProgramSummaryLine, ...]

    @property
    def title(self) -> str:
        return f"College summary — {self.college_name}"

    def as_payload(self) -> dict[str, JsonValue]:
        return {
            "college_id": str(self.college_id),
            "college_name": self.college_name,
            "period": self.period.as_payload(),
            "program_count": len(self.programs),
            "programs": [p.as_payload() for p in self.programs],
        }


# --- assembly -----------------------------------------------------------------


def synthesise_feedback(entries: Sequence[FeedbackEntry]) -> FeedbackSynthesis:
    """Fold feedback collections into one synthesis. Pure, deterministic, no model.

    §8 lists "feedback synthesis" under the Reporting agent, and the temptation is
    to hand the collections to a model and ask for a summary. R1 forbids it for the
    numbers: an average is a fact, it is computed here in `Decimal`, and the model
    is later allowed only to *explain* it — which is R2's shape ("may explain a
    number, may never produce one") applied outside money.

    Collections without a score are counted and reported but never averaged as
    zero; a null response count never becomes a zero either, so
    `total_responses` is `None` when nothing reported one at all.
    """
    scored = [e.summary_score for e in entries if e.summary_score is not None]
    counted = [e.response_count for e in entries if e.response_count is not None]
    average: Decimal | None = None
    if scored:
        average = (sum(scored, Decimal(0)) / Decimal(len(scored))).quantize(
            _SCORE_PLACES, rounding=ROUND_HALF_UP
        )
    return FeedbackSynthesis(
        entries=tuple(entries),
        collections=len(entries),
        scored_collections=len(scored),
        total_responses=sum(counted) if counted else None,
        average_score=average,
        lowest_score=min(scored) if scored else None,
        highest_score=max(scored) if scored else None,
    )


def assemble_governance_report(
    *,
    program: ProgramFacts,
    period: ReportPeriod,
    batches: Sequence[BatchFacts],
    trainers: Sequence[TrainerDeliveryFacts],
    student_attendance: StudentAttendanceFacts,
    assessments: AssessmentFacts,
    observations: int,
    tasks: TaskFacts,
    feedback: Sequence[FeedbackEntry],
    trainer_cost: TrainerCostSection | None = None,
) -> GovernanceReport:
    """Fold read rows into one report. Keyword-only, and every argument required.

    No defaults on the delivery inputs, deliberately. A caller that has not yet
    queried assessments should be made to say `AssessmentFacts()` — "we looked and
    there were none" — rather than have a report quietly claim zero assessments
    because an argument was forgotten. A governance report is read by a college.

    `trainer_cost` is the one optional argument and defaults to `None`, which is
    the safe direction: a caller who forgets it produces a non-commercial report
    (§4), never a commercial one by accident.
    """
    return GovernanceReport(
        program=program,
        period=period,
        delivery=DeliverySection(
            batches=tuple(batches),
            trainers=tuple(trainers),
            student_attendance=student_attendance,
            assessments=assessments,
            observations=observations,
            tasks=tasks,
        ),
        feedback=synthesise_feedback(feedback),
        trainer_cost=trainer_cost,
    )


def summarise_college(
    *,
    college_id: UUID,
    college_name: str,
    period: ReportPeriod,
    programs: Sequence[ProgramSummaryLine],
) -> CollegeSummary:
    """One college's programs, ordered by stage then name.

    Ordered here rather than in SQL so the ordering is testable without a database
    and identical for every caller — a summary whose rows move between two renders
    of the same period looks like the data changed.
    """
    return CollegeSummary(
        college_id=college_id,
        college_name=college_name,
        period=period,
        programs=tuple(sorted(programs, key=lambda p: (p.stage.value, p.program_name))),
    )
