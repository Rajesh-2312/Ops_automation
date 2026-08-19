"""The ERM sync queue's HTTP surface — CLAUDE.md §10, "manual by design".

Five things are pinned about `app/api/erm.py`:

* **The three 1900 policies are reproduced in code.** `app/db/session.py`
  connects with BYPASSRLS, so nothing in the database refuses anybody here. An
  LDE Executive reads trainer cards for their campus and cannot create one; a
  college login gets 403 for everything; a Manager outside the cluster gets 403
  on a program card.
* **Confirm freezes the pack AND stamps the source record.** §10: "Record carries
  erm_synced_at, erm_synced_by". Both halves, one transaction, with the audit row.
* **The pack is live before confirm and frozen after.** A card that sat in the
  queue for a week hands over today's values; a confirmed card hands back the
  evidence, unregenerated.
* **One open card per record.** Mirrors the partial unique index, because a
  record edited five times must produce one job rather than a queue nobody reads.
* **Nothing transmits.** No transport import in the router, and every transition
  writes through `write_within()` so the state change and its attribution commit
  together.

The session is faked the way `test_comms_api.py` fakes it: it holds ORM instances,
answers `get()` by primary key and `execute()` by selected entity, and does not
evaluate WHERE clauses. A test therefore loads exactly the rows its scenario
needs — reach is asserted by which colleges are present, not by the join.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import TextClause

from app.api import erm
from app.core.audit import AuditEvent, AuditWriter, get_audit_writer
from app.core.security import Principal, get_principal
from app.db.models import College, Program, Trainer
from app.db.session import get_session
from app.domain.enums import DocStatus, ErmStatus, Persona, ProgramStage, ProgramType
from app.services.erm import ErmSubjectKind, ErmSyncState, ErmSyncTask

COLLEGE_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
OTHER_COLLEGE_ID = uuid.UUID("1111111a-1111-1111-1111-111111111111")
PROGRAM_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
TRAINER_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
TASK_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
USER_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")
ASSIGNEE_ID = uuid.UUID("66666666-6666-6666-6666-666666666666")

NOW = dt.datetime(2026, 8, 18, tzinfo=dt.UTC)
URL = f"/erm/tasks/{TASK_ID}"


# --- the fakes -------------------------------------------------------------------


class FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> FakeResult:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)


class FakeSession:
    """In-memory stand-in for `AsyncSession`, keyed by mapped class.

    WHERE clauses are not evaluated, so a test loads exactly the rows its
    scenario needs. `execute()` of a `text()` statement — the confirm path's
    source-record stamp — is recorded verbatim rather than run, which is what
    lets `test_confirm_stamps_the_source_record` assert on the SQL that would go
    to Postgres.
    """

    def __init__(self, *rows: Any) -> None:
        self.rows = list(rows)
        self.statements: list[tuple[str, dict[str, Any]]] = []
        self.commits = 0

    def _of(self, model: type[Any]) -> list[Any]:
        return [row for row in self.rows if isinstance(row, model)]

    async def get(self, model: type[Any], pk: Any) -> Any:
        return next((row for row in self._of(model) if row.id == pk), None)

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> FakeResult:
        if isinstance(statement, TextClause):
            self.statements.append((str(statement), dict(params or {})))
            return FakeResult([])
        description = statement.column_descriptions[0]
        entity = description["entity"]
        rows = self._of(entity)
        if description["expr"] is entity:
            return FakeResult(rows)
        return FakeResult([getattr(row, description["name"]) for row in rows])

    def add(self, obj: Any) -> None:
        self.rows.append(obj)

    async def commit(self) -> None:
        self.commits += 1


class RecordingAudit(AuditWriter):
    """Keeps the two write paths apart, so the router's choice is assertable."""

    def __init__(self) -> None:
        self.atomic: list[AuditEvent] = []
        self.best_effort: list[AuditEvent] = []

    async def write(self, event: AuditEvent) -> None:
        self.best_effort.append(event)

    async def write_within(self, session: AsyncSession, event: AuditEvent) -> None:
        self.atomic.append(event)


# --- fixtures ----------------------------------------------------------------------


def college(college_id: uuid.UUID = COLLEGE_ID, name: str = "ABC Engineering College") -> College:
    return College(id=college_id, name=name)


def program() -> Program:
    return Program(
        id=PROGRAM_ID,
        college_id=COLLEGE_ID,
        type=ProgramType.BCAP,
        name="bCAP CSE-A 2026",
        start_date=dt.date(2026, 7, 1),
        end_date=dt.date(2026, 7, 31),
        stage=ProgramStage.TRAINER_ONBOARDING,
        created_at=NOW,
        updated_at=NOW,
    )


