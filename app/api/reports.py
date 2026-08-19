"""Reporting endpoints — Phase 6 (CLAUDE.md §8). Everything here is a DRAFT.

    POST /reports/programs/{program_id}/governance
    GET  /reports/programs/{program_id}/feedback
    GET  /reports/colleges/{college_id}/summary

This module is I/O, authorisation and serialisation. `app/services/reporting/`
owns the assembly (pure), the narration (grounded and cited) and the R4 binding;
none of it is reimplemented here, and no figure in a response was computed in
this file.

CEILING: DRAFT (§8)
===================
"Reporting — governance report, feedback synthesis, college summaries. Ceiling:
Draft." Nothing below sends, shares, publishes or transitions anything. The
governance endpoint returns an artifact in DRAFT and says so in the body and in
the `X-Artifact-State` header, exactly as `payouts.py` does with its sheets.
`governance_reports.shared_with_college_at` — the publish gate — is not written
by any handler in this file and must not become writable from one.

R1 — WHERE EVERY FIGURE COMES FROM
==================================
Batch rolls, attendance marks, student sessions, assessment counts, observation
counts, task states, feedback scores and trainer net pay are all SELECTed here and
handed to `assemble_governance_report()` as structured input. The optional
narrative is generated from that assembled payload and checked against it by
`app.agents.grounding.assert_grounded` inside the narrator — a model that states a
figure the payload does not contain gets the whole draft refused with 422, not a
corrected sentence.

R2 — NO ARITHMETIC ON MONEY
===========================
The only monetary values in this module are `remuneration_sheets.net_amount`
figures read back off rows the engine wrote. They are not added, averaged or
prorated anywhere — not even into a programme total; `TrainerCostSection`
explains why at length. They are serialised as strings (R7).

R5 / §4 — WHICH SIDE OF THE WALL EACH ENDPOINT SITS ON
======================================================
We are on a `BYPASSRLS` service-role connection, so the policies never run for us
and the wall is reproduced in code (see `app/core/security.py`):

    delivery reporting     is_internal() AND can_reach_college()
    trainer cost section   ALSO can_see_commercials()

So an LDE Executive may pull a governance report and a college summary — that is
their daily work (§4) — and is refused the moment they ask for
`include_trainer_cost`, before any remuneration row is read. A trainer and a
College login are refused outright: §4 gives a trainer "own deployment,
tracksheet, invoice status. Nothing else", and a college sees "published artifacts
only", which a DRAFT is not.

§14 Q3 IS SURFACED, NOT ANSWERED
================================
`APPROVAL_AUTHORITY` has no entry for `governance_reports`, so a governance report
cannot currently be approved by anybody. Every response carries that fact in its
`approval` block with the reason. It is not worked around here.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections import Counter
from collections.abc import Awaitable, Sequence
from decimal import Decimal
from typing import Annotated, Any, Final
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, model_validator
from sqlalchemy import ColumnExpressionArgument, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import AuditWriter, get_audit_writer
from app.core.llm import LLMClient
from app.core.security import (
    CurrentPrincipal,
    Principal,
    require_college_reach,
    require_commercials,
    require_internal,
)
from app.db.models import (
    Assessment,
    AttendanceRecord,
    Batch,
    College,
    Deployment,
    Feedback,
    Observation,
    Program,
    RemunerationSheet,
    Student,
    Task,
    Trainer,
    TrainerAttendance,
)
from app.db.models import (
    GovernanceReport as GovernanceReportRow,
)
from app.db.session import get_session
from app.domain.enums import ArtifactState, AttendanceMark, ProgramType, TaskStatus
from app.services.reporting.assembly import (
    AssessmentFacts,
    BatchFacts,
    FeedbackEntry,
    FeedbackSynthesis,
    GovernanceReport,
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
from app.services.reporting.drafts import (
    ApprovalReadiness,
    ReportDraft,
    draft_college_summary,
    draft_governance_report,
)
from app.services.reporting.narration import (
    Narration,
    NarrationRefused,
    ReportNarrator,
)

router = APIRouter(prefix="/reports", tags=["reports"])

Session = Annotated[AsyncSession, Depends(get_session)]
Audit = Annotated[AuditWriter, Depends(get_audit_writer)]

#: Mirrors `app.api.payouts.ARTIFACT_STATE_HEADER`. A client must not be able to
#: render a downloaded draft as though it were approved (R4).
ARTIFACT_STATE_HEADER: Final[str] = "X-Artifact-State"

#: `attendance_records.status` values (`0100_enums_and_utils.sql`). There is no
#: domain enum for student attendance — `AttendanceMark` is the TRAINER vocabulary
#: and the two must not be merged (`0600_monitoring.sql` is explicit). Counting
#: through a `Counter` keyed by the raw value means no status is ever compared
#: against a string literal, which is what §11 is really about.
_PRESENT_STATUSES: Final[tuple[str, ...]] = ("present", "late")
_ABSENT_STATUS: Final[str] = "absent"

#: Amounts leave as strings. Same reasoning as `app.api.payouts.Money`: Pydantic
#: would otherwise serialise a `Decimal` as a JSON float and lose paise (R7).
MoneyOut = Annotated[Decimal, PlainSerializer(str, return_type=str, when_used="json")]
ScoreOut = Annotated[Decimal, PlainSerializer(str, return_type=str, when_used="json")]


# --- the narrator dependency --------------------------------------------------


#: Why the narrator is unavailable, kept from `get_narrator` so the 503 can
#: still name the missing configuration at the point of use.
_NARRATOR_UNAVAILABLE: Final[str] = "_narrator_unavailable_reason"


def get_narrator() -> ReportNarrator | None:
    """The report narrator, built on the sole gateway (§2). `None` when unconfigured.

    A dependency rather than a module-level singleton so a test can override it
    with a canned `Completer` — no test in this repo may reach OpenRouter.

    THIS USED TO RAISE THE 503 ITSELF, AND THAT WAS A BUG.
    FastAPI resolves every `Depends` parameter BEFORE the handler body runs, so
    a raise here fired on every call to these three endpoints — including
    `include_narrative=false`, which needs no narrator at all. The message told
    the caller to "request the report without include_narrative", and that path
    was equally dead. In a Phase 1 deployment (§13: "Phase 1 has no AI in it"),
    where the OpenRouter variables are unset by design, `/reports/*` returned 503
    for everything.

    No test caught it because every test overrides this dependency, so the
    failing branch was the one branch never exercised.

    Returning `None` moves the decision to `_require_narrator()`, at the point
    where a narrator is actually wanted. `LLMClient()` raising is a
    configuration state, not a request error, so it is not an exception here.
    """
    try:
        return ReportNarrator(llm=LLMClient())
    except RuntimeError as exc:
        setattr(get_narrator, _NARRATOR_UNAVAILABLE, str(exc))
        return None


def _require_narrator(narrator: ReportNarrator | None) -> ReportNarrator:
    """The narrator, or a 503 naming what is missing.

    Called only on the branch that genuinely needs narration, so a facts-only
    report is unaffected by narration being switched off.
    """
    if narrator is not None:
        return narrator
    reason = getattr(get_narrator, _NARRATOR_UNAVAILABLE, "OpenRouter is not configured.")
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=(
            f"Report narration is not configured in this environment: {reason} "
            "Request the report without `include_narrative` for the facts alone — "
            "that path works and returns the figures, which come from the database "
            "rather than from the model (R1)."
        ),
    )


Narrator = Annotated[ReportNarrator | None, Depends(get_narrator)]


# --- requests -----------------------------------------------------------------


class GovernanceReportRequest(BaseModel):
    """One program, one reporting period.

    `extra="forbid"`: a mistyped `include_trainer_cost` must be a 422 and not a
    silently non-commercial report — the failure would be safe here, but a
    silently ignored flag on a report somebody then acts on is not.
    """

    model_config = ConfigDict(extra="forbid")

    period_start: dt.date
    period_end: dt.date

    include_trainer_cost: bool = Field(
        default=False,
        description=(
            "Include the COMMERCIAL trainer-cost section. Senior Manager and "
            "Manager only (§4); anyone else is refused before any remuneration "
            "row is read. Default false — a report is non-commercial unless "
            "somebody asked for the other kind."
        ),
    )
    include_narrative: bool = Field(
        default=False,
        description=(
            "Generate the prose narrative. Off by default: the facts are the "
            "artifact, and a caller who only needs the numbers should not spend a "
            "frontier-tier call to get them."
        ),
    )
    as_of: dt.date | None = Field(
        default=None,
        description=(
            "The date task overdue-ness is measured against. Defaults to the end "
            "of the period, never to today: a report regenerated for an old "
            "period must report what was true then."
        ),
    )

    @model_validator(mode="after")
    def _check_period(self) -> GovernanceReportRequest:
        if self.period_end < self.period_start:
            raise ValueError(f"period_end {self.period_end} precedes period_start")
        return self


# --- responses ----------------------------------------------------------------


class ApprovalOut(BaseModel):
    """Whether this draft can be approved, and by whom. §14 Q3 lives here."""

    model_config = ConfigDict(frozen=True)

    artifact_type: str
    can_be_approved: bool
    approvers: tuple[str, ...]
    blocked_reason: str | None = Field(
        description=(
            "Why approval is impossible, when it is. Today: no approval authority "
            "is defined for a governance report because CLAUDE.md §14 Q3 is open. "
            "The draft is still worth producing and reading."
        )
    )

    @classmethod
    def of(cls, readiness: ApprovalReadiness) -> ApprovalOut:
        return cls(
            artifact_type=readiness.artifact_type.value,
            can_be_approved=readiness.can_be_approved,
            approvers=tuple(p.value for p in readiness.approvers),
            blocked_reason=readiness.blocked_reason,
        )


class NarrativeOut(BaseModel):
    """The generated prose, plus the §11 telemetry of the call that produced it."""

    model_config = ConfigDict(frozen=True)

    body: str
    llm_task: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int

    @classmethod
    def of(cls, narration: Narration) -> NarrativeOut:
        return cls(
            body=narration.body,
            llm_task=narration.task.value,
            model=narration.model,
            prompt_tokens=narration.prompt_tokens,
            completion_tokens=narration.completion_tokens,
            latency_ms=narration.latency_ms,
        )


class TrainerCostLineOut(BaseModel):
    """One trainer's net pay, as the engine persisted it. COMMERCIAL (§4)."""

    model_config = ConfigDict(frozen=True)

    trainer: str
    pan: str
    period_start: dt.date
    period_end: dt.date
    net: MoneyOut | None
    invoice_no: str | None


