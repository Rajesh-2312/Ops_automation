"""`GET /payouts?month=YYYY-MM` — the work queue.

A separate file from `test_api_payouts.py` because it asserts a different thing.
That file drives the §6 fixtures through the engine over HTTP; this one asserts
the queue's three properties:

* **R5 before any row is read.** An LDE Executive, a College login and a trainer
  are refused with `session.queries == []` — the wall is in the router because
  the service-role connection bypasses RLS, and a wall that closes after the
  SELECT has already read a cluster's trainer names is not a wall.
* **Scope is reach, not persona alone.** A Manager with no assignments gets an
  empty queue and issues no query, which is what SQL's deny-by-default would say.
* **R7 on the wire.** The one money field, `payout.net`, is a string. The test
  parses the body with `parse_float` wired to fail, so a float anywhere in the
  response — not only in the field somebody remembered to assert — fails it.

The fake session is the one `test_api_payouts.py` documents: it holds ORM
instances, answers by mapped class, does not evaluate WHERE clauses, and raises
on any write. The last of those is how "the queue persists nothing" holds without
a test asserting it.
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

from app.api import payouts
from app.core.security import Principal, get_principal
from app.db.models import (
    Batch,
    College,
    Deployment,
    Program,
    RemunerationSheet,
    Trainer,
    TrainerAttendance,
    WorkOrder,
)
from app.db.session import get_session
from app.domain.enums import (
    AttendanceMark,
    DocStatus,
    Persona,
    ProgramType,
    RateBasis,
)

COLLEGE_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
PROGRAM_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
BATCH_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
DEPLOYMENT_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
TRAINER_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")
SHEET_ID = uuid.UUID("66666666-6666-6666-6666-666666666666")
USER_ID = uuid.UUID("77777777-7777-7777-7777-777777777777")

MONTH = "2026-07"
JULY_START = dt.date(2026, 7, 1)
JULY_END = dt.date(2026, 7, 31)


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
    """In-memory `AsyncSession` stand-in keyed by mapped class. Writes raise."""

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
        raise AssertionError("the payout queue tried to write (R4)")

    async def commit(self) -> None:  # pragma: no cover
        raise AssertionError("the payout queue tried to commit (R4)")


def scenario(
    *,
    deployment_start: dt.date | None = dt.date(2026, 7, 26),
    deployment_end: dt.date | None = None,
    program_type: ProgramType = ProgramType.BCAP,
    marks: dict[dt.date, AttendanceMark] | None = None,
    work_order: bool = True,
    sheet: RemunerationSheet | None = None,
) -> FakeSession:
    rows: list[Any] = [
        College(id=COLLEGE_ID, name="Malineni Lakshmaiah"),
        Program(id=PROGRAM_ID, college_id=COLLEGE_ID, type=program_type, name="bCAP 2026"),
        Batch(id=BATCH_ID, program_id=PROGRAM_ID, name="CSE-A"),
        Deployment(
            id=DEPLOYMENT_ID,
            trainer_id=TRAINER_ID,
            batch_id=BATCH_ID,
            start_date=deployment_start,
            end_date=deployment_end,
        ),
        Trainer(
            id=TRAINER_ID,
            pan="VEMAP1234K",
            full_name="VEMA PRUDHVI SAI",
            type="freelance",
            work_order_status=DocStatus.SIGNED,
            erm_status="synced",
        ),
    ]
    for day, mark in (marks or {}).items():
        rows.append(
            TrainerAttendance(
                id=uuid.uuid4(), deployment_id=DEPLOYMENT_ID, mark_date=day, mark=mark
            )
        )
    if work_order:
        rows.append(
            WorkOrder(
                id=uuid.uuid4(),
                trainer_id=TRAINER_ID,
                program_id=PROGRAM_ID,
                rate=Decimal("80000"),
                rate_basis=RateBasis.PER_MONTH,
                valid_from=dt.date(2026, 4, 1),
                valid_to=dt.date(2027, 3, 31),
                status=DocStatus.SIGNED,
            )
        )
    if sheet is not None:
        rows.append(sheet)
    return FakeSession(*rows)


def client(session: FakeSession, principal: Principal) -> TestClient:
    app = FastAPI()
    app.include_router(payouts.router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_principal] = lambda: principal
    return TestClient(app)


def a_principal(persona: Persona, *, reach: frozenset[uuid.UUID] | None = None) -> Principal:
    return Principal(
        user_id=USER_ID,
        persona=persona,
        college_ids=frozenset({COLLEGE_ID}) if reach is None else reach,
    )


def get_queue(session: FakeSession, principal: Principal, month: str = MONTH) -> Any:
    return client(session, principal).get("/payouts", params={"month": month})


# --- R5: the wall closes before any query ------------------------------------


@pytest.mark.parametrize(
    "persona",
    [Persona.LDE_EXECUTIVE, Persona.COLLEGE, Persona.TRAINER],
)
def test_queue_refuses_non_commercial_personas_before_reading_anything(persona: Persona) -> None:
    session = scenario()
    response = get_queue(session, a_principal(persona))

    assert response.status_code == 403
    assert session.queries == []


@pytest.mark.parametrize("persona", [Persona.MANAGER, Persona.SENIOR_MANAGER])
def test_queue_is_open_to_the_commercials_personas(persona: Persona) -> None:
    response = get_queue(scenario(), a_principal(persona))
    assert response.status_code == 200


def test_no_reach_is_an_empty_queue_and_no_query() -> None:
    session = scenario()
    response = get_queue(session, a_principal(Persona.MANAGER, reach=frozenset()))

    assert response.status_code == 200
    assert response.json()["items"] == []
    assert session.queries == []


# --- what a row carries -------------------------------------------------------


def test_queue_row_carries_the_triage_state() -> None:
    marks = {dt.date(2026, 7, day): AttendanceMark.PRESENT for day in range(26, 32)}
    response = get_queue(scenario(marks=marks), a_principal(Persona.MANAGER))

    body = response.json()
    assert body["month"] == MONTH
    assert body["period_start"] == "2026-07-01"
    assert body["period_end"] == "2026-07-31"
    assert body["count"] == 1

    item = body["items"][0]
    assert item["deployment_id"] == str(DEPLOYMENT_ID)
    assert item["trainer_pan"] == "VEMAP1234K"
    assert item["college_name"] == "Malineni Lakshmaiah"
    assert item["batch_name"] == "CSE-A"
    # The month clipped to the deployment window — the period to preview with.
    assert item["period_start"] == "2026-07-26"
    assert item["period_end"] == "2026-07-31"
    assert item["attendance"]["is_complete"] is True
    assert item["attendance"]["period_days"] == 6
    assert item["work_order_signed"] is True
    assert item["payout"] is None


def test_unmarked_days_are_reported_rather_than_assumed() -> None:
    """§5's asymmetry: a payable-day count without its completeness is unreviewable."""
    marks = {dt.date(2026, 7, 26): AttendanceMark.PRESENT}
    item = get_queue(scenario(marks=marks), a_principal(Persona.MANAGER)).json()["items"][0]

    assert item["attendance"]["marked"] == 1
    assert item["attendance"]["unmarked"] == 5
    assert item["attendance"]["is_complete"] is False


