"""SEC-07 — `GET /comms/messages` must limit what the caller may SEE.

The listing used to hand `LIMIT` to Postgres and then drop commercial rows in
Python::

    rows = ... .order_by(created_at.desc()).limit(limit)
    visible = [row for row in rows if principal.can_see_commercials or not row.is_commercial]

so `limit` meant "rows considered", not "rows returned". An LDE Executive asking
for fifty messages on a program whose fifty newest are payout chases received an
EMPTY queue while fifty operational messages sat behind them, with no indication
that anything had been withheld.

This is under-disclosure, not over-disclosure: the wall itself never moved and no
commercial row was ever returned to a persona that may not see one. It is filed
as a correctness defect for that reason. It still matters — this queue is what a
Manager or an LDE Executive reads to decide what to work next, and a queue that
silently hides work is a queue people stop trusting.

WHY THESE TESTS FAIL WITHOUT THE FIX
------------------------------------
`test_an_lde_executive_gets_a_full_page_of_visible_messages` builds sixty rows:
the fifty NEWEST are commercial and the ten oldest are operational, then asks for
ten. Before the fix the database returned the fifty newest, Python dropped all
fifty, and the response held **zero** messages while ten existed. After the fix
the predicate is in the `WHERE`, so the ten come back. `test_the_wall_is_applied_in_sql`
asserts the same thing structurally, on the statement the router actually issues.

The fake session evaluates only the three things this endpoint depends on — the
`is_commercial` predicate, `ORDER BY created_at DESC` and `LIMIT` — in the order
Postgres applies them. It deliberately does NOT evaluate `program_id`, which the
router has already checked with `require_college_reach()` before any row is read.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import comms
from app.core.audit import AuditEvent, AuditWriter, get_audit_writer
from app.core.security import Principal, get_principal
from app.db.models import College, CommsMessage, Program
from app.db.session import get_session
from app.domain.enums import ArtifactState, Persona, ProgramType
from app.services.comms import CommsChannel, CommsRecipientKind

COLLEGE_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
PROGRAM_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
USER_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")

NOW = dt.datetime(2026, 8, 19, tzinfo=dt.UTC)
URL = f"/comms/messages?program_id={PROGRAM_ID}"


# --- fakes ---------------------------------------------------------------------


class FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> FakeResult:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)


class QueueSession:
    """An in-memory stand-in that applies filter, then order, then limit.

    That order is the whole finding. `_limit` and `whereclause` are read off the
    statement the router built rather than simulated from the router's arguments,
    so the test is measuring what would actually reach Postgres.
    """

    def __init__(self, *rows: Any) -> None:
        self.rows = list(rows)
        self.wheres: list[str] = []

    async def get(self, model: type[Any], pk: Any) -> Any:
        return next((r for r in self.rows if isinstance(r, model) and r.id == pk), None)

    async def execute(self, statement: Any) -> FakeResult:
        entity = statement.column_descriptions[0]["entity"]
        where = "" if statement.whereclause is None else str(statement.whereclause)
        self.wheres.append(where)

        rows = [r for r in self.rows if isinstance(r, entity)]
        if "is_commercial" in where:
            rows = [r for r in rows if not r.is_commercial]
        rows.sort(key=lambda r: r.created_at, reverse=True)
        if statement._limit is not None:
            rows = rows[: statement._limit]
        return FakeResult(rows)


class SilentAudit(AuditWriter):
    async def write(self, event: AuditEvent) -> None:  # pragma: no cover - listing is a pure read
        raise AssertionError("a pure read must not raise an audit row")

    async def write_within(self, session: Any, event: AuditEvent) -> None:  # pragma: no cover
        raise AssertionError("a pure read must not raise an audit row")


def message(*, is_commercial: bool, created_at: dt.datetime) -> CommsMessage:
    """One queue row. Only the fields the listing orders, filters and serialises."""
    return CommsMessage(
        id=uuid.uuid4(),
        program_id=PROGRAM_ID,
        channel=CommsChannel.EMAIL,
        recipient_kind=CommsRecipientKind.COLLEGE,
        recipient_ref="principal@malineni.edu",
        recipient_name="Principal",
        template_key="attendance.complete.v1",
        template_body="Hello {name}",
        template_values={"name": "Rao"},
        subject="July attendance",
        body="Hello Rao",
        diff={"version": 1, "identical": False, "hunks": []},
        is_commercial=is_commercial,
        related_artifact_type=None,
        related_artifact_id=None,
        state=ArtifactState.DRAFT,
        content_hash=None,
        version=1,
        supersedes_id=None,
        superseded_at=None,
        created_by=USER_ID,
        created_at=created_at,
        submitted_by=None,
        submitted_at=None,
        approved_by=None,
        approved_at=None,
        released_by=None,
        released_at=None,
        notes=None,
        updated_at=created_at,
    )


def queue(*, commercial: int, operational: int) -> QueueSession:
    """A program whose `commercial` newest messages sit above `operational` older ones.

    The interleaving is the scenario: filtering after `LIMIT` returns nothing at
    all when the newest page is entirely commercial, which is exactly what a busy
    payout week looks like on a real program.
    """
    rows: list[Any] = [
        College(id=COLLEGE_ID, name="Malineni Lakshmaiah"),
        Program(id=PROGRAM_ID, college_id=COLLEGE_ID, type=ProgramType.BCAP, name="bCAP 2026"),
    ]
    rows += [
        message(is_commercial=False, created_at=NOW + dt.timedelta(minutes=index))
        for index in range(operational)
    ]
    rows += [
        message(is_commercial=True, created_at=NOW + dt.timedelta(hours=1, minutes=index))
        for index in range(commercial)
    ]
    return QueueSession(*rows)


def principal(persona: Persona) -> Principal:
    return Principal(
        user_id=USER_ID, persona=persona, college_ids=frozenset({COLLEGE_ID}), is_admin=False
    )


def client(session: QueueSession, who: Principal) -> TestClient:
    app = FastAPI()
    app.include_router(comms.router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_principal] = lambda: who
    app.dependency_overrides[get_audit_writer] = lambda: SilentAudit()
    return TestClient(app)


# --- the finding ---------------------------------------------------------------


def test_an_lde_executive_gets_a_full_page_of_visible_messages() -> None:
    """Ten visible messages exist and ten were asked for, so ten come back.

    Before the fix: the fifty newest rows were commercial, the database returned
    those, Python dropped every one, and the caller saw an empty queue.
    """
    session = queue(commercial=50, operational=10)
    response = client(session, principal(Persona.LDE_EXECUTIVE)).get(f"{URL}&limit=10")

    assert response.status_code == 200
    assert len(response.json()["messages"]) == 10


def test_the_page_is_still_capped_at_the_limit() -> None:
    """The fix must not turn the limit into a suggestion."""
    session = queue(commercial=0, operational=25)
    response = client(session, principal(Persona.LDE_EXECUTIVE)).get(f"{URL}&limit=10")

    assert len(response.json()["messages"]) == 10


def test_the_wall_is_applied_in_sql_not_after_the_limit() -> None:
    """Structural: the predicate must be in the statement the router issues.

    A behavioural test alone would pass again if somebody moved the filter back
    into Python and merely over-fetched. This one names where the predicate has to
    live, because `LIMIT` is applied by the database and so must the filter be.
    """
    session = queue(commercial=2, operational=2)
    client(session, principal(Persona.LDE_EXECUTIVE)).get(URL)

    listing = [where for where in session.wheres if "comms_messages" in where]
    assert listing, "the listing issued no statement against comms_messages"
    assert any("is_commercial" in where for where in listing)


def test_a_manager_still_sees_commercial_rows() -> None:
    """The control. The wall is a property of the ROW and of the PERSONA.

    A Manager clears `can_see_commercials()`, so no predicate is added and the
    payout chases stay in their queue — 1700's two policies, both halves.
    """
    session = queue(commercial=6, operational=4)
    response = client(session, principal(Persona.MANAGER)).get(f"{URL}&limit=10")

    body = response.json()["messages"]
    assert len(body) == 10
    assert sum(1 for row in body if row["is_commercial"]) == 6

    assert all("is_commercial" not in where for where in session.wheres)


@pytest.mark.parametrize("persona", [Persona.LDE_EXECUTIVE, Persona.MANAGER])
def test_no_commercial_row_ever_reaches_a_persona_behind_the_wall(persona: Persona) -> None:
    """R5, restated for the listing: the fix is about the count, never the contents.

    Asserted for both personas together so the test says what the difference IS:
    the LDE Executive sees none, the Manager sees them all, and neither number is
    a function of how many rows the database happened to return first.
    """
    session = queue(commercial=30, operational=30)
    body = client(session, principal(persona)).get(f"{URL}&limit=50").json()["messages"]

    commercial = [row for row in body if row["is_commercial"]]
    if persona is Persona.LDE_EXECUTIVE:
        assert commercial == []
        assert len(body) == 30, "all thirty operational messages are visible"
    else:
        assert len(body) == 50