class TrainerCostOut(BaseModel):
    """The commercial section. No total — R2 keeps money arithmetic in the engine."""

    model_config = ConfigDict(frozen=True)

    payout_count: int
    lines: tuple[TrainerCostLineOut, ...]
    trainers_without_payout: tuple[str, ...]
    total: None = Field(
        default=None,
        description=(
            "Always null. CLAUDE.md R2 puts all monetary arithmetic in "
            "services/remuneration/engine.py, where it is unit-tested; a sum "
            "computed in a reporting endpoint would be a second implementation of "
            "money that nobody reconciles."
        ),
    )


class FeedbackSynthesisOut(BaseModel):
    """Feedback across collections. Computed in `assembly.py`, never by a model."""

    model_config = ConfigDict(frozen=True)

    collections: int
    scored_collections: int = Field(
        description=(
            "How many collections carried a score. The average is over these, not "
            "over `collections` — an average silently taken over part of the data "
            "is how a wrong number reaches a college meeting."
        )
    )
    total_responses: int | None
    average_score: ScoreOut | None
    lowest_score: ScoreOut | None
    highest_score: ScoreOut | None

    @classmethod
    def of(cls, synthesis: FeedbackSynthesis) -> FeedbackSynthesisOut:
        return cls(
            collections=synthesis.collections,
            scored_collections=synthesis.scored_collections,
            total_responses=synthesis.total_responses,
            average_score=synthesis.average_score,
            lowest_score=synthesis.lowest_score,
            highest_score=synthesis.highest_score,
        )