def trainer() -> Trainer:
    return Trainer(
        id=TRAINER_ID,
        pan="BCDPV1234K",
        full_name="VEMA PRUDHVI SAI",
        email="vema@example.com",
        phone="9876543210",
        type="freelancer",
        work_order_status=DocStatus.SIGNED,
        zoho_id="ZH-1",
        erm_status=ErmStatus.NOT_PUSHED,
        created_at=NOW,
        updated_at=NOW,
    )


def task(
    *,
    kind: ErmSubjectKind = ErmSubjectKind.TRAINER,
    state: ErmSyncState = ErmSyncState.QUEUED,
    **overrides: Any,
) -> ErmSyncTask:
    row = ErmSyncTask(
        id=TASK_ID,
        subject_kind=kind,
        trainer_id=TRAINER_ID if kind is ErmSubjectKind.TRAINER else None,
        program_id=PROGRAM_ID if kind is ErmSubjectKind.PROGRAM else None,
        state=state,
        field_order_version=1,
        verified=False,
        created_at=NOW,
        updated_at=NOW,
    )
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


def principal(
    persona: Persona = Persona.MANAGER, colleges: frozenset[uuid.UUID] = frozenset({COLLEGE_ID})
) -> Principal:
    return Principal(user_id=USER_ID, persona=persona, college_ids=colleges)


def client(session: FakeSession, who: Principal, audit: AuditWriter | None = None) -> TestClient:
    app = FastAPI()
    app.include_router(erm.router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_principal] = lambda: who
    app.dependency_overrides[get_audit_writer] = lambda: audit or RecordingAudit()
    return TestClient(app)


@pytest.fixture
def audit() -> Iterator[RecordingAudit]:
    yield RecordingAudit()


# --- the three policies, in code -----------------------------------------------------


def test_an_lde_executive_may_not_file_a_trainer_card() -> None:
    """0400's split, mirrored: the roster belongs to the trainer pipeline.

    Sourcing and onboarding happen before any deployment exists, so trainer work
    is Senior Manager / Manager. An LDE Executive reads and does not act.
    """
    session = FakeSession(trainer(), college())
    response = client(session, principal(Persona.LDE_EXECUTIVE)).post(
        "/erm/tasks",
        json={"subject_kind": "trainer", "subject_id": str(TRAINER_ID)},
    )
    assert response.status_code == 403
    assert "Senior Manager" in response.json()["detail"]


def test_an_lde_executive_reads_a_trainer_card_on_their_campus() -> None:
    session = FakeSession(task(), trainer(), college())
    response = client(session, principal(Persona.LDE_EXECUTIVE)).get(URL)
    assert response.status_code == 200
    assert response.json()["task"]["subject_label"] == "VEMA PRUDHVI SAI"


def test_an_lde_executive_out_of_reach_gets_nothing() -> None:
    """The colleges in the session are the ones the trainer is deployed to; the
    principal reaches a different one."""
    session = FakeSession(task(), trainer(), college(OTHER_COLLEGE_ID, "XYZ Institute"))
    response = client(session, principal(Persona.LDE_EXECUTIVE)).get(URL)
    assert response.status_code == 403


def test_a_college_login_is_refused_everywhere() -> None:
    """§4 gives a college "published artifacts only". An internal job card to
    retype records into a portal is not a published artifact by any reading."""
    session = FakeSession(task(), trainer(), college())
    caller = client(session, principal(Persona.COLLEGE, frozenset({COLLEGE_ID})))
    assert caller.get(URL).status_code == 403
    assert caller.get("/erm/tasks").status_code == 403
    assert caller.post(f"{URL}/cancel", json={"reason": "x"}).status_code == 403


def test_a_manager_outside_the_cluster_cannot_touch_a_program_card() -> None:
    session = FakeSession(task(kind=ErmSubjectKind.PROGRAM), program(), college())
    response = client(session, principal(Persona.MANAGER, frozenset({OTHER_COLLEGE_ID}))).get(URL)
    assert response.status_code == 403


def test_the_queue_filters_per_row_rather_than_refusing_the_request() -> None:
    """A Manager and an LDE Executive both legitimately read this queue and should
    see different rows in it — which is what the SQL policies do."""
    session = FakeSession(task(), trainer(), college(OTHER_COLLEGE_ID, "XYZ Institute"))
    response = client(session, principal(Persona.LDE_EXECUTIVE)).get("/erm/tasks")
    assert response.status_code == 200
    assert response.json()["tasks"] == []


