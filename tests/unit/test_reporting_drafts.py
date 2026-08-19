"""`app/services/reporting/drafts.py` — R4 binding at the Draft ceiling (§8).

Two things are asserted, and the second is the one that matters:

* every artifact this package produces is DRAFT, carries no `content_hash`, and
  arrives with the `AuditEvent` that records its creation (§11);
* a governance report **cannot currently be approved**, because
  `APPROVAL_AUTHORITY` has no entry for it and §14 Q3 is open. The test asserts
  the block and asserts that the reason names the question. If somebody
  "fixes" the block by inventing an authority, this fails — which is the point.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytest

from app.domain.enums import (
    APPROVAL_AUTHORITY,
    ArtifactState,
    ArtifactType,
    LLMTask,
    Persona,
    ProgramStage,
    ProgramType,
)
from app.services.reporting.assembly import (
    AssessmentFacts,
    BatchFacts,
    ProgramFacts,
    ReportPeriod,
    StudentAttendanceFacts,
    TaskFacts,
    TrainerCostLine,
    TrainerCostSection,
    assemble_governance_report,
    summarise_college,
)
from app.services.reporting.drafts import (
    ReportAction,
    ReportDraft,
    approval_readiness,
    draft_college_summary,
    draft_governance_report,
)
from app.services.reporting.narration import Narration

JULY = ReportPeriod(start=dt.date(2026, 7, 1), end=dt.date(2026, 7, 31))
ACTOR = uuid.UUID("77777777-7777-7777-7777-777777777777")


def a_report(*, cost: TrainerCostSection | None = None) -> object:
    return assemble_governance_report(
        program=ProgramFacts(
            program_id=uuid.uuid4(),
            program_name="bCAP 2026",
            program_type=ProgramType.BCAP,
            stage=ProgramStage.ACTIVE_MONITORING,
            college_id=uuid.uuid4(),
            college_name="Malineni Lakshmaiah",
        ),
        period=JULY,
        batches=(BatchFacts(batch_id=uuid.uuid4(), name="CSE-A", student_count=60),),
        trainers=(),
        student_attendance=StudentAttendanceFacts(sessions=10, present=9, absent=1),
        assessments=AssessmentFacts(),
        observations=0,
        tasks=TaskFacts(),
        feedback=(),
        trainer_cost=cost,
    )


def a_narration() -> Narration:
    return Narration(
        body="One batch of 60 students.",
        task=LLMTask.GOVERNANCE_REPORT,
        model="a-frontier-model",
        prompt_chars=100,
        prompt_tokens=90,
        completion_tokens=10,
        latency_ms=5,
    )


def a_draft(**kwargs: object) -> ReportDraft:
    return draft_governance_report(
        a_report(),  # type: ignore[arg-type]
        artifact_id=uuid.uuid4(),
        actor_id=ACTOR,
        actor_persona=Persona.MANAGER,
        **kwargs,  # type: ignore[arg-type]
    )


# --- §14 Q3, carried and not answered ----------------------------------------


def test_a_governance_report_cannot_currently_be_approved() -> None:
    readiness = approval_readiness(ArtifactType.GOVERNANCE_REPORT)

    assert readiness.can_be_approved is False
    assert readiness.approvers == ()
    assert "Q3" in str(readiness.blocked_reason)


def test_the_approval_authority_table_still_has_no_governance_entry() -> None:
    """If this fails, somebody answered §14 Q3 — in code, and possibly not in life."""
    assert ArtifactType.GOVERNANCE_REPORT not in APPROVAL_AUTHORITY
    assert ArtifactType.PROGRAM_DOCUMENT not in APPROVAL_AUTHORITY


def test_a_type_with_an_authority_reports_its_approvers() -> None:
    readiness = approval_readiness(ArtifactType.REMUNERATION_SHEET)

    assert readiness.can_be_approved is True
    assert readiness.approvers == (Persona.SENIOR_MANAGER,)
    assert readiness.blocked_reason is None


# --- R4: DRAFT and nothing else ----------------------------------------------


def test_a_governance_draft_is_draft_and_unfrozen() -> None:
    draft = a_draft()

    assert draft.artifact.state is ArtifactState.DRAFT
    assert draft.artifact.version == 1
    assert draft.artifact.content_hash is None
    assert draft.artifact.is_frozen is False


def test_the_artifact_type_doubles_as_the_audit_entity_table() -> None:
    draft = a_draft()
    assert draft.event.entity_table == ArtifactType.GOVERNANCE_REPORT.value
    assert draft.event.entity_id == draft.artifact.artifact_id


def test_a_college_summary_is_filed_as_a_program_document_not_a_report() -> None:
    summary = summarise_college(
        college_id=uuid.uuid4(), college_name="Malineni", period=JULY, programs=[]
    )
    draft = draft_college_summary(summary, artifact_id=uuid.uuid4())

    assert draft.artifact.artifact_type is ArtifactType.PROGRAM_DOCUMENT
    assert draft.event.entity_table == ArtifactType.PROGRAM_DOCUMENT.value
    assert draft.approval.can_be_approved is False


def test_a_draft_constructed_in_any_other_state_is_refused() -> None:
    from app.services.approval.state_machine import Artifact

    approved = Artifact(
        artifact_type=ArtifactType.GOVERNANCE_REPORT,
        artifact_id=uuid.uuid4(),
        version=1,
        state=ArtifactState.APPROVED,
        payload={"a": 1},
        content_hash="deadbeef",
    )
    with pytest.raises(ValueError, match="ceiling is Draft"):
        ReportDraft(
            artifact=approved,
            event=a_draft().event,
            approval=approval_readiness(ArtifactType.GOVERNANCE_REPORT),
        )


# --- what gets frozen ---------------------------------------------------------


def test_the_narrative_is_inside_the_frozen_payload() -> None:
    """Prose outside the freeze is prose that can be rewritten after approval."""
    draft = a_draft(narrative=a_narration())

    assert draft.artifact.payload["narrative"] == "One batch of 60 students."
    assert draft.artifact.payload["narrative_model"] == "a-frontier-model"


def test_a_factless_draft_still_hashes_deterministically() -> None:
    report = a_report()
    first = draft_governance_report(report, artifact_id=uuid.uuid4())  # type: ignore[arg-type]
    second = draft_governance_report(report, artifact_id=uuid.uuid4())  # type: ignore[arg-type]

    # Different artifact ids, same content: the hash is over the payload only.
    assert first.artifact.payload_hash() == second.artifact.payload_hash()


def test_editing_the_narrative_changes_the_hash() -> None:
    report = a_report()
    plain = draft_governance_report(report, artifact_id=uuid.uuid4())  # type: ignore[arg-type]
    narrated = draft_governance_report(
        report,  # type: ignore[arg-type]
        artifact_id=uuid.uuid4(),
        narrative=a_narration(),
    )
    assert plain.artifact.payload_hash() != narrated.artifact.payload_hash()


# --- §11: the audit row -------------------------------------------------------


def test_the_audit_row_records_the_actor_the_action_and_the_hash() -> None:
    draft = a_draft(narrative=a_narration())
    after = draft.event.after or {}

    assert draft.event.action == ReportAction.DRAFTED
    assert draft.event.actor_id == ACTOR
    assert draft.event.actor_persona is Persona.MANAGER
    assert draft.event.before is None
    assert after["state"] == ArtifactState.DRAFT.value
    assert after["payload_hash"] == draft.artifact.payload_hash()
    assert after["narrative_chars"] == len("One batch of 60 students.")
    assert after["llm_task"] == LLMTask.GOVERNANCE_REPORT.value


def test_the_audit_row_says_whether_a_commercial_report_was_produced() -> None:
    """§4: "who generated a report containing trainer cost" must be answerable."""
    cost = TrainerCostSection(
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
    commercial = draft_governance_report(
        a_report(cost=cost),  # type: ignore[arg-type]
        artifact_id=uuid.uuid4(),
    )
    assert (commercial.event.after or {})["is_commercial"] is True
    assert (a_draft().event.after or {})["is_commercial"] is False


def test_a_scheduled_run_may_have_no_human_actor() -> None:
    """§11 allows a NULL actor only for a job with nobody behind it. Drafting is one."""
    draft = draft_governance_report(a_report(), artifact_id=uuid.uuid4())  # type: ignore[arg-type]
    assert draft.event.actor_id is None