class GovernanceReportOut(BaseModel):
    """A DRAFT governance report: facts, optional prose, and its approval state."""

    model_config = ConfigDict(frozen=True)

    artifact_id: UUID
    artifact_state: ArtifactState = Field(
        default=ArtifactState.DRAFT,
        description="Always DRAFT. This endpoint cannot approve or release (R4, §8).",
    )
    artifact_version: int
    payload_hash: str = Field(
        description=(
            "sha256 over the payload as it stands. Not a freeze — approval is what "
            "freezes (R4) — but it lets a caller prove the draft it is holding is "
            "the one it was given."
        )
    )
    is_commercial: bool = Field(
        description="True when the trainer-cost section is present; the report is then walled."
    )

    title: str
    program_id: UUID
    college_name: str
    program_name: str
    program_type: ProgramType
    period_start: dt.date
    period_end: dt.date

    batch_count: int
    trainer_count: int
    incomplete_tracksheet_count: int = Field(
        description=(
            "Deployments whose marks do not cover the period. §5: an unmarked day "
            "silently pays a bCAP trainer and silently underpays a CRT one, so this "
            "is an operational gap and not a cosmetic one."
        )
    )
    student_attendance_percent: ScoreOut | None
    assessments_conducted: int
    observations: int
    tasks_open: int
    tasks_overdue: int

    feedback: FeedbackSynthesisOut
    trainer_cost: TrainerCostOut | None
    narrative: NarrativeOut | None
    approval: ApprovalOut

    #: Published reports already on file for this program. Read-only context — a
    #: governance report is periodic, and the useful question when drafting one is
    #: what the last one said.
    prior_reports: tuple[str, ...]


class CollegeSummaryLineOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    program_id: UUID
    program_name: str
    program_type: ProgramType
    stage: str
    batch_count: int
    trainer_count: int
    incomplete_tracksheets: int


class CollegeSummaryOut(BaseModel):
    """Every program at one college. Carries no commercials by construction (§4)."""

    model_config = ConfigDict(frozen=True)

    artifact_id: UUID
    artifact_state: ArtifactState = ArtifactState.DRAFT
    title: str
    college_id: UUID
    college_name: str
    period_start: dt.date
    period_end: dt.date
    programs: tuple[CollegeSummaryLineOut, ...]
    narrative: NarrativeOut | None
    approval: ApprovalOut


class ProgramFeedbackOut(BaseModel):
    """Feedback synthesis for one program. Not an artifact — nothing to approve."""

    model_config = ConfigDict(frozen=True)

    program_id: UUID
    program_name: str
    period_start: dt.date
    period_end: dt.date
    synthesis: FeedbackSynthesisOut
    entries: tuple[FeedbackEntryOut, ...]
    narrative: NarrativeOut | None


class FeedbackEntryOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    source: str
    collected_on: dt.date | None
    summary_score: ScoreOut | None
    response_count: int | None


ProgramFeedbackOut.model_rebuild()


# --- authorisation ------------------------------------------------------------


async def _authorised_program(
    session: AsyncSession, principal: Principal, program_id: UUID
) -> tuple[Program, College]:
    """Load a program and its college, or refuse. Persona first, then reach.

    `require_internal()` runs before the row is fetched, so a trainer or a College
    login never learns whether the program id exists. Reach is checked after,
    because it cannot be checked without knowing which college the program belongs
    to — the same order `programs.py` uses.
    """
    require_internal(principal)
    program = await session.get(Program, program_id)
    if program is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Program not found")
    require_college_reach(principal, program.college_id)

    college = await session.get(College, program.college_id)
    if college is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Program is not attached to a college",
        )
    return program, college


async def _authorised_college(
    session: AsyncSession, principal: Principal, college_id: UUID
) -> College:
    """The college, if the caller is internal staff who reach it."""
    require_internal(principal)
    require_college_reach(principal, college_id)
    college = await session.get(College, college_id)
    if college is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="College not found")
    return college


# --- reading ------------------------------------------------------------------


async def _all(
    session: AsyncSession, model: type[Any], *criteria: ColumnExpressionArgument[bool]
) -> Sequence[Any]:
    """`SELECT * FROM model WHERE ...`, as a list. The only query shape here."""
    return (await session.execute(select(model).where(*criteria))).scalars().all()