# --- filing a card ---------------------------------------------------------------------


def test_filing_a_card_queues_it_unassigned_with_no_pack(audit: RecordingAudit) -> None:
    session = FakeSession(trainer(), college())
    response = client(session, principal(), audit).post(
        "/erm/tasks",
        json={"subject_kind": "trainer", "subject_id": str(TRAINER_ID)},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["state"] == ErmSyncState.QUEUED.value
    assert body["assigned_to"] is None
    assert body["confirmed_at"] is None
    assert [event.action for event in audit.atomic] == ["erm.task_queued"]
    assert audit.best_effort == []
    assert session.commits == 1


def test_a_second_open_card_for_the_same_record_is_refused() -> None:
    """§10's requeue fires on every drifting edit. Without this a record edited
    five times in an afternoon produces five job cards, and a queue of five
    identical jobs is a queue nobody reads."""
    session = FakeSession(task(), trainer(), college())
    response = client(session, principal()).post(
        "/erm/tasks",
        json={"subject_kind": "trainer", "subject_id": str(TRAINER_ID)},
    )
    assert response.status_code == 409
    assert "one open card per record" in response.json()["detail"].lower()


def test_filing_a_card_for_a_record_that_does_not_exist_is_a_404() -> None:
    session = FakeSession(college())
    response = client(session, principal()).post(
        "/erm/tasks",
        json={"subject_kind": "trainer", "subject_id": str(TRAINER_ID)},
    )
    assert response.status_code == 404


# --- the pack ---------------------------------------------------------------------------


def test_an_open_card_renders_its_pack_live_and_in_order() -> None:
    """A card that sat in the queue for a week must hand over today's values."""
    session = FakeSession(task(), trainer(), college())
    body = client(session, principal()).get(URL).json()

    assert body["pack_is_frozen"] is False
    assert [entry["label"] for entry in body["pack"]["entries"]] == [
        "Trainer Name",
        "PAN",
        "Email",
        "Mobile",
        "Trainer Type",
        "Work Order Status",
        "ZOHO ID",
        "College Assigned",
    ]
    assert body["pack"]["entries"][0]["value"] == "VEMA PRUDHVI SAI"
    assert body["pack"]["paste_text"].startswith("Trainer Name\tVEMA PRUDHVI SAI")


def test_every_pack_response_says_the_order_is_unverified() -> None:
    """Nobody has seen ERM's form. A client must not be able to render a pack
    without having been told the order is provisional."""
    session = FakeSession(task(), trainer(), college())
    caller = client(session, principal())
    assert caller.get(URL).json()["pack"]["field_order_verified"] is False
    assert caller.get("/erm/tasks").json()["field_order_verified"] is False


def test_a_confirmed_card_returns_the_frozen_pack_not_a_fresh_one() -> None:
    """That row is evidence of what was pasted. Regenerating it would quietly
    rewrite the evidence."""
    frozen = [
        {
            "label": "Trainer Name",
            "source": "trainers.full_name",
            "value": "OLD NAME",
            "is_blank": False,
        }
    ]
    session = FakeSession(
        task(
            state=ErmSyncState.CONFIRMED,
            confirmed_by=USER_ID,
            confirmed_at=NOW,
            field_pack=frozen,
            source_snapshot={"trainers.full_name": "OLD NAME"},
        ),
        trainer(),
        college(),
    )
    body = client(session, principal()).get(URL).json()

    assert body["pack_is_frozen"] is True
    assert body["pack"]["entries"][0]["value"] == "OLD NAME"


def test_drift_is_reported_against_the_frozen_snapshot() -> None:
    """The database decides THAT a record drifted; this names WHICH field."""
    session = FakeSession(
        task(
            state=ErmSyncState.STALE,
            confirmed_by=USER_ID,
            confirmed_at=NOW,
            stale_at=NOW,
            field_pack=[],
            source_snapshot={"trainers.phone": "9000000000"},
        ),
        trainer(),
        college(),
    )
    body = client(session, principal()).get(URL).json()

    drifted = {item["source"]: item for item in body["drift"]}
    assert drifted["trainers.phone"]["was"] == "9000000000"
    assert drifted["trainers.phone"]["now"] == "9876543210"


# --- assign, confirm, cancel ----------------------------------------------------------------


def test_assign_names_the_person_and_audits_atomically(audit: RecordingAudit) -> None:
    session = FakeSession(task(), trainer(), college())
    response = client(session, principal(), audit).post(
        f"{URL}/assign", json={"assignee_id": str(ASSIGNEE_ID)}
    )

    assert response.status_code == 200
    assert response.json()["assigned_to"] == str(ASSIGNEE_ID)
    assert [event.action for event in audit.atomic] == ["erm.task_assigned"]


def test_confirm_freezes_the_pack_and_stamps_the_source_record(audit: RecordingAudit) -> None:
    """§10: "Record carries erm_synced_at, erm_synced_by".

    Both halves in one transaction — the card's frozen pack and the source row's
    stamp — plus the audit event that attributes the claim.
    """
    row = task()
    session = FakeSession(row, trainer(), college())
    response = client(session, principal(), audit).post(
        f"{URL}/confirm",
        json={"erm_external_id": "ERM-T-220", "verified": True, "remarks": "checked"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == ErmSyncState.CONFIRMED.value
    assert body["confirmed_by"] == str(USER_ID)
    assert body["erm_external_id"] == "ERM-T-220"
    assert body["verified"] is True

    assert row.field_pack is not None
    assert row.source_snapshot is not None
    assert row.source_snapshot["trainers.full_name"] == "VEMA PRUDHVI SAI"

    sql, params = session.statements[-1]
    assert "update public.trainers" in sql
    assert "erm_synced_at = :at" in sql
    assert "erm_synced_by = :by" in sql
    assert params["synced"] == ErmStatus.SYNCED.value
    assert params["by"] == USER_ID
    assert params["id"] == TRAINER_ID

    assert [event.action for event in audit.atomic] == ["erm.task_confirmed"]


def test_the_confirm_stamp_touches_no_watched_column() -> None:
    """The property that decides whether the detector survives contact with use.

    If the stamp wrote a column the 1900 trigger watches, every record would be
    stale one millisecond after every successful sync.
    `tests/unit/test_erm_drift.py` holds the same line from the SQL side.
    """
    from app.services.erm import watched_columns

    session = FakeSession(task(), trainer(), college())
    client(session, principal()).post(f"{URL}/confirm", json={})

    sql, _ = session.statements[-1]
    for column in watched_columns(ErmSubjectKind.TRAINER):
        assert f"{column} =" not in sql


def test_confirming_a_confirmed_card_is_a_conflict() -> None:
    session = FakeSession(
        task(
            state=ErmSyncState.CONFIRMED,
            confirmed_by=USER_ID,
            confirmed_at=NOW,
            field_pack=[],
        ),
        trainer(),
        college(),
    )
    response = client(session, principal()).post(f"{URL}/confirm", json={})
    assert response.status_code == 409


def test_cancel_requires_a_reason_on_the_wire() -> None:
    session = FakeSession(task(), trainer(), college())
    caller = client(session, principal())
    assert caller.post(f"{URL}/cancel", json={"reason": ""}).status_code == 422

    response = caller.post(f"{URL}/cancel", json={"reason": "Trainer withdrew"})
    assert response.status_code == 200
    assert response.json()["cancelled_reason"] == "Trainer withdrew"


def test_a_program_card_takes_the_program_pack_and_stamps_programs() -> None:
    row = task(kind=ErmSubjectKind.PROGRAM)
    session = FakeSession(row, program(), college())
    body = client(session, principal()).post(f"{URL}/confirm", json={}).json()

    assert body["state"] == ErmSyncState.CONFIRMED.value
    assert row.source_snapshot is not None
    assert row.source_snapshot["programs.start_date"] == "2026-07-01"
    assert "update public.programs" in session.statements[-1][0]


def test_an_unknown_card_is_a_404() -> None:
    session = FakeSession(trainer(), college())
    assert client(session, principal()).get(URL).status_code == 404


# --- R3 / §10, structurally --------------------------------------------------------------


def test_the_router_imports_no_transport(repo_root) -> None:  # noqa: ANN001
    """§10 forbids a scraper and R3 forbids a release capability. Neither can be
    added here without failing this test."""
    source = (repo_root / "app" / "api" / "erm.py").read_text(encoding="utf-8")
    for token in ("import httpx", "import requests", "smtplib", "selenium", "playwright"):
        assert token not in source


def test_no_route_name_claims_a_transmission() -> None:
    """The route set is the capability set. "Confirm" is a human claim; "send"
    would be a capability this system does not have."""
    for route in erm.router.routes:
        name = getattr(route, "name", "")
        assert not any(
            word in name for word in ("send", "post_message", "release", "push", "sync_now")
        )
