"""`app/api/reports.py` — Phase 6's HTTP surface. Draft ceiling, walls in code.

The session is faked the way `test_api_payouts.py` fakes it: it holds ORM
instances, answers `execute()` by the selected entity, does NOT evaluate WHERE
clauses (a test loads exactly the rows its scenario has), and RAISES on any
write. That last property is how "no reporting endpoint persists or transitions
anything" (R4, §8) is asserted for free on every test in this file.

The narrator is overridden with a queued `FakeLLM` — no test here may reach
OpenRouter, and the interesting cases are the ones where the model says something
wrong, which a live model cannot be made to do on demand.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import reports
from app.core.audit import AuditEvent, AuditWriter, get_audit_writer
from app.core.security import Principal, get_principal
from app.db.models import (
    Assessment,
    AttendanceRecord,
    Batch,
    College,
    Deployment,
    Feedback,
    Program,
    RemunerationSheet,
    Student,
    Task,
    Trainer,
    TrainerAttendance,
)
from app.db.session import get_session
from app.domain.enums import (
    ArtifactState,
    AttendanceMark,
    DocStatus,
    Persona,
    ProgramStage,
    ProgramType,
    TaskCadence,
    TaskStatus,
)
from app.services.reporting.narration import ReportNarrator
from tests.unit.agent_fakes import FakeLLM

COLLEGE_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
PROGRAM_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
BATCH_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
DEPLOYMENT_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
TRAINER_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")
USER_ID = uuid.UUID("77777777-7777-7777-7777-777777777777")
OTHER_COLLEGE_ID = uuid.UUID("99999999-9999-9999-9999-999999999999")

PERIOD = {"period_start": "2026-07-01", "period_end": "2026-07-31"}
GOVERNANCE_URL = f"/reports/programs/{PROGRAM_ID}/governance"


class FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalar_one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None

    def scalars(self) -> FakeResult:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)


class FakeSession:
    def __init__(self, *rows: Any) -> None:
        self.rows = list(rows)
        self.queries: list[str] = []

    async def get(self, model: type[Any], pk: Any) -> Any:
        self.queries.append(model.__name__)
        return next((r for r in self.rows if isinstance(r, model) and r.id == pk), None)

    async def execute(self, statement: Any) -> FakeResult:
        entity = statement.column_descriptions[0]["entity"]
        self.queries.append(entity.__name__)
        return FakeResult([r for r in self.rows if isinstance(r, entity)])

    def add(self, _obj: Any) -> None:  # pragma: no cover
        raise AssertionError("a reporting endpoint tried to write (R4, §8)")

    async def commit(self) -> None:  # pragma: no cover
        raise AssertionError("a reporting endpoint tried to commit (R4, §8)")


class RecordingAudit(AuditWriter):
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def write(self, event: AuditEvent) -> None:
        self.events.append(event)


def scenario(*, sheets: bool = False, feedback: bool = True) -> FakeSession:
    """One college, one program, one batch, one trainer, July marks 1–30 of 31."""
    rows: list[Any] = [
        College(id=COLLEGE_ID, name="Malineni Lakshmaiah"),
        Program(
            id=PROGRAM_ID,
            college_id=COLLEGE_ID,
            type=ProgramType.BCAP,
            name="bCAP 2026",
            stage=ProgramStage.ACTIVE_MONITORING,
            start_date=dt.date(2026, 6, 1),
            end_date=dt.date(2026, 12, 31),
        ),
        Batch(id=BATCH_ID, program_id=PROGRAM_ID, name="CSE-A", expected_student_count=60),
        Deployment(id=DEPLOYMENT_ID, trainer_id=TRAINER_ID, batch_id=BATCH_ID),
        Trainer(
            id=TRAINER_ID,
            pan="VEMAP1234K",
            full_name="VEMA PRUDHVI SAI",
            type="freelance",
            work_order_status=DocStatus.SIGNED,
            erm_status="synced",
        ),
        Task(
            id=uuid.uuid4(),
            program_id=PROGRAM_ID,
            stage=ProgramStage.ACTIVE_MONITORING,
            title="Collect feedback",
            status=TaskStatus.PENDING,
            cadence=TaskCadence.ONE_TIME,
            due_date=dt.date(2026, 7, 10),
        ),
        Assessment(
            id=uuid.uuid4(),
            batch_id=BATCH_ID,
            title="Mid-term",
            conducted_on=dt.date(2026, 7, 15),
            report_url="https://example.invalid/report",
        ),
    ]
    rows += [
        Student(id=uuid.uuid4(), batch_id=BATCH_ID, credentials_status="issued") for _ in range(3)
    ]
    rows += [
        TrainerAttendance(
            id=uuid.uuid4(),
            deployment_id=DEPLOYMENT_ID,
            mark_date=dt.date(2026, 7, day),
            mark=AttendanceMark.PRESENT,
        )
        for day in range(1, 31)
    ]
    rows += [
        AttendanceRecord(
            id=uuid.uuid4(),
            deployment_id=DEPLOYMENT_ID,
            session_date=dt.date(2026, 7, 5),
            status=status,
        )
        for status in ("present", "present", "present", "absent")
    ]
    if feedback:
        rows.append(
            Feedback(
                id=uuid.uuid4(),
                batch_id=BATCH_ID,
                source="gform",
                collected_on=dt.date(2026, 7, 20),
                summary_score=Decimal("4.25"),
                response_count=40,
            )
        )
    if sheets:
        rows.append(
            RemunerationSheet(
                id=uuid.uuid4(),
                trainer_id=TRAINER_ID,
                program_id=PROGRAM_ID,
                period_start=dt.date(2026, 7, 1),
                period_end=dt.date(2026, 7, 31),
                net_amount=Decimal("58500"),
                invoice_no="VEMA/26-27/JUL1",
                currency="INR",
                payout_status=DocStatus.SENT,
            )
        )
    return FakeSession(*rows)


def a_principal(persona: Persona, *, reach: frozenset[uuid.UUID] | None = None) -> Principal:
    return Principal(
        user_id=USER_ID,
        persona=persona,
        college_ids=frozenset({COLLEGE_ID}) if reach is None else reach,
    )


def client(
    session: FakeSession,
    principal: Principal,
    *,
    audit: RecordingAudit | None = None,
    llm: FakeLLM | None = None,
) -> TestClient:
    app = FastAPI()
    app.include_router(reports.router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_principal] = lambda: principal
    app.dependency_overrides[get_audit_writer] = lambda: audit or RecordingAudit()
    app.dependency_overrides[reports.get_narrator] = lambda: ReportNarrator(
        llm=llm or FakeLLM(responses=[])
    )
    return TestClient(app)


# --- §4 / R5: who may read what ----------------------------------------------


@pytest.mark.parametrize("persona", [Persona.TRAINER, Persona.COLLEGE])
def test_a_governance_report_is_internal_only(persona: Persona) -> None:
    """§4: a trainer sees their own deployment; a college sees PUBLISHED artifacts."""
    response = client(scenario(), a_principal(persona)).post(GOVERNANCE_URL, json=PERIOD)
    assert response.status_code == 403


def test_a_program_outside_the_callers_reach_is_refused() -> None:
    session = scenario()
    principal = a_principal(Persona.MANAGER, reach=frozenset({OTHER_COLLEGE_ID}))
    assert client(session, principal).post(GOVERNANCE_URL, json=PERIOD).status_code == 403


def test_an_lde_executive_may_pull_delivery_reporting() -> None:
    """Delivery is not commercial (§4) — this is their daily work."""
    response = client(scenario(), a_principal(Persona.LDE_EXECUTIVE)).post(
        GOVERNANCE_URL, json=PERIOD
    )
    assert response.status_code == 200
    assert response.json()["is_commercial"] is False


def test_an_lde_executive_asking_for_trainer_cost_is_refused_before_any_money_is_read() -> None:
    session = scenario(sheets=True)
    response = client(session, a_principal(Persona.LDE_EXECUTIVE)).post(
        GOVERNANCE_URL, json={**PERIOD, "include_trainer_cost": True}
    )

    assert response.status_code == 403
    assert "RemunerationSheet" not in session.queries


@pytest.mark.parametrize("persona", [Persona.MANAGER, Persona.SENIOR_MANAGER])
def test_the_commercials_personas_get_the_cost_section(persona: Persona) -> None:
    response = client(scenario(sheets=True), a_principal(persona)).post(
        GOVERNANCE_URL, json={**PERIOD, "include_trainer_cost": True}
    )

    body = response.json()
    assert response.status_code == 200
    assert body["is_commercial"] is True
    assert body["trainer_cost"]["payout_count"] == 1
    # R7: money leaves as a string.
    assert body["trainer_cost"]["lines"][0]["net"] == "58500"
    # R2: no total, ever, computed outside the engine.
    assert body["trainer_cost"]["total"] is None


def test_no_amount_is_ever_a_json_float() -> None:
    response = client(scenario(sheets=True), a_principal(Persona.MANAGER)).post(
        GOVERNANCE_URL, json={**PERIOD, "include_trainer_cost": True}
    )

    def _refuse(raw: str) -> float:  # pragma: no cover - only runs on failure
        raise AssertionError(f"a float {raw!r} reached the wire (CLAUDE.md R7)")

    json.loads(response.text, parse_float=_refuse)


# --- R4 / §8: DRAFT, and nothing but ------------------------------------------


def test_a_governance_report_comes_back_as_a_draft_that_cannot_be_approved() -> None:
    response = client(scenario(), a_principal(Persona.MANAGER)).post(GOVERNANCE_URL, json=PERIOD)
    body = response.json()

    assert body["artifact_state"] == ArtifactState.DRAFT.value
    assert response.headers["X-Artifact-State"] == ArtifactState.DRAFT.value
    assert body["approval"]["can_be_approved"] is False
    # §14 Q3 is carried, not answered.
    assert "Q3" in body["approval"]["blocked_reason"]


def test_drafting_writes_one_audit_row_naming_the_actor_and_the_kind_of_report() -> None:
    audit = RecordingAudit()
    client(scenario(sheets=True), a_principal(Persona.MANAGER), audit=audit).post(
        GOVERNANCE_URL, json={**PERIOD, "include_trainer_cost": True}
    )

    assert len(audit.events) == 1
    event = audit.events[0]
    assert event.action == "report.drafted"
    assert event.actor_id == USER_ID
    assert (event.after or {})["is_commercial"] is True


# --- R1: the figures come from the rows --------------------------------------


def test_the_facts_are_read_from_the_systems_of_record() -> None:
    body = client(scenario(), a_principal(Persona.MANAGER)).post(GOVERNANCE_URL, json=PERIOD).json()

    assert body["batch_count"] == 1
    assert body["trainer_count"] == 1
    # 30 of July's 31 days marked — §5's silent failure, surfaced.
    assert body["incomplete_tracksheet_count"] == 1
    assert body["student_attendance_percent"] == "75.0"
    assert body["assessments_conducted"] == 1
    assert body["tasks_open"] == 1
    assert body["tasks_overdue"] == 1
    assert body["feedback"]["average_score"] == "4.25"


def test_overdue_is_measured_against_a_supplied_date_not_today() -> None:
    """A report regenerated for an old period must report what was true then."""
    body = (
        client(scenario(), a_principal(Persona.MANAGER))
        .post(GOVERNANCE_URL, json={**PERIOD, "as_of": "2026-07-05"})
        .json()
    )
    assert body["tasks_overdue"] == 0


def test_a_narrative_stating_an_invented_figure_refuses_the_draft() -> None:
    llm = FakeLLM(responses=["Attendance was around 97 percent this month."])
    response = client(scenario(), a_principal(Persona.MANAGER), llm=llm).post(
        GOVERNANCE_URL, json={**PERIOD, "include_narrative": True}
    )

    assert response.status_code == 422
    assert "R1" in response.text


def test_a_grounded_narrative_is_returned_with_its_telemetry() -> None:
    llm = FakeLLM(responses=["1 batch, 1 trainer, 1 assessment conducted."])
    body = (
        client(scenario(), a_principal(Persona.MANAGER), llm=llm)
        .post(GOVERNANCE_URL, json={**PERIOD, "include_narrative": True})
        .json()
    )

    assert body["narrative"]["body"].startswith("1 batch")
    assert body["narrative"]["llm_task"] == "governance_report"
    assert body["narrative"]["completion_tokens"] > 0


def test_no_model_is_called_when_no_narrative_was_asked_for() -> None:
    llm = FakeLLM(responses=[])  # would raise if called
    assert (
        client(scenario(), a_principal(Persona.MANAGER), llm=llm)
        .post(GOVERNANCE_URL, json=PERIOD)
        .status_code
        == 200
    )


# --- feedback synthesis -------------------------------------------------------


def test_feedback_synthesis_is_available_to_an_lde_executive() -> None:
    response = client(scenario(), a_principal(Persona.LDE_EXECUTIVE)).get(
        f"/reports/programs/{PROGRAM_ID}/feedback", params=PERIOD
    )

    body = response.json()
    assert response.status_code == 200
    assert body["synthesis"]["collections"] == 1
    assert body["synthesis"]["scored_collections"] == 1
    assert body["synthesis"]["average_score"] == "4.25"
    assert body["entries"][0]["source"] == "gform"


def test_feedback_synthesis_writes_no_audit_row() -> None:
    """§11 wants a row per state transition. A read is not one."""
    audit = RecordingAudit()
    client(scenario(), a_principal(Persona.MANAGER), audit=audit).get(
        f"/reports/programs/{PROGRAM_ID}/feedback", params=PERIOD
    )
    assert audit.events == []


# --- college summary ----------------------------------------------------------


def test_a_college_summary_lists_programs_and_carries_no_money() -> None:
    response = client(scenario(sheets=True), a_principal(Persona.LDE_EXECUTIVE)).get(
        f"/reports/colleges/{COLLEGE_ID}/summary", params=PERIOD
    )

    body = response.json()
    assert response.status_code == 200
    assert body["artifact_state"] == ArtifactState.DRAFT.value
    assert body["programs"][0]["program_name"] == "bCAP 2026"
    assert body["programs"][0]["incomplete_tracksheets"] == 1
    assert "net" not in response.text
    assert "58500" not in response.text


def test_a_college_outside_reach_is_refused() -> None:
    principal = a_principal(Persona.MANAGER, reach=frozenset({OTHER_COLLEGE_ID}))
    response = client(scenario(), principal).get(
        f"/reports/colleges/{COLLEGE_ID}/summary", params=PERIOD
    )
    assert response.status_code == 403


# --- input validation ---------------------------------------------------------


def test_a_reversed_period_is_a_422() -> None:
    response = client(scenario(), a_principal(Persona.MANAGER)).post(
        GOVERNANCE_URL, json={"period_start": "2026-07-31", "period_end": "2026-07-01"}
    )
    assert response.status_code == 422


def test_an_unknown_field_is_a_422_not_a_silent_default() -> None:
    response = client(scenario(), a_principal(Persona.MANAGER)).post(
        GOVERNANCE_URL, json={**PERIOD, "include_trainer_costs": True}
    )
    assert response.status_code == 422