async def _delivery_inputs(
    session: AsyncSession, program: Program, period: ReportPeriod, as_of: dt.date
) -> tuple[
    tuple[BatchFacts, ...],
    tuple[TrainerDeliveryFacts, ...],
    StudentAttendanceFacts,
    AssessmentFacts,
    int,
    TaskFacts,
    tuple[Deployment, ...],
]:
    """Every delivery fact for one program-period, read from the systems of record.

    Returned as a tuple of already-shaped `assembly` inputs rather than as raw
    rows: the shaping is where "which column means what" is decided, and doing it
    once here keeps the three endpoints from each deciding it slightly
    differently.

    The deployments come back too, because the commercial section needs their
    trainers and re-querying them under a different filter is how two sections of
    one report end up describing different trainers.
    """
    batches = await _all(session, Batch, Batch.program_id == program.id)
    batch_ids = [b.id for b in batches]

    students = await _all(session, Student, Student.batch_id.in_(batch_ids)) if batch_ids else ()
    per_batch = Counter(s.batch_id for s in students)
    batch_facts = tuple(
        BatchFacts(
            batch_id=b.id,
            name=b.name,
            student_count=per_batch.get(b.id, 0),
            expected_student_count=b.expected_student_count,
        )
        for b in batches
    )

    deployments = (
        await _all(session, Deployment, Deployment.batch_id.in_(batch_ids)) if batch_ids else ()
    )
    deployment_ids = [d.id for d in deployments]
    trainers = (
        {
            t.id: t
            for t in await _all(
                session, Trainer, Trainer.id.in_([d.trainer_id for d in deployments])
            )
        }
        if deployments
        else {}
    )
    batch_names = {b.id: b.name for b in batches}

    marks = (
        await _all(
            session,
            TrainerAttendance,
            TrainerAttendance.deployment_id.in_(deployment_ids),
            TrainerAttendance.mark_date >= period.start,
            TrainerAttendance.mark_date <= period.end,
        )
        if deployment_ids
        else ()
    )
    by_deployment: dict[UUID, Counter[AttendanceMark]] = {}
    seen_days: dict[UUID, set[dt.date]] = {}
    for row in marks:
        by_deployment.setdefault(row.deployment_id, Counter())[row.mark] += 1
        seen_days.setdefault(row.deployment_id, set()).add(row.mark_date)

    trainer_facts = tuple(
        TrainerDeliveryFacts(
            deployment_id=d.id,
            trainer_name=(
                trainers[d.trainer_id].full_name if d.trainer_id in trainers else "unknown"
            ),
            batch_name=batch_names.get(d.batch_id, "unknown"),
            days_in_period=period.days,
            # Distinct days, not row count: §6 is one row per day, and a duplicated
            # day must not be able to report a tracksheet as more complete than it
            # is. `payouts.py` refuses outright on a duplicate; a report says less.
            marked_days=len(seen_days.get(d.id, ())),
            present_days=by_deployment.get(d.id, Counter()).get(AttendanceMark.PRESENT, 0),
            absent_days=by_deployment.get(d.id, Counter()).get(AttendanceMark.ABSENT, 0),
        )
        for d in deployments
    )

    sessions = (
        await _all(
            session,
            AttendanceRecord,
            AttendanceRecord.deployment_id.in_(deployment_ids),
            AttendanceRecord.session_date >= period.start,
            AttendanceRecord.session_date <= period.end,
        )
        if deployment_ids
        else ()
    )
    statuses = Counter(r.status for r in sessions)
    student_attendance = StudentAttendanceFacts(
        sessions=len(sessions),
        present=sum(statuses.get(s, 0) for s in _PRESENT_STATUSES),
        absent=statuses.get(_ABSENT_STATUS, 0),
    )

    assessments = (
        await _all(
            session,
            Assessment,
            Assessment.batch_id.in_(batch_ids),
            Assessment.conducted_on >= period.start,
            Assessment.conducted_on <= period.end,
        )
        if batch_ids
        else ()
    )
    assessment_facts = AssessmentFacts(
        conducted=len(assessments),
        with_report=sum(1 for a in assessments if a.report_url),
    )

    observations = (
        await _all(
            session,
            Observation,
            Observation.deployment_id.in_(deployment_ids),
            Observation.observed_on >= period.start,
            Observation.observed_on <= period.end,
        )
        if deployment_ids
        else ()
    )

    tasks = await _all(session, Task, Task.program_id == program.id)
    task_facts = TaskFacts(
        open=sum(
            1 for t in tasks if t.status is TaskStatus.PENDING or t.status is TaskStatus.IN_PROGRESS
        ),
        overdue=sum(
            1
            for t in tasks
            if t.due_date is not None and t.due_date < as_of and t.status is not TaskStatus.DONE
        ),
        blocked=sum(1 for t in tasks if t.status is TaskStatus.BLOCKED),
        done=sum(1 for t in tasks if t.status is TaskStatus.DONE),
    )

    return (
        batch_facts,
        trainer_facts,
        student_attendance,
        assessment_facts,
        len(observations),
        task_facts,
        tuple(deployments),
    )


async def _feedback_entries(
    session: AsyncSession, batch_ids: Sequence[UUID], period: ReportPeriod
) -> tuple[FeedbackEntry, ...]:
    """Feedback collections for these batches in the period.

    A collection with no `collected_on` is INCLUDED. It is a real collection whose
    date nobody recorded, and dropping it would quietly shrink the denominator of
    the synthesis — the failure `FeedbackSynthesis` is written to make visible.
    """
    if not batch_ids:
        return ()
    rows = await _all(session, Feedback, Feedback.batch_id.in_(list(batch_ids)))
    return tuple(
        FeedbackEntry(
            source=row.source,
            collected_on=row.collected_on,
            summary_score=row.summary_score,
            response_count=row.response_count,
            external_url=row.external_url,
        )
        for row in rows
        if row.collected_on is None or period.start <= row.collected_on <= period.end
    )