def test_an_unsigned_work_order_shows_as_not_covered() -> None:
    item = get_queue(scenario(work_order=False), a_principal(Persona.MANAGER)).json()["items"][0]
    assert item["work_order_signed"] is False


def test_an_existing_payout_is_surfaced_with_its_state() -> None:
    sheet = RemunerationSheet(
        id=SHEET_ID,
        trainer_id=TRAINER_ID,
        program_id=PROGRAM_ID,
        period_start=JULY_START,
        period_end=JULY_END,
        net_amount=Decimal("14035"),
        invoice_no="VEMA/26-27/JUL1",
        currency="INR",
        payout_status=DocStatus.SENT,
    )
    item = get_queue(scenario(sheet=sheet), a_principal(Persona.MANAGER)).json()["items"][0]

    assert item["payout"]["sheet_id"] == str(SHEET_ID)
    assert item["payout"]["payout_status"] == DocStatus.SENT.value
    assert item["payout"]["invoice_no"] == "VEMA/26-27/JUL1"
    # R7: a string, never a JSON float.
    assert item["payout"]["net"] == "14035"


def test_no_amount_is_ever_a_json_float() -> None:
    sheet = RemunerationSheet(
        id=SHEET_ID,
        trainer_id=TRAINER_ID,
        program_id=PROGRAM_ID,
        period_start=JULY_START,
        period_end=JULY_END,
        net_amount=Decimal("14035.50"),
        currency="INR",
        payout_status=DocStatus.SENT,
    )
    response = get_queue(scenario(sheet=sheet), a_principal(Persona.MANAGER))

    def _refuse(raw: str) -> float:  # pragma: no cover - only runs on failure
        raise AssertionError(f"a float {raw!r} reached the wire (CLAUDE.md R7)")

    json.loads(response.text, parse_float=_refuse)


# --- which deployments are candidates ----------------------------------------


def test_a_deployment_that_ended_before_the_month_is_not_a_candidate() -> None:
    session = scenario(deployment_start=dt.date(2026, 5, 1), deployment_end=dt.date(2026, 6, 30))
    assert get_queue(session, a_principal(Persona.MANAGER)).json()["items"] == []


def test_an_open_ended_deployment_runs_the_whole_month() -> None:
    session = scenario(deployment_start=dt.date(2026, 1, 1), deployment_end=None)
    item = get_queue(session, a_principal(Persona.MANAGER)).json()["items"][0]

    assert item["period_start"] == "2026-07-01"
    assert item["period_end"] == "2026-07-31"


def test_a_malformed_month_is_a_422_that_says_what_a_month_is() -> None:
    response = get_queue(scenario(), a_principal(Persona.MANAGER), month="July 2026")
    assert response.status_code == 422
    assert "YYYY-MM" in response.text