async def _trainer_cost(
    session: AsyncSession,
    program: Program,
    deployments: Sequence[Deployment],
    period: ReportPeriod,
) -> TrainerCostSection:
    """The COMMERCIAL section (§4, R5). Caller must have cleared the wall already.

    Every figure is a persisted `net_amount` the engine wrote (R2). Nothing is
    recomputed and nothing is summed. `missing_payouts` names deployed trainers
    with no sheet overlapping the period, which is the governance signal that
    matters — a programme reported as delivered with unpaid trainers.
    """
    trainer_ids = sorted({d.trainer_id for d in deployments})
    if not trainer_ids:
        return TrainerCostSection(lines=())

    trainers = {t.id: t for t in await _all(session, Trainer, Trainer.id.in_(trainer_ids))}
    sheets = await _all(
        session,
        RemunerationSheet,
        RemunerationSheet.trainer_id.in_(trainer_ids),
        RemunerationSheet.program_id == program.id,
        RemunerationSheet.period_start <= period.end,
        RemunerationSheet.period_end >= period.start,
    )

    lines = tuple(
        TrainerCostLine(
            trainer_name=(
                trainers[s.trainer_id].full_name if s.trainer_id in trainers else "unknown"
            ),
            trainer_pan=trainers[s.trainer_id].pan if s.trainer_id in trainers else "",
            period_start=s.period_start,
            period_end=s.period_end,
            net=s.net_amount,
            invoice_no=s.invoice_no,
        )
        for s in sorted(sheets, key=lambda s: s.period_start)
    )
    paid = {s.trainer_id for s in sheets}
    missing = tuple(
        sorted(trainers[t].full_name for t in trainer_ids if t not in paid and t in trainers)
    )
    return TrainerCostSection(lines=lines, missing_payouts=missing)


# --- endpoints ----------------------------------------------------------------


@router.post(
    "/programs/{program_id}/governance",
    response_model=GovernanceReportOut,
    status_code=status.HTTP_200_OK,
    summary="Assemble a DRAFT governance report for one program and period",
)
async def draft_governance(
    program_id: UUID,
    request: GovernanceReportRequest,
    principal: CurrentPrincipal,
    session: Session,
    audit: Audit,
    response: Response,
    narrator: Narrator,
) -> GovernanceReportOut:
    """Read the period's delivery facts, assemble them, optionally narrate them.

    The result is a DRAFT artifact (R4) that this endpoint does not persist and
    cannot approve. It writes one audit row recording that a draft was produced,
    who asked for it, and whether it was the commercial variety — §11 wants the
    trail, and "who generated a report containing trainer cost" is a question
    worth being able to answer later.

    A `include_trainer_cost` request from a persona outside the commercials wall
    is refused before any remuneration row is read (R5), and the refusal is on the
    section, not on the report: the caller may ask again without it.

    If the model states a figure the assembled facts do not contain, the draft is
    refused with 422 rather than returned with a corrected sentence — R1, enforced
    by `assert_grounded` inside the narrator. Both that and a §9 citation failure
    surface as a 422 naming the rule, because a report the platform could not
    stand behind is a failure of the request, not a partial success.
    """
    program, college = await _authorised_program(session, principal, program_id)
    period = _period(request.period_start, request.period_end)
    as_of = request.as_of or period.end

    # R5: the wall closes before the remuneration rows are read, not after.
    if request.include_trainer_cost:
        require_commercials(principal)

    (
        batches,
        trainers,
        student_attendance,
        assessments,
        observations,
        tasks,
        deployments,
    ) = await _delivery_inputs(session, program, period, as_of)

    feedback = await _feedback_entries(session, [b.batch_id for b in batches], period)
    cost = (
        await _trainer_cost(session, program, deployments, period)
        if request.include_trainer_cost
        else None
    )

    report = assemble_governance_report(
        program=ProgramFacts(
            program_id=program.id,
            program_name=program.name,
            program_type=program.type,
            stage=program.stage,
            college_id=college.id,
            college_name=college.name,
            starts_on=program.start_date,
            ends_on=program.end_date,
        ),
        period=period,
        batches=batches,
        trainers=trainers,
        student_attendance=student_attendance,
        assessments=assessments,
        observations=observations,
        tasks=tasks,
        feedback=feedback,
        trainer_cost=cost,
    )

    narration = (
        await _narrate(_require_narrator(narrator).narrate_governance(report))
        if request.include_narrative
        else None
    )
    draft = draft_governance_report(
        report,
        artifact_id=uuid.uuid4(),
        actor_id=principal.user_id,
        actor_persona=principal.persona,
        narrative=narration,
    )
    await audit.write(draft.event)

    prior = await _all(session, GovernanceReportRow, GovernanceReportRow.program_id == program.id)
    response.headers[ARTIFACT_STATE_HEADER] = ArtifactState.DRAFT.value
    return _governance_out(report, draft, narration, prior)


@router.get(
    "/programs/{program_id}/feedback",
    response_model=ProgramFeedbackOut,
    status_code=status.HTTP_200_OK,
    summary="Synthesise collected feedback for one program and period",
)
async def program_feedback(
    program_id: UUID,
    principal: CurrentPrincipal,
    session: Session,
    narrator: Narrator,
    period_start: Annotated[dt.date, Query()],
    period_end: Annotated[dt.date, Query()],
    include_narrative: Annotated[bool, Query()] = False,
) -> ProgramFeedbackOut:
    """The §8 "feedback synthesis", computed deterministically.

    Internal, and available to an LDE Executive: feedback is delivery data, not
    commercials (§4). No artifact and no audit row — this is a read, and §11 wants
    audit rows for state transitions.

    The average is `assembly.synthesise_feedback()`'s, in `Decimal`, over the
    collections that carried a score. The model, when asked, explains it and may
    not restate it as anything else (R1).
    """
    program, _ = await _authorised_program(session, principal, program_id)
    period = _period(period_start, period_end)

    batches = await _all(session, Batch, Batch.program_id == program.id)
    entries = await _feedback_entries(session, [b.id for b in batches], period)
    synthesis = synthesise_feedback(entries)

    narration = (
        await _narrate(
            _require_narrator(narrator).narrate_feedback(synthesis, program_name=program.name)
        )
        if include_narrative
        else None
    )
    return ProgramFeedbackOut(
        program_id=program.id,
        program_name=program.name,
        period_start=period.start,
        period_end=period.end,
        synthesis=FeedbackSynthesisOut.of(synthesis),
        entries=tuple(
            FeedbackEntryOut(
                source=e.source,
                collected_on=e.collected_on,
                summary_score=e.summary_score,
                response_count=e.response_count,
            )
            for e in entries
        ),
        narrative=NarrativeOut.of(narration) if narration else None,
    )


@router.get(
    "/colleges/{college_id}/summary",
    response_model=CollegeSummaryOut,
    status_code=status.HTTP_200_OK,
    summary="Summarise every program running at one college",
)
async def college_summary(
    college_id: UUID,
    principal: CurrentPrincipal,
    session: Session,
    narrator: Narrator,
    period_start: Annotated[dt.date, Query()],
    period_end: Annotated[dt.date, Query()],
    include_narrative: Annotated[bool, Query()] = False,
) -> CollegeSummaryOut:
    """Every program at the college, with its stage and delivery state.

    Carries no commercial data at all — not behind a flag, not empty-but-present.
    §4 makes this the view an LDE Executive works from, and a section that exists
    for them but is blank is one refactor away from not being blank.

    Returned as a DRAFT artifact for the same reason the governance report is: a
    summary sent to a college is a college-facing communication, and R4 admits no
    path from here to a college that does not pass a human.
    """
    college = await _authorised_college(session, principal, college_id)
    period = _period(period_start, period_end)

    programs = await _all(session, Program, Program.college_id == college.id)
    lines: list[ProgramSummaryLine] = []
    for program in programs:
        batches, trainers, _, _, _, _, _ = await _delivery_inputs(
            session, program, period, period.end
        )
        lines.append(
            ProgramSummaryLine(
                program_id=program.id,
                program_name=program.name,
                program_type=program.type,
                stage=program.stage,
                batch_count=len(batches),
                trainer_count=len(trainers),
                incomplete_tracksheets=sum(1 for t in trainers if not t.is_complete),
                starts_on=program.start_date,
                ends_on=program.end_date,
            )
        )

    summary = summarise_college(
        college_id=college.id,
        college_name=college.name,
        period=period,
        programs=lines,
    )
    narration = (
        await _narrate(_require_narrator(narrator).narrate_college_summary(summary))
        if include_narrative
        else None
    )
    draft = draft_college_summary(
        summary,
        artifact_id=uuid.uuid4(),
        actor_id=principal.user_id,
        actor_persona=principal.persona,
        narrative=narration,
    )
    return CollegeSummaryOut(
        artifact_id=draft.artifact.artifact_id,
        title=summary.title,
        college_id=summary.college_id,
        college_name=summary.college_name,
        period_start=period.start,
        period_end=period.end,
        programs=tuple(
            CollegeSummaryLineOut(
                program_id=line.program_id,
                program_name=line.program_name,
                program_type=line.program_type,
                stage=line.stage.value,
                batch_count=line.batch_count,
                trainer_count=line.trainer_count,
                incomplete_tracksheets=line.incomplete_tracksheets,
            )
            for line in summary.programs
        ),
        narrative=NarrativeOut.of(narration) if narration else None,
        approval=ApprovalOut.of(draft.approval),
    )


# --- adapters -----------------------------------------------------------------


def _period(start: dt.date, end: dt.date) -> ReportPeriod:
    """Build a `ReportPeriod`, turning its refusal into a 422 rather than a 500."""
    try:
        return ReportPeriod(start=start, end=end)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc


async def _narrate(awaitable: Awaitable[Narration]) -> Narration:
    """Await a narration, turning R1 and §9 refusals into a 422 that names the rule.

    Not a 500: the platform behaved correctly — it caught a model stating a figure
    nobody gave it, or citing a source that was not retrieved, and threw the draft
    away rather than shipping it. The caller's request could not be satisfied, and
    the message says exactly why so the failure is not mistaken for a bug in the
    report.

    Deliberately no retry. A second roll of the dice would hide a systematic
    problem behind an occasional extra call, which is the reasoning
    `app.agents.grounding` states for raising in the first place.
    """
    from app.agents.grounding import UngroundedFigureError

    try:
        return await awaitable
    except UngroundedFigureError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"The generated narrative stated a figure that was not in the report's "
                f"structured data, so the draft was refused: {exc}"
            ),
        ) from exc
    except NarrationRefused as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(f"The generated narrative failed CLAUDE.md §9 and was discarded: {exc}"),
        ) from exc


def _governance_out(
    report: GovernanceReport,
    draft: ReportDraft,
    narration: Narration | None,
    prior: Sequence[GovernanceReportRow],
) -> GovernanceReportOut:
    """Copy the assembled report onto the wire. Pass-through, never re-derivation."""
    cost = report.trainer_cost
    return GovernanceReportOut(
        artifact_id=draft.artifact.artifact_id,
        artifact_version=draft.artifact.version,
        payload_hash=draft.artifact.payload_hash(),
        is_commercial=report.is_commercial,
        title=report.title,
        program_id=report.program.program_id,
        college_name=report.program.college_name,
        program_name=report.program.program_name,
        program_type=report.program.program_type,
        period_start=report.period.start,
        period_end=report.period.end,
        batch_count=len(report.delivery.batches),
        trainer_count=len(report.delivery.trainers),
        incomplete_tracksheet_count=len(report.delivery.incomplete_tracksheets),
        student_attendance_percent=report.delivery.student_attendance.attendance_percent,
        assessments_conducted=report.delivery.assessments.conducted,
        observations=report.delivery.observations,
        tasks_open=report.delivery.tasks.open,
        tasks_overdue=report.delivery.tasks.overdue,
        feedback=FeedbackSynthesisOut.of(report.feedback),
        trainer_cost=(
            TrainerCostOut(
                payout_count=len(cost.lines),
                lines=tuple(
                    TrainerCostLineOut(
                        trainer=line.trainer_name,
                        pan=line.trainer_pan,
                        period_start=line.period_start,
                        period_end=line.period_end,
                        net=line.net,
                        invoice_no=line.invoice_no,
                    )
                    for line in cost.lines
                ),
                trainers_without_payout=cost.missing_payouts,
            )
            if cost is not None
            else None
        ),
        narrative=NarrativeOut.of(narration) if narration else None,
        approval=ApprovalOut.of(draft.approval),
        prior_reports=tuple(r.title or r.url for r in prior),
    )
